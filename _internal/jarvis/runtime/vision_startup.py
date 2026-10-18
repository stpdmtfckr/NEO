"""Observable background startup of the local image service, without model loads."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlparse

from jarvis.runtime.diagnostics import write_report

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()


def _is_windows() -> bool:
    return os.name == "nt"


def _record(config, state: str, reason: str, **details) -> None:
    logger.info("Image backend startup state=%s reason=%s", state, reason)
    try:
        write_report(Path(config.paths.root) / "bootstrap" / "vision_status.json",
                     {"release": 145, "state": state, "reason": reason,
                      "updated_at": time.time(), "model_loaded_by_startup": False, **details})
    except OSError:
        logger.debug("Could not write image backend startup status", exc_info=True)


def _probe_local_version(base_url: str) -> tuple[bool, str]:
    import requests
    try:
        # A desktop proxy/VPN configuration must not redirect local image traffic.
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(base_url + "/api/version", timeout=(0.6, 1.0))
            response.raise_for_status()
            version = str(response.json().get("version", "") or "")
            return bool(version), version
    except (requests.RequestException, ValueError, AttributeError, TypeError):
        return False, ""


def _bootstrap_worker(config, endpoint: str, command: list[str], key: str) -> None:
    try:
        ready, version = _probe_local_version(endpoint)
        if ready:
            _record(config, "ready", "existing_server_reused", version=version)
            return
        logs = Path(config.paths.root) / "bootstrap"
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / "vision-bootstrap.log"
        if path.exists() and path.stat().st_size > 1024 * 1024:
            path.replace(path.with_suffix(".previous.log"))
        with path.open("ab", buffering=0) as log:
            log.write(f"\n=== NEO 145 image backend startup {time.time():.3f} ===\n".encode())
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       close_fds=True, creationflags=(
                                           getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) |
                                           getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)))
            _record(config, "starting", "hidden_bootstrap_spawned", pid=process.pid)
            try:
                code = process.wait(timeout=900)
            except subprocess.TimeoutExpired:
                # Installation may still be committing files. Never kill it or a
                # user's shared server merely because the observer timed out.
                _record(config, "unavailable", "bootstrap_wait_timeout", pid=process.pid)
                return
        ready, version = _probe_local_version(endpoint)
        if ready:
            _record(config, "ready", "local_api_verified", version=version, bootstrap_exit=code)
        else:
            _record(config, "unavailable", "local_api_not_ready", bootstrap_exit=code)
    except Exception as exc:
        _record(config, "unavailable", "bootstrap_exception", exception_type=type(exc).__name__)
        logger.exception("Image backend background startup failed")
    finally:
        with _LOCK:
            _INFLIGHT.discard(key)


def _native_bootstrap_worker(config, endpoint: str, key: str) -> None:
    """Start the already installed backend without any source script on disk.

    Setup owns installing dependencies; subsequent desktop launches only reuse
    a live server or start its CLI. This worker never loads/pulls a model and
    never changes a user's running Ollama process.
    """
    try:
        from jarvis.ollama_runtime import ensure_ollama_running
        result = ensure_ollama_running(endpoint, timeout=18.0, prefer_tray_app=False)
        if result.get('ready'):
            reason = 'local_api_verified' if result.get('started') else 'existing_server_reused'
            _record(config, 'ready', reason, version=result.get('version', ''),
                    backend_started=bool(result.get('started')))
        else:
            _record(config, 'unavailable', str(result.get('reason') or 'local_api_not_ready'),
                    backend_started=bool(result.get('started')))
    except Exception as exc:
        _record(config, 'unavailable', 'bootstrap_exception', exception_type=type(exc).__name__)
        logger.exception('Native image backend startup failed')
    finally:
        with _LOCK:
            _INFLIGHT.discard(key)


def start_vision_backend(config) -> bool:
    """Queue bounded startup work; this function never probes HTTP on the UI thread."""
    if not _is_windows():
        _record(config, "disabled", "unsupported_platform")
        return False
    if not (bool(config.get("ollama.vision_enabled", False)) or bool(config.get("local_chat.enabled", True))):
        _record(config, "disabled", "local_models_disabled")
        return False
    if not bool(config.get("ollama.auto_start", True)):
        _record(config, "disabled", "automatic_start_disabled")
        return False
    try:
        endpoint = urlparse(str(config.get("ollama.base_url", "http://127.0.0.1:11434")))
        port = endpoint.port or 11434
        if endpoint.hostname not in {"localhost", "127.0.0.1", "::1"} or endpoint.scheme != "http" or endpoint.username or endpoint.password or not 1024 <= port <= 65535:
            raise ValueError("Not a supported local HTTP endpoint")
    except ValueError:
        _record(config, "disabled", "invalid_local_endpoint")
        return False
    from jarvis.runtime.distribution164 import native_host
    native = native_host() is not None
    script = Path(__file__).resolve().parents[2] / "bootstrap_ollama.ps1"
    if not native and not script.is_file():
        _record(config, "unavailable", "bootstrap_script_missing")
        return False
    host = "[::1]" if endpoint.hostname == "::1" else "127.0.0.1"
    address = f"http://{host}:{port}"
    command = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
               "Bypass", "-File", str(script), "-Port", str(port)]
    if host == "[::1]":
        command.append("-IPv6")
    # Installation is handled automatically by the launcher; only startup is
    # user-switchable. No model download is requested by this bootstrap.
    key = str(Path(config.paths.root).resolve()) + "|" + address
    with _LOCK:
        if key in _INFLIGHT:
            return True
        _INFLIGHT.add(key)
    _record(config, "queued", "local_vision_requested")
    try:
        worker = _native_bootstrap_worker if native else _bootstrap_worker
        arguments = (config, address, key) if native else (config, address, command, key)
        threading.Thread(target=worker, args=arguments,
                         name="neo-vision-startup", daemon=True).start()
        return True
    except RuntimeError:
        with _LOCK:
            _INFLIGHT.discard(key)
        _record(config, "unavailable", "thread_start_failed")
        return False
