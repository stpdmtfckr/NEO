"""NEO 170: LoRA fine-tuning pipeline — data export, scoring, promotion rule, scheduling, tool.

The torch part (train_lora.py) runs in its own environment on the user's computer; its
end-to-end run (tiny Qwen2, real torch/peft, fake Ollama) is described in the build
guide. Here everything that decides *what* happens is tested without torch.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope='module')
def built():
    from jarvis.neo_llm import export
    return export.build(max_examples=700, eval_size=120, chat=40, languages=('ru', 'en'),
                        corrections=[{'language': 'ru', 'text': 'вруби ютубчик', 'right': {'tool': 'browser', 'arguments': {'action': 'open_url', 'value': 'youtube.com'}}}])


def test_export_rows_are_in_the_runtime_prompt_format(built):
    from jarvis.neo_llm import prompt
    row = next(r for r in built['train'] if r['meta']['kind'] == 'single')
    system, user, answer = row['messages'][0], row['messages'][-2], row['messages'][-1]
    assert system['role'] == 'system' and 'COMMANDS:' in system['content'] and user['role'] == 'user'
    assert json.loads(answer['content']) == {'do': row['meta']['do']}
    # exactly what the runtime would send for this text and these patterns
    patterns = [line[2:] for line in system['content'].split('COMMANDS:\n', 1)[1].splitlines() if line.startswith('- ')]
    assert prompt.messages(user['content'], row['meta']['language'], patterns)[0] == system


def test_every_target_command_is_read_by_neo_as_the_expected_intent(built):
    from jarvis.neo_llm.export import understood_as
    for row in built['train'] + built['eval']:
        meta = row['meta']
        for command, intent in zip(meta['do'], meta['gold']):
            assert understood_as(command, meta['language'], intent), (command, intent)


def test_export_has_every_kind_and_keeps_eval_templates_separate(built):
    kinds = {r['meta']['kind'] for r in built['train']}
    assert {'single', 'sequence', 'web', 'chat', 'correction'} <= kinds
    assert {'single', 'sequence', 'web', 'chat'} <= {r['meta']['kind'] for r in built['eval']}
    chat = [r for r in built['train'] if r['meta']['kind'] == 'chat']
    assert chat and all(r['teacher'] and r['messages'][-1]['role'] == 'user' for r in chat)
    correction = next(r for r in built['train'] if r['meta']['kind'] == 'correction')
    assert correction['messages'][-2]['content'] == 'вруби ютубчик'
    assert json.loads(correction['messages'][-1]['content'])['do'] == ['открой сайт youtube.com']


def test_sequences_and_web_flows(built):
    sequence = next(r for r in built['train'] if r['meta']['kind'] == 'sequence')
    assert len(sequence['meta']['do']) >= 2 and len(sequence['meta']['gold']) == len(sequence['meta']['do'])
    web = next(r for r in built['train'] if r['meta']['kind'] == 'web' and r['meta']['language'] == 'ru')
    assert web['meta']['do'][0].startswith('открой первый результат поиска ')


def test_score_counts_exact_command_sequences_and_chat():
    from jarvis.neo_llm.pipeline import score
    rows = [{'meta': {'kind': 'single', 'language': 'ru', 'gold': ['browser.search_first']}},
            {'meta': {'kind': 'sequence', 'language': 'ru', 'gold': ['application.open', 'ui.press']}},
            {'meta': {'kind': 'chat', 'language': 'ru', 'gold': []}},
            {'meta': {'kind': 'single', 'language': 'ru', 'gold': ['capture.replay_on']}}]
    answers = [{'do': ['открой первый результат поиска котики']},
               {'do': ['открой приложение блокнот', 'нажми клавишу enter']},
               {'reply': 'Привет!'},
               {'do': ['взломай всё']}]
    result = score(rows, answers)
    assert result['do_exact'] == round(2 / 3, 4) and result['chat_ok'] == 1.0 and result['json_ok'] == 1.0
    assert score(rows[:1], [None])['json_ok'] == 0.0 and score(rows[:1], [None])['chat_ok'] is None


def test_promotion_needs_a_clear_gain_and_no_chat_loss():
    from jarvis.neo_llm.pipeline import should_promote
    current = {'do_exact': 0.70, 'chat_ok': 0.90, 'json_ok': 1.0}
    assert should_promote(current, {'do_exact': 0.80, 'chat_ok': 0.88, 'json_ok': 0.99})[0]
    assert not should_promote(current, {'do_exact': 0.71, 'chat_ok': 0.95, 'json_ok': 1.0})[0]      # < +2 points
    assert not should_promote(current, {'do_exact': 0.90, 'chat_ok': 0.70, 'json_ok': 1.0})[0]      # lost conversation
    assert not should_promote(current, {'do_exact': 0.90, 'chat_ok': 0.90, 'json_ok': 0.80})[0]     # broken JSON


def test_sample_takes_every_kind():
    from jarvis.neo_llm.pipeline import sample
    rows = [{'meta': {'kind': 'single'}}] * 50 + [{'meta': {'kind': 'chat'}}] * 5 + [{'meta': {'kind': 'web'}}] * 5
    picked = sample(list(rows), 12)
    assert len(picked) == 12 and {r['meta']['kind'] for r in picked} == {'single', 'chat', 'web'}


def test_torch_is_installed_as_cuda_build_only_with_an_nvidia_gpu():
    from jarvis.neo_llm.pipeline import torch_install_commands
    gpu = torch_install_commands('py', True)
    assert [c[-1] for c in gpu[:2]] == ['https://download.pytorch.org/whl/cu124', 'https://download.pytorch.org/whl/cu121']
    assert gpu[-1] == ['py', '-m', 'pip', 'install', '--disable-pip-version-check', 'torch']
    assert torch_install_commands('py', False) == [gpu[-1]]


def test_modelfile_imports_the_merged_weights_with_neo_parameters(tmp_path):
    from jarvis.neo_llm.pipeline import modelfile
    text = modelfile(tmp_path / 'merged')
    assert text.startswith('FROM ./merged\nSYSTEM """') and 'PARAMETER num_ctx 3072' in text


