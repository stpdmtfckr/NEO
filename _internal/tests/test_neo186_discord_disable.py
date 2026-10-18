"""186: Discord disable contract on every await (inert transports only)."""
from __future__ import annotations

import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest

from jarvis.integrations.background import discord_bot as module
from jarvis.integrations.background import gateway_recovery as recovery
from jarvis.integrations.background.discord_bot import DiscordBackgroundBot
from jarvis.integrations.media_state import MediaRequestSuperseded

FLAGS = ["integrations.discord.enabled", "integrations.discord.background_bot_enabled"]


class Config:
    def __init__(self, root):
        self.values = {key: True for key in FLAGS}
        self.paths = SimpleNamespace(root=root)

    def get(self, key, default=None):
        return self.values.get(key, default)


@pytest.fixture
def bot(tmp_path):
    return DiscordBackgroundBot(Config(tmp_path))


def _off(bot, flag):
    bot.config.values[flag] = False


@pytest.mark.parametrize("flag", FLAGS)
def test_d01_disable_during_readiness_wait_submits_nothing(bot, flag, monkeypatch):
    bot._loop = SimpleNamespace(is_closed=lambda: False)
    monkeypatch.setattr(bot, "ensure_ready", lambda: _off(bot, flag))
    submitted = []
    monkeypatch.setattr(module.asyncio, "run_coroutine_threadsafe", lambda coro, loop: submitted.append(coro))

    async def work():
        return True

    coro = work()
    with pytest.raises(RuntimeError):
        bot._submit(coro)
    assert submitted == []
    assert coro.cr_frame is None  # closed, never scheduled


@pytest.mark.parametrize("flag", FLAGS)
def test_d01_disabled_before_submit_and_reenable_new_generation(bot, flag, monkeypatch):
    first = bot.admission()
    _off(bot, flag)
    assert bot.admission() is None
    bot.config.values[flag] = True
    second = bot.admission()
    assert second is not None and second != first
    with pytest.raises(MediaRequestSuperseded):
        bot._require_admitted(first)
    bot._require_admitted(second)


@pytest.mark.parametrize("flag", FLAGS)
def test_d02_disable_during_login_never_connects(flag):
    events = []
    state = {"enabled": True, "generation": 0}

    class Owner(SimpleNamespace):
        def admission(self):
            return state["generation"] if state["enabled"] else None

    owner = Owner(_closing=threading.Event(), _ready=threading.Event(), _failed=threading.Event(),
                  _lock=threading.RLock(), _client=None, _error="")

    class Client:
        async def login(self, token):
            events.append("login")
            state["enabled"] = False
            state["generation"] += 1

        async def connect(self, *, reconnect):
            events.append("connect")

        async def close(self):
            events.append("close")

    asyncio.run(recovery.run_gateway(owner, Client, "dummy"))
    assert events == ["login", "close"]
    assert owner._gateway_phase == "disabled" and owner._failed.is_set()


def test_d02_enabled_connect_still_happens():
    events = []
    owner = SimpleNamespace(_closing=threading.Event(), _ready=threading.Event(), _failed=threading.Event(),
                            _lock=threading.RLock(), _client=None, _error="")

    class Client:
        async def login(self, token):
            events.append("login")

        async def connect(self, *, reconnect):
            events.append("connect")
            owner._closing.set()

        async def close(self):
            events.append("close")

    asyncio.run(recovery.run_gateway(owner, Client, "dummy"))
    assert events == ["login", "connect", "close"]


@pytest.fixture
def transport(bot, monkeypatch):
    created, cleaned, plays = [], [], []

    class Source:
        def __init__(self, url, **_kwargs):
            self.url = url
            created.append(url)

        def cleanup(self):
            cleaned.append(self.url)

        def read(self):
            return b""

    class Voice:
        active = False

        def is_playing(self):
            return self.active

        def is_paused(self):
            return False

        def play(self, source, *, after):
            self.active = True
            plays.append(source.url)

        def stop(self):
            self.active = False

    voice = Voice()
    guild = SimpleNamespace(id=42, name="Guild", voice_client=voice)
    channel = SimpleNamespace(id=7, name="Music")
    monkeypatch.setitem(sys.modules, "discord", SimpleNamespace(
        FFmpegOpusAudio=Source, FFmpegPCMAudio=Source, PCMVolumeTransformer=lambda source, volume: source))
    monkeypatch.setattr(bot, "_ffmpeg_executables", lambda **_: ["inert-ffmpeg"])
    monkeypatch.setattr(bot, "_managed_local_source", lambda _: "")
    monkeypatch.setattr(module, "clear_media_levels", lambda *args: None)
    return SimpleNamespace(bot=bot, voice=voice, guild=guild, channel=channel,
                           created=created, cleaned=cleaned, plays=plays)


