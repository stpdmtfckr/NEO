# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QPlainTextEdit, QVBoxLayout

from jarvis.i18n import tr


class LogViewerWindow(QDialog):
    """Small live neo_jarvis.log viewer that never creates a taskbar entry."""

    def __init__(self, log_file: Path, parent=None, *, language: str = "ru") -> None:
        super().__init__(parent)
        self.log_file = Path(log_file)
        self.language = str(language or "ru")
        self.setWindowTitle(tr("NEO — логи", self.language))
        self.setWindowFlags(Qt.Tool | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.resize(920, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.output = QPlainTextEdit(self)
        self.output.setReadOnly(True)
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(9)
        self.output.setFont(font)
        layout.addWidget(self.output)
        self.apply_theme("#EAF2FF", "#0C1017")

        # UI185-LOG-EMPTY-LANGUAGE-01: «not loaded yet» is a sentinel, never equal to a
        # really empty file, so an invalidated view always re-reads.
        self._last_payload: bytes | None = None
        self._tail_start = 0
        self._timer = QTimer(self)
        self._timer.setInterval(650)
        self._timer.setTimerType(Qt.CoarseTimer)
        self._timer.timeout.connect(self.refresh)

    def _read_tail(self) -> bytes:
        self._read_tail_start = 0
        try:
            if not self.log_file.exists():
                return tr("Лог ещё не создан: {path}", self.language).format(path=self.log_file).encode("utf-8")
            size = self.log_file.stat().st_size
            limit = 350_000
            with self.log_file.open("rb") as handle:
                if size > limit:
                    handle.seek(size - limit)
                    handle.readline()
                self._read_tail_start = handle.tell()
                return handle.read()
        except Exception as exc:
            return tr("Не удалось прочитать лог: {error}", self.language).format(error=exc).encode("utf-8", "replace")

    def refresh(self) -> None:
        payload = self._read_tail()
        start = int(getattr(self, "_read_tail_start", 0) or 0)
        if payload == self._last_payload and start == self._tail_start:
            return
        previous, previous_start = self._last_payload, self._tail_start
        self._last_payload, self._tail_start = payload, start
        bar = self.output.verticalScrollBar()
        follow = previous is None or bar.value() >= max(0, bar.maximum() - 24)
        # NEO 183: when the user reads an older part, keep that part on screen after the refresh.
        # 186 (UI185-LOG-TAIL-ANCHOR-01): the anchor is the record's position in the FILE; the
        # 350 KB tail moves forward, so the old block number would point at another record.
        anchor_line = self.output.firstVisibleBlock().blockNumber()
        target_line = 0
        if not follow and previous:
            lines = previous.split(b"\n")
            absolute = previous_start + sum(len(line) + 1 for line in lines[:max(0, anchor_line)])
            relative = absolute - start
            if 0 <= relative <= len(payload):
                target_line = payload[:relative].count(b"\n")
        self.output.setPlainText(payload.decode("utf-8", "replace").replace("\r\n", "\n"))
        if follow:
            bar.setValue(bar.maximum())
        else:
            block = self.output.document().findBlockByNumber(max(0, target_line))
            if block.isValid():
                # Scroll to the end first: making the cursor visible from below puts the
                # anchored record at the TOP, whatever the line wrapping.
                bar.setValue(bar.maximum())
                cursor = self.output.textCursor()
                cursor.setPosition(block.position())
                self.output.setTextCursor(cursor)
                self.output.ensureCursorVisible()

    def set_language(self, language: str) -> None:
        """NEO 183: title and service messages follow the interface language after Apply."""
        self.language = str(language or "ru")
        self.setWindowTitle(tr("NEO — логи", self.language))
        self._last_payload = None
        if self.isVisible():
            self.refresh()

    def apply_theme(self, text_color: str, background: str, accent: str = "#57D7FF") -> None:
        """NEO 183: explicit colours for the dark and light themes (the text used to inherit the
        light theme colour while the editor kept the white system background)."""
        self.setStyleSheet(
            f"QDialog {{ background:{background}; }}"
            f"QPlainTextEdit {{ background:{background}; color:{text_color}; border:1px solid {accent}; "
            f"selection-background-color:{accent}; selection-color:{background}; }}"
        )

    def showEvent(self, event) -> None:  # noqa: N802
        self.refresh()
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        event.ignore()
        self.hide()
