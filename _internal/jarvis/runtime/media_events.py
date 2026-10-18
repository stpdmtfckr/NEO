# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Small in-process event bus for non-invasive media notifications.

Yandex and Discord playback live in background adapters/threads.  They publish a
compact event whenever a *new* track actually starts.  MainWindow drains the
queue on the Qt thread and renders chat bubbles.  This avoids polling browser or
music state and therefore adds almost no idle CPU cost.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import queue
import threading
import time
from typing import Any, Callable
import re
import uuid


@dataclass(frozen=True)
class MediaNowPlayingEvent:
    source: str
    title: str
    artist: str = ""
    track_id: str = ""
    reason: str = "track_started"
    duration_ms: int = 0
    position_ms: int = 0
    playing: bool = True
    requester: str = ""
    queue_position: int = 0
    created_at: float = 0.0
    event_id: str = ""
    playback_generation: int = 0
    announce: bool = True
    playback_event_id: str = ""
    playback_scope: str = ""
    # Shared local-player source epoch (Yandex and YouTube adapters share one
    # player). 0 means "not a local-player commit".
    source_epoch: int = 0

    @property
    def key(self) -> str:
        stable = str(self.track_id or "").strip()
        base = stable or f"{self.title.casefold().strip()}|{self.artist.casefold().strip()}"
        reason = str(self.reason or "track_started").casefold().strip()
        if reason == "queue_added":
            who = str(self.requester or "").casefold().strip()
            return f"{self.source}:queue_added:{base}:{who}:{int(self.queue_position or 0)}"
        return f"{self.source}:{base}"



_MEDIA_TITLE_SENTINELS = {
    "track", "трек", "unknown", "неизвестно", "metadata loading",
    "метаданные загружаются", "метаданные загружаются…",
}

def is_player_facing_media_title(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    low = raw.casefold().strip(" <>\"'«»[]()")
    if low in _MEDIA_TITLE_SENTINELS:
        return False
    if "метаданные загружа" in low or "metadata load" in low:
        return False
    if re.fullmatch(r"[0-9a-f]{24,64}", low):
        return False
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27,}", low):
        return False
    return True

_COMMAND_RESULT_OWNS_NOTICE: ContextVar[bool] = ContextVar("media_command_result_owns_notice", default=False)
_COMMAND_EVENTS: ContextVar[list[MediaNowPlayingEvent] | None] = ContextVar("media_command_events", default=None)


@contextmanager
def capture_media_events():
    """Collect this command's exact committed events for delivery reconciliation."""
    events: list[MediaNowPlayingEvent] = []
    token = _COMMAND_EVENTS.set(events)
    try:
        yield events
    finally:
        _COMMAND_EVENTS.reset(token)


@contextmanager
def command_result_owns_media_notice():
    """The synchronous command answer owns its track announcement.

    This flag follows nested calls on this command's thread only. Automatic
    transitions on the monitor thread remain normal announcements. Player state
    and all subscribers still receive every event regardless of this flag.
    """
    token = _COMMAND_RESULT_OWNS_NOTICE.set(True)
    try:
        yield
    finally:
        _COMMAND_RESULT_OWNS_NOTICE.reset(token)


_EVENTS: queue.Queue[MediaNowPlayingEvent] = queue.Queue(maxsize=128)
_SUBSCRIBERS_LOCK = threading.RLock()
_SUBSCRIBERS: set[Callable[[MediaNowPlayingEvent], None]] = set()
_LATEST_LOCK = threading.RLock()
_LATEST_BY_SOURCE: dict[str, MediaNowPlayingEvent] = {}
# MEDIA185-U15: newest committed source epoch per shared player scope.
_SOURCE_EPOCHS: dict[str, int] = {}


def subscribe_now_playing(callback: Callable[[MediaNowPlayingEvent], None]) -> None:
    if not callable(callback):
        return
    with _SUBSCRIBERS_LOCK:
        _SUBSCRIBERS.add(callback)


def unsubscribe_now_playing(callback: Callable[[MediaNowPlayingEvent], None]) -> None:
    with _SUBSCRIBERS_LOCK:
        _SUBSCRIBERS.discard(callback)



def stale_playback_observation(scope: str, generation: int, *, current_scope: str, current_generation: int) -> bool:
    """Compare known transport order only within one owner/guild source session.

    Scope is owner:guild:source_epoch. Epoch order survives source replacement;
    generations order tracks within that epoch. Unknown/legacy scopes carry no
    inferred ordering, and different owners or guilds must never be compared.
    """
    scope, current_scope = str(scope or ""), str(current_scope or "")
    if scope != current_scope:
        new_parts, old_parts = scope.rsplit(":", 1), current_scope.rsplit(":", 1)
        return bool(len(new_parts) == len(old_parts) == 2
                    and new_parts[0] == old_parts[0]
                    and new_parts[1].isdigit() and old_parts[1].isdigit()
                    and int(new_parts[1]) < int(old_parts[1]))
    return bool(generation and current_generation and generation < current_generation)


