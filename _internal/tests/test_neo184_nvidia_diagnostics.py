"""Bounded source diagnostics with inert adapters only."""
import json
import logging

import pytest

from jarvis.integrations import instant_replay169 as ir, nvidia_state as ns, nvidia_diagnostics184 as diagnostic
from jarvis.testing.isolation import external_io_blocked
from tests.test_neo169_instant_replay import cfg
from tests.test_neo184_nvidia_startup import watcher
from tests.test_neo184_nvidia_reliability import Control


@pytest.fixture(autouse=True)
def blocked():
    with external_io_blocked():
        yield


def rows(caplog):
    return [json.loads(record.getMessage().split(": ", 1)[1]) for record in caplog.records
            if record.name == diagnostic.__name__]


def test_default_reader_preserves_source_reason_without_duplicate_probe(monkeypatch):
    calls = []
    def snapshot():
        calls.append(1)
        return {"backend": "nvidia_app", "instant_replay": None,
                "source": "visible_uia", "reason": "overlay_not_foreground"}
    monkeypatch.setattr(ns, "snapshot", snapshot)
    engine = ir.InstantReplay(cfg(), overlay=lambda: True, is_windows=True)
    assert engine.state() is None
    row = engine.last_observation()
    assert row["backend"] == "nvidia_app" and row["reason"] == "overlay_not_foreground"
    assert row["source"] == "visible_uia" and row["overlay_process_present"] is True
    assert row["overlay_readiness"] == "unverified" and row["application_version"] is None
    for _ in range(20):
        engine.last_observation()
    assert calls == [1]


@pytest.mark.parametrize("state", [True, False, None])
def test_observation_keeps_live_app_source_without_registry_fallback(monkeypatch, state):
    expected = {"backend": "nvidia_app", "instant_replay": state,
                "source": "visible_uia", "reason": "observed" if state is not None else "controls_not_exposed"}
    monkeypatch.setattr(ns, "snapshot", lambda: dict(expected))
    assert ns.replay_observation() == expected


def test_closed_overlay_does_not_infer_replay_state_or_inject_keys(monkeypatch, caplog):
    monkeypatch.setattr(ns, "snapshot", lambda: {"backend": "nvidia_app", "instant_replay": None,
                         "source": "visible_uia", "reason": "overlay_not_foreground"})
    engine = ir.InstantReplay(cfg(), overlay=lambda: True, is_windows=True,
                              press=lambda _key: pytest.fail("unknown must not toggle"))
    with caplog.at_level(logging.INFO):
        assert not engine.run("replay_on")[0]
    row, = rows(caplog)
    assert row["reason"] == "overlay_not_foreground" and row["pressed"] is False


def test_verified_german_vendor_label_requires_visible_owned_exact_toggle():
    # Official 11.0.9.251 osc/assets/i18n/de_DE.json labels both menu/settings
    # InstantReplay as Videobeweis. Text alone still never proves state.
    assert "videobeweis" in ns.REPLAY_LABELS
    for value in (0, 1):
        assert ns.states_from_controls([Control("Videobeweis", value)], 1)["instant_replay"] is bool(value)
    for control in (Control("Videobeweis", 1, pid=2), Control("Videobeweis", 1, visible=False),
                    Control("Videobeweis", 1, enabled=False), Control("Videobeweis", True),
                    Control("Videobeweis", 2), Control("Videobeweis", "1"), Control("Videobeweis Einstellungen", 1)):
        assert ns.states_from_controls([control], 1)["instant_replay"] is None
    assert ns.states_from_controls([Control("Videobeweis", 0), Control("Videobeweis", 1)], 1)["instant_replay"] is None


def test_game_start_timeout_records_are_bounded_not_one_per_poll(caplog):
    fake, _config, _engine, watch, _ready, clock, _launched = watcher(None, True)
    with caplog.at_level(logging.INFO):
        watch.game_started("PRIVATE_SENTINEL")
        for _ in range(30):
            watch.poll()
        assert [row["event"] for row in rows(caplog)] == ["game_start"]
        clock[0] = 61
        for _ in range(10):
            watch.poll()
    assert [row["event"] for row in rows(caplog)] == ["game_start", "terminal"]
    assert rows(caplog)[-1]["outcome"] == "timeout"
    assert not fake.pressed and "PRIVATE_SENTINEL" not in caplog.text


