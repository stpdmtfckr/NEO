"""NEO 188 NEO-ASR-002 / TZ-ASR-RECOGNITION-EVENT-CONSISTENCY-01.

Recognition events belong to one session and one capture.  This module is the
adapter contract every speech path uses before a phrase reaches the command
handler:

* a partial hypothesis only REPLACES the preview of its own capture; a shorter
  revision really removes the vanished tail and never dispatches anything;
* final segments are kept by their result index (or appended in order when an
  engine has no index); a cumulative word array replaces the earlier array of
  the same result, so words are neither lost nor duplicated and the first
  final segment is not taken for the whole command;
* ``end()`` hands over the assembled logical command at most once; an end
  without a valid final result reports ``no_result`` and never claims success;
* ``cancel()`` discards everything: the buffered audio/text is not passed on;
* events of another session/capture/generation are refused, and a repeated
  delivery of an already dispatched result does not create a new command,
  while a new independent capture with the same words is accepted;
* stability, speaker labels and missing confidence never authorize an action;
  a missing confidence stays ``None`` (no invented number);
* state is bounded: rapid revisions keep only the latest hypothesis.

Local Whisper decodes one finished recording, so on the desktop the contract
is: partial = display-only replacement, one final per capture, one dispatch per
(scope, capture, text).  No streaming backend, cloud audio or second ASR pass
is added.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable

MAX_SEGMENTS = 64          # one spoken command never needs more final segments
MAX_TEXT = 4000            # characters of one logical command
MAX_GUARD = 128            # remembered dispatched captures
GUARD_TTL = 600.0          # seconds a dispatched capture stays remembered


def _clean(text: Any) -> str:
    return ' '.join(str(text or '').split())[:MAX_TEXT]


@dataclass
class Outcome:
    status: str                    # dispatch | no_result | incomplete | cancelled | duplicate | refused
    text: str = ''
    confidence: float | None = None


@dataclass
class UtteranceAssembler:
    session: str
    capture: str
    generation: int = 0
    _partial: str = ''
    _finals: 'OrderedDict[int, str]' = field(default_factory=OrderedDict)
    _next_index: int = 0
    _confidence: float | None = None
    _state: str = 'open'           # open | ended | cancelled
    revisions: int = 0

    # ----------------------------------------------------------------- events
    def owns(self, session: str, capture: str, generation: int | None = None) -> bool:
        if session != self.session or capture != self.capture:
            return False
        return generation is None or generation == self.generation

    def partial(self, text: str, *, session: str, capture: str, generation: int | None = None,
                index: int | None = None, stability: float | None = None, speaker: Any = None) -> str | None:
        """Replace the interim tail. Returns the preview text, or None when refused."""
        if self._state != 'open' or not self.owns(session, capture, generation):
            return None
        if index is not None:
            index = int(index)
            # An engine reported a smaller result list: finals past it are gone.
            for key in [k for k in self._finals if k >= index]:
                del self._finals[key]
            self._next_index = min(self._next_index, index)
        self._partial = _clean(text)
        self.revisions += 1
        return self.preview()

    def final(self, text: str, *, session: str, capture: str, generation: int | None = None,
              index: int | None = None, confidence: float | None = None) -> bool:
        if self._state != 'open' or not self.owns(session, capture, generation):
            return False
        value = _clean(text)
        if index is None:
            index = self._next_index
        index = int(index)
        if index < 0:
            return False
        if index not in self._finals and len(self._finals) >= MAX_SEGMENTS:
            return False
        self._finals[index] = value                     # same index again = replacement, not a duplicate
        self._finals = OrderedDict(sorted(self._finals.items()))
        self._next_index = max(self._next_index, index + 1)
        self._partial = ''
        if confidence is not None:
            try:
                number = float(confidence)
            except (TypeError, ValueError):
                number = None
            if number is not None and 0.0 <= number <= 1.0:
                self._confidence = number if self._confidence is None else min(self._confidence, number)
        return True

    def words(self, words: Iterable[Any], *, session: str, capture: str, generation: int | None = None,
              index: int | None = None, final: bool = False) -> bool:
        """A cumulative word array for one result replaces the earlier array of that result."""
        text = ' '.join(_clean(w) for w in words if _clean(w))
        if final:
            return self.final(text, session=session, capture=capture, generation=generation, index=index)
        return self.partial(text, session=session, capture=capture, generation=generation, index=index) is not None

    # -------------------------------------------------------------- lifecycle
    def preview(self) -> str:
        return _clean(' '.join([*(v for v in self._finals.values() if v), self._partial]))

    def text(self) -> str:
        return _clean(' '.join(v for v in self._finals.values() if v))

    def end(self, *, session: str, capture: str, generation: int | None = None) -> Outcome:
        """Stop receiving audio and hand over what was finally recognized — once."""
        if not self.owns(session, capture, generation):
            return Outcome('refused')
        if self._state == 'cancelled':
            return Outcome('cancelled')
        if self._state == 'ended':
            return Outcome('duplicate')
        self._state = 'ended'
        text = self.text()
        self._partial = ''
        if not text:
            return Outcome('no_result')               # disconnected / no valid final: no success claimed
        return Outcome('dispatch', text, self._confidence)

    def disconnect(self) -> Outcome:
        """The stream broke before the engine confirmed the end of the phrase.

        Final segments seen so far are NOT a complete command: nothing is
        dispatched and no success is claimed."""
        if self._state != 'open':
            return Outcome('duplicate' if self._state == 'ended' else 'cancelled')
        self._state = 'ended'
        text = self.text()
        self._partial = ''
        return Outcome('incomplete' if text else 'no_result', text)

    def cancel(self) -> Outcome:
        """Abort recognition and discard the buffered result."""
        self._state = 'cancelled'
        self._partial = ''
        self._finals.clear()
        return Outcome('cancelled')

    @property
    def state(self) -> str:
        return self._state


class DispatchGuard:
    """At most one logical dispatch per (scope, capture, text) inside a bounded window."""

    def __init__(self, *, size: int = MAX_GUARD, ttl: float = GUARD_TTL):
        self._seen: 'OrderedDict[tuple[str, str, str], float]' = OrderedDict()
        self._size, self._ttl = max(1, int(size)), float(ttl)

    def claim(self, scope: str, capture: str, text: str, *, now: float | None = None) -> bool:
        capture = str(capture or '')
        if not capture:
            return True                                   # ownership unknown: never invent a duplicate
        now = time.monotonic() if now is None else float(now)
        for key, at in list(self._seen.items()):
            if now - at > self._ttl:
                del self._seen[key]
        key = (str(scope or 'desktop'), capture, _clean(text).casefold())
        if key in self._seen:
            return False
        self._seen[key] = now
        while len(self._seen) > self._size:
            self._seen.popitem(last=False)
        return True

    def __len__(self) -> int:
        return len(self._seen)


def live_covers(partial_samples: int | None, final_samples: int, sample_rate: int, silence_seconds: float) -> bool:
    """A live partial may stand in for the final decode only when it covered the whole speech.

    The final recording may contain only the endpoint silence beyond the last
    partial; anything longer means the partial missed spoken words.
    """
    if partial_samples is None or final_samples <= 0 or sample_rate <= 0:
        return False
    tail = int(sample_rate * (max(0.0, float(silence_seconds or 0.0)) + 0.25))
    return int(partial_samples) >= int(final_samples) - tail
