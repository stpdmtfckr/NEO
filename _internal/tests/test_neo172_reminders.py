"""NEO 172: reminders keep their text, the list works, repeating reminders and scheduled commands."""
from __future__ import annotations

from datetime import datetime
import json
import time

import pytest

from jarvis.runtime import everyday172 as ev
from jarvis.runtime.timers import TimerManager, next_due, repeat_days


@pytest.mark.parametrize('text,label', [
    ('напоминание через 20 минут выпить воды', 'выпить воды'), ('напомни мне через час что надо позвонить маме', 'надо позвонить маме'),
    ('напомни завтра в 18:30 про встречу', 'встречу'), ('remind me in 10 minutes to call mom', 'call mom'),
    ('erinnere mich in 5 minuten an den tee', 'den tee'), ('rappelle-moi dans 10 minutes de sortir le linge', 'sortir le linge'),
])
def test_reminder_keeps_what_to_remind(text, label):
    from jarvis.tools.integrations import _timer_label
    assert _timer_label(text, 'reminder') == label


@pytest.mark.parametrize('text,schedule', [
    ('каждый день в 9 открывай почту', ev.Schedule(9, 0, 'daily', command='открой почту')),
    ('по будням в 8:30 напоминай выпить таблетки', ev.Schedule(8, 30, 'weekdays', reminder='выпить таблетки')),
    ('по выходным в 10 утра включай музыку', ev.Schedule(10, 0, 'weekends', command='включи музыку')),
    ('каждый понедельник в 7 вечера напоминай вынести мусор', ev.Schedule(19, 0, 'mon', reminder='вынести мусор')),
    ('по понедельникам и пятницам в 18:15 напоминай про спортзал', ev.Schedule(18, 15, 'mon,fri', reminder='спортзал')),
    ('every day at 9 remind me to stretch', ev.Schedule(9, 0, 'daily', reminder='stretch')),
    ('every weekday at 8:30 open mail', ev.Schedule(8, 30, 'weekdays', command='open mail')),
    ('jeden Tag um 7 erinnere mich an Wasser', ev.Schedule(7, 0, 'daily', reminder='Wasser')),
    ('tous les jours à 9 rappelle-moi de marcher', ev.Schedule(9, 0, 'daily', reminder='marcher')),
    ('todos los días a las 9 recuérdame beber agua', ev.Schedule(9, 0, 'daily', reminder='beber agua')),
    ('ogni giorno alle 9 ricordami di bere', ev.Schedule(9, 0, 'daily', reminder='bere')),
    ('todos os dias às 9 lembra-me de beber água', ev.Schedule(9, 0, 'daily', reminder='beber água')),
])
def test_schedules(text, schedule):
    assert ev.parse_schedule(text) == schedule


@pytest.mark.parametrize('text', ['напомни через 20 минут выпить воды', 'каждый день я гуляю', 'открой почту', 'каждый день в 25 открывай почту'])
def test_not_schedules(text):
    assert ev.parse_schedule(text) is None


def test_next_due_respects_days_and_time():
    monday_8 = datetime(2026, 9, 28, 8, 0).timestamp()          # Monday
    assert datetime.fromtimestamp(next_due(9, 0, 'daily', monday_8)) == datetime(2026, 9, 28, 9, 0)
    assert datetime.fromtimestamp(next_due(7, 0, 'daily', monday_8)) == datetime(2026, 9, 29, 7, 0)
    assert datetime.fromtimestamp(next_due(9, 0, 'weekends', monday_8)) == datetime(2026, 10, 3, 9, 0)
    assert datetime.fromtimestamp(next_due(9, 0, 'fri', monday_8)) == datetime(2026, 10, 2, 9, 0)
    assert repeat_days('mon,fri') == {0, 4} and repeat_days('weekdays') == {0, 1, 2, 3, 4}


def _manager(tmp_path):
    manager = TimerManager()
    manager._path = tmp_path / 'timers.json'
    return manager


