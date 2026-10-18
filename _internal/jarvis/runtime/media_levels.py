# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Low-overhead live media spectrum bus.

The robot belly visualizer and the standalone music overlay consume frequency
bands, not a synthetic time-domain envelope. Discord PCM frames are analysed in
the audio thread with one bounded FFT per 20 ms frame; the UI only reads the
latest spectrum and interpolates it at its own render cadence.
"""

from collections import deque
from dataclasses import dataclass
from functools import lru_cache
import math
import threading
import time
from typing import Deque, Iterable

try:
    import numpy as np
except Exception:  # pragma: no cover - runtime always ships numpy, keep safe fallback
    np = None  # type: ignore[assignment]


@dataclass
class _LevelState:
    values: Deque[float]
    updated: float = 0.0
    spectrum: bool = False


_LOCK = threading.RLock()
_STATES: dict[str, _LevelState] = {}


@lru_cache(maxsize=12)
def _hann_window(size: int):
    if np is None:
        return None
    return np.hanning(max(1, int(size))).astype(np.float32)


@lru_cache(maxsize=24)
def _band_slices(sample_rate: int, n_fft: int, count: int, low_hz: int, high_hz: int) -> tuple[tuple[int, int], ...]:
    """Precompute FFT-bin ownership for log-spaced frequency bars."""
    if np is None:
        return tuple()
    nyquist = max(100.0, float(sample_rate) * 0.5)
    lo = max(30.0, min(float(low_hz), nyquist * 0.35))
    hi = max(lo * 1.8, min(float(high_hz), nyquist * 0.96))
    edges = np.geomspace(lo, hi, max(4, int(count)) + 1)
    hz_per_bin = float(sample_rate) / max(1, int(n_fft))
    out: list[tuple[int, int]] = []
    max_bin = int(n_fft) // 2
    for i in range(max(4, int(count))):
        start = max(0, min(max_bin, int(math.ceil(float(edges[i]) / hz_per_bin))))
        end = max(start + 1, min(max_bin + 1, int(math.ceil(float(edges[i + 1]) / hz_per_bin))))
        if start >= max_bin + 1:
            start = max_bin
            end = max_bin + 1
        out.append((start, end))
    return tuple(out)


def _state(source: str) -> _LevelState:
    key = str(source or "media").strip().casefold() or "media"
    with _LOCK:
        item = _STATES.get(key)
        if item is None:
            item = _LevelState(deque(maxlen=64))
            _STATES[key] = item
        return item


def _resize_bins(values: list[float], width: int) -> list[float]:
    width = max(4, min(96, int(width)))
    if not values:
        return []
    if len(values) == width:
        return [max(0.0, min(1.0, float(v))) for v in values]
    if len(values) == 1:
        return [max(0.0, min(1.0, float(values[0])))] * width
    out: list[float] = []
    n = len(values)
    for i in range(width):
        pos = i * (n - 1) / max(1, width - 1)
        lo = int(math.floor(pos))
        hi = min(n - 1, lo + 1)
        frac = pos - lo
        value = float(values[lo]) * (1.0 - frac) + float(values[hi]) * frac
        out.append(max(0.0, min(1.0, value)))
    return out


def spectrum_from_samples(
    samples: Iterable[float],
    sample_rate: int,
    *,
    width: int = 48,
    low_hz: float = 55.0,
    high_hz: float = 18000.0,
) -> list[float]:
    """Return logarithmic low->high frequency bands from mono PCM samples.

    Output bands are amplitude-derived and deterministic. No random/sine fallback
    is used. The FFT size is capped so this stays cheap enough for Discord's
    20 ms audio callback and for the headless local-player worker.
    """
    if np is None:
        return []
    try:
        x = np.asarray(list(samples) if not hasattr(samples, "__array__") else samples, dtype=np.float32).reshape(-1)
    except Exception:
        return []
    if x.size < 64:
        return []
    # Bound work. 2048 samples is enough for a responsive visual spectrum while
    # avoiding a large FFT on long Qt Multimedia buffers.
    if x.size > 2048:
        x = x[-2048:]
    x = x - float(np.mean(x))
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak <= 1e-7:
        return [0.0] * max(4, min(96, int(width)))

    n_fft = 1
    target = max(256, int(x.size))
    while n_fft < target:
        n_fft <<= 1
    n_fft = max(512, min(4096, n_fft))
    window = _hann_window(int(x.size))
    if window is None:
        return []
    coherent = max(1e-6, float(np.sum(window)) / 2.0)
    spec = np.abs(np.fft.rfft(x * window, n=n_fft)).astype(np.float64) / coherent

    count = max(4, min(96, int(width)))
    slices = _band_slices(
        int(sample_rate), int(n_fft), int(count), int(round(float(low_hz))), int(round(float(high_hz)))
    )
    result: list[float] = []
    for start, end in slices:
        band = spec[start:end]
        if band.size <= 0:
            magnitude = 0.0
        else:
            # Mix peak and RMS so narrow transients remain visible without making
            # one noisy FFT bin dominate the entire bar. Cached slice ownership
            # avoids rebuilding 48 boolean masks for every 20 ms audio frame.
            magnitude = 0.62 * float(np.max(band)) + 0.38 * float(np.sqrt(np.mean(np.square(band))))
        db = 20.0 * math.log10(max(1e-8, magnitude))
        # Map a useful music range to 0..1. The exponent is only perceptual
        # compression; each bar still responds exclusively to its own band.
        level = max(0.0, min(1.0, (db + 72.0) / 60.0))
        result.append(level ** 0.72)
    return result


def spectrum_from_pcm_s16le(
    data: bytes,
    *,
    sample_rate: int = 48000,
    channels: int = 2,
    width: int = 48,
) -> list[float]:
    if np is None or len(data) < 128:
        return []
    try:
        raw = np.frombuffer(data, dtype="<i2").astype(np.float32, copy=False)
        channels = max(1, int(channels))
        frames = raw.size // channels
        if frames <= 0:
            return []
        raw = raw[: frames * channels]
        if channels > 1:
            mono = raw.reshape(frames, channels).mean(axis=1)
        else:
            mono = raw
        mono = mono / 32768.0
        return spectrum_from_samples(mono, sample_rate, width=width)
    except Exception:
        return []


def publish_audio_packet(source: str, data: bytes, *, opus: bool) -> None:
    """Publish real frequency bands when PCM is available.

    Opus packets cannot be mapped to acoustic frequency bands without decoding.
    We intentionally publish no fake spectrum for that path. Discord playback is
    configured to prefer PCM transport; Opus remains a compatibility fallback.
    """
    if not data:
        return
    item = _state(source)
    if opus:
        # Preserve freshness semantics but do not pretend packet size is a
        # spectrum. UI will hold a quiet baseline until PCM transport is available.
        with _LOCK:
            item.values.clear()
            item.updated = time.monotonic()
            item.spectrum = False
        return
    levels = spectrum_from_pcm_s16le(data, sample_rate=48000, channels=2, width=48)
    if not levels:
        return
    with _LOCK:
        item.values.clear()
        item.values.extend(float(v) for v in levels)
        item.updated = time.monotonic()
        item.spectrum = True


def publish_spectrum(source: str, values: Iterable[float]) -> None:
    cleaned = [max(0.0, min(1.0, float(v))) for v in values]
    if not cleaned:
        return
    item = _state(source)
    with _LOCK:
        item.values.clear()
        item.values.extend(cleaned[:96])
        item.updated = time.monotonic()
        item.spectrum = True


def clear_media_levels(source: str) -> None:
    item = _state(source)
    with _LOCK:
        item.values.clear()
        item.updated = 0.0
        item.spectrum = False


def media_levels(source: str, *, max_age: float = 1.0, width: int = 48) -> tuple[list[float], bool]:
    item = _state(source)
    with _LOCK:
        if (
            not item.values
            or not item.spectrum
            or item.updated <= 0.0
            or (time.monotonic() - item.updated) > max(0.1, float(max_age))
        ):
            return [], False
        values = list(item.values)
    return _resize_bins(values, width), True
