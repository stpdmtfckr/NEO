"""R041: real scheduler with fake clock/wait/storage; no threads or external effects."""
from __future__ import annotations

import json
import logging
import os
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from jarvis.runtime import timers
from jarvis.testing.isolation import external_io_blocked


@pytest.fixture(autouse=True)
def isolated_io():
    with external_io_blocked(), patch.object(os, 'system', side_effect=AssertionError('no shell')), patch.object(socket.socket, 'sendto', side_effect=AssertionError('no UDP')):
        yield


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(wall=1_700_000_000., mono=100.)
    monkeypatch.setattr(timers.time, 'time', lambda: value.wall)
    monkeypatch.setattr(timers.time, 'monotonic', lambda: value.mono)
    def advance(seconds):
        value.wall += seconds
        value.mono += seconds
    value.advance = advance
    return value


@pytest.fixture
def manager(tmp_path, monkeypatch):
    manager = timers.TimerManager()
    monkeypatch.setattr(manager, '_ensure_thread_locked', lambda: None)
    manager.configure(tmp_path, 'en')
    return manager


def disk_rows(manager):
    return json.loads(manager._path.read_text())


def add_repeating(manager, clock, name='Daily command', seconds=1, command='shutdown computer'):
    item = manager.create_repeating(9, 0, 'daily', name, command)
    item.due_at = clock.wall + seconds
    manager._save_locked()
    return item


def fail_save():
    raise OSError('Synthetic read-only storage')


def test_real_loop_survives_fault_keeps_future_timers_and_recovers(manager, clock, monkeypatch, caplog):
    first = manager.create(1, 'First')
    later = manager.create(12, 'Later')
    final = manager.create(35, 'Final')
    original_save = manager._save_locked
    writes = []
    def flaky_save():
        writes.append(clock.mono)
        if clock.mono < 125:
            fail_save()
        original_save()
    monkeypatch.setattr(manager, '_save_locked', flaky_save)
    class FakeKick:
        waits = []
        def wait(self, delay):
            self.waits.append(delay)
            assert delay >= .05
            clock.advance(delay)
            if clock.mono >= 160:
                manager._stop.set()
            assert len(self.waits) < 30, 'scheduler spun instead of waiting'
        def clear(self):
            pass
    kick = FakeKick()
    manager._kick = kick
    with caplog.at_level(logging.WARNING):
        manager._loop()  # Real synchronous loop, no background thread.
    assert manager._stop.is_set()
    events = manager.drain()
    for item in (first, later, final):
        assert len([event for event in events if event.timer_id == item.id and event.kind == 'chat']) == 1
    assert not manager.list() and disk_rows(manager) == []
    assert len([event for event in events if 'Could not save timers' in event.text]) == 1
    assert len([event for event in events if 'Timer saving recovered' in event.text]) == 1
    assert len([row for row in caplog.records if 'Timer state could not be saved' in row.message]) == 1
    assert len(writes) == 4
    assert not manager._storage_dirty and not manager._storage_outage


def test_continuous_fault_has_bounded_backoff_notice_and_no_spin(manager, clock, monkeypatch, caplog):
    manager.create(1, 'Tea')
    writes = []
    def broken():
        writes.append(clock.mono)
        fail_save()
    monkeypatch.setattr(manager, '_save_locked', broken)
    clock.advance(1)
    with caplog.at_level(logging.WARNING):
        manager._tick()
        # Lots of wakeups must not turn into disk retries or repeated messages.
        for _ in range(1_000):
            manager._tick()
        assert len(writes) == 1
        for delay in (5, 10, 20, 40, 60, 60):
            assert .05 <= manager._sleep_seconds(clock.wall) <= 30
            clock.advance(delay)
            manager._tick()
    assert [round(b-a) for a,b in zip(writes,writes[1:])] == [5,10,20,40,60,60]
    events = manager.drain()
    assert len([event for event in events if 'Could not save timers' in event.text]) == 1
    assert len([row for row in caplog.records if 'Timer state could not be saved' in row.message]) == 1
    assert manager._storage_dirty and manager._storage_retry_delay == 60


def test_wall_clock_jump_does_not_accelerate_retries(manager, clock, monkeypatch):
    manager.create(1, 'Tea')
    calls=[]
    monkeypatch.setattr(manager, '_save_locked', lambda: (calls.append(True), fail_save())[1])
    clock.advance(1); manager._tick()
    clock.wall += 86400
    manager._tick()
    assert len(calls)==1 and manager._sleep_seconds(clock.wall)==5
    clock.wall -= 172800
    manager._tick()
    assert len(calls)==1 and manager._sleep_seconds(clock.wall)==5
    clock.mono += 5
    manager._tick()
    assert len(calls)==2


def test_recurring_command_is_emitted_only_after_next_due_is_durable(manager, clock, monkeypatch):
    item = add_repeating(manager, clock)
    old_due = item.due_at
    original_save = manager._save_locked
    observations = []
    def checked_save():
        assert manager.drain() == []
        assert item.due_at > old_due
        original_save()
        observations.append(disk_rows(manager)[0]['due_at'])
    monkeypatch.setattr(manager, '_save_locked', checked_save)
    clock.advance(1); manager._tick()
    assert observations == [item.due_at]
    events = manager.drain()
    assert [event.text for event in events if event.kind == 'command'] == ['shutdown computer']
    assert any(event.kind == 'chat' and 'Scheduled' in event.text for event in events)
    manager._tick()
    assert manager.drain() == []


