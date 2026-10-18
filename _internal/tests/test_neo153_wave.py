"""Actual adapter, fake API boundary. Never erase recent tracks to force success."""
from types import SimpleNamespace,ModuleType
import sys
import pytest
from tests.test_media_reliability import local_adapter,Track

def test_rotor_request_retains_settings2_when_continuing(local_adapter,monkeypatch):
 adapter,_=local_adapter;calls=[]
 class Client:
  __module__='yandex_music.client'
  base_url='https://api.example.invalid'
 client=Client();client.request=SimpleNamespace(get=lambda *a,**kw:calls.append((a,kw)) or {'sequence':[{'track':{'id':'11','title':'New'}}],'batchId':'batch'})
 monkeypatch.setattr(adapter,'_api_client',lambda:client)
 module=ModuleType('yandex_music');module.StationTracksResult=SimpleNamespace(de_json=lambda raw,c:raw)
 monkeypatch.setitem(sys.modules,'yandex_music',module)
 tracks,batch=adapter._station_tracks('user:onyourwave',queue_track_id='10')
 assert calls[0][1]['params']=={'settings2':'True','queue':'10'}
 assert calls[0][1]['timeout']==5.0
 assert tracks[0]['id']=='11' and batch=='batch'

def test_filtered_batch_preserves_history_without_unplayed_cursor_feedback(local_adapter,monkeypatch):
 adapter,_=local_adapter;calls=[];starts=[]
 monkeypatch.setattr(adapter,'_api_client',lambda:SimpleNamespace(rotor_station_feedback_radio_started=lambda *a,**kw:starts.append((a,kw))))
 adapter.config.values['integrations.yandex_music.wave_repeat_policy']='strict'
 monkeypatch.setattr(adapter,'_wave_repeat_floor',lambda:({'1','2'},set()))
 monkeypatch.setattr(adapter,'_wave_recent_sets',lambda **kw:({'1','2'},set()))
 def fetch(station,queue_track_id=''):
  calls.append(queue_track_id)
  return ([Track('1'),Track('2')] if not queue_track_id else [Track('3')]),'b'
 monkeypatch.setattr(adapter,'_station_tracks',fetch)
 result=adapter.wave_tracks(fresh_start=True,fast_start=True)
 assert calls==[''] and result['tracks']==[]
 assert result['recent_filtered']==2 and result['repeat_floor_enforced']
 assert result['reason']=='recent_tracks_filtered' and len(starts)==1

def test_all_recent_is_reported_as_filtered_not_fabricated_api_empty(local_adapter,monkeypatch):
 adapter,_=local_adapter
 monkeypatch.setattr(adapter,'_api_client',lambda:SimpleNamespace())
 adapter.config.values['integrations.yandex_music.wave_repeat_policy']='strict'
 monkeypatch.setattr(adapter,'_wave_repeat_floor',lambda:({'1','2'},set()))
 monkeypatch.setattr(adapter,'_wave_recent_sets',lambda **kw:({'1','2'},set()))
 monkeypatch.setattr(adapter,'_station_tracks',lambda *a,**kw:([Track('1'),Track('2')],'b'))
 result=adapter.wave_tracks(fresh_start=True,fast_start=True)
 assert result['tracks']==[] and result['reason']=='recent_tracks_filtered'
 assert result['probe_count']<=6

def test_true_network_or_empty_is_not_reported_as_recent_filter(local_adapter,monkeypatch):
 adapter,_=local_adapter
 monkeypatch.setattr(adapter,'_api_client',lambda:SimpleNamespace())
 def fail(*a,**kw):raise TimeoutError('not exported')
 monkeypatch.setattr(adapter,'_station_tracks',fail)
 result=adapter.wave_tracks(fresh_start=True)
 assert not result['tracks'] and result['reason']=='api_error_or_empty'
 assert result['errors']==['TimeoutError']
