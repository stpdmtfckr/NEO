"""Safe fake NVIDIA adapters only. No GPU, capture, UI, driver or key injection."""
from __future__ import annotations

import ctypes
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.integrations import instant_replay169 as ir, nvidia_capture as nv, nvidia_state as ns
from tests.test_neo169_instant_replay import FakeNvidia, cfg, replay


@pytest.mark.parametrize('value', [2, -1, 256, b'\x00\x01\x00\x00', b'\x01\xff\x00\x00', b'\x01\x00', 'true', None])
def test_state_reader_rejects_malformed_not_boolean(value):
    engine = ir.InstantReplay(cfg(), reader=lambda: value, overlay=lambda: True, is_windows=True)
    assert engine.state() is None


@pytest.mark.parametrize('combo', ['', ' ', 'alt', 'shift+ctrl', 'alt++f10', 'alt+alt+f10', 'alt+control+ctrl+f10', 'a+b', 'f10+alt'])
def test_invalid_hotkeys_are_side_effect_free(combo):
    with pytest.raises(ValueError):
        nv.parse_hotkey(combo)
    fake = FakeNvidia(False)
    assert not replay(fake, cfg(**{'gaming.instant_replay.toggle_hotkey': combo})).run('replay_on')[0]
    assert not fake.pressed


@pytest.mark.parametrize('controller', ['replay', 'capture'])
def test_invalid_action_never_falls_through_to_record_or_toggle(controller):
    fake = FakeNvidia(False)
    engine = replay(fake) if controller == 'replay' else nv.NvidiaCapture(cfg(), press=fake.press, overlay=lambda: True, is_windows=True)
    for action in ['', 'unsupported', 'replay_save', 'screenshot']:
        assert not engine.run(action)[0]
    assert fake.pressed == []


@pytest.mark.parametrize('action,key', [('clip', 'capture.nvidia_clip_hotkey'), ('record_start', 'capture.nvidia_record_hotkey')])
def test_cleared_capture_shortcut_stays_disabled(action, key):
    pressed = []
    ctl = nv.NvidiaCapture(cfg(**{key: ''}), press=pressed.append, overlay=lambda: True, is_windows=True)
    assert not ctl.run(action)[0]
    assert pressed == []


def test_nvidia_app_ui_process_alone_is_not_overlay(monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, 'process_iter', lambda *a: [SimpleNamespace(info={'name': 'NVIDIA App.exe'})])
    assert nv.overlay_running() is False
    monkeypatch.setattr(psutil, 'process_iter', lambda *a: (_ for _ in ()).throw(PermissionError()))
    assert nv.overlay_running() is None


def test_input_exception_releases_all_modifiers(monkeypatch):
    events = []
    def event(key, _scan, flag, _extra):
        events.append((key, flag))
        if key == 0x79 and flag == 0:
            raise OSError('fake denied input')
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **kw: SimpleNamespace(keybd_event=event), raising=False)
    monkeypatch.setattr(nv.time, 'sleep', lambda _s: None)
    with pytest.raises(OSError):
        nv.press_hotkey('alt+shift+f10')
    assert events[-3:] == [(0x79, 2), (0x10, 2), (0x12, 2)]


def test_concurrent_replay_enables_press_once():
    fake = FakeNvidia(False)
    engine = replay(fake)
    started, release = threading.Event(), threading.Event()
    def press(key):
        started.set()
        assert release.wait(2)
        fake.press(key)
    engine._press = press
    results = []
    t1 = threading.Thread(target=lambda: results.append(engine.run('replay_on')))
    t2 = threading.Thread(target=lambda: results.append(engine.run('replay_on')))
    t1.start(); assert started.wait(2); t2.start(); release.set()
    t1.join(2); t2.join(2)
    assert len(results) == 2 and all(row[0] for row in results)
    assert fake.pressed == ['alt+shift+f10'] and fake.state is True


def test_temporary_unknown_after_press_can_recover():
    fake = FakeNvidia(False)
    engine = replay(fake)
    reads = iter([False, None, None, True])
    engine._reader = lambda: next(reads, True)
    assert engine.run('replay_on')[2]['verified'] is True


def test_unknown_after_idempotent_request_is_not_success():
    fake = FakeNvidia(False)
    engine = replay(fake)
    engine._press = lambda key: (fake.pressed.append(key), setattr(fake, 'state', None))
    ok, _text, data = engine.run('replay_on')
    assert not ok and data['pressed'] and data['verified'] is False


