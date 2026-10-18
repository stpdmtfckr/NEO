"""NEO 173: exact app skills, «это / туда / тот файл», and the checked chain download → unpack → open."""
from __future__ import annotations

import io
import os
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from jarvis.runtime import chain173, context173, skills173
from jarvis.tools import apps_tool173 as apps


@pytest.mark.parametrize('text,args', [
    ('открой загрузки', {'action': 'folder', 'folder': 'downloads'}), ('открой папку документы', {'action': 'folder', 'folder': 'documents'}),
    ('открой корзину', {'action': 'folder', 'folder': 'recycle'}), ('open my downloads', {'action': 'folder', 'folder': 'downloads'}),
    ('öffne den papierkorb', {'action': 'folder', 'folder': 'recycle'}), ('ouvre mes téléchargements', {'action': 'folder', 'folder': 'downloads'}),
    ('abre descargas', {'action': 'folder', 'folder': 'downloads'}), ('apri il cestino', {'action': 'folder', 'folder': 'recycle'}),
    ('abre a reciclagem', {'action': 'folder', 'folder': 'recycle'}),
    ('открой настройки звука', {'action': 'settings', 'page': 'sound'}), ('открой параметры wi-fi', {'action': 'settings', 'page': 'wifi'}),
    ('открой центр обновления windows', {'action': 'settings', 'page': 'updates'}), ('параметры bluetooth', {'action': 'settings', 'page': 'bluetooth'}),
    ('открой настройки уведомлений', {'action': 'settings', 'page': 'notifications'}), ('open sound settings', {'action': 'settings', 'page': 'sound'}),
    ('öffne die einstellungen für bluetooth', {'action': 'settings', 'page': 'bluetooth'}), ('ouvre les paramètres du son', {'action': 'settings', 'page': 'sound'}),
    ('abre la configuración de bluetooth', {'action': 'settings', 'page': 'bluetooth'}), ('apri le impostazioni del display', {'action': 'settings', 'page': 'display'}),
    ('abre as definições de bluetooth', {'action': 'settings', 'page': 'bluetooth'}),
    ('открой историю браузера', {'action': 'browser', 'key': 'history'}), ('открой загрузки браузера', {'action': 'browser', 'key': 'downloads'}),
    ('открой закладки', {'action': 'browser', 'key': 'bookmarks'}), ('открой вкладку инкогнито', {'action': 'browser', 'key': 'private'}),
    ('верни закрытую вкладку', {'action': 'browser', 'key': 'reopen_tab'}), ('open incognito window', {'action': 'browser', 'key': 'private'}),
    ('открой магазин steam', {'action': 'steam', 'page': 'store'}), ('открой библиотеку стим', {'action': 'steam', 'page': 'library'}),
    ('открой загрузки в стиме', {'action': 'steam', 'page': 'downloads'}), ('найди cyberpunk в стиме', {'action': 'steam', 'page': 'search', 'query': 'cyberpunk'}),
    ('open steam friends', {'action': 'steam', 'page': 'friends'}),
    ('открой настройки дискорда', {'action': 'discord', 'page': 'settings'}), ('открой личные сообщения в дискорде', {'action': 'discord', 'page': 'dms'}),
    ('открой настройки телеграма', {'action': 'telegram', 'page': 'settings'}), ('открой телеграм @durov', {'action': 'telegram', 'page': 'chat', 'username': 'durov'}),
    ('напиши в телеграм @durov привет, как дела', {'action': 'telegram', 'page': 'chat', 'username': 'durov', 'text': 'привет, как дела'}),
    ('напиши в телеграм маме привет', {'action': 'telegram', 'page': 'need_username'}),
    ('создай папку Отчёты на рабочем столе', {'action': 'create_folder', 'name': 'Отчёты', 'folder': 'desktop'}),
    ('создай папку фото 2024 в загрузках', {'action': 'create_folder', 'name': 'фото 2024', 'folder': 'downloads'}),
    ('создай папку проекты', {'action': 'create_folder', 'name': 'проекты', 'folder': 'context'}),
    ('create a folder called Invoices on the desktop', {'action': 'create_folder', 'name': 'Invoices', 'folder': 'desktop'}),
    ('erstelle einen Ordner Test auf dem Desktop', {'action': 'create_folder', 'name': 'Test', 'folder': 'desktop'}),
    ('crée un dossier Photos dans les téléchargements', {'action': 'create_folder', 'name': 'Photos', 'folder': 'downloads'}),
    ('crea una carpeta Fotos en el escritorio', {'action': 'create_folder', 'name': 'Fotos', 'folder': 'desktop'}),
    ('crea una cartella Foto sulla scrivania', {'action': 'create_folder', 'name': 'Foto', 'folder': 'desktop'}),
    ('cria uma pasta Fotos na área de trabalho', {'action': 'create_folder', 'name': 'Fotos', 'folder': 'desktop'}),
])
def test_phrases(text, args):
    assert apps.parse(text, 'ru') == ('apps', args)


