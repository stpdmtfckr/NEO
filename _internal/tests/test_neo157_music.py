"""Field regressions through production queues, controls, events and formatting."""
from types import SimpleNamespace
from unittest.mock import Mock
import time
import pytest
from tests.test_media_reliability import Config, Track, local_adapter
from jarvis.integrations import yandex_music as ym
from jarvis.runtime.media_events import drain_now_playing, latest_now_playing, publish_now_playing
from jarvis.runtime.localized_text import render_action_result
from jarvis.runtime.media_notice156 import network_notice
from jarvis.tools.domains.browser_media import register_browser_media_tools


def finished(adapter):
    deadline = time.monotonic() + 2
    while adapter._wave_refill_active and time.monotonic() < deadline:
        time.sleep(.002)
    assert not adapter._wave_refill_active


def current(adapter):
    adapter._current_track_obj = Track('A')
    adapter._current_track_meta = {'id':'A','title':'A','artist':'Artist'}
    adapter._station = 'user:onyourwave'
    adapter._station_queue = [Track('B'), Track('C')]
    adapter._auto_advance = True
    adapter.config.values['integrations.yandex_music.enabled'] = True


def test_rotor_does_not_claim_unplayed_branches_are_continuation(local_adapter,monkeypatch):
    adapter,_=local_adapter;calls=[]
    monkeypatch.setattr(adapter,'_api_client',lambda:object())
    adapter.config.values['integrations.yandex_music.wave_repeat_policy']='strict'
    monkeypatch.setattr(adapter,'_wave_repeat_floor',lambda:({'A','B','C','D','E','F'},set()))
    monkeypatch.setattr(adapter,'_wave_recent_sets',lambda **_: (set(),set()))
    def fetch(station,queue_track_id=''):
        calls.append(queue_track_id)
        names={'':'AB','B':'CD','D':'EF','F':'AB','A':'GHIJKL'}.get(queue_track_id,'AB')
        return [Track(x) for x in names], 'batch'
    monkeypatch.setattr(adapter,'_station_tracks',fetch)
    result=adapter.wave_tracks(fresh_start=True,fast_start=True,target_count=6)
    assert calls==['']
    assert result['tracks']==[] and result['reason']=='recent_tracks_filtered'
    assert result['repeat_floor_enforced']


def test_start_requests_current_plus_real_queue_without_duplicate_radio_started(local_adapter,monkeypatch):
    adapter,player=local_adapter;adapter.config.values['integrations.yandex_music.enabled']=True
    monkeypatch.setattr(adapter,'_api_client',lambda:object())
    wave=Mock(return_value={'tracks':[Track(x) for x in 'ABCDEF'],'batch_id':'batch'})
    monkeypatch.setattr(adapter,'wave_tracks',wave)
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth',lambda *_:None)
    feedback=Mock();monkeypatch.setattr(adapter,'_station_feedback',feedback)
    result=adapter.my_wave()
    assert result['verified'] and result['title']=='A'
    assert wave.call_args.kwargs['target_count']==1
    assert [t.id for t in adapter._station_queue]==list('BCDEF')
    assert [c.args[0] for c in feedback.call_args_list]==['trackStarted']


def test_refill_continues_from_real_previous_and_keeps_current_batch(local_adapter,monkeypatch):
    adapter,_=local_adapter;current(adapter);adapter._station_batch_id='current-batch'
    adapter._wave_last_completed_id='previous-real'
    wave=Mock(return_value={'tracks':[Track(x) for x in 'DEFGHIJKLM'],'batch_id':'refill-batch'})
    monkeypatch.setattr(adapter,'wave_tracks',wave)
    adapter._ensure_wave_queue_depth(8);finished(adapter)
    assert wave.call_args.kwargs['queue_track_id']=='previous-real'
    assert {'A','B','C'} <= wave.call_args.kwargs['avoid_ids']
    assert adapter._station_batch_id=='current-batch'
    assert [t.id for t in adapter._station_queue][:5]==list('BCDEF')


def test_exhausted_refill_cannot_hammer_rotor_on_repeated_status_reads(local_adapter,monkeypatch):
    adapter,_=local_adapter;current(adapter);adapter._station_queue=[]
    wave=Mock(return_value={'tracks':[]});monkeypatch.setattr(adapter,'wave_tracks',wave)
    direct=Mock(return_value=([],''));monkeypatch.setattr(adapter,'_station_tracks',direct)
    adapter._ensure_wave_queue_depth();finished(adapter)
    for _ in range(20):adapter.queue_status()
    assert wave.call_count==1 and direct.call_count==0
    assert adapter._wave_refill_retry_at > time.monotonic()


