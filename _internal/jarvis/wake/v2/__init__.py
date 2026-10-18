# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Wake detection V2 — phonetic model instead of a hand-maintained alias list."""

from .matcher import WakeConfig, WakeDecision, WakeMatcher, migrate_alias_list
from .phonetics import CONFUSABLE_WORDS, fold, phonetic_score, skeleton, syllables

__all__ = [
    "WakeMatcher", "WakeConfig", "WakeDecision", "migrate_alias_list",
    "phonetic_score", "fold", "skeleton", "syllables", "CONFUSABLE_WORDS",
]
