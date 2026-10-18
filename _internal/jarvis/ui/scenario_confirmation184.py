"""Scrollable plain-text approval, with cancellation as the default action."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog,QDialogButtonBox,QLabel,QPlainTextEdit,QVBoxLayout
from jarvis.runtime.scenario_authority184 import scenario_preview
from jarvis.ui.scenario_strings import st, issue_text
from jarvis.runtime.scenario_schema import failure_policy, step_kind, validate_scenario

def consequence_preview(definition, language='en', retry=None):
    report = validate_scenario(definition)
    if not report.valid:
        return '\n'.join([st('invalid', language), st('preview_help', language), *[issue_text(issue, language) for issue in report.issues]])
    rows = definition.get('steps', [])
    enabled = [s for s in rows if isinstance(s, dict) and s.get('enabled', True) is not False]
    lines = [st('preview_help', language)]
    if definition.get('enabled', True) is False:
        lines.append(st('disabled', language))
    if retry and retry.get("mode") == "placement":
        return st("retry_placement", language)
    if retry:
        index = retry['step_index']
        enabled = [rows[index - 1]]
        lines.extend([st('retry_step_only', language, step=index), st('retry_session', language)])
        # The caller supplies the verified mode solely for explaining the effect;
        # runtime authority always comes from its own record.
        lines.append(st('retry_' + retry.get('mode', 'open'), language))
    if any(step_kind(s) == 'open' for s in enabled) and not (retry and retry.get('mode') == 'placement'):
        lines.append(st('consequence_open', language))
    if any(step_kind(s) == 'command' for s in enabled):
        lines.append(st('consequence_command', language))
    seconds = sum(float(s.get('seconds', .5)) for s in enabled if step_kind(s) == 'wait' and isinstance(s.get('seconds', .5), (int, float)))
    lines.append(st('consequence_wait', language, seconds=f'{seconds:g}'))
    lines.append(st('consequence_policy', language, policy=st(definition.get('on_failure', 'continue'), language)))
    for index, step in enumerate(rows, 1):
        if isinstance(step, dict):
            lines.append(f"{index}. {st(step_kind(step), language)} · {st('enabled' if step.get('enabled', True) else 'disabled', language)} · {st(failure_policy(definition, step), language)}")
    return '\n'.join(lines)


def draft_definition_preview(definition):
    try:
        return scenario_preview(definition)
    except (ValueError, TypeError, OverflowError):
        # Invalid draft values are displayed for repair, never approved/executed.
        # ASCII escaping exposes bidi/zero-width controls even in the fallback.
        import json
        return json.dumps(definition, ensure_ascii=True, indent=2, default=str)


class ScenarioConfirmationDialog(QDialog):
    def __init__(self, parent, definition, language='en', *, retry=None, review_only=False):
        super().__init__(parent)
        config = getattr(parent, "config", None)
        def color(key, default):
            value = QColor(str(config.get("ui." + key, default) or default)) if config is not None else QColor(default)
            return value.name() if value.isValid() else default
        window = color("window_color", "#0C1017")
        panel = color("panel_color", "#111722")
        text = color("text_color", "#EAF2FF")
        border = color("border_color", "#57D7FF")
        element = color("element_color", "#192333")
        hover = color("element_hover_color", "#243247")
        # A parent foreground alone leaves native dialog/editor surfaces white.
        self.setStyleSheet(f"""
            QDialog {{ background: {window}; color: {text}; }}
            QLabel {{ background: transparent; color: {text}; }}
            QPlainTextEdit {{ background: {panel}; color: {text}; border: 1px solid {border}; }}
            QPushButton {{ background: {element}; color: {text}; border: 1px solid {border}; padding: 6px 14px; }}
            QPushButton:hover {{ background: {hover}; }}
            QPushButton:default, QPushButton:focus {{ border: 2px solid {border}; }}
        """)
        self.setWindowTitle(st('preview' if review_only else 'retry_review' if retry else 'confirm_title', language))
        area = self.screen().availableGeometry()
        self.resize(min(700, max(320, area.width() - 40)), min(520, max(240, area.height() - 40)))
        self.setMinimumSize(320, 240)
        layout = QVBoxLayout(self)
        heading = (st("retry_placement", language) if retry and retry.get("mode") == "placement" else
                   st('retry_step_only', language, step=retry['step_index']) if retry else
                   st('preview_help', language) if review_only else st('confirm_steps', language))
        self.heading = QLabel(heading)
        self.heading.setTextFormat(Qt.PlainText)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlainText(consequence_preview(definition, language, retry) + '\n\n' + draft_definition_preview(definition) + ('\n\n' + scenario_preview({'retry_request': retry}) if retry else ''))
        self.preview.setAccessibleName(st('confirm_title', language))
        layout.addWidget(self.preview, 1)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Yes | QDialogButtonBox.No)
        self.approve = self.buttons.button(QDialogButtonBox.Yes)
        self.approve.setText(st('retry' if retry else 'run', language))
        self.approve.setVisible(not review_only)
        self.approve.setEnabled(validate_scenario(definition).valid and definition.get("enabled", True) is not False
                                and (not retry or retry.get("mode") in ("open", "wait")))
        self.approve.setAutoDefault(False)
        self.cancel = self.buttons.button(QDialogButtonBox.No)
        self.cancel.setText(st('cancel', language))
        self.cancel.setDefault(True)
        self.cancel.setFocus()
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

def confirm_scenario(parent, definition, language='en', *, retry=None):
    dialog = ScenarioConfirmationDialog(parent, definition, language, retry=retry)
    try:
        return dialog.exec() == QDialog.Accepted
    finally:
        dialog.deleteLater()
