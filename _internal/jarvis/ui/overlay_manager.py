# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Qt renderer for persistent JARVIS information/checklist overlays."""

from copy import deepcopy
from typing import Any, Callable
import html
import math
import re
import time

from PySide6.QtCore import QPoint, QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QMouseEvent, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget

from jarvis.overlay_state import OverlayStore
from jarvis.gaming.presentation import dynamic_session_text
from jarvis.display_mapping import display_number_for_geometry, display_number_from_name, windows_monitor_geometries
from jarvis.i18n import tr
from jarvis.language import language_code



def _localized_dynamic_text(panel: dict[str, Any], code: str) -> str:
    """Localize only JARVIS-generated session timing, preserving user panel text."""
    rendered = dynamic_session_text(panel)
    code = str(code or "ru")
    if code == "ru":
        return rendered
    # 186: ownership is structural — only a panel bound to an application game
    # session carries generated timing; an ordinary note that merely starts a
    # line with «Сессия:» is user text and stays literal.
    binding = panel.get("binding") if isinstance(panel.get("binding"), dict) else {}
    if str(binding.get("type", "")).casefold() != "game_session":
        return rendered
    prefixes = {
        "en":"Session", "de":"Sitzung", "fr":"Session", "es":"Sesión",
        "it":"Sessione", "pt":"Sessão",
    }
    units = {
        "en":("h","min","s"), "de":("Std","Min","Sek"), "fr":("h","min","s"),
        "es":("h","min","s"), "it":("h","min","s"), "pt":("h","min","s"),
    }
    prefix = prefixes.get(code)
    if not prefix:
        return rendered
    lines = rendered.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("Сессия:"):
            value = line.split(":", 1)[1].strip()
            hour_u, min_u, sec_u = units[code]
            value = re.sub(r"(\d+)\s+ч\b", rf"\1 {hour_u}", value)
            value = re.sub(r"(\d+)\s+мин\b", rf"\1 {min_u}", value)
            value = re.sub(r"(\d+)\s+с\b", rf"\1 {sec_u}", value)
            lines[i] = f"{prefix}: {value}"
            break
    return "\n".join(lines)


def _rgba(value: str, opacity: int) -> str:
    color = QColor(str(value or "#0A0F18"))
    if not color.isValid():
        color = QColor("#0A0F18")
    alpha = max(0, min(255, round(255 * int(opacity) / 100)))
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha})"


