"""Bounded Ollama bootstrap compatibility entry point; never downloads models.

Normal CMD uses the same PowerShell helper in a detached process. Installation
of the official CLI is automatic; only starting its server is user-switchable.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from jarvis.config import ConfigManager
from jarvis.ollama_runtime import ensure_ollama_running, find_ollama_executable, probe_ollama
from jarvis.model_management import local_endpoint


def _silent_mode() -> bool:
    return '--background' in sys.argv[1:] or os.getenv('NEO_JARVIS_HIDDEN_BATCH', '') == '1'


def _log(message: str) -> None:
    # Callers supply fixed classifications, not raw process/network exceptions.
    if not _silent_mode():
        print(message)


def _is_windows() -> bool:
    return os.name == 'nt'


def _run_windows_helper(root: Path, *, install_only: bool, cancel: bool = False) -> int:
    script = root / 'bootstrap_ollama.ps1'
    if not script.is_file():
        _log('Ollama setup helper is missing. Extract the entire NEO archive.')
        return 2
    command = ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)]
    if install_only:
        command.append('-InstallOnly')
    if cancel:
        command.append('-Cancel')
    if not _silent_mode():
        command.append('-Foreground')
    kwargs = {'cwd': str(root), 'stdin': subprocess.DEVNULL, 'check': False, 'timeout': 1900}
    if _silent_mode():
        kwargs['creationflags'] = int(getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        return subprocess.run(command, **kwargs).returncode
    except subprocess.TimeoutExpired:
        _log('Ollama setup timed out. Retry from NEO.cmd; no models were downloaded.')
    except OSError:
        _log('Ollama setup could not start. Check Windows permissions and retry.')
    return 2


def bootstrap(config, root: Path, *, quick_check: bool = False, cancel: bool = False) -> int:
    try:
        base_url = local_endpoint(str(config.get('ollama.base_url', 'http://localhost:11434')))
    except ValueError:
        _log('External or invalid Ollama endpoint: no local installation or start.')
        return 0
    auto_start = bool(config.get('ollama.auto_start', True))
    if quick_check:
        if not auto_start:
            return 0 if find_ollama_executable() is not None else 2
        return 0 if probe_ollama(base_url, timeout=1.2)[0] else 2
    if _is_windows():
        # Shared cross-process installation/start lock lives in the PS helper.
        # Do not consult the retired auto_install switch: installation is always automatic.
        return _run_windows_helper(root, install_only=not auto_start, cancel=cancel)
    if cancel:
        return 0
    if find_ollama_executable() is None:
        _log('This bootstrap installs Ollama only on Windows. Use https://ollama.com/download.')
        return 2
    if not auto_start:
        return 0
    result = ensure_ollama_running(base_url, timeout=20, prefer_tray_app=False)
    return 0 if result.get('ready') else 2


def main() -> int:
    root = Path(__file__).resolve().parent
    config = ConfigManager(root / 'config/default_settings.json')
    return bootstrap(config, root, quick_check='--quick-check' in sys.argv[1:], cancel='--cancel' in sys.argv[1:])


if __name__ == '__main__':
    raise SystemExit(main())
