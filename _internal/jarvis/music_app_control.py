# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Strict application-only music control for NEO.

Music commands never inspect or control browser tabs.  The default target is the
Yandex Music desktop application.  Transport control uses Windows GSMTC, while
volume uses the application's own WASAPI audio session(s).
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from jarvis.audio.control_worker import audio_control_executor
from jarvis.audio.providers.windows_core_audio import WindowsCoreAudioOutputProvider

import psutil

logger = logging.getLogger(__name__)


def _hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt": return {}
    kwargs: dict[str, Any] = {}
    try: kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except Exception: pass
    try:
        si = subprocess.STARTUPINFO(); si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        si.wShowWindow = 0; kwargs["startupinfo"] = si
    except Exception: pass
    return kwargs

_BROWSER_MARKERS = (
    "firefox", "chrome", "msedge", "microsoftedge", "opera", "operagx",
    "browser.exe", "yandexbrowser", "brave", "vivaldi", "arc.exe",
)
_YANDEX_MARKERS = (
    "yandexmusic", "yandex.music", "yandex music", "яндекс музыка",
    "music.yandex", "ru.yandex.music",
)


def normalize(value: str) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", text).strip()


def canonical_music_app(value: str) -> str:
    text = normalize(value)
    if not text or text in {"music", "музыка", "музыку", "трек", "песня", "аудио"}:
        return "yandex_music"
    if ("yandex" in text or "яндекс" in text) and ("music" in text or "музык" in text):
        return "yandex_music"
    if text in {"ям", "я музыка", "яндекс"}:
        return "yandex_music"
    return text.replace(" ", "_")


def _is_browser_source(source: str) -> bool:
    s = normalize(source).replace(" ", "")
    return any(marker.replace(" ", "") in s for marker in _BROWSER_MARKERS)


def _looks_yandex(value: str) -> bool:
    compact = normalize(value).replace(" ", "")
    if any(marker.replace(" ", "") in compact for marker in _YANDEX_MARKERS):
        return True
    return ("yandex" in compact or "яндекс" in compact) and ("music" in compact or "музык" in compact)


def _run_coro(coro, timeout: float = 12.0):
    result: list[Any] = []
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=runner, name="jarvis-music-winrt", daemon=True)
    thread.start()
    thread.join(max(1.0, float(timeout)))
    if thread.is_alive():
        raise TimeoutError("Windows media API не ответил вовремя")
    if errors:
        raise errors[0]
    return result[0] if result else None


def _running_yandex_process_names() -> set[str]:
    result: set[str] = set()
    for proc in psutil.process_iter(["name", "exe", "cmdline"]):
        try:
            name = str(proc.info.get("name") or "").casefold()
            exe = str(proc.info.get("exe") or "").casefold()
            cmd = " ".join(str(x) for x in (proc.info.get("cmdline") or [])).casefold()
            hay = f"{name} {exe} {cmd}"
            # A browser may contain music.yandex.ru in its command line/title. It
            # must never make Firefox/Chrome look like the native music app.
            if _is_browser_source(f"{name} {exe}"):
                continue
            if _looks_yandex(hay):
                result.add(name)
                if exe:
                    result.add(Path(exe).name.casefold())
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return {x for x in result if x}


def _window_handle(window: Any) -> int:
    for value in (getattr(window, "handle", 0), getattr(getattr(window, "element_info", None), "handle", 0)):
        try:
            handle = int(value or 0)
            if handle:
                return handle
        except Exception:
            pass
    return 0


def _window_is_cloaked(window: Any) -> bool:
    """Return True for DWM-cloaked Electron helper windows.

    Electron applications can keep a nominally visible top-level UIA window while
    the real client is hidden. Treating that helper as the app window caused the
    old false "Яндекс Музыка запущена" success.
    """
    if os.name != "nt":
        return False
    handle = _window_handle(window)
    if not handle:
        return False
    try:
        import ctypes
        cloaked = ctypes.c_int(0)
        # DWMWA_CLOAKED = 14
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            handle, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        return hr == 0 and bool(cloaked.value)
    except Exception:
        return False


def _window_has_real_surface(window: Any) -> bool:
    try:
        visible = getattr(window, "is_visible", None)
        if callable(visible) and not visible():
            return False
    except Exception:
        pass
    if _window_is_cloaked(window):
        return False
    # Ignore tiny Electron helper/tool windows. If geometry is unavailable (for
    # example in a test double), process/title identity remains the fallback.
    try:
        rect = window.rectangle() if callable(getattr(window, "rectangle", None)) else window.element_info.rectangle
        width = max(0, int(rect.right) - int(rect.left))
        height = max(0, int(rect.bottom) - int(rect.top))
        minimized = False
        try:
            fn = getattr(window, "is_minimized", None)
            minimized = bool(fn()) if callable(fn) else False
        except Exception:
            pass
        if not minimized and (width < 260 or height < 140):
            return False
    except Exception:
        pass
    return True


