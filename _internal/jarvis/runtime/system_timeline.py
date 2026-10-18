# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Low-cost passive Windows context tracker for long-term system memory.

The tracker records only foreground changes, never screenshots or key input.  A
~1.5 second poll is enough to remember which processes/windows/folders the user
actually worked with while keeping CPU usage negligible.  Explorer UIA is queried
only after Explorer becomes the foreground window or its title changes.
"""

import ctypes
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from jarvis.runtime.context_memory import ContextMemory

logger = logging.getLogger(__name__)


class SystemContextTracker:
    def __init__(self, config: Any, memory: ContextMemory | None = None, observation_callback: Any = None) -> None:
        self.config = config
        self.memory = memory or ContextMemory(config)
        self.observation_callback = observation_callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_signature: tuple[int, int, str, str] | None = None
        self._last_explorer_key: tuple[int, str] | None = None
        self._last_explorer_folder = ""
        self._observation_policy = None
        self._observation_policy_time = 0.0

    def observation_allowed(self, process: str, title: str, folder: str = "", pid: int = 0) -> bool:
        from jarvis.runtime.observation_policy import ObservationPolicy
        # Before ContextMemory's direct write as well as the EventStore callback.
        # Refresh infrequently; foreground polling must not repeatedly resolve
        # all managed roots. User exclusions below are checked immediately.
        if self._observation_policy is None or time.monotonic()-self._observation_policy_time >= 30:
            self._observation_policy = ObservationPolicy(self.config)
            self._observation_policy_time = time.monotonic()
        if not self._observation_policy.allows_event('activity.window',
                {'process_name':process,'title':title,'folder_path':folder,'pid':pid}):
            return False
        if not bool(self.config.get('activity.enabled',True)):
            return False
        excluded={str(x).casefold() for x in self.config.get('activity.excluded_apps',[]) or []}
        if process.casefold() in excluded:
            return False
        private=('incognito','inprivate','private browsing','приватн','инкогнито','navigation privée','privates fenster','navegación privada','navegação privada','navigazione in incognito')
        if any(marker in title.casefold() for marker in private):
            return False
        if folder:
            path=Path(folder).resolve(strict=False)
            if any(path.is_relative_to(Path(p).expanduser().resolve(strict=False)) for p in self.config.get('activity.excluded_paths',[]) or []):
                return False
        return True

    def start(self) -> None:
        if os.name != "nt" or not self.memory.enabled():
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="NeoJarvis-SystemTimeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    @staticmethod
    def _window_text(hwnd: int) -> str:
        try:
            user32 = ctypes.windll.user32
            length = int(user32.GetWindowTextLengthW(int(hwnd)) or 0)
            buffer = ctypes.create_unicode_buffer(max(1, length + 1))
            user32.GetWindowTextW(int(hwnd), buffer, len(buffer))
            return str(buffer.value or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _foreground() -> tuple[int, int, str, str]:
        try:
            import psutil
            user32 = ctypes.windll.user32
            hwnd = int(user32.GetForegroundWindow() or 0)
            if not hwnd:
                return 0, 0, "", ""
            pid_value = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_value))
            pid = int(pid_value.value or 0)
            process = ""
            if pid:
                try:
                    process = str(psutil.Process(pid).name() or "").strip()
                except Exception:
                    pass
            return hwnd, pid, process, SystemContextTracker._window_text(hwnd)
        except Exception:
            return 0, 0, "", ""

    def _explorer_folder(self, hwnd: int, title: str) -> str:
        key = (int(hwnd), str(title or ""))
        if key == self._last_explorer_key:
            return self._last_explorer_folder
        self._last_explorer_key = key
        self._last_explorer_folder = ""
        try:
            from pywinauto import Desktop
            window = Desktop(backend="uia").window(handle=int(hwnd))
            candidates = list(window.descendants(control_type="Edit")) + list(window.descendants(control_type="ToolBar"))
            for item in candidates:
                bits: list[str] = []
                try:
                    bits.append(str(item.window_text() or ""))
                except Exception:
                    pass
                try:
                    bits.append(str(item.element_info.name or ""))
                    bits.append(str(item.element_info.help_text or ""))
                except Exception:
                    pass
                raw = " ".join(bits).strip()
                raw = re.sub(r"^(?:адрес|address)\s*:\s*", "", raw, flags=re.I).strip()
                if re.match(r"^[A-Za-z]:\\", raw):
                    path = Path(raw).expanduser().resolve(strict=False)
                    if path.exists() and path.is_dir():
                        self._last_explorer_folder = str(path)
                        break
        except Exception:
            # UIA exposure differs across Windows builds; missing a folder is
            # preferable to starting PowerShell/COM on every foreground sample.
            pass
        return self._last_explorer_folder

    def _run(self) -> None:
        try:
            interval = float(self.config.get("context_memory.system_poll_seconds", 2.0) or 2.0)
        except Exception:
            interval = 1.5
        interval = max(0.8, min(5.0, interval))
        while not self._stop.wait(interval):
            hwnd, pid, process, title = self._foreground()
            if not hwnd or not self.observation_allowed(process,title,pid=pid):
                continue
            signature = (hwnd, pid, process, title)
            if signature == self._last_signature:
                continue
            self._last_signature = signature
            folder = ""
            # HOTFIX133: pywinauto/PIL UIA import from this background thread was
            # present in a native access-violation crash. Folder-address probing is
            # optional and off by default; foreground process/title memory remains.
            if process.casefold() == "explorer.exe" and bool(
                self.config.get("context_memory.explorer_folder_tracking_enabled", False)
            ):
                folder = self._explorer_folder(hwnd, title)
            if not self.observation_allowed(process,title,folder,pid=pid):
                continue
            payload = {
                "process_name": process, "title": title, "pid": pid,
                "hwnd": hwnd, "folder_path": folder,
            }
            try:
                self.memory.record_system_observation(**payload)
            except Exception:
                logger.debug("System timeline observation failed", exc_info=True)
            callback = self.observation_callback
            if callable(callback):
                try:
                    callback(dict(payload))
                except Exception:
                    logger.debug("System timeline ambient callback failed", exc_info=True)
