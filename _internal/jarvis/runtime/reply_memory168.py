"""NEO 168: the last answers NEO gave, per conversation (desktop chat, Telegram, …).

«Скопируй то, что ты написал» copies the previous answer of the same
conversation. Only the few most recent answers are kept, in memory only.
"""
from __future__ import annotations

from collections import OrderedDict, deque
import threading

_LOCK = threading.Lock()
_REPLIES: OrderedDict[str, deque[str]] = OrderedDict()
# Answers that are only acknowledgements of a copy/paste request are not "what NEO wrote".
_SKIP_TOOLS = frozenset({'copy'})


def remember(scope: str, text: str, *, tools: list[str] | tuple[str, ...] = ()) -> None:
    value = str(text or '').strip()
    if not value or any(str(t).split(':', 1)[0].split('.', 1)[0].strip() in _SKIP_TOOLS for t in tools or ()):
        return
    key = str(scope or 'desktop')
    with _LOCK:
        history = _REPLIES.setdefault(key, deque(maxlen=5))
        history.append(value)
        _REPLIES.move_to_end(key)
        while len(_REPLIES) > 32:
            _REPLIES.popitem(last=False)


def last(scope: str, *, back: int = 1) -> str:
    """``back=1`` is the latest answer, 2 the one before it."""
    with _LOCK:
        history = list(_REPLIES.get(str(scope or 'desktop'), ()))
    if not history or back < 1 or back > len(history):
        return ''
    return history[-back]


def clear() -> None:
    with _LOCK:
        _REPLIES.clear()
