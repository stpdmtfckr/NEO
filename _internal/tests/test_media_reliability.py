"""Real adapter contracts with fake transports; no account, network or audio IO."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
import json
import threading
import time

import pytest

from jarvis.integrations.media_state import PlaybackSessionStore, cache_lock
from jarvis.integrations import yandex_music as ym
from jarvis.integrations.spotify import SpotifyAdapter, SpotifyAPIError
from jarvis.integrations.openweather import OpenWeatherAdapter, GeoPoint
from jarvis.integrations.background.discord_bot import DiscordBackgroundBot
from jarvis.integrations.discord_navigation import ReliableDiscordAdapter
from jarvis.integrations.telegram_remote import TelegramRemoteController
from jarvis.runtime.media_events import publish_now_playing, drain_now_playing


def test_discord_shared_mutation_timeout_never_replays_on_new_host(tmp_path, monkeypatch):
    adapter = ReliableDiscordAdapter(Config(tmp_path))
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        raise TimeoutError("ACK lost after execution")
    monkeypatch.setattr(adapter, "_bus_request", request)
    monkeypatch.setattr(adapter, "_invalidate_remote_cache", lambda *_: None)
    monkeypatch.setattr(adapter, "_remote_host", lambda *_: pytest.fail("Do not re-elect and replay a mutation"))
    with pytest.raises(TimeoutError):
        adapter._remote_request({"name": "Guild"}, {"bot_id": "1"}, "skip")
    assert len(calls) == 1


def test_discord_public_queue_ack_excludes_credentials_paths_and_signed_urls():
    from jarvis.integrations.background.discord_bot import _public_control_data
    data = _public_control_data({
        "title": "Song", "token": "SECRET", "url": "https://private.invalid/stream?token=SECRET",
        "path": "C:\\Users\\Private\\song.mp3", "current": {"artist": "Artist", "password": "SECRET"},
        "history": [{"title": "Song 1", "query": "C:\\Users\\Private\\song.mp3"}],
    })
    serialized = json.dumps(data)
    assert "SECRET" not in serialized and "Users" not in serialized and "https" not in serialized
    assert data["title"] == "Song" and data["current"]["artist"] == "Artist"


def test_spotify_seek_seconds_and_setup_commands_do_not_require_network(tmp_path, monkeypatch):
    from jarvis.integrations import spotify
    from jarvis.ui.desktop_events import drain_desktop_events
    config = Config(tmp_path)
    adapter = SpotifyAdapter(config)
    requests = []
    monkeypatch.setattr(adapter, "_request", lambda *a, **k: requests.append((a, k)) or {})
    monkeypatch.setattr(adapter, "status", lambda **kw: {"verified": True, "state": "playing"})
    monkeypatch.setattr(spotify.time, "sleep", lambda *_: None)
    adapter.execute("seek", seconds=2.5)
    assert requests[-1][1]["params"]["position_ms"] == 2500
    adapter.execute("seek", position_ms=0, seconds=20)
    assert requests[-1][1]["params"]["position_ms"] == 0
    requests.clear()
    drain_desktop_events(128)
    pending = adapter.execute("authorize")
    assert pending["verified"] is False and pending["pending"] is True
    assert drain_desktop_events() == [{"action": "settings.open", "section": "spotify"}]
    monkeypatch.setattr(spotify.webbrowser, "open", lambda url: url == "https://open.spotify.com")
    assert adapter.execute("open")["opened"] is True
    assert requests == []


def test_provider_tool_wrappers_preserve_zero_volume_and_allow_disabled_setup(tmp_path):
    from jarvis.tools.domains.browser_media import register_browser_media_tools
    registered, calls = {}, []
    registry = SimpleNamespace(register=lambda definition: registered.setdefault(definition.name, definition))
    config = Config(tmp_path)
    config.values.update({"integrations.yandex_music.enabled": True, "integrations.spotify.enabled": True})
    def execute(action, **kw):
        calls.append((action, kw))
        return {"verified": True, "volume": kw.get("volume"), "provider": "spotify"}
    adapter = SimpleNamespace(execute=execute)
    register_browser_media_tools(registry, config=config, yandex_adapter=adapter, spotify_adapter=adapter, youtube_adapter=adapter, discord_adapter=adapter)
    for provider in ("spotify", "yandex_music"):
        result = registered[provider].handler({"action": "set_volume", "volume": 0})
        assert result.success and calls[-1][1]["volume"] == 0
        assert "100%" not in result.text and "0%" in result.text
    config.values["integrations.spotify.enabled"] = False
    for action in ("connect", "authorize", "open"):
        assert registered["spotify"].handler({"action": action}).success
        assert calls[-1][0] == action
    assert not registered["spotify"].handler({"action": "next"}).success


class Config:
    def __init__(self, root):
        self.paths = SimpleNamespace(root=root)
        self.values = {}

    def get(self, name, default=None):
        return self.values.get(name, default)


class Track:
    def __init__(self, name):
        self.id = name
        self.title = name
        self.artists = [SimpleNamespace(name="Artist")]
        self.duration_ms = 180000

    def get_download_info(self):
        raise AssertionError("Prepared playback must not ask the network for metadata")


@pytest.fixture
def local_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("NEOJARVIS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(ym.YandexMusicAdapter, "clear_transient_cache_on_startup_once", lambda self: {})
    player = SimpleNamespace(
        set_volume=lambda *_: None,
        play=lambda *_, **__: {"verified": True, "duration_ms": 180000, "state": "playing"},
        status_if_running=lambda: {"state": "playing", "position_ms": 4000, "duration_ms": 180000},
    )
    monkeypatch.setattr(ym, "local_player", lambda: player)
    adapter = ym.YandexMusicAdapter(Config(tmp_path))
    monkeypatch.setattr(adapter, "_cached_candidate_path", lambda candidate: "/prepared.wav")
    monkeypatch.setattr(adapter, "_start_station_monitor", lambda: None)
    monkeypatch.setattr(adapter, "_start_prefetch_window", lambda *_, **__: None)
    monkeypatch.setattr(adapter, "_station_feedback", lambda *_, **__: None)
    for name in "ABCDEFG":
        adapter._prepared_candidates[name] = (time.monotonic(), [{
            "id": name, "title": name, "artist": "Artist", "url": "https://invalid.test/audio",
            "codec": "mp3", "bitrate_kbps": 192, "expected_duration_ms": 180000,
        }])
    return adapter, player


def test_confirmed_previous_five_persist_excluding_current(local_adapter):
    adapter, _ = local_adapter
    for name in "ABCDEF":
        adapter._play_track_object(Track(name), station="finite:test", queue=[], auto_advance=False)
    saved = adapter._session_store.load()
    assert saved["current"]["id"] == "F"
    assert [x["id"] for x in saved["previous"]] == list("ABCDE")
    assert all("url" not in x and "_live_track" not in x for x in saved["previous"])
    restored = ym.YandexMusicAdapter(adapter.config)
    assert [x["id"] for x in restored._history] == list("ABCDE")
    assert restored._current_track_obj is None  # no surprise autoplay


def test_previous_then_next_preserves_forward_track(local_adapter):
    adapter, _ = local_adapter
    for name in "ABC":
        adapter._play_track_object(Track(name), station="finite:test", queue=[], auto_advance=False)
    result = adapter._previous_background()
    assert result["title"] == "B"
    assert ym._track_id(adapter._station_queue[0]) == "C"
    assert [ym._track_id(x) for x in adapter._history] == ["A"]
    result = adapter._next_background()
    assert result["title"] == "C"
    assert [ym._track_id(x) for x in adapter._history] == ["A", "B"]


def test_failed_previous_does_not_pop_history(local_adapter, monkeypatch):
    adapter, player = local_adapter
    for name in "AB":
        adapter._play_track_object(Track(name), station="finite:test", queue=[], auto_advance=False)
    monkeypatch.setattr(player, "play", lambda *_, **__: (_ for _ in ()).throw(RuntimeError("transport failed")))
    with pytest.raises(RuntimeError):
        adapter._previous_background()
    assert [ym._track_id(x) for x in adapter._history] == ["A"]
    assert ym._track_id(adapter._current_track_obj) == "B"


def test_stop_does_not_erase_durable_history(local_adapter):
    adapter, _ = local_adapter
    for name in "AB":
        adapter._play_track_object(Track(name), station="finite:test", queue=[], auto_advance=False)
    adapter._clear_background_sequence(keep_current=False)
    assert [x["id"] for x in adapter._session_store.load()["previous"]] == ["A"]


def test_cache_same_track_single_flight_across_adapters(local_adapter, monkeypatch):
    adapter, _ = local_adapter
    second = ym.YandexMusicAdapter(adapter.config)
    active = 0
    maximum = 0
    def download(_candidate):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        time.sleep(0.025)
        active -= 1
        return "/ready"
    monkeypatch.setattr(adapter, "_cache_candidate_locked", download)
    monkeypatch.setattr(second, "_cache_candidate_locked", download)
    candidate = {"id": "same", "codec": "mp3", "bitrate_kbps": 192}
    threads = [threading.Thread(target=a._cache_candidate, args=(candidate,)) for a in (adapter, second)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert maximum == 1


def test_spotify_zero_is_mute(tmp_path, monkeypatch):
    adapter = SpotifyAdapter(Config(tmp_path))
    calls = []
    monkeypatch.setattr(adapter, "_request", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(adapter, "status", lambda **_: {"volume": 0})
    adapter.execute("set_volume", volume=0)
    assert calls[0][1]["params"]["volume_percent"] == 0


def test_spotify_401_refreshes_once_and_timeout_never_replays(tmp_path, monkeypatch):
    adapter = SpotifyAdapter(Config(tmp_path))
    responses = [SimpleNamespace(status_code=401), SimpleNamespace(status_code=204)]
    requests = []
    def request(*a, **k):
        requests.append(k)
        return responses.pop(0)
    monkeypatch.setattr(adapter.session, "request", request)
    monkeypatch.setattr(adapter, "_token", lambda: "old")
    monkeypatch.setattr(adapter, "_refresh_if_needed", lambda **_: "new")
    adapter._request("POST", "/me/player/next")
    assert len(requests) == 2
    assert requests[-1]["headers"]["Authorization"] == "Bearer new"
    def failed(*a, **k):
        requests.append(k)
        raise TimeoutError("unknown result")
    monkeypatch.setattr(adapter.session, "request", failed)
    with pytest.raises(TimeoutError): adapter._request("POST", "/me/player/next")
    assert len(requests) == 3


def test_weather_units_and_language_are_cache_keys(tmp_path, monkeypatch):
    adapter = OpenWeatherAdapter(Config(tmp_path))
    units = ["metric"]
    lang = ["ru"]
    calls = []
    monkeypatch.setattr(adapter, "_units", lambda: units[0])
    monkeypatch.setattr(adapter, "_lang", lambda: lang[0])
    monkeypatch.setattr(adapter, "_key", lambda: "fake")
    monkeypatch.setattr(adapter, "_get", lambda *a, **k: calls.append(a) or {"main": {"temp": 1}})
    point = GeoPoint("City", "XX", 1, 2)
    adapter._cached_endpoint("current", point)
    adapter._cached_endpoint("current", point)
    units[0] = "imperial"
    adapter._cached_endpoint("current", point)
    lang[0] = "de"
    adapter._cached_endpoint("current", point)
    assert len(calls) == 3


def test_discord_timeout_retry_joins_original_future(tmp_path, monkeypatch):
    bot = DiscordBackgroundBot(Config(tmp_path))
    bot._loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(bot, "ensure_ready", lambda: None)
    submissions = []
    future = Future()
    def submit(coroutine, _loop):
        coroutine.close()
        submissions.append(coroutine)
        return future
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit)
    async def _voice_action(profile, action): return None
    with pytest.raises(TimeoutError):
        bot._submit(_voice_action({"guild_id": "1"}, "next"), timeout=0.001)
    future.set_result({"track_id": "B"})
    result = bot._submit(_voice_action({"guild_id": "1"}, "next"), timeout=0.001)
    assert result["track_id"] == "B"
    assert len(submissions) == 1


def test_telegram_cleanup_deletes_only_confirmed_bot_owned_ids():
    """NEO 188 NEO-TG-001 supersedes the bot-only policy: in the PAIRED private chat the
    owner's own commands are deleted too; in any other chat only NEO's own messages."""
    def controller(allowed):
        remote = object.__new__(TelegramRemoteController)
        remote._lock = threading.RLock()
        remote._cleanup_lock = threading.Lock()
        remote._stop = threading.Event()
        remote._coverage_gap_at = 0.0
        remote._bot_token_cache = ""
        remote._allowed_chat_ids = lambda: set(allowed)
        remote._tracked_messages = [
            {"chat_id": "1", "message_id": 1, "ts": time.time() - 1, "owned": True},
            {"chat_id": "1", "message_id": 2, "ts": time.time() - 1},
            {"chat_id": "1", "message_id": 3, "ts": time.time() - 1, "owned": False},
        ]
        remote._panel_by_chat = {}
        remote._view_by_chat = {}
        remote._save_state = lambda: True
        calls = []
        remote._api = lambda *a, **k: calls.append((a, k))
        return remote, calls

    remote, calls = controller(allowed=())
    remote._cleanup_chat("1", recreate_home=False, aggressive=True)
    assert calls[0][0][1]["message_ids"] == [1]
    assert len(calls) == 1
    assert {x["message_id"] for x in remote._tracked_messages} == {2, 3}
    remote, calls = controller(allowed=("1",))
    remote._cleanup_chat("1", recreate_home=False, aggressive=True)
    assert calls[0][0][1]["message_ids"] == [1, 2, 3] and remote._tracked_messages == []


def test_media_event_identity_distinguishes_repeat_of_same_track():
    drain_now_playing(128)
    first = publish_now_playing("yandex_music", "Example song", track_id="id")
    second = publish_now_playing("yandex_music", "Example song", track_id="id")
    assert first and first != second
    events = drain_now_playing()
    assert events[-2].event_id == first and events[-1].event_id == second


@pytest.fixture
def discord_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(ym.YandexMusicAdapter, "clear_transient_cache_on_startup_once", lambda self: {})
    adapter = ReliableDiscordAdapter(Config(tmp_path))
    profile = {"guild_id": "123", "name": "Test"}
    monkeypatch.setattr(adapter, "_background_profile", lambda *a: profile)
    monkeypatch.setattr(adapter, "_local_voice_is_connected", lambda *a: True)
    monkeypatch.setattr(adapter, "_prefetch_queue_entry", lambda *a: None)
    monkeypatch.setattr(adapter, "_background_for_profile", lambda *a: SimpleNamespace(voice_action=lambda *a: None))
    state = adapter._queue_state(profile)
    def entry(name): return {"track_id": name, "title": name, "query": name, "artist": "Artist"}
    state.update(current=entry("C"), history=[entry("A"), entry("B")], items=[entry("D"), entry("E")])
    def fail(*a, **k): raise RuntimeError("fake media failure")
    monkeypatch.setattr(adapter, "_play_queue_entry", fail)
    return adapter, state, profile


def test_discord_previous_failure_restores_all_queue_state(discord_queue):
    adapter, state, _ = discord_queue
    with pytest.raises(RuntimeError): adapter.music_previous()
    assert state["current"]["track_id"] == "C"
    assert [x["track_id"] for x in state["history"]] == ["A", "B"]
    assert [x["track_id"] for x in state["items"]] == ["D", "E"]


def test_discord_next_failure_never_skips_another_track(discord_queue):
    adapter, state, _ = discord_queue
    with pytest.raises(RuntimeError): adapter.music_skip()
    assert state["current"]["track_id"] == "C"
    assert [x["track_id"] for x in state["history"]] == ["A", "B"]
    assert [x["track_id"] for x in state["items"]] == ["D", "E"]


def test_discord_history_restores_without_autoplay(discord_queue, monkeypatch):
    adapter, state, profile = discord_queue
    state["current"]["_prepared_streams"] = [{"url": "https://invalid.test/secret-signed-url"}]
    adapter._persist_queue(profile)
    restored = ReliableDiscordAdapter(adapter.config)._queue_state(profile)
    assert restored["current"] is None
    assert [x["track_id"] for x in restored["history"]] == ["A", "B"]
    assert [x["track_id"] for x in restored["items"]] == ["D", "E"]
    assert "secret-signed-url" not in next((Path(adapter.config.paths.root) / "media_sessions").glob("*.json")).read_text()


def test_discord_gateway_reconnect_uses_public_bounded_path(tmp_path):
    bot = DiscordBackgroundBot(Config(tmp_path))
    calls, clients = [], []
    class Client:
        def __init__(self):
            clients.append(self)
        async def login(self, token):
            calls.append((token, "login"))
            if len(clients) == 1:
                raise OSError("DNS temporarily unavailable")
        async def connect(self, *, reconnect):
            calls.append(("connect", reconnect))
            bot._closing.set()
        async def close(self):
            bot._network_changed.set()
    asyncio.run(bot._run_gateway(Client, "test-only"))
    assert len(clients) == 2 and clients[0] is not clients[1]
    assert calls == [("test-only", "login"), ("test-only", "login"), ("connect", False)]


def test_discord_voice_task_error_is_observed_not_swallowed(tmp_path):
    bot = DiscordBackgroundBot(Config(tmp_path))
    bot._playback_generation[42] = 2
    error = type("ConnectionClosed", (Exception,), {"code": 1006})()
    observed = []
    task = SimpleNamespace(get_name=lambda: "Voice websocket poller", done=lambda: True,
                           exception=lambda: observed.append(True))
    fallback = []
    loop = SimpleNamespace(default_exception_handler=lambda context: fallback.append(context))
    bot._loop_exception_handler(loop, {"exception": error, "future": task})
    assert observed == [True]
    assert "1006" in bot._playback_errors[42]
    bot._loop_exception_handler(loop, {"exception": ValueError("unexpected")})
    assert len(fallback) == 1


def test_telegram_next_has_one_ack_and_one_result():
    from jarvis.integrations.telegram_remote import _ToolJob
    remote = object.__new__(TelegramRemoteController)
    remote._lock = threading.RLock()
    remote._media_inflight_sources = {}
    remote._media_ack_until = {}
    remote._media_result_event_ids = set()
    remote._stop = threading.Event()
    remote._stop.set()
    sent = []
    remote._send_message = lambda chat, text: sent.append(text)
    remote._notify_desktop = lambda *a: None
    remote._tg_text = lambda text: text
    remote._tg_runtime_text = lambda text: text
    result = SimpleNamespace(success=True, text="Переключил трек: Example song", data={"title": "Example song", "track_id": "b", "media_event_id": "one-operation"})
    remote._tools_for_direct_action = lambda name: SimpleNamespace(execute=lambda *a: result)
    remote._execute_tool_now_blocking(_ToolJob(chat_id="1", tool_name="yandex_music", arguments={"action": "next"}, label="Next"))
    assert len(sent) == 2
    assert sent[0] == "Переключаю…" and "Example song" in sent[1]
    assert "one-operation" in remote._media_result_event_ids


@pytest.mark.parametrize("language", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_time_of_day_greeting_all_languages(tmp_path, language):
    from datetime import datetime
    from jarvis.integrations.daily_briefing import greeting_and_wish, _GREETING
    config = Config(tmp_path)
    config.values["app.language"] = language
    for hour, bucket in [(6, "morning"), (13, "day"), (20, "evening"), (2, "night")]:
        output = greeting_and_wish(config, datetime(2026, 9, 18, hour))
        assert output.startswith(_GREETING[language][bucket])
