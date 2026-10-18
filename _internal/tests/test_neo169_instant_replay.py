"""NEO 169: NVIDIA Instant Replay on/off and the Settings → Games notices."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.integrations.instant_replay169 import InstantReplay, ReplayWatch


def cfg(**values):
    base = {"app.language": "ru"}
    base.update(values)
    return SimpleNamespace(get=lambda key, default=None: base.get(key, default), data=base)


class FakeNvidia:
    """Observed state that flips when the toggle hotkey is pressed."""

    def __init__(self, state, *, responds=True):
        self.state, self.responds, self.pressed = state, responds, []

    def press(self, combo):
        self.pressed.append(combo)
        if self.responds and self.state is not None:
            self.state = not self.state

    def read(self):
        return self.state


def replay(nvidia, config=None, overlay=True):
    return InstantReplay(config or cfg(), press=nvidia.press, overlay=lambda: overlay, reader=nvidia.read,
                         is_windows=True, sleep=lambda _s: None)


@pytest.mark.parametrize("action,start,end", [("replay_on", False, True), ("replay_off", True, False)])
def test_on_off_presses_toggle_and_verifies(action, start, end):
    nvidia = FakeNvidia(start)
    ok, text, data = replay(nvidia).run(action)
    assert ok and nvidia.pressed == ["alt+shift+f10"] and nvidia.state is end
    assert data["verified"] and data["instant_replay"] is end
    assert ("Включил" if end else "Выключил") in text


def test_already_in_wanted_state_does_not_press():
    nvidia = FakeNvidia(True)
    ok, text, data = replay(nvidia).run("replay_on")
    assert ok and nvidia.pressed == [] and "уже включён" in text


def test_unknown_state_never_presses_the_toggle_for_on_or_off():
    nvidia = FakeNvidia(None)
    ok, text, data = replay(nvidia).run("replay_on")
    assert not ok and nvidia.pressed == [] and "переключи мгновенный повтор" in text


def test_explicit_toggle_with_unknown_state_is_reported_unverified():
    nvidia = FakeNvidia(None)
    ok, text, data = replay(nvidia).run("replay_toggle")
    assert ok and nvidia.pressed == ["alt+shift+f10"] and data["verified"] is False
    assert "Проверить результат не могу" in text


def test_hotkey_without_effect_is_a_failure_not_a_success():
    nvidia = FakeNvidia(False, responds=False)
    ok, text, data = replay(nvidia).run("replay_on")
    assert not ok and nvidia.pressed and "не изменилось" in text


def test_custom_hotkey_bad_hotkey_and_missing_overlay():
    nvidia = FakeNvidia(False)
    replay(nvidia, cfg(**{"gaming.instant_replay.toggle_hotkey": "ctrl+f7"})).run("replay_on")
    assert nvidia.pressed == ["ctrl+f7"]
    nvidia = FakeNvidia(False)
    ok, text, _ = replay(nvidia, cfg(**{"gaming.instant_replay.toggle_hotkey": "alt+nope"})).run("replay_on")
    assert not ok and nvidia.pressed == [] and "alt+nope" in text
    ok, text, _ = replay(nvidia, overlay=False).run("replay_on")
    assert not ok and nvidia.pressed == [] and "оверлея NVIDIA" in text


def test_status_and_languages():
    assert "включён" in replay(FakeNvidia(True)).run("replay_status")[1]
    assert "is off" in replay(FakeNvidia(False), cfg(**{"app.language": "en"})).run("replay_status")[1]
    ok, text, _ = replay(FakeNvidia(None)).run("replay_status")
    assert not ok and "Не могу прочитать" in text


def test_not_windows_is_refused():
    nvidia = FakeNvidia(False)
    engine = InstantReplay(cfg(), press=nvidia.press, reader=nvidia.read, is_windows=False)
    ok, _text, _ = engine.run("replay_on")
    assert not ok and nvidia.pressed == []


def watch(nvidia, **settings):
    return ReplayWatch(cfg(**settings), replay(nvidia))


def test_game_start_warns_only_when_replay_is_known_off():
    assert "мгновенный повтор NVIDIA выключен" in watch(FakeNvidia(False)).game_started("Dota 2")
    assert watch(FakeNvidia(True)).game_started("Dota 2") is None
    assert watch(FakeNvidia(None)).game_started("Dota 2") is None
    assert "Alt+Z" in watch(FakeNvidia(None), **{"gaming.notifications.replay_unknown_warning": True}).game_started("Dota 2")
    assert watch(FakeNvidia(False), **{"gaming.notifications.replay_off_warning": False}).game_started("Dota 2") is None
    assert watch(FakeNvidia(False), **{"gaming.enabled": False}).game_started("Dota 2") is None


def test_changes_during_game_are_reported_once():
    nvidia = FakeNvidia(True)
    w = watch(nvidia)
    w.game_started("Dota 2")
    assert w.poll() is None
    nvidia.state = False
    assert "выключился" in w.poll()
    assert w.poll() is None
    nvidia.state = True
    assert "включился" in w.poll()
    nvidia.state = False
    off = watch(nvidia, **{"gaming.notifications.replay_changes": False})
    off.game_started("Dota 2"); nvidia.state = True
    assert off.poll() is None


class _Memory:
    def enabled(self):
        return False


def tracker(monkeypatch, nvidia, **settings):
    import jarvis.gaming.memory as memory
    monkeypatch.setattr(memory, "record_active_game", lambda **kw: None)
    monkeypatch.setattr(memory, "clear_active_game", lambda **kw: None)
    monkeypatch.setattr(memory, "GameWebContextStore", lambda config: SimpleNamespace(record=lambda *a, **k: None))
    notices = []
    STARTED[0] = __import__("time").time() - 3930  # a session of 1 h 5 min, measured from now
    t = memory.GamingContextTracker(cfg(**settings), _Memory(), notice_callback=notices.append)
    t._replay_watch = ReplayWatch(t.config, replay(nvidia))
    return t, notices


STARTED = [0.0]


def snap(fg=False, pid=4242):
    return {"game_name": "Dota 2", "pid": pid, "session_started_at": STARTED[0], "session_seconds": 3900, "foreground_verified": fg}


def test_tracker_launch_greeting_and_replay_warning(monkeypatch):
    t, notices = tracker(monkeypatch, FakeNvidia(False))
    t._observe_snapshot(snap())
    assert len(notices) == 2 and "Dota 2" in notices[0] and "мгновенный повтор" in notices[1]
    t._observe_snapshot(snap())
    assert len(notices) == 2


def test_tracker_launch_greeting_can_be_turned_off(monkeypatch):
    t, notices = tracker(monkeypatch, FakeNvidia(True), **{"gaming.launch_greetings_enabled": False})
    t._observe_snapshot(snap())
    assert notices == []


def test_tracker_session_start_and_end_notices(monkeypatch):
    import jarvis.gaming.memory as memory
    settings = {"gaming.launch_greetings_enabled": False, "gaming.notifications.session_start": True,
                "gaming.notifications.session_end": True}
    t, notices = tracker(monkeypatch, FakeNvidia(True), **settings)
    t._observe_snapshot(snap(fg=False))
    assert notices == []
    t._observe_snapshot(snap(fg=True))
    t._observe_snapshot(snap(fg=True))
    assert notices == ["🎮 Игровая сессия началась: Dota 2. Таймер идёт."]
    monkeypatch.setattr(memory.psutil, "Process", lambda pid: (_ for _ in ()).throw(memory.psutil.NoSuchProcess(pid)))
    t._finish_if_dead()
    assert notices[-1].startswith("🎮 Игровая сессия Dota 2 завершена: 1 ч 5 мин")


def test_session_notices_are_off_by_default(monkeypatch):
    import jarvis.gaming.memory as memory
    t, notices = tracker(monkeypatch, FakeNvidia(True), **{"gaming.launch_greetings_enabled": False})
    t._observe_snapshot(snap(fg=True))
    monkeypatch.setattr(memory.psutil, "Process", lambda pid: (_ for _ in ()).throw(memory.psutil.NoSuchProcess(pid)))
    t._finish_if_dead()
    assert notices == []


def test_defaults_and_migration_keep_user_values():
    import json
    from pathlib import Path
    from jarvis.runtime import settings169
    defaults = json.loads((Path(__file__).parents[1] / "config/default_settings.json").read_text(encoding="utf-8"))
    assert defaults["gaming"]["notifications"]["replay_off_warning"] is True
    assert defaults["gaming"]["instant_replay"]["toggle_hotkey"] == "alt+shift+f10"
    old = {"gaming": {"enabled": True, "launch_greetings_enabled": False, "match_ready": {"enabled": False}}}
    settings169.migrate(old, 168)
    assert old["gaming"]["launch_greetings_enabled"] is False and old["gaming"]["match_ready"]["enabled"] is False
    assert old["gaming"]["notifications"]["session_start"] is False


def test_capture_tool_routes_replay_actions(monkeypatch):
    import jarvis.integrations.instant_replay169 as module
    from jarvis.tools.integrations import register_integrations
    nvidia = FakeNvidia(False)
    monkeypatch.setattr(module, "_SHARED", replay(nvidia))
    config = cfg()
    module._SHARED.config = config
    tools = {}
    register_integrations(SimpleNamespace(config=config, database=None, register=lambda t: tools.__setitem__(t.name, t)))
    capture = tools["capture"]
    assert "replay_on" in capture.parameters["properties"]["action"]["enum"]
    result = capture.handler({"action": "replay_on"})
    assert result.success and nvidia.state is True and nvidia.pressed == ["alt+shift+f10"]


@pytest.mark.parametrize("code,phrases", [
    ("ru", {"включи мгновенный повтор": "replay_on", "выключи мгновенный повтор": "replay_off",
            "переключи мгновенный повтор": "replay_toggle", "мгновенный повтор включён": "replay_status",
            "сохрани мгновенный повтор": "clip"}),
    ("en", {"turn on instant replay": "replay_on", "disable instant replay": "replay_off", "is instant replay on": "replay_status"}),
    ("de", {"instant replay einschalten": "replay_on", "schalte instant replay aus": "replay_off"}),
    ("fr", {"active la relecture instantanée": "replay_on"}),
    ("es", {"desactiva la repetición instantánea": "replay_off"}),
    ("it", {"attiva il replay istantaneo": "replay_on"}),
    ("pt", {"desliga a repetição instantânea": "replay_off"}),
])
def test_phrases_route_to_capture(code, phrases):
    import json
    from pathlib import Path
    from jarvis.runtime.canonical_commands import parse_canonical_command
    catalogue = json.loads((Path(__file__).parent / "fixtures/command_catalogue.json").read_text(encoding="utf-8"))
    schemas = {row["tool"]: row["schema"] for row in catalogue["tools"]}
    for phrase, action in phrases.items():
        parsed = parse_canonical_command(phrase, code, schemas.get)
        assert parsed is not None and not parsed.error, (phrase, parsed)
        assert (parsed.tool, parsed.arguments["action"]) == ("capture", action), phrase
