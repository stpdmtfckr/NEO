"""Correct misheard words in a speech transcript: «превет» -> «привет».

Whisper sometimes writes a word the way it sounded («превет», «пачему»,
«helo»). A word is corrected only when all of this holds:

* it is not a known word of the selected language (frequent-word list, command
  words, real inflections, application and assistant names);
* a frequent word lies within a small sound/keyboard edit budget (unstressed
  vowel, voiced/voiceless consonant, doubled or dropped letter, swapped letters);
* that candidate is clearly better than any other one;
* the word is not a proper name inside the sentence, not in another script
  (Discord in Russian speech), not a number, and never becomes a risky verb.

Data for one language (~20k frequent words plus hashes of known words) loads
lazily or in a background warm-up, never on the UI thread; until it is ready the
transcript is returned unchanged. Memory for the active language is about 2 MB.
"""
from __future__ import annotations

from array import array
import base64
from bisect import bisect_left
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import sys
import threading
from typing import Iterable

from jarvis.runtime.natural_commands import LANGUAGES, norm

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_TOKEN = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", re.UNICODE)
_CYRILLIC = re.compile(r"[а-яё\-’']+")


def data_path(code: str) -> Path:
    return _ROOT / "config" / f"spelling165_{code}.json"


def voice_budget(word: str) -> float:
    size = len(word)
    return 0.0 if size < 4 else 0.5 if size <= 5 else 0.8 if size <= 8 else 1.2


@dataclass
class _Data:
    blob: str
    offsets: array
    index: array   # CRC32(word or word minus one letter) << 20 | rank
    known: array   # sorted CRC32 of known words

    def word(self, rank: int) -> str:
        return self.blob[self.offsets[rank]:self.offsets[rank + 1] - 1]


def _unpack(value: str, kind: str) -> array:
    table = array(kind)
    table.frombytes(base64.b64decode(value))
    if sys.byteorder == "big":
        table.byteswap()
    return table


def _load(code: str) -> _Data | None:
    from jarvis.runtime.command_typos165 import _deletes, word_hash
    try:
        raw = json.loads(data_path(code).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    words = str(raw.get("targets", "")).split("\n")
    # Command words of the selected language are always valid corrections, even
    # when they are too rare for the frequency list («активировать», «включай»).
    try:
        from jarvis.runtime.command_typos165 import vocabulary
        present = set(words)
        words += sorted(w for w in vocabulary(code) if len(w) >= 4 and w not in present and "-" not in w)
    except Exception:
        logger.debug("Command vocabulary unavailable for spelling targets", exc_info=True)
    blob = "\n".join(words) + "\n"
    offsets = array("I", [0])
    for word in words:
        offsets.append(offsets[-1] + len(word) + 1)
    entries = sorted({word_hash(key) << 20 | rank
                      for rank, word in enumerate(words[:0xFFFFF]) for key in {word} | _deletes(word)})
    return _Data(blob, offsets, array("Q", entries), _unpack(str(raw.get("known", "")), "I"))


_CACHE: dict[str, _Data | None] = {}
_LOADING: set[str] = set()
_LOCK = threading.Lock()


def warm_up(code: str) -> None:
    """Load one language in a background thread (call once after startup)."""
    with _LOCK:
        if code in _CACHE or code in _LOADING or code not in LANGUAGES:
            return
        _LOADING.add(code)

    def run() -> None:
        data = _load(code)
        try:
            # Build everything is_known() needs here, never on the caller's thread.
            from jarvis.runtime import command_typos165 as typos
            typos.vocabulary(code)
            typos._tables(code)
            typos._names(code)
        except Exception:
            logger.debug("Spelling warm-up could not build the command vocabulary", exc_info=True)
        with _LOCK:
            _CACHE[code] = data
            _LOADING.discard(code)

    threading.Thread(target=run, name="neo-spelling-warmup", daemon=True).start()


def _data(code: str, *, wait: bool) -> _Data | None:
    with _LOCK:
        if code in _CACHE:
            return _CACHE[code]
    if not wait:
        warm_up(code)
        return None
    data = _load(code)
    with _LOCK:
        _CACHE[code] = data
        _LOADING.discard(code)
    return data


def _member(table: array, key: int) -> bool:
    at = bisect_left(table, key)
    return at < len(table) and table[at] == key


def is_known(word: str, code: str, data: _Data | None = None) -> bool:
    from jarvis.runtime.command_typos165 import known, word_hash
    data = data or _data(code, wait=True)
    return bool(known(word, code) or (data and _member(data.known, word_hash(word))))


def suggest(word: str, code: str, data: _Data) -> str:
    """The clearly best frequent word for one unknown word, or ''."""
    from jarvis.runtime.command_typos165 import _deletes, _forbidden_targets, distance, word_hash
    limit = voice_budget(word)
    if not limit:
        return ""
    ranked: dict[int, float] = {}
    for key in {word} | _deletes(word):
        base = word_hash(key)
        at = bisect_left(data.index, base << 20)
        while at < len(data.index) and data.index[at] >> 20 == base:
            rank = data.index[at] & 0xFFFFF
            at += 1
            if rank in ranked:
                continue
            score = distance(word, data.word(rank), code, limit)
            if score <= limit:
                ranked[rank] = score
    if not ranked:
        return ""
    # Equal edit cost: the more frequent word wins by a small margin.
    rows = sorted((score + rank / len(data.offsets) * 0.1, rank) for rank, score in ranked.items())
    best_score, best_rank = rows[0]
    if len(rows) > 1 and rows[1][0] - best_score < 0.2:
        return ""
    target = data.word(best_rank)
    return "" if target in _forbidden_targets(code) else target


def correct_transcript(text: str, code: str, *, wait: bool = False) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Return the corrected transcript and the (heard, written) pairs."""
    value = str(text or "")
    if code not in LANGUAGES or not value.strip() or len(value) > 2000:
        return value, ()
    data = _data(code, wait=wait)
    if data is None:
        return value, ()
    changes: list[tuple[str, str]] = []
    pieces: list[str] = []
    cursor = 0
    for number, match in enumerate(_TOKEN.finditer(value)):
        original = match.group(0)
        word = norm(original)
        if len(word) < 4 or bool(_CYRILLIC.fullmatch(word)) != (code == "ru"):
            continue
        if any(mark in word for mark in "'’-"):
            continue  # elisions and compounds («c'est», «n'ai», «веб-плеер») are not dictionary words
        # A capital letter inside a sentence marks a name («Скриптонит», «Brakk»).
        before = value[:match.start()].rstrip()
        if original[:1].isupper() and before and before[-1] not in ".!?…":
            continue
        if is_known(word, code, data):
            continue
        target = suggest(word, code, data)
        if not target:
            continue
        if original[:1].isupper():
            target = target[:1].upper() + target[1:]
        if original.isupper() and len(original) > 1:
            target = target.upper()
        pieces.append(value[cursor:match.start()])
        pieces.append(target)
        cursor = match.end()
        changes.append((original, target))
    if not changes:
        return value, ()
    pieces.append(value[cursor:])
    return "".join(pieces), tuple(changes)


def memory_estimate(code: str) -> int:
    data = _CACHE.get(code)
    if not data:
        return 0
    return (sys.getsizeof(data.blob) + data.offsets.itemsize * len(data.offsets)
            + data.index.itemsize * len(data.index) + data.known.itemsize * len(data.known))


def languages_with_data() -> Iterable[str]:
    return [code for code in LANGUAGES if data_path(code).exists()]
