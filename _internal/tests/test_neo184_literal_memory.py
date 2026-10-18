"""Literal memory keys survive the published German request wrappers."""
import copy
import pytest
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution

CASES = ['Bitte, merke dir enter: 25', 'Noch etwas: Bitte, merke dir enter: 25', 'Zum Anfang: merke dir enter: 25', 'Ich habe eine Bitte: merke dir enter: 25', 'Mach bitte Folgendes: Bitte, merke dir enter: 25', 'Nächste Aufgabe: Bitte, merke dir enter: 25', 'Hilf mir damit: merke dir enter: 25', 'Hilf mir damit: Bitte, merke dir enter: 25', 'Das brauche ich jetzt: merke dir enter: 25', 'Erledige bitte diese Aufgabe: Bitte, merke dir enter: 25', 'Nächste Aufgabe: merke dir enter: 25', 'Noch etwas: merke dir enter: 25', 'Ich habe eine Bitte: Bitte, merke dir enter: 25', 'Erledige bitte diese Aufgabe: merke dir enter: 25', 'Zum Anfang: Bitte, merke dir enter: 25', 'Mach bitte Folgendes: merke dir enter: 25', 'Das brauche ich jetzt: Bitte, merke dir enter: 25']
@pytest.fixture(scope='module')
def session(tmp_path_factory):
 with external_io_blocked():
  s=AtlasSession(tmp_path_factory.mktemp('literal_memory184')/'session')
  for d in s.agent.tools._tools.values():d.handler=lambda a:ToolExecution(True,'QA inert',{})
  s.agent.tools.set_execution_guard(lambda n,a,r:s.calls.append((n,copy.deepcopy(a))) or None)
  yield s;s.finish()

@pytest.mark.parametrize('phrase',CASES)
def test_wrapped_key_and_value_are_opaque(session,phrase):
 session.reset_case('de')
 session.agent.handle(phrase)
 assert session.calls==[('memory',{'action':'remember','key':'enter','value':'25'})]
