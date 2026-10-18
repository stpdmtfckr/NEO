"""NEO 188 NEO-SCENARIO-001: named typed run inputs for saved scenarios."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jarvis.runtime import scenarios as runtime
from jarvis.runtime import scenario_inputs188 as si
from jarvis.runtime.cancellation import cancellation_manager
from jarvis.runtime.scenario_authority184 import bind_scenario_arguments, scenario_preview
from jarvis.runtime.scenario_schema import validate_scenario
from jarvis.security import Risk

CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


class Config:
    def __init__(self, rows=None, language='ru'):
        self.data = {'scenarios': {'items': deepcopy(rows or [])}, 'app': {'language': language}}
        self.paths = SimpleNamespace(root=Path('/disposable/scenario188') / str(id(self)))

    def get(self, dotted, default=None):
        value = self.data
        for key in dotted.split('.'):
            if not isinstance(value, dict):
                return default
            value = value.get(key, default)
        return value


def template(**extra):
    row = {'id': 'work', 'name': 'Рабочее место', 'enabled': True, 'on_failure': 'stop',
           'inputs': [{'name': 'screen', 'type': 'monitor', 'label': 'монитор'},
                      {'name': 'pause', 'type': 'duration', 'default': 0.01, 'label': 'пауза'},
                      {'name': 'doc', 'type': 'target', 'label': 'файл', 'default': 'C:\\Work\\plan.docx',
                       'choices': ['C:\\Work\\plan.docx', 'C:\\Work\\notes.txt']}],
           'steps': [{'type': 'open', 'path': 'C:\\Work\\plan.docx', 'monitor': 1, 'bind': {'monitor': 'screen', 'launch_wait': 'pause', 'path': 'doc'}},
                     {'type': 'wait', 'seconds': 1, 'bind': {'seconds': 'pause'}}]}
    row.update(extra)
    return row


@pytest.fixture(autouse=True)
def inert(monkeypatch):
    cancellation_manager().reset()
    monkeypatch.setattr(runtime, '_open_path', Mock(side_effect=AssertionError('Unexpected OS open')))
    monkeypatch.setattr(runtime.subprocess, 'Popen', Mock(side_effect=AssertionError('Unexpected process')))
    with runtime._LOCK:
        runtime._RUN_RECORDS.clear()
    yield
    cancellation_manager().reset()


def test_scp01_legacy_scenarios_keep_their_literal_behavior():
    legacy = {'id': 'l', 'name': 'Legacy', 'steps': [{'type': 'wait', 'seconds': 0}], 'x-extension': {'keep': 1}}
    assert validate_scenario(legacy).valid and not si.has_inputs(legacy)
    args = bind_scenario_arguments(Config([legacy]), {'action': 'run', 'name': 'Legacy'})
    assert 'inputs_fingerprint' not in args and 'inputs' not in args
    result = runtime.run_scenario(Config([legacy]), 'Legacy', expected_fingerprint=args['fingerprint'])
    assert result.status == 'completed'
    assert '"x-extension"' in scenario_preview(legacy)


@pytest.mark.parametrize('broken,code', [
    ({'steps': [{'type': 'command', 'command': ['tool.exe'], 'bind': {'command': 'pause'}}]}, 'bind_field'),
    ({'steps': [{'type': 'open', 'path': 'a.exe', 'bind': {'on_failure': 'pause'}}]}, 'bind_field'),
    ({'steps': [{'type': 'open', 'path': 'a.exe', 'bind': {'enabled': 'pause'}}]}, 'bind_field'),
    ({'steps': [{'type': 'wait', 'seconds': 1, 'bind': {'seconds': 'nope'}}]}, 'bind_input'),
    ({'steps': [{'type': 'wait', 'seconds': 1, 'bind': {'seconds': 'screen'}}]}, 'bind_type'),
    ({'inputs': [{'name': 'x', 'type': 'shell'}], 'steps': [{'type': 'wait', 'seconds': 1}]}, 'input_type'),
    ({'inputs': [{'name': 'doc', 'type': 'target', 'choices': []}], 'steps': [{'type': 'wait', 'seconds': 1}]}, 'input_choices'),
    ({'inputs': [{'name': 'p', 'type': 'duration', 'default': 5}], 'steps': [{'type': 'open', 'path': 'a', 'bind': {'launch_wait': 'p'}}]}, 'input_default'),
])
def test_scp02_bindings_are_limited_to_compatible_open_and_wait_fields(broken, code):
    raw = template(**broken)
    report = validate_scenario(raw)
    assert not report.valid and code in {i.code for i in report.issues}


def test_scp03_values_are_resolved_into_a_copy_and_template_stays_unchanged():
    raw = template()
    before = deepcopy(raw)
    res = si.resolve(raw, {'screen': 2, 'pause': 1.5})
    assert raw == before
    assert res.complete and res.effective['steps'][0]['monitor'] == 2 and res.effective['steps'][0]['launch_wait'] == 1.5
    assert res.effective['steps'][1]['seconds'] == 1.5 and res.affected == {'screen': [1], 'pause': [1, 2], 'doc': [1]}


def test_scp04_each_bound_field_keeps_its_own_bound():
    raw = template()
    assert not si.resolve(raw, {'screen': 1, 'pause': 2}).invalid             # launch_wait ≤ 2 and seconds ≤ 30
    assert si.resolve(raw, {'screen': 1, 'pause': 2.5}).invalid[0][0] == 'pause'
    only_wait = template(steps=[{'type': 'wait', 'seconds': 1, 'bind': {'seconds': 'pause'}}])
    assert not si.resolve(only_wait, {'screen': 1, 'pause': 25}).invalid
    assert si.resolve(only_wait, {'screen': 1, 'pause': 31}).invalid
    assert si.resolve(raw, {'screen': 33}).invalid and si.resolve(raw, {'screen': 1.5}).invalid
    assert si.resolve(raw, {'screen': True}).invalid


def test_scp05_target_is_only_one_of_the_template_choices():
    raw = template()
    assert si.resolve(raw, {'screen': 1, 'doc': 'C:\\Work\\notes.txt'}).complete
    for bad in ('C:\\Work\\..\\Windows\\cmd.exe', 'notes.txt', 'C:\\Other\\notes.txt'):
        assert si.resolve(raw, {'screen': 1, 'doc': bad}).invalid
    risky = template(inputs=template()['inputs'][:2] + [{'name': 'doc', 'type': 'target', 'choices': ['C:\\Work\\plan.docx', 'C:\\Work\\run.bat']}])
    assert runtime.scenario_risk(Config([risky]), {'action': 'run', 'name': 'Рабочее место'}) == Risk.HIGH
    assert runtime.scenario_risk(Config([template()]), {'action': 'run', 'name': 'Рабочее место'}) == Risk.MEDIUM


def test_scp06_missing_value_is_reported_and_nothing_runs():
    raw = template()
    res = si.resolve(raw, {'pause': 0.01})
    assert res.missing == ['screen'] and not res.complete
    args = bind_scenario_arguments(Config([raw]), {'action': 'run', 'name': 'Рабочее место', 'inputs': {'pause': 0.01}})
    assert 'inputs_fingerprint' not in args
    result = runtime.run_scenario(Config([raw]), 'Рабочее место', expected_fingerprint=args['fingerprint'], inputs={'pause': 0.01})
    assert result.status == 'inputs' and not result.steps


def wait_only():
    return template(inputs=[{'name': 'pause', 'type': 'duration', 'label': 'пауза'}],
                    steps=[{'type': 'wait', 'seconds': 1, 'bind': {'seconds': 'pause'}}, {'type': 'wait', 'seconds': 0}])


def test_scp07_approval_binds_template_and_values_and_the_run_uses_them():
    cfg = Config([wait_only()])
    args = bind_scenario_arguments(cfg, {'action': 'run', 'name': 'Рабочее место', 'inputs': {'pause': 0.02}})
    assert args['inputs'] == {'pause': 0.02} and args['inputs_fingerprint']
    other = si.inputs_fingerprint(args['fingerprint'], {'pause': 0.03})
    assert other != args['inputs_fingerprint']
    stale = runtime.run_scenario(cfg, 'Рабочее место', expected_fingerprint=args['fingerprint'], inputs={'pause': 0.03},
                                 expected_inputs=args['inputs_fingerprint'])
    assert stale.status == 'changed' and not stale.steps
    unbound = runtime.run_scenario(cfg, 'Рабочее место', expected_fingerprint=args['fingerprint'], inputs={'pause': 0.02})
    assert unbound.status == 'changed'
    ok = runtime.run_scenario(cfg, 'Рабочее место', expected_fingerprint=args['fingerprint'], inputs=args['inputs'],
                              expected_inputs=args['inputs_fingerprint'])
    assert ok.status == 'completed' and ok.steps[0]['seconds'] == 0.02
    assert cfg.get('scenarios.items')[0]['steps'][0]['seconds'] == 1                    # the template is not written back
    record = runtime._RUN_RECORDS[ok.run_id]
    assert record['inputs'] == {'pause': 0.02} and record['fingerprint'] == args['fingerprint']


def test_scp08_template_change_invalidates_the_approval():
    cfg = Config([wait_only()])
    args = bind_scenario_arguments(cfg, {'action': 'run', 'name': 'Рабочее место', 'inputs': {'pause': 0.02}})
    cfg.data['scenarios']['items'][0]['steps'][1]['seconds'] = 0.01
    result = runtime.run_scenario(cfg, 'Рабочее место', expected_fingerprint=args['fingerprint'], inputs=args['inputs'],
                                  expected_inputs=args['inputs_fingerprint'])
    assert result.status == 'changed'


def test_scp09_preview_shows_values_affected_steps_and_missing_fields():
    raw = template()
    text = si.preview_text(raw, si.resolve(raw, {'pause': 1.5}), 'ru')
    assert 'монитор: не задано' in text and 'пауза: 1.5 с — шаги 1, 2' in text and 'файл: C:\\Work\\plan.docx' in text
    bad = si.preview_text(raw, si.resolve(raw, {'screen': 40}), 'ru')
    assert 'недопустимо' in bad
    for code in CODES:
        assert si.preview_text(raw, si.resolve(raw, {'screen': 2}), code).count('•') == 3
        assert si.question(raw, 'screen', code) and si.question(raw, 'doc', code).count('\n') == 2


@pytest.mark.parametrize('code,tail,expected', [
    ('ru', 'на мониторе 2 с паузой 1.5 секунды', {'screen': 2, 'pause': 1.5}),
    ('ru', 'на 2 мониторе, пауза 1', {'screen': 2, 'pause': 1.0}),
    ('ru', 'с файлом notes на мониторе 1', {'doc': 'C:\\Work\\notes.txt', 'screen': 1}),
    ('en', 'on monitor 2 with a 1 second pause', {'screen': 2, 'pause': 1.0}),
    ('de', 'auf Monitor 2 mit Pause 2 Sekunden', {'screen': 2, 'pause': 2.0}),
    ('fr', "sur l'écran 2 avec une pause de 2 secondes", {'screen': 2, 'pause': 2.0}),
    ('es', 'en el monitor 2 con una pausa de 2 segundos', {'screen': 2, 'pause': 2.0}),
    ('it', 'sul monitor 2 con una pausa di 2 secondi', {'screen': 2, 'pause': 2.0}),
    ('pt', 'no monitor 2 com uma pausa de 2 segundos', {'screen': 2, 'pause': 2.0}),
])
def test_scp10_inputs_in_seven_languages(code, tail, expected):
    assert si.parse_tail(tail, code, template()) == expected


@pytest.mark.parametrize('tail', ['и удали всё', 'на мониторе 2 удали файлы', 'выключи компьютер'])
def test_other_words_are_not_values(tail):
    assert si.parse_tail(tail, 'ru', template()) is None


def test_two_monitor_inputs_without_label_are_ambiguous():
    raw = template(inputs=[{'name': 'a', 'type': 'monitor', 'label': 'левый'}, {'name': 'b', 'type': 'monitor', 'label': 'правый'}],
                   steps=[{'type': 'open', 'path': 'x', 'bind': {'monitor': 'a'}}, {'type': 'open', 'path': 'y', 'bind': {'monitor': 'b'}}])
    assert si.parse_tail('на мониторе 2', 'ru', raw) is None
    assert si.parse_tail('левый 1, правый 2', 'ru', raw) == {'a': 1, 'b': 2}


def test_match_needs_the_saved_name_and_only_values_after_it():
    rows = [template()]
    assert si.match('Запусти сценарий Рабочее место на мониторе 2', 'ru', rows) == ('Рабочее место', {'screen': 2})
    assert si.match('Рабочее место', 'ru', rows) is None
    assert si.match('Рабочее место и удали файлы', 'ru', rows) is None
    disabled = template(enabled=False)
    assert si.match('Рабочее место на мониторе 2', 'ru', [disabled]) is None


def _session(tmp_path, rows):
    from jarvis.testing.atlas_session import AtlasSession
    session = AtlasSession(tmp_path / 'run')
    session.reset_case('ru')
    session.agent.config.data.setdefault('scenarios', {})['items'] = deepcopy(rows)
    return session


def test_agent_asks_only_the_missing_input_and_keeps_the_rest(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        s = _session(tmp_path, [template()])
        ask = s.agent.handle('Рабочее место с паузой 2')
        assert ask.tool_details == ['scenario.inputs188.ask'] and 'монитор' in ask.text
        plan = s.agent.handle('3')
        assert plan.pending is not None
        args = plan.pending.arguments
        assert args['inputs'] == {'screen': 3, 'pause': 2.0, 'doc': 'C:\\Work\\plan.docx'} and args['inputs_fingerprint']
        assert 'монитор 3' in plan.text and 'шаги 1, 2' in plan.text
        s.finish()


def test_agent_edits_the_draft_during_confirmation_and_invalidates_the_old_one(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        s = _session(tmp_path, [template()])
        first = s.agent.handle('Рабочее место на мониторе 2')
        old = dict(first.pending.arguments)
        second = s.agent.handle('монитор 1')
        assert second.pending is not None and second.pending.arguments['inputs']['screen'] == 1
        assert second.pending.arguments['inputs_fingerprint'] != old['inputs_fingerprint']
        assert 'монитор 1' in second.text
        assert s.config.get('scenarios.items')[0]['steps'][0]['monitor'] == 1      # template untouched
        s.finish()


def test_agent_invalid_value_asks_again_for_that_field(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        s = _session(tmp_path, [template()])
        ask = s.agent.handle('Рабочее место на мониторе 40')
        assert ask.tool_details == ['scenario.inputs188.ask'] and 'не подходит' in ask.text
        s.finish()