@pytest.mark.parametrize("flag", FLAGS)
def test_d03_disable_at_voice_resolution_creates_no_source(transport, flag, monkeypatch):
    bot = transport.bot

    async def ensure(*_args, **_kwargs):
        _off(bot, flag)
        return transport.guild, transport.channel, transport.voice

    monkeypatch.setattr(bot, "_ensure_voice", ensure)
    with pytest.raises(MediaRequestSuperseded):
        asyncio.run(bot._play({"guild_id": "42"}, "https://example.invalid/a", "A", "", "", fast_start=True))
    assert transport.created == [] and transport.plays == []


@pytest.mark.parametrize("flag", FLAGS)
def test_d03_disable_inside_ensure_voice_stops_before_connect(bot, flag, monkeypatch):
    connects = []
    guild = SimpleNamespace(id=5, name="G", voice_client=None)

    async def resolve_guild(_profile):
        _off(bot, flag)
        return guild

    async def connect(*_args, **_kwargs):
        connects.append(1)

    monkeypatch.setattr(bot, "_resolve_guild", resolve_guild)
    monkeypatch.setattr(bot, "_connect_voice_bounded", connect)
    with pytest.raises(MediaRequestSuperseded):
        asyncio.run(bot._ensure_voice({"guild_id": "5"}))
    assert connects == []


@pytest.mark.parametrize("flag", FLAGS)
def test_d03_disable_during_start_verification_stops_and_is_not_verified(transport, flag, monkeypatch):
    bot = transport.bot

    async def ensure(*_args, **_kwargs):
        return transport.guild, transport.channel, transport.voice

    real_sleep = asyncio.sleep

    async def sleep(delay):
        _off(bot, flag)
        await real_sleep(0)

    monkeypatch.setattr(bot, "_ensure_voice", ensure)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    with pytest.raises(MediaRequestSuperseded):
        asyncio.run(bot._play({"guild_id": "42"}, "https://example.invalid/a", "A", "", "", fast_start=False))
    assert transport.voice.active is False


def test_d03_enabled_play_is_verified(transport, monkeypatch):
    bot = transport.bot

    async def ensure(*_args, **_kwargs):
        return transport.guild, transport.channel, transport.voice

    monkeypatch.setattr(bot, "_ensure_voice", ensure)
    result = asyncio.run(bot._play({"guild_id": "42"}, "https://example.invalid/a", "A", "", "", fast_start=True))
    assert result["verified"] is True and transport.plays == ["https://example.invalid/a"]


class _Channel:
    def __init__(self, fail_first=False):
        self.sent = []
        self.fail_first = fail_first

    async def send(self, text):
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("known pre-dispatch failure")
        self.sent.append(text)
        return SimpleNamespace(id=len(self.sent), edit=self._edit, delete=self._delete)

    async def _edit(self, content):
        self.sent.append(content)

    async def _delete(self):
        pass


def _timer_bot(bot, monkeypatch, channel, on_resolve=None):
    guild = SimpleNamespace(id=9, name="G")

    async def resolve(_profile):
        return guild

    async def music_channel(_guild):
        if on_resolve:
            on_resolve()
        return channel

    async def find(_channel):
        return None

    monkeypatch.setattr(bot, "_resolve_guild", resolve)
    monkeypatch.setattr(bot, "_ensure_music_channel", music_channel)
    monkeypatch.setattr(bot, "_find_timer_dashboard", find)


@pytest.mark.parametrize("flag", FLAGS)
def test_d04_disable_while_resolving_channel_sends_nothing(bot, flag, monkeypatch):
    channel = _Channel()
    _timer_bot(bot, monkeypatch, channel, on_resolve=lambda: _off(bot, flag))
    with pytest.raises(MediaRequestSuperseded):
        asyncio.run(bot._set_timer_summaries({"guild_id": "9"}, ["Tea 05:00"]))
    assert channel.sent == []


def test_d05_failed_send_is_retried_not_cached(bot, monkeypatch):
    channel = _Channel(fail_first=True)
    _timer_bot(bot, monkeypatch, channel)
    assert asyncio.run(bot._set_timer_summaries({"guild_id": "9"}, ["Tea 05:00"])) is False
    assert bot._timer_delivery[9] == "error"
    assert asyncio.run(bot._set_timer_summaries({"guild_id": "9"}, ["Tea 05:00"])) is True
    assert len(channel.sent) == 1 and bot._timer_delivery[9] == "delivered"
    # Delivered: an identical repeat does not duplicate the panel.
    assert asyncio.run(bot._set_timer_summaries({"guild_id": "9"}, ["Tea 05:00"])) is True
    assert len(channel.sent) == 1


def test_d05_empty_rows_without_history_touch_no_channel(bot, monkeypatch):
    touched = []

    async def resolve(_profile):
        return SimpleNamespace(id=3, name="G")

    async def music_channel(_guild):
        touched.append(1)

    monkeypatch.setattr(bot, "_resolve_guild", resolve)
    monkeypatch.setattr(bot, "_ensure_music_channel", music_channel)
    assert asyncio.run(bot._set_timer_summaries({"guild_id": "3"}, [])) is True
    assert touched == []
