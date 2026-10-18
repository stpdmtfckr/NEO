# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Deterministic Discord desktop-client integration.

This module deliberately does *not* use a normal user's Discord token or the
Gateway. Discord prohibits self-bots. Every action here is an explicit action
requested by the local user and is performed through the installed desktop
client using its documented keyboard navigation plus Windows UI Automation.
"""

import ctypes
from difflib import SequenceMatcher
import logging
import os
import re
import subprocess

from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
import time
from pathlib import Path
from typing import Any

import psutil
import numpy as np

logger = logging.getLogger(__name__)

_BROWSER_NAMES = {"firefox.exe", "chrome.exe", "msedge.exe", "opera.exe", "opera_gx.exe", "browser.exe"}


def _windows_fast_path_enabled() -> bool:
    return os.name == "nt"


def _n(value: str) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9#*!@._ -]+", " ", text).strip()


def _discord_processes() -> list[psutil.Process]:
    result: list[psutil.Process] = []
    for proc in psutil.process_iter(["name", "exe", "cmdline"]):
        try:
            if _is_discord_process_identity(
                str(proc.info.get("name") or ""),
                str(proc.info.get("exe") or ""),
                proc.info.get("cmdline") or [],
            ):
                result.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return result


def _is_discord_process_identity(name: str = "", exe: str = "", cmdline: Any = None) -> bool:
    """Return True only for a real Discord process, never from a window title.

    The old title fallback was unsafe: a Windows Explorer folder named
    ``...DISCORD...`` was enough to be treated as the Discord client, so Ctrl+K
    was injected into Explorer.  Process identity is the only admissible signal.
    """
    name_n = str(name or "").casefold().strip()
    exe_n = str(exe or "").casefold().strip()
    if name_n in _BROWSER_NAMES:
        return False
    exe_base = Path(exe_n).name.casefold() if exe_n else ""
    # Normal, PTB and Canary builds all use a Discord-branded executable name.
    # Command-line text and window titles are deliberately ignored.
    allowed = {"discord.exe", "discordptb.exe", "discordcanary.exe", "discorddevelopment.exe"}
    return name_n in allowed or exe_base in allowed


def _discord_pid_set() -> set[int]:
    return {int(proc.pid) for proc in _discord_processes()}


def _foreground_window_info() -> tuple[int, int]:
    if os.name != "nt":
        return 0, 0
    try:
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow() or 0)
        pid = ctypes.c_ulong(0)
        if hwnd:
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return hwnd, int(pid.value)
    except Exception:
        return 0, 0


def _foreground_is_discord() -> bool:
    if os.name != "nt":
        return True
    _hwnd, pid = _foreground_window_info()
    if not pid:
        return False
    try:
        proc = psutil.Process(pid)
        return _is_discord_process_identity(proc.name(), proc.exe(), proc.cmdline())
    except Exception:
        return pid in _discord_pid_set()


def _window_handle(window: Any | None) -> int:
    if window is None:
        return 0
    for value in (
        getattr(window, "handle", 0),
        getattr(getattr(window, "element_info", None), "handle", 0),
    ):
        try:
            if int(value or 0):
                return int(value)
        except Exception:
            pass
    return 0



def _hwnd_process_id(hwnd: int) -> int:
    if os.name != "nt" or not hwnd:
        return 0
    try:
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _hwnd_class_name(hwnd: int) -> str:
    if os.name != "nt" or not hwnd:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(int(hwnd), buf, len(buf))
        return str(buf.value or "")
    except Exception:
        return ""


def _gui_focus_hwnd_for_thread(thread_id: int) -> int:
    """Return the HWND that owns keyboard focus in another GUI thread."""
    if os.name != "nt" or not thread_id:
        return 0
    try:
        from ctypes import wintypes

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT),
            ]

        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if ctypes.windll.user32.GetGUIThreadInfo(int(thread_id), ctypes.byref(info)):
            return int(info.hwndFocus or 0)
    except Exception:
        pass
    return 0


def _ensure_discord_keyboard_focus(window: Any | None = None, *, timeout: float = 0.9) -> bool:
    """Ensure the Discord GUI thread has a real keyboard-focus HWND.

    ``GetForegroundWindow`` proves only which *top-level* app is active. Chromium
    can still have no keyboard-focus window after programmatic activation. In that
    state a synthetic Ctrl+K can be inserted successfully yet be ignored. Windows
    explicitly supports attaching input queues before calling SetFocus on a
    window owned by another GUI thread, which is what we do here.
    """
    if os.name != "nt":
        return True

    hwnd = _window_handle(window)
    if not hwnd:
        rows = _discord_hwnds()
        if rows:
            rows.sort(
                key=lambda r: max(0, r["rect"][2] - r["rect"][0]) * max(0, r["rect"][3] - r["rect"][1]),
                reverse=True,
            )
            hwnd = int(rows[0].get("hwnd", 0) or 0)
    if not hwnd:
        return False

    if not _foreground_is_discord() and not _force_discord_foreground(window, timeout=max(0.6, timeout)):
        return False

    try:
        user32 = ctypes.windll.user32
        target_pid = ctypes.c_ulong(0)
        target_tid = int(user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid)) or 0)
        if not target_tid:
            return False

        def focus_is_discord() -> tuple[bool, int, str]:
            focus_hwnd = _gui_focus_hwnd_for_thread(target_tid)
            focus_pid = _hwnd_process_id(focus_hwnd)
            ok = bool(focus_hwnd and focus_pid in _discord_pid_set())
            return ok, focus_hwnd, _hwnd_class_name(focus_hwnd)

        ok, focus_hwnd, focus_class = focus_is_discord()
        if ok:
            logger.debug(
                "Discord keyboard focus already valid: hwnd=%s class=%r pid=%s",
                focus_hwnd, focus_class, _hwnd_process_id(focus_hwnd),
            )
            return True

        current_tid = int(ctypes.windll.kernel32.GetCurrentThreadId() or 0)
        attached = False
        if current_tid and current_tid != target_tid:
            try:
                attached = bool(user32.AttachThreadInput(current_tid, target_tid, True))
            except Exception:
                attached = False
        try:
            # SetActiveWindow/SetFocus are meaningful only for an attached input
            # queue when targeting a foreign GUI thread.
            try:
                user32.SetActiveWindow(hwnd)
            except Exception:
                pass
            try:
                user32.SetFocus(hwnd)
            except Exception:
                pass
        finally:
            if attached:
                try:
                    user32.AttachThreadInput(current_tid, target_tid, False)
                except Exception:
                    pass

        deadline = time.monotonic() + max(0.15, float(timeout))
        while time.monotonic() < deadline:
            ok, focus_hwnd, focus_class = focus_is_discord()
            if ok:
                logger.info(
                    "Discord keyboard focus verified: hwnd=%s class=%r pid=%s",
                    focus_hwnd, focus_class, _hwnd_process_id(focus_hwnd),
                )
                return True
            time.sleep(0.04)

        logger.warning(
            "Discord is foreground but keyboard focus is unresolved: main_hwnd=%s target_tid=%s focus_hwnd=%s class=%r",
            hwnd, target_tid, focus_hwnd, focus_class,
        )
        return False
    except Exception as exc:
        logger.warning("Discord keyboard-focus verification failed: %s", exc)
        return False


def _force_discord_foreground(window: Any | None = None, *, timeout: float = 2.2) -> bool:
    """Bring the real Discord HWND forward and prove it owns foreground input.

    Windows can legally refuse SetForegroundWindow for a background process.
    We therefore combine normal UIA focus, Win32 restore/z-order, an Alt-unlock
    attempt and finally a title-bar click, and *verify the foreground PID* before
    any global keyboard injection is allowed.
    """
    if os.name != "nt":
        return True
    if _foreground_is_discord():
        return True

    hwnd = _window_handle(window)
    if not hwnd:
        rows = _discord_hwnds()
        if rows:
            rows.sort(key=lambda r: max(0, r["rect"][2]-r["rect"][0]) * max(0, r["rect"][3]-r["rect"][1]), reverse=True)
            hwnd = int(rows[0]["hwnd"])
    if not hwnd:
        return False

    deadline = time.monotonic() + max(0.4, float(timeout))
    try:
        if window is not None:
            try:
                window.restore()
            except Exception:
                pass
            try:
                window.set_focus()
            except Exception:
                pass
    except Exception:
        pass

    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW

    while time.monotonic() < deadline:
        try:
            user32.ShowWindowAsync(hwnd, SW_RESTORE)
            # The temporary TOPMOST dance changes z-order without pretending that
            # SetForegroundWindow itself is guaranteed to succeed.
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        if _foreground_is_discord():
            return True

        # ALT is a user-input compatible foreground-unlock technique on Windows;
        # it is harmless by itself and we still verify the PID afterwards.
        try:
            VK_MENU = 0x12
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.SetForegroundWindow(hwnd)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        except Exception:
            pass
        if _foreground_is_discord():
            return True

        # Last resort: put the verified Discord HWND on top, then create a real
        # mouse activation event in its title/drag region. This cannot target an
        # Explorer window because ``hwnd`` came only from a Discord process PID.
        try:
            rect = None
            for row in _discord_hwnds():
                if int(row.get("hwnd", 0)) == hwnd:
                    rect = tuple(row.get("rect", (0, 0, 0, 0)))
                    break
            if rect and rect[2] > rect[0] and rect[3] > rect[1]:
                import pyautogui
                x = int((rect[0] + rect[2]) / 2)
                y = int(rect[1] + min(18, max(8, (rect[3]-rect[1]) * 0.02)))
                pyautogui.click(x, y)
        except Exception:
            pass
        if _foreground_is_discord():
            return True
        time.sleep(0.08)
    return False


def _discord_hwnds() -> list[dict[str, Any]]:
    """Find Discord top-level windows through Win32 only.

    v3.7 asked UI Automation to enumerate every visible desktop window before it
    could even start the Discord workflow.  On some Electron/Chromium trees that
    can stall for a very long time.  HWND/PID/title are sufficient to identify and
    activate Discord, so keep the top-level path UIA-free.
    """
    if os.name != "nt":
        return []
    discord_pids = {int(p.pid) for p in _discord_processes()}
    results: list[dict[str, Any]] = []
    try:
        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        @WNDENUMPROC
        def callback(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = int(user32.GetWindowTextLengthW(hwnd) or 0)
                buf = ctypes.create_unicode_buffer(min(max(length + 1, 2), 4096))
                user32.GetWindowTextW(hwnd, buf, len(buf))
                title = str(buf.value or "").strip()
                pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                pid_i = int(pid.value)
                # Never accept a window merely because its title contains
                # "Discord". Explorer can show a build folder named DISCORD_...
                # and would then receive Ctrl+K intended for Discord.
                if pid_i in discord_pids:
                    class _RECT(ctypes.Structure):
                        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
                    rect = _RECT()
                    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        coords = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
                    else:
                        coords = (0, 0, 0, 0)
                    results.append({"hwnd": int(hwnd), "pid": pid_i, "title": title, "rect": coords})
            except Exception:
                pass
            return True

        user32.EnumWindows(callback, 0)
    except Exception:
        return []
    return results


def _activate_discord_hwnd(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        SW_RESTORE = 9
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_SHOWWINDOW = 0x0040
        user32.ShowWindowAsync(hwnd, SW_RESTORE)
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        for _ in range(4):
            if int(user32.GetForegroundWindow()) == int(hwnd):
                return True
            time.sleep(0.05)
            user32.SetForegroundWindow(hwnd)
        return int(user32.GetForegroundWindow()) == int(hwnd)
    except Exception:
        return False


def _discord_windows() -> list[Any]:
    if os.name != "nt":
        return []
    try:
        from pywinauto import Desktop
        windows = Desktop(backend="uia").windows(visible_only=True)
    except Exception:
        return []
    result: list[Any] = []
    for window in windows:
        try:
            pid = int(window.element_info.process_id)
            proc = psutil.Process(pid)
            name = str(proc.name() or "").casefold()
            exe = str(proc.exe() or "").casefold()
            title = str(window.window_text() or "").casefold()
            if name in _BROWSER_NAMES:
                continue
            if _is_discord_process_identity(name, exe):
                result.append(window)
        except Exception:
            continue
    def _area(win: Any) -> int:
        try:
            r = win.rectangle()
            return max(0, int(r.right)-int(r.left)) * max(0, int(r.bottom)-int(r.top))
        except Exception:
            return 0
    result.sort(key=_area, reverse=True)
    return result


def _focus_discord_hwnd() -> tuple[int, tuple[int, int, int, int]]:
    rows = _discord_hwnds()
    if not rows:
        launch_discord()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            rows = _discord_hwnds()
            if rows:
                break
            time.sleep(0.20)
    if not rows:
        raise RuntimeError("Не вижу окно Discord")
    # Prefer the largest Discord window; this avoids notification/helper HWNDs.
    rows.sort(key=lambda r: max(0, r["rect"][2]-r["rect"][0]) * max(0, r["rect"][3]-r["rect"][1]), reverse=True)
    row = rows[0]
    _activate_discord_hwnd(int(row["hwnd"]))
    return int(row["hwnd"]), tuple(row["rect"])


def _find_discord_install() -> list[str] | None:
    if os.name != "nt":
        return None
    local = Path(os.getenv("LOCALAPPDATA", ""))
    update = local / "Discord" / "Update.exe"
    if update.exists():
        return [str(update), "--processStart", "Discord.exe"]
    root = local / "Discord"
    if root.exists():
        apps = sorted(root.glob("app-*/Discord.exe"), reverse=True)
        if apps:
            return [str(apps[0])]
    # Standard StartApps fallback.  It never uses a browser.
    script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$a=Get-StartApps | Where-Object {$_.Name -match 'Discord'} | Select-Object -First 1; "
        "if($a){$a.AppID}"
    )
    try:
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3.5, check=False,
            **hidden_process_kwargs(),
        )
        app_id = cp.stdout.strip()
        if cp.returncode == 0 and app_id:
            return ["explorer.exe", f"shell:AppsFolder\\{app_id}"]
    except Exception:
        pass
    return None


def launch_discord() -> dict[str, Any]:
    if os.name != "nt":
        raise OSError("Discord desktop integration is available on Windows")

    # Electron can remain resident in the tray after its main window is closed.
    # A resident process is therefore not sufficient proof that the desktop client
    # is usable.  If a real window already exists, keep it. Otherwise invoke the
    # registered launcher again so Discord restores/creates its main window.
    had_process = bool(_discord_processes())
    if _discord_hwnds():
        return {"started": False, "already_running": True, "window_verified": True}

    command = _find_discord_install()
    if not command:
        if had_process:
            raise RuntimeError(
                "Discord работает в фоне, но его окно скрыто, а способ повторного запуска не найден. "
                "Откройте Discord один раз вручную или переустановите desktop-клиент."
            )
        raise RuntimeError("Не нашёл установленный Discord. Установите desktop-клиент Discord и повторите команду.")

    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if _discord_hwnds():
            return {
                "started": not had_process,
                "reactivated": had_process,
                "already_running": had_process,
                "window_verified": True,
                "command": command[0],
            }
        time.sleep(0.25)
    raise RuntimeError(
        "Discord был запущен/активирован, но его видимое окно не появилось в течение 15 секунд. "
        "NEO не будет сообщать ложный успех."
    )


def focus_discord_window() -> Any:
    windows = _discord_windows()
    if not windows:
        launch_discord()
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            windows = _discord_windows()
            if windows:
                break
            time.sleep(0.25)
    if not windows:
        raise RuntimeError("Не вижу окно Discord")

    # ``_discord_windows`` now contains only windows owned by Discord.exe and is
    # sorted largest-first. Do not trust set_focus() alone: Windows may refuse it.
    window = windows[0]
    try:
        pid = int(window.element_info.process_id)
    except Exception:
        pid = 0
    try:
        title = str(window.window_text() or "")
    except Exception:
        title = ""
    # Preserve the old v3.3 focus attempt first; the native guard below then
    # verifies whether Windows actually granted foreground ownership.
    try:
        window.restore()
    except Exception:
        pass
    try:
        window.set_focus()
    except Exception:
        try:
            window.click_input()
        except Exception:
            pass
    if not _force_discord_foreground(window):
        fg_hwnd, fg_pid = _foreground_window_info()
        raise RuntimeError(
            "Не удалось передать клавиатурный фокус реальному окну Discord; "
            f"клавиши не отправлялись (target_pid={pid}, foreground_pid={fg_pid}, foreground_hwnd={fg_hwnd})."
        )
    logger.info("Discord foreground verified: pid=%s title=%r", pid, title)
    return window


def _descriptor(control: Any) -> str:
    try:
        return _n(" ".join([
            str(control.window_text() or ""),
            str(getattr(control.element_info, "name", "") or ""),
            str(getattr(control.element_info, "help_text", "") or ""),
            str(getattr(control.element_info, "automation_id", "") or ""),
        ]))
    except Exception:
        return ""


def _descendants(window: Any) -> list[Any]:
    try:
        return list(window.descendants())[:2500]
    except Exception:
        return []


def _has_text(window: Any, needle: str) -> bool:
    target = _n(needle)
    if not target:
        return False
    for control in _descendants(window):
        desc = _descriptor(control)
        if target == desc or target in desc:
            return True
    return False


def _find_controls(window: Any, words: tuple[str, ...], *, control_types: set[str] | None = None) -> list[Any]:
    result: list[tuple[int, Any]] = []
    normalized = tuple(_n(x) for x in words if x)
    for control in _descendants(window):
        desc = _descriptor(control)
        if not desc:
            continue
        try:
            ctype = str(getattr(control.element_info, "control_type", "") or "")
        except Exception:
            ctype = ""
        if control_types and ctype not in control_types:
            continue
        score = sum(5 for word in normalized if word and word in desc)
        if score:
            if ctype in {"Button", "Edit", "Document", "ListItem", "Hyperlink", "Text"}:
                score += 1
            result.append((score, control))
    result.sort(key=lambda item: item[0], reverse=True)
    return [control for _, control in result]


def _invoke(control: Any) -> bool:
    for method in ("invoke", "click_input", "click"):
        fn = getattr(control, method, None)
        if callable(fn):
            try:
                fn()
                return True
            except Exception:
                continue
    return False


def _sendinput_key_sequence(vks: list[int]) -> bool:
    """Inject a key chord with a correctly-sized Win32 ``INPUT`` structure.

    v3.9.7 accidentally defined the INPUT union with KEYBDINPUT only. On 64-bit
    Windows that makes ``ctypes.sizeof(INPUT)`` smaller than the real Win32
    INPUT structure, so SendInput rejects the call because ``cbSize`` is wrong.
    Keep all three official union members so the native ABI size is correct.
    """
    if os.name != "nt" or not vks:
        return False
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [
                ("mi", MOUSEINPUT),
                ("ki", KEYBDINPUT),
                ("hi", HARDWAREINPUT),
            ]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        MAPVK_VK_TO_VSC = 0

        rows: list[INPUT] = []
        for vk in vks:
            scan = int(user32.MapVirtualKeyW(int(vk), MAPVK_VK_TO_VSC) or 0)
            rows.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(int(vk), scan, 0, 0, 0)))
        for vk in reversed(vks):
            scan = int(user32.MapVirtualKeyW(int(vk), MAPVK_VK_TO_VSC) or 0)
            rows.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(int(vk), scan, KEYEVENTF_KEYUP, 0, 0)))

        array_type = INPUT * len(rows)
        payload = array_type(*rows)
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        ctypes.set_last_error(0)
        sent = int(user32.SendInput(len(rows), payload, ctypes.sizeof(INPUT)) or 0)
        err = int(ctypes.get_last_error() or 0)
        ok = sent == len(rows)
        if not ok:
            logger.warning(
                "Discord SendInput failed: sent=%s/%s input_size=%s last_error=%s",
                sent, len(rows), ctypes.sizeof(INPUT), err,
            )
        else:
            logger.debug(
                "Discord SendInput ok: events=%s input_size=%s chord=%s",
                sent, ctypes.sizeof(INPUT), [hex(int(v)) for v in vks],
            )
        return ok
    except Exception as exc:
        logger.warning("Discord SendInput exception: %s", exc)
        return False

def _discord_press(key: str) -> None:
    """Press a navigation key while refusing to inject outside Discord."""
    if not _foreground_is_discord():
        raise RuntimeError(f"Discord потерял foreground перед клавишей {key!r}")
    mapping = {
        "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
        "tab": 0x09, "home": 0x24, "down": 0x28, "up": 0x26,
        "backspace": 0x08,
    }
    vk = mapping.get(str(key or "").casefold())
    if vk is not None and _sendinput_key_sequence([vk]):
        return
    import pyautogui
    pyautogui.press(key)


def _discord_hotkey(*keys: str) -> None:
    if not _foreground_is_discord():
        raise RuntimeError(f"Discord потерял foreground перед hotkey {keys!r}")
    vk_map = {
        "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
        "k": 0x4B, "v": 0x56, "a": 0x41,
    }
    vks = [vk_map.get(str(k).casefold()) for k in keys]
    if all(v is not None for v in vks) and _sendinput_key_sequence([int(v) for v in vks if v is not None]):
        return
    try:
        from pywinauto.keyboard import send_keys
        expr = ""
        for k in keys[:-1]:
            kn = str(k).casefold()
            expr += {"ctrl": "^", "control": "^", "shift": "+", "alt": "%"}.get(kn, "")
        expr += str(keys[-1]) if keys else ""
        if expr:
            send_keys(expr, pause=0.02)
            return
    except Exception:
        pass
    import pyautogui
    pyautogui.hotkey(*keys)


def _paste(text: str) -> None:
    import pyperclip
    previous = pyperclip.paste()
    try:
        pyperclip.copy(str(text))
        if _foreground_is_discord():
            _discord_hotkey("ctrl", "v")
        else:
            import pyautogui
            pyautogui.hotkey("ctrl", "v")
        time.sleep(0.07)
    finally:
        try:
            pyperclip.copy(previous)
        except Exception:
            pass


def _discord_visual_snapshot(*, bottom: bool = False) -> np.ndarray | None:
    """Return a tiny grayscale snapshot of the real Discord window.

    This is deliberately OCR-free.  It is used only as a state-transition
    witness: Ctrl+K must visibly change the central UI before NEO is allowed
    to type a server/channel query; composer/slash actions must visibly change
    the lower chat area before success can be reported.
    """
    if os.name != "nt":
        return None
    rows = _discord_hwnds()
    if not rows:
        return None
    rows.sort(key=lambda r: max(0, r["rect"][2]-r["rect"][0]) * max(0, r["rect"][3]-r["rect"][1]), reverse=True)
    left, top, right, bottom_y = rows[0]["rect"]
    width = max(1, right-left)
    height = max(1, bottom_y-top)
    if bottom:
        x1 = int(left + width * 0.24)
        x2 = int(left + width * 0.86)
        y1 = int(top + height * 0.72)
        y2 = int(bottom_y - max(4, height * 0.01))
    else:
        # Quick Switcher is a large modal in the upper/central content area.
        # Keep the crop away from the chat timeline so animated GIFs/video do not
        # masquerade as a successful Ctrl+K transition.
        x1 = int(left + width * 0.22)
        x2 = int(left + width * 0.80)
        y1 = int(top + height * 0.07)
        y2 = int(top + height * 0.48)
    if x2 <= x1 or y2 <= y1:
        return None
    try:
        from PIL import ImageGrab
        image = ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True).convert("L")
        image = image.resize((72, 42))
        return np.asarray(image, dtype=np.float32)
    except Exception:
        return None


def _visual_delta(before: np.ndarray | None, after: np.ndarray | None) -> float:
    if before is None or after is None or before.shape != after.shape:
        return -1.0
    try:
        return float(np.mean(np.abs(after.astype(np.float32) - before.astype(np.float32))))
    except Exception:
        return -1.0


def _quick_switcher_uia_visible(window: Any) -> bool:
    """Best-effort accessibility witness that Ctrl+K opened Quick Switcher."""
    for control in _descendants(window):
        try:
            ctype = _control_type(control)
            if ctype not in {"Edit", "Document", "Pane", "Dialog"}:
                continue
            desc = _descriptor(control)
            if not any(token in desc for token in (
                "quick switch", "быстрый переход", "куда", "where would you like",
                "find or start", "найти или начать",
            )):
                continue
            focus = getattr(control, "has_keyboard_focus", None)
            if callable(focus):
                try:
                    if focus():
                        return True
                except Exception:
                    pass
            if ctype in {"Pane", "Dialog"}:
                return True
        except Exception:
            continue
    return False


def _open_quick_switcher_verified(window: Any) -> tuple[str, np.ndarray | None]:
    """Open Discord Quick Switcher and prove that Discord consumed Ctrl+K.

    The order deliberately combines the corrected native SendInput path with the
    *exact* legacy pyautogui chord used in v3.0-v3.3.  Before each attempt we
    verify not only the foreground process but also the GUI thread's keyboard
    focus, because Chromium can be foreground while its renderer has no focus.
    """
    if not _foreground_is_discord():
        focus_discord_window()
    if not _foreground_is_discord():
        raise RuntimeError("Discord не владеет foreground; Ctrl+K не отправлялся")

    attempts = ("sendinput", "legacy_pyautogui", "pywinauto_window", "pywinauto_global")
    for method in attempts:
        # Re-establish the same input preconditions before every retry.  A failed
        # attempt can leave Chromium focus in a transient popup/menu state.
        focus_discord_window()
        keyboard_focus = _ensure_discord_keyboard_focus(window)
        if not keyboard_focus:
            logger.warning("Discord Ctrl+K attempt=%s: foreground yes, keyboard-focus witness unavailable", method)

        before = _discord_visual_snapshot(bottom=False)
        delivered = False
        try:
            if method == "sendinput":
                delivered = _sendinput_key_sequence([0x11, 0x4B])
            elif method == "legacy_pyautogui":
                # Exact mechanism from v3.0-v3.3.
                import pyautogui
                pyautogui.hotkey("ctrl", "k", interval=0.045)
                delivered = True
            elif method == "pywinauto_window":
                # Send through the concrete Discord wrapper instead of whichever
                # HWND happens to own the current Python thread's focus.
                fn = getattr(window, "type_keys", None)
                if callable(fn):
                    fn("^k", pause=0.04, set_foreground=False)
                    delivered = True
            else:
                from pywinauto.keyboard import send_keys
                send_keys("^k", pause=0.04)
                delivered = True
        except Exception as exc:
            logger.warning("Discord Ctrl+K injection method=%s raised: %s", method, exc)
            delivered = False

        if not delivered:
            continue

        # Discord's modal animation is not instantaneous on slower Electron
        # builds.  Sample twice before declaring that the chord did nothing.
        best_delta = -1.0
        best_after = None
        uia = False
        for wait in (0.28, 0.34):
            time.sleep(wait)
            after = _discord_visual_snapshot(bottom=False)
            delta = _visual_delta(before, after)
            if delta > best_delta:
                best_delta = delta
                best_after = after
            if delta < 2.25:
                try:
                    uia = _quick_switcher_uia_visible(window)
                except Exception:
                    uia = False
            if delta >= 2.25 or uia:
                logger.info(
                    "Discord Ctrl+K opened Quick Switcher: method=%s visual_delta=%.2f uia=%s keyboard_focus=%s",
                    method, delta, uia, keyboard_focus,
                )
                return method, after

        logger.info(
            "Discord Ctrl+K attempt method=%s delivered=%s visual_delta=%.2f uia=%s keyboard_focus=%s",
            method, delivered, best_delta, uia, keyboard_focus,
        )
        # Escape only when the chord might have opened a hidden/transient menu.
        # This is safe because foreground/process identity was already verified.
        try:
            _discord_press("esc")
        except Exception:
            pass
        time.sleep(0.10)

    raise RuntimeError(
        "Discord получил фокус, но Ctrl+K не открыл Quick Switcher: ни исправленный SendInput, "
        "ни точный legacy-механизм v3.0-v3.3 не вызвали окно переключения. "
        "NEO не вводил название сервера/канала в обычный чат."
    )

def _quick_switch(prefix: str, query: str) -> dict[str, Any]:
    query = str(query or "").strip()
    if not query:
        raise ValueError("Не указано название для Discord Quick Switcher")
    window = focus_discord_window()
    method, opened_frame = _open_quick_switcher_verified(window)
    _paste(f"{prefix}{query}")
    time.sleep(0.75)

    # If Electron exposes the composer and the search text somehow landed there,
    # stop before Enter so we never send '*server'/'#channel' as a chat message.
    editor = _message_editor(window)
    if editor is not None:
        value = _n(_editor_value(editor))
        if _n(query) and _n(query) in value and not _quick_switcher_uia_visible(window):
            _clear_editor(editor)
            raise RuntimeError("Ctrl+K не удержал Quick Switcher: поисковый текст попал в поле сообщения; Enter не отправлялся")

    _discord_press("enter")
    time.sleep(0.80)
    closed_frame = _discord_visual_snapshot(bottom=False)
    close_delta = _visual_delta(opened_frame, closed_frame)
    still_open = False if close_delta >= 1.15 else _quick_switcher_uia_visible(window)
    logger.info(
        "Discord Quick Switch selected prefix=%r query=%r method=%s close_delta=%.2f still_open=%s",
        prefix, query, method, close_delta, still_open,
    )
    if still_open:
        try:
            _discord_press("esc")
        except Exception:
            pass
        raise RuntimeError(f"Discord Quick Switcher не закрылся после выбора {prefix}{query!s}")
    # A closed overlay should visibly differ from the opened one.  If screenshots
    # are unavailable (-1), keep the UIA/foreground evidence rather than failing.
    if close_delta >= 0 and close_delta < 1.15:
        raise RuntimeError(f"Discord не показал переход после выбора {prefix}{query!s}")
    return {"method": method, "visual_open_delta": close_delta, "query": query, "prefix": prefix}


def _quick_switch_fast(prefix: str, query: str) -> None:
    """Keyboard-only Quick Switcher path used by latency-critical music commands."""
    import pyautogui
    query = str(query or "").strip()
    if not query:
        raise ValueError("Не указано название для Discord Quick Switcher")
    _focus_discord_hwnd()
    pyautogui.hotkey("ctrl", "k")
    time.sleep(0.16)
    _paste(f"{prefix}{query}")
    time.sleep(0.42)
    pyautogui.press("enter")
    time.sleep(0.48)


def _focus_composer_by_geometry() -> tuple[int, int]:
    """Focus Discord's message composer without traversing Chromium accessibility.

    The composer is anchored along the bottom of the channel view.  Relative
    geometry is stable across maximized/windowed layouts and avoids a potentially
    unbounded ``window.descendants()`` call.
    """
    import pyautogui
    focus_discord_window()
    if not _foreground_is_discord():
        raise RuntimeError("Discord не владеет foreground перед кликом по composer")
    rows = _discord_hwnds()
    if not rows:
        raise RuntimeError("Не вижу HWND основного окна Discord")
    rows.sort(key=lambda r: max(0, r["rect"][2]-r["rect"][0]) * max(0, r["rect"][3]-r["rect"][1]), reverse=True)
    left, top, right, bottom = tuple(rows[0]["rect"])
    width = max(1, right - left)
    height = max(1, bottom - top)
    # Stay away from the left channel rail and the far-right member list.
    x = int(left + width * 0.56)
    y = int(bottom - max(38, min(64, height * 0.065)))
    pyautogui.click(x=x, y=y)
    time.sleep(0.10)
    return x, y


def _clear_focused_composer() -> None:
    if not _foreground_is_discord():
        raise RuntimeError("Discord потерял foreground перед очисткой поля сообщения")
    _discord_hotkey("ctrl", "a")
    _discord_press("backspace")
    time.sleep(0.05)


def _visual_change_ratio(before: np.ndarray | None, after: np.ndarray | None, *, threshold: float = 5.0) -> float:
    if before is None or after is None or before.shape != after.shape:
        return -1.0
    try:
        diff = np.abs(after.astype(np.float32) - before.astype(np.float32))
        return float(np.mean(diff >= float(threshold)))
    except Exception:
        return -1.0


def _visual_changed(before: np.ndarray | None, after: np.ndarray | None, *, min_delta: float = 0.10, min_ratio: float = 0.004) -> bool:
    delta = _visual_delta(before, after)
    ratio = _visual_change_ratio(before, after)
    if delta < 0 and ratio < 0:
        # Screen capture may be unavailable on locked/remote desktops.  The caller
        # must then rely on a UIA witness instead of declaring visual success.
        return False
    return delta >= float(min_delta) or ratio >= float(min_ratio)


def _music_slash_visual_fallback(window: Any, slash: str, argument: str, option_name: str) -> dict[str, Any]:
    """Restore the working v3.0 keyboard slash flow when Discord hides UIA.

    The user's current Electron build exposes the top-level Discord HWND but not
    the message contenteditable.  In that situation trying more UIA scans only
    adds delay.  We intentionally replay the old sequence that worked in v3.0:
    click composer -> /play -> Enter -> query -> Enter.  Unlike v3.0, every step
    is guarded by the real Discord PID and a visual state transition.
    """
    if not _foreground_is_discord():
        focus_discord_window()
    if not _foreground_is_discord():
        raise RuntimeError("Discord не владеет foreground перед /play")

    try:
        _discord_press("esc")
    except Exception:
        pass
    x, y = _focus_composer_by_geometry()
    if not _foreground_is_discord():
        raise RuntimeError("Discord потерял foreground после фокусировки поля сообщения")
    _clear_focused_composer()

    before_slash = _discord_visual_snapshot(bottom=True)
    _paste(slash)
    time.sleep(0.65)
    after_slash = _discord_visual_snapshot(bottom=True)
    slash_changed = _visual_changed(before_slash, after_slash, min_delta=0.08, min_ratio=0.0025)
    logger.info(
        "Discord visual slash fallback: typed=%r delta=%.3f ratio=%.4f composer=(%s,%s)",
        slash, _visual_delta(before_slash, after_slash), _visual_change_ratio(before_slash, after_slash), x, y,
    )
    if not slash_changed:
        _clear_focused_composer()
        raise RuntimeError("Discord не показал /play в нижней области чата; slash-команда не выбиралась")

    # This is the exact old v3.0 behaviour: first autocomplete result for /play.
    # No expensive Chromium UIA traversal is attempted on the fallback branch.
    _discord_press("enter")
    time.sleep(0.35)
    after_select = _discord_visual_snapshot(bottom=True)
    select_changed = _visual_changed(after_slash, after_select, min_delta=0.06, min_ratio=0.0020)
    logger.info(
        "Discord visual slash fallback: selected first /play delta=%.3f ratio=%.4f",
        _visual_delta(after_slash, after_select), _visual_change_ratio(after_slash, after_select),
    )
    if not select_changed:
        _clear_focused_composer()
        raise RuntimeError("Discord не подтвердил выбор /play: интерфейс после Enter не изменился")

    before_argument = after_select
    _paste(argument)
    time.sleep(0.30)
    after_argument = _discord_visual_snapshot(bottom=True)
    argument_changed = _visual_changed(before_argument, after_argument, min_delta=0.035, min_ratio=0.0012)
    logger.info(
        "Discord visual slash fallback: query typed delta=%.3f ratio=%.4f",
        _visual_delta(before_argument, after_argument), _visual_change_ratio(before_argument, after_argument),
    )
    if not argument_changed:
        _clear_focused_composer()
        raise RuntimeError("Discord не показал введённый query; музыкальный запрос не отправлялся")

    if not _foreground_is_discord():
        raise RuntimeError("Discord потерял foreground перед финальным Enter")
    _discord_press("enter")
    time.sleep(0.70)
    after_submit = _discord_visual_snapshot(bottom=True)
    submit_changed = _visual_changed(after_argument, after_submit, min_delta=0.06, min_ratio=0.0020)
    logger.info(
        "Discord visual slash fallback: submitted delta=%.3f ratio=%.4f",
        _visual_delta(after_argument, after_submit), _visual_change_ratio(after_argument, after_submit),
    )
    if not submit_changed:
        raise RuntimeError(
            "Discord не подтвердил отправку /play query: после финального Enter интерфейс не изменился. "
            "Успех не засчитывается."
        )
    return {
        "verified": True, "submitted": True,
        "selection_method": "v3.0_first_play_enter",
        "typed_visible": True,
        "verification": "discord_visual_transaction",
        "composer_point": (x, y),
    }


def _channel_query_candidates(channel: str) -> list[str]:
    raw = str(channel or "").strip().lstrip("#").strip()
    cleaned = _n(raw).lstrip("#").strip()
    # Keep the historical literal -> semantic order for generic callers. Music
    # automation deliberately selects the semantic candidate below because channel
    # labels often start with decorative emoji.
    out: list[str] = []
    for value in (raw, cleaned):
        if value and value not in out:
            out.append(value)
    return out


def _post_navigation_channel_verified(window: Any, channel: str, server: str = "") -> bool:
    """Verify an explicit channel navigation without requiring fragile UIA selection.

    After Ctrl+K → #channel → Enter, the presence of a message composer plus the
    channel name somewhere in the client is a strong enough witness. The stricter
    current-channel matcher is still preferred for the no-navigation fast path.
    """
    if _current_text_channel_matches(window, channel, server):
        return True
    editor = _message_editor(window)
    if editor is None or not _has_text(window, channel):
        return False
    if not server:
        return True
    # This function is used *after* an explicit server -> #channel Quick Switch
    # sequence. v3.4 made this stricter than the proven v3.0/v3.1 route by
    # requiring Discord to expose Selected/header state for the server. Electron
    # does not always expose that state. Keep the strict matcher first, then allow
    # the visible guild name as a post-navigation witness, matching the old flow.
    return _current_server_matches(window, server) or _has_text(window, server)


def _message_editor(window: Any) -> Any | None:
    candidates: list[tuple[int, Any]] = []
    for control in _descendants(window):
        try:
            ctype = str(getattr(control.element_info, "control_type", "") or "")
        except Exception:
            ctype = ""
        if ctype not in {"Edit", "Document"}:
            continue
        desc = _descriptor(control)
        score = 0
        if any(x in desc for x in ("сообщение", "message", "написать", "send a message")):
            score += 10
        if any(x in desc for x in ("поиск", "search", "quick switch")):
            score -= 20
        if score > 0:
            candidates.append((score, control))
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1] if candidates else None


def _control_selected(control: Any) -> bool:
    """Best-effort UIA selected-state check without trusting sidebar text alone."""
    try:
        fn = getattr(control, "is_selected", None)
        if callable(fn) and bool(fn()):
            return True
    except Exception:
        pass
    try:
        pattern = getattr(control, "iface_selection_item", None)
        if pattern is not None and bool(pattern.CurrentIsSelected):
            return True
    except Exception:
        pass
    try:
        # Some Electron/Chromium UIA wrappers expose selection in legacy props.
        props = getattr(getattr(control, "element_info", None), "legacy_properties", {}) or {}
        for key in ("selected", "is_selected", "SelectionItem.IsSelected"):
            if bool(props.get(key)):
                return True
    except Exception:
        pass
    return False


def _current_server_matches(window: Any, server: str) -> bool:
    target = _n(server)
    if not target:
        return True

    # Window title/name often contains the active guild.  This is stronger than
    # merely seeing the server name somewhere in Discord's left sidebar.
    if _token_present(_descriptor(window), target):
        return True

    for control in _descendants(window):
        if not _token_present(_descriptor(control), target):
            continue
        if _control_selected(control):
            return True
    return False


def _current_text_channel_matches(window: Any, channel: str, server: str = "") -> bool:
    target = _n(channel).lstrip("#").strip()
    if not target:
        return False

    # Strong witness 1: the message composer names the active channel. This is
    # the cleanest signal when Chromium exposes “Message #channel”.
    editor = _message_editor(window)
    if editor is not None and _token_present(_descriptor(editor), target):
        return _current_server_matches(window, server)

    # Strong witness 2: Discord marks the active channel as selected in UIA.
    # This works on builds where the composer is simply exposed as “Message”.
    matching = [c for c in _descendants(window) if _token_present(_descriptor(c), target)]
    if any(_control_selected(control) for control in matching):
        return _current_server_matches(window, server)

    # Strong witness 3: the active channel is repeated in Discord's top header.
    # Do not assume a fixed horizontal sidebar width: Discord can collapse the
    # channel rail and then the header starts near x=60 (exactly the layout seen
    # in the user's client). A same-name sidebar row is lower in the window, so
    # a tight top-band check remains a strong witness without rejecting compact
    # layouts.
    try:
        wr = window.rectangle() if callable(getattr(window, "rectangle", None)) else window.element_info.rectangle
        h = max(1, int(wr.bottom) - int(wr.top))
        header_bottom = int(wr.top) + min(150, max(75, int(h * 0.14)))
        for control in matching:
            try:
                rect = control.rectangle() if callable(getattr(control, "rectangle", None)) else control.element_info.rectangle
                ctype = _control_type(control)
                if int(rect.top) <= header_bottom and ctype in {"Text", "Heading", "Button", "Hyperlink", "ListItem"}:
                    return _current_server_matches(window, server)
            except Exception:
                continue
    except Exception:
        pass
    return False


def _focus_message_editor(window: Any) -> Any | None:
    editor = _message_editor(window)
    if editor is not None:
        try:
            editor.set_focus()
            return editor
        except Exception:
            try:
                editor.click_input()
                return editor
            except Exception:
                pass
    # Official Discord keyboard navigation supports Tab to focus the text area.
    import pyautogui
    pyautogui.press("esc")
    time.sleep(0.08)
    for _ in range(8):
        pyautogui.press("tab")
        time.sleep(0.05)
        editor = _message_editor(window)
        if editor is not None:
            try:
                if editor.has_keyboard_focus():
                    return editor
            except Exception:
                pass
    return _message_editor(window)


def _editor_value(editor: Any | None) -> str:
    if editor is None:
        return ""
    try:
        pattern = editor.iface_value
        return str(pattern.CurrentValue or "")
    except Exception:
        pass
    try:
        return str(editor.window_text() or "")
    except Exception:
        return ""


def _clear_editor(editor: Any | None) -> None:
    if editor is None:
        return
    import pyautogui
    try:
        editor.set_focus()
    except Exception:
        try:
            editor.click_input()
        except Exception:
            pass
    pyautogui.hotkey("ctrl", "a")
    pyautogui.press("backspace")
    time.sleep(0.08)


def _control_type(control: Any) -> str:
    try:
        return str(getattr(control.element_info, "control_type", "") or "")
    except Exception:
        return ""


def _interaction_chain(control: Any, max_parents: int = 4) -> list[Any]:
    result: list[Any] = []
    seen: set[int] = set()
    current = control
    for _ in range(max(1, int(max_parents)) + 1):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        result.append(current)
        try:
            parent = getattr(current, "parent", None)
            current = parent() if callable(parent) else None
        except Exception:
            break
    return result


def _token_present(text: str, token: str) -> bool:
    token = _n(token).strip("/ ")
    if not token:
        return False
    return bool(re.search(rf"(?<![a-zа-я0-9_-]){re.escape(token)}(?![a-zа-я0-9_-])", _n(text)))


def _slash_option_controls(window: Any, option_name: str) -> list[Any]:
    """Find the selected command's option state without trusting picker rows.

    Discord/Electron differs between releases: after selecting /play the required
    option may be an Edit/Document, or only a Text/Button pill near the composer.
    Picker suggestions are ListItem rows containing `/play query`; those are
    explicitly rejected by walking ancestors.
    """
    option = _n(option_name)
    if not option:
        return []
    controls = _descendants(window)
    editor = _message_editor(window)
    try:
        er = editor.rectangle() if editor is not None and callable(getattr(editor, "rectangle", None)) else editor.element_info.rectangle if editor is not None else None
    except Exception:
        er = None

    result: list[tuple[int, Any]] = []
    for control in controls:
        desc = _descriptor(control)
        if not _token_present(desc, option):
            continue
        if any(x in desc for x in ("поиск", "search", "quick switch")):
            continue
        ctype = _control_type(control)
        chain = _interaction_chain(control, max_parents=5)
        # A query label underneath a command-picker ListItem is still only a
        # suggestion. Do not use it as proof that /play was selected.
        picker_ancestor = False
        for ancestor in chain[1:]:
            if _control_type(ancestor) == "ListItem":
                ad = _descriptor(ancestor)
                if _token_present(ad, "play") or _token_present(ad, "playlist"):
                    picker_ancestor = True
                    break
        if picker_ancestor:
            continue

        score = 0
        if ctype in {"Edit", "Document"}:
            score = 30
        elif ctype in {"Text", "Button"}:
            # Option pills/labels are accepted only close to the composer.
            if er is not None:
                try:
                    cr = control.rectangle() if callable(getattr(control, "rectangle", None)) else control.element_info.rectangle
                    if int(cr.top) >= int(er.top) - 180 and int(cr.bottom) <= int(er.bottom) + 100:
                        score = 18 if ctype == "Button" else 14
                except Exception:
                    pass
            elif any(_control_type(item) in {"Edit", "Document", "Group", "Pane"} for item in chain[1:3]):
                score = 10
        if score:
            result.append((score, control))
    result.sort(key=lambda item: item[0], reverse=True)
    return [control for _, control in result]

def _slash_option_present(window: Any, option_name: str) -> bool:
    return bool(_slash_option_controls(window, option_name))


def _wait_for_slash_option(window: Any, option_name: str, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + max(0.4, float(timeout))
    while time.monotonic() < deadline:
        if _slash_option_present(window, option_name):
            return True
        time.sleep(0.08)
    return False


def _control_rect(control: Any) -> Any | None:
    try:
        return control.rectangle() if callable(getattr(control, "rectangle", None)) else control.element_info.rectangle
    except Exception:
        return None


def _click_control_center(control: Any) -> bool:
    """Click the concrete UIA node rectangle, never an over-broad parent pane."""
    rect = _control_rect(control)
    if rect is None:
        return False
    try:
        left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        try:
            left, top, right, bottom = map(int, rect)
        except Exception:
            return False
    if right <= left or bottom <= top:
        return False
    try:
        import pyautogui
        click = getattr(pyautogui, "click", None)
        if callable(click):
            click((left + right) // 2, (top + bottom) // 2)
            return True
    except Exception:
        pass
    # Test/accessibility fallback: invoke only the concrete node itself. Never
    # walk up to Chromium Pane/Group parents because that can land on /playlist.
    for method in ("click_input", "invoke", "click"):
        fn = getattr(control, method, None)
        if callable(fn):
            try:
                fn()
                return True
            except Exception:
                continue
    return False


def _slash_control_fingerprint(control: Any) -> tuple[str, int, int, int, int, str] | None:
    rect = _control_rect(control)
    if rect is None:
        return None
    try:
        return (
            _control_type(control),
            int(rect.left), int(rect.top), int(rect.right), int(rect.bottom),
            _descriptor(control),
        )
    except Exception:
        return None


def _slash_history_fingerprints(window: Any, slash: str, option_name: str = "") -> set[tuple[str, int, int, int, int, str]]:
    """Snapshot slash-looking rows *before* typing the command.

    Discord's virtualized chat history can contain an old message such as
    ``/play query nickiminaj``.  v3.6 mistook exactly such a historical message
    for the live command picker and clicked the message row.  A real picker row is
    created only after `/play` is typed, so subtracting the pre-typing snapshot is
    a much stronger discriminator than text/geometry alone.
    """
    command = _n(slash.lstrip("/"))
    option = _n(option_name)
    out: set[tuple[str, int, int, int, int, str]] = set()
    for control in _descendants(window):
        desc = _descriptor(control)
        if not desc or not _token_present(desc, command):
            continue
        if command == "play" and _token_present(desc, "playlist"):
            continue
        if option and not _token_present(desc, option) and _control_type(control) not in {"ListItem", "Button", "Text", "Hyperlink"}:
            continue
        fp = _slash_control_fingerprint(control)
        if fp is not None:
            out.add(fp)
    return out


def _looks_like_chat_history_row(desc: str) -> bool:
    """Reject Discord message-history accessibility containers from slash picker."""
    value = _n(desc)
    markers = (
        "chat-messages-", "добавить реакцию", "add reaction", "нажмите чтобы отреагировать",
        "press to react", "изменить переслать", "edit forward", "ответить переслать",
    )
    return any(marker in value for marker in markers)


def _slash_picker_rows(
    window: Any,
    slash: str,
    option_name: str = "",
    *,
    exclude_fingerprints: set[tuple[str, int, int, int, int, str]] | None = None,
) -> list[Any]:
    """Return exact `/play` suggestion nodes visible above the composer.

    The important invariant is that `/playlist` is never a match for `/play` and
    that the clickable node is the concrete row/text rectangle, not one of its
    Chromium parents.  Clicking a parent container was the cause of v3.5 selecting
    `/playlist create`/`list-tracks` even though `/play query` was ranked first.
    """
    command = _n(slash.lstrip("/"))
    option = _n(option_name)
    editor = _message_editor(window)
    er = _control_rect(editor) if editor is not None else None
    out: list[tuple[int, int, int, Any]] = []
    seen: set[tuple[int, int, int, int, str]] = set()

    for control in _descendants(window):
        desc = _descriptor(control)
        if not desc or not _token_present(desc, command):
            continue
        if _looks_like_chat_history_row(desc):
            continue
        # Hard exclusion: the requested command token must be standalone.
        if _token_present(desc, "playlist") and command == "play":
            continue
        ctype = _control_type(control)
        if ctype not in {"ListItem", "Button", "Text", "Hyperlink", "Group"}:
            continue
        rect = _control_rect(control)
        if rect is None:
            continue
        try:
            left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        except Exception:
            continue
        width, height = max(1, right-left), max(1, bottom-top)
        if height > 150 or width < 20:
            continue
        fp = _slash_control_fingerprint(control)
        if exclude_fingerprints and fp is not None and fp in exclude_fingerprints:
            continue
        if er is not None:
            try:
                # Suggestions must be above the message composer, not the selected
                # slash-command pill that appears inside it after selection.
                if bottom > int(er.top) - 6 or bottom < int(er.top) - 760:
                    continue
            except Exception:
                pass
        key = (left, top, right, bottom, desc)
        if key in seen:
            continue
        seen.add(key)
        score = 0
        if option and _token_present(desc, option):
            score += 80
        if desc == command or desc.startswith(command + " "):
            score += 40
        if ctype == "ListItem":
            score += 12
        elif ctype == "Button":
            score += 8
        elif ctype == "Text":
            score += 5
        elif ctype == "Group" and option and _token_present(desc, option):
            # Newer Discord/Electron builds sometimes expose the autocomplete
            # row only as a compact Group. Accept it only when it names both the
            # exact command and configured option; broad parent panes remain banned.
            score += 4
        elif ctype == "Group":
            continue
        out.append((-score, top, left, control))
    out.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in out]


def _slash_command_pill_present(window: Any, slash: str) -> bool:
    """Detect the selected slash command near the composer after picker closes."""
    command = _n(slash.lstrip("/"))
    editor = _message_editor(window)
    er = _control_rect(editor) if editor is not None else None
    if er is None:
        return False
    for control in _descendants(window):
        if control is editor:
            continue
        ctype = _control_type(control)
        if ctype not in {"Text", "Button", "Hyperlink", "Group", "Pane"}:
            continue
        desc = _descriptor(control)
        if not _token_present(desc, command) or (command == "play" and _token_present(desc, "playlist")):
            continue
        rect = _control_rect(control)
        if rect is None:
            continue
        try:
            # Selected command/option pills live in or immediately above the
            # composer. Suggestions have already been filtered to the area above.
            if int(rect.top) >= int(er.top) - 120 and int(rect.bottom) <= int(er.bottom) + 80:
                return True
        except Exception:
            continue
    return False


def _slash_selection_landed(
    window: Any,
    slash: str,
    option_name: str,
    *,
    had_picker: bool = True,
    raw_before: str = "",
    exact_candidate_clicked: bool = False,
    exclude_fingerprints: set[tuple[str, int, int, int, int, str]] | None = None,
) -> bool:
    """Verify that Discord really entered application-command mode.

    Picker disappearance by itself is not enough: it can also mean a click landed
    outside the suggestion.  Require an exposed query option, a `/play` command pill,
    or a composer state transition away from the raw `/play` text.
    """
    if _slash_option_present(window, option_name):
        return True
    if _slash_command_pill_present(window, slash):
        return True
    if had_picker and not _slash_picker_rows(
        window, slash, option_name, exclude_fingerprints=exclude_fingerprints
    ):
        # Once a geometrically bounded *exact* `/play query` node was clicked,
        # the picker closing is itself a reliable transition even if Discord's
        # Electron build hides the command pill/query editor from UIA.
        if exact_candidate_clicked:
            return True
        editor = _message_editor(window)
        value = _editor_value(editor).strip() if editor is not None else ""
        before = str(raw_before or "").strip()
        if before and value != before and not value.startswith(before):
            return True
        # In some builds selecting a slash command clears raw text and leaves the
        # required option keyboard-focused while UIA still reports no option node.
        if before and not value:
            return True
    return False


def _slash_command_candidates(
    window: Any,
    slash: str,
    bot_name: str = "",
    option_name: str = "",
    *,
    exclude_fingerprints: set[tuple[str, int, int, int, int, str]] | None = None,
) -> list[Any]:
    """Rank exact slash picker rows; query-bearing rows always outrank bare play."""
    rows = _slash_picker_rows(window, slash, option_name, exclude_fingerprints=exclude_fingerprints)
    bot = _n(bot_name)
    option = _n(option_name)
    ranked: list[tuple[int, int, Any]] = []
    for control in rows:
        desc = _descriptor(control)
        score = 0
        if option and _token_present(desc, option):
            score += 100
        if bot and bot in desc:
            score += 25
        if desc.startswith(_n(slash.lstrip("/")) + " "):
            score += 10
        rect = _control_rect(control)
        top = int(rect.top) if rect is not None else 1_000_000
        ranked.append((-score, top, control))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked]


def _select_slash_command_v33(window: Any, slash: str, bot_name: str, option_name: str) -> str:
    """Exact v3.1-v3.3 slash selection contract used by music playback.

    The song argument is not allowed to be typed until the `query` option is
    actually exposed by Discord. No keyboard-only synthetic success is accepted.
    """
    import pyautogui

    deadline = time.monotonic() + 2.2
    while time.monotonic() < deadline:
        candidates = _slash_command_candidates(window, slash, bot_name, option_name)
        if candidates:
            for control in candidates[:5]:
                for target in _interaction_chain(control):
                    for method in ("invoke", "click_input", "click"):
                        fn = getattr(target, method, None)
                        if not callable(fn):
                            continue
                        try:
                            fn()
                        except Exception:
                            continue
                        if _wait_for_slash_option(window, option_name, timeout=0.75):
                            return f"uia_command_picker_{method}"
        time.sleep(0.08)

    pyautogui.press("enter")
    if _wait_for_slash_option(window, option_name, timeout=2.0):
        return "keyboard_enter"

    editor = _focus_message_editor(window)
    _clear_editor(editor)
    _paste(slash)
    time.sleep(0.55)
    pyautogui.press("tab")
    time.sleep(0.12)
    pyautogui.press("enter")
    if _wait_for_slash_option(window, option_name, timeout=2.0):
        return "keyboard_tab_enter"

    editor = _focus_message_editor(window)
    _clear_editor(editor)
    raise RuntimeError(
        f"Discord не подтвердил выбор slash-команды {slash}: параметр «{option_name}» не появился. "
        "Название песни не вводилось, чтобы не отправить неверную обычную строку."
    )


def _select_slash_command(
    window: Any,
    slash: str,
    bot_name: str,
    option_name: str,
    *,
    baseline_fingerprints: set[tuple[str, int, int, int, int, str]] | None = None,
) -> str:
    """Select the exact `/play query` row with deterministic geometry first."""
    import pyautogui

    # UIA gives us the actual rectangle of the first `/play query` suggestion on
    # the user's Discord build.  Click *that rectangle only*; never invoke ancestors.
    deadline = time.monotonic() + 2.6
    while time.monotonic() < deadline:
        candidates = _slash_command_candidates(
            window, slash, bot_name, option_name,
            exclude_fingerprints=baseline_fingerprints,
        )
        if candidates:
            control = candidates[0]
            had_picker = bool(_slash_picker_rows(
                window, slash, option_name,
                exclude_fingerprints=baseline_fingerprints,
            ))
            editor = _message_editor(window)
            raw_before = _editor_value(editor).strip() if editor is not None else slash
            logger.info(
                "Discord slash candidate: %r type=%s rect=%s",
                _descriptor(control), _control_type(control), _control_rect(control),
            )
            if _click_control_center(control):
                time.sleep(0.30)
                if _slash_selection_landed(
                    window, slash, option_name,
                    had_picker=had_picker, raw_before=raw_before or slash,
                    exact_candidate_clicked=True,
                    exclude_fingerprints=baseline_fingerprints,
                ):
                    return "uia_command_picker_exact_geometry"
                logger.warning("Discord exact slash row click did not enter option mode; retrying keyboard state machine")
            break
        time.sleep(0.08)

    # `/playlist` remains excluded by _slash_picker_rows/_slash_command_candidates.
    # Historical v3.9.2 retry list was: attempts = ("home_enter", "enter", "tab_enter").
    # v3.9.9 intentionally removes those destructive retype retries.
    # Keyboard fallback: IMPORTANT — `/play` is already present in the composer.
    # v3.9.8 cleared/retyped it before the fallback. On the user's current Discord
    # build Ctrl+A/Backspace does not reliably clear an active slash-autocomplete
    # editor, so the retry turned `/play` into `/pla/play`. Do not mutate/retype the
    # command here. Reuse the exact text that triggered autocomplete and perform
    # the one keyboard selection that is proven on this client: Home -> Enter.
    editor = _focus_message_editor(window)
    if editor is not None:
        raw_before = _editor_value(editor).strip() or slash
        had_picker = bool(_slash_picker_rows(
            window, slash, option_name,
            exclude_fingerprints=baseline_fingerprints,
        ))
        pyautogui.press("home")
        time.sleep(0.10)
        pyautogui.press("enter")
        time.sleep(0.30)
        if _slash_selection_landed(
            window, slash, option_name,
            had_picker=had_picker, raw_before=raw_before,
            exclude_fingerprints=baseline_fingerprints,
        ):
            return "keyboard_home_enter_existing_slash"

    # Never perform a destructive retry that can concatenate `/play` with partially
    # cleared text. Fail cleanly instead of corrupting the command or sending a raw
    # chat message.
    editor = _focus_message_editor(window)
    _clear_editor(editor)
    raise RuntimeError(
        f"Discord не смог выбрать slash-команду {slash}. Строка очищена; команда не отправлена."
    )


def _focus_slash_option(window: Any, option_name: str) -> Any | None:
    controls = _slash_option_controls(window, option_name)
    for control in controls:
        try:
            ctype = str(getattr(control.element_info, "control_type", "") or "")
        except Exception:
            ctype = ""
        if ctype not in {"Edit", "Document"}:
            continue
        try:
            control.set_focus()
            return control
        except Exception:
            try:
                control.click_input()
                return control
            except Exception:
                continue
    # After command selection Discord normally leaves keyboard focus in the first
    # required option.  Returning None intentionally means "do not steal focus".
    return None


class DiscordAdapter:
    def __init__(self, config: Any):
        self.config = config

    def _enabled(self) -> bool:
        return bool(self.config.get("integrations.discord.enabled", True))

    def _verify(self) -> bool:
        return bool(self.config.get("integrations.discord.verify_actions", True))

    def profiles(self) -> list[dict[str, str]]:
        raw = self.config.get("integrations.discord.servers", []) or []
        profiles: list[dict[str, str]] = []
        seen: set[str] = set()
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "") or "").strip()
                if not name:
                    continue
                key = _n(name)
                if not key or key in seen:
                    continue
                seen.add(key)
                profiles.append({
                    "name": name,
                    "voice_channel": str(item.get("voice_channel", "") or "").strip(),
                    "music_text_channel": str(item.get("music_text_channel", "") or "").strip(),
                    "music_bot_name": str(item.get("music_bot_name", "") or "").strip(),
                })
        if not profiles:
            legacy_name = str(self.config.get("integrations.discord.default_server", "") or "").strip()
            legacy_voice = str(self.config.get("integrations.discord.default_voice_channel", "") or "").strip()
            legacy_music = str(self.config.get("integrations.discord.music_text_channel", "") or "").strip()
            legacy_bot = str(self.config.get("integrations.discord.music_bot_name", "") or "").strip()
            if any((legacy_name, legacy_voice, legacy_music, legacy_bot)):
                profiles.append({
                    "name": legacy_name, "voice_channel": legacy_voice,
                    "music_text_channel": legacy_music, "music_bot_name": legacy_bot,
                })
        return profiles

    def resolve_profile(self, server: str = "") -> dict[str, str]:
        profiles = self.profiles()
        requested = str(server or "").strip()
        default_name = str(self.config.get("integrations.discord.default_server", "") or "").strip()
        if not requested:
            requested = default_name
        if not profiles:
            return {"name": requested, "voice_channel": "", "music_text_channel": "", "music_bot_name": ""}
        target = _n(requested)
        if target:
            for profile in profiles:
                if _n(profile["name"]) == target:
                    return dict(profile)
            ranked = sorted(
                ((SequenceMatcher(None, target, _n(profile["name"])).ratio(), profile) for profile in profiles),
                key=lambda item: item[0], reverse=True,
            )
            if ranked and ranked[0][0] >= 0.72:
                return dict(ranked[0][1])
            # A named but unknown server must not silently redirect to the default.
            return {"name": requested, "voice_channel": "", "music_text_channel": "", "music_bot_name": ""}
        for profile in profiles:
            if default_name and _n(profile["name"]) == _n(default_name):
                return dict(profile)
        return dict(profiles[0])

    def defaults(self, server: str = "") -> dict[str, str]:
        profiles = self.profiles()
        profile = self.resolve_profile(server)
        requested = str(server or "").strip()
        legacy_server = str(self.config.get("integrations.discord.default_server", "") or "").strip()
        profile_matched = bool(
            profiles and any(_n(item.get("name", "")) == _n(profile.get("name", "")) for item in profiles)
        )
        # Legacy flat channel fields are only a fallback for the default/legacy
        # profile. If the user explicitly names an unknown server, never borrow
        # the default server's channels and accidentally post in the wrong guild.
        allow_legacy_channels = not profiles or not requested or profile_matched
        legacy_music = str(self.config.get("integrations.discord.music_text_channel", "") or "").strip() if allow_legacy_channels else ""
        legacy_voice = str(self.config.get("integrations.discord.default_voice_channel", "") or "").strip() if allow_legacy_channels else ""
        return {
            "server": str(profile.get("name", "") or "").strip() or requested or legacy_server,
            "music_channel": str(profile.get("music_text_channel", "") or "").strip() or legacy_music,
            "voice_channel": str(profile.get("voice_channel", "") or "").strip() or legacy_voice,
            "bot_name": str(profile.get("music_bot_name", "") or "").strip() or str(self.config.get("integrations.discord.music_bot_name", "") or "").strip(),
            "bot_mode": str(self.config.get("integrations.discord.music_bot_mode", "slash") or "slash").strip().casefold(),
            "play_template": str(self.config.get("integrations.discord.music_play_template", "/play {query}") or "/play {query}").strip(),
            "query_option": str(self.config.get("integrations.discord.music_query_option", "query") or "query").strip(),
        }

    def open(self) -> dict[str, Any]:
        if not self._enabled():
            raise RuntimeError("Интеграция Discord отключена в настройках")
        data = launch_discord()
        focus_discord_window()
        return {**data, "app": "discord", "method": "native_client"}

    def open_server(self, server: str) -> dict[str, Any]:
        server = self.defaults(server)["server"]
        if not server:
            raise ValueError("Не указан сервер Discord и сервер по умолчанию не задан в настройках")
        _quick_switch("*", server)
        window = focus_discord_window()
        verified = _has_text(window, server)
        if self._verify() and not verified:
            raise RuntimeError(f"Discord не подтвердил переход на сервер «{server}»")
        return {"server": server, "verified": verified, "method": "discord_quick_switcher"}

    def open_text_channel(self, channel: str, server: str = "") -> dict[str, Any]:
        channel = str(channel or "").strip()
        resolved = self.defaults(server)
        server = str(resolved["server"] or server or "").strip()
        if not channel:
            raise ValueError("Не указан текстовый канал Discord")

        window = focus_discord_window()
        if _current_text_channel_matches(window, channel, server):
            return {
                "server": server, "channel": channel.lstrip("#"), "kind": "text",
                "verified": True, "method": "discord_already_in_channel",
            }

        if server:
            logger.info("Discord navigation v3.3 focus path: server=%r", server)
            _quick_switch("*", server)

        candidates = _channel_query_candidates(channel)
        search_channel = candidates[-1] if candidates else channel.lstrip("#").strip()
        logger.info("Discord navigation v3.3 focus path: text channel=%r", search_channel)
        _quick_switch("#", search_channel)

        window = focus_discord_window()
        verified = False
        try:
            verified = _post_navigation_channel_verified(window, search_channel, server)
        except Exception:
            verified = False
        # Do not repeat server/channel navigation if Electron hides the active
        # guild/channel from UIA. The music slash state machine below is the hard
        # proof that keyboard focus really landed in a usable bot channel.
        return {
            "server": server, "channel": channel.lstrip("#"), "kind": "text",
            "verified": verified, "method": "discord_v33_focused_quick_switcher",
            "query_used": search_channel,
        }

    def join_voice_channel(self, channel: str, server: str = "") -> dict[str, Any]:
        defaults = self.defaults(server)
        channel = str(channel or "").strip() or defaults["voice_channel"]
        server = str(defaults["server"] or server or "").strip()
        if not channel:
            raise ValueError("Не указан голосовой канал и канал по умолчанию не задан в настройках")
        if server:
            self.open_server(server)
        _quick_switch("!", channel.lstrip("!"))
        window = focus_discord_window()
        # A visible channel name alone is not sufficient proof that the user
        # actually joined voice: Quick Switcher can navigate/focus a result
        # while the connection still fails.  Require a strong connection
        # indicator exposed by Discord's accessibility tree instead.
        voice_controls = _find_controls(
            window,
            (
                "отключиться", "disconnect", "voice connected", "голосовая связь",
                "подключено", "connected to voice", "voice connection",
            ),
            control_types={"Button", "Text"},
        )
        verified = bool(voice_controls)
        if self._verify() and not verified:
            raise RuntimeError(f"Discord не подтвердил подключение к голосовому каналу «{channel}»")
        return {"server": server, "channel": channel.lstrip("!"), "kind": "voice", "verified": verified, "method": "discord_quick_switcher"}

    def leave_voice_channel(self) -> dict[str, Any]:
        window = focus_discord_window()
        controls = _find_controls(window, ("отключиться", "disconnect"), control_types={"Button"})
        for control in controls[:8]:
            if _invoke(control):
                time.sleep(0.30)
                still = bool(_find_controls(window, ("отключиться", "disconnect"), control_types={"Button"}))
                if not still or not self._verify():
                    return {"verified": not still, "method": "discord_uia_disconnect"}
        raise RuntimeError("Не нашёл доступную кнопку отключения от голосового канала Discord")

    def _set_toggle(self, desired: bool, kind: str) -> dict[str, Any]:
        window = focus_discord_window()
        import pyautogui
        if kind == "mute":
            enable_words = ("включить микрофон", "unmute")
            disable_words = ("выключить микрофон", "mute")
            shortcut = ("ctrl", "shift", "m")
        else:
            enable_words = ("включить звук", "undeafen")
            disable_words = ("отключить звук", "deafen")
            shortcut = ("ctrl", "shift", "d")
        currently_on = bool(_find_controls(window, enable_words, control_types={"Button"}))
        if currently_on == desired:
            return {"desired": desired, "already_in_requested_state": True, "verified": True, "kind": kind}
        pyautogui.hotkey(*shortcut)
        time.sleep(0.30)
        after = bool(_find_controls(window, enable_words, control_types={"Button"}))
        if self._verify() and after != desired:
            raise RuntimeError(f"Discord не подтвердил изменение состояния {kind}")
        return {"desired": desired, "verified": after == desired, "kind": kind, "method": "discord_hotkey"}

    def send_message(self, text: str, channel: str = "", server: str = "") -> dict[str, Any]:
        text = str(text or "").strip()
        if not text:
            raise ValueError("Текст сообщения пуст")
        if channel:
            self.open_text_channel(channel, server)
        elif server:
            self.open_server(server)
        window = focus_discord_window()
        editor = _focus_message_editor(window)
        if editor is None:
            raise RuntimeError("Не удалось надёжно найти поле сообщения Discord")
        try:
            editor.set_focus()
        except Exception:
            pass
        import pyautogui
        _paste(text)
        before = _editor_value(editor)
        pyautogui.press("enter")
        time.sleep(0.45)
        after = _editor_value(_message_editor(window))
        # Virtualized message lists are inconsistent between Discord/Electron
        # versions. A cleared composer is the least invasive confirmation that
        # Enter submitted instead of merely typing into the wrong control.
        composer_cleared = bool(before) and not after.strip()
        visible = _has_text(window, text[-80:]) if len(text) <= 160 else False
        verified = composer_cleared or visible
        if self._verify() and not verified:
            raise RuntimeError("Текст был введён в Discord, но отправку не удалось подтвердить; не буду сообщать ложный успех")
        return {"server": server, "channel": channel, "text": text, "verified": verified, "method": "discord_uia_message"}

    def music_bot_play(self, query: str, server: str = "", channel: str = "") -> dict[str, Any]:
        """Play music through Discord using the v3.1-v3.3 focused-window flow.

        v3.9.3 could report success after a keyboard-only transaction even when
        Discord never received the keystrokes. v3.9.4 removes that synthetic
        success path completely. A song is typed only after Discord confirms
        that `/play` became an application command exposing the configured
        `query` option, and success is returned only after that option state closes.
        """
        query = str(query or "").strip()
        if not query:
            raise ValueError("Не указана музыка для бота")

        defaults = self.defaults(server)
        effective_server = str(defaults["server"] or server or "").strip()
        channel = str(channel or "").strip() or str(defaults["music_channel"] or "").strip()
        if not channel:
            raise ValueError("Укажите музыкальный текстовый канал в настройках Discord или в голосовой команде")

        template = defaults["play_template"] or "/play {query}"
        try:
            command = template.format(query=query)
        except Exception:
            command = f"/play {query}"

        mode = defaults["bot_mode"]
        if mode != "slash" or not command.lstrip().startswith("/"):
            data = self.send_message(command, channel=channel, server=effective_server)
            return {**data, "query": query, "music_bot": defaults["bot_name"], "mode": "text"}

        stripped = command.strip()
        parts = stripped.split(maxsplit=1)
        slash = parts[0]
        argument = parts[1] if len(parts) > 1 else query
        option_name = defaults.get("query_option", "query") or "query"

        started = time.monotonic()
        logger.info(
            "Discord music v3.9.9 single-slash flow: start server=%r channel=%r query=%r",
            effective_server, channel, query,
        )

        navigation = self.open_text_channel(channel, effective_server)
        logger.info(
            "Discord music v3.9.9 single-slash flow: navigation method=%s verified=%s query_used=%r",
            navigation.get("method"), navigation.get("verified"), navigation.get("query_used", channel),
        )

        window = focus_discord_window()
        # Probe the accessibility tree once. If an editor exists, use the old
        # v3.3 focus helper; if not, do not spend eight Tab/UIA retries on a build
        # that has already demonstrated that Chromium hides the composer.
        editor = _focus_message_editor(window) if _message_editor(window) is not None else None
        if editor is None:
            logger.warning(
                "Discord UIA did not expose the message editor; using v3.0 keyboard flow with visual verification"
            )
            visual = _music_slash_visual_fallback(window, slash, argument, option_name)
            elapsed = time.monotonic() - started
            logger.info("Discord music v3.9.7 visual transaction: verified submission in %.2fs", elapsed)
            return {
                "server": effective_server, "channel": channel, "query": query,
                "music_bot": defaults["bot_name"], "command": slash, "option": option_name,
                "verified": True, "submitted": True,
                "method": "discord_visual_slash_transaction",
                "implementation": "v3.3_keyboard_flow_plus_visual_witness",
                "foreground_guard": "discord_process_pid_verified",
                "selection_method": visual.get("selection_method"),
                "typed_visible": visual.get("typed_visible", False), "mode": "slash",
                "navigation_method": navigation.get("method"),
                "navigation_verified": bool(navigation.get("verified")),
                "verification": visual.get("verification"),
                "elapsed_seconds": round(elapsed, 3),
            }

        _clear_editor(editor)
        if not _foreground_is_discord():
            focus_discord_window()
        if not _foreground_is_discord():
            raise RuntimeError("Discord потерял фокус перед /play; slash-команда не вводилась")
        logger.info("Discord music v3.9.9 single-slash flow: type slash=%r", slash)
        # Snapshot old chat-history `/play` rows BEFORE typing the live command.
        # v3.9.8 accidentally took this baseline after `_paste(slash)`, which could
        # subtract the freshly-created autocomplete row from the candidate set and
        # force the buggy retype fallback.
        baseline = _slash_history_fingerprints(window, slash, option_name)
        _paste(slash)
        time.sleep(0.55)

        selection_method = _select_slash_command(
            window, slash, defaults["bot_name"], option_name,
            baseline_fingerprints=baseline,
        )
        logger.info(
            "Discord music v3.9.9 single-slash flow: slash selected via %s, option=%r",
            selection_method, option_name,
        )

        option_editor = _focus_slash_option(window, option_name)
        _paste(argument)
        time.sleep(0.20)

        typed_visible = False
        if option_editor is not None:
            typed_visible = _n(argument) in _n(_editor_value(option_editor))
        if not typed_visible:
            typed_visible = _has_text(window, argument[:100])
        logger.info("Discord music v3.9.9 single-slash flow: query typed visible=%s", typed_visible)

        import pyautogui
        if not _foreground_is_discord():
            raise RuntimeError("Discord потерял фокус перед отправкой query; Enter не отправлялся")
        pyautogui.press("enter")
        deadline = time.monotonic() + 3.0
        verified = False
        while time.monotonic() < deadline:
            current = _message_editor(window)
            value = _editor_value(current).strip() if current is not None else ""
            option_still_open = _slash_option_present(window, option_name)
            raw_slash_remains = value.startswith(slash)
            argument_remains = bool(value and _n(argument) in _n(value))
            if not option_still_open and not raw_slash_remains and not argument_remains:
                verified = True
                break
            time.sleep(0.10)

        elapsed = time.monotonic() - started
        if not verified:
            _clear_editor(_focus_message_editor(window))
            raise RuntimeError(
                "Discord не подтвердил отправку /play query: интерфейс slash/query не закрылся. "
                "NEO не считает команду выполненной."
            )

        logger.info("Discord music v3.9.9 single-slash flow: verified submission in %.2fs", elapsed)
        return {
            "server": effective_server, "channel": channel, "query": query,
            "music_bot": defaults["bot_name"], "command": slash, "option": option_name,
            "verified": True, "submitted": True,
            "method": "discord_slash_option_state_machine",
            "implementation": "v3.3_focus_plus_verified_picker",
            "foreground_guard": "discord_process_pid_verified",
            "selection_method": selection_method, "typed_visible": typed_visible, "mode": "slash",
            "navigation_method": navigation.get("method"),
            "navigation_verified": bool(navigation.get("verified")),
            "elapsed_seconds": round(elapsed, 3),
        }

    def execute(self, action: str, **kwargs: Any) -> dict[str, Any]:
        if not self._enabled():
            raise RuntimeError("Интеграция Discord отключена в настройках")
        action = str(action or "").strip().casefold()
        if action == "open":
            return self.open()
        if action == "open_server":
            return self.open_server(str(kwargs.get("server", "") or ""))
        if action == "open_text_channel":
            return self.open_text_channel(str(kwargs.get("channel", "") or ""), str(kwargs.get("server", "") or ""))
        if action == "join_voice_channel":
            return self.join_voice_channel(str(kwargs.get("channel", "") or ""), str(kwargs.get("server", "") or ""))
        if action == "leave_voice_channel":
            return self.leave_voice_channel()
        if action == "send_message":
            return self.send_message(str(kwargs.get("text", "") or ""), str(kwargs.get("channel", "") or ""), str(kwargs.get("server", "") or ""))
        if action == "music_bot_play":
            return self.music_bot_play(str(kwargs.get("query", "") or ""), str(kwargs.get("server", "") or ""), str(kwargs.get("channel", "") or ""))
        if action == "mute":
            return self._set_toggle(True, "mute")
        if action == "unmute":
            return self._set_toggle(False, "mute")
        if action == "deafen":
            return self._set_toggle(True, "deafen")
        if action == "undeafen":
            return self._set_toggle(False, "deafen")
        raise ValueError(f"Неизвестная команда Discord: {action}")
