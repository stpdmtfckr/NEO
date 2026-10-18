# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import os
import time
from typing import Any, Callable

import psutil

from jarvis.audio import AudioInputController, AudioOutputController
from jarvis.audio.control_worker import AudioJobOutcomeUnknown, audio_control_executor
from jarvis.audio.providers.windows_core_audio import WindowsCoreAudioOutputProvider
from jarvis.desktop_resolver import normalize as normalize_target, target_similarity
from jarvis.browser_control import canonical_service
from jarvis.music_app_control import canonical_music_app, set_music_app_volume
from jarvis.security import Risk
from jarvis.tools.registry import ToolDefinition, ToolExecution


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def _enum(values: list[str], description: str) -> dict[str, Any]:
    return {"type": "string", "enum": values, "description": description}


def register_audio_tools(registry: Any, *, config: Any, send_media_key: Callable[[int], None], run_command: Callable[..., Any]) -> None:
    _send_media_key = send_media_key
    _run = run_command
    # AUDIO
    audio_input = AudioInputController()
    audio_output = AudioOutputController()

    def _preferred_browser_process(cfg: Any) -> str:
        value = str(cfg.get("desktop.preferred_browser", "firefox") or "firefox").casefold()
        if "chrome" in value: return "chrome.exe"
        if "edge" in value: return "msedge.exe"
        if "opera" in value: return "opera.exe"
        if "brave" in value: return "brave.exe"
        if "vivaldi" in value: return "vivaldi.exe"
        if "yandex" in value or "яндекс" in value: return "browser.exe"
        return "firefox.exe"

    def audio_risk(args: dict[str, Any]) -> Risk:
        return Risk.MEDIUM if args.get("action") in {"set_default", "set_input_mute", "set_input_volume", "route_app_device"} else Risk.SAFE

    def _system_output_get_volume() -> int:
        """Read system master output volume inside the serialized COM apartment."""
        if os.name != "nt":
            raise OSError("Core Audio доступен только в Windows")
        if not WindowsCoreAudioOutputProvider.available():
            raise RuntimeError("Windows Core Audio/pycaw недоступен")
        return int(audio_control_executor().submit(WindowsCoreAudioOutputProvider.get_volume, timeout=8.0))

    def _system_output_set_volume(percent: int | float) -> int:
        """Set system master output volume without leaking COM objects to caller threads."""
        if os.name != "nt":
            raise OSError("Core Audio доступен только в Windows")
        if not WindowsCoreAudioOutputProvider.available():
            raise RuntimeError("Windows Core Audio/pycaw недоступен")
        return int(audio_control_executor().submit(WindowsCoreAudioOutputProvider.set_volume, percent, timeout=8.0))

    def _system_output_set_mute(muted: bool) -> bool:
        if os.name != "nt":
            raise OSError("Core Audio доступен только в Windows")
        if not WindowsCoreAudioOutputProvider.available():
            raise RuntimeError("Windows Core Audio/pycaw недоступен")
        return bool(audio_control_executor().submit(WindowsCoreAudioOutputProvider.set_mute, bool(muted), timeout=8.0))

    def _set_session_volume(target: str, level: float) -> int:
        """Set one app session volume on the single Core Audio owner thread."""
        if os.name != "nt" or not target:
            return 0

        def _work() -> int:
            # Initializes COM once on jarvis-audio-control and keeps every pycaw
            # session wrapper confined to that thread.
            WindowsCoreAudioOutputProvider._imports()
            from pycaw.pycaw import AudioUtilities

            target_norm = normalize_target(target)
            allowed: set[str] = set()
            if target_norm.startswith("process "):
                allowed.add(target_norm.split(" ", 1)[1])
            else:
                process_aliases = {
                    "browser": {"chrome.exe", "msedge.exe", "opera.exe", "opera_gx.exe", "firefox.exe", "browser.exe", "yandex.exe", "brave.exe", "vivaldi.exe"},
                    "yandex music": {"yandexmusic.exe", "yandex music.exe", "music.exe", "yandex.exe"},
                }
                allowed = set(process_aliases.get(target_norm, set()))

            changed = 0
            for session in AudioUtilities.GetAllSessions():
                process_name = ""
                try:
                    process = getattr(session, "Process", None)
                    if process is not None:
                        process_name = str(process.name() or "").casefold()
                except Exception:
                    process_name = ""
                label = normalize_target(" ".join([
                    process_name,
                    str(getattr(session, "DisplayName", "") or ""),
                    str(getattr(session, "Identifier", "") or ""),
                ]))
                matches = process_name in allowed if allowed else bool(target_norm and target_norm in label)
                if target_norm in {"yandex music", "яндекс музыка"} and not matches:
                    matches = "yandex" in label and ("music" in label or "музык" in label)
                if not matches:
                    continue
                volume = getattr(session, "SimpleAudioVolume", None)
                if volume is None:
                    continue
                volume.SetMasterVolume(max(0.0, min(1.0, level)), None)
                changed += 1
            return changed

        return int(audio_control_executor().submit(_work, timeout=8.0))

    def _change_session_volume(target: str, delta: float) -> list[dict[str, Any]]:
        if os.name != "nt" or not target:
            return []

        def _work() -> list[dict[str, Any]]:
            WindowsCoreAudioOutputProvider._imports()
            from pycaw.pycaw import AudioUtilities

            target_norm = normalize_target(target)
            aliases = {
                "discord": {"discord.exe"}, "дискорд": {"discord.exe"},
                "browser": {"chrome.exe","msedge.exe","opera.exe","opera_gx.exe","firefox.exe","browser.exe","brave.exe","vivaldi.exe"},
                "youtube": {_preferred_browser_process(config)}, "ютуб": {_preferred_browser_process(config)},
                "yandex music": {"yandexmusic.exe","yandex.exe","music.exe"}, "яндекс музыка": {"yandexmusic.exe","yandex.exe","music.exe"},
            }
            allowed = aliases.get(target_norm, set())
            changed: list[dict[str, Any]] = []
            for session in AudioUtilities.GetAllSessions():
                try:
                    proc = getattr(session, "Process", None)
                    proc_name = str(proc.name() or "").casefold() if proc is not None else ""
                except Exception:
                    proc_name = ""
                label = normalize_target(" ".join([proc_name, str(getattr(session,"DisplayName","") or ""), str(getattr(session,"Identifier","") or "")]))
                matches = proc_name in allowed if allowed else bool(target_norm and target_norm in label)
                if not matches:
                    continue
                vol = getattr(session, "SimpleAudioVolume", None)
                if vol is None:
                    continue
                try:
                    before = float(vol.GetMasterVolume())
                    after = max(0.0, min(1.0, before + float(delta)))
                    vol.SetMasterVolume(after, None)
                    changed.append({"process": proc_name, "before": round(before,3), "after": round(after,3)})
                except Exception:
                    continue
            return changed

        return list(audio_control_executor().submit(_work, timeout=8.0))

    def _list_audio_sessions() -> list[dict[str, Any]]:
        """Return current per-app Core Audio sessions without leaking COM wrappers."""
        if os.name != "nt":
            return []

        def _work() -> list[dict[str, Any]]:
            WindowsCoreAudioOutputProvider._imports()
            from pycaw.pycaw import AudioUtilities
            merged: dict[str, dict[str, Any]] = {}
            for session in AudioUtilities.GetAllSessions():
                proc_name = ""
                pid = 0
                try:
                    proc = getattr(session, "Process", None)
                    if proc is not None:
                        proc_name = str(proc.name() or "")
                        pid = int(getattr(proc, "pid", 0) or 0)
                except Exception:
                    proc_name = ""
                    pid = 0
                display_name = str(getattr(session, "DisplayName", "") or "").strip()
                identifier = str(getattr(session, "Identifier", "") or "").strip()
                volume = getattr(session, "SimpleAudioVolume", None)
                if volume is None:
                    continue
                try:
                    level = max(0.0, min(1.0, float(volume.GetMasterVolume())))
                    muted = bool(volume.GetMute())
                except Exception:
                    continue
                display = display_name or proc_name or identifier or "Системная аудиосессия"
                target = f"process {proc_name.casefold()}" if proc_name else display
                key = proc_name.casefold() if proc_name else normalize_target(display)
                if not key:
                    continue
                row = merged.get(key)
                if row is None:
                    merged[key] = {
                        "display": display, "process": proc_name, "pid": pid, "target": target,
                        "volume_percent": round(level * 100, 1), "muted": muted, "sessions": 1,
                    }
                else:
                    row["sessions"] = int(row.get("sessions", 1) or 1) + 1
                    # Show the loudest session as a useful aggregate for apps with
                    # helper/render processes; setting by process updates all of them.
                    row["volume_percent"] = max(float(row.get("volume_percent", 0) or 0), round(level * 100, 1))
                    row["muted"] = bool(row.get("muted", False)) and muted
            rows = list(merged.values())
            rows.sort(key=lambda x: str(x.get("display", "") or x.get("process", "")).casefold())
            return rows

        return list(audio_control_executor().submit(_work, timeout=8.0))

    def _foreground_browser_process() -> str:
        if os.name != "nt":
            return ""
        try:
            import ctypes as _ctypes
            from pywinauto import Desktop
            handle = int(_ctypes.windll.user32.GetForegroundWindow())
            if not handle:
                return ""
            win = Desktop(backend="uia").window(handle=handle)
            pid = int(win.element_info.process_id)
            return str(psutil.Process(pid).name() or "").casefold()
        except Exception:
            return ""



    def _foreground_process_name() -> str:
        if os.name != "nt": return ""
        try:
            import ctypes as _ctypes
            hwnd=int(_ctypes.windll.user32.GetForegroundWindow())
            pid=_ctypes.c_ulong(); _ctypes.windll.user32.GetWindowThreadProcessId(hwnd, _ctypes.byref(pid))
            return str(psutil.Process(int(pid.value)).name() or "").casefold()
        except Exception:
            return ""

    def audio(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", ""))
        if action == "list_devices":
            requested_type = str(args.get("device_type", "") or "").strip().casefold()
            if requested_type not in {"", "input", "output"}:
                raise ValueError("device_type должен быть input или output")
            devices: list[dict[str, Any]] = []
            if requested_type in {"", "input"}:
                devices.extend(
                    {**device.as_dict(), "type": "input"}
                    for device in audio_input.list_devices()
                )
            if requested_type in {"", "output"}:
                try:
                    devices.extend(
                        {**device.as_dict(), "type": "output"}
                        for device in audio_output.list_devices()
                    )
                except Exception:
                    # A machine without a native render-endpoint provider must not
                    # lose microphone enumeration. PortAudio is display-only and
                    # is used strictly as an output-list fallback.
                    try:
                        import sounddevice as sd
                        for info in sd.query_devices():
                            name = str(info.get("name", ""))
                            if int(info.get("max_output_channels", 0)) > 0:
                                devices.append({"name": name, "type": "output", "device_id": "", "is_default": False, "provider": "sounddevice"})
                    except Exception:
                        pass
            label = "Микрофонов" if requested_type == "input" else "Устройств вывода" if requested_type == "output" else "Аудиоустройств"
            return ToolExecution(True, f"{label}: {len(devices)}", devices)

        if action == "list_sessions":
            try:
                rows = _list_audio_sessions()
            except Exception as exc:
                return ToolExecution(False, f"Не удалось получить аудиосессии приложений: {exc}", [])
            if not rows:
                return ToolExecution(True, "Активных пользовательских аудиосессий сейчас нет.", [])
            lines = [f"• {str(x.get('display','') or x.get('process',''))}: {int(round(float(x.get('volume_percent',0) or 0)))}%" + (" · mute" if bool(x.get('muted',False)) else "") for x in rows[:50]]
            return ToolExecution(True, "Активные аудиосессии приложений:\n" + "\n".join(lines), rows)

        if action in {"set_volume", "change_volume", "mute"}:
            try:
                if action == "set_volume":
                    level = max(0.0, min(100.0, float(args.get("value", 50)))) / 100.0
                    target = str(args.get("target", "") or "").strip()
                    target_norm = canonical_service(target)
                    target_words = normalize_target(target)
                    preferred_browser = str(config.get("desktop.preferred_browser", "firefox") or "firefox").casefold()
                    preferred_music_app = canonical_music_app(str(config.get("desktop.preferred_music_app", "yandex_music") or "yandex_music"))

                    # Final media architecture: music volume belongs only to the
                    # native music application. Never look for VK/Yandex browser tabs.
                    if target_norm in {"music", "яндекс музыка"} or target_words in {"yandex_music", "yandex music", "яндекс музыка", "музыка"}:
                        try:
                            data = set_music_app_volume(round(level * 100), preferred_music_app)
                            return ToolExecution(True, f"Громкость приложения Яндекс Музыка установлена: {round(level * 100)}%", {**data, "domain": "music_app"})
                        except Exception as exc:
                            return ToolExecution(False, f"Не удалось изменить громкость приложения Яндекс Музыка: {exc}. Общую громкость и браузер не меняю.", {"domain": "music_app", "safe_failure": True})
                    if target_norm == "вконтакте" or target_words in {"vk", "вк", "вконтакте"}:
                        return ToolExecution(False, "Музыкой VK в браузере NEO намеренно не управляет. Для музыки используется отдельное приложение Яндекс Музыка.", {"browser_music_disabled": True, "safe_failure": True})

                    # Video lives only in the configured browser. Volume here means
                    # the Windows mixer volume of that one browser process, never the
                    # music app and never system master volume.
                    if target_norm in {"video", "youtube"} or target_words in {"video", "видео", "youtube", "ютуб", "browser", "браузер"}:
                        browser_exes = {
                            "firefox": "firefox.exe", "mozilla": "firefox.exe",
                            "chrome": "chrome.exe", "google chrome": "chrome.exe",
                            "edge": "msedge.exe", "microsoft edge": "msedge.exe",
                            "opera": "opera.exe", "yandex": "browser.exe", "яндекс": "browser.exe",
                            "brave": "brave.exe", "vivaldi": "vivaldi.exe",
                        }
                        process_name = next((exe for key, exe in browser_exes.items() if key in preferred_browser), "firefox.exe")
                        changed = _set_session_volume(f"process {process_name}", level)
                        if changed:
                            return ToolExecution(True, f"Громкость браузера {process_name} установлена: {round(level * 100)}%", {"target": "youtube" if target_norm in {"video", "youtube"} else "browser", "process": process_name, "sessions_changed": changed, "mode": "browser_app_mixer", "domain": "video_browser"})
                        return ToolExecution(False, f"Не нашёл активную аудиосессию выбранного браузера {process_name}; другие приложения и общую громкость не меняю.", {"process": process_name, "domain": "video_browser", "safe_failure": True})

                    if target:
                        changed = _set_session_volume(target, level)
                        if changed:
                            return ToolExecution(True, f"Громкость приложения {target} установлена: {round(level * 100)}%", {"target": target, "sessions_changed": changed, "mode": "windows_mixer"})
                        return ToolExecution(False, f"Не нашёл аудиосессию приложения {target}; общую громкость не меняю.", {"target": target, "safe_failure": True})

                    actual = _system_output_set_volume(round(level * 100))
                    return ToolExecution(True, f"Общая громкость установлена: {actual}%", {"value": actual, "provider": "windows_core_audio", "serialized": True})
                if action == "change_volume":
                    delta = float(args.get("value", 0))
                    # HIST-055: one job on one resolved device (no separate get/set
                    # that could address two different «default» endpoints).
                    change = getattr(WindowsCoreAudioOutputProvider, "change_volume", None)
                    if callable(change):
                        result = dict(audio_control_executor().submit(change, delta, timeout=8.0))
                        actual = int(result["value"])
                        return ToolExecution(True, f"Громкость: {actual}%", {"value": actual, "provider": "windows_core_audio",
                                                                           "serialized": True, "device_id": result.get("device_id", "")})
                    current = _system_output_get_volume()
                    requested = max(0, min(100, int(round(current + delta))))
                    actual = _system_output_set_volume(requested)
                    return ToolExecution(True, f"Громкость: {actual}%", {"value": actual, "provider": "windows_core_audio", "serialized": True})
                muted = bool(args.get("muted", True))
                actual = _system_output_set_mute(muted)
                return ToolExecution(True, "Звук выключен" if actual else "Звук включён", {"muted": actual, "provider": "windows_core_audio", "serialized": True})
            except Exception as exc:
                # Last-resort path uses Windows' own volume keys and therefore
                # survives pycaw/comtypes API changes. It is only used when Core
                # Audio access fails.
                if isinstance(exc, AudioJobOutcomeUnknown):
                    # HIST-054: the Core Audio change may still land; pressing volume
                    # keys now would apply it a second time.
                    return ToolExecution(False, "Windows не подтвердила изменение звука вовремя; итог неизвестен, поэтому я не повторял его.",
                                         {"outcome_unknown": True, "safe_failure": True})
                if os.name != "nt":
                    raise
                if action == "set_volume":
                    requested_target = str(args.get("target", "") or "").strip()
                    if requested_target:
                        return ToolExecution(False, f"Не удалось изменить громкость именно {requested_target}: {exc}. Общую громкость не меняю.", {"target": requested_target, "safe_failure": True})
                    target = max(0, min(100, int(round(float(args.get("value", 50))))))
                    for _ in range(55):
                        _send_media_key(0xAE)  # VK_VOLUME_DOWN
                    for _ in range(max(0, int(round(target / 2)))):
                        _send_media_key(0xAF)  # VK_VOLUME_UP
                    return ToolExecution(True, f"Громкость установлена примерно: {target}%", {"fallback": "volume_keys", "reason": str(exc)})
                if action == "change_volume":
                    delta = int(round(float(args.get("value", 0) or 0)))
                    if delta == 0:
                        # HIST-053: a zero step changes nothing (no single «up» key press).
                        return ToolExecution(True, "Громкость не изменена: шаг 0%.", {"fallback": "volume_keys", "unchanged": True})
                    key = 0xAF if delta > 0 else 0xAE
                    for _ in range(max(1, min(50, int(round(abs(delta) / 2))))):
                        _send_media_key(key)
                    return ToolExecution(True, f"Громкость изменена примерно на {delta}%", {"fallback": "volume_keys", "reason": str(exc)})
                # HIST-053: the mute key only TOGGLES, so «выключи звук» could turn sound
                # back on when it is already muted. Without a readable backend NEO refuses
                # instead of guessing.
                return ToolExecution(False, "Не удалось надёжно включить или выключить звук: Windows Core Audio недоступен, "
                                            "а клавиша mute только переключает состояние. Ничего не менял.",
                                     {"fallback_refused": True, "reason": str(exc), "safe_failure": True})

        if action == "change_target_volume":
            target=str(args.get("target","") or "").strip()
            if not target: raise ValueError("Не указано приложение")
            delta=max(-100.0,min(100.0,float(args.get("value",0) or 0)))/100.0
            changed=_change_session_volume(target,delta)
            return ToolExecution(bool(changed), f"Изменил громкость {target} на {float(args.get('value',0) or 0):+.0f}%" if changed else f"Не нашёл активную аудиосессию {target}.", {"target":target,"changed":changed,"delta_percent":float(args.get("value",0) or 0)})

        if action == "mute_except":
            from jarvis.runtime.audio_focus import audio_focus_manager
            target=str(args.get("target","") or "").strip().casefold()
            common={
                "discord":"discord.exe", "дискорд":"discord.exe",
                "youtube":_preferred_browser_process(config), "ютуб":_preferred_browser_process(config), "браузер":_preferred_browser_process(config),
                "яндекс музыка":"yandexmusic.exe", "yandex music":"yandexmusic.exe",
            }
            if target in {"game","игра","игру","игры"}:
                keep=_foreground_process_name()
            else:
                keep=common.get(target, target if target.endswith(".exe") else (target+".exe" if target else ""))
            if not keep:
                return ToolExecution(False,"Не удалось определить приложение, которое нужно оставить слышимым.",{"safe_failure":True})
            changed=audio_focus_manager().duck(0.0,except_processes={keep})
            return ToolExecution(True,f"Заглушил остальные аудиосессии; оставил {keep}.",{"except_process":keep,"sessions_changed":changed})

        if action == "mute_target":
            target=str(args.get("target","") or "").strip()
            if not target: raise ValueError("Не указано приложение")
            changed=_set_session_volume(target,0.0)
            return ToolExecution(bool(changed), f"Заглушил {target}." if changed else f"Не нашёл аудиосессию {target}.", {"sessions_changed":changed,"target":target})

        if action == "duck_system":
            from jarvis.runtime.audio_focus import audio_focus_manager
            factor = max(0.0, min(1.0, float(args.get("value", 18) or 18) / 100.0))
            except_target = str(args.get("target", "") or "").strip().casefold()
            except_processes: set[str] = set()
            if except_target:
                common = {
                    "discord": "discord.exe", "дискорд": "discord.exe",
                    "youtube": _preferred_browser_process(config), "браузер": _preferred_browser_process(config),
                    "яндекс музыка": "yandexmusic.exe", "yandex music": "yandexmusic.exe",
                }
                except_processes.add(common.get(except_target, except_target if except_target.endswith(".exe") else except_target + ".exe"))
            changed = audio_focus_manager().duck(factor, except_processes=except_processes)
            return ToolExecution(True, f"Временно приглушено аудиосессий: {changed}", {"sessions_changed": changed, "factor": factor})

        if action == "restore_ducking":
            from jarvis.runtime.audio_focus import audio_focus_manager
            changed = audio_focus_manager().restore()
            return ToolExecution(True, f"Предыдущая громкость восстановлена для {changed} аудиосессий.", {"sessions_restored": changed})

        if action == "set_default":
            if os.name != "nt":
                raise OSError("Доступно только в Windows")
            device_name = str(args.get("device_name", "") or "").strip()
            device_id = str(args.get("device_id", "") or "").strip()
            device_type = str(args.get("device_type", "output"))
            if device_type == "input":
                data = audio_input.set_default(device_id=device_id, device_name=device_name)
                if not bool(data.get("verified", False)):
                    return ToolExecution(False, "Windows не подтвердил новый микрофон по умолчанию.", data)
                return ToolExecution(True, f"Микрофон по умолчанию переключён: {data.get('name') or device_name}", data)
            data = audio_output.set_default(device_id=device_id, device_name=device_name)
            if not bool(data.get("verified", False)):
                return ToolExecution(False, "Windows не подтвердил новое устройство вывода по умолчанию.", data)
            # HOTFIX103: JARVIS' Yandex/Spotify hidden Qt player owns a QAudioOutput
            # in a separate process. Windows can change the system default while
            # that object remains bound to the previous endpoint. Rebind only an
            # already-running worker; never start music as a side effect of an
            # audio-device command.
            player_rebind: dict[str, Any] | None = None
            try:
                from jarvis.integrations.background.local_player import local_player
                player_rebind = local_player().refresh_output_device()
            except Exception as exc:
                # The OS switch itself succeeded, so keep that success but expose
                # the playback migration failure instead of silently hiding it.
                player_rebind = {"verified": False, "error": str(exc)}
            merged = dict(data)
            if player_rebind is not None:
                merged["background_player_rebind"] = player_rebind
            suffix = ""
            if player_rebind is not None and not bool(player_rebind.get("verified", False)):
                suffix = " Фоновый плеер не подтвердил перенос и может потребовать повторного запуска трека."
            return ToolExecution(True, f"Устройство вывода по умолчанию переключено: {data.get('name') or device_name}.{suffix}".rstrip(), merged)

        if action == "set_input_mute":
            if os.name != "nt":
                raise OSError("Управление микрофоном доступно только в Windows")
            muted = bool(args.get("muted", True))
            device_id = str(args.get("device_id", "") or "").strip()
            data = audio_input.set_mute(muted, device_id=device_id)
            if not bool(data.get("verified")):
                return ToolExecution(False, "Windows не подтвердил новое состояние микрофона.", data)
            if muted:
                text = "Системный микрофон выключен."
                if data.get("saved_volume") is not None:
                    text += f" Запомнил уровень {int(data['saved_volume'])}%."
            else:
                text = "Системный микрофон включён."
                if data.get("restored_volume") is not None:
                    text += f" Громкость восстановлена до {int(data['restored_volume'])}%."
            return ToolExecution(
                True, text,
                {**data, "endpoint_scope": "explicit" if device_id else "default recording"},
            )

        if action in {"get_input_volume", "set_input_volume"}:
            if os.name != "nt":
                raise OSError("Управление уровнем микрофона доступно только в Windows")
            device_id = str(args.get("device_id", "") or "").strip()
            if action == "set_input_volume":
                # HIST-051: an explicit 0 stays 0; 50 is only the default for a missing value.
                raw_level = args.get("value")
                level = max(0, min(100, int(round(float(50 if raw_level is None or raw_level == "" else raw_level)))))
                data = audio_input.set_volume(level, device_id=device_id)
                if not bool(data.get("verified")):
                    return ToolExecution(False, f"Windows не подтвердил уровень микрофона {level}%.", data)
            else:
                data = audio_input.get_volume(device_id=device_id)
            actual = int(data.get("value", 0) or 0)
            return ToolExecution(
                True, f"Громкость системного микрофона: {actual}%",
                {**data, "endpoint_scope": "explicit" if device_id else "default recording"},
            )

        if action == "route_app_device":
            if os.name != "nt":
                raise OSError("Маршрутизация аудио приложений доступна только в Windows")
            app_name = str(args.get("target", "") or "").strip()
            device_name = str(args.get("device_name", "") or "").strip()
            if not app_name or not device_name:
                raise ValueError("Нужны target и device_name")
            # Windows documents ms-settings:apps-volume as the Volume mixer page.
            # Per-app endpoint routing does not have a stable public desktop API,
            # so use Windows' own UI and fail closed if UIA cannot prove the row.
            os.startfile("ms-settings:apps-volume")  # type: ignore[attr-defined]
            from pywinauto import Desktop
            deadline = time.monotonic() + 6.0; settings = None
            while time.monotonic() < deadline and settings is None:
                for win in Desktop(backend="uia").windows(visible_only=True):
                    try:
                        pid = int(win.element_info.process_id); proc_name = str(psutil.Process(pid).name() or "").casefold()
                        if proc_name in {"systemsettings.exe", "applicationframehost.exe"}:
                            settings = win; break
                    except Exception:
                        continue
                if settings is None: time.sleep(0.15)
            if settings is None:
                return ToolExecution(False, "Открыл Volume mixer, но не смог подтвердить окно Windows Settings.", {"safe_failure": True, "uri": "ms-settings:apps-volume"})
            wanted = normalize_target(app_name); app_ctrl = None
            try:
                for ctrl in settings.descendants():
                    label = normalize_target(str(ctrl.window_text() or ""))
                    if wanted and (wanted == label or wanted in label):
                        app_ctrl = ctrl; break
            except Exception:
                app_ctrl = None
            if app_ctrl is None:
                return ToolExecution(False, f"Volume mixer открыт, но приложение {app_name} сейчас не показано как активная аудиосессия.", {"safe_failure": True, "uri": "ms-settings:apps-volume"})
            row = app_ctrl
            combos = []
            for _ in range(6):
                try:
                    combos = list(row.descendants(control_type="ComboBox"))
                except Exception:
                    combos = []
                if combos: break
                try: row = row.parent()
                except Exception: break
            if not combos:
                return ToolExecution(False, f"Нашёл {app_name}, но эта сборка Windows не открыла доступный UIA-список устройства. Ничего не переключено.", {"safe_failure": True, "uri": "ms-settings:apps-volume"})
            selected = False
            for combo in combos:
                try:
                    combo.expand(); time.sleep(0.18)
                    for item in Desktop(backend="uia").windows(visible_only=True):
                        for li in item.descendants(control_type="ListItem"):
                            label = str(li.window_text() or "")
                            if target_similarity(device_name, label) >= 0.78:
                                li.click_input(); selected = True; break
                        if selected: break
                    if selected: break
                except Exception:
                    continue
            if not selected:
                return ToolExecution(False, f"Не нашёл устройство «{device_name}» в строке {app_name}; ничего не менял.", {"safe_failure": True})
            time.sleep(0.25)
            return ToolExecution(True, f"Для {app_name} выбран аудиовыход «{device_name}» через Windows Volume mixer.", {"target": app_name, "device": device_name, "verified": True, "uri": "ms-settings:apps-volume"})

        raise ValueError(f"Неизвестное действие audio: {action}")

    registry.register(
        ToolDefinition(
            "audio",
            "Управление общей и per-app громкостью Windows, mute, микрофоном, устройствами, default endpoint и capability-aware маршрутизацией приложения через штатный Volume mixer.",
            _object_schema(
                {
                    "action": _enum(
                        ["set_volume", "change_volume", "change_target_volume", "mute", "list_sessions", "list_devices", "set_default", "set_input_mute", "get_input_volume", "set_input_volume", "route_app_device", "mute_target", "mute_except", "duck_system", "restore_ducking"],
                        "Операция",
                    ),
                    "value": {"type": "number", "description": "Процент или изменение в процентах"},
                    "muted": {"type": "boolean"},
                    "device_name": {"type": "string"},
                    "device_id": {"type": "string", "description": "Стабильный Windows endpoint ID для точного выбора микрофона или устройства вывода"},
                    "device_type": _enum(["input", "output"], "Тип аудиоустройства"),
                    "target": {"type": "string", "description": "Необязательная цель: browser/YouTube/VK/Яндекс Музыка или имя приложения. Для браузера меняется громкость только соответствующего browser.exe/chrome.exe и т.п."},
                },
                ["action"],
            ),
            audio,
            audio_risk,
        )
    )

