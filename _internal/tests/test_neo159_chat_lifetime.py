"""Late UI callbacks cannot touch rows removed while a worker was running."""
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from jarvis.ui.chat_view import ChatView


@pytest.fixture
def app():
    value = QApplication.instance() or QApplication([])
    yield value
    value.processEvents()


def remove_deferred():
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def late_callbacks(chat, bubble):
    chat.update_bubble(bubble, 'late result')
    chat.update_thinking(bubble, 'late reasoning')
    chat.collapse_thinking(bubble)
    chat.move_bubble_to_bottom(bubble)
    chat.remove_bubble(bubble)


def test_queued_result_after_clear_does_not_touch_deleted_bubble(app):
    chat = ChatView()
    old = chat.add_assistant('Думаю')
    chat.clear_messages()
    current = chat.add_assistant('Новый запрос')
    remove_deferred()
    assert not isValid(old)
    completed = []

    def arrive():
        late_callbacks(chat, old)
        completed.append(True)

    QTimer.singleShot(0, arrive)
    app.processEvents()
    assert completed == [True]
    assert chat.items == [current.item] and current.text == 'Новый запрос'
    chat.deleteLater()


def test_evicted_row_is_ignored_before_and_after_qt_deletion(app):
    chat = ChatView()
    old = chat.add_assistant('Old result')
    for index in range(180):
        current = chat.add_assistant(str(index))
    assert isValid(old)
    late_callbacks(chat, old)
    assert old.text == 'Old result' and len(chat.items) == 180
    remove_deferred()
    assert not isValid(old)
    late_callbacks(chat, old)
    assert len(chat.items) == 180 and chat.items[-1] is current.item
    chat.deleteLater()


def test_foreign_and_closed_chat_callbacks_are_ignored_but_live_update_works(app):
    first, second = ChatView(), ChatView()
    bubble = first.add_assistant('One')
    late_callbacks(second, bubble)
    assert bubble.text == 'One' and first.items == [bubble.item]
    first.update_bubble(bubble, 'Two')
    assert bubble.text == 'Two' and first.items[0].text == 'Two'
    first.deleteLater()
    remove_deferred()
    assert not isValid(first)
    late_callbacks(first, bubble)
    second.deleteLater()
