# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Deterministic text normalization shared by game routing and adaptation.

The module is dependency-free and cached. It normalizes spoken game names, ASR
variants and short dialogue references; it does not index or retrieve gameplay
facts.
"""

import re
import unicodedata
from functools import lru_cache

__all__ = [
    "norm",
    "latin_fold",
    "stem",
    "tokens",
    "stems",
    "ngrams",
    "split_identifier",
    "pretty_key",
]

# --------------------------------------------------------------------------- #
# Base folding
# --------------------------------------------------------------------------- #

_CYR2LAT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ы": "y", "э": "e", "ю": "yu", "я": "ya", "ь": "", "ъ": "",
        "і": "i", "ї": "i", "є": "e", "ґ": "g",
    }
)

_PUNCT_RE = re.compile(r"[^\w\s\-]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


@lru_cache(maxsize=65536)
def norm(value: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Keeps the alphabet."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


@lru_cache(maxsize=65536)
def latin_fold(value: str) -> str:
    """Fold any script into a comparable ASCII-ish form.

    This is what lets a Cyrillic question term match a Latin schema field.
    """
    text = norm(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.translate(_CYR2LAT)
    text = re.sub(r"[^a-z0-9\s\-]+", "", text)
    return _WS_RE.sub(" ", text).strip()


# --------------------------------------------------------------------------- #
# Light stemming
# --------------------------------------------------------------------------- #

# Russian inflectional tails, longest-first. Deliberately conservative: we only
# want "урона"/"уроном"/"урон" and "оружия"/"оружие" to converge, not a full
# morphological analyser.
_RU_SUFFIXES = (
    # infinitives and reflexives first -- longest match wins
    "иваться", "ываться", "оваться", "аться", "иться", "еться", "уться",
    "ываю", "иваю", "ивать", "ывать", "овать",
    "ться", "ется", "ится", "ются", "атся", "ятся",
    "ить", "ать", "ять", "еть", "ыть", "уть", "оть", "ти", "чь",
    # nominal / adjectival
    "иями", "ями", "ами", "иях", "ях", "ах", "ов", "ев", "ий", "ая", "яя",
    "ое", "ее", "ые", "ие", "ым", "им", "ом", "ем", "ей", "ой", "ую", "юю",
    "ешь", "ишь", "ете", "ите", "ет", "ит", "ут", "ют", "ат", "ят",
    # past tense keeps its thematic vowel, so strip it with the "л"
    "ились", "ались", "ился", "алась", "илась",
    "или", "али", "ели", "ыли", "ули",
    "ила", "ала", "ела", "ыла", "ула",
    "ило", "ало", "ело", "ыло", "уло",
    "ил", "ал", "ел", "ыл", "ул",
    "ла", "ло", "ли", "л", "ь", "я", "ю", "е", "у", "ы", "и", "а", "о",
)

_EN_SUFFIXES = ("ingly", "edly", "ings", "ing", "ies", "ied", "es", "ed", "s")


@lru_cache(maxsize=65536)
def stem(word: str) -> str:
    """Very small RU+EN stemmer operating on the latin-folded form."""
    w = latin_fold(word)
    if not w:
        return ""
    raw = norm(word)
    # Russian side: strip from the Cyrillic form first, then fold.
    if re.search(r"[а-я]", raw):
        for suf in _RU_SUFFIXES:
            if len(raw) - len(suf) >= 3 and raw.endswith(suf):
                raw = raw[: -len(suf)]
                break
        w = latin_fold(raw)
    else:
        for suf in _EN_SUFFIXES:
            if len(w) - len(suf) >= 3 and w.endswith(suf):
                w = w[: -len(suf)]
                break
    # Collapse doubled consonants; ASR and source spellings disagree on these.
    w = re.sub(r"(.)\1+", r"\1", w)
    return w


_ID_SPLIT_RE = re.compile(r"[_\-.:/]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Za-z])(?=[0-9])")


def split_identifier(value: str) -> list[str]:
    """Split camelCase / snake_case / dotted source keys into words.

    ``"weapon.dropLocation_v2"`` -> ``["weapon", "drop", "Location", "v", "2"]``
    """
    parts = _ID_SPLIT_RE.split(str(value or ""))
    return [p for p in parts if p]


def pretty_key(value: str) -> str:
    words = split_identifier(value)
    if not words:
        return str(value or "").strip()
    return " ".join(w.lower() for w in words)


# --------------------------------------------------------------------------- #
# Tokenisation
# --------------------------------------------------------------------------- #

_STOP = {
    # ru
    "и", "в", "во", "на", "с", "со", "у", "к", "по", "за", "из", "от", "для",
    "что", "как", "где", "куда", "чем", "чему", "это", "этот", "эта", "мне",
    "мой", "моя", "ли", "же", "бы", "а", "но", "или", "не", "ни", "то",
    "какой", "какая", "какие", "какое", "сколько", "можно", "нужно", "надо",
    "скажи", "расскажи", "покажи", "дай", "есть",
    # en
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "from", "with",
    "and", "or", "is", "are", "was", "were", "be", "do", "does", "did",
    "what", "which", "where", "how", "why", "when", "who", "can", "should",
    "tell", "show", "give", "me", "my", "it", "its", "this", "that",
}


def tokens(text: str) -> list[str]:
    return [t for t in norm(text).split() if t and t not in _STOP]


def stems(text: str) -> list[str]:
    out: list[str] = []
    for t in tokens(text):
        s = stem(t)
        if s and s not in out:
            out.append(s)
    return out


def ngrams(words: list[str], max_n: int = 4) -> list[tuple[int, int, str]]:
    """Return ``(start, end, phrase)`` for every n-gram, longest first.

    Longest-first ordering matters: a two-word entity name must be consumed
    before its individual words are offered to the field matcher.
    """
    out: list[tuple[int, int, str]] = []
    n = len(words)
    for size in range(min(max_n, n), 0, -1):
        for i in range(0, n - size + 1):
            out.append((i, i + size, " ".join(words[i : i + size])))
    return out
