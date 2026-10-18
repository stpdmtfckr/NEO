# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import re
from collections.abc import Callable, Iterator


def iter_stream_chunks(text: str, *, target_chars: int = 18, hard_max_chars: int = 48) -> Iterator[str]:
    """Yield small text-preserving chunks for already-computed responses.

    Real LLM output arrives as genuine Ollama deltas. Deterministic/tool paths do
    not have generation tokens, so this adapter exposes them through the same
    streaming contract without inventing a second response mode. Whitespace and
    punctuation are preserved exactly; joining the yielded chunks always rebuilds
    the original string byte-for-byte at Python's Unicode-string level.
    """

    value = str(text or "")
    if not value:
        return
    target = max(1, int(target_chars or 18))
    hard_max = max(target, int(hard_max_chars or 48))

    # Word + following whitespace is a good approximation of a display token for
    # precomputed replies and avoids character-at-a-time signal storms.
    pieces = re.findall(r"\S+\s*|\s+", value, flags=re.UNICODE)
    pending = ""
    for piece in pieces:
        if not piece:
            continue
        # Exceptionally long URL/path/hash: split it so one huge direct result does
        # not become a single UI update.
        while len(piece) > hard_max:
            prefix, piece = piece[:hard_max], piece[hard_max:]
            if pending:
                yield pending
                pending = ""
            yield prefix
        if not piece:
            continue
        if pending and len(pending) + len(piece) > target:
            yield pending
            pending = ""
        pending += piece
        if len(pending) >= target:
            yield pending
            pending = ""
    if pending:
        yield pending


class ResponseStreamSink:
    """One user-visible streaming contract for every Agent response.

    Ollama callbacks usually contain tiny live deltas and pass through unchanged.
    A complete deterministic/tool result is automatically split into small chunks,
    which lets the UI use exactly the same streaming path instead of a separate
    typewriter/final-answer branch.
    """

    def __init__(
        self,
        callback: Callable[[str], None] | None,
        *,
        target_chars: int = 18,
        hard_max_chars: int = 48,
    ) -> None:
        self.callback = callback
        self.target_chars = max(1, int(target_chars or 18))
        self.hard_max_chars = max(self.target_chars, int(hard_max_chars or 48))

    def emit(self, text: str) -> None:
        callback = self.callback
        value = str(text or "")
        if callback is None or not value:
            return
        # Genuine token/delta paths should remain low-latency and untouched.
        if len(value) <= self.target_chars:
            callback(value)
            return
        for chunk in iter_stream_chunks(
            value,
            target_chars=self.target_chars,
            hard_max_chars=self.hard_max_chars,
        ):
            callback(chunk)

    __call__ = emit
