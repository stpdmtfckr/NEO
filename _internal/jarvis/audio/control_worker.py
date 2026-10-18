# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Serialized audio-endpoint control executor.

Windows Core Audio / COM device control is intentionally owned by one daemon
thread.  Telegram, the desktop UI and agent tools can call audio controls from
unrelated worker threads; letting each of them create/destroy COM endpoint
wrappers concurrently is unnecessary and has historically made native teardown
fragile.  This executor gives mute/volume/default-device operations one stable
thread/COM apartment and returns only plain Python values to callers.
"""

from dataclasses import dataclass
import atexit
import gc
import os
import queue
import threading
from typing import Any, Callable, TypeVar

T = TypeVar("T")


@dataclass
class _Job:
    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    done: threading.Event
    result: Any = None
    error: BaseException | None = None
    started: bool = False
    abandoned: bool = False


class AudioJobOutcomeUnknown(TimeoutError):
    """HIST-054: the wait ended while the native job was already running.

    The operation may still complete later, so it is neither «done» nor «safely
    not done»: callers must not replay it blindly."""


class AudioControlExecutor:
    def __init__(self) -> None:
        self._queue: queue.Queue[_Job | None] = queue.Queue(maxsize=64)
        self._closing = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="jarvis-audio-control",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        # The native provider lazily initializes COM once on this owner thread.
        # Do not initialize a second time here: CoInitialize is reference-counted
        # and an unmatched extra call makes teardown unnecessarily fragile.
        try:
            while not self._closing.is_set():
                job = self._queue.get()
                if job is None:
                    break
                if job.abandoned:
                    job.done.set()                 # its caller timed out before it started: never run it
                    continue
                job.started = True
                try:
                    job.result = job.fn(*job.args, **job.kwargs)
                except BaseException as exc:  # propagate to the original caller
                    job.error = exc
                finally:
                    # comtypes wrappers may form tiny *new* cycles. Collect only
                    # generation 0 while still inside the COM-owning thread so
                    # their Release() is not deferred to an unrelated gaming/Qt
                    # worker. Avoid a full-process collection here: arbitrary Qt
                    # cycles also have thread-sensitive finalizers.
                    if os.name == "nt":
                        try:
                            gc.collect(0)
                        except Exception:
                            pass
                    job.done.set()
        finally:
            if os.name == "nt":
                try:
                    from jarvis.audio.providers import windows_core_audio as _core_audio
                    if bool(getattr(_core_audio._COM_THREAD_STATE, "initialized", False)):
                        # Jobs already collect generation 0 before returning, so
                        # a final young-generation pass is enough and does not
                        # drag unrelated UI cycles onto this worker thread.
                        try:
                            gc.collect(0)
                        except Exception:
                            pass
                        from comtypes import CoUninitialize
                        CoUninitialize()
                        _core_audio._COM_THREAD_STATE.initialized = False
                except Exception:
                    pass

    def submit(self, fn: Callable[..., T], *args: Any, timeout: float = 8.0, **kwargs: Any) -> T:
        if self._closing.is_set():
            raise RuntimeError("Audio control executor is shutting down")
        # Re-entrant use from the owner thread must execute directly or it would
        # deadlock waiting on its own queue.
        if threading.current_thread() is self._thread:
            return fn(*args, **kwargs)
        job = _Job(fn=fn, args=tuple(args), kwargs=dict(kwargs), done=threading.Event())
        try:
            self._queue.put(job, timeout=min(0.5, max(0.05, float(timeout) * 0.1)))
        except queue.Full as exc:
            raise RuntimeError("Audio control queue is saturated") from exc
        if not job.done.wait(max(0.2, float(timeout))):
            job.abandoned = True
            if job.started and not job.done.is_set():
                raise AudioJobOutcomeUnknown("Windows audio control operation timed out; its outcome is unknown")
            if not job.done.is_set():
                raise TimeoutError("Windows audio control operation timed out before it started; nothing was changed")
        if job.error is not None:
            raise job.error
        return job.result

    def close(self, timeout: float = 1.0) -> bool:
        if not self._closing.is_set():
            self._closing.set()
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                # The worker is already draining bounded jobs; it also observes the
                # closing flag and will exit without accepting new submissions.
                pass
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(max(0.05, float(timeout)))
        return not self._thread.is_alive()


_GLOBAL: AudioControlExecutor | None = None
_GLOBAL_LOCK = threading.Lock()


def audio_control_executor() -> AudioControlExecutor:
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            _GLOBAL = AudioControlExecutor()
        return _GLOBAL


def shutdown_audio_control_executor(timeout: float = 2.0) -> bool:
    """Stop the COM owner before interpreter teardown without creating it."""
    global _GLOBAL
    with _GLOBAL_LOCK:
        worker = _GLOBAL
    if worker is None:
        return True
    try:
        clean = bool(worker.close(timeout=max(0.05, float(timeout))))
    except Exception:
        clean = False
    if clean:
        with _GLOBAL_LOCK:
            if _GLOBAL is worker:
                _GLOBAL = None
    return clean


def _shutdown() -> None:
    try:
        shutdown_audio_control_executor(timeout=1.0)
    except Exception:
        pass


atexit.register(_shutdown)

__all__ = ["AudioControlExecutor", "audio_control_executor", "shutdown_audio_control_executor"]
