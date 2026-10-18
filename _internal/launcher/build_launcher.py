"""Rebuild the optional GUI shim with Zig 0.16.0; NEO.cmd remains the main entry."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys


def inspect_pe(path: Path) -> dict:
    data = path.read_bytes()
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[offset:offset + 4] != b"PE\0\0":
        raise ValueError("Not a PE executable")
    machine = struct.unpack_from("<H", data, offset + 4)[0]
    subsystem = struct.unpack_from("<H", data, offset + 24 + 68)[0]
    magic = struct.unpack_from("<H", data, offset + 24)[0]
    if (machine, subsystem, magic) != (0x8664, 2, 0x20B):
        raise ValueError(f"Wrong executable target: machine={machine:#x}, subsystem={subsystem}, magic={magic:#x}")
    return {"architecture": "x86_64", "subsystem": "Windows GUI", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def main() -> int:
    source = Path(__file__).resolve().parent
    target = source.parents[1] / "NEO.exe"
    zig = [shutil.which("zig")] if shutil.which("zig") else [sys.executable, "-m", "ziglang"]
    version = subprocess.check_output([*zig, "version"], text=True).strip()
    if version != "0.16.0":
        raise RuntimeError(f"Reproducible release compiler is Zig 0.16.0; found {version}")
    resource = ["rc", "/fo", "neo_launcher.res", "neo_launcher.rc"]
    compile_command = ["cc", "-target", "x86_64-windows-gnu", "-municode", "-Wl,--subsystem,windows", "-Os", "-s", "-Wall", "-Wextra", "neo_launcher.c", "neo_launcher.res", "-lshell32", "-luser32", "-o", "../../NEO.exe"]
    subprocess.run([*zig, *resource], cwd=source, check=True)
    subprocess.run([*zig, *compile_command], cwd=source, check=True)
    report = {"compiler": "Zig " + version, "command": compile_command, "resource_command": resource, "binary": inspect_pe(target), "sources": {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in ("neo_launcher.c", "neo_launcher.rc")}, "windows_execution_tested": False, "code_signed": False}
    (source / "build_manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
