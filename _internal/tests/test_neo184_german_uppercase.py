"""German uppercase SS is equivalent only in command literals, not slot data."""
import copy
import pytest
from jarvis.runtime.canonical_commands import parse_canonical_command
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution
CASES=[
 ('SCHLIESSE DAS PROGRAMM QA_Example_731','application',{'action':'close','name':'QA_Example_731'}),
 ('SCHALTE ALLE PROGRAMME AUSSER QA_Target_731 STUMM','audio',{'action':'mute_except','target':'QA_Target_731'}),
 ('SCHLIESSE DEN MINI-BROWSER','mini_browser',{'action':'close'}),
 ('SCHLIESSE DIE INFORMATIONSANZEIGE QA_Example_731','overlay',{'action':'close','name':'QA_Example_731'}),
 ('SETZE DIE TEXTGRÖSSE DER ANZEIGE QA_Example_731 AUF 16','overlay',{'action':'style','name':'QA_Example_731','font_size':16}),
 ('STELLE DIE GRÖSSE DES FENSTERS QA_Title_731 WIEDER HER','window',{'action':'restore','title':'QA_Title_731'}),
 ('SCHLIESSE DAS FENSTER QA_Title_731','window',{'action':'close','title':'QA_Title_731'}),
 ('ÄNDERE DIE GRÖSSE DES FENSTERS QA_Title_731 AUF 800 MAL 600','window',{'action':'move_resize','title':'QA_Title_731','width':800,'height':600}),
 ('SCHLIESSE DAS FENSTER MIT QA Text 731','window_advanced',{'action':'close_with_content','content':'QA Text 731'}),
]
@pytest.fixture(scope='module')
def session(tmp_path_factory):
 with external_io_blocked():
  s=AtlasSession(tmp_path_factory.mktemp('german_upper184')/'session')
  for d in s.agent.tools._tools.values():d.handler=lambda a:ToolExecution(True,'QA inert',{})
  s.agent.tools.set_execution_guard(lambda n,a,r:s.calls.append((n,copy.deepcopy(a))) or None)
  yield s;s.finish()
@pytest.mark.parametrize('phrase,tool,args',CASES)
def test_literal_template_accepts_german_uppercase(session,phrase,tool,args):
 parsed=parse_canonical_command(phrase,'de',lambda t:session.agent.tools.get(t).parameters)
 assert parsed is not None and not parsed.error
 assert (parsed.tool,parsed.arguments)==(tool,args)
@pytest.mark.parametrize('phrase,tool,args',CASES)
def test_full_agent_preserves_explicit_target(session,phrase,tool,args):
 session.reset_case('de');turn=session.agent.handle(phrase)
 calls=list(session.calls)
 if turn.pending:calls.append((turn.pending.tool_name,turn.pending.arguments))
 assert calls==[(tool,args)]
def test_slot_sharp_s_not_rewritten(session):
 p=parse_canonical_command('SCHLIESSE DAS PROGRAMM Straße CASE','de',lambda t:session.agent.tools.get(t).parameters)
 assert p.arguments['name']=='Straße CASE'