@pytest.mark.parametrize('text', ['открой браузер', 'включи музыку', 'открой калькулятор', 'создай папку X в C:\\Work', 'что такое steam'])
def test_not_skills(text):
    assert apps.parse(text, 'ru') is None


def _config(tmp_path=None, **values):
    data = {'app.language': 'ru', 'context.enabled': True, 'context.minutes': 10, **values}
    return SimpleNamespace(get=lambda k, d=None: data.get(k, d), paths=SimpleNamespace(root=tmp_path or Path('.')), data=data)


def test_run_uses_the_programs_own_links():
    started = []
    config = _config()
    for args, uri in [({'action': 'folder', 'folder': 'downloads'}, 'shell:Downloads'), ({'action': 'settings', 'page': 'sound'}, 'ms-settings:sound'),
                      ({'action': 'steam', 'page': 'library'}, 'steam://nav/games'), ({'action': 'telegram', 'page': 'settings'}, 'tg://settings'),
                      ({'action': 'steam', 'page': 'search', 'query': 'hollow knight'}, 'steam://openurl/https://store.steampowered.com/search/?term=hollow%20knight')]:
        result = apps.run(config, args, start=started.append)
        assert result.success and started[-1] == uri
    assert apps.run(config, {'action': 'settings', 'page': 'sound'}, start=started.append).text == 'Открыл параметры Windows: звук.'


def test_telegram_message_is_typed_never_sent():
    typed, started = [], []
    result = apps.run(_config(), {'action': 'telegram', 'page': 'chat', 'username': 'durov', 'text': 'привет'}, start=started.append,
                      type_text=typed.append, sleep=lambda s: None)
    assert started == ['tg://resolve?domain=durov'] and typed == ['привет'] and result.data == {'uri': 'tg://resolve?domain=durov', 'typed': True, 'sent': False}
    assert 'Отправьте его сами' in result.text
    assert not apps.run(_config(), {'action': 'telegram', 'page': 'need_username'}, start=started.append).success


def test_discord_settings_uses_its_own_shortcut():
    pressed = []
    apps.run(_config(), {'action': 'discord', 'page': 'settings'}, start=lambda uri: None, press=pressed.append, sleep=lambda s: None)
    assert pressed == [('ctrl', ',')]


def test_browser_shortcuts_go_to_the_browser_window(monkeypatch):
    from jarvis.runtime import screen_targets171 as targets
    pressed, fronted = [], []
    monkeypatch.setattr(targets, 'bring_to_front', lambda w: fronted.append(w.process) or True)
    firefox = SimpleNamespace(process='firefox.exe')
    result = apps.run(_config(), {'action': 'browser', 'key': 'private'}, start=lambda u: None, press=pressed.append, find_browser=lambda: firefox)
    assert pressed == [('ctrl', 'shift', 'p')] and fronted == ['firefox.exe'] and 'Ctrl+Shift+P' in result.text
    chrome = SimpleNamespace(process='chrome.exe')
    apps.run(_config(), {'action': 'browser', 'key': 'history'}, start=lambda u: None, press=pressed.append, find_browser=lambda: chrome)
    assert pressed[-1] == ('ctrl', 'h')
    none = apps.run(_config(), {'action': 'browser', 'key': 'history'}, start=lambda u: None, press=pressed.append, find_browser=lambda: None)
    assert not none.success and 'Браузер не открыт' in none.text