class OverlayPanel(QFrame):
    def __init__(
        self,
        name: str,
        on_moved: Callable[[str, "OverlayPanel"], None] | None = None,
        *,
        clock_interval_ms: int = 1000,
        language: str = "ru",
    ) -> None:
        super().__init__(None)
        self.name = name
        self._on_moved = on_moved
        self._drag_origin: QPoint | None = None
        self._window_origin: QPoint | None = None
        self._dragged = False
        self._model: dict[str, Any] = {}
        self.language = str(language or "ru")
        self._panel_background = QColor("#0A0F18")
        self._panel_border = QColor("#57D7FF")
        self._panel_background_opacity = 72
        self._panel_border_opacity = 100
        self._panel_border_width = 1
        self._panel_radius = 12.0
        self.setObjectName("jarvisOverlay")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setWindowFlag(Qt.Tool, True)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, True)

        # Each live session panel owns its own GUI-thread timer.  Qt documents
        # QTimer as emitting timeout at a constant interval in the object's event
        # loop; parenting it to the panel makes the lifetime/thread affinity
        # explicit and avoids relying on an unrelated manager heartbeat.
        self._clock_timer = QTimer(self)
        self._clock_timer.setTimerType(Qt.PreciseTimer)
        self._clock_timer.setInterval(max(250, int(clock_interval_ms or 1000)))
        self._clock_timer.timeout.connect(self.refresh_dynamic)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        self.title_label = QLabel()
        self.title_label.setObjectName("overlayTitle")
        self.title_label.setTextFormat(Qt.PlainText)    # 186: user heading is literal data
        self.title_label.setWordWrap(True)
        self.body_label = QLabel()
        self.body_label.setObjectName("overlayBody")
        self.body_label.setWordWrap(True)
        self.body_label.setTextInteractionFlags(Qt.NoTextInteraction)
        layout.addWidget(self.title_label)
        layout.addWidget(self.body_label)

    def apply_model(self, panel: dict[str, Any]) -> None:
        self._model = deepcopy(panel)
        style = panel.get("style", {}) if isinstance(panel.get("style"), dict) else {}
        font_family = str(style.get("font_family", "Segoe UI") or "Segoe UI")
        font_size = max(8, int(style.get("font_size", 18) or 18))
        text_color = str(style.get("text_color", "#F2F6FF") or "#F2F6FF")
        bg = str(style.get("background_color", "#0A0F18") or "#0A0F18")
        border = str(style.get("border_color", "#57D7FF") or "#57D7FF")
        legacy_opacity = max(0, min(100, int(style.get("opacity", 72) or 72)))
        background_opacity = max(0, min(100, int(style.get("background_opacity", legacy_opacity) if style.get("background_opacity", legacy_opacity) is not None else legacy_opacity)))
        border_opacity = max(0, min(100, int(style.get("border_opacity", 100) if style.get("border_opacity", 100) is not None else 100)))
        self._drag_enabled = bool(style.get("drag_enabled", True))
        border_width = max(0, int(style.get("border_width", 1) or 1))
        padding = max(0, int(style.get("padding", 14) or 14))
        width = max(160, int(style.get("width", 420) or 420))
        try:
            explicit_height = int(style.get("height", 0) or 0)
        except (TypeError, ValueError):
            explicit_height = 0
        click_through = bool(style.get("click_through", True))

        self.layout().setContentsMargins(padding, max(6, padding - 2), padding, max(6, padding - 2))
        # ``width`` remains the user's base size at the default 18 pt.  Scaling
        # it with the selected font prevents larger text from being squeezed into
        # an old tiny panel while preserving the user's chosen proportions.
        font_scale = max(0.72, min(2.4, font_size / 18.0))
        scaled_width = max(160, min(1800, int(round(width * font_scale))))
        self.setFixedWidth(scaled_width)
        if explicit_height > 0:
            scaled_height = max(90, min(1600, int(round(explicit_height * font_scale))))
            self.setFixedHeight(scaled_height)
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16777215)
        self.title_label.setFont(QFont(font_family, max(8, font_size - 2), QFont.DemiBold))
        self.body_label.setFont(QFont(font_family, font_size))

        # Paint panel chrome ourselves.  Stylesheet rgba on a translucent
        # top-level QFrame is compositor/theme dependent on Windows; direct
        # QPainter alpha makes the Background/Border transparency sliders map
        # 1:1 to the actual pixels of every panel.
        self._panel_background = QColor(bg) if QColor(bg).isValid() else QColor("#0A0F18")
        self._panel_border = QColor(border) if QColor(border).isValid() else QColor("#57D7FF")
        self._panel_background_opacity = background_opacity
        self._panel_border_opacity = border_opacity
        self._panel_border_width = border_width
        self.setStyleSheet(
            "QFrame#jarvisOverlay { background: transparent; border: none; }"
            f"QLabel {{ color: {text_color}; background: transparent; border: none; }}"
            "QLabel#overlayTitle { padding-bottom: 2px; }"
        )
        self.update()

        # Qt's native WindowTransparentForInput flag disables *all* pointer input,
        # including the drag handle. When dragging is enabled it intentionally
        # wins over click-through so the advertised upper-area drag actually
        # works; users who want a fully passive HUD can disable dragging.
        effective_click_through = bool(click_through and not self._drag_enabled)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, effective_click_through)
        self.setWindowFlag(Qt.WindowTransparentForInput, effective_click_through)

        raw_title = panel.get("title", self.name)
        title = str(raw_title if raw_title is not None else "").strip()
        self.title_label.setText(title)
        self.title_label.setVisible(bool(title))
        kind = str(panel.get("kind", "text"))
        if kind == "checklist":
            # Rich text lets a completed voice-commanded item be visibly crossed
            # out instead of merely changing the checkbox glyph.
            self.body_label.setTextFormat(Qt.RichText)
            lines: list[str] = []
            for item in panel.get("items", []) if isinstance(panel.get("items"), list) else []:
                if not isinstance(item, dict):
                    continue
                checked = bool(item.get("checked", False))
                safe_text = html.escape(str(item.get("text", "") or ""))
                mark = "☑" if checked else "☐"
                text_html = f"<s>{safe_text}</s>" if checked else safe_text
                lines.append(f"{mark}&nbsp;{text_html}")
            self.body_label.setText("<br>".join(lines) if lines else tr("Чек-лист пуст", self.language))
        else:
            self.body_label.setTextFormat(Qt.PlainText)
            self.body_label.setText(_localized_dynamic_text(panel, self.language))
        if explicit_height <= 0:
            self.adjustSize()

        binding = panel.get("binding", {}) if isinstance(panel.get("binding"), dict) else {}
        is_live_session = (
            str(binding.get("type", "") or "").casefold() == "game_session"
            and str(binding.get("status", "running") or "running").casefold() in {"running", "starting"}
        )
        should_tick = is_live_session and bool(panel.get("visible", True))
        if should_tick and not self._clock_timer.isActive():
            self._clock_timer.start()
        elif not should_tick and self._clock_timer.isActive():
            self._clock_timer.stop()

        # Re-applying a window flag can hide a native window, so visibility is set
        # only after all style/flag changes have been made.
        if bool(panel.get("visible", True)):
            self.show()
        else:
            self.hide()

    def suspend(self) -> None:
        """HIST-008: a disabled panel is hidden and stops its own periodic work."""
        if self._clock_timer.isActive():
            self._clock_timer.stop()
        self.hide()

    def refresh_dynamic(self) -> None:
        """Refresh clock-like fields on the GUI thread without disk churn."""
        if not self._model or str(self._model.get("kind", "text")) == "checklist":
            return
        rendered = _localized_dynamic_text(self._model, self.language)
        if rendered != self.body_label.text():
            self.body_label.setTextFormat(Qt.PlainText)
            self.body_label.setText(rendered)
            style = self._model.get("style", {}) if isinstance(self._model.get("style"), dict) else {}
            if int(style.get("height", 0) or 0) <= 0:
                self.adjustSize()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        inset = max(0.0, self._panel_border_width / 2.0)
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        bg = QColor(self._panel_background)
        bg.setAlpha(max(0, min(255, round(255 * self._panel_background_opacity / 100))))
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, self._panel_radius, self._panel_radius)
        if self._panel_border_width > 0 and self._panel_border_opacity > 0:
            border = QColor(self._panel_border)
            border.setAlpha(max(0, min(255, round(255 * self._panel_border_opacity / 100))))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(border, self._panel_border_width))
            painter.drawRoundedRect(rect, self._panel_radius, self._panel_radius)
        painter.end()
        super().paintEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (event.button() == Qt.LeftButton and not self.testAttribute(Qt.WA_TransparentForMouseEvents)
                and bool(getattr(self, "_drag_enabled", True)) and event.position().y() <= 42):
            self._drag_origin = event.globalPosition().toPoint()
            self._window_origin = self.pos()
            self._dragged = False
            event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_origin is not None and self._window_origin is not None:
            delta = event.globalPosition().toPoint() - self._drag_origin
            if delta.manhattanLength() >= 2:
                self._dragged = True
            self.move(self._window_origin + delta)
            event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        dragged = self._dragged
        self._drag_origin = None; self._window_origin = None; self._dragged = False
        if dragged and self._on_moved is not None:
            try:
                self._on_moved(self.name, self)
            except Exception:
                # Dragging is a convenience. A persistence error must never crash
                # the desktop assistant or steal focus from the game.
                pass
        super().mouseReleaseEvent(event)


