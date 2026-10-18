# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, asdict

import pyperclip


@dataclass
class ClipboardItem:
    text: str
    captured_at: float


class ClipboardHistory:
    def __init__(self, max_items: int = 50) -> None:
        self.max_items = max(2, int(max_items))
        self._items: list[ClipboardItem] = []
        self._lock = threading.RLock()
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self.capture()

    def capture(self) -> str:
        try:
            value = str(pyperclip.paste() or "")
        except Exception:
            value = ""
        if not value:
            return ""
        with self._lock:
            if not self._items or self._items[0].text != value:
                self._items.insert(0, ClipboardItem(value, time.time()))
                del self._items[self.max_items:]
        return value

    def set(self, text: str) -> None:
        pyperclip.copy(str(text))
        self.capture()

    def get(self, offset: int = 0) -> str:
        self.capture()
        with self._lock:
            if offset < 0 or offset >= len(self._items):
                raise IndexError("Такого элемента нет в истории буфера")
            return self._items[offset].text

    def clear(self) -> None:
        pyperclip.copy("")
        with self._lock:
            self._items.clear()

    def list(self, limit: int = 10) -> list[dict]:
        self.capture()
        with self._lock:
            return [asdict(x) for x in self._items[:max(1, int(limit))]]

    def start_monitor(self, interval: float = 0.90) -> None:
        """Continuously capture text clipboard changes while JARVIS is running.

        The monitor is intentionally text-only. File-drop clipboard payloads remain
        untouched and are handled by dedicated transfer tools.
        """
        with self._lock:
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return
            self._monitor_stop.clear()
            delay = max(0.20, min(2.0, float(interval)))
            def _loop() -> None:
                while not self._monitor_stop.wait(delay):
                    try: self.capture()
                    except Exception: pass
            self._monitor_thread = threading.Thread(target=_loop, name="JarvisClipboardHistory", daemon=True)
            self._monitor_thread.start()

    def stop_monitor(self) -> None:
        self._monitor_stop.set()
        thread = self._monitor_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.8)
        self._monitor_thread = None


_GLOBAL = ClipboardHistory()


def clipboard_history() -> ClipboardHistory:
    return _GLOBAL
