# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""AdaptiveIntelligence — the single object the rest of NEO talks to.

Everything else in this package is internal.  ``agent.py``, ``voice.py`` and the
wake worker only ever see this facade, so the adaptive layer can be disabled,
degraded or replaced without touching a call site.

Every method is failure-tolerant by construction: if the store is unavailable
the facade still answers, it just answers "nothing learned" (section 80).
"""

import logging
import threading
import time
from typing import Any, Sequence

from .events import FeedbackStrength, LearningEvent, RuleKind, RuleState, game_scope
from .feedback import (
    CorrectionSignal, detect_correction, detect_repeat_correction,
    detect_routing_correction, looks_like_denial,
)
from .intent_learning import IntentLearner
from .maintenance import AdaptiveMaintenance
from .resolver import ContextualSpeechResolver, ResolvedText
from .routing_learning import RoutingLearner, classify_domain
from .speech_learning import SpeechLearner
from .store import AdaptiveStore
from .wake_learning import WakeLearner

logger = logging.getLogger(__name__)

__all__ = ["AdaptiveIntelligence", "adaptive_intelligence", "install_adaptive_intelligence"]


class _Turn:
    """What the previous turn looked like, for MEDIUM feedback detection."""

    __slots__ = ("raw", "normalized", "game_id", "intent", "domain", "at", "subjects")

    def __init__(self) -> None:
        self.raw = ""
        self.normalized = ""
        self.game_id = ""
        self.intent = ""
        self.domain = ""
        self.subjects: list[str] = []
        self.at = 0.0


class AdaptiveIntelligence:
    def __init__(self, store: AdaptiveStore, *, config: Any = None, language: str = "ru") -> None:
        self.store = store
        self.config = config
        self.language = str(language or "ru")
        self.resolver = ContextualSpeechResolver(store)
        self.speech = SpeechLearner(store)
        self.intents = IntentLearner(store)
        self.routing = RoutingLearner(store)
        self.wake = WakeLearner(store)
        self.maintenance = AdaptiveMaintenance(store)
        self._lock = threading.RLock()
        self._previous = _Turn()
        self._apply_settings()

    # -- construction --------------------------------------------------------- #

    @classmethod
    def build(cls, config: Any, *, language: str = "ru") -> "AdaptiveIntelligence | None":
        try:
            store = AdaptiveStore.for_config(config)
        except Exception:
            logger.exception("Adaptive store construction failed; adaptation disabled")
            return None
        service = cls(store, config=config, language=language)
        logger.info(
            "Adaptive Intelligence %s (store=%s rules=%d)",
            "active" if store.available else f"degraded: {store.degraded_reason}",
            store.path, store.rule_count(),
        )
        return service

    def _apply_settings(self) -> None:
        if self.config is None:
            return
        def _get(key: str, default: Any) -> Any:
            try:
                return self.config.get(key, default)
            except Exception:
                return default
        try:
            self.resolver.accept_threshold = float(_get("adaptation.speech.accept_threshold", 0.86))
            self.resolver.shadow_threshold = float(_get("adaptation.speech.shadow_threshold", 0.72))
            self.resolver.max_corrections = int(_get("adaptation.speech.max_corrections", 2))
            self.maintenance.min_interval_seconds = float(
                _get("adaptation.maintenance.min_interval_seconds", 900)
            )
        except Exception:
            logger.debug("Adaptive settings parse failed; using defaults", exc_info=True)
        try:
            self.wake.set_phrase(str(_get("wake_word.phrase", "NEO")))
        except Exception:
            pass

    # -- the hot path --------------------------------------------------------- #

    def resolve(
        self,
        text: str,
        *,
        active_game: str = "",
        explicit_game: str = "",
        language: str = "",
    ) -> ResolvedText:
        """ASR text -> normalized query. Never raises; never loses the raw text."""
        try:
            return self.resolver.resolve(
                text, active_game=active_game, explicit_game=explicit_game,
                language=language or self.language,
            )
        except Exception:
            logger.debug("Adaptive resolution failed; using raw transcript", exc_info=True)
            return ResolvedText(raw=str(text or ""), text=str(text or ""), reason="error")

    def note_turn(
        self,
        raw: str,
        normalized: str,
        *,
        game_id: str = "",
        intent: str = "",
        domain: str = "",
        subjects: Sequence[str] = (),
    ) -> None:
        with self._lock:
            turn = _Turn()
            turn.raw = str(raw or "")
            turn.normalized = str(normalized or "")
            turn.game_id = str(game_id or "")
            turn.intent = str(intent or "")
            turn.domain = str(domain or "")
            turn.subjects = [str(s) for s in subjects or ()]
            turn.at = time.time()
            self._previous = turn

    def observe_user_feedback(
        self, text: str, *, active_game: str = "", language: str = "",
    ) -> list[CorrectionSignal]:
        """Detect and learn from an explicit or behavioural correction.

        Called once per turn *before* routing.  Returns what was learned so the
        caller can log it and, if it wants, act on it in the same turn.
        """
        signals: list[CorrectionSignal] = []
        if not str(text or "").strip():
            return signals
        with self._lock:
            previous = self._previous
        language = language or self.language
        game_id = str(active_game or previous.game_id or "")

        try:
            explicit = detect_correction(text, previous_text=previous.normalized or previous.raw)
            if explicit is not None:
                self.speech.learn(
                    explicit, game_id=game_id, language=language,
                    scoped_to_game=bool(game_id),
                )
                signals.append(explicit)

            routed = detect_routing_correction(text)
            if routed is not None:
                domain = classify_domain(routed.corrected) or classify_domain(text)
                if domain and (previous.normalized or previous.raw):
                    self.routing.learn(
                        previous.normalized or previous.raw, domain,
                        wrong_domain=previous.domain, game_id=game_id, language=language,
                    )
                    if previous.intent:
                        self.intents.learn(
                            previous.normalized or previous.raw, previous.intent,
                            game_id=game_id, language=language,
                            strength=FeedbackStrength.STRONG, positive=False,
                        )
                    signals.append(routed)

            if not signals and previous.raw:
                gap = max(0.0, time.time() - previous.at) if previous.at else 0.0
                repeat = detect_repeat_correction(
                    text, previous.normalized or previous.raw, gap_seconds=gap,
                )
                if repeat is not None:
                    self.speech.learn(
                        repeat, game_id=game_id, language=language,
                        scoped_to_game=bool(game_id),
                    )
                    signals.append(repeat)

            # A bare denial right after a correction NEO applied itself means the
            # correction was wrong (section 68). Penalise it, do not guess a new one.
            if not signals and looks_like_denial(text) and previous.normalized != previous.raw:
                self._penalise_last_correction(previous, game_id=game_id, language=language)
        except Exception:
            logger.debug("Feedback observation failed", exc_info=True)
        return signals

    def _penalise_last_correction(self, previous: _Turn, *, game_id: str, language: str) -> None:
        raw_words = previous.raw.split()
        new_words = previous.normalized.split()
        if len(raw_words) != len(new_words):
            return
        for was, now in zip(raw_words, new_words):
            if was != now:
                self.speech.penalise(was, now, game_id=game_id, language=language)

    # -- game-side hooks ------------------------------------------------------ #

    def learn_intent(self, phrase: str, intent: str, *, game_id: str = "", language: str = "") -> bool:
        return self.intents.learn(
            phrase, intent, game_id=game_id, language=language or self.language,
        )

    # -- maintenance / control ------------------------------------------------ #

    def on_idle(self, *, force: bool = False) -> bool:
        return self.maintenance.maybe_run_async(force=force)

    def reset(self, *, kind: str = "") -> int:
        """Section 98: wipe learning without touching any other user setting."""
        removed = self.store.reset(kind=kind)
        with self._lock:
            self._previous = _Turn()
        return removed

    def snapshot(self) -> dict[str, Any]:
        data = self.store.snapshot()
        data["thresholds"] = {
            "accept": self.resolver.accept_threshold,
            "shadow": self.resolver.shadow_threshold,
        }
        return data

    def learned_summary(self, *, limit: int = 40) -> list[dict[str, Any]]:
        """"What has NEO learned" — the data behind the settings view (section 97)."""
        out: list[dict[str, Any]] = []
        if not getattr(self.store, "available", False):
            return out
        for kind in RuleKind.ALL:
            for rule in self.store.rules(kind, states=(RuleState.ACTIVE, RuleState.SHADOW), limit=limit):
                out.append(rule.as_dict())
        out.sort(key=lambda r: (-r["confidence"], r["kind"]))
        return out[:limit]

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:
            logger.debug("Adaptive store close failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Process-wide accessor
# --------------------------------------------------------------------------- #

_LOCK = threading.RLock()
_INSTANCE: AdaptiveIntelligence | None = None


def install_adaptive_intelligence(config: Any, *, language: str = "ru") -> AdaptiveIntelligence | None:
    """Build the singleton. Safe to call more than once."""
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is not None:
            return _INSTANCE
        try:
            enabled = bool(config.get("adaptation.enabled", True))
        except Exception:
            enabled = True
        if not enabled:
            logger.info("Adaptive Intelligence disabled by settings")
            return None
        _INSTANCE = AdaptiveIntelligence.build(config, language=language)
        return _INSTANCE


def adaptive_intelligence() -> AdaptiveIntelligence | None:
    """The installed instance, or ``None`` when adaptation is unavailable."""
    with _LOCK:
        return _INSTANCE
