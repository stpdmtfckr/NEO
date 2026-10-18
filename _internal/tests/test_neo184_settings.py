"""NEO184 settings relocation and UI regression tests; all profiles are isolated."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from jarvis.config import ConfigManager
from jarvis.settings_storage184 import prepare_settings, canonical_settings_path, read_settings_path
from jarvis.testing.isolation import external_io_blocked


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ('HOME', 'LOCALAPPDATA', 'APPDATA', 'XDG_CACHE_HOME'):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setattr(ConfigManager, 'apply_wake_service', lambda *_: None)
    monkeypatch.setattr(ConfigManager, 'apply_autostart', lambda *_: None)
    with external_io_blocked(), patch.object(socket.socket, 'sendto', side_effect=PermissionError('QA blocks datagrams')), patch.object(os, 'system', side_effect=PermissionError('QA blocks shell')):
        yield


def config():
    return ConfigManager(ROOT / 'config/default_settings.json')


def test_fresh_settings_folder_and_chat_default():
    c = config()
    assert c.paths.settings == c.paths.root / 'настройки/settings.json'
    assert c.get('storage.settings_dir') == str(c.paths.settings.parent)
    assert c.get('ollama.chat_model') == 'qwen2.5:0.5b'
    assert c.get('ollama.vision_model') == 'qwen3.5:4b'
    assert not (c.paths.root / 'settings.json').exists()


def test_legacy_migration_is_lossless_and_idempotent():
    root = Path(os.environ['LOCALAPPDATA']) / 'NeoJarvis'
    root.mkdir(parents=True)
    legacy = root / 'settings.json'
    payload = {'settings_version': 179, 'assistant_name': 'My NEO', 'ollama': {'chat_model': 'user/model:7b'},
               'custom_untouched': {'future': ['x', 3]}, 'dialogue': {'style': 'military'},
               'integrations': {'telegram': {'remote_control_enabled': False, 'allowed_user_ids': [456]}}}
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    legacy.write_bytes(encoded)
    c = config()
    assert c.paths.settings.read_bytes() == encoded
    assert legacy.read_bytes() == encoded
    assert c.get('ollama.chat_model') == 'user/model:7b'
    assert c.get('custom_untouched') == payload['custom_untouched']
    assert c.paths.settings.with_name('settings.migration-backup.json').read_bytes() == encoded
    c.data['user_name'] = 'Saved'
    c.save(c.data)
    again = config()
    assert again.get('user_name') == 'Saved'
    assert legacy.read_bytes() == encoded
    assert again.get('ollama.chat_model') == 'user/model:7b'
    assert again.get('dialogue.style') == 'military'


def test_canonical_wins_legacy_conflict():
    c = config()
    c.data['user_name'] = 'canonical'
    c.save(c.data)
    legacy = c.paths.root / 'settings.json'
    legacy.write_text('{"user_name": "legacy"}')
    assert config().get('user_name') == 'canonical'
    assert 'legacy' in legacy.read_text()


def test_corrupt_settings_recover_last_good_and_preserve_broken_bytes():
    c = config()
    c.data['user_name'] = 'Recovery target'
    c.save(c.data)
    c.data['user_name'] = 'Later'
    c.save(c.data)
    c.paths.settings.write_text('{broken')
    recovered = config()
    assert recovered.get('user_name') == 'Recovery target'
    assert Path(recovered.recovered_settings_backup).read_text() == '{broken'
    assert read_settings_path(c.paths.root) == recovered.paths.settings


def test_save_failure_does_not_destroy_current_settings(monkeypatch):
    c = config()
    original = c.paths.settings.read_bytes()
    real_replace = os.replace
    def failing_replace(source, destination):
        if Path(destination) == c.paths.settings:
            raise OSError('simulated failed disk replacement')
        return real_replace(source, destination)
    monkeypatch.setattr(os, 'replace', failing_replace)
    c.data['user_name'] = 'never committed'
    with pytest.raises(OSError):
        c.save(c.data)
    assert c.paths.settings.read_bytes() == original
    assert not list(c.paths.settings.parent.glob('*.tmp'))


def test_rollback_keeps_displaced_settings():
    c = config()
    c.data['user_name'] = 'Before'
    c.save(c.data)
    c.data['user_name'] = 'After'
    c.save(c.data)
    c.rollback_settings()
    assert c.get('user_name') == 'Before'
    displaced = list(c.paths.settings.parent.glob('settings.before-rollback-*.json'))
    assert len(displaced) == 1
    assert json.loads(displaced[0].read_text())['user_name'] == 'After'


def test_migration_write_failure_uses_existing_legacy(tmp_path, monkeypatch):
    root = tmp_path / 'profile'
    root.mkdir()
    legacy = root / 'settings.json'
    legacy.write_text('{"user_name":"kept"}')
    from jarvis import settings_storage184 as storage
    monkeypatch.setattr(storage, 'atomic_write', lambda *_: (_ for _ in ()).throw(OSError('disk full')))
    assert prepare_settings(root, ROOT / 'config/default_settings.json') == legacy
    assert json.loads(legacy.read_text())['user_name'] == 'kept'


def test_settings_folder_is_excluded_from_activity():
    from jarvis.runtime.observation_policy import ObservationPolicy
    c = config()
    policy = ObservationPolicy(c)
    assert not policy.allows_path(c.paths.settings)
    assert not policy.allows_path(c.paths.settings.parent / 'settings.last-good.json')


@pytest.fixture(scope='module')
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(app, monkeypatch):
    from jarvis.ui.settings_dialog import SettingsDialog, QMessageBox
    from jarvis.ui.qt_localization import install_ui_language
    monkeypatch.setattr(SettingsDialog, '_refresh_microphones_async', lambda *_: None)
    monkeypatch.setattr(SettingsDialog, '_start_game_cache_stats158', lambda *_: None)
    c = config()
    c.data['app']['language'] = 'ru'
    c.data['dialogue']['style'] = 'military'
    c.data['scenarios']['items'] = [{'id': 'keep', 'name': 'work', 'phrase': 'работа', 'aliases': ['труд'],
                                    'steps': [{'type': 'open', 'path': 'C:/work.txt', 'custom': 'keep'}, {'type': 'wait', 'seconds': 2}, {'type': 'command', 'text': 'help'}]}]
    install_ui_language(app, 'ru')
    errors = []
    monkeypatch.setattr(QMessageBox, 'critical', lambda *args: errors.append(args))
    d = SettingsDialog(c)
    assert d._settings_ready160 and not d._settings_build_error160
    d._test_errors184 = errors
    yield d
    d.reject()
    d.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def page_for(dialog, widget):
    for index in range(dialog.tabs._stack.count()):
        page = dialog.tabs._stack.widget(index)
        if page.isAncestorOf(widget):
            return dialog._settings_pages160[index][1]
    return None


def test_dedicated_sections_and_developer_visibility(dialog, app):
    assert page_for(dialog, dialog.scenario_editor.list) == 'Сценарии'
    assert page_for(dialog, dialog.scenario_editor.validate_button) == 'Сценарии'
    assert page_for(dialog, dialog.autoclicker_cps) == 'Горячие клавиши и автокликер'
    assert page_for(dialog, dialog.app_mic_hotkey) == 'Горячие клавиши и автокликер'
    assert page_for(dialog, dialog.telegram_enabled) == 'Удалённое подключение'
    assert page_for(dialog, dialog.remote_wake_service) == 'Удалённое подключение'
    for widget in (dialog.dev_enabled, dialog.show_tools, dialog.log_content169, dialog.atlas2_enabled, dialog.llm_finetune_auto170, dialog.ollama_url):
        assert page_for(dialog, widget) == 'Режим разработчика'
        assert dialog.developer_advanced_container.isAncestorOf(widget)
    assert dialog.developer_advanced_container.isHidden()
    dialog.developer_settings_enabled.setChecked(True)
    assert not dialog.developer_advanced_container.isHidden()
    assert not hasattr(dialog, 'dialogue_style')
    assert page_for(dialog, dialog.chat_model) == 'Общение и память'   # NEO 187 HREQ-007: «Ассистент»


def test_nvidia_clear_reset_roundtrip_and_unknown_scenario_steps(dialog, app):
    from PySide6.QtWidgets import QPushButton
    telegram = copy.deepcopy(dialog.config.data['integrations']['telegram'])
    for field, default in ((dialog.replay_hotkey169, 'alt+shift+f10'), (dialog.capture_clip_hotkey, 'alt+f10'), (dialog.capture_record_hotkey, 'alt+f9')):
        buttons = field.parentWidget().findChildren(QPushButton)
        next(x for x in buttons if x.text() == 'Очистить').click()
        assert field.text() == ''
        next(x for x in buttons if x.text() == 'По умолчанию').click()
        assert field.text() == default
        field.clear()
    dialog._save(close=False)
    assert not dialog._test_errors184
    assert dialog.config.get('capture.nvidia_clip_hotkey') == ''
    assert dialog.config.get('capture.nvidia_record_hotkey') == ''
    assert dialog.config.get('gaming.instant_replay.toggle_hotkey') == ''
    assert dialog.config.get('dialogue.style') == 'military'
    steps = dialog.config.get('scenarios.items')[0]['steps']
    assert steps[0]['custom'] == 'keep'
    assert steps[1:] == [{'type': 'wait', 'seconds': 2}, {'type': 'command', 'text': 'help'}]
    for key in ('allowed_user_ids', 'allowed_chat_ids', 'remote_control_enabled', 'remote_wake_enabled', 'allow_critical_actions', 'blocked_tools'):
        saved = dialog.config.get('integrations.telegram.' + key)
        expected = telegram.get(key)
        assert (set(saved) == set(expected)) if key == 'blocked_tools' else (saved == expected)


def test_model_operations_async_preserve_config_and_handle_cancel(dialog, app, monkeypatch):
    from PySide6.QtCore import QObject, Signal
    import jarvis.model_management as service
    class FakeOperation(QObject):
        progress = Signal(str, int)
        completed = Signal(dict)
        failed = Signal(str)
        finished = Signal()
        def __init__(self, action, model, base_url, **kwargs):
            super().__init__()
            self.action, self.model, self.base_url = action, model, base_url
            self.cancelled = self.started = False
        def start(self): self.started = True
        def cancel(self): self.cancelled = True
    monkeypatch.setattr(service, 'ModelOperation', FakeOperation)
    original = copy.deepcopy(dialog.config.data)
    dialog.chat_model.setCurrentText('custom:7b')
    dialog.ollama_url.setText('http://127.0.0.1:11435')
    progress = []
    dialog.model_progress.connect(progress.append)
    dialog._start_model_operation184('check')
    operation = dialog._model_operation184
    assert operation.started and operation.model == 'custom:7b'
    assert operation.base_url == 'http://127.0.0.1:11435'
    assert not dialog.chat_model_download184.isEnabled()
    assert dialog.config.data == original
    operation.progress.emit('Проверка', 30)
    app.processEvents()
    assert dialog.chat_model_progress184.value() == 30
    dialog._cancel_model_operation184()
    assert operation.cancelled
    assert dialog._model_operation184 is operation
    operation.failed.emit('Операция отменена')
    operation.finished.emit()
    app.processEvents()
    assert dialog._model_operation184 is None
    assert dialog.chat_model_download184.isEnabled()
    assert progress


def test_model_delete_requires_exact_confirmation_and_close_cancels(dialog, app, monkeypatch):
    from jarvis.ui.settings_dialog import QMessageBox
    prompts = []
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: (prompts.append(args), QMessageBox.No)[1])
    dialog.chat_model.setCurrentText('unique:0.5b')
    dialog._start_model_operation184('delete')
    assert dialog._model_operation184 is None
    assert 'unique:0.5b' in prompts[0][2] and 'localhost:11434' in prompts[0][2]
    class Pending:
        cancelled = False
        def cancel(self): self.cancelled = True
    pending = Pending()
    dialog._model_operation184 = pending
    dialog.reject()
    assert pending.cancelled
    dialog._model_operation184 = None


def test_bom_legacy_profile_is_read_without_data_loss():
    root = Path(os.environ['LOCALAPPDATA']) / 'NeoJarvis'
    root.mkdir(parents=True)
    (root / 'settings.json').write_text(json.dumps({'settings_version': 179, 'user_name': 'BOM kept'}), encoding='utf-8-sig')
    c = config()
    assert c.get('user_name') == 'BOM kept'
    assert not c.recovered_settings_backup


def test_autostart_switch_persists_without_implicit_model_install(dialog, monkeypatch):
    import jarvis.neo_llm.setup as setup
    monkeypatch.setattr(setup, 'ensure_background', lambda *_args, **_kw: pytest.fail('Apply must never download a model'))
    dialog.ollama_auto_start184.setChecked(False)
    dialog.llm_agent_size170.setCurrentIndex(1 if dialog.llm_agent_size170.currentIndex() == 0 else 0)
    dialog._save(close=False)
    assert not dialog._test_errors184
    assert dialog.config.get('ollama.auto_start') is False
    assert dialog.config.get('ollama.auto_install') is True
    assert config().get('ollama.auto_start') is False


def test_effective_legacy_model_override_becomes_visible_and_saves(dialog):
    dialog.chat_model.setCurrentText('old-effective:custom')
    dialog._save(close=False)
    assert not dialog._test_errors184
    assert dialog.config.get('ollama.chat_model') == 'old-effective:custom'
    assert dialog.config.get('local_chat.model') == 'old-effective:custom'


def test_model_result_translations_keep_model_identifiers():
    from jarvis.ui.settings_text184 import model_text
    for code in ('en','de','fr','es','it','pt'):
        text = model_text('Модель user/model:0.5b скачана и проверена.', code)
        assert 'user/model:0.5b' in text and 'Модель' not in text


def test_existing_override_is_shown_without_mutating_config_on_open(app, monkeypatch):
    from jarvis.ui.settings_dialog import SettingsDialog
    from PySide6.QtCore import QCoreApplication, QEvent
    c = config()
    c.data['local_chat']['model'] = 'existing-effective:3b'
    c.data['ollama']['chat_model'] = 'different-unused:1b'
    original = copy.deepcopy(c.data)
    monkeypatch.setattr(SettingsDialog, '_refresh_microphones_async', lambda *_: None)
    monkeypatch.setattr(SettingsDialog, '_start_game_cache_stats158', lambda *_: None)
    d = SettingsDialog(c)
    try:
        assert d.chat_model.currentText() == 'existing-effective:3b'
        assert c.data == original
    finally:
        d.reject(); d.deleteLater(); QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_hotkey_clear_reset_fields_and_settings_folder_controls(dialog,monkeypatch):
    from PySide6.QtWidgets import QPushButton
    from PySide6.QtGui import QKeySequence, QDesktopServices
    for name,default in (('app_mic_hotkey','Ctrl+Alt+M'),('autoclicker_hotkey','Ctrl+Alt+C'),
                         ('chat_scroll_up_hotkey','Ctrl+Alt+Up'),('chat_scroll_down_hotkey','Ctrl+Alt+Down'),
                         ('window_topmost_hotkey','Ctrl+Alt+T')):
        field=getattr(dialog,name)
        buttons=field.parentWidget().findChildren(QPushButton)
        next(button for button in buttons if button.text()=='Очистить').click()
        assert field.keySequence().isEmpty()
        next(button for button in buttons if button.text()=='По умолчанию').click()
        assert field.keySequence()==QKeySequence(default)
    field=dialog.storage_settings_dir184
    assert field.edit.isReadOnly() and field.browse_button.isHidden() and field.default_button.isHidden()
    calls=[]
    monkeypatch.setattr(QDesktopServices,'openUrl',lambda url:(calls.append(url.toLocalFile()),True)[1])
    field.open_button.click()
    assert calls==[str(dialog.config.paths.settings.parent)]


def test_scenario_add_remove_is_draft_only(dialog, monkeypatch):
    from jarvis.ui import scenario_editor
    source = copy.deepcopy(dialog.config.get('scenarios.items'))
    editor = dialog.scenario_editor
    count = editor.list.count()
    editor.new_button.click()
    assert editor.list.count() == count + 1
    editor.name.setText('Draft scenario')
    assert dialog.config.get('scenarios.items') == source
    monkeypatch.setattr(scenario_editor, '_confirm', lambda *_: False)
    editor.delete_button.click()
    assert editor.list.count() == count + 1
    monkeypatch.setattr(scenario_editor, '_confirm', lambda *_: True)
    editor.delete_button.click()
    assert editor.list.count() == count
    assert dialog.config.get('scenarios.items') == source


def test_model_start_and_existing_list_test_buttons_are_async(dialog,app,monkeypatch):
    from PySide6.QtCore import QObject,Signal
    import jarvis.model_management as service
    calls=[]
    class Operation(QObject):
        progress=Signal(str,int);completed=Signal(dict);failed=Signal(str);finished=Signal()
        def __init__(self,action,model,base_url,**kwargs):
            super().__init__();calls.append((action,kwargs));self.started=False
        def start(self):self.started=True
        def cancel(self):pass
    monkeypatch.setattr(service,'ModelOperation',Operation)
    for button,action in ((dialog.chat_model_start184,'start'),(dialog.model_list_button184,'list'),(dialog.model_server_check_button184,'list')):
        button.click();operation=dialog._model_operation184
        assert operation.started and calls[-1][0]==action
        assert calls[-1][1]['auto_start'] is False
        operation.completed.emit({'action':action,'models':['qwen2.5:0.5b'],'message':'Ollama доступна. Установлено моделей: 1.'})
        operation.finished.emit();app.processEvents()
        assert dialog._model_operation184 is None


def test_scenario_headers_localize_without_touching_user_rows(dialog):
    from jarvis.ui.scenario_strings import st
    editor = dialog.scenario_editor
    original = editor.name.text()
    original_rows = copy.deepcopy(editor.items())
    for code in ('en', 'de', 'ru'):
        dialog.app_language.setCurrentIndex(dialog.app_language.findData(code))
        dialog._save(close=False)
        assert not dialog._test_errors184
        assert tuple(editor.steps.headerItem().text(i) for i in range(3)) == tuple(st(key, code) for key in ('step', 'summary', 'policy'))
        assert editor.name.text() == original
        assert editor.items() == original_rows
