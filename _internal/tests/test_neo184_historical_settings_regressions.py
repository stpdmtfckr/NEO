"""Full Qt entry-point regressions; every hardware/network boundary is inert."""
import json, os, threading, time
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from PySide6.QtCore import Qt, QTimer, QCoreApplication, QEvent
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog
from jarvis.ui.main_window import MainWindow
ORIGINALS={name:getattr(MainWindow,name) for name in ('_resume_wake_listening','_pause_wake_listening','_ensure_wake_monitor','_start_interrupt_wake_monitor')}
from tests.test_neo184_settings_mainwindow import app,window,open_settings,settle
from tests.test_neo184_settings import isolated,dialog
import tempfile
_OUTPUT_TMP = tempfile.TemporaryDirectory(prefix='neo-settings-qa-') if not os.environ.get('NEO_EVIDENCE_OUT') else None
OUT=Path(os.environ.get('NEO_EVIDENCE_OUT') or _OUTPUT_TMP.name)
OUT.mkdir(parents=True,exist_ok=True)
def record(name,value):
 (OUT/(name+'.json')).write_text(json.dumps(value,indent=2,ensure_ascii=False))
def drain(app,condition,seconds=3):
 end=time.monotonic()+seconds
 while time.monotonic()<end and not condition():
  QTest.qWait(5);app.processEvents()
 assert condition()

def restore_wake(w,monkeypatch):
 for name,value in ORIGINALS.items():monkeypatch.setattr(w,name,MethodType(value,w))
 w.audio_capture._qa_recording=False
 monkeypatch.setattr(type(w.audio_capture),'is_recording',property(lambda self:self._qa_recording))
 calls=[]
 def start(**kwargs):w.audio_capture._qa_recording=True;calls.append('wake')
 def stop(*a,**k):w.audio_capture._qa_recording=False;calls.append('stop')
 monkeypatch.setattr(w.audio_capture,'start_wake_monitor',start)
 monkeypatch.setattr(w.audio_capture,'stop',stop)
 monkeypatch.setattr(w.audio_capture,'restart_backend',lambda:None)
 monkeypatch.setattr(w.audio_capture,'start_command',lambda **kw:calls.append('command'))
 monkeypatch.setattr(w.audio_capture,'promote_to_command',lambda *a:calls.append('promote'))
 monkeypatch.setattr(w,'_restore_listening_audio',lambda:None)
 monkeypatch.setattr(w,'_configure_mail_monitor_timer',lambda:None)
 if getattr(w,'desktop_services',None):monkeypatch.setattr(w.desktop_services,'reconfigure',lambda:None)
 return calls

@pytest.mark.parametrize('disable',['voice','wake_word'])
def test_apply_invalidates_active_and_queued_wake(window,app,monkeypatch,disable):
 w=window;calls=restore_wake(w,monkeypatch)
 w.config.data['voice']['enabled']=True;w.config.data['wake_word']['enabled']=True
 d=open_settings(w,app)
 d.voice_enabled.setChecked(True);d.wake_enabled.setChecked(True)
 w._voice_mode='wake';w.audio_capture._qa_recording=True
 old=w._wake_generation
 w._pending_wake_recording=('stale',old);w._pending_wake_probe_recording=('stale',old,False)
 w._continuous_voice_session=True
 # A pending start from before Apply must not become live on re-enable.
 w._resume_wake_listening(delay_ms=80);queued=w._wake_generation
 (d.voice_enabled if disable=='voice' else d.wake_enabled).setChecked(False)
 service=SimpleNamespace(active=False,apply_preferences=Mock(),cancel=Mock(),close=Mock())
 monkeypatch.setattr(w,'_response_voice_service',service,raising=False)
 response_generation=w._agent_generation
 with patch.object(QTimer,'singleShot',lambda *_a,**_kw:None):d._save(close=False)
 assert w._wake_generation>queued and not w.audio_capture.is_recording and w._voice_mode==''
 assert w._pending_wake_recording is None and w._pending_wake_probe_recording is None
 assert calls==['stop'] and w._agent_generation==response_generation
 service.cancel.assert_not_called()
 if disable=='voice':
  assert not w._continuous_voice_session
  w._ensure_wake_monitor();w._start_wake_capture(w._wake_generation)
  w._start_command_capture();w._promote_wake_to_command()
  w._busy=True;w._ensure_wake_monitor();w._busy=False
 else:w._continuous_voice_session=False
 d.reject();QTest.qWait(330);app.processEvents()
 assert calls==['stop']
 # Re-enable after cancellation: the old timer remains stale, a fresh arm works.
 w.config.data['voice']['enabled']=True;w.config.data['wake_word']['enabled']=True
 w._start_wake_capture(queued);assert calls==['stop']
 w._resume_wake_listening(delay_ms=0);drain(app,lambda:calls[-1]=='wake')
 assert w.audio_capture.is_recording
 record('wake_'+disable,{'calls':calls.copy(),'stale_generation':queued,'final_generation':w._wake_generation,'response_generation_unchanged':w._agent_generation==response_generation,'tts_cancel_count':service.cancel.call_count})
 w._pause_wake_listening()

