# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Strict single-locale command normalization for NEO JARVIS.

Only the language selected in Settings is loaded from disk.  Arbitrary payloads
(song titles, file paths, URLs, application/game names) are preserved verbatim.
The selected locale is normalized to the mature internal Russian command surface;
that internal representation is an implementation detail, not a second language
corpus and is never used to accept foreign command syntax.
"""

import difflib
import hashlib
import json
from jarvis.runtime import cached_re as re
import unicodedata
from contextlib import contextmanager
import contextvars
import threading
from functools import lru_cache
from pathlib import Path

from jarvis.language import accepted_command_languages
from jarvis.runtime.natural_commands import normalize_natural_command, strip_request_shell, negated_action

TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿА-Яа-яЁё0-9]+", re.UNICODE)
_NORM_RE = re.compile(r"[^a-zà-öø-ÿа-яё0-9]+")
_SPACE_NONWORD_RE = re.compile(r"[^a-zà-öø-ÿа-яё0-9]+")
_SPACE_RE = re.compile(r"\s+")
_BARE_URL_RE = re.compile(r"(?:https?://\S+|www\.\S+|[a-z0-9-]+\.(?:com|ru|net|org|io|gg|dev|app|fr|de|es|it|pt)(?:/\S*)?)", re.I)
SUPPORTED = frozenset({"ru","en","de","fr","es","it","pt"})
_LIBRARY_DIR = Path(__file__).resolve().parents[1] / "locales" / "commands"
_FOREIGN_HASH_PATH = Path(__file__).resolve().parents[2] / "config" / "atlas2" / "foreign_phrase_hashes.json"

ACTION_KEYS = frozenset({
    "открой","закрой","запусти","покажи","скрой","найди","создай","скопируй","перемести","переименуй","удали",
    "прочитай","напиши","вставь","очисти","сохрани","отправь","скачай","установи","подключись","отключись",
    "сверни","разверни","восстанови","переключи","перейди","нажми","выбери","включи","выключи","пауза","стоп",
    "продолжи","дальше","скип","назад","перемотай","перемешай","повтор","лайк","дизлайк","громче","тише","приглуши",
    "заблокируй","перезагрузи","усыпи","запомни","забудь","переведи","проанализируй","расскажи","объясни","помоги",
})


def _code(code: str) -> str:
    value = str(code or "ru").casefold()
    return value if value in SUPPORTED else "ru"


@lru_cache(maxsize=2)
def _library(lang: str) -> dict[str, object]:
    """Load only one selected command JSON (plus at most the previous selection)."""
    code = _code(lang)
    try:
        payload = json.loads((_LIBRARY_DIR / f"{code}.json").read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


@lru_cache(maxsize=2)
def _aliases_for(lang: str) -> dict[str, tuple[str, ...]]:
    payload = _library(_code(lang))
    aliases = payload.get("aliases", {}) if isinstance(payload, dict) else {}
    parsed: dict[str, tuple[str, ...]] = {}
    if isinstance(aliases, dict):
        for canonical, values in aliases.items():
            if isinstance(values, list):
                clean = tuple(str(x).strip() for x in values if str(x).strip())
                if clean:
                    parsed[str(canonical)] = clean
    return parsed


def _norm(s: str) -> str:
    s = str(s or "").casefold().replace("’", "'")
    return _NORM_RE.sub("", s)


def _space_norm(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("’", "'")
    text = _SPACE_NONWORD_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _mutations(token: str) -> set[str]:
    key = _norm(token)
    if not key:
        return set()
    out = {key}
    if len(key) >= 5:
        for i in range(1, len(key)-1):
            out.add(key[:i] + key[i+1:])
        for i in range(0, len(key)-1):
            out.add(key[:i] + key[i+1] + key[i] + key[i+2:])
        out.add(key + key[-1])
    return out


@lru_cache(maxsize=2)
def _index(code: str) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    exact_owners: dict[str, set[str]] = {}
    fuzzy: dict[str, set[str]] = {}
    for canonical, aliases in _aliases_for(_code(code)).items():
        bucket = fuzzy.setdefault(canonical, set())
        for alias in aliases:
            # Multi-word aliases are rewritten first.  Token mutations are used
            # only for individual words and never for arbitrary payloads.
            parts = TOKEN_RE.findall(alias)
            if len(parts) != 1:
                continue  # An idiom is not a set of independent command words.
            for part in parts:
                key = _norm(part)
                if not key or len(key) < 2:
                    continue
                bucket.add(key)
                for mut in _mutations(key):
                    exact_owners.setdefault(mut, set()).add(canonical)
    exact = {k: next(iter(v)) for k, v in exact_owners.items() if len(v) == 1}
    return exact, {k: tuple(sorted(v)) for k, v in fuzzy.items()}


@lru_cache(maxsize=2)
def _alias_keys(code: str) -> frozenset[str]:
    _exact, fuzzy = _index(_code(code))
    return frozenset(key for values in fuzzy.values() for key in values)


_GUI_FAST_PATH188 = contextvars.ContextVar("neo_gui_fast_path188", default=False)


@contextmanager
def gui_fast_path():
    """HIST-023: the GUI thread never builds the cold typo catalogue.

    Inside this scope an unbuilt catalogue means «keep the word as typed»: exact
    shortcuts still work at once, and the agent worker (which waits for the one
    background build) does the full spelling repair."""
    token = _GUI_FAST_PATH188.set(True)
    try:
        yield
    finally:
        _GUI_FAST_PATH188.reset(token)


def _real_word(token: str, code: str) -> bool:
    if _GUI_FAST_PATH188.get():
        from jarvis.runtime.command_repair161 import ready
        if not ready(_code(code)):
            return True                      # not cached: the answer changes once the catalogue exists
    return _real_word_cached(token, code)


@lru_cache(maxsize=4096)
def _real_word_cached(token: str, code: str) -> bool:
    """A correctly spelled word of the selected language (command or lexicon)."""
    word = unicodedata.normalize("NFC", str(token or "")).casefold()
    if len(word) < 4:
        return False
    try:
        from jarvis.runtime.command_typos165 import known
        return bool(known(word, _code(code)))
    except Exception:
        return False


@lru_cache(maxsize=64)
def _fuzzy_candidates(code: str, key_len: int) -> tuple[tuple[str, str], ...]:
    # Preserve the exact legacy iteration order while pre-filtering impossible
    # lengths once. SequenceMatcher is the expensive part of command normalization.
    rows: list[tuple[str, str]] = []
    max_delta = max(2, int(key_len) // 3)
    _exact, fuzzy = _index(_code(code))
    for name, aliases in fuzzy.items():
        for alias in aliases:
            if abs(len(alias) - int(key_len)) <= max_delta:
                rows.append((name, alias))
    return tuple(rows)


@lru_cache(maxsize=8192)
def _fuzzy_canonical(code: str, key: str, first_position: bool) -> str:
    best_name = ""
    best = 0.0
    second = 0.0
    threshold = 0.80 if first_position else 0.92
    for name, alias in _fuzzy_candidates(_code(code), len(key)):
        score = difflib.SequenceMatcher(None, key, alias).ratio()
        if score > best:
            second = best
            best = score
            best_name = name
        elif score > second:
            second = score
    if best_name and best >= threshold and best - second >= (0.08 if first_position else 0.12):
        return best_name
    return ""


@lru_cache(maxsize=2)
def _phrase_rewrites(code: str) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for canonical, aliases in _aliases_for(_code(code)).items():
        for alias in aliases:
            if len(TOKEN_RE.findall(alias)) >= 2:
                rows.append((alias, canonical))
    rows.sort(key=lambda x: len(x[0]), reverse=True)
    return tuple(rows)


@lru_cache(maxsize=2)
def _compiled_phrase_rewrites(code: str) -> tuple[tuple[re.Pattern[str], str], ...]:
    # Regex compilation used to happen for every phrase on every user command.
    # Keep the public phrase table unchanged, but compile the selected locale once.
    return tuple(
        (re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.I), canonical)
        for phrase, canonical in _phrase_rewrites(_code(code))
    )


_DICTATE182 = re.compile(
    r"^\s*(écris|ecris|tape|saisis|escribe|escriba|teclea|scrivi|digita|escreve|escreva|digite|"
    r"schreib|schreibe|tippe|write|type|напиши|введи|напечатай)\s*[:,\-]?\s+(\S.*)$",
    re.I | re.S,
)
_DICTATE_OBJECT182 = re.compile(
    r"\b(?:fichier|document|note|lettre|message|mail|courriel|archivo|documento|nota|carta|mensaje|correo|"
    r"file|lettera|messaggio|arquivo|mensagem|datei|dokument|notiz|brief|nachricht|e-?mail|letter|"
    r"файл\w*|документ\w*|заметк\w*|письм\w*|сообщени\w*)\b",
    re.I,
)


_DICTATE_VERBS184 = {
    'ru': r'напиши|введи|напечатай', 'en': r'write|type',
    'de': r'schreib|schreibe|tippe', 'fr': r'écris|ecris|tape|saisis',
    'es': r'escribe|escriba|teclea', 'it': r'scrivi|digita',
    'pt': r'escreve|escreva|digite',
}
# Object words inside text do not turn dictation into a file/message command.
# Only a leading, syntactic target can do that; reporting predicates are text.
_DICTATE_TARGET184 = re.compile(
    r"^(?:(?:to|into|in|a|en|dans|nel|nella|no|na|em|в|на)\s+)?"
    r"(?:(?:the|a|an|my|le|la|les|un|une|mon|ma|el|una|mi|il|lo|o|um|uma|die|das|den|eine|ein)\s+)?"
    r"(?:fichier|document|note|lettre|message|mail|courriel|archivo|documento|nota|carta|mensaje|correo|"
    r"file|lettera|messaggio|arquivo|mensagem|datei|dokument|notiz|brief|nachricht|e-?mail|letter|"
    r"файл\w*|документ\w*|заметк\w*|письм\w*|сообщени\w*)\b"
    r"(?!\s+(?:says|said|reads|contains|shows|states|is|was|dit|dice|diz|sagt|говорит|гласит|содержит)\b)",
    re.I,
)


_DICTATE_FRAME187 = {
    'ru': r'(?:эту |следующую )?(?:фразу|текст|слова|строку)',
    'en': r'(?:down\s+)?(?:the\s+|this\s+)?(?:phrase|text|words|line|sentence)',
    'de': r'(?:den\s+|die\s+|diesen\s+)?(?:satz|text|worte|zeile)',
    'fr': r'(?:la\s+|le\s+|les\s+|cette\s+)?(?:phrase|texte|mots)',
    'es': r'(?:la\s+|el\s+|las\s+|esta\s+)?(?:frase|texto|palabras)',
    'it': r'(?:la\s+|il\s+|le\s+|questa\s+)?(?:frase|testo|parole)',
    'pt': r'(?:a\s+|o\s+|as\s+|esta\s+)?(?:frase|texto|palavras)',
}


def dictation_payload184(text: str, code: str) -> str | None:
    """Exact opaque data for a selected-language explicit dictation shell.

    Structured file/note/message requests keep their established parsers. No
    normalization, punctuation stripping or zero-width cleanup touches data.
    """
    verbs = _DICTATE_VERBS184.get(_code(code))
    if not verbs:
        return None
    value = str(text or '').strip()
    found = re.match(r'^(?:' + verbs + r')\s*[:,\-]?\s+(?P<payload>\S.*)$', value, re.I | re.S)
    if not found:
        return None
    # Existing exact incomplete-command contracts take precedence over the
    # generic dictation interpretation ("write files", "type input", etc.).
    # This is a syntax-only lookup; the callback never executes or resolves tools.
    from jarvis.runtime.dialogue_tasks import _partial
    if _partial(value, _code(code), lambda _value: None, 0.0, 60.0) is not None:
        return None
    payload = found.group('payload')
    if _DICTATE_TARGET184.match(payload):
        return None
    # NEO 187: «write down the phrase “X”» / «напиши фразу «X»» dictate X, not
    # the framing words. Only a framing noun followed by quoted data is removed.
    framed = _DICTATE_FRAME187.get(_code(code))
    if framed:
        inner = re.match(r'^(?:' + framed + r')\s*[:\-]?\s*(?P<data>[«"“„].+)$', payload, re.I | re.S)
        if inner:
            payload = inner.group('data').strip()
            if (payload[0], payload[-1]) == ('„', '“'):
                payload = payload[1:-1]
    if len(payload) >= 2 and (payload[0], payload[-1]) in {('"', '"'), ('«', '»'), ('“', '”'), ("'", "'")}:
        french = payload[0] == '«'
        payload = payload[1:-1]
        if french and len(payload) >= 2:
            # « bonjour » — typographic guillemet spacing is not part of the text.
            payload = re.sub(r'^[ \u00a0\u202f]', '', re.sub(r'[ \u00a0\u202f]$', '', payload))
    return payload


_PLAYLIST_NAME186 = re.compile(
    r"(?P<noun>\b(?:playlist|playlists|wiedergabeliste|liste de lecture|lista de reproducción|lista de reproduccion|"
    r"lista de reprodução|lista de reproducao|scaletta)\s+)"
    r"(?P<name>(?![\ue000])[^\s,.;:!?][^,.;:!?\r\n]*?)"
    r"(?=\s+(?:in|on|auf|im|sur|dans|en|su|no|na|em|from|von|de|da|di)\s+(?:the\s+)?(?:yandex|spotify|youtube|discord|яндекс|ютуб)\b|[,.;:!?]|\s*$)",
    re.I,
)


def normalize_multilingual_command(text: str, code: str) -> str:
    original = str(text or "")
    if not original.strip():
        return original
    code = _code(code)
    if negated_action(original, code):
        return original
    # MEDIA185-U03: a complete native music transport/mode phrase is one typed
    # command; it is never translated word by word.
    from jarvis.runtime.media_grammar186 import canonical_media_command
    media186 = canonical_media_command(original, code)
    if media186:
        return media186
    # NEO 188 atlas: a whole on/off request for Bluetooth, Wi-Fi or the microphone.
    from jarvis.runtime.toggle188 import toggle_command188
    toggle188 = toggle_command188(original, code)
    if toggle188:
        return toggle188
    # NEO 188 atlas: relative seek of the current video («avance de dix secondes»).
    from jarvis.runtime.seek188 import seek_command188
    seek188 = seek_command188(original, code)
    if seek188:
        return seek188
    # NEO 182/183: dictation payload is opaque user data — only the verb is localised, and the check
    # runs on the text as spoken (the request-shell cleanup used to cut a closing «!»/«?»).
    opaque184 = dictation_payload184(original, code)
    if opaque184 is not None:
        return 'напиши: ' + opaque184
    dictated = _DICTATE182.match(original) or _DICTATE182.match(strip_request_shell(original, code))
    if not (dictated and not _DICTATE_OBJECT182.search(dictated.group(2))):
        natural = normalize_natural_command(original, code)
        if natural is not None:
            return natural
    original = strip_request_shell(original, code)
    if dictated and not _DICTATE_OBJECT182.search(dictated.group(2)):
        payload = dictated.group(2).strip()
        if len(payload) >= 2 and (payload[0], payload[-1]) in {('"', '"'), ("«", "»"), ("“", "”"), ("'", "'")}:
            payload = payload[1:-1]          # «escribe "Hola"» types Hola, not the quotes
        return "напиши: " + payload
    # Quoted user data, paths and URLs are opaque. Previous global alias
    # replacement changed titles such as "Pause" and even path components.
    protected: list[str] = []
    def protect(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return "\ue000" + str(len(protected) - 1) + "\ue001"
    out = re.sub(r'"[^"\r\n]*"|«[^»\r\n]*»|https?://\S+|[A-Za-z]:[\\/][^\r\n,;]+', protect, original)
    def restore(value: str) -> str:
        return re.sub("\ue000([0-9]+)\ue001", lambda m: protected[int(m.group(1))], value)
    def protect_name(value: str) -> str:
        protected.append(value)
        return "\ue000" + str(len(protected) - 1) + "\ue001"
    # MEDIA185-U03: an unquoted playlist name is user data too («Night and Day»
    # must not become «ночь and day»). Only the name after the playlist noun is
    # protected; a trailing service qualifier stays a command word.
    out = _PLAYLIST_NAME186.sub(lambda m: m.group("noun") + protect_name(m.group("name")), out)
    # NEO 188 atlas: remember which internal Russian word stands for which word the
    # user actually said, so free-text slots can get the user's own words back.
    pairs188: list[tuple[str, str]] = []
    def rewrite188(canonical: str):
        def apply(match: re.Match[str]) -> str:
            pairs188.append((canonical, match.group(0)))
            return canonical
        return apply
    for pattern, canonical in _compiled_phrase_rewrites(code):
        out = pattern.sub(rewrite188(canonical), out)
    exact, fuzzy = _index(code)
    alias_keys = _alias_keys(code)
    spans = list(TOKEN_RE.finditer(out))
    if not spans:
        return restore(out)
    repl: dict[int, str] = {}
    # The shell/domain portion is bounded.  Everything after it is treated as
    # user payload and is not fuzzy-translated.
    for i, m in enumerate(spans[:8]):
        key = _norm(m.group(0))
        canonical = exact.get(key)
        if canonical:
            # MEDIA185-U03-H01: a typo variant must not swallow a different,
            # correctly spelled word (es «pasa» is not «pausa», de «setze» is
            # not «Seite»). Real alias words always map.
            if key in alias_keys or not _real_word(m.group(0), code):
                repl[i] = canonical
            continue
        if len(key) < 4 or _real_word(m.group(0), code):
            continue
        best_name = _fuzzy_canonical(code, key, i == 0)
        if best_name:
            repl[i] = best_name
    pairs188.extend((canonical, spans[i].group(0)) for i, canonical in sorted(repl.items()))
    _remember_pivot188(text, code, pairs188)
    if not repl:
        return restore(out)
    chunks: list[str] = []; last = 0
    for i, m in enumerate(spans):
        chunks.append(out[last:m.start()]); chunks.append(repl.get(i, m.group(0))); last = m.end()
    chunks.append(out[last:])
    return restore("".join(chunks))


# NEO 188 atlas (contract 4.2): the Russian command view is internal. Words the
# pivot replaced are recorded per phrase so a slot value built from that view
# (an app name, a search query, a title) can be mapped back to the raw words.
_PIVOT188: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}
_PIVOT_LOCK188 = threading.Lock()
_FREE_TEXT_SLOTS188 = frozenset({
    "name", "query", "title", "text", "content", "label", "playlist", "topic", "subject", "body",
    "message", "description", "question", "instruction", "new_name", "track", "artist", "album",
})


_TURN_PIVOT188: contextvars.ContextVar = contextvars.ContextVar('neo_turn_pivot188', default=None)


@contextmanager
def pivot_scope188(code: str):
    """One user turn: every pivot made during it can be mapped back at the tool boundary."""
    token = _TURN_PIVOT188.set({'code': _code(code), 'pairs': []})
    try:
        yield
    finally:
        _TURN_PIVOT188.reset(token)


def _remember_pivot188(text: str, code: str, pairs: list[tuple[str, str]]) -> None:
    kept = tuple((c, o) for c, o in pairs if c and o and c.casefold() != o.casefold())
    turn = _TURN_PIVOT188.get()
    if turn is not None and turn['code'] == _code(code):
        turn['pairs'].extend(pair for pair in kept if pair not in turn['pairs'])
    with _PIVOT_LOCK188:
        if len(_PIVOT188) >= 64:
            _PIVOT188.pop(next(iter(_PIVOT188)))
        _PIVOT188[(str(text or ""), _code(code))] = kept


def pivot_pairs188(text: str, code: str) -> tuple[tuple[str, str], ...]:
    with _PIVOT_LOCK188:
        return _PIVOT188.get((str(text or ""), _code(code)), ())


def restore_turn_slots188(arguments: dict) -> dict:
    """The tool-boundary form: the active turn's pivots, nothing outside a turn."""
    turn = _TURN_PIVOT188.get()
    if turn is None or not turn['pairs']:
        return arguments
    return restore_slot_words188(arguments, '', turn['code'])