def test_press_exception_is_honest_and_not_retried():
    fake = FakeNvidia(False)
    engine = replay(fake)
    engine._press = lambda _key: (_ for _ in ()).throw(PermissionError())
    ok, text, data = engine.run('replay_on')
    assert not ok and data['error_kind'] == 'input_failed' and 'права' in text


def watcher(state=False, **settings):
    fake = FakeNvidia(state)
    config = cfg(**{'gaming.instant_replay.auto_enable': True, **settings})
    engine = replay(fake, config)
    clock = [0.0]
    watch = ir.ReplayWatch(config, engine, clock=lambda: clock[0])
    return fake, config, engine, watch, clock


def test_late_unknown_state_or_overlay_enables_once_without_changes_notice_setting():
    fake, config, engine, watch, clock = watcher(None, **{'gaming.notifications.replay_changes': False, 'gaming.notifications.nvidia_startup': False})
    ready = [False]
    engine._overlay = lambda: ready[0]
    assert watch.game_started('For Honor') is None
    fake.state = False
    assert watch.poll() is None and fake.pressed == []
    ready[0] = True
    assert watch.poll() is None  # automatic success follows replay_changes
    assert watch.poll() is None and fake.pressed == ['alt+shift+f10']
    fake.state = False  # manual choice must not be overridden after startup
    assert watch.poll() is None and len(fake.pressed) == 1


def test_late_on_never_toggles_and_first_known_state_is_reported():
    fake, _cfg, _engine, watch, _clock = watcher(None)
    watch.game_started('For Honor')
    fake.state = True
    assert watch.poll() == ir.message('on', 'ru')
    assert fake.pressed == [] and watch.poll() is None


def test_unknown_gap_preserves_last_observation_and_notifies_once():
    fake, _cfg, _engine, watch, _clock = watcher(True)
    watch.game_started('For Honor'); fake.state = None
    assert watch.poll() is None
    fake.state = False
    assert 'выключился' in watch.poll()
    assert watch.poll() is None and fake.pressed == []


def test_duplicates_timeout_optout_and_relaunch():
    fake, config, _engine, watch, clock = watcher(None)
    watch.game_started('For Honor'); clock[0] = 61
    fake.state = False
    watch.poll()
    assert fake.pressed == []
    assert watch.game_started('For Honor') is None
    watch.game_ended(); assert watch.poll() is None
    assert 'включил' in watch.game_started('For Honor')
    assert len(fake.pressed) == 1
    watch.game_ended(); fake.state = None
    watch.game_started('For Honor'); config.data['gaming.instant_replay.auto_enable'] = False
    fake.state = False; watch.poll()
    assert len(fake.pressed) == 1


def test_game_end_cancels_queued_auto_worker_without_keys_or_late_notice():
    fake, _cfg, engine, watch, _clock = watcher(False)
    notices = []
    engine._lock.acquire()
    watch.game_started('For Honor', notify=notices.append)
    watch.game_ended()
    engine._lock.release()
    # Wait on an event driven observer of completion, no real polling delays.
    done = threading.Event()
    threading.Thread(target=lambda: (engine.run('replay_status'), done.set())).start()
    assert done.wait(2)
    assert fake.pressed == [] and notices == []


def test_capture_start_stop_requires_live_state_not_internal_intent():
    presses = []
    ctl = nv.NvidiaCapture(cfg(), press=presses.append, overlay=lambda: True, is_windows=True,
                           recording_reader=lambda: None, sleep=lambda _s: None)
    ctl.state.recording = True
    for action in ['record_start', 'record_stop']:
        assert not ctl.run(action)[0]
    assert presses == []


def test_capture_observed_manual_stop_does_not_restart():
    presses = []
    ctl = nv.NvidiaCapture(cfg(), press=presses.append, overlay=lambda: True, is_windows=True,
                           recording_reader=lambda: False, sleep=lambda _s: None)
    ctl.state.recording = True
    ok, _text, data = ctl.run('record_stop')
    assert ok and data['recording'] is False and presses == []


def test_capture_no_effect_and_no_file_are_not_success():
    ctl = nv.NvidiaCapture(cfg(), press=lambda _s: None, overlay=lambda: True, is_windows=True,
                           recording_reader=lambda: False, sleep=lambda _s: None, wait=lambda *a: None)
    ctl._folders = lambda: [Path('/fake/videos')]
    assert not ctl.run('record_start')[0]
    ok, text, data = ctl.run('clip')
    assert not ok and not data['verified'] and 'не найден' in text


