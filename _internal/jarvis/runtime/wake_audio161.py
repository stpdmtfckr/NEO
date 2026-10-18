"""Bounded wake prefix selection; original command PCM remains untouched."""
from __future__ import annotations

import numpy as np


def wake_probe_prefix(audio: np.ndarray, sample_rate: int, *, now: float,
                      stable_onset: float, window_seconds: float,
                      onset_guard_seconds: float) -> tuple[np.ndarray, float]:
    """Retain pre-consonant padding and the beginning, never the rolling tail.

    The energy gate confirms speech after a short stable run. Its timestamp is
    not the true first phoneme; subtract that run and another 100 ms rather than
    trimming exactly at the trigger. Window is bounded to the existing 1.4 s cap.
    """
    rate = max(1, int(sample_rate))
    value = np.asarray(audio, dtype=np.float32).reshape(-1)
    started = float(now) - value.size / rate
    onset = float(stable_onset) - max(0.0, float(onset_guard_seconds)) - 0.10
    first = max(0, min(value.size, int((onset - started) * rate)))
    limit = max(1, int(rate * max(0.50, min(1.40, float(window_seconds)))))
    return value[first:first + limit], max(0.0, started + first / rate)
