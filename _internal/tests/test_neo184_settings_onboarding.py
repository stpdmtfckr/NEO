"""First-run model invitation: disposable profiles and no real model requests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from .test_neo184_settings_mainwindow import app, window, settle, FakeOperation
from jarvis.config import ConfigManager
from jarvis.testing.isolation import external_io_blocked
from jarvis.ollama_models import DEFAULT_CHAT_MODEL

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def profile(tmp_path, monkeypatch):
    for name in ('HOME', 'LOCALAPPDATA', 'APPDATA', 'XDG_CACHE_HOME'):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setattr(ConfigManager, 'apply_wake_service', lambda *_: None)
    monkeypatch.setattr(ConfigManager, 'apply_autostart', lambda *_: None)
    with external_io_blocked(), patch.object(socket.socket, 'sendto', side_effect=PermissionError('QA datagram block')):
        yield lambda: ConfigManager(ROOT / 'config/default_settings.json')


def choice(window, action):
    box = window._model_setup_offer184
    return next(button for button in box.buttons() if button.property('model_setup_action184') == action)


def ready(window, app):
    import time
    dialog = window._settings_dialog
    deadline = time.monotonic() + 6
    while not dialog._settings_ready160 and time.monotonic() < deadline:
        settle(app, .02)
    settle(app, .05)
    assert dialog._settings_ready160
    return dialog


@pytest.fixture
def operations(monkeypatch):
    import jarvis.model_management as service
    created = []
    class Operation(FakeOperation):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.auto_start = kwargs.get('auto_start')
            created.append(self)
    monkeypatch.setattr(service, 'ModelOperation', Operation)
    return created


def test_fresh_marker_persists_without_becoming_a_packaged_default(profile):
    config = profile()
    assert config.get('local_chat.setup_offer_pending184') is True
    assert profile().get('local_chat.setup_offer_pending184') is True
    assert 'setup_offer_pending184' not in json.loads((ROOT / 'config/default_settings.json').read_text())['local_chat']


@pytest.mark.parametrize('relative', ['settings.json', 'настройки/settings.json'])
@pytest.mark.parametrize('model', ['custom-user:7b', DEFAULT_CHAT_MODEL])
def test_existing_profiles_never_receive_offer_marker(profile, relative, model):
    target = Path(os.environ['LOCALAPPDATA']) / 'NeoJarvis' / relative
    target.parent.mkdir(parents=True)
    payload = {'ollama': {'chat_model': model}, 'local_chat': {'model': model}}
    target.write_text(json.dumps(payload))
    config = profile()
    assert not config.get('local_chat.setup_offer_pending184', False)
    assert config.get('local_chat.model') == model


def test_visible_offer_later_is_persisted_and_not_repeated(window, app, operations):
    window._offer_default_model184()
    box = window._model_setup_offer184
    settle(app)
    assert box.isVisible() and not box.isModal()
    assert DEFAULT_CHAT_MODEL in box.text() and '398' in box.text()
    assert box.defaultButton() is choice(window, 'later')
    assert not operations
    choice(window, 'later').click(); settle(app)
    assert window._model_setup_offer184 is None
    assert ConfigManager(ROOT / 'config/default_settings.json').get('local_chat.setup_offer_pending184') is False
    window._offer_default_model184()
    assert window._model_setup_offer184 is None
    assert not operations


def test_open_settings_choice_focuses_model_without_download(window, app, operations):
    window._offer_default_model184()
    choice(window, 'settings').click()
    dialog = ready(window, app)
    assert dialog.tabs._stack.currentIndex() == dialog.page_index186('conversation')   # NEO 187 HREQ-007
    assert dialog.chat_model.currentText() == DEFAULT_CHAT_MODEL
    assert not operations


def test_download_choice_progress_cancel_and_disabled_autostart(window, app, operations):
    assert window.config.get('ollama.auto_start') is False
    window._offer_default_model184()
    choice(window, 'download').click()
    dialog = ready(window, app)
    assert len(operations) == 1
    operation = operations[0]
    assert (operation.action, operation.model, operation.auto_start) == ('download', DEFAULT_CHAT_MODEL, False)
    operation.progress.emit(DEFAULT_CHAT_MODEL + ': test progress (37%)', 37); settle(app)
    assert dialog.chat_model_progress184.value() == 37
    assert '37%' in window._model_progress_bubble.body_label.text()
    dialog.chat_model_cancel184.click()
    assert operation.cancelled
    operation.completed.emit({'status': 'cancelled', 'message': 'Операция с моделью отменена.'})
    operation.finished.emit(); settle(app)
    assert dialog._model_operation184 is None
    assert dialog.chat_model_download184.isEnabled()


def test_close_during_deferred_build_never_starts_download(window, app, operations):
    window._offer_default_model184()
    choice(window, 'download').click()
    dialog = window._settings_dialog
    assert not dialog._settings_ready160
    dialog.reject(); settle(app, .3)
    assert not operations
    assert window._settings_dialog is None


@pytest.mark.parametrize('draft_timing', ['before_choice', 'during_build'])
def test_invitation_consent_never_downloads_a_changed_draft_model(window, app, operations, draft_timing):
    window._offer_default_model184()
    if draft_timing == 'before_choice':
        window._open_settings()
        dialog = ready(window, app)
        dialog.chat_model.setCurrentText('user-draft:7b')
        choice(window, 'download').click()
    else:
        choice(window, 'download').click()
        dialog = window._settings_dialog
        dialog._settings_build_timer160.stop()
        while dialog._settings_page_index160 <= 8:
            dialog._build_next_settings_page160(schedule=False)
        dialog.chat_model.setCurrentText('user-draft:7b')
        while not dialog._settings_ready160:
            dialog._build_next_settings_page160(schedule=False)
    settle(app, .1)
    assert dialog.chat_model.currentText() == 'user-draft:7b'
    assert not operations
    assert 'изменилась' in dialog.chat_model_status184.text()
    assert dialog.chat_model_download184.isEnabled()


def test_service_missing_is_actionable_and_never_enables_autostart(window, app, operations):
    window._offer_default_model184()
    choice(window, 'download').click()
    dialog = ready(window, app)
    operation = operations[0]
    operation.failed.emit('Локальная служба Ollama не отвечает. Запустите Ollama и повторите проверку; если она не установлена: https://ollama.com/download. Результат операции не подтверждён.')
    operation.finished.emit(); settle(app)
    assert 'https://ollama.com/download' in dialog.chat_model_status184.text()
    assert dialog.chat_model_start184.isEnabled()
    assert window.config.get('ollama.auto_start') is False


def test_changed_model_before_invitation_is_never_overridden(window, app, operations):
    window.config.data['local_chat']['model'] = 'my-existing:9b'
    window.config.save(window.config.data)
    window._offer_default_model184()
    assert getattr(window, '_model_setup_offer184', None) is None
    assert window.config.get('local_chat.model') == 'my-existing:9b'
    assert not operations


@pytest.mark.parametrize('language', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_offer_captions_are_localized(window, app, language):
    window.config.data['app']['language'] = language
    window.config.save(window.config.data)
    window._offer_default_model184()
    box = window._model_setup_offer184
    settle(app)
    texts = [box.windowTitle(), box.text(), box.informativeText()] + [b.text() for b in box.buttons()]
    if language != 'ru':
        assert not any(re.search('[А-Яа-яЁё]', value) for value in texts)
    evidence = os.getenv('NEO184_ONBOARDING_SCREENSHOTS')
    if evidence and language in {'ru', 'en'}:
        target = Path(evidence); target.mkdir(parents=True, exist_ok=True)
        assert box.grab().save(str(target / (language + '-first-model-offer.png')))
    choice(window, 'later').click(); settle(app)


def test_existing_installed_default_reuses_actual_download_backend_without_pull(app, monkeypatch):
    from jarvis.model_management import ModelOperation
    import jarvis.model_management as service
    class Session:
        def close(self): pass
    monkeypatch.setattr(service.requests, 'Session', Session)
    monkeypatch.setattr(ModelOperation, '_models', lambda _: [DEFAULT_CHAT_MODEL])
    monkeypatch.setattr(ModelOperation, '_pull', lambda _: pytest.fail('An installed model must never be pulled'))
    results = []
    operation = ModelOperation('download', DEFAULT_CHAT_MODEL, 'http://localhost:11434', auto_start=False)
    operation.completed.connect(results.append)
    with external_io_blocked():
        operation._run()
    assert results[0]['status'] == 'installed'
    assert results[0]['model'] == DEFAULT_CHAT_MODEL
    settle(app)
