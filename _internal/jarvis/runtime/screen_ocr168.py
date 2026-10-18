"""Built-in Windows OCR (Windows.Media.Ocr) for screen text — no neural model, no GPU.

Used by «прочитай экран», by screen analysis (the recognized lines are given to
the vision model so it can look at a smaller image) and by accepting a match.
A full-HD window is recognised in roughly 0.1–0.4 s on an ordinary CPU.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class OcrWord:
    text: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class OcrLine:
    text: str
    words: tuple[OcrWord, ...]


def grab(left: int, top: int, width: int, height: int):
    """Screenshot of a screen region as a PIL image."""
    import mss
    from PIL import Image
    with mss.mss() as grabber:
        shot = grabber.grab({'left': int(left), 'top': int(top), 'width': int(width), 'height': int(height)})
    return Image.frombytes('RGB', shot.size, shot.bgra, 'raw', 'BGRX')


def _fit(image, max_edge: int):
    scale = min(1.0, float(max_edge) / max(1, max(image.size)))
    if scale < 1.0:
        image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
    return image, scale


async def _recognize(image) -> list[OcrLine]:
    from winsdk.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage.streams import DataWriter
    rgba = image.convert('RGBA')
    writer = DataWriter()
    data = rgba.tobytes('raw', 'BGRA')
    try:
        writer.write_bytes(data)
    except TypeError:
        writer.write_bytes(list(data))
    bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8, rgba.width, rgba.height)
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        return []
    result = await engine.recognize_async(bitmap)
    lines = []
    for line in result.lines:
        words = tuple(OcrWord(w.text, w.bounding_rect.x, w.bounding_rect.y, w.bounding_rect.width, w.bounding_rect.height)
                      for w in line.words)
        lines.append(OcrLine(line.text, words))
    return lines


def recognize(image, *, max_edge: int = 2400) -> tuple[list[OcrLine], float]:
    """(lines, scale) — coordinates are in the scaled image; divide by scale for screen pixels."""
    fitted, scale = _fit(image, max_edge)
    return asyncio.run(_recognize(fitted)), scale


_CACHE: "OrderedDict[str, tuple[float, list[OcrLine]]]" = OrderedDict()
_CACHE_SECONDS = 20.0


def _fingerprint(image, max_edge: int) -> str:
    """Cheap identity of a screenshot: a small grey thumbnail hashed (identical window → identical key)."""
    thumb = image.convert('L').resize((64, 40))
    return hashlib.blake2b(thumb.tobytes() + f'{image.size}{max_edge}'.encode(), digest_size=16).hexdigest()


def recognize_cached(image, *, max_edge: int = 2400) -> list[OcrLine]:
    """NEO 172: the same, unchanged window is not recognised twice within 20 s."""
    key = _fingerprint(image, max_edge)
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and now - hit[0] <= _CACHE_SECONDS:
        _CACHE.move_to_end(key)
        return hit[1]
    lines, _scale = recognize(image, max_edge=max_edge)
    _CACHE[key] = (now, lines)
    while len(_CACHE) > 8:
        _CACHE.popitem(last=False)
    return lines


def text_lines(image, *, max_edge: int = 2400, limit: int = 80) -> list[str]:
    """Readable lines top to bottom, duplicates and 1-character noise removed."""
    try:
        lines = recognize_cached(image, max_edge=max_edge)
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        text = ' '.join(str(line.text or '').split())
        key = text.casefold()
        if len(text) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def words_in_region(left: int, top: int, width: int, height: int, *, max_edge: int = 1600):
    """[(text, x, y, w, h)] in scaled image coordinates plus the scale — used by match accept."""
    image = grab(left, top, width, height)
    lines, scale = recognize(image, max_edge=max_edge)
    words = [(w.text, w.x, w.y, w.width, w.height) for line in lines for w in line.words]
    return words, scale
