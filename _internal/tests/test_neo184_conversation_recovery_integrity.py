"""R023/R024: restored answers and exact, confirmed aliases through the real Agent.

All profiles are disposable; tool execution, network and process launches are blocked.
"""
import time
from unittest.mock import Mock

import pytest

from jarvis.runtime import everyday172, local_chat169, reply_memory168
from jarvis.runtime.dialogue_tasks import PendingIntent, Slot
from jarvis.testing.atlas_session import AtlasConfig, AtlasSession, AuditAgent
from jarvis.testing.isolation import external_io_blocked

CASES = [
    ('ru', 'Запомни команду «{alias}» как «{command}»', 'открой приложение Calculator', 'да', 'Повтори свой предыдущий ответ', 'Расскажи шутку'),
    ('en', 'remember alias "{alias}" as "{command}"', 'open application Calculator', 'yes', 'repeat your previous answer', 'tell me a joke'),
    ('de', 'merke den alias "{alias}" als "{command}"', 'öffne das Programm Calculator', 'ja', 'wiederhole deine letzte antwort', 'erzähle einen witz'),
    ('fr', 'mémorise le raccourci «{alias}» comme «{command}»', "ouvre l'application Calculator", 'oui', 'répète ta dernière réponse', 'raconte une blague'),
    ('es', 'recuerda el alias "{alias}" como "{command}"', 'abre la aplicación Calculator', 'sí', 'repite tu última respuesta', 'cuenta un chiste'),
    ('it', 'ricorda l\'alias "{alias}" come "{command}"', "apri l'applicazione Calculator", 'sì', 'ripeti la tua ultima risposta', 'racconta una barzelletta'),
    ('pt', 'memoriza o atalho «{alias}» como «{command}»', 'abre a aplicação Calculator', 'sim', 'repete a tua última resposta', 'conta uma piada'),
]

@pytest.fixture
def session(tmp_path):
    with external_io_blocked():
        everyday172.clear_offer()
        reply_memory168.clear()
        local_chat169.clear_history()
        value = AtlasSession(tmp_path / 'run')
        value.config.data['local_chat']['live_small_talk'] = False
        yield value
        value.finish()
        everyday172.clear_offer()
        reply_memory168.clear()
        local_chat169.clear_history()


def configure(session, code):
    session.config.data['app']['language'] = code
    return session.agent


def proposal(case, alias='Work Mode QA!?'):
    return case[1].format(alias=alias, command=case[2])


@pytest.mark.parametrize('case', CASES + [(CASES[0][0], CASES[0][1], CASES[0][2], CASES[0][3], 'Повтори ответ', CASES[0][5])], ids=lambda row: row[0])
def test_persisted_answer_repeats_after_fresh_agent(session, monkeypatch, case):
    code, _, _, _, repeat, request = case
    agent = configure(session, code)
    session.config.data['activity']['enabled'] = True
    # Joke rotation may mention credentials, which the journal rightly omits.
    monkeypatch.setattr(agent.social_atlas, 'reply', lambda *a, **k: 'Fixture answer ' + code + ': Exact CASE, é!?')
    expected = agent.handle(request).text
    assert expected and len(agent.conversation_journal.recent('desktop')) == 2
    session.finish()
    reply_memory168.clear()
    local_chat169.clear_history()
    config = AtlasConfig(session.output / 'isolated_profile', code)
    config.data['activity']['enabled'] = True
    config.data['local_chat']['live_small_talk'] = False
    restored = AuditAgent(config)
    denied = Mock(return_value='blocked')
    restored.tools.set_execution_guard(denied)
    try:
        assert restored.conversations.get('desktop').last_reply == expected
        for _ in range(2):
            turn = restored.handle(repeat)
            assert turn.text == expected
            assert turn.tool_details == ['dialogue.repeat_answer']
        denied.assert_not_called()
    finally:
        restored.database.close()


