# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import difflib
import re
from typing import Iterable

from jarvis.runtime.text_normalization import normalize_spoken_command
from jarvis.runtime.phrase_atlas import domain_conflict as phrase_atlas_domain_conflict


def norm(text: str) -> str:
    value = normalize_spoken_command(text)
    return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip(" .,!?:;¿¡—-\t\r\n")


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]+", norm(text), flags=re.I)


def fuzzy_token(text: str, variants: Iterable[str], *, threshold: float = 0.80) -> bool:
    toks = tokens(text)
    refs = [norm(x) for x in variants if norm(x)]
    for token in toks:
        for ref in refs:
            if token == ref or token.startswith(ref) or ref.startswith(token):
                if min(len(token), len(ref)) >= 4:
                    return True
            if min(len(token), len(ref)) >= 4 and difflib.SequenceMatcher(None, token, ref).ratio() >= threshold:
                return True
    return False


def explicit_video(text: str) -> bool:
    t = norm(text)
    if re.search(r"\b(?:видео\w*|ролик\w*|ютуб\w*|youtube|video)\b", t):
        return True
    return fuzzy_token(t, ("ютуб", "youtube", "видео"), threshold=0.84)


def explicit_music(text: str) -> bool:
    t = norm(text)
    # A Discord music-bot phrase is its own integration domain, not Yandex Music.
    if re.search(r"\b(?:музыкальн\w*\s+бот\w*|dj\s*bot|дидже\w*\s+бот\w*)\b", t):
        return False
    return bool(re.search(r"\b(?:музык\w*|песн\w*|трек\w*|композиц\w*|аудио\w*|яндекс[\s._-]*музык\w*|yandex[\s._-]*music|music|song|track)\b", t))


def explicit_discord(text: str) -> bool:
    t = norm(text)
    return bool(re.search(r"\b(?:discord\w*|discort\w*|diskord\w*|diskort\w*|дискорд\w*|дискорт\w*|дискор\w*|сервер\w*|текстов\w*\s+канал\w*|голосов\w*\s+канал\w*|войс\w*|музыкальн\w*\s+бот\w*|dj\s*bot)\b", t))


def explicit_discord_media(text: str) -> bool:
    """True only for an explicit Discord media/bot domain.

    Generic words such as ``server`` must not steal unrelated media commands,
    while "музыкальный бот" is strong enough to mean the Discord player even
    when the user omits the word Discord in a follow-up.
    """
    t = norm(text)
    return bool(re.search(
        r"\b(?:discord\w*|discort\w*|diskord\w*|diskort\w*|дискорд\w*|дискорт\w*|дискор\w*|музыкальн\w*\s+бот\w*|дидже\w*\s+бот\w*|dj\s*bot)\b",
        t,
    ))


def explicit_yandex_music(text: str) -> bool:
    t = norm(text)
    # A generic «плейлист» belongs to the single active music provider.
    # Only an explicit Yandex namespace or the Yandex-specific My Wave feature
    # may claim the Yandex domain. This prevents Spotify/Apple playlist requests
    # from being stolen by the legacy Yandex firewall.
    return bool(
        re.search(r"\b(?:яндекс[\s._-]*музык\w*|yandex[\s._-]*music)\b", t)
        or re.search(r"\b(?:моя\s+волна|мою\s+волну)\b", t)
    )


def explicit_spotify_music(text: str) -> bool:
    return bool(re.search(r"\b(?:spotify|спотифай\w*|спотифи\w*|спотиф\w*)\b", norm(text)))




def pause_like(text: str, *, strong_domain: bool = False) -> bool:
    t = norm(text)
    if re.search(r"\b(?:пауза|паузу|паузе|паузы|pause|приостанов\w*|останов\w*)\b", t):
        return True
    # Whisper sometimes turns «паузу» into «паулу». Only permit the fuzzy rescue
    # when the utterance already has an unambiguous media object / pause grammar.
    if strong_domain and re.search(r"\b(?:на|в)\b", t) and fuzzy_token(t, ("паузу", "пауза"), threshold=0.78):
        return True
    return False


