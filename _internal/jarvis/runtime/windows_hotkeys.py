"""Pointer-width-safe Win32 hotkey calls; never changes global OS settings."""
from __future__ import annotations


def register_hotkey(hwnd: int, identifier: int, modifiers: int, key: int) -> tuple[bool, int]:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    register = user32.RegisterHotKey
    register.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
    register.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    ok = bool(register(hwnd, identifier, modifiers, key))
    return ok, 0 if ok else ctypes.get_last_error()


def unregister_hotkey(hwnd: int, identifier: int) -> bool:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    unregister = user32.UnregisterHotKey
    unregister.argtypes = (wintypes.HWND, ctypes.c_int)
    unregister.restype = wintypes.BOOL
    return bool(unregister(hwnd, identifier))
