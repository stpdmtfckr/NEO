"""A bounded, ordered best-effort lane for non-critical network feedback.

No retries of ambiguous remote mutations, no secret payload logging, no worker
until submission. The idle worker exits and can be restarted atomically.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import logging
import threading
import time
from typing import Callable, Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeferredItem:
    created: float
    payload: dict[str, Any]


class DeferredEffects:
    def __init__(self, send: Callable[[dict[str, Any]], None], *, capacity: int = 32,
                 max_age: float = 180, idle_seconds: float = 15, name: str = "NEO-Feedback"):
        self.send = send
        self.capacity = max(1, int(capacity))
        self.max_age = max(1.0, float(max_age))
        self.idle_seconds = max(0.1, float(idle_seconds))
        self.name = name
        self._condition = threading.Condition()
        self._items: deque[DeferredItem] = deque()
        self._worker: threading.Thread | None = None
        self._closed = False
        self.submitted = self.sent = self.failed = self.dropped = 0

    def submit(self, payload: dict[str, Any]) -> bool:
        with self._condition:
            if self._closed:
                return False
            if len(self._items) >= self.capacity:
                self._items.popleft()
                self.dropped += 1
            self._items.append(DeferredItem(time.monotonic(), dict(payload)))
            self.submitted += 1
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name=self.name, daemon=True)
                self._worker.start()
            self._condition.notify()
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._items and not self._closed:
                    self._condition.wait(self.idle_seconds)
                if self._closed or not self._items:
                    self._worker = None
                    return
                item = self._items.popleft()
            if time.monotonic() - item.created > self.max_age:
                self.dropped += 1
                continue
            try:
                self.send(item.payload)
                self.sent += 1
            except Exception as exc:
                # Boundary for optional third-party API failures. Never log
                # request bodies/URLs/authentication strings from exceptions.
                self.failed += 1
                logger.debug("%s deferred effect failed (%s)", self.name, type(exc).__name__)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self.dropped += len(self._items)
            self._items.clear()
            self._condition.notify_all()
