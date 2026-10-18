"""Disposable settings profiles; one shared QApplication, no external effects."""
import copy

from tests.test_neo184_settings import app, dialog, isolated, config, page_for


def controls(dialog):
    return [(dialog.nvidia_notices184, 'gaming.notifications.nvidia_enabled', True),
            (dialog.nvidia_startup_notice184, 'gaming.notifications.nvidia_startup', True),
            (dialog.nvidia_auto_launch184, 'gaming.instant_replay.auto_launch_app', False)]


def test_controls_are_on_gaming_and_cancel_preserves_saved_settings(dialog):
    before = copy.deepcopy(dialog.config.data)
    disk = dialog.config.paths.settings.read_bytes()
    for widget, key, default in controls(dialog):
        assert page_for(dialog, widget) == 'Игры'
        assert widget.isChecked() is default
        widget.setChecked(not default)
    dialog.reject()
    assert dialog.config.data == before
    assert dialog.config.paths.settings.read_bytes() == disk


def test_apply_roundtrip_preserves_other_gaming_options(dialog):
    for widget, _key, default in controls(dialog):
        widget.setChecked(not default)
    dialog.replay_auto_enable171.setChecked(True)
    dialog.replay_changes169.setChecked(False)
    dialog.replay_hotkey169.setText('ctrl+f8')
    dialog._save(close=False)
    assert not dialog._test_errors184
    again = config()
    for _widget, key, default in controls(dialog):
        assert again.get(key) is not default
    assert again.get('gaming.instant_replay.auto_enable') is True
    assert again.get('gaming.notifications.replay_changes') is False
    assert again.get('gaming.instant_replay.toggle_hotkey') == 'ctrl+f8'
    assert again.get('dialogue.style') == 'military'


def test_all_seven_languages_retranslate_controls_and_fit_gaming_scroll(dialog, app):
    from PySide6.QtWidgets import QScrollArea
    from PySide6.QtTest import QTest
    from jarvis.ui.settings_text184 import ROWS, LANGS
    source = {widget: widget.text() for widget, _key, _default in controls(dialog)}
    dialog.show()
    dialog.resize(800, 600)
    index = next(i for i in range(dialog.tabs._stack.count()) if dialog.tabs._stack.widget(i).isAncestorOf(dialog.nvidia_notices184))
    dialog.tabs.setCurrentIndex(index)
    for code in ('ru', *LANGS):
        dialog.app_language.setCurrentIndex(dialog.app_language.findData(code))
        dialog._save(close=False)
        assert not dialog._test_errors184
        QTest.qWait(25)
        for widget, key in source.items():
            assert widget.text() == (key if code == 'ru' else ROWS[key][LANGS.index(code)])
        page = dialog.tabs._stack.currentWidget()
        assert isinstance(page, QScrollArea)
        assert page.horizontalScrollBar().maximum() == 0
