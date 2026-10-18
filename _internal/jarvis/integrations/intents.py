# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from jarvis.wake_matcher import match_wake_phrase
from jarvis.runtime.semantics import (
    explicit_video, explicit_music, explicit_discord_media, explicit_yandex_music,
    pause_like, resume_like, looks_like_browser_search,
)


@dataclass
class ParsedIntegrationIntent:
    domain: str
    action: str
    arguments: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0


def _n(value: str) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = re.sub(r"\s+", " ", text).strip(" .,!?:;\"'«»")
    return text


def _strip_wake(text: str, *, wake_phrase: str = "", wake_aliases: Iterable[str] | None = None) -> str:
    t = _n(text)
    matched, remainder, _score = match_wake_phrase(
        t, wake_phrase, aliases=wake_aliases, threshold=0.90, allow_standalone_aliases=False
    )
    return _n(remainder) if matched else t


def _clean_tail(value: str) -> str:
    return re.sub(r"\s+(?:пожалуйста|плиз)$", "", str(value or "").strip(), flags=re.I).strip(" ,.;:\"'«»")


def _strip_yandex_domain_phrase(value: str) -> str:
    """Remove a spoken Yandex-Music location from the song payload itself."""
    text = str(value or "").strip()
    text = re.sub(
        r"(?:^|\s)(?:в|через|из)\s+(?:приложени\w*\s+)?(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)(?=\s|$)",
        " ", text, flags=re.I,
    )
    return re.sub(r"\s+", " ", text).strip(" ,.;:\"'«»")


def _extract_song_payload(payload: str) -> tuple[str, str]:
    """Split natural 'title by artist' wording without requiring a rigid phrase."""
    value = _clean_tail(payload)
    value = re.sub(r"^(?:песн\w*|трек\w*|композиц\w*|музык\w*)\s+", "", value, flags=re.I).strip()
    # Strong explicit artist separators.  'от' is intentionally accepted only
    # when both sides are non-empty, so a title containing the word does not
    # instantly disappear.
    patterns = [
        r"^(.+?)\s+(?:исполнител\w*|артист\w*|автор\w*)\s+(.+)$",
        r"^(.+?)\s+(?:у\s+исполнител\w*|у\s+автор\w*)\s+(.+)$",
        r"^(.+?)\s+от\s+(.+)$",
        r"^(.+?)\s+(?:by)\s+(.+)$",
    ]
    for pattern in patterns:
        m = re.match(pattern, value, flags=re.I)
        if m:
            return _clean_tail(m.group(1)), _clean_tail(m.group(2))
    return value, ""