def restore_slot_words188(arguments: dict, raw_text: str, code: str) -> dict:
    """Put the user's own words back into free-text slots of a pivoted command.

    Only words the multilingual pivot itself substituted are mapped back; the
    Russian selected language has no pivot, and enum/path arguments are untouched.
    """
    if _code(code) == "ru" or not isinstance(arguments, dict):
        return arguments
    turn = _TURN_PIVOT188.get()
    pairs = tuple(turn['pairs']) if turn is not None and turn['code'] == _code(code) else pivot_pairs188(raw_text, code)
    if not pairs:
        return arguments
    out = dict(arguments)
    for key, value in arguments.items():
        if key not in _FREE_TEXT_SLOTS188 or not isinstance(value, str) or not value:
            continue
        fixed = value
        for canonical, original in sorted(pairs, key=lambda pair: -len(pair[0])):
            fixed = re.sub(rf"(?<!\w){re.escape(canonical)}(?!\w)", lambda _m, o=original: o, fixed, flags=re.I)
        out[key] = fixed
    return out


_DOMAIN_KEYS = frozenset({
    "погода","температура","прогноз","дождь","ветер","влажность","осадки","восход","закат",
    "календарь","событие","встреча","непрочитанные","почта","письмо","таймер","напоминание","будильник",
    "процессор","видеокарта","оперативная","сводка","приложение","файл","папка","окно","музыка","видео",
    "браузер","дискорд","яндекс","ютуб","громкость","микрофон","блютуз","вайфай","экран","сеть",
})


