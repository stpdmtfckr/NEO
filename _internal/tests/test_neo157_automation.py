"""Real-Agent typed plans and real registry dispatch with mocked child effects."""
from pathlib import Path
import json
from unittest.mock import Mock

import pytest

from jarvis.runtime.automation_requests157 import parse_calls, reviewed_call, execute_reviewed_child
from jarvis.security import Risk
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution

ROOT = Path(__file__).resolve().parents[1]
LANGUAGES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
LINKS = ('затем', 'then', 'dann', 'puis', 'luego', 'poi', 'depois')


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        session = AtlasSession(tmp_path_factory.mktemp('automation157') / 'run')
        yield session
        session.finish()


def utterances(code, action):
    pack = json.loads((ROOT / 'jarvis/locales/commands' / (code + '.json')).read_text(encoding='utf-8'))
    return (pack['action_phrases']['automation.' + action][0],
            pack['action_phrases']['system_monitor.memory'][0],
            pack['action_phrases']['system_monitor.cpu'][0])


@pytest.mark.parametrize('code', LANGUAGES)
@pytest.mark.parametrize('action', ['queue', 'parallel'])
def test_real_agent_questions_then_exact_plan_with_normal_confirmation(session, code, action):
    session.reset_case(code)
    session.config.data['security']['confirm_medium_risk'] = True
    head, memory, cpu = utterances(code, action)
    first = session.agent.handle(head)
    pending = session.agent.conversations.get('desktop').pending_intent
    assert pending is not None and pending.tool == 'automation' and pending.arguments == {'action': action}
    assert first.text and not first.pending and not session.calls
    result = session.agent.handle(memory + '; ' + cpu)
    assert result.pending is not None, result.text
    assert result.pending.tool_name == 'automation'
    assert result.pending.arguments == {'action': action, 'calls': [
        {'tool': 'system_monitor', 'arguments': {'action': 'memory'}},
        {'tool': 'system_monitor', 'arguments': {'action': 'cpu'}},
    ]}
    assert result.pending.risk == Risk.MEDIUM and not session.calls
    session.agent.confirm_pending(False)
    assert not session.calls


@pytest.mark.parametrize('code,separator', list(zip(LANGUAGES, LINKS)))
def test_spoken_then_preserves_all_clauses(code, separator):
    expected = [('system_monitor', {'action': 'memory'}), ('system_monitor', {'action': 'cpu'})]
    parser = Mock(side_effect=expected)
    result = parse_calls('first ' + separator + ' second', code, parser)
    assert result == [{'tool': tool, 'arguments': args} for tool, args in expected]
    assert [call.args[0] for call in parser.call_args_list] == ['first', 'second']


@pytest.mark.parametrize('text', ['one', 'one;', ';two', 'one;;two', 'one\x00;two', '"one;two"', ';'.join(['one'] * 9)])
def test_invalid_lists_never_partially_parse(text):
    parser = Mock()
    assert parse_calls(text, 'en', parser) is None
    parser.assert_not_called()


@pytest.mark.parametrize('child', [
    ('filesystem', {'action': 'delete', 'source': 'C:/owned.txt'}),
    ('email', {'action': 'send', 'to': 'qa@example.invalid', 'subject': 'QA', 'body': 'QA'}),
    ('automation', {'action': 'queue', 'calls': []}),
    ('application', {'action': 'kill', 'name': 'QA'}),
    ('application', {'action': 'open'}),
    ('browser', {'action': 'open_url', 'value': 'javascript:alert(1)'}),
])
def test_unsupported_or_incomplete_child_rejects_whole_input(child):
    parser = Mock(side_effect=[('system_monitor', {'action': 'memory'}), child])
    assert parse_calls('first; second', 'en', parser) is None


def test_queue_can_open_but_parallel_is_read_only():
    call = ('application', {'action': 'open', 'name': 'Notepad'})
    assert reviewed_call(*call, 'queue')
    assert not reviewed_call(*call, 'parallel')


@pytest.mark.parametrize('action', ['queue', 'parallel'])
def test_real_agent_open_task_requires_queue_and_confirmation(session, action):
    session.reset_case('ru')
    session.config.data['security']['confirm_medium_risk'] = True
    head, _, cpu = utterances('ru', action)
    session.agent.handle(head)
    result = session.agent.handle('открой приложение Notepad; ' + cpu)
    assert not session.calls
    if action == 'parallel':
        assert result.pending is None
        assert session.agent.conversations.get('desktop').pending_intent is not None
    else:
        assert result.pending is not None, result.text
        call = result.pending.arguments['calls'][0]
        assert call['tool'] == 'application' and call['arguments']['action'] == 'open'
        assert call['arguments']['name'].casefold() == 'notepad'
        session.agent.confirm_pending(False)
        assert not session.calls


