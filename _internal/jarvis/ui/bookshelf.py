"""Transparent desktop shelf with native input holes and versioned editors."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QMimeData, QPoint, QRect, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QAction, QDesktopServices, QDrag, QFont, QImage, QKeySequence, QRegion, QShortcut, QTextOption, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QListView, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizeGrip, QSpinBox, QVBoxLayout, QWidget)

from jarvis.ui.window_geometry import CaptionDrag, RoundedToolWindow, reachable_position
from jarvis.language import language_code
from jarvis.ui.desktop_events import request_desktop
from jarvis.ui.desktop_strings import dt
from jarvis.ui.shelf_store import ShelfStore, VersionConflict
from jarvis.ui.qt_localization import LocalizedFileDialog as QFileDialog

CHECKMARK_URL = (Path(__file__).resolve().parents[2] / "assets" / "checkmark.svg").as_posix()

WIDGET_STYLE = "QWidget{color:#edf4fc;} QLineEdit,QPlainTextEdit,QListWidget{background:#101923;border:1px solid #344657;border-radius:8px;padding:6px;} QPushButton{background:#243443;border:1px solid #344657;border-radius:7px;padding:6px 10px;} QPushButton:hover{background:#345069;} QPushButton:pressed{background:#41647e;} QMenu{background:#152331;border:1px solid #40566b;border-radius:9px;padding:6px;} QMenu::item{padding:8px 20px;border-radius:5px;} QMenu::item:selected{background:#315873;} QMenu::separator{height:1px;background:#344657;margin:5px;} QCheckBox::indicator,QListWidget::indicator{width:14px;height:14px;border:1px solid #7aa2bd;border-radius:3px;background:#152536;} QCheckBox::indicator:checked,QListWidget::indicator:checked{background:#4bbfe8;border:1px solid #85e1ff;} QScrollBar:vertical{background:#172536;width:10px;border-radius:5px;} QScrollBar::handle:vertical{background:#56748b;border-radius:5px;min-height:24px;} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;} QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:none;}"

WIDGET_STYLE += f' QListWidget::indicator:checked,QCheckBox::indicator:checked{{image:url("{CHECKMARK_URL}");}}'


def ask_discard(parent, language):
    try:
        from jarvis.ui.qt_localization import localized_message_box
        return localized_message_box(parent, QMessageBox.Question, dt("reload", language), dt("discard_draft", language), QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel, code=language) == QMessageBox.Yes
    except ImportError:
        box = QMessageBox(QMessageBox.Question, dt("reload", language), dt("discard_draft", language), parent=parent)
        yes = box.addButton(dt("yes", language), QMessageBox.AcceptRole)
        cancel = box.addButton(dt("cancel", language), QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return box.clickedButton() is yes


class DragGrip(QLabel):
    """An explicit handle moves a panel; text selection never drags the window."""
    def __init__(self, target, finished=None):
        super().__init__("⠿", target)
        self.target, self.finished, self.origin = target, finished, None
        self.setFixedWidth(24)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.SizeAllCursor)

    def mousePressEvent(self, event):
        if self.isEnabled() and event.button() == Qt.LeftButton:
            self.origin = (event.globalPosition().toPoint(), self.target.pos())
            event.accept()

    def mouseMoveEvent(self, event):
        if self.isEnabled() and self.origin and event.buttons() & Qt.LeftButton:
            position = self.origin[1] + event.globalPosition().toPoint() - self.origin[0]
            parent = self.target.parentWidget()
            if parent:
                position.setX(max(0, min(position.x(), parent.width() - self.target.width())))
                position.setY(max(0, min(position.y(), parent.height() - self.target.height())))
            self.target.move(reachable_position(self.target, position, event.globalPosition().toPoint()))
            event.accept()

    def mouseReleaseEvent(self, event):
        moved = self.origin is not None
        self.origin = None
        if moved and self.finished:
            self.finished()
        event.accept()


class ChecklistChange(QUndoCommand):
    def __init__(self, editor, before, after):
        super().__init__()
        self.editor, self.before, self.after, self.first = editor, before, after, True

    def undo(self):
        self.editor._apply_checklist(self.before)
        self.editor._mark_dirty()

    def redo(self):
        if self.first:
            self.first = False
        else:
            self.editor._apply_checklist(self.after)
            self.editor._mark_dirty()


def object_mime(objects: list[dict[str, Any]]) -> QMimeData:
    mime = QMimeData()
    mime.setData("application/x-neo-shelf-ids", json.dumps([x["id"] for x in objects]).encode())
    urls = [QUrl.fromLocalFile(x["path"]) for x in objects if x.get("path") and Path(x["path"]).is_file()]
    if urls:
        mime.setUrls(urls)  # Qt maps this to CF_HDROP on Windows.
    texts = []
    for obj in objects:
        if obj["kind"] == "checklist":
            texts.append("\n".join(("[x] " if x.get("checked") else "[ ] ") + x["text"] for x in obj.get("items", [])))
        else:
            texts.append(obj.get("content") or obj.get("path") or obj.get("title", ""))
    mime.setText("\n\n".join(texts))
    if len(objects) == 1 and objects[0]["kind"] == "image" and objects[0].get("path"):
        # Clipboard image decode is bounded, unlike a generic untrusted file open.
        from PySide6.QtGui import QImageReader
        reader = QImageReader(objects[0]["path"])
        size = reader.size()
        if size.isValid() and size.width() * size.height() <= 32_000_000:
            image = reader.read()
            if not image.isNull():
                mime.setImageData(image)
    return mime


def restore_visible_geometry(widget: QWidget, item: dict) -> None:
    screens = QApplication.screens()
    screen = next((s for s in screens if s.name() == item.get("screen")), QApplication.primaryScreen())
    if not screen:
        return
    available = screen.availableGeometry()
    size = item.get("size", [420, 360])
    width = max(260, min(int(size[0]), available.width()))
    height = max(160, min(int(size[1]), available.height()))
    point = item.get("position", [])
    x, y = point if len(point) == 2 else (available.center().x() - width // 2, available.center().y() - height // 2)
    # Qt widget geometry uses device-independent pixels, avoiding double DPI scaling.
    x = max(available.left(), min(int(x), available.right() - width + 1))
    y = max(available.top(), min(int(y), available.bottom() - height + 1))
    widget.setGeometry(x, y, width, height)


class ObjectEditor(RoundedToolWindow):
    def __init__(self, store: ShelfStore, item: dict, language: str, on_saved: Callable) -> None:
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint)
        self.store, self.item, self.language, self.on_saved = store, item, language, on_saved
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMinimumSize(280, 220)
        self.setObjectName("neoObjectEditor")
        self.setStyleSheet(WIDGET_STYLE + " QWidget#neoObjectEditor{background:transparent;border:none;}")
        self.dirty = False
        self._loading = True
        self._conflict = False
        self._base_font_size = 14
        self._font_family, self._font_bold, self._font_italic, self._alignment = "Segoe UI", False, False, "left"
        self._autosave = QTimer(self)
        self._autosave.setSingleShot(True)
        self._autosave.setInterval(650)
        self._autosave.timeout.connect(self.save)
        self._undo_stack = QUndoStack(self)
        self._last_checklist_state = []
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        header = QHBoxLayout()
        self.drag_grip = DragGrip(self)
        header.addWidget(self.drag_grip)
        self.title_edit = QLineEdit()
        header.addWidget(self.title_edit, 1)
        self.close_button = QPushButton("×")
        self.close_button.setFixedSize(30, 30)
        self.close_button.clicked.connect(self.close)
        header.addWidget(self.close_button)
        root.addLayout(header)
        # The full empty top edge is draggable; the title field remains editable.
        self.content = QPlainTextEdit()
        self.content.setMinimumWidth(240)
        self.content.setStyleSheet("font-size: 14px;")
        root.addWidget(self.content, 1)
        self.checklist = QListWidget()
        self.checklist.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.checklist.setContextMenuPolicy(Qt.CustomContextMenu)
        self.checklist.customContextMenuRequested.connect(self._checklist_menu)
        root.addWidget(self.checklist, 1)
        self.add_item = QLineEdit()
        self.add_item.returnPressed.connect(self._append_item)
        root.addWidget(self.add_item)
        buttons = QHBoxLayout()
        self.pin = QCheckBox()
        self.reload_button = QPushButton()
        self.recover_button = QPushButton()
        self.copy_button = QPushButton()
        self.reload_button.clicked.connect(self.reload)
        self.recover_button.clicked.connect(self.save_conflict_copy)
        self.copy_button.clicked.connect(self.copy)
        self.pin.toggled.connect(self._pin_changed)
        for widget in (self.pin, self.copy_button):
            buttons.addWidget(widget)
        buttons.addStretch(1)
        self.resize_grip = QSizeGrip(self)
        buttons.addWidget(self.resize_grip)
        root.addLayout(buttons)
        recovery = QHBoxLayout()
        recovery.addWidget(self.recover_button)
        recovery.addWidget(self.reload_button)
        root.addLayout(recovery)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        root.addWidget(self.notice)
        self.title_edit.textEdited.connect(self._mark_dirty)
        self.content.textChanged.connect(self._mark_dirty)
        self.checklist.itemChanged.connect(self._checklist_changed)
        QShortcut(QKeySequence.Save, self).activated.connect(self.save)
        for key, callback in ((QKeySequence.Undo, self._undo_stack.undo), (QKeySequence.Redo, self._undo_stack.redo), (QKeySequence.Cut, self._cut_items), (QKeySequence.Copy, self._copy_items), (QKeySequence.Paste, self._paste_items), (QKeySequence.SelectAll, self.checklist.selectAll), (QKeySequence.Delete, self._delete_items)):
            shortcut = QShortcut(key, self.checklist)
            shortcut.setContext(Qt.WidgetShortcut)
            shortcut.activated.connect(callback)
        # Ctrl+Y remains available even on platforms whose standard Redo
        # binding is Ctrl+Shift+Z; Windows keeps its normal binding.
        for field in (self.content, self.title_edit):
            redo = QShortcut(QKeySequence("Ctrl+Y"), field)
            redo.setContext(Qt.WidgetShortcut)
            redo.activated.connect(field.redo)
        if QKeySequence(QKeySequence.Redo).toString(QKeySequence.PortableText) != "Ctrl+Y":
            redo = QShortcut(QKeySequence("Ctrl+Y"), self.checklist)
            redo.setContext(Qt.WidgetShortcut)
            redo.activated.connect(self._undo_stack.redo)
        self._load()
        self.retranslate(language)
        restore_visible_geometry(self, self.item)
        self._loading = False
        draft = self.store.draft(item["id"])
        if draft:
            self.item = draft["base"]
            self._load({**self.item, **draft["changes"]})
            self.dirty, self._conflict = True, True
            self._show_conflict(dt("unsaved_recovered", self.language))

    def _mark_dirty(self, *_args):
        if self._loading:
            return
        self.dirty = True
        self._autosave.start()

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, "_autosave"):
            self._mark_dirty()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.scale_text()
        if hasattr(self, "_autosave"):
            self._mark_dirty()

    def scale_text(self):
        if not hasattr(self, "content"):
            return
        # Scale with width only; long text scrolls instead of shrinking endlessly.
        font_size = max(12, min(28, round(self._base_font_size * max(0.86, min(1.35, self.width() / 420)))))
        font = QFont(self._font_family)
        font.setPixelSize(font_size)
        font.setBold(self._font_bold)
        font.setItalic(self._font_italic)
        self.content.setFont(font)
        self.checklist.setFont(font)
        title_font = QFont(font)
        title_font.setPixelSize(min(16, font_size))
        self.title_edit.setFont(title_font)
        # Retain explicit size for platform themes and deterministic layout tests.
        self.content.setStyleSheet(f"font-size:{font_size}px;")
        self.checklist.setStyleSheet(f"font-size:{font_size}px;")
        alignment = {"left": Qt.AlignLeft, "center": Qt.AlignHCenter, "right": Qt.AlignRight}.get(self._alignment, Qt.AlignLeft)
        option = self.content.document().defaultTextOption()
        option.setAlignment(alignment)
        self.content.document().setDefaultTextOption(option)
        self.checklist.blockSignals(True)
        for index in range(self.checklist.count()):
            self.checklist.item(index).setTextAlignment(alignment | Qt.AlignVCenter)
        self.checklist.blockSignals(False)
        self._sync_checklist_marks()

    def _apply_checklist(self, items):
        loading, self._loading = self._loading, True
        self.checklist.clear()
        for raw in items:
            item = QListWidgetItem(raw["text"], self.checklist)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
            item.setCheckState(Qt.Checked if raw.get("checked") else Qt.Unchecked)
            font = item.font()
            font.setStrikeOut(bool(raw.get("checked")))
            item.setFont(font)
        self._last_checklist_state = self._checklist_items()
        self._loading = loading

    def _checklist_items(self):
        return [{"text": self.checklist.item(i).text(), "checked": self.checklist.item(i).checkState() == Qt.Checked} for i in range(self.checklist.count())]

    def _sync_checklist_marks(self):
        blocked = self.checklist.blockSignals(True)
        try:
            for i in range(self.checklist.count()):
                item = self.checklist.item(i)
                font = QFont(self.checklist.font())
                font.setStrikeOut(item.checkState() == Qt.Checked)
                item.setFont(font)
        finally:
            self.checklist.blockSignals(blocked)

    def _checklist_changed(self, *_args):
        if self._loading:
            return
        self._sync_checklist_marks()
        after = self._checklist_items()
        if after != self._last_checklist_state:
            self._undo_stack.push(ChecklistChange(self, self._last_checklist_state, after))
            self._last_checklist_state = after
            self._mark_dirty()

    def _load(self, document=None):
        loading, self._loading = self._loading, True
        document = document or self.item
        self.title_edit.setText(document.get("title", ""))
        self.content.setPlainText(document.get("content", ""))
        self._apply_checklist(document.get("items", []))
        checklist = self.item["kind"] == "checklist"
        self.content.setVisible(not checklist)
        self.checklist.setVisible(checklist)
        self.add_item.setVisible(checklist)
        self.pin.blockSignals(True)
        self.pin.setChecked(bool(document.get("pinned")))
        self.pin.blockSignals(False)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.pin.isChecked())
        self.dirty = False
        self._undo_stack.clear()
        self._autosave.stop()
        self._loading = loading
        self._conflict = False
        self.reload_button.hide()
        self.recover_button.hide()

    def retranslate(self, language):
        self.language = language
        self.setWindowTitle(self.item.get("title") or dt(self.item["kind"], language))
        self.title_edit.setPlaceholderText(dt("title", language))
        self.pin.setText(dt("pin", language))
        self.reload_button.setText(dt("reload", language))
        self.recover_button.setText(dt("save_conflict_copy", language))
        self.copy_button.setText(dt("copy", language))
        self.add_item.setPlaceholderText(dt("add_item", language))
        self.close_button.setToolTip(dt("close", language))
        self.drag_grip.setToolTip(dt("move_section", language))
        self.resize_grip.setToolTip(dt("resize_section", language))

    def _append_item(self):
        text = self.add_item.text().strip()
        if text:
            self._loading = True
            item = QListWidgetItem(text, self.checklist)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
            item.setCheckState(Qt.Unchecked)
            self.add_item.clear()
            self._loading = False
            self._checklist_changed()

    def _pin_changed(self, value):
        self._mark_dirty()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, value)
        self.show()

    def _changes(self):
        screen = self.screen()
        return dict(title=self.title_edit.text(), content=self.content.toPlainText(), items=[{"text": self.checklist.item(i).text(), "checked": self.checklist.item(i).checkState() == Qt.Checked} for i in range(self.checklist.count())], pinned=self.pin.isChecked(), position=[self.x(), self.y()], size=[self.width(), self.height()], screen=screen.name() if screen else "", dpi=screen.logicalDotsPerInch() if screen else 96)

    def save(self) -> bool:
        self._autosave.stop()
        try:
            changes = {key: value for key, value in self._changes().items() if value != self.item.get(key)}
            latest = self.store.get(self.item["id"])
            # Card ordering and remote edits to an unrelated field must not
            # conflict with local typing, and may never overwrite newer content.
            if latest["version"] != self.item["version"]:
                if any(latest.get(key) != self.item.get(key) and latest.get(key) != value for key, value in changes.items() if key in {"title", "content", "items", "pinned"}):
                    raise VersionConflict("Concurrent text edit")
            self.item = self.store.update(latest["id"], latest["version"], **changes) if changes else latest
            self._loading = True
            if "title" not in changes: self.title_edit.setText(self.item.get("title", ""))
            if "content" not in changes and self.content.toPlainText() != self.item.get("content", ""): self.content.setPlainText(self.item.get("content", ""))
            if "items" not in changes and self._checklist_items() != self.item.get("items", []): self._apply_checklist(self.item.get("items", []))
            if "pinned" not in changes:
                self.pin.blockSignals(True)
                self.pin.setChecked(bool(self.item.get("pinned")))
                self.pin.blockSignals(False)
                was_visible = self.isVisible()
                self.setWindowFlag(Qt.WindowStaysOnTopHint, self.pin.isChecked())
                if was_visible: self.show()
            self._loading = False
            self.dirty = False
            self._conflict = False
            self.store.clear_draft(self.item["id"])
            self.notice.setText(dt("autosaved", self.language))
            self.reload_button.hide()
            self.recover_button.hide()
            self.on_saved()
            return True
        except (VersionConflict, KeyError):
            self.store.save_draft(self.item, self._changes())
            self._show_conflict(dt("conflict", self.language))
            return False
        except (OSError, ValueError) as exc:
            self.notice.setText(str(exc))
            return False

    def _show_conflict(self, text):
        self._conflict = True
        self.notice.setText(text)
        self.reload_button.show()
        self.recover_button.show()

    def save_conflict_copy(self):
        changes = self._changes()
        new = self.store.create(self.item["kind"], title=changes["title"] + " — " + dt("recover_draft", self.language), content=changes["content"], items=changes["items"], shelf=self.item.get("shelf", "default"), board=self.item.get("board", ""), pinned=changes["pinned"], size=changes["size"])
        self.store.clear_draft(self.item["id"])
        try:
            self.item = self.store.get(self.item["id"])
        except KeyError:
            self.dirty = False
            self.hide()
        self._load()
        self.notice.clear()
        self.on_saved()
        request_desktop("shelf.open", object_id=new["id"])

    def reload(self):
        if self.dirty:
            if not ask_discard(self, self.language):
                return
        self.store.clear_draft(self.item["id"])
        self.item = self.store.get(self.item["id"])
        self._load()
        self.notice.clear()

    def copy(self):
        QApplication.clipboard().setMimeData(object_mime([{**self.item, **self._changes()}]))

    def _copy_items(self):
        QApplication.clipboard().setText("\n".join(("[x] " if item.checkState() == Qt.Checked else "[ ] ") + item.text() for item in self.checklist.selectedItems()))

    def _delete_items(self):
        self._loading = True
        for item in self.checklist.selectedItems(): self.checklist.takeItem(self.checklist.row(item))
        self._loading = False
        self._checklist_changed()

    def _cut_items(self):
        self._copy_items()
        self._delete_items()

    def _paste_items(self):
        self._loading = True
        for line in QApplication.clipboard().text().splitlines()[:5000 - self.checklist.count()]:
            if not line.strip(): continue
            checked = line.startswith("[x] ")
            text = line[4:] if line.startswith(("[x] ", "[ ] ")) else line
            item = QListWidgetItem(text[:5000], self.checklist)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self._loading = False
        self._checklist_changed()

    def _checklist_menu(self, point):
        menu = QMenu(self)
        for key, callback in (("undo", self._undo_stack.undo), ("redo", self._undo_stack.redo), ("cut", self._cut_items), ("copy", self._copy_items), ("paste", self._paste_items), ("select_all", self.checklist.selectAll), ("delete_item", self._delete_items)):
            menu.addAction(dt(key, self.language), callback)
        menu.exec(self.checklist.mapToGlobal(point))

    def closeEvent(self, event):
        # Close folds the object, never deletes it. A conflict leaves the editor
        # open with the user's unsaved text rather than silently losing changes.
        if self.save():
            try:
                latest = self.store.get(self.item["id"])
                self.item = self.store.update(self.item["id"], latest["version"], visible=False)
            except KeyError:
                pass  # An explicitly deleted object need not be folded.
            except VersionConflict:
                self._show_conflict(dt("conflict", self.language))
                event.ignore()
                return
            event.accept()
        else:
            event.ignore()


class ShelfList(QListWidget):
    def __init__(self, shelf: "Bookshelf", kinds: set[str], board="notes"):
        super().__init__(shelf)
        self.shelf, self.kinds, self.board = shelf, kinds, board
        self.setViewMode(QListView.IconMode)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Free)
        self.setWrapping(True)
        self.setWordWrap(True)
        self.setSpacing(0)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.context_menu)
        self.itemClicked.connect(self.clicked_item)
        self.itemActivated.connect(self.clicked_item)
        self.verticalScrollBar().valueChanged.connect(lambda _value: shelf.update_input_mask())
        self.setStyleSheet("QListWidget{background:transparent;border:0;padding:0;} QListWidget::item{background:#172536;color:#edf4fc;border:1px solid #36546e;border-radius:9px;padding:14px;margin:5px;} QListWidget::item:selected{background:#285978;}")
        QShortcut(QKeySequence.Copy, self).activated.connect(self.copy_selected)
        QShortcut(QKeySequence.Paste, self).activated.connect(lambda: shelf.import_mime(QApplication.clipboard().mimeData(), board=self.board))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "shelf"):
            self.center_cards()
            QTimer.singleShot(0, self.shelf.update_input_mask)

    def center_cards(self):
        self.setViewportMargins(0, 0, 0, 0)
        self.doItemsLayout()
        grid = self.gridSize()
        if grid.width() <= 0 or not self.count(): return
        width = self.viewport().width()
        columns = max(1, width // grid.width())
        for start in range(0, self.count(), columns):
            count = min(columns, self.count() - start)
            left = max(0, (width - count * grid.width()) // 2)
            for column in range(count):
                index = self.model().index(start + column, 0)
                self.setPositionForIndex(QPoint(left + column * grid.width(), (start // columns) * grid.height()), index)

    def selected_objects(self):
        return [self.shelf.store.get(item.data(Qt.UserRole)) for item in self.selectedItems()]

    def clicked_item(self, item):
        self.shelf.clear_other_selections(self)
        if not (QApplication.keyboardModifiers() & (Qt.ControlModifier | Qt.ShiftModifier)):
            self.shelf.open_object(item.data(Qt.UserRole))

    def copy_selected(self):
        QApplication.clipboard().setMimeData(object_mime(self.selected_objects()))

    def startDrag(self, supportedActions):
        objects = self.selected_objects()
        if objects:
            drag = QDrag(self)
            drag.setMimeData(object_mime(objects))
            # Copy outward preserves original and managed assets in all targets.
            self.shelf._drag_active = True
            self.shelf.update_input_mask()
            try:
                drag.exec(Qt.CopyAction | Qt.MoveAction, Qt.CopyAction)
            finally:
                self.shelf._drag_active = False
                self.shelf.update_input_mask()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasText() or event.mimeData().hasImage() or event.mimeData().hasFormat("application/x-neo-shelf-ids"):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat("application/x-neo-shelf-ids"):
            try:
                ids = json.loads(bytes(mime.data("application/x-neo-shelf-ids")))
                target = self.itemAt(event.position().toPoint())
                index = self.row(target) if target else self.count()
                index -= sum(self.item(row).data(Qt.UserRole) in ids for row in range(index))
                self.shelf.reorder(ids, index, self.board)
            except (ValueError, KeyError, TypeError, VersionConflict) as exc:
                QMessageBox.warning(self, dt("error", self.shelf.language), str(exc))
                event.ignore()
                return
        else:
            self.shelf.import_mime(mime, board=self.board)
        event.setDropAction(Qt.CopyAction)
        event.accept()

    def context_menu(self, point):
        item = self.itemAt(point)
        if not item:
            return
        if not item.isSelected():
            self.clearSelection()
            item.setSelected(True)
        menu = QMenu(self)
        menu.setStyleSheet(WIDGET_STYLE)
        language = self.shelf.language
        obj = self.shelf.store.get(item.data(Qt.UserRole))
        menu.addAction(dt("open", language), lambda: self.shelf.open_object(obj["id"]))
        menu.addSeparator()
        menu.addAction(dt("copy", language), self.copy_selected)
        menu.addAction(dt("rename", language), lambda: self.shelf.rename(obj))
        menu.addAction(dt("unpin" if obj.get("pinned") else "pin", language), lambda: self.shelf.pin_object(obj))
        move = menu.addMenu(dt("move_shelf", language))
        for index in range(self.shelf.shelves.count()):
            target = self.shelf.shelves.itemText(index)
            move.addAction(target, lambda name=target: self.shelf.move_selected(self, name))
        menu.addAction(dt("remove", language), lambda: self.shelf.remove_selected(self))
        menu.addAction(dt("delete_disk", language), lambda: self.shelf.delete_selected_files(self))
        menu.exec(self.mapToGlobal(point))




class ShelfSection(QWidget):
    def __init__(self, shelf, board, kinds):
        super().__init__(shelf)
        self.shelf, self.board = shelf, board
        self.setObjectName("shelfSection")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self.setMinimumSize(240, 180)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.header = QWidget(self)
        self.header.setObjectName("sectionHeader")
        self.header.setStyleSheet("QWidget#sectionHeader{background:#172536;border:1px solid #36546e;border-radius:10px;}")
        bar = QHBoxLayout(self.header)
        bar.setContentsMargins(6, 5, 6, 5)
        self.grip = DragGrip(self, lambda: shelf.section_moved(self))
        bar.addWidget(self.grip)
        self.title = QLabel()
        bar.addWidget(self.title, 1)
        self._caption_drag = CaptionDrag(self, (self.header, self.title),
            lambda: shelf.section_moved(self),
            enabled=lambda: bool(shelf.config.get("shelf.layout_editable", True)))
        self.add = QPushButton("+")
        self.add.setFixedWidth(32)
        self.add.clicked.connect(shelf.import_dialog if board == "files" else lambda: shelf.new_object("note" if board == "notes" else "checklist", board))
        bar.addWidget(self.add)
        self.menu_button = QPushButton("⋯")
        self.menu_button.setFixedWidth(32)
        self.menu_button.clicked.connect(lambda: shelf.section_menu(self))
        bar.addWidget(self.menu_button)
        layout.addWidget(self.header)
        self.view = ShelfList(shelf, kinds, board)
        layout.addWidget(self.view, 1)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.grip_size = SectionSizeGrip(self)
        bottom.addWidget(self.grip_size)
        layout.addLayout(bottom)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self.shelf, "_layout_timer") and not self.shelf._restoring_layout:
            self.shelf._layout_timer.start()
            QTimer.singleShot(0, self.shelf.update_input_mask)

    def moveEvent(self, event):
        super().moveEvent(event)
        QTimer.singleShot(0, self.shelf.update_input_mask)

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat("application/x-neo-shelf-ids") or mime.hasUrls() or mime.hasText() or mime.hasImage():
            event.acceptProposedAction()

    def dropEvent(self, event):
        mime = event.mimeData()
        try:
            if mime.hasFormat("application/x-neo-shelf-ids"):
                self.shelf.reorder(json.loads(bytes(mime.data("application/x-neo-shelf-ids"))), self.view.count(), self.board)
            else:
                self.shelf.import_mime(mime, board=self.board)
            event.acceptProposedAction()
        except (ValueError, KeyError, TypeError, VersionConflict) as exc:
            QMessageBox.warning(self, dt("error", self.shelf.language), str(exc))
            event.ignore()


class SectionSizeGrip(QLabel):
    def __init__(self, section):
        super().__init__("◢", section)
        self.section, self.origin = section, None
        self.setFixedSize(22, 20)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#172536;border-radius:5px;color:#98b8ce;")
        self.setCursor(Qt.SizeFDiagCursor)

    def mousePressEvent(self, event):
        self.origin = (event.globalPosition().toPoint(), self.section.size())

    def mouseMoveEvent(self, event):
        if self.origin and event.buttons() & Qt.LeftButton:
            delta = event.globalPosition().toPoint() - self.origin[0]
            parent = self.section.parentWidget()
            self.section.resize(min(parent.width() - self.section.x(), max(240, self.origin[1].width() + delta.x())), min(parent.height() - self.section.y() - 70, max(180, self.origin[1].height() + delta.y())))

    def mouseReleaseEvent(self, event):
        self.origin = None
        self.section.shelf.persist_layout()


class Bookshelf(QWidget):
    def __init__(self, config: Any, run_scenario: Callable[[str], None], edit_scenarios: Callable[[], None]):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.config = config
        self.language = language_code(config)
        self.store = ShelfStore(config.paths.root)
        self.run_scenario, self.edit_scenarios = run_scenario, edit_scenarios
        self.editors: dict[str, ObjectEditor] = {}
        self.pinned_cards = {}
        self._allow_pinned_cards = bool(config.get("shelf.restore_pinned", True))
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.setStyleSheet(WIDGET_STYLE)
        self._restoring_layout = True
        self._drag_active = False
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(450)
        self._layout_timer.timeout.connect(self.persist_layout)
        # Named collections remain available through each section's menu.
        # There is deliberately no separate toolbar occupying the desktop.
        self.shelves = QComboBox(self)
        self.shelves.setEditable(True)
        self.shelves.hide()
        self.shelves.currentTextChanged.connect(lambda _text: self.refresh())
        self.managed = QCheckBox(self)
        self.managed.setChecked(bool(config.get("shelf.managed_copy_default", False)))
        self.managed.hide()
        self.sections = {}
        for key, kinds in (("checklist", {"checklist"}), ("files", {"file", "image", "link"}), ("notes", {"note", "text"})):
            self.sections[key] = ShelfSection(self, key, kinds)
        self.lists = [section.view for section in self.sections.values()]
        self.headers = [section.header for section in self.sections.values()]
        self.scenario_bar = QWidget()
        self.scenario_bar.setStyleSheet("background:#172536;color:#edf4fc;border-radius:9px;")
        self.scenario_layout = QHBoxLayout(self.scenario_bar)
        self.scenario_layout.setContentsMargins(12, 8, 12, 8)
        self.scenario_scroll = QScrollArea(self)
        self.scenario_scroll.setWidgetResizable(True)
        self.scenario_scroll.setWidget(self.scenario_bar)
        self.scenario_scroll.setStyleSheet("QScrollArea{background:transparent;border:0;}")
        self.scenario_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._restoring_layout = False
        self._sizing_signature = None
        self._editor_dimensions_signature = None
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.hide)
        self.refresh()
        self.retranslate(self.language)
        # Reopen pinned text objects on startup, even with the shelf itself hidden.
        if config.get("shelf.restore_pinned", True) and config.get("shelf.enabled", True):
            for obj in self.store.list():
                if obj.get("pinned") and obj["kind"] in {"note", "text", "checklist"} and obj.get("visible", True):
                    self.open_object(obj["id"], activate=False)
        self.apply_settings()

        # Recovery drafts survive conflicts and unexpected application exits.
        for draft in (self.store.drafts() if config.get("shelf.enabled", True) else []):
            base = draft.get("base", {})
            if base.get("id"):
                try:
                    self.open_object(base["id"], activate=False)
                except KeyError:
                    changes = draft.get("changes", {})
                    restored = self.store.create(base["kind"], title=changes.get("title", base.get("title", "")) + " — " + dt("recover_draft", self.language), content=changes.get("content", base.get("content", "")), items=changes.get("items", base.get("items", [])), shelf=base.get("shelf", "default"))
                    self.store.clear_draft(base["id"])
                    self.open_object(restored["id"], activate=False)

    def apply_settings(self):
        opacity = max(25, min(100, int(self.config.get("shelf.opacity_percent", 100)))) / 100
        font_size = max(12, min(28, int(self.config.get("shelf.font_size", 14))))
        self.setWindowOpacity(opacity)
        self.managed.blockSignals(True)
        self.managed.setChecked(bool(self.config.get("shelf.managed_copy_default", False)))
        self.managed.blockSignals(False)
        editable = bool(self.config.get("shelf.layout_editable", True))
        color = QColor(str(self.config.get("shelf.background_color", "#172536")))
        if not color.isValid():
            color = QColor("#172536")
        alpha = max(0, min(100, int(self.config.get("shelf.background_opacity_percent", 85))))
        for section in self.sections.values():
            section.setStyleSheet(f"QWidget#shelfSection{{background:rgba({color.red()},{color.green()},{color.blue()},{round(alpha * 2.55)});border-radius:12px;}}")
            section.grip.origin = None
            section.grip.setEnabled(editable)
            section.grip.setVisible(editable)
            section.grip_size.setEnabled(editable)
            section.grip_size.setVisible(editable)
        signature = tuple(self.config.get("shelf." + key, default) for key, default in (("section_width", 360), ("section_height", 420)))
        if self._sizing_signature != signature:
            first_apply = self._sizing_signature is None
            self._sizing_signature = signature
            self.restore_layout(use_saved=first_apply)
        self.refresh()
        editor_dimensions = (int(self.config.get("shelf.editor_width", 420)), int(self.config.get("shelf.editor_height", 360)))
        resize_editors = self._editor_dimensions_signature is not None and editor_dimensions != self._editor_dimensions_signature
        self._editor_dimensions_signature = editor_dimensions
        for editor in self.editors.values():
            editor._base_font_size = font_size
            editor._font_family = str(self.config.get("shelf.font_family", "Segoe UI"))
            editor._font_bold = bool(self.config.get("shelf.font_bold", False))
            editor._font_italic = bool(self.config.get("shelf.font_italic", False))
            editor._alignment = str(self.config.get("shelf.text_alignment", "left"))
            if resize_editors:
                restore_visible_geometry(editor, {**editor.item, "position": [editor.x(), editor.y()], "size": list(editor_dimensions)})
            editor.scale_text()
            editor.setWindowOpacity(opacity)
        if not self.config.get("shelf.enabled", True):
            self.hide()
            self.show_notes(False)
            for card in self.pinned_cards.values(): card.hide()

    def retranslate(self, language):
        self.language = language
        self.managed.setText(dt("managed", language))
        for key, section in self.sections.items():
            section.title.setText(dt(key, language))
            section.grip.setToolTip(dt("move_section", language))
            section.grip_size.setToolTip(dt("resize_section", language))
            section.add.setToolTip(dt("add", language))
            section.menu_button.setToolTip(dt("settings", language))
        for button in self.findChildren(QPushButton):
            key = button.property("desktop_key")
            if key:
                button.setText(dt(key, language))
        for editor in self.editors.values():
            editor.retranslate(language)
        self.refresh_scenarios()

    def new_shelf(self):
        text, ok = QInputDialog.getText(self, dt("new_shelf", self.language), dt("title", self.language))
        if ok and text.strip():
            name = text.strip()[:80]
            if self.shelves.findText(name) < 0:
                self.shelves.addItem(name)
            self.config.data.setdefault("shelf", {})["names"] = [self.shelves.itemText(i) for i in range(self.shelves.count())]
            self.config.save(self.config.data)
            self.shelves.setCurrentText(name)

    def _save_managed_default(self, enabled):
        self.config.data.setdefault("shelf", {})["managed_copy_default"] = bool(enabled)
        self.config.save(self.config.data)

    def refresh(self):
        objects = self.store.list()
        selected = self.shelves.currentText() or "default"
        self.shelves.blockSignals(True)
        existing = {self.shelves.itemText(i) for i in range(self.shelves.count())}
        for name in sorted({"default", selected, *(o.get("shelf", "default") for o in objects), *(self.config.get("shelf.names", []) or [])} - existing):
            self.shelves.addItem(name)
        self.shelves.setCurrentText(selected)
        self.shelves.blockSignals(False)
        for view in self.lists:
            selection = {i.data(Qt.UserRole) for i in view.selectedItems()}
            view.clear()
            width = max(100, min(400, int(self.config.get("shelf.cell_width", 180))))
            height = width if self.config.get("shelf.cell_shape", "rectangle") == "square" else max(64, min(400, int(self.config.get("shelf.cell_height", 100))))
            width = min(width, max(100, view.width() - 28))
            view.setGridSize(QSize(width + 14, height + 14))
            font = QFont(str(self.config.get("shelf.font_family", "Segoe UI")))
            font.setPixelSize(max(12, min(28, int(self.config.get("shelf.font_size", 14)))))
            font.setBold(bool(self.config.get("shelf.font_bold", False)))
            font.setItalic(bool(self.config.get("shelf.font_italic", False)))
            view.setFont(font)
            for obj in objects:
                if self.store.board_for(obj) == view.board and obj.get("shelf", "default") == selected:
                    label = ("📌 " if obj.get("pinned") else "") + (obj.get("title") or dt(obj["kind"], self.language))
                    item = QListWidgetItem(label)
                    item.setData(Qt.UserRole, obj["id"])
                    item.setToolTip(obj.get("path") or obj.get("content", "")[:500])
                    item.setSizeHint(QSize(width, height))
                    item.setTextAlignment({"left": Qt.AlignLeft, "center": Qt.AlignHCenter, "right": Qt.AlignRight}.get(self.config.get("shelf.text_alignment", "left"), Qt.AlignLeft) | Qt.AlignVCenter)
                    view.addItem(item)
                    item.setSelected(obj["id"] in selection)
            view.center_cards()
        self.refresh_scenarios()
        self._refresh_pinned_cards(objects)
        QTimer.singleShot(0, self.update_input_mask)

    def refresh_scenarios(self):
        while self.scenario_layout.count():
            item = self.scenario_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        label = QLabel(dt("scenarios", self.language))
        self.scenario_layout.addWidget(label)
        for raw in self.config.get("scenarios.items", []) or []:
            if not isinstance(raw, dict) or raw.get("enabled", True) is not True:
                continue
            # Stable identity is assigned on migration/settings save, never index.
            scenario_id = str(raw.get("id") or raw.get("name") or raw.get("phrase") or "")
            if not scenario_id:
                continue
            button = QPushButton(str(raw.get("name") or raw.get("phrase") or scenario_id))
            button.clicked.connect(lambda _checked=False, sid=scenario_id: self._run_scenario(sid))
            self.scenario_layout.addWidget(button)
        plus = QPushButton("+")
        plus.clicked.connect(self.edit_scenarios)
        self.scenario_layout.addWidget(plus)
        self.position_scenarios()
        QTimer.singleShot(0, self.position_scenarios)

    def position_scenarios(self):
        widgets = [self.scenario_layout.itemAt(i).widget() for i in range(self.scenario_layout.count())]
        for widget in widgets: widget.ensurePolished()
        self.scenario_layout.activate()
        natural = max(220, sum(widget.sizeHint().width() for widget in widgets) + max(0, len(widgets) - 1) * self.scenario_layout.spacing() + 24)
        self.scenario_bar.setMinimumWidth(natural)
        width = min(natural, max(240, self.width() - 48))
        self.scenario_scroll.setGeometry((self.width() - width) // 2, max(0, self.height() - 84), width, 64)
        self.update_input_mask()

    def restore_layout(self, use_saved=True):
        self._restoring_layout = True
        available_width, available_height = max(240, self.width()), max(280, self.height())
        screen = self.screen()
        saved = self.config.get("shelf.layout", {}) or {}
        screens = saved.get("screens", {}) if isinstance(saved, dict) else {}
        saved = screens.get(screen.name() if screen else "default", {}) if isinstance(screens, dict) else {}
        if not isinstance(saved, dict): saved = {}
        old_bounds = saved.get("bounds", [available_width, available_height])
        if not isinstance(old_bounds, list) or len(old_bounds) != 2 or not all(isinstance(v, (int, float)) and v > 0 for v in old_bounds): old_bounds = [available_width, available_height]
        saved_sections = saved.get("sections", {})
        if not isinstance(saved_sections, dict): saved_sections = {}
        width = max(240, min(int(self.config.get("shelf.section_width", 360)), (available_width - 64) // 3))
        height = max(180, min(int(self.config.get("shelf.section_height", 420)), available_height - 130))
        start_x = (available_width - 3 * width - 32) // 2
        for index, (key, section) in enumerate(self.sections.items()):
            geometry = saved_sections.get(key) if use_saved else None
            if isinstance(geometry, list) and len(geometry) == 4 and all(isinstance(v, (int, float)) for v in geometry):
                x, y, w, h = map(int, geometry)
                x = round(x * available_width / max(1, old_bounds[0]))
                y = round(y * available_height / max(1, old_bounds[1]))
            else:
                x, y, w, h = start_x + index * (width + 16), max(18, (available_height - 100 - height) // 2), width, height
            w, h = max(240, min(w, available_width)), max(180, min(h, available_height - 100))
            section.setGeometry(max(0, min(x, available_width - w)), max(0, min(y, available_height - h - 90)), w, h)
            section._last_position = section.pos()
        self.position_scenarios()
        self._restoring_layout = False
        self.update_input_mask()

    def persist_layout(self):
        if self._restoring_layout or not self.isVisible(): return
        self._layout_timer.stop()
        screen = self.screen()
        shelf_config = self.config.data.setdefault("shelf", {})
        layout = shelf_config.get("layout")
        if not isinstance(layout, dict): layout = shelf_config["layout"] = {}
        layout["version"] = 1
        if not isinstance(layout.get("screens"), dict): layout["screens"] = {}
        layout.setdefault("screens", {})[screen.name() if screen else "default"] = {"bounds": [self.width(), self.height()], "sections": {key: [section.x(), section.y(), section.width(), section.height()] for key, section in self.sections.items()}}
        self.config.save(self.config.data)

    def section_moved(self, section):
        # Crossing another section's header swaps their positions; otherwise
        # the exact free arrangement is retained, including vertical placement.
        for other in self.sections.values():
            if other is not section and other.geometry().contains(section.geometry().center()):
                origin = getattr(section, "_last_position", other.pos())
                section.move(other.pos())
                other.move(origin)
                break
        for panel in self.sections.values(): panel._last_position = panel.pos()
        self.persist_layout()
        self.update_input_mask()

    def section_menu(self, section):
        menu = QMenu(self)
        menu.setStyleSheet(WIDGET_STYLE)
        collections = menu.addMenu(dt("select_shelf", self.language))
        for index in range(self.shelves.count()):
            name = self.shelves.itemText(index)
            action = collections.addAction(name, lambda name=name: self.shelves.setCurrentText(name))
            action.setCheckable(True)
            action.setChecked(name == self.shelves.currentText())
        collections.addSeparator()
        collections.addAction(dt("new_shelf", self.language), self.new_shelf)
        menu.addAction(dt("paste", self.language), lambda: self.import_mime(QApplication.clipboard().mimeData(), board=section.board))
        menu.addAction(dt("settings", self.language), lambda: request_desktop("settings.open", section="shelf"))
        menu.addAction(dt("reset_layout", self.language), lambda: (self.restore_layout(False), self.persist_layout()))
        menu.addSeparator()
        menu.addAction(dt("close", self.language), self.hide)
        menu.exec(section.menu_button.mapToGlobal(QPoint(0, section.menu_button.height())))

    def _run_scenario(self, scenario_id):
        self.hide()
        self.run_scenario(scenario_id)

    def update_input_mask(self):
        if not self.isVisible():
            return
        # A native window region removes blank areas altogether, unlike
        # HTTRANSPARENT which only reliably forwards within one Windows thread.
        region = QRegion()
        if self._drag_active or int(self.config.get("shelf.background_opacity_percent", 85)) > 0:
            for section in self.sections.values(): region |= QRegion(section.geometry())
        for widget in [self.scenario_scroll, *self.headers, *(s.grip_size for s in self.sections.values())]:
            region |= QRegion(QRect(widget.mapTo(self, QPoint()), widget.size()))
        for view in self.lists:
            viewport = view.viewport()
            for i in range(view.count()):
                rect = view.visualItemRect(view.item(i)).intersected(viewport.rect())
                if not rect.isEmpty():
                    rect.translate(viewport.mapTo(self, QPoint()))
                    region |= QRegion(rect.adjusted(4, 4, -4, -4))
            if view.verticalScrollBar().isVisible():
                sb = view.verticalScrollBar()
                region |= QRegion(QRect(sb.mapTo(self, QPoint()), sb.size()))
        self.setMask(region)

    def showEvent(self, event):
        super().showEvent(event)
        self._allow_pinned_cards = True
        screen = QApplication.screenAt(self.cursor().pos()) or QApplication.primaryScreen()
        if screen:
            self.setGeometry(screen.availableGeometry())
        self.restore_layout()
        self.refresh()
        QTimer.singleShot(0, self.update_input_mask)

    def hideEvent(self, event):
        # Folding the shelf keeps only explicitly pinned objects on the desktop.
        # A conflicting editor stays open so local unsaved text is never lost.
        for editor in self.editors.values():
            if editor.isVisible() and not editor.pin.isChecked() and editor.save():
                editor.hide()
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "scenario_layout"): self.position_scenarios()
        QTimer.singleShot(0, self.update_input_mask)

    def new_object(self, kind, board=""):
        obj = self.store.create(kind, title=dt(kind, self.language), shelf=self.shelves.currentText() or "default", board=board, size=[int(self.config.get("shelf.editor_width", 420)), int(self.config.get("shelf.editor_height", 360))])
        self.refresh()
        self.open_object(obj["id"])

    def open_object(self, object_id, activate=True):
        obj = self.store.get(object_id)
        if obj["kind"] in {"note", "text", "checklist"}:
            if not obj.get("visible", True):
                obj = self.store.update(obj["id"], obj["version"], visible=True)
            editor = self.editors.get(object_id)
            if editor is None:
                editor = ObjectEditor(self.store, obj, self.language, self.refresh)
                if not obj.get("position"):
                    restore_visible_geometry(editor, {**obj, "size": [int(self.config.get("shelf.editor_width", 420)), int(self.config.get("shelf.editor_height", 360))]})
                self.editors[object_id] = editor
            elif not editor.dirty:
                editor.item = obj
                editor._load()
            editor.show()
            if activate:
                editor.raise_()
                editor.activateWindow()
            self.apply_settings()
        elif obj["kind"] == "image":
            request_desktop("image.open", path=obj["path"])
        elif obj["kind"] == "link":
            request_desktop("browser.open", url=obj["content"])
        elif obj.get("path"):
            file_path = Path(obj["path"])
            if not file_path.exists() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(file_path))):
                self._file_notice(dt("open_failed", self.language))

    def clear_other_selections(self, active):
        for view in self.lists:
            if view is not active:
                view.clearSelection()

    def rename(self, obj):
        text, ok = QInputDialog.getText(self, dt("rename", self.language), dt("title", self.language), text=obj["title"])
        if ok:
            self.store.update(obj["id"], obj["version"], title=text)
            self.refresh()

    def pin_object(self, obj):
        self._allow_pinned_cards = True
        new = self.store.update(obj["id"], obj["version"], pinned=not obj.get("pinned"))
        existing = self.editors.get(obj["id"])
        if existing and not existing.dirty:
            existing.item = new
            existing._load()
        if new.get("pinned") and new["kind"] in {"note", "text", "checklist"}:
            self.open_object(obj["id"])
        self.refresh()

    def move_selected(self, view, target):
        self.move_ids([obj["id"] for obj in view.selected_objects()], target)

    def move_ids(self, ids, target):
        if not isinstance(ids, list) or len(ids) > 1000 or not all(isinstance(oid, str) for oid in ids):
            raise ValueError("Invalid shelf selection")
        for object_id in dict.fromkeys(ids):
            obj = self.store.get(str(object_id))
            self.store.update(obj["id"], obj["version"], shelf=target)
        self.refresh()

    def _refresh_pinned_cards(self, objects):
        if not self.config.get("shelf.enabled", True) or not self._allow_pinned_cards:
            return
        wanted = {o["id"]: o for o in objects if o.get("pinned") and o["kind"] in {"file", "image", "link"}}
        for oid in list(self.pinned_cards):
            if oid not in wanted:
                self.pinned_cards.pop(oid).deleteLater()
        for oid, obj in wanted.items():
            if oid not in self.pinned_cards:
                card = PinnedCard(self, obj)
                self.pinned_cards[oid] = card
                card.show()
            elif not self.pinned_cards[oid].isVisible():
                self.pinned_cards[oid].show()

    def remove_selected(self, view):
        for obj in view.selected_objects():
            editor = self.editors.get(obj["id"])
            if editor and not editor.save():
                continue
            latest = self.store.get(obj["id"])
            if editor:
                editor.hide()
                editor.deleteLater()
                del self.editors[obj["id"]]
            self.store.delete(obj["id"], latest["version"])
        self.refresh()

    def _file_notice(self, text):
        from jarvis.ui.qt_localization import localized_message_box
        localized_message_box(self, QMessageBox.Warning, dt("shelf", self.language),
                              text, QMessageBox.Ok, QMessageBox.Ok, code=self.language)

    def delete_selected_files(self, view):
        """Recycle actual files only after explicit confirmation; never unlink/rmtree.

        Removing a shelf reference remains a separate, non-filesystem action.
        Managed copies refer to their own copied path, never the original source.
        """
        objects = [x for x in view.selected_objects() if x.get("path")]
        if not objects:
            self._file_notice(dt("delete_no_file", self.language))
            return
        from jarvis.ui.qt_localization import localized_message_box
        paths = list(dict.fromkeys(str(x["path"]) for x in objects))
        preview = dt("delete_confirm", self.language) + "\n\n" + "\n".join(paths[:20])
        if len(paths) > 20:
            preview += f"\n… (+{len(paths) - 20})"
        answer = localized_message_box(self, QMessageBox.Warning, dt("delete_disk", self.language),
            preview, QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel, code=self.language)
        if answer != QMessageBox.Yes:
            return
        from send2trash import send2trash
        removed, failures = set(), []
        for raw in paths:
            path = Path(raw)
            # A context-menu file action is not a recursive folder deletion tool.
            if not path.is_file() or path.is_symlink():
                failures.append(raw)
                continue
            try:
                send2trash(str(path))
                if not path.exists():
                    removed.add(raw)
                else:
                    failures.append(raw)
            except (OSError, ValueError):
                failures.append(raw)
        for obj in objects:
            if str(obj.get("path")) in removed:
                latest = self.store.get(obj["id"])
                self.store.delete(obj["id"], latest["version"])
        self.refresh()
        if failures:
            self._file_notice(dt("delete_failed", self.language) + "\n" + "\n".join(failures[:20]))

    def reorder(self, ids, index, board="notes"):
        self.store.place(ids, shelf=self.shelves.currentText() or "default", board=board, index=index)
        self.refresh()

    def import_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, dt("add", self.language))
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
        self.import_mime(mime)

    def import_mime(self, mime, target_shelf=None, board=""):
        try:
            metadata = {"shelf": target_shelf or self.shelves.currentText() or "default", "board": board}
            if mime.hasUrls():
                for url in mime.urls()[:100]:
                    if url.isLocalFile():
                        self.store.import_file(url.toLocalFile(), managed=self.managed.isChecked(), **metadata)
                    elif url.scheme() in {"http", "https"}:
                        self.store.create("link", title=url.host(), content=url.toString(), **metadata)
            elif mime.hasImage():
                image = QImage(mime.imageData())
                if image.isNull() or image.width() * image.height() > 32_000_000:
                    raise ValueError("Image exceeds clipboard pixel limit")
                import uuid
                target = self.store.assets / (uuid.uuid4().hex + ".png")
                if not image.save(str(target), "PNG"):
                    raise OSError("Could not save clipboard image")
                self.store.create("image", title=dt("image", self.language), path=str(target), managed=True, **metadata)
            elif mime.hasText():
                text = mime.text()
                kind = "link" if text.strip().startswith(("https://", "http://")) and "\n" not in text else "text"
                self.store.create(kind, title=text[:60], content=text, **metadata)
            self.refresh()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, dt("error", self.language), str(exc))

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasText() or event.mimeData().hasImage():
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.import_mime(event.mimeData())
        event.acceptProposedAction()

    def show_notes(self, visible: bool):
        for obj in self.store.list():
            if obj["kind"] not in {"note", "text", "checklist"}:
                continue
            if visible and obj.get("pinned"):
                self.open_object(obj["id"])
            elif not visible and obj["id"] in self.editors:
                editor = self.editors[obj["id"]]
                if editor.save():
                    editor.item = self.store.update(editor.item["id"], editor.item["version"], visible=False)
                    editor.hide()

    def shutdown(self):
        for editor in self.editors.values():
            editor.save()
            editor.hide()
        for card in self.pinned_cards.values():
            card.persist_geometry()
            card.hide()
        self.hide()


class PinnedCard(QWidget):
    """Small independent file/link card, persisting after the overall shelf hides."""
    def __init__(self, shelf, item):
        super().__init__(None, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.shelf, self.item = shelf, item
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle(item.get("title", "NEO"))
        self.setMinimumSize(220, 90)
        self.setStyleSheet("QWidget{background:#172536;color:#edf4fc;} QPushButton{background:#24384a;border:1px solid #36546e;border-radius:5px;padding:8px;}")
        layout = QVBoxLayout(self)
        button = QPushButton(item.get("title", ""))
        button.clicked.connect(lambda: shelf.open_object(item["id"]))
        layout.addWidget(button)
        copy_button = QPushButton(dt("copy", shelf.language))
        copy_button.clicked.connect(lambda: QApplication.clipboard().setMimeData(object_mime([shelf.store.get(item["id"])])))
        layout.addWidget(copy_button)
        state = {**item, "size": item.get("size", [280, 110])}
        restore_visible_geometry(self, state)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self.persist_geometry)

    def persist_geometry(self):
        try:
            latest = self.shelf.store.get(self.item["id"])
            self.item = self.shelf.store.update(latest["id"], latest["version"], position=[self.x(), self.y()], size=[self.width(), self.height()], screen=self.screen().name() if self.screen() else "")
        except (KeyError, VersionConflict):
            pass

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, "_save_timer"): self._save_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_save_timer"): self._save_timer.start()

    def closeEvent(self, event):
        self.persist_geometry()
        latest = self.shelf.store.get(self.item["id"])
        self.shelf.store.update(latest["id"], latest["version"], pinned=False)
        event.accept()
