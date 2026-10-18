# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import threading
import time
import math
import uuid
from contextvars import ContextVar
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

_TRACE: ContextVar[tuple[str, float] | None] = ContextVar("neo_latency_trace", default=None)


@contextmanager
def trace(correlation_id: str = "") -> Iterator[str]:
    """Carry timing identity across one worker turn without storing user text."""
    identity = str(correlation_id or uuid.uuid4().hex)[:100]
    token = _TRACE.set((identity, time.perf_counter()))
    try:
        yield identity
    finally:
        _TRACE.reset(token)


@dataclass
class LatencyEvent:
    stage: str
    seconds: float
    slow: bool
    timestamp: float
    detail: str = ""
    correlation_id: str = ""


class LatencyProfiler:
    def __init__(self, threshold_seconds: float = 2.0, max_events: int = 300) -> None:
        self.threshold_seconds = float(threshold_seconds)
        self._events: deque[LatencyEvent] = deque(maxlen=max_events)
        self._lock = threading.RLock()

    @contextmanager
    def measure(self, stage: str, detail: str = "", *, correlation_id: str = "") -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = max(0.0, time.perf_counter() - started)
            self.record(stage, elapsed, detail, correlation_id=correlation_id)

    def record(self, stage: str, seconds: float, detail: str = "", *, correlation_id: str = "") -> None:
        if not math.isfinite(float(seconds)):
            return
        value = max(0.0, float(seconds))
        current = _TRACE.get()
        identity = str(correlation_id or (current[0] if current else ""))[:100]
        with self._lock:
            self._events.append(LatencyEvent(str(stage), value, value > self.threshold_seconds, time.time(), str(detail), identity))

    def mark(self, stage: str, detail: str = "") -> None:
        """Measure from the preceding mark in trace(), then advance its clock."""
        current = _TRACE.get()
        if current is None:
            return
        now = time.perf_counter()
        self.record(stage, now - current[1], detail, correlation_id=current[0])
        _TRACE.set((current[0], now))

    def statistics(self) -> dict[str, dict]:
        """Nearest-rank observed percentiles; bounded by the same event ring."""
        groups: dict[str, list[float]] = {}
        for row in self.recent(self._events.maxlen or 300):
            groups.setdefault(row["stage"], []).append(row["seconds"] * 1000.0)
        result = {}
        for stage, values in groups.items():
            values.sort()
            result[stage] = {"samples": len(values), **{
                "p" + str(percentile) + "_ms": values[max(0, math.ceil(percentile / 100 * len(values)) - 1)]
                for percentile in (50, 95, 99)
            }}
        return result

    def recent(self, limit: int = 30) -> list[dict]:
        with self._lock:
            return [asdict(x) for x in list(self._events)[-max(1, int(limit)):]]

    def summary(self, limit: int = 30) -> str:
        rows = self.recent(limit)
        if not rows:
            return "Профиль задержек пока пуст."
        recent = " | ".join(
            f"{r['stage']} {r['seconds']:.2f}s" + (" ⚠" if r['slow'] else "") for r in rows[-8:]
        )
        statistics = self.statistics()
        stages = list(dict.fromkeys(r['stage'] for r in rows))[-8:]
        percentiles = [f"{stage}: p50={statistics[stage]['p50_ms']:.1f} ms, p95={statistics[stage]['p95_ms']:.1f} ms, p99={statistics[stage]['p99_ms']:.1f} ms (n={statistics[stage]['samples']})" for stage in stages if stage in statistics]
        return recent + "\n" + "\n".join(percentiles)

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.recent(300), ensure_ascii=False, indent=2), encoding="utf-8")


_GLOBAL = LatencyProfiler()


def latency_profiler() -> LatencyProfiler:
    return _GLOBAL