def test_restart_repeat_keeps_each_channel_isolated_and_empty_scope_empty(session):
    agent = configure(session, 'en')
    session.config.data['activity']['enabled'] = True
    agent.conversation_journal.record_turn('desktop', 'question', 'Desktop answer: Exact CASE!?')
    agent.conversation_journal.record_turn('telegram:chat-a', 'question', 'Telegram A answer: é!?')
    session.finish()
    config = AtlasConfig(session.output / 'isolated_profile', 'en')
    config.data['activity']['enabled'] = True
    restored = AuditAgent(config)
    denied = Mock(return_value='blocked')
    restored.tools.set_execution_guard(denied)
    try:
        for source, cid, expected in [('text', '', 'Desktop answer: Exact CASE!?'),
                                     ('telegram', 'chat-a', 'Telegram A answer: é!?'),
                                     ('telegram', 'chat-b', everyday172.t('nothing_to_repeat184', 'en')),
                                     ('text', '', 'Desktop answer: Exact CASE!?'),
                                     ('telegram', 'chat-a', 'Telegram A answer: é!?')]:
            assert restored.handle('repeat your previous answer', input_source=source, conversation_id=cid).text == expected
        denied.assert_not_called()
    finally:
        restored.database.close()


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
def test_explicit_alias_preserves_payload_and_never_executes_while_saving(session, case):
    agent = configure(session, case[0])
    alias = 'Work Mode QA!?'
    before = agent.database.list_memories(limit=200)
    turn = agent.handle(proposal(case, alias))
    state = agent.conversations.get('desktop')
    assert state.pending_alias is not None, turn
    assert state.pending_alias['phrase'] == alias and state.pending_alias['command'] == case[2]
    assert alias + ' → ' + case[2] in turn.text
    assert agent.personal_aliases.resolve(alias, case[0]) == ''
    assert agent.database.list_memories(limit=200) == before
    assert session.calls == [] and session.executions == []
    agent.handle(case[3])
    assert state.pending_alias is None
    assert agent.personal_aliases.resolve(alias, case[0]) == case[2]
    assert session.calls == [] and session.executions == []
    agent.handle(alias)
    assert len(session.calls) == 1
    assert session.calls[0]['tool'] == 'application'
    assert session.calls[0]['arguments'] == {'action': 'open', 'name': 'Calculator'}


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
def test_pending_text_slot_owns_even_exact_alias_request(session, case):
    agent = configure(session, case[0])
    state = agent.conversations.get('desktop')
    state.pending_intent = PendingIntent('text_edit', {'action': 'dictate'}, (Slot('text', 'text', 'text'),), case[0], time.monotonic() + 60)
    text = proposal(case)
    agent.handle(text)
    assert state.pending_alias is None
    assert agent.personal_aliases.rows(case[0]) == {}
    assert len(session.calls) == 1
    assert session.calls[0]['tool'] == 'text_edit'
    assert session.calls[0]['arguments'] == {'action': 'dictate', 'text': text}


@pytest.mark.parametrize('code,text,expected', [
    ('ru', 'запомни что встреча завтра', 'встреча завтра'),
    ('en', 'remember that meeting is tomorrow', 'meeting is tomorrow'),
    ('de', 'merke dir, dass das Treffen morgen ist', 'das Treffen morgen ist'),
    ('fr', 'retiens que la réunion est demain', 'la réunion est demain'),
    ('es', 'recuerda que la reunión es mañana', 'la reunión es mañana'),
    ('it', 'ricorda che la riunione è domani', 'la riunione è domani'),
    ('pt', 'lembra-te que a reunião é amanhã', 'a reunião é amanhã'),
])
def test_normal_remember_note_keeps_its_existing_route(session, monkeypatch, code, text, expected):
    from jarvis.runtime import memory169
    note = Mock(return_value=(True, 'Saved fixture note'))
    monkeypatch.setattr(memory169, 'note', note)
    agent = configure(session, code)
    agent.handle(text)
    assert note.call_args.args[2:] == (expected, code)
    assert agent.conversations.get('desktop').pending_alias is None
    assert session.calls == []


@pytest.mark.parametrize('finish', ['cancel', 'no', 'unrelated', 'expired', 'language', 'replacement'])
def test_abandoned_alias_cannot_be_revived_by_yes(session, finish):
    agent = configure(session, 'en')
    agent.handle(proposal(CASES[1]))
    state = agent.conversations.get('desktop')
    if finish == 'expired':
        state.pending_until = time.monotonic() - 1
    elif finish == 'language':
        session.config.data['app']['language'] = 'ru'
        agent.handle('да')
        session.config.data['app']['language'] = 'en'
    elif finish == 'replacement':
        agent.handle(proposal(CASES[1], alias='Other Mode QA!?'))
    else:
        agent.handle({'cancel': 'cancel', 'no': 'no', 'unrelated': 'hello'}[finish])
    agent.handle('yes')
    assert agent.personal_aliases.resolve('Work Mode QA!?', 'en') == ''
    if finish == 'replacement':
        assert agent.personal_aliases.resolve('Other Mode QA!?', 'en') == CASES[1][2]
    assert session.calls == []