def resume_like(text: str) -> bool:
    t = norm(text)
    return bool(re.search(r"\b(?:продолж\w*|возобнов\w*|воспроизвед\w*|доиграй|resume|play)\b", t))


_WEB_HOST_RE = re.compile(
    r"(?<![@\w.-])(?P<url>(?:https?://|www\.)?"
    r"(?:localhost(?::\d{1,5})?|(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?|"
    r"(?:[a-z0-9а-яё](?:[a-z0-9а-яё-]{0,61}[a-z0-9а-яё])?\.)+"
    r"(?:[a-zа-яё]{2,63}|xn--[a-z0-9-]{2,59})(?::\d{1,5})?)"
    r"(?:/[^\s<>{}\[\]\\\"'«»]*)?)",
    re.I,
)
_URL_OPEN_SHELL_RE = re.compile(
    r"\b(?:открой|открыть|зайди|зайти|перейди|перейти|запусти|покажи|"
    r"open|go\s+to|visit|navigate(?:\s+to)?)\b",
    re.I,
)
_URL_FILE_SUFFIXES = {
    "txt", "json", "yaml", "yml", "ini", "log", "pdf", "doc", "docx", "xls", "xlsx",
    "ppt", "pptx", "zip", "rar", "7z", "py", "js", "ts", "html", "htm", "css", "md",
    "exe", "lnk", "url", "appref-ms",
}


