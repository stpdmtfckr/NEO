"""Independent regressions for mutable approval, transport and cancellation boundaries."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import queue,threading
import pytest
from PySide6.QtCore import Qt,QCoreApplication,QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog
from jarvis.runtime import scenarios
from jarvis.runtime.cancellation import cancellation_manager
from jarvis.runtime.scenario_authority184 import bind_scenario_arguments,scenario_preview
from jarvis.integrations.telegram_remote import TelegramRemoteController,_ToolJob
from jarvis.ui.scenario_confirmation184 import ScenarioConfirmationDialog
from jarvis.runtime.dialogue_tasks import Slot
from jarvis.neo_model.runtime import _pending
from tests.test_neo184_scenario_authority import session
from tests.test_scenario_workflows184 import Config,item
from tests.test_neo158_settings_io import app

@pytest.mark.parametrize('temporary',[[],[item([{'type':'wait','seconds':0}])]])
def test_risk_cannot_be_downgraded_by_temporary_definition(session,monkeypatch,temporary):
 s,opened=session;high=deepcopy(s.config.data['scenarios']['items']);orig=s.config.get;lookups=[]
 def get(key,default=None):
  if key=='scenarios.items':
   lookups.append(1);return deepcopy(high if len(lookups)==1 else temporary)
  return orig(key,default)
 monkeypatch.setattr(s.config,'get',get)
 route=SimpleNamespace(tool='scenario',intent='scenario',arguments={'action':'run','name':'qa scene'})
 turn=s.agent._execute_routed_intent(route,'qa scene')
 assert not opened and not turn.pending and 'routing.risk_check_failed' in turn.tool_details
 assert len(lookups)==2

def test_bound_id_cannot_borrow_another_allowed_name():
 cfg=Config([item([{'type':'wait','seconds':0}])]);args={'action':'run','id':'test-id','name':'different name'}
 bind_scenario_arguments(cfg,args)
 with pytest.raises(ValueError):scenarios.scenario_risk(cfg,args)

def test_missing_scenario_slot_is_left_unbound(session):
 s,opened=session
 _pending(s.agent,'en','scenario',{'action':'run'},Slot('name','name','name'))
 p=s.agent.conversations.get('desktop').pending_intent
 assert p.missing().name=='name' and 'fingerprint' not in p.arguments and not opened

def remote(config):
 r=TelegramRemoteController.__new__(TelegramRemoteController);r.config=config;r._lock=threading.RLock();r._seen_callback_ids={}
 r._track_message=lambda *_a,**_k:None;r._is_authorized=lambda *_:True;r._answer_callback=lambda *_:None
 r.sent=[];r.secure=[];r.panels=[]
 r._send_message=lambda *a,**k:r.sent.append((a,k));r._tg_message=lambda x:x;r._tg_runtime_text=lambda x:x
 r._roll_panel_to_bottom=lambda *_:None;r._panel=lambda *a,**k:r.panels.append((a,k))
 r._agent=SimpleNamespace(tools=SimpleNamespace(risk_for=lambda n,a:scenarios.scenario_risk(config,a)))
 r._request_secure_action=lambda **k:r.secure.append(k)
 r._execute_tool_now=lambda *_a,**_k:(_ for _ in ()).throw(AssertionError('High-risk menu must request approval'))
 return r

def callback(data):return {'id':'','data':data,'from':{'id':1},'message':{'message_id':7,'chat':{'id':1,'type':'private'}}}

def test_actual_telegram_menu_binds_and_confirms_same_target_after_reorder():
 high=item([{'type':'command','command':['qa-tool','<b>payload</b>']}]);other=item([{'type':'wait','seconds':0}],id='other',name='Other',phrase='other')
 cfg=Config([high,other]);r=remote(cfg);r._show_scenarios_menu('1',7)
 data=r.panels[-1][0][3][0][0][1];assert len(data.encode())<=64
 cfg.data['scenarios']['items'].reverse();r._handle_callback(callback(data))
 assert len(r.secure)==1
 args=r.secure[0]['arguments'];assert args['id']=='test-id' and args['name']=='Work' and args['fingerprint']==scenarios.scenario_fingerprint(high)
 assert r.secure[0]['user_id']=='1'

@pytest.mark.parametrize('mutation',['changed','disabled','legacy'])
def test_stale_or_disabled_telegram_buttons_never_run(mutation):
 cfg=Config([item([{'type':'command','command':['qa-tool']}])]);r=remote(cfg)
 data='a:sc:0:'+scenarios.scenario_fingerprint(cfg.data['scenarios']['items'][0])[:32]
 if mutation=='changed':cfg.data['scenarios']['items'][0]['steps'][0]['command'].append('new')
 elif mutation=='disabled':cfg.data['scenarios']['items'][0]['enabled']=False
 else:data='a:sc:0'
 r._handle_callback(callback(data));assert not r.secure and r.sent

def test_unbound_runtime_keeps_old_cancellation_epoch(monkeypatch):
 manager=cancellation_manager();manager.reset();started=threading.Event();opened=[];result=[];restored=[]
 cfg=Config([item([{'type':'wait','seconds':.3},{'type':'open','path':'QA.exe'}])])
 monkeypatch.setattr(scenarios,'_open_path',lambda p:opened.append(p) or {'success':True})
 def work():
  result.append(scenarios.run_scenario(cfg,'test-id',on_event=lambda e:started.set() if e['state']=='step_started' else None))
  restored.append(manager.is_explicitly_bound())
 t=threading.Thread(target=work);t.start();assert started.wait(1);manager.cancel('stop');manager.reset();t.join(2)
 assert not t.is_alive() and result[0].status=='cancelled' and not opened and restored==[False]

def test_actual_telegram_lane_captures_queued_epoch(monkeypatch):
 manager=cancellation_manager();cfg=Config([item([{'type':'wait','seconds':0}])]);r=remote(cfg)
 r._tool_lane_queues={'desktop':queue.Queue()};r._stop=threading.Event();observed=[];restored=[]
 def execute(job):
  observed.append(manager.is_cancelled());r._stop.set()
 r._execute_tool_now_blocking=execute
 job=_ToolJob('1','scenario',{'action':'run','id':'test-id'},'QA');assert r._queue_tool_job(job)
 manager.cancel('stop before dequeue');manager.reset()
 def work():r._tool_lane_loop('desktop');restored.append(manager.is_explicitly_bound())
 t=threading.Thread(target=work);t.start();t.join(2)
 assert not t.is_alive() and observed==[True] and restored==[False]

@pytest.mark.parametrize('code',['ru','en','de','fr','es','it','pt'])
def test_confirmation_is_plain_scrollable_and_defaults_cancel(app,code):
 raw=item([{'type':'command','command':['qa.exe','<b>not markup</b>', 'x'*20000+'\u202e.exe']}],name='<img src=x>',on_failure='stop')
 d=ScenarioConfirmationDialog(None,raw,code);d.show();QTest.qWait(10)
 value=d.preview.toPlainText();assert '<b>not markup</b>' in value and '\\u202e.exe' in value and 'effective_on_failure' in value and '"stop"' in value
 assert len(value)>20000 and d.preview.isReadOnly() and d.cancel.isDefault() and d.heading.textFormat()==Qt.PlainText
 QTest.keyClick(d,Qt.Key_Return);assert d.result()==QDialog.Rejected and not d.isVisible()
 d.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

@pytest.mark.parametrize('kind',['tool','agent'])
def test_secure_phrase_queues_scenario_without_poll_thread_execution(monkeypatch,kind):
 import time
 import jarvis.integrations.telegram_remote as tg
 cfg=Config([item([{'type':'command','command':['qa-tool']}])]);r=remote(cfg);queued=[]
 args={'action':'run','name':'Work','id':'test-id'};bind_scenario_arguments(cfg,args)
 r._secure_pending=tg._SecurePending('nonce',kind,'1','1',time.monotonic(),time.monotonic()+60,'QA scenario',7,'scenario',deepcopy(args),'sc:0')
 r._auth_failures={};r._input=None;r._stop=threading.Event();r._notify_desktop=lambda *_:None;r._power_action=lambda *_:False
 r._execute_tool_now=lambda *a,**kw:queued.append((a,kw))
 r._agent.confirm_pending=lambda *_a,**_k:(_ for _ in ()).throw(AssertionError('poll thread must not execute/wait for Agent'))
 monkeypatch.setattr(tg,'telegram_critical_phrase',lambda _:'fixture approval only')
 r._handle_critical_phrase('1','1','fixture approval only')
 assert len(queued)==1 and queued[0][0][2]==args
 assert queued[0][1]['consume_agent_pending']==(kind=='agent')
 assert r._secure_pending is None

def test_agent_consumes_only_exact_still_pending_call(session):
 s,opened=session;turn=s.agent.handle('qa scene');args=deepcopy(turn.pending.arguments)
 assert not s.agent.consume_pending_call184('scenario',{**args,'name':'other'},input_source='text')
 assert s.agent._pending_context is not None
 assert s.agent.consume_pending_call184('scenario',args,input_source='text')
 assert s.agent._pending_context is None and not opened
 assert not s.agent.consume_pending_call184('scenario',args,input_source='text')

def test_telegram_rows_are_immutable_snapshots():
 cfg=Config([item()]);r=remote(cfg);rows=r._scenario_items();token=scenarios.scenario_fingerprint(rows[0])
 cfg.data['scenarios']['items'][0]['steps'][0]['seconds']=4
 assert scenarios.scenario_fingerprint(rows[0])==token

@pytest.mark.parametrize('changed',[False,True])
def test_agent_window_uses_same_plain_confirmation_only_for_bound_definition(monkeypatch,changed):
 from jarvis.ui.main_window import MainWindow,QMessageBox
 import jarvis.ui.scenario_confirmation184 as confirmation
 cfg=Config([item([{'type':'command','command':['qa-tool']}])]);args={'action':'run','id':'test-id'};bind_scenario_arguments(cfg,args)
 requested=[];monkeypatch.setattr(confirmation,'confirm_scenario',lambda parent,raw,lang:requested.append(deepcopy(raw)) or False)
 monkeypatch.setattr(QMessageBox,'question',lambda *_:(_ for _ in ()).throw(AssertionError('AutoText question must not handle scenario')))
 h=SimpleNamespace(config=cfg,_flush_response_stream=lambda:None,_stream_flush_timer=SimpleNamespace(stop=lambda:None),_append_tool_details=lambda _:None,_localize_text=lambda x:x,_pending_response_bubble=None,chat=SimpleNamespace(update_bubble=lambda *_:None),_set_busy=lambda *_:None,_agent_generation=1,_language_code=lambda:'en')
 if changed:cfg.data['scenarios']['items'][0]['steps'][0]['command'].append('changed')
 turn=SimpleNamespace(text='Confirm',tool_details=[],pending=SimpleNamespace(tool_name='scenario',arguments=args,summary='raw'))
 MainWindow._handle_agent_turn(h,turn)
 assert bool(requested) is (not changed)

def test_shelf_worker_restores_pooled_thread_epoch(app,monkeypatch):
 from jarvis.ui.main_window import MainWindow
 import jarvis.ui.scenario_confirmation184 as confirmation
 from jarvis.security import Risk
 manager=cancellation_manager();cfg=Config([item()]);jobs=[];seen=[];restored=[]
 monkeypatch.setattr(confirmation,'confirm_scenario',lambda *_:True)
 def execute(_n,_a):
  manager.raise_if_cancelled();seen.append(manager.is_explicitly_bound());return SimpleNamespace(text='done')
 h=SimpleNamespace(config=cfg,agent=SimpleNamespace(tools=SimpleNamespace(risk_for=lambda *_:Risk.MEDIUM,execute=execute)),_language_code=lambda:'en',thread_pool=SimpleNamespace(start=lambda w:jobs.append(w)),_append_assistant=lambda _:None,_set_status=lambda _:None)
 MainWindow._run_shelf_scenario(h,'test-id');assert len(jobs)==1
 def work():jobs[0].function();restored.append(manager.is_explicitly_bound())
 t=threading.Thread(target=work);t.start();t.join(1)
 assert seen==[True] and restored==[False]
 jobs[0].signals.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

@pytest.mark.parametrize('kind',['tool','agent'])
def test_secure_scenario_enqueue_returns_without_executing_any_tool(monkeypatch,kind):
 # The prior secure-phrase test proves this branch uses the queue for both kinds;
 # this one specifically proves the poll thread never consumes Agent state.
 import time
 import jarvis.integrations.telegram_remote as tg
 cfg=Config([item()]);r=remote(cfg);calls=[]
 args={'action':'run','name':'Work','id':'test-id'};bind_scenario_arguments(cfg,args)
 r._secure_pending=tg._SecurePending('n',kind,'1','1',time.monotonic(),time.monotonic()+20,'QA',0,'scenario',args,'sc:0')
 r._auth_failures={};r._stop=threading.Event();r._notify_desktop=lambda *_:None;r._power_action=lambda *_:False
 r._agent.consume_pending_call184=lambda *_a,**_k:(_ for _ in ()).throw(AssertionError('Agent consumption belongs in worker'))
 r._execute_tool_now=lambda *a,**k:calls.append((a,k));monkeypatch.setattr(tg,'telegram_critical_phrase',lambda _:'test approval')
 r._handle_critical_phrase('1','1','test approval')
 assert len(calls)==1

def test_cancelled_scenario_discards_only_exact_agent_pending_without_waiting_for_execution():
 cfg=Config([item()]);r=remote(cfg);seen=[]
 r._agent.consume_pending_call184=lambda *a,**k:seen.append((a,k)) or True
 r._agent.confirm_pending=lambda *_a,**_k:(_ for _ in ()).throw(AssertionError('must not use blocking old confirmation'))
 pending=SimpleNamespace(tool_name='scenario',arguments={'action':'run','id':'test-id','fingerprint':'old'})
 r._discard_agent_confirmation184(pending,'1')
 assert seen[0][0]==('scenario',pending.arguments)

@pytest.mark.parametrize('denial',['ownership','stale_object'])
def test_queued_authorization_preserves_original_guard(session,denial):
 from jarvis.routing.ownership_firewall import OwnershipClaim
 s,opened=session;turn=s.agent.handle('qa scene');args=deepcopy(turn.pending.arguments)
 if denial=='ownership':s.agent._pending_context['ownership_claim']=OwnershipClaim(owner='conversation',allowed_tools=(),enforced=True)
 else:s.agent._pending_context['context_guard157']={'expires_at':0,'path':'unused','identity':None}
 assert not s.agent.consume_pending_call184('scenario',args,input_source='text',return_authorization=True)
 assert not opened and s.agent._pending_context is None

def test_valid_authorization_returns_exact_guard_and_claim(session):
 s,opened=session;turn=s.agent.handle('qa scene');args=deepcopy(turn.pending.arguments)
 original=deepcopy(s.agent._pending_context['ownership_claim'])
 auth=s.agent.consume_pending_call184('scenario',args,input_source='text',return_authorization=True)
 assert auth['ownership_claim']==original and auth['context_guard157'] is None and not opened

def test_queued_scenario_does_not_learn_a_newer_chat_turn(session):
 from jarvis.tools.registry import suppress_execution_learning184
 s,opened=session;row=s.config.data['scenarios']['items'][0];row['steps']=[{'type':'wait','seconds':.25}]
 args={'action':'run','id':row['id']};bind_scenario_arguments(s.config,args)
 started=threading.Event();results=[]
 def listener(e):
  if e['state']=='step_started':started.set()
 scenarios.subscribe_scenarios(listener)
 def work():
  with suppress_execution_learning184():results.append(s.agent.tools.execute('scenario',args))
 thread=threading.Thread(target=work)
 try:
  cancellation_manager().reset();thread.start();assert started.wait(1)
  s.agent.handle('hello')
  state=(deepcopy(getattr(s.agent,'_last_executed172',None)),deepcopy(getattr(s.agent,'_correction_wait172',None)),getattr(s.agent,'_turn_recorded172',None))
  thread.join(2)
  assert not thread.is_alive() and results[0].success
  assert state==(getattr(s.agent,'_last_executed172',None),getattr(s.agent,'_correction_wait172',None),getattr(s.agent,'_turn_recorded172',None))
 finally:scenarios.unsubscribe_scenarios(listener);thread.join(2)
