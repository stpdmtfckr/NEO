"""Behavior checks for durable objects, resource limits and GUI integration.

Qt tests run with QT_QPA_PLATFORM=offscreen; Windows foreground/hit-testing are
separate acceptance checks because an offscreen renderer cannot verify them.
"""
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

from jarvis.ui.shelf_store import ShelfStore, VersionConflict
from jarvis.ui.gif_assets import inspect_gif, import_gif


class Config:
    def __init__(self, root):
        self.paths = SimpleNamespace(root=root)
        self.data = {"app": {"language": "en"}, "scenarios": {"items": [{"id": "stable", "name": "Work", "steps": []}]}}

    def get(self, key, default=None):
        item = self.data
        for part in key.split("."):
            if not isinstance(item, dict) or part not in item:
                return default
            item = item[part]
        return item

    def save(self, data):
        self.data = data


def test_shelf_durable_versions_geometry_and_conflict(tmp_path):
    store = ShelfStore(tmp_path)
    original = store.create("checklist", title="Shopping", items=[{"text": "Milk", "checked": False}])
    update = store.update(original["id"], 1, items=[{"text": "Milk", "checked": True}], pinned=True, position=[-400, 220], shelf="Work", order=3)
    restarted = ShelfStore(tmp_path)
    assert restarted.get(original["id"]) == update
    assert restarted.list(shelf="Work")[0]["items"][0]["checked"]
    with pytest.raises(VersionConflict):
        restarted.update(original["id"], 1, content="stale remote edit")
    assert restarted.history(original["id"])[-1]["items"][0]["checked"] is False


def test_shelf_delete_never_deletes_external_or_managed_file(tmp_path):
    source = tmp_path / "document.txt"
    source.write_text("keep me", encoding="utf-8")
    store = ShelfStore(tmp_path / "profile")
    for managed in (False, True):
        item = store.import_file(source, managed=managed)
        if managed:
            assert item["path"] != str(source)
        store.delete(item["id"], 1)
        assert source.read_text() == "keep me"
        assert Path(item["path"]).exists()
    assert store.list() == []


def test_shelf_rejects_unknown_and_overlarge_mutations(tmp_path):
    store = ShelfStore(tmp_path)
    item = store.create("note", title="Hello")
    with pytest.raises(ValueError):
        store.update(item["id"], 1, path="/unapproved")
    with pytest.raises(ValueError):
        store.update(item["id"], 1, content="a" * (store.MAX_TEXT + 1))
    assert store.get(item["id"])["version"] == 1


def gif_bytes():
    from PIL import Image
    stream = io.BytesIO()
    frames = [Image.new("RGB", (24, 16), color) for color in ("red", "blue")]
    frames[0].save(stream, format="GIF", save_all=True, append_images=frames[1:], duration=10, loop=0)
    return stream.getvalue()


