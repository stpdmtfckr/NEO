"""186: PC music queue contracts (inert player, no network, no audio)."""
from __future__ import annotations

import os
import socket
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jarvis.integrations import yandex_music as ym
from jarvis.runtime import media_activity as ma
from jarvis.runtime import media_events
from jarvis.runtime.media_notice156 import queue_labels, queue_notice
from jarvis.testing.isolation import external_io_blocked


class Config:
    def __init__(self, root):
        self.paths = SimpleNamespace(root=root)
        self.values = {"integrations.yandex_music.enabled": True}
        self.data = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def save(self, data):
        self.data = data


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Player:
    """Timeline driven by a fake clock; never decodes or plays a byte."""

    def __init__(self, clock, duration_ms=180000, timeline=True):
        self.clock = clock
        self.duration_ms = duration_ms
        self.timeline = timeline
        self.url = ""
        self.title = ""
        self.started_at = 0.0
        self.starts = []
        self.paused = False

    def status_if_running(self):
        if not self.url:
            return {"state": "stopped", "media_status": "no_media", "url": "", "position_ms": 0, "duration_ms": 0}
        elapsed = int((self.clock.t - self.started_at) * 1000)
        if self.paused:
            return {"state": "paused", "media_status": "buffered", "url": self.url, "title": self.title,
                    "position_ms": min(elapsed, self.duration_ms), "duration_ms": self.duration_ms}
        if elapsed >= self.duration_ms:
            return {"state": "stopped", "media_status": "endofmedia", "url": self.url, "title": self.title,
                    "position_ms": self.duration_ms, "duration_ms": self.duration_ms}
        return {"state": "playing", "media_status": "buffered", "url": self.url, "title": self.title,
                "position_ms": max(0, elapsed), "duration_ms": self.duration_ms}

    status = status_if_running

    def set_volume(self, value):
        return {"verified": True}

    def play(self, url, **kw):
        kw.get("before_start", lambda: None)()
        self.url, self.title = url, kw.get("title")
        self.started_at = self.clock.t
        self.starts.append(kw.get("title"))
        return dict(self.status_if_running(), verified=True, timeline_verified=self.timeline)

    def stop(self):
        self.url = ""
        return {"state": "stopped", "verified": True}


class Stop:
    """Monitor stop event that advances the fake clock instead of sleeping."""

    def __init__(self, clock, limit=1200):
        self.clock, self.limit, self.flag, self.calls = clock, limit, False, 0

    def wait(self, timeout):
        self.calls += 1
        self.clock.t += timeout
        return self.flag or self.calls > self.limit

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def clear(self):
        self.flag = False


@pytest.fixture(autouse=True)
def io_guard(monkeypatch):
    with external_io_blocked(), patch.object(os, "system", side_effect=PermissionError("blocked")), \
            patch.object(socket.socket, "sendto", side_effect=PermissionError("blocked")):
        monkeypatch.setattr(ma, "_STATE", {"owner": "", "state": "", "updated": 0.0})
        monkeypatch.setattr(ma, "_OWNERS", {})
        yield


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(ym.YandexMusicAdapter, "clear_transient_cache_on_startup_once", lambda *a: {})
    clock = Clock()
    player = Player(clock)
    monkeypatch.setattr(ym, "local_player", lambda: player)
    monkeypatch.setattr(ym.time, "monotonic", clock)
    obj = ym.YandexMusicAdapter(Config(tmp_path))
    monkeypatch.setattr(obj, "_after_playback_effect", lambda *a, **kw: None)
    audio = tmp_path / "inert-audio"
    audio.write_bytes(b"INERT; NEVER PLAYED")

    def entry(name):
        return {"id": "youtube:" + name, "track_id": "youtube:" + name, "title": name, "artist": "Synthetic",
                "_source_type": "youtube", "_source_url": "https://www.youtube.com/watch?v=" + name,
                "_youtube_cached_path": str(audio)}

    return SimpleNamespace(obj=obj, player=player, clock=clock, entry=entry)