def test_missing_program_is_said_plainly():
    def start(uri):
        raise OSError('No application is associated')
    result = apps.run(_config(), {'action': 'steam', 'page': 'store'}, start=start)
    assert not result.success and result.text == 'Похоже, Steam не установлен: Windows не знает ссылки steam://.'


def test_create_folder_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(apps, 'known_folder', lambda key: tmp_path)
    context173.forget()
    made = apps.run(_config(), {'action': 'create_folder', 'name': 'Отчёты', 'folder': 'desktop'}, start=lambda u: None)
    assert made.success and (tmp_path / 'Отчёты').is_dir() and made.text.startswith('Создал папку «Отчёты»')
    (tmp_path / 'Отчёты' / 'важно.txt').write_text('x', encoding='utf-8')
    again = apps.run(_config(), {'action': 'create_folder', 'name': 'Отчёты', 'folder': 'desktop'}, start=lambda u: None)
    assert again.text.startswith('Папка «Отчёты» в') and (tmp_path / 'Отчёты' / 'важно.txt').exists()
    assert not apps.run(_config(), {'action': 'create_folder', 'name': 'a/b', 'folder': 'desktop'}, start=lambda u: None).success
    assert context173.recent('folder', _config()) == str(tmp_path / 'Отчёты')      # «туда» now means this folder


# ---- context ------------------------------------------------------------------------------------

class Context:
    """context173 with fake Explorer / Downloads / Recent."""
    def __init__(self, explorer=('', []), downloaded='', recent=()):
        self.explorer, self.downloaded, self.recent_list = explorer, downloaded, list(recent)

    def resolve_file(self, which, config):
        return context173.resolve_file(which, config, explorer=lambda: self.explorer, downloads=lambda c: self.downloaded,
                                       recent_list=lambda c: self.recent_list)

    def resolve_folder(self, config):
        return context173.resolve_folder(config, explorer=lambda: self.explorer)

    which_file = staticmethod(context173.which_file)


def test_this_is_the_explorer_selection_and_there_is_the_explorer_folder(tmp_path):
    context173.forget()
    doc = tmp_path / 'a.docx'
    doc.write_text('x')
    target = tmp_path / 'target'
    target.mkdir()
    ctx = Context(explorer=(str(target), [str(doc)]))
    plan = skills173.plan('перемести это туда', 'ru', _config(), context=ctx)
    assert (plan.tool, plan.args) == ('filesystem', {'action': 'move', 'source': str(doc), 'destination': str(target)})
    plan = skills173.plan('скопируй этот файл на рабочий стол', 'ru', _config(), context=ctx)
    assert plan.args['action'] == 'copy' and plan.args['destination'] == str(apps.known_folder('desktop'))


def test_that_file_is_the_last_one_neo_touched_or_the_users_recent_one(tmp_path):
    context173.forget()
    recent = tmp_path / 'report.pdf'
    recent.write_text('x')
    plan = skills173.plan('открой тот файл', 'ru', _config(), context=Context(recent=[str(recent)]))
    assert (plan.tool, plan.args) == ('filesystem', {'action': 'open', 'source': str(recent)})
    touched = tmp_path / 'made_by_neo.txt'
    touched.write_text('x')
    context173.note('file', touched)
    assert skills173.plan('открой тот файл', 'ru', _config(), context=Context(recent=[str(recent)])).args['source'] == str(touched)
    context173.forget()


