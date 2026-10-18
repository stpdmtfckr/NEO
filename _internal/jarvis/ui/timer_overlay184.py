"""Bounded timer-stage presentation and an explicit, live, ID-bound timer list.

The existing MainWindow timer tick drives refresh; no extra polling thread/timer.
Large overlay fonts are preserved. A compact button stays inside a small stage.
"""
from __future__ import annotations
import math
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QPushButton, QToolButton, QVBoxLayout

_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_TEXT = {
 'all': ('Все таймеры', 'All timers', 'Alle Timer', 'Tous les minuteurs', 'Todos los temporizadores', 'Tutti i timer', 'Todos os temporizadores'),
 'cancel': ('Отменить выбранный', 'Cancel selected', 'Ausgewählten abbrechen', 'Annuler la sélection', 'Cancelar seleccionado', 'Annulla selezionato', 'Cancelar selecionado'),
 'empty': ('Активных таймеров нет.', 'No active timers.', 'Keine aktiven Timer.', 'Aucun minuteur actif.', 'No hay temporizadores activos.', 'Nessun timer attivo.', 'Nenhum temporizador ativo.'),
 'cancelled': ('Таймер отменён.', 'Timer cancelled.', 'Timer abgebrochen.', 'Minuteur annulé.', 'Temporizador cancelado.', 'Timer annullato.', 'Temporizador cancelado.'),
 'failed': ('Не удалось сохранить отмену. Таймер остаётся активным.', 'Could not save cancellation. The timer remains active.', 'Abbruch nicht gespeichert. Der Timer bleibt aktiv.', 'Annulation non enregistrée. Le minuteur reste actif.', 'No se pudo guardar la cancelación. El temporizador sigue activo.', 'Impossibile salvare l’annullamento. Il timer resta attivo.', 'Não foi possível guardar o cancelamento. O temporizador continua ativo.'),
 'close': ('Закрыть', 'Close', 'Schließen', 'Fermer', 'Cerrar', 'Chiudi', 'Fechar'),
}


def text(key, code):
    return _TEXT[key][_LANGS.index(code if code in _LANGS else 'en')]


def countdown(manager, item):
    remaining = manager.remaining(item)
    hours, rem = divmod(max(0, int(math.ceil(remaining))), 3600)
    minutes, seconds = divmod(rem, 60)
    return f'{hours:d}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:d}:{seconds:02d}'


class TimerListDialog(QDialog):
    def __init__(self, parent, manager, code, on_changed):
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.manager, self.code, self.on_changed = manager, code, on_changed
        self.rows = QListWidget(self)
        self.rows.setAccessibleName(text('all', code))
        self.notice = QLabel(self)
        self.notice.setTextFormat(Qt.PlainText)
        self.notice.setWordWrap(True)
        self.cancel_button = QPushButton(self)
        self.cancel_button.clicked.connect(self.cancel_selected)
        self.rows.itemSelectionChanged.connect(lambda: self.cancel_button.setEnabled(self.rows.currentItem() is not None))
        self.buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        for widget in (self.rows, self.notice, self.cancel_button, self.buttons):
            layout.addWidget(widget)
        self.resize(520, 360)
        self.refresh(manager.list(), code)

    def refresh(self, items, code):
        self.code = code
        self.setWindowTitle(text('all', code))
        self.cancel_button.setText(text('cancel', code))
        self.buttons.button(QDialogButtonBox.Close).setText(text('close', code))
        selected = self.rows.currentItem()
        selected_id = selected.data(Qt.UserRole) if selected else None
        existing = {self.rows.item(i).data(Qt.UserRole): self.rows.item(i) for i in range(self.rows.count())}
        wanted = {item.id for item in items}
        # In-place updates preserve scrolling; cancellation binds to an ID, never
        # a display index that changes when another timer expires or is removed.
        for index in range(self.rows.count()-1, -1, -1):
            if self.rows.item(index).data(Qt.UserRole) not in wanted:
                self.rows.takeItem(index)
        for item in items:
            row = existing.get(item.id)
            if row is None:
                row = QListWidgetItem(self.rows)
                row.setData(Qt.UserRole, item.id)
            content = f'{item.label} — {countdown(self.manager, item)} [{item.id}]'
            row.setText(content)
            row.setToolTip(content)
        if selected_id not in wanted:
            self.rows.setCurrentItem(None)
        self.cancel_button.setEnabled(self.rows.currentItem() is not None)
        if not items:
            self.notice.setText(text('empty', code))
        elif self.notice.text() == text('empty', code):
            self.notice.clear()

    def cancel_selected(self):
        row = self.rows.currentItem()
        if row is None:
            return
        timer_id = str(row.data(Qt.UserRole) or '')
        if not timer_id:
            return
        try:
            removed = self.manager.cancel(timer_id)
            self.notice.setText(text('cancelled', self.code) if removed else text('empty', self.code))
        except OSError:
            self.notice.setText(text('failed', self.code))
        self.refresh(self.manager.list(), self.code)
        self.on_changed()


