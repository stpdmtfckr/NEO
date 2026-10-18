"""Inert Discord ownership tests: no account, process, sound or provider IO."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
import sys

import pytest

from jarvis.integrations.background import discord_bot as module
from jarvis.integrations.background.discord_bot import DiscordBackgroundBot
from jarvis.integrations.media_state import MediaRequestSuperseded


@pytest.fixture
def transport(tmp_path, monkeypatch):
    config = SimpleNamespace(paths=SimpleNamespace(root=tmp_path), get=lambda _key, default=None: default)
    bot = DiscordBackgroundBot(config)
    created, cleaned, stops, plays, failures, levels = [], [], [], [], [], []

    class Source:
        def __init__(self, url, **_kwargs):
            self.url = url
            created.append(url)

        def cleanup(self):
            cleaned.append(self.url)

        def read(self):
            return b""

    class Voice:
        source = None
        active = False
        callback = None

        def is_playing(self):
            return self.active

        def is_paused(self):
            return False

        def play(self, source, *, after):
            self.source, self.callback, self.active = source, after, True
            plays.append(source.url)

        def stop(self):
            stops.append(self.source.url if self.source else "old")
            self.active = False
            if self.callback:
                self.callback(None)

    voice = Voice()
    guild = SimpleNamespace(id=42, name="Guild", voice_client=voice)
    channel = SimpleNamespace(id=7, name="Music")

    async def ensure(*_args, **_kwargs):
        return guild, channel, voice

    monkeypatch.setitem(sys.modules, "discord", SimpleNamespace(
        FFmpegOpusAudio=Source, FFmpegPCMAudio=Source,
        PCMVolumeTransformer=lambda source, volume: source,
    ))
    monkeypatch.setattr(bot, "_ensure_voice", ensure)
    monkeypatch.setattr(bot, "_ffmpeg_executables", lambda **_: ["inert-ffmpeg"])
    monkeypatch.setattr(bot, "_managed_local_source", lambda _: "")
    monkeypatch.setattr(bot, "_mark_ffmpeg_runtime_failure", lambda *args: failures.append(args))
    monkeypatch.setattr(module, "clear_media_levels", lambda *args: levels.append(args))
    return SimpleNamespace(bot=bot, voice=voice, guild=guild, channel=channel, created=created,
                           cleaned=cleaned, stops=stops, plays=plays, failures=failures, levels=levels)


def test_distinct_play_after_timeout_does_not_join_old_result(transport, monkeypatch):
    bot = transport.bot
    bot._loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(bot, "ensure_ready", lambda: None)
    pending = Future()
    completed = Future()
    completed.set_result({"title": "B", "verified": True})
    calls = []

    def submit(coroutine, _loop):
        calls.append(coroutine.cr_frame.f_locals["url"])
        coroutine.close()
        return pending if len(calls) == 1 else completed

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit)
    profile = {"guild_id": "42"}
    with pytest.raises(TimeoutError):
        bot._submit(bot._play(profile, "source-A", "A", "", ""), timeout=0.001)
    assert bot._submit(bot._play(profile, "source-B", "B", "", ""), timeout=0.001)["title"] == "B"
    pending.set_result({"title": "A", "verified": True})
    assert bot._submit(bot._play(profile, "source-A", "A", "", ""), timeout=0.001)["title"] == "A"
    assert calls == ["source-A", "source-B"]


def test_new_same_url_intent_has_distinct_pending_identity(transport, monkeypatch):
    bot = transport.bot
    bot._loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(bot, "ensure_ready", lambda: None)
    pending = Future()
    completed = Future()
    completed.set_result({"verified": True})
    calls = []

    def submit(coroutine, _loop):
        calls.append(1)
        coroutine.close()
        return pending if len(calls) == 1 else completed

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit)
    first_guard, second_guard = lambda: None, lambda: None
    with pytest.raises(TimeoutError):
        bot._submit(bot._play({}, "same", "A", "", "", before_start=first_guard), timeout=0.001)
    assert bot._submit(bot._play({}, "same", "A", "", "", before_start=second_guard), timeout=0.001)["verified"]
    assert len(calls) == 2
    pending.set_result({"verified": True})


@pytest.mark.parametrize("phase", ["drain", "verification"])
def test_old_coroutine_cannot_start_or_claim_newer_player(transport, monkeypatch, phase):
    bot, voice = transport.bot, transport.voice
    real_sleep = asyncio.sleep

    async def scenario():
        blocked, release = asyncio.Event(), asyncio.Event()
        if phase == "drain":
            voice.active = True

        async def sleep(delay):
            old = asyncio.current_task().get_name() == "old-play"
            target_delay = 0.006 if phase == "drain" else 0.012
            if old and delay == target_delay and not blocked.is_set():
                blocked.set()
                await release.wait()
                return
            await real_sleep(delay)

        monkeypatch.setattr(module.asyncio, "sleep", sleep)
        older = asyncio.create_task(bot._play({}, "old", "Old", "", "", fast_start=True), name="old-play")
        await asyncio.wait_for(blocked.wait(), timeout=1)
        newer = await bot._play({}, "new", "New", "", "", fast_start=True)
        releases_before = list(transport.stops)
        release.set()
        with pytest.raises(MediaRequestSuperseded):
            await older
        assert newer["verified"] and newer["title"] == "New"
        assert voice.source.url == "new" and voice.active
        assert transport.stops == releases_before
        assert bot._playback_metadata[42]["title"] == "New"
        assert not transport.failures

    asyncio.run(scenario())
    assert transport.plays == (["new"] if phase == "drain" else ["old", "new"])


def test_cancelled_after_ensure_voice_never_mutates_player(transport, monkeypatch):
    current = [True]

    def guard():
        if not current[0]:
            raise MediaRequestSuperseded("new request")

    async def ensure(*_args, **_kwargs):
        current[0] = False
        return transport.guild, transport.channel, transport.voice

    monkeypatch.setattr(transport.bot, "_ensure_voice", ensure)
    with pytest.raises(MediaRequestSuperseded):
        asyncio.run(transport.bot._play({}, "old", "Old", "", "", before_start=guard))
    assert not transport.created and not transport.stops and not transport.plays
    assert not transport.bot._playback_metadata
    assert not transport.levels


def test_cancelled_youtube_download_never_reaches_player(transport, monkeypatch):
    current = [True]

    def guard():
        if not current[0]:
            raise MediaRequestSuperseded("new request")

    def download(_url, *, check_cancelled):
        assert check_cancelled is guard
        current[0] = False
        return {"local_path": "/does-not-need-to-exist"}

    monkeypatch.setattr(transport.bot._youtube_audio, "download_audio", download)
    monkeypatch.setattr(transport.bot, "play_url", lambda *a, **k: pytest.fail("stale download reached playback"))
    with pytest.raises(MediaRequestSuperseded):
        transport.bot.play_youtube_url({}, "https://youtu.be/AbCdEfGhI12", before_start=guard)


def test_stale_finish_callback_preserves_newer_generation_and_file(transport, monkeypatch):
    bot = transport.bot
    bot._playback_generation[42] = 2
    bot._active_local_sources[42] = "/cache/same.mp3"
    bot._playback_metadata[42] = {"title": "New", "playback_generation": 2}
    deleted = []
    monkeypatch.setattr(bot, "_delete_file_with_retries", lambda path: deleted.append(path))
    bot._finish_playback(42, 1, "/cache/same.mp3", RuntimeError("old failure"))
    assert bot._playback_metadata[42]["title"] == "New"
    assert bot._active_local_sources[42] == "/cache/same.mp3"
    assert 42 not in bot._playback_errors and 42 not in bot._last_finished_generation
    assert not deleted and not transport.levels


def test_superseded_handshake_does_not_cleanup_or_retry(transport):
    current, calls = [True], []

    def guard():
        if not current[0]:
            raise MediaRequestSuperseded("new request")

    async def connect(**_kwargs):
        calls.append("connect")
        current[0] = False
        raise OSError("old connection failed")

    async def disconnect(**_kwargs):
        calls.append("disconnect-newer")

    guild = SimpleNamespace(voice_client=SimpleNamespace(disconnect=disconnect))
    target = SimpleNamespace(connect=connect)
    with pytest.raises(MediaRequestSuperseded):
        asyncio.run(transport.bot._connect_voice_bounded(guild, target, before_start=guard))
    assert calls == ["connect"]


def test_cancelled_channel_resolution_never_moves_voice(transport, monkeypatch):
    bot = transport.bot
    current, calls = [True], []

    def guard():
        if not current[0]:
            raise MediaRequestSuperseded("new request")

    async def guild(_profile):
        return transport.guild

    async def resolve(*_args):
        current[0] = False
        return transport.channel

    async def move(_target):
        calls.append("moved")

    transport.voice.is_connected = lambda: True
    transport.voice.channel = SimpleNamespace(id=99)
    transport.voice.move_to = move
    monkeypatch.setattr(bot, "_resolve_guild", guild)
    monkeypatch.setattr(bot, "_resolve_voice", resolve)
    with pytest.raises(MediaRequestSuperseded):
        asyncio.run(DiscordBackgroundBot._ensure_voice(bot, {}, before_start=guard))
    assert not calls


def test_cleanup_rechecks_path_acquired_by_newer_player(transport, monkeypatch, tmp_path):
    path = str(tmp_path / "reused.mp3")
    removed = []
    monkeypatch.setattr(module.time, "sleep", lambda _: transport.bot._active_local_sources.update({42: path}))
    monkeypatch.setattr(module.os.path, "exists", lambda _: True)
    monkeypatch.setattr(module.os, "remove", lambda path: removed.append(path))
    transport.bot._delete_file_with_retries(path)
    assert removed == []


def test_cancelled_source_is_cleaned_without_ffmpeg_retry(transport, monkeypatch):
    current = [True]
    original = sys.modules["discord"].FFmpegPCMAudio

    class CancelDuringSource(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            current[0] = False

    def guard():
        if not current[0]:
            raise MediaRequestSuperseded("new request")

    monkeypatch.setattr(sys.modules["discord"], "FFmpegPCMAudio", CancelDuringSource)
    with pytest.raises(MediaRequestSuperseded):
        asyncio.run(transport.bot._play({}, "old", "Old", "", "", before_start=guard))
    assert transport.created == ["old"] and transport.cleaned == ["old"]
    assert not transport.plays and not transport.failures


def test_many_superseded_terminal_plays_do_not_accumulate_or_replay(transport, monkeypatch):
    bot = transport.bot
    bot._loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(bot, "ensure_ready", lambda: None)
    current, futures = [0], []

    def submit(coroutine, _loop):
        coroutine.close()
        future = Future()
        futures.append(future)
        return future

    def guard_for(serial):
        def guard():
            if serial != current[0]:
                raise MediaRequestSuperseded("new request")
        return guard

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit)
    oldest_guard = guard_for(0)
    for serial in range(128):
        current[0] = serial
        guard = oldest_guard if serial == 0 else guard_for(serial)
        with pytest.raises(TimeoutError):
            bot._submit(bot._play({}, str(serial), "Track", "", "", before_start=guard), timeout=0.00001)
        futures[-1].set_result({"verified": True})
        assert len(bot._pending_operations) <= 1
    assert len(futures) == 128
    with pytest.raises(MediaRequestSuperseded):
        bot._submit(bot._play({}, "0", "Track", "", "", before_start=oldest_guard), timeout=0.00001)
    assert len(futures) == 128
    current[0] = -1
    bot._prune_superseded_play_operations()
    assert not bot._pending_operations


def test_active_pending_cap_preserves_unknown_calls_and_close_cancels(transport, monkeypatch):
    bot = transport.bot
    bot._loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(bot, "ensure_ready", lambda: None)
    futures = []

    def submit(coroutine, _loop):
        coroutine.close()
        future = Future()
        futures.append(future)
        return future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit)
    for serial in range(bot._pending_play_limit):
        with pytest.raises(TimeoutError):
            bot._submit(bot._play({}, str(serial), "Track", "", ""), timeout=0.00001)
    with pytest.raises(RuntimeError, match="слишком много"):
        bot._submit(bot._play({}, "one-too-many", "Track", "", ""), timeout=0.00001)
    assert len(futures) == bot._pending_play_limit
    assert all(not future.done() for future in futures)
    assert len(bot._pending_operations) == bot._pending_play_limit
    bot._loop = None
    assert bot.close()
    assert all(future.cancelled() for future in futures)
    assert not bot._pending_operations


def test_legacy_terminal_retention_cap_rejects_without_forgetting_retry(transport, monkeypatch):
    bot = transport.bot
    bot._loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(bot, "ensure_ready", lambda: None)
    futures = []

    def submit(coroutine, _loop):
        coroutine.close()
        future = Future()
        futures.append(future)
        return future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit)
    for serial in range(bot._terminal_play_limit):
        with pytest.raises(TimeoutError):
            bot._submit(bot._play({}, str(serial), "Track", "", ""), timeout=0.00001)
        futures[-1].set_result({"track": str(serial)})
    with pytest.raises(RuntimeError, match="слишком много"):
        bot._submit(bot._play({}, "new", "Track", "", ""), timeout=0.00001)
    assert len(bot._pending_operations) == bot._terminal_play_limit
    assert bot._submit(bot._play({}, "0", "Track", "", ""), timeout=0.00001) == {"track": "0"}
    assert len(futures) == bot._terminal_play_limit


@pytest.mark.parametrize("phase", ["ensure", "drain", "verification"])
def test_close_rejects_late_play_after_await_without_guard(transport, monkeypatch, phase):
    bot, voice = transport.bot, transport.voice
    real_sleep = asyncio.sleep

    async def scenario():
        blocked, release = asyncio.Event(), asyncio.Event()
        if phase == "drain":
            voice.active = True

        async def ensure(*_args, **_kwargs):
            if phase == "ensure":
                blocked.set()
                await release.wait()
            return transport.guild, transport.channel, voice

        async def sleep(delay):
            target_delay = 0.006 if phase == "drain" else 0.012
            if phase != "ensure" and delay == target_delay and not blocked.is_set():
                blocked.set()
                await release.wait()
                return
            await real_sleep(delay)

        monkeypatch.setattr(bot, "_ensure_voice", ensure)
        monkeypatch.setattr(module.asyncio, "sleep", sleep)
        playing = asyncio.create_task(bot._play({}, "old", "Old", "", "", fast_start=True))
        await asyncio.wait_for(blocked.wait(), timeout=1)
        assert bot.close()
        created, played, stops = list(transport.created), list(transport.plays), list(transport.stops)
        release.set()
        with pytest.raises(MediaRequestSuperseded):
            await playing
        assert transport.created == created and transport.plays == played and transport.stops == stops
        assert not transport.failures

    asyncio.run(scenario())


def test_public_control_ack_preserves_truthful_cancellation():
    result = module._public_control_data({
        "verified": False, "cancelled": True, "superseded": True,
        "token": "private", "url": "https://private.invalid/audio",
    })
    assert result == {"verified": False, "cancelled": True, "superseded": True}
