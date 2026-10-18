"""Inert regressions for a readiness race and uncertain input notification."""
import pytest

from jarvis.testing.isolation import external_io_blocked
from tests.test_neo184_nvidia_startup import watcher


@pytest.fixture(autouse=True)
def blocked():
    with external_io_blocked():
        yield


@pytest.mark.parametrize("state", [True, False])
@pytest.mark.parametrize("notices", [True, False])
def test_pending_launch_wins_when_overlay_disappears_after_state_read(state, notices):
    fake, _config, engine, watch, _ready, _clock, launched = watcher(
        None, None, **{"gaming.instant_replay.auto_enable": False,
                      "gaming.notifications.replay_changes": notices})
    watch.game_started("Game")
    assert not launched
    # Normal state() first sees a process and reads a state; the watcher's
    # subsequent process check sees the overlay disappear. No native calls.
    fake.state = state
    reads = iter([True, False])
    engine._overlay = lambda: next(reads)
    text = watch.poll()
    assert len(launched) == 1
    assert "запросил запуск" in text
    assert not fake.pressed


def test_uncertain_input_failure_does_not_claim_replay_remains_off_or_retry():
    fake, _config, engine, watch, _ready, _clock, _launched = watcher(
        False, True, **{"gaming.notifications.replay_unknown_warning": True})
    def fail_after_effect(hotkey):
        fake.press(hotkey)
        raise OSError("PRIVATE_SENTINEL")
    engine._press = fail_after_effect
    text = watch.game_started("Game")
    assert "был выключен" in text
    assert "мгновенный повтор NVIDIA выключен, и" not in text
    assert fake.state is True and len(fake.pressed) == 1
    watch.poll(); watch.poll()
    assert len(fake.pressed) == 1