def extract_explicit_browser_url(text: str) -> str:
    """Return a normalized URL only when the utterance clearly means navigation.

    The parser intentionally runs before the generic desktop resolver.  It accepts
    schemes, ``www.*``, bare domains/IPs and both verb orders (``открой github.com``
    and ``github.com открой``).  Explicit browser-search grammar still owns its
    payload, and file-looking suffixes are rejected so ``открой report.pdf`` stays
    in the filesystem domain.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    # "найди ... в браузере" is a search request even if its query contains a URL.
    if looks_like_browser_search(raw):
        return ""
    match = _WEB_HOST_RE.search(raw)
    if not match:
        return ""
    candidate = str(match.group("url") or "").strip().rstrip(".,;:!?)]}»")
    if not candidate:
        return ""
    has_scheme = bool(re.match(r"^https?://", candidate, re.I))
    has_www = candidate.casefold().startswith("www.")
    bare = re.sub(r"^https?://", "", candidate, flags=re.I).split("/", 1)[0]
    host_no_port = bare.rsplit(":", 1)[0] if ":" in bare and not bare.count(":") > 1 else bare
    suffix = host_no_port.rsplit(".", 1)[-1].casefold() if "." in host_no_port else ""
    if not has_scheme and not has_www and suffix in _URL_FILE_SUFFIXES:
        return ""

    # A fully-qualified URL can stand alone. Bare hosts require a navigation shell
    # unless the whole utterance itself is just the address.
    remainder = (raw[:match.start()] + " " + raw[match.end():]).strip(" \t\r\n,.;:!?—-()[]{}«»\"")
    # Mail.ru is also a mailbox provider name. A command to inspect messages
    # must not become browser navigation merely because the provider has a dot.
    if host_no_port.casefold() in {"mail.ru", "gmail.com", "outlook.com"} and re.search(
        r"\b(?:письм\w*|писем|почт\w*|непрочитан\w*|emails?|messages?|inbox|unread|courriels?|correo\w*|mensagens|messaggi|postfach)\b",
        remainder, re.I,
    ) and not re.search(r"\b(?:сайт|браузер|website|browser|navigateur|navegador|sito)\b", remainder, re.I):
        return ""
    address_only = not remainder
    if not (has_scheme or _URL_OPEN_SHELL_RE.search(raw) or address_only):
        return ""
    return candidate if has_scheme else "https://" + candidate


def looks_like_browser_search(text: str) -> bool:
    """Only an explicit *browser* request may open a search page.

    V7.7 deliberately moves "загугли" / "найди в интернете" to the
    SearchAPI Google-AI answer lane. This predicate is therefore intentionally
    narrow: it represents a user asking to see the search *in a browser*.
    """
    t = norm(text)
    return bool(
        re.search(r"\b(?:найди|поищи|отыщи|покажи|открой)\w*\b", t)
        and re.search(r"\b(?:в\s+браузер\w*|через\s+браузер\w*|в\s+поисковик\w*)\b", t)
    )


def looks_like_web_lookup(text: str) -> bool:
    """Explicit request for an answer from the internet, not browser UI."""
    t = norm(text)
    if re.match(r"^(?:загугли|погугли|google|гугл)\b", t):
        return True
    return bool(
        re.search(r"\b(?:найди|поищи|отыщи|проверь)\w*\b", t)
        and re.search(r"\b(?:в\s+интернет\w*|в\s+сети|через\s+google|через\s+гугл|google|гугл)\b", t)
        and not looks_like_browser_search(t)
    )


def looks_like_general_question(text: str) -> bool:
    t = norm(text)
    # Built-in UI languages share the same factual-question lane.  Local-domain
    # firewalls run before this predicate, so extending the interrogative shell
    # does not steal explicit media/system/browser commands.
    question = re.match(
        r"^(?:"
        r"что(?:\s+такое)?|кто(?:\s+такой|\s+такая)?|где|когда|как|какой|какая|какое|какие|"
        r"почему|зачем|сколько|объясни|расскажи|поясни|сравни|"
        r"what|who|where|when|how|which|why|explain|tell\s+me|compare|"
        r"was|wer|wo|wann|wie|welch\w*|warum|wieso|erkl[aä]r\w*|erz[aä]hl\w*|vergleich\w*|"
        r"quoi|qu['’]?est[- ]ce|qui|o[uù]|quand|comment|pourquoi|expliqu\w*|racont\w*|compar\w*|"
        r"qu[eé]|qui[eé]n|d[oó]nde|cu[aá]ndo|c[oó]mo|cu[aá]l|por\s+qu[eé]|explic\w*|cu[eé]ntame|compar\w*|"
        r"che\s+cos['’][eè]|che|cosa|chi|dove|quando|come|quale|perch[eé]|spieg\w*|raccont\w*|confront\w*|"
        r"o\s+que|quem|onde|quando|como|qual|por\s+que|porque|explic\w*|conte(?:-me)?|compar\w*"
        r")\b", t, re.I
    )
    return bool(question) and not looks_like_browser_search(t)


def strip_browser_search_scaffolding(text: str) -> str:
    t = norm(text)
    t = re.sub(r"^(?:загугли|погугли|найди|поищи|отыщи)\s+", "", t)
    location = r"(?:в\s+браузер\w*|в\s+интернет\w*|в\s+сети|через\s+google|через\s+гугл)"
    t = re.sub(rf"^{location}\s+", "", t)
    t = re.sub(rf"\s+{location}(?:\s+(?:про|о|об))?\s*", " ", t)
    t = re.sub(r"^(?:информаци\w*\s+)?(?:про|о|об)\s+", "", t)
    t = re.sub(r"^информаци\w*\s+(?:про|о|об)?\s*", "", t)
    t = re.sub(r"\s+(?:про|о|об)\s+", " ", t, count=1)
    return re.sub(r"\s+", " ", t).strip(" ,.-")


def normalize_entity_phrase(value: str) -> str:
    """Repair a few high-confidence ASR entity slips without touching payload text."""
    t = norm(value)
    t = re.sub(r"^(?:и\s+)?(?:а?ндекс|ендекс|yandex)\s+музык\w*\b", "яндекс музыка", t)
    t = re.sub(r"\b(?:дискордт|дскорд|дискорд)\b", "discord", t)
    t = re.sub(r"\b(?:стеам|стимм)\b", "steam", t)
    return re.sub(r"\s+", " ", t).strip()


def monitor_number(value: str) -> int | None:
    raw = norm(value)
    if raw.isdigit():
        number = int(raw)
        return number if 1 <= number <= 16 else None
    stems = (
        (r"перв\w*|один|одн\w*", 1),
        (r"втор\w*|два|две", 2),
        (r"трет\w*|три", 3),
        (r"четвер\w*|четыр\w*", 4),
        (r"пят\w*|пять", 5),
        (r"шест\w*|шесть", 6),
        (r"седьм\w*|семь", 7),
        (r"восьм\w*|восемь", 8),
    )
    for pattern, number in stems:
        if re.fullmatch(pattern, raw):
            return number
    return None


def explicit_window_move(text: str) -> bool:
    t = norm(text)
    return bool(
        re.search(r"\b(?:перенес\w*|перемест\w*|перетащ\w*|отправ\w*)\b", t)
        and re.search(r"\b(?:экран\w*|монитор\w*|диспле\w*)\b", t)
    )


def cross_domain_conflict(tool: str, action: str, text: str) -> str:
    """Return a reason when a proposed LLM tool call contradicts explicit semantics.

    Media domains are intentionally mutually exclusive.  This firewall is the
    last line of defence after deterministic routing, so an LLM fallback cannot
    turn "YouTube", "Discord" or "Яндекс Музыка" into another player.
    """
    original_text = str(text or "")
    t = norm(original_text)
    tool = norm(tool)
    action = norm(action)
    atlas_reason = phrase_atlas_domain_conflict(tool, action, original_text)
    if atlas_reason:
        return atlas_reason
    try:
        from jarvis.routing.ownership_firewall import routed_conflict
        ownership_reason = routed_conflict(tool, {"action": action})
    except Exception:
        ownership_reason = ""
    if ownership_reason:
        return "turn ownership: " + ownership_reason
    video = explicit_video(t)
    music = explicit_music(t)
    discord_media = explicit_discord_media(t)
    yandex_media = explicit_yandex_music(t)
    spotify_media = explicit_spotify_music(t)
    media_actions = {
        "play", "pause", "stop", "play_track", "music_bot_play",
        "music_queue_next", "music_queue_add", "music_queue_status",
        "music_queue_clear", "music_skip", "music_pause", "music_resume",
        "music_stop", "music_shuffle_on", "music_shuffle_off", "shuffle_on", "shuffle_off",
        "my_wave", "my_tracks", "next", "previous",
        "seek_forward", "seek_backward",
    }
    if video and tool in {"yandex_music", "spotify", "music", "discord"} and action in media_actions:
        return "явный объект 'видео/YouTube' запрещает музыкальный intent"
    if tool == "media" and action in media_actions and not video:
        return "media tool зарезервирован только для явного YouTube/видео; музыка принадлежит active-provider music tool"
    if discord_media and not video and tool in {"yandex_music", "spotify", "music", "media"} and action in media_actions:
        return "явный Discord/музыкальный бот запрещает desktop music/общий media intent"
    if yandex_media and not discord_media and not video and tool in {"discord", "media", "spotify", "music"} and action in media_actions:
        return "явная Яндекс Музыка запрещает другие media providers"
    if spotify_media and not discord_media and not video and tool in {"discord", "media", "yandex_music", "music"} and action in media_actions:
        return "явный Spotify запрещает другие media providers"
    if looks_like_browser_search(t) and tool in {"yandex_music", "spotify", "music", "discord"}:
        return "явный поиск в браузере запрещает музыкальные интеграции"
    if explicit_window_move(t) and tool in {"application", "desktop"} and action in {"open", "launch", "run"}:
        return "явное перемещение окна запрещает запуск нового приложения"
    # Destructive/closing desktop actions require an explicit imperative from the
    # user. This is deliberately independent of game routing: if a knowledge
    # question ever falls through to the LLM, the model still cannot turn a noun
    # phrase (e.g. ``класс вышел последним`` or ``урон``) into window.close or
    # application.close merely because the target text looks entity-like.
    if tool in {"window", "application"} and action in {"close", "kill", "terminate", "stop", "end"}:
        explicit_close = bool(re.search(
            r"\b(?:закрой\w*|заверш\w*|останов\w*|убей\w*|выключ\w*\s+(?:приложен\w*|программ\w*|процесс\w*)|"
            r"close|quit|exit|kill|terminate|end\s+task)\b", t, re.I
        ))
        if not explicit_close:
            return "закрытие/завершение приложения или окна требует явной команды пользователя"
    if music and re.search(r"\b(?:найди|поищи|отыщи)\b", t) and tool == "browser" and action == "search" and not looks_like_browser_search(t):
        return "явный поиск песни/трека без браузерного указания относится к музыкальному приложению"
    return ""
