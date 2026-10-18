# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Cross-script phonetic similarity.

The problem this solves is the whole point of section 9: Whisper writes what it
hears in the *user's* language, while game-specific names may be written in the *game's*
language.  «брак» and ``Brakk`` are the same sound and share no characters.

Both sides are folded into one consonant-weighted skeleton and compared.  This
is intentionally cruder than a real G2P model — it must run in microseconds for
every candidate on every turn, and the resolver's scope rules (section 11), not
this function, are what keep it safe.

Reuses :mod:`jarvis.gaming.normalize` for the base folding so the adaptive
layer and the game binder agree on what a word *is*.
"""

import difflib
import re
from functools import lru_cache

from jarvis.gaming.normalize import latin_fold, norm

__all__ = [
    "phonetic_key",
    "phonetic_similarity",
    "string_similarity",
    "combined_similarity",
]

# Latin digraphs and spelling variants that carry one sound. Applied after
# latin_fold, so both a Cyrillic and a Latin source arrive in the same shape.
_CLUSTERS = (
    ("sch", "s"), ("sh", "s"), ("zh", "j"), ("ch", "c"), ("ck", "k"),
    ("kh", "h"), ("ph", "f"), ("th", "t"), ("gh", "g"), ("qu", "kv"),
    ("ts", "c"), ("tz", "c"), ("ya", "a"), ("yu", "u"), ("yo", "o"),
    ("ye", "e"), ("ee", "i"), ("oo", "u"), ("ou", "u"), ("ai", "e"),
)

# Consonants that ASR routinely swaps between languages.
_EQUIVALENT = {
    "b": "b", "p": "b",
    "v": "v", "w": "v", "f": "v",
    "g": "g", "k": "g", "q": "g", "x": "gs",
    "d": "d", "t": "d",
    "z": "z", "s": "z", "c": "z",
    "j": "j",
    "m": "m", "n": "m",
    "l": "l", "r": "l",
    "h": "h",
    "y": "i",
}

_VOWELS = set("aeiou")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@lru_cache(maxsize=65536)
def phonetic_key(value: str) -> str:
    """A compact, script-independent sound skeleton.

    ``"брак"`` and ``"Brakk"`` both reduce to the same key; ``"брат"`` does not.
    """
    text = latin_fold(value)
    if not text:
        return ""
    text = _NON_ALNUM.sub("", text)
    if not text:
        return ""
    for src, dst in _CLUSTERS:
        text = text.replace(src, dst)
    out: list[str] = []
    for index, ch in enumerate(text):
        if ch.isdigit():
            out.append(ch)
            continue
        if ch in _VOWELS:
            # Keep only the leading vowel; interior vowels are the least
            # reliable part of a cross-language transcription.
            if index == 0:
                out.append(ch)
            continue
        out.append(_EQUIVALENT.get(ch, ch))
    skeleton = "".join(out)
    # Collapse runs: "brakk" -> "blak" -> "blak"; "brak" -> "blak".
    skeleton = re.sub(r"(.)\1+", r"\1", skeleton)
    return skeleton


@lru_cache(maxsize=32768)
def phonetic_similarity(left: str, right: str) -> float:
    """Similarity of the sound skeletons, in ``[0, 1]``."""
    a, b = phonetic_key(left), phonetic_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    # A shared first sound is disproportionately informative for proper names.
    if a[0] == b[0]:
        ratio = min(1.0, ratio + 0.06)
    length_penalty = 1.0 - min(0.35, abs(len(a) - len(b)) / max(len(a), len(b), 1) * 0.5)
    return max(0.0, min(1.0, ratio * length_penalty))


@lru_cache(maxsize=32768)
def string_similarity(left: str, right: str) -> float:
    a, b = latin_fold(left), latin_fold(right)
    if not a or not b:
        a, b = norm(left), norm(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def combined_similarity(left: str, right: str) -> float:
    """Blended score used for ranking ASR candidates.

    Phonetics dominates because that is the failure being repaired, but pure
    string similarity keeps a correctly-transcribed Latin term at the top.
    """
    phon = phonetic_similarity(left, right)
    text = string_similarity(left, right)
    return max(text, 0.72 * phon + 0.28 * text)
