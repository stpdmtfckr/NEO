"""Separate short connection deadlines from long-poll read deadlines."""
from __future__ import annotations
import math


def request_timeouts(read_seconds: float) -> tuple[float, float]:
    read = float(read_seconds)
    if not math.isfinite(read) or read <= 0:
        raise ValueError("HTTP timeout must be a positive finite number")
    return min(5.0, read), read
