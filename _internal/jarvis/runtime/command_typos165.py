"""Typo tolerance for typed and spoken commands. Proposes text, never executes.

Three bounded layers sit in front of the existing grammar:

* keyboard layout: ``jnrhjq gfgre`` -> ``открой папку`` (ЙЦУКЕН <-> QWERTY);
  a token flips only when the flipped form is a known word and the original is
  not, so Latin names such as Steam or Discord never change;
* soft repair (before routing): an unknown word may become the nearest command
  word; a real word («пагода») only when the whole result is made of command
  words («какая погода»). The cheapest edits are tried first and a result is
  accepted only when it parses as a typed command while the original does not;
* rescue (only instead of the final "I did not understand"): real-word and
  short-word slips («поставь ан паузу») are allowed and validated the same way.

Edit costs follow how people actually mistype: neighbouring keys, doubled or
dropped letters, swapped letters, unstressed vowels and missing diacritics are
cheap. Negation, quoted text, paths and URLs are never touched, a guess may never
produce an outgoing or destructive verb, and non-imperative verb forms
(«включил») never change, so a statement cannot become a command.

Resources: the command-word index (~90 KB) and the real-word lexicon (~170 KB)
are sorted integer arrays built lazily once per language; a correctly typed
command costs a few binary searches (tens of microseconds).
"""
from __future__ import annotations

from array import array
import base64
from bisect import bisect_left
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any, Callable, Iterable
import zlib

from jarvis.runtime.natural_commands import LANGUAGES, norm

_ROOT = Path(__file__).resolve().parents[2]
_LEXICON = _ROOT / 'config' / 'typo_lexicon165.json'

_TOKEN = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)
# Everything after quoted text, a path, an URL or an address is user data.
_DATA_START = re.compile(r"[\"«“„`]|[A-Za-z]:[\\/]|\\\\|/|https?:|www\.|@|\{")
_NEGATION = {'не', 'нет', 'ни', 'not', "don't", 'dont', 'never', 'nicht', 'kein', 'keine', 'ne', 'pas',
             'no', 'non', 'não', 'nao', 'nunca', 'nie', 'jamais', 'mai'}

_ROWS = {
    'ru': ('ёйцукенгшщзхъ', 'фывапролджэ', 'ячсмитьбю'),
    'en': ('qwertyuiop', 'asdfghjkl', 'zxcvbnm'),
    'de': ('qwertzuiopü', 'asdfghjklöä', 'yxcvbnm'),
    'fr': ('azertyuiop', 'qsdfghjklm', 'wxcvbn'),
    'es': ('qwertyuiop', 'asdfghjklñ', 'zxcvbnm'),
    'it': ('qwertyuiopè', 'asdfghjklòàù', 'zxcvbnm'),
    'pt': ('qwertyuiop', 'asdfghjklç', 'zxcvbnm'),
}
# Letters that sound alike or are commonly confused in spelling.
_SOUND = {
    'ru': ('оа', 'еи', 'ея', 'иы', 'ую', 'её', 'ьъ', 'шщ', 'еэ', 'зс', 'дт', 'бп', 'вф', 'гк', 'жш', 'цс', 'ий'),
    'en': ('ck', 'sz', 'iy', 'ae', 'ei', 'ou'),
    'de': ('ck', 'sz', 'iy', 'ei', 'dt', 'fv'),
    'fr': ('ck', 'sz', 'iy', 'ae', 'ei', 'ou'),
    'es': ('bv', 'sz', 'cz', 'iy', 'ae', 'ei', 'ou'),
    'it': ('ck', 'sz', 'iy', 'ae', 'ei', 'ou'),
    'pt': ('sz', 'cs', 'iy', 'ae', 'ei', 'ou'),
}
# The same physical keys on ЙЦУКЕН and US QWERTY, lower and shifted.
_RU_KEYS = 'ёйцукенгшщзхъфывапролджэячсмитьбю'
_EN_KEYS = "`qwertyuiop[]asdfghjkl;'zxcvbnm,."
_EN_SHIFT = '~QWERTYUIOP{}ASDFGHJKL:"ZXCVBNM<>'
_EN_TO_RU = str.maketrans(_EN_KEYS + _EN_SHIFT, _RU_KEYS + _RU_KEYS.upper())
_RU_TO_EN = str.maketrans(_RU_KEYS + _RU_KEYS.upper(), _EN_KEYS + _EN_KEYS.upper())
_LATIN_KEYS = re.compile(r"[A-Za-z`\[\];',.{}:\"<>~]+")
_CYRILLIC = re.compile(r"[А-Яа-яЁё]+")