@pytest.mark.parametrize('action', ['queue', 'parallel'])
def test_confirmed_plan_reaches_real_registry_children_including_parallel_ownership(session, action):
    session.reset_case('ru')
    session.config.data['security']['confirm_medium_risk'] = True
    head, memory, cpu = utterances('ru', action)
    session.agent.handle(head)
    turn = session.agent.handle(memory + '; ' + cpu)
    assert turn.pending is not None
    args = turn.pending.arguments
    definition = session.agent.tools.get('system_monitor')
    original = definition.handler
    definition.handler = Mock(side_effect=lambda a: ToolExecution(True, a['action'], {'fixture': True}))
    session._permitted.extend([('automation', args), *(('system_monitor', c['arguments']) for c in args['calls'])])
    try:
        result = session.agent.confirm_pending(True)
        assert result.success, result.text
        assert len(definition.handler.call_args_list) == 2
        assert [c['tool'] for c in session.calls].count('automation') == 1
        assert sorted(c['arguments']['action'] for c in session.calls if c['tool'] == 'system_monitor') == ['cpu', 'memory']
        assert not session._permitted
    finally:
        definition.handler = original


@pytest.mark.parametrize('fault', ['too_many', 'malformed', 'invalid_argument', 'nested', 'unsupported_parallel'])
def test_backend_preflights_entire_list_before_first_child(session, fault):
    session.reset_case('en')
    from jarvis.routing.ownership_firewall import ownership_claim_scope, OwnershipClaim
    first = {'tool': 'system_monitor', 'arguments': {'action': 'memory'}}
    second = {'tool': 'system_monitor', 'arguments': {'action': 'cpu'}}
    calls = [first, second]
    action = 'queue'
    if fault == 'too_many': calls = [first] * 13
    elif fault == 'malformed': calls.append('invalid')
    elif fault == 'invalid_argument': calls.append({'tool': 'system_monitor', 'arguments': {'action': '__unknown'}})
    elif fault == 'nested': calls.append({'tool': 'automation', 'arguments': {'action': 'queue', 'calls': [first]}})
    elif fault == 'unsupported_parallel':
        action = 'parallel'
        calls.append({'tool': 'application', 'arguments': {'action': 'open', 'name': 'Notepad'}})
    args = {'action': action, 'calls': calls}
    session._permitted.append(('automation', args))
    claim = OwnershipClaim(owner='native:automation', allowed_tools=('automation',), schema_tools=('automation',), enforced=True)
    with ownership_claim_scope(claim):
        result = session.agent.tools.execute('automation', args)
    assert not result.success
    assert not any(call['tool'] != 'automation' for call in session.calls)


def test_child_scope_helper_cannot_bypass_high_risk_operation(session):
    session.reset_case('en')
    with pytest.raises(ValueError, match='Unsupported automation child'):
        execute_reviewed_child(session.agent.tools, 'filesystem', {'action': 'delete', 'source': 'C:/QA.txt'})
    assert not session.calls


@pytest.mark.parametrize('fault', ['failure', 'cancellation'])
def test_queue_stops_before_second_child_after_failure_or_cancellation(session, fault):
    session.reset_case('en')
    session.config.data['security']['confirm_medium_risk'] = True
    head, memory, cpu = utterances('en', 'queue')
    session.agent.handle(head)
    turn = session.agent.handle(memory + '; ' + cpu)
    args = turn.pending.arguments
    definition = session.agent.tools.get('system_monitor')
    original = definition.handler

    def first_child(arguments):
        assert arguments['action'] == 'memory'
        if fault == 'cancellation':
            from jarvis.runtime.cancellation import cancellation_manager
            cancellation_manager().cancel('test cancellation')
        return ToolExecution(fault != 'failure', 'fixture outcome')

    definition.handler = Mock(side_effect=first_child)
    session._permitted.extend([('automation', args), ('system_monitor', args['calls'][0]['arguments'])])
    try:
        result = session.agent.confirm_pending(True)
        assert result.success is False
        definition.handler.assert_called_once()
        assert [c['arguments'].get('action') for c in session.calls] == ['queue', 'memory']
    finally:
        definition.handler = original


def test_new_automation_scenarios_are_part_of_complete_atlas(session):
    from dataclasses import asdict
    from tools.atlas_all_test import load_cases, check_phrase
    for code in LANGUAGES:
        session.reset_case(code)
        rows = [row for row in load_cases(ROOT, code) if row.source == 'automation157']
        assert len(rows) == 6
        assert sum(row.keep_context for row in rows) == 3
        for row in rows:
            result = check_phrase(session, asdict(row))
            expected = {'CONFIRMATION_REQUIRED', 'ARGUMENTS_PASS'} if row.keep_context else {'CLARIFICATION_PASS'}
            assert result['status'] in expected, result
