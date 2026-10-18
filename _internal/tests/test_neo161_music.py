"""Real Rotor/desktop/Discord state machines with only network and audio edges replaced."""
from types import SimpleNamespace
from unittest.mock import Mock
import threading
import time
import pytest
from tests.test_media_reliability import Config, Track, local_adapter
from jarvis.integrations.discord_navigation import ReliableDiscordAdapter
from jarvis.integrations import yandex_music as ym


def rotor(adapter, monkeypatch, batches):
    calls, starts = [], []
    def fetch(station, settings2=True, queue=None):
        calls.append(queue or '')
        names = batches(queue or '') if callable(batches) else batches
        return SimpleNamespace(sequence=[SimpleNamespace(track=Track(x)) for x in names], batch_id='batch-'+str(queue or 'initial'))
    client = SimpleNamespace(rotor_station_tracks=fetch,
        rotor_station_feedback_radio_started=lambda *a, **kw: starts.append(a))
    monkeypatch.setattr(adapter, '_api_client', lambda: client)
    return calls, starts


def played_history(adapter, ids='ABCDE'):
    now = time.time()
    adapter._wave_recent_entries = [{'id':x, 'fingerprint':ym._track_fingerprint(Track(x)), 'ts':now-60*(len(ids)-i)} for i,x in enumerate(ids)]
    adapter._persist_wave_recent_ids()


def test_cold_restart_all_five_recent_uses_actual_last_played_cursor(local_adapter, monkeypatch):
    adapter,_=local_adapter;played_history(adapter)
    calls,starts=rotor(adapter,monkeypatch,lambda hint:'ABCDE' if not hint else 'FG')
    result=adapter.wave_tracks(fresh_start=True)
    assert calls==['','E'] and len(starts)==1
    assert [t.id for t in result['tracks']]==['F','G']
    assert result['history_continuation_used'] and result['historical_fallback_count']==0
    assert result['repeat_floor_enforced']


def test_small_unchanging_real_pool_default_starts_oldest_without_erasing_history(local_adapter, monkeypatch):
    adapter,player=local_adapter;played_history(adapter)
    adapter.config.values['integrations.yandex_music.enabled']=True
    calls,starts=rotor(adapter,monkeypatch,'ABCDE')
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth',lambda *_:None)
    result=adapter.my_wave()
    assert result['title']=='A' and result['verified']
    assert calls==['','E'] and len(starts)==1
    assert [t.id for t in adapter._station_queue]==list('BCD')
    assert set(adapter._wave_recent_ids)==set('ABCDE')
    assert adapter._last_wave_diagnostic['historical_fallback_count']==4
    assert not adapter._last_wave_diagnostic['repeat_floor_enforced']
    assert adapter._last_wave_diagnostic['immediate_repeat_guard_enforced']


@pytest.mark.parametrize('policy',[{'integrations.yandex_music.wave_repeat_policy':'strict'},
    {'integrations.yandex_music.wave_no_repeat_hours':24}, {'integrations.yandex_music.wave_no_repeat_tracks':40}])
def test_explicit_no_repeat_policy_stays_strict_and_bounded(local_adapter, monkeypatch, policy):
    adapter,_=local_adapter;played_history(adapter);adapter.config.values.update(policy)
    adapter.config.values['integrations.yandex_music.enabled']=True
    calls,starts=rotor(adapter,monkeypatch,'ABCDE')
    with pytest.raises(ym.WaveUnavailable,match='недавно'):
        adapter.my_wave()
    assert calls==['','E'] and len(starts)==1
    assert adapter._current_track_obj is None
    assert set(adapter._wave_recent_ids)==set('ABCDE')


def test_older_fallback_never_duplicates_current_or_pending_queue(local_adapter,monkeypatch):
    adapter,_=local_adapter;played_history(adapter)
    rotor(adapter,monkeypatch,'ABCDE')
    result=adapter.wave_tracks(queue_track_id='E',avoid_ids={'A','B','C'})
    assert [t.id for t in result['tracks']]==['D']
    assert result['historical_fallback_count']==1


