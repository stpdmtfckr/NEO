"""Preserve real Qt conversation lifecycle after removing the avatar cloud."""
from __future__ import annotations

from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage, QPainter, QColor

from jarvis.ui.main_window import MainWindow
from jarvis.ui.chat_view import ChatView
from jarvis.ui.robot_widget import RobotWidget
from jarvis.ui.reply_activity159 import briefing_reply_activity
from tests.test_neo157_integration_review import reviewed_agent, full_voice_window, qt_app
from tests.test_neo158_robot import render, settled


@pytest.fixture
def window(reviewed_agent):
    app = qt_app()
    w = full_voice_window(reviewed_agent)
    w.robot = RobotWidget()
    w.robot.resize(520, 300)
    w.chat = ChatView()
    w._pending_response_bubble = w.chat.add_assistant_pending('Думаю')
    w._agent_generation = 7
    w._stream_thinking_text = ''
    w._response_briefing158 = None
    w._typing_timer = QTimer(w)
    w._typing_text = ''
    w._typing_index = 0
    w._typing_stream_live = w._typing_stream_complete = False
    w._wake_generation = 0
    w.audio_capture = SimpleNamespace(is_recording=False)
    w._set_busy = lambda busy, status, state: (setattr(w, '_busy', busy), w.robot.set_state(state))
    w.robot.set_state('thinking')
    for name in ('_start_typewriter', '_typewriter_tick', '_finish_typewriter',
                 '_handle_error', '_interrupt_current_operation', '_ambient_typewriter_tick'):
        setattr(w, name, MethodType(getattr(MainWindow, name), w))
    yield w
    w._typing_timer.stop()
    w._stream_flush_timer.stop()
    w._cancel_command_asr157()
    w.thread_pool.waitForDone(3000)
    w.chat.close(); w.chat.deleteLater()
    w.robot.close(); w.robot.deleteLater()
    w.deleteLater(); app.processEvents()


def event(w, kind, text, **kwargs):
    w._stream_response_event(dict(type=kind, text=text, generation=w._agent_generation, **kwargs))


