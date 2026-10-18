"""Real Settings controls: locale coverage, enum identity and live switching."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import pytest
from PySide6.QtWidgets import QApplication
from jarvis.config import ConfigManager
from jarvis.i18n import translate_widget_tree
from jarvis.ui.qt_localization import install_ui_language
from jarvis.ui.settings_dialog import SettingsDialog
from tools.inventory_settings_language import collect_widgets, findings


@pytest.fixture(scope="module")
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def make_dialog(tmp_path, monkeypatch, app):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(SettingsDialog, "_refresh_microphones_async", lambda self: None)
    dialogs = []
    def make(code):
        config = ConfigManager(Path(__file__).parents[1] / "config/default_settings.json")
        config.data["app"]["language"] = code
        install_ui_language(app, code)
        dialog = SettingsDialog(config)
        dialogs.append(dialog)
        app.processEvents()
        return dialog
    yield make
    for dialog in dialogs:
        dialog.close()
        dialog.deleteLater()
    app.processEvents()


@pytest.mark.parametrize("code", ("ru", "en", "de", "fr", "es", "it", "pt"))
def test_constructed_settings_have_no_generic_help_or_foreign_russian(code, make_dialog):
    dialog = make_dialog(code)
    issues = findings(collect_widgets(dialog), code)
    assert issues["generic_fallbacks"] == []
    assert issues["cyrillic_leaks"] == []
    assert dialog.keep_alive.currentData() == "0"
    assert not dialog.keep_alive.isEnabled()
    assert dialog.shelf_layout_editable.isChecked()


def test_live_language_changes_fixed_options_without_mutating_saved_values(make_dialog):
    dialog = make_dialog("ru")
    choices = [dialog.robot_active_animation, dialog.timer_overlay_style, dialog.google_calendar_briefing_period,
               dialog.openweather_units, dialog.gmail_notification_detail]
    before = [[combo.itemData(i) for i in range(combo.count())] for combo in choices]
    dialog.ollama_url.setText("http://localhost:11434/Мир")
    translate_widget_tree(dialog, "de")
    assert dialog.robot_active_animation.itemText(1) == "Regen"
    assert dialog.timer_overlay_style.itemText(0) == "Glas"
    translate_widget_tree(dialog, "ru")
    assert dialog.robot_active_animation.itemText(1) == "Дождь"
    assert dialog.timer_overlay_style.itemText(0) == "Стекло"
    assert [[combo.itemData(i) for i in range(combo.count())] for combo in choices] == before
    assert dialog.ollama_url.text() == "http://localhost:11434/Мир"


def test_dynamic_settings_status_keeps_payload_during_live_language_change(make_dialog):
    from jarvis.ui.settings_dialog import _localize_owner
    dialog = make_dialog("en")
    payload = "403: файл Стиль.txt {literal}"
    dialog._status_text(dialog.spotify_status, "Ошибка Spotify: {error}", error=payload)
    assert dialog.spotify_status.text() == "Spotify error: " + payload
    translate_widget_tree(dialog, "de")
    assert dialog.spotify_status.text() == "Spotify-Fehler: " + payload
    assert _localize_owner(dialog, "Сначала авторизуйте Calendar.") == "Authorize Calendar first."
    assert _localize_owner(dialog, "Неизвестная ошибка поставщика XYZ: 403 файл Стиль.txt") == "Неизвестная ошибка поставщика XYZ: 403 файл Стиль.txt"
