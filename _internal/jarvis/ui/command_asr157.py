"""Bounded command decoding with all completion callbacks on the Qt thread."""
from __future__ import annotations

from dataclasses import dataclass, field
import queue
import threading
import time
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer


@dataclass
class _Job:
    deadline: float
    result: Callable[[Any], None]
    error: Callable[[str], None]
    abort: Callable[[], None]
    cancelled: threading.Event = field(default_factory=threading.Event)


class CommandAsrRunner(QObject):
    """A single daemon decoder, independent of network/UI QThreadPool jobs.

    Neither worker completion nor cancellation touches widgets. A Qt-owned timer
    drains the mailbox, including the timeout path. A timed-out native call cannot
    create an unlimited sequence of stuck Python threads on subsequent utterances.
    """

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._mailbox: queue.Queue = queue.Queue(maxsize=2)
        self._job: _Job | None = None
        self._thread: threading.Thread | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._poll)

    def start(self, function, *, result, error, timeout: float, abort) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self.cancel()
        while not self._mailbox.empty():
            self._mailbox.get_nowait()
        job = _Job(time.monotonic() + max(0.05, float(timeout)), result, error, abort)
        self._job = job

        def run():
            try:
                value = function(job.cancelled)
                item = (job, False, value)
            except BaseException:
                item = (job, True, traceback.format_exc())
            if not job.cancelled.is_set():
                try:
                    self._mailbox.put_nowait(item)
                except queue.Full:
                    pass

        self._thread = threading.Thread(target=run, name="NeoJarvis-CommandASR", daemon=True)
        self._thread.start()
        self._timer.start()
        return True

    def cancel(self) -> None:
        job, self._job = self._job, None
        self._timer.stop()
        if job is not None:
            job.cancelled.set()
            job.abort()

    def _poll(self) -> None:
        job = self._job
        if job is None:
            self._timer.stop()
            return
        try:
            completed, failed, value = self._mailbox.get_nowait()
        except queue.Empty:
            if time.monotonic() >= job.deadline:
                self.cancel()
                job.error("CommandRecognitionTimeout")
            return
        if completed is not job or job.cancelled.is_set():
            return
        self._job = None
        self._timer.stop()
        (job.error if failed else job.result)(value)
