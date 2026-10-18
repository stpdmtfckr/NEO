"""Transactional OBS preferences. Network access is confined to explicit Check."""
from __future__ import annotations

import math
import os
from copy import deepcopy

from PySide6.QtCore import QObject, Qt, Slot, QThreadPool, QEvent
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget, QSizePolicy

from jarvis.integrations.obs_text184 import tr
from jarvis.ui.responsive178 import WrapCheckBox
from jarvis.workers import FunctionWorker


class _WrappedCheckBox(WrapCheckBox):
    """Recompute the minimum height even if a translation does not resize us."""

    def setText(self, text):  # noqa: N802
        super().setText(text)
        self._refresh_height()

    def _refresh_height(self):
        self._own178 = True
        try:
            self.setMinimumHeight(self.heightForWidth(max(1, self.width())))
        finally:
            self._own178 = False
        self.updateGeometry()

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._refresh_height()


class _WrappedLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextFormat(Qt.PlainText)
        self.setProperty('i18n_skip_text', True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.setMinimumWidth(0)

    def _refresh_height(self):
        self.setMinimumHeight(max(0, self.heightForWidth(max(1, self.width()))))
        self.updateGeometry()

    def setText(self, text):  # noqa: N802
        super().setText(text)
        self._refresh_height()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._refresh_height()

    def changeEvent(self, event):  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._refresh_height()


def _check_saved_settings(config):
    # This entire function runs in the pool: even service construction can do I/O.
    from jarvis.integrations.obs_service184 import get_obs_service
    return get_obs_service(config).status()


class _StatusRelay(QObject):
    """QObject receiver keeps all widget work on the GUI thread."""

    def __init__(self, page, generation):
        super().__init__(page)
        self.page = page
        self.generation = generation

    @Slot(object)
    def result(self, value):
        self.page._receive_status(self.generation, value)

    @Slot(str)
    def error(self, _trace):
        # Never display a transport traceback, which may contain credentials.
        self.page._receive_failure(self.generation)

    @Slot()
    def finished(self):
        self.page._check_finished(self)
        self.deleteLater()


class StreamingSettingsPage(QWidget):
    def __init__(self, config, parent=None, *, pool=None):
        super().__init__(parent)
        self.config = config
        self.language = str(config.get('app.language', 'en'))
        self._pool = pool
        self._generation = 0
        self._closed = False
        self._worker = None
        self._relay = None
        self._forget_pending = False
        self._status = None
        self._status_key = 'unchecked'
        self._texts = []
        self._fields = []
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        self.enabled = self._checkbox('enabled_label', 'enabled')
        self.host = QLineEdit('127.0.0.1')
        self.host.setReadOnly(True)
        self._field('host_label', self.host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        try:
            self.port.setValue(int(config.get('streaming.port', 4455)))
        except (ValueError, TypeError, OverflowError):
            self.port.setValue(4455)
        self._field('port_label', self.port)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setMaxLength(4096)
        self._field('password_label', self.password)
        self._label('password_help')
        self.remember = self._checkbox('remember_label')
        self.remember.setEnabled(os.name == 'nt')
        self._label('dpapi_only')
        self.forget_button = self._button('forget')
        self.forget_button.clicked.connect(self._stage_forget)
        self.credential_status = _WrappedLabel()
        layout.addWidget(self.credential_status)
        self.executable = QLineEdit(str(config.get('streaming.executable', '') or ''))
        self._field('executable_label', self.executable)
        self.auto_launch = self._checkbox('launch_label', 'auto_launch_on_game')
        self.auto_replay = self._checkbox('replay_label', 'auto_replay_on_game')
        self.microphone = QLineEdit(str(config.get('streaming.microphone_input', '') or ''))
        self._field('microphone_label', self.microphone)
        self.scene = QLineEdit(str(config.get('streaming.preferred_scene', '') or ''))
        self._field('scene_label', self.scene)
        self._label('names_help')
        self._label('setup_help')
        self.check_button = self._button('check')
        self.check_button.clicked.connect(self.check_saved_settings)
        self._label('check_help')
        self.status_label = _WrappedLabel()
        layout.addWidget(self.status_label)
        layout.addStretch(1)
        self.password.textChanged.connect(self._password_changed)
        self.retranslate()

    def _label(self, key):
        label = _WrappedLabel()
        self._texts.append((label, key))
        self.layout().addWidget(label)
        return label

    def _field(self, key, field):
        self._label(key).setBuddy(field)
        field.setMinimumWidth(0)
        field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._fields.append((field, key))
        self.layout().addWidget(field)

    def _checkbox(self, key, setting=None):
        checkbox = _WrappedCheckBox()
        checkbox.setProperty('i18n_skip_text', True)
        checkbox.setChecked(bool(self.config.get('streaming.' + setting, False)) if setting else False)
        self._texts.append((checkbox, key))
        self.layout().addWidget(checkbox)
        return checkbox

    def _button(self, key):
        button = QPushButton()
        button.setAutoDefault(False)
        button.setProperty('i18n_skip_text', True)
        self._texts.append((button, key))
        self.layout().addWidget(button)
        return button

    def retranslate(self):
        self.language = str(self.config.get('app.language', 'en'))
        for widget, key in self._texts:
            caption = tr(key, self.language)
            widget.setText(caption)
            widget.setAccessibleName(caption)
        for field, key in self._fields:
            field.setAccessibleName(tr(key, self.language))
        self.password.setPlaceholderText(tr('password_hint', self.language))
        self.credential_status.setText(tr('forget_pending', self.language) if self._forget_pending else '')
        self._render_status()
        self.layout().invalidate()
        self.updateGeometry()

    def _stage_forget(self):
        self.password.clear()
        self.remember.setChecked(False)
        self._forget_pending = True
        self.credential_status.setText(tr('forget_pending', self.language))

    def _password_changed(self, text):
        if text and self._forget_pending:
            self._forget_pending = False
            self.credential_status.clear()

    def prepare_save(self):
        """Return staged settings and a session update; do not mutate live state."""
        self.invalidate_status()
        saved = self.config.get('streaming', {})
        values = deepcopy(saved) if isinstance(saved, dict) else {}
        values.update(enabled=self.enabled.isChecked(), host='127.0.0.1', port=self.port.value(),
                      executable=self.executable.text(), auto_launch_on_game=self.auto_launch.isChecked(),
                      auto_replay_on_game=self.auto_replay.isChecked(), microphone_input=self.microphone.text(),
                      preferred_scene=self.scene.text())
        # Stored DPAPI ciphertext stays opaque. Never decode credentials into widgets.
        token = self.config.get('streaming.password_encrypted', '')
        values['password_encrypted'] = token if type(token) is str and token.startswith('dpapi:') and token[6:].strip() else ''
        password_update = None
        if self._forget_pending:
            values['password_encrypted'] = ''
            password_update = ''
        if self.password.text():
            password_update = self.password.text()
            values['password_encrypted'] = ''
            if self.remember.isChecked():
                if os.name != 'nt':
                    raise ValueError(tr('dpapi_only', self.language))
                try:
                    from jarvis.secrets import protect_secret
                    token = protect_secret(password_update)
                    if type(token) is not str or not token.startswith('dpapi:') or not token[6:].strip():
                        token = None
                except Exception:
                    # Provider errors can contain the plaintext input. Do not
                    # retain, chain, log or forward that exception to the dialog.
                    token = None
                if token is None:
                    raise ValueError(tr('credential_store_failed', self.language)) from None
                values['password_encrypted'] = token
        return values, password_update

    def save_succeeded(self, password_update):
        """Call only after config.save succeeded. Setter is memory-only."""
        if password_update is not None:
            from jarvis.integrations.obs_service184 import set_session_password
            set_session_password(self.config, password_update)
        self.password.clear()
        self.remember.setChecked(False)
        self._forget_pending = False
        self.credential_status.clear()
        self.invalidate_status()

    def invalidate_status(self):
        self._generation += 1
        self._status = None
        self._status_key = 'unchecked'
        self._render_status()

    @Slot()
    def check_saved_settings(self):
        if self._closed or self._worker is not None:
            return
        self._generation += 1
        self._status = None
        self._status_key = 'busy'
        self._render_status()
        relay = _StatusRelay(self, self._generation)
        worker = FunctionWorker(_check_saved_settings, self.config)
        worker.signals.result.connect(relay.result, Qt.QueuedConnection)
        worker.signals.error.connect(relay.error, Qt.QueuedConnection)
        worker.signals.finished.connect(relay.finished, Qt.QueuedConnection)
        self._worker = worker
        self._relay = relay
        self.check_button.setEnabled(False)
        try:
            (self._pool or QThreadPool.globalInstance()).start(worker)
        except Exception:
            self._receive_failure(self._generation)
            self._check_finished(relay)
            relay.deleteLater()

    def _receive_status(self, generation, value):
        if self._closed or generation != self._generation:
            return
        self._status = dict(value) if isinstance(value, dict) else None
        self._status_key = 'protocol_error'
        self._render_status()

    def _receive_failure(self, generation):
        if self._closed or generation != self._generation:
            return
        self._status = None
        self._status_key = 'connection_failed'
        self._render_status()

    def _check_finished(self, relay):
        # Apply invalidates results but retains the slot until the old job ends.
        if self._relay is relay:
            self._worker = None
            self._relay = None
            if not self._closed:
                self.check_button.setEnabled(True)

    def _state_text(self, value):
        return tr('active' if value is True else 'inactive' if value is False else 'unknown', self.language)

    def _duration_text(self, duration):
        if type(duration) in (float, int) and math.isfinite(duration) and duration >= 0:
            seconds = int(duration)
            return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'
        return tr('unknown', self.language)

    def _render_status(self):
        if not hasattr(self, 'status_label'):
            return
        state = self._status
        if state is None:
            self.status_label.setText(tr(self._status_key, self.language))
            return
        connection = state.get('connected')
        heading = tr('connected' if connection is True else 'disconnected' if connection is False else 'unknown', self.language)
        recording = tr('paused', self.language) if state.get('recording') is True and state.get('recording_paused') is True else self._state_text(state.get('recording'))
        duration_text = self._duration_text(state.get('duration_seconds'))
        dropped = state.get('dropped_frames')
        dropped_text = str(dropped) if type(dropped) is int and dropped >= 0 else tr('unknown', self.language)
        scene = state.get('scene')
        scene_text = scene if isinstance(scene, str) and scene else tr('unknown', self.language)
        summary = tr('status_summary', self.language, recording=recording, streaming=self._state_text(state.get('streaming')),
                     replay=self._state_text(state.get('replay_active')), duration=duration_text, dropped=dropped_text, scene=scene_text)
        microphone = tr('microphone_status', self.language, state=self._state_text(state.get('microphone_muted')))
        recording_detail = tr('recording_detail', self.language, paused=self._state_text(state.get('recording_paused')),
                              duration=self._duration_text(state.get('recording_duration_seconds')))
        error = state.get('error')
        self.status_label.setText('\n'.join(part for part in (heading, summary, recording_detail, microphone, error if isinstance(error, str) else '') if part))

    @Slot()
    def close_operations(self, *_args):
        self._closed = True
        self._generation += 1
        self.password.clear()
        self.remember.setChecked(False)
        self._forget_pending = False

    def closeEvent(self, event):  # noqa: N802
        self.close_operations()
        super().closeEvent(event)
