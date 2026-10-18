# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QPainter, QPalette, QTextDocument
from PySide6.QtGui import Qt as QtGuiQt
from shiboken6 import isValid
from jarvis.i18n import tr

import re as _re

_LONG_TOKEN183 = _re.compile(r"\S{24,}")
_ZWSP = "\u200b"
_HTML_UNIT = _re.compile(r"&#?\w+;|.", _re.S)


import unicodedata as _unicodedata


def _joins_previous(ch: str, prev: str) -> bool:
    """True when ``ch`` continues the user-perceived character started before it."""
    code = ord(ch)
    if _unicodedata.category(ch) in {"Mn", "Me", "Mc"}:
        return True                                   # combining accents
    if code in (0x200D, 0xFE0E, 0xFE0F) or 0x1F3FB <= code <= 0x1F3FF or 0xE0020 <= code <= 0xE007F:
        return True                                   # joiner, variation selector, skin tone, tag
    if prev and ord(prev) == 0x200D:
        return True                                   # the character after a zero-width joiner
    return False


def _clusters(token: str) -> list[str]:
    """UI185-CHAT-GRAPHEME-DISPLAY-01: grapheme-like clusters — a flag (two regional
    indicators), a ZWJ family emoji or a letter with combining accents stays whole."""
    out: list[str] = []
    for ch in token:
        code = ord(ch)
        if out:
            last = out[-1]
            regional = 0x1F1E6 <= code <= 0x1F1FF
            if _joins_previous(ch, last[-1]) or (regional and len(last) == 1 and 0x1F1E6 <= ord(last) <= 0x1F1FF):
                out[-1] = last + ch
                continue
        out.append(ch)
    return out


def _break_token(token: str, rich: bool = False) -> str:
    # NEO 184: a break opportunity between characters so a long word wraps at the real width of
    # the bubble. 186: only between whole clusters, and HTML entities are units of RICH text only
    # (UI185-CHAT-PLAIN-ENTITY-WRAP-01: «&WWW…;» in plain text is ordinary letters).
    units = _HTML_UNIT.findall(token) if rich else _clusters(token)
    return _ZWSP.join(units)


def _hinted_plain186(text: str) -> tuple[str, frozenset[int]]:
    """Plain display text plus the code-point positions of the inserted break hints."""
    value = str(text or "")
    if len(value) < 24:
        return value, frozenset()
    pieces: list[str] = []
    hints: set[int] = set()
    length = 0
    cursor = 0
    for match in _LONG_TOKEN183.finditer(value):
        head = value[cursor:match.start()]
        pieces.append(head); length += len(head)
        for index, unit in enumerate(_clusters(match.group(0))):
            if index:
                pieces.append(_ZWSP); hints.add(length); length += 1
            pieces.append(unit); length += len(unit)
        cursor = match.end()
    tail = value[cursor:]
    pieces.append(tail)
    return "".join(pieces), frozenset(hints)


def _breakable183(text: str, rich: bool = False) -> str:
    """Very long words (URLs, ids, «WWWW…») get invisible break points, so they wrap inside the
    bubble instead of being cut at its right edge. Tags of rich text are never touched; the
    original text (copy, history, links) stays unchanged — see ``ChatBodyLabel``."""
    value = str(text or "")
    if len(value) < 24:
        return value
    if not rich:
        return _hinted_plain186(value)[0]
    parts = _re.split(r"(<[^>]*>)", value)
    return "".join(part if part.startswith("<") else _LONG_TOKEN183.sub(lambda m: _break_token(m.group(0), True), part) for part in parts)


