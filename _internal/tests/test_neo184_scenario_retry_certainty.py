"""Retries require known absent effects; placement cannot claim window ownership."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication,QEvent
from jarvis.runtime import scenarios as runtime
from jarvis.runtime.cancellation import cancellation_manager
from jarvis.ui.scenario_editor import ScenarioEditor
from jarvis.ui.scenario_strings import LANGS, st
from tests.test_scenario_workflows184 import Config,item,app,no_actions

OPEN_PATH = runtime._open_path


@pytest.fixture(autouse=True)
def journal():
    runtime._RUN_RECORDS.clear();runtime._HISTORY.clear();cancellation_manager().reset()
    yield
    runtime._RUN_RECORDS.clear();runtime._HISTORY.clear();cancellation_manager().reset()


def forged(cfg,result):
    return {'action':'retry','id':'test-id','run_id':result.run_id,'step_index':1,
            'fingerprint':runtime.scenario_fingerprint(cfg.get('scenarios.items')[0])}


@pytest.mark.parametrize('replacement',['two_same_exe','closed_original_new_window'])
def test_window_changes_cannot_retarget_placement_retry(monkeypatch,replacement):
    original=SimpleNamespace(hwnd=101,pid=1,title='app',exe='app.exe')
    windows=[original]
    opened=[];moved=[]
    cfg=Config([item([{'type':'open','path':'app.exe','monitor':1,'launch_wait':0,'window_title':'app'}])])
    monkeypatch.setattr(runtime,'_open_path',lambda p:opened.append(p) or {'success':True})
    monkeypatch.setattr(runtime,'_move_opened_window',lambda *a:moved.append(tuple(w.hwnd for w in windows)) or {'moved':False,'error':'window not found'})
    result=runtime.run_scenario(cfg,'test-id')
    if replacement=='two_same_exe':windows.append(SimpleNamespace(hwnd=202,pid=2,title='app',exe='app.exe'))
    else:windows[:]=[SimpleNamespace(hwnd=303,pid=3,title='app',exe='app.exe')]
    assert result.outcomes[0]['opened'] and result.outcomes[0]['effect_state']=='opened'
    open_again=Mock(side_effect=AssertionError('retry must not launch'))
    move_again=Mock(side_effect=AssertionError('retry must not discover or move another window'))
    monkeypatch.setattr(runtime,'_open_path',open_again);monkeypatch.setattr(runtime,'_move_opened_window',move_again)
    with pytest.raises(ValueError,match='retry_placement'):runtime.scenario_retry_request(cfg,result.run_id,1)
    for attempt in (runtime.scenario_risk,runtime.retry_scenario):
        with pytest.raises(ValueError,match='retry_placement'):attempt(cfg,forged(cfg,result))
    open_again.assert_not_called();move_again.assert_not_called()
    assert opened==['app.exe'] and moved==[(101,)]


@pytest.mark.parametrize('outcome',[
    {'success':False},
    {'success':False,'effect_state':'unknown'},
    {'success':False,'effect_state':'partial'},
    {'success':False,'effect_state':'opened','opened':True},
    {'success':False,'effect_state':'none','opened':True},
    {'success':False,'effect_state':'none','placement':{'moved':False}},
])
def test_unknown_or_contradictory_effects_refuse_retry(monkeypatch,outcome):
    cfg=Config([item([{'type':'open','path':'app.exe'}])]);opens=[]
    monkeypatch.setattr(runtime,'_open_path',lambda p:opens.append(p) or deepcopy(outcome))
    result=runtime.run_scenario(cfg,'test-id')
    for attempt in (runtime.scenario_risk,runtime.retry_scenario):
        with pytest.raises(ValueError):attempt(cfg,{**forged(cfg,result),'effect_state':'none','mode':'open'})
    assert opens==['app.exe']


def test_launcher_exception_never_claims_no_effect(monkeypatch):
    # Inert API failure: the platform launcher reporting failure is insufficient evidence.
    with monkeypatch.context() as patch:
        patch.setattr(runtime.os,'name','nt')
        patch.setattr(runtime.os,'startfile',Mock(side_effect=OSError('uncertain handler failure')),raising=False)
        result=OPEN_PATH('app.exe')
    assert not result['success'] and result['effect_state']=='unknown'
    assert OPEN_PATH('')['effect_state']=='none'


def test_legacy_placement_mode_refuses_even_if_record_claims_no_effect(monkeypatch):
    cfg=Config([item([{'type':'open','path':'app.exe'}])])
    launch=Mock(return_value={'success':False,'effect_state':'none'})
    monkeypatch.setattr(runtime,'_open_path',launch)
    result=runtime.run_scenario(cfg,'test-id')
    runtime._RUN_RECORDS[result.run_id]['outcomes'][0]['retry_mode']='placement'
    with pytest.raises(ValueError,match='retry_placement'):runtime.retry_scenario(cfg,forged(cfg,result))
    assert launch.call_count==1


def test_known_no_effect_open_retry_is_single_use(monkeypatch):
    cfg=Config([item([{'type':'open','path':'app.exe'}])])
    launch=Mock(side_effect=[{'success':False,'effect_state':'none'},{'success':True}])
    monkeypatch.setattr(runtime,'_open_path',launch)
    result=runtime.run_scenario(cfg,'test-id');args=runtime.scenario_retry_request(cfg,result.run_id,1)
    assert runtime.retry_scenario(cfg,args).success
    with pytest.raises(ValueError):runtime.retry_scenario(cfg,args)
    assert launch.call_count==2


def test_failed_wait_carries_no_external_effects_and_retries(monkeypatch):
    cfg=Config([item([{'type':'wait','seconds':.01}])])
    sleep=runtime.time.sleep
    monkeypatch.setattr(runtime.time,'sleep',Mock(side_effect=TimeoutError('inert wait interruption')))
    result=runtime.run_scenario(cfg,'test-id')
    assert result.outcomes[0]['effect_state']=='none' and result.status=='failed'
    args=runtime.scenario_retry_request(cfg,result.run_id,1)
    monkeypatch.setattr(runtime.time,'sleep',sleep)
    assert runtime.retry_scenario(cfg,args).success
    runtime._open_path.assert_not_called();runtime.subprocess.Popen.assert_not_called()


@pytest.mark.parametrize('code',LANGS)
def test_placement_refusal_is_visible_and_localized(app,monkeypatch,code):
    cfg=Config([item([{'type':'open','path':'app.exe','monitor':1,'launch_wait':0}])],language=code)
    monkeypatch.setattr(runtime,'_open_path',lambda _: {'success':True})
    monkeypatch.setattr(runtime,'_move_opened_window',lambda *a: {'moved':False,'error':'window not found'})
    runtime.run_scenario(cfg,'test-id');editor=ScenarioEditor(cfg)
    assert editor.failed_steps.count()==1 and not editor.retry_button.isEnabled()
    assert editor.retry_help.text()==st('retry_placement',code)
    editor.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


def test_cancel_during_launch_wait_preserves_opened_effect_once(monkeypatch):
    cfg=Config([item([{'type':'open','path':'app.exe','monitor':1,'launch_wait':.7}, {'type':'wait','seconds':0}])])
    opens=[];moves=Mock(side_effect=AssertionError('cancelled before placement'))
    def opened(path):
        opens.append(path)
        cancellation_manager().cancel('stop after app launched')
        return {'success':True}
    monkeypatch.setattr(runtime,'_open_path',opened);monkeypatch.setattr(runtime,'_move_opened_window',moves)
    result=runtime.run_scenario(cfg,'test-id')
    assert result.status=='cancelled' and opens==['app.exe']
    step=[r for r in result.steps if r.get('step_index')==1]
    assert len(step)==1 and step[0]['opened'] and step[0]['cancelled']
    assert result.outcomes[0]['effect_state']=='opened' and result.outcomes[0]['placement_requested']
    assert result.outcomes[1]['not_run'] and result.summary=={'completed':0,'failed':1,'skipped':0,'not_run':1}
    assert len({r['step_index'] for r in result.outcomes})==len(result.outcomes)==2
    moves.assert_not_called()


def test_cancel_after_completed_step_does_not_append_second_step_failure(monkeypatch):
    cfg=Config([item([{'type':'open','path':'app.exe'}, {'type':'wait','seconds':0}])])
    monkeypatch.setattr(runtime,'_open_path',lambda _: {'success':True})
    def cancel(event):
        if event['state']=='step_finished' and event['step_index']==1:
            runtime.cancel_scenario(event['run_id'])
    result=runtime.run_scenario(cfg,'test-id',on_event=cancel)
    assert result.status=='cancelled'
    completed=[r for r in result.steps if r.get('step_index')==1]
    assert len(completed)==1 and completed[0]['success'] and completed[0]['opened']
    assert not [r for r in result.steps if r.get('step_index')==2]
    assert result.outcomes[0]['success'] and result.outcomes[1]['not_run']
    assert result.summary=={'completed':1,'failed':0,'skipped':0,'not_run':1}
    # The interruption is a run-level record (index 0), never a second step result.
    assert [r['step_index'] for r in result.steps]==[1,0]
