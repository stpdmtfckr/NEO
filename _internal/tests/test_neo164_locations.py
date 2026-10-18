"""Requested product locations route locally and expose existing executable paths."""
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from jarvis.runtime.cancellation import cancellation_manager
from jarvis.runtime.feature_commands import parse_feature_command
from jarvis.runtime.dialogue_tasks import prepare_dialogue
from jarvis.runtime.conversation import ConversationState
from jarvis.runtime import game_locations as locations
from jarvis.runtime import location_scan158 as scan

FOLDER_REQUESTS = (
    ('ru', 'в какой папке лежит программа'),
    ('en', 'in which folder is the program'),
    ('de', 'in welchem Ordner befindet sich das Programm'),
    ('fr', 'dans quel dossier se trouve le programme'),
    ('es', 'en qué carpeta está el programa'),
    ('it', 'in quale cartella si trova il programma'),
    ('pt', 'em qual pasta está o programa'),
)
SHORT_REQUESTS = (
    ('ru', 'папка программы'), ('ru', 'путь к программе'),
    ('ru', 'где экзешник программы'), ('ru', 'где программа'),
    ('en', 'where is the program'), ('de', 'wo ist das Programm'),
    ('fr', 'où est le programme'), ('es', 'dónde está el programa'),
    ('it', 'dove è il programma'), ('pt', 'onde está o programa'),
)
EXECUTABLE_REQUESTS = (
    ('ru', 'где находится экзешник программы'),
    ('en', 'find the executable for the program'),
    ('de', 'zeige die ausführbare Datei von dem Programm'),
    ('fr', 'trouve l’exécutable du programme'),
    ('es', 'busca el ejecutable del programa'),
    ('it', 'trova l’eseguibile del programma'),
    ('pt', 'encontra o executável do programa'),
)


@pytest.fixture(autouse=True)
def clear_cancellation():
    cancellation_manager().reset()
    yield
    cancellation_manager().reset()


@pytest.mark.parametrize('code,head', FOLDER_REQUESTS + EXECUTABLE_REQUESTS + SHORT_REQUESTS)
def test_location_question_preserves_product_name(code, head):
    assert parse_feature_command(f'{head} Cheat Engine?', code) == (
        'game_locations', {'action': 'find', 'kind': 'all', 'name': 'Cheat Engine'})


@pytest.mark.parametrize('code,head', FOLDER_REQUESTS + EXECUTABLE_REQUESTS + SHORT_REQUESTS)
def test_missing_product_asks_and_continues(code, head):
    state = ConversationState(language=code)
    parse = lambda text: parse_feature_command(text, code)
    first = prepare_dialogue(head, code, state, parse)
    assert first.reply and first.feature is None
    second = prepare_dialogue('Cheat Engine', code, state, parse)
    assert second.feature == ('game_locations', {'action': 'find', 'kind': 'all', 'name': 'Cheat Engine'})


@pytest.mark.parametrize('code,text', (
    ('ru', 'в какой папке установлен игровой клиент Darktide'),
    ('ru', 'сколько уровней в игре Darktide'),
    ('ru', 'не показывай путь к программе Cheat Engine'),
    ('en', 'where is Paris'),
    ('en', 'find a song about the program Cheat Engine'),
))
def test_unrelated_questions_do_not_enter_location_search(code, text):
    assert (parse_feature_command(text, code) or (None,))[0] != 'game_locations'


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        value = AtlasSession(tmp_path_factory.mktemp('locations164') / 'run')
        yield value
        value.finish()


@pytest.mark.parametrize('code,head', FOLDER_REQUESTS)
def test_production_agent_uses_local_tool_not_web_or_open(session, code, head):
    session.reset_case(code)
    result = session.agent.handle(f'{head} Cheat Engine')
    assert len(session.calls) == 1, result.text
    assert session.calls[0]['tool'] == 'game_locations', result.text
    assert session.calls[0]['arguments'] == {'action': 'find', 'name': 'Cheat Engine', 'kind': 'all'}


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'executable fixture, never executed')
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    import psutil
    monkeypatch.setattr(locations, '_steam_roots', lambda: [])
    monkeypatch.setattr(locations, '_xbox_roots', lambda: [])
    monkeypatch.setattr(locations, '_registry_games', lambda deadline: iter(()))
    monkeypatch.setattr(locations, '_installed_apps', lambda deadline: iter(()))
    monkeypatch.setattr(locations, '_application_roots', lambda: [])
    monkeypatch.setattr(locations, 'known_folders', lambda key: [tmp_path / key])
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs: iter(()))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'Local'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    values = {'gaming.locations_scan_roots': [str(tmp_path)]}
    return SimpleNamespace(paths=SimpleNamespace(screenshots=tmp_path/'Screenshots'), get=values.get)


