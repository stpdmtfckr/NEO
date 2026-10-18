"""Field regressions with fake transport: real adapter, tool, event bus and monitor."""
from types import SimpleNamespace
from unittest.mock import Mock
import threading
import pytest
from tests.test_media_reliability import Config, Track, local_adapter
from jarvis.integrations import yandex_music as ym
from jarvis.runtime.media_events import (command_result_owns_media_notice, drain_now_playing, publish_now_playing, latest_now_playing, subscribe_now_playing, unsubscribe_now_playing)
from jarvis.runtime.localized_text import render_action_result
from jarvis.tools.domains.browser_media import register_browser_media_tools


def test_command_notice_ownership_preserves_state_and_automatic_notices():
    drain_now_playing(128);received=[]
    subscribe_now_playing(received.append)
    try:
        with command_result_owns_media_notice():
            publish_now_playing('yandex_music','First',track_id='first')
            automatic=threading.Thread(target=lambda:publish_now_playing('yandex_music','Second',track_id='second'))
            automatic.start();automatic.join()
        publish_now_playing('yandex_music','Third',track_id='third')
    finally:unsubscribe_now_playing(received.append)
    events=drain_now_playing(128)
    assert [e.announce for e in events]==[False,True,True]
    assert received==events and latest_now_playing('yandex_music').title=='Third'


def test_nested_failed_command_restores_notice_context():
    drain_now_playing(128)
    with pytest.raises(ValueError),command_result_owns_media_notice():
        with command_result_owns_media_notice():publish_now_playing('yandex_music','Nested')
        publish_now_playing('yandex_music','Outer')
        raise ValueError('failed request')
    publish_now_playing('yandex_music','Automatic')
    assert [e.announce for e in drain_now_playing(128)]==[False,False,True]


def test_production_tool_owns_notice_and_returns_real_queue(tmp_path):
    drain_now_playing(128)
    def execute(action,**kw):
        publish_now_playing('yandex_music','Current','Artist','a')
        return {'verified':True,'state':'playing','title':'Current','artist':'Artist','station_kind':'my_wave'}
    adapter=SimpleNamespace(execute=execute,queue_status=lambda **kw:{'queue':[{'title':f'Song {i}'} for i in range(8)]})
    entries={};config=Config(tmp_path);config.values['integrations.yandex_music.enabled']=True
    registry=SimpleNamespace(register=lambda d:entries.setdefault(d.name,d))
    register_browser_media_tools(registry,config=config,yandex_adapter=adapter,spotify_adapter=adapter,youtube_adapter=adapter,discord_adapter=adapter)
    result=entries['yandex_music'].handler({'action':'my_wave'})
    assert result.success and 'Яндекс Музыку' in result.text and 'Current' in result.text
    assert len(result.data['queue'])==5
    events=drain_now_playing(128)
    assert len(events)==1 and events[0].announce is False


@pytest.mark.parametrize('code',['ru','en','de','fr','es','it','pt'])
@pytest.mark.parametrize('unchanged',[False,True])
def test_wave_result_has_service_station_current_and_five_real_songs(code,unchanged):
    data={'verified':True,'content_provider':'yandex_music','station_kind':'my_wave','title':'Current','artist':'Singer','unchanged':unchanged,'queue':[{'title':f'Queued {i}','artist':f'Artist {i}'} for i in range(7)]}
    result=render_action_result('yandex_music',{'action':'my_wave'},True,'ignored',data,code)
    assert ('Яндекс Музыка' in result or 'Яндекс Музыку' in result) if code=='ru' else 'Yandex Music' in result
    assert 'Current' in result and 'Singer' in result
    assert all(f'Queued {i}' in result for i in range(5))
    assert 'Queued 5' not in result and 'Queued 6' not in result and result.count('Current')==1


