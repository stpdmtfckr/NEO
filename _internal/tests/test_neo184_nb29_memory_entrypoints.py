"""NB29 historical replays through public Agent, real registry and SQLite memory.

Synthetic reconstructed payloads; all external handlers are fail-on-call. The
existing note store defines validity: nonempty text, NOTE_MAX characters, with
no separate 3-character minimum or 200-character storage limit.
"""
from pathlib import Path
from unittest.mock import Mock

import pytest

from jarvis.runtime import memory169
from jarvis.runtime.dialogue_tasks import PendingIntent, Slot
from jarvis.runtime.quality_memory_text184 import text as memory_text
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution

CASES = [
 ('ru', 'Запомни, что ', 'Пожалуйста, ', 'пример: текст', 'плейлист Работа нужен вечером', 'включи плейлист Работа', 'запомни enter: 25'),
 ('en', 'remember that ', 'Please, ', 'example: text', 'playlist Работа is needed tonight', 'play playlist Работа', 'remember enter: 25'),
 ('de', 'Merke dir, dass ', 'Bitte, ', 'Beispiel: Text', 'die Playlist Работа am Abend nötig ist', 'spiele die Playlist Работа', 'merke dir enter: 25'),
 ('fr', 'retiens que ', "S’il te plaît, ", 'exemple: texte', 'la playlist Работа est nécessaire ce soir', 'joue la playlist Работа', 'retiens enter: 25'),
 ('es', 'recuerda que ', 'Por favor, ', 'ejemplo: texto', 'la playlist Работа se necesita esta noche', 'reproduce la playlist Работа', 'recuerda enter: 25'),
 ('it', 'ricorda che ', 'Per favore, ', 'esempio: testo', 'la playlist Работа serve stasera', 'riproduci la playlist Работа', 'ricorda enter: 25'),
 ('pt', 'lembra-te que ', 'Por favor, ', 'exemplo: texto', 'a playlist Работа é necessária à noite', 'toca a playlist Работа', 'memoriza enter: 25'),
]


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        value = AtlasSession(tmp_path_factory.mktemp('nb29') / 'run')
        value.config.data['local_chat']['live_small_talk'] = False
        value.config.data['integrations']['youtube']['music_playlists'] = [
            {'name': 'Работа', 'url': 'https://www.youtube.com/playlist?list=PLNB29SYNTHETIC'}]
        yield value
        value.finish()


@pytest.fixture
def rig(session, monkeypatch):
    session.reset_case('en')
    forbidden = []
    def blocked(name):
        def execute(arguments):
            forbidden.append((name, dict(arguments)))
            raise AssertionError('NB29 forbids external handler: ' + name)
        return execute
    for name, definition in session.agent.tools._tools.items():
        if name != 'memory':
            monkeypatch.setattr(definition, 'handler', blocked(name))
    session.agent.tools.set_execution_guard(lambda name, arguments, risk: session.calls.append((name, dict(arguments))) or None)
    note = Mock(wraps=memory169.note)
    remember = Mock(wraps=memory169.remember)
    write = Mock(wraps=session.agent.database.set_memory)
    monkeypatch.setattr(memory169, 'note', note)
    monkeypatch.setattr(memory169, 'remember', remember)
    monkeypatch.setattr(session.agent.database, 'set_memory', write)
    yield session, note, remember, write, forbidden
    assert forbidden == [], forbidden


def facts_written(write):
    return [(call.args[0], call.args[1]) for call in write.call_args_list if call.args[0].startswith(('note:', 'pref:'))]


