"""Automatic fine-tuning must never provision tools or model weights implicitly."""
from __future__ import annotations

import json
import os
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.neo_llm import jobs, pipeline


def config_at(path, **values):
    settings = {'llm_agent.size': '0.5b', 'llm_agent.enabled': True,
                'llm_agent.finetune.auto': True, 'developer_mode.enabled': False}
    settings.update(values)
    return SimpleNamespace(paths=SimpleNamespace(root=path), values=settings,
                           get=lambda key, default=None: settings.get(key, default))


def provision(config, *, sharded=False):
    root = pipeline.work_root(config)
    python = pipeline.venv_python(root)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b'fake interpreter; never run')
    (root / 'venv' / 'neo_packages.json').write_text(json.dumps({'torch': '2.5.1', 'cuda': False}))
    folder = pipeline.base_folder(config)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ('config.json', 'tokenizer.json', 'tokenizer_config.json'):
        (folder / name).write_text('{}')
    if sharded:
        names = ['model-00001-of-00002.safetensors', 'model-00002-of-00002.safetensors']
        (folder / 'model.safetensors.index.json').write_text(json.dumps({'weight_map': dict(enumerate(names))}))
        for name in names:
            (folder / name).write_bytes(b'fake shard')
    else:
        (folder / 'model.safetensors').write_bytes(b'fake weights')
    return folder


@pytest.fixture(autouse=True)
def isolated_jobs(monkeypatch):
    monkeypatch.setattr(jobs, '_JOB', {'process': None, 'started': 0.0})
    monkeypatch.setattr(jobs, '_SETUP_WAIT', OrderedDict())
    monkeypatch.setattr(jobs, '_SCHEDULER', {'started': False})


def forbidden(*args, **kwargs):
    pytest.fail('automatic local-only run attempted setup, network, training or subprocess work')


def test_fresh_defaults_scheduler_waits_without_downloads_or_training(tmp_path, monkeypatch):
    defaults = json.loads((Path(__file__).parents[1] / 'config/default_settings.json').read_text())
    assert defaults['llm_agent']['finetune']['auto'] is True
    assert defaults['developer_mode']['enabled'] is False
    config = config_at(tmp_path, **{'llm_agent.size': defaults['llm_agent']['size']})
    timers = []

    class Timer:
        def __init__(self, delay, callback):
            self.delay, self.callback = delay, callback
            timers.append(self)

        def start(self):
            pass

    from jarvis.neo_model import jobs as command_jobs
    monkeypatch.setattr(jobs.threading, 'Timer', Timer)
    monkeypatch.setattr(jobs, 'os', SimpleNamespace(name='nt', replace=os.replace))
    monkeypatch.setattr(command_jobs, 'idle_seconds', lambda: 3600)
    monkeypatch.setattr(jobs, '_game_running', lambda: False)
    monkeypatch.setattr(jobs.subprocess, 'Popen', forbidden)
    checks = []
    check = pipeline.local_prerequisites
    monkeypatch.setattr(jobs, 'local_prerequisites', lambda value: checks.append(value) or check(value))
    jobs.start_scheduler(config)
    assert timers[0].delay == 120
    timers[0].callback()
    status = jobs.read_status(config)
    assert status['status']['stage'] == 'waiting_setup'
    assert 'explicitly' in status['status']['note']
    assert len(status['status']['missing']) == 3
    assert status['running'] is False
    assert not (tmp_path / 'llm_train' / 'venv').exists()
    assert not (tmp_path / 'llm_train' / 'base').exists()
    monkeypatch.setattr(command_jobs, 'idle_seconds', forbidden)
    monkeypatch.setattr(jobs, '_game_running', forbidden)
    timers[-1].callback()
    assert checks == [config]


