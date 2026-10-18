# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Launch a background helper without creating a visible Windows console."""

import os
from pathlib import Path
import subprocess
import sys


def _kwargs() -> dict:
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        kwargs["creationflags"] = flags
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 1))
            si.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
            kwargs["startupinfo"] = si
        except Exception:
            pass
    return kwargs


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    command = sys.argv[1:]
    try:
        subprocess.Popen(command, cwd=str(Path(__file__).resolve().parent), env=os.environ.copy(), **_kwargs())
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