@dataclass(frozen=True)
class Repair:
    text: str
    cost: float
    changes: tuple[tuple[str, str], ...]
    real_words: bool = False  # a real word (not merely an unknown one) was edited


def _fold(ch: str) -> str:
    return unicodedata.normalize('NFKD', ch)[:1]


@lru_cache(maxsize=7)
def _keyboard(code: str):
    rows = _ROWS.get(code, _ROWS['en'])
    position = {ch: (r, c) for r, row in enumerate(rows) for c, ch in enumerate(row)}
    sound = {frozenset(pair) for pair in _SOUND.get(code, ())}
    return position, sound


def _sub_cost(a: str, b: str, code: str) -> float:
    if a == b:
        return 0.0
    position, sound = _keyboard('en' if code == 'ru' and a.isascii() else code)
    if _fold(a) == _fold(b):
        return 0.2  # missing/extra diacritic: é/e, ü/u, ñ/n
    if frozenset((a, b)) in sound:
        return 0.35  # unstressed vowel, voiced/voiceless consonant
    pa, pb = position.get(a), position.get(b)
    if pa and pb and abs(pa[0] - pb[0]) <= 1 and abs(pa[1] - pb[1]) <= 1:
        return 0.5  # neighbouring key
    return 1.0


def distance(source: str, target: str, code: str, cap: float = 2.0) -> float:
    """Weighted Damerau distance from what was typed to a dictionary word."""
    n, m = len(source), len(target)
    if abs(n - m) > 2:
        return 9.0
    before: list[float] | None = None
    previous = [0.0]
    for j in range(1, m + 1):
        previous.append(previous[-1] + (0.4 if j > 1 and target[j - 1] == target[j - 2] else 0.8))
    for i in range(1, n + 1):
        extra = 0.4 if i > 1 and source[i - 1] == source[i - 2] else 0.8  # key pressed twice
        current = [previous[0] + extra]
        for j in range(1, m + 1):
            missing = 0.4 if j > 1 and target[j - 1] == target[j - 2] else 0.8  # dropped double
            value = min(previous[j] + extra, current[j - 1] + missing,
                        previous[j - 1] + _sub_cost(source[i - 1], target[j - 1], code))
            if (before is not None and j > 1 and source[i - 1] == target[j - 2]
                    and source[i - 2] == target[j - 1]):
                value = min(value, before[j - 2] + 0.5)  # swapped neighbours
            current.append(value)
        # A swap on the next row can still reach back to this row's predecessor.
        if min(current) > cap and min(previous) + 0.5 > cap:
            return 9.0
        before, previous = previous, current
    return previous[m]


def budget(word: str) -> float:
    size = len(word)
    return 0.5 if size <= 3 else 1.0 if size <= 5 else 1.3 if size <= 7 else 1.8


def _deletes(word: str) -> set[str]:
    return {word[:i] + word[i + 1:] for i in range(len(word))}


@lru_cache(maxsize=7)
def vocabulary(code: str) -> frozenset[str]:
    """Fixed command words of the selected language (shared with repair161)."""
    from jarvis.runtime.command_repair161 import catalog
    words = {w for w in catalog(code)[2] if len(w) >= 2 and _TOKEN.fullmatch(w)}
    return frozenset(words)


def word_hash(word: str) -> int:
    return zlib.crc32(word.encode('utf-8')) & 0xFFFFFFFF


@lru_cache(maxsize=7)
def _index(code: str) -> tuple[tuple[str, ...], array]:
    """Symmetric-delete index: CRC32(word or word minus one letter) << 16 | word id.

    About 11k sorted uint64 values (~90 KB) instead of a dict of strings; a hash
    collision only adds a candidate that the distance check then rejects.
    """
    words = tuple(sorted(vocabulary(code)))[:0xFFFF]
    entries = sorted({word_hash(key) << 16 | number
                      for number, word in enumerate(words) for key in {word} | _deletes(word)})
    return words, array('Q', entries)


def _near_words(key: str, code: str) -> Iterable[str]:
    words, entries = _index(code)
    base = word_hash(key)
    at = bisect_left(entries, base << 16)
    while at < len(entries) and entries[at] >> 16 == base:
        yield words[entries[at] & 0xFFFF]
        at += 1


def pack_hashes(values: Iterable[int]) -> str:
    """Sorted unique CRC32 values as base64 of little-endian uint32 (builder side)."""
    table = array('I', sorted(set(values)))
    if sys.byteorder == 'big':
        table.byteswap()
    return base64.b64encode(table.tobytes()).decode('ascii')