@pytest.mark.parametrize('code',['ru','en','de','fr','es','it','pt'])
def test_wave_short_queue_is_truthful_and_localized(code):
    data={'verified':True,'content_provider':'yandex_music','title':'Current','queue':[],'queue_refilling':True}
    empty=render_action_result('yandex_music',{'action':'my_wave'},True,'ignored',data,code)
    assert len(empty.splitlines())==2 and '1.' not in empty
    data['queue']=[{'title':'Only known song'}]
    partial=render_action_result('yandex_music',{'action':'my_wave'},True,'ignored',data,code)
    assert 'Only known song' in partial and '2.' not in partial and len(partial.splitlines())==4
    if code!='ru':assert 'Очеред' not in partial and 'Включил' not in partial


@pytest.mark.parametrize('effect',['_remember_current','_remember_wave_track','_start_station_monitor','_start_prefetch_window','_ensure_wave_queue_depth','_persist_playback','_station_feedback'])
def test_started_wave_is_not_retried_when_post_start_effect_fails(local_adapter,monkeypatch,effect):
    adapter,player=local_adapter;adapter.config.values['integrations.yandex_music.enabled']=True
    adapter._station='';adapter._auto_advance=False
    monkeypatch.setattr(adapter,'_api_client',lambda:object())
    wave=Mock(return_value={'tracks':[Track('A'),Track('B')],'batch_id':'batch'})
    monkeypatch.setattr(adapter,'wave_tracks',wave)
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth',lambda *_:None)
    monkeypatch.setattr(adapter,effect,Mock(side_effect=RuntimeError('side effect fixture')))
    player.play=Mock(return_value={'verified':True,'timeline_verified':True,'duration_ms':180000,'state':'playing'})
    result=adapter.my_wave()
    assert result['verified'] and result['title']=='A'
    assert player.play.call_count==1 and wave.call_count==1
    assert adapter._station=='user:onyourwave' and adapter._auto_advance


def test_duration_probe_failure_does_not_restart_verified_source(local_adapter,monkeypatch):
    adapter,player=local_adapter
    player.play=Mock(return_value={'verified':True,'timeline_verified':True,'duration_ms':0,'state':'playing'})
    player.status=Mock(side_effect=TimeoutError('status reply lost'))
    result=adapter._play_track_object(Track('A'),station='finite:test',auto_advance=False)
    assert result['verified'] and player.play.call_count==1


def test_automatic_next_rechecks_manual_pause_inside_transition_lane(local_adapter):
    adapter,player=local_adapter;adapter._manual_pause_active=True
    player.play=Mock(side_effect=AssertionError('manual pause must win'))
    result=adapter._next_background(auto=True)
    assert result['cancelled'] and result['manual_pause']
    player.play.assert_not_called()