def publish_now_playing(
    source: str,
    title: str,
    artist: str = "",
    track_id: str = "",
    *,
    reason: str = "track_started",
    duration_ms: int = 0,
    position_ms: int = 0,
    playing: bool = True,
    requester: str = "",
    queue_position: int = 0,
    event_id: str = "",
    playback_generation: int = 0,
    announce: bool | None = None,
    playback_scope: str = "",
    observation: bool = False,
    source_epoch: int = 0,
    source_scope: str = "",
) -> str:
    title = str(title or "").strip()
    epoch = max(0, int(source_epoch or 0))
    if epoch:
        scope_key = str(source_scope or "local")
        with _LATEST_LOCK:
            if epoch < _SOURCE_EPOCHS.get(scope_key, 0):
                # A delayed announcement of an already replaced source is history,
                # not the truth about what plays now: never publish it.
                return ""
            if len(_SOURCE_EPOCHS) > 64:
                _SOURCE_EPOCHS.clear()
            _SOURCE_EPOCHS[scope_key] = epoch
    if not is_player_facing_media_title(title):
        # A stop/disconnect is state, not a new track. It must invalidate an old
        # title even when the transport no longer has metadata to publish.
        terminal = str(reason or "").casefold() in {"stopped", "paused", "owner_left", "voice_empty", "playback_ended"}
        if playing or not terminal:
            return ""
        title = ""
    event = MediaNowPlayingEvent(
        source=str(source or "media").strip().casefold(),
        title=title,
        artist=str(artist or "").strip(),
        track_id=str(track_id or "").strip(),
        reason=str(reason or "track_started").strip(),
        duration_ms=max(0, int(duration_ms or 0)),
        position_ms=max(0, int(position_ms or 0)),
        playing=bool(playing),
        requester=str(requester or "").strip(),
        queue_position=max(0, int(queue_position or 0)),
        created_at=time.time(),
        event_id=str(event_id or uuid.uuid4().hex),
        playback_generation=max(0, int(playback_generation or 0)),
        playback_scope=str(playback_scope or ""),
        announce=not _COMMAND_RESULT_OWNS_NOTICE.get() if announce is None else bool(announce),
        source_epoch=epoch,
    )
    # Queue/activity notices are transient notifications, not player truth.  Do
    # not let a user adding a future track replace the actual Now Playing state
    # consumed by overlays, Discord observers and Telegram music controls.
    with _LATEST_LOCK:
        current = _LATEST_BY_SOURCE.get(event.source)
        if observation and current is not None:
            same_scope = current.playback_scope == event.playback_scope
            if stale_playback_observation(event.playback_scope, event.playback_generation,
                    current_scope=current.playback_scope, current_generation=current.playback_generation):
                # A slow status probe is not a new physical playback.
                return current.event_id
            if (same_scope and current.key == event.key
                    and current.playback_generation == event.playback_generation):
                # Passive status/broadcast repetition must not orphan an in-flight
                # queue read or its chat bubble. Explicit starts always get a new
                # identity, including repeat-one and the same track after reconnect.
                # State subscribers still receive the observation, while popup
                # consumers announce only a real physical start, not every poll.
                event = replace(event, event_id=current.event_id, announce=False)
        if event.reason.casefold() == "queue_updated":
            # Bind the refresh to this exact playback even when the same song
            # reappears within one Windows clock tick. Wall-clock ordering is
            # not an identity and can also move backwards after a clock sync.
            current = _LATEST_BY_SOURCE.get(event.source)
            if current is not None and current.playing and current.key == event.key:
                event = replace(event, playback_event_id=current.event_id)
        elif event.reason.casefold() not in {"queue_added", "queue_removed", "network_unavailable"}:
            _LATEST_BY_SOURCE[event.source] = event
    captured = _COMMAND_EVENTS.get()
    if captured is not None:
        captured.append(event)
    try:
        _EVENTS.put_nowait(event)
    except queue.Full:
        # Drop the oldest stale event, then preserve the most recent real track.
        try:
            _EVENTS.get_nowait()
        except queue.Empty:
            pass
        try:
            _EVENTS.put_nowait(event)
        except queue.Full:
            pass

    # HOTFIX89: now-playing is a multicast event. The desktop still drains the
    # legacy queue, while Telegram and future transports receive the same event
    # without stealing it from the Qt consumer. Subscribers must remain tiny;
    # Telegram only enqueues the event into its own worker here.
    with _SUBSCRIBERS_LOCK:
        subscribers = tuple(_SUBSCRIBERS)
    for callback in subscribers:
        try:
            callback(event)
        except Exception:
            # Media playback must never fail because a notification transport did.
            continue
    return event.event_id



def latest_now_playing(source: str = "", *, max_age_seconds: float = 3600.0) -> MediaNowPlayingEvent | None:
    """Return the newest player-facing media event without consuming the bus."""
    now = time.time()
    with _LATEST_LOCK:
        if source:
            event = _LATEST_BY_SOURCE.get(str(source).strip().casefold())
            if event and now - float(event.created_at or 0.0) <= max(0.0, float(max_age_seconds)):
                return event
            return None
        events = [e for e in _LATEST_BY_SOURCE.values() if now - float(e.created_at or 0.0) <= max(0.0, float(max_age_seconds))]
    return max(events, key=lambda e: e.created_at, default=None)


def drain_now_playing(limit: int = 32) -> list[MediaNowPlayingEvent]:
    events: list[MediaNowPlayingEvent] = []
    for _ in range(max(1, int(limit))):
        try:
            events.append(_EVENTS.get_nowait())
        except queue.Empty:
            break
    return events