def test_explicit_cheat_engine_request_returns_installed_executable(tmp_path, monkeypatch, env):
    exe = touch(tmp_path/'Program Files/Cheat Engine 7.5/cheatengine-x86_64.exe')
    touch(exe.parent/'unins000.exe')
    monkeypatch.setattr(locations, '_installed_apps', lambda deadline: iter([
        {'name': 'Cheat Engine 7.5', 'install': '', 'icon_exe': str(exe),
         'source': 'installation_registry', 'evidence': 'Uninstall/Cheat Engine'},
    ]))
    rows = locations.find_game_locations(env, 'Cheat Engine')
    executable = next(row for row in rows if row['kind'] == 'exe')
    assert executable['path'] == str(exe)
    assert executable['evidence'].endswith('/DisplayIcon')
    assert not executable['confirmed']  # Icon association is honestly inferred.
    assert any(row['kind'] == 'install' and row['path'] == str(exe.parent) for row in rows)
    assert not any('unins000' in row['path'] for row in rows)


def test_disk_only_cheat_engine_location_and_architecture_exe(tmp_path):
    exe = touch(tmp_path/'Portable/Cheat Engine 7.5/cheatengine-x86_64.exe')
    other = touch(exe.parent/'unins000.exe')
    scanner = scan.LocationScanner()
    rows, status = scanner.search([tmp_path], {'cheatengine'}, wait=.75)
    assert not status['continuable']
    assert any(row['kind'] == 'exe' and row['path'] == str(exe) for row in rows)
    assert any(row['kind'] == 'related' and row['path'] == str(exe.parent) for row in rows)
    assert not any(row['path'] == str(other) for row in rows)


def test_game_search_still_excludes_unrelated_trainer_and_cheat_engine(tmp_path):
    main = touch(tmp_path/'Games/Darktide/Darktide.exe')
    wrong = touch(tmp_path/'Tools/Cheat Engine for Darktide/DarktideTrainer.exe')
    scanner = scan.LocationScanner()
    rows, _ = scanner.search([tmp_path], {'darktide'}, wait=.75)
    assert any(row['path'] == str(main) for row in rows)
    assert not any('Cheat Engine' in row['path'] or row['path'] == str(wrong) for row in rows)


@pytest.mark.parametrize('raw,expected', (
    ('"C:\\Program Files\\Cheat Engine\\cheatengine-x86_64.exe",0', r'C:\Program Files\Cheat Engine\cheatengine-x86_64.exe'),
    (r'C:\Program Files\Cheat Engine\cheatengine-x86_64.exe,-4', r'C:\Program Files\Cheat Engine\cheatengine-x86_64.exe'),
    ('"C:\\App\\run.exe"', r'C:\App\run.exe'),
    (r'C:\App\icons.dll,0', ''),
    ('"C:\\App\\run.exe" --uninstall', ''),
    ('"C:\\App\\run.exe" & calc.exe', ''),
    ('C:\\App\\run.exe\nC:\\other.exe', ''),
))
def test_display_icon_is_literal_path_only(raw, expected):
    assert locations._registry_icon_executable(raw) == expected


def test_uninstaller_icon_is_not_returned_as_app_executable(tmp_path, monkeypatch, env):
    exe = touch(tmp_path/'App/unins000.exe')
    monkeypatch.setattr(locations, '_installed_apps', lambda deadline: iter([
        {'name': 'Example Program', 'icon_exe': str(exe), 'source': 'installation_registry', 'evidence': 'registry'},
    ]))
    assert not any(row['kind'] == 'exe' for row in locations.find_game_locations(env, 'Example Program'))


def test_missing_icon_target_is_not_reported(tmp_path, monkeypatch, env):
    monkeypatch.setattr(locations, '_installed_apps', lambda deadline: iter([
        {'name': 'Cheat Engine', 'icon_exe': str(tmp_path/'missing.exe'), 'source': 'installation_registry', 'evidence': 'registry'},
    ]))
    assert not locations.find_game_locations(env, 'Cheat Engine')


