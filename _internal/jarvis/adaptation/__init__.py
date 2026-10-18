# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Adaptive Intelligence — persistent, local-only personal adaptation.

This package is *strictly separate* from ``jarvis.runtime.context_memory``.

    jarvis.runtime.context_memory   -> the current conversation (volatile)
    jarvis.adaptation               -> what NEO has learned about this user
                                       (persistent, confidence-scored, decaying)

Design invariants, all of them load-bearing:

1. **Never a single point of failure.**  Every public entry point degrades to a
   no-op when the store is missing, corrupt or locked.  ``AdaptiveIntelligence``
   is an enhancement layer; NEO must start and answer without it.
2. **Never learn from its own guesses.**  A correction NEO inferred by itself is
   ``weak`` evidence and lands in ``shadow`` state.  Only user-confirmed signals
   promote a rule to ``active``.  See :mod:`jarvis.adaptation.confidence`.
3. **Local only.**  Nothing here touches the network.  Transcripts, vocabulary
   and corrections stay in one SQLite file under the user's data root.
4. **Resource aware.**  Heavy consolidation runs only through
   :mod:`jarvis.adaptation.maintenance`, which asks
   :func:`jarvis.runtime.resource_budget.resource_budget` for permission first.
"""

from .events import (
    FeedbackStrength,
    LearningEvent,
    RuleKind,
    RuleState,
)
from .confidence import ConfidenceModel, ConfidenceInputs
from .store import AdaptiveStore, AdaptiveRule, StoreUnavailable
from .resolver import ContextualSpeechResolver, ResolvedText, Correction
from .service import AdaptiveIntelligence, adaptive_intelligence, install_adaptive_intelligence

__all__ = [
    "FeedbackStrength",
    "LearningEvent",
    "RuleKind",
    "RuleState",
    "ConfidenceModel",
    "ConfidenceInputs",
    "AdaptiveStore",
    "AdaptiveRule",
    "StoreUnavailable",
    "ContextualSpeechResolver",
    "ResolvedText",
    "Correction",
    "AdaptiveIntelligence",
    "adaptive_intelligence",
    "install_adaptive_intelligence",
]
