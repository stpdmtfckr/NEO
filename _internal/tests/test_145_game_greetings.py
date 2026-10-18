from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import psutil

from jarvis.gaming.memory import GamingContextTracker
from jarvis.language import game_started_message


class Config:
    def __init__(self, root, **values):
        self.paths = SimpleNamespace(root=root, cache=root / "cache")
        self.values = {"app.language": "ru", **values}

    def get(self, key, default=None):
        return self.values.get(key, default)


def snapshot(pid=101, started=100.0, name="Game Мир: Don't Stop"):
    return {"pid": pid, "session_started_at": started, "game_name": name, "game_id": "game", "session_seconds": 5}


def tracker(root, **values):
    notices = []
    memory = Mock()
    memory.enabled.return_value = False
    obj = GamingContextTracker(Config(root, **values), memory, notice_callback=notices.append)
    obj.web_context = Mock()
    return obj, notices


@pytest.mark.parametrize("code", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_native_varied_greetings_preserve_proper_name(code):
    name = "Game Мир: Don't Stop {hello}"
    values = {game_started_message(name, code, variant=i) for i in range(3)}
    assert len(values) == 3
    assert all(name in text for text in values)
    if code != "ru":
        assert all("Запущена" not in text and "Приятной" not in text for text in values)


def test_verified_session_greets_once_even_if_memory_write_fails(tmp_path, monkeypatch):
    obj, notices = tracker(tmp_path)
    obj.memory.enabled.return_value = True
    obj.memory.record_game_session_event.side_effect = OSError("disk unavailable")
    monkeypatch.setattr("jarvis.gaming.memory.record_active_game", lambda **kw: None)
    for _ in range(30):
        obj._observe_snapshot(snapshot())
    assert len(notices) == 1
    assert "Game Мир: Don't Stop" in notices[0]
    assert obj.web_context.record.call_count == 1


def test_no_notice_for_failed_launch_and_opt_out(tmp_path):
    obj, notices = tracker(tmp_path)
    obj._on_new_game(snapshot(pid=0))
    obj._on_new_game(snapshot(started=0))
    assert notices == []
    obj.config.values["gaming.launch_greetings_enabled"] = False
    obj._on_new_game(snapshot())
    assert notices == []


def test_end_and_relaunch_rearm_including_reused_pid(tmp_path, monkeypatch):
    obj, notices = tracker(tmp_path)
    monkeypatch.setattr("jarvis.gaming.memory.record_active_game", lambda **kw: None)
    clear = Mock()
    monkeypatch.setattr("jarvis.gaming.memory.clear_active_game", clear)
    obj._observe_snapshot(snapshot())
    # PID still exists, but belongs to a later process: old session has ended.
    process = SimpleNamespace(is_running=lambda: True, create_time=lambda: 200.0)
    monkeypatch.setattr("jarvis.gaming.memory.psutil.Process", lambda pid: process)
    obj._finish_if_dead()
    assert obj._current is None
    clear.assert_called_once_with(pid=101)
    obj._observe_snapshot(snapshot(started=200.0))
    obj._observe_snapshot(snapshot(started=200.0))
    assert len(notices) == 2
    assert notices[0] != notices[1]


def test_access_denied_does_not_invent_session_end(tmp_path, monkeypatch):
    obj, _ = tracker(tmp_path)
    obj._current = snapshot()
    def denied(pid):
        raise psutil.AccessDenied(pid)
    monkeypatch.setattr("jarvis.gaming.memory.psutil.Process", denied)
    obj._finish_if_dead()
    assert obj._current is not None


def test_session_notice_memory_is_bounded(tmp_path):
    obj, notices = tracker(tmp_path)
    for i in range(500):
        obj._on_new_game(snapshot(pid=i + 1, started=i + 1))
    assert len(notices) == 500
    assert len(obj._notified_processes) <= 256

