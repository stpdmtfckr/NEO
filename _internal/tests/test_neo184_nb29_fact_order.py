"""NB29: original and learned explicit facts keep literal ownership.

Real public Agent routing, real learned lookup and SQLite notes; all native
handlers are fail-on-call except the exact inert owner fixtures below. Chat and
planner replies are fixed local values, so these tests need no model or network.
"""
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jarvis.runtime import learned177, memory169, typos175
from jarvis.runtime.dialogue_tasks import PendingIntent, Slot
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution


@pytest.fixture
def rig(tmp_path, monkeypatch):
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'fact_order', locale='en')
        session.config.data['local_chat']['live_small_talk'] = False
        session.config.data.setdefault('learning', {})['remember_resolved'] = True
        agent = session.agent
        forbidden, synthetic = [], []

        def blocked(name):
            def fail(*args, **kwargs):
                forbidden.append(name)
                raise AssertionError('No external effects in fact-order regression: ' + name)
            return fail

        for name, definition in agent.tools._tools.items():
            if name != 'memory':
                monkeypatch.setattr(definition, 'handler', blocked(name))
        for name in ('generate_text', 'chat', 'chat_stream'):
            if hasattr(agent.ollama, name):
                monkeypatch.setattr(agent.ollama, name, blocked('model.' + name))

        def chat(*args, **kwargs):
            synthetic.append('chat')
            return 'Synthetic conversation fixture'

        def planner(*args, **kwargs):
            synthetic.append('planner')
            return {'reply': 'Synthetic conversation fixture', '_model': 'inert-fixture'}

        monkeypatch.setattr(agent.ollama, 'chat_text', chat)
        monkeypatch.setattr(agent.ollama, 'agent_json', planner)
        monkeypatch.setattr(agent.ollama._session, 'get', blocked('transport.get'))
        monkeypatch.setattr(agent.ollama._session, 'post', blocked('transport.post'))
        agent.tools.set_execution_guard(
            lambda name, args, risk: session.calls.append((name, dict(args))) or None)
        note = Mock(wraps=memory169.note)
        write = Mock(wraps=agent.database.set_memory)
        repair = Mock(wraps=typos175.repair)
        monkeypatch.setattr(memory169, 'note', note)
        monkeypatch.setattr(agent.database, 'set_memory', write)
        monkeypatch.setattr(typos175, 'repair', repair)
        try:
            yield SimpleNamespace(session=session, agent=agent, note=note, write=write,
                                  repair=repair, synthetic=synthetic)
            assert forbidden == []
        finally:
            session.finish()


def remember_mapping(rig, original, meaning, code='en'):
    rig.session.config.data['app']['language'] = code
    assert learned177.remember(rig.session.config, original, meaning, code, 'nb29-regression')
    assert learned177.lookup(rig.session.config, original, code) == meaning


def fact_writes(rig):
    return [(call.args[0], call.args[1]) for call in rig.write.call_args_list
            if call.args[0].startswith(('note:', 'pref:'))]


def assert_literal_note(rig, turn, body, code='en'):
    assert turn.pending is None and turn.tool_details == ['memory.remember_that']
    rig.note.assert_called_once_with(rig.agent.database, rig.session.config, body, code)
    rows = fact_writes(rig)
    assert len(rows) == 1 and rows[0][0].startswith('note:')
    # The unchanged note store owns whitespace normalization, not the router.
    assert rows[0][1] == ' '.join(body.split())
    assert rig.agent.database.get_memory(rows[0][0]) == rows[0][1]
    assert rig.session.calls == [] and rig.synthetic == []
    rig.repair.assert_not_called()


def assert_no_fact(rig, turn):
    assert turn.pending is None
    rig.note.assert_not_called()
    assert fact_writes(rig) == [] and rig.session.calls == []


def test_original_fact_precedes_real_competing_learned_command(rig):
    original, meaning = 'запомни что пример', 'открой калькулятор'
    remember_mapping(rig, original, meaning, 'ru')
    assert_literal_note(rig, rig.agent.handle(original), 'пример', 'ru')
    assert learned177.lookup(rig.session.config, original, 'ru') == meaning


