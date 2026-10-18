"""Bounded, provider-independent media persistence and background preparation.

Only stable descriptors are persisted: never OAuth tokens or expiring CDN URLs.
Restoring a session does not start playback. No worker exists until first use.
"""
from __future__ import annotations

import hashlib
from functools import wraps
import json
import logging
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import weakref
from typing import Any, Callable

logger = logging.getLogger(__name__)
_STATE_LOCK = threading.RLock()
_CACHE_LOCKS = [threading.RLock() for _ in range(64)]
_PINNED: dict[str, set[str]] = {}


def pin_cache(owner: str, paths: set[str]) -> None:
    with _STATE_LOCK:
        if paths:
            _PINNED[owner] = {os.path.normcase(os.path.abspath(x)) for x in paths if x}
        else:
            _PINNED.pop(owner, None)


def cache_is_pinned(path: str | Path) -> bool:
    key = os.path.normcase(os.path.abspath(path))
    with _STATE_LOCK:
        return any(key in paths for paths in _PINNED.values())


def cache_lock(key: str) -> threading.RLock:
    # Fixed stripes bound memory while serializing the same key across adapters.
    index = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % len(_CACHE_LOCKS)
    return _CACHE_LOCKS[index]


class PlaybackSessionStore:
    def __init__(self, config: Any, route: str):
        self._last_encoded = ""
        try:
            root = Path(config.paths.root)
            key = hashlib.sha256(route.encode("utf-8")).hexdigest()[:20]
            self.path: Path | None = root / "media_sessions" / (key + ".json")
        except (AttributeError, TypeError):
            self.path = None

    def load(self) -> dict[str, Any]:
        with _STATE_LOCK:
            try:
                if self.path is None or self.path.stat().st_size > 512 * 1024:
                    return {}
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) and data.get("schema_version") == 1 else {}
            except (OSError, ValueError):
                return {}

    def save(self, data: dict[str, Any]) -> None:
        if self.path is None:
            return
        stable = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if stable == self._last_encoded:
            return
        payload = {**data, "schema_version": 1, "saved_at": time.time()}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 512 * 1024:
            raise ValueError("Media session exceeds its storage budget")
        with _STATE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".session-", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, self.path)
                self._last_encoded = stable
            finally:
                try:
                    os.unlink(name)
                except FileNotFoundError:
                    pass


class BoundedPrefetch:
    """A process-wide two-worker queue; a stale queue cannot grow thread count."""
    def __init__(self):
        self._jobs: queue.Queue[tuple[str, Callable[[], None]]] = queue.Queue(maxsize=20)
        self._keys: set[str] = set()
        self._lock = threading.Lock()
        self._started = False

    def submit(self, key: str, function: Callable[[], None]) -> bool:
        with self._lock:
            if key in self._keys:
                return True
            if self._jobs.full():
                return False
            self._keys.add(key)
            self._jobs.put_nowait((key, function))
            if not self._started:
                self._started = True
                for index in range(2):
                    threading.Thread(target=self._run, name=f"neo-media-prefetch-{index}", daemon=True).start()
            return True

    def _run(self) -> None:
        while True:
            key, function = self._jobs.get()
            try:
                function()
            except Exception:
                logger.debug("Media preparation failed", exc_info=True)
            finally:
                with self._lock:
                    self._keys.discard(key)
                self._jobs.task_done()


PREFETCH = BoundedPrefetch()


def serialized_transport(function: Callable[..., Any]) -> Callable[..., Any]:
    """Reject overlapping manual transitions without allowing double queue pops."""
    @wraps(function)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        with _STATE_LOCK:
            lock = getattr(self, "_manual_transition_lock", None)
            if lock is None:
                lock = threading.Lock()
                self._manual_transition_lock = lock
        if not lock.acquire(blocking=False):
            raise RuntimeError("Переключение уже выполняется; повторная команда не отправлена")
        try:
            return function(self, *args, **kwargs)
        finally:
            lock.release()
    return wrapped


class MediaRequestSuperseded(RuntimeError):
    """Preparation belongs to an older request; it must never commit playback."""


def transport_checkpoint(owner: Any) -> None:
    local = getattr(owner, "_transport_request_local", None)
    serial = getattr(local, "serial", None)
    if serial is not None and serial != getattr(owner, "_transport_request_serial", 0):
        raise MediaRequestSuperseded("Команда заменена более новым запросом")


def _transport_guard_lock(owner: Any) -> threading.RLock:
    with _STATE_LOCK:
        lock = getattr(owner, "_transport_request_guard", None)
        if lock is None:
            lock = owner._transport_request_guard = threading.RLock()
        return lock


def cancel_transport_requests(owner: Any) -> None:
    """Invalidate a preparing transition, including one still waiting its turn."""
    with _transport_guard_lock(owner):
        owner._transport_request_serial = int(getattr(owner, "_transport_request_serial", 0)) + 1