def _start(rig, names, station="mixed:youtube", seed=None):
    entries = [rig.entry(n) for n in names]
    with rig.obj._playback_lock:
        rig.obj._playlist_repeat_seed = list(seed if seed is not None else entries)
    rig.obj._play_track_object(entries[0], station=station, queue=entries[1:], auto_advance=True)
    return entries


def _titles(values):
    return [ym._track_title(v) for v in values]


def _run_monitor(rig, monkeypatch, limit=1200):
    calls = []
    stop = Stop(rig.clock, limit)

    def auto_next(serial, epoch):
        calls.append(rig.clock.t)
        stop.set()
        return {"verified": True}

    monkeypatch.setattr(rig.obj, "_observed_auto_next", auto_next)
    rig.obj._station_monitor(stop)
    return calls


@pytest.mark.parametrize("timeline", [True, False])
def test_auto01_natural_end_advances_with_or_without_verified_timeline(rig, monkeypatch, timeline):
    rig.player.timeline = timeline
    rig.player.duration_ms = 60000
    _start(rig, ["A", "B", "C"])
    started = rig.clock.t
    calls = _run_monitor(rig, monkeypatch)
    assert len(calls) == 1
    assert calls[0] - started < 62.0


@pytest.mark.parametrize("seconds", [2, 5, 9, 12])
def test_u12_short_item_end_is_accepted_inside_start_guard(rig, monkeypatch, seconds):
    rig.player.duration_ms = seconds * 1000
    _start(rig, ["A", "B"])
    started = rig.clock.t
    calls = _run_monitor(rig, monkeypatch)
    assert len(calls) == 1
    assert calls[0] - started <= seconds + 1.5


def test_u12_stale_eof_of_replaced_source_is_ignored_in_guard(rig, monkeypatch):
    rig.player.duration_ms = 4000
    _start(rig, ["A", "B"])
    # The worker reports an EOF for another URL (the replaced source).
    real = rig.player.status_if_running

    def stale():
        data = real()
        data["url"] = "inert:old-source"
        data.update(state="stopped", media_status="endofmedia", position_ms=4000)
        return data

    rig.player.status_if_running = stale
    calls = _run_monitor(rig, monkeypatch, limit=12)  # ~5.4 s, all inside the 9 s guard
    assert calls == []


def test_u12_pause_is_not_an_end(rig, monkeypatch):
    rig.player.duration_ms = 3000
    _start(rig, ["A", "B"])
    rig.player.paused = True
    assert _run_monitor(rig, monkeypatch, limit=60) == []


def test_u07_shuffle_preview_equals_real_next_sequence(rig):
    obj = rig.obj
    _start(rig, ["A", "B", "C", "D", "E", "F", "G"])
    obj.set_shuffle(True)
    preview = [row["title"] for row in obj.queue_status(limit=5)["queue"]]
    consumed = []
    for _ in range(5):
        consumed.append(obj._next_background()["title"])
    assert consumed == preview


def test_u07_pinned_next_up_keeps_priority_under_shuffle(rig):
    obj = rig.obj
    _start(rig, ["A", "B", "C", "D"])
    obj.set_shuffle(True)
    with obj._playback_lock:
        obj._station_queue.insert(0, rig.entry("PINNED"))
    assert obj.queue_status(limit=1)["queue"][0]["title"] == "PINNED"
    assert obj._next_background()["title"] == "PINNED"


@pytest.mark.parametrize("count", [1, 2, 7])
def test_u05_youtube_repeat_playlist_wraps(rig, count):
    obj = rig.obj
    names = [chr(65 + i) for i in range(count)]
    _start(rig, names)
    obj.set_repeat("playlist")
    played = [names[0]]
    for _ in range(count * 2):
        result = obj._next_background(auto=True)
        assert not result.get("playlist_finished")
        played.append(obj._current_track_meta["title"])
    expected = (names * 3)[:len(played)]
    assert played == expected


