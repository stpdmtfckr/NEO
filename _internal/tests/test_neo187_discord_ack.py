"""NEO 187: REC-DPN-01 (typed queue-add ACK) and REC-DPN-02 (pending ≠ disconnected)."""
from types import SimpleNamespace

import pytest

from jarvis.integrations import discord_navigation
from jarvis.integrations.background.discord_bot import _public_control_data
from jarvis.integrations.discord_navigation import ReliableDiscordAdapter
from tests.test_media_reliability import Config, Track, local_adapter  # noqa: F401


def test_ack_keeps_typed_queue_fields_and_still_redacts():
    started = _public_control_data({
        "queue_started_immediately": True, "queued": False, "verified": True,
        "item": {"title": "Song C:\\Users\\me\\a.mp3", "artist": "A", "track_id": "t1",
                 "webpage_url": "https://secret.invalid/x?sig=1", "_track_obj": object(), "token": "s3cr3t"},
        "token": "s3cr3t", "stream_url": "https://cdn.invalid/a?sig=2",
    })
    assert started["queue_started_immediately"] is True and started["queued"] is False
    assert started["item"] == {"title": "Song [private source]", "artist": "A", "track_id": "t1"}
    assert "token" not in started and "stream_url" not in started
    queued = _public_control_data({"queued": True, "queue_started_immediately": False, "position": 3,
                                   "item": {"title": "B"}, "next_up": True})
    assert queued == {"queued": True, "queue_started_immediately": False, "position": 3,
                      "item": {"title": "B"}, "next_up": True}


class _Nav:
    def __init__(self, status, ack):
        self.config = SimpleNamespace(get=lambda key, default=None: default)
        self.calls, self._status, self._ack = [], status, ack

    def _background_profile(self, server):
        return {"name": server}

    def music_queue_status(self, server=""):
        return dict(self._status)

    def music_queue_add(self, query, server="", *, next_up=False, requester=""):
        self.calls.append(("add", query, next_up))
        return _public_control_data(dict(self._ack))     # producer → wire → consumer

    def music_skip(self, server=""):
        self.calls.append(("skip",))
        return {"verified": True}


def _play(nav, mode="now"):
    return ReliableDiscordAdapter._handle_music_panel_action(
        nav, {"name": "Server"}, "play", {"query": "song", "requester": "Ann", "mode": mode})


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(discord_navigation, "publish_now_playing", lambda *a, **k: None)


def test_play_now_started_through_wire_does_not_skip():
    nav = _Nav({"connected": True}, {"queue_started_immediately": True, "queued": False, "item": {"title": "song"}})
    result = _play(nav)
    assert nav.calls == [("add", "song", True)]
    assert result["message"].startswith("Включаю сейчас")


def test_play_now_into_busy_queue_still_replaces_current():
    nav = _Nav({"connected": True, "playing": True},
               {"queued": True, "queue_started_immediately": False, "position": 1, "item": {"title": "song"}})
    _play(nav)
    assert nav.calls == [("add", "song", True), ("skip",)]


def test_unknown_ack_never_skips_blindly():
    nav = _Nav({"connected": True, "playing": True}, {"verified": True})
    result = _play(nav)
    assert nav.calls == [("add", "song", True)]
    assert "не получено" in result["message"]


def test_pending_snapshot_with_fresh_connected_transport_is_admitted():
    nav = _Nav({"shared_state_pending": True, "playing": False, "paused": False, "transport_connected": True},
               {"queued": True, "queue_started_immediately": False, "position": 2, "item": {"title": "song"}})
    result = _play(nav, "last")
    assert nav.calls == [("add", "song", False)]
    assert result["message"].startswith("Добавлено в очередь")


def test_pending_snapshot_without_transport_says_updating_not_owner_start():
    nav = _Nav({"shared_state_pending": True, "playing": False, "paused": False}, {})
    with pytest.raises(RuntimeError, match="обновляется"):
        _play(nav)
    assert nav.calls == []


def test_really_disconnected_transport_keeps_owner_start_error():
    nav = _Nav({"connected": False, "playing": False, "paused": False}, {})
    with pytest.raises(RuntimeError, match="Сначала владелец"):
        _play(nav)


def test_starting_snapshot_reports_transport_separately(local_adapter, monkeypatch):
    yandex, _ = local_adapter
    adapter = ReliableDiscordAdapter(yandex.config)
    profile = {"name": "Guild", "guild_id": "1", "discord_user_id": "2"}
    physical = {"connected": True, "playing": True, "playback_generation": 7,
                "current": {"title": "A", "track_id": "a"}}
    monkeypatch.setattr(adapter, "_background_for_profile",
                        lambda *_: SimpleNamespace(playback_status=lambda *_: dict(physical)))
    state = adapter._queue_state(profile)
    state["current"] = {"title": "B", "track_id": "b", "starting": True, "_transport_generation": 2}
    snapshot = adapter._local_queue_snapshot(profile)
    assert snapshot["shared_state_pending"] is True and snapshot["verified"] is False
    assert snapshot["transport_state"] == "pending" and snapshot["transport_connected"] is True
    assert "connected" not in snapshot           # queue metadata is not claimed


class _Cfg:
    def __init__(self, rows, default=''):
        self.values = {'integrations.discord.servers': rows, 'integrations.discord.default_server': default}

    def get(self, key, default=None):
        return self.values.get(key, default)


