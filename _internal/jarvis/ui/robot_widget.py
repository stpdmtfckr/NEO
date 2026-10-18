# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import math
import os
import random
import time

from PySide6.QtCore import QPointF, QRectF, QSize, QTimer, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QMovie,
    QImage,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget


class RobotWidget(QWidget):
    layout_changed = Signal(dict)
    # NEO 177: the main window shows the robot's state as a coloured status light.
    state_changed = Signal(str)

    STATES = {
        "idle",
        "listening",
        "thinking",
        "working",
        "speaking",
        "searching",
        "browsing",
        "reading",
        "scanning",
        "coding",
        "happy",
        "curious",
        "alert",
        "success",
        "error",
        "sad",
        "sleeping",
        "playful",
        "looking",
        "dancing",
        "stretching",
        "waving",
        "confused",
        "excited",
        "typing",
    }

    # Immutable animation lookup data is shared across render ticks.
    _FRAME_INTERVALS = {30: 33, 60: 16, 120: 8}
    _ANGULAR_SPEEDS = {
        "idle": 1.15, "listening": 3.7, "thinking": 3.4, "working": 4.6,
        "speaking": 5.2, "searching": 3.8, "browsing": 3.2, "reading": 2.5,
        "scanning": 4.6, "coding": 3.8, "happy": 2.5, "curious": 1.9,
        "alert": 4.4, "success": 2.8, "error": 2.0, "sleeping": 0.7,
        "playful": 4.1, "looking": 1.5, "dancing": 5.1, "stretching": 2.1,
        "waving": 3.0, "confused": 2.0, "excited": 4.8, "typing": 4.0,
    }
    _GALAXY_GAZE = ((11.5, -3.0), (-11.5, -4.5), (0.5, -9.5), (-11.0, 5.5), (11.5, 5.8))
    _RIGHT_IDLE_SCENES = frozenset({"hologram", "inspect_orb", "solar_system", "galaxy", "constellation"})
    _BOTTOM_IDLE_SCENES = frozenset()
    _SHORT_IDLE_SCENES = frozenset({"paper_plane", "shoulder_roll"})

    def __init__(self, accent: str = "#57D7FF", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(72)
        # This is a child widget, not a native top-level window. Keep it
        # composition-friendly for the whole lifetime and control transparency
        # only by what paintEvent draws; toggling translucent attributes during
        # live resize is both unnecessary and fragile on Windows.
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setAutoFillBackground(False)
        self._background = QColor("#0C1017")
        self._transparent_background = False
        self._background_opacity_percent = 100
        self._active_animation = "off"
        self._accessory_coffee = False
        self._headphones_enabled = True
        self._headphones_color = QColor(accent)
        self._gif_movie = None
        self._gif_mapping = {}
        self._gif_enabled = False
        self._gif_scale = 1.0
        self._gif_path = ""
        self._accessory_cigarette = False
        self._accessory_mug_color = QColor("#DDE7F4")
        self._accessory_cigarette_color = QColor("#718198")
        self._accessory_hand_color = QColor("#313C4F")
        self._state = "idle"
        # Incremental sections may briefly use the ordinary typing expression.
        # Reply text is rendered only in the main conversation.
        self._reply_chunk_until159 = 0.0
        self._phase = 0.0
        self._scale_factor = 1.0
        self._layout_scale = 1.0
        self._colors = {
            "body": QColor("#313C4F"),
            "shell": QColor("#DDE7F4"),
            "screen": QColor("#101721"),
            "eye": QColor(accent),
            "trim": QColor("#718198"),
            "glow": QColor(accent),
        }
        self._environment_colors = {
            "moon": QColor("#DDE7F4"),
            "cloud": QColor("#DCE7F3"),
            "rain": QColor("#91CFFF"),
            "snow": QColor("#F6FAFF"),
            "starfall": QColor("#EAF6FF"),
            "starry_sky": QColor("#EAF6FF"),
        }
        # HOTFIX47: optional live information rendered inside the robot stage.
        # Values are supplied by MainWindow; RobotWidget never performs network or
        # media I/O from paintEvent.  Each item can be positioned independently.
        self._info_config = {
            "clock_enabled": False, "clock_position": "top_center",
            "track_enabled": False, "track_position": "bottom_center",
            "temperature_enabled": False, "temperature_position": "top_right",
            "font_size": 12,
            # HOTFIX49: normalized custom layout.  Coordinates are fractions of
            # RobotWidget, so a user arrangement survives window resizing/DPI.
            "layout": {
                "temperature": {"x": 0.69, "y": 0.02, "w": 0.28, "h": 0.09},
                "track": {"x": 0.18, "y": 0.82, "w": 0.64, "h": 0.10},
                "clock": {"x": 0.82, "y": 0.82, "w": 0.15, "h": 0.10},
            },
            "edit_mode": False,
        }
        self._layout_edit_target = ""
        self._layout_edit_action = ""
        self._layout_edit_press = QPointF()
        self._layout_edit_start_rect = QRectF()
        self._layout_edit_hover = ""
        self._info_state: dict[str, object] = {
            "title": "", "artist": "", "playing": False, "paused": False,
            "levels": [], "audio_levels_real": False, "audio_levels_kind": "", "temperature": None, "temperature_city": "",
            "temperature_unit": "°C", "temperature_icon": "",
        }
        self._clock_cache_minute = -1
        self._clock_cache_text = ""
        self._info_font_px = -1
        self._info_font: QFont | None = None
        # HOTFIX48: the inline robot waveform owns a high-rate eased display
        # buffer just like the standalone music overlay.  Media state can be
        # refreshed at a much lower cadence without making bars visibly jump.
        self._info_wave_bar_count = 72
        self._info_wave_display = [0.075] * self._info_wave_bar_count
        self._info_wave_targets = [0.075] * self._info_wave_bar_count
        self._blink_until = 0.0
        self._next_blink = random.uniform(2.0, 5.0)
        self._elapsed = 0.0
        self._last_tick = time.perf_counter()
        self._state_changed_at = 0.0
        # State transitions are rendered by drawing the previous and current pose
        # with complementary opacity. Do NOT use QWidget.grab()/QPixmap snapshots
        # here: on Windows, grabbing a rapidly repainting frameless/translucent
        # widget can enter native Qt/DirectComposition code and occasionally crash
        # the entire process with 0xC0000005.
        self._transition_from_state = "idle"
        self._transition_from_idle_action = "none"
        self._transition_started_at = -10.0
        self._safe_ui = os.environ.get("NEO_JARVIS_SAFE_UI", "").strip() == "1"
        self._transition_duration = 0.0 if self._safe_ui else 0.34
        self._idle_action = "none"
        self._sleep_deferred187 = False
        self._idle_action_started_at = 0.0
        self._idle_action_until = 0.0
        self._music_scene_forced = False
        self._music_visual_strength = 0.0
        self._music_hold_until = 0.0
        self._next_idle_action = random.uniform(8.0, 16.0)
        self._gaze_x = 0.0
        self._gaze_target = 0.0
        self._gaze_y = 0.0
        self._gaze_target_y = 0.0
        # Arkanoid uses a tiny real-time physics state instead of a decorative
        # Numeric pose interpolation replaces full-frame cross-fading.  Every
        # head/body/arm target is approached continuously, so state changes are
        # smooth without drawing two complete robot frames (which was unstable on
        # some Windows Qt/DirectComposition stacks).
        self._pose = {
            "center_dx": 0.0, "body_dx": 0.0, "body_dy": 0.0,
            "head_dx": 0.0, "head_dy": 0.0, "float_strength": 5.0,
            "arm_offset": 0.0, "left_extra": 0.0, "right_extra": 0.0,
            "left_w": 28.0, "right_w": 28.0, "left_h": 65.0, "right_h": 65.0,
            "head_tilt": 0.0,
        }
        # NEO 177: every pose/face channel is a damped spring (value + velocity).
        self._pose_vel: dict[str, float] = {}
        self._face_vel: dict[str, float] = {}
        # Smooth eyelid blinks, tiny eye saccades and the accessory life cycle.
        self._blink_started177 = -10.0
        self._saccade177 = [0.0, 0.0]
        self._saccade_target177 = [0.0, 0.0]
        self._next_saccade177 = random.uniform(0.8, 2.4)
        self._acc177 = {"coffee": {"start": -1.0, "next": random.uniform(5.0, 9.0)},
                        "cigarette": {"start": -1.0, "next": random.uniform(3.0, 6.0)}}
        self._particles177: list[list[float]] = []
        self._smoke_clock177 = 0.0
        self._steam_clock177 = 0.0
        self._exhale_until177 = 0.0
        self._exhaled177 = False
        self._combo_phase184 = ""
        self._lowering184: dict[str, tuple[float, float]] = {}
        self._exhale_ring_count184 = 0
        self._exhale_style181 = "cloud"
        self._exhale_started181 = 0.0
        self._wisp_next181 = random.uniform(2.5, 5.0)
        # NEO 185: one ash flick per smoke break, while the hand holds the cigarette aside.
        self._flick_done185 = True
        self._flick_started185 = -10.0
        self._flick_ash_pending185 = False
        self._stream_clock185 = 0.0
        self._ash177 = 0.2
        self._ember177: tuple[float, float, float] | None = None
        self._mouth177: tuple[float, float, float] | None = None
        self._steam177: tuple[float, float, float] | None = None
        self._eq_bars177: list[float] = []
        self._eq_peaks177: list[float] = []
        self._eq_hold177: list[float] = []
        # Facial features are numeric too. Earlier builds interpolated the
        # body but switched happy/sad/listening faces instantly, which still looked
        # like a cut. These values continuously morph between emotions.
        self._face = {
            "eye_h": 22.0, "left_w": 13.0, "right_w": 13.0,
            "eye_y": 0.0, "brow_left": 0.0, "brow_right": 0.0,
            "mouth": 0.0, "mouth_open": 0.0,
        }
        self._timer = QTimer(self)
        # HOTFIX133: 60 Hz is the visual ceiling; idle dynamically drops to 30.
        # This halves QPainter CPU versus the former 120 Hz startup default.
        self._target_fps = 60
        self._timer.setTimerType(Qt.CoarseTimer if self._safe_ui else Qt.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        # A constructed/hidden robot must not consume render ticks.
        self._timer.setInterval(16)

    def set_state(self, state: str) -> None:
        target = state if state in self.STATES else "idle"
        # NEO 187 HREQ-006: falling asleep waits for the idle scene that is
        # already playing (a sip, a wave, a stretch) instead of cutting it; then
        # the usual smooth transition lowers the head, closes the eyes and shows
        # the moon. Any other state cancels the pending sleep.
        if (target == "sleeping" and self._state == "idle" and self._idle_action not in {"none", "", "music_bop"}
                and self._idle_action_until > self._elapsed):
            self._sleep_deferred187 = True
            # A long scene (up to a minute) ends through its own outro within 6 s.
            self._idle_action_until = min(self._idle_action_until, self._elapsed + 6.0)
            self.update()
            return
        self._sleep_deferred187 = False
        self._reply_chunk_until159 = 0.0
        if target != self._state:
            # Remember the previous logical pose. paintEvent blends the two poses
            # without QWidget.grab(), QPixmap snapshots or native screen capture.
            self._begin_visual_transition()
            self._state = target
            self._state_changed_at = self._elapsed
            # NEO 177: a small anticipation dip (or a hop for joy) on every change.
            kick = -95.0 if target in {"happy", "success", "excited", "playful"} else 34.0
            self._pose_vel["body_dy"] = self._pose_vel.get("body_dy", 0.0) + kick
            self._pose_vel["head_dy"] = self._pose_vel.get("head_dy", 0.0) + kick * 1.15
            self._transition_started_at = self._elapsed
            self._idle_action = "none"
            self._idle_action_started_at = 0.0
            self._idle_action_until = 0.0
            # Music rendering is derived from real media state, not this transient
            # UI state. It therefore survives listening/thinking/speaking states.
            self._music_scene_forced = False
            self._gaze_target = 0.0
            self._gaze_target_y = 0.0
            self._select_state_gif()
            self._sync_timer_interval()
            self.state_changed.emit(target)
        self.update()

    def state(self) -> str:
        return self._state

    def set_reply_activity(self, active: bool, text: str = "") -> None:
        """Compatibility for response lifecycle hooks; the avatar has no cloud.

        The normal typing/speaking face follows set_state. Never retain answer
        text, allocate decorations, or start a timer from this legacy hook.
        """
        del active, text

    def show_reply_chunk(self, *, waiting: bool = False) -> None:
        """Show an applied section briefly, then return to its pending reads.

        Uses the existing animation clock; no second timer or task is created.
        Streaming text uses ordinary typing state until generation completes.
        """
        self.set_state("typing")
        if waiting:
            self._reply_chunk_until159 = time.perf_counter() + 0.8

    def _expire_reply_chunk159(self, now: float) -> None:
        if self._reply_chunk_until159 and now >= self._reply_chunk_until159:
            self._reply_chunk_until159 = 0.0
            if self._state == "typing":
                self.set_state("thinking")

    def set_scale_factor(self, value: float) -> None:
        self._scale_factor = max(0.7, min(1.80, float(value)))
        self._update_minimum_height()
        self.updateGeometry()
        self.update()

    def set_layout_scale(self, value: float) -> None:
        self._layout_scale = max(0.72, min(1.70, float(value)))
        self._update_minimum_height()
        self.updateGeometry()
        self.update()

    @staticmethod
    def _default_info_layout() -> dict[str, dict[str, float]]:
        return {
            "temperature": {"x": 0.69, "y": 0.02, "w": 0.28, "h": 0.09},
            "track": {"x": 0.18, "y": 0.82, "w": 0.64, "h": 0.10},
            "clock": {"x": 0.82, "y": 0.82, "w": 0.15, "h": 0.10},
        }

    def layout_edit_enabled(self) -> bool:
        return bool(self._info_config.get("edit_mode", False))

    def _normalized_layout(self) -> dict[str, dict[str, float]]:
        raw = self._info_config.get("layout")
        defaults = self._default_info_layout()
        result: dict[str, dict[str, float]] = {}
        for key, fallback in defaults.items():
            item = raw.get(key, {}) if isinstance(raw, dict) else {}
            if not isinstance(item, dict):
                item = {}
            try:
                x = float(item.get("x", fallback["x"]))
                y = float(item.get("y", fallback["y"]))
                w = float(item.get("w", fallback["w"]))
                h = float(item.get("h", fallback["h"]))
            except Exception:
                x, y, w, h = fallback["x"], fallback["y"], fallback["w"], fallback["h"]
            w = max(0.10, min(0.90, w)); h = max(0.055, min(0.45, h))
            x = max(0.0, min(1.0 - w, x)); y = max(0.0, min(1.0 - h, y))
            result[key] = {"x": x, "y": y, "w": w, "h": h}
        return result

    def _layout_rect(self, key: str) -> QRectF:
        item = self._normalized_layout().get(key, self._default_info_layout()[key])
        return QRectF(
            item["x"] * self.width(), item["y"] * self.height(),
            max(26.0, item["w"] * self.width()), max(20.0, item["h"] * self.height()),
        )

    def _store_layout_rect(self, key: str, rect: QRectF) -> None:
        w0, h0 = max(1.0, float(self.width())), max(1.0, float(self.height()))
        rw = max(0.10, min(0.90, rect.width() / w0))
        rh = max(0.055, min(0.45, rect.height() / h0))
        rx = max(0.0, min(1.0 - rw, rect.left() / w0))
        ry = max(0.0, min(1.0 - rh, rect.top() / h0))
        layout = self._normalized_layout()
        layout[key] = {"x": rx, "y": ry, "w": rw, "h": rh}
        self._info_config["layout"] = layout

    def reset_info_layout(self) -> None:
        self._info_config["layout"] = self._default_info_layout()
        self.layout_changed.emit(self._normalized_layout())
        self.update()

    def _info_reserves(self) -> tuple[int, int, int, int]:
        """Infer a robot-safe center from the user's custom HUD rectangles.

        Items near an edge reserve that edge only.  This lets the robot use all
        remaining space and shrink automatically when the chat or HUD consumes
        more room, without forcing the information back into rigid lanes.
        """
        enabled = {
            "clock": bool(self._info_config.get("clock_enabled", False)),
            "track": bool(self._info_config.get("track_enabled", False)),
            "temperature": bool(self._info_config.get("temperature_enabled", False)),
        }
        W, H = float(max(1, self.width())), float(max(1, self.height()))
        top = right = bottom = left = 0.0
        pad = max(6.0, 9.0 * self._layout_scale)
        for key, on in enabled.items():
            if not on:
                continue
            r = self._layout_rect(key)
            cx, cy = r.center().x() / W, r.center().y() / H
            if cy <= 0.36:
                top = max(top, r.bottom() + pad)
            elif cy >= 0.64:
                bottom = max(bottom, H - r.top() + pad)
            elif cx <= 0.36:
                left = max(left, r.right() + pad)
            elif cx >= 0.64:
                right = max(right, W - r.left() + pad)
        # Never let a badly placed item eliminate the entire robot stage.
        return (
            int(min(H * 0.42, top)), int(min(W * 0.38, right)),
            int(min(H * 0.42, bottom)), int(min(W * 0.38, left)),
        )

    def recommended_stage_size(self) -> tuple[int, int]:
        # HOTFIX49 custom positioning avoids recursive lane growth. The top-level
        # window can stay compact; the robot scales to whatever space remains.
        base_w = 344 if any(bool(self._info_config.get(k, False)) for k in ("track_enabled", "temperature_enabled")) else 322
        base_h = int(222 * self._scale_factor)
        return max(280, base_w), max(165, base_h)

    def _content_center(self) -> tuple[float, float]:
        top, right, bottom, left = self._info_reserves()
        x0, x1 = float(left), float(max(left + 1, self.width() - right))
        y0, y1 = float(top), float(max(top + 1, self.height() - bottom))
        return (x0 + x1) * 0.5, (y0 + y1) * 0.5

    def _content_rect(self) -> QRectF:
        """Actual free rectangle available to the robot after HUD reservations."""
        top, right, bottom, left = self._info_reserves()
        x0 = float(left); y0 = float(top)
        x1 = float(max(left + 1, self.width() - right))
        y1 = float(max(top + 1, self.height() - bottom))
        return QRectF(x0, y0, max(1.0, x1 - x0), max(1.0, y1 - y0))

    def _update_minimum_height(self) -> None:
        # HOTFIX82: robot scale/HUD options must not impose a fixed-height floor
        # on the whole assistant. The painter scales down to the remaining stage
        # when chat height grows. Robot-only mode still uses recommended_stage_size.
        self.setMinimumHeight(72)

    def set_colors(
        self,
        *,
        body: str,
        shell: str,
        screen: str,
        eye: str,
        trim: str,
        glow: str,
    ) -> None:
        values = {
            "body": body,
            "shell": shell,
            "screen": screen,
            "eye": eye,
            "trim": trim,
            "glow": glow,
        }
        for key, value in values.items():
            color = QColor(value)
            if color.isValid():
                self._colors[key] = color
        self.update()

    def set_environment_colors(
        self,
        *,
        moon: str,
        cloud: str,
        rain: str,
        snow: str,
        starfall: str,
        starry_sky: str | None = None,
    ) -> None:
        values = {
            "moon": moon, "cloud": cloud, "rain": rain, "snow": snow,
            "starfall": starfall, "starry_sky": starry_sky or starfall,
        }
        for key, value in values.items():
            candidate = QColor(str(value or ""))
            if candidate.isValid():
                self._environment_colors[key] = candidate
        self.update()

    def set_background(self, color: str) -> None:
        candidate = QColor(color)
        if candidate.isValid():
            self._background = candidate
            self.update()

    def set_transparent_background(self, enabled: bool) -> None:
        self._transparent_background = bool(enabled)
        self._background_opacity_percent = 0 if enabled else 100
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setAutoFillBackground(False)
        self.update()

    def set_background_opacity(self, percent: int) -> None:
        self._background_opacity_percent = max(0, min(100, int(percent)))
        self._transparent_background = self._background_opacity_percent <= 0
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setAutoFillBackground(False)
        self.update()

    def set_active_animation(self, mode: str) -> None:
        value = str(mode or "off").casefold().strip()
        value = value if value in {"off", "rain", "snow", "starfall", "cloudy", "starry_sky", "fireflies"} else "off"
        if value != getattr(self, "_active_animation", "off"):
            # NEO 188 HREQ-006: snow starts building up from this moment, up to a fixed limit.
            self._ambience_since188 = float(getattr(self, "_elapsed", 0.0) or 0.0)
            self._snow_head_since188 = self._ambience_since188
        self._active_animation = value
        self._sync_timer_interval()
        self.update()

    def set_cloud_density(self, value: str) -> None:
        self._cloud_density188 = value if value in {"few", "normal", "many"} else "normal"
        self.update()

    def set_accessories(
        self, *, coffee: bool = False, cigarette: bool = False,
        mug_color: str | None = None, cigarette_color: str | None = None, hand_color: str | None = None,
    ) -> None:
        # NEO 177: the coffee mug and (on request) a cigarette are held in the robot's
        # hands, with sips/puffs, steam and smoke. Both are purely visual.
        self._accessory_coffee = bool(coffee)
        self._accessory_cigarette = bool(cigarette)
        if not (self._accessory_coffee and self._accessory_cigarette):
            self._combo_phase184 = ""
            self._lowering184.clear()
        if not self._accessory_cigarette:
            self._particles177 = [p for p in self._particles177 if p[10] == 0.0]
            self._ember177 = None
            self._acc177["cigarette"]["start"] = -1.0
            self._exhale_until177 = 0.0
        if not self._accessory_coffee:
            self._particles177 = [p for p in self._particles177 if p[10] != 0.0]
            self._steam177 = None
            self._acc177["coffee"]["start"] = -1.0
        for attr, value in (
            ("_accessory_mug_color", mug_color),
            ("_accessory_cigarette_color", cigarette_color),
            ("_accessory_hand_color", hand_color),
        ):
            if value:
                color = QColor(str(value))
                if color.isValid():
                    setattr(self, attr, color)
        # Accessories are composited into the existing render loop: no extra timer.
        self._sync_timer_interval()
        self.update()

    def set_info_config(self, **values: object) -> None:
        allowed_positions = {
            "top_left", "top_center", "top_right",
            "left_center", "right_center",
            "bottom_left", "bottom_center", "bottom_right",
        }
        for key in ("clock_enabled", "track_enabled", "temperature_enabled"):
            if key in values:
                self._info_config[key] = bool(values[key])
        for key in ("clock_position", "track_position", "temperature_position"):
            if key in values:
                value = str(values[key] or "")
                if value in allowed_positions:
                    self._info_config[key] = value
        if "font_size" in values:
            try:
                self._info_config["font_size"] = max(8, min(28, int(values["font_size"])))
            except Exception:
                pass
        if "layout" in values and isinstance(values.get("layout"), dict):
            self._info_config["layout"] = values.get("layout")
            self._info_config["layout"] = self._normalized_layout()
        if "edit_mode" in values:
            self._info_config["edit_mode"] = bool(values.get("edit_mode"))
            if not self.layout_edit_enabled():
                self._layout_edit_target = ""
                self._layout_edit_action = ""
                self.setCursor(Qt.ArrowCursor)
        self._update_minimum_height()
        self.updateGeometry()
        self._sync_timer_interval()
        self.update()

    def set_info_state(self, **values: object) -> None:
        # Avoid scheduling extra full-widget repaints when a coarse status poll
        # reports exactly the same media/weather values as the previous poll.
        # Audio levels still update normally because their list changes while
        # music is playing; command/wake latency is completely independent.
        changed = False
        wave_changed = False
        for key in (
            "title", "artist", "playing", "paused", "levels", "audio_levels_real", "audio_levels_kind",
            "temperature", "temperature_city", "temperature_unit", "temperature_icon", "temperature_stale",
        ):
            if key not in values:
                continue
            value = values[key]
            if self._info_state.get(key) == value:
                continue
            self._info_state[key] = value
            changed = True
            if key in {"playing", "levels", "audio_levels_real", "audio_levels_kind"}:
                wave_changed = True
        if "playing" in values and not bool(values["playing"]):
            self._music_hold_until = 0.0
            self._music_visual_strength = 0.0
            if self._idle_action == "music_bop":
                self._idle_action = ""
                self._idle_action_until = self._elapsed
            self._info_state["title"] = ""
            self._info_state["artist"] = ""
            changed = True
        if not changed:
            return
        if wave_changed:
            self._info_wave_targets = self._inline_wave_targets()
        self._sync_timer_interval()
        self.update()

    def start_idle_scene(self, *, duration: float = 32.0, scene: str | None = None) -> None:
        if self._state != "idle":
            return
        rich_scenes = (
            "solar_system", "galaxy", "constellation",
            "hologram", "paper_plane", "shoulder_roll",
        )
        chosen = scene if scene in rich_scenes else random.choice(rich_scenes)
        self._begin_visual_transition()
        self._idle_action = chosen
        self._idle_action_started_at = self._elapsed
        ceiling = 12.0 if chosen in self._SHORT_IDLE_SCENES else 68.0
        floor = 5.0 if chosen in self._SHORT_IDLE_SCENES else 14.0
        self._idle_action_until = self._elapsed + max(floor, min(ceiling, float(duration)))
        self._next_idle_action = self._idle_action_until + random.uniform(8.0, 16.0)
        self._gaze_target = 0.0
        self._gaze_target_y = 0.0
        if chosen in {"constellation", "hologram"}:
            self._gaze_target = 5.0
        elif chosen == "solar_system":
            # Solar system now lives above the robot; look clearly upward.
            self._gaze_target, self._gaze_target_y = 0.0, -12.0
        elif chosen == "galaxy":
            self._gaze_target, self._gaze_target_y = 8.0, -2.0
        self._sync_timer_interval()
        self.update()

    def finish_idle_scene(self) -> None:
        if self._state != "idle":
            return
        if self._idle_action != "none":
            self._begin_visual_transition()
        self._idle_action = "none"
        self._idle_action_started_at = 0.0
        self._idle_action_until = 0.0
        self._gaze_target = 0.0
        self._gaze_target_y = 0.0
        self._next_idle_action = self._elapsed + random.uniform(7.0, 14.0)
        self._sync_timer_interval()
        self.update()
        if self._sleep_deferred187:
            self.set_state("sleeping")

    def set_frame_rate(self, fps: int) -> None:
        # HOTFIX133: 60 Hz is the visual ceiling. 120 Hz doubled QPainter work
        # with almost no visible benefit on this compact overlay.
        value = int(fps or 60)
        self._target_fps = 30 if value <= 30 else 60
        self._sync_timer_interval()

    def set_headphones(self, enabled: bool, color: str) -> None:
        self._headphones_enabled = bool(enabled)
        candidate = QColor(color)
        if candidate.isValid():
            self._headphones_color = candidate
        self.update()

    def configure_gifs(self, enabled: bool, mapping: dict, scale_percent: int = 100) -> None:
        self._gif_enabled = bool(enabled)
        self._gif_mapping = dict(mapping) if isinstance(mapping, dict) else {}
        self._gif_scale = max(0.2, min(1.8, int(scale_percent) / 100.0))
        self._select_state_gif()

    def _select_state_gif(self) -> None:
        path = str(self._gif_mapping.get(self._state, "")) if self._gif_enabled else ""
        if path == self._gif_path:
            return
        if self._gif_movie is not None:
            self._gif_movie.stop()
            self._gif_movie.deleteLater()
            self._gif_movie = None
        self._gif_path = path
        if path:
            try:
                from pathlib import Path
                from jarvis.ui.gif_assets import inspect_gif, MAX_BYTES
                with Path(path).open("rb") as stream:
                    info = inspect_gif(stream.read(MAX_BYTES + 1))
                movie = QMovie(path, parent=self)
                movie.setCacheMode(QMovie.CacheNone)
                movie.setSpeed(max(1, min(100, int(info["min_delay_ms"] / 17 * 100))))
                movie.frameChanged.connect(lambda _frame: self.update())
                movie.error.connect(self._gif_failed)
                if not movie.isValid():
                    movie.deleteLater()
                    self._gif_path = ""
                else:
                    self._gif_movie = movie
                    movie.start()
                    movie.setPaused(not self.isVisible())
            except (OSError, ValueError):
                self._gif_path = ""
        self._sync_timer_interval()
        self.update()

    def _gif_failed(self, *_args):
        if self._gif_movie:
            self._gif_movie.stop()
            self._gif_movie.deleteLater()
        self._gif_movie = None
        self._gif_path = ""
        self._sync_timer_interval()
        self.update()

    def _sync_timer_interval(self) -> None:
        """One visible animation clock, independent from audio/ASR work."""
        if not self.isVisible():
            self._timer.stop()
            return
        elif self._gif_movie is not None:
            self._timer.stop()
            return
        elif self._safe_ui:
            interval = 33
            desired_type = Qt.CoarseTimer
        else:
            # Dynamic rendering budget: ordinary idle needs only 30 Hz; active
            # interaction and music use 60 Hz so nodding/notes stay fluid.
            music_active = bool(self._info_state.get("playing", False)) and not bool(self._info_state.get("paused", False))
            accessories = bool(self._accessory_coffee or self._accessory_cigarette)
            # NEO 183: the stage weather (rain, snow, stars, clouds, fireflies) moves all the time,
            # so it gets the full 60 Hz too — it used to drop to 30 Hz whenever the robot idled.
            weather = getattr(self, "_active_animation", "off") != "off"
            # 186 (ROBOT185-TIMING-REVIEW): visible accessories keep the full 60 Hz between
            # breaks too — the drifting smoke and steam are animation, and the frame rate
            # is never lowered for optimisation. Savings come from skipping unchanged work.
            active_motion = (self._state not in {"idle", "sleeping"} or music_active or self._idle_action != "none"
                             or (accessories and self._state != "sleeping") or weather)
            fps = 60 if active_motion and self._target_fps >= 60 else 30
            interval = self._FRAME_INTERVALS[fps]
            desired_type = Qt.PreciseTimer if fps >= 60 else Qt.CoarseTimer
        if self._timer.timerType() != desired_type:
            self._timer.setTimerType(desired_type)
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)
        if not self._timer.isActive():
            self._timer.start()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._expire_reply_chunk159(time.perf_counter())
        self._last_tick = time.perf_counter()
        if self._gif_movie:
            self._gif_movie.setPaused(False)
        self._sync_timer_interval()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        if self._gif_movie:
            self._gif_movie.setPaused(True)
        self._sync_timer_interval()

    @staticmethod
    def _smoothstep(value: float) -> float:
        value = max(0.0, min(1.0, float(value)))
        return value * value * (3.0 - 2.0 * value)

    @staticmethod
    def _smootherstep185(value: float) -> float:
        value = max(0.0, min(1.0, float(value)))
        return value * value * value * (value * (value * 6.0 - 15.0) + 10.0)

    def _idle_scene_reveal(self) -> float:
        if self._idle_action == "none" or self._idle_action_started_at <= 0.0:
            return 1.0
        # Large solar choreography changes both scene opacity and robot scale.
        # Give it a longer easing envelope so neither appearance nor return-to-size
        # snaps, while keeping the timing of all other idle toys unchanged.
        intro_seconds = 1.25 if self._idle_action == "solar_system" else 0.48
        outro_seconds = 1.15 if self._idle_action == "solar_system" else 0.42
        intro = self._smoothstep((self._elapsed - self._idle_action_started_at) / intro_seconds)
        if self._idle_action_until <= 0.0:
            return intro
        outro = self._smoothstep((self._idle_action_until - self._elapsed) / outro_seconds)
        return max(0.0, min(intro, outro))

    def _idle_scene_offset(self, action: str) -> tuple[float, float]:
        hidden = 1.0 - self._idle_scene_reveal()
        right_side = self._RIGHT_IDLE_SCENES
        bottom = self._BOTTOM_IDLE_SCENES
        if action in {"galaxy", "constellation", "solar_system"}:
            # Large sky scenes already fade in/out. Sliding the whole scene by
            # half a widget caused galaxies/constellations to be clipped by the
            # RobotWidget even though the top-level window still had free space.
            return 0.0, 0.0
        if action in right_side:
            return self.width() * 0.46 * hidden, 0.0
        if action in bottom:
            return 0.0, self.height() * 0.42 * hidden
        return 0.0, 0.0

    def set_accent(self, color: str) -> None:
        candidate = QColor(color)
        if candidate.isValid():
            self._colors["eye"] = candidate
            self._colors["glow"] = candidate
            self.update()

    def _begin_visual_transition(self) -> None:
        # Pure painter-based transition. We only remember logical state; no native
        # widget screenshots are taken. This keeps transitions smooth and removes a
        # Windows-only access-violation path observed exactly when wake word changed
        # the robot from idle -> listening.
        self._transition_from_state = self._state
        self._transition_from_idle_action = self._idle_action
        self._transition_started_at = self._elapsed

    def _tick(self) -> None:
        if not self.isVisible():
            self._timer.stop()
            return
        try:
            self._advance_animation()
        except KeyboardInterrupt:
            # PyCharm sends Ctrl+C to every Python process when Stop is pressed.
            # Do not print a traceback from a harmless animation callback.
            self._timer.stop()


    @staticmethod
    def _spatial_smooth_wave(values: list[float]) -> list[float]:
        if len(values) < 3:
            return list(values)
        out = list(values)
        for i in range(len(values)):
            left = values[i - 1] if i > 0 else values[i]
            right = values[i + 1] if i + 1 < len(values) else values[i]
            out[i] = left * 0.20 + values[i] * 0.60 + right * 0.20
        return out

    def _inline_wave_targets(self) -> list[float]:
        bars = int(self._info_wave_bar_count)
        playing = bool(self._info_state.get("playing", False)) and not bool(self._info_state.get("paused", False))
        if not playing:
            return [0.075] * bars
        raw_levels = list(self._info_state.get("levels", []) or [])
        levels_are_real = bool(self._info_state.get("audio_levels_real", False))
        levels_kind = str(self._info_state.get("audio_levels_kind", "") or "").casefold()
        if raw_levels and levels_are_real and levels_kind == "spectrum":
            cleaned: list[float] = []
            for item in raw_levels[:96]:
                try:
                    cleaned.append(max(0.0, min(1.0, abs(float(item)))))
                except Exception:
                    continue
            if cleaned:
                mapped: list[float] = []
                n = len(cleaned)
                for i in range(bars):
                    pos = i * (n - 1) / max(1, bars - 1)
                    lo = int(math.floor(pos))
                    hi = min(n - 1, lo + 1)
                    frac = pos - lo
                    value = cleaned[lo] * (1.0 - frac) + cleaned[hi] * frac
                    mapped.append(max(0.045, min(1.0, value)))
                return self._spatial_smooth_wave(mapped)

        # Never fabricate a spectrum while music is playing.  If the backend has
        # not supplied real PCM-derived levels yet (for example during a Discord
        # transport transition), keep a quiet baseline until genuine samples
        # arrive. This makes every visible movement causally tied to audio.
        return [0.075] * bars

    def _advance_inline_wave(self, dt: float) -> None:
        # Source spectra update at the media-poll cadence (~30 Hz). Re-map only
        # when source state changes; the paint clock does cheap interpolation.
        targets = self._info_wave_targets
        if len(self._info_wave_display) != len(targets):
            self._info_wave_display = [0.075] * len(targets)
        for i, target in enumerate(targets):
            current = self._info_wave_display[i]
            rate = 15.0 if target >= current else 10.0
            alpha = 1.0 - math.exp(-rate * dt)
            self._info_wave_display[i] = current + (target - current) * alpha
        # NEO 177: the belly screen shows 28 rounded bars with peak caps that hold
        # for a moment and then fall, like a hardware equalizer (real levels only).
        display = self._info_wave_display
        bars = 28
        if not display:
            return
        samples = []
        for i in range(bars):
            lo_pos = i * len(display) / bars
            hi_pos = (i + 1) * len(display) / bars
            lo = int(lo_pos); hi = max(lo + 1, int(math.ceil(hi_pos)))
            chunk = display[lo:hi]
            samples.append(sum(chunk) / len(chunk))
        if len(self._eq_peaks177) != bars:
            self._eq_peaks177 = list(samples)
            self._eq_hold177 = [0.0] * bars
        for i, value in enumerate(samples):
            if value >= self._eq_peaks177[i]:
                self._eq_peaks177[i] = value
                self._eq_hold177[i] = 0.32
            elif self._eq_hold177[i] > 0.0:
                self._eq_hold177[i] -= dt
            else:
                self._eq_peaks177[i] = max(value, self._eq_peaks177[i] - 0.62 * dt)
        self._eq_bars177 = samples

    def _advance_animation(self) -> None:
        now = time.perf_counter()
        self._expire_reply_chunk159(now)
        dt = max(0.001, min(0.050, now - self._last_tick))
        self._last_tick = now
        angular_speed = self._ANGULAR_SPEEDS.get(self._state, 1.15)
        self._phase = (self._phase + angular_speed * dt) % (math.pi * 2)
        self._elapsed += dt
        if self._timer.interval() <= 8 and (self._elapsed - self._transition_started_at) >= 0.55:
            self._sync_timer_interval()
        self._advance_inline_wave(dt)
        if self._state == "idle" and self._idle_action == "galaxy":
            # Visit five galaxies in a stable loop; gaze has physical smoothing.
            galaxy_gaze = self._GALAXY_GAZE
            # Hold each look long enough for the movement to read as deliberate
            # attention instead of rapid eye jitter.
            idx = int(max(0.0, self._elapsed - self._idle_action_started_at) / 2.75) % len(galaxy_gaze)
            self._gaze_target, self._gaze_target_y = galaxy_gaze[idx]
        elif self._state == "idle" and self._idle_action == "solar_system":
            self._gaze_target, self._gaze_target_y = 0.0, -12.0
        elif self._state == "idle" and self._idle_action in {"paper_plane"}:
            self._gaze_target = 10.0
            self._gaze_target_y = -3.0 + 5.0 * math.sin(self._elapsed * 1.4)

        if self._elapsed >= self._next_blink:
            # NEO 177: a real blink is a curve (fast close, slower open), sometimes doubled.
            self._blink_started177 = self._elapsed
            self._blink_until = self._elapsed + 0.19
            self._next_blink = self._elapsed + (0.30 if random.random() < 0.16 else random.uniform(2.4, 5.8))
        if self._elapsed >= self._next_saccade177:
            # Tiny involuntary eye movements make a resting gaze look alive.
            calm = self._state in {"idle", "listening", "speaking", "curious", "happy"}
            self._saccade_target177 = [random.uniform(-2.4, 2.4), random.uniform(-1.3, 1.3)] if calm else [0.0, 0.0]
            self._next_saccade177 = self._elapsed + random.uniform(0.7, 2.6)
        fast = 1.0 - math.exp(-dt * 30.0)
        self._saccade177[0] += (self._saccade_target177[0] - self._saccade177[0]) * fast
        self._saccade177[1] += (self._saccade_target177[1] - self._saccade177[1]) * fast

        # Small autonomous behaviours make idle feel alive without changing the
        # public state machine. While local/Discord music is actually playing, the
        # normal random scene scheduler yields to a persistent lightweight
        # headphones/music-bop scene. Listening/working states still pre-empt it.
        raw_music_active = bool(self._info_state.get("playing", False)) and not bool(self._info_state.get("paused", False))
        if raw_music_active:
            self._music_hold_until = self._elapsed + 0.55
        music_active = raw_music_active or self._elapsed < self._music_hold_until
        target_music = 1.0 if music_active else 0.0
        if (target_music > 0.0 and getattr(self, "_active_animation", "off") == "snow"
                and self._music_visual_strength < 0.05 and not getattr(self, "_safe_ui", False)):
            # NEO 188 HREQ-006: before the headphones go on, the robot shakes the snow off its head.
            from jarvis.ui.ambience180 import snow_depth
            shake_at = float(getattr(self, "_snow_shake188", -100.0) or -100.0)
            head_since = float(getattr(self, "_snow_head_since188", 0.0) or 0.0)
            if self._elapsed - shake_at > 1.2 and snow_depth(self._elapsed, head_since, 90.0) > 0.15:
                self._snow_shake188 = self._elapsed
                shake_at = self._elapsed
            if self._elapsed - shake_at < 0.9:
                target_music = 0.0
                self._snow_head_since188 = self._elapsed        # the cap is gone; it builds up again slowly
        # One monotonic envelope prevents rapid status polling from repeatedly
        # putting the headphones on/off. Removal is deliberately slower.
        # NEO 177: it runs in every state, so music started by a voice command puts
        # the headphones on right away instead of waiting for the robot to go idle.
        speed = 5.2 if target_music > self._music_visual_strength else 2.7
        self._music_visual_strength += (target_music - self._music_visual_strength) * min(1.0, dt * speed)
        if self._state == "idle":
            if music_active and not self._music_scene_forced:
                self._begin_visual_transition()
                self._music_scene_forced = True
                self._idle_action = "music_bop"
                self._idle_action_started_at = self._elapsed
                self._idle_action_until = 0.0
                self._gaze_target = 0.0
                self._gaze_target_y = 0.0
                self._sync_timer_interval()
            elif not music_active and self._music_scene_forced:
                self._begin_visual_transition()
                self._music_scene_forced = False
                self._idle_action = "none"
                self._idle_action_started_at = 0.0
                self._idle_action_until = 0.0
                self._next_idle_action = self._elapsed + random.uniform(7.0, 14.0)
                self._sync_timer_interval()
            if self._music_scene_forced:
                pass
            elif self._idle_action_until and self._elapsed >= self._idle_action_until:
                self._begin_visual_transition()
                self._idle_action = "none"
                self._idle_action_started_at = 0.0
                self._idle_action_until = 0.0
                self._gaze_target = 0.0
                self._gaze_target_y = 0.0
                self._next_idle_action = self._elapsed + random.uniform(7.0, 14.0)
                self._sync_timer_interval()
                if getattr(self, "_sleep_deferred187", False):
                    self.set_state("sleeping")
            elif not self._idle_action_until and self._elapsed >= self._next_idle_action:
                self._begin_visual_transition()
                self._idle_action = random.choice(
                    [
                        "glance_left", "glance_right", "squint", "wave", "curious",
                        "nod", "head_shake", "peek", "body_sway", "stretch", "yawn",
                        "hologram",
                        "smile", "ponder", "tiny_wave", "inspect_orb",
                        "solar_system", "galaxy", "constellation",
                        "paper_plane", "shoulder_roll",
                    ]
                )
                self._idle_action_started_at = self._elapsed
                # Let idle scenes remain visible long enough to be noticed. Rich
                # space scenes get the longest dwell time so all five gaze targets
                # are visited at least once.
                if self._idle_action in self._SHORT_IDLE_SCENES:
                    self._idle_action_until = self._elapsed + random.uniform(6.0, 10.0)
                elif self._idle_action in {"galaxy", "solar_system", "constellation"}:
                    self._idle_action_until = self._elapsed + random.uniform(34.0, 50.0)
                else:
                    self._idle_action_until = self._elapsed + random.uniform(24.0, 40.0)
                self._sync_timer_interval()
                self._gaze_target_y = 0.0
                if self._idle_action == "glance_left":
                    self._gaze_target = -11.0
                elif self._idle_action == "glance_right":
                    self._gaze_target = 11.0
                elif self._idle_action == "curious":
                    self._gaze_target = 6.0
                elif self._idle_action == "peek":
                    self._gaze_target = 12.0
                elif self._idle_action == "head_shake":
                    self._gaze_target = math.sin(self._phase * 2.1) * 8.0
        else:
            if self._state == "looking":
                self._gaze_target = math.sin(self._phase * 0.7) * 13.0
            elif self._state == "confused":
                self._gaze_target = math.sin(self._phase * 1.1) * 8.0
            elif self._state == "thinking":
                # NEO 177: thinking looks up and aside, reading follows lines, work looks down.
                self._gaze_target, self._gaze_target_y = 7.0, -6.0 + math.sin(self._elapsed * 0.7) * 1.4
            elif self._state == "reading":
                line = (self._elapsed * 0.42) % 1.0
                self._gaze_target, self._gaze_target_y = -8.0 + 16.0 * min(1.0, line / 0.86), 3.0
            elif self._state == "searching":
                self._gaze_target, self._gaze_target_y = math.sin(self._elapsed * 1.1) * 10.0, -2.0
            elif self._state == "browsing":
                self._gaze_target, self._gaze_target_y = math.sin(self._elapsed * 0.8) * 6.0, 2.0
            elif self._state in {"working", "coding", "typing"}:
                self._gaze_target, self._gaze_target_y = 2.5, 4.0
            else:
                self._gaze_target = 0.0
                self._gaze_target_y = 0.0

        # Exponential smoothing is framerate-independent and prevents abrupt eye jumps.
        follow = 1.0 - math.exp(-dt * 8.2)
        self._gaze_x += (self._gaze_target - self._gaze_x) * follow
        self._gaze_y += (self._gaze_target_y - self._gaze_y) * follow
        self._smooth_pose(dt)
        self._smooth_face(dt)
        self._advance_accessories177(dt)
        self.update()

    def _pose_targets(self) -> dict[str, float]:
        state = self._state
        action = self._idle_action
        p = self._phase
        target = {
            "center_dx": 0.0, "body_dx": 0.0, "body_dy": 0.0,
            "head_dx": 0.0, "head_dy": 0.0, "float_strength": 5.0,
            "arm_offset": 4.0 * math.sin(p * 1.4), "left_extra": 0.0, "right_extra": 0.0,
            "left_w": 28.0, "right_w": 28.0, "left_h": 65.0, "right_h": 65.0,
        }
        if state == "listening":
            target.update(center_dx=-13.0, head_dx=-8.0, head_dy=math.sin(p * 0.9) * 2.0, float_strength=2.0,
                          left_extra=-13.0, left_w=39.0, left_h=76.0)
        elif state == "thinking":
            target.update(head_dx=math.sin(p * 0.8) * 4.0, head_dy=-3.0)
        elif state in {"typing", "coding"}:
            target.update(body_dy=abs(math.sin(p * 1.8)) * 2.5, head_dy=abs(math.sin(p * 1.8 + 0.3)) * 3.5)
        elif state in {"dancing", "excited"}:
            target.update(center_dx=math.sin(p * 1.7) * 13.0, body_dx=math.sin(p * 1.8) * 10.0,
                          head_dx=math.sin(p * 1.8 + 0.35) * 15.0, float_strength=9.0)
        elif state == "looking":
            target.update(center_dx=math.sin(p * 0.55) * 5.0, head_dx=math.sin(p * 0.7) * 5.0)
        elif state in {"waving", "excited"}:
            target.update(center_dx=math.sin(p * 1.3) * 5.0, right_extra=12.0,
                          arm_offset=-18.0 + math.sin(p * 2.2) * 10.0)
        elif state in {"stretching"}:
            target.update(left_extra=-35.0, right_extra=35.0, arm_offset=-10.0)
        elif state == "sad":
            target.update(head_dy=5.0, head_dx=-2.0, float_strength=2.0)
        elif state == "sleeping":
            # Slow breathing: tiny body/head movement rather than the generic hover.
            breath = (math.sin(p * 0.42) + 1.0) * 0.5
            settle = (math.sin(self._elapsed * 0.27) + 1.0) * 0.5
            target.update(body_dy=2.0 + breath * 2.4, head_dy=3.0 + breath * 1.6,
                          head_dx=-2.5 - 3.0 * settle, float_strength=0.7)

        shake188 = float(getattr(self, "_elapsed", 0.0) or 0.0) - float(getattr(self, "_snow_shake188", -100.0) or -100.0)
        if 0.0 <= shake188 < 0.9:
            target["head_dx"] += math.sin(shake188 * 26.0) * 10.0 * (1.0 - shake188 / 0.9)
        if state == "idle":
            target["body_dy"] += math.sin(p * 0.55) * 1.8
            target["head_dy"] += math.sin(p * 0.72 + 0.4) * 2.2
            if action == "nod":
                target["head_dy"] += math.sin(p * 2.5) * 8.0
            elif action == "head_shake":
                target["head_dx"] += math.sin(p * 2.8) * 11.0
            elif action == "peek":
                target["head_dx"] += 12.0; target["head_dy"] -= 4.0
            elif action == "body_sway":
                target["body_dx"] += math.sin(p * 1.4) * 10.0
                target["head_dx"] += math.sin(p * 1.4 + 0.15) * 13.0
            elif action == "mini_dance":
                target["body_dx"] += math.sin(p * 2.0) * 14.0
                target["body_dy"] += abs(math.sin(p * 2.0)) * -7.0
                target["head_dx"] += math.sin(p * 2.0 + 0.3) * 17.0
                target["head_dy"] += abs(math.sin(p * 2.0 + 0.2)) * -9.0
                target["float_strength"] = 9.0
            elif action == "stretch":
                target.update(left_extra=-35.0, right_extra=35.0, arm_offset=-10.0)
            elif action == "shoulder_roll":
                roll = math.sin(self._elapsed * 1.65)
                target.update(arm_offset=roll * 14.0, head_dx=roll * 3.5,
                              head_dy=-3.0 * abs(roll), left_extra=-6.0, right_extra=6.0)
            elif action in {"paper_plane"}:
                target.update(head_dx=5.0, head_dy=-3.0, right_extra=8.0,
                              arm_offset=-7.0 + 3.0 * math.sin(self._elapsed * 1.4))
            elif action in {"wave", "tiny_wave"}:
                target.update(right_extra=10.0, arm_offset=-15.0 + math.sin(p * 2.2) * 9.0)
            elif action == "solar_system":
                # HOTFIX108: give the solar system a visibly larger overhead lane.
                # The robot moves down as one composition while remaining inside
                # its fitted stage, so the planets can grow without crossing the
                # shell or clipping the widget edges.
                target["head_dx"] += math.sin(p * 0.32) * 1.8
                target["head_dy"] += 29.0
                target["body_dy"] += 35.0
                target["body_dx"] += math.sin(p * 0.28) * 1.2
                target["float_strength"] = 1.8
            elif action in {"hologram", "inspect_orb", "galaxy", "constellation"}:
                target["head_dx"] += math.sin(p * 0.45) * 4.0; target["head_dy"] -= 3.0
                target["body_dx"] += math.sin(p * 0.34) * 2.0
            elif action == "music_bop":
                # A human-like beat: the head leads with a clear nod while the
                # body follows with a smaller side sway. No sharp/random motion.
                beat = math.sin(p * 2.05)
                target["head_dx"] += math.sin(p * 1.02 + 0.18) * 5.5
                target["head_dy"] += beat * 5.8 - abs(beat) * 1.3
                target["body_dx"] += math.sin(p * 1.02) * 4.2
                target["body_dy"] += abs(math.sin(p * 1.02)) * -1.8
            elif action == "ponder":
                target["head_dx"] += 5.0; target["head_dy"] -= 4.0

        # Music is a persistent physical layer, not merely an idle scene. Keep a
        # gentle nod/sway for the entire time real music is playing, even while
        # NEO temporarily enters listening/thinking/speaking states. Avoid
        # double-applying it when the idle music_bop branch above already ran.
        music_active = bool(self._info_state.get("playing", False)) and not bool(self._info_state.get("paused", False))
        if music_active and not (state == "idle" and action == "music_bop"):
            beat = math.sin(p * 2.05)
            target["head_dx"] += math.sin(p * 1.02 + 0.18) * 5.5
            target["head_dy"] += beat * 5.8 - abs(beat) * 1.3
            target["body_dx"] += math.sin(p * 1.02) * 4.2
            target["body_dy"] += abs(math.sin(p * 1.02)) * -1.8
        # NEO 177: the head tilts into its motion and with the mood (degrees).
        tilt = target["head_dx"] * 0.34
        tilt += {"curious": 7.0, "confused": -8.0, "listening": -2.5, "thinking": 3.5, "sad": -4.5,
                 "sleeping": -7.0, "looking": 2.0}.get(state, 0.0)
        if state == "idle" and action in {"curious", "ponder", "peek"}:
            tilt += 6.0
        if music_active:
            tilt += math.sin(p * 1.02 + 0.5) * 3.5
        target["head_tilt"] = max(-12.0, min(12.0, tilt))
        return target

    # NEO 177: (natural frequency rad/s, damping ratio) per pose channel. The head is
    # softer than the body, so it follows with a slight lag and settles with a small
    # overshoot, like a real mass on a neck, instead of easing in like a slider.
    _SPRINGS177 = {
        "center_dx": (6.0, 0.86), "body_dx": (8.0, 0.74), "body_dy": (8.0, 0.70),
        "head_dx": (6.6, 0.62), "head_dy": (6.6, 0.60), "head_tilt": (5.6, 0.58),
        "float_strength": (3.2, 1.0), "arm_offset": (8.4, 0.55), "left_extra": (7.6, 0.56),
        "right_extra": (7.6, 0.56), "left_w": (9.0, 0.8), "right_w": (9.0, 0.8),
        "left_h": (9.0, 0.8), "right_h": (9.0, 0.8),
    }

    @staticmethod
    def _spring177(values: dict, velocities: dict, targets: dict, dt: float, constants, default: tuple[float, float]) -> None:
        steps = max(1, int(math.ceil(dt * 120.0)))
        h = dt / steps
        for key, goal in targets.items():
            omega, zeta = constants.get(key, default) if isinstance(constants, dict) else constants
            x = float(values.get(key, goal))
            v = float(velocities.get(key, 0.0))
            goal = float(goal)
            for _ in range(steps):
                v += (omega * omega * (goal - x) - 2.0 * zeta * omega * v) * h
                x += v * h
            values[key] = x
            velocities[key] = v

    def _smooth_pose(self, dt: float) -> None:
        self._spring177(self._pose, self._pose_vel, self._pose_targets(), max(0.001, min(0.1, dt)),
                        self._SPRINGS177, (7.0, 0.75))

    def _face_targets(self) -> dict[str, float]:
        state = self._state
        action = self._idle_action
        target = {
            "eye_h": 22.0, "left_w": 13.0, "right_w": 13.0,
            "eye_y": 0.0, "brow_left": 0.0, "brow_right": 0.0,
            "mouth": 0.0, "mouth_open": 0.0,
        }
        if state == "listening":
            target.update(eye_h=27.0, left_w=17.0, right_w=17.0, mouth=-0.05)
        elif state == "thinking":
            target.update(eye_h=12.0, left_w=24.0, right_w=24.0, eye_y=3.0, brow_left=0.18, brow_right=-0.18)
        elif state in {"happy", "success", "playful", "dancing", "excited"}:
            target.update(eye_h=8.0, left_w=24.0, right_w=24.0, mouth=1.0)
        elif state == "sad":
            target.update(eye_h=13.0, left_w=19.0, right_w=19.0, eye_y=2.0, brow_left=-0.75, brow_right=0.75, mouth=-1.0)
        elif state == "error":
            target.update(eye_h=10.0, left_w=20.0, right_w=20.0, brow_left=0.85, brow_right=-0.85, mouth=-0.82)
        elif state in {"curious", "confused"}:
            # V5.3: expressions may tilt brows / lids, but must not make one eye
            # physically thicker than the other.  The old asymmetric widths
            # (14/19 and 19/11) looked like a rendering defect during side glances.
            eye_w = 17.0 if state == "curious" else 16.0
            target.update(eye_h=24.0 if state == "curious" else 16.0, left_w=eye_w,
                          right_w=eye_w, mouth=0.12, brow_left=-0.20, brow_right=0.25)
        elif state == "sleeping":
            target.update(eye_h=2.0, left_w=25.0, right_w=25.0, mouth=0.0)
        elif state == "alert":
            target.update(eye_h=28.0, left_w=15.0, right_w=15.0, mouth_open=0.8)
        elif state in {"speaking", "typing"}:
            target.update(eye_h=21.0, left_w=14.0, right_w=14.0, mouth_open=0.60)
        if state == "idle":
            if action == "smile":
                target.update(eye_h=12.0, left_w=21.0, right_w=21.0, mouth=0.88)
            elif action == "squint":
                target.update(eye_h=8.0, left_w=22.0, right_w=22.0, mouth=0.18)
            elif action in {"curious", "ponder", "inspect_orb", "solar_system", "galaxy", "constellation"}:
                target.update(eye_h=22.0, left_w=17.0, right_w=17.0, brow_left=-0.15, brow_right=0.22, mouth=0.08)
            elif action == "yawn":
                target.update(eye_h=7.0, left_w=23.0, right_w=23.0, mouth_open=1.0)
            elif action in {"music_bop", "mini_dance"}:
                target["mouth"] = 0.55
                # A relaxed brief eye-close every few nod cycles reads as
                # listening to the beat rather than a normal rapid blink.
                if action == "music_bop":
                    phase = (self._elapsed - max(0.0, self._idle_action_started_at)) % 5.9
                    if 4.25 <= phase <= 4.90:
                        target.update(eye_h=2.0, left_w=24.0, right_w=24.0, mouth=0.62)

        # While real music is playing, add a relaxed periodic eye-close in every
        # transient assistant state. This keeps the "listening to music" body
        # language continuous instead of disappearing when a wake command starts.
        music_active = bool(self._info_state.get("playing", False)) and not bool(self._info_state.get("paused", False))
        if music_active:
            phase = (self._elapsed - max(0.0, self._idle_action_started_at)) % 5.9
            if 4.25 <= phase <= 4.90:
                target.update(eye_h=2.0, left_w=24.0, right_w=24.0)

        # NEO 177: a sip of coffee closes the eyes contentedly; a drag narrows them.
        if self._accessory_lift177("coffee") > 0.55:
            target.update(eye_h=3.0, left_w=22.0, right_w=22.0, mouth=0.0, mouth_open=0.0, eye_y=-1.0)
        if self._accessory_lift177("cigarette") > 0.55:
            target.update(eye_h=8.0, left_w=21.0, right_w=21.0, mouth=0.0, mouth_open=0.0)

        # Face-geometry invariant: both luminous eye capsules always keep the
        # same width.  Gaze is expressed by shared translation; emotion by
        # eyelid height and brows.  This protects every current/future animation
        # from accidentally reintroducing the one-thick-eye artifact.
        if self._accessory_coffee or self._accessory_cigarette:
            # NEO 185: no smile with a cigarette and/or coffee, in any state or transition.
            target["mouth"] = min(0.0, float(target.get("mouth", 0.0)))
        eye_w = (float(target.get("left_w", 13.0)) + float(target.get("right_w", 13.0))) * 0.5
        target["left_w"] = eye_w
        target["right_w"] = eye_w
        return target

    def _smooth_face(self, dt: float) -> None:
        self._spring177(self._face, self._face_vel, self._face_targets(), max(0.001, min(0.1, dt)),
                        (10.5, 0.82), (10.5, 0.82))

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        parent = self.parentWidget()
        clip_factory = getattr(parent, "content_clip_path184", None)
        if callable(clip_factory):
            # The parent already paints this same stage colour. Never clear a
            # square hole in its rounded frame with CompositionMode_Source.
            clip = clip_factory()
            clip.translate(-self.x(), -self.y())
            painter.setClipPath(clip)
        else:
            painter.setCompositionMode(QPainter.CompositionMode_Source)
            if self._transparent_background or self._background_opacity_percent <= 0:
                painter.fillRect(self.rect(), Qt.transparent)
            else:
                stage_background = QColor(self._background)
                stage_background.setAlpha(round(255 * self._background_opacity_percent / 100.0))
                painter.fillRect(self.rect(), stage_background)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.setClipRect(self.rect())
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
        if self._gif_movie is not None:
            pixmap = self._gif_movie.currentPixmap()
            if not pixmap.isNull():
                fit = pixmap.size().scaled(self.size(), Qt.KeepAspectRatio)
                width, height = fit.width() * self._gif_scale, fit.height() * self._gif_scale
                painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
                painter.drawPixmap(QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height), pixmap, QRectF(pixmap.rect()))
                self._draw_info_hud(painter)
                return
        self._draw_active_animation(painter)
        # Single-pass rendering is both smoother and more stable on Windows. Pose
        # values themselves are interpolated continuously, so there is no hard cut
        # when state/emotion changes and no double-frame compositor work.
        self._paint_robot_frame(painter, self._state, self._idle_action)
        self._draw_active_overlay188(painter)
        self._draw_info_hud(painter)

    def _draw_active_overlay188(self, painter: QPainter) -> None:
        mode = getattr(self, "_active_animation", "off")
        if mode not in {"rain", "snow"} or self.width() <= 1 or self.height() <= 1:
            return
        from jarvis.ui import ambience180
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        ambience180.draw_overlay(self, painter, mode, float(self.width()), float(self.height()), float(self._elapsed))
        painter.restore()

    def _clock_text(self) -> str:
        # The HUD is painted up to 120 FPS, while HH:MM changes only once a
        # minute. Preserve exact displayed text without calling strftime each frame.
        minute = int(time.time() // 60)
        if minute != self._clock_cache_minute:
            self._clock_cache_minute = minute
            self._clock_cache_text = time.strftime("%H:%M")
        return self._clock_cache_text

    def _draw_info_hud(self, painter: QPainter) -> None:
        """Draw movable, resizable and permanently frameless robot HUD items."""
        cfg = self._info_config
        edit = self.layout_edit_enabled()
        enabled = {
            "clock": bool(cfg.get("clock_enabled", False)),
            "track": bool(cfg.get("track_enabled", False)),
            "temperature": bool(cfg.get("temperature_enabled", False)),
        }
        if not any(enabled.values()):
            return
        title = str(self._info_state.get("title", "") or "").strip()
        artist = str(self._info_state.get("artist", "") or "").strip()
        payloads: dict[str, str] = {
            "clock": self._clock_text(),
            "track": (f"♪ {title}{' — ' + artist if artist else ''}" if (title or artist) else ("♪ Сейчас играет" if edit else "")),
            "temperature": "",
        }
        if self._info_state.get("temperature") is not None:
            try:
                temp = float(self._info_state.get("temperature"))
                unit = str(self._info_state.get("temperature_unit", "°C") or "°C")
                city = str(self._info_state.get("temperature_city", "") or "").strip()
                icon = str(self._info_state.get("temperature_icon", "") or "").strip()
                payloads["temperature"] = f"{icon + ' ' if icon else ''}{city + '  ' if city else ''}{temp:+.0f}{unit}"
                if self._info_state.get("temperature_stale"):
                    payloads["temperature"] += " ⏱"
            except Exception:
                pass
        elif edit:
            payloads["temperature"] = "Город  +13°C"

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        font_px = max(8, min(28, int(cfg.get("font_size", 12) or 12)))
        accent = QColor(self._colors["glow"]); accent.setAlpha(245)
        text_color = QColor(self._colors["shell"]); text_color.setAlpha(248)
        shadow = QColor("#000000"); shadow.setAlpha(155)
        if self._info_font is None or self._info_font_px != font_px:
            self._info_font_px = font_px
            self._info_font = QFont("Segoe UI", font_px, QFont.DemiBold)
        painter.setFont(self._info_font)
        fm = painter.fontMetrics()

        for key in ("temperature", "track", "clock"):
            if not enabled[key]:
                continue
            rect = self._layout_rect(key).adjusted(2.0, 2.0, -2.0, -2.0)
            text = payloads.get(key, "")
            if not text:
                continue
            painter.save()
            painter.setClipRect(rect)
            full_w = float(fm.horizontalAdvance(text))
            if key == "track" and full_w > rect.width() - 4.0:
                gap = 42.0; speed = 34.0; loop = full_w + gap
                offset = (self._elapsed * speed) % loop
                positions = (rect.left() + 2.0 - offset, rect.left() + 2.0 - offset + loop)
            else:
                positions = (rect.left() + max(2.0, (rect.width() - full_w) / 2.0),)
            for tx in positions:
                ty = rect.center().y() + (fm.ascent() - fm.descent()) / 2.0
                painter.setPen(shadow); painter.drawText(QPointF(tx + 1.0, ty + 1.0), text)
                painter.setPen(text_color); painter.drawText(QPointF(tx, ty), text)
            painter.restore()

            # Editing guides exist only while layout editing is explicitly enabled.
            # They are never part of the normal HUD, preserving the frameless look.
            if edit:
                guide = QColor(accent); guide.setAlpha(185 if key == self._layout_edit_target else 105)
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(guide, 1.0, Qt.DashLine))
                painter.drawRoundedRect(rect, 5.0, 5.0)
                handle = 8.0
                painter.setPen(Qt.NoPen); painter.setBrush(guide)
                painter.drawRect(QRectF(rect.right()-handle, rect.bottom()-handle, handle, handle))
                painter.drawRect(QRectF(rect.right()-4.0, rect.top()+14.0, 4.0, max(10.0, rect.height()-28.0)))
                painter.drawRect(QRectF(rect.left()+14.0, rect.bottom()-4.0, max(10.0, rect.width()-28.0), 4.0))
                tiny = QFont("Segoe UI", max(7, font_px - 3)); painter.setFont(tiny)
                painter.setPen(guide); painter.drawText(QPointF(rect.left()+3.0, rect.top()+max(9.0, font_px-1.0)), key.upper())
                painter.setFont(QFont("Segoe UI", font_px, QFont.DemiBold))
        painter.restore()

    def _editable_info_keys(self) -> tuple[str, ...]:
        return tuple(key for key in ("clock", "track", "temperature") if bool(self._info_config.get(f"{key}_enabled", False)))

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.layout_edit_enabled() and event.button() == Qt.LeftButton:
            pos = event.position()
            for key in reversed(self._editable_info_keys()):
                rect = self._layout_rect(key)
                if rect.contains(pos):
                    corner = QRectF(rect.right()-16.0, rect.bottom()-16.0, 18.0, 18.0)
                    right_handle = QRectF(rect.right()-8.0, rect.top()+14.0, 12.0, max(16.0, rect.height()-28.0))
                    bottom_handle = QRectF(rect.left()+14.0, rect.bottom()-8.0, max(16.0, rect.width()-28.0), 12.0)
                    self._layout_edit_target = key
                    if corner.contains(pos):
                        self._layout_edit_action = "resize_xy"
                        cursor = Qt.SizeFDiagCursor
                    elif bottom_handle.contains(pos):
                        self._layout_edit_action = "resize_y"
                        cursor = Qt.SizeVerCursor
                    elif right_handle.contains(pos):
                        self._layout_edit_action = "resize_x"
                        cursor = Qt.SizeHorCursor
                    else:
                        self._layout_edit_action = "move"
                        cursor = Qt.ClosedHandCursor
                    self._layout_edit_press = QPointF(pos)
                    self._layout_edit_start_rect = QRectF(rect)
                    self.setCursor(cursor)
                    event.accept(); self.update(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.layout_edit_enabled() and self._layout_edit_target and self._layout_edit_action:
            delta = event.position() - self._layout_edit_press
            rect = QRectF(self._layout_edit_start_rect)
            if self._layout_edit_action == "move":
                rect.translate(delta)
            else:
                min_w = 92.0 if self._layout_edit_target != "clock" else 58.0
                min_h = 24.0
                if self._layout_edit_action in {"resize_x", "resize_xy"}:
                    rect.setWidth(max(min_w, rect.width() + delta.x()))
                if self._layout_edit_action in {"resize_y", "resize_xy"}:
                    rect.setHeight(max(min_h, rect.height() + delta.y()))
            # Keep every element fully inside the scene.
            if rect.left() < 0: rect.moveLeft(0)
            if rect.top() < 0: rect.moveTop(0)
            if rect.right() > self.width(): rect.moveRight(self.width())
            if rect.bottom() > self.height(): rect.moveBottom(self.height())
            self._store_layout_rect(self._layout_edit_target, rect)
            self.update(); event.accept(); return
        if self.layout_edit_enabled():
            pos = event.position(); hover = ""; resize = False
            cursor = Qt.ArrowCursor
            for key in reversed(self._editable_info_keys()):
                rect = self._layout_rect(key)
                if rect.contains(pos):
                    hover = key
                    corner = QRectF(rect.right()-16.0, rect.bottom()-16.0, 18.0, 18.0)
                    right_handle = QRectF(rect.right()-8.0, rect.top()+14.0, 12.0, max(16.0, rect.height()-28.0))
                    bottom_handle = QRectF(rect.left()+14.0, rect.bottom()-8.0, max(16.0, rect.width()-28.0), 12.0)
                    if corner.contains(pos):
                        cursor = Qt.SizeFDiagCursor
                    elif bottom_handle.contains(pos):
                        cursor = Qt.SizeVerCursor
                    elif right_handle.contains(pos):
                        cursor = Qt.SizeHorCursor
                    else:
                        cursor = Qt.OpenHandCursor
                    break
            self._layout_edit_hover = hover
            self.setCursor(cursor if hover else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.layout_edit_enabled() and event.button() == Qt.LeftButton and self._layout_edit_target:
            self._layout_edit_target = ""
            self._layout_edit_action = ""
            self.setCursor(Qt.ArrowCursor)
            self.layout_changed.emit(self._normalized_layout())
            self.update(); event.accept(); return
        super().mouseReleaseEvent(event)

    def _draw_active_animation(self, painter: QPainter) -> None:
        """NEO 180: layered stage weather (jarvis/ui/ambience180.py)."""
        mode = self._active_animation
        if mode == "off" or self.width() <= 1 or self.height() <= 1:
            return
        from jarvis.ui import ambience180
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        ambience180.draw(self, painter, mode, float(self.width()), float(self.height()), float(self._elapsed))
        painter.restore()

    def _draw_belly_equalizer(self, painter: QPainter, body_rect: QRectF, scale: float) -> None:
        """NEO 177: a glass screen on the chest; while music plays it shows the real spectrum."""
        panel = body_rect.adjusted(15 * scale, 13 * scale, -15 * scale, -12 * scale)
        if panel.width() <= 6 or panel.height() <= 6:
            return
        painter.save()
        screen = QLinearGradient(panel.topLeft(), panel.bottomLeft())
        screen.setColorAt(0.0, QColor(self._colors["screen"]).lighter(150))
        screen.setColorAt(1.0, QColor(self._colors["screen"]).darker(115))
        painter.setPen(QPen(QColor(self._colors["trim"]).darker(125), max(0.8, 1.4 * scale)))
        painter.setBrush(screen)
        painter.drawRoundedRect(panel, 17 * scale, 17 * scale)
        accent = QColor(self._colors["glow"])
        playing = bool(self._info_state.get("playing", False)) and not bool(self._info_state.get("paused", False))
        bars = list(getattr(self, "_eq_bars177", []) or [])
        if not playing or not bars:
            # At rest: a calm status light that breathes with the robot.
            breath = 0.5 + 0.5 * math.sin(self._elapsed * 1.55)
            status = QColor(accent); status.setAlpha(int(45 + 70 * breath))
            halo = QColor(accent); halo.setAlpha(int(18 + 26 * breath))
            painter.setPen(QPen(halo, max(2.0, 5.0 * scale), Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(panel.center().x() - 17 * scale, panel.center().y()), QPointF(panel.center().x() + 17 * scale, panel.center().y()))
            painter.setPen(QPen(status, max(1.0, 2.0 * scale), Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(panel.center().x() - 14 * scale, panel.center().y()), QPointF(panel.center().x() + 14 * scale, panel.center().y()))
            painter.restore()
            return
        inner = panel.adjusted(9 * scale, 6 * scale, -9 * scale, -6 * scale)
        count = len(bars)
        step = inner.width() / count
        bar_w = max(1.2, step * 0.58)
        baseline = inner.center().y()
        max_half = inner.height() * 0.46
        shape = QPainterPath()
        peaks = QPainterPath()
        for i, value in enumerate(bars):
            half = max(1.1 * scale, max_half * max(0.04, min(1.0, float(value))))
            x = inner.left() + (i + 0.5) * step
            shape.addRoundedRect(QRectF(x - bar_w / 2, baseline - half, bar_w, half * 2), bar_w / 2, bar_w / 2)
            peak = self._eq_peaks177[i] if i < len(self._eq_peaks177) else value
            peak_half = max(half + 1.6 * scale, max_half * max(0.04, min(1.0, float(peak))) + 1.6 * scale)
            if peak_half < max_half + 2.0 * scale:
                peaks.addEllipse(QPointF(x, baseline - peak_half), bar_w * 0.48, 0.9 * scale)
                peaks.addEllipse(QPointF(x, baseline + peak_half), bar_w * 0.48, 0.9 * scale)
        glow = QColor(accent); glow.setAlpha(60)
        painter.setPen(QPen(glow, max(1.5, bar_w * 0.9), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(shape)
        fill = QLinearGradient(QPointF(0, baseline - max_half), QPointF(0, baseline + max_half))
        tip = QColor(accent).lighter(150)
        fill.setColorAt(0.0, tip); fill.setColorAt(0.5, accent); fill.setColorAt(1.0, tip)
        painter.setPen(Qt.NoPen)
        painter.setBrush(fill)
        painter.drawPath(shape)
        cap = QColor(accent).lighter(170); cap.setAlpha(220)
        painter.setBrush(cap)
        painter.drawPath(peaks)
        # reflection on the glass
        painter.setBrush(QColor(255, 255, 255, 14))
        painter.drawRoundedRect(QRectF(panel.left() + 4 * scale, panel.top() + 2.5 * scale, panel.width() - 8 * scale, panel.height() * 0.32), 12 * scale, 12 * scale)
        painter.restore()

    _LIGHT186 = (-0.6, -0.8)       # unit vector towards the light (upper left)

    def _reflection_geometry186(self, dx: float, dy: float, tilt: float) -> tuple[float, float, float, int]:
        """Local band shift (x, y), counter-rotation (degrees) and alpha for a head pose."""
        lx, ly = self._LIGHT186
        shift_x = max(-16.0, min(16.0, -0.55 * dx))
        shift_y = max(-10.0, min(10.0, -0.45 * dy))
        distance = math.hypot(dx, dy)
        facing = 0.0 if distance < 1e-6 else (dx * lx + dy * ly) / distance * min(1.0, distance / 18.0)
        alpha = int(round(max(6.0, min(24.0, 13.0 * (1.0 + 0.55 * facing - 0.012 * abs(tilt))))))
        return shift_x, shift_y, -float(tilt), alpha

    def _draw_glass_reflection186(self, painter: QPainter, face_rect: QRectF, scale: float,
                                  dx: float, dy: float, tilt: float) -> None:
        shift_x, shift_y, angle, alpha = self._reflection_geometry186(dx, dy, tilt)
        self._reflection186 = (shift_x, shift_y, angle, alpha)
        painter.save()
        screen = QPainterPath()
        screen.addRoundedRect(face_rect, 32 * scale, 32 * scale)
        painter.setClipPath(screen, Qt.IntersectClip)
        center = face_rect.center()
        painter.translate(center.x() + shift_x * scale, center.y() + shift_y * scale)
        painter.rotate(angle)
        painter.translate(-center.x(), -center.y())
        band = QPainterPath()
        band.moveTo(face_rect.left() + 46 * scale, face_rect.top() - 6 * scale)
        band.lineTo(face_rect.left() + 82 * scale, face_rect.top() - 6 * scale)
        band.lineTo(face_rect.left() + 42 * scale, face_rect.top() + 72 * scale)
        band.lineTo(face_rect.left() + 8 * scale, face_rect.top() + 72 * scale)
        band.closeSubpath()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, alpha))
        painter.drawPath(band)
        painter.restore()

    def _paint_robot_frame(
        self, painter: QPainter, state: str, idle_action: str
    ) -> None:
        # Existing drawing helpers read self._state/self._idle_action. Temporarily
        # swap only these plain Python fields while drawing one pose; this stays
        # entirely inside paintEvent on the GUI thread.
        actual_state = self._state
        actual_action = self._idle_action
        self._state = state
        self._idle_action = idle_action
        try:
            desired_scale = min(self.width() / 520, self.height() / 300) * self._scale_factor
            content_rect = self._content_rect()
            # Fit against the actual visual extents (ears + head + torso), not the
            # whole widget. This prevents clipping when track/clock HUD reserves
            # part of the stage or when the user selected >100% robot scale.
            free_w = max(1.0, content_rect.width() - 12.0)
            free_h = max(1.0, content_rect.height() - 10.0)
            music_active = bool(self._info_state.get("playing", False)) and not bool(self._info_state.get("paused", False))
            # Headphones extend ~36 units above the normal shell and follow the
            # nod sideways. Reserve that extra envelope when music is active so
            # the arch is never clipped even after the user resizes the robot.
            fit_w_units = 300.0 if music_active else 282.0
            fit_h_units = 258.0 if music_active else 226.0
            if self._accessory_coffee:
                fit_w_units += 44.0          # NEO 177: the mug floats beside the torso
            if self._accessory_cigarette:
                fit_w_units += 56.0          # NEO 181: a magnetic hand holds the cigarette aside during a smoke break
            micro_reveal = self._idle_scene_reveal() if state == "idle" and idle_action in {"paper_plane"} else 0.0
            # Reserve the decoration's real envelope before fitting, including
            # at the smallest tray/overlay sizes and with a custom HUD layout.
            side_reveal = micro_reveal
            fit_w_units += (430.0 - fit_w_units) * side_reveal
            fit_scale = min(free_w / fit_w_units, free_h / fit_h_units)
            scale = max(0.08, min(1.45, desired_scale, fit_scale))
            # HOTFIX108: the solar-system pose intentionally lowers the robot so
            # the orbital toy can grow above it. Account for that asymmetric pose
            # when fitting the robot, otherwise short stages can clip the torso.
            # In this pose the visual bottom is ~167 scale units below centre.
            if idle_action == "solar_system":
                solar_bottom_room = max(1.0, content_rect.height() * 0.5 - 5.0)
                normal_scale = scale
                solar_scale = max(0.08, min(normal_scale, solar_bottom_room / 167.0))
                # Use the same eased reveal as the orbital scene itself. On entry
                # the robot shrinks gradually; before the scene disappears it grows
                # back to its normal size, eliminating both visual jumps.
                reveal = self._idle_scene_reveal()
                scale = normal_scale + (solar_scale - normal_scale) * reveal
            pose = self._pose
            content_cx, content_cy = content_rect.center().x(), content_rect.center().y()
            center_x = content_cx + float(pose.get("center_dx", 0.0)) * scale
            center_x -= 63.0 * scale * side_reveal
            float_strength = float(pose.get("float_strength", 5.0))
            hover = math.sin(self._phase) * float_strength
            center_y = content_cy - (7.0 if music_active else 3.0) + hover
            body_x = center_x + float(pose.get("body_dx", 0.0)) * scale
            body_y = center_y + float(pose.get("body_dy", 0.0)) * scale
            head_x = center_x + float(pose.get("head_dx", 0.0)) * scale
            # Raise the head slightly so the belly equalizer has clear breathing
            # room instead of visually touching the shell.
            head_y = center_y + float(pose.get("head_dy", 0.0)) * scale - 8.0 * scale
            # NEO 177: short physical accents on top of the springs: an error shakes the
            # head «no», success/joy gives a little hop.
            shake, hop = self._state_accent177()
            head_x += shake * scale
            body_x += shake * 0.4 * scale
            body_y -= hop * scale
            head_y -= hop * 1.15 * scale
            tilt = float(pose.get("head_tilt", 0.0)) + shake * 0.35
            breath = self._breath177()
            self._last_scale177 = scale

            eye_color = QColor("#FF6478") if self._state == "error" else QColor(self._colors["eye"])
            if self._state == "sad":
                eye_color = QColor(self._colors["eye"]).darker(115)
            glow_color = QColor(eye_color if self._state == "error" else self._colors["glow"])

            # Soft contact shadow: the robot floats above it, so it shrinks and fades as it rises.
            self._draw_ground_shadow177(painter, body_x, content_cy - (7.0 if music_active else 3.0), scale, hover + hop)

            # Ambient aura: a radial falloff instead of a flat disc.
            aura_strength = 0.55 + 0.45 * (math.sin(self._phase) + 1) / 2
            aura_center = QPointF((head_x + body_x) / 2, (head_y + body_y) / 2 + 6 * scale)
            aura_color = QColor(glow_color); aura_color.setAlpha(58)
            self._draw_glow177(painter, aura_center, 150 * scale, 124 * scale, aura_color, aura_strength,
                               ((0.0, 1.0), (0.55, 0.38), (1.0, 0.0)))

            # Arms are interpolated as part of the numeric pose as well.
            arm_offset = float(pose.get("arm_offset", 0.0)) * scale

            # ---- torso (breathes: a slight vertical expansion around its base) ----
            body_rect = QRectF(body_x - 87 * scale, body_y + 54 * scale, 174 * scale, 78 * scale)
            painter.save()
            painter.translate(body_x, body_rect.bottom())
            painter.scale(1.0 + breath * 0.35, 1.0 + breath)
            painter.translate(-body_x, -body_rect.bottom())
            body_gradient = QLinearGradient(body_rect.topLeft(), body_rect.bottomLeft())
            body_gradient.setColorAt(0, self._colors["body"].lighter(138))
            body_gradient.setColorAt(0.45, self._colors["body"].lighter(108))
            body_gradient.setColorAt(1, self._colors["body"].darker(150))
            painter.setBrush(body_gradient)
            painter.setPen(QPen(self._colors["trim"], max(1.2, 2 * scale)))
            painter.drawRoundedRect(body_rect, 29 * scale, 29 * scale)
            # Rim light along the lower edge picks up the accent colour.
            rim = QLinearGradient(body_rect.topLeft(), body_rect.bottomLeft())
            rim_on = QColor(glow_color); rim_on.setAlpha(110)
            rim_off = QColor(glow_color); rim_off.setAlpha(0)
            rim.setColorAt(0.55, rim_off); rim.setColorAt(1.0, rim_on)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(rim, max(1.0, 1.6 * scale)))
            painter.drawRoundedRect(body_rect.adjusted(1.4 * scale, 1.4 * scale, -1.4 * scale, -1.4 * scale), 27.5 * scale, 27.5 * scale)
            self._draw_belly_equalizer(painter, body_rect, scale)
            painter.restore()

            # ---- head group: ears, shell, face, expression and headphones tilt together ----
            painter.save()
            if abs(tilt) >= 0.2:
                # (below a fifth of a degree the rotation is invisible but doubles the cost)
                painter.translate(head_x, head_y)
                painter.rotate(tilt)
                painter.translate(-head_x, -head_y)
            else:
                tilt = 0.0
            self._head_frame177 = (head_x, head_y, tilt)
            self._head_geom188 = (head_x, head_y, scale, tilt)
            # Side ear panels belong to the head, not the torso.
            left_x = head_x - 126 * scale + float(pose.get("left_extra", 0.0)) * scale
            right_x = head_x + 98 * scale + float(pose.get("right_extra", 0.0)) * scale
            left_w = float(pose.get("left_w", 28.0)) * scale
            right_w = float(pose.get("right_w", 28.0)) * scale
            left_h = float(pose.get("left_h", 65.0)) * scale
            right_h = float(pose.get("right_h", 65.0)) * scale
            listening = 1.0 if self._state == "listening" else 0.0
            for ear_rect in (QRectF(left_x, head_y - 23 * scale + arm_offset, left_w, left_h),
                             QRectF(right_x, head_y - 23 * scale - arm_offset, right_w, right_h)):
                ear_gradient = QLinearGradient(ear_rect.topLeft(), ear_rect.topRight())
                ear_gradient.setColorAt(0, self._colors["body"].lighter(132))
                ear_gradient.setColorAt(1, self._colors["body"].darker(140))
                painter.setBrush(ear_gradient)
                painter.setPen(QPen(self._colors["trim"], max(1.2, 2 * scale)))
                painter.drawRoundedRect(ear_rect, 13 * scale, 13 * scale)
                # A small status light on each ear: brighter while NEO listens.
                led = QColor(glow_color)
                pulse = 0.5 + 0.5 * math.sin(self._elapsed * (5.0 if listening else 1.6))
                led.setAlpha(int(70 + 60 * pulse + 110 * listening))
                painter.setPen(Qt.NoPen)
                led_center = QPointF(ear_rect.center().x(), ear_rect.top() + 14 * scale)
                halo_color = QColor(glow_color); halo_color.setAlpha(255)
                self._draw_glow177(painter, led_center, 9 * scale, 9 * scale, halo_color, led.alpha() / 255.0 * 0.45)
                painter.setBrush(led)
                painter.drawEllipse(led_center, 2.6 * scale, 2.6 * scale)

            head_rect = QRectF(head_x - 103 * scale, head_y - 72 * scale, 206 * scale, 146 * scale)
            shell_gradient = QLinearGradient(head_rect.topLeft(), head_rect.bottomRight())
            shell_gradient.setColorAt(0, self._colors["shell"].lighter(122))
            shell_gradient.setColorAt(0.55, self._colors["shell"])
            shell_gradient.setColorAt(1, self._colors["shell"].darker(128))
            painter.setBrush(shell_gradient)
            painter.setPen(QPen(self._colors["trim"].lighter(130), max(1.5, 3 * scale)))
            painter.drawRoundedRect(head_rect, 55 * scale, 55 * scale)
            # Glossy highlight on the upper-left of the shell.
            gloss = QPainterPath()
            gloss.moveTo(head_rect.left() + 26 * scale, head_rect.top() + 46 * scale)
            gloss.cubicTo(head_rect.left() + 28 * scale, head_rect.top() + 14 * scale,
                          head_rect.left() + 64 * scale, head_rect.top() + 6 * scale,
                          head_rect.left() + 104 * scale, head_rect.top() + 7 * scale)
            gloss_color = QColor(255, 255, 255, 120)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(gloss_color, max(1.2, 4.2 * scale), Qt.SolidLine, Qt.RoundCap))
            painter.drawPath(gloss)

            face_rect = QRectF(head_x - 78 * scale, head_y - 45 * scale, 156 * scale, 91 * scale)
            screen_gradient = QLinearGradient(face_rect.topLeft(), face_rect.bottomLeft())
            screen_gradient.setColorAt(0, self._colors["screen"].lighter(145))
            screen_gradient.setColorAt(0.5, self._colors["screen"])
            screen_gradient.setColorAt(1, self._colors["screen"].darker(118))
            painter.setBrush(screen_gradient)
            painter.setPen(QPen(self._colors["trim"].darker(130), max(1.0, 2 * scale)))
            painter.drawRoundedRect(face_rect, 32 * scale, 32 * scale)
            # Glass reflection (186, USER185-11): a cheap 2D mirror model of one
            # fixed light at the upper left of the window. When the head moves or
            # tilts, the band slides across the glass the opposite way, keeps its
            # world orientation and brightens while the face turns toward the
            # light; it is clipped by the rounded screen. No randomness and no
            # extra repaint: it follows the pose already drawn in this frame.
            self._draw_glass_reflection186(painter, face_rect, scale,
                                           (head_x - content_cx) / max(scale, 1e-6),
                                           (head_y - content_cy) / max(scale, 1e-6), tilt)

            self._draw_expression(painter, head_x, head_y, scale, eye_color)
            self._draw_headphones(painter, head_x, head_y, scale)
            painter.restore()

            painter.save()
            self._draw_state_effect(painter, head_x, head_y, scale, eye_color)
            self._draw_idle_toy(painter, head_x, head_y, scale, eye_color)
            self._draw_micro_scene(painter, head_x, head_y, scale, eye_color)
            self._draw_persistent_accessories(painter, head_x, head_y, body_x, body_y, scale, arm_offset)
            self._draw_particles177(painter)
            painter.restore()

        finally:
            self._state = actual_state
            self._idle_action = actual_action

    # ---- NEO 177: physical accents, breathing, shadow, accessories, particles ----

    def _glow_sprite177(self, radius: float, color: QColor, stops: tuple[tuple[float, float], ...] = ((0.0, 1.0), (1.0, 0.0))) -> QImage:
        """A soft radial glow rendered once into an image and reused every frame.

        Gradients filled per frame were the most expensive part of a frame; a cached
        sprite blits ~10x faster and looks identical. Rebuilt only when size/colour change."""
        size = max(2, int(math.ceil(radius * 2.0)))
        key = (size, color.rgba(), stops)
        cache = self.__dict__.setdefault("_sprites177", {})
        sprite = cache.get(key)
        if sprite is None:
            if len(cache) > 48:
                cache.clear()
            sprite = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
            sprite.fill(Qt.transparent)
            painter = QPainter(sprite)
            painter.setRenderHint(QPainter.Antialiasing, True)
            gradient = QRadialGradient(QPointF(size / 2.0, size / 2.0), size / 2.0)
            for at, strength in stops:
                stop = QColor(color)
                stop.setAlphaF(max(0.0, min(1.0, color.alphaF() * strength)))
                gradient.setColorAt(at, stop)
            painter.setPen(Qt.NoPen)
            painter.setBrush(gradient)
            painter.drawEllipse(QRectF(0.0, 0.0, size, size))
            painter.end()
            cache[key] = sprite
        return sprite

    def _draw_glow177(self, painter: QPainter, center: QPointF, rx: float, ry: float, color: QColor,
                      opacity: float = 1.0, stops: tuple[tuple[float, float], ...] = ((0.0, 1.0), (1.0, 0.0)),
                      sprite_radius: float | None = None) -> None:
        """Blit a cached glow into an ellipse; sizes that change every frame pass a fixed sprite_radius."""
        if rx <= 0.5 or ry <= 0.5 or opacity <= 0.004:
            return
        sprite = self._glow_sprite177(sprite_radius or max(rx, ry), color, stops)
        previous = painter.opacity()
        smooth = painter.testRenderHint(QPainter.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setOpacity(previous * max(0.0, min(1.0, opacity)))
        painter.drawImage(QRectF(center.x() - rx, center.y() - ry, rx * 2.0, ry * 2.0), sprite)
        painter.setOpacity(previous)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, smooth)

    def _state_accent177(self) -> tuple[float, float]:
        """(sideways shake, upward hop) in robot units for the first moments of a state."""
        since = max(0.0, self._elapsed - self._state_changed_at)
        shake = hop = 0.0
        if self._state == "error" and since < 1.0:
            shake = math.sin(since * 31.0) * 9.0 * math.exp(-since * 4.2)
        elif self._state in {"success", "happy", "excited", "playful"} and since < 0.62:
            hop = math.sin(since / 0.62 * math.pi) * 11.0
        elif self._state == "alert" and since < 0.5:
            hop = math.sin(since / 0.5 * math.pi) * 5.0
        return shake, hop

    def _breath177(self) -> float:
        """Torso expansion (fraction): slow in sleep, calm at rest, quicker while busy."""
        rate = {"sleeping": 1.05, "idle": 1.55, "listening": 1.9}.get(self._state, 2.3)
        depth = {"sleeping": 0.022, "idle": 0.014}.get(self._state, 0.009)
        return math.sin(self._elapsed * rate) * depth

    def _draw_ground_shadow177(self, painter: QPainter, x: float, base_y: float, scale: float, lift: float) -> None:
        ground = base_y + 136.0 * scale
        spread = max(0.55, 1.0 - max(-6.0, lift) * 0.022)
        strength = 0.55 if self._transparent_background or self._background_opacity_percent <= 0 else 1.0
        width, height = 118.0 * scale * spread, 13.0 * scale * spread
        self._draw_glow177(painter, QPointF(x, ground), width, height, QColor(0, 0, 0, 95), strength * spread,
                           sprite_radius=64.0)

    # rise, hold, lower (seconds) of one sip / one drag
    # NEO 185: the hand takes the cigarette out slower (2.1 s), holds it while the robot
    # exhales and flicks the ash, and puts it back unhurried (1.8 s).
    _ACC_TIMING177 = {"coffee": (0.95, 1.6, 1.0), "cigarette": (2.1, 3.8, 1.8)}
    _CALM_STATES177 = frozenset({"idle", "thinking", "working", "reading", "browsing", "searching", "coding",
                                 "looking", "curious", "scanning", "happy"})

    def _accessory_lift177(self, kind: str) -> float:
        lowering = self._lowering184.get(kind)
        if lowering is not None:
            started, progress = lowering
            phase = progress + (self._elapsed - started) / self._ACC_TIMING177[kind][2]
            return 1.0 - self._smoothstep(max(0.0, min(1.0, phase)))
        row = getattr(self, "_acc177", {}).get(kind)
        if not row or row["start"] < 0.0:
            return 0.0
        rise, hold, lower = self._ACC_TIMING177[kind]
        t = self._elapsed - row["start"]
        if t < 0.0:
            return 0.0
        if t < rise:
            return self._smootherstep185(t / rise) if kind == "cigarette" else self._smoothstep(t / rise)
        if kind == "cigarette" and self._combo_phase184 in {"smoke", "sip"}:
            return 1.0  # Keep it out of the mouth until the cup is lowered.
        if t < rise + hold:
            return 1.0
        if t < rise + hold + lower:
            return 1.0 - self._smoothstep((t - rise - hold) / lower)
        return 0.0

    def _begin_exhale184(self, style: str) -> None:
        self._exhaled177 = True
        self._exhale_style181 = style
        self._exhale_started181 = self._elapsed
        self._exhale_ring_count184 = 0
        self._exhale_until177 = self._elapsed + {"rings": 2.6, "cloud": 2.4, "stream": 2.8}[style]
        self._stream_clock185 = 0.0
        self._flick_done185 = False
        self._flick_ash_pending185 = False

    def _lower_accessory184(self, kind: str) -> None:
        """Enter the lowering curve without snapping the current hand position."""
        rise, hold, lower = self._ACC_TIMING177[kind]
        current = self._accessory_lift177(kind)
        lo, hi = 0.0, 1.0
        for _ in range(14):
            mid = (lo + hi) / 2.0
            if 1.0 - self._smoothstep(mid) > current:
                lo = mid
            else:
                hi = mid
        # Store lowering independently: backdating a start during the first
        # seconds of app uptime would collide with the negative idle sentinel.
        self._lowering184[kind] = (self._elapsed, lo)

    def _advance_combined_accessories184(self, calm: bool) -> bool:
        """Remove cigarette, exhale, sip, lower cup, then return cigarette."""
        if not (self._accessory_coffee and self._accessory_cigarette):
            return False
        coffee, cigarette = self._acc177["coffee"], self._acc177["cigarette"]
        rise, hold, lower = self._ACC_TIMING177["cigarette"]
        if not self._combo_phase184:
            if not calm or self._elapsed < coffee["next"] or coffee["start"] >= 0.0:
                return False
            self._lowering184.clear()
            self._combo_phase184 = "smoke"
            if cigarette["start"] < 0.0 or self._elapsed - cigarette["start"] >= rise + hold:
                cigarette["start"] = self._elapsed
                self._exhaled177 = False
        if not calm:
            self._exhale_until177 = 0.0
            if self._combo_phase184 == "smoke":
                self._lower_accessory184("cigarette")
                self._combo_phase184 = "return"
            elif self._combo_phase184 == "sip":
                cr, ch, _cl = self._ACC_TIMING177["coffee"]
                if "coffee" not in self._lowering184 and self._elapsed - coffee["start"] < cr + ch:
                    self._lower_accessory184("coffee")
        phase = self._combo_phase184
        if phase == "smoke":
            if not self._exhaled177 and self._elapsed - cigarette["start"] >= rise + 0.15:
                self._begin_exhale184("rings")
            if self._exhaled177 and self._elapsed >= self._exhale_until177 + 0.20:
                coffee["start"] = self._elapsed
                self._combo_phase184 = "sip"
        elif phase == "sip":
            coffee_done = (self._accessory_lift177("coffee") <= 0.0 if "coffee" in self._lowering184
                           else self._elapsed - coffee["start"] >= sum(self._ACC_TIMING177["coffee"]))
            if coffee_done:
                coffee["start"] = -1.0
                self._lowering184.pop("coffee", None)
                coffee["next"] = self._elapsed + random.uniform(11.0, 19.0)
                self._lower_accessory184("cigarette")
                self._combo_phase184 = "return"
        elif phase == "return":
            if self._accessory_lift177("cigarette") <= 0.0:
                cigarette["start"] = -1.0
                self._lowering184.clear()
                cigarette["next"] = self._elapsed + random.uniform(7.0, 12.0)
                # An interrupted preparation should not immediately retry.
                coffee["next"] = max(coffee["next"], self._elapsed + random.uniform(11.0, 19.0))
                self._wisp_next181 = self._elapsed + random.uniform(4.5, 8.5)
                self._combo_phase184 = ""
        return True

    _FLICK_SECONDS185 = 0.62

    def _accessory_motion185(self) -> bool:
        acc = getattr(self, "_acc177", {})
        return bool(any(row.get("start", -1.0) >= 0.0 for row in acc.values())
                    or getattr(self, "_lowering184", None) or getattr(self, "_combo_phase184", "")
                    or self._elapsed < getattr(self, "_exhale_until177", 0.0) + 0.8
                    or self._elapsed - getattr(self, "_flick_started185", -10.0) < 0.8)

    def _maybe_flick185(self) -> None:
        """Tap the ash off once the cigarette is held aside and the exhale has begun."""
        row = self._acc177["cigarette"]
        if not self._accessory_cigarette or row["start"] < 0.0 or self._flick_done185 or self._lowering184.get("cigarette"):
            return
        rise = self._ACC_TIMING177["cigarette"][0]
        if self._elapsed - row["start"] >= rise + 1.45 and self._accessory_lift177("cigarette") >= 0.999:
            self._flick_done185 = True
            self._flick_started185 = self._elapsed
            self._flick_ash_pending185 = True

    def _flick_shape185(self) -> float:
        """Two quick taps that die away: 0 at rest, ±1 at the strongest tap."""
        since = self._elapsed - self._flick_started185
        if since < 0.0 or since >= self._FLICK_SECONDS185:
            return 0.0
        f = since / self._FLICK_SECONDS185
        return math.sin(f * math.tau * 2.0) * (1.0 - f) ** 1.4

    def _advance_accessories177(self, dt: float) -> None:
        calm = self._state in self._CALM_STATES177
        enabled = {"coffee": bool(self._accessory_coffee), "cigarette": bool(self._accessory_cigarette)}
        if calm:
            self._maybe_flick185()
        motion = bool((enabled["coffee"] or enabled["cigarette"]) and self._accessory_motion185())
        if motion != getattr(self, "_acc_motion185", None):
            self._acc_motion185 = motion
            self._sync_timer_interval()
        if self._advance_combined_accessories184(calm):
            self._emit_particles177(dt, enabled)
            self._update_particles177(dt)
            return
        for kind in ("coffee", "cigarette"):
            row = self._acc177[kind]
            rise, hold, lower = self._ACC_TIMING177[kind]
            if not enabled[kind]:
                row["start"] = -1.0
                continue
            if row["start"] >= 0.0:
                t = self._elapsed - row["start"]
                if not calm and t < rise + hold:
                    # Busy now (speaking, listening): lower it smoothly from where it is.
                    current = self._accessory_lift177(kind)
                    lo, hi = 0.0, 1.0
                    for _ in range(14):
                        mid = (lo + hi) / 2.0
                        if 1.0 - self._smoothstep(mid) > current:
                            lo = mid
                        else:
                            hi = mid
                    row["start"] = self._elapsed - (rise + hold + lower * lo)
                elif t >= rise + hold + lower:
                    row["start"] = -1.0
                    row["next"] = self._elapsed + (random.uniform(11.0, 19.0) if kind == "coffee" else random.uniform(7.0, 12.0))
                if kind == "cigarette" and not self._exhaled177 and t >= rise + 0.15:
                    # NEO 181: the robot breathes the smoke out while the hand holds the cigarette aside.
                    self._begin_exhale184(random.choice(("rings", "cloud", "stream", "rings")))
            else:
                other = self._acc177["cigarette" if kind == "coffee" else "coffee"]
                if calm and self._elapsed >= row["next"] and other["start"] < 0.0:
                    row["start"] = self._elapsed
                    if kind == "cigarette":
                        self._exhaled177 = False
                elif (kind == "cigarette" and self._state != "sleeping" and self._elapsed >= self._wisp_next181
                      and self._elapsed >= self._exhale_until177 and other["start"] < 0.0):
                    # between breaks a thin wisp leaves the corner of the mouth now and then
                    self._exhale_style181 = "wisp"
                    self._exhale_started181 = self._elapsed
                    self._exhale_until177 = self._elapsed + 0.7
                    self._wisp_next181 = self._elapsed + random.uniform(4.5, 8.5)
        # Ash grows while the cigarette burns and drops off now and then.
        # NEO 181: the tech cigarette has a heating tip, not ash.
        self._emit_particles177(dt, enabled)
        self._update_particles177(dt)

    def _emit_particles177(self, dt: float, enabled: dict[str, bool]) -> None:
        particles = self._particles177
        if (enabled["cigarette"] and self._flick_ash_pending185 and self._ember177 is not None
                and self._elapsed - self._flick_started185 >= self._FLICK_SECONDS185 * 0.13):
            self._flick_ash_pending185 = False
            x, y, scale = self._ember177
            for _ in range(random.randint(4, 6)):
                particles.append([x + random.uniform(-1.2, 1.2) * scale, y + random.uniform(0.0, 1.5) * scale,
                                  random.uniform(-7.0, 4.0) * scale, random.uniform(6.0, 16.0) * scale,
                                  0.0, random.uniform(0.8, 1.25), random.uniform(0.9, 1.5), random.uniform(0.7, 1.2),
                                  random.uniform(190.0, 235.0), random.uniform(0.0, math.tau), 2.0, scale])
        if enabled["cigarette"] and self._ember177 is not None:
            x, y, scale = self._ember177
            inhale = getattr(self, "_inhale181", 0.0) > 0.5
            self._smoke_clock177 += dt
            # Sparse, short-lived wisps from the ember; the exhale is separate.
            # NEO 185: a little denser and brighter, rising slower and living longer.
            period = 0.40 if inhale else 0.30
            while self._smoke_clock177 >= period:
                self._smoke_clock177 -= period
                particles.append([x + random.uniform(-0.8, 0.8) * scale, y - 1.5 * scale,
                                  random.uniform(-1.4, 1.4) * scale, random.uniform(-10.5, -6.5) * scale,
                                  0.0, random.uniform(2.6, 3.4), 1.4, random.uniform(7.5, 9.0),
                                  random.uniform(56.0, 70.0), random.uniform(0.0, math.tau), 1.0, scale])
        if self._elapsed < self._exhale_until177 and self._mouth177 is not None:
            # NEO 181: several ways to breathe the smoke out.
            x, y, scale = self._mouth177
            style = getattr(self, "_exhale_style181", "cloud")
            since = self._elapsed - getattr(self, "_exhale_started181", 0.0)
            if style == "rings":
                # two or three smoke rings, one after another, drifting up and growing
                count = int(since / 0.72) + 1
                if count <= 3 and self._exhale_ring_count184 < count and since < 2.2:
                    self._exhale_ring_count184 += 1
                    particles.append([x, y - 1.0 * scale, random.uniform(-5.5, -2.5) * scale, random.uniform(-9.5, -6.5) * scale,
                                      0.0, random.uniform(4.2, 5.0), 4.0, random.uniform(20.0, 25.0),
                                      random.uniform(195.0, 225.0), random.uniform(0.0, math.tau), 3.0, scale])
            elif style == "stream":
                # NEO 185: a slow, smooth jet up and to the side. Puffs leave at an even
                # rate (no random gaps) and the jet eases in and out with the breath.
                breath = self._smoothstep(since / 0.5) * (1.0 - self._smoothstep((since - 2.0) / 0.8))
                self._stream_clock185 = getattr(self, "_stream_clock185", 0.0) + dt
                while self._stream_clock185 >= 1.0 / 20.0:
                    self._stream_clock185 -= 1.0 / 20.0
                    if breath <= 0.02:
                        continue
                    speed = (13.0 + 5.0 * breath) * scale
                    particles.append([x + random.uniform(-0.6, 0.6) * scale, y,
                                      -speed * 0.78, -speed * 0.40, 0.0, random.uniform(2.6, 3.2), 1.7,
                                      random.uniform(10.0, 13.0), (80.0 + 50.0 * breath), random.uniform(0.0, math.tau), 1.0, scale])
            else:
                # «cloud»: a soft layer spreading in front of the face; «wisp»: a small one from the corner
                wisp = style == "wisp"
                rate = 9.0 if wisp else 20.0
                if random.random() < min(1.0, dt * rate):
                    direction = random.uniform(-0.9, 0.3)
                    speed = random.uniform(10.0, 20.0) * scale * (0.6 if wisp else 1.0)
                    particles.append([x + random.uniform(-2.0, 2.0) * scale, y,
                                      math.sin(direction) * speed - 9.0 * scale, -math.cos(direction) * speed * 0.4,
                                      0.0, random.uniform(2.4, 3.3), 2.6 if wisp else 3.4,
                                      random.uniform(8.0, 11.0) if wisp else random.uniform(15.0, 22.0),
                                      random.uniform(62.0, 80.0) if wisp else random.uniform(98.0, 122.0),
                                      random.uniform(0.0, math.tau), 1.0, scale])
        if enabled["coffee"] and self._steam177 is not None and self._accessory_lift177("coffee") < 0.25:
            x, y, scale = self._steam177
            self._steam_clock177 += dt
            while self._steam_clock177 >= 0.2:
                self._steam_clock177 -= 0.2
                particles.append([x + random.uniform(-6.0, 6.0) * scale, y,
                                  random.uniform(-1.5, 1.5) * scale, random.uniform(-17.0, -12.0) * scale,
                                  0.0, random.uniform(1.5, 2.1), 2.0, random.uniform(6.5, 8.5),
                                  random.uniform(34.0, 48.0), random.uniform(0.0, math.tau), 0.0, scale])
        if len(particles) > 150:
            del particles[:len(particles) - 150]

    def _update_particles177(self, dt: float) -> None:
        alive = []
        for p in self._particles177:
            p[4] += dt
            if p[4] >= p[5]:
                continue
            if p[10] == 3.0:                      # smoke ring: rises, slows, wobbles a little
                p[3] *= max(0.0, 1.0 - 0.45 * dt)
                p[2] *= max(0.0, 1.0 - 0.6 * dt)
            elif p[10] == 2.0:                      # ash: falls, flutters a little
                p[3] += 150.0 * p[11] * dt
                p[2] *= max(0.0, 1.0 - 1.4 * dt)
                p[2] += math.sin(p[4] * 9.0 + p[9]) * 10.0 * p[11] * dt
            else:                                 # smoke / steam: rises, slows, curls
                p[3] *= max(0.0, 1.0 - 0.36 * dt)
                p[2] *= max(0.0, 1.0 - 0.22 * dt)
                p[2] += math.sin(p[4] * 1.8 + p[9]) * 7.5 * p[11] * dt
            p[0] += p[2] * dt
            p[1] += p[3] * dt
            alive.append(p)
        self._particles177 = alive

    def _draw_particles177(self, painter: QPainter) -> None:
        if not self._particles177:
            return
        painter.save()
        painter.setPen(Qt.NoPen)
        smoke = QColor("#C9D3DE")
        steam = QColor(self._colors["shell"]).lighter(112)
        ash = QColor("#8E9196")
        # one 64 px sprite per colour, scaled to each puff (smooth scaling for soft edges)
        sprite_colors = {1.0: QColor(smoke.red(), smoke.green(), smoke.blue(), 255),
                         0.0: QColor(steam.red(), steam.green(), steam.blue(), 255)}
        for x, y, _vx, _vy, age, life, r0, r1, alpha, _wobble, kind, scale in self._particles177:
            u = age / life
            if kind == 3.0:
                grow = 1.0 - (1.0 - u) ** 2
                radius = (r0 + (r1 - r0) * grow) * scale
                a = int(alpha * self._smoothstep(age / 0.2) * (1.0 - u) ** 1.3)
                if a > 2:
                    ring = QColor(smoke); ring.setAlpha(a // 3)
                    painter.setBrush(Qt.NoBrush)
                    painter.setPen(QPen(ring, max(1.2, radius * 0.55)))
                    painter.drawEllipse(QPointF(x, y), radius, radius * 0.78)
                    ring.setAlpha(a)
                    painter.setPen(QPen(ring, max(0.8, radius * 0.2)))
                    painter.drawEllipse(QPointF(x, y), radius, radius * 0.78)
                    painter.setPen(Qt.NoPen)
                continue
            if kind == 2.0:
                painter.setBrush(QColor(ash.red(), ash.green(), ash.blue(), int(alpha * (1.0 - u))))
                painter.drawEllipse(QPointF(x, y), r0 * scale, r1 * scale)
                continue
            grow = 1.0 - (1.0 - u) ** 2
            radius = (r0 + (r1 - r0) * grow) * scale
            fade = self._smoothstep(age / 0.22) * (1.0 - u) ** 1.5
            a = int(alpha * fade)
            if a <= 1 or radius <= 0.3:
                continue
            base = smoke if kind == 1.0 else steam
            self._draw_glow177(painter, QPointF(x, y), radius, radius, sprite_colors[kind], a / 255.0,
                               ((0.0, 1.0), (0.55, 0.45), (1.0, 0.0)), sprite_radius=32.0)
        painter.restore()

    def _head_point177(self, dx: float, dy: float, scale: float) -> QPointF:
        """A point on the (tilted) head in widget coordinates."""
        head_x, head_y, tilt = getattr(self, "_head_frame177", (0.0, 0.0, 0.0))
        a = math.radians(tilt)
        x, y = dx * scale, dy * scale
        return QPointF(head_x + x * math.cos(a) - y * math.sin(a), head_y + x * math.sin(a) + y * math.cos(a))

    def _draw_mag_hand181(self, painter: QPainter, center: QPointF, angle: float, scale: float, *,
                          opacity: float = 1.0, grip: float = 1.0, tether: QPointF | None = None, size: float = 1.5,
                          gap: float = 5.0) -> None:
        """NEO 183: a floating magnetic hand — nothing grips, the field holds.

        The palm is a small capsule like the robot's ears (dark glass, light rim, a cyan LED).
        Its front face (``x = 0`` in the hand's frame, pointing along ``angle``) is a glowing
        magnet plate; the held object floats ``gap`` px in front of it inside soft field
        rings. A wrist orb hovers behind the palm, joined to it only by the field.
        """
        if opacity <= 0.01:
            return
        u = scale * size
        g = gap * scale
        glow = QColor(self._colors["glow"])
        shell = QColor(self._colors["shell"])
        trim = QColor(self._colors["trim"])
        dark = QColor(self._colors["body"])
        t = self._elapsed
        grip = max(0.0, min(1.0, grip))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setOpacity(painter.opacity() * max(0.0, min(1.0, opacity)))
        if tether is not None:
            line = QColor(glow); line.setAlpha(55)
            pen = QPen(line, max(0.8, 1.1 * u), Qt.CustomDashLine, Qt.RoundCap)
            pen.setDashPattern([0.8, 3.6])
            pen.setDashOffset(-t * 6.0)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            bend = QPointF((tether.x() + center.x()) / 2, max(tether.y(), center.y()) + 10 * u)
            path = QPainterPath(tether)
            path.quadTo(bend, center)
            painter.drawPath(path)
        # NEO 187 HREQ-006: no extra glow pool under the floating hand; the
        # magnet field in front of the palm stays.
        painter.translate(center)
        painter.rotate(angle)
        bob = 0.4 * u * math.sin(t * 2.2)

        # the field: a soft light in the gap and rings that travel from the plate to the object
        self._draw_glow177(painter, QPointF(g * 0.55, bob), g * 0.9 + 2.0 * u, 5.5 * u, QColor(glow),
                           (0.22 + 0.10 * math.sin(t * 3.1)) * grip, sprite_radius=24.0)
        for k in range(3):
            phase = (t * 1.1 + k / 3.0) % 1.0
            ring = QColor(glow); ring.setAlpha(int(150 * math.sin(math.pi * phase) * grip))
            painter.setPen(QPen(ring, max(0.6, 0.75 * u), Qt.SolidLine, Qt.RoundCap))
            painter.setBrush(Qt.NoBrush)
            x = g * phase
            ry = (5.6 - 1.6 * phase) * u
            painter.drawArc(QRectF(x - 1.1 * u, bob - ry, 2.2 * u, 2 * ry), -90 * 16, 180 * 16)

        # wrist orb behind the palm, joined only by the field
        orb = QPointF(-13.0 * u, bob * 0.6)
        link = QColor(glow)
        for i in range(3):
            pulse = 0.5 + 0.5 * math.sin(t * 5.0 - i * 1.3)
            link.setAlpha(int(70 + 110 * pulse))
            painter.setPen(Qt.NoPen)
            painter.setBrush(link)
            painter.drawEllipse(QPointF(-10.4 * u + i * 0.95 * u, bob * 0.8), 0.38 * u, 0.38 * u)
        orb_fill = QRadialGradient(QPointF(orb.x() - 0.8 * u, orb.y() - 0.9 * u), 3.2 * u)
        orb_fill.setColorAt(0.0, shell.lighter(125)); orb_fill.setColorAt(1.0, shell.darker(135))
        painter.setBrush(orb_fill)
        painter.setPen(QPen(trim, max(0.7, 0.9 * u)))
        painter.drawEllipse(orb, 2.5 * u, 2.5 * u)
        core = QColor(glow); core.setAlpha(220)
        painter.setPen(Qt.NoPen); painter.setBrush(core)
        painter.drawEllipse(orb, 0.85 * u, 0.85 * u)

        painter.translate(0, bob)
        # palm capsule (like the ears) with a light rim
        palm = QRectF(-8.6 * u, -6.4 * u, 8.6 * u, 12.8 * u)
        palm_fill = QLinearGradient(palm.topLeft(), palm.topRight())
        palm_fill.setColorAt(0.0, dark.lighter(140)); palm_fill.setColorAt(1.0, dark.darker(130))
        painter.setBrush(palm_fill)
        painter.setPen(QPen(trim, max(0.9, 1.2 * u)))
        painter.drawRoundedRect(palm, 4.3 * u, 4.3 * u)
        rim = QColor(shell); rim.setAlpha(150)
        painter.setPen(QPen(rim, max(0.5, 0.75 * u), Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(palm.adjusted(0.9 * u, 0.9 * u, -0.9 * u, -0.9 * u), 80 * 16, 100 * 16)
        # the magnet plate: a light shell cap on the front with a glowing face
        plate = QRectF(-2.2 * u, -5.6 * u, 2.6 * u, 11.2 * u)
        cap = QLinearGradient(plate.topLeft(), plate.bottomLeft())
        cap.setColorAt(0.0, shell.lighter(120)); cap.setColorAt(1.0, shell.darker(130))
        painter.setBrush(cap)
        painter.setPen(QPen(trim, max(0.7, 0.9 * u)))
        painter.drawRoundedRect(plate, 1.3 * u, 1.3 * u)
        face = QColor(glow).lighter(118); face.setAlpha(int(150 + 90 * grip))
        painter.setPen(QPen(face, max(0.9, 1.2 * u), Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(0.4 * u, -4.2 * u), QPointF(0.4 * u, 4.2 * u))
        led = QColor(glow); led.setAlpha(int(190 + 55 * math.sin(t * 3.4)))
        self._draw_glow177(painter, QPointF(-5.4 * u, 0), 4.0 * u, 5.2 * u, QColor(glow), 0.32, sprite_radius=24.0)
        painter.setPen(Qt.NoPen); painter.setBrush(led)
        painter.drawRoundedRect(QRectF(-6.0 * u, -2.6 * u, 1.3 * u, 5.2 * u), 0.65 * u, 0.65 * u)
        painter.restore()

    def _draw_persistent_accessories(
        self, painter: QPainter, head_x: float, head_y: float, body_x: float, body_y: float, scale: float, arm_offset: float
    ) -> None:
        coffee = bool(self._accessory_coffee)
        cigarette = bool(self._accessory_cigarette)
        self._mouth177 = None
        if not coffee and not cigarette:
            self._ember177 = self._steam177 = None
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        mouth = self._head_point177(0.0, 25.0, scale)
        smoke_mouth = self._head_point177(-7.0, 25.0, scale)       # the free side of the mouth
        self._mouth177 = (smoke_mouth.x(), smoke_mouth.y(), scale)

        if coffee:
            # Tired under-eye shadows: the coffee robot is a little sleepy.
            bruise_fill = QColor("#59647E"); bruise_fill.setAlpha(58)
            painter.setPen(Qt.NoPen)
            painter.setBrush(bruise_fill)
            for side in (-38.5, 38.5):
                center = self._head_point177(side + self._gaze_x * 0.5, -7.0 + self._gaze_y + 13.0, scale)
                painter.drawEllipse(center, 14.0 * scale, 4.2 * scale)
            self._draw_coffee177(painter, body_x, body_y, scale, mouth)
        else:
            self._steam177 = None
        if cigarette:
            self._draw_cigarette177(painter, body_x, body_y, scale, mouth)
        else:
            self._ember177 = None
        painter.restore()

    def _draw_coffee177(self, painter: QPainter, body_x: float, body_y: float, scale: float, mouth: QPointF) -> None:
        """NEO 181: a mug in the robot's style, floating in a magnetic hand beside the torso."""
        lift = self._accessory_lift177("coffee")
        ease = self._smoothstep(lift)
        bob = math.sin(self._elapsed * 1.3) * 1.6 * scale
        rest = QPointF(body_x + 122.0 * scale, body_y + 74.0 * scale + bob)
        sip = QPointF(mouth.x() + 28.0 * scale, mouth.y() + 10.0 * scale)
        center = QPointF(rest.x() + (sip.x() - rest.x()) * ease, rest.y() + (sip.y() - rest.y()) * ease)
        angle = -30.0 * self._smoothstep(max(0.0, (lift - 0.35) / 0.65))
        w, h = 30.0 * scale, 35.0 * scale
        a = math.radians(angle)

        def local(px: float, py: float) -> QPointF:
            return QPointF(center.x() + px * math.cos(a) - py * math.sin(a), center.y() + px * math.sin(a) + py * math.cos(a))

        hand = local(-w / 2 - 6.0 * scale, 2.0 * scale)
        self._draw_mag_hand181(painter, hand, angle, scale, tether=QPointF(body_x + 84.0 * scale, body_y + 88.0 * scale), gap=6.0)

        shell = QColor(self._accessory_mug_color); shell.setAlpha(255)
        trim = QColor(self._colors["trim"])
        glow = QColor(self._colors["glow"])
        painter.save()
        painter.translate(center)
        painter.rotate(angle)
        body = QRectF(-w / 2, -h / 2, w, h)
        shade = QLinearGradient(QPointF(-w / 2, 0), QPointF(w / 2, 0))
        shade.setColorAt(0.0, shell.darker(135)); shade.setColorAt(0.3, shell.lighter(112))
        shade.setColorAt(0.65, shell); shade.setColorAt(1.0, shell.darker(150))
        painter.setPen(QPen(trim, max(0.9, 1.4 * scale)))
        painter.setBrush(shade)
        painter.drawRoundedRect(body, 7.0 * scale, 7.0 * scale)
        # dark base plate, like the robot's torso
        base = QRectF(-w / 2 + 1.0 * scale, h / 2 - 7.0 * scale, w - 2.0 * scale, 6.0 * scale)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self._colors["body"]).darker(110))
        painter.drawRoundedRect(base, 3.0 * scale, 3.0 * scale)
        # glowing band with a light running around it
        band = QColor(glow); band.setAlpha(200)
        painter.setPen(QPen(band, max(1.0, 2.0 * scale), Qt.SolidLine, Qt.FlatCap))
        painter.drawLine(QPointF(-w / 2 + 1.0 * scale, 2.0 * scale), QPointF(w / 2 - 1.0 * scale, 2.0 * scale))
        run = (self._elapsed * 0.6) % 1.0
        self._draw_glow177(painter, QPointF(-w / 2 + w * run, 2.0 * scale), 5.0 * scale, 3.0 * scale, glow, 0.8, sprite_radius=16.0)
        # rim and coffee
        rim = QRectF(-w / 2 + 1.0 * scale, -h / 2 - 3.0 * scale, w - 2.0 * scale, 6.5 * scale)
        painter.setPen(QPen(trim, max(0.8, 1.2 * scale)))
        painter.setBrush(shell.lighter(108))
        painter.drawEllipse(rim)
        coffee = QRadialGradient(QPointF(-3.0 * scale, -h / 2), w * 0.55)
        coffee.setColorAt(0.0, QColor("#6E4426")); coffee.setColorAt(1.0, QColor("#2A170B"))
        painter.setPen(Qt.NoPen)
        painter.setBrush(coffee)
        painter.drawEllipse(rim.adjusted(2.4 * scale, 1.2 * scale, -2.4 * scale, -1.2 * scale))
        painter.setPen(QPen(QColor(255, 255, 255, 110), max(0.7, 1.2 * scale), Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(-w / 2 + 5.0 * scale, -h / 2 + 5.0 * scale), QPointF(-w / 2 + 5.0 * scale, h / 2 - 10.0 * scale))
        painter.restore()
        steam = local(0.0, -h / 2 - 3.0 * scale)
        self._steam177 = (steam.x(), steam.y(), scale)

    def _draw_tech_cigarette181(self, painter: QPainter, scale: float, glow_level: float) -> QPointF:
        """The stick in its own frame (x along it, mouthpiece at 0). Returns the tip point."""
        u = scale
        length, thick = 34.0 * u, 4.4 * u        # NEO 183: a little smaller
        glow = QColor(self._colors["glow"])
        trim = QColor(self._colors["trim"])
        shell = QColor(self._colors["shell"])
        # mouthpiece in the dark body colour
        piece = QLinearGradient(QPointF(0, -thick / 2), QPointF(0, thick / 2))
        piece.setColorAt(0.0, QColor(self._colors["body"]).lighter(140)); piece.setColorAt(1.0, QColor(self._colors["body"]).darker(130))
        painter.setPen(QPen(trim, max(0.6, 0.9 * u)))
        painter.setBrush(piece)
        painter.drawRoundedRect(QRectF(-2.0 * u, -thick / 2 + 0.3 * u, 10.0 * u, thick - 0.6 * u), 1.9 * u, 1.9 * u)
        # body in the shell colours
        body = QLinearGradient(QPointF(0, -thick / 2), QPointF(0, thick / 2))
        body.setColorAt(0.0, shell.lighter(118)); body.setColorAt(0.5, shell); body.setColorAt(1.0, shell.darker(145))
        painter.setBrush(body)
        painter.drawRoundedRect(QRectF(8.5 * u, -thick / 2, length - 13.5 * u, thick), 1.3 * u, 1.3 * u)
        ring = QColor(glow); ring.setAlpha(220)
        painter.setPen(QPen(ring, max(0.8, 1.3 * u), Qt.SolidLine, Qt.FlatCap))
        painter.drawLine(QPointF(9.0 * u, -thick / 2 + 0.5 * u), QPointF(9.0 * u, thick / 2 - 0.5 * u))
        # LED window that brightens on a drag
        led = QColor(glow); led.setAlpha(int(110 + 140 * glow_level))
        painter.setPen(Qt.NoPen); painter.setBrush(led)
        painter.drawRoundedRect(QRectF(13.0 * u, -0.7 * u, 10.0 * u, 1.4 * u), 0.7 * u, 0.7 * u)
        # metal cap and the heating tip
        cap = QLinearGradient(QPointF(0, -thick / 2), QPointF(0, thick / 2))
        cap.setColorAt(0.0, trim.lighter(150)); cap.setColorAt(1.0, trim.darker(130))
        painter.setPen(QPen(trim.darker(120), max(0.6, 0.8 * u)))
        painter.setBrush(cap)
        painter.drawRoundedRect(QRectF(length - 5.0 * u, -thick / 2 - 0.25 * u, 5.0 * u, thick + 0.5 * u), 1.4 * u, 1.4 * u)
        tip = QPointF(length, 0.0)
        flicker = 0.85 + 0.15 * math.sin(self._elapsed * 11.0) * math.sin(self._elapsed * 6.3 + 1.0)
        level = min(1.0, 0.45 * flicker + 0.65 * glow_level)
        halo = QColor(glow).lighter(115)
        self._draw_glow177(painter, tip, (5.0 + 6.0 * level) * u, (5.0 + 6.0 * level) * u, halo, 0.5 + 0.5 * level, sprite_radius=24.0)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, int(160 + 90 * level)))
        painter.drawEllipse(tip, 1.3 * u, 1.3 * u)
        return tip

    def _draw_cigarette177(self, painter: QPainter, body_x: float, body_y: float, scale: float, mouth: QPointF) -> None:
        """NEO 181: a tech cigarette in the corner of the mouth.

        Now and then (awake, calm states) a magnetic hand flies in, the tip glows on the drag,
        the hand takes the cigarette out and holds it aside while the robot breathes the smoke
        out (rings / a cloud / a jet), then puts it back. Asleep it simply stays in the mouth.
        """
        del body_x, body_y, mouth
        lift = self._accessory_lift177("cigarette")
        approach = self._smoothstep(min(1.0, lift / 0.42))
        out = self._smoothstep(max(0.0, (lift - 0.42) / 0.58))
        self._cig_out181 = out
        inhale = approach * (1.0 - out)
        self._inhale181 = inhale
        head_x, head_y, tilt = getattr(self, "_head_frame177", (0.0, 0.0, 0.0))
        talking = 1.0 if self._state == "speaking" else 0.0
        bob = math.sin(self._elapsed * 7.9) * 1.2 * talking + math.sin(self._elapsed * 0.9) * 1.2
        u = scale
        mouth_base, mouth_angle = QPointF(7.0 * u, 26.0 * u), 17.0 + bob - 6.0 * inhale
        # NEO 183: taken out to the side opposite the mug (the mug floats on the right)
        held_base, held_angle = QPointF(-104.0 * u, 80.0 * u), -150.0 + math.sin(self._elapsed * 1.4) * 2.0
        flick = self._flick_shape185() * out
        if flick:
            # NEO 185: the wrist flicks down twice; the stick pivots at the grip.
            held_base = QPointF(held_base.x(), held_base.y() + max(0.0, flick) * 3.2 * u)
            held_angle += flick * 7.0
        base = QPointF(mouth_base.x() + (held_base.x() - mouth_base.x()) * out,
                       mouth_base.y() + (held_base.y() - mouth_base.y()) * out)
        angle = mouth_angle + (held_angle - mouth_angle) * out
        painter.save()
        painter.translate(head_x, head_y)
        painter.rotate(tilt)
        # the hand grips the stick in the middle, from below
        a = math.radians(angle)
        grip = QPointF(base.x() + math.cos(a) * 18.0 * u, base.y() + math.sin(a) * 18.0 * u)
        normal = QPointF(-math.sin(a), math.cos(a))
        if normal.y() < 0:                       # the hand always holds it from below
            normal = QPointF(-normal.x(), -normal.y())
        far = (1.0 - approach) * 30.0 * u
        reach = 2.2 * u + 7.0 * u + far
        hand = QPointF(grip.x() + normal.x() * reach, grip.y() + normal.y() * reach + far * 0.4)
        hand_angle = math.degrees(math.atan2(-normal.y(), -normal.x()))
        painter.save()
        painter.translate(base)
        painter.rotate(angle)
        tip = self._draw_tech_cigarette181(painter, u, inhale)
        ember = painter.worldTransform().map(tip)
        painter.restore()
        if approach > 0.01:
            self._draw_mag_hand181(painter, hand, hand_angle, u, opacity=approach, grip=approach, gap=7.0)
        free = QPointF(0.0 if out > 0.5 else -7.0 * u, 25.0 * u)
        mouth_point = painter.worldTransform().map(free)
        painter.restore()
        self._mouth177 = (mouth_point.x(), mouth_point.y(), scale)
        self._ember177 = (ember.x(), ember.y(), scale)
        # a thin ribbon of smoke curls up from the tip before it breaks into puffs
        ribbon = QPainterPath(ember)
        height = 18.0 * scale
        for step in range(1, 13):
            k = step / 12.0
            curl = math.sin(k * 5.2 - self._elapsed * 2.6) * (0.8 + 3.0 * k) * scale
            ribbon.lineTo(QPointF(ember.x() + curl + k * 4.0 * scale, ember.y() - k * height))
        fade = QLinearGradient(ember, QPointF(ember.x(), ember.y() - height))
        fade.setColorAt(0.0, QColor(214, 222, 232, 48))
        fade.setColorAt(1.0, QColor(214, 222, 232, 0))
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(fade, max(0.6, 0.9 * scale), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(ribbon)

    def _draw_expression(
        self, painter: QPainter, center_x: float, center_y: float, scale: float, eye_color: QColor
    ) -> None:
        face = self._face
        accessory_active = bool(self._accessory_coffee)
        no_smile = bool(self._accessory_coffee or self._accessory_cigarette)
        blinking = self._elapsed < self._blink_until and self._state not in {"listening", "speaking", "error", "sad", "sleeping"}
        # NEO 177: the eyelid follows a curve: closes in 70 ms, opens in 120 ms.
        since_blink = self._elapsed - getattr(self, "_blink_started177", -10.0)
        closure = 0.0
        if blinking:
            closure = self._smoothstep(since_blink / 0.07) if since_blink < 0.07 else 1.0 - self._smoothstep((since_blink - 0.07) / 0.12)
        open_h = float(face.get("eye_h", 22.0))
        eye_h = max(2.4, open_h + (2.6 - open_h) * closure) * scale
        left_w = float(face.get("left_w", 13.0)) * scale
        right_w = float(face.get("right_w", 13.0)) * scale
        saccade = getattr(self, "_saccade177", (0.0, 0.0))
        eye_y = center_y + (-7.0 + float(face.get("eye_y", 0.0)) + self._gaze_y + saccade[1]) * scale
        eye_shift = (self._gaze_x + saccade[0]) * scale

        if self._state == "sleeping":
            # Natural closed eyelids. With coffee equipped the robot
            # must never smile, so sleeping uses a calm flat mouth instead.
            painter.setBrush(Qt.NoBrush)
            sleepy = QColor(eye_color)
            sleepy.setAlpha(220)
            painter.setPen(QPen(sleepy, max(1.8, 3.2 * scale), Qt.SolidLine, Qt.RoundCap))
            for ex in (-39.0, 39.0):
                rect = QRectF(center_x + (ex - 15.0) * scale, eye_y - 7.0 * scale, 30.0 * scale, 14.0 * scale)
                painter.drawArc(rect, 200 * 16, 140 * 16)
            mouth_y = center_y + (24.0 + math.sin(self._phase * 0.42) * 0.8) * scale
            painter.setPen(QPen(sleepy, max(1.4, 2.4 * scale), Qt.SolidLine, Qt.RoundCap))
            if no_smile:
                painter.drawLine(QPointF(center_x - 8.0 * scale, mouth_y + 1.0 * scale), QPointF(center_x + 8.0 * scale, mouth_y + 1.0 * scale))
            else:
                painter.drawArc(QRectF(center_x - 9 * scale, mouth_y - 2 * scale, 18 * scale, 8 * scale), 195 * 16, 150 * 16)
            return

        painter.setPen(Qt.NoPen)
        eyes = (QRectF(center_x - 45 * scale + eye_shift, eye_y - eye_h / 2, left_w, eye_h),
                QRectF(center_x + 32 * scale + eye_shift, eye_y - eye_h / 2, right_w, eye_h))
        # NEO 177: light bleeds softly around the eyes, each eye is a lit capsule with a
        # brighter core and a small specular highlight.
        bloom_color = QColor(eye_color); bloom_color.setAlpha(62)
        for eye in eyes:
            bloom_radius = 25.0 * scale
            self._draw_glow177(painter, eye.center(), bloom_radius,
                               bloom_radius * (0.55 + 0.45 * min(1.0, eye_h / (22.0 * scale))), bloom_color)
        for eye in eyes:
            fill = QLinearGradient(eye.topLeft(), eye.bottomLeft())
            fill.setColorAt(0.0, QColor(eye_color).lighter(140))
            fill.setColorAt(0.5, eye_color)
            fill.setColorAt(1.0, QColor(eye_color).darker(118))
            painter.setBrush(fill)
            painter.drawRoundedRect(eye, min(7 * scale, eye_h / 2), min(7 * scale, eye_h / 2))
            if eye_h > 9.0 * scale:
                painter.setBrush(QColor(255, 255, 255, 175))
                painter.drawEllipse(QPointF(eye.left() + eye.width() * 0.34, eye.top() + min(5.5 * scale, eye_h * 0.24)),
                                    1.9 * scale, 2.4 * scale)

        # Brows morph smoothly into thoughtful/sad/angry expressions.
        brow_l = float(face.get("brow_left", 0.0))
        brow_r = float(face.get("brow_right", 0.0))
        if abs(brow_l) > 0.03 or abs(brow_r) > 0.03:
            painter.setPen(QPen(eye_color, max(1.4, 2.6 * scale), Qt.SolidLine, Qt.RoundCap))
            by = eye_y - 20 * scale
            painter.drawLine(
                QPointF(center_x - 51 * scale, by + brow_l * 6 * scale),
                QPointF(center_x - 29 * scale, by - brow_l * 6 * scale),
            )
            painter.drawLine(
                QPointF(center_x + 29 * scale, by - brow_r * 6 * scale),
                QPointF(center_x + 51 * scale, by + brow_r * 6 * scale),
            )

        mouth_open = float(face.get("mouth_open", 0.0))
        if accessory_active:
            mouth_open = 0.0
        if self._state == "speaking":
            # NEO 177: syllables, not a metronome: a fast syllable beat under a slower phrase envelope.
            syllable = abs(math.sin(self._elapsed * 7.9)) ** 0.8
            phrase = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(self._elapsed * 2.3 + 0.7))
            mouth_open *= 0.22 + 0.78 * syllable * phrase
        elif self._state == "typing":
            mouth_open *= 0.55 + 0.45 * (math.sin(self._phase * 2.4) + 1) / 2
        coffee_lift = self._accessory_lift177("coffee") if self._accessory_coffee else 0.0
        if coffee_lift > 0.30:
            # NEO 185: the lips follow the sip: they purse as the mug comes up, touch the rim
            # and move with each swallow, then relax back into a line as the mug goes down.
            k = self._smoothstep((coffee_lift - 0.30) / 0.55)
            row = self._acc177.get("coffee", {})
            since = self._elapsed - float(row.get("start", -1.0))
            rise, hold, _lower = self._ACC_TIMING177["coffee"]
            swallow = 0.0
            if coffee_lift >= 0.995 and rise <= since <= rise + hold:
                swallow = 0.5 - 0.5 * math.cos((since - rise) / hold * math.tau * 2.0)
            lips = QColor(eye_color)
            painter.setPen(QPen(lips, max(1.4, 2.6 * scale), Qt.SolidLine, Qt.RoundCap))
            cx = center_x + 3.5 * k * scale
            rx = (11.0 - 6.2 * k + 0.6 * swallow) * scale
            ry = (0.6 + 2.6 * k - 0.9 * swallow * k) * scale
            if ry <= 1.0 * scale:
                painter.setBrush(Qt.NoBrush)
                painter.drawLine(QPointF(cx - rx, center_y + 25 * scale), QPointF(cx + rx, center_y + 25 * scale))
            else:
                painter.setBrush(QColor(self._colors["screen"]).darker(150))
                painter.drawEllipse(QPointF(cx, center_y + 25 * scale), rx, ry)
            return
        exhaling = getattr(self, "_exhale_until177", 0.0) > self._elapsed
        # Compute from this frame, rather than the previous paint's hand pose.
        cigarette_lift = self._accessory_lift177("cigarette")
        in_mouth = self._smoothstep(max(0.0, (cigarette_lift - 0.42) / 0.58)) < 0.5
        if exhaling and self._accessory_cigarette and not self._accessory_lift177("coffee"):
            # NEO 181: «o» for rings, a wider one for a cloud, a small one for a jet or a wisp.
            style = getattr(self, "_exhale_style181", "cloud")
            rx, ry = {"rings": (4.6, 4.2), "cloud": (6.5, 4.4), "stream": (3.0, 2.8), "wisp": (3.2, 2.6)}.get(style, (4.4, 3.8))
            painter.setBrush(QColor(self._colors["screen"]).darker(140))
            painter.setPen(QPen(eye_color, max(1.4, 2.4 * scale)))
            painter.drawEllipse(QPointF(center_x - (7 if in_mouth else 0) * scale, center_y + 25 * scale), rx * scale, ry * scale)
            return
        if self._accessory_cigarette and in_mouth and mouth_open <= 0.12 and not self._accessory_lift177("coffee"):
            # Lips closed around the cigarette: a short line that ends at the corner, pinched at the cigarette.
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(eye_color, max(1.6, 3.0 * scale), Qt.SolidLine, Qt.RoundCap))
            smirk = QPainterPath(QPointF(center_x - 12 * scale, center_y + 25.0 * scale))
            smirk.quadTo(QPointF(center_x - 2 * scale, center_y + 24.6 * scale), QPointF(center_x + 6 * scale, center_y + 25.0 * scale))
            painter.drawPath(smirk)
            return
        if mouth_open > 0.12:
            inside = QColor(self._colors["screen"]).darker(150)
            painter.setBrush(inside)
            painter.setPen(QPen(eye_color, max(1.8, 3.1 * scale)))
            h = (5.0 + mouth_open * 12.0) * scale
            w = (26.0 - mouth_open * 5.0) * scale
            painter.drawRoundedRect(QRectF(center_x - w / 2, center_y + 17 * scale, w, h), 7 * scale, 7 * scale)
            return

        mouth = max(-1.0, min(1.0, float(face.get("mouth", 0.0))))
        if no_smile:
            mouth = min(0.0, mouth)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(eye_color, max(1.6, 3.0 * scale), Qt.SolidLine, Qt.RoundCap))
        if abs(mouth) < 0.10:
            if self._state not in {"thinking", "listening", "sleeping"}:
                painter.drawLine(QPointF(center_x - 11 * scale, center_y + 25 * scale), QPointF(center_x + 11 * scale, center_y + 25 * scale))
        elif mouth > 0:
            # Positive mouth value bends into a smile.
            h = (12 + 7 * mouth) * scale
            painter.drawArc(QRectF(center_x - 18 * scale, center_y + 10 * scale, 36 * scale, h), 180 * 16, 180 * 16)
        else:
            h = (12 + 7 * abs(mouth)) * scale
            painter.drawArc(QRectF(center_x - 18 * scale, center_y + 23 * scale, 36 * scale, h), 0, 180 * 16)

    def _draw_state_effect(
        self, painter: QPainter, center_x: float, center_y: float, scale: float, color: QColor
    ) -> None:
        # NEO 180: lit, moving effects for every state (jarvis/ui/state_fx180.py).
        from jarvis.ui import state_fx180
        painter.save()
        try:
            if state_fx180.draw(self, painter, self._state, center_x, center_y, scale, color):
                return
        finally:
            painter.restore()
        if self._state == "listening":
            # Soft symmetric sound ripples replace the old three rigid rings on
            # only one side. Each ripple expands and fades continuously, making
            # wake/listening feedback readable without looking like a detached ear.
            painter.setBrush(Qt.NoBrush)
            for side in (-1.0, 1.0):
                # Keep the first arc clearly outside the physical ear panel.
                ear_x = center_x + side * 145.0 * scale
                ear_y = center_y - 4.0 * scale
                for index in range(3):
                    phase = (self._elapsed * 0.82 + index / 3.0 + (0.06 if side > 0 else 0.0)) % 1.0
                    radius = (12.0 + 27.0 * phase) * scale
                    alpha = int(175 * (1.0 - phase) ** 1.65)
                    wave = QColor(color)
                    wave.setAlpha(max(0, alpha))
                    painter.setPen(QPen(wave, max(0.8, (1.75 - phase * 0.65) * scale), Qt.SolidLine, Qt.RoundCap))
                    rect = QRectF(ear_x - radius, ear_y - radius, radius * 2.0, radius * 2.0)
                    if side < 0:
                        painter.drawArc(rect, 90 * 16, 180 * 16)
                    else:
                        painter.drawArc(rect, -90 * 16, 180 * 16)
            return

        if self._state == "speaking":
            # NEO 177: soft voice waves leave the face on the right while NEO talks.
            painter.setBrush(Qt.NoBrush)
            x = center_x + 134.0 * scale
            y = center_y + 6.0 * scale
            for index in range(3):
                phase = (self._elapsed * 1.25 + index / 3.0) % 1.0
                radius = (8.0 + 22.0 * phase) * scale
                wave = QColor(color)
                wave.setAlpha(int(150 * (1.0 - phase) ** 1.5))
                painter.setPen(QPen(wave, max(0.8, (1.8 - phase) * scale), Qt.SolidLine, Qt.RoundCap))
                painter.drawArc(QRectF(x - radius, y - radius, radius * 2.0, radius * 2.0), -50 * 16, 100 * 16)
            return

        if self._state in {"thinking", "working"}:
            if self._state == "working" and int(max(0.0, self._elapsed - self._state_changed_at) / 9.0) % 2:
                self._draw_work_cards(painter, center_x, center_y, scale, color)
            else:
                self._draw_gears(painter, center_x + 122 * scale, center_y + 47 * scale, scale, color, self._phase)
            return

        if self._state == "searching":
            x = center_x + 116 * scale
            y = center_y - 45 * scale
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(color, max(2.0, 4 * scale), Qt.SolidLine, Qt.RoundCap))
            painter.drawEllipse(QPointF(x, y), 19 * scale, 19 * scale)
            painter.drawLine(
                QPointF(x + 14 * scale, y + 14 * scale),
                QPointF(x + 30 * scale, y + 30 * scale),
            )
            return

        if self._state == "browsing":
            rect = QRectF(center_x + 91 * scale, center_y - 67 * scale, 74 * scale, 52 * scale)
            painter.setPen(QPen(color, max(1.2, 2.5 * scale)))
            pane = QColor(self._colors["screen"])
            pane.setAlpha(220)
            painter.setBrush(pane)
            painter.drawRoundedRect(rect, 8 * scale, 8 * scale)
            painter.drawLine(rect.topLeft() + QPointF(0, 12 * scale), rect.topRight() + QPointF(0, 12 * scale))
            for i in range(3):
                painter.setBrush(color)
                painter.drawEllipse(
                    QPointF(rect.left() + (8 + i * 8) * scale, rect.top() + 6 * scale),
                    2 * scale,
                    2 * scale,
                )
            return

        if self._state == "reading":
            x = center_x + 120 * scale
            y = center_y - 30 * scale
            painter.setBrush(QColor(self._colors["screen"]))
            painter.setPen(QPen(color, max(1.2, 2.5 * scale)))
            left = QPainterPath()
            left.moveTo(x, y + 30 * scale)
            left.lineTo(x - 38 * scale, y + 20 * scale)
            left.lineTo(x - 38 * scale, y - 24 * scale)
            left.quadTo(x - 17 * scale, y - 20 * scale, x, y - 9 * scale)
            left.closeSubpath()
            right = QPainterPath()
            right.moveTo(x, y + 30 * scale)
            right.lineTo(x + 38 * scale, y + 20 * scale)
            right.lineTo(x + 38 * scale, y - 24 * scale)
            right.quadTo(x + 17 * scale, y - 20 * scale, x, y - 9 * scale)
            right.closeSubpath()
            painter.drawPath(left)
            painter.drawPath(right)
            return

        if self._state == "scanning":
            # Use orbiting dots instead of a horizontal scan line. The old line
            # looked like a rendering artifact on transparent Windows surfaces.
            painter.setPen(Qt.NoPen)
            for index in range(4):
                angle = self._phase * 1.7 + index * math.pi / 2
                dot = QColor(color)
                dot.setAlpha(115 + index * 30)
                painter.setBrush(dot)
                painter.drawEllipse(
                    QPointF(
                        center_x + math.cos(angle) * 116 * scale,
                        center_y - 8 * scale + math.sin(angle) * 72 * scale,
                    ),
                    4.5 * scale,
                    4.5 * scale,
                )
            return

        if self._state == "coding":
            painter.setPen(QPen(color, max(1.6, 3 * scale), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            x = center_x + 119 * scale
            y = center_y - 36 * scale
            painter.drawLine(QPointF(x - 25 * scale, y), QPointF(x - 36 * scale, y + 11 * scale))
            painter.drawLine(QPointF(x - 36 * scale, y + 11 * scale), QPointF(x - 25 * scale, y + 22 * scale))
            painter.drawLine(QPointF(x + 25 * scale, y), QPointF(x + 36 * scale, y + 11 * scale))
            painter.drawLine(QPointF(x + 36 * scale, y + 11 * scale), QPointF(x + 25 * scale, y + 22 * scale))
            painter.drawLine(QPointF(x + 7 * scale, y - 4 * scale), QPointF(x - 7 * scale, y + 27 * scale))
            return

        if self._state == "playful":
            # Keep the happy emotion but remove the old bouncing/orbiting ball.
            painter.setPen(Qt.NoPen)
            for index in range(3):
                sparkle = QColor(color)
                sparkle.setAlpha(70 + index * 35)
                painter.setBrush(sparkle)
                a = self._phase * 0.45 + index * 2.1
                x = center_x + (105 + index * 10) * scale
                y = center_y - (42 + index * 18) * scale + math.sin(a) * 5 * scale
                painter.drawEllipse(QPointF(x, y), (2.6 + index * 0.5) * scale, (2.6 + index * 0.5) * scale)
            return

        if self._state == "dancing":
            # HOTFIX76: remove the old two-floating-note decoration completely.
            # The robot may still use the dancing body pose when explicitly asked,
            # but that obsolete two-note idle animation can no longer appear.
            return

        if self._state == "looking":
            painter.setPen(QPen(color, max(1.4, 2.4 * scale)))
            painter.setBrush(Qt.NoBrush)
            x = center_x + 118 * scale
            y = center_y - 45 * scale
            painter.drawEllipse(QPointF(x, y), 16 * scale, 16 * scale)
            painter.drawLine(QPointF(x + 12 * scale, y + 12 * scale), QPointF(x + 24 * scale, y + 24 * scale))
            return

        if self._state == "stretching":
            painter.setPen(QPen(color, max(1.3, 2.4 * scale), Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(center_x - 115 * scale, center_y + 16 * scale), QPointF(center_x - 165 * scale, center_y - 18 * scale))
            painter.drawLine(QPointF(center_x + 115 * scale, center_y + 16 * scale), QPointF(center_x + 165 * scale, center_y - 18 * scale))
            return

        if self._state in {"happy", "success", "excited"}:
            painter.setPen(Qt.NoPen)
            for index in range(5):
                angle = self._phase * 0.55 + index * math.pi * 2 / 5
                radius = (118 + 9 * math.sin(self._phase * 1.4 + index)) * scale
                dot = QColor(color)
                dot.setAlpha(90 + (index % 3) * 45)
                painter.setBrush(dot)
                painter.drawEllipse(
                    QPointF(center_x + math.cos(angle) * radius, center_y + math.sin(angle) * 65 * scale),
                    (3.5 + (index % 2) * 1.5) * scale,
                    (3.5 + (index % 2) * 1.5) * scale,
                )
            return

        if self._state == "confused":
            painter.setPen(QPen(color, max(1.4, 2.6 * scale)))
            painter.setFont(QFont("Segoe UI", max(10, int(20 * scale)), QFont.Bold))
            painter.drawText(QPointF(center_x + 108 * scale, center_y - 52 * scale), "?")
            return

        if self._state == "typing":
            painter.setPen(QPen(color, max(1.2, 2.2 * scale)))
            y = center_y + 103 * scale
            for i in range(5):
                h = (4 + 6 * (math.sin(self._phase * 2.0 + i) + 1) / 2) * scale
                painter.drawLine(
                    QPointF(center_x - 34 * scale + i * 17 * scale, y),
                    QPointF(center_x - 34 * scale + i * 17 * scale, y - h),
                )
            return

        if self._state == "sleeping":
            # HOTFIX40.2: vector Z particles instead of changing font sizes every
            # few frames. Each particle eases out of the robot, drifts upward on a
            # continuous path and spends a long tail fading to zero before its
            # modulo cycle restarts. This removes the visible baseline/size jumps
            # that made the old text-glyph animation look torn or jerky.
            for index in range(5):
                cycle = (self._elapsed / 7.2 + index / 5.0) % 1.0
                travel = self._smoothstep(cycle)
                fade_in = self._smoothstep(cycle / 0.14)
                fade_out = self._smoothstep((1.0 - cycle) / 0.38)
                envelope = min(fade_in, fade_out)
                alpha = int(226 * envelope)
                if alpha < 2:
                    continue

                drift = math.sin(cycle * math.tau * 0.82 + index * 1.37) * (5.0 + index * 0.7)
                x = center_x + (84.0 + 54.0 * travel + drift) * scale
                y = center_y - (35.0 + 104.0 * travel) * scale
                z_size = (8.0 + index * 0.55 + 4.8 * travel) * scale
                half = z_size * 0.50
                sleepy = QColor(color)
                sleepy.setAlpha(alpha)
                painter.setPen(QPen(
                    sleepy,
                    max(1.0, (1.35 + 0.35 * travel) * scale),
                    Qt.SolidLine,
                    Qt.RoundCap,
                    Qt.RoundJoin,
                ))
                z_path = QPainterPath()
                z_path.moveTo(x - half, y - half)
                z_path.lineTo(x + half, y - half)
                z_path.lineTo(x - half, y + half)
                z_path.lineTo(x + half, y + half)
                painter.drawPath(z_path)

            # Bright transparent crescent. QPainterPath subtraction keeps the cut
            # physically transparent, so clouds/background remain visible through it.
            moon = QColor(self._environment_colors["moon"])
            mx, my, mr = center_x - 132 * scale, center_y - 82 * scale, 17 * scale
            halo = QColor(moon); halo.setAlpha(34)
            painter.setPen(Qt.NoPen); painter.setBrush(halo)
            painter.drawEllipse(QPointF(mx, my), mr * 1.65, mr * 1.65)
            full = QPainterPath(); full.addEllipse(QPointF(mx, my), mr, mr)
            cut = QPainterPath(); cut.addEllipse(QPointF(mx + 7.5 * scale, my - 4.0 * scale), mr * 0.96, mr * 0.96)
            crescent = full.subtracted(cut)
            moon.setAlpha(220)
            painter.setBrush(moon); painter.drawPath(crescent)
            # A few slow, fixed dream stars complement the breathing pose. No
            # particle objects, random allocation or additional timer per star.
            for index, (dx, dy) in enumerate(((-112, -115), (-145, -45), (24, -105))):
                twinkle = 0.5 + 0.5 * math.sin(self._elapsed * 0.65 + index * 2.0)
                star = QColor(color)
                star.setAlpha(45 + int(105 * twinkle))
                painter.setBrush(star)
                painter.drawPath(self._star_path(center_x + dx * scale, center_y + dy * scale,
                                                (2.4 + 1.4 * twinkle) * scale, 1.2 * scale, 4))
            return

    def _draw_work_cards(self, painter: QPainter, cx: float, cy: float, scale: float, color: QColor) -> None:
        """Small sorting-page motion between gear sequences, not fake progress."""
        for index in (1, 0):
            lift = math.sin(self._elapsed * 1.6 + index * 1.7) * 3.0
            rect = QRectF(cx + (98.0 + index * 6.0) * scale,
                          cy + (6.0 - index * 8.0 + lift) * scale, 43.0 * scale, 49.0 * scale)
            painter.setBrush(self._colors["screen"])
            painter.setPen(QPen(color, max(0.8, 1.5 * scale), Qt.SolidLine, Qt.RoundCap))
            painter.drawRoundedRect(rect, 5.0 * scale, 5.0 * scale)
            for row in range(3):
                start = rect.topLeft() + QPointF(9.0 * scale, (13.0 + row * 10.0) * scale)
                painter.drawLine(start, start + QPointF((24.0 - row * 4.0) * scale, 0.0))

    def _draw_micro_scene(self, painter: QPainter, cx: float, cy: float, scale: float, color: QColor) -> None:
        if self._state != "idle" or self._idle_action not in {"paper_plane"}:
            return
        painter.save()
        painter.setOpacity(self._idle_scene_reveal())
        t = max(0.0, self._elapsed - self._idle_action_started_at)
        x = cx + (180.0 + 12.0 * math.sin(t * 0.8)) * scale
        y = cy + (-21.0 + 18.0 * math.sin(t * 1.4)) * scale
        painter.setPen(QPen(color, max(0.8, 1.5 * scale), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        if self._idle_action == "paper_plane":
            # NEO 180: a lit paper plane on a figure-eight loop with a fading dotted contrail.
            from jarvis.ui.ambience180 import _blit, _glow

            def where(tt: float) -> QPointF:
                return QPointF(cx + (180.0 + 26.0 * math.sin(tt * 0.8)) * scale,
                               cy + (-24.0 + 22.0 * math.sin(tt * 1.6)) * scale)

            here = where(t)
            ahead = where(t + 0.05)
            for index in range(14):
                lag = 0.12 + index * 0.11
                dot = where(t - lag)
                trail = QColor(color)
                trail.setAlpha(int(150 * (1.0 - index / 14.0) ** 1.6))
                painter.setPen(Qt.NoPen)
                painter.setBrush(trail)
                painter.drawEllipse(dot, (1.5 - index * 0.06) * scale, (1.5 - index * 0.06) * scale)
            _blit(painter, _glow(color, 48), here.x(), here.y(), 26.0 * scale, None, 0.35)
            painter.translate(here)
            painter.rotate(math.degrees(math.atan2(ahead.y() - here.y(), ahead.x() - here.x())))
            wing = QPainterPath(QPointF(-21.0 * scale, -10.0 * scale))
            for px, py in ((23, 0), (-8, 2), (-21, -10)):
                wing.lineTo(px * scale, py * scale)
            fold = QPainterPath(QPointF(-15.0 * scale, 17.0 * scale))
            for px, py in ((23, 0), (-8, 2), (-15, 17)):
                fold.lineTo(px * scale, py * scale)
            paper = QLinearGradient(QPointF(-20 * scale, -10 * scale), QPointF(20 * scale, 12 * scale))
            paper.setColorAt(0.0, QColor(255, 255, 255, 235))
            paper.setColorAt(1.0, QColor(color).lighter(150))
            painter.setPen(QPen(color, max(0.8, 1.3 * scale), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(paper)
            painter.drawPath(wing)
            painter.setBrush(QColor(color).lighter(120))
            painter.drawPath(fold)
        painter.restore()

    @staticmethod
    def _star_path(x: float, y: float, outer: float, inner: float, points: int = 5, rotation: float = -math.pi / 2) -> QPainterPath:
        path = QPainterPath()
        count = max(3, int(points)) * 2
        for i in range(count):
            a = rotation + i * math.pi / max(3, int(points))
            r = outer if i % 2 == 0 else inner
            pt = QPointF(x + math.cos(a) * r, y + math.sin(a) * r)
            if i == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
        path.closeSubpath()
        return path

    @staticmethod
    def _draw_cloud_shape(
        painter: QPainter, x: float, y: float, width: float, height: float,
        color: QColor, *, variant: int = 0,
    ) -> None:
        """Draw one of several smooth cloud silhouettes.

        The old cloud was a single repeated blob, so a row of copies looked like
        stacked circles. Variants keep the same bounding box but change crown and
        tail proportions while staying soft enough for translucent atmosphere.
        """
        v = int(variant) % 4
        path = QPainterPath()
        if v == 0:
            path.moveTo(x, y + height * 0.72)
            path.cubicTo(x + width * 0.04, y + height * 0.39, x + width * 0.15, y + height * 0.29, x + width * 0.28, y + height * 0.43)
            path.cubicTo(x + width * 0.32, y + height * 0.08, x + width * 0.55, y - height * 0.02, x + width * 0.64, y + height * 0.36)
            path.cubicTo(x + width * 0.79, y + height * 0.20, x + width * 0.94, y + height * 0.35, x + width, y + height * 0.61)
        elif v == 1:
            path.moveTo(x, y + height * 0.69)
            path.cubicTo(x + width * 0.06, y + height * 0.42, x + width * 0.19, y + height * 0.36, x + width * 0.30, y + height * 0.49)
            path.cubicTo(x + width * 0.38, y + height * 0.13, x + width * 0.61, y + height * 0.12, x + width * 0.68, y + height * 0.45)
            path.cubicTo(x + width * 0.82, y + height * 0.28, x + width * 0.97, y + height * 0.43, x + width, y + height * 0.66)
        elif v == 2:
            path.moveTo(x, y + height * 0.74)
            path.cubicTo(x + width * 0.03, y + height * 0.49, x + width * 0.11, y + height * 0.37, x + width * 0.23, y + height * 0.46)
            path.cubicTo(x + width * 0.27, y + height * 0.19, x + width * 0.43, y + height * 0.10, x + width * 0.52, y + height * 0.31)
            path.cubicTo(x + width * 0.62, y - height * 0.01, x + width * 0.80, y + height * 0.08, x + width * 0.82, y + height * 0.41)
            path.cubicTo(x + width * 0.94, y + height * 0.36, x + width, y + height * 0.52, x + width, y + height * 0.70)
        else:
            path.moveTo(x, y + height * 0.70)
            path.cubicTo(x + width * 0.08, y + height * 0.48, x + width * 0.18, y + height * 0.48, x + width * 0.27, y + height * 0.56)
            path.cubicTo(x + width * 0.31, y + height * 0.21, x + width * 0.48, y + height * 0.15, x + width * 0.56, y + height * 0.43)
            path.cubicTo(x + width * 0.65, y + height * 0.16, x + width * 0.86, y + height * 0.21, x + width * 0.88, y + height * 0.50)
            path.cubicTo(x + width * 0.95, y + height * 0.46, x + width, y + height * 0.56, x + width, y + height * 0.68)
        path.cubicTo(x + width * 1.01, y + height * 0.87, x + width * 0.88, y + height, x + width * 0.67, y + height * 0.92)
        path.lineTo(x + width * 0.18, y + height * 0.92)
        path.cubicTo(x + width * 0.07, y + height * 0.91, x - width * 0.01, y + height * 0.83, x, y + height * 0.70)
        path.closeSubpath()
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPath(path)
        highlight = QColor(color)
        highlight.setAlpha(min(130, max(20, color.alpha() + 18)))
        painter.setPen(QPen(highlight, 0.75, Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

    def _draw_constellation_pattern(
        self, painter: QPainter, cx: float, cy: float, scale: float,
        points: tuple[tuple[float, float], ...], edges: tuple[tuple[int, int], ...],
        color: QColor, phase: float,
    ) -> None:
        # Two-pass luminous line work: a soft halo underneath a crisp thin line.
        halo = QColor(color); halo.setAlpha(42)
        painter.setPen(QPen(halo, max(2.2, 4.0 * scale), Qt.SolidLine, Qt.RoundCap))
        for a, b in edges:
            if a < len(points) and b < len(points):
                painter.drawLine(
                    QPointF(cx + points[a][0] * scale, cy + points[a][1] * scale),
                    QPointF(cx + points[b][0] * scale, cy + points[b][1] * scale),
                )
        line = QColor(color); line.setAlpha(205)
        painter.setPen(QPen(line, max(0.85, 1.18 * scale), Qt.SolidLine, Qt.RoundCap))
        for a, b in edges:
            if a < len(points) and b < len(points):
                painter.drawLine(
                    QPointF(cx + points[a][0] * scale, cy + points[a][1] * scale),
                    QPointF(cx + points[b][0] * scale, cy + points[b][1] * scale),
                )
        painter.setPen(Qt.NoPen)
        for i, (px, py) in enumerate(points):
            pulse = (math.sin(self._elapsed * 0.92 + phase + i * 0.91) + 1.0) * 0.5
            x = cx + px * scale; y = cy + py * scale
            glow_star = QColor(color); glow_star.setAlpha(int(28 + 34 * pulse))
            painter.setBrush(glow_star)
            painter.drawEllipse(QPointF(x, y), (4.8 + pulse * 1.8) * scale, (4.8 + pulse * 1.8) * scale)
            star = QColor(color); star.setAlpha(int(205 + 48 * pulse))
            painter.setBrush(star)
            rr = (1.72 + (i % 3) * 0.28 + pulse * 0.58) * scale
            painter.drawEllipse(QPointF(x, y), rr, rr)


    @staticmethod
    def _ease_out_back177(value: float) -> float:
        value = max(0.0, min(1.0, float(value)))
        c1 = 1.45
        return 1.0 + (c1 + 1.0) * (value - 1.0) ** 3 + c1 * (value - 1.0) ** 2

    def _music_energy177(self) -> tuple[float, float]:
        """(bass, overall) loudness 0..1 from the real spectrum shown on the belly."""
        bars = list(getattr(self, "_eq_bars177", []) or [])
        if not bars:
            return 0.0, 0.0
        bass = sum(bars[:5]) / max(1, len(bars[:5]))
        overall = sum(bars) / len(bars)
        return max(0.0, min(1.0, bass)), max(0.0, min(1.0, overall))

    def _draw_headphones(self, painter: QPainter, cx: float, cy: float, scale: float) -> None:
        """NEO 177: over-ear headphones with a padded band, sliders, shaded cups and an LED ring."""
        if not self._headphones_enabled:
            return
        strength = float(getattr(self, "_music_visual_strength", 0.0) or 0.0)
        if strength <= 0.015:
            return
        glow = QColor(self._headphones_color)
        # HOTFIX128: the cups sit on the robot's real side-ear panels (x = ±98..±126).
        intro = self._ease_out_back177(strength)
        drop = (1.0 - intro) * 88.0 * scale
        fade = max(0.0, min(1.0, strength * 1.6))
        bass, overall = self._music_energy177()
        painter.save()
        painter.setOpacity(fade)
        cup_y = cy - 37.0 * scale - drop
        cup_w = 50.0 * scale
        cup_h = 82.0 * scale
        left_cup = QRectF(cx - 145.0 * scale, cup_y, cup_w, cup_h)
        right_cup = QRectF(cx + 95.0 * scale, cup_y, cup_w, cup_h)

        # Padded band: dark cushion under a coloured band with a light accent line.
        band = QPainterPath(QPointF(cx - 119.0 * scale, cy - 28.0 * scale - drop))
        band.cubicTo(
            QPointF(cx - 116.0 * scale, cy - 108.0 * scale - drop),
            QPointF(cx + 116.0 * scale, cy - 108.0 * scale - drop),
            QPointF(cx + 119.0 * scale, cy - 28.0 * scale - drop),
        )
        cushion = QPainterPath(QPointF(cx - 62.0 * scale, cy - 79.0 * scale - drop))
        cushion.cubicTo(
            QPointF(cx - 34.0 * scale, cy - 89.0 * scale - drop),
            QPointF(cx + 34.0 * scale, cy - 89.0 * scale - drop),
            QPointF(cx + 62.0 * scale, cy - 79.0 * scale - drop),
        )
        shadow = QColor(0, 0, 0, 70)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(shadow, max(4.0, 9.5 * scale), Qt.SolidLine, Qt.RoundCap))
        painter.drawPath(band)
        pad_dark = QColor(self._colors["trim"]).darker(118)
        painter.setPen(QPen(pad_dark, max(3.0, 9.0 * scale), Qt.SolidLine, Qt.RoundCap))
        painter.drawPath(cushion)
        stitch = QColor(self._colors["trim"]).lighter(135); stitch.setAlpha(150)
        painter.setPen(QPen(stitch, max(0.6, 1.0 * scale), Qt.DashLine, Qt.RoundCap))
        painter.drawPath(cushion)
        band_pen = QLinearGradient(QPointF(cx, cy - 108.0 * scale - drop), QPointF(cx, cy - 28.0 * scale - drop))
        band_pen.setColorAt(0.0, glow.lighter(130)); band_pen.setColorAt(1.0, glow.darker(125))
        painter.setPen(QPen(band_pen, max(2.4, 6.0 * scale), Qt.SolidLine, Qt.RoundCap))
        painter.drawPath(band)
        accent_line = QColor(255, 255, 255, 120)
        painter.setPen(QPen(accent_line, max(0.7, 1.3 * scale), Qt.SolidLine, Qt.RoundCap))
        painter.drawPath(band.translated(0.0, -1.3 * scale))

        # Metal sliders from the band into the cups.
        slider = QColor(self._colors["trim"]).lighter(125)
        painter.setPen(QPen(slider, max(1.4, 3.4 * scale), Qt.SolidLine, Qt.RoundCap))
        for side in (-1.0, 1.0):
            painter.drawLine(QPointF(cx + side * 119.0 * scale, cy - 30.0 * scale - drop),
                             QPointF(cx + side * 120.0 * scale, cup_y + 6.0 * scale))

        # Cups pulse slightly with the bass of the real music.
        pump = 1.0 + 0.045 * bass
        for cup in (left_cup, right_cup):
            painter.save()
            painter.translate(cup.center())
            painter.scale(pump, pump)
            painter.translate(-cup.center())
            shell = QLinearGradient(cup.topLeft(), cup.bottomRight())
            shell.setColorAt(0.0, glow.lighter(135))
            shell.setColorAt(0.5, glow)
            shell.setColorAt(1.0, glow.darker(150))
            painter.setPen(QPen(glow.darker(170), max(1.0, 1.6 * scale)))
            painter.setBrush(shell)
            painter.drawRoundedRect(cup, 21.0 * scale, 21.0 * scale)
            painter.restore()

        pad = QColor(self._colors["screen"]); pad.setAlpha(int(185 + 45 * min(1.0, intro)))
        pad_y = cy - 28.0 * scale - drop
        pads = (QRectF(cx - 137.0 * scale, pad_y, 36.0 * scale, 64.0 * scale),
                QRectF(cx + 102.0 * scale, pad_y, 36.0 * scale, 64.0 * scale))
        for plate in pads:
            plate_fill = QRadialGradient(plate.center(), plate.height() * 0.62)
            plate_fill.setColorAt(0.0, QColor(pad).lighter(150))
            plate_fill.setColorAt(1.0, pad)
            painter.setPen(QPen(QColor(0, 0, 0, 90), max(0.8, 1.2 * scale)))
            painter.setBrush(plate_fill)
            painter.drawRoundedRect(plate, 15.0 * scale, 15.0 * scale)
            # LED ring that breathes with the music's loudness.
            ring = QColor(glow).lighter(125)
            ring.setAlpha(int(80 + 170 * min(1.0, overall * 1.6)))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(ring, max(1.0, 2.0 * scale)))
            painter.drawRoundedRect(plate.adjusted(6.0 * scale, 9.0 * scale, -6.0 * scale, -9.0 * scale), 10.0 * scale, 10.0 * scale)
            painter.setPen(Qt.NoPen)
            painter.setBrush(ring)
            painter.drawEllipse(plate.center(), 2.4 * scale, 2.4 * scale)
        painter.restore()

    def _draw_idle_toy(self, painter: QPainter, cx: float, cy: float, scale: float, color: QColor) -> None:
        music_active = bool(self._info_state.get("playing", False)) and not bool(self._info_state.get("paused", False))
        music_visible = float(getattr(self, "_music_visual_strength", 0.0) or 0.0) > 0.015
        if self._state != "idle" and not music_visible:
            return
        action = "music_bop" if music_visible else self._idle_action
        if action not in {"hologram", "music_bop", "inspect_orb", "solar_system", "galaxy", "constellation"}:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        # Music is present for the whole playback session. On a transient state
        # change the idle-scene timestamp may be reset, so never fade/head-drop
        # the headphones out merely because NEO started listening or speaking.
        reveal = self._idle_scene_reveal() if self._state == "idle" else 1.0
        dx, dy = self._idle_scene_offset(action)
        painter.translate(dx, dy)
        painter.setOpacity(max(0.02, reveal))
        glow = QColor(color)
        glow.setAlpha(190)
        painter.setPen(QPen(glow, max(1.2, 2.2 * scale), Qt.SolidLine, Qt.RoundCap))
        painter.setBrush(Qt.NoBrush)
        # Interactive idle props are anchored to the stable stage, not the
        # robot's animated head coordinates. This prevents constellations/toys
        # from snapping left/right when the pose interpolates and keeps props
        # outside the robot silhouette while still scaling with the window.
        stage_cx, stage_cy = self._content_center()
        stage_w = float(max(1, self.width()))
        stage_h = float(max(1, self.height()))
        robot_guard = QRectF(
            stage_cx - 142.0 * scale,
            stage_cy - 91.0 * scale,
            284.0 * scale,
            220.0 * scale,
        )
        if action == "solar_system":
            # Match the HOTFIX108 downward pose so collision fitting sees the
            # robot where it is actually painted.
            robot_guard.translate(0.0, 33.0 * scale)

        def _right_anchor(extent_x: float, extent_y: float, y_value: float | None = None) -> tuple[float, float]:
            margin = max(6.0, 8.0 * scale)
            left = robot_guard.right() + max(8.0, 13.0 * scale)
            right = stage_w - margin
            available = max(1.0, right - left)
            x = left + available * 0.5
            y = stage_cy - 8.0 * scale if y_value is None else float(y_value)
            x = max(extent_x + margin, min(stage_w - extent_x - margin, x))
            y = max(extent_y + margin, min(stage_h - extent_y - margin, y))
            return x, y
        if action in {"hologram", "inspect_orb"}:
            # HOTFIX44: scientific atom scene. Three fixed orbital ellipses cross
            # through one nucleus, while electrons actually travel along those
            # paths. The orbit geometry does not wobble or randomly reshape.
            orbit_rx = 37.0 * scale
            orbit_ry = 14.5 * scale
            atom_margin = orbit_rx + 7.0 * scale
            x, y = _right_anchor(atom_margin, 31.0 * scale, stage_cy - 8.0 * scale)
            orbit_angles = (0.0, 60.0, 120.0)
            for i, angle_deg in enumerate(orbit_angles):
                painter.save()
                painter.translate(x, y)
                painter.rotate(angle_deg)
                orbit_halo = QColor(glow); orbit_halo.setAlpha(38)
                painter.setPen(QPen(orbit_halo, max(2.2, 4.0 * scale), Qt.SolidLine, Qt.RoundCap))
                painter.drawEllipse(QRectF(-orbit_rx, -orbit_ry, orbit_rx * 2, orbit_ry * 2))
                orbit = QColor(glow); orbit.setAlpha(190)
                painter.setPen(QPen(orbit, max(0.8, 1.15 * scale), Qt.SolidLine, Qt.RoundCap))
                painter.drawEllipse(QRectF(-orbit_rx, -orbit_ry, orbit_rx * 2, orbit_ry * 2))
                a = self._elapsed * (1.25 + i * 0.13) + i * math.tau / 3.0
                ex = math.cos(a) * orbit_rx
                ey = math.sin(a) * orbit_ry
                electron_halo = QColor(glow); electron_halo.setAlpha(70)
                painter.setPen(Qt.NoPen); painter.setBrush(electron_halo)
                painter.drawEllipse(QPointF(ex, ey), 5.2 * scale, 5.2 * scale)
                electron = QColor(glow); electron.setAlpha(250)
                painter.setBrush(electron)
                painter.drawEllipse(QPointF(ex, ey), 2.2 * scale, 2.2 * scale)
                painter.restore()
            # Nucleus = a compact proton/neutron cluster, not a single generic orb.
            nucleus_halo = QColor(glow); nucleus_halo.setAlpha(60)
            painter.setPen(Qt.NoPen); painter.setBrush(nucleus_halo)
            painter.drawEllipse(QPointF(x, y), 10.0 * scale, 10.0 * scale)
            nucleus_points = ((-3.2,-1.7),(3.0,-1.5),(0.1,3.0))
            for i, (nx, ny) in enumerate(nucleus_points):
                c = QColor(glow if i != 1 else self._colors["shell"]); c.setAlpha(245)
                painter.setBrush(c)
                painter.drawEllipse(QPointF(x + nx*scale, y + ny*scale), 3.6*scale, 3.6*scale)
        elif action == "music_bop":
            if not self._headphones_enabled:
                return
            glow = QColor(self._headphones_color)
            # NEO 177: the headphones themselves are drawn with the head (and tilt with it).
            # HOTFIX129: musical notes should feel alive, not like an occasional
            # slow particle effect. Keep eight independent lanes (four per side)
            # and a ~1.34 s rise cycle; phased spawning yields a dense, lively
            # stream of notes without overlapping the robot itself.
            lane_margin = max(14.0 * scale, 18.0)
            left_start = lane_margin
            left_end = max(left_start + 36.0 * scale, robot_guard.left() - 18.0 * scale)
            right_start = min(stage_w - lane_margin - 36.0 * scale, robot_guard.right() + 18.0 * scale)
            right_end = stage_w - lane_margin
            def _four_lanes(a: float, b: float) -> tuple[float, float, float, float]:
                span = max(1.0, b - a)
                return (a + span * 0.12, a + span * 0.38, a + span * 0.64, a + span * 0.90)
            lane_xs = _four_lanes(left_start, left_end) + _four_lanes(right_start, right_end)
            phase_offsets = (0.00, 0.13, 0.26, 0.39, 0.52, 0.65, 0.78, 0.91)
            note_glyphs = ("♪", "♫", "♩", "♬", "♭", "♯")
            start_y = stage_cy - 28.0 * scale
            top_y = max(14.0 * scale, stage_cy - 154.0 * scale)
            note_font = QFont("Segoe UI Symbol", max(11, int(14.5 * scale)), QFont.DemiBold)
            for lane_index, (lane_x, phase_offset) in enumerate(zip(lane_xs, phase_offsets)):
                absolute_cycle = self._elapsed / 1.34 + phase_offset
                cycle_index = int(math.floor(absolute_cycle))
                cycle = absolute_cycle - cycle_index
                travel = self._smoothstep(cycle)
                fade_in = self._smoothstep(cycle / 0.16)
                fade_out = self._smoothstep((1.0 - cycle) / 0.22)
                fade = min(fade_in, fade_out)
                if fade <= 0.012:
                    continue
                # Glyph changes once per spawn, not every frame. Position is a
                # straight vertical path, eliminating the old side-to-side jitter.
                glyph = note_glyphs[(cycle_index * 5 + lane_index * 3) % len(note_glyphs)]
                alpha = int(236 * fade)
                x = lane_x
                y = start_y + (top_y - start_y) * travel
                note_color = QColor(glow)
                note_color.setAlpha(alpha)
                # NEO 177: a note pops in, leans smoothly as it rises and carries a soft glow.
                pop = 0.55 + 0.45 * self._smoothstep(cycle / 0.18)
                lean = (travel - 0.5) * (16.0 if lane_index % 2 else -16.0)
                halo = QColor(glow).lighter(130)
                halo.setAlpha(int(alpha * 0.35))
                painter.save()
                painter.translate(x, y)
                painter.rotate(lean)
                painter.scale(pop, pop)
                painter.setFont(note_font)
                painter.setPen(QPen(halo, max(2.0, 3.0 * scale), Qt.SolidLine, Qt.RoundCap))
                painter.drawText(QPointF(1.0 * scale, 1.0 * scale), glyph)
                painter.setPen(QPen(note_color, max(0.9, 1.05 * scale), Qt.SolidLine, Qt.RoundCap))
                painter.drawText(QPointF(0.0, 0.0), glyph)
                painter.restore()

        elif action == "solar_system":
            # HOTFIX108: large overhead solar-system toy. The orbit field is centred
            # above the head, grows beyond normal robot scale when space permits,
            # and contracts only as needed to stay inside both the window and a
            # padded robot exclusion zone. It is never allowed to paint through
            # the shell or outside the stage.
            base_rx = 142.0
            base_ry = 37.0
            margin = max(6.0, 7.5 * scale)
            gap = max(7.0, 10.0 * scale)
            top_limit = margin
            bottom_limit = robot_guard.top() - gap
            available_h = bottom_limit - top_limit
            available_w = stage_w - margin * 2.0

            # Prefer a visibly larger-than-robot-scale system. On compact windows
            # the exact same geometry scales down instead of overlapping/clipping.
            fit_w = max(0.0, available_w / (base_rx * 2.0 + 10.0))
            fit_h = max(0.0, available_h / (base_ry * 2.0 + 8.0))
            solar_scale = min(scale * 1.72, fit_w, fit_h)
            if solar_scale < 0.24 * scale:
                painter.restore()
                return

            max_rx = base_rx * solar_scale
            max_ry = base_ry * solar_scale
            x = max(margin + max_rx, min(stage_w - margin - max_rx, stage_cx))
            # Keep the bottom edge close to the robot so the scene reads as an
            # object above its head rather than a generic sky decoration.
            y = bottom_limit - max_ry
            y = max(top_limit + max_ry, min(bottom_limit - max_ry, y))

            glow_base = QColor(self._colors["glow"])
            eye_base = QColor(self._colors["eye"])
            shell_base = QColor(self._colors["shell"])
            trim_base = QColor(self._colors["trim"])
            body_base = QColor(self._colors["body"])

            solar_glow = QColor(glow_base); solar_glow.setAlpha(46)
            painter.setPen(Qt.NoPen); painter.setBrush(solar_glow)
            painter.drawEllipse(QPointF(x, y), 18.5*solar_scale, 18.5*solar_scale)
            sun = QColor(glow_base.lighter(132)); sun.setAlpha(248)
            painter.setBrush(sun)
            painter.drawEllipse(QPointF(x, y), 10.2*solar_scale, 10.2*solar_scale)

            palette = (
                trim_base.lighter(125), shell_base.darker(118), eye_base.lighter(118),
                glow_base.darker(112), body_base.lighter(155), trim_base.lighter(155),
            )
            bodies = (
                (25,  8, .84, 2.2, 0.25),
                (39, 12, .64, 3.0, 1.15),
                (55, 17, .48, 3.2, 2.10),
                (74, 22, .38, 2.9, 3.05),
                (109, 30, .26, 5.2, 4.00),
                (142, 37, .19, 4.7, 5.10),
            )
            for i, (rx, ry, speed, size, phase) in enumerate(bodies):
                orbit = QColor(trim_base.lighter(118)); orbit.setAlpha(36 + i * 4)
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(orbit, max(.65, .88*solar_scale)))
                painter.drawEllipse(QRectF(x-rx*solar_scale, y-ry*solar_scale, 2*rx*solar_scale, 2*ry*solar_scale))
                a = self._elapsed * speed * math.tau + phase
                px = x + math.cos(a) * rx * solar_scale
                py = y + math.sin(a) * ry * solar_scale
                planet = QColor(palette[i]); planet.setAlpha(238)
                halo = QColor(planet); halo.setAlpha(44)
                painter.setPen(Qt.NoPen); painter.setBrush(halo)
                painter.drawEllipse(QPointF(px, py), (size+2.0)*solar_scale, (size+2.0)*solar_scale)
                painter.setBrush(planet)
                painter.drawEllipse(QPointF(px, py), size*solar_scale, size*solar_scale)
                if i == 5:
                    ring = QColor(trim_base.lighter(145)); ring.setAlpha(170)
                    painter.setBrush(Qt.NoBrush); painter.setPen(QPen(ring, max(.65, .95*solar_scale)))
                    painter.drawEllipse(QRectF(px-7.4*solar_scale, py-2.5*solar_scale, 14.8*solar_scale, 5.0*solar_scale))
        elif action == "galaxy":
            # HOTFIX72: six galaxies total (three on each side) with a stable
            # placement pass so they keep clear of the robot and of each other.
            painter.setPen(Qt.NoPen)
            placed_galaxy_rects: list[QRectF] = []
            def draw_galaxy(gx: float, gy: float, size: float, phase: float, arms: int, tilt: float, speed: float, flatten: float) -> None:
                painter.save(); painter.translate(gx, gy); painter.rotate(tilt)
                core = QColor(glow).lighter(125 if arms % 2 else 108); core.setAlpha(205); painter.setBrush(core)
                painter.drawEllipse(QPointF(0,0), 5.8*scale*size, 3.5*scale*size)
                for arm in range(arms):
                    for i in range(11):
                        t = i / 10.0
                        a = self._elapsed * speed + phase + arm*math.tau/arms + t*(3.45 + 0.25*arms)
                        r = (7.0 + 34.0*t) * scale * size
                        dot = QColor(glow).lighter(100 + (arm*7)%24); dot.setAlpha(max(32, 190-int(t*118))); painter.setBrush(dot)
                        jitter = math.sin(self._elapsed*0.8 + i*1.7 + arm) * 1.3 * scale * size
                        px = math.cos(a)*r
                        py = math.sin(a)*r*flatten + jitter
                        rr = (2.45 - 1.05*t) * scale * size
                        painter.drawEllipse(QPointF(px,py), rr, rr)
                painter.restore()
            def safe_galaxy(gx: float, gy: float, size: float, phase: float, arms: int, tilt: float, speed: float, flatten: float) -> None:
                # Fit each galaxy into a stage-side zone and shrink it if needed.
                # The stable robot guard prevents every arm/core from ever being
                # painted over the robot, even during pose transitions.
                local_size = float(size)
                candidate_offsets = (
                    (0.0, 0.0), (0.0, -26.0*scale), (0.0, 26.0*scale),
                    (-28.0*scale, 0.0), (28.0*scale, 0.0),
                    (-18.0*scale, -18.0*scale), (18.0*scale, 18.0*scale),
                )
                for _ in range(7):
                    extent = (41.0 * scale * local_size) + 5.0
                    width = float(max(1, self.width())); height = float(max(1, self.height()))
                    base_x = width * 0.5 if width <= extent * 2 else max(extent, min(width - extent, gx))
                    base_y = height * 0.5 if height <= extent * 2 else max(extent, min(height - extent, gy))
                    for off_x, off_y in candidate_offsets:
                        px = max(extent, min(width - extent, base_x + off_x))
                        py = max(extent, min(height - extent, base_y + off_y))
                        rect = QRectF(px - extent, py - extent, extent * 2.0, extent * 2.0)
                        if rect.intersects(robot_guard):
                            continue
                        if any(rect.adjusted(-6.0*scale, -6.0*scale, 6.0*scale, 6.0*scale).intersects(prev) for prev in placed_galaxy_rects):
                            continue
                        draw_galaxy(px, py, local_size, phase, arms, tilt, speed, flatten)
                        placed_galaxy_rects.append(rect)
                        return
                    local_size *= 0.78
                # If a very compact window has no safe footprint, omit this one
                # rather than allowing a broken/clipped galaxy over the robot.

            # Three galaxies on the left and three on the right. The upper-right
            # galaxy remains horizontal and slightly above the head line.
            galaxy_specs = (
                (stage_w*0.12, stage_h*0.15, .78, 1.1, 4, 24, -.50, .55),
                (stage_w*0.08, stage_h*0.50, .72, 2.1, 2, -12, .72, .34),
                (stage_w*0.16, stage_h*0.82, .70, 4.8, 3, -32, -.58, .40),
                (stage_w*0.88, stage_h*0.14, .86, 0.0, 3, 0, .66, .34),
                (stage_w*0.92, stage_h*0.52, .76, 2.8, 5, -38, .44, .48),
                (stage_w*0.86, stage_h*0.84, .70, 4.0, 3, 38, -.56, .39),
            )
            for spec in galaxy_specs:
                safe_galaxy(*spec)
            painter.setPen(QPen(glow, max(1.0, 1.5*scale)))
        elif action == "constellation":
            # HOTFIX44: stable but naturally irregular star field. Every scene gets
            # one seeded layout: constellations are scattered at different heights
            # and scales, then remain completely fixed until the scene ends.
            # No square/Great-Square pattern is used because it read as a broken
            # overlay in the compact widget.
            constellations = (
                # Big Dipper / Ursa Major asterism
                (((-31,11),(-20,2),(-8,4),(4,-5),(17,-9),(30,-3),(25,11)),
                 ((0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,3))),
                # Cassiopeia W
                (((-30,5),(-15,-12),(0,4),(16,-13),(31,3)),
                 ((0,1),(1,2),(2,3),(3,4))),
                # Orion
                (((-22,-23),(21,-21),(-12,-4),(0,-2),(12,-4),(-17,25),(18,26),(0,14)),
                 ((0,2),(1,4),(2,3),(3,4),(2,5),(4,6),(3,7))),
                # Cygnus / Northern Cross
                (((0,-31),(0,-12),(0,7),(0,31),(-27,1),(27,1)),
                 ((0,1),(1,2),(2,3),(4,2),(2,5))),
                # Scorpius hook
                (((-29,-17),(-18,-8),(-7,-13),(4,-5),(7,7),(14,18),(25,20),(31,11),(27,2)),
                 ((0,1),(1,2),(2,3),(3,4),(4,5),(5,6),(6,7),(7,8))),
                # Leo
                (((-29,9),(-18,-6),(-5,-16),(8,-9),(6,5),(22,14),(31,7),(-8,18)),
                 ((0,1),(1,2),(2,3),(3,4),(4,7),(7,0),(4,5),(5,6))),
                # Taurus: Hyades V + horns
                (((-27,10),(-13,2),(0,8),(12,-1),(27,-14),(18,17),(34,23)),
                 ((0,1),(1,2),(2,3),(3,4),(2,5),(5,6))),
                # Aquila
                (((-30,4),(-15,-2),(0,0),(16,-4),(31,3),(-5,18),(8,20)),
                 ((0,1),(1,2),(2,3),(3,4),(2,5),(5,6),(6,2))),
            )

            width = float(max(1, self.width()))
            height = float(max(1, self.height()))
            # Seed depends only on scene start, therefore resizing/painting never
            # shuffles identities or makes patterns teleport.
            seed = int(max(1.0, self._idle_action_started_at) * 1000.0) ^ 0x44C057
            rng = random.Random(seed)
            order = list(range(len(constellations)))
            rng.shuffle(order)
            # Anchors deliberately occupy top, far sides and middle side-space.
            anchors = [
                (0.08,0.13),(0.35,0.075),(0.68,0.11),(0.92,0.20),
                (0.07,0.49),(0.27,0.34),(0.30,0.70),(0.88,0.72),
            ]
            # HOTFIX83: the previous 0.78-1.36 range plus strict no-overlap
            # rejection made compact stages drop almost every constellation. Keep
            # useful variation, but let patterns step down gracefully until they
            # fit instead of simply disappearing.
            scales = [0.62,0.68,0.74,0.80,0.86,0.92,0.98,1.04]
            rng.shuffle(scales)
            # The small Lyra from the screenshot is stage-anchored instead of
            # attached to head_x. That removes the left/right snapping seen while
            # the robot pose was interpolating.
            hand_points = ((-12,-20),(9,-13),(14,8),(-7,15),(-17,2))
            hand_edges = ((0,1),(1,2),(2,3),(3,4),(4,1))
            hand_x, hand_y = _right_anchor(28.0*scale, 28.0*scale, stage_cy - 20.0*scale)
            hand_exclusion = QRectF(hand_x - 31*scale, hand_y - 33*scale, 62*scale, 66*scale)
            placed_rects: list[QRectF] = []

            def overlap_fraction(rect: QRectF, other: QRectF) -> float:
                inter = rect.intersected(other)
                if inter.isEmpty():
                    return 0.0
                area = max(1.0, rect.width() * rect.height())
                return max(0.0, inter.width() * inter.height()) / area

            fallback_anchors = (
                (0.06,0.12),(0.22,0.08),(0.48,0.08),(0.76,0.08),(0.94,0.14),
                (0.06,0.46),(0.94,0.46),(0.06,0.78),(0.24,0.90),(0.76,0.90),(0.94,0.78),
            )
            for slot, idx in enumerate(order[:8]):
                pts, edges = constellations[idx]
                fx, fy = anchors[slot]
                fx += rng.uniform(-0.018, 0.018)
                fy += rng.uniform(-0.020, 0.020)
                candidate_anchors = ((fx, fy),) + fallback_anchors[slot:] + fallback_anchors[:slot]
                chosen: tuple[float, float, float, QRectF] | None = None

                # Try the preferred size first, then progressively smaller sizes.
                # Mild constellation/constellation overlap is allowed; only the
                # robot silhouette and hand detail remain hard exclusions.
                for shrink in (1.0, 0.90, 0.80, 0.70, 0.60, 0.52, 0.45):
                    item_scale = max(0.34 * scale, scales[slot] * scale * shrink)
                    max_x = max(abs(px) for px, _ in pts) * item_scale + 5.0 * scale
                    max_y = max(abs(py) for _, py in pts) * item_scale + 5.0 * scale
                    for afx, afy in candidate_anchors:
                        x = width * afx
                        y = height * afy
                        if width > max_x * 2:
                            x = max(max_x, min(width - max_x, x))
                        if height > max_y * 2:
                            y = max(max_y, min(height - max_y, y))
                        item_rect = QRectF(x - max_x, y - max_y, max_x * 2.0, max_y * 2.0)
                        padded = item_rect.adjusted(-4.0*scale, -4.0*scale, 4.0*scale, 4.0*scale)
                        if padded.intersects(robot_guard) or padded.intersects(hand_exclusion):
                            continue
                        if any(overlap_fraction(padded, prev) > 0.34 for prev in placed_rects):
                            continue
                        chosen = (x, y, item_scale, padded)
                        break
                    if chosen is not None:
                        break

                if chosen is None:
                    # Extremely short robot stages can have almost no free area.
                    # Pick the least-obstructed outer anchor at a compact scale
                    # rather than dropping the constellation entirely.
                    item_scale = max(0.28 * scale, min(scales[slot], 0.46) * scale)
                    max_x = max(abs(px) for px, _ in pts) * item_scale + 4.0 * scale
                    max_y = max(abs(py) for _, py in pts) * item_scale + 4.0 * scale
                    best = None
                    for afx, afy in fallback_anchors:
                        x = max(max_x, min(width - max_x, width * afx)) if width > max_x * 2 else width * 0.5
                        y = max(max_y, min(height - max_y, height * afy)) if height > max_y * 2 else height * 0.5
                        rect = QRectF(x - max_x, y - max_y, max_x * 2.0, max_y * 2.0)
                        score = 0.0
                        if rect.intersects(robot_guard):
                            score += 50.0 + overlap_fraction(rect, robot_guard) * 100.0
                        if rect.intersects(hand_exclusion):
                            score += 25.0 + overlap_fraction(rect, hand_exclusion) * 50.0
                        score += sum(overlap_fraction(rect, prev) for prev in placed_rects) * 8.0
                        if best is None or score < best[0]:
                            best = (score, x, y, rect)
                    if best is not None:
                        _, x, y, padded = best
                        chosen = (x, y, item_scale, padded)

                if chosen is None:
                    continue
                x, y, item_scale, padded = chosen
                placed_rects.append(padded)
                c = QColor(glow); c.setAlpha(240)
                self._draw_constellation_pattern(
                    painter, x, y, item_scale, pts, edges, c, slot * 0.83,
                )

            # Small Lyra in the robot's hand remains a separate deliberate detail.
            hand_color = QColor(glow); hand_color.setAlpha(250)
            self._draw_constellation_pattern(
                painter, hand_x, hand_y, 1.02*scale, hand_points, hand_edges, hand_color, 6.5,
            )

        painter.restore()

    @staticmethod
    def _draw_gears(
        painter: QPainter, x: float, y: float, scale: float, color: QColor, phase: float
    ) -> None:
        """Draw a toothed gear silhouette instead of a wheel with spokes."""
        painter.save()
        painter.translate(x, y)
        painter.rotate(math.degrees(phase) * 1.8)
        teeth = 12
        outer = 27.0 * scale
        root = 20.0 * scale
        path = QPainterPath()
        for i in range(teeth * 4):
            a = (i / (teeth * 4.0)) * math.tau
            slot = i % 4
            radius = outer if slot in {1, 2} else root
            point = QPointF(math.cos(a) * radius, math.sin(a) * radius)
            if i == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        path.closeSubpath()
        gear = QColor(color); gear.setAlpha(215)
        painter.setBrush(gear)
        painter.setPen(QPen(color, max(1.0, 1.7 * scale)))
        painter.drawPath(path)
        hole = QColor("#101721"); hole.setAlpha(245)
        painter.setBrush(hole)
        painter.drawEllipse(QPointF(0, 0), 7.0 * scale, 7.0 * scale)
        painter.restore()