def _without_breaks(text: str) -> str:
    return str(text or "").replace(_ZWSP, "")

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class ChatBodyLabel(QLabel):
    """NEO 184: a message body whose copies are the original text.

    The visible text carries invisible break points inside long words; selection, Ctrl+C and
    the context menu return the text without them (a copied URL or id is byte for byte the
    one in the message)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hovered_link = ""
        self.linkHovered.connect(lambda link: setattr(self, "_hovered_link", str(link or "")))

    def set_message(self, text: str, *, plain: bool) -> None:
        self._source_text = str(text or "")
        self._source_plain = plain
        rich = (not plain) and QtGuiQt.mightBeRichText(str(text or ""))
        # Format must be set before content, including a reused label. Ordinary
        # chat payloads can contain app names/paths that look like HTML resources.
        text_format = Qt.RichText if rich else Qt.PlainText
        if self.textFormat() != text_format:
            # Changing format can parse the *previous* contents immediately.
            # Remove a plain payload while it is still plain before enabling rich.
            self.clear()
        self.setTextFormat(text_format)
        if rich:
            self._display186, self._hints186 = "", frozenset()
            self.setText(_breakable183(text, rich=True))
        else:
            self._display186, self._hints186 = _hinted_plain186(str(text or ""))
            self.setText(self._display186)

    def selectedText(self) -> str:  # noqa: N802 - Qt API name
        selected = super().selectedText()
        if (selected and getattr(self, "_source_plain", False) and self.selectionStart() == 0
                and len(selected.encode("utf-16-le", errors="surrogatepass")) // 2 == self._selection_length()):
            return self._source_text
        if selected and self.textFormat() == Qt.PlainText and getattr(self, "_display186", ""):
            # UI185-PARTIAL-COPY-ZWSP-01: remove only the hints NEO inserted; a real
            # U+200B the user typed is part of the copied fragment.
            return self._plain_fragment186(self.selectionStart(), selected).replace("\u2029", "\n")
        return _without_breaks(selected).replace("\u2029", "\n")

    def _plain_fragment186(self, start_utf16: int, selected: str) -> str:
        display = self._display186
        hints = self._hints186
        want = len(selected.encode("utf-16-le", errors="surrogatepass")) // 2
        position = 0
        out: list[str] = []
        taken = 0
        for index, ch in enumerate(display):
            width = 2 if ord(ch) > 0xFFFF else 1
            if position >= start_utf16 and taken < want:
                if index not in hints:
                    out.append(ch)
                taken += width
            position += width
            if taken >= want and position > start_utf16:
                break
        if not out and selected:
            return _without_breaks(selected)
        return "".join(out)

    def _copy_selection(self) -> bool:
        selected = self.selectedText()
        if not selected:
            return False
        QGuiApplication.clipboard().setText(selected)
        return True

    def _selection_length(self) -> int:
        value = self.text()
        rich = self.textFormat() == Qt.RichText or (
            self.textFormat() == Qt.AutoText and QtGuiQt.mightBeRichText(value)
        )
        document = QTextDocument()
        if rich:
            document.setHtml(value)
        else:
            document.setPlainText(value)
        # The document handles UTF-16 positions and normalized paragraph breaks.
        return max(0, document.characterCount() - 1)

    def _select_all(self) -> None:
        self.setSelection(0, self._selection_length())

    def _style_context_menu(self, menu: QMenu) -> None:
        theme = {}
        node = self
        while node is not None:
            candidate = getattr(node, "_theme_cache", None)
            if isinstance(candidate, dict) and candidate:
                theme = candidate
                break
            node = node.parentWidget()
        palette = QGuiApplication.palette()
        def color(key, role):
            result = QColor(theme.get(key, palette.color(role)))
            return result if result.isValid() else palette.color(role)
        background = color("dialog_bg", QPalette.Window)
        foreground = color("text_color", QPalette.WindowText)
        accent = color("accent", QPalette.Highlight)
        def luminance(value):
            channels = [component / 12.92 if component <= .04045 else ((component + .055) / 1.055) ** 2.4
                        for component in (value.redF(), value.greenF(), value.blueF())]
            return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2]
        def contrast(a, b):
            x, y = sorted((luminance(a), luminance(b)))
            return (y + .05) / (x + .05)
        def readable(bg):
            return max((QColor("#000000"), QColor("#ffffff")), key=lambda fg: contrast(fg, bg))
        if contrast(foreground, background) < 4.5:
            foreground = readable(background)
        selected = readable(accent)
        disabled = QColor.fromRgbF(
            *[.6 * fg + .4 * bg for fg, bg in zip(
                (foreground.redF(), foreground.greenF(), foreground.blueF()),
                (background.redF(), background.greenF(), background.blueF()))])
        menu.setStyleSheet(
            f"QMenu {{ background:{background.name()}; color:{foreground.name()}; "
            f"border:1px solid {accent.name()}; border-radius:6px; padding:4px; }}"
            "QMenu::item { padding:6px 18px; border-radius:3px; }"
            f"QMenu::item:selected {{ background:{accent.name()}; color:{selected.name()}; }}"
            f"QMenu::item:disabled {{ color:{disabled.name()}; }}"
        )

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt API name
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self._select_all()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Copy) and self._copy_selection():
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt API name
        menu = QMenu(self)
        self._style_context_menu(menu)
        copy = menu.addAction(tr("Копировать", _language_of(self)))
        copy.setEnabled(self.hasSelectedText())
        copy.triggered.connect(self._copy_selection)
        link = self._hovered_link
        if link:
            menu.addAction(tr("Копировать ссылку", _language_of(self))).triggered.connect(
                lambda: QGuiApplication.clipboard().setText(_without_breaks(link)))
        select_all = menu.addAction(tr("Выделить всё", _language_of(self)))
        select_all.triggered.connect(self._select_all)
        try:
            menu.exec(event.globalPos())
            event.accept()
        finally:
            if isValid(menu):
                menu.deleteLater()


def _language_of(widget: QWidget) -> str:
    node = widget
    while node is not None:
        language = getattr(node, "_language", None)
        if isinstance(language, str) and language:
            return language
        node = node.parentWidget()
    return "ru"


@dataclass
class ChatItem:
    kind: str
    text: str
    author: str = ""
    thinking: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))


class MessageBubble(QFrame):
    def __init__(
        self,
        role: str,
        author: str,
        text: str,
        item: ChatItem,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.role = role
        self.author = author
        self.text = text
        self.item = item
        self.setObjectName(f"bubble_{role}")
        self._scale = 1.0
        self._font_size_px = 14
        self._font_family = "Segoe UI"
        self._language = "ru"
        self._available_width = 660
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout = QVBoxLayout(self)
        self._layout = layout
        layout.setContentsMargins(9, 7, 9, 8)
        layout.setSpacing(4)
        self.author_label = QLabel()
        # 186: a configured name is literal data, never a rich-text template.
        self.author_label.setTextFormat(Qt.PlainText)
        self.author_label.setText(author)
        self.author_label.setProperty("i18n_skip_text", True)
        self.author_label.setObjectName("bubbleAuthor")
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.author_label, 1)
        try:
            timestamp = datetime.fromisoformat(item.timestamp)
        except (TypeError, ValueError):
            timestamp = datetime.now().astimezone()
            item.timestamp = timestamp.isoformat()
        self.time_label = QLabel(timestamp.strftime("%H:%M"))  # NEO 177: less noise; exact time in the tooltip
        self.time_label.setProperty("i18n_skip_text", True)
        self.time_label.setObjectName("bubbleTime")
        self.time_label.setToolTip(item.timestamp)
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignTop)
        header.addWidget(self.time_label, 0, Qt.AlignRight | Qt.AlignTop)
        layout.addLayout(header)
        self.thinking_toggle: QPushButton | None = None
        self.thinking_label: QLabel | None = None
        if role == "assistant":
            self.thinking_toggle = QPushButton("▼ Размышление")
            self.thinking_toggle.setObjectName("thinkingToggle")
            self.thinking_toggle.setCheckable(True)
            self.thinking_toggle.setChecked(True)
            self.thinking_toggle.clicked.connect(self._toggle_thinking)
            self.thinking_toggle.hide()
            self.thinking_label = QLabel("")
            self.thinking_label.setProperty("i18n_skip_text", True)
            self.thinking_label.setObjectName("thinkingBody")
            self.thinking_label.setWordWrap(True)
            self.thinking_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.thinking_label.hide()
            layout.addWidget(self.thinking_toggle)
            layout.addWidget(self.thinking_label)
        self.body_label = ChatBodyLabel()
        # Both user and assistant messages contain untrusted literal payloads.
        # Trusted rich labels opt in directly through ChatBodyLabel.set_message.
        self.body_label.set_message(text, plain=True)
        # Chat history contains user text, filenames, source quotations and
        # provider errors. Language Apply changes chrome, never past payloads.
        self.body_label.setProperty("i18n_skip_text", True)
        self.body_label.setWordWrap(True)
        self.body_label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.body_label.setOpenExternalLinks(True)
        self.body_label.setObjectName("bubbleBody")
        self.body_label.setMinimumWidth(0)
        self.body_label.setMaximumHeight(16777215)
        self.body_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        layout.addWidget(self.body_label)
        self._game_message = False
        self.setMaximumHeight(16777215)
        self._apply_width_limit()

    def set_text(self, text: str) -> None:
        if text == self.text:
            return
        self.text = text
        self.item.text = text
        self.body_label.set_message(text, plain=True)
        self._sync_body_height()
        self.body_label.adjustSize()
        self.adjustSize()

    def set_game_message(self, enabled: bool = True) -> None:
        """Mark an assistant message as game-data content.

        Game answers can contain long recipes, quest objectives and source/wiki
        URLs.  They must never be clipped to the historical compact bubble
        height.  The chat viewport may scroll, but the bubble itself always owns
        enough height for the complete text.
        """
        self._game_message = bool(enabled)
        self.setMaximumHeight(16777215)
        self.body_label.setMaximumHeight(16777215)
        self._sync_body_height()
        self.updateGeometry()
        self.adjustSize()

    def _sync_body_height(self) -> None:
        """Force QLabel's wrapped height into the parent layout.

        On Windows/Qt a word-wrapped QLabel inside a compact scroll-area can
        keep the pre-wrap sizeHint.  With long game replies this produced a
        rounded bubble whose bottom border cut through the last lines.
        ``heightForWidth`` is the authoritative wrapped height, so feed it back
        as the label minimum and let QScrollArea scroll the full bubble.
        """
        width = max(1, int(self.body_label.maximumWidth()))
        if width >= 16777215:
            width = max(1, int(self.body_label.width()))
        # NEO 177: a word-wrapped QLabel prefers a narrow column («Привет! Всё / отлично. Чем /
        # помочь?»). Let short replies keep their natural width, long ones use the full bubble.
        metrics = self.body_label.fontMetrics()
        lines = str(self.text or "").splitlines() or [""]
        natural = max(metrics.horizontalAdvance(line) for line in lines) + 6
        self.body_label.setMinimumWidth(max(0, min(natural, width)))
        # Qt clamps heightForWidth to the current minimum height. Keeping the
        # value from the 30px font made the 10px measurement at least as tall.
        self.body_label.setMinimumHeight(0)
        self.body_label.setMaximumHeight(16777215)
        self.body_label.ensurePolished()
        self.body_label.updateGeometry()
        self._layout.invalidate()
        try:
            required = int(self.body_label.heightForWidth(width))
        except Exception:
            required = -1
        if required > 0:
            self.body_label.setMinimumHeight(required)
        else:
            self.body_label.setMinimumHeight(0)
        self.body_label.updateGeometry()
        self._layout.invalidate()
        self._layout.activate()
        self.setMinimumHeight(0)
        self.updateGeometry()
        row = self.parentWidget()
        if row is not None and row.layout() is not None:
            row.setMinimumHeight(0)
            row.layout().invalidate()
            row.updateGeometry()

    def set_thinking(self, text: str, *, expanded: bool = True) -> None:
        if self.thinking_toggle is None or self.thinking_label is None:
            return
        self.item.thinking = text
        self.thinking_label.setText(text)
        self.thinking_toggle.show()
        self.thinking_toggle.setChecked(bool(expanded))
        self.thinking_label.setVisible(bool(expanded))
        self.thinking_toggle.setText(("▼ " if expanded else "▶ ") + tr("Размышление", self._language))
        self.adjustSize()

    def collapse_thinking(self) -> None:
        if self.thinking_toggle is None or self.thinking_label is None:
            return
        if not self.item.thinking:
            return
        self.thinking_toggle.setChecked(False)
        self.thinking_label.hide()
        self.thinking_toggle.setText("▶ " + tr("Размышление", self._language))
        self.adjustSize()

    def _toggle_thinking(self, checked: bool) -> None:
        if self.thinking_toggle is None or self.thinking_label is None:
            return
        self.thinking_label.setVisible(bool(checked))
        self.thinking_toggle.setText(("▼ " if checked else "▶ ") + tr("Размышление", self._language))
        self.adjustSize()

    def set_language(self, code: str) -> None:
        self._language = str(code or "ru")
        if self.role == "user" and not self.property("custom_author"):
            self.author = tr("Вы", self._language)
            self.item.author = self.author
            self.author_label.setText(self.author)
        if self.thinking_toggle is not None:
            expanded = bool(self.thinking_toggle.isChecked())
            label = tr("Размышление", self._language)
            self.thinking_toggle.setText(("▼ " if expanded else "▶ ") + label)

    def _apply_width_limit(self) -> None:
        # The compact main window can be much narrower than the historical
        # 660px bubble cap.  Constrain both the bubble and the wrapping labels
        # to the actual chat viewport so long replies never disappear under
        # the right edge or vertical scrollbar.
        scaled_cap = max(240, int(660 * self._scale))
        limit = max(170, min(scaled_cap, int(self._available_width)))
        self.setMaximumWidth(limit)
        inner = max(120, limit - 30)
        self.body_label.setMaximumWidth(inner)
        if self.thinking_label is not None:
            self.thinking_label.setMaximumWidth(inner)
        self._sync_body_height()
        self.updateGeometry()

    def set_available_width(self, width: int) -> None:
        width = max(170, int(width))
        if width == self._available_width:
            return
        self._available_width = width
        self._apply_width_limit()

    def _apply_fonts(self) -> None:
        body_px = max(8, min(36, int(self._font_size_px)))
        author_font = self.author_label.font()
        author_font.setFamily(self._font_family)
        author_font.setPixelSize(max(8, body_px - 2)); author_font.setBold(True)
        time_font = self.time_label.font()
        time_font.setFamily(self._font_family)
        time_font.setPixelSize(max(8, body_px - 3))
        self.time_label.setFont(time_font)
        self.author_label.setFont(author_font)
        body_font = self.body_label.font(); body_font.setFamily(self._font_family); body_font.setPixelSize(body_px); self.body_label.setFont(body_font)
        if self.thinking_label is not None:
            thinking_font = self.thinking_label.font(); thinking_font.setFamily(self._font_family); thinking_font.setPixelSize(max(8, body_px - 2)); self.thinking_label.setFont(thinking_font)
        if self.thinking_toggle is not None:
            toggle_font = self.thinking_toggle.font(); toggle_font.setFamily(self._font_family); toggle_font.setPixelSize(max(8, body_px - 3)); self.thinking_toggle.setFont(toggle_font)

    def set_font_family(self, family: str) -> None:
        self._font_family = str(family or "Segoe UI").strip() or "Segoe UI"
        self._apply_fonts()
        self._sync_body_height()
        self.body_label.updateGeometry()
        self.adjustSize()

    def set_font_size(self, pixels: int) -> None:
        self._font_size_px = max(8, min(36, int(pixels)))
        self._apply_fonts()
        self._sync_body_height()
        self.body_label.updateGeometry()
        self.adjustSize()

    def set_scale(self, scale: float) -> None:
        # Window scaling changes bubble geometry only. Text stays at the explicit
        # pixel size selected in Settings so it is independent of window size.
        self._scale = max(0.5, float(scale))
        inset_x = max(6, int(9 * self._scale))
        inset_top = max(5, int(7 * self._scale))
        inset_bottom = max(5, int(8 * self._scale))
        self._layout.setContentsMargins(inset_x, inset_top, inset_x, inset_bottom)
        self._layout.setSpacing(max(3, int(4 * self._scale)))
        self._apply_width_limit()
        self._apply_fonts()
        self._sync_body_height()
        self.body_label.updateGeometry()
        self.adjustSize()


class ToolDetails(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(f"⚙  {text}", parent)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setObjectName("toolDetails")
        self.setProperty("i18n_skip_text", True)
        self._font_size_px = 11
        self._font_family = "Segoe UI"

    def set_font_family(self, family: str) -> None:
        self._font_family = str(family or "Segoe UI").strip() or "Segoe UI"
        font = self.font(); font.setFamily(self._font_family); font.setPixelSize(self._font_size_px); self.setFont(font)

    def set_font_size(self, pixels: int) -> None:
        self._font_size_px = max(8, min(32, int(pixels)))
        font = self.font(); font.setFamily(self._font_family); font.setPixelSize(self._font_size_px); self.setFont(font)

    def set_scale(self, scale: float) -> None:
        # Kept for call-site compatibility; font is deliberately px-based now.
        font = self.font(); font.setFamily(self._font_family); font.setPixelSize(self._font_size_px); self.setFont(font)


class PillScrollBar(QScrollBar):
    """A small, genuinely rounded chat scrollbar.

    Qt/Windows can occasionally fall back to the native rectangular slider even
    when a stylesheet specifies ``border-radius`` on ``QScrollBar::handle``.
    Painting the thumb ourselves keeps the chat history scrollbar pill-shaped on
    every theme while leaving QScrollBar's scrolling/dragging behaviour intact.
    """

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self._handle_color = QColor("#57D7FF")
        self._handle_hover_color = QColor("#57D7FF")
        self._handle_alpha = 74
        self._handle_hover_alpha = 118
        self.setMouseTracking(True)

    def set_handle_color(self, color: str) -> None:
        candidate = QColor(str(color or ""))
        if candidate.isValid():
            self._handle_color = candidate
            self._handle_hover_color = QColor(candidate)
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        if self.orientation() != Qt.Vertical or self.maximum() <= self.minimum():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)

        width = float(max(4, self.width()))
        height = float(max(1, self.height()))
        top_margin = 7.0
        bottom_margin = 7.0
        track_h = max(1.0, height - top_margin - bottom_margin)
        total = max(1, int(self.maximum() - self.minimum()) + max(1, int(self.pageStep())))
        visible_ratio = max(0.0, min(1.0, float(max(1, self.pageStep())) / float(total)))
        handle_h = max(24.0, track_h * visible_ratio)
        handle_h = min(track_h, handle_h)
        travel = max(0.0, track_h - handle_h)
        denom = max(1, int(self.maximum() - self.minimum()))
        position = max(0.0, min(1.0, float(self.value() - self.minimum()) / float(denom)))
        y = top_margin + travel * position

        inset = max(1.5, width * 0.20)
        handle_w = max(3.0, width - inset * 2.0)
        color = QColor(self._handle_hover_color if self.underMouse() else self._handle_color)
        color.setAlpha(self._handle_hover_alpha if self.underMouse() else self._handle_alpha)
        painter.setBrush(color)
        radius = handle_w * 0.5
        painter.drawRoundedRect(QRectF(inset, y, handle_w, handle_h), radius, radius)


class ChatView(QScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatView")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBar(PillScrollBar(Qt.Vertical, self))
        self.verticalScrollBar().setObjectName("chatScrollBar")
        self._container = QWidget()
        self._container.setObjectName("chatContainer")
        self._container.setAttribute(Qt.WA_StyledBackground, True)
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(4, 5, 4, 5)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        self.setWidget(self._container)
        # QScrollArea.setWidget() enables autoFillBackground on the child by
        # default. Disable it again so rgba(…,0) really reveals the desktop
        # through the permanently translucent top-level window.
        self._container.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)
        self._container.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.viewport().setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.items: list[ChatItem] = []
        self._widgets: list[QWidget] = []
        self._assistant_name = "NEO"
        self._user_name = ""
        self._language = "ru"
        self._scale = 1.0
        self._font_size_px = 14
        self._font_family = "Segoe UI"
        self._follow_bottom = True
        self._navigation_suppressed = False
        self._reader_anchor: tuple[QWidget, int] | None = None
        self._internal_scroll = False
        self._bottom_threshold = 24
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        # Coalesce layout-driven work into two queued turns. Following is a
        # position policy, not a polling timer or a timed generation window.
        self._scroll_timer.timeout.connect(lambda: self._scroll_settle_timer.start(0))
        self._scroll_settle_timer = QTimer(self)
        self._scroll_settle_timer.setSingleShot(True)
        self._scroll_settle_timer.setInterval(0)
        self._scroll_settle_timer.timeout.connect(self._scroll_to_bottom)
        self.verticalScrollBar().rangeChanged.connect(self._range_changed)
        self.verticalScrollBar().valueChanged.connect(self._scroll_value_changed)
        self.verticalScrollBar().sliderPressed.connect(self._cancel_pending_scroll)
        self.verticalScrollBar().sliderReleased.connect(
            lambda: self._scroll_value_changed(self.verticalScrollBar().value()))
        QTimer.singleShot(0, self, self._update_bubble_widths)

    def set_scrollbar_color(self, color: str) -> None:
        bar = self.verticalScrollBar()
        if isinstance(bar, PillScrollBar):
            bar.set_handle_color(color)

    def clear_messages(self) -> None:
        self._scroll_timer.stop()
        self._scroll_settle_timer.stop()
        self._follow_bottom = True
        self._navigation_suppressed = False
        self._reader_anchor = None
        for widget in self._widgets:
            widget.deleteLater()
        self._widgets.clear()
        self.items.clear()

    def set_assistant_name(self, name: str) -> None:
        self._assistant_name = name

    def set_language(self, code: str) -> None:
        self._language = str(code or "ru")
        for row in self._widgets:
            for bubble in row.findChildren(MessageBubble):
                bubble.set_language(self._language)

    def set_user_name(self, name: str) -> None:
        self._user_name = str(name or "").strip()

    def add_user(self, text: str) -> MessageBubble:
        return self._add_message("user", self._user_name or tr("Вы", self._language), text)

    def add_assistant(self, text: str) -> MessageBubble:
        return self._add_message("assistant", self._assistant_name, text)

    def add_assistant_pending(self, text: str = "Думает…") -> MessageBubble:
        return self.add_assistant(text)

    def _owns_bubble159(self, bubble: MessageBubble | None) -> bool:
        """A queued result may outlive a cleared, evicted or closed chat row."""
        if bubble is None or not isValid(self) or not isValid(bubble):
            return False
        return bubble.parentWidget() in self._widgets

    def update_bubble(self, bubble: MessageBubble | None, text: str) -> None:
        if not self._owns_bubble159(bubble):
            return
        self._prepare_scroll_change()
        bubble.set_text(text)

    def remove_bubble(self, bubble: MessageBubble | None) -> None:
        """Remove a provisional chat bubble, used when microphone input was only noise."""
        if not self._owns_bubble159(bubble):
            return
        row = bubble.parentWidget()
        try:
            item = bubble.item
            if item in self.items:
                self.items.remove(item)
        except Exception:
            pass
        if row in self._widgets:
            self._widgets.remove(row)
        if row is not None:
            self._layout.removeWidget(row)
            row.deleteLater()
        else:
            bubble.deleteLater()

    def move_bubble_to_bottom(self, bubble: MessageBubble | None) -> None:
        """Move an existing message row to the live edge without recreating it.

        Voice interaction uses one assistant bubble for the ephemeral
        ``Слушаю`` -> ``Думаю`` transition.  The final user transcript is known
        only after recording/ASR, so this helper keeps that same assistant bubble
        while moving it below the newly resolved user message.
        """
        if not self._owns_bubble159(bubble):
            return
        row = bubble.parentWidget()
        if row is None or row not in self._widgets:
            return
        self._layout.removeWidget(row)
        self._layout.insertWidget(self._layout.count() - 1, row)
        self._widgets.remove(row)
        self._widgets.append(row)
        try:
            item = bubble.item
            if item in self.items:
                self.items.remove(item)
                self.items.append(item)
        except Exception:
            pass

    def update_thinking(
        self, bubble: MessageBubble | None, text: str, *, expanded: bool = True
    ) -> None:
        if not self._owns_bubble159(bubble):
            return
        self._prepare_scroll_change()
        bubble.set_thinking(text, expanded=expanded)

    def collapse_thinking(self, bubble: MessageBubble | None) -> None:
        if not self._owns_bubble159(bubble):
            return
        self._prepare_scroll_change()
        bubble.collapse_thinking()

    def add_tool_details(self, details: list[str]) -> None:
        if not details:
            return
        text = "\n".join(details)
        self.items.append(ChatItem("details", text))
        label = ToolDetails(text)
        label.set_font_family(self._font_family)
        label.set_font_size(max(8, self._font_size_px - 3))
        label.set_scale(self._scale)
        row = QWidget()
        row.setObjectName("chatRow")
        row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(18, 0, 18, 0)
        row_layout.addWidget(label, 1)
        self._insert_widget(row)

    def _add_message(self, role: str, author: str, text: str) -> MessageBubble:
        item = ChatItem(role, text, author)
        self.items.append(item)
        bubble = MessageBubble(role, author, text, item)
        bubble.setProperty("custom_author", role == "user" and bool(self._user_name))
        bubble.set_language(self._language)
        bubble.set_font_family(self._font_family)
        bubble.set_font_size(self._font_size_px)
        bubble.set_scale(self._scale)
        row = QWidget()
        row.setObjectName("chatRow")
        row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(2, 0, 2, 0)
        if role == "user":
            row_layout.addStretch(1)
            row_layout.addWidget(bubble)
        else:
            row_layout.addWidget(bubble)
            row_layout.addStretch(1)
        self._insert_widget(row)
        return bubble

    def _insert_widget(self, widget: QWidget) -> None:
        self._prepare_scroll_change()
        self._layout.insertWidget(self._layout.count() - 1, widget)
        self._widgets.append(widget)
        # Qt message rows are comparatively expensive objects. The durable JSON
        # context keeps conversation history, so cap rendered widgets to prevent a
        # day-long session from accumulating hundreds/thousands of hidden rows.
        max_rows = 180
        while len(self._widgets) > max_rows:
            oldest = self._widgets.pop(0)
            if self._reader_anchor is not None and self._reader_anchor[0] is oldest:
                # The oldest visible row itself expired: retain the next row's
                # screen position where possible, then clamp at the new top.
                next_row = self._widgets[0]
                self._reader_anchor = (next_row, next_row.y() - self.verticalScrollBar().value())
            self._layout.removeWidget(oldest)
            oldest.deleteLater()
            if self.items:
                self.items.pop(0)
        self._update_bubble_widths()
        self._queue_scroll_settle()

    def _prepare_scroll_change(self) -> None:
        # Layout can shrink/clamp a reader to the live edge without a user
        # valueChanged signal. Decide before the next mutation grows the range.
        bar = self.verticalScrollBar()
        if (not self._navigation_suppressed and not bar.isSliderDown()
                and bar.maximum() - bar.value() <= self._bottom_threshold):
            self._follow_bottom = True
            self._reader_anchor = None

    def _remember_reader_position(self) -> None:
        bar = self.verticalScrollBar()
        value = bar.value()
        if value == bar.minimum():
            # A new row may not have its layout position yet. Staying at the
            # absolute top also handles an expired oldest row without a jump.
            self._reader_anchor = None
            return
        self._reader_anchor = next(
            ((row, row.y() - value) for row in self._widgets
             if row.y() + row.height() > value), None)

    def _cancel_pending_scroll(self) -> None:
        self._follow_bottom = False
        self._navigation_suppressed = True
        self._scroll_timer.stop()
        self._scroll_settle_timer.stop()
        self._remember_reader_position()

    def _scroll_value_changed(self, value: int) -> None:
        if self._internal_scroll:
            return
        # Navigation wins over any queued layout/submit scroll. Reaching the
        # live edge again allows later assistant text to follow naturally.
        self._cancel_pending_scroll()
        self._navigation_suppressed = False
        bar = self.verticalScrollBar()
        self._follow_bottom = not bar.isSliderDown() and bar.maximum() - value <= self._bottom_threshold
        if self._follow_bottom:
            self._reader_anchor = None

    def _queue_scroll_settle(self) -> None:
        if not self._scroll_timer.isActive() and not self._scroll_settle_timer.isActive():
            self._scroll_timer.start(0)

    def _range_changed(self, _minimum: int, _maximum: int) -> None:
        if not self._internal_scroll:
            # Restore before Qt clamps the old value on a shrinking range. The
            # guarded value change must not be mistaken for user navigation.
            self._scroll_to_bottom()
        self._queue_scroll_settle()

    def _request_scroll_to_bottom(self, widget: QWidget | None = None) -> None:
        del widget
        self._follow_bottom = True
        self._navigation_suppressed = False
        self._reader_anchor = None
        self._queue_scroll_settle()

    def scroll_to_bottom(self, *, force: bool = False) -> None:
        """User submissions force the live edge; passive updates respect it."""
        if force or self._follow_bottom:
            self._request_scroll_to_bottom()

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta().y() or event.pixelDelta().y()
        self._cancel_pending_scroll()
        super().wheelEvent(event)
        if delta <= 0:
            # At the lower boundary Qt emits no valueChanged for a down-wheel.
            self._scroll_value_changed(self.verticalScrollBar().value())

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in {Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End}:
            self._cancel_pending_scroll()
        super().keyPressEvent(event)
        if event.key() in {Qt.Key_Down, Qt.Key_PageDown, Qt.Key_End}:
            self._scroll_value_changed(self.verticalScrollBar().value())

    def scroll_by_hotkey(self, direction: int, *, pixels: int = 46) -> None:
        self._cancel_pending_scroll()
        bar = self.verticalScrollBar()
        delta = max(12, int(pixels)) * (-1 if int(direction) < 0 else 1)
        bar.setValue(max(bar.minimum(), min(bar.maximum(), bar.value() + delta)))
        if delta > 0:
            self._scroll_value_changed(bar.value())

    def _scroll_to_bottom(self) -> None:
        if self._internal_scroll:
            return
        bar = self.verticalScrollBar()
        self._internal_scroll = True
        try:
            self._layout.activate()
            if self._follow_bottom:
                bar.setValue(bar.maximum())
            elif self._reader_anchor is not None:
                row, offset = self._reader_anchor
                if isValid(row) and row in self._widgets:
                    bar.setValue(row.y() - offset)
        finally:
            self._internal_scroll = False

    def _update_bubble_widths(self) -> None:
        # HIST-013: a late pass after the view (or its viewport) was deleted is a no-op.
        try:
            viewport = self.viewport()
            if not isValid(self) or viewport is None or not isValid(viewport):
                return
            viewport_width = max(1, viewport.width())
        except RuntimeError:
            return
        # Keep a small breathing margin on both sides and account for the
        # scrollbar without ever enabling a horizontal scrollbar.
        available = max(170, int(viewport_width * 0.94) - 4)
        for row in self._widgets:
            row.setMaximumWidth(max(1, viewport_width - 2))
            for bubble in row.findChildren(MessageBubble):
                bubble.set_available_width(available)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_bubble_widths()
        QTimer.singleShot(0, self, self._update_bubble_widths)

    def set_font_family(self, family: str) -> None:
        self._font_family = str(family or "Segoe UI").strip() or "Segoe UI"
        for row in self._widgets:
            for bubble in row.findChildren(MessageBubble):
                bubble.set_font_family(self._font_family)
            for details in row.findChildren(ToolDetails):
                details.set_font_family(self._font_family)
        self._update_bubble_widths()

    def set_font_size(self, pixels: int) -> None:
        self._font_size_px = max(8, min(36, int(pixels)))
        for row in self._widgets:
            for bubble in row.findChildren(MessageBubble):
                bubble.set_font_size(self._font_size_px)
            for details in row.findChildren(ToolDetails):
                details.set_font_size(max(8, self._font_size_px - 3))
        self._update_bubble_widths()
        self._layout.invalidate()
        self._layout.activate()
        self._container.updateGeometry()
        QTimer.singleShot(0, self, self._update_bubble_widths)

    def set_scale(self, scale: float) -> None:
        self._scale = max(0.5, float(scale))
        side = max(2, int(4 * self._scale))
        vertical = max(3, int(5 * self._scale))
        self._layout.setContentsMargins(side, vertical, side, vertical)
        self._layout.setSpacing(max(4, int(6 * self._scale)))
        for row in self._widgets:
            row_layout = row.layout()
            if isinstance(row_layout, QHBoxLayout):
                row_side = max(1, int(2 * self._scale))
                row_layout.setContentsMargins(row_side, 0, row_side, 0)
            for bubble in row.findChildren(MessageBubble):
                bubble.set_scale(self._scale)
            for details in row.findChildren(ToolDetails):
                details.set_scale(self._scale)
        self._update_bubble_widths()
        self._container.updateGeometry()
