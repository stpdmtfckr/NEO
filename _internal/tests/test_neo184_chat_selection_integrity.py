"""Chat selection, clipboard fidelity, themed menus and menu disposal."""
import os
import re
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QWidget
from jarvis.ui.chat_view import ChatBodyLabel
from jarvis.ui.theme_presets import THEME_PRESETS

@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])

@pytest.fixture
def label(app):
    item = ChatBodyLabel()
    item.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
    item.show()
    yield item
    item.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

@pytest.mark.parametrize('value', [
    'Hello', 'Привет 😀 世界', '<b>literal</b>', 'one\ntwo', 'one\r\ntwo',
    'a\u200bb', 'https://example.com/' + 'x' * 180, '',
])
def test_plain_select_all_copies_exact_original(label, value):
    label.setTextFormat(Qt.PlainText)
    label.set_message(value, plain=True)
    QTest.keyClick(label, Qt.Key_A, Qt.ControlModifier)
    assert label.selectedText() == value
    if value:
        QTest.keyClick(label, Qt.Key_C, Qt.ControlModifier)
        assert QApplication.clipboard().text() == value

@pytest.mark.parametrize('value, expected', [
    ('<b>Hello</b>', 'Hello'),
    ('<p>A😀</p><p>B &amp; C</p>', 'A😀\nB & C'),
    ('<b>' + 'x' * 180 + '</b>', 'x' * 180),
    ('hello\nworld', 'hello\nworld'),
])
def test_rich_select_all_uses_rendered_length(label, value, expected):
    label.set_message(value, plain=False)
    label._select_all()
    assert label.selectedText() == expected
    assert label._copy_selection()
    assert QApplication.clipboard().text() == expected

def test_partial_selection_preserves_newlines(label):
    label.setTextFormat(Qt.PlainText)
    label.set_message('first\nsecond\nthird', plain=True)
    label.setSelection(2, 10)
    assert label.selectedText() == 'rst\nsecond'

def test_context_select_all_and_repeated_disposal(label, app):
    label.set_message('<b>Hello</b>', plain=False)
    observations = []
    def finish_menu():
        menu = app.activePopupWidget()
        if isinstance(menu, QMenu):
            observations.append(len(menu.actions()))
            menu.actions()[-1].trigger()
            menu.close()
    for _ in range(12):
        QTimer.singleShot(0, finish_menu)
        label.contextMenuEvent(QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(2, 2), label.mapToGlobal(QPoint(2, 2))))
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert label.findChildren(QMenu) == []
    assert len(observations) == 12
    assert label.selectedText() == 'Hello'

def luminance(text):
    color = QColor(text)
    values = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4
              for v in (color.redF(), color.greenF(), color.blueF())]
    return sum(a * b for a, b in zip(values, (.2126, .7152, .0722)))

def contrast(a, b):
    low, high = sorted([luminance(a), luminance(b)])
    return (high + .05) / (low + .05)

@pytest.mark.parametrize('name', list(THEME_PRESETS))
def test_menu_colors_are_readable_for_each_theme(app, name):
    parent = QWidget()
    theme = THEME_PRESETS[name]['window']
    parent._theme_cache = {'dialog_bg': theme['panel_color'], 'text_color': theme['text_color'], 'accent': theme['accent']}
    label = ChatBodyLabel(parent)
    menu = QMenu(label)
    label._style_context_menu(menu)
    style = menu.styleSheet()
    colors = re.findall(r'background:(#[0-9a-f]+); color:(#[0-9a-f]+);', style)
    assert len(colors) == 2
    assert all(contrast(bg, fg) >= 4.5 for bg, fg in colors)
    assert colors[0][0].lower() == theme['panel_color'].lower()
    assert colors[1][0].lower() == theme['accent'].lower()
    parent.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
