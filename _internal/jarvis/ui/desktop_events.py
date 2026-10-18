"""Bounded GUI requests. Producers never import Qt or touch widgets."""
from __future__ import annotations

from queue import Empty, Full, Queue
from typing import Any
from threading import Lock

_EVENTS: Queue[dict[str, Any]] = Queue(maxsize=128)
_COALESCED: set[str] = set()
_LOCK = Lock()


def request_desktop(action: str, **payload: Any) -> bool:
    coalesce = action in {"shelf.refresh", "services.reconfigure", "settings.apply"}
    with _LOCK:
        if coalesce and action in _COALESCED:
            return True
        try:
            _EVENTS.put_nowait({"action": str(action), **payload})
            if coalesce:
                _COALESCED.add(action)
            return True
        except Full:
            return False


def drain_desktop_events(limit: int = 32) -> list[dict[str, Any]]:
    result = []
    for _ in range(max(1, min(128, limit))):
        try:
            item = _EVENTS.get_nowait()
            with _LOCK:
                _COALESCED.discard(item["action"])
            result.append(item)
        except Empty:
            break
    return result
