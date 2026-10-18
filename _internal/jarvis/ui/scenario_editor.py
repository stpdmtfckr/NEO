"""Lossless scenario editor. Editing/validation never executes a scenario."""
from __future__ import annotations

import json
import uuid
import weakref
from shiboken6 import isValid
from copy import deepcopy
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSizePolicy, QSplitter, QStackedWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget, QHeaderView)

from jarvis.i18n import language_code, translate_widget_tree
from jarvis.ui.qt_localization import LocalizedFileDialog as QFileDialog
from jarvis.ui.responsive178 import WrapCheckBox as QCheckBox, FlowLayout
from jarvis.runtime.scenario_schema import command_argv, step_kind, validate_scenario
from jarvis.runtime.scenarios import (active_scenarios, cancel_scenario, scenario_events,
                                    subscribe_scenarios, unsubscribe_scenarios,
                                    latest_scenario_result, scenario_retry_request)
from jarvis.ui.scenario_strings import install_catalogue, issue_text, st, execution_error_text

install_catalogue()


def _text_widget(widget, key, language):
    widget.setText(st(key, language))
    widget.setProperty("i18n_text_source", st(key, "ru"))
    return widget


def _label(key, language, wrap=False):
    widget = _text_widget(QLabel(), key, language)
    widget.setWordWrap(wrap)
    if wrap:
        widget.setMinimumWidth(0)
        widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
    return widget


def _button(key, language, action):
    widget = _text_widget(QPushButton(), key, language)
    widget.setAutoDefault(False)
    widget.clicked.connect(action)
    return widget


def _policy(language, inherit=False):
    combo = QComboBox()
    combo.setProperty("i18n_skip_combo", True)
    for key in (("inherit", "stop", "continue") if inherit else ("stop", "continue")):
        combo.addItem(st(key, language), key)
    return combo


def _confirm(parent, language, text):
    box = QMessageBox(QMessageBox.Question, st("title", language), text, parent=parent)
    box.setTextFormat(Qt.PlainText)
    yes = box.addButton(st("yes", language), QMessageBox.YesRole)
    no = box.addButton(st("cancel", language), QMessageBox.RejectRole)
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    try:
        box.exec()
        return isValid(box) and box.clickedButton() is yes
    finally:
        if isValid(box):
            box.deleteLater()


