# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import re
from dataclasses import dataclass

from jarvis.runtime.semantics import norm


@dataclass(frozen=True)
class MediaPhraseIntent:
    intent: str
    tool: str
    action: str
    query: str = ""
    priority: int = 960
    confidence: float = 0.999


# This module is deliberately grammar/lexicon based rather than a flat list of
# complete sentences.  A handful of interchangeable verb/domain/noun groups cover
# thousands of natural Russian word orders while keeping media domains mutually
# exclusive before the LLM is involved.
_DISCORD = r"(?:discord\w*|discort\w*|diskord\w*|diskort\w*|дискорд\w*|дискорт\w*|дискор\w*|дескорд\w*|детскорд\w*|дескорт\w*|дискорде|дискорду|дискорда|музыкальн\w*\s+бот\w*|дидже\w*\s+бот\w*|dj\s*bot)"
_YANDEX = r"(?:яндекс(?:е|у|ом|а)?(?:[\s._-]*музык\w*)?|yandex(?:[\s._-]*music)?|яндексомузык\w*)"
_SPOTIFY = r"(?:spotify|спотифай\w*|спотифи\w*|спотиф\w*)"
_YOUTUBE = r"(?:youtube|ютуб\w*|ютьюб\w*|ютюб\w*)"
_VIDEO = r"(?:видео\w*|ролик\w*|видос\w*|video\w*|clip\w*)"

_PLAY_VERB = r"(?:включ\w*|постав\w*|запуст\w*|вруб\w*|проигра\w*|сыгра\w*|воспроизвед\w*|vklyuch\w*|vkluch\w*|postav\w*|zapust\w*|vrub\w*|proigra\w*|sygra\w*|play)"
_PAUSE = r"(?:пауз\w*|приостанов\w*|pauz\w*|priostanov\w*|постав\w*\s+(?:это\s+|его\s+|видео\s+|музык\w*\s+)?на\s+пауз\w*|pause)"
_RESUME = r"(?:продолж\w*|возобнов\w*|prodolzh\w*|prodolj\w*|vozobnov\w*|сними\w*\s+(?:его\s+|видео\s+|музык\w*\s+)?с\s+пауз\w*|resume)"
_STOP = r"(?:останов\w*|выключ\w*|прекрат\w*|стоп\w*|стопни\w*|ostanov\w*|vyklyuch\w*|prekrat\w*|stop\w*)"
_SKIP = r"(?:пропуст\w*|скипн\w*|propust\w*|skip|дальше|далше|dalshe|следующ\w*|sledu\w*)"
_PREV = r"(?:предыдущ\w*|predyd\w*|predid\w*|верни\w*\s+(?:предыдущ\w*|назад)|назад|nazad)"

_MEDIA_NOUN = r"(?:песн\w*|трек\w*|композиц\w*|музык\w*|pesn\w*|trek\w*|track\w*|song\w*|muzik\w*|muzyk\w*|music)"
_PLAYLIST = r"(?:плейлист\w*|playlist\w*)"
_FILLERS = r"(?:мне|mne|пожалуйста|pozhaluysta|плиз|please|давай|davai|ну|ка|сейчас|вот|эту|этот|такую|такой)"


def _has(pattern: str, text: str) -> bool:
    return re.search(rf"\b{pattern}\b", text, flags=re.I) is not None


def _clean_query(text: str, *, domain_pattern: str, play_verb: bool = True) -> str:
    """Remove command scaffolding while preserving artist/title payload.

    It is intentionally conservative: only command words, service markers and media
    nouns are stripped.  English/Russian song title words are otherwise untouched.
    """
    value = norm(text)
    value = re.sub(r"^(?:нео|нио|нэо|neo|nio|джарвис|jarvis|darvis|джарвес)\b\s*[,.:;-]?\s*", "", value, flags=re.I)
    value = re.sub(rf"\b(?:в|во|через|для|на|v|in|through|cherez|dlya|na)\s+{domain_pattern}\b", " ", value, flags=re.I)
    value = re.sub(rf"\b{domain_pattern}\b", " ", value, flags=re.I)
    if play_verb:
        value = re.sub(rf"\b{_PLAY_VERB}\b", " ", value, flags=re.I)
    value = re.sub(rf"\b{_FILLERS}\b", " ", value, flags=re.I)
    value = re.sub(rf"\b{_MEDIA_NOUN}\b", " ", value, flags=re.I)
    value = re.sub(r"\b(?:у\s+бота|ботом|боту|в\s+канале|в\s+голосов\w*\s+канал\w*)\b", " ", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n,.:;!?—-«»\"'")
    return value


