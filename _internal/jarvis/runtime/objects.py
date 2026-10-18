# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class DesktopObject:
    kind: str
    identity: str
    label: str = ""
    app: str = ""
    hwnd: int = 0
    pid: int = 0
    monitor: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)


class DesktopObjectModel:
    """Small recency model used for pronouns such as 'его', 'там', 'это'."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._objects: dict[str, DesktopObject] = {}
        self._last_by_kind: dict[str, str] = {}
        self._last_any = ""

    def remember(self, obj: DesktopObject) -> None:
        with self._lock:
            obj.updated_at = time.time()
            self._objects[obj.identity] = obj
            self._last_by_kind[obj.kind] = obj.identity
            self._last_any = obj.identity

    def last(self, kind: str = "") -> DesktopObject | None:
        with self._lock:
            key = self._last_by_kind.get(kind, "") if kind else self._last_any
            return self._objects.get(key) if key else None

    def get(self, identity: str) -> DesktopObject | None:
        with self._lock:
            return self._objects.get(identity)

    def snapshot(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            values = sorted(self._objects.values(), key=lambda x: x.updated_at, reverse=True)
            return [asdict(x) for x in values[:limit]]


_GLOBAL = DesktopObjectModel()


def desktop_objects() -> DesktopObjectModel:
    return _GLOBAL
