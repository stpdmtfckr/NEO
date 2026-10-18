"""Build the NEO process host (bin/NEO.exe) from neo_host.c + neo_host.rc.

    python _internal/launcher/build_host.py

Uses MinGW-w64 (x86_64-w64-mingw32-gcc + windres) or Zig (`zig cc`, `zig rc`). Writes
bin/NEO.exe and host_manifest.json with the source and binary SHA-256. No compiler is
needed on a user's computer: the binary ships with NEO.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def inspect_pe(path: Path) -> dict:
    data = path.read_bytes()
    offset = struct.unpack_from('<I', data, 0x3C)[0]
    if data[offset:offset + 4] != b'PE\0\0':
        raise ValueError('not a PE file')
    machine = struct.unpack_from('<H', data, offset + 4)[0]
    magic = struct.unpack_from('<H', data, offset + 24)[0]
    subsystem = struct.unpack_from('<H', data, offset + 24 + 68)[0]
    if (machine, magic, subsystem) != (0x8664, 0x20B, 2):
        raise ValueError(f'unexpected target machine={machine:#x} magic={magic:#x} subsystem={subsystem}')
    return {'architecture': 'x86_64', 'subsystem': 'Windows GUI', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def main() -> int:
    out = HERE / 'bin' / 'NEO.exe'
    out.parent.mkdir(exist_ok=True)
    if shutil.which('x86_64-w64-mingw32-gcc') and shutil.which('x86_64-w64-mingw32-windres'):
        subprocess.run(['x86_64-w64-mingw32-windres', 'neo_host.rc', '-O', 'coff', '-o', 'neo_host.res.o'], cwd=HERE, check=True)
        subprocess.run(['x86_64-w64-mingw32-gcc', '-municode', '-mwindows', '-Os', '-s', '-Wall', '-Wextra', 'neo_host.c',
                        'neo_host.res.o', '-lshell32', '-o', str(out)], cwd=HERE, check=True)
        (HERE / 'neo_host.res.o').unlink()
        compiler = 'MinGW-w64 gcc'
    else:
        zig = [shutil.which('zig')] if shutil.which('zig') else [sys.executable, '-m', 'ziglang']
        subprocess.run([*zig, 'rc', '/fo', 'neo_host.res', 'neo_host.rc'], cwd=HERE, check=True)
        subprocess.run([*zig, 'cc', '-target', 'x86_64-windows-gnu', '-municode', '-mwindows', '-Os', '-s', 'neo_host.c',
                        'neo_host.res', '-lshell32', '-o', str(out)], cwd=HERE, check=True)
        compiler = 'Zig cc'
    report = {'compiler': compiler, 'binary': inspect_pe(out),
              'sources': {n: hashlib.sha256((HERE / n).read_bytes()).hexdigest() for n in ('neo_host.c', 'neo_host.rc')}}
    (HERE / 'host_manifest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