def _discord_queue_query(text: str, *, next_up: bool) -> str:
    value = norm(text)
    value = re.sub(r"^(?:нео|нио|нэо|neo|nio|джарвис|jarvis|darvis|джарвес)\b\s*[,.:;-]?\s*", "", value, flags=re.I)

    captures: tuple[str, ...]
    if next_up:
        captures = (
            rf"\bследующ\w*\s+(?:песн\w*|трек\w*)\s+(?:в\s+)?{_DISCORD}\s*(?:пусть\s+будет|будет|сделай|поставь|[-—:])\s*(.+)$",
            rf"\bследующ\w*\s+(?:песн\w*|трек\w*)\s+(?:в\s+)?{_DISCORD}\s+(.+)$",
            rf"\bследующ\w*\s+(?:песн\w*|трек\w*)\s+(?:для\s+(?:бот\w*\s+)?)?{_DISCORD}\s+(.+)$",
            rf"\bследующ\w*\s+(?:песн\w*|трек\w*)\s*(?:пусть\s+будет|будет|сделай|поставь|[-—:])\s*(.+?)\s+(?:в\s+)?{_DISCORD}\s*$",
            rf"\b(?:поставь|сделай|добавь)\w*\s+следующ\w*\s+(?:песн\w*|трек\w*)\s+(.+?)(?:\s+(?:в\s+)?{_DISCORD})?\s*$",
            rf"\bпосле\s+(?:эт\w*|не[её]?)\s*(?:песн\w*|трек\w*)?\s*(?:поставь|включи|добавь)?\s*(.+?)(?:\s+(?:в\s+)?{_DISCORD})?\s*$",
        )
    else:
        captures = (
            rf"\b(?:добавь|закинь|кинь|поставь)\w*\s+(?:мне\s+)?(.+?)\s+(?:в\s+)?очеред\w*(?:\s+(?:в\s+)?{_DISCORD})?\s*$",
            rf"\b(?:добавь|закинь|кинь|поставь)\w*\s+(?:мне\s+)?(?:в\s+)?очеред\w*(?:\s+(?:в\s+)?{_DISCORD})?\s+(.+)$",
            rf"\b(?:в\s+)?очеред\w*\s+(?:в\s+)?{_DISCORD}\s+(?:добавь|закинь|кинь|поставь)\w*\s+(.+)$",
        )
    for pattern in captures:
        m = re.search(pattern, value, flags=re.I)
        if m:
            q = m.group(1)
            q = re.sub(rf"\b{_DISCORD}\b", " ", q, flags=re.I)
            q = re.sub(rf"\b{_FILLERS}\b", " ", q, flags=re.I)
            q = re.sub(rf"^\s*{_MEDIA_NOUN}\s+", "", q, flags=re.I)
            return re.sub(r"\s+", " ", q).strip(" \t\r\n,.:;!?—-«»\"'")
    return ""



