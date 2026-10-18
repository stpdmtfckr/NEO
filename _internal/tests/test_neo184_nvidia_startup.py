"""Launch policy with inert adapters; no NVIDIA execution, keys or Windows changes."""
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from jarvis.integrations import instant_replay169 as ir, nvidia_startup184 as startup
from jarvis.integrations.nvidia_startup_text184 import MESSAGES
from jarvis.testing.isolation import external_io_blocked
from tests.test_neo169_instant_replay import FakeNvidia, cfg, replay


@pytest.fixture(autouse=True)
def blocked():
    with external_io_blocked():
        yield


def app():
    return startup.InstalledApp('NVIDIA App', Path('/fake/Program Files/NVIDIA Corporation/NVIDIA App/CEF/NVIDIA app.exe'))


def adapter(*, running=False, installed=True, launch=None):
    return startup.CaptureAppStartup(running=lambda: running, installed=lambda: app() if installed else None,
                                     launch=launch or (lambda _app: None), is_windows=True)


@pytest.mark.parametrize('running,status', [(True, 'already_running'), (None, 'probe_unknown')])
def test_already_running_and_unknown_do_not_discover_or_launch(running, status):
    ctl = startup.CaptureAppStartup(running=lambda: running, installed=lambda: pytest.fail('no discovery needed'),
                                    launch=lambda _app: pytest.fail('no launch'), is_windows=True)
    assert ctl.ensure_running(guard=lambda: True).status == status


def test_missing_launch_failure_and_nonwindows_are_honest():
    assert adapter(installed=False).ensure_running(guard=lambda: True).status == 'not_installed'
    def fail(_app): raise OSError('fake failure')
    assert adapter(launch=fail).ensure_running(guard=lambda: True).status == 'launch_failed'
    ctl = adapter(); ctl._windows = False
    assert ctl.ensure_running(guard=lambda: True).status == 'unsupported'


def test_process_that_starts_during_discovery_is_not_launched_again():
    reads = iter([False, True]); launched = []
    ctl = adapter(launch=launched.append); ctl._running = lambda: next(reads)
    assert ctl.ensure_running(guard=lambda: True).status == 'already_running'
    assert not launched


def test_guard_rechecked_after_discovery_and_before_launch():
    allowed = [True]; launched = []
    ctl = adapter(launch=launched.append)
    ctl._installed = lambda: (allowed.__setitem__(0, False), app())[1]
    assert ctl.ensure_running(guard=lambda: allowed[0]).status == 'cancelled'
    assert not launched


@pytest.mark.parametrize('name', sorted(startup.APP_PROCESSES))
def test_process_detection_accepts_supported_names_and_handles_denial(monkeypatch, name):
    import psutil
    monkeypatch.setattr(psutil, 'process_iter', lambda *a: [SimpleNamespace(info={'name': name.upper()})])
    assert startup.application_running() is True
    monkeypatch.setattr(psutil, 'process_iter', lambda *a: [SimpleNamespace(info={'name': 'nvcontainer.exe'})])
    assert startup.application_running() is False
    monkeypatch.setattr(psutil, 'process_iter', lambda *a: (_ for _ in ()).throw(PermissionError()))
    assert startup.application_running() is None


def test_discovery_supports_only_app_and_ignores_path(tmp_path, monkeypatch):
    for key in ['ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)']:
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / 'Programs'; monkeypatch.setenv('ProgramFiles', str(root))
    assert startup.installed_app() is None
    legacy = root / 'NVIDIA Corporation/NVIDIA GeForce Experience/NVIDIA GeForce Experience.exe'
    legacy.parent.mkdir(parents=True); legacy.write_text('inert fake, never executed')
    assert startup.installed_app() is None
    name, rel = startup.INSTALL_PATHS[0]; target = root / rel
    target.parent.mkdir(parents=True); target.write_text('inert fake, never executed')
    assert startup.installed_app() == startup.InstalledApp(name, target)
    monkeypatch.setenv('ProgramFiles', 'relative/path')
    assert startup.installed_app() is None