def test_late_queue_publishes_queue_only_for_reported_current_and_preserves_player_truth(local_adapter,monkeypatch):
    adapter,_=local_adapter;current(adapter);adapter._station_queue=[]
    drain_now_playing(128);publish_now_playing('yandex_music','A','Artist','A')
    original=latest_now_playing('yandex_music');drain_now_playing(128)
    adapter.mark_queue_reported('A',False)
    monkeypatch.setattr(adapter,'wave_tracks',lambda **_: {'tracks':[Track(x) for x in 'BCDEFGHIJ']})
    adapter._ensure_wave_queue_depth(8);finished(adapter)
    events=drain_now_playing(128)
    assert [e.reason for e in events]==['queue_updated']
    assert events[0].track_id=='A' and latest_now_playing('yandex_music')==original
    adapter._ensure_wave_queue_depth(8);finished(adapter)
    assert not drain_now_playing(128)


@pytest.mark.parametrize('code',['ru','en','de','fr','es','it','pt'])
@pytest.mark.parametrize('action',['status','background_status','queue_status','next','previous','play_track'])
def test_every_track_response_contains_literal_current_and_actual_next_queue(code,action):
    data={'verified':True,'state':'playing','content_provider':'yandex_music','title':'CurrentRealTitle',
          'queue':[{'title':'NextRealTitle','artist':'RealArtist'}]}
    result=render_action_result('yandex_music',{'action':action},True,'ignored',data,code)
    assert result.count('CurrentRealTitle')==1
    assert '1. RealArtist — NextRealTitle' in result


def test_network_next_does_not_skip_other_candidates_or_reset_worker(local_adapter,monkeypatch):
    adapter,player=local_adapter;current(adapter)
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth',lambda *_:None)
    player.play=Mock(side_effect=TimeoutError('CDN offline private diagnostic'))
    player.close=Mock()
    with pytest.raises(ConnectionError):adapter._next_background(auto=True)
    assert player.play.call_count==1 and not player.close.called
    assert adapter._current_track_obj.id=='A' and [x.id for x in adapter._station_queue]==['B','C']
    player.play=Mock(return_value={'verified':True,'state':'playing','duration_ms':180000})
    recovered=adapter._next_background(auto=True)
    assert recovered['title']=='B' and player.play.call_count==1
    assert [x.id for x in adapter._station_queue]==['C']


@pytest.mark.parametrize('code',['ru','en','de','fr','es','it','pt'])
def test_network_failure_text_has_no_diagnostics_or_claimed_success(tmp_path,code):
    config=Config(tmp_path);config.values.update({'integrations.yandex_music.enabled':True,'app.language':code})
    adapter=SimpleNamespace(execute=Mock(side_effect=TimeoutError('timeout 43567ms http://private.invalid?token=secret')))
    entries={};registry=SimpleNamespace(register=lambda d:entries.setdefault(d.name,d))
    register_browser_media_tools(registry,config=config,yandex_adapter=adapter,spotify_adapter=adapter,youtube_adapter=adapter,discord_adapter=adapter)
    result=entries['yandex_music'].handler({'action':'next'})
    assert not result.success and result.data['error_kind']=='network'
    rendered=render_action_result('yandex_music',{'action':'next'},False,result.text,result.data,code)
    assert rendered==network_notice(code) and result.text==rendered
    assert all(x not in rendered for x in ['43567','http','secret','timeout'])


def monitor_setup(adapter,player,monkeypatch,*,pause_at=0):
    current(adapter);adapter._current_started_monotonic=1
    adapter._transition_guard_until=0;adapter._manual_transport_guard_until=0
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth',lambda *_:None)
    clock=[10.];tick=[0]
    class Stop:
        def wait(self,_):
            tick[0]+=1;clock[0]+=5
            if tick[0]==pause_at:
                adapter._manual_transport_serial+=1;adapter._manual_pause_active=True
                adapter.on_network_changed()
            return tick[0]>9
    adapter._monitor_stop=Stop()
    def status():
        return {'state':'stopped' if tick[0]==2 else 'playing','error':'Network error' if tick[0]==2 else '',
                'position_ms':45000,'duration_ms':180000,'media_status':'InvalidMedia' if tick[0]==2 else 'LoadedMedia'}
    player.status_if_running=status
    monkeypatch.setattr(ym.time,'monotonic',lambda:clock[0])
    return clock,tick


def test_mid_track_network_off_on_retries_same_track_and_restores_position(local_adapter,monkeypatch):
    adapter,player=local_adapter;drain_now_playing(128)
    monitor_setup(adapter,player,monkeypatch)
    player.play=Mock(side_effect=[TimeoutError('network unreachable'),{'verified':True,'state':'playing','duration_ms':180000}])
    player.seek=Mock(return_value={'verified':True});player.close=Mock()
    next_call=Mock(side_effect=AssertionError('network outage must not skip songs'))
    monkeypatch.setattr(adapter,'_next_background',next_call)
    adapter._station_monitor()
    assert player.play.call_count==2 and not next_call.called and not player.close.called
    assert adapter._current_track_obj.id=='A' and [x.id for x in adapter._station_queue]==['B','C']
    assert player.seek.call_args.args==(45000,)
    events=drain_now_playing(128)
    assert len([e for e in events if e.reason=='network_unavailable'])==1
    assert all(e.announce is False for e in events if e.reason=='track_started')


