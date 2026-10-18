"""Independent review: blocked external I/O, real ConfigManager, inert backend."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from jarvis.testing.isolation import external_io_blocked

from jarvis.config import ConfigManager
from jarvis.neo_llm import setup, pipeline
from jarvis.runtime import agent_llm170, model_life173
from jarvis.ollama_client import OllamaClient, OllamaError

ROOT = Path(__file__).resolve().parents[1]
SMALL = 'qwen2.5:0.5b'

@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    for key in ('HOME', 'LOCALAPPDATA', 'APPDATA', 'XDG_CACHE_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setattr(ConfigManager, 'apply_autostart', lambda *args: None)
    monkeypatch.setattr(ConfigManager, 'apply_wake_service', lambda *args: None)
    monkeypatch.setattr(setup, '_BACKGROUND', {'thread': None, 'status': '', 'code': None})
    monkeypatch.setattr(agent_llm170, 'reset_availability', lambda: None)
    model_life173._PRELOAD_INFLIGHT[0] = False
    model_life173._LAST_PRELOAD[0] = 0
    with external_io_blocked():
        yield

def config():
    return ConfigManager(ROOT / 'config/default_settings.json')

class PendingThread:
    callbacks = []
    def __init__(self, target, **kwargs): self.callbacks.append(target)
    def start(self): pass
    def is_alive(self): return True

def test_late_setup_completion_cannot_race_real_settings_save(monkeypatch):
    """A GUI save can run between the last selection getter and the in-memory write."""
    c = config()
    callbacks = []
    class Thread(PendingThread): pass
    Thread.callbacks = callbacks
    monkeypatch.setattr('threading.Thread', Thread)
    monkeypatch.setattr(setup, 'ensure', lambda *args, **kwargs: 0)
    assert setup.ensure_background(c, size='1.5b', force=True)
    original_get = c.get
    count = 0
    def interleaving_get(key, default=None):
        nonlocal count
        value = original_get(key, default)
        if key == 'llm_agent.fine_tuned':
            count += 1
            if count == 2:  # Final getter in the completion comparison
                newer = deepcopy(c.data)
                newer['llm_agent'].update(size='0.5b', base_model='my-deliberate:7b')
                c.save(newer)
        return value
    monkeypatch.setattr(c, 'get', interleaving_get)
    callbacks[0]()
    assert json.loads(c.paths.settings.read_text())['llm_agent']['base_model'] == 'my-deliberate:7b'
    assert c.get('llm_agent.base_model') == 'my-deliberate:7b'

def test_stale_training_does_not_replace_newer_selected_weights(monkeypatch):
    """Rejecting a stale settings write must precede replacing a live model alias."""
    c = config()
    c.data['llm_agent']['fine_tuned'] = pipeline.FINE_TUNED
    c.save(c.data)
    worker = pipeline.Run(c, log=lambda _: None)
    candidate_digest = 'sha256:' + 'a' * 64
    current_digest = 'sha256:' + 'b' * 64
    worker.imported_digest = candidate_digest
    worker.state['run_key'] = worker.hf_id + '|corrections=1|v170'
    current = ConfigManager(ROOT / 'config/default_settings.json')
    def newer(data):
        data['llm_agent'].update(size='1.5b', base_model='qwen2.5:1.5b',
            fine_tuned_provenance={pipeline.FINE_TUNED: {
                'hf_base': 'Qwen/Qwen2.5-1.5B-Instruct', 'ollama_base': 'qwen2.5:1.5b',
                'model_digest': current_digest, 'base_url': worker.base_url, 'run_key': 'newer-run'}})
    current.update(newer)
    weights = {pipeline.CANDIDATE: candidate_digest, pipeline.FINE_TUNED: current_digest}
    commands = []
    monkeypatch.setattr(pipeline, 'model_digest', lambda url, model, **kw: weights.get(model, ''))
    def copy(command, **kwargs):
        commands.append(command)
        weights[command[3]] = weights[command[2]]
        return 0
    monkeypatch.setattr(worker, '_run', copy)
    success, reason = worker._promote('inert-ollama')
    assert not success and 'settings changed' in reason.lower()
    assert c.get('llm_agent.base_model') == 'qwen2.5:1.5b'
    assert weights[pipeline.FINE_TUNED] == current_digest, commands

class Response:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def raise_for_status(self): pass
    def json(self): return self.payload
    def close(self): pass

@pytest.mark.parametrize('revision', [None, 1])
@pytest.mark.parametrize('small_present', [False, True])
def test_contradictory_saved_alias_never_warms_old_four_b(monkeypatch, revision, small_present):
    c = config()
    data = deepcopy(c.data)
    if revision is None: data.pop('model_defaults_revision', None)
    data['ollama']['chat_model'] = SMALL
    data['llm_agent'].update(size='0.5b', base_model=SMALL, fine_tuned='neo-agent-ft', allow_large_fallback=True)
    c.save(data)
    c = config()
    client = OllamaClient(c)
    calls = []
    names = ['neo-agent-ft:latest', 'neo-agent:latest', 'qwen3.5:4b'] + ([SMALL] if small_present else [])
    def get(url, **kwargs):
        return Response({'models': [] if url.endswith('/api/ps') else [{'name': name} for name in names]})
    def post(url, **kwargs):
        calls.append((url, kwargs.get('json')))
        return Response({'modelfile': 'FROM qwen3.5:4b', 'message': {'content': '{"reply":"inert"}'}})
    client._session = SimpleNamespace(get=get, post=post, close=lambda: None)
    if small_present:
        assert client.resolve_agent_model() == client.resolve_chat_model() == SMALL
    else:
        with pytest.raises(OllamaError): client.resolve_agent_model()
    model_life173.preload(client, background=False, now=100)
    generated = [body for url, body in calls if url.endswith('/api/generate')]
    assert all(body['model'] == SMALL for body in generated)
    assert len(generated) == int(small_present)


def staged_worker(monkeypatch):
    c = config()
    old_digest = 'sha256:' + 'b' * 64
    old_proof = {'hf_base': 'Qwen/Qwen2.5-0.5B-Instruct', 'ollama_base': SMALL,
                 'base_url': c.get('ollama.base_url'), 'run_key': 'old', 'model_digest': old_digest}
    c.data['llm_agent'].update(fine_tuned=pipeline.FINE_TUNED,
                             fine_tuned_provenance={pipeline.FINE_TUNED: old_proof})
    c.save(c.data)
    worker = pipeline.Run(c, log=lambda _: None)
    worker.candidate = 'neo-agent-ft-run-' + 'a' * 32
    worker.imported_digest = 'sha256:' + 'a' * 64
    worker.state['run_key'] = worker.hf_id + '|new'
    weights = {worker.candidate: worker.imported_digest, pipeline.FINE_TUNED: old_digest}
    def digest(url, name, **kw):
        import jarvis.config as cm
        assert not cm._SAVE_LOCK182._is_owned(), 'Backend read under GUI settings lock'
        return weights.get(name, '')
    monkeypatch.setattr(pipeline, 'model_digest', digest)
    monkeypatch.setattr(worker, '_run', lambda *a, **kw: pytest.fail('Promotion must not mutate backend aliases'))
    return c, worker, weights, old_proof


def test_successful_promotion_changes_only_selection_and_keeps_rollback_pointer(monkeypatch):
    from jarvis.neo_llm import jobs
    c, worker, weights, old_proof = staged_worker(monkeypatch)
    original = weights.copy()
    assert worker._promote('unused')[0]
    assert weights == original
    assert c.get('llm_agent.fine_tuned') == worker.candidate
    assert c.get('llm_agent.fine_tuned_previous') == pipeline.FINE_TUNED
    assert c.get('llm_agent.fine_tuned_provenance')[pipeline.FINE_TUNED] == old_proof
    c.reload()
    assert c.get('llm_agent.fine_tuned') == worker.candidate
    monkeypatch.setattr(jobs, '_model_exists', lambda *a: True)
    assert jobs.rollback(c) == pipeline.FINE_TUNED
    assert c.get('llm_agent.fine_tuned_previous') == '' and weights == original
    assert jobs.rollback(c) == 'neo-agent'


@pytest.mark.parametrize('change', ['base', 'custom_model', 'proof_only', 'theme'])
def test_selection_change_during_backend_verification_is_atomic(monkeypatch, change):
    c, worker, weights, _ = staged_worker(monkeypatch)
    original = weights.copy()
    other = config()
    def read_digest(url, name, **kw):
        import jarvis.config as cm
        assert not cm._SAVE_LOCK182._is_owned()
        def update(data):
            if change == 'base': data['llm_agent'].update(size='1.5b', base_model='qwen2.5:1.5b')
            elif change == 'custom_model': data['llm_agent']['model'] = 'new-custom:7b'
            elif change == 'proof_only': data['llm_agent']['fine_tuned_provenance'][pipeline.FINE_TUNED]['run_key'] = 'newer'
            else: data['ui']['theme'] = 'new-theme'
        other.update(update)
        return weights.get(name, '')
    monkeypatch.setattr(pipeline, 'model_digest', read_digest)
    success, reason = worker._promote('unused')
    assert success == (change == 'theme')
    assert weights == original
    if change != 'theme':
        assert 'settings changed' in reason.lower()
        assert c.get('llm_agent.fine_tuned') == pipeline.FINE_TUNED
    else:
        assert c.get('ui.theme') == 'new-theme'
        assert c.get('llm_agent.fine_tuned') == worker.candidate


@pytest.mark.parametrize('failure_stage', ['before_replace', 'post_commit'])
def test_failed_settings_persistence_never_changes_existing_weights(monkeypatch, failure_stage):
    import jarvis.config as cm
    c, worker, weights, _ = staged_worker(monkeypatch)
    original = weights.copy()
    if failure_stage == 'before_replace':
        replace = cm.os.replace
        def fail(source, destination):
            if Path(destination) == c.paths.settings: raise OSError('inert save failure')
            return replace(source, destination)
        monkeypatch.setattr(cm.os, 'replace', fail)
    else:
        monkeypatch.setattr(c, '_apply_saved_system_settings', lambda *a: (_ for _ in ()).throw(RuntimeError('post-save failure')))
    success, reason = worker._promote('unused')
    assert success == (failure_stage == 'post_commit')
    disk = json.loads(c.paths.settings.read_text())
    assert disk['llm_agent']['fine_tuned'] == (worker.candidate if success else pipeline.FINE_TUNED)
    assert weights == original and weights[worker.candidate] == worker.imported_digest
    assert ('post-save refresh failed' if success else 'settings could not be saved') in reason


def test_two_import_attempts_use_distinct_unselected_aliases(monkeypatch):
    c, worker, weights, _ = staged_worker(monkeypatch)
    original_live = weights[pipeline.FINE_TUNED]
    merged = worker.run_root / 'merged'
    merged.mkdir(parents=True);(merged / 'config.json').write_text('{}')
    monkeypatch.setattr(pipeline, 'ollama_exe', lambda: 'inert')
    calls = []
    def create(command, **kwargs):
        import jarvis.config as cm
        assert not cm._SAVE_LOCK182._is_owned()
        calls.append(command)
        assert command[1] == 'create' and command[2] not in weights
        weights[command[2]] = 'sha256:' + 'a' * 64
        return 0
    monkeypatch.setattr(worker, '_run', create)
    assert worker.import_model();first = worker.candidate
    assert worker.import_model();second = worker.candidate
    assert first != second and first in weights and second in weights
    assert c.get('llm_agent.fine_tuned') == pipeline.FINE_TUNED
    assert weights[pipeline.FINE_TUNED] == original_live


def test_concurrent_new_selection_during_import_keeps_newer_weights(monkeypatch):
    c, worker, weights, _ = staged_worker(monkeypatch)
    merged = worker.run_root / 'merged';merged.mkdir(parents=True);(merged / 'config.json').write_text('{}')
    monkeypatch.setattr(pipeline, 'ollama_exe', lambda: 'inert')
    other = config()
    def create(command, **kw):
        assert command[1] == 'create' and command[2] not in weights
        weights[command[2]] = 'sha256:' + 'a' * 64
        weights[pipeline.FINE_TUNED] = 'sha256:' + 'c' * 64  # Newer completed run.
        def update(data):
            data['llm_agent'].update(size='1.5b', base_model='qwen2.5:1.5b')
        other.update(update)
        return 0
    monkeypatch.setattr(worker, '_run', create)
    assert worker.import_model()
    success, reason = worker._promote('unused')
    assert not success and 'settings changed' in reason.lower()
    assert weights[pipeline.FINE_TUNED] == 'sha256:' + 'c' * 64
    assert c.get('llm_agent.base_model') == 'qwen2.5:1.5b'


def test_run_owned_alias_requires_exact_digest_provenance(monkeypatch):
    c, worker, weights, _ = staged_worker(monkeypatch)
    assert worker._promote('unused')[0]
    client = OllamaClient(c)
    client._session = SimpleNamespace(get=lambda *a, **kw: Response({'models': [
        {'name': worker.candidate + ':latest', 'digest': weights[worker.candidate]}, {'name': SMALL}]}))
    assert client.resolve_agent_model() == worker.candidate + ':latest'
    weights[worker.candidate] = 'sha256:' + 'd' * 64
    assert client.resolve_agent_model() == SMALL


def test_rollback_verification_cannot_overwrite_new_custom_selection(monkeypatch):
    from jarvis.neo_llm import jobs
    c, worker, weights, _ = staged_worker(monkeypatch)
    assert worker._promote('unused')[0]
    other = config()
    monkeypatch.setattr(jobs, '_model_exists', lambda *a: True)
    def digest(url, name, **kw):
        other.update(lambda data: data['llm_agent'].update(fine_tuned='deliberate-custom:7b'))
        return weights.get(name, '')
    monkeypatch.setattr(pipeline, 'model_digest', digest)
    assert jobs.rollback(c) == 'deliberate-custom:7b'
    assert c.get('llm_agent.fine_tuned') == 'deliberate-custom:7b'


@pytest.mark.parametrize('case', ['save_failure', 'theme'])
def test_setup_completion_uses_transaction_and_preserves_unrelated_edits(monkeypatch, case):
    import jarvis.config as cm
    c = config();callbacks = [];resets = []
    class Thread(PendingThread): pass
    Thread.callbacks = callbacks
    monkeypatch.setattr('threading.Thread', Thread)
    monkeypatch.setattr(agent_llm170, 'reset_availability', lambda: resets.append(True))
    def ensure(*a, **kw):
        assert not cm._SAVE_LOCK182._is_owned(), 'Setup backend under GUI settings lock'
        c.update(lambda data: data['ui'].update(theme='new-theme'))
        return 0
    monkeypatch.setattr(setup, 'ensure', ensure)
    assert setup.ensure_background(c, size='1.5b', force=True)
    if case == 'save_failure':
        original = c._save_locked182
        def fail(data):
            if data['llm_agent']['base_model'] == 'qwen2.5:1.5b': raise OSError('inert save failure')
            return original(data)
        monkeypatch.setattr(c, '_save_locked182', fail)
    callbacks[0]()
    assert c.get('ui.theme') == 'new-theme'
    assert c.get('llm_agent.base_model') == (SMALL if case == 'save_failure' else 'qwen2.5:1.5b')
    assert len(resets) == (0 if case == 'save_failure' else 1)
    assert json.loads(c.paths.settings.read_text())['llm_agent']['base_model'] == c.get('llm_agent.base_model')