def test_launch_uses_windows_nonactivating_request_without_shell_or_vendor_flags(monkeypatch):
    calls = []
    monkeypatch.setattr(startup.subprocess, 'STARTUPINFO', lambda: SimpleNamespace(dwFlags=0, wShowWindow=0), raising=False)
    monkeypatch.setattr(startup.subprocess, 'STARTF_USESHOWWINDOW', 1, raising=False)
    monkeypatch.setattr(startup.subprocess, 'Popen', lambda args, **kwargs: calls.append((args, kwargs)))
    startup.launch_minimized(app())
    args, options = calls[0]
    assert args == [str(app().executable)] and options['shell'] is False
    assert options['cwd'] == str(app().executable.parent)
    assert options['startupinfo'].dwFlags & 1 and options['startupinfo'].wShowWindow == 7
    assert options['stdin'] == options['stdout'] == options['stderr'] == startup.subprocess.DEVNULL


def watcher(state=None, overlay=False, **settings):
    config = cfg(**{'gaming.instant_replay.auto_enable': True, 'gaming.instant_replay.auto_launch_app': True, **settings})
    fake = FakeNvidia(state); engine = replay(fake, config)
    ready = [overlay]; clock = [0.0]; launched = []
    engine._overlay = lambda: ready[0]
    watch = ir.ReplayWatch(config, engine, clock=lambda: clock[0], startup=adapter(launch=launched.append))
    return fake, config, engine, watch, ready, clock, launched


def test_launch_then_wait_for_overlay_and_verified_off_enables_once():
    fake, _config, _engine, watch, ready, _clock, launched = watcher()
    assert 'запросил запуск NVIDIA App' in watch.game_started('Game', session=(1, 10))
    assert len(launched) == 1 and not fake.pressed
    for _ in range(5):
        assert watch.game_started('Game', session=(1, 10)) is None
        assert watch.poll() is None
    assert len(launched) == 1
    ready[0] = True; fake.state = False
    assert 'включил' in watch.poll()
    assert fake.pressed == ['alt+shift+f10']
    assert watch.poll() is None
    fake.state = False
    assert 'выключился' in watch.poll()
    assert len(fake.pressed) == 1  # respect manual OFF after startup


def test_existing_overlay_never_launches_an_app():
    fake, _c, _e, watch, _ready, _clock, launched = watcher(False, True)
    assert 'включил' in watch.game_started('Game')
    assert not launched and len(fake.pressed) == 1


def test_running_app_is_not_relaunched_while_waiting_for_overlay():
    fake, _c, _e, watch, ready, _clock, launched = watcher()
    watch._startup._running = lambda: True
    assert 'уже работает' in watch.game_started('Game')
    for _ in range(3): assert watch.poll() is None
    ready[0] = True; fake.state = False; watch.poll()
    assert not launched and len(fake.pressed) == 1


@pytest.mark.parametrize('status', ['not_installed', 'launch_failed', 'probe_unknown'])
def test_startup_failure_is_one_notice_with_no_keys_or_retry(status):
    fake, _c, _e, watch, _ready, _clock, launched = watcher()
    calls = []
    watch._startup = SimpleNamespace(ensure_running=lambda **kw: (calls.append(1), startup.StartupResult(status))[1])
    assert watch.game_started('Game')
    for _ in range(4): assert watch.poll() is None
    assert calls == [1] and not fake.pressed and not launched


def test_readiness_timeout_reports_once_and_cannot_enable_late():
    fake, _c, _e, watch, ready, clock, launched = watcher()
    watch.game_started('Game'); clock[0] = 61
    assert '60 секунд' in watch.poll()
    assert watch.poll() is None
    ready[0] = True; fake.state = False; watch.poll()
    assert len(launched) == 1 and not fake.pressed


def test_overlay_ready_with_unknown_state_never_toggles_or_claims_on():
    fake, _c, _e, watch, ready, clock, _launched = watcher(**{'gaming.notifications.replay_unknown_warning': True})
    watch.game_started('Game'); ready[0] = True
    assert watch.poll() is None
    clock[0] = 61
    assert 'Не удалось безопасно' in watch.poll()
    assert not fake.pressed


def test_unknown_overlay_probe_never_launches_until_missing_is_confirmed():
    fake, _c, _e, watch, ready, _clock, launched = watcher(overlay=None)
    assert watch.game_started('Game') is None and not launched
    assert watch.poll() is None and not launched
    ready[0] = False
    assert 'запросил запуск' in watch.poll()
    assert len(launched) == 1 and not fake.pressed


def test_notification_master_mutes_passive_actions_but_explicit_reply_is_informative():
    fake, _c, engine, watch, ready, clock, launched = watcher(**{'gaming.notifications.nvidia_enabled': False})
    assert watch.game_started('Game') is None
    ready[0] = True; fake.state = False
    assert watch.poll() is None and len(fake.pressed) == 1 and len(launched) == 1
    ok, text, data = engine.run('replay_status')
    assert ok and text and data['instant_replay'] is True
    fake.state = False
    assert watch.poll() is None


