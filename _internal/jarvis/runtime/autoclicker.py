# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import ctypes
import os
import threading
import time
from typing import Any


_BUTTON_FLAGS = {
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
    "middle": (0x0020, 0x0040),
}


class AutoClickerController:
    def __init__(self, config: Any) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active = False
        self._owner: _Run | None = None
        self._last_error = ""
        self._button = "left"
        self._cps = 10.0
        self.reload_settings()

    def reload_settings(self) -> None:
        with self._lock:
            button = str(self.config.get("autoclicker.button", "left") or "left").strip().casefold()
            self._button = button if button in _BUTTON_FLAGS else "left"
            self._cps = max(0.5, min(100.0, float(self.config.get("autoclicker.cps", 10.0) or 10.0)))

    def configure(self, *, button: str | None = None, cps: float | None = None) -> dict[str, Any]:
        with self._lock:
            if button is not None:
                value = str(button or "").strip().casefold()
                if value not in _BUTTON_FLAGS:
                    raise ValueError("Кнопка автокликера должна быть left, right или middle")
                self._button = value
            if cps is not None:
                self._cps = max(0.5, min(100.0, float(cps)))
            return self.status()

    def start(self, *, button: str | None = None, cps: float | None = None) -> dict[str, Any]:
        if not bool(self.config.get("autoclicker.enabled", True)):
            raise RuntimeError("Автокликер отключён в разделе «Дополнительно»")
        self.configure(button=button, cps=cps)
        with self._lock:
            if self._active and self._thread is not None and self._thread.is_alive():
                return self.status()
            # HIST-049: every run owns its own stop event; an older run can neither
            # be re-admitted by this start nor reset the state this run owns.
            run = _Run(threading.Event())
            self._owner = run
            self._stop = run.stop
            self._active = True
            self._last_error = ""
            self._thread = threading.Thread(target=self._run, args=(run,), name="NeoJarvis-Autoclicker", daemon=True)
            self._thread.start()
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            run = getattr(self, "_owner", None)
            if run is not None:
                run.stop.set()
            self._owner = None
            self._active = False
            self._stop.set()
            return self.status()

    def toggle(self) -> dict[str, Any]:
        with self._lock:
            active = self._active
        return self.stop() if active else self.start()

    def status(self) -> dict[str, Any]:
        state = {"active": bool(self._active), "button": self._button, "cps": float(self._cps)}
        error = str(getattr(self, "_last_error", "") or "")
        if error:
            state["error"] = error             # HIST-050: the failure reason, separate from «active»
        return state

    @staticmethod
    def _click(button: str) -> None:
        if os.name == "nt":
            down, up = _BUTTON_FLAGS[button]
            ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)
            return
        try:
            import pyautogui
            pyautogui.click(button=button)
        except Exception:
            pass

    def _owns(self, run: "_Run") -> bool:
        return getattr(self, "_owner", None) is run

    def _run(self, run: "_Run | None" = None) -> None:
        if run is None:                       # legacy call without an owner token
            run = _Run(self._stop)
            with self._lock:
                self._owner = run
        deadline = time.perf_counter()
        try:
            while not run.stop.is_set():
                with self._lock:
                    if not self._active or not self._owns(run):
                        break
                    button = self._button
                    cps = self._cps
                self._click(button)
                interval = 1.0 / max(0.5, cps)
                deadline += interval
                remaining = deadline - time.perf_counter()
                if remaining > 0:
                    run.stop.wait(remaining)
                else:
                    deadline = time.perf_counter()
        except Exception as exc:
            # HIST-050: a backend failure ends this run and is reported in status.
            with self._lock:
                if self._owns(run):
                    self._last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
        finally:
            with self._lock:
                if self._owns(run):           # an old tail never resets a newer run
                    self._active = False
                    self._owner = None


class _Run:
    __slots__ = ("stop",)

    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop


_controller: AutoClickerController | None = None
_controller_lock = threading.Lock()


def autoclicker_controller(config: Any) -> AutoClickerController:
    global _controller
    with _controller_lock:
        if _controller is None:
            _controller = AutoClickerController(config)
        else:
            _controller.config = config
        return _controller
