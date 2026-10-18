"""Supervised native Qt regressions: queued replies outlive short runnables.

The pre-fix worker can segfault the interpreter, so exercise it in a child. The
music case uses the real quick-command handlers, adapter and Qt player process;
only account lookup and the generated audio source are replaced (NEO 179: replies are text only).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


def run_probe(script, timeout=25):
    result = subprocess.run([sys.executable, "-X", "faulthandler", "-u", "-c", script],
        capture_output=True, text=True, timeout=timeout,
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", OPENBLAS_NUM_THREADS="1"))
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_fast_workers_deliver_all_queued_results_progress_and_errors_then_release():
    report = run_probe(r'''
import gc,json,threading,time,weakref
import shiboken6
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QThreadPool,Slot
from PySide6.QtWidgets import QApplication
from jarvis.workers import FunctionWorker,WorkerSignals
app=QApplication([]);pool=QThreadPool();pool.setMaxThreadCount(4)
main=threading.get_ident();events={};done=[];destroyed=[];refs=[];threads=[];released=[];payloads=[]
class Payload:
 def __del__(self):released.append(threading.get_ident())
def record(index,kind,value=None):
 threads.append(threading.get_ident());events.setdefault(index,[]).append((kind,value))
def launch(index):
 payload=Payload();payloads.append(weakref.ref(payload))
 def work(arg,*,extra,on_event):
  assert arg is payload and extra is payload
  if index>=120:time.sleep(.002)
  on_event(1);on_event(2)
  if index%11==0:raise ValueError('deliberate fixture error')
  return index
 worker=FunctionWorker(work,payload,extra=payload,progress_keyword='on_event');refs.append(weakref.ref(worker))
 worker.signals.progress.connect(lambda value,i=index:record(i,'progress',value))
 worker.signals.result.connect(lambda value,i=index:record(i,'result',value))
 worker.signals.error.connect(lambda value,i=index:record(i,'error','ValueError' in value))
 worker.signals.finished.connect(lambda i=index:done.append(i))
 worker.signals.destroyed.connect(lambda *_,i=index:destroyed.append(i))
 pool.start(worker)
live_pumps=0
for index in range(240):
 launch(index)
 if index>=120 and index%3==0:
  live_pumps+=int(pool.activeThreadCount()>0)
  app.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
assert live_pumps>0,'Did not exercise cleanup while the pool was running'
assert pool.waitForDone(5000)
gc.collect() # No caller-owned runnable references survive until delivery.
deadline=time.monotonic()+5
while len(destroyed)<240 and time.monotonic()<deadline:
 app.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
assert sorted(done)==list(range(240)),('lost finished',len(done))
for index in range(240):
 expected=[('progress',1),('progress',2),('error',True) if index%11==0 else ('result',index)]
 assert events.get(index)==expected,(index,events.get(index))
assert set(threads)=={main},'A callback mutated the UI from a worker thread'
assert len(destroyed)==240 and not app.findChildren(WorkerSignals)
gc.collect();assert not any(ref() is not None for ref in payloads),'Completed audio/job inputs retained'
assert len(released)==240 and set(released)=={main},'Job inputs released outside their GUI owner'
# A bound QObject receiver is automatically disconnected on window destruction.
gate=threading.Event();late=[]
class Receiver(QObject):
 @Slot(object)
 def receive(self,value):late.append(value)
receiver=Receiver();worker=FunctionWorker(lambda:(gate.wait(2),'late')[1])
worker.signals.result.connect(receiver.receive);pool.start(worker)
receiver.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
del worker;gate.set();assert pool.waitForDone(3000)
for _ in range(4):
 app.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
assert not late and not app.findChildren(WorkerSignals)
pool.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
del pool
# PySide may retain invalid Python wrappers in its pool bookkeeping. Assert the
# actual native resource lifetime instead of treating that binding detail as a
# live QRunnable; all signal QObjects were already counted as destroyed above.
gc.collect();assert all(ref() is None or not shiboken6.isValid(ref()) for ref in refs)
print(json.dumps({'delivered':240,'ordered_progress':True,'gui_thread':True,'released':240,'late_disconnected':True}))
''')
    assert report == {"delivered": 240, "ordered_progress": True, "gui_thread": True,
                      "released": 240, "late_disconnected": True}


def test_late_worker_emissions_after_application_destruction_are_discarded():
    report = run_probe(r'''
import json,threading
import shiboken6
from PySide6.QtWidgets import QApplication
from jarvis.workers import FunctionWorker
# Synchronous non-Qt callers keep their existing direct signal behavior.
direct=[];worker=FunctionWorker(lambda:42)
worker.signals.result.connect(direct.append);worker.run();assert direct==[42]
app=QApplication([]);gate=threading.Event();entered=threading.Event();done=threading.Event();late=[]
def work(on_event):
 entered.set();gate.wait(3);on_event('late progress');return 'late result'
worker=FunctionWorker(work,progress_keyword='on_event')
worker.signals.progress.connect(late.append);worker.signals.result.connect(late.append)
def run():
 try:worker.run()
 finally:done.set()
thread=threading.Thread(target=run);thread.start();assert entered.wait(1)
shiboken6.delete(app);gate.set();thread.join(3)
assert done.is_set() and not late
print(json.dumps({'sync_no_app':True,'late_ignored':True,'worker_returned':True}))
''')
    assert report == {"sync_no_app": True, "late_ignored": True, "worker_returned": True}


def test_actual_quick_stop_keeps_qt_chat_alive_and_resumes_same_generated_track():
    report = run_probe(r'''
import gc,json,os,tempfile,threading,time,wave
from pathlib import Path
from types import MethodType,SimpleNamespace
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QThreadPool
from PySide6.QtWidgets import QApplication
from jarvis.integrations.background.local_player import BackgroundLocalPlayer
from jarvis.integrations import yandex_music as ym
from jarvis.quick_media_commands import parse_quick_media_command
from jarvis.ui.chat_view import ChatView
from jarvis.ui.main_window import MainWindow
from jarvis.ui.robot_widget import RobotWidget
from jarvis.workers import WorkerSignals
app=QApplication([]);root=tempfile.TemporaryDirectory();path=Path(root.name)
os.environ['NEOJARVIS_CACHE_DIR']=str(path/'cache')
wav=path/'generated.wav'
with wave.open(str(wav),'wb') as out:
 out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000)
 out.writeframes(b'\0\0'*16000*20)
class Config:
 paths=SimpleNamespace(root=path)
 def get(self,key,default=None):
  return {'integrations.yandex_music.enabled':True,'ui.language':'ru'}.get(key,default)
config=Config();player=BackgroundLocalPlayer();ym.local_player=lambda:player
ym.YandexMusicAdapter.clear_transient_cache_on_startup_once=lambda self:{}
adapter=ym.YandexMusicAdapter(config)
adapter._current_track_obj={'id':'generated','title':'Generated track','duration_ms':20000}
adapter._current_track_meta={'id':'generated','title':'Generated track'}
adapter._station_queue=[{'id':'next','title':'Next fixture track'}]
calls=[];responses=[];watchdogs=[];main=threading.get_ident()
def execute(tool,args):
 assert tool=='yandex_music'
 calls.append((args['action'],threading.get_ident()))
 data=adapter.transport(args['action'])
 return SimpleNamespace(success=True,data=data,text='')
w=QObject();w.config=config;w.agent=SimpleNamespace(tools=SimpleNamespace(execute=execute,yandex_adapter=adapter),
 personalize_external_response=lambda value:value)
w._language_code=lambda:'ru';w._cached_quick_media_owner=lambda:'yandex_music';w._quick_media_server=lambda:''
w._last_quick_media_key='';w._last_quick_media_at=0;w._busy=False;w._quitting=False
w._continuous_voice_session=False;w._listening_bubble=None;w._music_overlay_last_event={}
w._last_now_playing_by_source={};w._pause_wake_listening=lambda:None
w._suppress_media_notice=lambda *args:None;w._clear_listening_bubble=lambda:None
w._set_status=lambda *args:None
w._mirror_assistant_to_telegram=lambda text:responses.append((text,threading.get_ident()))  # the result shown in the chat row
w._refresh_robot_stage_info=lambda:None
w._schedule_wake_watchdog=lambda delay:watchdogs.append(delay)
w.chat=ChatView();w.chat.resize(400,400);w.chat.show()
w.robot=RobotWidget();w.robot.show();w._quick_media_pool=QThreadPool(w);w._quick_media_pool.setMaxThreadCount(1)
w._append_assistant=lambda text,**kwargs:w.chat.add_assistant(text)
for name in ('_dispatch_quick_media_command','_quick_media_action_enabled','_execute_quick_media_command',
             '_quick_media_finished','_quick_media_failed'):
 setattr(w,name,MethodType(getattr(MainWindow,name),w))
def pump_until(condition,timeout=5):
 deadline=time.monotonic()+timeout
 while not condition() and time.monotonic()<deadline:
  app.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);time.sleep(.002)
 assert condition(),'Queued quick-media result disappeared'
def command(text):
 before=len(responses);quick=parse_quick_media_command(text,'ru');assert quick is not None
 w._dispatch_quick_media_command(quick)
 assert w._quick_media_pool.waitForDone(6000)
 gc.collect();pump_until(lambda:len(responses)==before+1)
 assert len(w.chat.items)==before+1 and w.robot.state()=='idle'
 assert w.chat.items[-1].text==responses[-1][0]
try:
 player.set_volume(0);started=player.play(str(wav),title='Generated track');assert started['verified']
 command('стоп');paused=player.status_if_running();assert paused['state']=='paused'
 saved=paused['position_ms'];pump_until(lambda:True)
 time.sleep(.12);assert abs(player.status_if_running()['position_ms']-saved)<50
 # Same rolling-window duplicate is suppressed without changing transport.
 w._dispatch_quick_media_command(parse_quick_media_command('стоп','ru'))
 assert len(calls)==1 and len(w.chat.items)==1
 command('продолжить');resumed=player.status_if_running()
 assert resumed['state']=='playing' and resumed['position_ms']>=saved+70
 command('пауза');assert player.status_if_running()['state']=='paused'
 assert adapter._manual_pause_active and adapter._station_queue[0]['id']=='next'
 assert adapter._current_track_obj['id']=='generated'
 assert [x[0] for x in calls]==['pause','play','pause']
 assert all(ident!=main for _,ident in calls) and all(ident==main for _,ident in responses)
 # Late completion during shutdown does not add chat rows or re-arm capture.
 w._quitting=True;before=len(w.chat.items);wake_count=len(watchdogs)
 w._quick_media_finished({'ok':True,'text':'late','action':'pause'})
 w._quick_media_failed('late error')
 assert len(w.chat.items)==before and len(watchdogs)==wake_count
 print(json.dumps({'real_player':True,'pause_resume':True,'same_track_queue':True,'single_rows':3,'ui_alive':True}))
finally:
 w._quitting=True;player.close();adapter._feedback_lane.close()
 w._quick_media_pool.waitForDone(3000);w.robot.hide();w.chat.hide()
 for _ in range(5):
  app.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
 root.cleanup()
''', timeout=35)
    assert report == {"real_player": True, "pause_resume": True, "same_track_queue": True,
                      "single_rows": 3, "ui_alive": True}
