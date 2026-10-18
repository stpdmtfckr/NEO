"""Real Qt callbacks stay responsive while a provider queue is unavailable."""
import threading
import time
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

from jarvis.ui.media_queue158 import MediaQueuePreview


@pytest.fixture
def app():
    value = QApplication.instance() or QApplication([])
    yield value
    value.processEvents()


def pump(app, predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.002)
    assert predicate()


def test_slow_queue_keeps_qt_heartbeat_and_other_provider_responsive(app):
    gate = threading.Event()
    calls, completed, beats = [], [], []
    main = threading.get_ident()
    reader = MediaQueuePreview(None, lambda source, identity: completed.append((source, identity, threading.get_ident())))
    timer = QTimer(); timer.setInterval(5); timer.timeout.connect(lambda: beats.append(True)); timer.start()
    def blocked():
        calls.append(threading.get_ident()); gate.wait(3); return 'discord queue'
    try:
        assert reader.request('discord', ('D',), blocked) is None
        for _ in range(100):
            reader.request('discord', ('D',), blocked)
        reader.request('yandex_music', ('A',), lambda: 'next track')
        pump(app, lambda: len(beats) >= 5 and bool(completed))
        assert not gate.is_set() and len(calls) == 1 and calls[0] != main
        assert completed == [('yandex_music', ('A',), main)]
        assert reader.request('yandex_music', ('A',), lambda: pytest.fail('cached read called provider')) == 'next track'
    finally:
        gate.set(); timer.stop(); reader.close()


def test_late_queue_cannot_overwrite_new_track_or_same_track_new_generation(app):
    gate = threading.Event(); completed = []
    reader = MediaQueuePreview(None, lambda source, identity: completed.append(identity))
    try:
        reader.request('yandex_music', ('A', 1), lambda: (gate.wait(3), 'old')[1])
        reader.request('yandex_music', ('B', 2), lambda: 'intermediate')
        reader.request('yandex_music', ('A', 3), lambda: 'new')
        gate.set()
        pump(app, lambda: completed == [('A', 3)])
        assert reader.request('yandex_music', ('A', 3), lambda: 'wrong') == 'new'
    finally:
        gate.set(); reader.close()


def test_refill_invalidates_inflight_snapshot_and_close_drops_completion(app):
    gate = threading.Event(); completed = []
    reader = MediaQueuePreview(None, lambda *args: completed.append(args))
    try:
        reader.request('yandex_music', ('A',), lambda: (gate.wait(3), 'before refill')[1])
        reader.invalidate('yandex_music')
        reader.request('yandex_music', ('A',), lambda: 'after refill')
        gate.set(); pump(app, lambda: bool(completed))
        assert len(completed) == 1
        assert reader.request('yandex_music', ('A',), lambda: '') == 'after refill'
        reader.invalidate('yandex_music')
        gate.clear(); reader.request('yandex_music', ('A',), lambda: (gate.wait(3), 'late')[1])
        reader.close(); gate.set()
        for _ in range(20): app.processEvents(); time.sleep(.002)
        assert len(completed) == 1 and not reader._timer.isActive()
    finally:
        gate.set(); reader.close()


def test_actual_mainwindow_preview_returns_before_tool_and_updates_same_bubble(app, monkeypatch):
    from jarvis.ui.main_window import MainWindow
    from jarvis.ui.chat_view import ChatView
    from jarvis.runtime import media_events as bus
    monkeypatch.setattr(bus, '_LATEST_BY_SOURCE', {})
    gate = threading.Event(); called = threading.Event(); ids = []
    owner = QObject(); owner._quitting = False; owner._language_code = lambda: 'ru'
    owner._media_bubbles157 = {}; owner.chat = ChatView(); owner._quick_media_server = lambda: ''
    def execute(tool, args):
        ids.append(threading.get_ident()); called.set(); gate.wait(3)
        return SimpleNamespace(success=True, data={'queue':[{'artist':'Singer', 'title':'Next'}]})
    owner.agent = SimpleNamespace(tools=SimpleNamespace(execute=execute))
    for name in ['_now_playing_queue_preview', '_media_queue_ready158', '_refresh_media_bubble_queue157']:
        setattr(owner, name, MethodType(getattr(MainWindow, name), owner))
    bus.publish_now_playing('yandex_music', 'Song', 'Singer', 'A', announce=False)
    current = bus.latest_now_playing('yandex_music')
    bubble = owner.chat.add_assistant('Song')
    owner._media_bubbles157['yandex_music'] = (current.event_id, current.key, bubble, 'Song')
    try:
        initial = owner._now_playing_queue_preview('yandex_music')
        assert called.wait(1) and not gate.is_set()
        assert 'Next' not in initial and ids == [ids[0]] and ids[0] != threading.get_ident()
        gate.set(); pump(app, lambda: 'Next' in bubble.text)
        assert len(owner.chat.items) == 1 and owner.chat.items[0] is bubble.item
    finally:
        gate.set(); owner._media_queue_reader158.close(); owner.chat.deleteLater()
        bus.drain_now_playing(1000)


