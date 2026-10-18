"""Reachable frameless windows; Qt logical coordinates, including negative screens.

This never moves other applications. Editable controls keep text-selection
semantics. Only an explicit caption surface or the empty top edge drags NEO.
"""
from __future__ import annotations
from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget


def reachable_position(widget: QWidget, position: QPoint, pointer: QPoint | None = None) -> QPoint:
    parent = widget.parentWidget()
    if parent is not None and not widget.isWindow():
        return QPoint(max(0, min(position.x(), max(0, parent.width() - widget.width()))),
                      max(0, min(position.y(), max(0, parent.height() - widget.height()))))
    screens = QApplication.screens()
    if not screens:
        return position
    probe = pointer if pointer is not None else position + QPoint(widget.width() // 2, 16)
    screen = QApplication.screenAt(probe)
    if screen is None:
        # Never assume desktop origin is (0,0): a left/top monitor is legitimate.
        screen = min(screens, key=lambda s: (s.availableGeometry().center() - probe).manhattanLength())
    area = screen.availableGeometry()
    return QPoint(max(area.left(), min(position.x(), area.right() - min(widget.width(), area.width()) + 1)),
                  max(area.top(), min(position.y(), area.bottom() - min(widget.height(), area.height()) + 1)))


class CaptionDrag(QObject):
    """Install on a top-level empty 10px band and optional non-editable captions."""
    def __init__(self, target: QWidget, captions=(), finished=None, enabled=None):
        super().__init__(target)
        self.target, self.finished, self.enabled = target, finished, enabled
        self.captions = set(captions)
        self.origin = None
        target.installEventFilter(self)
        for caption in self.captions:
            caption.installEventFilter(self)
            caption.setCursor(Qt.SizeAllCursor)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            if self.enabled is not None and not self.enabled():
                return False
            if watched not in self.captions and event.position().y() > 10:
                return False
            self.origin = (event.globalPosition().toPoint(), self.target.pos())
            return True
        if kind == QEvent.MouseMove and self.origin and event.buttons() & Qt.LeftButton:
            pointer = event.globalPosition().toPoint()
            point = self.origin[1] + pointer - self.origin[0]
            self.target.move(reachable_position(self.target, point, pointer))
            return True
        if kind == QEvent.MouseButtonRelease and self.origin:
            self.origin = None
            if self.finished is not None:
                self.finished()
            return True
        if kind == QEvent.Show and watched is self.target and self.target.isWindow():
            QTimer.singleShot(0, self.ensure_reachable)
        return False

    def ensure_reachable(self):
        if self.target.isVisible():
            point = reachable_position(self.target, self.target.pos())
            if point != self.target.pos():
                self.target.move(point)


class RoundedToolWindow(QWidget):
    """Paint an alpha-backed rounded root, not just a CSS border on a rectangle."""
    def __init__(self, parent=None, flags=Qt.Tool | Qt.FramelessWindowHint):
        super().__init__(parent, flags)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self._caption_drag = CaptionDrag(self)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor('#425b70'), 1))
        painter.setBrush(QColor('#172536'))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 12, 12)
        painter.end()