def test_gif_import_is_managed_and_validates_budgets(tmp_path):
    source = tmp_path / "original.gif"
    source.write_bytes(gif_bytes())
    managed, info = import_gif(source, tmp_path / "profile")
    assert info["frames"] == 2
    assert info["min_delay_ms"] == 10
    source.unlink()
    assert Path(managed).exists()
    assert inspect_gif(Path(managed).read_bytes())["width"] == 24
    with pytest.raises(ValueError):
        inspect_gif(b"GIF89a" + b"\xff" * 30)
    with pytest.raises(ValueError):
        inspect_gif(gif_bytes()[:-5])


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_shelf_editor_conflict_clipboard_and_mask(tmp_path, qapp):
    from jarvis.ui.bookshelf import Bookshelf, object_mime
    from PySide6.QtCore import Qt
    cfg = Config(tmp_path)
    shelf = Bookshelf(cfg, lambda _id: None, lambda: None)
    item = shelf.store.create("note", title="Editable", content="before")
    shelf.show()
    qapp.processEvents()
    shelf.open_object(item["id"])
    editor = shelf.editors[item["id"]]
    editor.content.setPlainText("local edit")
    shelf.store.update(item["id"], 1, content="remote edit")
    assert not editor.save()
    assert editor.content.toPlainText() == "local edit"
    editor.resize(300, 260)
    editor.scale_text()
    small_style = editor.content.styleSheet()
    editor.resize(700, 500)
    editor.scale_text()
    assert editor.content.styleSheet() != small_style
    assert "12px" not in editor.content.styleSheet()
    assert shelf.store.get(item["id"])["content"] == "remote edit"
    assert not shelf.mask().isEmpty()
    assert shelf.mask().boundingRect().width() <= shelf.width()
    path = tmp_path / "copy.txt"
    path.write_text("copy")
    file_obj = shelf.store.import_file(path)
    mime = object_mime([file_obj])
    assert mime.hasUrls() and Path(mime.urls()[0].toLocalFile()).resolve() == path.resolve()
    editor.hide()
    editor.deleteLater()
    shelf.editors.clear()
    shelf.hide()
    shelf.deleteLater()
    qapp.processEvents()


def test_robot_preserves_visible_animation_and_stops_hidden(tmp_path, qapp):
    from PySide6.QtGui import QMovie
    from jarvis.ui.robot_widget import RobotWidget
    robot = RobotWidget()
    robot.resize(420, 400)
    robot.show()
    robot.set_state("working")
    qapp.processEvents()
    assert robot._timer.isActive()
    assert robot._timer.interval() == 16
    robot.hide()
    qapp.processEvents()
    assert not robot._timer.isActive()
    source = tmp_path / "state.gif"
    source.write_bytes(gif_bytes())
    robot.configure_gifs(True, {"working": str(source)}, 100)
    robot.show()
    qapp.processEvents()
    assert robot._gif_movie is not None
    assert not robot._timer.isActive()
    robot.hide()
    assert robot._gif_movie.state() == QMovie.Paused
    robot.set_state("thinking")  # No asset -> built-in vector animation.
    robot.show()
    qapp.processEvents()
    assert robot._gif_movie is None and robot._timer.isActive()
    robot.hide()
    robot.deleteLater()


def test_new_ui_text_complete_in_all_seven_languages():
    from jarvis.ui.desktop_strings import _ROWS, LANGS, dt
    for key, row in _ROWS.items():
        assert len(row) == len(LANGS)
        assert all(dt(key, lang) for lang in LANGS)


def test_browser_rejects_local_and_script_protocols():
    from jarvis.ui.mini_windows import safe_browser_url
    for value in ("file:///secret.txt", "javascript:alert(1)", "data:text/html,test"):
        with pytest.raises(ValueError):
            safe_browser_url(value)
    assert safe_browser_url("example.com").scheme() == "https"
    assert "q=" in safe_browser_url("simple query").toString()