def test_existing_per_event_settings_mute_auto_success_and_failure():
    fake, _c, _e, watch, _r, _t, _l = watcher(False, True, **{'gaming.notifications.replay_changes': False})
    assert watch.game_started('Game') is None and len(fake.pressed) == 1
    fake, _c, _e, watch, _r, _t, _l = watcher(False, True, **{'gaming.notifications.replay_off_warning': False})
    fake.responds = False
    assert watch.game_started('Game') is None and len(fake.pressed) == 1


def test_startup_notices_toggle_does_not_prevent_launch_or_replay():
    fake, _c, _e, watch, ready, _t, launched = watcher(**{'gaming.notifications.nvidia_startup': False})
    assert watch.game_started('Game') is None and len(launched) == 1
    ready[0] = True; fake.state = False
    assert 'включил' in watch.poll()


def test_disabled_autostart_can_warn_but_does_not_launch():
    fake, _c, _e, watch, _r, _t, launched = watcher(**{'gaming.instant_replay.auto_launch_app': False})
    assert 'оверлей NVIDIA не запущен' in watch.game_started('Game')
    assert watch.poll() is None and not launched and not fake.pressed


def test_disabled_auto_enable_still_allows_app_launch_without_toggle():
    fake, _c, _e, watch, ready, _t, launched = watcher(**{'gaming.instant_replay.auto_enable': False})
    assert watch.game_started('Game') and len(launched) == 1
    ready[0] = True; fake.state = False; watch.poll()
    assert not fake.pressed


def test_disabled_gaming_does_nothing():
    fake, _c, _e, watch, _r, _t, launched = watcher(**{'gaming.enabled': False})
    assert watch.game_started('Game') is None and watch.poll() is None
    assert not launched and not fake.pressed


def queued_worker(monkeypatch):
    targets = []
    class Worker:
        def __init__(self, *, target, **kwargs): self.target = target
        def start(self): targets.append(self.target)
    monkeypatch.setattr(ir.threading, 'Thread', Worker)
    return targets


@pytest.mark.parametrize('change', ['game_end', 'new_game', 'profile_object', 'profile_path', 'gaming_disabled', 'launch_disabled'])
def test_queued_launch_cancelled_before_any_external_effect(monkeypatch, change):
    targets = queued_worker(monkeypatch)
    fake, config, _engine, watch, _r, _t, launched = watcher()
    config.paths = SimpleNamespace(settings=Path('/profile/a.json'))
    notices = []
    watch.game_started('Game', notify=notices.append)
    assert len(targets) == 1 and not launched and not notices
    if change == 'game_end': watch.game_ended()
    elif change == 'new_game': watch.game_started('New Game', notify=notices.append)
    elif change == 'profile_object': watch.config = cfg()
    elif change == 'profile_path': config.paths.settings = Path('/profile/b.json')
    elif change == 'gaming_disabled': config.data['gaming.enabled'] = False
    else: config.data['gaming.instant_replay.auto_launch_app'] = False
    targets[0]()
    assert not launched and not notices and not fake.pressed


def test_queued_auto_enable_cancelled_on_profile_change(monkeypatch):
    targets = queued_worker(monkeypatch)
    fake, config, _engine, watch, _r, _t, launched = watcher(False, True)
    notices = []; watch.game_started('Game', notify=notices.append)
    watch.config = cfg(**{'gaming.instant_replay.auto_enable': True})
    targets[0]()
    assert not fake.pressed and not notices and not launched


def test_notice_options_are_rechecked_when_worker_delivers(monkeypatch):
    targets = queued_worker(monkeypatch)
    fake, config, _engine, watch, _r, _t, launched = watcher()
    notices = []; watch.game_started('Game', notify=notices.append)
    config.data['gaming.notifications.nvidia_startup'] = False
    targets[0]()
    assert len(launched) == 1 and not notices


def test_new_session_can_launch_again_after_prior_game_ended():
    _f, _c, _e, watch, _r, _t, launched = watcher()
    watch.game_started('Game', session=(1, 10)); watch.game_ended()
    watch.game_started('Game', session=(2, 20))
    assert len(launched) == 2