class MusicWaveform(QWidget):
    """High-refresh music visualizer with continuous time-based motion.

    The old implementation refreshed synthetic target envelopes only every
    ~45 ms. Even though the QWidget repaint timer ran near 60 Hz, those target
    steps made the waveform *look* like a 20–25 FPS animation. This renderer
    computes fallback amplitudes continuously on every frame and applies
    framerate-independent attack/release smoothing to both fallback and real
    backend levels. The configured 60 FPS setting uses a 16 ms PreciseTimer
    (62.5 Hz nominal); 120 FPS uses 8 ms (125 Hz nominal), so rounding never
    drops the actual timer cadence below the requested floor.
    """
    BAR_COUNT = 64

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(48)
        self.levels: list[float] = []
        self.playing = False
        self._display = [0.10] * self.BAR_COUNT
        self._targets = [0.10] * self.BAR_COUNT
        self._last_tick = time.perf_counter()
        self._fps = 60
        self._mode = "symmetric"
        self._spectrum_real = False
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._active = True
        self._timer.start()

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        if self._active:
            if not self._timer.isActive():
                self._last_tick = time.perf_counter()
                self._timer.start()
        elif self._timer.isActive():
            self._timer.stop()

    def _apply_timer_rate(self) -> None:
        # int() deliberately floors 1000/FPS: 60 -> 16 ms (~62.5 Hz),
        # 120 -> 8 ms (~125 Hz). round() would turn 60 FPS into 17 ms/58.8 Hz.
        interval = max(4, int(1000 / max(60, self._fps))) if self.playing else 100
        self._timer.setInterval(interval)

    def set_fps(self, value: int) -> None:
        self._fps = max(60, min(120, int(value or 60)))
        self._apply_timer_rate()

    def set_mode(self, value: str) -> None:
        mode = str(value or "symmetric").strip().casefold()
        self._mode = mode if mode in {"symmetric", "up"} else "symmetric"
        self.update()

    def set_state(
        self, *, levels: list[float] | None = None, playing: bool = False,
        seed_text: str = "", spectrum_real: bool = False,
    ) -> None:
        self.levels = [max(0.0, min(1.0, float(x))) for x in (levels or [])[:96]]
        self.playing = bool(playing)
        self._spectrum_real = bool(spectrum_real)
        if not self.playing:
            self._targets = [0.075] * self.BAR_COUNT
        elif self.levels and self._spectrum_real:
            # Map real low->high FFT bands only when the backend snapshot changes.
            # The 120 Hz render timer then performs interpolation only.
            self._targets = self._targets_from_levels(self.levels)
        else:
            self._targets = [0.075] * self.BAR_COUNT
        self._apply_timer_rate()
        # Do not snap _display here. New backend values are eased by _tick.
        self.update()

    def _targets_from_levels(self, values: list[float]) -> list[float]:
        if not values:
            return [0.08] * self.BAR_COUNT
        mapped: list[float] = []
        n = len(values)
        for i in range(self.BAR_COUNT):
            # Linear sampling between source bins avoids a visible staircase when
            # the backend provides fewer bins than the panel renders.
            pos = i * (n - 1) / max(1, self.BAR_COUNT - 1)
            lo = int(math.floor(pos))
            hi = min(n - 1, lo + 1)
            frac = pos - lo
            value = float(values[lo]) * (1.0 - frac) + float(values[hi]) * frac
            mapped.append(max(0.045, min(1.0, value)))
        return self._spatial_smooth(mapped)

    @staticmethod
    def _spatial_smooth(values: list[float]) -> list[float]:
        if len(values) < 3:
            return list(values)
        out = list(values)
        for i in range(len(values)):
            left = values[i - 1] if i > 0 else values[i]
            right = values[i + 1] if i + 1 < len(values) else values[i]
            out[i] = left * 0.20 + values[i] * 0.60 + right * 0.20
        return out

    def _tick(self) -> None:
        now = time.perf_counter()
        dt = max(0.001, min(0.050, now - self._last_tick))
        self._last_tick = now
        targets = self._targets

        # Framerate-independent attack/release smoothing. Rising bars react a
        # little faster than falling bars, but neither direction can hard-snap.
        for i, target in enumerate(targets):
            current = self._display[i]
            rate = 15.0 if target >= current else 10.0
            alpha = 1.0 - math.exp(-rate * dt)
            self._display[i] = current + (target - current) * alpha
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
        accent = self.palette().color(self.foregroundRole())
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        count = self.BAR_COUNT
        gap = 2.5
        bar_w = max(2.0, (self.width() - gap * (count - 1)) / count)
        mode = self._mode
        center = self.height() / 2.0
        baseline = self.height() - 2.0
        for i, level in enumerate(self._display):
            h = max(3.0, max(0.04, min(1.0, level)) * (self.height() - 6))
            x = i * (bar_w + gap)
            y = baseline - h if mode == "up" else center - h / 2.0
            painter.drawRoundedRect(QRectF(x, y, bar_w, h), bar_w / 2, bar_w / 2)