def test_alias_proposal_replaces_previous_confirmation_and_offer(session):
    agent = configure(session, 'en')
    agent._pending_context = {'tool': 'fixture-that-must-never-run'}
    everyday172.set_offer('Unrelated previous fact')
    agent.handle(proposal(CASES[1]))
    assert agent._pending_context is None
    assert everyday172._offer_pending() == ''
    agent.handle('yes')
    assert agent.personal_aliases.resolve('Work Mode QA!?', 'en') == CASES[1][2]
    assert session.calls == []


def test_foreign_channel_yes_does_not_save_desktop_proposal(session):
    agent = configure(session, 'en')
    agent.handle(proposal(CASES[1]))
    agent.handle('yes', input_source='telegram', conversation_id='chat-a')
    assert agent.personal_aliases.resolve('Work Mode QA!?', 'en') == ''
    assert agent.conversations.get('desktop').pending_alias is not None
    agent.handle('yes')
    assert agent.personal_aliases.resolve('Work Mode QA!?', 'en') == CASES[1][2]
    assert session.calls == []


@pytest.mark.parametrize('alias', ['yes', 'stop', ' ', 'x' * 121])
def test_invalid_alias_never_creates_a_confirmable_proposal(session, alias):
    agent = configure(session, 'en')
    agent.handle(proposal(CASES[1], alias=alias))
    assert agent.conversations.get('desktop').pending_alias is None
    agent.handle('yes')
    assert agent.personal_aliases.rows('en') == {}
    assert session.calls == []


@pytest.mark.parametrize('setting', ['activity_disabled', 'memory_disabled', 'paused'])
def test_restore_respects_journal_privacy_settings(session, setting):
    agent = configure(session, 'en')
    session.config.data['activity']['enabled'] = True
    agent.conversation_journal.record_turn('desktop', 'question', 'Saved scoped fixture')
    session.finish()
    config = AtlasConfig(session.output / 'isolated_profile', 'en')
    config.data['activity']['enabled'] = setting != 'activity_disabled'
    config.data['activity']['paused'] = setting == 'paused'
    config.data.setdefault('dialogue', {}).setdefault('memory', {})['enabled'] = setting != 'memory_disabled'
    restored = AuditAgent(config)
    restored.tools.set_execution_guard(lambda *a: 'blocked')
    try:
        assert restored.history == [] and restored._last_visible_answer184 == ''
        assert restored.handle('repeat your previous answer').text == everyday172.t('nothing_to_repeat184', 'en')
    finally:
        restored.database.close()


def test_unconfirmed_alias_is_not_restored_after_restart(session):
    agent = configure(session, 'en')
    session.config.data['activity']['enabled'] = True
    agent.handle(proposal(CASES[1]))
    session.finish()
    config = AtlasConfig(session.output / 'isolated_profile', 'en')
    config.data['activity']['enabled'] = True
    restored = AuditAgent(config)
    denied = Mock(return_value='blocked')
    restored.tools.set_execution_guard(denied)
    try:
        assert restored.conversations.get('desktop').pending_alias is None
        restored.handle('yes')
        assert restored.personal_aliases.rows('en') == {}
        denied.assert_not_called()
    finally:
        restored.database.close()


@pytest.mark.parametrize('case,prefix', list(zip(CASES, ['напиши: ', 'type: ', 'schreibe: ', 'écris: ', 'escribe: ', 'scrivi: ', 'escreve: '])), ids=[row[0] for row in CASES])
def test_explicit_dictation_keeps_embedded_alias_request_literal(session, case, prefix):
    agent = configure(session, case[0])
    payload = proposal(case)
    agent.handle(prefix + payload)
    assert agent.conversations.get('desktop').pending_alias is None
    assert agent.personal_aliases.rows(case[0]) == {}
    assert len(session.calls) == 1
    assert session.calls[0]['tool'] == 'text_edit'
    assert session.calls[0]['arguments'] == {'action': 'dictate', 'text': payload}


@pytest.mark.parametrize('text', [
    'remember alias "Exact Alias" comme "open application Calculator"',
    'remember alias «Exact Alias" as "open application Calculator»',
    'remember alias "Exact Alias» as «open application Calculator"',
])
def test_malformed_or_foreign_alias_grammar_never_claims_confirmation(session, text):
    agent = configure(session, 'en')
    agent.handle(text)
    assert agent.conversations.get('desktop').pending_alias is None
    agent.handle('yes')
    assert agent.personal_aliases.rows('en') == {}
    assert session.calls == []


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
def test_approved_alias_retains_quoted_edge_and_internal_whitespace(session, case):
    from jarvis.runtime.conversation import PersonalAliases
    agent = configure(session, case[0])
    alias = '  Exact  Alias É!?  '
    command = '  ' + case[2] + '  '
    turn = agent.handle(case[1].format(alias=alias, command=command))
    assert alias + ' → ' + command in turn.text
    agent.handle(case[3])
    fresh = PersonalAliases(agent.database)
    assert fresh.resolve('exact alias é', case[0]) == command
    assert list(fresh.rows(case[0]).values()) == [{'phrase': alias, 'command': command, 'enabled': True}]
    assert session.calls == []


