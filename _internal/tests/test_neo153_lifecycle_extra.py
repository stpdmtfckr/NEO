"""Exercise real lifecycle methods without importing a Qt/native audio backend."""
from __future__ import annotations
import ast
from datetime import datetime
import json
import logging
from pathlib import Path
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from tests.test_neo153_response_voice import extracted
from jarvis.quick_media_commands import QuickMediaCommand
from jarvis.runtime.briefing_stream import stream_to
from jarvis.tools.registry import ToolExecution


def test_cancelled_rolling_capture_cannot_switch_track():
 ns,name=extracted('_wake_transcribed');ns['RecordingResult']=object
 window=SimpleNamespace(_quitting=False,_wake_generation=5,
  _probable_system_playback_echo=lambda *a,**k:False,
  _dispatch_quick_media_command=Mock(side_effect=AssertionError('stale command')),
  _output_stop_from_voice=Mock(side_effect=AssertionError('stale stop')))
 ns[name](window,{'fast_command':QuickMediaCommand(action='next',target='auto',heard='дальше')},4)
 window._dispatch_quick_media_command.assert_not_called()
 window._output_stop_from_voice.assert_not_called()


def test_non_user_source_cannot_stop_or_start_response():
 ns,name=extracted('_send_text')
 from jarvis.runtime.input_provenance import normalize_input_source,is_executable_input_source
 ns.update(normalize_input_source=normalize_input_source,is_executable_input_source=is_executable_input_source)
 w=SimpleNamespace(_response_active=Mock(side_effect=AssertionError('must check source first')))
 ns[name](w,'стоп',input_source='system_playback');w._response_active.assert_not_called()


def test_raw_short_queue_bypasses_expensive_and_mutating_asr_repair():
 ns,name=extracted('_normalize_recognized_command')
 window=SimpleNamespace(_language_code=lambda:'ru',speech_understanding=Mock(),config=Mock())
 assert ns[name](window,'очередь дискорд')=='очередь дискорд'
 window.config.get.assert_not_called();window.speech_understanding.resolve.assert_not_called()


def test_briefing_handler_publishes_date_before_slow_reads_finish(tmp_path):
 r=Path(__file__).resolve().parents[1]
 tree=ast.parse((r/'jarvis/tools/integrations.py').read_text())
 fn=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='briefing')
 values={'app.language':'ru','briefing.include_weather':True,'integrations.openweather.enabled':True,
         'integrations.openweather.city':'TestCity','briefing.include_system':False,
         'briefing.include_devices':False,'briefing.include_bluetooth':False}
 cfg=SimpleNamespace(get=lambda k,d=None:values.get(k,d),paths=SimpleNamespace(root=tmp_path))
 started=threading.Event();release=threading.Event();date_seen=threading.Event();events=[];out=[]
 def weather():
  started.set();release.wait(2)
  return 'Weather result',{'temperature':12}
 store=SimpleNamespace(startup_already_delivered=lambda now:False,mark_startup_delivered=Mock())
 ns={'config':cfg,'language_code':lambda c:'ru','datetime':datetime,'Any':object,'ToolExecution':ToolExecution,
     'time':time,'uuid':uuid,'logger':logging.getLogger(__name__),
     'DailyBriefingStore':lambda c:store,'greeting_and_wish':lambda c,n:'Date and time now',
     'weather_adapter':SimpleNamespace(answer=lambda action:weather())}
 exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'briefing','exec'),ns)
 def publish(event):
  events.append(event)
  if event['section']=='greeting':date_seen.set()
 def run():
  with stream_to(publish):out.append(ns['briefing']({'action':'startup_summary'}))
 thread=threading.Thread(target=run,daemon=True);thread.start()
 try:
  assert date_seen.wait(1)
  assert events[0]['text']=='Date and time now'
  assert started.wait(1) and not out
 finally:
  release.set();thread.join(2)
 assert not thread.is_alive() and out[0].success
 assert out[0].data['section_status']['weather']=='ready'
 assert next(event for event in events if event['section']=='weather' and event['state']=='ready')['text']=='Weather result'
 assert events[-1]['section']=='completion' and events[-1]['text']=='Сводка готова.'
 # Tool completion is not evidence that a GUI ever displayed the briefing.
 # Release 158 records delivery only in the receiving window's completion slot.
 store.mark_startup_delivered.assert_not_called()
