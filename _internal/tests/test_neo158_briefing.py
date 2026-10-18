"""Real Qt delivery + real briefing handler, with inert device/network reads.

These tests do not claim authenticated integration or physical Windows coverage.
They assert visible message identity, receipt timing, cancellation and Qt liveness.
"""
from __future__ import annotations

from datetime import datetime
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QObject, QTimer
from jarvis.ui.main_window import MainWindow
from jarvis.ui.chat_view import ChatView
from jarvis.runtime.briefing_stream import BriefingDocument, collect, stream_to
from jarvis.integrations.daily_briefing import DailyBriefingStore
from tests.test_neo157_integration_review import qt_app, pump, full_voice_window


class BriefingWindow(QObject):
    _start_morning_briefing = MainWindow._start_morning_briefing
    _run_morning_briefing = MainWindow._run_morning_briefing
    _morning_briefing_part = MainWindow._morning_briefing_part
    _morning_briefing_failed = MainWindow._morning_briefing_failed
    _morning_briefing_ready = MainWindow._morning_briefing_ready
    _render_morning_briefing158 = MainWindow._render_morning_briefing158
    _cancel_morning_briefing158 = MainWindow._cancel_morning_briefing158
    _record_morning_briefing_delivery158 = MainWindow._record_morning_briefing_delivery158

    def __init__(self, session):
        super().__init__()
        self.config = session.config
        self.agent = session.agent
        self.chat = ChatView()
        self._quitting = self._busy = False
        self._agent_generation = 1
        self._language_code = lambda: 'ru'
        self._say = Mock()
        self._rearm_voice_after_answer = Mock()
        self.update_threads = []
        original = self.chat.update_bubble
        def update(bubble, text):
            self.update_threads.append(threading.get_ident())
            original(bubble, text)
        self.chat.update_bubble = update

    def _append_assistant(self, text, **kwargs):
        self.update_threads.append(threading.get_ident())
        return self.chat.add_assistant(text)


@pytest.fixture
def session(tmp_path, monkeypatch):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    from jarvis.runtime.cancellation import cancellation_manager
    cancellation_manager().reset()
    with external_io_blocked():
        item = AtlasSession(tmp_path / 'briefing_run')
        # NEO 187: these contracts check section identity/receipts, not the
        # typing cadence; TZ-SUMMARY-01 pacing has its own test (neo187).
        item.config.data.setdefault('ui', {})['character_streaming_enabled'] = False
        item.config.data['briefing'].update(
            enabled=True, show_on_startup=True, include_system=True,
            include_weather=False, include_devices=False, include_bluetooth=False,
            include_processes=False)
        for provider in ('gmail', 'mailru', 'google_calendar', 'telegram'):
            item.config.data['integrations'][provider]['enabled'] = False
            item.config.data['integrations'][provider]['include_in_briefing'] = False
        from jarvis.tools import integrations
        monkeypatch.setattr(integrations, 'snapshot', lambda: {'test_system': True})
        monkeypatch.setattr(integrations, 'format_snapshot', lambda *args: 'Система: исправна')
        yield item
        item.finish()


def permit(session, action='startup_summary'):
    session._permitted.append(('briefing', {'action': action}))


def close_window(app, window):
    window._cancel_morning_briefing158()
    window.chat.close()
    window.chat.deleteLater()
    window.deleteLater()
    app.processEvents()