def _music_icon(kind: str, color: str, size: int = 18) -> QIcon:
    """Draw a monochrome vector transport icon; no emoji/sticker glyphs."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    qcolor = QColor(str(color or "#F2F6FF"))
    if not qcolor.isValid():
        qcolor = QColor("#F2F6FF")
    pen = QPen(qcolor, max(1.4, size / 11.0), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(qcolor)
    m = size * 0.22
    c = size * 0.5
    if kind == "play":
        painter.drawPolygon(QPolygonF([QPointF(m + 1, m), QPointF(size - m, c), QPointF(m + 1, size - m)]))
    elif kind == "pause":
        painter.drawRoundedRect(QRectF(size * .29, m, size * .13, size - 2*m), 1, 1)
        painter.drawRoundedRect(QRectF(size * .58, m, size * .13, size - 2*m), 1, 1)
    elif kind in {"previous", "next"}:
        flip = -1 if kind == "previous" else 1
        cx = c
        # draw in canonical next orientation then mirror coordinates for previous
        pts1 = [(m, m), (c, c), (m, size-m)]
        pts2 = [(c-1, m), (size-m-1, c), (c-1, size-m)]
        def pt(x, y):
            return QPointF(size-x if flip < 0 else x, y)
        painter.drawPolygon(QPolygonF([pt(x,y) for x,y in pts1]))
        painter.drawPolygon(QPolygonF([pt(x,y) for x,y in pts2]))
        xline = size-m if flip < 0 else m
        painter.drawLine(QPointF(xline, m), QPointF(xline, size-m))
    painter.end()
    return QIcon(pix)


class MusicOverlayPanel(OverlayPanel):
    def __init__(self, name: str, on_moved=None, *, state_provider=None, media_action=None, language: str = "ru") -> None:
        super().__init__(name, on_moved, clock_interval_ms=1000, language=language)
        self._state_provider = state_provider
        self._media_action = media_action
        self._last_state: dict[str, Any] = {}
        self._icon_color = "#F2F6FF"
        self.body_label.hide()
        # Music uses one compact now-playing line: "Track — Artist".
        self.title_label.hide()
        self.now_playing_label = QLabel()
        self.now_playing_label.setTextFormat(Qt.PlainText)    # 186: track/artist are literal data
        self.now_playing_label.setText("Ничего не играет")
        self.now_playing_label.setObjectName("musicNowPlaying")
        self.now_playing_label.setWordWrap(True)
        self.now_playing_label.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        # The header is also the drag handle. Let pointer events reach the frame.
        self.now_playing_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.layout().insertWidget(0, self.now_playing_label)

        self.waveform = MusicWaveform(self)
        self.layout().addWidget(self.waveform)

        timeline = QHBoxLayout()
        timeline.setContentsMargins(0, 0, 0, 0)
        self.progress = QSlider(Qt.Horizontal)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setEnabled(False)
        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setObjectName("musicTime")
        timeline.addWidget(self.progress, 1)
        timeline.addWidget(self.time_label)
        self.layout().addLayout(timeline)

        self.controls_widget = QWidget(self)
        controls = QHBoxLayout(self.controls_widget)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        controls.addStretch(1)
        self.previous_button = QPushButton()
        self.play_button = QPushButton()
        self.next_button = QPushButton()
        for button in (self.previous_button, self.play_button, self.next_button):
            button.setObjectName("musicControl")
            button.setCursor(Qt.PointingHandCursor)
            button.setFixedSize(36, 30)
            controls.addWidget(button)
        controls.addStretch(1)
        self.layout().addWidget(self.controls_widget)
        self.previous_button.clicked.connect(lambda: self._action("previous"))
        self.play_button.clicked.connect(self._toggle_play)
        self.next_button.clicked.connect(lambda: self._action("next"))

        self._music_timer = QTimer(self)
        self._music_timer.setTimerType(Qt.PreciseTimer)
        # PCM spectrum is sampled independently from painting. 30 Hz source
        # refresh is enough for transients while the visualizer interpolates at
        # 120 Hz; idle drops to a low-frequency poll.
        self._music_timer.setInterval(250)
        self._music_timer.timeout.connect(self.refresh_music)
        self._music_timer.start()

    @staticmethod
    def _clock(seconds: float) -> str:
        total = max(0, int(seconds or 0))
        return f"{total // 60}:{total % 60:02d}"

    def _target(self) -> str:
        source = str(self._last_state.get("source", "") or "").casefold()
        if source in {"yandex", "yandex_music"}:
            return "yandex_music"
        if source == "discord":
            return "discord"
        return "music"

    def _action(self, action: str) -> None:
        if callable(self._media_action):
            self._media_action(action, self._target())

    def _toggle_play(self) -> None:
        paused = bool(self._last_state.get("paused", False))
        playing = bool(self._last_state.get("playing", False))
        self._action("resume" if paused or not playing else "pause")

    def suspend(self) -> None:
        # HIST-008: no provider reads and no icon builds while the panel is disabled;
        # apply_model() refreshes once with current data and restarts the poll on show.
        super().suspend()
        if self._music_timer.isActive():
            self._music_timer.stop()
        self.waveform.set_active(False)

    def _refresh_icons(self, paused: bool, playing: bool) -> None:
        key = (bool(paused), bool(playing), self._icon_color)
        if getattr(self, "_icon_key188", None) == key:
            return                                   # same glyphs: nothing to redraw
        self._icon_key188 = key
        self.previous_button.setIcon(_music_icon("previous", self._icon_color))
        self.play_button.setIcon(_music_icon("play" if paused or not playing else "pause", self._icon_color))
        self.next_button.setIcon(_music_icon("next", self._icon_color))

    def apply_model(self, panel: dict[str, Any]) -> None:
        super().apply_model(panel)
        self.body_label.hide(); self.title_label.hide()
        style = panel.get("style", {}) if isinstance(panel.get("style"), dict) else {}
        font_family = str(style.get("font_family", "Segoe UI") or "Segoe UI")
        font_size = max(8, int(style.get("font_size", 18) or 18))
        header_font = QFont(font_family, max(8, font_size - 1), QFont.DemiBold)
        self.now_playing_label.setFont(header_font)
        metrics = QFontMetrics(header_font)
        two_lines = max(24, metrics.lineSpacing() * 2 + 4)
        self.now_playing_label.setMinimumHeight(two_lines)
        self.now_playing_label.setMaximumHeight(two_lines)
        self.time_label.setFont(QFont(font_family, max(8, font_size - 5)))
        text_color = str(style.get("text_color", "#F2F6FF") or "#F2F6FF")
        self._icon_color = str(style.get("control_color", text_color) or text_color)
        self.waveform.setStyleSheet(f"color: {text_color};")
        self.waveform.set_fps(int(style.get("visualizer_fps", 60) or 60))
        self.waveform.set_mode(str(style.get("visualizer_mode", "symmetric") or "symmetric"))
        controls_visible = bool(style.get("controls_visible", True))
        self.controls_widget.setVisible(controls_visible)
        control_bg = str(style.get("control_background_color", style.get("background_color", "#0A0F18")) or "#0A0F18")
        control_bg_opacity = max(0, min(100, int(style.get("control_background_opacity", 0) or 0)))
        control_border = str(style.get("control_border_color", self._icon_color) or self._icon_color)
        control_border_opacity = max(0, min(100, int(style.get("control_border_opacity", 80) or 0)))
        self.controls_widget.setStyleSheet(
            "QPushButton#musicControl {"
            f"background-color: {_rgba(control_bg, control_bg_opacity)};"
            f"border: 1px solid {_rgba(control_border, control_border_opacity)};"
            "border-radius: 10px; padding: 0px; }"
            "QPushButton#musicControl:hover { border-width: 2px; }"
            "QPushButton#musicControl:pressed { padding-top: 1px; }"
        )
        self.refresh_music()
        visible = bool(panel.get("visible", True))
        self.waveform.set_active(visible)
        if visible and not self._music_timer.isActive():
            self._music_timer.start()
        elif not visible and self._music_timer.isActive():
            self._music_timer.stop()
        if int(style.get("height", 0) or 0) <= 0:
            self.adjustSize()

    def refresh_music(self) -> None:
        state: dict[str, Any] = {}
        if callable(self._state_provider):
            try:
                value = self._state_provider()
                if isinstance(value, dict):
                    state = value
            except Exception:
                state = {}
        self._last_state = state
        title = str(state.get("title", "") or "").strip() or "Ничего не играет"
        artist = str(state.get("artist", "") or "").strip()
        suffix = artist or str(state.get("source_label", "") or "").strip()
        self.now_playing_label.setText(f"{title} — {suffix}" if suffix and title != "Ничего не играет" else title)
        position = float(state.get("position_seconds", 0.0) or 0.0)
        duration = float(state.get("duration_seconds", 0.0) or 0.0)
        if duration > 0:
            self.progress.setValue(max(0, min(1000, int(round(position / duration * 1000)))))
        else:
            self.progress.setValue(0)
        self.time_label.setText(f"{self._clock(position)} / {self._clock(duration)}")
        playing = bool(state.get("playing", False))
        paused = bool(state.get("paused", False))
        self._refresh_icons(paused, playing)
        levels = state.get("levels") if isinstance(state.get("levels"), list) else []
        spectrum_real = bool(state.get("audio_levels_real", False)) and str(state.get("audio_levels_kind", "") or "").casefold() == "spectrum"
        self.waveform.set_state(
            levels=levels, playing=playing, seed_text=title + artist, spectrum_real=spectrum_real
        )
        target_poll = 33 if playing else 250
        if self._music_timer.interval() != target_poll:
            self._music_timer.setInterval(target_poll)



class OverlayManager:
    def __init__(self, config, *, music_state_provider=None, media_action=None) -> None:
        self.config = config
        self.music_state_provider = music_state_provider
        self.media_action = media_action
        self.panels: dict[str, OverlayPanel] = {}
        self.store = OverlayStore(config.paths.root / "overlays.json", defaults=self._defaults())
        # Windows created by JARVIS are runtime UI, not desktop startup items.
        # Keep ordinary checklist/note content on disk but start it hidden; remove
        # stale game-session/game-specific HUD panels completely.
        self.store.prepare_startup(remove_game_panels=True, hide_all=True)
        # Music is a first-class semantic panel, so voice can address it as
        # «панель музыки», «оверлей музыки» or simply «музыка». It is created
        # once and then obeys the same persistent show/hide/move settings as all
        # other panels.
        if bool(self.config.get("desktop.overlay.music_panel_enabled", True)):
            current = self.store.load().get("panels", {})
            has_music = any(isinstance(v, dict) and str(v.get("purpose", "")).casefold() == "music" for v in current.values())
            if not has_music:
                music_style = self.config.get("desktop.overlay.styles.music", {}) or {}
                self.store.apply({
                    "action": "show", "name": "Музыка", "title": "Музыка", "purpose": "music",
                    "aliases": ["музыка", "музыки", "панель музыки", "панель музыка", "музыкальная панель", "музыкальный оверлей", "оверлей музыки", "окно музыки", "music", "music panel", "music overlay"],
                    "kind": "music",
                    "monitor": int(music_style.get("monitor", 1) or 1),
                    "position": str(music_style.get("position", "bottom_right") or "bottom_right"),
                })
                self.store.apply({"action": "hide", "name": "Музыка"})
        self.reload()

    def _defaults(self) -> dict[str, Any]:
        return {
            "monitor": int(self.config.get("desktop.overlay.monitor", 1) or 1),
            "position": str(self.config.get("desktop.overlay.position", "top_left") or "top_left"),
            "style": {
                "font_family": self.config.get("desktop.overlay.font_family", "Segoe UI"),
                "font_size": int(self.config.get("desktop.overlay.font_size", 18) or 18),
                "text_color": self.config.get("desktop.overlay.text_color", "#F2F6FF"),
                "background_color": self.config.get("desktop.overlay.background_color", "#0A0F18"),
                "border_color": self.config.get("desktop.overlay.border_color", "#57D7FF"),
                "opacity": int(self.config.get("desktop.overlay.opacity", 72) or 72),
                "background_opacity": int(self.config.get("desktop.overlay.background_opacity", self.config.get("desktop.overlay.opacity", 72)) or 0),
                "border_opacity": int(self.config.get("desktop.overlay.border_opacity", 100) or 0),
                "drag_enabled": bool(self.config.get("desktop.overlay.drag_enabled", True)),
                "width": int(self.config.get("desktop.overlay.width", 420) or 420),
                "height": int(self.config.get("desktop.overlay.height", 0) or 0),
                "click_through": bool(self.config.get("desktop.overlay.click_through", True)),
            },
        }

    @staticmethod
    def _panel_style_category(panel: dict[str, Any]) -> str:
        purpose = str(panel.get("purpose", "") or "").casefold()
        kind = str(panel.get("kind", "text") or "text").casefold()
        if purpose == "game_session":
            return "session"
        if kind == "checklist" or "checklist" in purpose:
            return "checklist"
        if purpose == "music":
            return "music"
        if purpose == "notes" or "note" in purpose:
            return "notes"
        return "default"

    def _effective_style(self, panel: dict[str, Any]) -> dict[str, Any]:
        # v2.8 presentation is configured by panel *type*, not by individual
        # voice-created windows.  This keeps every checklist visually consistent
        # and prevents only one duplicate/session panel from receiving a style.
        style = deepcopy(self._defaults().get("style", {}))
        category = self._panel_style_category(panel)
        if category != "default":
            overrides = self.config.get(f"desktop.overlay.styles.{category}", {})
            if isinstance(overrides, dict):
                style.update({key: value for key, value in overrides.items() if value is not None})
        if bool(panel.get("style_override", False)):
            local = panel.get("style", {})
            if isinstance(local, dict):
                style.update({key: value for key, value in local.items() if value is not None})
        return style

    def _effective_placement(self, panel: dict[str, Any]) -> tuple[int, str]:
        # Voice/tool/manual movement is a per-panel operation.  Previously the
        # renderer always re-applied the category monitor/anchor from Settings,
        # so a command could appear to do nothing or move every panel of the same
        # type.  Explicit placement now wins for that saved panel only.
        if bool(panel.get("placement_override", False)):
            monitor = panel.get("monitor", 1)
            position = panel.get("position", "top_left")
            return max(1, int(monitor or 1)), str(position or "top_left")
        category = self._panel_style_category(panel)
        if category == "default":
            monitor = self.config.get("desktop.overlay.monitor", panel.get("monitor", 1))
            position = self.config.get("desktop.overlay.position", panel.get("position", "top_left"))
        else:
            cfg = self.config.get(f"desktop.overlay.styles.{category}", {}) or {}
            monitor = cfg.get("monitor", panel.get("monitor", 1)) if isinstance(cfg, dict) else panel.get("monitor", 1)
            position = cfg.get("position", panel.get("position", "top_left")) if isinstance(cfg, dict) else panel.get("position", "top_left")
        return max(1, int(monitor or 1)), str(position or "top_left")

    def _sync_placement_settings(self, panel: dict[str, Any]) -> None:
        category = self._panel_style_category(panel)
        monitor = max(1, int(panel.get("monitor", 1) or 1))
        position = str(panel.get("position", "top_left") or "top_left")
        data = self.config.data
        overlay = data.setdefault("desktop", {}).setdefault("overlay", {})
        if category == "default":
            overlay["monitor"] = monitor
            overlay["position"] = position
        else:
            style = overlay.setdefault("styles", {}).setdefault(category, {})
            style["monitor"] = monitor
            style["position"] = position
        self.config.save(data)

    def reload(self) -> None:
        self.store.defaults = self._defaults()
        model = self.store.load()
        configured = model.get("panels", {}) if isinstance(model.get("panels"), dict) else {}
        for name in list(self.panels):
            if name not in configured:
                self.panels.pop(name).close()
        if not bool(self.config.get("desktop.overlay.enabled", True)):
            for panel in self.panels.values():
                panel.suspend()
            return
        for name, state in configured.items():
            if not isinstance(state, dict):
                continue
            category = self._panel_style_category(state)
            type_enabled = True if category == "default" else bool(self.config.get(f"desktop.overlay.styles.{category}.enabled", True))
            widget = self.panels.get(name)
            if not type_enabled:
                if widget is not None:
                    widget.suspend()
                continue
            purpose = str(state.get("purpose", "") or "").casefold()
            wants_music = purpose == "music"
            if widget is not None and wants_music != isinstance(widget, MusicOverlayPanel):
                widget.close(); self.panels.pop(name, None); widget = None
            if widget is None:
                if wants_music:
                    widget = MusicOverlayPanel(
                        name, self._persist_dragged_position, state_provider=self.music_state_provider,
                        media_action=self.media_action, language=language_code(self.config),
                    )
                else:
                    widget = OverlayPanel(
                        name, self._persist_dragged_position, clock_interval_ms=1000,
                        language=language_code(self.config),
                    )
                self.panels[name] = widget
            widget.language = language_code(self.config)
            render_state = deepcopy(state)
            render_state["style"] = self._effective_style(state)
            monitor, position = self._effective_placement(state)
            # If Settings selected a different monitor/anchor, treat it as an
            # exact semantic placement and do not reuse a stale manual-drag
            # offset from the previous anchor. Dragged placement still preserves
            # offsets because drag synchronizes store + settings together.
            if monitor != int(state.get("monitor", 1) or 1) or position != str(state.get("position", "top_left") or "top_left"):
                render_state["offset_x"] = 0
                render_state["offset_y"] = 0
            render_state["monitor"] = monitor
            render_state["position"] = position
            widget.apply_model(render_state)
            self._place(widget, render_state)

    @staticmethod
    def _anchor_points(widget: OverlayPanel, area) -> dict[str, tuple[int, int]]:
        margin = 24
        x_left = area.left() + margin
        x_center = area.left() + (area.width() - widget.width()) // 2
        x_right = area.right() - widget.width() - margin + 1
        y_top = area.top() + margin
        y_center = area.top() + (area.height() - widget.height()) // 2
        y_bottom = area.bottom() - widget.height() - margin + 1
        return {
            "top_left": (x_left, y_top), "top_center": (x_center, y_top), "top_right": (x_right, y_top),
            "center_left": (x_left, y_center), "center": (x_center, y_center), "center_right": (x_right, y_center),
            "bottom_left": (x_left, y_bottom), "bottom_center": (x_center, y_bottom), "bottom_right": (x_right, y_bottom),
        }

    @staticmethod
    def _screen_geometry_tuple(screen) -> tuple[int, int, int, int]:
        area = screen.geometry()
        return int(area.left()), int(area.top()), int(area.right()) + 1, int(area.bottom()) + 1

    @classmethod
    def _windows_number_for_screen(cls, screen, fallback: int = 1) -> int:
        try:
            number = display_number_from_name(str(screen.name() or ""))
            if number is not None:
                return number
        except Exception:
            pass
        try:
            number = display_number_for_geometry(cls._screen_geometry_tuple(screen), windows_monitor_geometries())
            if number is not None:
                return number
        except Exception:
            pass
        return max(1, int(fallback))

    @classmethod
    def _screen_for_number(cls, requested: int, screens):
        requested = max(1, int(requested or 1))
        for index, screen in enumerate(screens):
            if cls._windows_number_for_screen(screen, index + 1) == requested:
                return screen
        # Only if Windows device names/geometries are unavailable do we fall back
        # to Qt order. This preserves old behaviour without silently remapping a
        # known DISPLAY3 onto screens[2] when Windows numbered them differently.
        return screens[min(requested - 1, len(screens) - 1)]

    @classmethod
    def _place(cls, widget: OverlayPanel, panel: dict[str, Any]) -> None:
        screens = QApplication.screens()
        if not screens:
            return
        requested = max(1, int(panel.get("monitor", 1) or 1))
        screen = cls._screen_for_number(requested, screens)
        area = screen.availableGeometry()
        points = cls._anchor_points(widget, area)
        pos = str(panel.get("position", "top_left"))
        x, y = points.get(pos, points["top_left"])
        x += int(panel.get("offset_x", 0) or 0)
        y += int(panel.get("offset_y", 0) or 0)
        # Voice placement is exact and bounded to the requested monitor. Even a
        # stale manual-drag offset or a newly enlarged window must not spill onto
        # another display/off-screen.
        max_x = max(area.left(), area.right() - widget.width() + 1)
        max_y = max(area.top(), area.bottom() - widget.height() + 1)
        x = min(max(int(x), int(area.left())), int(max_x))
        y = min(max(int(y), int(area.top())), int(max_y))
        widget.move(x, y)

    def _persist_dragged_position(self, name: str, widget: OverlayPanel) -> None:
        screens = QApplication.screens()
        if not screens:
            return
        center = widget.frameGeometry().center()
        screen = QApplication.screenAt(center)
        if screen is None:
            screen = screens[0]
        fallback = next((i + 1 for i, item in enumerate(screens) if item is screen), 1)
        monitor = self._windows_number_for_screen(screen, fallback)
        points = self._anchor_points(widget, screen.availableGeometry())
        x_now, y_now = widget.x(), widget.y()
        position, (base_x, base_y) = min(
            points.items(),
            key=lambda item: (x_now - item[1][0]) ** 2 + (y_now - item[1][1]) ** 2,
        )
        _text, snapshot = self.store.apply({
            "action": "move", "name": name, "monitor": monitor, "position": position,
            "offset_x": x_now - base_x, "offset_y": y_now - base_y,
        })
        # OverlayStore already persists this panel's monitor/anchor/offset and
        # marks it as an explicit placement override.  Do not rewrite the whole
        # category's Settings placement when the user drags one panel.
        del snapshot

    def refresh_dynamic(self) -> None:
        for panel in list(self.panels.values()):
            try:
                panel.refresh_dynamic()
            except RuntimeError:
                pass

    def close_all(self) -> None:
        for panel in list(self.panels.values()):
            panel.close()
        self.panels.clear()