def _config(tmp_path, **values):
    settings = {'llm_agent.enabled': True, 'llm_agent.finetune.auto': True, 'llm_agent.finetune.idle_minutes': 20}
    settings.update(values)
    return SimpleNamespace(paths=SimpleNamespace(root=str(tmp_path)), get=lambda k, d=None: settings.get(k, d), data={'llm_agent': {}})


def test_scheduler_waits_for_idle_no_game_and_new_data(tmp_path):
    from jarvis.neo_llm import jobs
    config = _config(tmp_path)
    assert not jobs.due(config, idle=60, game=False)                 # user is active
    assert not jobs.due(config, idle=3600, game=True)                # a game is running
    assert jobs.due(config, idle=3600, game=False)
    assert not jobs.due(_config(tmp_path, **{'llm_agent.finetune.auto': False}), idle=3600, game=False)
    root = tmp_path / 'llm_train'
    root.mkdir()
    import time
    (root / 'status.json').write_text(json.dumps({'stage': 'failed', 'time': time.strftime('%Y-%m-%dT%H:%M:%S')}))
    assert not jobs.due(config, idle=3600, game=False)               # failed today: retry tomorrow
    (root / 'status.json').write_text(json.dumps({'stage': 'done', 'time': '2026-01-01T00:00:00'}))
    key = 'Qwen/Qwen2.5-0.5B-Instruct|corrections=none|v188'   # NEO 188: fingerprint of the allowed corrections
    (root / 'state.json').write_text(json.dumps({'run_key': key, 'finished_key': key}))
    assert not jobs.due(config, idle=3600, game=False)               # already trained on this data


def test_pause_and_stop_flags(tmp_path):
    from jarvis.neo_llm import jobs
    config = _config(tmp_path)
    jobs.set_paused(config, True)
    root = tmp_path / 'llm_train'
    assert (root / 'pause.flag').exists() and jobs.read_status(config)['paused']
    jobs.set_paused(config, False)
    assert not (root / 'pause.flag').exists()
    jobs.stop(config)
    assert (root / 'stop.flag').exists()


