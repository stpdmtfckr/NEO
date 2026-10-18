"""Actual Qt/briefing-tool retries; no authenticated services are contacted."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
pytest.importorskip('PySide6')
from PySide6.QtCore import QTimer
from jarvis.runtime import briefing_stream as stream
from tests.test_neo158_briefing import session, BriefingWindow, close_window, permit, qt_app, pump


@pytest.fixture(autouse=True)
def short_pauses(monkeypatch):
    monkeypatch.setattr(stream, 'RETRY_DELAYS', (.06, .08))


def weather(session, monkeypatch, read):
    from jarvis.tools import integrations
    session.config.data['briefing']['include_weather'] = True
    session.config.data['integrations']['openweather'].update(enabled=True, city='Fixture')
    monkeypatch.setattr(integrations.OpenWeatherAdapter, 'answer', read)


def test_qt_transient_weather_retries_independently_and_keeps_one_bubble(session, monkeypatch):
    calls = []
    def read(*_a, **_k):
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise ConnectionError('https://fixture.invalid/?token=PRIVATE')
        return 'Погода: ясно.', {'fixture': True}
    weather(session, monkeypatch, read)
    app, w = qt_app(), BriefingWindow(session)
    beats = []
    timer = QTimer(w)
    timer.timeout.connect(lambda: beats.append(time.monotonic()))
    timer.start(5)
    permit(session)
    try:
        w._start_morning_briefing()
        pump(app, lambda: 'Повторная попытка 2 из 3' in w.chat.items[0].text)
        bubble = w._briefing_bubble158
        assert 'Система: исправна' in bubble.text
        assert 'данные сейчас недоступны' not in bubble.text
        pump(app, lambda: not w._briefing_active)
        assert len(calls) == 2 and calls[1] - calls[0] >= .05
        assert len(beats) >= 3 and len(w.chat.items) == 1
        assert w._briefing_bubble158 is bubble and 'Погода: ясно.' in bubble.text
        assert 'Повторная попытка' not in bubble.text and 'PRIVATE' not in bubble.text
        result = session.executions[-1]
        assert result.data['section_attempts'] == {'weather': 2, 'system': 1}
        assert not result.data['partial']
    finally:
        timer.stop()
        close_window(app, w)


def test_three_failed_attempts_have_pauses_and_only_one_terminal_error(session, monkeypatch, caplog):
    calls = []
    def read(*_a, **_k):
        calls.append(time.monotonic())
        raise TimeoutError('token=PRIVATE')
    weather(session, monkeypatch, read)
    app, w = qt_app(), BriefingWindow(session)
    permit(session)
    caplog.set_level('INFO', logger='jarvis.tools.integrations')
    try:
        w._start_morning_briefing()
        pump(app, lambda: 'Повторная попытка 3 из 3' in w.chat.items[0].text)
        assert len(calls) == 2 and 'данные сейчас недоступны' not in w.chat.items[0].text
        pump(app, lambda: not w._briefing_active)
        assert len(calls) == 3
        assert calls[1] - calls[0] >= .05 and calls[2] - calls[1] >= .07
        final = w.chat.items[0].text
        assert final.count('Погода: данные сейчас недоступны.') == 1
        assert 'Система: исправна' in final and 'Получаю информацию' not in final
        assert len(w.chat.items) == 1
        assert session.executions[-1].data['weather_error']['attempts'] == 3
        logs = [r.message for r in caplog.records if 'Briefing section attempt:' in r.message and 'section=weather' in r.message]
        assert sum('state=started' in x for x in logs) == 3
        assert sum('state=error' in x for x in logs) == 3
        assert 'PRIVATE' not in caplog.text
    finally:
        close_window(app, w)


def test_cancel_between_attempts_starts_no_second_read_and_drops_stale_result(session, monkeypatch):
    read = Mock(side_effect=ConnectionError('offline'))
    weather(session, monkeypatch, read)
    monkeypatch.setattr(stream, 'RETRY_DELAYS', (.25, .25))
    app, w = qt_app(), BriefingWindow(session)
    permit(session)
    try:
        w._start_morning_briefing()
        pump(app, lambda: 'Повторная попытка 2 из 3' in w.chat.items[0].text)
        bubble, generation = w._briefing_bubble158, w._briefing_generation
        w._cancel_morning_briefing158()
        final = bubble.text
        pump(app, lambda: bool(session.executions))
        w._morning_briefing_part({'generation': generation, 'section': 'weather', 'state': 'ready', 'text': 'STALE'})
        assert read.call_count == 1 and bubble.text == final
        assert session.executions[-1].data['cancelled']
        assert len(w.chat.items) == 1
    finally:
        close_window(app, w)


def test_hung_provider_does_not_overlap_across_runs_or_overwrite_new_generation(session, monkeypatch):
    from jarvis.tools import integrations
    release, entered, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def read():
        calls.append(threading.get_ident())
        if len(calls) == 1:
            entered.set()
            release.wait(3)
            finished.set()
            return {'label': 'OLD'}
        return {'label': 'NEW'}
    monkeypatch.setattr(integrations, 'snapshot', read)
    monkeypatch.setattr(integrations, 'format_snapshot', lambda value, *_: value['label'])
    app, w = qt_app(), BriefingWindow(session)
    permit(session)
    try:
        w._start_morning_briefing()
        pump(app, lambda: entered.is_set())
        old_bubble = w._briefing_bubble158
        w._cancel_morning_briefing158()
        old_final = old_bubble.text
        w._briefing_started158 = False
        permit(session)
        w._start_morning_briefing()
        new_bubble = w._briefing_bubble158
        assert new_bubble is not old_bubble
        until = time.monotonic() + .1
        pump(app, lambda: time.monotonic() >= until)
        assert len(calls) == 1, 'A hung read was overlapped by a replacement briefing'
        release.set()
        pump(app, lambda: finished.is_set() and not w._briefing_active)
        assert len(calls) == 2 and 'NEW' in new_bubble.text and 'OLD' not in new_bubble.text
        assert old_bubble.text == old_final and len(w.chat.items) == 2
    finally:
        release.set()
        close_window(app, w)


def test_hung_provider_has_no_retry_but_healthy_section_is_immediately_ready():
    release = threading.Event()
    read = Mock(side_effect=lambda: release.wait(2))
    try:
        rows = list(stream.collect({'hung': read, 'ready': lambda: 'READY'}, timeout=.12,
                                   max_attempts=3, scope=object()))
        assert rows[0] == ('ready', 'ready', 'READY')
        assert rows[-1][0:2] == ('hung', 'timeout') and read.call_count == 1
        assert rows[-1][2]['attempts'] == 1
    finally:
        release.set()


def test_nine_sections_use_at_most_eight_actual_reads_and_queued_section_eventually_runs(monkeypatch):
    monkeypatch.setattr(stream, '_READ_SLOTS', threading.BoundedSemaphore(2))
    lock = threading.Lock()
    state = {'active': 0, 'peak': 0, 'done': 0}
    def read():
        with lock:
            state['active'] += 1
            state['peak'] = max(state['peak'], state['active'])
        threading.Event().wait(.02)
        with lock:
            state['active'] -= 1
            state['done'] += 1
        return 'OK'
    rows = list(stream.collect({str(i): read for i in range(9)}, timeout=2,
                               max_attempts=3, scope=object()))
    assert len(rows) == 9 and all(row[1] == 'ready' for row in rows)
    assert state == {'active': 0, 'peak': 2, 'done': 9}


def test_two_mail_services_retry_only_failed_provider_and_keep_ready_mail(session, monkeypatch):
    from jarvis.integrations import mail_services
    from jarvis.tools import integrations
    calls = {'gmail': 0, 'mailru': 0}
    def client(_config, provider):
        def status(**_):
            calls[provider] += 1
            if provider == 'mailru' and calls[provider] < 3:
                raise ConnectionError('offline')
            return SimpleNamespace(compact=lambda: {'provider': provider, 'ok': True})
        return SimpleNamespace(configured=lambda: True, status=status)
    monkeypatch.setattr(mail_services, 'provider_client', client)
    monkeypatch.setattr(integrations, 'format_mail_briefing', lambda _c, p, _v: p + ': READY')
    for p in calls:
        session.config.data['integrations'][p].update(enabled=True, include_in_briefing=True)
    app, w = qt_app(), BriefingWindow(session)
    permit(session)
    try:
        w._start_morning_briefing()
        pump(app, lambda: 'Повторная попытка 3 из 3' in w.chat.items[0].text)
        assert 'gmail: READY' in w.chat.items[0].text
        pump(app, lambda: not w._briefing_active)
        assert calls == {'gmail': 1, 'mailru': 3} and len(w.chat.items) == 1
        assert 'mailru: READY' in w.chat.items[0].text
        assert set(session.executions[-1].data['mail']) == {'gmail', 'mailru'}
    finally:
        close_window(app, w)


@pytest.mark.parametrize('message', [
    'OpenWeather API key is not configured. Add it in Settings.',
    'OpenWeather: Invalid API key.', 'Google Calendar HTTP 401: invalid credentials',
    'Google Calendar не авторизован', 'Mail.ru не настроен. Укажите адрес и пароль',
    'Google Calendar: invalid_grant', '[AUTHENTICATIONFAILED] Invalid credentials',
])
def test_permanent_credentials_failure_is_not_retried_or_exposed(session, monkeypatch, message):
    read = Mock(side_effect=RuntimeError(message + ' PRIVATE'))
    weather(session, monkeypatch, read)
    permit(session)
    result = session.agent.tools.execute('briefing', {'action': 'startup_summary'})
    assert read.call_count == 1
    assert result.data['weather_error']['reason'] == 'configuration'
    assert 'проверьте подключение и авторизацию сервиса' in result.text
    assert 'PRIVATE' not in result.text and result.data['partial']


@pytest.mark.parametrize('code', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_retry_status_replaces_placeholder_and_is_localized(code):
    document = stream.BriefingDocument(code)
    document.update('weather', stream.status_text('weather', code), 'waiting')
    document.update('weather', stream.status_text('weather', code, attempt=2), 'waiting')
    document.update('system', 'READY')
    assert '2' in document.render() and '3' in document.render()
    document.update('weather', 'WEATHER READY')
    assert document.render().count('WEATHER READY') == 1
    assert stream.status_text('weather', code, attempt=2) not in document.render()
    assert stream.STARTUP_TIMEOUT_MS > stream.BRIEFING_TIMEOUT_SECONDS * 1000


def test_actual_mailru_partial_folder_failure_is_structured(monkeypatch):
    from jarvis.integrations.mail_services import MailRuClient, MailFolderStatus
    client = object.__new__(MailRuClient)
    client.account_email = 'fixture@example.invalid'
    monkeypatch.setattr(client, 'selected_folders', lambda: ['Inbox', 'Archive'])
    def folder(name):
        if name == 'Archive':
            raise TimeoutError('offline')
        return MailFolderStatus(name, total=4)
    monkeypatch.setattr(client, '_folder_status', folder)
    payload = client.status(include_latest=0).compact()
    assert payload['total'] == 4 and payload['partial']
    assert payload['unavailable_folders'] == ['Archive']
    assert payload['folders'][0]['name'] == 'Inbox'
    failure = stream.validate_section_result(('Four messages', payload))
    assert failure['retryable'] and failure['partial_result'][1] is payload


def test_partial_mail_kept_after_three_failed_reads(session, monkeypatch):
    from jarvis.integrations import mail_services
    from jarvis.tools import integrations
    session.config.data['integrations']['mailru'].update(enabled=True, include_in_briefing=True)
    value = {'provider': 'mailru', 'partial': True, 'unavailable_folders': ['Archive'], 'total': 4}
    read = Mock(return_value=SimpleNamespace(compact=lambda: value))
    monkeypatch.setattr(mail_services, 'provider_client', lambda *_: SimpleNamespace(configured=lambda: True, status=read))
    monkeypatch.setattr(integrations, 'format_mail_briefing', lambda *_: 'Inbox: четыре письма.')
    app, w = qt_app(), BriefingWindow(session)
    permit(session)
    try:
        w._start_morning_briefing()
        pump(app, lambda: 'Повторная попытка 3 из 3' in w.chat.items[0].text)
        assert 'Inbox: четыре письма.' in w.chat.items[0].text
        pump(app, lambda: not w._briefing_active)
        assert read.call_count == 3 and len(w.chat.items) == 1
        assert 'Inbox: четыре письма.' in w.chat.items[0].text
        assert 'Mail.ru: часть данных сейчас недоступна.' in w.chat.items[0].text
        result = session.executions[-1]
        assert result.data['partial'] and result.data['mail']['mailru']['total'] == 4
    finally:
        close_window(app, w)


def test_partial_mail_survives_following_hung_retry_deadline():
    release = threading.Event()
    calls = []
    partial = ('Inbox: four messages', {'partial': True, 'unavailable_folders': ['Archive']})
    def read():
        calls.append(True)
        if len(calls) == 1:
            return partial
        release.wait(2)
        return ('LATE', {})
    try:
        rows = list(stream.collect({'mail.mailru': read}, timeout=.12, retry_delays=(.01,),
                                   max_attempts=3, scope=object(), validate=stream.validate_section_result))
        assert len(calls) == 2 and rows[-1][1] == 'timeout'
        assert rows[-1][2]['partial_result'] == partial
    finally:
        release.set()


def test_per_attempt_clients_close_sessions_after_success_and_failure(session, monkeypatch):
    from jarvis.integrations import mail_services
    from jarvis.tools import integrations
    session.config.data['integrations']['gmail'].update(enabled=True, include_in_briefing=True)
    session.config.data['integrations']['google_calendar'].update(enabled=True, include_in_briefing=True)
    clients = {'mail': [], 'calendar': []}
    def make_client(kind):
        item = SimpleNamespace(_session=SimpleNamespace(close=Mock()), configured=lambda: True)
        clients[kind].append(item)
        def status(**_):
            if len(clients[kind]) == 1:
                raise TimeoutError('offline')
            return SimpleNamespace(compact=lambda: {'provider': 'gmail'}) if kind == 'mail' else []
        item.status = item.today = status
        return item
    monkeypatch.setattr(mail_services, 'provider_client', lambda *_: make_client('mail'))
    monkeypatch.setattr(integrations, 'GoogleCalendarClient', lambda *_: make_client('calendar'))
    monkeypatch.setattr(integrations, 'format_mail_briefing', lambda *_: 'MAIL')
    monkeypatch.setattr(integrations, 'format_calendar_events', lambda *_a, **_k: 'CALENDAR')
    permit(session)
    result = session.agent.tools.execute('briefing', {'action': 'startup_summary'})
    assert not result.data['partial']
    assert len(clients['mail']) == len(clients['calendar']) == 2
    for client in clients['mail'] + clients['calendar']:
        client._session.close.assert_called_once_with()


def test_cancel_partial_mail_between_retries_keeps_received_text_same_qt_bubble(session, monkeypatch):
    from jarvis.integrations import mail_services
    from jarvis.tools import integrations
    session.config.data['integrations']['mailru'].update(enabled=True, include_in_briefing=True)
    value = {'provider': 'mailru', 'partial': True, 'unavailable_folders': ['Archive'], 'total': 4}
    read = Mock(return_value=SimpleNamespace(compact=lambda: value))
    monkeypatch.setattr(stream, 'RETRY_DELAYS', (.25, .25))
    monkeypatch.setattr(mail_services, 'provider_client', lambda *_: SimpleNamespace(configured=lambda: True, status=read))
    monkeypatch.setattr(integrations, 'format_mail_briefing', lambda *_: 'Inbox: четыре письма.')
    app, w = qt_app(), BriefingWindow(session)
    permit(session)
    try:
        w._start_morning_briefing()
        pump(app, lambda: 'Повторная попытка 2 из 3' in w.chat.items[0].text)
        bubble = w._briefing_bubble158
        assert 'Inbox: четыре письма.' in bubble.text
        w._cancel_morning_briefing158()
        final = bubble.text
        pump(app, lambda: bool(session.executions))
        assert 'Inbox: четыре письма.' in final and 'часть данных сейчас недоступна.' in final
        assert 'Повторная попытка' not in final and 'Получаю информацию' not in final
        assert bubble.text == final and len(w.chat.items) == 1 and read.call_count == 1
    finally:
        close_window(app, w)


def test_partial_text_survives_user_requested_briefing_stream_cancellation(session):
    from tests.test_neo157_integration_review import full_voice_window
    from jarvis.ui.chat_view import ChatView
    app = qt_app()
    w = full_voice_window(session)
    w.chat = ChatView()
    try:
        w._pending_response_bubble = w.chat.add_assistant('Думаю')
        w._response_briefing158 = None
        w._stream_response_event({'type': 'briefing', 'generation': w._agent_generation,
            'section': 'mail.mailru', 'state': 'waiting',
            'text': 'Inbox: четыре письма.\nПолучаю информацию: Mail.ru…',
            'partial_text': 'Inbox: четыре письма.'})
        document = w._response_briefing158
        document.finish(cancelled=True)
        w.chat.update_bubble(w._pending_response_bubble, document.render())
        assert 'Inbox: четыре письма.' in w._pending_response_bubble.text
        assert 'часть данных сейчас недоступна' in w._pending_response_bubble.text
    finally:
        w.chat.close()
        w.chat.deleteLater()
        w.deleteLater()
        app.processEvents()
