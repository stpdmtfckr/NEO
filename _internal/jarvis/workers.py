# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import traceback
import weakref
from typing import Any, Callable

from PySide6.QtCore import QCoreApplication, QObject, QRunnable, QThread, Qt, Signal, Slot


class WorkerSignals(QObject):
    result = Signal(object)
    progress = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, parent: QObject | None = None, worker=None) -> None:
        super().__init__(parent)
        self._worker_ref = weakref.ref(worker) if worker is not None else None
        # The runnable wrapper normally dies before the queued cleanup. Keep
        # its inputs under GUI ownership too, so a last bound-window reference
        # is not released by the pool thread. This contains no strong runnable.
        self._job_inputs = ((worker.function, worker.args, worker.kwargs)
                            if parent is not None and worker is not None else None)

    @Slot()
    def dispose_finished(self) -> None:
        """Release completed inputs on the GUI thread, then the signal object.

        Some PySide versions keep invalid QRunnable Python wrappers in their
        pool bookkeeping after deleting the native runnable. Those wrappers
        must not retain audio buffers, job inputs or a bound window method.
        """
        reference, self._worker_ref = self._worker_ref, None
        worker = reference() if reference is not None else None
        if worker is not None:
            worker.function = None
            worker.args = ()
            worker.kwargs = {}
            worker.progress_keyword = None
        self._job_inputs = None
        self.deleteLater()


def _safe_emit(signal: Any, *args: Any) -> None:
    """Ignore late emissions after the owning window has been destroyed."""

    try:
        signal.emit(*args)
    except RuntimeError:
        pass


class FunctionWorker(QRunnable):
    def __init__(
        self,
        function: Callable[..., Any],
        *args: Any,
        progress_keyword: str | None = None,
        **kwargs: Any,
    ):
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.progress_keyword = progress_keyword
        # QThreadPool deletes the runnable immediately after run() returns.
        # Its unparented signal object used to die on that worker thread too,
        # while result/progress callbacks were still queued in the GUI thread.
        # With fast transports (notably pause) that lost answers and could crash
        # Qt while dispatching an already-destroyed lambda receiver. Keep the
        # QObject, not the runnable, alive until its queued callbacks arrive.
        app = QCoreApplication.instance()
        owner = app if app is not None and QThread.currentThread() == app.thread() else None
        self.signals = WorkerSignals(owner, self)
        if owner is not None:
            # Posted after result/error and all progress events. deleteLater
            # then releases the signal object on its owning event-loop thread;
            # completed jobs do not accumulate application children.
            self.signals.finished.connect(self.signals.dispose_finished, Qt.QueuedConnection)

    @Slot()
    def run(self) -> None:
        kwargs = {}
        result = None
        try:
            kwargs = dict(self.kwargs)
            if self.progress_keyword:
                kwargs[self.progress_keyword] = lambda value: _safe_emit(
                    self.signals.progress, value
                )
            result = self.function(*self.args, **kwargs)
            _safe_emit(self.signals.result, result)
        except Exception:
            _safe_emit(self.signals.error, traceback.format_exc())
        finally:
            # Queued GUI cleanup can run as soon as finished is emitted, even
            # before this Python frame has physically returned. Drop copied
            # inputs first while signals still owns their GUI-thread lifetime.
            kwargs = {}
            result = None
            _safe_emit(self.signals.finished)