def assert_note(rig, case, request, body, *, source='text'):
    session, note, remember, write, forbidden = rig
    session.config.data['app']['language'] = case[0]
    turn = session.agent.handle(request, input_source=source)
    assert turn.pending is None and session.calls == [] and forbidden == []
    assert turn.tool_details == ['memory.remember_that'], turn
    remember.assert_not_called()
    note.assert_called_once_with(session.agent.database, session.config, body, case[0])
    rows = facts_written(write)
    assert len(rows) == 1 and rows[0][0].startswith('note:'), rows
    # The storage layer owns newline/whitespace policy; the exact literal bytes
    # reaching that real layer are asserted above, including CR/LF sequences.
    assert ' '.join(rows[0][1].split()) == ' '.join(body.split())
    assert session.agent.database.get_memory(rows[0][0]) == rows[0][1]
    assert turn.text == memory169.t('note_saved', case[0])
    return turn


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
@pytest.mark.parametrize('polite', [False, True])
def test_nb29_fact_colon_stays_one_note(rig, case, polite):
    assert_note(rig, case, (case[2] if polite else '') + case[1] + case[3], case[3])


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
def test_nb29_complete_key_value_keeps_native_preference_route(rig, case):
    import json
    session, note, remember, write, _ = rig
    session.config.data['app']['language'] = case[0]
    turn = session.agent.handle(case[6])
    assert turn.pending is None
    assert session.calls == [('memory', {'action': 'remember', 'key': 'enter', 'value': '25'})]
    note.assert_not_called()
    remember.assert_called_once_with(session.agent.database, session.config, 'enter', '25', case[0])
    rows = facts_written(write)
    assert len(rows) == 1 and rows[0][0] == 'pref:enter'
    assert json.loads(session.agent.database.get_memory('pref:enter')) == {'name': 'enter', 'value': '25'}


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
@pytest.mark.parametrize('colon', [False, True])
def test_nb29_polite_fact_with_configured_playlist_is_opaque(rig, case, colon):
    body = case[4] + (': ' + case[3] if colon else '')
    assert_note(rig, case, case[2] + case[1] + body, body)


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
@pytest.mark.parametrize('body', ['', ' ', '\t', '\n'])
def test_nb29_empty_fact_clarifies_without_storing_conjunction(rig, case, body):
    session, note, remember, write, _ = rig
    session.config.data['app']['language'] = case[0]
    turn = session.agent.handle(case[1] + body)
    assert turn.text == memory_text('empty_value', case[0]) and turn.outcome == 'clarification'
    assert turn.pending is None and session.calls == [] and facts_written(write) == []
    note.assert_not_called(); remember.assert_not_called()


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
@pytest.mark.parametrize('length', [1, 2, 3])
def test_nb29_short_nonempty_fact_uses_existing_store_policy(rig, case, length):
    body = 'X' * length
    assert_note(rig, case, case[1] + body, body)


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
@pytest.mark.parametrize('length', [199, 200, 201, memory169.NOTE_MAX])
def test_nb29_length_recognition_does_not_change_fact_meaning(rig, case, length):
    ending = ': ' + case[5]
    body = 'X' * (length - len(ending)) + ending
    assert_note(rig, case, case[1] + body, body)


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
def test_nb29_over_store_limit_explicitly_refuses_without_alternate_route(rig, case):
    session, note, remember, write, _ = rig
    session.config.data['app']['language'] = case[0]
    ending = ': ' + case[5]
    body = 'X' * (memory169.NOTE_MAX + 1 - len(ending)) + ending
    turn = session.agent.handle(case[1] + body)
    assert turn.text == memory169.t('too_long', case[0], length=len(body), limit=memory169.NOTE_MAX)
    assert turn.pending is None and session.calls == [] and facts_written(write) == []
    note.assert_called_once_with(session.agent.database, session.config, body, case[0])
    remember.assert_not_called()


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
@pytest.mark.parametrize('source', ['text', 'voice'])
def test_nb29_multiline_fact_never_executes_command_like_second_line(rig, case, newline, source):
    body = case[3] + newline + case[5]
    assert_note(rig, case, case[1] + body, body, source=source)


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
def test_nb29_memory_disabled_still_owns_fact_and_never_falls_through(rig, case):
    session, note, remember, write, _ = rig
    session.config.data['app']['language'] = case[0]
    session.config.data['memory']['enabled'] = False
    try:
        turn = session.agent.handle(case[2] + case[1] + case[5])
        assert turn.text == memory169.t('is_off', case[0])
        assert session.calls == [] and facts_written(write) == [] and turn.pending is None
    finally:
        session.config.data['memory']['enabled'] = True


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
def test_nb29_pending_dictation_keeps_fact_shell_as_literal_data(rig, case, monkeypatch):
    import time
    session, note, remember, write, _ = rig
    session.config.data['app']['language'] = case[0]
    captured = []
    monkeypatch.setattr(session.agent.tools.get('text_edit'), 'handler', lambda args: captured.append(dict(args)) or ToolExecution(True, 'Inert dictation'))
    session.agent.conversations.get('desktop').pending_intent = PendingIntent(
        'text_edit', {'action': 'dictate'}, (Slot('text', 'text', 'text'),), case[0], time.monotonic() + 60)
    text = case[1] + case[3]
    session.agent.handle(text)
    assert captured == [{'action': 'dictate', 'text': text}]
    assert facts_written(write) == []
    note.assert_not_called(); remember.assert_not_called()


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
def test_nb29_legacy_note_prompt_owns_fact_shaped_payload(rig, case, monkeypatch):
    session, note, remember, write, _ = rig
    session.config.data['app']['language'] = case[0]
    captured = []
    monkeypatch.setattr(session.agent.tools.get('overlay'), 'handler', lambda args: captured.append(dict(args)) or ToolExecution(True, 'Inert note'))
    session.agent._set_pending_overlay_followup('append_text', 'Synthetic Note')
    text = case[1] + case[3]
    session.agent.handle(text)
    assert len(captured) == 1 and captured[0]['action'] == 'append_text'
    assert captured[0]['text'] == text and session.agent._pending_overlay_followup is None
    assert facts_written(write) == []
    note.assert_not_called(); remember.assert_not_called()


def test_nb29_fact_supersedes_old_confirmation_questions_without_answering_them(rig, monkeypatch):
    from jarvis.runtime import translation184
    session, note, remember, write, _ = rig
    session.config.data['app']['language'] = 'en'
    forbidden_install = Mock(side_effect=AssertionError('No translation installation'))
    monkeypatch.setattr(translation184, 'installer', forbidden_install)
    translation184.ask(translation184.Request('Synthetic words', 'ru', code='en'))
    session.agent._pending_context = {'tool': 'obsolete_action_that_must_never_run'}
    try:
        session.agent.handle('remember that fixture: open application Calculator')
        assert translation184.pending() is None and session.agent._pending_context is None
        session.agent.handle('yes')
        assert session.calls == []
        assert len(facts_written(write)) == 1
        forbidden_install.assert_not_called()
    finally:
        translation184.forget_question()


