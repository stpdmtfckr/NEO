# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import difflib
import json
import logging
import math
import os
import queue
import random
import re
import subprocess
import threading
import traceback
import time
import uuid
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Any


from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, QTimer, Qt, QThread, QThreadPool, Signal
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QCursor,
    QIcon,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QFontMetrics,
    QRegion,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizeGrip,
    QSizePolicy,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.agent import Agent, AgentTurn
from jarvis.config import ConfigManager
from jarvis.ui.audio_capture import AudioCaptureController
from jarvis.ui.overlay_manager import OverlayManager
from jarvis.overlay_state import drain_overlay_events
from jarvis.ui.chat_view import ChatView, MessageBubble
from jarvis.ui.robot_widget import RobotWidget
from jarvis.ui.reply_activity159 import ambient_reply_activity, briefing_reply_activity
from jarvis.ui.settings_dialog import SettingsDialog
from jarvis.voice import (
    FASTER_WHISPER_MODEL,
    RecordingResult,
    SpeechRecognizer,
    TranscriptionService,
)
from jarvis.wake_guard import WakeWordGuard
from jarvis.wake_detector import HybridWakeDetector
from jarvis.wake_matcher import match_wake_phrase, looks_wake_like, wake_alias_is_strong, wake_rescue_match, wake_alias_key, wake_alias_requires_standalone
from jarvis.quick_media_commands import QuickMediaCommand, parse_quick_media_command
from jarvis.runtime.quick_capture165 import QuickCapture, parse_quick_capture
from jarvis.integrations.bluetooth_devices import list_bluetooth_devices
from jarvis.integrations.mail_services import collect_statuses, format_mail_notification
from jarvis.windows_voice_unlock import attempt_voice_unlock, ensure_native_provider_registered, is_current_session_locked, native_provider_registered, phrase_matches as voice_unlock_phrase_matches
from jarvis.language import language_code, command_transcription_prompt
from jarvis.i18n import translate_widget_tree, localize_runtime_text, localized_source_name, tr
from jarvis.runtime.multilingual_commands import multilingual_hotwords, normalize_multilingual_command
from jarvis.workers import FunctionWorker
from jarvis.runtime.audio_focus import audio_focus_manager
from jarvis.runtime.cancellation import cancellation_manager
from jarvis.runtime.input_provenance import is_executable_input_source, normalize_input_source
from jarvis.runtime.discord_audio_meter import discord_session_meter
from jarvis.runtime.clipboard_history import clipboard_history
from jarvis.runtime.developer import developer_bus
from jarvis.runtime.latency import latency_profiler
from jarvis.ui.log_viewer import LogViewerWindow
from jarvis.runtime.media_events import drain_now_playing, publish_now_playing, is_player_facing_media_title
from jarvis.runtime.media_levels import media_levels
from jarvis.runtime.media_activity import recent_music_owner, recent_music_states, note_media_observation
from jarvis.integrations.music_provider import active_music_provider, provider_label
from jarvis.runtime.command_variations import repair_command_vocabulary
from jarvis.runtime.speech_understanding import SpeechUnderstandingEngine
from jarvis.runtime.timers import timer_manager
from jarvis.integrations.background.local_player import local_player
from jarvis.integrations.background.discord_bot import shutdown_all_discord_bots
from jarvis.runtime.ui_commands import drain_ui_commands
from jarvis.integrations.openweather import OpenWeatherAdapter
from jarvis.ui.desktop_events import drain_desktop_events
from jarvis.ui.desktop_strings import dt, install_catalogue

install_catalogue()

logger = logging.getLogger(__name__)


_COMMAND_HOTWORDS_BASE = (
    "открыть open browser chrome opera file файл папка folder "
    "приложение программа application app игра game exe executable launcher лаунчер "
    "sirius сириус minecraft майнкрафт darktide дарктайд elden ring wow warcraft "
    "play pause resume stop music muzyka muzika продолжи prodolzhi возобнови vozobnovi воспроизведение next previous skip пропусти propusti пропустить скипни музыка music видео "
    "следующий предыдущий дальше очередь очереди очисти очередь что играет сейчас играет приостанови останови сними с паузы "
    "яндекс yandex яндекс музыка моя волна мои треки мои песни плейлист playlist включи плейлист запусти плейлист "
    "лайк like дизлайк dislike трек песня композиция исполнитель альбом подборка "
    "discord discort diskord diskort дискорд дискорт сервер общая очередь совместная очередь моя волна в discord плейлист в discord включи песню в discord "
    "youtube ютуб ютьюб ролик видос "
    "текстовый канал голосовой канал войс voice dj bot музыкальный бот play slash mute unmute deafen "
    "громкость volume screen экран чеклист чек-лист панель окно монитор документ напиши "
    "gmail гугл почта mail.ru мейл.ру email письмо письма непрочитанные входящие отправитель "
    "добавь отметь сними удали очисти шрифт прозрачность цвет перетащи сессия билд"
)


def _all_configured_wake_terms(config: ConfigManager) -> list[str]:
    """Return every observed wake spelling for the active language.

    This is intentionally broader than the runtime immediate-accept list: old
    profiles contain useful observations such as ``There it is`` and ``Service``
    that should trigger a *verification* pass, never wake NEO directly.
    """
    values = [str(config.get("wake_word.phrase", "") or "").strip()]
    active = getattr(config, "active_wake_aliases", None)
    if callable(active):
        values.extend(active())
    else:
        values.extend(str(x).strip() for x in (config.get("wake_word.aliases", []) or []) if str(x).strip())
        scoped = config.get("wake_word.language_aliases", {}) or {}
        if isinstance(scoped, dict):
            lang_values = scoped.get(language_code(config), []) or []
            if isinstance(lang_values, str):
                lang_values = lang_values.splitlines()
            values.extend(str(x).strip() for x in lang_values if str(x).strip())
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = wake_alias_key(value)
        if key and key not in seen:
            seen.add(key)
            out.append(value)
    return out


@lru_cache(maxsize=48)
def _partition_wake_terms_cached(phrase: str, values: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Partition wake aliases once instead of running phonetic scoring per probe."""
    strong: list[str] = []
    rescue: list[str] = []
    for value in values:
        (strong if wake_alias_is_strong(value, phrase) else rescue).append(value)
    return tuple(strong), tuple(rescue)


def _configured_wake_terms(config: ConfigManager) -> list[str]:
    """Return only phonetic Jarvis-family aliases safe for instant wake."""
    phrase = str(config.get("wake_word.phrase", "") or "NEO").strip()
    strong, _rescue = _partition_wake_terms_cached(phrase, tuple(_all_configured_wake_terms(config)))
    return list(strong)


def _configured_wake_rescue_terms(config: ConfigManager) -> list[str]:
    """Return historical weak ASR spellings that require cross-language proof."""
    phrase = str(config.get("wake_word.phrase", "") or "NEO").strip()
    _strong, rescue = _partition_wake_terms_cached(phrase, tuple(_all_configured_wake_terms(config)))
    return list(rescue)


def _command_hotwords(config: ConfigManager) -> str:
    """Return compact command hints, never the full wake-word alias corpus.

    Wake aliases are useful only in the dedicated wake recognizer. Feeding dozens of
    Jarvis spellings into command Whisper consumes decoder context and can push
    faster-whisper/CTranslate2 past Whisper's 448-position decoder limit. Keep the
    command hints focused on actions, media destinations and the user's dictionary.
    The STT worker applies an additional token-aware safety cap before decoding.
    """
    try:
        from jarvis.runtime.dictionary import hotwords as dictionary_hotwords, installed_application_terms
        # User vocabulary is useful, but it must not crowd the finite Whisper
        # decoder prompt. Keep a small personal prefix and reserve most hint
        # capacity for stable action/domain words.
        personal_terms = [str(x).strip() for x in dictionary_hotwords(config) if str(x).strip()]
        personal = " ".join(personal_terms[:20])
        # A very small local-app slice gives Whisper the spelling of unusual
        # proper names without recreating the old 448-token prompt overflow.
        local_apps = " ".join(installed_application_terms(12))
    except Exception:
        personal = ""
        local_apps = ""
    lang = language_code(config)
    # Restore the broad pre-regression vocabulary distribution.  Phrase-specific
    # prompts over-biased Whisper toward a handful of media expressions and hurt
    # arbitrary speech.  Domain brands remain available through the normal
    # multilingual vocabulary and personal dictionary rather than dominating the
    # first decoder tokens.
    core = " ".join(multilingual_hotwords(lang, 88))
    neutral_questions = "какой какая какое какую какие какого какой какое какую" if lang == "ru" else ""
    return (personal + " " + local_apps + " " + core + " " + neutral_questions).strip()


def _rgba(color_value: str, opacity_percent: int) -> str:
    color = QColor(color_value)
    if not color.isValid():
        color = QColor("#000000")
    alpha = max(0, min(255, round(255 * opacity_percent / 100)))
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha})"


_SLEEP_STATUS182 = "\u0000neo-sleep-status"   # NEO 182: sentinel for the localized sleep status

class RoundedSurface(QFrame):
    """Opaque rounded surface with exactly one antialiased outline."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mainSurface")
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self._background = QColor("#0C1017")
        self._border = QColor("#57D7FF")
        self._radius = 26

    def set_theme(
        self,
        background: str,
        border: str,
        radius: int,
        *,
        background_opacity: int = 100,
        border_opacity: int = 100,
        transparent_background: bool = False,
        transparent_border: bool = False,
    ) -> None:
        background_color = QColor(background)
        border_color = QColor(border)
        if background_color.isValid():
            bg_percent = 0 if transparent_background else max(0, min(100, int(background_opacity)))
            background_color.setAlpha(round(255 * bg_percent / 100.0))
            self._background = background_color
        if border_color.isValid():
            border_percent = 0 if transparent_border else max(0, min(100, int(border_opacity)))
            border_color.setAlpha(round(255 * border_percent / 100.0))
            self._border = border_color
        self._radius = max(0, int(radius))
        self.update()

    def content_clip_path184(self) -> QPainterPath:
        """Keep child stage paint inside the outline, including antialiased edges."""
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(2.0, 2.0, -2.0, -2.0),
                            max(0.0, self._radius - 1.0), max(0.0, self._radius - 1.0))
        return path

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        # A translucent top-level window does not receive the normal system
        # background erase.  Explicitly clear the complete old frame first so
        # resize/repaint never leaves stale glyphs or coloured fragments behind.
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(self.rect(), Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        # Windows can hit-test fully transparent pixels of a layered frameless
        # window as if the application were not there.  The assistant window is
        # intentionally NEVER click-through (extra panels keep their own setting),
        # so at 0% background opacity paint a virtually invisible alpha=1 veil.
        # 1/255 is visually transparent but keeps the complete assistant rectangle
        # in the native input surface and restores dragging of the transparent top.
        if self._background.alpha() <= 0:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 1))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, self._radius, self._radius)
        painter.fillPath(path, self._background)
        if self._border.alpha() > 0:
            painter.setPen(QPen(self._border, 1.0))
            painter.drawPath(path)


def _set_qss180(widget, qss: str) -> None:
    """NEO 180: re-apply a style sheet only when it really changed.

    Every setStyleSheet() re-polishes the whole widget subtree — including an open Settings
    dialog parented to the main window — which made each «Apply» freeze the app for seconds
    on Windows although the theme had not changed at all."""
    if widget is None:
        return
    text = str(qss or "")
    # Hex colours arrive as #EAF2FF from defaults and #eaf2ff after a save: same style, no restyle.
    if widget.styleSheet().casefold() != text.casefold():
        widget.setStyleSheet(text)


# NEO 177: one icon language for every button of the main window (no emoji/glyph fonts).
def _paint_icon177(kind: str, color: QColor, accent: QColor | None = None, *, size: int = 64) -> QIcon:
    del size  # NEO 180: vector icons are drawn for the exact size and screen scale (icons180)
    from jarvis.ui.icons180 import icon
    return icon(kind, color, accent)


class MicToolButton(QToolButton):
    """The command-line microphone: «🎙» / «■» state texts are shown as vector icons."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icons177: tuple[QIcon, QIcon] | None = None

    def set_icons177(self, mic: QIcon, stop: QIcon) -> None:
        self._icons177 = (mic, stop)
        self.setText(self.text())

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        if self._icons177 is not None:
            self.setIcon(self._icons177[1] if text == "■" else self._icons177[0])
            self.setToolButtonStyle(Qt.ToolButtonIconOnly)


class StatusDot(QWidget):
    """NEO 177: a small light in front of the status text; its colour is the robot's state."""

    COLORS = {"idle": "#3DDC97", "sleeping": "#7C8AA0", "listening": "", "speaking": "", "typing": "",
              "thinking": "#FFB547", "working": "#FFB547", "searching": "#FFB547", "browsing": "#FFB547",
              "reading": "#FFB547", "scanning": "#FFB547", "coding": "#FFB547", "error": "#FF5B6A",
              "alert": "#FF8A3D", "sad": "#7C8AA0", "success": "#3DDC97", "happy": "#3DDC97"}

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor("#3DDC97")
        self._accent = QColor("#57D7FF")
        self._state = "idle"
        self.setFixedSize(12, 12)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def set_accent(self, color: str) -> None:
        candidate = QColor(color)
        if candidate.isValid():
            self._accent = candidate
            self.set_state(self._state)

    def set_state(self, state: str) -> None:
        self._state = str(state or "idle")
        value = self.COLORS.get(self._state, "#3DDC97")
        self._color = QColor(self._accent) if value == "" else QColor(value)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        halo = QColor(self._color); halo.setAlpha(60)
        painter.setPen(Qt.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.setBrush(self._color)
        inset = rect.width() * 0.27
        painter.drawEllipse(rect.adjusted(inset, inset, -inset, -inset))


class CommandLineEdit(QLineEdit):
    """Full-width command field with shell-like command history."""

    historyUpRequested = Signal()
    historyDownRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mic_button = MicToolButton(self)
        self.mic_button.setObjectName("inputMicButton")
        self.mic_button.setText("🎙")
        self.mic_button.setCursor(Qt.PointingHandCursor)
        self.mic_button.setToolTip("Голосовой ввод / остановить запись")
        self.send_button = QToolButton(self)
        self.send_button.setObjectName("inputSendButton")
        self.send_button.setText("↑")
        self.send_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.send_button.setCursor(Qt.PointingHandCursor)
        self.send_button.setToolTip("Отправить сообщение")
        # NEO 177: a comfortable pill-shaped field (34 px at 100%) with round buttons.
        self.setFixedHeight(34)
        self._send_icons177: tuple[QIcon, QIcon] | None = None
        self.textChanged.connect(self._sync_send_state177)
        self._place_buttons()

    def _sync_send_state177(self, text: str = "") -> None:
        active = bool(str(text or self.text()).strip())
        if self.send_button.property("active") != active:
            self.send_button.setProperty("active", active)
            self.send_button.style().unpolish(self.send_button)
            self.send_button.style().polish(self.send_button)
        if self._send_icons177:
            self.send_button.setIcon(self._send_icons177[1 if active else 0])

    def set_icons177(self, mic: QIcon, stop: QIcon, send_idle: QIcon, send_active: QIcon) -> None:
        self.mic_button.set_icons177(mic, stop)
        self._send_icons177 = (send_idle, send_active)
        self._sync_send_state177()

    def _place_buttons(self) -> None:
        side = max(20, min(30, self.height() - 6))
        gap = 2
        right = 3
        cy = max(1, (self.height() - side) // 2)
        self.send_button.setGeometry(max(0, self.width() - right - side), cy, side, side)
        self.mic_button.setGeometry(max(0, self.width() - right - side * 2 - gap), cy, side, side)
        self.setTextMargins(12, 0, side * 2 + gap + right + 4, 0)
        self.mic_button.setIconSize(QSize(max(14, int(side * 0.72)), max(14, int(side * 0.72))))
        self.send_button.setIconSize(QSize(max(14, side - 4), max(14, side - 4)))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_buttons()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Up and not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)):
            self.historyUpRequested.emit()
            event.accept()
            return
        if event.key() == Qt.Key_Down and not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)):
            self.historyDownRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


from jarvis.ui.response_output import ResponseOutputMixin



def _owner_setting(owner: Any, key: str, default: Any) -> Any:
    config = getattr(owner, "config", None)
    getter = getattr(config, "get", None)
    return getter(key, default) if callable(getter) else default


def _owner_language(owner: Any) -> str:
    language = getattr(owner, "_language_code", None)
    return str(language() if callable(language) else _owner_setting(owner, "app.language", "ru") or "ru")


def _quick_capture_from(owner: Any, text: str) -> QuickCapture | None:
    """Wake-less «клип / запись / стоп запись / скриншот», when enabled in Settings."""
    if not bool(_owner_setting(owner, "wake_word.quick_capture_enabled", False)):
        return None
    try:
        return parse_quick_capture(text, _owner_language(owner))
    except Exception:
        logger.debug("Quick capture parse failed", exc_info=True)
        return None


def _correct_heard_words(owner: Any, text: str) -> str:
    """«превет» -> «привет»: misheard words of the selected language, UI-thread safe."""
    if not bool(_owner_setting(owner, "voice.spoken_spelling_enabled", True)):
        return text
    try:
        from jarvis.runtime.spoken_spelling165 import correct_transcript
        corrected, changes = correct_transcript(text, _owner_language(owner))
    except Exception:
        logger.debug("Spoken spelling correction failed", exc_info=True)
        return text
    if changes:
        logger.info("Misheard words corrected: %s", list(changes))
        developer_bus().emit("speech_understanding", "spelling", data={"raw": text, "text": corrected, "changes": [list(x) for x in changes]})
    return corrected

def _reveal187(owner):
    """One briefing reveal cursor per window (TZ-SUMMARY-01)."""
    reveal = getattr(owner, "_briefing_reveal187", None)
    if reveal is None or getattr(reveal, "_chat", None) is not owner.chat:
        from jarvis.ui.briefing_reveal187 import GrowingReveal
        reveal = GrowingReveal(owner.chat, owner.config, owner)
        owner._briefing_reveal187 = reveal
    return reveal


def _gui_parse_quick_media_command188(text: str, code: str):
    """Quick-media parsing on the Qt thread never builds a cold catalogue (HIST-023)."""
    from jarvis.runtime.multilingual_commands import gui_fast_path
    with gui_fast_path():
        return parse_quick_media_command(text, code)


def _release_quick_media_transport188(window, serial: int | None) -> None:
    """Release the quick-media transport admission only when this callback owns it (HIST-007)."""
    owner = getattr(window, "_quick_media_transport_token188", None)
    if serial is not None and owner is not None and owner != serial:
        return
    window._quick_media_transport_inflight = False
    window._quick_media_transport_action = ""
    window._quick_media_transport_token188 = None


class MainWindow(ResponseOutputMixin, QMainWindow):
    desktop_service_event_signal = Signal(dict)
    telegram_event_signal = Signal(str)
    game_event_signal = Signal(str)
    match_notice_signal = Signal(str)
    scenario_event_signal184 = Signal(dict)
    stt_switch_signal186 = Signal(int, str, str, str)

    def __init__(self, config: ConfigManager) -> None:
        super().__init__()
        self.config = config
        from jarvis.ui.qt_localization import install_ui_language
        install_ui_language(QApplication.instance(), self._language_code())
        self.telegram_event_signal.connect(self._on_telegram_event)
        self.game_event_signal.connect(self._on_game_event)
        self.match_notice_signal.connect(self._on_match_notice)
        self.log_viewer = LogViewerWindow(self.config.paths.logs / "neo_jarvis.log", self, language=self._language_code())
        self.agent = Agent(config)
        # Smart-speaker-like ASR post-processing.  This stays outside Agent so
        # typed chat text is never silently "corrected" as speech.
        self.speech_understanding = SpeechUnderstandingEngine(config, agent=self.agent)
        # Misheard-word correction data loads in a background thread after the UI
        # is up, so neither startup nor animations wait for it.
        QTimer.singleShot(6000, self._warm_spoken_spelling)
        self.thread_pool = QThreadPool.globalInstance()
        from jarvis.ui.window_geometry import CaptionDrag
        self._caption_drag = CaptionDrag(self)
        # Dedicated lane cannot wait behind background metadata/network jobs.
        self._quick_media_pool = QThreadPool(self)
        self._quick_media_pool.setMaxThreadCount(1)
        self._quick_media_pool.setExpiryTimeout(-1)
        # Capture text clipboard changes even when they happened outside NEO so
        # phrases like “вставь то, что я копировал два раза назад” are real history.
        clipboard_history().start_monitor()
        # Command and wake-word recognition use separate native processes. This
        # prevents permanent wake listening from blocking a high-quality command
        # pass and lets each process keep the best model for its job in memory.
        self.transcription_service = TranscriptionService("NeoJarvis-STT-Command")
        self.wake_transcription_service = TranscriptionService("NeoJarvis-STT-Wake")
        # Provisional tiny transcripts share the tiny lane, never the final
        # command model/lock. Finished speech has its own native decoder.
        self.partial_transcription_service = self.wake_transcription_service
        from jarvis.ui.command_asr157 import CommandAsrRunner
        self._command_asr_runner157 = CommandAsrRunner(self)
        self._command_asr_pending157 = False
        self.recognizer = SpeechRecognizer(
            config,
            self.transcription_service,
            purpose="command",
            initial_prompt=command_transcription_prompt(language_code(self.config)),
            hotwords=_command_hotwords(self.config),
        )
        self.partial_recognizer = SpeechRecognizer(
            config,
            self.partial_transcription_service,
            model_name=str(self.config.get("voice.partial_stt_model", "tiny")),
            purpose="partial",
        )
        self.wake_recognizer = SpeechRecognizer(
            config,
            self.wake_transcription_service,
            model_name=str(self.config.get("wake_word.stt_model", "tiny")),
            purpose="wake",
            # IMPORTANT: do not bias Whisper toward the wake word. Hotwords and an
            # initial Jarvis prompt made RC11 hallucinate "Jarvis" from unrelated
            # speech/noise. Precision comes from the strict matcher below.
            initial_prompt="",
            hotwords="",
        )
        # HOTFIX43.1 STABILITY: reuse the already-warm, crash-isolated tiny wake
        # recognizer for short transport commands instead of loading a second tiny
        # model in the command STT process. This keeps the fast lane while removing
        # the startup/native-memory spike seen in field logs after HOTFIX43.
        self.fast_media_recognizer = self.wake_recognizer
        self._stt_preload_thread: threading.Thread | None = None
        self._ollama_warmup_inflight = False
        self._ollama_warmup_defer_count = 0
        self.audio_capture = AudioCaptureController(config, self)
        self.audio_capture.finished.connect(self._audio_finished)
        self.audio_capture.partial_ready.connect(self._audio_partial_ready)
        self.audio_capture.wake_probe_ready.connect(self._audio_wake_probe_ready)
        self.audio_capture.wake_chunk_ready.connect(self._audio_wake_chunk_ready)
        self.audio_capture.failed.connect(self._audio_failed)
        self.audio_capture.recovered.connect(self._audio_recovered)
        self.audio_capture.level_changed.connect(self._audio_level_changed)
        self.audio_capture.started.connect(self._audio_started)

        self._busy = False
        self._voice_mode = ""
        self._continuous_voice_session = False
        self._wake_generation = 0
        self._drag_start_global: QPoint | None = None
        self._drag_start_window: QPoint | None = None
        self._chat_resize_start_global_y: int | None = None
        self._chat_resize_start_height: int | None = None
        self._quitting = False
        self._full_shutdown_requested = False
        self._shutdown_watchdog_event = threading.Event()
        self._shutdown_watchdog_started = False
        self._shutdown_incomplete = False
        self._tray_notice_shown = False
        self._scale = 1.0
        self._last_response_state = "idle"
        self._pending_response_bubble: MessageBubble | None = None
        self._listening_bubble: MessageBubble | None = None
        from PySide6.QtGui import QShortcut
        self._speech_stop_shortcut = QShortcut(QKeySequence('Escape'), self)
        self._speech_stop_shortcut.activated.connect(lambda: self._interrupt_current_operation(source='escape') if self._response_active() else None)
        self._stream_text = ""
        self._stream_pending_text = ""
        self._stream_thinking_text = ""
        self._stream_received = False
        self._thinking_received = False
        self._agent_generation = 0
        self._pending_media_results157 = {}
        self._recovered_media_notices157 = set()
        self._interrupt_wake_active = False
        self._listening_ducked = False
        self._typing_text = ""
        self._typing_index = 0
        self._typing_final_state = "idle"
        self._typing_stream_live = False
        self._typing_stream_complete = False
        self._partial_generation = 0
        self._partial_worker_active = False
        self._pending_partial_recording: RecordingResult | None = None
        self._wake_worker_active = False
        self._wake_probe_worker_active = False
        self._pending_wake_recording: tuple[RecordingResult, int] | None = None
        # The audio process may emit a better/later rolling wake slice while the
        # first tiny-Whisper probe is still decoding. Keep exactly one freshest
        # probe instead of dropping it; this is the latency-critical HOTFIX142
        # queue and remains strictly serialized, so CPU use cannot fan out.
        self._pending_wake_probe_recording: tuple[RecordingResult, int, bool] | None = None
        # Wake ASR deliberately avoids Qt's global QThreadPool. On some Windows
        # machines the short-lived QRunnable/Signal lifetime around a successful
        # wake transition correlated with native 0xC0000005 exits. A plain Python
        # thread talks only to the isolated STT subprocess and posts data into this
        # queue; Qt consumes it from a timer on the UI thread.
        self._wake_result_queue: queue.Queue[tuple[str, Any, int, RecordingResult | None]] = queue.Queue(maxsize=32)
        self._wake_thread: threading.Thread | None = None
        self._wake_command_ui_pending = False
        self._last_wake_reject_log_at = 0.0
        self._wake_rescue_pending_generation = -1
        self._wake_rescue_pending_at = 0.0
        self._last_quick_media_key = ""
        self._last_quick_media_at = 0.0
        self._last_quick_media_semantic = ""
        self._last_quick_media_semantic_at = 0.0
        self._quick_media_transport_inflight = False
        self._quick_media_transport_action = ""
        self._post_interrupt_require_wake = False
        self._discord_observer_inflight = False
        self._discord_observer_last_key = ""
        self._discord_observer_last_seen = 0.0
        self._discord_cold_attach_attempt = 0
        self._discord_cold_attach_done = False
        self._last_now_playing_by_source: dict[str, str] = {}
        # User-initiated media commands already produce one authoritative tool
        # answer containing now-playing + queue.  Suppress the asynchronous media
        # event bubble during that command so My Wave / My Tracks / playlists /
        # Discord play can never render the same state twice.
        self._media_notice_suppressed_until: dict[str, float] = {}
        self._active_user_command_text = ""
        self._music_overlay_last_event: dict[str, Any] = {}
        self._music_overlay_state_cache: dict[str, Any] = {}
        self._music_overlay_state_cache_at = 0.0
        self._app_mic_muted = False
        self._mic_mute_restore_continuous = False
        self._mic_hotkey_registered = False
        self._mic_hotkey_local: QShortcut | None = None
        self._mic_hotkey_id = 0x4A13
        self._aux_hotkey_registered: set[int] = set()
        self._aux_hotkey_local: list[QShortcut] = []
        self._autoclicker_hotkey_id = 0x4A14
        self._chat_scroll_up_hotkey_id = 0x4A15
        self._chat_scroll_down_hotkey_id = 0x4A16
        self._window_topmost_hotkey_id = 0x4A17
        self._shelf_hotkey_id = 0x4A18
        self.bookshelf = None
        self._mini_browser = None
        self._image_viewers = []
        self._seen_desktop_events = set()
        self._seen_desktop_event_order = deque(maxlen=512)
        self._desktop_event_bubbles = {}
        self.desktop_service_event_signal.connect(self._desktop_service_event)
        self.stt_switch_signal186.connect(self._stt_switch_finished186)
        # Runtime callbacks are producer-thread events; the Qt signal owns UI dispatch.
        import weakref
        from jarvis.runtime.scenarios import subscribe_scenarios, unsubscribe_scenarios
        scenario_owner = weakref.ref(self)
        def scenario_callback(event):
            owner = scenario_owner()
            if owner is not None:
                try:
                    owner.scenario_event_signal184.emit(event)
                except RuntimeError:
                    pass
        self._scenario_callback184 = scenario_callback
        self._pending_scenario_status184 = None
        self._scenario_status_timer184 = QTimer(self)
        self._scenario_status_timer184.setSingleShot(True)
        self._scenario_status_timer184.setInterval(33)
        self._scenario_status_timer184.timeout.connect(self._flush_scenario_status184)
        self.scenario_event_signal184.connect(self._scenario_event184)
        subscribe_scenarios(scenario_callback)
        self.destroyed.connect(lambda *_: unsubscribe_scenarios(scenario_callback))
        self._runtime_topmost = False
        self._live_user_bubble: MessageBubble | None = None
        self._sleeping = False
        self._idle_action_generation = 0
        self._heard_target_text = ""
        self._heard_display_text = ""
        self._wake_guard = WakeWordGuard()
        # NEO 168: skip redundant wake decodes during long nearby conversations.
        from jarvis.runtime.wake_budget168 import ConversationBackoff
        self._wake_backoff = ConversationBackoff(enabled=bool(self.config.get("wake_word.conversation_backoff", True)))
        self._acoustic_wake_disabled = os.environ.get(
            "NEO_NEO_DISABLE_ACOUSTIC_WAKE", ""
        ).strip() == "1"
        self._hybrid_wake_detector = HybridWakeDetector()

        self._typing_timer = QTimer(self)
        # Initial interval is immediately synchronized with the global 30/60/120
        # visual FPS in _apply_settings(). Fast character speeds reveal multiple
        # characters per frame, preserving characters/second without extra repaints.
        self._typing_timer.setInterval(8)
        self._typing_timer.timeout.connect(self._typewriter_tick)

        # Non-Agent messages (system notifications, Telegram mirror, Bluetooth,
        # now-playing notices, quick-media acknowledgements, startup briefing)
        # use a second serialized typewriter lane.  Previously _append_assistant()
        # bypassed the configured character streaming and dumped those messages
        # instantly while normal Agent answers typed gradually.
        self._ambient_typing_queue: deque[tuple[MessageBubble, str]] = deque()
        self._ambient_typing_bubble: MessageBubble | None = None
        self._ambient_typing_text = ""
        self._ambient_typing_index = 0
        self._ambient_typing_timer = QTimer(self)
        self._ambient_typing_timer.setInterval(8)
        self._ambient_typing_timer.timeout.connect(self._ambient_typewriter_tick)
        # V4.8 streaming repaint throttle. Tokens may arrive much faster than a
        # human-visible frame rate; batching them avoids a QText/layout repaint per
        # token while preserving immediate first-token feedback.
        self._stream_flush_timer = QTimer(self)
        animation_fps = int(self.config.get("ui.animation_fps", 60) or 60)
        if animation_fps not in {30, 60}:
            animation_fps = 60
        self._stream_flush_timer.setInterval({30: 33, 60: 16}[animation_fps])
        self._stream_flush_timer.timeout.connect(self._flush_response_stream)
        self._heard_timer = QTimer(self)
        # Initial cadence is synchronized with the global 30/60 visual FPS
        # in _apply_settings(); it never changes ASR or command dispatch cadence.
        self._heard_timer.setInterval({30: 33, 60: 16}[animation_fps])
        self._heard_timer.timeout.connect(self._heard_text_tick)
        # Audio analysis does NOT run at UI FPS. The backend publishes bounded
        # meter targets; this lightweight Qt interpolator restores smooth 30/60
        # visual motion without multiplying DSP/Whisper work.
        self._mic_level_target = 0.0
        self._mic_level_rendered = 0.0
        self._mic_meter_timer = QTimer(self)
        # The audio meter deliberately renders at 30 FPS even when robot/global
        # animation is 60 FPS.  Audio publishes only ~12 Hz wake targets and
        # this cheap interpolation keeps it fluid without tying CPU use to 120 FPS.
        self._mic_meter_timer.setInterval(33)
        self._mic_meter_timer.timeout.connect(self._mic_meter_smooth_tick)
        self._speech_timer = QTimer(self)
        self._speech_timer.setSingleShot(True)
        self._speech_timer.timeout.connect(self._finish_visual_speaking)
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(700)
        self._geometry_timer.timeout.connect(self._save_geometry)
        self._style_scale_timer = QTimer(self)
        self._style_scale_timer.setSingleShot(True)
        self._style_scale_timer.setInterval(32)
        self._style_scale_timer.timeout.connect(self._refresh_main_style)
        self._theme_cache: dict[str, Any] = {}
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(45_000)
        self._idle_timer.timeout.connect(self._go_sleepy)
        self._idle_action_timer = QTimer(self)
        self._idle_action_timer.setSingleShot(True)
        self._idle_action_timer.timeout.connect(self._play_idle_action)
        # Self-healing wake watchdog. If an audio stop/start event, model worker
        # completion or safe-mode recovery leaves wake listening disarmed, restore
        # it automatically instead of silently ignoring every later "Джарвис".
        self._wake_watchdog = QTimer(self)
        self._wake_watchdog.setTimerType(Qt.CoarseTimer)
        self._wake_watchdog.setInterval(900)
        self._wake_watchdog.timeout.connect(self._ensure_wake_monitor)
        self._wake_watchdog.start()
        self._wake_result_timer = QTimer(self)
        # This timer is armed only while wake-ASR work is in flight, so an 8 ms
        # precise poll costs essentially nothing while idle but removes up to a
        # frame of avoidable latency between a tiny-Whisper hit and the visible
        # "Слушаю" acknowledgement.
        self._wake_result_timer.setTimerType(Qt.PreciseTimer)
        self._wake_result_timer.setInterval(8)
        self._wake_result_timer.timeout.connect(self._poll_wake_results)
        # HOTFIX8: do not wake the Qt thread 28 times/s while idle. This timer is
        # armed only while a wake-ASR worker can actually place results in queue.

        self.setWindowTitle("NEO")
        taskbar_icon = Path(__file__).resolve().parents[2] / "assets" / "neo_jarvis.ico"
        if taskbar_icon.exists():
            self.setWindowIcon(QIcon(str(taskbar_icon)))
        # Keep one ordinary taskbar HWND with native minimize capability even
        # though its caption is custom. Explorer owns taskbar toggle behavior.
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
                            | Qt.WindowSystemMenuHint | Qt.WindowMinimizeButtonHint)
        # The assistant itself must always own mouse input.  Click-through is a
        # feature of OverlayPanel only; never inherit it on the main robot window.
        self.setWindowFlag(Qt.WindowTransparentForInput, False)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        # The top-level window supports real per-pixel alpha in v4.0.15.
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setAutoFillBackground(False)
        self.setWindowOpacity(1.0)
        self.setMinimumSize(320, 360)

        # UI event filters can receive child/layout events while _build_ui() is
        # still constructing widgets. Keep resize/filter callbacks inert until
        # the complete widget tree exists (HOTFIX1 for the PySide6 NULL layout
        # startup crash seen on Windows).
        self._ui_ready = False
        self._build_ui()
        self._ui_ready = True
        QTimer.singleShot(1200, self._announce_settings_recovery183)
        QTimer.singleShot(1800, self, self._offer_default_model184)
        from jarvis.ui.responsiveness158 import UiResponsivenessMonitor
        self._responsiveness_monitor158 = UiResponsivenessMonitor(self)
        self._create_tray()
        self._apply_language()
        # OverlayManager owns top-level transparent information/checklist panels.
        # Agent tools only write their model; this timer performs every Qt change
        # on the GUI thread, which keeps voice/tool workers crash-safe.
        self.overlay_manager = OverlayManager(
            self.config, music_state_provider=self._music_overlay_state, media_action=self._overlay_media_action
        )
        self._overlay_timer = QTimer(self)
        self._overlay_timer.setTimerType(Qt.CoarseTimer)
        self._overlay_timer.setInterval(250)
        self._overlay_timer.timeout.connect(self._poll_overlay_events)
        self._overlay_timer.start()
        # Track-start notifications are event-driven by integrations; this cheap
        # coarse timer only drains an in-process queue and never polls media apps.
        self._media_event_timer = QTimer(self)
        self._media_event_timer.setTimerType(Qt.CoarseTimer)
        self._media_event_timer.setInterval(900)
        self._media_event_timer.timeout.connect(self._drain_media_now_playing_events)
        self._media_event_timer.start()
        # Discord Gateway is an observer/control connection, not a voice join.
        # Prewarm it at startup so this NEO sees a My Wave that another NEO
        # started before we ever issued a Discord command. A slow passive status
        # poll mirrors shared now-playing metadata into the normal media event bus
        # (and therefore Telegram) without touching Discord UI.
        self._discord_observer_timer = QTimer(self)
        self._discord_observer_timer.setTimerType(Qt.CoarseTimer)
        self._discord_observer_timer.setInterval(5000)
        self._discord_observer_timer.timeout.connect(self._poll_discord_observer)
        self._discord_observer_timer.start()
        QTimer.singleShot(1200, self._prewarm_discord_async)
        # Commands originating from worker/tool threads must never touch QWidget
        # directly. Drain the tiny in-process UI queue on the Qt thread.
        self._ui_command_timer = QTimer(self)
        self._ui_command_timer.setTimerType(Qt.CoarseTimer)
        self._ui_command_timer.setInterval(80)
        self._ui_command_timer.timeout.connect(self._drain_ui_commands)
        self._ui_command_timer.start()
        # HOTFIX47: robot-stage information is fed from already available
        # in-process media state. Weather is refreshed separately on a slow worker
        # so paintEvent and the GUI thread never perform network I/O.
        self._robot_weather_state: dict[str, Any] = {}
        self._robot_weather_inflight = False
        self._robot_info_timer = QTimer(self)
        self._robot_info_timer.setTimerType(Qt.CoarseTimer)
        self._robot_info_timer.setInterval(650)
        self._robot_info_timer.timeout.connect(self._refresh_robot_stage_info)
        self._robot_info_timer.start()
        self._robot_weather_timer = QTimer(self)
        self._robot_weather_timer.setTimerType(Qt.CoarseTimer)
        self._robot_weather_timer.setInterval(60 * 60 * 1000)
        self._robot_weather_timer.timeout.connect(self._refresh_robot_temperature_async)
        self._robot_weather_timer.start()
        # Bluetooth notifications are deliberately slow-polled in a worker. The
        # 30 s cadence is enough for connect/battery warnings and has no impact
        # on microphone/render latency.
        self._bluetooth_snapshot: dict[str, dict[str, Any]] = {}
        self._bluetooth_battery_warned: set[str] = set()
        self._bluetooth_monitor_inflight = False
        self._bluetooth_monitor_timer = QTimer(self)
        self._bluetooth_monitor_timer.setTimerType(Qt.VeryCoarseTimer)
        self._bluetooth_monitor_timer.setInterval(30000)
        self._bluetooth_monitor_timer.timeout.connect(self._poll_bluetooth_devices_async)
        self._bluetooth_monitor_timer.start()
        QTimer.singleShot(4200, self._poll_bluetooth_devices_async)
        # Gmail/Mail.ru polling is network-bound and always runs through the worker
        # pool. The Qt timer is very coarse and only starts one bounded check at a
        # time, so mail integration cannot block microphone capture or rendering.
        self._mail_monitor_inflight = False
        self._mail_seen_ids: dict[str, set[str]] = {}
        self._mail_seen_accounts: dict[str, str] = {}
        self._mail_monitor_timer = QTimer(self)
        self._mail_monitor_timer.setTimerType(Qt.VeryCoarseTimer)
        self._mail_monitor_timer.timeout.connect(self._poll_mail_async)
        self._configure_mail_monitor_timer()
        QTimer.singleShot(8000, self._poll_mail_async)
        # Persistent timers/reminders run in their own sleeping daemon thread; this
        # coarse Qt timer only drains already-produced notification events.
        self._timer_manager = timer_manager(self.config.paths.root, self._language_code())
        try:
            # NEO 172: bounded cache and old settings copies — once a day, in the background.
            from jarvis.runtime import housekeeping172
            housekeeping172.start_background(self.config)
            from jarvis.runtime import model_life173
            if getattr(getattr(self, "agent", None), "ollama", None) is not None:
                model_life173.register(self.agent.ollama)
            # NEO 173: a quiet self-check a minute after start; a notice only when something needed is missing.
            QTimer.singleShot(60_000, self._startup_selfcheck173)
            QTimer.singleShot(8_000, self._warm_up174)
        except Exception:
            logger.debug("Housekeeping could not start", exc_info=True)
        self._timer_event_timer = QTimer(self)
        self._timer_event_timer.setTimerType(Qt.PreciseTimer)
        self._timer_event_timer.setInterval(1000)
        self._timer_event_timer.timeout.connect(self._poll_timer_events_and_hud)
        self._timer_event_timer.start()
        self._restore_geometry()
        self._apply_settings(first_show=True)
        self._audio_settings_signature = self._speech_settings_signature()
        self._apply_language()
        # HOTFIX94: do not prepend a generic readiness bubble. When the configured
        # startup briefing is enabled, it is the first visible assistant message.
        # Wake STT gets first startup priority. A warm tiny recognizer is what makes
        # the physical "Jarvis" -> "Слушаю" transition feel immediate. Ollama is
        # still preloaded, but its heavyweight model load is coordinated so it does
        # not compete with the first wake-model initialization.
        QTimer.singleShot(35, self._preload_stt_async)
        if bool(self.config.get("ollama.requests_enabled", False)):
            QTimer.singleShot(90, self._check_ollama_async)
        if bool(self.config.get("briefing.enabled", True)) and bool(self.config.get("briefing.show_on_startup", True)):
            QTimer.singleShot(0, self._start_morning_briefing)
        self._restart_idle_timer()
        QTimer.singleShot(0, self._register_app_mic_hotkey)
        QTimer.singleShot(0, self._register_aux_hotkeys)
        QTimer.singleShot(1800, self._ensure_voice_unlock_provider_async)
        # Only the lightweight shelf store/editor widgets load here; Chromium and
        # GIF decoding stay demand-driven. Pinned notes survive a restart.
        QTimer.singleShot(2200, self._ensure_bookshelf)
        QTimer.singleShot(2500, self._show_activity_notice)
        self._resume_wake_listening(delay_ms=40)

    def _ensure_voice_unlock_provider_async(self) -> None:
        """Install HOTFIX47's native unlock provider once for existing profiles.

        New configurations install it directly when Settings are saved.  This
        startup path primarily migrates users who already enabled voice unlock
        in HOTFIX46.  Never request elevation while the workstation is locked.
        """
        if os.name != "nt" or not bool(self.config.get("voice_unlock.enabled", False)):
            return
        if not bool(self.config.get("voice_unlock.native_provider_preferred", True)):
            return
        if bool(getattr(self, "_voice_unlock_provider_install_inflight", False)):
            return
        try:
            if native_provider_registered() or is_current_session_locked():
                return
        except Exception:
            logger.debug("Voice unlock provider readiness check failed", exc_info=True)
            return
        self._voice_unlock_provider_install_inflight = True
        worker = FunctionWorker(lambda: ensure_native_provider_registered(elevate=True))
        worker.signals.result.connect(self._voice_unlock_provider_install_finished)
        worker.signals.error.connect(self._voice_unlock_provider_install_failed)
        self.thread_pool.start(worker)

    def _voice_unlock_provider_install_finished(self, result: Any) -> None:
        self._voice_unlock_provider_install_inflight = False
        ok = bool(getattr(result, "ok", False))
        status = str(getattr(result, "status", "") or "")
        if ok:
            logger.info("Voice unlock Credential Provider ready: %s", status)
        else:
            logger.warning("Voice unlock Credential Provider setup incomplete: %s", status)

    def _voice_unlock_provider_install_failed(self, trace: str) -> None:
        self._voice_unlock_provider_install_inflight = False
        logger.warning("Voice unlock Credential Provider setup failed: %s", trace)

    def _start_interrupt_wake_monitor(self, generation: int) -> None:
        if getattr(self, "_wake_candidate_pending158", False):
            return  # The same input stream is retaining the post-address command.
        if self._app_mic_muted:
            return
        if generation != self._agent_generation or not self._response_active() or self._quitting:
            return
        if not bool(self.config.get("voice.enabled", True)):
            return
        if self.audio_capture.is_recording:
            if self._voice_mode in {'wake', 'interrupt_wake'}:
                self._voice_mode = 'interrupt_wake'
                self._interrupt_wake_active = True
            return
        self._interrupt_wake_active = True
        self._voice_mode = "interrupt_wake"
        self.audio_capture.start_wake_monitor(
            window_seconds=min(1.1, float(self.config.get("wake_word.chunk_seconds", 0.95))),
            emit_interval=min(0.12, float(self.config.get("wake_word.silence_seconds", 0.08))),
        )
        logger.info("Barge-in monitor armed while agent generation=%s is busy", generation)

    def _interrupt_current_operation(self, *, command: str = "", source: str = "voice") -> None:
        response_voice = getattr(self, "_response_voice_service", None)
        if response_voice is not None:
            response_voice.cancel()
        self._cancel_command_asr157()
        cancellation_manager().cancel(f"Прервано пользователем ({source})")
        cancel_briefing = getattr(self, '_cancel_morning_briefing158', None)
        if callable(cancel_briefing):
            cancel_briefing()
        else:
            self._briefing_generation = getattr(self, '_briefing_generation', 0) + 1
            self._briefing_active = False
            event = getattr(self, '_briefing_cancel', None)
            if event is not None:
                event.set()
        self._ambient_typing_timer.stop()
        self._ambient_typing_queue.clear()
        self._ambient_typing_bubble = None
        self._ambient_typing_text = ''
        self._ambient_typing_index = 0
        try:
            cancelled_streams = int(self.agent.ollama.cancel_active())
            if cancelled_streams:
                logger.info("Cancelled %s active Ollama stream(s) after user interrupt", cancelled_streams)
        except Exception:
            logger.debug("Could not close active Ollama stream during interrupt", exc_info=True)
        self._typing_timer.stop()
        self._typing_stream_live = False
        self._typing_stream_complete = False
        self._stream_flush_timer.stop()
        self._stream_pending_text = ""
        self._agent_generation += 1  # stale worker output/results are ignored
        self._wake_generation += 1
        self._pending_wake_recording = None
        self._pending_wake_probe_recording = None
        self._wake_worker_active = False
        self._interrupt_wake_active = False
        if self.audio_capture.is_recording:
            self.audio_capture.stop(emit_result=False)
        self._voice_mode = ""
        self._busy = False
        self._continuous_voice_session = False
        self.input.setEnabled(True)
        self.send_button.setEnabled(True)
        self.settings_button.setEnabled(True)
        cancelled_bubble = self._pending_response_bubble
        if cancelled_bubble is not None:
            # Freeze exactly the visible prefix. Do not replace it, flush hidden
            # deltas, or complete the old typewriter after cancellation.
            self.chat.collapse_thinking(self._pending_response_bubble)
            self._pending_response_bubble = None
        response_had_visible_text = bool(getattr(self, '_stream_received', False) or getattr(self, '_typing_index', 0))
        self._typing_text = ''
        self._typing_index = 0
        self._stream_text = ''
        self._active_user_command_text = ''
        from jarvis.runtime.answer_stop179 import STOPPED
        stopped = STOPPED.get(self._language_code(), STOPPED['en'])
        briefing = getattr(self, '_response_briefing158', None)
        if briefing is not None and cancelled_bubble is not None:
            briefing.finish(cancelled=True)
            self.chat.update_bubble(cancelled_bubble, briefing.render())
        visible = getattr(cancelled_bubble, 'text', None)
        if isinstance(visible, str):
            # Keep the same response row and its visible prefix. Hidden stream
            # deltas are discarded; an unresolved thinking label is replaced.
            placeholder = visible.strip().casefold().rstrip('.…') in {
                '', 'думаю', 'думает', 'thinking', 'слушаю', 'listening'
            }
            placeholder = placeholder or (
                not response_had_visible_text
                and getattr(self, '_response_briefing158', None) is None
            )
            self.chat.update_bubble(cancelled_bubble, stopped if placeholder else visible + '\n\n' + stopped)
        else:
            self._append_assistant(stopped, paced=False)
        self._response_briefing158 = None
        reply_activity = getattr(self.robot, 'set_reply_activity', None)
        if callable(reply_activity):
            reply_activity(False)
        value = self._normalize_recognized_command(str(command or "").strip())
        if value and not self._is_interrupt_phrase(value):
            # Wake+replacement (or text replacement) is intentional: cancel the
            # stale task and immediately run the replacement.
            self._post_interrupt_require_wake = False
            self._append_user(value)
            input_source = "text" if str(source).startswith("text") else "voice"
            QTimer.singleShot(0, lambda text=value, src=input_source: self._send_text(text, user_already_shown=True, input_source=src))
            return
        # A bare Stop while NEO is busy is a hard conversation cancel, not a
        # media pause. Drop continuous command capture and return to wake-only
        # standby. The microphone still runs the lightweight wake detector, but no
        # following speech becomes a command until the user says the wake word.
        self._post_interrupt_require_wake = True
        self._set_status("Операция прервана. Жду wake word…")
        self.robot.set_state("idle")
        self._resume_wake_listening(delay_ms=35)

    def _is_interrupt_phrase(self, text: str) -> bool:
        raw = str(text or "")
        if self._strict_stop_phrase(raw):
            return True
        canonical = normalize_multilingual_command(raw, self._language_code())
        norm = lambda value: re.sub(r"[^a-zа-яё0-9]+", " ", str(value or "").casefold()).strip()
        values = {norm(raw), norm(canonical)}
        phrases = {norm(x) for x in (self.config.get("voice.interrupt_phrases", []) or [])}
        phrases.update({"стоп", "отмена", "отмени", "не надо", "хватит", "stop", "cancel",
                        # NEO 169: stop a running multi-step task
                        "отмени задачу", "останови задачу", "прекрати задачу", "cancel the task", "stop the task",
                        "brich die aufgabe ab", "annule la tâche", "cancela la tarea", "annulla il compito", "cancela a tarefa"})
        if values & phrases:
            return True
        # Voice ASR frequently returns repeated emphasis: «стоп, стоп, стоп».
        # Treat a sequence made only of interrupt words as one interrupt command.
        repeated = r"(?:стоп|стап|stop|отмена|отмени|cancel)(?:\s+(?:стоп|стап|stop|отмена|отмени|cancel))*"
        return any(bool(re.fullmatch(repeated, value)) for value in values if value)

    def notify_previous_crash(self, recovery_path: Path) -> None:
        # Keep recovery evidence in diagnostics without a scary startup bubble.
        logger.warning("Previous session recovery evidence: %s", recovery_path)

    def run_startup_self_test(self) -> None:
        if self._quitting:
            return
        worker = FunctionWorker(self.agent.tools.execute, "diagnostics", {"action": "self_test", "quick": bool(self.config.get("startup_self_test.quick", True))})
        worker.signals.result.connect(self._startup_self_test_done)
        worker.signals.error.connect(lambda trace: logger.warning("Startup Self-Test failed: %s", trace))
        self.thread_pool.start(worker)

    def _startup_self_test_done(self, execution: object) -> None:
        success = bool(getattr(execution, "success", False))
        text = str(getattr(execution, "text", "") or "")
        payload = getattr(execution, "data", {}) or {}
        checks = payload.get("checks", []) if isinstance(payload, dict) else []
        issues = [row for row in checks if isinstance(row, dict) and (not row.get("ok", True) or row.get("status") in {"FAIL", "UNAVAILABLE"})]
        if issues:
            from jarvis.runtime.self_test_localization import render_report
            self._append_assistant(render_report(payload, self._language_code(), issues_only=True))
            return
        if success:
            logger.info("Startup Self-Test: %s", text)
            if not bool(self.config.get("startup_self_test.show_only_on_failure", True)):
                self._append_assistant(text)
        else:
            self._append_assistant(dt("self_test_issue", self._language_code()) + ": " + text)

    def _build_ui(self) -> None:
        self.surface = RoundedSurface()
        # HOTFIX38: the ordinary window surface always keeps the standard arrow
        # cursor. Drag affordance belongs only to the thin top handle and the
        # robot itself; otherwise the OpenHand cursor is inherited by unrelated
        # child widgets on Windows.
        self.surface.setCursor(Qt.ArrowCursor)
        self.setCentralWidget(self.surface)
        outer = QVBoxLayout(self.surface)
        self._outer_layout = outer
        outer.setContentsMargins(12, 9, 12, 10)
        outer.setSpacing(8)

        self.header = QFrame()
        self.header.setObjectName("dragHandle")
        self.header.setCursor(
            Qt.OpenHandCursor if bool(self.config.get("ui.drag_enabled", True)) else Qt.ArrowCursor
        )
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(2, 0, 2, 0)
        header_layout.setSpacing(0)
        header_layout.addStretch(1)
        # v4.0.15: the upper area is deliberately empty so the robot has a clean
        # stage. Window/settings controls live below the input row.
        self.header.setFixedHeight(6)
        self.reset_button = None
        # NEO 183: the drag handle is a transparent strip laid over the top of the stage instead
        # of a row of its own, so the stage weather and the robot reach the top edge under it.
        self.header.setParent(self.surface)
        self.header.setAttribute(Qt.WA_TranslucentBackground, True)
        self.header.setStyleSheet("QFrame#dragHandle { background: transparent; border: none; }")

        self.robot = RobotWidget(str(self.config.get("ui.accent", "#57D7FF")))
        # HOTFIX82: the robot stage is the flexible part of the normal window.
        # Increasing chat height must consume robot space, never grow the whole
        # top-level window. RobotWidget scales its drawing to the remaining area.
        self.robot.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.robot.layout_changed.connect(self._robot_layout_changed)
        self.robot.state_changed.connect(self._robot_state_changed177)
        outer.addWidget(self.robot, 4)
        # HOTFIX49: direct chat-height editor. It is invisible in normal mode;
        # when layout editing is enabled it becomes a slim horizontal drag handle.
        self.layout_edit_handle = QFrame()
        self.layout_edit_handle.setObjectName("layoutEditHandle")
        self.layout_edit_handle.setFixedHeight(8)
        self.layout_edit_handle.setCursor(Qt.SizeVerCursor)
        self.layout_edit_handle.setToolTip("Режим компоновки: потяните, чтобы изменить высоту чата")
        self.layout_edit_handle.installEventFilter(self)
        self.layout_edit_handle.hide()
        outer.addWidget(self.layout_edit_handle, 0)
        # Active timers/reminders belong to the robot stage itself rather than a
        # separate top-level window. A compact list button remains accessible
        # when the chosen font or timer count exceeds the stage's available area.
        self.timer_stage_label = QLabel(self.robot)
        self.timer_stage_label.setObjectName("timerStageLabel")
        self.timer_stage_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.timer_stage_label.setWordWrap(False)
        self.timer_stage_label.setTextFormat(Qt.PlainText)
        self.timer_stage_label.hide()
        from jarvis.ui.timer_overlay184 import TimerListButton
        self.timer_list_button184 = TimerListButton(self.robot, self._refresh_timer_stage_label)
        self.timer_list_button184.hide()

        self.chat = ChatView()
        self.chat.set_assistant_name(str(self.config.get("assistant_name", "NEO")))
        self.chat.set_user_name(str(self.config.get("user_name", "")) if self.config.get("user_name_enabled", True) else "")
        outer.addWidget(self.chat, 5)

        input_row = QHBoxLayout()
        self._input_row_layout = input_row
        input_row.setSpacing(0)
        self.input = CommandLineEdit()
        self.input.setPlaceholderText("Напишите команду…")
        self.input.returnPressed.connect(self._send_from_input)
        # NEO 174: typing a command warms up like the wake word does (model + service connections).
        self.input.textChanged.connect(self._typing_started174)
        self._input_history: list[str] = []
        self._input_history_index = 0
        self._input_history_draft = ""
        self.input.historyUpRequested.connect(lambda: self._recall_input_history(-1))
        self.input.historyDownRequested.connect(lambda: self._recall_input_history(1))
        self.mic_button = self.input.mic_button
        self.mic_button.clicked.connect(self._record_voice)
        self.send_button = self.input.send_button
        self.send_button.clicked.connect(self._send_from_input)
        input_row.addWidget(self.input, 1)
        outer.addLayout(input_row)

        # Compact bottom control strip.  Window controls sit to the LEFT of
        # the microphone name + level meter as requested, keeping the robot
        # stage completely clean.
        status_row = QHBoxLayout()
        self._status_row_layout = status_row
        status_row.setSpacing(4)
        self.logs_button = QPushButton("")
        self.logs_button.setObjectName("logsButton")
        self.logs_button.setToolTip("Показать логи запуска")
        self.logs_button.setCursor(Qt.ArrowCursor)
        self.logs_button.clicked.connect(self._toggle_launcher_logs)
        self.settings_button = QPushButton("")
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setToolTip("Настройки")
        self.settings_button.clicked.connect(self._open_settings)
        self.minimize_button = QPushButton("")
        self.minimize_button.setObjectName("minimizeButton")
        self.minimize_button.setToolTip("Свернуть окно")
        self.minimize_button.clicked.connect(self.showMinimized)
        self.close_button = QPushButton("")
        self.close_button.setObjectName("closeButton")
        self.close_button.setToolTip("Закрыть: выбрать полное завершение или сворачивание в трей")
        self.close_button.clicked.connect(self.close)
        status_row.addWidget(self.logs_button)
        status_row.addWidget(self.settings_button)
        status_row.addWidget(self.minimize_button)
        status_row.addWidget(self.close_button)
        status_row.addSpacing(2)

        self.status = QLabel("Ожидание")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setProperty("i18n_skip_text", True)
        self._status_source = "Ожидание"
        self.status.setObjectName("status")
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status.setMinimumWidth(70)
        self.status.setMaximumWidth(180)
        self.status.setToolTip("Состояние / активный микрофон")
        self.mic_level = QProgressBar()
        self.mic_level.setObjectName("micLevel")
        self.mic_level.setRange(0, 100)
        self.mic_level.setTextVisible(False)
        self.mic_level.setMinimumWidth(72)
        self.mic_level.setMaximumWidth(156)
        self.mic_level.hide()
        self.mic_mute_button = QPushButton("")
        self.mic_mute_button.setObjectName("micMuteButton")
        self.mic_mute_button.setCursor(Qt.ArrowCursor)
        self.mic_mute_button.setToolTip("Выключить микрофон для NEO")
        self.mic_mute_button.clicked.connect(self._toggle_app_microphone)
        self.status_dot = StatusDot()
        self.status_dot.setToolTip("Состояние NEO")
        status_row.addWidget(self.status_dot, 0, Qt.AlignVCenter)
        status_row.addWidget(self.status, 1)
        status_row.addWidget(self.mic_mute_button, 0)
        status_row.addWidget(self.mic_level, 0)
        self.size_grip = QSizeGrip(self.surface)
        self.size_grip.setToolTip("Изменить размер окна")
        status_row.addWidget(self.size_grip, 0, Qt.AlignRight | Qt.AlignBottom)
        outer.addLayout(status_row)
        self._active_microphone_name = ""

        QShortcut(QKeySequence("Ctrl+L"), self).activated.connect(self.input.setFocus)
        QShortcut(QKeySequence("Ctrl+Space"), self).activated.connect(self._record_voice)
        QShortcut(QKeySequence("Esc"), self).activated.connect(self.hide)

        # Install filters only after all attributes used by eventFilter exist.
        # Creating QLayout children itself emits events, so installing the header
        # filter earlier could call eventFilter before self.robot was assigned.
        # HOTFIX38: only the actual drag targets receive the drag event filter.
        # Installing it on the full surface caused a hand cursor to leak into
        # most of the application after a drag/release cycle.
        self.header.installEventFilter(self)
        self.robot.installEventFilter(self)

    def _create_tray(self) -> None:
        icon_path = Path(__file__).resolve().parents[2] / "assets" / "neo_jarvis.ico"
        icon = QIcon(str(icon_path)) if icon_path.exists() else self.windowIcon()
        self.tray = QSystemTrayIcon(icon, self)
        menu = QMenu()
        show_action = QAction("Показать NEO", self)
        show_action.triggered.connect(self._show_from_tray)
        settings_action = QAction("Настройки", self)
        settings_action.triggered.connect(self._open_settings)
        exit_action = QAction("Полностью завершить", self)
        exit_action.triggered.connect(self._quit_application)
        menu.addAction(show_action)
        menu.addAction(settings_action)
        shelf_action = QAction(dt("shelf", self._language_code()), self)
        shelf_action.setProperty("desktop_key", "shelf")
        shelf_action.triggered.connect(self._toggle_bookshelf)
        menu.addAction(shelf_action)
        self.activity_tray_action = QAction(dt("activity", self._language_code()), self)
        self.activity_tray_action.setProperty("desktop_key", "activity")
        self.activity_tray_action.setCheckable(True)
        self.activity_tray_action.setChecked(bool(self.config.get("activity.enabled", True)))
        self.activity_tray_action.triggered.connect(self._set_activity_enabled)
        menu.addAction(self.activity_tray_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self._show_from_tray()

    def _drain_ui_commands(self) -> None:
        for command in drain_ui_commands():
            if command.action == "show_main_window":
                self._show_from_tray()
            elif command.action == "quit_application":
                self._quit_application()
        for event in drain_desktop_events():
            try:
                self._handle_desktop_event(event)
            except Exception:
                logger.exception("Desktop request failed: %s", event.get("action"))

    def _ensure_bookshelf(self):
        if self.bookshelf is None:
            from jarvis.ui.bookshelf import Bookshelf
            self.bookshelf = Bookshelf(self.config, self._run_shelf_scenario, self._open_scenario_settings)
            manager = getattr(self, "overlay_manager", None)
            if manager:
                panels = manager.store.load().get("panels", {})
                self.bookshelf.store.migrate_overlays([p for p in panels.values() if isinstance(p, dict) and not p.get("game_id") and str(p.get("purpose", "")) not in {"music", "game_session"}])
                self.bookshelf.refresh()
        return self.bookshelf

    def _set_activity_enabled(self, enabled):
        self.config.data.setdefault("activity", {})["enabled"] = bool(enabled)
        self.config.save(self.config.data)
        if getattr(self, "desktop_services", None):
            self.desktop_services.reconfigure()

    def _show_activity_notice(self):
        if self.config.get("activity.enabled", True) and not self.config.get("activity.notice_shown", False):
            self.chat.add_assistant(dt("activity_notice", self._language_code()))
            self.config.data.setdefault("activity", {})["notice_shown"] = True
            self.config.save(self.config.data)

    def _toggle_bookshelf(self):
        if not self.config.get("shelf.enabled", True):
            self._set_status(dt("shelf_disabled", self._language_code()))
            return
        shelf = self._ensure_bookshelf()
        shelf.setVisible(not shelf.isVisible())

    def _open_scenario_settings(self):
        self._open_settings()
        dialog = getattr(self, "_settings_dialog", None)
        if dialog is not None:
            # Scenario page is lazy; schedule focus after its owner finishes it.
            opener = getattr(dialog, "open_page", None)
            if callable(opener):
                opener("scenarios")
            editor = getattr(dialog, "scenario_editor", None)
            if editor is not None:
                editor.list.setFocus()

    def _run_shelf_scenario(self, scenario_id):
        from jarvis.security import SecurityPolicy
        from copy import deepcopy
        args = {"action": "run", "id": str(scenario_id)}
        scenarios = deepcopy(self.config.get("scenarios.items", []) or [])
        chosen = next((x for x in scenarios if str(x.get("id") or x.get("name") or x.get("phrase")) == str(scenario_id)), None)
        if chosen is None or chosen.get("enabled", True) is not True:
            return
        from jarvis.runtime.scenarios import scenario_fingerprint
        args["fingerprint"] = scenario_fingerprint(chosen)
        try:
            risk = self.agent.tools.risk_for("scenario", args)
        except (ValueError, TypeError):
            from jarvis.ui.scenario_strings import st
            self._set_status(st("changed", self._language_code()))
            return
        if SecurityPolicy(self.config).needs_confirmation(risk):
            from jarvis.ui.scenario_confirmation184 import confirm_scenario
            if not confirm_scenario(self, chosen, self._language_code()):
                return
        generation = cancellation_manager().reset()
        def execute():
            from jarvis.tools.registry import suppress_execution_learning184
            with cancellation_manager().bound_generation(generation), suppress_execution_learning184():
                current = self.config.get("scenarios.items", []) or []
                if chosen not in current:
                    raise ValueError("Scenario changed after confirmation; select it again")
                return self.agent.tools.execute("scenario", args)
        worker = FunctionWorker(execute)
        worker.signals.result.connect(lambda result: self._append_assistant(str(getattr(result, "text", result)), localize=False))
        worker.signals.error.connect(lambda error: self._set_status(str(error)))
        self.thread_pool.start(worker)

    def _retry_shelf_scenario(self, arguments):
        """Manual one-step retry shares the normal scenario risk/tool gateway."""
        from copy import deepcopy
        from jarvis.security import SecurityPolicy
        from jarvis.runtime.scenarios import (find_scenario, scenario_retry_request,
                                               latest_scenario_result, active_scenarios)
        from jarvis.ui.scenario_strings import st
        if getattr(self, "_scenario_retry_pending184", False) or active_scenarios(self.config):
            self._set_status(st("busy", self._language_code()))
            return
        self._scenario_retry_pending184 = True
        queued = False
        try:
            args = deepcopy(arguments)
            if scenario_retry_request(self.config, args.get("run_id"), args.get("step_index")) != args:
                raise ValueError("retry_unavailable")
            chosen = deepcopy(find_scenario(self.config, args["id"]))
            record = latest_scenario_result(self.config, args["id"])
            outcome = next(row for row in record["outcomes"] if row["step_index"] == args["step_index"])
            risk = self.agent.tools.risk_for("scenario", args)
            if SecurityPolicy(self.config).needs_confirmation(risk):
                from jarvis.ui.scenario_confirmation184 import confirm_scenario
                if not confirm_scenario(self, chosen, self._language_code(), retry={**args, "mode": outcome["retry_mode"]}):
                    return
            generation = cancellation_manager().reset()
            def execute():
                from jarvis.tools.registry import suppress_execution_learning184
                with cancellation_manager().bound_generation(generation), suppress_execution_learning184():
                    return self.agent.tools.execute("scenario", args)
            def release(*_):
                self._scenario_retry_pending184 = False
            worker = FunctionWorker(execute)
            worker.signals.result.connect(release)
            worker.signals.error.connect(release)
            worker.signals.result.connect(lambda result: self._append_assistant(str(getattr(result, "text", result)), localize=False))
            worker.signals.error.connect(lambda error: self._set_status(str(error)))
            self.thread_pool.start(worker)
            queued = True
        except (ValueError, TypeError, KeyError, StopIteration) as exc:
            self._set_status(st(str(exc) if str(exc).startswith("retry_") else "retry_unavailable", self._language_code()))
        finally:
            if not queued:
                self._scenario_retry_pending184 = False

    def _scenario_event184(self, event: dict) -> None:
        if getattr(self, "_quitting", False):
            return
        scope = str(getattr(getattr(self.config, "paths", None), "root", id(self.config)))
        if event.get("scope") != scope:
            return
        self._pending_scenario_status184 = event
        if not self._scenario_status_timer184.isActive():
            self._scenario_status_timer184.start()

    def _flush_scenario_status184(self) -> None:
        event = self._pending_scenario_status184
        self._pending_scenario_status184 = None
        if not event or getattr(self, "_quitting", False):
            return
        self._set_status("", scenario_event=event)

    def _handle_desktop_event(self, event):
        action = event.get("action", "")
        if action in {"assistant.window.show", "assistant.window.hide"}:
            self._set_runtime_topmost(action.endswith(".show"), hide=action.endswith(".hide"))
        elif action == "services.reconfigure":
            if getattr(self, "desktop_services", None): self.desktop_services.reconfigure()
            self.activity_tray_action.setChecked(bool(self.config.get("activity.enabled", True)))
        elif action == "settings.apply":
            self._settings_updated()
        elif action == "ui.hints":
            from jarvis.ui.hint_overlay168 import show_hints
            show_hints(self, list(event.get("marks") or []), float(event.get("ttl", 30) or 30))
        elif action == "ui.hints_clear":
            from jarvis.ui.hint_overlay168 import clear_hints
            clear_hints(self)
        elif action.startswith("shelf.") or action.startswith("notes."):
            if not self.config.get("shelf.enabled", True) and action in {"shelf.show", "shelf.toggle", "notes.show"}:
                self._set_status(dt("shelf_disabled", self._language_code()))
                return
            shelf = self._ensure_bookshelf()
            if action == "shelf.show": shelf.show()
            elif action == "shelf.hide": shelf.hide()
            elif action == "shelf.toggle": self._toggle_bookshelf()
            elif action == "shelf.refresh": shelf.refresh()
            elif action == "shelf.open": shelf.open_object(event.get("object_id", event.get("id", "")))
            elif action == "shelf.copy":
                from jarvis.ui.bookshelf import object_mime
                ids = event.get("ids") or [event.get("object_id", event.get("id", ""))]
                QApplication.clipboard().setMimeData(object_mime([shelf.store.get(oid) for oid in ids if oid]))
            elif action == "notes.show": shelf.show_notes(True)
            elif action == "notes.hide": shelf.show_notes(False)
        elif action == "settings.open":
            self._open_settings()
            dialog = getattr(self, "_settings_dialog", None)
            if dialog is not None:
                section = str(event.get("section", "advanced") or "advanced")
                opener = getattr(dialog, "open_page", None)
                if callable(opener) and not opener(section):
                    opener("advanced")
                if section == "spotify":
                    dialog.spotify_integration_enabled.setFocus()
        elif action == "scenario.run":
            self._run_shelf_scenario(event.get("id", event.get("scenario_id", "")))
        elif action == "image.open":
            from jarvis.ui.mini_windows import ImageViewer
            viewer = ImageViewer(self.config, event.get("path", ""), event.get("id", ""))
            self._image_viewers.append(viewer)
            viewer.destroyed.connect(lambda _obj=None, w=viewer: self._image_viewers.remove(w) if w in self._image_viewers else None)
            viewer.show()
        elif action == "browser.open":
            from jarvis.ui.mini_windows import MiniBrowser, safe_browser_url
            if event.get("path"):
                self._handle_desktop_event({"action": "image.open", "path": event["path"], "id": event.get("id", "")})
                return
            url = event.get("url", event.get("query", ""))
            if self._mini_browser is not None:
                self._mini_browser.navigate(url)
                self._mini_browser.show()
            else:
                try:
                    browser = MiniBrowser(self.config, url)
                except ImportError:
                    from PySide6.QtGui import QDesktopServices
                    if QMessageBox.question(self, dt("browser", self._language_code()), dt("web_missing", self._language_code())) == QMessageBox.Yes:
                        QDesktopServices.openUrl(safe_browser_url(url))
                    return
                self._mini_browser = browser
                browser.destroyed.connect(lambda: setattr(self, "_mini_browser", None))
                browser.show()
        elif action.startswith("browser."):
            window_id = str(event.get("id", ""))
            if window_id == "image":
                target = self._image_viewers[-1] if self._image_viewers else None
            elif window_id and window_id != "browser":
                target = next((viewer for viewer in self._image_viewers if viewer.viewer_id == window_id), None)
            else:
                target = self._mini_browser
            if target is None:
                self._set_status(dt("window_unavailable", self._language_code()))
                return
            if action == "browser.close": target.close()
            elif action == "browser.pin": target.pin.setChecked(bool(event.get("pinned", event.get("enabled", True))))
            elif action == "browser.zoom":
                if "zoom" in event:
                    value = float(event["zoom"])
                    factor = max(0.25, min(3.0, value / 100 if value > 10 else value))
                    if target is self._mini_browser:
                        target.view.setZoomFactor(factor)
                    else:
                        target.set_zoom_factor(factor)
                else:
                    target.zoom(float(event.get("delta", 0.1)))

    def _desktop_service_event(self, event):
        """This slot always runs on Qt's thread; acknowledge only after rendering."""
        services = getattr(self, "desktop_services", None)
        event_id = str(event.get("event_id", ""))
        revision = int(event.get("revision", 0))
        previous = self._desktop_event_bubbles.get(event_id)
        if event.get("correction") or (previous and revision > previous["revision"]):
            # Provider evidence can correct a delivered terminal outcome. Edit
            # its original row, never append another completion/error notice.
            if previous and revision > previous["revision"]:
                from jarvis.runtime.desktop_services import event_text
                bubble = previous["bubble"]
                try:
                    if any(item is bubble.item for item in self.chat.items):
                        self.chat.update_bubble(bubble, event_text(event, self._language_code()))
                except RuntimeError:
                    pass  # The bounded chat may already have deleted this row.
                previous["revision"] = revision
            if services and event_id:
                services.store.acknowledge(event_id, revision=revision)
            return
        if event_id and event_id in self._seen_desktop_events:
            if services: services.store.acknowledge(event_id, revision=revision)
            return
        if event.get("type") == "notification":
            from jarvis.runtime.desktop_services import event_text
            text = event_text(event, self._language_code())
            bubble = self.chat.add_assistant(text) if text else None
            if event_id:
                if len(self._seen_desktop_event_order) == self._seen_desktop_event_order.maxlen:
                    expired = self._seen_desktop_event_order[0]
                    self._seen_desktop_events.discard(expired)
                    self._desktop_event_bubbles.pop(expired, None)
                self._seen_desktop_events.add(event_id)
                self._seen_desktop_event_order.append(event_id)
                if bubble is not None:
                    self._desktop_event_bubbles[event_id] = {"bubble": bubble, "revision": revision}
                if services: services.store.acknowledge(event_id, revision=revision)
        elif event.get("type") == "network_changed":
            try:
                from jarvis.tools.desktop_features import notify_network_changed
                worker = FunctionWorker(lambda: notify_network_changed(self.agent.tools))
                self.thread_pool.start(worker)
                telegram = getattr(self, "telegram_remote", None)
                callback = getattr(telegram, "on_network_changed", None)
                if callable(callback):
                    self.thread_pool.start(FunctionWorker(callback))
            except (ImportError, AttributeError):
                logger.debug("Network adapter hook unavailable", exc_info=True)

    def _speech_settings_signature(self):
        return json.dumps({"voice": self.config.get("voice", {}), "wake_word": self.config.get("wake_word", {}), "audio": self.config.get("audio", {}), "language": self.config.get("app.language", "en")}, sort_keys=True, default=str)

    def _show_from_tray(self) -> None:
        if not self._set_runtime_topmost(True):
            return
        self.activateWindow()
        self._restart_idle_timer()

    def _restore_geometry(self) -> None:
        width = int(self.config.get("ui.window_width", 322))
        height = int(self.config.get("ui.window_height", 476))
        # Legacy compact-geometry migration. HOTFIX37 now ships with the
        # requested 322x476 default; keep this branch only for very old oversized
        # user profiles and never shrink the new compact baseline again.
        if not bool(self.config.get("ui.compact_geometry_v4015_applied", False)):
            if width > 460 or height > 630:
                width = max(320, int(round(width * 0.80)))
                height = max(360, int(round(height * 0.70)))
            try:
                data = self.config.data
                ui = data.setdefault("ui", {})
                ui["window_width"] = width
                ui["window_height"] = height
                ui["compact_geometry_v4015_applied"] = True
                self.config.save(data)
            except Exception:
                logger.exception("Could not persist v4.0.15 compact geometry migration")
        self.resize(width, height)
        x = self.config.get("ui.window_x", None)
        y = self.config.get("ui.window_y", None)
        if isinstance(x, int) and isinstance(y, int):
            self.move(x, y)

    def _save_geometry(self) -> None:
        if self._quitting:
            return
        data = self.config.data
        ui = data.setdefault("ui", {})
        geometry = {
            "window_x": self.x(),
            "window_y": self.y(),
        }
        # Robot-only mode has its own content-fitted height.  Do not overwrite
        # the user's normal chat-window geometry with that temporary compact
        # presentation size; otherwise leaving robot-only would restore a tiny
        # chat window.
        if not bool(self.config.get("ui.robot_only_mode", False)):
            geometry["window_width"] = self.width()
            geometry["window_height"] = self.height()
        ui.update(geometry)
        try:
            self.config.save(data)
        except Exception:
            logger.exception("Could not persist window geometry")

    def _apply_settings(self, *, first_show: bool = False) -> None:
        response_voice = getattr(self, "_response_voice_service", None)
        if response_voice is not None:
            response_voice.apply_preferences()
        was_visible = self.isVisible()
        self.setWindowOpacity(1.0)
        ui = self.config.data.setdefault("ui", {})
        accent = str(self.config.get("ui.accent", "#57D7FF"))
        window_color = str(self.config.get("ui.window_color", "#0C1017"))
        panel_color = str(self.config.get("ui.panel_color", "#111722"))
        border_color = str(self.config.get("ui.border_color", accent))
        text_color = str(self.config.get("ui.text_color", "#EAF2FF"))
        muted = str(self.config.get("ui.muted_text_color", "#8F9DB2"))
        element = str(self.config.get("ui.element_color", "#192333"))
        element_hover = str(self.config.get("ui.element_hover_color", "#243247"))
        chat_color = str(self.config.get("ui.chat_color", "#101722"))
        user_color = str(self.config.get("ui.chat_user_color", "#23465A"))
        assistant_color = str(self.config.get("ui.chat_assistant_color", "#182232"))
        legacy_transparent_background = bool(self.config.get("ui.transparent_background", False))
        legacy_transparent_borders = bool(self.config.get("ui.transparent_borders", False))
        window_opacity = max(0, min(100, int(self.config.get("ui.window_opacity_percent", 0 if legacy_transparent_background else 100) or 0)))
        panel_opacity = max(0, min(100, int(self.config.get("ui.panel_opacity_percent", 100) or 0)))
        element_opacity = max(0, min(100, int(self.config.get("ui.element_opacity_percent", 100) or 0)))
        chat_opacity = max(0, min(100, int(self.config.get("ui.chat_opacity_percent", 100) or 0)))
        bubble_opacity = max(0, min(100, int(self.config.get("ui.bubble_opacity_percent", 100) or 0)))
        border_opacity = max(0, min(100, int(self.config.get("ui.border_opacity_percent", 0 if legacy_transparent_borders else 100) or 0)))
        robot_only = bool(self.config.get("ui.robot_only_mode", False))
        # Robot-only mode hides the conversation/input layer, not the application
        # chrome or robot stage. Background/border transparency remains entirely
        # controlled by the user's normal opacity settings.
        effective_window_opacity = window_opacity
        effective_border_opacity = border_opacity
        transparent_background = effective_window_opacity <= 0
        transparent_borders = effective_border_opacity <= 0
        radius = int(self.config.get("ui.corner_radius", 26))
        self._corner_radius = radius
        # IMPORTANT (Qt/Windows): WA_TranslucentBackground must be enabled when
        # the top-level window is created and should not be toggled after show().
        # We therefore keep the alpha-capable native surface for the whole lifetime
        # and vary only the alpha of the pixels we paint.
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.surface.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.surface.set_theme(
            window_color,
            border_color,
            radius,
            background_opacity=effective_window_opacity,
            border_opacity=effective_border_opacity,
            transparent_background=transparent_background,
            transparent_border=transparent_borders,
        )
        self._update_window_mask()
        self.robot.set_background(window_color)
        # HOTFIX101: the robot scene follows the real window opacity. A second
        # scene-opacity control was misleading and had no independent UX value.
        self.robot.set_background_opacity(effective_window_opacity)
        self.robot.set_colors(
            body=str(self.config.get("ui.robot_body_color", "#313C4F")),
            shell=str(self.config.get("ui.robot_shell_color", "#DDE7F4")),
            screen=str(self.config.get("ui.robot_screen_color", "#101721")),
            eye=str(self.config.get("ui.robot_eye_color", accent)),
            trim=str(self.config.get("ui.robot_trim_color", "#718198")),
            glow=str(self.config.get("ui.robot_glow_color", accent)),
        )
        self.robot.set_environment_colors(
            moon=str(self.config.get("ui.robot_moon_color", "#DDE7F4")),
            cloud=str(self.config.get("ui.robot_cloud_color", "#DCE7F3")),
            rain=str(self.config.get("ui.robot_rain_color", "#91CFFF")),
            snow=str(self.config.get("ui.robot_snow_color", "#F6FAFF")),
            starfall=str(self.config.get("ui.robot_starfall_color", "#EAF6FF")),
            starry_sky=str(self.config.get("ui.robot_starry_sky_color", self.config.get("ui.robot_starfall_color", "#EAF6FF"))),
        )
        animation_fps = int(self.config.get("ui.animation_fps", 60) or 60)
        if animation_fps not in {30, 60}:
            animation_fps = 60
        self.robot.set_frame_rate(animation_fps)
        # One global visual cadence. Source polling (mic/player IPC) remains at a
        # lower efficient rate and the UI interpolates at this render FPS.
        frame_ms = {30: 33, 60: 16}[animation_fps]
        self._typing_timer.setInterval(frame_ms)
        self._heard_timer.setInterval(frame_ms)
        self._stream_flush_timer.setInterval(frame_ms)
        self._mic_meter_timer.setInterval(33)
        try:
            music_style = self.config.data.setdefault("desktop", {}).setdefault("overlay", {}).setdefault("styles", {}).setdefault("music", {})
            music_style["visualizer_fps"] = animation_fps
        except Exception:
            pass
        self.robot.set_scale_factor(float(self.config.get("ui.robot_scale_percent", 100)) / 100.0)
        self.robot.set_headphones(bool(self.config.get("ui.robot_headphones_enabled", True)), str(self.config.get("ui.robot_headphones_color", accent)))
        self.robot.configure_gifs(bool(self.config.get("ui.robot_gif_enabled", False)), self.config.get("ui.robot_gif_states", {}) or {}, int(self.config.get("ui.robot_gif_scale_percent", 100)))
        self.robot.set_active_animation(str(self.config.get("ui.robot_active_animation", "off") or "off"))
        if hasattr(self.robot, "set_cloud_density"):
            self.robot.set_cloud_density(str(self.config.get("ui.robot_cloud_density", "normal") or "normal"))
        self.robot.set_accessories(
            coffee=bool(self.config.get("ui.robot_accessory_coffee", False)),
            cigarette=bool(self.config.get("ui.robot_accessory_cigarette", False)),
            cigarette_color=str(self.config.get("ui.robot_accessory_cigarette_color", self.config.get("ui.robot_trim_color", "#718198")) or "#718198"),
            mug_color=str(self.config.get("ui.robot_accessory_mug_color", self.config.get("ui.robot_shell_color", "#DDE7F4")) or "#DDE7F4"),
            hand_color=str(self.config.get("ui.robot_accessory_hand_color", self.config.get("ui.robot_body_color", "#313C4F")) or "#313C4F"),
        )
        self.robot.set_info_config(
            clock_enabled=bool(self.config.get("ui.robot_info_clock_enabled", False)),
            clock_position=str(self.config.get("ui.robot_info_clock_position", "top_center") or "top_center"),
            track_enabled=bool(self.config.get("ui.robot_info_track_enabled", False)),
            track_position=str(self.config.get("ui.robot_info_track_position", "bottom_center") or "bottom_center"),
            temperature_enabled=bool(self.config.get("ui.robot_info_temperature_enabled", False)),
            temperature_position=str(self.config.get("ui.robot_info_temperature_position", "top_right") or "top_right"),
            font_size=int(self.config.get("ui.robot_info_font_size", 12) or 12),
            layout=self.config.get("ui.robot_info_layout", {}) or {},
            edit_mode=bool(self.config.get("ui.robot_layout_edit_enabled", False)),
        )
        self._apply_chat_height_setting()
        if hasattr(self.chat, "set_scrollbar_color"):
            self.chat.set_scrollbar_color(accent)
        edit_layout = bool(self.config.get("ui.robot_layout_edit_enabled", False))
        if hasattr(self, "_outer_layout"):
            if edit_layout:
                self._outer_layout.setContentsMargins(8, 6, 8, 8)
                self._outer_layout.setSpacing(7)
                if hasattr(self, "header"):
                    self._place_drag_handle183()
            else:
                self._outer_layout.setContentsMargins(6, 5, 6, 6)
                self._outer_layout.setSpacing(5)
                if hasattr(self, "header"):
                    self._place_drag_handle183()
        if hasattr(self, "layout_edit_handle"):
            handle_color = _rgba(accent, 62 if edit_layout else 0)
            _set_qss180(self.layout_edit_handle,
                "QFrame#layoutEditHandle { background: transparent; "
                f"border-top: 1px dashed {handle_color}; border-bottom: 1px dashed {handle_color}; }}"
            )
            self.layout_edit_handle.setVisible(edit_layout and not robot_only)
        self.robot.setVisible(robot_only or bool(self.config.get("ui.show_robot", True)))
        QTimer.singleShot(0, self._fit_robot_info_window)
        if bool(self.config.get("ui.robot_info_temperature_enabled", False)):
            QTimer.singleShot(0, self._refresh_robot_temperature_async)
        self.chat.set_assistant_name(str(self.config.get("assistant_name", "NEO")))
        self.chat.set_user_name(str(self.config.get("user_name", "")) if self.config.get("user_name_enabled", True) else "")
        desired_typewriter_ms = max(1, min(120, int(self.config.get("ui.typewriter_interval_ms", 8))))
        # Respect the global 30/60/120 visual cadence. If the requested character
        # speed is faster than one character per frame, _typewriter_tick() reveals
        # multiple characters at once; if it is slower, the timer itself may tick
        # less often. This keeps reading speed independent from render FPS without
        # reintroducing the old hard 30-FPS cap.
        self._typing_timer.setInterval(max(frame_ms, desired_typewriter_ms))
        self._ambient_typing_timer.setInterval(max(frame_ms, desired_typewriter_ms))
        if hasattr(self, "timer_stage_label"):
            timer_font = self.timer_stage_label.font()
            timer_font.setFamily(self.font().family())
            timer_font.setPixelSize(max(8, min(48, int(self.config.get("ui.timer_overlay_font_size", 12) or 12))))
            self.timer_stage_label.setFont(timer_font)
            timer_text = str(self.config.get("ui.timer_overlay_text_color", text_color) or text_color)
            timer_bg = str(self.config.get("ui.timer_overlay_background_color", panel_color) or panel_color)
            timer_border = str(self.config.get("ui.timer_overlay_border_color", border_color) or border_color)
            timer_opacity = max(10, min(100, int(self.config.get("ui.timer_overlay_opacity_percent", 88) or 88)))
            timer_style = str(self.config.get("ui.timer_overlay_style", "glass") or "glass").casefold()
            radius, padding, border_width = {
                "minimal": (2, "3px 5px", 0),
                "pill": (15, "5px 10px", 1),
            }.get(timer_style, (7, "5px 7px", 1))
            _set_qss180(self.timer_stage_label,
                "QLabel#timerStageLabel {"
                f"color:{timer_text}; background:{_rgba(timer_bg, timer_opacity)}; "
                f"border:{border_width}px solid {timer_border}; border-radius:{radius}px; padding:{padding}; }}"
            )
            self._refresh_timer_stage_label()

        # HOTFIX130: never reset top-level WindowFlags while the application is
        # alive. On Windows that recreates the HWND and invalidates global hotkey
        # registrations. Runtime topmost/visibility is handled by
        # ``_set_runtime_topmost`` using the existing native window.

        panel_rgba = _rgba(panel_color, panel_opacity)
        element_rgba = _rgba(element, element_opacity)
        chat_rgba = _rgba(chat_color, chat_opacity)
        border_soft = _rgba(border_color, round(border_opacity * 0.34))
        border_assistant = _rgba(border_color, round(border_opacity * 0.25))
        border_input = _rgba(border_color, round(border_opacity * 0.44))
        border_button = _rgba(border_color, round(border_opacity * 0.48))
        border_meter = _rgba(border_color, round(border_opacity * 0.40))
        border_thinking = _rgba(border_color, round(border_opacity * 0.42))
        focus_border = "transparent" if transparent_borders else _rgba(accent, border_opacity)
        main_background = "transparent"
        self._theme_cache = {
            "accent": accent, "text_color": text_color, "muted": muted,
            "element": element, "element_hover": element_hover,
            "panel_rgba": panel_rgba, "element_rgba": element_rgba, "chat_rgba": chat_rgba,
            "border_soft": border_soft, "border_assistant": border_assistant,
            "border_input": border_input, "border_button": border_button,
            "border_meter": border_meter, "border_thinking": border_thinking,
            "focus_border": focus_border, "user_color": user_color,
            "assistant_color": assistant_color, "bubble_opacity": bubble_opacity,
            "border_opacity": effective_border_opacity, "element_opacity": element_opacity,
            "radius": radius,
            # NEO 182: modal message boxes get an opaque background in the theme colour,
            # so the light text of dark themes is never drawn on the light system default.
            "dialog_bg": QColor(panel_color).name(QColor.HexRgb) if QColor(panel_color).isValid() else "#0C1017",
        }
        self._refresh_main_style()
        self._apply_dynamic_scale()
        self._apply_robot_only_mode(robot_only)
        if (first_show or was_visible) and not getattr(self, "_runtime_demoted184", False):
            if self._runtime_topmost:
                # Preserve the no-focus overlay contract even when theme/layout
                # settings rebuild native window flags.
                self._set_runtime_topmost(True, hide=False)
            else:
                self.show()

    def _refresh_main_style(self) -> None:
        theme = getattr(self, "_theme_cache", None) or {}
        if not theme:
            return
        scale = max(0.72, min(1.70, float(getattr(self, "_scale", 1.0))))
        px = lambda value, floor=1: max(floor, int(round(value * scale)))
        accent = theme["accent"]; text_color = theme["text_color"]; muted = theme["muted"]
        element = theme["element"]; element_hover = theme["element_hover"]
        panel_rgba = theme["panel_rgba"]; element_rgba = theme["element_rgba"]; chat_rgba = theme["chat_rgba"]
        border_soft = theme["border_soft"]; border_assistant = theme["border_assistant"]
        border_input = theme["border_input"]; border_button = theme["border_button"]
        border_meter = theme["border_meter"]; border_thinking = theme["border_thinking"]
        focus_border = theme["focus_border"]; user_color = theme["user_color"]; assistant_color = theme["assistant_color"]
        bubble_opacity = int(theme["bubble_opacity"]); border_opacity = int(theme["border_opacity"]); element_opacity = int(theme["element_opacity"])
        radius = int(theme["radius"]); dynamic_radius = max(px(8, 6), int(radius * scale))
        _set_qss180(self,
            f"""
            QMainWindow {{ background:transparent; color:{text_color}; font-family:'Segoe UI'; }}
            QFrame#mainSurface {{ background:transparent; border:none; }}
            QWidget {{ color:{text_color}; font-family:'Segoe UI'; }}
            QMessageBox {{ background:{theme.get("dialog_bg") or ("#0C1017" if QColor(text_color).lightness() > 128 else "#FFFFFF")}; }}
            QMessageBox QLabel {{ color:{text_color}; background:transparent; }}
            QLabel#status {{ color:{muted}; }}
            QScrollArea#chatView {{ background:transparent; border:1px solid {border_soft}; border-radius:{max(px(8,6), dynamic_radius // 2)}px; }}
            QScrollArea#chatView QWidget#qt_scrollarea_viewport {{ background:{chat_rgba}; border-radius:{max(px(7,5), dynamic_radius // 2 - 1)}px; }}
            QWidget#chatContainer {{ background:transparent; }}
            QWidget#chatRow {{ background:transparent; }}
            QFrame#bubble_user {{ background:qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {_rgba(user_color, bubble_opacity)}, stop:1 {_rgba(accent, max(8, bubble_opacity // 5) if bubble_opacity else 0)}); border:1px solid {_rgba(accent, round(border_opacity * 0.40))}; border-radius:{px(15,10)}px; border-bottom-right-radius:{px(5,3)}px; }}
            QFrame#bubble_assistant {{ background:{_rgba(assistant_color, bubble_opacity)}; border:1px solid {border_assistant}; border-left:{max(2, px(3,2))}px solid {_rgba(accent, round(border_opacity * 0.55))}; border-radius:{px(15,10)}px; border-bottom-left-radius:{px(5,3)}px; }}
            QLabel#bubbleAuthor {{ color:{accent}; font-weight:700; }}
            QLabel#bubbleTime {{ color:{muted}; }}
            QLabel#bubbleBody {{ color:{text_color}; }}
            QLabel#thinkingBody {{ color:{muted}; padding:{px(4,3)}px {px(7,5)}px; border-left:{max(1,px(2,1))}px solid {border_thinking}; }}
            QPushButton#thinkingToggle {{ background:transparent; color:{muted}; border:none; padding:{px(2,1)}px 0; text-align:left; }}
            QPushButton#thinkingToggle:hover {{ color:{accent}; background:transparent; }}
            QLabel#toolDetails {{ color:{muted}; padding:{px(3,2)}px; }}
            QLineEdit {{ background:{element_rgba}; color:{text_color}; border:1px solid {border_input}; border-radius:{px(16,14)}px; padding:{px(6,4)}px {px(7,5)}px; selection-background-color:{accent}; }}
            QLineEdit:focus {{ border:1px solid {focus_border}; }}
            QToolButton#inputMicButton, QToolButton#inputSendButton {{ background:transparent; color:{text_color}; border:none; border-radius:{px(14,10)}px; padding:0; font-weight:700; }}
            QToolButton#inputMicButton:hover, QToolButton#inputSendButton:hover {{ background:{_rgba(element_hover, max(element_opacity, 48))}; color:{accent}; }}
            QToolButton#inputMicButton:pressed, QToolButton#inputSendButton:pressed {{ background:{panel_rgba}; }}
            QToolButton#inputMicButton:disabled, QToolButton#inputSendButton:disabled {{ color:{muted}; background:transparent; }}
            QPushButton {{ background:{element_rgba}; color:{text_color}; border:1px solid {border_button}; border-radius:{px(7,5)}px; padding:{px(3,2)}px {px(7,5)}px; }}
            QPushButton:hover {{ background:{_rgba(element_hover, max(element_opacity, 35))}; border-color:{focus_border}; }}
            QPushButton:pressed {{ background:{panel_rgba}; }}
            QPushButton:disabled {{ color:{muted}; background:{_rgba(element, 35)}; }}
            QPushButton#logsButton, QPushButton#settingsButton, QPushButton#minimizeButton, QPushButton#closeButton, QPushButton#micMuteButton {{ padding:{px(2,1)}px; border-radius:{px(10,7)}px; background:transparent; border:1px solid transparent; }}
            QPushButton#logsButton:hover:enabled, QPushButton#settingsButton:hover:enabled, QPushButton#minimizeButton:hover:enabled, QPushButton#closeButton:hover:enabled, QPushButton#micMuteButton:hover:enabled {{ background:{_rgba(element_hover, max(65, element_opacity))}; color:{text_color}; border-color:{_rgba(accent, round(border_opacity * 0.45))}; }}
            QPushButton#logsButton:pressed:enabled, QPushButton#settingsButton:pressed:enabled, QPushButton#minimizeButton:pressed:enabled, QPushButton#closeButton:pressed:enabled, QPushButton#micMuteButton:pressed:enabled {{ background:{_rgba(element, max(75, element_opacity))}; border-color:{focus_border}; }}
            QPushButton#logsButton:focus, QPushButton#settingsButton:focus, QPushButton#minimizeButton:focus, QPushButton#closeButton:focus, QPushButton#micMuteButton:focus {{ border-color:{focus_border}; }}
            QPushButton#closeButton {{ font-weight:500; }}
            QPushButton#micButton {{ min-width:{px(32,26)}px; }}
            QProgressBar#micLevel {{ background:{element_rgba}; border:1px solid {border_meter}; border-radius:{px(4,3)}px; }}
            QProgressBar#micLevel::chunk {{ background:{accent}; border-radius:{px(3,2)}px; }}
            /* HOTFIX60: keep the chat thumb physically inside the rounded
               viewport. Previously the thumb touched the bottom/right clip and
               its rounded corners were cut into a square while resizing. */
            QScrollArea#chatView QScrollBar:vertical {{ background:transparent; width:{px(10,8)}px; margin:{px(7,5)}px {px(3,2)}px {px(7,5)}px {px(1,1)}px; border:none; border-radius:{px(5,4)}px; }}
            QScrollArea#chatView QScrollBar::handle:vertical {{ background:{_rgba(accent, 48)}; border:none; border-radius:{px(5,4)}px; min-height:{px(24,20)}px; margin:{px(1,1)}px; }}
            QScrollArea#chatView QScrollBar::handle:vertical:hover {{ background:{_rgba(accent, 78)}; }}
            QScrollArea#chatView QScrollBar::add-line:vertical, QScrollArea#chatView QScrollBar::sub-line:vertical {{ height:0px; background:transparent; border:none; }}
            QScrollArea#chatView QScrollBar::add-page:vertical, QScrollArea#chatView QScrollBar::sub-page:vertical {{ background:transparent; border:none; }}
            QScrollArea#chatView::corner {{ background:transparent; border:none; }}
            QSizeGrip {{ background:transparent; }}
            """
        )
        # Windows/Qt can occasionally bypass descendant scrollbar QSS after a
        # resize. Apply the same rounded style directly to the concrete bar so
        # its thumb cannot fall back to the square native appearance.
        chat_bar = self.chat.verticalScrollBar()
        chat_bar.setObjectName("chatScrollBar")
        _set_qss180(chat_bar,
            f"""
            QScrollBar#chatScrollBar:vertical {{ background:transparent; width:{px(10,8)}px; margin:{px(7,5)}px {px(2,1)}px {px(7,5)}px {px(1,1)}px; border:none; border-radius:{px(5,4)}px; }}
            QScrollBar#chatScrollBar::handle:vertical {{ background:{_rgba(accent, 48)}; border:none; border-radius:{px(5,4)}px; min-height:{px(24,20)}px; margin:{px(1,1)}px; }}
            QScrollBar#chatScrollBar::handle:vertical:hover {{ background:{_rgba(accent, 78)}; }}
            QScrollBar#chatScrollBar::add-line:vertical, QScrollBar#chatScrollBar::sub-line:vertical {{ height:0px; background:transparent; border:none; }}
            QScrollBar#chatScrollBar::add-page:vertical, QScrollBar#chatScrollBar::sub-page:vertical {{ background:transparent; border:none; }}
            """
        )
        viewer = getattr(self, "log_viewer", None)
        if viewer is not None and hasattr(viewer, "apply_theme"):
            # NEO 183: readable logs on every theme
            viewer.apply_theme(text_color, theme.get("dialog_bg") or ("#0C1017" if QColor(text_color).lightness() > 128 else "#FFFFFF"), accent)

    def _apply_dynamic_scale(self) -> None:
        # HOTFIX37 baseline is the compact 322x476 default requested for the
        # primary assistant window. Geometry scales with the window; chat text
        # has its own explicit px setting and does not participate in this ratio.
        width_ratio = max(0.1, self.width() / 322.0)
        height_ratio = max(0.1, self.height() / 476.0)
        robot_only = bool(self.config.get("ui.robot_only_mode", False))
        # In robot-only mode the window is intentionally much shorter.  Scaling
        # by its compact height would also shrink the robot, defeating the point
        # of the presentation mode.  Use width as the scale driver there.
        if robot_only:
            self._scale = max(0.72, min(1.70, width_ratio))
        else:
            self._scale = max(0.72, min(1.70, min(width_ratio, height_ratio)))
        text_scale = self._scale
        chat_font_px = max(8, min(36, int(self.config.get("ui.chat_font_size_px", 14) or 14)))
        chat_font_family = str(self.config.get("ui.chat_font_family", "Segoe UI") or "Segoe UI").strip() or "Segoe UI"

        if hasattr(self, "_outer_layout"):
            side_margin = max(5, int(8 * self._scale))
            if robot_only:
                # User-requested compact robot presentation: exactly 20 px from
                # the top edge to the robot stage and 20 px below the bottom
                # control strip.  Left/right spacing continues to scale normally.
                self._outer_layout.setContentsMargins(side_margin, 20, side_margin, 20)
            else:
                self._outer_layout.setContentsMargins(
                    side_margin, max(5, int(8 * self._scale)),
                    side_margin, max(5, int(8 * self._scale)),
                )
            self._outer_layout.setSpacing(max(4, int(6 * self._scale)))
        if hasattr(self, "_input_row_layout"):
            self._input_row_layout.setSpacing(max(2, int(4 * self._scale)))
        if hasattr(self, "_status_row_layout"):
            self._status_row_layout.setSpacing(max(2, int(4 * self._scale)))
        pass
        self._place_drag_handle183()

        status_font = self.status.font()
        status_font.setPixelSize(max(8, int(12 * text_scale)))
        self.status.setFont(status_font)
        self.status.setMinimumWidth(max(58, int(70 * self._scale)))
        self.status.setMaximumWidth(max(150, int(230 * self._scale)))

        input_font = self.input.font()
        input_font.setPixelSize(max(10, int(14 * text_scale)))
        self.input.setFont(input_font)
        # Requested compact command line: ~28 px at 100% instead of the old
        # 32+ px row, while still scaling on high-DPI/custom-size layouts.
        self.input.setFixedHeight(max(30, int(34 * self._scale)))

        for button in (self.reset_button, self.logs_button, self.settings_button, self.minimize_button, self.close_button, self.mic_mute_button):
            if button is None:
                continue
            font = button.font()
            font.setPixelSize(max(8, int(12 * text_scale)))
            button.setFont(font)
            button.setMinimumHeight(max(20, int(21 * self._scale)))
        control_side = max(20, int(21 * self._scale))
        control_font_px = max(11, int(14 * text_scale))
        for button in (self.logs_button, self.settings_button, self.minimize_button, self.close_button):
            button.setFixedSize(control_side, control_side)
            font = button.font()
            font.setPixelSize(control_font_px)
            font.setBold(False)
            button.setFont(font)
        self.logs_button.setIconSize(QSize(max(13, int(15 * self._scale)), max(13, int(15 * self._scale))))

        mute_side = max(19, int(20 * self._scale))
        self.mic_mute_button.setFixedSize(mute_side, mute_side)
        self.mic_mute_button.setIconSize(QSize(max(14, int(16 * self._scale)), max(14, int(16 * self._scale))))
        self.mic_level.setMinimumWidth(max(54, int(72 * self._scale)))
        self.mic_level.setMaximumWidth(max(82, int(112 * self._scale)))
        self.mic_level.setFixedHeight(max(6, int(8 * self._scale)))
        grip_side = max(12, int(16 * self._scale))
        self.size_grip.setFixedSize(grip_side, grip_side)

        self._refresh_mic_mute_icon()
        self._refresh_launcher_logs_icon()
        self._refresh_window_icons177()
        dot_side = max(9, int(11 * self._scale))
        self.status_dot.setFixedSize(dot_side, dot_side)
        control_icon = max(11, int(14 * self._scale))
        for button in (self.settings_button, self.minimize_button, self.close_button):
            button.setIconSize(QSize(control_icon, control_icon))
        self._refit_status_text()
        self.robot.set_layout_scale(self._scale)
        self.chat.set_scale(self._scale)
        self.chat.set_font_family(chat_font_family)
        self.chat.set_font_size(chat_font_px)
        if hasattr(self, "_style_scale_timer"):
            self._style_scale_timer.start()

    def _normal_window_vertical_budget(self) -> int:
        """Vertical pixels used by everything except chat and the robot stage."""
        outer = getattr(self, "_outer_layout", None)
        if outer is None:
            return 120
        margins = outer.contentsMargins()
        spacing = max(0, int(outer.spacing()))
        header_h = int(getattr(self, "header", QWidget()).sizeHint().height()) if hasattr(self, "header") else 10
        input_h = int(max(26, getattr(self, "input", QWidget()).height())) if hasattr(self, "input") else 28
        controls = [
            getattr(self, "logs_button", None), getattr(self, "settings_button", None),
            getattr(self, "minimize_button", None), getattr(self, "close_button", None),
            getattr(self, "mic_mute_button", None), getattr(self, "status", None),
        ]
        status_h = max([int(w.sizeHint().height()) for w in controls if w is not None] + [24])
        handle_h = int(getattr(self, "layout_edit_handle", QWidget()).height()) if getattr(self, "layout_edit_handle", None) is not None and self.layout_edit_handle.isVisible() else 0
        # Header, robot, optional handle, chat, input row and status row create
        # up to five inter-item gaps in the outer vertical layout.
        return int(margins.top() + margins.bottom() + header_h + input_h + status_h + handle_h + spacing * 5)

    def _max_chat_height_for_current_window(self) -> int:
        # The robot is intentionally allowed to become compact.  82 px is enough
        # for the responsive painter to keep a recognizable robot without forcing
        # the top-level window to grow.
        min_robot = max(72, int(round(82 * max(0.78, min(1.25, getattr(self, "_scale", 1.0))))))
        return max(100, int(self.height()) - self._normal_window_vertical_budget() - min_robot)

    def _apply_chat_height_setting(self) -> None:
        chat = getattr(self, "chat", None)
        if chat is None:
            return
        value = max(0, int(self.config.get("ui.chat_height_px", 0) or 0))
        if value > 0:
            requested = max(100, min(1600, value))
            actual = min(requested, self._max_chat_height_for_current_window())
            chat.setMinimumHeight(actual)
            chat.setMaximumHeight(actual)
        else:
            # Automatic mode lets the normal QVBox stretch factors divide the
            # fixed window between chat and robot.
            chat.setMinimumHeight(0)
            chat.setMaximumHeight(16777215)
        robot = getattr(self, "robot", None)
        if robot is not None:
            robot.updateGeometry()
            robot.update()

    def _persist_ui_layout_value(self, key: str, value: Any) -> None:
        try:
            ui = self.config.data.setdefault("ui", {})
            if not isinstance(ui, dict):
                ui = {}; self.config.data["ui"] = ui
            ui[str(key)] = value
            self.config.save(self.config.data)
        except Exception:
            logger.exception("Could not persist robot layout setting %s", key)

    def _sync_open_settings_layout(
        self,
        *,
        window_width: int | None = None,
        window_height: int | None = None,
        chat_height: int | None = None,
    ) -> None:
        dialog = getattr(self, "_settings_dialog", None)
        if dialog is None:
            return
        try:
            dialog.sync_live_layout_values(
                window_width=window_width,
                window_height=window_height,
                chat_height=chat_height,
            )
        except (RuntimeError, AttributeError):
            pass
        except Exception:
            logger.debug("Could not synchronize live layout values into Settings", exc_info=True)

    def _robot_layout_changed(self, layout: dict) -> None:
        if not isinstance(layout, dict):
            return
        self._persist_ui_layout_value("robot_info_layout", layout)
        QTimer.singleShot(0, self._fit_robot_info_window)

    def _fit_robot_info_window(self) -> None:
        """Ensure the robot stage and enabled HUD lanes have real layout space.

        In robot-only mode the frameless window follows the content in both
        directions. In the normal chat layout the outer window is completely
        user-owned: HUD/chat changes reflow inside the current rectangle and are
        never allowed to enlarge or restore a stale saved width/height.
        """
        if self._quitting:
            return
        robot = getattr(self, "robot", None)
        outer = getattr(self, "_outer_layout", None)
        if robot is None or outer is None or not robot.isVisible():
            return
        robot_only = bool(self.config.get("ui.robot_only_mode", False))
        try:
            stage_w, stage_h = robot.recommended_stage_size()
        except Exception:
            stage_w, stage_h = max(320, robot.minimumWidth()), max(120, robot.minimumHeight())
        margins = outer.contentsMargins()
        spacing = max(5, int(outer.spacing()))

        if robot_only:
            controls = [
                getattr(self, "logs_button", None), getattr(self, "settings_button", None),
                getattr(self, "minimize_button", None), getattr(self, "close_button", None),
                getattr(self, "mic_mute_button", None), getattr(self, "status", None),
                getattr(self, "mic_level", None),
            ]
            controls_h = max([int(w.sizeHint().height()) for w in controls if w is not None] + [24])
            controls_w = sum([int(w.sizeHint().width()) for w in controls if w is not None][:5]) + 155
            target_w = max(320, int(max(stage_w + margins.left() + margins.right(), controls_w + margins.left() + margins.right())))
            target_h = max(220, int(margins.top() + stage_h + spacing + controls_h + margins.bottom()))
        else:
            # HOTFIX83: normal-mode width AND height are user-owned. Previous
            # builds still compared the current width with a stale configured
            # width/minimumSizeHint, which could snap the assistant back wider
            # immediately after a manual resize or after Settings -> Apply.
            target_w = int(self.width())
            target_h = int(self.height())

        # Avoid growing past the usable desktop. The stage itself still reserves
        # its lanes, while Qt can compress lower-priority chat content if a very
        # small display cannot physically fit every requested element.
        try:
            available = self.screen().availableGeometry() if self.screen() is not None else None
            if available is not None:
                target_w = min(target_w, max(320, int(available.width() * 0.94)))
                target_h = min(target_h, max(220, int(available.height() * 0.94)))
        except Exception:
            target_w = min(target_w, 1100)
            target_h = min(target_h, 900)

        if abs(self.width() - target_w) > 2 or abs(self.height() - target_h) > 2:
            self.resize(int(target_w), int(target_h))
            if robot_only:
                # One deferred convergence pass accounts for width-driven UI
                # scaling changing margins after the first resize.
                QTimer.singleShot(35, self._fit_robot_info_window)

    def _fit_robot_only_height(self) -> None:
        # Historical method name retained for call sites/tests from older builds.
        self._fit_robot_info_window()

    def _apply_robot_only_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        # "Only robot" is intentionally a presentation mode, not a lock-out
        # mode. Keep the bottom control strip (logs/settings/minimize/close,
        # microphone mute, microphone name/level and size grip) available so
        # the user can always recover settings without relying on the tray.
        for widget in (self.header, self.chat, self.input, self.mic_button, self.send_button):
            widget.setVisible(not enabled)
        if hasattr(self, "layout_edit_handle"):
            self.layout_edit_handle.setVisible((not enabled) and bool(self.config.get("ui.robot_layout_edit_enabled", False)))
        if enabled:
            self.robot.show()
            self.robot.setCursor(Qt.ArrowCursor if self.robot.layout_edit_enabled() else (Qt.OpenHandCursor if bool(self.config.get("ui.drag_enabled", True)) else Qt.ArrowCursor))
        else:
            self.robot.setVisible(bool(self.config.get("ui.show_robot", True)))
            self.robot.setCursor(Qt.ArrowCursor if self.robot.layout_edit_enabled() else (Qt.OpenHandCursor if bool(self.config.get("ui.drag_enabled", True)) else Qt.ArrowCursor))
        # Recalculate spacing after visibility changes; opacity/background stays
        # governed by the ordinary Window/Robot transparency settings.
        self._apply_dynamic_scale()
        if enabled:
            # Fit after Qt has processed the visibility changes so the robot-only
            # window contains no large unused vertical area.
            QTimer.singleShot(0, self._fit_robot_only_height)
        else:
            # Restore the normal chat-window height that robot-only mode never
            # writes back into ui.window_height.
            normal_h = max(360, int(self.config.get("ui.window_height", 476) or 476))
            if abs(self.height() - normal_h) > 2:
                self.resize(self.width(), normal_h)

    def eventFilter(self, watched: Any, event: QEvent) -> bool:  # noqa: N802
        # Be defensive during QWidget construction/destruction: Qt can invoke an
        # installed Python event filter for child/layout events before every UI
        # attribute has been assigned (or while objects are being torn down).
        robot = getattr(self, "robot", None)
        header = getattr(self, "header", None)
        robot_drag = robot is not None and watched is robot
        header_drag = header is not None and watched is header
        chat_resize = getattr(self, "layout_edit_handle", None) is not None and watched is self.layout_edit_handle
        if chat_resize:
            if event.type() == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent) and event.button() == Qt.LeftButton:
                self._chat_resize_start_global_y = event.globalPosition().toPoint().y()
                self._chat_resize_start_height = max(100, self.chat.height())
                return True
            if event.type() == QEvent.Type.MouseMove and isinstance(event, QMouseEvent) and self._chat_resize_start_global_y is not None and self._chat_resize_start_height is not None and event.buttons() & Qt.LeftButton:
                delta_y = event.globalPosition().toPoint().y() - self._chat_resize_start_global_y
                max_chat = self._max_chat_height_for_current_window()
                target = max(100, min(max_chat, self._chat_resize_start_height - delta_y))
                self.chat.setMinimumHeight(target); self.chat.setMaximumHeight(target)
                self.robot.updateGeometry(); self.robot.update()
                return True
            if event.type() == QEvent.Type.MouseButtonRelease and self._chat_resize_start_global_y is not None:
                self._chat_resize_start_global_y = None; self._chat_resize_start_height = None
                chat_height = int(self.chat.height())
                self._persist_ui_layout_value("chat_height_px", chat_height)
                self._sync_open_settings_layout(chat_height=chat_height)
                QTimer.singleShot(0, self._fit_robot_info_window)
                return True
        if robot_drag and event.type() == QEvent.Type.ContextMenu:
            self._show_main_context_menu()
            return True
        if robot_drag and robot is not None and robot.layout_edit_enabled() and event.type() in {QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease}:
            # Let RobotWidget consume its own drag/resize gestures instead of
            # moving the whole frameless window.
            return False
        if header_drag or robot_drag:
            drag_enabled = bool(self.config.get("ui.drag_enabled", True))
            if event.type() == QEvent.Type.MouseButtonPress:
                mouse = event
                if isinstance(mouse, QMouseEvent) and mouse.button() == Qt.LeftButton and drag_enabled:
                    self._drag_start_global = mouse.globalPosition().toPoint()
                    self._drag_start_window = self.pos()
                    watched.setCursor(Qt.ClosedHandCursor)
                    return True
            elif event.type() == QEvent.Type.MouseMove:
                mouse = event
                if (
                    isinstance(mouse, QMouseEvent)
                    and self._drag_start_global is not None
                    and self._drag_start_window is not None
                    and drag_enabled
                    and mouse.buttons() & Qt.LeftButton
                ):
                    delta = mouse.globalPosition().toPoint() - self._drag_start_global
                    self.move(self._drag_start_window + delta)
                    return True
            elif event.type() == QEvent.Type.MouseButtonRelease:
                self._drag_start_global = None
                self._drag_start_window = None
                watched.setCursor(Qt.OpenHandCursor if drag_enabled else Qt.ArrowCursor)
                return True
        return super().eventFilter(watched, event)

    def _show_main_context_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("Настройки", self._open_settings)
        menu.addAction("Скрыть", self.hide)
        menu.addSeparator()
        menu.addAction("Полностью завершить", self._quit_application)
        menu.exec(QCursor.pos())

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        del event
        self._show_main_context_menu()

    def _update_window_mask(self) -> None:
        # The window is permanently per-pixel translucent. A native QWidget mask
        # is unnecessary and on Windows can produce repaint artefacts while the
        # frameless window is being resized. Rounded clipping is drawn by
        # RoundedSurface itself, so keep the native window unmasked.
        if not self.mask().isEmpty():
            self.clearMask()

    def _announce_settings_recovery183(self) -> None:
        """NEO 183: tell the user in the chat when damaged settings were recovered at start."""
        config = self.config
        backup = str(getattr(config, "recovered_settings_backup", "") or "")
        repaired = list(getattr(config, "repaired_settings", []) or [])
        if not backup and not repaired:
            return
        code = self._language_code()
        if backup:
            rows = {
                "ru": "Файл настроек был повреждён, поэтому NEO запущен с настройками по умолчанию. Старый файл сохранён: {path}",
                "en": "The settings file was damaged, so NEO started with the default settings. The old file was kept: {path}",
                "de": "Die Einstellungsdatei war beschädigt, daher startete NEO mit den Standardeinstellungen. Die alte Datei wurde aufbewahrt: {path}",
                "fr": "Le fichier des réglages était endommagé : NEO a démarré avec les réglages par défaut. L’ancien fichier est conservé : {path}",
                "es": "El archivo de ajustes estaba dañado, así que NEO se inició con los ajustes predeterminados. El archivo antiguo se guardó: {path}",
                "it": "Il file delle impostazioni era danneggiato, quindi NEO è partito con le impostazioni predefinite. Il vecchio file è stato conservato: {path}",
                "pt": "O ficheiro de definições estava danificado, por isso o NEO iniciou com as definições predefinidas. O ficheiro antigo foi guardado: {path}",
            }
            recovered_from = str(getattr(config, "recovered_settings_source", "") or "")
            default_path = str(getattr(config, "default_settings_path", "") or "")
            if recovered_from and recovered_from != default_path:
                rows = {
                    "ru": "Повреждённые настройки восстановлены из сохранённой копии. Повреждённый файл сохранён: {path}",
                    "en": "Damaged settings were restored from a saved copy. The damaged file was kept: {path}",
                    "de": "Beschädigte Einstellungen wurden aus einer gespeicherten Kopie wiederhergestellt. Die beschädigte Datei wurde aufbewahrt: {path}",
                    "fr": "Les réglages endommagés ont été restaurés depuis une copie sauvegardée. Le fichier endommagé est conservé : {path}",
                    "es": "Los ajustes dañados se restauraron desde una copia guardada. El archivo dañado se conservó: {path}",
                    "it": "Le impostazioni danneggiate sono state ripristinate da una copia salvata. Il file danneggiato è stato conservato: {path}",
                    "pt": "As definições danificadas foram restauradas de uma cópia guardada. O ficheiro danificado foi preservado: {path}",
                }
            text = rows.get(code, rows["en"]).format(path=backup)
        else:
            rows = {
                "ru": "Некоторые настройки имели неверное значение и были возвращены к стандартным: {names}",
                "en": "Some settings had an invalid value and were reset to their defaults: {names}",
                "de": "Einige Einstellungen hatten einen ungültigen Wert und wurden zurückgesetzt: {names}",
                "fr": "Certains réglages avaient une valeur invalide et ont été réinitialisés : {names}",
                "es": "Algunos ajustes tenían un valor no válido y se restablecieron: {names}",
                "it": "Alcune impostazioni avevano un valore non valido e sono state ripristinate: {names}",
                "pt": "Algumas definições tinham um valor inválido e foram repostas: {names}",
            }
            text = rows.get(code, rows["en"]).format(names=", ".join(repaired[:8]))
        try:
            self._append_assistant(text, mirror_telegram=False, localize=False)
        except Exception:
            logger.debug("Could not show the settings recovery notice", exc_info=True)

    def _place_drag_handle183(self) -> None:
        header = getattr(self, "header", None)
        surface = getattr(self, "surface", None)
        if header is None or surface is None:
            return
        top = self._outer_layout.contentsMargins().top() if hasattr(self, "_outer_layout") else 8
        height = max(14, top + 10)
        header.setFixedHeight(height)
        header.setGeometry(0, 0, surface.width(), height)
        header.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_window_mask()
        self._place_drag_handle183()
        if bool(getattr(self, "_ui_ready", False)):
            self._apply_dynamic_scale()
            self._position_timer_stage_label()
            self._sync_open_settings_layout(window_width=self.width(), window_height=self.height())
        self._geometry_timer.start()

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self._geometry_timer.start()

    def _language_code(self) -> str:
        return language_code(self.config)

    def _localize_text(self, text: str) -> str:
        return localize_runtime_text(str(text or ""), self._language_code())

    def _refit_status_text(self) -> None:
        status = getattr(self, "status", None)
        if status is None:
            return
        text = str(status.text() or "")
        scale = max(0.72, min(1.70, float(getattr(self, "_scale", 1.0))))
        base_px = max(8, int(12 * scale))
        available = max(56, int(status.width() or status.maximumWidth() or 120) - 6)
        font = status.font()
        font.setPixelSize(base_px)
        # The active microphone name must stay complete. Shrink only as much as
        # needed to fit the actual layout slot instead of eliding "HyperX SoloCast".
        minimum = max(7, int(8 * scale))
        while font.pixelSize() > minimum and QFontMetrics(font).horizontalAdvance(text) > available:
            font.setPixelSize(font.pixelSize() - 1)
        status.setFont(font)

    def _make_launcher_logs_icon(self, visible: bool) -> QIcon:
        """Terminal/log icon (NEO 180: vector, no emoji fonts)."""
        theme = getattr(self, "_theme_cache", {}) or {}
        base = QColor(str(theme.get("text_color") or "#EAF2FF"))
        accent = QColor(str(theme.get("accent") or "#57D7FF"))
        return _paint_icon177("logs_on" if visible else "logs", base, accent)

    def _refresh_launcher_logs_icon(self) -> None:
        button = getattr(self, "logs_button", None)
        if button is None:
            return
        visible = bool(getattr(self, "log_viewer", None) and self.log_viewer.isVisible())
        button.setEnabled(True)
        button.setText("")
        button.setIcon(self._make_launcher_logs_icon(visible))
        tooltip = "Скрыть логи" if visible else "Показать логи"
        button.setToolTip(self._localize_text(tooltip))

    def _toggle_launcher_logs(self) -> None:
        viewer = getattr(self, "log_viewer", None)
        if viewer is None:
            return
        if viewer.isVisible():
            viewer.hide()
        else:
            viewer.show()
            viewer.raise_()
            viewer.activateWindow()
        self._refresh_launcher_logs_icon()

    def hide_launcher_console(self) -> None:
        """Compatibility hook: hide the in-app log viewer if it is open."""
        viewer = getattr(self, "log_viewer", None)
        if viewer is not None:
            viewer.hide()
        self._refresh_launcher_logs_icon()

    def _make_microphone_icon(self, muted: bool) -> QIcon:
        # Drawn, not an emoji glyph (those render as an empty square on some Windows font stacks).
        theme = getattr(self, "_theme_cache", {}) or {}
        base = QColor(str(theme.get("text_color") or "#EAF2FF"))
        accent = QColor(str(theme.get("accent") or "#57D7FF"))
        return _paint_icon177("mic_muted" if muted else "mic", base, accent)

    def _refresh_mic_mute_icon(self) -> None:
        button = getattr(self, "mic_mute_button", None)
        if button is None:
            return
        button.setText("")
        button.setIcon(self._make_microphone_icon(bool(getattr(self, "_app_mic_muted", False))))

    def _robot_state_changed177(self, state: str) -> None:
        dot = getattr(self, "status_dot", None)
        if dot is not None:
            dot.set_state(state)

    def _refresh_window_icons177(self) -> None:
        """NEO 177: crisp vector icons in the theme colours for every main-window button."""
        theme = getattr(self, "_theme_cache", {}) or {}
        base = QColor(str(theme.get("text_color") or "#EAF2FF"))
        muted = QColor(str(theme.get("muted") or "#8F9DB2"))
        accent = QColor(str(theme.get("accent") or "#57D7FF"))
        for button, kind in ((getattr(self, "settings_button", None), "settings"),
                             (getattr(self, "minimize_button", None), "minimize"),
                             (getattr(self, "close_button", None), "close")):
            if button is not None:
                button.setText("")
                button.setIcon(_paint_icon177(kind, base))
        field = getattr(self, "input", None)
        if isinstance(field, CommandLineEdit):
            idle = QColor(muted); idle.setAlpha(120)
            field.set_icons177(_paint_icon177("mic", base), _paint_icon177("stop", base, QColor("#FF5B6A")),
                               _paint_icon177("send", base, idle), _paint_icon177("send", base, accent))
        dot = getattr(self, "status_dot", None)
        if dot is not None:
            dot.set_accent(accent.name())
            dot.set_state(self.robot.state() if hasattr(self, "robot") else "idle")

    def _set_status(self, text: str, *, scenario_event: dict | None = None) -> None:
        self._status_scenario_event184 = dict(scenario_event) if scenario_event is not None else None
        if scenario_event is not None:
            from jarvis.ui.scenario_strings import st
            state = scenario_event.get("state", "")
            if state == "step_finished" and scenario_event.get("success") is False:
                state = "failed"
            # Translate only the state, retaining the scenario name as literal data.
            text = f"{scenario_event.get('name', '')} · {scenario_event.get('step_index', 0)}/{scenario_event.get('total', 0)} · {st(state, self._language_code())}"
            value = text
        else:
            # NEO 182: the sleep status is built in the UI language (the wake phrase is user data).
            value = self._sleep_status182() if text is _SLEEP_STATUS182 else self._localize_text(str(text or ""))
        self._status_source = str(text or "")
        # Audio-level events can arrive many times per second while the microphone
        # is armed. Re-setting identical QLabel text re-runs font fitting/layout and
        # schedules another fit pass, so treat status updates as idempotent.
        if self.status.text() == value:
            return
        self.status.setText(value)
        self._refit_status_text()
        QTimer.singleShot(0, self._refit_status_text)

    def _apply_language(self) -> None:
        code = self._language_code()
        from jarvis.ui.qt_localization import install_ui_language
        install_ui_language(QApplication.instance(), code)
        try:
            # An open Settings dialog is a child of this window but translates itself on Apply.
            translate_widget_tree(self, code, exclude=(getattr(self, "_settings_dialog", None),))
        except Exception:
            logger.debug("Main window localization failed", exc_info=True)
        try:
            self.chat.set_language(code)
        except Exception:
            logger.debug("Chat language update failed", exc_info=True)
        if hasattr(self, "status"):
            source = getattr(self, "_status_source", self.status.text())
            self._set_status(_SLEEP_STATUS182 if source == _SLEEP_STATUS182 else source,
                             scenario_event=getattr(self, "_status_scenario_event184", None))
        if getattr(self, "bookshelf", None) is not None:
            self.bookshelf.retranslate(code)
        viewer = getattr(self, "log_viewer", None)
        if viewer is not None and hasattr(viewer, "set_language"):
            viewer.set_language(code)          # NEO 183: the log window follows the language too
        tray = getattr(self, "tray", None)
        if tray and tray.contextMenu():
            for action in tray.contextMenu().actions():
                key = action.property("desktop_key")
                if key:
                    action.setText(dt(key, code))

    def _on_telegram_event(self, text: str) -> None:
        """Mirror remote Telegram actions into the local NEO conversation."""
        value = str(text or "").strip()
        if not value:
            return
        try:
            # The Telegram transport has already sent this result to the phone.
            # Show it locally, but do not echo it back into Telegram a second time.
            from jarvis.integrations.telegram_remote import TELEGRAM_DIALOGUE_MARKER, TELEGRAM_SUBMISSION_MARKER
            if value.startswith(TELEGRAM_SUBMISSION_MARKER):
                self.chat.scroll_to_bottom(force=True)
                return
            if value.startswith(TELEGRAM_DIALOGUE_MARKER):
                # «Telegram команда "…"» / «Я ответил "…"»: already in the interface language
                self._append_assistant(value[len(TELEGRAM_DIALOGUE_MARKER):], mirror_telegram=False, localize=False)
            else:
                self._append_assistant(value, mirror_telegram=False)
        except Exception:
            logger.debug("Could not mirror Telegram event into chat", exc_info=True)

    def _on_game_event(self, text: str) -> None:
        """Show game-session detection notices in the local chat."""
        value = str(text or "").strip()
        if not value:
            return
        try:
            bubble = self._append_assistant(value)
            bubble.set_game_message(True)
        except Exception:
            logger.debug("Could not show game event in chat", exc_info=True)

    def _on_match_notice(self, text: str) -> None:
        """A game found a match: chat line, tray balloon and a short spoken notice.

        Telegram gets its own message with an «Accept» button from the watcher,
        so this chat line is not mirrored a second time.
        """
        value = str(text or "").strip()
        if not value:
            return
        try:
            bubble = self._append_assistant(value, mirror_telegram=False, paced=False)
            bubble.set_game_message(True)
        except Exception:
            logger.debug("Could not show match notice in chat", exc_info=True)
        try:
            if self.tray.isVisible():
                self.tray.showMessage("NEO", value, QSystemTrayIcon.Information, 10000)
        except Exception:
            logger.debug("Could not show match tray notice", exc_info=True)

    def _append_user(self, text: str, *, scroll: bool = True) -> MessageBubble:
        bubble = self.chat.add_user(text)
        if scroll:
            self.chat.scroll_to_bottom(force=True)
        self._restart_idle_timer()
        return bubble

    def _mirror_assistant_to_telegram(self, text: str) -> None:
        value = str(text or "").strip()
        if not value:
            return
        remote = getattr(self, "telegram_remote", None)
        if remote is not None:
            try:
                remote.mirror_assistant_message(value)
            except Exception:
                logger.debug("Could not mirror assistant message to Telegram", exc_info=True)

    def _append_assistant(
        self, text: str, *, mirror_telegram: bool = True, paced: bool | None = None, localize: bool = True
    ) -> MessageBubble:
        """Append one assistant message through the desktop's single pacing rule.

        Telegram receives the final text immediately.  The desktop chat, when
        character streaming is enabled (the shipped default), reveals *every*
        assistant message through the same user-selected typewriter speed.
        """
        text = self._localize_text(text) if localize else str(text or "")
        use_pacing = bool(self.config.get("ui.character_streaming_enabled", True)) if paced is None else bool(paced)
        bubble = self.chat.add_assistant("" if use_pacing else text)
        self._restart_idle_timer()
        if mirror_telegram:
            self._mirror_assistant_to_telegram(text)
        if use_pacing and text:
            self._ambient_typing_queue.append((bubble, text))
            self._start_interrupt_wake_monitor(self._agent_generation)
            if not self._busy and not self._typing_timer.isActive() and not self._ambient_typing_timer.isActive():
                self._ambient_typing_timer.start()
        return bubble

    def _ambient_typewriter_tick(self) -> None:
        # The main Agent response owns the foreground reading lane. Queue passive
        # notifications behind it and stop this timer completely while the Agent
        # is typing, so unified pacing does not add a new 120-Hz idle wakeup.
        if self._busy or self._typing_timer.isActive():
            # The foreground response owns its own output state from now on.
            self._ambient_robot_state159 = None
            self._ambient_typing_timer.stop()
            return
        if self._ambient_typing_bubble is None:
            while self._ambient_typing_queue:
                bubble, text = self._ambient_typing_queue.popleft()
                try:
                    # Accessing a deleted Qt wrapper raises RuntimeError.
                    bubble.objectName()
                except RuntimeError:
                    continue
                self._ambient_typing_bubble = bubble
                self._ambient_typing_text = str(text or "")
                self._ambient_typing_index = 0
                break
            if self._ambient_typing_bubble is None:
                self._ambient_typing_timer.stop()
                ambient_reply_activity(self, False)
                return

        ambient_reply_activity(self, True)
        desired_ms = max(1, min(120, int(self.config.get("ui.typewriter_interval_ms", 8) or 8)))
        fps = int(self.config.get("ui.animation_fps", 60) or 60)
        if fps not in {30, 60}:
            fps = 60
        frame_ms = {30: 33, 60: 16}[fps]
        chars = max(1, int(round(frame_ms / desired_ms))) if desired_ms < frame_ms else 1
        self._ambient_typing_index = min(
            len(self._ambient_typing_text), self._ambient_typing_index + chars
        )
        bubble = self._ambient_typing_bubble
        try:
            self.chat.update_bubble(bubble, self._ambient_typing_text[: self._ambient_typing_index])
        except RuntimeError:
            self._ambient_typing_index = len(self._ambient_typing_text)
        if self._ambient_typing_index >= len(self._ambient_typing_text):
            self._ambient_typing_bubble = None
            self._ambient_typing_text = ""
            self._ambient_typing_index = 0
            if not self._ambient_typing_queue:
                self._ambient_typing_timer.stop()
                ambient_reply_activity(self, False)

    def _show_listening_bubble(self) -> None:
        # One ephemeral assistant-side bubble owns the complete voice pre-response
        # lifecycle.  Reuse it instead of deleting/recreating rows so the user sees
        # an immediate, stable ``Слушаю`` -> ``Думаю`` transition.
        if self._listening_bubble is None:
            self._listening_bubble = self._append_assistant("Слушаю", mirror_telegram=False, paced=False)
        else:
            self.chat.update_bubble(self._listening_bubble, "Слушаю")

    def _show_thinking_bubble(self) -> None:
        if self._listening_bubble is None:
            self._listening_bubble = self._append_assistant("Думаю", mirror_telegram=False, paced=False)
        else:
            self.chat.update_bubble(self._listening_bubble, "Думаю")

    def _clear_listening_bubble(self) -> None:
        bubble = self._listening_bubble
        self._listening_bubble = None
        if bubble is not None:
            try:
                self.chat.remove_bubble(bubble)
            except Exception:
                logger.debug("Could not remove listening acknowledgement", exc_info=True)

    def _append_tool_details(self, details: list[str] | None) -> None:
        if details and bool(self.config.get("ui.show_tool_details", False)):
            self.chat.add_tool_details([self._localize_text(x) for x in details])

    def _set_busy(
        self,
        busy: bool,
        status: str = "Ожидание",
        state: str = "idle",
        *,
        allow_mic_stop: bool = False,
    ) -> None:
        self._busy = busy
        # Text input and Settings remain responsive while a worker is thinking. A
        # submitted replacement text cancels the stale generation first.
        self.input.setEnabled(True)
        self.send_button.setEnabled(True)
        voice_enabled = bool(self.config.get("voice.enabled", True))
        self.mic_button.setEnabled(((not busy and voice_enabled) or allow_mic_stop) and not self._app_mic_muted)
        if self.reset_button is not None:
            self.reset_button.setEnabled(not busy)
        self.settings_button.setEnabled(self._voice_mode != "command")
        self._set_status(status)
        self.robot.set_state(state)
        if state != "listening":
            self.mic_level.hide()
            self.mic_level.setValue(0)
        self._restart_idle_timer()

    def _recall_input_history(self, direction: int) -> None:
        history = self._input_history
        if not history:
            return
        if direction < 0:
            if self._input_history_index >= len(history):
                self._input_history_draft = self.input.text()
            self._input_history_index = max(0, self._input_history_index - 1)
            self.input.setText(history[self._input_history_index])
        else:
            self._input_history_index = min(len(history), self._input_history_index + 1)
            if self._input_history_index >= len(history):
                self.input.setText(self._input_history_draft)
            else:
                self.input.setText(history[self._input_history_index])
        self.input.setCursorPosition(len(self.input.text()))

    def _send_from_input(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        if not self._input_history or self._input_history[-1] != text:
            self._input_history.append(text)
            del self._input_history[:-100]
        self._input_history_index = len(self._input_history)
        self._input_history_draft = ""
        self.input.clear()
        if self._busy:
            self._interrupt_current_operation(command=text, source="text-replacement")
            return
        self._send_text(text)

    def _suppress_media_notice(self, source: str, seconds: float = 20.0) -> None:
        key = str(source or "").strip().casefold()
        if key:
            self._media_notice_suppressed_until[key] = max(
                float(self._media_notice_suppressed_until.get(key, 0.0) or 0.0),
                time.monotonic() + max(0.5, float(seconds)),
            )

    def _arm_media_notice_suppression_for_text(self, text: str) -> None:
        value = normalize_multilingual_command(str(text or ""), self._language_code()).casefold()
        if not value:
            return
        media_cues = (
            "музык", "трек", "песн", "плейлист", "моя волна", "мою волн",
            "мои трек", "яндекс", "лайк", "дизлайк", "следующ", "дальше",
            "предыдущ", "пауза", "продолж", "воспроизвед", "music", "track",
            "song", "playlist", "my wave", "like", "dislike", "next", "previous",
            "pause", "resume", "play ",
        )
        if not any(cue in value for cue in media_cues):
            return
        if "discord" in value or "дискорд" in value:
            self._suppress_media_notice("discord")
        else:
            # NEO's domain rule: personal/local music without an explicit Discord
            # word belongs to the desktop Yandex player.
            self._suppress_media_notice("yandex_music")
            self._suppress_media_notice("yandex")

    def _set_runtime_topmost(self, enabled: bool, *, hide: bool = False) -> bool:
        """Lower or raise once, preserving native identity and window state."""
        from shiboken6 import isValid
        if getattr(self, "_quitting", False) or not isValid(self):
            return False
        show_overlay = bool(enabled) and not bool(hide)
        if QThread.currentThread() != self.thread():
            from jarvis.runtime.self_window184 import queue_visibility
            return queue_visibility("show" if show_overlay else "hide")
        # This legacy name remains for the hotkey/theme callers. Visibility is
        # never a sticky topmost mode, and cannot make theme updates re-raise us.
        self._runtime_topmost = False
        if not show_overlay:
            # HWND_BOTTOM also removes native topmost status. Never hide,
            # minimize, activate another app, or change Qt flags (which can
            # recreate the HWND and invalidate global hotkey registrations).
            if os.name == "nt":
                try:
                    import ctypes
                    from ctypes import wintypes
                    hwnd = wintypes.HWND(int(self.winId()))
                    HWND_BOTTOM = wintypes.HWND(1)
                    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
                    lowered = bool(ctypes.windll.user32.SetWindowPos(
                        hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                        SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE))
                    if lowered:
                        self._runtime_demoted184 = True
                    return lowered
                except Exception:
                    logger.debug("Could not lower assistant native window", exc_info=True)
                    return False
            self.lower()
            self._runtime_demoted184 = True
            return True
        self._runtime_demoted184 = False
        previous_no_activate = self.testAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        try:
            if self.isMinimized():
                # Clear only minimized state; preserve a previously maximized size.
                self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
            self.show()
            if os.name == "nt":
                try:
                    import ctypes
                    from ctypes import wintypes
                    user32 = ctypes.windll.user32
                    hwnd = wintypes.HWND(int(self.winId()))
                    HWND_TOP = wintypes.HWND(0)
                    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0010, 0x0040
                    flags = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW
                    # Qt has restored the prior state above; SW_SHOWNA retains
                    # its current (possibly maximized) geometry without focus.
                    user32.ShowWindow(hwnd, 8)  # SW_SHOWNA
                    if user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0, flags):
                        return True
                except Exception:
                    logger.debug("Could not raise assistant native window", exc_info=True)
            self.raise_()
            return True
        finally:
            # The one-shot no-focus request must not leak into later ordinary
            # tray/taskbar restores or change their normal activation behavior.
            self.setAttribute(Qt.WA_ShowWithoutActivating, previous_no_activate)

    def _try_local_voice_control(self, text: str) -> str | None:
        from jarvis.runtime.self_window184 import configured_action, visibility_reply
        action = configured_action(self.config, text)
        if action is not None:
            enabled = bool(self.config.get("ui.voice_window_visibility_commands_enabled", True))
            accepted = enabled and self._set_runtime_topmost(action == "show", hide=action == "hide")
            return visibility_reply(action, self._language_code(), enabled=enabled, accepted=accepted)
        value = normalize_multilingual_command(str(text or ""), self._language_code()).casefold().replace("ё", "е")
        compact = " ".join(value.split())
        if "автоклик" not in compact:
            return None
        from jarvis.runtime.self_window184 import autoclicker_request_shape
        shape = autoclicker_request_shape(str(text or ""), compact)
        if shape == "negated":
            # HIST-046: «не включай автокликер» never starts (or stops) it.
            from jarvis.runtime.natural_commands import locale_message
            return locale_message("negated", self._language_code())
        if shape != "command":
            return None   # HIST-047: quoted/dictated/discussed text is data, not an order
        try:
            from jarvis.runtime.autoclicker import autoclicker_controller
            ctl = autoclicker_controller(self.config)
            stopping = any(x in compact for x in ("выключ", "останов", "стоп", "отключ"))
            if not bool(self.config.get("autoclicker.enabled", True)) and not stopping:
                # HIST-048: switching the feature off forbids new runs; stopping a
                # running autoclicker always stays available.
                return "Автокликер отключён в разделе «Дополнительно»."
            button = None
            if any(x in compact for x in ("правой", "правая", "правую")): button = "right"
            elif any(x in compact for x in ("средней", "средняя", "среднюю")): button = "middle"
            elif any(x in compact for x in ("левой", "левая", "левую")): button = "left"
            match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:клик|нажат)", compact)
            cps = float(match.group(1).replace(",", ".")) if match else None
            if stopping:
                data = ctl.stop()
            elif any(x in compact for x in ("включ", "запуст")):
                data = ctl.start(button=button, cps=cps)
            elif cps is not None or button is not None:
                data = ctl.configure(button=button, cps=cps)
            else:
                data = ctl.status()
            button_name = {"left": "левой", "right": "правой", "middle": "средней"}.get(str(data.get("button")), "левой")
            state = "включён" if data.get("active") else "выключен"
            return f"Автокликер {state}: {float(data.get('cps', 10.0)):g} кликов в секунду, {button_name} кнопкой мыши."
        except Exception as exc:
            return f"Не удалось изменить автокликер: {exc}"

    def _send_text(
        self, text: str, *, user_already_shown: bool = False, input_source: str = "text",
        raw_text: str = "", user_submission: bool = True, delayed_action_id184: str = "",
    ) -> None:
        # Viewport intent is separate from command provenance/authorization.
        # Scheduled commands remain executable text input without moving history.
        source_name = normalize_input_source(input_source)
        if not is_executable_input_source(source_name):
            # Defense in depth: even a future integration bug cannot promote
            # loopback/system/ambient observations into a visible user command.
            logger.warning("Blocked non-executable input at UI command boundary: source=%s text=%r", source_name, str(text or "")[:160])
            return
        input_source = source_name
        self._clarification_voice_origin156 = input_source == "voice"
        if self._response_active() and self._strict_stop_phrase(text):
            self._interrupt_current_operation(source=input_source + '-stop')
            return
        response_voice = getattr(self, "_response_voice_service", None)
        if response_voice is not None:
            response_voice.cancel()
        from jarvis.runtime.voice_followup156 import pending_slot
        awaiting_slot = pending_slot(getattr(self, 'agent', None), self._language_code())
        from jarvis.runtime.multilingual_commands import gui_fast_path
        with gui_fast_path():                     # HIST-023: no cold catalogue build on the Qt thread
            local_reply = self._try_local_voice_control(raw_text or text) if awaiting_slot is None and not delayed_action_id184 else None
        if local_reply is not None:
            self._pause_wake_listening()
            if not user_already_shown:
                if user_submission:
                    self._append_user(text)
                else:
                    self._append_user(text, scroll=False)
            reply_text = self.agent.personalize_external_response(local_reply)
            if input_source == "voice" and self._listening_bubble is not None:
                bubble = self._listening_bubble
                self._listening_bubble = None
                self.chat.update_bubble(bubble, reply_text)
            else:
                self._append_assistant(reply_text)
            speak = getattr(self, "_queue_response_voice", None)
            if callable(speak):
                speak(reply_text)
            self._set_status("Ожидание")
            self._rearm_voice_after_answer()
            return
        self._cancel_command_asr157()
        self._pause_wake_listening()
        self._active_user_command_text = str(text or "")
        self._arm_media_notice_suppression_for_text(text)
        cancellation_generation = cancellation_manager().reset()
        self._agent_generation += 1
        generation = self._agent_generation
        trace_id = str(getattr(self, "_last_voice_correlation", "")) if input_source == "voice" else ""
        trace_id = trace_id or uuid.uuid4().hex
        self._ui_turn_trace = (generation, trace_id, time.monotonic())
        if not user_already_shown:
            if user_submission:
                self._append_user(text)
            else:
                self._append_user(text, scroll=False)
        if input_source == "voice" and self._listening_bubble is not None:
            # Adopt the existing voice-status row as the real response bubble.
            # From this point streaming/tool output replaces ``Думаю`` in-place.
            self._pending_response_bubble = self._listening_bubble
            self._listening_bubble = None
            self.chat.update_bubble(self._pending_response_bubble, "Думаю")
        else:
            self._pending_response_bubble = self.chat.add_assistant_pending(tr("Думает…", self._language_code()))
        state, status = self._intent_animation(text)  # Cosmetic only; never run the full normalizer in Qt.
        self._set_busy(True, status, state)
        self._stream_flush_timer.stop()
        self._stream_text = ""
        self._stream_pending_text = ""
        self._stream_thinking_text = ""
        self._stream_received = False
        self._thinking_received = False
        self._response_briefing158 = None
        reply_activity = getattr(self.robot, 'set_reply_activity', None)
        if callable(reply_activity):
            reply_activity(False)
        worker = FunctionWorker(
            self._run_agent_stream,
            text,
            generation,
            str(input_source or "text"),
            cancellation_generation,
            str(raw_text or text),
            trace_id,
            delayed_action_id184,
            progress_keyword="on_event",
        )
        worker.signals.progress.connect(self._stream_response_event)
        worker.signals.result.connect(self._handle_agent_result)
        worker.signals.error.connect(lambda trace, g=generation: self._handle_agent_error(g, trace))
        self.thread_pool.start(worker)
        if bool(self.config.get("voice.interrupt_on_wake_while_busy", True)):
            QTimer.singleShot(120, lambda g=generation: self._start_interrupt_wake_monitor(g))

    @staticmethod
    def _intent_animation(text: str) -> tuple[str, str]:
        lowered = text.lower()
        if any(word in lowered for word in ("на экране", "скриншот", "изображен", "переведи текст")):
            return "scanning", "Сканирую и анализирую экран…"
        if any(word in lowered for word in ("загугл", "найди в интернете", "поищи", "поиск")):
            return "searching", "Ищу информацию…"
        if any(word in lowered for word in ("браузер", "сайт", "ссылк", "youtube", "гугл")):
            return "browsing", "Работаю с браузером…"
        if any(word in lowered for word in ("прочитай", "документ", "файл текста", "книга")):
            return "reading", "Читаю содержимое…"
        if any(word in lowered for word in ("создай приложение", "напиши код", "проект", "python", "исправь код")):
            return "coding", "Продумываю и создаю код…"
        if any(word in lowered for word in ("открой", "запусти", "перемести", "удали", "измени", "выключ", "громк")):
            return "working", "Выполняю действие на компьютере…"
        # NEO 173: a question that goes to the internet or a web service shows it at once.
        if any(word in lowered for word in ("почт", "письм", "писем", "непрочитан", "календар", "событи", "погод", "mail", "calendar", "weather")):
            return "searching", "Спрашиваю сервис…"
        try:
            from jarvis.runtime.web_answers171 import classify
            if classify(text) == "web":
                return "searching", "Ищу в интернете…"
        except Exception:
            pass
        return "thinking", "Думаю…"

    def _run_agent_stream(
        self, text: str, generation: int, input_source: str = "text",
        cancellation_generation: int = 0, raw_text: str = "", correlation_id: str = "", delayed_action_id184: str = "", *, on_event
    ) -> tuple[int, AgentTurn]:
        # Worker-pool threads are reused. Bind the exact generation created by
        # _send_text so a later command can never resurrect a cancelled old turn.
        cancellation_manager().bind(int(cancellation_generation))
        cancellation_manager().raise_if_cancelled()
        from jarvis.runtime.media_events import capture_media_events, command_result_owns_media_notice
        from jarvis.runtime.briefing_stream import stream_to
        from types import SimpleNamespace
        cancelled = SimpleNamespace(is_set=cancellation_manager().is_cancelled)
        with capture_media_events() as media_events, command_result_owns_media_notice(), stream_to(
            lambda event: on_event(dict(event, type='briefing', generation=generation)), cancelled
        ):
            try:
                if delayed_action_id184:
                    turn = self.agent.run_delayed_action184(delayed_action_id184,
                        on_text=lambda chunk: on_event({"type": "content", "text": chunk, "generation": generation}))
                else:
                    turn = self.agent.handle_stream(
                        text,
                        on_text=lambda chunk: on_event({"type": "content", "text": chunk, "generation": generation}),
                        on_thinking=lambda chunk: on_event({"type": "thinking", "text": chunk, "generation": generation}),
                        input_source=str(input_source or "text"),
                        raw_user_text=str(raw_text or text),
                        correlation_id=correlation_id,
                    )
            finally:
                if media_events:
                    # Keep committed player changes even if an answer is cancelled
                    # after the side effect. A later UI turn never owns this reply.
                    pending = getattr(self, "_pending_media_results157", None)
                    if pending is None:
                        pending = self._pending_media_results157 = {}
                    pending[generation] = tuple(media_events)
                    for old in sorted(pending)[:-16]:
                        pending.pop(old, None)
        return generation, turn

    def _run_confirm_pending_stream(self, approved: bool, generation: int, confirmation_id: str = "", *, on_event) -> tuple[int, AgentTurn]:
        if confirmation_id and generation != self._agent_generation:
            from jarvis.runtime.streaming_commands184 import live_confirmation_changed_text
            return generation, AgentTurn(live_confirmation_changed_text(self._language_code()),
                success=False, outcome="clarification", tool_details=["obs.confirmation_changed"])
        turn = self.agent.confirm_pending(
            approved,
            on_text=lambda chunk: on_event({"type": "content", "text": chunk, "generation": generation}),
            on_thinking=lambda chunk: on_event({"type": "thinking", "text": chunk, "generation": generation}),
            **({"confirmation_id": confirmation_id} if confirmation_id else {}),
        )
        return generation, turn

    def _handle_agent_result(self, payload: object) -> None:
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        generation, turn = payload
        if int(generation) != self._agent_generation or not isinstance(turn, AgentTurn):
            logger.info("Ignoring stale agent result generation=%s current=%s", generation, self._agent_generation)
            self._recover_media_notice157(generation)
            return
        trace_data = getattr(self, "_ui_turn_trace", None)
        if trace_data and trace_data[0] == generation:
            latency_profiler().record("dispatch_to_response_ui", time.monotonic() - trace_data[2], correlation_id=trace_data[1])
        response_bubble = self._pending_response_bubble
        self._handle_agent_turn(turn)
        for event in getattr(self, "_pending_media_results157", {}).pop(generation, ()):
            if event.playing and event.reason not in {"queue_added", "queue_updated", "network_unavailable"}:
                self._remember_media_bubble157(event, response_bubble, turn.text)

    def _recover_media_notice157(self, generation: int) -> None:
        """A cancelled answer cannot hide a track that actually started playing."""
        from jarvis.runtime.media_events import latest_now_playing
        from jarvis.runtime.media_notice156 import now_playing_notice
        events = getattr(self, "_pending_media_results157", {}).pop(generation, ())
        delivered = getattr(self, "_recovered_media_notices157", None)
        if delivered is None:
            delivered = self._recovered_media_notices157 = set()
        for event in events:
            if event.reason in {"queue_added", "queue_updated", "network_unavailable"} or not event.playing:
                continue
            current = latest_now_playing(event.source)
            # Compare physical event identity as well as track: an old A→B→A
            # command result is still obsolete even when the same song returns.
            if current is None or not current.playing or current.key != event.key or current.event_id != event.event_id:
                continue
            if event.event_id in delivered:
                continue
            row = getattr(self, "_media_bubbles157", {}).get(event.source)
            if row and row[0] == event.event_id:
                self._refresh_media_bubble_queue157(current)
                continue
            delivered.add(event.event_id)
            if len(delivered) > 128:
                delivered.intersection_update({event.event_id})
            label = localized_source_name(event.source, self._language_code())
            track = f"«{event.title}»" + (f" — {event.artist}" if event.artist else "")
            notice = now_playing_notice(label, track, self._now_playing_queue_preview(event.source), self._language_code())
            bubble = self._append_assistant(notice, paced=False)
            self._remember_media_bubble157(event, bubble, notice)
            self._last_now_playing_by_source[event.source] = event.key

    def _remember_media_bubble157(self, event, bubble, text: str) -> None:
        if bubble is None:
            return
        from jarvis.runtime.media_events import latest_now_playing
        current = latest_now_playing(event.source)
        if current is None or current.event_id != event.event_id:
            return
        rows = getattr(self, "_media_bubbles157", None)
        if rows is None:
            rows = self._media_bubbles157 = {}
        base = str(text or "").splitlines()[0] if str(text or "").strip() else ""
        if str(event.title) not in base:
            from jarvis.runtime.media_notice156 import now_playing_notice
            track = f"«{event.title}»" + (f" — {event.artist}" if event.artist else "")
            base = now_playing_notice(localized_source_name(event.source, self._language_code()), track, "", self._language_code())
        rows[event.source] = (event.event_id, event.key, bubble, base)
        pending = getattr(self, "_deferred_queue_notice157", {}).pop(event.source, None)
        if pending is not None and pending.key == event.key and pending.created_at >= event.created_at:
            self._refresh_media_bubble_queue157(current)

    def _refresh_media_bubble_queue157(self, current, *, preserve_pending: bool = False) -> bool:
        if current.source == "discord" and getattr(self, "_discord_queue_observation_inactive158", False):
            return False
        row = getattr(self, "_media_bubbles157", {}).get(current.source)
        if not row or row[0] != current.event_id or row[1] != current.key:
            return False
        _event_id, _key, bubble, base = row
        queue_preview = (self._now_playing_queue_preview(current.source, refresh=True)
                         if preserve_pending else self._now_playing_queue_preview(current.source))
        if preserve_pending:
            from jarvis.runtime.media_notice156 import queue_notice
            reader = getattr(self, "_media_queue_reader158", None)
            if (reader is not None and reader.is_pending(current.source)
                    and queue_preview == queue_notice(None, self._language_code())):
                # A repeated observation is not evidence that a known queue was
                # lost. Keep the same playback's existing text until this read
                # completes. Real queue edits use the normal invalidation path.
                return True
        notice = base + queue_preview
        try:
            self.chat.update_bubble(bubble, notice)
        except RuntimeError:  # The user may have cleared this chat row.
            getattr(self, "_media_bubbles157", {}).pop(current.source, None)
            return False
        # Retire old text buffers so a later typewriter tick cannot restore the
        # earlier 'queue pending' text over this same, now-complete message.
        if bubble is getattr(self, "_pending_response_bubble", None):
            self._typing_text = self._stream_text = notice
            self._typing_index = len(notice)
            self._stream_pending_text = ""
        if bubble is getattr(self, "_ambient_typing_bubble", None):
            self._ambient_typing_text = notice
            self._ambient_typing_index = len(notice)
        queued = getattr(self, "_ambient_typing_queue", None)
        if queued is not None:
            for index, item in enumerate(queued):
                if item[0] is bubble:
                    queued[index] = (bubble, notice)
        return True

    def _handle_agent_error(self, generation: int, trace: str) -> None:
        self._recover_media_notice157(generation)
        if generation != self._agent_generation:
            logger.info("Ignoring stale agent error generation=%s", generation)
            return
        self._handle_error(trace)

    def _stream_response_event(self, event: object) -> None:
        if not isinstance(event, dict) or self._pending_response_bubble is None:
            return
        if int(event.get("generation", self._agent_generation) or 0) != self._agent_generation:
            return
        kind = str(event.get("type", ""))
        text = str(event.get("text", "") or "")
        if not text:
            return
        if kind == 'briefing':
            from jarvis.runtime.briefing_stream import BriefingDocument
            document = getattr(self, '_response_briefing158', None)
            if document is None:
                document = self._response_briefing158 = BriefingDocument(self._language_code())
            if document.update(str(event.get('section', '')), text, str(event.get('state', 'ready')),
                               partial_text=event.get('partial_text')):
                self.chat.collapse_thinking(self._pending_response_bubble)
                # TZ-SUMMARY-01: sections type out at the chosen reply speed.
                _reveal187(self).show(self._pending_response_bubble, document.render())
                if event.get('state', 'ready') == 'ready':
                    show_chunk = getattr(self.robot, 'show_reply_chunk', None)
                    if callable(show_chunk):
                        show_chunk(waiting=any(item[1] == 'waiting' for item in document.sections.values()))
                    else:
                        self.robot.set_state('typing')
                else:
                    self.robot.set_state('thinking')
                self._set_status('Получаю информацию…')
            return
        if kind == "thinking":
            self._thinking_received = True
            self._stream_thinking_text += text
            self.chat.update_thinking(
                self._pending_response_bubble,
                self._stream_thinking_text,
                expanded=not self._stream_received,
            )
            self.robot.set_state("thinking")
            self._set_status("Размышляю…")
            return
        if kind != "content":
            return
        if getattr(self, '_response_briefing158', None) is not None:
            # Agent emits its complete tool answer after section events. Keep
            # the progressive message intact until the authoritative turn.
            return
        # Only actual answer content owns the speech cloud. A later reasoning
        # event can return to the gear; subsequent content must restore it.
        self.robot.set_state("typing")
        paced = bool(self.config.get("ui.character_streaming_enabled", True))
        if not self._stream_received:
            self._stream_received = True
            self._stream_text = ""
            self.chat.collapse_thinking(self._pending_response_bubble)
            self._set_status("Отвечаю…")
            if paced:
                self._typing_text = ""
                self._typing_index = 0
                self._typing_final_state = "idle"
                self._typing_stream_live = True
                self._typing_stream_complete = False
                self.chat.update_bubble(self._pending_response_bubble, "")
        if paced:
            # Preserve the provider/model stream, but decouple *generation speed*
            # from *reading speed*. Incoming chunks fill a buffer; the typewriter
            # timer reveals that buffer at the user-selected character cadence.
            self._stream_text += text
            self._typing_text = self._stream_text
            if not self._typing_timer.isActive():
                self._typing_timer.start()
            return
        self._stream_pending_text += text
        # Show the first visible chunk immediately (TTFT), then coalesce later
        # deltas to ~30 FPS instead of repainting the chat for every model token.
        if not self._stream_text:
            self._flush_response_stream()
        if not self._stream_flush_timer.isActive():
            self._stream_flush_timer.start()

    def _flush_response_stream(self) -> None:
        if self._pending_response_bubble is None or not self._stream_pending_text:
            return
        self._stream_text += self._stream_pending_text
        self._stream_pending_text = ""
        self.chat.update_bubble(self._pending_response_bubble, self._stream_text)

    def _handle_agent_turn(self, turn: AgentTurn) -> None:
        self._flush_response_stream()
        self._stream_flush_timer.stop()
        self._append_tool_details(turn.tool_details)
        is_game_turn = any(str(detail or "").startswith("game_web.") for detail in (turn.tool_details or []))
        if is_game_turn and self._pending_response_bubble is not None:
            # Recipes/walkthroughs/source URLs are intentionally unbounded. The
            # chat viewport scrolls; the game message bubble itself must not crop.
            self._pending_response_bubble.set_game_message(True)
        # Agent/tool boundaries already localize their framing. The final body
        # also contains model prose, quoted content and exact user-owned names.
        # Treat it like the streaming body, not like a legacy system UI label.
        display_text = str(turn.text or "")
        detail_text = " ".join(str(x or "").casefold() for x in (turn.tool_details or []))
        if "yandex_music:" in detail_text:
            self._suppress_media_notice("yandex_music", 3.0)
        if "discord:" in detail_text:
            self._suppress_media_notice("discord", 3.0)
        if turn.pending:
            self.chat.update_bubble(self._pending_response_bubble, display_text)
            self._set_busy(True, "Ожидаю подтверждения…", "alert")
            confirming_generation = self._agent_generation
            if turn.pending.tool_name == "scenario":
                from copy import deepcopy
                from jarvis.runtime.scenarios import find_scenario, scenario_fingerprint
                from jarvis.ui.scenario_confirmation184 import confirm_scenario
                try:
                    args = turn.pending.arguments
                    chosen = deepcopy(find_scenario(self.config, str(args.get("id") or args.get("name") or "")))
                    unchanged = bool(args.get("fingerprint")) and args["fingerprint"] == scenario_fingerprint(chosen)
                    retry = None
                    if args.get("action") == "retry":
                        from jarvis.runtime.scenarios import scenario_retry_request, latest_scenario_result
                        if not unchanged or scenario_retry_request(self.config, args.get("run_id"), args.get("step_index")) != args:
                            raise ValueError("retry_unavailable")
                        record = latest_scenario_result(self.config, args.get("id"))
                        outcome = next(row for row in record["outcomes"] if row["step_index"] == args["step_index"])
                        retry = {**args, "mode": outcome["retry_mode"]}
                    decision = QMessageBox.Yes if unchanged and (confirm_scenario(self, chosen, self._language_code(), retry=retry) if retry else confirm_scenario(self, chosen, self._language_code())) else QMessageBox.No
                except (ValueError, TypeError, KeyError, StopIteration):
                    decision = QMessageBox.No
            else:
                # PHRASE185-CONFIRM-PAYLOAD-01: the summary is already written in the UI
                # language around exact user content («Привет, мир»); it is shown as is.
                decision = QMessageBox.question(
                    self,
                    tr("Подтверждение действия", self._language_code()),
                    str(turn.pending.summary or ""),
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
            if confirming_generation != self._agent_generation or self._pending_response_bubble is None:
                return
            self.chat.update_bubble(self._pending_response_bubble, self._localize_text("Выполняю подтверждённое действие…"))
            self._set_busy(True, "Выполняю подтверждённое действие…", "working")
            self._stream_flush_timer.stop()
            self._stream_text = ""
            self._stream_pending_text = ""
            self._stream_thinking_text = ""
            self._stream_received = False
            self._thinking_received = False
            generation = self._agent_generation
            worker = FunctionWorker(
                self._run_confirm_pending_stream,
                decision == QMessageBox.Yes, generation,
                getattr(turn.pending, "confirmation_id", ""),
                progress_keyword="on_event",
            )
            worker.signals.progress.connect(self._stream_response_event)
            worker.signals.result.connect(self._handle_agent_result)
            worker.signals.error.connect(lambda trace, g=generation: self._handle_agent_error(g, trace))
            self.thread_pool.start(worker)
            return

        # Agent responses update an existing pending bubble instead of going through
        # _append_assistant(), so mirror the authoritative final text here exactly once.
        self._mirror_assistant_to_telegram(display_text)
        speak = getattr(self, "_queue_response_voice", None)
        if callable(speak):
            speak(display_text)
        self.chat.collapse_thinking(self._pending_response_bubble)
        final_state = self._infer_response_emotion(display_text, turn.tool_details)
        if getattr(self, '_response_briefing158', None) is not None:
            # The sections were already readable while their providers worked.
            # Never erase them and re-type the complete briefing from scratch.
            self._response_briefing158.finish()
            bubble = self._pending_response_bubble

            def done(bubble=bubble, final_state=final_state) -> None:
                if self._pending_response_bubble is bubble:
                    self._finish_response_ui(final_state)
            # The visible prefix stays; only the rest of the final text types out.
            _reveal187(self).show(bubble, display_text, on_done=done)
            return
        paced = bool(self.config.get("ui.character_streaming_enabled", True))
        if self._stream_received:
            if paced:
                # Generation/API work is complete, but do not dump a large final
                # answer into the chat. Let the reader-controlled typewriter catch
                # up to the authoritative final text, then finish the response UI.
                self._stream_text = display_text
                self._typing_text = display_text
                self._typing_final_state = final_state
                self._typing_stream_live = True
                self._typing_stream_complete = True
                if self._typing_index >= len(self._typing_text):
                    self._typing_timer.stop()
                    self._finish_typewriter()
                elif not self._typing_timer.isActive():
                    self._typing_timer.start()
            else:
                self.chat.update_bubble(self._pending_response_bubble, display_text)
                self._finish_response_ui(final_state)
        else:
            # Search/API/tool backends often return a complete answer in one event.
            # The same display preference applies to those responses too.
            if paced:
                self._start_typewriter(display_text, final_state)
            else:
                self.chat.update_bubble(self._pending_response_bubble, display_text)
                self._finish_response_ui(final_state)

    def _start_typewriter(self, text: str, final_state: str) -> None:
        if self._pending_response_bubble is None:
            self._pending_response_bubble = self.chat.add_assistant_pending("")
        self._typing_text = str(text)
        self._typing_index = 0
        self._typing_final_state = final_state
        self._typing_stream_live = False
        self._typing_stream_complete = True
        self.chat.update_bubble(self._pending_response_bubble, "")
        self._set_status("Отвечаю…")
        self.robot.set_state("typing")
        self._typing_timer.start()

    def _typewriter_tick(self) -> None:
        if self._pending_response_bubble is None:
            self._typing_timer.stop()
            return
        if self._typing_index >= len(self._typing_text):
            # A live model stream may temporarily be caught up while generation is
            # still running. Stop repainting until the next chunk arrives instead
            # of spinning the timer on an empty buffer.
            if self._typing_stream_live and not self._typing_stream_complete:
                self._typing_timer.stop()
                return
            self._typing_timer.stop()
            self._finish_typewriter()
            return
        desired_ms = max(1, min(120, int(self.config.get("ui.typewriter_interval_ms", 8) or 8)))
        step = max(1, int(round(self._typing_timer.interval() / desired_ms)))
        self._typing_index = min(len(self._typing_text), self._typing_index + step)
        self.chat.update_bubble(
            self._pending_response_bubble,
            self._typing_text[: self._typing_index],
        )

        if self._typing_index >= len(self._typing_text) and self._typing_stream_live and not self._typing_stream_complete:
            self._typing_timer.stop()

    def _finish_typewriter(self) -> None:
        if self._pending_response_bubble is None:
            return
        self.chat.update_bubble(self._pending_response_bubble, self._typing_text)
        self._typing_stream_live = False
        self._typing_stream_complete = False
        self._finish_response_ui(self._typing_final_state)

    def _finish_response_ui(self, final_state: str) -> None:
        reply_activity = getattr(self.robot, 'set_reply_activity', None)
        if callable(reply_activity):
            reply_activity(False)
        self._response_briefing158 = None
        if self._voice_mode == "interrupt_wake" and self.audio_capture.is_recording:
            self.audio_capture.stop(emit_result=False)
            self._voice_mode = ""
            self._interrupt_wake_active = False
        self._stream_flush_timer.stop()
        self._pending_response_bubble = None
        self._stream_text = ""
        self._stream_pending_text = ""
        self._stream_thinking_text = ""
        self._stream_received = False
        self._thinking_received = False
        self._busy = False
        self._active_user_command_text = ""
        self.input.setEnabled(True)
        self.send_button.setEnabled(True)
        self.mic_button.setEnabled(bool(self.config.get("voice.enabled", True)) and not self._app_mic_muted)
        if self.reset_button is not None:
            self.reset_button.setEnabled(True)
        self.settings_button.setEnabled(True)
        self._last_response_state = final_state
        self.robot.set_state(final_state)
        self._set_status("Ожидание")
        if (self._ambient_typing_bubble is not None or self._ambient_typing_queue) and not self._ambient_typing_timer.isActive():
            self._ambient_typing_timer.start()
        if final_state == "dancing":
            # Event-driven dance: no background media polling and therefore no extra
            # permanent CPU cost while a track keeps playing.
            QTimer.singleShot(6500, self._return_robot_to_idle)
        elif final_state == "playful":
            QTimer.singleShot(900, self._start_robot_game_scene)
        elif final_state != "idle":
            QTimer.singleShot(1200, self._return_robot_to_idle)
        # Voice-first overlay must not steal keyboard focus from the active app.
        self._restart_idle_timer()
        self._rearm_voice_after_answer()

    def _rearm_voice_after_answer(self) -> None:
        if self._response_active():
            self._start_interrupt_wake_monitor(self._agent_generation)
            return
        if self._quitting or self._busy:
            return
        arm_followup = getattr(self, '_arm_clarification_listen156', None)
        if callable(arm_followup) and arm_followup():
            return
        if self._continuous_voice_session and bool(self.config.get("voice.enabled", True)) and not self._app_mic_muted:
            rearm_ms = int(self.config.get("voice.continuous_rearm_ms", 35) or 0)
            self._set_status("Диалог активен — говорите. Скажите «стоп», чтобы завершить.")
            QTimer.singleShot(max(0, rearm_ms), lambda: self._start_command_capture(seamless=False) if (not self._busy and not self._quitting and self._continuous_voice_session and not self._app_mic_muted) else None)
        else:
            self._restore_listening_audio()
            rearm_ms = int(self.config.get("voice.post_answer_rearm_ms", 20) or 0)
            self._wake_guard.suppress_for(max(0.02, rearm_ms / 1000.0))
            self._resume_wake_listening(delay_ms=rearm_ms)

    def _silence_retry188(self, recording: RecordingResult) -> bool:
        """NEO 188 NEO-VOICE-002: after CONFIRMED natural silence, ask the same open question once more.

        Only for a voice-started ordinary clarification whose follow-up capture just
        ended with «no_speech_timeout». Stop, mute, errors, an unknown end reason,
        confirmations, continuous mode, a changed/expired question or a used retry
        never re-open the microphone. The TTL is not extended."""
        try:
            from jarvis.runtime import voice_followup156 as followup
            return followup.silence_retry188(self, recording)
        except Exception:
            logger.debug("Silence retry check failed; returning to wake listening", exc_info=True)
            return False

    def _arm_clarification_listen156(self) -> bool:
        """Allow 'Downloads' directly after a voice question, once per turn."""
        if (not getattr(self, '_clarification_voice_origin156', False)
                or self._continuous_voice_session or self._app_mic_muted
                or self._quitting or self._busy
                or self._response_active()
                or not bool(self.config.get('voice.enabled', True))):
            return False
        from jarvis.runtime.voice_followup156 import pending_question
        question = pending_question(self.agent, self._language_code())
        if question is None:
            return False
        stamp = (self._agent_generation, question)
        if getattr(self, '_clarification_armed156', None) == stamp:
            return False
        self._clarification_armed156 = stamp
        # The usual capture has its own silence/max-duration bounds. On silence
        # or cancellation the existing path returns to wake-only monitoring.
        self._start_command_capture(seamless=False)
        return True

    @staticmethod
    def _infer_response_emotion(text: str, details: list[str] | None) -> str:
        lowered = text.lower()
        detail_text = " ".join(str(item or "").lower() for item in (details or []))
        if any(word in lowered for word in ("аварийн", "runtimeerror", "traceback", "критическ")):
            return "error"
        if any(word in lowered for word in ("ошибка", "не удалось", "не могу", "не найден", "не получилось")):
            return "sad"
        media_detail = "yandex_music:" in detail_text or "discord:" in detail_text
        stopped = any(word in (lowered + " " + detail_text) for word in ("пауза", "останов", "стоп", "выключ", "pause", "stop"))
        started = any(word in (lowered + " " + detail_text) for word in ("включ", "запущ", "играет", "воспроиз", "play", "started", "продолж"))
        if media_detail and started and not stopped:
            return "dancing"
        if "game:" in detail_text:
            return "playful"
        if any(word in lowered for word in ("готово", "выполнено", "успеш", "открыто", "создан")) or details:
            return "success"
        if any(word in lowered for word in ("рад", "отлично", "супер", "пожалуйста")):
            return "happy"
        if text.strip().endswith("?"):
            return "curious"
        return "idle"

    def _start_visual_speaking(self, text: str, final_state: str) -> None:
        self._last_response_state = final_state
        self.robot.set_state("speaking")
        duration = max(750, min(4200, 500 + len(text) * 22))
        self._speech_timer.start(duration)

    def _finish_visual_speaking(self) -> None:
        state = self._last_response_state
        self.robot.set_state(state)
        self._set_status("Ожидание")
        if state != "idle":
            QTimer.singleShot(1800, self._return_robot_to_idle)

    def _start_robot_game_scene(self) -> None:
        if self._busy or self._sleeping or self._quitting:
            return
        self.robot.set_state("idle")
        # Game detection no longer forces a dedicated gamepad prop animation.
        # Keep the ordinary idle state and let the normal scene scheduler decide.
        self.robot.update()

    def _return_robot_to_idle(self) -> None:
        foreground_recording = self._voice_mode == "command" and self.audio_capture.is_recording
        if (
            not self._busy
            and not foreground_recording
            and not self._speech_timer.isActive()
            and not self._typing_timer.isActive()
            and not self._sleeping
        ):
            self.robot.set_state("idle")

    def _toggle_app_microphone(self) -> None:
        self._set_app_microphone_muted(not self._app_mic_muted)

    def _set_app_microphone_muted(self, muted: bool) -> None:
        muted = bool(muted)
        if muted == self._app_mic_muted:
            return
        if muted:
            captured = self._voice_mode in {"command", "command_pending"} or getattr(self, "_command_asr_pending157", False)
            self._cancel_command_asr157()
            if captured:
                self._set_busy(False, "Микрофон NEO выключен", "idle")
            self._mic_mute_restore_continuous = bool(self._continuous_voice_session)
            self._app_mic_muted = True
            self._wake_generation += 1
            self._partial_generation += 1
            self._pending_wake_recording = None
            self._pending_partial_recording = None
            self._wake_command_ui_pending = False
            self._interrupt_wake_active = False
            if self.audio_capture.is_recording:
                self.audio_capture.stop(emit_result=False)
            self._voice_mode = ""
            self._clear_listening_bubble()
            self._restore_listening_audio()
            self.mic_level.setValue(0)
            self.mic_level.hide()
            self.mic_button.setEnabled(False)
            self.mic_button.setText("🎙")
            self._refresh_mic_mute_icon()
            self.mic_mute_button.setToolTip(self._localize_text("Включить микрофон для NEO"))
            self._set_status("Микрофон NEO выключен")
            logger.info("Application microphone muted; capture stopped without changing the Windows device")
            return

        self._app_mic_muted = False
        self._refresh_mic_mute_icon()
        self.mic_mute_button.setToolTip(self._localize_text("Выключить микрофон для NEO"))
        self.mic_button.setEnabled(bool(self.config.get("voice.enabled", True)) and not self._busy)
        logger.info("Application microphone unmuted")
        if self._mic_mute_restore_continuous and self._continuous_voice_session and not self._busy:
            QTimer.singleShot(60, lambda: self._start_command_capture(seamless=False) if (not self._app_mic_muted and self._continuous_voice_session and not self._busy) else None)
        else:
            self._resume_wake_listening(delay_ms=80)
        self._mic_mute_restore_continuous = False

    @staticmethod
    def _windows_hotkey_parts(sequence: str, *, no_repeat: bool = True) -> tuple[int, int] | None:
        text = str(sequence or "").strip()
        if not text:
            return None
        parts = [p.strip().casefold() for p in text.replace(" ", "").split("+") if p.strip()]
        if not parts:
            return None
        mods = 0x4000 if no_repeat else 0
        key = parts[-1]
        for part in parts[:-1]:
            if part in {"ctrl", "control"}: mods |= 0x0002
            elif part == "alt": mods |= 0x0001
            elif part == "shift": mods |= 0x0004
            elif part in {"win", "meta", "windows"}: mods |= 0x0008
            else: return None
        if len(key) == 1 and (key.isalpha() or key.isdigit()):
            vk = ord(key.upper())
        elif re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", key):
            vk = 0x70 + int(key[1:]) - 1
        else:
            key_map = {
                "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
                "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdown": 0x22,
                "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
                "escape": 0x1B, "esc": 0x1B, "delete": 0x2E, "backspace": 0x08,
            }
            vk = key_map.get(key, 0)
            if not vk:
                return None
        return mods, vk

    def _unregister_app_mic_hotkey(self) -> None:
        if os.name == "nt" and self._mic_hotkey_registered:
            try:
                from jarvis.runtime.windows_hotkeys import unregister_hotkey
                unregister_hotkey(int(getattr(self, "_mic_hotkey_hwnd", int(self.winId()))), int(self._mic_hotkey_id))
            except Exception:
                logger.debug("Could not unregister global microphone hotkey", exc_info=True)
        self._mic_hotkey_registered = False
        if self._mic_hotkey_local is not None:
            try: self._mic_hotkey_local.setEnabled(False)
            except Exception: pass
            self._mic_hotkey_local.deleteLater()
            self._mic_hotkey_local = None

    def _register_app_mic_hotkey(self) -> None:
        active_conflicts = getattr(self, "_hotkey_conflicts", set())
        active_conflicts.discard(getattr(self, "_mic_hotkey_last_attempt", ""))
        self._hotkey_conflicts = active_conflicts
        self._unregister_app_mic_hotkey()
        sequence = str(self.config.get("hotkeys.app_microphone_toggle", "Ctrl+Alt+M") or "").strip()
        self._mic_hotkey_last_attempt = sequence
        if not sequence:
            return

        # On Windows prefer RegisterHotKey and do NOT install the same QShortcut
        # when registration succeeds. Otherwise a focused NEO window can receive
        # both WM_HOTKEY and QShortcut and toggle the microphone twice.
        if os.name == "nt":
            parts = self._windows_hotkey_parts(sequence)
            if parts:
                try:
                    from jarvis.runtime.windows_hotkeys import register_hotkey
                    mods, vk = parts
                    hwnd = int(self.winId())
                    ok, error = register_hotkey(hwnd, int(self._mic_hotkey_id), int(mods), int(vk))
                    self._mic_hotkey_registered = ok
                    if ok:
                        self._mic_hotkey_hwnd = hwnd
                    if ok:
                        logger.info("Global NEO microphone hotkey registered: %s", sequence)
                        return
                    logger.warning("Global microphone hotkey registration failed: key=%s win32_error=%s; focused-window fallback", sequence, error)
                    self._notify_hotkey_conflict(sequence)
                except Exception:
                    logger.debug("Could not register global microphone hotkey", exc_info=True)
            else:
                logger.warning("Unsupported global microphone hotkey: %s; using focused-window fallback", sequence)

        try:
            self._mic_hotkey_local = QShortcut(QKeySequence(sequence), self)
            self._mic_hotkey_local.activated.connect(self._toggle_app_microphone)
        except Exception:
            logger.debug("Could not create local microphone shortcut", exc_info=True)

    def _unregister_aux_hotkeys(self) -> None:
        if os.name == "nt":
            try:
                from jarvis.runtime.windows_hotkeys import unregister_hotkey
                handles = getattr(self, "_aux_hotkey_handles", {})
                for hotkey_id in list(self._aux_hotkey_registered):
                    unregister_hotkey(int(handles.get(hotkey_id, int(self.winId()))), int(hotkey_id))
                self._aux_hotkey_handles = {}
            except Exception:
                logger.debug("Could not unregister auxiliary hotkeys", exc_info=True)
        self._aux_hotkey_registered.clear()
        for shortcut in self._aux_hotkey_local:
            try:
                shortcut.setEnabled(False)
                shortcut.deleteLater()
            except Exception:
                pass
        self._aux_hotkey_local.clear()

    def _register_aux_hotkeys(self) -> None:
        active_conflicts = getattr(self, "_hotkey_conflicts", set())
        active_conflicts.difference_update(getattr(self, "_aux_hotkey_conflict_keys", set()))
        self._hotkey_conflicts = active_conflicts
        self._aux_hotkey_conflict_keys = set()
        self._unregister_aux_hotkeys()
        specs = [
            (self._autoclicker_hotkey_id, str(self.config.get("hotkeys.autoclicker_toggle", "Ctrl+Alt+C") or ""), self._toggle_autoclicker_hotkey, False),
            (self._chat_scroll_up_hotkey_id, str(self.config.get("hotkeys.chat_scroll_up", "Ctrl+Alt+Up") or ""), lambda: self.chat.scroll_by_hotkey(-1), True),
            (self._chat_scroll_down_hotkey_id, str(self.config.get("hotkeys.chat_scroll_down", "Ctrl+Alt+Down") or ""), lambda: self.chat.scroll_by_hotkey(1), True),
            (self._window_topmost_hotkey_id, str(self.config.get("hotkeys.window_topmost_toggle", "Ctrl+Alt+T") or ""), self._toggle_window_topmost_hotkey, False),
            (self._shelf_hotkey_id, str(self.config.get("hotkeys.bookshelf_toggle", "Ctrl+Alt+B") or ""), self._toggle_bookshelf, False),
        ]
        for hotkey_id, sequence, callback, repeat in specs:
            sequence = sequence.strip()
            if not sequence:
                continue
            registered = False
            if os.name == "nt":
                parts = self._windows_hotkey_parts(sequence, no_repeat=not repeat)
                if parts:
                    try:
                        from jarvis.runtime.windows_hotkeys import register_hotkey
                        mods, vk = parts
                        hwnd = int(self.winId())
                        registered, error = register_hotkey(hwnd, int(hotkey_id), int(mods), int(vk))
                        if registered:
                            self._aux_hotkey_registered.add(int(hotkey_id))
                            handles = getattr(self, "_aux_hotkey_handles", {})
                            handles[int(hotkey_id)] = hwnd
                            self._aux_hotkey_handles = handles
                        else:
                            logger.warning("Global hotkey registration failed: key=%s win32_error=%s", sequence, error)
                    except Exception:
                        logger.debug("Could not register auxiliary global hotkey %s", sequence, exc_info=True)
            if registered:
                continue
            if os.name == "nt":
                self._notify_hotkey_conflict(sequence)
                self._aux_hotkey_conflict_keys.add(sequence)
            try:
                shortcut = QShortcut(QKeySequence(sequence), self)
                shortcut.setAutoRepeat(bool(repeat))
                shortcut.activated.connect(callback)
                self._aux_hotkey_local.append(shortcut)
            except Exception:
                logger.debug("Could not create auxiliary local hotkey %s", sequence, exc_info=True)

    def _notify_hotkey_conflict(self, sequence):
        active = getattr(self, "_hotkey_conflicts", set())
        active.add(sequence)
        self._hotkey_conflicts = active
        shown = getattr(self, "_reported_hotkey_conflicts", set())
        if sequence in shown:
            return
        shown.add(sequence)
        self._reported_hotkey_conflicts = shown
        logger.warning("Global shortcut unavailable; focused-window shortcut retained: %s", sequence)
        # Platform diagnostics stay in logs and the Settings conflict state.
        # They are not user requests: do not append a chat bubble or mirror to a
        # remote chat when a hotkey is unavailable.

    def _toggle_autoclicker_hotkey(self) -> None:
        try:
            from jarvis.runtime.autoclicker import autoclicker_controller
            autoclicker_controller(self.config).toggle()
        except Exception as exc:
            self._set_status(f"Автокликер: {exc}")

    def _toggle_window_topmost_hotkey(self) -> None:
        now = time.monotonic()
        if now < float(getattr(self, "_window_toggle_guard_until", 0.0) or 0.0):
            return
        self._window_toggle_guard_until = now + 0.20
        if getattr(self, "_runtime_demoted184", False) or not self.isVisible() or self.isMinimized():
            self._set_runtime_topmost(True, hide=False)
            self._set_status("Окно NEO показано поверх окон без перехвата фокуса")
        else:
            self._set_runtime_topmost(False, hide=True)
            self._set_status("Окно NEO убрано за другие окна")

    def nativeEvent(self, event_type, message):  # noqa: N802
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes
                msg = wintypes.MSG.from_address(int(message))
                # HOTFIX83: while layout editing is enabled, every outer edge of
                # the frameless assistant behaves like a normal Windows resize
                # border.  Native hit-testing is more reliable than child-widget
                # mouse filters because it also works over chat/robot contents.
                if int(msg.message) == 0x0084 and bool(self.config.get("ui.robot_layout_edit_enabled", False)):
                    lparam = int(msg.lParam)
                    screen_x = ctypes.c_short(lparam & 0xFFFF).value
                    screen_y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                    frame = self.frameGeometry()
                    border = max(7, min(12, int(round(8 * max(0.8, min(1.35, getattr(self, "_scale", 1.0)))))))
                    near_left = screen_x <= frame.left() + border
                    near_right = screen_x >= frame.right() - border
                    near_top = screen_y <= frame.top() + border
                    near_bottom = screen_y >= frame.bottom() - border
                    if near_top and near_left:
                        return True, 13   # HTTOPLEFT
                    if near_top and near_right:
                        return True, 14   # HTTOPRIGHT
                    if near_bottom and near_left:
                        return True, 16   # HTBOTTOMLEFT
                    if near_bottom and near_right:
                        return True, 17   # HTBOTTOMRIGHT
                    if near_left:
                        return True, 10   # HTLEFT
                    if near_right:
                        return True, 11   # HTRIGHT
                    if near_top:
                        return True, 2    # HTCAPTION: top middle drags; corners still resize
                    if near_bottom:
                        return True, 15   # HTBOTTOM
                if int(msg.message) == 0x0232:  # WM_EXITSIZEMOVE: keep the caption reachable
                    QTimer.singleShot(0, self._caption_drag.ensure_reachable)
                if self._mic_hotkey_registered and int(msg.message) == 0x0312 and int(msg.wParam) == int(self._mic_hotkey_id):
                    QTimer.singleShot(0, self._toggle_app_microphone)
                    return True, 0
                if int(msg.message) == 0x0312 and int(msg.wParam) in self._aux_hotkey_registered:
                    hotkey_id = int(msg.wParam)
                    if hotkey_id == self._autoclicker_hotkey_id:
                        QTimer.singleShot(0, self._toggle_autoclicker_hotkey)
                    elif hotkey_id == self._chat_scroll_up_hotkey_id:
                        QTimer.singleShot(0, lambda: self.chat.scroll_by_hotkey(-1))
                    elif hotkey_id == self._chat_scroll_down_hotkey_id:
                        QTimer.singleShot(0, lambda: self.chat.scroll_by_hotkey(1))
                    elif hotkey_id == self._window_topmost_hotkey_id:
                        QTimer.singleShot(0, self._toggle_window_topmost_hotkey)
                    elif hotkey_id == self._shelf_hotkey_id:
                        QTimer.singleShot(0, self._toggle_bookshelf)
                    return True, 0
            except Exception:
                logger.debug("Native Windows event parse failed", exc_info=True)
        return super().nativeEvent(event_type, message)

    def _record_voice(self) -> None:
        response_voice = getattr(self, "_response_voice_service", None)
        if response_voice is not None:
            response_voice.cancel()
        if getattr(self, "_wake_candidate_pending158", False):
            self._cancel_command_asr157()
            self._voice_mode = ""
            self._clear_listening_bubble()
            self._restore_listening_audio()
            self._set_busy(False, "Ожидание", "idle")
            self._resume_wake_listening(delay_ms=40)
            return
        if self._app_mic_muted:
            self._set_status("Микрофон NEO выключен")
            return
        if self.audio_capture.is_recording:
            if self._voice_mode == "command":
                self._set_status("Останавливаю запись…")
                self.audio_capture.stop()
            else:
                self.audio_capture.stop(emit_result=False)
                self._voice_mode = ""
                self._start_command_capture()
            return
        if self._busy or not bool(self.config.get("voice.enabled", True)):
            return
        self._pause_wake_listening()
        self._start_command_capture()

    def _end_capture_owner188(self) -> bool:
        """HIST-021: end the active capture owner after a backend/STT switch.

        Old ASR/partial results are invalidated by generation, the UI leaves the
        listening state, and continuous conversation or wake listening re-arms
        on the new backend. Returns False when no capture owned the microphone."""
        mode = str(getattr(self, "_voice_mode", "") or "")
        if mode not in {"command", "command_pending", "wake", "interrupt_wake"}:
            return False
        self._voice_mode = ""
        self._wake_command_ui_pending = False
        self._interrupt_wake_active = False
        self._command_asr_generation = int(getattr(self, "_command_asr_generation", 0) or 0) + 1
        self._partial_generation = int(getattr(self, "_partial_generation", 0) or 0) + 1
        self._pending_partial_recording = None
        self._wake_generation = int(getattr(self, "_wake_generation", 0) or 0) + 1
        self._pending_wake_recording = None
        self._heard_timer.stop()
        self._restore_listening_audio()
        if mode in {"command", "command_pending"}:
            self._clear_listening_bubble()
            live = getattr(self, "_live_user_bubble", None)
            self._live_user_bubble = None
            if live is not None:
                try:
                    self.chat.remove_bubble(live)
                except Exception:
                    logger.debug("Could not remove the interrupted live transcript", exc_info=True)
        try:
            self.mic_button.setText("🎙")
            self.mic_level.hide()
        except Exception:
            pass
        if self._quitting:
            return True
        if self._continuous_voice_session:
            QTimer.singleShot(250, lambda: self._start_command_capture(seamless=False)
                              if self._continuous_voice_session and not self._busy and not self._voice_mode else None)
        elif not self._busy:
            self._resume_wake_listening(delay_ms=250)
        return True

    def _prepare_command_capture_ui(self, seed_text: str = "") -> None:
        """Arm command UI only after the isolated audio worker confirms command mode."""
        self._wake_candidate_pending158 = False
        self._sleeping = False
        self._voice_mode = "command"
        self._partial_generation += 1
        self._partial_worker_active = False
        self._pending_partial_recording = None
        self._heard_timer.stop()
        self._heard_target_text = str(seed_text).strip()
        self._heard_display_text = str(seed_text).strip()
        self.input.setText(self._heard_display_text)
        # Raw/partial Whisper text is provisional and must never become chat
        # history. Keep live ASR only in the input field; the user bubble is
        # created once, after SpeechUnderstanding/command repair has produced the
        # final text that NEO actually routes. This keeps chat and execution in
        # agreement even when Whisper heard «телеграммм» but NEO resolved Telegram.
        self._live_user_bubble = None
        listen_status = (
            "Слушаю… Говорите. Скажите «стоп», чтобы завершить диалог."
            if self._continuous_voice_session else "Слушаю… Говорите."
        )
        self._set_busy(True, listen_status, "listening", allow_mic_stop=True)
        self._show_listening_bubble()
        self.mic_button.setText("■")
        self.mic_level.show()

    def _duck_for_listening(self) -> None:
        if self._listening_ducked or not bool(self.config.get("voice.duck_system_while_listening", False)):
            return
        try:
            factor = max(0.0, min(1.0, float(self.config.get("voice.listening_duck_percent", 18) or 18) / 100.0))
            audio_focus_manager().duck(factor)
            self._listening_ducked = True
        except Exception:
            logger.debug("Listening duck failed", exc_info=True)

    def _restore_listening_audio(self) -> None:
        if not self._listening_ducked:
            return
        try: audio_focus_manager().restore()
        except Exception: logger.debug("Listening audio restore failed", exc_info=True)
        self._listening_ducked = False

    def _start_command_capture(self, seed_text: str = "", *, seamless: bool = False, resume_from_last_finish: bool = False) -> None:
        if self._app_mic_muted or self._quitting or not bool(self.config.get("voice.enabled", True)):
            return
        self._wake_command_ui_pending = False
        self._duck_for_listening()
        self._prepare_command_capture_ui(seed_text)
        self._command_asr_generation = getattr(self, "_command_asr_generation", 0) + 1
        self.audio_capture.start_command(seamless=seamless, resume_from_last_finish=resume_from_last_finish)

    def _promote_wake_to_command(self, recording: RecordingResult | None = None) -> None:
        """Switch wake capture to command capture without reopening PortAudio."""
        if self._app_mic_muted or self._quitting or not bool(self.config.get("voice.enabled", True)):
            return
        self._wake_command_ui_pending = True
        self._voice_mode = "command_pending"
        wake_label = str(self.config.get("wake_word.phrase", "") or "wake word").strip()
        # Do not overwrite the instant wake acknowledgement with a "preparing"
        # state. From the user's perspective NEO is already listening now.
        self._set_status(f"Слушаю… ({wake_label})")
        self._command_asr_generation = getattr(self, "_command_asr_generation", 0) + 1
        if not self.audio_capture.promote_to_command(recording):
            self._wake_command_ui_pending = False
            logger.warning("Wake promotion failed; falling back to a fresh command capture")
            self._start_command_capture(seamless=False)

    def _audio_started(self, device_name: str) -> None:
        self._active_microphone_name = str(device_name or "Микрофон").strip() or "Микрофон"
        self.status.setToolTip(self._active_microphone_name)
        if self._wake_command_ui_pending and self.audio_capture.mode == "command":
            self._wake_command_ui_pending = False
            self._prepare_command_capture_ui("")
            self._set_status(self._active_microphone_name)
            logger.info("Wake transition: isolated audio worker confirmed command mode")
            return
        if self._voice_mode in {"command", "wake"}:
            # Keep the bottom strip visually quiet: only the selected device
            # name is shown beside the live meter, without “Микрофон активен”
            # or a repeated wake-word sentence.
            self._set_status(self._active_microphone_name)
            self.mic_level.show()

    def _audio_recovered(self, message: str) -> None:
        logger.warning("%s", message)
        if self._voice_mode in {"command", "wake"}:
            self._set_status(self._active_microphone_name or "Микрофон")
            self.mic_level.show()

    def _audio_level_changed(self, level: int) -> None:
        if self._app_mic_muted:
            self._mic_level_target = 0.0
            self._mic_level_rendered = 0.0
            self._mic_meter_timer.stop()
            self.mic_level.setValue(0)
            self.mic_level.hide()
            return
        self._mic_level_target = float(max(0, min(100, int(level))))
        if self._voice_mode in {"wake", "command"} and not self._mic_meter_timer.isActive():
            self._mic_meter_timer.start()
        if self._voice_mode in {"wake", "command"} and not self._quitting:
            if self._voice_mode == "wake" and not self._busy and self._active_microphone_name:
                self._set_status(self._active_microphone_name)
            self.mic_level.show()

    def _mic_meter_smooth_tick(self) -> None:
        if self._app_mic_muted or self._quitting:
            self._mic_meter_timer.stop()
            return
        delta = self._mic_level_target - self._mic_level_rendered
        if abs(delta) < 0.55:
            self._mic_level_rendered = self._mic_level_target
            self.mic_level.setValue(int(round(self._mic_level_rendered)))
            self._mic_meter_timer.stop()
            return
        interval = max(1, int(self._mic_meter_timer.interval()))
        alpha = 0.70 if interval >= 30 else (0.45 if interval >= 15 else 0.28)
        self._mic_level_rendered += delta * alpha
        self.mic_level.setValue(int(round(self._mic_level_rendered)))

    def _audio_partial_ready(self, recording: RecordingResult) -> None:
        if self._voice_mode != "command" or not self.audio_capture.is_recording:
            return
        self._pending_partial_recording = recording
        if not self._partial_worker_active:
            self._start_partial_transcription()

    def _start_partial_transcription(self) -> None:
        recording = self._pending_partial_recording
        if recording is None or self._voice_mode != "command" or getattr(self, "_quitting", False):
            return
        self._pending_partial_recording = None
        self._partial_worker_active = True
        generation = self._partial_generation
        samples = int(getattr(getattr(recording, 'audio', None), 'size', 0) or 0)
        worker = FunctionWorker(self.partial_recognizer.transcribe, recording)
        worker.signals.result.connect(lambda text: self._partial_transcribed(text, generation, samples))
        worker.signals.error.connect(lambda trace: logger.debug("Live transcription failed: %s", trace))
        worker.signals.finished.connect(lambda: self._partial_finished(generation))
        self.thread_pool.start(worker)

    def _partial_transcribed(self, text: str, generation: int, samples: int | None = None) -> None:
        # HIST-005: a late partial of a closed window never restarts the heard timer.
        if getattr(self, "_quitting", False) or generation != self._partial_generation or self._voice_mode != "command":
            return
        recognized = str(text).strip()
        if recognized:
            # NEO 188 NEO-ASR-002: remember how much audio this preview covered.
            self._heard_samples188 = samples
            self._set_heard_target(recognized)

    def _set_heard_target(self, text: str) -> None:
        text = str(text).strip()
        if not text:
            return
        # Whisper may revise the last word. Keep the longest common prefix and
        # animate only the changed suffix so the transcript feels live.
        common = 0
        for left, right in zip(self._heard_display_text, text):
            if left != right:
                break
            common += 1
        if common < len(self._heard_display_text):
            self._heard_display_text = self._heard_display_text[:common]
        self._heard_target_text = text
        if not self._heard_timer.isActive():
            self._heard_timer.start()

    def _heard_text_tick(self) -> None:
        if self._voice_mode != "command" and not self._heard_target_text:
            self._heard_timer.stop()
            return
        if self._heard_display_text == self._heard_target_text:
            self._heard_timer.stop()
            return
        # Reveal several characters per 60 Hz tick. This keeps approximately the
        # same visible catch-up speed as the old 8 ms timer with half the repaints.
        step = 4 if len(self._heard_target_text) - len(self._heard_display_text) > 12 else 2
        self._heard_display_text = self._heard_target_text[: len(self._heard_display_text) + step]
        shown = self._heard_display_text or "Слушаю…"
        self.input.setText(self._heard_display_text)
        self.input.setCursorPosition(len(self._heard_display_text))
        if self._live_user_bubble is not None:
            self.chat.update_bubble(self._live_user_bubble, shown)

    def _finalize_live_user_bubble(self, text: str) -> None:
        self._heard_timer.stop()
        self._heard_target_text = text
        self._heard_display_text = text
        if self._live_user_bubble is None:
            self._live_user_bubble = self.chat.add_user(text)
        else:
            self.chat.update_bubble(self._live_user_bubble, text)
        self._live_user_bubble = None
        # The assistant status bubble was created before the final transcript was
        # known. Move that same row below the resolved user command so normal chat
        # chronology remains USER -> NEO while preserving the visual replacement.
        if self._listening_bubble is not None:
            self.chat.move_bubble_to_bottom(self._listening_bubble)
        self.chat.scroll_to_bottom(force=True)

    def _partial_finished(self, generation: int) -> None:
        if getattr(self, "_quitting", False):
            # HIST-005: a closing window starts no further partial worker.
            self._partial_worker_active = False
            return
        if generation != self._partial_generation:
            return
        self._partial_worker_active = False
        if self._pending_partial_recording is not None and self._voice_mode == "command":
            self._start_partial_transcription()

    def _audio_finished(self, recording: RecordingResult) -> None:
        mode = self._voice_mode
        self._voice_mode = ""
        self._wake_candidate_pending158 = bool(mode == "command" and recording.wake_candidate)
        if self._wake_candidate_pending158:
            self._voice_mode = "command_pending"
        if mode == "command" and recording.audio.size and not recording.wake_candidate:
            # Recording is over: keep the exact same assistant message and switch
            # it immediately from ``Слушаю`` to ``Думаю`` while ASR/Agent work.
            self._show_thinking_bubble()
        # Keep system audio ducked while ASR, tool execution and the response are
        # in progress.  Restoration belongs to the interaction lifecycle, not the
        # end of raw microphone recording.
        if mode == "command":
            self._partial_generation += 1
            self._pending_partial_recording = None
        self.mic_button.setText("🎙")
        self.mic_level.hide()
        if mode == "command":
            self._transcribe_command(recording)

    def _transcribe_command(self, recording: RecordingResult) -> None:
        live_text = str(self._heard_target_text or self._heard_display_text).strip()
        # HOTFIX119 accuracy gate: streaming partial text is display-only. A partial
        # hypothesis can change while the final consonants are still arriving;
        # treating it as the final command made short and ordinary speech regress.
        # Every command now gets a completed audio decode. The short transport lane
        # below is still fast, but it runs on the *finished* recording.
        use_live = bool(self.config.get("voice.use_live_result_as_final", False))
        if use_live and len(live_text) >= 2:
            # NEO 188 NEO-ASR-002: a stale preview (it missed the last words) never
            # stands in for the command; the finished recording is decoded instead.
            from jarvis.runtime.recognition_events188 import live_covers
            use_live = live_covers(getattr(self, '_heard_samples188', None), int(recording.audio.size),
                                   int(recording.sample_rate), float(self.config.get("voice.silence_seconds", 1.05) or 1.05))
        self._heard_samples188 = None
        if use_live and len(live_text) >= 2:
            self._voice_recognized(live_text)
            return
        if recording.audio.size == 0:
            self._clear_listening_bubble()
            self._heard_timer.stop()
            if self._live_user_bubble is not None:
                self.chat.update_bubble(self._live_user_bubble, "Речь не обнаружена")
                self._live_user_bubble = None
            if self._continuous_voice_session:
                self._set_busy(False, "Диалог активен — слушаю дальше…", "curious")
                QTimer.singleShot(int(self.config.get("voice.continuous_rearm_ms", 20) or 20), lambda: self._start_command_capture(seamless=False) if self._continuous_voice_session and not self._busy else None)
            elif self._silence_retry188(recording):
                return
            else:
                self._restore_listening_audio()
                if recording.empty_reason == "wake_start_timeout":
                    self._set_busy(False, "Ожидаю обращения…", "idle")
                else:
                    self._set_busy(False, "Речь не обнаружена. Увеличьте чувствительность или выберите другой микрофон.", "curious")
                self._resume_wake_listening(delay_ms=40)
            return

        # Speaker/system playback may be acoustically picked up by the physical
        # microphone. Reject it before spending command-STT CPU whenever the
        # loopback reference already proves the origin.
        if self._probable_system_playback_echo(recording, stage="command-pre-asr"):
            self._discard_system_echo_command()
            return

        if recording.wake_candidate:
            self._set_busy(True, "Слушаю…", "listening", allow_mic_stop=True)
        else:
            self._set_busy(True, "Распознаю вашу речь…", "thinking")
        duration = float(recording.audio.size) / max(1, int(recording.sample_rate))
        from jarvis.runtime.voice_followup156 import pending_slot
        awaiting_slot = pending_slot(getattr(self, 'agent', None), self._language_code())

        def _timed_command_asr(rec: RecordingResult, cancelled=None) -> dict[str, Any]:
            started = time.perf_counter()
            try:
                if cancelled is not None and cancelled.is_set():
                    return {}
                # A tiny hot recognizer is enough to decide only the tightly
                # constrained transport vocabulary.  If it does not produce an
                # exact safe media intent, fall through to the accurate small model.
                if (
                    awaiting_slot is None
                    and bool(self.config.get("voice.fast_media_enabled", True))
                    and duration <= float(self.config.get("voice.fast_media_max_seconds", 2.4) or 2.4)
                ):
                    try:
                        fast_text = str(self.fast_media_recognizer.transcribe_fast_control_probe(
                            rec, timeout_seconds=3.0, wait_for_slot=False
                        ) or "").strip()
                        quick = _gui_parse_quick_media_command188(fast_text, self._language_code())
                        if quick is not None:
                            return {"kind": "quick_media", "text": fast_text, "command": quick, "recording": rec}
                    except Exception:
                        logger.debug("Fast media STT lane failed; using full command STT", exc_info=True)
                if cancelled is not None and cancelled.is_set():
                    return {}
                slot_profile188 = None
                if awaiting_slot is not None:
                    # NEO 188 NEO-ASR-001: hints for the awaited field, for this request only.
                    try:
                        from jarvis.runtime.asr_profile188 import profile_for
                        slot_profile188 = profile_for(awaiting_slot, self._language_code(), self.config)
                    except Exception:
                        slot_profile188 = None
                if slot_profile188 is not None:
                    try:
                        detailed = self.recognizer.transcribe_detailed(rec, profile=slot_profile188)
                    except TypeError:
                        detailed = self.recognizer.transcribe_detailed(rec)
                else:
                    detailed = self.recognizer.transcribe_detailed(rec)
                if cancelled is not None and cancelled.is_set():
                    return {}
                alternatives = list(detailed.hypotheses)
                if awaiting_slot is not None:
                    # A song/note/name answer is data for the active question.
                    # Do not re-decode it against unrelated desktop entities.
                    return {"kind": "text", "text": detailed.text, "alternatives": alternatives[:5], "recording": rec}
                # Conditional context rescue: ordinary clear commands stop here.
                # Only a constrained local-entity slot that remains unresolved
                # gets one cheap beam-2 re-decode on the already-warm command model.
                try:
                    command_language = str(
                        self.config.get("voice.command_language", self._language_code())
                        or self._language_code()
                    ).casefold()
                    if command_language in {"", "auto", "detect", "automatic"}:
                        command_language = self._language_code()
                    preview = self.speech_understanding.resolve(
                        detailed.text, alternatives=alternatives, language=command_language, record=False
                    )
                    max_rescue_seconds = float(
                        self.config.get("voice.speech_understanding_rescue_max_seconds", 6.0) or 6.0
                    )
                    if not preview.changed and duration <= max(1.0, min(12.0, max_rescue_seconds)):
                        terms = self.speech_understanding.contextual_rescue_terms(
                            detailed.text, language=command_language, limit=4
                        )
                        if terms:
                            if cancelled is not None and cancelled.is_set():
                                return {}
                            rescued = self.recognizer.transcribe_context_rescue(
                                rec, candidates=terms, language=command_language
                            )
                            rescued_text = str(rescued.get("text", "") or "").strip() if isinstance(rescued, dict) else ""
                            if rescued_text and rescued_text.casefold() != str(detailed.text or "").strip().casefold():
                                alternatives.append(dict(rescued))
                                developer_bus().emit(
                                    "speech_understanding", "context_rescue",
                                    data={"terms": terms, "raw": detailed.text, "rescued": rescued_text},
                                )
                except Exception:
                    logger.debug("Conditional contextual ASR rescue failed", exc_info=True)
                return {"kind": "text", "text": detailed.text, "alternatives": alternatives[:5], "recording": rec}
            finally:
                elapsed = time.perf_counter() - started
                latency_profiler().record("ASR", elapsed, "command", correlation_id=rec.correlation_id)
                developer_bus().emit("latency", "ASR", data={"stage": "ASR", "duration_ms": elapsed * 1000.0}, duration_ms=elapsed * 1000.0)

        generation = getattr(self, "_command_asr_generation", 0)
        agent_generation = self._agent_generation
        # NEO 188 NEO-ASR-001: remember which question this decode answers; a
        # cancelled, expired or replaced question never receives the result.
        from jarvis.runtime.voice_followup156 import pending_question
        self._asr_question188 = (generation, pending_question(getattr(self, 'agent', None), self._language_code())
                                 if awaiting_slot is not None else None)
        self._command_asr_pending157 = True
        # Completion always returns through a Qt-owned timer. A global pool full
        # of Discord/network jobs must not queue microphone decoding indefinitely.
        deadline = max(15.0, min(60.0, 20.0 + duration * 2.0))
        logger.info("Command ASR queued: generation=%s audio_s=%.2f deadline_s=%.1f", generation, duration, deadline)
        started = self._command_asr_runner157.start(
            lambda cancelled: _timed_command_asr(recording, cancelled),
            result=lambda value: self._deliver_command_asr155(value, generation, agent_generation),
            error=lambda value: self._deliver_command_asr155(value, generation, agent_generation, error=True),
            timeout=deadline, abort=self.transcription_service.interrupt,
        )
        if not started:
            self._deliver_command_asr155("CommandRecognitionBusy", generation, agent_generation, error=True)

    def _cancel_command_asr157(self) -> None:
        capture = getattr(self, "audio_capture", None)
        if capture is not None and (getattr(self, "_wake_candidate_pending158", False)
                                    or getattr(capture, "_command_waiting158", False)):
            capture.stop(emit_result=False)
        self._wake_candidate_pending158 = False
        self._command_asr_generation = getattr(self, "_command_asr_generation", 0) + 1
        self._command_asr_pending157 = False
        runner = getattr(self, "_command_asr_runner157", None)
        if runner is not None:
            runner.cancel()

    def _command_asr_failed157(self, trace: str, message: str | None = None) -> None:
        logger.warning("Command ASR did not complete: %s", str(trace).splitlines()[-1])
        self._heard_timer.stop()
        self._partial_generation += 1
        self._pending_partial_recording = None
        self._voice_mode = ""
        self._restore_listening_audio()
        bubble = getattr(self, "_listening_bubble", None)
        self._listening_bubble = None
        self._live_user_bubble = None
        messages = {
            "ru": "Не удалось распознать фразу. Пожалуйста, повторите.",
            "en": "I couldn't recognize the phrase. Please repeat it.",
            "de": "Ich konnte den Satz nicht erkennen. Bitte wiederhole ihn.",
            "fr": "Je n’ai pas reconnu la phrase. Peux-tu la répéter ?",
            "es": "No pude reconocer la frase. Repítela, por favor.",
            "it": "Non ho riconosciuto la frase. Ripetila, per favore.",
            "pt": "Não consegui reconhecer a frase. Repita, por favor.",
        }
        message = message or messages.get(self._language_code(), messages["en"])
        self._set_busy(False, message, "curious")
        self.mic_button.setText("🎙")
        if bubble is not None:
            self.chat.update_bubble(bubble, message)
        else:
            self._clear_listening_bubble()
            self._append_assistant(message, paced=False)
        self._rearm_voice_after_answer()

    def _deliver_command_asr155(self, result, generation, agent_generation, *, error=False):
        if (self._quitting or self._app_mic_muted
                or generation != getattr(self, '_command_asr_generation', 0)
                or agent_generation != self._agent_generation):
            return
        self._command_asr_generation = generation + 1
        self._command_asr_pending157 = False
        self._wake_candidate_pending158 = False
        if getattr(self, "_voice_mode", "") == "command_pending":
            self._voice_mode = ""
        logger.info("Command ASR completed: generation=%s success=%s", generation, not error)
        asked = getattr(self, '_asr_question188', None)
        if not error and asked and asked[0] == generation and asked[1] is not None:
            from jarvis.runtime.asr_profile188 import stale_answer
            if stale_answer(asked[1], getattr(self, 'agent', None), self._language_code()):
                logger.info("Command ASR result dropped: the awaited question changed during decoding")
                from jarvis.runtime.asr_profile188 import stale_text
                self._command_asr_failed157('stale pending answer', message=stale_text(self._language_code()))
                return
        if error:
            self._command_asr_failed157(str(result))
        else:
            try:
                self._command_transcription_ready(result)
            except Exception:
                self._command_asr_failed157(traceback.format_exc())

    def _command_transcription_ready(self, result: Any) -> None:
        payload = result if isinstance(result, dict) else {"kind": "text", "text": str(result or "")}
        recording = payload.get("recording") if isinstance(payload.get("recording"), RecordingResult) else None
        if recording:
            self._last_voice_correlation = recording.correlation_id
            if recording.speech_ended_at > 0:
                latency_profiler().record("VAD_end_to_transcript", time.monotonic() - recording.speech_ended_at, "VAD endpoint proxy", correlation_id=recording.correlation_id)
        # Re-check after ASR because the loopback signature may have arrived while
        # Whisper was decoding. This protects both full commands and the wakeless
        # quick-media lane from speaker echo.
        if self._probable_system_playback_echo(recording, stage="command-post-asr"):
            self._discard_system_echo_command()
            return
        if recording is not None and recording.wake_candidate and not str(payload.get("text", "") or "").strip():
            self._start_command_after_acoustic_wake()
            return
        # NEO 188 NEO-ASR-002: the same final result of the same capture is one
        # logical command; a repeated delivery never creates a second one.
        heard188 = str(payload.get("text", "") or (payload["command"].heard if isinstance(payload.get("command"), QuickMediaCommand) else "")).strip()
        # A wake-candidate recording shares its capture id with the command it
        # promotes to, so only completed command captures are claimed.
        if heard188 and recording is not None and not recording.wake_candidate:
            guard = getattr(self, '_dispatch_guard188', None)
            if guard is None:
                from jarvis.runtime.recognition_events188 import DispatchGuard
                guard = self._dispatch_guard188 = DispatchGuard()
            if not guard.claim('desktop', recording.correlation_id, heard188):
                logger.info("Command ASR duplicate result ignored: capture=%s", recording.correlation_id)
                return
        from jarvis.runtime.voice_followup156 import pending_slot
        awaiting_slot = pending_slot(getattr(self, 'agent', None), self._language_code())
        if awaiting_slot is None and payload.get("kind") == "quick_media" and isinstance(payload.get("command"), QuickMediaCommand):
            command = payload["command"]
            heard = str(payload.get("text", "") or command.heard).strip()
            # In continuous conversation, a bare/repeated stop is a conversation
            # control command, not a request to pause arbitrary media.  Explicit
            # targets such as «стоп музыка» still stay on the quick-media lane.
            if self._continuous_voice_session and self._is_continuous_stop_phrase(heard):
                logger.info("Fast command STT matched conversation stop: heard=%r", heard)
                self._busy = False
                self._voice_recognized(heard)
                return
            display_heard = self._canonical_quick_media_heard(command, raw=heard)
            if display_heard:
                self._finalize_live_user_bubble(display_heard)
                command = QuickMediaCommand(action=command.action, target=command.target, heard=display_heard, recognized=heard)
            self._busy = False
            logger.info("Fast command STT matched media transport: action=%s heard=%r", command.action, display_heard or heard)
            self._dispatch_quick_media_command(command)
            return
        self._voice_recognized(
            str(payload.get("text", "") or ""),
            alternatives=payload.get("alternatives", []) if isinstance(payload, dict) else [],
        )

    def _warm_spoken_spelling(self) -> None:
        if bool(self.config.get("voice.enabled", True)) and bool(self.config.get("voice.spoken_spelling_enabled", True)):
            from jarvis.runtime.spoken_spelling165 import warm_up
            warm_up(self._language_code())

    def _normalize_recognized_command(self, text: str, *, alternatives=None) -> str:
        from jarvis.runtime.command_contracts150 import short_request
        if short_request(str(text or ""), self._language_code()):
            return str(text or "")
        from jarvis.runtime.self_window184 import configured_action
        if configured_action(self.config, text) is not None:
            return str(text or "")
        from jarvis.runtime.spoken_surface155 import normalize_spoken
        original = normalize_spoken(str(text or ""), wake=str(self.config.get("wake_word.phrase", "NEO")))
        from jarvis.runtime.command_contracts150 import short_request
        if short_request(original, self._language_code()):
            return original
        # «превет» -> «привет» before command understanding and the chat bubble.
        original = _correct_heard_words(self, original)
        try:
            language = str(self.config.get("voice.command_language", self._language_code()) or self._language_code())
            if language in {"auto", "detect", "automatic"}:
                language = self._language_code()
            result = self.speech_understanding.resolve(original, alternatives=alternatives, language=language)
            if result.changed:
                developer_bus().emit("speech_understanding", "corrected", data=result.as_dict())
            return result.text
        except Exception:
            logger.debug("Speech understanding failed", exc_info=True)
            return original

    @staticmethod
    def _looks_like_linguistic_text(text: str) -> bool:
        """Reject non-linguistic Whisper debris without rejecting short commands."""
        normalized = re.sub(r"[^a-zа-яё0-9]+", " ", str(text or "").casefold()).strip()
        if not normalized:
            return False
        words = normalized.split()
        letters = sum(ch.isalpha() for ch in normalized)
        if letters < 2:
            return False
        filler = {"а", "э", "эм", "мм", "м", "ээ", "угу", "ага", "хм", "uh", "um", "hmm"}
        if all(word in filler for word in words):
            return False
        # Common Whisper hallucinations produced by room noise / clipped media.
        garbage_phrases = {
            "субтитры", "продолжение следует", "спасибо за просмотр",
            "thanks for watching", "thank you for watching", "subscribe", "music",
            # Frequent Whisper-on-silence stock outro hallucinations observed on
            # the user's microphone. These exact canned sentences are discarded
            # only in voice noise filtering; typed chat remains untouched.
            "this is the end of this video thank you for watching",
            "this is the end of the video thank you for watching",
            "this is the end of this video thanks for watching",
        }
        if normalized in garbage_phrases:
            return False
        # A one-word command is fine, but one-character/filler fragments are not.
        if len(words) == 1 and len(words[0]) < 2:
            return False
        return True

    def _is_continuous_stop_phrase(self, text: str) -> bool:
        """Return True only for an explicit request to end the voice session.

        NEO 188 NEO-VOICE-001: the bounded stop grammar of the SELECTED language
        (de «stopp», pt «pare», fr «arrête»…) plus the user's own stop phrases;
        media/system commands such as «стоп музыка» stay untouched. Used by both
        the fast and the final recognition paths.
        """
        from jarvis.runtime.continuous_stop188 import is_stop
        configured = self.config.get("voice.stop_phrases", []) or []
        wake_phrase = str(self.config.get("wake_word.phrase", "") or "").strip()
        names = [item for item in [wake_phrase, *_configured_wake_terms(self.config)]
                 if str(item).strip() and not wake_alias_requires_standalone(item)]
        return is_stop(text, self._language_code(), configured=configured, assistant_words=names)

    def _voice_recognized(
        self, text: str, *, alternatives: list[dict[str, Any]] | None = None
    ) -> None:
        text = str(text).strip()
        raw_transcript = text
        developer_bus().emit("speech_raw", text)
        from jarvis.runtime.voice_followup156 import pending_slot
        awaiting_slot = pending_slot(getattr(self, 'agent', None), self._language_code())
        if awaiting_slot is None and self._voice_unlock_phrase_matches(text):
            self._dispatch_voice_unlock(text)
            return
        phrase = str(self.config.get("wake_word.phrase", "") or "").strip()
        if phrase and awaiting_slot is None:
            matched, remainder = self._match_wake_phrase(text, phrase)
            if matched and not remainder.strip():
                self._start_command_after_acoustic_wake()
                return
            if matched and remainder:
                text = remainder
        if awaiting_slot is None:
            text = self._normalize_recognized_command(text, alternatives=alternatives)
        elif getattr(awaiting_slot, "kind", "") == "text":
            # Dictated note/message body: fix misheard words, keep everything else.
            text = _correct_heard_words(self, text)
        developer_bus().emit("normalized", text)
        if awaiting_slot is None and bool(self.config.get("voice.ignore_nonlinguistic_noise", True)) and not self._looks_like_linguistic_text(text):
            self._clear_listening_bubble()
            # Stay completely quiet on bumps/clicks/breathing. In continuous mode
            # simply re-arm the microphone instead of posting a fake chat message.
            self._heard_timer.stop()
            if self._live_user_bubble is not None:
                try:
                    self.chat.remove_bubble(self._live_user_bubble)
                except Exception:
                    self.chat.update_bubble(self._live_user_bubble, "")
                self._live_user_bubble = None
            if self._continuous_voice_session:
                self._busy = False
                self._set_status("Диалог активен — слушаю…")
                self.robot.set_state("idle")
                QTimer.singleShot(int(self.config.get("voice.continuous_rearm_ms", 35) or 35), lambda: self._start_command_capture(seamless=False) if self._continuous_voice_session and not self._busy else None)
            else:
                self._busy = False
                self._restore_listening_audio()
                self._resume_wake_listening(delay_ms=25)
            return
        if awaiting_slot is None and self._continuous_voice_session and self._is_continuous_stop_phrase(text):
            self._clear_listening_bubble()
            self._continuous_voice_session = False
            self._heard_timer.stop()
            if self._live_user_bubble is not None:
                self.chat.update_bubble(self._live_user_bubble, text or "Стоп")
                self._live_user_bubble = None
            from jarvis.runtime.continuous_stop188 import ended_text
            self._append_assistant(ended_text(self._language_code()), localize=False)
            self._set_busy(False, f"Ожидаю «{phrase or 'wake word'}»", "idle")
            self._restore_listening_audio()
            self._resume_wake_listening(delay_ms=20)
            return
        if not text:
            self._clear_listening_bubble()
            self._heard_timer.stop()
            if self._live_user_bubble is not None:
                self.chat.update_bubble(self._live_user_bubble, "Речь не распознана")
                self._live_user_bubble = None
            if self._continuous_voice_session:
                self._set_busy(False, "Не разобрал фразу — продолжаю слушать…", "curious")
                QTimer.singleShot(int(self.config.get("voice.continuous_rearm_ms", 35) or 35), lambda: self._start_command_capture(seamless=False) if self._continuous_voice_session and not self._busy else None)
            else:
                self._restore_listening_audio()
                self._set_busy(False, "Не удалось разобрать слова. Попробуйте говорить ближе к микрофону.", "curious")
                self._resume_wake_listening(delay_ms=40)
            return
        self._finalize_live_user_bubble(text)
        self.input.setText(text)
        self.input.setCursorPosition(len(text))
        self._busy = False
        if awaiting_slot is None:
            # Other Whisper hypotheses of this utterance: the Agent tries them only
            # if the chosen transcript turns out not to be a command at all.
            remember = getattr(getattr(self, "agent", None), "remember_voice_hypotheses", None)
            if callable(remember):
                remember(raw_transcript, alternatives)
        # HOTFIX118: keep the queued GUI handoff (avoids re-entrancy) but remove
        # the artificial 40 ms wait before dispatching an already-final STT result.
        QTimer.singleShot(0, lambda t=text, r=raw_transcript: self._send_recognized_text(t, raw_text=r))

    def _send_recognized_text(self, text: str, *, raw_text: str = "") -> None:
        if self._quitting:
            return
        self.input.clear()
        self._send_text(
            text, user_already_shown=True, input_source="voice", raw_text=str(raw_text or text)
        )

    def _audio_failed(self, message: str) -> None:
        self._restore_listening_audio()
        self._voice_mode = ""
        self._clear_listening_bubble()
        self._heard_timer.stop()
        if self._live_user_bubble is not None:
            self.chat.update_bubble(self._live_user_bubble, "Ошибка микрофона")
            self._live_user_bubble = None
        self.mic_button.setText("🎙")
        self.mic_level.hide()
        self._append_assistant(f"Микрофон: {message}")
        self._set_busy(False, "Ошибка микрофона", "error")
        if self._continuous_voice_session:
            QTimer.singleShot(max(50, int(self.config.get("voice.continuous_rearm_ms", 35) or 35)), lambda: self._start_command_capture(seamless=False) if self._continuous_voice_session and not self._busy else None)
        else:
            self._restore_listening_audio()
            self._resume_wake_listening(delay_ms=80)

    def _schedule_wake_watchdog(self, delay_ms: int = 120) -> None:
        """Safely ask the existing wake watchdog to re-arm soon.

        HOTFIX4 called this helper after quick-media commands but never defined it,
        which raised AttributeError on the GUI thread and could leave wake listening
        feeling unreliable.  Keep the helper tiny and idempotent: the real state
        checks remain centralized in _ensure_wake_monitor().
        """
        if self._quitting:
            return
        try:
            delay = max(0, int(delay_ms))
        except (TypeError, ValueError):
            delay = 120
        QTimer.singleShot(delay, self._ensure_wake_monitor)

    def _ensure_wake_monitor(self) -> None:
        if not bool(self.config.get("voice.enabled", True)):
            return
        if self._response_active():
            self._start_interrupt_wake_monitor(self._agent_generation)
            return
        if self._app_mic_muted:
            return
        if self._quitting:
            return
        if self._voice_mode == "interrupt_wake" and self.audio_capture.is_recording and not self._busy:
            self._wake_generation += 1
            self._pending_wake_recording = None
            self._interrupt_wake_active = False
            self._voice_mode = "wake"
            if self._continuous_voice_session:
                self._start_command_capture(seamless=True)
            return
        if self._continuous_voice_session:
            if self._busy or self._voice_mode in {"command", "command_pending"}:
                return
            if self.audio_capture.is_recording:
                return
            logger.info("Conversation watchdog: restoring continuous command capture")
            self._start_command_capture(seamless=False)
            return
        if not bool(self.config.get("wake_word.enabled", False)):
            return
        if self._busy or self._voice_mode in {"command", "command_pending"}:
            return
        if self._voice_mode == "wake" and self.audio_capture.is_recording:
            return
        logger.info("Wake watchdog: restoring wake monitor")
        self._resume_wake_listening(delay_ms=20)

    def _pause_wake_listening(self) -> None:
        self._wake_generation += 1
        self._pending_wake_recording = None
        self._pending_wake_probe_recording = None
        if self._voice_mode in {"wake", "interrupt_wake"}:
            if self.audio_capture.is_recording:
                self.audio_capture.stop(emit_result=False)
            self._voice_mode = ""
            self._interrupt_wake_active = False

    def _resume_wake_listening(self, *, delay_ms: int = 120) -> None:
        if self._app_mic_muted or not bool(self.config.get("voice.enabled", True)):
            return
        if not bool(self.config.get("wake_word.enabled", False)) or self._quitting:
            return
        generation = self._wake_generation = self._wake_generation + 1
        QTimer.singleShot(max(0, delay_ms), lambda: self._start_wake_capture(generation))

    def _start_wake_capture(self, generation: int) -> None:
        if self._app_mic_muted or not bool(self.config.get("voice.enabled", True)):
            return
        if generation != self._wake_generation or self._quitting:
            return
        if not bool(self.config.get("wake_word.enabled", False)):
            return
        if self._response_active():
            self._start_interrupt_wake_monitor(self._agent_generation)
            return
        if self._busy:
            QTimer.singleShot(100, lambda: self._start_wake_capture(generation))
            return
        if self.audio_capture.is_recording and self._voice_mode != "wake":
            QTimer.singleShot(100, lambda: self._start_wake_capture(generation))
            return
        self._voice_mode = "wake"
        phrase = self.config.get("wake_word.phrase", "")
        self._set_status(f"Готов. Скажите «{phrase}»")
        if not self._sleeping:
            self.robot.set_state("idle")
        self.audio_capture.start_wake_monitor(
            window_seconds=float(self.config.get("wake_word.chunk_seconds", 0.95)),
            emit_interval=float(self.config.get("wake_word.silence_seconds", 0.08)),
        )

    def _acoustic_wake_enabled_for_current_phrase(self) -> bool:
        if self._acoustic_wake_disabled or not bool(self.config.get("wake_word.acoustic_enabled", False)):
            return False
        phrase = re.sub(r"[^a-zа-яё0-9]+", "", str(self.config.get("wake_word.phrase", "") or "").casefold())
        trained_phrase = re.sub(r"[^a-zа-яё0-9]+", "", str(self.config.get("wake_word.acoustic_model_phrase", "") or "").casefold())
        return bool(phrase and trained_phrase and phrase == trained_phrase)

    def _audio_wake_probe_ready(self, recording: RecordingResult) -> None:
        """Queue/launch a rolling wake probe without ever running two STT decodes.

        The former implementation dropped every probe arriving while one tiny
        Whisper call was active. The isolated audio worker nevertheless counted
        the dropped probe against its two-probe budget, so an early incomplete
        syllable could consume the first decode and discard the useful full-word
        slice. HOTFIX142 keeps one freshest replacement probe and runs it as soon
        as the current wake decode yields.
        """
        if self._app_mic_muted:
            return
        acoustic_enabled = self._acoustic_wake_enabled_for_current_phrase() and not self._response_active()
        fast_asr_enabled = bool(self.config.get("wake_word.fast_asr_probe_enabled", True))
        if (
            self._voice_mode not in {"wake", "interrupt_wake"}
            or (self._busy and self._voice_mode != "interrupt_wake")
            or self._quitting
            or not (acoustic_enabled or fast_asr_enabled)
        ):
            return
        if not self._wake_guard.accepts(recording, int(self.config.get("voice.sensitivity", 92))):
            return
        # NEO 168: the same speaker-playback check that runs on the probe result,
        # applied before the decode instead of after it (identical decision).
        if MainWindow._probable_system_playback_echo(self, recording, stage="wake-pre-asr"):
            return
        duration = float(recording.audio.size) / max(1, int(recording.sample_rate))
        min_probe = max(0.14, min(0.60, float(self.config.get("wake_word.fast_asr_probe_min_audio_seconds", 0.15) or 0.15)))
        if duration < min_probe:
            return
        generation = self._wake_generation
        now = time.monotonic()
        verify_rescue = bool(
            self._wake_rescue_pending_generation == generation
            and now - self._wake_rescue_pending_at <= 1.45
        )
        if self._wake_rescue_pending_generation == generation and not verify_rescue:
            self._wake_rescue_pending_generation = -1
            self._wake_rescue_pending_at = 0.0

        if self._wake_probe_worker_active or self._wake_worker_active:
            pending = self._pending_wake_probe_recording
            from jarvis.runtime.wake_queue160 import prefer_recording
            if pending is None or pending[1] != generation or prefer_recording(recording, pending[0]):
                self._pending_wake_probe_recording = (recording, generation, verify_rescue)
            return
        self._launch_wake_probe(recording, generation, verify_rescue=verify_rescue)

    def _launch_wake_probe(self, recording: RecordingResult, generation: int, *, verify_rescue: bool = False) -> None:
        if generation != self._wake_generation or self._quitting:
            return
        if self._busy and self._voice_mode != "interrupt_wake":
            return
        self._wake_probe_worker_active = True
        acoustic_enabled = self._acoustic_wake_enabled_for_current_phrase() and not self._response_active()
        if acoustic_enabled:
            worker = FunctionWorker(self._hybrid_wake_detector.score, recording)
            worker.signals.result.connect(lambda score: self._wake_probe_scored(score, generation))
            worker.signals.error.connect(lambda trace: logger.debug("Wake acoustic probe failed: %s", trace))
            worker.signals.finished.connect(lambda: self._wake_probe_finished(generation))
            self.thread_pool.start(worker)
            return

        if not self._wake_result_timer.isActive():
            self._wake_result_timer.start()

        def run_fast_probe() -> None:
            started = time.perf_counter()
            decode_started_at = time.monotonic()
            try:
                text = str(self.wake_recognizer.transcribe_fast_wake_probe(
                    recording, language_override="en" if verify_rescue else None
                ) or "").strip()
                self._enqueue_wake_result(("probe_result", {
                    "text": text, "decoded_at": time.monotonic(),
                    "decode_ms": (time.perf_counter() - started) * 1000.0,
                    "started_at": decode_started_at,
                    "decode_language": self.wake_recognizer._fast_wake_language("en" if verify_rescue else None),
                }, generation, recording))
            except BaseException:
                self._enqueue_wake_result(("probe_error", traceback.format_exc(), generation, recording))
            finally:
                elapsed = time.perf_counter() - started
                latency_profiler().record("ASR:wake_probe", elapsed, "wake", correlation_id=recording.correlation_id)
                self._enqueue_wake_result(("probe_finished", None, generation, recording))

        threading.Thread(
            target=run_fast_probe,
            name=f"NeoJarvis-WakeProbe-{generation}",
            daemon=True,
        ).start()

    def _enqueue_wake_result(self, item: tuple[str, Any, int, RecordingResult | None]) -> None:
        """Bound cross-thread wake events and discard the oldest stale item."""
        try:
            self._wake_result_queue.put_nowait(item)
        except queue.Full:
            try:
                self._wake_result_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._wake_result_queue.put_nowait(item)
            except queue.Full:
                pass

    def _wake_probe_finished(self, generation: int | None = None) -> None:
        self._wake_probe_worker_active = False
        # An old native decode can finish after cancellation and a new wake
        # session has started. Its result is stale, but releasing its occupied
        # lane must still start the NEW session's pending recording.
        if MainWindow._start_pending_wake160(self):
            return
        if not self._wake_worker_active and self._wake_result_timer.isActive() and self._wake_result_queue.empty():
            self._wake_result_timer.stop()

    def _start_pending_wake160(self) -> bool:
        from jarvis.runtime.wake_queue160 import select_pending
        if self._wake_worker_active or self._wake_probe_worker_active:
            return False
        pending = select_pending(self._wake_generation, self._pending_wake_recording, self._pending_wake_probe_recording)
        self._pending_wake_recording = None
        self._pending_wake_probe_recording = None
        if (pending is None or self._quitting or getattr(self, "_app_mic_muted", False)
                or self._voice_mode not in {"wake", "interrupt_wake"}
                or (self._busy and self._voice_mode != "interrupt_wake")):
            return False
        kind, item = pending
        recording, pending_generation = item[:2]
        if kind == "complete":
            self._launch_wake_transcription(recording, pending_generation)
        else:
            verify_rescue = bool(item[2])
            self._launch_wake_probe(recording, pending_generation, verify_rescue=verify_rescue)
        return True

    def _probable_system_playback_echo(self, recording: RecordingResult | None, *, stage: str) -> bool:
        """Reject microphone audio that is a delayed copy of PC playback.

        The comparison uses only compact loopback envelopes; raw system PCM never
        enters this process and no extra neural inference is required.
        """
        response_voice = getattr(self, "_response_voice_service", None)
        if response_voice is not None and response_voice.rejects_recording(recording):
            return True
        ambient = getattr(self, "ambient_learning", None)
        checker = getattr(ambient, "probable_system_echo", None) if ambient is not None else None
        if not callable(checker) or recording is None:
            return False
        try:
            matched, score = checker(recording)
        except Exception:
            logger.debug("System playback echo guard failed open at %s", stage, exc_info=True)
            return False
        if matched:
            logger.info("Rejected system-playback echo at %s: similarity=%.3f", stage, float(score or 0.0))
            developer_bus().emit("audio_provenance", "system_echo_rejected", data={"stage": stage, "similarity": float(score or 0.0)})
            return True
        return False

    def _discard_system_echo_command(self) -> None:
        """Silently discard a command proven to originate from speaker playback."""
        self._clear_listening_bubble()
        self._heard_timer.stop()
        if self._live_user_bubble is not None:
            try:
                self.chat.remove_bubble(self._live_user_bubble)
            except Exception:
                try:
                    self.chat.update_bubble(self._live_user_bubble, "")
                except Exception:
                    pass
            self._live_user_bubble = None
        self._busy = False
        self._restore_listening_audio()
        if self._continuous_voice_session:
            self._set_status("Диалог активен — слушаю…")
            self.robot.set_state("idle")
            QTimer.singleShot(30, lambda: self._start_command_capture(seamless=False) if self._continuous_voice_session and not self._busy else None)
        else:
            self._set_status(f"Ожидаю «{str(self.config.get('wake_word.phrase', '') or 'wake word')}»")
            self.robot.set_state("idle")
            self._resume_wake_listening(delay_ms=25)

    def _wake_hypothesis_needs_verification(
        self, text: str, recording: RecordingResult | None = None,
    ) -> bool:
        """Return True for cheap-ASR outputs that must never wake NEO alone.

        Tiny Whisper occasionally turns a ~0.5 s proper name into a fluent English
        phrase (``there it is``, ``let's go do this``).  The transcript is useful
        evidence that speech occurred, but it is not trustworthy wake evidence.
        Such rows consume the already-budgeted second rolling probe in English;
        they do not add continuous CPU load and they never execute directly.
        """
        value = str(text or "").strip()
        if not value:
            return False
        if wake_rescue_match(value, _configured_wake_rescue_terms(self.config)):
            return True
        words = re.findall(r"[^\W_]+", value, flags=re.UNICODE)
        if recording is None or not words:
            return False
        duration = float(recording.audio.size) / max(1, int(recording.sample_rate))
        # An implausibly verbose transcript for a sub-second clip is a classic
        # decoder-language hallucination. Verification is cheap and only happens
        # after the first tiny probe has already produced this suspicious output.
        if duration <= 1.15 and len(words) >= 3:
            speech_rate = len(words) / max(0.18, duration)
            if speech_rate >= 4.2:
                return True
            # In Russian command mode a fluent multi-word Latin sentence from a
            # sub-second wake clip is much more likely to be a tiny-Whisper
            # language detour than the intended command. Do not enumerate every
            # future phrase ("there it is", "let's go do this", ...); verify it.
            lang = self._language_code()
            latin_words = sum(bool(re.fullmatch(r"[A-Za-z]+(?:'[A-Za-z]+)?", word)) for word in words)
            if lang == "ru" and latin_words == len(words):
                return True
        return False

    def _fast_wake_probe_transcribed(
        self, text: Any, generation: int, recording: RecordingResult | None = None,
    ) -> None:
        timing = text if isinstance(text, dict) else {}
        text = str(timing.get("text", "") or "") if timing else str(text or "")
        if generation != self._wake_generation or self._voice_mode not in {"wake", "interrupt_wake"}:
            return
        if self._busy and self._voice_mode != "interrupt_wake":
            return
        value = str(text or "").strip()
        if not value:
            return
        backoff = getattr(self, "_wake_backoff", None)
        if recording is not None and backoff is not None:
            backoff.remember_probe(recording.correlation_id, value)
        if self._probable_system_playback_echo(recording, stage="wake-fast"):
            return
        if recording:
            self._last_voice_correlation = recording.correlation_id
        if self._output_stop_from_voice(value):
            return
        if self._voice_unlock_phrase_matches(value):
            logger.info("Voice unlock phrase matched by streaming Whisper probe: %r", value)
            self._dispatch_voice_unlock(value)
            return
        if (
            bool(self.config.get("wake_word.quick_commands_enabled", True))
        ):
            quick = _gui_parse_quick_media_command188(value, self._language_code())
            if quick is not None and self._wakeless_quick_media_allowed(quick):
                canonical = self._canonical_quick_media_heard(quick, raw=value)
                quick = QuickMediaCommand(action=quick.action, target=quick.target, heard=canonical or value, recognized=value)
                logger.info("Quick media command matched by rolling probe: action=%s target=%s heard=%r", quick.action, quick.target, value)
                self._dispatch_quick_media_command(quick)
                return
        capture = _quick_capture_from(self, value)
        if capture is not None:
            logger.info("Quick capture command matched by rolling probe: action=%s heard=%r", capture.action, value)
            self._dispatch_quick_capture(capture)
            return
        phrase = str(self.config.get("wake_word.phrase", "") or "").strip()
        aliases = tuple(_configured_wake_terms(self.config))
        matched, _remainder, score = match_wake_phrase(
            value, phrase,
            threshold=float(self.config.get("wake_word.fuzzy_threshold", 0.88)),
            aliases=aliases,
            # A rolling probe may contain only the ordinary interjection «ну»
            # before its next word arrives. Only completed utterances may use
            # ambiguous standalone aliases; strong names remain immediate.
            allow_standalone_aliases=False,
        )
        verifying_rescue = bool(
            self._wake_rescue_pending_generation == generation
            and time.monotonic() - self._wake_rescue_pending_at <= 1.45
        )
        if not matched:
            if verifying_rescue:
                # This was the second, forced-English view of the same utterance.
                # If it still is not genuinely Jarvis-like, reject rather than
                # teaching an ordinary English phrase to behave as the wake word.
                logger.debug("Wake rescue verification rejected: %r score=%.3f", value, score)
                self._wake_rescue_pending_generation = -1
                self._wake_rescue_pending_at = 0.0
                return
            if self._wake_hypothesis_needs_verification(value, recording):
                self._wake_rescue_pending_generation = generation
                self._wake_rescue_pending_at = time.monotonic()
                logger.debug("Suspicious wake ASR held for cross-language verification: %r", value)
                return
            logger.debug("Streaming Whisper wake probe rejected: %r score=%.3f", value, score)
            return
        self._wake_rescue_pending_generation = -1
        self._wake_rescue_pending_at = 0.0
        logger.info("Wake word matched by streaming Whisper probe: %r score=%.3f", value, score)
        # Deliberately discard the partial suffix: promotion keeps the rolling
        # microphone pre-roll and command mode will capture the rest of the user's
        # sentence. Executing a partial suffix here would cut off long requests.
        self._wake_transcribed(
            {"matched": True, "text": phrase, "remainder": "", "score": score,
             "source": "whisper-stream", "command_tail": bool(_remainder.strip()),
             **{key: timing[key] for key in ("decoded_at", "decode_ms", "started_at", "decode_language") if key in timing}},
            generation, recording,
        )

    def _wake_probe_scored(self, score: Any, generation: int) -> None:
        if generation != self._wake_generation or self._voice_mode not in {"wake", "interrupt_wake"}:
            return
        if self._busy and self._voice_mode != "interrupt_wake":
            return
        if not self._acoustic_wake_enabled_for_current_phrase():
            return
        try:
            value = float(score)
        except (TypeError, ValueError):
            return
        threshold = float(self.config.get("wake_word.acoustic_threshold", 0.12) or 0.12)
        if value < threshold:
            return
        logger.info("Wake word matched by streaming acoustic probe: score=%.3f", value)
        self._wake_heard173()
        if self._voice_mode == "interrupt_wake" and self._busy:
            QTimer.singleShot(0, lambda: self._interrupt_current_operation(source="wake-acoustic"))
            return
        self._sleeping = False
        interaction_mode = str(self.config.get("voice.interaction_mode", "") or "").strip().casefold()
        if interaction_mode not in {"continuous", "wake_each_command"}:
            interaction_mode = "continuous" if bool(self.config.get("voice.continuous_after_wake", False)) else "wake_each_command"
        self._continuous_voice_session = interaction_mode == "continuous"
        self.robot.set_state("listening")
        self._show_listening_bubble()
        wake_label = str(self.config.get("wake_word.phrase", "") or "wake word").strip()
        self._set_status(f"Услышал «{wake_label}». Говорите команду…")
        self._wake_generation += 1
        QTimer.singleShot(0, self._promote_wake_to_command)

    def _audio_wake_chunk_ready(self, recording: RecordingResult) -> None:
        if self._app_mic_muted:
            return
        if self._voice_mode not in {"wake", "interrupt_wake"} or self._quitting:
            return
        if self._busy and self._voice_mode != "interrupt_wake":
            return
        if not self._wake_guard.accepts(
            recording, int(self.config.get("voice.sensitivity", 92))
        ):
            return
        if MainWindow._probable_system_playback_echo(self, recording, stage="wake-pre-asr"):
            return
        generation = self._wake_generation
        if self._wake_worker_active or self._wake_probe_worker_active:
            # Always keep only the freshest rolling window. In particular, do not
            # start robust tiny-Whisper while a streaming tiny-Whisper probe is
            # still using the CPU; serialize them to flatten CPU peaks.
            self._pending_wake_recording = (recording, generation)
            return
        self._launch_wake_transcription(recording, generation)

    def _transcribe_wake_robust(self, recording: RecordingResult) -> dict[str, Any]:
        """Detect Jarvis with a purpose-built acoustic model plus Whisper fallback.

        Short isolated names are a weak case for speech-to-text models. RC8 first
        asks openWakeWord's ``hey_jarvis`` acoustic classifier and then keeps the
        multilingual Whisper/fuzzy path as an independent fallback.
        """

        phrase = str(self.config.get("wake_word.phrase", "") or "").strip()
        aliases = tuple(_configured_wake_terms(self.config))
        threshold = float(self.config.get("wake_word.fuzzy_threshold", 0.88))
        acoustic_threshold = float(self.config.get("wake_word.acoustic_threshold", 0.12))
        duration = float(recording.audio.size) / max(1, int(recording.sample_rate))
        allow_standalone_aliases = bool(getattr(recording, "completed_utterance", False))
        acoustic_score = 0.0
        acoustic_enabled = self._acoustic_wake_enabled_for_current_phrase()
        robust_started_at = time.monotonic()
        decode_attempts = 0
        rescue_checked = False

        def verify_suspicious_wake(raw_text: str) -> dict[str, Any] | None:
            nonlocal rescue_checked, decode_attempts
            if rescue_checked or not self._wake_hypothesis_needs_verification(raw_text, recording):
                return None
            rescue_checked = True
            try:
                decode_attempts += 1
                verified = str(self.wake_recognizer.transcribe_fast_wake_probe(
                    recording, language_override="en"
                ) or "").strip()
            except Exception:
                logger.debug("Wake rescue English verification failed", exc_info=True)
                return None
            if not verified:
                return None
            ok, _tail, verify_score = match_wake_phrase(
                verified, phrase, threshold=threshold, aliases=aliases,
                allow_standalone_aliases=allow_standalone_aliases,
            )
            if not ok:
                logger.debug("Wake rescue English verification rejected: first=%r verify=%r score=%.3f", raw_text, verified, verify_score)
                return None
            logger.info("Wake rescue verified cross-language: first=%r verify=%r score=%.3f", raw_text, verified, verify_score)
            return {
                "matched": True,
                # Canonicalize the wake text: semantic Whisper hallucinations must
                # never leak into the visible/chat-facing interaction state.
                "text": phrase,
                "remainder": "",
                "score": verify_score,
                "source": "whisper-rescue-en",
                "decode_language": "en",
            }
        if acoustic_enabled:
            try:
                acoustic_score = float(self._hybrid_wake_detector.score(recording))
            except Exception as exc:
                logger.debug("Acoustic wake detector failed: %s", exc)
                acoustic_score = 0.0
            if acoustic_score >= acoustic_threshold:
                logger.info("Wake word matched acoustically: score=%.3f", acoustic_score)
                return {
                    "matched": True,
                    "text": phrase,
                    "remainder": "",
                    "score": acoustic_score,
                    "source": "openwakeword",
                }

        # HOTFIX120: if a short utterance reached endpointing before a rolling
        # probe succeeded, try the same beam-1 tiny decoder used by the streaming
        # path before the heavier robust wake decode. This single fast result can
        # resolve either Jarvis *or* a transport word, so it is not a blind extra
        # decode on the successful path. The robust decoder remains the fallback.
        fast_first = ""
        if duration <= 1.35:
            try:
                decode_attempts += 1
                fast_first = str(self.wake_recognizer.transcribe_fast_control_probe(recording) or "").strip()
            except Exception:
                logger.debug("Completed fast wake/control probe failed", exc_info=True)
            if fast_first and self._response_active() and self._strict_stop_phrase(fast_first):
                return {"matched": False, "text": fast_first, "source": "interrupt-fast"}
            if fast_first and self._voice_unlock_phrase_matches(fast_first):
                return {"matched": False, "voice_unlock": True, "text": fast_first, "remainder": "", "score": 1.0, "source": "voice-unlock-fast"}
            if (
                fast_first
                and bool(self.config.get("wake_word.quick_commands_enabled", True))
            ):
                quick = _gui_parse_quick_media_command188(fast_first, self._language_code())
                if quick is not None and self._wakeless_quick_media_allowed(quick):
                    canonical = self._canonical_quick_media_heard(quick, raw=fast_first)
                    quick = QuickMediaCommand(action=quick.action, target=quick.target, heard=canonical or fast_first, recognized=fast_first)
                    return {"matched": False, "fast_command": quick, "text": fast_first, "remainder": "", "score": 1.0, "source": "quick-media-fast"}
            capture = _quick_capture_from(self, fast_first) if fast_first else None
            if capture is not None:
                return {"matched": False, "fast_capture": capture, "text": fast_first, "remainder": "", "score": 1.0, "source": "quick-capture-fast"}
            if fast_first:
                matched, remainder, score = match_wake_phrase(fast_first, phrase, threshold=threshold, aliases=aliases,
                    allow_standalone_aliases=allow_standalone_aliases)
                if matched:
                    logger.info("Wake ASR matched completed fast probe: %r (match-score %.3f)", fast_first, score)
                    return {"matched": True, "text": phrase, "remainder": remainder, "score": score, "source": "whisper-fast-complete", "decode_language": self.wake_recognizer._control_language()}
                rescued = verify_suspicious_wake(fast_first)
                if rescued is not None:
                    return rescued

        decode_attempts += 1
        first = str(self.wake_recognizer.transcribe(recording) or "").strip()
        candidates = [fast_first] if fast_first else []
        if first and first not in candidates:
            candidates.append(first)
        if first and self._voice_unlock_phrase_matches(first):
            logger.info("Voice unlock phrase matched without wake: %r", first)
            return {"matched": False, "voice_unlock": True, "text": first, "remainder": "", "score": 1.0, "source": "voice-unlock"}
        if (
            first
            and bool(self.config.get("wake_word.quick_commands_enabled", True))
        ):
            quick = _gui_parse_quick_media_command188(first, self._language_code())
            if quick is not None:
                # Bare Stop/Pause/Play on the permanent wake stream is too easy
                # to trigger from room speech, videos or NEO' own audio. Keep
                # wake-less next/previous/like/dislike, but require an explicit
                # media target ("стоп музыка", "продолжи Discord") for state
                # changing pause/resume commands. Busy Stop remains the dedicated
                # conversation-interrupt path.
                if self._wakeless_quick_media_allowed(quick):
                    canonical = self._canonical_quick_media_heard(quick, raw=first)
                    quick = QuickMediaCommand(action=quick.action, target=quick.target, heard=canonical or first, recognized=first)
                    logger.info("Quick media command matched without wake: action=%s target=%s heard=%r", quick.action, quick.target, first)
                    return {"matched": False, "fast_command": quick, "text": first, "remainder": "", "score": 1.0, "source": "quick-media"}
                logger.debug("Ignored ownerless bare wake-less media state command: %r", first)
        capture = _quick_capture_from(self, first) if first else None
        if capture is not None:
            return {"matched": False, "fast_capture": capture, "text": first, "remainder": "", "score": 1.0, "source": "quick-capture"}
        if first:
            matched, remainder, score = match_wake_phrase(first, phrase, threshold=threshold, aliases=aliases,
                allow_standalone_aliases=allow_standalone_aliases)
            if matched:
                wake_language = self.wake_recognizer._wake_language()
                logger.info("Wake ASR matched selected %s: %r (match-score %.3f)", wake_language, first, score)
                return {"matched": True, "text": phrase, "remainder": remainder, "score": score, "source": f"whisper-{wake_language}", "decode_language": wake_language}
            rescued = verify_suspicious_wake(first)
            if rescued is not None:
                return rescued

        # A different language is useful only for an already wake-like name.
        # Do not decode the selected language twice or walk RU/EN after automatic
        # detection. This bounded retry follows the configured wake spelling.
        first_token = (first.split() or [""])[0]
        if (
            duration <= 3.2
            and len(first.split()) <= 6
            and not wake_alias_requires_standalone(first_token)
            and looks_wake_like(first_token, phrase, minimum=0.78, aliases=aliases)
        ):
            retry_languages = self.wake_recognizer._wake_retry_languages()
            for language in retry_languages:
                try:
                    decode_attempts += 1
                    text = str(self.wake_recognizer.transcribe_with_language(recording, language) or "").strip()
                except Exception as exc:
                    logger.debug("Wake %s retry failed: %s", language, exc)
                    continue
                if text and text not in candidates:
                    candidates.append(text)
                matched, remainder, score = match_wake_phrase(text, phrase, threshold=max(0.84, threshold - 0.02), aliases=aliases,
                    allow_standalone_aliases=allow_standalone_aliases)
                if matched:
                    logger.info("Wake ASR matched %s: %r (match-score %.3f)", language, text, score)
                    return {"matched": True, "text": phrase, "remainder": remainder, "score": score, "source": f"whisper-{language}", "decode_language": language}

        now = time.monotonic()
        # HOTFIX127: rejected wake hypotheses are normal ambient telemetry, not
        # one INFO event per spoken fragment. Historical field logs contained
        # thousands of ``you`` / ``Thank you`` rows during long sessions. Keep a
        # periodic sample for diagnostics and send the rest to DEBUG, preventing
        # avoidable disk churn without changing recognition behaviour.
        emit = logger.info if now - self._last_wake_reject_log_at >= 12.0 else logger.debug
        emit("Wake rejected; acoustic=%.3f threshold=%.3f asr=%r acoustic_enabled=%s model=%s language=%s decodes=%d decode_ms=%.1f queue_ms=%.1f correlation=%s",
             acoustic_score, acoustic_threshold, candidates[:3], acoustic_enabled,
             self.config.get("wake_word.stt_model", "tiny"), self.wake_recognizer._wake_language(),
             decode_attempts, (now - robust_started_at) * 1000.0,
             max(0.0, robust_started_at - recording.captured_at) * 1000.0 if recording.captured_at else 0.0,
             recording.correlation_id)
        if now - self._last_wake_reject_log_at >= 12.0:
            self._last_wake_reject_log_at = now
        return {
            "matched": False,
            "text": first or (candidates[0] if candidates else ""),
            "remainder": "",
            "score": max(acoustic_score, 0.0),
            "source": "none",
        }

    def _wakeless_quick_media_allowed(self, quick: QuickMediaCommand) -> bool:
        """Safety gate shared by rolling and completed wake-stream controls."""
        # Permanent microphone listening must not turn unrelated «дальше» or
        # «продолжить» into a request to start the configured but inactive player.
        # Explicit media objects/providers are intentional; bare controls require
        # an observed player, including its paused state. This also preserves
        # active music control after the conversational turn was cancelled.
        if quick.target != "auto":
            return True
        return bool(self._cached_quick_media_owner(allow_historical=False))

    def _canonical_quick_media_heard(self, command: QuickMediaCommand, *, raw: str = "") -> str:
        """Return stable chat text for a recognized transport intent.

        Raw tiny-Whisper spellings remain available in logs, but the visible user
        bubble reflects the intent NEO actually accepted. Explicit provider names
        are still passed through the conservative speech resolver.
        """
        value = str(raw or command.heard or "").strip()
        if command.target != "auto":
            fixed = self._normalize_recognized_command(value) if value else value
            return fixed or value
        code = self._language_code()
        labels = {
            "ru": {
                "pause":"пауза", "resume":"продолжить", "next":"дальше", "previous":"предыдущий",
                "like":"лайк", "dislike":"дизлайк", "volume_up":"громче", "volume_down":"тише",
                "repeat_toggle":"повтор", "shuffle_toggle":"перемешать",
            },
            "en": {
                "pause":"pause", "resume":"resume", "next":"next", "previous":"previous",
                "like":"like", "dislike":"dislike", "volume_up":"louder", "volume_down":"quieter",
                "repeat_toggle":"repeat", "shuffle_toggle":"shuffle",
            },
        }
        if code in labels:
            return labels[code].get(command.action, value) or value
        # Other UI languages already have their own exact quick-command corpus;
        # preserve the accepted wording rather than displaying a Russian label.
        return self._normalize_recognized_command(value) if value else value

    def _quick_media_server(self) -> str:
        return str(self.config.get("integrations.discord.default_server", "") or "").strip()

    def _cached_quick_media_owner(self, *, allow_historical: bool = True) -> str:
        """Return the likely music owner without any API round-trip.

        HOTFIX44 first trusts the live overlay event, then the agent's last concrete
        music integration. If that optimistic owner rejects the transport command,
        _execute_quick_media_command falls back to authoritative status probes.
        """
        state = dict(getattr(self, "_music_overlay_last_event", {}) or {})
        if state and bool(state.get("playing", False)):
            captured = float(state.get("captured_monotonic", 0.0) or 0.0)
            if captured > 0.0 and time.monotonic() - captured <= 20 * 60:
                source = str(state.get("source", "") or "").strip().casefold()
                if source in {"yandex", "yandex_music", "яндекс", "яндекс музыка"}: return "yandex_music"
                if source == "spotify": return "spotify"
                if source == "discord": return "discord"
        # The NEO Yandex player is a local helper process. Querying its IPC
        # status is cheap and authoritative, and fixes the case where an earlier
        # event/context hint was lost although audio is still playing.
        try:
            from jarvis.integrations.background.local_player import local_player
            local = local_player().cached_status() or {}
            local_state = str(local.get("state", "") or "").casefold()
            if local_state in {"playing", "paused", "buffering", "starting"}:
                note_media_observation("yandex_music", playing=local_state in {"playing", "buffering", "starting"}, paused=local_state == "paused")
                return "yandex_music"
            # During a Qt/CDN transition the worker may briefly report Stopped
            # while still holding NEO' loaded source. Presence of that source
            # is authoritative ownership even though it is not currently playing.
            if local.get("url") or local.get("title"):
                return "yandex_music"
        except Exception:
            logger.debug("Local music owner probe failed", exc_info=True)

        # Telegram uses its own Agent, so Agent-private last-target fields are
        # insufficient.  Trust the process-wide successful-tool hint first; both
        # desktop and Telegram registries publish into it.
        shared_owner = recent_music_owner(max_age_seconds=(24 * 60 * 60 if allow_historical else 20 * 60))
        if shared_owner in {"yandex_music", "spotify", "discord"}:
            return shared_owner
        if not allow_historical:
            return ""
        for attr in ("_last_music_target", "_last_integration_target"):
            source = str(getattr(self.agent, attr, "") or "").strip().casefold()
            if source in {"yandex", "yandex_music", "яндекс", "яндекс музыка"}: return "yandex_music"
            if source == "spotify": return "spotify"
            if source == "discord": return "discord"
        return ""

    def _quick_media_statuses(self, *, include_video: bool = True) -> dict[str, str]:
        statuses = {"yandex_music": "inactive", "spotify": "inactive", "discord": "inactive", "video": "inactive"}
        # Fast authoritative local-player probe before any tool/API path.
        try:
            from jarvis.integrations.background.local_player import local_player
            local = local_player().cached_status() or {}
            state = str(local.get("state", "") or "").casefold()
            if state in {"playing", "buffering", "starting"}:
                statuses["yandex_music"] = "playing"
                note_media_observation("yandex_music", playing=True)
            elif state == "paused":
                statuses["yandex_music"] = "paused"
                note_media_observation("yandex_music", playing=False, paused=True)
            elif local.get("title") or local.get("url"):
                statuses["yandex_music"] = "present"
        except Exception:
            pass
        provider = active_music_provider(self.config)
        if provider:
            try:
                result = self.agent.tools.execute("music", {"action": "background_status"})
                data = result.data if getattr(result, "success", False) and isinstance(result.data, dict) else {}
                actual = str(data.get("provider", "") or provider).casefold()
                state = str(data.get("state", "") or "").casefold()
                player = data.get("player") if isinstance(data.get("player"), dict) else {}
                if not state: state = str(player.get("state", "") or "").casefold()
                if bool(data.get("playing")) or state in {"playing", "buffering", "starting"}: statuses[actual] = "playing"
                elif bool(data.get("paused")) or state == "paused": statuses[actual] = "paused"
                elif bool(data.get("owns_track")) or data.get("title") or data.get("track_id") or data.get("current"): statuses[actual] = "present"
            except Exception:
                pass
        try:
            result = self.agent.tools.execute("discord", {"action": "music_queue_status", "server": self._quick_media_server()})
            data = result.data if getattr(result, "success", False) and isinstance(result.data, dict) else {}
            if bool(data.get("playing")): statuses["discord"] = "playing"
            elif bool(data.get("paused")): statuses["discord"] = "paused"
            elif bool(data.get("current")): statuses["discord"] = "present"
        except Exception:
            pass
        if include_video:
            try:
                result = self.agent.tools.execute("media", {"action": "status", "target": "youtube"})
                data = result.data if getattr(result, "success", False) and isinstance(result.data, dict) else {}
                state = str(data.get("state", "") or "").casefold()
                if bool(data.get("playing")) or state == "playing": statuses["video"] = "playing"
                elif bool(data.get("paused")) or state == "paused": statuses["video"] = "paused"
                elif data.get("title") or data.get("tab_index"): statuses["video"] = "present"
            except Exception:
                pass
        return statuses

    def _quick_media_action_enabled(self, command: QuickMediaCommand) -> bool:
        action = str(command.action or "").casefold()
        if action in {"volume_up", "volume_down"}:
            return bool(self.config.get("voice.quick_media_volume_enabled", True))
        if action in {"repeat_toggle", "shuffle_toggle"}:
            return bool(self.config.get("voice.quick_media_repeat_shuffle_enabled", True))
        return True

    @staticmethod
    def _adaptive_volume_down_target(current: int) -> int:
        value = max(0, min(100, int(current)))
        if value > 10:
            return max(0, value - 10)
        if value > 5:
            return max(0, value - 5)
        return max(0, value - 1)

    def _execute_quick_media_command(self, command: QuickMediaCommand) -> dict[str, Any]:
        from .quick_media_messages import content_identity, message
        code = self._language_code()
        action = command.action
        target = command.target
        # HOTFIX82: bare pause/resume still prefers real music owners, but if no
        # Yandex/Discord source accepts the command and an actual YouTube media
        # session is present, it may control that video. This restores the older
        # hands-free «стоп / продолжи» video behavior without guessing video while
        # music is active. Explicit «... видео» remains deterministic.
        cached_owner = ""
        if target in {"auto", "music"} and action in {"pause", "resume", "next", "previous", "like", "dislike", "volume_up", "volume_down", "repeat_toggle", "shuffle_toggle"}:
            cached_owner = self._cached_quick_media_owner()
        # HOTFIX97: pause/resume is a desired state for *all active music* when
        # no source is named.  A cached last-owner remains useful for NEXT/PREV,
        # but must never suppress the second simultaneously playing source.
        # The old shortcut made bare «Stop» pause only Discord *or* Yandex.
        fanout_music_state = target in {"auto", "music"} and action in {"pause", "resume"}
        # If recent playback state already names an owner, touch that transport
        # first. Provider/status fan-out is reconciliation work and must not sit in
        # front of the user's audible pause/resume/next action.
        statuses = (
            {} if fanout_music_state and cached_owner
            else (self._quick_media_statuses(include_video=False) if fanout_music_state
                  else ({} if target in {"yandex_music", "spotify", "discord", "video"} or cached_owner else self._quick_media_statuses(include_video=False)))
        )
        results: list[dict[str, Any]] = []
        labels: list[str] = []
        now_playing: list[dict[str, str]] = []
        errors: list[str] = []

        def run(tool: str, args: dict[str, Any], label: str, source: str) -> bool:
            try:
                result = self.agent.tools.execute(tool, args)
            except Exception as exc:
                logger.debug("Quick media %s failed: %s", label, exc)
                return False
            record = getattr(self.agent, "record_quick_attempt", None)
            if callable(record):
                record(tool, args, result, code)
            pending_data = result.data if isinstance(getattr(result, "data", None), dict) else {}
            accepted_pending = bool(pending_data.get("starting") and pending_data.get("requested"))
            if not getattr(result, "success", False) and not accepted_pending:
                logger.debug("Quick media %s rejected: %s", label, getattr(result, "text", ""))
                error = str(getattr(result, "text", "") or "").strip()
                if error and error not in errors:
                    errors.append(error)
                return False
            data = result.data if isinstance(getattr(result, "data", None), dict) else {}
            # The local Yandex adapter also transports YouTube audio. Keep the
            # transport owner for controls, but display the actual content source.
            current_meta = data.get("current") if isinstance(data.get("current"), dict) else {}
            has_provenance = any(meta.get(key) for meta in (data, current_meta) for key in ("content_provider", "active_content_provider", "media_source"))
            if source == "yandex_music" and not has_provenance:
                adapter = getattr(self.agent.tools, "yandex_adapter", None)
                probe = getattr(adapter, "playback_provenance", None)
                if callable(probe):
                    try:
                        data = {**probe(), **data}
                    except Exception:
                        pass
            provider, label = content_identity(data, source, label)
            results.append({"source": source, "content_provider": provider, "label": label, "data": data, "text": str(getattr(result, "text", "") or "").strip()})
            labels.append(label)
            try:
                if source in {"yandex_music", "spotify", "discord"} and not data.get("starting") and data.get("verified") is not False:
                    # Volume/repeat/shuffle change settings only; never forge a
                    # playback-state transition.  Long-lived media ownership is
                    # refreshed only by actual transport operations.
                    if action == "pause":
                        note_media_observation(source, playing=False, paused=True)
                    elif action in {"resume", "next", "previous", "like", "dislike"}:
                        note_media_observation(source, playing=True, paused=False)
            except Exception:
                pass
            title = str(data.get("title", "") or ((data.get("current") or {}).get("title", "") if isinstance(data.get("current"), dict) else "")).strip()
            artist = str(data.get("artist", "") or ((data.get("current") or {}).get("artist", "") if isinstance(data.get("current"), dict) else "")).strip()
            track_id = str(data.get("track_id", "") or ((data.get("current") or {}).get("track_id", "") if isinstance(data.get("current"), dict) else "")).strip()
            if title and not accepted_pending:
                current = data.get("current") if isinstance(data.get("current"), dict) else {}
                duration_ms = int(data.get("duration_ms", 0) or data.get("expected_duration_ms", 0) or current.get("duration_ms", 0) or current.get("expected_duration_ms", 0) or 0)
                position_ms = int(data.get("position_ms", 0) or current.get("position_ms", 0) or 0)
                now_playing.append({
                    "source": source, "content_provider": provider, "label": label, "title": title, "artist": artist, "track_id": track_id,
                    "duration_ms": duration_ms, "position_ms": position_ms,
                })
            return True

        # Volume/repeat/shuffle are intentionally *local music only*. Discord is
        # excluded even when it is the most recently active media owner.
        if action in {"volume_up", "volume_down", "repeat_toggle", "shuffle_toggle"}:
            if target == "discord":
                return {"ok": False, "text": message("not_discord", code), "labels": [], "now_playing": [], "action": action, "target": target}
            owner = target if target in {"yandex_music", "spotify"} else ""
            if not owner and cached_owner in {"yandex_music", "spotify"}:
                owner = cached_owner
            if not owner:
                owner = str(active_music_provider(self.config) or "")
            if owner not in {"yandex_music", "spotify"}:
                return {"ok": False, "text": message("unavailable", code), "labels": [], "now_playing": [], "action": action, "target": target}
            try:
                status_result = self.agent.tools.execute(owner, {"action": "background_status"})
                status_data = status_result.data if getattr(status_result, "success", False) and isinstance(status_result.data, dict) else {}
            except Exception:
                status_data = {}
            if action in {"volume_up", "volume_down"}:
                raw = status_data.get("volume_percent", status_data.get("volume", None))
                try:
                    current = int(round(float(raw)))
                except Exception:
                    current = int(self.config.get("integrations.yandex_music.background_volume_percent", 100) or 100) if owner == "yandex_music" else 50
                target_volume = min(100, current + 10) if action == "volume_up" else self._adaptive_volume_down_target(current)
                if run(owner, {"action": "set_volume", "volume": target_volume}, provider_label(owner, code), owner):
                    return {"ok": True, "text": message("volume", code, source=labels[-1], value=target_volume), "labels": labels, "now_playing": now_playing, "action": action, "target": owner}
            elif action == "shuffle_toggle":
                enabled = bool(status_data.get("shuffle", False))
                next_action = "shuffle_off" if enabled else "shuffle_on"
                if run(owner, {"action": next_action}, provider_label(owner, code), owner):
                    return {"ok": True, "text": message("shuffle_off" if enabled else "shuffle_on", code, source=labels[-1]), "labels": labels, "now_playing": now_playing, "action": action, "target": owner}
            else:
                repeat_mode = str(status_data.get("repeat_mode", "off") or "off").casefold()
                next_action = "repeat_off" if repeat_mode != "off" else "repeat_track"
                if run(owner, {"action": next_action}, provider_label(owner, code), owner):
                    return {"ok": True, "text": message("repeat_off" if repeat_mode != "off" else "repeat_on", code, source=labels[-1]), "labels": labels, "now_playing": now_playing, "action": action, "target": owner}
            return {"ok": False, "text": "\n".join(errors) or message("failed", code), "labels": labels, "now_playing": now_playing, "action": action, "target": owner}

        y_action = {"pause": "pause", "resume": "play", "next": "next", "previous": "previous", "like": "like", "dislike": "dislike"}.get(action, "")
        d_action = {"pause": "music_pause", "resume": "music_resume", "next": "music_skip", "previous": "music_previous", "like": "music_like", "dislike": "music_dislike"}.get(action, "")
        v_action = {"pause": "pause", "resume": "play", "next": "next", "previous": "previous"}.get(action, "")
        server = self._quick_media_server()

        def active_for_transport(name: str) -> bool:
            state = statuses.get(name, "inactive")
            if action == "pause":
                return state == "playing"
            if action == "resume":
                return state == "paused"
            return state == "playing"

        def run_owner(owner: str) -> bool:
            if owner in {"yandex_music", "spotify"}:
                # NEXT/PREVIOUS own the switch. A preliminary PAUSE added an IPC
                # round trip and could leave a previously playing track paused
                # when the switch failed. The visible ACK supplies feedback.
                return run(owner, {"action": y_action}, provider_label(owner, code), owner)
            if owner == "discord":
                return run("discord", {"action": d_action, "server": server}, "Discord", "discord")
            if owner == "video" and v_action:
                return run("media", {"action": v_action, "target": "youtube"}, "YouTube", "youtube")
            return False

        if target in {"yandex_music", "spotify"}:
            run_owner(target)
        elif target == "discord":
            run_owner("discord")
        elif target == "video":
            # Likes/dislikes belong to music providers only. Other explicit video
            # commands use the passive Windows-media-session path.
            if action not in {"like", "dislike"}:
                run_owner("video")
        elif target in {"music", "auto"}:
            if action in {"pause", "resume"}:
                # React to the known owner first, then reconcile other confirmed
                # active transports. A successful fast action must not suppress
                # a second player, nor wake an idle Discord/Spotify integration.
                attempted: set[str] = set()
                cached_success = False
                if cached_owner in {"yandex_music", "spotify", "discord"}:
                    attempted.add(cached_owner)
                    cached_success = run_owner(cached_owner)
                if cached_success:
                    statuses = recent_music_states(max_age_seconds=30.0)
                elif not statuses:
                    statuses = self._quick_media_statuses(include_video=False)
                for owner in ("yandex_music", "spotify", "discord"):
                    if owner not in attempted and active_for_transport(owner):
                        attempted.add(owner)
                        run_owner(owner)
                # No music owner handled the command. Only now probe YouTube;
                # this prevents a background browser video from stealing pause
                # from active music while restoring bare stop/play for video.
                if not results and v_action:
                    video_state = self._quick_media_statuses(include_video=True).get("video", "inactive")
                    video_active = video_state == "playing" if action == "pause" else video_state == "paused"
                    if video_active:
                        run_owner("video")
            else:
                # HOTFIX43 fast path: if the overlay/event bus already knows who is
                # playing, execute NEXT/PREVIOUS immediately instead of first making
                # two status API calls.  If that cached owner rejects the command,
                # fall back to the authoritative status probes.
                if cached_owner and run_owner(cached_owner):
                    owner = cached_owner
                else:
                    if not statuses:
                        statuses = self._quick_media_statuses(include_video=False)
                    playing = [x for x in tuple(y for y in (active_music_provider(self.config), "discord") if y) if statuses.get(x) == "playing"]
                    last = str(getattr(self.agent, "_last_integration_target", "") or "").casefold()
                    owner = playing[0] if len(playing) == 1 else (last if last in playing else "")
                    if not owner:
                        present = [x for x in tuple(y for y in (active_music_provider(self.config), "discord") if y) if statuses.get(x) in {"paused", "present"}]
                        owner = present[0] if len(present) == 1 else (last if last in present else "")
                    if owner:
                        run_owner(owner)
                    if not results:
                        configured_owner = active_music_provider(self.config)
                        if configured_owner in {"yandex_music", "spotify"}:
                            run_owner(configured_owner)

        if not results:
            return {"ok": False, "text": "\n".join(errors) or message("unavailable", code), "labels": [], "now_playing": [], "action": action, "target": target}

        def _quick_queue_suffix() -> str:
            # HOTFIX129: next/previous adapters return their local queue snapshot
            # together with the transport result. Render it directly so the fast
            # lane stays fast and the chat always tells the user what comes next.
            for row in reversed(results):
                payload = row.get("data") if isinstance(row, dict) else {}
                if not isinstance(payload, dict):
                    continue
                queue_rows = [x for x in list(payload.get("queue") or []) if isinstance(x, dict)]
                if not queue_rows:
                    continue
                labels_q: list[str] = []
                for idx, item_q in enumerate(queue_rows[:5], 1):
                    title_q = str(item_q.get("title", "") or item_q.get("query", "") or "").strip()
                    artist_q = str(item_q.get("artist", "") or "").strip()
                    label_q = " — ".join(x for x in (artist_q, title_q) if x)
                    if label_q:
                        labels_q.append(f"{idx}. {label_q}")
                if labels_q:
                    return "\n" + message("queue", code) + "\n" + "\n".join(labels_q)
            return ""

        heard = command.heard.strip().rstrip(".!?") or command.action
        starting = any(bool(row.get("data", {}).get("starting")) for row in results)
        if starting and not now_playing:
            text = "\n".join(row["text"] for row in results if row.get("text")) or message("accepted", code, source=", ".join(labels))
        elif action in {"next", "previous"} and now_playing and starting:
            item = now_playing[-1]
            track = f"«{item['title']}»" + (f" — {item['artist']}" if item.get('artist') else "")
            text = message("starting", code, track=track) + _quick_queue_suffix()
        elif action in {"next", "previous"} and now_playing:
            item = now_playing[-1]
            track = f"«{item['title']}»" + (f" — {item['artist']}" if item.get("artist") else "")
            text = message(action, code, track=track) + _quick_queue_suffix()
        elif starting:
            text = "\n".join(row["text"] for row in results if row.get("text")) or message("accepted", code, source=", ".join(labels))
        elif action in {"pause", "resume"} and all(row.get("data", {}).get("unchanged") for row in results):
            text = "\n".join(row["text"] for row in results if row.get("text"))
        elif action == "pause":
            text = message("pause_video" if target == "video" else "pause", code, source=", ".join(labels))
        elif action == "resume":
            text = message("resume_video" if target == "video" else "resume", code, source=", ".join(labels))
        elif action == "like":
            item = now_playing[-1] if now_playing else {}
            track = (f" «{item.get('title','')}»" + (f" — {item.get('artist','')}" if item.get('artist') else "")) if item.get('title') else ""
            text = message("like", code, track=track)
        elif action == "dislike":
            item = now_playing[-1] if now_playing else {}
            text = message("dislike", code)
            if item.get("title"):
                track = f"«{item['title']}»" + (f" — {item['artist']}" if item.get("artist") else "")
                text += " " + message("next", code, track=track)
        else:
            # Without confirmed track metadata, preserve the provider's outcome;
            # acceptance alone must not invent a newly playing track.
            text = "\n".join(row["text"] for row in results if row.get("text")) or message("accepted", code, source=", ".join(labels))
        return {"ok": True, "text": text, "labels": labels, "now_playing": now_playing, "action": action, "target": target, "starting": starting}

    def _voice_unlock_phrase_matches(self, text: str) -> bool:
        if not bool(self.config.get("voice_unlock.enabled", False)):
            return False
        value = str(text or "").strip()
        if not value:
            return False
        try:
            if bool(self.config.get("voice_unlock.locked_session_only", True)) and not is_current_session_locked():
                return False
        except Exception:
            logger.debug("Voice unlock lock-state probe failed", exc_info=True)
            return False
        phrase = str(self.config.get("voice_unlock.phrase", "") or "").strip()
        aliases = tuple(str(x).strip() for x in (self.config.get("voice_unlock.aliases", []) or []) if str(x).strip())
        return voice_unlock_phrase_matches(value, phrase, aliases)

    def _dispatch_voice_unlock(self, heard: str) -> None:
        now = time.monotonic()
        cooldown = max(2.0, float(self.config.get("voice_unlock.cooldown_seconds", 4.0) or 4.0))
        last = float(getattr(self, "_last_voice_unlock_attempt_at", 0.0) or 0.0)
        if now - last < cooldown or bool(getattr(self, "_voice_unlock_inflight", False)):
            logger.debug("Duplicate voice unlock trigger suppressed: %r", heard)
            return
        self._last_voice_unlock_attempt_at = now
        self._voice_unlock_inflight = True
        self._wake_generation += 1
        self._pending_wake_recording = None
        try:
            self._pause_wake_listening()
        except Exception:
            logger.debug("Voice unlock could not pause wake capture", exc_info=True)
        logger.info("Executing voice unlock for current Windows session; heard=%r", heard)
        try:
            self._set_status("Кодовая фраза принята — разблокирую Windows…")
            self.robot.set_state("working")
        except Exception:
            pass
        worker = FunctionWorker(attempt_voice_unlock, self.config)
        worker.signals.result.connect(self._voice_unlock_finished)
        worker.signals.error.connect(self._voice_unlock_failed)
        self.thread_pool.start(worker)

    def _voice_unlock_finished(self, result: Any) -> None:
        self._voice_unlock_inflight = False
        data = result if isinstance(result, dict) else {}
        ok = bool(data.get("ok", False))
        status = str(data.get("status", "") or "")
        method = str(data.get("method", "") or "")
        if ok:
            logger.info("Voice unlock completed method=%s status=%s", method, status)
            try:
                self._set_status("Windows разблокирована голосовой фразой.")
                self.robot.set_state("happy")
            except Exception:
                pass
        else:
            logger.warning("Voice unlock failed method=%s status=%s", method, status)
            try:
                self._set_status(status or "Не удалось разблокировать Windows.")
                self.robot.set_state("curious")
            except Exception:
                pass
        if not self._busy:
            self._resume_wake_listening(delay_ms=80)

    def _voice_unlock_failed(self, trace: str) -> None:
        self._voice_unlock_inflight = False
        logger.error("Voice unlock worker failed:\n%s", trace)
        try:
            self._set_status("Голосовая разблокировка завершилась ошибкой. Подробности в логе.")
            self.robot.set_state("curious")
        except Exception:
            pass
        if not self._busy:
            self._resume_wake_listening(delay_ms=100)

    def _dispatch_quick_capture(self, capture: QuickCapture) -> None:
        from jarvis.runtime.voice_followup156 import pending_slot
        if self._busy or pending_slot(getattr(self, 'agent', None), self._language_code()) is not None:
            return
        now = time.monotonic()
        # Overlapping wake windows can decode one «клип» twice: one action per 3 s.
        last_action, last_at = getattr(self, "_last_quick_capture", ("", 0.0))
        if capture.action == last_action and now - last_at < 3.0:
            return
        self._last_quick_capture = (capture.action, now)
        QTimer.singleShot(0, lambda: self._send_text(capture.command, input_source="voice", raw_text=capture.heard))

    def _dispatch_quick_media_command(self, command: QuickMediaCommand) -> None:
        from jarvis.runtime.voice_followup156 import pending_slot
        if (not str(command.heard or '').startswith('панель:')
                and pending_slot(getattr(self, 'agent', None), self._language_code()) is not None):
            self._voice_recognized(str(command.recognized or command.heard))
            return
        if not self._quick_media_action_enabled(command):
            logger.debug("Quick media action disabled by Settings: %s", command.action)
            return
        now = time.monotonic()
        normalized_heard = re.sub(r"[^a-zа-я0-9]+", "", command.heard.casefold())
        key = f"{command.action}:{command.target}:{normalized_heard}"
        semantic_key = f"{command.action}:{command.target}"
        last_semantic = str(getattr(self, "_last_quick_media_semantic", "") or "")
        last_semantic_at = float(getattr(self, "_last_quick_media_semantic_at", 0.0) or 0.0)
        # The wake recognizer works on overlapping rolling windows. One spoken
        # «дальше» may therefore be decoded twice with different spellings
        # (e.g. «Дальше.» and «Dalsze.»). Suppress by semantic action, not only
        # by transcript text. The primary defence below also discards pending
        # overlapping windows before executing the transport action.
        transport_action = command.action in {"next", "previous", "dislike"}
        if transport_action and bool(getattr(self, "_quick_media_transport_inflight", False)):
            logger.info(
                "Quick media transport command suppressed while %s is still in flight: heard=%r",
                getattr(self, "_quick_media_transport_action", "transport"), command.heard,
            )
            return
        semantic_cooldown = 0.45 if command.action in {"next", "previous"} else 1.0
        if (
            (key == self._last_quick_media_key and now - self._last_quick_media_at < semantic_cooldown)
            or (semantic_key == last_semantic and now - last_semantic_at < semantic_cooldown)
        ):
            logger.debug("Duplicate rolling-window quick media command suppressed: %s", semantic_key)
            return
        self._last_quick_media_key = key
        self._last_quick_media_at = now
        self._last_quick_media_semantic = semantic_key
        self._last_quick_media_semantic_at = now
        self._quick_media_trace = (getattr(self, "_last_voice_correlation", "") or uuid.uuid4().hex, now)
        # HIST-007: every dispatch owns a serial; only the owner of the transport
        # admission may release it, never an older pause/resume/error callback.
        serial = int(getattr(self, "_quick_media_serial188", 0) or 0) + 1
        self._quick_media_serial188 = serial
        if transport_action:
            self._quick_media_transport_inflight = True
            self._quick_media_transport_action = command.action
            self._quick_media_transport_token188 = serial

        # Stop the rolling wake capture before touching media. Otherwise queued
        # overlapping windows can finish ASR several seconds later and execute
        # the same pause/next a second time. The quick-media completion watchdog
        # re-arms wake listening immediately afterwards.
        self._pause_wake_listening()
        logger.info("Executing quick media command: action=%s target=%s", command.action, command.target)
        target_hint = str(command.target or "auto").casefold()
        if target_hint == "discord":
            self._suppress_media_notice("discord", 8.0)
        elif target_hint in {"yandex", "yandex_music"}:
            self._suppress_media_notice("yandex_music", 8.0)
        else:
            # Auto transport can resolve to either active owner; suppress only the
            # asynchronous duplicate bubble, never the actual playback action.
            self._suppress_media_notice("discord", 8.0)
            self._suppress_media_notice("yandex_music", 8.0)
        try:
            self._set_status("Быстрая медиа-команда…")
            self.robot.set_state("working")
        except Exception:
            pass
        # The acknowledgement is visible BEFORE dispatch/IPC/network work. It
        # must never be delayed by the typewriter or overwritten by the result.
        from .quick_media_messages import acknowledgement
        self._clear_listening_bubble()
        domain = "video" if str(getattr(command, "target", "") or "") == "video" else ""
        response_bubble = self._append_assistant(acknowledgement(command.action, command.recognized or command.heard, self._language_code(), domain), paced=False, mirror_telegram=False)
        if not str(command.heard or "").startswith("панель:"):
            # Accepted voice command only; duplicate probes and result callbacks
            # never move the reader. A final-transcript request coalesces here.
            self.chat.scroll_to_bottom(force=True)
        worker = FunctionWorker(self._execute_quick_media_command, command)
        worker.signals.result.connect(lambda result, bubble=response_bubble, serial=serial: self._quick_media_finished(result, bubble=bubble, serial=serial))
        worker.signals.error.connect(lambda error, bubble=response_bubble, domain=domain, serial=serial: self._quick_media_failed(error, bubble=bubble, domain=domain, serial=serial))
        # The robot remains in the working/gear animation until the actual result
        # callback, not merely until the worker thread emits its generic finished
        # signal. This makes wakeless commands visibly transactional.
        self._quick_media_pool.start(worker)

    def _quick_media_failed(self, trace: str, *, bubble=None, domain: str = "", serial: int | None = None) -> None:
        if bool(getattr(self, "_quitting", False)):
            return
        _release_quick_media_transport188(self, serial)
        logger.error("Quick media command failed:\n%s", trace)
        try:
            from .quick_media_messages import message
            failure_text = self.agent.personalize_external_response(message("failed_video" if domain == "video" else "failed", self._language_code()))
            self._set_status(failure_text)
            if bubble is not None:
                self.chat.update_bubble(bubble, failure_text)
                self._mirror_assistant_to_telegram(failure_text)
            elif self._listening_bubble is not None:
                pending_bubble = self._listening_bubble
                self._listening_bubble = None
                self.chat.update_bubble(pending_bubble, failure_text)
            else:
                self._append_assistant(failure_text, paced=False)
        except Exception:
            pass
        try:
            if not self._busy:
                self.robot.set_state("idle")
        except Exception:
            pass
        if self._continuous_voice_session and not self._busy:
            QTimer.singleShot(70, lambda: self._start_command_capture(seamless=False) if self._continuous_voice_session and not self._busy else None)
        else:
            self._schedule_wake_watchdog(45)

    def _quick_media_finished(self, result: Any, *, bubble=None, serial: int | None = None) -> None:
        if bool(getattr(self, "_quitting", False)):
            return
        trace_data = getattr(self, "_quick_media_trace", None)
        if trace_data:
            latency_profiler().record("quick_media_dispatch_to_response", time.monotonic() - trace_data[1], correlation_id=trace_data[0])
        _release_quick_media_transport188(self, serial)
        data = result if isinstance(result, dict) else {}
        text = str(data.get("text", "") or "").strip()
        if text:
            display_text = self.agent.personalize_external_response(text) if not bool(data.get("ok", False)) else text
            self._set_status(display_text)
            if bubble is not None:
                self.chat.update_bubble(bubble, display_text)
                self._mirror_assistant_to_telegram(display_text)
            elif self._listening_bubble is not None:
                pending_bubble = self._listening_bubble
                self._listening_bubble = None
                self.chat.update_bubble(pending_bubble, display_text)
            else:
                self._append_assistant(display_text, paced=False)
        elif self._listening_bubble is not None:
            # A successful silent transport action should not leave a stale
            # ``Думаю`` row behind forever.
            self._clear_listening_bubble()
        # Mark tracks already named by the acknowledgement so the asynchronous
        # event notification does not immediately duplicate the same chat bubble.
        for item in list(data.get("now_playing") or []):
            if not isinstance(item, dict):
                continue
            source = str(item.get("content_provider") or item.get("source", "") or "").casefold()
            stable = str(item.get("track_id", "") or "").strip()
            if stable:
                key = f"{source}:{stable}"
            else:
                key = f"{source}:{str(item.get('title','')).casefold().strip()}|{str(item.get('artist','')).casefold().strip()}"
            if source and key:
                self._last_now_playing_by_source[source] = key
        if bubble is not None and text and data.get("ok"):
            from jarvis.runtime.media_events import latest_now_playing
            for item in list(data.get("now_playing") or []):
                provider = str(item.get("content_provider") or item.get("source") or "")
                event = latest_now_playing(provider)
                if event is not None and str(item.get("track_id") or "") == event.track_id:
                    self._remember_media_bubble157(event, bubble, text)
        action = str(data.get("action", "") or "").casefold()
        target = str(data.get("target", "") or "").casefold()
        now_items = [x for x in list(data.get("now_playing") or []) if isinstance(x, dict)]
        if now_items and bool(data.get("ok", False)):
            item = dict(now_items[-1])
            source = str(item.get("source", target or "music") or "music").casefold()
            self._music_overlay_last_event = {
                "source": source,
                "source_label": str(item.get("label", "") or "Музыка"),
                "title": str(item.get("title", "") or ""),
                "track_id": str(item.get("track_id", "") or ""),
                "artist": str(item.get("artist", "") or ""),
                "position_seconds": max(0.0, float(item.get("position_ms", 0) or 0) / 1000.0),
                "duration_seconds": max(0.0, float(item.get("duration_ms", 0) or 0) / 1000.0),
                "playing": action != "pause" and not bool(data.get("starting", False)),
                "paused": action == "pause",
                "captured_monotonic": time.monotonic(),
            }
        elif bool(data.get("ok", False)) and not data.get("starting") and self._music_overlay_last_event and action in {"pause", "resume"}:
            state = self._music_overlay_last_event
            # Freeze/resume inferred timeline for backends that do not expose a
            # direct position probe (notably Discord voice).
            captured = float(state.get("captured_monotonic", time.monotonic()) or time.monotonic())
            pos = float(state.get("position_seconds", 0.0) or 0.0)
            if bool(state.get("playing", False)):
                pos += max(0.0, time.monotonic() - captured)
            state["position_seconds"] = pos
            state["captured_monotonic"] = time.monotonic()
            state["playing"] = action == "resume"
            state["paused"] = action == "pause"
        self._music_overlay_state_cache = {}
        self._music_overlay_state_cache_at = 0.0
        self._refresh_robot_stage_info()
        logger.info("Quick media result: %s", text or data)
        try:
            if not self._busy:
                self.robot.set_state("idle")
        except Exception:
            pass
        if not self._busy:
            if self._continuous_voice_session:
                QTimer.singleShot(70, lambda: self._start_command_capture(seamless=False) if self._continuous_voice_session and not self._busy else None)
            else:
                self._schedule_wake_watchdog(45)

    def _launch_wake_transcription(self, recording: RecordingResult, generation: int) -> None:
        if generation != self._wake_generation or self._quitting:
            return
        if self._busy and self._voice_mode != "interrupt_wake":
            return
        duration = float(recording.audio.size) / max(1, int(recording.sample_rate))

        def onset_is_wake_like(probe: str) -> bool:
            phrase = str(self.config.get("wake_word.phrase", "") or "")
            aliases = tuple(_configured_wake_terms(self.config))
            head = " ".join(probe.split()[:2])
            return any(not wake_alias_requires_standalone(token)
                       and looks_wake_like(token, phrase, minimum=0.62, aliases=aliases)
                       for token in [head] + head.split())

        backoff = getattr(self, "_wake_backoff", None)
        if backoff is not None and backoff.should_skip(recording.correlation_id, duration, onset_is_wake_like):
            logger.debug("Wake decode skipped during nearby conversation: correlation=%s duration=%.2f skipped=%d",
                         recording.correlation_id, duration, backoff.skipped)
            return
        self._wake_worker_active = True
        if not self._wake_result_timer.isActive():
            self._wake_result_timer.start()

        def run_wake_asr() -> None:
            started = time.perf_counter()
            decode_started_at = time.monotonic()
            try:
                result = self._transcribe_wake_robust(recording)
                if isinstance(result, dict) and backoff is not None:
                    if result.get("matched") or result.get("fast_command") or result.get("fast_capture") or result.get("voice_unlock"):
                        backoff.record_wake()
                    else:
                        backoff.record_rejected(str(result.get("text", "") or ""), duration)
                if isinstance(result, dict):
                    result["decoded_at"] = time.monotonic()
                    result["started_at"] = decode_started_at
                    result["decode_ms"] = (time.perf_counter() - started) * 1000.0
                self._enqueue_wake_result(("result", result, generation, recording))
            except BaseException:
                self._enqueue_wake_result(("error", traceback.format_exc(), generation, recording))
            finally:
                elapsed = time.perf_counter() - started
                latency_profiler().record("ASR:wake", elapsed, "wake", correlation_id=recording.correlation_id)
                developer_bus().emit("latency", "Wake ASR", data={"stage": "wake_asr", "duration_ms": elapsed * 1000.0}, duration_ms=elapsed * 1000.0)
                self._enqueue_wake_result(("finished", None, generation, recording))

        thread = threading.Thread(
            target=run_wake_asr,
            name=f"NeoJarvis-WakeASR-{generation}",
            daemon=True,
        )
        self._wake_thread = thread
        thread.start()

    def _poll_wake_results(self) -> None:
        for _ in range(16):
            try:
                kind, payload, generation, recording = self._wake_result_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "result":
                self._wake_transcribed(payload, generation, recording)
            elif kind == "error":
                self._wake_failed(str(payload), generation)
            elif kind == "finished":
                self._wake_worker_finished(generation)
            elif kind == "probe_result":
                self._fast_wake_probe_transcribed(payload, generation, recording)
            elif kind == "probe_error":
                logger.debug("Wake streaming ASR probe failed: %s", payload)
            elif kind == "probe_finished":
                self._wake_probe_finished(generation)

    def _wake_worker_finished(self, generation: int) -> None:
        self._wake_worker_active = False
        if MainWindow._start_pending_wake160(self):
            return
        if self._wake_result_timer.isActive() and not self._wake_probe_worker_active:
            self._wake_result_timer.stop()

    def _wake_transcribed(self, result: Any, generation: int, recording: RecordingResult | None = None) -> None:
        received_at = time.monotonic()
        if self._quitting:
            return
        # A completed/cancelled capture is not reusable after the generation
        # changes: a late rolling window could otherwise switch the next track
        # after Stop or duplicate a command already handled by the fast probe.
        payload = result if isinstance(result, dict) else {"text": str(result or ""), "matched": False}
        # Provenance check must happen before stale quick-media handling too: a
        # delayed result originating from speaker echo must never be executed just
        # because the wake generation advanced while ASR was running.
        if self._probable_system_playback_echo(recording, stage="wake-result"):
            return
        if recording:
            self._last_voice_correlation = recording.correlation_id
        if generation == self._wake_generation and self._output_stop_from_voice(str(payload.get('text', '') or '')):
            return
        if generation != self._wake_generation:
            logger.debug("Discarding stale voice capture: %s != %s", generation, self._wake_generation)
            return
        # Keep the interrupt contract stable even if an audio callback briefly
        # changes _voice_mode before the wake/quick result is delivered.
        interrupt_mode = bool(self._busy and (self._interrupt_wake_active or self._voice_mode == "interrupt_wake"))
        if self._busy and not interrupt_mode:
            return
        heard = str(payload.get("text", "") or "").strip()
        if bool(payload.get("voice_unlock", False)):
            self._dispatch_voice_unlock(heard)
            return
        fast_command = payload.get("fast_command")
        if isinstance(fast_command, QuickMediaCommand):
            # Preserve the mature barge-in contract: while the assistant itself is
            # talking/working, a bare «стоп» still cancels that operation. Explicit
            # «стоп музыка/видео/Discord/Яндекс» remains an instant media command.
            bare = re.sub(r"[^a-zа-я]+", " ", fast_command.heard.casefold()).strip()
            if interrupt_mode and fast_command.target == "auto" and bare in {"стоп", "стап", "stop", "пауза", "pause"}:
                self._interrupt_current_operation(source="direct-stop")
                return
            self._dispatch_quick_media_command(fast_command)
            return
        fast_capture = payload.get("fast_capture")
        if isinstance(fast_capture, QuickCapture) and not interrupt_mode:
            self._dispatch_quick_capture(fast_capture)
            return
        phrase = str(self.config.get("wake_word.phrase", "")).strip()
        matched = bool(payload.get("matched", False))
        remainder = str(payload.get("remainder", "") or "").strip()
        if not matched:
            # A rejected rolling window must not regain permission for a weak
            # standalone alias while crossing the GUI result boundary.
            matched, remainder, _score = match_wake_phrase(
                heard, phrase, aliases=tuple(_configured_wake_terms(self.config)),
                threshold=float(self.config.get("wake_word.fuzzy_threshold", 0.88)),
                allow_standalone_aliases=bool(getattr(recording, "completed_utterance", False)),
            )
        if interrupt_mode:
            if not matched and bool(self.config.get("voice.interrupt_without_wake", True)) and self._is_interrupt_phrase(heard):
                logger.info("Barge-in direct interrupt phrase matched: %r", heard)
                self._interrupt_current_operation(source="direct-stop")
                return
            if matched:
                logger.info("Barge-in wake matched; cancelling current operation")
                self._interrupt_current_operation(command=remainder, source=str(payload.get("source", "wake") or "wake"))
                return
            return
        if not matched:
            # Lifelong ambient learning reuses the transcript that wake detection
            # already paid for. There is deliberately no second microphone STT
            # pass here; command/wake latency and CPU remain unchanged.
            if heard and self._looks_like_linguistic_text(heard):
                try:
                    ambient = getattr(self, "ambient_learning", None)
                    if ambient is not None:
                        ambient.observe_microphone_transcript(heard)
                except Exception:
                    logger.debug("Ambient microphone observation failed", exc_info=True)
            return

        source = str(payload.get("source", "") or "")
        self._post_interrupt_require_wake = False
        logger.info("Wake transition: matched source=%s", source or "unknown")
        self._wake_heard173()
        if recording is not None and recording.captured_at > 0:
            started_at = float(payload.get("started_at", recording.captured_at))
            decoded_at = float(payload.get("decoded_at", received_at))
            logger.info(
                "Wake pipeline: source=%s capture_to_ui_ms=%.1f decode_ms=%s audio_s=%.2f correlation=%s queue_ms=%.1f ui_delivery_ms=%.1f model=%s language=%s",
                source or "unknown", max(0.0, received_at - recording.captured_at) * 1000.0,
                round(float(payload["decode_ms"]), 1) if "decode_ms" in payload else "unknown",
                recording.audio.size / max(1, recording.sample_rate), recording.correlation_id,
                max(0.0, started_at - recording.captured_at) * 1000.0,
                max(0.0, received_at - decoded_at) * 1000.0,
                str(self.config.get("wake_word.stt_model", "tiny")),
                str(payload.get("decode_language", "acoustic" if source == "openwakeword" else self.wake_recognizer._wake_language())),
            )
        # Invalidate queued wake chunks before changing mode. No Qt QRunnable or
        # native UI operation happens here; only the isolated audio child is told
        # to change logical mode.
        self._wake_generation += 1
        self._pending_wake_recording = None
        self._wake_worker_active = False
        self._sleeping = False
        self._restart_idle_timer()
        interaction_mode = str(self.config.get("voice.interaction_mode", "") or "").strip().casefold()
        if interaction_mode not in {"continuous", "wake_each_command"}:
            interaction_mode = "continuous" if bool(self.config.get("voice.continuous_after_wake", False)) else "wake_each_command"
        self._continuous_voice_session = interaction_mode == "continuous"
        # Visible acknowledgement is emitted at the exact moment the wake word is
        # accepted, before waiting for the audio-worker promotion acknowledgement.
        # That makes the UI react to "Jarvis" immediately while the same microphone
        # stream is already being promoted in the background.
        self.robot.set_state("listening")
        self._set_status("Слушаю…")
        self.mic_level.show()
        self._show_listening_bubble()
        if recording:
            if recording.captured_at > 0:
                latency_profiler().record("wake_capture_to_ui", time.monotonic() - recording.captured_at, "capture emission to acknowledgement", correlation_id=recording.correlation_id)
            latency_profiler().record("wake_result_to_ui", time.monotonic() - float(payload.get("decoded_at", received_at)), correlation_id=recording.correlation_id)
        # Audio duck begins at the semantic start of interaction: the wake word
        # has been accepted and NEO is now listening/working for the user.
        self._duck_for_listening()

        # A suffix is only evidence of command onset, never executable partial
        # text. Full ASR still receives original PCM, including words spoken while
        # wake decoding was in flight. Wake-only PCM must wait for a new onset.
        if recording is not None:
            recording.wake_has_command = bool(remainder or payload.get("command_tail", False))
        self._promote_wake_to_command(recording)

    def _voice_after_acoustic_wake(self, text: str) -> None:
        value = str(text or "").strip()
        phrase = str(self.config.get("wake_word.phrase", "") or "").strip()
        matched, remainder = self._match_wake_phrase(value, phrase) if value else (False, "")
        if matched:
            value = remainder.strip()
        value = self._normalize_recognized_command(value)
        if not value or len(value) <= 2:
            self._start_command_after_acoustic_wake()
            return
        self._voice_recognized(value)

    def _start_command_after_acoustic_wake(self) -> None:
        self._busy = False
        self._heard_timer.stop()
        if self._live_user_bubble is not None:
            try:
                self.chat.remove_bubble(self._live_user_bubble)
            except Exception:
                pass
            self._live_user_bubble = None
        self._voice_mode = "command_pending"
        QTimer.singleShot(0, lambda: self._start_command_capture(seamless=True, resume_from_last_finish=True))

    def _match_wake_phrase(self, heard: str, phrase: str) -> tuple[bool, str]:
        aliases = tuple(_configured_wake_terms(self.config))
        matched, remainder, _score = match_wake_phrase(
            heard,
            phrase,
            threshold=float(self.config.get("wake_word.fuzzy_threshold", 0.88)),
            aliases=aliases,
        )
        return matched, remainder

    def _wake_failed(self, trace: str, generation: int) -> None:
        if generation != self._wake_generation:
            return
        logger.warning("Wake phrase recognition failed: %s", trace)
        # The next audio chunk is already recording; keep the loop alive silently.
        self._set_status("Wake word: продолжаю слушать…")

    def _handle_error(self, trace: str) -> None:
        logger.error(trace)
        self._stream_flush_timer.stop()
        self._stream_pending_text = ''
        briefing = getattr(self, '_response_briefing158', None)
        if briefing is not None:
            briefing.finish()
        self._response_briefing158 = None
        reply_activity = getattr(self.robot, 'set_reply_activity', None)
        if callable(reply_activity):
            reply_activity(False)
        self._heard_timer.stop()
        if self._live_user_bubble is not None:
            self.chat.update_bubble(self._live_user_bubble, "Команда не распознана")
            self._live_user_bubble = None
        last_line = trace.strip().splitlines()[-1] if trace.strip() else "Неизвестная ошибка"
        if self._pending_response_bubble is not None:
            error_text = self.agent.personalize_external_response(f"Ошибка: {last_line}")
            if briefing is not None and briefing.render():
                error_text = briefing.render() + '\n\n' + error_text
            self.chat.update_bubble(self._pending_response_bubble, error_text)
            self._mirror_assistant_to_telegram(error_text)
            self._pending_response_bubble = None
        else:
            self._append_assistant(self.agent.personalize_external_response(f"Ошибка: {last_line}"))
        self._typing_timer.stop()
        self._set_busy(False, "Ошибка", "error")
        # 186: a terminal foreground error frees the output channel too; passive
        # notices that queued up meanwhile (timers, reminders) resume here.
        if (self._ambient_typing_bubble is not None or self._ambient_typing_queue) and not self._ambient_typing_timer.isActive():
            self._ambient_typing_timer.start()
        self.mic_button.setText("🎙")
        if self._continuous_voice_session:
            QTimer.singleShot(max(50, int(self.config.get("voice.continuous_rearm_ms", 35) or 35)), lambda: self._start_command_capture(seamless=False) if self._continuous_voice_session and not self._busy else None)
        else:
            self._resume_wake_listening(delay_ms=80)

    def _offer_default_model184(self) -> None:
        """One visible, persisted invitation for new profiles; never starts a pull."""
        if self._quitting or getattr(self, "_model_setup_offer184", None) is not None:
            return
        if self.config.get("local_chat.setup_offer_pending184", False) is not True:
            return
        if getattr(self, "_settings_dialog", None) is not None or self._voice_mode == "command":
            return
        from jarvis.ollama_models import DEFAULT_CHAT_MODEL, local_endpoint
        selected = str(self.config.get("local_chat.model", "") or self.config.get("ollama.chat_model", DEFAULT_CHAT_MODEL))
        eligible = selected == DEFAULT_CHAT_MODEL and bool(self.config.get("local_chat.enabled", True))
        try:
            local_endpoint(str(self.config.get("ollama.base_url", "http://localhost:11434")))
        except ValueError:
            eligible = False

        def consume(data):
            chat = data.setdefault("local_chat", {})
            if chat.get("setup_offer_pending184") is not True:
                return False
            chat["setup_offer_pending184"] = False
        try:
            if not self.config.update(consume) or not eligible:
                return
        except Exception:
            logger.exception("Could not persist model setup invitation")
            return
        code = self._language_code()
        box = QMessageBox(self)
        box.setAttribute(Qt.WA_DeleteOnClose, True)
        box.setModal(False)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(tr("Разговорная модель NEO", code))
        box.setText(tr("Первая разговорная модель: qwen2.5:0.5b (около 398 МБ).", code))
        box.setInformativeText(tr("Скачивание начнётся только после нажатия «Скачать». Уже установленная модель будет проверена без повторной загрузки. Прогресс и отмена доступны в настройках; статус также появится в чате.", code))
        for source, action, role in (("Скачать", "download", QMessageBox.AcceptRole),
                                     ("Открыть настройки модели", "settings", QMessageBox.ActionRole),
                                     ("Позже", "later", QMessageBox.RejectRole)):
            button = box.addButton(tr(source, code), role)
            button.setProperty("model_setup_action184", action)
            if action == "later":
                box.setDefaultButton(button)
                box.setEscapeButton(button)
        box.buttonClicked.connect(self._model_setup_choice184)
        box.finished.connect(self._model_setup_closed184)
        self._model_setup_offer184 = box
        box.show()

    def _model_setup_choice184(self, button) -> None:
        if self._quitting:
            return
        action = button.property("model_setup_action184")
        if action not in {"download", "settings"}:
            return
        self._open_settings()
        dialog = getattr(self, "_settings_dialog", None)
        if dialog is not None:
            dialog.focus_conversation_model184(download=action == "download")

    def _model_setup_closed184(self, *_args) -> None:
        self._model_setup_offer184 = None

    def _open_settings(self) -> None:
        # HOTFIX51: settings is a singleton *modeless* dialog. The old modal
        # dialog disabled the gear button until dialog.exec() returned. If the
        # user minimized the settings window, the button therefore looked dead
        # until NEO was restarted. Re-clicking the gear now restores/raises the
        # existing dialog instead of spawning a second copy.
        existing = getattr(self, "_settings_dialog", None)
        if existing is not None:
            try:
                if existing.isMinimized():
                    existing.showNormal()
                else:
                    existing.show()
                existing.raise_()
                existing.activateWindow()
                return
            except RuntimeError:
                self._settings_dialog = None
            except Exception:
                logger.debug("Could not restore existing settings dialog", exc_info=True)

        # Settings must remain reachable even while an agent worker is thinking.
        # Only active command microphone capture competes with settings editing.
        if self._voice_mode == "command":
            return
        self._pause_wake_listening()
        opened_at = time.perf_counter()
        logger.info("Settings open requested")
        try:
            # Flush the current live window geometry before Settings snapshots
            # config values. This closes the last 700 ms debounce race where a
            # freshly resized window could open Settings with the previous size
            # and later snap back on Apply.
            self._save_geometry()
            dialog = SettingsDialog(self.config, self, deferred=True)
            dialog.setAttribute(Qt.WA_DeleteOnClose, True)
            logger.info("Settings dialog constructed duration_ms=%.1f", (time.perf_counter() - opened_at) * 1000.0)
            self._settings_dialog = dialog
            dialog.setModal(False)
            dialog.settings_saved.connect(self._settings_updated)
            dialog.model_notice184.connect(self._model_operation_message)
            dialog.model_operation_started184.connect(self._watch_model_operation184)
            dialog.scenario_run_requested.connect(self._run_shelf_scenario)
            dialog.scenario_retry_requested.connect(self._retry_shelf_scenario)
            dialog.finished.connect(self._settings_dialog_closed)
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
        except Exception as exc:
            self._settings_dialog = None
            logger.exception("Could not open settings dialog")
            QMessageBox.critical(self, tr("Настройки", self._language_code()), self._localize_text(f"Не удалось открыть настройки: {exc}"))
            self._resume_wake_listening(delay_ms=250)

    def _watch_model_operation184(self, relay) -> None:
        if not hasattr(self, "_model_progress_bubbles184"):
            self._model_progress_bubbles184 = {}
        if not hasattr(self, "_model_inventory_operations184"):
            self._model_inventory_operations184 = {}
        self._model_inventory_operations184[relay.operation_id] = self.agent.ollama.reserve_model_inventory()
        self._model_progress_bubbles184[relay.operation_id] = None
        self._model_progress_operation184 = relay.operation_id
        self._model_progress_bubble = None
        relay.status.connect(self._model_operation_message)
        relay.disposed.connect(self._model_operation_finished184)
        relay.terminal_result.connect(self._model_operation_terminal184)

    def _model_operation_finished184(self, operation_id: str) -> None:
        getattr(self, "_model_progress_bubbles184", {}).pop(operation_id, None)
        getattr(self, "_model_inventory_operations184", {}).pop(operation_id, None)

    def _model_operation_message(self, message: str, operation_id: str = "") -> None:
        """Each operation keeps its own bubble, including late terminal results."""
        if self._quitting:
            return
        bubbles = getattr(self, "_model_progress_bubbles184", {})
        if operation_id and operation_id not in bubbles:
            return  # The conversation was reset while this operation was active.
        if not operation_id:
            self._model_progress_operation184 = None
        bubble = bubbles.get(operation_id) if operation_id else None
        try:
            if bubble is not None:
                self.chat.update_bubble(bubble, str(message))
            else:
                bubble = self.chat.add_assistant(str(message))
                if operation_id:
                    bubbles[operation_id] = bubble
            if not operation_id or operation_id == getattr(self, "_model_progress_operation184", None):
                self._model_progress_bubble = bubble
        except (RuntimeError, AttributeError):
            if operation_id:
                bubbles.pop(operation_id, None)
    def _model_operation_terminal184(self, result: dict) -> None:
        """Only a completed operation can change inventory/availability state."""
        if self._quitting:
            return
        client = self.agent.ollama
        base_url = str(result.get("base_url", "")).rstrip("/")
        if base_url != client.base_url:
            return
        models = result.get("models")
        mutation = result.get("action") in {"download", "delete"}
        sequence = getattr(self, "_model_inventory_operations184", {}).get(str(result.get("operation_id", "")))
        accepted = client.accept_model_inventory(
            base_url, models if isinstance(models, list) else None,
            sequence=sequence, invalidate=mutation,
        )
        # A late successful mutation needs a fresh availability check; a stale
        # read-only completion cannot override newer inventory/availability.
        if (accepted or mutation) and (result.get("ready") or result.get("status") in {"installed", "ready"}):
            from jarvis.runtime.local_chat169 import reset_availability
            reset_availability()
        from jarvis.ollama_status import reset_status
        reset_status()

    def _settings_dialog_closed(self, _result: int = 0) -> None:
        self._settings_dialog = None
        if not self._busy and not self._quitting:
            self._resume_wake_listening(delay_ms=250)

    def _poll_timer_events_and_hud(self) -> None:
        self._drain_timer_events()
        self._drain_translation_events184()
        self._weekly_learning_report172()
        self._refresh_timer_stage_label()
        self._sync_discord_timer_summary()

    def _sync_discord_timer_summary(self) -> None:
        """Publish timer identity/due time only when the timer set changes.

        Discord never receives a one-second countdown stream: that would create
        needless message edits/rate pressure and recreate the white loading rows the
        music dashboard is designed to avoid. Absolute due times stay correct on
        their own and require an edit only on create/cancel/expiry.
        """
        manager = getattr(self, "_timer_manager", None)
        if manager is None:
            return
        rows = list(manager.list())
        # HIST-030: a duration shows its monotonic projection (a wall-clock jump re-publishes).
        shown_due = getattr(manager, "display_due", None)
        due_of = {str(x.id): float(shown_due(x)) if callable(shown_due) else float(x.due_at) for x in rows[:6]}
        signature = tuple((str(x.id), str(x.label), int(round(due_of[str(x.id)]))) for x in rows[:6])
        if signature == getattr(self, "_discord_timer_signature", None):
            return
        if (signature == getattr(self, "_discord_timer_failed187", (None, 0.0))[0]
                and time.monotonic() < getattr(self, "_discord_timer_failed187", (None, 0.0))[1]):
            return          # bounded retry after a known failure, not every tick
        self._discord_timer_signature = signature
        now = time.time()
        labels: list[str] = []
        for item in rows[:6]:
            due = time.localtime(due_of[str(item.id)])
            if time.localtime(now)[:3] == due[:3]:
                stamp = time.strftime("%H:%M:%S", due)
            else:
                stamp = time.strftime("%d.%m %H:%M", due)
            labels.append(f"{item.label} — {stamp}")
        def deliver() -> None:
            try:
                adapter.sync_timer_summaries(labels)
            except Exception:
                # NEO 187: a known failure must not leave the unchanged set
                # marked as delivered; the next tick may try again.
                if getattr(self, "_discord_timer_signature", None) == signature:
                    self._discord_timer_signature = None
                    self._discord_timer_failed187 = (signature, time.monotonic() + 30.0)
                logger.debug("Discord timer summary delivery failed", exc_info=True)
        try:
            from jarvis.integrations.discord_navigation import shared_reliable_discord_adapter
            adapter = shared_reliable_discord_adapter(self.config)
            # Network/Gateway work must never block the Qt timer HUD.
            threading.Thread(target=deliver, name="neo-discord-timer-sync", daemon=True).start()
        except Exception:
            self._discord_timer_signature = None
            logger.debug("Discord timer summary scheduling failed", exc_info=True)

    def _refresh_timer_stage_label(self) -> None:
        if getattr(self, "timer_list_button184", None) is not None:
            from jarvis.ui.timer_overlay184 import refresh_overlay
            refresh_overlay(self)
            return
        label = getattr(self, "timer_stage_label", None)
        manager = getattr(self, "_timer_manager", None)
        if label is None or manager is None:
            return
        if not bool(self.config.get("ui.timer_overlay_enabled", True)):
            label.hide()
            return
        rows = manager.list()
        if not rows:
            label.hide()
            return
        now = time.time()
        lines=[]
        for item in rows[:4]:
            remaining=max(0,int(math.ceil(manager.remaining(item))))
            hours, rem=divmod(remaining,3600); minutes, seconds=divmod(rem,60)
            stamp=f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"
            lines.append(f"{item.label} — {stamp}")
        if len(rows)>4:
            lines.append(f"+{len(rows)-4}")
        label.setText("\n".join(lines))
        label.adjustSize()
        label.show()
        label.raise_()
        self._position_timer_stage_label()

    def _position_timer_stage_label(self) -> None:
        if getattr(self, "timer_list_button184", None) is not None:
            from jarvis.ui.timer_overlay184 import refresh_overlay
            refresh_overlay(self)
            return
        label=getattr(self,"timer_stage_label",None); robot=getattr(self,"robot",None)
        if label is None or robot is None or not label.isVisible():
            return
        margin=max(8,int(10*getattr(self,"_scale",1.0)))
        position=str(self.config.get("ui.timer_overlay_position","bottom_left") or "bottom_left").casefold()
        # Nine deterministic anchors. Positioning is calculated from the current
        # label dimensions on every refresh, so changing font/size cannot drift.
        if position.endswith("_right"):
            x=max(margin, robot.width()-label.width()-margin)
        elif position.endswith("_center") or position == "center":
            x=max(margin, (robot.width()-label.width())//2)
        else:
            x=margin
        if position.startswith("top_"):
            y=margin
        elif position.startswith("center_") or position == "center":
            y=max(margin, (robot.height()-label.height())//2)
        else:
            y=max(margin, robot.height()-label.height()-margin)
        label.move(int(x), int(y))

    def _startup_selfcheck173(self) -> None:
        if self._quitting:
            return
        from jarvis.runtime import fieldkit173
        ollama = getattr(getattr(self, "agent", None), "ollama", None)
        config = self.config
        code = self._language_code()

        def ollama_ok() -> bool:
            from jarvis.ollama_status import readiness
            return bool(readiness(str(config.get("ollama.base_url", "http://127.0.0.1:11434")), retries=1)["ready"])

        def agent_model_ok() -> bool:
            try:
                return bool(ollama and ollama.resolve_agent_model())
            except Exception:
                return False

        def ocr_ok() -> bool:
            try:
                from winsdk.windows.media.ocr import OcrEngine
                return OcrEngine.try_create_from_user_profile_languages() is not None
            except Exception:
                return False

        def work() -> str:
            problems = fieldkit173.startup_problems(config, ollama_ok=ollama_ok, agent_model_ok=agent_model_ok, ocr_ok=ocr_ok)
            return fieldkit173.problems_text(fieldkit173.not_told_recently(config, problems), code)
        worker = FunctionWorker(work)
        worker.signals.result.connect(lambda text: self._append_assistant(str(text)) if text else None)
        worker.signals.error.connect(lambda trace: logger.debug("Startup self-check failed: %s", trace))
        self.thread_pool.start(worker)

    def _warm_up174(self) -> None:
        """NEO 174: build the phrase tables in the background so the first command is fast too."""
        agent = getattr(self, "agent", None)
        if agent is None or self._quitting:
            return
        from jarvis.runtime import speed174
        code = self._language_code()
        worker = FunctionWorker(lambda: speed174.warm_up(agent, code))
        worker.signals.error.connect(lambda trace: logger.debug("Warm-up failed: %s", trace))
        self.thread_pool.start(worker)

    def _typing_started174(self, text: str) -> None:
        """NEO 174: the first keystrokes of a command load the NEO model in the background (at most every 20 s)."""
        now = time.monotonic()
        if not str(text or "").strip() or now < getattr(self, "_typing_warm_at174", 0.0):
            return
        self._typing_warm_at174 = now + 20.0
        self._wake_heard173()

    def _wake_heard173(self) -> None:
        """NEO 173: while the user is still speaking, open the service connections and load the NEO model."""
        try:
            from jarvis.runtime import http_pool173, model_life173
            http_pool173.prewarm(self.config)
            ollama = getattr(getattr(self, "agent", None), "ollama", None)
            if ollama is not None:
                model_life173.register(ollama)
                model_life173.preload(ollama)
        except Exception:
            logger.debug("Wake warm-up failed", exc_info=True)

    def _weekly_learning_report172(self) -> None:
        """NEO 172: once a week, «как я вас понимаю» in the chat (measured off the UI thread)."""
        now = time.monotonic()
        if now < getattr(self, "_learning_check_at172", 0.0) or self._quitting:
            return
        self._learning_check_at172 = now + 3600.0
        from jarvis.runtime import learning172
        if not learning172.weekly_due(self.config):
            return
        code = self._language_code()
        worker = FunctionWorker(lambda: learning172.report_text(self.config, code))
        worker.signals.result.connect(lambda text: self._append_assistant(str(text)) if text else None)
        worker.signals.error.connect(lambda trace: logger.debug("Weekly learning report failed: %s", trace))
        self.thread_pool.start(worker)

    def _drain_translation_events184(self) -> None:
        """NEO 184: progress of the translation model download (and the queued translation) in the chat."""
        try:
            from jarvis.runtime import translation184
            for text in translation184.installer().drain(8):
                self._append_assistant(text)
        except Exception:
            logger.debug("Translation model events could not be shown", exc_info=True)

    def _drain_timer_events(self) -> None:
        manager = getattr(self, "_timer_manager", None)
        if manager is None:
            return
        queued = getattr(self, "_scheduled_commands172", None)
        if queued is None:
            from collections import deque
            queued = self._scheduled_commands172 = deque(maxlen=8)
        actions = getattr(self, "_delayed_actions184", None)
        if actions is None:
            from collections import deque
            actions = self._delayed_actions184 = deque()
        active_ids = {item.id for item in manager.list()} if actions else None
        if active_ids is not None:
            self._delayed_actions184 = actions = type(actions)(row for row in actions if row[1] in active_ids)
        for event in manager.drain(32):
            text = str(getattr(event, "text", "") or "").strip()
            if not text:
                continue
            kind = str(getattr(event, "kind", "chat") or "chat")
            if kind == "action":
                if active_ids is None:
                    active_ids = {item.id for item in manager.list()}
                if event.timer_id in active_ids:
                    actions.append((text, event.timer_id))
                continue
            if kind == "command":
                # NEO 172: a scheduled command runs like a typed one — visible in the chat.
                queued.append(text)
                continue
            if kind == "system":
                try:
                    if self.tray.isVisible():
                        self.tray.showMessage("NEO", text, QSystemTrayIcon.Information, 10000)
                except Exception:
                    logger.exception("Timer system notification failed")
            else:
                # PHRASE185-CONFIRM-PAYLOAD-01: the event is already written in the UI
                # language; re-localizing it would replace a Cyrillic label («Купить
                # хлеб») by a generic fallback in a foreign locale.
                written = str(getattr(event, "language", "") or "")
                self._append_assistant(text, localize=not written or written != self._language_code())
        if (actions or queued) and not getattr(self, "_busy", False) and not self._quitting:
            agent = getattr(self, "agent", None)
            if getattr(agent, "_pending_context", None) or getattr(agent, "_pending_target_choice", None):
                return
            # HIST-006: a due scheduled command is not an answer to the user's open
            # question (note text, name, choice…). It waits until that question is
            # answered, cancelled or expired instead of filling the user's slot.
            from jarvis.runtime.voice_followup156 import pending_slot
            try:
                if agent is not None and pending_slot(agent, self._language_code()) is not None:
                    return
            except Exception:
                logger.debug("Pending-slot check before a scheduled command failed", exc_info=True)
            next_command = actions.popleft() if actions else queued.popleft()
            if isinstance(next_command, tuple):
                text, timer_id = next_command
                if not any(item.id == timer_id for item in manager.list()):
                    return  # cancelled after becoming due
                self._send_text(text, input_source="text", user_submission=False, delayed_action_id184=timer_id)
            else:
                self._send_text(next_command, input_source="text", user_submission=False)

    def _poll_bluetooth_devices_async(self) -> None:
        if self._quitting or bool(getattr(self, "_bluetooth_monitor_inflight", False)):
            return
        if not (bool(self.config.get("security.bluetooth_connect_notifications", True)) or bool(self.config.get("security.bluetooth_battery_notifications", True))):
            return
        self._bluetooth_monitor_inflight = True
        worker = FunctionWorker(list_bluetooth_devices, background=True)
        worker.signals.result.connect(self._bluetooth_devices_ready)
        worker.signals.error.connect(lambda trace: logger.debug("Bluetooth monitor failed: %s", trace))
        worker.signals.finished.connect(lambda: setattr(self, "_bluetooth_monitor_inflight", False))
        self.thread_pool.start(worker)

    def _bluetooth_devices_ready(self, rows: object) -> None:
        devices = [dict(x) for x in (rows or []) if isinstance(x, dict)] if isinstance(rows, list) else []
        current = {str(x.get("name", "") or "").casefold(): x for x in devices if str(x.get("name", "") or "").strip()}
        previous = dict(getattr(self, "_bluetooth_snapshot", {}) or {})
        # First observation establishes a connection baseline so startup does not
        # announce every already-connected device. Low battery is still worth a
        # one-time warning immediately after startup.
        for key, item in current.items():
            was = bool((previous.get(key) or {}).get("connected", False)) if previous else bool(item.get("connected", False))
            now_connected = bool(item.get("connected", False))
            name = str(item.get("name", "Bluetooth") or "Bluetooth")
            if now_connected and not was and bool(self.config.get("security.bluetooth_connect_notifications", True)):
                self._append_assistant(f"🔵 Bluetooth подключён: {name}.")
            battery = item.get("battery")
            threshold = max(5, min(50, int(self.config.get("security.bluetooth_battery_threshold_percent", 20) or 20)))
            if now_connected and isinstance(battery, int) and battery <= threshold and bool(self.config.get("security.bluetooth_battery_notifications", True)):
                if key not in self._bluetooth_battery_warned:
                    self._bluetooth_battery_warned.add(key)
                    self._append_assistant(f"🪫 Низкий заряд Bluetooth-устройства «{name}»: {battery}%.")
            elif isinstance(battery, int) and battery > threshold + 5:
                self._bluetooth_battery_warned.discard(key)
        self._bluetooth_snapshot = current

    def _configure_mail_monitor_timer(self) -> None:
        timer = getattr(self, "_mail_monitor_timer", None)
        if timer is None:
            return
        intervals: list[int] = []
        for provider in ("gmail", "mailru"):
            # If notifications are disabled, do not keep an unnecessary mailbox
            # poller alive. Daily briefing performs its own one-shot status read.
            if bool(self.config.get(f"integrations.{provider}.enabled", False)) and bool(
                self.config.get(f"integrations.{provider}.notifications_enabled", True)
            ):
                try:
                    intervals.append(max(30, min(900, int(self.config.get(f"integrations.{provider}.poll_seconds", 60) or 60))))
                except Exception:
                    intervals.append(60)
        if not intervals:
            timer.stop()
            return
        timer.setInterval(min(intervals) * 1000)
        if not timer.isActive():
            timer.start()

    def _poll_mail_async(self) -> None:
        if self._quitting or bool(getattr(self, "_mail_monitor_inflight", False)):
            return
        providers = [
            provider for provider in ("gmail", "mailru")
            if bool(self.config.get(f"integrations.{provider}.enabled", False))
            and bool(self.config.get(f"integrations.{provider}.notifications_enabled", True))
        ]
        if not providers:
            return
        self._mail_monitor_inflight = True
        # HIST-033: a provider with alerts off is not polled (nor waited for) by the monitor.
        worker = FunctionWorker(collect_statuses, self.config, include_latest=20, providers=providers)
        worker.signals.result.connect(self._mail_statuses_ready)
        worker.signals.error.connect(lambda trace: logger.debug("Mail monitor failed: %s", trace))
        worker.signals.finished.connect(lambda: setattr(self, "_mail_monitor_inflight", False))
        self.thread_pool.start(worker)

    def _mail_statuses_ready(self, payload: object) -> None:
        if getattr(self, "_quitting", False):
            return      # NEO 187: a result that arrives during shutdown is dropped
        rows = dict(payload or {}) if isinstance(payload, dict) else {}
        for provider in ("gmail", "mailru"):
            status = rows.get(provider)
            if not bool(self.config.get(f"integrations.{provider}.enabled", False)):
                continue    # disabled after the poll was admitted
            configured = str(self.config.get("integrations.gmail.account_email" if provider == "gmail"
                                             else "integrations.mailru.email", "") or "").strip()
            if (isinstance(status, dict) and configured and str(status.get("account", "") or "").strip()
                    and configured.casefold() != str(status.get("account", "")).strip().casefold()):
                continue    # the configured account changed while the poll ran
            if not isinstance(status, dict) or status.get("error"):
                if isinstance(status, dict) and status.get("error"):
                    logger.debug("%s mail monitor unavailable: %s", provider, status.get("error"))
                continue
            latest = [dict(x) for x in (status.get("latest", []) or []) if isinstance(x, dict)]
            current_ids = {str(x.get("message_id", "") or "") for x in latest if str(x.get("message_id", "") or "")}
            account = str(status.get("account", "") or "")
            previous_account = str(self._mail_seen_accounts.get(provider, "") or "")
            if provider not in self._mail_seen_ids or (previous_account and account and previous_account.casefold() != account.casefold()):
                # Baseline only: never dump an old inbox as a wall of startup notifications.
                self._mail_seen_ids[provider] = set(current_ids)
                self._mail_seen_accounts[provider] = account
                continue
            previous = set(self._mail_seen_ids.get(provider, set()))
            new_messages = [item for item in reversed(latest) if str(item.get("message_id", "") or "") not in previous]
            # Keep a deterministic bounded rolling set.  Always preserve IDs from
            # the current latest window first, then fill the remainder from the
            # previous baseline; set iteration order must never decide retention.
            bounded_ids = set(current_ids)
            if len(bounded_ids) < 250:
                for old_id in sorted(previous):
                    if len(bounded_ids) >= 250:
                        break
                    bounded_ids.add(old_id)
            self._mail_seen_ids[provider] = bounded_ids
            self._mail_seen_accounts[provider] = account
            if not new_messages or not bool(self.config.get(f"integrations.{provider}.notifications_enabled", True)):
                continue
            try:
                text = format_mail_notification(self.config, provider, status, new_messages)
                if text.strip():
                    # Mail content can be private. Do not automatically forward
                    # background inbox notifications into the Telegram remote.
                    # NEO 187: already localized; sender/subject are user data,
                    # so runtime re-localization must not touch this text.
                    self._append_assistant(text, mirror_telegram=False, localize=False)
            except Exception:
                logger.debug("Could not format %s mail notification", provider, exc_info=True)

    def _start_morning_briefing(self) -> None:
        if (self._quitting or getattr(self, '_briefing_started158', False)
                or not bool(self.config.get('briefing.enabled', True))
                or not bool(self.config.get('briefing.show_on_startup', True))):
            return
        from jarvis.runtime.briefing_stream import BriefingDocument, STARTUP_TIMEOUT_MS
        self._briefing_started158 = True
        self._briefing_generation = getattr(self, '_briefing_generation', 0) + 1
        generation = self._briefing_generation
        self._briefing_user_generation158 = self._agent_generation
        self._briefing_active = True
        self._briefing_cancel = threading.Event()
        self._briefing_document158 = BriefingDocument(self._language_code())
        self._briefing_bubble158 = None
        self._briefing_delivered158 = False
        # Share the retry-aware collector budget, with extra time for registry
        # preparation and final queued GUI delivery. Never cut off retry 2/3.
        self._briefing_deadline159 = QTimer(self)
        self._briefing_deadline159.setSingleShot(True)
        self._briefing_deadline159.timeout.connect(
            lambda g=generation: self._morning_briefing_failed(g, 'DeadlineExceeded'))
        self._briefing_deadline159.start(STARTUP_TIMEOUT_MS)
        # This first section is a pure local formatting operation. Even a blocked
        # registry or stalled provider cannot prevent the date/time from appearing.
        from datetime import datetime
        from jarvis.integrations.daily_briefing import date_and_time_line
        self._briefing_document158.update('greeting', date_and_time_line(
            datetime.now(), self._language_code(),
            include_time=bool(self.config.get('briefing.include_time', True))))
        self._render_morning_briefing158()
        briefing_reply_activity(self, 'ready')
        worker = FunctionWorker(self._run_morning_briefing, generation, self._briefing_cancel,
                                progress_keyword='on_event')
        worker.signals.progress.connect(self._morning_briefing_part)
        worker.signals.result.connect(self._morning_briefing_ready)
        worker.signals.error.connect(lambda trace, g=generation: self._morning_briefing_failed(g, trace))
        self._briefing_worker = worker  # Keep Qt signal owner alive until delivery.
        # Do not queue the first date/time behind startup's network prewarm jobs.
        threading.Thread(target=worker.run, daemon=True, name='NEO-Startup-Briefing').start()

    def _run_morning_briefing(self, generation, cancel, *, on_event):
        from jarvis.runtime.briefing_stream import stream_to
        with stream_to(lambda event: on_event(dict(event, generation=generation)), cancel):
            execution = self.agent.tools.execute('briefing', {'action': 'startup_summary'})
        return generation, execution

    def _morning_briefing_part(self, event) -> None:
        if (not isinstance(event, dict) or self._quitting
                or not getattr(self, '_briefing_active', False)
                or event.get('generation') != getattr(self, '_briefing_generation', None)):
            return
        section, text = event.get('section', ''), str(event.get('text', '') or '')
        document = getattr(self, '_briefing_document158', None)
        if document is None or not document.update(section, text, event.get('state', 'ready'),
                                                   partial_text=event.get('partial_text')):
            return
        self._render_morning_briefing158()
        briefing_reply_activity(self, str(event.get('state', 'ready')))

    def _render_morning_briefing158(self) -> bool:
        document = getattr(self, '_briefing_document158', None)
        text = document.render() if document is not None else ''
        if not text:
            return False
        if self._briefing_bubble158 is None:
            # TZ-SUMMARY-01: the startup briefing types out like an ordinary reply.
            self._briefing_bubble158 = self._append_assistant("", paced=False, mirror_telegram=False, localize=False)
        else:
            try:
                self._briefing_bubble158.objectName()
            except RuntimeError:
                # A cleared chat is not permission to resurrect old results.
                self._briefing_cancel.set()
                self._briefing_active = False
                return False
        _reveal187(self).show(self._briefing_bubble158, text)
        self._briefing_delivered158 = self._briefing_bubble158 is not None
        return self._briefing_delivered158

    def _cancel_morning_briefing158(self) -> None:
        briefing_reply_activity(self, finished=True)
        timer = getattr(self, '_briefing_deadline159', None)
        if timer is not None:
            timer.stop()
        event = getattr(self, '_briefing_cancel', None)
        if event is not None:
            event.set()
        document = getattr(self, '_briefing_document158', None)
        if document is not None and getattr(self, '_briefing_active', False):
            document.finish(cancelled=True)
            self._render_morning_briefing158()
        self._briefing_generation = getattr(self, '_briefing_generation', 0) + 1
        self._briefing_active = False

    def _morning_briefing_failed(self, generation, trace):
        if (generation != getattr(self, '_briefing_generation', None)
                or self._quitting or not getattr(self, '_briefing_active', False)):
            return
        briefing_reply_activity(self, finished=True)
        self._briefing_active = False
        timer = getattr(self, '_briefing_deadline159', None)
        if timer is not None:
            timer.stop()
        cancel = getattr(self, '_briefing_cancel', None)
        if cancel is not None:
            cancel.set()
        logger.warning('Morning briefing failed: %s', trace)
        document = getattr(self, '_briefing_document158', None)
        if document is not None:
            from jarvis.runtime.briefing_stream import status_text
            document.update('briefing', status_text('briefing', self._language_code(), failed=True), 'error')
            document.finish()
            self._render_morning_briefing158()
        self._rearm_voice_after_answer()

    def _morning_briefing_ready(self, payload) -> None:
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        generation, execution = payload
        if (generation != getattr(self, '_briefing_generation', None) or self._quitting
                or not getattr(self, '_briefing_active', False)):
            return
        briefing_reply_activity(self, finished=True)
        self._briefing_active = False
        timer = getattr(self, '_briefing_deadline159', None)
        if timer is not None:
            timer.stop()
        document = getattr(self, '_briefing_document158', None)
        data = getattr(execution, 'data', None)
        data = data if isinstance(data, dict) else {}
        if document is not None:
            # Use the progressive document, not a second final message. When a
            # provider failed, its waiting row becomes a clear unavailable row.
            if not bool(getattr(execution, 'success', False)):
                from jarvis.runtime.briefing_stream import status_text
                document.update('briefing', status_text('briefing', self._language_code(), failed=True), 'error')
            document.finish(cancelled=bool(data.get('cancelled', False)))
            self._render_morning_briefing158()
        if not bool(getattr(execution, 'success', False)):
            logger.warning('Morning briefing failed: %s', getattr(execution, 'text', ''))
        elif getattr(self, '_briefing_delivered158', False) and not data.get('cancelled'):
            # Receipt is after the GUI applied content, never on tool execution.
            # Its small disk write also stays outside the GUI thread.
            from datetime import datetime
            moment = str(data.get('time', '') or '')
            try:
                delivered_at = datetime.fromisoformat(moment) if moment else datetime.now()
            except ValueError:
                delivered_at = datetime.now()
            QTimer.singleShot(0, lambda g=generation, at=delivered_at:
                              self._record_morning_briefing_delivery158(g, at))
        self._rearm_voice_after_answer()

    def _record_morning_briefing_delivery158(self, generation, delivered_at) -> None:
        if self._quitting or generation != getattr(self, '_briefing_generation', None):
            return
        bubble = getattr(self, '_briefing_bubble158', None)
        try:
            if bubble is None or bubble.item not in self.chat.items or not bubble.text.strip():
                return
        except RuntimeError:
            return
        from jarvis.integrations.daily_briefing import DailyBriefingStore
        store = DailyBriefingStore(self.config)
        threading.Thread(target=store.mark_startup_delivered, args=(delivered_at,),
                         daemon=True, name='NEO-Briefing-Receipt').start()

    def _discord_adapter(self):
        return getattr(getattr(self.agent, "tools", None), "discord_adapter", None)

    def _prewarm_discord_async(self) -> None:
        adapter = self._discord_adapter()
        if adapter is None or self._quitting:
            return
        worker = FunctionWorker(adapter.prewarm_background, 7.0)
        worker.signals.result.connect(self._discord_prewarm_ready)
        worker.signals.error.connect(lambda trace: logger.debug("Discord observer prewarm failed: %s", trace))
        self.thread_pool.start(worker)

    def _discord_prewarm_ready(self, data: object) -> None:
        logger.info("Discord observer prewarm: %s", data)
        if self._quitting:
            return
        ready = int((data or {}).get("ready", 0) or 0) if isinstance(data, dict) else 0
        # V4.1 could time out its 7 s prewarm a fraction before Discord Gateway
        # emitted READY.  Do not treat that one timing race as permanent.  Start
        # a bounded cold-attach handshake after the event loop has had time to
        # settle; no voice join or media mutation happens here.
        delay = 650 if ready > 0 else 1800
        QTimer.singleShot(delay, lambda: self._start_discord_cold_attach(force_probe=True))

    def _start_discord_cold_attach(self, *, force_probe: bool = False) -> None:
        if self._quitting or self._discord_observer_inflight or self._discord_cold_attach_done:
            return
        adapter = self._discord_adapter()
        if adapter is None:
            return
        observe = getattr(adapter, "observe_shared_playback", None)
        if not callable(observe):
            return
        self._discord_observer_inflight = True
        worker = FunctionWorker(observe, "", force_probe=bool(force_probe), wait=1.1)
        worker.signals.result.connect(self._discord_cold_attach_ready)
        worker.signals.error.connect(lambda trace: logger.debug("Discord cold attach unavailable: %s", trace))
        worker.signals.finished.connect(lambda: setattr(self, "_discord_observer_inflight", False))
        self.thread_pool.start(worker)

    def _discord_cold_attach_ready(self, data: object) -> None:
        if self._quitting or not isinstance(data, dict):
            return
        current = data.get("current") if isinstance(data.get("current"), dict) else {}
        title = str(current.get("title", "") or current.get("query", "") or "").strip()
        usable_title = title if is_player_facing_media_title(title) else ""
        if usable_title:
            self._discord_cold_attach_done = True
            self._discord_observer_ready(data)
            logger.info("Discord cold attach acquired already-playing track: %s", usable_title)
            return
        # Playback without display metadata is only a partial attach.  Feed the
        # audio owner/meter immediately but keep probing until title/artist are
        # recovered; never lock the startup handshake on the placeholder.
        if bool(data.get("playing")) or bool(data.get("paused")):
            self._discord_observer_ready(data)
        self._discord_cold_attach_attempt += 1
        # Gateway reconnects and a same-token shared host can take a few seconds
        # to exchange the first control packet.  Retry only a handful of times;
        # the normal 5 s observer remains the long-term self-healing path.
        retry_delays = (1600, 3200, 6500, 12000)
        if self._discord_cold_attach_attempt <= len(retry_delays):
            delay = retry_delays[self._discord_cold_attach_attempt - 1]
            QTimer.singleShot(delay, lambda: self._start_discord_cold_attach(force_probe=True))

    def _poll_discord_observer(self) -> None:
        if self._quitting or self._discord_observer_inflight:
            return
        adapter = self._discord_adapter()
        if adapter is None:
            return
        # No GUI blocking: Gateway discovery/control-bus status can take seconds
        # during reconnect and therefore always runs on the worker pool.
        self._discord_observer_inflight = True
        last_seen = float(getattr(self, "_discord_observer_last_seen", 0.0) or 0.0)
        cold = last_seen <= 0.0 or time.monotonic() - last_seen > 18.0
        observe = getattr(adapter, "observe_shared_playback", None)
        if cold and callable(observe):
            worker = FunctionWorker(observe, "", force_probe=False, wait=0.45)
        else:
            worker = FunctionWorker(adapter.music_queue_status, "")
        worker.signals.result.connect(self._discord_observer_ready)
        worker.signals.error.connect(lambda trace: logger.debug("Passive Discord observer unavailable: %s", trace))
        worker.signals.finished.connect(lambda: setattr(self, "_discord_observer_inflight", False))
        self.thread_pool.start(worker)

    def _discord_observer_ready(self, data: object) -> None:
        if self._quitting or not isinstance(data, dict):
            return
        current = data.get("current") if isinstance(data.get("current"), dict) else {}
        raw_title = str(current.get("title", "") or current.get("query", "") or "").strip()
        title = raw_title if is_player_facing_media_title(raw_title) else ""
        artist = str(current.get("artist", "") or "").strip() if title else ""
        track_id = str(current.get("track_id", "") or "").strip() if title else ""
        playing = bool(data.get("playing"))
        paused = bool(data.get("paused"))
        from jarvis.runtime.media_events import latest_now_playing, stale_playback_observation
        latest = latest_now_playing("discord")
        generation = int(data.get("playback_generation", 0) or 0)
        scope = str(data.get("playback_scope", "") or "")
        if latest and stale_playback_observation(scope, generation,
                current_scope=latest.playback_scope, current_generation=latest.playback_generation):
            return
        if (data.get("verified") is True and not data.get("shared_state_pending")
                and not data.get("stale_shared_snapshot")):
            if data.get("playing") is False and data.get("paused") is False:
                # An explicit stopped/disconnected observation supersedes old
                # queue work, even when it has no title to publish as an event.
                self._discord_queue_observation_inactive158 = True
                reader = getattr(self, "_media_queue_reader158", None)
                if reader is not None:
                    reader.invalidate("discord")
                if not title:
                    # Empty metadata still carries confirmed terminal state.
                    # Publish it without a popup so older queued active events
                    # cannot revive this playback's preview on the next drain.
                    publish_now_playing(
                        "discord", "", playing=False, reason="playback_ended",
                        announce=False, observation=True,
                        playback_scope=scope, playback_generation=generation,
                    )
            elif data.get("playing") is True or data.get("paused") is True:
                self._discord_queue_observation_inactive158 = False
        # Audio ownership is independent from metadata availability.  This keeps
        # the belly waveform alive while a remote title is still being recovered.
        discord_session_meter().set_active(bool(playing and not paused and self._music_visuals_visible()))
        note_media_observation("discord", playing=bool(playing and not paused), paused=bool(paused))
        now = time.monotonic()
        if not title:
            self._discord_observer_last_seen = now
            previous = dict(self._music_overlay_last_event or {})
            previous_title = str(previous.get("title", "") or "").strip()
            if str(previous.get("source", "") or "").casefold() == "discord" and is_player_facing_media_title(previous_title):
                # Never let an unresolved passive snapshot overwrite a real title
                # learned from a quick media command or earlier host broadcast.
                previous["playing"] = playing
                previous["paused"] = paused
                previous["captured_monotonic"] = now
                self._music_overlay_last_event = previous
            elif playing or paused:
                # Keep a metadata-less Discord state so the spectrum provider is
                # still polled and the robot can animate before title recovery.
                self._music_overlay_last_event = {
                    "source": "discord", "source_label": localized_source_name("discord", self._language_code()),
                    "title": "", "artist": "", "playing": playing, "paused": paused,
                    "captured_monotonic": now,
                    "levels": list(data.get("levels") or []),
                    "audio_levels_real": bool(data.get("audio_levels_real", False)),
                    "audio_levels_kind": str(data.get("audio_levels_kind", "") or ""),
                }
            self._music_overlay_state_cache = {}
            self._music_overlay_state_cache_at = 0.0
            self._refresh_robot_stage_info()
            return
        key = track_id or (title.casefold() + "|" + artist.casefold())
        duration_ms = int(current.get("duration_ms", 0) or current.get("expected_duration_ms", 0) or data.get("duration_ms", 0) or 0)
        position_ms = int(current.get("position_ms", 0) or data.get("position_ms", 0) or 0)
        self._music_overlay_last_event = {
            "source": "discord", "source_label": localized_source_name("discord", self._language_code()),
            "title": title, "artist": artist,
            "position_seconds": max(0.0, position_ms / 1000.0),
            "duration_seconds": max(0.0, duration_ms / 1000.0),
            "playing": playing, "paused": paused, "captured_monotonic": now,
            "levels": list(data.get("levels") or []),
            "audio_levels_real": bool(data.get("audio_levels_real", False)),
            "audio_levels_kind": str(data.get("audio_levels_kind", "") or ""),
        }
        self._music_overlay_state_cache = {}
        self._music_overlay_state_cache_at = 0.0
        self._discord_observer_last_seen = now
        # Repeated status is still useful for queue changes, but it must retain
        # the physical event identity used by the asynchronous preview callback.
        if title:
            self._discord_observer_last_key = key
            # One shared event fans out to desktop chat and connected Telegram.
            publish_now_playing(
                "discord", title, artist, track_id, reason="shared_discord_observer",
                duration_ms=duration_ms, position_ms=position_ms, playing=playing and not paused,
                observation=True, playback_scope=str(data.get("playback_scope", "") or ""),
                playback_generation=int(data.get("playback_generation", 0) or 0),
            )
        self._refresh_robot_stage_info()

    def _now_playing_queue_preview(self, source: str, *, limit: int = 5, refresh: bool = False) -> str:
        """Return cached queue text; provider locks/IPC always stay off Qt."""
        from jarvis.runtime.media_notice156 import queue_notice
        from jarvis.runtime.media_events import latest_now_playing
        from jarvis.ui.media_queue158 import MediaQueuePreview
        src = str(source or "").strip().casefold()
        owner = "yandex_music" if src in {"yandex", "yandex_music", "яндекс музыка", "youtube"} else src
        if owner not in {"yandex_music", "discord"} or getattr(self, "_quitting", False):
            return ""
        if owner == "discord" and getattr(self, "_discord_queue_observation_inactive158", False):
            return queue_notice(None, self._language_code())
        reader = getattr(self, "_media_queue_reader158", None)
        if reader is None:
            reader = self._media_queue_reader158 = MediaQueuePreview(self, self._media_queue_ready158)
        current = latest_now_playing(src)
        language = self._language_code()
        server = self._quick_media_server() if owner == "discord" else ""
        count = max(1, min(5, int(limit)))
        registry = self.agent.tools
        identity = (src, current.event_id if current else "", current.playback_generation if current else 0,
                    language, server, count, current.playback_scope if current else "", id(registry))
        arguments = ({"action": "queue_status"} if owner == "yandex_music" else
                     {"action": "music_queue_status", "server": server})

        def load():
            result = registry.execute(owner, arguments)
            if not getattr(result, "success", False) or not isinstance(result.data, dict) or "queue" not in result.data:
                return queue_notice(None, language)
            if owner == "discord" and current is not None:
                # A status request may straddle next/reconnect or return a cached
                # remote host snapshot. Never attach that queue to another playback.
                data = result.data
                observed = data.get("current")
                if data.get("shared_state_pending") or data.get("stale_shared_snapshot"):
                    return queue_notice(None, language)
                if (current.playback_scope and data.get("playback_scope") != current.playback_scope
                        or current.playback_generation and data.get("playback_generation") != current.playback_generation):
                    return queue_notice(None, language)
                if isinstance(observed, dict):
                    tid = str(observed.get("track_id", "") or "")
                    title = str(observed.get("title", "") or observed.get("query", "") or "")
                    artist = str(observed.get("artist", "") or "")
                    if ((current.track_id and tid and current.track_id != tid)
                            or (not tid or not current.track_id) and (current.title, current.artist) != (title, artist)):
                        return queue_notice(None, language)
            data = result.data
            if owner == "yandex_music" and current is not None and isinstance(data.get("current"), dict):
                # REC-QUEUE-REFRESH-SOURCE-01: the local player may already belong
                # to another source; never attach its queue to this event.
                tid = str(data["current"].get("id", "") or data["current"].get("track_id", "") or "")
                if current.track_id and tid and tid != current.track_id:
                    return queue_notice(None, language)
            from jarvis.runtime.media_notice156 import queue_labels
            labels = queue_labels(data.get("queue") or [], language, count)
            if not labels and (data.get("queue_refilling") or data.get("queue_unavailable")):
                # Refilling / unavailable is "waiting for next tracks", not empty.
                return queue_notice(None, language)
            return queue_notice(labels, language)

        value = (reader.request(owner, identity, load, refresh=True)
                 if refresh else reader.request(owner, identity, load))
        return value if value is not None else queue_notice(None, language)

    def _media_queue_ready158(self, _owner: str, identity: tuple) -> None:
        if getattr(self, "_quitting", False):
            return
        if len(identity) != 8 or identity[7] != id(self.agent.tools):
            return
        if identity[0] == "discord" and getattr(self, "_discord_queue_observation_inactive158", False):
            return
        from jarvis.runtime.media_events import latest_now_playing
        current = latest_now_playing(identity[0])
        if identity[3] != self._language_code() or (identity[0] == "discord" and identity[4] != self._quick_media_server()):
            return
        if (current and current.event_id == identity[1]
                and current.playback_generation == identity[2] and current.playback_scope == identity[6]):
            self._refresh_media_bubble_queue157(current)

    def _drain_media_now_playing_events(self) -> None:
        events = drain_now_playing(32)
        if not events:
            return
        notify = bool(self.config.get("ui.media_now_playing_notifications", True))
        for event in events:
            source = str(event.source or "media").casefold()
            reason = str(getattr(event, "reason", "track_started") or "track_started").casefold()
            if reason in {"queue_updated", "network_unavailable"}:
                from jarvis.runtime.media_events import latest_now_playing
                current = latest_now_playing(source)
                if reason == "queue_updated" and current is not None and current.key == event.key:
                    # MEDIA185-U11: a committed queue edit always invalidates the
                    # cached/in-flight projection and refreshes the existing
                    # bubble, independent of popup notification preferences.
                    reader = getattr(self, "_media_queue_reader158", None)
                    if reader is not None:
                        reader.invalidate("yandex_music" if source in {"yandex", "yandex_music", "youtube"} else source)
                    if not bool(getattr(event, "announce", True)) or not notify:
                        self._refresh_media_bubble_queue157(current)
                        continue
                if not notify or not bool(getattr(event, "announce", True)):
                    continue
                if reason == "queue_updated":
                    if (current is None or current.key != event.key or not current.playing
                            or current.event_id != getattr(event, "playback_event_id", "")
                            or (event.playback_generation and current.playback_generation != event.playback_generation)):
                        continue
                    if self._refresh_media_bubble_queue157(current):
                        continue
                    if getattr(self, "_busy", False):
                        deferred = getattr(self, "_deferred_queue_notice157", None)
                        if deferred is None:
                            deferred = self._deferred_queue_notice157 = {}
                        deferred[source] = event
                        continue
                    from jarvis.runtime.media_notice156 import now_playing_notice
                    track = f"«{current.title}»" + (f" — {current.artist}" if current.artist else "")
                    notice = now_playing_notice(localized_source_name(source, self._language_code()), track, self._now_playing_queue_preview(source), self._language_code())
                    bubble = self._append_assistant(notice, mirror_telegram=False, paced=False)
                    self._remember_media_bubble157(current, bubble, notice)
                else:
                    from jarvis.runtime.media_notice156 import network_notice
                    self._append_assistant(network_notice(self._language_code()), mirror_telegram=False, paced=False)
                continue
            if reason == "queue_added":
                # External Discord /play actions are user activity, not a change
                # of the currently playing track. Show them in the desktop chat
                # but never overwrite the overlay/player state. Telegram receives
                # the same event from its own media subscriber, so avoid mirroring
                # this bubble a second time through the transcript bridge.
                requester = str(getattr(event, "requester", "") or "Discord").strip()
                track = f"«{event.title}»" + (f" — {event.artist}" if event.artist else "")
                position = int(getattr(event, "queue_position", 0) or 0)
                from jarvis.runtime.media_notice156 import queue_added_notice
                self._append_assistant(
                    "Discord: " + queue_added_notice(requester, track, position, self._language_code()),
                    mirror_telegram=False, paced=False,
                )
                continue
            if source == "discord":
                # A passive shared-queue broadcast can arrive before the next
                # 5 s UI observer tick.  Arm the crash-isolated Windows Discord
                # audio-session meter immediately so the robot belly waveform
                # reacts to music started by another user/NEO as soon as the
                # metadata event is observed.
                observed_playing = bool(getattr(event, "playing", True))
                from jarvis.runtime.media_events import latest_now_playing
                if latest_now_playing("discord") is event:
                    if observed_playing:
                        self._discord_queue_observation_inactive158 = False
                    elif reason in {"stopped", "owner_left", "voice_empty", "playback_ended"}:
                        self._discord_queue_observation_inactive158 = True
                        reader = getattr(self, "_media_queue_reader158", None)
                        if reader is not None:
                            reader.invalidate("discord")
                discord_session_meter().set_active(bool(observed_playing and self._music_visuals_visible()))
                note_media_observation("discord", playing=observed_playing, paused=not observed_playing)
                self._discord_observer_last_seen = time.monotonic()
            self._music_overlay_last_event = {
                "source": "yandex_music" if source == "youtube" else source,
                "content_provider": source,
                "source_label": localized_source_name(source, self._language_code()),
                "title": str(event.title or ""),
                "track_id": str(getattr(event, "track_id", "") or ""),
                "artist": str(event.artist or ""),
                "position_seconds": max(0.0, float(getattr(event, "position_ms", 0) or 0) / 1000.0),
                "duration_seconds": max(0.0, float(getattr(event, "duration_ms", 0) or 0) / 1000.0),
                "playing": bool(getattr(event, "playing", True)),
                "paused": not bool(getattr(event, "playing", True)),
                "captured_monotonic": time.monotonic(),
            }
            # A passive event may arrive between two robot/overlay polls.
            # Invalidate the tiny 20 ms provider cache so the track HUD and
            # waveform consume this exact event on the very next GUI refresh.
            self._music_overlay_state_cache = {}
            self._music_overlay_state_cache_at = 0.0
            handled = getattr(self, "_last_now_playing_event158", None)
            if handled is None:
                handled = self._last_now_playing_event158 = {}
            if handled.get(source) == event.event_id:
                if reason in {"shared_discord_observer", "shared_queue_state"}:
                    # Same physical playback: refresh the snapshot without
                    # cancelling its admitted read every observer tick.
                    # Otherwise a read slower than the observer interval can
                    # never publish, even when every identity matches.
                    self._refresh_media_bubble_queue157(event, preserve_pending=True)
                continue
            handled[source] = event.event_id
            self._last_now_playing_by_source[source] = event.key
            # The command result owns its chat reply. The event still updates
            # the overlay above; automatic track transitions remain audible.
            if not bool(getattr(event, "announce", True)):
                continue
            aliases = {source}
            if source in {"yandex", "yandex_music", "youtube"}:
                aliases.update({"yandex", "yandex_music"})
            suppressed = any(
                time.monotonic() < float(self._media_notice_suppressed_until.get(key, 0.0) or 0.0)
                for key in aliases
            )
            if suppressed:
                logger.debug("Suppressed duplicate now-playing bubble source=%s key=%s", source, event.key)
                continue
            if notify and bool(getattr(event, "playing", True)):
                label = localized_source_name(source, self._language_code())
                track = f"«{event.title}»" + (f" — {event.artist}" if event.artist else "")
                queue_preview = self._now_playing_queue_preview(source)
                from jarvis.runtime.media_notice156 import now_playing_notice
                notice = now_playing_notice(label, track, queue_preview, self._language_code())
                bubble = self._append_assistant(notice)
                self._remember_media_bubble157(event, bubble, notice)
        self._refresh_robot_stage_info()

    def _music_overlay_state(self) -> dict[str, Any]:
        """Return non-invasive now-playing state for the music panel.

        Calls from the overlay and robot may land within the same UI frame. A
        tiny cache prevents duplicate loopback status probes while still allowing
        ~30 Hz spectrum updates.
        """
        now = time.monotonic()
        cache = dict(getattr(self, "_music_overlay_state_cache", {}) or {})
        cache_at = float(getattr(self, "_music_overlay_state_cache_at", 0.0) or 0.0)
        if cache and now - cache_at <= 0.020:
            return cache
        try:
            local = local_player().cached_status() or {}
        except Exception:
            local = {}
        local_state = str(local.get("state", "") or "").casefold()
        if (local.get("title") or local.get("artist")) and local_state in {"playing", "paused"}:
            provenance = dict(self._music_overlay_last_event or {})
            provider = str(provenance.get("content_provider") or provenance.get("active_content_provider") or "yandex_music")
            result = {
                # Transport ownership stays local-player/Yandex; provenance is a
                # separate display label so YouTube audio keeps its own name.
                "source": "yandex_music", "transport_owner": "yandex_music",
                "content_provider": provider,
                "source_label": str(provenance.get("media_source") or localized_source_name(provider, self._language_code())),
                "title": str(local.get("title", "") or ""), "artist": str(local.get("artist", "") or ""),
                "position_seconds": max(0.0, float(local.get("position_ms", 0) or 0) / 1000.0),
                "duration_seconds": max(0.0, float(local.get("duration_ms", 0) or 0) / 1000.0),
                "playing": local_state == "playing", "paused": local_state == "paused",
                "levels": list(local.get("levels") or []),
                "audio_levels_real": bool(local.get("audio_levels_real", False)),
                "audio_levels_kind": str(local.get("audio_levels_kind", "") or ""),
            }
            self._music_overlay_state_cache = dict(result)
            self._music_overlay_state_cache_at = now
            return result
        state = dict(self._music_overlay_last_event or {})
        if state.get("source") in {"yandex", "yandex_music", "youtube"} and local_state in {"stopped", "unknown"}:
            # A newer actual local stop/crash (or a stale/unreachable worker)
            # takes precedence over an old track-start broadcast.
            state.update(playing=False, paused=False, title="", artist="", levels=[])
        if state and bool(state.get("playing", False)):
            captured = float(state.get("captured_monotonic", now) or now)
            position = float(state.get("position_seconds", 0.0) or 0.0) + max(0.0, now - captured)
            duration = float(state.get("duration_seconds", 0.0) or 0.0)
            state["position_seconds"] = min(position, duration) if duration > 0 else position
            if duration > 0 and position > duration + 3.0:
                state.update(playing=False, paused=False, title="", artist="", levels=[])
            source = str(state.get("source", "") or "").casefold()
            if source == "discord":
                levels, measured = media_levels("discord", max_age=0.7, width=96)
                kind = "spectrum" if measured else ""
                if not measured:
                    local_levels, local_measured = media_levels("discord_session", max_age=0.25, width=96)
                    if local_measured:
                        levels, measured, kind = local_levels, True, "amplitude"
                if not measured and state.get("levels") and bool(state.get("audio_levels_real", False)):
                    levels = list(state.get("levels") or [])
                    measured = True
                    kind = str(state.get("audio_levels_kind", "") or "spectrum")
                state["levels"] = levels
                state["audio_levels_real"] = measured
                state["audio_levels_kind"] = kind
        self._music_overlay_state_cache = dict(state)
        self._music_overlay_state_cache_at = now
        return state

    def _music_visuals_visible(self):
        if hasattr(self, "robot") and self.robot.isVisible():
            return True
        manager = getattr(self, "overlay_manager", None)
        return bool(manager and any(panel.isVisible() and str(getattr(panel, "_model", {}).get("purpose", "")) == "music" for panel in manager.panels.values()))

    def showEvent(self, event):
        super().showEvent(event)
        if not self.isMinimized():
            self._runtime_demoted184 = False
        if os.name == "nt":
            try:
                import ctypes
                from jarvis.runtime.self_window184 import ensure_taskbar_minimize
                if not ensure_taskbar_minimize(int(self.winId()), ctypes.windll.user32):
                    logger.debug("Taskbar minimize style could not be confirmed")
            except Exception:
                logger.debug("Taskbar minimize style check failed", exc_info=True)
        if hasattr(self, "_robot_info_timer"):
            QTimer.singleShot(0, self._refresh_robot_stage_info)
        media = getattr(self, "_music_overlay_last_event", {}) or {}
        if media.get("source") == "discord" and media.get("playing"):
            discord_session_meter().set_active(True)

    def changeEvent(self, event):
        super().changeEvent(event)
        # Normal user/taskbar restore is also a return from demotion. Observe
        # existing Qt events only; do not activate, raise, or poll the window.
        if not getattr(self, "_runtime_demoted184", False) or self.isMinimized():
            return
        if ((event.type() == QEvent.Type.WindowStateChange
             and bool(event.oldState() & Qt.WindowMinimized))
                or (event.type() == QEvent.Type.ActivationChange and self.isActiveWindow())):
            self._runtime_demoted184 = False

    def hideEvent(self, event):
        super().hideEvent(event)
        if not self._music_visuals_visible():
            discord_session_meter().set_active(False)

    def _refresh_robot_stage_info(self) -> None:
        if self._quitting or not hasattr(self, "robot"):
            return
        if not self.robot.isVisible():
            if self._robot_info_timer.interval() != 750:
                self._robot_info_timer.setInterval(750)
            return
        # The belly equalizer is automatic. Poll media state whenever NEO has
        # an active/recent music owner, or when the optional track label is shown.
        owner = str(recent_music_owner(max_age_seconds=24 * 60 * 60) or "")
        wants_media = (
            bool(self.config.get("ui.robot_info_track_enabled", False))
            or bool(owner)
            or bool((self._music_overlay_last_event or {}).get("playing", False))
            or (time.monotonic() - float(getattr(self, "_discord_observer_last_seen", 0.0) or 0.0) < 3.0)
        )
        media: dict[str, Any] = self._music_overlay_state() if wants_media else {}
        if media and not bool(media.get("playing", False)):
            media = dict(media)
            media["title"] = ""
            media["artist"] = ""
        units = str(self.config.get("integrations.openweather.units", "metric") or "metric").casefold()
        unit_label = "°F" if units == "imperial" else ("K" if units == "standard" else "°C")
        weather = dict(getattr(self, "_robot_weather_state", {}) or {})
        weather_stale = bool(weather.get("stale")) or (bool(weather.get("updated_at")) and time.time() - float(weather["updated_at"]) > 70 * 60)
        show_weather_city = bool(self.config.get("ui.robot_info_temperature_show_city", True))
        self.robot.set_info_state(
            title=str(media.get("title", "") or ""),
            artist=str(media.get("artist", "") or ""),
            playing=bool(media.get("playing", False)),
            paused=bool(media.get("paused", False)),
            levels=list(media.get("levels") or []),
            audio_levels_real=bool(media.get("audio_levels_real", False)),
            audio_levels_kind=str(media.get("audio_levels_kind", "") or ""),
            temperature=weather.get("temperature"),
            temperature_city=str(weather.get("city", "") or "") if show_weather_city else "",
            temperature_unit=unit_label,
            temperature_icon=str(weather.get("icon", "") or ""),
            temperature_stale=weather_stale,
        )
        if bool(self.config.get("ui.robot_info_temperature_enabled", False)):
            self.robot.setToolTip(dt("weather_stale", self._language_code()) if weather_stale else "")
        # RobotWidget interpolates the visual motion itself. Keep source/IPC polling
        # deliberately slower so long music sessions cannot create thousands of
        # unnecessary local-player socket calls per minute. Eight polls/second are
        # enough to refresh levels while the animation stays smooth independently.
        target_interval = 125 if bool(media.get("playing", False)) else 750
        if self._robot_info_timer.interval() != target_interval:
            self._robot_info_timer.setInterval(target_interval)

    def _refresh_robot_temperature_async(self) -> None:
        if self._quitting or bool(getattr(self, "_robot_weather_inflight", False)):
            return
        if not bool(self.config.get("ui.robot_info_temperature_enabled", False)):
            return
        if not bool(self.config.get("integrations.openweather.enabled", False)):
            self._robot_weather_state = {}
            self._refresh_robot_stage_info()
            return
        city = str(self.config.get("integrations.openweather.city", "") or "").strip()
        encrypted = str(self.config.get("integrations.openweather.api_key_encrypted", "") or "").strip()
        if not city or not encrypted:
            self._robot_weather_state = {}
            self._refresh_robot_stage_info()
            return
        self._robot_weather_inflight = True
        signature = self._weather_settings_signature()
        self._robot_weather_request_signature = signature
        worker = FunctionWorker(lambda: OpenWeatherAdapter(self.config).current())
        worker.signals.result.connect(lambda payload, key=signature: self._robot_temperature_ready(payload) if key == self._weather_settings_signature() else None)
        worker.signals.error.connect(self._robot_temperature_error)
        worker.signals.finished.connect(lambda key=signature: self._weather_request_finished(key))
        self.thread_pool.start(worker)

    def _weather_settings_signature(self):
        return json.dumps({"weather": self.config.get("integrations.openweather", {}), "language": self._language_code()}, sort_keys=True)

    def _weather_request_finished(self, signature):
        self._robot_weather_inflight = False
        if signature != self._weather_settings_signature():
            QTimer.singleShot(0, self._refresh_robot_temperature_async)

    @staticmethod
    def _weather_emoji(payload: dict[str, Any]) -> str:
        desc = str(payload.get("description", "") or "").casefold()
        rain = float(payload.get("rain_1h_mm", 0.0) or 0.0)
        snow = float(payload.get("snow_1h_mm", 0.0) or 0.0)
        clouds = float(payload.get("clouds_percent", 0.0) or 0.0)
        if "thunder" in desc or "гроза" in desc: return "⛈️"
        if snow > 0 or any(x in desc for x in ("snow", "снег")): return "❄️"
        if rain > 0 or any(x in desc for x in ("rain", "drizzle", "дожд", "морось")): return "🌧️"
        if any(x in desc for x in ("fog", "mist", "haze", "туман", "дымк")): return "🌫️"
        if any(x in desc for x in ("clear", "ясно")) and clouds < 20: return "☀️"
        if clouds >= 80 or any(x in desc for x in ("overcast", "пасмур")): return "☁️"
        if clouds >= 25 or "cloud" in desc or "облач" in desc: return "⛅"
        return "🌤️"

    def _robot_temperature_ready(self, payload: Any) -> None:
        if isinstance(payload, dict):
            self._robot_weather_state = {
                "temperature": payload.get("temperature"),
                "city": str(payload.get("city", "") or ""),
                "icon": self._weather_emoji(payload),
                "updated_at": time.time(),
                "stale": False,
            }
        else:
            self._robot_weather_state = {}
        self._refresh_robot_stage_info()

    def _robot_temperature_error(self, trace: str) -> None:
        # Weather is optional stage decoration: keep the previous value and avoid
        # interrupting the user with modal UI when the network/API is unavailable.
        logger.debug("Robot temperature refresh failed: %s", str(trace or "").splitlines()[-1:] or "unknown")
        self._robot_weather_state["stale"] = True
        self._refresh_robot_stage_info()

    def _overlay_media_action(self, action: str, target: str = "music") -> None:
        action = str(action or "").casefold().strip()
        target = str(target or "music").casefold().strip()
        if action not in {"pause", "resume", "next", "previous", "like", "dislike"}:
            return
        if target not in {"music", "auto", "yandex_music", "discord", "video"}:
            target = "music"
        self._dispatch_quick_media_command(QuickMediaCommand(action=action, target=target, heard=f"панель:{action}"))

    def _poll_overlay_events(self) -> None:
        events = drain_overlay_events()
        if events:
            # State is persisted atomically, so coalescing any number of model
            # events into one reload is both faster and deterministic.
            self.overlay_manager.reload()

    def _settings_updated(self) -> None:
        self.config.reload()
        discord_adapter = self._discord_adapter()
        if discord_adapter is not None:
            try:
                discord_adapter.refresh_snapshot_profiles()
            except Exception:
                logger.debug("Discord snapshot profile refresh failed", exc_info=True)
        if not (bool(self.config.get("integrations.discord.enabled", True)) and bool(self.config.get("integrations.discord.background_bot_enabled", True))):
            # Discord switched off: disconnect running bots off the UI thread.
            from jarvis.integrations.background.discord_bot import stop_disabled_discord_bots
            threading.Thread(target=stop_disabled_discord_bots, name="neo-discord-disable", daemon=True).start()
        # Invalidate queued wake starts/results before another event-loop turn.
        # Microphone preferences do not cancel typed responses or their TTS.
        if not bool(self.config.get("voice.enabled", True)) or not bool(self.config.get("wake_word.enabled", False)):
            self._pause_wake_listening()
        if not bool(self.config.get("voice.enabled", True)):
            self._continuous_voice_session = False
        from jarvis.ui.qt_localization import install_ui_language
        install_ui_language(QApplication.instance(), self._language_code())
        if self._weather_settings_signature() != getattr(self, "_robot_weather_request_signature", None):
            self._robot_weather_state = {}
            QTimer.singleShot(0, self._refresh_robot_temperature_async)
        speech_changed = self._speech_settings_signature() != getattr(self, "_audio_settings_signature", "")
        # HOTFIX116: fresh-install Settings used to update config.json while the
        # already-created Agent kept pre-save routing/API state until the next
        # process launch. Refresh only configuration-bound runtime layers in place.
        try:
            self.agent.refresh_runtime_settings()
        except Exception:
            logger.exception("Could not refresh Agent runtime after settings save")
        animation_fps = int(self.config.get("ui.animation_fps", 60) or 60)
        if animation_fps not in {30, 60}:
            animation_fps = 60
        self._stream_flush_timer.setInterval({30: 33, 60: 16}[animation_fps])
        if speech_changed:
            self.recognizer = SpeechRecognizer(self.config, self.transcription_service, purpose="command", initial_prompt=command_transcription_prompt(language_code(self.config)), hotwords=_command_hotwords(self.config))
            self.partial_recognizer = SpeechRecognizer(self.config, self.partial_transcription_service, model_name=str(self.config.get("voice.partial_stt_model", "tiny")), purpose="partial")
            self.wake_recognizer = SpeechRecognizer(self.config, self.wake_transcription_service, model_name=str(self.config.get("wake_word.stt_model", "tiny")), purpose="wake", initial_prompt="", hotwords="")
            self.fast_media_recognizer = self.wake_recognizer
            self.audio_capture.restart_backend()
            # HIST-021: the restart emits no terminal event, so the capture owner
            # of the old STT generation is ended here, once, and listening re-arms.
            self._end_capture_owner188()
            self._audio_settings_signature = self._speech_settings_signature()
            self._start_stt_switch186()
        elif getattr(self, "_stt_switch_failed188", None) is not None:
            self._start_stt_switch186()          # HIST-022: Apply again = a conscious retry of the failed model
        if getattr(self, "desktop_services", None) is not None:
            self.desktop_services.reconfigure()
        desired_width = int(self.config.get("ui.window_width", self.width()))
        desired_height = int(self.config.get("ui.window_height", self.height()))
        self.resize(desired_width, desired_height)
        self._apply_chat_height_setting()
        self.header.setCursor(
            Qt.OpenHandCursor if bool(self.config.get("ui.drag_enabled", True)) else Qt.ArrowCursor
        )
        # Never let a drag cursor be inherited from the full central surface.
        self.surface.setCursor(Qt.ArrowCursor)
        self._apply_settings()
        self._apply_language()
        self.activity_tray_action.setChecked(bool(self.config.get("activity.enabled", True)))
        if self.bookshelf:
            self.bookshelf.apply_settings()
        self._register_app_mic_hotkey()
        self._register_aux_hotkeys()
        self._configure_mail_monitor_timer()
        try:
            from jarvis.runtime.autoclicker import autoclicker_controller
            autoclicker_controller(self.config).reload_settings()
        except Exception:
            logger.debug("Could not reload autoclicker settings", exc_info=True)
        try:
            self._timer_manager = timer_manager(self.config.paths.root, self._language_code())
        except Exception:
            logger.exception("Could not reconfigure timer manager")
        self._refresh_timer_stage_label()
        self.overlay_manager.reload()
        # HOTFIX56: Telegram Remote UI is configured from Settings and should react
        # immediately to Apply/Save without requiring a NEO restart.
        telegram_remote = getattr(self, "telegram_remote", None)
        if telegram_remote is not None:
            try:
                # HOTFIX98: Telegram Business/Secretary messages use the same
                # Bot API long-poll transport as Remote Control.  Saving settings
                # must not stop that transport when Remote Control is off but
                # Secretary Mode is still enabled.
                telegram_transport_needed = bool(
                    self.config.get("integrations.telegram.remote_control_enabled", False)
                    or self.config.get("integrations.telegram.messages_enabled", False)
                    or self.config.get("integrations.telegram.enabled", False)
                )
                if telegram_transport_needed:
                    telegram_remote.start()
                else:
                    telegram_remote.stop()
            except Exception:
                logger.exception("Could not reconfigure Telegram Remote after settings save")
            try:
                remote_agent = getattr(telegram_remote, "agent_or_none", None)
                if remote_agent is not None and remote_agent is not self.agent:
                    remote_agent.refresh_runtime_settings()
            except Exception:
                logger.exception("Could not refresh Telegram Agent runtime after settings save")
        # Discord Gateway configuration is intentionally warmed again after Save.
        # This is the missing first-session step that previously made a newly
        # entered token appear to work only after restarting NEO.
        self._discord_cold_attach_done = False
        QTimer.singleShot(120, self._prewarm_discord_async)
        if self._app_mic_muted:
            self._set_status("Микрофон NEO выключен")
        else:
            self._set_status("Настройки сохранены")

    def _reset_conversation(self) -> None:
        self._cancel_morning_briefing158()
        self._typing_timer.stop()
        self._heard_timer.stop()
        self._live_user_bubble = None
        self._pending_response_bubble = None
        self._stream_thinking_text = ""
        self.agent.reset()
        self._continuous_voice_session = False
        self.chat.clear_messages()
        self._model_progress_bubble = None
        self._model_progress_bubbles184 = {}
        self._model_progress_operation184 = None
        self._append_assistant("Новый диалог начат.")
        self.robot.set_state("happy")
        QTimer.singleShot(1500, self._return_robot_to_idle)

    def _preload_stt_async(self) -> None:
        if getattr(self, "_stt_switch_key186", None) is None and getattr(self, "recognizer", None) is not None:
            try:
                self._stt_switch_key186 = (str(self.recognizer.model_name),) + tuple(self.recognizer._device_policy())
            except Exception:
                self._stt_switch_key186 = (str(getattr(self.recognizer, "model_name", "")),)
        if self._quitting or not bool(self.config.get("voice.enabled", True)):
            return
        # HOTFIX43.1: do not run long native-model preloads inside a QRunnable.
        # Previous builds already documented native Windows exits around short-lived
        # Qt worker lifetimes. A single daemon Python thread owns startup preloading;
        # failures remain isolated in the STT/wake subprocesses and cannot take the
        # GUI thread down with them.
        thread = self._stt_preload_thread
        if isinstance(thread, threading.Thread) and thread.is_alive():
            return
        def _run() -> None:
            try:
                self._preload_stt_models()
            except BaseException:
                logger.debug("STT preload failed", exc_info=True)
        thread = threading.Thread(target=_run, name="NeoJarvis-STT-Preload", daemon=True)
        self._stt_preload_thread = thread
        thread.start()

    def _start_stt_switch186(self) -> None:
        """186 (USER185-04): one transactional switch of the command STT model.

        The new model is loaded in the background with a load-sized deadline
        (a first large-v3-turbo load must not be killed by the short command
        timeout), the state is shown as loading → ready / error, and only the
        latest switch may report. A failed load leaves the UI, the microphone and
        routing working and says so instead of failing every later command."""
        if self._quitting or not bool(self.config.get("voice.enabled", True)):
            return
        recognizer = self.recognizer
        model = str(getattr(recognizer, "model_name", "") or "")
        try:
            key = (model,) + tuple(recognizer._device_policy())
        except Exception:
            key = (model,)
        previous = getattr(self, "_stt_switch_key186", None)
        self._stt_switch_key186 = key
        if previous is None or previous == key:
            return            # only a real change of the command model/device is a switch
        self._stt_switch_generation186 = int(getattr(self, "_stt_switch_generation186", 0)) + 1
        generation = self._stt_switch_generation186
        # HIST-022: one attempt per user Apply; a failed key can be retried consciously.
        self._stt_switch_failed188 = None
        self._stt_switch_attempt188 = (generation, key, previous)
        self._set_status(tr("Загружаю модель распознавания речи {model}…", self._language_code()).format(model=model))

        def run() -> None:
            try:
                recognizer.service.preload(
                    model_name=recognizer.model_name,
                    device=recognizer._device_policy()[0],
                    compute_type=recognizer._device_policy()[1],
                    cpu_threads=recognizer._cpu_threads(),
                    timeout_seconds=max(300.0, float(self.config.get("voice.transcription_timeout_seconds", 60) or 60)),
                )
                self.stt_switch_signal186.emit(generation, "ready", model, "")
            except BaseException as exc:  # noqa: BLE001 - reported to the user
                from jarvis.runtime.diagnostics import safe_error
                logger.warning("STT model switch to %s failed", model, exc_info=True)
                try:
                    self.stt_switch_signal186.emit(generation, "error", model, safe_error(exc)[:240])
                except RuntimeError:
                    pass
        threading.Thread(target=run, name="NeoJarvis-STT-Switch", daemon=True).start()

    def _stt_switch_finished186(self, generation: int, state: str, model: str, detail: str) -> None:
        if self._quitting or generation != int(getattr(self, "_stt_switch_generation186", 0)):
            return            # a later switch owns the status
        code = self._language_code()
        attempt = getattr(self, "_stt_switch_attempt188", None)
        if state == "ready":
            self._stt_switch_failed188 = None
            self._set_status(tr("Модель распознавания речи {model} готова", code).format(model=model))
        else:
            if attempt is not None and attempt[0] == generation:
                # HIST-022: the failed key is not remembered as active, so pressing
                # Apply again with the same choice retries it (no automatic loop).
                self._stt_switch_key186 = attempt[2]
                self._stt_switch_failed188 = attempt[1]
            text = tr("Не удалось загрузить модель распознавания речи {model}. Текстовые команды работают; выберите другую модель или повторите попытку.", code).format(model=model)
            self._set_status(text)
            if detail:
                logger.warning("STT model %s: %s", model, detail)

    def _preload_stt_models(self) -> None:
        # Build the selected command lexicon on this existing background worker,
        # before the first voice result could request it on the Qt thread.
        try:
            from jarvis.runtime.command_repair161 import catalog
            catalog(language_code(self.config))
        except Exception:
            logger.debug("Command lexicon preload failed", exc_info=True)
        loaded: set[tuple[int, str]] = set()
        recognizers = []
        if bool(self.config.get("wake_word.enabled", False)):
            # Acoustic openWakeWord remains available as an opt-in diagnostic path,
            # but HOTFIX43.1 disables it by default because field logs showed a
            # constant 0.000 score while native-process load correlated with crashes.
            if (not self._acoustic_wake_disabled and bool(self.config.get("wake_word.acoustic_enabled", False))):
                try:
                    self._hybrid_wake_detector.preload()
                except Exception as exc:
                    logger.debug("Acoustic wake preload failed: %s", exc)
            recognizers.append(self.wake_recognizer)
        if int(self.config.get("voice.live_partial_max_updates", 0)) > 0:
            recognizers.append(self.partial_recognizer)
        # fast_media_recognizer aliases wake_recognizer in HOTFIX43.1, so no extra
        # tiny model/process is loaded here.
        recognizers.append(self.recognizer)
        for recognizer in recognizers:
            key = (id(recognizer.service), recognizer.model_name)
            if key in loaded:
                continue
            recognizer.preload()
            loaded.add(key)

    def _check_ollama_async(self) -> None:
        if self._quitting or getattr(self, "_ollama_check_inflight", False):
            return
        self._ollama_check_inflight = True
        worker = FunctionWorker(self._health_and_models)
        worker.signals.result.connect(self._ollama_checked)
        worker.signals.error.connect(self._handle_error)
        worker.signals.finished.connect(lambda: setattr(self, "_ollama_check_inflight", False))
        self.thread_pool.start(worker)

    def _health_and_models(self) -> tuple[bool, str, list[str]]:
        from jarvis.ollama_status import readiness
        result = readiness(self.agent.ollama.base_url, retries=1)
        return bool(result["ready"]), str(result["message"]), list(result["models"])

    def _ollama_checked(self, result: tuple[bool, str, list[str]]) -> None:
        ok, message, models = result
        self._set_status(message)
        if not ok:
            self.robot.set_state("error")
            return
        chat_model = str(self.config.get("local_chat.model", "") or self.config.get("ollama.chat_model", "qwen2.5:0.5b") or "qwen2.5:0.5b")
        vision_model = str(self.config.get("ollama.vision_model", chat_model) or chat_model)
        selected_models = list(dict.fromkeys(
            ([chat_model] if self.config.get("local_chat.enabled", True) else [])
            + ([vision_model] if self.config.get("ollama.vision_enabled", True) else [])))
        missing = [model for model in selected_models
                   if model not in models and not (":" not in model and model + ":latest" in models)]
        if not selected_models:
            self._set_status("Ollama доступна. Локальные модели отключены в настройках.")
            self._return_robot_to_idle()
            return
        if missing:
            model_text = ", ".join(missing)
            action = "Установите именно указанные модели в Ollama."
            if chat_model in missing and self.config.get("local_chat.enabled", True):
                action += " Для разговорной модели доступна кнопка «Скачать» в настройках."
            self._set_status(f"Ollama доступна, но не установлено: {model_text}. " + action)
            self.robot.set_state("error")
            return
        self._set_status("Ollama готова. " + ", ".join(selected_models))
        self.robot.set_state("success")
        QTimer.singleShot(1300, self._return_robot_to_idle)
        if bool(self.config.get("ollama.preload_model", True)):
            self._warm_ollama_async()

    def _warm_ollama_async(self) -> None:
        if self._quitting or self._ollama_warmup_inflight:
            return
        # Do not make the tiny wake recognizer and the 4B model fight for CPU/RAM
        # during the first seconds of startup. STT wins; Ollama still preloads as
        # soon as that initialization is finished.
        thread = self._stt_preload_thread
        if thread is not None and thread.is_alive() and self._ollama_warmup_defer_count < 40:
            self._ollama_warmup_defer_count += 1
            QTimer.singleShot(125, self._warm_ollama_async)
            return
        self._ollama_warmup_defer_count = 0
        self._ollama_warmup_inflight = True
        if not self._busy and not self.audio_capture.is_recording:
            self._set_status("Подготавливаю модель для быстрого ответа…")
        worker = FunctionWorker(self.agent.ollama.warmup)
        worker.signals.result.connect(self._ollama_warmed)
        worker.signals.error.connect(self._ollama_warmup_failed)
        worker.signals.finished.connect(self._ollama_warmup_finished)
        self.thread_pool.start(worker)

    def _ollama_warmup_failed(self, trace: str) -> None:
        logger.debug("Ollama warmup failed: %s", trace)

    def _ollama_warmup_finished(self) -> None:
        self._ollama_warmup_inflight = False

    def _ollama_warmed(self, message: object) -> None:
        if self._quitting or self._busy or self.audio_capture.is_recording:
            return
        self._set_status(str(message))
        QTimer.singleShot(1400, lambda: self._set_status("Ожидание") if not self._busy else None)

    @staticmethod
    def _resolve_model_name(current: str, models: list[str]) -> str | None:
        if current in models:
            return current
        current_base = current.split(":")[0].lower()
        for model in models:
            if model.split(":")[0].lower() == current_base:
                # Keep the exact configured base name. Ollama resolves the default
                # tag internally; keep the configured name unchanged.
                return current if ":" not in current else model
        return None

    def _restart_idle_timer(self) -> None:
        self._sleeping = False
        self._idle_action_generation += 1
        sleep_seconds = max(20, int(self.config.get("ui.sleep_after_seconds", 20)))
        self._idle_timer.start(sleep_seconds * 1000)
        self._schedule_idle_action(self._idle_action_generation)

    def _schedule_idle_action(self, generation: int) -> None:
        if not bool(self.config.get("ui.idle_animations", True)):
            return
        # Long calm scenes are deliberately spaced out. RobotWidget animates the
        # scene continuously, so there is no 2-second state flicker anymore.
        delay = random.randint(9000, 18000)
        self._idle_action_timer.start(delay)

    def _play_idle_action(self) -> None:
        if self._busy or self._voice_mode == "command" or self._sleeping or self._quitting:
            return
        duration = random.uniform(15.0, 30.0)
        self.robot.set_state("idle")
        self.robot.start_idle_scene(duration=duration)
        generation = self._idle_action_generation
        QTimer.singleShot(int(duration * 1000), lambda g=generation: self._finish_idle_action(g))

    def _finish_idle_action(self, generation: int) -> None:
        if generation != self._idle_action_generation or self._busy or self._sleeping:
            return
        self.robot.finish_idle_scene()
        self.robot.set_state("idle")
        self._schedule_idle_action(generation)

    def _go_sleepy(self) -> None:
        foreground_recording = self._voice_mode == "command" and self.audio_capture.is_recording
        if not self._busy and not foreground_recording and not self._speech_timer.isActive() and not self._typing_timer.isActive():
            self._sleeping = True
            self._idle_action_timer.stop()
            self.robot.set_state("sleeping")
            self._set_status(_SLEEP_STATUS182)

    def _sleep_status182(self) -> str:
        """NEO 182: the sleep status follows the real listener state and the UI language."""
        code = self._language_code()
        index = ("ru", "en", "de", "fr", "es", "it", "pt").index(code) if code in ("ru", "en", "de", "fr", "es", "it", "pt") else 1
        phrase = str(self.config.get("wake_word.phrase", "") or "").strip()
        listening = (bool(self.config.get("voice.enabled", True)) and bool(self.config.get("wake_word.enabled", False))
                     and not getattr(self, "_app_mic_muted", False) and bool(phrase))
        if listening:
            return ("Сплю, но услышу «{p}»", "Sleeping — say «{p}» to wake me", "Ich schlafe — sag «{p}», um mich zu wecken",
                    "Je dors — dites «{p}» pour me réveiller", "Duermo — di «{p}» para despertarme",
                    "Dormo — di' «{p}» per svegliarmi", "Estou a dormir — diz «{p}» para me acordar")[index].format(p=phrase)
        if bool(self.config.get("voice.enabled", True)) and not getattr(self, "_app_mic_muted", False):
            return ("Сплю. Нажмите микрофон или напишите сообщение", "Sleeping. Press the microphone or type a message",
                    "Ich schlafe. Drück das Mikrofon oder schreib eine Nachricht", "Je dors. Appuyez sur le micro ou écrivez un message",
                    "Duermo. Pulsa el micrófono o escribe un mensaje", "Dormo. Premi il microfono o scrivi un messaggio",
                    "Estou a dormir. Carrega no microfone ou escreve uma mensagem")[index]
        return ("Сплю. Микрофон выключен — напишите сообщение", "Sleeping. Microphone is off — type a message",
                "Ich schlafe. Mikrofon ist aus — schreib eine Nachricht", "Je dors. Micro coupé — écrivez un message",
                "Duermo. Micrófono apagado — escribe un mensaje", "Dormo. Microfono spento — scrivi un messaggio",
                "Estou a dormir. Microfone desligado — escreve uma mensagem")[index]

    @staticmethod
    def _terminate_jarvis_helper_children(*, grace_seconds: float = 0.7) -> None:
        """Terminate only helper subprocesses owned by this NEO process.

        This is a last-resort complement to each service's graceful ``close()``.
        It deliberately does *not* kill arbitrary applications launched by the
        user through NEO and does not touch a separately running Ollama.
        """
        try:
            import psutil
        except Exception:
            return

        helper_modules = (
            "jarvis.wake_worker",
            "jarvis.audio_worker",
            "jarvis.stt_worker",
            "jarvis.ambient_loopback_worker",
            "jarvis.integrations.background.local_player_worker",
        )
        selected = []
        try:
            children = psutil.Process(os.getpid()).children(recursive=True)
        except Exception:
            return
        for proc in children:
            try:
                name = str(proc.name() or "").casefold()
                cmd = " ".join(str(part) for part in (proc.cmdline() or [])).casefold()
            except Exception:
                continue
            is_python_helper = any(module in cmd for module in helper_modules)
            is_media_helper = name.startswith("ffmpeg") or name in {"deno.exe", "deno"}
            if is_python_helper or is_media_helper:
                selected.append(proc)

        if not selected:
            return
        for proc in selected:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            _, alive = psutil.wait_procs(selected, timeout=max(0.1, float(grace_seconds)))
        except Exception:
            alive = selected
        for proc in alive:
            try:
                proc.kill()
            except Exception:
                pass
        if alive:
            try:
                psutil.wait_procs(alive, timeout=0.5)
            except Exception:
                pass
        try:
            logger.info("Full shutdown helper cleanup: targeted=%d forced=%d", len(selected), len(alive))
        except Exception:
            pass

    def _arm_shutdown_watchdog(self, timeout_seconds: float = 10.0) -> None:
        """Force only the NEO parent process down if graceful shutdown wedges.

        A non-daemon Python watchdog is deliberate: if Qt exits normally, app.py
        sets the event and the thread returns immediately. If an active worker or
        native library prevents interpreter teardown, the watchdog calls os._exit
        after the bounded grace period so no ghost NEO process remains.
        Child media/Discord workers are explicitly closed before QApplication.quit.
        """
        if self._shutdown_watchdog_started:
            return
        self._shutdown_watchdog_started = True
        event = self._shutdown_watchdog_event

        def watchdog() -> None:
            if event.wait(max(3.0, float(timeout_seconds))):
                return
            try:
                logger.error("Full shutdown watchdog forced process exit after timeout")
            except Exception:
                pass
            try:
                self._terminate_jarvis_helper_children(grace_seconds=0.2)
            except Exception:
                pass
            os._exit(0)

        threading.Thread(target=watchdog, name="NeoJarvis-FullShutdownWatchdog", daemon=False).start()

    def mark_shutdown_complete(self) -> None:
        if bool(getattr(self, "_shutdown_incomplete", False)):
            logger.error("Shutdown remains incomplete; full-shutdown watchdog stays armed")
            return
        self._shutdown_watchdog_event.set()

    def shutdown_resources(self) -> None:
        if getattr(self, "bookshelf", None) is not None:
            self.bookshelf.shutdown()
        if getattr(self, "_mini_browser", None) is not None:
            self._mini_browser.close()
        for viewer in list(getattr(self, "_image_viewers", [])):
            viewer.close()
        if self._quitting:
            return
        self._quitting = True
        # HIST-005: partial-transcript state of the closing window is invalidated at once.
        self._partial_generation = int(getattr(self, "_partial_generation", 0) or 0) + 1
        self._pending_partial_recording = None
        heard_timer = getattr(self, "_heard_timer", None)
        if heard_timer is not None:
            heard_timer.stop()
        response_voice = getattr(self, "_response_voice_service", None)
        if response_voice is not None:
            response_voice.close()
        response_voice_timer = getattr(self, "_response_voice_timer", None)
        if response_voice_timer is not None:
            response_voice_timer.stop()
        from jarvis.runtime.scenarios import unsubscribe_scenarios
        timer = getattr(self, "_scenario_status_timer184", None)
        if timer is not None:
            timer.stop()
        self._pending_scenario_status184 = None
        callback = getattr(self, "_scenario_callback184", None)
        if callback is not None:
            unsubscribe_scenarios(callback)
            self._scenario_callback184 = None
        self._cancel_morning_briefing158()
        if getattr(self, "_media_queue_reader158", None) is not None:
            self._media_queue_reader158.close()
        cancellation_manager().cancel("NEO завершает работу")
        try:
            self.thread_pool.clear()
            self._quick_media_pool.clear()
        except Exception:
            pass
        self._unregister_app_mic_hotkey()
        self._unregister_aux_hotkeys()
        try:
            from jarvis.runtime.autoclicker import autoclicker_controller
            autoclicker_controller(self.config).stop()
        except Exception:
            pass
        clipboard_history().stop_monitor()
        self._restore_listening_audio()
        self._pause_wake_listening()
        self._typing_timer.stop()
        self._heard_timer.stop()
        self._speech_timer.stop()
        self._idle_timer.stop()
        self._idle_action_timer.stop()
        self._wake_watchdog.stop()
        self._wake_result_timer.stop()
        self._overlay_timer.stop()
        if hasattr(self, "_media_event_timer"):
            self._media_event_timer.stop()
        if hasattr(self, "_ambient_typing_timer"):
            self._ambient_typing_timer.stop()
        if hasattr(self, "_discord_observer_timer"):
            self._discord_observer_timer.stop()
        try:
            discord_session_meter().close()
        except Exception:
            pass
        if hasattr(self, "_robot_info_timer"):
            self._robot_info_timer.stop()
        if hasattr(self, "_robot_weather_timer"):
            self._robot_weather_timer.stop()
        if hasattr(self, "_timer_event_timer"):
            self._timer_event_timer.stop()
        self.overlay_manager.close_all()
        if self.audio_capture.is_recording:
            self.audio_capture.stop(emit_result=False)
        self.audio_capture.close()
        self._cancel_command_asr157()
        self._hybrid_wake_detector.close()
        self.transcription_service.close()
        self.wake_transcription_service.close()

        # Close the lightweight game web HTTP session. There is no game database
        # migrator/synchronizer in the runtime.
        try:
            game_web = getattr(self.agent, "game_web", None)
            if game_web is not None and hasattr(game_web, "close"):
                game_web.close()
        except Exception:
            logger.debug("Game web shutdown failed", exc_info=True)

        # HOTFIX77: terminate helper processes immediately instead of relying on
        # Python atexit/daemon-thread semantics. This closes the hidden local Qt
        # player, Discord Gateway/voice sessions and their FFmpeg children.
        try:
            local_player().close()
        except Exception:
            logger.debug("Local background player shutdown failed", exc_info=True)
        discord_adapter = self._discord_adapter()
        if discord_adapter is not None:
            # Close publisher admission before bot shutdown; join only after
            # bot close has released any synchronous publication waiting on it.
            try:
                discord_adapter.close_snapshot_publication(timeout=0.0)
            except Exception:
                self._shutdown_incomplete = True
                logger.debug("Discord snapshot admission shutdown failed", exc_info=True)
        try:
            if not shutdown_all_discord_bots(timeout_per_bot=2.8):
                self._shutdown_incomplete = True
        except Exception:
            self._shutdown_incomplete = True
            logger.debug("Discord background shutdown failed", exc_info=True)
        if discord_adapter is not None:
            try:
                if not discord_adapter.close_snapshot_publication(timeout=0.5):
                    self._shutdown_incomplete = True
            except Exception:
                self._shutdown_incomplete = True
                logger.debug("Discord snapshot publisher shutdown failed", exc_info=True)
        try:
            self._terminate_jarvis_helper_children(grace_seconds=0.7)
        except Exception:
            logger.debug("Residual NEO helper cleanup failed", exc_info=True)
        try:
            self.thread_pool.waitForDone(900)
            self._quick_media_pool.waitForDone(900)
        except Exception:
            pass
        self.tray.hide()

    def _quit_application(self) -> None:
        self._full_shutdown_requested = True
        self._arm_shutdown_watchdog()
        self._save_geometry()
        self.shutdown_resources()
        QApplication.quit()

    def _hide_to_tray(self) -> None:
        self.hide()
        if self.tray.isVisible() and not self._tray_notice_shown:
            self.tray.showMessage(
                tr("NEO продолжает работать", self._language_code()),
                tr("Окно скрыто. Откройте его значком в области уведомлений или завершите через меню значка.", self._language_code()),
                QSystemTrayIcon.Information,
                3500,
            )
            self._tray_notice_shown = True

    def _ask_close_action(self) -> str:
        box = QMessageBox(self)
        # 186: the temporary modal is released on every path, including an
        # exception before or inside exec(); garbage collection is not relied on.
        try:
            box.setIcon(QMessageBox.Question)
            code = self._language_code()
            box.setWindowTitle(tr("Закрытие NEO", code))
            box.setText(tr("Что сделать с NEO?", code))
            box.setInformativeText(tr("Полное завершение остановит NEO и его фоновые медиапроцессы. Сворачивание оставит ассистента работающим в трее.", code))
            quit_button = box.addButton(tr("Полностью выключить", code), QMessageBox.DestructiveRole)
            tray_button = box.addButton(tr("Свернуть в трей", code), QMessageBox.AcceptRole)
            cancel_button = box.addButton(tr("Отмена", code), QMessageBox.RejectRole)
            box.setDefaultButton(tray_button)
            box.setEscapeButton(cancel_button)
            box.exec()
            clicked = box.clickedButton()
            if clicked is quit_button:
                return "quit"
            if clicked is tray_button:
                return "tray"
            return "cancel"
        finally:
            box.hide()
            box.deleteLater()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._quitting:
            self._hybrid_wake_detector.close()
            self.transcription_service.close()
            self.wake_transcription_service.close()
            event.accept()
            return
        action = self._ask_close_action()
        event.ignore()
        if action == "quit":
            QTimer.singleShot(0, self._quit_application)
        elif action == "tray":
            self._hide_to_tray()
