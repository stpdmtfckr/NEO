"""Small, local-only Settings extensions for model choices and explicit user facts."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout)

from jarvis.i18n import language_code
from jarvis.runtime import memory169
from jarvis.runtime.quality_memory_text184 import ROWS, install, text

# Verified against https://ollama.com/library/qwen2.5/tags on 2026-10-05.
# These are optional model tags, never automatic hardware-selected profiles.
MODEL_CHOICES = {'qwen2.5:0.5b': 'fast', 'qwen2.5:1.5b': 'balanced',
                 'qwen2.5:3b': 'quality_3', 'qwen2.5:7b': 'quality_7'}
install()


def _label(key, code, **values):
    label = QLabel(text(key, code, **values))
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setProperty('i18n_text_source', ROWS[key][0])
    label.setProperty('i18n_text_values', values)
    return label


def restore_quality_choices(combo, code='en'):
    for model, key in MODEL_CHOICES.items():
        if combo.findText(model) < 0:
            combo.addItem(model)
        combo.setItemData(combo.findText(model), text(key, code), Qt.ToolTipRole)


def append_quality_choices(dialog, form):
    combo = dialog.chat_model
    selected = combo.currentText()
    restore_quality_choices(combo, language_code(dialog.config))
    # Adding options never selects a preset in place of a trained/custom model.
    combo.setCurrentText(selected)
    code = language_code(dialog.config)
    dialog.quality_help184 = _label('custom', code)
    dialog.quality_help184.setObjectName('help')
    form.addRow(_label('quality', code), dialog.quality_help184)
    help_label = _label('quality_help', code)
    help_label.setObjectName('help')
    form.addRow(help_label)

    def update(*_):
        code = language_code(dialog.config)
        key = MODEL_CHOICES.get(combo.currentText().strip(), 'custom')
        label = dialog.quality_help184
        label.setProperty('i18n_text_source', ROWS[key][0])
        label.setText(text(key, code))
        for model, key in MODEL_CHOICES.items():
            index = combo.findText(model)
            if index >= 0:
                combo.setItemData(index, text(key, code), Qt.ToolTipRole)
    combo.currentTextChanged.connect(update)
    dialog.settings_saved.connect(update)
    update()


class FactEditDialog(QDialog):
    def __init__(self, database, config, row, parent=None, *, edits_enabled=lambda: True):
        super().__init__(parent)
        self.database, self.config, self.row = database, config, dict(row)
        self.edits_enabled = edits_enabled
        self.code = language_code(config)
        self.setWindowTitle(text('edit', self.code))
        self.setMinimumWidth(340)
        self.resize(560, 340)
        kind, name, value = memory169._decode(row)
        layout = QVBoxLayout(self)
        layout.addWidget(_label('edit_help', self.code, limit=memory169.NOTE_MAX if kind == 'note' else memory169.VALUE_MAX))
        form = QFormLayout()
        name_field = QLineEdit(text('note', self.code) if kind == 'note' else name)
        name_field.setReadOnly(True)
        form.addRow(text('name', self.code), name_field)
        self.value = QPlainTextEdit(value)
        # Qt displays CR/CRLF paragraph breaks as LF. An untouched note (including
        # edits undone back to the initial display) must retain its original text.
        # Edited text uses Qt's LF representation verbatim; never diff legacy text.
        self._original_note_value = value if kind == 'note' else None
        self._initial_note_display = self._note_editor_text() if kind == 'note' else None
        form.addRow(text('value', self.code), self.value)
        layout.addLayout(form)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row_buttons = QHBoxLayout()
        row_buttons.addStretch(1)
        self.save_button = QPushButton(text('save', self.code))
        self.save_button.setObjectName('primaryButton')
        self.save_button.clicked.connect(self.save)
        cancel = QPushButton(text('cancel', self.code))
        cancel.clicked.connect(self.reject)
        row_buttons.addWidget(self.save_button)
        row_buttons.addWidget(cancel)
        layout.addLayout(row_buttons)

    def _note_editor_text(self):
        # toPlainText also changes NBSP to regular space. Read the raw document
        # instead and normalize only Qt's paragraph/line separator characters.
        return self.value.document().toRawText().replace('\u2029', '\n').replace('\u2028', '\n')

    def save(self):
        if not self.edits_enabled() or not memory169.enabled(self.config):
            self.status.setText(text('off', self.code))
            return
        self.save_button.setEnabled(False)
        try:
            if self._original_note_value is not None:
                value = self._note_editor_text()
                if value == self._initial_note_display:
                    value = self._original_note_value
            else:
                value = self.value.toPlainText()
            ok, message = memory169.edit_user_fact(self.database, self.config, self.row, value, self.code)
        except Exception:
            ok, message = False, text('failed', self.code)
        if ok:
            self.accept()
        else:
            self.status.setText(message)
            self.save_button.setEnabled(True)


class MemoryFactsDialog(QDialog):
    changed = Signal()

    def __init__(self, database, config, parent=None, *, edits_enabled=lambda: True):
        super().__init__(parent)
        self.database, self.config = database, config
        self.edits_enabled = edits_enabled
        self.code = language_code(config)
        self.rows = []
        self._loaded = False
        self._busy = False
        self.setWindowTitle(text('facts', self.code))
        self.resize(720, 520)
        self.setMinimumSize(380, 360)
        root = QVBoxLayout(self)
        root.addWidget(_label('scope', self.code))
        self.off_label = _label('off', self.code)
        root.addWidget(self.off_label)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([text('name', self.code), text('value', self.code)])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.itemSelectionChanged.connect(self._buttons)
        self.table.itemDoubleClicked.connect(lambda *_: self.edit_selected())
        root.addWidget(self.table, 1)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        actions = QHBoxLayout()
        self.edit_button = QPushButton(text('edit', self.code))
        self.edit_button.clicked.connect(self.edit_selected)
        self.delete_button = QPushButton(text('delete', self.code))
        self.delete_button.clicked.connect(self.delete_selected)
        self.refresh_button = QPushButton(text('refresh', self.code))
        self.refresh_button.clicked.connect(self.reload)
        for button in (self.edit_button, self.delete_button, self.refresh_button):
            actions.addWidget(button)
        actions.addStretch(1)
        root.addLayout(actions)
        bottom = QHBoxLayout()
        self.clear_button = QPushButton(text('delete_all', self.code))
        self.clear_button.clicked.connect(self.delete_all)
        bottom.addWidget(self.clear_button)
        bottom.addStretch(1)
        close = QPushButton(text('close', self.code))
        close.clicked.connect(self.reject)
        bottom.addWidget(close)
        root.addLayout(bottom)
        self.reload()

    def _can_edit(self):
        return memory169.enabled(self.config) and bool(self.edits_enabled())

    def _buttons(self):
        selected = self.table.currentRow() >= 0 and self._loaded and not self._busy
        self.edit_button.setEnabled(selected and self._can_edit())
        self.delete_button.setEnabled(selected)
        self.clear_button.setEnabled(bool(self.rows) and self._loaded and not self._busy)
        self.refresh_button.setEnabled(not self._busy)
        self.off_label.setVisible(not self._can_edit())

    def reload(self):
        try:
            rows = memory169.user_fact_rows(self.database)
        except Exception:
            self._loaded = False
            self.status.setText(text('load_failed', self.code))
            self._buttons()
            return False
        self._loaded = False
        self.table.setRowCount(0)
        self.rows = rows
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            kind, name, value = memory169._decode(row)
            self.table.setItem(i, 0, QTableWidgetItem(text('note', self.code) if kind == 'note' else name))
            self.table.setItem(i, 1, QTableWidgetItem(value))
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self._loaded = True
        self.status.setText(text('count', self.code, count=len(rows)))
        self._buttons()
        return True

    def edit_selected(self):
        index = self.table.currentRow()
        if not self._loaded or self._busy or not self._can_edit() or index < 0:
            return
        self._busy = True
        self._buttons()
        try:
            editor = FactEditDialog(self.database, self.config, self.rows[index], self, edits_enabled=self._can_edit)
            try:
                changed = editor.exec() == QDialog.Accepted
            finally:
                editor.deleteLater()
            if changed:
                self.changed.emit()
                self.reload()
        finally:
            self._busy = False
            self._buttons()

    def _delete(self, rows, *, all_shown=False):
        if not rows or self._busy or not self._loaded:
            return
        self._busy = True
        self._buttons()
        try:
            prompt = text('confirm_all', self.code, count=len(rows)) if all_shown else text('confirm_one', self.code)
            box = QMessageBox(QMessageBox.Question, text('delete', self.code), prompt,
                              QMessageBox.Yes | QMessageBox.No, self)
            box.setDefaultButton(QMessageBox.No)
            box.button(QMessageBox.Yes).setText(text('delete', self.code))
            box.button(QMessageBox.No).setText(text('cancel', self.code))
            try:
                confirmed = box.exec() == QMessageBox.Yes
            finally:
                box.deleteLater()
            if not confirmed:
                return
            try:
                ok, message = memory169.delete_user_facts(self.database, rows, self.code)
            except Exception:
                ok, message = False, text('failed', self.code)
            if ok:
                self.changed.emit()
                self.reload()
            else:
                self.status.setText(message)
        finally:
            self._busy = False
            self._buttons()

    def delete_selected(self):
        index = self.table.currentRow()
        if index >= 0:
            self._delete([dict(self.rows[index])])

    def delete_all(self):
        self._delete([dict(row) for row in self.rows], all_shown=True)


def open_memory_editor(dialog):
    """Use the active profile DB; never reset/recreate the application profile."""
    owner = dialog.parent()
    agent = getattr(owner, 'agent', None)
    database = getattr(agent, 'database', None)
    owned = database is None
    try:
        if owned:
            from jarvis.database import Database
            database = Database(dialog.config.paths.database, prune_history=False)
        editor = MemoryFactsDialog(database, dialog.config, dialog,
                                   edits_enabled=dialog.memory_enabled169.isChecked)
        try:
            editor.exec()
        finally:
            editor.deleteLater()
    except Exception:
        QMessageBox.warning(dialog, text('facts', language_code(dialog.config)),
                            text('load_failed', language_code(dialog.config)))
    finally:
        if owned and database is not None:
            database.close()


def append_memory_editor(dialog, form):
    button = QPushButton(ROWS['manage'][0])
    button.setProperty('i18n_text_source', ROWS['manage'][0])
    button.clicked.connect(lambda: open_memory_editor(dialog))
    dialog.memory_editor_button184 = button
    form.addRow(button)
