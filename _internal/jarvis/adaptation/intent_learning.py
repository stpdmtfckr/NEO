# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Adaptive intent examples (spec section 14).

The adaptive intent learner is independent from live game web search. It stays
small, fast and explainable: when deterministic routing misses and the user
corrects the result, that phrasing is remembered for next time.

Matching is stem-set overlap, not a classifier: it has to run in microseconds on
the hot path, and a wrong adaptive intent must be cheap to explain and undo.
"""

import logging
from typing import Any, Sequence

from .events import FeedbackStrength, LearningEvent, RuleKind, RuleState, game_scope
from jarvis.gaming.normalize import stems

logger = logging.getLogger(__name__)

__all__ = ["IntentLearner", "IntentSuggestion"]

_MIN_OVERLAP = 0.62


class IntentSuggestion:
    __slots__ = ("intent", "score", "rule_id", "example")

    def __init__(self, intent: str, score: float, rule_id: int, example: str) -> None:
        self.intent = intent
        self.score = score
        self.rule_id = rule_id
        self.example = example

    def as_dict(self) -> dict[str, Any]:
        return {"intent": self.intent, "score": round(self.score, 3),
                "rule_id": self.rule_id, "example": self.example}


class IntentLearner:
    def __init__(self, store: Any) -> None:
        self.store = store

    def _usable(self) -> bool:
        return self.store is not None and getattr(self.store, "available", False)

    @staticmethod
    def _key(text: str) -> str:
        """Order-independent stem signature; «где выбить X» == «X где выбить»."""
        return " ".join(sorted(set(stems(text))))

    def learn(
        self,
        phrase: str,
        intent: str,
        *,
        game_id: str = "",
        language: str = "",
        strength: FeedbackStrength = FeedbackStrength.MEDIUM,
        positive: bool = True,
    ) -> bool:
        if not self._usable():
            return False
        key = self._key(phrase)
        intent = str(intent or "").strip()
        if not key or not intent:
            return False
        rule = self.store.observe(
            LearningEvent(
                kind=RuleKind.INTENT_EXAMPLE, key=key, value=intent,
                scope=game_scope(game_id) if game_id else "global",
                game_id=str(game_id or ""), language=language,
                strength=strength, positive=positive,
                source="user_correction" if strength >= FeedbackStrength.MEDIUM else "runtime",
                reason="intent_example", payload={"phrase": str(phrase or "")[:160]},
            )
        )
        if rule is None:
            return False
        logger.info(
            "Intent learned example intent=%s scope=%s state=%s confidence=%.3f phrase=%r",
            intent, rule.scope, rule.state, rule.confidence, str(phrase or "")[:60],
        )
        return True

    def suggest(
        self, phrase: str, *, game_id: str = "", language: str = "", limit: int = 3,
    ) -> list[IntentSuggestion]:
        """Adaptive intent guesses, ordered best-first. Advisory only.

        The caller must treat these as a *hint* that loses to any deterministic
        match — an adaptive intent is a memory of one user's habit, not a rule
        about the language.
        """
        if not self._usable():
            return []
        query = set(stems(phrase))
        if not query:
            return []
        scopes = [game_scope(game_id)] if game_id else ["global"]
        out: list[IntentSuggestion] = []
        for rule in self.store.rules(
            RuleKind.INTENT_EXAMPLE, scopes=scopes, states=(RuleState.ACTIVE,),
            language=language, limit=200,
        ):
            stored = set(rule.key.split())
            if not stored:
                continue
            overlap = len(query & stored) / max(len(query | stored), 1)
            if overlap < _MIN_OVERLAP:
                continue
            out.append(IntentSuggestion(
                rule.value, overlap * rule.confidence, rule.rule_id,
                str(rule.payload.get("phrase") or ""),
            ))
        out.sort(key=lambda s: s.score, reverse=True)
        return out[: max(1, int(limit))]