def test_rollback_goes_to_the_plain_neo_model_without_a_previous_version(tmp_path, monkeypatch):
    from jarvis.neo_llm import jobs, pipeline
    from tests.test_neo184_lightweight_training import Config
    config = Config(tmp_path, **{'llm_agent.fine_tuned': 'neo-agent-ft'})
    monkeypatch.setattr(jobs, '_model_exists', lambda config, name: False)
    assert jobs.rollback(config) == 'neo-agent' and config.get('llm_agent.fine_tuned') == ''
    config.data['llm_agent'].update(fine_tuned='neo-agent-ft', fine_tuned_provenance={'neo-agent-ft-prev': {
        'hf_base': 'Qwen/Qwen2.5-0.5B-Instruct', 'ollama_base': 'qwen2.5:0.5b',
        'base_url': 'http://localhost:11434', 'run_key': 'verified', 'model_digest': 'sha256:' + 'a' * 64}})
    config.data['llm_agent'].pop('fine_tuned_previous', None)
    monkeypatch.setattr(jobs, '_model_exists', lambda config, name: True)
    monkeypatch.setattr(pipeline, 'model_digest', lambda *a, **kw: 'sha256:' + 'a' * 64)
    assert jobs.rollback(config) == 'neo-agent-ft-prev'


def test_assistant_model_tool_status_and_train(tmp_path, monkeypatch):
    from jarvis.neo_llm import jobs
    from jarvis.tools import assistant_model_tool170 as tool
    config = _config(tmp_path, **{'app.language': 'ru', 'llm_agent.size': '1.5b'})
    ollama = SimpleNamespace(resolve_agent_model=lambda: 'neo-agent:latest')
    result = tool.run(config, {'action': 'status'}, ollama=ollama)
    assert 'neo-agent:latest (1.5b)' in result.text and 'ещё не запускалось' in result.text
    started = []
    monkeypatch.setattr(jobs, 'start', lambda config, force=False: started.append(force) or True)
    monkeypatch.setattr(jobs, 'running', lambda: False)
    assert 'Начал дообучение' in tool.run(config, {'action': 'train'}).text and started == [True]
    missing = SimpleNamespace(resolve_agent_model=lambda: (_ for _ in ()).throw(RuntimeError('no')))
    assert 'не установлена' in tool.run(config, {'action': 'status'}, ollama=missing).text


@pytest.mark.parametrize('code,phrase,action', [
    ('ru', 'дообучи модель ассистента', 'train'), ('ru', 'как идёт дообучение модели ассистента', 'status'),
    ('ru', 'откати модель ассистента', 'rollback'), ('en', 'fine-tune the assistant model', 'train'),
    ('de', 'installiere das Assistenzmodell', 'install'), ('pt', 'para a afinação do modelo do assistente', 'stop'),
])
def test_voice_commands(code, phrase, action):
    from jarvis.runtime.canonical_commands import parse_canonical_command
    catalogue = json.loads((Path(__file__).parent / 'fixtures/command_catalogue.json').read_text(encoding='utf-8'))
    schemas = {row['tool']: row['schema'] for row in catalogue['tools']}
    parsed = parse_canonical_command(phrase, code, schemas.get)
    assert parsed is not None and (parsed.tool, parsed.arguments['action']) == ('assistant_model', action)


def test_train_script_is_self_contained():
    """train_lora.py runs in the separate training environment: it must not import NEO."""
    source = (Path(__file__).parents[1] / 'jarvis/neo_llm/train_lora.py').read_text(encoding='utf-8')
    assert 'import jarvis' not in source and 'from jarvis' not in source
    for needed in ('apply_chat_template', 'labels = [-100] * len(prompt_ids)', 'merge_and_unload', 'pause_file', 'stop_file', 'EXIT_STOPPED'):
        assert needed in source