def test_only_last_recording_cannot_replay_even_under_default_policy(local_adapter,monkeypatch):
    adapter,_=local_adapter;played_history(adapter,'A')
    rotor(adapter,monkeypatch,'A')
    result=adapter.wave_tracks(fresh_start=True)
    assert not result['tracks'] and result['probe_count']==2
    assert result['reason']=='recent_tracks_filtered'


def test_five_song_pool_continues_past_batch_boundary_without_double_play(local_adapter,monkeypatch):
    adapter,player=local_adapter;played_history(adapter)
    adapter.config.values['integrations.yandex_music.enabled']=True
    calls,starts=rotor(adapter,monkeypatch,'ABCDE')
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth',lambda *_:None)
    original=player.play;player.play=Mock(side_effect=original)
    heard=[adapter.my_wave()['title']]
    for _ in range(12):
        heard.append(adapter._next_background(auto=True)['title'])
    assert len(heard)==13 and player.play.call_count==13
    assert all(a!=b for a,b in zip(heard,heard[1:]))
    assert set(heard)==set('ABCDE') and len(starts)==1
    assert set(adapter._wave_recent_ids)==set('ABCDE')


def test_network_failure_during_history_recovery_never_replays_old_track(local_adapter,monkeypatch):
    adapter,_=local_adapter;played_history(adapter)
    calls=[]
    def batches(hint):
        calls.append(hint)
        if hint:raise TimeoutError('offline')
        return 'ABCDE'
    rotor(adapter,monkeypatch,batches)
    result=adapter.wave_tracks(fresh_start=True)
    assert result['network_unavailable'] and result['tracks']==[]
    assert calls==['','E'] and result['historical_fallback_count']==0


@pytest.fixture
def discord_wave(local_adapter,monkeypatch):
    yandex,_=local_adapter
    adapter=ReliableDiscordAdapter(yandex.config);adapter._yandex=yandex
    profile={'name':'Guild','guild_id':'1','discord_user_id':'2'}
    monkeypatch.setattr(adapter,'_remote_for_any_profile',lambda *_:None)
    monkeypatch.setattr(adapter,'_background_profile',lambda *_:profile)
    monkeypatch.setattr(adapter,'_prefetch_queue_entry',lambda *_:None)
    monkeypatch.setattr(adapter,'_prefetch_next_queue_item',lambda *_:None)
    monkeypatch.setattr(adapter,'_start_queue_monitor',lambda *_:None)
    monkeypatch.setattr(adapter,'_broadcast_queue_snapshot',lambda *_,**__:None)
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth_background',lambda *_:None)
    status={'connected':True,'playing':False,'paused':False}
    actions=[];songs=[]
    def action(profile,name,**kw):
        actions.append(name)
        if name=='pause':status.update(playing=False,paused=True)
        elif name=='resume':status.update(playing=True,paused=False)
        elif name=='stop':status.update(playing=False,paused=False)
        return {**status,'verified':True}
    def play_url(profile,url,**kw):
        songs.append(kw['title']);status.update(playing=True,paused=False)
        return {'verified':True,'playback_generation':len(songs),'expected_duration_ms':180000}
    bot=SimpleNamespace(voice_action=action,play_url=play_url,playback_status=lambda *_:dict(status))
    monkeypatch.setattr(adapter,'_background_for_profile',lambda *_:bot)
    monkeypatch.setattr(yandex,'prepare_playback_candidates_for_track',lambda track,**_:[{'id':track.id,'source':'https://audio.invalid/'+track.id,'title':track.title,'artist':'Artist'}])
    feedback=[]
    monkeypatch.setattr(yandex,'submit_wave_feedback',lambda *a,**kw:feedback.append((a,kw)))
    return adapter,yandex,profile,status,actions,songs,feedback


