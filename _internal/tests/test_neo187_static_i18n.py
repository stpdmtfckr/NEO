"""NEO 187 REC-STATIC-I18N-01: one message key = one placeholder contract."""
import ast
import string
from pathlib import Path

import pytest

from jarvis.tools.ui_messages168 import msg

ROOT = Path(__file__).resolve().parents[1]
CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def _fields(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_duplicate_registrations_never_change_placeholders():
    tree = ast.parse((ROOT / 'jarvis/tools/ui_messages168.py').read_text(encoding='utf-8'))
    seen = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, 'id', '') == 'add' and node.args:
            key = node.args[0].value
            fields = [_fields(arg.value) for arg in node.args[1:] if isinstance(arg, ast.Constant)]
            if key in seen:
                assert seen[key] == fields, key
            seen[key] = fields


@pytest.mark.parametrize('code', CODES)
def test_named_and_numbered_missing_field_messages_both_format(code):
    assert 'Поле' in msg('no_field', 'ru', name='Поле') or code != 'ru'
    assert msg('no_field', code, name='Q')
    assert msg('no_field_numbered', code, window='W', index=3, count=2)


@pytest.mark.parametrize('code', CODES[1:])
def test_cancelled_and_already_running_are_rendered_truthfully(code):
    from jarvis.runtime.localized_text import render_action_result
    cancelled = render_action_result('filesystem', {'action': 'delete'}, False, 'Отменено.', {'cancelled': True}, code)
    failed = render_action_result('filesystem', {'action': 'delete'}, False, 'Отменено.', {}, code)
    assert cancelled.splitlines()[0] != failed.splitlines()[0]
    switched = render_action_result('application', {'action': 'open'}, True, 'Открыл Steam.',
                                    {'verified': True, 'label': 'Steam', 'already_running': True, 'focused': True}, code)
    opened = render_action_result('application', {'action': 'open'}, True, 'Открыл Steam.',
                                  {'verified': True, 'label': 'Steam', 'focused': True}, code)
    assert 'Steam' in switched and switched != opened


def test_old_launch_watcher_cannot_mutate_a_newer_launch():
    from jarvis.gaming.runtime import clear_game_launch, current_launch_token, record_game_launch, recent_game_launch, update_game_launch
    first = record_game_launch(query='A', status='launch_requested')
    second = record_game_launch(query='B', status='launch_requested')
    assert first['launch_token'] != second['launch_token'] == current_launch_token()
    update_game_launch(token=first['launch_token'], status='timeout', verified=False)
    assert recent_game_launch()['status'] == 'launch_requested'
    update_game_launch(token=second['launch_token'], status='running', verified=True)
    assert recent_game_launch()['status'] == 'running'
    clear_game_launch()


def test_model_lock_mutex_is_released_when_cleanup_fails(tmp_path, monkeypatch):
    import os
    from jarvis.response_voice.model_store import ModelStore
    store = ModelStore(tmp_path)
    real_close = os.close
    calls = []

    def failing_close(fd):
        real_close(fd)
        if not calls:
            calls.append(fd)
            raise OSError('close failed after the work')
    monkeypatch.setattr(os, 'close', failing_close)
    with pytest.raises(OSError):
        with store.operation_lock():
            pass
    with store.operation_lock():                 # not "busy": the mutex was released
        pass


@pytest.mark.parametrize('text,strict,fallback', [
    ('.5 minutes', 30.0, None), ('0.5 minutes', 30.0, 30.0), ('5 minutes', 300.0, 300.0),
    ('-5 minutes', None, None), ('−5 minutes', None, None), ('App v1.5 minutes', None, None),
])
def test_duration_keeps_fraction_sign_and_number_boundary(text, strict, fallback):
    from jarvis.runtime.duration import parse_duration
    from jarvis.tools.integrations import _extract_duration_seconds
    assert parse_duration(text, 'en') == strict
    assert _extract_duration_seconds(text) == fallback


def test_settings_recovery_without_profile_lock_leaves_files_untouched(tmp_path, monkeypatch):
    import jarvis.config as cfg
    settings = tmp_path / 'settings.json'
    settings.write_text('{ broken', encoding='utf-8')

    class Busy:
        def __init__(self, *_a):
            pass

        def __enter__(self):
            raise TimeoutError('another NEO process holds the lock')
    monkeypatch.setattr(cfg, '_OwnedProfileLock186', Busy)
    monkeypatch.setattr(cfg.time, 'sleep', lambda *_: None)
    manager = cfg.ConfigManager.__new__(cfg.ConfigManager)
    manager._bootstrap_root = tmp_path
    manager.default_settings_path = tmp_path / 'defaults.json'
    called = []
    monkeypatch.setattr(cfg, 'recover_settings', lambda *a, **k: called.append(a) or ('', ''))
    assert manager._quarantine_settings182(settings, ValueError('bad json')) == ''
    assert manager._recovery_deferred187 is True and not called
    assert settings.read_text(encoding='utf-8') == '{ broken'
