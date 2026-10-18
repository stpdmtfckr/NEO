"""NEO 185: a light «robot assistant» colour for any voice. Pure numpy, no state, CPU-cheap.

The effect mixes the voice with (1) a ring-modulated copy — the classic metallic «robot»
timbre — and (2) a short comb resonance that sounds like a small metal speaker. Speech
stays intelligible because the dry signal is always kept in the mix.
"""
from __future__ import annotations


def robotize(samples, rate: int, amount: float):
    import numpy as np
    x = np.asarray(samples, dtype=np.float32)
    amount = max(0.0, min(1.0, float(amount)))
    if amount <= 0.0 or x.ndim != 1 or x.size == 0 or rate <= 0:
        return x
    peak = float(np.max(np.abs(x))) or 1.0
    t = np.arange(x.size, dtype=np.float32) / float(rate)
    ring = x * np.sin(2.0 * np.pi * 52.0 * t, dtype=np.float32)
    delay = max(1, int(rate * 0.0055))
    comb = x.copy()
    comb[delay:] += 0.5 * x[:-delay]
    if 2 * delay < x.size:
        comb[2 * delay:] += 0.25 * x[:-2 * delay]
    wet = 0.6 * ring + 0.55 * comb
    y = (1.0 - 0.75 * amount) * x + amount * wet
    new_peak = float(np.max(np.abs(y))) or 1.0
    return np.clip(y * (peak / new_peak), -1.0, 1.0).astype(np.float32)
