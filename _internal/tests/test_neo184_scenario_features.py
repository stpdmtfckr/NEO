"""Scenario templates and failed-step retries, with every effect replaced by an inert adapter."""
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QDialog
from jarvis.runtime import scenarios as runtime
from jarvis.runtime.cancellation import cancellation_manager
from jarvis.runtime.scenario_templates184 import make_template
from jarvis.runtime.scenario_schema import validate_scenario
from jarvis.ui.scenario_editor import ScenarioEditor
from jarvis.ui.scenario_strings import LANGS, _ROWS, st
from jarvis.ui.scenario_confirmation184 import ScenarioConfirmationDialog, consequence_preview
from tests.test_scenario_workflows184 import Config, item, app, no_actions


@pytest.fixture(autouse=True)
def clean_journal():
    with runtime._LOCK:
        runtime._RUN_RECORDS.clear()
        runtime._HISTORY.clear()
    yield
    with runtime._LOCK:
        runtime._RUN_RECORDS.clear()
        runtime._HISTORY.clear()


def failed_open(monkeypatch, *, other_steps=True, policy='continue'):
    calls = []
    def fake_open(path):
        calls.append(path)
        return {'success':path != 'broken.exe', 'effect_state':'none' if path == 'broken.exe' else 'opened', 'error':'missing' if path == 'broken.exe' else ''}
    monkeypatch.setattr(runtime, '_open_path', fake_open)
    steps = ([{'type':'open','path':'done.exe'}] if other_steps else []) + [{'type':'open','path':'broken.exe'}]
    if other_steps:
        steps += [{'type':'wait','seconds':0}, {'type':'open','path':'skip.exe','enabled':False}]
    cfg=Config([item(steps,on_failure=policy)])
    result=runtime.run_scenario(cfg,'test-id')
    return cfg,result,calls


@pytest.mark.parametrize('code',LANGS)
def test_templates_are_disabled_editable_and_pure(app, code):
    cfg=Config(language=code);editor=ScenarioEditor(cfg)
    before=deepcopy(cfg.data)
    ids,names=set(),set()
    for kind in ('work','game','study','work'):
        editor.template_choice.setCurrentIndex(editor.template_choice.findData(kind))
        editor.add_template()
        draft=editor.items()[-1]
        assert draft['enabled'] is False and draft['phrase']=='' and draft['aliases']==[]
        assert draft['id'] not in ids and draft['name'] not in names
        ids.add(draft['id']);names.add(draft['name'])
        assert [s['type'] for s in draft['steps']]==['open','wait','open']
        assert all(s.get('label') for s in draft['steps'])
        assert [s['path'] for s in draft['steps'] if s['type']=='open']==['','']
        assert not editor.run_button.isEnabled()
        assert editor.template_help.isHidden() is False
        assert not validate_scenario(draft).valid
    assert cfg.data==before
    runtime._open_path.assert_not_called();runtime.subprocess.Popen.assert_not_called()
    editor.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


def test_template_name_avoids_existing_phrases_and_aliases(app):
    cfg=Config([item(name='Other',phrase='Work',aliases=['Work 2'])]);editor=ScenarioEditor(cfg)
    editor.add_template()
    assert editor.items()[-1]['name']=='Work 3'
    editor.deleteLater()


@pytest.mark.parametrize('code',LANGS)
def test_consequence_preview_and_retry_are_plain_no_effect(app,code):
    cfg=Config([item([{'type':'open','path':'x.exe'},{'type':'command','command':['qa','<b>x</b>\u202e.exe']},{'type':'wait','seconds':2}])])
    raw=cfg.get('scenarios.items')[0]
    d=ScenarioConfirmationDialog(None,raw,code,review_only=True)
    assert not d.approve.isVisible() and d.cancel.isDefault()
    assert st('consequence_open',code) in d.preview.toPlainText()
    assert st('consequence_command',code) in d.preview.toPlainText()
    assert '\\u202e' in d.preview.toPlainText()
    d.reject();d.deleteLater()
    text=consequence_preview(raw,code,{'step_index':1,'mode':'open'})
    assert st('retry_step_only',code,step=1) in text
    blocked=ScenarioConfirmationDialog(None,raw,code,retry={'step_index':1,'mode':'placement'})
    assert not blocked.approve.isEnabled() and st('retry_placement',code) in blocked.preview.toPlainText()
    blocked.deleteLater()
    runtime._open_path.assert_not_called();runtime.subprocess.Popen.assert_not_called()