def test_discord_starts_same_recent_pool_and_sends_actual_batch_feedback_once(discord_wave,monkeypatch):
    adapter,yandex,profile,status,actions,songs,feedback=discord_wave
    played_history(yandex);calls,starts=rotor(yandex,monkeypatch,'ABCDE')
    first=adapter.music_my_wave()
    assert first['title']=='A' and songs==['A']
    assert feedback[0][0]==('trackStarted','A')
    assert feedback[0][1]['batch_id']=='batch-initial'
    second=adapter.music_my_wave()
    assert second['unchanged'] and songs==['A'] and len(starts)==1
    snapshot=adapter._local_queue_snapshot(profile)
    assert [x['track_id'] for x in snapshot['queue']]==list('BCD')


def test_discord_next_uses_same_session_feedback_and_real_queue(discord_wave,monkeypatch):
    adapter,yandex,profile,status,actions,songs,feedback=discord_wave
    played_history(yandex);calls,starts=rotor(yandex,monkeypatch,'ABCDE')
    adapter.music_my_wave()
    for _ in range(7):adapter.music_skip()
    assert len(songs)==8 and all(a!=b for a,b in zip(songs,songs[1:]))
    assert len(starts)==1
    assert [a[0] for a,_ in feedback][:3]==['trackStarted','skip','trackStarted']
    completed=[a[1] for a,_ in feedback if a[0]=='skip']
    assert completed==songs[:-1]
    assert len([1 for a,_ in feedback if a[0]=='trackStarted'])==len(songs)


def test_discord_stop_preserves_wave_and_repeat_request_resumes_current(discord_wave,monkeypatch):
    adapter,yandex,profile,status,actions,songs,feedback=discord_wave
    rotor(yandex,monkeypatch,'ABCDE');adapter.music_my_wave()
    stopped=adapter.execute('music_stop')
    assert stopped['wave_preserved'] and actions[-1]=='pause'
    assert not status['playing'] and status['paused']
    resumed=adapter.music_my_wave()
    assert resumed['unchanged'] and songs==['A'] and actions[-1]=='resume'
    assert len(feedback)==1


def test_discord_stop_cancels_pending_wave_start(discord_wave,monkeypatch):
    adapter,yandex,profile,status,actions,songs,feedback=discord_wave
    entered=threading.Event();release=threading.Event()
    original=rotor(yandex,monkeypatch,'ABCDE')
    real=yandex.wave_tracks
    def delayed(**kw):
        entered.set();assert release.wait(2)
        return real(**kw)
    monkeypatch.setattr(yandex,'wave_tracks',delayed)
    result=[]
    worker=threading.Thread(target=lambda:result.append(adapter.music_my_wave()))
    worker.start();assert entered.wait(1)
    adapter.execute('music_stop');release.set();worker.join(2)
    assert not worker.is_alive() and result[0]['cancelled']
    assert songs==[] and feedback==[]


def test_discord_refill_reads_once_from_completed_context_and_does_not_restart(discord_wave,monkeypatch):
    adapter,yandex,profile,status,actions,songs,feedback=discord_wave
    rotor(yandex,monkeypatch,'ABCDE');adapter.music_my_wave()
    state=adapter._queue_state(profile)
    state['source_data']['last_completed_id']='Z'
    wave=Mock(return_value={'tracks':[]})
    monkeypatch.setattr(yandex,'wave_tracks',wave)
    method=ReliableDiscordAdapter._ensure_wave_queue_depth_background.__get__(adapter)
    method(profile,8)
    deadline=time.monotonic()+2
    while state.get('wave_background_refill_active') and time.monotonic()<deadline:time.sleep(.001)
    for _ in range(10):method(profile,8)
    assert wave.call_count==1
    assert wave.call_args.kwargs['queue_track_id']=='Z' and wave.call_args.kwargs['fresh_start'] is False
    assert {'A','B','C','D','E'} <= wave.call_args.kwargs['avoid_ids']
    assert songs==['A'] and len(feedback)==1


