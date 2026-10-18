"""Whitespace in content-search syntax never changes the requested operation."""
import copy
import pytest
from jarvis.runtime.file_requests162 import parse_file_request
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution
HEADS={'ru':'найди файл по содержимому','en':'find file with the text','de':'finde eine Datei mit dem Inhalt','fr':'trouve un fichier avec le texte','es':'busca un archivo que contenga','it':'trova un file che contenga','pt':'encontra um ficheiro que contenha'}
@pytest.fixture(scope='module')
def session(tmp_path_factory):
 with external_io_blocked():
  s=AtlasSession(tmp_path_factory.mktemp('content_spacing184')/'session')
  for d in s.agent.tools._tools.values():d.handler=lambda a:ToolExecution(True,'QA inert',{})
  s.agent.tools.set_execution_guard(lambda n,a,r:s.calls.append((n,copy.deepcopy(a))) or None)
  yield s;s.finish()
@pytest.mark.parametrize('code',list(HEADS))
@pytest.mark.parametrize('space',[' ','  ','\t'])
def test_content_mode_survives_shell_whitespace(session,code,space):
 session.reset_case(code)
 session.agent.handle(HEADS[code].replace(' ',space)+space+'QA_Query_731')
 assert session.calls==[('search',{'action':'content','query':'QA_Query_731'})]
@pytest.mark.parametrize('code',list(HEADS))
def test_payload_whitespace_is_literal(code):
 assert parse_file_request(HEADS[code].replace(' ','\t')+'\tNeedle  KEEP\tCASE',code)==('search',{'action':'content','query':'Needle  KEEP\tCASE'})
