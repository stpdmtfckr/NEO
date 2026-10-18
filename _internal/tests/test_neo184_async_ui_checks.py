"""UI checks remain event-driven and do not block or reopen closed windows."""
import threading
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import QObject,QTimer,QCoreApplication,QEvent
from PySide6.QtTest import QTest
from jarvis.ui.settings_dialog import SettingsDialog
from jarvis.ui.main_window import MainWindow
from jarvis.integrations.instant_replay169 import InstantReplay
from jarvis.testing.isolation import external_io_blocked
from tests.test_neo158_settings_io import app,config


def test_nvidia_check_is_single_flight_and_event_loop_stays_live(app,config,monkeypatch):
 started=threading.Event();release=threading.Event();calls=[]
 def probe(self):
  calls.append(1);started.set();release.wait(2);return False
 original_init=InstantReplay.__init__
 monkeypatch.setattr(InstantReplay,'__init__',lambda self,config:original_init(self,config,is_windows=True))
 monkeypatch.setattr(InstantReplay,'state',probe)
 with external_io_blocked():
  d=SettingsDialog(config);d.show();ticks=[];timer=QTimer();timer.setInterval(1);timer.timeout.connect(lambda:ticks.append(1));timer.start()
  try:
   d._check_replay169();d._check_replay169();QTest.qWait(45)
   assert started.is_set() and len(calls)==1 and ticks
   assert not d.replay_check184.isEnabled()
   release.set()
   for _ in range(40):
    QTest.qWait(5)
    if not d._replay_check_busy184:break
   assert d.replay_check184.isEnabled() and d.replay_state169.text()
  finally:
   release.set();timer.stop();QTest.qWait(25);d.reject();d.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


def test_closed_settings_ignore_late_nvidia_result(app,config,monkeypatch):
 release=threading.Event()
 original_init=InstantReplay.__init__
 monkeypatch.setattr(InstantReplay,'__init__',lambda self,config:original_init(self,config,is_windows=True))
 monkeypatch.setattr(InstantReplay,'state',lambda self:release.wait(2) or True)
 with external_io_blocked():
  d=SettingsDialog(config);d._check_replay169();d.reject()
  old=d.replay_state169.text();release.set();QTest.qWait(60)
  assert d.replay_state169.text()==old
  d.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

class ProgressHarness(QObject):
 _scenario_event184=MainWindow._scenario_event184
 _flush_scenario_status184=MainWindow._flush_scenario_status184
 def __init__(self):
  super().__init__();self.config=SimpleNamespace(paths=SimpleNamespace(root=Path('/qa/current')));self._quitting=False;self.messages=[]
  self._pending_scenario_status184=None;self._scenario_status_timer184=QTimer(self);self._scenario_status_timer184.setSingleShot(True);self._scenario_status_timer184.setInterval(33);self._scenario_status_timer184.timeout.connect(self._flush_scenario_status184)
 def _language_code(self):return 'en'
 def _set_status(self,text):self.messages.append(text)

def test_scenario_progress_is_coalesced_and_scoped(app):
 h=ProgressHarness()
 for i in range(200):h._scenario_event184({'scope':'/qa/current','name':'QA Name','step_index':i,'total':200,'state':'step_started'})
 assert h.messages==[];QTest.qWait(55)
 assert len(h.messages)==1 and '199/200' in h.messages[0]
 h._scenario_event184({'scope':'/qa/other','name':'Other','state':'started'});QTest.qWait(40);assert len(h.messages)==1
 h._quitting=True;h._scenario_event184({'scope':'/qa/current','name':'Closed','state':'completed'});QTest.qWait(40);assert len(h.messages)==1
 h.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