def latest_transport(function: Callable[..., Any]) -> Callable[..., Any]:
    """One transition at a time; only the latest waiting request may execute.

    No threads/executors are created here. Long preparation must poll the
    checkpoint; a cancelled waiter never consumes a queue entry.
    """
    @wraps(function)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Never hold global cache/session state while taking the adapter queue
        # lock: persistence takes those locks in the opposite order.
        with _transport_guard_lock(self):
            local = getattr(self, "_transport_request_local", None)
            if local is None:
                local = self._transport_request_local = threading.local()
            nested = getattr(local, "serial", None) is not None
            if not nested:
                lock = getattr(self, "_manual_transition_lock", None)
                if lock is None:
                    lock = self._manual_transition_lock = threading.Lock()
                self._transport_request_serial = int(getattr(self, "_transport_request_serial", 0)) + 1
                serial = self._transport_request_serial
                hook = getattr(self, "_supersede_music_request", None)
                if callable(hook):
                    hook()
        if nested:
            transport_checkpoint(self)
            return function(self, *args, **kwargs)
        local.serial = serial
        acquired = False
        try:
            while True:
                transport_checkpoint(self)
                if lock.acquire(timeout=0.025):
                    acquired = True
                    break
            transport_checkpoint(self)
            return function(self, *args, **kwargs)
        except MediaRequestSuperseded:
            return {"verified": False, "cancelled": True, "superseded": True,
                    "non_invasive": True}
        finally:
            local.serial = None
            if acquired:
                lock.release()
    return wrapped


def transport_guard(owner: Any) -> Callable[[], None]:
    """Capture on the requesting thread; invoke safely from the asyncio thread."""
    serial = getattr(getattr(owner, "_transport_request_local", None), "serial", None)
    def check() -> None:
        if serial is not None and serial != getattr(owner, "_transport_request_serial", 0):
            raise MediaRequestSuperseded("Команда заменена более новым запросом")
    return check


class LocalPlaybackSession:
    """RAM-only ownership of one shared local player, independent of adapters.

    A request epoch invalidates preparation; the source owner changes only after
    verified playback. Failed preparation never transfers ownership.
    No callback or adapter lock is acquired while this lock is held.
    """
    def __init__(self):
        self.lock = threading.RLock()
        self.epoch = 0
        self.source_epoch = 0
        self._owner = None
        self._preparing = None
        self._submitted = None
        self.submitted_epoch = 0
        self._unverified_submission = False

    def owner(self):
        with self.lock:
            return self._owner() if self._owner is not None else None

    def preparing_owner(self):
        with self.lock:
            return self._preparing() if self._preparing is not None else None

    def begin(self, owner):
        with self.lock:
            self.epoch += 1
            self._preparing = weakref.ref(owner)
            return self.epoch

    def owned_by(self, owner, *, allow_unclaimed=True):
        with self.lock:
            current = self._owner() if self._owner is not None else None
            if current is owner:
                return True
            return bool(allow_unclaimed and self.source_epoch == 0 and self.submitted_epoch == 0)

    def begin_recovery(self, owner, *, expected_epoch=None):
        """Atomic background admission; never preempt explicit preparation."""
        with self.lock:
            if (not self.owned_by(owner) or self._preparing is not None
                    or expected_epoch is not None and expected_epoch != self.epoch):
                return None
            return self.begin(owner)

    def begin_transport(self, owner, *, expected_epoch=None):
        with self.lock:
            if expected_epoch is not None and expected_epoch != self.epoch:
                return None
            preparing = self._preparing() if self._preparing is not None else None
            submitted = self._submitted() if self._submitted is not None else None
            if not self.owned_by(owner) and preparing is not owner and submitted is not owner:
                return None
            return self.begin(owner)

    def mark_submitted(self, owner, epoch):
        """Called under the native command lane, immediately before play IPC."""
        with self.lock:
            if epoch != self.epoch:
                return False
            self._submitted = weakref.ref(owner)
            self.submitted_epoch = epoch
            self._unverified_submission = True
            return True

    def submitted_owner(self):
        with self.lock:
            return self._submitted() if self._submitted is not None else None

    def needs_source_rebuild(self):
        """An attempted replacement may differ from the last verified source.

        Submission precedes IPC, so even a failed send is conservatively dirty.
        Stop/Pause cannot establish which source loaded; only verified playback
        clears this flag. Never resume uncertain native audio under old metadata.
        """
        with self.lock:
            return self._unverified_submission

    def mark_stopped(self, epoch):
        with self.lock:
            if epoch == self.epoch:
                self._submitted = None
                self.submitted_epoch = 0

    def queue_current(self, owner, epoch):
        with self.lock:
            return epoch == self.epoch and self.owned_by(owner) and self._preparing is None

    def current(self, epoch):
        with self.lock:
            return epoch == self.epoch

    def commit(self, owner, epoch):
        with self.lock:
            if epoch != self.epoch:
                return False
            self._owner = weakref.ref(owner)
            self.source_epoch = epoch
            self._submitted = weakref.ref(owner)
            self.submitted_epoch = epoch
            self._unverified_submission = False
            return True

    def finish(self, epoch):
        with self.lock:
            if epoch == self.epoch:
                self._preparing = None


def local_playback_session(player: Any) -> LocalPlaybackSession:
    """Attach state to the actual player, avoiding cross-profile/global leakage."""
    with _STATE_LOCK:
        session = vars(player).get("_neo_playback_session")
        if not isinstance(session, LocalPlaybackSession):
            session = LocalPlaybackSession()
            player._neo_playback_session = session
        return session
