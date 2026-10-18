"""NEO 180: the stage weather behind the robot — rain, snow, starfall, starry sky, clouds, fireflies.

Rewritten for depth and softness: every effect has 2–3 parallax layers (far = small, dim, slow;
near = large, bright, fast) and glows are cached sprites, so a frame costs a few blits instead of
per-frame gradients.  Everything is a pure function of the stage size and the clock (no RNG state),
so the picture never jumps when the frame rate changes, and Safe UI gets a still frame.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient

_CACHE: dict = {}


def _h(value: float) -> float:
    """Stable hash → [0, 1)."""
    return (math.sin(value * 12.9898 + 78.233) * 43758.5453) % 1.0


def _smooth(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def _cached(key, build):
    image = _CACHE.get(key)
    if image is None:
        if len(_CACHE) > 64:
            _CACHE.clear()
        image = build()
        _CACHE[key] = image
    return image


def _glow(color: QColor, size: int = 64, falloff: tuple = ((0.0, 1.0), (0.35, 0.55), (1.0, 0.0))) -> QImage:
    def build():
        image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        p = QPainter(image)
        p.setRenderHint(QPainter.Antialiasing, True)
        g = QRadialGradient(QPointF(size / 2, size / 2), size / 2)
        for at, k in falloff:
            c = QColor(color); c.setAlphaF(k)
            g.setColorAt(at, c)
        p.setPen(Qt.NoPen); p.setBrush(g)
        p.drawEllipse(QRectF(0, 0, size, size))
        p.end()
        return image
    return _cached(("glow", color.rgb(), size, falloff), build)


def _blit(p: QPainter, image: QImage, x: float, y: float, rx: float, ry: float | None = None, opacity: float = 1.0) -> None:
    if opacity <= 0.004 or rx <= 0.2:
        return
    ry = rx if ry is None else ry
    old = p.opacity()
    p.setOpacity(old * min(1.0, opacity))
    p.drawImage(QRectF(x - rx, y - ry, rx * 2, ry * 2), image)
    p.setOpacity(old)


# ----------------------------------------------------------------------------------- rain
def rain(p: QPainter, w: float, h: float, t: float, base: QColor, safe: bool) -> None:
    gust = math.radians(11.0 + 4.0 * math.sin(t * 0.23) + 2.0 * math.sin(t * 0.61))
    dx, dy = math.sin(gust), math.cos(gust)
    # a cool haze where the rain meets the ground
    haze = QLinearGradient(QPointF(0, h * 0.62), QPointF(0, h))
    c0 = QColor(base); c0.setAlpha(0)
    c1 = QColor(base); c1.setAlpha(26)
    haze.setColorAt(0.0, c0); haze.setColorAt(1.0, c1)
    p.fillRect(QRectF(0, h * 0.62, w, h * 0.38), haze)
    layers = ((0.30, 70 if not safe else 24, 230.0, 9.0, 0.8, 80),
              (0.62, 42 if not safe else 14, 350.0, 16.0, 1.1, 150),
              (1.00, 18 if not safe else 6, 500.0, 26.0, 1.5, 215))
    ground = h - 4.0
    splash_glow = _glow(base, 32)
    for depth, count, speed, length, width, alpha in layers:
        span = h + length + 40.0 + speed * 0.55          # the tail end is time for the ripple
        c = QColor(base); c.setAlpha(int(alpha * 0.35))
        faint = QPen(c, width, Qt.SolidLine, Qt.RoundCap)        # NEO 183: two pens per layer, not two per drop
        c = QColor(base).lighter(112); c.setAlpha(alpha)
        bright = QPen(c, width, Qt.SolidLine, Qt.RoundCap)
        for i in range(count):
            seed = depth * 1000 + i * 7.31
            phase = _h(seed) * span
            raw = t * speed * (0.9 + 0.2 * _h(seed + 1)) + phase
            cycle = math.floor(raw / span)
            y = raw % span - length - 20.0
            x0 = _h(seed + cycle * 3.7) * (w + 80.0) - 40.0 + (y + 20.0) * dx / max(dy, 0.2)
            head = QPointF(x0, y)
            tail = QPointF(x0 - dx * length, y - dy * length)
            mid = QPointF((head.x() + tail.x()) / 2, (head.y() + tail.y()) / 2)
            if y > ground + length:
                head = tail = mid = None
            if head is not None:
                p.setPen(faint)
                p.drawLine(tail, mid)
                p.setPen(bright)
                p.drawLine(mid, head)
            if depth < 0.6:
                continue
            # ripple where the near drops land: two thin rings that open and fade
            land = (ground + length + 20.0)
            age = ((raw % span) - land) / speed
            if 0.0 <= age < 0.5:
                k = age / 0.5
                lx = x0 + (ground - y) * dx / max(dy, 0.2)
                ring = QColor(base); ring.setAlpha(int(alpha * 0.8 * (1 - k)))
                p.setPen(QPen(ring, 0.9))
                p.setBrush(Qt.NoBrush)
                r = 2.0 + 13.0 * depth * k
                p.drawEllipse(QPointF(lx, ground), r, r * 0.28)
                if k < 0.6:
                    ring.setAlpha(int(alpha * 0.5 * (1 - k / 0.6)))
                    p.setPen(QPen(ring, 0.7))
                    p.drawEllipse(QPointF(lx, ground), r * 0.5, r * 0.14)
                _blit(p, splash_glow, lx, ground, 5.0 * depth, 2.0, 0.35 * (1 - k))
    p.setPen(Qt.NoPen)


# ----------------------------------------------------------------------------------- snow
def snow(p: QPainter, w: float, h: float, t: float, base: QColor, safe: bool) -> None:
    soft = _glow(base, 48, ((0.0, 1.0), (0.45, 0.6), (1.0, 0.0)))
    floor = QLinearGradient(QPointF(0, h * 0.8), QPointF(0, h))
    c0 = QColor(base); c0.setAlpha(0)
    c1 = QColor(base); c1.setAlpha(30)
    floor.setColorAt(0.0, c0); floor.setColorAt(1.0, c1)
    p.fillRect(QRectF(0, h * 0.8, w, h * 0.2), floor)
    layers = ((0.3, 60 if not safe else 24, 9.0, 1.1, 0.45),
              (0.6, 34 if not safe else 14, 16.0, 2.0, 0.7),
              (1.0, 12 if not safe else 5, 26.0, 3.4, 0.95))
    for depth, count, speed, size, bright in layers:
        span = h + 30.0
        for i in range(count):
            seed = 500 + depth * 777 + i * 3.17
            fall = t * speed * (0.8 + 0.4 * _h(seed))
            y = (fall + _h(seed + 2) * span) % span - 15.0
            sway = math.sin(t * (0.35 + 0.3 * _h(seed + 3)) + seed) * (6.0 + 10.0 * depth)
            x = (_h(seed + 4) * (w + 40.0) - 20.0 + sway + t * 3.0 * depth) % (w + 40.0) - 20.0
            r = size * (0.8 + 0.5 * _h(seed + 5))
            _blit(p, soft, x, y, r * 2.6, None, bright * 0.75)
            if depth >= 1.0 and i % 3 == 0:
                # a few near flakes show their six-fold crystal
                c = QColor(base); c.setAlpha(int(200 * bright))
                p.setPen(QPen(c, 0.8, Qt.SolidLine, Qt.RoundCap))
                rot = t * 0.4 + seed
                for k in range(6):
                    a = rot + k * math.pi / 3
                    ex, ey = x + math.cos(a) * r * 2.2, y + math.sin(a) * r * 2.2
                    p.drawLine(QPointF(x, y), QPointF(ex, ey))
                    bx, by = x + math.cos(a) * r * 1.3, y + math.sin(a) * r * 1.3
                    for side in (-0.6, 0.6):
                        p.drawLine(QPointF(bx, by), QPointF(bx + math.cos(a + side) * r * 0.7, by + math.sin(a + side) * r * 0.7))
                p.setPen(Qt.NoPen)


# ----------------------------------------------------------------------------- star field
def _star_field(p: QPainter, w: float, h: float, t: float, base: QColor, count: int, safe: bool) -> None:
    dot = _glow(base, 32, ((0.0, 1.0), (0.25, 0.7), (1.0, 0.0)))
    for i in range(count):
        seed = 900 + i * 1.913
        x = _h(seed) * w
        y = _h(seed + 1) * h
        depth = _h(seed + 2) ** 2
        tw = 1.0 if safe else 0.55 + 0.45 * math.sin(t * (0.8 + 2.2 * _h(seed + 3)) + seed * 5.0)
        r = 0.7 + 1.9 * depth
        _blit(p, dot, x, y, r * 2.2, None, (0.25 + 0.6 * depth) * tw)


def _spark(p: QPainter, x: float, y: float, size: float, color: QColor) -> None:
    inner = size * 0.18
    path = QPainterPath(QPointF(x, y - size))
    path.lineTo(x + inner, y - inner); path.lineTo(x + size, y); path.lineTo(x + inner, y + inner)
    path.lineTo(x, y + size); path.lineTo(x - inner, y + inner); path.lineTo(x - size, y); path.lineTo(x - inner, y - inner)
    path.closeSubpath()
    p.setPen(Qt.NoPen); p.setBrush(color)
    p.drawPath(path)


def _meteor(p: QPainter, w: float, h: float, t: float, base: QColor, index: int, period: float, glow: QImage,
            scale: float = 1.0) -> None:
    cycle = math.floor((t + index * period * 0.37) / period)
    local = ((t + index * period * 0.37) % period) / period
    life = 0.32
    if local > life:
        return
    k = local / life
    seed = 70 + index * 13.1 + cycle * 3.3
    # NEO 183: every meteor falls the same way (down-left), only the slope varies a little
    angle = math.radians(208 + 10 * _h(seed))
    ux, uy = math.cos(angle), -math.sin(angle)
    sx = w * (0.30 + 0.75 * _h(seed + 1))          # start further right, since they all travel left
    sy = h * (0.02 + 0.32 * _h(seed + 2))
    travel = (120 + 120 * _h(seed + 3)) * scale
    hx, hy = sx + ux * travel * k, sy + uy * travel * k
    fade = math.sin(math.pi * k) ** 0.8
    length = (40 + 50 * _h(seed + 4)) * scale * (0.4 + 0.6 * fade)
    tx, ty = hx - ux * length, hy - uy * length
    nx, ny = -uy, ux
    wide = 2.2 * scale
    trail = QPainterPath(QPointF(hx + nx * wide, hy + ny * wide))
    trail.lineTo(hx - nx * wide, hy - ny * wide)
    trail.lineTo(tx, ty)
    trail.closeSubpath()
    g = QLinearGradient(QPointF(hx, hy), QPointF(tx, ty))
    head = QColor(base).lighter(130); head.setAlpha(int(230 * fade))
    end = QColor(base); end.setAlpha(0)
    g.setColorAt(0.0, head); g.setColorAt(1.0, end)
    p.setPen(Qt.NoPen); p.setBrush(g)
    p.drawPath(trail)
    _blit(p, glow, hx, hy, 9.0 * scale, None, 0.9 * fade)
    _blit(p, glow, (hx + tx) / 2, (hy + ty) / 2, length * 0.35, 2.2 * scale, 0.25 * fade)   # soft light along the trail
    core = QColor(255, 255, 255, int(255 * fade))
    p.setBrush(core)
    p.drawEllipse(QPointF(hx, hy), 1.5 * scale, 1.5 * scale)


def starfall(p: QPainter, w: float, h: float, t: float, base: QColor, safe: bool) -> None:
    _star_field(p, w, h, t, base, 50 if not safe else 30, safe)
    glow = _glow(base, 48)
    if safe:
        return
    size = max(0.7, min(1.4, min(w / 520.0, h / 300.0)))
    for i in range(6):
        _meteor(p, w, h, t, base, i, 1.9 + 0.55 * i, glow, size)


def starry_sky(p: QPainter, w: float, h: float, t: float, base: QColor, safe: bool) -> None:
    # Milky Way: a faint diagonal band of dust, built once for this stage size
    W, H = max(1, int(w)), max(1, int(h))

    def band():
        image = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        q = QPainter(image)
        q.setRenderHint(QPainter.Antialiasing, True)
        cloud = _glow(base, 64, ((0.0, 1.0), (1.0, 0.0)))
        for i in range(70):
            u = _h(i * 2.1)
            cx = u * W
            cy = H * (0.85 - 0.7 * u) + (_h(i * 3.3) - 0.5) * H * 0.22
            r = (18 + 40 * _h(i * 4.7)) * max(0.6, W / 520.0)
            q.setOpacity(0.05 + 0.06 * _h(i * 5.9))
            q.drawImage(QRectF(cx - r, cy - r * 0.6, r * 2, r * 1.2), cloud)
        q.setOpacity(1.0)
        dust = QColor(base)
        for i in range(260):
            u = _h(i * 0.77 + 9)
            cx = u * W
            cy = H * (0.85 - 0.7 * u) + (_h(i * 1.37 + 3) - 0.5) * H * 0.3 * (0.4 + _h(i * 2.9))
            dust.setAlpha(int(25 + 60 * _h(i * 6.1)))       # the faint, steady dust
            q.setPen(Qt.NoPen); q.setBrush(dust)
            q.drawEllipse(QPointF(cx, cy), 0.45, 0.45)
        q.end()
        return image

    p.drawImage(QRectF(0, 0, w, h), _cached(("milky", W, H, base.rgb()), band))
    # NEO 183: the small stars inside the Milky Way light up and fade out one by one
    tiny = _glow(base, 16, ((0.0, 1.0), (0.3, 0.75), (1.0, 0.0)))
    for i in range(0 if safe else 120):
        u = _h(i * 1.91 + 41)
        cx = u * w
        cy = h * (0.85 - 0.7 * u) + (_h(i * 2.53 + 7) - 0.5) * h * 0.26 * (0.4 + _h(i * 3.7))
        speed = 0.5 + 1.6 * _h(i * 4.1)
        pulse = math.sin(t * speed + i * 2.399)
        level = max(0.0, pulse) ** 2.2                                   # mostly dark, then a short sparkle
        if level > 0.03:
            _blit(p, tiny, cx, cy, 1.2 + 1.6 * _h(i * 5.3) * (0.6 + 0.4 * level), None, 0.25 + 0.75 * level)
    _star_field(p, w, h, t, base, 80 if not safe else 50, safe)
    # a few bright four-point stars that slowly breathe
    for i in range(7):
        seed = 400 + i * 5.7
        x, y = _h(seed) * w, _h(seed + 1) * h * 0.85
        k = 1.0 if safe else 0.6 + 0.4 * math.sin(t * (0.5 + 0.3 * _h(seed + 2)) + seed)
        c = QColor(base).lighter(120); c.setAlpha(int(220 * k))
        _spark(p, x, y, 4.0 + 2.5 * _h(seed + 3), c)
        _blit(p, _glow(base, 32), x, y, 7.0, None, 0.5 * k)
    if not safe:
        _meteor(p, w, h, t, base, 0, 9.0, _glow(base, 48), max(0.7, min(1.3, w / 520.0)))


# --------------------------------------------------------------------------------- clouds
def _cloud_image(color: QColor, variant: int, width: int, height: int) -> QImage:
    """NEO 183: one cloud = one soft silhouette (the union of its puffs) lit from above.

    The 180 cloud painted every puff as its own translucent disc, so the puffs showed through each
    other like stacked stickers. Here the puffs are merged into a single shape, filled once with a
    top-lit gradient and softened by a cheap down/up-scale blur; the whole cloud is opaque inside
    and its translucency is applied once when it is drawn.
    """
    def build():
        shape = QPainterPath()
        shape.setFillRule(Qt.WindingFill)           # overlapping puffs merge instead of cancelling out
        puffs = 6 + variant % 3
        for k in range(puffs):
            u = (k + 0.5) / puffs
            cx = width * (0.14 + 0.72 * u)
            bulge = math.sin(u * math.pi)
            r = height * (0.20 + 0.26 * bulge) * (0.85 + 0.3 * _h(variant * 31 + k))
            cy = height * 0.64 - bulge * height * 0.16 - _h(variant * 17 + k) * height * 0.06
            shape.addEllipse(QPointF(cx, cy), r, r * 0.92)
        shape.addRoundedRect(QRectF(width * 0.12, height * 0.52, width * 0.76, height * 0.26), height * 0.13, height * 0.13)
        shape = shape.simplified()
        shape.setFillRule(Qt.WindingFill)
        sharp = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
        sharp.fill(Qt.transparent)
        q = QPainter(sharp)
        q.setRenderHint(QPainter.Antialiasing, True)
        fill = QLinearGradient(QPointF(0, height * 0.12), QPointF(0, height * 0.86))
        fill.setColorAt(0.0, QColor(color).lighter(135))
        fill.setColorAt(0.55, QColor(color))
        fill.setColorAt(1.0, QColor(color).darker(150))
        q.setPen(Qt.NoPen)
        q.setBrush(fill)
        q.drawPath(shape)
        # a thin bright rim on the sunlit top edge
        rim = QColor(color).lighter(160); rim.setAlpha(120)
        q.setClipPath(shape)
        q.setPen(QPen(rim, max(1.0, height * 0.035)))
        q.setBrush(Qt.NoBrush)
        q.translate(0, height * 0.03)
        q.drawPath(shape)
        q.end()
        # soft edges: a blurred copy under a slightly blurred body
        def blurred(image: QImage, factor: int) -> QImage:
            small = image.scaled(max(1, width // factor), max(1, height // factor), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            return small.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        q = QPainter(image)
        q.setRenderHint(QPainter.SmoothPixmapTransform, True)
        q.setOpacity(0.55)
        q.drawImage(0, 0, blurred(sharp, 9))
        q.setOpacity(1.0)
        q.drawImage(0, 0, blurred(sharp, 3))
        q.end()
        return image
    return _cached(("cloud183", color.rgb(), variant, width, height), build)


_DENSITY = {'few': ((0.55, 0.45, 0.62), (1.00, 0.85, 0.85)),
            'normal': ((0.35, 0.05, 0.10), (0.55, 0.45, 0.62), (0.75, 0.20, 0.33), (1.00, 0.85, 0.85)),
            'many': ((0.30, 0.62, 0.48), (0.35, 0.05, 0.10), (0.45, 0.30, 0.78), (0.55, 0.45, 0.62),
                     (0.75, 0.20, 0.33), (0.85, 0.70, 0.05), (1.00, 0.85, 0.85))}


def cloudy(p: QPainter, w: float, h: float, t: float, base: QColor, safe: bool, band_bottom: float, density: str = 'normal') -> None:
    """Clouds drift in lanes (far ones higher, smaller, slower). They are painted opaque into one
    layer — a nearer cloud simply covers a farther one — and the layer is laid over the stage
    once, so overlapping clouds never show through each other."""
    band_bottom = max(26.0, band_bottom)
    tw = 0.0 if safe else t
    size = max(0.6, min(1.4, w / 520.0))
    lanes = _DENSITY.get(density, _DENSITY['normal'])          # NEO 188 HREQ-006: cloud density setting
    layer_h = max(1, int(min(h, band_bottom + 40.0 * size)))
    key = ("cloud_layer", int(w), layer_h)
    layer = _CACHE.get(key)
    if layer is None:
        if len(_CACHE) > 64:
            _CACHE.clear()
        layer = QImage(max(1, int(w)), layer_h, QImage.Format_ARGB32_Premultiplied)
        _CACHE[key] = layer
    layer.fill(Qt.transparent)
    q = QPainter(layer)
    q.setRenderHint(QPainter.SmoothPixmapTransform, True)
    for i, (depth, lane, offset) in enumerate(lanes):
        width = (100 + 120 * depth) * size
        height = width * 0.42
        speed = 3.0 + 8.0 * depth
        loop = w + width * 2.2
        x = ((offset * loop + tw * speed) % loop) - width * 1.1
        y = max(-height * 0.30, (band_bottom - height * 0.95) * lane - height * 0.18 + math.sin(tw * 0.25 + i * 1.7) * 1.6)
        tint = QColor(base).darker(int(100 + 28 * (1.0 - depth)))      # farther clouds are a little dimmer
        q.drawImage(QRectF(x, y, width, height), _cloud_image(tint, i, int(width), int(height)))
    q.end()
    old = p.opacity()
    p.setOpacity(old * 0.78)
    p.drawImage(QPointF(0, 0), layer)
    p.setOpacity(old)


# ------------------------------------------------------------------------------ fireflies
def fireflies(p: QPainter, w: float, h: float, t: float, safe: bool) -> None:
    t = 0.0 if safe else t
    count = 16 if safe else 32
    size = max(0.65, min(1.35, min(w / 520.0, h / 300.0)))
    halo = _glow(QColor("#C8F06A"), 48, ((0.0, 1.0), (0.3, 0.45), (1.0, 0.0)))
    core = _glow(QColor("#F6FFD8"), 16, ((0.0, 1.0), (0.6, 0.8), (1.0, 0.0)))
    for i in range(count):
        phase = i * 2.399963229728653
        speed = 0.13 + (i % 7) * 0.012
        def pos(tt: float) -> tuple[float, float]:
            return (w * (0.50 + 0.43 * math.sin(phase + tt * speed) + 0.025 * math.sin(phase * 2.1 + tt * 0.52)),
                    h * (0.50 + 0.42 * math.sin(phase * 1.73 + tt * speed * 0.72) + 0.028 * math.sin(phase * 0.83 + tt * 0.39)))
        x, y = pos(t)
        y += math.sin(t * (1.6 + (i % 3) * 0.4) + phase) * 2.2 * size        # a light hover, like a real insect
        if i % 5 in (0, 2):
            # NEO 188 HREQ-006: some fireflies glow steadily (only a slow, faint breath) …
            glow = 0.82 + 0.12 * math.sin(t * 0.45 + phase)
        else:
            # … the others flash: a short bright pulse, then real darkness until the next one.
            beat = math.sin(phase * 3.0 + t * (0.7 + (i % 7) * 0.09))
            glow = 0.04 + 0.96 * max(0.0, beat) ** 6
        r = (0.9 + (i % 4) * 0.2) * size
        if not safe:
            for lag, a in ((0.25, 0.18), (0.5, 0.08)):
                px, py = pos(t - lag)
                _blit(p, core, px, py, r * 1.6, None, a * glow)
        _blit(p, halo, x, y, r * 7.0, None, 0.55 * glow)
        _blit(p, core, x, y, r * 2.2, None, 0.6 + 0.4 * glow)


def draw(robot, p: QPainter, mode: str, w: float, h: float, t: float) -> None:
    safe = bool(getattr(robot, "_safe_ui", False))
    colors = robot._environment_colors
    tt = 0.0 if safe else t
    p.setPen(Qt.NoPen)
    if mode == "fireflies":
        fireflies(p, w, h, t, safe)
    elif mode == "rain":
        rain(p, w, h, tt, QColor(colors["rain"]), safe)
    elif mode == "snow":
        snow(p, w, h, tt, QColor(colors["snow"]), safe)
    elif mode == "starfall":
        starfall(p, w, h, tt, QColor(colors["starfall"]), safe)
    elif mode == "starry_sky":
        starry_sky(p, w, h, tt, QColor(colors["starry_sky"]), safe)
    elif mode == "cloudy":
        scale = max(0.28, min(1.45, min(w / 520.0, h / 300.0) * robot._scale_factor))
        neutral_head_top = h / 2.0 - 3.0 - 80.0 * scale
        cloudy(p, w, h, tt, QColor(colors["cloud"]), safe, neutral_head_top - max(8.0, 11.0 * scale),
               str(getattr(robot, "_cloud_density188", "normal") or "normal"))


# ------------------------------------------------------------------- over the robot (188)
_NEAR_RAIN = (1.00, 18, 6, 500.0, 26.0, 215)          # depth, count, safe count, speed, length, alpha of the near layer


def _rain_on_head(p: QPainter, w: float, h: float, t: float, base: QColor, safe: bool, head) -> None:
    """Near drops that meet the top of the head burst into a few small droplets."""
    if safe or head is None:
        return
    hx, hy, scale, tilt = head
    top = hy - 72.0 * scale
    half = 70.0 * scale
    gust = math.radians(11.0 + 4.0 * math.sin(t * 0.23) + 2.0 * math.sin(t * 0.61))
    dx, dy = math.sin(gust), math.cos(gust)
    depth, count, _safe, speed, length, alpha = _NEAR_RAIN
    span = h + length + 40.0 + speed * 0.55
    color = QColor(base).lighter(118)
    for i in range(count):
        seed = depth * 1000 + i * 7.31
        phase = _h(seed) * span
        raw = t * speed * (0.9 + 0.2 * _h(seed + 1)) + phase
        cycle = math.floor(raw / span)
        age = ((raw % span) - (top + length + 20.0)) / speed
        if not 0.0 <= age < 0.32:
            continue
        x = _h(seed + cycle * 3.7) * (w + 80.0) - 40.0 + (top + 20.0) * dx / max(dy, 0.2)
        if abs(x - hx) > half:
            continue
        k = age / 0.32
        for n in range(4):
            angle = math.radians(-150 + 40 * n + 12 * _h(seed + n + cycle))
            v = (60.0 + 40.0 * _h(seed * 3 + n)) * scale ** 0.5
            px = x + math.cos(angle) * v * age
            py = top + math.sin(angle) * v * age + 260.0 * age * age
            c = QColor(color); c.setAlpha(int(alpha * (1.0 - k)))
            p.setPen(QPen(c, 1.1, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(px, py), QPointF(px - math.cos(angle) * 2.5, py - math.sin(angle) * 2.5))
    p.setPen(Qt.NoPen)


def snow_depth(t: float, since: float, full_after: float = 120.0) -> float:
    """0…1: snow builds up slowly and stops at a fixed limit."""
    return max(0.0, min(1.0, (t - since) / full_after))


def _snow_cover(p: QPainter, w: float, h: float, t: float, base: QColor, safe: bool, robot, head) -> None:
    since = float(getattr(robot, "_ambience_since188", 0.0) or 0.0)
    k = 1.0 if safe else snow_depth(t, since)
    size = max(0.6, min(1.4, min(w / 520.0, h / 300.0)))
    # the ground: a soft drift that never grows above its limit
    if k > 0.02:
        top = h - (2.0 + 9.0 * k) * size
        path = QPainterPath(QPointF(0, h))
        steps = 24
        for n in range(steps + 1):
            x = w * n / steps
            bump = (math.sin(n * 1.3) * 0.5 + math.sin(n * 0.47 + 1.0) * 0.5) * 2.2 * size * k
            path.lineTo(x, top - bump)
        path.lineTo(w, h)
        path.closeSubpath()
        fill = QLinearGradient(QPointF(0, top - 4), QPointF(0, h))
        a = QColor(base).lighter(115); a.setAlpha(int(215 * min(1.0, k * 1.6)))
        b = QColor(base); b.setAlpha(int(150 * min(1.0, k * 1.6)))
        fill.setColorAt(0.0, a); fill.setColorAt(1.0, b)
        p.setPen(Qt.NoPen); p.setBrush(fill)
        p.drawPath(path)
    if head is None:
        return
    hx, hy, scale, tilt = head
    top = hy - 72.0 * scale
    shake_at = float(getattr(robot, "_snow_shake188", -100.0) or -100.0)
    head_since = float(getattr(robot, "_snow_head_since188", since) or since)
    music = float(getattr(robot, "_music_visual_strength", 0.0) or 0.0)
    cap = 0.0 if music > 0.5 else (1.0 if safe else snow_depth(t, head_since, 90.0))
    p.save()
    if abs(tilt) >= 0.2:
        p.translate(hx, hy); p.rotate(tilt); p.translate(-hx, -hy)
    if cap > 0.03:
        thick = (2.0 + 9.0 * cap) * scale
        span = 78.0 * scale
        # a lumpy snow cap that follows the top of the head and hangs a little over its rounded corners
        points = []
        lumps = 9
        for n in range(lumps + 1):
            u = n / lumps
            edge = math.sin(math.pi * u) ** 0.5
            lump = (0.8 + 0.2 * math.sin(n * 2.3 + 0.7)) * thick * edge
            points.append(QPointF(hx - span + 2 * span * u, top + (1.0 - edge) * 13 * scale - lump))
        path = QPainterPath(QPointF(hx - span, top + 14 * scale))
        path.lineTo(points[0])
        for a_pt, b_pt in zip(points, points[1:]):
            mid = QPointF((a_pt.x() + b_pt.x()) / 2, (a_pt.y() + b_pt.y()) / 2 - thick * 0.18)
            path.quadTo(a_pt, mid)
        path.lineTo(points[-1])
        path.lineTo(hx + span, top + 14 * scale)
        path.cubicTo(QPointF(hx + span * 0.7, top + 2.5 * scale), QPointF(hx - span * 0.7, top + 2.5 * scale), QPointF(hx - span, top + 14 * scale))
        path = path.simplified()
        shadow = QColor(70, 96, 128, int(90 * min(1.0, cap * 2)))
        p.setPen(QPen(shadow, max(1.0, 1.6 * scale)))
        p.setBrush(Qt.NoBrush)
        p.save(); p.translate(0, 1.4 * scale); p.drawPath(path); p.restore()
        fill = QLinearGradient(QPointF(0, top - thick), QPointF(0, top + 8 * scale))
        a = QColor(255, 255, 255, 250)
        b = QColor(base).lighter(104); b.setAlpha(235)
        fill.setColorAt(0.0, a); fill.setColorAt(1.0, b)
        p.setPen(QPen(QColor(205, 222, 240, 200), max(0.8, 1.0 * scale)))
        p.setBrush(fill)
        p.drawPath(path)
        sparkle = _glow(QColor(255, 255, 255), 16, ((0.0, 1.0), (0.4, 0.5), (1.0, 0.0)))
        for n in range(5):
            u = 0.15 + 0.7 * _h(n * 3.1)
            tw = 0.5 + 0.5 * math.sin(t * (1.3 + n * 0.4) + n)
            _blit(p, sparkle, hx - span + 2 * span * u, top - thick * 0.6 * math.sin(math.pi * u) ** 0.6, 2.2 * scale, None, 0.8 * tw * cap)
    p.restore()
    # the snow shaken off the head falls away
    since_shake = t - shake_at
    if not safe and 0.0 <= since_shake < 1.1:
        soft = _glow(base, 32, ((0.0, 1.0), (0.5, 0.6), (1.0, 0.0)))
        for n in range(12):
            u = (n + 0.5) / 12
            x0 = hx + (u - 0.5) * 140 * scale
            vx = (u - 0.5) * 120 * scale + math.sin(n * 2.1) * 20 * scale
            px = x0 + vx * since_shake
            py = top - 4 * scale - 40 * scale * since_shake + 420 * scale * since_shake * since_shake
            _blit(p, soft, px, py, (2.0 + 1.5 * _h(n)) * scale ** 0.5 * 2.2, None, 0.9 * (1.0 - since_shake / 1.1))


def draw_overlay(robot, p: QPainter, mode: str, w: float, h: float, t: float) -> None:
    """NEO 188 HREQ-006: weather that touches the robot — rain splashes on the head, snow on the ground and head."""
    if mode not in {"rain", "snow"}:
        return
    safe = bool(getattr(robot, "_safe_ui", False))
    colors = robot._environment_colors
    head = getattr(robot, "_head_geom188", None)
    p.setPen(Qt.NoPen)
    if mode == "rain":
        _rain_on_head(p, w, h, 0.0 if safe else t, QColor(colors["rain"]), safe, head)
    else:
        _snow_cover(p, w, h, 0.0 if safe else t, QColor(colors["snow"]), safe, robot, head)