def _playlist_query(text: str, *, domain_pattern: str) -> str:
    value = _clean_query(text, domain_pattern=domain_pattern)
    value = re.sub(rf"\b{_PLAYLIST}\b", " ", value, flags=re.I)
    value = re.sub(r"\b(?:с\s+названи\w*|под\s+названи\w*|который\s+называется)\b", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip(" \t\r\n,.:;!?—-«»\"'")

def _extract_seconds(text: str, default: float = 10.0) -> float:
    m = re.search(r"\b(\d{1,4})\s*(?:секунд\w*|сек\b|s\b)", text, flags=re.I)
    if m:
        return max(1.0, min(3600.0, float(m.group(1))))
    words = {
        "пять": 5, "десять": 10, "пятнадцать": 15, "двадцать": 20,
        "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
        "минута": 60, "минуту": 60,
    }
    for word, seconds in words.items():
        if re.search(rf"\b{word}\b", text):
            return float(seconds)
    return float(default)


def _extract_absolute_time(text: str) -> float | None:
    t = norm(text)
    m = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", t)
    if m:
        if m.group(3) is not None:
            return float(int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)))
        return float(int(m.group(1))*60 + int(m.group(2)))
    m = re.search(r"\b(?:(\d{1,2})\s*(?:час\w*|h)\s*)?(?:(\d{1,3})\s*(?:минут\w*|мин\b|m)\s*)?(?:(\d{1,2})\s*(?:секунд\w*|сек\b|s))?\b", t, re.I)
    if m and any(m.groups()):
        h, mn, sec = (int(x or 0) for x in m.groups())
        if h or mn or sec:
            return float(h*3600 + mn*60 + sec)
    return None


