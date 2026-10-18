"""NEO 187 HREQ-003: the native style keeps taskbar minimize/restore.

Only the style bits are checked here with a fake user32; the real Explorer
toggle needs a Windows run.
"""
from jarvis.runtime.self_window184 import (GWL_STYLE, WS_MINIMIZEBOX, WS_SYSMENU,
                                           ensure_taskbar_minimize)


class FakeUser32:
    def __init__(self, style):
        self.style, self.calls = style, []

    def GetWindowLongW(self, hwnd, index):
        assert index == GWL_STYLE
        value = self.style
        return value - 0x100000000 if value >= 0x80000000 else value

    def SetWindowLongW(self, hwnd, index, value):
        self.calls.append(("set", value))
        self.style = value & 0xFFFFFFFF

    def SetWindowPos(self, *args):
        self.calls.append(("pos", args[-1]))
        return 1


def test_missing_minimize_box_is_added_without_touching_other_bits():
    popup = 0x80000000 | 0x00080000 * 0  # WS_POPUP only
    user32 = FakeUser32(popup | 0x10000000)  # WS_VISIBLE
    assert ensure_taskbar_minimize(7, user32)
    assert user32.style == popup | 0x10000000 | WS_MINIMIZEBOX | WS_SYSMENU
    assert user32.calls[-1] == ("pos", 0x0037)


def test_present_style_is_left_alone():
    user32 = FakeUser32(0x80000000 | WS_MINIMIZEBOX | WS_SYSMENU)
    assert ensure_taskbar_minimize(7, user32)
    assert user32.calls == []


def test_failure_is_reported_not_raised():
    class Broken:
        def GetWindowLongW(self, *a):
            raise OSError("no window")
    assert ensure_taskbar_minimize(7, Broken()) is False
