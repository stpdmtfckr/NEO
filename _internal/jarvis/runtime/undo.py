# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Callable


@dataclass
class UndoRecord:
    label: str
    kind: str
    data: dict[str, Any]
    created_at: float


class UndoManager:
    def __init__(self, max_items: int = 100) -> None:
        self._lock = threading.RLock()
        self._items: list[tuple[UndoRecord, Callable[[], Any]]] = []
        self.max_items = max_items

    def push(self, label: str, kind: str, data: dict[str, Any], callback: Callable[[], Any]) -> None:
        with self._lock:
            self._items.append((UndoRecord(label, kind, dict(data), time.time()), callback))
            del self._items[:-self.max_items]

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [asdict(x[0]) for x in reversed(self._items[-limit:])]

    def undo(self, count: int = 1) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for _ in range(max(1, min(20, int(count)))):
            with self._lock:
                if not self._items:
                    break
                record, callback = self._items.pop()
            try:
                value = callback()
                results.append({"label": record.label, "success": True, "result": value})
            except Exception as exc:
                # A transient Windows/UIA failure must not destroy the only undo
                # record. Put it back so «отмени последнее действие» can be
                # retried after the target window becomes responsive again.
                with self._lock:
                    self._items.append((record, callback))
                results.append({"label": record.label, "success": False, "error": str(exc)})
                break
        return results


_GLOBAL = UndoManager()


def undo_manager() -> UndoManager:
    return _GLOBAL
