"""Production Settings opens a light shell and yields between real Qt pages."""
from copy import deepcopy
import time

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QWidget

from jarvis.i18n import language_code
from jarvis.ui.settings_dialog import SettingsDialog, QMessageBox
from jarvis.testing.isolation import external_io_blocked
from tests.test_neo158_settings_io import app, config


def pump(app, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.001)
    assert predicate()


@pytest.fixture
def dialogs(app, config, monkeypatch):
    windows = []
    monkeypatch.setattr(QMessageBox, 'critical', lambda *args: pytest.fail(str(args[2:])))
    def make(**kwargs):
        dialog = SettingsDialog(config, **kwargs)
        windows.append(dialog)
        return dialog
    with external_io_blocked():
        yield make
        for dialog in windows:
            if not shiboken6.isValid(dialog):
                continue
            dialog.reject()
            dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()


def test_deferred_shell_lets_qt_heartbeat_and_cancel_run_between_all_real_pages(dialogs, app):
    dialog = dialogs(deferred=True)
    assert dialog.tabs._stack.count() == 15  # Dedicated response Voice page follows the existing sections.
    assert dialog._settings_page_index160 == 0
    assert not hasattr(dialog, 'input_device')
    assert dialog.cancel_button.isEnabled()
    assert not dialog.apply_button.isEnabled() and not dialog.defaults_button.isEnabled()
    beats = []
    heartbeat = QTimer(dialog)
    heartbeat.setInterval(1)
    heartbeat.timeout.connect(lambda: beats.append(dialog._settings_page_index160))
    heartbeat.start()
    dialog.show()
    dialog.tabs.setCurrentIndex(3)
    pump(app, lambda: dialog._settings_ready160)
    heartbeat.stop()
    assert set(range(8)).issubset(set(beats))
    assert dialog.tabs._stack.currentIndex() == 3
    assert dialog.tabs._buttons[3].isChecked()
    assert all(factory and title for factory, title in dialog._settings_pages160)
    assert not hasattr(dialog, 'voice_output')  # The removed legacy voice output stays removed.
    assert hasattr(dialog, 'response_voice_page') and hasattr(dialog, 'robot_active_animation')
    assert dialog.apply_button.isEnabled() and dialog.save_button.isEnabled()
    assert not dialog._settings_load_status160.isVisible()


def test_deferred_and_eager_real_controls_save_identical_configuration(dialogs, app, config, monkeypatch):
    eager = dialogs()
    delayed = dialogs(deferred=True)
    pump(app, lambda: delayed._settings_ready160)
    snapshots = []
    monkeypatch.setattr(config, 'save', lambda data: snapshots.append(deepcopy(data)))
    eager._save(close=False)
    delayed._save(close=False)
    assert len(snapshots) == 2
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize('built', [0, 1, 4, 8, 9, 10, 11, 12, 13])
def test_close_at_each_stage_cancels_pending_page_build_and_never_leaves_callbacks(dialogs, app, built, monkeypatch):
    errors = []
    monkeypatch.setattr('sys.excepthook', lambda *args: errors.append(args))
    dialog = dialogs(deferred=True)
    dialog._settings_build_timer160.stop()
    for _ in range(built):
        dialog._build_next_settings_page160(schedule=False)
    dialog.reject()
    for _ in range(3):
        dialog._build_next_settings_page160()
        app.processEvents()
    assert dialog._settings_page_index160 == built
    assert not dialog._settings_build_timer160.isActive()
    assert not errors
    # Owned geometry/collapse timers are destroyed with the widgets, including
    # pending 0-ms callbacks before the first normal event-loop iteration.
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    assert not errors


def test_incomplete_settings_cannot_save_or_reset(dialogs, config, monkeypatch):
    dialog = dialogs(deferred=True)
    calls = []
    monkeypatch.setattr(config, 'save', lambda data: calls.append('save'))
    monkeypatch.setattr(config, 'reset_to_defaults', lambda: calls.append('reset'))
    dialog._save(close=False)
    dialog._reset_all_settings()
    assert calls == []


def test_failed_page_replaces_loading_text_with_localized_error_and_remains_closable(dialogs, config, monkeypatch, app):
    config.data['app']['language'] = 'de'
    def broken(_self):
        raise RuntimeError('injected page construction failure')
    monkeypatch.setattr(SettingsDialog, '_media_tab', broken)
    dialog = dialogs(deferred=True)
    pump(app, lambda: dialog._settings_build_error160)
    assert not dialog._settings_build_timer160.isActive()
    assert not dialog._settings_ready160
    assert 'konnten nicht geladen' in dialog._settings_load_status160.text()
    assert dialog.cancel_button.isEnabled()
    assert not dialog.save_button.isEnabled()
    index = dialog._settings_page_index160
    for _ in range(3):
        app.processEvents()
        dialog._build_next_settings_page160()
    assert dialog._settings_page_index160 == index == 1
    dialog.cancel_button.click()
    assert dialog._settings_closed160


@pytest.mark.parametrize('code,label', [('en','Fireflies'), ('de','Glühwürmchen'), ('fr','Lucioles')])
def test_deferred_pages_are_localized_and_apply_language_after_completion(dialogs, config, app, code, label):
    config.data['app']['language'] = code
    dialog = dialogs(deferred=True)
    pump(app, lambda: dialog._settings_ready160)
    combo = dialog.robot_active_animation
    assert combo.itemText(combo.findData('fireflies')) == label
    dialog.app_language.setCurrentIndex(dialog.app_language.findData('ru'))
    dialog._save(close=False)
    assert language_code(config) == 'ru'
    assert combo.itemText(combo.findData('fireflies')) == 'Светлячки'


def test_repeated_theme_refresh_does_not_repolish_all_controls_and_palette_change_does(dialogs, config, monkeypatch):
    dialog = dialogs()
    original = dialog.setStyleSheet
    applied = []
    def style(value):
        applied.append(value)
        original(value)
    monkeypatch.setattr(dialog, 'setStyleSheet', style)
    for _ in range(5):
        dialog._refresh_dialog_theme()
    assert applied == []
    config.data['ui']['accent'] = '#FED321'
    dialog._refresh_dialog_theme()
    assert len(applied) == 1 and '#fed321' in applied[0]
    dialog._refresh_dialog_theme()
    assert len(applied) == 1