class TimerListButton(QToolButton):
    def __init__(self, parent, on_changed):
        super().__init__(parent)
        self.on_changed = on_changed
        self.dialog = None
        self.manager = None
        self.code = 'en'
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.clicked.connect(self.open_list)

    def open_list(self):
        if self.manager is None:
            return
        if self.dialog is None:
            dialog = TimerListDialog(self.window(), self.manager, self.code, self.on_changed)
            self.dialog = dialog
            dialog.destroyed.connect(lambda *_: setattr(self, 'dialog', None))
        self.dialog.refresh(self.manager.list(), self.code)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def refresh(self, manager, items, code):
        self.manager, self.code = manager, code
        caption = f'{text("all", code)} ({len(items)})'
        self.setText(caption)
        self.setToolTip(caption)
        self.setAccessibleName(caption)
        if self.dialog is not None:
            self.dialog.refresh(items, code)


def refresh_overlay(window):
    label, button, robot = window.timer_stage_label, window.timer_list_button184, window.robot
    manager = getattr(window, '_timer_manager', None)
    if manager is None:
        label.hide(); button.hide()
        return
    items = manager.list()
    code = window._language_code()
    button.refresh(manager, items, code)
    if not items or not window.config.get('ui.timer_overlay_enabled', True):
        label.hide(); button.hide()
        return
    margin = max(8, int(10*getattr(window, '_scale', 1.)))
    width, height = max(1, robot.width()-2*margin), max(1, robot.height()-2*margin)
    button_height = min(height, button.fontMetrics().height()+12)
    button_width = min(width, button.fontMetrics().horizontalAdvance(button.text())+22)
    button.setFixedSize(button_width, button_height)
    button.setText(button.fontMetrics().elidedText(button.text(), Qt.ElideRight, max(1, button_width-14)))
    gap = 4
    label_budget = max(0, height-button_height-gap)
    label.setMaximumSize(width, max(1, label_budget))
    metrics = label.fontMetrics()
    lines = []
    for item in items[:4]:
        suffix = ' — '+countdown(manager, item)
        if metrics.horizontalAdvance(suffix)+24 > width:
            break
        name = metrics.elidedText(str(item.label), Qt.ElideRight, max(1, width-metrics.horizontalAdvance(suffix)-24))
        lines.append(name+suffix)
    while lines:
        label.setText('\n'.join(lines))
        if label.sizeHint().height() <= label_budget:
            break
        lines.pop()
    if lines:
        label.adjustSize(); label.show(); label.raise_()
    else:
        label.hide()
    label_height = label.height() if lines else 0
    group_height = label_height+(gap if lines else 0)+button_height
    position = str(window.config.get('ui.timer_overlay_position', 'bottom_left') or 'bottom_left')
    top = margin if position.startswith('top_') else max(margin, (robot.height()-group_height)//2) if position.startswith('center_') or position=='center' else max(margin, robot.height()-group_height-margin)
    def left(widget):
        if position.endswith('_right'):
            return max(margin, robot.width()-widget.width()-margin)
        if position.endswith('_center') or position=='center':
            return max(margin, (robot.width()-widget.width())//2)
        return margin
    if lines:
        label.move(left(label), top)
    button.move(left(button), top+label_height+(gap if lines else 0))
    button.show(); button.raise_()
