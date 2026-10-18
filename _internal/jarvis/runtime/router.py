# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

from jarvis.runtime import cached_re as re
from dataclasses import dataclass, field
from typing import Any

from jarvis.runtime.media_phrases import parse_explicit_media_phrase
from jarvis.integrations.google_calendar import parse_calendar_add_command
from jarvis.runtime.integration_phrases import parse_integration_phrase
from jarvis.runtime.domains import classify_domain
from jarvis.runtime.semantics import (
    explicit_video, explicit_discord_media, explicit_yandex_music, pause_like, resume_like, looks_like_browser_search,
    strip_browser_search_scaffolding, extract_explicit_browser_url, normalize_entity_phrase, monitor_number,
    explicit_window_move, norm as semantic_norm,
)


@dataclass(frozen=True)
class RoutedIntent:
    intent: str
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    confidence: float = 1.0
    negative_rules: tuple[str, ...] = ()

    @property
    def domain(self) -> str:
        return classify_domain(self.tool, intent=self.intent, arguments=self.arguments).domain


def norm(text: str) -> str:
    return semantic_norm(text)


def _ordinal_value(value: str) -> int | None:
    return monitor_number(value)


class HybridRouter:
    """Priority deterministic router for obvious commands.

    It intentionally handles only commands where the target/action is sufficiently
    explicit.  Everything else falls through to the existing Agent and finally the
    local LLM.  Negative rules are encoded beside each high-priority intent.
    """

    def __init__(self) -> None:
        self.priority_table = {
            "interrupt": 1000,
            "pause_current_media": 950,
            "resume_current_media": 945,
            "move_window": 900,
            "browser_tab": 860,
            "scenario": 850,
            "clipboard": 820,
            "power": 800,
            "hardware": 760,
            "email": 740,
            "screen_help": 700,
            "filesystem_search": 650,
            "audio_session": 780,
            "browser_advanced": 855,
            "history": 840,
            "text_edit": 730,
            "screenshot": 720,
            "application": 620,
        }

    def route(self, text: str, *, scenario_phrases: list[str] | None = None) -> RoutedIntent | None:
        t = norm(text)
        if not t:
            return None
        # Strong media-domain markers are computed once and reused by every
        # deterministic branch.  This prevents a phrase such as "очередь
        # музыкального бота" from ever being mistaken for Yandex Music merely
        # because it contains the word "музыкального".
        discord_media_explicit = explicit_discord_media(t)
        yandex_media_explicit = explicit_yandex_music(t)
        youtube_media_explicit = explicit_video(t)

        # Interrupt is a control-plane command, not a media stop/search request.
        if re.fullmatch(r"(?:(?:джарвис|нео|нио|neo)\s+)?(?:стоп|отмена|не надо|прерви|прекрати|cancel|stop)", t):
            return RoutedIntent("interrupt", "automation", {"action": "cancel"}, self.priority_table["interrupt"], 1.0,
                                ("не media.stop с объектом", "не stop_process"))

        # Deictic visual/UI questions are screen-analysis commands, not text
        # translation.  ``что означает это окно`` used to match the translation
        # grammar solely because of ``что означает``.
        if re.search(r"\b(?:что\s+означает\s+это\s+окно|что\s+значит\s+это\s+окно|что\s+здесь\s+нужно\s+нажать|что\s+тут\s+нужно\s+нажать|почему\s+кнопка\s+неактивна|помоги\s+по\s+экрану|посоветуй\s+по\s+экрану)\b", t, re.I):
            return RoutedIntent(
                "screen_help", "screen", {"action": "analyze", "instruction": text},
                992, 0.999, ("deictic UI shell", "не translation"),
            )

        # User-defined scenario phrases are exact contracts and therefore own
        # their literal command before any topic parser. A scenario named
        # "Погода" or "RAM" must not become Weather/System Monitor. Interrupt
        # above remains the only higher-priority control-plane command.
        phrases = [str(x).strip() for x in (scenario_phrases or []) if norm(x)]
        literal = [x for x in phrases if x.casefold() == str(text).strip().casefold()]
        matches = literal or [x for x in phrases if norm(x) == t]
        if matches:
            # Retain original spelling/punctuation. If normalization collapses
            # several names, leave the original input for ownership validation.
            owners = {x.casefold(): x for x in matches}
            name = next(iter(owners.values())) if len(owners) == 1 else str(text).strip()
            return RoutedIntent("scenario", "scenario", {"action": "run", "name": name}, 990, 1.0, ("user scenario identity requires unique lookup",))

        # Domain-shell arbitration -------------------------------------------------
        # The *syntactic shell* of a command owns arbitrary payload words.  A song
        # titled "Погода", a file named RAM.txt or a phrase being translated may
        # legitimately contain vocabulary from another domain.  Resolve the
        # container/action before topic integrations to keep domains isolated.
        parsed_integration = parse_integration_phrase(t)
        translation_shell = bool(
            parsed_integration is not None
            and parsed_integration.tool == "translate"
            and (
                re.search(r"^(?:переведи|перевести|переводится|translate|как\s+(?:это\s+)?переводится|что\s+(?:это\s+)?(?:означает|значит)|как\s+сказать)\b", t, re.I)
                or re.search(r"\b(?:переведи\s+(?:это|её|его)|как\s+это\s+переводится|что\s+это\s+(?:означает|значит))\s*$", t, re.I)
            )
        )
        if translation_shell:
            return RoutedIntent(
                parsed_integration.intent, parsed_integration.tool,
                dict(parsed_integration.arguments), parsed_integration.priority,
                parsed_integration.confidence,
                ("translation shell owns arbitrary payload", "domain locked before payload parsing"),
            )

        # Explicit URL navigation owns the address before application/file/web-search
        # fallbacks. This also accepts speech/Telegram forms such as
        # "github.com открой" and a bare domain without http(s)://.
        explicit_url = extract_explicit_browser_url(text)
        if explicit_url:
            return RoutedIntent(
                "browser_open_url", "browser", {"action": "open_url", "value": explicit_url},
                988, 1.0,
                ("explicit URL navigation owns address", "не browser.search", "не desktop/application/file"),
            )

        # Only an explicit request to search *in a browser* opens browser UI.
        # V7.7 routes "загугли" / "найди в интернете" through SearchAPI
        # before this router, so those phrases return an answer inside NEO.
        if looks_like_browser_search(t):
            query = strip_browser_search_scaffolding(t)
            if query:
                return RoutedIntent(
                    "browser_search", "browser", {"action": "search", "value": query},
                    985, 0.999,
                    ("explicit web lookup owns payload topic", "не weather/tool topic parser"),
                )

        # Explicit media syntax owns the song/query payload.  Run it before
        # weather/timer/system topic parsers so tracks named "Погода" or "Timer"
        # remain media. Translation above is the intentional meta-command
        # exception because its payload may itself be a media command.
        parsed_media = parse_explicit_media_phrase(t)
        if parsed_media is not None:
            media_args: dict[str, Any] = {"action": parsed_media.action}
            if parsed_media.tool == "media":
                media_args["target"] = "youtube"
                if parsed_media.action in {"seek_forward", "seek_backward"} and parsed_media.query:
                    try:
                        media_args["seconds"] = float(parsed_media.query)
                    except (TypeError, ValueError):
                        pass
                elif parsed_media.action == "seek_to" and parsed_media.query:
                    try:
                        media_args["position_seconds"] = float(parsed_media.query)
                    except (TypeError, ValueError):
                        pass
                elif parsed_media.action == "set_speed" and parsed_media.query:
                    try:
                        media_args["speed"] = float(parsed_media.query)
                    except (TypeError, ValueError):
                        pass
                elif parsed_media.action == "set_quality" and parsed_media.query:
                    media_args["quality"] = parsed_media.query
                elif parsed_media.action == "play_search" and parsed_media.query:
                    media_args["query"] = parsed_media.query
            elif parsed_media.query:
                media_args["query"] = parsed_media.query
            return RoutedIntent(
                parsed_media.intent, parsed_media.tool, media_args,
                parsed_media.priority, parsed_media.confidence,
                ("v5.3 media shell owns arbitrary track payload", "domain locked before topic integrations"),
            )

        # Explicit file/folder opens are another payload shell. Resolve them
        # before hardware/weather topic parsers so names such as RAM.txt or
        # Погода.txt remain literal filesystem objects.
        file_open = re.match(r"^(?:открой|покажи|запусти)\w*\s+(?:файл|документ)\w*\s+(.+)$", t, re.I)
        if file_open:
            query = file_open.group(1).strip(' "«».,;')
            if query:
                return RoutedIntent(
                    "desktop_file_open", "desktop", {"action": "open", "query": query, "hint": "file"},
                    988, 0.999, ("literal file shell owns payload", "не hardware/weather/application"),
                )
        folder_open = re.match(r"^(?:открой|покажи|запусти)\w*\s+(?:папк|директор)\w*\s+(.+)$", t, re.I)
        if folder_open:
            query = folder_open.group(1).strip(' "«».,;')
            if query:
                return RoutedIntent(
                    "desktop_folder_open", "desktop", {"action": "open", "query": query, "hint": "folder"},
                    988, 0.999, ("literal folder shell owns payload", "не topic integration/application"),
                )

        # Explicit file/folder/application/audio containers similarly suppress
        # topic integrations. The mature downstream routes/legacy compatibility
        # layer still executes them; this flag only prevents e.g. RAM.txt from
        # becoming a system-monitor request.
        external_payload_shell = bool(
            re.search(r"\b(?:открой\w*|закрой\w*|запусти\w*|найди\w*|поищи\w*|отыщи\w*|покажи\w*|переключи\w*|поставь\w*|выбери\w*|перемести\w*|создай\w*|отправь\w*)\b", t, re.I)
            and re.search(
                r"\b(?:файл\w*|документ\w*|папк\w*|директор\w*|приложени\w*|программ\w*|"
                r"окн\w*|вкладк\w*|буфер\w*|письм\w*|черновик\w*|почт\w*|gmail|"
                r"discord\w*|дискорд\w*|сообщени\w*|сервер\w*|канал\w*|"
                r"микрофон\w*|устройств\w*\s+(?:ввода|вывода)|аудиовход\w*|аудиовыход\w*)\b",
                t, re.I,
            )
        )

        # API/productivity/topic grammar.  Only use it when a stronger external
        # container did not already establish ownership.
        if parsed_integration is not None and not external_payload_shell:
            return RoutedIntent(
                parsed_integration.intent, parsed_integration.tool,
                dict(parsed_integration.arguments), parsed_integration.priority,
                parsed_integration.confidence,
                ("HOTFIX23 integrations grammar", "domain locked before LLM"),
            )

        # Explicit video desired-state has the highest semantic priority after
        # cancellation. The pause detector deliberately tolerates a narrow ASR slip
        # such as «паулу» only because the utterance already contains an explicit
        # video object. This prevents stale Discord/Yandex context from stealing it.
        video_tab_navigation = bool(
            re.search(r"\bвкладк\w*\b", t)
            and re.search(r"\b(?:перейди|вернись|открой|закрой|активируй|переключись|переключи)\b", t)
            and not pause_like(t, strong_domain=True)
            and not resume_like(t)
        )
        if explicit_video(t) and not video_tab_navigation:
            if pause_like(t, strong_domain=True):
                return RoutedIntent("pause_current_media", "media", {"action": "pause", "target": "youtube"}, 950, 1.0,
                                    ("никогда не search_track", "никогда не yandex_music.play_track", "никогда не discord.music_bot_play"))
            # NEO 187 HREQ-026: «вернись к предыдущему видео» names another
            # video; it is checked before the resume verbs («верни…»).
            if re.search(r"\b(?:предыдущ\w*|прошл\w*\s+(?:видео|ролик\w*)|назад\s+(?:видео|ролик))\b", t):
                return RoutedIntent("youtube_previous", "media", {"action": "previous", "target": "youtube"}, 944, 1.0,
                                    ("только YouTube", "не Discord/Yandex"))
            if re.search(r"\b(?:следующ\w*|пропусти|скипни)\b", t):
                return RoutedIntent("youtube_next", "media", {"action": "next", "target": "youtube"}, 944, 1.0,
                                    ("только YouTube", "не Discord/Yandex"))
            if resume_like(t) or re.search(r"\b(?:запуст\w*|верни\w*)\b", t):
                return RoutedIntent("resume_current_media", "media", {"action": "play", "target": "youtube"}, 945, 1.0,
                                    ("не toggle если указан desired state", "не music_bot_play"))
            if re.search(r"\bдальше\b", t):
                return RoutedIntent("youtube_next", "media", {"action": "next", "target": "youtube"}, 944, 1.0,
                                    ("только YouTube", "не Discord/Yandex"))

        # API-only personal library. "Мои треки/мои песни" is an account-level
        # Yandex Music concept and must never fall into generic media/YouTube.
        # It is safe even without the words "Яндекс Музыка" because Discord and
        # YouTube are explicitly excluded and neither owns a personal "Мои треки"
        # library in this assistant.
        if (
            re.search(r"\b(?:включи|запусти|воспроизведи|поставь|открой)\w*\s+(?:мои|мою)\s+(?:треки|песни|музыку)\b", t)
            or (yandex_media_explicit and re.search(r"\b(?:мои|мою)\s+(?:треки|песни|музыку)\b", t))
        ) and not discord_media_explicit and not youtube_media_explicit:
            return RoutedIntent("yandex_my_tracks", "yandex_music", {"action": "my_tracks"}, 957, 0.999,
                                ("API liked-tracks queue", "не generic media", "не Discord/YouTube"))

        # High-confidence Yandex Music shortcuts. Keep these above generic app
        # routing so a compound clause such as «и включи волну» stays in the
        # music-app domain rather than being interpreted as an application name.
        if (
            (re.search(r"\b(?:моя\s+волна|мою\s+волну|включи\s+(?:мне\s+)?волну|запусти\s+(?:мне\s+)?волну)\b", t)
             or (yandex_media_explicit and re.search(r"\bволн\w*\b", t)))
            and not discord_media_explicit and not youtube_media_explicit
        ):
            return RoutedIntent("yandex_wave", "yandex_music", {"action": "my_wave"}, 956, 0.999,
                                ("именно user:onyourwave", "не мои треки", "не Discord/YouTube"))

        # Queue shuffle is an explicit desired-state command. Resolve it before
        # named-track and STOP routing because «выключи ... перемешивание» contains
        # the generic stop verb but must only change queue ordering.
        if yandex_media_explicit and not discord_media_explicit and not youtube_media_explicit and re.search(r"\b(?:перемешиван\w*|шаффл|shuffle)\b", t):
            if re.search(r"\b(?:включи|активируй|запусти|включить)\w*\b", t):
                return RoutedIntent("yandex_music_shuffle_on", "yandex_music", {"action": "shuffle_on"}, 959, 0.999,
                                    ("explicit Yandex", "background queue shuffle"))
            if re.search(r"\b(?:выключи|отключи|убери|выключить)\w*\b", t):
                return RoutedIntent("yandex_music_shuffle_off", "yandex_music", {"action": "shuffle_off"}, 959, 0.999,
                                    ("explicit Yandex", "background queue shuffle"))

        # Headless Yandex/JARVIS-player volume and account actions. These are
        # deliberately separate from Windows/system volume so a game can keep its
        # own loudness unchanged.
        if not discord_media_explicit and not youtube_media_explicit:
            music_words = bool(re.search(r"\b(?:музык\w*|трек\w*|песн\w*|яндекс(?:\s+музык\w*)?|yandex(?:\s+music)?)\b", t))
            volume_match = re.search(r"(?:громкост\w*(?:\s+(?:музык\w*|плеер\w*|яндекс\w*))?[^0-9]{0,16})(\d{1,3})\s*(?:%|процент\w*)?", t)
            if volume_match and music_words:
                value = max(0, min(100, int(volume_match.group(1))))
                return RoutedIntent("music_volume", "music", {"action": "set_volume", "volume": value}, 946, 0.995,
                                    ("не system volume", "hidden-player only"))
            if music_words and re.search(r"\b(?:громче|погромче|увеличь\s+громкост\w*)\b", t):
                return RoutedIntent("music_volume_up", "music", {"action": "volume_up", "step": 10}, 946, 0.995,
                                    ("не system volume", "hidden-player only"))
            if music_words and re.search(r"\b(?:тише|потише|уменьши\s+громкост\w*)\b", t):
                return RoutedIntent("music_volume_down", "music", {"action": "volume_down", "step": 10}, 946, 0.995,
                                    ("не system volume", "hidden-player only"))
            if re.search(r"\b(?:дизлайк(?:ни)?|дислайк(?:ни)?|dislike|мне\s+не\s+нравится)\b", t) and (music_words or re.search(r"\b(?:текущ|эт)\w*\b", t)):
                return RoutedIntent("music_dislike", "music", {"action": "dislike"}, 948, 0.997,
                                    ("current Yandex track", "negative preference + next"))
            if re.search(r"\b(?:убери|сними|удали)\w*\s+(?:лайк|из\s+(?:любим\w*|мне\s+нравится))\b", t) and (music_words or re.search(r"\bтекущ\w*\b", t)):
                return RoutedIntent("music_unlike", "music", {"action": "unlike"}, 946, 0.995,
                                    ("не social like", "current Yandex track"))
            like_phrase = bool(
                re.search(r"\b(?:поставь|добавь)\w*.*\b(?:лайк|любим\w*|мне\s+нравится)\b", t)
                or re.search(r"\b(?:лайкни|лайкнуть|залайкай|залайкать)\w*\b", t)
                or re.search(r"\b(?:мне\s+нравится|нравится)\s+(?:эт\w*\s+)?(?:трек|песн|музык)\w*\b", t)
            )
            if like_phrase and (music_words or re.search(r"\b(?:текущ|эт)\w*\b", t)):
                return RoutedIntent("music_like", "music", {"action": "like"}, 946, 0.995,
                                    ("не social like", "current Yandex track"))

        # Explicit named-track requests in the Yandex domain.  Strip service
        # scaffolding here so the provider receives only the song/artist payload.
        # The adapter repeats this sanitation defensively for LLM/tool fallbacks.
        if yandex_media_explicit and not discord_media_explicit and not youtube_media_explicit:
            yandex_named_patterns = (
                r"^(?:в|через)\s+(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s+(?:включи|поставь|запусти|вруби|воспроизведи|сыграй)\w*\s+(?:песн\w*|трек\w*|композиц\w*)?\s*(.+)$",
                r"^(?:включи|поставь|запусти|вруби|воспроизведи|сыграй)\w*\s+(?:песн\w*|трек\w*|композиц\w*)?\s*(.+?)\s+(?:в|через)\s+(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s*$",
                r"^(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s+(?:включи|поставь|запусти|вруби|воспроизведи|сыграй)\w*\s+(?:песн\w*|трек\w*|композиц\w*)?\s*(.+)$",
            )
            transportish = bool(
                pause_like(t, strong_domain=True) or resume_like(t)
                or re.search(r"\b(?:следующ\w*|предыдущ\w*|дальше|пропусти|скипни|останови|стопни|прекрати|выключи)\w*\b", t)
            )
            for pattern in yandex_named_patterns:
                m_yandex = re.search(pattern, t, flags=re.I)
                if m_yandex and not transportish:
                    query = m_yandex.group(1).strip(" \t\n\r«»\"'")
                    if query and not re.fullmatch(r"(?:музык\w*|песн\w*|трек\w*)", query):
                        return RoutedIntent("yandex_music_play_named", "yandex_music", {"action": "play_track", "query": query}, 955, 0.999,
                                            ("explicit Yandex", "payload without service scaffolding", "не Discord/YouTube"))

        # Desired-state controls for JARVIS/Yandex background audio. These must
        # reach YandexMusicAdapter instead of the generic Windows media-session
        # controller because the hidden Qt player intentionally has no GSMTC
        # session. Discord wording is excluded and handled by its own bot rules.
        if yandex_media_explicit and not discord_media_explicit and not youtube_media_explicit:
            music_context = True
            if music_context:
                if re.search(r"\b(?:останови|стопни|прекрати|выключи)\w*\b", t):
                    return RoutedIntent("yandex_music_stop", "yandex_music", {"action": "stop"}, 944, 0.995,
                                        ("не Discord", "не YouTube", "hidden-player aware"))
                if re.search(r"\b(?:следующ\w*|дальше|скипни|пропусти)\b", t):
                    return RoutedIntent("yandex_music_next", "yandex_music", {"action": "next"}, 944, 0.995,
                                        ("не Discord", "не YouTube", "hidden-player aware"))
                if re.search(r"\b(?:предыдущ\w*|назад\s+трек\w*)\b", t):
                    return RoutedIntent("yandex_music_previous", "yandex_music", {"action": "previous"}, 944, 0.995,
                                        ("не Discord", "не YouTube", "hidden-player aware"))
                if pause_like(t, strong_domain=True):
                    return RoutedIntent("yandex_music_pause", "yandex_music", {"action": "pause"}, 943, 0.995,
                                        ("не Discord", "не YouTube", "hidden-player aware"))
                if resume_like(t) or re.search(r"\b(?:продолжи|возобнови)\w*\b", t):
                    return RoutedIntent("yandex_music_resume", "yandex_music", {"action": "play"}, 942, 0.995,
                                        ("не Discord", "не YouTube", "hidden-player aware"))

        # HOTFIX125: a bare «включи музыку» is a provider-level start request,
        # not a named-track search with an empty payload. The generic music tool
        # forwards play_music to the selected provider; Yandex maps it to My Wave
        # while Spotify resumes its current playback context. Keep Discord/YouTube
        # out unless the user names them explicitly.
        if (
            re.fullmatch(r"(?:включи|запусти|вруби|воспроизведи|поставь)\s+музык(?:у|а)", t, flags=re.I)
            and not discord_media_explicit and not youtube_media_explicit
        ):
            return RoutedIntent("music_play_generic", "music", {"action": "play_music"}, 946, 0.999,
                                ("без пустого track query", "Yandex -> My Wave", "Spotify -> resume"))

        # Explicit song/track lookup belongs to the desktop music application unless
        # the user explicitly requested a browser/web search. This rule executes
        # before stale integration context can influence the legacy parser.
        song = re.match(
            r"^(?:найди(?:те)?|поищи(?:те)?|отыщи(?:те)?)\s+(?:в\s+(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s+)?"
            r"(?:песн\w*|трек\w*|композиц\w*|музык\w*)\s+(.+?)(?:\s+и\s+(?:включи|запусти|воспроизведи|поставь|вруби)(?:\s+(?:ее|её|его))?)?$",
            t, flags=re.I,
        )
        if song and not looks_like_browser_search(t):
            query = re.sub(r"\s+(?:в|через)\s+(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s*$", "", song.group(1), flags=re.I).strip(" ,.-")
            if query and not pause_like(query):
                play_requested = bool(re.search(r"\s+и\s+(?:включи|запусти|воспроизведи|поставь|вруби)(?:\s+(?:ее|её|его))?$", t))
                provider = "yandex_music" if re.search(r"яндекс[\s._-]*музык\w*|yandex[\s._-]*music", t) else "music"
                return RoutedIntent("music_search", provider, {"action": "play_track" if play_requested else "search_track", "query": query}, 940, 0.995,
                                    ("не browser.search без явного 'в браузере/интернете'", "не discord.music_bot_play"))

        direct_song = re.match(
            r"^(?:включи|поставь|вруби|запусти|воспроизведи|сыграй)\s+"
            r"(?:песн\w*|трек\w*|композиц\w*|музык\w*)\s+(.+)$",
            t, flags=re.I,
        )
        if direct_song and not re.search(r"\b(?:discord|дискорд|youtube|ютуб|видео|ролик)\w*\b", t):
            query = re.sub(
                r"\s+(?:в|через)\s+(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s*$",
                "", direct_song.group(1), flags=re.I,
            ).strip(" ,.-")
            if query and not pause_like(query):
                return RoutedIntent("music_play_named", "music", {"action": "play_track", "query": query}, 940, 0.995,
                                    ("не browser.search", "не discord.music_bot_play", "объект песни/трека указан явно"))

        # Explicit web/browser lookup is a browser action. General questions such as
        # «что такое X» intentionally fall through to the local LLM answer path.
        if looks_like_browser_search(t):
            query = strip_browser_search_scaffolding(t)
            if query:
                return RoutedIntent("browser_search", "browser", {"action": "search", "value": query}, 935, 0.995,
                                    ("не yandex_music.play_track", "не filesystem.search без объекта файл/документ"))

        # Pronoun/media followups can use the agent's remembered domain; do not guess here.

        # Power management. Desired-state phrases are deterministic and outrank
        # generic application/window commands.
        if re.search(r"\b(?:выключи|погаси)\b.*\b(?:монитор|экран)\w*\b", t):
            return RoutedIntent("power", "system", {"action": "turn_off_displays"}, 800)
        if re.search(r"\b(?:заблокируй|блокируй|lock)\b.*\b(?:пк|компьютер|windows)?\b", t):
            return RoutedIntent("power", "system", {"action": "lock"}, 800)
        if re.search(r"^(?:(?:джарвис|нео|нио|neo)\s+)?(?:сон|спящий режим|усыпи компьютер|переведи компьютер в сон)(?:\s+(?:пожалуйста|сейчас|немедленно|аккуратно))?$", t) or re.search(r"\b(?:переведи|отправь)\b.*\b(?:пк|компьютер)\b.*\bсон\b", t):
            return RoutedIntent("power", "system", {"action": "sleep"}, 800)
        plan = re.search(r"(?:план\w* питания|режим\w* питания)(?:\s+на|\s*[-—:]?)?\s*(высок\w* производительност\w*|сбалансированн\w*|энергосбережен\w*|игров\w*)", t)
        if plan and re.search(r"\b(?:поставь|включи|выбери|переключи|установи|режим|план)\b", t):
            raw=plan.group(1)
            value="Высокая производительность" if raw.startswith("высок") or raw.startswith("игров") else "Сбалансированный" if raw.startswith("сбаланс") else "Энергосбережение"
            return RoutedIntent("power", "system", {"action": "set_power_plan", "power_plan": value}, 800)
        if re.search(r"\b(?:гибернац\w*|hibernate)\b", t):
            return RoutedIntent("power", "system", {"action": "hibernate"}, 800)
        if re.search(r"\b(?:отмени|отменить)\b.*\b(?:выключ\w*|завершен\w*|shutdown)\b", t):
            return RoutedIntent("power", "system", {"action": "cancel_shutdown"}, 800)
        m = re.search(r"\bвыключ\w*\b.*?через\s+(\d+)\s*(минут\w*|секунд\w*|час\w*)", t)
        if m:
            value = int(m.group(1)); unit = m.group(2)
            seconds = value * (3600 if unit.startswith("час") else 60 if unit.startswith("минут") else 1)
            return RoutedIntent("power", "system", {"action": "shutdown", "delay_seconds": seconds}, 800)

        if re.search(r"\b(?:выключи|выруби|отключи|заверши\s+работу)\w*\b.*\b(?:пк|компьютер|windows|виндовс|машин\w*)\b", t):
            # Give the confirmation layer a small cancellation window after the
            # HIGH-risk command is approved.
            return RoutedIntent("power", "system", {"action": "shutdown", "delay_seconds": 10}, 801, 1.0)

        # Hardware state questions.
        if (
            not external_payload_shell
            and re.search(r"\b(?:температур\w*|нагрузк\w*|частот\w*|потреблен\w*|vram|видеопамят\w*|оперативн\w*|ram|gpu|видеокарт\w*|процессор\w*|cpu|fps)\b", t)
            and re.search(r"\b(?:какая|какой|сколько|покажи|состояние|что с|как)\b", t)
        ):
            target = "gpu" if re.search(r"\b(?:gpu|видеокарт\w*|vram|видеопамят\w*)\b", t) else "cpu" if re.search(r"\b(?:cpu|процессор\w*)\b", t) else "all"
            return RoutedIntent("hardware", "hardware", {"action": "status", "target": target}, 760)

        # Discord music queue and transport.  Any utterance that explicitly
        # names Discord owns the media command: it must never fall through to
        # Yandex Music or the generic Windows media-session controller. Queue
        # insertion is checked before bare "next" so "следующая песня ... X"
        # queues X while "следующий трек в Discord" skips the current item.
        discord_explicit = discord_media_explicit
        if discord_explicit:
            # Shuffle is a queue policy and has priority over the broad Discord
            # «выключи» -> music_stop rule below.
            if re.search(r"\b(?:перемешиван\w*|шаффл|shuffle)\b", t):
                if re.search(r"\b(?:включи|активируй|запусти|включить)\w*\b", t):
                    return RoutedIntent("discord_music_shuffle_on", "discord", {"action": "music_shuffle_on"}, 956, 0.999, ("explicit Discord", "background queue shuffle"))
                if re.search(r"\b(?:выключи|отключи|убери|выключить)\w*\b", t):
                    return RoutedIntent("discord_music_shuffle_off", "discord", {"action": "music_shuffle_off"}, 956, 0.999, ("explicit Discord", "background queue shuffle"))
            if re.search(r"\b(?:очисти|сбрось|удали\s+всю)\w*\s+очеред\w*", t):
                return RoutedIntent("discord_music_queue_clear", "discord", {"action": "music_queue_clear"}, 951, 0.999, ("explicit Discord", "background queue"))
            if (
                re.search(r"\b(?:что|какая|какой|какие|покажи|назови|перечисли)\w*.*\bочеред\w*", t)
                or re.search(r"\b(?:что|какая|какой|кто)\w*.*\b(?:сейчас\s+)?(?:играет|проигрыва\w*|звучит)\b", t)
            ):
                return RoutedIntent("discord_music_queue_status", "discord", {"action": "music_queue_status"}, 950, 0.999, ("explicit Discord", "background queue/status"))

            queue_next_patterns = (
                r"\bследующ\w*\s+(?:песн|трек)\w*\s+(?:в\s+)?(?:discord|дискорд)\w*\s*(?:пусть\s+будет|будет|[-—:])\s*(.+)$",
                r"\bследующ\w*\s+(?:песн|трек)\w*\s*(?:пусть\s+будет|будет|[-—:])\s*(.+?)\s+(?:в\s+)?(?:discord|дискорд)\w*\s*$",
                r"\bследующ\w*\s+(?:песн|трек)\w*\s+(?:для\s+(?:бот\w*\s+)?)?(?:в\s+)?(?:discord|дискорд)\w*\s+(.+)$",
                r"\b(?:поставь|сделай)\w*\s+следующ\w*\s+(?:песн|трек)\w*\s+(.+?)\s+(?:в\s+)?(?:discord|дискорд)\w*\s*$",
                r"\bпосле\s+(?:этой|не[её]?)\s+(?:песн\w*|трек\w*)?\s*(?:включи|поставь)?\s*(.+?)\s+(?:в\s+)?(?:discord|дискорд)\w*\s*$",
            )
            for pattern in queue_next_patterns:
                m_queue = re.search(pattern, t)
                if m_queue:
                    query = m_queue.group(1).strip(" \t\n\r«»\"'")
                    if query and not re.fullmatch(r"(?:музык\w*|песн\w*|трек\w*|бот\w*)", query):
                        return RoutedIntent("discord_music_queue_next", "discord", {"action": "music_queue_next", "query": query}, 953, 0.999, ("explicit Discord", "background queue"))

            queue_add_patterns = (
                r"\b(?:добавь|поставь)\w*\s+(.+?)\s+в\s+очеред\w*\s+(?:discord|дискорд)\w*\s*$",
                r"\b(?:добавь|поставь)\w*\s+(?:в\s+)?очеред\w*\s+(?:discord|дискорд)\w*\s+(.+)$",
            )
            for pattern in queue_add_patterns:
                m_queue = re.search(pattern, t)
                if m_queue:
                    query = m_queue.group(1).strip(" \t\n\r«»\"'")
                    if query:
                        return RoutedIntent("discord_music_queue_add", "discord", {"action": "music_queue_add", "query": query}, 952, 0.999, ("explicit Discord", "background queue"))

            # Skip/next has no previous-history semantics: the finished/current
            # file is discarded and only the remaining queue moves forward.
            discord_skip = bool(
                re.search(r"\b(?:пропусти|скипни)\w*(?:\s+(?:текущ\w*\s+)?(?:песн|трек|музык)\w*)?", t)
                or re.search(r"\bпереключи\w*.*\bследующ\w*", t)
                or re.search(r"\b(?:следующ\w*\s+(?:песн|трек)\w*|следующ\w*|дальше)\b", t)
                # Whisper sometimes hears Russian «пропусти» as «запусти».
                # Only reinterpret it when the utterance also explicitly asks for
                # the next/current track, never for a named song.
                or re.search(r"\bзапусти\w*\s+(?:следующ\w*|дальше|текущ\w*\s+(?:трек|песн)\w*)", t)
                # Frequent Whisper confusion: spoken «пропусти трек в Discord»
                # becomes «запусти трек в Discord». Treat only the payload-less
                # form as skip; a named-song request still reaches music_bot_play.
                or re.fullmatch(r"(?:(?:джарвис|нео|нио|neo)\s+)?запусти\w*\s+(?:трек|песн|музык)\w*\s+(?:в\s+)?(?:discord|дискорд)\w*", t)
            )
            if discord_skip:
                return RoutedIntent("discord_music_skip", "discord", {"action": "music_skip"}, 950, 0.999, ("explicit Discord", "background queue"))

            # Explicit Discord desired-state controls do not require the user to
            # repeat the word "music". This covers "пауза в Discord",
            # "продолжи в Discord" and "стоп в Discord" as well as longer forms.
            if re.search(r"\b(?:останови|стопни|прекрати|выключи|стоп)\w*\b", t):
                return RoutedIntent("discord_music_stop", "discord", {"action": "music_stop"}, 949, 0.999, ("explicit Discord", "background bot"))
            if pause_like(t, strong_domain=True) or re.search(r"\b(?:пауза|приостанови)\w*\b", t):
                return RoutedIntent("discord_music_pause", "discord", {"action": "music_pause"}, 948, 0.999, ("explicit Discord", "background bot"))
            if resume_like(t) or re.search(r"\b(?:продолжи|возобнови|возобновить)\w*\b", t):
                return RoutedIntent("discord_music_resume", "discord", {"action": "music_resume"}, 947, 0.999, ("explicit Discord", "background bot"))

        # Deterministic background Discord playback.  Do not leave the most
        # common hands-free phrase to the LLM: it must resolve to the official
        # bot path even while the user is in a game.
        discord_bot_request_patterns = (
            r"^(?:сделай|отправь|дай)\s+(?:запрос|команду)\s+(?:моему\s+)?музыкальн\w*\s+бот\w*\s+(?:в\s+)?(?:discord|дискорд)\w*\s+(?:включи\w*\s+)?(.+)$",
            r"^(?:сделай|отправь|дай)\s+(?:запрос|команду)\s+(?:в\s+)?(?:discord|дискорд)\w*\s+(?:музыкальн\w*\s+бот\w*)?\s*(?:включи\w*\s+)?(.+)$",
            r"^(?:включи|поставь|проиграй)\w*\s+(.+?)\s+(?:через\s+)?музыкальн\w*\s+бот\w*\s+(?:в\s+)?(?:discord|дискорд)\w*$",
        )
        for pattern in discord_bot_request_patterns:
            m_bot_request = re.search(pattern, t)
            if m_bot_request:
                query = m_bot_request.group(1).strip(" \t\n\r«»\"'")
                if query and not re.fullmatch(r"(?:музык\w*|трек\w*|песн\w*|бот\w*)", query):
                    return RoutedIntent(
                        "discord_music_play", "discord",
                        {"action": "music_bot_play", "query": query},
                        942, 0.999, ("official NEO Bot only", "no desktop UI"),
                    )

        # Short hands-free Discord playback. Accept all common word orders:
        #   «включи Starboy в Discord»
        #   «включи в Discord Starboy»
        #   «в Discord включи Starboy»
        #   «включи мне в Discord песню Starboy»
        # Explicit Discord owns the command, so these never fall through to Yandex.
        discord_play_patterns = (
            r"\b(?:включи|поставь|запусти|вруби|сыграй|проиграй|play)\w*\s+(?:мне\s+)?(?:(?:песн|трек|музык)\w*\s+)?(.+?)\s+(?:в|через)\s+(?:discord|дискорд)\w*\s*$",
            r"\b(?:включи|поставь|запусти|вруби|сыграй|проиграй|play)\w*\s+(?:мне\s+)?(?:в|через)\s+(?:discord|дискорд)\w*\s+(?:(?:песн|трек|музык)\w*\s+)?(.+?)\s*$",
            r"\b(?:в|через)\s+(?:discord|дискорд)\w*\s+(?:включи|поставь|запусти|вруби|сыграй|проиграй|play)\w*\s+(?:мне\s+)?(?:(?:песн|трек|музык)\w*\s+)?(.+?)\s*$",
            r"\b(?:включи|поставь|запусти|вруби|сыграй|проиграй|play)\w*\s+(?:мне\s+)?(?:(?:в|через)\s+)?(?:discord|дискорд)\w*\s+(?:песн\w*|трек\w*|музык\w*)\s+(.+?)\s*$",
        )
        for discord_pattern in discord_play_patterns:
            discord_play = re.search(discord_pattern, t)
            if not discord_play:
                continue
            query = discord_play.group(1).strip(" \t\n\r«»\"'")
            query = re.sub(r"^(?:песн|трек|музык)\w*\s+", "", query).strip()
            if query and not re.fullmatch(r"(?:музык\w*|трек\w*|песн\w*|бот\w*)", query):
                return RoutedIntent(
                    "discord_music_play", "discord",
                    {"action": "music_bot_play", "query": query},
                    945, 0.999, ("explicit Discord", "short background bot command"),
                )

        # Visible applications/windows are a user-facing UI inventory, not the
        # raw process list. Explorer folders and games are included when they
        # own a real visible top-level window.
        if re.search(r"\b(?:покажи|перечисли|какие|список)\b.*\b(?:активн\w*|открыт\w*|запущенн\w*)\b.*\b(?:приложен\w*|программ\w*|окн\w*)\b", t, re.I):
            return RoutedIntent("active_applications", "application", {"action": "list_active"}, 860, 0.999, ("visible UI windows only",))

        # Telegram personal messages through official Secretary Mode. These
        # deterministic shells run before generic communication/game fallbacks.
        if re.search(r"\b(?:telegram|телеграм\w*)\b", t, re.I):
            reply = re.search(r"(?:ответь|ответить)\s+(?:(?:в|через)\s+телеграм(?:е|ме)?\s+)?(?P<who>ему|ей|[\w@.-]+)(?:\s*,)?\s*(?:(?:то\s+что|что|следующее)\s+)?(?P<body>.+)$", t, re.I)
            if reply:
                who=reply.group("who").strip(); body=reply.group("body").strip(" ,.:;«»\"'")
                return RoutedIntent("telegram_reply", "telegram_messages", {"action":"reply","sender":"" if who in {"ему","ей"} else who,"text":body}, 852, 0.999, ("Secretary Mode only", "message shell stripped"))
            who_wrote = re.search(r"(?:что|чего)\s+(?:мне\s+)?написал[аи]?\s+([\w@.-]+)(?:\s+в\s+телеграм\w*)?", t, re.I)
            if who_wrote:
                return RoutedIntent("telegram_from_sender", "telegram_messages", {"action":"from_sender","sender":who_wrote.group(1).strip()}, 851, 0.999, ("Secretary Mode only",))
            if re.search(r"\b(?:сколько|количеств\w*)\b.*\bсообщен\w*\b|\bсообщен\w*\b.*\b(?:сколько|количеств\w*)\b", t, re.I):
                recent24=bool(re.search(r"\b(?:24\s*час\w*|сутк\w*|за\s+день)\b",t,re.I))
                return RoutedIntent("telegram_message_count", "telegram_messages", {"action":"status","recent24h":recent24}, 850, 0.999, ("Secretary Mode only",))
            if (
                re.search(r"\b(?:последн\w*|крайн\w*|прочитай|покажи)\b.*\b(?:сообщен\w*|написал\w*|написали\w*)\b", t, re.I)
                or re.search(r"\b(?:кто|что)\s+(?:мне\s+)?последн\w*\s+(?:написал\w*|прислал\w*)", t, re.I)
                or re.search(r"\bпоследн\w*\s+сообщен\w*(?:\s+за\s+все\s+время)?", t, re.I)
            ):
                return RoutedIntent("telegram_latest", "telegram_messages", {"action":"latest"}, 849, 0.995, ("Secretary Mode only", "all locally observed history"))
            if re.search(r"\b(?:проверь|диагност\w*|почему)\b.*\b(?:telegram|телеграм\w*)\b.*\b(?:сообщен\w*|business|секретар\w*)\b", t, re.I):
                return RoutedIntent("telegram_diagnostic", "telegram_messages", {"action":"diagnostic"}, 848, 0.997, ("Secretary diagnostics",))

        # Google Calendar owns schedule/event phrases and is kept separate from
        # local Timer/Reminder semantics.
        if re.search(r"\b(?:календар\w*|расписан\w*|событи\w*|встреч\w*)\b", t, re.I):
            # Mutation verbs win before read-only date queries. Otherwise a
            # phrase such as "внеси в расписание ... завтра ..." can be stolen
            # by the generic "что у меня завтра" branch and lose the event.
            if re.search(r"\b(?:добавь|добавить|внеси|внести|создай|создать|запланируй|запланировать|поставь|поставить|запиши|записать)\b", t, re.I):
                try:
                    parsed = parse_calendar_add_command(text)
                except ValueError:
                    parsed = {}
                if parsed:
                    return RoutedIntent("calendar_add", "google_calendar", {"action":"add", **parsed}, 850, 0.999, ("calendar mutation before date query", "calendar shell stripped", "calendar not timer"))
            if re.search(r"\b(?:все|всё|полное|целиком)\b.*\bрасписан\w*\b|\bпокажи\b.*\b(?:все|всё)\b.*\b(?:событи|встреч)\w*\b", t, re.I):
                return RoutedIntent("calendar_all", "google_calendar", {"action":"all","limit":100}, 849, 0.999, ("calendar not timer",))
            if re.search(r"\b(?:сегодня|на\s+сегодня)\b", t, re.I) and re.search(r"\b(?:какие|что|покажи|расписан|событи|встреч)\w*\b", t, re.I):
                return RoutedIntent("calendar_today", "google_calendar", {"action":"today"}, 848, 0.999, ("calendar not timer",))
            if re.search(r"\bзавтра\b", t, re.I) and re.search(r"\b(?:какие|что|покажи|расписан|событи|встреч)\w*\b", t, re.I):
                return RoutedIntent("calendar_tomorrow", "google_calendar", {"action":"tomorrow"}, 848, 0.999, ("calendar not timer",))
            if re.search(r"\b(?:следующ\w*|ближайш\w*)\s+(?:событи|встреч)\w*", t, re.I):
                return RoutedIntent("calendar_next", "google_calendar", {"action":"next"}, 848, 0.999, ("calendar not timer",))

        # HOTFIX98: the user explicitly does not want NeoBot pretending to be
        # their Discord account. Official Discord forbids self-bot/user-bot
        # automation, so manual "join/send as me" requests fail closed instead of
        # silently executing under the bot identity. Music remains NeoBot-only.
        if re.search(r"\b(?:discord|дискорд|голосов\w*\s+канал|войс|текстов\w*\s+канал)\b", t, re.I):
            user_account_control = bool(
                # Explicit channel navigation was previously executed by NeoBot,
                # which is not what a user asking to move *their* Discord account
                # expects.  Ordinary Discord user accounts must never be automated.
                re.search(r"\b(?:подключись|подключи(?:сь)?|зайди|вступи|выйди|отключись|привяжись)\b.*\b(?:канал|голосов\w*|текстов\w*|voice|войс|discord|дискорд)\b", t, re.I)
                or re.search(r"\b(?:отправь|напиши)\b.*\b(?:discord|дискорд|канал)\b", t, re.I)
                or re.search(r"\b(?:discord|дискорд)\b.*\b(?:отправь|напиши)\b", t, re.I)
            )
            if user_account_control:
                return RoutedIntent("discord_user_account_automation_unavailable", "discord", {"action": "user_account_automation_unavailable"}, 990, 1.0, ("official Discord API only", "never user token/self-bot"))

        # Discord non-music controls are API-only as well.  Desktop navigation
        # (open server/channel, mute/deafen via client UI, Quick Switcher, etc.)
        # was intentionally removed from the integration in v4.0.11.
        if re.search(r"\b(?:discord|дискорд|сервер|текстов\w*\s+канал\w*|голосов\w*\s+канал\w*|войс)\b", t):
            channel_attach = re.search(r"(?:подключись|подключи(?:сь)?|привяжись|выбери|зайди|вступи)\s+(?:к\s+|на\s+|в\s+)?(?:(текстов\w*|голосов\w*|voice|войс)\s+)?канал\w*\s+[«\"']?(.+?)[»\"']?(?:\s+на\s+сервере\s+[«\"']?(.+?)[»\"']?)?(?:\s+в\s+(?:discord|дискорд))?$", t, re.I | re.S)
            if channel_attach:
                kind=str(channel_attach.group(1) or "").casefold(); channel_name=" ".join(channel_attach.group(2).split()).strip(); server_name=" ".join(str(channel_attach.group(3) or "").split()).strip()
                channel_name=re.sub(r"\s+(?:в\s+)?(?:discord|дискорд)\w*$", "", channel_name, flags=re.I).strip()
                if kind.startswith("текст"):
                    action="attach_text_channel"; intent="discord_attach_text_channel"
                elif kind.startswith("голос") or kind in {"voice","войс"}:
                    action="bot_join_voice"; intent="discord_bot_join_voice"
                else:
                    action="connect_channel"; intent="discord_connect_named_channel"
                args={"action":action,"channel":channel_name}
                if server_name: args["server"]=re.sub(r"\s+(?:в\s+)?(?:discord|дискорд)\w*$", "", server_name, flags=re.I).strip()
                return RoutedIntent(intent, "discord", args, 846, 0.999, ("explicit named Discord channel wins auto-follow",))
            if re.search(r"(?:отключись|отвяжись)\s+(?:от\s+)?(?:текстов\w*\s+)?канал", t, re.I):
                return RoutedIntent("discord_detach_text_channel", "discord", {"action":"detach_text_channel"}, 846, 0.999, ("official bot text detach",))
            if re.search(r"(?:к\s+какому|какой)\s+(?:текстов\w*\s+)?канал\w*.*(?:подключ|привязан)", t, re.I):
                return RoutedIntent("discord_text_channel_status", "discord", {"action":"text_channel_status"}, 846, 0.995, ("official bot text status",))
            if re.search(r"\b(?:отключись|выйди)\b.*\b(?:голосов|войс)", t):
                return RoutedIntent("discord_bot_leave_voice", "discord", {"action": "bot_leave_voice"}, 835, 0.99, ("official bot only",))
            voice = re.search(r"(?:подключись|зайди|вступи)\s+(?:в\s+)?(?:голосов\w*\s+канал|войс)\s+[«\"']?(.+?)[»\"']?(?:\s+на\s+сервере\s+(.+))?$", t)
            if voice:
                args={"action":"bot_join_voice","channel":voice.group(1).strip()}
                if voice.group(2): args["server"]=voice.group(2).strip()
                return RoutedIntent("discord_bot_join_voice", "discord", args, 835, 0.98, ("official bot only",))
            msg = re.search(r"(?:отправь|напиши)\s+(?:в\s+(?:discord|дискорд)\s+)?(?:сообщение\s+)?[«\"']?(.+?)[»\"']?\s+(?:в|на)\s+(?:канал|текстов\w*\s+канал)\s+[«\"']?([^»\"']+?)[»\"']?(?:\s+на\s+сервере\s+[«\"']?(.+?)[»\"']?)?$", t)
            if msg:
                channel_name = re.sub(r"\s+(?:в\s+)?(?:discord|дискорд)\w*$", "", msg.group(2).strip())
                args={"action":"send_message","text":msg.group(1).strip(),"channel":channel_name}
                if msg.group(3): args["server"]=msg.group(3).strip()
                return RoutedIntent("discord_send_message", "discord", args, 835, 0.96, ("official bot only",))
            # Once a writable text channel is attached, a short explicit Discord
            # send command uses that persisted destination. The message payload is
            # captured only after the Discord command shell, so words inside the
            # message cannot switch routing domains.
            attached_msg = re.search(r"(?:отправь|напиши)\s+(?:в|через)\s+(?:discord|дискорд)\w*\s+(?:сообщение\s+)?[«\"']?(.+?)[»\"']?\s*$", t, re.I)
            if not attached_msg:
                attached_msg = re.search(r"(?:отправь|напиши)\s+(?:сообщение\s+)?[«\"']?(.+?)[»\"']?\s+(?:в|через)\s+(?:discord|дискорд)\w*\s*$", t, re.I)
            if attached_msg:
                body = attached_msg.group(1).strip(" «»\"'.,")
                if body:
                    return RoutedIntent("discord_send_attached_message", "discord", {"action":"send_message","text":body}, 835, 0.965, ("official bot attached text channel",))
            if re.search(r"\b(?:открой|перейди|зайди|покажи)\b.*\b(?:сервер|текстов\w*\s+канал|discord|дискорд)\b", t):
                return RoutedIntent(
                    "discord_ui_removed", "discord", {"action": "user_account_automation_unavailable"}, 834, 0.99,
                    ("desktop Discord UI removed", "never application.open"),
                )


        # Email: Gmail API + Mail.ru IMAP/SMTP.  The mail command shell owns
        # the whole turn before game/system/browser fallbacks.  Generic read-only
        # questions aggregate all configured mailboxes; an explicit service name
        # narrows the same action to that provider.  Folder names are payload and
        # never change the owner of the turn.
        if re.search(r"\b(?:почт\w*|письм\w*|писем|email\w*|gmail|mail\.ru|мейл\.ру|мэйл\.ру)\b", t) and not re.search(r"\bвкладк\w*\b", t):
            if re.search(r"\bgmail\b", t):
                provider = "gmail"
            elif re.search(r"\b(?:mail\.ru|мейл\.ру|мэйл\.ру)\b", t):
                provider = "mailru"
            else:
                provider = "auto"

            folder = ""
            fm = re.search(
                r"\b(?:в|из)\s+папк\w*\s+[«\"']?(.+?)[»\"']?(?=\s+(?:за|сегодня|вчера|последн\w*)\b|\s+в\s+(?:gmail|mail\.ru|мейл\.ру|мэйл\.ру)\b|\s+(?:gmail|mail\.ru|мейл\.ру|мэйл\.ру)\b|[,.!?]|$)",
                t,
            )
            if fm:
                folder = fm.group(1).strip(" «»\"'.,!? ")

            base: dict[str, Any] = {"provider": provider}
            if folder:
                base["folder"] = folder

            # Connection/source discovery.
            if re.search(r"\b(?:какие|покажи|перечисли|список)\b.*\b(?:папк|ярлык)\w*\b", t):
                return RoutedIntent("email", "email", {**base, "action": "folders"}, 792)
            if re.search(r"\b(?:какие|покажи|перечисли)\b.*\b(?:почт\w*|ящик\w*|аккаунт\w*)\b.*\b(?:подключ|доступ)", t):
                return RoutedIntent("email", "email", {"action": "providers", "provider": provider}, 790)
            if re.search(r"\b(?:открой|зайди)\b", t) and re.search(r"\b(?:входящ\w*|почт\w*|gmail|mail\.ru|мейл\.ру|мэйл\.ру)\b", t):
                return RoutedIntent("email", "email", {**base, "action": "open_inbox"}, 780)

            count_shape = bool(re.search(r"\b(?:сколько|количеств\w*|посчитай|счётчик|счетчик)\b", t))
            list_shape = bool(re.search(r"\b(?:покажи|перечисли|какие|выведи|список)\b", t))
            unread = bool(re.search(r"\bнепрочитан\w*\b", t))
            today = bool(re.search(r"\b(?:сегодня|за сегодня|с начала дня)\b", t))
            recent24 = bool(
                re.search(r"\b(?:за|последн\w*)\s+(?:24|двадцать четыре)\s+(?:час|часа|часов)\b", t)
                or re.search(r"\b(?:за\s+)?(?:сутки|последние сутки)\b", t)
            )
            all_time = bool(re.search(r"\b(?:за\s+вс[её]\s+время|за\s+весь\s+период|всего\s+писем|общее\s+количеств\w*)\b", t))

            # Counts are deliberately count-only: asking "сколько" must never
            # dump an arbitrary top-10 list.  Folder breakdown is produced by the
            # mail tool from the user's selected sources.
            if count_shape and unread and today:
                return RoutedIntent("email", "email", {**base, "action": "unread_today"}, 799)
            if count_shape and unread and recent24:
                return RoutedIntent("email", "email", {**base, "action": "unread_recent24h"}, 799)
            if count_shape and unread:
                return RoutedIntent("email", "email", {**base, "action": "unread"}, 797)
            if count_shape and today:
                return RoutedIntent("email", "email", {**base, "action": "today"}, 796)
            if count_shape and recent24:
                return RoutedIntent("email", "email", {**base, "action": "recent24h"}, 796)
            if count_shape and all_time:
                return RoutedIntent("email", "email", {**base, "action": "total"}, 796)

            # Explicit lists use the configured presentation mode unless the user
            # asks for a specific field of the latest message.
            if list_shape and unread and today:
                return RoutedIntent("email", "email", {**base, "action": "unread_today_list", "limit": 10}, 794)
            if list_shape and unread and recent24:
                return RoutedIntent("email", "email", {**base, "action": "unread_recent24h_list", "limit": 10}, 794)
            if list_shape and unread:
                return RoutedIntent("email", "email", {**base, "action": "unread_list", "limit": 10}, 793)
            if list_shape and today:
                return RoutedIntent("email", "email", {**base, "action": "today_list", "limit": 10}, 793)
            if list_shape and recent24:
                return RoutedIntent("email", "email", {**base, "action": "recent24h_list", "limit": 10}, 793)

            # Latest/read queries.  Singular wording returns one message, plural
            # wording returns a bounded list.  "от кого" and "тема" select a
            # deterministic output detail rather than relying on the LLM.
            if re.search(r"\b(?:прочитай|прочти|зачитай)\b.*\bпоследн\w*\s+письм", t):
                return RoutedIntent("email", "email", {**base, "action": "read"}, 800)
            if re.search(r"\b(?:от\s+кого|кто\s+отправил)\b.*\bпоследн\w*\s+письм", t):
                return RoutedIntent("email", "email", {**base, "action": "latest", "limit": 1, "detail": "sender"}, 799)
            if re.search(r"\b(?:какая|покажи|скажи)?\s*тем\w*\b.*\bпоследн\w*\s+письм", t):
                return RoutedIntent("email", "email", {**base, "action": "latest", "limit": 1, "detail": "sender_subject"}, 799)
            if re.search(r"\b(?:какое|покажи|скажи)\b.*\bпоследн\w*\s+письм", t):
                return RoutedIntent("email", "email", {**base, "action": "latest", "limit": 1}, 797)
            if re.search(r"\bпоследн\w*\s+письм", t):
                singular = bool(re.search(r"\b(?:письмо|письма)\b", t)) and not bool(re.search(r"\b(?:письма|писем)\b", t))
                return RoutedIntent("email", "email", {**base, "action": "latest", "limit": 1 if singular else 10}, 790)

            # Search by sender / subject / free text.
            sender = re.search(r"(?:письм\w*\s+от|найди\s+(?:письм\w*\s+)?от)\s+(.+?)(?=\s+(?:в\s+папке|gmail|mail\.ru|мейл\.ру|мэйл\.ру)\b|$)", t)
            if sender:
                return RoutedIntent("email", "email", {**base, "action": "search", "sender": sender.group(1).strip(), "limit": 10}, 792)
            subject = re.search(r"(?:с\s+темой|по\s+теме|тема)\s+[«\"']?(.+?)[»\"']?(?=\s+(?:в\s+папке|gmail|mail\.ru|мейл\.ру|мэйл\.ру)\b|$)", t)
            if re.search(r"\b(?:найди|покажи|поищи)\b", t) and subject:
                return RoutedIntent("email", "email", {**base, "action": "search", "query": f"subject:{subject.group(1).strip()}", "limit": 10}, 791)

            # Common write/reply forms.  Higher-risk execution and confirmation are
            # still enforced by the tool registry; this parser only fixes ownership.
            reply = re.search(r"\b(?:ответь|ответить)\b.*?\b(?:письм\w*|ему|ей)\b\s*[:,-]?\s*(.+)$", t)
            if reply and reply.group(1).strip():
                return RoutedIntent("email", "email", {**base, "action": "reply", "body": reply.group(1).strip()}, 795)
            send = re.search(r"\b(?:отправь|напиши)\s+письм\w*\s+(?:на\s+)?([\w.+-]+@[\w.-]+\.\w+)\s*[:,-]?\s*(.+)$", t)
            if send:
                return RoutedIntent("email", "email", {**base, "action": "send", "to": send.group(1), "body": send.group(2).strip()}, 795)
            if re.search(r"\b(?:создай|сделай|открой)\b.*\b(?:черновик|новое письмо|письмо)\b", t):
                recipient = ""
                rm = re.search(r"(?:кому|для|на адрес)\s+([^,;]+)", t)
                if rm:
                    recipient = rm.group(1).strip()
                return RoutedIntent("email", "email", {**base, "action": "draft", "to": recipient}, 775)

            # General mailbox status remains a read-only aggregate query.
            if re.search(r"\b(?:статус|состояни\w*|что\s+с\s+почт\w*)\b", t):
                return RoutedIntent("email", "email", {**base, "action": "status"}, 770)

        # Screen advisor: deterministic call to the vision tool; LLM still performs the visual reasoning.
        if re.search(r"\b(?:что здесь нужно нажать|что тут нужно нажать|почему кнопка неактивна|что означает это окно|что делать в этой игре|что мне делать в игре|помоги по экрану|посоветуй по экрану)\b", t):
            return RoutedIntent("screen_help", "screen", {"action": "analyze", "instruction": text}, 700)

        # Human file/content search.
        if re.match(r"^(?:найди|поищи|отыщи)\b", t) and re.search(r"\b(?:файл|документ|pdf|excel|xlsx|docx|pptx|скриншот)\b", t):
            content = bool(re.search(r"\b(?:где я писал|в котором|где написано|по содержим|про\s+\w+)\b", t))
            return RoutedIntent("filesystem_search", "search", {"action": "content" if content else "files", "query": text}, 650)

        # Clipboard history.
        if (re.search(r"\bбуфер\w*\b", t) or re.search(r"\bкопировал\w*\s+(?:два|три|\d+)\s+раз", t)) and re.match(r"^(?:вставь(?:те)?|вставить|очисти(?:те)?)\b", t):
            if re.search(r"\bочист", t):
                return RoutedIntent("clipboard", "clipboard", {"action": "clear"}, 820)
            m = re.search(r"(?:копировал\w*\s+)?(два|три|четыре|пять|[2-9]|\d{1,2})\s+раз", t)
            if m:
                raw=m.group(1); words={"два":2,"три":3,"четыре":4,"пять":5}; n=words.get(raw,int(raw) if raw.isdigit() else 1)
                offset=max(0,min(49,n-1))
            else:
                offset=0
            return RoutedIntent("clipboard", "clipboard", {"action": "paste_history", "offset": offset}, 820)

        # Audio endpoints and microphone. These are desired-state commands, not
        # generic application launches.  Device-list/status requests must be
        # resolved here before the flexible ``покажи X`` application grammar.
        if re.search(r"\b(?:покажи|покажи\s+мне|перечисли|список|какие|какой)\b", t) and re.search(r"\b(?:(?:микрофон|микро)\w*|устройств\w*\s+ввода|аудиовход\w*)\b", t):
            return RoutedIntent(
                "audio_input_devices", "audio",
                {"action": "list_devices", "device_type": "input"},
                794, 0.995, ("не application.open", "capture endpoints only"),
            )
        if re.search(r"\b(?:покажи|покажи\s+мне|перечисли|список|какие|какой)\b", t) and re.search(r"\b(?:устройств\w*\s+вывода|аудиовыход\w*|выход\w*\s+аудио|наушник\w*|динамик\w*|колонк\w*)\b", t):
            return RoutedIntent(
                "audio_output_devices", "audio",
                {"action": "list_devices", "device_type": "output"},
                794, 0.995, ("не application.open", "render endpoints only"),
            )

        # System master volume has its own explicit namespace.  Keep the broad
        # game phrase ``звук в <game>`` out of this rule: absolute master volume
        # requires ``система/Windows/общая громкость`` or an exact bare mute form.
        system_volume = re.search(
            r"(?:громкост\w*\s+(?:систем\w*|windows|виндовс|компьютер\w*|общ\w*)|"
            r"(?:систем\w*|windows|виндовс|компьютер\w*|общ\w*)\s+громкост\w*)"
            r"(?:\s+(?:на|в|до))?\s+(\d{1,3})(?:\s*%|\s*процент\w*)?",
            t, re.I,
        )
        if system_volume:
            return RoutedIntent(
                "system_volume", "audio",
                {"action": "set_volume", "value": max(0, min(100, int(system_volume.group(1))))},
                793, 0.999, ("system master only", "не per-app/game audio"),
            )
        if re.fullmatch(r"(?:(?:джарвис|нео|нио|neo)\s+)?(?:выключи|убери|отключи|замьюти)\s+звук(?:\s+(?:системы|windows|виндовс|компьютера))?", t):
            return RoutedIntent("system_audio_mute", "audio", {"action": "mute", "muted": True}, 793, 0.999, ("system master only",))
        if re.fullmatch(r"(?:(?:джарвис|нео|нио|neo)\s+)?(?:включи|верни|размьюти)\s+звук(?:\s+(?:системы|windows|виндовс|компьютера))?", t):
            return RoutedIntent("system_audio_unmute", "audio", {"action": "mute", "muted": False}, 793, 0.999, ("system master only",))

        if re.search(r"\b(?:выключи|выруби|заглуши|замуть)\s+(?:мой\s+)?(?:микрофон|микро)\b", t):
            return RoutedIntent("microphone_mute", "audio", {"action":"set_input_mute","muted":True}, 790, 1.0, ("не system mute output",))
        if re.search(r"\b(?:включи|размуть|unmute)\s+(?:мой\s+)?(?:микрофон|микро)\b", t):
            return RoutedIntent("microphone_unmute", "audio", {"action":"set_input_mute","muted":False}, 790, 1.0, ("не system mute output",))
        mic_level = re.search(r"(?:громкост\w*|уровен\w*)\s+(?:системн\w*\s+)?микрофон\w*(?:\s+(?:на|в))?\s+(\d{1,3})|(?:поставь|установи|сделай)\s+(?:громкост\w*\s+)?(?:системн\w*\s+)?микрофон\w*\s+(?:на\s+)?(\d{1,3})", t)
        if mic_level:
            raw = next((g for g in mic_level.groups() if g), "50")
            return RoutedIntent("microphone_volume", "audio", {"action":"set_input_volume","value":max(0,min(100,int(raw)))}, 791, 1.0)
        if re.search(r"\b(?:какая|покажи|скажи)\b.*\b(?:громкост\w*|уровен\w*)\b.*\bмикрофон\w*\b", t):
            return RoutedIntent("microphone_volume", "audio", {"action":"get_input_volume"}, 791, 0.99)
        micdev = re.search(r"(?:микрофон|вход)\s*[-—:]?\s*(.+)$", t)
        if micdev and re.search(r"\b(?:переключи|поставь|выбери)\b", t):
            return RoutedIntent("audio_input_device", "audio", {"action":"set_default","device_type":"input","device_name":micdev.group(1).strip()}, 790, 0.97)
        outdev = re.search(r"(?:переключи|поставь|выбери|выведи)\w*\s+(?:звук|вывод|аудио)\s+(?:на|в)\s+(.+)$", t)
        if outdev:
            return RoutedIntent("audio_output_device", "audio", {"action":"set_default","device_type":"output","device_name":outdev.group(1).strip()}, 790, 0.97)
        routeapp = re.search(r"(?:переключи|выведи)\s+(.+?)\s+(?:звук\s+)?(?:на|в)\s+(.+)$", t)
        if routeapp and re.search(r"\b(?:discord|дискорд|браузер|youtube|ютуб|steam|игр)\w*\b", routeapp.group(1)):
            device = routeapp.group(2).strip()
            audio_semantics = bool(re.search(r"\b(?:звук|аудио|вывод|наушник|колонк|динамик|гарнитур|speaker|headphone)\w*\b", t))
            not_display = not re.search(r"\b(?:экран|монитор|окно)\w*\b", device)
            if audio_semantics and not_display:
                return RoutedIntent("audio_route_app", "audio", {"action":"route_app_device","target":routeapp.group(1).strip(),"device_name":device}, 790, 0.93)

        # Per-application audio mixer and devices.
        rel = re.search(r"(?:сделай\s+)?(discord|дискорд|youtube|ютуб|браузер|chrome|хром|яндекс(?:\s+музык\w*)?|steam|стим)\s+(тише|громче)", t)
        if rel:
            target=rel.group(1).strip(); delta=-15 if rel.group(2)=="тише" else 15
            return RoutedIntent("audio_session", "audio", {"action":"change_target_volume","target":target,"value":delta}, 780, 0.99, ("не system volume",))
        m = re.search(r"(?:сделай|установи|поставь)\s+(.+?)\s+(?:на\s+)?(\d{1,3})\s*%", t)
        if m and (re.search(r"\b(?:громк\w*|звук\w*|тише|громче|discord|дискорд|youtube|ютуб|chrome|хром|браузер|steam|стим|яндекс)\b", t)):
            return RoutedIntent("audio_session", "audio", {"action": "set_volume", "target": m.group(1).strip(), "value": int(m.group(2))}, 780)
        if re.search(r"\bзаглуши\s+все\b.*\bкроме\b", t):
            except_target = re.split(r"\bкроме\b", t, maxsplit=1)[-1].strip()
            return RoutedIntent("audio_session", "audio", {"action": "mute_except", "target": except_target}, 780)
        if re.search(r"\bверни\b.*\b(?:громк|звук)\b", t):
            return RoutedIntent("audio_session", "audio", {"action": "restore_ducking"}, 780)

        mouse_abs = re.search(r"(?:чувствительност\w*|скорост\w*)\s+мыш\w*(?:\s+(?:на|в))?\s+(\d{1,2})|(?:поставь|установи|сделай)\s+(?:чувствительност\w*|скорост\w*)\s+мыш\w*\s+(?:на\s+)?(\d{1,2})", t)
        if mouse_abs:
            raw = next((g for g in mouse_abs.groups() if g), "10")
            return RoutedIntent("mouse_sensitivity", "system", {"action":"set_mouse_sensitivity","value":max(1,min(20,int(raw)))}, 792, 1.0)
        if re.search(r"\b(?:увеличь|повысь|сделай)\w*\b.*\b(?:чувствительност\w*|скорост\w*)\b.*\bмыш\w*\b|\bмыш\w*\b.*\bчувствительн\w*\b.*\b(?:выше|больше)\b", t):
            return RoutedIntent("mouse_sensitivity", "system", {"action":"change_mouse_sensitivity","value":2}, 792, 0.99)
        if re.search(r"\b(?:уменьши|понизь|сделай)\w*\b.*\b(?:чувствительност\w*|скорост\w*)\b.*\bмыш\w*\b|\bмыш\w*\b.*\bчувствительн\w*\b.*\b(?:ниже|меньше)\b", t):
            return RoutedIntent("mouse_sensitivity", "system", {"action":"change_mouse_sensitivity","value":-2}, 792, 0.99)
        if re.search(r"\b(?:какая|покажи|скажи)\b.*\b(?:чувствительност\w*|скорост\w*)\b.*\bмыш\w*\b", t):
            return RoutedIntent("mouse_sensitivity", "system", {"action":"get_mouse_sensitivity"}, 792, 0.99)

        # Desired *window state* must be distinguished from minimizing.  Users
        # naturally say «сверни обратно», «убери полный экран», «верни обычный
        # размер» after a maximize action.  Those phrases mean SW_RESTORE, not
        # minimize-to-taskbar.  Keep this above generic window/application rules.
        terse_restore = bool(re.fullmatch(
            r"(?:сверни|уменьши|верни)(?:\s+(?:его|ее|её|это))?\s+(?:обратно|назад)(?:\s+пожалуйста)?",
            t.strip(),
        ))
        restore_window = bool(
            terse_restore
            or (re.search(r"\b(?:окн\w*|его|ее|её|это|текущее|текущий)\b", t)
            and (
                re.search(r"\b(?:обычн\w*|нормальн\w*)\s+(?:размер|режим)", t)
                or re.search(r"\b(?:убери|выключи|сними|выйди\w*|верни\w*)\b.*\b(?:полноэкран\w*|полный\s+экран|максимиз\w*)", t)
                or re.search(r"\b(?:сверни|уменьши|верни)\w*\b.*\b(?:обратно|назад)\b", t)
                or re.search(r"\bне\s+(?:в\s+)?полноэкран\w*\b", t)
                or re.search(r"\bне\s+(?:в\s+)?полный\s+(?:режим|экран)\b", t)
            ))
        )
        if restore_window:
            # A title is intentionally optional. The window tool treats "active"
            # as the real foreground window, while pronoun/context resolution in
            # the Agent can still replace it with a remembered concrete title.
            title = "active"
            named = re.search(
                r"(?:окно\s+(?:с\s+)?(?:приложени\w*\s+)?)(.+?)(?:\s+(?:в|до)\s+(?:обычн|нормальн)|\s+обратно|\s+назад|$)",
                t,
            )
            if named:
                candidate = normalize_entity_phrase(named.group(1).strip())
                if (
                    candidate
                    and candidate not in {"обратно", "назад", "active", "текущее", "текущий"}
                    and not re.search(r"\b(?:обычн|нормальн|полный|полноэкран|режим|размер)\w*\b", candidate)
                ):
                    title = candidate
            return RoutedIntent(
                "restore_window", "window", {"action": "restore", "title": title, "kind": "any"},
                925, 0.995, ("не minimize", "не application.open", "не fullscreen toggle"),
            )

        # Explicit Win32 desired-state commands.  These routes intentionally do
        # not depend on the LLM: phrases such as «разверни окно Discord на весь
        # экран» must deterministically reach window.maximize.  Video/tab
        # fullscreen is excluded so YouTube F/fullscreen semantics stay separate.
        window_subject = bool(re.search(r"\bокн\w*\b", t))
        app_fullscreen = bool(
            re.search(r"\b(?:на|во)\s+(?:весь|полный)\s+экран\b|\bмаксимиз\w*\b", t)
            and not re.search(r"\b(?:видео|ролик|плеер|вкладк\w*)\b", t)
        )
        wants_maximize = bool(
            re.search(r"\b(?:разверни|разворачивай|максимизируй|увеличь)\w*\b", t)
            and (window_subject or app_fullscreen)
        )
        if wants_maximize:
            title = "active"
            named = re.search(
                r"(?:разверни|разворачивай|максимизируй|увеличь)\w*\s+(?:окн\w*\s+)?(.+?)"
                r"(?:\s+(?:на|во)\s+(?:весь|полный)\s+экран|\s+полностью|$)",
                t, flags=re.I,
            )
            if named:
                candidate = normalize_entity_phrase(named.group(1).strip())
                if candidate and candidate not in {"его", "ее", "её", "это", "текущее", "текущий", "active"}:
                    title = candidate
            return RoutedIntent(
                "maximize_window", "window", {"action": "maximize", "title": title, "kind": "any"},
                924, 0.995, ("Win32 maximize", "не F11", "не video fullscreen"),
            )

        wants_minimize = bool(
            (re.search(r"\b(?:сверни|минимизируй)\w*\b", t) and not re.search(r"\b(?:вкладк\w*|панел\w*\s+вклад)\b", t))
            or re.search(r"\bубери\w*\b.*\b(?:окн\w*|приложени\w*)\b.*\b(?:панел\w*\s+задач|taskbar)\b", t)
        )
        if wants_minimize:
            title = "active"
            named = re.search(
                r"(?:сверни|минимизируй)\w*\s+(?:окн\w*\s+)?(.+?)(?:\s+(?:в|на)\s+панел\w*\s+задач|$)",
                t, flags=re.I,
            )
            if named:
                candidate = normalize_entity_phrase(named.group(1).strip())
                if candidate in {"активное", "активное окно", "текущее", "текущее окно", "текущий", "текущий окно", "окно"}:
                    title = "active"
                elif candidate and candidate not in {"его", "ее", "её", "это", "active"}:
                    title = candidate
            return RoutedIntent(
                "minimize_window", "window", {"action": "minimize", "title": title, "kind": "any"},
                923, 0.995, ("Win32 minimize", "не close", "не browser tab"),
            )

        # Multiple windows of the same application must be matched before the
        # generic move rule so «второе окно Chrome» retains its ordinal instance.
        mw = re.search(r"перенеси\s+(перв\w*|втор\w*|трет\w*|четверт\w*|пят\w*|\d+)\s+(?:окно\s+)?(.+?)\s+на\s+(перв\w*|втор\w*|трет\w*|четверт\w*|пят\w*|\d+)\s+(?:монитор|экран)", t)
        if mw:
            idx=_ordinal_value(mw.group(1)); mon=_ordinal_value(mw.group(3))
            if idx and mon:
                return RoutedIntent("move_window_instance","window_advanced",{"action":"move_instance","query":mw.group(2).strip(),"index":idx,"monitor":mon},900,0.99,("не application.open",))

        # Generic physical-window move. Handles natural forms such as
        # «перенеси окно с приложением Яндекс Музыка на третий экран», including
        # conservative entity correction for common ASR slips («андекс музыка»).
        if explicit_window_move(t):
            move = re.match(
                r"^(?:перенеси|перемести|перетащи|отправь)\s+(.+?)\s+(?:на|в)\s+"
                r"(перв\w*|втор\w*|трет\w*|четверт\w*|пят\w*|шест\w*|седьм\w*|восьм\w*|\d+)\s+"
                r"(?:экран\w*|монитор\w*|диспле\w*)(?:\s+(?:пожалуйста|сейчас|быстро|аккуратно|прямо сейчас))?$", t, flags=re.I,
            )
            if move:
                subject = move.group(1).strip()
                source_monitor = 0
                source = re.search(
                    r"\s+(?:со|с)\s+(перв\w*|втор\w*|трет\w*|четверт\w*|пят\w*|шест\w*|седьм\w*|восьм\w*|\d+)\s+(?:экрана?\w*|монитора?\w*|дисплея?\w*)$",
                    subject, flags=re.I,
                )
                if source:
                    source_monitor = monitor_number(source.group(1)) or 0
                    subject = subject[:source.start()].strip()
                active_subject = bool(re.fullmatch(r"(?:активн\w*|текущ\w*)(?:\s+окн\w*)?", subject, flags=re.I))
                subject = re.sub(r"^(?:это|этот|эту|текущее|текущий|текущую)\s+", "", subject, flags=re.I)
                subject = re.sub(r"^(?:окно\s+)?(?:с\s+)?(?:приложени\w*|программ\w*)\s+", "", subject, flags=re.I)
                subject = re.sub(r"^окно\s+", "", subject, flags=re.I).strip()
                subject = "active" if active_subject else (normalize_entity_phrase(subject) or "active")
                mon = monitor_number(move.group(2))
                if mon:
                    args: dict[str, Any] = {"action": "move_to_monitor", "title": subject, "monitor": mon, "kind": "any"}
                    if source_monitor:
                        args["source_monitor"] = source_monitor
                    return RoutedIntent("move_window", "window", args, 910, 0.995,
                                        ("никогда не application.open", "никогда не создавать новое окно вместо перемещения"))

        wc = re.search(r"закрой\s+окно\s+(.+?)\s+с\s+(.+)$", t)
        if wc:
            return RoutedIntent("close_window_content","window_advanced",{"action":"close_with_content","query":wc.group(1).strip(),"content":wc.group(2).strip()},900,0.97)
        keep = re.search(r"оставь\s+только\s+окно\s+(.+?)\s+с\s+(.+)$", t)
        if keep:
            return RoutedIntent("keep_window_content","window_advanced",{"action":"keep_only_content","query":keep.group(1).strip(),"content":keep.group(2).strip()},900,0.97)

        # Browser context / advanced tab controls.
        direct_new = re.search(r"(?:перемести|перенеси)\s+(.+?)\s+(?:в|на)\s+нов\w*\s+окн", t)
        if direct_new:
            return RoutedIntent("browser_advanced", "browser_advanced", {"action":"move_to_new_window","query":direct_new.group(1).strip()}, 855, 0.97)
        if re.search(r"\bвкладк", t):
            # Ordinal tab commands are positional, not title searches.  This also
            # covers terse follow-ups such as «третья вкладка» after a browser
            # command, which previously reached the LLM as a fake title.
            ordinal = re.search(
                r"\b(перв\w*|втор\w*|трет\w*|четверт\w*|пят\w*|шест\w*|седьм\w*|восьм\w*|девят\w*|десят\w*|\d+)\s+вкладк\w*\b",
                t,
            )
            if ordinal:
                index = _ordinal_value(ordinal.group(1))
                if index:
                    action = "close_tab" if re.search(r"\bзакрой\w*\b", t) else "activate_tab"
                    return RoutedIntent("browser_tab", "browser", {"action": action, "index": index, "browser": ""}, 865, 0.995,
                                        ("не activate_tab_title",))
            if re.search(r"\b(?:сгруппируй|группируй|объедини)\b", t):
                return RoutedIntent("browser_advanced", "browser_advanced", {"action": "group_by_topic"}, 855)
            if re.search(r"\bвосстанов\w*\b.*\bзакрыт", t):
                return RoutedIntent("browser_advanced", "browser_advanced", {"action": "restore_closed"}, 855)
            if re.search(r"\bзакрой\w*\b.*\b(?:все|остальн)\b.*\bкроме\b.*\b(?:эт|текущ)", t):
                return RoutedIntent("browser_advanced", "browser_advanced", {"action": "close_others"}, 855)
            if re.search(r"\bзакреп", t):
                return RoutedIntent("browser_advanced", "browser_advanced", {"action": "pin_current"}, 855)
            if re.search(r"\b(?:новое|новый)\s+окн", t) and re.search(r"\bперемест|перенес", t):
                q=""
                qm=re.search(r"(?:перемести|перенеси)\s+(?:вкладк\w*\s+)?(.+?)\s+(?:в|на)\s+нов\w*\s+окн", t)
                if qm: q=qm.group(1).strip()
                return RoutedIntent("browser_advanced", "browser_advanced", {"action": "move_to_new_window", "query": q}, 855)
            topic = re.search(r"(?:перейди|вернись|открой)\s+(?:на\s+)?вкладк\w*\s+(?:с\s+|где\s+)?(.+)$", t)
            if topic:
                return RoutedIntent("browser_advanced", "browser_advanced", {"action": "activate_by_topic", "query": topic.group(1).strip()}, 855)

        if re.search(r"\b(?:верни|возврати)\s+(?:это\s+)?окно\s+(?:обратно|назад|на прежнее место)\b", t):
            return RoutedIntent("history", "history", {"action": "undo", "count": 1}, 840, 0.99, ("не window.move",))

        # History/undo.
        if re.search(r"\b(?:что ты сейчас сделал|что сейчас сделал|последн\w* действ)\b", t):
            return RoutedIntent("history", "history", {"action": "recent", "limit": 5}, 840)
        undo = re.search(r"\bотмени\w*\b(?:\s+последн\w*)?(?:\s+(\d+|три|два))?\s*(?:операц|действ)?", t)
        if undo and not re.search(r"\b(?:выключ|shutdown)\b", t):
            raw = undo.group(1) or "1"; count = 3 if raw == "три" else 2 if raw == "два" else int(raw) if raw.isdigit() else 1
            return RoutedIntent("history", "history", {"action": "undo", "count": count}, 840)

        # Screenshots: monitor/window/app/game/save/clipboard options are deterministic.
        # Natural variants such as «сфотографируй второй экран» do not need the
        # literal word «скриншот». An explicit app/game target is captured by its
        # window bounds; ordinal monitors are kept separate from app instances.
        screenshot_request = bool(
            re.search(r"\b(?:сделай|сними|создай)\w*.*\bскрин(?:шот)?\w*", t)
            or re.search(r"\b(?:сфотографируй|сфоткай|заскринь|заскриншоть|снимок)\w*", t)
            or re.search(r"\b(?:capture|screenshot)\b", t)
        )
        if screenshot_request:
            monitor = 0
            ordinal_map = {
                "первый": 1, "первого": 1, "первом": 1,
                "второй": 2, "второго": 2, "втором": 2,
                "третий": 3, "третьего": 3, "третьем": 3,
                "четвертый": 4, "четвёртый": 4, "четвертого": 4, "четвёртого": 4,
                "пятый": 5, "пятого": 5,
            }
            mm = re.search(r"(?:монитор|экран)\w*\s*(?:номер\s*)?(\d+)", t)
            if not mm:
                mm = re.search(r"\b(перв(?:ый|ого|ом)|втор(?:ой|ого|ом)|трет(?:ий|ьего|ьем)|четв[её]рт(?:ый|ого|ом)|пят(?:ый|ого|ом))\s+(?:монитор|экран)\w*", t)
            if mm:
                raw_monitor = mm.group(1).casefold().replace("ё", "е")
                monitor = int(raw_monitor) if raw_monitor.isdigit() else ordinal_map.get(raw_monitor, 0)

            explicit_window = bool(re.search(r"\b(?:окн\w*|приложени\w*|программ\w*|игр\w*)\b", t))
            action = "capture_window" if explicit_window else "capture"
            args: dict[str, Any] = {"action": action, "clipboard": bool(re.search(r"\bбуфер", t))}
            if monitor and action == "capture":
                args["monitor"] = str(monitor)   # HIST-067: the screen schema types monitor as a string
            dest = re.search(r"(?:сохрани|положи)\s+(?:его\s+)?(?:в|на)\s+(.+?)(?:\s+и\s+|$)", t)
            if dest:
                args["destination"] = dest.group(1).strip()
            if action == "capture_window":
                win = re.search(r"(?:окн\w*|приложени\w*|программ\w*|игр\w*)\s+(?:с\s+)?(.+?)(?:\s+и\s+|$)", t)
                if win:
                    title = re.sub(r"^(?:под\s+названи\w*\s+)?", "", win.group(1), flags=re.I).strip(' \"«».,;')
                    if title:
                        args["title"] = title
            return RoutedIntent("screenshot", "screen", args, 920, 0.995, ("explicit screenshot", "not media"))

        # Smart cross-app transfer.
        if re.search(r"\b(?:возьми|скопируй)\b.*\b(?:ссылк|адрес)\b.*\b(?:discord|дискорд)\b", t):
            channel=""; server=""
            cm=re.search(r"(?:в|на)\s+канал\s+[«\"']?(.+?)[»\"']?(?:\s+на\s+сервере\s+[«\"']?(.+?)[»\"']?)?$", t)
            if cm:
                channel=cm.group(1).strip(); server=(cm.group(2) or "").strip()
            topic="youtube" if "youtube" in t or "ютуб" in t else "chrome" if "chrome" in t or "хром" in t else ""
            return RoutedIntent("transfer_url","transfer",{"action":"page_url_to_discord","channel":channel,"server":server,"topic":topic},825,0.96)
        if re.search(r"\b(?:перенеси|прикрепи|отправь)\b.*\b(?:этот|выбранн\w*)\s+файл\b.*\b(?:discord|дискорд)\b", t):
            return RoutedIntent("transfer_selected_file","transfer",{"action":"selected_explorer_file_to_discord"},825,0.96)
        if re.search(r"\b(?:прикрепи|отправь)\b.*\bпоследн\w*\s+скриншот\b.*\b(?:discord|дискорд)\b", t):
            return RoutedIntent("transfer_screenshot","transfer",{"action":"last_screenshot_to_discord"},825,0.96)

        # Voice text editing in the active editor.
        dictate = re.match(r"^(?:напиши|введи|напечатай)\s*[:,-]?\s*(.+)$", t)
        if dictate and not re.search(r"\b(?:файл|документ|заметк|письм|сообщени)\b", t):
            payload = dictate.group(1)
            raw = re.match(r"^\s*(?:напиши|введи|напечатай)\s*[:,-]?\s*(.+?)\s*$", str(text or ""), re.I | re.S)
            if raw and norm(raw.group(1)) == norm(payload):
                payload = raw.group(1)        # NEO 182: keep the user's own casing and punctuation
            return RoutedIntent("text_edit", "text_edit", {"action": "dictate", "text": payload}, 730)
        if re.search(r"\bисправь\b.*\b(?:орфограф|текст|выделен)", t):
            return RoutedIntent("text_edit", "text_edit", {"action": "fix_selection"}, 730)
        if re.search(r"\bвыдели\b.*\bпредыдущ\w*\s+абзац", t):
            return RoutedIntent("text_edit", "text_edit", {"action": "select_previous_paragraph"}, 730)

        # HOTFIX16: typed game requests must never be swallowed by the generic
        # application route. This restores the mature behaviour of commands like
        # «запусти игру DarkTide» / «launch game DarkTide»: the noun ``игра`` is a
        # semantic type marker and the Steam/Epic resolver must receive kind=game.
        typed_game = re.match(
            r"^(?:открой|запусти|найди|open|launch|run|start)\s+(?:мне\s+)?(?:игр\w*|game)\s+(.+)$",
            t, re.I,
        )
        if typed_game:
            game_name = typed_game.group(1).strip()
            if game_name and len(game_name) < 120:
                return RoutedIntent(
                    "application_game", "application",
                    {"action": "open", "name": game_name, "kind": "game"},
                    690, 0.99, ("typed game target", "не application kind=app"),
                )

        # Flexible application wording.  Exclude window/file/tab/media operations so
        # "поставь видео на паузу" can never become an application launch.
        if not re.search(
            r"\b(?:вкладк|окн|окошк|файл|папк|видео|музык|трек|песн|пауз|громк|игр|"
            r"систем|комп|пк|желез|нагруз|температур|диагност|задерж|латент|процессор|видеокарт)\w*\b|\bgame\b",
            t,
        ):
            app_patterns = [
                r"^(?:открой|запусти|покажи|выведи)\s+(?:мне\s+)?(.+)$",
                r"^давай\s+(.+)$",
                r"^(.+?)\s*,?\s+(?:покажи|открой|запусти|выведи)(?:\s+наверх)?$",
            ]
            for pat in app_patterns:
                m = re.match(pat, t)
                if m:
                    name = m.group(1).strip()
                    # NEO 182: «открой приложение Calculator» (also after translation from FR/ES)
                    # names the app, not «приложение calculator»; keep the user's own spelling.
                    name = re.sub(r"^(?:(?:l['’]|la\s+|le\s+|el\s+|il\s+|lo\s+|a\s+|o\s+|the\s+|die\s+|das\s+)?"
                                  r"(?:приложени[еяю]|программ[ауы]|application|app)\s+)", "", name).strip(" ?!.…")
                    original = re.search(re.escape(name), str(text or ""), re.I) if name else None
                    if original:
                        name = original.group(0)
                    if name.casefold() in _DEICTIC182:
                        return None          # «открой это / ouvre ça» without context: ask, don't guess a name
                    if name and len(name) < 80:
                        return RoutedIntent("application", "application", {"action": "open", "name": name, "kind": "app"}, 620, 0.96,
                                            ("не window.move", "не media", "не filesystem"))

        return None


_DEICTIC182 = frozenset({
    "это", "этот", "эту", "эти", "то", "его", "её", "ее", "их", "оно",
    "this", "that", "it", "these", "those", "das", "dies", "es", "ça", "ca", "cela", "ceci",
    "esto", "eso", "aquello", "lo", "la", "le", "questo", "quello", "isto", "isso", "aquilo",
})

_GLOBAL = HybridRouter()


def hybrid_router() -> HybridRouter:
    return _GLOBAL
