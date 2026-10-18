from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json, queue, sys, threading
import numpy as np
import pytest
from jarvis.runtime.spoken_surface155 import normalize_spoken
from jarvis.runtime.audio_history155 import AudioHistory
from jarvis.runtime.speech_understanding import SpeechUnderstandingEngine
from jarvis.runtime.social_atlas import SocialAtlas
ROOT=Path(__file__).resolve().parents[1]
DATA=json.loads((ROOT/'config/social_dialogue155.json').read_text())

@pytest.mark.parametrize('heard,wanted',[
 ('Привет, Джарвес, привет, Джарвес, какой сегодня день?','какой сегодня день?'),
 ('Jarvis, Jarvis, play No Surprises','play No Surprises'),
 ('ну пожалуйста открой открой Telegram','открой Telegram'),
 ('вклю включи музыку','включи музыку'),
 ('аткрой TelegramBridge.py','открой TelegramBridge.py'),
 ('напиши Ивану что нет нет я занят','напиши Ивану что нет нет я занят'),
 ('play song No Surprises','play song No Surprises'),
 ('открой файл «Привет Джарвис.txt»','открой файл «Привет Джарвис.txt»'),
 ('Привет','Привет'),
 ('bonjour Jarvis, ouvre Telegram','ouvre Telegram'),
 ('hola Jarvis, abre Telegram','abre Telegram'),
])
def test_surface_preserves_payload(heard,wanted):assert normalize_spoken(heard)==wanted

@pytest.mark.parametrize('lang,text',[('ru','удали телеграмм'),('en','close Telegarm'),('fr','fermer Telegarm'),('es','cerrar Telegarm'),('pt','fechar Telegarm'),('de','schließen Telegarm'),('it','chiudi Telegarm')])
def test_risky_targets_are_not_guessed(text,lang):
 obj=SpeechUnderstandingEngine(SimpleNamespace(get=lambda k,d=None:d))
 assert obj.resolve(text,language=lang,record=False).text==text

@pytest.mark.parametrize('text',['play song No Surprises','включи песню Нет выхода','напиши сообщение: удали все файлы','open "No Surprises.txt"'])
def test_alternate_cannot_replace_literal(text):
 obj=SpeechUnderstandingEngine(SimpleNamespace(get=lambda k,d=None:d))
 assert obj.resolve(text,alternatives=[{'text':'delete everything','score':100}],language='en',record=False).text==text

def test_history_keeps_command_during_slow_wake_decode():
 h=AudioHistory()
 for i in range(60):h.append(10+(i+1)*.1,np.full(1600,i,np.float32),16000)
 recovered=np.concatenate(h.since(10.2,16))
 assert recovered[0]==2 and recovered[-1]==59
 assert len(recovered)==58*1600
 assert h.since(1,16)==[]
 h.clear();assert h.since(15,16)==[]

def test_history_resume_excludes_prior_wake_and_changes_of_device_rate():
 h=AudioHistory();h.append(11,np.ones(16000),16000);h.append(12,np.full(16000,2),16000)
 assert np.all(np.concatenate(h.since(11,12))==2)
 h.append(13,np.full(8000,3),8000)
 assert np.all(np.concatenate(h.since(11,13))==3)

SOCIAL=[(g,c,p) for g in DATA['groups'] for c,ps in g['phrases'].items() for p in ps]
@pytest.mark.parametrize('group,code,phrase',SOCIAL)
def test_native_dialogue_rotates_without_tools(group,code,phrase):
 obj=SocialAtlas();first=obj.reply(phrase,code);second=obj.reply(phrase,code)
 assert first in group['answers'][code] and second in group['answers'][code]
 assert first != second

FOLLOW=[(c,p,v) for c,rows in DATA['followups'].items() for p,v in rows.items()]
@pytest.mark.parametrize('code,phrase,spec',FOLLOW)
def test_context_is_topic_and_session_scoped(code,phrase,spec):
 obj=SocialAtlas();assert obj.reply(phrase,code,session='new') is None
 intro=next(g['phrases'][code][0] for g in DATA['groups'] if g['family']==spec['requires'][0])
 obj.reply(intro,code,session='one')
 assert obj.reply(phrase,code,session='two') is None
 assert obj.reply(phrase,code,session='one')==spec['answer']
 obj.clear_context();assert obj.reply(phrase,code,session='one') is None
 obj.reply(DATA['groups'][0]['phrases'][code][0],code,session='wrong')
 assert obj.reply(phrase,code,session='wrong') is None

