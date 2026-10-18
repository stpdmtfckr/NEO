"""Capability-aware non-destructive startup checks. Does not load a model."""
from __future__ import annotations
import ctypes
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from .diagnostics import write_report


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    seconds: float
    status: str = "PASS"
    capability: str = "core"


def _timed(name: str, fn, capability: str = "core") -> Check:
    started = time.perf_counter()
    try:
        return Check(name, True, str(fn()), time.perf_counter() - started, capability=capability)
    except Exception as exc:
        return Check(name, False, f"{type(exc).__name__}: {exc}", time.perf_counter() - started, "FAIL", capability)


def _skip(name: str, detail: str, status: str = "SKIP", capability: str = "optional") -> Check:
    return Check(name, True, detail, 0.0, status, capability)


def _settings_write(config: Any) -> str:
    parent = Path(config.paths.settings).parent
    with tempfile.NamedTemporaryFile(mode="w+b", prefix=".neo-write-probe-", dir=parent, delete=True) as stream:
        stream.write(b"NEO write probe")
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        if stream.read() != b"NEO write probe":
            raise OSError("settings-directory readback mismatch")
    return "settings directory write/read/delete succeeded"


def run_self_test(config: Any, *, quick: bool = True) -> dict[str, Any]:
    checks = [_timed("python", lambda: sys.version.split()[0])]
    def system():
        import psutil
        return f"cpu={psutil.cpu_count()} ram={round(psutil.virtual_memory().total / 1024**3, 1)}GB"
    checks.append(_timed("psutil", system))
    vision_enabled = bool(config.get("ollama.vision_enabled", config.get("ollama.requests_enabled", False)))
    chat_enabled = bool(config.get("local_chat.enabled", True))
    snapshot = None
    if vision_enabled or chat_enabled:
        from jarvis.ollama_status import readiness
        snapshot = readiness(str(config.get("ollama.base_url", "http://127.0.0.1:11434")), retries=1)
    for prefix, enabled, capability, wanted in (
        ("ollama", vision_enabled, "vision", str(config.get("ollama.vision_model", "") or "")),
        ("ollama_chat", chat_enabled, "conversation", str(config.get("local_chat.model", "") or config.get("ollama.chat_model", "qwen2.5:0.5b") or "qwen2.5:0.5b")),
    ):
        if not enabled:
            checks.append(_skip(prefix, "Local capability is disabled; no model request made", "DISABLED", capability))
            checks.append(_skip(prefix + "_model", "Local capability is disabled", "DISABLED", capability))
            continue
        ready = bool(snapshot and snapshot.get("ready"))
        if prefix == "ollama" or not vision_enabled:
            checks.append(Check(prefix, True, snapshot["message"], 0.0, "PASS" if ready else "UNAVAILABLE", capability))
        if ready:
            names = snapshot["models"]
            found = bool(wanted) and any(x == wanted or (":" not in wanted and x == wanted + ":latest") for x in names)
            if found:
                detail = wanted
            elif capability == "vision":
                detail = f"Image model is not installed: {wanted or '(not configured)'}"
            else:
                detail = f"Conversation model is not installed: {wanted or '(not configured)'}"
            checks.append(Check(prefix + "_model", True, detail, 0.0, "PASS" if found else "UNAVAILABLE", capability))
        else:
            # Do not duplicate the same connection failure as a second model
            # failure: its presence cannot be established until the API is ready.
            checks.append(_skip(prefix + "_model", "API unavailable; model status was not checked", "SKIP", capability))
    if os.name == "nt":
        checks.append(_timed("windows_api", lambda: f"foreground HWND={int(ctypes.windll.user32.GetForegroundWindow())}"))
        def microphone():
            count = int(ctypes.windll.winmm.waveInGetNumDevs())
            if count <= 0:
                raise RuntimeError("No microphone detected; check Windows privacy/device settings")
            return f"inputs={count}; WinMM presence only, audio recognition not tested"
        checks.append(_timed("microphone", microphone, "speech"))
    else:
        checks.extend((_skip("windows_api", "Requires a Windows host"), _skip("microphone", "Requires a Windows host; no acoustic test performed")))
    checks.append(_skip("discord_install", "Optional; API/login not contacted by startup diagnostics"))
    checks.append(_timed("browser", lambda: str(config.get("desktop.preferred_browser", "firefox"))))
    checks.append(_skip("nvidia_smi", shutil.which("nvidia-smi") or "Optional tool absent; no GPU workload started"))
    if not quick:
        checks.append(_timed("settings_write", lambda: _settings_write(config)))
    result = {"ok": all(x.ok for x in checks), "checks": [asdict(x) for x in checks], "generated_at": time.time(), "run_id": uuid.uuid4().hex, "quick": quick, "platform": sys.platform, "not_tested": ["recognition accuracy", "Windows GUI actions", "authenticated APIs", "installer", "CPU/GPU performance"]}
    try:
        path = Path(config.paths.root) / "diagnostics" / "self_test_latest.json"
        write_report(path, result)
        result["report_path"] = str(path)
    except (OSError, AttributeError) as exc:
        result["report_error"] = str(exc)
    return result
