# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Lifecycle helpers for a local Ollama installation on Windows.

The desktop build starts ``ollama serve`` without a console or a desktop window.
An already-running user-managed tray server is reused without changing its UI.
"""

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

_LOCK = threading.RLock()
_LAST_ATTEMPT = 0.0


def is_local_ollama_url(base_url: str) -> bool:
    try:
        host = (urlparse(str(base_url or "")).hostname or "").casefold()
    except Exception:
        return False
    return host in {"localhost", "127.0.0.1", "::1"}


def probe_ollama(base_url: str, timeout: float = 0.8) -> tuple[bool, str]:
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(str(base_url).rstrip("/") + "/api/version", timeout=max(0.2, float(timeout)))
            response.raise_for_status()
            data = response.json() if response.content else {}
            return True, str(data.get("version", "") or "") if isinstance(data, dict) else ""
    except Exception:
        return False, ""


def find_ollama_executable() -> Path | None:
    found = shutil.which("ollama") or shutil.which("ollama.exe")
    if found:
        return Path(found)
    if os.name != "nt":
        return None
    local = Path(os.getenv("LOCALAPPDATA", ""))
    program_files = Path(os.getenv("ProgramFiles", ""))
    candidates = [
        local / "Programs" / "Ollama" / "ollama.exe",
        local / "Programs" / "Ollama-NEO" / "0.34.4" / "ollama.exe",
        local / "Ollama" / "ollama.exe",
        program_files / "Ollama" / "ollama.exe",
    ]
    return next((p for p in candidates if str(p) and p.is_file()), None)


def find_ollama_app_executable(cli_executable: Path | str | None = None) -> Path | None:
    """Return Ollama's Windows tray executable when the desktop app is installed."""
    if os.name != "nt":
        return None
    candidates: list[Path] = []
    if cli_executable:
        cli = Path(cli_executable)
        candidates.extend((cli.parent / "ollama app.exe", cli.parent / "Ollama.exe"))
    local = Path(os.getenv("LOCALAPPDATA", ""))
    candidates.extend(
        (
            local / "Programs" / "Ollama" / "ollama app.exe",
            local / "Programs" / "Ollama" / "Ollama.exe",
        )
    )
    return next((p for p in candidates if str(p) and p.is_file()), None)



def _hide_ollama_top_level_windows(pid: int, seconds: float = 6.0) -> None:
    """Keep the official Ollama app in the notification area without a visible home window."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        SW_HIDE = 0
        target_pid = int(pid or 0)
        deadline = time.monotonic() + max(0.5, float(seconds))

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        while time.monotonic() < deadline:
            def visit(hwnd, _lparam):
                try:
                    if not user32.IsWindowVisible(hwnd):
                        return True
                    owner = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                    title_len = int(user32.GetWindowTextLengthW(hwnd) or 0)
                    title = ""
                    if title_len > 0:
                        buf = ctypes.create_unicode_buffer(title_len + 1)
                        user32.GetWindowTextW(hwnd, buf, title_len + 1)
                        title = buf.value
                    if int(owner.value) == target_pid or "ollama" in title.casefold():
                        user32.ShowWindowAsync(hwnd, SW_HIDE)
                except Exception:
                    pass
                return True
            user32.EnumWindows(WNDENUMPROC(visit), 0)
            time.sleep(0.20)
    except Exception:
        return


def _hide_ollama_windows_async(pid: int, seconds: float = 6.0) -> None:
    threading.Thread(
        target=_hide_ollama_top_level_windows,
        args=(int(pid or 0), float(seconds)),
        name="NeoJarvis-OllamaWindowHider",
        daemon=True,
    ).start()


def _hidden_popen_kwargs() -> dict:
    env = os.environ.copy()
    # JARVIS is an interactive desktop assistant, not a batch inference server.
    # Keep one Ollama request/model resident at a time so background verification
    # cannot oversubscribe the CPU while a game is running. Existing explicit
    # user environment overrides are preserved.
    env["OLLAMA_NO_CLOUD"] = "1"  # NEO-created local service never sends prompts to cloud models.
    env.setdefault("OLLAMA_NUM_PARALLEL", "1")
    env.setdefault("OLLAMA_MAX_LOADED_MODELS", "1")
    env.setdefault("OLLAMA_KEEP_ALIVE", "0")
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if os.name == "nt":
        # CREATE_NO_WINDOW prevents the fallback server from flashing a console.
        # SW_HIDE also keeps launchers quiet when starting the GUI/tray executable.
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(getattr(subprocess, "DETACHED_PROCESS", 0))
        kwargs["creationflags"] = flags
        try:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0))
            startup.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
            kwargs["startupinfo"] = startup
        except Exception:
            pass
    return kwargs


def _wait_for_api(base_url: str, seconds: float) -> tuple[bool, str]:
    deadline = time.monotonic() + max(0.2, float(seconds))
    while time.monotonic() < deadline:
        ok, version = probe_ollama(base_url, timeout=0.6)
        if ok:
            return True, version
        time.sleep(0.25)
    return False, ""


def ensure_ollama_running(
    base_url: str = "http://localhost:11434",
    *,
    timeout: float = 15.0,
    cooldown: float = 3.0,
    prefer_tray_app: bool = False,
) -> dict:
    """Start a local Ollama endpoint and wait until its API is reachable.

    Reuses a healthy endpoint or starts a hidden CLI server. prefer_tray_app is
    retained only for call compatibility: NEO never opens Ollama's desktop UI.
    """
    ok, version = probe_ollama(base_url)
    if ok:
        return {"ready": True, "started": False, "version": version}
    if not is_local_ollama_url(base_url):
        return {"ready": False, "started": False, "reason": "remote_endpoint"}

    global _LAST_ATTEMPT
    with _LOCK:
        ok, version = probe_ollama(base_url)
        if ok:
            return {"ready": True, "started": False, "version": version}
        now = time.monotonic()
        if now - _LAST_ATTEMPT < max(0.0, cooldown):
            return {"ready": False, "started": False, "reason": "startup_in_progress"}
        _LAST_ATTEMPT = now
        exe = find_ollama_executable()
        if exe is None:
            return {"ready": False, "started": False, "reason": "ollama_not_found"}

        # Do not launch the desktop UI: it may show a full window before it can
        # be hidden. Existing user-managed tray/server processes are reused above.
        started_mode = ""
        kwargs = _hidden_popen_kwargs()
        # Bind a server started by NEO to the selected loopback endpoint. An
        # inherited OLLAMA_HOST (or the default port) must not silently redirect
        # this new process while we wait on a different port forever.
        try:
            endpoint = urlparse(base_url)
            port = endpoint.port or 11434
            if endpoint.scheme != 'http' or endpoint.username or endpoint.password:
                raise ValueError('Unsupported local endpoint')
            host = '[::1]' if endpoint.hostname == '::1' else '127.0.0.1'
            kwargs['env']['OLLAMA_HOST'] = f'{host}:{port}'
        except ValueError:
            return {'ready': False, 'started': False, 'reason': 'invalid_local_endpoint'}
        # NEO 175: start the server in Ollama's own folder. Without this it inherited NEO's
        # folder as its current directory and kept the NEO folder from being deleted.
        kwargs["cwd"] = str(Path(exe).parent)
        try:
            subprocess.Popen([str(exe), "serve"], **kwargs)
            started_mode = "serve_hidden"
        except Exception as exc:
            return {
                "ready": False,
                "started": bool(started_mode),
                "reason": "start_failed",
                "error": str(exc),
                "executable": str(exe),
                "mode": started_mode,
            }

    remaining = max(2.0, min(60.0, float(timeout)))
    ok, version = _wait_for_api(base_url, remaining)
    if ok:
        return {
            "ready": True,
            "started": True,
            "version": version,
            "executable": str(exe),
            "mode": started_mode or "serve",
        }
    return {
        "ready": False,
        "started": True,
        "reason": "startup_timeout",
        "executable": str(exe),
        "mode": started_mode or "serve",
    }


def list_ollama_models(base_url: str, timeout: float = 2.5) -> set[str]:
    """Return locally installed model names using Ollama's /api/tags endpoint."""
    response = requests.get(str(base_url).rstrip("/") + "/api/tags", timeout=max(0.5, float(timeout)))
    response.raise_for_status()
    payload = response.json() if response.content else {}
    models = payload.get("models", []) if isinstance(payload, dict) else []
    names: set[str] = set()
    for item in models if isinstance(models, list) else []:
        if not isinstance(item, dict):
            continue
        for key in ("name", "model"):
            value = str(item.get(key, "") or "").strip()
            if value:
                names.add(value.casefold())
    return names