def test_retry_only_failed_step_preserves_results_and_consumes_request(monkeypatch):
    cfg,result,calls=failed_open(monkeypatch)
    args=runtime.scenario_retry_request(cfg,result.run_id,2)
    calls.clear();monkeypatch.setattr(runtime,'_open_path',lambda p:calls.append(p) or {'success':True})
    retried=runtime.retry_scenario(cfg,args)
    assert calls==['broken.exe'] and [s['step_index'] for s in retried.steps]==[2]
    assert retried.success and retried.retry_of==result.run_id
    assert retried.summary=={'completed':3,'failed':0,'skipped':1,'not_run':0}
    assert retried.outcomes[0]['success'] and retried.outcomes[3]['skipped']
    with pytest.raises(ValueError,match='retry_unavailable'):runtime.retry_scenario(cfg,args)
    assert calls==['broken.exe']


def test_stop_policy_leaves_later_steps_unrun_after_retry(monkeypatch):
    cfg,result,calls=failed_open(monkeypatch,policy='stop')
    assert result.summary=={'completed':1,'failed':1,'skipped':0,'not_run':2}
    args=runtime.scenario_retry_request(cfg,result.run_id,2)
    calls.clear();monkeypatch.setattr(runtime,'_open_path',lambda p:calls.append(p) or {'success':True})
    retried=runtime.retry_scenario(cfg,args)
    assert retried.status=='partial' and calls==['broken.exe'] and retried.summary['not_run']==2
    with pytest.raises(ValueError):runtime.scenario_retry_request(cfg,retried.run_id,3)


def test_placement_failure_refuses_retry_without_launch_or_move(monkeypatch):
    cfg=Config([item([{'type':'open','path':'app.exe','monitor':1,'launch_wait':0,'window_title':'Unique app'}])])
    calls=[];moves=[]
    monkeypatch.setattr(runtime,'_open_path',lambda p:calls.append(p) or {'success':True})
    monkeypatch.setattr(runtime,'_move_opened_window',lambda *a:moves.append(a) or {'moved':False,'error':'window not found'})
    result=runtime.run_scenario(cfg,'test-id')
    assert result.steps[0]['opened'] and result.steps[0]['placement_requested']
    assert 'retry_mode' not in result.steps[0]
    with pytest.raises(ValueError,match='retry_placement'):
        runtime.scenario_retry_request(cfg,result.run_id,1)
    args={'action':'retry','id':'test-id','run_id':result.run_id,'step_index':1,'fingerprint':runtime.scenario_fingerprint(cfg.get('scenarios.items')[0])}
    for check in (runtime.scenario_risk,runtime.retry_scenario):
        with pytest.raises(ValueError,match='retry_placement'):check(cfg,args)
    assert calls==['app.exe'] and len(moves)==1


@pytest.mark.parametrize('change',['edited','deleted','disabled','rebound','reordered','other_owner','history_lost','new_run','bad_step','bad_fingerprint'])
def test_stale_retry_cannot_execute(monkeypatch,change):
    cfg,result,calls=failed_open(monkeypatch)
    args=runtime.scenario_retry_request(cfg,result.run_id,2)
    rows=cfg.data['scenarios']['items']
    if change=='edited':rows[0]['steps'][1]['path']='new.exe'
    elif change=='deleted':rows.clear()
    elif change=='disabled':rows[0]['enabled']=False
    elif change=='rebound':rows[0]=item([{'type':'open','path':'different.exe'}])
    elif change=='reordered':rows[0]['steps'].reverse()
    elif change=='other_owner':
        cfg=Config(rows)
    elif change=='history_lost':runtime._RUN_RECORDS.clear()
    elif change=='new_run':runtime.run_scenario(cfg,'test-id')
    elif change=='bad_step':args['step_index']=1
    elif change=='bad_fingerprint':args['fingerprint']='wrong'
    calls.clear()
    with pytest.raises(ValueError):runtime.retry_scenario(cfg,args)
    with pytest.raises(ValueError):runtime.scenario_risk(cfg,args)
    assert calls==[]


def test_command_partial_effects_never_retry(monkeypatch):
    cfg=Config([item([{'type':'command','command':['qa']}])]);commands=[]
    monkeypatch.setattr(runtime,'_command',lambda *a:commands.append(a) or {'success':False,'returncode':1})
    result=runtime.run_scenario(cfg,'test-id')
    with pytest.raises(ValueError,match='retry_command'):runtime.scenario_retry_request(cfg,result.run_id,1)
    forged={'action':'retry','id':'test-id','run_id':result.run_id,'step_index':1,'fingerprint':runtime.scenario_fingerprint(cfg.get('scenarios.items')[0])}
    with pytest.raises(ValueError,match='retry_command'):runtime.retry_scenario(cfg,forged)
    assert len(commands)==1


