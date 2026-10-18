"""Managed GIF assets with metadata/resource validation before Qt decoding."""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct

MAX_BYTES = 16 * 1024 * 1024
MAX_SIDE = 2048
MAX_FRAMES = 500
MAX_TOTAL_PIXELS = 128 * 1024 * 1024


def inspect_gif(data: bytes) -> dict[str, int]:
    if len(data) > MAX_BYTES or len(data) < 14 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("GIF must be valid and no larger than 16 MiB")
    width, height, packed = struct.unpack_from("<HHB", data, 6)
    if not 0 < width <= MAX_SIDE or not 0 < height <= MAX_SIDE:
        raise ValueError("GIF dimensions must be between 1 and 2048 pixels")
    pos = 13 + (3 * (2 ** ((packed & 7) + 1)) if packed & 0x80 else 0)
    frames, pixels, min_delay = 0, 0, 1000

    def blocks(offset):
        while True:
            if offset >= len(data):
                raise ValueError("Truncated GIF data")
            count = data[offset]
            offset += 1
            if count == 0:
                return offset
            offset += count
            if offset > len(data):
                raise ValueError("Truncated GIF block")

    while pos < len(data):
        tag = data[pos]
        pos += 1
        if tag == 0x3B:
            if not frames:
                raise ValueError("GIF contains no frames")
            return {"width": width, "height": height, "frames": frames, "min_delay_ms": min_delay}
        if tag == 0x21:
            if pos >= len(data):
                raise ValueError("Truncated GIF extension")
            extension = data[pos]
            pos += 1
            if extension == 0xF9 and pos + 6 <= len(data) and data[pos] == 4:
                delay = struct.unpack_from("<H", data, pos + 2)[0] * 10
                min_delay = min(min_delay, max(10, delay))
            pos = blocks(pos)
        elif tag == 0x2C:
            if pos + 9 >= len(data):
                raise ValueError("Truncated GIF image")
            _x, _y, fw, fh, flags = struct.unpack_from("<HHHHB", data, pos)
            if fw > MAX_SIDE or fh > MAX_SIDE or not fw or not fh:
                raise ValueError("GIF frame dimensions exceed limit")
            pos += 9
            if flags & 0x80:
                pos += 3 * (2 ** ((flags & 7) + 1))
            pos += 1  # LZW minimum code size
            pos = blocks(pos)
            frames += 1
            pixels += width * height
            if frames > MAX_FRAMES or pixels > MAX_TOTAL_PIXELS:
                raise ValueError("GIF frame count or decoded pixel budget exceeds limit")
        else:
            raise ValueError("Invalid GIF block")
    raise ValueError("GIF trailer missing")


def import_gif(path: str | Path, root: str | Path) -> tuple[str, dict[str, int]]:
    source = Path(path)
    with source.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    info = inspect_gif(data)
    folder = Path(root) / "data" / "assets" / "robot_gifs"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (hashlib.sha256(data).hexdigest() + ".gif")
    if not target.exists():
        total = sum(p.stat().st_size for p in folder.glob("*.gif") if p.is_file())
        if total + len(data) > 256 * 1024 * 1024:
            raise ValueError("Managed GIF library exceeds 256 MiB quota")
        try:
            with target.open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            pass
    return str(target), info
