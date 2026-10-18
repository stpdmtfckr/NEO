"""NEO 178: settings pages that fit their window.

Long check box labels wrap onto the next line, rows of buttons flow onto a second line,
the sidebar shortens long section names with «…»; nothing needs a horizontal scrollbar.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QBoxLayout, QCheckBox, QLineEdit, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLayout, QPushButton,
    QSizePolicy, QStyle, QStyleOptionButton, QStylePainter, QWidget,
)


class WrapCheckBox(QCheckBox):
    """NEO 178: a check box whose label wraps onto the next line when the window is narrow.

    Qt's own check box keeps its text on one line, so long labels were cut off or pushed a
    horizontal scrollbar into every page.  ``text()``/``setText()`` and the stylesheet
    (indicator, colours, search highlight) work as before; a click on any line toggles it.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def _parts(self) -> tuple[QStyleOptionButton, QRect, int]:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        indicator = self.style().subElementRect(QStyle.SE_CheckBoxIndicator, option, self)
        spacing = self.style().pixelMetric(QStyle.PM_CheckBoxLabelSpacing, option, self)
        return option, indicator, spacing

    def _text_rect(self, width: int, indicator: QRect, spacing: int) -> QRect:
        left = indicator.right() + 1 + spacing
        return QRect(left, 0, max(24, width - left - 2), 100000)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return bool(self.text())

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        _option, indicator, spacing = self._parts()
        bounds = self.fontMetrics().boundingRect(self._text_rect(width, indicator, spacing), int(Qt.TextWordWrap | Qt.TextShowMnemonic), self.text())
        padding = max(0, super().sizeHint().height() - max(indicator.height(), self.fontMetrics().height()))
        return max(indicator.height(), bounds.height()) + padding

    def sizeHint(self) -> QSize:  # noqa: N802
        base = super().sizeHint()
        return QSize(base.width(), max(base.height(), self.heightForWidth(base.width())))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        _option, indicator, spacing = self._parts()
        words = [self.fontMetrics().horizontalAdvance(word) for word in self.text().split()] or [0]
        width = indicator.right() + 1 + spacing + min(max(words), 160) + 4
        return QSize(width, super().minimumSizeHint().height())

    def hitButton(self, pos) -> bool:  # noqa: N802
        return self.rect().contains(pos)

    def resizeEvent(self, event) -> None:  # noqa: N802
        # Not every Qt layout asks heightForWidth() (QFormLayout rows do not), so the
        # wrapped height is also given as the minimum height: no line is ever cut off.
        super().resizeEvent(event)
        need = max(self.heightForWidth(self.width()) if self.text() else 0, getattr(self, '_floor178', 0))
        if need and need != self.minimumHeight():
            self._own178 = True
            try:
                self.setMinimumHeight(need)
            finally:
                self._own178 = False

    def setMinimumHeight(self, height: int) -> None:  # noqa: N802
        # A minimum asked for by the page (e.g. 30 px rows) stays the floor under the wrapped height.
        if not getattr(self, '_own178', False):
            self._floor178 = int(height)
        super().setMinimumHeight(height)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QStylePainter(self)
        option, indicator, spacing = self._parts()
        option.text = ""
        painter.drawPrimitive(QStyle.PE_Widget, option)
        metrics = self.fontMetrics()
        area = self._text_rect(self.width(), indicator, spacing)
        lines = max(1, round(metrics.boundingRect(area, int(Qt.TextWordWrap), self.text()).height() / max(1, metrics.lineSpacing())))
        box = QStyleOptionButton(option)
        if lines > 1:
            # Several lines: the box stands next to the first one.
            top = max(0, (self.height() - metrics.lineSpacing() * lines) // 2)
            box.rect = QRect(indicator.x(), top + (metrics.lineSpacing() - indicator.height()) // 2, indicator.width(), indicator.height())
            area = QRect(area.x(), top, area.width(), self.height() - top)
            flags = Qt.AlignLeft | Qt.AlignTop
        else:
            box.rect = indicator
            area = QRect(area.x(), 0, area.width(), self.height())
            flags = Qt.AlignLeft | Qt.AlignVCenter
        painter.drawPrimitive(QStyle.PE_IndicatorCheckBox, box)
        group = QPalette.Active if self.isEnabled() else QPalette.Disabled
        painter.setPen(self.palette().color(group, QPalette.WindowText))
        painter.drawText(area, int(flags | Qt.TextWordWrap | Qt.TextShowMnemonic), self.text())
        if self.hasFocus() and self.focusPolicy() != Qt.NoFocus:
            # NEO 182: keyboard users see which switch Space will toggle.
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(QPen(self.palette().color(QPalette.Active, QPalette.Highlight), 1.6))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 5, 5)


class NavButton(QPushButton):
    """NEO 178: a sidebar section button that shortens its title with «…» in a narrow window
    (the full title stays in ``text()``, the tooltip and the page header)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # HIST-012: Enter in a settings field must not «press» a navigation button
        # (a QPushButton in a dialog is auto-default); Space/Return on the focused
        # button itself still work.
        self.setAutoDefault(False)
        self.setDefault(False)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(40, super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(40, super().sizeHint().height())

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        contents = self.style().subElementRect(QStyle.SE_PushButtonContents, option, self)
        font = QFont(self.font())
        if self.isChecked():
            font.setWeight(QFont.DemiBold)                           # the selected section is drawn bold
        option.text = QFontMetrics(font).elidedText(self.text(), Qt.ElideRight, max(10, contents.width() - 18))
        painter.drawControl(QStyle.CE_PushButton, option)


_GROWS = (QSizePolicy.Expanding, QSizePolicy.MinimumExpanding, QSizePolicy.Ignored)


class FlowLayout(QLayout):
    """NEO 178: a row of buttons that moves the buttons that do not fit to the next line."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list = []
        self._gap = spacing
        # UI185-MODEL-WIDTH-01: the field that stretched in the original row keeps
        # growing here, whatever its size-policy enum says.
        self._grow: set[int] = set()
        self.setContentsMargins(0, 0, 0, 0)

    def mark_growing(self, widget) -> None:
        self._grow.add(id(widget))

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._place(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._place(rect, move=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        visible = [item for item in self._items if not item.isEmpty()]
        width = sum(item.sizeHint().width() for item in visible) + self._gap * max(0, len(visible) - 1)
        height = max((item.sizeHint().height() for item in visible), default=0)
        return QSize(width, height)

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _place(self, rect: QRect, *, move: bool) -> int:
        # Lines first, then the room left on each line goes to its field (line edit, list).
        lines: list[list] = [[]]
        x = 0
        for item in self._items:
            if item.isEmpty():
                continue
            width = item.sizeHint().width()
            if lines[-1] and x + width > rect.width():
                lines.append([])
                x = 0
            lines[-1].append(item)
            x += width + self._gap
        y = rect.y()
        for row in lines:
            if not row:
                continue
            used = sum(item.sizeHint().width() for item in row) + self._gap * (len(row) - 1)
            growing = [item for item in row if item.widget() is not None
                       and (id(item.widget()) in self._grow
                            or item.widget().sizePolicy().horizontalPolicy() in _GROWS)]
            extra = max(0, rect.width() - used) // len(growing) if growing else 0
            height = max(item.sizeHint().height() for item in row)
            x = rect.x()
            for item in row:
                width = min(rect.width(), item.sizeHint().width() + (extra if item in growing else 0))
                if move:
                    item.setGeometry(QRect(x, y, width, height))
                x += width + self._gap
            y += height + self._gap
        return max(0, y - self._gap - rect.y())


def _button_row(layout) -> list[QPushButton] | None:
    """The buttons of a row made only of buttons (stretches allowed), else None."""
    if type(layout) is not QHBoxLayout or layout.count() < 2:
        return None
    widgets = []
    for index in range(layout.count()):
        item = layout.itemAt(index)
        widget = item.widget()
        if widget is None:
            if item.layout() is not None:
                return None
            continue                                              # stretches / spacers
        if not isinstance(widget, (QPushButton, QLineEdit, QComboBox)):
            return None
        widgets.append(widget)
    fields = [w for w in widgets if not isinstance(w, QPushButton)]
    if len(widgets) < 2 or len(fields) > 1 or sum(w.sizeHint().width() for w in widgets) < 230:
        return None
    return widgets


def _flow(widgets: list[QPushButton], spacing: int, parent: QWidget | None = None, grow=()) -> FlowLayout:
    flow = FlowLayout(parent, spacing)
    for widget in widgets:
        flow.addWidget(widget)
        if widget in grow or not isinstance(widget, QPushButton):
            flow.mark_growing(widget)              # a field (model name, path) takes the free room
    return flow


def _stretched(layout, widgets) -> list:
    out = []
    for index in range(layout.count()):
        widget = layout.itemAt(index).widget()
        if widget in widgets and layout.stretch(index) > 0:
            out.append(widget)
    return out


def _flow_button_rows(root: QWidget) -> None:
    """Rows made only of buttons become flowing rows (they no longer force a wide window)."""
    for box in [root] + root.findChildren(QWidget):
        layout = box.layout()
        widgets = _button_row(layout)
        if widgets:
            spacing = max(4, layout.spacing())
            grow = _stretched(layout, widgets)
            for widget in widgets:
                layout.removeWidget(widget)
            QWidget().setLayout(layout)                              # detach and drop the old row
            _flow(widgets, spacing, box, grow)
            continue
        if layout is None:
            continue
        found = []
        stack = [layout]
        while stack:                                                 # rows nested in a page layout
            parent = stack.pop()
            for index in range(parent.count()):
                child = parent.itemAt(index).layout()
                if child is None:
                    continue
                widgets = _button_row(child)
                if widgets and isinstance(parent, QBoxLayout):
                    found.append((parent, child, widgets))
                else:
                    stack.append(child)
        for parent, child, widgets in found:
            # The emptied old row stays where it was (Qt owns it); the flowing row goes just before it.
            spacing = max(4, child.spacing())
            grow = _stretched(child, widgets)
            for widget in widgets:
                child.removeWidget(widget)
            flow = _flow(widgets, spacing, None, grow)
            parent.insertLayout(max(0, parent.indexOf(child)), flow)
            # PySide does not hand an unparented layout over to Qt: keep it alive with the page.
            kept = getattr(box, '_flows178', None)
            if kept is None:
                kept = []
                box._flows178 = kept
            kept.append(flow)


class _GroupTitleFitter(QObject):
    """A group box title is one line: in a narrow window it is shortened with «…» (full title in the tooltip)."""

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Resize and isinstance(watched, QGroupBox):
            _elide_title(watched)
        return False


_FITTER = None


def _elide_title(group: QGroupBox) -> None:
    shown = group.title()
    if shown != group.property('shownTitle178'):                    # set or translated by someone else
        group.setProperty('fullTitle178', shown)
    full = str(group.property('fullTitle178') or shown)
    if group.property('i18n_title_source') is None and full:
        group.setProperty('i18n_title_source', full)               # translation keeps the full title as its source
    room = max(40, group.width() - 40)
    text = QFontMetrics(group.font()).elidedText(full, Qt.ElideRight, room) if group.width() > 0 else full
    group.setProperty('shownTitle178', text)
    if text != shown:
        group.setTitle(text)
    group.setToolTip(full if text != full else group.toolTip() if group.toolTip() != full else '')


def full_title(group: QGroupBox) -> str:
    """The whole title of a group box, also while it is shown shortened."""
    shown = group.title()
    if shown != group.property('shownTitle178'):
        return shown                                                # changed since (e.g. translated)
    return str(group.property('fullTitle178') or shown)


def _fit_group_title(group: QGroupBox) -> None:
    global _FITTER
    if group.property('titleFit178'):
        return
    if _FITTER is None:
        _FITTER = _GroupTitleFitter(QApplication.instance())
    group.setProperty('titleFit178', True)
    group.setProperty('fullTitle178', group.title())
    group.setProperty('shownTitle178', group.title())
    hint = group.minimumSizeHint().width()
    title = QFontMetrics(group.font()).horizontalAdvance(group.title()) + 40
    inner = group.layout().minimumSize().width() if group.layout() is not None else 0
    if title > inner + 20:
        group.setMinimumWidth(max(100, min(hint, inner + 20)))       # the title no longer sets the width
    group.installEventFilter(_FITTER)


class _WrappedLabelHeight(QObject):
    """NEO 182: a word-wrapped form label gets the height its text needs at its real width
    (QFormLayout sizes the row from the label's preferred width and cut off the last line)."""

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() in (QEvent.Resize, QEvent.Show, QEvent.FontChange) and isinstance(obj, QLabel) and obj.wordWrap():
            need = obj.heightForWidth(max(1, obj.width()))
            if need > 0 and obj.minimumHeight() != need:
                obj.setMinimumHeight(need)
        return False


_WRAPPED_LABEL_HEIGHT = None


def _keep_wrapped_label_height(label: QLabel) -> None:
    global _WRAPPED_LABEL_HEIGHT
    if _WRAPPED_LABEL_HEIGHT is None:
        _WRAPPED_LABEL_HEIGHT = _WrappedLabelHeight()
    if not label.property("neoWrapHeight182"):
        label.setProperty("neoWrapHeight182", True)
        label.installEventFilter(_WRAPPED_LABEL_HEIGHT)


def responsive(root: QWidget) -> None:
    """Let a settings page shrink with the window: long texts wrap instead of being cut off."""
    for form in root.findChildren(QFormLayout):
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    for label in root.findChildren(QLabel):
        if label.objectName() in {"settingsPageTitle"} or label.pixmap() is not None and not label.pixmap().isNull():
            continue
        if len(label.text()) > 24 and not label.wordWrap():
            label.setWordWrap(True)
    for form in root.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            cell = form.itemAt(row, QFormLayout.LabelRole)
            label = cell.widget() if cell else None
            if isinstance(label, QLabel):
                _keep_wrapped_label_height(label)
    for combo in root.findChildren(QComboBox):
        if combo.sizeAdjustPolicy() != QComboBox.AdjustToMinimumContentsLengthWithIcon:
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
    for spin in root.findChildren(QAbstractSpinBox):
        if spin.minimumWidth() == 0:
            spin.setMinimumWidth(min(spin.minimumSizeHint().width(), 84))   # «360 px» still fits
    for group in root.findChildren(QGroupBox):
        _fit_group_title(group)
    _flow_button_rows(root)


__all__ = ['FlowLayout', 'full_title', 'NavButton', 'WrapCheckBox', 'responsive']