def _unpack_hashes(value: str) -> array:
    table = array('I')
    table.frombytes(base64.b64decode(value))
    if sys.byteorder == 'big':
        table.byteswap()
    return table


def _lexicon_file() -> dict[str, dict[str, str]]:
    """Read once per language by _tables(); the raw text is not kept in memory."""
    try:
        data = json.loads(_LEXICON.read_text(encoding='utf-8'))
        return dict(data.get('languages', {}))
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


@lru_cache(maxsize=7)
def _tables(code: str) -> tuple[array, array] | None:
    """Real word forms near command words of one language: sorted CRC32 arrays.

    Only the selected language is unpacked (4 bytes per word). A hash collision
    only makes one more word "real", i.e. slightly more careful.
    """
    row = _lexicon_file().get(code)
    if not isinstance(row, dict):
        return None
    try:
        return _unpack_hashes(row.get('soft', '')), _unpack_hashes(row.get('stable', ''))
    except (ValueError, TypeError):
        return None


def _lexicon() -> dict[str, tuple[array, array]]:
    return {code: tables for code in LANGUAGES if (tables := _tables(code)) is not None}


def _member(table: array, word: str) -> bool:
    key = word_hash(word)
    at = bisect_left(table, key)
    return at < len(table) and table[at] == key


@lru_cache(maxsize=7)
def _names(code: str) -> frozenset[str]:
    """Statements and names that are valid exactly as typed."""
    from jarvis.runtime.command_repair160 import STATEMENTS
    words: set[str] = set(STATEMENTS.get(code, '').split())
    try:
        from jarvis.desktop_resolver import APP_ALIASES
        for alias in APP_ALIASES:
            words.update(norm(part) for part in _TOKEN.findall(str(alias)))
    except Exception:
        pass
    try:
        from jarvis.runtime.utterance_purpose import _DEFAULT_ADDRESSES
        for alias in _DEFAULT_ADDRESSES:
            words.update(norm(part) for part in _TOKEN.findall(str(alias)))
    except Exception:
        pass
    return frozenset(words)


def is_stable(word: str, code: str) -> bool:
    """Never edited, not even by the rescue: verb forms, statements, names."""
    tables = _tables(code)
    return word in _names(code) or bool(tables and _member(tables[1], word))


def is_real_word(word: str, code: str) -> bool:
    """A real word that merely looks like a typo of a command word."""
    tables = _tables(code)
    return bool(tables and _member(tables[0], word)) or is_stable(word, code)


@lru_cache(maxsize=7)
def _forbidden_targets(code: str) -> frozenset[str]:
    """Never guess an outgoing/destructive verb or a negation."""
    from jarvis.runtime.command_repair161 import _RISK_HEADS
    return frozenset({norm(w) for w in _RISK_HEADS} | _NEGATION)


def known(word: str, code: str) -> bool:
    return word in vocabulary(code) or is_real_word(word, code)


@lru_cache(maxsize=1024)
def word_options(word: str, code: str, allow_known: bool = False, allow_short: bool = False) -> tuple[tuple[float, str], ...]:
    """Nearest command words for one typed word, cheapest first."""
    if code not in LANGUAGES or len(word) < 2 or len(word) > 24 or word in vocabulary(code):
        return ()
    if word in _NEGATION or is_stable(word, code) or (not allow_known and is_real_word(word, code)):
        return ()
    if len(word) <= 3 and not allow_short:
        return ()  # a two-letter word is too ambiguous before routing has failed
    forbidden, limit = _forbidden_targets(code), budget(word)
    keys = {word} | _deletes(word)
    if len(word) >= 7:
        keys |= {x for key in _deletes(word) for x in _deletes(key)}
    found: dict[str, float] = {}
    risky = 9.0
    for key in keys:
        for target in _near_words(key, code):
            if target in found:
                continue
            score = distance(word, target, code, limit)
            if score > limit:
                continue
            if target in forbidden or target in _NEGATION:
                risky = min(risky, score)
            else:
                found[target] = score
    options = sorted((score, target) for target, score in found.items())
    # «выклюич» is closest to the protected «выключи»: never fall back to the
    # next nearest word («включи»), which may mean the opposite.
    if options and risky <= options[0][0] + 0.3:
        return ()
    # Fused words: "откройпапку" -> "открой папку" (each half must be a word).
    if not options and len(word) >= 6:
        # A report with a lost space stays a report: «включивмузыку», «открылпапку».
        if any(is_stable(word[:cut], code) and word[cut:] in vocabulary(code)
               for cut in range(3, len(word) - 1)):
            return ()
        for cut in range(2, len(word) - 1):
            left, right = word[:cut], word[cut:]
            left_options = ((0.0, left),) if left in vocabulary(code) else word_options(left, code) if len(left) >= 4 else ()
            right_options = ((0.0, right),) if right in vocabulary(code) else word_options(right, code) if len(right) >= 4 else ()
            if left_options and right_options:
                score = 0.3 + left_options[0][0] + right_options[0][0]
                if score <= limit and left_options[0][1] not in forbidden:
                    options.append((score, left_options[0][1] + ' ' + right_options[0][1]))
        options.sort()
    if not options:
        return ()
    best = options[0][0]
    return tuple(row for row in options if row[0] <= best + 0.25)[:3]