@lru_cache(maxsize=2)
def _selected_signal_index(code: str) -> tuple[tuple[tuple[str, int], ...], tuple[str, ...]]:
    amap = _aliases_for(_code(code))
    action_rows: list[tuple[str, int]] = []
    domain_rows: list[str] = []
    for canonical in ACTION_KEYS:
        for alias in amap.get(canonical, ()):
            normalized = _space_norm(alias)
            if normalized:
                action_rows.append((normalized, 4 if " " in normalized else 3))
    for canonical in _DOMAIN_KEYS:
        for alias in amap.get(canonical, ()):
            normalized = _space_norm(alias)
            if normalized:
                domain_rows.append(normalized)
    return tuple(action_rows), tuple(domain_rows)


def _selected_command_signal(text: str, code: str) -> int:
    prefix = _space_norm(text)
    if not prefix:
        return 0
    words = prefix.split()[:5]
    short = " ".join(words)
    score = 0
    action_rows, domain_rows = _selected_signal_index(code)
    first_word = words[0] if words else ""
    for alias, weight in action_rows:
        if short == alias or short.startswith(alias + " "):
            score = max(score, weight)
        elif first_word == alias:
            score = max(score, 3)
    # Non-imperative information/status commands: native domain words anywhere
    # in the short shell are sufficient evidence without examining other locales.
    full = " " + prefix + " "
    for alias in domain_rows:
        if f" {alias} " in full:
            score += 1
    return score


