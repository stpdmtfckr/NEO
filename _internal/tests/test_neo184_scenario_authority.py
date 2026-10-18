"""Approval is bound to a scenario snapshot, never a mutable ID alone."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.runtime import scenarios
from jarvis.runtime.scenario_authority184 import bind_scenario_arguments
from jarvis.agent import Agent

@pytest.fixture
def session(tmp_path,monkeypatch):
 with external_io_blocked():
  s=AtlasSession(tmp_path/'scenario-authority')
  s.agent.tools.set_execution_guard(lambda n,a,r:None if n=='scenario' else 'inert test denial')
  s.config.data['scenarios']={'items':[{'id':'qa-safe-id','name':'QA scene','phrase':'qa scene','aliases':[], 'enabled':True,'steps':[{'type':'open','path':'C:/QA/first.cmd'}]}]}
  opened=[];monkeypatch.setattr(scenarios,'_open_path',lambda p:opened.append(p) or {'success':True,'path':p})
  yield s,opened
  s.finish()

def test_changed_definition_cannot_reuse_confirmation(session):
 s,opened=session;turn=s.agent.handle('qa scene')
 assert turn.pending and turn.pending.arguments.get('fingerprint')
 s.config.data['scenarios']['items'][0]['steps'][0]['path']='C:/QA/changed.cmd'
 result=s.agent.confirm_pending(True)
 assert result.success is False and opened==[]

def test_unchanged_confirmed_definition_runs_exact_path(session):
 s,opened=session;turn=s.agent.handle('qa scene')
 assert turn.pending and not opened
 result=s.agent.confirm_pending(True)
 assert result.success is True and opened==['C:/QA/first.cmd']

def test_unbound_registry_call_fails_closed(session):
 s,opened=session
 result=s.agent.tools.execute('scenario',{'action':'run','id':'qa-safe-id'})
 assert not result.success and not opened

def test_disabled_scenario_not_registered_as_voice_phrase(session):
 s,opened=session;s.config.data['scenarios']['items'][0]['enabled']=False
 assert 'qa scene' not in [x.casefold() for x in s.agent._scenario_phrases()]
 args={'action':'run','id':'qa-safe-id'};bind_scenario_arguments(s.config,args)
 result=s.agent.tools.execute('scenario',args)
 assert not result.success and not opened

def test_binding_never_reauthorizes_old_fingerprint(session):
 s,_=session;args={'action':'run','id':'qa-safe-id'};bind_scenario_arguments(s.config,args);old=args['fingerprint']
 s.config.data['scenarios']['items'][0]['steps'].append({'type':'wait','seconds':1})
 bind_scenario_arguments(s.config,args)
 assert args['fingerprint']==old

def test_settings_run_signal_uses_confirmed_mainwindow_route():
 from pathlib import Path
 p=Path(__file__).resolve().parents[1]/'jarvis/ui/main_window.py';text=p.read_text()
 assert 'dialog.scenario_run_requested.connect(self._run_shelf_scenario)' in text
 assert 'args["fingerprint"] = scenario_fingerprint(chosen)' in text