@lru_cache(maxsize=7)
def _chat_words(code: str) -> frozenset[str]:
    """Words of the already loaded social atlas («привет», «спасибо»)."""
    try:
        from jarvis.runtime.social_atlas import catalog
        return frozenset(word for phrase in catalog(code) for word in _TOKEN.findall(phrase))
    except Exception:
        return frozenset()


def _layout_word(word: str, code: str) -> bool:
    return known(word, code) or word in _chat_words(code)


# 186: a file name the user said («отчёт.docx») is literal data; its words are never
# «repaired» into other words (отчёт → ответ changed which file was read).
_FILE_NAME186 = re.compile(r"[^\s«»\"'()]+\.[A-Za-z0-9]{1,5}(?![\w.])")


def _editable_tokens(text: str, code: str):
    end = _DATA_START.search(text)
    stop = end.start() if end else len(text)
    names = [m.span() for m in _FILE_NAME186.finditer(text)]
    return [m for m in _TOKEN.finditer(text[:stop])
            if not any(start < m.end() and m.start() < finish for start, finish in names)]


def fix_layout(text: str, code: str) -> str:
    """Undo a wrong keyboard layout for tokens that become known words."""
    original = str(text or '')
    value = original.rstrip()
    # The ЙЦУКЕН full stop/comma key types "/" and "?" on QWERTY.
    ending = value[len(value.rstrip('/?')):] if code == 'ru' else ''
    value = value[:len(value) - len(ending)]
    if (code not in {'ru', 'en'} or not value.strip() or len(value) > 400
            or _DATA_START.search(value.replace('"', '').replace('`', ''))):
        return original
    # Real words of one language practically never type another language's
    # word in the wrong layout, so the foreign catalogue is not loaded here.
    pattern, table = (_LATIN_KEYS, _EN_TO_RU) if code == 'ru' else (_CYRILLIC, _RU_TO_EN)
    spans = [m for m in pattern.finditer(value) if any(ch.isalpha() for ch in m.group(0))]
    if not spans:
        return original
    decisions = []
    for match in spans:
        raw = match.group(0)
        core = raw.rstrip(",.;:!?") if code == 'en' else raw
        flipped = norm(core.translate(table)).strip(" ,.")
        words = _TOKEN.findall(flipped)
        # A lone letter («нажми E») is a key or a letter name, not a word typed in
        # the wrong layout; it flips only when the whole message was mistyped.
        good = (len(core.strip(",.;:!?")) > 1 and bool(words) and not _layout_word(norm(core), code)
                and all(_layout_word(w, code) or word_options(w, code) for w in words)
                and any(_layout_word(w, code) for w in words))
        decisions.append((match, core, good))
    flips = sum(ok for _, _, ok in decisions)
    if not flips:
        return original
    # The whole message was typed in the wrong layout: payload words flip too.
    whole = flips * 2 >= len(decisions)
    pieces, cursor = [], 0
    for match, core, ok in decisions:
        if ok or (whole and not _layout_word(norm(core), code)):
            pieces.append(value[cursor:match.start()])
            pieces.append(core.translate(table) + match.group(0)[len(core):])
            cursor = match.end()
    pieces.append(value[cursor:])
    return ''.join(pieces) + ending.translate(str.maketrans('/?', '.,'))


