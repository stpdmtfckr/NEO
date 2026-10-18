# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Routing correction learning (spec section 15).

«Нет, я спрашивал про игру» / «нет, это команда Windows».  These are the two
signals that tell NEO its *domain* decision was wrong, not its transcription.

Section 15 is explicit that one weak event must not become an absolute rule, so
routing corrections are held to the same shadow/promote lifecycle as everything
else — and they are advisory at the call site: they nudge domain selection, they
never override a deterministic domain owner.
"""

import logging
from typing import Any

from .events import FeedbackStrength, LearningEvent, RuleKind, RuleState, game_scope
from jarvis.gaming.normalize import stems

logger = logging.getLogger(__name__)

__all__ = ["RoutingLearner"]

_MIN_OVERLAP = 0.66

# Words the user actually uses to name a domain, mapped to NEO's internal names.
# Data, not dispatch: nothing here executes, it only labels a correction.
_DOMAIN_WORDS = {
    "game_web": ("игру", "игра", "игре", "игровой", "game", "варфрейм", "warframe",
                  "дарктайд", "darktide", "тарков", "tarkov", "майнкрафт", "minecraft"),
    "system": ("windows", "виндовс", "винду", "систему", "система", "командой", "команда"),
    "media": ("музыку", "музыка", "плеер", "громкость", "media", "music"),
    "weather": ("погоду", "погода", "weather"),
    "discord": ("дискорд", "discord"),
    "telegram": ("телеграм", "telegram"),
}


def classify_domain(text: str) -> str:
    """Best-effort domain label from the user's own words. ``""`` when unclear."""
    lowered = " " + str(text or "").casefold().replace("ё", "е") + " "
    for domain, words in _DOMAIN_WORDS.items():
        for word in words:
            if f" {word} " in lowered or lowered.strip().endswith(f" {word}"):
                return domain
    return ""


class RoutingLearner:
    def __init__(self, store: Any) -> None:
        self.store = store

    def _usable(self) -> bool:
        return self.store is not None and getattr(self.store, "available", False)

    @staticmethod
    def _key(text: str) -> str:
        return " ".join(sorted(set(stems(text))))

    def learn(
        self,
        phrase: str,
        domain: str,
        *,
        wrong_domain: str = "",
        game_id: str = "",
        language: str = "",
        strength: FeedbackStrength = FeedbackStrength.STRONG,
    ) -> bool:
        if not self._usable():
            return False
        key = self._key(phrase)
        domain = str(domain or "").strip()
        if not key or not domain:
            return False
        rule = self.store.observe(
            LearningEvent(
                kind=RuleKind.ROUTING_CORRECTION, key=key, value=domain,
                scope=game_scope(game_id) if game_id else "global",
                game_id=str(game_id or ""), language=language,
                strength=strength, positive=True, source="user_correction",
                reason="routing_corrected",
                payload={"phrase": str(phrase or "")[:160], "wrong_domain": wrong_domain},
            )
        )
        if rule is None:
            return False
        logger.info(
            "Routing correction learned '%s' -> domain=%s (was %s) state=%s confidence=%.3f",
            str(phrase or "")[:60], domain, wrong_domain or "-", rule.state, rule.confidence,
        )
        return True

    def suggest(self, phrase: str, *, game_id: str = "", language: str = "") -> tuple[str, float]:
        """Advisory domain hint. Never authoritative (section 90)."""
        if not self._usable():
            return "", 0.0
        query = set(stems(phrase))
        if not query:
            return "", 0.0
        scopes = [game_scope(game_id), "global"] if game_id else ["global"]
        best_domain, best_score, best_id = "", 0.0, 0
        for rule in self.store.rules(
            RuleKind.ROUTING_CORRECTION, scopes=scopes, states=(RuleState.ACTIVE,),
            language=language, limit=200,
        ):
            stored = set(rule.key.split())
            if not stored:
                continue
            overlap = len(query & stored) / max(len(query | stored), 1)
            if overlap < _MIN_OVERLAP:
                continue
            score = overlap * rule.confidence
            if score > best_score:
                best_domain, best_score, best_id = rule.value, score, rule.rule_id
        if best_id:
            self.store.mark_used(best_id)
        return best_domain, best_score
