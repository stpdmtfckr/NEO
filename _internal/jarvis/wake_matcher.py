# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import difflib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Iterable

# NEO 178: the wake word is «NEO». A three-letter name is close to everyday words
# («не», «не-а», «неа», «нет», «но»), so a short wake phrase never uses fuzzy or
# phonetic guessing: only a whole word from the alias list wakes NEO, words are never
# glued together («не о…» is not «нео»), and these sounds never wake it even if they
# appear in the alias list (for example after automatic alias learning). The
# requested ambiguous «ну» is the sole exception: configured standalone input only.
SHORT_WAKE_MAX_LETTERS = 5
SHORT_WAKE_STOP_WORDS = frozenset({
    "не", "неа", "неаа", "неее", "нее", "неё", "нея", "неэ", "нэ", "нэа", "неу", "ниа", "нет", "нету",
    "но", "ну", "на", "ни", "ня", "ноу", "нау", "нах", "нэт",
    "ne", "nea", "neah", "nee", "neh", "nia", "nya", "no", "noo", "nah", "naw", "nay", "nope", "nein", "non", "nao", "não", "na", "ni", "nu",
})
_HYPHENS = "-‐‑‒–—"
_STANDALONE_WAKE_ALIASES = frozenset({"ну"})
_WAKE_INPUT_LIMIT = 4096
_WAKE_EDGE_PUNCTUATION = " \t.!?,;:…"
_WAKE_TAIL_SEPARATORS = " ,.!?:;…—-。．！？：；，"
_SHORT_WAKE_TOKENS = re.compile(r"[^\W_]+(?:[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]+[^\W_]*)*")
_SINGLE_RECOGNITION = re.compile(r'''\[\s*(['"])([^\[\]\\\r\n'"]*)\1\s*\]''')


def _wake_text(value) -> str:
    """Decode one bounded ASR transcript, never evaluate a recognition repr.

    A one-element list or its simple printed form is an ASR transport wrapper.
    Multiple alternatives, nesting and escapes are ambiguous and fail closed.
    Other speech is kept byte-for-byte apart from outer whitespace.
    """
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            return ""
        value = value[0]
    if not isinstance(value, str) or len(value) > _WAKE_INPUT_LIMIT:
        return ""
    text = value.strip()
    if text.startswith("["):
        match = _SINGLE_RECOGNITION.fullmatch(text)
        return match.group(2).strip() if match else ""
    return text


@lru_cache(maxsize=1024)
def wake_alias_requires_standalone(value: str) -> bool:
    """Ambiguous ASR aliases are not assistant addresses or command prefixes."""
    return unicodedata.normalize("NFKC", _wake_text(value)).casefold().strip(_WAKE_EDGE_PUNCTUATION) in _STANDALONE_WAKE_ALIASES


def wake_alias_key(value: str) -> str:
    """Shared Unicode-safe key for bounded configured wake spellings."""
    return _normalize(_wake_text(value))

LEADING_FILLERS = {"эй", "hey", "хей", "ok", "okay", "окей", "привет", "здравствуй", "hello", "hi", "hallo", "bonjour", "salut", "hola", "ciao", "olá", "oi"}