def test_chat_stream_coalesces_scroll_and_does_not_relayout_unchanged_history(app, monkeypatch):
    from jarvis.ui.chat_view import ChatView
    chat = ChatView(); chat.resize(380, 400); chat.show(); app.processEvents()
    rows = [chat.add_assistant('A line ' * 30) for _ in range(20)]
    chat.scroll_to_bottom(force=True)
    pump(app, lambda: not chat._scroll_timer.isActive() and not chat._scroll_settle_timer.isActive())
    syncs = []
    for row in rows[:-1]:
        monkeypatch.setattr(row, '_sync_body_height', lambda: syncs.append(True))
    chat._update_bubble_widths(); chat._update_bubble_widths()
    assert not syncs
    # Incoming streaming follows the prior live edge, while one burst of
    # updates coalesces pending scrolling and leaves older row layouts alone.
    timer_ids = set()
    for i in range(100):
        chat.update_bubble(rows[-1], 'stream ' * (i + 1))
        if chat._scroll_timer.isActive():
            timer_ids.add(chat._scroll_timer.timerId())
    assert len(timer_ids) <= 1
    assert not syncs
    app.processEvents()
    pump(app, lambda: not chat._scroll_timer.isActive() and not chat._scroll_settle_timer.isActive())
    bar = chat.verticalScrollBar()
    assert bar.value() == bar.maximum()
    before = len(syncs); chat.update_bubble(rows[-1], rows[-1].text)
    assert len(syncs) == before
    bar.setValue(bar.maximum() // 2)
    scroll_before = bar.value()
    assert bar.maximum() - scroll_before > chat._bottom_threshold
    for i in range(100):
        chat.update_bubble(rows[-1], 'reading history ' * (i + 101))
    app.processEvents()
    pump(app, lambda: not chat._scroll_timer.isActive() and not chat._scroll_settle_timer.isActive())
    assert bar.value() == scroll_before
    assert not chat._follow_bottom
    assert not syncs
    chat.close(); chat.deleteLater()


@pytest.mark.parametrize('changed,value,limit', [('language', 'en', 5), ('server', 'Server B', 5), ('limit', 1, 1)])
def test_actual_preview_cache_includes_language_server_and_visible_limit(app, monkeypatch, changed, value, limit):
    from jarvis.ui.main_window import MainWindow
    from jarvis.runtime import media_events as bus
    monkeypatch.setattr(bus, '_LATEST_BY_SOURCE', {})
    context = {'language': 'ru', 'server': 'Server A'}
    calls, refreshed = [], []
    owner = QObject()
    owner._quitting = False
    owner._language_code = lambda: context['language']
    owner._quick_media_server = lambda: context['server']
    owner._refresh_media_bubble_queue157 = lambda current: refreshed.append(current)

    def execute(tool, args):
        calls.append(args.copy())
        return SimpleNamespace(success=True, data={'queue': [
            {'title': args['server'] + ' song ' + str(i)} for i in range(1, 6)
        ]})

    owner.agent = SimpleNamespace(tools=SimpleNamespace(execute=execute))
    for name in ['_now_playing_queue_preview', '_media_queue_ready158']:
        setattr(owner, name, MethodType(getattr(MainWindow, name), owner))
    bus.publish_now_playing('discord', 'Current', 'Singer', 'A', announce=False)
    try:
        owner._now_playing_queue_preview('discord')
        pump(app, lambda: len(refreshed) == 1)
        old = owner._now_playing_queue_preview('discord')
        assert 'Дальше в очереди:' in old and 'Server A song 5' in old
        if changed != 'limit':
            context[changed] = value
        immediate = owner._now_playing_queue_preview('discord', limit=limit)
        assert 'Server A song' not in immediate  # Old rendered queue is not reused.
        pump(app, lambda: len(refreshed) == 2)
        current = owner._now_playing_queue_preview('discord', limit=limit)
        assert len(calls) == 2
        assert calls[-1]['server'] == context['server']
        assert ('Up next:' if context['language'] == 'en' else 'Дальше в очереди:') in current
        assert context['server'] + ' song 1' in current
        assert ('song 5' in current) == (limit == 5)
    finally:
        owner._media_queue_reader158.close()
        bus.drain_now_playing(1000)


@pytest.mark.parametrize('changed,value', [('language', 'en'), ('server', 'Server B')])
def test_queue_completion_after_settings_change_cannot_update_bubble(app, monkeypatch, changed, value):
    from jarvis.ui.main_window import MainWindow
    from jarvis.runtime import media_events as bus
    monkeypatch.setattr(bus, '_LATEST_BY_SOURCE', {})
    context = {'language': 'ru', 'server': 'Server A'}
    gate = threading.Event()
    refreshed = []
    owner = QObject()
    owner._quitting = False
    owner._language_code = lambda: context['language']
    owner._quick_media_server = lambda: context['server']
    owner._refresh_media_bubble_queue157 = lambda current: refreshed.append(current)

    def execute(tool, args):
        gate.wait(3)
        return SimpleNamespace(success=True, data={'queue': [{'title': args['server'] + ' song'}]})

    owner.agent = SimpleNamespace(tools=SimpleNamespace(execute=execute))
    for name in ['_now_playing_queue_preview', '_media_queue_ready158']:
        setattr(owner, name, MethodType(getattr(MainWindow, name), owner))
    bus.publish_now_playing('discord', 'Current', 'Singer', 'A', announce=False)
    try:
        owner._now_playing_queue_preview('discord')
        context[changed] = value
        gate.set()
        pump(app, lambda: not owner._media_queue_reader158._timer.isActive())
        assert refreshed == []  # Even the same track cannot accept old settings.
        owner._now_playing_queue_preview('discord')
        pump(app, lambda: len(refreshed) == 1)
        current = owner._now_playing_queue_preview('discord')
        assert context['server'] + ' song' in current
        assert ('Up next:' if context['language'] == 'en' else 'Дальше в очереди:') in current
    finally:
        gate.set()
        owner._media_queue_reader158.close()
        bus.drain_now_playing(1000)
