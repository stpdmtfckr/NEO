# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations
import re
import logging

from copy import deepcopy
import os
import secrets
import time
import hmac
import uuid
import threading
from pathlib import Path
from typing import Any

import requests

from PySide6.QtCore import Qt, Signal, Slot, QObject, QTimer, QUrl, QThreadPool, QEvent, QRect, QSize
from PySide6.QtGui import QColor, QKeySequence, QDesktopServices, QFont, QShortcut, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QToolTip,
    QCheckBox as _QtCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QInputDialog,
    QProgressBar,
    QFormLayout,
    QGroupBox,
    QFontComboBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QKeySequenceEdit,
    QMessageBox as _QtMessageBox,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QStackedWidget,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QPlainTextEdit,
    QProxyStyle,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
)

from jarvis.config import ConfigManager, DEFAULT_ASSISTANT_NAME, DEFAULT_USER_NAME, DEFAULT_ASSISTANT_PROMPT
from jarvis.ui.qt_localization import LocalizedFileDialog as QFileDialog, LocalizedColorDialog as QColorDialog
from jarvis.ollama_client import OllamaClient
from jarvis.secrets import protect_secret, unprotect_secret, telegram_critical_phrase
from jarvis.windows_voice_unlock import current_windows_account, ensure_native_provider_registered, validate_windows_password
from jarvis.ui.audio_capture import AudioCaptureController
from jarvis.ui.theme_presets import THEME_PRESETS
from jarvis.runtime.diagnostics import safe_error
from jarvis.i18n import LANGUAGES, language_code, translate_widget_tree, tr, _ui_catalog, EXACT
from jarvis.runtime.localized_text import localize_runtime_text
from jarvis.integrations.yandex_auth import authorize_yandex_music
from jarvis.integrations.mail_services import authorize_gmail, test_gmail, test_mailru
from jarvis.integrations.google_calendar import authorize_google_calendar
from jarvis.integrations.telegram_messages import telegram_message_center
from jarvis.game_web import GameWebCache
from jarvis.game_web.searchapi import SearchAPIGoogleProvider
from jarvis.workers import FunctionWorker
from jarvis.ui.desktop_strings import dt, install_catalogue

install_catalogue()
from jarvis.ui.settings_text184 import install as _install_settings_text184, model_text as _model_text184
from jarvis.ui.quality_memory184 import append_quality_choices, append_memory_editor, restore_quality_choices
_install_settings_text184()
from jarvis.integrations.obs_text184 import install_catalogue as _install_obs_text184
_install_obs_text184()


def _localize_owner(widget: QWidget | None, text: str) -> str:
    """Translate late-created Settings chrome using the dialog's selected locale."""
    value = str(text or "")
    try:
        owner = widget.window() if widget is not None else None
        config = getattr(owner, "config", None) or getattr(widget, "config", None)
        if config is not None:
            code = language_code(config)
            if value in _ui_catalog(code) or value in EXACT:
                return tr(value, code)
            # NEO 184: an already formatted text («Gmail авторизован: qa@…») matches its catalog template.
            from jarvis.i18n import _translate_template
            templated = _translate_template(value, code) if code != "ru" else ""
            if templated:
                return templated
            localized = localize_runtime_text(value, code)
            from jarvis.runtime.localized_text import _FALLBACK
            # Unknown provider errors must not disappear behind a generic
            # "not localized" result. Explicit templates format payloads below.
            return value if any(localized in pair for pair in _FALLBACK.values()) else localized
    except Exception:
        pass
    return value


class QMessageBox(_QtMessageBox):
    """QMessageBox facade that never leaks Russian application chrome after a locale switch."""

    @staticmethod
    def warning(parent, title, text, *args, **kwargs):
        return _QtMessageBox.warning(parent, _localize_owner(parent, title), _localize_owner(parent, text), *args, **kwargs)

    @staticmethod
    def information(parent, title, text, *args, **kwargs):
        return _QtMessageBox.information(parent, _localize_owner(parent, title), _localize_owner(parent, text), *args, **kwargs)

    @staticmethod
    def critical(parent, title, text, *args, **kwargs):
        return _QtMessageBox.critical(parent, _localize_owner(parent, title), _localize_owner(parent, text), *args, **kwargs)

    @staticmethod
    def question(parent, title, text, *args, **kwargs):
        return _QtMessageBox.question(parent, _localize_owner(parent, title), _localize_owner(parent, text), *args, **kwargs)

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(_localize_owner(self.parentWidget(), text))

    def setWindowTitle(self, title: str) -> None:  # noqa: N802
        super().setWindowTitle(_localize_owner(self.parentWidget(), title))

    def addButton(self, button, role=None):  # noqa: N802
        if isinstance(button, str):
            button = _localize_owner(self.parentWidget(), button)
        if role is None:
            return super().addButton(button)
        return super().addButton(button, role)


class ColorField(QWidget):
    def __init__(self, value: str, parent: QWidget | None = None, *, default_value: str = "") -> None:
        super().__init__(parent)
        self._default_value = str(default_value or "")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(value)
        self.button = QPushButton("Выбрать")
        self.button.clicked.connect(self._choose)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)
        self.edit.textChanged.connect(self._refresh_preview)
        self._refresh_preview()

    def value(self) -> str:
        return self.edit.text().strip()

    def _choose(self) -> None:
        initial = QColor(self.value())
        color = QColorDialog.getColor(initial if initial.isValid() else QColor("#FFFFFF"), self)
        if color.isValid():
            self.edit.setText(color.name())

    def _refresh_preview(self) -> None:
        color = QColor(self.value())
        if color.isValid():
            self.button.setStyleSheet(
                f"QPushButton {{ background:{color.name()}; color:{'#000000' if color.lightness() > 145 else '#FFFFFF'}; }}"
            )
        else:
            self.button.setStyleSheet("")


class StoragePathField(QWidget):
    """Editable storage directory with Browse/Open controls.

    The path text is user-owned and therefore intentionally left untouched by
    localization. Only the surrounding buttons/tooltips are translated.
    """
    def __init__(self, value: str, parent: QWidget | None = None, *, default_value: str = "") -> None:
        super().__init__(parent)
        self._default_value = str(default_value or "")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.edit = QLineEdit(str(value or ""))
        self.edit.setPlaceholderText("Путь к папке")
        self.browse_button = QPushButton("Выбрать")
        self.open_button = QPushButton("Открыть")
        self.default_button = QPushButton("По умолчанию")
        self.browse_button.clicked.connect(self._browse)
        self.open_button.clicked.connect(self._open)
        self.default_button.clicked.connect(self._reset_default)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.browse_button)
        layout.addWidget(self.open_button)
        layout.addWidget(self.default_button)

    def value(self) -> str:
        return self.edit.text().strip()

    def _reset_default(self) -> None:
        self.edit.setText(self._default_value)

    def _browse(self) -> None:
        current = self.value()
        expanded = os.path.expandvars(os.path.expanduser(current)) if current else ""
        start = expanded if expanded and Path(expanded).exists() else str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, _localize_owner(self, "Выберите папку"), start)
        if selected:
            self.edit.setText(str(Path(selected)))

    def _open(self) -> None:
        value = self.value()
        if not value:
            return
        path = Path(os.path.expandvars(os.path.expanduser(value)))
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            QMessageBox.warning(self, "Папка", f"Не удалось открыть папку: {exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))


from jarvis.ui.responsive178 import WrapCheckBox as QCheckBox, NavButton as _NavButton, full_title as _full_title178, responsive as _responsive178  # noqa: E402


class ScrollableSettingsTabs(QWidget):
    """NEO 174: settings sections in a sidebar with search, and a page header.

    Keeps the old interface (``addTab``, ``replaceTab``, ``setCurrentIndex``,
    ``_stack``, ``_buttons``): the former horizontal strip cut long titles and
    needed its own scrollbar once there were ten sections.
    """

    # Short description under each page title (same order as the pages).
    SUBTITLES = (
        "Распознавание речи, микрофон и слово-активация",
        "Музыка, видео и управление воспроизведением",
        "Поведение NEO во время игр",
        "Локальная модель перевода и настройки перевода",
        "Цвета, шрифты и размер интерфейса",
        "Внешний вид и анимация робота",
        "Ключи и подключения интернет-сервисов",
        "Подтверждения, приватность и журналы",
        "Запуск, производительность и служебные функции",
        "Виджеты на рабочем столе",
        "Сохранённые команды, шаги и подтверждения",
        "Сочетания клавиш и автоматические нажатия мыши",
        "Диагностика, модели и технические параметры",
        "Telegram, привязка владельца и ограничения доступа",
        "",  # Existing Voice page intentionally had no subtitle.
        "Локальное управление OBS Studio",
        "Локальная разговорная модель, память и обучение на этом компьютере",
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QHBoxLayout(self)
        self._root_layout = root
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        self._nav = QWidget()
        self._nav.setObjectName("settingsNav")
        nav_layout = QVBoxLayout(self._nav)
        self._nav_layout = nav_layout
        nav_layout.setContentsMargins(10, 12, 10, 12)
        nav_layout.setSpacing(8)
        self._search = QLineEdit()
        self._search.setObjectName("settingsSearch")
        self._search.setPlaceholderText("Найти настройку…")
        self._search.setClearButtonEnabled(True)
        nav_layout.addWidget(self._search)
        self._tab_scroll = QScrollArea()
        self._tab_scroll.setObjectName("settingsTabScroll")
        self._tab_scroll.setWidgetResizable(True)
        self._tab_scroll.setFrameShape(QScrollArea.NoFrame)
        self._tab_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._tab_strip = QWidget()
        self._tab_strip.setObjectName("settingsTabStrip")
        self._tab_layout = QVBoxLayout(self._tab_strip)
        self._tab_layout.setContentsMargins(0, 0, 0, 0)
        self._tab_layout.setSpacing(3)
        self._tab_layout.addStretch(1)
        self._tab_scroll.setWidget(self._tab_strip)
        nav_layout.addWidget(self._tab_scroll, 1)
        self._no_results = QLabel("Ничего не найдено")
        self._no_results.setObjectName("help")
        self._no_results.setWordWrap(True)
        self._no_results.hide()
        nav_layout.insertWidget(1, self._no_results)
        # 186 (settings TZ 4): where the found setting lives — «Группа → Страница → Блок».
        self._search_path186 = QLabel("")
        self._search_path186.setObjectName("help")
        self._search_path186.setWordWrap(True)
        self._search_path186.setTextFormat(Qt.PlainText)
        self._search_path186.setProperty("i18n_skip_text", True)
        self._search_path186.hide()
        nav_layout.insertWidget(2, self._search_path186)
        self._group_headers186: list[tuple[QLabel, tuple[int, ...]]] = []
        self._group_of_page186: dict[int, QLabel] = {}
        self._page_keywords186: dict[int, str] = {}
        root.addWidget(self._nav, 0)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        self._content_layout = content_layout
        content_layout.setContentsMargins(0, 4, 0, 0)
        content_layout.setSpacing(2)
        # NEO 187 HREQ-007: the left menu holds the main categories only; the
        # subsections of the chosen category are tabs at the TOP of this panel.
        from PySide6.QtWidgets import QTabBar
        self._subtabs187 = QTabBar()
        self._subtabs187.setObjectName("settingsSubTabs")
        self._subtabs187.setExpanding(False)
        self._subtabs187.setUsesScrollButtons(True)
        self._subtabs187.setElideMode(Qt.ElideNone)
        self._subtabs187.setDrawBase(False)
        self._subtabs187.setProperty("i18n_skip_text", True)
        self._subtabs187.hide()
        self._subtab_pages187: list[int] = []
        self._subtab_group187 = -1
        self._subtab_guard187 = False
        self._last_in_group187: dict[int, int] = {}
        self._grouped187 = False
        self._subtabs187.currentChanged.connect(self._subtab_changed187)
        content_layout.addWidget(self._subtabs187)
        self._title = QLabel("")
        self._title.setObjectName("settingsPageTitle")
        # 186: a long title at a large UI scale wraps instead of being clipped;
        # the full text also stays available to tooltip and accessibility.
        self._title.setWordWrap(True)
        self._title.setTextFormat(Qt.PlainText)
        self._title.setMinimumWidth(1)
        self._title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._subtitle = QLabel("")
        self._subtitle.setObjectName("settingsPageSubtitle")
        self._subtitle.setWordWrap(True)
        content_layout.addWidget(self._title)
        content_layout.addWidget(self._subtitle)
        content_layout.addSpacing(8)
        self._stack = QStackedWidget()
        self._stack.setObjectName("settingsStack")
        content_layout.addWidget(self._stack, 1)
        root.addWidget(content, 1)

        self._buttons: list[QPushButton] = []
        self._subtitles: list[str] = []
        self._search_index: dict[int, list[tuple[str, QWidget]]] = {}
        self._ui_scale = 1.0
        # Kept for callers that stop it on close (the strip no longer needs deferred geometry).
        self._geometry_timer160 = QTimer(self)
        self._geometry_timer160.setSingleShot(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(160)
        self._search_timer.timeout.connect(self._apply_search)
        self._search.textChanged.connect(lambda _text: (self._invalidate_hits186(), self._search_timer.start()))
        self._search.returnPressed.connect(self._jump_to_first_hit)
        self._hit_timer = QTimer(self)
        self._hit_timer.setSingleShot(True)
        self._hit_timer.setInterval(1600)
        self._hit_timer.timeout.connect(self._clear_hit)
        self._hit_widget: QWidget | None = None
        # 186: deferred hit work belongs to one query/locale/page revision.
        self._search_rev186 = 0

    def addTab(self, widget: QWidget, title: str) -> int:  # noqa: N802
        _responsive178(widget)
        self._forms_dirty178 = True
        index = self._stack.addWidget(widget)
        button = _NavButton(title)
        button.setObjectName("settingsTabButton")
        button.setCheckable(True)
        button.setProperty("tabIndex", index)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(title)
        button.clicked.connect(lambda _checked=False, i=index: self.setCurrentIndex(i))
        self._tab_layout.insertWidget(self._tab_layout.count() - 1, button)
        self._buttons.append(button)
        self._subtitles.append(self.SUBTITLES[index] if index < len(self.SUBTITLES) else "")
        if index == 0:
            self.setCurrentIndex(0)
        return index

    def set_navigation_groups186(self, groups) -> None:
        """186 (settings TZ 2): show the pages under six group headings.

        Only the menu order changes; the stack keeps the dependency-safe build
        order, so page indices, button IDs and stable page IDs stay valid."""
        for header, _indices in self._group_headers186:
            self._tab_layout.removeWidget(header)
            header.deleteLater()
        self._group_headers186 = []
        self._group_of_page186 = {}
        for button in self._buttons:
            self._tab_layout.removeWidget(button)
        position = 0
        placed: set[int] = set()
        for title, indices in groups:
            indices = tuple(i for i in indices if 0 <= i < len(self._buttons) and i not in placed)
            if not indices:
                continue
            # NEO 187 HREQ-007: a category is a button; its pages open as top tabs.
            header = _NavButton(title)
            header.setObjectName("settingsNavGroup")
            header.setCheckable(True)
            header.setCursor(Qt.PointingHandCursor)
            header.setToolTip(title)
            header.setAccessibleName(title)
            group_number = len(self._group_headers186)
            header.clicked.connect(lambda _checked=False, g=group_number: self._open_group187(g))
            self._tab_layout.insertWidget(position, header); position += 1
            for index in indices:
                self._tab_layout.insertWidget(position, self._buttons[index]); position += 1
                self._group_of_page186[index] = header
                placed.add(index)
            self._group_headers186.append((header, indices))
        for index, button in enumerate(self._buttons):      # never lose a page
            if index not in placed:
                self._tab_layout.insertWidget(position, button); position += 1
        self._grouped187 = bool(self._group_headers186) and not (set(range(len(self._buttons))) - placed)
        self._sync_nav187()
        self._apply_nav_tab_order188()

    def _apply_nav_tab_order188(self) -> None:
        """HIST-011: Tab/Shift+Tab follow the menu as drawn, not the creation order.

        Search → category/page buttons top to bottom → the top tabs of the page.
        Hidden entries are skipped by Qt itself, so one chain serves both the
        category view and the search results view."""
        chain: list[QWidget] = [self._search]
        for position in range(self._tab_layout.count()):
            widget = self._tab_layout.itemAt(position).widget()
            if widget is not None:
                chain.append(widget)
        chain.append(self._subtabs187)
        for before, after in zip(chain, chain[1:]):
            QWidget.setTabOrder(before, after)

    def _sync_nav187(self) -> None:
        """Outside a search the sidebar shows categories; pages are top tabs."""
        if self._grouped187 and not self._norm(self._search.text()):
            for button in self._buttons:
                button.hide()
            for header, _indices in self._group_headers186:
                header.show()
        self._sync_subtabs187(self._stack.currentIndex())

    def _group_of187(self, index: int) -> int:
        return next((g for g, (_header, ids) in enumerate(self._group_headers186) if index in ids), -1)

    def _sync_subtabs187(self, index: int) -> None:
        if not self._grouped187:
            self._subtabs187.hide()
            return
        group = self._group_of187(index)
        for g, (header, _ids) in enumerate(self._group_headers186):
            header.setChecked(g == group)
        if group < 0:
            self._subtabs187.hide()
            return
        pages = list(self._group_headers186[group][1])
        self._subtab_guard187 = True
        try:
            if group != self._subtab_group187 or pages != self._subtab_pages187:
                while self._subtabs187.count():
                    self._subtabs187.removeTab(0)
                for page in pages:
                    self._subtabs187.addTab(self._buttons[page].text())
                self._subtab_pages187, self._subtab_group187 = pages, group
            else:
                for position, page in enumerate(pages):
                    if self._subtabs187.tabText(position) != self._buttons[page].text():
                        self._subtabs187.setTabText(position, self._buttons[page].text())
            for position, page in enumerate(pages):
                self._subtabs187.setTabToolTip(position, self._buttons[page].toolTip() or self._buttons[page].text())
            self._subtabs187.setCurrentIndex(pages.index(index))
        finally:
            self._subtab_guard187 = False
        self._subtabs187.show()
        self._last_in_group187[group] = index

    def _subtab_changed187(self, position: int) -> None:
        if self._subtab_guard187 or not 0 <= position < len(self._subtab_pages187):
            return
        self.setCurrentIndex(self._subtab_pages187[position])

    def _open_group187(self, group: int) -> None:
        if not 0 <= group < len(self._group_headers186):
            return
        ids = self._group_headers186[group][1]
        target = self._last_in_group187.get(group, ids[0])
        self.setCurrentIndex(target if target in ids else ids[0])
        self._sync_subtabs187(self._stack.currentIndex())   # a re-click keeps its category checked

    def set_page_keywords186(self, keywords: dict[int, str]) -> None:
        """Synonyms and former names that also find a page (never secrets)."""
        self._page_keywords186 = {int(i): self._norm(text) for i, text in keywords.items()}

    def page_path186(self, index: int, widget: QWidget | None = None) -> str:
        """«Группа → Страница → Блок» of a page (and of a widget inside it)."""
        parts: list[str] = []
        header = self._group_of_page186.get(index)
        if header is not None:
            parts.append(header.text())
        if 0 <= index < len(self._buttons):
            parts.append(self._buttons[index].text())
        node = widget.parentWidget() if widget is not None else None
        while node is not None and node is not self._stack:
            if isinstance(node, QGroupBox):
                title = _full_title178(node)
                if title:
                    parts.append(title)
                break
            node = node.parentWidget()
        return " → ".join(part for part in parts if part)

    def replaceTab(self, index: int, widget: QWidget) -> None:  # noqa: N802
        """Replace a loading page while preserving selection and button IDs."""
        if not 0 <= index < self._stack.count():
            raise IndexError(index)
        old = self._stack.widget(index)
        selected = self._stack.currentIndex()
        self._stack.removeWidget(old)
        _responsive178(widget)
        self._stack.insertWidget(index, widget)
        self._forms_dirty178 = True
        self._fit_forms178()
        self._stack.setCurrentIndex(selected)
        self._invalidate_hits186()
        old.deleteLater()
        self._search_index.pop(index, None)
        self._apply_nav_tab_order188()          # HIST-011: a deferred page keeps the menu order
        if self._search.text().strip():
            self._search_timer.start()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        if not (0 <= index < self._stack.count()):
            return
        self._stack.setCurrentIndex(index)
        for i, button in enumerate(self._buttons):
            button.setChecked(i == index)
        self._sync_subtabs187(index)
        if index < len(self._buttons):
            self._tab_scroll.ensureWidgetVisible(self._buttons[index], 0, 10)
            self.refresh_header()
            if self._search.text().strip():
                hits = self._hits(index, self._search.text())
                if hits:
                    ticket = (self._search_rev186, index, self._stack.widget(index), self._search.text())
                    QTimer.singleShot(0, self, lambda w=hits[0], t=ticket: self._show_deferred_hit186(w, t))

    def _invalidate_hits186(self) -> None:
        self._search_rev186 += 1

    def _show_deferred_hit186(self, widget: QWidget, ticket) -> None:
        """Apply a queued hit only if it still belongs to the current search state."""
        rev, index, page, query = ticket
        if rev != self._search_rev186 or self._stack.currentIndex() != index:
            return
        if self._stack.widget(index) is not page or self._search.text() != query:
            return
        try:
            if page is None or not page.isAncestorOf(widget):
                return
        except RuntimeError:          # the target was destroyed meanwhile
            return
        if widget not in self._hits(index, query):
            return
        self._show_hit(widget)

    def refresh_header(self) -> None:
        """Title from the (translated) section button, subtitle translated by the owner dialog."""
        index = self._stack.currentIndex()
        if not 0 <= index < len(self._buttons):
            return
        button = self._buttons[index]
        self._title.setText(button.text())
        self._title.setToolTip(button.text())
        self._title.setAccessibleName(button.text())
        # NEO 182: the title's translation source follows the selected section, so a later
        # retranslation (Apply) keeps this section's title instead of the first one's.
        self._title.setProperty("i18n_text_source", button.property("i18n_text_source") or button.text())
        source = self._subtitles[index] if index < len(self._subtitles) else ""
        self._subtitle.setProperty("i18n_text_source", source)
        owner = self.window()
        translate = getattr(owner, "_t", None)
        self._subtitle.setText(translate(source) if callable(translate) and source else source)
        self._subtitle.setVisible(bool(source))

    def retranslated182(self) -> None:
        """NEO 182: after the dialog was retranslated, rebuild the header and the search index."""
        self._invalidate_hits186()
        self._search_index.clear()
        self.refresh_header()
        self._sync_subtabs187(self._stack.currentIndex())
        if self._search.text().strip():
            self._apply_search()

    def set_ui_scale(self, scale: float) -> None:
        self._ui_scale = max(0.80, min(1.55, float(scale)))
        self._fit_nav178()
        self._root_layout.setSpacing(max(10, int(14 * self._ui_scale)))
        for button in self._buttons:
            button.setMinimumHeight(max(30, int(36 * self._ui_scale)))

    def _fit_nav178(self) -> None:
        """Sidebar width follows the window: about a quarter of it, never wider than before."""
        widest = max(190, int(236 * self._ui_scale))
        target = max(148, min(widest, int(self.width() * 0.25))) if self.width() > 0 else widest
        if self._nav.width() != target:
            self._nav.setFixedWidth(target)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_nav178()
        self._fit_forms178()

    def _fit_forms178(self) -> None:
        """A narrow window puts each setting's name above its field instead of beside it."""
        narrow = 0 < self.width() - self._nav.width() < 520
        if narrow == getattr(self, '_narrow178', None) and not getattr(self, '_forms_dirty178', False):
            return
        self._narrow178 = narrow
        self._forms_dirty178 = False
        policy = QFormLayout.WrapAllRows if narrow else QFormLayout.WrapLongRows
        for form in self._stack.findChildren(QFormLayout):
            if form.rowWrapPolicy() != policy:
                form.setRowWrapPolicy(policy)

    # ---- search -----------------------------------------------------------------------
    @staticmethod
    def _norm(text: str) -> str:
        return " ".join(str(text or "").replace("ё", "е").replace("Ё", "Е").casefold().split())

    def _index_page(self, index: int) -> list[tuple[str, QWidget]]:
        cached = self._search_index.get(index)
        if cached is not None:
            return cached
        page = self._stack.widget(index)
        rows: list[tuple[str, QWidget]] = []
        if page is not None:
            for kind in (QGroupBox, _QtCheckBox, QLabel, QPushButton):
                for widget in page.findChildren(kind):
                    text = _full_title178(widget) if isinstance(widget, QGroupBox) else widget.text()
                    if text and len(text) < 400:
                        rows.append((self._norm(text), widget))
        self._search_index[index] = rows
        return rows

    @staticmethod
    def _near187(word: str, text: str) -> bool:
        """REC-SETTINGS-SEARCH-SOURCE-02: bounded typo tolerance for one query word.

        Exact substrings are handled by the caller first. Here a word of 4+
        letters may differ from one caption word (or its prefix) by one edit
        or one swap of neighbours; 8+ letters allow two edits.
        """
        if len(word) < 4:
            return False
        limit = 2 if len(word) >= 8 else 1
        for token in text.split():
            for target in {token, token[:len(word)], token[:len(word) + 1]}:
                if abs(len(target) - len(word)) > limit:
                    continue
                if _edit_distance187(word, target, limit) <= limit:
                    return True
        return False

    def _hits(self, index: int, query: str, *, fuzzy: bool = False) -> list[QWidget]:
        words = self._norm(query).split()
        if not words:
            return []

        def has(word: str, text: str) -> bool:
            return word in text or (fuzzy and self._near187(word, text))
        title = self._norm(self._buttons[index].text()) if index < len(self._buttons) else ""
        hits = [widget for text, widget in self._index_page(index) if all(has(word, text) for word in words)]
        if not hits and all(has(word, title) for word in words):
            hits = [self._buttons[index]]
        if not hits and index < len(self._buttons):
            keywords = self._page_keywords186.get(index, "")
            if keywords and all(has(word, keywords) for word in words):
                hits = [self._buttons[index]]
        return hits

    def _search_mode187(self, query: str) -> bool:
        """Exact matches anywhere win; typo matching is only a fallback."""
        return not any(self._hits(index, query) for index in range(len(self._buttons)))

    def _apply_search(self) -> None:
        query = self._search.text()
        if not self._norm(query):
            for button in self._buttons:
                button.setVisible(not self._grouped187)   # HREQ-007: pages are top tabs then
            for header, _indices in self._group_headers186:
                header.show()
            self._no_results.hide()
            self._search_path186.hide()
            return
        first = -1
        fuzzy = self._search_mode187(query)
        for index, button in enumerate(self._buttons):
            found = bool(self._hits(index, query, fuzzy=fuzzy))
            button.setVisible(found)
            if found and first < 0:
                first = index
        for header, indices in self._group_headers186:
            header.setVisible(any(not self._buttons[i].isHidden() for i in indices))
        self._no_results.setVisible(first < 0)
        if first < 0:
            self._search_path186.hide()
        if first >= 0 and not self._buttons[self._stack.currentIndex()].isVisible():
            self.setCurrentIndex(first)
        elif first >= 0:
            # The page stays: the shown path must describe *this* query's hit
            # in the current language, never an older query's breadcrumb.
            current = self._stack.currentIndex()
            hits = [w for w in self._hits(current, query, fuzzy=fuzzy) if w not in self._buttons]
            path = self.page_path186(current, hits[0]) if hits else ""
            if path:
                self._search_path186.setText(path)
                self._search_path186.show()
            else:
                self._search_path186.hide()

    def _jump_to_first_hit(self) -> None:
        self._search_timer.stop()
        self._apply_search()
        current = self._stack.currentIndex()
        order = [current] + [i for i in range(len(self._buttons)) if i != current]
        fuzzy = self._search_mode187(self._search.text())
        for index in order:
            hits = self._hits(index, self._search.text(), fuzzy=fuzzy)
            if hits:
                if index != current:
                    self.setCurrentIndex(index)
                else:
                    self._show_hit(hits[0])
                return

    def _show_hit(self, widget: QWidget) -> None:
        if widget is None or widget in self._buttons:
            return
        page = self._stack.currentWidget()
        owner = self.window()
        developer = getattr(owner, "developer_advanced_container", None)
        if developer is not None and developer.isHidden() and developer.isAncestorOf(widget):
            widget = owner.developer_settings_enabled
            hint = owner._t("Параметр находится в режиме разработчика. Включите его, чтобы увидеть поле.")
            self._collapsed_hint182 = hint
            QToolTip.showText(widget.mapToGlobal(widget.rect().bottomLeft()), hint, widget)
        if page is not None and not widget.isVisibleTo(page):
            # NEO 182: the hit sits in a collapsed (switched-off) integration. Point at the group's
            # visible switch and say how to open it; the search never switches an integration on.
            holder = widget.parentWidget()
            while holder is not None and holder is not page and not holder.isVisibleTo(page):
                holder = holder.parentWidget()
            visible = holder if holder is not None and holder is not page else None
            if isinstance(visible, QGroupBox):
                switch = next((box for box in visible.findChildren(_QtCheckBox) if box.isVisibleTo(page)), None)
                visible = switch or visible
            if visible is not None:
                widget = visible
                owner = self.window()
                translate = getattr(owner, "_t", None)
                hint = "Параметр находится в свёрнутой интеграции. Включите её, чтобы увидеть поле."
                hint = translate(hint) if callable(translate) else hint
                self._collapsed_hint182 = hint
                QToolTip.showText(widget.mapToGlobal(widget.rect().bottomLeft()), hint, widget)
        area = widget.parentWidget()
        while area is not None and not isinstance(area, QScrollArea):
            area = area.parentWidget()
        if area is not None:
            area.ensureWidgetVisible(widget, 20, 40)
        self._clear_hit()
        self._hit_widget = widget
        path = self.page_path186(self._stack.currentIndex(), widget)
        if path and self._search.text().strip():
            self._search_path186.setText(path)
            self._search_path186.show()
        widget.setProperty("searchHit", True)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        self._hit_timer.start()

    def _clear_hit(self) -> None:
        widget, self._hit_widget = self._hit_widget, None
        if widget is None:
            return
        try:
            widget.setProperty("searchHit", False)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        except RuntimeError:
            pass


def _edit_distance187(a: str, b: str, limit: int) -> int:
    """Optimal-string-alignment distance with an early exit above ``limit``."""
    if a == b:
        return 0
    previous2: list[int] | None = None
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if previous2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                current[j] = min(current[j], previous2[j - 2] + 1)
        if min(current) > limit:
            return limit + 1
        previous2, previous = previous, current
    return previous[-1]


class _SixRowComboStyle(QProxyStyle):
    """Force Qt's list-view combo popup instead of the Windows menu-style popup.

    Several Windows styles report SH_ComboBox_Popup=True. In that mode Qt is
    allowed to ignore maxVisibleItems(), which is why font/theme lists could grow
    to almost the whole monitor. Returning False restores the same scrollable
    list-view geometry used by the microphone selector.
    """

    def styleHint(self, hint, option=None, widget=None, returnData=None):  # noqa: N802
        try:
            if hint == QStyle.StyleHint.SH_ComboBox_Popup:
                return 0
        except AttributeError:
            if hint == QStyle.SH_ComboBox_Popup:
                return 0
        return super().styleHint(hint, option, widget, returnData)


def _configure_six_row_combo(combo: QComboBox) -> None:
    combo.setMaxVisibleItems(6)
    # Keep one proxy style alive for as long as the combo exists.  The dialog-wide
    # pass also sees CompactComboBox instances, so this helper must be idempotent.
    proxy = getattr(combo, "_neo_six_row_popup_style", None)
    if proxy is None:
        proxy = _SixRowComboStyle()
        proxy.setParent(combo)  # Qt owns the style for the full native widget lifetime.
        combo._neo_six_row_popup_style = proxy  # type: ignore[attr-defined]
        combo.setStyle(proxy)
    view = combo.view()
    if view is None:
        return
    view.setVerticalScrollMode(QAbstractItemView.ScrollPerItem)
    view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)


class CompactComboBox(QComboBox):
    """Normal QComboBox popup capped at six visible rows.

    HOTFIX39 keeps popup geometry with Qt but disables the Windows menu-style popup. The previous manual
    resize of QComboBoxPrivateContainer could collapse the list into thin
    horizontal strips on Windows. ``setMaxVisibleItems`` gives the standard
    microphone-device style popup while the item view supplies a normal
    vertical scrollbar whenever more than six entries exist.
    """

    MAX_VISIBLE_ROWS = 6

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        _configure_six_row_combo(self)


class CompactFontComboBox(QFontComboBox):
    """Font selector using the same standard six-row scrollable popup."""

    MAX_VISIBLE_ROWS = 6

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditable(False)
        _configure_six_row_combo(self)


class _ModelOperationRelay184(QObject):
    """Keep the real terminal outcome alive after its settings view is closed."""

    progress = Signal(str, int)
    completed = Signal(dict)
    failed = Signal(str)
    finished = Signal()
    status = Signal(str, str)
    disposed = Signal(str)
    terminal_result = Signal(dict)

    _UNCONFIRMED = {
        "ru": "Операция завершилась без подтверждённого результата. Нажмите «Проверить».",
        "en": "The operation ended without a confirmed result. Click Check.",
        "de": "Der Vorgang endete ohne bestätigtes Ergebnis. Klicken Sie auf Prüfen.",
        "fr": "L’opération s’est terminée sans résultat confirmé. Cliquez sur Vérifier.",
        "es": "La operación terminó sin un resultado confirmado. Pulsa Comprobar.",
        "it": "L’operazione è terminata senza un risultato confermato. Premi Verifica.",
        "pt": "A operação terminou sem um resultado confirmado. Clique em Verificar.",
    }

    def __init__(self, operation, config, *, action: str, base_url: str):
        super().__init__(QApplication.instance())
        self.operation = operation
        self.action = str(action)
        self.base_url = str(base_url).rstrip("/")
        self.config = config
        self.operation_id = uuid.uuid4().hex
        self.cancel_requested = False
        self.terminal = False
        self.done = False
        # Only this application-owned receiver observes the worker directly.
        # Its widget receivers disconnect automatically when the dialog dies.
        operation.progress.connect(self._progress, Qt.QueuedConnection)
        operation.completed.connect(self._completed, Qt.QueuedConnection)
        operation.failed.connect(self._failed, Qt.QueuedConnection)
        operation.finished.connect(self._finished, Qt.QueuedConnection)

    def _text(self, message: str) -> str:
        code = language_code(self.config)
        text = _model_text184(str(message), code)
        if text in _ui_catalog(code) or text in EXACT:
            return tr(text, code)
        localized = localize_runtime_text(text, code)
        from jarvis.runtime.localized_text import _FALLBACK
        return text if any(localized in pair for pair in _FALLBACK.values()) else localized

    def announce(self, message: str) -> None:
        self.status.emit(self._text(message), self.operation_id)

    def cancel(self) -> None:
        if self.terminal or self.cancel_requested:
            return
        self.cancel_requested = True
        self.operation.cancel()
        # Cancellation is a request. A committed delete may still succeed, or
        # its verification may fail; only the worker can report that outcome.
        self.announce("Отменяю операцию…")

    @Slot(str, int)
    def _progress(self, message: str, percent: int) -> None:
        if self.terminal or self.cancel_requested:
            return
        self.progress.emit(message, percent)
        self.announce(message)

    @Slot(dict)
    def _completed(self, result: dict) -> None:
        if self.terminal:
            return
        self.terminal = True
        self.terminal_result.emit(dict(result, action=self.action, base_url=self.base_url, operation_id=self.operation_id))
        self.completed.emit(result)
        self.announce(str(result.get("message") or "Операция с моделью завершена"))

    @Slot(str)
    def _failed(self, message: str) -> None:
        if self.terminal:
            return
        self.terminal = True
        self.terminal_result.emit({"action": self.action, "base_url": self.base_url, "operation_id": self.operation_id, "status": "failed", "ready": False})
        self.failed.emit(message)
        self.announce(message)

    @Slot()
    def _finished(self) -> None:
        if self.done:
            return
        self.done = True
        if not self.terminal:
            self._failed(self._UNCONFIRMED.get(language_code(self.config), self._UNCONFIRMED["en"]))
        self.finished.emit()
        self.disposed.emit(self.operation_id)
        self.operation = None
        self.deleteLater()


class _DiscordDiscoveryConfig184:
    """Read-only request snapshot; a background lookup never follows profile edits."""
    def __init__(self, config):
        self.data = deepcopy(config.data)
        self.paths = deepcopy(config.paths)

    def get(self, dotted_key: str, default=None):
        value = self.data
        for part in dotted_key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


# Only private discovery adapters enter this lane. A failed cleanup retains one
# owner and blocks new Gateway allocation until that owner is confirmed stopped.
_DISCORD_DISCOVERY_LANE184 = threading.Lock()
_DISCORD_DISCOVERY_PENDING184 = None


def _close_discovery_bots184(adapter) -> dict[str, Any]:
    """Close only this private lookup adapter's clients, never the global registry.

    ReliableDiscordAdapter starts with its own empty bot map and constructs new
    bot instances in _background_for_profile; it does not borrow the app's music
    clients. Take the ownership snapshot under its lock, then release that lock
    before waiting for Gateway teardown.
    """
    owned = getattr(adapter, "_background_bots", None)
    if owned is None:
        # Inert adapters without transports are also useful to embedding callers.
        return {"attempted": 0, "closed": 0, "remaining": 0, "complete": True, "errors": []}
    with adapter._background_bots_lock:
        bots = list({id(bot): bot for bot in owned.values()}.values())
    report = {"attempted": len(bots), "closed": 0, "remaining": 0, "complete": True, "errors": []}
    for bot in bots:
        try:
            stopped = bot.close(timeout=3.0) is True
        except Exception as exc:
            stopped = False
            report["errors"].append(type(exc).__name__)
        if stopped:
            report["closed"] += 1
            with adapter._background_bots_lock:
                for key, candidate in list(owned.items()):
                    if candidate is bot:
                        owned.pop(key, None)
        else:
            # Keep failed clients reachable; their normal application shutdown
            # watchdog must not mistake an unconfirmed close for stopped native IO.
            report["complete"] = False
    with adapter._background_bots_lock:
        report["remaining"] = len({id(bot) for bot in owned.values()})
    # Do not report complete if a late callback allocated another private client
    # while close was waiting. Retain it for the normal shutdown watchdog.
    report["complete"] = report["complete"] and report["remaining"] == 0
    adapter._discovery_transport_cleanup184 = report
    if not report["complete"]:
        logging.getLogger(__name__).warning(
            "Discord discovery transport cleanup unconfirmed: stopped %s of %s attempted clients; %s remain",
            report["closed"], report["attempted"], report["remaining"],
        )
    return report


def _discovery_cleanup_failure184(cleanup, lookup_error=None):
    message = "Не удалось подтвердить закрытие временного подключения Discord."
    if lookup_error is not None:
        message = safe_error(lookup_error) + "\n" + message
    failure = RuntimeError(message)
    failure.discovery_transport_cleanup = cleanup
    return failure


def _discover_discord_channels184(config, server: str, cancelled) -> dict[str, Any]:
    # Capture only snapshot data and a thread-safe cancellation token, never a
    # widget or a bound dialog method in the runnable's retained input closure.
    global _DISCORD_DISCOVERY_PENDING184
    if cancelled.is_set():
        return {"cancelled": True}
    if not _DISCORD_DISCOVERY_LANE184.acquire(blocking=False):
        raise RuntimeError("Обнаружение каналов Discord уже выполняется.")
    try:
        pending = _DISCORD_DISCOVERY_PENDING184
        if pending is not None:
            cleanup = _close_discovery_bots184(pending)
            if not cleanup["complete"]:
                raise _discovery_cleanup_failure184(cleanup)
            _DISCORD_DISCOVERY_PENDING184 = None
        if cancelled.is_set():
            return {"cancelled": True}
        from jarvis.integrations.discord_navigation import ReliableDiscordAdapter
        adapter = ReliableDiscordAdapter(config)
        _DISCORD_DISCOVERY_PENDING184 = adapter
        result = {}
        lookup_error = None
        try:
            if not cancelled.is_set():
                result = dict(adapter.execute("list_channels", server=server) or {})
        except Exception as exc:
            lookup_error = exc
            raise
        finally:
            cleanup = _close_discovery_bots184(adapter)
            if cleanup["complete"]:
                _DISCORD_DISCOVERY_PENDING184 = None
            else:
                # The owner stays reachable. Another lookup may retry its close,
                # but may not allocate a replacement while teardown is uncertain.
                raise _discovery_cleanup_failure184(cleanup, lookup_error) from lookup_error
        # Close itself may block briefly; cancellation during that wait still wins.
        return {"cancelled": True} if cancelled.is_set() else result
    finally:
        _DISCORD_DISCOVERY_LANE184.release()


class _DiscordDiscoveryRelay184(QObject):
    """Dialog-owned queued receiver: destruction disconnects every worker edge."""
    result = Signal(object, object)
    error = Signal(object, str)
    finished = Signal(object)

    def __init__(self, parent, *, context, server: str, cancelled):
        super().__init__(parent)
        self.context = context
        self.server = server
        self.cancelled = cancelled
        self.worker = None
        self.terminal = False
        self.done = False
        # deleteLater() without QDialog.finished must also invalidate a lookup.
        self.destroyed.connect(cancelled.set)

    def bind(self, worker) -> None:
        self.worker = worker
        worker.signals.result.connect(self._result, Qt.QueuedConnection)
        worker.signals.error.connect(self._error, Qt.QueuedConnection)
        worker.signals.finished.connect(self._finished, Qt.QueuedConnection)

    def cancel(self) -> None:
        self.cancelled.set()

    @Slot(object)
    def _result(self, result: object) -> None:
        if self.done or self.terminal or self.cancelled.is_set():
            return
        self.terminal = True
        self.result.emit(self, result)

    @Slot(str)
    def _error(self, trace: str) -> None:
        if self.done or self.terminal or self.cancelled.is_set():
            return
        self.terminal = True
        self.error.emit(self, trace)

    @Slot()
    def _finished(self) -> None:
        if self.done:
            return
        self.done = True
        self.finished.emit(self)
        self.worker = None
        self.context = None
        self.deleteLater()


class SettingsDialog(QDialog):
    settings_saved = Signal()
    scenario_run_requested = Signal(str)
    scenario_retry_requested = Signal(dict)
    model_progress = Signal(str)
    model_notice184 = Signal(str)
    model_operation_started184 = Signal(object)

    def _t(self, source: str) -> str:
        """Translate application-owned runtime UI text in the currently selected locale."""
        return tr(str(source or ""), language_code(self.config))

    def _rt(self, source: str) -> str:
        """Localize dynamic/status text while guaranteeing no foreign UI leakage."""
        return _localize_owner(self, str(source or ""))

    def _status_text(self, label: QLabel, template: str, **values: object) -> None:
        """Translate only fixed framing, including subsequent live Apply."""
        label.setProperty("i18n_text_source", template)
        label.setProperty("i18n_text_values", values)
        label.setText(self._t(template).format(**values))


    def __init__(self, config: ConfigManager, parent: QWidget | None = None, *, deferred: bool = False) -> None:
        super().__init__(parent)
        self.config = config
        from jarvis.response_voice.text import install_catalogue
        install_catalogue()
        self._settings_ready160 = False
        self._settings_closed160 = False
        self._settings_build_error160 = False
        self._settings_page_index160 = 0
        self._settings_build_timer160 = QTimer(self)
        self._settings_build_timer160.setSingleShot(True)
        self._settings_build_timer160.setInterval(12)
        self._settings_build_timer160.timeout.connect(self._build_next_settings_page160)
        self.finished.connect(self._stop_settings_build160)
        self._discord_discovery_worker = None
        self._discord_discovery_relay184 = None
        self.finished.connect(self._cancel_discord_discovery184)
        self.settings_saved.connect(self._invalidate_discord_discovery_view184)
        self._game_cache_stats_closed158 = False
        self._game_cache_stats_generation158 = 0
        self._game_cache_stats_worker158 = None
        self._game_cache_stats_running_generation158 = 0
        self.finished.connect(self._close_game_cache_stats158)
        self.ollama = OllamaClient(config)
        self.data = deepcopy(config.data)
        self._model_operation184 = None
        self._model_relay184 = None
        self._model_operation_generation184 = 0
        self._model_setup_request184 = None
        self._model_setup_timer184 = QTimer(self)
        self._model_setup_timer184.setSingleShot(True)
        self._model_setup_timer184.timeout.connect(self._run_model_setup_request184)
        self.finished.connect(self._cancel_model_operation184)
        self._gmail_tokens: dict[str, Any] = {
            "refresh_token": unprotect_secret(str(config.get("integrations.gmail.refresh_token_encrypted", "") or "")),
            "access_token": unprotect_secret(str(config.get("integrations.gmail.access_token_encrypted", "") or "")),
            "expires_at": float(config.get("integrations.gmail.token_expires_at", 0.0) or 0.0),
            "account_email": str(config.get("integrations.gmail.account_email", "") or ""),
        }
        self._calendar_tokens: dict[str, Any] = {
            "refresh_token": unprotect_secret(str(config.get("integrations.google_calendar.refresh_token_encrypted", "") or "")),
            "access_token": unprotect_secret(str(config.get("integrations.google_calendar.access_token_encrypted", "") or "")),
            "expires_at": float(config.get("integrations.google_calendar.token_expires_at", 0.0) or 0.0),
        }
        self._preset_guard = False
        self._ui_scale = 1.0
        self._theme_resize_timer = QTimer(self)
        self._theme_resize_timer.setSingleShot(True)
        self._theme_resize_timer.setInterval(24)
        self._theme_resize_timer.timeout.connect(self._refresh_dialog_theme)
        self.setWindowTitle("Настройки NEO")
        # NEO 174: a sidebar with sections and search; a little wider than the old tab strip.
        self.resize(1000, 660)
        # NEO 178: pages wrap their texts, so the window may be smaller than before.
        self.setMinimumSize(600, 420)

        layout = QVBoxLayout(self)
        self._root_layout = layout
        self.tabs = ScrollableSettingsTabs()
        self._settings_pages160 = (
            (self._voice_tab, "Микрофон и wake word"),
            (self._media_tab, "Медиа"),
            (self._gaming_tab169, "Игры"),
            (self._translation_tab184, "Перевод"),
            (self._appearance_tab, "Окно"),
            (self._robot_tab, "Робот"),
            (self._games_tab, "API-сервисы"),
            (self._security_tab, "Безопасность"),
            (self._additional_tab, "Дополнительно"),
            (self._shelf_tab, dt("widgets", "ru")),
            (self._scenarios_tab, "Сценарии"),
            (self._hotkeys_tab184, "Горячие клавиши и автокликер"),
            (self._developer_settings_tab184, "Режим разработчика"),
            (self._remote_connection_tab, "Удалённое подключение"),
            (self._response_voice_tab, "Голос и ответы"),
            (self._streaming_tab184, "Стриминг"),
            # NEO 187 HREQ-007: «Общение и память» moved here from «Дополнительно»;
            # built last because it adopts the block «Дополнительно» created.
            (self._conversation_tab187, "Общение и память"),
        )
        # NEW185-SETTINGS-STALE-PAGE-INDEX: commands and deep links name a page by a
        # stable ID; its position in the menu is looked up, never hard-coded.
        self._settings_page_ids186 = (
            "microphone", "media", "games", "translation", "window", "robot", "api", "security",
            "advanced", "widgets", "scenarios", "hotkeys", "developer", "remote", "voice", "streaming",
            "conversation",
        )
        for _factory, title in self._settings_pages160:
            placeholder = QWidget()
            placeholder_layout = QVBoxLayout(placeholder)
            placeholder_layout.addWidget(QLabel("Загружаю настройки…"))
            placeholder_layout.addStretch(1)
            self.tabs.addTab(placeholder, title)
        # 186 (settings TZ 2): six groups in the menu; the build order above stays
        # dependency-safe (the developer page reuses blocks built by «Дополнительно»).
        page_index = {page_id: i for i, page_id in enumerate(self._settings_page_ids186)}
        self.tabs.set_navigation_groups186([
            (title, [page_index[page_id] for page_id in ids]) for title, ids in self.NAV_GROUPS186])
        self.tabs.set_page_keywords186({page_index[k]: v for k, v in self.PAGE_KEYWORDS186.items() if k in page_index})
        layout.addWidget(self.tabs, 1)

        buttons_row = QWidget()
        buttons_layout = QHBoxLayout(buttons_row)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(10)
        self.defaults_button = QPushButton("По умолчанию")
        self.cancel_button = QPushButton("Отмена")
        self.apply_button = QPushButton("Применить")
        self.save_button = QPushButton("Сохранить")
        self.defaults_button.clicked.connect(self._reset_all_settings)
        self.cancel_button.clicked.connect(self.reject)
        self.apply_button.clicked.connect(lambda _checked=False: self._save(close=False))
        self.save_button.clicked.connect(self._save)
        # NEO 174: «По умолчанию» on the left, away from «Сохранить»; «Сохранить» is the main button.
        self.defaults_button.setObjectName("quietButton")
        self.save_button.setObjectName("primaryButton")
        for button in (self.defaults_button, self.cancel_button, self.apply_button, self.save_button):
            button.setMinimumWidth(120)
            button.setAutoDefault(False)      # Enter in a field (search, keys) never saves or closes the window
            button.setCursor(Qt.PointingHandCursor)
            buttons_layout.addWidget(button)
            if button is self.defaults_button:
                buttons_layout.addStretch(1)
        buttons_row.setObjectName("settingsFooter")
        layout.addWidget(buttons_row)
        find = QShortcut(QKeySequence.Find, self)
        find.activated.connect(lambda: (self.tabs._search.setFocus(), self.tabs._search.selectAll()))

        self._settings_load_status160 = QLabel("Загружаю настройки…", self)
        self._settings_load_status160.setWordWrap(True)
        layout.insertWidget(0, self._settings_load_status160)
        for button in (self.defaults_button, self.apply_button, self.save_button):
            button.setEnabled(False)
        # Set the inherited stylesheet on a small shell before creating ~1900
        # descendants. Reapplying an identical sheet would repolish every one.
        self._refresh_dialog_theme()
        translate_widget_tree(self, language_code(self.config))
        self.tabs.refresh_header()
        if deferred:
            self._settings_build_timer160.start()
        else:
            # Public eager default supports embedders that need controls
            # immediately. Production opens a shell and yields between pages.
            while not self._settings_ready160 and not self._settings_build_error160:
                self._build_next_settings_page160(schedule=False)

    NAV_GROUPS186 = (
        ("Ассистент", ("microphone", "voice", "translation", "conversation")),
        ("Внешний вид", ("window", "robot", "widgets")),
        ("Медиа и игры", ("media", "games")),
        ("Стриминг", ("streaming",)),
        ("Подключения и безопасность", ("api", "remote", "security")),
        ("Команды", ("scenarios", "hotkeys")),
        ("Система", ("advanced", "developer")),
    )

    # Settings TZ 4: synonyms and former names (ASR/STT/микрофон; вейк; Голос/TTS/озвучка;
    # словарь/алиасы; performance/нагрузка; OBS/стриминг; Telegram/удалёнка …).
    PAGE_KEYWORDS186 = {
        "microphone": "asr stt микрофон распознавание речи вейк wake word фраза активации словарь алиасы dictionary поведение",
        "voice": "голос tts озвучка озвучивание ответы voice speech",
        "translation": "перевод translate translation переводчик",
        "window": "окно window цвета тема прозрачность шрифт сводка briefing",
        "robot": "робот robot анимация аватар gif",
        "widgets": "виджеты полка shelf bookshelf таймеры widgets",
        "media": "музыка медиа media spotify яндекс youtube discord плейлист видео",
        "games": "игры games steam",
        "streaming": "obs стриминг streaming трансляция",
        "api": "api ключи gmail почта календарь calendar searchapi погода",
        "remote": "telegram телеграм удаленка удалённое подключение remote бот",
        "security": "безопасность security privacy приватность доступ файлы",
        "scenarios": "сценарии scenarios команды",
        "hotkeys": "горячие клавиши hotkeys hotkey автокликер autoclicker",
        "advanced": "дополнительно performance производительность нагрузка профиль язык language автозапуск startup папки",
        "conversation": "общение память memory разговор беседа диалог chat conversation локальная модель qwen ollama обучение learning",
        "developer": "разработчик developer ollama диагностика модели",
    }

    PAGE_ALIASES186 = {
        "shelf": "widgets", "bookshelf": "widgets", "widget": "widgets", "privacy": "security",
        "spotify": "media", "music": "media", "wake": "microphone", "dictionary": "microphone",
        "tts": "voice", "obs": "streaming", "telegram": "remote", "api_services": "api",
        "hotkey": "hotkeys", "scenario": "scenarios", "appearance": "window",
    }

    def page_index186(self, page_id: str) -> int | None:
        """Menu position of a stable settings page ID (or a documented alias)."""
        key = str(page_id or "").strip().casefold()
        key = self.PAGE_ALIASES186.get(key, key)
        ids = getattr(self, "_settings_page_ids186", ())
        return ids.index(key) if key in ids else None

    def open_page(self, page_id: str) -> bool:
        index = self.page_index186(page_id)
        if index is None:
            return False
        self.tabs.setCurrentIndex(index)
        return True

    def _build_next_settings_page160(self, *, schedule: bool = True) -> None:
        if self._settings_closed160 or self._settings_ready160 or self._settings_build_error160:
            return
        try:
            index = self._settings_page_index160
            factory, _title = self._settings_pages160[index]
            page = factory()
            self._configure_combo_popups(page)
            self._configure_spinbox_wheel_guard(page)
            self._configure_combo_wheel_guard(page)
            self._name_form_fields184(page)
            translate_widget_tree(page, language_code(self.config))
            self._translate_table_headers184(page)
            self._tidy_forms174(page)
            self.tabs.replaceTab(index, page)
            self._settings_page_index160 += 1
            if self._settings_page_index160 == len(self._settings_pages160):
                self._settings_ready160 = True
                self._snapshot_numeric_fields185()
                self._settings_load_status160.hide()
                for button in (self.defaults_button, self.apply_button, self.save_button):
                    button.setEnabled(True)
                # Native device discovery is asynchronous, after its controls exist.
                if schedule:
                    self._microphone_start_timer160 = QTimer(self)
                    self._microphone_start_timer160.setSingleShot(True)
                    self._microphone_start_timer160.timeout.connect(self._refresh_microphones_async)
                    self._microphone_start_timer160.start(0)
                else:
                    QTimer.singleShot(0, self, self._refresh_microphones_async)
                if self._model_setup_request184 is not None:
                    self._model_setup_timer184.start(0)
                return
        except Exception:
            logging.getLogger(__name__).exception("Could not construct settings page index=%s", self._settings_page_index160)
            self._settings_build_error160 = True
            self._settings_build_timer160.stop()
            source = "Не удалось загрузить настройки. Закройте окно и откройте его снова."
            self._status_text(self._settings_load_status160, source)
            self.cancel_button.setText(self._t("Закрыть"))
            # The error replaces the loading page as well, including when the
            # user has selected a tab whose controls have not been created yet.
            for index in range(self._settings_page_index160, len(self._settings_pages160)):
                page = QWidget()
                layout = QVBoxLayout(page)
                notice = QLabel(self._t(source)); notice.setWordWrap(True)
                layout.addWidget(notice); layout.addStretch(1)
                self.tabs.replaceTab(index, page)
            return
        if schedule:
            self._settings_build_timer160.start()

    def _translate_table_headers184(self, root: QWidget) -> None:
        """Translate application-owned table chrome while leaving user rows intact."""
        for table in root.findChildren(QTableWidget):
            for column in range(table.columnCount()):
                item = table.horizontalHeaderItem(column)
                if item is None:
                    continue
                source = item.data(int(Qt.UserRole) + 184)
                if source is None:
                    source = item.text()
                    item.setData(int(Qt.UserRole) + 184, source)
                item.setText(self._t(str(source)))
                tooltip = item.data(int(Qt.UserRole) + 185)
                if tooltip is None:
                    tooltip = item.toolTip()
                    item.setData(int(Qt.UserRole) + 185, tooltip)
                if tooltip:
                    item.setToolTip(self._t(str(tooltip)))

    @staticmethod
    def _name_form_fields184(page: QWidget) -> None:
        """NEO 184: every editor in a form row is named by the row's label (screen readers).

        Rows added directly (colour fields, folder fields, size pairs) did not get a name;
        the name follows the interface language like the label itself."""
        editors = (QKeySequenceEdit, QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox)

        def focus_targets(field: QWidget) -> list[QWidget]:
            # UI185-HOTKEY-A11Y-01: a QKeySequenceEdit is the Tab-focus target; its private
            # QLineEdit is not what a screen reader lands on.
            if isinstance(field, editors):
                return [field]
            found = [w for w in field.findChildren(QWidget) if isinstance(w, editors)]
            sequences = [w for w in found if isinstance(w, QKeySequenceEdit)]
            return [w for w in found if not any(seq.isAncestorOf(w) for seq in sequences)]

        for form in page.findChildren(QFormLayout):
            for row in range(form.rowCount()):
                label_item = form.itemAt(row, QFormLayout.LabelRole)
                field_item = form.itemAt(row, QFormLayout.FieldRole)
                label = label_item.widget() if label_item is not None else None
                field = field_item.widget() if field_item is not None else None
                if not isinstance(label, QLabel) or field is None:
                    continue
                title = str(label.property("i18n_text_source") or label.text() or "").strip().rstrip(":")
                if not title:
                    continue
                inner = focus_targets(field)
                for widget in inner:
                    if not widget.accessibleName():
                        widget.setProperty("i18n_a11y_name_source", title)
                        widget.setAccessibleName(title)
                for button in field.findChildren(QPushButton):
                    if not button.accessibleDescription():
                        button.setProperty("i18n_a11y_desc_source", title)
                        button.setAccessibleDescription(title)
                buddy = label.buddy()
                if inner and (buddy is None or any(isinstance(w, QKeySequenceEdit) and w.isAncestorOf(buddy) for w in inner)):
                    label.setBuddy(inner[0])

    @staticmethod
    def _tidy_forms174(page: QWidget) -> None:
        """NEO 174: a long label moves above its field instead of pushing the page off-screen."""
        for form in page.findChildren(QFormLayout):
            form.setRowWrapPolicy(QFormLayout.WrapLongRows)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            form.setHorizontalSpacing(max(form.horizontalSpacing(), 16))
            form.setVerticalSpacing(max(form.verticalSpacing(), 10))

    def _stop_settings_build160(self, *_args) -> None:
        self._settings_closed160 = True
        self._model_setup_request184 = None
        self._model_setup_timer184.stop()
        self._settings_build_timer160.stop()
        self._theme_resize_timer.stop()
        self.tabs._geometry_timer160.stop()
        timer = getattr(self, '_microphone_start_timer160', None)
        if timer is not None:
            timer.stop()

    def _on_language_preview_changed(self, *_args) -> None:
        """Stage the selected locale without mutating the live Settings UI.

        HOTFIX106: language is transactional, exactly like the other Settings
        values.  Changing the combo must not resize/retranslate the dialog or
        switch language-dependent integrations before Apply/Save is pressed.
        """
        self._pending_language_code = (
            str(self.app_language.currentData() or "en").casefold()
            if hasattr(self, "app_language") else language_code(self.config)
        )

    def _configure_combo_popups(self, root: QWidget | None = None) -> None:
        """Cap every Settings QComboBox at six visible rows.

        This is intentionally implemented with Qt's own combo-popup sizing, not
        by resizing the private popup window. That keeps the list visually and
        behaviorally identical to the working microphone device selector.
        """
        for combo in (root if root is not None else self).findChildren(QComboBox):
            _configure_six_row_combo(combo)

    def _configure_spinbox_wheel_guard(self, root: QWidget | None = None) -> None:
        """Keep the mouse wheel from accidentally changing numeric settings.

        Wheel input over a spin box is redirected to the surrounding settings
        page instead. Values can still be changed by typing or by the explicit
        up/down buttons. This applies to both integer and floating-point controls,
        including controls created directly rather than through ``_spin``.
        """
        for spin in (root if root is not None else self).findChildren(QAbstractSpinBox):
            spin.installEventFilter(self)

    def _configure_combo_wheel_guard(self, root: QWidget | None = None) -> None:
        """Do not let a closed dropdown change selection from mouse-wheel input.

        The wheel still scrolls the popup list after the user explicitly opens it
        because Qt sends those events to the popup view, not to the QComboBox.
        When the list is closed, the same wheel gesture scrolls the surrounding
        settings page instead of silently changing a preset/font/device/value.
        """
        for combo in (root if root is not None else self).findChildren(QComboBox):
            combo.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        if isinstance(watched, (QAbstractSpinBox, QComboBox)) and event.type() == QEvent.Type.Wheel:
            # Preserve normal page scrolling when the pointer happens to be over a
            # value editor, but never feed a closed-wheel event to the control.
            # This covers spin boxes, presets, fonts, devices, languages, modes
            # and every other QComboBox created anywhere in Settings.
            parent = watched.parentWidget()
            area = None
            while parent is not None:
                if isinstance(parent, QScrollArea):
                    area = parent
                    break
                parent = parent.parentWidget()
            if area is not None:
                bar = area.verticalScrollBar()
                delta = int(event.angleDelta().y())
                steps = delta / 120.0 if delta else 0.0
                if steps:
                    amount = max(18, int(bar.singleStep()) * 3)
                    bar.setValue(bar.value() - int(round(steps * amount)))
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def sync_live_layout_values(
        self,
        *,
        window_width: int | None = None,
        window_height: int | None = None,
        chat_height: int | None = None,
    ) -> None:
        """Mirror live layout editing into the still-open Settings controls.

        Layout editing is intentionally live: the main window, chat divider and
        inline HUD can be resized while Settings remains open.  Before HOTFIX83
        the dialog kept the values it had when it was constructed, so the next
        press of Apply wrote those stale numbers back over the live geometry.
        Keep the visible controls synchronized instead of letting Apply undo the
        user's last drag.
        """

        pairs = (
            ("window_width", window_width),
            ("window_height", window_height),
            ("chat_height_px", chat_height),
        )
        for name, value in pairs:
            if value is None or not hasattr(self, name):
                continue
            widget = getattr(self, name)
            try:
                old = widget.blockSignals(True)
                widget.setValue(int(value))
                widget.blockSignals(old)
            except Exception:
                try:
                    widget.blockSignals(False)
                except Exception:
                    pass

    def _preview_language_change(self, _index: int = -1) -> None:
        # Backward-compatible slot: selection is staged only.  The saved locale
        # is applied by _save() after the user explicitly confirms Settings.
        self._on_language_preview_changed(_index)

    def _theme_value(self, key: str, default: str) -> str:
        # Settings are transactional: controls may contain staged values, but the
        # dialog and the main UI continue using the last saved/applied palette.
        # This prevents a preset/color selection from changing appearance before
        # the user explicitly presses «Применить» or «Сохранить».
        value = str(self.config.get(f"ui.{key}", default) or default)
        return QColor(value).name() if QColor(value).isValid() else default

    def _refresh_dialog_theme(self) -> None:
        if getattr(self, '_settings_closed160', False):
            return
        # Palette/explicit UI scale changes may restyle the dialog. Ordinary
        # geometry changes must not repolish thousands of controls: responsive
        # layouts handle width, while Qt keeps native-DPI text/icons sharp.
        accent = self._theme_value("accent", "#57D7FF")
        window = self._theme_value("window_color", "#0D121B")
        panel = self._theme_value("panel_color", "#131B27")
        border = self._theme_value("border_color", accent)
        text = self._theme_value("text_color", "#EAF2FF")
        muted = self._theme_value("muted_text_color", "#8F9DB2")
        element = self._theme_value("element_color", "#161F2D")
        hover = self._theme_value("element_hover_color", "#24364B")
        assets_dir = Path(__file__).resolve().parents[2] / "assets"
        check_icon = (assets_dir / "checkmark.svg").as_posix()
        down_icon = (assets_dir / "chevron_down.svg").as_posix()
        up_icon = (assets_dir / "chevron_up.svg").as_posix()
        try:
            # UI185-THEME-ARROW-01 / CHECKMARK-01: arrows and ticks get a stroke that stays
            # readable (>= 3:1) on the field / checked fill of the current theme.
            from jarvis.ui.theme_icons186 import icon_path, readable_on
            icons_dir = Path(getattr(getattr(self.config, "paths", None), "cache", None) or (Path.home() / ".neo_cache")) / "theme_icons"
            arrow = readable_on(element, text)
            down_icon = icon_path(icons_dir, "chevron_down", arrow)
            up_icon = icon_path(icons_dir, "chevron_up", arrow)
            check_icon = icon_path(icons_dir, "checkmark", readable_on(accent, "#071018"))
        except Exception:
            logging.getLogger(__name__).debug("Theme icons unavailable; using the packaged ones", exc_info=True)
        scale = max(0.80, min(1.55, float(getattr(self, "_ui_scale", 1.0))))
        px = lambda value, floor=1: max(floor, int(round(value * scale)))

        def mix(first: str, second: str, share: float) -> str:
            a, b = QColor(first), QColor(second)
            return QColor(round(a.red() * share + b.red() * (1 - share)), round(a.green() * share + b.green() * (1 - share)),
                          round(a.blue() * share + b.blue() * (1 - share))).name()
        # NEO 174: calm frames; the accent colour marks only focus, selection and the main button.
        soft = mix(border, panel, 0.32)
        nav_bg = mix(panel, window, 0.55)
        on_accent = "#0B1220" if QColor(accent).lightness() > 140 else "#FFFFFF"

        signature = (accent, window, panel, border, text, muted, element, hover,
                     tuple(px(value) for value in range(1, 45)))
        if signature == getattr(self, '_theme_signature160', None):
            return
        self._theme_signature160 = signature

        if hasattr(self, "_root_layout"):
            self._root_layout.setContentsMargins(px(9, 6), px(9, 6), px(9, 6), px(9, 6))
            self._root_layout.setSpacing(px(8, 5))
        if hasattr(self, "tabs"):
            self.tabs.set_ui_scale(scale)

        stylesheet = f"""
            QDialog {{ background:{window}; color:{text}; font-family:'Segoe UI Variable Text','Segoe UI'; font-size:{px(12, 10)}px; }}
            QWidget {{ color:{text}; font-family:'Segoe UI Variable Text','Segoe UI'; }}
            QGroupBox {{ background:{panel}; border:1px solid {soft}; border-radius:{px(12,9)}px; margin-top:{px(16,12)}px; padding:{px(14,10)}px {px(10,7)}px {px(8,6)}px {px(10,7)}px; font-weight:600; }}
            QGroupBox::title {{ subcontrol-origin:margin; left:{px(14,10)}px; top:{px(2,1)}px; padding:0 {px(6,4)}px; color:{accent}; font-size:{px(13,11)}px; }}
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QKeySequenceEdit {{ background:{element}; color:{text}; border:1px solid {soft}; border-radius:{px(8,6)}px; padding:{px(6,4)}px {px(10,7)}px; min-height:{px(18,16)}px; selection-background-color:{accent}; selection-color:{on_accent}; }}
            QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover, QPlainTextEdit:hover {{ border-color:{border}; }}
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {{ border-color:{accent}; }}
            QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ color:{muted}; background:{panel}; }}
            QComboBox {{ padding-right:{px(30,25)}px; }}
            QComboBox::drop-down {{ subcontrol-origin:padding; subcontrol-position:top right; width:{px(28,24)}px; border:none; border-top-right-radius:{px(8,6)}px; border-bottom-right-radius:{px(8,6)}px; background:transparent; }}
            QComboBox::down-arrow {{ image:url("{down_icon}"); width:{px(11,9)}px; height:{px(11,9)}px; }}
            QComboBox QAbstractItemView {{ background:{panel}; color:{text}; border:1px solid {soft}; border-radius:{px(8,6)}px; selection-background-color:{hover}; outline:0; padding:{px(4,3)}px; }}
            QSpinBox, QDoubleSpinBox {{ padding-right:{px(36,30)}px; }}
            QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin:border; subcontrol-position:top right; width:{px(30,26)}px; border:none; background:transparent; border-top-right-radius:{px(8,6)}px; margin:{px(1,1)}px {px(1,1)}px 0 0; }}
            QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin:border; subcontrol-position:bottom right; width:{px(30,26)}px; border:none; background:transparent; border-bottom-right-radius:{px(8,6)}px; margin:0 {px(1,1)}px {px(1,1)}px 0; }}
            QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background:{hover}; }}
            QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed, QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {{ background:{panel}; }}
            QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image:url("{up_icon}"); width:{px(11,9)}px; height:{px(11,9)}px; }}
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image:url("{down_icon}"); width:{px(11,9)}px; height:{px(11,9)}px; }}
            QTableWidget, QListWidget, QTreeWidget {{ background:{window}; color:{text}; border:1px solid {soft}; border-radius:{px(8,6)}px; gridline-color:{soft}; selection-background-color:{hover}; selection-color:{text}; }}
            QListWidget::item:selected, QTreeWidget::item:selected {{ background:{hover}; color:{text}; }}
            QListWidget::item:hover, QTreeWidget::item:hover {{ background:{hover}; color:{text}; }}
            QProgressBar {{ background:{element}; color:{text}; border:1px solid {soft}; border-radius:{px(6,4)}px; min-height:{px(18,16)}px; text-align:center; }}
            QProgressBar::chunk {{ background:{mix(accent, window, 0.28)}; border-radius:{px(5,3)}px; }}
            QHeaderView::section {{ background:{element}; color:{text}; border:0; border-right:1px solid {soft}; border-bottom:1px solid {soft}; padding:{px(6,4)}px; }}
            QPushButton {{ background:{element}; color:{text}; border:1px solid {soft}; border-radius:{px(8,6)}px; padding:{px(7,5)}px {px(14,10)}px; min-height:{px(18,16)}px; }}
            QPushButton:hover {{ background:{hover}; border-color:{border}; }}
            QPushButton:pressed {{ background:{panel}; }}
            QPushButton:disabled {{ color:{muted}; background:{panel}; border-color:{soft}; }}
            QPushButton#primaryButton {{ background:{accent}; color:{on_accent}; border:1px solid {accent}; font-weight:600; }}
            QPushButton#primaryButton:hover {{ background:{mix(accent, text, 0.85)}; }}
            QPushButton#primaryButton:disabled {{ background:{panel}; color:{muted}; border-color:{soft}; }}
            QPushButton#quietButton {{ background:transparent; color:{muted}; border:1px solid transparent; }}
            QPushButton#quietButton:hover {{ color:{text}; background:{hover}; }}
            QDialogButtonBox QPushButton {{ min-width:{px(88,74)}px; }}
            QWidget#settingsFooter {{ border:none; padding-top:{px(10,8)}px; }}
            QWidget#settingsNav {{ background:{nav_bg}; border:1px solid {soft}; border-radius:{px(12,9)}px; }}
            QLineEdit#settingsSearch {{ background:{element}; border-radius:{px(9,7)}px; padding:{px(7,5)}px {px(10,7)}px; }}
            QScrollArea#settingsTabScroll {{ background:transparent; border:none; }}
            QWidget#settingsTabStrip {{ background:transparent; }}
            QPushButton#settingsTabButton {{ background:transparent; color:{muted}; text-align:left; padding:{px(8,6)}px {px(12,9)}px; border:none; border-left:{px(3,2)}px solid transparent; border-radius:{px(7,5)}px; min-height:{px(20,18)}px; }}
            QPushButton#settingsTabButton:hover {{ color:{text}; background:{hover}; }}
            QPushButton#settingsTabButton:checked {{ color:{text}; background:{element}; border-left:{px(3,2)}px solid {accent}; font-weight:600; }}
            QPushButton#settingsNavGroup {{ background:transparent; color:{text}; text-align:left; padding:{px(9,7)}px {px(12,9)}px; border:none; border-left:{px(3,2)}px solid transparent; border-radius:{px(7,5)}px; font-weight:600; min-height:{px(22,18)}px; }}
            QPushButton#settingsNavGroup:hover {{ background:{hover}; }}
            QPushButton#settingsNavGroup:checked {{ background:{element}; border-left:{px(3,2)}px solid {accent}; }}
            QTabBar#settingsSubTabs::tab {{ background:transparent; color:{muted}; padding:{px(7,5)}px {px(14,10)}px; border:none; border-bottom:{px(2,2)}px solid transparent; }}
            QTabBar#settingsSubTabs::tab:hover {{ color:{text}; }}
            QTabBar#settingsSubTabs::tab:selected {{ color:{text}; border-bottom:{px(2,2)}px solid {accent}; font-weight:600; }}
            QLabel#settingsPageTitle {{ font-size:{px(20,16)}px; font-weight:600; color:{text}; padding:0 {px(2,1)}px; }}
            QLabel#settingsPageSubtitle {{ color:{muted}; font-size:{px(12,10)}px; padding:0 {px(2,1)}px; }}
            QStackedWidget#settingsStack {{ background:transparent; border:none; }}
            *[searchHit="true"] {{ background:{hover}; border-radius:{px(6,4)}px; }}
            QLabel#help {{ color:{muted}; font-size:{px(11,9)}px; }}
            QScrollArea {{ border:none; background:transparent; }}
            QScrollArea > QWidget > QWidget {{ background:transparent; }}
            QScrollBar:vertical {{ background:transparent; width:{px(8,7)}px; margin:0px; }}
            QScrollBar::handle:vertical {{ background:{soft}; border-radius:{px(4,3)}px; min-height:{px(26,22)}px; margin:0px; }}
            QScrollBar::handle:vertical:hover {{ background:{muted}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; background:transparent; border:none; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; border:none; }}
            QScrollBar:horizontal {{ background:transparent; height:{px(7,6)}px; margin:0px; }}
            QScrollBar::handle:horizontal {{ background:{soft}; border-radius:{px(3,2)}px; min-width:{px(28,24)}px; margin:0px; }}
            QScrollBar::handle:horizontal:hover {{ background:{muted}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; background:transparent; border:none; }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background:transparent; border:none; }}
            QCheckBox {{ spacing:{px(10,7)}px; padding:{px(4,3)}px 0; }}
            QCheckBox::indicator {{ width:{px(18,15)}px; height:{px(18,15)}px; border:1px solid {border}; border-radius:{px(5,4)}px; background:{element}; }}
            QCheckBox::indicator:hover {{ border-color:{accent}; }}
            QCheckBox::indicator:checked {{ background:{accent}; border-color:{accent}; image:url("{check_icon}"); }}
            QCheckBox::indicator:disabled {{ background:{panel}; border-color:{soft}; }}
            QCheckBox:disabled, QRadioButton:disabled, QLabel:disabled {{ color:{muted}; }}
            QRadioButton {{ spacing:{px(10,7)}px; padding:{px(4,3)}px 0; }}
            QRadioButton::indicator {{ width:{px(16,14)}px; height:{px(16,14)}px; border:1px solid {border}; border-radius:{px(8,7)}px; background:{element}; }}
            QRadioButton::indicator:hover {{ border-color:{accent}; }}
            QRadioButton::indicator:checked {{ background:{accent}; border:{px(4,3)}px solid {element}; outline:1px solid {accent}; }}
            QPushButton:focus, QToolButton:focus {{ border-color:{accent}; }}
            QPushButton:checked {{ background:{hover}; border-color:{accent}; }}
            QToolButton {{ background:{element}; color:{text}; border:1px solid {soft}; border-radius:{px(8,6)}px; padding:{px(5,4)}px {px(8,6)}px; }}
            QToolButton:hover {{ background:{hover}; border-color:{border}; }}
            QToolButton:pressed {{ background:{panel}; }}
            QToolButton:disabled {{ color:{muted}; background:{panel}; }}
            QKeySequenceEdit:hover {{ border-color:{border}; }}
            QKeySequenceEdit:focus {{ border-color:{accent}; }}
            QPlainTextEdit:disabled, QKeySequenceEdit:disabled {{ color:{muted}; background:{panel}; }}
            QSlider::groove:horizontal {{ height:{px(4,3)}px; background:{soft}; border-radius:{px(2,2)}px; }}
            QSlider::sub-page:horizontal {{ background:{mix(accent, window, 0.28)}; border-radius:{px(2,2)}px; }}
            QSlider::handle:horizontal {{ width:{px(16,14)}px; margin:-{px(6,5)}px 0; border-radius:{px(8,7)}px; background:{accent}; border:1px solid {accent}; }}
            QSlider::handle:horizontal:disabled {{ background:{muted}; border-color:{muted}; }}
            QTabWidget::pane {{ border:1px solid {soft}; border-radius:{px(8,6)}px; background:{panel}; top:-1px; }}
            QTabBar::tab {{ background:transparent; color:{muted}; padding:{px(7,5)}px {px(14,10)}px; border:none; border-bottom:{px(2,2)}px solid transparent; }}
            QTabBar::tab:hover {{ color:{text}; }}
            QTabBar::tab:selected {{ color:{text}; border-bottom:{px(2,2)}px solid {accent}; font-weight:600; }}
            QToolTip {{ background:{panel}; color:{text}; border:1px solid {soft}; border-radius:{px(6,4)}px; padding:{px(5,4)}px {px(8,6)}px; }}
            """
        if stylesheet != getattr(self, '_applied_stylesheet160', None):
            self._applied_stylesheet160 = stylesheet
            self.setStyleSheet(stylesheet)


    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # Window resize is layout, not zoom. Rebuilding the complete QSS here
        # generated a style storm on every drag pause and blocked input/paint.
        # Tabs/rows continue to resize synchronously; nothing waits for mouse-up.
        # _ui_scale remains stable and Qt applies actual screen/DPR changes.

    def _window_color_changed(self) -> None:
        # Only stage the edit. Theme changes are committed in _save().
        self._mark_window_custom()

    @staticmethod
    def _scroll(content: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setWidget(content)
        _responsive178(content)
        return area

    @staticmethod
    def _help(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("help")
        label.setWordWrap(True)
        return label

    @classmethod
    def _add_field(cls, form: QFormLayout, title: str, widget: QWidget, help_text: str = "") -> None:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(3)
        layout.addWidget(widget)
        if help_text:
            layout.addWidget(cls._help(help_text))
        form.addRow(title, box)
        # NEO 182/183: the row's label names the editor itself (screen readers, label click focus).
        # A composite row (a line edit with a checkbox beside it, a colour field…) names its first
        # editable child; the names are translated with the rest of the window.
        target = widget
        if not isinstance(widget, (QKeySequenceEdit, QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox, _QtCheckBox)):
            # UI185-HOTKEY-A11Y-01: a hotkey editor itself takes the focus, not its private line edit.
            sequences = [w for w in widget.findChildren(QKeySequenceEdit)]
            editors = sequences or [w for w in widget.findChildren(QWidget) if isinstance(w, (QLineEdit, QPlainTextEdit, QComboBox, QAbstractSpinBox))]
            if editors:
                target = editors[0]
        label = form.labelForField(box)
        if isinstance(label, QLabel):
            label.setBuddy(target)
        name = str(title or "").rstrip(": ")
        for item in {id(widget): widget, id(target): target}.values():
            if not item.accessibleName():
                item.setProperty("i18n_a11y_name_source", name)
                item.setAccessibleName(name)
            if help_text and not item.accessibleDescription():
                item.setProperty("i18n_a11y_desc_source", help_text)
                item.setAccessibleDescription(help_text)

    def _bind_collapsible_group(self, group: QGroupBox, toggles: list[QCheckBox], extra_widgets: list[QWidget] | None = None) -> None:
        """Collapse optional integration details while keeping enable toggles visible."""
        keep = {id(x) for x in toggles if x is not None}
        extras = list(extra_widgets or [])
        layout = group.layout()
        if layout is None:
            return

        def iter_widgets(layout_obj):
            for index in range(layout_obj.count()):
                item = layout_obj.itemAt(index)
                widget = item.widget()
                if widget is not None:
                    yield widget
                child_layout = item.layout()
                if child_layout is not None:
                    yield from iter_widgets(child_layout)

        widgets = list(dict.fromkeys(iter_widgets(layout)))

        def apply(*_args) -> None:
            expanded = any(bool(toggle.isChecked()) for toggle in toggles)
            for widget in widgets:
                if id(widget) in keep:
                    widget.setVisible(True)
                else:
                    widget.setVisible(expanded)
            for widget in extras:
                widget.setVisible(expanded)
            group.setProperty("neoCollapsed", not expanded)
            source = "Интеграция выключена — параметры свёрнуты." if not expanded else ""
            group.setProperty("i18n_tooltip_source", source)
            group.setToolTip(tr(source, language_code(self.config)))

        for toggle in toggles:
            toggle.toggled.connect(apply)
        timer = QTimer(group)
        timer.setSingleShot(True)
        timer.timeout.connect(apply)
        timer.start(0)

    @staticmethod
    def _spin(minimum: int, maximum: int, value: int, suffix: str = "", step: int = 1) -> QSpinBox:
        box = QSpinBox()
        box.setRange(minimum, maximum)
        box.setSingleStep(step)
        box.setValue(value)
        box.setSuffix(suffix)
        return box

    @staticmethod
    def _percent_spin(value: int) -> QSpinBox:
        box = SettingsDialog._spin(0, 100, int(value), "%", 5)
        box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        box.setProperty("flatPercent", False)
        box.setAccelerated(True)
        box.setToolTip("Введите 0–100% или используйте стрелки ↑/↓. 0% = полностью прозрачно.")
        return box

    def _models_tab(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)

        connection = QGroupBox("Подключение к Ollama")
        form = QFormLayout(connection)
        self.ollama_url = QLineEdit(str(self.config.get("ollama.base_url", "http://localhost:11434")))
        self.vision_model = QComboBox()
        self.vision_model.setEditable(True)
        self.vision_model.addItem(str(self.config.get("ollama.vision_model", "qwen3.5:4b")))
        model_buttons = QWidget()
        model_buttons_layout = QHBoxLayout(model_buttons)
        model_buttons_layout.setContentsMargins(0, 0, 0, 0)
        load_button = self.model_list_button184 = QPushButton("Показать установленные модели")
        load_button.clicked.connect(self._load_models)
        test_button = self.model_server_check_button184 = QPushButton("Проверить Ollama")
        test_button.clicked.connect(self._test_ollama)
        model_buttons_layout.addWidget(load_button)
        model_buttons_layout.addWidget(test_button)
        self._add_field(form, "Адрес", self.ollama_url, "Обычно менять не нужно. Стандартный адрес Ollama: http://localhost:11434")
        self._add_field(form, "Модель изображения", self.vision_model, "Нужна мультимодальная модель, умеющая принимать скриншоты. Qwen3.5:4b поддерживает и текст, и изображения, поэтому его можно использовать для обоих полей.")
        form.addRow(model_buttons)
        root.addWidget(connection)

        performance = QGroupBox("Качество и нагрузка")
        perf = QFormLayout(performance)
        self.context = self._spin(1024, 32768, int(self.config.get("ollama.num_ctx", 4096)), " токенов", 1024)
        self.creativity = self._spin(0, 100, int(round(float(self.config.get("ollama.temperature", 0.2)) * 100)), "%", 5)
        self.max_output = self._spin(64, 8192, int(self.config.get("ollama.num_predict", 512)), " токенов", 64)
        self.timeout = self._spin(30, 900, int(self.config.get("ollama.request_timeout_seconds", 75)), " сек", 15)
        self.first_token_timeout = self._spin(5, 180, int(self.config.get("ollama.first_token_timeout_seconds", 20)), " сек", 5)
        self.preload_model = QCheckBox("Подготавливать модель при запуске")
        self.preload_model.setChecked(False)
        self.preload_model.setEnabled(False)
        self.character_streaming_enabled = QCheckBox("Показывать ответы постепенно, посимвольно")
        self.character_streaming_enabled.setChecked(bool(self.config.get("ui.character_streaming_enabled", True)))
        self.typewriter_speed = self._spin(1, 120, int(self.config.get("ui.typewriter_interval_ms", 8) or 8), " мс/симв.", 1)
        self.keep_alive = QComboBox()
        keep_options = [
            ("Выгружать сразу — меньше VRAM, ответ запускается дольше", "0"),
        ]
        for label, data in keep_options:
            self.keep_alive.addItem(label, data)
        current_keep = str(self.config.get("ollama.vision_keep_alive", "0"))
        index = self.keep_alive.findData(current_keep)
        self.keep_alive.setCurrentIndex(max(0, index))
        # The image-only runtime policy unloads after each request. Do not offer
        # retention choices that migration/runtime would silently override.
        self.keep_alive.setEnabled(False)
        self._add_field(perf, dt("vision_context", "ru"), self.context)
        self._add_field(perf, "Свобода ответа модели", self.creativity, "Действует на запросы к модели без собственных параметров. Анализ изображений использует фиксированную низкую вариативность, чтобы описание было точным.")
        self._add_field(perf, "Максимальная длина ответа", self.max_output, "Общий предел для запросов к модели без собственного лимита. У анализа изображений свои пределы: карточки экрана — по профилю производительности, помощь по экрану — короткий ответ, остальные снимки — отдельный лимит анализа изображений.")
        self._add_field(perf, "Максимальное ожидание", self.timeout, "Общий предел ожидания запроса. Меньше — зависание обнаружится быстрее.")
        self._add_field(perf, "Ожидание первого слова", self.first_token_timeout, "Сколько ждать начала ответа. Для прогретой 4B-модели обычно достаточно 10–20 секунд. Меньше — быстрее обнаруживается зависание.")
        self.preload_model.hide()
        perf.addRow("", self._help(dt("vision_only", "ru")))
        perf.addRow(self.character_streaming_enabled)
        self._add_field(perf, "Скорость посимвольного вывода", self.typewriter_speed, "Интервал между символами. Меньше — текст появляется быстрее. Настройка применяется к готовым ответам API/tools и к потоковым ответам без изменения скорости генерации самой модели.")
        self._add_field(perf, "Хранение модели в памяти", self.keep_alive, "Обычно модель изображений выгружается сразу после анализа. Исключение — карточки экрана: профиль производительности может оставить модель в памяти для быстрых повторных ответов (Низкий — нет, Сбалансированный — 2 мин, Быстрый — 10 мин, Максимальный — 30 мин).")
        root.addWidget(performance)
        self._append_computer_controls(root)
        root.addStretch(1)
        return self._scroll(content)

    def _behavior_tab(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)

        # NEO 187 HREQ-008: the non-working «стиль общения» control is gone; the
        # group only holds names and personal address, and is titled that way.
        group = QGroupBox("Имена и обращение")
        form = QFormLayout(group)
        self.assistant_name = QLineEdit(str(self.config.get("assistant_name", DEFAULT_ASSISTANT_NAME) or DEFAULT_ASSISTANT_NAME))
        self.user_name = QLineEdit(str(self.config.get("user_name", DEFAULT_USER_NAME) or DEFAULT_USER_NAME))
        self.user_name.setMaxLength(80)
        self.user_name_enabled = QCheckBox("Использовать имя в каждом ответе")
        self.user_name_enabled.setChecked(bool(self.config.get("user_name_enabled", True)))
        user_name_row = QWidget()
        user_name_layout = QHBoxLayout(user_name_row)
        user_name_layout.setContentsMargins(0, 0, 0, 0)
        user_name_layout.setSpacing(10)
        user_name_layout.addWidget(self.user_name, 1)
        user_name_layout.addWidget(self.user_name_enabled)
        self._add_field(form, "Имя", self.assistant_name, "Так робот подписывает свои сообщения.")
        self._add_field(
            form,
            "Имя пользователя",
            user_name_row,
            "Имя для персонального обращения. По умолчанию поле пустое. Галочка справа включает обращение по имени в каждом ответе; при выключении имя не используется. Само имя сохраняется как введено и не переводится.",
        )
        root.addWidget(group)

        voice_behavior = QGroupBox("Поведение голосового диалога")
        voice_form = QFormLayout(voice_behavior)
        self.voice_interaction_mode = QComboBox()
        self.voice_interaction_mode.addItem(
            "Непрерывный диалог — после wake word слушать до стоп-фразы",
            "continuous",
        )
        self.voice_interaction_mode.addItem(
            "Одна команда — после каждого ответа снова ждать wake word",
            "wake_each_command",
        )
        configured_mode = str(self.config.get("voice.interaction_mode", "") or "").strip()
        if configured_mode not in {"continuous", "wake_each_command"}:
            configured_mode = "continuous" if bool(self.config.get("voice.continuous_after_wake", False)) else "wake_each_command"
        mode_index = self.voice_interaction_mode.findData(configured_mode)
        self.voice_interaction_mode.setCurrentIndex(max(0, mode_index))
        self._add_field(
            voice_form,
            "Режим после wake word",
            self.voice_interaction_mode,
            "Непрерывный режим подходит для разговора серией команд и заканчивается стоп-фразой. Режим одной команды работает как классический ассистент: wake word → одна команда/ответ → снова ожидание wake word.",
        )
        self.stop_phrases_edit = QPlainTextEdit()
        self.stop_phrases_edit.setFixedHeight(92)
        self.stop_phrases_edit.setPlainText("\n".join(str(x) for x in (self.config.get("voice.stop_phrases", []) or [])))
        self._add_field(
            voice_form,
            "Стоп-фразы непрерывного режима",
            self.stop_phrases_edit,
            "По одной фразе на строку. Они завершают только непрерывный голосовой сеанс; в режиме одной команды не требуются.",
        )
        self.interrupt_on_wake = QCheckBox("Wake word во время ответа/операции мгновенно прерывает текущую задачу")
        self.interrupt_on_wake.setChecked(bool(self.config.get("voice.interrupt_on_wake_while_busy", True)))
        self.interrupt_without_wake = QCheckBox("Разрешить короткие «стоп/отмена» как barge-in без повторного wake word")
        self.interrupt_without_wake.setChecked(bool(self.config.get("voice.interrupt_without_wake", True)))
        self.duck_while_listening = QCheckBox("Приглушать остальные приложения на время голосового взаимодействия")
        self.duck_while_listening.setChecked(bool(self.config.get("voice.duck_system_while_listening", False)))
        self.listening_duck = self._spin(0, 100, int(self.config.get("voice.listening_duck_percent", 18)), "%")
        voice_form.addRow(self.interrupt_on_wake)
        voice_form.addRow(self.interrupt_without_wake)
        voice_form.addRow(self.duck_while_listening)
        self._add_field(voice_form, "Громкость фона при прослушивании", self.listening_duck, "0% — полностью заглушить другие аудиосессии; 15–25% обычно достаточно. В режиме одной команды громкость восстанавливается после действия/ответа; в непрерывном диалоге — после стоп-фразы.")
        root.addWidget(voice_behavior)
        root.addStretch(1)
        return self._scroll(content)

    def _streaming_tab184(self) -> QWidget:
        from jarvis.ui.streaming_settings184 import StreamingSettingsPage
        self.streaming_page = StreamingSettingsPage(self.config, self)
        self.finished.connect(self.streaming_page.close_operations)
        return self._scroll(self.streaming_page)

    def _response_voice_tab(self) -> QWidget:
        from jarvis.ui.response_voice_page import ResponseVoicePage
        owner = self.parent()
        ensure = getattr(owner, '_ensure_response_voice', None)
        service = ensure() if callable(ensure) else None
        self.response_voice_page = ResponseVoicePage(self.config, self, service=service)
        self.finished.connect(self.response_voice_page.close_operations)
        return self._scroll(self.response_voice_page)

    def _voice_tab(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)

        microphone = QGroupBox("Распознавание речи")
        form = QFormLayout(microphone)
        self.voice_enabled = QCheckBox("Разрешить голосовой ввод")
        self.voice_enabled.setChecked(bool(self.config.get("voice.enabled", True)))
        self.adaptive_decode_retry = QCheckBox("Повторно распознавать только сомнительные фразы")
        self.adaptive_decode_retry.setChecked(bool(self.config.get("voice.adaptive_decode_retry", True)))
        self.input_device = QComboBox()
        # HIST-059: a user pick bumps the draft revision; a late device list
        # restores only the selection of its own revision.
        self._mic_draft_rev188 = 0
        self.input_device.activated.connect(lambda _index: setattr(self, "_mic_draft_rev188", self._mic_draft_rev188 + 1))
        # NEO 183: service captions in the interface language; device names stay as Windows reports them
        self.input_device.addItem(self._t("Системный микрофон по умолчанию"), "")
        self.input_device.setItemData(self.input_device.count() - 1, "Системный микрофон по умолчанию", int(Qt.UserRole) + 77)
        selected_input = str(self.config.get("voice.input_device", "") or "")
        if selected_input:
            self._add_current_device186(selected_input)
            self.input_device.setCurrentIndex(1)
        self._microphone_worker: FunctionWorker | None = None
        refresh_mics = QPushButton("Обновить список микрофонов")
        refresh_mics.clicked.connect(self._refresh_microphones_async)
        self.stt_model = QComboBox()
        for label, value in [("tiny — самый быстрый", "tiny"), ("base", "base"), ("small — рекомендуется", "small"), ("medium — точнее, тяжелее", "medium"), ("large-v3-turbo — GPU", "turbo")]:
            self.stt_model.addItem(label, value)
        idx = self.stt_model.findData(str(self.config.get("voice.stt_model", "small")))
        self.stt_model.setCurrentIndex(max(0, idx))
        self.sensitivity = self._spin(1, 100, int(self.config.get("voice.sensitivity", 90)), "%")
        self.noise_rejection = self._spin(0, 100, int(self.config.get("voice.noise_rejection_percent", 55)), "%")
        self.start_timeout = self._spin(2, 30, int(self.config.get("voice.start_timeout_seconds", 7)), " сек")
        self.silence_seconds = QDoubleSpinBox()
        from jarvis.runtime.numeric_schema186 import configure_spinbox as _numeric186
        _numeric186(self.silence_seconds, "voice.silence_seconds")   # SETTINGS185-RANGE-01
        self.silence_seconds.setSuffix(" сек")
        self.silence_seconds.setValue(float(self.config.get("voice.silence_seconds", 1.25)))
        self.max_record = self._spin(5, 90, int(self.config.get("voice.max_record_seconds", 16)), " сек", 1)
        self.live_partial = QDoubleSpinBox()
        self.live_partial.setRange(1.2, 4.0)
        self.live_partial.setSingleStep(0.1)
        self.live_partial.setDecimals(1)
        self.live_partial.setSuffix(" сек")
        self.live_partial.setValue(float(self.config.get("voice.live_partial_seconds", 2.0)))
        form.addRow(self.voice_enabled)
        form.addRow(self.adaptive_decode_retry)
        self.spoken_spelling = QCheckBox("Исправлять неверно услышанные слова («превет» → «привет»)")
        self.spoken_spelling.setChecked(bool(self.config.get("voice.spoken_spelling_enabled", True)))
        form.addRow(self.spoken_spelling)
        form.addRow("", self._help("Адаптивный повтор не запускает Whisper дважды постоянно: второй проход выполняется только для подозрительной расшифровки (очень низкая уверенность, неожиданная письменность или слишком короткий текст для длинной записи)."))
        self._add_field(form, "Устройство ввода", self.input_device, "PortAudio работает в отдельном subprocess и автоматически перебирает безопасные Windows-аудиохосты при ошибке. Сначала используйте системный микрофон по умолчанию; конкретное устройство выбирайте только при необходимости.")
        form.addRow(refresh_mics)
        self._add_field(form, "Модель распознавания", self.stt_model, "Для команд рекомендуется small: заметно точнее tiny и лучше переносит смешанный RU+EN. При хорошей NVIDIA GPU можно выбрать turbo.")
        self._add_field(form, "Чувствительность микрофона", self.sensitivity, "Больше — легче услышать тихую речь. Для настольного микрофона обычно 88–96%. Если речь тихая — повышайте; если фон легко запускает запись — понижайте.")
        self._add_field(form, "Отсечение фонового шума", self.noise_rejection, "Фильтрует короткие щелчки клавиатуры/мыши, дыхание и постоянный шум до распознавания. 45–65% — рекомендуемый диапазон; слишком высокое значение может обрезать очень тихую речь.")
        self._add_field(form, "Ожидание начала речи", self.start_timeout, "Сколько ждать, пока вы начнёте говорить после нажатия микрофона.")
        self._add_field(form, "Пауза завершает команду", self.silence_seconds, "Рекомендуется 1,2–1,3 секунды: это лучше сохраняет тихие окончания фраз. При низком отсечении шума внутренний VAD Whisper дополнительно не режет команду.")
        self._add_field(form, "Максимальная длина команды", self.max_record, "Запись остановится автоматически, даже если вокруг остаётся шум.")
        # 186: Save always stores live_partial_max_updates=0 (stability policy), so
        # the interval has no effect; the field says so instead of recommending a value.
        self.live_partial.setEnabled(False)
        self._add_field(form, "Предварительный текст", self.live_partial, "Предварительное распознавание длинных команд отключено для стабильности, поэтому интервал сейчас не используется. Команда распознаётся целиком после паузы.")
        form.addRow("", self._help("После окончания речи всегда выполняется отдельное качественное распознавание. Faster-whisper работает в изолированном процессе и автоматически перезапускается при нативном сбое."))
        root.addWidget(microphone)

        wake = QGroupBox("Фраза активации")
        wake_form = QFormLayout(wake)
        self.wake_enabled = QCheckBox("Слушать фразу активации в фоне")
        self.wake_enabled.setChecked(bool(self.config.get("wake_word.enabled", False)))
        self.wake_phrase = QLineEdit(str(self.config.get("wake_word.phrase", "")))
        self.wake_aliases_edit = QPlainTextEdit()
        self.wake_aliases_edit.setTabChangesFocus(True)      # NEO 183: Tab moves on (one phrase per line, no tabs)
        self.wake_aliases_edit.setFixedHeight(118)
        self.wake_aliases_edit.setPlaceholderText("Один вариант распознавания на строку")
        self.wake_aliases_edit.setPlainText("\n".join(str(x) for x in (self.config.get("wake_word.aliases", []) or [])))
        self.wake_model = QComboBox()
        self.wake_model.addItem("base — точнее, но заметно тяжелее для фонового CPU", "base")
        self.wake_model.addItem("small — максимальная точность, самый тяжёлый вариант", "small")
        self.wake_model.addItem("tiny — рекомендуется: минимальная нагрузка + расширенные варианты wake word", "tiny")
        idx = self.wake_model.findData(str(self.config.get("wake_word.stt_model", "tiny")))
        self.wake_model.setCurrentIndex(max(0, idx))
        self.wake_chunk = QDoubleSpinBox()
        from jarvis.runtime.numeric_schema186 import configure_spinbox as _numeric186
        _numeric186(self.wake_chunk, "wake_word.chunk_seconds")
        self.wake_chunk.setSuffix(" сек")
        self.wake_chunk.setValue(float(self.config.get("wake_word.chunk_seconds", 2.2)))
        wake_form.addRow(self.wake_enabled)
        self._add_field(wake_form, "Основной wake word", self.wake_phrase, "Главная фраза активации. После неё можно сразу произнести команду.")
        self._add_field(
            wake_form,
            "Варианты wake word",
            self.wake_aliases_edit,
            "По одному варианту на строку. Добавляйте сюда альтернативные произношения и ошибки ASR. Список хранится в настройках, а не в Python-коде.",
        )
        self._add_field(wake_form, "Модель", self.wake_model, "Tiny рекомендуется для постоянного фонового wake word: он заметно легче, а расширенный список вариантов компенсирует типичные ошибки короткого имени. Base/Small оставлены для ручного выбора.")
        self._add_field(wake_form, "Максимальная длина фразы", self.wake_chunk, "Короткое окно быстрее отдаёт wake word на распознавание. Рекомендуется 2,2–2,7 секунды.")
        self.wake_scan_interval = QDoubleSpinBox()
        _numeric186(self.wake_scan_interval, "wake_word.silence_seconds")
        self.wake_scan_interval.setSuffix(" сек")
        self.wake_scan_interval.setValue(float(self.config.get("wake_word.silence_seconds", 0.30)))
        self._add_field(wake_form, "Пауза завершает wake-фразу", self.wake_scan_interval, "После этой короткой паузы имя сразу отправляется на распознавание. Рекомендуется 0,30–0,40 секунды.")
        self.wake_match_sensitivity = QSpinBox()
        self.wake_match_sensitivity.setRange(82, 96)
        self.wake_match_sensitivity.setSingleStep(1)
        self.wake_match_sensitivity.setSuffix(" %")
        self.wake_match_sensitivity.setValue(round(float(self.config.get("wake_word.fuzzy_threshold", 0.88)) * 100))
        self._add_field(wake_form, "Порог совпадения", self.wake_match_sensitivity, "Порог намеренно строгий: ниже 82% значение не опускается, чтобы обычные слова и шум не активировали ассистента. Рекомендуется 88–92%.")
        root.addWidget(wake)

        behavior_heading = QLabel("Поведение ассистента")
        behavior_heading.setStyleSheet("font-size:15px; font-weight:700; padding:8px 2px 0 2px;")
        root.addWidget(behavior_heading)
        root.addWidget(self._help("Параметры бывшего раздела «Поведение» перенесены сюда: характер, режим голосового диалога, barge-in и приглушение звука теперь находятся рядом с микрофоном и wake word."))
        self._adopt_scroll_sections(root, self._behavior_tab())
        # 186 (settings TZ 5.1): the personal dictionary lives next to the microphone.
        self._adopt_scroll_sections(root, self._dictionary_tab())
        root.addStretch(1)
        return self._scroll(content)

    def _append_computer_controls(self, root: QVBoxLayout) -> None:
        group = QGroupBox("Управление компьютером")
        form = QFormLayout(group)
        self.screen_aware_actions = QCheckBox("Перед действиями с файлами и окнами фиксировать актуальный экран и использовать визуальный контекст")
        self.screen_aware_actions.setChecked(bool(self.config.get("desktop.screen_aware_actions", True)))
        self.vision_ambiguous = QCheckBox("Использовать Vision для неоднозначных команд: «это», «там», «первую ссылку»")
        self.vision_ambiguous.setChecked(bool(self.config.get("desktop.vision_for_ambiguous_actions", True)))
        form.addRow(self.screen_aware_actions)
        form.addRow(self.vision_ambiguous)
        root.addWidget(group)
        root.addWidget(self._help("Экран и Vision используются только для файлов, папок, окон, вкладок и элементов интерфейса. Музыка, Discord, заметки, чек-листы, разговор и обычные команды не запускают фоновый снимок экрана."))

    def _desktop_tab(self) -> QWidget:
        # Compatibility helper for older code; the visible «Компьютер» tab was
        # removed in v4.0.15 and these controls live inside the «ИИ» tab.
        content = QWidget()
        root = QVBoxLayout(content)
        self._append_computer_controls(root)
        root.addStretch(1)
        return self._scroll(content)

    def _media_tab(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)

        general_group = QGroupBox("Медиа · общие настройки")
        general_form = QFormLayout(general_group)
        self.quick_media_commands = QCheckBox("Быстрые медиакоманды без wake word (дальше / скип / стоп / пауза / назад / лайк / дизлайк)")
        self.quick_media_commands.setChecked(bool(self.config.get("wake_word.quick_commands_enabled", True)))
        general_form.addRow(self.quick_media_commands)
        general_form.addRow("", self._help("Все быстрые голосовые команды транспорта собраны в разделе «Медиа». Если выключено, те же команды требуют wake word."))
        self.media_now_playing_notifications = QCheckBox("Показывать в диалоге каждый новый трек")
        self.media_now_playing_notifications.setChecked(bool(self.config.get("ui.media_now_playing_notifications", True)))
        general_form.addRow(self.media_now_playing_notifications)
        self.quick_media_volume_commands = QCheckBox("Быстрые голосовые команды громкости «громче / тише»")
        self.quick_media_volume_commands.setChecked(bool(self.config.get("voice.quick_media_volume_enabled", True)))
        self.quick_media_repeat_shuffle_commands = QCheckBox("Быстрые голосовые команды повтора и перемешивания")
        self.quick_media_repeat_shuffle_commands.setChecked(bool(self.config.get("voice.quick_media_repeat_shuffle_enabled", True)))
        general_form.addRow(self.quick_media_volume_commands)
        general_form.addRow(self.quick_media_repeat_shuffle_commands)
        general_form.addRow(self._help("Команды громкости управляют только локальной музыкой Яндекс/Spotify/YouTube Audio и никогда не меняют громкость Discord. Повтор/перемешивание также работают без wake word, если включены здесь."))
        general_form.addRow(self._help(
            "Выберите один локальный музыкальный сервис. Яндекс Музыка доступна только при русском языке интерфейса; Spotify доступен на всех языках. Discord и YouTube настраиваются отдельно."
        ))
        root.addWidget(general_group)


        # --- Yandex Music -------------------------------------------------
        self.yandex_group = QGroupBox("Яндекс Музыка")
        yandex_group = self.yandex_group
        yandex_form = QFormLayout(yandex_group)
        self.yandex_integration_enabled = QCheckBox("Активировать Яндекс Музыку")
        self.yandex_integration_enabled.setChecked(bool(self.config.get("integrations.yandex_music.enabled", True)))
        self.yandex_api_enabled = QCheckBox("Использовать API для поиска метаданных и действий аккаунта")
        self.yandex_api_enabled.setChecked(bool(self.config.get("integrations.yandex_music.api_enabled", True)))
        self.yandex_background_playback = QCheckBox("Фоновое воспроизведение NEO без открытия окна Яндекс Музыки")
        self.yandex_background_playback.setChecked(bool(self.config.get("integrations.yandex_music.background_playback", True)))
        self.yandex_background_volume = self._spin(0, 100, int(self.config.get("integrations.yandex_music.background_volume_percent", 100) or 100), "%", 5)
        self.yandex_never_steal_input = QCheckBox("Не перехватывать мышь/клавиатуру и не менять активное окно")
        self.yandex_never_steal_input.setChecked(True)
        self.yandex_ui_fallback = QCheckBox("UI fallback отключён архитектурой")
        self.yandex_ui_fallback.setChecked(False); self.yandex_ui_fallback.setEnabled(False)
        self.yandex_protocol_fallback = QCheckBox("Protocol fallback отключён архитектурой")
        self.yandex_protocol_fallback.setChecked(False); self.yandex_protocol_fallback.setEnabled(False)
        self.yandex_api_token = QLineEdit(); self.yandex_api_token.setEchoMode(QLineEdit.Password)
        self._existing_yandex_token_encrypted = str(self.config.get("integrations.yandex_music.token_encrypted", "") or "")
        self.yandex_api_token.setPlaceholderText(
            "OAuth-токен уже сохранён — оставьте пустым, чтобы не менять"
            if self._existing_yandex_token_encrypted else "Необязательно: OAuth-токен Яндекс Музыки"
        )
        self.yandex_connect_button = QPushButton("Переподключить Яндекс Музыку" if self._existing_yandex_token_encrypted else "Подключить Яндекс Музыку")
        self.yandex_connect_button.clicked.connect(self._start_yandex_music_auth)
        self.yandex_open_auth_button = QPushButton("Открыть страницу авторизации"); self.yandex_open_auth_button.setVisible(False)
        self.yandex_open_auth_button.clicked.connect(self._open_yandex_auth_page)
        self._yandex_auth_url = ""; self._yandex_auth_worker = None
        self.yandex_connect_status = QLabel("Статус: подключено" if self._existing_yandex_token_encrypted else "Статус: не подключено")
        self.yandex_connect_status.setWordWrap(True); self.yandex_connect_status.setTextInteractionFlags(Qt.TextSelectableByMouse); self.yandex_connect_status.setObjectName("help")
        self.yandex_auth_code = QLineEdit(); self.yandex_auth_code.setReadOnly(True); self.yandex_auth_code.setPlaceholderText("Код авторизации появится здесь"); self.yandex_auth_code.setVisible(False)
        self.yandex_copy_code_button = QPushButton("Копировать код"); self.yandex_copy_code_button.setVisible(False)
        self.yandex_copy_code_button.clicked.connect(lambda: self._copy_code_field(self.yandex_auth_code, "Код авторизации скопирован"))
        auth_row = QWidget(); auth_layout = QHBoxLayout(auth_row); auth_layout.setContentsMargins(0, 0, 0, 0); auth_layout.setSpacing(8)
        auth_layout.addWidget(self.yandex_connect_button); auth_layout.addWidget(self.yandex_open_auth_button); auth_layout.addStretch(1)
        yandex_form.addRow(self.yandex_integration_enabled)
        yandex_form.addRow(self.yandex_api_enabled); yandex_form.addRow(self.yandex_background_playback)
        self._add_field(yandex_form, "Громкость фонового плеера", self.yandex_background_volume, "Отдельная громкость скрытого плеера NEO. Она не меняет общую громкость Windows.")
        yandex_form.addRow("Подключение аккаунта", auth_row); yandex_form.addRow("", self.yandex_connect_status)
        yandex_code_row = QWidget(); yandex_code_layout = QHBoxLayout(yandex_code_row); yandex_code_layout.setContentsMargins(0, 0, 0, 0); yandex_code_layout.setSpacing(6)
        yandex_code_layout.addWidget(self.yandex_auth_code, 1); yandex_code_layout.addWidget(self.yandex_copy_code_button)
        yandex_form.addRow("Код авторизации", yandex_code_row)
        self._add_field(yandex_form, "API token", self.yandex_api_token, "Ручной резервный способ. Токен хранится через Windows DPAPI.")
        yandex_form.addRow(self._help("Если включить Spotify, этот тумблер выключится автоматически: NEO допускает только один активный музыкальный провайдер."))
        root.addWidget(yandex_group); self._bind_collapsible_group(yandex_group, [self.yandex_integration_enabled])
        def _sync_yandex_language_visibility(*_args):
            # Account capability is independent of the assistant UI language.
            yandex_group.setVisible(True)
        self._sync_yandex_language_visibility = _sync_yandex_language_visibility
        _sync_yandex_language_visibility()

        # --- Spotify ------------------------------------------------------
        spotify_group = QGroupBox("Spotify")
        spotify_form = QFormLayout(spotify_group)
        self.spotify_integration_enabled = QCheckBox("Активировать Spotify")
        self.spotify_integration_enabled.setChecked(bool(self.config.get("integrations.spotify.enabled", False)))
        self._existing_spotify_client_id_encrypted = str(self.config.get("integrations.spotify.client_id_encrypted", "") or "")
        self._existing_spotify_access_token_encrypted = str(self.config.get("integrations.spotify.access_token_encrypted", "") or "")
        self._existing_spotify_refresh_token_encrypted = str(self.config.get("integrations.spotify.refresh_token_encrypted", "") or "")
        self.spotify_client_id = QLineEdit(unprotect_secret(self._existing_spotify_client_id_encrypted) if self._existing_spotify_client_id_encrypted else "")
        self.spotify_client_id.setPlaceholderText("Client ID из Spotify Developer Dashboard")
        self.spotify_redirect_uri = QLineEdit(str(self.config.get("integrations.spotify.redirect_uri", "http://127.0.0.1:8765/callback") or "http://127.0.0.1:8765/callback"))
        self.spotify_connect_button = QPushButton("Переподключить Spotify" if self._existing_spotify_refresh_token_encrypted else "Подключить Spotify")
        self.spotify_connect_button.clicked.connect(self._start_spotify_auth)
        self.spotify_test_button = QPushButton("Проверить Spotify"); self.spotify_test_button.clicked.connect(self._test_spotify_connection)
        spotify_buttons = QWidget(); spotify_buttons_l = QHBoxLayout(spotify_buttons); spotify_buttons_l.setContentsMargins(0,0,0,0); spotify_buttons_l.setSpacing(8)
        spotify_buttons_l.addWidget(self.spotify_connect_button); spotify_buttons_l.addWidget(self.spotify_test_button); spotify_buttons_l.addStretch(1)
        self.spotify_status = QLabel("Статус: подключено" if self._existing_spotify_refresh_token_encrypted else "Статус: не подключено")
        self.spotify_status.setWordWrap(True); self.spotify_status.setObjectName("help")
        self._spotify_auth_worker = None; self._pending_spotify_tokens: dict[str, Any] = {}
        spotify_form.addRow(self.spotify_integration_enabled)
        self._add_field(spotify_form, "Client ID", self.spotify_client_id, "Создайте приложение в Spotify Developer Dashboard. Для desktop NEO использует Authorization Code + PKCE, поэтому Client Secret не нужен и не хранится.")
        self._add_field(spotify_form, "Redirect URI", self.spotify_redirect_uri, "Должен точно совпадать с URI приложения Spotify. Используйте loopback 127.0.0.1, например http://127.0.0.1:8765/callback.")
        spotify_form.addRow("OAuth", spotify_buttons); spotify_form.addRow("", self.spotify_status)
        spotify_form.addRow(self._help("Управление воспроизведением выполняется через официальный Spotify Web API / Spotify Connect. Для playback endpoints Spotify требуется Premium."))
        root.addWidget(spotify_group); self._bind_collapsible_group(spotify_group, [self.spotify_integration_enabled])

        # Discord keeps its official NeoBot music transport and is not one of
        # the mutually-exclusive local music providers.
        discord_heading = QLabel("Discord")
        discord_heading.setStyleSheet("font-size:15px; font-weight:700; padding:8px 2px 0 2px;")
        root.addWidget(discord_heading)
        _ru_mode = str(self.config.get("app.language", "en") or "en").casefold() == "ru"
        self.discord_source_help = self._help(
            "Discord NeoBot воспроизводит источники Яндекс Музыки и прямые ссылки YouTube. Он остаётся отдельным Discord-плеером."
            if _ru_mode else
            "Discord NeoBot воспроизводит прямые ссылки YouTube и остаётся отдельным Discord-плеером."
        )
        root.addWidget(self.discord_source_help)
        self._adopt_scroll_sections(root, self._discord_tab())

        # --- YouTube ------------------------------------------------------
        youtube_group = QGroupBox("YouTube")
        youtube_form = QFormLayout(youtube_group)
        self.youtube_integration_enabled = QCheckBox("Активировать YouTube")
        self.youtube_integration_enabled.setChecked(bool(self.config.get("integrations.youtube.enabled", True)))
        self.preferred_browser = QComboBox()
        for label, value in [("Mozilla Firefox", "firefox"), ("Google Chrome", "chrome"), ("Microsoft Edge", "edge"), ("Opera / Opera GX", "opera"), ("Яндекс Браузер", "yandex")]:
            self.preferred_browser.addItem(label, value)
        idx = self.preferred_browser.findData(str(self.config.get("desktop.preferred_browser", "firefox"))); self.preferred_browser.setCurrentIndex(max(0, idx))
        self._existing_youtube_key_encrypted = str(self.config.get("integrations.youtube.api_key_encrypted", "") or "")
        self.youtube_api_key = QLineEdit(); self.youtube_api_key.setEchoMode(QLineEdit.Password)
        self.youtube_api_key.setPlaceholderText("API key уже сохранён — оставьте пустым" if self._existing_youtube_key_encrypted else "YouTube Data API v3 API key")
        self.youtube_test_button = QPushButton("Проверить YouTube API"); self.youtube_test_button.clicked.connect(self._test_youtube_api)
        self.youtube_status = QLabel("Data API key сохранён" if self._existing_youtube_key_encrypted else "Data API key не настроен; прямые URL и транспорт браузера остаются доступны")
        self.youtube_status.setWordWrap(True); self.youtube_status.setObjectName("help"); self._youtube_test_worker = None
        youtube_form.addRow(self.youtube_integration_enabled)
        self._add_field(youtube_form, "Браузер для видео", self.preferred_browser, "NEO открывает найденное видео в выбранном браузере и управляет активной YouTube media-session безопасными транспортными командами.")
        self._add_field(youtube_form, "YouTube Data API key", self.youtube_api_key, "API key нужен для голосового поиска видео по названию. Для уже открытого видео и прямой youtube.com ссылки ключ не требуется.")
        youtube_form.addRow("Проверка", self.youtube_test_button); youtube_form.addRow("", self.youtube_status)
        youtube_form.addRow(self._help("Создайте Google Cloud project, включите YouTube Data API v3 и создайте API key. NEO не просит пароль YouTube/Google."))
        root.addWidget(youtube_group); self._bind_collapsible_group(youtube_group, [self.youtube_integration_enabled])

        # Named playlists use the existing local YouTube audio transport.
        # Keep their editor independent of the video search/API key controls.
        from jarvis.runtime.youtube_playlists184 import rows_for_ui
        self.youtube_music_group = QGroupBox("Музыка YouTube")
        youtube_music_layout = QVBoxLayout(self.youtube_music_group)
        self.youtube_music_help = self._help(
            "Добавьте название и ссылку на плейлист YouTube с параметром list. API key не нужен. "
            "Например, назовите плейлист «Для работы» и скажите: «Включи плейлист Для работы». "
            "Для Discord добавьте «в Discord» в конце команды. "
            "Изменения сохраняются кнопками «Применить» или «Сохранить»."
        )
        youtube_music_layout.addWidget(self.youtube_music_help)
        self._youtube_music_rows184: list[dict[str, Any]] = []
        self._youtube_music_rows_layout184 = QVBoxLayout()
        youtube_music_layout.addLayout(self._youtube_music_rows_layout184)
        for playlist in rows_for_ui(self.config.get("integrations.youtube.music_playlists", [])):
            self._add_youtube_music_playlist184(playlist)
        self.youtube_music_add_button = QPushButton("Добавить плейлист")
        self.youtube_music_add_button.setAutoDefault(False)
        self.youtube_music_add_button.clicked.connect(lambda _checked=False: self._add_youtube_music_playlist184())
        youtube_music_layout.addWidget(self.youtube_music_add_button)
        root.addWidget(self.youtube_music_group)

        # --- Browser privacy -----------------------------------------------
        # HOTFIX106: destructive browser cleanup is available both from the
        # Telegram Browser panel and from Settings.  Settings uses the same
        # DPAPI-protected critical-action phrase as Telegram so there is one
        # security contract instead of two unrelated passwords.
        browser_privacy_group = QGroupBox("Очистка браузера")
        browser_privacy_form = QFormLayout(browser_privacy_group)
        self.browser_privacy_target = QComboBox()
        try:
            from jarvis.integrations.browser_privacy import available_browsers
            _browsers = list(available_browsers() or [])
        except Exception:
            _browsers = []
        for item in _browsers:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "") or "").strip()
            label = str(item.get("label", "") or key).strip()
            if key:
                self.browser_privacy_target.addItem(label, key)
        if self.browser_privacy_target.count() == 0:
            self.browser_privacy_target.addItem("Поддерживаемые браузеры не найдены", "")
            self.browser_privacy_target.setEnabled(False)
        browser_privacy_buttons = QWidget()
        browser_privacy_buttons_l = QHBoxLayout(browser_privacy_buttons)
        browser_privacy_buttons_l.setContentsMargins(0, 0, 0, 0)
        browser_privacy_buttons_l.setSpacing(8)
        self.browser_clear_history_button = QPushButton("Очистить историю")
        self.browser_full_cleanup_button = QPushButton("Полный снос данных")
        self.browser_clear_history_button.clicked.connect(lambda _checked=False: self._run_browser_privacy_cleanup("clear_history"))
        self.browser_full_cleanup_button.clicked.connect(lambda _checked=False: self._run_browser_privacy_cleanup("full_cleanup"))
        self.browser_clear_history_button.setEnabled(self.browser_privacy_target.isEnabled())
        self.browser_full_cleanup_button.setEnabled(self.browser_privacy_target.isEnabled())
        browser_privacy_buttons_l.addWidget(self.browser_clear_history_button)
        browser_privacy_buttons_l.addWidget(self.browser_full_cleanup_button)
        browser_privacy_buttons_l.addStretch(1)
        browser_privacy_form.addRow("Браузер", self.browser_privacy_target)
        browser_privacy_form.addRow("Действия", browser_privacy_buttons)
        browser_privacy_form.addRow(self._help("Очистка истории удаляет только локальную историю посещений. Полный снос удаляет локальный профиль браузера — cookies, сохранённые пароли, историю и пользовательские настройки — но не удаляет сам браузер. Браузер должен быть полностью закрыт. Оба действия требуют кодовую фразу критических действий."))
        root.addWidget(browser_privacy_group)

        self._music_provider_toggle_guard = False
        for provider, toggle in (("yandex_music", self.yandex_integration_enabled), ("spotify", self.spotify_integration_enabled)):
            toggle.toggled.connect(lambda checked, name=provider: self._music_provider_toggled(name, checked))
        # Normalize a legacy/hand-edited profile immediately in the UI.
        active = str(self.config.get("integrations.music.active_provider", "") or "").strip().casefold()
        enabled = [name for name, toggle in (("yandex_music", self.yandex_integration_enabled), ("spotify", self.spotify_integration_enabled)) if toggle.isChecked()]
        if len(enabled) > 1:
            self._music_provider_toggled(active if active in enabled else enabled[0], True)

        root.addStretch(1)
        return self._scroll(content)

    def _run_browser_privacy_cleanup(self, mode: str) -> None:
        """Run browser cleanup from Settings after an explicit code phrase."""
        code = language_code(self.config)
        L = lambda text: tr(text, code)
        target = str(getattr(self, "browser_privacy_target", None).currentData() or "") if hasattr(self, "browser_privacy_target") else ""
        if not target:
            QMessageBox.warning(self, L("Очистка браузера"), L("Поддерживаемый браузер не выбран."))
            return
        expected = str(telegram_critical_phrase(self.config) or "")
        if not expected:
            QMessageBox.warning(
                self,
                L("Кодовая фраза не настроена"),
                L("Сначала задайте кодовую фразу критических действий в разделе удалённого подключения Telegram."),
            )
            return
        if mode == "full_cleanup":
            answer = QMessageBox.warning(
                self,
                L("Полный снос данных браузера"),
                L("Будет удалён локальный профиль выбранного браузера: cookies, сохранённые пароли, история и пользовательские настройки. Сам браузер останется установлен. Продолжить?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        supplied, accepted = QInputDialog.getText(
            self,
            L("Подтверждение критического действия"),
            L("Введите кодовую фразу:"),
            QLineEdit.EchoMode.Password,
        )
        if not accepted:
            return
        if not hmac.compare_digest(str(supplied).strip(), expected.strip()):
            QMessageBox.warning(self, L("Неверная кодовая фраза"), L("Кодовая фраза неверна. Действие отменено."))
            return
        try:
            from jarvis.integrations.browser_privacy import clear_history, full_cleanup
            result = clear_history(target) if mode == "clear_history" else full_cleanup(target)
            message = str(result.get("message", "") or L("Операция завершена.")) if isinstance(result, dict) else str(result)
            QMessageBox.information(self, L("Очистка браузера"), message)
        except Exception as exc:
            QMessageBox.warning(self, L("Очистка браузера"), str(exc))

    def _add_youtube_music_playlist184(self, playlist: dict[str, str] | None = None) -> None:
        """Stage one editable row, including malformed saved rows needing repair."""
        values = playlist or {}
        container = QWidget(self.youtube_music_group)
        form = QFormLayout(container)
        form.setContentsMargins(0, 4, 0, 4)
        name = QLineEdit(values.get("name", ""))
        name.setPlaceholderText("Название плейлиста")
        url = QLineEdit(values.get("url", ""))
        url.setPlaceholderText("https://www.youtube.com/playlist?list=…")
        delete = QPushButton("Удалить")
        delete.setAutoDefault(False)
        url_container = QWidget()
        url_layout = QHBoxLayout(url_container)
        url_layout.setContentsMargins(0, 0, 0, 0)
        url_layout.addWidget(url, 1)
        url_layout.addWidget(delete)
        form.addRow("Название плейлиста", name)
        form.addRow("Ссылка на плейлист", url_container)
        row = {"widget": container, "name": name, "url": url, "delete": delete}
        self._youtube_music_rows184.append(row)
        self._youtube_music_rows_layout184.addWidget(container)
        delete.clicked.connect(lambda _checked=False, current=row: self._remove_youtube_music_playlist184(current))
        self._name_form_fields184(container)
        self._tidy_forms174(container)
        # Rows created after initial construction need source-tagged chrome too.
        # translate_widget_tree deliberately leaves editable user text untouched.
        translate_widget_tree(container, language_code(self.config))

    def _remove_youtube_music_playlist184(self, row: dict[str, Any]) -> None:
        if row not in self._youtube_music_rows184:
            return
        self._youtube_music_rows184.remove(row)
        widget = row["widget"]
        self._youtube_music_rows_layout184.removeWidget(widget)
        widget.hide()
        widget.deleteLater()

    def _youtube_music_playlist_values184(self) -> list[dict[str, str]]:
        return [{"name": row["name"].text(), "url": row["url"].text()}
                for row in self._youtube_music_rows184]

    def _music_provider_toggled(self, provider: str, checked: bool) -> None:
        if bool(getattr(self, "_music_provider_toggle_guard", False)):
            return
        if not checked:
            return
        mapping = {
            "yandex_music": getattr(self, "yandex_integration_enabled", None),
            "spotify": getattr(self, "spotify_integration_enabled", None),
        }
        self._music_provider_toggle_guard = True
        try:
            for name, toggle in mapping.items():
                if toggle is not None and name != provider and toggle.isChecked():
                    toggle.setChecked(False)
        finally:
            self._music_provider_toggle_guard = False

    def _active_music_provider_from_ui(self) -> str:
        for name, attr in (("yandex_music", "yandex_integration_enabled"), ("spotify", "spotify_integration_enabled")):
            toggle = getattr(self, attr, None)
            if toggle is not None and toggle.isChecked():
                return name
        return ""

    def _persist_media_partial(self, provider: str, values: dict[str, Any], *, enable_provider: bool = False) -> None:
        def apply(data: dict[str, Any]) -> None:
            integrations = data.setdefault("integrations", {})
            integrations.setdefault(provider, {}).update(values)
            if enable_provider and provider in {"yandex_music", "spotify"}:
                integrations.setdefault("music", {})["active_provider"] = provider
                for name in ("yandex_music", "spotify"):
                    integrations.setdefault(name, {})["enabled"] = name == provider
        updater = getattr(self.config, "update", None)
        if callable(updater):
            # NEO 183: patch the newest settings on disk under the save lock (a fresh OAuth result).
            updater(apply, credentials=True)
            return
        data = deepcopy(self.config.data)
        apply(data)
        self.config.save(data)

    def _start_spotify_auth(self) -> None:
        if getattr(self, "_spotify_auth_worker", None) is not None:
            return
        client_id = self.spotify_client_id.text().strip()
        redirect_uri = self.spotify_redirect_uri.text().strip()
        if not client_id:
            QMessageBox.warning(self, "Spotify", "Укажите Spotify Client ID.")
            return
        from jarvis.integrations.spotify import authorize_spotify_pkce
        self.spotify_connect_button.setEnabled(False); self._status_text(self.spotify_status, "Статус: открыл Spotify OAuth в браузере…")
        worker = FunctionWorker(authorize_spotify_pkce, client_id, redirect_uri)
        self._spotify_auth_worker = worker
        # NEO 183: the tokens belong to the app that was asked; editing the field meanwhile changes nothing
        self._spotify_auth_context183 = {"client_id": client_id, "redirect_uri": redirect_uri}
        # NEO 182: every attempt has a generation; a result arriving after Cancel/close is ignored.
        self._spotify_auth_generation182 = generation = int(getattr(self, "_spotify_auth_generation182", 0)) + 1
        worker.signals.result.connect(lambda result, g=generation: self._on_spotify_auth_success(result, g))
        worker.signals.error.connect(lambda trace, g=generation: None if self._spotify_auth_stale182(g) else self._status_text(self.spotify_status, "Ошибка Spotify OAuth: {error}", error=safe_error(str(trace or ""))[-1200:]))
        worker.signals.finished.connect(self._on_spotify_auth_finished)
        QThreadPool.globalInstance().start(worker)

    def _spotify_auth_stale182(self, generation: int | None) -> bool:
        if bool(getattr(self, "_settings_closed160", False)):
            return True
        return generation is not None and generation != int(getattr(self, "_spotify_auth_generation182", 0))

    def _on_spotify_auth_success(self, result: object, generation: int | None = None) -> None:
        if self._spotify_auth_stale182(generation):
            logging.getLogger(__name__).info("Spotify OAuth result ignored: the Settings window was closed or a newer attempt started")
            return
        info = dict(result or {}) if isinstance(result, dict) else {}
        access = str(info.get("access_token", "") or ""); refresh = str(info.get("refresh_token", "") or "")
        if not access:
            self._status_text(self.spotify_status, "Spotify OAuth завершился без access token."); return
        context = dict(getattr(self, "_spotify_auth_context183", {}) or {})
        used_client = str(context.get("client_id") or self.spotify_client_id.text().strip())
        used_redirect = str(context.get("redirect_uri") or self.spotify_redirect_uri.text().strip())
        if self.spotify_client_id.text().strip() != used_client:
            self.spotify_client_id.setText(used_client)
        self._existing_spotify_client_id_encrypted = protect_secret(used_client)
        self._existing_spotify_access_token_encrypted = protect_secret(access)
        if refresh:
            self._existing_spotify_refresh_token_encrypted = protect_secret(refresh)
        expires = float(info.get("expires_at", time.time() + 3500) or 0.0)
        self.spotify_integration_enabled.setChecked(True)
        self._persist_media_partial("spotify", {
            "client_id_encrypted": self._existing_spotify_client_id_encrypted,
            "access_token_encrypted": self._existing_spotify_access_token_encrypted,
            "refresh_token_encrypted": self._existing_spotify_refresh_token_encrypted,
            "token_expires_at": expires,
            "redirect_uri": used_redirect or "http://127.0.0.1:8765/callback",
        }, enable_provider=True)
        self._status_text(self.spotify_status, "Статус: Spotify подключён. Провайдер Spotify активирован.")
        self.spotify_connect_button.setText(self._t("Переподключить Spotify"))

    def _on_spotify_auth_finished(self) -> None:
        self._spotify_auth_worker = None
        if not bool(getattr(self, "_settings_closed160", False)):
            self.spotify_connect_button.setEnabled(True)

    def _test_spotify_connection(self) -> None:
        try:
            from jarvis.integrations.spotify import SpotifyAdapter
            if not self.config.get("integrations.spotify.refresh_token_encrypted", "") and not self.config.get("integrations.spotify.access_token_encrypted", ""):
                raise RuntimeError("Сначала нажмите «Подключить Spotify».")
            info = SpotifyAdapter(self.config).execute("status")
            title = " — ".join(x for x in (str(info.get("artist", "") or ""), str(info.get("title", "") or "")) if x)
            self._status_text(self.spotify_status, "API отвечает · {track}" if title else "API отвечает · активного трека нет", **({"track": title} if title else {}))
        except Exception as exc:
            self._status_text(self.spotify_status, "Ошибка Spotify: {error}", error=safe_error(exc))

    def _current_youtube_key(self) -> str:
        typed = self.youtube_api_key.text().strip()
        if typed:
            return typed
        return str(unprotect_secret(self._existing_youtube_key_encrypted) if self._existing_youtube_key_encrypted else "").strip()

    def _test_youtube_api(self) -> None:
        key = self._current_youtube_key()
        if not key:
            QMessageBox.warning(self, "YouTube", self._t("Укажите YouTube Data API key.")); return
        if getattr(self, "_youtube_test_worker184", None) is not None:
            return
        # NEO 184: the request runs in the background; the window stays responsive meanwhile.
        self._youtube_test_generation184 = generation = int(getattr(self, "_youtube_test_generation184", 0)) + 1
        self.youtube_test_button.setEnabled(False)
        self.youtube_test_button.setText(self._t("Проверяю…"))
        self._status_text(self.youtube_status, "YouTube Data API: проверяю…")

        def probe(api_key: str) -> dict[str, Any]:
            response = requests.get("https://www.googleapis.com/youtube/v3/search",
                                    params={"part": "snippet", "type": "video", "maxResults": 1, "q": "YouTube", "key": api_key},
                                    timeout=12)
            payload = response.json() if response.content else {}
            if response.status_code >= 400:
                err = payload.get("error") if isinstance(payload, dict) else {}
                msg = str((err or {}).get("message", "") if isinstance(err, dict) else err or "")
                raise RuntimeError(msg or f"HTTP {response.status_code}")
            return {"ok": True}

        worker = FunctionWorker(probe, key)
        self._youtube_test_worker184 = worker

        def stale() -> bool:
            return bool(self._settings_closed160) or generation != int(getattr(self, "_youtube_test_generation184", 0))

        def ok(_result: object) -> None:
            if not stale():
                self._status_text(self.youtube_status, "YouTube Data API: OK. Поиск видео доступен.")

        def failed(trace: object) -> None:
            if stale():
                return
            # NEO 183: the key never shows up in the visible error (query parameter or literal)
            error = self._short_network_error184(trace, secrets=(key,))
            self._status_text(self.youtube_status, "YouTube Data API: ошибка — {error}", error=error)

        def done() -> None:
            self._youtube_test_worker184 = None
            if not self._settings_closed160:
                self.youtube_test_button.setEnabled(True)
                self.youtube_test_button.setText(self._t("Проверить YouTube API"))
        worker.signals.result.connect(ok)
        worker.signals.error.connect(failed)
        worker.signals.finished.connect(done)
        QThreadPool.globalInstance().start(worker)

    def _short_network_error184(self, trace: object, *, secrets: tuple[str, ...] = ()) -> str:
        """One short, translated line for a provider error (the technical text stays in the log)."""
        raw = str(trace or "")
        # REC-PRIVACY-SOURCE-DATA-01 R1: caller-owned secrets are removed from the
        # whole text before any logging or truncation; patterns only add to that.
        for secret in secrets:
            if secret and len(str(secret)) >= 4:
                raw = raw.replace(str(secret), "[REDACTED]")
        raw = safe_error(raw)
        logging.getLogger(__name__).info("Provider check failed: %s", raw[-600:])
        low = raw.casefold()
        if "proxyerror" in low or "unable to connect to proxy" in low:
            return self._t("нет соединения через прокси-сервер")
        if "timeout" in low or "timed out" in low:
            return self._t("сервер не ответил вовремя")
        if "connectionerror" in low or "max retries exceeded" in low or "name resolution" in low or "failed to establish" in low:
            return self._t("нет соединения с сервером")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        last = lines[-1] if lines else raw
        last = re.sub(r"^(?:[\w.]+(?:Error|Exception)):\s*", "", last)
        return last[:300]

    @staticmethod
    def _adopt_scroll_sections(target: QVBoxLayout, area: QScrollArea) -> None:
        content = area.takeWidget()
        if content is None or content.layout() is None:
            return
        source = content.layout()
        while source.count():
            item = source.takeAt(0)
            widget = item.widget()
            if widget is not None:
                target.addWidget(widget)
        area.deleteLater()
        content.deleteLater()

    def _open_yandex_auth_page(self) -> None:
        url = str(getattr(self, "_yandex_auth_url", "") or "").strip()
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _start_yandex_music_auth(self) -> None:
        if getattr(self, "_yandex_auth_worker", None) is not None:
            return
        self.yandex_connect_button.setEnabled(False)
        self.yandex_open_auth_button.setVisible(False)
        if hasattr(self, "yandex_auth_code"):
            self.yandex_auth_code.clear(); self.yandex_auth_code.setVisible(False); self.yandex_copy_code_button.setVisible(False)
        self._yandex_auth_url = ""
        self._status_text(self.yandex_connect_status, "Статус: подключение… Получаю код авторизации.")

        worker = FunctionWorker(
            authorize_yandex_music,
            progress_keyword="progress_callback",
        )
        self._yandex_auth_worker = worker
        # NEO 183: like Spotify — a result after Cancel/close or after a newer attempt is ignored.
        self._yandex_auth_generation183 = g = int(getattr(self, "_yandex_auth_generation183", 0)) + 1
        worker.signals.progress.connect(lambda payload, g=g: None if self._yandex_auth_stale183(g) else self._on_yandex_auth_progress(payload))
        worker.signals.result.connect(lambda result, g=g: None if self._yandex_auth_stale183(g) else self._on_yandex_auth_success(result))
        worker.signals.error.connect(lambda trace, g=g: None if self._yandex_auth_stale183(g) else self._on_yandex_auth_error(trace))
        worker.signals.finished.connect(self._on_yandex_auth_finished)
        QThreadPool.globalInstance().start(worker)

    def _yandex_auth_stale183(self, generation: int | None) -> bool:
        if bool(getattr(self, "_settings_closed160", False)):
            return True
        return generation is not None and generation != int(getattr(self, "_yandex_auth_generation183", 0))

    def _on_yandex_auth_progress(self, payload: object) -> None:
        info = payload if isinstance(payload, dict) else {}
        stage = str(info.get("stage", "") or "")
        message = str(info.get("message", "") or "").strip()
        if stage == "code":
            self._yandex_auth_url = str(info.get("verification_url", "") or "").strip()
            user_code = str(info.get("user_code", "") or "").strip()
            if self._yandex_auth_url:
                self.yandex_open_auth_button.setVisible(True)
                QDesktopServices.openUrl(QUrl(self._yandex_auth_url))
            if user_code:
                if hasattr(self, "yandex_auth_code"):
                    self.yandex_auth_code.setText(user_code)
                    self.yandex_auth_code.setVisible(True)
                    self.yandex_copy_code_button.setVisible(True)
                self._status_text(self.yandex_connect_status,
                    "Ждём подтверждение. Код: {code}. Страница Яндекса открыта в браузере.", code=user_code)
                return
        if message:
            self._status_text(self.yandex_connect_status, "Статус: {message}", message=message)

    def _on_yandex_auth_success(self, result: object) -> None:
        info = result if isinstance(result, dict) else {}
        token = str(info.get("token", "") or "").strip()
        if not token:
            self._on_yandex_auth_error("Авторизация завершилась без access token.")
            return
        encrypted = protect_secret(token)
        refresh_token = str(info.get("refresh_token", "") or "").strip()
        refresh_encrypted = protect_secret(refresh_token) if refresh_token else ""
        self._existing_yandex_token_encrypted = encrypted
        self.yandex_api_token.clear()
        self.yandex_api_token.setPlaceholderText("OAuth-токен уже сохранён — оставьте пустым, чтобы не менять")
        self.yandex_integration_enabled.setChecked(True)
        self.yandex_api_enabled.setChecked(True)

        # Connection is an explicit action, so persist only the new credential
        # immediately. Other staged settings in this dialog remain transactional.
        data = deepcopy(self.config.data)
        ym = data.setdefault("integrations", {}).setdefault("yandex_music", {})
        ym["enabled"] = True
        ym["api_enabled"] = True
        ym["token_encrypted"] = encrypted
        if refresh_encrypted:
            ym["refresh_token_encrypted"] = refresh_encrypted
        ym["token_type"] = str(info.get("token_type", "") or "")
        try:
            ym["token_expires_in"] = max(0, int(info.get("token_expires_in", 0) or 0))
        except (TypeError, ValueError):
            ym["token_expires_in"] = 0
        ym["token_saved_at"] = int(time.time())
        try:
            self.config.save(data)
        except Exception as exc:
            self._status_text(self.yandex_connect_status, "Не удалось сохранить подключение: {error}", error=safe_error(exc))
            QMessageBox.warning(self, "Яндекс Музыка", f"Авторизация прошла, но токен не удалось сохранить: {exc}")
            return
        self.data = deepcopy(self.config.data)
        self._status_text(self.yandex_connect_status,
            "Статус: подключено. Токен сохранён через Windows DPAPI. Если API Яндекс Музыки отвечает медленно, повторная авторизация не требуется.")
        self.yandex_connect_button.setText(self._t("Переподключить Яндекс Музыку"))
        self.settings_saved.emit()

    def _on_yandex_auth_error(self, trace: str) -> None:
        text = str(trace or "").strip()
        # FunctionWorker sends a traceback; surface only the useful final line.
        last_line = text.splitlines()[-1].strip() if text else "Неизвестная ошибка авторизации"
        if last_line.startswith("RuntimeError:"):
            last_line = last_line.split(":", 1)[1].strip()
        self._status_text(self.yandex_connect_status, "Ошибка подключения: {error}", error=last_line)
        QMessageBox.warning(self, "Яндекс Музыка", last_line)

    def _on_yandex_auth_finished(self) -> None:
        self._yandex_auth_worker = None
        if not bool(getattr(self, "_settings_closed160", False)):
            self.yandex_connect_button.setEnabled(True)

    def _discord_tab(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)

        base = QGroupBox("Discord")
        form = QFormLayout(base)
        self.discord_integration_enabled = QCheckBox("Разрешить интеграцию Discord")
        self.discord_integration_enabled.setChecked(bool(self.config.get("integrations.discord.enabled", True)))
        self.discord_background_bot_enabled = QCheckBox("Фоновый NEO Bot через официальный Discord API")
        self.discord_background_bot_enabled.setChecked(bool(self.config.get("integrations.discord.background_bot_enabled", True)))
        self.discord_auto_follow_user_voice = QCheckBox("Автоматически входить в голосовой канал, где сейчас находится пользователь")
        self.discord_auto_follow_user_voice.setChecked(bool(self.config.get("integrations.discord.auto_follow_user_voice", True)))
        self.discord_never_steal_input = QCheckBox("Не перехватывать мышь/клавиатуру и не менять активное окно")
        self.discord_never_steal_input.setChecked(bool(self.config.get("integrations.discord.never_steal_input", True)))
        self._legacy_discord_token_encrypted = str(self.config.get("integrations.discord.bot_token_encrypted", "") or "")
        form.addRow(self.discord_integration_enabled)
        form.addRow(self.discord_background_bot_enabled)
        form.addRow(self.discord_auto_follow_user_voice)
        self.discord_leave_with_owner = QCheckBox(dt("discord_leave_owner", "ru"))
        self.discord_leave_with_owner.setChecked(bool(self.config.get("integrations.discord.leave_with_owner", True)))
        form.addRow(self.discord_leave_with_owner)
        form.addRow(self._help(dt("discord_owner_help", "ru")))
        form.addRow(self._help("Только официальный NEO Bot/API. Сервер и voice-канал определяются автоматически; desktop Discord, pyautogui и UI fallback не используются."))
        root.addWidget(base)
        self._bind_collapsible_group(base, [self.discord_integration_enabled])

        servers_group = QGroupBox("Серверы Discord")
        servers_layout = QVBoxLayout(servers_group)
        self.discord_servers_table = QTableWidget(0, 4)
        self.discord_servers_table.setHorizontalHeaderLabels([
            "Основной", "Сервер", "BOT token", dt("discord_owner_id", "ru"),
        ])
        self.discord_servers_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.discord_servers_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.discord_servers_table.setColumnWidth(1, 170)
        self.discord_servers_table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.discord_servers_table.setColumnWidth(2, 190)
        self.discord_servers_table.setColumnWidth(3, 170)
        self.discord_servers_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.discord_servers_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.discord_servers_table.setMinimumHeight(220)

        profiles = self.config.get("integrations.discord.servers", []) or []
        default_server = str(self.config.get("integrations.discord.default_server", "") or "").strip()
        if not isinstance(profiles, list):
            profiles = []
        if not profiles and any((default_server, self.config.get("integrations.discord.music_text_channel", ""), self.config.get("integrations.discord.default_voice_channel", ""))):
            profiles = [{
                "name": default_server,
                "voice_channel": str(self.config.get("integrations.discord.default_voice_channel", "") or ""),
                "music_text_channel": str(self.config.get("integrations.discord.music_text_channel", "") or ""),
                "music_bot_name": str(self.config.get("integrations.discord.music_bot_name", "") or ""),
            }]
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            self._discord_add_server_row(
                name=str(profile.get("name", "") or ""),
                bot_token_encrypted=str(profile.get("bot_token_encrypted", "") or ""),
                guild_id=str(profile.get("guild_id", "") or ""),
                user_id=str(profile.get("discord_user_id", "") or ""),
                is_default=bool(default_server and str(profile.get("name", "") or "").casefold().strip() == default_server.casefold()),
            )
        if self.discord_servers_table.rowCount() and not any(self.discord_servers_table.item(r, 0).checkState() == Qt.Checked for r in range(self.discord_servers_table.rowCount())):
            self.discord_servers_table.item(0, 0).setCheckState(Qt.Checked)
        self.discord_servers_table.itemChanged.connect(self._discord_default_item_changed)
        self.discord_servers_table.itemChanged.connect(self._invalidate_discord_discovery_view184)
        self.discord_servers_table.itemSelectionChanged.connect(self._invalidate_discord_discovery_view184)
        servers_layout.addWidget(self.discord_servers_table)
        buttons = QWidget()
        buttons_layout = QHBoxLayout(buttons)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        add_server = QPushButton("Добавить сервер")
        add_server.clicked.connect(lambda: self._discord_add_server_row())
        remove_server = QPushButton("Удалить выбранный")
        remove_server.clicked.connect(self._discord_remove_selected_server)
        discover_server = QPushButton("Обнаружить каналы")
        self.discord_discover_button184 = discover_server
        discover_server.setEnabled(self._discord_discovery_worker is None)
        discover_server.clicked.connect(self._discord_discover_selected_channels)
        buttons_layout.addWidget(add_server)
        buttons_layout.addWidget(remove_server)
        buttons_layout.addWidget(discover_server)
        buttons_layout.addStretch(1)
        servers_layout.addWidget(buttons)
        servers_layout.addWidget(self._help("Guild ID и User ID вводить больше не нужно: NEO определяет доступный сервер и занятый пользователем voice-канал автоматически. Для нескольких установок общая очередь синхронизируется через NEO Bot."))
        self.discord_source_help = self._help(
            "Discord NeoBot воспроизводит источники Яндекс Музыки и прямые ссылки YouTube. Он остаётся отдельным Discord-плеером."
            if str(self.config.get("app.language", "en") or "en").casefold() == "ru"
            else "Discord NeoBot воспроизводит прямые ссылки YouTube и остаётся отдельным Discord-плеером."
        )
        servers_layout.addWidget(self.discord_source_help)
        root.addWidget(servers_group)
        self.discord_integration_enabled.toggled.connect(servers_group.setVisible)
        visibility_timer = QTimer(servers_group)
        visibility_timer.setSingleShot(True)
        visibility_timer.timeout.connect(lambda: servers_group.setVisible(self.discord_integration_enabled.isChecked()))
        visibility_timer.start(0)

        root.addStretch(1)
        return self._scroll(content)

    def _discord_add_server_row(
        self,
        name: str = "",
        bot_token_encrypted: str = "",
        guild_id: str = "",
        user_id: str = "",
        *,
        is_default: bool = False,
    ) -> None:
        row = self.discord_servers_table.rowCount()
        self.discord_servers_table.insertRow(row)
        default_item = QTableWidgetItem("")
        default_item.setFlags((default_item.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
        default_item.setCheckState(Qt.Checked if is_default else Qt.Unchecked)
        self.discord_servers_table.setItem(row, 0, default_item)
        self.discord_servers_table.setItem(row, 1, QTableWidgetItem(str(name or "")))
        token_edit = QLineEdit()
        token_edit.setEchoMode(QLineEdit.Password)
        token_edit.textChanged.connect(self._invalidate_discord_discovery_view184)
        token_edit.setProperty("existing_encrypted", str(bot_token_encrypted or ""))
        token_edit.setPlaceholderText("Сохранён — оставьте пустым" if bot_token_encrypted else "BOT token этого сервера")
        token_edit.setToolTip("BOT token хранится через Windows DPAPI. Оставьте поле пустым, чтобы сохранить уже записанный токен.")
        self.discord_servers_table.setCellWidget(row, 2, token_edit)
        self.discord_servers_table.setItem(row, 3, QTableWidgetItem(str(user_id or "")))
        # Legacy IDs remain in config for backwards compatibility, but are no
        # longer user-facing fields. Automatic discovery is authoritative.
        default_item.setData(Qt.UserRole + 20, str(guild_id or ""))
        default_item.setData(Qt.UserRole + 21, str(user_id or ""))
        if row == 0 and not is_default:
            default_item.setCheckState(Qt.Checked)

    def _discord_remove_selected_server(self) -> None:
        row = self.discord_servers_table.currentRow()
        if row < 0:
            return
        was_default = self.discord_servers_table.item(row, 0).checkState() == Qt.Checked
        self.discord_servers_table.removeRow(row)
        if was_default and self.discord_servers_table.rowCount():
            self.discord_servers_table.item(0, 0).setCheckState(Qt.Checked)

    def _discord_discovery_context184(self):
        """Compare the selected row and saved profile without retaining widgets."""
        try:
            row = self.discord_servers_table.currentRow()
            if row < 0:
                return None
            item = self.discord_servers_table.item(row, 1)
            default = self.discord_servers_table.item(row, 0)
            user = self.discord_servers_table.item(row, 3)
            token = self.discord_servers_table.cellWidget(row, 2)
            return (
                id(self.config), str(self.config.paths.root), id(self.discord_servers_table), row,
                str(item.text() if item is not None else "").strip(),
                str(user.text() if user is not None else ""),
                str(default.data(Qt.UserRole + 20) if default is not None else ""),
                str(token.text() if token is not None else ""),
                str(token.property("existing_encrypted") if token is not None else ""),
                deepcopy(self.config.get("integrations.discord", {}) or {}),
            )
        except (RuntimeError, AttributeError, TypeError):
            return None

    def _discord_discovery_current184(self, request) -> bool:
        return (not self._settings_closed160
                and request is self._discord_discovery_relay184
                and request.worker is self._discord_discovery_worker
                and not request.cancelled.is_set()
                and request.context == self._discord_discovery_context184())

    @Slot()
    def _invalidate_discord_discovery_view184(self, *_args) -> None:
        # Cancellation invalidates this result even if selection later returns
        # to the same row. Keep single-flight ownership until its worker ends.
        request = self._discord_discovery_relay184
        if request is not None:
            request.cancel()

    @Slot()
    def _cancel_discord_discovery184(self, *_args) -> None:
        request = self._discord_discovery_relay184
        self._discord_discovery_relay184 = None
        self._discord_discovery_worker = None
        if request is not None:
            request.cancel()

    @Slot(object, object)
    def _discord_discovery_result184(self, request, result: object) -> None:
        if not self._discord_discovery_current184(request):
            return
        info = dict(result or {}) if isinstance(result, dict) else {}
        if info.get("cancelled"):
            return
        voice = [str(x.get("name", "")) for x in (info.get("voice_channels") or []) if isinstance(x, dict)]
        text = [str(x.get("name", "")) for x in (info.get("text_channels") or []) if isinstance(x, dict)]
        body = [f"Сервер: {request.server}", "", "Голосовые каналы:"]
        body.extend([f"• {x}" for x in voice] or ["• нет доступных"])
        body.extend(["", "Текстовые каналы:"])
        body.extend([f"• {x}" for x in text] or ["• нет доступных"])
        QMessageBox.information(self, "Discord · доступные каналы", "\n".join(body))

    @Slot(object, str)
    def _discord_discovery_error184(self, request, trace: str) -> None:
        if self._discord_discovery_current184(request):
            QMessageBox.warning(self, "Discord", safe_error(str(trace or "Ошибка обнаружения"))[-1800:])

    @Slot(object)
    def _discord_discovery_finished184(self, request) -> None:
        # A delayed/duplicate finish may only release the request it owns.
        if (request is not self._discord_discovery_relay184
                or request.worker is not self._discord_discovery_worker):
            return
        self._discord_discovery_worker = None
        self._discord_discovery_relay184 = None
        if not self._settings_closed160:
            try:
                self.discord_discover_button184.setEnabled(True)
            except RuntimeError:
                pass  # The media page itself may have been destroyed/rebuilt.

    def _discord_discover_selected_channels(self) -> None:
        if self._settings_closed160 or self._discord_discovery_worker is not None:
            return
        row = self.discord_servers_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Discord", "Сначала выберите сервер в таблице.")
            return
        item = self.discord_servers_table.item(row, 1)
        server = str(item.text() if item is not None else "").strip()
        if not server:
            QMessageBox.warning(self, "Discord", "У выбранной строки не указано название сервера.")
            return
        saved = self.config.get("integrations.discord.servers", []) or []
        if not any(isinstance(x, dict) and str(x.get("name", "") or "").casefold().strip() == server.casefold() for x in saved):
            QMessageBox.information(self, "Discord", "Сначала нажмите «Применить»/«Сохранить», чтобы NEO сохранил BOT token этого сервера, затем повторите обнаружение каналов.")
            return
        context = self._discord_discovery_context184()
        if context is None:
            return
        cancelled = threading.Event()
        snapshot = _DiscordDiscoveryConfig184(self.config)
        worker = FunctionWorker(_discover_discord_channels184, snapshot, server, cancelled)
        request = _DiscordDiscoveryRelay184(self, context=context, server=server, cancelled=cancelled)
        request.bind(worker)
        request.result.connect(self._discord_discovery_result184)
        request.error.connect(self._discord_discovery_error184)
        request.finished.connect(self._discord_discovery_finished184)
        self._discord_discovery_worker = worker
        self._discord_discovery_relay184 = request
        self.discord_discover_button184.setEnabled(False)
        try:
            QThreadPool.globalInstance().start(worker)
        except Exception as exc:
            self._discord_discovery_finished184(request)
            request.cancel()
            request.worker = None
            request.context = None
            request.deleteLater()
            # An unsubmitted FunctionWorker will never emit finished, so its
            # application-owned signal object needs explicit GUI-thread cleanup.
            cleanup = getattr(worker.signals, "dispose_finished", None)
            if callable(cleanup):
                cleanup()
            else:
                worker.signals.deleteLater()
            QMessageBox.warning(self, "Discord", safe_error(exc))

    def _discord_default_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0 or item.checkState() != Qt.Checked:
            return
        self.discord_servers_table.blockSignals(True)
        try:
            for row in range(self.discord_servers_table.rowCount()):
                other = self.discord_servers_table.item(row, 0)
                if other is not None and other is not item:
                    other.setCheckState(Qt.Unchecked)
        finally:
            self.discord_servers_table.blockSignals(False)

    def _discord_profile_rows(self) -> tuple[list[dict[str, str]], str]:
        profiles: list[dict[str, str]] = []
        default_name = ""
        seen: set[str] = set()
        existing_profiles = self.config.get("integrations.discord.servers", []) or []
        existing_by_name = {
            str(item.get("name", "") or "").casefold().strip(): item
            for item in existing_profiles if isinstance(item, dict) and str(item.get("name", "") or "").strip()
        } if isinstance(existing_profiles, list) else {}

        for row in range(self.discord_servers_table.rowCount()):
            def cell(col: int) -> str:
                item = self.discord_servers_table.item(row, col)
                return str(item.text() if item is not None else "").strip()

            name = cell(1)
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                raise ValueError(f"Сервер Discord «{name}» добавлен несколько раз")
            seen.add(key)
            old_profile = existing_by_name.get(key, {})
            token_widget = self.discord_servers_table.cellWidget(row, 2)
            entered_token = token_widget.text().strip() if isinstance(token_widget, QLineEdit) else ""
            existing_token = (
                str(token_widget.property("existing_encrypted") or "").strip()
                if isinstance(token_widget, QLineEdit) else ""
            )
            if not existing_token:
                existing_token = str(old_profile.get("bot_token_encrypted", "") or "").strip()
            default_item_for_token = self.discord_servers_table.item(row, 0)
            # v4.0.7 had one global token. On first save, migrate it into the
            # default/only server row without ever decrypting it into the UI.
            if not existing_token and self._legacy_discord_token_encrypted:
                if default_item_for_token is not None and default_item_for_token.checkState() == Qt.Checked:
                    existing_token = self._legacy_discord_token_encrypted
            owner_id = cell(3)
            if owner_id and (not owner_id.isdigit() or not 1 <= int(owner_id) < 2**64):
                raise ValueError(dt("discord_owner_invalid", "ru"))
            profile = {
                "name": name,
                "bot_token_encrypted": protect_secret(entered_token) if entered_token else existing_token,
                "guild_id": str(old_profile.get("guild_id", "") or (default_item_for_token.data(Qt.UserRole + 20) if default_item_for_token is not None else "") or ""),
                "discord_user_id": owner_id,
            }
            profiles.append(profile)
            default_item = self.discord_servers_table.item(row, 0)
            if default_item is not None and default_item.checkState() == Qt.Checked and not default_name:
                default_name = name
        if profiles and not default_name:
            default_name = profiles[0]["name"]
        return profiles, default_name

    def _gaming_tab169(self) -> QWidget:
        """NEO 169: game session, game notices and NVIDIA Instant Replay in one place."""
        content = QWidget()
        root = QVBoxLayout(content)

        gaming_group = QGroupBox("Определение игровой сессии")
        gaming_form = QFormLayout(gaming_group)
        self.gaming_enabled = QCheckBox("Определять запущенную игру и вести таймер сессии")
        self.gaming_enabled.setChecked(bool(self.config.get("gaming.enabled", True)))
        self.gaming_telemetry_refresh = self._spin(
            1, 30,
            int(round(float(self.config.get("gaming.telemetry_refresh_seconds", 2.0) or 2.0))),
            " с", 1,
        )
        gaming_form.addRow(self.gaming_enabled)
        self._add_field(
            gaming_form, "Проверка состояния игры", self.gaming_telemetry_refresh,
            "Как часто проверять, продолжает ли работать игровой процесс. Это только лёгкая проверка процесса; игровые базы не используются.",
        )
        root.addWidget(gaming_group)

        notices = QGroupBox("Игровые уведомления")
        notices_form = QFormLayout(notices)
        self.game_launch_notice169 = QCheckBox("Уведомление о запуске игры")
        self.game_launch_notice169.setChecked(bool(self.config.get("gaming.launch_greetings_enabled", True)))
        self.game_session_start169 = QCheckBox("Уведомление о начале игровой сессии (окно игры впервые на переднем плане)")
        self.game_session_start169.setChecked(bool(self.config.get("gaming.notifications.session_start", False)))
        self.game_session_end169 = QCheckBox("Уведомление о завершении сессии с её длительностью")
        self.game_session_end169.setChecked(bool(self.config.get("gaming.notifications.session_end", False)))
        self.match_ready_enabled169 = QCheckBox("Уведомление о готовности матча")
        self.match_ready_enabled169.setChecked(bool(self.config.get("gaming.match_ready.enabled", True)))
        self.match_ready_telegram169 = QCheckBox("Присылать готовность матча в Telegram (с кнопкой «Принять»)")
        self.match_ready_telegram169.setChecked(bool(self.config.get("gaming.match_ready.telegram", True)))
        for box in (self.game_launch_notice169, self.game_session_start169, self.game_session_end169,
                    self.match_ready_enabled169, self.match_ready_telegram169):
            notices_form.addRow(box)
        notices_form.addRow(self._help("Уведомления появляются в чате NEO. Готовность матча дополнительно показывается в области уведомлений Windows."))
        root.addWidget(notices)

        replay = QGroupBox("Мгновенный повтор NVIDIA")
        replay_form = QFormLayout(replay)
        self.replay_off_warning169 = QCheckBox("Предупреждать при запуске игры, если мгновенный повтор выключен")
        self.replay_off_warning169.setChecked(bool(self.config.get("gaming.notifications.replay_off_warning", True)))
        self.replay_auto_enable171 = QCheckBox("Автоматически включать мгновенный повтор при запуске игры, если он точно выключен")
        self.replay_auto_enable171.setChecked(bool(self.config.get("gaming.instant_replay.auto_enable", False)))
        self.replay_changes169 = QCheckBox("Сообщать, если мгновенный повтор включился или выключился во время игры")
        self.replay_changes169.setChecked(bool(self.config.get("gaming.notifications.replay_changes", True)))
        self.replay_unknown169 = QCheckBox("Напоминать проверить повтор, если NVIDIA не сообщает его состояние")
        self.replay_unknown169.setChecked(bool(self.config.get("gaming.notifications.replay_unknown_warning", False)))
        self.nvidia_notices184 = QCheckBox("Уведомления NVIDIA в чате (ответы на команды остаются)")
        self.nvidia_notices184.setChecked(bool(self.config.get("gaming.notifications.nvidia_enabled", True)))
        self.nvidia_startup_notice184 = QCheckBox("Сообщать об отсутствующем оверлее и результате запуска приложения NVIDIA")
        self.nvidia_startup_notice184.setChecked(bool(self.config.get("gaming.notifications.nvidia_startup", True)))
        self.nvidia_auto_launch184 = QCheckBox("Запускать установленную NVIDIA App при старте игры")
        self.nvidia_auto_launch184.setChecked(bool(self.config.get("gaming.instant_replay.auto_launch_app", False)))
        self.nvidia_command_launch185 = QCheckBox("Запускать NVIDIA App в фоне для команд повтора, клипа и записи")
        self.nvidia_command_launch185.setChecked(bool(self.config.get("gaming.instant_replay.launch_for_commands", False)))
        self.replay_hotkey169 = QLineEdit(str(self.config.get("gaming.instant_replay.toggle_hotkey", "alt+shift+f10")))
        for box in (self.nvidia_notices184, self.nvidia_startup_notice184, self.nvidia_auto_launch184, self.nvidia_command_launch185,
                    self.replay_auto_enable171, self.replay_off_warning169, self.replay_changes169, self.replay_unknown169):
            replay_form.addRow(box)
        replay_form.addRow(self._help("Для запуска приложения и повтора включите обе автоматические настройки. NEO просит Windows запустить приложение свёрнутым без активации, но NVIDIA может показать окно. Оверлей должен быть разрешён в NVIDIA; ожидание готовности — до 60 секунд. При неизвестном состоянии повтор не переключается. Автоустановка приложений и драйверов не выполняется."))
        replay_form.addRow("Горячая клавиша включения/выключения повтора", self._hotkey_controls184(self.replay_hotkey169, "alt+shift+f10"))
        self.replay_state169 = QLabel("")
        self.replay_state169.setWordWrap(True)
        check = QPushButton("Проверить состояние")
        self.replay_check184 = check
        check.clicked.connect(self._check_replay169)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(self.replay_state169, 1)
        row_layout.addWidget(check)
        replay_form.addRow(row)
        replay_form.addRow(self._help("NEO поддерживает NVIDIA App. Состояние читается только из доступного элемента видимого оверлея. Если состояние неизвестно, включение и выключение не выполняются вслепую. Для диагностики: NEO.cmd --nvidia-diagnostic."))
        root.addWidget(replay)

        # --- NVIDIA clips and recording (NEO 183: moved here from «Медиа») ---
        capture_group = QGroupBox("Клипы и запись NVIDIA")
        capture_form = QFormLayout(capture_group)
        self.quick_capture_commands = QCheckBox("Быстрые команды без wake word (клип / запись / стоп запись / скриншот)")
        self.quick_capture_commands.setChecked(bool(self.config.get("wake_word.quick_capture_enabled", False)))
        capture_form.addRow(self.quick_capture_commands)
        self.capture_clip_hotkey = QLineEdit(str(self.config.get("capture.nvidia_clip_hotkey", "alt+f10")))
        capture_form.addRow("Горячая клавиша клипа NVIDIA", self._hotkey_controls184(self.capture_clip_hotkey, "alt+f10"))
        self.capture_record_hotkey = QLineEdit(str(self.config.get("capture.nvidia_record_hotkey", "alt+f9")))
        capture_form.addRow("Горячая клавиша записи NVIDIA", self._hotkey_controls184(self.capture_record_hotkey, "alt+f9"))
        self.capture_video_folder = QLineEdit(str(self.config.get("capture.video_folder", "") or ""))
        capture_form.addRow("Папка видео NVIDIA App (пусто — папка «Видео»)", self.capture_video_folder)
        capture_form.addRow(self._help("Если в NVIDIA App выбрана другая папка сохранения, укажите её здесь. Существующий выбор папки NEO сохраняется."))
        capture_form.addRow(self._help("Длительность клипа задаётся в оверлее NVIDIA. Отправленная горячая клавиша не доказывает успешную запись: NEO подтверждает только наблюдаемое состояние или найденный стабильный файл. При неизвестном состоянии запись не переключается вслепую; очищенная клавиша отключает действие."))
        root.addWidget(capture_group)
        root.addStretch(1)
        return self._scroll(content)

    def _check_replay169(self) -> None:
        if getattr(self, "_replay_check_busy184", False) or self._settings_closed160:
            return
        from jarvis.integrations.instant_replay169 import InstantReplay
        self._replay_check_busy184 = True
        self.replay_check184.setEnabled(False)
        self.replay_state169.setText(self._t("Проверяю состояние NVIDIA…"))
        # UI Automation may wait for another process. Never run it on the GUI thread.
        worker = FunctionWorker(InstantReplay(self.config).run, "replay_status")
        worker.signals.result.connect(self._replay_state_ready184)
        worker.signals.error.connect(self._replay_state_error184)
        worker.signals.finished.connect(self._replay_state_finished184)
        QThreadPool.globalInstance().start(worker)

    def _replay_state_ready184(self, state) -> None:
        if self._settings_closed160:
            return
        from jarvis.integrations.instant_replay169 import message
        unsupported = False
        if isinstance(state, tuple) and len(state) == 3 and isinstance(state[2], dict):
            # The read-only adapter retains platform diagnostics alongside state.
            unsupported = state[2].get("error_kind") == "disabled"
            state = state[2].get("instant_replay")
        key = "unsupported" if unsupported else "on" if state is True else "off" if state is False else "unknown"
        self.replay_state169.setText(message(key, language_code(self.config)))

    def _replay_state_error184(self, _error) -> None:
        self._replay_state_ready184(None)

    def _replay_state_finished184(self) -> None:
        self._replay_check_busy184 = False
        if not self._settings_closed160:
            self.replay_check184.setEnabled(True)

    def _install_agent_model170(self) -> None:
        from jarvis.neo_llm.setup import ensure_background
        started = ensure_background(self.config, size=str(self.llm_agent_size170.currentData() or "0.5b"), force=True)
        self.llm_agent_status170.setText(self._t("Устанавливаю модель в фоне. Прогресс — в журнале NEO; можно продолжать работу.")
                                         if started else self._t("Установка уже идёт."))

    # ---- NEO 184: Settings → Перевод -----------------------------------------------------------

    _TARGETS184 = (("", "Автоматически"), ("ru", "Русский"), ("en", "Английский"), ("de", "Немецкий"), ("fr", "Французский"),
                   ("es", "Испанский"), ("it", "Итальянский"), ("pt", "Португальский"), ("uk", "Украинский"), ("pl", "Польский"),
                   ("tr", "Турецкий"), ("zh", "Китайский"), ("ja", "Японский"), ("ko", "Корейский"))

    def _translation_tab184(self) -> QWidget:
        """NEO 184: the local translation model (download / delete) and the translation options."""
        from jarvis.runtime import translation184
        content = QWidget()
        root = QVBoxLayout(content)

        model_group = QGroupBox("Локальная модель перевода")
        model_form = QFormLayout(model_group)
        self.translation_status184 = QLabel("")
        self.translation_status184.setWordWrap(True)
        self.translation_status184.setObjectName("translationStatus")
        model_form.addRow(self.translation_status184)
        self.translation_progress184 = QProgressBar()
        self.translation_progress184.setRange(0, 100)
        self.translation_progress184.setTextVisible(True)
        self.translation_progress184.setVisible(False)
        model_form.addRow(self.translation_progress184)
        self.translation_model184 = QComboBox()
        self.translation_model184.setEditable(True)
        self.translation_model184.addItem("translategemma:4b", "translategemma:4b")
        self.translation_model184.addItem("translategemma:12b", "translategemma:12b")
        current = translation184.model_name(self.config)
        index = self.translation_model184.findData(current)
        if index >= 0:
            self.translation_model184.setCurrentIndex(index)
        else:
            self.translation_model184.setEditText(current)
        self._add_field(model_form, "Модель", self.translation_model184,
                        "translategemma:4b — около 3,3 ГБ, быстрая, подходит большинству компьютеров. "
                        "translategemma:12b — около 8,1 ГБ, точнее, но медленнее и требует больше памяти.")
        buttons = QWidget()
        buttons_layout = QHBoxLayout(buttons)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        self.translation_download184 = QPushButton("Скачать модель")
        self.translation_cancel184 = QPushButton("Отменить загрузку")
        self.translation_delete184 = QPushButton("Удалить модель")
        self.translation_check184 = QPushButton("Проверить")
        self.translation_download184.clicked.connect(self._translation_download184)
        self.translation_cancel184.clicked.connect(self._translation_cancel184)
        self.translation_delete184.clicked.connect(self._translation_delete184)
        self.translation_check184.clicked.connect(lambda _checked=False: self._translation_refresh184(probe=True))
        for button in (self.translation_download184, self.translation_cancel184, self.translation_delete184, self.translation_check184):
            button.setAutoDefault(False)
            buttons_layout.addWidget(button)
        buttons_layout.addStretch(1)
        model_form.addRow(buttons)
        model_form.addRow(self._help(
            "TranslateGemma — открытая модель перевода от Google (55 языков). Она работает на этом компьютере через Ollama: "
            "текст никуда не отправляется и не расходует запросы SearchAPI. Без неё перевод идёт через SearchAPI.io и "
            "расходует бесплатные ежедневные запросы. Прогресс загрузки показывается здесь и в чате."))
        root.addWidget(model_group)

        options = QGroupBox("Как переводить")
        options_form = QFormLayout(options)
        self.translation_engine184 = QComboBox()
        self.translation_engine184.addItem("Автоматически: локальная модель или SearchAPI", "auto")
        self.translation_engine184.addItem("Только локальная модель", "local")
        self.translation_engine184.addItem("Только SearchAPI", "searchapi")
        engine_index = self.translation_engine184.findData(translation184.engine(self.config))
        self.translation_engine184.setCurrentIndex(max(0, engine_index))
        self._add_field(options_form, "Чем переводить", self.translation_engine184)
        self.translation_target184 = QComboBox()
        for code, title in self._TARGETS184:
            self.translation_target184.addItem(title, code)
        target_index = self.translation_target184.findData(str(translation184.setting(self.config, "default_target") or ""))
        self.translation_target184.setCurrentIndex(max(0, target_index))
        self._add_field(options_form, "Язык перевода по умолчанию", self.translation_target184,
                        "Используется, когда язык не назван в просьбе. «Автоматически»: на язык интерфейса, а текст на языке интерфейса — на английский.")
        self.translation_offer184 = QCheckBox("Спрашивать об установке модели, если её нет")
        self.translation_offer184.setChecked(translation184.setting(self.config, "offer_install"))
        self.translation_fallback184 = QCheckBox("Если локальная модель не ответила — переводить через SearchAPI")
        self.translation_fallback184.setChecked(translation184.setting(self.config, "fallback_to_searchapi"))
        self.translation_show184 = QCheckBox("Указывать в ответе, чем выполнен перевод")
        self.translation_show184.setChecked(translation184.setting(self.config, "show_engine"))
        for box in (self.translation_offer184, self.translation_fallback184, self.translation_show184):
            options_form.addRow(box)
        self.translation_keep184 = self._spin(0, 30, max(0, min(30, translation184.setting(self.config, "keep_alive_minutes"))), " мин")
        self._add_field(options_form, "Держать модель в памяти после перевода", self.translation_keep184,
                        "0 — выгружать сразу. Пока модель в памяти, следующий перевод начинается быстрее; потом память освобождается.")
        self.translation_limit184 = self._spin(500, 20000, max(500, min(20000, translation184.setting(self.config, "max_chars"))), "", 500)
        self._add_field(options_form, "Максимальная длина текста (символов)", self.translation_limit184,
                        "Длинный текст локальная модель переводит частями по абзацам. Через SearchAPI за раз можно не больше 3000 символов.")
        options_form.addRow(self._help(
            "Голосом: «переведи … на английский», «переведи выделенное», «установи модель перевода», "
            "«отмени загрузку модели перевода», «модель перевода установлена?»."))
        root.addWidget(options)
        root.addStretch(1)

        self._translation_installed184: bool | None = None
        # UI185-TRANSLATION-MODEL-IDENTITY-01: evidence belongs to one model name.
        self._translation_installed_by186: dict[str, bool] = {}
        self._translation_done_seen186: tuple | None = None
        self._translation_probe_generation184 = 0
        # A changed choice shows «checking» at once; the light probe waits for typing to settle.
        self._translation_probe_debounce186 = QTimer(content)
        self._translation_probe_debounce186.setSingleShot(True)
        self._translation_probe_debounce186.setInterval(400)
        self._translation_probe_debounce186.timeout.connect(lambda: self._translation_refresh184(probe=True))
        self.translation_model184.currentTextChanged.connect(
            lambda _text: (self._translation_refresh184(probe=False), self._translation_probe_debounce186.start()))
        self._translation_timer184 = QTimer(content)
        self._translation_timer184.setInterval(500)
        self._translation_timer184.timeout.connect(lambda: self._translation_refresh184(probe=False))
        self._translation_timer184.start()
        self.finished.connect(self._translation_timer184.stop)
        self._translation_refresh184(probe=True)
        return self._scroll(content)

    def _translation_selected_model184(self) -> str:
        from jarvis.runtime import translation184
        value = str(self.translation_model184.currentData() or "") if self.translation_model184.currentIndex() >= 0 else ""
        text = self.translation_model184.currentText().strip()
        return text or value or translation184.DEFAULT_MODEL

    def _translation_refresh184(self, *, probe: bool) -> None:
        """Show the model state; ``probe`` asks Ollama in the background whether the model is installed."""
        if self._settings_closed160 or not hasattr(self, "translation_status184"):
            return
        from jarvis.runtime import translation184
        snap = translation184.installer().snapshot()
        downloading = snap["state"] == "downloading"
        model = self._translation_selected_model184()
        by_model = getattr(self, "_translation_installed_by186", None)
        if by_model is None:
            by_model = self._translation_installed_by186 = {}
        if probe and not downloading:
            self._translation_probe_generation184 += 1
            generation = self._translation_probe_generation184
            probed = model
            config = self.config
            worker = FunctionWorker(lambda: translation184.installed(config, fresh=True, model=probed))
            self._translation_probe_worker184 = worker

            def done(value: object, generation: int = generation, probed: str = probed) -> None:
                if self._settings_closed160:
                    return
                if generation != self._translation_probe_generation184 and probed == self._translation_selected_model184():
                    return                       # an older probe of the same model
                by_model[probed] = bool(value)   # a late answer about A never becomes B's state
                self._translation_refresh184(probe=False)
            worker.signals.result.connect(done)
            QThreadPool.globalInstance().start(worker)
        done_key = (snap.get("state"), snap.get("model"), snap.get("done"), snap.get("total"))
        if snap["state"] == "done" and done_key != getattr(self, "_translation_done_seen186", None):
            # A finished download proves only ITS model, once; a newer check wins afterwards.
            self._translation_done_seen186 = done_key
            if snap.get("model"):
                by_model[str(snap["model"])] = True
        self._translation_installed184 = by_model.get(model)
        self.translation_progress184.setVisible(downloading)
        if downloading:
            self.translation_progress184.setValue(int(snap["percent"]))
            total = int(snap.get("total") or 0)
            if total:
                self._status_text(self.translation_status184, "Загрузка {model}: {percent}% ({done} из {total} ГБ).",
                                  model=snap.get("model") or model, percent=int(snap["percent"]),
                                  done=f"{int(snap.get('done') or 0) / 1e9:.1f}", total=f"{total / 1e9:.1f}")
            else:
                self._status_text(self.translation_status184, "Подготовка загрузки {model}…", model=snap.get("model") or model)
        elif snap["state"] == "error" and self._translation_installed184 is not True:
            self._status_text(self.translation_status184, "Не удалось загрузить модель: {error}", error=snap.get("error") or "")
        elif self._translation_installed184 is None:
            self._status_text(self.translation_status184, "Проверяю, установлена ли модель…")
        else:
            # UI185-TRANSLATION-MODE-PROMISE-01: installation is reported for the SAVED mode;
            # «Только локальная модель» never promises SearchAPI.
            engine = translation184.engine(self.config)
            fallback = bool(translation184.setting(self.config, "fallback_to_searchapi"))
            if self._translation_installed184:
                template = ("Модель {model} установлена. Сейчас выбран режим «Только SearchAPI»." if engine == "searchapi"
                            else "Модель {model} установлена: перевод работает без интернета.")
            elif engine == "local":
                template = "Модель {model} не установлена. В режиме «Только локальная модель» перевод недоступен, пока её не скачать."
            elif engine == "searchapi":
                template = "Модель {model} не установлена. Перевод выполняется через SearchAPI (выбран режим «Только SearchAPI»)."
            elif fallback:
                template = "Модель {model} не установлена: в режиме «Автоматически» перевод идёт через SearchAPI."
            else:
                template = "Модель {model} не установлена. Для локального перевода её нужно скачать."
            self._status_text(self.translation_status184, template, model=model)
        self.translation_download184.setEnabled(not downloading and self._translation_installed184 is not True)
        self.translation_cancel184.setVisible(downloading)
        self.translation_delete184.setEnabled(not downloading and self._translation_installed184 is True)
        self.translation_model184.setEnabled(not downloading)

    def _translation_download184(self) -> None:
        from jarvis.runtime import translation184
        message = translation184.installer().start(self.config, language_code(self.config), model=self._translation_selected_model184())
        self.translation_status184.setText(message)
        self._translation_refresh184(probe=False)

    def _translation_cancel184(self) -> None:
        from jarvis.runtime import translation184
        translation184.installer().cancel()
        self._translation_refresh184(probe=True)

    def _translation_delete184(self) -> None:
        from jarvis.runtime import translation184
        model = self._translation_selected_model184()
        answer = QMessageBox.question(
            self, self._t("Перевод"),
            self._t("Удалить модель {model}? Место на диске освободится; для локального перевода модель нужно будет скачать снова. Режим перевода не изменится.").format(model=model),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        config = self.config
        self.translation_delete184.setEnabled(False)
        worker = FunctionWorker(lambda: translation184.delete_model(config, model))
        self._translation_delete_worker184 = worker

        def done(result: object) -> None:
            if self._settings_closed160:
                return
            ok, error = result if isinstance(result, tuple) else (False, "")
            if ok:
                self._translation_installed_by186[model] = False
                self._translation_installed184 = False
                self._translation_refresh184(probe=False)
            else:
                self._status_text(self.translation_status184, "Не удалось удалить модель: {error}", error=error)
                self._translation_refresh184(probe=True)
        worker.signals.result.connect(done)
        QThreadPool.globalInstance().start(worker)

    def _games_tab(self) -> QWidget:
        """External API services plus game-context SearchAPI.io settings."""
        content = QWidget()
        root = QVBoxLayout(content)

        api_group = QGroupBox("API и внешние сервисы")
        api_form = QFormLayout(api_group)

        self.openweather_enabled = QCheckBox("Включить OpenWeather")
        self.openweather_enabled.setChecked(bool(self.config.get("integrations.openweather.enabled", False)))
        self.openweather_api_key = QLineEdit(unprotect_secret(str(self.config.get("integrations.openweather.api_key_encrypted", "") or "")))
        self.openweather_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openweather_city = QLineEdit(str(self.config.get("integrations.openweather.city", "") or ""))
        self.openweather_country = QLineEdit(str(self.config.get("integrations.openweather.country_code", "") or ""))
        self.openweather_country.setMaxLength(2)
        self.openweather_units = QComboBox()
        self.openweather_units.addItem("°C / metric", "metric"); self.openweather_units.addItem("°F / imperial", "imperial")
        idx = self.openweather_units.findData(str(self.config.get("integrations.openweather.units", "metric") or "metric")); self.openweather_units.setCurrentIndex(max(0, idx))
        api_form.addRow(self.openweather_enabled)
        self._add_field(api_form, "OpenWeather API key", self.openweather_api_key, "Ключ OpenWeather хранится локально в DPAPI-зашифрованном виде.")
        self._add_field(api_form, "Город погоды", self.openweather_city, "Город по умолчанию для вопросов о погоде и утренней сводки.")
        self._add_field(api_form, "Код страны", self.openweather_country, "Двухбуквенный ISO-код, например RU, NL, DE. Также используется для определения государственных праздников.")
        self._add_field(api_form, "Единицы погоды", self.openweather_units)

        root.addWidget(api_group)
        self._bind_collapsible_group(api_group, [self.openweather_enabled])

        gmail_group = QGroupBox("Gmail и Календарь")
        gmail_form = QFormLayout(gmail_group)
        self.gmail_enabled = QCheckBox("Включить интеграцию Gmail")
        self.gmail_enabled.setChecked(bool(self.config.get("integrations.gmail.enabled", False)))
        gmail_form.addRow(self.gmail_enabled)
        self.gmail_client_id = QLineEdit(unprotect_secret(str(self.config.get("integrations.gmail.client_id_encrypted", "") or "")))
        self.gmail_client_id.setPlaceholderText("OAuth Client ID типа Desktop app")
        self.gmail_client_secret = QLineEdit(unprotect_secret(str(self.config.get("integrations.gmail.client_secret_encrypted", "") or "")))
        self.gmail_client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.gmail_client_secret.setPlaceholderText("Client secret из Google Cloud")
        # NEO 184: the staged Google tokens belong to this Client ID (Gmail and Calendar share it).
        self._google_token_client184 = self.gmail_client_id.text().strip()
        self._google_auth_generation184 = {"gmail": 0, "calendar": 0}
        self.gmail_client_id.textChanged.connect(self._google_client_changed184)
        self.gmail_client_secret.textChanged.connect(self._google_client_changed184)
        self._add_field(gmail_form, "OAuth Client ID", self.gmail_client_id, "Создайте OAuth 2.0 Client ID типа Desktop app в Google Cloud. Пароль Gmail NEO не запрашивает.")
        self._add_field(gmail_form, "OAuth Client secret", self.gmail_client_secret, "Хранится локально в DPAPI-зашифрованном виде. Для Desktop app это часть OAuth-конфигурации, а не пароль Gmail.")
        gmail_buttons = QWidget(); gmail_buttons_layout = QHBoxLayout(gmail_buttons); gmail_buttons_layout.setContentsMargins(0,0,0,0); gmail_buttons_layout.setSpacing(8)
        self.gmail_authorize_button = QPushButton("Авторизовать Gmail")
        self.gmail_test_button = QPushButton("Проверить")
        self.gmail_disconnect_button = QPushButton("Сбросить OAuth")
        self.gmail_authorize_button.clicked.connect(self._authorize_gmail_async)
        self.gmail_test_button.clicked.connect(self._test_gmail_async)
        self.gmail_disconnect_button.clicked.connect(self._clear_gmail_auth)
        gmail_buttons_layout.addWidget(self.gmail_authorize_button); gmail_buttons_layout.addWidget(self.gmail_test_button); gmail_buttons_layout.addWidget(self.gmail_disconnect_button); gmail_buttons_layout.addStretch(1)
        gmail_form.addRow("Подключение", gmail_buttons)
        _gmail_authorized = bool(self._gmail_tokens.get("refresh_token"))
        _gmail_account = str(self._gmail_tokens.get("account_email", "") or "").strip()
        self.gmail_status_label = QLabel("")
        self.gmail_status_label.setWordWrap(True)
        if _gmail_authorized and _gmail_account:
            self._status_text(self.gmail_status_label, "OAuth подключён · {account}", account=_gmail_account)
        else:
            self._status_text(self.gmail_status_label, "OAuth подключён" if _gmail_authorized else "OAuth не подключён")
        gmail_form.addRow("Статус", self.gmail_status_label)
        # HOTFIX96: Calendar shares the exact same Desktop OAuth Client ID /
        # Client Secret shown above. Calendar keeps a separate refresh token
        # because its OAuth scope is different, but there is no duplicate app
        # credential field anywhere in Settings.
        self.google_calendar_enabled = QCheckBox("Включить Google Calendar")
        self.google_calendar_enabled.setChecked(bool(self.config.get("integrations.google_calendar.enabled", False)))
        gmail_form.addRow(self.google_calendar_enabled)
        cal_buttons=QWidget(); cal_layout=QHBoxLayout(cal_buttons); cal_layout.setContentsMargins(0,0,0,0); cal_layout.setSpacing(8)
        self.google_calendar_authorize_button=QPushButton("Авторизовать Calendar"); self.google_calendar_authorize_button.clicked.connect(self._authorize_calendar_async)
        self.google_calendar_test_button=QPushButton("Проверить Calendar"); self.google_calendar_test_button.clicked.connect(self._test_calendar_async)
        self.google_calendar_reset_button=QPushButton("Сбросить Calendar OAuth"); self.google_calendar_reset_button.clicked.connect(self._clear_calendar_auth)
        cal_layout.addWidget(self.google_calendar_authorize_button); cal_layout.addWidget(self.google_calendar_test_button); cal_layout.addWidget(self.google_calendar_reset_button); cal_layout.addStretch(1)
        gmail_form.addRow("Google Calendar",cal_buttons)
        self.google_calendar_status_label=QLabel("Calendar OAuth подключён" if self._calendar_tokens.get("refresh_token") else "Calendar OAuth не подключён")
        self.google_calendar_status_label.setWordWrap(True); gmail_form.addRow("Статус Calendar",self.google_calendar_status_label)
        self.google_calendar_briefing = QCheckBox("Добавлять события Google Calendar в сводку на день")
        self.google_calendar_briefing.setChecked(bool(self.config.get("integrations.google_calendar.include_in_briefing", False)))
        gmail_form.addRow(self.google_calendar_briefing)
        self.google_calendar_briefing_period=QComboBox()
        self.google_calendar_briefing_period.addItem("Сегодняшние события","today")
        self.google_calendar_briefing_period.addItem("Ближайшие 3 дня","3_days")
        self.google_calendar_briefing_period.addItem("Ближайшие 31 день","month")
        _calp=self.google_calendar_briefing_period.findData(str(self.config.get("integrations.google_calendar.briefing_period","today") or "today")); self.google_calendar_briefing_period.setCurrentIndex(max(0,_calp))
        self._add_field(gmail_form,"События в сводке",self.google_calendar_briefing_period,"Период Calendar, который будет показан в стартовой сводке NEO.")
        self.gmail_include_inbox = QCheckBox("Использовать папку «Входящие»")
        self.gmail_include_inbox.setChecked(bool(self.config.get("integrations.gmail.include_inbox", True)))
        gmail_form.addRow(self.gmail_include_inbox)
        gmail_extra_raw = self.config.get("integrations.gmail.additional_folders", []) or []
        gmail_extra_text = "; ".join(str(x) for x in gmail_extra_raw) if isinstance(gmail_extra_raw, (list, tuple)) else str(gmail_extra_raw)
        self.gmail_additional_folders = QLineEdit(gmail_extra_text)
        self.gmail_additional_folders.setPlaceholderText("Проекты; Рассылки; Отправленные себе")
        self._add_field(gmail_form, "Дополнительные папки / ярлыки", self.gmail_additional_folders, "Введите названия через ;. Gmail: поддерживаются пользовательские ярлыки, а также Рассылки/Промоакции, Соцсети, Обновления, Форумы, Отправленные и Отправленные себе.")
        self.gmail_notifications_enabled = QCheckBox("Показывать уведомления о новых письмах")
        self.gmail_notifications_enabled.setChecked(bool(self.config.get("integrations.gmail.notifications_enabled", True)))
        self.gmail_notify_total_count = QCheckBox("Показывать общее количество писем в выбранных папках")
        self.gmail_notify_total_count.setChecked(bool(self.config.get("integrations.gmail.notify_total_count", False)))
        self.gmail_notify_unread_count = QCheckBox("Показывать общее количество непрочитанных")
        self.gmail_notify_unread_count.setChecked(bool(self.config.get("integrations.gmail.notify_unread_count", True)))
        self.gmail_notify_recent24h_count = QCheckBox("Показывать количество писем за последние 24 часа")
        self.gmail_notify_recent24h_count.setChecked(bool(self.config.get("integrations.gmail.notify_recent24h_count", True)))
        gmail_form.addRow(self.gmail_notifications_enabled); gmail_form.addRow(self.gmail_notify_total_count); gmail_form.addRow(self.gmail_notify_unread_count); gmail_form.addRow(self.gmail_notify_recent24h_count)
        self.gmail_notification_detail = QComboBox()
        self.gmail_notification_detail.addItem("Только счётчик", "count_only")
        self.gmail_notification_detail.addItem("Отправитель", "sender")
        self.gmail_notification_detail.addItem("Отправитель + тема", "sender_subject")
        self.gmail_notification_detail.addItem("Отправитель + содержимое", "sender_preview")
        self.gmail_notification_detail.addItem("Отправитель + тема + содержимое", "sender_subject_preview")
        idx = self.gmail_notification_detail.findData(str(self.config.get("integrations.gmail.message_display_detail", "sender_subject") or "sender_subject")); self.gmail_notification_detail.setCurrentIndex(max(0,idx))
        self._add_field(gmail_form, "Формат показа писем", self.gmail_notification_detail, "Используется для уведомлений и списков писем. Содержимое — короткий безопасный preview без вложений.")
        self.gmail_poll_seconds = self._spin(30, 900, int(self.config.get("integrations.gmail.poll_seconds", 60) or 60), " с", 10)
        self._add_field(gmail_form, "Проверять почту", self.gmail_poll_seconds, "Фоновая проверка выполняется в worker-потоке и не блокирует микрофон/UI.")
        self.gmail_include_in_briefing = QCheckBox("Добавлять Gmail в сводку на день")
        self.gmail_include_in_briefing.setChecked(bool(self.config.get("integrations.gmail.include_in_briefing", True)))
        self.gmail_briefing_show_senders = QCheckBox("В сводке показывать отправителей непрочитанных писем")
        self.gmail_briefing_show_senders.setChecked(bool(self.config.get("integrations.gmail.briefing_show_senders", False)))
        self.gmail_briefing_show_folders = QCheckBox("В сводке показывать разбивку по папкам")
        self.gmail_briefing_show_folders.setChecked(bool(self.config.get("integrations.gmail.briefing_show_folders", True)))
        gmail_form.addRow(self.gmail_include_in_briefing); gmail_form.addRow(self.gmail_briefing_show_senders); gmail_form.addRow(self.gmail_briefing_show_folders)
        root.addWidget(gmail_group)
        self._bind_collapsible_group(gmail_group, [self.gmail_enabled, self.google_calendar_enabled])

        mailru_group = QGroupBox("Mail.ru")
        mailru_form = QFormLayout(mailru_group)
        self.mailru_enabled = QCheckBox("Включить интеграцию Mail.ru")
        self.mailru_enabled.setChecked(bool(self.config.get("integrations.mailru.enabled", False)))
        mailru_form.addRow(self.mailru_enabled)
        self.mailru_email = QLineEdit(str(self.config.get("integrations.mailru.email", "") or "")); self.mailru_email.setPlaceholderText("name@mail.ru")
        self.mailru_app_password = QLineEdit(unprotect_secret(str(self.config.get("integrations.mailru.app_password_encrypted", "") or ""))); self.mailru_app_password.setEchoMode(QLineEdit.EchoMode.Password); self.mailru_app_password.setPlaceholderText("Пароль для внешнего приложения")
        self._add_field(mailru_form, "Адрес Mail.ru", self.mailru_email)
        self._add_field(mailru_form, "Пароль приложения", self.mailru_app_password, "Используется только пароль для внешнего приложения Mail.ru. Он хранится через Windows DPAPI; обычный пароль ящика сюда вводить не нужно.")
        mailru_buttons = QWidget(); mailru_buttons_layout = QHBoxLayout(mailru_buttons); mailru_buttons_layout.setContentsMargins(0,0,0,0); mailru_buttons_layout.setSpacing(8)
        self.mailru_test_button = QPushButton("Проверить IMAP/SMTP")
        self.mailru_test_button.clicked.connect(self._test_mailru_async)
        mailru_buttons_layout.addWidget(self.mailru_test_button); mailru_buttons_layout.addStretch(1)
        mailru_form.addRow("Подключение", mailru_buttons)
        self.mailru_status_label = QLabel("IMAP: imap.mail.ru:993 · SMTP: smtp.mail.ru:465")
        self.mailru_status_label.setWordWrap(True); mailru_form.addRow("Статус", self.mailru_status_label)
        self.mailru_include_inbox = QCheckBox("Использовать папку «Входящие»")
        self.mailru_include_inbox.setChecked(bool(self.config.get("integrations.mailru.include_inbox", True)))
        mailru_form.addRow(self.mailru_include_inbox)
        mailru_extra_raw = self.config.get("integrations.mailru.additional_folders", []) or []
        mailru_extra_text = "; ".join(str(x) for x in mailru_extra_raw) if isinstance(mailru_extra_raw, (list, tuple)) else str(mailru_extra_raw)
        self.mailru_additional_folders = QLineEdit(mailru_extra_text)
        self.mailru_additional_folders.setPlaceholderText("Проекты; Рассылки; Отправленные")
        self._add_field(mailru_form, "Дополнительные папки", self.mailru_additional_folders, "Введите точные названия IMAP-папок Mail.ru через ;. Поддерживаются в том числе русские названия и вложенные папки.")
        self.mailru_notifications_enabled = QCheckBox("Показывать уведомления о новых письмах")
        self.mailru_notifications_enabled.setChecked(bool(self.config.get("integrations.mailru.notifications_enabled", True)))
        self.mailru_notify_total_count = QCheckBox("Показывать общее количество писем в выбранных папках")
        self.mailru_notify_total_count.setChecked(bool(self.config.get("integrations.mailru.notify_total_count", False)))
        self.mailru_notify_unread_count = QCheckBox("Показывать общее количество непрочитанных")
        self.mailru_notify_unread_count.setChecked(bool(self.config.get("integrations.mailru.notify_unread_count", True)))
        self.mailru_notify_recent24h_count = QCheckBox("Показывать количество писем за последние 24 часа")
        self.mailru_notify_recent24h_count.setChecked(bool(self.config.get("integrations.mailru.notify_recent24h_count", True)))
        mailru_form.addRow(self.mailru_notifications_enabled); mailru_form.addRow(self.mailru_notify_total_count); mailru_form.addRow(self.mailru_notify_unread_count); mailru_form.addRow(self.mailru_notify_recent24h_count)
        self.mailru_notification_detail = QComboBox()
        self.mailru_notification_detail.addItem("Только счётчик", "count_only")
        self.mailru_notification_detail.addItem("Отправитель", "sender")
        self.mailru_notification_detail.addItem("Отправитель + тема", "sender_subject")
        self.mailru_notification_detail.addItem("Отправитель + содержимое", "sender_preview")
        self.mailru_notification_detail.addItem("Отправитель + тема + содержимое", "sender_subject_preview")
        idx = self.mailru_notification_detail.findData(str(self.config.get("integrations.mailru.message_display_detail", "sender_subject") or "sender_subject")); self.mailru_notification_detail.setCurrentIndex(max(0,idx))
        self._add_field(mailru_form, "Формат показа писем", self.mailru_notification_detail, "Используется для уведомлений и списков писем. Содержимое показывает только короткий preview начала письма.")
        self.mailru_poll_seconds = self._spin(30, 900, int(self.config.get("integrations.mailru.poll_seconds", 60) or 60), " с", 10)
        self._add_field(mailru_form, "Проверять почту", self.mailru_poll_seconds)
        self.mailru_include_in_briefing = QCheckBox("Добавлять Mail.ru в сводку на день")
        self.mailru_include_in_briefing.setChecked(bool(self.config.get("integrations.mailru.include_in_briefing", True)))
        self.mailru_briefing_show_senders = QCheckBox("В сводке показывать отправителей непрочитанных писем")
        self.mailru_briefing_show_senders.setChecked(bool(self.config.get("integrations.mailru.briefing_show_senders", False)))
        self.mailru_briefing_show_folders = QCheckBox("В сводке показывать разбивку по папкам")
        self.mailru_briefing_show_folders.setChecked(bool(self.config.get("integrations.mailru.briefing_show_folders", True)))
        mailru_form.addRow(self.mailru_include_in_briefing); mailru_form.addRow(self.mailru_briefing_show_senders); mailru_form.addRow(self.mailru_briefing_show_folders)
        root.addWidget(mailru_group)
        self._bind_collapsible_group(mailru_group, [self.mailru_enabled])

        web_group = QGroupBox("Игровой веб-поиск — Google AI Mode через SearchAPI.io")
        web_form = QFormLayout(web_group)
        self.game_web_enabled = QCheckBox("Включить игровой веб-поиск Google AI Mode через SearchAPI.io")
        self.game_web_enabled.setChecked(bool(self.config.get("gaming.web_knowledge.enabled", True)))
        web_form.addRow(self.game_web_enabled)

        self.game_web_searchapi_key = QLineEdit(
            unprotect_secret(str(self.config.get("gaming.web_knowledge.searchapi_api_key_encrypted", "") or ""))
        )
        self.game_web_searchapi_key.setEchoMode(QLineEdit.Password)
        self.game_web_searchapi_key.setPlaceholderText("API key из searchapi.io")
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(8)
        key_layout.addWidget(self.game_web_searchapi_key, 1)
        self.game_web_test_searchapi = QPushButton("Проверить ключ")
        self.game_web_test_searchapi.setToolTip("Проверить ключ через лёгкий SearchAPI Account API (/api/v1/me), без Google-поиска.")
        self.game_web_test_searchapi.clicked.connect(self._test_searchapi_key)
        key_layout.addWidget(self.game_web_test_searchapi)
        self.game_web_open_searchapi = QPushButton("Открыть SearchAPI")
        self.game_web_open_searchapi.setToolTip("Открыть searchapi.io для создания API key и просмотра Usage.")
        self.game_web_open_searchapi.clicked.connect(self._open_searchapi_dashboard)
        key_layout.addWidget(self.game_web_open_searchapi)
        self._add_field(
            web_form, "SearchAPI.io API key", key_row,
            "Обязателен для Google AI Mode. На Windows ключ сохраняется через DPAPI и не выводится в лог/URL. "
            "Переменная SEARCHAPI_API_KEY поддерживается только как fallback, если поле пустое.",
        )
        self.game_web_searchapi_status = QLabel("Ключ ещё не проверялся в этой сессии.")
        self.game_web_searchapi_status.setWordWrap(True)
        web_form.addRow("Проверка", self.game_web_searchapi_status)

        self.game_web_searchapi_gl = QComboBox()
        for label, code in (("США (us) — рекомендуемый стабильный AI Mode", "us"), ("Нидерланды (nl)", "nl"),
                            ("Германия (de)", "de"), ("Великобритания (gb)", "gb")):
            self.game_web_searchapi_gl.addItem(label, code)
        gl = str(self.config.get("gaming.web_knowledge.searchapi_gl", "us") or "us").casefold()
        idx = self.game_web_searchapi_gl.findData(gl)
        if idx < 0:
            self.game_web_searchapi_gl.addItem(f"Пользовательский ({gl})", gl)
            idx = self.game_web_searchapi_gl.count() - 1
        self.game_web_searchapi_gl.setCurrentIndex(idx)
        self._add_field(
            web_form, "Регион Google", self.game_web_searchapi_gl,
            "Параметр gl задаёт регион выдачи Google. Язык AI-ответа определяется самим текстом вопроса; для русских вопросов ответ остаётся русским.",
        )

        self.game_web_searchapi_sources = self._spin(
            1, 20, int(self.config.get("gaming.web_knowledge.searchapi_max_sources", 8) or 8), " источн.", 1,
        )
        self._add_field(
            web_form, "Сохранять источников", self.game_web_searchapi_sources,
            "Сколько ссылок Google AI Mode сохранять рядом с ответом в локальном кэше. Это не увеличивает число поисковых запросов.",
        )
        self.game_web_show_sources = QCheckBox("Показывать ссылки под ответами на общие вопросы")
        self.game_web_show_sources.setChecked(bool(self.config.get("gaming.web_knowledge.show_sources", False)))
        self.game_web_show_sources.setToolTip("Выключено по умолчанию. Касается общих вопросов из интернета; ответы об играх всегда показывают свои источники. Происхождение ответа (SearchAPI.io) указывается всегда, источники сохраняются в кэше для диагностики.")
        web_form.addRow(self.game_web_show_sources)

        self.game_web_searchapi_timeout = self._spin(
            6, 60, int(round(float(self.config.get("gaming.web_knowledge.searchapi_timeout_seconds", 18.0) or 18.0))), " с", 1,
        )
        self._add_field(
            web_form, "Таймаут Google AI Mode", self.game_web_searchapi_timeout,
            "Максимальное ожидание одного HTTPS-запроса. Chromium/Edge, CDP и Google-профиль для Game Web не запускаются.",
        )

        self.help_confirm172 = QCheckBox("Спрашивать перед отправкой текста с экрана в поиск («что за ошибка?»)")
        self.help_confirm172.setChecked(bool(self.config.get("help.confirm_before_search", True)))
        self.help_vision172 = QCheckBox("Если в окне нет текста, описывать его моделью анализа экрана (выгружается сразу после)")
        self.help_vision172.setChecked(bool(self.config.get("help.use_screen_model", True)))
        web_form.addRow(self.help_confirm172)
        web_form.addRow(self.help_vision172)
        self.game_web_structured171 = QCheckBox("Короткий ответ: только главное по вашему вопросу (полностью — «подробнее»)")
        self.game_web_structured171.setChecked(bool(self.config.get("gaming.web_knowledge.structured_answers", True)))
        web_form.addRow(self.game_web_structured171)
        self.game_web_suggest_followup = QCheckBox("Предлагать в конце ответа, что ещё найти (отзывы, другой год, цену…)")
        self.game_web_suggest_followup.setChecked(bool(self.config.get("gaming.web_knowledge.suggest_followup", True)))
        self.game_web_suggest_followup.setToolTip("Например: «Хотите, чтобы я нашёл отзывы об этой модели или характеристики другого года?». Ответьте «да», «отзывы», «второе» или год — NEO выполнит этот поиск.")
        web_form.addRow(self.game_web_suggest_followup)

        self.game_web_cache_ttl = self._spin(
            1, 720, int(self.config.get("gaming.web_knowledge.cache_ttl_hours", 72) or 72), " ч", 1,
        )
        self._add_field(web_form, "Кэш обычных вопросов", self.game_web_cache_ttl,
                        "Сколько хранить ответы по стабильным механикам до повторного запроса Google AI Mode.")

        self.game_web_dynamic_ttl = self._spin(
            5, 720, int(self.config.get("gaming.web_knowledge.dynamic_cache_ttl_minutes", 60) or 60), " мин", 5,
        )
        self._add_field(web_form, "Кэш актуальных/патчевых вопросов", self.game_web_dynamic_ttl,
                        "Для запросов «сейчас», «последний патч», «актуальная мета» используется короткий TTL.")

        self.game_web_cache_max_mb = self._spin(
            10, 1024, int(self.config.get("gaming.web_knowledge.cache_max_mb", 100) or 100), " МБ", 10,
        )
        self._add_field(web_form, "Максимальный размер кэша", self.game_web_cache_max_mb,
                        "Кэш хранит только итоговый Google AI ответ и сведения об источниках, а не игровые базы или полные сайты.")

        cache_row = QWidget()
        cache_layout = QHBoxLayout(cache_row)
        cache_layout.setContentsMargins(0, 0, 0, 0)
        cache_layout.setSpacing(8)
        self.game_web_cache_stats = QLabel("")
        self.game_web_cache_stats.setWordWrap(True)
        clear_cache = QPushButton("Очистить игровой кэш")
        clear_cache.clicked.connect(self._clear_game_web_cache)
        cache_layout.addWidget(self.game_web_cache_stats, 1)
        cache_layout.addWidget(clear_cache)
        web_form.addRow("Локальная библиотека ответов", cache_row)
        self._refresh_game_web_cache_stats()

        hint = QLabel(
            "NEO хранит только активную игровую сессию и локальный кэш Google AI ответов. "
            "Вопрос без названия игры относится к текущей запущенной игре; явно указанная другая игра используется как есть. "
            "На cache miss выполняется один SearchAPI.io Google AI Mode запрос. NEO показывает markdown/text_blocks Google напрямую: "
            "локальная Qwen не переписывает игровые факты и не запускается для web follow-up."
        )
        hint.setWordWrap(True)
        web_form.addRow(hint)
        root.addWidget(web_group)
        self._bind_collapsible_group(web_group, [self.game_web_enabled])
        root.addStretch(1)
        return self._scroll(content)


    # ---- NEO 184: Google OAuth (Gmail and Calendar share one Client ID) ----------------------------

    def _google_bump184(self, provider: str) -> int:
        generations = self._google_auth_generation184
        generations[provider] = int(generations.get(provider, 0)) + 1
        return generations[provider]

    def _google_stale184(self, provider: str, generation: int) -> bool:
        """A result of a closed window, a cleared connection or an older attempt changes nothing."""
        if bool(getattr(self, "_settings_closed160", False)):
            return True
        return generation != int(self._google_auth_generation184.get(provider, 0))

    def _google_client_changed184(self, *_args) -> None:
        # A different OAuth app: answers still on their way belong to the old one.
        for provider in ("gmail", "calendar"):
            self._google_bump184(provider)
        has_tokens = bool(self._gmail_tokens.get("refresh_token") or self._calendar_tokens.get("refresh_token"))
        if self.gmail_client_id.text().strip() != self._google_token_client184 and has_tokens:
            self._status_text(self.gmail_status_label, "Client ID изменён: после сохранения авторизуйте Gmail и Calendar заново.")
            self._google_client_warned186 = True
        elif getattr(self, "_google_client_warned186", False):
            # UI185-ACCOUNT-CLIENT-REVERT-STATUS-01: back to the Client ID the tokens belong to —
            # the warning goes; the tokens themselves were never touched.
            self._google_client_warned186 = False
            account = str(self._gmail_tokens.get("account_email", "") or "").strip()
            if self._gmail_tokens.get("refresh_token"):
                if account:
                    self._status_text(self.gmail_status_label, "OAuth подключён · {account}", account=account)
                else:
                    self._status_text(self.gmail_status_label, "OAuth подключён")
            else:
                self._status_text(self.gmail_status_label, "OAuth не подключён")

    def _google_message184(self, kind: str, title: str, template: str, **values: object) -> None:
        text = self._t(template).format(**values)
        (QMessageBox.information if kind == "information" else QMessageBox.warning)(self, title, text)

    @staticmethod
    def _oauth_error_text184(trace: object) -> str:
        raw = str(trace or "OAuth").strip()
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        message = lines[-1] if lines else raw
        message = re.sub(r"^(?:RuntimeError|ValueError|TimeoutError|\w+Error):\s*", "", message)
        return safe_error(message)[-1800:]

    def _authorize_calendar_async(self) -> None:
        client_id = self.gmail_client_id.text().strip()
        client_secret = self.gmail_client_secret.text().strip()
        if not client_id:
            QMessageBox.warning(self, "Google Calendar", self._t("Укажите OAuth Client ID в разделе «Gmail и Календарь»."))
            return
        if getattr(self, "_calendar_auth_worker", None) is not None:
            return
        generation = self._google_bump184("calendar")
        self.google_calendar_authorize_button.setEnabled(False)
        self.google_calendar_authorize_button.setText(self._t("Жду Google…"))
        self._status_text(self.google_calendar_status_label, "Открываю браузер для входа в Google… Разрешите доступ к Google Calendar и вернитесь сюда.")
        worker = FunctionWorker(authorize_google_calendar, client_id, client_secret, timeout_seconds=240)
        self._calendar_auth_worker = worker

        def ok(result: object, generation: int = generation, client_id: str = client_id) -> None:
            if self._google_stale184("calendar", generation):
                logging.getLogger(__name__).info("Google Calendar OAuth result ignored (cancelled, cleared or replaced)")
                return
            info = dict(result or {}) if isinstance(result, dict) else {}
            self._calendar_tokens.update({
                "refresh_token": str(info.get("refresh_token", "") or self._calendar_tokens.get("refresh_token", "")),
                "access_token": str(info.get("access_token", "") or ""),
                "expires_at": float(info.get("expires_at", 0.0) or 0.0)})
            self._google_token_client184 = client_id
            self.google_calendar_enabled.setChecked(True)
            self._status_text(self.google_calendar_status_label, "OAuth Calendar получен. Нажмите Применить/Сохранить.")
            self._google_message184("information", "Google Calendar", "Google Calendar авторизован.")

        def failed(trace: object, generation: int = generation) -> None:
            if self._google_stale184("calendar", generation):
                return
            self._status_text(self.google_calendar_status_label, "Авторизация не завершена")
            QMessageBox.warning(self, "Google Calendar", self._oauth_error_text184(trace))

        def done() -> None:
            self._calendar_auth_worker = None
            if bool(getattr(self, "_settings_closed160", False)):
                return
            self.google_calendar_authorize_button.setEnabled(True)
            self.google_calendar_authorize_button.setText(self._t("Авторизовать Calendar"))
        worker.signals.result.connect(ok)
        worker.signals.error.connect(failed)
        worker.signals.finished.connect(done)
        QThreadPool.globalInstance().start(worker)

    def _test_calendar_async(self) -> None:
        token = str(self._calendar_tokens.get("access_token", "") or "")
        if not token:
            QMessageBox.warning(self, "Google Calendar", self._t("Сначала авторизуйте Calendar."))
            return
        self.google_calendar_test_button.setEnabled(False)
        self.google_calendar_test_button.setText(self._t("Проверяю…"))

        def probe(access: str) -> dict[str, Any]:
            r = requests.get("https://www.googleapis.com/calendar/v3/users/me/calendarList", params={"maxResults": 1},
                             headers={"Authorization": f"Bearer {access}"}, timeout=12)
            if r.status_code >= 400:
                raise RuntimeError(f"Google Calendar HTTP {r.status_code}: {r.text[:500]}")
            return {"ok": True}
        worker = FunctionWorker(probe, token)
        self._calendar_test_worker = worker
        worker.signals.result.connect(lambda _r: None if self._settings_closed160 else self._google_message184(
            "information", "Google Calendar", "Подключение Google Calendar работает."))
        worker.signals.error.connect(lambda trace: None if self._settings_closed160 else QMessageBox.warning(
            self, "Google Calendar", self._oauth_error_text184(trace)))

        def done() -> None:
            self._calendar_test_worker = None
            if not self._settings_closed160:
                self.google_calendar_test_button.setEnabled(True)
                self.google_calendar_test_button.setText(self._t("Проверить"))
        worker.signals.finished.connect(done)
        QThreadPool.globalInstance().start(worker)

    def _clear_calendar_auth(self) -> None:
        self._google_bump184("calendar")          # an answer still on its way cannot bring the tokens back
        self._calendar_tokens = {"refresh_token": "", "access_token": "", "expires_at": 0.0}
        self.google_calendar_enabled.setChecked(False)
        self._status_text(self.google_calendar_status_label, "OAuth-токены будут удалены после сохранения.")

    def _authorize_gmail_async(self) -> None:
        client_id = self.gmail_client_id.text().strip()
        client_secret = self.gmail_client_secret.text().strip()
        if not client_id:
            QMessageBox.warning(self, "Gmail", self._t("Сначала укажите OAuth Client ID типа Desktop app."))
            return
        if getattr(self, "_gmail_auth_worker", None) is not None:
            return
        generation = self._google_bump184("gmail")
        self.gmail_authorize_button.setEnabled(False)
        self.gmail_authorize_button.setText(self._t("Жду Google…"))
        self._status_text(self.gmail_status_label, "Открываю браузер для входа в Google… Разрешите NEO доступ к Gmail и вернитесь сюда.")
        worker = FunctionWorker(authorize_gmail, client_id, client_secret, timeout_seconds=240)
        self._gmail_auth_worker = worker
        worker.signals.result.connect(lambda result, g=generation, c=client_id: self._gmail_authorized(result, g, c))
        worker.signals.error.connect(lambda trace, g=generation: self._gmail_auth_failed(trace, g))
        worker.signals.finished.connect(self._gmail_auth_finished)
        QThreadPool.globalInstance().start(worker)

    def _gmail_authorized(self, result: object, generation: int | None = None, client_id: str = "") -> None:
        if generation is not None and self._google_stale184("gmail", generation):
            logging.getLogger(__name__).info("Gmail OAuth result ignored (cancelled, cleared or replaced)")
            return
        info = dict(result or {}) if isinstance(result, dict) else {}
        if not info.get("refresh_token") and self._gmail_tokens.get("refresh_token"):
            info["refresh_token"] = self._gmail_tokens.get("refresh_token")
        self._gmail_tokens.update({
            "refresh_token": str(info.get("refresh_token", "") or ""),
            "access_token": str(info.get("access_token", "") or ""),
            "expires_at": float(info.get("expires_at", 0.0) or 0.0),
            "account_email": str(info.get("account_email", "") or ""),
        })
        if client_id:
            self._google_token_client184 = client_id
        account = self._gmail_tokens.get("account_email") or "Google account"
        self._status_text(self.gmail_status_label, "Подключён: {account}. Нажмите «Применить» или «Сохранить», чтобы сохранить токены через DPAPI.", account=account)
        self.gmail_enabled.setChecked(True)
        self._google_message184("information", "Gmail", "Gmail авторизован: {account}.", account=account)

    def _gmail_auth_failed(self, trace: str, generation: int | None = None) -> None:
        if generation is not None and self._google_stale184("gmail", generation):
            return
        self._status_text(self.gmail_status_label, "Авторизация Gmail не завершена.")
        # FunctionWorker returns a traceback, but settings should present only
        # the actionable final exception instead of an internal Python stack.
        QMessageBox.warning(self, "Gmail", self._oauth_error_text184(trace))

    def _gmail_auth_finished(self) -> None:
        self._gmail_auth_worker = None
        if bool(getattr(self, "_settings_closed160", False)):
            return
        self.gmail_authorize_button.setEnabled(True)
        self.gmail_authorize_button.setText(self._t("Авторизовать Gmail"))

    def _clear_gmail_auth(self) -> None:
        self._google_bump184("gmail")             # an answer still on its way cannot bring the tokens back
        self._gmail_tokens = {"refresh_token": "", "access_token": "", "expires_at": 0.0, "account_email": ""}
        self.gmail_enabled.setChecked(False)
        self._status_text(self.gmail_status_label, "OAuth-токены будут удалены после «Применить»/«Сохранить».")

    def _test_gmail_async(self) -> None:
        client_id = self.gmail_client_id.text().strip()
        client_secret = self.gmail_client_secret.text().strip()
        refresh = str(self._gmail_tokens.get("refresh_token", "") or "")
        access = str(self._gmail_tokens.get("access_token", "") or "")
        expiry = float(self._gmail_tokens.get("expires_at", 0.0) or 0.0)
        if not client_id or not refresh:
            QMessageBox.warning(self, "Gmail", self._t("Сначала выполните «Авторизовать Gmail»."))
            return
        self.gmail_test_button.setEnabled(False); self.gmail_test_button.setText(self._t("Проверяю…"))
        worker = FunctionWorker(test_gmail, client_id, client_secret, refresh, access, expiry)
        self._gmail_test_worker = worker
        worker.signals.result.connect(self._gmail_test_ok)
        worker.signals.error.connect(lambda trace: None if self._settings_closed160 else QMessageBox.warning(self, "Gmail", self._oauth_error_text184(trace)))
        worker.signals.finished.connect(self._gmail_test_finished)
        QThreadPool.globalInstance().start(worker)

    def _gmail_test_ok(self, result: object) -> None:
        if bool(getattr(self, "_settings_closed160", False)):
            return
        info = dict(result or {}) if isinstance(result, dict) else {}
        email = str(info.get("email", "") or self._gmail_tokens.get("account_email", "") or "Gmail")
        if email and email != "Gmail": self._gmail_tokens["account_email"] = email
        self._status_text(self.gmail_status_label, "Подключение работает: {email}.", email=email)
        self._google_message184("information", "Gmail", "Подключение Gmail работает: {email}.", email=email)

    def _gmail_test_finished(self) -> None:
        self._gmail_test_worker = None
        if bool(getattr(self, "_settings_closed160", False)):
            return
        self.gmail_test_button.setEnabled(True); self.gmail_test_button.setText(self._t("Проверить"))

    def _test_mailru_async(self) -> None:
        email = self.mailru_email.text().strip()
        password = self.mailru_app_password.text().strip()
        if not email or not password:
            QMessageBox.warning(self, "Mail.ru", "Укажите адрес Mail.ru и пароль для внешнего приложения.")
            return
        self.mailru_test_button.setEnabled(False); self.mailru_test_button.setText(self._t("Проверяю…"))
        worker = FunctionWorker(test_mailru, email, password)
        self._mailru_test_worker = worker
        worker.signals.result.connect(self._mailru_test_ok)
        worker.signals.error.connect(lambda trace: QMessageBox.warning(self, "Mail.ru", str(trace or "Ошибка")[-1800:]))
        worker.signals.finished.connect(self._mailru_test_finished)
        QThreadPool.globalInstance().start(worker)

    def _mailru_test_ok(self, result: object) -> None:
        info = dict(result or {}) if isinstance(result, dict) else {}
        self._status_text(self.mailru_status_label, "Подключение работает: {email} · входящих {total} · непрочитанных {unread}", email=info.get("email", self.mailru_email.text().strip()), total=int(info.get("total", 0) or 0), unread=int(info.get("unread", 0) or 0))
        self.mailru_enabled.setChecked(True)
        QMessageBox.information(self, "Mail.ru", "Подключение Mail.ru работает: IMAP и SMTP успешно авторизованы.")

    def _mailru_test_finished(self) -> None:
        self.mailru_test_button.setEnabled(True); self.mailru_test_button.setText(self._t("Проверить IMAP/SMTP"))
        self._mailru_test_worker = None

    def _refresh_game_web_cache_stats(self) -> None:
        """Keep cache directory scans out of Settings construction and painting."""
        if self._game_cache_stats_closed158:
            return
        self._game_cache_stats_generation158 += 1
        self._status_text(self.game_web_cache_stats, "Проверяю…")
        if self._game_cache_stats_worker158 is None:
            self._start_game_cache_stats158()

    def _start_game_cache_stats158(self) -> None:
        generation = self._game_cache_stats_generation158
        config = self.config
        # Capture plain data only. A closed/deleted dialog is never accessed by
        # this worker; all widget updates are queued QObject receiver slots.
        worker = FunctionWorker(lambda: (generation, GameWebCache.read_stats(config)))
        self._game_cache_stats_running_generation158 = generation
        self._game_cache_stats_worker158 = worker
        worker.signals.result.connect(self._game_cache_stats_ready158, Qt.QueuedConnection)
        worker.signals.error.connect(self._game_cache_stats_failed158, Qt.QueuedConnection)
        worker.signals.finished.connect(self._game_cache_stats_finished158, Qt.QueuedConnection)
        QThreadPool.globalInstance().start(worker)

    def _game_cache_stats_ready158(self, payload: object) -> None:
        if self._game_cache_stats_closed158 or not isinstance(payload, tuple) or len(payload) != 2:
            return
        generation, stats = payload
        if generation != self._game_cache_stats_generation158 or not isinstance(stats, dict):
            return
        entries = int(stats.get("entries") or 0)
        size_mb = float(stats.get("bytes") or 0) / (1024.0 * 1024.0)
        self._status_text(self.game_web_cache_stats, "Сохранено ответов: {entries} · {size_mb} МБ", entries=entries, size_mb=f"{size_mb:.2f}")

    def _game_cache_stats_failed158(self, trace: str) -> None:
        if (self._game_cache_stats_closed158
                or self._game_cache_stats_running_generation158 != self._game_cache_stats_generation158):
            return
        last_line = str(trace or "").strip().splitlines()
        self._status_text(self.game_web_cache_stats, "Статус кэша недоступен: {error}",
                          error=last_line[-1][-240:] if last_line else "")

    def _game_cache_stats_finished158(self) -> None:
        self._game_cache_stats_worker158 = None
        if (not self._game_cache_stats_closed158
                and self._game_cache_stats_running_generation158 != self._game_cache_stats_generation158):
            # Coalesce repeated refresh/clear requests into one fresh snapshot.
            self._start_game_cache_stats158()

    def _close_game_cache_stats158(self, *_args) -> None:
        self._game_cache_stats_closed158 = True
        self._game_cache_stats_generation158 += 1

    def _clear_game_web_cache(self) -> None:
        try:
            removed = GameWebCache(self.config).clear()
            self._refresh_game_web_cache_stats()
            QMessageBox.information(self, "Игровой веб-кэш", self._t("Удалено сохранённых ответов: {count}.").format(count=removed))
        except Exception as exc:
            QMessageBox.warning(self, "Игровой веб-кэш", self._t("Не удалось очистить кэш: {error}").format(error=exc))

    def _open_searchapi_dashboard(self) -> None:
        QDesktopServices.openUrl(QUrl("https://www.searchapi.io/"))

    def _probe_searchapi_key(self, key: str) -> dict[str, Any]:
        provider = SearchAPIGoogleProvider(self.config)
        try:
            return provider.account_status(key_override=key)
        finally:
            provider.close()

    def _test_searchapi_key(self) -> None:
        key = self.game_web_searchapi_key.text().strip()
        if not key:
            QMessageBox.warning(self, "SearchAPI.io", "Сначала вставьте API key.")
            return
        self.game_web_test_searchapi.setEnabled(False)
        self.game_web_test_searchapi.setText(self._t("Проверяю…"))
        self.game_web_searchapi_status.setText(self._t("Проверяю авторизацию через SearchAPI.io…"))
        worker = FunctionWorker(self._probe_searchapi_key, key)
        self._searchapi_probe_worker = worker
        worker.signals.result.connect(self._searchapi_probe_succeeded)
        worker.signals.error.connect(self._searchapi_probe_failed)
        worker.signals.finished.connect(self._searchapi_probe_finished)
        QThreadPool.globalInstance().start(worker)

    def _searchapi_probe_succeeded(self, result: object) -> None:
        info = result if isinstance(result, dict) else {}
        # Response fields can evolve; do not depend on a particular plan schema.
        self.game_web_searchapi_status.setText(self._t("Ключ принят SearchAPI.io. Авторизация работает."))
        QMessageBox.information(self, "SearchAPI.io", "API key принят. Теперь сохраните настройки и задайте игровой вопрос.")

    def _searchapi_probe_failed(self, trace: str) -> None:
        key = self.game_web_searchapi_key.text().strip()
        safe = str(trace or "Неизвестная ошибка")
        if key:
            safe = safe.replace(key, "<redacted-api-key>")
        self.game_web_searchapi_status.setText(self._t("Проверка ключа не пройдена."))
        QMessageBox.warning(self, "SearchAPI.io", safe[-1600:])

    def _searchapi_probe_finished(self) -> None:
        self.game_web_test_searchapi.setEnabled(True)
        self.game_web_test_searchapi.setText(self._t("Проверить ключ"))
        self._searchapi_probe_worker = None

    def _append_briefing_settings(self, root: QVBoxLayout) -> None:
        """Window-owned startup briefing controls.

        The briefing is visual desktop-window content, so its visibility belongs
        next to the rest of Window presentation settings rather than API services.
        """
        group = QGroupBox("Сводка на день")
        form = QFormLayout(group)
        self.morning_briefing_enabled = QCheckBox("Показывать сводку на день при запуске")
        self.morning_briefing_enabled.setChecked(bool(self.config.get("briefing.show_on_startup", True)))
        form.addRow(self.morning_briefing_enabled)

        self.briefing_include_time = QCheckBox("Время запуска ассистента")
        self.briefing_include_weather = QCheckBox("Погода и температура")
        self.briefing_include_system = QCheckBox("Полные сведения о компьютере — CPU / GPU / RAM / диски")
        self.briefing_include_processes = QCheckBox("Краткие сведения о процессах")
        self.briefing_include_devices = QCheckBox("Подключённые устройства компьютера")
        self.briefing_include_bluetooth = QCheckBox("Подключённые Bluetooth-устройства")
        self.briefing_include_services = QCheckBox("Проверять подключённые API-сервисы")
        self.briefing_include_services.setToolTip("Проверяет включённые сервисы при сборе сводки. Не запускает музыку и не отправляет сообщения. Запросы погоды и YouTube могут расходовать квоту API.")
        for widget, key, default in (
            (self.briefing_include_time, "include_time", True),
            (self.briefing_include_weather, "include_weather", True),
            (self.briefing_include_system, "include_system", True),
            (self.briefing_include_processes, "include_processes", False),
            (self.briefing_include_devices, "include_devices", True),
            (self.briefing_include_bluetooth, "include_bluetooth", True),
            (self.briefing_include_services, "include_services", False),
        ):
            widget.setChecked(bool(self.config.get(f"briefing.{key}", default)))
            form.addRow(widget)
        form.addRow(self._help("Каждый блок можно включать независимо. Полные сведения о компьютере используют тот же формат CPU → GPU → RAM → диски, что и команда состояния системы."))
        root.addWidget(group)

    def _append_additional_panels(self, root: QVBoxLayout) -> None:
        # HOTFIX29: there is no vague global "Additional panels / Overlay" group.
        # Users configure the four real NEO panel types directly. Root defaults
        # stay internal for backwards-compatible rendering/fallback only.
        legacy_opacity = int(self.config.get("desktop.overlay.opacity", 72) or 72)
        positions = [("Слева сверху", "top_left"), ("По центру сверху", "top_center"), ("Справа сверху", "top_right"), ("Слева по центру", "center_left"), ("По центру", "center"), ("Справа по центру", "center_right"), ("Слева снизу", "bottom_left"), ("По центру снизу", "bottom_center"), ("Справа снизу", "bottom_right")]

        self.overlay_type_style_fields: dict[str, dict[str, Any]] = {}
        global_defaults = {
            "font_family": str(self.config.get("desktop.overlay.font_family", "Segoe UI") or "Segoe UI"),
            "font_size": int(self.config.get("desktop.overlay.font_size", 18) or 18),
            "background_opacity": int(self.config.get("desktop.overlay.background_opacity", legacy_opacity) or 0),
            "border_opacity": int(self.config.get("desktop.overlay.border_opacity", 100) or 0),
            "drag_enabled": bool(self.config.get("desktop.overlay.drag_enabled", True)),
            "monitor": int(self.config.get("desktop.overlay.monitor", 1) or 1),
            "position": str(self.config.get("desktop.overlay.position", "top_left") or "top_left"),
            "width": int(self.config.get("desktop.overlay.width", 420) or 420),
            "height": int(self.config.get("desktop.overlay.height", 0) or 0),
            "click_through": bool(self.config.get("desktop.overlay.click_through", True)),
            "text_color": str(self.config.get("desktop.overlay.text_color", "#F2F6FF") or "#F2F6FF"),
            "background_color": str(self.config.get("desktop.overlay.background_color", "#0A0F18") or "#0A0F18"),
            "border_color": str(self.config.get("desktop.overlay.border_color", "#57D7FF") or "#57D7FF"),
        }
        groups = (("session", "Игровая сессия"), ("checklist", "Чек-листы"), ("notes", "Заметки"), ("music", "Музыкальный оверлей"))
        for style_key, group_title in groups:
            style_group = QGroupBox(f"Панель: {group_title}")
            style_form = QFormLayout(style_group)
            prefix = f"desktop.overlay.styles.{style_key}"
            enabled = QCheckBox("Показывать этот тип дополнительной панели")
            enabled.setChecked(bool(self.config.get(prefix + ".enabled", True)))
            style_form.addRow(enabled)
            panel_preset = CompactComboBox()
            panel_preset.addItem("Пользовательский", "Custom")
            for preset_name in self._preset_names():
                panel_preset.addItem(preset_name, preset_name)
            configured_panel_preset = str(self.config.get(prefix + ".theme_preset", "NEO Cyan") or "NEO Cyan")
            panel_preset.setCurrentIndex(max(0, panel_preset.findData(configured_panel_preset)))
            family = CompactFontComboBox()
            family.setFontFilters(QFontComboBox.FontFilter.AllFonts)
            configured_family = str(self.config.get(prefix + ".font_family", global_defaults["font_family"]) or global_defaults["font_family"])
            family.setCurrentFont(QFont(configured_family))
            family.setToolTip("Список всех шрифтов, установленных в Windows. Выберите шрифт и нажмите «Применить» или «Сохранить».")
            font_size = self._spin(8, 96, int(self.config.get(prefix + ".font_size", global_defaults["font_size"]) or global_defaults["font_size"]), " px", 1)
            bg_opacity = self._spin(0, 100, int(self.config.get(prefix + ".background_opacity", self.config.get(prefix + ".opacity", global_defaults["background_opacity"])) or 0), "%", 5)
            border_opacity = self._spin(0, 100, int(self.config.get(prefix + ".border_opacity", global_defaults["border_opacity"]) or 0), "%", 5)
            width = self._spin(160, 1600, int(self.config.get(prefix + ".width", global_defaults["width"]) or global_defaults["width"]), " px", 20)
            height = self._spin(0, 1400, int(self.config.get(prefix + ".height", global_defaults["height"]) or 0), " px", 20)
            monitor = self._spin(1, 16, int(self.config.get(prefix + ".monitor", global_defaults["monitor"]) or global_defaults["monitor"]), "", 1)
            position = QComboBox()
            for label, value in positions:
                position.addItem(label, value)
            position.setCurrentIndex(max(0, position.findData(str(self.config.get(prefix + ".position", global_defaults["position"]) or global_defaults["position"]))))
            click_through = QCheckBox("Пропускать клики сквозь эту панель")
            click_through.setChecked(bool(self.config.get(prefix + ".click_through", global_defaults["click_through"])))
            drag_enabled = QCheckBox("Разрешить перетаскивание за верхнюю область")
            drag_enabled.setChecked(bool(self.config.get(prefix + ".drag_enabled", global_defaults["drag_enabled"])))
            text_color = ColorField(str(self.config.get(prefix + ".text_color", global_defaults["text_color"]) or global_defaults["text_color"]))
            background_color = ColorField(str(self.config.get(prefix + ".background_color", global_defaults["background_color"]) or global_defaults["background_color"]))
            border_color = ColorField(str(self.config.get(prefix + ".border_color", global_defaults["border_color"]) or global_defaults["border_color"]))
            style_form.addRow("Пресет панели", panel_preset)
            style_form.addRow("Шрифт", family); style_form.addRow("Размер шрифта", font_size)
            style_form.addRow("Прозрачность фона", bg_opacity); style_form.addRow("Прозрачность рамки", border_opacity)
            style_form.addRow("Ширина", width); style_form.addRow("Высота (0 = автоматически)", height)
            style_form.addRow("Монитор", monitor); style_form.addRow("Положение", position)
            style_form.addRow(click_through); style_form.addRow(drag_enabled)
            style_form.addRow("Цвет текста", text_color); style_form.addRow("Цвет фона", background_color); style_form.addRow("Цвет рамки", border_color)
            fields = {
                "enabled": enabled,
                "theme_preset": panel_preset,
                "font_family": family, "font_size": font_size, "background_opacity": bg_opacity, "border_opacity": border_opacity,
                "width": width, "height": height, "monitor": monitor, "position": position, "click_through": click_through,
                "drag_enabled": drag_enabled, "text_color": text_color, "background_color": background_color, "border_color": border_color,
            }
            if style_key == "music":
                controls_visible = QCheckBox("Показывать кнопки управления треком")
                controls_visible.setChecked(bool(self.config.get(prefix + ".controls_visible", True)))
                control_color = ColorField(str(self.config.get(prefix + ".control_color", global_defaults["text_color"]) or global_defaults["text_color"]))
                control_bg_color = ColorField(str(self.config.get(prefix + ".control_background_color", global_defaults["background_color"]) or global_defaults["background_color"]))
                control_bg_opacity = self._spin(0, 100, int(self.config.get(prefix + ".control_background_opacity", 0) or 0), "%", 5)
                control_border_color = ColorField(str(self.config.get(prefix + ".control_border_color", global_defaults["text_color"]) or global_defaults["text_color"]))
                control_border_opacity = self._spin(0, 100, int(self.config.get(prefix + ".control_border_opacity", 80) or 0), "%", 5)
                visualizer_fps = self._spin(30, 60, min(60, int(self.config.get(prefix + ".visualizer_fps", 60) or 60)), " FPS", 5)
                visualizer_mode = QComboBox()
                visualizer_mode.addItem("Симметричная", "symmetric")
                visualizer_mode.addItem("Только вверх от нижней линии", "up")
                visualizer_mode.setCurrentIndex(max(0, visualizer_mode.findData(str(self.config.get(prefix + ".visualizer_mode", "symmetric") or "symmetric"))))
                style_form.addRow(controls_visible)
                style_form.addRow("Цвет иконок кнопок", control_color)
                style_form.addRow("Цвет фона кнопок", control_bg_color)
                style_form.addRow("Прозрачность фона кнопок", control_bg_opacity)
                style_form.addRow("Цвет рамки кнопок", control_border_color)
                style_form.addRow("Прозрачность рамки кнопок", control_border_opacity)
                style_form.addRow("Плавность визуализатора", visualizer_fps)
                style_form.addRow("Направление музыкальной волны", visualizer_mode)
                fields.update({
                    "controls_visible": controls_visible,
                    "control_color": control_color,
                    "control_background_color": control_bg_color,
                    "control_background_opacity": control_bg_opacity,
                    "control_border_color": control_border_color,
                    "control_border_opacity": control_border_opacity,
                    "visualizer_fps": visualizer_fps,
                    "visualizer_mode": visualizer_mode,
                })
            self.overlay_type_style_fields[style_key] = fields
            panel_preset.currentIndexChanged.connect(
                lambda _index, key=style_key, combo=panel_preset: self._apply_panel_preset(key, str(combo.currentData() or ""))
            )
            for color_field in (text_color, background_color, border_color):
                color_field.edit.textChanged.connect(lambda _text, key=style_key: self._mark_panel_custom(key))
            if style_key == "music":
                for color_field in (control_color, control_bg_color, control_border_color):
                    color_field.edit.textChanged.connect(lambda _text, key=style_key: self._mark_panel_custom(key))
            root.addWidget(style_group)

    def _dictionary_tab(self) -> QWidget:
        content = QWidget(); root = QVBoxLayout(content)
        group = QGroupBox("Персональный словарь распознавания и объектов")
        layout = QVBoxLayout(group)
        self.personal_dictionary_enabled = QCheckBox("Использовать словарь при ASR repair и поиске приложений/папок")
        self.personal_dictionary_enabled.setChecked(bool(self.config.get("personal_dictionary.enabled", True)))
        layout.addWidget(self.personal_dictionary_enabled)
        self.dictionary_table = QTableWidget(0, 4)
        self.dictionary_table.setHorizontalHeaderLabels(["Название", "Тип", "Путь", "Ключевые слова / алиасы"])
        self.dictionary_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.dictionary_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.dictionary_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.dictionary_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        for raw in (self.config.get("personal_dictionary.entries", []) or []):
            if not isinstance(raw, dict): continue
            row = self.dictionary_table.rowCount(); self.dictionary_table.insertRow(row)
            self.dictionary_table.setItem(row, 0, QTableWidgetItem(str(raw.get("name", "") or "")))
            self.dictionary_table.setItem(row, 1, QTableWidgetItem(str(raw.get("kind", "app") or "app")))
            self.dictionary_table.setItem(row, 2, QTableWidgetItem(str(raw.get("path", "") or "")))
            aliases = raw.get("aliases", []) or []
            self.dictionary_table.setItem(row, 3, QTableWidgetItem(", ".join(str(x) for x in aliases)))
        layout.addWidget(self.dictionary_table)
        buttons = QHBoxLayout(); add = QPushButton("Добавить"); remove = QPushButton("Удалить строку")
        add.clicked.connect(lambda: self.dictionary_table.insertRow(self.dictionary_table.rowCount()))
        remove.clicked.connect(lambda: self.dictionary_table.removeRow(self.dictionary_table.currentRow()) if self.dictionary_table.currentRow() >= 0 else None)
        buttons.addWidget(add); buttons.addWidget(remove); buttons.addStretch(1); layout.addLayout(buttons)
        layout.addWidget(self._help("Типы: app, game, folder, file, discord_channel, term. Для сложного приложения можно указать полный путь и несколько вариантов произношения. Совпадение используется только при высокой уверенности, чтобы не открывать похожий объект случайно."))
        root.addWidget(group); root.addStretch(1); return self._scroll(content)

    def _scenarios_tab(self) -> QWidget:
        from jarvis.ui.scenario_editor import ScenarioEditor
        self.scenario_editor = ScenarioEditor(self.config, self)
        self.scenario_editor.run_requested.connect(self.scenario_run_requested.emit)
        self.scenario_editor.retry_requested.connect(self.scenario_retry_requested.emit)
        self.settings_saved.connect(self.scenario_editor.retranslate)
        return self._scroll(self.scenario_editor)

    def _remote_connection_tab(self) -> QWidget:
        """Telegram remote-control settings kept in one dedicated page."""
        content = QWidget()
        root = QVBoxLayout(content)
        root.setSpacing(10)

        intro = QLabel(
            "Telegram используется как удалённый вход в тот же локальный набор команд и инструментов NEO. "
            "Даже если ограничения уже выставлены в BotFather, NEO дополнительно принимает команды только "
            "от одного привязанного числового Telegram user ID и только в личном чате."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        connection = QGroupBox("Telegram-бот")
        form = QFormLayout(connection)
        self.telegram_enabled = QCheckBox("Включить удалённое управление через Telegram")
        self.telegram_enabled.setChecked(bool(self.config.get("integrations.telegram.remote_control_enabled", self.config.get("integrations.telegram.enabled", False))))
        form.addRow(self.telegram_enabled)
        self.remote_wake_service = QCheckBox("Разрешить удалённый запуск ассистента через Telegram")
        self.remote_wake_service.setChecked(bool(self.config.get("integrations.telegram.remote_wake_enabled", True)))
        self.remote_wake_service.setToolTip(
            "Управляет NEO_WAKE_SERVICE.exe. Если выключено, сервис удаляется из автозапуска "
            "и уже запущенный процесс завершается. Telegram-сообщения внутри работающего NEO не отключаются."
        )
        form.addRow(self.remote_wake_service)

        self._existing_telegram_token_encrypted = str(
            self.config.get("integrations.telegram.bot_token_encrypted", "") or ""
        )
        self._telegram_initial_token_plain = unprotect_secret(self._existing_telegram_token_encrypted)
        self.telegram_bot_token = QLineEdit(self._telegram_initial_token_plain)
        self.telegram_bot_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.telegram_bot_token.setPlaceholderText("BotFather token: 123456789:AA...")

        token_row = QWidget()
        token_layout = QHBoxLayout(token_row)
        token_layout.setContentsMargins(0, 0, 0, 0)
        token_layout.setSpacing(6)
        token_layout.addWidget(self.telegram_bot_token, 1)
        self.telegram_show_token = QPushButton("Показать")
        self.telegram_show_token.setCheckable(True)
        self.telegram_show_token.toggled.connect(self._telegram_toggle_token_visibility)
        token_layout.addWidget(self.telegram_show_token)
        self.telegram_test_button = QPushButton("Проверить и подключить")
        self.telegram_test_button.clicked.connect(self._telegram_test_and_connect)
        token_layout.addWidget(self.telegram_test_button)
        self._add_field(
            form,
            "Bot token",
            token_row,
            "Токен проверяется через официальный Telegram Bot API. На диск сохраняется только DPAPI-зашифрованное значение для текущего пользователя Windows.",
        )

        self.telegram_bot_identity = QLabel()
        self.telegram_bot_identity.setWordWrap(True)
        form.addRow("Бот", self.telegram_bot_identity)
        self.telegram_connection_status = QLabel()
        self.telegram_connection_status.setWordWrap(True)
        form.addRow("Состояние", self.telegram_connection_status)
        root.addWidget(connection)
        self._bind_collapsible_group(connection, [self.telegram_enabled])

        owner = QGroupBox("Привязка владельца")
        owner_form = QFormLayout(owner)
        self.telegram_owner_user = QLineEdit()
        self.telegram_owner_user.setReadOnly(True)
        self.telegram_owner_chat = QLineEdit()
        self.telegram_owner_chat.setReadOnly(True)
        self._add_field(
            owner_form,
            "Telegram user ID",
            self.telegram_owner_user,
            "NEO использует числовой ID, а не @username. В этой версии может быть привязан только один владелец.",
        )
        self._add_field(owner_form, "Private chat ID", self.telegram_owner_chat)

        self.telegram_pair_command = QLineEdit()
        self.telegram_pair_command.setReadOnly(True)
        self.telegram_pair_command.setPlaceholderText("Нажмите «Создать код привязки»")
        pair_row = QWidget()
        pair_layout = QHBoxLayout(pair_row)
        pair_layout.setContentsMargins(0, 0, 0, 0)
        pair_layout.setSpacing(6)
        pair_layout.addWidget(self.telegram_pair_command, 1)
        self.telegram_pair_copy_button = QPushButton("Копировать код")
        self.telegram_pair_copy_button.setVisible(False)
        self.telegram_pair_copy_button.clicked.connect(lambda: self._copy_code_field(self.telegram_pair_command, "Код привязки скопирован"))
        pair_layout.addWidget(self.telegram_pair_copy_button)
        self.telegram_pair_button = QPushButton("Создать код привязки")
        self.telegram_pair_button.clicked.connect(self._telegram_generate_pairing)
        pair_layout.addWidget(self.telegram_pair_button)
        self._add_field(
            owner_form,
            "Команда /pair",
            pair_row,
            "Одноразовый код действует 15 минут. Отправьте показанную команду своему боту в Telegram. После успеха код удаляется автоматически.",
        )

        owner_buttons = QWidget()
        owner_buttons_layout = QHBoxLayout(owner_buttons)
        owner_buttons_layout.setContentsMargins(0, 0, 0, 0)
        owner_buttons_layout.setSpacing(6)
        self.telegram_unpair_button = QPushButton("Отвязать Telegram-аккаунт")
        self.telegram_unpair_button.clicked.connect(self._telegram_unpair_owner)
        owner_buttons_layout.addWidget(self.telegram_unpair_button)
        self.telegram_remove_button = QPushButton("Отключить и удалить токен")
        self.telegram_remove_button.clicked.connect(self._telegram_remove_credentials)
        owner_buttons_layout.addWidget(self.telegram_remove_button)
        owner_form.addRow(owner_buttons)
        root.addWidget(owner)

        critical = QGroupBox("Кодовая фраза критических действий")
        critical_form = QFormLayout(critical)
        self.telegram_critical_phrase = QLineEdit(
            unprotect_secret(str(self.config.get("integrations.telegram.critical_phrase_encrypted", "") or ""))
        )
        self.telegram_critical_phrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.telegram_critical_phrase.setPlaceholderText("Задайте отдельную фразу или PIN для Telegram")
        critical_phrase_row = QWidget()
        critical_phrase_layout = QHBoxLayout(critical_phrase_row)
        critical_phrase_layout.setContentsMargins(0, 0, 0, 0)
        critical_phrase_layout.setSpacing(6)
        critical_phrase_layout.addWidget(self.telegram_critical_phrase, 1)
        self.telegram_show_critical_phrase = QPushButton("Показать")
        self.telegram_show_critical_phrase.setCheckable(True)
        self.telegram_show_critical_phrase.toggled.connect(self._telegram_toggle_critical_phrase_visibility)
        critical_phrase_layout.addWidget(self.telegram_show_critical_phrase)
        self._add_field(
            critical_form,
            "Кодовая фраза / PIN",
            critical_phrase_row,
            "Используется только для критичных удалённых действий. На диске хранится через Windows DPAPI. "
            "Сообщение с фразой в Telegram удаляется сразу после проверки и не зеркалируется в локальный чат.",
        )

        self.telegram_critical_ttl = QSpinBox()
        self.telegram_critical_ttl.setRange(20, 300)
        self.telegram_critical_ttl.setSuffix(" сек")
        self.telegram_critical_ttl.setValue(int(self.config.get("integrations.telegram.critical_phrase_ttl_seconds", 90) or 90))
        self._add_field(critical_form, "Время на ввод фразы", self.telegram_critical_ttl)

        self.telegram_critical_attempts = QSpinBox()
        self.telegram_critical_attempts.setRange(1, 5)
        self.telegram_critical_attempts.setValue(int(self.config.get("integrations.telegram.critical_phrase_max_attempts", 3) or 3))
        self._add_field(critical_form, "Попыток до блокировки", self.telegram_critical_attempts)

        self.telegram_critical_lockout = QSpinBox()
        self.telegram_critical_lockout.setRange(15, 600)
        self.telegram_critical_lockout.setSuffix(" сек")
        self.telegram_critical_lockout.setValue(int(self.config.get("integrations.telegram.critical_phrase_lockout_seconds", 60) or 60))
        self._add_field(critical_form, "Блокировка после ошибок", self.telegram_critical_lockout)
        root.addWidget(critical)

        chat_group = QGroupBox("Чат и журнал действий")
        chat_form = QFormLayout(chat_group)
        self.telegram_chat_cleanup_enabled = QCheckBox("Автоматически очищать управляемую историю Telegram")
        self.telegram_chat_cleanup_enabled.setChecked(bool(self.config.get("integrations.telegram.chat_cleanup_enabled", True)))
        chat_form.addRow(self.telegram_chat_cleanup_enabled)

        self.telegram_cleanup_interval = QSpinBox()
        self.telegram_cleanup_interval.setRange(5, 240)
        self.telegram_cleanup_interval.setSuffix(" мин")
        self.telegram_cleanup_interval.setValue(int(self.config.get("integrations.telegram.chat_cleanup_interval_minutes", 30) or 30))
        self._add_field(
            chat_form,
            "Период очистки",
            self.telegram_cleanup_interval,
            "NEO удаляет известные сообщения этого личного чата — ваши команды и ответы NEO. Telegram разрешает боту удалять только сообщения младше 48 часов; более старые и сообщения без известного ID нужно удалить вручную.",
        )

        self.telegram_cleanup_on_startup = QCheckBox("Очищать известные сообщения при запуске NEO")
        self.telegram_cleanup_on_startup.setChecked(bool(self.config.get("integrations.telegram.cleanup_on_startup", True)))
        chat_form.addRow(self.telegram_cleanup_on_startup)
        self.telegram_cleanup_on_shutdown = QCheckBox("Очищать чат перед завершением NEO")
        self.telegram_cleanup_on_shutdown.setChecked(bool(self.config.get("integrations.telegram.cleanup_on_shutdown", True)))
        chat_form.addRow(self.telegram_cleanup_on_shutdown)
        # NEO 188 NEO-TG-001: the honest result of the last cleanup pass.
        try:
            import json as _json188
            state188 = _json188.loads((Path(self.config.paths.root) / "telegram_remote_state.json").read_text(encoding="utf-8"))
        except Exception:
            state188 = {}
        from jarvis.integrations.telegram_cleanup188 import summary as telegram_cleanup_summary188
        self.telegram_cleanup_status188 = QLabel(telegram_cleanup_summary188(
            state188.get("cleanup_report") if isinstance(state188, dict) else None, language_code(self.config)))
        self.telegram_cleanup_status188.setWordWrap(True)
        self.telegram_cleanup_status188.setProperty("i18n_skip_text", True)
        chat_form.addRow(self.telegram_cleanup_status188)
        # 186: the flag applies to the cleanup at start and to /start; the periodic
        # background cleanup restores the section that was open.
        self.telegram_show_home_after_cleanup = QCheckBox("Показывать новую главную панель после очистки при запуске и по /start")
        self.telegram_show_home_after_cleanup.setToolTip("Периодическая фоновая очистка сохраняет открытый раздел и от этого флага не зависит.")
        self.telegram_show_home_after_cleanup.setChecked(bool(self.config.get("integrations.telegram.show_home_after_cleanup", True)))
        chat_form.addRow(self.telegram_show_home_after_cleanup)
        self.telegram_mirror_actions = QCheckBox("Показывать Telegram-действия в диалоговом окне NEO")
        self.telegram_mirror_actions.setChecked(bool(self.config.get("integrations.telegram.mirror_actions_to_desktop", True)))
        chat_form.addRow(self.telegram_mirror_actions)
        root.addWidget(chat_group)

        security = QGroupBox("Безопасность удалённых команд")
        security_form = QFormLayout(security)
        self.telegram_private_only = QCheckBox("Только личные чаты Telegram")
        self.telegram_private_only.setChecked(True)
        self.telegram_private_only.setEnabled(False)
        security_form.addRow(self.telegram_private_only)

        self.telegram_confirm_medium = QCheckBox("Подтверждать также команды среднего риска")
        self.telegram_confirm_medium.setChecked(bool(self.config.get("integrations.telegram.confirm_medium_risk", False)))
        security_form.addRow(self.telegram_confirm_medium)

        self.telegram_command_interval = QDoubleSpinBox()
        self.telegram_command_interval.setRange(0.10, 10.0)
        self.telegram_command_interval.setDecimals(2)
        self.telegram_command_interval.setSingleStep(0.05)
        self.telegram_command_interval.setSuffix(" сек")
        self.telegram_command_interval.setValue(float(self.config.get("integrations.telegram.min_command_interval_seconds", 0.8) or 0.8))
        self._add_field(security_form, "Минимальный интервал команд", self.telegram_command_interval)

        self.telegram_max_command_chars = QSpinBox()
        self.telegram_max_command_chars.setRange(64, 4000)
        self.telegram_max_command_chars.setValue(int(self.config.get("integrations.telegram.max_command_chars", 1500) or 1500))
        self.telegram_max_command_chars.setSuffix(" симв.")
        self._add_field(security_form, "Максимальная длина команды", self.telegram_max_command_chars)

        blocked = {
            str(x or "").strip().casefold()
            for x in (self.config.get("integrations.telegram.blocked_tools", []) or [])
            if str(x or "").strip()
        }
        labels = {
            "powershell": "PowerShell",
            "developer": "Developer tools",
            "email": "Email",
            "transfer": "Передача файлов/данных",
        }
        self.telegram_blocked_tool_checks: dict[str, QCheckBox] = {}
        blocked_box = QWidget()
        blocked_layout = QVBoxLayout(blocked_box)
        blocked_layout.setContentsMargins(0, 0, 0, 0)
        blocked_layout.setSpacing(3)
        for tool_name, label in labels.items():
            check = QCheckBox(f"Блокировать {label}")
            check.setChecked(tool_name in blocked)
            blocked_layout.addWidget(check)
            self.telegram_blocked_tool_checks[tool_name] = check
        self._add_field(
            security_form,
            "Жёстко запрещённые инструменты",
            blocked_box,
            "Проверка выполняется уже внутри NEO перед запуском инструмента. Сценарии из локальных настроек NEO разрешены отдельно и запускаются только по точному сохранённому имени.",
        )
        root.addWidget(security)

        messages_group = QGroupBox("Telegram Messages · Secretary Mode")
        messages_form = QFormLayout(messages_group)
        self.telegram_messages_enabled = QCheckBox("Получать личные сообщения через Connected Business Bot")
        self.telegram_messages_enabled.setChecked(bool(self.config.get("integrations.telegram.messages_enabled", False)))
        self.telegram_messages_notifications = QCheckBox("Показывать уведомления о новых сообщениях")
        self.telegram_messages_notifications.setChecked(bool(self.config.get("integrations.telegram.messages_notifications_enabled", True)))
        messages_form.addRow(self.telegram_messages_enabled); messages_form.addRow(self.telegram_messages_notifications)
        self.telegram_messages_detail = QComboBox()
        self.telegram_messages_detail.addItem("Только новое сообщение", "notification_only")
        self.telegram_messages_detail.addItem("Отправитель", "sender")
        self.telegram_messages_detail.addItem("Отправитель + содержимое", "sender_text")
        idx=self.telegram_messages_detail.findData(str(self.config.get("integrations.telegram.messages_notification_detail", "sender_text") or "sender_text")); self.telegram_messages_detail.setCurrentIndex(max(0,idx))
        self._add_field(messages_form,"Формат уведомления",self.telegram_messages_detail)
        self.telegram_messages_briefing = QCheckBox("Добавлять Telegram в сводку на день")
        self.telegram_messages_briefing.setChecked(bool(self.config.get("integrations.telegram.include_in_briefing", False)))
        self.telegram_briefing_24h = QCheckBox("В сводке показывать сообщения за последние 24 часа")
        self.telegram_briefing_24h.setChecked(bool(self.config.get("integrations.telegram.briefing_show_recent24h", True)))
        self.telegram_briefing_total = QCheckBox("В сводке показывать всё количество учтённых сообщений")
        self.telegram_briefing_total.setChecked(bool(self.config.get("integrations.telegram.briefing_show_total", False)))
        self.telegram_briefing_senders = QCheckBox("В сводке показывать отправителей и количество")
        self.telegram_briefing_senders.setChecked(bool(self.config.get("integrations.telegram.briefing_show_senders", False)))
        self.telegram_query_senders = QCheckBox("В голосовом запросе количества показывать разбивку по отправителям")
        self.telegram_query_senders.setChecked(bool(self.config.get("integrations.telegram.query_show_senders", True)))
        messages_form.addRow(self.telegram_messages_briefing); messages_form.addRow(self.telegram_briefing_24h); messages_form.addRow(self.telegram_briefing_total); messages_form.addRow(self.telegram_briefing_senders); messages_form.addRow(self.telegram_query_senders)
        _people=self.config.get("integrations.telegram.outbound_allowed_people",[]) or []
        _groups=self.config.get("integrations.telegram.outbound_allowed_groups",[]) or []
        self.telegram_allowed_people=QLineEdit("; ".join(str(x) for x in _people) if isinstance(_people,(list,tuple)) else str(_people))
        self.telegram_allowed_people.setPlaceholderText("Иван")
        self._add_field(messages_form,"Разрешённые люди для отправки",self.telegram_allowed_people,"Только эти точные имена/username можно использовать в голосовой команде отправки. Получатель должен ранее появиться в Secretary Mode.")
        self.telegram_allowed_groups=QLineEdit("; ".join(str(x) for x in _groups) if isinstance(_groups,(list,tuple)) else str(_groups))
        self.telegram_allowed_groups.setPlaceholderText("Семья; Проект")
        self._add_field(messages_form,"Разрешённые группы",self.telegram_allowed_groups,"Отправка в группу разрешается только при точном совпадении с этим списком и ранее наблюдавшимся Business-чатом.")
        try:
            tg_status=telegram_message_center(self.config).connection_status()
            status_text = "Подключён" if tg_status.get("connected") else "Не подключён. Одного переключателя Secretary Mode в BotFather недостаточно: подключите этого бота в Telegram → Настройки → Telegram Business → Чат-боты/Secretary, выберите доступные чаты и выдайте право Reply."
        except Exception:
            status_text = "Статус Secretary Mode недоступен"
        self.telegram_messages_status = QLabel(status_text); self.telegram_messages_status.setWordWrap(True); messages_form.addRow("Business connection",self.telegram_messages_status)
        self.telegram_messages_check_button = QPushButton("Проверить получение сообщений")
        self.telegram_messages_check_button.clicked.connect(self._telegram_check_messages_status)
        messages_form.addRow("Диагностика", self.telegram_messages_check_button)
        self.telegram_messages_history_note = QLabel(
            "NEO хранит последнее и предыдущие Business-сообщения локально без ограничения «24 часа». "
            "Но Telegram Bot API не позволяет задним числом получить личные сообщения, отправленные до подключения Connected Business Bot/до получения update."
        )
        self.telegram_messages_history_note.setWordWrap(True)
        messages_form.addRow("История", self.telegram_messages_history_note)
        root.addWidget(messages_group)
        self._bind_collapsible_group(messages_group, [self.telegram_messages_enabled])

        root.addStretch(1)

        self._telegram_status_timer = QTimer(self)
        self._telegram_status_timer.setInterval(1000)
        self._telegram_status_timer.timeout.connect(self._telegram_refresh_status)
        self._telegram_status_timer.start()
        self._telegram_refresh_status()
        return self._scroll(content)

    def _telegram_toggle_token_visibility(self, visible: bool) -> None:
        self.telegram_bot_token.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self.telegram_show_token.setText(self._t("Скрыть" if visible else "Показать"))

    def _telegram_toggle_critical_phrase_visibility(self, visible: bool) -> None:
        self.telegram_critical_phrase.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self.telegram_show_critical_phrase.setText(self._t("Скрыть" if visible else "Показать"))

    @staticmethod
    def _telegram_api_call(token: str, method: str, payload: dict[str, Any] | None = None, timeout: float = 15.0) -> Any:
        value = str(token or "").strip()
        if not value:
            raise RuntimeError("Bot token не указан")
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{value}/{method}",
                json=payload or {},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(str(exc).replace(value, "<redacted-token>")) from None
        try:
            data = response.json()
        except Exception:
            data = {}
        if response.status_code >= 400 or not bool(data.get("ok")):
            raise RuntimeError(str(data.get("description") or f"Telegram HTTP {response.status_code}"))
        return data.get("result")

    @classmethod
    def _telegram_probe_and_prepare(cls, token: str) -> dict[str, Any]:
        me = cls._telegram_api_call(token, "getMe") or {}
        username = str(me.get("username", "") or "").strip()
        if not username:
            raise RuntimeError("Telegram getMe не вернул username бота")
        # Desktop NEO uses long polling; normalize the bot away from webhook mode.
        cls._telegram_api_call(token, "deleteWebhook", {"drop_pending_updates": False})
        cls._telegram_api_call(
            token,
            "setMyCommands",
            {
                "commands": [
                    {"command": "launch", "description": "Запуск"},
                ]
            },
        )
        try:
            cls._telegram_api_call(token, "setChatMenuButton", {"menu_button": {"type": "commands"}})
        except Exception:
            pass
        return {"username": username, "id": str(me.get("id", "") or "")}

    def _copy_code_field(self, field: QLineEdit, status_text: str = "Код скопирован") -> None:
        value = str(field.text() or "").strip()
        if not value:
            return
        QApplication.clipboard().setText(value)
        field.selectAll()
        field.setToolTip(self._t(status_text))

    def _telegram_test_and_connect(self) -> None:
        token = self.telegram_bot_token.text().strip()
        if not token or ":" not in token:
            QMessageBox.warning(self, "Telegram", "Вставьте BotFather token в формате 123456789:AA...")
            return
        self.telegram_test_button.setEnabled(False)
        self.telegram_test_button.setText(self._t("Проверяю…"))
        self.telegram_connection_status.setText(self._t("Проверка Bot API…"))
        worker = FunctionWorker(self._telegram_probe_and_prepare, token)
        worker.signals.result.connect(self._telegram_probe_succeeded)
        worker.signals.error.connect(self._telegram_probe_failed)
        QThreadPool.globalInstance().start(worker)

    def _telegram_probe_succeeded(self, result: object) -> None:
        info = result if isinstance(result, dict) else {}
        token = self.telegram_bot_token.text().strip()
        try:
            self.config.reload()
            tg = self.config.data.setdefault("integrations", {}).setdefault("telegram", {})
            tg["enabled"] = True
            tg["remote_control_enabled"] = True
            tg["bot_token_encrypted"] = protect_secret(token)
            tg["bot_username"] = str(info.get("username", "") or "")
            tg["bot_id"] = str(info.get("id", "") or "")
            tg["private_chats_only"] = True
            self._telegram_apply_security_values(tg)
            self.config.save(self.config.data)
            self.config.reload()
            self._telegram_initial_token_plain = token
            self._existing_telegram_token_encrypted = str(
                self.config.get("integrations.telegram.bot_token_encrypted", "") or ""
            )
            self.telegram_enabled.setChecked(True)
            controller = getattr(self.parent(), "telegram_remote", None)
            if controller is not None:
                controller.start()
                try:
                    remote_agent = getattr(controller, "agent_or_none", None)
                    if remote_agent is not None:
                        remote_agent.refresh_runtime_settings()
                except Exception:
                    pass
            QMessageBox.information(
                self,
                "Telegram",
                f"Бот @{str(info.get('username', '') or '')} проверен и подключён. Теперь создайте код привязки владельца.",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Telegram", f"Не удалось сохранить подключение: {exc}")
        finally:
            self.telegram_test_button.setEnabled(True)
            self.telegram_test_button.setText(self._t("Проверить и подключить"))
            self._telegram_refresh_status()

    def _telegram_probe_failed(self, trace: str) -> None:
        token = self.telegram_bot_token.text().strip()
        safe = str(trace or "Неизвестная ошибка").replace(token, "<redacted-token>") if token else str(trace or "")
        self.telegram_test_button.setEnabled(True)
        self.telegram_test_button.setText(self._t("Проверить и подключить"))
        self.telegram_connection_status.setText(self._t("Ошибка подключения к Telegram Bot API"))
        QMessageBox.warning(self, "Telegram", safe[-1800:])

    def _telegram_apply_security_values(self, tg: dict[str, Any]) -> None:
        tg["private_chats_only"] = True
        # Kept only for compatibility with HOTFIX54/55 state; HOTFIX56 uses the critical-phrase TTL above.
        tg["confirmation_ttl_seconds"] = int(self.config.get("integrations.telegram.confirmation_ttl_seconds", 90) or 90)
        tg["confirm_medium_risk"] = bool(self.telegram_confirm_medium.isChecked())
        tg["min_command_interval_seconds"] = float(self.telegram_command_interval.value())
        tg["max_command_chars"] = int(self.telegram_max_command_chars.value())
        tg["blocked_tools"] = sorted(
            name for name, check in self.telegram_blocked_tool_checks.items() if check.isChecked()
        )
        phrase = self.telegram_critical_phrase.text().strip()
        tg["critical_phrase_encrypted"] = protect_secret(phrase) if phrase else ""
        tg["critical_phrase_ttl_seconds"] = int(self.telegram_critical_ttl.value())
        tg["critical_phrase_max_attempts"] = int(self.telegram_critical_attempts.value())
        tg["critical_phrase_lockout_seconds"] = int(self.telegram_critical_lockout.value())
        tg["chat_cleanup_enabled"] = bool(self.telegram_chat_cleanup_enabled.isChecked())
        tg["chat_cleanup_interval_minutes"] = int(self.telegram_cleanup_interval.value())
        tg["cleanup_on_startup"] = bool(self.telegram_cleanup_on_startup.isChecked())
        tg["cleanup_on_shutdown"] = bool(self.telegram_cleanup_on_shutdown.isChecked())
        tg["show_home_after_cleanup"] = bool(self.telegram_show_home_after_cleanup.isChecked())
        tg["mirror_actions_to_desktop"] = bool(self.telegram_mirror_actions.isChecked())
        # Power-off is available remotely but remains HIGH risk and therefore
        # still requires the configured Telegram critical phrase.
        tg["allow_remote_shutdown"] = True

    def _telegram_generate_pairing(self) -> None:
        token = self.telegram_bot_token.text().strip()
        if not token:
            QMessageBox.warning(self, "Telegram", "Сначала укажите token и нажмите «Проверить и подключить».")
            return
        if token != self._telegram_initial_token_plain:
            QMessageBox.warning(self, "Telegram", "Token был изменён. Сначала нажмите «Проверить и подключить».")
            return
        secret = secrets.token_urlsafe(9)
        expires_at = int(time.time() + 15 * 60)
        try:
            self.config.reload()
            tg = self.config.data.setdefault("integrations", {}).setdefault("telegram", {})
            tg["enabled"] = True
            tg["remote_control_enabled"] = True
            tg["bot_token_encrypted"] = protect_secret(token)
            tg["private_chats_only"] = True
            tg["pairing_enabled"] = True
            tg["pairing_secret_encrypted"] = protect_secret(secret)
            tg["pairing_expires_at"] = expires_at
            self._telegram_apply_security_values(tg)
            self.config.save(self.config.data)
            self.telegram_enabled.setChecked(True)
            controller = getattr(self.parent(), "telegram_remote", None)
            if controller is not None:
                controller.start()
            self.telegram_pair_command.setText(f"/pair {secret}")
            self.telegram_pair_copy_button.setVisible(True)
            QMessageBox.information(
                self,
                "Telegram",
                "Код создан на 15 минут. Скопируйте команду /pair из поля и отправьте её своему боту. После успешной привязки NEO оставит только этот Telegram user ID владельцем.",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Telegram", f"Не удалось создать код привязки: {exc}")
        self._telegram_refresh_status()

    def _telegram_unpair_owner(self) -> None:
        if QMessageBox.question(
            self,
            "Telegram",
            "Отвязать текущий Telegram-аккаунт? Бот перестанет принимать обычные команды до новой /pair-привязки.",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.config.reload()
            tg = self.config.data.setdefault("integrations", {}).setdefault("telegram", {})
            tg["allowed_user_ids"] = []
            tg["allowed_chat_ids"] = []
            tg["pairing_enabled"] = False
            tg["pairing_secret_encrypted"] = ""
            tg["pairing_expires_at"] = 0
            self.config.save(self.config.data)
            self.telegram_pair_command.clear()
        except Exception as exc:
            QMessageBox.critical(self, "Telegram", str(exc))
        self._telegram_refresh_status()

    def _telegram_remove_credentials(self) -> None:
        if QMessageBox.question(
            self,
            "Telegram",
            "Отключить Telegram Remote, удалить локально сохранённый token и привязку владельца?",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.config.reload()
            tg = self.config.data.setdefault("integrations", {}).setdefault("telegram", {})
            tg.update(
                {
                    "enabled": False,
                    "bot_token_encrypted": "",
                    "bot_username": "",
                    "bot_id": "",
                    "allowed_user_ids": [],
                    "allowed_chat_ids": [],
                    "pairing_enabled": False,
                    "pairing_secret_encrypted": "",
                    "pairing_expires_at": 0,
                    "private_chats_only": True,
                }
            )
            self.config.save(self.config.data)
            self.telegram_enabled.setChecked(False)
            self.telegram_bot_token.clear()
            self._telegram_initial_token_plain = ""
            self._existing_telegram_token_encrypted = ""
            self.telegram_pair_command.clear()
            controller = getattr(self.parent(), "telegram_remote", None)
            if controller is not None:
                # stop() is intentionally bounded; active long polling notices the stop flag.
                controller.stop()
        except Exception as exc:
            QMessageBox.critical(self, "Telegram", str(exc))
        self._telegram_refresh_status()

    def _telegram_check_messages_status(self) -> None:
        try:
            center = telegram_message_center(self.config)
            status = center.connection_status()
            stats = center.stats()
            latest = center.latest()
            controller = getattr(self.parent(), "telegram_remote", None)
            transport = "long polling подключён" if controller is not None and bool(getattr(controller, "connected", False)) else "long polling не подключён"
            if controller is not None and str(getattr(controller, "last_error", "") or "").strip():
                transport += f"; ошибка: {str(getattr(controller, 'last_error', '') or '')[:180]}"
            if status.get("connected"):
                conn = (status.get("connections") or [{}])[0]
                rights = dict(conn.get("rights") or {})
                rights_text = ", ".join(k for k,v in rights.items() if bool(v)) or "права не перечислены"
                business = f"Business connection активен ({conn.get('owner_name') or conn.get('id')}); права: {rights_text}"
            else:
                business = (
                    "Business connection не наблюдался. Проверьте не только BotFather: в Telegram аккаунта откройте Business → Chatbots, "
                    "подключите именно этого бота, разрешите existing/new chats (или нужных получателей) и право Reply."
                )
            if latest:
                latest_text = f"Последнее сохранённое: {latest.get('sender_name','Telegram')} — {str(latest.get('text',''))[:180]}"
            else:
                latest_text = "NEO ещё не получил ни одного входящего business_message. Старую историю Telegram Bot API не выдаёт задним числом."
            text = f"{transport}.\n{business}.\nЛокально сохранено входящих: {int(stats.get('total',0) or 0)}.\n{latest_text}"
            self.telegram_messages_status.setText(self._rt(text))
            QMessageBox.information(self, "Telegram Messages", text)
        except Exception as exc:
            QMessageBox.warning(self, "Telegram Messages", str(exc))

    def _telegram_refresh_status(self) -> None:
        code = language_code(self.config)
        users = [str(x) for x in (self.config.get("integrations.telegram.allowed_user_ids", []) or []) if str(x).strip()]
        chats = [str(x) for x in (self.config.get("integrations.telegram.allowed_chat_ids", []) or []) if str(x).strip()]
        self.telegram_owner_user.setText(users[0] if users else tr("Не привязан", code))
        self.telegram_owner_chat.setText(chats[0] if chats else tr("Не привязан", code))
        username = str(self.config.get("integrations.telegram.bot_username", "") or "").strip()
        bot_id = str(self.config.get("integrations.telegram.bot_id", "") or "").strip()
        self.telegram_bot_identity.setText(
            (f"@{username}" + (f" · ID {bot_id}" if bot_id else "")) if username else tr("Не проверен", code)
        )

        self.telegram_pair_copy_button.setVisible(False)
        if bool(self.config.get("integrations.telegram.pairing_enabled", False)):
            expires = int(self.config.get("integrations.telegram.pairing_expires_at", 0) or 0)
            encrypted = str(self.config.get("integrations.telegram.pairing_secret_encrypted", "") or "")
            secret = unprotect_secret(encrypted)
            remaining = max(0, expires - int(time.time()))
            if secret and remaining > 0:
                self.telegram_pair_command.setText(f"/pair {secret}")
                self.telegram_pair_copy_button.setVisible(True)
                self.telegram_pair_button.setText(f"{tr('Создать новый код', code)} ({remaining // 60}:{remaining % 60:02d})")
            else:
                self.telegram_pair_button.setText(tr("Создать новый код", code))
        else:
            self.telegram_pair_button.setText(tr("Создать код привязки", code))
            if users:
                self.telegram_pair_command.setText(tr("Привязка выполнена", code))
            elif not self.telegram_pair_command.hasFocus():
                self.telegram_pair_command.clear()

        controller = getattr(self.parent(), "telegram_remote", None)
        remote_enabled = bool(self.config.get("integrations.telegram.remote_control_enabled", self.config.get("integrations.telegram.enabled", False)))
        messages_enabled = bool(self.config.get("integrations.telegram.messages_enabled", False))
        enabled = bool(remote_enabled or messages_enabled)
        has_token = bool(str(self.config.get("integrations.telegram.bot_token_encrypted", "") or "").strip())
        if not enabled:
            status = tr("Отключено", code)
        elif not has_token:
            status = tr("Включено, но token не сохранён", code)
        elif controller is not None and bool(getattr(controller, "connected", False)):
            status = tr("Подключено · long polling активен", code)
        elif controller is not None and str(getattr(controller, "last_error", "") or "").strip():
            from jarvis.i18n import localize_runtime_text
            err = localize_runtime_text(str(getattr(controller, "last_error", "") or "").strip()[:240], code)
            status = tr("Переподключение", code) + " · " + err
        else:
            status = tr("Запускается / ожидает соединения", code)
        self.telegram_connection_status.setText(status)

    def _conversation_tab187(self) -> QWidget:
        """«Ассистент → Общение и память»: the local conversation/memory block."""
        content = QWidget()
        root = QVBoxLayout(content)
        root.setSpacing(10)
        group = getattr(self, "_conversation_group187", None)
        if group is not None:
            old_parent = group.parentWidget()
            if old_parent is not None and old_parent.layout() is not None:
                old_parent.layout().removeWidget(group)
            root.addWidget(group)
            group.show()
        root.addStretch(1)
        return self._scroll(content)

    def _additional_tab(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)
        root.setSpacing(10)

        # NEO 169: local conversation, memory and the NEO command model — all on this computer.
        local_ai = QGroupBox("Общение и память")
        local_form = QFormLayout(local_ai)
        self._append_conversation_model184(local_form)
        self._local_ai_developer_group184 = QGroupBox("Локальные модели и диагностика")
        developer_form = QFormLayout(self._local_ai_developer_group184)
        self.local_chat_enabled169 = QCheckBox("Отвечать на вопросы и разговаривать через локальную модель (без интернета)")
        self.local_chat_enabled169.setChecked(bool(self.config.get("local_chat.enabled", True)))
        self.memory_enabled169 = QCheckBox("Запоминать мои предпочтения, названия команд и исправления")
        self.memory_enabled169.setChecked(bool(self.config.get("memory.enabled", True)))
        self.neo_model_enabled169 = QCheckBox("Понимать перефразированные команды моделью команд NEO")
        self.neo_model_enabled169.setChecked(bool(self.config.get("neo_model.enabled", True)))
        self.neo_model_schedule169 = QCheckBox("Дообучать модель команд ночью, когда компьютер не используется")
        self.neo_model_schedule169.setChecked(bool(self.config.get("neo_model.schedule.enabled", False)))
        self.log_content169 = QCheckBox("Подробный журнал: записывать тексты запросов и ответов (для диагностики)")
        self.log_content169.setChecked(bool(self.config.get("diagnostics.log_content", False)))
        for box in (self.local_chat_enabled169, self.memory_enabled169):
            local_form.addRow(box)
        append_memory_editor(self, local_form)
        for box in (self.neo_model_enabled169, self.neo_model_schedule169, self.log_content169):
            developer_form.addRow(box)
        local_form.addRow(self._help("Память: «что ты обо мне запомнил», «забудь …», «очисти сохранённую память». "
                                     "Поиск по файлам: «разреши искать в папке Документы». Модель команд: «покажи состояние модели команд»."))
        # NEO 170: the small NEO agent model (conversation and actions).
        self.llm_agent_enabled170 = QCheckBox("Модель ассистента NEO: разговор и выполнение просьб своими словами")
        self.llm_agent_enabled170.setChecked(bool(self.config.get("llm_agent.enabled", True)))
        self.llm_agent_size170 = CompactComboBox()
        self.llm_agent_size170.addItem("Qwen2.5 0.5B — минимум ресурсов", "0.5b")
        self.llm_agent_size170.addItem("Qwen2.5 1.5B — лучше понимает", "1.5b")
        self.llm_agent_size170.setCurrentIndex(max(0, self.llm_agent_size170.findData(str(self.config.get("llm_agent.size", "0.5b") or "0.5b"))))
        self.llm_finetune_auto170 = QCheckBox("Дообучать модель ассистента на командах NEO в фоне, когда компьютер простаивает")
        self.llm_finetune_auto170.setChecked(bool(self.config.get("llm_agent.finetune.auto", True)))
        developer_form.addRow(self.llm_agent_enabled170)
        developer_form.addRow("Размер модели ассистента", self.llm_agent_size170)
        developer_form.addRow(self.llm_finetune_auto170)
        # NEO 188 NEO-PRIVACY-002: applying a correction is not consent to training.
        self.train_on_corrections188 = QCheckBox("Использовать мои исправления команд для обучения моделей NEO (обезличенно)")
        self.train_on_corrections188.setChecked(bool(self.config.get("privacy.train_on_corrections", False)))
        developer_form.addRow(self.train_on_corrections188)
        from jarvis.runtime.training_corrections188 import settings_note as training_note188
        developer_form.addRow(self._help(training_note188()))
        # NEO 172: streaming answers, memory offers, measured learning (collection is off until the user agrees).
        self.local_stream172 = QCheckBox("Показывать ответ по мере его появления")
        self.local_stream172.setChecked(bool(self.config.get("local_chat.stream", True)))
        self.memory_propose172 = QCheckBox("Предлагать запомнить то, что я рассказываю о себе")
        self.memory_propose172.setChecked(bool(self.config.get("memory.propose", True)))
        self.learning_collect172 = QCheckBox("Собирать мои команды для проверки понимания (только выполненные команды и исправления, на этом компьютере)")
        self.learning_collect172.setChecked(bool(self.config.get("learning.collect_phrases", False)))
        # NEO 177: a typo or phrasing NEO understood once is answered at once next time.
        self.learning_remember177 = QCheckBox("Запоминать понятые опечатки и фразы, чтобы в следующий раз отвечать сразу (только короткие команды и приветствия, на этом компьютере)")
        self.learning_remember177.setChecked(bool(self.config.get("learning.remember_resolved", True)))
        for box172 in (self.local_stream172, self.memory_propose172, self.learning_remember177):
            local_form.addRow(box172)
        developer_form.addRow(self.learning_collect172)
        self.llm_agent_status170 = QLabel("")
        self.llm_agent_status170.setWordWrap(True)
        install = QPushButton("Установить / обновить модель")
        install.clicked.connect(self._install_agent_model170)
        row170 = QWidget()
        row170_layout = QHBoxLayout(row170)
        row170_layout.setContentsMargins(0, 0, 0, 0)
        row170_layout.addWidget(self.llm_agent_status170, 1)
        row170_layout.addWidget(install)
        developer_form.addRow(row170)
        developer_form.addRow(self._help("Модель ассистента понимает просьбы своими словами и превращает их в команды NEO "
                                     "(например: «открой браузер, загугли рецепт борща и перейди на первый сайт»), а на вопросы отвечает сама. "
                                     "Любое действие проходит те же проверки и подтверждения, что и обычная команда."))
        # NEO 187 HREQ-007: the block is shown on «Ассистент → Общение и память».
        # It is parked in this page until that page (built right after) adopts it.
        self._conversation_group187 = local_ai
        local_ai.hide()
        root.addWidget(local_ai)

        language_group = QGroupBox("Язык интерфейса и ассистента")
        language_form = QFormLayout(language_group)
        self.app_language = QComboBox()
        self.app_language.setProperty("i18n_skip_combo", True)
        for code, native_name in LANGUAGES.items():
            self.app_language.addItem(native_name, code)
        current_language = str(self.config.get("app.language", "en") or "en").casefold()
        lang_index = self.app_language.findData(current_language)
        self.app_language.setCurrentIndex(max(0, lang_index))
        self._add_field(
            language_form, "Язык", self.app_language,
            "Меняет интерфейс, язык ответов и словарь команд. Активен только словарь выбранного языка; словари остальных языков не подключаются до переключения языка. Имена треков, файлов, сайтов, приложений и другой пользовательский текст остаются без изменений.",
        )
        # Language is staged like every other setting and takes effect only
        # after «Применить» / «Сохранить».
        self.app_language.currentIndexChanged.connect(self._on_language_preview_changed)
        self.dialogue_memory_enabled = QCheckBox(dt("memory_enabled", "ru"))
        self.dialogue_memory_enabled.setChecked(bool(self.config.get("dialogue.memory.enabled", True)))
        local_form.addRow(self.dialogue_memory_enabled)
        self.dialogue_memory_days = self._spin(1, 30, int(self.config.get("dialogue.memory.retention_days", 7)), "", 1)
        self.dialogue_memory_turns = self._spin(20, 500, int(self.config.get("dialogue.memory.max_turns", 200)), "", 10)
        local_form.addRow(dt("retention", "ru"), self.dialogue_memory_days)
        local_form.addRow(dt("memory_turns", "ru"), self.dialogue_memory_turns)
        memory_help = QLabel(dt("memory_privacy", "ru")); memory_help.setWordWrap(True)
        local_form.addRow(memory_help)
        for field in (self.dialogue_memory_days, self.dialogue_memory_turns):
            field.setEnabled(self.dialogue_memory_enabled.isChecked())
            self.dialogue_memory_enabled.toggled.connect(field.setEnabled)
        root.addWidget(language_group)

        features_group = QGroupBox("Дополнительные возможности")
        features_root = QVBoxLayout(features_group)
        features_root.setSpacing(10)
        root.addWidget(features_group)

        startup_group = QGroupBox("Запуск и производительность")
        startup_form = QFormLayout(startup_group)
        self.autostart = QCheckBox("Запускать вместе с Windows")
        self.autostart.setChecked(bool(self.config.get("ui.start_with_windows", False)))
        self.priority_startup = QCheckBox("Приоритетный запуск — запуск с административными правами сразу")
        self.priority_startup.setChecked(bool(self.config.get("ui.priority_startup", False)))
        self.priority_startup.setEnabled(self.autostart.isChecked())
        self.priority_startup.setToolTip(
            "Создаёт задачу Планировщика Windows с максимальными правами и запускает NEO сразу при создании пользовательской сессии. При первом включении Windows может один раз показать UAC."
        )
        self.autostart.toggled.connect(self.priority_startup.setEnabled)
        startup_form.addRow(self.autostart)
        startup_form.addRow(self.priority_startup)
        # NEO 168: one switch for listening frequency, screen-picture size and how long the vision model stays loaded.
        # 186: moved here from «Окно» (settings TZ 5.15), next to startup.
        self.performance_profile = CompactComboBox()
        for profile_key, profile_label in (("low", "Низкий — слабый ПК и игры"), ("balanced", "Сбалансированный"),
                                           ("fast", "Быстрый"), ("maximum", "Максимальный"),
                                           ("auto", "Авто — по процессору и памяти")):
            self.performance_profile.addItem(profile_label, profile_key)
        profile_index = self.performance_profile.findData(str(self.config.get("performance.profile", "balanced") or "balanced"))
        self.performance_profile.setCurrentIndex(max(0, profile_index if profile_index >= 0 else 1))
        self._add_field(startup_form, "Профиль производительности", self.performance_profile, "Как часто NEO слушает микрофон, насколько подробно смотрит на экран и сколько держит модель зрения в памяти. Значения профиля применяются после сохранения.")
        root.addWidget(startup_group)

        storage = QGroupBox("Папки данных NEO")
        storage_form = QFormLayout(storage)
        unified_root = Path(self.config.paths.root)
        self.storage_data_dir = StoragePathField(str(unified_root), default_value=str(unified_root))
        self.storage_settings_dir184 = StoragePathField(str(self.config.paths.settings.parent), default_value=str(self.config.paths.settings.parent))
        self.storage_logs_dir = StoragePathField(str(unified_root / "logs"), default_value=str(unified_root / "logs"))
        self.storage_screenshots_dir = StoragePathField(str(unified_root / "screenshots"), default_value=str(unified_root / "screenshots"))
        self.storage_backups_dir = StoragePathField(str(unified_root / "backups"), default_value=str(unified_root / "backups"))
        self.storage_cache_dir = StoragePathField(str(unified_root / "cache"), default_value=str(unified_root / "cache"))
        self.storage_runtime_dir = StoragePathField(str(unified_root / "runtime"), default_value=str(unified_root / "runtime"))
        self.storage_startup_dir = StoragePathField(str(unified_root / "startup"), default_value=str(unified_root / "startup"))
        self.storage_bootstrap_dir = StoragePathField(str(unified_root / "bootstrap"), default_value=str(unified_root / "bootstrap"))
        self.storage_instance_dir = StoragePathField(str(unified_root / "instance"), default_value=str(unified_root / "instance"))
        storage_fields = (
            self.storage_data_dir, self.storage_settings_dir184, self.storage_logs_dir, self.storage_screenshots_dir,
            self.storage_backups_dir, self.storage_cache_dir, self.storage_runtime_dir,
            self.storage_startup_dir, self.storage_bootstrap_dir, self.storage_instance_dir,
        )
        for field in storage_fields:
            field.edit.setReadOnly(True)
            field.browse_button.hide()
            field.default_button.hide()
        self._add_field(storage_form, "Единая папка NEO", self.storage_data_dir, "Все пользовательские данные NEO теперь находятся под одним корнем LocalAppData/NeoJarvis. Старое Roaming/NeoJarvis переносится сюда автоматически.")
        self._add_field(storage_form, "Настройки", self.storage_settings_dir184, "settings.json и резервные копии настроек. Старый файл сохраняется для безопасного отката; действующие настройки находятся в подпапке «настройки».")
        self._add_field(storage_form, "Логи", self.storage_logs_dir, "Журналы работы, launcher/instance diagnostics и crash recovery находятся в едином дереве NEO.")
        self._add_field(storage_form, "Кэш", self.storage_cache_dir, "Временные данные NEO внутри единой папки.")
        self._add_field(storage_form, "Runtime", self.storage_runtime_dir, "Управляемая среда Python NEO.")
        self._add_field(storage_form, "Startup", self.storage_startup_dir, "Служебные файлы автозапуска NEO.")
        self._add_field(storage_form, "Bootstrap", self.storage_bootstrap_dir, "Служебные файлы первичной загрузки и восстановления.")
        self._add_field(storage_form, "Instance", self.storage_instance_dir, "Блокировка единственного экземпляра и IPC-состояние NEO.")
        self._add_field(storage_form, "Скриншоты", self.storage_screenshots_dir, "Снимки экрана, созданные NEO.")
        self._add_field(storage_form, "Резервные копии", self.storage_backups_dir, "Резервные копии и журнал безопасного отката.")
        root.addWidget(storage)

        window_voice_group = QGroupBox("Голосовое управление окном")
        window_voice_form = QFormLayout(window_voice_group)
        self.voice_window_visibility_commands = QCheckBox("Разрешить команды «покажи окно» / «скройся»")
        self.voice_window_visibility_commands.setChecked(bool(self.config.get("ui.voice_window_visibility_commands_enabled", True)))
        window_voice_form.addRow(self.voice_window_visibility_commands)
        features_root.addWidget(window_voice_group)
        features_root.addWidget(self._help("Персональный словарь находится в разделе «Ассистент → Микрофон и wake word»."))

        # 186 (settings TZ 5.15): language, startup and the performance profile
        # come first, then conversation and memory; folders and window voice after.
        monitoring_group = self._monitoring_group188()
        for group in (language_group, startup_group, local_ai, storage, features_group):
            root.removeWidget(group)
        for group in (language_group, startup_group, local_ai, storage, features_group, monitoring_group):
            root.addWidget(group)
        root.addStretch(1)
        return self._scroll(content)

    def _monitoring_group188(self) -> QGroupBox:
        """NEO 188 HREQ-020/021: temperature and process-load notices, Steam installs, LibreHardwareMonitor."""
        group = QGroupBox("Мониторинг оборудования и процессов")
        form = QFormLayout(group)
        self.hw_alerts188 = QCheckBox("Предупреждать о высокой температуре процессора и видеокарты")
        self.hw_alerts188.setChecked(bool(self.config.get("monitoring.hardware_alerts_enabled", False)))
        form.addRow(self.hw_alerts188)
        self.cpu_temp188 = self._spin(50, 110, int(self.config.get("monitoring.cpu_temp_c", 90) or 90), " °C")
        self.gpu_temp188 = self._spin(50, 110, int(self.config.get("monitoring.gpu_temp_c", 85) or 85), " °C")
        form.addRow("Порог процессора", self.cpu_temp188)
        form.addRow("Порог видеокарты", self.gpu_temp188)
        self.proc_alerts188 = QCheckBox("Предупреждать о резком росте нагрузки отдельных процессов")
        self.proc_alerts188.setChecked(bool(self.config.get("monitoring.process_alerts_enabled", False)))
        form.addRow(self.proc_alerts188)
        self.proc_cpu188 = self._spin(10, 100, int(self.config.get("monitoring.process_cpu_percent", 60) or 60), " %")
        self.proc_ram188 = self._spin(256, 131072, int(self.config.get("monitoring.process_ram_mb", 4096) or 4096), " МБ", 256)
        form.addRow("Порог процессора для процесса", self.proc_cpu188)
        form.addRow("Порог памяти для процесса", self.proc_ram188)
        self.steam_installs188 = QCheckBox("Сообщать о начале и подтверждённом завершении установок Steam")
        self.steam_installs188.setChecked(bool(self.config.get("monitoring.steam_installs_enabled", False)))
        form.addRow(self.steam_installs188)
        self.monitor_interval188 = self._spin(15, 600, int(self.config.get("monitoring.interval_seconds", 30) or 30), " с", 5)
        form.addRow("Как часто проверять", self.monitor_interval188)
        form.addRow(self._help("Проверки идут только при включённых флажках. Нагрузка процесса — доля всего процессора; "
                               "видеопамять отдельных процессов Windows здесь не показывает. Установка Steam считается завершённой "
                               "только по отметке Steam в манифесте игры, а не по прекращению загрузки."))
        from jarvis.runtime import lhm188
        self.lhm_folder188 = QLineEdit(str(lhm188.default_folder(self.config)))
        form.addRow("Папка LibreHardwareMonitor", self.lhm_folder188)
        self.lhm_status188 = QLabel("")
        self.lhm_status188.setWordWrap(True)
        self.lhm_status188.setProperty("i18n_skip_text", True)
        buttons = QWidget()
        row = QHBoxLayout(buttons)
        row.setContentsMargins(0, 0, 0, 0)
        for label, action in (("Скачать", "download"), ("Проверить", "check"), ("Запустить", "launch"), ("Удалить", "remove")):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, a=action: self._lhm_action188(a))
            row.addWidget(button)
        form.addRow(buttons)
        form.addRow(self.lhm_status188)
        form.addRow(self._help("LibreHardwareMonitor даёт температуры процессора через WMI. Он скачивается с официального GitHub только по кнопке; "
                               "при запуске Windows может запросить разрешение — NEO этот запрос не обходит. Датчики, которых нет у устройства, не показываются."))
        return group

    def _lhm_action188(self, action: str) -> None:
        from jarvis.runtime import lhm188
        code = language_code(self.config)
        if getattr(self, "_lhm_busy188", False):
            return
        folder = self.lhm_folder188.text().strip()
        base = self.config

        class StagedConfig:
            # The folder typed here is used for this action only; nothing is saved before «Сохранить».
            paths = base.paths

            def get(self, key, default=None):
                return folder if key == "monitoring.lhm_folder" else base.get(key, default)
        staged = StagedConfig()

        def run():
            if action == "download":
                return lhm188.t("downloaded", code, folder=str(lhm188.download(staged)))
            if action == "launch":
                lhm188.launch(staged)
                return lhm188.t("launched", code)
            if action == "remove":
                lhm188.remove(staged)
                return lhm188.t("removed", code)
            return lhm188.status_text(lhm188.check(staged), code)

        def done(text):
            self._lhm_busy188 = False
            if not self._settings_closed160:
                self.lhm_status188.setText(str(text))

        def failed(trace):
            self._lhm_busy188 = False
            if self._settings_closed160:
                return
            error = getattr(self, "_lhm_error188", None)
            self.lhm_status188.setText(lhm188.error_text(error, code) if error is not None else str(trace).splitlines()[-1][:300])

        def guarded():
            self._lhm_error188 = None
            try:
                return run()
            except lhm188.LhmError as exc:
                self._lhm_error188 = exc
                raise
        self._lhm_busy188 = True
        self.lhm_status188.setText("…")
        worker = FunctionWorker(guarded)
        worker.signals.result.connect(done)
        worker.signals.error.connect(failed)
        QThreadPool.globalInstance().start(worker)

    @staticmethod
    def _hotkey_controls184(edit: QWidget, default_value: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        clear = QPushButton("Очистить")
        reset = QPushButton("По умолчанию")
        clear.clicked.connect(lambda _checked=False: edit.clear())
        def reset_value(_checked=False):
            if isinstance(edit, QKeySequenceEdit):
                edit.setKeySequence(QKeySequence(default_value))
            else:
                edit.setText(default_value)
        reset.clicked.connect(reset_value)
        layout.addWidget(edit, 1)
        layout.addWidget(clear)
        layout.addWidget(reset)
        return row

    def _hotkeys_tab184(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)
        hotkeys = QGroupBox("Горячие клавиши")
        hotkey_form = QFormLayout(hotkeys)

        self.app_mic_hotkey = QKeySequenceEdit(QKeySequence(str(self.config.get("hotkeys.app_microphone_toggle", "Ctrl+Alt+M"))))
        self.autoclicker_hotkey = QKeySequenceEdit(QKeySequence(str(self.config.get("hotkeys.autoclicker_toggle", "Ctrl+Alt+C"))))
        self.chat_scroll_up_hotkey = QKeySequenceEdit(QKeySequence(str(self.config.get("hotkeys.chat_scroll_up", "Ctrl+Alt+Up"))))
        self.chat_scroll_down_hotkey = QKeySequenceEdit(QKeySequence(str(self.config.get("hotkeys.chat_scroll_down", "Ctrl+Alt+Down"))))
        self.window_topmost_hotkey = QKeySequenceEdit(QKeySequence(str(self.config.get("hotkeys.window_topmost_toggle", "Ctrl+Alt+T"))))
        self._add_field(hotkey_form, "Вкл./выкл. микрофон NEO", self._hotkey_controls184(self.app_mic_hotkey, "Ctrl+Alt+M"), "Глобальный хоткей выключает только прослушивание микрофона внутри NEO. Системный микрофон Windows и другие приложения не затрагиваются.")
        self._add_field(hotkey_form, "Вкл./выкл. автокликер", self._hotkey_controls184(self.autoclicker_hotkey, "Ctrl+Alt+C"), "Глобально переключает автокликер. Настройки кнопки и скорости находятся ниже.")
        self._add_field(hotkey_form, "Прокрутка чата вверх", self._hotkey_controls184(self.chat_scroll_up_hotkey, "Ctrl+Alt+Up"), "При удержании горячей клавиши история чата плавно прокручивается вверх и автоматически открепляется от генерации.")
        self._add_field(hotkey_form, "Прокрутка чата вниз", self._hotkey_controls184(self.chat_scroll_down_hotkey, "Ctrl+Alt+Down"), "При удержании горячей клавиши история чата плавно прокручивается вниз. В самой нижней точке автоматическая привязка к ответу включается снова.")
        self._add_field(hotkey_form, dt("toggle_window", "ru"), self._hotkey_controls184(self.window_topmost_hotkey, "Ctrl+Alt+T"))
        root.addWidget(hotkeys)
        conflicts = sorted(getattr(self.parentWidget(), "_hotkey_conflicts", set()))
        if conflicts:
            notice = QLabel("\n".join(dt("hotkey_conflict", language_code(self.config)).format(key=key) for key in conflicts))
            notice.setWordWrap(True)
            hotkey_form.addRow(notice)

        clicker_group = QGroupBox("Автокликер")
        clicker_form = QFormLayout(clicker_group)
        self.autoclicker_enabled = QCheckBox("Разрешить автокликер и голосовые команды")
        self.autoclicker_enabled.setChecked(bool(self.config.get("autoclicker.enabled", True)))
        self.autoclicker_cps = QDoubleSpinBox(); self.autoclicker_cps.setRange(0.5, 100.0); self.autoclicker_cps.setDecimals(1); self.autoclicker_cps.setSingleStep(1.0); self.autoclicker_cps.setSuffix(" кликов/с")
        self.autoclicker_cps.setValue(float(self.config.get("autoclicker.cps", 10.0) or 10.0))
        self.autoclicker_button = QComboBox(); self.autoclicker_button.addItem("Левая", "left"); self.autoclicker_button.addItem("Правая", "right"); self.autoclicker_button.addItem("Средняя", "middle")
        wanted_button = str(self.config.get("autoclicker.button", "left") or "left"); idx = self.autoclicker_button.findData(wanted_button); self.autoclicker_button.setCurrentIndex(max(0, idx))
        clicker_form.addRow(self.autoclicker_enabled)
        self._add_field(clicker_form, "Скорость", self.autoclicker_cps, "Скорость можно менять и голосом: «выставь скорость автокликера 10 кликов в секунду».")
        self._add_field(clicker_form, "Кнопка мыши", self.autoclicker_button, "Левая, правая или средняя кнопка. Голосовая команда может временно выбрать другую кнопку.")
        root.addWidget(clicker_group)

        root.addStretch(1)
        return self._scroll(content)

    def _developer_settings_tab184(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)
        developer_switch_group = QGroupBox("Режим разработчика")
        developer_switch_form = QFormLayout(developer_switch_group)
        self.developer_settings_enabled = QCheckBox("Включить режим разработчика")
        self.developer_settings_enabled.setChecked(bool(self.config.get("developer_mode.settings_enabled", False)))
        developer_switch_form.addRow(self.developer_settings_enabled)
        developer_switch_form.addRow(self._help(
            "По умолчанию NEO автоматически выбирает локальную модель и безопасные параметры. "
            "Режим разработчика возвращает скрытые настройки Ollama, моделей, контекста, "
            "длины ответа, таймаутов и диагностического pipeline inspector."
        ))
        root.addWidget(developer_switch_group)

        self.developer_advanced_container = QWidget()
        developer_advanced_layout = QVBoxLayout(self.developer_advanced_container)
        developer_advanced_layout.setContentsMargins(0, 0, 0, 0)
        developer_advanced_layout.setSpacing(10)
        self.show_tools = QCheckBox("Показывать технические детали выполненных действий")
        self.show_tools.setChecked(bool(self.config.get("ui.show_tool_details", False)))
        developer_advanced_layout.addWidget(self.show_tools)
        developer_advanced_layout.addWidget(self._local_ai_developer_group184)
        self._adopt_scroll_sections(developer_advanced_layout, self._models_tab())
        self._adopt_scroll_sections(developer_advanced_layout, self._developer_tab())
        self.developer_advanced_container.setVisible(self.developer_settings_enabled.isChecked())
        self.developer_settings_enabled.toggled.connect(self.developer_advanced_container.setVisible)
        root.addWidget(self.developer_advanced_container)

        atlas_group = QGroupBox("Phrase Atlas 2.0")
        atlas_form = QFormLayout(atlas_group)
        self.atlas2_enabled = QCheckBox("Включить Atlas 2.0 (Static → Semantic → Context → AI)")
        self.atlas2_enabled.setChecked(bool(self.config.get("routing.atlas2_enabled", True)))
        self.atlas2_semantic = QCheckBox("Семантический matcher для гибких формулировок")
        self.atlas2_semantic.setChecked(bool(self.config.get("routing.semantic_enabled", True)))
        self.atlas2_lazy_vision = QCheckBox("LazyVision — анализировать экран только по необходимости")
        self.atlas2_lazy_vision.setChecked(bool(self.config.get("routing.lazy_vision_enabled", True)))
        self.atlas2_threshold = QDoubleSpinBox()
        self.atlas2_threshold.setRange(0.30, 0.90); self.atlas2_threshold.setSingleStep(0.01); self.atlas2_threshold.setDecimals(2)
        self.atlas2_threshold.setValue(float(self.config.get("routing.semantic_threshold", 0.43) or 0.43))
        atlas_form.addRow(self.atlas2_enabled)
        atlas_form.addRow(self.atlas2_semantic)
        atlas_form.addRow(self.atlas2_lazy_vision)
        self._add_field(atlas_form, "Порог semantic matcher", self.atlas2_threshold, "Чем выше значение, тем строже Atlas принимает гибкую формулировку. Для компактных локальных векторов NEO шкала ниже, чем у transformer-эмбеддингов: рекомендуемое значение 0.43. Опасные/целевые команды всё равно не исполняются только по similarity и проходят обычную SecurityPolicy.")
        atlas_form.addRow(self._help("Static/Regex остаётся самым быстрым и приоритетным уровнем. Semantic использует компактные локальные предрасчитанные векторы NumPy без FAISS/USearch и без отдельной модели. LazyVision не делает постоянных скриншотов."))
        developer_advanced_layout.addWidget(atlas_group)

        root.addStretch(1)
        return self._scroll(content)

    def _developer_tab(self) -> QWidget:
        content=QWidget(); root=QVBoxLayout(content); group=QGroupBox("Developer Mode / pipeline inspector"); form=QFormLayout(group)
        self.dev_enabled=QCheckBox("Показывать отдельное окно Developer")
        self.dev_enabled.setChecked(bool(self.config.get("developer_mode.enabled", False)))
        self.dev_always_on_top=QCheckBox("Developer всегда поверх окон")
        self.dev_always_on_top.setChecked(bool(self.config.get("developer_mode.always_on_top", True)))
        self.dev_raw=QCheckBox("Распознанная речь"); self.dev_raw.setChecked(bool(self.config.get("developer_mode.show_raw_speech", True)))
        self.dev_norm=QCheckBox("Нормализованный текст"); self.dev_norm.setChecked(bool(self.config.get("developer_mode.show_normalized", True)))
        self.dev_intent=QCheckBox("Intent / entities / fallback"); self.dev_intent.setChecked(bool(self.config.get("developer_mode.show_intent", True)))
        self.dev_tools=QCheckBox("Выбранный tool и результат"); self.dev_tools.setChecked(bool(self.config.get("developer_mode.show_tool", True)))
        self.dev_latency=QCheckBox("Latency profiler"); self.dev_latency.setChecked(bool(self.config.get("developer_mode.show_latency", True)))
        self.dev_slow=QDoubleSpinBox(); self.dev_slow.setRange(0.2,20.0); self.dev_slow.setSingleStep(0.2); self.dev_slow.setSuffix(" сек"); self.dev_slow.setValue(float(self.config.get("developer_mode.slow_threshold_seconds",2.0)))
        for w in (self.dev_enabled,self.dev_always_on_top,self.dev_raw,self.dev_norm,self.dev_intent,self.dev_tools,self.dev_latency): form.addRow(w)
        self._add_field(form,"Подсветка медленных этапов от",self.dev_slow,"В окне Developer видно speech → normalized → intent/entities → tool/fallback → latency → result. Оно только наблюдает и не вмешивается в действия.")
        root.addWidget(group); root.addStretch(1); return self._scroll(content)

    @staticmethod
    def _preset_names() -> list[str]:
        # Keep Window and Robot preset lists deterministic and easy to scan.
        return sorted(THEME_PRESETS.keys(), key=lambda value: value.casefold())

    def _mark_window_custom(self) -> None:
        if self._preset_guard or not hasattr(self, "window_theme_preset"):
            return
        custom_index = self.window_theme_preset.findData("Custom")
        if custom_index >= 0 and self.window_theme_preset.currentIndex() != custom_index:
            self.window_theme_preset.blockSignals(True)
            self.window_theme_preset.setCurrentIndex(custom_index)
            self.window_theme_preset.blockSignals(False)

    def _mark_robot_custom(self) -> None:
        if self._preset_guard or not hasattr(self, "robot_theme_preset"):
            return
        custom_index = self.robot_theme_preset.findData("Custom")
        if custom_index >= 0 and self.robot_theme_preset.currentIndex() != custom_index:
            self.robot_theme_preset.blockSignals(True)
            self.robot_theme_preset.setCurrentIndex(custom_index)
            self.robot_theme_preset.blockSignals(False)

    def _mark_panel_custom(self, style_key: str) -> None:
        if self._preset_guard or not hasattr(self, "overlay_type_style_fields"):
            return
        fields = self.overlay_type_style_fields.get(str(style_key), {})
        combo = fields.get("theme_preset")
        if not isinstance(combo, QComboBox):
            return
        custom_index = combo.findData("Custom")
        if custom_index >= 0 and combo.currentIndex() != custom_index:
            combo.blockSignals(True)
            combo.setCurrentIndex(custom_index)
            combo.blockSignals(False)

    def _apply_panel_preset(self, style_key: str, preset_name: str) -> None:
        preset = THEME_PRESETS.get(str(preset_name), {}).get("window", {})
        fields = getattr(self, "overlay_type_style_fields", {}).get(str(style_key), {})
        if not preset or not fields:
            return
        mapping = {
            "text_color": "text_color",
            "background_color": "panel_color",
            "border_color": "border_color",
        }
        self._preset_guard = True
        try:
            for field_name, preset_key in mapping.items():
                field = fields.get(field_name)
                if isinstance(field, ColorField) and preset.get(preset_key):
                    field.edit.setText(str(preset[preset_key]))
            if str(style_key) == "music":
                music_mapping = {
                    "control_color": "text_color",
                    "control_background_color": "element_color",
                    "control_border_color": "border_color",
                }
                for field_name, preset_key in music_mapping.items():
                    field = fields.get(field_name)
                    if isinstance(field, ColorField) and preset.get(preset_key):
                        field.edit.setText(str(preset[preset_key]))
        finally:
            self._preset_guard = False

    def _apply_window_preset(self, preset_name: str) -> None:
        preset = THEME_PRESETS.get(str(preset_name), {}).get("window", {})
        if not preset or not hasattr(self, "ui_colors"):
            return
        self._preset_guard = True
        try:
            for key, value in preset.items():
                field = self.ui_colors.get(key)
                if field is not None:
                    field.edit.setText(value)
        finally:
            self._preset_guard = False
        # Do not preview the window palette here; Apply/Save owns the commit.

    def _apply_robot_preset(self, preset_name: str) -> None:
        preset = THEME_PRESETS.get(str(preset_name), {}).get("robot", {})
        if not preset or not hasattr(self, "robot_colors"):
            return
        self._preset_guard = True
        try:
            for key, value in preset.items():
                field = self.robot_colors.get(key)
                if field is not None:
                    field.edit.setText(value)
            # Persistent accessories follow the robot palette when a preset is
            # selected, but remain individually editable afterwards.
            if hasattr(self, "robot_accessory_colors"):
                mapping = {
                    "robot_accessory_mug_color": preset.get("robot_shell_color", "#DDE7F4"),
                    "robot_accessory_hand_color": preset.get("robot_body_color", "#313C4F"),
                }
                for key, value in mapping.items():
                    field = self.robot_accessory_colors.get(key)
                    if field is not None:
                        field.edit.setText(value)
        finally:
            self._preset_guard = False

    def _appearance_tab(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)

        geometry = QGroupBox("Размер и поведение окна")
        form = QFormLayout(geometry)
        self.window_width = self._spin(280, 2200, int(self.config.get("ui.window_width", 322)), " px", 2)
        self.window_height = self._spin(320, 1800, int(self.config.get("ui.window_height", 476)), " px", 2)
        self.drag_enabled = QCheckBox("Разрешить перетаскивание окна за верхнюю область")
        self.drag_enabled.setChecked(bool(self.config.get("ui.drag_enabled", True)))
        self.robot_only_mode = QCheckBox("Режим «только робот» — скрыть чат и ввод, оставить нижние кнопки управления")
        self.robot_only_mode.setChecked(bool(self.config.get("ui.robot_only_mode", False)))
        self.robot_only_mode.setToolTip("Чат и строка ввода будут скрыты. Робот, его фон и нижние кнопки управления останутся видимыми; прозрачность регулируется обычными настройками.")
        self.chat_font_family = CompactFontComboBox()
        self.chat_font_family.setCurrentFont(QFont(str(self.config.get("ui.chat_font_family", "Segoe UI") or "Segoe UI")))
        self.chat_font_size = self._spin(8, 36, int(self.config.get("ui.chat_font_size_px", 14)), " px", 1)
        self.corner_radius = self._spin(0, 60, int(self.config.get("ui.corner_radius", 26)), " px", 2)
        self.animation_fps = CompactComboBox()
        for fps in (30, 60):
            self.animation_fps.addItem(f"{fps} FPS", fps)
        configured_fps = int(self.config.get("ui.animation_fps", 60) or 60)
        fps_index = self.animation_fps.findData(configured_fps if configured_fps in {30, 60} else 60)
        self.animation_fps.setCurrentIndex(max(0, fps_index))
        self._add_field(form, "Ширина", self.window_width, "Ширина основного окна в пикселях.")
        self._add_field(form, "Высота", self.window_height, "Высота основного окна в пикселях. Окно также можно растягивать вручную.")
        form.addRow(self.drag_enabled)
        form.addRow(self.robot_only_mode)
        self._add_field(form, "Шрифт диалогов", self.chat_font_family, "Семейство шрифта только для сообщений, имени автора, размышления и технических деталей чата. Список показывает 6 строк и прокручивается колесом/scrollbar.")
        self._add_field(form, "Размер текста диалога", self.chat_font_size, "Размер текста сообщений в чате в пикселях. Он меняется независимо от размера окна и не масштабирует кнопки/служебные элементы.")
        self._add_field(form, "Скругление углов", self.corner_radius)
        self._add_field(form, "Частота анимаций", self.animation_fps, "Единая частота кадров для робота, индикаторов, посимвольного вывода и музыкальной волны. 60 FPS — максимум, чтобы не расходовать CPU впустую; аудио/ASR работают независимо.")
        root.addWidget(geometry)

        preset_group = QGroupBox("Пресет окна")
        preset_form = QFormLayout(preset_group)
        self.window_theme_preset = CompactComboBox()
        self.window_theme_preset.addItem("Пользовательский", "Custom")
        for name in self._preset_names():
            self.window_theme_preset.addItem(name, name)
        configured_window_preset = str(self.config.get("ui.window_theme_preset", "NEO Cyan") or "NEO Cyan")
        preset_index = self.window_theme_preset.findData(configured_window_preset)
        self.window_theme_preset.setCurrentIndex(max(0, preset_index))
        self._add_field(
            preset_form,
            "Цветовая схема",
            self.window_theme_preset,
            "Семейство NEO включает Cyan, Orange, Blue, Purple, Yellow, Green, Red, Gray, Dark, Full Dark и White. Те же пресеты доступны для робота; изменения вступают в силу только после «Применить» или «Сохранить».",
        )
        root.addWidget(preset_group)

        colors = QGroupBox("Цвета интерфейса")
        color_form = QFormLayout(colors)
        self.ui_colors: dict[str, ColorField] = {}
        color_specs = [
            ("accent", "Акцент и выделение", "#57D7FF"),
            ("window_color", "Основной фон окна", "#0C1017"),
            ("panel_color", "Наполнение панелей", "#111722"),
            ("border_color", "Рамка окна", "#57D7FF"),
            ("text_color", "Основной текст", "#EAF2FF"),
            ("muted_text_color", "Дополнительный текст", "#8F9DB2"),
            ("element_color", "Кнопки и поле ввода", "#192333"),
            ("element_hover_color", "Элементы при наведении", "#243247"),
            ("chat_color", "Фон диалоговой области", "#101722"),
            ("chat_user_color", "Сообщения пользователя", "#23465A"),
            ("chat_assistant_color", "Сообщения робота", "#182232"),
        ]
        for key, title, default in color_specs:
            field = ColorField(str(self.config.get(f"ui.{key}", default)))
            field.edit.textChanged.connect(lambda _text: self._window_color_changed())
            self.ui_colors[key] = field
            color_form.addRow(title, field)
        root.addWidget(colors)

        transparency = QGroupBox("Прозрачность интерфейса")
        opacity_form = QFormLayout(transparency)
        legacy_bg = bool(self.config.get("ui.transparent_background", False))
        legacy_border = bool(self.config.get("ui.transparent_borders", False))
        self.window_background_opacity = self._percent_spin(int(self.config.get("ui.window_opacity_percent", 0 if legacy_bg else 100)))
        self.panel_opacity = self._percent_spin(int(self.config.get("ui.panel_opacity_percent", 100)))
        self.element_opacity = self._percent_spin(int(self.config.get("ui.element_opacity_percent", 100)))
        self.chat_opacity = self._percent_spin(int(self.config.get("ui.chat_opacity_percent", 100)))
        self.bubble_opacity = self._percent_spin(int(self.config.get("ui.bubble_opacity_percent", 100)))
        self.border_opacity = self._percent_spin(int(self.config.get("ui.border_opacity_percent", 0 if legacy_border else 100)))
        self._add_field(opacity_form, "Фон всего окна", self.window_background_opacity, "0% = фон физически прозрачный: сквозь окно виден рабочий стол/живые обои, а не чёрная заливка.")
        self._add_field(opacity_form, "Панели", self.panel_opacity, "0% убирает заливку внутренних панелей.")
        self._add_field(opacity_form, "Кнопки и поля", self.element_opacity, "Можно оставить только текст/контуры элементов.")
        self._add_field(opacity_form, "Диалоговая область", self.chat_opacity)
        self._add_field(opacity_form, "Сообщения", self.bubble_opacity)
        self._add_field(opacity_form, "Рамки", self.border_opacity, "0% полностью скрывает внешние и внутренние рамки.")
        opacity_form.addRow(self._help("Для полностью «голого» интерфейса выставьте фон окна, панели, чат, сообщения, элементы и рамки в 0%. Текст и интерактивные зоны останутся доступными."))
        root.addWidget(transparency)

        # Apply a selected preset only after all ColorField widgets exist.
        self.window_theme_preset.currentIndexChanged.connect(
            lambda _index: self._apply_window_preset(str(self.window_theme_preset.currentData() or ""))
        )

        self._append_briefing_settings(root)
        root.addStretch(1)
        return self._scroll(content)

    def _robot_tab(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)
        root.addWidget(self._help("Настройки робота отделены от окна. Атмосферные эффекты, внешний вид, информационные элементы и постоянные атрибуты применяются ко всем обычным состояниям и анимациям робота."))
        self._append_robot_settings(root)
        root.addStretch(1)
        return self._scroll(content)

    def _set_shelf_cell_height_enabled(self, _index: int = -1) -> None:
        """Keep square shelf cells from exposing a meaningless height field.

        A named slot is used instead of a zero-argument lambda.  PySide emits
        the integer ``currentIndexChanged`` payload, and Cython preserves that
        signal signature in the source-free build.
        """
        field = self.shelf_dimensions.get("cell_height")
        if field is not None:
            field.setEnabled(self.shelf_cell_shape.currentData() != "square")

    def _shelf_tab(self):
        from jarvis.ui.desktop_events import request_desktop
        content = QWidget()
        root = QVBoxLayout(content)
        group = QGroupBox(dt("shelf", "ru"))
        form = QFormLayout(group)
        self.shelf_enabled = QCheckBox(dt("shelf_enabled", "ru"))
        self.shelf_enabled.setChecked(bool(self.config.get("shelf.enabled", True)))
        self.shelf_managed = QCheckBox(dt("managed", "ru"))
        self.shelf_managed.setChecked(bool(self.config.get("shelf.managed_copy_default", False)))
        self.shelf_restore_pinned = QCheckBox(dt("restore_pinned", "ru"))
        self.shelf_restore_pinned.setChecked(bool(self.config.get("shelf.restore_pinned", True)))
        self.shelf_layout_editable = QCheckBox(dt("layout_editable", "ru"))
        self.shelf_layout_editable.setChecked(bool(self.config.get("shelf.layout_editable", True)))
        # NEO 187 HREQ-008: the user asked what «Включить книжную полку» does —
        # explain it next to the switch (the feature itself stays).
        form.addRow(self._help(dt("shelf_help187", "ru")))
        for field in (self.shelf_enabled, self.shelf_managed, self.shelf_restore_pinned, self.shelf_layout_editable): form.addRow(field)
        self.bookshelf_hotkey = QKeySequenceEdit(QKeySequence(str(self.config.get("hotkeys.bookshelf_toggle", "Ctrl+Alt+B") or "Ctrl+Alt+B")))
        form.addRow("Горячие клавиши", self.bookshelf_hotkey)
        self.shelf_font_size = QSpinBox()
        self.shelf_font_size.setRange(12, 28)
        self.shelf_font_size.setValue(int(self.config.get("shelf.font_size", 14)))
        form.addRow(dt("font_size", "ru"), self.shelf_font_size)
        self.shelf_font_family = QFontComboBox()
        self.shelf_font_family.setCurrentFont(QFont(str(self.config.get("shelf.font_family", "Segoe UI"))))
        form.addRow(dt("font_family", "ru"), self.shelf_font_family)
        self.shelf_font_bold = QCheckBox(dt("font_bold", "ru"))
        self.shelf_font_bold.setChecked(bool(self.config.get("shelf.font_bold", False)))
        self.shelf_font_italic = QCheckBox(dt("font_italic", "ru"))
        self.shelf_font_italic.setChecked(bool(self.config.get("shelf.font_italic", False)))
        form.addRow(self.shelf_font_bold, self.shelf_font_italic)
        self.shelf_alignment = QComboBox()
        for key in ("left", "center", "right"): self.shelf_alignment.addItem(dt("align_" + key, "ru"), key)
        self.shelf_alignment.setCurrentIndex(max(0, self.shelf_alignment.findData(str(self.config.get("shelf.text_alignment", "left")))))
        form.addRow(dt("text_alignment", "ru"), self.shelf_alignment)
        self.shelf_cell_shape = QComboBox()
        for key in ("rectangle", "square"): self.shelf_cell_shape.addItem(dt(key, "ru"), key)
        self.shelf_cell_shape.setCurrentIndex(max(0, self.shelf_cell_shape.findData(str(self.config.get("shelf.cell_shape", "rectangle")))))
        form.addRow(dt("cell_shape", "ru"), self.shelf_cell_shape)
        self.shelf_dimensions = {}
        for prefix, title, limits, defaults in (("section", "section_size", (240, 1600, 180, 1400), (360, 420)), ("cell", "cell_size", (100, 400, 64, 400), (180, 100)), ("editor", "editor_size", (280, 1600, 220, 1400), (420, 360))):
            row = QWidget(); fields = QHBoxLayout(row); fields.setContentsMargins(0, 0, 0, 0)
            for dimension, lo, hi, default in (("width", limits[0], limits[1], defaults[0]), ("height", limits[2], limits[3], defaults[1])):
                key = prefix + "_" + dimension
                field = self._spin(lo, hi, int(self.config.get("shelf." + key, default)), " px", 10)
                field.setToolTip(dt(dimension, "ru"))
                fields.addWidget(QLabel(dt(dimension, "ru")))
                fields.addWidget(field)
                self.shelf_dimensions[key] = field
            form.addRow(dt(title, "ru"), row)
        self.shelf_cell_shape.currentIndexChanged.connect(self._set_shelf_cell_height_enabled)
        self._set_shelf_cell_height_enabled()
        self.shelf_opacity = QSpinBox()
        self.shelf_opacity.setRange(25, 100)
        self.shelf_opacity.setValue(int(self.config.get("shelf.opacity_percent", 100)))
        self.shelf_opacity.setSuffix(" %")
        form.addRow(dt("opacity", "ru"), self.shelf_opacity)
        self.shelf_background_color = ColorField(str(self.config.get("shelf.background_color", "#172536")))
        self.shelf_background_opacity = QSpinBox()
        self.shelf_background_opacity.setRange(0, 100)
        self.shelf_background_opacity.setSuffix(" %")
        self.shelf_background_opacity.setValue(int(self.config.get("shelf.background_opacity_percent", 85)))
        form.addRow(dt("shelf_background_color", "ru"), self.shelf_background_color)
        form.addRow(dt("shelf_background_opacity", "ru"), self.shelf_background_opacity)
        button = QPushButton(dt("shelf", "ru"))
        button.clicked.connect(lambda: request_desktop("shelf.show"))
        form.addRow(button)
        root.addWidget(group)
        self._append_additional_panels(root)
        if hasattr(self, "timer_widgets_group"):
            root.addWidget(self.timer_widgets_group)
        for layout in content.findChildren(QFormLayout):
            layout.setRowWrapPolicy(QFormLayout.WrapLongRows)
            for row in range(layout.rowCount()):
                cell = layout.itemAt(row, QFormLayout.LabelRole)
                label = cell.widget() if cell else None
                if isinstance(label, QLabel):
                    label.setWordWrap(True)
                    label.setMaximumWidth(250)
        root.addStretch(1)
        return self._scroll(content)

    def _append_desktop_robot_settings(self, root: QVBoxLayout) -> None:
        group = QGroupBox("Медиа")
        form = QVBoxLayout(group)
        self.robot_headphones_enabled = QCheckBox(dt("headphones", "ru"))
        self.robot_headphones_enabled.setChecked(bool(self.config.get("ui.robot_headphones_enabled", True)))
        form.addWidget(self.robot_headphones_enabled)
        controls = QWidget()
        colors = QFormLayout(controls)
        self.robot_headphones_color = ColorField(str(self.config.get("ui.robot_headphones_color", "#57D7FF")))
        colors.addRow(dt("headphones_color", "ru"), self.robot_headphones_color)
        presets = CompactComboBox()
        presets.addItem("Пользовательский", "")
        for name in self._preset_names():
            presets.addItem(name, name)
        presets.currentIndexChanged.connect(lambda _i: self.robot_headphones_color.edit.setText(str(THEME_PRESETS.get(str(presets.currentData()), {}).get("robot", {}).get("robot_glow_color", self.robot_headphones_color.edit.text()))))
        colors.addRow("Цветовая схема", presets)
        form.addWidget(controls)
        controls.setVisible(self.robot_headphones_enabled.isChecked())
        self.robot_headphones_enabled.toggled.connect(controls.setVisible)
        self.robot_gif_enabled = QCheckBox(dt("gif_enabled", "ru"))
        self.robot_gif_enabled.setChecked(bool(self.config.get("ui.robot_gif_enabled", False)))
        self._robot_gif_states = deepcopy(self.config.get("ui.robot_gif_states", {}) or {})
        self._robot_gif_scale = int(self.config.get("ui.robot_gif_scale_percent", 100))
        form.addWidget(self.robot_gif_enabled)
        self.gif_settings_button = QPushButton(dt("gif", "ru"))
        self.gif_settings_button.setVisible(self.robot_gif_enabled.isChecked())
        self.robot_gif_enabled.toggled.connect(self.gif_settings_button.setVisible)
        self.gif_settings_button.clicked.connect(self._open_gif_settings)
        form.addWidget(self.gif_settings_button)
        root.addWidget(group)

    def _open_gif_settings(self):
        from jarvis.ui.gif_settings import GifSettingsDialog
        dialog = GifSettingsDialog(self.config.paths.root, self._robot_gif_states, self._robot_gif_scale, language_code(self.config), self)
        try:
            if dialog.exec() == QDialog.Accepted:
                self._robot_gif_states = dialog.mapping
                self._robot_gif_scale = dialog.scale.value()
        finally:
            dialog.deleteLater()

    def _append_desktop_service_settings(self, root: QVBoxLayout) -> None:
        group = QGroupBox("NEO")
        form = QFormLayout(group)
        self.desktop_service_toggles = {}
        for key, label in (("smart_downloads_enabled", "smart_downloads"), ("vpn_notifications_enabled", "vpn"), ("latency_notifications_enabled", "latency")):
            checkbox = QCheckBox(dt(label, "ru"))
            checkbox.setChecked(bool(self.config.get("security." + key, False)))
            self.desktop_service_toggles[key] = checkbox
            form.addRow(checkbox)
        self.latency_threshold = QSpinBox()
        self.latency_threshold.setRange(50, 5000)
        self.latency_threshold.setValue(int(self.config.get("security.latency_threshold_ms", 250)))
        self.latency_threshold.setSuffix(" ms")
        form.addRow(dt("latency", "ru"), self.latency_threshold)
        self.download_directories = QPlainTextEdit("\n".join(self.config.get("downloads.directories", []) or []))
        self.download_directories.setMaximumHeight(75)
        form.addRow(dt("download_dirs", "ru"), self.download_directories)
        self.activity_enabled = QCheckBox(dt("activity", "ru"))
        self.activity_enabled.setChecked(bool(self.config.get("activity.enabled", True)))
        form.addRow(self.activity_enabled)
        self.activity_retention = QSpinBox()
        self.activity_retention.setRange(1, 365)
        self.activity_retention.setValue(int(self.config.get("activity.retention_days", 30)))
        form.addRow("Хранить историю (дней)", self.activity_retention)
        self.activity_excluded_apps = QPlainTextEdit("\n".join(self.config.get("activity.excluded_apps", []) or []))
        self.activity_excluded_paths = QPlainTextEdit("\n".join(self.config.get("activity.excluded_paths", []) or []))
        for label, field in (("excluded_apps", self.activity_excluded_apps), ("excluded_paths", self.activity_excluded_paths)):
            field.setMaximumHeight(65)
            form.addRow(dt(label, "ru"), field)
        self.file_index_enabled = QCheckBox(dt("index", "ru"))
        self.file_index_enabled.setChecked(bool(self.config.get("file_index.enabled", True)))
        form.addRow(self.file_index_enabled)
        self.vision_enabled = QCheckBox(dt("vision", "ru"))
        self.vision_enabled.setChecked(bool(self.config.get("ollama.vision_enabled", True)))
        form.addRow(self.vision_enabled)
        root.addWidget(group)

    def _append_robot_settings(self, root: QVBoxLayout) -> None:
        self._append_desktop_robot_settings(root)
        behavior = QGroupBox("Отображение робота")
        behavior_form = QFormLayout(behavior)
        self.show_robot = QCheckBox("Отображать анимированного робота в главном окне")
        self.show_robot.setChecked(bool(self.config.get("ui.show_robot", True)))
        self.idle_animations = QCheckBox("Разрешить роботу развлекать себя в ожидании")
        self.idle_animations.setChecked(bool(self.config.get("ui.idle_animations", True)))
        self.sleep_after = self._spin(20, 600, int(self.config.get("ui.sleep_after_seconds", 20)), " сек", 5)
        behavior_form.addRow(self.show_robot)
        behavior_form.addRow(self.idle_animations)
        self._add_field(behavior_form, "Засыпать через", self.sleep_after)
        root.addWidget(behavior)

        preset_group = QGroupBox("Пресет робота")
        preset_form = QFormLayout(preset_group)
        self.robot_theme_preset = CompactComboBox()
        self.robot_theme_preset.addItem("Пользовательский", "Custom")
        for name in self._preset_names():
            self.robot_theme_preset.addItem(name, name)
        configured_robot_preset = str(self.config.get("ui.robot_theme_preset", "NEO Cyan") or "NEO Cyan")
        robot_preset_index = self.robot_theme_preset.findData(configured_robot_preset)
        self.robot_theme_preset.setCurrentIndex(max(0, robot_preset_index))
        self._add_field(
            preset_form,
            "Цветовая схема",
            self.robot_theme_preset,
            "Семейство NEO включает Cyan, Orange, Blue, Purple, Yellow, Green, Red, Gray, Dark, Full Dark и White. Можно выбрать одинаковый пресет для окна и робота или смешать разные; изменения вступают в силу после «Применить» или «Сохранить».",
        )
        root.addWidget(preset_group)

        colors = QGroupBox("Цвета робота")
        form = QFormLayout(colors)
        self.robot_colors: dict[str, ColorField] = {}
        specs = [
            ("robot_body_color", "Корпус", "#313C4F"),
            ("robot_shell_color", "Светлая оболочка головы", "#DDE7F4"),
            ("robot_screen_color", "Экран лица", "#101721"),
            ("robot_eye_color", "Глаза и эмоции", "#57D7FF"),
            ("robot_trim_color", "Контуры и детали", "#718198"),
            ("robot_glow_color", "Свечение вокруг робота", "#57D7FF"),
        ]
        for key, title, default in specs:
            field = ColorField(str(self.config.get(f"ui.{key}", default)))
            field.edit.textChanged.connect(lambda _text: self._mark_robot_custom())
            self.robot_colors[key] = field
            form.addRow(title, field)
        self.robot_scale = QSpinBox()
        self.robot_scale.setRange(60, 180)
        self.robot_scale.setValue(int(self.config.get("ui.robot_scale_percent", 100)))
        self.robot_scale.setSuffix("%")
        self._add_field(form, "Размер робота", self.robot_scale, "Размер меняется вместе с окном; этот параметр задаёт дополнительный множитель.")
        self.robot_active_animation = CompactComboBox()
        for label, value in (("Выключено", "off"), ("Дождь", "rain"), ("Снег", "snow"), ("Звездопад", "starfall"), ("Облака", "cloudy"), ("Звёздное небо", "starry_sky"), ("Светлячки", "fireflies")):
            self.robot_active_animation.addItem(label, value)
        anim_idx = self.robot_active_animation.findData(str(self.config.get("ui.robot_active_animation", "off") or "off"))
        self.robot_active_animation.setCurrentIndex(max(0, anim_idx))
        self._add_field(form, "Активная анимация", self.robot_active_animation, "Постоянный атмосферный эффект на сцене робота: дождь, снег, звездопад, облака, звёздное небо или светлячки.")
        # NEO 188 HREQ-006: how many clouds drift over the stage.
        self.robot_cloud_density188 = CompactComboBox()
        for label, value in (("Мало", "few"), ("Средне", "normal"), ("Много", "many")):
            self.robot_cloud_density188.addItem(label, value)
        self.robot_cloud_density188.setCurrentIndex(max(0, self.robot_cloud_density188.findData(str(self.config.get("ui.robot_cloud_density", "normal") or "normal"))))
        self._add_field(form, "Кучность облаков", self.robot_cloud_density188, "Сколько облаков плывёт по сцене, когда выбран эффект «Облака».")
        self.robot_cloud_density188.setEnabled(self.robot_active_animation.currentData() == "cloudy")
        self.robot_active_animation.currentIndexChanged.connect(
            lambda _i: self.robot_cloud_density188.setEnabled(self.robot_active_animation.currentData() == "cloudy"))
        root.addWidget(colors)

        accessories = QGroupBox("Постоянные атрибуты робота")
        accessories_form = QFormLayout(accessories)
        self.robot_accessory_coffee = QCheckBox("Чашка кофе — держит в руке и иногда отпивает")
        self.robot_accessory_coffee.setChecked(bool(self.config.get("ui.robot_accessory_coffee", False)))
        # NEO 177: a cigarette in the other hand: smoke, a drag now and then, ash.
        self.robot_accessory_cigarette = QCheckBox("Сигарета — постоянно курит")
        self.robot_accessory_cigarette.setChecked(bool(self.config.get("ui.robot_accessory_cigarette", False)))
        accessories_row177 = QWidget()
        accessories_row177_layout = QHBoxLayout(accessories_row177)
        accessories_row177_layout.setContentsMargins(0, 0, 0, 0)
        accessories_row177_layout.setSpacing(18)
        accessories_row177_layout.addWidget(self.robot_accessory_coffee)
        accessories_row177_layout.addWidget(self.robot_accessory_cigarette)
        accessories_row177_layout.addStretch(1)
        accessories_form.addRow(accessories_row177)
        self.robot_accessory_colors: dict[str, ColorField] = {}
        accessory_specs = [
            ("robot_accessory_mug_color", "Цвет кружки", str(self.config.get("ui.robot_shell_color", "#DDE7F4") or "#DDE7F4")),
            ("robot_accessory_hand_color", "Цвет рук", str(self.config.get("ui.robot_body_color", "#313C4F") or "#313C4F")),
        ]
        for key, title, fallback in accessory_specs:
            field = ColorField(str(self.config.get(f"ui.{key}", fallback) or fallback))
            self.robot_accessory_colors[key] = field
            accessories_form.addRow(title, field)
        accessories_form.addRow(self._help("Кружка и руки по умолчанию подхватывают палитру выбранного робота. После выбора пресета каждый из этих цветов можно изменить отдельно. Кофе и сигарету можно включить вместе: кружка справа, сигарета слева."))
        root.addWidget(accessories)

        environment = QGroupBox("Цвета атмосферных анимаций")
        environment_form = QFormLayout(environment)
        self.environment_colors: dict[str, ColorField] = {}
        environment_specs = [
            ("robot_moon_color", "Луна", "#DDE7F4"),
            ("robot_cloud_color", "Облака", "#DCE7F3"),
            ("robot_rain_color", "Дождь", "#91CFFF"),
            ("robot_starfall_color", "Звездопад / звёздное небо", "#EAF6FF"),
            ("robot_snow_color", "Снег", "#F6FAFF"),
        ]
        for key, title, default in environment_specs:
            field = ColorField(str(self.config.get(f"ui.{key}", default) or default))
            self.environment_colors[key] = field
            environment_form.addRow(title, field)
        root.addWidget(environment)

        timer_hud = QGroupBox("Таймеры и напоминания на сцене робота")
        self.timer_widgets_group = timer_hud
        timer_form = QFormLayout(timer_hud)
        self.timer_overlay_enabled = QCheckBox("Показывать активные таймеры и напоминания")
        self.timer_overlay_enabled.setChecked(bool(self.config.get("ui.timer_overlay_enabled", True)))
        self.timer_overlay_position = QComboBox()
        active_lang = language_code(self.config)
        for label_text, position_key in (
            ("Слева вверху", "top_left"), ("По центру вверху", "top_center"), ("Справа вверху", "top_right"),
            ("Слева по центру", "center_left"), ("По центру", "center"), ("Справа по центру", "center_right"),
            ("Слева внизу", "bottom_left"), ("По центру внизу", "bottom_center"), ("Справа внизу", "bottom_right"),
        ):
            self.timer_overlay_position.addItem(label_text, position_key)
        pos_idx=self.timer_overlay_position.findData(str(self.config.get("ui.timer_overlay_position", "bottom_left") or "bottom_left")); self.timer_overlay_position.setCurrentIndex(max(0,pos_idx))
        self.timer_overlay_font_size = QSpinBox(); self.timer_overlay_font_size.setRange(8, 48); self.timer_overlay_font_size.setValue(int(self.config.get("ui.timer_overlay_font_size", 12) or 12)); self.timer_overlay_font_size.setSuffix(" px")
        self.timer_overlay_opacity = QSpinBox(); self.timer_overlay_opacity.setRange(10, 100); self.timer_overlay_opacity.setValue(int(self.config.get("ui.timer_overlay_opacity_percent", 88) or 88)); self.timer_overlay_opacity.setSuffix(" %")
        self.timer_overlay_style = QComboBox()
        self.timer_overlay_style.addItem("Стекло", "glass"); self.timer_overlay_style.addItem("Минимализм", "minimal"); self.timer_overlay_style.addItem("Плашка", "pill")
        style_idx=self.timer_overlay_style.findData(str(self.config.get("ui.timer_overlay_style", "glass") or "glass")); self.timer_overlay_style.setCurrentIndex(max(0, style_idx))
        self.timer_overlay_text_color = ColorField(str(self.config.get("ui.timer_overlay_text_color", "#EAF2FF") or "#EAF2FF"))
        self.timer_overlay_background_color = ColorField(str(self.config.get("ui.timer_overlay_background_color", "#0A0F18") or "#0A0F18"))
        self.timer_overlay_border_color = ColorField(str(self.config.get("ui.timer_overlay_border_color", "#57D7FF") or "#57D7FF"))
        timer_form.addRow(self.timer_overlay_enabled)
        self._add_field(timer_form, "Положение", self.timer_overlay_position, "Таймеры рисуются внутри области робота и не создают отдельного окна.")
        self._add_field(timer_form, "Размер текста", self.timer_overlay_font_size, "Используется то же семейство шрифта, что и у основного интерфейса.")
        self._add_field(timer_form, "Прозрачность", self.timer_overlay_opacity, "Прозрачность фона окна таймера.")
        self._add_field(timer_form, "Стиль", self.timer_overlay_style, "Стекло, минимальный вид или компактная плашка.")
        self._add_field(timer_form, "Цвет текста", self.timer_overlay_text_color, "Цвет текста активного таймера.")
        self._add_field(timer_form, "Цвет фона", self.timer_overlay_background_color, "Цвет фона активного таймера.")
        self._add_field(timer_form, "Цвет рамки", self.timer_overlay_border_color, "Цвет рамки активного таймера.")
        root.addWidget(timer_hud)

        editor = QGroupBox("Редактор компоновки главного окна")
        editor_form = QFormLayout(editor)
        self.robot_layout_edit_enabled = QCheckBox("Разрешить перетаскивание и изменение размеров информационных элементов")
        self.robot_layout_edit_enabled.setChecked(bool(self.config.get("ui.robot_layout_edit_enabled", False)))
        self.robot_layout_edit_enabled.setToolTip("После «Применить» элементы времени, трека и температуры можно перетаскивать мышью прямо на сцене робота. Потяните за квадрат в правом нижнем углу элемента, чтобы изменить его ширину и высоту. После отключения режима позиции фиксируются.")
        self.chat_height_px = QSpinBox(); self.chat_height_px.setRange(0, 1600); self.chat_height_px.setSingleStep(10); self.chat_height_px.setSuffix(" px"); self.chat_height_px.setSpecialValueText("Авто")
        self.chat_height_px.setValue(max(0, int(self.config.get("ui.chat_height_px", 0) or 0)))
        self.reset_robot_layout_button = QPushButton("Сбросить расположение информации")
        self._robot_info_layout_pending = deepcopy(self.config.get("ui.robot_info_layout", {}) or {})
        # Runtime dragging is persisted immediately by MainWindow.  This flag is
        # intentionally False until the user presses the explicit reset button;
        # otherwise a still-open Settings dialog would overwrite freshly dragged
        # positions with the stale snapshot it had when the dialog was opened.
        self._robot_info_layout_reset_pending = False
        self.reset_robot_layout_button.clicked.connect(self._reset_robot_info_layout_pending)
        editor_form.addRow(self.robot_layout_edit_enabled)
        self._add_field(editor_form, "Высота чата", self.chat_height_px, "0 = автоматическая. В режиме редактирования между сценой робота и чатом появляется тонкая ручка: её можно тянуть вверх/вниз, и робот автоматически уменьшится или вернётся к нормальному размеру в оставшемся пространстве.")
        editor_form.addRow(self.reset_robot_layout_button)
        root.addWidget(editor)

        info_hud = QGroupBox("Информация на сцене робота")
        info_form = QFormLayout(info_hud)
        active_lang = language_code(self.config)
        position_items = (
            ("Слева сверху", "top_left"), ("Сверху по центру", "top_center"), ("Справа сверху", "top_right"),
            ("Слева по центру", "left_center"), ("Справа по центру", "right_center"),
            ("Слева внизу", "bottom_left"), ("Снизу по центру", "bottom_center"), ("Справа внизу", "bottom_right"),
        )
        def make_position(key: str, default: str) -> QComboBox:
            combo = QComboBox()
            for label, value in position_items:
                combo.addItem(label, value)
            idx = combo.findData(str(self.config.get(f"ui.{key}", default) or default))
            combo.setCurrentIndex(max(0, idx))
            return combo

        self.robot_info_clock_enabled = QCheckBox("Показывать текущее время")
        self.robot_info_clock_enabled.setChecked(bool(self.config.get("ui.robot_info_clock_enabled", False)))
        self.robot_info_clock_position = make_position("robot_info_clock_position", "top_center")
        info_form.addRow(self.robot_info_clock_enabled)

        self.robot_info_track_enabled = QCheckBox("Показывать текущую песню и исполнителя")
        self.robot_info_track_enabled.setChecked(bool(self.config.get("ui.robot_info_track_enabled", False)))
        self.robot_info_track_position = make_position("robot_info_track_position", "bottom_center")
        info_form.addRow(self.robot_info_track_enabled)

        info_form.addRow(self._help("Музыкальная волна больше не настраивается отдельно: когда NEO сам воспроизводит музыку на компьютере или в Discord, компактный эквалайзер автоматически появляется прямо на корпусе робота и исчезает после остановки/паузы."))

        self.robot_info_temperature_enabled = QCheckBox("Показывать температуру города по умолчанию")
        self.robot_info_temperature_enabled.setChecked(bool(self.config.get("ui.robot_info_temperature_enabled", False)))
        self.robot_info_temperature_position = make_position("robot_info_temperature_position", "top_right")
        info_form.addRow(self.robot_info_temperature_enabled)
        self.robot_info_temperature_show_city = QCheckBox("Показывать название города рядом с погодой")
        self.robot_info_temperature_show_city.setChecked(bool(self.config.get("ui.robot_info_temperature_show_city", True)))
        info_form.addRow(self.robot_info_temperature_show_city)

        self.robot_info_font_size = QSpinBox(); self.robot_info_font_size.setRange(8, 28)
        self.robot_info_font_size.setValue(int(self.config.get("ui.robot_info_font_size", 12) or 12)); self.robot_info_font_size.setSuffix(" px")
        self._add_field(info_form, "Размер текста", self.robot_info_font_size)
        root.addWidget(info_hud)
        root.addWidget(self._help("Положение и размер каждого включённого элемента задаются мышью в режиме редактирования. В обычном режиме информационный слой полностью без рамок и не перехватывает мышь."))

        self.robot_theme_preset.currentIndexChanged.connect(
            lambda _index: self._apply_robot_preset(str(self.robot_theme_preset.currentData() or ""))
        )
        root.addWidget(self._help("Если снять «Отображать робота», вся верхняя анимированная область исчезнет, а чат автоматически займёт освободившееся место."))

    def _reset_robot_info_layout_pending(self) -> None:
        self._robot_info_layout_reset_pending = True
        self._robot_info_layout_pending = {
            "temperature": {"x": 0.69, "y": 0.02, "w": 0.28, "h": 0.09},
            "track": {"x": 0.18, "y": 0.82, "w": 0.64, "h": 0.10},
            "clock": {"x": 0.82, "y": 0.82, "w": 0.15, "h": 0.10},
        }
        self.reset_robot_layout_button.setText(self._t("Расположение сброшено — нажмите «Применить»"))

    def _security_tab(self) -> QWidget:
        content = QWidget()
        root = QVBoxLayout(content)
        self._append_desktop_service_settings(root)
        group = QGroupBox("Подтверждения и доступ к файлам")
        form = QFormLayout(group)
        self.confirm_medium = QCheckBox("Подтверждать обычные изменения файлов (не рекомендуется для голосового режима)")
        self.confirm_medium.setChecked(bool(self.config.get("security.confirm_medium_risk", False)))
        self.auto_file_edits = QCheckBox("Создание и редактирование текстовых файлов выполнять сразу")
        self.auto_file_edits.setChecked(bool(self.config.get("security.auto_file_edits_without_confirmation", True)))
        self.full_user_file_access = QCheckBox("Разрешить работу со всеми обычными папками на дисках (системные каталоги защищены)")
        self.full_user_file_access.setChecked(bool(self.config.get("security.full_user_file_access", True)))
        # 186: the two flags are independent and full access has priority; the
        # caption and its live hint say so instead of promising a replacement.
        self.restrict_paths = QCheckBox("Ограничить изменения файлов списком разрешённых папок (действует, когда полный доступ выключен)")
        self.restrict_paths.setChecked(bool(self.config.get("security.restrict_file_mutations", True)))

        def _path_mode_hint186(*_args) -> None:
            text = ("Сейчас не действует: включён доступ ко всем обычным папкам, он имеет приоритет."
                    if self.full_user_file_access.isChecked() else
                    "Изменять файлы можно только в папках из списка ниже.")
            self.restrict_paths.setProperty("i18n_tooltip_source", text)
            translate = getattr(self, "_t", None)
            self.restrict_paths.setToolTip(translate(text) if callable(translate) else text)
        self.full_user_file_access.toggled.connect(_path_mode_hint186)
        _path_mode_hint186()
        self.allow_powershell = QCheckBox("Разрешить произвольные команды PowerShell")
        self.allow_powershell.setChecked(bool(self.config.get("security.allow_arbitrary_powershell", False)))
        self.dangerous_preview = QCheckBox("Показывать предварительное подтверждение опасных действий (выключение, безвозвратное удаление, массовые операции)")
        self.dangerous_preview.setChecked(bool(self.config.get("safety.dangerous_preview", True)))
        self.allowed_roots = QPlainTextEdit()
        self.allowed_roots.setPlainText("\n".join(self.config.get("security.allowed_roots", [])))
        self.allowed_roots.setMinimumHeight(160)
        form.addRow(self.confirm_medium)
        form.addRow(self.auto_file_edits)
        form.addRow(self.full_user_file_access)
        form.addRow(self.restrict_paths)
        form.addRow(self.allow_powershell)
        form.addRow(self.dangerous_preview)
        self._add_field(form, "Разрешённые папки", self.allowed_roots, "По одному полному пути на строку. При полном пользовательском доступе этот список не ограничивает обычные папки; Windows/Program Files/ProgramData всё равно защищены.")
        root.addWidget(group)
        root.addWidget(self._help("По умолчанию создание, редактирование, перенос, переименование и удаление В КОРЗИНУ выполняются без подтверждения. Переключатели выше могут включить подтверждение этих действий, а массовые операции — показывать предпросмотр. Безвозвратное удаление, опасные действия и защищённые системные каталоги всегда требуют отдельного подтверждения или остаются заблокированы."))

        bt_group = QGroupBox("Bluetooth-уведомления")
        bt_form = QFormLayout(bt_group)
        self.bluetooth_connect_notifications = QCheckBox("Сообщать в чате о подключении Bluetooth-устройства")
        self.bluetooth_connect_notifications.setChecked(bool(self.config.get("security.bluetooth_connect_notifications", True)))
        self.bluetooth_battery_notifications = QCheckBox("Предупреждать о низком заряде Bluetooth-устройств")
        self.bluetooth_battery_notifications.setChecked(bool(self.config.get("security.bluetooth_battery_notifications", True)))
        self.bluetooth_battery_threshold = QSpinBox(); self.bluetooth_battery_threshold.setRange(5, 50); self.bluetooth_battery_threshold.setSuffix(" %")
        self.bluetooth_battery_threshold.setValue(int(self.config.get("security.bluetooth_battery_threshold_percent", 20) or 20))
        bt_form.addRow(self.bluetooth_connect_notifications)
        bt_form.addRow(self.bluetooth_battery_notifications)
        self._add_field(bt_form, "Порог низкого заряда", self.bluetooth_battery_threshold)
        root.addWidget(bt_group)

        # Computer unlock is security-sensitive configuration.
        self._append_voice_unlock_section(root)
        root.addStretch(1)
        return self._scroll(content)

    def _append_voice_unlock_section(self, root: QVBoxLayout) -> None:
        group = QGroupBox("Голосовая разблокировка Windows")
        form = QFormLayout(group)
        self.voice_unlock_enabled = QCheckBox("Разрешить разблокировку компьютера кодовой фразой без wake word")
        self.voice_unlock_enabled.setChecked(bool(self.config.get("voice_unlock.enabled", False)))

        configured_account = str(self.config.get("voice_unlock.account", "") or "").strip()
        self.voice_unlock_account = QLineEdit(configured_account or current_windows_account())
        self.voice_unlock_account.setPlaceholderText(r"COMPUTER\User или MicrosoftAccount\email@example.com")

        self._existing_voice_unlock_password_encrypted = str(self.config.get("voice_unlock.password_encrypted", "") or "")
        self.voice_unlock_password = QLineEdit()
        self.voice_unlock_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.voice_unlock_password.setPlaceholderText(
            "Пароль уже сохранён — оставьте поле пустым, чтобы не менять"
            if self._existing_voice_unlock_password_encrypted else "Пароль учётной записи Windows"
        )

        password_row = QWidget()
        password_layout = QHBoxLayout(password_row)
        password_layout.setContentsMargins(0, 0, 0, 0)
        password_layout.setSpacing(6)
        password_layout.addWidget(self.voice_unlock_password, 1)
        self.voice_unlock_test = QPushButton("Проверить")
        self.voice_unlock_test.clicked.connect(self._test_voice_unlock_password)
        password_layout.addWidget(self.voice_unlock_test)

        self.voice_unlock_phrase = QLineEdit(str(self.config.get("voice_unlock.phrase", "") or ""))
        self.voice_unlock_phrase.setPlaceholderText("Например: красный")
        self.voice_unlock_aliases = QPlainTextEdit()
        self.voice_unlock_aliases.setPlainText("\n".join(str(x) for x in (self.config.get("voice_unlock.aliases", []) or []) if str(x).strip()))
        self.voice_unlock_aliases.setMinimumHeight(125)
        self.voice_unlock_aliases.setPlaceholderText("По одному ASR-варианту на строку")
        self.voice_unlock_status = QLabel(
            "Пароль хранится только в DPAPI-зашифрованном виде. Кодовая фраза срабатывает без wake word только на заблокированной Windows-сессии. Для Secure Desktop используется системный Credential Provider."
        )
        self.voice_unlock_status.setWordWrap(True)

        form.addRow(self.voice_unlock_enabled)
        self._add_field(form, "Учётная запись Windows", self.voice_unlock_account, "Обычно определяется автоматически. Для Microsoft Account можно указать MicrosoftAccount\\email@example.com.")
        self._add_field(form, "Пароль Windows", password_row, "Нужен для проверки владельца сессии. В settings.json сохраняется только Windows DPAPI-шифротекст.")
        self._add_field(form, "Кодовая фраза", self.voice_unlock_phrase, "Фраза распознаётся напрямую на постоянном аудиопотоке, без wake word.")
        self._add_field(form, "Варианты кодовой фразы", self.voice_unlock_aliases, "Добавьте сюда реальные варианты, которыми Whisper может услышать кодовую фразу. Сопоставление точное после нормализации, без fuzzy-разблокировки.")
        root.addWidget(group)
        root.addWidget(self.voice_unlock_status)
        root.addWidget(self._help(
            "При первом включении/обновлении Windows может один раз показать UAC для регистрации системного модуля. После этого кодовая фраза создаёт короткоживущий защищённый запрос, который LogonUI передаёт Winlogon/LSA. Сам пароль в открытом виде на диск не записывается. NEO всё равно должен быть запущен и слышать микрофон в уже созданной пользовательской сессии."
        ))

    def _test_voice_unlock_password(self) -> None:
        account = self.voice_unlock_account.text().strip() or current_windows_account()
        password = self.voice_unlock_password.text()
        if not password:
            existing = unprotect_secret(self._existing_voice_unlock_password_encrypted)
            password = existing
        if not password:
            QMessageBox.warning(self, "Разблокировка Windows", "Введите пароль Windows, чтобы выполнить проверку.")
            return
        result = validate_windows_password(account, password)
        if result.ok:
            QMessageBox.information(self, "Разблокировка Windows", result.status)
        else:
            QMessageBox.warning(self, "Разблокировка Windows", result.status)

    def _reset_all_settings(self) -> None:
        if not self._settings_ready160 or self._settings_closed160:
            return
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Question)
        dialog.setWindowTitle(self._t("Сброс настроек"))
        # NEO 184: two explicit kinds of reset; «всё» really removes the connections.
        dialog.setText(self._rt("Сбросить настройки NEO к значениям по умолчанию?"))
        dialog.setInformativeText(self._rt(
            "«Сбросить всё» также удалит подключения: токены Spotify, Gmail, Google Календаря, Яндекс Музыки, "
            "Telegram и Discord, ключи API и Client ID. «Сохранить подключения» вернёт по умолчанию всё остальное."))
        all_button = dialog.addButton(self._t("Сбросить всё"), QMessageBox.ButtonRole.YesRole)
        keep_button = dialog.addButton(self._t("Сохранить подключения"), QMessageBox.ButtonRole.AcceptRole)
        no_button = dialog.addButton(self._t("Отмена"), QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(no_button)
        dialog.setEscapeButton(no_button)
        try:
            dialog.exec()
            clicked = dialog.clickedButton()
            confirmed = clicked in (all_button, keep_button)
            keep = clicked is keep_button
        finally:
            dialog.deleteLater()
        if not confirmed:
            return
        try:
            self.config.reset_to_defaults(keep_connections=keep)
            left = [] if keep else self.config.stored_credentials()
            if left:
                QMessageBox.warning(self, self._t("Сброс настроек"),
                                    self._t("Не все подключения удалены: {items}").format(items=", ".join(left)))
            else:
                QMessageBox.information(self, self._t("Настройки"), self._t(
                    "Настройки восстановлены по умолчанию, подключения сохранены." if keep
                    else "Все настройки восстановлены по умолчанию, подключения удалены."))
            self.settings_saved.emit()
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, self._t("Сброс настроек"), safe_error(exc))

    def _refresh_microphones_async(self) -> None:
        if self._settings_closed160:
            return
        if getattr(self, "_microphone_worker", None) is not None:
            return
        current = self.input_device.currentData() if hasattr(self, "input_device") else ""
        revision = int(getattr(self, "_mic_draft_rev188", 0) or 0)
        worker = FunctionWorker(AudioCaptureController.available_input_devices)
        self._microphone_worker = worker
        worker.signals.result.connect(lambda rows, selected=current, rev=revision: self._apply_microphone_rows(rows, selected, revision=rev))
        worker.signals.error.connect(self._on_microphone_rows_error)
        worker.signals.finished.connect(self._microphone_rows_finished)
        QThreadPool.globalInstance().start(worker)

    def _apply_microphone_rows(self, rows: object, selected: object = "", *, revision: int | None = None) -> None:
        if self._settings_closed160:
            return
        if not hasattr(self, "input_device"):
            return
        if revision is not None and revision != int(getattr(self, "_mic_draft_rev188", 0) or 0):
            selected = self.input_device.currentData() or ""     # the user picked again meanwhile: keep that
        current = str(selected or self.input_device.currentData() or "")
        self.input_device.clear()
        # NEO 183: service captions in the interface language; device names stay as Windows reports them
        self.input_device.addItem(self._t("Системный микрофон по умолчанию"), "")
        self.input_device.setItemData(self.input_device.count() - 1, "Системный микрофон по умолчанию", int(Qt.UserRole) + 77)
        try:
            values = list(rows or [])
        except Exception:
            values = []
        for row in values:
            try:
                token, label = row
                self.input_device.addItem(str(label), str(token))
                self.input_device.setItemData(self.input_device.count() - 1, True, int(Qt.UserRole) + 78)   # literal device name
            except Exception:
                continue
        idx = self.input_device.findData(current)
        if idx < 0 and current:
            for candidate in range(self.input_device.count()):
                if current.casefold() in self.input_device.itemText(candidate).casefold():
                    idx = candidate
                    break
        if idx < 0 and current:
            self._add_current_device186(current)
            idx = self.input_device.count() - 1
        self.input_device.setCurrentIndex(max(0, idx))

    def _add_current_device186(self, device: str) -> None:
        """«Текущий микрофон: {device}» — a translated template around the literal device name."""
        self.input_device.addItem(self._t("Текущий микрофон: {device}").replace("{device}", str(device)), str(device))
        index = self.input_device.count() - 1
        self.input_device.setItemData(index, "Текущий микрофон: {device}", int(Qt.UserRole) + 77)
        self.input_device.setItemData(index, {"device": str(device)}, int(Qt.UserRole) + 79)

    def _on_microphone_rows_error(self, trace: str) -> None:
        if not hasattr(self, "input_device"):
            return
        last = str(trace or "").strip().splitlines()
        message = last[-1] if last else "неизвестная ошибка"
        self.input_device.setProperty("i18n_tooltip_source", "Не удалось обновить список микрофонов: {error}")
        self.input_device.setProperty("i18n_tooltip_values", {"error": message})
        self.input_device.setToolTip(self._t("Не удалось обновить список микрофонов: {error}").format(error=message))

    def _microphone_rows_finished(self) -> None:
        self._microphone_worker = None

    def _populate_microphones(self) -> None:
        """Compatibility alias: microphone enumeration is asynchronous."""
        self._refresh_microphones_async()

    def _append_conversation_model184(self, form: QFormLayout) -> None:
        self.chat_model = QComboBox()
        self.chat_model.setEditable(True)
        self.chat_model.setProperty("i18n_skip_combo", True)
        selected = str(self.config.get("local_chat.model", "") or self.config.get("ollama.chat_model", "qwen2.5:0.5b") or "qwen2.5:0.5b")
        self.chat_model.addItem(selected)
        if selected != "qwen2.5:0.5b":
            self.chat_model.addItem("qwen2.5:0.5b")
        # USER185-12: the whole model name is always reachable (tooltip) and the field takes
        # the free width of its row (see responsive178.FlowLayout.mark_growing).
        self.chat_model.setToolTip(selected)
        self.chat_model.currentTextChanged.connect(self.chat_model.setToolTip)
        self.chat_model.setMinimumContentsLength(18)
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.chat_model, 1)
        self.chat_model_delete184 = QPushButton("Удалить")
        self.chat_model_download184 = QPushButton("Скачать")
        self.chat_model_check184 = QPushButton("Проверить")
        self.chat_model_delete184.clicked.connect(lambda: self._start_model_operation184("delete"))
        self.chat_model_download184.clicked.connect(lambda: self._start_model_operation184("download"))
        self.chat_model_check184.clicked.connect(lambda: self._start_model_operation184("check"))
        for button in (self.chat_model_delete184, self.chat_model_download184, self.chat_model_check184):
            layout.addWidget(button)
        self._add_field(form, "Разговорная модель", row,
                        "Для нового профиля: Qwen2.5 0.5B (около 398 МБ). Уже выбранная модель сохраняется. Скачивание начинается только по кнопке «Скачать».")
        append_quality_choices(self, form)
        self.ollama_auto_start184 = QCheckBox("Автоматически запускать Ollama вместе с NEO")
        self.ollama_auto_start184.setChecked(bool(self.config.get("ollama.auto_start", True)))
        form.addRow(self.ollama_auto_start184)
        form.addRow(self._help("NEO.cmd всегда устанавливает отсутствующую Ollama в фоне. Этот переключатель управляет только запуском службы; модели скачиваются отдельно по кнопке «Скачать»."))
        self.chat_model_status184 = QLabel("Модель ещё не проверена")
        self.chat_model_status184.setWordWrap(True)
        self.chat_model_progress184 = QProgressBar()
        self.chat_model_progress184.setRange(0, 100)
        self.chat_model_progress184.hide()
        service_row = QWidget()
        service_layout = QHBoxLayout(service_row)
        service_layout.setContentsMargins(0, 0, 0, 0)
        self.chat_model_start184 = QPushButton("Запустить Ollama")
        self.chat_model_start184.clicked.connect(lambda: self._start_model_operation184("start"))
        self.chat_model_cancel184 = QPushButton("Отмена")
        self.chat_model_cancel184.setEnabled(False)
        self.chat_model_cancel184.clicked.connect(self._cancel_model_operation184)
        service_layout.addWidget(self.chat_model_start184)
        service_layout.addWidget(self.chat_model_cancel184)
        service_layout.addStretch(1)
        form.addRow(self.chat_model_status184)
        form.addRow(self.chat_model_progress184)
        form.addRow(service_row)
        form.addRow(self._help("Если Ollama не установлена: https://ollama.com/download. Кнопка запускает только уже установленную службу."))

    def focus_conversation_model184(self, *, download: bool = False) -> None:
        """Queue the explicit first-run choice until the normal settings UI exists."""
        if self._settings_closed160 or self._settings_build_error160:
            return
        self._model_setup_request184 = bool(download)
        self.open_page("conversation")
        if self._settings_ready160:
            self._model_setup_timer184.start(0)

    def _run_model_setup_request184(self) -> None:
        if self._settings_closed160 or not self._settings_ready160 or self._model_setup_request184 is None:
            return
        download = self._model_setup_request184
        self._model_setup_request184 = None
        self.tabs._search.clear()
        self.open_page("conversation")
        self.tabs._show_hit(self.chat_model)
        self.chat_model.setFocus()
        if download:
            from jarvis.ollama_models import DEFAULT_CHAT_MODEL
            # A concurrent explicit model edit wins over stale onboarding intent.
            if self.chat_model.currentText().strip() == DEFAULT_CHAT_MODEL:
                self._start_model_operation184("download")
            else:
                self._model_error184(self._t("Выбранная модель изменилась. Проверьте выбор и нажмите «Скачать» рядом с моделью."))

    def _model_controls_busy184(self, busy: bool) -> None:
        for name in ("chat_model", "chat_model_delete184", "chat_model_download184", "chat_model_check184",
                     "chat_model_start184", "model_list_button184", "model_server_check_button184", "ollama_url", "vision_model"):
            control = getattr(self, name, None)
            if control is not None:
                control.setEnabled(not busy)
        self.chat_model_cancel184.setEnabled(busy)
        self.chat_model_progress184.setVisible(busy)

    def _start_model_operation184(self, action: str) -> None:
        if self._settings_closed160 or self._model_operation184 is not None:
            return
        from jarvis.model_management import ModelOperation, local_endpoint
        model = self.chat_model.currentText().strip()
        url_field = getattr(self, "ollama_url", None)
        url = url_field.text().strip() if url_field is not None else str(self.config.get("ollama.base_url", "http://localhost:11434"))
        try:
            local_endpoint(url)
        except ValueError as exc:
            self._model_error184(str(exc))
            return
        if action not in {"list", "start"} and not model:
            self._model_error184(self._t("Укажите разговорную модель"))
            return
        if action == "delete":
            prompt = self._t("Удалить модель {model} с сервера {server}? Чтобы использовать её снова, потребуется повторное скачивание.").format(model=model, server=url)
            if QMessageBox.question(self, "Удалить модель", prompt, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        operation = ModelOperation(action, model, url, auto_start=bool(self.config.get("ollama.auto_start", False)) if action == "download" else False)
        self._model_operation184 = operation
        relay = _ModelOperationRelay184(operation, self.config, action=action, base_url=url)
        self._model_relay184 = relay
        self._model_operation_generation184 += 1
        self._model_controls_busy184(True)
        self.chat_model_progress184.setRange(0, 0)
        self.chat_model_status184.setText(self._t("Проверяю локальную модель…"))
        # The relay receives on the GUI thread and outlives this disposable view.
        relay.progress.connect(self._model_progress184)
        relay.completed.connect(self._model_completed184)
        relay.failed.connect(self._model_error184)
        relay.finished.connect(self._model_finished184)
        self.model_operation_started184.emit(relay)
        relay.announce("Проверяю локальную модель…")
        operation.start()

    def _model_progress184(self, message: str, percent: int) -> None:
        if self._settings_closed160:
            return
        text = _model_text184(str(message), language_code(self.config))
        text = self._rt(text)
        self.chat_model_status184.setText(text)
        if percent < 0:
            self.chat_model_progress184.setRange(0, 0)
        else:
            self.chat_model_progress184.setRange(0, 100)
            self.chat_model_progress184.setValue(max(0, min(100, percent)))
        self.model_progress.emit(text)

    def _model_completed184(self, result: dict) -> None:
        if self._settings_closed160:
            return
        models = result.get("models")
        if isinstance(models, list):
            for name in ("chat_model", "vision_model"):
                combo = getattr(self, name, None)
                if combo is None:
                    continue
                selected = combo.currentText()
                combo.clear()
                combo.addItems([str(value) for value in models])
                if name == "chat_model":
                    restore_quality_choices(combo, language_code(self.config))
                if combo.findText(selected) < 0:
                    combo.addItem(selected)
                combo.setCurrentText(selected)
        text = self._rt(_model_text184(str(result.get("message") or self._t("Операция с моделью завершена")), language_code(self.config)))
        self.chat_model_status184.setText(text)
        self.model_progress.emit(text)

    def _model_error184(self, message: str) -> None:
        if self._settings_closed160:
            return
        text = self._rt(_model_text184(str(message), language_code(self.config)))
        self.chat_model_status184.setText(text)
        self.model_progress.emit(text)
        if self._model_relay184 is None:
            self.model_notice184.emit(text)

    def _model_finished184(self) -> None:
        operation = self._model_operation184
        self._model_operation184 = None
        self._model_relay184 = None
        if operation is not None:
            try:
                operation.deleteLater()
            except RuntimeError:
                pass  # ModelOperation also self-cleans after a closed dialog.
        if not self._settings_closed160:
            self._model_controls_busy184(False)

    def _cancel_model_operation184(self, *_args) -> None:
        operation = self._model_operation184
        if operation is None:
            return
        relay = self._model_relay184
        if relay is not None:
            relay.cancel()
            if relay.terminal:
                return
        else:
            operation.cancel()
        if not self._settings_closed160:
            self.chat_model_cancel184.setEnabled(False)
            self.chat_model_status184.setText(self._t("Отменяю операцию…"))

    def _load_models(self) -> None:
        self._start_model_operation184("list")

    def _test_ollama(self) -> None:
        self._start_model_operation184("list")

    def _validated_color(self, field: ColorField, title: str) -> str:
        value = field.value()
        if not QColor(value).isValid():
            raise ValueError(f"Некорректный цвет «{title}»: {value}")
        return QColor(value).name()

    # Numeric fields whose stored value may have more precision, a different
    # type or a value outside the control's range. When the user does not touch
    # the control, Save writes back the stored value unchanged instead of the
    # rounded/clamped widget value (QA cp31: silence 1.05→1.1, wake chunk
    # 0.95→0.9, timeout 4.0→4, Telegram interval 0.15→0.25).
    _NUMERIC_FIELDS185 = (
        ("silence_seconds", "voice.silence_seconds"),
        ("live_partial", "voice.live_partial_seconds"),
        ("max_record", "voice.max_record_seconds"),
        ("start_timeout", "voice.start_timeout_seconds"),
        ("sensitivity", "voice.sensitivity"),
        ("noise_rejection", "voice.noise_rejection_percent"),
        ("listening_duck", "voice.listening_duck_percent"),
        ("wake_chunk", "wake_word.chunk_seconds"),
        ("wake_scan_interval", "wake_word.silence_seconds"),
        ("wake_match_sensitivity", "wake_word.fuzzy_threshold"),
        ("timeout", "ollama.request_timeout_seconds"),
        ("first_token_timeout", "ollama.first_token_timeout_seconds"),
        ("creativity", "ollama.temperature"),
        ("telegram_command_interval", "integrations.telegram.min_command_interval_seconds"),
        ("telegram_critical_lockout", "integrations.telegram.critical_phrase_lockout_seconds"),
        ("telegram_critical_ttl", "integrations.telegram.critical_phrase_ttl_seconds"),
        ("gmail_poll_seconds", "integrations.gmail.poll_seconds"),
        ("mailru_poll_seconds", "integrations.mailru.poll_seconds"),
        ("atlas2_threshold", "routing.semantic_threshold"),
        ("autoclicker_cps", "autoclicker.cps"),
        ("dev_slow", "developer_mode.slow_threshold_seconds"),
        ("sleep_after", "ui.sleep_after_seconds"),
    )

    # 186: switches that other surfaces (tray, voice commands) also change while
    # the modeless dialog stays open. An untouched switch never writes its stale
    # widget state over a newer value; only a switch the user changed is applied.
    _TOGGLE_FIELDS186 = (
        ("activity_enabled", "activity.enabled"),
    )

    def _snapshot_numeric_fields185(self) -> None:
        toggles: dict[str, tuple[Any, bool]] = {}
        previous = dict(getattr(self, "_toggle_snapshot186", {}) or {})
        for attr, key in self._TOGGLE_FIELDS186:
            widget = getattr(self, attr, None)
            try:
                if key in previous and previous[key][0] is widget and bool(widget.isChecked()) == previous[key][1]:
                    # Untouched since the last snapshot: show the stored value.
                    widget.setChecked(bool(self.config.get(key, widget.isChecked())))
                if widget is not None and hasattr(widget, "isChecked"):
                    toggles[key] = (widget, bool(widget.isChecked()))
            except RuntimeError:
                continue
        self._toggle_snapshot186 = toggles
        snapshot: dict[str, tuple[Any, Any, Any]] = {}
        for attr, key in self._NUMERIC_FIELDS185:
            widget = getattr(self, attr, None)
            raw = self.config.get(key, None)
            if widget is None or not hasattr(widget, "value") or isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue
            try:
                snapshot[key] = (widget, widget.value(), raw)
            except RuntimeError:
                continue
        self._numeric_snapshot185 = snapshot

    def _restore_untouched_numeric185(self, data: dict[str, Any]) -> None:
        reload = getattr(self.config, "reload_if_changed", None)
        if callable(reload):
            try:
                reload()
            except Exception:
                logging.getLogger(__name__).debug("Settings reload before Apply failed", exc_info=True)
        missing = object()

        def put(key: str, value: Any) -> None:
            node: Any = data
            parts = key.split(".")
            for part in parts[:-1]:
                node = node.get(part) if isinstance(node, dict) else None
            if isinstance(node, dict) and parts[-1] in node and value is not missing:
                node[parts[-1]] = value

        for key, (widget, initial, raw) in dict(getattr(self, "_numeric_snapshot185", {}) or {}).items():
            try:
                if widget.value() != initial:
                    continue
            except RuntimeError:
                continue
            # 186: the newest stored value, not the one seen when the dialog opened.
            current = self.config.get(key, raw)
            put(key, current if isinstance(current, (int, float)) and not isinstance(current, bool) else raw)
        for key, (widget, initial) in dict(getattr(self, "_toggle_snapshot186", {}) or {}).items():
            try:
                if bool(widget.isChecked()) != initial:
                    continue
            except RuntimeError:
                continue
            current = self.config.get(key, missing)
            put(key, bool(current) if current is not missing else missing)

    def _save(self, *, close: bool = True) -> None:
        if not self._settings_ready160 or self._settings_closed160:
            return
        # Reject the entire Apply/Save before any setting can be persisted.
        from jarvis.runtime.youtube_playlists184 import validate_playlists
        try:
            youtube_music_playlists = validate_playlists(self._youtube_music_playlist_values184())
        except ValueError as exc:
            QMessageBox.warning(self, self._t("Музыка YouTube"), self._t(str(exc)))
            return
        google_commit188 = None
        try:
            data: dict[str, Any] = deepcopy(self.config.data)
            if hasattr(self, "response_voice_page"):
                data["response_voice"] = self.response_voice_page.values()
            if hasattr(self, "streaming_page"):
                data["streaming"], streaming_password_update = self.streaming_page.prepare_save()
            selected_language = str(self.app_language.currentData() or "en").casefold()
            data.setdefault("app", {})["language"] = selected_language
            data.setdefault("dialogue", {})
            data["dialogue"]["memory"] = {"enabled": self.dialogue_memory_enabled.isChecked(), "retention_days": self.dialogue_memory_days.value(), "max_turns": self.dialogue_memory_turns.value()}
            data["assistant_name"] = self.assistant_name.text().strip() or DEFAULT_ASSISTANT_NAME
            data["user_name"] = self.user_name.text().strip()[:80] or DEFAULT_USER_NAME
            data["user_name_enabled"] = bool(self.user_name_enabled.isChecked())
            data["assistant_prompt"] = DEFAULT_ASSISTANT_PROMPT
            data.setdefault("local_chat", {})["model"] = self.chat_model.currentText().strip() or "qwen2.5:0.5b"
            data.setdefault("ollama", {}).update(
                {
                    "base_url": self.ollama_url.text().strip(),
                    "auto_start": self.ollama_auto_start184.isChecked(),
                    "auto_install": True,
                    "chat_model": self.chat_model.currentText().strip() or "qwen2.5:0.5b",
                    "vision_model": self.vision_model.currentText().strip() or "qwen3.5:4b",
                    "num_ctx": self.context.value(),
                    "temperature": self.creativity.value() / 100.0,
                    "num_predict": self.max_output.value(),
                    "unlimited_response": False,
                    "stream_responses": True,
                    "think": False,
                    "preload_model": False,
                    "requests_enabled": False,
                    "vision_enabled": self.vision_enabled.isChecked(),
                    "vision_keep_alive": 0,
                    "request_timeout_seconds": self.timeout.value(),
                    "first_token_timeout_seconds": self.first_token_timeout.value(),
                }
            )
            data.setdefault("voice", {}).update(
                {
                    "enabled": self.voice_enabled.isChecked(),
                    "adaptive_decode_retry": self.adaptive_decode_retry.isChecked(),
                    "spoken_spelling_enabled": self.spoken_spelling.isChecked(),
                    "language": "auto",
                    "command_language": selected_language,
                    "stt_model": self.stt_model.currentData() or "small",
                    "partial_stt_model": "tiny",
                    # Preserve the gameplay-safe runtime target. The Settings UI
                    # currently has no explicit STT device selector; older code
                    # silently rewrote CPU/int8 back to auto on every Save, which
                    # could re-enable CUDA and compete with a running game.
                    "stt_device": str(data.get("voice", {}).get("stt_device", "cpu") or "cpu"),
                    "stt_compute_type": str(data.get("voice", {}).get("stt_compute_type", "int8") or "int8"),
                    "input_device": self.input_device.currentData() or "",
                    "sensitivity": self.sensitivity.value(),
                    "noise_rejection_percent": self.noise_rejection.value(),
                    "start_timeout_seconds": self.start_timeout.value(),
                    "silence_seconds": self.silence_seconds.value(),
                    "max_record_seconds": self.max_record.value(),
                    "live_partial_seconds": self.live_partial.value(),
                    "use_live_result_as_final": False,
                    "live_partial_max_updates": 0,
                    "post_answer_rearm_ms": data.get("voice", {}).get("post_answer_rearm_ms", 20),
                    "interaction_mode": self.voice_interaction_mode.currentData() or "wake_each_command",
                    "continuous_after_wake": (self.voice_interaction_mode.currentData() or "wake_each_command") == "continuous",
                    "continuous_rearm_ms": data.get("voice", {}).get("continuous_rearm_ms", 35),
                    "stop_phrases": [
                        line.strip()
                        for line in self.stop_phrases_edit.toPlainText().splitlines()
                        if line.strip()
                    ],
                    "interrupt_on_wake_while_busy": self.interrupt_on_wake.isChecked(),
                    "interrupt_without_wake": self.interrupt_without_wake.isChecked(),
                    "duck_system_while_listening": self.duck_while_listening.isChecked(),
                    "listening_duck_percent": self.listening_duck.value(),
                }
            )
            data.setdefault("routing", {}).update({
                "atlas2_enabled": self.atlas2_enabled.isChecked(),
                "static_hash_enabled": True,
                "semantic_enabled": self.atlas2_semantic.isChecked(),
                "semantic_threshold": float(self.atlas2_threshold.value()),
                "semantic_margin": float(data.get("routing", {}).get("semantic_margin", 0.05) or 0.05),
                "semantic_dimension": 384,
                "lazy_vision_enabled": self.atlas2_lazy_vision.isChecked(),
            })
            data.setdefault("wake_word", {}).update(
                {
                    "enabled": self.wake_enabled.isChecked(),
                    "phrase": self.wake_phrase.text().strip(),
                    "aliases": [
                        line.strip()
                        for line in self.wake_aliases_edit.toPlainText().splitlines()
                        if line.strip()
                    ],
                    "stt_model": self.wake_model.currentData() or "tiny",
                    "chunk_seconds": self.wake_chunk.value(),
                    "silence_seconds": self.wake_scan_interval.value(),
                    "fuzzy_threshold": self.wake_match_sensitivity.value() / 100.0,
                    "acoustic_enabled": False,
                    "quick_commands_enabled": self.quick_media_commands.isChecked(),
                    "quick_capture_enabled": self.quick_capture_commands.isChecked(),
                }
            )
            data.setdefault("capture", {}).update(
                {
                    "nvidia_clip_hotkey": self.capture_clip_hotkey.text().strip().casefold(),
                    "nvidia_record_hotkey": self.capture_record_hotkey.text().strip().casefold(),
                    "video_folder": self.capture_video_folder.text().strip(),
                }
            )
            entered_unlock_password = self.voice_unlock_password.text()
            unlock_password_encrypted = (
                protect_secret(entered_unlock_password)
                if entered_unlock_password else self._existing_voice_unlock_password_encrypted
            )
            unlock_phrase = self.voice_unlock_phrase.text().strip()
            unlock_aliases = [
                line.strip() for line in self.voice_unlock_aliases.toPlainText().splitlines() if line.strip()
            ]
            if self.voice_unlock_enabled.isChecked():
                if len(unlock_phrase.strip()) < 3:
                    raise ValueError("Для голосовой разблокировки задайте кодовую фразу длиной не менее 3 символов.")
                if not unlock_password_encrypted:
                    raise ValueError("Для голосовой разблокировки сохраните пароль учётной записи Windows.")
            data.setdefault("voice_unlock", {}).update({
                "enabled": self.voice_unlock_enabled.isChecked(),
                "account": self.voice_unlock_account.text().strip() or current_windows_account(),
                "password_encrypted": unlock_password_encrypted,
                "phrase": unlock_phrase,
                "aliases": unlock_aliases,
                "locked_session_only": True,
                "tscon_fallback": bool(data.get("voice_unlock", {}).get("tscon_fallback", True)),
                "native_provider_preferred": True,
                "revalidate_password_before_unlock": False,
                "cooldown_seconds": 4.0,
            })

            roots = [line.strip() for line in self.allowed_roots.toPlainText().splitlines() if line.strip()]
            data.setdefault("security", {}).update(
                {
                    "confirm_medium_risk": self.confirm_medium.isChecked(),
                    "auto_file_edits_without_confirmation": self.auto_file_edits.isChecked(),
                    "full_user_file_access": self.full_user_file_access.isChecked(),
                    "restrict_file_mutations": self.restrict_paths.isChecked(),
                    "allowed_roots": roots,
                    "allow_arbitrary_powershell": self.allow_powershell.isChecked(),
                    "bluetooth_connect_notifications": self.bluetooth_connect_notifications.isChecked(),
                    "bluetooth_battery_notifications": self.bluetooth_battery_notifications.isChecked(),
                    "bluetooth_battery_threshold_percent": self.bluetooth_battery_threshold.value(),
                }
            )
            data.setdefault("safety", {})["dangerous_preview"] = self.dangerous_preview.isChecked()
            data.setdefault("security", {}).update({key: widget.isChecked() for key, widget in self.desktop_service_toggles.items()})
            data["security"]["latency_threshold_ms"] = self.latency_threshold.value()
            if hasattr(self, "hw_alerts188"):
                default_lhm = str(Path(self.config.paths.root) / "tools" / "LibreHardwareMonitor")
                lhm_folder = self.lhm_folder188.text().strip()
                data.setdefault("monitoring", {}).update(
                    hardware_alerts_enabled=self.hw_alerts188.isChecked(), cpu_temp_c=self.cpu_temp188.value(),
                    gpu_temp_c=self.gpu_temp188.value(), process_alerts_enabled=self.proc_alerts188.isChecked(),
                    process_cpu_percent=self.proc_cpu188.value(), process_ram_mb=self.proc_ram188.value(),
                    steam_installs_enabled=self.steam_installs188.isChecked(), interval_seconds=self.monitor_interval188.value(),
                    lhm_folder="" if lhm_folder == default_lhm else lhm_folder)
            data.setdefault("downloads", {})["directories"] = [x.strip() for x in self.download_directories.toPlainText().splitlines() if x.strip()]
            data.setdefault("activity", {}).update(enabled=self.activity_enabled.isChecked(), retention_days=self.activity_retention.value(), excluded_apps=[x.strip() for x in self.activity_excluded_apps.toPlainText().splitlines() if x.strip()], excluded_paths=[x.strip() for x in self.activity_excluded_paths.toPlainText().splitlines() if x.strip()])
            data.setdefault("file_index", {})["enabled"] = self.file_index_enabled.isChecked()

            hotkeys_data = data.setdefault("hotkeys", {})
            hotkeys_data["app_microphone_toggle"] = self.app_mic_hotkey.keySequence().toString(QKeySequence.SequenceFormat.PortableText).strip()
            hotkeys_data["autoclicker_toggle"] = self.autoclicker_hotkey.keySequence().toString(QKeySequence.SequenceFormat.PortableText).strip()
            hotkeys_data["chat_scroll_up"] = self.chat_scroll_up_hotkey.keySequence().toString(QKeySequence.SequenceFormat.PortableText).strip()
            hotkeys_data["chat_scroll_down"] = self.chat_scroll_down_hotkey.keySequence().toString(QKeySequence.SequenceFormat.PortableText).strip()
            hotkeys_data["window_topmost_toggle"] = self.window_topmost_hotkey.keySequence().toString(QKeySequence.SequenceFormat.PortableText).strip()
            hotkeys_data["bookshelf_toggle"] = self.bookshelf_hotkey.keySequence().toString(QKeySequence.SequenceFormat.PortableText).strip()
            data.setdefault("shelf", {}).update(enabled=self.shelf_enabled.isChecked(), managed_copy_default=self.shelf_managed.isChecked(), restore_pinned=self.shelf_restore_pinned.isChecked(), layout_editable=self.shelf_layout_editable.isChecked(), font_size=self.shelf_font_size.value(), opacity_percent=self.shelf_opacity.value())
            data["shelf"].update(background_color=self.shelf_background_color.edit.text(), background_opacity_percent=self.shelf_background_opacity.value())
            data["shelf"].update(font_family=self.shelf_font_family.currentFont().family(), font_bold=self.shelf_font_bold.isChecked(), font_italic=self.shelf_font_italic.isChecked(), text_alignment=self.shelf_alignment.currentData(), cell_shape=self.shelf_cell_shape.currentData(), **{key: field.value() for key, field in self.shelf_dimensions.items()})
            data.setdefault("autoclicker", {}).update({
                "enabled": self.autoclicker_enabled.isChecked(),
                "cps": float(self.autoclicker_cps.value()),
                "button": str(self.autoclicker_button.currentData() or "left"),
            })
            data.setdefault("ui", {})["voice_window_visibility_commands_enabled"] = self.voice_window_visibility_commands.isChecked()

            unified_root = Path(self.config.paths.root)
            storage_values = {
                "data_dir": str(unified_root),
                "settings_dir": str(self.config.paths.settings.parent),
                "logs_dir": str(unified_root / "logs"),
                "screenshots_dir": str(unified_root / "screenshots"),
                "backups_dir": str(unified_root / "backups"),
                "cache_dir": str(unified_root / "cache"),
            }
            data.setdefault("storage", {}).update(storage_values)

            integrations = data.setdefault("integrations", {})
            telegram = integrations.setdefault("telegram", {})
            telegram_token = self.telegram_bot_token.text().strip()
            telegram.update({
                "remote_control_enabled": bool(self.telegram_enabled.isChecked()),
                "remote_wake_enabled": bool(self.remote_wake_service.isChecked()),
                "messages_enabled": bool(self.telegram_messages_enabled.isChecked()),
                "enabled": bool(telegram_token and (self.telegram_enabled.isChecked() or self.telegram_messages_enabled.isChecked())),
                "bot_token_encrypted": protect_secret(telegram_token) if telegram_token else "",
                "messages_notifications_enabled": self.telegram_messages_notifications.isChecked(),
                "messages_notification_detail": str(self.telegram_messages_detail.currentData() or "sender_text"),
                "include_in_briefing": self.telegram_messages_briefing.isChecked(),
                "briefing_show_recent24h": self.telegram_briefing_24h.isChecked(),
                "briefing_show_total": self.telegram_briefing_total.isChecked(),
                "briefing_show_senders": self.telegram_briefing_senders.isChecked(),
                "query_show_senders": self.telegram_query_senders.isChecked(),
                "outbound_allowed_people": [x.strip() for x in self.telegram_allowed_people.text().replace("\n", ";").split(";") if x.strip()],
                "outbound_allowed_groups": [x.strip() for x in self.telegram_allowed_groups.text().replace("\n", ";").split(";") if x.strip()],
            })
            self._telegram_apply_security_values(telegram)
            integrations.setdefault("openweather", {}).update({
                "enabled": self.openweather_enabled.isChecked(),
                "api_key_encrypted": protect_secret(self.openweather_api_key.text().strip()) if self.openweather_api_key.text().strip() else "",
                "city": self.openweather_city.text().strip(),
                "country_code": self.openweather_country.text().strip().upper(),
                "units": self.openweather_units.currentData() or "metric",
            })
            # Paid cloud translation was removed in HOTFIX96: no API key or
            # paid translation configuration is persisted.
            integrations.pop("google_translate", None)
            # NEO 184: tokens are saved only with the Client ID they were issued to (Gmail and Calendar share it).
            # HIST-025: the decision is made on copies; the staged tokens change only
            # after config.save() succeeded, so a failed Apply can be retried as is.
            google_client_now = self.gmail_client_id.text().strip()
            if google_client_now != str(getattr(self, "_google_token_client184", google_client_now)):
                calendar_tokens188 = {"refresh_token": "", "access_token": "", "expires_at": 0.0}
                gmail_tokens188 = {"refresh_token": "", "access_token": "", "expires_at": 0.0, "account_email": ""}
                google_commit188 = (calendar_tokens188, gmail_tokens188, google_client_now)
            else:
                calendar_tokens188, gmail_tokens188 = dict(self._calendar_tokens), dict(self._gmail_tokens)
            cal_refresh=str(calendar_tokens188.get("refresh_token","") or ""); cal_access=str(calendar_tokens188.get("access_token","") or "")
            integrations.setdefault("google_calendar", {}).update({
                "enabled": bool(self.google_calendar_enabled.isChecked() and self.gmail_client_id.text().strip() and cal_refresh),
                "refresh_token_encrypted": protect_secret(cal_refresh) if cal_refresh else "",
                "access_token_encrypted": protect_secret(cal_access) if cal_access else "",
                "token_expires_at": float(calendar_tokens188.get("expires_at",0.0) or 0.0),
                "timeout_seconds": 12.0,
                "include_in_briefing": bool(self.google_calendar_briefing.isChecked()),
                "briefing_period": str(self.google_calendar_briefing_period.currentData() or "today"),
            })
            # Remove obsolete duplicated OAuth application credentials.
            for _k in ("use_gmail_oauth_client","client_id_encrypted","client_secret_encrypted"):
                integrations["google_calendar"].pop(_k, None)
            gmail_client_id = self.gmail_client_id.text().strip()
            gmail_client_secret = self.gmail_client_secret.text().strip()
            gmail_refresh = str(gmail_tokens188.get("refresh_token", "") or "")
            gmail_access = str(gmail_tokens188.get("access_token", "") or "")
            integrations.setdefault("gmail", {}).update({
                "enabled": bool(self.gmail_enabled.isChecked() and gmail_client_id and gmail_refresh),
                "client_id_encrypted": protect_secret(gmail_client_id) if gmail_client_id else "",
                "client_secret_encrypted": protect_secret(gmail_client_secret) if gmail_client_secret else "",
                "refresh_token_encrypted": protect_secret(gmail_refresh) if gmail_refresh else "",
                "access_token_encrypted": protect_secret(gmail_access) if gmail_access else "",
                "token_expires_at": float(gmail_tokens188.get("expires_at", 0.0) or 0.0),
                "account_email": str(gmail_tokens188.get("account_email", "") or ""),
                "notifications_enabled": self.gmail_notifications_enabled.isChecked(),
                "include_inbox": self.gmail_include_inbox.isChecked(),
                "additional_folders": [x.strip() for x in self.gmail_additional_folders.text().replace(",", ";").split(";") if x.strip()],
                "notify_total_count": self.gmail_notify_total_count.isChecked(),
                "notify_unread_count": self.gmail_notify_unread_count.isChecked(),
                "notify_recent24h_count": self.gmail_notify_recent24h_count.isChecked(),
                "message_display_detail": str(self.gmail_notification_detail.currentData() or "sender_subject"),
                "include_in_briefing": self.gmail_include_in_briefing.isChecked(),
                "briefing_show_senders": self.gmail_briefing_show_senders.isChecked(),
                "briefing_show_folders": self.gmail_briefing_show_folders.isChecked(),
                "poll_seconds": int(self.gmail_poll_seconds.value()),
                "timeout_seconds": 10.0,
            })
            mailru_email = self.mailru_email.text().strip()
            mailru_password = self.mailru_app_password.text().strip()
            integrations.setdefault("mailru", {}).update({
                "enabled": bool(self.mailru_enabled.isChecked() and mailru_email and mailru_password),
                "email": mailru_email,
                "app_password_encrypted": protect_secret(mailru_password) if mailru_password else "",
                "notifications_enabled": self.mailru_notifications_enabled.isChecked(),
                "include_inbox": self.mailru_include_inbox.isChecked(),
                "additional_folders": [x.strip() for x in self.mailru_additional_folders.text().replace(",", ";").split(";") if x.strip()],
                "notify_total_count": self.mailru_notify_total_count.isChecked(),
                "notify_unread_count": self.mailru_notify_unread_count.isChecked(),
                "notify_recent24h_count": self.mailru_notify_recent24h_count.isChecked(),
                "message_display_detail": str(self.mailru_notification_detail.currentData() or "sender_subject"),
                "include_in_briefing": self.mailru_include_in_briefing.isChecked(),
                "briefing_show_senders": self.mailru_briefing_show_senders.isChecked(),
                "briefing_show_folders": self.mailru_briefing_show_folders.isChecked(),
                "poll_seconds": int(self.mailru_poll_seconds.value()),
                "timeout_seconds": 10.0,
            })
            data.setdefault("briefing", {}).update({
                "enabled": True, "show_on_startup": self.morning_briefing_enabled.isChecked(),
                "include_time": self.briefing_include_time.isChecked(),
                "include_weather": self.briefing_include_weather.isChecked(),
                "include_system": self.briefing_include_system.isChecked(),
                "include_processes": self.briefing_include_processes.isChecked(),
                "include_devices": self.briefing_include_devices.isChecked(),
                "include_bluetooth": self.briefing_include_bluetooth.isChecked(),
                "include_services": self.briefing_include_services.isChecked(),
            })
            data.setdefault("timers", {}).setdefault("enabled", True)
            storage_changed = False

            dictionary_rows = []
            for row in range(self.dictionary_table.rowCount()):
                def cell(col: int) -> str:
                    item = self.dictionary_table.item(row, col); return item.text().strip() if item else ""
                name, kind, path, aliases_text = cell(0), cell(1), cell(2), cell(3)
                if not any((name, path, aliases_text)): continue
                dictionary_rows.append({"name": name or Path(path).stem, "kind": kind or "app", "path": path, "aliases": [x.strip() for x in aliases_text.replace(";", ",").split(",") if x.strip()]})
            data.setdefault("personal_dictionary", {}).update({"enabled": self.personal_dictionary_enabled.isChecked(), "entries": dictionary_rows})

            # The editor retains complete legacy steps/aliases/IDs and extension
            # metadata; ConfigManager saves the whole draft atomically below.
            data.setdefault("scenarios", {})["items"] = self.scenario_editor.items()

            developer = data.setdefault("developer_mode", {})
            developer["settings_enabled"] = bool(self.developer_settings_enabled.isChecked())
            developer["enabled"] = bool(self.dev_enabled.isChecked()) if developer["settings_enabled"] else False
            developer["always_on_top"] = bool(self.dev_always_on_top.isChecked())
            developer["show_raw_speech"] = bool(self.dev_raw.isChecked())
            developer["show_normalized"] = bool(self.dev_norm.isChecked())
            developer["show_intent"] = bool(self.dev_intent.isChecked())
            developer["show_entities"] = bool(self.dev_intent.isChecked())
            developer["show_tool"] = bool(self.dev_tools.isChecked())
            developer["show_fallback"] = bool(self.dev_intent.isChecked())
            developer["show_latency"] = bool(self.dev_latency.isChecked())
            developer["show_result"] = bool(self.dev_tools.isChecked())
            developer["slow_threshold_seconds"] = float(self.dev_slow.value())

            data.setdefault("voice", {}).update({
                "quick_media_volume_enabled": bool(self.quick_media_volume_commands.isChecked()),
                "quick_media_repeat_shuffle_enabled": bool(self.quick_media_repeat_shuffle_commands.isChecked()),
            })
            active_music_provider = self._active_music_provider_from_ui()
            desktop = data.setdefault("desktop", {})
            desktop.update({
                "preferred_browser": self.preferred_browser.currentData() or "firefox",
                "preferred_music_app": active_music_provider or "yandex_music",
                "auto_launch_music_app": False,
                "screen_aware_actions": self.screen_aware_actions.isChecked(),
                "vision_for_ambiguous_actions": self.vision_ambiguous.isChecked(),
            })
            integrations = data.setdefault("integrations", {})
            integrations.pop("apple_music", None)
            integrations.setdefault("music", {})["active_provider"] = active_music_provider
            integrations.setdefault("yandex_music", {}).update({
                "enabled": active_music_provider == "yandex_music",
                "api_enabled": self.yandex_api_enabled.isChecked(),
                "background_playback": self.yandex_background_playback.isChecked(),
                "background_volume_percent": int(self.yandex_background_volume.value()),
                "never_steal_input": True,
                "ui_fallback": False,
                "protocol_fallback": False,
                "token_encrypted": (
                    protect_secret(self.yandex_api_token.text().strip())
                    if self.yandex_api_token.text().strip() else self._existing_yandex_token_encrypted
                ),
            })
            spotify_client_id = self.spotify_client_id.text().strip()
            integrations.setdefault("spotify", {}).update({
                "enabled": active_music_provider == "spotify",
                "client_id_encrypted": protect_secret(spotify_client_id) if spotify_client_id else self._existing_spotify_client_id_encrypted,
                # NEO 182: tokens come from the live settings, not from the snapshot taken when the window
                # opened — Spotify may have rotated the refresh token meanwhile (OAuth here saves them too).
                "access_token_encrypted": str(self.config.get("integrations.spotify.access_token_encrypted", "") or ""),
                "refresh_token_encrypted": str(self.config.get("integrations.spotify.refresh_token_encrypted", "") or ""),
                "token_expires_at": float(self.config.get("integrations.spotify.token_expires_at", 0.0) or 0.0),
                "redirect_uri": self.spotify_redirect_uri.text().strip() or "http://127.0.0.1:8765/callback",
            })
            youtube_key = self.youtube_api_key.text().strip()
            integrations.setdefault("youtube", {}).update({
                "enabled": bool(self.youtube_integration_enabled.isChecked()),
                "api_key_encrypted": protect_secret(youtube_key) if youtube_key else self._existing_youtube_key_encrypted,
                "music_playlists": youtube_music_playlists,
            })
            discord_profiles, discord_default_server = self._discord_profile_rows()
            default_profile = next((p for p in discord_profiles if p["name"].casefold() == discord_default_server.casefold()), discord_profiles[0] if discord_profiles else {})
            integrations.setdefault("discord", {}).update({
                "enabled": self.discord_integration_enabled.isChecked(),
                "background_bot_enabled": self.discord_background_bot_enabled.isChecked(),
                "auto_follow_user_voice": self.discord_auto_follow_user_voice.isChecked(),
                "leave_with_owner": self.discord_leave_with_owner.isChecked(),
                "shared_music_queue_enabled": True,
                "never_steal_input": True,
                "ui_fallback": False,
                # Kept only as a legacy fallback for pre-v4.0.8 settings. New
                # tokens live inside each server profile above.
                "bot_token_encrypted": "",
                "servers": discord_profiles,
                "default_server": discord_default_server,
                # Legacy mirrors keep older parsers/tools compatible with the selected default profile.
                "music_text_channel": "",
                "default_voice_channel": "",
                "music_bot_name": "",
                "music_bot_mode": "api_only",
                "music_play_template": "",
                "music_query_option": "",
                "verify_actions": True,
            })
            gaming_data = data.setdefault("gaming", {})
            gaming_data.update({
                "enabled": self.gaming_enabled.isChecked(),
                "auto_detect": self.gaming_enabled.isChecked(),
                "telemetry_refresh_seconds": float(self.gaming_telemetry_refresh.value()),
                "overlay_clock_refresh_ms": 1000,
            })
            web_knowledge = gaming_data.setdefault("web_knowledge", {})
            # V7.6 runtime is SearchAPI.io Google AI Mode only. Retired browser,
            # SerpApi and Tavily knobs are deleted on every save.
            for retired_key in (
                "ai_mode_backend", "ai_mode_browser_path", "ai_mode_browser_timeout_seconds",
                "ai_mode_virtual_time_ms", "ai_mode_cdp_timeout_seconds",
                "ai_mode_auto_open_profile_setup", "ai_mode_require_signed_in_profile",
                "serpapi_api_key_encrypted", "serpapi_timeout_seconds",
                "tavily_api_key_encrypted", "tavily_search_depth", "tavily_answer_mode",
                "tavily_max_results", "tavily_chunks_per_source", "tavily_timeout_seconds",
                "searchapi_ai_overview_fallback",
            ):
                web_knowledge.pop(retired_key, None)
            searchapi_key = self.game_web_searchapi_key.text().strip()
            web_knowledge.update({
                "enabled": bool(self.game_web_enabled.isChecked()),
                "searchapi_api_key_encrypted": protect_secret(searchapi_key) if searchapi_key else "",
                "searchapi_gl": str(self.game_web_searchapi_gl.currentData() or "us"),
                "searchapi_max_sources": int(self.game_web_searchapi_sources.value()),
                "searchapi_timeout_seconds": float(self.game_web_searchapi_timeout.value()),
                "show_sources": bool(self.game_web_show_sources.isChecked()),
                "suggest_followup": bool(self.game_web_suggest_followup.isChecked()),
                "structured_answers": bool(self.game_web_structured171.isChecked()),
                "cache_ttl_hours": int(self.game_web_cache_ttl.value()),
                "dynamic_cache_ttl_minutes": int(self.game_web_dynamic_ttl.value()),
                "cache_max_mb": int(self.game_web_cache_max_mb.value()),
            })
            style_root = desktop.setdefault("overlay", {}).setdefault("styles", {})
            for style_key, fields in self.overlay_type_style_fields.items():
                style_data = {
                    "enabled": bool(fields["enabled"].isChecked()) if "enabled" in fields else True,
                    "theme_preset": str(fields["theme_preset"].currentData() or "Custom") if "theme_preset" in fields else "Custom",
                    "font_family": fields["font_family"].currentFont().family().strip() or "Segoe UI",
                    "font_size": int(fields["font_size"].value()),
                    "opacity": int(fields["background_opacity"].value()),
                    "background_opacity": int(fields["background_opacity"].value()),
                    "border_opacity": int(fields["border_opacity"].value()),
                    "drag_enabled": bool(fields["drag_enabled"].isChecked()),
                    "width": int(fields["width"].value()),
                    "height": int(fields["height"].value()),
                    "monitor": int(fields["monitor"].value()),
                    "position": fields["position"].currentData() or "top_left",
                    "click_through": bool(fields["click_through"].isChecked()),
                    "text_color": self._validated_color(fields["text_color"], f"overlay.styles.{style_key}.text_color"),
                    "background_color": self._validated_color(fields["background_color"], f"overlay.styles.{style_key}.background_color"),
                    "border_color": self._validated_color(fields["border_color"], f"overlay.styles.{style_key}.border_color"),
                }
                if style_key == "music" and "controls_visible" in fields:
                    style_data.update({
                        "controls_visible": bool(fields["controls_visible"].isChecked()),
                        "control_color": self._validated_color(fields["control_color"], "overlay.styles.music.control_color"),
                        "control_background_color": self._validated_color(fields["control_background_color"], "overlay.styles.music.control_background_color"),
                        "control_background_opacity": int(fields["control_background_opacity"].value()),
                        "control_border_color": self._validated_color(fields["control_border_color"], "overlay.styles.music.control_border_color"),
                        "control_border_opacity": int(fields["control_border_opacity"].value()),
                        "visualizer_fps": int(fields["visualizer_fps"].value()),
                        "visualizer_mode": fields["visualizer_mode"].currentData() or "symmetric",
                    })
                # Merge: keep supported keys this page has no control for
                # (e.g. notes padding / border_width).
                merged_style = dict(style_root.get(style_key) or {}) if isinstance(style_root.get(style_key), dict) else {}
                merged_style.update(style_data)
                style_root[style_key] = merged_style
            data.setdefault("performance", {})["profile"] = str(self.performance_profile.currentData() or "balanced")
            data.setdefault("local_chat", {})["enabled"] = self.local_chat_enabled169.isChecked()
            data.setdefault("memory", {})["enabled"] = self.memory_enabled169.isChecked()
            neo169 = data.setdefault("neo_model", {})
            neo169["enabled"] = self.neo_model_enabled169.isChecked()
            neo169.setdefault("schedule", {})["enabled"] = self.neo_model_schedule169.isChecked()
            data.setdefault("diagnostics", {})["log_content"] = self.log_content169.isChecked()
            agent170 = data.setdefault("llm_agent", {})
            previous_size = str(agent170.get("size", "0.5b") or "0.5b")
            agent170["enabled"] = self.llm_agent_enabled170.isChecked()
            agent170["size"] = str(self.llm_agent_size170.currentData() or "0.5b")
            from jarvis.runtime.settings170 import model_for
            if agent170["size"] != previous_size or not agent170.get("base_model"):
                agent170["base_model"] = model_for(agent170["size"])["ollama"]
            agent170.setdefault("finetune", {})["auto"] = self.llm_finetune_auto170.isChecked()
            data.setdefault("privacy", {})["train_on_corrections"] = self.train_on_corrections188.isChecked()
            data.setdefault("local_chat", {})["stream"] = self.local_stream172.isChecked()
            data.setdefault("memory", {})["propose"] = self.memory_propose172.isChecked()
            data.setdefault("learning", {})["collect_phrases"] = self.learning_collect172.isChecked()
            data.setdefault("learning", {})["remember_resolved"] = self.learning_remember177.isChecked()
            # NEO 184: Settings → Перевод.
            translation184 = data.setdefault("translation", {})
            if not isinstance(translation184, dict):
                translation184 = data["translation"] = {}
            translation184.update({
                "engine": str(self.translation_engine184.currentData() or "auto"),
                "model": self._translation_selected_model184(),
                "default_target": str(self.translation_target184.currentData() or ""),
                "offer_install": self.translation_offer184.isChecked(),
                "fallback_to_searchapi": self.translation_fallback184.isChecked(),
                "show_engine": self.translation_show184.isChecked(),
                "keep_alive_minutes": int(self.translation_keep184.value()),
                "max_chars": int(self.translation_limit184.value()),
            })
            help172 = data.setdefault("help", {})
            help172["confirm_before_search"] = self.help_confirm172.isChecked()
            help172["use_screen_model"] = self.help_vision172.isChecked()
            if agent170["size"] != previous_size:
                # Size selection is staged configuration only. The dedicated
                # developer Install button is the sole model-install action.
                agent170["fine_tuned"] = ""
            gaming169 = data.setdefault("gaming", {})
            gaming169["launch_greetings_enabled"] = self.game_launch_notice169.isChecked()
            gaming169.setdefault("notifications", {}).update({
                "session_start": self.game_session_start169.isChecked(),
                "session_end": self.game_session_end169.isChecked(),
                "nvidia_enabled": self.nvidia_notices184.isChecked(),
                "nvidia_startup": self.nvidia_startup_notice184.isChecked(),
                "replay_off_warning": self.replay_off_warning169.isChecked(),
                "replay_changes": self.replay_changes169.isChecked(),
                "replay_unknown_warning": self.replay_unknown169.isChecked(),
            })
            gaming169.setdefault("match_ready", {}).update({
                "enabled": self.match_ready_enabled169.isChecked(),
                "telegram": self.match_ready_telegram169.isChecked(),
            })
            gaming169.setdefault("instant_replay", {})["toggle_hotkey"] = self.replay_hotkey169.text().strip().casefold()
            gaming169["instant_replay"]["auto_enable"] = self.replay_auto_enable171.isChecked()
            gaming169["instant_replay"]["auto_launch_app"] = self.nvidia_auto_launch184.isChecked()
            gaming169["instant_replay"]["launch_for_commands"] = self.nvidia_command_launch185.isChecked()
            ui = data.setdefault("ui", {})
            # HOTFIX53: the robot HUD can be dragged while this dialog remains
            # open after pressing Apply. MainWindow writes that layout directly to
            # ConfigManager, so always take the *latest* live layout here unless
            # the user explicitly staged "Reset information layout" in Settings.
            live_robot_info_layout = self.config.get("ui.robot_info_layout", {}) or {}
            robot_info_layout_to_save = deepcopy(
                self._robot_info_layout_pending
                if bool(getattr(self, "_robot_info_layout_reset_pending", False))
                else live_robot_info_layout
            )
            ui.update(
                {
                    "window_width": self.window_width.value(),
                    "window_height": self.window_height.value(),
                    "animation_fps": int(self.animation_fps.currentData() or 60),
                    "drag_enabled": self.drag_enabled.isChecked(),
                    "always_on_top": False,
                    "start_with_windows": self.autostart.isChecked(),
                    "priority_startup": bool(self.autostart.isChecked() and self.priority_startup.isChecked()),
                    "show_tool_details": self.show_tools.isChecked(),
                    "robot_only_mode": self.robot_only_mode.isChecked(),
                    "media_now_playing_notifications": self.media_now_playing_notifications.isChecked(),
                    "character_streaming_enabled": bool(self.character_streaming_enabled.isChecked()),
                    "typewriter_interval_ms": self.typewriter_speed.value(),
                    "chat_font_family": self.chat_font_family.currentFont().family().strip() or "Segoe UI",
                    "chat_font_size_px": self.chat_font_size.value(),
                    "window_theme_preset": str(self.window_theme_preset.currentData() or "Custom"),
                    "robot_theme_preset": str(self.robot_theme_preset.currentData() or "Custom"),
                    "window_opacity_percent": int(self.window_background_opacity.value()),
                    "panel_opacity_percent": int(self.panel_opacity.value()),
                    "element_opacity_percent": int(self.element_opacity.value()),
                    "chat_opacity_percent": int(self.chat_opacity.value()),
                    "bubble_opacity_percent": int(self.bubble_opacity.value()),
                    "border_opacity_percent": int(self.border_opacity.value()),
                    # Legacy mirrors remain for older modules/configs.
                    "transparent_background": self.window_background_opacity.value() == 0,
                    "transparent_borders": self.border_opacity.value() == 0,
                    "show_robot": self.show_robot.isChecked(),
                    "idle_animations": self.idle_animations.isChecked(),
                    "sleep_after_seconds": self.sleep_after.value(),
                    "corner_radius": self.corner_radius.value(),
                    "robot_scale_percent": self.robot_scale.value(),
                    "robot_active_animation": self.robot_active_animation.currentData() or "off",
                    "robot_cloud_density": self.robot_cloud_density188.currentData() or "normal",
                    "robot_accessory_coffee": self.robot_accessory_coffee.isChecked(),
                    "robot_accessory_cigarette": self.robot_accessory_cigarette.isChecked(),
                    "timer_overlay_enabled": self.timer_overlay_enabled.isChecked(),
                    "timer_overlay_position": self.timer_overlay_position.currentData() or "bottom_left",
                    "timer_overlay_font_size": self.timer_overlay_font_size.value(),
                    "timer_overlay_opacity_percent": self.timer_overlay_opacity.value(),
                    "timer_overlay_style": str(self.timer_overlay_style.currentData() or "glass"),
                    "timer_overlay_text_color": self.timer_overlay_text_color.value(),
                    "timer_overlay_background_color": self.timer_overlay_background_color.value(),
                    "timer_overlay_border_color": self.timer_overlay_border_color.value(),
                    "robot_info_clock_enabled": self.robot_info_clock_enabled.isChecked(),
                    "robot_info_clock_position": self.robot_info_clock_position.currentData() or "top_center",
                    "robot_info_track_enabled": self.robot_info_track_enabled.isChecked(),
                    "robot_info_track_position": self.robot_info_track_position.currentData() or "bottom_center",
                    "robot_info_temperature_enabled": self.robot_info_temperature_enabled.isChecked(),
                    "robot_info_temperature_position": self.robot_info_temperature_position.currentData() or "top_right",
                    "robot_info_temperature_show_city": self.robot_info_temperature_show_city.isChecked(),
                    "robot_info_font_size": self.robot_info_font_size.value(),
                    "robot_layout_edit_enabled": self.robot_layout_edit_enabled.isChecked(),
                    "chat_height_px": self.chat_height_px.value(),
                    "robot_info_layout": robot_info_layout_to_save if isinstance(robot_info_layout_to_save, dict) else {},
                }
            )
            for key, field in self.ui_colors.items():
                ui[key] = self._validated_color(field, key)
            for key, field in self.robot_colors.items():
                ui[key] = self._validated_color(field, key)
            ui.update(robot_headphones_enabled=self.robot_headphones_enabled.isChecked(), robot_headphones_color=self._validated_color(self.robot_headphones_color, "robot_headphones_color"), robot_gif_enabled=self.robot_gif_enabled.isChecked(), robot_gif_states=deepcopy(self._robot_gif_states), robot_gif_scale_percent=self._robot_gif_scale)
            for key, field in self.robot_accessory_colors.items():
                ui[key] = self._validated_color(field, key)
            for key, field in self.environment_colors.items():
                ui[key] = self._validated_color(field, key)
            # Starry sky intentionally shares the starfall palette unless a future
            # build exposes it separately. The renderer still has its own channel.
            ui["robot_starry_sky_color"] = ui.get("robot_starfall_color", "#EAF6FF")
            self._restore_untouched_numeric185(data)
            self.config.save(data)
            if google_commit188 is not None:
                self._calendar_tokens, self._gmail_tokens, self._google_token_client184 = google_commit188
            self._snapshot_numeric_fields185()
            if hasattr(self, "streaming_page"):
                self.streaming_page.save_succeeded(streaming_password_update)
            self._robot_info_layout_pending = deepcopy(self.config.get("ui.robot_info_layout", {}) or {})
            self._robot_info_layout_reset_pending = False
            # Reflect Yandex connection state immediately in the still-open dialog;
            # reopening Settings is no longer required.
            self._existing_yandex_token_encrypted = str(self.config.get("integrations.yandex_music.token_encrypted", "") or "")
            if hasattr(self, "yandex_connect_status"):
                self._status_text(self.yandex_connect_status, "Статус: подключено" if self._existing_yandex_token_encrypted else "Статус: не подключено")
                self.yandex_connect_button.setText(self._t("Переподключить Яндекс Музыку" if self._existing_yandex_token_encrypted else "Подключить Яндекс Музыку"))
            provider_install_result = None
            if self.voice_unlock_enabled.isChecked() and os.name == "nt":
                provider_install_result = ensure_native_provider_registered(elevate=True)
        except Exception as exc:
            QMessageBox.critical(self, "Не удалось сохранить настройки", str(exc))
            return
        if locals().get("provider_install_result") is not None:
            result = provider_install_result
            self.voice_unlock_status.setText(self._rt(result.status))
            if not result.ok:
                QMessageBox.warning(self, "Разблокировка Windows", result.status)
        native_error = str(getattr(self.config, "native_apply_error186", "") or "")
        if native_error:
            # 186: the settings are saved; only the Windows startup entry failed.
            QMessageBox.warning(self, self._t("Настройки сохранены"),
                                self._t("Настройки сохранены, но автозапуск Windows применить не удалось. NEO повторит попытку при следующем сохранении.") + "\n\n" + native_error)
        self.settings_saved.emit()
        self.data = deepcopy(self.config.data)
        self._refresh_dialog_theme()
        applied_code = language_code(self.config)
        translate_widget_tree(self, applied_code)
        if hasattr(self, "streaming_page"):
            self.streaming_page.retranslate()
        self._translate_table_headers184(self)
        self.tabs.retranslated182()
        if hasattr(self, "telegram_connection_status"):
            self._telegram_refresh_status()
        sync = getattr(self, "_sync_yandex_language_visibility", None)
        if callable(sync):
            sync()
        label = getattr(self, "discord_source_help", None)
        if label is not None:
            if applied_code == "ru":
                label.setText(self._t("Discord NeoBot воспроизводит источники Яндекс Музыки и прямые ссылки YouTube. Он остаётся отдельным Discord-плеером."))
            else:
                label.setText(tr("Discord NeoBot воспроизводит прямые ссылки YouTube и остаётся отдельным Discord-плеером.", applied_code))
        if locals().get("storage_changed", False):
            QMessageBox.information(
                self,
                "Папки данных NEO",
                "Новые папки сохранены. Для полного переключения базы данных, логирования и всех фоновых интеграций перезапустите NEO. Существующие файлы не перемещаются автоматически.",
            )
        if close:
            self.accept()