def test_monitor_advances_once_at_real_end_and_ignores_stale_end(local_adapter,monkeypatch):
    adapter,player=local_adapter
    adapter._current_track_obj=Track('A');adapter._current_track_meta={'id':'A'}
    adapter._station='user:onyourwave';adapter._auto_advance=True
    adapter._current_started_monotonic=1;adapter._manual_transport_guard_until=0;adapter._transition_guard_until=0
    clock=[15.];tick=[0]
    class Stop:
        def wait(self,_):
            tick[0]+=1;clock[0]+=15
            return tick[0]>4
    adapter._monitor_stop=Stop()
    player.status_if_running=Mock(side_effect=[{'state':'playing','position_ms':120000,'duration_ms':180000},{'state':'stopped','media_status':'EndOfMedia','position_ms':180000,'duration_ms':180000},{'state':'stopped','media_status':'EndOfMedia','position_ms':180000,'duration_ms':180000},{'state':'playing','position_ms':1000,'duration_ms':180000}])
    def advance(**kwargs):
        assert kwargs=={'auto':True}
        adapter._current_track_obj=Track('B');adapter._current_started_monotonic=clock[0]
    next_call=Mock(side_effect=advance);monkeypatch.setattr(adapter,'_next_background',next_call)
    monkeypatch.setattr(ym,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    adapter._station_monitor()
    assert next_call.call_count==1


@pytest.mark.parametrize('effect',['_station_feedback','_remember_wave_track','_queue_preview_state','_persist_playback'])
def test_automatic_next_post_start_failure_keeps_exactly_one_selected_track(local_adapter,monkeypatch,effect):
    adapter,player=local_adapter
    adapter._current_track_obj=Track('A');adapter._current_track_meta={'id':'A'}
    adapter._station='user:onyourwave';adapter._auto_advance=True
    adapter._station_queue=[Track('B'),Track('C')]
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth',lambda *_:None)
    monkeypatch.setattr(adapter,effect,Mock(side_effect=RuntimeError('ancillary fixture')))
    player.play=Mock(return_value={'verified':True,'duration_ms':180000,'state':'playing'})
    result=adapter._next_background(auto=True)
    assert result['verified'] and result['title']=='B'
    assert player.play.call_count==1 and ym._track_id(adapter._current_track_obj)=='B'
    assert [ym._track_id(t) for t in adapter._station_queue]==['C']


@pytest.mark.parametrize('effect',['_persist_playback','_queue_preview_state'])
def test_previous_post_start_failure_is_not_reported_as_transport_failure(local_adapter,monkeypatch,effect):
    adapter,player=local_adapter
    adapter._current_track_obj=Track('B');adapter._current_track_meta={'id':'B'}
    adapter._station='finite:test';adapter._history=[Track('A')]
    monkeypatch.setattr(adapter,effect,Mock(side_effect=RuntimeError('ancillary fixture')))
    player.play=Mock(return_value={'verified':True,'duration_ms':180000,'state':'playing'})
    result=adapter._previous_background()
    assert result['verified'] and result['title']=='A' and player.play.call_count==1
    assert adapter._history==[] and ym._track_id(adapter._station_queue[0])=='B'


def test_coalesced_next_is_pending_tool_success_without_replaying(tmp_path):
    entries={};config=Config(tmp_path);config.values['integrations.yandex_music.enabled']=True
    adapter=SimpleNamespace(execute=Mock(return_value={'already_advancing':True,'verified':False,'starting':True,'requested':True}))
    registry=SimpleNamespace(register=lambda d:entries.setdefault(d.name,d))
    register_browser_media_tools(registry,config=config,yandex_adapter=adapter,spotify_adapter=adapter,youtube_adapter=adapter,discord_adapter=adapter)
    result=entries['yandex_music'].handler({'action':'next'})
    assert result.success and result.data['outcome_pending'] and result.data['verified'] is False
    assert adapter.execute.call_count==1


def test_monitor_does_not_schedule_second_next_on_successful_track_feedback_failure(local_adapter,monkeypatch):
    adapter,player=local_adapter
    adapter._current_track_obj=Track('A');adapter._current_track_meta={'id':'A'}
    adapter._station='user:onyourwave';adapter._auto_advance=True
    adapter._station_queue=[Track('B'),Track('C')]
    adapter._current_started_monotonic=1;adapter._transition_guard_until=0;adapter._manual_transport_guard_until=0
    clock=[20.];ticks=[0]
    class Stop:
        def wait(self,_):
            ticks[0]+=1;clock[0]+=15
            return ticks[0]>5
    adapter._monitor_stop=Stop()
    def status():
        return {'state':'stopped' if ticks[0]==2 else 'playing','media_status':'EndOfMedia' if ticks[0]==2 else 'LoadedMedia','position_ms':180000 if ticks[0]==2 else 3000,'duration_ms':180000}
    player.status_if_running=status
    player.play=Mock(return_value={'verified':True,'duration_ms':180000,'state':'playing'})
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth',lambda *_:None)
    monkeypatch.setattr(adapter,'_station_feedback',Mock(side_effect=RuntimeError('feedback unavailable')))
    monkeypatch.setattr(ym.time,'monotonic',lambda:clock[0])
    adapter._station_monitor()
    assert player.play.call_count==1 and ym._track_id(adapter._current_track_obj)=='B'


@pytest.mark.parametrize('action',['pause','stop'])
@pytest.mark.parametrize('phase',['cache','verification'])
def test_new_manual_pause_or_stop_cancels_automatic_prepared_playback(local_adapter,monkeypatch,action,phase):
    adapter,player=local_adapter
    adapter.config.values['integrations.yandex_music.enabled']=True
    adapter._current_track_obj=Track('A');adapter._current_track_meta={'id':'A'}
    adapter._station='user:onyourwave';adapter._auto_advance=True
    adapter._station_queue=[Track('B'),Track('C')]
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth',lambda *_:None)
    player.pause=Mock(return_value={'verified':True,'state':'paused'})
    player.stop=Mock(return_value={'verified':True,'state':'stopped'})
    def interrupt():
        adapter.transport(action)
    def cache(candidate):
        if phase=='cache':interrupt()
        return '/prepared.wav'
    def play(*a,**kw):
        if phase=='verification':interrupt()
        return {'verified':True,'state':'playing','duration_ms':180000}
    monkeypatch.setattr(adapter,'_cached_candidate_path',cache)
    player.play=Mock(side_effect=play)
    result=adapter._next_background(auto=True)
    assert result['cancelled'] and not result['verified']
    assert adapter._manual_pause_active and ym._track_id(adapter._current_track_obj)=='A'
    assert player.play.call_count==(0 if phase=='cache' else 1)
    assert [ym._track_id(t) for t in adapter._station_queue]==['B','C']


def test_new_pause_during_wave_resolution_cannot_restart_station(local_adapter,monkeypatch):
    adapter,player=local_adapter
    adapter.config.values['integrations.yandex_music.enabled']=True
    adapter._current_track_obj=Track('A');adapter._current_track_meta={'id':'A'}
    adapter._station='similar:A';adapter._auto_advance=True
    player.pause=Mock(return_value={'verified':True,'state':'paused'})
    player.play=Mock(side_effect=AssertionError('paused user must not be restarted'))
    monkeypatch.setattr(adapter,'_api_client',lambda:object())
    def wave(**kw):
        adapter.transport('pause')
        return {'tracks':[Track('B')],'batch_id':'new'}
    monkeypatch.setattr(adapter,'wave_tracks',wave)
    result=adapter.my_wave()
    assert result['cancelled'] and adapter._manual_pause_active
    assert adapter._station=='similar:A'
    player.play.assert_not_called()


def test_player_dispatch_keeps_pause_order_and_does_not_lock_verification(monkeypatch):
    from jarvis.integrations.background.local_player import BackgroundLocalPlayer
    player=BackgroundLocalPlayer();sent=[];in_play=threading.Event();release=threading.Event();pause_sent=threading.Event()
    monkeypatch.setattr(player,'_ensure',lambda:None)
    def dispatch(payload,**kw):
        action=payload['action'];sent.append(action)
        if action=='play':in_play.set();assert release.wait(1)
        if action=='pause':pause_sent.set()
        return {'state':'playing' if action=='play' else 'paused','verified':True}
    monkeypatch.setattr(player,'_request_unlocked',dispatch)
    generation=[0]
    def guard():
        if generation[0]!=0:raise ym.PlaybackSuperseded('new pause')
    first=threading.Thread(target=lambda:player._request({'action':'play'},before_send=guard))
    first.start();assert in_play.wait(1)
    generation[0]=1
    second=threading.Thread(target=lambda:player._request({'action':'pause'}))
    second.start();assert not pause_sent.is_set()
    release.set();first.join(1);second.join(1)
    assert sent==['play','pause']
    with pytest.raises(ym.PlaybackSuperseded):player._request({'action':'play'},before_send=guard)
    assert sent==['play','pause']
