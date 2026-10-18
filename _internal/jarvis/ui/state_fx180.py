"""NEO 180: what the robot shows next to itself in each state — rewritten.

The 158–177 effects were flat line drawings (a gear, a magnifier, dots, a «?» from a font).
These are soft, lit and moving: every effect has a glow layer (cached sprites, cheap to blit)
and motion with ease-in/ease-out, so states read at a glance and changes look natural.
Coordinates are in robot units around the head centre (cx, cy), multiplied by ``scale``.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient

from jarvis.ui.ambience180 import _blit, _glow, _smooth


def _pen(color: QColor, width: float, alpha: int | None = None) -> QPen:
    c = QColor(color)
    if alpha is not None:
        c.setAlpha(max(0, min(255, alpha)))
    return QPen(c, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)


def _glow_stroke(p: QPainter, path: QPainterPath, color: QColor, width: float, alpha: int = 230) -> None:
    """A stroke with a soft halo: wide faint pass + crisp core."""
    p.setBrush(Qt.NoBrush)
    p.setPen(_pen(color, width * 3.2, int(alpha * 0.16)))
    p.drawPath(path)
    p.setPen(_pen(color, width * 1.8, int(alpha * 0.28)))
    p.drawPath(path)
    p.setPen(_pen(color.lighter(125), width, alpha))
    p.drawPath(path)


def _sparkle(p: QPainter, x: float, y: float, size: float, color: QColor, alpha: int) -> None:
    inner = size * 0.2
    path = QPainterPath(QPointF(x, y - size))
    for px, py in ((x + inner, y - inner), (x + size, y), (x + inner, y + inner), (x, y + size),
                   (x - inner, y + inner), (x - size, y), (x - inner, y - inner)):
        path.lineTo(px, py)
    path.closeSubpath()
    c = QColor(color).lighter(130); c.setAlpha(alpha)
    p.setPen(Qt.NoPen); p.setBrush(c)
    p.drawPath(path)


def listening(p, t, cx, cy, s, color):
    halo = _glow(color, 48)
    for side in (-1.0, 1.0):
        ex, ey = cx + side * 140.0 * s, cy - 4.0 * s
        for k in range(3):
            phase = (t * 0.8 + k / 3.0) % 1.0
            r = (14.0 + 26.0 * phase) * s
            a = int(200 * (1.0 - phase) ** 1.6)
            arc = QPainterPath()
            start = 120 if side < 0 else -60
            arc.arcMoveTo(QRectF(ex - r, ey - r, 2 * r, 2 * r), start)
            arc.arcTo(QRectF(ex - r, ey - r, 2 * r, 2 * r), start, 120)
            _glow_stroke(p, arc, color, max(0.9, 1.6 * s), a)
        # sound flowing in: small motes drift towards the ear
        for k in range(5):
            u = (t * 0.9 + k / 5.0) % 1.0
            mx = ex + side * (62.0 - 52.0 * _smooth(u)) * s
            my = ey + math.sin(u * math.pi * 2 + k) * 10.0 * s * (1.0 - u)
            _blit(p, halo, mx, my, 4.0 * s, None, 0.75 * math.sin(math.pi * u))


def speaking(p, t, cx, cy, s, color):
    x, y = cx + 132.0 * s, cy + 6.0 * s
    for k in range(3):
        phase = (t * 1.2 + k / 3.0) % 1.0
        r = (9.0 + 24.0 * phase) * s
        arc = QPainterPath()
        arc.arcMoveTo(QRectF(x - r, y - r, 2 * r, 2 * r), -50)
        arc.arcTo(QRectF(x - r, y - r, 2 * r, 2 * r), -50, 100)
        _glow_stroke(p, arc, color, max(0.9, 1.7 * s), int(190 * (1.0 - phase) ** 1.4))


def thinking(p, t, cx, cy, s, color):
    """Three lit orbs circle in a tilted orbit above the head; thought bubbles rise to it."""
    halo = _glow(color, 48)
    ox, oy = cx + 118.0 * s, cy - 70.0 * s
    for k in range(3):
        a = t * 2.1 + k * math.tau / 3
        depth = 0.5 + 0.5 * math.sin(a)
        x = ox + math.cos(a) * 22.0 * s
        y = oy + math.sin(a) * 8.0 * s
        r = (2.4 + 2.0 * depth) * s
        _blit(p, halo, x, y, r * 4.0, None, 0.35 + 0.45 * depth)
        c = QColor(color).lighter(140); c.setAlpha(int(150 + 100 * depth))
        p.setPen(Qt.NoPen); p.setBrush(c)
        p.drawEllipse(QPointF(x, y), r, r)
    ring = QPainterPath()
    ring.addEllipse(QPointF(ox, oy), 22.0 * s, 8.0 * s)
    p.setBrush(Qt.NoBrush); p.setPen(_pen(color, max(0.6, 1.0 * s), 60))
    p.drawPath(ring)
    for k in range(3):
        u = (t * 0.55 + k / 3.0) % 1.0
        bx = cx + (88.0 + 22.0 * u) * s
        by = cy - (26.0 + 32.0 * u) * s
        r = (2.0 + 3.5 * u) * s
        a = int(200 * math.sin(math.pi * u))
        p.setPen(_pen(color, max(0.7, 1.2 * s), a))
        p.setBrush(QColor(color.red(), color.green(), color.blue(), a // 5))
        p.drawEllipse(QPointF(bx, by), r, r)


def _gear_path(teeth: int, outer: float, root: float) -> QPainterPath:
    path = QPainterPath()
    for i in range(teeth * 4):
        a = i / (teeth * 4.0) * math.tau
        r = outer if i % 4 in (1, 2) else root
        pt = QPointF(math.cos(a) * r, math.sin(a) * r)
        path.moveTo(pt) if i == 0 else path.lineTo(pt)
    path.closeSubpath()
    path.addEllipse(QPointF(0, 0), root * 0.38, root * 0.38)
    path.setFillRule(Qt.OddEvenFill)
    return path


def working(p, t, cx, cy, s, color, screen: QColor):
    """Two meshing metal gears with a lit rim and sparks where the teeth meet."""
    big, small = 24.0 * s, 14.0 * s
    bx, by = cx + 122.0 * s, cy + 40.0 * s
    sx, sy = bx + (big + small - 2.0 * s) * 0.80, by - (big + small - 2.0 * s) * 0.60
    _blit(p, _glow(color, 64), bx, by, big * 1.9, None, 0.35)
    for x, y, r, teeth, rot in ((bx, by, big, 12, t * 70.0), (sx, sy, small, 8, -t * 70.0 * 12 / 8 + 11)):
        p.save()
        p.translate(x, y)
        p.rotate(rot)
        g = QRadialGradient(QPointF(-r * 0.3, -r * 0.3), r * 1.3)
        g.setColorAt(0.0, QColor(color).lighter(150))
        g.setColorAt(0.6, QColor(color))
        g.setColorAt(1.0, QColor(color).darker(170))
        p.setBrush(g)
        p.setPen(_pen(QColor(color).lighter(150), max(0.7, 1.0 * s), 200))
        p.drawPath(_gear_path(teeth, r, r * 0.78))
        p.setBrush(screen)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(0, 0), r * 0.18, r * 0.18)
        p.restore()
    # sparks at the contact point
    mx, my = (bx + sx) / 2 + 2 * s, (by + sy) / 2
    for k in range(4):
        u = (t * 2.4 + k / 4.0) % 1.0
        a = -1.2 + k * 0.6
        x = mx + math.cos(a) * 18.0 * s * u
        y = my + math.sin(a) * 18.0 * s * u + 14.0 * s * u * u
        _blit(p, _glow(QColor("#FFD28A"), 16), x, y, 3.0 * s, None, 1.0 - u)


def searching(p, t, cx, cy, s, color):
    """A lens sweeps in a small arc; the glass has a moving glint."""
    a = math.sin(t * 1.6) * 0.5
    x = cx + 118.0 * s + math.cos(a) * 8.0 * s
    y = cy - 46.0 * s + math.sin(a * 2) * 6.0 * s
    r = 17.0 * s
    lens = QRadialGradient(QPointF(x - r * 0.3, y - r * 0.3), r)
    lens.setColorAt(0.0, QColor(255, 255, 255, 70))
    lens.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 30))
    p.setBrush(lens); p.setPen(Qt.NoPen)
    p.drawEllipse(QPointF(x, y), r, r)
    rim = QPainterPath(); rim.addEllipse(QPointF(x, y), r, r)
    _glow_stroke(p, rim, color, max(1.2, 2.6 * s))
    handle = QPainterPath(QPointF(x + r * 0.72, y + r * 0.72))
    handle.lineTo(x + r * 1.65, y + r * 1.65)
    _glow_stroke(p, handle, color, max(1.8, 4.2 * s))
    g = (t * 0.7) % 1.0
    glint = QPainterPath(QPointF(x - r * 0.55 + g * r * 0.4, y - r * 0.2 - g * r * 0.3))
    glint.lineTo(x - r * 0.15 + g * r * 0.4, y - r * 0.6 - g * r * 0.3)
    p.setPen(_pen(QColor(255, 255, 255), max(0.8, 1.6 * s), int(200 * math.sin(math.pi * g))))
    p.drawPath(glint)


def browsing(p, t, cx, cy, s, color, screen: QColor):
    rect = QRectF(cx + 90 * s, cy - 70 * s, 78 * s, 54 * s)
    _blit(p, _glow(color, 64), rect.center().x(), rect.center().y(), rect.width() * 0.75, rect.height() * 0.8, 0.25)
    pane = QColor(screen); pane.setAlpha(235)
    p.setBrush(pane)
    p.setPen(_pen(color, max(1.0, 1.8 * s), 220))
    p.drawRoundedRect(rect, 8 * s, 8 * s)
    p.drawLine(QPointF(rect.left(), rect.top() + 12 * s), QPointF(rect.right(), rect.top() + 12 * s))
    for i in range(3):
        c = QColor(color); c.setAlpha(150 + 35 * i)
        p.setPen(Qt.NoPen); p.setBrush(c)
        p.drawEllipse(QPointF(rect.left() + (8 + i * 7) * s, rect.top() + 6 * s), 2 * s, 2 * s)
    # skeleton rows with a shimmer passing over them
    shimmer = (t * 0.8) % 1.4 - 0.2
    for row in range(3):
        y = rect.top() + (21 + row * 10) * s
        w = (54 - row * 12) * s
        bar = QRectF(rect.left() + 9 * s, y, w, 5 * s)
        g = QLinearGradient(bar.topLeft(), bar.topRight())
        dim = QColor(color); dim.setAlpha(70)
        lit = QColor(color).lighter(140); lit.setAlpha(220)
        g.setColorAt(0.0, dim)
        g.setColorAt(max(0.0, min(1.0, shimmer)), lit)
        g.setColorAt(1.0, dim)
        p.setBrush(g)
        p.drawRoundedRect(bar, 2.5 * s, 2.5 * s)


def reading(p, t, cx, cy, s, color, screen: QColor):
    """An open book whose page turns every few seconds."""
    x, y = cx + 122 * s, cy - 30 * s
    _blit(p, _glow(color, 64), x, y, 48 * s, 34 * s, 0.22)
    page = QColor(screen).lighter(115)

    def leaf(sign: float, lift: float = 0.0) -> QPainterPath:
        path = QPainterPath(QPointF(x, y + 28 * s))
        tip = 38 * s * math.cos(lift * math.pi) * sign
        top = -22 * s - 10 * s * math.sin(lift * math.pi)
        path.lineTo(x + tip, y + 18 * s - 8 * s * math.sin(lift * math.pi))
        path.lineTo(x + tip, y + top)
        path.quadTo(x + tip * 0.45, y - 20 * s, x, y - 9 * s)
        path.closeSubpath()
        return path

    p.setBrush(page)
    p.setPen(_pen(color, max(1.0, 1.8 * s), 220))
    p.drawPath(leaf(-1)); p.drawPath(leaf(1))
    for side in (-1, 1):
        for row in range(3):
            ly = y + (-8 + row * 9) * s
            p.setPen(_pen(color, max(0.6, 1.0 * s), 110))
            p.drawLine(QPointF(x + side * 8 * s, ly), QPointF(x + side * (28 - row * 3) * s, ly + 2 * s))
    turn = (t % 3.2) / 1.1
    if turn < 1.0:
        p.setBrush(page.lighter(108))
        p.setPen(_pen(color, max(1.0, 1.6 * s), 230))
        p.drawPath(leaf(1, _smooth(turn)))


def scanning(p, t, cx, cy, s, color):
    """A beam ring sweeps around the head: three lit arc segments with a bright leading edge."""
    rx, ry = 124 * s, 80 * s
    rect = QRectF(cx - rx, cy - 8 * s - ry, 2 * rx, 2 * ry)
    for k in range(3):
        start = (t * 150.0 + k * 120.0) % 360.0
        seg = QPainterPath()
        seg.arcMoveTo(rect, start)
        seg.arcTo(rect, start, 46)
        _glow_stroke(p, seg, color, max(1.0, 2.0 * s), 200)
        lead = math.radians(start + 46)
        _blit(p, _glow(color, 32), rect.center().x() + math.cos(lead) * rx, rect.center().y() - math.sin(lead) * ry,
              6 * s, None, 0.9)


def coding(p, t, cx, cy, s, color):
    """</> with a cursor and code lines being typed under it."""
    x, y = cx + 142 * s, cy - 44 * s
    for sign in (-1, 1):
        chevron = QPainterPath(QPointF(x + sign * 22 * s, y - 11 * s))
        chevron.lineTo(x + sign * 33 * s, y)
        chevron.lineTo(x + sign * 22 * s, y + 11 * s)
        _glow_stroke(p, chevron, color, max(1.2, 2.6 * s))
    slash = QPainterPath(QPointF(x + 6 * s, y - 14 * s))
    slash.lineTo(x - 6 * s, y + 14 * s)
    _glow_stroke(p, slash, color, max(1.2, 2.6 * s))
    typed = (t * 0.9) % 1.0
    for row in range(3):
        full = (40 - row * 9) * s
        k = max(0.0, min(1.0, typed * 3 - row))
        if k <= 0:
            continue
        ly = y + (26 + row * 8) * s
        p.setPen(_pen(color, max(0.8, 2.0 * s), 150))
        p.drawLine(QPointF(x - 20 * s, ly), QPointF(x - 20 * s + full * k, ly))
        if k < 1.0 and int(t * 4) % 2 == 0:
            p.setPen(_pen(QColor(color).lighter(140), max(0.8, 1.6 * s), 230))
            p.drawLine(QPointF(x - 18 * s + full * k, ly - 4 * s), QPointF(x - 18 * s + full * k, ly + 4 * s))


def happy(p, t, cx, cy, s, color):
    """Sparkles pop around the head one after another."""
    for k in range(7):
        u = (t * 0.7 + k / 7.0) % 1.0
        a = k * 2.4 + 0.6
        rad = (112 + 18 * u) * s
        x = cx + math.cos(a) * rad
        y = cy - 8 * s + math.sin(a) * rad * 0.62
        size = (3.0 + 6.0 * math.sin(math.pi * u)) * s
        alpha = int(240 * math.sin(math.pi * u))
        _blit(p, _glow(color, 32), x, y, size * 2.2, None, 0.5 * math.sin(math.pi * u))
        _sparkle(p, x, y, size, color, alpha)


def confused(p, t, cx, cy, s, color):
    x, y = cx + 116 * s, cy - 62 * s
    wobble = math.sin(t * 2.2) * 10.0
    p.save()
    p.translate(x, y)
    p.rotate(wobble)
    q = QPainterPath(QPointF(-8 * s, -6 * s))
    q.cubicTo(QPointF(-8 * s, -18 * s), QPointF(10 * s, -18 * s), QPointF(9 * s, -6 * s))
    q.cubicTo(QPointF(8 * s, 2 * s), QPointF(0, 1 * s), QPointF(0, 9 * s))
    _glow_stroke(p, q, color, max(1.4, 3.0 * s))
    c = QColor(color).lighter(125)
    p.setPen(Qt.NoPen); p.setBrush(c)
    p.drawEllipse(QPointF(0, 17 * s), 2.2 * s, 2.2 * s)
    p.restore()


def typing(p, t, cx, cy, s, color):
    """Three chat dots bouncing in turn under the face."""
    y0 = cy + 104 * s
    for k in range(3):
        u = (t * 1.6 - k * 0.18) % 1.0
        hop = math.sin(math.pi * min(1.0, u / 0.5)) if u < 0.5 else 0.0
        x = cx + (k - 1) * 14 * s
        y = y0 - hop * 7 * s
        _blit(p, _glow(color, 32), x, y, 7 * s, None, 0.25 + 0.35 * hop)
        c = QColor(color).lighter(120); c.setAlpha(int(150 + 100 * hop))
        p.setPen(Qt.NoPen); p.setBrush(c)
        p.drawEllipse(QPointF(x, y), 3.3 * s, 3.3 * s)


def playful(p, t, cx, cy, s, color):
    for k in range(3):
        a = t * 0.45 + k * 2.1
        x = cx + (105 + k * 10) * s
        y = cy - (42 + k * 18) * s + math.sin(a) * 5 * s
        _sparkle(p, x, y, (3.0 + k) * s, color, 120 + k * 40)


def stretching(p, t, cx, cy, s, color):
    for side in (-1, 1):
        for k in range(2):
            u = (t * 0.9 + k * 0.5) % 1.0
            arc = QPainterPath()
            r = (20 + 18 * u) * s
            x = cx + side * 130 * s
            rect = QRectF(x - r, cy - r, 2 * r, 2 * r)
            start = 150 if side < 0 else -30
            arc.arcMoveTo(rect, start)
            arc.arcTo(rect, start, 60)
            _glow_stroke(p, arc, color, max(0.8, 1.4 * s), int(170 * (1 - u)))


def sleeping(p, t, cx, cy, s, color, moon: QColor, star_path, robot=None):
    halo = _glow(moon, 64)
    mx, my, mr = cx - 132 * s, cy - 82 * s, 17 * s
    _blit(p, halo, mx, my, mr * 2.6, None, 0.35 + 0.1 * math.sin(t * 0.5))
    full = QPainterPath(); full.addEllipse(QPointF(mx, my), mr, mr)
    cut = QPainterPath(); cut.addEllipse(QPointF(mx + 7.5 * s, my - 4.0 * s), mr * 0.96, mr * 0.96)
    lit = QColor(moon); lit.setAlpha(230)
    p.setPen(Qt.NoPen); p.setBrush(lit)
    crescent = full.subtracted(cut)
    p.drawPath(crescent)
    if robot is not None and getattr(robot, "_active_animation", "off") == "cloudy":
        # HIST-078 / HREQ-006: a passing cloud covers the moon. The stage clouds
        # were painted before the robot; repaint the same cloud layer only inside
        # the crescent, so the moon sits behind the cloud instead of in front.
        from jarvis.ui import ambience180
        p.save()
        p.setClipPath(crescent, Qt.IntersectClip)
        ambience180.draw(robot, p, "cloudy", float(robot.width()), float(robot.height()), float(t))
        p.restore()
    for index, (dx, dy) in enumerate(((-112, -115), (-150, -45), (24, -108), (-80, -130))):
        tw = 0.5 + 0.5 * math.sin(t * 0.65 + index * 2.0)
        _sparkle(p, cx + dx * s, cy + dy * s, (2.4 + 1.6 * tw) * s, color, 60 + int(140 * tw))
    for index in range(4):
        cycle = (t / 6.4 + index / 4.0) % 1.0
        travel = _smooth(cycle)
        env = min(_smooth(cycle / 0.14), _smooth((1.0 - cycle) / 0.38))
        if env < 0.02:
            continue
        drift = math.sin(cycle * math.tau * 0.82 + index * 1.37) * (5.0 + index * 0.7)
        x = cx + (84.0 + 54.0 * travel + drift) * s
        y = cy - (35.0 + 104.0 * travel) * s
        half = (8.0 + index * 0.55 + 4.8 * travel) * s * 0.5
        z = QPainterPath(QPointF(x - half, y - half))
        z.lineTo(x + half, y - half); z.lineTo(x - half, y + half); z.lineTo(x + half, y + half)
        _glow_stroke(p, z, color, max(1.0, 1.5 * s), int(226 * env))


def draw(robot, p: QPainter, state: str, cx: float, cy: float, s: float, color: QColor) -> bool:
    """Draw the effect for ``state``; False when this module has none (caller keeps its own)."""
    t = float(robot._elapsed)
    screen = QColor(robot._colors["screen"])
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    if state == "listening":
        listening(p, t, cx, cy, s, color)
    elif state == "speaking":
        speaking(p, t, cx, cy, s, color)
    elif state == "thinking":
        thinking(p, t, cx, cy, s, color)
    elif state == "working":
        working(p, t, cx, cy, s, color, screen)
    elif state in ("searching", "looking"):
        searching(p, t, cx, cy, s, color)
    elif state == "browsing":
        browsing(p, t, cx, cy, s, color, screen)
    elif state == "reading":
        reading(p, t, cx, cy, s, color, screen)
    elif state == "scanning":
        scanning(p, t, cx, cy, s, color)
    elif state == "coding":
        coding(p, t, cx, cy, s, color)
    elif state in ("happy", "success", "excited"):
        happy(p, t, cx, cy, s, color)
    elif state == "confused":
        confused(p, t, cx, cy, s, color)
    elif state == "typing":
        typing(p, t, cx, cy, s, color)
    elif state == "playful":
        playful(p, t, cx, cy, s, color)
    elif state == "stretching":
        stretching(p, t, cx, cy, s, color)
    elif state == "sleeping":
        sleeping(p, t, cx, cy, s, color, QColor(robot._environment_colors["moon"]), robot._star_path, robot)
    else:
        return False
    return True