FOREIGN_COMMAND_SENTINELS: dict[str, frozenset[str]] = {
    # Tiny isolation firewall only; these are not routing dictionaries and are
    # never normalized into actions. They prevent obvious foreign imperatives
    # from reaching tool selection without loading another locale JSON.
    "ru": frozenset({"открой","закрой","покажи","найди","создай","удали","включи","выключи","пауза","стоп","дальше","назад","отправь","переведи"}),
    "en": frozenset({"open","close","show","find","search","create","delete","play","pause","stop","next","previous","resume","skip","back","like","dislike","send","translate","restart","shutdown"}),
    "de": frozenset({"öffne","oeffne","schließe","schliesse","zeige","finde","suche","erstelle","lösche","loesche","spiele","pausiere","stoppe","weiter","fortsetzen","zurück","zurueck","gefällt","gefaellt","sende","übersetze","uebersetze"}),
    "fr": frozenset({"ouvre","ferme","affiche","montre","trouve","cherche","crée","cree","supprime","joue","pause","arrête","arrete","reprends","continue","suivant","précédent","precedent","aime","envoie","traduis"}),
    "es": frozenset({"abre","cierra","muestra","busca","encuentra","crea","elimina","reproduce","pausa","detén","deten","reanuda","continúa","continua","siguiente","anterior","gusta","envía","envia","traduce"}),
    "it": frozenset({"apri","chiudi","mostra","cerca","trova","crea","elimina","riproduci","pausa","ferma","riprendi","continua","successivo","precedente","piace","invia","traduci"}),
    "pt": frozenset({"abra","abre","feche","fecha","mostre","mostra","pesquise","pesquisa","procura","encontre","encontra","crie","cria","exclua","apaga","reproduza","reproduz","pause","pausa","pare","para","retome","retoma","continue","continua","próxima","proxima","anterior","curtir","envie","envia","traduza","traduz"}),
}

