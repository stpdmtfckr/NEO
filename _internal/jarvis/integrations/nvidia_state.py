"""Read-only NVIDIA state adapters with a fail-closed UI Automation probe.

NVIDIA App has no documented live-state API in this integration. Its saved
ShareSettings and saved registry values are not runtime evidence.
Only an exact, visible, NVIDIA-owned control exposing Microsoft's ToggleState
is accepted. No overlay is opened, no key pressed, and no screenshot captured.
The UIA process is isolated and killed on timeout; a hung provider cannot hang
NEO's game watcher. Compatibility with each NVIDIA build requires Windows QA.
"""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

PROCESS_BACKENDS = {"nvidia overlay.exe": "nvidia_app"}
# Exact accessible feature labels only. Shortcut-editor labels and button text
# such as "Turn on", "On", or "Save" are deliberately not state evidence.
REPLAY_LABELS = {"instant replay", "мгновенный повтор", "relecture instantanée", "replay instantané",
                 "repetición instantánea", "replay istantaneo", "repetição instantânea", "videobeweis"}
RECORD_LABELS = {"record", "recording", "запись", "aufnehmen", "aufnahme", "enregistrer", "enregistrement",
                 "grabar", "grabación", "registra", "registrazione", "gravar", "gravação"}
_PROBE_LOCK = threading.Lock()


def processes() -> list[dict[str, Any]]:
    import psutil
    rows = []
    for proc in psutil.process_iter(["name", "pid", "create_time"]):
        name = str(proc.info.get("name") or "").casefold()
        if name in PROCESS_BACKENDS:
            rows.append({"pid": int(proc.info["pid"]), "started": proc.info.get("create_time"),
                         "backend": PROCESS_BACKENDS[name]})
    return rows


def backend_of(rows: list[dict[str, Any]]) -> str:
    return "nvidia_app" if rows and all(row.get("backend") == "nvidia_app" for row in rows) else "unavailable"


def foreground() -> tuple[int, int]:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    hwnd = int(user32.GetForegroundWindow() or 0)
    pid = wintypes.DWORD()
    if hwnd:
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return hwnd, int(pid.value)


def states_from_controls(controls: list[Any], pid: int) -> dict[str, bool | None]:
    """Interpret only explicit TogglePattern state; contradictory matches fail."""
    states: dict[str, list[bool]] = {"instant_replay": [], "recording": []}
    for control in controls:
        try:
            if int(control.element_info.process_id) != pid or not control.is_visible() or not control.is_enabled():
                continue
            name = " ".join(control.window_text().strip().casefold().split())
            kind = "instant_replay" if name in REPLAY_LABELS else "recording" if name in RECORD_LABELS else None
            if kind is None:
                continue
            value = control.get_toggle_state()
            if type(value) is not int or value not in (0, 1):
                continue
            states[kind].append(bool(value))
        except Exception:
            continue
    return {key: values[0] if values and len(set(values)) == 1 else None for key, values in states.items()}


def _uia_snapshot() -> dict[str, Any]:
    """Worker only. COM wrappers never escape this short-lived process."""
    rows = processes()
    backend = backend_of(rows)
    result = {"backend": backend, "instant_replay": None, "recording": None, "source": "visible_uia", "reason": "overlay_not_foreground"}
    hwnd, pid = foreground()
    owner = next((row for row in rows if row["pid"] == pid), None)
    if not owner or backend != "nvidia_app":
        return result
    from comtypes import CoInitialize, CoUninitialize
    CoInitialize()
    try:
        from pywinauto import Desktop
        root = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
        pending = deque([(root, 0)])
        controls = []
        while pending and len(controls) < 256:
            control, depth = pending.popleft()
            controls.append(control)
            children = control.children()
            if children and depth >= 6:
                result["reason"] = "control_tree_limit"
                return result
            pending.extend((child, depth + 1) for child in children)
        if pending:
            result["reason"] = "control_tree_limit"
            return result
        found = states_from_controls(controls, pid)
        # Do not publish an observation collected from an overlay that has
        # since closed, lost foreground, or restarted with a recycled PID.
        if foreground() != (hwnd, pid) or owner not in processes():
            result["reason"] = "overlay_changed"
            return result
        result.update(found, reason="observed" if any(value is not None for value in found.values()) else "controls_not_exposed")
        return result
    finally:
        CoUninitialize()


