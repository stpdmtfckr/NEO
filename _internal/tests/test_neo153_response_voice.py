"""Answer lifecycle: the strict voice «стоп», interruption and the briefing stream.

NEO 179: NEO no longer speaks; the speech segmentation/playback tests are gone."""
from __future__ import annotations
import ast
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import logging
import threading
import time
import pytest
from jarvis.runtime.answer_stop179 import stop_phrase,STOPPED
from jarvis.ui.response_output import ResponseOutputMixin
from jarvis.runtime.briefing_stream import collect,stream_to,emit

@pytest.mark.parametrize('code,text',[('ru','стоп'),('ru','Стоп!'),('ru','Нео, стоп'),('ru','Нео останови ответ'),('en','stop the answer'),('de','stopp'),('fr','arrête la réponse'),('es','alto'),('it','fermati'),('pt','pare'),('ru','стоп стоп')])
def test_interrupt_phrases_are_whole_utterances(code,text):
 assert stop_phrase(text,code,('Нео','NEO'))

@pytest.mark.parametrize('text',['не говори стоп','я сказал стоп вчера','напиши слово стоп','останови музыку','стоп-кран','"стоп"','«стоп»','stopwatch','stop the music','хватит времени на таймер'])
def test_interrupt_never_means_substring(text):
 assert not stop_phrase(text,'ru',('Нео','Jarvis'))

def extracted(name):
 path=Path(__file__).resolve().parents[1]/'jarvis/ui/main_window.py'
 cls=next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.ClassDef) and n.name=='MainWindow')
 fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name==name)
 namespace={'logger':logging.getLogger('test153'),'time':time,'Any':object}
 exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),str(path),'exec'),namespace)
 return namespace,name

def test_stop_preserves_the_visible_prefix_and_drops_pending_deltas(monkeypatch):
 ns,name=extracted('_interrupt_current_operation')
 manager=SimpleNamespace(cancel=Mock());ns['cancellation_manager']=lambda:manager
 ns['QTimer']=SimpleNamespace(singleShot=lambda *a:None)
 shown={'bubble':'Уже написанная часть'};added=[];audio=SimpleNamespace(is_recording=True,stop=Mock())
 chat=SimpleNamespace(collapse_thinking=Mock(),update_bubble=Mock(side_effect=AssertionError('must not replace text')))
 timer=lambda:SimpleNamespace(stop=Mock())
 w=SimpleNamespace(_cancel_command_asr157=Mock(),_briefing_cancel=threading.Event(),_briefing_generation=5,
   _ambient_typing_timer=timer(),_ambient_typing_queue=deque(['remaining']),_ambient_typing_bubble=object(),
   agent=SimpleNamespace(ollama=SimpleNamespace(cancel_active=lambda:0)),_typing_timer=timer(),_stream_flush_timer=timer(),
   _agent_generation=4,_wake_generation=6,audio_capture=audio,input=SimpleNamespace(setEnabled=Mock()),
   send_button=SimpleNamespace(setEnabled=Mock()),settings_button=SimpleNamespace(setEnabled=Mock()),
   _pending_response_bubble=shown,chat=chat,_append_assistant=lambda t,**kw:added.append(t),
   _language_code=lambda:'ru',_normalize_recognized_command=lambda t:t,_is_interrupt_phrase=lambda t:True,
   _set_status=Mock(),robot=SimpleNamespace(set_state=Mock()),_resume_wake_listening=Mock(),_restore_listening_audio=Mock(),
   _restart_idle_timer=Mock())
 ns[name](w,source='voice-stop')
 assert shown=={'bubble':'Уже написанная часть'} and w._pending_response_bubble is None
 assert len(added)==1 and added[0]==STOPPED['ru']
 assert w._agent_generation==5 and w._wake_generation==7
 assert w._briefing_cancel.is_set() and not w._ambient_typing_queue
 assert w._stream_pending_text=='' and w._typing_text==''
 w._cancel_command_asr157.assert_called_once()

class Window(ResponseOutputMixin):pass

def test_only_stop_ends_a_running_answer_not_other_command():
 w=Window();w._busy=True
 w._language_code=lambda:'ru';w.config=SimpleNamespace(get=lambda k,d=None:d);w._interrupt_current_operation=Mock()
 assert not w._output_stop_from_voice('удали файл')
 assert w._output_stop_from_voice('стоп')
 w._interrupt_current_operation.assert_called_once()

def test_briefing_fast_part_does_not_wait_for_slow_part():
 release=threading.Event();finished=threading.Event();events=[];results=[]
 def slow():
  try:
   release.wait(1)
   emit('slow','late data must not update the completed briefing')
   return 'late'
  finally:finished.set()
 started=time.monotonic()
 try:
  with stream_to(events.append):
   emit('greeting','date now')
   results=list(collect({'slow':slow,'fast':lambda:'weather'},timeout=.07))
  assert events[0]['text']=='date now'
  assert results[0]==('fast','ready','weather')
  assert results[1]==('slow','timeout',{'error_type':'DeadlineExceeded','attempts':1})
  assert time.monotonic()-started<.8
  before=list(events)
  release.set()
  assert finished.wait(.8)
  assert events==before, 'A late provider published into the completed briefing'
 finally:release.set()

def test_briefing_cancel_no_late_publishing_and_callback_scope_restored():
 event=threading.Event();event.set();out=[]
 with stream_to(out.append,event):
  emit('weather','do not show')
  result=list(collect({'read':lambda:'value'},timeout=.2))
 emit('other','not in callback')
 assert not out and result==[('read','cancelled',None)]

@pytest.mark.parametrize('code',['ru','en','de','fr','es','it','pt'])
def test_briefing_status_text_is_localized(code):
 from jarvis.runtime.briefing_stream import status_text
 assert status_text('weather',code)!=status_text('weather',code,failed=True)
 if code!='ru':assert 'Получаю' not in status_text('weather',code)