def test_settings_preserves_scenario_and_provider_on_language_change(tmp_path, qapp, monkeypatch):
    from copy import deepcopy
    from jarvis.config import ConfigManager
    from jarvis.ui.settings_dialog import SettingsDialog, QMessageBox
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(SettingsDialog, "_refresh_microphones_async", lambda _self: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **kw: None)
    failures = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **kw: failures.append(str(a)))
    config = ConfigManager(Path(__file__).parents[1] / "config" / "default_settings.json")
    original = {"id": "stable-id", "name": "Work", "phrase": "work mode", "aliases": ["office"], "steps": [{"type": "open", "path": "editor.exe", "required": True}, {"type": "wait", "seconds": 1}, {"type": "command", "command": ["echo", "hello"]}]}
    config.data.setdefault("scenarios", {})["items"] = [deepcopy(original)]
    config.data.setdefault("integrations", {}).setdefault("yandex_music", {})["enabled"] = True
    config.data.setdefault("integrations", {}).setdefault("spotify", {})["enabled"] = False
    config.data.setdefault("dialogue", {})["style"] = "business"
    config.save(config.data)
    dialog = SettingsDialog(config)
    qapp.processEvents()
    dialog.app_language.setCurrentIndex(dialog.app_language.findData("de"))
    dialog.yandex_integration_enabled.setChecked(True)
    dialog.desktop_service_toggles["smart_downloads_enabled"].setChecked(True)
    dialog.robot_headphones_enabled.setChecked(False)
    assert not hasattr(dialog, "dialogue_style")  # selector retired; saved legacy behavior is preserved
    dialog._save(close=False)
    assert not failures
    assert config.get("integrations.yandex_music.enabled")
    assert config.get("app.language") == "de"
    assert config.get("security.smart_downloads_enabled")
    assert not config.get("ui.robot_headphones_enabled")
    assert config.get("dialogue.style") == "business"
    saved = config.get("scenarios.items")[0]
    assert saved["id"] == original["id"]
    assert saved["aliases"] == original["aliases"]
    assert saved["steps"][1:] == original["steps"][1:]
    assert saved["steps"][0]["required"]
    dialog.hide()
    dialog.deleteLater()
    qapp.processEvents()


def test_main_desktop_events_hotkey_and_settings_section(tmp_path, qapp, monkeypatch):
    from PySide6.QtCore import QTimer
    from jarvis.config import ConfigManager
    from jarvis.ui import main_window
    from jarvis.ui.settings_dialog import SettingsDialog
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(QTimer, "singleShot", lambda *args: None)
    monkeypatch.setattr(main_window.MainWindow, "_resume_wake_listening", lambda *args, **kwargs: None)
    monkeypatch.setattr(main_window.clipboard_history(), "start_monitor", lambda: None)
    monkeypatch.setattr(SettingsDialog, "_refresh_microphones_async", lambda _self: None)
    config = ConfigManager(Path(__file__).parents[1] / "config" / "default_settings.json")
    window = main_window.MainWindow(config)
    for timer in window.findChildren(QTimer): timer.stop()
    window._set_runtime_topmost(False, hide=True)
    assert not window.isVisible()
    assert not window._quitting
    window._set_runtime_topmost(True)
    assert window.isVisible()
    window._handle_desktop_event({"action": "shelf.show"})
    assert window.bookshelf.isVisible()
    window._handle_desktop_event({"action": "shelf.hide"})
    assert not window.bookshelf.isVisible()
    window._handle_desktop_event({"action": "settings.open", "section": "shelf"})
    assert window._settings_dialog.tabs._stack.currentIndex() == 7
    restarts = []
    monkeypatch.setattr(window.audio_capture, "restart_backend", lambda: restarts.append(True))
    monkeypatch.setattr(window, "_prewarm_discord_async", lambda: None)
    config.data["ui"]["robot_headphones_color"] = "#FF9000"
    config.save(config.data)
    window._handle_desktop_event({"action": "settings.apply"})
    assert not restarts, "Changing an appearance setting must not restart audio"
    window._settings_dialog.hide()
    window.bookshelf.shutdown()
    window.audio_capture.close()
    window._quitting = True
    window.close()
    window.deleteLater()
    qapp.processEvents()