def test_scheduled_command_fires_as_a_command_and_comes_back_next_time(tmp_path):
    manager = _manager(tmp_path)
    item = manager.create_repeating(9, 0, 'daily', 'открой почту', command='открой почту')
    item.due_at = time.time() - 1
    manager._stop.set()           # run one pass of the loop body by hand
    manager._timers[item.id] = item
    now = time.time()
    # the loop body, executed once
    manager._stop.clear()
    manager._kick.set()
    import threading
    thread = threading.Thread(target=manager._loop, daemon=True)
    thread.start()
    deadline = time.time() + 3
    events = []
    while time.time() < deadline and not any(e.kind == 'command' for e in events):
        events += manager.drain()
        time.sleep(0.02)
    manager._stop.set(); manager._kick.set(); thread.join(2)
    assert [e.text for e in events if e.kind == 'command'] == ['открой почту']
    assert any(e.kind == 'chat' and 'По расписанию' in e.text for e in events)
    assert item.id in manager._timers and manager._timers[item.id].due_at > now      # rescheduled, not removed


def test_missed_repeating_items_are_not_run_late_after_a_restart(tmp_path):
    past = time.time() - 7200
    (tmp_path / 'timers.json').write_text(json.dumps([
        {'id': 'r1', 'label': 'таблетки', 'due_at': past, 'created_at': past - 10, 'kind': 'reminder', 'repeat': 'daily', 'hour': 9, 'minute': 0},
        {'id': 'o1', 'label': 'старое', 'due_at': past, 'created_at': past - 10, 'kind': 'reminder'},
    ]), encoding='utf-8')
    manager = TimerManager()
    manager._path = tmp_path / 'timers.json'
    manager._load_locked()
    assert list(manager._timers) == ['r1'] and manager._timers['r1'].due_at > time.time()


def test_loop_sleeps_until_the_next_event_not_twice_a_second(tmp_path):
    manager = _manager(tmp_path)
    assert manager._sleep_seconds(time.time()) == 30.0                      # nothing scheduled
    manager.create(5, 'чай')
    assert 4.0 < manager._sleep_seconds(time.time()) <= 5.0
    manager2 = _manager(tmp_path)
    manager2.create(3600, 'встреча')
    assert manager2._sleep_seconds(time.time()) == 30.0                     # next warning is in 45 min: wake at most every 30 s


def _session(tmp_path):
    from jarvis.testing.atlas_session import AtlasSession
    session = AtlasSession(tmp_path / 'run')
    session.reset_case('ru')
    return session


def test_list_schedule_and_delete_through_the_agent(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = _session(tmp_path)
        try:
            manager = ev._timers(session.agent.config)
            for item in list(manager.list()):
                manager.cancel(item.id)
            session.agent.handle('покажи напоминания')
            assert session.calls[-1]['tool'] == 'timer' and session.calls[-1]['arguments'] == {'action': 'list'}   # the timer tool lists
            made = session.agent.handle('каждый день в 9 открывай почту')
            assert made.text.startswith('Готово: каждый день в 09:00 — выполнить «открой почту».') and made.tool_details == ['timer.schedule']
            session.agent.handle('по будням в 8:30 напоминай выпить таблетки')
            listing = ev.listing_text(manager.list(), 'ru')           # what the timer tool shows
            assert 'по будням 08:30 — напоминание «выпить таблетки»' in listing
            assert 'каждый день 09:00 — выполнить «открой почту»' in listing and listing.endswith('«удали напоминание 2».')
            number = next(line.split('.')[0] for line in listing.splitlines() if 'открой почту' in line)
            assert session.agent.handle(f'удали напоминание {number}').text == 'Удалил: открой почту.'
            assert 'открой почту' not in ev.listing_text(manager.list(), 'ru')
            assert all(call['arguments'] == {'action': 'list'} for call in session.calls)    # nothing else ran now
        finally:
            session.finish()


def test_unknown_or_risky_commands_are_not_scheduled(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = _session(tmp_path)
        try:
            assert session.agent.handle('каждый день в 9 делай что-нибудь интересное').tool_details == ['timer.schedule_unknown']
            refused = session.agent.handle('каждый день в 23 выключай компьютер')
            assert refused.tool_details == ['timer.schedule_refused'] and 'только по вашей команде' in refused.text
        finally:
            session.finish()


def test_main_window_runs_scheduled_commands_visibly():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / 'jarvis' / 'ui' / 'main_window.py').read_text(encoding='utf-8')
    block = source[source.index('def _drain_timer_events'):source.index('def _poll_bluetooth_devices_async')]
    assert 'kind == "command"' in block and 'self._send_text(queued.popleft(), input_source="text", user_submission=False)' in block
    assert 'not getattr(self, "_busy", False)' in block
