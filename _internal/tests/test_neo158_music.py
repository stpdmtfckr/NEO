"""Rotor protocol and stream recovery field regressions, using real adapters.

Only account/CDN/audio-device edges are substituted. A separate process test
executes the actual Qt Multimedia worker on a generated WAV.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.test_media_reliability import Track, local_adapter
from tests.test_neo157_music import current, finished, monitor_setup
from jarvis.integrations import yandex_music as ym
from jarvis.runtime.media_events import drain_now_playing


def test_default_history_is_bounded_by_age_and_count(local_adapter):
    adapter,_=local_adapter;now=time.time()
    adapter._wave_recent_entries=[{'id':'yesterday','fingerprint':'old/song','ts':now-13*3600},
                                  {'id':'today','fingerprint':'new/song','ts':now-60}]
    ids,fp=adapter._wave_repeat_floor()
    assert ids=={'today'} and fp=={'new/song'}


@pytest.mark.parametrize('custom',[{'integrations.yandex_music.wave_repeat_policy':'strict'},
                                 {'integrations.yandex_music.wave_no_repeat_hours':24},
                                 {'integrations.yandex_music.wave_no_repeat_tracks':40}])
def test_explicit_or_custom_history_policy_keeps_original_exclusions(local_adapter,custom):
    adapter,_=local_adapter;adapter.config.values.update(custom)
    adapter._wave_recent_entries=[{'id':'older','fingerprint':'old/song','ts':time.time()-48*3600}]
    assert 'older' in adapter._wave_repeat_floor()[0]


def test_rotor_continuation_uses_completed_track_and_awaits_real_feedback(local_adapter,monkeypatch):
    adapter,_=local_adapter;current(adapter);adapter._station_queue=[]
    entered=threading.Event();release=threading.Event();trace=[]
    def send(event):
        trace.append(('feedback',event['type'],event['track_id']))
        entered.set();release.wait(2)
    monkeypatch.setattr(adapter,'_api_enabled',lambda:True)
    monkeypatch.setattr(adapter,'_send_station_feedback_now',send)
    adapter._station_feedback=ym.YandexMusicAdapter._station_feedback.__get__(adapter)
    adapter._station_feedback('skip','previous',seconds=28)
    assert entered.wait(1)
    monkeypatch.setattr(adapter,'_api_client',lambda:object())
    def fetch(station,queue_track_id=''):
        trace.append(('fetch',queue_track_id));return [Track(x) for x in 'BCDEFG'],'b'
    monkeypatch.setattr(adapter,'_station_tracks',fetch)
    adapter._ensure_wave_queue_depth();time.sleep(.03)
    assert not any(x[0]=='fetch' for x in trace)
    release.set();finished(adapter)
    assert trace==[('feedback','skip','previous'),('fetch','previous')]
    assert [x.id for x in adapter._station_queue]==list('BCDEFG')
    adapter._feedback_lane.close()


def test_real_rotor_feedback_sequence_keeps_upcoming_queue_and_batch_origin(local_adapter,monkeypatch):
    adapter,player=local_adapter;adapter.config.values['integrations.yandex_music.enabled']=True
    trace=[];state={'current':'','completed':''}
    class Rotor:
        def rotor_station_feedback_radio_started(self,*a,**kw):trace.append(('radioStarted',''))
        def rotor_station_feedback(self,station,event,**kwargs):
            tid=kwargs.get('track_id','');trace.append((event,tid,kwargs.get('batch_id')))
            if event in {'skip','trackFinished'}:state['completed']=tid
            if event=='trackStarted':state['current']=tid
        def rotor_station_tracks(self,station,settings2=True,queue=None):
            trace.append(('read',queue))
            assert queue in {None,state['completed']}, 'Only actual previous track is a continuation'
            names='BCDEFG' if state['current']=='A' else 'CDEFG' if state['current']=='B' else 'ABCDE'
            return SimpleNamespace(sequence=[SimpleNamespace(track=Track(x)) for x in names],batch_id='batch-'+str(queue))
    client=Rotor();monkeypatch.setattr(adapter,'_api_client',lambda:client)
    monkeypatch.setattr(adapter,'_api_enabled',lambda:True)
    monkeypatch.setattr(ym,'yandex_music_token',lambda config:'fixture-account')
    adapter._client_token='fixture-account';adapter._feedback_client=client
    adapter._feedback_token='fixture-account';adapter._feedback_generation=adapter._network_generation
    adapter._station_feedback=ym.YandexMusicAdapter._station_feedback.__get__(adapter)
    first=adapter.my_wave();finished(adapter)
    assert first['title']=='A' and [x.id for x in adapter._station_queue][:5]==list('BCDEF')
    adapter._wave_refill_retry_at=0
    second=adapter.transport('next');finished(adapter)
    assert second['title']=='B' and adapter._current_track_obj.id=='B'
    assert [x.id for x in adapter._station_queue][:5]==list('CDEFG')
    assert [x[:2] for x in trace if x[0] in {'radioStarted','trackStarted','skip'}]==[
        ('radioStarted',''),('trackStarted','A'),('skip','A'),('trackStarted','B')]
    assert all(x[1] in {None,'A'} for x in trace if x[0]=='read')
    adapter._feedback_lane.close()


def test_queue_status_does_not_wait_for_player_ipc(local_adapter):
    adapter,player=local_adapter;current(adapter)
    player.cached_status=Mock(return_value={'state':'playing'})
    player.status_if_running=Mock(side_effect=AssertionError('UI queue read blocked on IPC'))
    adapter._wave_refill_retry_at=time.monotonic()+100
    result=adapter.queue_status()
    assert result['queue'][0]['id']=='B' and result['player']['state']=='playing'
    player.status_if_running.assert_not_called()


def test_early_eof_with_cleared_qt_duration_recovers_same_track_position(local_adapter,monkeypatch):
    adapter,player=local_adapter;_,tick=monitor_setup(adapter,player,monkeypatch)
    drain_now_playing(128);resumed=[False]
    statuses=[{'state':'playing','media_status':'BufferedMedia','position_ms':45000,'duration_ms':180000},
              {'state':'stopped','media_status':'EndOfMedia','position_ms':0,'duration_ms':0}]
    def status():
        if resumed[0]:return {'state':'playing','media_status':'BufferedMedia','position_ms':46000,'duration_ms':180000}
        return statuses.pop(0) if statuses else {'state':'stopped','media_status':'EndOfMedia','position_ms':0,'duration_ms':0}
    player.status_if_running=status
    def recover(*a,**k):resumed[0]=True;return {'state':'playing','verified':True,'timeline_verified':True,'duration_ms':180000}
    player.play=Mock(side_effect=recover);player.seek=Mock(return_value={'seek_applied':True})
    next_call=Mock();monkeypatch.setattr(adapter,'_next_background',next_call)
    adapter._station_monitor()
    assert player.play.call_count==1 and not next_call.called
    assert player.seek.call_args.args==(45000,) and adapter._current_track_obj.id=='A'


def test_qt_network_enum_recovers_even_if_localized_error_text_is_unknown(local_adapter,monkeypatch):
    adapter,player=local_adapter;monitor_setup(adapter,player,monkeypatch);recovered=[False]
    player.status_if_running=lambda: {'state':'playing','position_ms':45000,'duration_ms':180000} if recovered[0] else {
        'state':'stopped','media_status':'InvalidMedia','error_kind':'network','error':'Transport fermé','position_ms':45000,'duration_ms':180000}
    def recover(*a,**k):recovered[0]=True;return {'state':'playing','verified':True,'duration_ms':180000}
    player.play=Mock(side_effect=recover);player.seek=Mock(return_value={'seek_applied':False})
    next_call=Mock();monkeypatch.setattr(adapter,'_next_background',next_call)
    adapter._station_monitor()
    assert player.play.call_count==1 and not next_call.called


def test_real_qt_player_worker_progresses_ends_and_preserves_network_error_enum(tmp_path):
    pytest.importorskip('PySide6')
    # Separate QCoreApplication avoids accidental reuse of another test's widgets.
    script=r'''
import json,math,struct,sys,time,wave
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from PySide6.QtMultimedia import QMediaPlayer
from jarvis.integrations.background.local_player_worker import PlayerBridge
app=QCoreApplication([]);bridge=PlayerBridge(app);bridge.audio.setVolume(0);path=Path(sys.argv[1])
with wave.open(str(path),'wb') as w:
 w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000)
 w.writeframes(b''.join(struct.pack('<h',int(1000*math.sin(i*.1))) for i in range(32000)))
bridge._handle({'action':'play','url':str(path),'title':'Generated tone'})
positions=[];states=[];deadline=time.monotonic()+5;seeked=False
while time.monotonic()<deadline:
 app.processEvents();s=bridge.snapshot();positions.append(s['position_ms']);states.append(s['media_status'])
 if s['seekable'] and s['position_ms']>=300 and not seeked:
  bridge._handle({'action':'seek','position_ms':1200});seeked=True
 if 'endofmedia' in s['media_status']:break
 time.sleep(.005)
assert seeked and max(positions)>=1800 and 'endofmedia' in states,(positions[-5:],states[-5:])
bridge._on_error(QMediaPlayer.Error.NetworkError,'Transport fermé')
assert bridge.snapshot()['error_kind']=='network'
bridge.player.stop();print(json.dumps({'real_qt_progress':True,'seek':seeked,'end':True}))
'''
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path/'generated.wav')],capture_output=True,text=True,timeout=12,env=dict(os.environ,QT_QPA_PLATFORM='offscreen'))
    assert result.returncode==0,result.stderr+result.stdout
    assert json.loads(result.stdout.strip().splitlines()[-1])=={'real_qt_progress':True,'seek':True,'end':True}


def test_partial_queue_growth_updates_existing_report(local_adapter,monkeypatch):
    adapter,_=local_adapter;current(adapter);adapter._station_queue=[Track('B')]
    drain_now_playing(128)
    assert adapter.mark_queue_reported('A',True,known_count=1) is None
    monkeypatch.setattr(adapter,'wave_tracks',lambda **_: {'tracks':[Track(x) for x in 'CDEFG']})
    adapter._ensure_wave_queue_depth();finished(adapter)
    events=drain_now_playing(128)
    assert [x.reason for x in events]==['queue_updated'] and events[0].track_id=='A'
    assert adapter._queue_reported_count==5


def controller_for_health(monkeypatch):
    from jarvis.integrations.background.local_player import BackgroundLocalPlayer
    player=BackgroundLocalPlayer()
    process=Mock();process.poll.return_value=None
    player._process=process;player._worker_generation=7;player._port=4567
    close=Mock();monkeypatch.setattr(player,'close',close)
    return player,process,close


def test_worker_health_never_restarts_responsive_player(monkeypatch):
    player,_,close=controller_for_health(monkeypatch)
    player._status_failures=8
    monkeypatch.setattr(player,'status_if_running',lambda: {'state':'stopped','error_kind':'network'})
    assert not player.recreate_if_unresponsive() and not close.called


def test_worker_health_does_not_kill_replacement_generation(monkeypatch):
    player,_,close=controller_for_health(monkeypatch)
    def probe():
        player._status_failures+=1
        player._worker_generation+=1
        player._process=Mock(poll=lambda:None)
        return None
    monkeypatch.setattr(player,'status_if_running',probe)
    assert not player.recreate_if_unresponsive() and not close.called


def test_worker_health_rechecks_pause_before_retiring_owned_child(monkeypatch):
    player,_,close=controller_for_health(monkeypatch);checks=[]
    def probe():player._status_failures+=1;return None
    monkeypatch.setattr(player,'status_if_running',probe)
    def guard():
        checks.append(True)
        if len(checks)==3:raise ym.PlaybackSuperseded('pause now owns playback')
    with pytest.raises(ym.PlaybackSuperseded):player.recreate_if_unresponsive(before_restart=guard)
    close.assert_not_called()


def test_worker_health_retires_exact_owned_child_after_two_failed_probes(monkeypatch):
    player,process,close=controller_for_health(monkeypatch)
    def probe():player._status_failures+=1;return None
    monkeypatch.setattr(player,'status_if_running',probe)
    checks=Mock()
    assert player.recreate_if_unresponsive(before_restart=checks)
    close.assert_called_once()
    assert player._process is process and checks.call_count==4


def test_real_qt_http_disconnect_and_owned_worker_recovery():
    pytest.importorskip('PySide6')
    result=subprocess.run([sys.executable,'-m','jarvis.testing.qt_music_fault158'],
        capture_output=True,text=True,timeout=45,env=dict(os.environ,QT_QPA_PLATFORM='offscreen'))
    assert result.returncode==0,result.stderr+result.stdout
    report=json.loads(result.stdout.strip().splitlines()[-1])
    assert report['cut_http_stream'] and report['recovered_timeline']


def test_real_worker_exits_when_owned_parent_lifeline_closes():
    pytest.importorskip('PySide6')
    from jarvis.integrations.background.local_player import BackgroundLocalPlayer
    player=BackgroundLocalPlayer()
    try:
        player.set_volume(0)
        child=player._process
        assert child is not None and child.poll() is None
        child.stdin.close()
        assert child.wait(timeout=4)==0
    finally:
        player.close()


def test_auto_retry_does_not_repeat_ambiguous_finished_feedback(local_adapter,monkeypatch):
    adapter,_=local_adapter;current(adapter)
    sent=[]
    def send(event):
        sent.append((event['type'],event['track_id']))
        raise TimeoutError('acknowledgement lost after server accepted feedback')
    monkeypatch.setattr(adapter,'_send_station_feedback_now',send)
    monkeypatch.setattr(adapter,'_api_enabled',lambda:True)
    adapter._station_feedback=ym.YandexMusicAdapter._station_feedback.__get__(adapter)
    adapter._station_feedback('trackFinished','A',seconds=180)
    adapter._wait_wave_feedback()
    adapter._station_feedback('trackFinished','A',seconds=180)
    adapter._station_feedback('skip','A',seconds=180)
    adapter._wait_wave_feedback()
    assert sent==[('trackFinished','A')]
    adapter._feedback_lane.close()
