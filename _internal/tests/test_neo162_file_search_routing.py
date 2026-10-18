"""Exercise local lookup through the production Agent without external I/O."""
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from jarvis.runtime.feature_commands import parse_feature_command
from jarvis.runtime.file_requests162 import parse_file_request
from jarvis.runtime.dialogue_tasks import prepare_dialogue, record_attempt
from jarvis.runtime.conversation import ConversationState
from jarvis.runtime.natural_commands import LANGUAGES

HEADS = ('найди файл', 'find a file', 'finde eine Datei', 'trouve un fichier',
         'busca un archivo', 'trova il file', 'encontra um ficheiro')
WHERES = ('где находится файл', 'where is the file', 'wo ist die Datei',
          'où est le fichier', 'dónde está el archivo', 'dove è il file', 'onde está o ficheiro')
SCOPES = ('в папке', 'in the folder', 'in dem Ordner', 'dans le dossier',
          'en la carpeta', 'nella cartella', 'na pasta')
CONTENT = ('найди файл по содержимому', 'find a file containing', 'finde eine Datei mit dem Inhalt',
           'trouve un fichier contenant', 'busca un archivo que contenga',
           'trova il file che contenga', 'encontra um ficheiro que contenha')
INSTALLED = ('где установлено приложение', 'where is the application installed',
             'wo ist die Anwendung installiert', "où est installée l’application",
             'dónde está instalada la aplicación', "dove è installata l’applicazione",
             'onde está instalada a aplicação')


@pytest.mark.parametrize('code', LANGUAGES)
def test_name_scope_and_case_are_literal(code):
    i = LANGUAGES.index(code)
    query = 'Мой Reprot FINAL.txt'
    root = r'C:\Users\Иван\Мои документы'
    text = f'{WHERES[i]} "{query}" {SCOPES[i]} "{root}"'
    assert parse_feature_command(text, code) == ('search', {'action': 'files', 'query': query, 'root': root, 'literal': True})
    assert parse_file_request(f'{HEADS[i]} "name {SCOPES[i]} Downloads.txt"', code) == (
        'search', {'action': 'files', 'query': f'name {SCOPES[i]} Downloads.txt', 'literal': True})


@pytest.mark.parametrize('code', LANGUAGES)
def test_missing_name_preserves_selected_folder_and_asks(code):
    i = LANGUAGES.index(code)
    state = ConversationState(language=code)
    parse = lambda value: parse_feature_command(value, code)
    first = prepare_dialogue(f'{HEADS[i]} {SCOPES[i]} Downloads', code, state, parse)
    assert first.reply and first.feature is None
    second = prepare_dialogue('Reprot 2026.txt', code, state, parse)
    assert second.feature == ('search', {'action': 'files', 'root': 'Downloads', 'query': 'Reprot 2026.txt'})


@pytest.mark.parametrize('code', LANGUAGES)
def test_missing_installed_application_is_not_a_literal_application_title(code):
    i = LANGUAGES.index(code)
    state = ConversationState(language=code)
    parse = lambda value: parse_feature_command(value, code)
    first = prepare_dialogue(INSTALLED[i], code, state, parse)
    assert first.reply and not first.feature
    second = prepare_dialogue('Darktide', code, state, parse)
    assert second.feature == ('game_locations', {'action': 'find', 'kind': 'all', 'name': 'Darktide'})


@pytest.mark.parametrize('code', LANGUAGES)
def test_content_clarification_accepts_next_message_as_data(code):
    i = LANGUAGES.index(code)
    state = ConversationState(language=code)
    parse = lambda value: parse_feature_command(value, code)
    first = prepare_dialogue(CONTENT[i], code, state, parse)
    assert first.reply and state.pending_intent.tool == 'search'
    phrase = ('открой файл', 'open a file', 'öffne eine Datei', 'ouvre un fichier',
              'abre un archivo', 'apri un file', 'abre um ficheiro')[i]
    second = prepare_dialogue(phrase, code, state, parse)
    assert second.feature == ('search', {'action': 'content', 'query': phrase})