_SHUTDOWN = dict(zip(['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'], [
    'выключи компьютер', 'shut down the computer', 'fahre den Computer herunter',
    "éteins l'ordinateur", 'apaga el ordenador', 'spegni il computer', 'desliga o computador']))


@pytest.mark.parametrize('case', CASES, ids=lambda row: row[0])
@pytest.mark.parametrize('failure', ['write_error', 'validation_error', 'validation_refusal', 'empty', 'oversize'])
def test_nb29_recognized_fact_discards_old_authority_before_any_failure(rig, case, failure, monkeypatch):
    import sqlite3
    import time
    from jarvis.runtime import everyday172
    session, note, remember, write, _ = rig
    agent = session.agent
    session.config.data['app']['language'] = case[0]
    proposed = agent.handle(_SHUTDOWN[case[0]])
    assert proposed.pending is not None and proposed.pending.tool_name == 'system'
    assert session.calls == []
    state = agent.conversations.get('desktop')
    agent._pending_target_choice = {'kind': 'app', 'options': [{'label': 'Obsolete synthetic app'}]}
    state.pending_intent = PendingIntent('application', {'action': 'open'},
        (Slot('name', 'name', 'name'),), case[0], time.monotonic() - 1)
    state.pending_shell, state.pending_until = 'obsolete command shell', time.monotonic() + 60
    observed_authority = []
    def observe_authority():
        observed_authority.append((agent._pending_context, agent._pending_target_choice,
                                   state.pending_intent, state.pending_shell, state.pending_alias))
    if failure == 'write_error':
        original = agent.database.set_memory
        def fail_write(key, value):
            if key.startswith('note:'):
                observe_authority()
                raise sqlite3.OperationalError('synthetic read-only database')
            return original(key, value)
        monkeypatch.setattr(agent.database, 'set_memory', fail_write)
    elif failure == 'validation_error':
        def invalid_value(text, code):
            observe_authority()
            raise ValueError('synthetic validation failure')
        monkeypatch.setattr(memory169, '_note_value', invalid_value)
    elif failure == 'validation_refusal':
        def refused_value(text, code):
            observe_authority()
            return text, 'Synthetic validation refusal'
        monkeypatch.setattr(memory169, '_note_value', refused_value)
    body = '' if failure == 'empty' else 'X' * (memory169.NOTE_MAX + 1) if failure == 'oversize' else case[3]
    before = memory169.user_fact_rows(agent.database)
    turn = agent.handle(case[1] + body)
    assert turn.success is False and turn.pending is None
    assert agent._pending_context is None and agent._pending_target_choice is None
    assert state.pending_intent is None and state.pending_shell == '' and state.pending_alias is None
    if observed_authority:
        assert observed_authority == [(None, None, None, '', None)]
    if failure in {'write_error', 'validation_error'}:
        assert turn.text == everyday172.t('remember_failed', case[0])
        assert turn.tool_details == ['memory.remember_failed']
    elif failure == 'validation_refusal':
        assert turn.text == 'Synthetic validation refusal'
    elif failure == 'empty':
        assert turn.text == memory_text('empty_value', case[0])
    else:
        assert turn.text == memory169.t('too_long', case[0], length=len(body), limit=memory169.NOTE_MAX)
    assert memory169.user_fact_rows(agent.database) == before
    assert session.calls == []
    # The same public UI confirmation entrypoint that reproduced the bug now has
    # no authority, even when approval arrives after the failed replacement.
    later = agent.confirm_pending(True)
    assert later.success is not True and later.pending is None and not later.tool_details
    agent.handle({'ru':'да','en':'yes','de':'ja','fr':'oui','es':'sí','it':'sì','pt':'sim'}[case[0]])
    assert session.calls == []
    remember.assert_not_called()


def test_nb29_post_commit_failure_reports_uncertainty_without_retry_or_stale_action(rig, monkeypatch):
    import sqlite3
    from jarvis.runtime import everyday172
    session, note, remember, write, _ = rig
    agent = session.agent
    session.config.data['app']['language'] = 'en'
    assert agent.handle('turn off the computer').pending is not None
    original = agent.database.set_memory
    committed = []
    def commit_then_fail(key, value):
        result = original(key, value)
        if key.startswith('note:'):
            committed.append((key, value))
            assert agent._pending_context is None
            raise sqlite3.OperationalError('synthetic error after commit')
        return result
    monkeypatch.setattr(agent.database, 'set_memory', commit_then_fail)
    turn = agent.handle('remember that Synthetic post-commit fact')
    assert turn.success is False and turn.text == everyday172.t('remember_failed', 'en')
    assert len(committed) == 1 and agent.database.get_memory(committed[0][0]) == 'Synthetic post-commit fact'
    note.assert_called_once()
    assert agent.confirm_pending(True).success is not True and session.calls == []
