"""Behavioral contracts for movable shelves and recoverable auto-save editors."""
import os
from pathlib import Path
from types import SimpleNamespace
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from jarvis.ui.bookshelf import Bookshelf, ObjectEditor
from jarvis.ui.shelf_store import ShelfStore


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Config:
    def __init__(self, root):
        self.paths = SimpleNamespace(root=root)
        self.data = {"app": {"language": "de"}, "shelf": {"restore_pinned": False}, "scenarios": {"items": [{"id": "stable", "name": "Arbeit", "steps": []}]}}
    def get(self, path, default=None):
        node = self.data
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node: return default
            node = node[key]
        return node
    def save(self, data): self.data = data


def test_place_cards_is_atomic_preserves_kind_content_and_versions(tmp_path):
    store = ShelfStore(tmp_path)
    one = store.create("note", content="one")
    two = store.create("checklist", items=[{"text": "two", "checked": True}])
    three = store.create("note", content="three")
    store.place([two["id"], one["id"]], shelf="Work", board="notes", index=0)
    moved = store.list(shelf="Work")
    assert [o["id"] for o in moved] == [two["id"], one["id"]]
    assert moved[0]["kind"] == "checklist" and moved[0]["items"][0]["checked"]
    assert moved[1]["content"] == "one" and moved[1]["version"] == 2
    with pytest.raises(KeyError): store.place([three["id"], "missing"], shelf="Work", board="files", index=0)
    assert store.get(three["id"])["version"] == 1
    store.place([one["id"]], shelf="Work", board="notes", index=0)
    assert store.list(shelf="Work")[0]["id"] == one["id"]


def test_section_layout_lock_preserves_card_access_and_can_be_reenabled(tmp_path, app):
    config = Config(tmp_path)
    shelf = Bookshelf(config, lambda _: None, lambda: None)
    shelf.show(); app.processEvents()
    section = shelf.sections["notes"]
    original = section.geometry()
    config.data["shelf"]["layout_editable"] = False
    shelf.apply_settings(); app.processEvents()
    assert not section.grip.isEnabled() and not section.grip.isVisible()
    assert not section.grip_size.isEnabled() and not section.grip_size.isVisible()
    QTest.mousePress(section.grip, Qt.LeftButton, pos=QPoint(5, 5))
    QTest.mouseMove(section.grip, QPoint(50, 40))
    QTest.mouseRelease(section.grip, Qt.LeftButton, pos=QPoint(50, 40))
    assert section.geometry() == original
    assert section.view.isEnabled() and section.add.isEnabled()
    config.data["shelf"]["layout_editable"] = True
    shelf.apply_settings(); app.processEvents()
    assert section.grip.isVisible() and section.grip_size.isVisible()
    assert section.grip.isEnabled() and section.grip_size.isEnabled()
    shelf.close(); shelf.deleteLater(); app.processEvents()


def test_autosave_no_save_button_close_flush_and_checklist_undo(tmp_path, app):
    store = ShelfStore(tmp_path)
    obj = store.create("note", content="before")
    editor = ObjectEditor(store, obj, "de", lambda: None)
    assert editor.windowFlags() & Qt.FramelessWindowHint
    assert not hasattr(editor, "save_button")
    editor.show(); app.processEvents()
    editor.content.setFocus(); editor.activateWindow(); app.processEvents()
    QTest.keyClicks(editor.content, "typed ")
    typed = editor.content.toPlainText()
    QTest.keyClick(editor.content, Qt.Key_Z, Qt.ControlModifier)
    assert editor.content.toPlainText() != typed
    QTest.keyClick(editor.content, Qt.Key_Y, Qt.ControlModifier)
    assert editor.content.toPlainText() == typed
    editor.content.setPlainText("saved by timer")
    # The 650 ms autosave timer must fire on its own; a fixed 720 ms wait left only
    # 70 ms of slack and failed on a busy machine, so wait for the result (up to 3 s).
    import time
    deadline = time.monotonic() + 3.0
    while store.get(obj["id"])["content"] != "saved by timer" and time.monotonic() < deadline:
        QTest.qWait(20)
    assert store.get(obj["id"])["content"] == "saved by timer"
    editor.content.setPlainText("close flush")
    editor.close()
    assert store.get(obj["id"])["content"] == "close flush"
    assert not store.get(obj["id"])["visible"]
    check = ObjectEditor(store, store.create("checklist", items=[{"text": "a", "checked": False}]), "de", lambda: None)
    check.checklist.item(0).setCheckState(Qt.Checked)
    check._undo_stack.undo(); assert check.checklist.item(0).checkState() == Qt.Unchecked
    check._undo_stack.redo(); assert check.checklist.item(0).checkState() == Qt.Checked
    check.add_item.setText("b"); check._append_item(); assert check.checklist.count() == 2
    check._undo_stack.undo(); assert check.checklist.count() == 1
    check.close(); editor.deleteLater(); check.deleteLater(); app.processEvents()