_FOREIGN_TOKEN_OWNERS: dict[str, tuple[str, ...]] = {}
for _lang, _sentinels in FOREIGN_COMMAND_SENTINELS.items():
    for _token in _sentinels:
        _FOREIGN_TOKEN_OWNERS.setdefault(_token, tuple())
        _FOREIGN_TOKEN_OWNERS[_token] = (*_FOREIGN_TOKEN_OWNERS[_token], _lang)


@lru_cache(maxsize=1)
def _foreign_phrase_hash_index() -> dict[str, tuple[str, ...]]:
    """Load only one-way hashes of reviewed Phrase Atlas commands.

    The hash index is intentionally not a second language corpus: it contains no
    command text and cannot be used for routing. It exists solely to reject an
    exact command example that belongs to a non-selected locale before fallback
    or URL ownership can see it.
    """
    try:
        data = json.loads(_FOREIGN_HASH_PATH.read_text(encoding="utf-8"))
        rows = data.get("hash_owners", {}) if isinstance(data, dict) else {}
        if not isinstance(rows, dict):
            return {}
        return {str(k): tuple(str(x) for x in v if str(x) in SUPPORTED) for k, v in rows.items() if isinstance(v, list)}
    except Exception:
        return {}


def _phrase_digest(value: str) -> str:
    normalized = _space_norm(value)
    return hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()[:24] if normalized else ""


