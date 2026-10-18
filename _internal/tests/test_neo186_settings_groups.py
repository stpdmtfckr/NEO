"""186: settings window in six groups (settings TZ sections 2, 4, 5.1, 5.15; acceptance A01/A02/A09)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(app, tmp_path, monkeypatch):
    from jarvis.config import ConfigManager
    from jarvis.ui.settings_dialog import SettingsDialog
    for name in ("HOME", "LOCALAPPDATA", "APPDATA", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setattr(SettingsDialog, "_refresh_microphones_async", lambda *_: None)
    monkeypatch.setattr(SettingsDialog, "_start_game_cache_stats158", lambda *_: None)
    config = ConfigManager(ROOT / "config/default_settings.json")
    config.data["app"]["language"] = "ru"
    d = SettingsDialog(config)
    assert d._settings_ready160 and not d._settings_build_error160
    yield d
    d.reject()
    d.deleteLater()


def _menu(tabs):
    """NEO 187 HREQ-007: categories on the left, each with its pages as top tabs."""
    from PySide6.QtWidgets import QPushButton
    rows = []
    layout = tabs._tab_layout
    for i in range(layout.count()):
        widget = layout.itemAt(i).widget()
        if isinstance(widget, QPushButton) and widget.objectName() == "settingsNavGroup":
            rows.append(("group", widget.text()))
    for header, ids in tabs._group_headers186:
        header.click()
        bar = tabs._subtabs187
        rows.extend(("page", bar.tabText(i)) for i in range(bar.count()))
        assert [tabs._buttons[i].text() for i in ids] == [bar.tabText(i) for i in range(bar.count())]
    return rows


def test_pages_in_categories_with_top_tabs_open_directly(dialog):
    rows = _menu(dialog.tabs)
    groups = [text for kind, text in rows if kind == "group"]
    assert groups == ["Ассистент", "Внешний вид", "Медиа и игры", "Стриминг", "Подключения и безопасность", "Команды", "Система"]
    pages = [text for kind, text in rows if kind == "page"]
    assert len(pages) == 17 and len(set(pages)) == 17
    assert pages[:4] == ["Микрофон и wake word", "Голос и ответы", "Перевод", "Общение и память"]
    assert pages[-2:] == ["Дополнительно", "Режим разработчика"]
    assert all(button.isHidden() for button in dialog.tabs._buttons)   # pages are not in the left menu
    for page_id in dialog._settings_page_ids186:                 # every page directly by its stable ID
        assert dialog.open_page(page_id)
        assert dialog.tabs._stack.currentIndex() == dialog.page_index186(page_id)
        bar = dialog.tabs._subtabs187
        assert bar.tabText(bar.currentIndex()) == dialog.tabs._buttons[dialog.page_index186(page_id)].text()


def test_top_tab_click_switches_the_page_and_the_category_remembers_it(dialog):
    tabs = dialog.tabs
    dialog.open_page("translation")
    tabs._subtabs187.setCurrentIndex(3)
    assert tabs._stack.currentIndex() == dialog.page_index186("conversation")
    dialog.open_page("robot")
    assistant = tabs._group_headers186[0][0]
    assistant.click()
    assert tabs._stack.currentIndex() == dialog.page_index186("conversation") and assistant.isChecked()


def test_dictionary_is_on_the_microphone_page_and_profile_in_advanced(dialog):
    stack = dialog.tabs._stack
    microphone = stack.widget(dialog.page_index186("microphone"))
    advanced = stack.widget(dialog.page_index186("advanced"))
    window = stack.widget(dialog.page_index186("window"))
    conversation = stack.widget(dialog.page_index186("conversation"))
    assert microphone.isAncestorOf(dialog.dictionary_table) and microphone.isAncestorOf(dialog.personal_dictionary_enabled)
    assert advanced.isAncestorOf(dialog.performance_profile) and not window.isAncestorOf(dialog.performance_profile)
    from PySide6.QtWidgets import QGroupBox

    def block(widget, page):
        node = widget
        top = None
        while node is not None and node is not page:
            if isinstance(node, QGroupBox):
                top = node
            node = node.parentWidget()
        return top

    def order(widget):
        top = block(widget, advanced)
        return top.parentWidget().layout().indexOf(top)
    # top order of «Дополнительно»: language, then startup (+profile)
    assert order(dialog.app_language) < order(dialog.autostart)
    assert block(dialog.autostart, advanced) is block(dialog.performance_profile, advanced)
    # NEO 187 HREQ-007: conversation and memory moved to «Ассистент → Общение и память»
    assert conversation.isAncestorOf(dialog.local_chat_enabled169) and not advanced.isAncestorOf(dialog.local_chat_enabled169)
    assert block(dialog.dialogue_memory_enabled, conversation) is block(dialog.local_chat_enabled169, conversation)


def test_regrouped_values_still_save(dialog):
    index = dialog.performance_profile.findData("fast")
    dialog.performance_profile.setCurrentIndex(index)
    dialog._save(close=False)
    assert dialog.config.get("performance.profile") == "fast"
    assert dialog.config.get("personal_dictionary.enabled") == dialog.personal_dictionary_enabled.isChecked()


def test_search_shows_path_and_understands_synonyms(dialog):
    tabs = dialog.tabs
    tabs._search.setText("stt")
    tabs._apply_search()
    assert not tabs._buttons[dialog.page_index186("microphone")].isHidden()
    tabs._search.setText("озвучка")
    tabs._apply_search()
    assert not tabs._buttons[dialog.page_index186("voice")].isHidden()
    tabs._search.setText("Профиль производительности")
    tabs._jump_to_first_hit()
    assert tabs._stack.currentIndex() == dialog.page_index186("advanced")
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.processEvents()
    assert tabs._search_path186.text().startswith("Система → Дополнительно")
    tabs._search.setText("")
    tabs._apply_search()
    assert tabs._search_path186.isHidden() and all(not h.isHidden() for h, _ in tabs._group_headers186)
    assert all(button.isHidden() for button in tabs._buttons)   # NEO 187: back to categories + top tabs