def test_real_registry_reader_uses_display_icon_without_install_location(monkeypatch):
    import sys
    branch = r'Software\Microsoft\Windows\CurrentVersion\Uninstall'
    fields = {'DisplayName': 'Cheat Engine 7.5', 'DisplayIcon': r'"C:\Program Files\Cheat Engine 7.5\cheatengine-x86_64.exe",0'}
    class Key:
        def __init__(self, path): self.path = path
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def open_key(parent, path, *args):
        joined = parent.path + '/' + path if isinstance(parent, Key) else path
        if joined not in (branch, branch + '/Cheat Engine'):
            raise OSError('absent fixture key')
        return Key(joined)
    def enum_key(root, index):
        if index: raise OSError('end')
        return 'Cheat Engine'
    def query(key, field):
        if field not in fields: raise OSError('absent fixture value')
        return fields[field], 1
    fake = SimpleNamespace(HKEY_CURRENT_USER=1, HKEY_LOCAL_MACHINE=2, KEY_READ=4,
        KEY_WOW64_32KEY=8, KEY_WOW64_64KEY=16, OpenKey=open_key, EnumKey=enum_key, QueryValueEx=query)
    monkeypatch.setitem(sys.modules, 'winreg', fake)
    monkeypatch.setattr(locations, 'os', SimpleNamespace(name='nt'))
    rows = list(locations._installed_apps(time.monotonic()+1))
    assert rows and all(row['install'] == '' for row in rows)
    assert all(row['icon_exe'] == r'C:\Program Files\Cheat Engine 7.5\cheatengine-x86_64.exe' for row in rows)


def test_scan_polling_does_not_restat_unchanged_results_but_final_is_fresh(tmp_path, monkeypatch):
    exe = touch(tmp_path/'Cheat Engine/cheatengine-x86_64.exe')
    scanner = scan.LocationScanner()
    rows, status = scanner.search([tmp_path], {'cheatengine'}, wait=.75)
    assert not status['continuable']
    calls = []
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        calls.append(str(path))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat)
    for _ in range(20):
        rows, _ = scanner.search([tmp_path], {'cheatengine'}, wait=0, validate=False)
    assert calls == []
    exe.unlink()
    final = scan.existing_rows(rows)
    assert len(calls) == len(rows)  # One native stat for each candidate, once.
    assert all(row['path'] != str(exe) for row in final)
    # Default public calls (a new query) retain eager stale-path validation.
    rows, _ = scanner.search([tmp_path], {'cheatengine'}, wait=0)
    assert all(row['path'] != str(exe) for row in rows)


def test_final_publication_removes_file_deleted_during_progress(tmp_path, monkeypatch, env):
    exe = touch(tmp_path/'Cheat Engine/cheatengine-x86_64.exe')
    monkeypatch.setattr(scan, '_scanner', scan.LocationScanner())
    events = []
    def progress(rows):
        events.append(rows)
        if any(row['path'] == str(exe) for row in rows) and exe.exists():
            exe.unlink()
    final = locations.find_game_locations(env, 'Cheat Engine', on_progress=progress)
    assert events and all(row['path'] != str(exe) for row in final)
    assert list(events[-1]) == list(final)


def test_final_validation_honours_cancellation(tmp_path):
    from jarvis.runtime.cancellation import OperationCancelled
    exe = touch(tmp_path/'Example.exe')
    cancellation_manager().cancel('cancel during final publication')
    with pytest.raises(OperationCancelled):
        scan.existing_rows([{'path': str(exe), 'path_type': 'file'}])


def test_german_existing_show_install_folder_keeps_open_action_and_typed_question(session):
    session.reset_case('de')
    first = session.agent.handle('zeige den Installationsordner von')
    assert first.outcome == 'clarification' and not session.calls
    pending = session.agent.conversations.get('desktop').pending_intent
    assert pending.tool == 'game_locations'
    assert pending.arguments == {'action': 'open', 'kind': 'install'}
    session.agent.handle('Darktide')
    assert session.calls[-1]['tool'] == 'game_locations'
    assert session.calls[-1]['arguments'] == {'action': 'open', 'kind': 'install', 'name': 'Darktide'}
    session.reset_case('de')
    session.agent.handle('zeige den Installationsordner von Darktide')
    assert session.calls[-1]['tool'] == 'game_locations'
    # Preserve the established full-command owner too. That older catalogue
    # supplies action/name; kind=install is supplied by its typed clarification.
    assert session.calls[-1]['arguments']['action'] == 'open'
    assert session.calls[-1]['arguments']['name'] == 'Darktide'
    session.reset_case('de')
    session.agent.handle('finde den Installationsordner von Darktide')
    # 186 (CANDIDATE185-GAME-LOCATION-KIND-LOSS): an explicitly named installation folder keeps kind=install.
    assert session.calls[-1]['arguments'] == {'action': 'find', 'kind': 'install', 'name': 'Darktide'}