def test_u05_repeat_off_finishes_queue(rig):
    obj = rig.obj
    _start(rig, ["A", "B"])
    obj.set_repeat("off")
    obj._next_background(auto=True)
    assert obj._next_background(auto=True).get("playlist_finished")


@pytest.mark.parametrize("count", [3, 7])
def test_u06_playlist_repeat_keeps_order_and_members(rig, count):
    obj = rig.obj
    names = [chr(65 + i) for i in range(count)]
    _start(rig, names, station="playlist:qa")
    obj.set_repeat("playlist")
    played = [names[0]]
    for _ in range(count * 2):
        obj._next_background(auto=True)
        played.append(obj._current_track_meta["title"])
    assert played == (names * 3)[:len(played)]


def test_u06_finite_queue_keeps_declared_repeated_occurrences(rig):
    obj = rig.obj
    entries = [rig.entry(n) for n in ["A", "B", "A", "C"]]
    obj._play_track_object(entries[0], station="playlist:qa", queue=entries[1:], auto_advance=True)
    obj.set_repeat("off")
    played = ["A"]
    for _ in range(3):
        obj._next_background(auto=True)
        played.append(obj._current_track_meta["title"])
    assert played == ["A", "B", "A", "C"]


@pytest.mark.parametrize("mode", ["track", "playlist"])
def test_u09_single_item_repeat_arms_end_monitor(rig, monkeypatch, mode):
    obj = rig.obj
    starts = []
    monkeypatch.setattr(obj, "_start_station_monitor", lambda: starts.append(1))
    entry = rig.entry("Solo")
    obj._play_track_object(entry, station="mixed:youtube", queue=[], auto_advance=False)
    assert obj._auto_advance is False
    obj.set_repeat(mode)
    assert obj._auto_advance is True and starts
    result = obj._next_background(auto=True)
    assert not result.get("playlist_finished")
    assert obj._current_track_meta["title"] == "Solo"
    assert rig.player.starts == ["Solo", "Solo"]


def test_u10_retiring_monitor_does_not_block_new_monitor(rig, monkeypatch):
    obj = rig.obj
    release = threading.Event()
    started = []

    def fake_monitor(stop=None):
        started.append(stop)
        release.wait(2)

    monkeypatch.setattr(obj, "_station_monitor", fake_monitor)
    obj._auto_advance = True
    obj._start_station_monitor()
    old_thread = obj._monitor_thread
    obj._clear_background_sequence(keep_current=True)  # retire: stop set, thread alive
    assert old_thread.is_alive()
    obj._auto_advance = True
    obj._start_station_monitor()
    try:
        assert obj._monitor_thread is not old_thread and len(started) == 2
        assert started[0].is_set() and not started[1].is_set()
    finally:
        release.set()
        old_thread.join(2)
        obj._monitor_thread.join(2)


def test_u14_late_next_keeps_confirmed_shuffle_order(rig):
    obj = rig.obj
    entries = _start(rig, ["A", "B", "C", "D", "E", "F", "G"])
    with obj._playback_lock:
        revision = obj._queue_revision
        snapshot = list(obj._station_queue)
    consumed = snapshot[0]
    # A confirmed edit (reverse order) commits before the late transition.
    with obj._playback_lock:
        obj._station_queue = list(reversed(obj._station_queue))
        obj._bump_queue_revision()
        result = obj._reconciled_queue(snapshot[1:], revision,
                                       lambda live: obj._without_occurrence(live, consumed))
    assert _titles(result) == ["G", "F", "E", "D", "C"]
    assert entries  # silence unused


def test_u14_unchanged_revision_uses_prepared_queue(rig):
    obj = rig.obj
    _start(rig, ["A", "B", "C"])
    with obj._playback_lock:
        revision = obj._queue_revision
        prepared = list(obj._station_queue[1:])
        assert obj._reconciled_queue(prepared, revision, None) == prepared


