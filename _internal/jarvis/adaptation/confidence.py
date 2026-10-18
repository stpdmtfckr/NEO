# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Confidence model for learned rules (spec section 8).

Three things have to be true at once:

* a rule the user confirmed out loud is **stable** — it must not evaporate
  because it was unused for a fortnight;
* a rule NEO guessed for itself is **fragile** — one contradiction should be
  enough to stop applying it;
* nothing is ever *certain*, so a rule can always be overridden by a stronger
  signal later.

The estimator is a Beta posterior over (positive, negative) confirmations, with
the prior weighted by the strongest evidence the rule has ever received, then
multiplied by a recency factor.  Explicit user confirmations additionally raise a
floor so decay cannot retire them.
"""

import math
import time
from dataclasses import dataclass

from .events import FeedbackStrength, RuleState

__all__ = ["ConfidenceInputs", "ConfidenceModel", "DEFAULT_MODEL"]

_DAY = 86400.0


@dataclass(slots=True)
class ConfidenceInputs:
    """Everything the model is allowed to look at."""

    positive_count: int = 0
    negative_count: int = 0
    strength: FeedbackStrength = FeedbackStrength.WEAK
    age_seconds: float = 0.0
    idle_seconds: float = 0.0
    use_count: int = 0
    # Signal quality at capture time. All optional; missing means "unknown",
    # which is treated as neutral rather than as evidence.
    asr_confidence: float | None = None
    phonetic_similarity: float | None = None
    context_match: float | None = None
    intent_match: float | None = None
    game_match: bool | None = None


class ConfidenceModel:
    def __init__(
        self,
        *,
        half_life_days: float = 45.0,
        weak_floor: float = 0.0,
        medium_floor: float = 0.25,
        strong_floor: float = 0.55,
        promote_at: float = 0.72,
        demote_at: float = 0.30,
        retire_at: float = 0.12,
        min_positive_for_promotion: int = 2,
    ) -> None:
        self.half_life_days = max(1.0, float(half_life_days))
        self.floors = {
            FeedbackStrength.WEAK: max(0.0, float(weak_floor)),
            FeedbackStrength.MEDIUM: max(0.0, float(medium_floor)),
            FeedbackStrength.STRONG: max(0.0, float(strong_floor)),
        }
        self.promote_at = float(promote_at)
        self.demote_at = float(demote_at)
        self.retire_at = float(retire_at)
        self.min_positive_for_promotion = max(1, int(min_positive_for_promotion))

    # -- core ---------------------------------------------------------------- #

    def score(self, inputs: ConfidenceInputs) -> float:
        """Return a confidence in ``[0, 1]``."""
        strength = FeedbackStrength.parse(inputs.strength)

        # Prior strength: a strong signal starts the rule much closer to belief
        # than an inference NEO made on its own.
        prior_positive = {FeedbackStrength.WEAK: 0.4,
                          FeedbackStrength.MEDIUM: 1.0,
                          FeedbackStrength.STRONG: 2.4}[strength]
        prior_negative = {FeedbackStrength.WEAK: 2.2,
                          FeedbackStrength.MEDIUM: 1.4,
                          FeedbackStrength.STRONG: 0.8}[strength]

        pos = max(0, int(inputs.positive_count)) + prior_positive
        neg = max(0, int(inputs.negative_count)) + prior_negative
        base = pos / (pos + neg)

        base *= self._quality_multiplier(inputs)
        base *= self._recency(inputs.idle_seconds)

        floor = self.floors.get(strength, 0.0)
        if int(inputs.negative_count) > 0:
            # A contradiction always costs something, even for an explicit rule:
            # the user is allowed to change their mind.
            floor *= 0.5 ** min(3, int(inputs.negative_count))
        return max(0.0, min(1.0, max(base, floor)))

    def _quality_multiplier(self, inputs: ConfidenceInputs) -> float:
        """Fold optional per-signal quality into a bounded multiplier.

        Every component is optional. Unknown means neutral — never a penalty,
        because a missing measurement is not evidence of a bad match.
        """
        multiplier = 1.0
        components: list[float] = []
        for value, weight in (
            (inputs.asr_confidence, 0.10),
            (inputs.phonetic_similarity, 0.16),
            (inputs.context_match, 0.14),
            (inputs.intent_match, 0.10),
        ):
            if value is None:
                continue
            value = max(0.0, min(1.0, float(value)))
            components.append(1.0 + weight * (value - 0.5) * 2.0)
        for component in components:
            multiplier *= component
        if inputs.game_match is False:
            multiplier *= 0.85
        # Repeated successful use is itself mild evidence.
        if inputs.use_count > 0:
            multiplier *= 1.0 + min(0.08, 0.01 * math.log1p(inputs.use_count))
        return max(0.55, min(1.30, multiplier))

    def _recency(self, idle_seconds: float) -> float:
        """Exponential decay on time since last use (spec section 8).

        Unused, unconfirmed rules must lose influence.  Confirmed rules are
        protected by their floor, not by an exemption from decay, so the numbers
        stay comparable between rule families.
        """
        idle_days = max(0.0, float(idle_seconds)) / _DAY
        if idle_days <= 0.0:
            return 1.0
        return 0.5 ** (idle_days / self.half_life_days)

    # -- lifecycle ------------------------------------------------------------ #

    def next_state(
        self,
        current_state: str,
        confidence: float,
        *,
        positive_count: int,
        negative_count: int,
        strength: FeedbackStrength,
    ) -> str:
        """Decide the state a rule should now be in.

        Section 17: a new variant is observed in ``shadow`` first and is only
        promoted once it has earned it.  Section 68: a corrected rule must stop
        being applied as before.  A ``blocked`` rule is terminal for automatic
        transitions — only an explicit user confirmation reopens it.
        """
        strength = FeedbackStrength.parse(strength)
        if current_state == RuleState.BLOCKED:
            if strength is FeedbackStrength.STRONG and positive_count > negative_count:
                return RuleState.SHADOW
            return RuleState.BLOCKED

        if negative_count >= 2 and negative_count > positive_count:
            return RuleState.BLOCKED
        if confidence <= self.retire_at and positive_count == 0:
            return RuleState.RETIRED
        if confidence < self.demote_at:
            return RuleState.SHADOW

        if confidence >= self.promote_at:
            # An explicit correction is trusted on the first pass; anything
            # weaker needs repetition before it may act on a live turn.
            if strength is FeedbackStrength.STRONG:
                return RuleState.ACTIVE
            if positive_count >= self.min_positive_for_promotion:
                return RuleState.ACTIVE
        return current_state if current_state in RuleState.ALL else RuleState.SHADOW

    def applies(self, state: str, confidence: float, *, minimum: float = 0.0) -> bool:
        """Only ``active`` rules above threshold may change a live turn."""
        return state == RuleState.ACTIVE and confidence >= max(self.demote_at, minimum)


DEFAULT_MODEL = ConfidenceModel()


def now() -> float:
    return time.time()