def test_unknown_open_exception_cannot_retry(monkeypatch):
    cfg=Config([item([{'type':'open','path':'qa.exe'}])])
    def fail(_):raise RuntimeError('uncertain external effect')
    monkeypatch.setattr(runtime,'_open_path',fail)
    result=runtime.run_scenario(cfg,'test-id')
    with pytest.raises(ValueError,match='retry_unknown'):runtime.scenario_retry_request(cfg,result.run_id,1)


def test_cancel_after_launch_preserves_partial_result_without_retry(monkeypatch):
    cfg=Config([item([{'type':'open','path':'app.exe','monitor':1,'launch_wait':0}])])
    monkeypatch.setattr(runtime,'_open_path',lambda _: {'success':True})
    def cancel(*args):
        cancellation_manager().cancel('stop')
        args[-1]()
    monkeypatch.setattr(runtime,'_move_opened_window',cancel)
    result=runtime.run_scenario(cfg,'test-id')
    assert result.status=='cancelled' and result.outcomes[0]['opened'] and not result.outcomes[0]['success']
    with pytest.raises(ValueError,match='retry_cancelled'):runtime.scenario_retry_request(cfg,result.run_id,1)


def test_retry_is_atomic_against_concurrent_requests_and_other_runs(monkeypatch):
    cfg,result,calls=failed_open(monkeypatch,other_steps=False)
    args=runtime.scenario_retry_request(cfg,result.run_id,1)
    started,release=threading.Event(),threading.Event();results=[]
    def opening(path):
        calls.append(path);started.set();assert release.wait(2);return {'success':True}
    monkeypatch.setattr(runtime,'_open_path',opening)
    thread=threading.Thread(target=lambda:results.append(runtime.retry_scenario(cfg,args)));thread.start()
    try:
        assert started.wait(1)
        with pytest.raises(ValueError):runtime.retry_scenario(cfg,args)
        assert runtime.run_scenario(cfg,'test-id').status=='busy'
    finally:release.set();thread.join(2)
    assert not thread.is_alive() and results[0].success and calls==['broken.exe','broken.exe']


def test_failed_retry_makes_new_manual_request_and_never_loops(monkeypatch):
    cfg,result,calls=failed_open(monkeypatch,other_steps=False)
    args=runtime.scenario_retry_request(cfg,result.run_id,1)
    result2=runtime.retry_scenario(cfg,args)
    assert not result2.success and len(calls)==2
    with pytest.raises(ValueError):runtime.retry_scenario(cfg,args)
    args2=runtime.scenario_retry_request(cfg,result2.run_id,1)
    assert args2['run_id']!=args['run_id']
    assert len(calls)==2


def test_editor_displays_partial_result_and_disables_dirty_retry(app,monkeypatch):
    cfg,result,calls=failed_open(monkeypatch,policy='stop');editor=ScenarioEditor(cfg)
    assert st('result_summary','en',**result.summary)==editor.result_summary.text()
    assert editor.failed_steps.count()==1 and editor.retry_button.isEnabled()
    requests=[];editor.retry_requested.connect(requests.append);editor.request_retry()
    assert requests[0]['run_id']==result.run_id
    editor.name.setText('Edited')
    assert not editor.retry_button.isEnabled()
    editor.deleteLater()


def test_retry_uses_normal_risk_of_bound_scenario(monkeypatch):
    from jarvis.security import Risk
    cfg,result,calls=failed_open(monkeypatch,other_steps=False)
    assert runtime.scenario_risk(cfg,runtime.scenario_retry_request(cfg,result.run_id,1))==Risk.MEDIUM
    cfg.get('scenarios.items')[0]['steps'][0]['path']='broken.ps1'
    result=runtime.run_scenario(cfg,'test-id')
    # Force a safe adapter's explicit failed result for the risky open target.
    monkeypatch.setattr(runtime,'_open_path',lambda _: {'success':False,'effect_state':'none'})
    result=runtime.run_scenario(cfg,'test-id')
    assert runtime.scenario_risk(cfg,runtime.scenario_retry_request(cfg,result.run_id,1))==Risk.HIGH


