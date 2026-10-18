"""NB18: inert blocked probes must not block cancellation or publish stale work."""
import threading
from types import SimpleNamespace

import pytest

from jarvis.integrations import instant_replay169 as ir
from jarvis.testing.isolation import external_io_blocked
from tests.test_neo184_nvidia_startup import watcher


@pytest.fixture(autouse=True)
def blocked():
    with external_io_blocked():
        yield


def begin_probe(monkeypatch, entrypoint, probe):
    fake, config, engine, watch, ready, clock, launched = watcher(None, True)
    notices = []
    if entrypoint != 'game_started':
        watch.game_started('Game', notify=notices.append, session=1)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    results, errors = [], []
    def slow():
        entered.set()
        if not release.wait(3):
            raise AssertionError('test did not release inert probe')
        return False if probe == 'state' else True
    monkeypatch.setattr(engine, probe, slow)
    def work():
        try:
            if entrypoint == 'game_started':
                results.append(watch.game_started('Game', notify=notices.append, session=1))
            elif entrypoint == 'poll':
                results.append(watch.poll())
            else:
                results.append(watch._start_auto())
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    assert entered.wait(1)
    return SimpleNamespace(fake=fake, config=config, engine=engine, watch=watch, ready=ready, clock=clock,
                           launched=launched, notices=notices, entered=entered, release=release,
                           finished=finished, worker=worker, results=results, errors=errors)


def finish(probe):
    probe.release.set()
    probe.worker.join(2)
    assert probe.finished.is_set() and not probe.errors


@pytest.mark.parametrize('entrypoint,probe', [('game_started', 'state'), ('game_started', 'overlay_state'),
    ('poll', 'state'), ('poll', 'overlay_state'), ('_start_auto', 'overlay_state')])
def test_game_end_returns_before_blocked_probe_is_released(monkeypatch, entrypoint, probe):
    p = begin_probe(monkeypatch, entrypoint, probe)
    ended = threading.Event()
    cancel = threading.Thread(target=lambda: (p.watch.game_ended(), ended.set()), daemon=True)
    try:
        cancel.start()
        assert ended.wait(.25), 'game_ended waited for a slow state/overlay provider'
        assert not p.release.is_set() and p.watch._game == ''
    finally:
        finish(p); cancel.join(2)
    assert p.results == [None] and not p.fake.pressed and not p.launched and not p.notices
    assert p.watch._last is None and not p.watch._pending and not p.watch._launch_pending


@pytest.mark.parametrize('entrypoint', ['game_started', 'poll'])
@pytest.mark.parametrize('change', ['new_game', 'profile', 'disabled', 'expired'])
def test_probe_revalidates_session_settings_and_deadline(monkeypatch, entrypoint, change):
    p = begin_probe(monkeypatch, entrypoint, 'state')
    try:
        if change == 'new_game':
            # The replacement session must initialize while the old probe waits.
            monkeypatch.setattr(p.engine, 'state', lambda: True)
            p.watch.game_started('New Game', notify=p.notices.append, session=2)
            assert p.watch._game == 'New Game' and p.watch._last is True
        elif change == 'profile':
            p.watch.config = SimpleNamespace(get=lambda key, default=None: default)
        elif change == 'disabled':
            p.config.data['gaming.instant_replay.auto_enable'] = False
            p.config.data['gaming.instant_replay.auto_launch_app'] = False
            p.config.data['gaming.notifications.nvidia_enabled'] = False
        else:
            p.clock[0] = 61
            p.config.data['gaming.notifications.nvidia_enabled'] = False
    finally:
        finish(p)
    assert p.results == [None] and not p.fake.pressed and not p.launched and not p.notices
    if change == 'new_game':
        assert p.watch._game == 'New Game' and p.watch._last is True
    if change == 'expired':
        assert p.watch._deadline == 60.0 and not p.watch._pending and not p.watch._launch_pending


def test_duplicate_poll_is_single_flight_while_probe_waits(monkeypatch):
    p = begin_probe(monkeypatch, 'poll', 'state')
    try:
        assert p.watch.poll() is None
        assert p.watch.game_started('Game', session=1) is None
        p.watch.game_ended()
    finally:
        finish(p)
    assert not p.fake.pressed and not p.launched and not p.notices


def test_failed_probe_releases_slot_and_next_poll_can_recover(monkeypatch):
    fake, _config, engine, watch, _ready, _clock, _launched = watcher(None, True)
    watch.game_started('Game')
    def fail(): raise RuntimeError('inert provider failed')
    monkeypatch.setattr(engine, 'state', fail)
    with pytest.raises(RuntimeError): watch.poll()
    assert watch._probe_generation is None
    monkeypatch.setattr(engine, 'state', lambda: None)
    assert watch.poll() is None


def test_slow_notice_callback_does_not_hold_the_cancellation_lock():
    fake, _config, _engine, watch, _ready, _clock, _launched = watcher(False, True)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def notify(_text):
        entered.set()
        release.wait(3)
        finished.set()
    watch.game_started('Game', notify=notify)
    assert entered.wait(1)
    ended = threading.Event()
    cancel = threading.Thread(target=lambda: (watch.game_ended(), ended.set()), daemon=True)
    try:
        cancel.start()
        assert ended.wait(.25), 'a slow notice callback blocked game_ended'
    finally:
        release.set(); cancel.join(2)
    assert finished.wait(1) and fake.pressed == ['alt+shift+f10']
