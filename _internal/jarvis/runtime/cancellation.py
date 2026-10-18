# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import threading
from contextlib import contextmanager
import time
from dataclasses import dataclass


class OperationCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class CancellationSnapshot:
    generation: int
    cancelled: bool
    reason: str
    changed_at: float


class CancellationManager:
    """Process-local cooperative cancellation with generation isolation.

    Older builds used one ``threading.Event`` for the whole process.  That had a
    subtle but severe race: Stop set the event, then the next command immediately
    called ``reset()`` and cleared it while the *old* worker was still running.
    A blocked/slow old turn could then continue, keep Agent._lock forever and make
    every later command appear dead.

    Each worker thread now keeps the generation it started in. ``cancel()`` marks
    every generation that already exists as cancelled, while a later ``reset()``
    creates a strictly newer generation.  Clearing the current command therefore
    never resurrects an older cancelled worker.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._generation = 0
        self._cancel_through_generation = -1
        self._reason = ""
        self._changed_at = time.monotonic()
        self._local = threading.local()

    def reset(self) -> int:
        """Start/bind a fresh cancellation generation for the calling thread."""
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._changed_at = time.monotonic()
            # Do not erase ``_cancel_through_generation``: older worker threads
            # must stay cancelled after a new command begins.
            self._local.generation = generation
            self._local.explicit_bound = False
            return generation

    def bind(self, generation: int) -> int:
        """Bind the calling thread to an existing generation (tests/helpers)."""
        value = max(0, int(generation))
        self._local.generation = value
        self._local.explicit_bound = True
        return value

    @contextmanager
    def bound_generation(self, generation: int):
        """Temporarily bind a queued job without contaminating a pooled thread."""
        missing = object()
        old_generation = getattr(self._local, "generation", missing)
        old_explicit = getattr(self._local, "explicit_bound", missing)
        self.bind(generation)
        try:
            yield
        finally:
            for name, old in (("generation", old_generation), ("explicit_bound", old_explicit)):
                if old is missing:
                    if hasattr(self._local, name):
                        delattr(self._local, name)
                else:
                    setattr(self._local, name, old)

    def is_explicitly_bound(self) -> bool:
        return bool(getattr(self._local, "explicit_bound", False))

    def current_generation(self) -> int:
        value = getattr(self._local, "generation", None)
        if value is not None:
            try:
                return int(value)
            except Exception:
                pass
        with self._lock:
            return int(self._generation)

    def cancel(self, reason: str = "Пользователь отменил операцию") -> None:
        with self._lock:
            # Cancel all work that was already alive at this exact point. Future
            # generations created by reset() remain usable immediately.
            self._cancel_through_generation = max(
                int(self._cancel_through_generation), int(self._generation)
            )
            self._cancel_count188 = int(getattr(self, "_cancel_count188", 0)) + 1
            self._reason = str(reason or "Пользователь отменил операцию")
            self._changed_at = time.monotonic()

    def cancel_count(self) -> int:
        """How many cancel() calls happened so far (HIST-031: «cancelled since X»)."""
        with self._lock:
            return int(getattr(self, "_cancel_count188", 0))

    def cancelled_since(self, count: int) -> bool:
        """True when a cancel() after ``count`` covers the calling thread's generation."""
        return self.cancel_count() > int(count) and self.is_cancelled()

    def is_cancelled(self) -> bool:
        generation = self.current_generation()
        with self._lock:
            return generation <= int(self._cancel_through_generation)

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            snap = self.snapshot()
            raise OperationCancelled(snap.reason or "Операция отменена")

    def snapshot(self) -> CancellationSnapshot:
        generation = self.current_generation()
        with self._lock:
            cancelled = generation <= int(self._cancel_through_generation)
            return CancellationSnapshot(
                generation=generation,
                cancelled=cancelled,
                reason=self._reason if cancelled else "",
                changed_at=self._changed_at,
            )


_GLOBAL = CancellationManager()


def cancellation_manager() -> CancellationManager:
    return _GLOBAL