@lru_cache(maxsize=8192)
def _normalize(value: str) -> str:
    """Unicode-safe wake normalization.

    Whisper can spell the same wake word with Polish diacritics, Hangul or
    another alphabet. ``isalnum`` keeps those letters instead of silently
    deleting them, while NFKC folds compatibility forms consistently.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(ch for ch in text if ch.isalnum())


_CYRILLIC_WAKE_LATIN = str.maketrans({
    "а":"a", "б":"b", "в":"v", "г":"g", "д":"d", "е":"e", "ё":"e",
    "ж":"j", "з":"z", "и":"i", "й":"y", "к":"k", "л":"l", "м":"m",
    "н":"n", "о":"o", "п":"p", "р":"r", "с":"s", "т":"t", "у":"u",
    "ф":"f", "х":"h", "ц":"c", "ч":"ch", "ш":"sh", "щ":"sh", "ы":"y",
    "э":"e", "ю":"u", "я":"a", "ь":"", "ъ":"",
})


@lru_cache(maxsize=8192)
def _latin_fold(value: str) -> str:
    """Fold likely ASR alphabets/diacritics into a comparison-friendly Latin form."""
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # Treat Russian/Belarusian/Ukrainian variants before single-character mapping.
    text = text.replace("дж", "j").replace("дз", "j")
    text = text.translate(_CYRILLIC_WAKE_LATIN)
    return "".join(ch for ch in text if "a" <= ch <= "z")


@lru_cache(maxsize=4096)
def _phonetic_signature(value: str) -> str:
    """Return a conservative consonant signature for Jarvis-like spellings.

    This is not a general speech recogniser. It only makes variants such as
    Jarvis/Jervis/Djarvis/Dżarwis/Жарвис/Gervis converge while fuzzy matching is
    still restricted to safe utterance positions below.
    """
    text = _latin_fold(value)
    if not text:
        return ""
    for src in ("dzh", "dj", "dzz", "dz", "zh"):
        text = text.replace(src, "j")
    # ASR engines regularly swap the initial affricate for nearby onsets.
    if text.startswith(("sh", "ch")):
        text = "j" + text[2:]
    # English/European soft G can represent J only before e/i/y. Mapping every
    # initial G made unrelated words such as "garrows" collapse to the exact
    # Jarvis consonant skeleton (jrvs). Exact configured G-aliases still work.
    if len(text) >= 2 and text.startswith("g") and text[1] in "eiy":
        text = "j" + text[1:]
    text = text.replace("w", "v").replace("f", "v")
    text = text.replace("z", "s").replace("c", "s").replace("x", "s")
    # Vowels are the least stable part across multilingual ASR renderings.
    text = re.sub(r"[aeiouy]+", "", text)
    text = re.sub(r"(.)\1+", r"\1", text)
    return text


def is_short_wake_phrase(phrase: str) -> bool:
    """«NEO»-like short wake phrases use the exact-word mode below."""
    letters = _normalize(phrase)
    return 2 <= len(letters) <= SHORT_WAKE_MAX_LETTERS


def _short_alias_key(value: str) -> str:
    """Normalized single-word alias, or '' when it cannot be a short wake word."""
    raw = unicodedata.normalize("NFKC", _wake_text(value)).casefold().strip(_WAKE_EDGE_PUNCTUATION)
    if len(raw) > 32 or not re.fullmatch(r"[^\W_]+", raw, flags=re.UNICODE):
        return ""
    key = _normalize(raw)
    if not (2 <= len(key) <= 8) or key in SHORT_WAKE_STOP_WORDS - _STANDALONE_WAKE_ALIASES:
        return ""
    return key


def _match_short_wake(heard: str, candidates: frozenset[str], *, allow_standalone_aliases=True) -> tuple[bool, str, float]:
    """Exact whole-word match of a short wake phrase anywhere in the utterance."""
    for token in _SHORT_WAKE_TOKENS.finditer(heard):
        key = _normalize(token.group(0))
        if not key or key not in candidates:
            continue
        if key in _STANDALONE_WAKE_ALIASES:
            if allow_standalone_aliases and _short_alias_key(heard) == key:
                return True, "", 1.0
            continue
        if key in SHORT_WAKE_STOP_WORDS:
            continue
        after = heard[token.end():token.end() + 2]
        before = heard[max(0, token.start() - 2):token.start()]
        # «нео-нацизм», «не-а»: part of a hyphenated word, not the name.
        if (after and after[0] == "_") or (before and before[-1] == "_") or \
                (len(after) == 2 and after[0] in _HYPHENS and after[1].isalnum()) or \
                (len(before) == 2 and before[1] in _HYPHENS and before[0].isalnum()):
            continue
        return True, heard[token.end():].lstrip(_WAKE_TAIL_SEPARATORS), 1.0
    return False, "", 0.0


def wake_alias_is_strong(value: str, phrase: str = "NEO") -> bool:
    """Return True only for aliases that are safe for immediate wake acceptance.

    Historical builds accumulated semantic Whisper hallucinations (``Service``,
    ``There it is``, ``Jeremy``...) in the same alias list as genuine phonetic
    Jarvis spellings.  Those observations are still useful as *rescue signals*,
    but accepting them directly makes ordinary room speech indistinguishable from
    the wake word.  Keep the hot path precision-first: immediate aliases must be
    short and acoustically/phoneticly Jarvis-like.
    """
    raw = str(value or "").strip().strip(".!?")
    if not raw:
        return False
    if is_short_wake_phrase(phrase):
        return bool(_short_alias_key(raw))
    tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", raw).casefold(), flags=re.UNICODE)
    if not (1 <= len(tokens) <= 2):
        return False
    if _normalize(raw) == _normalize(phrase):
        return True
    try:
        from jarvis.wake.v2.phonetics import CONFUSABLE_WORDS, fold, phonetic_score
        folded = fold(raw)
        lexical = raw.casefold().strip(" .!?,")
        if folded in CONFUSABLE_WORDS or lexical in CONFUSABLE_WORDS:
            return False
        # Same calibrated floor used by Wake V2: below this value the historical
        # corpus starts admitting unrelated words.
        return float(phonetic_score(raw, phrase or "Jarvis")) >= 0.66
    except Exception:
        return _safe_alias_shape(raw, _normalize(phrase))


def wake_rescue_match(value: str, rescue_aliases: Iterable[str]) -> bool:
    """Cheap exact rescue check for known *bad transcripts* of the wake word.

    Rescue aliases never wake NEO by themselves.  They merely justify spending
    the already-budgeted second tiny-Whisper probe on a different language view
    of the same audio.  This preserves recall without turning phrases such as
    ``there it is`` into wake words.
    """
    key = _normalize(value)
    if not key:
        return False
    for alias in rescue_aliases:
        if key == _normalize(str(alias or "")):
            return True
    return False


def _phonetic_similarity(value: str, candidates: Iterable[str]) -> float:
    signature = _phonetic_signature(value)
    # Require a Jarvis-family onset and enough consonants. This prevents ordinary
    # words such as "service" (srvs) from becoming a wake word just because their
    # consonant skeleton is superficially similar.
    if len(signature) < 3 or not signature.startswith("j") or "v" not in signature:
        return 0.0
    signatures = {_phonetic_signature(item) for item in candidates}
    signatures = {item for item in signatures if len(item) >= 3 and item.startswith("j")}
    if not signatures:
        return 0.0
    return max(difflib.SequenceMatcher(None, item, signature).ratio() for item in signatures)


@lru_cache(maxsize=1)
def _file_wake_defaults() -> tuple[str, tuple[str, ...]]:
    """Read packaged wake defaults; Python contains no wake spelling list."""
    try:
        path = Path(__file__).resolve().parents[1] / "config" / "default_settings.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        wake = payload.get("wake_word", {}) or {}
        phrase = str(wake.get("phrase", "") or "").strip()
        aliases = tuple(str(item).strip() for item in (wake.get("aliases", []) or []) if str(item).strip())
        return phrase, aliases
    except Exception:
        return "", ()


def _safe_alias_shape(value: str, target: str) -> bool:
    """Return True for aliases safe enough for fuzzy/any-position matching.

    Short field-observed ASR substitutions such as ``Doris`` or ``Janice`` are
    intentionally *not* classified as fuzzy-safe here.  They are still kept as
    exact observed aliases by :func:`_candidate_sets`, but only in safe utterance
    positions.  This preserves useful wake recovery without letting unrelated
    long speech become a wake word through fuzzy similarity.
    """
    item = _normalize(value)
    if not item:
        return False
    if item == target:
        return True
    raw_tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", str(value or "")).casefold(), flags=re.UNICODE)
    if not (1 <= len(raw_tokens) <= 2) or not (4 <= len(item) <= 14):
        return False
    folded = _latin_fold(value)
    if not folded or len(folded) > 16:
        return False
    # Fuzzy-safe Jarvis-family spellings normally retain R plus V/W/F and an
    # s/z/c-like tail.  Other short aliases are handled as exact observed ASR
    # substitutions instead of being discarded.
    folded = folded.replace("w", "v").replace("f", "v")
    has_rv = bool(re.search(r"r.{0,5}v|v.{0,5}r", folded))
    tail = folded[-4:]
    return has_rv and any(ch in tail for ch in "szc")


def _candidate_sets_uncached(phrase: str, aliases: Iterable[str] | None) -> tuple[set[str], set[str]]:
    """Return (strict, observed) normalized wake aliases.

    ``strict`` aliases may be matched anywhere and participate in fuzzy/phonetic
    recovery. ``observed`` aliases are short (1-2 token) field ASR substitutions
    such as Doris / Janice / Jeremy's / Жарились.  They are exact-only and are
    accepted only at the beginning (after an optional filler) or at the end of
    an utterance.  Long historical ASR phrases are deliberately excluded.
    """
    target = _normalize(phrase)
    if aliases is None:
        default_phrase, default_aliases = _file_wake_defaults()
        values = default_aliases if not target else ()
        if not target:
            target = _normalize(default_phrase)
    else:
        values = tuple(aliases)

    strict = {target} if target else set()
    observed: set[str] = set()
    if target and is_short_wake_phrase(phrase or target):
        # NEO 178: short wake phrase: whole single words only, never stop words.
        strict.update(key for key in (_short_alias_key(value) for value in values) if key)
        strict.discard("")
        strict.difference_update(SHORT_WAKE_STOP_WORDS - _STANDALONE_WAKE_ALIASES)
        return strict, observed
    for value in values:
        raw = str(value or "").strip()
        item = _normalize(raw)
        if not item:
            continue
        if _safe_alias_shape(raw, target):
            strict.add(item)
            continue
        raw_tokens = re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", raw).casefold(), flags=re.UNICODE)
        # Curated short aliases from real logs remain valid exact wake candidates.
        # This restores Doris/Janice/Jeremy's-like substitutions while ensuring
        # long ordinary phrases can never become aliases merely because they were
        # once seen in ASR output.
        if 1 <= len(raw_tokens) <= 2 and 4 <= len(item) <= 14:
            observed.add(item)
    observed.difference_update(strict)
    return strict, observed


@lru_cache(maxsize=128)
def _candidate_sets_cached(phrase: str, aliases: tuple[str, ...] | None) -> tuple[frozenset[str], frozenset[str]]:
    strict, observed = _candidate_sets_uncached(phrase, aliases)
    return frozenset(strict), frozenset(observed)


def _candidate_sets(phrase: str, aliases: Iterable[str] | None) -> tuple[frozenset[str], frozenset[str]]:
    # Wake matching runs many times per second. Re-normalizing hundreds of ASR
    # aliases on every audio probe was pure CPU overhead. The settings list changes
    # rarely, so key the prepared candidate sets by an immutable tuple. MainWindow
    # already passes a tuple; preserve it without rebuilding ~700 strings per probe.
    if aliases is None:
        packed = None
    elif isinstance(aliases, tuple):
        packed = aliases
    else:
        packed = tuple(str(item) for item in aliases)
    return _candidate_sets_cached(str(phrase or ""), packed)


def _short_mode(phrase: str, strict: Iterable[str]) -> bool:
    """Short-phrase mode, also when the phrase comes from the packaged defaults."""
    if str(phrase or "").strip():
        return is_short_wake_phrase(phrase)
    default_phrase, _aliases = _file_wake_defaults()
    return is_short_wake_phrase(default_phrase)


def _alias_set(phrase: str, aliases: Iterable[str] | None) -> set[str]:
    strict, observed = _candidate_sets(phrase, aliases)
    return strict | observed

def wake_candidate_similarity(value: str, phrase: str = "", *, aliases: Iterable[str] | None = None) -> float:
    value = _wake_text(value)
    item = _normalize(value)
    strict, observed = _candidate_sets(phrase, aliases)
    candidates = strict | observed
    if not item or not candidates:
        return 0.0
    if _short_mode(phrase, strict):
        return 1.0 if _short_alias_key(value) in candidates else 0.0
    if item in candidates:
        return 0.0 if item in SHORT_WAKE_STOP_WORDS else 1.0
    if not strict:
        return 0.0
    lexical = max(difflib.SequenceMatcher(None, alias, item).ratio() for alias in strict)
    phonetic = _phonetic_similarity(value, strict)
    return max(lexical, phonetic)


def looks_wake_like(
    value: str,
    phrase: str = "",
    *,
    minimum: float = 0.64,
    aliases: Iterable[str] | None = None,
) -> bool:
    """Cheap precision-first gate before an expensive forced-language retry."""
    value = _wake_text(value)
    token = _normalize(value)
    strict, observed = _candidate_sets(phrase, aliases)
    candidates = strict | observed
    if not token or not candidates:
        return False
    if _short_mode(phrase, strict):
        return _short_alias_key(value) in candidates
    if token in candidates:
        return True
    # Only strict Jarvis-family aliases participate in fuzzy recovery. Observed
    # short substitutions are exact-only.
    onset = [candidate for candidate in strict if candidate and candidate[0] == token[0]]
    lexical = max((difflib.SequenceMatcher(None, item, token).ratio() for item in onset), default=0.0)
    phonetic = _phonetic_similarity(value, strict)
    return max(lexical, phonetic) >= minimum


def match_wake_phrase(
    heard: str,
    phrase: str = "",
    *,
    threshold: float = 0.86,
    aliases: Iterable[str] | None = None,
    phonetic_recovery: bool = True,
    allow_standalone_aliases: bool = True,
) -> tuple[bool, str, float]:
    """Match a configured wake phrase in a rolling utterance.

    Exact configured aliases are allowed *anywhere* in the utterance.  This is
    important for real microphone windows such as ``"а, Джарвис"`` or a long
    sentence ending in ``"... Джарвис"``: background/filler speech before the
    wake word must not hide it.  Fuzzy recovery remains precision-first and is
    limited to the beginning (after a tiny filler) or the final few tokens, so
    ordinary long conversation is not scanned fuzzily for accidental matches.
    """
    heard = _wake_text(heard)
    if not heard:
        return False, "", 0.0
    strict_candidates, observed_candidates = _candidate_sets(phrase, aliases)
    candidates = strict_candidates | observed_candidates
    if not candidates:
        return False, "", 0.0
    if _short_mode(phrase, strict_candidates):
        return _match_short_wake(heard, strict_candidates, allow_standalone_aliases=allow_standalone_aliases)
    tokens = list(re.finditer(r"[^\W_]+", heard, flags=re.UNICODE))
    if not tokens:
        return False, "", 0.0
    # HOTFIX40 precision floor. User logs showed unrelated natural speech at
    # 0.889 activating the wake transition; fuzzy recovery below 0.92 is no
    # longer allowed. Exact safe aliases remain score 1.0.
    threshold = max(0.92, min(0.97, float(threshold)))

    # Pass 1: exact aliases. Strict Jarvis-family aliases are valid anywhere.
    # Short field-observed substitutions (Doris/Janice/Jeremy's/etc.) are valid
    # only at a safe edge: utterance start (after an optional filler) or end.
    filler_offset = 0
    if _normalize(tokens[0].group(0)) in LEADING_FILLERS and len(tokens) > 1:
        filler_offset = 1
    for index in range(len(tokens)):
        # Prefer the longest exact alias so Jeremy's is consumed before Jeremy.
        for width in (3, 2, 1):
            end_index = index + width
            if end_index > len(tokens):
                continue
            value = _normalize("".join(t.group(0) for t in tokens[index:end_index]))
            if not value:
                continue
            strict_hit = value in strict_candidates
            observed_hit = value in observed_candidates and (index == filler_offset or end_index == len(tokens))
            if strict_hit or observed_hit:
                end = tokens[end_index - 1].end()
                return True, heard[end:].lstrip(" ,.!?:;…—-"), 1.0

    def fuzzy_score(raw: str) -> float:
        value = _normalize(raw)
        if not value or not strict_candidates:
            return 0.0
        same_onset = [item for item in strict_candidates if item and item[0] == value[0]]
        lexical = max((difflib.SequenceMatcher(None, item, value).ratio() for item in same_onset), default=0.0)
        phonetic = _phonetic_similarity(raw, strict_candidates)
        return max(lexical, phonetic)

    # Pass 2: fuzzy recovery only in safe positions.  Start-of-utterance keeps the
    # historical behaviour; final positions cover "... <slightly mangled Jarvis>".
    start_index = 0
    first_norm = _normalize(tokens[0].group(0))
    if first_norm in LEADING_FILLERS and len(tokens) > 1:
        start_index = 1
    positions = [start_index]
    for index in range(max(0, len(tokens) - 3), len(tokens)):
        if index not in positions:
            positions.append(index)

    best_score = 0.0
    best_end = -1
    for index in positions:
        for width in (1, 2):
            end_index = index + width
            if end_index > len(tokens):
                continue
            joined = "".join(t.group(0) for t in tokens[index:end_index])
            score = fuzzy_score(joined)
            if score > best_score:
                best_score = score
                best_end = tokens[end_index - 1].end()
    if best_end >= 0 and best_score >= threshold:
        return True, heard[best_end:].lstrip(" ,.!?:;…—-"), best_score

    # ---------------------------------------------------------------- V5
    # Pass 3: phonetic recovery (added in V5.0).
    #
    # This runs ONLY after passes 1 and 2 have declined, so every one of the
    # configured aliases keeps behaving exactly as before -- nothing in the
    # list is removed, weakened or reordered. The phonetic model adds coverage
    # for renderings that are simply not in the list yet, which is what stops
    # the list needing a new entry for every Whisper quirk.
    #
    # Calibrated against 6 717 real ASR lines from neo_jarvis.log: 0 false
    # accepts at threshold 0.66. See jarvis/wake/v2/ and docs/DIAGNOSIS.md §5.
    if phonetic_recovery:
        try:
            from jarvis.wake.v2 import WakeConfig, WakeMatcher
        except Exception:
            return False, "", 0.0
        try:
            matcher = _phonetic_matcher(phrase or "NEO", tuple(aliases or ()))
            decision = matcher.match(heard)
            if decision.accepted:
                return True, decision.command_tail, float(decision.score)
        except Exception:
            pass
    return False, "", best_score


@lru_cache(maxsize=8)
def _phonetic_matcher(phrase: str, aliases: tuple[str, ...]):
    """Build (once) a V2 matcher that also honours every configured alias.

    The full alias list is passed through as ``exceptions`` so an explicitly
    configured spelling is always accepted regardless of its phonetic distance
    -- the user's list wins, the model only extends it.
    """
    from jarvis.wake.v2 import WakeConfig, WakeMatcher

    return WakeMatcher(WakeConfig(phrase=phrase, exceptions=aliases))
