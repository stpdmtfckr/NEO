# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Contextual speech resolver for confirmed user-specific ASR corrections.

The resolver sits between ASR and routing.  It deliberately has no gameplay
vocabulary provider and no factual game corpus.  The active game is used only as
a *scope* for corrections the user previously confirmed while discussing that
game.  This keeps ordinary speech isolated and makes every substitution
persistent, explainable and reversible.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .events import RuleKind, RuleState, game_scope
from jarvis.gaming.normalize import norm, stems

logger = logging.getLogger(__name__)

__all__ = ["Correction", "ResolvedText", "ContextualSpeechResolver"]


_GAME_QUESTION_ROOTS = frozenset(stems(
    "где выбить как получить откуда падает что дропает кто дропает где фармить "
    "как скрафтить как создать что нужно для сколько урона какие характеристики "
    "против чего слаб какая слабость чем контрить где взять где достать "
    "как открыть как разблокировать что даёт для чего нужен куда идёт "
    "шанс выпадения дроп фарм крафт рецепт чертёж билд сборка статы оружие "
    "способности класс персонаж босс миссия квест ресурс предмет "
    "where to get how to get what drops who drops how to craft weapon abilities "
    "class character boss mission quest resource item weak against drop chance "
    "blueprint stats build unlock"
))

_NON_GAME_SHAPE = re.compile(
    r"\b(?:счастлив\w*|развод\w*|жен(?:а|ы|ой)|муж(?:а|ем)?|свадьб\w*|семь(?:я|и|ей)|"
    r"брак(?:а|у|ом|е)\s+(?:с|между)|заключ\w*\s+брак|"
    r"погод\w*|музык\w*|громкост\w*|напомн\w*|таймер|будильник)\b",
    re.I | re.U,
)


@dataclass(slots=True)
class Correction:
    """One reversible substitution with its justification."""

    original: str
    replacement: str
    confidence: float
    reason: str
    source: str
    scope: str = "global"
    span: tuple[int, int] = (0, 0)
    rule_id: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "original": self.original,
            "replacement": self.replacement,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "source": self.source,
            "scope": self.scope,
            "span": list(self.span),
            "rule_id": self.rule_id,
        }


@dataclass(slots=True)
class ResolvedText:
    raw: str
    text: str
    corrections: list[Correction] = field(default_factory=list)
    scope: str = "global"
    game_id: str = ""
    considered: bool = False
    reason: str = ""
    ms: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.corrections) and self.text != self.raw

    def revert(self) -> str:
        return self.raw

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "normalized": self.text,
            "changed": self.changed,
            "scope": self.scope,
            "game_id": self.game_id,
            "considered": self.considered,
            "reason": self.reason,
            "corrections": [c.as_dict() for c in self.corrections],
            "ms": round(self.ms, 3),
        }


class ContextualSpeechResolver:
    def __init__(
        self,
        store: Any = None,
        *,
        accept_threshold: float = 0.86,
        shadow_threshold: float = 0.72,
        max_corrections: int = 2,
    ) -> None:
        self.store = store
        # Kept as settings-compatible metadata. Only already-confirmed ACTIVE
        # rules are applied; the resolver does not generate phonetic candidates.
        self.accept_threshold = float(accept_threshold)
        self.shadow_threshold = float(shadow_threshold)
        self.max_corrections = max(1, int(max_corrections))

    @staticmethod
    def looks_like_game_question(text: str) -> bool:
        raw = str(text or "")
        if _NON_GAME_SHAPE.search(raw):
            return False
        question_stems = set(stems(raw))
        return bool(question_stems and question_stems & _GAME_QUESTION_ROOTS)

    def _scope_for(self, *, active_game: str, text: str, explicit_game: str = "") -> tuple[str, str, str]:
        game_id = str(explicit_game or active_game or "").casefold().strip()
        if not game_id:
            return "", "global", "no_active_game"
        if explicit_game:
            return game_id, game_scope(game_id), "explicit_game"
        if not self.looks_like_game_question(text):
            return "", game_scope(game_id), "not_game_shaped"
        return game_id, game_scope(game_id), "active_game_and_game_shaped"

    def _learned_corrections(self, scope: str, language: str) -> dict[str, tuple[str, float, int]]:
        if self.store is None or not getattr(self.store, "available", False):
            return {}
        out: dict[str, tuple[str, float, int]] = {}
        for rule in self.store.rules(
            RuleKind.SPEECH_CORRECTION,
            scopes=[scope],
            states=(RuleState.ACTIVE,),
            language=language,
            limit=400,
        ):
            key = norm(rule.key)
            if key and (key not in out or rule.confidence > out[key][1]):
                out[key] = (rule.value, rule.confidence, rule.rule_id)
        return out

    def resolve(
        self,
        text: str,
        *,
        active_game: str = "",
        explicit_game: str = "",
        language: str = "",
    ) -> ResolvedText:
        started = time.perf_counter()
        raw = str(text or "")
        result = ResolvedText(raw=raw, text=raw)
        if not raw.strip():
            return result

        game_id, scope, reason = self._scope_for(
            active_game=active_game, text=raw, explicit_game=explicit_game,
        )
        result.scope, result.game_id, result.reason = scope, game_id, reason

        # Confirmed global speech corrections are safe everywhere.
        working, applied = self._apply_learned(raw, [], scope="global", language=language)
        if game_id:
            result.considered = True
            # Game-scoped corrections are also user-confirmed; game identity only
            # chooses the scope and never supplies a candidate/fact itself.
            working, applied = self._apply_learned(
                working, applied, scope=scope, language=language,
            )

        result.text = working
        result.corrections = applied
        result.ms = (time.perf_counter() - started) * 1000.0
        if applied:
            self._log(result)
        return result

    def _apply_learned(
        self,
        text: str,
        applied: list[Correction],
        *,
        scope: str,
        language: str,
    ) -> tuple[str, list[Correction]]:
        rules = self._learned_corrections(scope, language)
        if not rules:
            return text, applied
        words = text.split()
        changed = False
        for index, word in enumerate(words):
            if len(applied) >= self.max_corrections:
                break
            bare = word.strip(".,!?;:«»\"'")
            hit = rules.get(norm(bare))
            if hit is None:
                continue
            replacement, confidence, rule_id = hit
            if norm(replacement) == norm(bare):
                continue
            applied.append(Correction(
                original=bare,
                replacement=replacement,
                confidence=confidence,
                reason="confirmed_user_correction",
                source="adaptive_store",
                scope=scope,
                span=(index, index + 1),
                rule_id=rule_id,
            ))
            words[index] = word.replace(bare, replacement)
            changed = True
            try:
                self.store.mark_used(rule_id)
            except Exception:
                logger.debug("mark_used failed", exc_info=True)
        return (" ".join(words) if changed else text), applied

    @staticmethod
    def _log(result: ResolvedText) -> None:
        for correction in result.corrections:
            logger.info(
                "AdaptiveSpeech confirmed correction '%s'->'%s' confidence=%.3f source=%s scope=%s",
                correction.original,
                correction.replacement,
                correction.confidence,
                correction.source,
                correction.scope,
            )
