"""Briefing completion, Windows inventory bounds and exact command ownership."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QTimer
from jarvis.runtime.briefing_stream import completion_text, empty_section_text
from tests.test_neo158_briefing import session, BriefingWindow, close_window, permit, qt_app, pump


@pytest.mark.parametrize('code', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_empty_bluetooth_and_device_lists_have_values_and_terminal_message(session, monkeypatch, code):
    from jarvis.tools import integrations
    session.config.data['app']['language'] = code
    session.config.data['briefing'].update(include_devices=True, include_bluetooth=True)
    monkeypatch.setattr(integrations, 'list_present_devices', lambda *_: [])
    monkeypatch.setattr(integrations, 'list_bluetooth_devices', lambda **_: [])
    permit(session)
    result = session.agent.tools.execute('briefing', {'action': 'startup_summary'})
    assert result.success and not result.data['partial']
    assert empty_section_text('bluetooth', code) in result.text
    assert empty_section_text('devices', code) in result.text
    assert result.text.endswith(completion_text(code))
    assert result.data['section_status']['bluetooth'] == 'ready'


def test_bluetooth_hang_does_not_hide_calendar_or_leave_waiting_qt_message(session, monkeypatch, caplog):
    from jarvis.tools import integrations
    import jarvis.runtime.briefing_stream as stream
    app = qt_app()
    release = threading.Event()
    entered = threading.Event()
    session.config.data['briefing']['include_bluetooth'] = True
    session.config.data['integrations']['google_calendar'].update(enabled=True, include_in_briefing=True)
    def bluetooth_read(**_):
        entered.set()
        release.wait(3)
        return [{'name': 'LATE-BLUETOOTH'}]
    monkeypatch.setattr(integrations, 'list_bluetooth_devices', bluetooth_read)
    monkeypatch.setattr(integrations.GoogleCalendarClient, 'today', lambda *_a, **_k: [])
    monkeypatch.setattr(integrations, 'format_calendar_events', lambda *_a, **_k: 'Календарь: событий нет.')
    original = stream.collect
    monkeypatch.setattr(stream, 'collect', lambda tasks, **_: original(tasks, timeout=.20))
    w = BriefingWindow(session)
    beats = []
    timer = QTimer(w)
    timer.timeout.connect(lambda: beats.append(time.monotonic()))
    timer.start(5)
    permit(session)
    caplog.set_level('INFO', logger='jarvis.tools.integrations')
    try:
        w._start_morning_briefing()
        pump(app, lambda: entered.is_set() and 'Календарь: событий нет.' in w.chat.items[0].text)
        bubble = w._briefing_bubble158
        assert 'Получаю информацию: Bluetooth' in bubble.text
        pump(app, lambda: not w._briefing_active)
        final = bubble.text
        assert len(beats) > 3
        assert len(w.chat.items) == 1 and w._briefing_bubble158 is bubble
        assert 'Bluetooth: данные сейчас недоступны.' in final
        assert 'Календарь: событий нет.' in final and 'Система: исправна' in final
        assert 'Получаю информацию' not in final and final.endswith(completion_text('ru', partial=True))
        assert not w._briefing_deadline159.isActive()
        assert 'section=bluetooth state=timeout' in caplog.text and 'Briefing finished:' in caplog.text
        release.set()
        app.processEvents()
        assert bubble.text == final
    finally:
        release.set()
        timer.stop()
        close_window(app, w)


def test_outer_startup_deadline_covers_registry_stall_and_drops_late_result(session, monkeypatch):
    app = qt_app()
    release = threading.Event()
    entered = threading.Event()
    exited = threading.Event()
    original = session.agent.tools.execute
    def stalled(*args, **kwargs):
        entered.set()
        try:
            release.wait(3)
            return original(*args, **kwargs)
        finally:
            exited.set()
    monkeypatch.setattr(session.agent.tools, 'execute', stalled)
    receipt = Mock()
    monkeypatch.setattr('jarvis.integrations.daily_briefing.DailyBriefingStore.mark_startup_delivered', receipt)
    w = BriefingWindow(session)
    permit(session)
    try:
        w._start_morning_briefing()
        from jarvis.runtime.briefing_stream import STARTUP_TIMEOUT_MS
        assert w._briefing_deadline159.interval() == STARTUP_TIMEOUT_MS
        w._briefing_deadline159.start(90)
        pump(app, lambda: entered.is_set() and not w._briefing_active)
        bubble = w._briefing_bubble158
        final = bubble.text
        assert 'Сегодня' in final and 'Сводка: данные сейчас недоступны.' in final
        assert w._briefing_cancel.is_set() and not w._briefing_deadline159.isActive()
        assert len(w.chat.items) == 1
        release.set()
        pump(app, lambda: exited.is_set())
        app.processEvents()
        assert bubble.text == final
        receipt.assert_not_called()
    finally:
        release.set()
        close_window(app, w)


def test_malformed_provider_does_not_abort_other_sections(session, monkeypatch):
    from jarvis.tools import integrations
    session.config.data['briefing']['include_weather'] = True
    session.config.data['integrations']['openweather'].update(enabled=True, city='Fixture')
    monkeypatch.setattr(integrations.OpenWeatherAdapter, 'answer', lambda *_a, **_k: 'broken provider')
    permit(session)
    result = session.agent.tools.execute('briefing', {'action': 'startup_summary'})
    assert result.success and result.data['section_status']['weather'] == 'error'
    assert result.data['weather_error']['error_type'] == 'InvalidSectionResult'
    assert 'Система: исправна' in result.text and 'Погода: данные сейчас недоступны.' in result.text
    assert result.text.endswith(completion_text('ru', partial=True))


def test_winrt_stalled_driver_is_cancelled_within_inventory_budget(monkeypatch):
    import jarvis.integrations.bluetooth_devices as bt
    from types import ModuleType
    started, cancelled = [], []
    class DeviceInformation:
        @staticmethod
        async def find_all_async(_selector):
            started.append(True)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
    device = SimpleNamespace(get_device_selector_from_pairing_state=lambda *_: 'paired',
                             get_device_selector_from_connection_status=lambda *_: 'connected')
    radio = ModuleType('winsdk.windows.devices.bluetooth')
    radio.BluetoothConnectionStatus = SimpleNamespace(CONNECTED=1)
    radio.BluetoothDevice = radio.BluetoothLEDevice = device
    enumeration = ModuleType('winsdk.windows.devices.enumeration')
    enumeration.DeviceInformation = DeviceInformation
    monkeypatch.setitem(sys.modules, radio.__name__, radio)
    monkeypatch.setitem(sys.modules, enumeration.__name__, enumeration)
    monkeypatch.setattr(bt, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(bt, '_WINRT_INVENTORY_TIMEOUT', .04)
    before = time.monotonic()
    assert bt._winrt_bluetooth_devices() == []
    assert time.monotonic() - before < .8 and started == cancelled == [True]


@pytest.mark.parametrize('empty', [True, False])
def test_bluetooth_cache_includes_empty_inventory_and_disconnected_devices(monkeypatch, empty):
    import jarvis.integrations.bluetooth_devices as bt
    monkeypatch.setattr(bt, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(bt, '_BT_CACHE', (0.0, []))
    monkeypatch.setattr(bt, '_winrt_bluetooth_devices', lambda: [])
    rows = [] if empty else [{'name': 'Offline headset', 'connected': False}, {'name': 'Online speaker', 'connected': True}]
    read = Mock(return_value=json.dumps(rows))
    monkeypatch.setattr(bt, '_powershell', read)
    connected = bt.list_bluetooth_devices(connected_only=True)
    all_rows = bt.list_bluetooth_devices()
    assert [x['name'] for x in connected] == ([] if empty else ['Online speaker'])
    assert {x['name'] for x in all_rows} == {x['name'] for x in rows}
    assert read.call_count == 1


ROUTE_CASES = [
    ('ru', 'сводка дня'), ('ru', 'покажи сводку дня'),
    ('ru', 'пожалуйста покажи мне сводку дня'), ('ru', 'можешь показать мне сводку дня'),
    ('ru', 'покажи утреннюю сводку'), ('ru', 'дай сводку на сегодня'),
    ('en', 'show me the daily briefing'), ('de', 'zeige mir die Tagesübersicht'),
    ('fr', 'montre moi le résumé du jour'), ('es', 'muéstrame el resumen del día'),
    ('it', 'mostrami il riepilogo del giorno'), ('pt', 'mostra o resumo do dia'),
]

@pytest.mark.parametrize('code, phrase', ROUTE_CASES)
def test_real_agent_briefing_forms_do_not_open_application(tmp_path, code, phrase):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        item = AtlasSession(tmp_path / 'routes', locale=code)
        try:
            item.agent.handle(phrase)
            assert len(item.calls) == 1
            assert item.calls[0]['tool'] == 'briefing'
            assert item.calls[0]['arguments'] == {'action': 'summary'}
        finally:
            item.finish()


@pytest.mark.parametrize('phrase, tool', [
    ('открой файл сводка дня.txt', 'filesystem'),
    ('покажи документ утренняя сводка.txt', 'desktop'),
    ('открой приложение сводка дня', 'application'),
    ('переведи show me the daily briefing на русский', 'translate'),
])
def test_briefing_words_in_literal_payload_keep_their_owner(tmp_path, phrase, tool):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        item = AtlasSession(tmp_path / 'payload')
        try:
            item.agent.handle(phrase)
            assert item.calls and item.calls[0]['tool'] == tool
            assert all(call['tool'] != 'briefing' for call in item.calls)
        finally:
            item.finish()