class ScenarioStepDialog(QDialog):
    def __init__(self, raw=None, *, language="en", parent=None):
        super().__init__(parent)
        self.language = language
        self.original = deepcopy(raw if isinstance(raw, dict) else {"type": "open"})
        self.result_step = None
        self.setWindowTitle(st("step", language))
        self.setMinimumWidth(420)
        self.resize(600, 490)
        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.kind = QComboBox()
        self.kind.setProperty("i18n_skip_combo", True)
        for key in ("open", "wait", "command"):
            self.kind.addItem(st(key, language), key)
        form.addRow(_label("type", language), self.kind)
        self.enabled = _text_widget(QCheckBox(), "enabled", language)
        self.enabled.setChecked(self.original.get("enabled", True) is not False)
        form.addRow(self.enabled)
        self.step_label = QLineEdit(str(self.original.get("label", "")))
        self.step_label.setMaxLength(200)
        form.addRow(_label("label", language), self.step_label)
        self.policy = _policy(language, inherit=True)
        policy = self.original.get("on_failure", "stop" if self.original.get("required") else "continue" if "required" in self.original else "inherit")
        self.policy.setCurrentIndex(max(0, self.policy.findData(policy)))
        form.addRow(_label("policy", language), self.policy)
        root.addLayout(form)
        self.pages = QStackedWidget()
        root.addWidget(self.pages)
        self.fields = {}
        for kind in ("open", "wait", "command"):
            page = QWidget()
            page_form = QFormLayout(page)
            page_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            page_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
            if kind == "open":
                path_row = QHBoxLayout()
                self.path = QLineEdit(str(self.original.get("path", "")))
                self.path.setMaxLength(8192)
                self.path.setAccessibleName(st("path", language))
                path_row.addWidget(self.path, 1)
                path_row.addWidget(_button("browse", language, self._browse))
                page_form.addRow(_label("path", language), path_row)
                for field, default in (("monitor", 0), ("window_title", ""), ("launch_wait", 0.7)):
                    edit = QLineEdit(str(self.original.get(field, default)))
                    edit.setMaxLength(500 if field == "window_title" else 30)
                    self.fields[field] = edit
                    page_form.addRow(_label(field, language, True), edit)
            elif kind == "wait":
                self.fields["seconds"] = QLineEdit(str(self.original.get("seconds", 0.5)))
                page_form.addRow(_label("seconds", language), self.fields["seconds"])
            else:
                self.command = QPlainTextEdit()
                argv = command_argv(self.original) if "command" in self.original else ["", ""]
                self.command.setPlainText(json.dumps(argv, ensure_ascii=False, indent=2))
                self.command.setAccessibleName(st("argv", language))
                self.command.setMinimumHeight(100)
                page_form.addRow(_label("argv", language, True), self.command)
                self.fields["timeout"] = QLineEdit(str(self.original.get("timeout", 15)))
                page_form.addRow(_label("timeout", language, True), self.fields["timeout"])
                page_form.addRow(_label("argv_help", language, True))
            self.pages.addWidget(page)
        self.kind.currentIndexChanged.connect(self.pages.setCurrentIndex)
        self.kind.setCurrentIndex(max(0, self.kind.findData(step_kind(self.original))))
        self.pages.setCurrentIndex(self.kind.currentIndex())
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setProperty("i18n_skip_text", True)
        root.addWidget(self.error)
        buttons = QDialogButtonBox()
        self.done_button = buttons.addButton(st("save", language), QDialogButtonBox.AcceptRole)
        self.done_button.setDefault(True)
        buttons.addButton(st("cancel", language), QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(self, st("browse", self.language), self.path.text())
        if path:
            self.path.setText(path)

    def accept(self):
        step = deepcopy(self.original)
        kind = self.kind.currentData()
        step.update(type=kind, enabled=self.enabled.isChecked())
        if self.step_label.text() or "label" in step:
            step["label"] = self.step_label.text()
        policy = self.policy.currentData()
        if policy == "inherit":
            step.pop("on_failure", None)
            step.pop("required", None)
        else:
            step["on_failure"] = policy
        if kind == "open":
            step["path"] = self.path.text().strip()
            step["window_title"] = self.fields["window_title"].text().strip()
            fields = ("monitor", "launch_wait")
        elif kind == "wait":
            fields = ("seconds",)
        else:
            try:
                step["command"] = json.loads(self.command.toPlainText())
            except (ValueError, TypeError):
                self.error.setText(st("argv_error", self.language))
                self.command.setFocus()
                return
            fields = ("timeout",)
        for field in fields:
            try:
                step[field] = int(self.fields[field].text()) if field == "monitor" else float(self.fields[field].text())
            except ValueError:
                step[field] = self.fields[field].text()
        report = validate_scenario({"name": "Validation", "steps": [step]})
        # A disabled step can be edited on its own, then used with other steps.
        issues = [x for x in report.issues if x.code != "empty"]
        if issues:
            self.error.setText("\n".join(issue_text(x, self.language) for x in issues[:4]))
            field = self.fields.get(issues[0].field)
            if field is not None:
                field.setFocus()
            return
        self.result_step = step
        super().accept()


class ScenarioEditor(QWidget):
    event_received = Signal(dict)
    run_requested = Signal(str)
    retry_requested = Signal(dict)

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.language = language_code(config)
        rows = config.get("scenarios.items", []) or []
        self._items = deepcopy(rows) if isinstance(rows, list) else []
        self._collection_error = not isinstance(rows, list)
        # IDs are assigned once per draft and do not change on Apply.
        for raw in self._items:
            if isinstance(raw, dict) and not raw.get("id"):
                raw["id"] = uuid.uuid4().hex
        self._original_items = deepcopy(self._items)
        self._original_by_id = {str(raw.get("id", "")): deepcopy(raw) for raw in self._items if isinstance(raw, dict)}
        self._current = -1
        self._loading = False
        self._run_id = ""
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(_label("intro", self.language, True))
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        root.addWidget(self.splitter, 1)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 4, 0)
        self.list = QListWidget()
        self.list.setMinimumWidth(140)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.setAccessibleName(st("title", self.language))
        self.list.currentRowChanged.connect(self._select)
        left_layout.addWidget(self.list, 1)
        actions = QGridLayout()
        self.new_button = _button("new", self.language, self.add_scenario)
        self.duplicate_button = _button("duplicate", self.language, self.duplicate_scenario)
        self.delete_button = _button("delete", self.language, self.delete_scenario)
        actions.addWidget(self.new_button, 0, 0, 1, 2)
        actions.addWidget(self.duplicate_button, 1, 0)
        actions.addWidget(self.delete_button, 1, 1)
        left_layout.addLayout(actions)
        self.template_choice = QComboBox()
        self.template_choice.setProperty("i18n_skip_combo", True)
        self.template_choice.setAccessibleName(st("template", self.language))
        for kind in ("work", "game", "study"):
            self.template_choice.addItem(st("template_" + kind, self.language), kind)
        left_layout.addWidget(self.template_choice)
        self.template_button = _button("add_template", self.language, self.add_template)
        left_layout.addWidget(self.template_button)
        self.splitter.addWidget(left)
        self.details = QWidget()
        detail = QVBoxLayout(self.details)
        detail.setContentsMargins(4, 0, 0, 0)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.name = QLineEdit(); self.name.setMaxLength(200)
        self.phrase = QLineEdit(); self.phrase.setMaxLength(500)
        self.aliases = QPlainTextEdit(); self.aliases.setMaximumHeight(68)
        self.name.setProperty("i18n_skip_text", True)
        self.phrase.setProperty("i18n_skip_text", True)
        for field in ("name", "phrase", "aliases"):
            widget = getattr(self, field)
            widget.setAccessibleName(st(field, self.language))
            form.addRow(_label(field, self.language, True), widget)
            widget.textChanged.connect(self._metadata_changed)
        self.enabled = _text_widget(QCheckBox(), "enabled", self.language)
        self.enabled.toggled.connect(self._metadata_changed)
        form.addRow(self.enabled)
        self.policy = _policy(self.language)
        self.policy.currentIndexChanged.connect(self._metadata_changed)
        form.addRow(_label("policy", self.language), self.policy)
        self.max_seconds = QLineEdit()
        self.max_seconds.setMaximumWidth(100)
        self.max_seconds.textChanged.connect(self._metadata_changed)
        form.addRow(_label("max_seconds", self.language), self.max_seconds)
        detail.addLayout(form)
        self.template_help = QLabel()
        self.template_help.setTextFormat(Qt.PlainText)
        self.template_help.setWordWrap(True)
        self.template_help.setProperty("i18n_skip_text", True)
        detail.addWidget(self.template_help)
        self.steps = QTreeWidget()
        self.steps.setHeaderLabels([st("step", self.language), st("summary", self.language), st("policy", self.language)])
        self.steps.setRootIsDecorated(False)
        self.steps.setUniformRowHeights(True)
        self.steps.setSelectionMode(QAbstractItemView.SingleSelection)
        self.steps.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.steps.setMinimumHeight(145)
        self.steps.setWordWrap(False)
        self.steps.setAccessibleName(st("steps", self.language))
        self.steps.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.steps.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.steps.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.steps.itemDoubleClicked.connect(lambda *_: self.edit_step())
        self.steps.itemSelectionChanged.connect(self._step_buttons)
        detail.addWidget(self.steps, 1)
        buttons = QGridLayout()
        self.add_step_button = _button("add_step", self.language, self.add_step)
        self.edit_step_button = _button("edit", self.language, self.edit_step)
        self.delete_step_button = _button("delete", self.language, self.delete_step)
        self.up_button = _button("up", self.language, lambda: self.move_step(-1))
        self.down_button = _button("down", self.language, lambda: self.move_step(1))
        for i, button in enumerate((self.add_step_button, self.edit_step_button, self.delete_step_button)):
            buttons.addWidget(button, 0, i)
        buttons.addWidget(self.up_button, 1, 0)
        buttons.addWidget(self.down_button, 1, 1)
        detail.addLayout(buttons)
        self.splitter.addWidget(self.details)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)
        # Start in the minimum-width layout; the first real resize expands it.
        self._narrow = True
        self.splitter.setOrientation(Qt.Vertical)
        self.list.setMaximumHeight(120)
        self.list.setMinimumHeight(70)
        self.splitter.widget(0).setMaximumHeight(280)
        self.splitter.setSizes([180, 520])
        root.addWidget(_label("safety", self.language, True))
        row = FlowLayout()
        self.preview_button = _button("preview", self.language, self.preview_current)
        row.addWidget(self.preview_button)
        self.validate_button = _button("validate", self.language, self.validate_current)
        self.cancel_button = _button("cancel_run", self.language, self.cancel_run)
        self.cancel_button.setEnabled(False)
        self.run_button = _button("run", self.language, self.request_run)
        row.addWidget(self.validate_button); row.addWidget(self.run_button); row.addWidget(self.cancel_button)
        root.addLayout(row)
        self.status = _label("idle", self.language, True)
        self.status.setProperty("i18n_skip_text", True)
        root.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1); self.progress.setValue(0)
        self.progress.setAccessibleName(st("log", self.language))
        root.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        self.log.setMaximumHeight(112)
        self.log.setAccessibleName(st("log", self.language))
        root.addWidget(self.log)
        self.result_summary = QLabel()
        self.result_summary.setTextFormat(Qt.PlainText)
        self.result_summary.setWordWrap(True)
        self.result_summary.setProperty("i18n_skip_text", True)
        root.addWidget(self.result_summary)
        retry_row = QHBoxLayout()
        self.failed_steps = QComboBox()
        self.failed_steps.setProperty("i18n_skip_combo", True)
        self.failed_steps.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.failed_steps.setMinimumContentsLength(6)
        self.failed_steps.setMinimumWidth(0)
        self.failed_steps.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.failed_steps.currentTextChanged.connect(self.failed_steps.setToolTip)
        self.failed_steps.setAccessibleName(st("retry", self.language))
        self.failed_steps.currentIndexChanged.connect(self._update_retry_button)
        retry_row.addWidget(self.failed_steps, 1)
        self.retry_button = _button("retry", self.language, self.request_retry)
        retry_row.addWidget(self.retry_button)
        root.addLayout(retry_row)
        self.retry_help = _label("retry_session", self.language, True)
        self.retry_help.setProperty("i18n_skip_text", True)
        root.addWidget(self.retry_help)
        for key, widget, action in (("Alt+Up", self.steps, lambda: self.move_step(-1)),
                                    ("Alt+Down", self.steps, lambda: self.move_step(1)),
                                    ("Delete", self.steps, self.delete_step),
                                    ("Return", self.steps, self.edit_step),
                                    ("Delete", self.list, self.delete_scenario)):
            shortcut = QShortcut(QKeySequence(key), widget)
            shortcut.setContext(Qt.WidgetShortcut)
            shortcut.activated.connect(action)
        self._rebuild_list(0 if self._items else -1)
        self.event_received.connect(self._on_event)
        ref = weakref.ref(self)
        def listener(event):
            editor = ref()
            if editor is not None:
                try: editor.event_received.emit(event)
                except RuntimeError: pass
        self._listener = listener
        subscribe_scenarios(listener)
        self.destroyed.connect(lambda: unsubscribe_scenarios(listener))
        self._show_recent_events()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Only cross a layout breakpoint; never rebuild widgets or global QSS.
        narrow = self.width() < 640
        if narrow == getattr(self, "_narrow", False) or not hasattr(self, "splitter"):
            return
        self._narrow = narrow
        self.splitter.setOrientation(Qt.Vertical if narrow else Qt.Horizontal)
        self.list.setMaximumHeight(120 if narrow else 16777215)
        self.list.setMinimumHeight(70 if narrow else 0)
        self.splitter.widget(0).setMaximumHeight(280 if narrow else 16777215)
        self.splitter.setSizes([180, 520] if narrow else [190, 650])

    def _raw(self):
        if 0 <= self._current < len(self._items) and isinstance(self._items[self._current], dict):
            return self._items[self._current]
        return None

    def _rebuild_list(self, selected):
        self.list.blockSignals(True)
        self.list.clear()
        for raw in self._items:
            item = QListWidgetItem()
            self.list.addItem(item)
            self._list_label(item, raw)
        self.list.setCurrentRow(selected)
        self.list.blockSignals(False)
        self._select(selected)

    def _list_label(self, item, raw):
        name = str(raw.get("name", "")) if isinstance(raw, dict) else st("unsupported", self.language)
        suffix = "" if isinstance(raw, dict) and raw.get("enabled", True) else " · " + st("disabled", self.language)
        item.setText(name + suffix)
        item.setToolTip(name + suffix)
        item.setData(Qt.AccessibleTextRole, name + suffix)

    def _select(self, row):
        self._current = row
        raw = self._raw()
        self.details.setEnabled(raw is not None)
        self.duplicate_button.setEnabled(raw is not None)
        self.delete_button.setEnabled(row >= 0)
        self.validate_button.setEnabled(raw is not None)
        self._loading = True
        raw = raw or {}
        self.name.setText(str(raw.get("name", "")))
        self.phrase.setText(str(raw.get("phrase", "")))
        aliases = raw.get("aliases", [])
        self.aliases.setPlainText("\n".join(str(x) for x in aliases) if isinstance(aliases, list) else str(aliases))
        self.enabled.setChecked(raw.get("enabled", True) is not False)
        self.policy.setCurrentIndex(max(0, self.policy.findData(raw.get("on_failure", "continue"))))
        self.max_seconds.setText(str(raw.get("max_seconds", 300)))
        self._loading = False
        self.template_help.setText(st("template_help", self.language) if raw.get("template") else "")
        self.template_help.setVisible(bool(raw.get("template")))
        self._rebuild_steps()
        if hasattr(self, "log"):
            self._show_recent_events()

    def _metadata_changed(self, *_):
        raw = self._raw()
        if self._loading or raw is None:
            return
        # Update only the changed field: untouched aliases/unknown data stay exact.
        source = self.sender()
        if source is self.name:
            raw["name"] = self.name.text()
        elif source is self.phrase:
            raw["phrase"] = self.phrase.text()
        elif source is self.aliases:
            raw["aliases"] = [x.strip() for x in self.aliases.toPlainText().splitlines() if x.strip()]
        elif source is self.enabled:
            raw["enabled"] = self.enabled.isChecked()
        elif source is self.policy:
            raw["on_failure"] = self.policy.currentData()
            self._rebuild_steps()
        elif source is self.max_seconds:
            try: raw["max_seconds"] = float(self.max_seconds.text())
            except ValueError: raw["max_seconds"] = self.max_seconds.text()
        self._list_label(self.list.item(self._current), raw)
        self._update_run_button()

    def _unique_name(self, name):
        used = {str(v).strip().casefold() for x in self._items if isinstance(x, dict)
                for v in [x.get("name", ""), x.get("phrase", ""), *(x.get("aliases", []) if isinstance(x.get("aliases", []), list) else [])]}
        candidate, n = name[:200], 2
        while candidate.casefold() in used:
            candidate = name[:190] + f" {n}"
            n += 1
        return candidate

    def add_scenario(self):
        self._items.append({"id": uuid.uuid4().hex, "name": self._unique_name(st("new_name", self.language)),
                            "phrase": "", "aliases": [], "enabled": True, "on_failure": "stop", "steps": []})
        self._rebuild_list(len(self._items) - 1)
        self.name.setFocus(); self.name.selectAll()

    def add_template(self):
        from jarvis.runtime.scenario_templates184 import make_template
        draft = make_template(self.template_choice.currentData(), self.language)
        draft["name"] = self._unique_name(draft["name"])
        self._items.append(draft)
        self._rebuild_list(len(self._items) - 1)
        self.status.setText(st("template_help", self.language))
        self.name.setFocus()

    def duplicate_scenario(self):
        raw = self._raw()
        if raw is None:
            return
        copied = deepcopy(raw)
        copied.update(id=uuid.uuid4().hex, name=self._unique_name(st("copy_name", self.language, name=raw.get("name", ""))), phrase="", aliases=[])
        self._items.append(copied)
        self._rebuild_list(len(self._items) - 1)
        self.name.setFocus(); self.name.selectAll()

    def delete_scenario(self):
        if self._current < 0:
            return
        raw = self._raw() or {}
        if not _confirm(self, self.language, st("delete_confirm", self.language, name=raw.get("name", ""))):
            return
        index = self._current
        self._items.pop(index)
        self._rebuild_list(min(index, len(self._items) - 1))

    def _step_rows(self):
        raw = self._raw() or {}
        rows = raw.get("steps", [])
        return rows if isinstance(rows, list) else []

    def _rebuild_steps(self, selected=0):
        self.steps.clear()
        raw = self._raw() or {}
        for index, step in enumerate(self._step_rows(), 1):
            if isinstance(step, dict):
                kind = step_kind(step)
                summary = str(step.get("path", "")) if kind == "open" else str(step.get("seconds", 0.5)) + " s" if kind == "wait" else json.dumps(step.get("command", []), ensure_ascii=False)
                if step.get("label"):
                    summary = str(step["label"]) + " · " + summary
                policy = step.get("on_failure", "stop" if step.get("required") else "continue" if "required" in step else raw.get("on_failure", "continue"))
                title = f"{index}. {st(kind, self.language)}"
                if step.get("enabled", True) is False:
                    title += " · " + st("disabled", self.language)
            else:
                title, summary, policy = str(index), st("unsupported", self.language), "stop"
            item = QTreeWidgetItem([title, summary, st(policy, self.language)])
            for col in range(3):
                item.setToolTip(col, item.text(col))
            self.steps.addTopLevelItem(item)
        if self.steps.topLevelItemCount():
            self.steps.setCurrentItem(self.steps.topLevelItem(min(max(selected, 0), self.steps.topLevelItemCount() - 1)))
        self._step_buttons()
        self._update_run_button()

    def _step_index(self):
        return self.steps.indexOfTopLevelItem(self.steps.currentItem())

    def _step_buttons(self):
        n, count = self._step_index(), self.steps.topLevelItemCount()
        self.edit_step_button.setEnabled(n >= 0)
        self.delete_step_button.setEnabled(n >= 0)
        self.up_button.setEnabled(n > 0)
        self.down_button.setEnabled(0 <= n < count - 1)
        raw = self._raw()
        self.add_step_button.setEnabled(raw is not None and isinstance(raw.get("steps", []), list) and count < 100)

    def add_step(self):
        raw = self._raw()
        if raw is None or not isinstance(raw.get("steps", []), list) or len(self._step_rows()) >= 100:
            return
        dialog = ScenarioStepDialog(language=self.language, parent=self)
        try:
            if dialog.exec() == QDialog.Accepted and isValid(self):
                raw.setdefault("steps", []).append(deepcopy(dialog.result_step))
                self._rebuild_steps(len(raw["steps"]) - 1)
        finally:
            if isValid(dialog):
                dialog.deleteLater()

    def edit_step(self):
        index = self._step_index()
        if index < 0:
            return
        step = self._step_rows()[index]
        if not isinstance(step, dict) or step_kind(step) not in ("open", "wait", "command"):
            self.status.setText(st("unsupported", self.language))
            return
        dialog = ScenarioStepDialog(step, language=self.language, parent=self)
        try:
            if dialog.exec() == QDialog.Accepted and isValid(self):
                self._step_rows()[index] = deepcopy(dialog.result_step)
                self._rebuild_steps(index)
        finally:
            if isValid(dialog):
                dialog.deleteLater()

    def delete_step(self):
        index = self._step_index()
        if index >= 0 and _confirm(self, self.language, st("delete_step", self.language)):
            self._step_rows().pop(index)
            self._rebuild_steps(index)

    def move_step(self, offset):
        index, rows = self._step_index(), self._step_rows()
        if 0 <= index < len(rows) and 0 <= index + offset < len(rows):
            rows[index], rows[index + offset] = rows[index + offset], rows[index]
            self._rebuild_steps(index + offset)
            self.steps.setFocus()

    def _run_blocker186(self) -> str:
        """186: the specific reason a run is unavailable ("" = runnable).

        save_first only for a really unsaved draft; a saved but disabled scenario
        asks to be enabled, an invalid one to be fixed."""
        raw = self._raw()
        if raw is None:
            return "fix_invalid"
        saved = self.config.get("scenarios.items", []) or []
        if not isinstance(saved, list):
            return "save_first"
        matches = [row for row in saved if isinstance(row, dict) and row.get("id") == raw.get("id")]
        # Reject a dirty draft before scanning its step definitions on every keypress.
        if len(matches) != 1 or raw != matches[0]:
            return "save_first"
        if raw.get("enabled", True) is False:
            return "enable_first"
        return "" if validate_scenario(raw).valid else "fix_invalid"

    def _saved_and_runnable(self):
        return self._run_blocker186() == ""

    def _update_run_button(self):
        if not hasattr(self, "run_button"):
            return
        active = any(event.get("state") not in ("completed", "failed", "partial", "cancelled")
                     for event in active_scenarios(self.config))
        blocker = self._run_blocker186()
        ready = not blocker and not self._run_id and not active
        self.run_button.setEnabled(ready)
        key = "busy" if active else "run_safely" if ready else (blocker or "busy")
        self.run_button.setToolTip(st(key, self.language))
        self.run_button.setProperty("i18n_tooltip_source", st(key, "ru"))
        self._update_retry_button()

    def request_run(self):
        blocker = self._run_blocker186()
        if not blocker and not self._run_id:
            self.run_requested.emit(str(self._raw()["id"]))
        else:
            self.status.setText(st(blocker or "busy", self.language))

    def preview_current(self):
        raw = self._raw()
        if raw is None:
            return
        from jarvis.ui.scenario_confirmation184 import ScenarioConfirmationDialog
        dialog = ScenarioConfirmationDialog(self, deepcopy(raw), self.language, review_only=True)
        try:
            dialog.exec()
        finally:
            if isValid(dialog):
                dialog.deleteLater()

    def _refresh_results(self):
        if not hasattr(self, "failed_steps"):
            return
        record = latest_scenario_result(self.config, (self._raw() or {}).get("id", ""))
        previous = self.failed_steps.currentData()
        self.failed_steps.blockSignals(True)
        self.failed_steps.clear()
        self.result_summary.setText(st("result_summary", self.language, **record["summary"]) if record and record.get("summary") else "")
        if record:
            for outcome in record["outcomes"]:
                if not outcome.get("success") and not outcome.get("not_run"):
                    index = outcome["step_index"]
                    title = f"{st('step', self.language)} {index} · {st(outcome.get('type', ''), self.language)}"
                    if outcome.get("opened"):
                        title += " · " + st("result_opened", self.language)
                    self.failed_steps.addItem(title, (record["run_id"], index))
        selected = self.failed_steps.findData(previous)
        if selected >= 0:
            self.failed_steps.setCurrentIndex(selected)
        self.failed_steps.blockSignals(False)
        self.failed_steps.setToolTip(self.failed_steps.currentText())
        self._update_retry_button()

    def _update_retry_button(self, *_):
        if not hasattr(self, "retry_button"):
            return
        reason, ready = "retry_session", False
        data = self.failed_steps.currentData()
        if data:
            try:
                scenario_retry_request(self.config, *data)
                active = any(e.get("state") not in ("completed", "failed", "partial", "cancelled") for e in active_scenarios(self.config))
                ready = self._saved_and_runnable() and not self._run_id and not active
                if not ready:
                    reason = "busy" if active else "save_first"
            except ValueError as exc:
                reason = str(exc)
        self.retry_button.setEnabled(ready)
        self.retry_help.setText(st(reason, self.language))
        self.retry_button.setToolTip(st(reason, self.language))

    def request_retry(self):
        self._update_retry_button()
        if not self.retry_button.isEnabled():
            return
        try:
            args = scenario_retry_request(self.config, *self.failed_steps.currentData())
        except ValueError as exc:
            self.status.setText(st(str(exc), self.language))
            return
        self.retry_requested.emit(args)
        self._update_retry_button()

    def validate_current(self):
        raw = self._raw()
        if raw is None:
            return False
        report = validate_scenario(raw)
        text = st("valid", self.language, count=report.step_count) if report.valid else "\n".join(issue_text(x, self.language) for x in report.issues)
        self.status.setText(st("valid", self.language, count=report.step_count) if report.valid else st("invalid", self.language))
        self.log.setPlainText(text)
        return report.valid

    def items(self) -> list[dict[str, Any]]:
        """Return a validated deep copy for ConfigManager's atomic Settings save."""
        if self._collection_error:
            raise ValueError(st("object", self.language))
        # An unrelated Settings Apply must not rewrite or reject legacy rows.
        # Runtime validation still refuses malformed/unknown steps before actions.
        if self._items == self._original_items:
            return deepcopy(self._items)
        seen_ids, names = set(), {}
        for index, raw in enumerate(self._items):
            if not isinstance(raw, dict):
                self.list.setCurrentRow(index)
                raise ValueError(st("object", self.language))
            sid = str(raw.get("id", "")).strip().casefold()
            if sid in seen_ids:
                self.list.setCurrentRow(index)
                raise ValueError(st("duplicate_id", self.language))
            seen_ids.add(sid)
            # Disabled drafts can be saved without discarding unsupported legacy steps.
            changed = raw != self._original_by_id.get(str(raw.get("id", "")))
            if changed and raw.get("enabled", True) is not False:
                report = validate_scenario(raw)
                if not report.valid:
                    self.list.setCurrentRow(index)
                    self.validate_current()
                    raise ValueError(str(raw.get("name", "")) + "\n" + "\n".join(issue_text(x, self.language) for x in report.issues[:6]))
            aliases = raw.get("aliases", [])
            for name in [raw.get("name", ""), raw.get("phrase", ""), *(aliases if isinstance(aliases, list) else [])]:
                value = str(name).strip().casefold()
                if not value:
                    continue
                if value in names and names[value] != index:
                    self.list.setCurrentRow(index)
                    raise ValueError(st("ambiguous", self.language, name=name))
                names[value] = index
        return deepcopy(self._items)

    def _matches_event(self, event):
        raw = self._raw() or {}
        scope = str(getattr(getattr(self.config, "paths", None), "root", id(self.config)))
        return (event.get("scope") == scope and
                (str(raw.get("id", "")) == event.get("scenario_id") or
                 (not event.get("scenario_id") or event.get("scenario_id") == str(raw.get("name", "")).casefold())))

    def _show_recent_events(self):
        self.log.clear()
        self._run_id = ""
        self.cancel_button.setEnabled(False)
        self.status.setText(st("idle", self.language) if self._raw() else st("empty_list", self.language))
        self.progress.setRange(0, 1); self.progress.setValue(0)
        for event in scenario_events(self.config):
            if self._matches_event(event):
                self._on_event(event)
        for event in active_scenarios(self.config):
            if self._matches_event(event):
                self._run_id = event["run_id"]
                self.cancel_button.setEnabled(True)
        self._refresh_results()
        self._update_run_button()

    def _on_event(self, event):
        if not self._matches_event(event):
            self._update_run_button()
            return
        state = event.get("state", "")
        terminal = state in ("completed", "failed", "partial", "cancelled")
        self._run_id = "" if terminal else event.get("run_id", "")
        self.cancel_button.setEnabled(bool(self._run_id))
        index, total = int(event.get("step_index", 0)), int(event.get("total", 1))
        state_text = st("failed" if state == "step_finished" and event.get("success") is False else state, self.language)
        line = f"{index}/{total} · {state_text} · {event.get('elapsed', 0):.1f} s"
        self.status.setText(line)
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(max(0, index - 1) if state == "step_started" else index)
        error = execution_error_text(event.get("error"), self.language)
        if error:
            line += " · " + error[:1000]
        placement = event.get("placement") or {}
        placement_error = execution_error_text(placement.get("error"), self.language)
        if placement_error and placement_error != error:
            line += " · " + placement_error[:1000]
        if terminal and event.get("summary"):
            line += "\n" + st("result_summary", self.language, **event["summary"])
            for outcome in event.get("outcomes", []):
                key = ("result_not_run" if outcome.get("not_run") else "step_skipped" if outcome.get("skipped")
                       else "result_done" if outcome.get("success") else "failed")
                line += f"\n{st('step', self.language)} {outcome.get('step_index', 0)} · {st(key, self.language)}"
                if outcome.get("opened") and not outcome.get("success"):
                    line += " · " + st("result_opened", self.language)
        if event.get("opened") and event.get("success") is False:
            line += " · " + st("result_opened", self.language)
        self.log.appendPlainText(line)
        if hasattr(self, "_refresh_results"):
            self._refresh_results()
        self._update_run_button()

    def cancel_run(self):
        if self._run_id and cancel_scenario(self._run_id):
            self.status.setText(st("cancel_requested", self.language))
            self.cancel_button.setEnabled(False)

    def retranslate(self):
        self.language = language_code(self.config)
        translate_widget_tree(self, self.language)
        for index in range(self.template_choice.count()):
            self.template_choice.setItemText(index, st("template_" + self.template_choice.itemData(index), self.language))
        for index in range(self.policy.count()):
            self.policy.setItemText(index, st(self.policy.itemData(index), self.language))
        self.steps.setHeaderLabels([st("step", self.language), st("summary", self.language), st("policy", self.language)])
        self._rebuild_list(self._current)