@pytest.mark.parametrize('address', ['', 'NEO, '])
def test_addressed_and_plain_fact_do_not_repair_literal_typo(rig, address):
    body = 'spotiy is a synthetic fixture'
    assert_literal_note(rig, rig.agent.handle(address + 'remember that ' + body), body)


def test_accepted_learned_fact_preserves_its_exact_mapped_body(rig):
    original, body = 'synthetic archive fixture', 'Keep Literal SPoTiY'
    meaning = 'remember that ' + body
    remember_mapping(rig, original, meaning)
    assert_literal_note(rig, rig.agent.handle(original), body)
    assert learned177.lookup(rig.session.config, original, 'en') == meaning


@pytest.mark.parametrize('observed', [
    'do not remember that fixture',
    '"remember that fixture"',
])
def test_distinct_observed_negative_or_quote_beats_canonical_fact(rig, observed):
    turn = rig.agent.handle_stream('remember that fixture', raw_user_text=observed)
    assert_no_fact(rig, turn)


def test_observed_fact_body_wins_over_transport_normalization(rig):
    body = ' \tSpOtIy\r\n"Literal" '
    observed = 'NEO, remember that ' + body
    turn = rig.agent.handle_stream('remember that spotify Literal', raw_user_text=observed)
    assert_literal_note(rig, turn, body)


@pytest.mark.parametrize('code,seeded,observed', [
    ('ru', 'не запоминай что пример', 'не запоминай что пример'),
    ('en', 'synthetic archive fixture', '"synthetic archive fixture"'),
])
def test_learned_fact_cannot_override_original_negative_or_quote(rig, code, seeded, observed):
    meaning = ('запомни что ' if code == 'ru' else 'remember that ') + 'Never store fixture'
    remember_mapping(rig, seeded, meaning, code)
    # The real lookup normalizes quotation marks even though quoted seeds are
    # not admitted by remember(), so this quote is a valid competing mapping.
    assert learned177.lookup(rig.session.config, observed, code) == meaning
    assert_no_fact(rig, rig.agent.handle(observed))


@pytest.mark.parametrize('owner', ['slot', 'overlay'])
def test_live_literal_owner_receives_original_alias_instead_of_mapped_fact(rig, monkeypatch, owner):
    original = 'synthetic archive fixture'
    remember_mapping(rig, original, 'remember that Keep Literal SPoTiY')
    if owner == 'slot':
        rig.agent.conversations.get('desktop').pending_intent = PendingIntent(
            'text_edit', {'action': 'dictate'}, (Slot('text', 'text', 'text'),),
            'en', time.monotonic() + 60)
        tool, expected = 'text_edit', {'action': 'dictate', 'text': original}
    else:
        rig.agent._set_pending_overlay_followup('append_text', 'Synthetic Note')
        tool, expected = 'overlay', {'name': 'Synthetic Note', 'action': 'append_text',
                                    'text': original, 'title': 'Synthetic Note'}
    captured = []
    monkeypatch.setattr(rig.agent.tools.get(tool), 'handler',
                        lambda args: captured.append(dict(args)) or ToolExecution(True, 'Inert literal owner'))
    turn = rig.agent.handle(original)
    assert turn.pending is None and captured == [expected]
    assert rig.session.calls == [(tool, expected)]
    rig.note.assert_not_called()
    assert fact_writes(rig) == []


@pytest.mark.parametrize('code,seeded,observed', [
    ('ru', 'не запоминай что пример', 'не запоминай что пример'),
    ('en', 'synthetic archive fixture', '"synthetic archive fixture"'),
])
def test_owner_expiry_cannot_expose_a_vetoed_learned_fact(rig, monkeypatch, code, seeded, observed):
    meaning = ('запомни что ' if code == 'ru' else 'remember that ') + 'Never store fixture'
    remember_mapping(rig, seeded, meaning, code)
    rig.agent._set_pending_overlay_followup('append_text', 'Expiring Note')
    original_owned = rig.agent._handle_stream_owned

    def expire_before_owned(*args, **kwargs):
        rig.agent._pending_overlay_followup['expires_at'] = time.monotonic() - 1
        return original_owned(*args, **kwargs)

    monkeypatch.setattr(rig.agent, '_handle_stream_owned', expire_before_owned)
    assert_no_fact(rig, rig.agent.handle(observed))