def test_queue01_trackshort_rows_are_never_shown_as_empty(rig):
    class Short:
        def __init__(self, tid, embedded=None):
            self.id = tid
            self.track = embedded

    class Track:
        def __init__(self, tid, title):
            self.id, self.title = tid, title
            self.artists = [SimpleNamespace(name="Artist " + title)]

    obj = rig.obj
    ym._TRACK_META_CACHE.clear()
    obj._current_track_meta = {"id": "1", "title": "A"}
    obj._station = "likes:library"
    obj._station_queue = [Short("2", Track("2", "B")), Short("3"), Short("4"), Short("5"), Short("6"), Short("7")]
    requested = []
    obj._request_queue_metadata = lambda items: requested.extend(ym._track_id(i) for i in items)
    data = obj.queue_status(limit=5)
    assert data["queue"][0]["title"] == "B"  # embedded TrackShort
    assert data["queue_metadata_pending"] == 5 and requested[:5] == ["3", "4", "5", "6", "7"]
    for language in ("ru", "en", "de", "fr", "es", "it", "pt"):
        labels = queue_labels(data["queue"], language, 5)
        assert len(labels) == 5
        text = queue_notice(labels, language)
        assert queue_notice([], language) not in text
    ym._remember_track_meta("3", "C", "Artist C")
    assert obj.queue_status(limit=5)["queue"][1]["title"] == "C"


def test_u15_late_announcement_of_replaced_source_is_dropped():
    scope = "local:test-u15"
    a = media_events.publish_now_playing("yandex_music", "Song C", track_id="c", source_epoch=3, source_scope=scope)
    assert a
    late = media_events.publish_now_playing("yandex_music", "Song A", track_id="a", source_epoch=1, source_scope=scope)
    assert late == ""
    latest = media_events.latest_now_playing("yandex_music")
    assert latest is not None and latest.title == "Song C"


def _music_tools(rig, spotify_calls):
    from jarvis.tools.domains.browser_media import register_browser_media_tools
    registered = {}
    spotify = SimpleNamespace(execute=lambda action, **kw: spotify_calls.append(action) or {"verified": True, "state": "playing"})
    registry = SimpleNamespace(register=lambda definition: registered.setdefault(definition.name, definition))
    register_browser_media_tools(registry, config=rig.obj.config, yandex_adapter=rig.obj, spotify_adapter=spotify,
                                 youtube_adapter=SimpleNamespace(), discord_adapter=SimpleNamespace())
    return registered


@pytest.mark.parametrize("action", ["shuffle_on", "shuffle_off", "repeat_playlist", "repeat_track", "repeat_off", "next"])
@pytest.mark.parametrize("yandex_enabled", [True, False])
def test_u04_loaded_youtube_owns_commands_regardless_of_default(rig, action, yandex_enabled):
    obj = rig.obj
    obj.config.values.update({"integrations.yandex_music.enabled": yandex_enabled,
                              "integrations.spotify.enabled": True,
                              "integrations.music.active_provider": "spotify"})
    _start(rig, ["A", "B", "C"])
    ma.note_tool_execution("music", {"action": "play"}, True, {"provider": "yandex_music", "content_provider": "youtube"})
    spotify_calls = []
    tools = _music_tools(rig, spotify_calls)
    result = tools["music"].handler({"action": action})
    assert spotify_calls == []
    assert result.data.get("provider") == "yandex_music"
    assert result.success, result.text


def test_u04_explicit_spotify_start_keeps_spotify_for_bare_pause(rig):
    obj = rig.obj
    obj.config.values.update({"integrations.yandex_music.enabled": True,
                              "integrations.spotify.enabled": True,
                              "integrations.music.active_provider": "yandex_music"})
    spotify_calls = []
    tools = _music_tools(rig, spotify_calls)
    ma.note_tool_execution("spotify", {"action": "play_track", "query": "Amber Sky"}, True, {"provider": "spotify"})
    result = tools["music"].handler({"action": "pause"})
    assert spotify_calls == ["pause"] and result.data.get("provider") == "spotify"
