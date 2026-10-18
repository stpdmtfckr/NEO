"""Named, user-owned YouTube music bookmarks. No network or playback here."""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit


SETTINGS_KEY = "integrations.youtube.music_playlists"


def name_key(value: str) -> str:
    """Match spoken names without casing, spacing or accent distinctions."""
    value = unicodedata.normalize("NFKD", str(value).casefold())
    return " ".join("".join(c for c in value if not unicodedata.combining(c)).split())


def playlist_url(value: str) -> str:
    """Validate a playlist URL and drop video/trackers, retaining the list ID."""
    value = str(value or "").strip()
    if not value or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise ValueError("Укажите корректную ссылку YouTube с параметром list.")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold()
        allowed = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}
        if (parsed.scheme.casefold() not in {"https", "http"} or host not in allowed
                or parsed.username is not None or parsed.password is not None or parsed.port is not None):
            raise ValueError
        if host.endswith("youtu.be"):
            if not re.fullmatch(r"/[A-Za-z0-9_-]+/?", parsed.path):
                raise ValueError
        elif parsed.path not in {"/playlist", "/watch"}:
            raise ValueError
        lists = parse_qs(parsed.query, keep_blank_values=True).get("list", [])
        if len(lists) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{2,256}", lists[0]):
            raise ValueError
        return "https://www.youtube.com/playlist?list=" + lists[0]
    except (ValueError, UnicodeError):
        raise ValueError("Укажите корректную ссылку YouTube с параметром list.") from None


def rows_for_ui(value) -> list[dict[str, str]]:
    """Expose broken rows for repair instead of silently discarding user data."""
    if value is None:
        return []
    if not isinstance(value, list):
        return [{"name": "", "url": str(value)}]
    return [{"name": str(row.get("name", "") or ""), "url": str(row.get("url", "") or "")}
            if isinstance(row, dict) else {"name": "", "url": str(row or "")} for row in value]


def validate_playlists(rows) -> list[dict[str, str]]:
    if not isinstance(rows, list):
        raise ValueError("Некорректный список плейлистов YouTube.")
    output, names, urls = [], set(), set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("url"), str):
            raise ValueError("Некорректный список плейлистов YouTube.")
        name = " ".join(row["name"].split())
        if not name or not name_key(name):
            raise ValueError("Укажите название каждого плейлиста.")
        if len(name) > 100 or any(unicodedata.category(c).startswith("C") for c in name):
            raise ValueError("Название плейлиста должно содержать не более 100 символов.")
        key = name_key(name)
        if key in names:
            raise ValueError("Названия плейлистов должны быть уникальными.")
        url = playlist_url(row["url"])
        if url in urls:
            raise ValueError("Этот плейлист YouTube уже добавлен.")
        output.append({"name": name, "url": url})
        names.add(key)
        urls.add(url)
    return output


@dataclass(frozen=True)
class PlaylistRequest:
    name: str
    target: str = "local"
    explicit: bool = True
    youtube: bool = False


# Full request shells in the selected command language. Literal titles are
# captured after the shell; they are never translated or searched on the web.
_HEADS = {
    "ru": r"(?:включи|поставь|запусти|проиграй)(?:\s+мне)?\s+(?:(?:мой|мои|мою|этот|этот\s+самый)\s+)?",
    "en": r"(?:play|start|put\s+on)\s+(?:(?:my|the)\s+)?",
    "de": r"(?:spiel|spiele|starte)\s+(?:(?:die|meine|meinen|den)\s+)?",
    "fr": r"(?:joue|lance|mets)\s+(?:(?:ma|la|une|mon|le)\s+)?",
    "es": r"(?:reproduce|pon|inicia)\s+(?:(?:mi|la|una|el)\s+)?",
    "it": r"(?:riproduci|metti|avvia)\s+(?:(?:la\s+mia|la|mia|il)\s+)?",
    "pt": r"(?:reproduz|reproduza|toca|toque|inicia|inicie|coloca|coloque)\s+(?:(?:a\s+minha|a|minha|uma|o)\s+)?",
}
_NOUNS = {
    "ru": r"(?:плейлист|плей\s+лист|музыку)", "en": r"(?:playlist|music)",
    "de": r"(?:playlist|wiedergabeliste|musik)", "fr": r"(?:playlist|liste\s+de\s+lecture|musique)",
    "es": r"(?:playlist|lista\s+de\s+reproducción|música)",
    "it": r"(?:playlist|lista\s+di\s+riproduzione|musica)",
    "pt": r"(?:playlist|lista\s+de\s+reprodução|música)",
}
_NAMED = {
    "ru": r"(?:(?:под\s+названием|с\s+названием)\s+)?", "en": r"(?:(?:called|named)\s+)?",
    "de": r"(?:(?:namens)\s+)?", "fr": r"(?:(?:appelée?|nommée?)\s+)?",
    "es": r"(?:(?:llamada|llamado)\s+)?", "it": r"(?:(?:chiamata|chiamato)\s+)?",
    "pt": r"(?:(?:chamada|chamado)\s+)?",
}
_TARGETS = {
    "ru": (r"(?:в|на)\s+(?:discord|дискорд[еа]?)", r"(?:локально|на\s+(?:моём\s+|моем\s+)?компьютере)"),
    "en": (r"(?:in|on)\s+discord", r"(?:locally|on\s+(?:my|the)\s+computer)"),
    "de": (r"(?:in|auf)\s+discord", r"(?:lokal|auf\s+meinem\s+computer)"),
    "fr": (r"(?:sur|dans)\s+discord", r"(?:en\s+local|sur\s+mon\s+ordinateur)"),
    "es": (r"en\s+discord", r"(?:localmente|en\s+mi\s+(?:ordenador|computadora))"),
    "it": (r"(?:su|in)\s+discord", r"(?:localmente|sul\s+mio\s+computer)"),
    "pt": (r"(?:no|na|em)\s+discord", r"(?:localmente|no\s+meu\s+computador)"),
}
_POLITE = {"ru": "пожалуйста", "en": "please", "de": "bitte", "fr": "s’il vous plaît|s'il vous plaît", "es": "por favor", "it": "per favore", "pt": "por favor"}