@pytest.mark.parametrize('code,text', [
    ('ru', 'не найди файл отчет.txt'), ('en', 'do not find a file report.txt'),
    ('ru', 'найди в интернете инструкцию по игре'), ('en', 'find a song named File'),
    ('ru', 'открой файл отчет.txt'), ('en', 'open the folder Downloads'),
    ('ru', 'сколько уровней в игре Darktide'), ('en', 'where is London'),
])
def test_local_search_does_not_capture_other_actions(code, text):
    assert parse_file_request(text, code) is None


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        value = AtlasSession(tmp_path_factory.mktemp('file-routing162') / 'run')
        yield value
        value.finish()


@pytest.mark.parametrize('code', LANGUAGES)
def test_agent_routes_location_and_scoped_search_before_web_or_open(session, code):
    i = LANGUAGES.index(code)
    session.reset_case(code)
    result = session.agent.handle(f'{WHERES[i]} "Reprot FINAL.txt" {SCOPES[i]} Downloads')
    assert len(session.calls) == 1, result.text
    assert session.calls[0]['tool'] == 'search'
    assert session.calls[0]['arguments'] == {'action': 'files', 'query': 'Reprot FINAL.txt', 'root': 'Downloads', 'literal': True}
    session.reset_case(code)
    first = session.agent.handle(HEADS[i])
    assert first.outcome == 'clarification' and not session.calls, first.text
    result = session.agent.handle('Reprot FINAL.txt')
    assert len(session.calls) == 1, result.text
    assert session.calls[0]['tool'] == 'search'
    assert session.calls[0]['arguments'] == {'action': 'files', 'query': 'Reprot FINAL.txt'}


def test_actual_search_result_can_be_opened_by_followup_without_renaming(tmp_path):
    path = tmp_path / 'Reprot FINAL.txt'
    path.write_text('test', encoding='utf-8')
    state = ConversationState(language='ru')
    now = time.monotonic()
    execution = SimpleNamespace(success=True, text=str(path), data={'results': [{'path': str(path)}]})
    record_attempt(state, 'search', {'action': 'files', 'query': 'Reprot'}, execution, now=now)
    followup = prepare_dialogue('открой его', 'ru', state, lambda text: parse_feature_command(text, 'ru'), now=now + .01)
    assert followup.feature == ('filesystem', {'action': 'open', 'source': str(path)})
    assert followup.guard157['path'] == str(path)


@pytest.mark.parametrize('code', LANGUAGES)
def test_localized_search_never_translates_cyrillic_user_paths(code):
    from jarvis.runtime.localized_text import render_action_result
    text = r'1. C:\Users\Иван\Документы\Reprot.txt'
    for tool, action in [('search', 'files'), ('search', 'content'), ('filesystem', 'find')]:
        assert render_action_result(tool, {'action': action}, True, text, {'results': []}, code) == text


@pytest.mark.parametrize('code', LANGUAGES)
def test_real_agent_executes_scoped_lookup_and_displays_paths(session, code):
    i = LANGUAGES.index(code)
    session.reset_case(code)
    root = session.workspace / ('search-' + code)
    root.mkdir(exist_ok=True)
    path = root / 'Reprot Иван.txt'
    path.write_text('needle162', encoding='utf-8')
    args = {'action': 'files', 'query': path.name, 'root': str(root), 'literal': True}
    session._permitted.append(('search', args))
    result = session.agent.handle(f'{HEADS[i]} "{path.name}" {SCOPES[i]} "{root}"')
    assert result.success, result.text
    assert len(session.calls) == 1 and session.calls[0]['arguments'] == args
    assert str(path) in result.text, result.text
    assert 'needle162' not in result.text


def test_both_lookup_tools_share_bounded_scanner_and_keep_nonrecursive_scope(session):
    session.reset_case('ru')
    root = session.workspace / 'legacy-search'
    root.mkdir(exist_ok=True)
    direct = root / 'report-a.txt'
    direct.write_text('first', encoding='utf-8')
    nested = root / 'nested' / 'report-b.txt'
    nested.parent.mkdir(exist_ok=True)
    nested.write_text('second', encoding='utf-8')
    for recursive, expected in [(False, [direct]), (True, [direct, nested])]:
        args = {'action': 'find', 'source': str(root), 'query': 'report*.txt', 'recursive': recursive}
        session._permitted.append(('filesystem', args))
        result = session.agent.tools.execute('filesystem', args)
        assert result.success, result.text
        assert set(result.data) == {str(p) for p in expected}
        assert all(str(p) in result.text for p in expected)


