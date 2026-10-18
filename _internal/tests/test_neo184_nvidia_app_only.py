"""App-only migration and inert negative coverage for removed legacy support."""
import builtins
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.integrations import instant_replay169 as replay
from jarvis.integrations import nvidia_capture as capture
from jarvis.integrations import nvidia_diagnostics184 as diagnostic
from jarvis.integrations import nvidia_startup184 as startup
from jarvis.integrations import nvidia_state as state
from jarvis.runtime import settings169, settings184
from tests.test_neo169_instant_replay import cfg


@pytest.mark.parametrize('names,available', [
    (['NVIDIA GeForce Experience.exe', 'NVIDIA Share.exe'], False),
    (['NVIDIA Overlay.exe'], True),
    (['NVIDIA Share.exe', 'NVIDIA Overlay.exe'], True),
])
def test_only_app_processes_enable_capture_or_state(monkeypatch, names, available):
    import psutil
    rows = [SimpleNamespace(info={'name': name, 'pid': i + 1, 'create_time': 123})
            for i, name in enumerate(names)]
    monkeypatch.setattr(psutil, 'process_iter', lambda _fields: rows)
    assert startup.application_running() is available
    assert capture.overlay_running() is available
    found = state.processes()
    assert state.backend_of(found) == ('nvidia_app' if available else 'unavailable')
    assert all(row['backend'] == 'nvidia_app' for row in found)


def test_legacy_installation_is_neither_queried_nor_launched(monkeypatch, tmp_path):
    for key in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('ProgramFiles', str(tmp_path))
    queried, launched = [], []
    def exists(path):
        queried.append(str(path))
        # Only a legacy executable exists in this simulated installation.
        return 'GeForce Experience' in str(path)
    monkeypatch.setattr(Path, 'is_file', exists)
    controller = startup.CaptureAppStartup(running=lambda: False, installed=startup.installed_app,
                                          launch=launched.append, is_windows=True)
    assert controller.ensure_running(guard=lambda: True).status == 'not_installed'
    assert queried and all('NVIDIA App/CEF/NVIDIA app.exe' in path for path in queried)
    assert not launched


def test_old_registry_setting_is_removed_without_losing_preferences(tmp_path):
    data = {'gaming': {
        'enabled': False,
        'notifications': {'nvidia_enabled': False, 'nvidia_startup': False, 'replay_changes': False,
                          'replay_off_warning': False, 'replay_unknown_warning': True},
        'instant_replay': {'registry_values': ['DVREnabled', 'CustomLegacyValue'],
                          'auto_launch_app': True, 'auto_enable': True, 'toggle_hotkey': ''}},
        'capture': {'video_folder': str(tmp_path / 'chosen'), 'nvidia_clip_hotkey': 'ctrl+f7',
                    'nvidia_record_hotkey': ''}}
    expected = copy.deepcopy(data)
    del expected['gaming']['instant_replay']['registry_values']
    settings184.migrate(data)
    settings184.migrate(data)
    assert data['gaming'] == expected['gaming']
    assert data['capture'] == expected['capture']
    settings169.migrate(data, 184)
    assert 'registry_values' not in data['gaming']['instant_replay']
    assert data['capture'] == expected['capture']
    defaults = json.loads((Path(__file__).parents[1] / 'config/default_settings.json').read_text())
    assert 'registry_values' not in defaults['gaming']['instant_replay']


def test_retired_settings_never_reach_reader_or_trigger_registry_access(monkeypatch, tmp_path, capsys):
    original_import = builtins.__import__
    def no_registry(name, *args, **kwargs):
        if name == 'winreg':
            pytest.fail('NVIDIA App integration must not query the legacy registry')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_registry)
    snapshot = {'backend': 'nvidia_app', 'instant_replay': None, 'recording': None,
                'source': 'visible_uia', 'reason': 'overlay_not_foreground'}
    monkeypatch.setattr(state, 'snapshot', lambda: dict(snapshot))
    base = cfg(**{'gaming.instant_replay.registry_values': ['DVREnabled'],
                  'gaming.instant_replay.backend': 'geforce_experience'})
    original_get = base.get
    def get(key, default=None):
        assert key not in {'gaming.instant_replay.registry_values', 'gaming.instant_replay.backend'}
        return original_get(key, default)
    base.get = get
    pressed = []
    engine = replay.InstantReplay(base, press=pressed.append, overlay=lambda: True, is_windows=True)
    for action in ('replay_on', 'replay_off', 'replay_status'):
        assert not engine.run(action)[0]
    assert not pressed and engine.last_observation()['backend'] == 'nvidia_app'
    assert state.main([], config=base) == 0
    assert json.loads(capsys.readouterr().out)['instant_replay'] is None
    import jarvis.runtime.file_index as files
    monkeypatch.setattr(files, 'known_folders', lambda _kind: [])
    assert tmp_path in capture.video_folders(str(tmp_path))


def test_chosen_and_windows_video_folders_are_searched_for_clips(monkeypatch, tmp_path):
    chosen, windows = tmp_path / 'Chosen App Folder', tmp_path / 'Videos'
    chosen.mkdir(); windows.mkdir()
    import jarvis.runtime.file_index as files
    monkeypatch.setattr(files, 'known_folders', lambda _kind: [str(windows)])
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    assert capture.video_folders(str(chosen)) == [chosen, windows]
    assert capture.video_folders('') == [windows]
    clip = chosen / 'clip.mp4'; clip.write_bytes(b'inert video fixture')
    found = capture._recent_videos(capture.video_folders(str(chosen)), since=0)
    assert any(row[2] == clip for row in found)


def test_unsupported_backend_and_legacy_diagnostic_labels_are_unavailable():
    assert state.backend_of([{'backend': 'geforce_experience'}]) == 'unavailable'
    facts = diagnostic.facts({'backend': 'geforce_experience', 'source': 'legacy_registry',
                              'launch_target': 'geforce_experience', 'reason': 'legacy_registry_observed'})
    assert facts['backend'] == facts['source'] == facts['launch_target'] == 'unavailable'
    assert facts['reason'] == 'invalid_observation'


def test_app_help_and_folder_guidance_have_all_translations():
    from jarvis.ui.settings_text184 import ROWS
    keys = [key for key in ROWS if key.startswith(('NEO поддерживает NVIDIA App.',
                                                 'Папка видео NVIDIA App',
                                                 'Если в NVIDIA App выбрана другая папка'))]
    assert len(keys) == 3
    for key in keys:
        assert len(ROWS[key]) == 6 and all(ROWS[key])
    for key, values in ROWS.items():
        assert 'GeForce Experience' not in key
        assert all('GeForce Experience' not in value for value in values)