def parse_yandex_music_intent(
    text: str,
    *,
    active_domain: str = "",
    wake_phrase: str = "",
    wake_aliases: Iterable[str] | None = None,
) -> ParsedIntegrationIntent | None:
    raw = _strip_wake(text, wake_phrase=wake_phrase, wake_aliases=wake_aliases)
    if not raw:
        return None
    t = _n(raw)
    active = _n(active_domain) in {"yandex_music", "yandex music", "яндекс музыка", "music"}
    explicit = bool(
        re.search(r"\b(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music|ям\b|моя\s+волна|мою\s+волну|плейлист\w*|playlist\w*)\b", t)
    )

    # These words identify another media domain and must never be stolen.
    if re.search(r"\b(?:youtube|ютуб|видео|discord|дискорд|музыкальн\w*\s+бот\w*|dj\s*bot)\b", t) and not explicit:
        return None

    if re.fullmatch(r"(?:открой|запусти|включи|открой\s+мне|запусти\s+мне)\s+(?:приложени\w*\s+)?(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music|музык\w*)", t):
        return ParsedIntegrationIntent("yandex_music", "open")

    m_playlist = re.search(r"\b(?:плейлист\w*|playlist\w*)\s+(?:с\s+названи\w*\s+|под\s+названи\w*\s+)?(.+)$", t, flags=re.I)
    if m_playlist and (explicit or active):
        value = _strip_yandex_domain_phrase(_clean_tail(m_playlist.group(1)))
        value = re.sub(r"\s+(?:в|через)\s+(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s*$", "", value, flags=re.I).strip()
        if value:
            return ParsedIntegrationIntent("yandex_music", "playlist", {"playlist": value, "query": value}, 0.995)

    if re.search(r"\b(?:моя\s+волна|мою\s+волну)\b", t):
        return ParsedIntegrationIntent("yandex_music", "my_wave")

    # v3.9: state-changing media phrases outrank track search.  In particular
    # “поставь трек/музыку на паузу” is a pause request, never a request to
    # search for a song literally named “на паузу”.  Explicit music nouns are
    # enough to select the native music domain even when there is no prior
    # Yandex-Music conversational context.
    music_object = r"(?:музык\w*|трек\w*|песн\w*|композиц\w*|аудио\w*)"
    if re.fullmatch(
        rf"(?:(?:поставь|поставить)\s+(?:текущ(?:ую|ий)\s+)?{music_object}\s+на\s+паузу|"
        rf"(?:приостанови|останови|остановить)\s+(?:текущ(?:ую|ий)\s+)?{music_object}|"
        rf"(?:текущ(?:ую|ий)\s+)?{music_object}\s+(?:на\s+паузу|поставь\s+на\s+паузу))",
        t, flags=re.I,
    ):
        return ParsedIntegrationIntent("yandex_music", "pause", confidence=1.0)
    if re.fullmatch(
        rf"(?:(?:продолжи|возобнови|возобновить|доиграй|верни)\s+(?:воспроизведени\w*\s+)?(?:текущ(?:ую|ий)\s+)?{music_object}|"
        rf"(?:включи|воспроизведи)\s+(?:снова\s+)?(?:текущ(?:ую|ий)\s+)?{music_object})",
        t, flags=re.I,
    ):
        return ParsedIntegrationIntent("yandex_music", "play", confidence=1.0)

    # Personal library is API-only. “Мои треки/песни” maps to the account's
    # liked-tracks list and never opens the desktop Yandex Music app.
    if re.fullmatch(
        r"(?:включи|запусти|воспроизведи|открой|поставь)\s+(?:мои\s+)?(?:треки|песни|музыку)"
        r"(?:\s+в\s+(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music))?"
        r"(?:\s+и\s+(?:включи|запусти|воспроизведи|поставь)\s+(?:перв(?:ый|ую)\s+)?(?:трек|песн\w*|композиц\w*))?",
        t,
    ):
        return ParsedIntegrationIntent("yandex_music", "my_tracks")

    if explicit or active:
        # State toggles accept the destination in any natural position, e.g.
        # «включи в Яндекс Музыке перемешивание» and
        # «выключи перемешивание в Яндекс Музыке».  Strip only the service
        # scaffolding before applying the desired-state grammar.
        state_t = re.sub(
            r"(?:^|\s)(?:в|во|через|для|на)?\s*(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)(?=\s|$)",
            " ", t, flags=re.I,
        )
        state_t = re.sub(r"\s+", " ", state_t).strip()
        if re.fullmatch(r"(?:включи|активируй|запусти)\s+(?:режим\s+)?(?:перемешиван\w*|шаффл|shuffle)", state_t):
            return ParsedIntegrationIntent("yandex_music", "shuffle_on")
        if re.fullmatch(r"(?:выключи|отключи|убери)\s+(?:режим\s+)?(?:перемешиван\w*|шаффл|shuffle)", state_t):
            return ParsedIntegrationIntent("yandex_music", "shuffle_off")
        if re.fullmatch(r"(?:(?:включи|активируй|запусти)\s+)?(?:режим\s+)?(?:повтор\w*|репит|repeat)\s+(?:текущ\w*\s+)?(?:трек\w*|песн\w*)", state_t):
            return ParsedIntegrationIntent("yandex_music", "repeat_track")
        if re.fullmatch(r"(?:(?:включи|активируй|запусти)\s+)?(?:режим\s+)?(?:повтор\w*|репит|repeat)\s+(?:плейлист\w*|очеред\w*|контекст\w*)", state_t):
            return ParsedIntegrationIntent("yandex_music", "repeat_playlist")
        if re.fullmatch(r"(?:включи|активируй|запусти)\s+(?:режим\s+)?(?:повтор\w*|репит|repeat)", state_t):
            return ParsedIntegrationIntent("yandex_music", "repeat_track")
        if re.fullmatch(r"(?:выключи|отключи|убери)\s+(?:режим\s+)?(?:повтор\w*|репит|repeat)", state_t):
            return ParsedIntegrationIntent("yandex_music", "repeat_off")

    if explicit or active:
        m = re.fullmatch(r"(?:поставь|установи|сделай)?\s*громкост\w*(?:\s+(?:музык\w*|плеер\w*))?\s+(?:на\s+)?(\d{1,3})\s*(?:%|процент\w*)?", t)
        if m:
            return ParsedIntegrationIntent("yandex_music", "set_volume", {"volume": max(0, min(100, int(m.group(1))))})
        if re.fullmatch(r"(?:сделай|сделать|сделай\s+музык\w*)?\s*(?:музык\w*\s+)?(?:громче|погромче)", t):
            return ParsedIntegrationIntent("yandex_music", "volume_up", {"step": 10})
        if re.fullmatch(r"(?:сделай|сделать|сделай\s+музык\w*)?\s*(?:музык\w*\s+)?(?:тише|потише)", t):
            return ParsedIntegrationIntent("yandex_music", "volume_down", {"step": 10})

    # Like/unlike/dislike is safe only when the Yandex domain is explicit or
    # already the conversational media context. Strip only a trailing explicit
    # Yandex destination before matching the preference grammar.
    if explicit or active:
        preference_t = re.sub(
            r"\s+(?:в|через)\s+(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s*$",
            "", t, flags=re.I,
        ).strip()
        if re.fullmatch(r"(?:дизлайк(?:ни)?|дислайк(?:ни)?|dislike|thumbs\s+down|мне\s+не\s+нравится)(?:\s+(?:эт(?:от|у)|текущ\w*)\s+(?:трек|песн\w*))?", preference_t):
            return ParsedIntegrationIntent("yandex_music", "dislike")
        if re.fullmatch(r"(?:сними|убери|удали)\s+(?:этот\s+)?лайк|(?:убери|удали)\s+(?:из\s+)?(?:любим\w*|мне\s+нравится)|(?:больше\s+не\s+нравится)", preference_t):
            return ParsedIntegrationIntent("yandex_music", "unlike")
        if re.fullmatch(r"(?:лайк|(?:a\s+)?like|лайкни(?:\s+(?:эт(?:от|у)|текущ\w*)\s+(?:трек|песн\w*))?|залайкай(?:\s+(?:эт(?:от|у)|текущ\w*)\s+(?:трек|песн\w*))?|поставь\s+(?:этому\s+треку\s+)?лайк|мне\s+нравится(?:\s+(?:эт(?:от|а)|эта)\s+(?:трек|песн\w*))?|добавь\s+(?:эт(?:у|от)\s+)?(?:песн\w*|трек\w*)?\s*(?:в\s+)?(?:любим\w*|мне\s+нравится)|добавь\s+в\s+любим\w*)", preference_t):
            return ParsedIntegrationIntent("yandex_music", "like")

        if re.fullmatch(r"(?:следующ\w*|дальше|скип|пропусти|следующ\w*\s+(?:трек|песн\w*))", t):
            return ParsedIntegrationIntent("yandex_music", "next")
        if re.fullmatch(r"(?:предыдущ\w*|назад|верни\s+предыдущ\w*|предыдущ\w*\s+(?:трек|песн\w*))", t):
            return ParsedIntegrationIntent("yandex_music", "previous")
        if re.fullmatch(r"(?:пауза|останови|приостанови|поставь\s+на\s+паузу|останови\s+музык\w*|поставь\s+музык\w*\s+на\s+паузу)", t):
            return ParsedIntegrationIntent("yandex_music", "pause")
        if re.fullmatch(r"(?:продолжи|воспроизведи|играй|возобнови|продолжи\s+музык\w*|верни\s+музык\w*|продолжай)", t):
            return ParsedIntegrationIntent("yandex_music", "play")
        if re.fullmatch(r"(?:что\s+(?:сейчас\s+)?играет|что\s+за\s+(?:песня|трек)|какая\s+(?:песня|музыка)\s+играет|назови\s+(?:песню|трек))", t):
            return ParsedIntegrationIntent("yandex_music", "status")

    # Direct explicit Yandex destination with a named payload. HybridRouter
    # normally catches this first, but keep the mature parser equally strict.
    if explicit:
        direct_yandex_patterns = (
            r"^(?:в|через)\s+(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s+(?:включи|поставь|запусти|вруби|воспроизведи|сыграй)\w*\s+(?:песн\w*|трек\w*|композиц\w*)?\s*(.+)$",
            r"^(?:включи|поставь|запусти|вруби|воспроизведи|сыграй)\w*\s+(?:в|через)\s+(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s+(?:песн\w*|трек\w*|композиц\w*)?\s*(.+)$",
            r"^(?:включи|поставь|запусти|вруби|воспроизведи|сыграй)\w*\s+(?:песн\w*|трек\w*|композиц\w*)?\s*(.+?)\s+(?:в|через)\s+(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s*$",
        )
        transportish = bool(
            pause_like(t, strong_domain=True) or resume_like(t)
            or re.search(r"\b(?:следующ\w*|предыдущ\w*|дальше|пропусти|скипни|останови|стопни|прекрати|выключи)\w*\b", t)
        )
        if not transportish:
            for pattern in direct_yandex_patterns:
                m_direct = re.match(pattern, t, flags=re.I)
                if m_direct:
                    payload = _strip_yandex_domain_phrase(_clean_tail(m_direct.group(1)))
                    payload = re.sub(r"^(?:песн\w*|трек\w*|композиц\w*)\s+", "", payload, flags=re.I).strip()
                    if payload:
                        return ParsedIntegrationIntent("yandex_music", "play_track", {"query": payload, "artist": ""}, 0.995)

    # v3.9: searching for a concrete song is deliberately explicit.  A phrase
    # such as “поставь трек на паузу” must never be interpreted as a song title.
    # To start a specific result the user says “найди песню/трек/музыку X”
    # (optionally “…и включи её”).  “Мои треки” remains a separate library action.
    search_patterns = [
        r"^(?:найди|поищи|отыщи)\s+(?:в\s+(?:приложени\w*\s+)?(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s+)?(?:песн\w*|трек\w*|композиц\w*|музык\w*)\s+(.+?)(?:\s+и\s+(?:включи|запусти|воспроизведи|поставь|вруби)(?:\s+(?:ее|её|его|этот\s+трек|эту\s+песню))?)?$",
        r"^(?:в\s+(?:приложени\w*\s+)?(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s+)?(?:найди|поищи|отыщи)\s+(?:песн\w*|трек\w*|композиц\w*|музык\w*)\s+(.+?)(?:\s+и\s+(?:включи|запусти|воспроизведи|поставь|вруби)(?:\s+(?:ее|её|его|этот\s+трек|эту\s+песню))?)?$",
    ]
    for pattern in search_patterns:
        m = re.match(pattern, t, flags=re.I)
        if not m:
            continue
        payload = _strip_yandex_domain_phrase(_clean_tail(m.group(1)))
        # Remove a trailing service phrase that appears after the spoken title:
        # “найди песню Ревную в Яндекс Музыке и включи её”.
        payload = re.sub(
            r"\s+(?:в|через)\s+(?:приложени\w*\s+)?(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\s*$",
            "", payload, flags=re.I,
        ).strip()
        title, artist = _extract_song_payload(payload)
        if title and not re.fullmatch(r"(?:на\s+паузу|паузу|пауза|стоп)", title, flags=re.I):
            return ParsedIntegrationIntent(
                "yandex_music", "play_track", {"query": title, "artist": artist}, 0.99
            )

    # A bare “найди X” after a previous music command is intentionally *not*
    # converted into a song search.  The object word keeps the boundary between
    # desktop search, files, applications and music deterministic.
    return None