def test_startup_real_qt_same_bubble_progress_and_visible_receipt(session, monkeypatch):
    from jarvis.tools import integrations
    app = qt_app()
    release = threading.Event()
    started = threading.Event()
    def system_read():
        started.set()
        release.wait(3)
        return {'test_system': True}
    monkeypatch.setattr(integrations, 'snapshot', system_read)
    receipts = []
    original_receipt = DailyBriefingStore.mark_startup_delivered
    def receipt(store, now=None):
        receipts.append(threading.get_ident())
        return original_receipt(store, now)
    monkeypatch.setattr(DailyBriefingStore, 'mark_startup_delivered', receipt)
    w = BriefingWindow(session)
    permit(session)
    main_thread = threading.get_ident()
    beats = []
    timer = QTimer(w)
    timer.setInterval(5)
    timer.timeout.connect(lambda: beats.append(time.monotonic()))
    timer.start()
    try:
        w._start_morning_briefing()
        pump(app, lambda: len(w.chat.items) == 1 and started.is_set())
        bubble = w._briefing_bubble158
        initial = bubble.text
        assert 'Сегодня' in initial and 'Получаю информацию' in initial
        assert not receipts
        state = json.loads((session.config.paths.root/'daily_briefing_state.json').read_text())
        assert 'last_startup_date' not in state
        pump(app, lambda: len(beats) >= 5)
        # Concurrent user content remains independent of the startup document.
        unrelated = w.chat.add_assistant('Ответ на другую команду')
        w._agent_generation += 1
        w._say.reset_mock()
        release.set()
        pump(app, lambda: not w._briefing_active and bool(receipts))
        assert w._briefing_bubble158 is bubble
        assert len(w.chat.items) == 2 and unrelated.text == 'Ответ на другую команду'
        assert 'Сегодня' in bubble.text and 'Система: исправна' in bubble.text
        assert 'Получаю информацию' not in bubble.text
        w._say.assert_not_called()
        assert set(w.update_threads) == {main_thread}
        assert receipts == [receipts[0]] and receipts[0] != main_thread
        before = len(session.calls)
        w._start_morning_briefing()
        app.processEvents()
        assert len(session.calls) == before, 'Duplicate startup timer reran the tool'
    finally:
        release.set()
        timer.stop()
        close_window(app, w)


def test_previous_same_day_delivery_does_not_hide_enabled_next_launch(session):
    app = qt_app()
    store = DailyBriefingStore(session.config)
    store.mark_startup_delivered(datetime.now())
    assert store.startup_already_delivered()
    w = BriefingWindow(session)
    permit(session)
    try:
        w._start_morning_briefing()
        pump(app, lambda: not w._briefing_active)
        assert len(w.chat.items) == 1
        assert 'Сегодня' in w.chat.items[0].text
        assert 'Система: исправна' in w.chat.items[0].text
        assert not session.executions[-1].data.get('suppressed')
    finally:
        close_window(app, w)


@pytest.mark.parametrize('setting', ['enabled', 'show_on_startup'])
def test_disabled_startup_has_no_tool_read_or_receipt(session, setting):
    app = qt_app()
    session.config.data['briefing'][setting] = False
    w = BriefingWindow(session)
    try:
        w._start_morning_briefing()
        app.processEvents()
        assert not session.calls and not w.chat.items
        assert not (session.config.paths.root/'daily_briefing_state.json').exists()
    finally:
        close_window(app, w)


def test_failed_network_and_deadline_keep_local_parts_in_one_bubble(session, monkeypatch):
    from jarvis.tools import integrations
    import jarvis.runtime.briefing_stream as stream
    app = qt_app()
    release = threading.Event()
    session.config.data['briefing'].update(include_weather=True, include_devices=True)
    session.config.data['integrations']['openweather'].update(enabled=True, city='Fixture')
    def broken_weather(*args, **kwargs):
        raise ConnectionError('https://example.invalid/?token=NEVER_SHOW_TOKEN')
    monkeypatch.setattr(integrations.OpenWeatherAdapter, 'answer', broken_weather)
    monkeypatch.setattr(integrations, 'list_present_devices', lambda *_: (release.wait(3), ['late'])[1])
    original_collect = stream.collect
    monkeypatch.setattr(stream, 'collect', lambda tasks, **kwargs: original_collect(tasks, timeout=.10))
    w = BriefingWindow(session)
    permit(session)
    try:
        w._start_morning_briefing()
        pump(app, lambda: not w._briefing_active)
        assert len(w.chat.items) == 1
        final = w.chat.items[0].text
        assert 'Сегодня' in final and 'Система: исправна' in final
        assert 'Погода: данные сейчас недоступны' in final
        assert 'Устройства: данные сейчас недоступны' in final
        assert 'Получаю информацию' not in final and 'NEVER_SHOW_TOKEN' not in final
        assert session.executions[-1].data['section_status']['devices'] == 'timeout'
        release.set()
        app.processEvents()
        assert w.chat.items[0].text == final
    finally:
        release.set()
        close_window(app, w)


