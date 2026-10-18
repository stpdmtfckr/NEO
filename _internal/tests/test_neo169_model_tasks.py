"""NEO 169: the NEO command model (data, network, runtime rules, versions) and verified multi-step tasks."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


class Config:
    def __init__(self, root: Path, data: dict | None = None) -> None:
        self.data = data or {'app': {'language': 'ru'}}
        self.paths = SimpleNamespace(root=root)

    def get(self, key, default=None):
        current = self.data
        for part in key.split('.'):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current


# ------------------------------------------------------------------ data
@pytest.fixture(scope='module')
def data():
    from jarvis.neo_model import dataset
    return dataset.build(ROOT, languages=('ru', 'en'))


def test_train_and_test_never_share_a_template_group(data):
    train_groups = {r['group'] for r in data['train']}
    assert not train_groups & {r['group'] for r in data['test'] if r['source'] not in {'context'}}


def test_dataset_covers_languages_negatives_context_and_typos(data):
    from jarvis.neo_model.dataset import NONE
    assert {r['language'] for r in data['train']} == {'ru', 'en'}
    assert any(r['intent'] == NONE for r in data['train']) and any(r['intent'] == NONE for r in data['test'])
    assert any(r['source'] == 'context' and r['context'] == 'browser' for r in data['train'])
    assert data['typo_test'] and all(r['split'] == 'typo_test' for r in data['typo_test'])
    assert all(r['text'] for r in data['train'])


def test_test_slot_values_are_not_training_values(data):
    train_values = {s['value'] for r in data['train'] for s in r['slots'] if s['name'] == 'name'}
    test_values = {s['value'] for r in data['test'] for s in r['slots'] if s['name'] == 'name'}
    assert test_values and not (test_values & train_values)


def test_corrections_join_training_only(tmp_path):
    from jarvis.neo_model import dataset
    fix = {'language': 'ru', 'text': 'сделай пошире', 'right': {'tool': 'window', 'arguments': {'action': 'maximize'}}}
    built = dataset.build(ROOT, languages=('ru',), corrections=[fix])
    assert any(r['source'] == 'correction' for r in built['train'])
    assert not any(r['source'] == 'correction' for r in built['test'])


# --------------------------------------------------------------- network
def test_features_are_stable_between_runs():
    from jarvis.neo_model.features import sentence_features
    a = sentence_features('Открой браузер', 'ru', '', 1 << 16)
    b = sentence_features('открой   браузер', 'ru', '', 1 << 16)
    assert np.array_equal(a[0], b[0]) and a[1] == b[1]


def test_small_network_learns_and_round_trips(tmp_path):
    from jarvis.neo_model.features import sentence_features
    from jarvis.neo_model.network import IntentNet, make_batch
    rows = [('открой браузер', 0), ('запусти браузер', 0), ('выключи звук', 1), ('убери звук', 1)] * 20
    batch = make_batch([sentence_features(t, 'ru', '', 4096) for t, _ in rows], [y for _, y in rows])
    net = IntentNet(4096, 16, 2)
    net.train([batch], epochs=30)
    probe = make_batch([sentence_features('открой браузер пожалуйста', 'ru', '', 4096)], [0])
    assert net.predict(probe)[0].argmax() == 0
    net.save(tmp_path / 'n.npz')
    loaded = IntentNet.load(tmp_path / 'n.npz')
    assert loaded.w1.dtype == np.int8                         # int8 + per-row scale: stays small in memory
    assert loaded.predict(probe)[0].argmax() == 0
    # Quantization keeps the answer: probabilities barely move.
    assert abs(float(loaded.predict(probe)[0, 0]) - float(net.predict(probe)[0, 0])) < 0.02
    # Rows that no training example touched are stored as zeros (an unknown word adds nothing).
    from jarvis.neo_model.network import quantize_rows
    untouched = np.flatnonzero(~net.touched)
    assert len(untouched) and not loaded.w1.q[untouched].any()


def test_float16_models_from_earlier_builds_still_load(tmp_path):
    from jarvis.neo_model.network import IntentNet, SlotNet
    np.savez_compressed(tmp_path / 'i.npz', w1=np.ones((8, 4), np.float16), b1=np.zeros(4, np.float32),
                        w2=np.ones((4, 2), np.float16), b2=np.zeros(2, np.float32))
    np.savez_compressed(tmp_path / 's.npz', w=np.ones((8, 3), np.float16), b=np.zeros(3, np.float32))
    assert IntentNet.load(tmp_path / 'i.npz').w1.dtype == np.float16
    assert SlotNet.load(tmp_path / 's.npz').w.shape == (8, 3)


# ---------------------------------------------------- versions / rollback
def test_promotion_gate_rejects_a_worse_version():
    from jarvis.neo_model.store import compare
    old = {'test_accuracy': 0.98, 'typo_accuracy': 0.85, 'confident_wrong': 0.003, 'none_false_command': 0.01}
    assert compare(dict(old), old) == []
    assert compare({**old, 'test_accuracy': 0.96}, old)
    assert compare({**old, 'confident_wrong': 0.02}, old)
    assert compare({**old, 'typo_accuracy': 0.86}, old) == []


def test_rollback_switches_to_the_previous_version(tmp_path, monkeypatch):
    from jarvis.neo_model import store
    config = Config(tmp_path)
    fake = [{'version': 'v1', 'origin': 'bundled', 'path': str(tmp_path / 'v1'), 'metrics': {}},
            {'version': 'local-001', 'origin': 'local', 'path': str(tmp_path / 'local-001'), 'metrics': {}}]
    monkeypatch.setattr(store, 'versions', lambda cfg: fake)
    store._save_state(config, {'active': 'local-001', 'previous': 'v1'})
    assert store.active_folder(config).name == 'local-001'
    assert store.rollback(config) == 'v1'
    assert store.active_folder(config).name == 'v1'
    assert store.rollback(config, 'missing') is None


# ------------------------------------------------------- runtime decisions
class _Predictor:
    def __init__(self, ranked, slots=None):
        self._ranked, self._slots = ranked, slots or {}
        self.labels = []

    def ranked(self, text, code, context='', top=3):
        return self._ranked

    def slots(self, text, intent):
        return dict(self._slots)


def _agent(tmp_path, tools):
    from jarvis.runtime.conversation import ConversationStore
    from jarvis.tools.registry import ToolDefinition
    registry = SimpleNamespace(_tools={}, get=None)
    for name, schema in tools.items():
        registry._tools[name] = ToolDefinition(name, name, schema, lambda a: None, lambda a: None)
    registry.get = lambda name: registry._tools[name]
    from jarvis.security import Risk
    registry.risk_for = lambda name, args: Risk.SAFE
    return SimpleNamespace(config=Config(tmp_path, {'app': {'language': 'ru'}}), tools=registry,
                           conversations=ConversationStore(), _conversation_key='desktop')


_UI = {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['scroll_down', 'type']}, 'text': {'type': 'string'}},
       'required': ['action'], 'additionalProperties': False}


def _propose(monkeypatch, agent, predictor, text):
    from jarvis.neo_model import runtime
    monkeypatch.setattr(runtime, 'model', lambda config: predictor)
    return runtime.propose(agent, text, 'ru')


def test_confident_command_is_proposed_with_validated_arguments(tmp_path, monkeypatch):
    agent = _agent(tmp_path, {'ui': _UI})
    proposal = _propose(monkeypatch, agent, _Predictor([('ui.scroll_down', 0.97), ('__none__', 0.01)]), 'полистай ниже')
    assert proposal.tool == 'ui' and proposal.arguments == {'action': 'scroll_down'} and not proposal.needs_clarification


def test_not_a_command_goes_to_conversation(tmp_path, monkeypatch):
    agent = _agent(tmp_path, {'ui': _UI})
    assert _propose(monkeypatch, agent, _Predictor([('__none__', 0.9), ('ui.scroll_down', 0.05)]), 'мне скучно') is None


def test_a_question_needs_very_high_confidence(tmp_path, monkeypatch):
    agent = _agent(tmp_path, {'ui': _UI})
    assert _propose(monkeypatch, agent, _Predictor([('ui.scroll_down', 0.95), ('__none__', 0.02)]), 'почему это происходит?') is None


def test_unknown_tool_and_invalid_arguments_are_rejected(tmp_path, monkeypatch):
    agent = _agent(tmp_path, {'ui': _UI})
    assert _propose(monkeypatch, agent, _Predictor([('rm_rf.everything', 0.99)]), 'сотри всё') is None
    assert _propose(monkeypatch, agent, _Predictor([('ui.explode', 0.99)]), 'взорви окно') is None


def test_uncertain_command_asks_first_and_yes_is_bound_to_it(tmp_path, monkeypatch):
    agent = _agent(tmp_path, {'ui': _UI})
    proposal = _propose(monkeypatch, agent, _Predictor([('ui.scroll_down', 0.7), ('__none__', 0.1)]), 'полистай')
    assert proposal.needs_clarification and '?' in proposal.question
    pending = agent.conversations.get('desktop').pending_intent
    assert pending.purpose == 'neo_model169' and pending.tool == 'ui' and pending.arguments == {'action': 'scroll_down'}


def test_negation_narration_quotes_and_gibberish_are_left_alone(tmp_path, monkeypatch):
    agent = _agent(tmp_path, {'ui': _UI})
    predictor = _Predictor([('ui.scroll_down', 0.99)])
    for text in ('не прокручивай вниз', 'я вчера прокрутил вниз', '«прокрути вниз»', 'прокртувниз ыфвапр'):
        assert _propose(monkeypatch, agent, predictor, text) is None, text


def test_a_risky_guess_is_only_suggested(tmp_path, monkeypatch):
    from jarvis.security import Risk
    agent = _agent(tmp_path, {'ui': _UI})
    agent.tools.risk_for = lambda name, args: Risk.MEDIUM
    proposal = _propose(monkeypatch, agent, _Predictor([('ui.scroll_down', 0.99)]), 'прокрути вниз')
    assert proposal.needs_clarification


def test_sequences_are_left_to_the_task_planner(tmp_path, monkeypatch):
    agent = _agent(tmp_path, {'ui': _UI})
    assert _propose(monkeypatch, agent, _Predictor([('ui.scroll_down', 0.99)]), 'прокрути вниз, затем прокрути вниз') is None


def test_bundled_model_understands_rephrasings_and_rejects_small_talk():
    from jarvis.neo_model.predictor import NeoCommandModel
    from jarvis.neo_model.store import BUNDLED
    folders = sorted(p for p in BUNDLED.iterdir() if (p / 'meta.json').exists())
    assert folders, 'the NEO command model must ship with NEO'
    model = NeoCommandModel(folders[-1])
    assert model.ranked('будь добр прокрути страничку вниз', 'ru')[0][0] in {'ui.scroll_down'}
    assert model.ranked('как дела', 'ru')[0][0] == '__none__'
    assert model.ranked('back', 'en', 'browser')[0][0] == 'browser_nav.back'
    assert model.ranked('back', 'en', 'music')[0][0] == 'music.previous'


# -------------------------------------------------------------- tasks
def test_task_split_and_plan_only_when_every_step_is_known():
    from jarvis.runtime import tasks169
    assert tasks169.split('открой блокнот, затем напечатай привет, потом нажми Enter', 'ru') == ['открой блокнот', 'напечатай привет', 'нажми Enter']
    known = {'открой блокнот': ('application', {'action': 'open', 'name': 'блокнот'}), 'нажми Enter': ('ui', {'action': 'press', 'key': 'Enter'})}
    assert tasks169.plan('открой блокнот затем нажми Enter', 'ru', known.get)
    assert tasks169.plan('открой блокнот затем взломай почту', 'ru', known.get) is None
    assert tasks169.plan('открой блокнот', 'ru', known.get) is None


def _result(success=True, **data):
    return SimpleNamespace(success=success, text='done' if success else 'failed', data=data)


def test_step_without_a_confirmed_effect_stops_the_task():
    from jarvis.runtime import tasks169
    steps = [tasks169.Step('a', 'ui', {'action': 'press'}), tasks169.Step('b', 'ui', {'action': 'press'})]
    calls = []
    outcome = tasks169.run(steps, 'en', lambda s: calls.append(s.text) or _result(), needs_confirmation=lambda s: False,
                           cancelled=lambda: False)
    assert not outcome.ok and calls == ['a'] and 'could not confirm' in outcome.text


def test_verified_steps_complete_the_task():
    from jarvis.runtime import tasks169
    steps = [tasks169.Step('a', 'ui', {'action': 'press'}), tasks169.Step('b', 'memory', {'action': 'list'})]
    outcome = tasks169.run(steps, 'en', lambda s: _result(verified_input_sent=True), needs_confirmation=lambda s: False,
                           cancelled=lambda: False)
    assert outcome.ok and [s.status for s in steps] == ['done', 'done']


def test_only_repeatable_steps_are_retried_and_not_when_already_done(monkeypatch):
    from jarvis.runtime import tasks169
    once = tasks169.Step('send', 'telegram_messages', {'action': 'send'})
    tasks169.run([once], 'en', lambda s: _result(success=False), needs_confirmation=lambda s: False, cancelled=lambda: False)
    assert once.attempts == 1                                      # never repeat a message
    opener = tasks169.Step('open notepad', 'application', {'action': 'open', 'name': 'notepad'})
    monkeypatch.setattr(tasks169, '_window_titles', lambda: [])
    tasks169.run([opener], 'en', lambda s: _result(success=False), needs_confirmation=lambda s: False, cancelled=lambda: False)
    assert opener.attempts == 2
    again = tasks169.Step('open notepad', 'application', {'action': 'open', 'name': 'notepad'})
    state = {'titles': []}
    monkeypatch.setattr(tasks169, '_window_titles', lambda: state['titles'])

    def execute(step):
        state['titles'] = ['Untitled - Notepad']                   # it opened, but after the check timed out
        return _result(success=False)
    outcome = tasks169.run([again], 'en', execute, needs_confirmation=lambda s: False, cancelled=lambda: False)
    assert again.attempts == 1 and outcome.ok                      # already done -> not opened twice


def test_cancel_stops_before_the_next_step():
    from jarvis.runtime import tasks169
    flag = {'stop': False}
    steps = [tasks169.Step('a', 'memory', {'action': 'list'}), tasks169.Step('b', 'memory', {'action': 'list'})]

    def execute(step):
        flag['stop'] = True
        return _result()
    outcome = tasks169.run(steps, 'ru', execute, needs_confirmation=lambda s: False, cancelled=lambda: flag['stop'])
    assert not outcome.ok and steps[1].status == 'planned' and 'отменена' in outcome.text


# ---------------------------------------------------------------- screen
def test_button_names_and_matching():
    from jarvis.runtime import ui_elements168 as elements
    from jarvis.tools.ui_control168 import button_name
    assert button_name('на кнопку «Продолжить» в активном окне') == 'Продолжить'
    assert button_name('На главную') == 'На главную'
    items = [elements.Element(n, 'Button', (0, i * 30, 80, i * 30 + 20)) for i, n in enumerate(['Продолжить', 'Отмена', 'Настройки'])]
    assert elements.match_by_name(items, 'продолжить')[0].name == 'Продолжить'
    assert elements.match_by_name(items, 'Сохранить') == (None, [])


def test_key_names_given_as_buttons_press_the_key(monkeypatch):
    from jarvis.runtime import input168
    from jarvis.tools import ui_control168
    pressed = []
    monkeypatch.setattr(input168, 'press', lambda spec, **kw: pressed.append(spec.keys))
    monkeypatch.setattr(ui_control168, '_target', lambda code: None)
    config = SimpleNamespace(get=lambda k, d=None: 'ru' if k == 'app.language' else d)
    result = ui_control168.press_key(config, None, 'кнопку esc')
    assert result.success and pressed == [('esc',)]


def test_back_right_after_the_browser_is_page_navigation(tmp_path):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'run')
        try:
            session.reset_case('ru')
            session.agent.conversations.get('desktop').last_tool = 'browser_nav'
            session.agent.handle('назад')
            assert session.calls and session.calls[0]['tool'] == 'browser_nav' and session.calls[0]['arguments'] == {'action': 'back'}
        finally:
            session.finish()


def test_model_choice_answer_runs_the_chosen_candidate():
    from jarvis.runtime.dialogue_tasks import PendingIntent, Slot, _complete
    pending = PendingIntent('ui', {'_neo169': '2'}, (Slot('_neo169', 'choice', 'choose', choices=(('a', '1'), ('b', '2'))),), 'ru', 1e12,
                            purpose='neo_model169', guard157={'neo169': {'1': ['ui', {'action': 'scroll_up'}], '2': ['browser_nav', {'action': 'back'}]}})
    assert _complete(pending) == ('browser_nav', {'action': 'back'})
