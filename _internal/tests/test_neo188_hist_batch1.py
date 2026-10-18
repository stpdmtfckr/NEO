"""NEO 188: historical records HIST-001 (Discord play now) and HIST-002 (Telegram state saves)."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.integrations import discord_navigation
from jarvis.integrations.discord_navigation import ReliableDiscordAdapter
from jarvis.integrations.telegram_remote import TelegramRemoteController as TelegramRemoteControl


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(discord_navigation, "publish_now_playing", lambda *a, **k: None)


class _Nav:
    def __init__(self, guard):
        self.config = SimpleNamespace(get=lambda key, default=None: default)
        self.calls, self.guard = [], guard

    def _background_profile(self, server):
        return {"name": server}

    def music_queue_status(self, server=""):
        return {"connected": True, "playing": True}

    def music_queue_add(self, query, server="", *, next_up=False, requester=""):
        self.calls.append(("add", query))
        return {"queued": True, "queue_started_immediately": False, "position": 1, "item": {"title": query}}

    def _play_now_guard188(self, server, ack):
        return self.guard

    def music_skip(self, server="", *, expect_front=None):
        self.calls.append(("skip", expect_front))
        return {"queue_changed": expect_front == "changed"}


def _play(nav):
    return ReliableDiscordAdapter._handle_music_panel_action(nav, {"name": "S"}, "play",
                                                             {"query": "X", "requester": "Ann", "mode": "now"})


def test_hist001_play_now_skips_only_to_its_own_entry():
    nav = _Nav("entry-X")
    result = _play(nav)
    assert nav.calls == [("add", "X"), ("skip", "entry-X")] and result["message"].startswith("Включаю сейчас")


def test_hist001_changed_queue_is_reported_not_skipped_blindly():
    nav = _Nav(False)
    result = _play(nav)
    assert nav.calls == [("add", "X")] and "очередь изменилась" in result["message"]
    nav = _Nav("changed")
    assert "очередь изменилась" in _play(nav)["message"]


def test_hist001_skip_refuses_when_front_is_not_the_expected_entry():
    mine, other = {"title": "X"}, {"title": "B"}
    state = {"items": [other, mine], "current": {"title": "A"}, "manual_paused": True}
    fake = SimpleNamespace(
        _background_profile=lambda server: {"name": "S"}, _local_voice_is_connected=lambda profile: True,
        _profile_key=lambda profile: "k", _queue_state=lambda profile: state, _queue_lock=threading.RLock(),
        _queues={"k": state})
    result = ReliableDiscordAdapter.music_skip(fake, "S", expect_front=mine)
    assert result["skipped"] is False and result["queue_changed"] is True
    assert state["items"] == [other, mine] and state["current"] == {"title": "A"} and state["manual_paused"] is True
    state["items"] = []
    assert ReliableDiscordAdapter.music_skip(fake, "S", expect_front=mine)["queue_changed"] is True
    assert state["current"] == {"title": "A"}                 # an emptied queue never stops the song


def _telegram(tmp_path):
    remote = TelegramRemoteControl.__new__(TelegramRemoteControl)
    remote._state_path = tmp_path / "telegram_state.json"
    remote._last_update_id = 100
    remote._tracked_messages = []
    remote._last_cleanup_at = 0.0
    remote._cleanup_report = {}
    remote._coverage_gap_at = 0.0
    remote._cleanup_retry_at = 0.0
    remote._panel_by_chat = {}
    remote._view_by_chat = {}
    return remote


def test_hist002_saves_are_serialized_and_never_roll_back(tmp_path, monkeypatch):
    remote = _telegram(tmp_path)
    temps = []
    real_write = Path.write_text
    gate = threading.Event()

    def write(self, data, *a, **k):
        temps.append(self.name)
        if '"last_update_id": 100' in data:
            gate.wait(0.3)                      # A=100 is slow …
        return real_write(self, data, *a, **k)
    monkeypatch.setattr(Path, "write_text", write)
    first = threading.Thread(target=remote._save_state)
    first.start()
    remote._last_update_id = 200
    second = threading.Thread(target=remote._save_state)
    second.start()
    gate.set()
    first.join(); second.join()
    saved = json.loads(remote._state_path.read_text(encoding="utf-8"))
    assert saved["last_update_id"] == 200                      # … but cannot land after B=200
    assert len(set(temps)) == len(temps) == 2                   # every write owns its temp file
    assert saved["state_seq"] == 2 and not list(tmp_path.glob("*.tmp"))


def test_hist002_sharing_violation_is_retried_once_then_reported(tmp_path, monkeypatch):
    remote = _telegram(tmp_path)
    import os as _os
    real_replace = _os.replace
    calls = []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst)
    monkeypatch.setattr("jarvis.integrations.telegram_remote.os.replace", flaky)
    assert remote._save_state() is True and len(calls) == 2
    monkeypatch.setattr("jarvis.integrations.telegram_remote.os.replace", lambda s, d: (_ for _ in ()).throw(PermissionError(32, "busy")))
    assert remote._save_state() is False and not list(tmp_path.glob("*.tmp"))


# HIST-003 / HIST-004: low-level STT request honours cancellation before start and before return.
from jarvis.voice import TranscriptionService


class _Proc:
    def __init__(self):
        self.stdin, self.pid, self.lines = self, 1, []

    def write(self, line):
        self.lines.append(line)

    def flush(self):
        pass

    def poll(self):
        return None


def _stt(monkeypatch):
    service = TranscriptionService("test-stt")
    starts, cleanups = [], []
    monkeypatch.setattr(service, "_start_locked", lambda: starts.append(1) or setattr(service, "_process", _Proc()))
    monkeypatch.setattr(service, "_cleanup_locked", lambda force: cleanups.append(force))
    return service, starts, cleanups


def test_hist003_cancelled_request_never_starts_a_worker(monkeypatch):
    service, starts, _ = _stt(monkeypatch)
    request = {"id": "r1", "cancel_serial": service._cancel_serial}
    service._cancel_serial += 1
    with pytest.raises(TimeoutError):
        service._request_once_locked(request, 1.0)
    assert starts == []
    service, starts, _ = _stt(monkeypatch)
    service._closed = True
    with pytest.raises(TimeoutError):
        service._request_once_locked({"id": "r2", "cancel_serial": 0}, 1.0)
    assert starts == []


def test_hist003_worker_prepared_for_a_cancelled_owner_is_released(monkeypatch):
    service, starts, cleanups = _stt(monkeypatch)

    def start_then_cancel():
        starts.append(1)
        service._process = _Proc()
        service._closed = True                       # cancel() arrived while the worker was starting
    monkeypatch.setattr(service, "_start_locked", start_then_cancel)
    with pytest.raises(TimeoutError):
        service._request_once_locked({"id": "r3", "cancel_serial": 0}, 1.0)
    assert starts == [1] and cleanups == [True] and service._process.lines == []


def test_hist004_matching_answer_after_interrupt_is_not_returned(monkeypatch):
    service, _, _ = _stt(monkeypatch)
    request = {"id": "r4", "cancel_serial": service._cancel_serial}

    class Late:
        def get(self, timeout=None):
            service._cancel_serial += 1               # interrupt() while waiting in queue.get
            return {"id": "r4", "ok": True, "text": "старый текст"}
    monkeypatch.setattr(service, "_responses", Late())
    with pytest.raises(TimeoutError):
        service._request_once_locked(request, 1.0)
    service, _, _ = _stt(monkeypatch)
    monkeypatch.setattr(service, "_responses", SimpleNamespace(get=lambda timeout=None: {"id": "r5", "ok": True, "text": "ок"}))
    assert service._request_once_locked({"id": "r5", "cancel_serial": 0}, 1.0) == "ок"


# HIST-005: late partial callbacks of a closed window.
from jarvis.ui.main_window import MainWindow


class _Timer:
    def __init__(self):
        self.started = 0

    def isActive(self):
        return False

    def start(self):
        self.started += 1

    def stop(self):
        pass


def _window(quitting):
    window = SimpleNamespace(_quitting=quitting, _partial_generation=3, _voice_mode="command",
                             _heard_display_text="", _heard_target_text="", _heard_timer=_Timer(),
                             _pending_partial_recording=object(), _partial_worker_active=True, started=[])
    window._set_heard_target = lambda text: MainWindow._set_heard_target(window, text)
    window._start_partial_transcription = lambda: window.started.append(1)
    return window


def test_hist005_partial_callbacks_after_shutdown_do_nothing():
    window = _window(True)
    MainWindow._partial_transcribed(window, "поздний текст", 3)
    MainWindow._partial_finished(window, 3)
    assert window._heard_timer.started == 0 and window.started == [] and window._partial_worker_active is False
    MainWindow._start_partial_transcription(SimpleNamespace(_pending_partial_recording=object(), _voice_mode="command",
                                                            _quitting=True))       # returns before touching the pool
    live = _window(False)
    MainWindow._partial_transcribed(live, "живой текст", 3)
    MainWindow._partial_finished(live, 3)
    assert live._heard_timer.started == 1 and live.started == [1]