def test_local_only_pipeline_never_calls_setup_or_data_when_unprepared(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    run = pipeline.Run(config, local_only=True, log=lambda _: None)
    monkeypatch.setattr(run, '_run', forbidden)
    for name in ('venv', 'packages', 'base', 'data', 'teacher', 'train', 'import_model'):
        monkeypatch.setattr(run, name, forbidden)
    assert run.execute(force=True) == pipeline.EXIT_WAITING
    assert json.loads((run.root / 'status.json').read_text())['stage'] == 'waiting_setup'
    assert not run.state_path.exists()


def test_local_only_stage_guards_survive_files_disappearing(tmp_path, monkeypatch):
    run = pipeline.Run(config_at(tmp_path), local_only=True, log=lambda _: None)
    monkeypatch.setattr(run, '_run', forbidden)
    monkeypatch.setattr(pipeline, 'gpu_present', forbidden)
    monkeypatch.setattr(pipeline.subprocess, 'run', forbidden)
    assert run.venv() is False
    assert run.packages() is False
    assert run.base() is None


def test_local_prerequisites_require_exact_selected_weights(tmp_path):
    config = config_at(tmp_path)
    provision(config)
    assert pipeline.local_prerequisites(config) == []
    config.values['llm_agent.size'] = '1.5b'
    assert pipeline.local_prerequisites(config) == ['selected model weights and tokenizer']


@pytest.mark.parametrize('damage', ['missing_shard', 'empty_shard', 'bad_index', 'missing_tokenizer', 'bad_marker'])
def test_partial_or_invalid_local_setup_does_not_start(tmp_path, damage):
    config = config_at(tmp_path)
    folder = provision(config, sharded=True)
    assert pipeline.local_prerequisites(config) == []
    if damage == 'missing_shard':
        (folder / 'model-00002-of-00002.safetensors').unlink()
    elif damage == 'empty_shard':
        (folder / 'model-00002-of-00002.safetensors').write_bytes(b'')
    elif damage == 'bad_index':
        (folder / 'model.safetensors.index.json').write_text('{bad json')
    elif damage == 'missing_tokenizer':
        (folder / 'tokenizer.json').unlink()
    else:
        (pipeline.work_root(config) / 'venv/neo_packages.json').write_text('[]')
    assert pipeline.local_prerequisites(config)
    assert jobs.start(config, local_only=True) is False


def test_wait_is_per_profile_and_size_bounded_and_retries_after_backoff(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(jobs.time, 'monotonic', lambda: now[0])
    config = config_at(tmp_path / 'one')
    assert not jobs.start(config, local_only=True)
    provision(config)
    assert not jobs.due(config, idle=3600, game=False)
    assert not jobs._waiting_for_setup(config_at(tmp_path / 'two'))
    config.values['llm_agent.size'] = '1.5b'
    assert not jobs._waiting_for_setup(config)
    config.values['llm_agent.size'] = '0.5b'
    now[0] += jobs._SETUP_RETRY_SECONDS + 1
    assert jobs.due(config, idle=3600, game=False)
    for index in range(20):
        jobs.start(config_at(tmp_path / str(index)), local_only=True)
    assert len(jobs._SETUP_WAIT) == 16


@pytest.mark.parametrize('local_only', [False, True])
def test_explicit_training_bypasses_wait_and_ready_automatic_job_is_local_only(tmp_path, monkeypatch, local_only):
    config = config_at(tmp_path)
    assert not jobs.start(config, local_only=True)
    if local_only:
        provision(config)
        monkeypatch.setattr(jobs.time, 'monotonic', lambda: float('inf'))
    calls = []
    process = SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(jobs.subprocess, 'Popen', lambda command, **kwargs: calls.append((command, kwargs)) or process)
    assert jobs.start(config, force=not local_only, local_only=local_only)
    command, kwargs = calls[0]
    assert ('--local-only' in command) is local_only
    assert ('--force' in command) is not local_only
    assert not jobs._SETUP_WAIT
    assert kwargs['stdout'].closed


def test_explicit_stages_keep_setup_and_download_capability(tmp_path, monkeypatch):
    run = pipeline.Run(config_at(tmp_path), log=lambda _: None)
    calls = []
    monkeypatch.setattr(run, '_run', lambda command, **kwargs: calls.append(command) or 1)
    from jarvis.runtime import process_host171
    monkeypatch.setattr(process_host171, 'base_python', lambda: Path('/fake/python'))
    monkeypatch.setattr(pipeline, 'gpu_present', lambda: False)
    assert run.venv() is False
    assert run.packages() is False
    assert run.base() is None
    assert any('-m' in command and 'venv' in command for command in calls)
    assert any('pip' in command and 'install' in command for command in calls)
    assert any(any('snapshot_download' in argument for argument in command) for command in calls)


def test_local_only_child_tools_are_forced_offline(tmp_path, monkeypatch):
    monkeypatch.setenv('OLLAMA_HOST', 'https://unrelated-remote.example')
    monkeypatch.setenv('HTTP_PROXY', 'https://unrelated-proxy.example')
    run = pipeline.Run(config_at(tmp_path), local_only=True, log=lambda _: None)
    calls = []
    process = SimpleNamespace(wait=lambda timeout=None: 0)
    monkeypatch.setattr(pipeline.subprocess, 'Popen', lambda command, **kwargs: calls.append(kwargs) or process)
    assert run._run(['fake-local-command']) == 0
    env = calls[0]['env']
    for key in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE', 'HF_DATASETS_OFFLINE', 'HF_HUB_DISABLE_TELEMETRY', 'PIP_NO_INDEX'):
        assert env[key] == '1'
    assert env['OLLAMA_HOST'] == 'http://localhost:11434'
    assert env['NO_PROXY'] == env['no_proxy'] == '*'


def test_pipeline_cli_propagates_local_only_and_force(tmp_path, monkeypatch):
    from jarvis import config as config_module
    config = config_at(tmp_path)
    monkeypatch.setattr(config_module, 'ConfigManager', lambda _: config)
    calls = []

    class FakeRun:
        def __init__(self, value, *, local_only=False):
            calls.append((value, local_only))

        def execute(self, *, force=False):
            calls.append(force)
            return pipeline.EXIT_WAITING

    monkeypatch.setattr(pipeline, 'Run', FakeRun)
    assert pipeline.main(['run', '--local-only', '--force']) == pipeline.EXIT_WAITING
    assert calls == [(config, True), True]


@pytest.mark.parametrize('language', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_waiting_status_is_actionable_and_localized(tmp_path, monkeypatch, language):
    from jarvis.tools import assistant_model_tool170 as tool
    config = config_at(tmp_path, **{'app.language': language})
    monkeypatch.setattr(jobs, 'read_status', lambda _: {'running': False, 'status': {
        'stage': 'waiting_setup', 'missing': ['training packages'], 'note': 'private raw path must not appear'}})
    result = tool.run(config, {'action': 'status'}, ollama=SimpleNamespace(resolve_agent_model=lambda: 'neo-agent'))
    assert tool._t('waiting_setup', language) in result.text
    assert tool._t('never', language) not in result.text
    assert 'private raw path' not in result.text
    assert result.data['status']['status']['missing'] == ['training packages']


def test_due_does_not_read_unbounded_history(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    monkeypatch.setattr(jobs, 'read_status', forbidden)
    assert jobs.due(config, idle=3600, game=False)
    config.values['llm_agent.finetune.auto'] = False
    assert not jobs.due(config, idle=3600, game=False)


@pytest.mark.parametrize('url,allowed', [
    ('http://localhost:11434', True), ('http://127.0.0.1:11434', True), ('http://[::1]:11434', True),
    ('https://models.example:11434', False), ('http://192.168.1.5:11434', False),
    ('http://localhost.example:11434', False), ('http://localhost:bad', False),
    ('http://user:secret@localhost:11434', False), ('file:///localhost', False),
])
def test_automatic_endpoint_must_be_loopback_without_dns(tmp_path, monkeypatch, url, allowed):
    config = config_at(tmp_path, **{'ollama.base_url': url})
    provision(config)
    assert pipeline.loopback_endpoint(url) is allowed
    assert ('local Ollama endpoint' in pipeline.local_prerequisites(config)) is not allowed


def test_automatic_request_never_uses_remote_endpoint_proxy_or_redirect(monkeypatch):
    import requests
    calls = []
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: {'message': {'content': 'local reply'}})
    session = SimpleNamespace(trust_env=True, close=lambda: None,
                              post=lambda *args, **kwargs: calls.append((args, kwargs)) or response)
    monkeypatch.setattr(requests, 'Session', lambda: session)
    monkeypatch.setattr(requests, 'post', forbidden)
    assert pipeline.ask_model('http://external.example', 'test', [], schema=None, local_only=True) is None
    assert calls == []
    assert pipeline.ask_model('http://127.0.0.1:11434', 'test', [], schema=None, local_only=True) == 'local reply'
    assert session.trust_env is False
    assert calls[0][1]['allow_redirects'] is False


@pytest.mark.parametrize('language', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_remote_endpoint_blocker_is_localized_without_private_address(tmp_path, monkeypatch, language):
    from jarvis.tools import assistant_model_tool170 as tool
    config = config_at(tmp_path, **{'app.language': language, 'ollama.base_url': 'https://private.example'})
    monkeypatch.setattr(jobs, 'read_status', lambda _: {'running': False, 'status': {
        'stage': 'waiting_setup', 'missing': ['local Ollama endpoint']}})
    result = tool.run(config, {'action': 'status'}, ollama=SimpleNamespace(resolve_agent_model=lambda: 'neo-agent'))
    assert tool._t('waiting_local', language) in result.text
    assert 'private.example' not in result.text


def test_manual_remote_endpoint_remains_explicit_and_supported(tmp_path, monkeypatch):
    config = config_at(tmp_path, **{'ollama.base_url': 'https://explicit.example'})
    assert not jobs.start(config, local_only=True)
    process = SimpleNamespace(pid=123, poll=lambda: None)
    monkeypatch.setattr(jobs.subprocess, 'Popen', lambda *args, **kwargs: process)
    assert jobs.start(config, force=True)
