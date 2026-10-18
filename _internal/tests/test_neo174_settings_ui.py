"""NEO 174: settings window with a sidebar, search and page header."""
import time

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QLabel, QPushButton

from jarvis.ui.settings_dialog import SettingsDialog, QMessageBox
from jarvis.testing.isolation import external_io_blocked
from tests.test_neo158_settings_io import app, config


@pytest.fixture
def dialog(app, config, monkeypatch):
    monkeypatch.setattr(QMessageBox, 'critical', lambda *args: pytest.fail(str(args[2:])))
    with external_io_blocked():
        value = SettingsDialog(config)
        value.resize(1000, 660)
        value.show()
        app.processEvents()
        yield value
        value.reject()
        value.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()


def settle(app, seconds=0.25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def test_sections_are_a_sidebar_with_full_names_and_a_header(dialog, app):
    tabs = dialog.tabs
    assert tabs._stack.count() == 17 and len(tabs._buttons) == 17  # NEO184 sections + 186 voice/streaming + 187 conversation
    assert all(button.objectName() == 'settingsTabButton' and button.toolTip() for button in tabs._buttons)
    tabs.setCurrentIndex(5)
    assert tabs._title.text() == tabs._buttons[5].text() and tabs._subtitle.text()
    assert dialog.save_button.objectName() == 'primaryButton'
    # Enter in the search box must never press «Сохранить» and close the window.
    footer = (dialog.defaults_button, dialog.cancel_button, dialog.apply_button, dialog.save_button)
    assert not any(button.isDefault() or button.autoDefault() for button in footer)


def test_search_shows_only_matching_sections_and_jumps_to_the_setting(dialog, app):
    tabs = dialog.tabs
    tabs._search.setText('SearchAPI')
    tabs._search_timer.stop()
    tabs._apply_search()
    visible = [i for i, button in enumerate(tabs._buttons) if not button.isHidden()]
    assert 6 in visible and len(visible) < 10                               # «API-сервисы» (NEO 184: after «Перевод») and a few others
    tabs._jump_to_first_hit()
    settle(app)
    assert tabs._stack.currentIndex() in visible
    assert tabs._hit_widget is not None and tabs._hit_widget.property('searchHit') is True
    tabs._search.setText('')
    tabs._apply_search()
    # NEO 187 HREQ-007: outside a search the menu shows categories; the pages of
    # the chosen category are tabs at the top.
    assert all(button.isHidden() for button in tabs._buttons)
    assert all(not header.isHidden() for header, _ids in tabs._group_headers186)
    assert not tabs._subtabs187.isHidden() and tabs._subtabs187.count() >= 1


def test_enter_in_search_does_not_close_settings(dialog, app):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    dialog.tabs._search.setFocus()
    dialog.tabs._search.setText('Gmail')
    QTest.keyClick(dialog.tabs._search, Qt.Key_Return)
    settle(app)
    assert dialog.isVisible() and dialog.result() == 0


def test_search_with_no_match_says_so(dialog):
    tabs = dialog.tabs
    tabs._search.setText('zzqqxx-нет-такого')
    tabs._apply_search()
    assert not tabs._no_results.isHidden() and all(button.isHidden() for button in tabs._buttons)


def test_long_labels_wrap_instead_of_widening_the_page(dialog):
    from PySide6.QtWidgets import QFormLayout
    forms = dialog.findChildren(QFormLayout)
    assert forms and all(form.rowWrapPolicy() == QFormLayout.WrapLongRows for form in forms)


def test_new_texts_are_translated(config, app, monkeypatch):
    from jarvis.i18n import tr
    subtitles = [text for text in __import__('jarvis.ui.settings_dialog', fromlist=['x']).ScrollableSettingsTabs.SUBTITLES if text]  # the Voice page has none
    for source in ('Найти настройку…', 'Ничего не найдено', *subtitles):
        for code in ('en', 'de', 'fr', 'es', 'it', 'pt'):
            assert tr(source, code) != source, (source, code)