def test_conflict_draft_survives_restart_and_recovers_as_separate_copy(tmp_path, app):
    store = ShelfStore(tmp_path)
    obj = store.create("note", title="Shared", content="original")
    editor = ObjectEditor(store, obj, "en", lambda: None)
    editor.content.setPlainText("local unsaved")
    store.update(obj["id"], 1, content="remote saved")
    assert not editor.save()
    assert ShelfStore(tmp_path).draft(obj["id"])["changes"]["content"] == "local unsaved"
    disabled = Config(tmp_path); disabled.data["shelf"]["enabled"] = False
    disabled_shelf = Bookshelf(disabled, lambda _: None, lambda: None)
    assert not disabled_shelf.editors and disabled_shelf.store.draft(obj["id"])
    disabled_shelf.deleteLater()
    editor._autosave.stop(); editor.hide(); editor.deleteLater(); app.processEvents()
    restored = ObjectEditor(ShelfStore(tmp_path), store.get(obj["id"]), "en", lambda: None)
    assert restored.content.toPlainText() == "local unsaved" and restored._conflict
    restored.save_conflict_copy()
    assert store.get(obj["id"])["content"] == "remote saved"
    assert sorted(o["content"] for o in store.list()) == ["local unsaved", "remote saved"]
    assert not store.draft(obj["id"])
    restored.close(); restored.deleteLater(); app.processEvents()


def test_remote_layout_move_does_not_conflict_with_text_edit(tmp_path, app):
    store = ShelfStore(tmp_path)
    obj = store.create("note", content="before")
    editor = ObjectEditor(store, obj, "en", lambda: None)
    editor.content.setPlainText("local")
    store.place([obj["id"]], shelf="Work", board="files", index=0)
    assert editor.save()
    latest = store.get(obj["id"])
    assert latest["content"] == "local" and latest["shelf"] == "Work" and latest["board"] == "files"
    editor.close(); editor.deleteLater(); app.processEvents()


def test_sections_layout_persist_mask_and_square_cards(tmp_path, app):
    config = Config(tmp_path)
    # Explicit legacy transparent mode; the default now has a configurable backing.
    config.data["shelf"]["background_opacity_percent"] = 0
    shelf = Bookshelf(config, lambda _: None, lambda: None)
    assert not hasattr(shelf, "toolbar")
    note = shelf.store.create("note", title="Notiz")
    shelf.show(); app.processEvents()
    panel = shelf.sections["notes"]
    panel.move(40, 120); panel.resize(300, 280)
    shelf.persist_layout()
    saved = panel.geometry()
    panel.move(0, 0); shelf.restore_layout()
    assert panel.geometry() == saved
    config.data["shelf"].update(cell_shape="square", font_bold=True, font_italic=True, text_alignment="center")
    shelf.apply_settings(); app.processEvents()
    item = shelf.sections["notes"].view.item(0)
    assert item.sizeHint().width() == item.sizeHint().height()
    assert shelf.sections["notes"].view.font().bold()
    assert shelf.sections["notes"].view.font().italic()
    shelf.open_object(note["id"])
    config.data["shelf"].update(editor_width=540, editor_height=300)
    shelf.apply_settings(); app.processEvents()
    assert shelf.editors[note["id"]].width() == 540
    shelf.update_input_mask()
    assert not shelf.mask().contains(QPoint(1, 1))
    assert shelf.mask().contains(panel.header.mapTo(shelf, QPoint(10, 10)))
    blank = panel.mapTo(shelf, QPoint(20, panel.height() - 45))
    assert not shelf.mask().contains(blank)
    shelf._drag_active = True; shelf.update_input_mask()
    assert shelf.mask().contains(blank)
    shelf._drag_active = False; shelf.update_input_mask()
    assert not shelf.mask().contains(blank)
    # Native input holes are represented by a region; Windows passthrough is
    # separately verified on the target platform, not inferred from offscreen.
    shelf.shutdown(); shelf.deleteLater(); app.processEvents()


