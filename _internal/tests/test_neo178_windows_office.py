"""NEO 178: Windows Settings pages and switches, Word / Excel / PowerPoint files — in 7 languages."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.runtime import office178, winsettings178 as ws


class FakeWindows:
    nt = True

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.registry: dict[tuple[str, str], object] = {}
        self.writes: list[tuple[str, str, object]] = []
        self.commands: list[list[str]] = []
        self.started: list[str] = []
        self.toggled: list[tuple[str, bool]] = []
        self.wallpaper = ''
        self.toggle_result = 'on'

    def open_uri(self, uri):
        self.opened.append(uri)

    def reg_get(self, key, name):
        return self.registry.get((key, name))

    def reg_set(self, key, name, kind, value):
        self.registry[(key, name)] = value
        self.writes.append((key, name, value))

    def broadcast(self, area):
        pass

    def run(self, args, timeout=15.0):
        self.commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, '', '')

    def start(self, exe):
        self.started.append(exe)

    def close_process(self, name):
        return 1

    def process_running(self, name):
        return False

    def set_wallpaper(self, path):
        self.wallpaper = path
        return True

    def clear_clipboard_history(self):
        return True

    def settings_toggle(self, uri, names, enabled):
        self.opened.append(uri)
        self.toggled.append((uri, enabled))
        return self.toggle_result if self.toggle_result != 'on' else ('on' if enabled else 'off')

    def settings_press(self, uri, names):
        self.opened.append(uri)
        return True


# ---- Windows Settings: every page from the screenshots opens by voice, in 7 languages ------------------

PAGES = {
    'ru': {'открой настройки дисплея': 'display', 'открой параметры звука': 'sound', 'открой уведомления': 'notifications',
           'открой фокусировку': 'focus', 'открой настройки питания': 'power', 'открой память устройства': 'storage',
           'открой многозадачность': 'multitasking', 'открой устранение неполадок': 'troubleshoot', 'открой восстановление': 'recovery',
           'открой буфер обмена в параметрах': 'clipboard', 'открой о системе': 'about', 'открой bluetooth и устройства': 'bluetooth',
           'открой сеть и интернет': 'network', 'открой персонализацию': 'personalization', 'открой приложения в параметрах': 'apps',
           'открой учетные записи в параметрах': 'accounts', 'открой время и язык': 'time_language', 'открой специальные возможности': 'accessibility',
           'открой конфиденциальность и защиту': 'privacy', 'открой центр обновления windows': 'updates', 'открой активацию': 'activation'},
    'en': {'open display settings': 'display', 'open sound settings': 'sound', 'open multitasking settings': 'multitasking',
           'open windows update': 'updates', 'open privacy and security': 'privacy', 'open bluetooth and devices': 'bluetooth'},
    'de': {'öffne die anzeigeeinstellungen': 'display', 'öffne soundeinstellungen': 'sound', 'öffne windows update': 'updates'},
    'fr': {"ouvre les paramètres d'affichage": 'display', 'ouvre les paramètres du son': 'sound'},
    'es': {'abre la configuración de pantalla': 'display', 'abre la configuración de sonido': 'sound'},
    'it': {'apri le impostazioni dello schermo': 'display', 'apri le impostazioni audio': 'sound'},
    'pt': {'abre as definições de ecrã': 'display', 'abre as configurações de som': 'sound'},
}


@pytest.mark.parametrize('code,phrase,page', [(c, p, k) for c, rows in PAGES.items() for p, k in rows.items()])
def test_every_settings_page_opens_by_voice(code, phrase, page):
    parsed = ws.parse(phrase, code)
    assert parsed is not None, phrase
    tool, args = parsed
    assert tool == 'winsettings' and args['action'] == 'open' and args['page'] == page, (phrase, args)
    win = FakeWindows()
    result = ws.run(None, args, code, win=win)
    assert result.ok and win.opened == [ws.PAGES[page].uri]


TOGGLES = [
    ('ru', 'включи тёмную тему', 'dark_mode', True), ('ru', 'включи светлую тему', 'dark_mode', False),
    ('ru', 'включи историю буфера обмена', 'clipboard_history', True), ('ru', 'выключи игровой режим', 'game_mode', False),
    ('ru', 'включи ночной свет', 'night_light', True), ('ru', 'включи не беспокоить', 'do_not_disturb', True),
    ('en', 'turn on dark mode', 'dark_mode', True), ('en', 'turn the night light off', 'night_light', False),
    ('de', 'schalte den dunkelmodus ein', 'dark_mode', True), ('fr', 'active le mode sombre', 'dark_mode', True),
    ('es', 'activa el modo oscuro', 'dark_mode', True), ('it', 'attiva la modalità scura', 'dark_mode', True),
    ('pt', 'ativa o modo escuro', 'dark_mode', True),
]


@pytest.mark.parametrize('code,phrase,what,enabled', TOGGLES)
def test_switches_in_seven_languages(code, phrase, what, enabled):
    parsed = ws.parse(phrase, code)
    assert parsed is not None, phrase
    args = parsed[1]
    assert args['action'] == 'toggle' and args['what'] == what and bool(args['enabled']) is enabled, (phrase, args)
    win = FakeWindows()
    result = ws.run(None, args, code, win=win)
    assert result.ok, result.text


def test_dark_mode_writes_only_the_users_own_registry_and_reports_already():
    win = FakeWindows()
    result = ws.run(None, {'action': 'toggle', 'what': 'dark_mode', 'enabled': True}, 'ru', win=win)
    assert result.ok and win.writes and all(key.startswith('HKCU') or key.startswith('Software') for key, _n, _v in win.writes)
    assert all(value == 0 for _k, _n, value in win.writes)
    again = ws.run(None, {'action': 'toggle', 'what': 'dark_mode', 'enabled': True}, 'ru', win=win)
    assert again.ok and 'уже' in again.text.casefold()


def test_power_mode_uses_the_documented_overlay():
    parsed = ws.parse('включи режим максимальной производительности', 'ru')
    assert parsed and parsed[1]['action'] == 'power_mode' and parsed[1]['mode'] == 'best_performance'
    win = FakeWindows()
    assert ws.run(None, parsed[1], 'ru', win=win).ok
    assert win.commands == [['powercfg', '/overlaysetactive', ws.POWER_OVERLAYS['best_performance']]]


def test_dangerous_pages_only_open_and_remote_desktop_is_never_switched_on():
    parsed = ws.parse('включи удаленный рабочий стол', 'ru')
    assert parsed and parsed[1]['action'] == 'open' and parsed[1].get('refused') == 'remote_desktop'
    win = FakeWindows()
    result = ws.run(None, parsed[1], 'ru', win=win)
    assert result.data.get('refused') and not win.writes and not win.toggled
    for page in ('recovery', 'activation', 'sign_in', 'security', 'remote_desktop'):
        assert ws.PAGES[page].protected


def test_privacy_switches_ask_first():
    assert ws.risk({'action': 'toggle', 'what': 'camera_access', 'enabled': False}) == 'medium'
    assert ws.risk({'action': 'clear_clipboard_history'}) == 'medium'
    assert ws.risk({'action': 'open', 'page': 'display'}) == 'safe'


def test_about_and_unrelated_phrases():
    assert ws.parse('о системе', 'ru')[1]['action'] == 'about'
    for phrase in ('включи музыку', 'открой блокнот', 'какая погода', 'open info', 'привет, как дела'):
        assert ws.parse(phrase, 'ru' if phrase[0] > 'z' else 'en') is None, phrase


def test_not_on_windows_says_so():
    win = FakeWindows()
    win.nt = False
    assert not ws.run(None, {'action': 'open', 'page': 'display'}, 'en', win=win).ok


# ---- Office: Word / Excel / PowerPoint --------------------------------------------------------------

OFFICE = [
    ('ru', 'создай таблицу с колонками имя, телефон, город', 'create', 'xlsx'),
    ('ru', 'создай документ про космос', 'create', 'docx'),
    ('ru', 'сделай презентацию про кошек на 5 слайдов', 'create', 'pptx'),
    ('ru', 'запиши в ячейку B2 100', 'set_cell', ''), ('ru', 'что в ячейке A1', 'get_cell', ''),
    ('ru', 'посчитай сумму по столбцу C', 'column_stat', ''), ('ru', 'добавь слайд Итоги: рост, прибыль', 'add_slide', ''),
    ('ru', 'добавь в документ абзац: Привет мир', 'append', ''), ('ru', 'экспортируй таблицу в csv', 'export', 'xlsx'),
    ('ru', 'отмени последнее изменение в документе', 'undo', ''), ('ru', 'открой файл отчёт.docx', 'open', ''),
    ('en', 'create a spreadsheet with columns name, phone', 'create', 'xlsx'), ('en', 'write 100 in cell B2', 'set_cell', ''),
    ('en', 'export the spreadsheet to csv', 'export', 'xlsx'), ('en', 'undo the last document change', 'undo', ''),
    ('de', 'erstelle eine präsentation über katzen mit 5 folien', 'create', 'pptx'), ('de', 'als pdf speichern', 'export', ''),
    ('de', 'mach die letzte dokumentänderung rückgängig', 'undo', ''),
    ('fr', 'crée un tableau avec colonnes nom, téléphone', 'create', 'xlsx'), ('fr', 'écris 100 dans la cellule B2', 'set_cell', ''),
    ('es', 'crea un documento sobre el espacio', 'create', 'docx'), ('es', 'suma de la columna C', 'column_stat', ''),
    ('it', 'crea una presentazione sui gatti con 5 diapositive', 'create', 'pptx'), ('it', 'salva in pdf', 'export', ''),
    ('pt', 'crie uma planilha com colunas nome, telefone', 'create', 'xlsx'), ('pt', 'desfaça a última alteração do documento', 'undo', ''),
]


@pytest.mark.parametrize('code,phrase,action,kind', OFFICE)
def test_office_phrases_in_seven_languages(code, phrase, action, kind):
    parsed = office178.parse(phrase, code)
    assert parsed is not None, phrase
    assert parsed[1]['action'] == action, (phrase, parsed)
    if kind:
        assert parsed[1].get('kind') == kind


def test_office_does_not_take_other_commands():
    for phrase in ('поменяй язык на английский', 'замени название заметок на 1.45', 'напиши письмо в A4', 'добавь строку в заметку купить хлеб',
                   'включи музыку', 'сохрани', 'открой блокнот'):
        parsed = office178.parse(phrase, 'ru')
        assert parsed is None or not office178.claims(parsed[1], live_finder=lambda *a: None), (phrase, parsed)
    assert office178.parse('открой файл отчёт.docx', 'ru')[1]['name'] == 'отчёт.docx'


@pytest.fixture()
def office(tmp_path, monkeypatch):
    monkeypatch.setitem(office178._STATE, 'last', {})
    monkeypatch.setitem(office178._STATE, 'created', set())
    config = SimpleNamespace(paths=SimpleNamespace(cache=str(tmp_path / 'cache'), root=str(tmp_path)), get=lambda key, default=None: default)
    opened: list[str] = []

    def run(args, code='ru'):
        return office178.run(config, args, code, live_finder=lambda *a: None, selection=lambda: '', open_file=opened.append, folder=tmp_path)
    return SimpleNamespace(run=run, folder=tmp_path, opened=opened, config=config)


def test_spreadsheet_create_fill_count_and_export(office):
    pytest.importorskip('openpyxl')
    created = office.run({'action': 'create', 'kind': 'xlsx', 'values': 'имя, телефон, сумма', 'title': 'Клиенты'})
    assert created.ok and Path(created.data['path']).exists() and office.opened == [created.data['path']]
    assert office.run({'action': 'add_row', 'values': 'Иван; 8900; 100'}).ok
    assert office.run({'action': 'add_row', 'values': 'Пётр, 8911, 250.5'}).ok
    assert '8900' in office.run({'action': 'get_cell', 'cell': 'B2'}).text
    total = office.run({'action': 'column_stat', 'stat': 'sum', 'column': 'C'})
    assert total.ok and '350.5' in total.text
    assert not office.run({'action': 'set_cell', 'cell': 'ZZZZ1', 'value': '1'}).ok
    exported = office.run({'action': 'export', 'format': 'csv', 'kind': 'xlsx'})
    assert exported.ok
    csv = Path(office.folder / 'Клиенты.csv').read_text(encoding='utf-8-sig')
    assert csv.splitlines()[1].startswith('Иван;8900;100')


def test_document_edit_replace_and_undo(office):
    pytest.importorskip('docx')
    assert office.run({'action': 'create', 'kind': 'docx', 'title': 'План', 'text': 'Кошка спит. Кошка ест.'}).ok
    assert office.run({'action': 'heading', 'text': 'Введение'}).ok
    assert office.run({'action': 'append', 'text': 'Новый абзац'}).ok
    replaced = office.run({'action': 'replace', 'find': 'Кошка', 'text': 'Собака'})
    assert replaced.ok and ': 2' in replaced.text
    assert 'Собака спит' in office.run({'action': 'read', 'kind': 'docx'}).text
    assert not office.run({'action': 'replace', 'find': 'Слон', 'text': 'Мышь'}).ok
    assert office.run({'action': 'undo'}).ok                       # undoes the replace, not the failed one
    text = office.run({'action': 'read', 'kind': 'docx'}).text
    assert 'Кошка спит' in text and 'Новый абзац' in text


def test_presentation_slides_in_the_users_language(office):
    pytest.importorskip('pptx')
    assert office.run({'action': 'create', 'kind': 'pptx', 'title': 'Bericht', 'count': 3}, 'de').ok
    assert office.run({'action': 'add_slide', 'title': 'Fazit', 'items': 'Wachstum, Gewinn'}, 'de').ok
    text = office.run({'action': 'read', 'kind': 'pptx'}, 'de').text
    assert 'Folie 2' in text and 'Fazit' in text and 'Wachstum' in text


def test_files_neo_did_not_create_ask_before_changing(office, tmp_path):
    docx = pytest.importorskip('docx')
    path = tmp_path / 'Чужой.docx'
    docx.Document().save(str(path))
    office178.remember(path, 'docx')
    assert office178.risk({'action': 'append', 'text': 'x'}, office.config) in {'medium', 'safe'}
    assert office178.risk({'action': 'read'}, office.config) == 'safe'
    assert office178.risk({'action': 'undo'}, office.config) == 'medium'


def test_office_log_never_contains_document_text(office, caplog):
    pytest.importorskip('docx')
    caplog.set_level('DEBUG')
    office.run({'action': 'create', 'kind': 'docx', 'title': 'Секрет', 'text': 'пароль от сейфа 1234'})
    office.run({'action': 'append', 'text': 'ещё секретный текст'})
    assert 'сейфа' not in caplog.text and 'секретный' not in caplog.text


def test_missing_library_is_explained():
    assert 'python-docx' in office178.t('missing_lib', 'ru', lib='python-docx')


def test_requirements_list_the_office_libraries():
    text = (Path(__file__).resolve().parents[1] / 'requirements.txt').read_text(encoding='utf-8')
    for name in ('openpyxl', 'python-docx', 'python-pptx'):
        assert name in text


# ---- through the agent ----------------------------------------------------------------------------------

ROUTES = [
    ('ru', 'открой многозадачность', 'winsettings.open'), ('ru', 'включи ночной свет', 'winsettings.toggle'),
    ('ru', 'включи тёмную тему', 'winsettings.toggle'), ('ru', 'о системе', 'winsettings.about'),
    ('ru', 'включи удаленный рабочий стол', 'winsettings.open'), ('ru', 'выключай экран через 10 минут', 'winsettings.screen_timeout'),
    ('ru', 'выключи микрофон', 'audio'), ('ru', 'запусти сценарий утро', 'scenario'),
    ('ru', 'создай таблицу с колонками имя, телефон', 'office.create'), ('ru', 'сделай презентацию про космос на 5 слайдов', 'office.create'),
    ('en', 'open display settings', 'winsettings.open'), ('en', 'create a document about space', 'office.create'),
    ('de', 'öffne soundeinstellungen', 'winsettings.open'), ('fr', 'active le mode sombre', 'winsettings.toggle'),
    ('es', 'crea una hoja de cálculo con columnas nombre, teléfono', 'office.create'), ('it', 'apri le impostazioni audio', 'winsettings.open'),
    ('pt', 'crie um documento sobre o espaço', 'office.create'),
]


def test_agent_routes_settings_and_office_phrases(tmp_path):
    """The whole agent (test mode blocks the real action) picks the Settings / Office skill for these phrases."""
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        sessions = {}
        for code, phrase, detail in ROUTES:
            if code not in sessions:
                sessions[code] = AtlasSession(tmp_path / f'run_{code}', locale=code)
                sessions[code].agent._llm_agent170 = lambda *a, **k: None
            session = sessions[code]
            session.reset_case(code)
            if detail == 'scenario':
                # Risk and approval must refer to an actually saved definition.
                session.config.data['scenarios'] = {'items': [{
                    'id': 'qa-morning', 'name': 'утро', 'enabled': True,
                    'steps': [{'type': 'wait', 'seconds': 0}],
                }]}
            turn = session.agent.handle(phrase)
            calls = [f"{c['tool']}.{c['arguments'].get('action')}" for c in session.calls]
            assert detail in (turn.tool_details or []) or any(c == detail or c.startswith(detail + '.') for c in calls), (phrase, turn.text, turn.tool_details, calls)
        for session in sessions.values():
            session.finish()


def test_program_names_lose_their_article(tmp_path):
    """«öffne den Rechner» opens «rechner», not a program called «den rechner»."""
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        for code, phrase, name in (('de', 'öffne den Rechner', 'rechner'), ('fr', 'ouvre la calculatrice', 'calculatrice'),
                                   ('en', 'open the calculator', 'calculator')):
            session = AtlasSession(tmp_path / f'a_{code}', locale=code)
            session.agent._llm_agent170 = lambda *a, **k: None
            session.reset_case(code)
            session.agent.handle(phrase)
            names = [call['arguments'].get('name') for call in session.calls if call['tool'] == 'application']
            assert names and names[0].casefold() == name, (phrase, names)
            session.finish()