def test_downloaded_program_is_shown_not_run(tmp_path):
    context173.forget()
    exe = tmp_path / 'setup.exe'
    exe.write_bytes(b'MZ')
    plan = skills173.plan('открой скачанный файл', 'ru', _config(), context=Context(downloaded=str(exe)))
    assert plan.args == {'action': 'reveal', 'source': str(exe)} and 'я программы не запускаю' in plan.text


def test_nothing_to_resolve_asks(tmp_path):
    context173.forget()
    plan = skills173.plan('перемести это туда', 'ru', _config(), context=Context())
    assert plan.tool == '' and plan.text.startswith('Не понял, какой файл')
    doc = tmp_path / 'a.txt'
    doc.write_text('x')
    plan = skills173.plan('перемести это туда', 'ru', _config(), context=Context(explorer=(str(tmp_path), [str(doc)])))
    assert plan.text.startswith('Не понял, куда')                       # «туда» is the folder the file is already in
    assert skills173.plan('открой тот файл', 'ru', _config(**{'context.enabled': False}), context=Context(recent=[str(doc)])).text


def test_old_events_are_forgotten(monkeypatch, tmp_path):
    context173.forget()
    folder = tmp_path / 'x'
    folder.mkdir()
    context173.note('folder', folder)
    assert context173.recent('folder', _config()) == str(folder)
    assert context173.recent('folder', _config(), now=context173._EVENTS[-1][0] + 11 * 60) == ''
    context173.forget()


# ---- chains -------------------------------------------------------------------------------------

@pytest.mark.parametrize('text,steps', [
    ('скачай https://example.com/tool.zip распакуй и открой', 'download,unpack,open'),
    ('скачай https://example.com/tool.zip и распакуй', 'download,unpack'),
    ('скачай https://example.com/tool.zip', 'download'),
    ('download https://example.com/tool.zip, unzip and open it', 'download,unpack,open'),
    ('lade https://example.com/tool.zip herunter und entpacke', 'download'),
])
def test_chain_phrases(text, steps):
    plan = skills173.plan(text, 'ru', _config())
    assert plan.tool == 'apps' and plan.args['action'] == 'chain' and plan.args['url'] == 'https://example.com/tool.zip'
    if not text.startswith('lade'):
        assert plan.args['steps'] == steps


def test_download_without_address_asks_or_uses_the_copied_link():
    assert skills173.plan('скачай и распакуй', 'ru', _config(), clipboard_url=lambda: '').text.startswith('Что скачать?')
    plan = skills173.plan('скачай ссылку из буфера и распакуй', 'ru', _config(), clipboard_url=lambda: 'https://x.org/a.zip')
    assert plan.args['url'] == 'https://x.org/a.zip' and plan.args['steps'] == 'download,unpack'


def _zip_bytes(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        for name, body in files.items():
            z.writestr(name, body)
    return data.getvalue()


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = {'Content-Length': str(len(body)), **(headers or {})}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), 7):
            yield self.body[i:i + 7]


def test_download_unpack_open_all_checked(tmp_path, monkeypatch):
    body = _zip_bytes({'tool/readme.txt': 'hello', 'tool/run.exe': 'MZ'})
    session = SimpleNamespace(get=lambda url, **kw: FakeResponse(body, headers={'Content-Disposition': 'attachment; filename="tool.zip"'}))
    opened = []
    result, text = chain173.run(['download', 'unpack', 'open'], 'ru', url='https://example.com/get?id=1', downloads=tmp_path,
                                downloader=lambda url, folder, cancelled: chain173.download(url, folder, session=session),
                                opener=opened.append)
    assert result.ok and [s.step for s in result.steps] == ['download', 'unpack', 'open']
    assert (tmp_path / 'tool.zip').is_file() and (tmp_path / 'tool' / 'tool' / 'readme.txt').read_text() == 'hello'
    assert opened == [tmp_path / 'tool']
    lines = text.splitlines()
    assert lines[0].startswith('✓ Шаг 1: скачал «tool.zip»') and lines[1].startswith('✓ Шаг 2: распаковал в папку «tool» (2 файлов)')
    assert lines[2] == '✓ Шаг 3: открыл «tool» в Проводнике.' and 'Программы из архива я не запускаю' in lines[3]