def test_card_drop_changes_board_and_grips_save_arrangement(tmp_path, app):
    from PySide6.QtCore import QMimeData, QPointF, QEvent
    from PySide6.QtGui import QDropEvent, QMouseEvent
    import json
    config = Config(tmp_path)
    shelf = Bookshelf(config, lambda _: None, lambda: None)
    obj = shelf.store.create("checklist", title="Keep type", items=[{"text": "keep text", "checked": True}])
    shelf.show(); app.processEvents()
    mime = QMimeData(); mime.setData("application/x-neo-shelf-ids", json.dumps([obj["id"]]).encode())
    event = QDropEvent(QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    shelf.sections["notes"].view.dropEvent(event)
    assert event.isAccepted()
    moved = shelf.store.get(obj["id"])
    assert moved["board"] == "notes" and moved["kind"] == "checklist" and moved["items"][0]["checked"]
    header_drop = QDropEvent(QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    shelf.sections["files"].dropEvent(header_drop)
    assert header_drop.isAccepted() and shelf.store.get(obj["id"])["board"] == "files"
    panel = shelf.sections["notes"]
    before = panel.pos()
    grip = panel.grip
    start = grip.mapToGlobal(QPoint(8, 8))
    grip.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(8, 8), QPointF(start), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    grip.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(8, 40), QPointF(start + QPoint(-10, 60)), Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    grip.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(8, 40), QPointF(start + QPoint(-10, 60)), Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    assert panel.pos() != before
    saved = panel.geometry(); shelf.restore_layout(); assert panel.geometry() == saved
    assert config.get("shelf.layout.version") == 1
    size_grip = panel.grip_size
    size_before = panel.size()
    start = size_grip.mapToGlobal(QPoint(5, 5))
    size_grip.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(5, 5), QPointF(start), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    size_grip.mouseMoveEvent(QMouseEvent(QEvent.MouseMove, QPointF(5, 5), QPointF(start + QPoint(20, -40)), Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
    size_grip.mouseReleaseEvent(QMouseEvent(QEvent.MouseButtonRelease, QPointF(5, 5), QPointF(start + QPoint(20, -40)), Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    assert panel.size() != size_before
    resized = panel.size(); shelf.restore_layout(); assert panel.size() == resized
    shelf.shutdown(); shelf.deleteLater(); app.processEvents()


def test_cards_are_centered_in_rows_and_survive_resize(tmp_path, app):
    cfg = Config(tmp_path); cfg.data["shelf"].update(cell_width=110, cell_height=80)
    shelf = Bookshelf(cfg, lambda _: None, lambda: None)
    for index in range(3): shelf.store.create("note", title=str(index))
    shelf.show(); app.processEvents()
    panel = shelf.sections["notes"]
    panel.resize(360, 300); shelf.refresh(); app.processEvents()
    view = panel.view; view.center_cards()
    a,b,c = [view.visualItemRect(view.item(index)) for index in range(3)]
    assert a.y() == b.y() and a.x() < b.x() and c.y() > a.y()
    assert abs(c.center().x() - view.viewport().rect().center().x()) <= 10
    panel.resize(250, 300); app.processEvents(); view.center_cards()
    assert view.visualItemRect(view.item(2)).bottom() > view.visualItemRect(view.item(0)).bottom()
    shelf.shutdown(); shelf.deleteLater(); app.processEvents()


def test_widgets_settings_own_all_panel_options_and_memory_roundtrip(tmp_path, app, monkeypatch):
    from jarvis.config import ConfigManager
    from jarvis.ui.settings_dialog import SettingsDialog, QMessageBox
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path)); monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(SettingsDialog, "_refresh_microphones_async", lambda self: None)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **kw: None)
    errors = []; monkeypatch.setattr(QMessageBox, "critical", lambda *a, **kw: errors.append(a))
    config = ConfigManager(Path(__file__).parents[1] / "config/default_settings.json")
    dialog = SettingsDialog(config)
    dialog.shelf_font_bold.setChecked(True)
    dialog.shelf_dimensions["editor_width"].setValue(540)
    dialog.shelf_cell_shape.setCurrentIndex(dialog.shelf_cell_shape.findData("square"))
    dialog.dialogue_memory_enabled.setChecked(False)
    dialog.dialogue_memory_days.setValue(12)
    dialog.dialogue_memory_turns.setValue(80)
    assert not dialog.shelf_dimensions["cell_height"].isEnabled()
    assert set(dialog.overlay_type_style_fields) == {"session", "checklist", "notes", "music"}
    widgets_tab = dialog.tabs._stack.widget(9)  # NEO 169: «Игры» after «Медиа»; NEO 184: + «Перевод»
    for fields in dialog.overlay_type_style_fields.values(): assert widgets_tab.isAncestorOf(fields["enabled"])
    assert widgets_tab.isAncestorOf(dialog.timer_overlay_enabled)
    dialog._save(close=False)
    assert not errors
    assert config.get("shelf.font_bold") and config.get("shelf.editor_width") == 540
    assert config.get("dialogue.memory") == {"enabled": False, "retention_days": 12, "max_turns": 80}
    dialog.close(); dialog.deleteLater(); app.processEvents()
