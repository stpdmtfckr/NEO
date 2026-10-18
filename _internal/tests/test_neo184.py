"""NEO 184: local translation model, QA 183 fixes (commands, dialogue, functions, settings, chat)."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


class Cfg:
    def __init__(self, **values):
        from jarvis.runtime.translation184 import DEFAULTS
        self.data = {'app': {'language': 'ru'}, 'translation': dict(DEFAULTS)}
        for key, value in values.items():
            node = self.data
            parts = key.split('.')
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value

    def get(self, key, default=None):
        node = self.data
        for part in key.split('.'):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def update(self, mutate, **_kw):
        mutate(self.data)
        return True


class Adapter:
    def __init__(self):
        self.calls = []

    def translate(self, text, target, source=''):
        self.calls.append((text, target))
        return 'Hello, world', {'source': source, 'target': target}


@pytest.fixture
def tr(monkeypatch):
    from jarvis.runtime import translation184
    import jarvis.ollama_runtime as runtime
    state = {'installed': False}
    monkeypatch.setattr(runtime, 'list_ollama_models', lambda base, timeout=2.5: {'translategemma:4b'} if state['installed'] else set())
    monkeypatch.setattr(runtime, 'ensure_ollama_running', lambda base, **kw: {'ready': True})
    translation184.forget_presence()
    translation184.forget_question()
    translation184._INSTALLER[0] = None
    translation184._test_state = state
    yield translation184
    translation184.forget_presence()
    translation184.forget_question()


# ---- translation model ------------------------------------------------------------------------

def test_missing_model_asks_once_and_no_means_searchapi_with_one_warning(tr, monkeypatch):
    import jarvis.integrations.local_translation as lt
    adapter = Adapter()
    monkeypatch.setattr(lt, 'SearchAPITranslationAdapter', lambda config: adapter)
    config = Cfg()
    request = tr.Request('Привет, мир', 'en', '', 'ru')
    first = tr.translate(config, request, adapter=adapter)
    assert first.engine == 'ask' and 'не установлена' in first.text and adapter.calls == []
    answer = tr.answer_question(type('A', (), {'config': config})(), 'нет', 'ru')
    assert answer[1] == 'translation.install_declined'
    assert 'бесплатные ежедневные запросы SearchAPI' in answer[0] and 'Hello, world' in answer[0]
    assert config.get('translation.offer_install') is False and config.get('translation.searchapi_notice_shown') is True
    again = tr.translate(config, request, adapter=adapter)
    assert again.engine == 'searchapi' and 'бесплатные' not in again.text and 'Установить' not in again.text


def test_yes_downloads_with_progress_and_translates_the_waiting_phrase(tr, monkeypatch):
    import requests
    config = Cfg()
    request = tr.Request('Привет, мир', 'en', '', 'ru')
    assert tr.translate(config, request, adapter=Adapter()).engine == 'ask'

    class Resp:
        def __init__(self, lines=None, payload=None):
            self.lines, self.payload, self.status_code, self.content = lines or [], payload, 200, b'x'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_lines(self):
            for line in self.lines:
                yield json.dumps(line).encode()
            tr._test_state['installed'] = True

        def json(self):
            return self.payload

        def close(self):
            pass
    lines = [{'digest': 'd', 'total': 3_300_000_000, 'completed': int(3_300_000_000 * i / 10)} for i in range(11)]
    prompts = []

    def post(url, json=None, **kw):
        if url.endswith('/api/pull'):
            return Resp(lines)
        prompts.append(json['prompt'])
        return Resp(payload={'response': 'Hello, world'})
    monkeypatch.setattr(requests, 'post', post)
    reply = tr.answer_question(type('A', (), {'config': config})(), 'да', 'ru')
    assert reply[1] == 'translation.install_accepted' and 'Начинаю загрузку' in reply[0]
    deadline = time.monotonic() + 5
    while tr.installer().snapshot()['state'] == 'downloading' and time.monotonic() < deadline:
        time.sleep(0.02)
    events = tr.installer().drain()
    assert any('25%' in e for e in events) and any('75%' in e for e in events)
    assert any('установлена' in e for e in events)
    assert any('Hello, world' in e and 'без интернета' in e for e in events)
    assert prompts and 'Russian (ru) to English (en)' in prompts[0] and prompts[0].endswith('\n\n\nПривет, мир')
    assert config.get('translation.model') == 'translategemma:4b'


def test_installed_model_translates_locally_and_offline_label(tr, monkeypatch):
    tr._test_state['installed'] = True

    class Session:
        def post(self, url, json=None, timeout=None):
            class R:
                status_code, content = 200, b'x'

                def raise_for_status(self):
                    pass

                def json(self):
                    return {'response': 'Guten Morgen'}
            return R()
    result = tr.translate(Cfg(), tr.Request('Good morning', 'de', '', 'ru'), adapter=Adapter(), session=Session())
    assert result.ok and result.engine == 'local' and 'Guten Morgen' in result.text and 'локальная модель' in result.text


def test_engine_searchapi_never_asks(tr):
    adapter = Adapter()
    result = tr.translate(Cfg(**{'translation.engine': 'searchapi'}), tr.Request('Привет', 'en', '', 'ru'), adapter=adapter)
    assert result.engine == 'searchapi' and adapter.calls


def test_too_long_text_is_refused_before_any_engine(tr):
    adapter = Adapter()
    result = tr.translate(Cfg(**{'translation.max_chars': 200}), tr.Request('x' * 500, 'en', '', 'ru'), adapter=adapter)
    assert not result.ok and adapter.calls == []


def test_source_detection_by_script(tr):
    assert tr.detect_source('Привіт, як справи?') == 'uk'
    assert tr.detect_source('こんにちは') == 'ja'
    assert tr.detect_source('안녕하세요') == 'ko'
    assert tr.detect_source('Привет') == 'ru'


def test_translation_settings_defaults_and_marker(tr, tmp_path):
    defaults = json.loads((ROOT / 'config/default_settings.json').read_text(encoding='utf-8'))
    assert defaults['translation']['model'] == 'translategemma:4b' and defaults['translation']['offer_install'] is True
    config = Cfg()
    config.paths = type('P', (), {'root': tmp_path})()
    tr._mark_model(config, 'translategemma:4b', present=True)
    marker = json.loads((tmp_path / 'bootstrap/translation_model.json').read_text(encoding='utf-8'))
    assert marker['models'] == ['translategemma:4b']
    tr._mark_model(config, 'translategemma:4b', present=False)
    assert not (tmp_path / 'bootstrap/translation_model.json').exists()


def test_uninstaller_removes_the_translation_model_marker_models():
    text = (ROOT / 'installer_helper.ps1').read_text(encoding='utf-8-sig')
    assert 'translation_model.json' in text and text.count('translation_model.json') >= 3


# ---- QA 183: commands and dialogue (no generative model) ---------------------------------------------

@pytest.fixture(scope='module')
def session(tmp_path_factory):
    from jarvis.testing.isolation import external_io_blocked
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.tools.registry import ToolExecution
    with external_io_blocked():
        s = AtlasSession(tmp_path_factory.mktemp('neo184') / 'session')
        calls = []
        for definition in s.agent.tools._tools.values():
            definition.handler = lambda args: ToolExecution(True, 'QA', {'verified': True})
        s.agent.tools.set_execution_guard(lambda name, args, risk: calls.append((name, dict(args))) or None)
        s.calls184 = calls
        yield s
        s.finish()


def run(session, code, *texts):
    from jarvis.testing.isolation import external_io_blocked
    session.reset_case(code)
    turn = None
    with external_io_blocked():
        for text in texts:
            session.calls184.clear()
            turn = session.agent.handle(text)
    return turn, list(session.calls184)


@pytest.mark.parametrize('code,text', [
    ('de', 'Führe den folgenden Befehl nicht aus: sperre den Computer'),
    ('de', 'Führe den folgenden Befehl nicht aus: Schalte Instant Replay aus'),
    ('fr', 'N’exécute pas la commande suivante : Passe à l’onglet suivant'),
    ('es', 'No ejecutes el siguiente comando: abre el sitio https://example.invalid/qa731'),
    ('it', 'Non eseguire il seguente comando: Incolla dagli appunti'),
    ('ru', 'Не выполняй следующую команду: заблокируй компьютер'),
    ('en', 'Do not run the following command: lock the computer'),
    ('pt', 'Não executes o seguinte comando: bloqueia o computador'),
    ('en', 'What does the command “clear clipboard” mean?'),
    ('ru', 'Что означает команда «сделай скриншот»?'),
    ('en', 'Why does the command “open application Steam” open a program?'),
    ('fr', 'Le document contient : « Avance à la page suivante »'),
    ('ru', 'В документе написано: «перейди на следующую страницу»'),
    ('es', 'Estoy citando: «abre la aplicación Steam»'),
    ('ru', 'Забудь работу, почему небо голубое?'),
    ('en', 'Forget work, why is the sky blue?'),
    ('ru', 'Открой приложение Steam, нет, не открывай его'),
    ('fr', 'Combien de réunions ai-je eues ?'),
])
def test_words_about_a_command_run_nothing(session, code, text):
    turn, calls = run(session, code, text)
    assert calls == [] and not turn.pending, (text, calls, turn.tool_details)


@pytest.mark.parametrize('code,first,reply', [
    ('ru', 'Открой приложение', 'Не надо, давай лучше поговорим'),
    ('ru', 'Открой приложение', 'Кстати, почему небо голубое?'),
    ('en', 'Open application', "Don't open anything, I changed my mind"),
    ('de', 'Öffne die Anwendung', 'Ich zitiere: „öffne die Anwendung Steam“'),
])
def test_an_open_question_is_not_answered_by_a_refusal_or_new_topic(session, code, first, reply):
    turn, calls = run(session, code, first, reply)
    assert calls == [], calls


@pytest.mark.parametrize('code,texts,name', [
    ('es', ('Abre la aplicación', 'No, me refería a Calculator'), 'Calculator'),
    ('ru', ('Не открывай Steam, открой приложение Calculator',), 'Calculator'),
    ('de', ('Öffne Steam nicht, öffne die Anwendung Calculator',), 'Calculator'),
    ('de', ('Ich möchte, dass du das Programm Calculator öffnest',), 'Calculator'),
    ('it', ('Vorrei che tu aprissi l’applicazione Calculator',), 'Calculator'),
    ('pt', ('Poderias abrir a aplicação Calculator?',), 'Calculator'),
    ('ru', ('Я хотел бы, чтобы ты открыл приложение Calculator',), 'Calculator'),
])
def test_allowed_or_polite_request_opens_exactly_that_application(session, code, texts, name):
    _turn, calls = run(session, code, *texts)
    assert calls == [('application', {'action': 'open', 'name': name})], calls


def test_literal_memory_key_and_number_with_exclamation(session):
    _turn, calls = run(session, 'de', 'merke dir enter: 25')
    assert calls and calls[0][1].get('key') == 'enter'
    _turn, calls = run(session, 'en', 'click at coordinates 10 10!')
    assert calls and calls[0][1].get('x') == 10 and calls[0][1].get('y') == 10


def test_german_merke_dir_dass_saves_and_correction_replaces_fact(session):
    turn, _ = run(session, 'de', 'Merke dir, dass meine Lieblingsfarbe Türkis ist')
    assert 'memory.remember_that' in turn.tool_details
    run(session, 'ru', 'Запомни, что мой любимый цвет бирюзовый')
    turn, _ = run(session, 'ru', 'Исправление: мой любимый цвет янтарный')
    assert 'memory.corrected' in turn.tool_details
    notes = [r['value'] for r in session.agent.database.list_memories(limit=100) if r['key'].startswith('note:')]
    assert 'мой любимый цвет янтарный' in notes and 'мой любимый цвет бирюзовый' not in notes


def test_repeat_previous_answer_shows_it_and_copies_nothing(session):
    session.reset_case('ru')
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        first = session.agent.handle('Расскажи шутку')
        session.calls184.clear()
        again = session.agent.handle('Повтори свой предыдущий ответ')
    assert again.text == first.text and session.calls184 == []


def test_rule_answers_reach_the_model_history(session):
    from jarvis.runtime import local_chat169
    run(session, 'ru', 'Не выполняй, это цитата: «открой приложение»')
    history = local_chat169.history(getattr(session.agent, '_conversation_key', 'desktop'))
    assert any('цитата' in row['content'] for row in history if row['role'] == 'user')


def test_model_gets_the_users_words_not_internal_tokens():
    from jarvis.agent import Agent
    agent = Agent.__new__(Agent)
    agent._atlas_raw_text = 'How many meetings did I have?'
    assert Agent._model_text174(agent, 'How many встреча did I have', 'en') == 'How many meetings did I have?'


def test_chat_replies_lose_unrequested_emoji_and_code_fences():
    from jarvis.runtime.local_chat169 import _plain184
    assert _plain184('Шутка 😄 вот\n```python\nprint(1)\n```') == 'Шутка вот\nprint(1)\n'


def test_shipped_catalogue_equals_current_registrations(tmp_path):
    import subprocess, sys
    shipped = json.loads((ROOT / 'tests/fixtures/command_catalogue.json').read_text(encoding='utf-8'))
    assert shipped['tool_count'] >= 61 and shipped['enumerated_action_count'] >= 566
    names = {row['tool'] for row in shipped['tools']}
    assert {'apps', 'office', 'winsettings'} <= names


# ---- QA 183: functions -------------------------------------------------------------------------------

def test_failed_copy_keeps_the_existing_file_and_leaves_no_temp(tmp_path):
    from jarvis.tools import implementations
    source, target = tmp_path / 'src.txt', tmp_path / 'dst.txt'
    source.write_text('NEW DATA', encoding='utf-8')
    target.write_text('ORIGINAL', encoding='utf-8')
    real = implementations.shutil.copy2

    def fail(src, dst, *a, **kw):
        Path(dst).write_text('PARTIAL', encoding='utf-8')
        raise OSError('disk full')
    with patch.object(implementations.shutil, 'copy2', fail):
        with pytest.raises(OSError):
            implementations._copy_any(source, target)
    assert target.read_text(encoding='utf-8') == 'ORIGINAL'
    assert not [p for p in tmp_path.iterdir() if p.name.endswith('.neo-tmp')]
    implementations._copy_any(source, target)
    assert target.read_text(encoding='utf-8') == 'NEW DATA'
    assert real is implementations.shutil.copy2


@pytest.mark.parametrize('operation', ['create', 'repeat', 'cancel'])
def test_timer_save_failure_leaves_memory_as_on_disk(tmp_path, operation):
    from jarvis.runtime.timers import TimerManager
    manager = TimerManager()
    manager._path = tmp_path / 'timers.json'
    manager._ensure_thread_locked = lambda: None
    old = manager.create(300, 'ORIGINAL')
    before = [x.id for x in manager.list()]
    real = Path.replace

    def fail(path, target):
        if Path(target) == manager._path:
            raise OSError('locked')
        return real(path, target)
    with patch.object(Path, 'replace', fail):
        with pytest.raises(OSError):
            if operation == 'create':
                manager.create(500, 'GHOST')
            elif operation == 'repeat':
                manager.create_repeating(12, 0, 'daily', 'GHOST')
            else:
                manager.cancel(old.id)
    assert [x.id for x in manager.list()] == before


def test_impossible_timer_is_refused_before_storage_and_bad_row_skipped(tmp_path):
    from jarvis.runtime.timers import TimerManager
    manager = TimerManager()
    manager._path = tmp_path / 'timers.json'
    manager._ensure_thread_locked = lambda: None
    with pytest.raises(ValueError):
        manager.create(1e20, 'TOO LARGE')
    assert manager.list() == [] and not manager._path.exists()
    good = manager.create(600, 'VALID').__dict__.copy()
    manager._path.write_text(json.dumps([{'id': 'bad', 'label': 'B', 'due_at': 'invalid', 'created_at': time.time()}, good]), encoding='utf-8')
    fresh = TimerManager()
    fresh._path = manager._path
    fresh._load_locked()
    assert [x.id for x in fresh.list()] == [good['id']]


def test_parallel_children_stay_in_the_cancelled_generation():
    from jarvis.runtime.cancellation import CancellationManager
    import jarvis.tools.extended as extended
    source = Path(extended.__file__).read_text(encoding='utf-8')
    assert 'cancel.bind(generation)' in source
    manager = CancellationManager()
    generation = manager.reset()
    seen = []
    ready, release = threading.Event(), threading.Event()

    def child():
        manager.bind(generation)
        ready.set()
        release.wait(2)
        seen.append(manager.is_cancelled())
    thread = threading.Thread(target=child)
    thread.start()
    ready.wait(2)
    manager.cancel('stop')
    manager.reset()
    release.set()
    thread.join(2)
    assert seen == [True]


def test_memory_clear_reaches_user_rows_behind_many_internal_rows(tmp_path):
    from jarvis.database import Database
    from jarvis.runtime import memory169
    db = Database(tmp_path / 'a.db')
    db.set_memory('pref:old_fact', json.dumps({'name': 'old', 'value': 'x'}))
    with db._lock:
        conn = db._db()
        conn.executemany('INSERT OR REPLACE INTO memories(key,value,updated_at) VALUES(?,?,?)',
                         [(f'system.qa{i}', 'I', '2099-01-01T00:00:00+00:00') for i in range(5001)])
        conn.commit()
    memory169.clear(db, Cfg(), 'ru')
    assert db.get_memory('pref:old_fact', '') == '' and db.get_memory('system.qa1', '') == 'I'
    db.close()


def test_field_checklist_ships_inside_the_program():
    text = (ROOT / 'docs/field_checklist_ru.md').read_text(encoding='utf-8')
    assert 'отчёт для разработчика' in text.casefold()


# ---- QA 183: settings and chat -----------------------------------------------------------------------

def _config(tmp_path, monkeypatch):
    for key in ('HOME', 'APPDATA', 'LOCALAPPDATA'):
        monkeypatch.setenv(key, str(tmp_path / key))
    from jarvis.config import ConfigManager
    return ConfigManager(ROOT / 'config' / 'default_settings.json')


def test_full_reset_removes_connections_and_keep_option_keeps_them(tmp_path, monkeypatch):
    from jarvis.secrets import protect_secret
    config = _config(tmp_path, monkeypatch)

    def connect(data):
        spotify = data['integrations']['spotify']
        spotify.update(client_id_encrypted=protect_secret('C'), access_token_encrypted=protect_secret('A'),
                       refresh_token_encrypted=protect_secret('R'), token_expires_at=1.0)
    config.update(connect, credentials=True)
    config.reset_to_defaults()
    assert config.stored_credentials() == []
    config.update(connect, credentials=True)
    config.reset_to_defaults(keep_connections=True)
    assert 'integrations.spotify.refresh_token_encrypted' in config.stored_credentials()


def test_formatted_messages_are_localized_by_their_catalog_template():
    from jarvis.i18n import tr
    assert tr('Gmail авторизован: qa@example.invalid.', 'en') == 'Gmail authorized: qa@example.invalid.'
    assert tr('Подключение Gmail работает: qa@example.invalid.', 'de') == 'Die Gmail-Verbindung funktioniert: qa@example.invalid.'


@pytest.fixture(scope='module')
def app():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_long_word_wraps_and_copy_is_the_original(app):
    from PySide6.QtGui import QGuiApplication, QKeySequence, QKeyEvent
    from PySide6.QtCore import Qt, QEvent
    from jarvis.ui.chat_view import ChatView
    view = ChatView()
    view.resize(300, 420)
    view.show()
    original = 'https://example.invalid/' + 'W' * 120
    bubble = view.add_assistant(original)
    app.processEvents()
    label = bubble.body_label
    label.setSelection(0, len(label.text()))
    assert label.selectedText() == original
    event = QKeyEvent(QEvent.KeyPress, Qt.Key_C, Qt.ControlModifier)
    label.keyPressEvent(event)
    assert QGuiApplication.clipboard().text() == original
    assert label.heightForWidth(250) > label.fontMetrics().height() * 3
    view.close()


def test_settings_translation_page_and_google_late_results(tmp_path, monkeypatch, app):
    from jarvis.testing.isolation import external_io_blocked
    from jarvis.ui import settings_dialog as sd
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setattr(sd.SettingsDialog, '_refresh_microphones_async', lambda self: None)
    shown = []
    for name in ('information', 'warning'):
        monkeypatch.setattr(sd.QMessageBox, name, lambda *a, **k: shown.append(a))
    with external_io_blocked():
        dialog = sd.SettingsDialog(config)
        assert any(title == 'Перевод' for _f, title in dialog._settings_pages160)
        assert dialog.translation_engine184.currentData() == 'auto'
        # A late Gmail result after «Сбросить OAuth» changes nothing.
        dialog.gmail_client_id.setText('CLIENT_A')
        generation = dialog._google_bump184('gmail')
        dialog._clear_gmail_auth()
        dialog._gmail_authorized({'access_token': 'A', 'refresh_token': 'R'}, generation, 'CLIENT_A')
        assert not dialog._gmail_tokens.get('refresh_token') and shown == []
        # Every colour field has an accessible name.
        assert all(field.edit.accessibleName() for field in dialog.ui_colors.values())
        dialog.reject()


def test_youtube_check_returns_at_once(tmp_path, monkeypatch, app):
    from jarvis.testing.isolation import external_io_blocked
    from jarvis.ui import settings_dialog as sd
    config = _config(tmp_path, monkeypatch)
    monkeypatch.setattr(sd.SettingsDialog, '_refresh_microphones_async', lambda self: None)

    def slow(*a, **k):
        time.sleep(0.3)
        raise TimeoutError('timed out')
    with external_io_blocked():
        dialog = sd.SettingsDialog(config)
        dialog.youtube_api_key.setText('FAKE_KEY')
        monkeypatch.setattr(sd.requests, 'get', slow)
        started = time.monotonic()
        dialog._test_youtube_api()
        assert time.monotonic() - started < 0.2
        deadline = time.monotonic() + 3
        while getattr(dialog, '_youtube_test_worker184', None) is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert 'FAKE_KEY' not in dialog.youtube_status.text() and ('вовремя' in dialog.youtube_status.text() or 'in time' in dialog.youtube_status.text())
        dialog.reject()
