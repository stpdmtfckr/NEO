"""Native launches restart an installed image backend without source scripts."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jarvis import ollama_runtime as runtime
from jarvis.runtime import vision_startup as vision


def config(tmp_path, **overrides):
    values = {'ollama.vision_enabled': True, 'ollama.auto_start': True,
              'ollama.auto_install': False, 'ollama.base_url': 'http://127.0.0.1:12444'}
    values.update(overrides)
    return SimpleNamespace(get=values.get, paths=SimpleNamespace(root=tmp_path))


def status(tmp_path):
    return json.loads((tmp_path/'bootstrap/vision_status.json').read_text())


def test_native_start_queues_once_with_no_script_or_ui_thread_probe(tmp_path, monkeypatch):
    monkeypatch.setenv('NEO_HOST_EXECUTABLE', str(tmp_path/'NEO.exe'))
    monkeypatch.setenv('NEO_PACKAGE_ROOT', str(tmp_path/'native'))
    monkeypatch.setattr(vision, '__file__', str(tmp_path/'native/_internal/jarvis/runtime/vision_startup.pyd'))
    monkeypatch.setattr(vision, '_is_windows', lambda: True)
    monkeypatch.setattr(vision, '_INFLIGHT', set())
    factory = Mock()
    monkeypatch.setattr(vision.threading, 'Thread', factory)
    start = Mock(side_effect=AssertionError('No UI-thread network or process launch'))
    monkeypatch.setattr(runtime, 'ensure_ollama_running', start)
    cfg = config(tmp_path)
    assert vision.start_vision_backend(cfg)
    assert vision.start_vision_backend(cfg)
    factory.assert_called_once()
    assert factory.call_args.kwargs['target'] is vision._native_bootstrap_worker
    assert factory.call_args.kwargs['args'][1] == 'http://127.0.0.1:12444'
    assert status(tmp_path)['state'] == 'queued'
    start.assert_not_called()


@pytest.mark.parametrize('result,expected', (
    ({'ready': True, 'started': False, 'version': 'test'}, 'existing_server_reused'),
    ({'ready': True, 'started': True, 'version': 'test'}, 'local_api_verified'),
    ({'ready': False, 'started': False, 'reason': 'ollama_not_found'}, 'ollama_not_found'),
    ({'ready': False, 'started': True, 'reason': 'startup_timeout'}, 'startup_timeout'),
))
def test_native_worker_uses_bounded_lifecycle_helper_and_reports_actual_result(tmp_path, monkeypatch, result, expected):
    start = Mock(return_value=result)
    monkeypatch.setattr(runtime, 'ensure_ollama_running', start)
    monkeypatch.setattr(vision, '_INFLIGHT', {'test-key'})
    popen = Mock(side_effect=AssertionError('Native worker must not launch PowerShell'))
    monkeypatch.setattr(vision.subprocess, 'Popen', popen)
    vision._native_bootstrap_worker(config(tmp_path), 'http://127.0.0.1:12444', 'test-key')
    start.assert_called_once_with('http://127.0.0.1:12444', timeout=18.0, prefer_tray_app=False)
    report = status(tmp_path)
    assert report['reason'] == expected
    assert report['state'] == ('ready' if result['ready'] else 'unavailable')
    assert report['model_loaded_by_startup'] is False
    assert not vision._INFLIGHT
    popen.assert_not_called()


def test_native_worker_clears_reservation_after_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'ensure_ollama_running', Mock(side_effect=OSError('fixture error')))
    monkeypatch.setattr(vision, '_INFLIGHT', {'test-key'})
    vision._native_bootstrap_worker(config(tmp_path), 'http://127.0.0.1:12444', 'test-key')
    assert not vision._INFLIGHT
    assert status(tmp_path)['reason'] == 'bootstrap_exception'


@pytest.mark.parametrize('endpoint,expected', (
    ('http://127.0.0.1:12444', '127.0.0.1:12444'),
    ('http://localhost:11434', '127.0.0.1:11434'),
    ('http://[::1]:13444', '[::1]:13444'),
))
def test_owned_server_binds_selected_local_port(endpoint, expected, monkeypatch):
    monkeypatch.setenv('OLLAMA_HOST', '0.0.0.0:22222')
    monkeypatch.setattr(runtime, '_LAST_ATTEMPT', 0)
    monkeypatch.setattr(runtime, 'probe_ollama', lambda *a, **k: (False, ''))
    monkeypatch.setattr(runtime, 'find_ollama_executable', lambda: Path('/fixture/ollama.exe'))
    monkeypatch.setattr(runtime, '_wait_for_api', lambda *a, **k: (True, 'test'))
    start = Mock()
    monkeypatch.setattr(runtime.subprocess, 'Popen', start)
    result = runtime.ensure_ollama_running(endpoint)
    assert result['ready'] and result['started']
    assert start.call_args.args[0] == ['/fixture/ollama.exe', 'serve']
    assert start.call_args.kwargs['env']['OLLAMA_HOST'] == expected
    assert os.environ['OLLAMA_HOST'] == '0.0.0.0:22222'  # No parent/global mutation.


def test_live_user_server_is_reused_without_spawn_or_changes(monkeypatch):
    monkeypatch.setattr(runtime, 'probe_ollama', lambda *a, **k: (True, 'test'))
    start = Mock()
    monkeypatch.setattr(runtime.subprocess, 'Popen', start)
    result = runtime.ensure_ollama_running('http://127.0.0.1:12444')
    assert result['ready'] and not result['started']
    start.assert_not_called()
