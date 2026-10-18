"""NEO 180: crisp vector icons for the main window buttons.

The 177 icons were drawn once at 64 px and scaled down to 13–16 px, which blurred the thin
strokes on Windows (the gear turned into a smudge).  These icons are drawn by a QIconEngine
for the exact pixel size and screen scale Qt asks for, on a 24-unit grid with strokes that
never get thinner than 1.5 device pixels.  No fonts or emoji are used.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QImage, QPainter, QPainterPath, QPen, QPixmap

KINDS = ("logs", "logs_on", "settings", "minimize", "close", "mic", "mic_muted", "stop", "send")


def _gear_path() -> QPainterPath:
    """Filled gear with eight teeth and a hole, on the 24-unit grid."""
    path = QPainterPath()
    teeth, outer, inner = 8, 10.6, 7.6
    half = math.pi / teeth * 0.46
    points = []
    for i in range(teeth):
        a = i * 2 * math.pi / teeth
        for angle, radius in ((a - half * 1.25, inner), (a - half, outer), (a + half, outer), (a + half * 1.25, inner)):
            points.append(QPointF(12 + math.cos(angle) * radius, 12 + math.sin(angle) * radius))
        b = a + math.pi / teeth
        points.append(QPointF(12 + math.cos(b) * inner, 12 + math.sin(b) * inner))
    path.moveTo(points[0])
    for p in points[1:]:
        path.lineTo(p)
    path.closeSubpath()
    path.addEllipse(QPointF(12, 12), 3.3, 3.3)
    path.setFillRule(Qt.OddEvenFill)
    return path


def draw(painter: QPainter, rect: QRectF, kind: str, color: QColor, accent: QColor | None = None) -> None:
    """Draw ``kind`` into ``rect`` (any size) with ``painter``."""
    side = min(rect.width(), rect.height())
    if side <= 0:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.translate(rect.x() + (rect.width() - side) / 2, rect.y() + (rect.height() - side) / 2)
    unit = side / 24.0
    painter.scale(unit, unit)
    # 2 grid units, but never thinner than 1.5 real pixels.
    width = max(2.0, 1.5 / max(unit, 1e-6))
    pen = QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    accent = QColor(accent) if accent is not None else QColor(color)
    if kind in ("logs", "logs_on"):
        frame = QPen(accent if kind == "logs_on" else color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(frame)
        painter.drawRoundedRect(QRectF(2.5, 4, 19, 16), 3.2, 3.2)
        painter.setPen(pen)
        chevron = QPainterPath(QPointF(7, 9.2))
        chevron.lineTo(10.2, 12)
        chevron.lineTo(7, 14.8)
        painter.drawPath(chevron)
        painter.drawLine(QPointF(12.6, 15.2), QPointF(17, 15.2))
    elif kind == "settings":
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPath(_gear_path())
    elif kind == "minimize":
        painter.drawLine(QPointF(6, 12.5), QPointF(18, 12.5))
    elif kind == "close":
        painter.drawLine(QPointF(6.8, 6.8), QPointF(17.2, 17.2))
        painter.drawLine(QPointF(17.2, 6.8), QPointF(6.8, 17.2))
    elif kind in ("mic", "mic_muted"):
        painter.drawRoundedRect(QRectF(9, 2.8, 6, 11.4), 3, 3)
        arc = QPainterPath(QPointF(5.4, 10.8))
        arc.arcTo(QRectF(5.4, 4.2, 13.2, 13.2), 180, 180)
        painter.drawPath(arc)
        painter.drawLine(QPointF(12, 17.6), QPointF(12, 21))
        painter.drawLine(QPointF(8.8, 21), QPointF(15.2, 21))
        if kind == "mic_muted":
            painter.setPen(QPen(accent, width * 1.1, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawLine(QPointF(4, 3.5), QPointF(20, 20.5))
    elif kind == "stop":
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(QRectF(7, 7, 10, 10), 2, 2)
    elif kind == "send":
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        painter.drawEllipse(QRectF(1.5, 1.5, 21, 21))
        painter.setPen(QPen(QColor("#0C1017"), max(2.2, width), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawLine(QPointF(12, 17), QPointF(12, 7.4))
        head = QPainterPath(QPointF(7.8, 11.4))
        head.lineTo(12, 7.2)
        head.lineTo(16.2, 11.4)
        painter.drawPath(head)
    painter.restore()


class VectorIconEngine(QIconEngine):
    """QIcon backend that redraws the vector for every requested size and scale."""

    def __init__(self, kind: str, color: QColor, accent: QColor | None = None) -> None:
        super().__init__()
        self.kind = kind
        self.color = QColor(color)
        self.accent = QColor(accent) if accent is not None else None

    def _colors(self, mode) -> tuple[QColor, QColor | None]:
        if mode == QIcon.Disabled:
            dim = QColor(self.color)
            dim.setAlpha(110)
            return dim, dim if self.accent is not None else None
        return self.color, self.accent

    def paint(self, painter: QPainter, rect: QRect, mode, state) -> None:  # noqa: N802
        color, accent = self._colors(mode)
        draw(painter, QRectF(rect), self.kind, color, accent)

    def pixmap(self, size: QSize, mode, state) -> QPixmap:  # noqa: N802
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size: QSize, mode, state, scale: float) -> QPixmap:  # noqa: N802
        scale = max(1.0, float(scale or 1.0))
        w, h = max(1, round(size.width() * scale)), max(1, round(size.height() * scale))
        image = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        color, accent = self._colors(mode)
        draw(painter, QRectF(0, 0, w, h), self.kind, color, accent)
        painter.end()
        pix = QPixmap.fromImage(image)
        pix.setDevicePixelRatio(scale)
        return pix

    def clone(self) -> "VectorIconEngine":
        return VectorIconEngine(self.kind, self.color, self.accent)

    def key(self) -> str:
        return "neo-vector-180"


def icon(kind: str, color: QColor | str, accent: QColor | str | None = None) -> QIcon:
    if kind not in KINDS:
        raise ValueError(kind)
    return QIcon(VectorIconEngine(kind, QColor(color), QColor(accent) if accent is not None else None))