def command_language_violation(text: str, code: str) -> tuple[bool, str]:
    """Reject command shells spoken in a non-selected language.

    Only the selected Phrase Atlas is loaded. A bare URL/domain remains language
    neutral, but adding a foreign imperative (including after "Jarvis"/polite
    wrappers) is rejected before URL routing. Shared international verbs such as
    ``pause`` are accepted when they are also valid in the selected locale.
    """
    code = _code(code)
    value = _space_norm(text)
    if not value:
        return False, ""

    # A reviewed selected-language shell is stronger evidence than an isolated
    # shared token inside its payload (Spanish "para" is not Portuguese "stop"
    # in "Pon una alarma para…"). Load only the selected grammar.
    from jarvis.runtime.canonical_commands import matches_selected_grammar
    if matches_selected_grammar(text, code) or normalize_natural_command(text, code) is not None:
        return False, ""

    from jarvis.runtime.music_requests157 import parse_music_request157, bare_transport157
    from jarvis.runtime.everyday_requests157 import everyday_request157
    from jarvis.runtime.script_requests157 import script_request157
    from jarvis.runtime.location_requests158 import parse_location_request
    if parse_location_request(text, code):
        return False, ""
    from jarvis.runtime.file_requests162 import parse_file_request
    if parse_file_request(text, code):
        return False, ""
    if script_request157(text, code):
        return False, ""
    if parse_music_request157(text, code) or bare_transport157(text, code) or everyday_request157(text, code):
        return False, ""

    from jarvis.runtime.command_contracts150 import short_request
    if short_request(text, code):
        return False, ""

    digest = _phrase_digest(value)
    owners = _foreign_phrase_hash_index().get(digest, ()) if digest else ()
    if owners:
        # An exact reviewed Atlas phrase in the selected locale is authoritative.
        # This prevents shared Romance/Germanic imperative tokens (for example
        # Portuguese ``mostra`` vs Italian ``mostra``) from being falsely rejected
        # by the tiny sentinel firewall below. If the exact phrase belongs only to
        # other locales, reject it before any URL/fallback route can inspect it.
        if code in owners:
            return False, ""
        return True, next((lang for lang in owners if lang != code), owners[0])

    words = value.split()
    # Inspect a short command shell, ignoring common wake/polite wrappers. We do
    # not scan the full payload, so a foreign word inside a song/file title cannot
    # steal the route.
    wrapper = {
        "jarvis", "neo", "please", "bitte", "s'il", "sil", "vous", "plaît", "plait",
        "por", "favor", "per", "favore", "porfavor", "пожалуйста", "джарвис", "босс", "boss",
    }
    shell = [w for w in words[:6] if w not in wrapper][:4]
    selected_sentinels = FOREIGN_COMMAND_SENTINELS.get(code, frozenset())
    for token in shell:
        owners = _FOREIGN_TOKEN_OWNERS.get(token, ())
        if not owners:
            continue
        # A shared command word is valid when it belongs to the selected locale.
        if token in selected_sentinels:
            break
        foreign = next((lang for lang in owners if lang != code), "")
        if foreign:
            return True, foreign
        break

    if _selected_command_signal(value, code) > 0:
        return False, ""
    # Bare URLs/domains are language-neutral. The foreign shell was already
    # checked above, so this exception cannot bypass strict language isolation.
    if _BARE_URL_RE.fullmatch(str(text).strip()):
        return False, ""
    return False, ""

