"""OBS settings contracts. No real OBS, socket, credential or executable access."""
from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PySide6.QtCore import QThread, QThreadPool, Qt
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit

from jarvis.integrations.obs_text184 import CODES, ROWS, tr
from jarvis.ui.streaming_settings184 import StreamingSettingsPage


ROOT = Path(__file__).resolve().parents[1]


class Config:
    def __init__(self, streaming=None):
        self.data = {'app': {'language': 'en'}, 'streaming': deepcopy(streaming or {})}

    def get(self, key, default=None):
        value = self.data
        for part in key.split('.'):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def save(self, data):
        self.data = deepcopy(data)


class HeldPool:
    def __init__(self):
        self.jobs = []

    def start(self, worker):
        self.jobs.append(worker)


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def page(app):
    widget = StreamingSettingsPage(Config(), pool=HeldPool())
    yield widget
    widget.close_operations()
    widget.deleteLater()
    app.processEvents()


def test_all_messages_have_seven_locales_and_matching_placeholders():
    assert CODES == ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
    required = {'disabled', 'connected', 'disconnected', 'auth_required', 'credential_missing', 'local_only',
                'dependency_missing', 'connection_failed', 'protocol_error', 'unsupported', 'request_failed',
                'uncertain', 'confirmation_required', 'invalid_argument', 'not_found', 'ambiguous', 'changed',
                'already', 'saved', 'file_unverified', 'scenes', 'status_summary', 'launch_failed', 'busy',
                'active', 'inactive', 'paused', 'unknown'}
    assert required <= ROWS.keys()
    for key, row in ROWS.items():
        assert len(row) == 7, key
        fields = [{field for _, field, _, _ in Formatter().parse(text) if field is not None} for text in row]
        assert all(text for text in row), key
        assert all(field == fields[0] for field in fields), key
    for language in CODES:
        assert '601' in tr('request_failed', language, code=601)
        assert '<b> Scene </b>' in tr('scenes', language, names='<b> Scene </b>')
    assert tr('unknown', 'unsupported-locale') == tr('unknown', 'en')


def test_schema_defaults_are_opt_in_and_migration_preserves_existing_settings():
    from jarvis.runtime.settings184 import migrate
    defaults = json.loads((ROOT / 'config/default_settings.json').read_text(encoding='utf-8'))['streaming']
    assert defaults == dict(enabled=False, host='127.0.0.1', port=4455, password_encrypted='', executable='',
                            auto_launch_on_game=False, auto_replay_on_game=False, microphone_input='', preferred_scene='')
    data = {'streaming': {'port': 4466, 'preferred_scene': ' edge '},
            'gaming': {'instant_replay': {'registry_values': {'old': 1}, 'hotkey': 'alt+f10', 'auto_launch_app': True}}}
    migrate(data)
    assert data['streaming']['port'] == 4466
    assert data['streaming']['preferred_scene'] == ' edge '
    assert data['streaming']['enabled'] is False
    assert data['gaming']['instant_replay'] == {'hotkey': 'alt+f10', 'auto_launch_app': True}


def test_open_and_ordinary_save_never_construct_service_or_read_password(app):
    config = Config({'password_encrypted': 'dpapi:opaque-token'})
    with patch('jarvis.integrations.obs_service184.get_obs_service') as factory, \
         patch('jarvis.integrations.obs_service184.set_session_password') as setter, \
         patch('jarvis.secrets.unprotect_secret') as decrypt, patch('jarvis.secrets.protect_secret') as encrypt:
        page = StreamingSettingsPage(config, pool=HeldPool())
        assert page.password.text() == ''
        assert page.password.echoMode() == QLineEdit.Password
        assert not page.remember.isChecked()
        values, update = page.prepare_save()
        assert values['password_encrypted'] == 'dpapi:opaque-token'
        assert update is None
        page.save_succeeded(update)
        page.close_operations()
        factory.assert_not_called()
        setter.assert_not_called()
        decrypt.assert_not_called()
        encrypt.assert_not_called()
        page.deleteLater()


def test_names_and_executable_are_preserved_verbatim(page):
    page.microphone.setText(' Mic ')
    page.scene.setText('  Scene  ')
    page.executable.setText('C:/Program Files/OBS Studio/bin/64bit/obs64.exe')
    values, _ = page.prepare_save()
    assert values['microphone_input'] == ' Mic '
    assert values['preferred_scene'] == '  Scene  '
    assert values['executable'] == page.executable.text()
    assert values['host'] == '127.0.0.1'
    assert page.host.isReadOnly()