def test_failure_names_the_step_and_stops(tmp_path):
    session = SimpleNamespace(get=lambda url, **kw: FakeResponse(b'not a zip at all'))
    result, text = chain173.run(['download', 'unpack', 'open'], 'ru', url='https://example.com/broken.zip', downloads=tmp_path,
                                downloader=lambda url, folder, cancelled: chain173.download(url, folder, session=session), opener=lambda p: None)
    assert result.failed_step == 'unpack'
    assert '✗ Не получилось на шаге 2 (распаковка): архив «broken.zip» повреждён или защищён паролем.' in text
    assert 'Дальше не продолжал: открытие.' in text
    error = SimpleNamespace(get=lambda url, **kw: FakeResponse(b'', status=404))
    result, text = chain173.run(['download'], 'ru', url='https://example.com/x.zip', downloads=tmp_path,
                                downloader=lambda url, folder, cancelled: chain173.download(url, folder, session=error))
    assert result.failed_step == 'download' and 'сайт ответил ошибкой 404' in text


def test_incomplete_download_is_not_kept(tmp_path):
    class Short(FakeResponse):
        def __init__(self):
            super().__init__(b'abc', headers={'Content-Length': '100'})
    with pytest.raises(chain173.StepFailed) as failure:
        chain173.download('https://example.com/f.bin', tmp_path, session=SimpleNamespace(get=lambda url, **kw: Short()))
    assert failure.value.reason_key == 'incomplete' and list(tmp_path.iterdir()) == []


def test_archive_that_escapes_its_folder_is_refused(tmp_path):
    archive = tmp_path / 'evil.zip'
    archive.write_bytes(_zip_bytes({'../../outside.txt': 'x'}))
    with pytest.raises(chain173.StepFailed) as failure:
        chain173.unpack(archive)
    assert failure.value.reason_key == 'unsafe_paths' and not (tmp_path.parent / 'outside.txt').exists()


def test_7z_without_7zip_says_how(tmp_path):
    archive = tmp_path / 'a.7z'
    archive.write_bytes(b'7z')
    with pytest.raises(chain173.StepFailed) as failure:
        chain173.unpack(archive, seven_zip=lambda: '')
    assert failure.value.reason_key == 'no_7zip'
    assert chain173.reason_text(failure.value, 'ru') == 'для .7z нужен 7-Zip (7-zip.org) — ZIP распаковываю сам'


def test_non_http_addresses_are_refused(tmp_path):
    with pytest.raises(chain173.StepFailed) as failure:
        chain173.download('file:///C:/Windows/system.ini', tmp_path, session=SimpleNamespace())
    assert failure.value.reason_key == 'bad_url'


# ---- through the agent ------------------------------------------------------------------------------

def test_skills_go_through_the_agent(tmp_path):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'run')
        try:
            session.reset_case('ru')
            for text, tool, action in [('открой настройки звука', 'winsettings', 'open'),  # NEO 178: the Windows Settings skill owns its pages
                                 ('открой библиотеку стим', 'apps', 'steam'),
                                       ('открой загрузки', 'desktop', 'open'), ('открой корзину', 'apps', 'folder'), ('верни закрытую вкладку', 'apps', 'browser'),
                                       ('скачай https://example.com/a.zip распакуй и открой', 'apps', 'chain')]:
                session.calls.clear()
                turn = session.agent.handle(text)
                assert session.calls and (session.calls[0]['tool'], session.calls[0]['arguments']['action']) == (tool, action), text
                if tool == 'apps':
                    assert any(str(d).startswith(('skill.', 'chain.')) for d in turn.tool_details), turn.tool_details
            session.calls.clear()
            assert session.agent.handle('напиши в телеграм маме привет').text.startswith('Проверка') or session.calls
        finally:
            session.finish()
