"""Numbered badges over the elements of the active window («покажи номера»).

One transparent, click-through, always-on-top widget per screen; it never takes
focus, so the numbered window stays active. Badges disappear after a timeout or
after a click by number. Coordinates arrive in physical pixels (UI Automation)
and are converted per screen for mixed-DPI setups.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import QWidget


class _ScreenOverlay(QWidget):
    def __init__(self, geometry: QRect) -> None:
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setGeometry(geometry)
        self.marks: list[tuple[int, QPoint]] = []
        self.font_ = QFont('Segoe UI', 10, QFont.Bold)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setFont(self.font_)
        metrics = QFontMetrics(self.font_)
        for number, point in self.marks:
            label = str(number)
            width = max(20, metrics.horizontalAdvance(label) + 10)
            box = QRect(point.x() - 4, point.y() - 4, width, 20)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor('#E0533D'))
            painter.drawRoundedRect(box, 6, 6)
            painter.setPen(QColor('#FFFFFF'))
            painter.drawText(box, Qt.AlignCenter, label)
        painter.end()


def _screen_for(x: int, y: int):
    """Screen holding a physical point; Qt keeps screen origins in native pixels."""
    for screen in QGuiApplication.screens():
        geometry, ratio = screen.geometry(), screen.devicePixelRatio()
        if geometry.x() <= x < geometry.x() + geometry.width() * ratio and geometry.y() <= y < geometry.y() + geometry.height() * ratio:
            return screen
    return QGuiApplication.primaryScreen()


def show_hints(owner, marks: list[dict], ttl: float = 30.0) -> None:
    clear_hints(owner)
    per_screen: dict[object, list[tuple[int, QPoint]]] = {}
    for mark in marks:
        x, y = int(mark.get('x', 0)), int(mark.get('y', 0))
        screen = _screen_for(x, y)
        geometry, ratio = screen.geometry(), screen.devicePixelRatio()
        local = QPoint(int((x - geometry.x()) / ratio), int((y - geometry.y()) / ratio))
        per_screen.setdefault(screen, []).append((int(mark.get('n', 0)), local))
    overlays = []
    for screen, items in per_screen.items():
        overlay = _ScreenOverlay(screen.geometry())
        overlay.marks = items
        overlay.show()
        overlays.append(overlay)
    owner._hint_overlays168 = overlays
    timer = QTimer(owner)
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: clear_hints(owner))
    timer.start(int(max(3.0, min(120.0, float(ttl))) * 1000))
    owner._hint_timer168 = timer


def clear_hints(owner) -> None:
    timer = getattr(owner, '_hint_timer168', None)
    if timer is not None:
        timer.stop()
        timer.deleteLater()
        owner._hint_timer168 = None
    for overlay in getattr(owner, '_hint_overlays168', []) or []:
        try:
            overlay.hide()
            overlay.deleteLater()
        except RuntimeError:
            pass
    owner._hint_overlays168 = []