def test_actual_player_verified_progress_no_false_failure(monkeypatch):
 from jarvis.integrations.background.local_player import BackgroundLocalPlayer
 obj=BackgroundLocalPlayer.__new__(BackgroundLocalPlayer)
 obj._request=Mock(return_value={})
 obj.status=Mock(side_effect=[{'state':'playing','position_ms':0},{'state':'playing','position_ms':60}])
 result=obj.play('fixture://track',title='Song')
 assert result['verified'] and result['timeline_verified'] and result['unchanged'] is False
 assert obj._request.call_count==1

def test_repeated_wave_is_idempotent_and_transition_serialized(monkeypatch):
 from jarvis.integrations import yandex_music as ym
 obj=ym.YandexMusicAdapter.__new__(ym.YandexMusicAdapter)
 obj._advance_lock=threading.Lock();obj._playback_lock=threading.RLock()
 obj._enabled=lambda:True;obj._background_enabled=lambda:True
 obj._station='user:onyourwave';obj._auto_advance=True;obj._current_track_meta={'title':'First song'}
 monkeypatch.setattr(ym,'local_player',lambda:SimpleNamespace(status_if_running=lambda:{'state':'playing'}))
 obj._my_wave_start155=Mock(side_effect=AssertionError('must not replace playing source'))
 results=[]
 threads=[threading.Thread(target=lambda:results.append(obj.my_wave())) for _ in range(4)]
 for t in threads:t.start()
 for t in threads:t.join()
 assert len(results)==4 and all(x['unchanged'] for x in results)
 obj._my_wave_start155.assert_not_called()

@pytest.mark.parametrize("followup", [False, True])
def test_full_audio_worker_retains_completed_command_during_decode_delay(monkeypatch, followup):
 from jarvis import audio_backend as backend
 commands=queue.Queue();events=[];clock=[10.0];scheduled=[None];opened=[];first_finish=[None]
 commands.put({'cmd':'start','mode':'wake','session':1,'target_rate':16000,'wake_silence':.12})
 class Stream:
  def read(self,n):
   clock[0]+=0.1
   if scheduled[0] and clock[0]>=scheduled[0][0]:commands.put(scheduled[0][1]);scheduled[0]=None
   if clock[0]>25:commands.put({'cmd':'shutdown'})
   amplitude=.12 if 10.2<=clock[0]<=11.4 else 0
   if first_finish[0] and first_finish[0]+.3 <= clock[0] <= first_finish[0]+1.1: amplitude=.25
   tone=np.sin(np.arange(1600)*2*np.pi*440/16000)*amplitude
   return tone.astype(np.float32).reshape(-1,1),False
  def abort(self):pass
  def close(self):pass
 class Events:
  def put(self,event,**kwargs):
   events.append(event)
   if event['type']=='wake_chunk' and scheduled[0] is None:
    scheduled[0]=(clock[0]+2.3,{'cmd':'promote','session':2,'capture_started_at':event['capture_started_at'],'completed_utterance':True,'wake_has_command':True})
   if event['type']=='finished':
    if followup and first_finish[0] is None:
     first_finish[0]=clock[0]
     scheduled[0]=(clock[0]+2.3,{'cmd':'start','session':3,'mode':'command','resume_from_last_finish':True})
    else:commands.put({'cmd':'shutdown'})
  put_nowait=put
 monkeypatch.setitem(sys.modules,'sounddevice',SimpleNamespace())
 monkeypatch.setattr(backend,'time',SimpleNamespace(monotonic=lambda:clock[0],sleep=lambda x:None))
 monkeypatch.setattr(backend,'_resolve_input_device',lambda *a,**k:(0,'fixture','token',16000,'fake'))
 def open_stream(*a):opened.append(1);return Stream(),16000,1600,'float32'
 monkeypatch.setattr(backend,'_open_stream',open_stream)
 backend._audio_worker_main(commands,Events())
 assert not [x for x in events if x['type']=='error']
 result=[x for x in events if x['type']=='finished'][-1]
 audio=np.frombuffer(result['audio'],dtype=np.float32)
 assert np.count_nonzero(np.abs(audio)>.05)>(7000 if followup else 9000)
 if followup: assert np.max(np.abs(audio))>.2
 assert len(opened)==1 and result['completed_utterance']