def test_retry_mainwindow_confirmation_cancel_and_double_click(app,monkeypatch):
    from jarvis.ui.main_window import MainWindow
    from jarvis.security import Risk
    import jarvis.ui.scenario_confirmation184 as confirmation
    cfg,result,calls=failed_open(monkeypatch,other_steps=False)
    args=runtime.scenario_retry_request(cfg,result.run_id,1)
    jobs=[];reviews=[];executed=[]
    h=SimpleNamespace(config=cfg,agent=SimpleNamespace(tools=SimpleNamespace(risk_for=lambda n,a: runtime.scenario_risk(cfg,a),execute=lambda n,a:executed.append(a) or SimpleNamespace(text='done'))),_language_code=lambda:'en',thread_pool=SimpleNamespace(start=jobs.append),_append_assistant=lambda _:None,_set_status=lambda _:None)
    monkeypatch.setattr(confirmation,'confirm_scenario',lambda *a,**kw:reviews.append(kw) or False)
    MainWindow._retry_shelf_scenario(h,args)
    assert not jobs and not h._scenario_retry_pending184 and reviews[0]['retry']['step_index']==1
    monkeypatch.setattr(confirmation,'confirm_scenario',lambda *a,**kw:True)
    MainWindow._retry_shelf_scenario(h,args);MainWindow._retry_shelf_scenario(h,args)
    assert len(jobs)==1 and not executed
    jobs[0].run();assert executed==[args] and not h._scenario_retry_pending184
    jobs[0].signals.deleteLater()


def test_equivalent_reload_and_unrelated_settings_save_keep_retry(monkeypatch):
    cfg,result,calls=failed_open(monkeypatch,other_steps=False)
    args=runtime.scenario_retry_request(cfg,result.run_id,1)
    cfg.data=deepcopy(cfg.data)
    cfg.data['app']['theme']='dark'
    assert runtime.scenario_retry_request(cfg,result.run_id,1)==args
    monkeypatch.setattr(runtime,'_open_path',lambda p:calls.append(p) or {'success':True})
    assert runtime.retry_scenario(cfg,args).success
    assert calls==['broken.exe','broken.exe']


def test_expired_history_fails_closed(monkeypatch):
    cfg,result,calls=failed_open(monkeypatch,other_steps=False)
    args=runtime.scenario_retry_request(cfg,result.run_id,1)
    monkeypatch.setattr(runtime,'MAX_RUN_RECORDS',2)
    for n in range(3):
        runtime.run_scenario(Config([item()]),'test-id')
    assert len(runtime._RUN_RECORDS)==2
    with pytest.raises(ValueError,match='retry_unavailable'):runtime.retry_scenario(cfg,args)
    assert calls==['broken.exe']


def test_different_active_scenario_blocks_retry_without_consuming_it(monkeypatch):
    cfg,result,calls=failed_open(monkeypatch,other_steps=False)
    args=runtime.scenario_retry_request(cfg,result.run_id,1)
    cfg.data['scenarios']['items'].append(item([{'type':'wait','seconds':10}],id='other',name='Other',phrase='other',aliases=[]))
    started=threading.Event();running=[]
    thread=threading.Thread(target=lambda:running.append(runtime.run_scenario(cfg,'other',on_event=lambda e:started.set() if e['state']=='step_started' else None)))
    thread.start()
    try:
        assert started.wait(1)
        assert runtime.retry_scenario(cfg,args).status=='busy'
        assert runtime.scenario_retry_request(cfg,result.run_id,1)==args
        runtime.cancel_scenario(runtime.active_scenarios(cfg)[0]['run_id'])
    finally:
        for event in runtime.active_scenarios(cfg):runtime.cancel_scenario(event['run_id'])
        thread.join(2)
    assert not thread.is_alive() and running[0].status=='cancelled'
    monkeypatch.setattr(runtime,'_open_path',lambda _: {'success':True})
    assert runtime.retry_scenario(cfg,args).success


from tests.test_neo184_scenario_authority import session

def test_registered_gateway_retries_with_bound_arguments_and_rejects_replay(session,monkeypatch):
    s,_=session;cfg=s.config;row=cfg.get('scenarios.items')[0];calls=[]
    monkeypatch.setattr(runtime,'_open_path',lambda p:calls.append(p) or {'success':False,'error':'missing','effect_state':'none'})
    args={'action':'run','id':row['id'],'fingerprint':runtime.scenario_fingerprint(row)}
    failed=s.agent.tools.execute('scenario',args)
    retry=runtime.scenario_retry_request(cfg,failed.data['run_id'],1)
    monkeypatch.setattr(runtime,'_open_path',lambda p:calls.append(p) or {'success':True})
    done=s.agent.tools.execute('scenario',retry)
    assert done.success and done.data['summary']['completed']==1
    assert done.data['retry_of']==failed.data['run_id'] and len(calls)==2
    stale=s.agent.tools.execute('scenario',retry)
    assert not stale.success and len(calls)==2