def test_cancelled_briefing_discards_late_events_and_never_records_receipt(session, monkeypatch):
    from jarvis.tools import integrations
    app = qt_app()
    release = threading.Event()
    monkeypatch.setattr(integrations, 'snapshot', lambda: (release.wait(3), {})[1])
    receipt = Mock()
    monkeypatch.setattr(DailyBriefingStore, 'mark_startup_delivered', receipt)
    w = BriefingWindow(session)
    permit(session)
    try:
        w._start_morning_briefing()
        pump(app, lambda: bool(w.chat.items) and 'Получаю информацию' in w.chat.items[0].text)
        generation = w._briefing_generation
        bubble = w._briefing_bubble158
        w._cancel_morning_briefing158()
        final = bubble.text
        assert 'Получаю информацию' not in final
        w._morning_briefing_part({'generation': generation, 'section':'system', 'text':'OBSOLETE', 'state':'ready'})
        release.set()
        pump(app, lambda: bool(session.executions))
        assert bubble.text == final and 'OBSOLETE' not in final
        assert len(w.chat.items) == 1
        receipt.assert_not_called()
    finally:
        release.set()
        close_window(app, w)


def test_tool_execution_without_gui_cannot_mark_visible_delivery(session):
    permit(session)
    result = session.agent.tools.execute('briefing', {'action':'startup_summary'})
    assert result.success and 'Сегодня' in result.text
    assert not DailyBriefingStore(session.config).startup_already_delivered()


def test_repeated_hung_reads_have_a_process_wide_thread_bound(monkeypatch):
    import jarvis.runtime.briefing_stream as stream
    # A private semaphore keeps this test isolated from other tests' live reads.
    monkeypatch.setattr(stream, '_READ_SLOTS', threading.BoundedSemaphore(2))
    release = threading.Event()
    finished = threading.Event()
    calls = []
    def blocked():
        calls.append(threading.get_ident())
        release.wait(3)
        if len(calls) == 2:
            finished.set()
        return 'late'
    try:
        first = list(collect({'a':blocked, 'b':blocked}, timeout=.03))
        second = list(collect({'a':blocked, 'b':blocked}, timeout=.03))
        assert all(row[1] == 'timeout' for row in first)
        assert all(row[1] == 'error' for row in second)
        assert len(calls) == 2
    finally:
        release.set()
        assert finished.wait(1)


def test_pre_cancelled_briefing_starts_no_reads():
    cancel = threading.Event()
    cancel.set()
    read = Mock()
    with stream_to(lambda _: None, cancel):
        assert list(collect({'read':read})) == [('read','cancelled',None)]
    read.assert_not_called()


def test_document_keeps_completed_text_before_pending_sections():
    doc = BriefingDocument('ru')
    doc.update('greeting','Сегодня хороший день')
    doc.update('weather','Получаю информацию: Погода…','waiting')
    doc.update('system','Получаю информацию: Система…','waiting')
    doc.update('system','Система: исправна')
    assert doc.render().index('Система: исправна') < doc.render().index('Получаю информацию')
    assert not doc.update('system','stale placeholder','waiting')
    doc.finish()
    assert 'Получаю информацию' not in doc.render()
    assert not doc.update('weather','late result')


