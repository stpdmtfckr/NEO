# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Windows display-number mapping independent from Qt enumeration order.

Qt's QApplication.screens() order is not guaranteed to be the same numbering
shown in Windows Settings.  On Windows the stable display device name is usually
``\\\\.\\DISPLAY<n>``.  We parse that first and, when necessary, correlate Qt's
virtual-desktop geometry with MONITORINFOEX returned by User32.
"""

import ctypes
import os
import re
from ctypes import wintypes
from typing import Iterable


def display_number_from_name(value: str) -> int | None:
    text = str(value or "")
    match = re.search(r"(?:\\\\\.\\)?DISPLAY\s*([1-9][0-9]*)", text, flags=re.I)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def windows_monitor_geometries() -> list[tuple[int, tuple[int, int, int, int], str]]:
    """Return ``(windows_number, (left, top, right, bottom), device_name)``."""
    if os.name != "nt":
        return []

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    user32 = ctypes.windll.user32
    MonitorEnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM)
    result: list[tuple[int, tuple[int, int, int, int], str]] = []

    def callback(hmonitor, _hdc, _rect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        try:
            ok = bool(user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)))
        except Exception:
            ok = False
        if ok:
            device = str(info.szDevice or "")
            number = display_number_from_name(device)
            if number is not None:
                rect = info.rcMonitor
                result.append((number, (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)), device))
        return True

    cb = MonitorEnumProc(callback)
    try:
        user32.EnumDisplayMonitors(None, None, cb, 0)
    except Exception:
        return []
    result.sort(key=lambda x: x[0])
    return result


def _overlap_area(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    left = max(a[0], b[0]); top = max(a[1], b[1])
    right = min(a[2], b[2]); bottom = min(a[3], b[3])
    return max(0, right - left) * max(0, bottom - top)


def display_number_for_geometry(geometry: tuple[int, int, int, int], native: Iterable[tuple[int, tuple[int, int, int, int], str]] | None = None) -> int | None:
    items = list(native if native is not None else windows_monitor_geometries())
    if not items:
        return None
    scored = sorted((( _overlap_area(geometry, rect), number) for number, rect, _name in items), reverse=True)
    return scored[0][1] if scored and scored[0][0] > 0 else None
