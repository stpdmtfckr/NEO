"""Exercise real command/configuration paths with inert models and blocked external I/O."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jarvis.neo_llm import jobs, pipeline, setup, train_lora
from jarvis.testing.isolation import external_io_blocked


class Config:
    def __init__(self, root, **values):
        self.paths = SimpleNamespace(root=root)
        self.data = {}
        self.writes = 0
        for key, value in values.items():
            target = self.data
            *parents, last = key.split('.')
            for name in parents:
                target = target.setdefault(name, {})
            target[last] = value

    def get(self, key, default=None):
        node = self.data
        for name in key.split('.'):
            if not isinstance(node, dict) or name not in node:
                return default
            node = node[name]
        return node

    def update(self, mutate):
        data = deepcopy(self.data)
        if mutate(data) is False:
            return False
        self.data = data
        self.writes += 1
        return True


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'roaming'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    with external_io_blocked(), patch.object(os, 'system', side_effect=AssertionError('No shell')), \
            patch.object(socket.socket, 'sendto', side_effect=AssertionError('No UDP')):
        yield


def config_at(tmp_path, **values):
    return Config(tmp_path, **values)


def worker_at(tmp_path, **values):
    config = config_at(tmp_path, **values)
    worker = pipeline.Run(config, log=lambda _: None)
    worker.state['run_key'] = f'{worker.hf_id}|corrections=0|v170'
    return worker


@pytest.mark.parametrize('size,hf', [('0.5b', 'Qwen/Qwen2.5-0.5B-Instruct'),
                                     ('1.5b', 'Qwen/Qwen2.5-1.5B-Instruct')])
def test_download_and_trainer_commands_use_exact_real_weights(tmp_path, monkeypatch, size, hf):
    values = {} if size == '0.5b' else {'llm_agent.size': size, 'llm_agent.base_model': 'qwen2.5:1.5b'}
    worker = worker_at(tmp_path, **values)
    calls = []

    def capture(command, **kwargs):
        calls.append(command)
        if 'snapshot_download' in ' '.join(command):
            folder = Path(command[-1])
            folder.mkdir(parents=True)
            (folder / 'config.json').write_text('{}')
        return 0

    monkeypatch.setattr(worker, '_run', capture)
    monkeypatch.setattr(worker, '_gpu', lambda: False)
    base = worker.base()
    assert worker.hf_id == hf and calls[0][-2] == hf
    assert base == pipeline.base_folder(worker.config)
    assert worker.train(base, tmp_path / 'data') == 0
    command = calls[1]
    assert Path(command[1]).name == 'train_lora.py'
    assert command[command.index('--base') + 1] == str(base)
    assert command[command.index('--cpu-threads') + 1] == '2'
    assert command[command.index('--out') + 1] == str(worker.run_root)
    assert worker.state['trained_key'] == worker.state['run_key']
    assert worker.config.writes == 0


def test_packaged_default_selects_smallest_real_training_weights(tmp_path):
    config = config_at(tmp_path)
    config.data = json.loads((Path(__file__).parents[1] / 'config/default_settings.json').read_text())
    worker = pipeline.Run(config, log=lambda _: None)
    assert worker.size == '0.5b'
    assert worker.base_model == 'qwen2.5:0.5b'
    assert worker.hf_id == 'Qwen/Qwen2.5-0.5B-Instruct'
    assert config.get('llm_agent.finetune.cpu_threads') == 2


@pytest.mark.parametrize('values', [
    {'llm_agent.size': '7b'},
    {'llm_agent.base_model': 'custom:8b'},
    {'llm_agent.size': '0.5b', 'llm_agent.base_model': 'qwen2.5:1.5b'},
    {'llm_agent.size': '1.5b', 'llm_agent.base_model': 'qwen2.5:0.5b'},
])
def test_unsupported_custom_training_base_is_preserved_and_never_substituted(tmp_path, values):
    config = config_at(tmp_path, **values)
    before = deepcopy(config.data)
    with pytest.raises(ValueError, match='Unsupported|configured base'):
        pipeline.Run(config, log=lambda _: None)
    assert pipeline.local_prerequisites(config)
    assert not jobs.start(config, local_only=True)
    assert setup.ensure(config=config, write_settings=False, log=lambda _: None) == 6
    assert config.data == before and config.writes == 0


def test_default_setup_cli_and_api_use_smallest_base_without_training(tmp_path, monkeypatch):
    from jarvis import ollama_runtime
    calls = []
    monkeypatch.setattr(ollama_runtime, 'ensure_ollama_running', lambda *args, **kwargs: None)
    monkeypatch.setattr(setup, 'installed', lambda _: [])
    monkeypatch.setattr(setup, 'pull', lambda url, base, log: calls.append(('pull', base)))
    monkeypatch.setattr(setup, 'create', lambda url, name, base, log: calls.append(('create', name, base)))
    monkeypatch.setattr(setup, 'smoke_test', lambda url, name: 0.0)
    assert setup.ensure(write_settings=False, log=lambda _: None) == 0
    assert calls == [('pull', 'qwen2.5:0.5b'), ('create', 'neo-agent', 'qwen2.5:0.5b')]
    calls.clear()
    monkeypatch.setattr(setup, 'ensure', lambda size, **kwargs: calls.append((size, kwargs)) or 0)
    assert setup.main(['--no-settings']) == 0
    assert calls[0][0] == '0.5b'


def test_default_background_setup_and_custom_base_guard(tmp_path, monkeypatch):
    from jarvis.runtime import agent_llm170
    calls = []

    class Thread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

        def is_alive(self):
            return False

    monkeypatch.setattr(setup, '_BACKGROUND', {'thread': None, 'status': '', 'code': None})
    monkeypatch.setattr('threading.Thread', Thread)
    monkeypatch.setattr(agent_llm170, 'reset_availability', lambda: None)
    monkeypatch.setattr(setup, 'ensure', lambda size, **kwargs: calls.append(size) or 0)
    config = config_at(tmp_path)
    assert setup.ensure_background(config)
    assert calls == ['0.5b'] and config.get('llm_agent.base_model') == 'qwen2.5:0.5b'
    custom = config_at(tmp_path / 'custom', **{'llm_agent.base_model': 'deliberate:7b'})
    assert not setup.ensure_background(custom, force=True)
    assert calls == ['0.5b'] and custom.get('llm_agent.base_model') == 'deliberate:7b'


@pytest.mark.parametrize('timing', ['queued', 'late'])
@pytest.mark.parametrize('key,value', [
    ('llm_agent.size', '1.5b'),
    ('llm_agent.base_model', 'custom:7b'),
    ('llm_agent.model', 'custom-planner:7b'),
    ('llm_agent.fine_tuned', 'custom-trained:7b'),
    ('ollama.base_url', 'http://127.0.0.1:22222'),
])
def test_background_setup_preserves_queued_and_late_selection_changes(tmp_path, monkeypatch, timing, key, value):
    from jarvis.runtime import agent_llm170
    callbacks, calls, resets = [], [], []
    config = config_at(tmp_path, **{'llm_agent.size': '0.5b', 'llm_agent.base_model': 'qwen2.5:0.5b',
                                 'llm_agent.model': 'neo-agent', 'llm_agent.fine_tuned': '',
                                 'ollama.base_url': 'http://127.0.0.1:11111'})

    class Thread:
        def __init__(self, target, **kwargs):
            callbacks.append(target)

        def start(self):
            pass

        def is_alive(self):
            return True

    def change_selection():
        section, setting = key.split('.', 1)
        config.data[section][setting] = value

    def ensure(size, *, config: object, **kwargs):
        calls.append((size, config.get('ollama.base_url')))
        if timing == 'late':
            change_selection()
        # The backend operation keeps its original server even after live edits.
        calls.append((size, config.get('ollama.base_url')))
        return 0

    monkeypatch.setattr(setup, '_BACKGROUND', {'thread': None, 'status': '', 'code': None})
    monkeypatch.setattr('threading.Thread', Thread)
    monkeypatch.setattr(setup, 'ensure', ensure)
    monkeypatch.setattr(agent_llm170, 'reset_availability', lambda: resets.append(True))
    assert setup.ensure_background(config, size='1.5b', force=True)
    if timing == 'queued':
        change_selection()
    callbacks[0]()
    assert config.get(key) == value
    # Explicit setup for 1.5b must not replace newer unrelated selection fields.
    if key != 'llm_agent.base_model':
        assert config.get('llm_agent.base_model') == 'qwen2.5:0.5b'
    if key != 'llm_agent.size':
        assert config.get('llm_agent.size') == '0.5b'
    assert not resets
    if timing == 'queued':
        assert calls == [] and setup._BACKGROUND['code'] == 7
    else:
        assert calls == [('1.5b', 'http://127.0.0.1:11111')] * 2
        assert setup._BACKGROUND['code'] == 0


def test_background_setup_allows_unrelated_theme_edits(tmp_path, monkeypatch):
    from jarvis.runtime import agent_llm170
    callbacks = []
    config = config_at(tmp_path, **{'llm_agent.size': '0.5b', 'llm_agent.base_model': 'qwen2.5:0.5b'})

    class Thread:
        def __init__(self, target, **kwargs):
            callbacks.append(target)

        def start(self):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(setup, '_BACKGROUND', {'thread': None, 'status': '', 'code': None})
    monkeypatch.setattr('threading.Thread', Thread)
    monkeypatch.setattr(setup, 'ensure', lambda *args, **kwargs: 0)
    monkeypatch.setattr(agent_llm170, 'reset_availability', lambda: None)
    assert setup.ensure_background(config, size='1.5b', force=True)
    config.data['ui'] = {'theme': 'new user choice'}
    callbacks[0]()
    assert config.get('llm_agent.size') == '1.5b'
    assert config.get('llm_agent.base_model') == 'qwen2.5:1.5b'
    assert config.get('ui.theme') == 'new user choice'


def test_checkpoints_for_other_bases_and_legacy_runs_are_never_reused(tmp_path, monkeypatch):
    worker = worker_at(tmp_path)
    legacy = worker.root / 'run/checkpoint/adapter_config.json'
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"base_model_name_or_path":"Qwen/Qwen2.5-1.5B-Instruct"}')
    larger = worker_at(tmp_path, **{'llm_agent.size': '1.5b'})
    larger_checkpoint = larger.run_root / 'checkpoint/adapter_config.json'
    larger_checkpoint.parent.mkdir(parents=True)
    larger_checkpoint.write_text('large checkpoint')
    commands = []
    monkeypatch.setattr(worker, '_gpu', lambda: False)
    monkeypatch.setattr(worker, '_run', lambda command, **kwargs: commands.append(command) or 0)
    worker.train(pipeline.base_folder(worker.config), tmp_path / 'data')
    output = Path(commands[0][commands[0].index('--out') + 1])
    assert output == worker.run_root and output != legacy.parents[1] and output != larger.run_root
    assert not (output / 'checkpoint').exists()
    assert larger_checkpoint.read_text() == 'large checkpoint'
    assert '1.5B' in legacy.read_text()


def test_base_change_retrains_even_without_new_corrections(tmp_path, monkeypatch):
    worker = worker_at(tmp_path)
    old_key = 'Qwen/Qwen2.5-1.5B-Instruct|corrections=0|v170'
    worker.state_path.write_text(json.dumps({'run_key': old_key, 'finished_key': old_key}))
    (worker.root / 'status.json').write_text('{"stage":"done"}')
    monkeypatch.setattr(jobs, 'running', lambda: False)
    monkeypatch.setattr(jobs, '_waiting_for_setup', lambda _: False)
    monkeypatch.setattr(jobs, '_new_corrections', lambda *args: False)
    assert jobs.due(worker.config, idle=3600, game=False)
    current_key = worker.state['run_key']
    worker.state_path.write_text(json.dumps({'run_key': current_key, 'finished_key': current_key}))
    assert not jobs.due(worker.config, idle=3600, game=False)


@pytest.mark.parametrize('threads,cpus,expected', [(None, 32, 2), (None, 1, 1), (4, 32, 4), (16, 2, 2)])
def test_inert_trainer_loads_selected_base_with_bounded_cpu_and_lora(tmp_path, monkeypatch, threads, cpus, expected):
    seen = {}
    model = SimpleNamespace(config=SimpleNamespace(use_cache=True), parameters=lambda: [],
                            named_modules=lambda: [(f'layer.{name}', None) for name in
                                ('q_proj', 'k_proj', 'v_proj', 'o_proj', 'gate_proj', 'up_proj', 'down_proj')],
                            to=lambda device: seen.update(device=device))
    tokenizer = SimpleNamespace()

    def load_model(base, **kwargs):
        seen.update(model_base=base, model_kwargs=kwargs)
        return model

    def load_tokenizer(base, **kwargs):
        seen['tokenizer_base'] = base
        return tokenizer

    def lora(**kwargs):
        seen['lora'] = kwargs
        return kwargs

    torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False), float32='float32',
                            set_num_threads=lambda n: seen.update(threads=n),
                            set_num_interop_threads=lambda n: seen.update(interop=n))
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(
        AutoModelForCausalLM=SimpleNamespace(from_pretrained=load_model),
        AutoTokenizer=SimpleNamespace(from_pretrained=load_tokenizer)))
    monkeypatch.setitem(sys.modules, 'peft', SimpleNamespace(LoraConfig=lora, PeftModel=SimpleNamespace(),
                                                           get_peft_model=lambda base, settings: base))
    monkeypatch.setattr(train_lora.os, 'cpu_count', lambda: cpus)
    data = tmp_path / 'empty.jsonl'
    data.write_text('')
    base = str(tmp_path / 'Qwen__Qwen2.5-0.5B-Instruct')
    argv = ['--data', str(data), '--base', base, '--out', str(tmp_path / 'out')]
    if threads is not None:
        argv += ['--cpu-threads', str(threads)]
    assert train_lora.main(argv) == 2  # stop before optimizer creation; no training or downloads
    assert seen['model_base'] == seen['tokenizer_base'] == base
    assert seen['model_kwargs']['torch_dtype'] == 'float32'
    assert seen['threads'] == expected and seen['interop'] == 1
    assert seen['lora']['task_type'] == 'CAUSAL_LM'
    assert seen['lora']['r'] == 16 and 'q_proj' in seen['lora']['target_modules']
    assert model.config.use_cache is False


@pytest.mark.parametrize('value', ['0', '17', '-1', 'bad'])
def test_cpu_thread_limit_rejects_invalid_values_before_import_or_training(tmp_path, value):
    with pytest.raises(SystemExit) as exc:
        train_lora.main(['--data', 'unused', '--base', 'unused', '--out', str(tmp_path), '--cpu-threads', value])
    assert exc.value.code == 2


def test_training_only_labels_assistant_tokens():
    tokenizer = SimpleNamespace(apply_chat_template=lambda messages, **kwargs:
                                [10, 20, 30, 40] if messages[-1]['role'] == 'assistant' else [10, 20])
    messages = [{'role': 'user', 'content': 'test'}, {'role': 'assistant', 'content': 'answer'}]
    assert train_lora._encode(tokenizer, messages, 10) == ([10, 20, 30, 40], [-100, -100, 30, 40])


@pytest.mark.parametrize('local_only', [False, True])
def test_teacher_resolves_exact_runtime_selection_and_closes_own_session(tmp_path, monkeypatch, local_only):
    import jarvis.ollama_client as module
    worker = pipeline.Run(config_at(tmp_path), local_only=local_only, log=lambda _: None)
    seen = {}
    original_proof = {'neo-agent-ft': {'model_digest': 'original'}}
    worker.selected_provenance = deepcopy(original_proof)
    worker.config.data.setdefault('llm_agent', {})['fine_tuned_provenance'] = {'neo-agent-ft': {'model_digest': 'newer'}}

    class Client:
        def __init__(self, config):
            seen['config'] = config
            self._session = SimpleNamespace(close=lambda: seen.update(closed=True))

        def resolve_agent_model(self):
            return 'qwen2.5:0.5b'

    monkeypatch.setattr(module, 'OllamaClient', Client)
    assert worker.current_model() == 'qwen2.5:0.5b'
    assert seen['closed'] and seen['config'].get('ollama.base_url') == worker.base_url
    assert seen['config'].get('llm_agent.fine_tuned_provenance') == original_proof
    if local_only:
        assert seen['config'].get('ollama.auto_start') is False
        assert seen['config'].get('llm_agent.auto_install') is False


@pytest.mark.parametrize('digest', ['a' * 64, 'sha256:' + 'a' * 64])
def test_model_identity_uses_exact_manifest_digest_without_proxy_or_redirect(tmp_path, monkeypatch, digest):
    import requests
    calls = []
    response = SimpleNamespace(raise_for_status=lambda: None, close=lambda: None, json=lambda: {'models': [
        {'name': 'neo-agent-ft-other:latest', 'digest': 'b' * 64},
        {'name': 'neo-agent-ft:latest', 'digest': digest}]})
    session = SimpleNamespace(trust_env=True, close=lambda: None,
                              get=lambda *args, **kwargs: calls.append((args, kwargs)) or response)
    monkeypatch.setattr(requests, 'Session', lambda: session)
    assert pipeline.model_digest('http://localhost:11434', pipeline.FINE_TUNED, local_only=True) == 'sha256:' + 'a' * 64
    assert not session.trust_env and calls[0][1]['allow_redirects'] is False
    calls.clear()
    assert pipeline.model_digest('https://external.example', pipeline.FINE_TUNED, local_only=True) == ''
    assert not calls


def test_import_uses_small_merged_weights_and_captures_manifest_identity(tmp_path, monkeypatch):
    worker = worker_at(tmp_path)
    merged = worker.run_root / 'merged'
    merged.mkdir(parents=True)
    (merged / 'config.json').write_text('{}')
    calls = []
    monkeypatch.setattr(pipeline, 'ollama_exe', lambda: 'inert-ollama')
    monkeypatch.setattr(pipeline, 'model_digest', lambda *args, **kwargs: 'sha256:' + 'a' * 64)
    monkeypatch.setattr(worker, '_run', lambda command, **kwargs: calls.append(command) or 0)
    assert worker.import_model()
    assert worker.candidate.startswith('neo-agent-ft-run-')
    assert calls[0] == ['inert-ollama', 'create', worker.candidate, '-f', str(worker.run_root / 'Modelfile'), '--quantize', 'q4_K_M']
    assert (worker.run_root / 'Modelfile').read_text().startswith('FROM ./merged\n')
    assert worker.imported_digest == 'sha256:' + 'a' * 64
    assert worker.config.writes == 0


def promotion_worker(tmp_path, monkeypatch):
    worker = worker_at(tmp_path)
    worker.imported_digest = 'sha256:' + 'a' * 64
    worker.candidate = 'neo-agent-ft-run-' + 'e' * 32
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'eval.jsonl').write_text('{"meta":{"kind":"single"}}\n')
    calls = []
    monkeypatch.setattr(worker, 'current_model', lambda: 'qwen2.5:0.5b')
    monkeypatch.setattr(pipeline, 'ollama_exe', lambda: 'inert-ollama')
    monkeypatch.setattr(pipeline, 'model_digest', lambda *args, **kwargs: worker.imported_digest)
    monkeypatch.setattr(pipeline, 'evaluate', lambda url, model, rows, **kwargs:
                        {'do_exact': 0.9 if model == worker.candidate else 0.5, 'json_ok': 1.0, 'chat_ok': 1.0})
    monkeypatch.setattr(worker, '_run', lambda command, **kwargs: calls.append(command) or 0)
    return worker, data, calls


def test_successful_evaluated_promotion_saves_bound_provenance_atomically(tmp_path, monkeypatch):
    worker, data, calls = promotion_worker(tmp_path, monkeypatch)
    worker.config.data['ui'] = {'theme': 'new user choice'}
    report = worker.decide(data)
    assert report['promoted'] and worker.config.writes == 1
    assert calls == []  # Selecting a staged alias never overwrites a live one.
    assert worker.config.get('llm_agent.fine_tuned') == worker.candidate
    assert worker.config.get('ui.theme') == 'new user choice'
    assert worker.config.get('llm_agent.fine_tuned_provenance')[worker.candidate] == {
        'hf_base': worker.hf_id, 'ollama_base': 'qwen2.5:0.5b', 'model_digest': worker.imported_digest,
        'base_url': worker.base_url, 'run_key': worker.state['run_key']}


@pytest.mark.parametrize('failure', ['before_evaluation', 'during_evaluation', 'score', 'settings_changed'])
def test_promotion_failures_never_record_untrusted_weights(tmp_path, monkeypatch, failure):
    worker, data, calls = promotion_worker(tmp_path, monkeypatch)
    if failure in {'before_evaluation', 'during_evaluation'}:
        values = iter(([''] if failure == 'before_evaluation' else
                       [worker.imported_digest, 'sha256:' + 'b' * 64]))
        monkeypatch.setattr(pipeline, 'model_digest', lambda *args, **kwargs: next(values))
    elif failure == 'score':
        monkeypatch.setattr(pipeline, 'evaluate', lambda *args, **kwargs: {'do_exact': 0.5, 'json_ok': 1.0, 'chat_ok': 1.0})
    elif failure == 'settings_changed':
        worker.config.data['llm_agent'] = {'size': '1.5b', 'base_model': 'deliberate:7b'}
    if failure == 'before_evaluation':
        with pytest.raises(RuntimeError, match='before evaluation'):
            worker.decide(data)
    else:
        assert not worker.decide(data)['promoted']
    assert worker.config.writes == 0
    assert not worker.config.get('llm_agent.fine_tuned_provenance')
    assert not worker.config.get('llm_agent.fine_tuned')
    if failure in {'before_evaluation', 'during_evaluation', 'score'}:
        assert not calls
    if failure == 'settings_changed':
        assert worker.config.get('llm_agent.base_model') == 'deliberate:7b'


def test_previous_trained_provenance_keeps_original_rollback_alias(tmp_path, monkeypatch):
    worker, data, calls = promotion_worker(tmp_path, monkeypatch)
    old_digest = 'sha256:' + 'b' * 64
    old_proof = {'hf_base': worker.hf_id, 'ollama_base': worker.base_model, 'model_digest': old_digest,
                 'base_url': worker.base_url, 'run_key': worker.hf_id + '|corrections=1|v170'}
    worker.selected_agent = ('neo-agent', pipeline.FINE_TUNED)
    worker.selected_provenance = {pipeline.FINE_TUNED: deepcopy(old_proof)}
    worker.config.data['llm_agent'] = {'fine_tuned': pipeline.FINE_TUNED,
                                     'fine_tuned_provenance': {pipeline.FINE_TUNED: deepcopy(old_proof)}}
    assert worker.decide(data)['promoted']
    assert calls == []
    assert worker.config.get('llm_agent.fine_tuned_previous') == pipeline.FINE_TUNED
    assert worker.config.get('llm_agent.fine_tuned_provenance')[pipeline.FINE_TUNED] == old_proof
    assert worker.config.get('llm_agent.fine_tuned_provenance')[worker.candidate]['model_digest'] == worker.imported_digest


def test_real_teacher_resolver_ignores_stale_larger_alias(tmp_path, monkeypatch):
    import requests
    seen = []
    session = SimpleNamespace(close=lambda: seen.append('closed'), trust_env=True,
                              get=lambda *args, **kwargs: SimpleNamespace(raise_for_status=lambda: None,
                                  json=lambda: {'models': [{'name': name} for name in
                                      ['neo-agent:latest', 'neo-agent-ft:latest', 'qwen2.5:0.5b', 'qwen2.5:1.5b']]}),
                              post=lambda *args, **kwargs: SimpleNamespace(raise_for_status=lambda: None,
                                  json=lambda: {'modelfile': 'FROM qwen2.5:1.5b\n'}))
    monkeypatch.setattr(requests, 'Session', lambda: session)
    worker = worker_at(tmp_path, **{'llm_agent.fine_tuned': 'neo-agent-ft'})
    assert worker.current_model() == 'qwen2.5:0.5b'
    assert seen == ['closed']


@pytest.mark.parametrize('value', [0, 17, -1])
def test_pipeline_invalid_cpu_limits_cannot_launch_trainer(tmp_path, monkeypatch, value):
    worker = worker_at(tmp_path, **{'llm_agent.finetune.cpu_threads': value})
    monkeypatch.setattr(worker, '_gpu', lambda: False)
    with pytest.raises(ValueError, match='between 1 and 16'):
        worker.train(pipeline.base_folder(worker.config), tmp_path / 'data')