def test_session_password_is_committed_only_after_successful_save(page):
    page.password.setText('  private test password  ')
    before = deepcopy(page.config.data)
    with patch('jarvis.integrations.obs_service184.set_session_password') as setter:
        values, update = page.prepare_save()
        setter.assert_not_called()
        assert page.config.data == before
        assert values['password_encrypted'] == ''
        with patch.object(page.config, 'save', side_effect=OSError('test save failure')):
            with pytest.raises(OSError):
                page.config.save(dict(page.config.data, streaming=values))
        setter.assert_not_called()
        assert page.password.text() == '  private test password  '
        page.config.save(dict(page.config.data, streaming=values))
        page.save_succeeded(update)
        setter.assert_called_once_with(page.config, '  private test password  ')
        assert page.password.text() == ''


def test_cancel_discards_only_staged_password_and_forget(page):
    page.config.data['streaming']['password_encrypted'] = 'dpapi:existing'
    with patch('jarvis.integrations.obs_service184.set_session_password') as setter:
        page.password.setText('new')
        page._stage_forget()
        page.close_operations()
        setter.assert_not_called()
    assert page.config.get('streaming.password_encrypted') == 'dpapi:existing'
    assert page.password.text() == ''


def test_explicit_forget_is_transactional(page):
    page.config.data['streaming']['password_encrypted'] = 'dpapi:existing'
    page._stage_forget()
    with patch('jarvis.integrations.obs_service184.set_session_password') as setter:
        values, update = page.prepare_save()
        assert values['password_encrypted'] == '' and update == ''
        assert page.config.get('streaming.password_encrypted') == 'dpapi:existing'
        setter.assert_not_called()
        page.config.save(dict(page.config.data, streaming=values))
        page.save_succeeded(update)
        setter.assert_called_once_with(page.config, '')


def test_new_password_replaces_pending_forget(page):
    page._stage_forget()
    page.password.setText('replacement')
    assert not page._forget_pending
    assert page.credential_status.text() == ''
    values, update = page.prepare_save()
    assert update == 'replacement'
    assert values['password_encrypted'] == ''


def test_remember_requires_windows_and_real_dpapi_token(page):
    page.password.setText('test secret')
    page.remember.setChecked(True)
    with patch('jarvis.ui.streaming_settings184.os', SimpleNamespace(name='posix')), patch('jarvis.secrets.protect_secret') as protect:
        with pytest.raises(ValueError):
            page.prepare_save()
        protect.assert_not_called()
    with patch('jarvis.ui.streaming_settings184.os', SimpleNamespace(name='nt')):
        for invalid_token in ('plain64:not-encrypted', 'dpapi:', 'dpapi:   ', None, b'dpapi:bytes'):
            with patch('jarvis.secrets.protect_secret', return_value=invalid_token):
                with pytest.raises(ValueError) as raised:
                    page.prepare_save()
                assert str(raised.value) == tr('credential_store_failed', page.language)
        with patch('jarvis.secrets.protect_secret', return_value='dpapi:protected'):
            values, update = page.prepare_save()
            assert values['password_encrypted'] == 'dpapi:protected'
            assert update == 'test secret'
    assert page.config.get('streaming.password_encrypted', '') == ''


def test_protector_exception_cannot_expose_password_or_provider_text(page):
    secret = 'synthetic OBS credential 184'
    provider_text = 'synthetic provider dump'
    page.password.setText(secret)
    page.remember.setChecked(True)
    before = deepcopy(page.config.data)
    with patch('jarvis.ui.streaming_settings184.os', SimpleNamespace(name='nt')), \
         patch('jarvis.secrets.protect_secret', side_effect=RuntimeError(provider_text + ': ' + secret)), \
         patch('jarvis.integrations.obs_service184.set_session_password') as setter:
        for language in CODES:
            page.config.data['app']['language'] = language
            page.retranslate()
            with pytest.raises(ValueError) as raised:
                page.prepare_save()
            # SettingsDialog displays str(exc); only fixed translated text may cross.
            displayed_error = str(raised.value)
            assert displayed_error == tr('credential_store_failed', language)
            assert secret not in displayed_error and provider_text not in displayed_error
            assert raised.value.__context__ is None
            assert raised.value.__cause__ is None
        setter.assert_not_called()
    assert page.config.data['streaming'] == before['streaming']
    assert page.password.text() == secret


def test_single_flight_and_apply_generation_drop_late_results(page):
    page.check_saved_settings()
    relay = page._relay
    assert len(page._pool.jobs) == 1
    page.check_saved_settings()
    assert len(page._pool.jobs) == 1
    page.prepare_save()
    text = page.status_label.text()
    relay.result({'connected': True, 'scene': 'stale scene'})
    relay.error('secret traceback must not render')
    assert page.status_label.text() == text
    page.check_saved_settings()
    assert len(page._pool.jobs) == 1
    relay.finished()
    assert page.check_button.isEnabled()
    page.check_saved_settings()
    assert len(page._pool.jobs) == 2


