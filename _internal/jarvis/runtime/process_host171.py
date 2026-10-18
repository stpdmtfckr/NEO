"""NEO 171: run NEO's processes as «NEO» (name and icon in Task Manager), not as «Python».

The host (launcher/bin/NEO.exe, source launcher/neo_host.c) is copied into a runtime's
Scripts folder and behaves exactly like pythonw.exe there: same interpreter, same
virtual environment, same command line. Only the process image is NEO's.
"""
from __future__ import annotations

import filecmp
import logging
import os
from pathlib import Path
import shutil
import sys

logger = logging.getLogger(__name__)
SOURCE = Path(__file__).resolve().parents[2] / 'launcher' / 'bin' / 'NEO.exe'
HOST_NAME = 'NEO.exe'
# Our own process names, for code that must recognise NEO itself (never "a game", never "the user's window").
OWN_PROCESS_NAMES = frozenset({'neo.exe', 'neo_wake_service.exe', 'python.exe', 'pythonw.exe'})


def _windows() -> bool:
    return os.name == 'nt'


def install(runtime_root: Path) -> Path | None:
    """Copy the host into ``<runtime_root>/Scripts`` (only when it differs). Returns the host path or None."""
    if not _windows() or not SOURCE.is_file():
        return None
    scripts = Path(runtime_root) / 'Scripts'
    if not (Path(runtime_root) / 'pyvenv.cfg').is_file() or not scripts.is_dir():
        return None
    target = scripts / HOST_NAME
    try:
        if not target.is_file() or not filecmp.cmp(SOURCE, target, shallow=False):
            temporary = target.with_suffix('.new')
            shutil.copy2(SOURCE, temporary)
            os.replace(temporary, target)
        return target
    except OSError:
        # A running NEO.exe cannot be replaced; the existing copy keeps working.
        logger.debug('NEO host could not be installed into %s', scripts, exc_info=True)
        return target if target.is_file() else None


def branded(python: str | os.PathLike) -> Path:
    """The NEO host next to ``python`` when there is one, otherwise ``python`` itself."""
    path = Path(python)
    if not _windows() or path.name.casefold() == HOST_NAME.casefold():
        return path
    host = path.with_name(HOST_NAME)
    return host if host.is_file() and (path.parent.parent / 'pyvenv.cfg').is_file() else path


def fix_base_executable() -> None:
    """Under the host, point sys._base_executable at the real base python.exe (venv creation needs it)."""
    if not _windows() or Path(sys.executable).name.casefold() != HOST_NAME.casefold():
        return
    base = Path(sys.base_prefix) / 'python.exe'
    if base.is_file() and Path(getattr(sys, '_base_executable', '') or '').name.casefold() == HOST_NAME.casefold():
        sys._base_executable = str(base)


def base_python() -> Path:
    """A console python.exe that can create virtual environments (the base interpreter)."""
    candidate = Path(sys.base_prefix) / ('python.exe' if os.name == 'nt' else 'bin/python3')
    return candidate if candidate.is_file() else Path(sys.executable)