def test_shelf_fold_keeps_only_pinned_and_moves_objects_between_shelves(tmp_path, qapp):
    from jarvis.ui.bookshelf import Bookshelf
    config = Config(tmp_path)
    config.data["shelf"] = {"restore_pinned": False, "names": ["Work"]}
    store = ShelfStore(tmp_path)
    asset = tmp_path / "asset.txt"
    asset.write_text("leave original")
    pinned_file = store.import_file(asset, pinned=True)
    shelf = Bookshelf(config, lambda _id: None, lambda: None)
    assert not shelf.pinned_cards, "Restore-pinned off must also apply to files"
    plain = store.create("note", title="Plain", content="before")
    pinned = store.create("note", title="Pinned", pinned=True)
    shelf.show()
    shelf.open_object(plain["id"])
    shelf.open_object(pinned["id"])
    qapp.processEvents()
    shelf.editors[plain["id"]].content.setPlainText("saved on fold")
    shelf.hide()
    assert not shelf.editors[plain["id"]].isVisible()
    assert shelf.editors[pinned["id"]].isVisible()
    assert shelf.pinned_cards[pinned_file["id"]].isVisible()
    assert store.get(plain["id"])["content"] == "saved on fold"
    shelf.move_ids([plain["id"], pinned_file["id"]], "Work")
    assert {o["id"] for o in store.list(shelf="Work")} == {plain["id"], pinned_file["id"]}
    assert asset.read_text() == "leave original"
    shelf.shutdown()
    for widget in [*shelf.editors.values(), *shelf.pinned_cards.values(), shelf]:
        widget.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize("action,observed,expected_action", [("pause", "playing", "music_pause"), ("resume", "paused", "music_resume")])
def test_quick_media_cached_owner_reconciles_other_active_without_probing(monkeypatch, action, observed, expected_action):
    from jarvis.ui import main_window
    from jarvis.quick_media_commands import QuickMediaCommand
    from jarvis.runtime import media_activity
    calls = []

    def execute(tool, args):
        calls.append((tool, args["action"]))
        assert "status" not in args["action"], "Cached transport must not probe inactive providers"
        return SimpleNamespace(success=True, data={})

    monkeypatch.setattr(media_activity, "_OWNERS", {})
    media_activity.note_media_observation("discord", playing=observed == "playing", paused=observed == "paused")
    # A loaded but inactive Spotify track must never be started by bare resume.
    media_activity._set("spotify", "present")
    fake = SimpleNamespace(agent=SimpleNamespace(tools=SimpleNamespace(execute=execute)),
                           _language_code=lambda: "en",
                           _cached_quick_media_owner=lambda: "yandex_music",
                           _quick_media_server=lambda: "known-server")
    fake._quick_media_statuses = lambda **kwargs: pytest.fail("No blocking status probes after a successful cached command")
    result = main_window.MainWindow._execute_quick_media_command(fake, QuickMediaCommand(action, "auto", action))
    assert result["ok"]
    assert calls == [("yandex_music", "pause" if action == "pause" else "play"), ("discord", expected_action)]


def test_passive_media_snapshot_expires_and_stop_clears_only_owner(monkeypatch):
    from jarvis.runtime import media_activity
    monkeypatch.setattr(media_activity, "_OWNERS", {})
    clock = [10.0]
    monkeypatch.setattr(media_activity.time, "monotonic", lambda: clock[0])
    media_activity.note_media_observation("discord", playing=True)
    media_activity.note_media_observation("spotify", playing=False, paused=True)
    media_activity.note_media_observation("discord", playing=False)
    assert media_activity.recent_music_states() == {"spotify": "paused"}
    clock[0] = 45.0
    assert media_activity.recent_music_states() == {}


