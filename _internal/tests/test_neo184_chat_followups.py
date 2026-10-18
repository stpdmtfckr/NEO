"""Independent routing oracle; mocked conversation text is not a model quality test."""
import copy
from pathlib import Path
import pytest
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution

CASES={
 'ru':['Скажи то же самое короче','Объясни проще','Можешь привести конкретный пример?','Почему ты так считаешь?'],
 'en':['Say the same thing more briefly','Explain more simply','Can you give a specific example?','Why do you think that?'],
 'de':['Sag dasselbe kürzer','Erkläre es einfacher','Kannst du ein konkretes Beispiel nennen ?','Warum denkst du das?'],
 'fr':['Dis la même chose plus brièvement','Explique plus simplement','Peux-tu donner un exemple concret ?','Pourquoi penses-tu cela ?'],
 'es':['Di lo mismo más brevemente','Explícalo más sencillo','¿Puedes dar un ejemplo concreto?','¿Por qué piensas eso?'],
 'it':['Di la stessa cosa più brevemente','Spiegalo più semplicemente','Puoi fare un esempio concreto?','Perché la pensi così?'],
 'pt':['Diz o mesmo de forma mais breve','Explica de forma mais simples','Podes dar um exemplo concreto?','Porque pensas isso?'],
}
@pytest.fixture(scope='module')
def session(tmp_path_factory):
 with external_io_blocked():
  s=AtlasSession(tmp_path_factory.mktemp('followup184')/'session')
  for d in s.agent.tools._tools.values():d.handler=lambda a:ToolExecution(True,'QA inert',{})
  s.agent.tools.set_execution_guard(lambda n,a,r:s.calls.append({'tool':n,'arguments':copy.deepcopy(a)}) or None)
  yield s
  s.finish()

@pytest.mark.parametrize('code,text',[(c,t) for c,rows in CASES.items() for t in rows])
def test_context_rewrite_uses_conversation_not_action_planner(session,monkeypatch,code,text):
 from jarvis.runtime import local_chat169
 session.reset_case(code);local_chat169.clear_history()
 session.agent._append_history({'role':'user','content':'Earlier question'})
 session.agent._append_history({'role':'assistant','content':'Earlier complete explanation'})
 seen=[]
 monkeypatch.setattr(local_chat169,'answer',lambda *a,**k:seen.append((a[2],a[3],k.get('scope'))) or 'QA conversation answer')
 turn=session.agent.handle(text)
 assert seen==[(text,code,'desktop')]
 assert not session.calls and not turn.pending
 assert 'chat_followup184.rephrase' in turn.tool_details

@pytest.mark.parametrize('code,command',[
 ('ru','установи громкость на 25 процентов'),('en','set the volume to 25 percent'),
 ('de','stelle die Lautstärke auf 25 Prozent'),('fr','règle le volume à 25 pour cent'),
 ('es','ajusta el volumen al 25 por ciento'),('it','imposta il volume al 25 per cento'),
 ('pt','define o volume para 25 por cento')])
def test_real_desktop_command_after_answer_is_not_a_chat_rewrite(session,monkeypatch,code,command):
 from jarvis.runtime import local_chat169
 session.reset_case(code);local_chat169.clear_history()
 session.agent._append_history({'role':'assistant','content':'Earlier complete explanation'})
 monkeypatch.setattr(local_chat169,'answer',lambda *a,**k:pytest.fail('Action routed into chat'))
 session.agent.handle(command)
 assert len(session.calls)==1 and session.calls[0]['tool']=='audio',session.calls
 assert session.calls[0]['arguments'].get('value')==25,session.calls

@pytest.mark.parametrize('code',list(CASES))
def test_quoted_or_appended_command_is_outside_the_rewrite_grammar(code):
 from jarvis.runtime.chat_followup184 import is_followup
 phrase=CASES[code][0]
 assert not is_followup('“'+phrase+'”',code)
 assert not is_followup(phrase+', open application Calculator',code)