@pytest.mark.parametrize('code', ['ru','en','de','fr','es','it','pt'])
def test_cancelled_location_section_has_no_briefing_completion_claim(code):
    from jarvis.runtime.briefing_stream import section_name, status_text
    doc = BriefingDocument(code)
    doc.update('locations.paths', 'C:/Games/Example/game.exe')
    doc.update('locations.search', status_text('locations.search', code), 'waiting')
    doc.finish(cancelled=True)
    error = doc.sections['locations.search'][0]
    expected = {
        'ru': 'данные сейчас недоступны.', 'en': 'data unavailable.',
        'de': 'Daten nicht verfügbar.', 'fr': 'données indisponibles.',
        'es': 'datos no disponibles.', 'it': 'dati non disponibili.',
        'pt': 'dados indisponíveis.',
    }
    separator = ' : ' if code == 'fr' else ': '
    assert error == section_name('locations.search', code) + separator + expected[code]
    assert 'C:/Games/Example/game.exe' in doc.render()


def test_on_demand_real_agent_stream_preserves_one_qt_response_row(session, monkeypatch):
    from jarvis.tools import integrations
    app = qt_app()
    release = threading.Event()
    monkeypatch.setattr(integrations, 'snapshot', lambda: (release.wait(3), {})[1])
    w = full_voice_window(session)
    w.chat = ChatView()
    w._append_user = w.chat.add_user
    permit(session, 'summary')
    updates = []
    original_update = w.chat.update_bubble
    w.chat.update_bubble = lambda row, text: (updates.append((row,text)), original_update(row,text))[-1]
    try:
        w._send_text('сводка дня')
        bubble = w._pending_response_bubble
        pump(app, lambda: 'Получаю информацию' in bubble.text, seconds=10)
        assert 'Сегодня' in bubble.text
        release.set()
        pump(app, lambda: not w._busy, seconds=10)
        assert len([item for item in w.chat.items if item.kind == 'assistant']) == 1
        assert all(row is bubble for row, _ in updates)
        first_content = next(i for i, (_, text) in enumerate(updates) if 'Сегодня' in text)
        assert all(text for _, text in updates[first_content:]), 'Briefing was erased for retyping'
        assert 'Система: исправна' in bubble.text and 'Получаю информацию' not in bubble.text
        assert not DailyBriefingStore(session.config).startup_already_delivered()
    finally:
        release.set()
        w.thread_pool.waitForDone(3000)
        w.chat.deleteLater()
        w.deleteLater()
        app.processEvents()


@pytest.mark.parametrize('source', ['text','voice'])
def test_thinking_to_real_nonstream_reply_keeps_qt_bubble_identity(session, source):
    app = qt_app()
    w = full_voice_window(session)
    w.chat = ChatView()
    w._append_user = w.chat.add_user
    w._listening_bubble = w.chat.add_assistant('Думаю') if source == 'voice' else None
    original = w._listening_bubble
    try:
        w._send_text('привет', input_source=source)
        bubble = w._pending_response_bubble
        if source == 'voice':
            assert bubble is original
        pump(app, lambda: not w._busy, seconds=10)
        assert len([item for item in w.chat.items if item.kind == 'assistant']) == 1
        assert bubble.item in w.chat.items and bubble.text != 'Думаю'
        assert bubble.text.strip()
    finally:
        w.thread_pool.waitForDone(3000)
        w.chat.deleteLater()
        w.deleteLater()
        app.processEvents()


def test_startup_handler_failure_still_displays_local_date_once(session, monkeypatch):
    app = qt_app()
    session.agent.tools.execute = Mock(side_effect=RuntimeError('read failed'))
    receipt = Mock()
    monkeypatch.setattr(DailyBriefingStore, 'mark_startup_delivered', receipt)
    w = BriefingWindow(session)
    try:
        w._start_morning_briefing()
        # The pure local section is visible before processing a worker callback.
        bubble = w._briefing_bubble158
        assert 'Сегодня' in bubble.text
        pump(app, lambda: not w._briefing_active)
        assert len(w.chat.items) == 1 and w._briefing_bubble158 is bubble
        assert 'Сегодня' in bubble.text and 'Сводка: данные сейчас недоступны' in bubble.text
        receipt.assert_not_called()
    finally:
        close_window(app, w)