def _phrase_candidates(text: str, code: str, *, allow_short: bool, strict: bool = False,
                       max_changes: int = 2) -> list[Repair]:
    tokens = _editable_tokens(text, code)
    if not tokens or len(tokens) > 40:
        return []
    if any(norm(m.group(0)) in _NEGATION for m in _TOKEN.finditer(text)):
        return []
    slots = []
    for position, match in enumerate(tokens):
        word = norm(match.group(0))
        options = word_options(word, code, True, allow_short)
        if options:
            slots.append((position, match, options))
    if not slots:
        return []
    if strict:
        # Last chance: every other word of the selected language must already be
        # understood, so a half-repaired phrase never runs as some other command.
        # Words in another script (names such as Discord in Russian) stay payload.
        fixed = {position for position, _, _ in slots}
        for position, match in enumerate(tokens):
            word = norm(match.group(0))
            native = bool(_CYRILLIC.fullmatch(word)) == (code == 'ru')
            if position not in fixed and native and not _layout_word(word, code):
                return []
    rows: list[Repair] = []

    unknown = {position for position, match, _ in slots if not is_real_word(norm(match.group(0)), code)}

    def build(chosen):
        # Every unknown word that looks like a typo must be explained, otherwise
        # «следующий тек» would become a song title instead of a question.
        if unknown - {position for position, _, _ in chosen}:
            return None
        pieces, cursor, cost, changes, real = [], 0, 0.0, [], False
        for _, match, (score, target) in chosen:
            real = real or is_real_word(norm(match.group(0)), code)
            original = match.group(0)
            replacement = target
            if original[:1].isupper():
                replacement = replacement[:1].upper() + replacement[1:]
            pieces.append(text[cursor:match.start()])
            pieces.append(replacement)
            cursor = match.end()
            cost += score
            changes.append((original, replacement))
        pieces.append(text[cursor:])
        return Repair(''.join(pieces), round(cost, 3), tuple(changes), real)

    for position, match, options in slots:
        for option in options:
            rows.append(build([(position, match, option)]))
    if max_changes >= 2:
        for a in range(len(slots)):
            for b in range(a + 1, min(len(slots), a + 6)):
                rows.append(build([(slots[a][0], slots[a][1], slots[a][2][0]), (slots[b][0], slots[b][1], slots[b][2][0])]))
    rows = [row for row in rows if row is not None]
    rows.sort(key=lambda row: (row.cost, len(row.changes)))
    unique, seen = [], set()
    for row in rows:
        if row.text not in seen:
            seen.add(row.text)
            unique.append(row)
    return unique[:24]


def _choose(options: Iterable[Repair], check: Callable[[str], Any], limit: int) -> tuple[Repair | None, tuple[str, ...]]:
    """Cheapest valid repair; distinct equally cheap interpretations are returned to ask."""
    found: list[tuple[Repair, str]] = []
    tried = 0
    for row in options:
        if found and row.cost > found[0][0].cost + 0.15:
            break
        if tried >= limit:
            break
        tried += 1
        identity = check(row.text)
        if identity is not None:
            found.append((row, repr(identity)))
    if not found:
        return None, ()
    identities = {identity for _, identity in found}
    if len(identities) == 1:
        return found[0][0], ()
    return None, tuple(dict.fromkeys(row.text for row, _ in found))[:4]


def repair(text: str, code: str, proposal: Callable[[str], Any]) -> tuple[str, tuple[str, ...]]:
    """Soft repair before routing; a typed parse of the result must confirm it."""
    if code not in LANGUAGES or not str(text or '').strip() or len(text) > 400:
        return text, ()
    from jarvis.runtime.command_repair160 import descriptive_statement
    if descriptive_statement(text, code):
        return text, ()
    options = _phrase_candidates(text, code, allow_short=False)
    # A real word («пагода», «пауз») changes only when the whole result is made
    # of command words («какая погода»): no payload can be rewritten that way.
    grammar = vocabulary(code)
    options = [row for row in options if not row.real_words
               or all(norm(word) in grammar for word in _TOKEN.findall(row.text))]
    if not options or proposal(text) is not None:
        return text, ()
    chosen, alternatives = _choose(options, proposal, limit=8)
    return (chosen.text if chosen else text), alternatives


def rescue(text: str, code: str, check: Callable[[str], Any]) -> Repair | None:
    """Last-chance repair instead of "not understood": real words may change too."""
    if code not in LANGUAGES or not str(text or '').strip() or len(text) > 400:
        return None
    from jarvis.runtime.command_repair160 import descriptive_statement
    if descriptive_statement(text, code):
        return None
    options = _phrase_candidates(text, code, allow_short=True, strict=True)
    if not options:
        return None
    chosen, alternatives = _choose(options, check, limit=10)
    return None if alternatives else chosen