def test_additive_defaults_preserve_all_existing_values():
    from jarvis.runtime import settings184
    data = {'gaming': {'notifications': {'nvidia_enabled': False, 'nvidia_startup': False, 'replay_changes': False},
                       'instant_replay': {'auto_launch_app': True, 'auto_enable': True, 'toggle_hotkey': 'ctrl+f8'}}}
    settings184.migrate(data); settings184.migrate(data)
    assert data['gaming']['notifications'] == {'nvidia_enabled': False, 'nvidia_startup': False, 'replay_changes': False}
    assert data['gaming']['instant_replay'] == {'auto_launch_app': True, 'auto_enable': True, 'toggle_hotkey': 'ctrl+f8'}
    fresh = {}; settings184.migrate(fresh)
    assert fresh['gaming']['instant_replay']['auto_launch_app'] is False
    defaults = json.loads((Path(__file__).parents[1] / 'config/default_settings.json').read_text())
    assert fresh['gaming']['notifications']['nvidia_enabled'] is defaults['gaming']['notifications']['nvidia_enabled'] is True


def test_notices_and_new_controls_have_all_seven_languages():
    from jarvis.ui.settings_text184 import ROWS
    for row in MESSAGES.values():
        assert len(row) == 7 and all(row)
        for text in row:
            assert '{game}' in text
    source = (Path(__file__).parents[1] / 'jarvis/ui/settings_dialog.py').read_text()
    for key in ['Уведомления NVIDIA в чате (ответы на команды остаются)',
                'Сообщать об отсутствующем оверлее и результате запуска приложения NVIDIA',
                'Запускать установленную NVIDIA App при старте игры',
                'Автоматически включать мгновенный повтор при запуске игры, если он точно выключен']:
        assert key in source and len(ROWS[key]) == 6 and all(ROWS[key])


def test_unreadable_process_name_does_not_prove_app_absence(monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, 'process_iter', lambda *a: [SimpleNamespace(info={'name': None})])
    assert startup.application_running() is None


@pytest.mark.parametrize('section', [{'gaming': None}, {'gaming': {'notifications': None, 'instant_replay': False}}])
def test_malformed_new_settings_sections_recover_without_throwing(section):
    from jarvis.runtime.settings184 import migrate
    migrate(section)
    assert section['gaming']['instant_replay']['auto_launch_app'] is False


@pytest.mark.parametrize('status', ['not_installed', 'launch_failed', 'probe_unknown'])
def test_failed_app_startup_preserves_late_safe_replay_enable(status):
    fake, _config, _engine, watch, ready, clock, launched = watcher()
    calls = []
    watch._startup = SimpleNamespace(ensure_running=lambda **kw: (calls.append(1), startup.StartupResult(status))[1])
    assert watch.game_started('Game')
    assert watch._pending and not watch._launch_pending and watch._launch_attempted
    # The user or vendor makes the overlay available before the next normal poll.
    clock[0] = 4; ready[0] = True; fake.state = False
    assert 'включил' in watch.poll()
    assert fake.pressed == ['alt+shift+f10'] and calls == [1] and not launched
    assert watch.poll() is None
    fake.state = False
    watch.poll()  # A later manual OFF remains the user's choice.
    assert fake.pressed == ['alt+shift+f10'] and calls == [1]


@pytest.mark.parametrize('status', ['not_installed', 'launch_failed', 'probe_unknown'])
@pytest.mark.parametrize('cancel', ['game_end', 'profile_change', 'gaming_disabled', 'auto_enable_disabled', 'deadline'])
def test_failed_app_startup_keeps_replay_cancellation_and_original_deadline(status, cancel):
    fake, config, _engine, watch, ready, clock, launched = watcher()
    calls = []
    watch._startup = SimpleNamespace(ensure_running=lambda **kw: (calls.append(1), startup.StartupResult(status))[1])
    watch.game_started('Game')
    assert watch._pending and watch._deadline == 60.0
    if cancel == 'game_end': watch.game_ended()
    elif cancel == 'profile_change': watch.config = cfg(**{'gaming.instant_replay.auto_enable': True})
    elif cancel == 'gaming_disabled': config.data['gaming.enabled'] = False
    elif cancel == 'auto_enable_disabled': config.data['gaming.instant_replay.auto_enable'] = False
    clock[0] = 61 if cancel == 'deadline' else 4
    ready[0] = True; fake.state = False
    watch.poll(); watch.poll()
    assert not watch._pending and not fake.pressed and calls == [1] and not launched
