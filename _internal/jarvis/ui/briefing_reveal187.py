"""NEO 187 TZ-SUMMARY-01: the daily briefing types out like an ordinary reply.

The briefing grows section by section (greeting, waiting lines, ready
results). Each accepted revision is revealed through one cursor at the speed
the user chose for ordinary replies:

* the already visible prefix is never erased while the text only grows;
* a replaced section (waiting -> ready) keeps everything before it and types
  the rest again from that point;
* with character streaming switched off the whole text appears at once;
* a deleted bubble (cleared chat) stops the reveal without resurrecting it.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer


class GrowingReveal(QObject):
    def __init__(self, chat: Any, config: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._chat, self._config = chat, config
        self._bubble = None
        self._target = ""
        self._shown = 0
        self._done: Callable[[], None] | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def _paced(self) -> bool:
        return bool(self._config.get("ui.character_streaming_enabled", True))

    def _step(self) -> tuple[int, int]:
        desired = max(1, min(120, int(self._config.get("ui.typewriter_interval_ms", 8) or 8)))
        fps = int(self._config.get("ui.animation_fps", 60) or 60)
        frame = {30: 33, 60: 16}.get(fps, 16)
        chars = max(1, int(round(frame / desired))) if desired < frame else 1
        return max(frame, desired), chars

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    def show(self, bubble: Any, text: str, on_done: Callable[[], None] | None = None) -> None:
        text = str(text or "")
        if bubble is not self._bubble:
            self.flush()
            self._bubble, self._shown = bubble, 0
        self._done = on_done
        if not self._paced():
            self._target, self._shown = text, len(text)
            self._paint(text)
            self._finish()
            return
        visible = self._target[:self._shown]
        self._shown = len(os.path.commonprefix([visible, text]))
        self._target = text
        if self._shown >= len(text):
            self._paint(text)
            self._finish()
            return
        interval, _chars = self._step()
        self._timer.setInterval(interval)
        if not self._timer.isActive():
            self._timer.start()

    def flush(self) -> None:
        """Show the whole current target immediately (a newer bubble took over)."""
        if self._bubble is not None and self._shown < len(self._target):
            self._shown = len(self._target)
            self._paint(self._target)
        self._finish()

    def _paint(self, text: str) -> bool:
        if self._bubble is None:
            return False
        try:
            self._chat.update_bubble(self._bubble, text)
            return True
        except RuntimeError:
            # The chat was cleared: never resurrect the old briefing.
            self._bubble, self._target, self._shown = None, "", 0
            return False

    def _tick(self) -> None:
        _interval, chars = self._step()
        self._shown = min(len(self._target), self._shown + chars)
        if not self._paint(self._target[:self._shown]) or self._shown >= len(self._target):
            self._finish()

    def _finish(self) -> None:
        self._timer.stop()
        done, self._done = self._done, None
        if done is not None:
            done()