def snapshot() -> dict[str, Any]:
    result = {"backend": "unavailable", "instant_replay": None, "recording": None, "source": "visible_uia", "reason": "not_windows"}
    if os.name != "nt":
        return result
    try:
        rows = processes()
        result.update(backend=backend_of(rows), reason="overlay_not_foreground")
        _hwnd, pid = foreground()
        if not any(row["pid"] == pid for row in rows) or result["backend"] != "nvidia_app":
            return result
        # Multiple callers do not pile up processes while a provider is slow.
        if not _PROBE_LOCK.acquire(blocking=False):
            result["reason"] = "probe_busy"
            return result
        try:
            from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
            # Root NEO.exe is a GUI shim, not a Python command-line host.
            # Use only the console interpreter alongside this running runtime.
            python = Path(sys.executable).with_name("python.exe")
            if not python.is_file():
                result["reason"] = "runtime_python_unavailable"
                return result
            env = dict(os.environ)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
            child = subprocess.run([str(python), "-u", "-m", "jarvis.integrations.nvidia_state", "--probe"], env=env,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=1.5, check=False, **hidden_process_kwargs())
            if child.returncode != 0 or len(child.stdout) > 4096:
                result["reason"] = "probe_failed"
                return result
            data = json.loads(child.stdout)
            if data.get("backend") != result["backend"] or any(data.get(k) is not None and type(data[k]) is not bool for k in ("instant_replay", "recording")):
                result["reason"] = "invalid_probe"
                return result
            return {key: data.get(key) for key in result}
        finally:
            _PROBE_LOCK.release()
    except subprocess.TimeoutExpired:
        result["reason"] = "probe_timeout"
    except Exception:
        result["reason"] = "probe_unavailable"
    return result


def replay_observation() -> dict[str, Any]:
    """Keep the live App probe's provenance; never infer state from saved settings."""
    return snapshot()


def replay_state() -> bool | None:
    return replay_observation()["instant_replay"]


def recording_state() -> bool | None:
    return snapshot()["recording"]


def main(argv=None, *, config=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--probe" in args:
        try:
            print(json.dumps(_uia_snapshot(), ensure_ascii=True))
            return 0
        except Exception:
            return 2
    # Safe user diagnostic: allowlisted status facts only, no paths, screen
    # contents, arbitrary UI text, credentials, or settings file uploads.
    if "--diagnose" in args:
        print('{"read_only": true, "sampling_delay_seconds": 5}', flush=True)
        time.sleep(5.0)
    data = snapshot()
    if config is not None:
        data["gaming_enabled"] = bool(config.get("gaming.enabled", True))
        data["auto_enable"] = bool(config.get("gaming.instant_replay.auto_enable", False))
        data["change_notices"] = bool(config.get("gaming.notifications.replay_changes", True))
        from jarvis.integrations.nvidia_capture import parse_hotkey
        for name, key, default in [("replay_hotkey", "gaming.instant_replay.toggle_hotkey", "alt+shift+f10"),
                                    ("clip_hotkey", "capture.nvidia_clip_hotkey", "alt+f10"),
                                    ("record_hotkey", "capture.nvidia_record_hotkey", "alt+f9")]:
            value = str(config.get(key, default) or "")
            try:
                parse_hotkey(value)
                data[name] = value.strip().casefold()
            except ValueError:
                data[name] = "invalid_or_cleared"
    data["compatibility_verified_on_this_build"] = False
    data["note"] = "Read-only: no keys, clicks, recording, screenshots, or configuration changes. Open Alt+Z yourself and run again to check visible controls."
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
