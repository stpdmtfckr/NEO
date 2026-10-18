# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class LauncherConsole:
    """Control the console window shared by START_JARVIS.bat and app.py.

    A double-clicked .bat file creates a normal Windows console. Python inherits
    that console, so GetConsoleWindow returns the same HWND. Hiding the HWND keeps
    the launcher and its live stdout/stderr attached while removing it from the
    taskbar/Alt-Tab. Showing it later reveals the *same* live log stream.
    """

    SW_HIDE = 0
    SW_SHOWNORMAL = 1
    SW_SHOW = 5
    SW_RESTORE = 9

    def __init__(self) -> None:
        self._hwnd = 0
        self._user32 = None
        self._kernel32 = None
        if os.name != "nt":
            return
        try:
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._kernel32.GetConsoleWindow.restype = wintypes.HWND
            self._user32.IsWindow.argtypes = [wintypes.HWND]
            self._user32.IsWindow.restype = wintypes.BOOL
            self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
            self._user32.IsWindowVisible.restype = wintypes.BOOL
            self._user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            self._user32.ShowWindow.restype = wintypes.BOOL
            self._user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            self._user32.SetForegroundWindow.restype = wintypes.BOOL
            hwnd = self._kernel32.GetConsoleWindow()
            self._hwnd = int(hwnd or 0)
        except Exception:
            self._hwnd = 0
            self._user32 = None
            self._kernel32 = None

    @property
    def hwnd(self) -> int:
        return int(self._hwnd or 0)

    def available(self) -> bool:
        if not self._hwnd or self._user32 is None:
            return False
        try:
            return bool(self._user32.IsWindow(self._hwnd))
        except Exception:
            return False

    def is_visible(self) -> bool:
        if not self.available():
            return False
        try:
            return bool(self._user32.IsWindowVisible(self._hwnd))
        except Exception:
            return False

    def hide(self) -> bool:
        if not self.available():
            return False
        try:
            self._user32.ShowWindow(self._hwnd, self.SW_HIDE)
            return not self.is_visible()
        except Exception:
            return False

    def show(self, *, activate: bool = True) -> bool:
        if not self.available():
            return False
        try:
            # SW_RESTORE also handles a console the user manually minimized.
            self._user32.ShowWindow(self._hwnd, self.SW_RESTORE)
            self._user32.ShowWindow(self._hwnd, self.SW_SHOW)
            if activate:
                try:
                    self._user32.SetForegroundWindow(self._hwnd)
                except Exception:
                    pass
            return self.is_visible()
        except Exception:
            return False

    def toggle(self) -> bool:
        """Toggle visibility and return the new visible state."""
        if self.is_visible():
            self.hide()
            return False
        return self.show(activate=True)


_default_console: LauncherConsole | None = None


def launcher_console() -> LauncherConsole:
    global _default_console
    if _default_console is None:
        _default_console = LauncherConsole()
    return _default_console


def hide_launcher_console() -> bool:
    return launcher_console().hide()


def show_launcher_console() -> bool:
    return launcher_console().show(activate=True)