def test_disabled_input_text_response_and_tts_still_work(window,app,monkeypatch):
 from jarvis.agent_types import AgentTurn
 w=window;calls=restore_wake(w,monkeypatch)
 w.config.data['voice']['enabled']=False;w.config.data['wake_word']['enabled']=True
 w.config.data['ui']['character_streaming_enabled']=False
 w.config.data.setdefault('response_voice',{})['enabled']=True
 service=SimpleNamespace(active=False,cancel=Mock(),submit=Mock(return_value=True),close=Mock())
 monkeypatch.setattr(w,'_response_voice_service',service,raising=False)
 w._response_voice_timer=QTimer(w)
 monkeypatch.setattr(w,'_run_agent_stream',lambda text,generation,*a,**kw:(generation,AgentTurn('QA audible answer.')))
 w._send_text('qa typed request while microphone is disabled')
 drain(app,lambda:service.submit.call_count>0)
 assert service.submit.call_args.args[0]=='QA audible answer.'
 QTest.qWait(200);app.processEvents()
 assert not calls and not w._busy
 record('disabled_input_text_tts',{'submitted':list(service.submit.call_args.args),'microphone_calls':calls,'busy':w._busy})


def test_nvidia_real_status_adapter_all_locales(dialog,app,monkeypatch):
 from jarvis.integrations.instant_replay169 import InstantReplay,message
 d=dialog;d.show();original_init=InstantReplay.__init__;rows=[];press=Mock(side_effect=AssertionError('status must never press keys'))
 for language in ('ru','en','de','fr','es','it','pt'):
  d.config.data['app']['language']=language
  for windows,value,key in ((False,None,'unsupported'),(True,None,'unknown'),(True,False,'off'),(True,True,'on')):
   monkeypatch.setattr(InstantReplay,'__init__',lambda self,config,windows=windows,value=value:original_init(self,config,is_windows=windows,press=press,overlay=lambda:True,reader=lambda _names:value))
   d.replay_check184.click();drain(app,lambda:not d._replay_check_busy184)
   text=d.replay_state169.text();assert text==message(key,language)
   assert d.replay_check184.isEnabled()
   rows.append({'language':language,'key':key,'text':text})
 press.assert_not_called();record('nvidia_28_localized_states',rows)


def test_nvidia_async_singleflight_close_late_result(dialog,app,monkeypatch):
 from jarvis.integrations.instant_replay169 import InstantReplay
 d=dialog;d.show();started=threading.Event();release=threading.Event();probes=[];ticks=[]
 original_init=InstantReplay.__init__
 monkeypatch.setattr(InstantReplay,'__init__',lambda self,config:original_init(self,config,is_windows=True,overlay=lambda:True,reader=lambda _:False))
 def state(self):
  probes.append('read');started.set();release.wait(3);return False
 monkeypatch.setattr(InstantReplay,'state',state)
 timer=QTimer();timer.setInterval(1);timer.timeout.connect(lambda:ticks.append(1));timer.start()
 try:
  d.replay_check184.click();d._check_replay169()
  drain(app,started.is_set);QTest.qWait(30)
  assert len(probes)==1 and ticks and not d.replay_check184.isEnabled()
  d.reject();old=d.replay_state169.text();release.set()
  drain(app,lambda:not d._replay_check_busy184)
  assert d.replay_state169.text()==old
  record('nvidia_async',{'probe_count':len(probes),'event_loop_ticks':len(ticks),'late_result_ignored':True})
 finally:release.set();timer.stop()


def luminance(color):
 values=[v/255 for v in (color.red(),color.green(),color.blue())]
 values=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in values]
 return sum(a*b for a,b in zip(values,(.2126,.7152,.0722)))
def contrast(a,b):
 low,high=sorted((luminance(a),luminance(b)));return (high+.05)/(low+.05)

def test_confirmation_all_theme_roles_literal_and_default_no(window,app):
 from jarvis.ui.scenario_confirmation184 import ScenarioConfirmationDialog,confirm_scenario
 from jarvis.ui.theme_presets import THEME_PRESETS
 definition={'id':'literal','name':'<b>Literal & title</b>','phrase':'qa literal','enabled':True,'steps':[{'type':'wait','seconds':0}]}
 w=window;rows=[]
 for theme,values in THEME_PRESETS.items():
  w.config.data['ui'].update(values['window']);w._apply_settings()
  d=ScenarioConfirmationDialog(w,definition);d.show();QTest.qWait(10);app.processEvents()
  roles=[('preview',d.preview,QPalette.Text,QPalette.Base),('heading',d.heading,QPalette.WindowText,QPalette.Window),('approve',d.approve,QPalette.ButtonText,QPalette.Button),('cancel',d.cancel,QPalette.ButtonText,QPalette.Button)]
  row={'theme':theme}
  for name,widget,fg,bg in roles:
   palette=widget.palette();foreground=palette.color(fg);background=palette.color(bg)
   if name=='heading':background=d.palette().color(QPalette.Window)
   value=contrast(foreground,background);assert value>=4.5,(theme,name,value)
   row[name]={'foreground':foreground.name(),'background':background.name(),'contrast':value}
  assert '<b>Literal & title</b>' in d.preview.toPlainText() and d.preview.isReadOnly()
  assert d.heading.textFormat()==Qt.PlainText and d.cancel.isDefault() and not d.approve.autoDefault()
  QTest.keyClick(d,Qt.Key_Return);assert d.result()==QDialog.Rejected
  d.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
  rows.append(row)
 record('scenario_all_role_contrasts',rows)
 assert not w.findChildren(ScenarioConfirmationDialog)
 # The original modal wrapper must dispose a literal draft after No/Escape/close.
 for action in ('no','escape','close'):
  def dismiss(action=action):
   modal=app.activeModalWidget();assert isinstance(modal,ScenarioConfirmationDialog)
   if action=='no':modal.cancel.click()
   elif action=='escape':QTest.keyClick(modal,Qt.Key_Escape)
   else:modal.close()
  QTimer.singleShot(0,dismiss);assert not confirm_scenario(w,definition)
  QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);app.processEvents()
 assert not w.findChildren(ScenarioConfirmationDialog)