def test_external_feedback_lane_does_not_mutate_desktop_station(local_adapter,monkeypatch):
    adapter,_=local_adapter
    adapter._station='likes:library';adapter._station_batch_id='desktop-batch'
    monkeypatch.setattr(adapter,'_api_enabled',lambda:True)
    events=[];adapter._feedback_lane.close()
    adapter._feedback_lane=SimpleNamespace(submit=lambda e:events.append(e) or True)
    adapter.submit_wave_feedback('trackStarted','D',batch_id='discord-batch')
    assert (adapter._station,adapter._station_batch_id)==('likes:library','desktop-batch')
    assert events[0]['station']=='user:onyourwave' and events[0]['batch_id']=='discord-batch'
    assert events[0]['type']=='trackStarted' and events[0]['track_id']=='D'


def test_discord_stop_at_source_commit_gap_cannot_autoplay(discord_wave,monkeypatch):
    adapter,yandex,profile,status,actions,songs,feedback=discord_wave
    rotor(yandex,monkeypatch,'ABCDE')
    real=adapter._begin_personal_source
    def intervening_stop(*args,**kwargs):
        adapter.execute('music_stop')
        return real(*args,**kwargs)
    monkeypatch.setattr(adapter,'_begin_personal_source',intervening_stop)
    result=adapter.music_my_wave()
    assert result['cancelled'] and songs==[] and feedback==[]
    assert not adapter._queue_state(profile).get('current')


def test_discord_stop_while_preparing_source_cannot_autoplay(discord_wave,monkeypatch):
    adapter,yandex,profile,status,actions,songs,feedback=discord_wave
    rotor(yandex,monkeypatch,'ABCDE')
    real=yandex.prepare_playback_candidates_for_track
    def stop_during_prepare(*args,**kwargs):
        adapter.execute('music_stop')
        return real(*args,**kwargs)
    monkeypatch.setattr(yandex,'prepare_playback_candidates_for_track',stop_during_prepare)
    result=adapter.music_my_wave()
    assert result['cancelled'] and songs==[] and feedback==[]
    assert not adapter._queue_state(profile).get('current')


def test_discord_pause_time_is_excluded_from_rotor_listening_feedback(discord_wave,monkeypatch):
    adapter,yandex,profile,status,actions,songs,feedback=discord_wave
    rotor(yandex,monkeypatch,'ABCDE');adapter.music_my_wave()
    state=adapter._queue_state(profile);now=time.monotonic()
    state['current']['started_at']=now-5
    adapter.execute('music_pause')
    # Simulate a minute on pause, with 5 s of real music before and after it.
    state['current']['started_at']-=60
    state['current']['_pause_started_at']-=60
    adapter.execute('music_resume')
    state['current']['started_at']-=5
    adapter.music_skip()
    completed=[kw for args,kw in feedback if args[0]=='skip']
    assert len(completed)==1 and 9.8<=completed[0]['seconds']<=10.5


def test_discord_late_paused_snapshot_after_stop_never_resumes(discord_wave,monkeypatch):
    adapter,yandex,profile,status,actions,songs,feedback=discord_wave
    rotor(yandex,monkeypatch,'ABCDE');adapter.music_my_wave();adapter.execute('music_pause')
    real=adapter._local_queue_snapshot
    def delayed_snapshot(profile):
        snapshot=real(profile)
        adapter.execute('music_stop')
        return snapshot
    monkeypatch.setattr(adapter,'_local_queue_snapshot',delayed_snapshot)
    result=adapter.music_my_wave()
    assert result['cancelled'] and actions[-1]=='pause' and 'resume' not in actions
    assert status['paused'] and not status['playing']


def test_discord_paused_history_still_supports_previous(discord_wave,monkeypatch):
    adapter,yandex,profile,status,actions,songs,feedback=discord_wave
    rotor(yandex,monkeypatch,'ABCDE');adapter.music_my_wave()
    adapter.execute('music_pause');adapter.execute('music_resume')
    adapter.music_skip();result=adapter.music_previous()
    assert result['title']=='A' and songs==['A','B','A']