def test_streaming_answer_updates_existing_qt_thinking_bubble(session):
    from jarvis.agent_types import AgentTurn
    app = qt_app()
    release = threading.Event()
    def stream(text, *, on_text, **kwargs):
        on_text('Первая часть. ')
        release.wait(3)
        on_text('Вторая часть.')
        return AgentTurn('Первая часть. Вторая часть.')
    session.agent.handle_stream = stream
    w = full_voice_window(session)
    w.chat = ChatView()
    w._append_user = w.chat.add_user
    try:
        w._send_text('расскажи подробнее')
        bubble = w._pending_response_bubble
        pump(app, lambda: 'Первая часть.' in bubble.text)
        assert 'Дума' not in bubble.text
        release.set()
        pump(app, lambda: not w._busy)
        assert len([item for item in w.chat.items if item.kind == 'assistant']) == 1
        assert bubble.text == 'Первая часть. Вторая часть.'
    finally:
        release.set()
        w.thread_pool.waitForDone(3000)
        w.chat.deleteLater()
        w.deleteLater()
        app.processEvents()


def test_worker_error_replaces_thinking_in_the_same_qt_bubble(session):
    from types import MethodType
    app = qt_app()
    session.agent.handle_stream = Mock(side_effect=RuntimeError('ControlledFailure'))
    w = full_voice_window(session)
    w.chat = ChatView()
    w._append_user = w.chat.add_user
    w._handle_error = MethodType(MainWindow._handle_error, w)
    w._recover_media_notice157 = MethodType(MainWindow._recover_media_notice157, w)
    w._typing_timer = QTimer(w)
    try:
        w._send_text('привет')
        bubble = w._pending_response_bubble
        pump(app, lambda: not w._busy)
        assert len([item for item in w.chat.items if item.kind == 'assistant']) == 1
        assert 'ControlledFailure' in bubble.text and 'Дума' not in bubble.text
        assert not w._stream_flush_timer.isActive()
        assert w._pending_response_bubble is None
    finally:
        w.thread_pool.waitForDone(3000)
        w.chat.deleteLater()
        w.deleteLater()
        app.processEvents()


@pytest.mark.parametrize('visible,streamed', [('Думаю', False), ('Уже показанная часть', True), ('Напечатанная часть', False)])
def test_cancel_keeps_qt_response_identity_and_discards_late_result(session, visible, streamed):
    from types import MethodType
    from jarvis.agent_types import AgentTurn
    from jarvis.runtime.answer_stop179 import STOPPED
    app = qt_app()
    w = full_voice_window(session)
    w.chat = ChatView()
    w._append_assistant = lambda text, **kw: w.chat.add_assistant(text)
    w._interrupt_current_operation = MethodType(MainWindow._interrupt_current_operation, w)
    w._recover_media_notice157 = MethodType(MainWindow._recover_media_notice157, w)
    w._wake_generation = 1
    w.audio_capture = SimpleNamespace(is_recording=False)
    w._is_interrupt_phrase = lambda text: True
    w._normalize_recognized_command = lambda text: text
    w._ambient_typing_queue = []
    w._ambient_typing_bubble = None
    w._typing_timer = QTimer(w)
    w._stream_received = streamed
    w._typing_index = len(visible) if visible == 'Напечатанная часть' else 0
    w._stream_pending_text = 'NEVER_APPEND_HIDDEN_DELTA'
    w._pending_response_bubble = w.chat.add_assistant(visible)
    bubble = w._pending_response_bubble
    generation = w._agent_generation
    try:
        w._interrupt_current_operation(source='voice-stop')
        assert len(w.chat.items) == 1
        assert w._pending_response_bubble is None
        if streamed or visible == 'Напечатанная часть':
            assert bubble.text == visible + '\n\n' + STOPPED['ru']
        else:
            assert bubble.text == STOPPED['ru']
        final = bubble.text
        w._handle_agent_result((generation, AgentTurn('OBSOLETE RESULT')))
        w._stream_response_event({'generation':generation, 'type':'content', 'text':'OBSOLETE CHUNK'})
        assert bubble.text == final and 'NEVER_APPEND_HIDDEN_DELTA' not in final
    finally:
        w.chat.deleteLater()
        w.deleteLater()
        app.processEvents()
