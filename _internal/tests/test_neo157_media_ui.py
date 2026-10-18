"""Committed playback, late queue and cancellation use one physical chat row."""
from collections import deque
from types import SimpleNamespace, MethodType
from unittest.mock import Mock
import time

import pytest
from jarvis.ui.main_window import MainWindow
from jarvis.runtime import media_events as bus


def window():
    w = SimpleNamespace(config=SimpleNamespace(get=lambda k,d=None:d), _language_code=lambda:'ru',
        _busy=False,_pending_response_bubble=None,_last_now_playing_by_source={},
        _media_notice_suppressed_until={},_music_overlay_last_event={},_refresh_robot_stage_info=Mock(),
        _pending_media_results157={},_ambient_typing_queue=deque(),_typing_text='',_stream_text='',
        _now_playing_queue_preview=lambda source:'\nДальше в очереди:\n1. Singer — Next')
    w.bubbles=[]
    def append(text,**kwargs):
        row={'text':text};w.bubbles.append(row);return row
    w._append_assistant=Mock(side_effect=append)
    w.chat=SimpleNamespace(update_bubble=lambda row,text:row.update(text=text))
    for name in ['_remember_media_bubble157','_refresh_media_bubble_queue157','_drain_media_now_playing_events','_recover_media_notice157']:
        setattr(w,name,MethodType(getattr(MainWindow,name),w))
    return w


@pytest.fixture(autouse=True)
def clean_bus(monkeypatch):
    bus.drain_now_playing(1000)
    monkeypatch.setattr(bus,'_LATEST_BY_SOURCE',{})
    yield
    bus.drain_now_playing(1000)


def started(title='Song',track_id='A',announce=False):
    bus.publish_now_playing('yandex_music',title,'Singer',track_id,announce=announce)
    return bus.latest_now_playing('yandex_music')


def test_late_queue_updates_same_command_bubble_without_second_message_or_old_typewriter():
    w=window();event=started()
    row=w._append_assistant('Включил Яндекс Музыку — Моя волна. Сейчас играет: Song.\nСледующие треки пока не получены.')
    w._remember_media_bubble157(event,row,row['text'])
    w._pending_response_bubble=row;w._typing_text=row['text'];w._typing_index=4
    w._ambient_typing_queue.append((row,row['text']))
    w._drain_media_now_playing_events()
    bus.publish_now_playing('yandex_music','Song','Singer','A',reason='queue_updated')
    w._drain_media_now_playing_events()
    assert len(w.bubbles)==1 and w._append_assistant.call_count==1
    assert row['text'].startswith('Включил Яндекс Музыку — Моя волна.')
    assert '1. Singer — Next' in row['text'] and 'пока не получены' not in row['text']
    assert w._typing_text==row['text'] and w._typing_index==len(row['text'])
    assert w._ambient_typing_queue[0][1]==row['text']


def test_queue_that_arrives_before_command_result_is_deferred_into_same_bubble():
    w=window();w._busy=True;event=started()
    bus.publish_now_playing('yandex_music','Song','Singer','A',reason='queue_updated')
    w._drain_media_now_playing_events()
    assert not w.bubbles
    row=w._append_assistant('Включил Song.\nОчередь пока недоступна.')
    w._remember_media_bubble157(event,row,row['text'])
    assert len(w.bubbles)==1 and '1. Singer — Next' in row['text']


def test_obsolete_refill_never_changes_later_same_title_playback(monkeypatch):
    # Windows wall-clock resolution can give all these events the same time.
    # The playback identity, not timestamp ordering, must reject an old refill.
    monkeypatch.setattr(bus.time, 'time', lambda: 1234567890.)
    w=window();first=started()
    row=w._append_assistant('First Song');w._remember_media_bubble157(first,row,row['text'])
    bus.publish_now_playing('yandex_music','Song','Singer','A',reason='queue_updated')
    # The same track appears again after a different transition, with new identity.
    started('Other','B');new=started()
    assert new.event_id!=first.event_id
    w._drain_media_now_playing_events()
    assert row['text']=='First Song' and len(w.bubbles)==1


def test_current_refill_updates_same_bubble_when_system_clock_moves_backwards(monkeypatch):
    clock=[1234567890.]
    monkeypatch.setattr(bus.time,'time',lambda:clock[0])
    w=window();current=started()
    row=w._append_assistant('Current Song');w._remember_media_bubble157(current,row,row['text'])
    w._drain_media_now_playing_events()
    clock[0]-=60
    bus.publish_now_playing('yandex_music','Song','Singer','A',reason='queue_updated')
    w._drain_media_now_playing_events()
    assert len(w.bubbles)==1 and w.bubbles[0] is row
    assert row['text'].startswith('Current Song') and '1. Singer — Next' in row['text']


def test_stale_agent_result_recovers_only_current_committed_track_once():
    w=window();event=started()
    w._drain_media_now_playing_events() # announce=False still updates old state dedup
    assert w._last_now_playing_by_source[event.source]==event.key
    w._pending_media_results157[7]=(event,)
    w._recover_media_notice157(7)
    assert len(w.bubbles)==1 and 'Song' in w.bubbles[0]['text'] and 'Next' in w.bubbles[0]['text']
    w._pending_media_results157[7]=(event,)
    w._recover_media_notice157(7)
    assert len(w.bubbles)==1


@pytest.mark.parametrize('replacement', ['other_track','pause','same_track_new_playback'])
def test_stale_result_cannot_announce_an_obsolete_track(replacement):
    w=window();event=started()
    if replacement=='other_track':started('Other','B')
    elif replacement=='same_track_new_playback':started()
    else:bus.publish_now_playing('yandex_music','Song','Singer','A',reason='paused',playing=False)
    w._pending_media_results157[7]=(event,)
    w._recover_media_notice157(7)
    assert not w.bubbles


def test_network_notice_is_plain_and_does_not_change_track_state():
    w=window();started();w._drain_media_now_playing_events()
    before=dict(w._music_overlay_last_event)
    bus.publish_now_playing('yandex_music','Song','Singer','A',reason='network_unavailable',playing=False)
    w._drain_media_now_playing_events()
    assert w._music_overlay_last_event==before
    assert len(w.bubbles)==1 and w.bubbles[0]['text']=='Обнаружена высокая задержка или нарушение работы сети.'
