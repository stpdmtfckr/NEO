# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import os
import subprocess
from typing import Any


def hidden_process_kwargs() -> dict[str, Any]:
    """Return Windows flags that prevent console helpers from flashing/focusing."""
    if os.name != "nt":
        return {}
    kwargs: dict[str, Any] = {}
    try:
        kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    except Exception:
        pass
    try:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 1))
        startup.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
        kwargs["startupinfo"] = startup
    except Exception:
        pass
    return kwargs