def test_existing_or_growing_video_is_not_saved_success(monkeypatch):
    path = Path('/fake/videos/old.mp4')
    monkeypatch.setattr(nv, '_recent_videos', lambda *a: [(10.0, 4, path)])
    assert nv.wait_for_new_video([], 10, 0, baseline={path: (10.0, 4)}) is None
    tick = [0]
    def growing(*args):
        tick[0] += 1
        return [(10.0, tick[0], path)]
    monkeypatch.setattr(nv, '_recent_videos', growing)
    assert nv.wait_for_new_video([], 10, 1, sleep=lambda _s: None) is None


class Control:
    def __init__(self, name, toggle, *, visible=True, enabled=True, pid=1):
        self.name, self.toggle = name, toggle
        self.visible, self.enabled = visible, enabled
        self.element_info = SimpleNamespace(process_id=pid)
    def window_text(self): return self.name
    def is_visible(self): return self.visible
    def is_enabled(self): return self.enabled
    def get_toggle_state(self): return self.toggle


@pytest.mark.parametrize('label', sorted(ns.REPLAY_LABELS))
def test_exact_visible_nvidia_toggle_is_observable(label):
    assert ns.states_from_controls([Control(label, 1)], 1)['instant_replay'] is True
    assert ns.states_from_controls([Control(label, 0)], 1)['instant_replay'] is False


@pytest.mark.parametrize('control', [Control('Instant Replay', 2), Control('Instant Replay', True), Control('Instant Replay', 1, visible=False),
    Control('Instant Replay', 1, pid=2), Control('Instant Replay', 1, enabled=False), Control('Turn on Instant Replay', 0),
    Control('Instant Replay settings', 1), Control('On', 1), Control('Instant Replay', '1')])
def test_untrusted_or_ambiguous_ui_is_never_state(control):
    assert ns.states_from_controls([control], 1)['instant_replay'] is None
    assert ns.states_from_controls([Control('Instant Replay', 0), Control('Instant Replay', 1)], 1)['instant_replay'] is None


@pytest.mark.parametrize('state', [True, False, None])
def test_replay_state_uses_only_the_live_app_snapshot(monkeypatch, state):
    calls = []
    def snapshot():
        calls.append(1)
        return {'backend': 'nvidia_app', 'instant_replay': state}
    monkeypatch.setattr(ns, 'snapshot', snapshot)
    assert ns.replay_state() is state
    assert calls == [1]


def test_all_user_messages_have_seven_localizations():
    for messages in [nv.MESSAGES, ir.MESSAGES]:
        assert all(len(row) == 7 and all(isinstance(text, str) and text for text in row) for row in messages.values())


def test_new_process_of_same_game_gets_new_startup_attempt():
    fake, _cfg, _engine, watch, _clock = watcher(False)
    assert 'включил' in watch.game_started('For Honor', session=(10, 100))
    fake.state = False
    assert watch.game_started('For Honor', session=(10, 100)) is None
    assert 'включил' in watch.game_started('For Honor', session=(11, 101))
    assert len(fake.pressed) == 2


def test_pending_auto_times_out_with_one_honest_notice():
    fake, _cfg, _engine, watch, clock = watcher(None, **{'gaming.notifications.replay_unknown_warning': True})
    watch.game_started('For Honor'); clock[0] = 61
    assert 'Не удалось безопасно' in watch.poll()
    assert watch.poll() is None and fake.pressed == []


def test_tracker_keeps_polling_when_overlay_takes_foreground(monkeypatch):
    from tests.test_neo169_instant_replay import tracker, snap
    from jarvis.gaming import memory
    t, notices = tracker(monkeypatch, FakeNvidia(True))
    t._current = snap()
    t._finish_if_dead = lambda: None  # already verified living process
    t._stop = SimpleNamespace(is_set=lambda: False, wait=lambda _interval: True)
    t._replay_watch = SimpleNamespace(poll=lambda: 'observed replay change')
    monkeypatch.setattr(memory, 'game_presence_service', lambda manager: SimpleNamespace(snapshot=lambda **kw: None))
    t._run()
    assert notices == ['observed replay change']