def _extract_server_channel(t: str, kind: str) -> tuple[str, str] | None:
    kind_pattern = r"текстов\w*\s+канал\w*|чат\w*" if kind == "text" else r"голосов\w*\s+канал\w*|войс\w*|voice\s+channel"
    # 'на сервере A открой текстовый канал B'
    m = re.search(
        rf"(?:на\s+)?сервер\w*\s+(.+?)\s+(?:открой|перейди|переключись|зайди|войди|подключись|подключи\s+меня)\s+(?:в\s+|на\s+)?(?:{kind_pattern})(?:\s+(?:под\s+названи\w*|с\s+названи\w*))?\s+(.+)$",
        t,
        flags=re.I,
    )
    if m:
        return _clean_tail(m.group(1)), _clean_tail(m.group(2))
    return None


def parse_discord_intent(
    text: str,
    *,
    active_domain: str = "",
    default_server: str = "",
    default_music_channel: str = "",
    default_voice_channel: str = "",
    known_servers: Iterable[str] | None = None,
    wake_phrase: str = "",
    wake_aliases: Iterable[str] | None = None,
) -> ParsedIntegrationIntent | None:
    raw = _strip_wake(text, wake_phrase=wake_phrase, wake_aliases=wake_aliases)
    if not raw:
        return None
    t = _n(raw)
    active = _n(active_domain) == "discord"
    discord_word = r"(?:discord\w*|discort\w*|diskord\w*|diskort\w*|дискорд\w*|дискорт\w*|дискор\w*)"
    explicit = bool(re.search(rf"\b(?:{discord_word}|музыкальн\w*\s+бот\w*|дидже\w*\s+бот\w*|dj\s*bot)\b", t))

    # Semantic firewall: stale Discord conversational context is never allowed to
    # steal a fresh explicit YouTube/video command or a browser-search command.
    # This also catches narrow ASR slips such as «поставь видео на паулу».
    if explicit_video(t) and not explicit:
        return None
    if looks_like_browser_search(t) and not explicit:
        return None

    # «открой Discord и ...» is a single user goal; every deeper Discord action
    # already focuses/launches the client, so the prefix can be removed safely.
    t = re.sub(r"^(?:открой|запусти)\s+(?:приложени\w*\s+)?(?:discord|дискорд)\s+(?:и\s+)?", "", t).strip() or t
    domainish = explicit or active or bool(re.search(r"\b(?:сервер\w*|текстов\w*\s+канал\w*|голосов\w*\s+канал\w*|музыкальн\w*\s+бот\w*|dj\s*bot)\b", t))
    if not domainish:
        return None

    # Desired-state controls for JARVIS' own background music bot. Explicit
    # Discord wording always owns the media command. When Discord is already the
    # active integration, short follow-ups such as "следующий трек", "пауза" and
    # "продолжи" are accepted without repeating the destination.
    # Shuffle is a queue policy, not a named track. Resolve it before every
    # playlist/transport branch so «выключи ... перемешивание» cannot be mistaken
    # for music_stop merely because it contains «выключи».
    discord_state_t = re.sub(rf"(?:^|\s)(?:в|во|через|для|на)?\s*{discord_word}(?=\s|$)", " ", t, flags=re.I)
    discord_state_t = re.sub(r"\s+", " ", discord_state_t).strip()
    if explicit or active:
        if re.fullmatch(r"(?:включи|активируй|запусти)\s+(?:режим\s+)?(?:перемешиван\w*|шаффл|shuffle)", discord_state_t):
            return ParsedIntegrationIntent("discord", "music_shuffle_on", {"server": default_server}, 0.999)
        if re.fullmatch(r"(?:выключи|отключи|убери)\s+(?:режим\s+)?(?:перемешиван\w*|шаффл|shuffle)", discord_state_t):
            return ParsedIntegrationIntent("discord", "music_shuffle_off", {"server": default_server}, 0.999)
        if re.fullmatch(r"(?:(?:включи|активируй|запусти)\s+)?(?:режим\s+)?(?:повтор\w*|репит|repeat)\s+(?:текущ\w*\s+)?(?:трек\w*|песн\w*)", discord_state_t):
            return ParsedIntegrationIntent("discord", "music_repeat_track", {"server": default_server}, 0.999)
        if re.fullmatch(r"(?:(?:включи|активируй|запусти)\s+)?(?:режим\s+)?(?:повтор\w*|репит|repeat)\s+(?:плейлист\w*|очеред\w*|контекст\w*)", discord_state_t):
            return ParsedIntegrationIntent("discord", "music_repeat_playlist", {"server": default_server}, 0.999)
        if re.fullmatch(r"(?:выключи|отключи|убери)\s+(?:режим\s+)?(?:повтор\w*|репит|repeat)", discord_state_t):
            return ParsedIntegrationIntent("discord", "music_repeat_off", {"server": default_server}, 0.999)
    if explicit and re.search(r"\b(?:моя\s+волна|мою\s+волну)\b", t):
        return ParsedIntegrationIntent("discord", "music_my_wave", {"server": default_server}, 0.998)
    if explicit and re.search(r"\b(?:плейлист\w*|playlist\w*)\b", t):
        m_pl = re.search(r"\b(?:плейлист\w*|playlist\w*)\s+(?:с\s+названи\w*\s+|под\s+названи\w*\s+)?(.+)$", t, flags=re.I)
        if m_pl:
            value = _clean_tail(m_pl.group(1))
            value = re.sub(r"\s+(?:в|через)\s+(?:discord|дискорд)\w*\s*$", "", value, flags=re.I).strip()
            if value:
                return ParsedIntegrationIntent("discord", "music_playlist", {"playlist": value, "query": value, "server": default_server}, 0.998)

    discord_music_context = bool(re.search(r"\b(?:музык\w*|трек\w*|песн\w*|очеред\w*|плейлист\w*|волн\w*|музыкальн\w*\s+бот\w*|jarvis\s+bot|джарвис\w*\s+бот\w*)\b", t))
    discord_transport_short = bool(
        re.fullmatch(r"(?:следующ\w*(?:\s+(?:трек|трэк|трак|трап|песн)\w*)?|дальше|пауза|продолжи|возобнови|останови|стоп(?:\s+музык\w*)?|пропусти(?:\s+(?:трек|трэк|трак|трап|песн)\w*)?|скипни(?:\s+(?:трек|трэк|трак|трап|песн)\w*)?)", t)
        or re.search(r"\bпереключи\w*.*\bследующ\w*", t)
    )
    discord_transport_explicit = bool(re.search(
        r"\b(?:пауза|приостанови|продолжи|возобнови|останови|стоп|выключи|пропусти|скипни|следующ\w*|дальше|очеред\w*|играет|проигрыва\w*|звучит)\b", t
    ))
    discord_preference = bool(re.search(r"\b(?:лайк|(?:a\s+)?like|дизлайк(?:ни)?|дислайк(?:ни)?|dislike|thumbs\s+down|мне\s+не\s+нравится)\b", t, flags=re.I))
    if (explicit and (discord_music_context or discord_transport_short or discord_transport_explicit or discord_preference)) or (active and (discord_music_context or discord_transport_short or discord_preference)):
        if re.search(r"\b(?:дизлайк(?:ни)?|дислайк(?:ни)?|dislike|thumbs\s+down|мне\s+не\s+нравится)\b", t, flags=re.I):
            return ParsedIntegrationIntent("discord", "music_dislike", {"server": default_server})
        if re.search(r"\b(?:лайк|(?:a\s+)?like|лайкни|залайкай|мне\s+нравится)\b", t, flags=re.I):
            return ParsedIntegrationIntent("discord", "music_like", {"server": default_server})
        if re.search(r"\b(?:очисти|сбрось|удали\s+всю)\w*\s+очеред\w*", t):
            return ParsedIntegrationIntent("discord", "music_queue_clear", {"server": default_server})
        if (
            re.search(r"\b(?:что|какая|какой|какие|покажи|назови|перечисли)\w*.*\bочеред\w*", t)
            or re.search(r"\b(?:что|какая|какой|кто)\w*.*\b(?:сейчас\s+)?(?:играет|проигрыва\w*|звучит)\b", t)
        ):
            return ParsedIntegrationIntent("discord", "music_queue_status", {"server": default_server})

        next_patterns = (
            r"\bследующ\w*\s+(?:песн|трек|трэк|трак|трап)\w*\s*(?:пусть\s+будет|будет|[-—:])\s*(.+?)(?:\s+(?:в|через)\s+(?:discord|дискорд)\w*)?$",
            r"\bследующ\w*\s+(?:песн|трек|трэк|трак|трап)\w*\s+(?:для\s+(?:бот\w*\s+)?)?(?:discord|дискорд)\w*\s+(.+)$",
        )
        for pattern in next_patterns:
            m_next = re.search(pattern, t)
            if m_next:
                query = _clean_tail(m_next.group(1))
                if query:
                    return ParsedIntegrationIntent("discord", "music_queue_next", {"query": query, "server": default_server})
        m_add = re.search(r"\b(?:добавь|поставь)\w*\s+(.+?)\s+в\s+очеред\w*(?:\s+(?:discord|дискорд)\w*)?$", t)
        if m_add:
            query = _clean_tail(m_add.group(1))
            if query:
                return ParsedIntegrationIntent("discord", "music_queue_add", {"query": query, "server": default_server})

        if re.search(r"\b(?:предыдущ\w*(?:\s+(?:трек|трэк|трак|трап|песн)\w*)?|назад)\b", t):
            return ParsedIntegrationIntent("discord", "music_previous", {"server": default_server})
        if (
            re.search(r"\b(?:пропусти|скипни)\w*(?:\s+(?:текущ\w*\s+)?(?:песн|трек|музык)\w*)?", t)
            or re.search(r"\bпереключи\w*.*\bследующ\w*", t)
            or re.search(r"\b(?:следующ\w*\s+(?:песн|трек|трэк|трак|трап)\w*|следующ\w*|дальше)\b", t)
        ):
            return ParsedIntegrationIntent("discord", "music_skip", {"server": default_server})
        if re.search(r"\b(?:останови|стопни|прекрати|выключи|стоп)\w*\b", t):
            return ParsedIntegrationIntent("discord", "music_stop", {"server": default_server})
        if pause_like(t, strong_domain=True) or re.search(r"\b(?:пауза|приостанови)\w*\b", t):
            return ParsedIntegrationIntent("discord", "music_pause", {"server": default_server})
        if resume_like(t) or re.search(r"\b(?:продолжи|возобнови|возобновить)\w*\b", t):
            return ParsedIntegrationIntent("discord", "music_resume", {"server": default_server})

    requested_server = str(default_server or "").strip()
    # Prefer an explicitly named configured server. This makes phrases such as
    # «музыкальному боту на сервере Бойцовский клуб включи ...» deterministic
    # instead of silently falling back to the default guild.
    for configured_server in (known_servers or []):
        server_name = str(configured_server or "").strip()
        if server_name and _n(server_name) in t:
            requested_server = server_name
            break
    if requested_server == str(default_server or "").strip():
        m_server = re.search(r"(?:на\s+)?сервер\w*\s+(.+?)(?=\s+(?:чтобы|и\s+)?(?:включ|постав|запуст|сыграй|play|музыкальн\w*\s+бот)|[,;:]|$)", t, flags=re.I)
        if m_server:
            candidate_server = _clean_tail(m_server.group(1))
            if candidate_server:
                requested_server = candidate_server

    if re.fullmatch(r"(?:открой|запусти|включи)\s+(?:приложени\w*\s+)?(?:discord|дискорд)", t):
        return ParsedIntegrationIntent("discord", "open")

    # Natural hands-free phrasing for JARVIS' own music bot. Discord is an
    # explicit destination here, so no desktop/conversational context is needed.
    # Keep a narrow transliterated branch because Whisper sometimes emits an
    # otherwise-Russian command in Latin letters (e.g. "vklyuchi ... v discord").
    dw = discord_word
    play_word = r"(?:включи|поставь|запусти|вруби|сыграй|проиграй|vklyuch\w*|vkluch\w*|postav\w*|zapust\w*|vrub\w*|sygra\w*|proigra\w*|play)"
    prep_word = r"(?:в|на|через|для|v|in|na|cherez|dlya|through)"
    media_word = r"(?:песн\w*|трек\w*|музык\w*|pesn\w*|trek\w*|track\w*|song\w*|muzik\w*|muzyk\w*|music)"
    direct_music_patterns = (
        rf"^{play_word}\s+(?:мне\s+|mne\s+)?(.+?)\s+{prep_word}\s+{dw}$",
        rf"^{play_word}\s+(?:мне\s+|mne\s+)?{prep_word}\s+{dw}\s+(.+)$",
        rf"^{prep_word}\s+{dw}\s+{play_word}\s+(?:мне\s+|mne\s+)?(.+)$",
        rf"^{play_word}\s+(?:мне\s+|mne\s+)?{dw}\s+{media_word}?\s*(.+)$",
    )
    for pattern in direct_music_patterns:
        m_direct = re.match(pattern, t, flags=re.I)
        if not m_direct:
            continue
        query = _clean_tail(m_direct.group(1))
        query = re.sub(r"^(?:песн\w*|трек\w*|музык\w*)\s+", "", query, flags=re.I).strip()
        if query and not pause_like(query) and not resume_like(query):
            return ParsedIntegrationIntent(
                "discord", "music_bot_play",
                {"query": query, "server": requested_server, "channel": ""}, 0.99,
            )

    # Destination-first safety net for ASR word-order variations. If the utterance
    # explicitly names Discord and contains a play verb, Discord owns the request;
    # remove only routing/scaffolding words and keep the rest verbatim as the song
    # query. Known Discord UI controls are excluded so "включи микрофон в Discord"
    # does not become a song search.
    if explicit and re.search(rf"\b{play_word}\b", t):
        control_words = bool(re.search(r"\b(?:микрофон|мик|звук|уведомлен|канал|сервер|приложен|демонстрац|камера)\w*\b", t))
        if not control_words:
            query_text = re.sub(rf"(?:^|\s)(?:{prep_word})?\s*{dw}(?=\s|$)", " ", t, flags=re.I)
            query_text = re.sub(rf"^\s*{play_word}\s+(?:мне\s+|mne\s+)?", "", query_text, flags=re.I)
            query_text = re.sub(rf"^(?:{media_word}|композиц\w*)\s+", "", query_text, flags=re.I)
            query = _clean_tail(re.sub(r"\s+", " ", query_text))
            if query and not pause_like(query) and not resume_like(query):
                return ParsedIntegrationIntent(
                    "discord", "music_bot_play",
                    {"query": query, "server": requested_server, "channel": ""}, 0.992,
                )

    # Dedicated music-bot intent goes before ordinary message parsing.  It never
    # needs a user token: it navigates the desktop client and submits one command
    # explicitly requested by the user.
    music_bot = re.search(r"(?:музыкальн\w*\s+бот\w*|dj\s*bot|дидже\w*\s+бот\w*|через\s+бот\w*)", t)
    if music_bot:
        # Extract the play request independently from word order. Server/profile
        # clauses are routing metadata and must never become part of the song
        # query. This accepts both «боту на сервере X включи Y» and
        # «включи Y через музыкального бота на сервере X».
        query_text = t
        for server_name in (known_servers or []):
            sn = _n(str(server_name or "").strip())
            if sn:
                query_text = re.sub(rf"(?:на\s+)?сервер\w*\s+{re.escape(sn)}", " ", query_text, flags=re.I)
        if requested_server:
            sn = _n(requested_server)
            if sn:
                query_text = re.sub(rf"(?:на\s+)?сервер\w*\s+{re.escape(sn)}", " ", query_text, flags=re.I)
        query_text = re.sub(r"(?:сделай|выполни|отправь)?\s*запрос\w*\s+(?:к\s+)?", " ", query_text, flags=re.I)
        query_text = re.sub(r"(?:через\s+)?(?:музыкальн\w*\s+бот\w*|dj\s*bot|дидже\w*\s+бот\w*|бот\w*)", " ", query_text, flags=re.I)
        query_text = re.sub(r"\s+", " ", query_text).strip(" ,:-")
        m_play = re.search(r"(?:включ(?:и|ить)|постав(?:ь|ить)|запуст(?:и|ить)|сыграй|play)\s+(.+)$", query_text)
        query = _clean_tail(m_play.group(1)) if m_play else ""
        query = re.sub(r"^(?:песн\w*|трек\w*|музык\w*)\s+", "", query, flags=re.I).strip()
        if query:
            return ParsedIntegrationIntent(
                "discord", "music_bot_play",
                # Leave channel empty deliberately: DiscordAdapter resolves the
                # text channel from the selected server profile. Legacy settings
                # are still used automatically when no profiles exist.
                {
                    "query": query, "server": requested_server,
                    "channel": default_music_channel if _n(requested_server) == _n(default_server) else "",
                },
            )

    # Very short phrase can use the configured music channel after Discord has
    # become the active integration context.
    if active and default_music_channel:
        m = re.match(r"^(?:бот\w*\s+)?(?:включи|поставь|сыграй)\s+(.+)$", t)
        # Short-context music control is intentionally very conservative. It may
        # only consume a bare request when there is no explicit video/browser
        # domain and the payload is not a desired-state pause/resume command.
        if (
            m
            and not re.search(r"\b(?:яндекс|youtube|ютуб|видео|ролик|браузер|интернет)\b", t)
            and not pause_like(t, strong_domain=explicit_video(t))
            and not resume_like(t)
        ):
            payload = _clean_tail(m.group(1))
            if not re.search(r"\b(?:видео|ролик|youtube|ютуб|пауз\w*|pause)\b", payload):
                return ParsedIntegrationIntent(
                    "discord", "music_bot_play",
                    {"query": payload, "server": default_server, "channel": default_music_channel}, 0.84,
                )

    both = _extract_server_channel(t, "text")
    if both:
        server, channel = both
        return ParsedIntegrationIntent("discord", "open_text_channel", {"server": server, "channel": channel})
    both = _extract_server_channel(t, "voice")
    if both:
        server, channel = both
        return ParsedIntegrationIntent("discord", "join_voice_channel", {"server": server, "channel": channel})

    m = re.match(r"^(?:в\s+discord\s+|в\s+дискорд\w*\s+)?(?:перейди|переключись|открой|зайди|войди)\s+(?:на\s+)?сервер\w*\s+(.+)$", t)
    if m:
        return ParsedIntegrationIntent("discord", "open_server", {"server": _clean_tail(m.group(1))})

    m = re.match(r"^(?:перейди|переключись|открой|зайди|войди)\s+(?:в\s+|на\s+)?(?:текстов\w*\s+канал\w*|чат\w*)(?:\s+(?:под\s+названи\w*|с\s+названи\w*))?\s+(.+?)\s+(?:на\s+)?сервер\w*\s+(.+)$", t)
    if m:
        return ParsedIntegrationIntent("discord", "open_text_channel", {"server": _clean_tail(m.group(2)), "channel": _clean_tail(m.group(1))})

    m = re.match(r"^(?:зайди|войди|перейди|переключись|подключись|подключи\s+меня)\s+(?:в\s+|на\s+)?(?:голосов\w*\s+канал\w*|войс\w*|voice\s+channel)(?:\s+(?:под\s+названи\w*|с\s+названи\w*))?\s+(.+?)\s+(?:на\s+)?сервер\w*\s+(.+)$", t)
    if m:
        return ParsedIntegrationIntent("discord", "join_voice_channel", {"server": _clean_tail(m.group(2)), "channel": _clean_tail(m.group(1))})

    m = re.match(r"^(?:перейди|переключись|открой|зайди|войди)\s+(?:в\s+|на\s+)?(?:текстов\w*\s+канал\w*|чат\w*)(?:\s+(?:под\s+названи\w*|с\s+названи\w*))?\s+(.+)$", t)
    if m:
        return ParsedIntegrationIntent("discord", "open_text_channel", {"server": default_server, "channel": _clean_tail(m.group(1))})

    m = re.match(r"^(?:зайди|войди|перейди|переключись|подключись|подключи\s+меня)\s+(?:в\s+|на\s+)?(?:голосов\w*\s+канал\w*|войс\w*|voice\s+channel)(?:\s+(?:под\s+названи\w*|с\s+названи\w*))?\s+(.+)$", t)
    if m:
        return ParsedIntegrationIntent("discord", "join_voice_channel", {"server": default_server, "channel": _clean_tail(m.group(1))})

    # «на сервере A в канале B напиши C»
    m = re.match(r"^(?:на\s+)?сервер\w*\s+(.+?)\s+(?:в\s+)?(?:текстов\w*\s+)?канал\w*\s+([^,:]+?)\s+(?:напиши|отправь|пошли|скинь)\s+(.+)$", t, flags=re.I)
    if m:
        return ParsedIntegrationIntent("discord", "send_message", {"server": _clean_tail(m.group(1)), "channel": _clean_tail(m.group(2)), "text": _clean_tail(m.group(3))})

    # 'напиши в текстовый канал general привет всем'
    m = re.match(
        r"^(?:напиши|отправь|пошли|скинь)\s+(?:сообщени\w*\s+)?(?:в\s+)?(?:текстов\w*\s+канал\w*|канал\w*|чат\w*)\s+([^,:]+?)[,:]?\s+(?:текст\s+)?(.+)$",
        t, flags=re.I,
    )
    if m:
        return ParsedIntegrationIntent(
            "discord", "send_message",
            {"server": default_server, "channel": _clean_tail(m.group(1)), "text": _clean_tail(m.group(2))},
        )
    if active:
        m = re.match(r"^(?:напиши|отправь|пошли|скинь)\s+(?:сообщени\w*\s+)?(.+)$", t)
        if m:
            payload = _clean_tail(m.group(1))
            if re.search(r"\b(?:файл\w*|документ\w*|заметк\w*|чек[- ]?лист\w*|письм\w*|email|почт\w*)\b", payload):
                return None
            return ParsedIntegrationIntent("discord", "send_message", {"server": default_server, "channel": "", "text": payload}, 0.88)

    if domainish and default_voice_channel and re.fullmatch(r"(?:зайди|войди|подключись|подключи\s+меня|перейди|переключись)\s+(?:в\s+|на\s+)?(?:голосов\w*\s+канал\w*|войс\w*|voice\s+channel)", t):
        return ParsedIntegrationIntent("discord", "join_voice_channel", {"server": default_server, "channel": default_voice_channel}, 0.92)

    if active or explicit:
        if re.fullmatch(r"(?:замуть|выключи\s+микрофон|выключи\s+мик|микрофон\s+выключи|mute)", t):
            return ParsedIntegrationIntent("discord", "mute")
        if re.fullmatch(r"(?:размуть|включи\s+микрофон|включи\s+мик|unmute)", t):
            return ParsedIntegrationIntent("discord", "unmute")
        if re.fullmatch(r"(?:оглуши|выключи\s+звук\s+дискорда|deafen)", t):
            return ParsedIntegrationIntent("discord", "deafen")
        if re.fullmatch(r"(?:сними\s+оглушение|включи\s+звук\s+дискорда|undeafen)", t):
            return ParsedIntegrationIntent("discord", "undeafen")
        if re.fullmatch(r"(?:выйди|отключись|покинь)\s+(?:из\s+)?(?:голосов\w*\s+канал\w*|войс\w*)", t):
            return ParsedIntegrationIntent("discord", "leave_voice_channel")

    return None


