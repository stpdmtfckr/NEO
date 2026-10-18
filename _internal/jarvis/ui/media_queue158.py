"""Read provider queues without doing integration work on the Qt thread."""
from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time
from typing import Callable

from PySide6.QtCore import QObject, QTimer


@dataclass
class _Request:
    identity: tuple
    revision: int
    load: Callable[[], str]


class MediaQueuePreview(QObject):
    """Two bounded daemon lanes, one per supported transport.

    A slow/unreachable integration cannot hold up painting, wake handling or
    the other transport. Repeated events replace the pending request instead of
    creating more threads. Only the Qt-owned mailbox poll invokes the UI callback.
    """

    def __init__(self, parent, ready: Callable[[str, tuple], None]):
        super().__init__(parent)
        self._ready = ready
        self._closed = threading.Event()
        self._mailbox: queue.Queue = queue.Queue(maxsize=4)
        self._running: dict[str, _Request] = {}
        self._wanted: dict[str, _Request] = {}
        self._revision: dict[str, int] = {}
        self._cache: dict[str, tuple] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._poll)

    def invalidate(self, source: str) -> None:
        self._revision[source] = self._revision.get(source, 0) + 1
        self._cache.pop(source, None)

    def is_pending(self, source: str) -> bool:
        """Qt-owner-only state: an admitted read still awaits its completion."""
        return not self._closed.is_set() and source in self._running

    def request(self, source: str, identity: tuple, load: Callable[[], str], *, refresh: bool = False) -> str | None:
        if self._closed.is_set() or source not in {"yandex_music", "discord"}:
            return None
        revision = self._revision.get(source, 0)
        entry = self._cache.get(source)
        if not refresh and entry and entry[:2] == (identity, revision) and time.monotonic() - entry[2] < 3.0:
            return entry[3]
        request = _Request(identity, revision, load)
        self._wanted[source] = request
        if source not in self._running:
            self._start(source, request)
        return entry[3] if entry and entry[:2] == (identity, revision) else None

    def _start(self, source: str, request: _Request) -> None:
        self._running[source] = request
        mailbox, closed = self._mailbox, self._closed

        def run():
            try:
                value = str(request.load())
            except Exception:
                value = None
            if not closed.is_set():
                mailbox.put_nowait((source, request, value))

        threading.Thread(target=run, name="NEO-QueuePreview-" + source, daemon=True).start()
        self._timer.start()

    def _poll(self) -> None:
        if self._closed.is_set():
            return
        for _ in range(4):
            try:
                source, completed, value = self._mailbox.get_nowait()
            except queue.Empty:
                break
            if self._running.get(source) is not completed:
                continue
            self._running.pop(source, None)
            wanted = self._wanted.get(source)
            if (wanted and (wanted.identity, wanted.revision) == (completed.identity, completed.revision)
                    and completed.revision == self._revision.get(source, 0)):
                # Cache failures as well: repainting must not retry a dead
                # integration at frame rate. A later event invalidates the entry.
                self._cache[source] = (completed.identity, completed.revision, time.monotonic(), value)
                # A retained known queue must not conceal a completed failure.
                # None remains a cached failure for the existing retry interval;
                # the Qt owner can now replace its pending display truthfully.
                self._ready(source, completed.identity)
            elif wanted and wanted.revision == self._revision.get(source, 0):
                self._start(source, wanted)
        if not self._running:
            self._timer.stop()

    def close(self) -> None:
        self._closed.set()
        self._timer.stop()
        self._ready = lambda *_: None
        self._wanted.clear()