def test_launch_check_terminal_keep_settings_and_presence_distinct(caplog):
    fake, _config, _engine, watch, ready, _clock, launched = watcher(
        **{"gaming.notifications.nvidia_enabled": False})
    with caplog.at_level(logging.INFO):
        assert watch.game_started("PRIVATE_SENTINEL") is None
        for _ in range(10): watch.poll()
        ready[0] = True; fake.state = False
        assert watch.poll() is None
        for _ in range(10): watch.poll()
    output = rows(caplog)
    assert [row["event"] for row in output] == ["game_start", "launch", "check", "terminal"]
    assert output[0]["auto_launch"] is output[0]["auto_enable"] is True
    assert output[0]["overlay_process_present"] is False
    assert output[1]["launch_target"] == "nvidia_app" and output[1]["app_process_present"] is None
    assert output[2]["outcome"] == "overlay_present"
    assert output[-1]["outcome"] == "verified_on" and output[-1]["instant_replay"] is True
    assert len(launched) == len(fake.pressed) == 1


def test_explicit_status_uses_its_single_observation(caplog):
    reads = []
    engine = ir.InstantReplay(cfg(), reader=lambda: (reads.append(1), True)[1],
                              overlay=lambda: True, is_windows=True)
    with caplog.at_level(logging.INFO):
        assert engine.run("replay_status")[0]
    assert reads == [1]
    row, = rows(caplog)
    assert row["event"] == "check" and row["instant_replay"] is True


@pytest.mark.parametrize("gaming,launch,enable", [(True, True, True), (True, True, False),
                                                (True, False, True), (True, False, False), (False, True, True)])
def test_explicit_command_distinguishes_configured_and_effective_flags(gaming, launch, enable, caplog):
    reads = []
    config = cfg(**{"gaming.enabled": gaming, "gaming.instant_replay.auto_launch_app": launch,
                    "gaming.instant_replay.auto_enable": enable})
    engine = ir.InstantReplay(config, reader=lambda: (reads.append(1), True)[1],
                              overlay=lambda: True, is_windows=True)
    with caplog.at_level(logging.INFO):
        assert engine.run("replay_status")[0]
    row, = rows(caplog)
    assert row["gaming_enabled"] is gaming
    assert row["configured_auto_launch"] is launch and row["configured_auto_enable"] is enable
    assert row["auto_launch"] is (gaming and launch) and row["auto_enable"] is (gaming and enable)
    assert reads == [1]


def test_missing_or_unreadable_settings_remain_unknown_in_diagnostic(caplog):
    class BrokenConfig:
        def get(self, *_args):
            raise OSError("PRIVATE_SENTINEL")
    assert all(value is None for value in diagnostic.settings(BrokenConfig()).values())
    with caplog.at_level(logging.INFO):
        diagnostic.emit("check", "observed", {})
    row, = rows(caplog)
    assert all(row[field] is None for field in
               ("auto_launch", "auto_enable", "gaming_enabled", "configured_auto_launch", "configured_auto_enable"))
    assert "PRIVATE_SENTINEL" not in caplog.text


@pytest.mark.parametrize("bad", ["/private/PRIVATE_SENTINEL\nforged=yes", [], {}, True, 42, None])
def test_raw_provider_config_and_exception_content_never_enters_diagnostics(bad, caplog):
    with caplog.at_level(logging.INFO):
        diagnostic.emit("check", bad, {key: bad for key in
            ("backend", "source", "reason", "instant_replay", "overlay_process_present", "launch_target",
             "application_version", "application_version_source", "argv", "token", "user_text")},
            auto_launch=bad, auto_enable=bad, gaming_enabled=bad,
            configured_auto_launch=bad, configured_auto_enable=bad, pressed=bad)
    row, = rows(caplog)
    assert row["application_version"] is None and row["application_version_source"] == "unavailable"
    assert "PRIVATE_SENTINEL" not in caplog.text and "forged=" not in caplog.text
    assert not {"argv", "token", "user_text"} & row.keys()


def test_logger_failure_cannot_interrupt_flow(monkeypatch):
    monkeypatch.setattr(diagnostic.logger, "info", lambda *args: (_ for _ in ()).throw(OSError("PRIVATE_SENTINEL")))
    fake, _config, _engine, watch, _ready, _clock, _launched = watcher(False, True)
    assert "включил" in watch.game_started("Game")
    assert len(fake.pressed) == 1


def test_uncertain_input_diagnostic_does_not_reuse_pre_press_off_state(caplog):
    fake, _config, engine, watch, _ready, _clock, _launched = watcher(False, True)
    def fail(hotkey):
        fake.press(hotkey)
        raise OSError("PRIVATE_SENTINEL")
    engine._press = fail
    with caplog.at_level(logging.DEBUG):
        watch.game_started("Game")
    terminal = [row for row in rows(caplog) if row["event"] == "terminal"]
    assert len(terminal) == 1 and terminal[0]["outcome"] == "input_failed"
    assert terminal[0]["instant_replay"] is None and terminal[0]["pressed"] is None
    assert "PRIVATE_SENTINEL" not in caplog.text