def parse_explicit_media_phrase(text: str) -> MediaPhraseIntent | None:
    """High-confidence parser for explicit YouTube / Discord / Yandex commands.

    Domain precedence is explicit and deterministic:
      * Discord mention -> Discord bot/player, even though its audio source is Yandex.
      * YouTube/video mention -> browser video controller.
      * Yandex marker/personal library -> headless Yandex player.

    Generic short follow-ups ("следующий трек") are intentionally not claimed here;
    the Agent's real playback-owner resolver handles those based on active state.
    """
    t = norm(text)
    if not t:
        return None

    has_discord = _has(_DISCORD, t)
    has_spotify = _has(_SPOTIFY, t)
    has_youtube = _has(_YOUTUBE, t) or (_has(_VIDEO, t) and not has_discord and not _has(_YANDEX, t) and not has_spotify)
    has_yandex = (
        _has(_YANDEX, t)
        or bool(re.search(r"\b(?:моя\s+волна|мою\s+волну)\b", t))
    )

    # A phrase like "включи X в Discord из Яндекс Музыки" still targets Discord;
    # Yandex is merely the source provider in this architecture.
    if has_discord:
        # Queue shuffle is a desired-state command and must win before STOP:
        # «выключи в Discord перемешивание» contains a broad stop verb but should
        # only change queue order. The service marker may appear anywhere.
        discord_state = re.sub(rf"(?:^|\s)(?:в|во|через|для|на)?\s*{_DISCORD}(?=\s|$)", " ", t, flags=re.I)
        discord_state = re.sub(r"\s+", " ", discord_state).strip()
        if re.fullmatch(r"(?:(?:включи|активируй|запусти|включить)\s+(?:режим\s+)?(?:перемешиван\w*|шаффл|shuffle)|перемеша(?:й|ть)(?:\s+(?:плейлист\w*|очеред\w*|музык\w*|трек\w*|песн\w*))?)", discord_state, flags=re.I):
            return MediaPhraseIntent("discord_music_shuffle_on", "discord", "music_shuffle_on", priority=992)
        if re.fullmatch(r"(?:выключи|отключи|убери|выключить)\s+(?:режим\s+)?(?:перемешиван\w*|шаффл|shuffle)", discord_state, flags=re.I):
            return MediaPhraseIntent("discord_music_shuffle_off", "discord", "music_shuffle_off", priority=992)
        if re.fullmatch(r"(?:(?:включи|активируй|запусти|включить)\s+)?(?:режим\s+)?(?:повтор\w*|репит|repeat)\s+(?:текущ\w*\s+)?(?:трек\w*|песн\w*)", discord_state, flags=re.I):
            return MediaPhraseIntent("discord_music_repeat_track", "discord", "music_repeat_track", priority=993)
        if re.fullmatch(r"(?:(?:включи|активируй|запусти|включить)\s+)?(?:режим\s+)?(?:повтор\w*|репит|repeat)\s+(?:плейлист\w*|очеред\w*|контекст\w*)", discord_state, flags=re.I):
            return MediaPhraseIntent("discord_music_repeat_playlist", "discord", "music_repeat_playlist", priority=993)
        if re.fullmatch(r"(?:выключи|отключи|убери|выключить)\s+(?:режим\s+)?(?:повтор\w*|репит|repeat)", discord_state, flags=re.I):
            return MediaPhraseIntent("discord_music_repeat_off", "discord", "music_repeat_off", priority=993)

        # Preference commands act on the track currently playing through the shared
        # Discord bot, but the preference itself is written to THIS local user's
        # Yandex account. Dislike also advances the shared Discord queue.
        if re.search(r"\b(?:дизлайк(?:ни)?|дислайк(?:ни)?|dislike|thumbs\s+down|мне\s+не\s+нравится)\b", t):
            return MediaPhraseIntent("discord_music_dislike", "discord", "music_dislike", priority=989)
        if re.search(r"\b(?:лайк(?:ни)?|залайкай|like|a\s+like|поставь\w*\s+лайк|мне\s+нравится)\b", t):
            return MediaPhraseIntent("discord_music_like", "discord", "music_like", priority=988)
        # Personalized sources deliberately replace the shared queue. My Wave,
        # My Tracks (the authenticated user's liked library) and named playlists
        # are executed by the requesting JARVIS instance so its own encrypted
        # Yandex OAuth/account is used.
        if re.search(r"\b(?:мои\s+(?:треки|песни)|мою\s+музыку)\b", t) and (_has(_PLAY_VERB, t) or re.search(r"\b(?:мои\s+(?:треки|песни)|мою\s+музыку)\b", t)):
            return MediaPhraseIntent("discord_music_my_tracks", "discord", "music_my_tracks", priority=981)
        if re.search(r"\b(?:моя\s+волна|мою\s+волну)\b", t) and (_has(_PLAY_VERB, t) or re.search(r"\b(?:моя\s+волна|мою\s+волну)\b", t)):
            return MediaPhraseIntent("discord_music_my_wave", "discord", "music_my_wave", priority=980)
        if _has(_PLAYLIST, t) and (_has(_PLAY_VERB, t) or re.search(r"\b(?:плейлист|playlist)\b", t, flags=re.I)):
            q = _playlist_query(t, domain_pattern=_DISCORD)
            if q:
                return MediaPhraseIntent("discord_music_playlist", "discord", "music_playlist", q, priority=979)

        # Queue inspection / current item.
        if re.search(r"\b(?:очист\w*|сброс\w*|обнул\w*|удал\w*\s+(?:всю|все)\w*)\b.*\bочеред\w*", t):
            return MediaPhraseIntent("discord_music_queue_clear", "discord", "music_queue_clear", priority=972)
        if (
            re.search(r"\b(?:что|какая|какой|какие|покажи|назови|перечисли|скажи)\w*.*\bочеред\w*", t)
            or re.search(r"\b(?:что\s+(?:будет|играет|звучит)\s+дальше|что\s+дальше)\b", t)
        ):
            return MediaPhraseIntent("discord_music_queue_status", "discord", "music_queue_status", priority=971)
        if re.search(r"\b(?:что|какая|какой|назови|скажи)\w*.*\b(?:сейчас\s+)?(?:играет|проигрыва\w*|звучит)\b", t):
            return MediaPhraseIntent("discord_music_queue_status", "discord", "music_queue_status", priority=971)

        # Queue insertion must precede skip/next matching.
        next_up_mark = bool(
            re.search(r"\bследующ\w*\s+(?:песн\w*|трек\w*)\b.*(?:пусть\s+будет|будет|поставь|сделай|добавь|[-—:])", t)
            or re.search(r"\b(?:поставь|сделай|добавь)\w*.*\bследующ\w*\s+(?:песн\w*|трек\w*)\b", t)
            or re.search(r"\bпосле\s+(?:эт\w*|не[её]?)\b", t)
            or re.search(rf"\bследующ\w*\s+(?:песн\w*|трек\w*)\s+(?:в\s+)?{_DISCORD}\s+\S+", t)
            or re.search(rf"\bследующ\w*\s+(?:песн\w*|трек\w*)\s+(?:для\s+(?:бот\w*\s+)?)?{_DISCORD}\s+\S+", t)
        )
        if next_up_mark:
            q = _discord_queue_query(t, next_up=True)
            if q:
                return MediaPhraseIntent("discord_music_queue_next", "discord", "music_queue_next", q, priority=974)

        queue_add_mark = bool(
            re.search(r"\b(?:добавь|закинь|кинь|поставь)\w*.*\bочеред\w*", t)
            or re.search(r"\bочеред\w*.*\b(?:добавь|закинь|кинь|поставь)\w*", t)
        )
        if queue_add_mark:
            q = _discord_queue_query(t, next_up=False)
            if q:
                return MediaPhraseIntent("discord_music_queue_add", "discord", "music_queue_add", q, priority=973)

        # Transport.  A payload-less "запусти трек в Discord" is treated as the
        # common Whisper confusion for spoken "пропусти трек"; named payload stays play.
        if (
            _has(_SKIP, t)
            or re.search(r"\bпереключ\w*.*\bследующ\w*", t)
            or re.fullmatch(rf"(?:(?:джарвис|нео|нио|neo)\s+)?запуст\w*\s+(?:текущ\w*\s+)?{_MEDIA_NOUN}\s+(?:в|через)?\s*{_DISCORD}", t)
        ) and not next_up_mark:
            # Queue insertion forms were consumed above; any remaining explicit
            # next/skip wording is transport, regardless of whether the domain
            # marker follows the words "следующий трек".
            return MediaPhraseIntent("discord_music_skip", "discord", "music_skip", priority=970)
        if _has(_STOP, t):
            return MediaPhraseIntent("discord_music_stop", "discord", "music_stop", priority=969)
        if _has(_RESUME, t):
            return MediaPhraseIntent("discord_music_resume", "discord", "music_resume", priority=968)
        if _has(_PAUSE, t):
            return MediaPhraseIntent("discord_music_pause", "discord", "music_pause", priority=967)

        # Named play.  Do this after transport so "пропусти трек в Discord" never
        # becomes a search query.  The domain can appear before or after the title.
        if _has(_PLAY_VERB, t):
            q = _clean_query(t, domain_pattern=_DISCORD)
            # Remove transport-like bare words that carry no song payload.
            if q and not re.fullmatch(r"(?:музык\w*|трек\w*|песн\w*|бот\w*|дальше|следующ\w*)", q):
                return MediaPhraseIntent("discord_music_play", "discord", "music_bot_play", q, priority=966)
        return None

    if has_youtube:
        # HOTFIX102: explicit YouTube voice control mirrors the Telegram panel.
        # Only background-verifiable Windows media-session operations remain;
        # fullscreen/theater/quality/speed/video navigation/Shorts are omitted
        # instead of stealing keyboard or pointer input.
        if re.search(r"\b(?:перейд\w*|перемот\w*|промот\w*|мотни\w*)\b.*\b(?:на|к)\b", t, re.I) and not re.search(r"\b(?:вперед|вперёд|назад|обратно)\b", t, re.I):
            at = _extract_absolute_time(t)
            if at is not None:
                return MediaPhraseIntent("youtube_seek_to", "media", "seek_to", str(at), priority=984)
        if re.search(r"\b(?:перемот\w*|промот\w*|мотни\w*)\b.*\b(?:вперед|вперёд|дальше)\b", t):
            return MediaPhraseIntent("youtube_seek_forward", "media", "seek_forward", str(_extract_seconds(t)), priority=975)
        if re.search(r"\b(?:перемот\w*|промот\w*|мотни\w*)\b.*\b(?:назад|обратно)\b", t):
            return MediaPhraseIntent("youtube_seek_backward", "media", "seek_backward", str(_extract_seconds(t)), priority=975)
        # Desired-state controls must be resolved before generic media verbs.
        # «сними видео с паузы» contains the word «паузы», so resume has to win
        # over the broad pause token. Conversely «поставь видео на паузу» must
        # never be normalized into a play command merely because «поставь» can
        # mean "play" in music contexts.
        if _has(_RESUME, t):
            return MediaPhraseIntent("youtube_play", "media", "play", priority=981)
        if _has(_PAUSE, t) or _has(_STOP, t) or re.search(r"\b(?:останов\w*\s+(?:видео|ролик)|замри|поставь\s+(?:видео|ролик)\s+на\s+паузу)\b", t):
            return MediaPhraseIntent("youtube_pause", "media", "pause", priority=980)
        if re.search(r"\b(?:включи|запусти)\w*\s+(?:видео|ролик)\b", t):
            return MediaPhraseIntent("youtube_play", "media", "play", priority=979)
        if re.search(r"\b(?:что|какое|какой|скажи|назови)\w*.*\b(?:играет|смотрю|видео|ролик)\b", t):
            return MediaPhraseIntent("youtube_status", "media", "status", priority=965)
        if _has(_PLAY_VERB, t):
            q = _clean_query(t, domain_pattern=rf"(?:{_YOUTUBE}|{_VIDEO})")
            if q and not re.fullmatch(r"(?:видео\w*|ролик\w*|ютуб\w*)", q):
                return MediaPhraseIntent("youtube_play_search", "media", "play_search", q, priority=973)
        return None

    if has_spotify and not has_discord and not has_youtube:
        spotify_state = re.sub(rf"(?:^|\s)(?:в|во|через|для|на)?\s*{_SPOTIFY}(?=\s|$)", " ", t, flags=re.I)
        spotify_state = re.sub(r"\s+", " ", spotify_state).strip()
        if re.fullmatch(r"(?:(?:включи|активируй|запусти|включить)\s+(?:режим\s+)?(?:перемешиван\w*|шаффл|shuffle)|перемеша(?:й|ть)(?:\s+(?:плейлист\w*|очеред\w*|музык\w*|трек\w*|песн\w*))?)", spotify_state, flags=re.I):
            return MediaPhraseIntent("spotify_shuffle_on", "spotify", "shuffle_on", priority=992)
        if re.fullmatch(r"(?:выключи|отключи|убери|выключить)\s+(?:режим\s+)?(?:перемешиван\w*|шаффл|shuffle)", spotify_state, flags=re.I):
            return MediaPhraseIntent("spotify_shuffle_off", "spotify", "shuffle_off", priority=992)
        if re.fullmatch(r"(?:(?:включи|активируй|запусти|включить)\s+)?(?:режим\s+)?(?:повтор\w*|репит|repeat)\s+(?:текущ\w*\s+)?(?:трек\w*|песн\w*)", spotify_state, flags=re.I):
            return MediaPhraseIntent("spotify_repeat_track", "spotify", "repeat_track", priority=993)
        if re.fullmatch(r"(?:(?:включи|активируй|запусти|включить)\s+)?(?:режим\s+)?(?:повтор\w*|репит|repeat)\s+(?:плейлист\w*|очеред\w*|контекст\w*)", spotify_state, flags=re.I):
            return MediaPhraseIntent("spotify_repeat_playlist", "spotify", "repeat_playlist", priority=993)
        if re.fullmatch(r"(?:выключи|отключи|убери|выключить)\s+(?:режим\s+)?(?:повтор\w*|репит|repeat)", spotify_state, flags=re.I):
            return MediaPhraseIntent("spotify_repeat_off", "spotify", "repeat_off", priority=993)
        if re.search(r"\b(?:что|какая|какой|скажи|назови)\w*.*\b(?:играет|звучит|трек|песня)\b", t):
            return MediaPhraseIntent("spotify_status", "spotify", "status", priority=972)
        if _has(_STOP, t): return MediaPhraseIntent("spotify_stop", "spotify", "stop", priority=974)
        if _has(_SKIP, t): return MediaPhraseIntent("spotify_next", "spotify", "next", priority=974)
        if _has(_PREV, t): return MediaPhraseIntent("spotify_previous", "spotify", "previous", priority=974)
        if _has(_RESUME, t): return MediaPhraseIntent("spotify_resume", "spotify", "play", priority=974)
        if _has(_PAUSE, t): return MediaPhraseIntent("spotify_pause", "spotify", "pause", priority=974)
        if _has(_PLAYLIST, t) and _has(_PLAY_VERB, t):
            q = _playlist_query(t, domain_pattern=_SPOTIFY)
            if q: return MediaPhraseIntent("spotify_playlist", "spotify", "playlist", q, priority=973)
        if re.search(r"\b(?:лайкни|залайкай|поставь\w*\s+лайк|добавь\w*\s+в\s+любим\w*)\b", t):
            return MediaPhraseIntent("spotify_like", "spotify", "like", priority=972)
        if re.search(r"\b(?:убери|сними|удали)\w*.*\b(?:лайк|любим\w*)\b", t):
            return MediaPhraseIntent("spotify_unlike", "spotify", "unlike", priority=972)
        if _has(_PLAY_VERB, t):
            q = _clean_query(t, domain_pattern=_SPOTIFY)
            if q: return MediaPhraseIntent("spotify_play_named", "spotify", "play_track", q, priority=973)
        return None


    if not any((has_discord, has_youtube, has_spotify, has_yandex)):
        if re.fullmatch(r"(?:(?:включи|активируй|запусти|включить)\s+)?(?:режим\s+)?(?:повтор\w*|репит|repeat)\s+(?:текущ\w*\s+)?(?:трек\w*|песн\w*)", t, flags=re.I):
            return MediaPhraseIntent("music_repeat_track", "music", "repeat_track", priority=993)
        if re.fullmatch(r"(?:(?:включи|активируй|запусти|включить)\s+)?(?:режим\s+)?(?:повтор\w*|репит|repeat)\s+(?:плейлист\w*|очеред\w*|контекст\w*)", t, flags=re.I):
            return MediaPhraseIntent("music_repeat_playlist", "music", "repeat_playlist", priority=993)
        if re.fullmatch(r"(?:выключи|отключи|убери|выключить)\s+(?:режим\s+)?(?:повтор\w*|репит|repeat)", t, flags=re.I):
            return MediaPhraseIntent("music_repeat_off", "music", "repeat_off", priority=993)
        if re.fullmatch(r"(?:(?:включи|активируй|запусти|включить)\s+(?:режим\s+)?(?:перемешиван\w*|шаффл|shuffle)|перемеша(?:й|ть)(?:\s+(?:плейлист\w*|очеред\w*|музык\w*|трек\w*|песн\w*))?)", t, flags=re.I):
            return MediaPhraseIntent("music_shuffle_on", "music", "shuffle_on", priority=992)
        if re.fullmatch(r"(?:выключи|отключи|убери|выключить)\s+(?:режим\s+)?(?:перемешиван\w*|шаффл|shuffle)", t, flags=re.I):
            return MediaPhraseIntent("music_shuffle_off", "music", "shuffle_off", priority=992)

    if _has(_PLAYLIST, t) and _has(_PLAY_VERB, t) and not any((has_discord, has_youtube, has_spotify, has_yandex)):
        q = _playlist_query(t, domain_pattern=r"(?!)")
        if q:
            return MediaPhraseIntent("music_playlist", "music", "playlist", q, priority=971)

    if has_yandex and not has_discord:
        # Resolve shuffle before STOP for the same reason as Discord.
        yandex_state = re.sub(rf"(?:^|\s)(?:в|во|через|для|на)?\s*{_YANDEX}(?=\s|$)", " ", t, flags=re.I)
        yandex_state = re.sub(r"\s+", " ", yandex_state).strip()
        if re.fullmatch(r"(?:(?:включи|активируй|запусти|включить)\s+(?:режим\s+)?(?:перемешиван\w*|шаффл|shuffle)|перемеша(?:й|ть)(?:\s+(?:плейлист\w*|очеред\w*|музык\w*|трек\w*|песн\w*))?)", yandex_state, flags=re.I):
            return MediaPhraseIntent("yandex_shuffle_on", "yandex_music", "shuffle_on", priority=992)
        if re.fullmatch(r"(?:выключи|отключи|убери|выключить)\s+(?:режим\s+)?(?:перемешиван\w*|шаффл|shuffle)", yandex_state, flags=re.I):
            return MediaPhraseIntent("yandex_shuffle_off", "yandex_music", "shuffle_off", priority=992)
        if re.fullmatch(r"(?:(?:включи|активируй|запусти|включить)\s+)?(?:режим\s+)?(?:повтор\w*|репит|repeat)\s+(?:текущ\w*\s+)?(?:трек\w*|песн\w*)", yandex_state, flags=re.I):
            return MediaPhraseIntent("yandex_repeat_track", "yandex_music", "repeat_track", priority=993)
        if re.fullmatch(r"(?:(?:включи|активируй|запусти|включить)\s+)?(?:режим\s+)?(?:повтор\w*|репит|repeat)\s+(?:плейлист\w*|очеред\w*|контекст\w*)", yandex_state, flags=re.I):
            return MediaPhraseIntent("yandex_repeat_playlist", "yandex_music", "repeat_playlist", priority=993)
        if re.fullmatch(r"(?:выключи|отключи|убери|выключить)\s+(?:режим\s+)?(?:повтор\w*|репит|repeat)", yandex_state, flags=re.I):
            return MediaPhraseIntent("yandex_repeat_off", "yandex_music", "repeat_off", priority=993)

        if _has(_PLAYLIST, t) and (_has(_PLAY_VERB, t) or re.search(r"\b(?:плейлист|playlist)\b", t, flags=re.I)):
            q = _playlist_query(t, domain_pattern=_YANDEX)
            if q:
                return MediaPhraseIntent("yandex_playlist", "yandex_music", "playlist", q, priority=978)
        if (re.search(r"\b(?:моя\s+волна|мою\s+волну|волн\w*)\b", t) and _has(_PLAY_VERB, t)) or re.search(r"\b(?:моя\s+волна|мою\s+волну)\b", t):
            return MediaPhraseIntent("yandex_wave", "yandex_music", "my_wave", priority=976)
        if re.search(r"\b(?:мои|мою)\s+(?:треки|песни|музыку)\b", t) and (_has(_PLAY_VERB, t) or re.search(r"\b(?:мои|мою)\b", t)):
            return MediaPhraseIntent("yandex_my_tracks", "yandex_music", "my_tracks", priority=976)
        if re.search(r"\b(?:что|какая|какой|скажи|назови)\w*.*\b(?:сейчас\s+)?(?:играет|звучит|трек|песня)\b", t):
            return MediaPhraseIntent("yandex_status", "yandex_music", "status", priority=970)
        if re.search(r"\b(?:дизлайк(?:ни)?|дислайк(?:ни)?|dislike|thumbs\s+down|мне\s+не\s+нравится)\b", t):
            return MediaPhraseIntent("yandex_dislike", "yandex_music", "dislike", priority=974)
        if re.search(r"\b(?:убери|сними|удали)\w*.*\b(?:лайк|любим\w*|мне\s+нравится)\b", t):
            return MediaPhraseIntent("yandex_unlike", "yandex_music", "unlike", priority=972)
        if re.search(r"\b(?:лайкни|залайкай|поставь\w*\s+лайк|добавь\w*\s+в\s+любим\w*|мне\s+нравится)\b", t):
            return MediaPhraseIntent("yandex_like", "yandex_music", "like", priority=972)
        if _has(_STOP, t):
            return MediaPhraseIntent("yandex_stop", "yandex_music", "stop", priority=970)
        if _has(_SKIP, t):
            return MediaPhraseIntent("yandex_next", "yandex_music", "next", priority=970)
        if _has(_PREV, t):
            return MediaPhraseIntent("yandex_previous", "yandex_music", "previous", priority=970)
        if _has(_RESUME, t):
            return MediaPhraseIntent("yandex_resume", "yandex_music", "play", priority=971)
        if _has(_PAUSE, t):
            return MediaPhraseIntent("yandex_pause", "yandex_music", "pause", priority=970)
        if _has(_PLAY_VERB, t):
            q = _clean_query(t, domain_pattern=_YANDEX)
            if q and not re.fullmatch(r"(?:музык\w*|трек\w*|песн\w*)", q):
                return MediaPhraseIntent("yandex_play_named", "yandex_music", "play_track", q, priority=969)
        return None

    return None
