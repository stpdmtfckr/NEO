# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Spawn the Qt GUI independently from START_JARVIS.bat.

HOTFIX62 deliberately avoids CMD ``start /b``.  The child gets no inherited
console handles and, when Windows permits it, breaks away from the launcher job.
Closing the bootstrap console therefore cannot terminate the real assistant.
"""

import os
from pathlib import Path
import subprocess
import sys


def _spawn(command: list[str], cwd: Path, *, breakaway: bool) -> subprocess.Popen:
    flags = 0
    startupinfo = None
    if os.name == "nt":
        flags |= int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        # Some Windows Terminal/ConPTY configurations can briefly surface a
        # console even for helper-style launches.  SW_HIDE is independent of the
        # parent's terminal and is therefore used in addition to pythonw.exe.
        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 1))
            startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
        except Exception:
            startupinfo = None
        if breakaway:
            flags |= int(getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000))
    return subprocess.Popen(
        command,
        cwd=str(cwd),
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
        startupinfo=startupinfo,
    )


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: _detached_gui_launch.py <pythonw.exe> <app.py>", file=sys.stderr)
        return 2
    pyw = Path(sys.argv[1]).resolve()
    app = Path(sys.argv[2]).resolve()
    if not pyw.is_file() or not app.is_file():
        print(f"launcher target missing: {pyw} / {app}", file=sys.stderr)
        return 3
    command = [str(pyw), str(app), "--detached-launch"]
    try:
        proc = _spawn(command, app.parent, breakaway=True)
    except OSError:
        # Some corporate/Windows jobs disallow BREAKAWAY_FROM_JOB. DETACHED_PROCESS
        # still prevents console-close propagation, so retry without breakaway.
        proc = _spawn(command, app.parent, breakaway=False)
    print(proc.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
