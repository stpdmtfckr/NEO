"""Incomplete command heads ask for missing data, never type their syntax."""
import copy
import pytest
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution
CASES = [('en', 'write files, please', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('en', 'write now files', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('en', 'write files', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('de', 'schreibe bitte dateien', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('de', 'schreibe dateien', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('de', 'schreibe jetzt dateien', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('fr', 'écris fichiers, s’il te plaît', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('fr', 'écris fichiers', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('fr', 'écris maintenant fichiers', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('es', 'escribe archivos', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('es', 'escribe ahora archivos', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('es', 'escribe archivos, por favor', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('it', 'scrivi adesso file', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('pt', 'escreva arquivos', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('pt', 'escreva arquivos, por favor', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('pt', 'escreva agora arquivos', {'tool': 'filesystem', 'arguments': {'action': 'write_text'}, 'missing': 'source'}), ('ru', 'введи текст', {'tool': 'input_control', 'arguments': {'action': 'type_text'}, 'missing': 'text'}), ('en', 'type now input', {'tool': 'input_control', 'arguments': {'action': 'type_text'}, 'missing': 'text'}), ('en', 'type input, please', {'tool': 'input_control', 'arguments': {'action': 'type_text'}, 'missing': 'text'}), ('en', 'type input', {'tool': 'input_control', 'arguments': {'action': 'type_text'}, 'missing': 'text'}), ('en', 'write to', {'tool': 'telegram_messages', 'arguments': {'action': 'send'}, 'missing': 'target'}), ('fr', 'écris à', {'tool': 'telegram_messages', 'arguments': {'action': 'send'}, 'missing': 'target'}), ('ru', 'введи под диктовку', {'tool': 'text_edit', 'arguments': {'action': 'dictate'}, 'missing': 'text'}), ('ru', 'введи в поле', {'tool': 'input_control', 'arguments': {'action': 'set_control_text'}, 'missing': 'name'})]
@pytest.fixture(scope='module')
def session(tmp_path_factory):
 with external_io_blocked():
  s=AtlasSession(tmp_path_factory.mktemp('incomplete184')/'session')
  for d in s.agent.tools._tools.values():d.handler=lambda a:ToolExecution(True,'QA inert',{})
  s.agent.tools.set_execution_guard(lambda n,a,r:s.calls.append((n,copy.deepcopy(a))) or None)
  yield s;s.finish()
@pytest.mark.parametrize('code,phrase,expected',CASES)
def test_incomplete_head_is_not_literal_dictation(session,code,phrase,expected):
 session.reset_case(code)
 turn=session.agent.handle(phrase)
 pending=session.agent.conversations.get('desktop').pending_intent
 assert not session.calls and not turn.pending
 assert pending is not None and pending.tool==expected['tool']
 assert pending.arguments.get('action')==expected['arguments']['action']
 assert pending.missing().name==expected['missing']