@pytest.mark.parametrize('phrase,command', [
    (' ', 'open application Calculator'),
    ('yes', 'open application Calculator'),
    ('alias', ' '),
    ('x' * 121, 'open application Calculator'),
    ('alias', 'x' * 1001),
    ('alias', ' ' + 'x' * 1000),
    ('alias', 'Alias!?'),
])
def test_personal_alias_api_rejects_invalid_payload_without_mutation(session, phrase, command):
    aliases = session.agent.personal_aliases
    with pytest.raises(ValueError):
        aliases.add(phrase, command, 'en', confirmed=True)
    assert aliases.rows('en') == {}


def test_personal_alias_api_requires_approval_and_normalizes_only_lookup(session):
    from jarvis.runtime.conversation import PersonalAliases
    aliases = session.agent.personal_aliases
    with pytest.raises(ValueError):
        aliases.add('Alias', 'command', 'en')
    assert aliases.rows('en') == {}
    aliases.add('  Exact  Alias É!?  ', '  Exact Command É!?  ', 'en', confirmed=True)
    assert aliases.resolve('EXACT ALIAS É', 'en') == '  Exact Command É!?  '
    assert aliases.set_enabled('exact alias é?', 'en', False)
    assert aliases.resolve('Exact Alias É', 'en') == ''
    assert aliases.set_enabled('Exact Alias É', 'en', True)
    assert PersonalAliases(session.agent.database).resolve('exact alias é', 'en') == '  Exact Command É!?  '
    assert aliases.delete('  Exact Alias É!?', 'en')
    assert aliases.rows('en') == {}
    aliases.add('x' * 120, 'y' * 1000, 'en', confirmed=True)
    assert aliases.resolve('X' * 120, 'en') == 'y' * 1000


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
@pytest.mark.parametrize('action,key', [('append_text', 'text'), ('checklist_add', 'item')])
def test_legacy_note_prompt_owns_exact_alias_shaped_payload(session, case, action, key):
    agent = configure(session, case[0])
    agent._set_pending_overlay_followup(action, 'QA Notes')
    payload = proposal(case)
    agent.handle(payload)
    assert agent.conversations.get('desktop').pending_alias is None
    assert agent.personal_aliases.rows(case[0]) == {}
    assert agent._pending_overlay_followup is None
    assert len(session.calls) == 1 and session.calls[0]['tool'] == 'overlay'
    assert session.calls[0]['arguments'][key] == payload
    assert session.calls[0]['arguments']['action'] == action
    agent.handle(case[3])
    assert len(session.calls) == 1


@pytest.mark.parametrize('ending', ['cancel', 'expired', 'confirmed'])
def test_new_alias_drops_stale_application_choice_before_any_later_yes(session, ending):
    agent = configure(session, 'ru')
    agent._pending_target_choice = {'kind': 'app', 'options': [{'label': 'Calculator', 'kind': 'path', 'value': 'fixture.exe'}]}
    agent.handle(proposal(CASES[0]))
    assert agent._pending_target_choice is None
    if ending == 'expired':
        agent.conversations.get('desktop').pending_until = time.monotonic() - 1
    else:
        agent.handle('отмена' if ending == 'cancel' else 'да')
    agent.handle('да')
    assert session.calls == [] and session.executions == []


def test_new_window_command_cannot_leave_alias_waiting_for_a_later_yes(session, monkeypatch):
    from jarvis.runtime import self_window184
    agent = configure(session, 'en')
    agent.handle(proposal(CASES[1]))
    monkeypatch.setattr(self_window184, 'configured_action', lambda _config, text: 'hide' if text == 'hide yourself' else '')
    monkeypatch.setattr(self_window184, 'queue_visibility', lambda _action: True)
    agent.handle('hide yourself')
    agent.handle('yes')
    assert agent.conversations.get('desktop').pending_alias is None
    assert agent.personal_aliases.rows('en') == {}
    assert session.calls == []


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
def test_unapproved_alias_invocation_never_executes_or_implicitly_saves(session, case):
    agent = configure(session, case[0])
    agent.handle(proposal(case))
    agent.handle('Work Mode QA!?')
    agent.handle(case[3])
    assert agent.conversations.get('desktop').pending_alias is None
    assert agent.personal_aliases.rows(case[0]) == {}
    assert session.calls == []