@pytest.mark.parametrize('pause_at',[3,4])
def test_network_recovery_never_overrides_new_pause_or_stop(local_adapter,monkeypatch,pause_at):
    adapter,player=local_adapter
    monitor_setup(adapter,player,monkeypatch,pause_at=pause_at)
    player.play=Mock(side_effect=TimeoutError('network unavailable'));player.seek=Mock()
    next_call=Mock();monkeypatch.setattr(adapter,'_next_background',next_call)
    adapter._station_monitor()
    assert player.play.call_count==(0 if pause_at==3 else 1)
    assert not player.seek.called and not next_call.called
    assert adapter._manual_pause_active and adapter._current_track_obj.id=='A'
    assert [x.id for x in adapter._station_queue]==['B','C']


def test_refill_winning_snapshot_to_answer_race_is_returned_in_same_answer(local_adapter):
    adapter,_=local_adapter;current(adapter)
    drain_now_playing(128)
    rows=adapter.mark_queue_reported('A',False)
    assert [row['id'] for row in rows]==['B','C']
    assert adapter._queue_reported_empty is False and not drain_now_playing(128)
    assert adapter.mark_queue_reported('stale-track',False) is None


def test_exact_command_event_capture_excludes_other_threads_and_restores_nested_scope():
    import threading
    from jarvis.runtime.media_events import capture_media_events, command_result_owns_media_notice
    drain_now_playing(128)
    with capture_media_events() as outer, command_result_owns_media_notice():
        first_id=publish_now_playing('yandex_music','Command first',track_id='first')
        with capture_media_events() as inner:
            publish_now_playing('yandex_music','Nested',track_id='nested')
        thread=threading.Thread(target=lambda:publish_now_playing('yandex_music','Other thread',track_id='other'))
        thread.start();thread.join()
        publish_now_playing('yandex_music','Command last',track_id='last')
    assert [e.track_id for e in outer]==['first','last']
    assert [e.track_id for e in inner]==['nested']
    assert outer[0].event_id==first_id and all(not e.announce for e in outer)
    events=drain_now_playing(128)
    assert len(events)==4 and next(e for e in events if e.track_id=='other').announce


def test_wave_start_never_downloads_full_song_on_command_thread(local_adapter,monkeypatch):
    adapter,player=local_adapter
    adapter.config.values['integrations.yandex_music.enabled']=True
    monkeypatch.setattr(adapter,'_api_client',lambda:object())
    monkeypatch.setattr(adapter,'wave_tracks',lambda **_: {'tracks':[Track(x) for x in 'ABCDEF'],'batch_id':'batch'})
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth',lambda *_:None)
    monkeypatch.setattr(adapter,'_cached_candidate_path',lambda *_:'')
    cache=Mock(side_effect=AssertionError('full download on foreground command'))
    monkeypatch.setattr(adapter,'_cache_candidate',cache)
    prefetch=Mock();monkeypatch.setattr(adapter,'_start_prefetch_window',prefetch)
    player.play=Mock(return_value={'verified':True,'state':'playing','duration_ms':180000})
    result=adapter.my_wave()
    assert result['verified'] and result['source_method']=='direct_stream_start'
    assert player.play.call_args.args==('https://invalid.test/audio',)
    assert player.play.call_args.kwargs['fast_verify'] is False
    assert not cache.called and prefetch.call_args.args[0][0].id=='A'
    assert len(prefetch.call_args.args[0])<=6


def test_transient_desktop_queue_notice_cannot_corrupt_telegram_now_playing(tmp_path):
    from jarvis.integrations.telegram_remote import TelegramRemoteController
    from jarvis.runtime.media_events import MediaNowPlayingEvent
    controller=TelegramRemoteController.__new__(TelegramRemoteController)
    original=MediaNowPlayingEvent(source='yandex_music',title='Current',track_id='A')
    controller._latest_media_by_source={'yandex_music':original}
    controller._media_history_by_source={}
    for reason in ['queue_updated','network_unavailable']:
        controller._enqueue_media_notification(MediaNowPlayingEvent(source='yandex_music',title='Other',track_id='B',reason=reason,playing=False))
    assert controller._latest_media_by_source=={'yandex_music':original}
    assert not controller._media_history_by_source


def test_stalled_buffer_without_error_recovers_current_without_next(local_adapter,monkeypatch):
    adapter,player=local_adapter;drain_now_playing(128)
    _,tick=monitor_setup(adapter,player,monkeypatch)
    resumed=[False]
    player.status_if_running=lambda:{'state':'playing','media_status':'LoadedMedia' if resumed[0] else 'StalledMedia','position_ms':45000,'duration_ms':180000}
    def restart(*args,**kwargs):
        resumed[0]=True
        return {'verified':True,'state':'playing','duration_ms':180000}
    player.play=Mock(side_effect=restart)
    player.seek=Mock(return_value={'verified':True})
    next_call=Mock();monkeypatch.setattr(adapter,'_next_background',next_call)
    adapter._station_monitor()
    assert player.play.call_count==1 and not next_call.called
    assert adapter._current_track_obj.id=='A' and player.seek.call_args.args==(45000,)
    assert len([e for e in drain_now_playing(128) if e.reason=='network_unavailable'])==1