def _activate_yandex_window(window: Any) -> bool:
    try:
        window.restore()
    except Exception:
        pass
    handle = _window_handle(window)
    if os.name == "nt" and handle:
        try:
            import ctypes
            ctypes.windll.user32.ShowWindow(handle, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(handle)
        except Exception:
            pass
    try:
        window.set_focus()
    except Exception:
        try:
            window.click_input()
        except Exception:
            pass
    time.sleep(0.05)
    return _window_has_real_surface(window)


def _yandex_windows() -> list[Any]:
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
            title = str(window.window_text() or "")
            pid = int(window.element_info.process_id)
            proc = psutil.Process(pid)
            proc_name = str(proc.name() or "")
            proc_exe = str(proc.exe() or "")
            if _is_browser_source(f"{proc_name} {proc_exe}"):
                continue
            proc_hay = " ".join([proc_name, proc_exe, title])
            if _looks_yandex(proc_hay) and _window_has_real_surface(window):
                result.append(window)
        except Exception:
            continue
    return result



def find_yandex_music_install() -> dict[str, str] | None:
    """Find the native Yandex Music app without opening any browser or app.

    Windows PowerShell 5.1 does not default to UTF-8 for redirected stdout, so
    the command explicitly requests UTF-8 before serialising Start Apps. This
    keeps Cyrillic app names intact on Russian Windows installations.
    """
    if os.name != "nt":
        return None
    # The current official desktop client is commonly installed per-user under
    # %LOCALAPPDATA%\Programs\YandexMusic. Prefer the exact executable before
    # StartApps because a stale shell registration can exist after app updates.
    local_root = Path(os.getenv("LOCALAPPDATA", "")) / "Programs" / "YandexMusic"
    for filename in ("Яндекс Музыка.exe", "Yandex Music.exe", "YandexMusic.exe"):
        candidate = local_root / filename
        if candidate.exists():
            return {"name": candidate.stem, "path": str(candidate), "method": "exe"}

    # The official desktop app registers the yandexmusic:// protocol.  Reading
    # that registration gives us another exact executable path even if the user
    # installed it somewhere non-standard.
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"yandexmusic\shell\open\command") as key:
            command, _ = winreg.QueryValueEx(key, None)
        command = str(command or "").strip()
        match = re.match(r'^"([^"]+\.exe)"', command, flags=re.I) or re.match(r'^([^ ]+\.exe)', command, flags=re.I)
        if match:
            candidate = Path(match.group(1))
            if candidate.exists() and _looks_yandex(candidate.name):
                return {"name": candidate.stem, "path": str(candidate), "method": "protocol_registry"}
    except Exception:
        logger.debug("Yandex Music protocol registry discovery failed", exc_info=True)

    script = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "$OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=False,
            **_hidden_subprocess_kwargs(),
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw = json.loads(proc.stdout.strip())
            if isinstance(raw, dict):
                raw = [raw]
            candidates: list[tuple[int, dict[str, str]]] = []
            for item in raw if isinstance(raw, list) else []:
                name = str(item.get("Name", "") or "").strip()
                app_id = str(item.get("AppID", "") or "").strip()
                combined = f"{name} {app_id}"
                if not app_id or _is_browser_source(combined) or not _looks_yandex(combined):
                    continue
                score = 10 if _looks_yandex(app_id) else 8
                candidates.append((score, {"name": name or "Yandex Music", "app_id": app_id, "method": "start_apps"}))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                return candidates[0][1]
    except Exception:
        logger.debug("Get-StartApps Yandex Music discovery failed", exc_info=True)

    roots = [
        Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.getenv("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path.home() / "Desktop",
    ]
    for root in roots:
        if not root.exists():
            continue
        try:
            for link in root.rglob("*.lnk"):
                if _looks_yandex(link.stem) and not _is_browser_source(link.stem):
                    return {"name": link.stem, "path": str(link), "method": "shortcut"}
        except OSError:
            continue
    return None

def launch_yandex_music(wait_seconds: float = 20.0) -> dict[str, Any]:
    """Launch/focus the native Yandex Music app and verify a real visible window.

    A background Electron process is *not* considered a successful launch.  The
    desktop app can remain resident without a visible window, so NEO activates
    the registered app/executable and waits until Windows UI Automation can see the
    actual Yandex Music window before reporting success.
    """
    if os.name != "nt":
        raise OSError("Яндекс Музыка как приложение поддерживается здесь только в Windows")

    visible = _yandex_windows()
    if visible:
        window = visible[0]
        if _activate_yandex_window(window):
            return {"started": False, "already_running": True, "window_verified": True}

    had_process = bool(_running_yandex_process_names())
    install = find_yandex_music_install()
    if not install:
        raise RuntimeError("Не нашёл установленное приложение Яндекс Музыка. Установите десктопное приложение и повторите команду.")

    if install.get("method") == "start_apps" and install.get("app_id"):
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{install['app_id']}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif install.get("path"):
        path = str(install["path"])
        if str(install.get("method", "")) == "exe":
            subprocess.Popen([path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    else:
        raise RuntimeError("Установка Яндекс Музыки найдена, но не удалось определить способ запуска")

    deadline = time.monotonic() + max(4.0, float(wait_seconds))
    while time.monotonic() < deadline:
        windows = _yandex_windows()
        if windows:
            window = windows[0]
            if _activate_yandex_window(window):
                return {
                    "started": not had_process,
                    "reactivated": had_process,
                    "already_running": had_process,
                    "window_verified": True,
                    **install,
                }
        time.sleep(0.25)

    raise RuntimeError(
        "Яндекс Музыка была запущена/активирована, но её окно не появилось. "
        "NEO не будет сообщать ложный успех."
    )


async def _describe_sessions() -> list[dict[str, Any]]:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager

    manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
    sessions = list(manager.get_sessions() or [])
    described: list[dict[str, Any]] = []
    for session in sessions:
        try:
            props = await session.try_get_media_properties_async()
            playback = session.get_playback_info()
            source = str(getattr(session, "source_app_user_model_id", "") or "")
            described.append({
                "session": session,
                "source": source,
                "title": str(getattr(props, "title", "") or ""),
                "artist": str(getattr(props, "artist", "") or ""),
                "album": str(getattr(props, "album_title", "") or ""),
                "status": str(getattr(getattr(playback, "playback_status", None), "name", getattr(playback, "playback_status", ""))),
                "browser": _is_browser_source(source),
            })
        except Exception as exc:
            logger.debug("Cannot describe media session: %s", exc)
    return described


def _choose_yandex_session(described: list[dict[str, Any]]) -> dict[str, Any]:
    app_processes = _running_yandex_process_names()
    scored: list[tuple[float, dict[str, Any]]] = []
    non_browser = [x for x in described if not x.get("browser")]
    for info in non_browser:
        source = str(info.get("source", "") or "")
        source_n = normalize(source)
        compact = source_n.replace(" ", "")
        score = 0.0
        if _looks_yandex(source):
            score = 10.0
        for proc_name in app_processes:
            stem = Path(proc_name).stem.casefold().replace(" ", "")
            if stem and stem in compact:
                score = max(score, 9.0)
        # Do not accept a generic/anonymous session merely because Yandex Music is
        # running. A browser or another Electron app can otherwise become the only
        # remaining non-browser session. Fail closed unless the session identity
        # itself matches Yandex Music or a verified Yandex process name.
        if score:
            scored.append((score, info))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        available = ", ".join(str(x.get("source") or x.get("title") or "?") for x in non_browser)
        raise RuntimeError(
            "Не нашёл отдельную медиа-сессию приложения Яндекс Музыка. "
            "Браузерные сессии намеренно не используются."
            + (f" Доступные приложения: {available[:240]}" if available else "")
        )
    if len(scored) > 1 and scored[1][0] == scored[0][0] and scored[0][0] < 9.0:
        raise RuntimeError("Нашёл несколько похожих музыкальных приложений и не буду угадывать")
    return scored[0][1]


async def _control_yandex_async(action: str, seconds: float = 10.0) -> dict[str, Any]:
    described = await _describe_sessions()
    info = _choose_yandex_session(described)
    session = info["session"]
    data = {k: v for k, v in info.items() if k != "session"}
    data["app"] = "yandex_music"
    data["domain"] = "music_app"

    if action == "status":
        try:
            timeline = session.get_timeline_properties()
            pos = getattr(timeline, "position", None)
            end = getattr(timeline, "end_time", None)
            if hasattr(pos, "total_seconds"):
                data["position_seconds"] = round(float(pos.total_seconds()), 2)
            if hasattr(end, "total_seconds"):
                data["duration_seconds"] = round(float(end.total_seconds()), 2)
        except Exception:
            pass
        return data

    if action in {"shuffle_on", "shuffle_off"}:
        desired = action == "shuffle_on"
        method = getattr(session, "try_change_shuffle_active_async", None)
        if not callable(method):
            raise RuntimeError("Windows media session Яндекс Музыки не поддерживает управление перемешиванием")
        ok = await method(desired)
        if ok is False:
            raise RuntimeError("Яндекс Музыка отклонила изменение перемешивания")
        data.update({"shuffle": desired, "control_success": True, "verified": True})
        return data

    if action in {"repeat_on", "repeat_off"}:
        method = getattr(session, "try_change_auto_repeat_mode_async", None)
        if not callable(method):
            raise RuntimeError("Windows media session Яндекс Музыки не поддерживает управление повтором")
        try:
            from winsdk.windows.media import MediaPlaybackAutoRepeatMode
            mode = MediaPlaybackAutoRepeatMode.LIST if action == "repeat_on" else MediaPlaybackAutoRepeatMode.NONE
        except Exception:
            # WinRT enum values are stable: None=0, Track=1, List=2.  Keep this
            # compatibility path for older winsdk builds that omit the enum export.
            mode = 2 if action == "repeat_on" else 0
        ok = await method(mode)
        if ok is False:
            raise RuntimeError("Яндекс Музыка отклонила изменение повтора")
        data.update({"repeat": action == "repeat_on", "control_success": True, "verified": True})
        return data

    if action in {"seek_forward", "seek_backward"}:
        timeline = session.get_timeline_properties()
        pos = getattr(timeline, "position", None)
        if not hasattr(pos, "total_seconds"):
            raise RuntimeError("Яндекс Музыка не сообщает текущую позицию трека")
        current = float(pos.total_seconds())
        delta = abs(float(seconds or 10.0)) * (1 if action == "seek_forward" else -1)
        target = max(0.0, current + delta)
        ok = await session.try_change_playback_position_async(int(target * 10_000_000))
        if not ok:
            raise RuntimeError("Яндекс Музыка отклонила перемотку")
        data["position_seconds"] = round(target, 2)
        data["control_success"] = True
        return data

    status_n = normalize(str(data.get("status", "")))
    if action == "play" and "playing" in status_n:
        data["already_in_requested_state"] = True
        data["control_success"] = True
        return data
    if action in {"pause", "stop"} and any(x in status_n for x in ("paused", "stopped", "closed")):
        data["already_in_requested_state"] = True
        data["control_success"] = True
        return data

    method_name = {
        "play": "try_play_async",
        "pause": "try_pause_async",
        "stop": "try_pause_async",  # conversational stop = pause for music
        "play_pause": "try_toggle_play_pause_async",
        "next": "try_skip_next_async",
        "previous": "try_skip_previous_async",
    }.get(action)
    if not method_name:
        raise ValueError(f"Неподдерживаемая команда музыки: {action}")
    before = (str(data.get("title", "")), str(data.get("artist", "")), status_n)
    ok = await getattr(session, method_name)()
    if not ok:
        raise RuntimeError(f"Яндекс Музыка отклонила команду {action}")
    data["control_success"] = True

    # Re-read state for a useful verified response. API success is still retained
    # because Next/Previous may keep the same track title briefly during buffering.
    await asyncio.sleep(0.16)
    try:
        props = await session.try_get_media_properties_async()
        playback = session.get_playback_info()
        after = (
            str(getattr(props, "title", "") or ""),
            str(getattr(props, "artist", "") or ""),
            normalize(str(getattr(getattr(playback, "playback_status", None), "name", getattr(playback, "playback_status", "")))),
        )
        data["title"] = after[0]
        data["artist"] = after[1]
        data["status"] = after[2]
        data["state_changed"] = after != before
    except Exception:
        pass
    return data


def control_music_app(action: str, app: str = "yandex_music", seconds: float = 10.0, *, auto_launch: bool = True) -> dict[str, Any]:
    app = canonical_music_app(app)
    if app != "yandex_music":
        raise RuntimeError(f"Музыкальное приложение «{app}» пока не настроено. Выберите Яндекс Музыку в настройках.")
    if auto_launch and not (_yandex_windows() or _running_yandex_process_names()):
        launch_yandex_music()
        # Give the app a moment to publish its media session. Do not block long.
        deadline = time.monotonic() + 3.5
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return _run_coro(_control_yandex_async(action, seconds), timeout=5.0)
            except Exception as exc:
                last_exc = exc
                time.sleep(0.20)
        if last_exc:
            raise last_exc
    return _run_coro(_control_yandex_async(action, seconds), timeout=8.0)


def music_app_status(app: str = "yandex_music") -> dict[str, Any]:
    return control_music_app("status", app, auto_launch=False)


def set_music_app_volume(percent: int, app: str = "yandex_music") -> dict[str, Any]:
    if os.name != "nt":
        raise OSError("Громкость приложения поддерживается только в Windows")
    app = canonical_music_app(app)
    if app != "yandex_music":
        raise RuntimeError(f"Музыкальное приложение «{app}» пока не поддерживается")
    requested = max(0, min(100, int(percent)))

    def _work() -> dict[str, Any]:
        # Keep every pycaw/comtypes wrapper inside the single long-lived Core
        # Audio apartment; callers only receive plain dict/list values.
        WindowsCoreAudioOutputProvider._imports()
        from pycaw.pycaw import AudioUtilities

        process_names = _running_yandex_process_names()
        level = requested / 100.0
        changed = 0
        matched: list[str] = []
        for session in AudioUtilities.GetAllSessions():
            try:
                proc = getattr(session, "Process", None)
                proc_name = str(proc.name() or "").casefold() if proc is not None else ""
                label = " ".join([
                    proc_name,
                    str(getattr(session, "DisplayName", "") or ""),
                    str(getattr(session, "Identifier", "") or ""),
                ])
                # A Firefox/Chrome audio session may expose the page title
                # "Яндекс Музыка". Process identity outranks display metadata.
                if _is_browser_source(proc_name):
                    continue
                match = proc_name in process_names or _looks_yandex(label)
                if not match:
                    continue
                volume = getattr(session, "SimpleAudioVolume", None)
                if volume is None:
                    continue
                volume.SetMasterVolume(level, None)
                changed += 1
                matched.append(proc_name or label[:80])
            except Exception:
                continue
        if not changed:
            raise RuntimeError("Не нашёл аудиосессию приложения Яндекс Музыка. Запустите воспроизведение хотя бы одного трека.")
        return {"app": "yandex_music", "percent": requested, "sessions_changed": changed, "matched": matched, "serialized": True}

    return dict(audio_control_executor().submit(_work, timeout=8.0))


def focus_yandex_music_window() -> Any | None:
    windows = _yandex_windows()
    if not windows:
        return None
    for window in windows:
        if _activate_yandex_window(window):
            return window
    return None


def _yandex_control_descriptor(control: Any) -> str:
    try:
        return normalize(" ".join([
            str(control.window_text() or ""),
            str(getattr(control.element_info, "name", "") or ""),
            str(getattr(control.element_info, "help_text", "") or ""),
            str(getattr(control.element_info, "automation_id", "") or ""),
        ]))
    except Exception:
        return ""


def _yandex_control_type(control: Any) -> str:
    try:
        return str(getattr(control.element_info, "control_type", "") or "")
    except Exception:
        return ""


def _yandex_descendants(window: Any) -> list[Any]:
    try:
        return list(window.descendants())[:2500]
    except Exception:
        return []


def _yandex_rect(control: Any) -> tuple[int, int, int, int] | None:
    try:
        rect = control.rectangle() if callable(getattr(control, "rectangle", None)) else control.element_info.rectangle
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        return None


def _yandex_invoke(control: Any) -> str:
    """Interact with one already-matched control and report the method used."""
    for method in ("invoke", "click_input", "click"):
        fn = getattr(control, method, None)
        if not callable(fn):
            continue
        try:
            fn()
            return method
        except Exception:
            continue
    rect = _yandex_rect(control)
    if rect is not None:
        try:
            import pyautogui
            left, top, right, bottom = rect
            if right - left >= 5 and bottom - top >= 5:
                pyautogui.click((left + right) // 2, (top + bottom) // 2)
                return "center_click"
        except Exception:
            pass
    return ""


def _find_yandex_search_entry(window: Any) -> Any | None:
    ranked: list[tuple[int, Any]] = []
    for control in _yandex_descendants(window):
        desc = _yandex_control_descriptor(control)
        if not desc or not ("поиск" in desc or "search" in desc):
            continue
        if any(x in desc for x in ("очистить поиск", "clear search", "история поиска")):
            continue
        ctype = _yandex_control_type(control)
        score = 0
        if ctype in {"Hyperlink", "Button"}:
            score += 30
        elif ctype in {"Text", "ListItem"}:
            score += 18
        elif ctype in {"Edit", "Document"}:
            score += 8
        if desc in {"поиск", "search", "по щелчку поиск"}:
            score += 30
        if "главное меню" in desc or "navigation" in desc:
            score += 8
        if score:
            ranked.append((score, control))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1] if ranked else None


def _find_yandex_search_editor(window: Any) -> Any | None:
    """Return the actual Search editor, not an arbitrary Electron Document."""
    candidates: list[tuple[int, Any]] = []
    try:
        wr = window.rectangle() if callable(getattr(window, "rectangle", None)) else window.element_info.rectangle
        window_top = int(wr.top)
        window_bottom = int(wr.bottom)
    except Exception:
        window_top, window_bottom = -10**9, 10**9

    for control in _yandex_descendants(window):
        ctype = _yandex_control_type(control)
        if ctype not in {"Edit", "Document"}:
            continue
        desc = _yandex_control_descriptor(control)
        if any(x in desc for x in ("сообщение", "message", "чат", "chat")):
            continue
        try:
            focused = bool(control.has_keyboard_focus())
        except Exception:
            focused = False
        search_named = any(x in desc for x in ("поиск", "search", "трек альбом исполнитель", "track album artist"))
        # Chromium often exposes the page itself as Document.  Accept Document
        # only if it is explicitly Search-related or currently owns the caret.
        if ctype == "Document" and not (search_named or focused):
            continue

        score = 35 if ctype == "Edit" else 8
        if search_named:
            score += 45
        if focused:
            score += 60
        rect = _yandex_rect(control)
        if rect is not None:
            _left, top, _right, bottom = rect
            h = max(1, window_bottom - window_top)
            if top <= window_top + int(h * 0.30):
                score += 18
            if bottom - top < 12:
                score -= 20
        candidates.append((score, control))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates else None


def _query_tokens(query: str) -> list[str]:
    return [token for token in normalize(query).split() if len(token) >= 2]


def _find_yandex_result_play(window: Any, query: str) -> tuple[Any | None, bool]:
    """Return a result-row Play button and whether it was tied to query text."""
    tokens = _query_tokens(query)
    controls = _yandex_descendants(window)
    title_rects: list[tuple[tuple[int, int, int, int], int]] = []
    for control in controls:
        desc = _yandex_control_descriptor(control)
        if not desc or not tokens:
            continue
        coverage = sum(token in desc for token in tokens)
        if coverage < 1:
            continue
        ctype = _yandex_control_type(control)
        if ctype not in {"Text", "Hyperlink", "ListItem", "Group", "Pane"}:
            continue
        rect = _yandex_rect(control)
        if rect is not None:
            title_rects.append((rect, coverage))

    try:
        wr = window.rectangle() if callable(getattr(window, "rectangle", None)) else window.element_info.rectangle
        wtop, wbottom = int(wr.top), int(wr.bottom)
        wh = max(1, wbottom - wtop)
    except Exception:
        wtop, wbottom, wh = -10**9, 10**9, 1

    ranked: list[tuple[int, Any, bool]] = []
    for control in controls:
        if _yandex_control_type(control) != "Button":
            continue
        desc = _yandex_control_descriptor(control)
        if not desc:
            continue
        is_play = any(x in desc for x in ("воспроизвед", "воспроизвести", "play"))
        if not is_play or any(x in desc for x in ("трейлер", "trailer", "моя волна", "my wave", "пауза", "pause")):
            continue
        rect = _yandex_rect(control)
        if rect is None:
            continue
        left, top, right, bottom = rect
        # Exclude the persistent bottom player and top navigation.
        if top >= wtop + int(wh * 0.78) or bottom <= wtop + int(wh * 0.12):
            continue
        direct = sum(token in desc for token in tokens) if tokens else 0
        tied = False
        score = 20 + direct * 18
        cy = (top + bottom) / 2.0
        for tr, coverage in title_rects:
            tl, tt, tright, tb = tr
            tcy = (tt + tb) / 2.0
            if abs(cy - tcy) <= 70 and left <= tright + 350 and right >= tl - 350:
                score += 50 + coverage * 10
                tied = True
                break
        ranked.append((score, control, tied or direct > 0))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        return None, False
    return ranked[0][1], ranked[0][2]


def _yandex_ui_query_visible(window: Any, query: str, *, bottom_only: bool = False) -> bool:
    tokens = _query_tokens(query)
    if not tokens:
        return False
    try:
        wr = window.rectangle() if callable(getattr(window, "rectangle", None)) else window.element_info.rectangle
        cutoff = int(wr.top) + int((int(wr.bottom) - int(wr.top)) * 0.72)
    except Exception:
        cutoff = -10**9
    for control in _yandex_descendants(window):
        desc = _yandex_control_descriptor(control)
        if not desc:
            continue
        if sum(token in desc for token in tokens) < max(1, min(2, len(tokens))):
            continue
        if bottom_only:
            rect = _yandex_rect(control)
            if rect is None or rect[1] < cutoff:
                continue
        return True
    return False



def _yandex_window_bounds(window: Any) -> tuple[int, int, int, int]:
    rect = window.rectangle() if callable(getattr(window, "rectangle", None)) else window.element_info.rectangle
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


def _click_yandex_search_field(window: Any, *, x_ratio: float = 0.36, settle: float = 0.35) -> None:
    """Focus the visible Search control inside the verified Yandex Music window.

    Current Electron builds use a two-stage interaction: the first click can open
    Search mode, and only after React/Chromium rerenders does the text editor
    become focusable.  v3.4 typed too early and therefore left the query empty.
    """
    import pyautogui
    left, top, right, bottom = _yandex_window_bounds(window)
    width, height = max(1, right-left), max(1, bottom-top)
    x_ratio = max(0.28, min(0.62, float(x_ratio)))
    pyautogui.click(int(left + width * x_ratio), int(top + height * 0.066))
    time.sleep(max(0.08, float(settle)))


def _focus_yandex_search_editor(window: Any) -> str:
    """Open Yandex Search and leave keyboard focus in its real editor."""
    editor = _find_yandex_search_editor(window)
    if editor is not None and _yandex_control_type(editor) == "Edit":
        try:
            editor.set_focus()
            return "uia_search_edit"
        except Exception:
            try:
                editor.click_input()
                return "uia_search_edit_click"
            except Exception:
                pass

    # Prefer the exact Search navigation control over a guessed coordinate.
    entry = _find_yandex_search_entry(window)
    if entry is not None and _yandex_control_type(entry) not in {"Edit", "Document"}:
        invoked = _yandex_invoke(entry)
        if invoked:
            time.sleep(0.30)
            editor = _find_yandex_search_editor(window)
            if editor is not None and _yandex_control_type(editor) == "Edit":
                try:
                    editor.set_focus()
                    return f"search_entry_{invoked}+uia_edit"
                except Exception:
                    try:
                        editor.click_input()
                        return f"search_entry_{invoked}+uia_edit_click"
                    except Exception:
                        pass
            rect = _yandex_rect(entry)
            if rect is not None:
                try:
                    import pyautogui
                    left, top, right, bottom = rect
                    pyautogui.click((left + right) // 2, (top + bottom) // 2)
                    time.sleep(0.16)
                    return f"search_entry_{invoked}+center_click"
                except Exception:
                    pass

    # Safe fallback is limited to the verified Yandex window's top Search band.
    _click_yandex_search_field(window, x_ratio=0.34, settle=0.42)
    editor = _find_yandex_search_editor(window)
    if editor is not None and _yandex_control_type(editor) == "Edit":
        try:
            editor.set_focus()
            return "native_search_open+uia_edit"
        except Exception:
            try:
                editor.click_input()
                return "native_search_open+uia_edit_click"
            except Exception:
                pass
    _click_yandex_search_field(window, x_ratio=0.42, settle=0.16)
    return "native_window_search_field_two_stage"


def _focused_text_via_clipboard() -> str:
    """Read the currently focused text field using ordinary Ctrl+A/C semantics."""
    import pyautogui
    import pyperclip
    marker = f"__jarvis_probe_{time.time_ns()}__"
    pyperclip.copy(marker)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.04)
    pyautogui.hotkey("ctrl", "c")
    time.sleep(0.08)
    value = str(pyperclip.paste() or "")
    return "" if value == marker else value


def _yandex_search_editor_contains_query(window: Any, query: str) -> bool:
    """Verify query text only from the Search editor itself."""
    wanted = normalize(query)
    if not wanted:
        return False
    editor = _find_yandex_search_editor(window)
    if editor is None:
        return False
    values: list[str] = []
    try:
        values.append(str(editor.window_text() or ""))
    except Exception:
        pass
    info = getattr(editor, "element_info", None)
    for attr in ("name", "help_text", "value"):
        try:
            values.append(str(getattr(info, attr, "") or ""))
        except Exception:
            pass
    try:
        iface = getattr(editor, "iface_value", None)
        values.append(str(getattr(iface, "CurrentValue", "") or ""))
    except Exception:
        pass
    for method_name in ("get_value", "legacy_properties"):
        fn = getattr(editor, method_name, None)
        if not callable(fn):
            continue
        try:
            value = fn()
            if isinstance(value, dict):
                values.extend(str(value.get(k, "") or "") for k in ("Value", "Name", "Description"))
            else:
                values.append(str(value or ""))
        except Exception:
            pass
    hay = normalize(" ".join(values))
    return wanted in hay


def _paste_yandex_search_query(window: Any, query: str) -> str:
    """Type Search text and prove it reached Search before reporting success.

    A result row that was already visible before typing is deliberately *not* a
    proof of input.  This prevents stale UI text from turning a failed paste into
    the false-success seen in v3.9.0.
    """
    import pyautogui
    import pyperclip

    previous = pyperclip.paste()
    methods: list[str] = []
    confirmed = False
    try:
        for attempt in range(4):
            if attempt == 0:
                method = _focus_yandex_search_editor(window)
            else:
                ratio = (0.36, 0.46, 0.54)[min(attempt - 1, 2)]
                _click_yandex_search_field(window, x_ratio=ratio, settle=0.20)
                method = f"native_window_search_field_retry_{attempt}"
            methods.append(method)

            results_visible_before = _yandex_ui_query_visible(window, query)
            pyperclip.copy(query)
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("backspace")
            if attempt % 2:
                pyautogui.hotkey("shift", "insert")
            else:
                pyautogui.hotkey("ctrl", "v")
            time.sleep(0.16)

            copied = _focused_text_via_clipboard()
            editor_verified = _yandex_search_editor_contains_query(window, query)
            if normalize(copied) == normalize(query) or editor_verified:
                confirmed = True
                pyautogui.press("enter")
                time.sleep(0.72)
                break

            # Some Electron builds hide the contenteditable value from UIA and
            # block Ctrl+C. Submit once, but only accept a newly-created result.
            pyautogui.press("enter")
            time.sleep(0.45)
            results_visible_after = _yandex_ui_query_visible(window, query)
            if results_visible_after and not results_visible_before:
                confirmed = True
                methods.append("new_search_result_verified")
                break

        if not confirmed:
            raise RuntimeError(
                "Строка поиска Яндекс Музыки открылась, но NEO не смог подтвердить, "
                "что название трека действительно введено в поле поиска."
            )
    finally:
        try:
            pyperclip.copy(previous)
        except Exception:
            pass
    return "+".join(methods) + "+input_verified"


def _find_yandex_result_row(window: Any, query: str) -> tuple[Any | None, bool]:
    """Find the first compact result row/title tied to the requested query."""
    tokens = _query_tokens(query)
    if not tokens:
        return None, False
    try:
        wl, wt, wr, wb = _yandex_window_bounds(window)
        wh, ww = max(1, wb-wt), max(1, wr-wl)
    except Exception:
        wl, wt, wh, ww = 0, 0, 1000, 1000
    ranked: list[tuple[int, int, Any, bool]] = []
    for control in _yandex_descendants(window):
        desc = _yandex_control_descriptor(control)
        if not desc:
            continue
        coverage = sum(token in desc for token in tokens)
        if coverage < 1:
            continue
        ctype = _yandex_control_type(control)
        if ctype not in {"Text", "Hyperlink", "ListItem", "Group", "Pane", "Button"}:
            continue
        rect = _yandex_rect(control)
        if rect is None:
            continue
        left, top, right, bottom = rect
        # Ignore search/top navigation and persistent bottom player.
        if bottom <= wt + int(wh * 0.13) or top >= wt + int(wh * 0.80):
            continue
        rw, rh = max(1, right-left), max(1, bottom-top)
        score = coverage * 28
        if ctype in {"ListItem", "Hyperlink", "Text"}:
            score += 18
        if 12 <= rh <= 110:
            score += 12
        if rw <= int(ww * 0.90):
            score += 6
        # Prefer the first result region over recommendations far below.
        score += max(0, 18 - int((top - (wt + wh*0.13)) / max(1, wh) * 30))
        # One query token is enough for a row witness because Yandex often
        # exposes artist and title as separate sibling text nodes. Final playback
        # verification still compares the combined media title/artist when GSMTC
        # is available.
        ranked.append((score, top, control, coverage >= 1))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked:
        return None, False
    return ranked[0][2], ranked[0][3]


def _activate_yandex_result(window: Any, query: str) -> tuple[str, bool]:
    """Start the requested result without relying on an exposed Play button."""
    play_control, tied = _find_yandex_result_play(window, query)
    if play_control is not None:
        used = _yandex_invoke(play_control)
        if used:
            return used, bool(tied)

    row, row_tied = _find_yandex_result_row(window, query)
    if row is None:
        return "", False
    # Track rows in the Electron client commonly start playback on double click.
    # Prefer wrapper methods; fall back to a physical double click *inside the
    # query-matched row*, never at a global hard-coded desktop coordinate.
    for method in ("double_click_input", "click_input", "invoke", "click"):
        fn = getattr(row, method, None)
        if not callable(fn):
            continue
        try:
            if method == "click_input":
                fn(double=True)
            else:
                fn()
            return f"result_row_{method}", bool(row_tied)
        except TypeError:
            try:
                fn()
                return f"result_row_{method}", bool(row_tied)
            except Exception:
                pass
        except Exception:
            pass
    rect = _yandex_rect(row)
    if rect is not None:
        try:
            import pyautogui
            left, top, right, bottom = rect
            x = int(left + max(8, min((right-left)*0.38, right-left-8)))
            y = int((top+bottom)/2)
            pyautogui.doubleClick(x, y, interval=0.08)
            return "result_row_double_click", bool(row_tied)
        except Exception:
            pass
    return "", False


def _find_yandex_nav_control(window: Any, names: tuple[str, ...]) -> Any | None:
    """Find one *leaf-sized* left-navigation item, never the whole nav container.

    v3.4 could match a parent accessibility node whose descriptor contained
    "Треки Альбомы Исполнители Плейлисты". Clicking its centre landed on
    "Плейлисты" even though the intended child was "Треки".
    """
    wanted = tuple(normalize(name) for name in names if normalize(name))
    try:
        wl, wt, wr, wb = _yandex_window_bounds(window)
        ww, wh = max(1, wr-wl), max(1, wb-wt)
    except Exception:
        wl, wt, ww, wh = 0, 0, 1200, 900

    sibling_words = ("альбом", "исполнител", "плейлист", "радио", "подкаст", "главн", "мои файлы")
    ranked: list[tuple[int, int, int, Any]] = []
    for control in _yandex_descendants(window):
        desc = _yandex_control_descriptor(control)
        if not desc:
            continue
        token_hit = any(re.search(rf"(?<![a-zа-я0-9]){re.escape(word)}(?![a-zа-я0-9])", desc) for word in wanted)
        if not token_hit:
            continue
        rect = _yandex_rect(control)
        if rect is None:
            continue
        left, top, right, bottom = rect
        width, height = max(1, right-left), max(1, bottom-top)
        # Navigation items live in the left rail and are short rows. This rejects
        # large Pane/Group parents that contain several neighbouring labels.
        if left > wl + int(ww * 0.30) or height > max(90, int(wh * 0.12)):
            continue
        ctype = _yandex_control_type(control)
        score = 0
        if desc in wanted:
            score += 80
        elif any(desc.startswith(word + " ") or desc.endswith(" " + word) for word in wanted):
            score += 45
        else:
            score += 25
        if ctype in {"Hyperlink", "ListItem", "Button", "Text"}:
            score += 20
        if any(word in desc for word in sibling_words) and not any(desc == word for word in wanted):
            score -= 45
        score -= min(20, width // 80)
        ranked.append((score, height, top, control))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return ranked[0][3] if ranked else None


def _yandex_tracks_page_visible(window: Any) -> bool:
    """Confirm that the main content pane, not merely the sidebar, says Tracks."""
    try:
        wl, wt, wr, wb = _yandex_window_bounds(window)
        ww, wh = max(1, wr-wl), max(1, wb-wt)
    except Exception:
        return False
    for control in _yandex_descendants(window):
        desc = _yandex_control_descriptor(control)
        if not desc or not re.search(r"(?<![a-zа-я0-9])(?:треки|tracks)(?![a-zа-я0-9])", desc):
            continue
        rect = _yandex_rect(control)
        if rect is None:
            continue
        left, top, right, bottom = rect
        if left >= wl + int(ww * 0.24) and top <= wt + int(wh * 0.35):
            return True
    return False


def _yandex_player_is_playing(window: Any) -> bool:
    """Best-effort native UI witness that playback is active.

    Yandex Music shows a Pause action while a track is playing.  This is useful
    on Electron builds that do not publish a stable GSMTC session.
    """
    for control in _yandex_descendants(window):
        desc = _yandex_control_descriptor(control)
        if not desc:
            continue
        ctype = _yandex_control_type(control)
        if ctype not in {"Button", "Text", "Hyperlink"}:
            continue
        if re.search(r"(?<![a-zа-я0-9])(?:пауза|pause)(?![a-zа-я0-9])", desc):
            rect = _yandex_rect(control)
            if rect is None:
                continue
            try:
                wl, wt, wr, wb = _yandex_window_bounds(window)
                left, top, right, bottom = rect
                # Player controls live in the lower band; ignore explanatory text.
                if top >= wt + int(max(1, wb-wt) * 0.72):
                    return True
            except Exception:
                return True
    return False


def _first_yandex_track_text_control(window: Any) -> Any | None:
    """Locate the first title text in the Tracks table when rows are not UIA ListItems.

    Recent Yandex Music Electron builds expose track title/artist/album as separate
    Text nodes.  Clicking a hard-coded 29% vertical position lands on the column
    header (the exact miss visible in the user's screenshot).  Restrict candidates
    to the title column and choose the top-most semantic text below the header.
    """
    try:
        wl, wt, wr, wb = _yandex_window_bounds(window)
        ww, wh = max(1, wr-wl), max(1, wb-wt)
    except Exception:
        wl, wt, ww, wh = 0, 0, 1200, 900

    excluded_exact = {
        "трек", "треки", "track", "tracks", "исполнитель", "artist",
        "альбом", "album", "поиск", "search", "радио", "главное",
    }
    candidates: list[tuple[int, int, int, Any]] = []
    for control in _yandex_descendants(window):
        ctype = _yandex_control_type(control)
        if ctype not in {"Text", "Hyperlink", "ListItem"}:
            continue
        desc = _yandex_control_descriptor(control)
        rect = _yandex_rect(control)
        if not desc or rect is None or desc in excluded_exact:
            continue
        left, top, right, bottom = rect
        width, height = max(1, right-left), max(1, bottom-top)
        # On the Tracks view the title column starts just to the right of album art
        # and ends well before the Artist column. Use proportions so DPI/resize are
        # handled without hard-coding the user's 1711x884 screenshot.
        if left < wl + int(ww * 0.26) or left > wl + int(ww * 0.58):
            continue
        if top < wt + int(wh * 0.31) or top > wt + int(wh * 0.76):
            continue
        if height > max(80, int(wh * 0.10)) or width < 20:
            continue
        # A full-width ListItem/Pane may describe the entire row (title + artist
        # + album). Clicking its centre is not the same as clicking the title and
        # can land far away from the first-track hit target. Prefer compact leaf
        # text and reject over-broad wrappers here; row wrappers are handled by
        # the dedicated row path in _activate_first_yandex_track().
        if width > int(ww * 0.43):
            continue
        if re.fullmatch(r"\d{1,2}:\d{2}", desc):
            continue
        if any(desc.startswith(prefix) for prefix in ("исполнитель ", "artist ", "альбом ", "album ")):
            continue
        type_rank = 0 if ctype == "Text" else 1 if ctype == "Hyperlink" else 2
        candidates.append((top, type_rank, left, control))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3] if candidates else None


def _first_yandex_track_geometry(window: Any) -> tuple[int, int, str]:
    """Return a DPI-safe point inside the first Tracks-table row.

    Prefer the real ``ТРЕК`` column header as an anchor.  The first row is one
    row-height below it in every current desktop layout, while the absolute
    ``0.355 * height`` fallback remains only for builds that expose no header.
    """
    wl, wt, wr, wb = _yandex_window_bounds(window)
    ww, wh = max(1, wr-wl), max(1, wb-wt)
    headers: list[tuple[int, int, int, int]] = []
    for control in _yandex_descendants(window):
        desc = _yandex_control_descriptor(control)
        # pywinauto frequently exposes the same caption through both
        # window_text() and element_info.name, so the normalized descriptor can
        # be "трек трек" / "track track".  Treat one or repeated copies as the
        # singular table header; do not require byte-for-byte equality.
        header_tokens = desc.split()
        if not header_tokens or not all(token in {"трек", "track"} for token in header_tokens):
            continue
        rect = _yandex_rect(control)
        if rect is None:
            continue
        left, top, right, bottom = rect
        # Sidebar uses plural "Треки"; this bounds the singular table header to
        # the main content area even on resized/DPI-scaled windows.
        if left < wl + int(ww * 0.22) or top < wt + int(wh * 0.16) or top > wt + int(wh * 0.46):
            continue
        headers.append((top, left, right, bottom))
    if headers:
        top, left, right, bottom = sorted(headers)[0]
        x = int(max(wl + ww * 0.29, min(wl + ww * 0.48, left + max(58, ww * 0.055))))
        y = int(bottom + max(26, min(58, wh * 0.052)))
        return x, y, "header_relative"
    return int(wl + ww * 0.36), int(wt + wh * 0.355), "window_ratio"


def _activate_first_yandex_track(window: Any) -> tuple[str, bool]:
    """Start the first visible library track, bounded to the main content pane."""
    try:
        wl, wt, wr, wb = _yandex_window_bounds(window)
        ww, wh = max(1, wr-wl), max(1, wb-wt)
    except Exception:
        wl, wt, ww, wh = 0, 0, 1200, 900

    # Prefer explicit Play buttons in the content area.
    buttons: list[tuple[int, Any]] = []
    for control in _yandex_descendants(window):
        desc = _yandex_control_descriptor(control)
        if not desc or not any(x in desc for x in ("воспроизвед", "play")):
            continue
        if any(x in desc for x in ("моя волна", "my wave", "пауза", "pause")):
            continue
        rect = _yandex_rect(control)
        if rect is None:
            continue
        left, top, right, bottom = rect
        if left < wl + int(ww * 0.25) or top < wt + int(wh * 0.15) or top >= wt + int(wh * 0.82):
            continue
        buttons.append((top, control))
    buttons.sort(key=lambda item: item[0])
    for _top, control in buttons[:8]:
        used = _yandex_invoke(control)
        if used:
            return f"first_track_play_{used}", True

    # Electron often exposes each track as a compact ListItem/Hyperlink but hides
    # the hover-only play button. Pick only rows in the main pane; the missing X
    # filter in v3.4 is what allowed sidebar items to be activated accidentally.
    rows: list[tuple[int, int, Any]] = []
    generic = ("трек", "треки", "track", "tracks", "альбом", "исполнител", "плейлист", "главн", "радио", "подкаст", "поиск")
    for control in _yandex_descendants(window):
        ctype = _yandex_control_type(control)
        if ctype not in {"ListItem", "Hyperlink"}:
            continue
        desc = _yandex_control_descriptor(control)
        rect = _yandex_rect(control)
        if not desc or rect is None:
            continue
        left, top, right, bottom = rect
        width, height = max(1, right-left), max(1, bottom-top)
        if left < wl + int(ww * 0.25) or top < wt + int(wh * 0.16) or top >= wt + int(wh * 0.80):
            continue
        if height > max(150, int(wh * 0.18)) or width < 100:
            continue
        if any(desc == word or desc.startswith(word + " ") for word in generic):
            continue
        # Track rows usually have meaningful text and are wider than tiny controls.
        semantic = len([x for x in desc.split() if len(x) >= 2])
        if semantic < 1:
            continue
        rows.append((top, -width, control))
    rows.sort(key=lambda item: (item[0], item[1]))
    for _top, _neg_width, control in rows[:6]:
        for method in ("double_click_input", "click_input", "invoke", "click"):
            fn = getattr(control, method, None)
            if not callable(fn):
                continue
            try:
                if method == "click_input":
                    fn(double=True)
                else:
                    fn()
                return f"first_track_row_{method}", False
            except TypeError:
                try:
                    fn()
                    return f"first_track_row_{method}", False
                except Exception:
                    pass
            except Exception:
                pass

    # Some Yandex Electron builds expose only the individual title Text node.
    # Click its real rectangle instead of guessing the row from the page heading.
    title_control = _first_yandex_track_text_control(window)
    if title_control is not None:
        rect = _yandex_rect(title_control)
        if rect is not None:
            try:
                import pyautogui
                left, top, right, bottom = rect
                pyautogui.doubleClick(int((left + right) / 2), int((top + bottom) / 2), interval=0.08)
                logger.info("Yandex tracks: clicked first title node %r at %s", _yandex_control_descriptor(title_control), rect)
                return "first_track_title_geometry", False
            except Exception:
                pass

    # Final native-only geometry fallback. Anchor to the visible table header when
    # Chromium exposes it; only then fall back to a window ratio.
    try:
        import pyautogui
        x, y, geometry_source = _first_yandex_track_geometry(window)
        pyautogui.doubleClick(x, y, interval=0.08)
        logger.info("Yandex tracks: geometry fallback (%s) click at (%s, %s)", geometry_source, x, y)
        method = "first_track_native_geometry_v36" if geometry_source == "window_ratio" else "first_track_native_geometry_header_relative"
        return method, False
    except Exception:
        return "", False


def play_my_tracks_in_yandex() -> dict[str, Any]:
    """Open My Music → Tracks and start the first track in the native app."""
    if not (_yandex_windows() or _running_yandex_process_names()):
        launch_yandex_music()
    window = focus_yandex_music_window()
    if window is None:
        raise RuntimeError("Не вижу окно приложения Яндекс Музыка")

    nav = _find_yandex_nav_control(window, ("треки", "tracks"))
    nav_method = ""
    if nav is not None:
        # For text wrappers, clicking the actual label centre is safer than
        # invoking a possibly over-broad ancestor in Chromium.
        rect = _yandex_rect(nav)
        if rect is not None:
            try:
                import pyautogui
                l, t, r, b = rect
                pyautogui.click((l+r)//2, (t+b)//2)
                nav_method = "tracks_label_click"
            except Exception:
                nav_method = _yandex_invoke(nav)
        else:
            nav_method = _yandex_invoke(nav)

    if not nav_method:
        try:
            import pyautogui
            left, top, right, bottom = _yandex_window_bounds(window)
            pyautogui.click(int(left + (right-left)*0.075), int(top + (bottom-top)*0.37))
            nav_method = "native_tracks_geometry"
        except Exception as exc:
            raise RuntimeError("Не удалось открыть раздел «Треки» в приложении Яндекс Музыка") from exc

    # Allow the React view to settle. If the first interaction hit a parent row,
    # explicitly retry the known Tracks label geometry rather than scrolling or
    # activating another navigation item.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not _yandex_tracks_page_visible(window):
        time.sleep(0.12)
    if not _yandex_tracks_page_visible(window):
        try:
            import pyautogui
            left, top, right, bottom = _yandex_window_bounds(window)
            pyautogui.click(int(left + (right-left)*0.075), int(top + (bottom-top)*0.37))
            nav_method += "+tracks_geometry_retry"
            time.sleep(0.75)
        except Exception:
            pass

    # Snapshot the previous media session so an old paused title cannot be mistaken
    # for proof that the first row was successfully started.
    before_status: dict[str, Any] = {}
    try:
        before_status = music_app_status("yandex_music")
    except Exception:
        pass

    used, strong_action = _activate_first_yandex_track(window)
    if not used:
        raise RuntimeError("Раздел «Треки» открыт, но первую композицию не удалось запустить через доступный интерфейс приложения")

    verified = False
    status: dict[str, Any] = {}
    deadline = time.monotonic() + 3.2
    while time.monotonic() < deadline:
        try:
            status = music_app_status("yandex_music")
            state = normalize(str(status.get("status", "")))
            # Require actual playback. v3.5 accepted any stale title, which is why
            # it could report success after clicking above the first row.
            if state in {"playing", "воспроизведение", "play"}:
                verified = True
                break
        except Exception:
            pass
        if _yandex_player_is_playing(window):
            verified = True
            break
        time.sleep(0.18)

    # If a weak row/geometry interaction did not start playback, make one precise
    # retry against the detected title node (or corrected first-row coordinate).
    if not verified and not strong_action:
        retry_control = _first_yandex_track_text_control(window)
        try:
            import pyautogui
            if retry_control is not None and _yandex_rect(retry_control) is not None:
                l, t, r, b = _yandex_rect(retry_control)
                pyautogui.doubleClick(int((l+r)/2), int((t+b)/2), interval=0.10)
                retry_method = "title_retry"
            else:
                gx, gy, source = _first_yandex_track_geometry(window)
                pyautogui.doubleClick(gx, gy, interval=0.10)
                retry_method = f"geometry_retry_{source}"
            used += "+" + retry_method
            retry_deadline = time.monotonic() + 2.2
            while time.monotonic() < retry_deadline:
                try:
                    status = music_app_status("yandex_music")
                    if normalize(str(status.get("status", ""))) in {"playing", "воспроизведение", "play"}:
                        verified = True
                        break
                except Exception:
                    pass
                if _yandex_player_is_playing(window):
                    verified = True
                    break
                time.sleep(0.18)
        except Exception:
            pass

    return {
        **status,
        "app": "yandex_music", "started": bool(verified or strong_action),
        "verified": bool(verified or strong_action),
        "command_sent": True, "method": f"tracks:{nav_method}:{used}",
        "verification": "windows_media_session_or_native_pause" if verified else ("native_play_control" if strong_action else "unverified_track_click"),
        "previous_title": str(before_status.get("title", "") or ""),
    }

def play_track_in_yandex(query: str) -> dict[str, Any]:
    """Search/play a track strictly inside the native Yandex Music app.

    Yandex exposes Search as a navigation link first; activating it moves focus to
    the editor. Older NEO builds searched for an Edit control on the Radio/Home
    screen and therefore failed before opening Search at all. The flow below
    follows the app's accessibility model and does not open a browser.
    """
    query = str(query or "").strip()
    if not query:
        raise ValueError("Не указано название трека")
    if not (_yandex_windows() or _running_yandex_process_names()):
        launch_yandex_music()
        time.sleep(0.7)
    window = focus_yandex_music_window()
    if window is None:
        raise RuntimeError("Не вижу окно приложения Яндекс Музыка")

    search_entry = _find_yandex_search_entry(window)
    entry_method = "already_search"
    if search_entry is not None and _yandex_control_type(search_entry) not in {"Edit", "Document"}:
        used = _yandex_invoke(search_entry)
        if used:
            entry_method = f"search_link_{used}"
            time.sleep(0.35)

    typing_method = _paste_yandex_search_query(window, query)
    entry_method = f"{entry_method}:{typing_method}"

    deadline = time.monotonic() + 3.2
    used = ""
    tied = False
    keyboard_fallback = False
    while time.monotonic() < deadline:
        used, tied = _activate_yandex_result(window, query)
        if used:
            break
        time.sleep(0.15)
    if not used:
        # Restore the compact v3.0/v3.1 keyboard route, but only *after* v3.5 has
        # positively verified that the requested text was inside Search. This is
        # safe inside the focused native app and avoids dependence on Chromium
        # exposing result rows/buttons through UIA.
        try:
            import pyautogui
            focus_yandex_music_window()
            pyautogui.press("down")
            time.sleep(0.14)
            pyautogui.press("enter")
            time.sleep(0.75)
            used = "keyboard_first_result"
            tied = True
            keyboard_fallback = True
        except Exception:
            visible = _yandex_ui_query_visible(window, query)
            detail = "Запрос виден в интерфейсе, но результат не дал доступного действия." if visible else "Текст запроса не появился в доступном дереве интерфейса."
            raise RuntimeError(
                "Поиск в приложении Яндекс Музыка открыт, но первый подходящий трек не удалось запустить. " + detail
            )

    # A click/Enter is not proof of success. Verify that the requested track is
    # actually playing. Prefer the Windows media session when it exposes both
    # matching metadata and an active playback state; otherwise require a
    # query-matched native player region plus a visible Pause state.
    deadline = time.monotonic() + 3.8
    wanted = _query_tokens(query)
    while time.monotonic() < deadline:
        try:
            last = music_app_status("yandex_music")
            hay = normalize(f"{last.get('artist','')} {last.get('title','')}")
            state = normalize(str(last.get("status", "") or last.get("playback_status", "") or ""))
            title_matches = (not wanted) or sum(token in hay for token in wanted) >= max(1, min(2, len(wanted)))
            playback_active = any(token in state for token in ("playing", "play", "воспроизвод")) and "paused" not in state and "pause" not in state
            if title_matches and playback_active:
                return {
                    **last, "query": query, "started": True,
                    "method": f"yandex_app_accessibility:{entry_method}:{used}",
                    "verified": True, "verification": "windows_media_session_playing",
                }
        except Exception:
            pass

        try:
            if _yandex_ui_query_visible(window, query, bottom_only=True) and _yandex_player_is_playing(window):
                return {
                    "app": "yandex_music", "query": query, "started": True,
                    "method": f"yandex_app_accessibility:{entry_method}:{used}",
                    "verified": True, "verification": "native_player_query_and_pause",
                }
        except Exception:
            pass
        time.sleep(0.18)

    # Never turn an input action into a false-positive success. Exact-row UIA
    # activation and keyboard fallback both still need a playback confirmation.
    if tied and not keyboard_fallback:
        raise RuntimeError(
            "Нужный результат Яндекс Музыки был выбран, но приложение не подтвердило, что трек действительно воспроизводится."
        )
    if keyboard_fallback:
        raise RuntimeError(
            "Первый результат Яндекс Музыки был отправлен на воспроизведение с клавиатуры, но приложение не подтвердило запуск трека."
        )
    raise RuntimeError(
        "Яндекс Музыка получила команду воспроизведения первого результата, но NEO не смог связать его с запросом."
    )

