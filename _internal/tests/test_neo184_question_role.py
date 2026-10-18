"""Actual OllamaClient role resolution with inert HTTP; no model quality claim."""
import json
from types import SimpleNamespace
import pytest
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.ollama_client import OllamaClient

QUESTIONS={'ru':'Почему небо голубое?','en':'Why is the sky blue?','de':'Warum ist der Himmel blau?',
 'fr':'Pourquoi le ciel est-il bleu ?','es':'¿Por qué el cielo es azul?','it':'Perché il cielo è blu?','pt':'Porque é que o céu é azul?'}

@pytest.fixture(scope='module')
def session(tmp_path_factory):
 with external_io_blocked():
  s=AtlasSession(tmp_path_factory.mktemp('question_role184')/'session')
  yield s;s.finish()

class Response:
 def raise_for_status(self):pass
 def json(self):return {'message':{'content':'SELECTED_CHAT_RESULT'}}
 def __enter__(self):return self
 def __exit__(self,*a):return False
 def iter_lines(self,**kw):yield json.dumps({'message':{'content':'SELECTED_CHAT_RESULT'},'done':True})

@pytest.mark.parametrize('code',list(QUESTIONS))
@pytest.mark.parametrize('state',['enabled','missing','disabled'])
def test_general_question_honors_exact_chat_selection_and_never_falls_back_to_planner(session,monkeypatch,code,state):
 from jarvis.runtime import local_chat169,agent_llm170
 session.reset_case(code);local_chat169.clear_history();local_chat169.reset_availability();agent_llm170.reset_availability()
 session.config.data['ollama']['chat_model']='selected-chat:1'
 # 186: information questions go to SearchAPI by default (TXT «Маршрут вопросов»); this test
 # covers the explicit local-answer choice, so it opts into it.
 session.config.data.setdefault('local_chat',{}).update(enabled=state!='disabled',questions='local',information_questions='local')
 session.config.data.setdefault('llm_agent',{}).update(model='different-planner:1',base_model='different-planner:1',fine_tuned='',allow_large_fallback=False)
 client=OllamaClient(session.config)
 monkeypatch.setattr(client,'_list_models',lambda force=False:['different-planner:1']+([] if state=='missing' else ['selected-chat:1']))
 calls=[]
 def post(url,json=None,**kw):calls.append({'url':url,'payload':json});return Response()
 client._session=SimpleNamespace(post=post)
 session.agent.ollama=client
 monkeypatch.setattr(agent_llm170,'decide',lambda *a,**kw:pytest.fail('A pure question called the action planner'))
 turn=session.agent.handle(QUESTIONS[code])
 assert not session.calls and not turn.pending
 if state=='enabled':
  assert turn.text=='SELECTED_CHAT_RESULT',turn
  assert len(calls)==1 and calls[0]['payload']['model']=='selected-chat:1',calls
  assert 'format' not in calls[0]['payload']
 else:
  assert calls==[] and 'local_chat.no_model' in (turn.tool_details or []),turn
  assert turn.text and turn.text!='SELECTED_CHAT_RESULT'


STATEMENTS={
 'ru':'Сегодня был долгий день, хочу спокойно поговорить',
 'en':'Today was a long day and I feel like chatting',
 'de':'Heute war ein langer Tag und ich möchte reden',
 'fr':'La journée a été longue et je veux discuter',
 'es':'Hoy fue un día largo y quiero conversar',
 'it':'Oggi è stata una giornata lunga e vorrei parlare',
 'pt':'Hoje foi um dia longo e quero conversar',
}

@pytest.mark.parametrize('code',list(STATEMENTS))
@pytest.mark.parametrize('state',['enabled','missing','disabled'])
def test_nonquestion_planner_reply_cannot_bypass_selected_chat_model(session,monkeypatch,code,state):
 from jarvis.runtime import local_chat169,agent_llm170
 session.reset_case(code);local_chat169.clear_history();local_chat169.reset_availability()
 session.config.data['ollama']['chat_model']='selected-chat:1'
 session.config.data.setdefault('local_chat',{}).update(enabled=state!='disabled')
 session.config.data.setdefault('llm_agent',{}).update(model='different-planner:1',base_model='different-planner:1',fine_tuned='',allow_large_fallback=False)
 client=OllamaClient(session.config)
 monkeypatch.setattr(client,'_list_models',lambda force=False:['different-planner:1']+([] if state=='missing' else ['selected-chat:1']))
 calls=[]
 def post(url,json=None,**kw):calls.append(json);return Response()
 client._session=SimpleNamespace(post=post);session.agent.ollama=client
 monkeypatch.setattr(agent_llm170,'decide',lambda *a,**kw:agent_llm170.Decision('reply',text='WRONG_PLANNER_REPLY',model='different-planner:1'))
 turn=session.agent._llm_agent170(STATEMENTS[code],code,None)
 assert turn is not None and turn.text!='WRONG_PLANNER_REPLY'
 assert not session.calls and not turn.pending
 if state=='enabled':
  assert turn.text=='SELECTED_CHAT_RESULT'
  assert len(calls)==1 and calls[0]['model']=='selected-chat:1'
 else:
  assert calls==[] and turn.text!='SELECTED_CHAT_RESULT'


def test_explicit_action_clarification_stays_with_planner(session,monkeypatch):
 from jarvis.runtime import agent_llm170,local_chat169
 session.reset_case('en')
 monkeypatch.setattr(agent_llm170,'decide',lambda *a,**kw:agent_llm170.Decision('ask',text='Which application should I open?'))
 monkeypatch.setattr(local_chat169,'answer',lambda *a,**kw:pytest.fail('Action clarification called chat'))
 turn=session.agent._llm_agent170('Open application','en',None)
 assert turn.text=='Which application should I open?' and 'llm_agent.ask' in turn.tool_details
 assert not session.calls


def test_explicit_action_reply_is_not_reinterpreted_as_chitchat(session,monkeypatch):
 from jarvis.runtime import agent_llm170,local_chat169
 session.reset_case('en')
 monkeypatch.setattr(agent_llm170,'decide',lambda *a,**kw:agent_llm170.Decision('reply',text='Please clarify the application path'))
 monkeypatch.setattr(local_chat169,'answer',lambda *a,**kw:pytest.fail('Action reply called chat'))
 turn=session.agent._llm_agent170('Open application Calculator','en',None)
 assert turn.text=='Please clarify the application path' and 'llm_agent.reply' in turn.tool_details
 assert not session.calls
