# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

r"""Multilingual phonetic model for wake-word matching.

The problem in the shipped build
--------------------------------
``default_settings.json`` carries **~530 hand-written wake aliases** -- every
observed Whisper misspelling of "Джарвис", each stored three times (bare, with
".", with "!").  Three consequences:

* Unbounded growth.  Every new ASR quirk needs a manual entry, and the list
  already contains items like ``Jeremy``, ``Janice``, ``Doris`` and ``Service``
  which are ordinary words -- guaranteed false accepts.
* No calibration.  ``fuzzy_threshold = 0.92`` is compared against a
  ``difflib`` ratio on raw text, so a single vowel change flips the decision.
* Punctuation triples the list for no acoustic reason at all.

The approach here
-----------------
Model the *sound* of one canonical phrase instead of enumerating spellings.
A wake candidate is scored on three independent axes -- consonant skeleton,
syllable shape, and edit distance over a folded form -- and only the combined
score is thresholded.  ~530 aliases collapse to one phrase plus a handful of
genuinely irregular exceptions.
"""

import re
import unicodedata
from functools import lru_cache

__all__ = ["skeleton", "syllables", "fold", "phonetic_score", "CONFUSABLE_WORDS"]

_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "o",
    "ж": "j", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "c", "ш": "s", "щ": "s", "ы": "i",
    "э": "e", "ю": "u", "я": "a", "ь": "", "ъ": "",
    "і": "i", "ї": "i", "є": "e", "ґ": "g",
    # Polish/Czech renderings Whisper produces for Slavic proper names
    "ż": "j", "ź": "z", "ą": "a", "ę": "e", "ł": "l", "ń": "n",
    "ó": "o", "ś": "s", "ć": "c", "š": "s", "č": "c", "ž": "j",
})

# Digraphs that represent one sound, applied before single-char folding.
_DIGRAPHS = (
    ("dzh", "j"), ("dzi", "j"), ("dz", "j"), ("dj", "j"), ("dż", "j"),
    ("zh", "j"), ("ch", "c"), ("sh", "s"), ("cz", "c"), ("sz", "s"),
    ("rz", "j"), ("kh", "h"), ("ts", "c"), ("ph", "f"),
)

_VOWELS = set("aeiou")

# Onsets that ASR routinely swaps for the /dʒ/ of "Jarvis".
_ONSET_EQUIV = {"j", "g", "z", "c", "s", "d", "y", "x"}


@lru_cache(maxsize=16384)
def fold(value: str) -> str:
    """Script-agnostic phonetic fold: one symbol per approximate phoneme."""
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.translate(_TRANSLIT)
    text = re.sub(r"[^a-z\s]+", "", text)
    for src, dst in _DIGRAPHS:
        text = text.replace(src, dst)
    # Voiced/voiceless pairs and common ASR substitutions collapse together.
    text = (text.replace("w", "v").replace("f", "v")
                .replace("q", "k").replace("x", "ks"))
    text = re.sub(r"(.)\1+", r"\1", text)
    return re.sub(r"\s+", "", text)


@lru_cache(maxsize=16384)
def skeleton(value: str) -> str:
    """Consonant skeleton: the most stable part across languages."""
    folded = fold(value)
    return "".join(ch for ch in folded if ch not in _VOWELS)


@lru_cache(maxsize=16384)
def syllables(value: str) -> int:
    """Approximate syllable count from vowel groups."""
    folded = fold(value)
    return len(re.findall(r"[aeiou]+", folded)) or (1 if folded else 0)


def _ratio(a: str, b: str) -> float:
    """Normalised Levenshtein similarity (iterative, two-row)."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ca != cb),
            ))
        previous = current
    return 1.0 - previous[-1] / max(len(a), len(b))


# Ordinary words that the shipped alias list wrongly accepts as wake words.
# They are rejected outright regardless of score.
CONFUSABLE_WORDS = frozenset({
    "service", "services", "servis", "jeremy", "jeremys", "janice", "doris",
    "jarl", "jazz", "jarhead", "harvest", "harvard", "garage", "gervais",
    "сервис", "джерси", "чарльз", "шарль", "джем", "жарь", "жарить",
    "гарнир", "царь", "жара", "жарко",
})


def phonetic_score(candidate: str, target: str) -> float:
    """Combined phonetic similarity in ``[0, 1]``.

    Three axes are blended so that no single ASR artefact can flip the
    decision on its own:

    ``skeleton`` (0.45)
        Consonants survive accent, alphabet and vowel reduction.
    ``fold`` (0.40)
        Full folded form; catches insertions/deletions the skeleton hides.
    ``syllables`` (0.15)
        Rejects same-consonant words of the wrong length
        ("Jarvis" vs "Jarvisovich").
    """
    cand_fold, target_fold = fold(candidate), fold(target)
    if not cand_fold or not target_fold:
        return 0.0
    if cand_fold in CONFUSABLE_WORDS or str(candidate).casefold().strip(" .!?,") in CONFUSABLE_WORDS:
        return 0.0

    cand_skel, target_skel = skeleton(candidate), skeleton(target)
    skel_score = _ratio(cand_skel, target_skel)
    fold_score = _ratio(cand_fold, target_fold)

    cand_syl, target_syl = syllables(candidate), syllables(target)
    syl_score = 1.0 - min(abs(cand_syl - target_syl) / max(target_syl, 1), 1.0)

    score = 0.45 * skel_score + 0.40 * fold_score + 0.15 * syl_score

    # A hard G is not the soft G/J affricate. Preserve the close, documented
    # ASR rendering Garvis/Гарвис, but do not let the broad onset class plus a
    # loose consonant skeleton accept unrelated Garrows/Garris. The older
    # lexical matcher already guarded this; the V2 fallback must do so too.
    # Exact user-configured exceptions are still resolved by WakeMatcher first.
    if (target_fold.startswith("j") and cand_fold.startswith("g")
            and cand_fold[1:2] not in {"e", "i", "y"} and fold_score < 0.75):
        score *= 0.80

    # A matching onset class is strong evidence; a mismatched one is a red flag.
    if cand_fold[:1] and target_fold[:1]:
        if cand_fold[0] == target_fold[0]:
            score = min(1.0, score + 0.05)
        elif not (cand_fold[0] in _ONSET_EQUIV and target_fold[0] in _ONSET_EQUIV):
            score *= 0.80
    return round(max(0.0, min(1.0, score)), 4)