def test_thinking_keeps_gear_with_inert_legacy_activity_hook(window):
    robot = window.robot
    robot.set_reply_activity(True)
    assert robot.state() not in {"typing", "speaking"}
    event(window, 'thinking', 'Проверяю информацию')
    assert robot.state() == 'thinking'
    assert robot.state() not in {"typing", "speaking"}
    image = QImage(520, 300, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    # NEO 180: «thinking» is the orbiting lights effect (state_fx180.thinking), every frame.
    from jarvis.ui import state_fx180
    thinking, cards = state_fx180.thinking, robot._draw_work_cards
    state_fx180.thinking, robot._draw_work_cards = Mock(), Mock()
    try:
        for elapsed in (1, 10, 21, 99):
            robot._elapsed = elapsed
            robot._draw_state_effect(painter, 200, 140, 1, QColor('cyan'))
        assert state_fx180.thinking.call_count == 4
        assert not robot._draw_work_cards.called
    finally:
        state_fx180.thinking, robot._draw_work_cards = thinking, cards
        painter.end()


def test_first_answer_chunk_replaces_thinking_in_same_bubble_and_animates_face(window):
    original = window._pending_response_bubble
    assert window.robot.state() not in {"typing", "speaking"}
    event(window, 'content', 'Вот ответ')
    assert window.robot.state() in {"typing", "speaking"} and window.robot.state() == 'typing'
    assert window._pending_response_bubble is original
    assert original.text == 'Вот ответ'
    assert len(window.chat.items) == 1
    # A provider can interleave internal work and later answer chunks.
    event(window, 'thinking', 'Проверяю продолжение')
    assert window.robot.state() not in {"typing", "speaking"}
    event(window, 'content', ', продолжение.')
    assert window.robot.state() in {"typing", "speaking"}
    window._finish_response_ui('idle')
    assert window.robot.state() not in {"typing", "speaking"}


@pytest.mark.parametrize('ending', ['cancel', 'error', 'finish'])
def test_reply_pose_ends_at_actual_response_end(window, ending):
    event(window, 'content', 'Уже готовая часть')
    settled(window.robot)
    assert window.robot.state() in {"typing", "speaking"}
    if ending == 'cancel':
        window._interrupt_current_operation(source='test')
    elif ending == 'error':
        window._handle_error('TestError: unavailable')
    else:
        window._finish_response_ui('idle')
    assert window.robot.state() not in {"typing", "speaking"}
    assert not hasattr(window.robot, "_draw_reply_bubble")
    # A stale chunk from the cancelled/completed generation cannot resurrect it.
    window._stream_response_event(dict(type='content', text='obsolete', generation=7))
    assert window.robot.state() not in {"typing", "speaking"}


def test_complete_nonstream_answer_keeps_typing_lifecycle(window):
    window.config.data['ui']['character_streaming_enabled'] = True
    original = window._pending_response_bubble
    window._start_typewriter('Готово', 'idle')
    assert window.robot.state() in {"typing", "speaking"}
    for _ in range(10):
        window._typewriter_tick()
    assert window.robot.state() not in {"typing", "speaking"}
    assert original.text == 'Готово'


def test_progressive_briefing_gear_while_waiting_typing_for_ready_section(window):
    original = window._pending_response_bubble
    event(window, 'briefing', 'Получаю информацию: Bluetooth', section='bluetooth', state='waiting')
    event(window, 'briefing', 'Получаю информацию: погода', section='weather', state='waiting')
    assert window.robot.state() == 'thinking' and window.robot.state() not in {"typing", "speaking"}
    event(window, 'briefing', 'Bluetooth: подключённых устройств нет', section='bluetooth', state='ready')
    assert window.robot.state() in {"typing", "speaking"}
    assert 'подключённых устройств нет' in original.text
    assert window.robot._reply_chunk_until159
    window.robot._expire_reply_chunk159(window.robot._reply_chunk_until159 + .01)
    assert window.robot.state() not in {"typing", "speaking"}
    assert 'подключённых устройств нет' in original.text
    window._finish_response_ui('idle')
    assert window.robot.state() not in {"typing", "speaking"}
    assert len(window.chat.items) == 1


def test_startup_ready_waiting_finish_and_late_section_cannot_override_user(window):
    window._busy = False
    window._briefing_user_generation158 = window._agent_generation
    window.robot.set_state('idle')
    briefing_reply_activity(window, 'waiting')
    assert window.robot.state() == 'thinking' and window.robot.state() not in {"typing", "speaking"}
    briefing_reply_activity(window, 'ready')
    assert window.robot.state() in {"typing", "speaking"}
    briefing_reply_activity(window, finished=True)
    assert window.robot.state() == 'idle' and window.robot.state() not in {"typing", "speaking"}
    window._agent_generation += 1
    window.robot.set_state('listening')
    briefing_reply_activity(window, 'ready')
    assert window.robot.state() == 'listening' and window.robot.state() not in {"typing", "speaking"}


@pytest.mark.parametrize("state", ["thinking", "typing", "speaking", "idle"])
def test_legacy_cloud_hook_cannot_change_rendering_or_retain_text(window, state):
    robot = window.robot
    robot.set_state(state)
    settled(robot)
    before = render(robot)
    robot.set_reply_activity(True, "This text belongs only in the chat")
    assert before == render(robot)
    robot.set_reply_activity(False)
    assert before == render(robot)
    assert not hasattr(robot, "_draw_reply_bubble")
    assert not any(isinstance(value, str) and "only in the chat" in value for value in vars(robot).values())


def test_ambient_answer_types_until_last_character_then_restores_idle(window):
    from collections import deque
    window._busy = False
    window._pending_response_bubble = None
    window.robot.set_state('idle')
    bubble = window.chat.add_assistant('')
    window._ambient_typing_queue = deque([(bubble, 'Готово: продолжаю музыку')])
    window._ambient_typewriter_tick()
    assert bubble.text and window.robot.state() in {"typing", "speaking"}
    for _ in range(30):
        window._ambient_typewriter_tick()
    assert bubble.text == 'Готово: продолжаю музыку'
    assert window.robot.state() == 'idle' and window.robot.state() not in {"typing", "speaking"}


def test_ambient_answer_cannot_replace_listening_or_sleep_animation(window):
    from collections import deque
    window._busy = False
    for state in ('listening', 'sleeping'):
        window.robot.set_state(state)
        window._ambient_typing_bubble = None
        window._ambient_typing_queue = deque([(window.chat.add_assistant(''), 'Уведомление')])
        window._ambient_typewriter_tick()
        assert window.robot.state() == state and window.robot.state() not in {"typing", "speaking"}
