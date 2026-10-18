# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np

from jarvis.voice import RecordingResult


class WakeWordGuard:
    """Cheap sanity gate before wake-word STT.

    The audio subprocess already performs utterance segmentation and noise-floor
    tracking. RC4 then applied a second, stricter stationarity test which could
    reject a perfectly clean short word such as "Джарвис" because almost every
    frame was voiced. RC5 only rejects true silence, corrupt audio and extremely
    short clicks; false wake-ups are handled by the phonetic text matcher instead.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._suppressed_until = 0.0

    def suppress_for(self, seconds: float) -> None:
        self._suppressed_until = max(
            self._suppressed_until, self._clock() + max(0.0, float(seconds))
        )

    def accepts(self, recording: RecordingResult, sensitivity: int) -> bool:
        if self._clock() < self._suppressed_until:
            return False
        audio = np.asarray(recording.audio, dtype=np.float32).reshape(-1)
        minimum = max(1200, int(recording.sample_rate * 0.12))
        if audio.size < minimum:
            return False
        finite = audio[np.isfinite(audio)]
        if finite.size < minimum:
            return False

        finite = finite - float(np.mean(finite, dtype=np.float64))
        rms = float(np.sqrt(np.mean(np.square(finite), dtype=np.float64)))
        peak = float(np.max(np.abs(finite)))
        sensitivity = max(1, min(100, int(sensitivity)))
        # At high sensitivity the threshold intentionally goes very low. The audio
        # backend has already decided this is a voiced utterance; this gate should
        # not second-guess quiet USB/headset microphones.
        base_threshold = 10 ** (-(42.0 + sensitivity * 0.25) / 20.0)
        return rms >= base_threshold * 0.16 and peak >= base_threshold * 0.55