def test_download_correction_edits_same_bubble_and_acknowledges_revision(qapp, monkeypatch):
    from collections import deque
    from jarvis.ui.main_window import MainWindow
    from jarvis.ui.chat_view import ChatView
    from jarvis.runtime import desktop_services
    monkeypatch.setattr(desktop_services, "event_text", lambda event, language: event["data"]["text"])
    acknowledged = []
    chat = ChatView()
    fake = SimpleNamespace(chat=chat, _language_code=lambda: "en", _seen_desktop_events=set(),
                           _seen_desktop_event_order=deque(maxlen=2), _desktop_event_bubbles={},
                           desktop_services=SimpleNamespace(store=SimpleNamespace(acknowledge=lambda eid, revision=None: acknowledged.append((eid, revision)))))
    original = {"type": "notification", "event_id": "download:1:terminal", "revision": 0, "data": {"text": "Completion inferred"}}
    MainWindow._desktop_service_event(fake, original)
    bubble = fake._desktop_event_bubbles[original["event_id"]]["bubble"]
    correction = {**original, "revision": 1, "correction": True, "data": {"text": "Download cancelled (browser confirmed)"}}
    MainWindow._desktop_service_event(fake, correction)
    assert len(chat.items) == 1 and chat.items[0].text == correction["data"]["text"]
    assert fake._desktop_event_bubbles[original["event_id"]]["bubble"] is bubble
    MainWindow._desktop_service_event(fake, {**correction, "data": {"text": "Same revision must not overwrite"}})
    assert chat.items[0].text == correction["data"]["text"]
    # A new revision can race the first ACK and still have correction=False.
    revised = {**original, "revision": 2, "data": {"text": "Corrected provider detail"}}
    MainWindow._desktop_service_event(fake, revised)
    assert len(chat.items) == 1 and chat.items[0].text == revised["data"]["text"]
    chat.clear_messages()
    MainWindow._desktop_service_event(fake, {**correction, "revision": 3})
    assert not chat.items
    MainWindow._desktop_service_event(fake, {**correction, "event_id": "not-in-this-session", "revision": 4})
    assert not chat.items and acknowledged[-1] == ("not-in-this-session", 4)
    chat.deleteLater()
    qapp.processEvents()


def test_image_window_controls_target_explicit_id_and_preserve_browser(tmp_path, qapp):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PIL import Image
    from jarvis.ui.main_window import MainWindow
    path = tmp_path / "synthetic.png"
    Image.new("RGB", (180, 90), "navy").save(path)
    statuses = []
    fake = SimpleNamespace(config=Config(tmp_path), _image_viewers=[], _mini_browser=None,
                           _language_code=lambda: "en", _set_status=statuses.append)
    fake._handle_desktop_event = lambda event: MainWindow._handle_desktop_event(fake, event)
    fake._handle_desktop_event({"action": "browser.open", "path": str(path), "id": "first"})
    fake._handle_desktop_event({"action": "browser.open", "path": str(path), "id": "second"})
    qapp.processEvents()
    first, second = fake._image_viewers
    fake._handle_desktop_event({"action": "browser.pin", "id": "first", "pinned": True})
    assert first.pin.isChecked() and not second.pin.isChecked()
    fake._handle_desktop_event({"action": "browser.zoom", "id": "image", "zoom": 150})
    assert first._zoom_factor == 1 and second._zoom_factor == 1.5
    # An unspecified browser target must not accidentally close an image.
    fake._handle_desktop_event({"action": "browser.close"})
    assert statuses and len(fake._image_viewers) == 2
    fake._handle_desktop_event({"action": "browser.close", "id": "first"})
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert fake._image_viewers == [second]
    fake._handle_desktop_event({"action": "browser.close", "id": "image"})
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not fake._image_viewers


@pytest.mark.parametrize("language", ("ru", "en", "de", "fr", "es", "it", "pt"))
def test_startup_optional_check_has_native_issue_heading_and_redacted_evidence(language):
    from jarvis.ui.main_window import MainWindow
    from jarvis.runtime.self_test_localization import word
    messages = []
    fake = SimpleNamespace(_language_code=lambda: language, _append_assistant=messages.append)
    payload = {"ok": True, "checks": [
        {"name": "ollama_model", "ok": True, "status": "UNAVAILABLE", "detail": "Image model is not installed: ExampleVision:4b"},
        {"name": "ollama", "ok": True, "status": "UNAVAILABLE", "detail": "Connection refused; token=should-not-appear"},
    ]}
    MainWindow._startup_self_test_done(fake, SimpleNamespace(success=True, text="Raw complete", data=payload))
    assert len(messages) == 1
    assert messages[0].startswith(word("optional", language))
    assert not messages[0].startswith(word("complete", language))
    assert "ExampleVision:4b" in messages[0]
    assert "should-not-appear" not in messages[0]