def test_invalid_scope_cannot_silently_search_other_roots(session):
    session.reset_case('en')
    root = str(session.workspace / 'DOES_NOT_EXIST')
    for tool, args in [
        ('search', {'action': 'files', 'query': 'source.txt', 'root': root}),
        ('filesystem', {'action': 'find', 'query': 'source.txt', 'source': root}),
    ]:
        session._permitted.append((tool, args))
        result = session.agent.tools.execute(tool, args)
        assert not result.success
        assert not isinstance(result.data, list)


def test_empty_completed_search_reports_no_results_not_a_fake_success(session):
    session.reset_case('ru')
    root = session.workspace / 'empty-search'
    root.mkdir(exist_ok=True)
    args = {'action': 'files', 'query': 'missing-162', 'root': str(root)}
    session._permitted.append(('search', args))
    result = session.agent.tools.execute('search', args)
    assert not result.success, result.text
    assert result.data['results'] == [] and result.data['complete'] is True


def test_quoted_date_title_and_brackets_survive_real_agent_lookup(session):
    session.reset_case('en')
    root = session.workspace / 'literal-search'
    root.mkdir(exist_ok=True)
    for name in ['Yesterday', '[2026].txt', '2026.txt']:
        (root / name).write_text('fixture', encoding='utf-8')
    for name in ['Yesterday', '[2026].txt']:
        session.reset_case('en')
        args = {'action': 'files', 'query': name, 'root': str(root), 'literal': True}
        session._permitted.append(('search', args))
        result = session.agent.handle(f'find the file "{name}" in the folder "{root}"')
        assert result.success, result.text
        rows = session.executions[-1].data['results']
        assert [row['path'] for row in rows] == [str(root / name)]


@pytest.mark.parametrize('phrase,root', [
    ('найди файл report.txt в загрузках', 'загрузках'),
    ('find a file report.txt in Downloads', 'Downloads'),
    ('найди файл report.txt в D:\\Reports', r'D:\Reports'),
])
def test_common_directory_qualifiers(phrase, root):
    code = 'en' if phrase.startswith('find') else 'ru'
    assert parse_feature_command(phrase, code) == ('search', {'action': 'files', 'query': 'report.txt', 'root': root})


def test_search_job_continues_and_replaces_one_progress_section(monkeypatch, tmp_path):
    from jarvis.runtime import search_jobs162 as jobs
    from jarvis.runtime.file_search import FileSearchResults
    from jarvis.runtime.briefing_stream import stream_to
    from jarvis.runtime.cancellation import cancellation_manager
    cancellation_manager().reset()
    calls, events = [], []
    def scan(query, **kwargs):
        calls.append(kwargs)
        return FileSearchResults([{'path': str(tmp_path / f'report-{len(calls)}.txt'), 'name': 'report'}],
                                 incomplete=len(calls) < 3, continuable=len(calls) < 3)
    monkeypatch.setattr(jobs, 'search_files', scan)
    with stream_to(events.append):
        rows = jobs.run_search('report', roots=[tmp_path])
    assert len(calls) == 3 and not rows.incomplete
    assert all(call['max_seconds'] <= .75 for call in calls)
    assert {event['section'] for event in events} == {'local_files'}


def test_search_job_obeys_cancellation_between_slices(monkeypatch, tmp_path):
    from jarvis.runtime import search_jobs162 as jobs
    from jarvis.runtime.file_search import FileSearchResults
    from jarvis.runtime.cancellation import cancellation_manager, OperationCancelled
    manager = cancellation_manager()
    manager.reset()
    calls = []
    def scan(*args, **kwargs):
        calls.append(True)
        manager.cancel('test')
        return FileSearchResults(incomplete=True, continuable=True)
    monkeypatch.setattr(jobs, 'search_files', scan)
    try:
        with pytest.raises(OperationCancelled):
            jobs.run_search('report', roots=[tmp_path])
        assert len(calls) == 1
    finally:
        manager.reset()
