# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Speech / ASR correction learning (spec section 9).

Thin, deliberate wrapper over the store: the interesting decisions (how strong a
signal is, how confidence evolves) live in :mod:`~jarvis.adaptation.feedback`
and :mod:`~jarvis.adaptation.confidence`.  This module only decides *what* gets
written and in which scope.
"""

import logging
from typing import Any

from .confidence import ConfidenceInputs
from .events import FeedbackStrength, LearningEvent, RuleKind, game_scope
from .feedback import CorrectionSignal
from .phonetics import phonetic_similarity
from jarvis.gaming.normalize import norm

logger = logging.getLogger(__name__)

__all__ = ["SpeechLearner"]


class SpeechLearner:
    def __init__(self, store: Any) -> None:
        self.store = store

    def _usable(self) -> bool:
        return self.store is not None and getattr(self.store, "available", False)

    def learn(
        self,
        signal: CorrectionSignal,
        *,
        game_id: str = "",
        language: str = "",
        scoped_to_game: bool = True,
    ) -> bool:
        """Fold a user correction into belief.

        ``scoped_to_game`` is what keeps «брак»->``Brakk`` from leaking into
        ordinary conversation (section 11).  A correction learned while a game
        was active stays inside ``game:<id>``; only a correction made with no
        game in play becomes global.
        """
        if not self._usable() or not signal.corrected:
            return False
        key = norm(signal.replaced)
        if not key:
            logger.debug("Speech correction without an identifiable source token; stored as vocabulary only")
            return self.learn_vocabulary(signal.corrected, game_id=game_id, language=language)
        if key == norm(signal.corrected):
            return False
        scope = game_scope(game_id) if (game_id and scoped_to_game) else "global"
        rule = self.store.observe(
            LearningEvent(
                kind=RuleKind.SPEECH_CORRECTION, key=key, value=signal.corrected,
                scope=scope, game_id=game_id if scoped_to_game else "", language=language,
                strength=signal.strength, positive=True,
                source="user_correction", reason=signal.reason,
                payload={"phrase": signal.payload.get("phrase", "")},
            ),
            quality=ConfidenceInputs(
                phonetic_similarity=phonetic_similarity(signal.replaced, signal.corrected),
                context_match=signal.confidence or None,
                game_match=bool(game_id),
            ),
        )
        if rule is None:
            return False
        logger.info(
            "AdaptiveSpeech correction learned '%s'->'%s' scope=%s strength=%s state=%s confidence=%.3f",
            key, signal.corrected, scope, signal.strength.label, rule.state, rule.confidence,
        )
        self.learn_vocabulary(signal.corrected, game_id=game_id, language=language)
        return True

    def penalise(self, heard: str, replacement: str, *, game_id: str = "", language: str = "") -> bool:
        """Negative feedback: this correction was wrong (section 68).

        The rule keeps existing — history is useful — but its confidence drops
        and, after a second contradiction, it is blocked from applying at all.
        """
        if not self._usable():
            return False
        scope = game_scope(game_id) if game_id else "global"
        rule = self.store.observe(
            LearningEvent(
                kind=RuleKind.SPEECH_CORRECTION, key=norm(heard), value=replacement,
                scope=scope, game_id=game_id, language=language,
                strength=FeedbackStrength.STRONG, positive=False,
                source="user_correction", reason="user_rejected_correction",
            )
        )
        if rule is None:
            return False
        logger.info(
            "AdaptiveSpeech correction rejected '%s'->'%s' state=%s confidence=%.3f negatives=%d",
            heard, replacement, rule.state, rule.confidence, rule.negative_count,
        )
        return True

    def learn_vocabulary(self, term: str, *, game_id: str = "", language: str = "") -> bool:
        """Remember that this word exists in the user's world at all.

        Vocabulary is weaker than a correction: it biases recognition and
        ranking, but never rewrites a transcript on its own.
        """
        if not self._usable():
            return False
        term = str(term or "").strip()
        if len(norm(term)) < 2:
            return False
        rule = self.store.observe(
            LearningEvent(
                kind=RuleKind.USER_VOCABULARY, key=norm(term), value=term,
                scope=game_scope(game_id) if game_id else "global",
                game_id=game_id, language=language,
                strength=FeedbackStrength.MEDIUM, positive=True,
                source="user_correction", reason="vocabulary",
            )
        )
        return rule is not None

    def corrections(self, *, game_id: str = "", language: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if not self._usable():
            return []
        scopes = [game_scope(game_id)] if game_id else ["global"]
        return [r.as_dict() for r in self.store.rules(
            RuleKind.SPEECH_CORRECTION, scopes=scopes, states=(), language=language, limit=limit,
        )]
