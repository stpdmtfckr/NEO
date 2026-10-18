"""NEO 168: fewer Whisper runs while the microphone hears ordinary speech.

Three independent savings on the permanent wake stream:

1. Bounded encoder window. The full wake decode receives at most ~3 s of audio,
   yet Whisper encoded a 30 s window (the rest is padding). The window now
   follows the audio length plus one second; the rolling probe already used a
   2 s window in production.
2. Speaker playback (videos, games, calls) is recognised by the existing echo
   check *before* decoding. The check and its decision are unchanged; it used to
   run only after the decode had already been paid for.
3. Conversation back-off. After several long rejected utterances in a row
   (people talking nearby, a podcast from a phone), an utterance whose onset
   probe clearly was not the wake word is not decoded a second time for a short
   while. Short utterances (quick commands, a lone «Джарвис») are never skipped.
"""
from __future__ import annotations

from collections import OrderedDict, deque
import math
import threading
import time
from typing import Callable


def decode_window_seconds(audio_seconds: float) -> int:
    """Whisper encoder window for a completed wake utterance (4..30 s)."""
    try:
        seconds = float(audio_seconds)
    except (TypeError, ValueError):
        return 30
    if not math.isfinite(seconds) or seconds <= 0:
        return 4
    return int(min(30, max(4, math.ceil(seconds + 1.0))))


class ConversationBackoff:
    """Thread-safe bookkeeping; decisions are pure functions of recorded events."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, window_seconds: float = 12.0,
                 needed: int = 3, hold_seconds: float = 15.0, min_words: int = 4,
                 min_skip_seconds: float = 1.35, enabled: bool = True) -> None:
        self.clock = clock
        self.window_seconds = float(window_seconds)
        self.needed = int(needed)
        self.hold_seconds = float(hold_seconds)
        self.min_words = int(min_words)
        self.min_skip_seconds = float(min_skip_seconds)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._rejected: deque[float] = deque(maxlen=16)
        self._active_until = 0.0
        self._probes: OrderedDict[str, str] = OrderedDict()
        self.skipped = 0

    # ---------------------------------------------------------------- events
    def record_rejected(self, text: str, duration: float) -> None:
        words = len(str(text or '').split())
        if words < self.min_words or float(duration or 0.0) < self.min_skip_seconds:
            return
        now = self.clock()
        with self._lock:
            self._rejected.append(now)
            recent = [t for t in self._rejected if now - t <= self.window_seconds]
            if len(recent) >= self.needed:
                self._active_until = now + self.hold_seconds

    def record_wake(self) -> None:
        with self._lock:
            self._rejected.clear()
            self._active_until = 0.0

    def remember_probe(self, correlation_id: str, text: str) -> None:
        if not correlation_id:
            return
        with self._lock:
            self._probes[str(correlation_id)] = str(text or '')
            self._probes.move_to_end(str(correlation_id))
            while len(self._probes) > 64:
                self._probes.popitem(last=False)

    # -------------------------------------------------------------- decisions
    def active(self) -> bool:
        with self._lock:
            return self.enabled and self.clock() < self._active_until

    def should_skip(self, correlation_id: str, duration: float, is_wake_like: Callable[[str], bool]) -> bool:
        """Skip the full decode of a long utterance whose onset was clearly not the wake word."""
        if not self.active() or float(duration or 0.0) <= self.min_skip_seconds:
            return False
        with self._lock:
            probe = self._probes.get(str(correlation_id or ''), None)
        if not probe or not probe.strip():
            return False  # no onset evidence: decode as before
        try:
            if is_wake_like(probe):
                return False
        except Exception:
            return False
        with self._lock:
            self.skipped += 1
        return True