def test_close_generation_drops_late_results(page):
    page.check_saved_settings()
    relay = page._relay
    page.close_operations()
    text = page.status_label.text()
    relay.result({'connected': True, 'scene': 'late scene'})
    relay.error('late error')
    relay.finished()
    assert page.status_label.text() == text
    page.check_saved_settings()
    assert len(page._pool.jobs) == 1


def test_service_factory_and_status_run_off_gui_with_saved_settings(app):
    config = Config({'enabled': True, 'port': 4455})
    pool = QThreadPool()
    page = StreamingSettingsPage(config, pool=pool)
    page.port.setValue(4466)
    calls = []

    def factory(received):
        assert QThread.currentThread() != app.thread()
        assert received.get('streaming.port') == 4455
        calls.append('factory')
        def status():
            assert QThread.currentThread() != app.thread()
            calls.append('status')
            return {'connected': True, 'recording': None, 'recording_paused': None, 'streaming': None,
                    'replay_active': None, 'microphone_muted': None, 'scene': '<b>OBS scene</b>',
                    'duration_seconds': None, 'recording_duration_seconds': None, 'dropped_frames': None, 'error': ''}
        return SimpleNamespace(status=status)

    with patch('jarvis.integrations.obs_service184.get_obs_service', side_effect=factory):
        page.check_saved_settings()
        assert pool.waitForDone(5000)
        app.processEvents()
        assert calls == ['factory', 'status']
        assert '<b>OBS scene</b>' in page.status_label.text()
        assert page.status_label.textFormat() == Qt.PlainText
        assert 'unknown' in page.status_label.text()
        assert 'inactive' not in page.status_label.text()
    page.close_operations()
    page.deleteLater()


def test_live_language_change_recalculates_narrow_wrapped_heights(page, app):
    page.resize(290, 1800)
    page.show()
    app.processEvents()
    for language in CODES:
        page.config.data['app']['language'] = language
        page.retranslate()
        app.processEvents()
        for checkbox in (page.enabled, page.remember, page.auto_launch, page.auto_replay):
            assert checkbox.minimumHeight() >= checkbox.heightForWidth(checkbox.width())
        for label in page.findChildren(QLabel):
            assert label.textFormat() == Qt.PlainText
            if label.text():
                assert label.height() >= label.heightForWidth(label.width())
        assert page.enabled.text() == tr('enabled_label', language)


def test_unknown_pause_duration_and_literal_errors_are_preserved(page):
    page._receive_status(page._generation, {'connected': True, 'recording': True, 'recording_paused': None,
                         'streaming': False, 'replay_active': None, 'microphone_muted': None,
                         'scene': ' edge ', 'duration_seconds': None, 'recording_duration_seconds': 61,
                         'dropped_frames': None, 'error': '<img src="file:///private">'})
    result = page.status_label.text()
    assert 'Recording paused: unknown' in result
    assert 'recording duration: 00:01:01' in result
    assert ' edge ' in result
    assert '<img src="file:///private">' in result
    assert page.status_label.textFormat() == Qt.PlainText


def test_dialog_append_and_save_order_contract():
    source = (ROOT / 'jarvis/ui/settings_dialog.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'SettingsDialog')
    save = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_save')
    calls = [node for node in ast.walk(save) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    saved = next(node.lineno for node in calls if node.func.attr == 'save' and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == 'config')
    committed = next(node.lineno for node in calls if node.func.attr == 'save_succeeded')
    prepared = next(node.lineno for node in calls if node.func.attr == 'prepare_save')
    assert prepared < saved < committed
    init = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '__init__')
    pages = next(node.value for node in ast.walk(init) if isinstance(node, ast.Assign) and any(isinstance(target, ast.Attribute) and target.attr == '_settings_pages160' for target in node.targets))
    assert len(pages.elts) == 17   # NEO 187 HREQ-007: + «Общение и память»
    assert pages.elts[2].elts[1].value == 'Игры'
    assert pages.elts[13].elts[1].value == 'Удалённое подключение'
    assert pages.elts[14].elts[1].value == 'Голос и ответы'   # 186: settings TZ 2 rename of «Голос»
    assert pages.elts[15].elts[1].value == 'Стриминг'
    assert pages.elts[16].elts[1].value == 'Общение и память'
    tabs = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ScrollableSettingsTabs')
    subtitles = next(node.value for node in tabs.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'SUBTITLES' for target in node.targets))
    assert subtitles.elts[14].value == ''
    assert subtitles.elts[15].value == ROWS['subtitle'][0]
