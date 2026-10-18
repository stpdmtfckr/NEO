"""Read-only Explorer snapshot and identity-checked closure of one window.

No focus change, Alt+F4, fuzzy title matching or process termination is used.
Window identity is revalidated after clarification/confirmation; a reused HWND
cannot close a different window. Tabs remain scoped to their containing window.
"""
from __future__ import annotations
import ctypes
import json
import os
import time


def snapshot() -> list[dict]:
    if os.name != 'nt':
        raise OSError('Explorer window control requires Windows')
    import psutil
    import pygetwindow
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    rows = []
    for window in pygetwindow.getAllWindows():
        hwnd = int(getattr(window, '_hWnd', 0) or 0)
        title = str(getattr(window, 'title', '') or '').strip()
        if not hwnd or not title:
            continue
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, len(buf))
        if buf.value.casefold() not in {'cabinetwclass', 'explorewclass'}:
            continue
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            process = psutil.Process(pid.value)
            if process.name().casefold() != 'explorer.exe':
                continue
            rows.append({'hwnd': hwnd, 'pid': pid.value, 'created': process.create_time(), 'title': title})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sorted(rows, key=lambda row: (row['title'].casefold(), row['hwnd']))


def encode(row: dict) -> str:
    return json.dumps({key: row[key] for key in ('hwnd', 'pid', 'created', 'title')}, ensure_ascii=False, separators=(',', ':'))


def close_selected(selection: str) -> tuple[bool, str, str]:
    """Returns status, localized-message key, label. Never selects a replacement."""
    try:
        expected = json.loads(selection)
        if (set(expected) != {'hwnd', 'pid', 'created', 'title'}
                or type(expected['hwnd']) is not int or expected['hwnd'] <= 0
                or type(expected['pid']) is not int or expected['pid'] <= 0
                or not isinstance(expected['created'], (int, float))
                or not isinstance(expected['title'], str)):
            return False, 'folder_changed156', ''
    except (ValueError, TypeError, KeyError):
        return False, 'folder_changed156', ''
    current = next((row for row in snapshot() if row == expected), None)
    if current is None:
        return False, 'folder_changed156', str(expected['title'])
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.IsWindow.argtypes = [wintypes.HWND]
    if not user32.PostMessageW(expected['hwnd'], 0x0010, 0, 0):  # WM_CLOSE only this Explorer window
        return False, 'folder_failed156', str(expected['title'])
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not user32.IsWindow(expected['hwnd']):
            return True, 'folder_closed156', str(expected['title'])
        time.sleep(0.025)
    return False, 'folder_unverified156', str(expected['title'])
