"""Paths and worker commands shared by source and native user distributions."""
from __future__ import annotations
import os
from pathlib import Path
import sys


def native_host() -> Path | None:
    value = os.environ.get('NEO_HOST_EXECUTABLE', '')
    return Path(value) if value and os.environ.get('NEO_PACKAGE_ROOT') else None


def project_root() -> Path:
    host = native_host()
    return host.parent if host else Path(__file__).resolve().parents[3]


def console_python() -> Path:
    if native_host():
        return Path(os.environ['NEO_RUNTIME_ROOT']) / 'Scripts' / 'python.exe'
    path = Path(sys.executable)
    return path.with_name('python.exe') if path.name.casefold() == 'pythonw.exe' and path.with_name('python.exe').is_file() else path


def worker_command(module: str, *, python=None, args=()) -> list[str]:
    """Run own workers through the branded host (optionally with another private interpreter)."""
    host = native_host()
    if host:
        command = [str(host)]
        if python:
            selected = Path(python)
            if selected.parent.name.casefold() == 'scripts':
                command += ['--neo-runtime', str(selected.parent.parent)]
        return command + ['-u', '-m', module, *map(str,args)]
    from jarvis.runtime.process_host171 import branded   # NEO 171: workers run as «NEO», not «Python»
    return [str(branded(python or sys.executable)), '-u', '-m', module, *map(str,args)]