def parse_integration_intent(text: str, *, context: dict[str, Any] | None = None) -> ParsedIntegrationIntent | None:
    context = dict(context or {})
    active = str(context.get("active_domain", "") or "")
    wake_phrase = str(context.get("wake_phrase", "") or "")
    wake_aliases = context.get("wake_aliases", None)
    semantic_text = _strip_wake(text, wake_phrase=wake_phrase, wake_aliases=wake_aliases)

    # A fresh explicit video/browser domain always outranks stale integration
    # context. HybridRouter normally handles these first, but this guard protects
    # the mature direct parser and any future LLM fallback as well.
    if explicit_video(semantic_text) or looks_like_browser_search(semantic_text):
        return None

    explicit_discord_now = explicit_discord_media(semantic_text)
    explicit_yandex_now = explicit_yandex_music(semantic_text)

    # Discord's explicit bot/channel vocabulary is checked first; a music-bot
    # command contains 'включи' but belongs to Discord, not Yandex Music.
    discord = parse_discord_intent(
        text,
        active_domain=active,
        default_server=str(context.get("discord_default_server", "") or ""),
        default_music_channel=str(context.get("discord_music_channel", "") or ""),
        default_voice_channel=str(context.get("discord_voice_channel", "") or ""),
        known_servers=context.get("discord_known_servers", []) or [],
        wake_phrase=wake_phrase,
        wake_aliases=wake_aliases,
    )
    if discord is not None:
        # Do not let stale active_domain drive short media follow-ups. Those are
        # resolved by Agent playback ownership checks after deterministic routing.
        if not explicit_discord_now:
            return None
        allowed_discord = {
            "music_bot_play", "music_queue_next", "music_queue_add", "music_queue_status",
            "music_queue_clear", "music_skip", "music_previous", "music_pause", "music_resume", "music_stop",
            "music_shuffle_on", "music_shuffle_off", "music_repeat_track", "music_repeat_playlist", "music_repeat_off",
            "music_my_wave", "music_playlist", "music_like", "music_dislike",
            "send_message", "bot_join_voice", "bot_leave_voice", "background_status",
        }
        # Legacy parser names for voice join/leave are mapped to official-bot actions.
        if discord.action == "join_voice_channel":
            discord = ParsedIntegrationIntent(discord.domain, "bot_join_voice", dict(discord.arguments), discord.confidence)
        elif discord.action == "leave_voice_channel":
            discord = ParsedIntegrationIntent(discord.domain, "bot_leave_voice", dict(discord.arguments), discord.confidence)
        if discord.action in allowed_discord:
            return discord
        return None

    yandex = parse_yandex_music_intent(text, active_domain=active, wake_phrase=wake_phrase, wake_aliases=wake_aliases)
    if yandex is None:
        return None
    if not explicit_yandex_now:
        return None
    allowed_yandex = {
        "status", "play", "pause", "stop", "next", "previous", "play_track",
        "my_wave", "my_tracks", "playlist", "like", "unlike", "dislike", "shuffle_on", "shuffle_off", "repeat_on", "repeat_track", "repeat_playlist", "repeat_off", "set_volume", "volume_up", "volume_down",
        "api_status", "background_status",
    }
    return yandex if yandex.action in allowed_yandex else None
