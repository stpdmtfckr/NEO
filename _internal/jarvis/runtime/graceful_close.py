"""WM_CLOSE only; a normal close must never become TerminateProcess."""
from __future__ import annotations
import os
import time


def request_close(pids: set[int], timeout: float = 1.5) -> dict:
    if os.name != 'nt':
        return {'requested':[], 'closed':[], 'pending':[], 'supported':False}
    import ctypes
    from ctypes import wintypes
    api=ctypes.WinDLL('user32',use_last_error=True)
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    api.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    api.IsWindowVisible.argtypes=[wintypes.HWND]
    api.IsWindow.argtypes=[wintypes.HWND]
    api.PostMessageW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
    api.EnumWindows.argtypes=[callback_type,wintypes.LPARAM]
    targets=[]
    @callback_type
    def visit(hwnd,_):
        pid=wintypes.DWORD()
        api.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        if pid.value in pids and api.IsWindowVisible(hwnd):
            targets.append((int(hwnd),pid.value))
        return True
    api.EnumWindows(visit,0)
    sent=[{'hwnd':hwnd,'pid':pid} for hwnd,pid in targets if api.PostMessageW(hwnd,0x0010,0,0)]
    deadline=time.monotonic()+min(3.,max(0.,timeout))
    while time.monotonic()<deadline and any(api.IsWindow(r['hwnd']) for r in sent):
        time.sleep(.05)
    closed=[r for r in sent if not api.IsWindow(r['hwnd'])]
    pending=[r for r in sent if api.IsWindow(r['hwnd'])]
    return {'requested':sent,'closed':closed,'pending':pending,'supported':True}

