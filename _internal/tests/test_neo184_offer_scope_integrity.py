"""R035: pending memory offers and declined facts belong to one conversation."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jarvis.agent_types import AgentTurn
from jarvis.runtime import everyday172 as ev
from jarvis.runtime.conversation_journal import conversation_scope
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked

CASES = [
    ('ru', 'да', 'нет', 'я работаю в фотошопе', 'очисти память диалога'),
    ('en', 'yes', 'no', 'I work at a bakery', 'clear conversation memory'),
    ('de', 'ja', 'nein', 'ich arbeite in Berlin', 'lösche den gesprächsspeicher'),
    ('fr', 'oui', 'non', 'je travaille à Paris', 'efface la mémoire de conversation'),
    ('es', 'sí', 'no', 'trabajo en Madrid', 'borra la memoria de conversación'),
    ('it', 'sì', 'no', 'lavoro a Roma', 'cancella la memoria della conversazione'),
    ('pt', 'sim', 'não', 'trabalho em Lisboa', 'apaga a memória da conversa'),
]


@pytest.fixture(autouse=True)
def isolated_offers(monkeypatch):
    with external_io_blocked():
        monkeypatch.setattr(ev, '_memory_states', OrderedDict())
        yield


@pytest.fixture
def rig(monkeypatch):
    from jarvis.runtime import memory169, screen_help172, tidy172, translation184
    config = SimpleNamespace(get=lambda key, default=None: {'memory.enabled': True, 'memory.propose': True}.get(key, default))
    agent = SimpleNamespace(config=config, database=object())
    note = Mock(return_value=(True, 'Saved fixture fact'))
    monkeypatch.setattr(memory169, 'note', note)
    monkeypatch.setattr(translation184, 'answer_question', lambda *a: None)
    monkeypatch.setattr(translation184, 'handle_command', lambda *a: None)
    monkeypatch.setattr(screen_help172, 'pending', lambda: False)
    monkeypatch.setattr(tidy172, 'pending_plan', lambda: None)
    return agent, note


def pending(scope):
    with conversation_scope(scope):
        return ev._offer_pending()


def offer(scope, text):
    with conversation_scope(scope):
        ev.set_offer(text)


def reply(agent, scope, text, code='en'):
    with conversation_scope(scope):
        return ev.handle(agent, text, code)


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
@pytest.mark.parametrize('message_kind', ['yes', 'no', 'unrelated'])
@pytest.mark.parametrize('source,destination', [('desktop', 'telegram:a'), ('telegram:a', 'desktop'), ('telegram:a', 'telegram:b')])
def test_foreign_chat_cannot_accept_decline_or_clear_an_offer(rig, case, message_kind, source, destination):
    agent, note = rig
    code, yes, no, fact, _ = case
    offer(source, fact)
    text = {'yes': yes, 'no': no, 'unrelated': 'hello'}[message_kind]
    foreign = reply(agent, destination, text, code)
    assert foreign is None
    assert pending(source) == fact and pending(destination) == ''
    note.assert_not_called()
    accepted = reply(agent, source, yes, code)
    assert accepted.detail == 'memory.offer_saved'
    assert note.call_args.args == (agent.database, agent.config, fact, code)
    assert pending(source) == ''


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
def test_same_chat_decline_suppresses_only_that_conversation(rig, case):
    agent, note = rig
    code, yes, no, fact, _ = case
    offer('telegram:a', fact)
    declined = reply(agent, 'telegram:a', no, code)
    assert declined.detail == 'memory.offer_declined'
    with conversation_scope('telegram:a'):
        assert ev.about_me(fact, agent.config) == ''
    for scope in ['desktop', 'telegram:b']:
        with conversation_scope(scope):
            assert ev.about_me(fact, agent.config) == fact
    assert reply(agent, 'telegram:a', yes, code) is None
    note.assert_not_called()


@pytest.mark.parametrize('scope', ['desktop', 'telegram:a', 'telegram:b'])
@pytest.mark.parametrize('text', ['cancel', 'hello'])
def test_same_chat_cancel_or_new_request_expires_offer_without_later_accept(rig, scope, text):
    agent, note = rig
    offer(scope, 'Owned fact')
    assert reply(agent, scope, text) is None
    assert pending(scope) == ''
    assert reply(agent, scope, 'yes') is None
    note.assert_not_called()


def test_offers_can_coexist_and_be_replaced_independently(rig):
    agent, note = rig
    for scope in ['desktop', 'telegram:a', 'telegram:b']:
        offer(scope, scope + ' fact')
    offer('telegram:a', 'replacement fact')
    reply(agent, 'telegram:b', 'yes')
    reply(agent, 'telegram:a', 'yes')
    reply(agent, 'desktop', 'yes')
    assert [call.args[2] for call in note.call_args_list] == ['telegram:b fact', 'replacement fact', 'desktop fact']


def test_unscoped_api_keeps_desktop_default_and_explicit_clear_is_narrow():
    ev.set_offer('Desktop fact')
    offer('telegram:a', 'Remote fact')
    assert ev._offer_pending() == 'Desktop fact'
    ev.clear_offer(scope='telegram:a')
    assert pending('telegram:a') == '' and ev._offer_pending() == 'Desktop fact'
    offer('telegram:a', 'Another remote fact')
    ev.clear_offer()
    assert ev._offer_pending() == '' and pending('telegram:a') == 'Another remote fact'


@pytest.mark.parametrize('age,accepted', [(119.9, True), (120, True), (120.001, False)])
def test_offer_expiry_never_extends_on_reads(rig, monkeypatch, age, accepted):
    agent, note = rig
    clock = [1000.0]
    monkeypatch.setattr(ev.time, 'monotonic', lambda: clock[0])
    offer('telegram:a', 'Expiring fact')
    clock[0] += 90
    assert pending('telegram:a') == 'Expiring fact'
    clock[0] = 1000 + age
    turn = reply(agent, 'telegram:a', 'yes')
    assert bool(turn and turn.detail == 'memory.offer_saved') is accepted
    assert note.call_count == int(accepted)
    assert pending('telegram:a') == ''


def test_empty_remote_reads_do_not_evict_valid_offer():
    offer('desktop', 'Keep this offer')
    for index in range(ev._MEMORY_SCOPE_LIMIT * 3):
        assert pending('telegram:' + str(index)) == ''
    assert pending('desktop') == 'Keep this offer'
    assert len(ev._memory_states) == 1


def test_scope_store_is_bounded_and_eviction_does_not_resurrect_old_offers():
    for index in range(ev._MEMORY_SCOPE_LIMIT + 1):
        offer('telegram:' + str(index), 'Fact ' + str(index))
    assert len(ev._memory_states) == ev._MEMORY_SCOPE_LIMIT
    assert pending('telegram:0') == ''
    assert pending('telegram:' + str(ev._MEMORY_SCOPE_LIMIT)) == 'Fact ' + str(ev._MEMORY_SCOPE_LIMIT)


def test_decline_store_is_bounded_and_oldest_suppression_expires(rig):
    agent, _ = rig
    with conversation_scope('telegram:a'):
        for index in range(ev._MEMORY_DECLINE_LIMIT + 1):
            ev._decline_offer('I work at bakery ' + str(index))
        assert len(ev._memory_states['telegram:a'].declined) == ev._MEMORY_DECLINE_LIMIT
        assert ev.about_me('I work at bakery 0', agent.config) == 'I work at bakery 0'
        assert ev.about_me('I work at bakery ' + str(ev._MEMORY_DECLINE_LIMIT), agent.config) == ''


def test_idle_scope_expiry_removes_offer_and_decline_state(rig, monkeypatch):
    agent, _ = rig
    clock = [1000.0]
    monkeypatch.setattr(ev.time, 'monotonic', lambda: clock[0])
    with conversation_scope('telegram:a'):
        ev._decline_offer('I work at a bakery')
        ev.set_offer('Pending fact')
    clock[0] += ev._MEMORY_SCOPE_TTL + 1
    with conversation_scope('telegram:a'):
        assert ev._offer_pending() == ''
        assert ev.about_me('I work at a bakery', agent.config) == 'I work at a bakery'
    assert not ev._memory_states


def test_atomic_consumption_and_thread_context_isolation():
    offer('telegram:a', 'Single-use fact')
    def take(_):
        with conversation_scope('telegram:a'):
            return ev._take_offer()
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(take, range(32)))
    assert results.count('Single-use fact') == 1
    assert results.count('') == 31
    assert ev._offer_pending() == ''


@pytest.fixture
def session(tmp_path):
    value = AtlasSession(tmp_path / 'run', locale='en')
    value.config.data['local_chat']['live_small_talk'] = False
    yield value
    value.finish()


@pytest.mark.parametrize('text', ['yes', 'no', 'hello', 'remember alias "work mode" as "open application Calculator"'])
def test_real_agent_telegram_does_not_consume_desktop_offer(session, monkeypatch, text):
    from jarvis.runtime import memory169
    note = Mock(return_value=(True, 'Saved fixture fact'))
    monkeypatch.setattr(memory169, 'note', note)
    ev.set_offer('Desktop-only fact')
    session.agent.handle(text, input_source='telegram', conversation_id='remote-a')
    assert ev._offer_pending() == 'Desktop-only fact'
    note.assert_not_called()
    assert session.calls == []
    session.agent.handle('yes')
    assert note.call_count == 1 and note.call_args.args[2] == 'Desktop-only fact'


@pytest.mark.parametrize('scope', ['desktop', 'telegram:a'])
def test_agent_reset_clears_only_active_offer_and_decline_state(session, scope):
    for key in ['desktop', 'telegram:a', 'telegram:b']:
        with conversation_scope(key):
            ev._decline_offer('I work at a bakery')
            ev.set_offer(key + ' fact')
    session.agent._activate_conversation(scope)
    session.agent.reset()  # no ambient conversation_scope: explicit active key is required
    for key in ['desktop', 'telegram:a', 'telegram:b']:
        with conversation_scope(key):
            assert ev._offer_pending() == ('' if key == scope else key + ' fact')
            assert ev.about_me('I work at a bakery', session.config) == ('I work at a bakery' if key == scope else '')


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
def test_clear_history_clears_only_target_scope_memory_questions(session, case):
    code, yes, no, fact, clear = case
    session.config.data['app']['language'] = code
    for key in ['desktop', 'telegram:a', 'telegram:b']:
        with conversation_scope(key):
            ev._decline_offer(fact)
            ev.set_offer(key + ' pending fact')
    turn = session.agent.handle(clear, input_source='telegram', conversation_id='a')
    assert turn.outcome == 'memory_cleared'
    assert pending('desktop') == 'desktop pending fact'
    assert pending('telegram:b') == 'telegram:b pending fact'
    with conversation_scope('telegram:a'):
        assert ev._offer_pending() == ''
        assert ev.about_me(fact, session.config) == fact
    assert session.calls == []


def test_unshown_offer_cleanup_uses_completed_turn_scope_outside_context(session, monkeypatch):
    ev.set_offer('Keep desktop question')
    def invisible(*args, **kwargs):
        ev.set_offer('Never shown remote question')
        session.agent._memory_offer172 = 'Never shown offer'
        return AgentTurn('A command result')
    monkeypatch.setattr(session.agent, '_handle_stream_unscoped', invisible)
    session.agent.handle('fixture', input_source='telegram', conversation_id='a')
    assert pending('telegram:a') == ''
    assert ev._offer_pending() == 'Keep desktop question'


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
def test_real_agent_creates_and_accepts_visible_offer_in_its_own_chat(session, monkeypatch, case):
    from jarvis.runtime import local_chat169, memory169
    code, yes, no, fact, _ = case
    session.config.data['app']['language'] = code
    monkeypatch.setattr(local_chat169, 'answer', lambda *a, **k: 'Fixture conversation reply')
    note = Mock(return_value=(True, 'Saved fixture fact'))
    monkeypatch.setattr(memory169, 'note', note)
    turn = session.agent.handle(fact, input_source='telegram', conversation_id='a')
    assert ev.t('remember_offer', code, text=fact) in turn.text
    assert pending('telegram:a') == fact and pending('desktop') == ''
    session.agent.handle(yes, input_source='telegram', conversation_id='b')
    note.assert_not_called()
    assert pending('telegram:a') == fact
    accepted = session.agent.handle(yes, input_source='telegram', conversation_id='a')
    assert accepted.tool_details == ['memory.offer_saved']
    assert note.call_args.args[2:] == (fact, code)
    assert pending('telegram:a') == '' and session.calls == []


def test_italian_native_affirmative_accepts_only_current_offer(rig):
    agent, note = rig
    offer('telegram:a', 'lavoro a Roma')
    assert reply(agent, 'telegram:b', 'sì', 'it') is None
    note.assert_not_called()
    assert reply(agent, 'telegram:a', 'sì', 'it').detail == 'memory.offer_saved'
    assert note.call_count == 1