def command_library_payload(code: str) -> dict[str, object]:
    code = _code(code)
    return {
        "language": code,
        "accepted_with": list(accepted_command_languages(code)),
        "aliases": {k: list(v) for k, v in _aliases_for(code).items()},
        "phrase_rewrites": [{"phrase": p, "canonical": c} for p, c in _phrase_rewrites(code)],
    }


@lru_cache(maxsize=2)
def _known_vocabulary(code: str) -> frozenset[str]:
    known: set[str] = set()
    for _canonical, aliases in _aliases_for(_code(code)).items():
        for alias in aliases:
            for part in TOKEN_RE.findall(alias):
                key = _norm(part)
                if key:
                    known.add(key)
    return frozenset(known)


def command_vocabulary_score_for_language(text: str, code: str) -> float:
    words = [_norm(x) for x in TOKEN_RE.findall(str(text or ""))]
    words = [w for w in words if w]
    if not words:
        return 0.0
    known = _known_vocabulary(code)
    hits = sum(1 for w in words[:8] if w in known)
    return hits / max(1, min(len(words), 8))


def multilingual_hotwords(code: str, limit: int = 80) -> list[str]:
    words: list[str] = []; seen: set[str] = set()
    for _canonical, aliases in _aliases_for(_code(code)).items():
        for alias in aliases:
            for token in TOKEN_RE.findall(alias):
                key = _norm(token)
                if len(key) >= 3 and key not in seen:
                    seen.add(key); words.append(token)
                    if len(words) >= limit:
                        return words
    return words
