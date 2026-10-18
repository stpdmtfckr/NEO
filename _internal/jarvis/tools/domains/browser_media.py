# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any
from urllib.parse import quote_plus
import webbrowser

from jarvis.browser_control import (
    activate_tab_by_index, activate_tab_by_title, canonical_service, close_tab_by_index,
    click_first_page_link, control_selected_tab_media, control_service_media,
    control_tab_media_by_index, list_browser_tabs,
)
from jarvis.desktop_resolver import normalize as normalize_target
from jarvis.runtime.undo import undo_manager
from jarvis.security import Risk
from jarvis.integrations.music_provider import active_music_provider, provider_label, provider_disabled_message
from jarvis.tools.registry import ToolDefinition, ToolExecution


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}

def _enum(values: list[str], description: str) -> dict[str, Any]:
    return {"type": "string", "enum": values, "description": description}

def register_browser_media_tools(registry: Any, *, config: Any, yandex_adapter: Any, spotify_adapter: Any, youtube_adapter: Any, discord_adapter: Any):
    # BROWSER
    def _browser_key(value: str = "") -> str:
        raw = normalize_target(value)
        if not raw or raw in {"browser", "браузер", "active", "current", "текущий"}:
            return ""
        if "firefox" in raw or "mozilla" in raw or "мозил" in raw or "мазил" in raw or "фаерфокс" in raw:
            return "firefox"
        if "chrome" in raw or "google chrome" in raw or "хром" in raw:
            return "chrome"
        if "edge" in raw or "microsoft edge" in raw or "эдж" in raw:
            return "edge"
        if "opera" in raw or "опера" in raw:
            return "opera"
        if "yandex" in raw or "яндекс браузер" in raw:
            return "yandex"
        if "brave" in raw:
            return "brave"
        if "vivaldi" in raw:
            return "vivaldi"
        return ""

    def _preferred_browser_name(requested: str = "") -> str:
        explicit = _browser_key(requested)
        if explicit:
            return explicit
        raw = str(config.get("desktop.preferred_browser", "firefox") or "firefox").strip()
        return _browser_key(raw) or "firefox"

    def _existing_browser_scope(requested: str = "") -> str:
        """Scope existing-window actions only when the user named a browser.

        Empty scope lets browser_control prefer the foreground/recent open browser.
        This prevents a configured Firefox preference from hiding an already-open
        Opera/Chrome/Edge window.
        """
        return _browser_key(requested)

    def _open_in_preferred_browser(url: str, requested: str = "") -> str:
        preferred = _preferred_browser_name(requested)
        if os.name != "nt" or not preferred or preferred.casefold() in {"system", "default", "системный"}:
            webbrowser.open(url)
            return preferred or "system"
        aliases = {
            "firefox": [os.path.expandvars(r"%ProgramFiles%\Mozilla Firefox\firefox.exe"), os.path.expandvars(r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe")],
            "mozilla": [os.path.expandvars(r"%ProgramFiles%\Mozilla Firefox\firefox.exe")],
            "chrome": [os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"), os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")],
            "edge": [os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"), os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")],
            "opera": [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\opera.exe")],
            "yandex": [os.path.expandvars(r"%LOCALAPPDATA%\Yandex\YandexBrowser\Application\browser.exe")],
        }
        key = preferred.casefold()
        if "firefox" in key or "моз" in key: key = "firefox"
        elif "chrome" in key or "хром" in key: key = "chrome"
        elif "edge" in key or "эдж" in key: key = "edge"
        elif "opera" in key or "опера" in key: key = "opera"
        elif "yandex" in key or "яндекс" in key: key = "yandex"
        for candidate in aliases.get(key, []):
            if candidate and Path(candidate).exists():
                subprocess.Popen([candidate, url])
                return preferred
        webbrowser.open(url)
        return preferred

    _TAB187 = {
        # REC-WORDING-SOURCE-01 W01: structured tab replies; browser name and tab
        # title stay literal. Activating an existing tab is "switched", not "opened".
        "activate": ("Переключился на вкладку {index}: {title}", "Switched to tab {index}: {title}", "Zu Tab {index} gewechselt: {title}",
                     "Passé à l’onglet {index} : {title}", "He cambiado a la pestaña {index}: {title}", "Passato alla scheda {index}: {title}",
                     "Mudei para o separador {index}: {title}"),
        "activate_title": ("Переключился на вкладку: {title}", "Switched to tab: {title}", "Zum Tab gewechselt: {title}",
                           "Passé à l’onglet : {title}", "He cambiado a la pestaña: {title}", "Passato alla scheda: {title}",
                           "Mudei para o separador: {title}"),
        "close": ("Закрыл вкладку {index}: {title}", "Closed tab {index}: {title}", "Tab {index} geschlossen: {title}",
                  "Onglet {index} fermé : {title}", "He cerrado la pestaña {index}: {title}", "Ho chiuso la scheda {index}: {title}",
                  "Fechei o separador {index}: {title}"),
        "new": ("Открыл новую вкладку в {browser}", "Opened a new tab in {browser}", "Neuen Tab in {browser} geöffnet",
                "Nouvel onglet ouvert dans {browser}", "He abierto una pestaña nueva en {browser}", "Ho aperto una nuova scheda in {browser}",
                "Abri um novo separador em {browser}"),
    }

    def _tab_text187(key: str, **values: Any) -> str:
        from jarvis.language import language_code
        codes = ("ru", "en", "de", "fr", "es", "it", "pt")
        code = language_code(config)
        return _TAB187[key][codes.index(code) if code in codes else 1].format(**values)

    def browser(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", ""))
        value = str(args.get("value", "") or "").strip()
        if action == "search":
            url = f"https://www.google.com/search?q={quote_plus(value)}"
            browser_name = _open_in_preferred_browser(url, str(args.get("browser", "") or ""))
            return ToolExecution(True, f"Ищу в {browser_name}: {value}", {"url": url, "query": value, "browser": browser_name})
        if action in {"open_url", "open"}:
            url = value
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
                url = "https://" + url
            browser_name = _open_in_preferred_browser(url, str(args.get("browser", "") or ""))
            return ToolExecution(True, f"Открыто в {browser_name}: {url}", {"url": url, "browser": browser_name})
        if action == "activate_tab":
            index = int(args.get("index", 1) or 1)
            browser_name = _existing_browser_scope(str(args.get("browser", "") or ""))
            data = activate_tab_by_index(index, browser_name)
            return ToolExecution(True, _tab_text187("activate", index=index, title=data.get('title', '')), {**data, "localized186": True})
        if action == "activate_tab_title":
            browser_name = _existing_browser_scope(str(args.get("browser", "") or ""))
            data = activate_tab_by_title(value, browser_name)
            return ToolExecution(True, _tab_text187("activate_title", title=data.get('title', '')), {**data, "localized186": True})
        if action == "close_tab":
            index = int(args.get("index", 1) or 1)
            browser_name = _existing_browser_scope(str(args.get("browser", "") or ""))
            data = close_tab_by_index(index, browser_name)
            def _restore_closed_tab():
                import pyautogui
                try:
                    tabs = list_browser_tabs(browser_name)
                    if tabs:
                        tabs[0].window.set_focus(); time.sleep(0.05)
                except Exception:
                    pass
                pyautogui.hotkey("ctrl", "shift", "t"); time.sleep(0.25)
                return {"restored": True, "browser": browser_name, "title": data.get("title", "")}
            undo_manager().push(f"Восстановить вкладку «{data.get('title', '') or index}»", "browser_tab", {"browser": browser_name, "title": data.get("title", ""), "index": index}, _restore_closed_tab)
            return ToolExecution(True, _tab_text187("close", index=index, title=data.get('title', '')), {**data, "localized186": True})
        if action == "new_tab":
            browser_name = _existing_browser_scope(str(args.get("browser", "") or ""))
            # Focus the active/explicit browser before sending the browser-native shortcut.
            try:
                tabs = list_browser_tabs(browser_name)
                if tabs:
                    tabs[0].window.set_focus()
                    time.sleep(0.05)
            except Exception:
                pass
            import pyautogui
            pyautogui.hotkey("ctrl", "t")
            return ToolExecution(True, _tab_text187("new", browser=browser_name), {"action": "new_tab", "browser": browser_name, "localized186": True})
        if action == "search_first":
            if not value:
                raise ValueError("Не указан поисковый запрос")
            url = f"https://www.google.com/search?q={quote_plus(value)}"
            browser_name = _open_in_preferred_browser(url, str(args.get("browser", "") or ""))
            time.sleep(0.75)
            data = click_first_page_link(browser_name)
            data.update({"query": value, "browser": browser_name})
            return ToolExecution(True, f"Нашёл «{value}» и открыл первую ссылку: {data.get('title', '')}", data)
        if action in {"list_browsers", "clear_history", "full_cleanup"}:
            from jarvis.integrations.browser_privacy import available_browsers, clear_history, full_cleanup
            if action == "list_browsers":
                rows = available_browsers()
                text = "Доступные браузерные профили:\n" + ("\n".join(f"• {row['label']}" for row in rows) if rows else "• не найдены")
                return ToolExecution(True, text, rows)
            browser_key = str(args.get("browser", "") or value).strip()
            if not browser_key:
                raise ValueError("Не выбран браузер для очистки.")
            data = clear_history(browser_key) if action == "clear_history" else full_cleanup(browser_key)
            label = str(data.get("label", browser_key))
            if action == "clear_history":
                return ToolExecution(True, f"История {label} очищена.", data)
            return ToolExecution(True, f"Локальные данные профиля {label} полностью удалены. Сам браузер не удалён.", data)
        raise ValueError(f"Неизвестное действие browser: {action}")

    registry.register(
        ToolDefinition(
            "browser",
            "Открытие URL/поиска и управление вкладками. Для уже открытых вкладок без явно названного браузера используется активное/доступное окно браузера; настройка preferred_browser применяется только при запуске нового браузера/URL.",
            _object_schema(
                {
                    "action": _enum(["open_url", "open", "search", "search_first", "activate_tab", "activate_tab_title", "close_tab", "new_tab", "list_browsers", "clear_history", "full_cleanup"], "Операция"),
                    "value": {"type": "string", "description": "URL или поисковая фраза"},
                    "index": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Номер вкладки слева направо, начиная с 1"},
                    "browser": {"type": "string", "description": "Необязательный браузер: Mozilla/Firefox, Chrome, Opera и т.п."},
                },
                ["action"],
            ),
            browser,
            lambda args: (Risk.CRITICAL if str(args.get("action", "")).casefold() == "full_cleanup" else (Risk.HIGH if str(args.get("action", "")).casefold() == "clear_history" else Risk.SAFE)),
        )
    )

    # APPLICATION INTEGRATIONS: YANDEX MUSIC + DISCORD
    def yandex_music_tool(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "status") or "status").strip().casefold()
        disabled_catalogue = not bool(config.get("integrations.yandex_music.enabled", True))
        local_allowed = getattr(yandex_adapter, "local_youtube_transport_allowed", None)
        if disabled_catalogue and not (callable(local_allowed) and local_allowed(action)):
            return ToolExecution(False, "Интеграция Яндекс Музыки не включена. Включите её в Настройки → Медиа → Яндекс Музыка.", {"integration_disabled": True})
        if disabled_catalogue and action == "status":
            action = "background_status"
        # Backward/LLM compatibility aliases. Older prompts and local models may
        # emit play_wave even though the stable adapter action is my_wave. Treat
        # these as the same semantic operation instead of failing after routing.
        action = {
            "play_wave": "my_wave", "wave": "my_wave", "mywave": "my_wave",
            "play_my_wave": "my_wave", "play_my_tracks": "my_tracks", "my_songs": "my_tracks",
            "play_song": "play_track", "play_music": "my_wave", "search_track": "search_track",
            "find_track": "search_track", "find_song": "search_track", "start_track": "play_track",
            "play_playlist": "playlist", "playlist_by_name": "playlist", "start_playlist": "playlist",
            "like_song": "like", "like_track": "like",
            "dislike_song": "dislike", "dislike_track": "dislike", "thumbs_down": "dislike",
            "unlike_song": "unlike", "unlike_track": "unlike",
        }.get(action, action)
        try:
            from jarvis.runtime.media_events import command_result_owns_media_notice
            with command_result_owns_media_notice():
                data = yandex_adapter.execute(
                    action,
                    query=str(args.get("query", "") or ""),
                    artist=str(args.get("artist", "") or ""),
                    volume=args.get("volume"), step=args.get("step"),
                    playlist=str(args.get("playlist", "") or ""),
                    queue=bool(args.get("queue", False)), next_up=bool(args.get("next_up", False)),
                )
        except Exception as exc:
            # Retained OAuth credentials are not evidence of an auth failure.
            # Expose a typed failure, never the internal RuntimeError/JSON dump.
            from jarvis.integrations.yandex_music import PlaylistAmbiguous, YandexMusicAdapter
            if isinstance(exc, PlaylistAmbiguous):
                return ToolExecution(False, str(exc), {"error_kind": "ambiguous", "needs_clarification": True,
                                                       "choices": list(exc.titles), "verified": False})
            kind = YandexMusicAdapter._yandex_error_kind(exc)
            logging.getLogger(__name__).warning("Music action %s failed (%s)", action, kind, exc_info=True)
            if kind == "api":
                kind = str(getattr(yandex_adapter, "_api_last_error_kind", "") or kind)
            if kind == "network":
                from jarvis.runtime.media_notice156 import network_notice
                return ToolExecution(False, network_notice(config.get("app.language", "ru")), {"error_kind": "network", "outcome_unknown": False, "verified": False})
            if kind == "auth":
                return ToolExecution(False, "Музыкальный сервис отклонил авторизацию. Подключите аккаунт заново в настройках.", {"error_kind": "auth", "verified": False})
            if re.search(r"не найден|не наш|не удалось найти|not found|no results", str(exc), re.I):
                return ToolExecution(False, "Не нашёл подходящий трек. Уточните название или исполнителя.", {"error_kind": "not_found", "query": str(args.get("query", "") or ""), "verified": False})
            if re.search(r"internal[ _-]?error|internal server error|traceback|[\"']error[\"']\s*:", str(exc), re.I):
                return ToolExecution(False, "Музыкальный сервис вернул ошибку. Попробуйте команду ещё раз немного позже.", {"error_kind": "service", "verified": False})
            return ToolExecution(False, str(exc), {"error_kind": kind, "verified": False})
        data = dict(data or {})
        if action == "search_track":
            from jarvis.runtime.dialogue_messages import message
            track = " — ".join(str(data.get(k, "")) for k in ("artist", "title") if data.get(k))
            return ToolExecution(bool(data.get("verified")), message("found_track", config.get("app.language", "ru"), track=track, provider="Yandex Music"), data)
        provenance = getattr(yandex_adapter, "playback_provenance", None)
        if callable(provenance):
            try:
                for key, value in provenance().items():
                    data.setdefault(key, value)
            except Exception:
                logging.getLogger(__name__).warning("Music result confirmed; provenance unavailable", exc_info=True)
        data.setdefault("transport_owner", "yandex_music")
        data.setdefault("content_provider", "youtube" if data.get("app") == "youtube_audio" or data.get("media_source") == "YouTube" else "yandex_music")
        data.setdefault("media_source", "YouTube" if data["content_provider"] == "youtube" else "Yandex Music")
        if action == "next" and isinstance(data, dict) and bool(data.get("already_advancing", False)):
            # Acknowledged/coalesced is pending, not a failed command to replay.
            data["outcome_pending"] = True
            return ToolExecution(True, "Переключение трека уже выполняется.", data)
        verified_actions = {"play_track", "youtube_url", "my_wave", "my_tracks", "playlist", "next", "previous", "pause", "play", "stop", "like", "unlike", "dislike", "shuffle_on", "shuffle_off", "repeat_on", "repeat_track", "repeat_playlist", "repeat_off"}
        if action in verified_actions and (not isinstance(data, dict) or not bool(data.get("verified", False))):
            return ToolExecution(
                False,
                "Я отправил команду Яндекс Музыке, но не получил подтверждение фактического состояния. Успех не засчитываю.",
                data if isinstance(data, dict) else {"result": data},
            )
        title = str(data.get("title", "") or "").strip() if isinstance(data, dict) else ""
        artist = str(data.get("artist", "") or "").strip() if isinstance(data, dict) else ""
        if action == "queue_status" and isinstance(data, dict) and not title:
            current_meta = dict(data.get("current") or {})
            player_meta = dict(data.get("player") or {})
            title = str(player_meta.get("title", "") or current_meta.get("title", "") or "").strip()
            artist = str(player_meta.get("artist", "") or current_meta.get("artist", "") or "").strip()
        now = " — ".join(x for x in (artist, title) if x)

        def _yandex_queue_suffix() -> str:
            try:
                qdata = data if action == "queue_status" and isinstance(data, dict) else yandex_adapter.queue_status(limit=5)
            except Exception:
                return ""
            rows = list(qdata.get("queue") or []) if isinstance(qdata, dict) else []
            if isinstance(data, dict):
                data["queue"] = rows[:5]
                data["queue_refilling"] = bool(qdata.get("queue_refilling", getattr(yandex_adapter, "_wave_refill_active", False)))
                data["queue_unavailable"] = bool(qdata.get("queue_unavailable", False))
                marker = getattr(yandex_adapter, "mark_queue_reported", None)
                if callable(marker):
                    ready = marker(str((qdata.get("current") or {}).get("id") or data.get("track_id") or ""), bool(rows), known_count=len(rows))
                    if isinstance(ready, list) and ready:
                        rows = ready[:5]
                        data["queue"] = rows
                        data["queue_unavailable"] = False
            if not rows:
                return "\nСледующие треки пока не получены." if data.get("queue_refilling") or data.get("queue_unavailable") else "\nОчередь: пуста."
            from jarvis.runtime.media_notice156 import queue_labels
            labels = queue_labels(rows, "ru", 5)
            return ("\nОчередь дальше:\n" + "\n".join(labels)) if labels else "\nОчередь: пуста."

        queue_suffix = _yandex_queue_suffix() if action in {"status", "background_status", "queue_status", "my_wave", "my_tracks", "playlist", "next", "previous", "play_track", "dislike"} else ""
        wave_message = (f"Включил Яндекс Музыку — «Моя волна». Сейчас играет: {now}." if now else "Включил Яндекс Музыку — «Моя волна».") + queue_suffix
        my_tracks_message = (f"Включил «Мои треки». Сейчас играет: {now}." if now else "Включил «Мои треки».") + queue_suffix
        playlist_name = str(data.get("playlist", "") or args.get("playlist", "") or args.get("query", "") or "").strip() if isinstance(data, dict) else str(args.get("playlist", "") or "").strip()
        playlist_message = (f"Включил плейлист «{playlist_name}». Сейчас играет: {now}." if now else f"Включил плейлист «{playlist_name}».") + queue_suffix
        if action == "youtube_url" and isinstance(data, dict):
            count = int(data.get("added_count", 1) or 1)
            kind = str(data.get("kind", "video") or "video")
            playlist_title = str(data.get("playlist_title", "") or "").strip()
            queued = bool(args.get("queue", False))
            started = bool(data.get("queue_started_immediately"))
            label = now or title or "YouTube-видео"
            if queued and not started:
                where = "следующей в очередь на компьютере" if bool(args.get("next_up", False)) else "в очередь на компьютере"
                if kind == "playlist":
                    name_part = f" «{playlist_title}»" if playlist_title else ""
                    text_out = f"Добавил YouTube-плейлист{name_part} {where}: {count} элементов."
                else:
                    text_out = f"Добавил YouTube-видео {where}: {label}."
            else:
                if kind == "playlist":
                    name_part = f" «{playlist_title}»" if playlist_title else ""
                    text_out = f"Включил YouTube-плейлист{name_part} на компьютере: {count} элементов."
                    if label:
                        text_out += f" Сейчас играет: {label}."
                else:
                    text_out = f"Включил аудиодорожку YouTube на компьютере: {label}."
            return ToolExecution(True, text_out, data)

        messages = {
            "open": "Открыл приложение Яндекс Музыка.",
            "my_wave": wave_message,
            "my_tracks": my_tracks_message,
            "playlist": playlist_message,
            "shuffle_on": "Включил перемешивание в Яндекс Музыке.",
            "shuffle_off": "Выключил перемешивание в Яндекс Музыке.",
            "repeat_on": "Включил повтор текущего трека в Яндекс Музыке.",
            "repeat_track": "Включил повтор текущего трека в Яндекс Музыке.",
            "repeat_playlist": "Включил повтор плейлиста в Яндекс Музыке.",
            "repeat_off": "Выключил повтор в Яндекс Музыке.",
            "like": "Поставил лайк текущему треку.",
            "unlike": "Убрал лайк с текущего трека.",
            "dislike": (f"Поставил дизлайк и включил следующий трек. Сейчас играет: {now}." if now else "Поставил дизлайк и включил следующий трек.") + queue_suffix,
            "set_volume": f"Установил громкость фоновой музыки NEO на {int(float(100 if args.get('volume') is None else args['volume']))}%.",
            "volume_up": "Сделал фоновую музыку NEO громче.",
            "volume_down": "Сделал фоновую музыку NEO тише.",
            "next": (f"Включил следующий трек. Сейчас играет: {now}." if now else "Включил следующий трек.") + queue_suffix,
            "previous": (f"Вернул предыдущий трек. Сейчас играет: {now}." if now else "Вернул предыдущий трек.") + queue_suffix,
            "pause": "Поставил Яндекс Музыку на паузу.",
            "play": "Продолжил воспроизведение в Яндекс Музыке.",
            "stop": "Остановил воспроизведение в Яндекс Музыке.",
            "status": f"Сейчас играет: {now or 'Яндекс Музыка'}" + queue_suffix,
            "background_status": f"Сейчас играет: {now or 'Яндекс Музыка'}" + queue_suffix,
            "queue_status": (f"Сейчас играет: {now}." if now else "Сейчас ничего не играет.") + queue_suffix,
            "queue_clear": "Очистил очередь Яндекс Музыки; текущий трек продолжает играть.",
            "play_track": (f"Включил: {now}." if now else f"Включил «{str(args.get('query','') or '').strip()}».") + queue_suffix,
            "api_status": "Проверил интеграцию API Яндекс Музыки.",
        }
        return ToolExecution(True, messages.get(action, "Команда Яндекс Музыки выполнена."), data)

    registry.register(
        ToolDefinition(
            "yandex_music",
            "Яндекс Музыка: ТОЛЬКО фоновый API + собственный NEO player. Desktop UI, pyautogui, GSMTC и UI fallback для Яндекс Музыки отключены.",
            _object_schema(
                {
                    "action": _enum(["status", "play", "pause", "stop", "next", "previous", "play_track", "play_song", "play_music", "search_track", "find_track", "find_song", "start_track", "my_wave", "my_tracks", "playlist", "play_playlist", "playlist_by_name", "start_playlist", "like", "unlike", "dislike", "shuffle_on", "shuffle_off", "repeat_on", "repeat_track", "repeat_playlist", "repeat_off", "set_volume", "volume_up", "volume_down", "api_status", "background_status", "queue_status", "queue_clear"], "Фоновая операция Яндекс Музыки"),
                    "query": {"type": "string", "description": "Название песни/трек для play_track"},
                    "artist": {"type": "string", "description": "Исполнитель, если пользователь его назвал"},
                    "playlist": {"type": "string", "description": "Название пользовательского плейлиста Яндекс Музыки"},
                    "volume": {"type": "number", "minimum": 0, "maximum": 100, "description": "Громкость скрытого плеера NEO в процентах"},
                    "step": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Шаг изменения громкости"},
                },
                ["action"],
            ),
            yandex_music_tool,
            lambda _: Risk.SAFE,
        )
    )

    def _simple_provider_execution(provider: str, adapter: Any, args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "status") or "status").strip().casefold()
        if not bool(config.get(f"integrations.{provider}.enabled", False)) and action not in {"open", "connect", "authorize"}:
            return ToolExecution(False, provider_disabled_message(provider, config.get("app.language", "ru")), {"integration_disabled": True, "provider": provider})
        aliases = {
            "play_song": "play_track", "play_music": "play", "search_track": "search_track",
            "find_track": "search_track", "find_song": "search_track", "start_track": "play_track",
            "resume": "play", "prev": "previous", "volume": "set_volume",
        }
        action = aliases.get(action, action)
        try:
            data = adapter.execute(
                action,
                query=str(args.get("query", "") or ""),
                playlist=str(args.get("playlist", "") or ""),
                volume=args.get("volume"), step=args.get("step"),
                position_ms=args.get("position_ms"), seconds=args.get("seconds"),
            )
        except Exception as exc:
            return ToolExecution(False, f"{provider_label(provider)}: {exc}", {"provider": provider, "error": str(exc)})
        data = dict(data or {}) if isinstance(data, dict) else {"result": data}
        data.setdefault("provider", provider)
        if action == "search_track":
            from jarvis.runtime.dialogue_messages import message
            track = " — ".join(str(data.get(k, "")) for k in ("artist", "title") if data.get(k))
            return ToolExecution(bool(data.get("verified")), message("found_track", config.get("app.language", "ru"), track=track, provider=provider_label(provider)), data)
        data.setdefault("content_provider", provider)
        data.setdefault("transport_owner", provider)
        if provider == "spotify" and action == "queue_status":
            from jarvis.runtime.spotify_queue157 import format_spotify_queue157
            from jarvis.language import language_code
            return ToolExecution(bool(data.get("verified")), format_spotify_queue157(data, language_code(config)), data)
        if action in {"connect", "authorize"} and data.get("settings_requested"):
            return ToolExecution(True, f"Запрос на открытие настроек {provider_label(provider)} передан окну NEO.", data)
        if action == "open" and data.get("opened") is False:
            return ToolExecution(False, f"Не удалось открыть веб-плеер {provider_label(provider)}.", data)
        title = str(data.get("title", "") or "").strip(); artist = str(data.get("artist", "") or "").strip()
        now = " — ".join(x for x in (artist, title) if x)
        label = provider_label(provider)
        messages = {
            "status": f"{label}: " + (f"сейчас играет {now}." if now else f"состояние {str(data.get('state','stopped') or 'stopped')}"),
            "background_status": f"{label}: " + (f"сейчас играет {now}." if now else f"состояние {str(data.get('state','stopped') or 'stopped')}"),
            "play_track": f"Включил в {label}: {now or str(args.get('query','') or '').strip()}.",
            "playlist": f"Включил плейлист «{str(args.get('playlist') or args.get('query') or '').strip()}» в {label}.",
            "play": f"Продолжил воспроизведение в {label}.",
            "pause": f"Поставил {label} на паузу.",
            "stop": f"Остановил {label}.",
            "next": f"Переключил {label} на следующий трек.",
            "previous": f"Вернул предыдущий трек в {label}.",
            "shuffle_on": f"Включил перемешивание в {label}.",
            "shuffle_off": f"Выключил перемешивание в {label}.",
            "repeat_on": f"Включил повтор текущего трека в {label}.",
            "repeat_track": f"Включил повтор текущего трека в {label}.",
            "repeat_playlist": f"Включил повтор плейлиста/контекста в {label}.",
            "repeat_off": f"Выключил повтор в {label}.",
            "set_volume": f"Установил громкость {label} на {int(float(100 if args.get('volume') is None else args['volume']))}%.",
            "volume_up": f"Сделал {label} громче.",
            "volume_down": f"Сделал {label} тише.",
            "queue_add": f"Добавил «{str(args.get('query','') or '').strip()}» в очередь {label}.",
            "queue_next": f"Добавил «{str(args.get('query','') or '').strip()}» в очередь {label}.",
            "like": f"Добавил текущий трек {label} в понравившиеся.",
            "unlike": f"Убрал текущий трек {label} из понравившихся.",
            "open": f"Открыт веб-плеер {label}.",
            "connect": f"Открыл подключение {label}.",
            "authorize": f"Открыл авторизацию {label}.",
        }
        return ToolExecution(True, messages.get(action, f"Команда {label} выполнена."), data)

    def spotify_tool(args: dict[str, Any]) -> ToolExecution:
        return _simple_provider_execution("spotify", spotify_adapter, args)

    provider_actions = ["status", "background_status", "queue_status", "play", "pause", "stop", "next", "previous", "play_track", "play_song", "play_music", "search_track", "find_track", "find_song", "start_track", "playlist", "shuffle_on", "shuffle_off", "repeat_on", "repeat_track", "repeat_playlist", "repeat_off", "set_volume", "volume_up", "volume_down", "queue_add", "queue_next", "like", "unlike", "seek", "open", "connect", "authorize"]
    provider_schema = _object_schema({
        "action": _enum(provider_actions, "Операция музыкального провайдера"),
        "query": {"type":"string","description":"Название трека / поисковая фраза"},
        "playlist": {"type":"string","description":"Название плейлиста"},
        "volume": {"type":"number","minimum":0,"maximum":100},
        "step": {"type":"integer","minimum":1,"maximum":50},
        "position_ms": {"type":"integer","minimum":0},
        "seconds": {"type":"number","minimum":0},
    }, ["action"])
    registry.register(ToolDefinition(
        "spotify",
        "Spotify через официальный Web API/Spotify Connect. Использовать только когда Spotify явно назван или выбран активным музыкальным провайдером.",
        provider_schema, spotify_tool, lambda _: Risk.SAFE,
    ))
    def music_tool(args: dict[str, Any]) -> ToolExecution:
        """Generic desktop music command routed to the one active provider."""
        provider = active_music_provider(config)
        from jarvis.runtime.media_activity import recent_music_owner
        local_allowed = getattr(yandex_adapter, "local_youtube_transport_allowed", None)
        owns_loaded = getattr(yandex_adapter, "owns_loaded_source", None)
        action = str(args.get("action", "status") or "status")
        owner = recent_music_owner(max_age_seconds=24 * 60 * 60)
        # MEDIA185-U04: a transport command belongs to the session that is
        # actually playing, not to whichever provider is configured as default.
        # An explicit Spotify start keeps Spotify; a loaded PC queue (Yandex or
        # local YouTube) keeps the PC player even when its catalogue is off. A
        # refusal of the real owner is reported, never re-sent elsewhere.
        transport = action in {"play", "pause", "stop", "next", "previous", "shuffle_on", "shuffle_off",
                               "repeat_on", "repeat_track", "repeat_playlist", "repeat_off", "queue_status",
                               "queue_clear", "status", "background_status", "set_volume", "volume_up", "volume_down", "seek"}
        if transport and owner == "spotify" and bool(config.get("integrations.spotify.enabled", False)):
            provider = "spotify"
        elif transport and owner == "yandex_music" and (
                (callable(owns_loaded) and owns_loaded()) or (callable(local_allowed) and local_allowed(action))):
            provider = "yandex_music"
        if not provider:
            return ToolExecution(False, "Музыкальный сервис не выбран. Включите Яндекс Музыку или Spotify в Настройки → Медиа.", {"integration_disabled": True, "provider": ""})
        if provider == "yandex_music":
            result = yandex_music_tool(args)
        elif provider == "spotify":
            result = spotify_tool(args)
        else:
            return ToolExecution(False, "Неизвестный активный музыкальный провайдер. Выберите Яндекс Музыку или Spotify.", {"provider": provider})
        if isinstance(result.data, dict):
            result.data.setdefault("provider", provider)
            result.data["active_provider"] = provider
        return result

    registry.register(ToolDefinition(
        "music",
        "Единый музыкальный tool. Всегда маршрутизирует команду только в один активный провайдер: Яндекс Музыка или Spotify, выбранный в Настройки → Медиа.",
        _object_schema({
            "action": _enum(["status","background_status","play","pause","stop","next","previous","play_track","play_song","play_music","search_track","find_track","find_song","start_track","playlist","my_wave","my_tracks","like","unlike","dislike","shuffle_on","shuffle_off","repeat_on","repeat_track","repeat_playlist","repeat_off","set_volume","volume_up","volume_down","queue_status","queue_clear","queue_add","queue_next","seek"], "Операция активного музыкального сервиса"),
            "query": {"type":"string"}, "artist": {"type":"string"}, "playlist": {"type":"string"},
            "volume": {"type":"number","minimum":0,"maximum":100}, "step": {"type":"integer","minimum":1,"maximum":50},
            "position_ms": {"type":"integer","minimum":0}, "seconds": {"type":"number","minimum":0},
        }, ["action"]),
        music_tool, lambda _: Risk.SAFE,
    ))

    def discord_tool(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "open") or "open").strip().casefold()
        if action == "user_account_automation_unavailable":
            return ToolExecution(
                False,
                "Автоматизация обычного Discord user-аккаунта отключена: официальный Discord API не разрешает self-bot/user-bot управление. NEO использует только официальный NeoBot для музыкального голосового транспорта и /play.",
                {"safe_failure": True, "official_bot_only": True, "user_token_supported": False},
            )
        if not bool(config.get("integrations.discord.enabled", True)):
            return ToolExecution(False, "Интеграция Discord не включена. Включите её в Настройки → Медиа → Discord и настройте официальный NeoBot.", {"integration_disabled": True})
        data = discord_adapter.execute(
            action,
            server=str(args.get("server", "") or ""),
            channel=str(args.get("channel", "") or ""),
            text=str(args.get("text", "") or ""),
            query=str(args.get("query", "") or ""),
            playlist=str(args.get("playlist", "") or ""),
            requester=str(args.get("requester", "") or ""),
            volume=args.get("volume"), step=args.get("step"),
            next_up=bool(args.get("next_up", False)),
            play_now=bool(args.get("play_now", False)),
        )
        if isinstance(data, dict) and data.get("cancelled"):
            return ToolExecution(False, "Предыдущая музыкальная команда заменена новым запросом.", data)
        server = str(data.get("server", "") or "") if isinstance(data, dict) else ""
        channel = str(data.get("channel", "") or "") if isinstance(data, dict) else ""
        title = str(data.get("title", "") or "").strip() if isinstance(data, dict) else ""
        artist = str(data.get("artist", "") or "").strip() if isinstance(data, dict) else ""
        current = data.get("current") if isinstance(data, dict) and isinstance(data.get("current"), dict) else {}
        if not title and current:
            title = str(current.get("title", "") or current.get("query", "") or "").strip()
        if not artist and current:
            artist = str(current.get("artist", "") or "").strip()

        def _clean_now_title(value: str) -> str:
            raw = str(value or "").strip()
            low = raw.casefold()
            if low in {"трек", "track", "unknown", "неизвестно", "метаданные загружаются"}:
                return ""
            if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27,}", low) or re.fullmatch(r"[0-9a-f]{24,64}", low):
                return ""
            if re.match(r"^https?://", low):
                return ""
            return raw

        title = _clean_now_title(title)
        now = " — ".join(x for x in (artist, title) if x)

        def _discord_queue_suffix(payload: dict[str, Any] | None = None) -> str:
            qdata = payload if isinstance(payload, dict) else {}
            rows = list(qdata.get("queue") or [])
            if not rows and action != "music_queue_clear":
                try:
                    qdata = discord_adapter.execute("music_queue_status", server=server)
                    rows = list(qdata.get("queue") or []) if isinstance(qdata, dict) else []
                except Exception:
                    rows = []
            if not rows:
                return "\nОчередь: пуста."
            from jarvis.runtime.media_notice156 import queue_labels
            labels = queue_labels(rows, "ru", 5)
            return ("\nОчередь дальше:\n" + "\n".join(labels)) if labels else "\nОчередь: пуста."

        discord_queue_suffix = _discord_queue_suffix(data if isinstance(data, dict) else {}) if action in {
            "music_bot_play", "music_my_tracks", "music_my_wave", "music_playlist", "music_queue_status",
            "music_skip", "music_previous", "music_resume", "music_dislike"
        } else ""
        messages = {
            "open": "Открыл Discord.",
            "open_server": f"Перешёл на сервер «{server or str(args.get('server',''))}».",
            "open_text_channel": f"Открыл текстовый канал «{channel or str(args.get('channel',''))}».",
            "join_voice_channel": f"Подключился к голосовому каналу «{channel or str(args.get('channel',''))}».",
            "leave_voice_channel": "Отключился от голосового канала Discord.",
            "send_message": "Отправил сообщение в Discord.",
            "list_channels": "Получил доступные Discord-каналы.",
            "connect_channel": f"NEO Bot подключён к {('голосовому' if str(data.get('channel_kind','')) == 'voice' else 'текстовому')} каналу «{str(data.get('channel', '') or channel or args.get('channel', '')).strip()}».",
            "attach_text_channel": f"NEO Bot подключён к текстовому каналу «{str(data.get('channel', '') or channel or args.get('channel', '')).strip()}».",
            "detach_text_channel": "NEO Bot отключён от выбранного текстового канала.",
            "text_channel_status": (f"NEO Bot подключён к текстовому каналу «{str(data.get('channel', '') or '').strip()}»." if bool(data.get('attached')) else "Текстовый канал Discord не выбран."),
            "music_bot_play": (f"Включил в Discord: {now}." if now else f"Включил в Discord «{str(args.get('query','') or '').strip()}».") + discord_queue_suffix,
            "music_my_tracks": (f"Включил ваши «Мои треки» в Discord. Сейчас играет: {now}." if now else "Включил ваши «Мои треки» в Discord.") + discord_queue_suffix,
            "music_my_wave": (f"Включил вашу «Мою волну» в Discord. Сейчас играет: {now}." if now else "Включил вашу «Мою волну» в Discord.") + discord_queue_suffix,
            "music_playlist": (f"Включил плейлист «{str(data.get('source_title','') or args.get('playlist','') or args.get('query','')).strip()}» в Discord. Сейчас играет: {now}." if now else f"Включил плейлист «{str(args.get('playlist','') or args.get('query','')).strip()}» в Discord.") + discord_queue_suffix,
            "music_queue_next": f"Следующей в Discord поставил «{str(args.get('query','') or '').strip()}».",
            "music_queue_add": f"Добавил «{str(args.get('query','') or '').strip()}» в очередь Discord.",
            "music_queue_status": (f"Сейчас играет: {now}." if now else "Сейчас ничего не играет в Discord.") + discord_queue_suffix,
            "music_queue_clear": "Очистил очередь музыки Discord; текущая песня продолжает играть.",
            "music_shuffle_on": "Включил перемешивание в Discord Music.",
            "music_shuffle_off": "Выключил перемешивание в Discord Music.",
            "music_repeat_track": "Включил повтор текущего трека в Discord Music.",
            "music_repeat_playlist": "Включил повтор плейлиста в Discord Music.",
            "music_repeat_off": "Выключил повтор в Discord Music.",
            "music_like": (f"Поставил лайк текущему Discord-треку в вашем аккаунте Яндекс Музыки: {now}." if now else "Поставил лайк текущему Discord-треку в вашем аккаунте Яндекс Музыки."),
            "music_dislike": (f"Поставил дизлайк текущему Discord-треку в вашем аккаунте Яндекс Музыки и переключил на: {now}." if now else "Поставил дизлайк текущему Discord-треку в вашем аккаунте Яндекс Музыки и переключил трек."),
            "music_skip": (f"Пропустил текущий трек. Сейчас играет: {now}." if now else (
                "Запрос на следующий трек принят: «Моя волна» ещё подбирает новые треки, следующий пока не начался."
                if isinstance(data, dict) and data.get("pending") and data.get("queue_refilling") else
                "Пропустил текущий трек. Очередь Discord пуста." if isinstance(data, dict) and data.get("queue_empty") else "Пропустил текущий трек Discord.")) + discord_queue_suffix,
            "music_previous": (f"Вернул предыдущий трек Discord. Сейчас играет: {now}." if now else "Вернул предыдущий трек Discord.") + discord_queue_suffix,
            "music_set_volume": f"Установил громкость Discord Music на {int(float(data['volume'] if data.get('volume') is not None else (100 if args.get('volume') is None else args['volume'])))}%.",
            "music_volume_up": f"Сделал Discord Music громче: {int(100 if data.get('volume') is None else data['volume'])}%.",
            "music_volume_down": f"Сделал Discord Music тише: {int(100 if data.get('volume') is None else data['volume'])}%.",
            "music_pause": f"Поставил музыку Discord на паузу. Сейчас играет: {now}." if now else "Поставил музыку Discord на паузу.",
            "music_resume": (f"Продолжил музыку Discord. Сейчас играет: {now}." if now else "Продолжил музыку Discord.") + discord_queue_suffix,
            "music_stop": f"Остановил музыку Discord: {now}." if now else "Остановил музыку Discord.",
            "bot_join_voice": "NEO Bot подключился к голосовому каналу.",
            "bot_leave_voice": "NEO Bot отключился от голосового канала.",
            "background_status": "Проверил состояние фонового Discord Bot.",
            "mute": "Выключил микрофон в Discord.",
            "unmute": "Включил микрофон в Discord.",
            "deafen": "Отключил звук Discord.",
            "undeafen": "Включил звук Discord.",
        }
        if action == "music_youtube_url" and isinstance(data, dict):
            verified = bool(data.get("verified")) and bool(data.get("submitted"))
            if args.get("play_now") is True:
                from jarvis.runtime.youtube_playlists184 import playback_confirmed
                verified = verified and playback_confirmed(data, discord=True)
            if not verified:
                return ToolExecution(False, "Discord не подтвердил добавление YouTube-ссылки.", data)
            kind = str(data.get("kind", "video") or "video")
            count = int(data.get("added_count", 1) or 1)
            started = bool(data.get("queue_started_immediately"))
            play_now = bool(data.get("play_now"))
            playlist_title = str(data.get("playlist_title", "") or "").strip()
            current_title = str(data.get("title", "") or "").strip()
            current_artist = str(data.get("artist", "") or "").strip()
            current_label = " — ".join(x for x in (current_artist, current_title) if x)
            if kind == "playlist":
                title_part = f" «{playlist_title}»" if playlist_title else ""
                if play_now:
                    text_out = f"Включил YouTube-плейлист{title_part} в Discord: {count} элементов."
                else:
                    text_out = f"Добавил YouTube-плейлист{title_part} в общую очередь Discord: {count} элементов."
                if started and current_label:
                    text_out += f" Сейчас играет: {current_label}."
                if bool(data.get("truncated")):
                    text_out += " Плейлист ограничен лимитом элементов NEO."
            else:
                if play_now or started:
                    text_out = f"Включил аудиодорожку YouTube в Discord: {current_label or 'видео'}."
                else:
                    text_out = f"Добавил YouTube-видео в общую очередь Discord: {current_label or 'видео'}."
            return ToolExecution(True, text_out, data)

        if action == "music_bot_play":
            verified = isinstance(data, dict) and bool(data.get("verified")) and bool(data.get("submitted"))
            if not verified:
                return ToolExecution(
                    False,
                    "Discord не подтвердил запуск музыки. Успех не засчитываю.",
                    data,
                )
        if action == "music_queue_status" and isinstance(data, dict):
            current = data.get("current") if isinstance(data.get("current"), dict) else {}
            queue = data.get("queue") if isinstance(data.get("queue"), list) else []
            source_title = str(data.get("source_title", "") or "").strip()

            def friendly(item: dict[str, Any]) -> str:
                title = str(item.get("title", "") or "").strip()
                artist = str(item.get("artist", "") or "").strip()
                source = str(item.get("media_source", "") or "").strip()
                low = title.casefold()
                internal = bool(
                    not title
                    or low in {"трек", "track", "unknown", "неизвестно", "метаданные загружаются"}
                    or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27,}", low)
                    or re.fullmatch(r"\d{5,}", low)
                    or re.fullmatch(r"[0-9a-f]{16,64}", low)
                    or re.match(r"^https?://", low)
                )
                if internal:
                    if source:
                        title = f"{source} · метаданные загружаются"
                    elif source_title:
                        title = f"{source_title} · следующий трек"
                    else:
                        title = "Следующий трек"
                return f"{title} — {artist}" if artist else title

            from jarvis.language import language_code
            code = language_code(config)
            headings = {
                'ru': ('Очередь Discord','Сейчас','Дальше','Недавно','Пусто'),
                'en': ('Discord queue','Now','Next','Recent','Empty'),
                'de': ('Discord-Warteschlange','Jetzt','Als Nächstes','Zuletzt','Leer'),
                'fr': ('File Discord','En cours','Ensuite','Historique récent','Vide'),
                'es': ('Cola Discord','Ahora','Siguiente','Reciente','Vacío'),
                'it': ('Coda Discord','Ora','Successivi','Recenti','Vuoto'),
                'pt': ('Fila Discord','Agora','A seguir','Recentes','Vazio'),
            }.get(code, ('Discord queue','Now','Next','Recent','Empty'))
            lines = [headings[0] + (f" · {source_title}" if source_title else "")]
            if current: lines.append(headings[1]+': '+friendly(current))
            for heading, entries in ((headings[2], queue[:5]), (headings[3], list(data.get('history') or [])[-5:])):
                lines.append(heading+':')
                if not entries: lines.append(headings[4])
                for index,item in enumerate(entries,1):
                    if isinstance(item,dict): lines.append(f"{index}. {friendly(item)}")
            return ToolExecution(True, "\n".join(lines), data)
        return ToolExecution(True, messages.get(action, "Команда Discord выполнена."), data)

    registry.register(
        ToolDefinition(
            "discord",
            "Discord: ТОЛЬКО официальный фоновый NEO Bot/API без foreground, мыши и клавиатуры: сообщения, voice music, очередь, pause/resume/skip/stop. Desktop Quick Switcher/UI Automation и user-token/self-bot отключены.",
            _object_schema(
                {
                    "action": _enum(["send_message", "user_account_automation_unavailable", "music_bot_play", "music_youtube_url", "music_my_tracks", "music_my_wave", "music_playlist", "music_queue_next", "music_queue_add", "music_queue_status", "music_queue_clear", "music_shuffle_on", "music_shuffle_off", "music_repeat_track", "music_repeat_playlist", "music_repeat_off", "music_like", "music_dislike", "music_skip", "music_previous", "music_set_volume", "music_volume_up", "music_volume_down", "music_pause", "music_resume", "music_stop", "bot_join_voice", "bot_leave_voice", "list_channels", "connect_channel", "attach_text_channel", "detach_text_channel", "text_channel_status", "background_status"], "Операция официального Discord Bot"),
                    "server": {"type": "string", "description": "Название сервера; можно оставить пустым для настроенного сервера по умолчанию"},
                    "channel": {"type": "string", "description": "Название текстового или голосового канала"},
                    "text": {"type": "string", "description": "Текст сообщения, который пользователь явно попросил отправить"},
                    "query": {"type": "string", "description": "Песня/запрос для NEO Bot или очереди Discord"},
                    "playlist": {"type": "string", "description": "Название плейлиста Яндекс Музыки для воспроизведения через Discord"},
                    "requester": {"type": "string", "description": "Необязательный идентификатор пользователя, добавившего элемент в общую очередь"},
                    "next_up": {"type": "boolean", "description": "Добавить трек следующим в общей очереди Discord"},
                    "play_now": {"type": "boolean", "description": "Начать воспроизведение сразу; используется для прямой YouTube-ссылки"},
                    "volume": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Громкость Discord Music"},
                    "step": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Шаг громкости Discord Music"},
                },
                ["action"],
            ),
            discord_tool,
            lambda _: Risk.SAFE,
        )
    )

    # MEDIA SESSIONS
    def _send_media_key(vk: int) -> None:
        if os.name != "nt":
            raise OSError("Системные медиаклавиши реализованы для Windows")
        user32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

    def _run_async(coro, *, timeout: float = 30.0):
        """Run one WinRT coroutine exactly once in an isolated thread.

        winsdk awaitables are single-use. RC6 retried the same coroutine after
        asyncio.run() raised, which produced "cannot reuse already awaited
        coroutine". A fresh thread owns one event loop and consumes it once.
        """
        import asyncio

        result: list[Any] = []
        errors: list[BaseException] = []

        def runner() -> None:
            try:
                result.append(asyncio.run(coro))
            except BaseException as exc:  # propagate the original WinRT error
                errors.append(exc)

        worker = threading.Thread(target=runner, name="jarvis-winrt", daemon=True)
        worker.start()
        worker.join(max(1.0, float(timeout)))
        if worker.is_alive():
            raise TimeoutError("Windows API did not respond in time")
        if errors:
            raise errors[0]
        return result[0] if result else None

    # Browser/global GSMTC correlation code from RC24 was intentionally removed
    # in v2.0. Music now has its own native-app controller and video has its own
    # YouTube/browser controller, so there is no shared media-session fallback.


    def media(args: dict[str, Any]) -> ToolExecution:
        """YouTube/browser media only. Music is never routed through this tool."""
        action = str(args.get("action", "status") or "status").strip().casefold()
        allowed_actions = {"status", "play_search", "play_pause", "play", "pause", "stop", "seek_forward", "seek_backward", "seek_to", "fullscreen", "theater", "next", "previous"}
        if action not in allowed_actions:
            raise ValueError(f"Неизвестное действие media: {action}")
        from jarvis.runtime.delayed_actions184 import current_binding, execute_bound_video
        delayed_target = current_binding('stop_video')
        if delayed_target is not None:
            if action != 'stop':
                raise ValueError('Invalid bound media action')
            data = execute_bound_video(delayed_target)
            return ToolExecution(data.get('verified') is True, 'Scheduled video pause', data)
        if action == "play_search":
            if args.get("local_audio") is True:
                from jarvis.integrations.youtube_audio import normalize_youtube_url
                clean = normalize_youtube_url(str(args.get("query", "") or ""))
                if not clean:
                    return ToolExecution(False, "Для локального аудио нужна прямая ссылка YouTube.", {"invalid_url": True, "local_audio": True})
                data = dict(yandex_adapter.execute("youtube_url", query=clean, queue=bool(args.get("queue", False)), next_up=bool(args.get("next_up", False))))
                data.update(local_audio=True, transport_owner="yandex_music", content_provider="youtube", media_source="YouTube", domain="local_music")
                queued = bool(data.get("queued") and not data.get("queue_started_immediately") and not data.get("play_now"))
                title = " — ".join(str(data.get(key, "") or "").strip() for key in ("artist", "title") if str(data.get(key, "") or "").strip())
                from jarvis.runtime.youtube_playlists184 import playback_confirmed
                if data.get("verified") is not True or (not queued and not playback_confirmed(data)):
                    return ToolExecution(False, "Результат локального воспроизведения пока не подтверждён.", data, outcome="unverified")
                elif queued:
                    message = "Добавил аудио YouTube в очередь локального плеера."
                else:
                    message = "Включил аудио YouTube в локальном плеере" + (": " + title if title else "") + "."
                    from jarvis.runtime.media_activity import note_media_observation
                    note_media_observation("yandex_music", playing=True)
                return ToolExecution(True, message, data)
            if args.get("queue") or args.get("next_up"):
                return ToolExecution(False, "Очередь YouTube доступна для локального аудио. Уточните, что нужно добавить музыку в локальную очередь.", {"requires_local_audio": True})
            if not bool(config.get("integrations.youtube.enabled", True)):
                return ToolExecution(False, "Поиск YouTube отключён. Пауза/продолжение видео работают независимо от YouTube Data API.", {"integration_disabled": True, "domain": "video_browser"})
            query = str(args.get("query", "") or args.get("title", "") or args.get("target", "") or "").strip()
            author = str(args.get("author", "") or "").strip()
            if not query:
                raise ValueError("Не указано название видео YouTube")
            try:
                found = youtube_adapter.search_video(query, author=author)
                browser_name = _open_in_preferred_browser(str(found.get("url", "") or ""), str(args.get("browser", "") or ""))
                return ToolExecution(True, f"Открыл в YouTube: {str(found.get('title','') or query)}.", {**found, "browser": browser_name, "domain_locked": True, "domain": "video_browser"})
            except Exception as exc:
                from jarvis.runtime.diagnostics import safe_error
                return ToolExecution(False, f"Не удалось найти видео через YouTube Data API: {safe_error(exc)}", {"domain_locked": True, "domain": "video_browser", "error": safe_error(exc)})
        raw_seconds = args.get("seconds", 10.0)
        seconds = max(0.0, min(24 * 3600.0, float(raw_seconds if raw_seconds is not None else 10.0)))
        if action in {"seek_forward", "seek_backward"}:
            seconds = max(1.0, seconds)
        if action == "seek_to":
            seconds = max(0.0, float(args.get("position_seconds", seconds) or 0.0))
        raw_target = str(args.get("target", "") or "").strip()
        target_norm = canonical_service(raw_target)
        tab_index = 0
        preferred_browser = _existing_browser_scope(str(args.get("browser", "") or ""))
        is_video = target_norm in {"video", "youtube"} or normalize_target(raw_target) in {"video", "youtube", "ютуб"} or action == "seek_to"
        if not is_video:
            return ToolExecution(False, "Media tool управляет только YouTube/видео. Для музыки используется активный провайдер Яндекс/Spotify; Discord управляется отдельно.", {"requires_domain": True, "api_only_media": True})
        if action in {"next", "previous"}:
            # NEO 187 HREQ-026: another video, not a seek. Verified by a changed
            # title; an unavailable previous video is reported, not faked.
            from jarvis.browser_control import MediaVerificationError, VideoNavigationUnavailable
            from jarvis.language import language_code
            from jarvis.runtime.video_nav187 import text as nav_text
            code = language_code(config)
            base = {"domain_locked": True, "domain": "video_browser", "localized187": True, "action": action}
            try:
                data = control_service_media(action, "youtube", preferred_browser, seconds=seconds)
            except VideoNavigationUnavailable:
                return ToolExecution(False, nav_text(action + "_unavailable", code), {**base, "unavailable": True}, outcome="user_state")
            except MediaVerificationError:
                return ToolExecution(False, nav_text("unverified", code), {**base, "verified": False, "command_sent": True}, outcome="unverified")
            except Exception as exc:
                logging.getLogger(__name__).debug("YouTube navigation failed", exc_info=True)
                return ToolExecution(False, nav_text("no_session", code), {**base, "error": str(exc)[:200]})
            if data.get("verified") is not True:
                return ToolExecution(False, nav_text("unverified", code), {**base, **data, "localized187": True}, outcome="unverified")
            return ToolExecution(True, nav_text(action + "_done", code, title=data.get("title", "")), {**data, **base})
        try:
            data = control_service_media(action, "youtube", preferred_browser, seconds=seconds)
            if action in {"play", "pause", "seek_forward", "seek_backward", "seek_to", "fullscreen", "theater"} and not bool(data.get("verified")):
                raise RuntimeError("не получено подтверждение целевой YouTube-команды")
            messages = {
                "play": "Продолжил воспроизведение видео в YouTube через фоновую Windows media session.",
                "pause": "Поставил видео YouTube на паузу через фоновую Windows media session.",
                "seek_forward": f"Перемотал видео YouTube вперёд на {int(round(seconds))} секунд.",
                "seek_backward": f"Перемотал видео YouTube назад на {int(round(seconds))} секунд.",
                "seek_to": f"Перешёл в YouTube к {int(seconds)//60}:{int(seconds)%60:02d}.",
                "fullscreen": "Переключил полноэкранный режим YouTube.",
                "theater": "Переключил театральный режим YouTube.",
            }
            return ToolExecution(True, messages.get(action, f"Видео-команда {action} выполнена в YouTube"), {**data, "domain_locked": True, "domain": "video_browser"})
        except Exception as exc:
            return ToolExecution(False, f"Не удалось выполнить команду именно в YouTube: {exc}. Discord и активный музыкальный сервис не трогаю.", {"domain_locked": True, "domain": "video_browser", "browser": preferred_browser, "tab_index": tab_index})


    registry.register(
        ToolDefinition(
            "media",
            "YouTube/browser media only. YouTube Data API выполняет поиск; транспорт воспроизведения использует фоновые проверяемые Play/Pause/Seek через Windows media session; полноэкранный и театральный режим выполняются только через UI Automation точной YouTube-вкладки. Слепые координаты и глобальные клавиши не используются. Музыка и Discord изолированы.",
            _object_schema(
                {
                    "action": _enum(["status", "play_search", "play_pause", "play", "pause", "stop", "seek_forward", "seek_backward", "seek_to", "fullscreen", "theater", "next", "previous"], "Операция YouTube; next/previous — другое видео, а не перемотка"),
                    "query": {"type": "string", "description": "Название видео или YouTube URL для play_search"},
                    "title": {"type": "string", "description": "Название видео для поиска"},
                    "author": {"type": "string", "description": "Необязательный автор/канал для поиска"},
                    "local_audio": {"type": "boolean", "description": "Явный запрос аудио по прямой ссылке YouTube в управляемом локальном плеере. Обычное видео остаётся в браузере."},
                    "queue": {"type": "boolean", "description": "Добавить в локальную аудиоочередь; требует local_audio=true."},
                    "next_up": {"type": "boolean", "description": "Добавить следующим в локальную аудиоочередь; требует local_audio=true и queue=true."},
                    "seconds": {"type": "number", "minimum": 0, "maximum": 86400, "description": "Секунды для относительной перемотки или служебное значение"},
                    "position_seconds": {"type": "number", "minimum": 0, "maximum": 86400, "description": "Абсолютная позиция видео в секундах"},
                    "target": {"type": "string", "description": "Только video/YouTube."},
                    "browser": {"type": "string", "description": "Необязательный ЯВНО названный браузер. Если не указан — использовать активный/уже открытый браузер."},
                },
                ["action"],
            ),
            media,
            lambda _: Risk.SAFE,
        )
    )

    return _send_media_key, _run_async
