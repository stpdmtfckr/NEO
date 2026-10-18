# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Adaptive wake-word learning with shadow mode (spec sections 16-18).

The asymmetry here is the design.  A missed wake costs one repeat; a false wake
opens the microphone and can fire a command.  So:

* a new variant enters as ``shadow`` and is **observed only** (section 17);
* promotion needs repeated confirmation, never a single similar recognition
  (acceptance test 8);
* one confirmed false positive is heavily penalised, two block the candidate.

The existing ``jarvis.wake.v2.matcher`` keeps full authority over the live
decision.  This layer contributes *additional accepted aliases* once they have
earned it, and never lowers the matcher's threshold.
"""

import logging
from typing import Any

from .confidence import ConfidenceInputs
from .events import FeedbackStrength, LearningEvent, RuleKind, RuleState
from .phonetics import phonetic_similarity
from jarvis.gaming.normalize import norm

logger = logging.getLogger(__name__)

__all__ = ["WakeLearner"]

_SCOPE = "wake"
# A candidate must at least sound like the wake word. Without this floor, any
# noise transcript could enter shadow state and slowly accumulate confirmations.
_MIN_PHONETIC = 0.55


class WakeLearner:
    def __init__(self, store: Any, *, phrase: str = "джарвис") -> None:
        self.store = store
        self.phrase = str(phrase or "джарвис")

    def _usable(self) -> bool:
        return self.store is not None and getattr(self.store, "available", False)

    def set_phrase(self, phrase: str) -> None:
        if str(phrase or "").strip():
            self.phrase = str(phrase).strip()

    # -- observation ---------------------------------------------------------- #

    def observe_candidate(
        self, heard: str, *, accepted: bool, followed_by_command: bool = False,
    ) -> str:
        """Record one near-wake recognition.

        Returns the candidate's state, so the caller can log why a phrase did or
        did not open the microphone.
        """
        if not self._usable():
            return RuleState.SHADOW
        key = norm(heard)
        if len(key) < 3:
            return RuleState.SHADOW
        similarity = phonetic_similarity(heard, self.phrase)
        if similarity < _MIN_PHONETIC:
            return RuleState.SHADOW
        # A wake that produced a real command is the only positive evidence
        # worth anything; a bare accepted wake proves nothing (section 18).
        positive = bool(accepted and followed_by_command)
        rule = self.store.observe(
            LearningEvent(
                kind=RuleKind.WAKE_CANDIDATE, key=key, value=self.phrase,
                scope=_SCOPE, strength=FeedbackStrength.WEAK, positive=positive,
                source="wake_runtime",
                reason="wake_with_command" if positive else "wake_observed",
                payload={"similarity": round(similarity, 3)},
            ),
            quality=ConfidenceInputs(phonetic_similarity=similarity),
        )
        if rule is None:
            return RuleState.SHADOW
        if rule.state == RuleState.SHADOW:
            logger.debug("Wake candidate shadow '%s' confidence=%.3f similarity=%.3f",
                         key, rule.confidence, similarity)
        return rule.state

    def confirm(self, heard: str) -> bool:
        """«Я сказал Джарвис» — explicit confirmation (section 18)."""
        if not self._usable():
            return False
        key = norm(heard)
        if len(key) < 3:
            return False
        rule = self.store.observe(
            LearningEvent(
                kind=RuleKind.WAKE_CANDIDATE, key=key, value=self.phrase, scope=_SCOPE,
                strength=FeedbackStrength.STRONG, positive=True,
                source="user_confirmation", reason="user_said_wake",
            ),
            quality=ConfidenceInputs(phonetic_similarity=phonetic_similarity(heard, self.phrase)),
        )
        if rule is None:
            return False
        if rule.state == RuleState.ACTIVE:
            logger.info("Wake candidate promoted '%s' confidence=%.3f positives=%d",
                        key, rule.confidence, rule.positive_count)
        else:
            logger.info("Wake candidate shadow '%s' confidence=%.3f (awaiting confirmation)",
                        key, rule.confidence)
        return True

    def false_positive(self, heard: str) -> bool:
        """A wake fired but the user did not want it (section 18)."""
        if not self._usable():
            return False
        key = norm(heard)
        if not key:
            return False
        rule = self.store.observe(
            LearningEvent(
                kind=RuleKind.WAKE_CANDIDATE, key=key, value=self.phrase, scope=_SCOPE,
                strength=FeedbackStrength.STRONG, positive=False,
                source="wake_runtime", reason="false_positive",
            )
        )
        self.store.observe(
            LearningEvent(
                kind=RuleKind.WAKE_NEGATIVE, key=key, value=self.phrase, scope=_SCOPE,
                strength=FeedbackStrength.STRONG, positive=True,
                source="wake_runtime", reason="false_positive",
            )
        )
        if rule is not None and rule.state == RuleState.BLOCKED:
            logger.info("Wake candidate demoted '%s' state=blocked negatives=%d",
                        key, rule.negative_count)
        return True

    def false_negative(self, heard: str) -> bool:
        """The user repeated the wake word because nothing happened."""
        if not self._usable():
            return False
        key = norm(heard)
        if len(key) < 3:
            return False
        similarity = phonetic_similarity(heard, self.phrase)
        if similarity < _MIN_PHONETIC:
            return False
        self.store.observe(
            LearningEvent(
                kind=RuleKind.WAKE_CANDIDATE, key=key, value=self.phrase, scope=_SCOPE,
                strength=FeedbackStrength.MEDIUM, positive=True,
                source="wake_runtime", reason="missed_wake_repeated",
            ),
            quality=ConfidenceInputs(phonetic_similarity=similarity),
        )
        return True

    # -- consumption ---------------------------------------------------------- #

    def active_aliases(self, *, limit: int = 32) -> list[str]:
        """Promoted variants only. A shadow candidate never opens the mic."""
        if not self._usable():
            return []
        blocked = {
            r.key for r in self.store.rules(
                RuleKind.WAKE_NEGATIVE, scopes=[_SCOPE], states=(RuleState.ACTIVE, RuleState.SHADOW),
                limit=200,
            )
        }
        out: list[str] = []
        for rule in self.store.rules(
            RuleKind.WAKE_CANDIDATE, scopes=[_SCOPE], states=(RuleState.ACTIVE,), limit=limit * 2,
        ):
            if rule.key in blocked or rule.negative_count > 0:
                continue
            out.append(rule.key)
            if len(out) >= limit:
                break
        return out

    def candidates(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if not self._usable():
            return []
        return [r.as_dict() for r in self.store.rules(
            RuleKind.WAKE_CANDIDATE, scopes=[_SCOPE], states=(), limit=limit,
        )]
