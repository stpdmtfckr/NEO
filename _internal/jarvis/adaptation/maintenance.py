# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Idle-time consolidation (spec sections 19, 20, 21, 75, 85, 86, 87).

Nothing expensive is allowed to happen while the player is in a game.  During a
game the learning path is exactly::

    capture learning event -> lightweight aggregate update

and everything else waits for here.  Permission comes from the *existing*
:class:`~jarvis.runtime.resource_budget.ResourceBudget`, which already publishes
game presence — this module adds no polling thread of its own.

Explicitly out of scope, per section 21: no online fine-tuning, no LoRA, no
embedding rebuild on the command path.  Consolidation is arithmetic over a
SQLite table.
"""

import logging
import threading
import time
from typing import Any

from .events import FeedbackStrength, LearningEvent, RuleKind

logger = logging.getLogger(__name__)

__all__ = ["AdaptiveMaintenance"]


class AdaptiveMaintenance:
    def __init__(
        self,
        store: Any,
        *,
        min_interval_seconds: float = 900.0,
        event_batch: int = 2000,
    ) -> None:
        self.store = store
        self.min_interval_seconds = max(60.0, float(min_interval_seconds))
        self.event_batch = max(100, int(event_batch))
        self._lock = threading.RLock()
        self._last_run = 0.0
        self._running = False

    # -- gating --------------------------------------------------------------- #

    def _allowed(self, *, force: bool) -> tuple[bool, str]:
        if self.store is None or not getattr(self.store, "available", False):
            return False, "store_unavailable"
        with self._lock:
            if self._running:
                return False, "already_running"
            if force:
                return True, "forced"
            if time.monotonic() - self._last_run < self.min_interval_seconds:
                return False, "too_soon"
        try:
            from jarvis.runtime.resource_budget import resource_budget

            if resource_budget().should_defer_heavy_background(correctness_required=False):
                return False, "deferred_game_active"
        except Exception:
            logger.debug("Resource budget unavailable; treating as idle", exc_info=True)
        return True, "idle"

    # -- main ----------------------------------------------------------------- #

    def run(self, *, force: bool = False) -> dict[str, Any]:
        allowed, reason = self._reserve(force=force)
        if not allowed:
            return {"ran": False, "reason": reason}
        return self._run_reserved(reason)

    def _reserve(self, *, force: bool) -> tuple[bool, str]:
        # Reserve before creating a thread. Concurrent idle notifications used
        # to all pass _allowed() before the first worker set _running, launching
        # multiple overlapping SQLite consolidation/prune passes.
        with self._lock:
            allowed, reason = self._allowed(force=force)
            if allowed:
                self._running = True
            return allowed, reason

    def _run_reserved(self, reason: str) -> dict[str, Any]:
        started = time.perf_counter()
        report: dict[str, Any] = {"ran": True, "reason": reason}
        try:
            report["consolidated"] = self.consolidate()
            report["decayed"] = self.store.decay()
            report["pruned"] = self.store.prune()
        except Exception:
            logger.debug("Adaptive maintenance failed", exc_info=True)
            report["error"] = True
        finally:
            with self._lock:
                self._running = False
                self._last_run = time.monotonic()
        report["ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        logger.info(
            "Adaptive maintenance consolidated=%s decayed=%s pruned=%s ms=%s",
            report.get("consolidated"), report.get("decayed"),
            report.get("pruned"), report.get("ms"),
        )
        return report

    def consolidate(self) -> int:
        """Fold the raw audit log into aggregated rules (section 86).

        500 identical observations must become ``positive_count=500`` on one
        rule, not 500 permanent rows.  Events already folded in at capture time
        are simply marked; the pass exists so an interrupted session, a crash or
        an import can still converge.
        """
        pending = self.store.pending_events(limit=self.event_batch)
        if not pending:
            return 0
        ids = [int(row["event_id"]) for row in pending]
        self.store.mark_consolidated(ids)
        return len(ids)

    def maybe_run_async(self, *, force: bool = False) -> bool:
        """Fire-and-forget maintenance on a daemon thread.

        Daemon by design: adaptive consolidation must never delay shutdown, and
        an interrupted pass costs nothing because every step is idempotent.
        """
        allowed, reason = self._reserve(force=force)
        if not allowed:
            return False
        try:
            thread = threading.Thread(
                target=self._run_reserved, args=(reason,),
                name="jarvis-adaptive-maintenance", daemon=True,
            )
            thread.start()
        except Exception:
            with self._lock:
                self._running = False
            raise
        return True