def _adapter(rows, default=''):
    adapter = ReliableDiscordAdapter.__new__(ReliableDiscordAdapter)
    adapter.config = _Cfg(rows, default)
    adapter.resolve_profile = lambda name: {'name': name}
    return adapter


ROWS = [{'name': 'Alpha', 'guild_id': '101', 'bot_token_encrypted': 'enc-a'},
        {'name': 'Beta', 'guild_id': '202', 'bot_token_encrypted': 'enc-b'}]


def test_known_guild_id_wins_over_conflicting_display_name():                       # DGR-R01 / R06
    adapter = _adapter(ROWS, default='Alpha')
    key = adapter._server_key_for_guild('202', 'Alpha')
    assert key == '202' and adapter._background_profile(key)['guild_id'] == '202'
    assert adapter._profile_for_bus('202', 'Alpha')['bot_token_encrypted'] == 'enc-b'


def test_id_only_and_renamed_or_duplicate_names_keep_identity():                    # DGR-R02..R04
    rows = ROWS + [{'name': 'beta ', 'guild_id': '303', 'bot_token_encrypted': 'enc-c'}]
    adapter = _adapter(rows)
    assert adapter._profile_for_bus('202', '')['guild_id'] == '202'
    assert adapter._profile_for_bus('303', 'Beta')['guild_id'] == '303'
    assert adapter._profile_for_bus('', 'Alpha')['guild_id'] == '101'     # name-only still works


def test_unknown_guild_with_another_guilds_name_fails_closed():                     # DGR-R05
    adapter = _adapter(ROWS)
    with pytest.raises(RuntimeError, match='не настроен'):
        adapter._server_key_for_guild('999', 'Alpha')
    assert adapter._server_key_for_guild('999', 'Gamma') == 'Gamma'


@pytest.fixture
def skip_rig(local_adapter, monkeypatch):
    yandex, _ = local_adapter
    adapter = ReliableDiscordAdapter(yandex.config)
    profile = {'name': 'Guild', 'guild_id': '1', 'discord_user_id': '2'}
    monkeypatch.setattr(adapter, '_background_profile', lambda *_: profile)
    monkeypatch.setattr(adapter, '_local_voice_is_connected', lambda *_: True)
    monkeypatch.setattr(adapter, '_prefetch_queue_entry', lambda *_: None)
    monkeypatch.setattr(adapter, '_prefetch_next_queue_item', lambda *_: None)
    monkeypatch.setattr(adapter, '_broadcast_queue_snapshot', lambda *a, **k: None)
    monkeypatch.setattr(adapter, '_persist_queue', lambda *_: None)
    monkeypatch.setattr(adapter, '_background_for_profile',
                        lambda *_: SimpleNamespace(playback_status=lambda *_: {'connected': True, 'playing': True}))
    state = adapter._queue_state(profile)
    state['current'] = {'title': 'A', 'track_id': 'a', 'query': 'A'}
    state['items'] = [{'title': 'B', 'track_id': 'b', 'query': 'B'}, {'title': 'C', 'track_id': 'c', 'query': 'C'}]
    return adapter, profile, state


def test_late_monitor_failure_keeps_committed_switch(skip_rig, monkeypatch):            # DSP-R01
    adapter, profile, state = skip_rig
    monkeypatch.setattr(adapter, '_play_queue_entry', lambda entry, *a, **k: {'title': entry['title'], 'track_id': entry['track_id'], 'verified': True})

    def monitor(*_):
        raise RuntimeError('owned monitor start failed')
    monkeypatch.setattr(adapter, '_start_queue_monitor', monitor)
    result = adapter.music_skip('Guild')
    assert result['postcommit_degraded'] == ['monitor']
    assert state['current']['track_id'] == 'b'
    assert [row['track_id'] for row in state['items']] == ['c']
    assert [row['track_id'] for row in state['history']] == ['a']


def test_precommit_transport_failure_still_rolls_back(skip_rig, monkeypatch):           # DSP-R02
    adapter, profile, state = skip_rig

    def fail(*_a, **_k):
        raise RuntimeError('transport rejected before start')
    monkeypatch.setattr(adapter, '_play_queue_entry', fail)
    monkeypatch.setattr(adapter, '_start_queue_monitor', lambda *_: None)
    with pytest.raises(RuntimeError, match='очередь и история сохранены'):
        adapter.music_skip('Guild')
    assert state['current']['track_id'] == 'a'
    assert [row['track_id'] for row in state['items']] == ['b', 'c']


def test_deferred_wave_skip_is_pending_not_completed(skip_rig, monkeypatch):            # WAVE_COOLDOWN
    adapter, profile, state = skip_rig
    state['items'] = []
    state['source_mode'] = 'wave'
    state['wave_refill_retry_after'] = 10 ** 12
    monkeypatch.setattr(adapter, '_advance_queue', lambda key: None)        # refill is in its retry pause
    result = adapter.music_skip('Guild')
    assert result['pending'] is True and result['verified'] is False and result['queue_refilling'] is True


def test_finite_empty_stop_failure_is_not_verified(skip_rig, monkeypatch):              # FINITE_EMPTY_CONTROL
    adapter, profile, state = skip_rig
    state['items'] = []

    def bot(*_):
        def voice_action(profile, name):
            raise RuntimeError('stop rejected')
        return SimpleNamespace(voice_action=voice_action, playback_status=lambda *_: {'connected': True})
    monkeypatch.setattr(adapter, '_background_for_profile', bot)
    result = adapter.music_skip('Guild')
    assert result['queue_empty'] is True and result['verified'] is False
