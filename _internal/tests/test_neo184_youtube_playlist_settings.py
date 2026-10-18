"""Named YouTube settings are transactional, localizable and offline-only in QA."""
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
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from jarvis.config import ConfigManager
from jarvis.testing.isolation import external_io_blocked

URL_A = "https://www.youtube.com/playlist?list=PL_work_123"
URL_B = "https://www.youtube.com/playlist?list=PL_evening_456"
KEY = "integrations.youtube.music_playlists"
LANGUAGES = ("ru", "en", "de", "fr", "es", "it", "pt")
ERRORS = (
    "Укажите название каждого плейлиста.",
    "Названия плейлистов должны быть уникальными.",
    "Укажите корректную ссылку YouTube с параметром list.",
    "Этот плейлист YouTube уже добавлен.",
    "Название плейлиста должно содержать не более 100 символов.",
    "Некорректный список плейлистов YouTube.",
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    # Match test_neo184_settings.py: never touch the real profile or launch apps.
    for name in ("HOME", "LOCALAPPDATA", "APPDATA", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setattr(ConfigManager, "apply_wake_service", lambda *_: None)
    monkeypatch.setattr(ConfigManager, "apply_autostart", lambda *_: None)
    with external_io_blocked(), patch.object(socket.socket, "sendto", side_effect=PermissionError("QA blocks datagrams")), patch.object(os, "system", side_effect=PermissionError("QA blocks shell")):
        yield


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def make_dialog(app, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from jarvis.ui.settings_dialog import SettingsDialog, QMessageBox
    from jarvis.ui.qt_localization import install_ui_language

    # Fake device/cache discovery adapters; no probes, playback or credentials.
    monkeypatch.setattr(SettingsDialog, "_refresh_microphones_async", lambda *_: None)
    monkeypatch.setattr(SettingsDialog, "_start_game_cache_stats158", lambda *_: None)
    dialogs = []
    def create(config=None):
        c = config or ConfigManager(ROOT / "config/default_settings.json")
        if config is None:
            c.data["app"]["language"] = "ru"
        install_ui_language(app, c.get("app.language"))
        warnings, errors = [], []
        monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args))
        monkeypatch.setattr(QMessageBox, "critical", lambda *args: errors.append(args))
        d = SettingsDialog(c)
        assert d._settings_ready160 and not d._settings_build_error160
        d._playlist_test_warnings = warnings
        d._playlist_test_errors = errors
        dialogs.append(d)
        return d
    yield create
    for dialog in dialogs:
        dialog.reject()
        dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def add_row(dialog, name="", url=""):
    dialog.youtube_music_add_button.click()
    row = dialog._youtube_music_rows184[-1]
    row["name"].setText(name)
    row["url"].setText(url)
    return row


def assert_no_dialog_errors(dialog):
    assert not dialog._playlist_test_warnings
    assert not dialog._playlist_test_errors


def test_group_is_separate_on_media_and_works_without_api_key(make_dialog):
    from PySide6.QtWidgets import QGroupBox
    d = make_dialog()
    page = next(page for page in (d.tabs._stack.widget(i) for i in range(d.tabs._stack.count()))
                if page.isAncestorOf(d.youtube_music_group))
    assert d._settings_pages160[d.tabs._stack.indexOf(page)][1] == "Медиа"
    assert isinstance(d.youtube_music_group, QGroupBox)
    assert d.youtube_music_group.title() == "Музыка YouTube"
    assert not d.youtube_music_group.isAncestorOf(d.youtube_api_key)
    assert not d._existing_youtube_key_encrypted
    assert d.youtube_api_key.text() == ""
    assert "API key не нужен" in d.youtube_music_help.text()
    assert "Включи плейлист Для работы" in d.youtube_music_help.text()
    assert d._youtube_music_playlist_values184() == []


def test_add_edit_delete_apply_restart_and_save_empty(make_dialog):
    d = make_dialog()
    first = add_row(d, " Work ", "https://youtu.be/video123?list=PL_work_123&t=9")
    second = add_row(d, "Evening", URL_B)
    first["name"].setText("  Для   работы  ")
    second["delete"].click()
    assert len(d._youtube_music_rows184) == 1
    assert first["name"].text() == "  Для   работы  "
    d.apply_button.click()
    assert_no_dialog_errors(d)
    assert not d._settings_closed160
    expected = [{"name": "Для работы", "url": URL_A}]
    assert d.config.get(KEY) == expected
    assert not d.config.get("integrations.youtube.api_key_encrypted")
    restarted_config = ConfigManager(ROOT / "config/default_settings.json")
    assert restarted_config.get(KEY) == expected
    reopened = make_dialog(restarted_config)
    assert reopened._youtube_music_playlist_values184() == expected
    reopened._youtube_music_rows184[0]["name"].setText("Updated")
    reopened.apply_button.click()
    assert_no_dialog_errors(reopened)
    assert ConfigManager(ROOT / "config/default_settings.json").get(KEY)[0]["name"] == "Updated"
    reopened._youtube_music_rows184[0]["delete"].click()
    reopened.save_button.click()
    assert_no_dialog_errors(reopened)
    assert reopened._settings_closed160
    assert ConfigManager(ROOT / "config/default_settings.json").get(KEY) == []


def test_cancel_discards_add_edit_and_delete(make_dialog):
    c = ConfigManager(ROOT / "config/default_settings.json")
    c.data["integrations"]["youtube"]["music_playlists"] = [
        {"name": "Work", "url": URL_A}, {"name": "Evening", "url": URL_B}]
    c.save(c.data)
    d = make_dialog(c)
    original_data = copy.deepcopy(c.data)
    original_file = c.paths.settings.read_bytes()
    d._youtube_music_rows184[0]["name"].setText("Unsaved edit")
    d._youtube_music_rows184[0]["url"].setText("https://invalid.example")
    d._youtube_music_rows184[1]["delete"].click()
    add_row(d, "Unsaved addition", URL_B)
    d.cancel_button.click()
    assert d._settings_closed160
    assert c.data == original_data
    assert c.paths.settings.read_bytes() == original_file
    restarted = ConfigManager(ROOT / "config/default_settings.json")
    assert restarted.get(KEY) == original_data["integrations"]["youtube"]["music_playlists"]


@pytest.mark.parametrize("rows,error", [
    ([{"name": "", "url": URL_A}], ERRORS[0]),
    ([{"name": " \t ", "url": URL_A}], ERRORS[0]),
    ([{"name": "Work", "url": URL_A}, {"name": " work ", "url": URL_B}], ERRORS[1]),
    ([{"name": "Café mix", "url": URL_A}, {"name": "  CAFE\u0301  MIX ", "url": URL_B}], ERRORS[1]),
    ([{"name": "Work", "url": ""}], ERRORS[2]),
    ([{"name": "Work", "url": "https://www.youtube.com/watch?v=video123"}], ERRORS[2]),
    ([{"name": "Work", "url": "https://youtube.com.evil.example/playlist?list=PL_work_123"}], ERRORS[2]),
    ([{"name": "Work", "url": "javascript:alert(1)"}], ERRORS[2]),
    ([{"name": "Work", "url": URL_A}, {"name": "Other", "url": "https://youtube.com/watch?v=video123&list=PL_work_123"}], ERRORS[3]),
    ([{"name": "x" * 101, "url": URL_A}], ERRORS[4]),
])
def test_invalid_rows_block_all_persistence(make_dialog, monkeypatch, rows, error):
    d = make_dialog()
    original_data = copy.deepcopy(d.config.data)
    original_file = d.config.paths.settings.read_bytes()
    for row in rows:
        add_row(d, **row)
    d.user_name.setText("Must remain staged")
    saved = []
    monkeypatch.setattr(d.config, "save", lambda *args, **kwargs: saved.append(args))
    d.apply_button.click()
    d.save_button.click()
    assert not saved
    assert not d._settings_closed160
    assert not d._playlist_test_errors
    assert [warning[2] for warning in d._playlist_test_warnings] == [error, error]
    assert d.config.data == original_data
    assert d.config.paths.settings.read_bytes() == original_file


def test_broken_saved_rows_are_visible_and_repairable(make_dialog):
    c = ConfigManager(ROOT / "config/default_settings.json")
    c.data["app"]["language"] = "ru"
    c.data["integrations"]["youtube"]["music_playlists"] = [
        {"name": "Existing", "url": "broken link"}, "broken second row"]
    d = make_dialog(c)
    assert d._youtube_music_playlist_values184() == [
        {"name": "Existing", "url": "broken link"}, {"name": "", "url": "broken second row"}]
    d.apply_button.click()
    assert d._playlist_test_warnings[-1][2] == ERRORS[2]
    d._youtube_music_rows184[0]["url"].setText(URL_A)
    d._youtube_music_rows184[1]["delete"].click()
    d._playlist_test_warnings.clear()
    d.apply_button.click()
    assert_no_dialog_errors(d)
    assert c.get(KEY) == [{"name": "Existing", "url": URL_A}]


def test_live_locales_and_late_rows_preserve_user_text(make_dialog):
    from jarvis.i18n import tr
    d = make_dialog()
    original_name = "Удалить Café Работа"
    row = add_row(d, original_name, URL_A)
    for code in LANGUAGES:
        d.app_language.setCurrentIndex(d.app_language.findData(code))
        # Merely selecting a language stages it; Apply updates all current rows.
        d.apply_button.click()
        assert_no_dialog_errors(d)
        assert d.youtube_music_group.title() == tr("Музыка YouTube", code)
        assert d.youtube_music_add_button.text() == tr("Добавить плейлист", code)
        assert row["delete"].text() == tr("Удалить", code)
        assert row["name"].placeholderText() == tr("Название плейлиста", code)
        assert row["name"].accessibleName() == tr("Название плейлиста", code)
        assert row["url"].accessibleName() == tr("Ссылка на плейлист", code)
        assert row["name"].text() == original_name
        assert row["url"].text() == URL_A
        late = add_row(d, "Untranslated user title", URL_B)
        assert late["delete"].text() == tr("Удалить", code)
        assert late["name"].placeholderText() == tr("Название плейлиста", code)
        assert late["name"].text() == "Untranslated user title"
        late["name"].clear()
        d.apply_button.click()
        assert d._playlist_test_warnings[-1][1:] == (tr("Музыка YouTube", code), tr(ERRORS[0], code))
        d._playlist_test_warnings.clear()
        late["delete"].click()


def test_all_seven_catalogs_have_full_playlist_chrome_and_feedback():
    keys = ("Музыка YouTube", "Добавить плейлист", "Название плейлиста", "Ссылка на плейлист", *ERRORS,
            "Плейлист YouTube не найден. Добавьте его в настройках → Медиа → Музыка YouTube.",
            "Исправьте плейлисты YouTube в настройках → Медиа → Музыка YouTube.",
            "Не удалось подтвердить воспроизведение плейлиста YouTube.",
            "Не удалось включить плейлист YouTube.",
            "Не удалось определить плеер. Укажите Discord или локальное воспроизведение.",
            "Включил плейлист YouTube «{name}».",
            "Включил плейлист YouTube «{name}» в Discord.")
    for code in LANGUAGES:
        catalog = json.loads((ROOT / "jarvis/locales/ui" / (code + ".json")).read_text(encoding="utf-8"))["strings"]
        for key in keys:
            assert key in catalog
            assert catalog[key].strip()
            if code != "ru":
                assert not any("\u0400" <= char <= "\u04ff" for char in catalog[key])
