# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class DeveloperEvent:
    at: float
    stage: str
    value: str
    data: dict[str, Any]
    duration_ms: float | None = None


class DeveloperEventBus:
    """Thread-safe pipeline event stream consumed by the optional Developer window."""

    def __init__(self, max_items: int = 500) -> None:
        self._items: deque[DeveloperEvent] = deque(maxlen=max(50, int(max_items)))
        self._lock = threading.RLock()
        self._sequence = 0

    def emit(self, stage: str, value: Any = "", *, data: dict[str, Any] | None = None, duration_ms: float | None = None) -> int:
        event = DeveloperEvent(
            at=time.time(), stage=str(stage or "event"), value=str(value or ""),
            data=dict(data or {}), duration_ms=None if duration_ms is None else float(duration_ms),
        )
        with self._lock:
            self._sequence += 1
            self._items.append(event)
            return self._sequence

    def snapshot(self, limit: int = 100) -> tuple[int, list[dict[str, Any]]]:
        with self._lock:
            rows = list(self._items)[-max(1, min(500, int(limit))):]
            return self._sequence, [asdict(x) for x in rows]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._sequence += 1


_GLOBAL = DeveloperEventBus()


def developer_bus() -> DeveloperEventBus:
    return _GLOBAL
