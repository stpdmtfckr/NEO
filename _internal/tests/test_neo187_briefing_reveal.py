"""NEO 187 TZ-SUMMARY-01: the briefing types out at the chosen speed, never erasing."""
import os

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


@pytest.fixture(scope='module')
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class Cfg:
    def __init__(self, **values):
        self.values = {'ui.character_streaming_enabled': True, 'ui.typewriter_interval_ms': 16,
                       'ui.animation_fps': 60, **values}

    def get(self, key, default=None):
        return self.values.get(key, default)


def _pump(app, reveal, limit=4000):
    import time
    end = time.monotonic() + 5
    while reveal.active and time.monotonic() < end:
        app.processEvents()


def test_growing_document_keeps_prefix_and_types_the_rest(app):
    from jarvis.ui.briefing_reveal187 import GrowingReveal
    from jarvis.ui.chat_view import ChatView
    chat = ChatView()
    bubble = chat.add_assistant('')
    seen = []
    original = chat.update_bubble
    chat.update_bubble = lambda b, t: (seen.append(t), original(b, t))
    reveal = GrowingReveal(chat, Cfg())
    reveal.show(bubble, 'Сегодня вторник.\nПолучаю информацию…')
    assert reveal.active and bubble.text == ''                    # nothing dumped at once
    _pump(app, reveal)
    assert bubble.text == 'Сегодня вторник.\nПолучаю информацию…'
    assert all(len(b) >= len(a) for a, b in zip(seen, seen[1:]))   # monotonic growth
    seen.clear()
    reveal.show(bubble, 'Сегодня вторник.\nСистема: исправна')    # waiting line replaced
    _pump(app, reveal)
    assert bubble.text == 'Сегодня вторник.\nСистема: исправна'
    assert all(t.startswith('Сегодня вторник.') for t in seen)      # the earlier section is never erased
    chat.close()


def test_pacing_off_shows_the_whole_text_at_once(app):
    from jarvis.ui.briefing_reveal187 import GrowingReveal
    from jarvis.ui.chat_view import ChatView
    chat = ChatView()
    bubble = chat.add_assistant('')
    done = []
    reveal = GrowingReveal(chat, Cfg(**{'ui.character_streaming_enabled': False}))
    reveal.show(bubble, 'Сводка готова', on_done=lambda: done.append(True))
    assert bubble.text == 'Сводка готова' and done == [True] and not reveal.active
    chat.close()


def test_speed_follows_the_reply_setting(app):
    from jarvis.ui.briefing_reveal187 import GrowingReveal
    slow = GrowingReveal(None, Cfg(**{'ui.typewriter_interval_ms': 60}))._step()
    fast = GrowingReveal(None, Cfg(**{'ui.typewriter_interval_ms': 2}))._step()
    assert slow == (60, 1) and fast[1] > 1