def _probe_environment(monkeypatch, tmp_path, outcome):
    import json
    rows = [{'pid': 42, 'started': 5, 'backend': 'nvidia_app'}]
    monkeypatch.setattr(ns, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(ns, 'processes', lambda: rows)
    monkeypatch.setattr(ns, 'foreground', lambda: (100, 42))
    monkeypatch.setattr(ns, 'sys', SimpleNamespace(executable=str(tmp_path / 'NEO.exe')))
    (tmp_path / 'python.exe').write_bytes(b'fake; never executed')
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(returncode=0, stdout=json.dumps(outcome))
    monkeypatch.setattr(ns.subprocess, 'run', run)
    return calls


def test_uia_subprocess_uses_runtime_python_with_fixed_module_and_timeout(monkeypatch, tmp_path):
    expected = {'backend': 'nvidia_app', 'instant_replay': True, 'recording': False, 'source': 'visible_uia', 'reason': 'observed'}
    calls = _probe_environment(monkeypatch, tmp_path, expected)
    assert ns.snapshot() == expected
    command, options = calls[0]
    assert command == [str(tmp_path / 'python.exe'), '-u', '-m', 'jarvis.integrations.nvidia_state', '--probe']
    assert options['timeout'] == 1.5 and options['check'] is False
    assert options['env']['PYTHONPATH'].endswith('_internal')


def test_uia_probe_failure_unknown_and_no_extra_process(monkeypatch, tmp_path):
    calls = _probe_environment(monkeypatch, tmp_path, ns.subprocess.TimeoutExpired('fake', 1.5))
    result = ns.snapshot()
    assert result['reason'] == 'probe_timeout' and result['instant_replay'] is None
    ns._PROBE_LOCK.acquire()
    try:
        assert ns.snapshot()['reason'] == 'probe_busy'
    finally:
        ns._PROBE_LOCK.release()
    assert len(calls) == 1


@pytest.mark.parametrize('bad', [1, 'on', [], {}])
def test_malformed_worker_state_is_rejected(monkeypatch, tmp_path, bad):
    _probe_environment(monkeypatch, tmp_path, {'backend': 'nvidia_app', 'instant_replay': bad, 'recording': None})
    result = ns.snapshot()
    assert result['reason'] == 'invalid_probe' and result['instant_replay'] is None


def test_diagnostic_bootstrap_route_is_read_only(monkeypatch, capsys):
    import bootstrap
    calls = []
    monkeypatch.setattr(ns, 'main', lambda args, **kw: (calls.append((args, kw)), 0)[1])
    monkeypatch.setattr(bootstrap, 'launch', lambda: pytest.fail('must not launch app'))
    monkeypatch.setattr(bootstrap, 'ReadOnlyConfig', lambda: 'read-only-config')
    assert bootstrap.main(['--nvidia-diagnostic']) == 0
    assert calls == [(['--diagnose'], {'config': 'read-only-config'})]


def test_diagnostic_output_has_only_allowlisted_status_and_no_raw_config(monkeypatch, capsys):
    import json
    monkeypatch.setattr(ns, 'snapshot', lambda: {'backend': 'nvidia_app', 'instant_replay': None, 'recording': None, 'source': 'visible_uia', 'reason': 'controls_not_exposed'})
    monkeypatch.setattr(ns.time, 'sleep', lambda _s: None)
    config = cfg(**{'gaming.instant_replay.toggle_hotkey': 'secret-looking-value', 'capture.nvidia_clip_hotkey': ''})
    assert ns.main([], config=config) == 0
    text = capsys.readouterr().out
    data = json.loads(text)
    assert data['replay_hotkey'] == data['clip_hotkey'] == 'invalid_or_cleared'
    assert 'secret-looking-value' not in text and data['compatibility_verified_on_this_build'] is False


def test_launcher_diagnostic_is_allowlisted_and_refuses_missing_dependencies():
    root = Path(__file__).parents[1]
    launch = (root / 'launch.ps1').read_text(encoding='utf-8-sig')
    assert "'nvidia-diagnostic'" in launch and 'NEO.cmd --nvidia-diagnostic' in launch
    start = launch.index("if (-not $healthy -or $Mode -eq '--repair')")
    before_setup = launch[start:launch.index("$env:NEO_VENV = $runtime", start)]
    assert "'--nvidia-diagnostic'" in before_setup and 'exit 2' in before_setup
    cmd = (root.parent / 'NEO.cmd').read_text()
    assert cmd.index('goto diagnostics') < cmd.index('start_ollama_background.ps1')
    assert '-Mode "%NEO_MODE%"' in cmd


def test_stopping_game_tracker_invalidates_pending_auto(monkeypatch):
    from tests.test_neo169_instant_replay import tracker
    t, _notices = tracker(monkeypatch, FakeNvidia(None), **{'gaming.instant_replay.auto_enable': True})
    t._replay_watch.game_started('For Honor')
    assert t._replay_watch._pending
    t.stop()
    assert not t._replay_watch._pending and not t._replay_watch._game
