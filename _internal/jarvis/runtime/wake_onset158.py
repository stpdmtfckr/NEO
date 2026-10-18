"""Separate the accepted address from the onset of a following command.

Only acoustic evidence after the wake probe can arm ordinary endpointing.  The
retained wake PCM is still passed intact to the final decoder for one-flow speech.
No model, thread, disk write or additional microphone stream is introduced here.
"""

from __future__ import annotations

import math

import numpy as np


class PostWakeOnset:
    def __init__(
        self,
        boundary: float,
        *,
        guard_seconds: float = 0.12,
        minimum_seconds: float = 0.18,
    ):
        self.after = float(boundary) + max(0.0, float(guard_seconds))
        self.minimum = max(0.08, float(minimum_seconds))
        self.run = 0.0
        self.confirmed = False
        self.started_at = 0.0
        self.last_voice_at = 0.0
        self.quiet_seconds = 0.0
        self.separated = False

    def observe(self, block, rate: int, end: float, *, threshold: float, shape):
        """Consume only fresh PCM; frame long history blocks exactly like live PCM."""
        audio = np.asarray(block, dtype=np.float32).reshape(-1)
        rate = max(1, int(rate))
        beginning = float(end) - audio.size / rate
        offset = max(0, min(audio.size, math.ceil((self.after - beginning) * rate)))
        frame = max(1, int(rate * 0.02))
        for index in range(offset, audio.size, frame):
            sample = audio[index : index + frame]
            if not sample.size:
                continue
            duration = sample.size / rate
            frame_end = beginning + (index + sample.size) / rate
            rms = float(np.linalg.norm(sample) / math.sqrt(sample.size))
            peak = float(np.max(np.abs(sample)))
            voiced = (rms >= threshold or peak >= threshold * 3.8) and shape(
                sample, rate
            )
            if voiced:
                if not self.confirmed and self.quiet_seconds >= 0.18:
                    self.separated = True
                self.quiet_seconds = 0.0
                self.run += duration
                if self.run + 1e-6 >= self.minimum:
                    if not self.confirmed:
                        self.started_at = frame_end - self.run
                    self.confirmed = True
                    self.last_voice_at = frame_end
            else:
                self.quiet_seconds += duration
                self.run = max(0.0, self.run - duration * 2.0)
        return self.confirmed