@pytest.mark.parametrize('operation',['skip','previous','automatic'])
def test_discord_stop_cancels_pending_transition(discord_wave,monkeypatch,operation):
    adapter,yandex,profile,status,actions,songs,feedback=discord_wave
    rotor(yandex,monkeypatch,'ABCDE');adapter.music_my_wave()
    if operation=='previous':adapter.music_skip()
    before=list(songs)
    real=yandex.prepare_playback_candidates_for_track
    def stopped_prepare(*args,**kwargs):
        adapter.execute('music_stop')
        return real(*args,**kwargs)
    monkeypatch.setattr(yandex,'prepare_playback_candidates_for_track',stopped_prepare)
    if operation=='automatic':
        state=adapter._queue_state(profile)
        adapter._wave_event(state,state['current'],'trackFinished');state['current']=None
        adapter._advance_queue(adapter._profile_key(profile))
    else:
        result=getattr(adapter,'music_'+operation)()
        assert result['cancelled']
    assert songs==before and not status['playing']
    assert not adapter._queue_state(profile).get('current')


def test_discord_refill_deduplicates_recording_across_different_ids(discord_wave,monkeypatch):
    adapter,yandex,profile,status,actions,songs,feedback=discord_wave
    rotor(yandex,monkeypatch,'ABCDE');adapter.music_my_wave()
    duplicate={'id':'other-id','title':'B','artist':'Artist'}
    fresh={'id':'new-id','title':'New real song','artist':'Artist'}
    monkeypatch.setattr(yandex,'wave_tracks',lambda **_:{'tracks':[duplicate,fresh],'batch_id':'later'})
    method=ReliableDiscordAdapter._ensure_wave_queue_depth_background.__get__(adapter)
    method(profile,8);state=adapter._queue_state(profile)
    deadline=time.monotonic()+2
    while state.get('wave_background_refill_active') and time.monotonic()<deadline:time.sleep(.001)
    assert [x['track_id'] for x in state['items']]==['B','C','D','E','new-id']


def test_old_wave_return_after_stop_and_new_play_does_not_stop_new_owner(discord_wave,monkeypatch):
    adapter,yandex,profile,status,actions,songs,feedback=discord_wave
    rotor(yandex,monkeypatch,'ABCDE')
    bot=adapter._background_for_profile(profile);real=bot.play_url
    def replaced_while_playing(profile,url,**kwargs):
        data=real(profile,url,**kwargs)
        status['playback_generation']=data['playback_generation']
        if kwargs['title']=='A':
            adapter.execute('music_stop')
            fresh=adapter._wave_entries({'tracks':[Track('F')],'batch_id':'fresh'})
            serial=adapter._queue_state(profile)['manual_action_serial']
            new=adapter._begin_personal_source(profile,fresh,mode='wave',title='Моя волна',expected_manual_serial=serial)
            assert new['title']=='F'
        return data
    monkeypatch.setattr(bot,'play_url',replaced_while_playing)
    result=adapter.music_my_wave()
    assert result['cancelled'] and songs==['A','F']
    assert status['playing'] and adapter._queue_state(profile)['current']['track_id']=='F'
    assert actions.count('stop')==1
    assert [a for a,_ in feedback]==[('trackStarted','F')]


def test_ambiguous_native_start_error_never_replays_or_uses_direct_fallback(local_adapter,monkeypatch):
    adapter,player=local_adapter;adapter.config.values['integrations.yandex_music.enabled']=True
    monkeypatch.setattr(adapter,'_api_client',lambda:object())
    resolver=Mock(return_value={'tracks':[Track('A'),Track('B')],'batch_id':'b'})
    monkeypatch.setattr(adapter,'wave_tracks',resolver)
    direct=Mock();monkeypatch.setattr(adapter,'_station_tracks',direct)
    audible=[]
    def uncertain_start(track,**kw):
        audible.append(track.id)
        raise RuntimeError('native acknowledgement lost after audible start')
    monkeypatch.setattr(adapter,'_play_track_object',uncertain_start)
    with pytest.raises(RuntimeError,match='Не удалось включить'):
        adapter.my_wave()
    assert audible==['A'] and resolver.call_count==1 and not direct.called