def parse_request(text: str, code: str) -> PlaylistRequest | None:
    if code not in _HEADS:
        return None
    value = str(text or "").strip()
    # A quoted whole command, question, negation or multiple action sequence is
    # not an imperative. Agent's normal meta/dialogue guards still own it.
    if not value or value[0] in '\"\'«“„‘' or any(c in value for c in "\n\r"):
        return None
    value = re.sub(r"^(?:" + _POLITE[code] + r")[, ]+", "", value, flags=re.I)
    value = re.sub(r"[, ]+(?:" + _POLITE[code] + r")\.?$", "", value, flags=re.I).rstrip(".")
    head = re.match(_HEADS[code], value, flags=re.I)
    if head is None:
        return None
    body = value[head.end():].strip()
    # A named source/target from another provider keeps its existing route.
    # Quoted names ending with those words remain opaque names.
    if re.search(r"\s+(?:on|in|auf|sur|dans|en|su|no|na|em|в|на)\s+(?:spotify|спотифай|яндекс(?:\s+музык[аеу])?|yandex(?:\s+music)?|apple\s+music)\s*$", body, re.I):
        return None
    # NEO 188 atlas: «the saved YouTube playlist X», «YouTube-Playlist X», «la lista de YouTube X»,
    # «la playlist YouTube enregistrée X», «a playlist guardada do YouTube X».
    saved = r"(?:saved|gespeicherte|enregistrée|enregistree|guardada|salvata|сохранённый|сохраненный)"
    noun = re.match(r"(?:" + saved + r"\s+)?(?:(?P<before>youtube|ютуб)(?:-|\s+))?(?P<kind>" + _NOUNS[code] + r"|lista(?=\s+(?:" + saved + r"\s+)?de\s+youtube))"
                    r"(?:\s+" + saved + r")?(?:\s+(?:(?:do|de|di|von|del)\s+)?(?P<after>youtube|ютуб))?(?:\s+" + saved + r")?\s+" + _NAMED[code], body, re.I)
    youtube = bool(noun and (noun.group("before") or noun.group("after")))
    explicit = bool(noun and name_key(noun.group("kind")) not in {"музыку", "music", "musik", "musique", "musica"})
    if noun:
        body = body[noun.end():].strip()
    # German's separable verb suffix follows the literal playlist name/target.
    if code == "de":
        body = re.sub(r"\s+ab$", "", body, flags=re.I)
    target = "local"
    targets = _TARGETS[code]
    markers = []
    while (tail := re.search(r"\s+(?P<target>(?:" + targets[0] + "|" + targets[1] + r"))$", body, re.I)):
        marker = tail.group("target")
        markers.append("discord" if re.fullmatch(targets[0], marker, re.I) else "local")
        body = body[:tail.start()].strip()
    if markers:
        target = "conflict" if len(set(markers)) > 1 else markers[0]
    pairs = {'"': '"', '«': '»', '“': '”', '„': '“', "'": "'"}
    if len(body) > 1 and body[0] in pairs and body[-1] == pairs[body[0]]:
        body = body[1:-1].strip()
    if not body:
        return None
    return PlaylistRequest(body, target, explicit, youtube)


def resolve_request(text: str, code: str, rows) -> tuple[PlaylistRequest, dict[str, str] | None] | None:
    request = parse_request(text, code)
    if request is None:
        return None
    # A short name-only command is claimed only by an exact saved bookmark.
    # Malformed storage must never turn an unrelated "start app" into an error.
    known = any(name_key(row["name"]) == name_key(request.name) for row in rows_for_ui(rows))
    if not request.youtube and not known:
        return None
    entries = validate_playlists([] if rows is None else rows)
    match = next((row for row in entries if name_key(row["name"]) == name_key(request.name)), None)
    # Unconfigured catalogue playlists and ordinary music requests keep their
    # established provider route. Only YouTube explicitly owns unknown names.
    if match is None and not request.youtube and request.target != "conflict":
        return None
    return request, match


def playback_confirmed(data, *, discord: bool = False) -> bool:
    """Transport evidence for a play-now result, before any ownership update."""
    if not isinstance(data, dict):
        return False
    return (data.get("verified") is True
        and not any(data.get(key) for key in ("starting", "pending", "requires_confirmation", "paused",
            "cancelled", "superseded", "outcome_pending", "outcome_unknown", "requires_confirmation_of_result"))
        and data.get("completed") is not False and data.get("playing") is not False
        and str(data.get("state", "")).casefold() not in {"paused", "stopped", "error", "loading", "starting", "buffering"}
        and (not discord or (data.get("submitted") is True and
             (data.get("play_now") is True or data.get("queue_started_immediately") is True))))


def transport_action(text: str, code: str) -> str | None:
    """Only unqualified, exact existing transport forms inherit a bookmark's player."""
    from jarvis.runtime.music_requests157 import FORMS, BARE
    from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell
    if code not in LANGUAGES or any(mark in text for mark in ('"', "'", '«', '»', '“', '”', '‘', '’')):
        return None
    index = LANGUAGES.index(code)
    value = norm(strip_request_shell(text, code))
    for action in ("next", "previous", "pause", "play", "stop"):
        phrases = (FORMS[action][index] + "|" + BARE[action][index]).split("|")
        if value in {norm(phrase) for phrase in phrases}:
            return action
    return None