def test_failed_recurring_occurrence_skips_without_replay_after_recovery_or_restart(manager, clock, monkeypatch):
    item = add_repeating(manager, clock)
    old_due = item.due_at
    original_save = manager._save_locked
    monkeypatch.setattr(manager, '_save_locked', fail_save)
    clock.advance(1); manager._tick()
    assert item.due_at > old_due
    assert not any(event.kind == 'command' or event.text.startswith('Scheduled:') for event in manager.drain())
    assert disk_rows(manager)[0]['due_at'] == old_due
    # A fresh process skips a past occurrence even before the store recovers.
    restarted = timers.TimerManager()
    monkeypatch.setattr(restarted, '_ensure_thread_locked', lambda: None)
    restarted.configure(manager._path.parent, 'en')
    restarted._tick()
    assert not any(event.kind == 'command' for event in restarted.drain())
    monkeypatch.setattr(manager, '_save_locked', original_save)
    clock.advance(5); manager._tick()
    assert disk_rows(manager)[0]['due_at'] == item.due_at
    assert not any(event.kind == 'command' for event in manager.drain())
    # A later, new occurrence is not stranded by the failed one.
    clock.advance(item.due_at-clock.wall); manager._tick()
    assert len([event for event in manager.drain() if event.kind == 'command']) == 1
    manager._tick()
    assert not any(event.kind == 'command' for event in manager.drain())


def test_commands_due_during_backoff_are_skipped_even_if_disk_has_recovered(manager, clock, monkeypatch):
    manager.create(1, 'Start outage')
    command = add_repeating(manager, clock, seconds=3)
    original_save = manager._save_locked
    monkeypatch.setattr(manager, '_save_locked', fail_save)
    clock.advance(1); manager._tick(); manager.drain()
    monkeypatch.setattr(manager, '_save_locked', original_save)
    clock.advance(2); manager._tick()
    assert not any(event.kind == 'command' for event in manager.drain())
    clock.advance(3); manager._tick()
    assert not any(event.kind == 'command' for event in manager.drain())
    assert disk_rows(manager)[0]['due_at'] == command.due_at


def test_multiple_command_batch_has_no_partial_emission_on_failed_commit(manager, clock, monkeypatch):
    add_repeating(manager, clock, 'A', command='close browser')
    add_repeating(manager, clock, 'B', command='stop video')
    ordinary = manager.create(1, 'Ordinary reminder')
    monkeypatch.setattr(manager, '_save_locked', fail_save)
    clock.advance(1); manager._tick()
    events = manager.drain()
    assert not any(event.kind == 'command' for event in events)
    assert any(event.timer_id == ordinary.id and event.kind == 'chat' for event in events)
    assert all(item.due_at > clock.wall for item in manager.list())


def test_future_warning_and_expiry_remain_once_during_outage(manager, clock, monkeypatch):
    item = manager.create(120, 'Tea')
    monkeypatch.setattr(manager, '_save_locked', fail_save)
    clock.advance(60); manager._tick(); manager._tick()
    events = manager.drain()
    assert len([event for event in events if event.timer_id==item.id and event.kind=='chat']) == 1
    clock.advance(60); manager._tick(); manager._tick()
    events = manager.drain()
    assert len([event for event in events if event.timer_id==item.id and event.kind=='chat']) == 1


def test_delayed_action_take_still_requires_its_own_durable_commit(manager, clock, monkeypatch):
    manager.create(1, 'Start outage')
    spec={'action':'shutdown','language':'en','target':{},'risk':2,'confirmed':True}
    action = manager.create_action(2, 'Shutdown', spec)
    original_save = manager._save_locked
    monkeypatch.setattr(manager, '_save_locked', fail_save)
    clock.advance(1); manager._tick()
    clock.advance(1); manager._tick()
    assert any(event.kind=='action' and event.timer_id==action.id for event in manager.drain())
    with pytest.raises(OSError): manager.take_action(action.id)
    assert any(item.id==action.id for item in manager.list())
    monkeypatch.setattr(manager, '_save_locked', original_save)
    assert manager.take_action(action.id).id==action.id
    assert manager.take_action(action.id) is None
    assert disk_rows(manager)==[]


def test_explicit_create_cancel_rollback_semantics_unchanged_during_outage(manager, clock, monkeypatch):
    item = manager.create(60, 'Tea')
    monkeypatch.setattr(manager, '_save_locked', fail_save)
    with pytest.raises(OSError): manager.create(60, 'Not stored')
    with pytest.raises(OSError): manager.cancel(item.id)
    assert [row.id for row in manager.list()]==[item.id]


@pytest.mark.parametrize('code', ['ru','en','de','fr','es','it','pt'])
def test_outage_and_recovery_feedback_exist_in_selected_locale(manager, code):
    manager._lang=code
    failure=manager._storage_message(False); recovery=manager._storage_message(True)
    assert failure and recovery and failure!=recovery
    if code!='en':
        assert 'Could not save timers' not in failure and 'Timer saving recovered' not in recovery