def _model_keys(model: str) -> set[str]:
    value = str(model or "").strip().casefold()
    if not value:
        return set()
    keys = {value}
    if ":" not in value:
        keys.add(value + ":latest")
    elif value.endswith(":latest"):
        keys.add(value[:-7])
    return keys


def ollama_model_present(base_url: str, model: str, timeout: float = 2.5) -> bool:
    wanted = _model_keys(model)
    if not wanted:
        return True
    try:
        installed = list_ollama_models(base_url, timeout=timeout)
    except Exception:
        return False
    return bool(wanted & installed)


def pull_ollama_model(
    model: str, base_url: str = "http://localhost:11434", *, timeout: float = 60 * 60, visible_progress: bool = False
) -> dict:
    """Download one missing model, preferring the local CLI for visible progress."""
    model = str(model or "").strip()
    if not model:
        return {"ready": True, "model": model, "already_present": True}
    if ollama_model_present(base_url, model):
        return {"ready": True, "model": model, "already_present": True}
    if not is_local_ollama_url(base_url):
        return {"ready": False, "model": model, "reason": "remote_endpoint_model_missing"}

    exe = find_ollama_executable()
    if exe is not None:
        try:
            run_kwargs = {} if visible_progress else _hidden_popen_kwargs()
            completed = subprocess.run([str(exe), "pull", model], timeout=max(30.0, float(timeout)), check=False, **run_kwargs)
            if completed.returncode == 0 and ollama_model_present(base_url, model, timeout=4.0):
                return {"ready": True, "model": model, "pulled": True, "method": "ollama_cli"}
            return {
                "ready": False,
                "model": model,
                "reason": "pull_failed",
                "returncode": int(completed.returncode),
                "method": "ollama_cli",
            }
        except subprocess.TimeoutExpired:
            return {"ready": False, "model": model, "reason": "pull_timeout", "method": "ollama_cli"}
        except Exception as exc:
            cli_error = str(exc)
    else:
        cli_error = "ollama executable not found"

    # API fallback is useful for local service-style installations where the CLI
    # executable is not discoverable. ``stream:false`` returns only after pull.
    try:
        response = requests.post(
            str(base_url).rstrip("/") + "/api/pull",
            json={"model": model, "stream": False},
            timeout=max(30.0, float(timeout)),
        )
        response.raise_for_status()
        return {
            "ready": ollama_model_present(base_url, model, timeout=4.0),
            "model": model,
            "pulled": True,
            "method": "ollama_api",
        }
    except Exception as exc:
        return {
            "ready": False,
            "model": model,
            "reason": "pull_failed",
            "error": str(exc),
            "cli_error": cli_error,
        }
