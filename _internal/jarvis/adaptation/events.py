# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Learning event vocabulary.

The single most dangerous failure mode for an adaptive assistant is described in
section 7 of the specification::

    ASR misheard
      -> NEO invented a correction
      -> NEO stored it as truth
      -> NEO now repeats the mistake forever

The whole package is built to make that sequence impossible, and it starts here:
every signal carries an explicit :class:`FeedbackStrength`.  Only ``STRONG`` (and
repeated ``MEDIUM``) evidence can promote a rule out of ``shadow`` state.
"""

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

__all__ = [
    "FeedbackStrength",
    "RuleKind",
    "RuleState",
    "LearningEvent",
]


class FeedbackStrength(IntEnum):
    """How much a signal is allowed to change stored belief.

    ``WEAK`` is what NEO concluded on its own.  It may create a *shadow*
    candidate and nothing more.  ``MEDIUM`` is behavioural evidence from the
    user (an immediate rephrase).  ``STRONG`` is the user saying it outright
    ("нет, я сказал Brakk").
    """

    WEAK = 1        # NEO's own inference from context
    MEDIUM = 2      # user immediately repeated/rephrased with a changed token
    STRONG = 3      # user explicitly corrected NEO in words

    @property
    def label(self) -> str:
        return {1: "weak", 2: "medium", 3: "strong"}[int(self)]

    @classmethod
    def parse(cls, value: Any, default: "FeedbackStrength" = None) -> "FeedbackStrength":
        default = default or cls.WEAK
        if isinstance(value, FeedbackStrength):
            return value
        try:
            return cls(int(value))
        except Exception:
            pass
        text = str(value or "").strip().casefold()
        return {"weak": cls.WEAK, "medium": cls.MEDIUM, "strong": cls.STRONG}.get(text, default)


class RuleKind:
    """Discriminator for the unified ``rules`` table.

    Section 6 of the spec lists nine conceptual entities.  They share one
    physical table (identical metadata columns, identical confidence/decay
    lifecycle) and are exposed under the spec's names as SQL views, so both the
    requested schema and a maintainable one exist at the same time.
    """

    SPEECH_CORRECTION = "speech_correction"
    INTENT_EXAMPLE = "intent_example"
    WAKE_CANDIDATE = "wake_candidate"
    WAKE_NEGATIVE = "wake_negative"
    ROUTING_CORRECTION = "routing_correction"
    USER_VOCABULARY = "user_vocabulary"

    ALL = (
        SPEECH_CORRECTION, INTENT_EXAMPLE, WAKE_CANDIDATE,
        WAKE_NEGATIVE, ROUTING_CORRECTION, USER_VOCABULARY,
    )


class RuleState:
    """Lifecycle of a learned rule.

    ``shadow`` is observed but never applied (section 17).  ``active`` is
    applied.  ``blocked`` was demoted for causing false positives and is never
    re-promoted automatically.  ``retired`` decayed below usefulness.
    """

    SHADOW = "shadow"
    ACTIVE = "active"
    BLOCKED = "blocked"
    RETIRED = "retired"

    ALL = (SHADOW, ACTIVE, BLOCKED, RETIRED)


@dataclass(slots=True)
class LearningEvent:
    """One observation worth remembering.

    Events are the audit log.  They are aggregated into ``rules`` and then
    pruned (sections 85-86): 500 identical observations must become one rule
    with ``positive_count=500``, not 500 permanent rows.
    """

    kind: str
    key: str = ""
    value: str = ""
    scope: str = "global"
    game_id: str = ""
    language: str = ""
    strength: FeedbackStrength = FeedbackStrength.WEAK
    positive: bool = True
    source: str = "runtime"
    reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "value": self.value,
            "scope": self.scope,
            "game_id": self.game_id,
            "language": self.language,
            "strength": self.strength.label,
            "positive": self.positive,
            "source": self.source,
            "reason": self.reason,
            "payload": dict(self.payload),
        }


def game_scope(game_id: str) -> str:
    """Canonical scope string for a game-bound rule.

    Scope is what stops «у них счастливый брак» from becoming «Brakk» in normal
    conversation (sections 11 and 65): a rule learned inside ``game:warframe``
    is only ever consulted while that game owns the turn.
    """
    game_id = str(game_id or "").strip().casefold()
    return f"game:{game_id}" if game_id else "global"
