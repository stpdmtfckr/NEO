from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QMovie
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget
from jarvis.ui.qt_localization import LocalizedFileDialog as QFileDialog

from jarvis.ui.desktop_strings import dt
from jarvis.ui.gif_assets import import_gif
from jarvis.ui.robot_widget import RobotWidget


class GifSettingsDialog(QDialog):
    def __init__(self, root, mapping: dict, scale: int, language: str, parent=None):
        super().__init__(parent)
        self.root, self.language, self.mapping = root, language, deepcopy(mapping)
        self.movie = None
        self.setWindowTitle(dt("gif", language))
        self.resize(720, 540)
        layout = QVBoxLayout(self)
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setFixedHeight(130)
        layout.addWidget(self.preview)
        self.scale = QSpinBox()
        self.scale.setRange(20, 180)
        self.scale.setValue(scale)
        self.scale.setSuffix(" %")
        form = QHBoxLayout()
        scale_label = QLabel(dt("scale", language))
        # 186: the visible caption is also the semantic label of the field.
        scale_label.setBuddy(self.scale)
        self.scale.setAccessibleName(dt("scale", language))
        self.scale.setAccessibleDescription(dt("gif_scale_help", language))
        form.addWidget(scale_label)
        form.addWidget(self.scale)
        layout.addLayout(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        rows = QVBoxLayout(content)
        for state in sorted(RobotWidget.STATES):
            row = QHBoxLayout()
            state_name = dt("state_" + state, language)
            label = QLabel(state_name)
            label.setMinimumWidth(85)
            edit = QLineEdit(str(self.mapping.get(state, "")))
            edit.setReadOnly(True)
            label.setBuddy(edit)
            # 186: the user file name stays the value; the state is the label.
            edit.setAccessibleName(dt("gif_path_for", language).format(state=state_name))
            button = QPushButton(dt("load", language))
            clear = QPushButton(dt("clear", language))
            button.setAccessibleName(dt("gif_load_for", language).format(state=state_name))
            clear.setAccessibleName(dt("gif_clear_for", language).format(state=state_name))
            button.setAccessibleDescription(button.accessibleName())
            clear.setAccessibleDescription(clear.accessibleName())
            button.clicked.connect(lambda _checked=False, key=state, field=edit: self.load(key, field))
            clear.clicked.connect(lambda _checked=False, key=state, field=edit: (self.mapping.pop(key, None), field.clear()))
            for w in (label, edit, button, clear):
                row.addWidget(w)
            rows.addLayout(row)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Cancel).setText(dt("cancel", language))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def load(self, state, field):
        path, _ = QFileDialog.getOpenFileName(self, dt("load", self.language), "", "GIF (*.gif)")
        if not path:
            return
        try:
            managed, info = import_gif(path, self.root)
            self.mapping[state] = managed
            field.setText(managed)
            if self.movie:
                self.movie.stop()
                self.movie.deleteLater()
            self.movie = QMovie(managed, parent=self)
            self.movie.setCacheMode(QMovie.CacheNone)
            self.movie.setScaledSize(QSize(info["width"], info["height"]).scaled(240, 130, Qt.KeepAspectRatio))
            self.movie.setSpeed(min(100, int(info["min_delay_ms"] / 17 * 100)))
            self.preview.setMovie(self.movie)
            self.movie.start()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, dt("error", self.language), str(exc))

    def hideEvent(self, event):
        if self.movie:
            self.movie.setPaused(True)
        super().hideEvent(event)
