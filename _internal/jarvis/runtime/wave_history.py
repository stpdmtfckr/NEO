"""Cross-adapter anti-repeat history; entries contain IDs/fingerprints, no tokens."""
from __future__ import annotations
from functools import wraps
from pathlib import Path
import math
import threading
import time
from typing import Any

_LOCKS: dict[str, Any] = {}
_GUARD = threading.Lock()
_FALLBACK = threading.RLock()


def history_lock(path: Path | None):
    key = str(path.resolve(strict=False)) if path else "unconfigured"
    with _GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def synchronized(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        with getattr(self, "_wave_history_lock", _FALLBACK):
            return function(self, *args, **kwargs)
    return call


def merge_entries(*groups: list[dict], now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    rows = []
    for group in groups:
        for row in group:
            if not isinstance(row, dict):
                continue
            try:
                stamp = float(row.get("ts", 0))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(stamp) or stamp < now-30*86400:
                continue
            key, fingerprint = str(row.get("id") or ""), str(row.get("fingerprint") or "")
            if key or fingerprint:
                rows.append({"id": key[:160], "fingerprint": fingerprint[:1024], "ts": min(now, stamp)})
    rows.sort(key=lambda item: item["ts"], reverse=True)
    ids: set[str] = set(); fingerprints: set[str] = set(); chosen = []
    for row in rows:
        if (row["id"] and row["id"] in ids) or (row["fingerprint"] and row["fingerprint"] in fingerprints):
            continue
        chosen.append(row)
        if row["id"]: ids.add(row["id"])
        if row["fingerprint"]: fingerprints.add(row["fingerprint"])
        if len(chosen) >= 2000: break
    return list(reversed(chosen))
