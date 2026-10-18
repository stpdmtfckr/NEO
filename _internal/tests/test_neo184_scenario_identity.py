"""NB49: replay exact phrases through public Agent, approval and runtime lookup."""
from copy import deepcopy
import pytest
from jarvis.runtime.router import HybridRouter
from jarvis.runtime.scenarios import find_scenario
from jarvis.runtime.scenario_authority184 import bind_scenario_arguments
from tests.test_neo184_scenario_authority import session
from tests.test_scenario_workflows184 import Config,item


@pytest.mark.parametrize('phrase,names,wanted',[
    ('Учёба',['Учёба'],'id0'),
    ('Work!',['Work!','Work'],'id0'),
    ('Work',['Work!','Work'],'id1'),
    ('учеба',['Учёба'],'id0'),
])
def test_nb49_router_to_lookup_preserves_identity(phrase,names,wanted):
    rows=[item(id='id'+str(i),name=name,phrase='',aliases=[]) for i,name in enumerate(names)]
    cfg=Config(rows)
    routed=HybridRouter().route(phrase,scenario_phrases=names)
    assert routed.tool=='scenario'
    assert find_scenario(cfg,routed.arguments['name'])['id']==wanted


@pytest.mark.parametrize('phrase,names,wanted,code',[
    ('Учёба',['Учёба'],'id0','ru'),
    ('Work!',['Work!','Work'],'id0','en'),
    ('Work',['Work!','Work'],'id1','en'),
    ('учеба',['Учёба'],'id0','ru'),
])
def test_nb49_public_agent_confirmation_runs_exact_scenario(session,phrase,names,wanted,code):
    s,opened=session;s.config.data['app']['language']=code
    s.config.data['scenarios']['items']=[{'id':'id'+str(i),'name':name,'phrase':name,'aliases':[],'enabled':True,'steps':[{'type':'open','path':f'C:/QA/scene{i}.cmd'}]} for i,name in enumerate(names)]
    turn=s.agent.handle(phrase)
    assert turn.pending and not opened
    assert turn.pending.arguments['id']==wanted
    assert find_scenario(s.config,turn.pending.arguments['id'])['id']==wanted
    answer=s.agent.confirm_pending(True)
    assert answer.success and opened==[f'C:/QA/scene{wanted[-1]}.cmd']


def test_nb49_public_agent_ambiguous_normalization_has_no_pending_or_effect(session):
    s,opened=session;s.config.data['scenarios']['items']=[{'id':'id'+str(i),'name':name,'phrase':name,'aliases':[],'enabled':True,'steps':[{'type':'open','path':f'C:/QA/scene{i}.cmd'}]} for i,name in enumerate(['Work!','Work'])]
    turn=s.agent.handle('Work?')
    assert not opened and not turn.pending
    assert not s.agent.confirm_pending(True).success and not opened


def test_nb49_binding_resolves_id_once_and_preserves_mismatched_id_guard():
    cfg=Config([item(id='study',name='Учёба',phrase='учиться')]);args={'action':'run','name':'учеба'}
    bind_scenario_arguments(cfg,args)
    assert args['id']=='study' and args['name']=='Учёба'
    cfg.get('scenarios.items')[0]['name']='Elsewhere'
    old=deepcopy(args);bind_scenario_arguments(cfg,args)
    assert args==old


@pytest.mark.parametrize('phrase,code',[
    ("Don't run Work!",'en'),
    ('What does "Work!" mean?','en'),
    ('Remind me to run Work! tomorrow','en'),
    ('Не запускай Учёба','ru'),
    ('Что означает сценарий «Учёба»?','ru'),
    ('Напомни запустить Учёба завтра','ru'),
])
def test_nb49_public_negative_quote_reminder_does_not_run_scenario(session,phrase,code):
    s,opened=session;s.config.data['app']['language']=code
    s.config.data['scenarios']['items']=[{'id':'id'+str(i),'name':name,'phrase':name,'aliases':[],'enabled':True,'steps':[{'type':'open','path':f'C:/QA/scene{i}.cmd'}]} for i,name in enumerate(['Work!','Work','Учёба'])]
    turn=s.agent.handle(phrase)
    assert not opened
    assert not turn.pending or turn.pending.tool_name!='scenario' or turn.pending.arguments.get('action')!='run'
