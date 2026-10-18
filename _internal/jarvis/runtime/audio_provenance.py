# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Cheap acoustic provenance helpers.

The wake recognizer listens only to the physical microphone. System playback is
captured separately through WASAPI loopback and reduced to a compact 48-float
spectro-temporal signature. Comparing those signatures lets NEO reject speaker
echo without another neural model and without persisting raw system audio.
"""

from typing import Any
import numpy as np

_SIGNATURE_FRAMES = 16
_SIGNATURE_FEATURES = 3  # log-RMS, zero-crossing rate, high-band energy ratio
_SIGNATURE_SIZE = _SIGNATURE_FRAMES * _SIGNATURE_FEATURES


def _to_analysis_rate(arr: np.ndarray, sample_rate: int, target_rate: int = 8000) -> tuple[np.ndarray, int]:
    sr = max(1, int(sample_rate))
    if sr <= target_rate:
        return arr, sr
    # Integer decimation is sufficient for a provenance fingerprint; the spectral
    # feature is deliberately broad and does not need hi-fi reconstruction.
    step = max(1, int(round(sr / float(target_rate))))
    return arr[::step].astype(np.float32, copy=False), max(1, int(round(sr / step)))


def compact_audio_envelope(audio: Any, sample_rate: int, *, seconds: float = 0.96, bins: int = 48) -> np.ndarray:
    """Return a fixed 48-float spectro-temporal fingerprint.

    ``bins`` is retained for API compatibility; HOTFIX124 intentionally fixes the
    signature geometry at 16 time frames × 3 robust features.
    """
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    if arr.size == 0 or int(sample_rate) <= 0:
        return np.zeros((_SIGNATURE_SIZE,), dtype=np.float32)
    wanted = max(256, int(float(sample_rate) * float(seconds)))
    if arr.size > wanted:
        arr = arr[-wanted:]
    np.nan_to_num(arr, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    arr = arr - float(np.mean(arr, dtype=np.float64))
    arr, sr = _to_analysis_rate(arr, int(sample_rate), 8000)
    if arr.size < _SIGNATURE_FRAMES * 8:
        return np.zeros((_SIGNATURE_SIZE,), dtype=np.float32)

    edges = np.linspace(0, arr.size, _SIGNATURE_FRAMES + 1, dtype=np.int64)
    feats = np.zeros((_SIGNATURE_FRAMES, _SIGNATURE_FEATURES), dtype=np.float32)
    for idx in range(_SIGNATURE_FRAMES):
        a, b = int(edges[idx]), int(edges[idx + 1])
        frame = arr[a:b]
        if frame.size < 8:
            continue
        rms = float(np.sqrt(np.mean(np.square(frame), dtype=np.float64)))
        signs = np.signbit(frame)
        zcr = float(np.count_nonzero(signs[1:] != signs[:-1])) / max(1.0, float(frame.size - 1))
        # Broad high-band ratio. A room/speaker path changes gain, but the phoneme
        # sequence's voiced/unvoiced spectral pattern is still highly correlated.
        windowed = frame * np.hanning(frame.size).astype(np.float32)
        spec = np.abs(np.fft.rfft(windowed)).astype(np.float32)
        power = np.square(spec, dtype=np.float32)
        freqs = np.fft.rfftfreq(frame.size, d=1.0 / float(sr))
        total = float(np.sum(power, dtype=np.float64)) + 1e-12
        high = float(np.sum(power[freqs >= 1200.0], dtype=np.float64)) / total
        feats[idx] = (np.log1p(rms * 120.0), zcr, high)

    # Normalize each feature over time. This removes fixed gain/EQ differences
    # between loopback and the same sound after speaker→room→microphone propagation.
    for col in range(_SIGNATURE_FEATURES):
        values = feats[:, col]
        mean = float(np.mean(values, dtype=np.float64))
        std = float(np.std(values, dtype=np.float64))
        if std >= 1e-4:
            feats[:, col] = (values - mean) / std
        else:
            feats[:, col] = 0.0
    out = feats.reshape(-1)
    norm = float(np.linalg.norm(out))
    if norm <= 1e-6:
        return np.zeros((_SIGNATURE_SIZE,), dtype=np.float32)
    out /= norm
    return out.astype(np.float32, copy=False)


def best_envelope_similarity(a: Any, b: Any, *, max_shift: int = 10, min_overlap: int = 24) -> float:
    x = np.asarray(a, dtype=np.float32).reshape(-1)
    y = np.asarray(b, dtype=np.float32).reshape(-1)
    if x.size != _SIGNATURE_SIZE or y.size != _SIGNATURE_SIZE or not np.any(x) or not np.any(y):
        return 0.0
    xm = x.reshape(_SIGNATURE_FRAMES, _SIGNATURE_FEATURES)
    ym = y.reshape(_SIGNATURE_FRAMES, _SIGNATURE_FEATURES)
    # max_shift historically meant scalar bins. Map it to a conservative number
    # of time frames (~60 ms each) while preserving API compatibility.
    row_limit = max(0, min(6, int(round(max_shift / float(_SIGNATURE_FEATURES)))))
    min_rows = max(5, int(np.ceil(float(min_overlap) / _SIGNATURE_FEATURES)))
    best = -1.0
    for shift in range(-row_limit, row_limit + 1):
        if shift >= 0:
            xa, ya = xm[shift:], ym[: _SIGNATURE_FRAMES - shift]
        else:
            off = -shift
            xa, ya = xm[: _SIGNATURE_FRAMES - off], ym[off:]
        n = min(xa.shape[0], ya.shape[0])
        if n < min_rows:
            continue
        xv = xa[:n].reshape(-1)
        yv = ya[:n].reshape(-1)
        nx = float(np.linalg.norm(xv))
        ny = float(np.linalg.norm(yv))
        if nx <= 1e-6 or ny <= 1e-6:
            continue
        score = float(np.dot(xv, yv) / (nx * ny))
        best = max(best, score)
    return max(0.0, min(1.0, best))


__all__ = ["compact_audio_envelope", "best_envelope_similarity"]
