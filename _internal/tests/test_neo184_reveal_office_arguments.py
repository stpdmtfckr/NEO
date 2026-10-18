"""Published reveal destinations and document courtesy text are not payload."""
import copy
import pytest
from jarvis.runtime import office178
from jarvis.runtime.natural_commands import TASK_FRAMES, normalize_natural_command
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution

@pytest.fixture(scope='module')
def session(tmp_path_factory):
 with external_io_blocked():
  s=AtlasSession(tmp_path_factory.mktemp('reveal_office184')/'session')
  for d in s.agent.tools._tools.values():d.handler=lambda a:ToolExecution(True,'QA inert',{})
  s.agent.tools.set_execution_guard(lambda n,a,r:s.calls.append((n,copy.deepcopy(a))) or None)
  yield s;s.finish()

@pytest.mark.parametrize('prefix',['',*[x+': ' for x in TASK_FRAMES['en']]])
def test_locate_in_explorer_preserves_destination(session,tmp_path,prefix):
 source=tmp_path/'QA Data.txt';source.write_text('inert fixture')
 session.reset_case('en');session.agent.handle(prefix+f'locate file {source} in Explorer')
 assert session.calls==[('filesystem',{'action':'reveal','source':str(source)})]

@pytest.mark.parametrize('phrase',['locate file QA.txt','find file QA.txt'])
def test_plain_search_remains_search(phrase):
 assert normalize_natural_command(phrase,'en')=='найди файл QA.txt'

@pytest.mark.parametrize('phrase,title',[
 ('crée un document texte éditable, s’il te plaît','texte éditable'),
 ('create a document QA, please','QA'),
 ('создай документ План, пожалуйста','План'),
 ('erstelle ein Dokument QA, bitte','QA'),
 ('crea un documento QA, por favor','QA'),
 ('crea un documento QA, per favore','QA'),
 ('cria um documento QA, por favor','QA'),
])
def test_courtesy_not_an_implicit_document_title(phrase,title):
 code={'crée':'fr','create':'en','создай':'ru','erstelle':'de','cria':'pt'}.get(phrase.split()[0], 'it' if 'per favore' in phrase else 'es')
 assert office178.parse(phrase,code)==('office',{'action':'create','kind':'docx','title':title})

@pytest.mark.parametrize('phrase,title',[
 ('create a document "QA, please"','QA, please'),
 ('crée un document «QA, s’il te plaît»','QA, s’il te plaît'),
])
def test_quoted_title_keeps_literal_courtesy(phrase,title):
 assert office178.parse(phrase,'en' if phrase.startswith('create') else 'fr')[1]['title']==title

def test_body_keeps_literal_courtesy():
 assert office178.parse('create a document with text: Say hello, please','en')[1]['text']=='Say hello, please'

@pytest.mark.parametrize('phrase,source',[
 ('find file "QA Report.txt" in Explorer','QA Report.txt'),
 ('locate the file "QA Report.txt" in File Explorer','QA Report.txt'),
])
def test_reveal_quoted_source(phrase,source):
 from jarvis.runtime.file_requests162 import parse_file_request
 assert parse_file_request(phrase,'en')==('filesystem',{'action':'reveal','source':source})

def test_explorer_words_inside_filename_are_not_destination():
 from jarvis.runtime.file_requests162 import parse_file_request
 assert parse_file_request('locate file "QA in Explorer"','en')==('search',{'action':'files','literal':True,'query':'QA in Explorer'})

@pytest.mark.parametrize('code,phrase',[
 ('ru','прочитай этот файл'),('ru','прочитай файлы'),('en','read this file'),
 ('de','lies diese datei'),('fr','lis ce fichier'),('es','lee este archivo'),
 ('it','leggi questo file'),('pt','lê este ficheiro'),
])
@pytest.mark.parametrize('suffix',['.txt','.docx'])
def test_read_selected_file_uses_its_actual_kind(session,tmp_path,monkeypatch,code,phrase,suffix):
 from jarvis.runtime import context173
 path=tmp_path/('QA'+suffix);path.write_text('inert resolver fixture')
 monkeypatch.setattr(context173,'resolve_file',lambda *a,**k:context173.Found(str(path),'explorer_selection'))
 monkeypatch.setattr(office178,'_recent',lambda *a:'')
 monkeypatch.setattr(office178.Live,'find',lambda *a:None)
 session.reset_case(code);session.agent.handle(phrase)
 expected=('filesystem',{'action':'read_text','source':str(path)}) if suffix=='.txt' else ('office',{'action':'read','kind':'','name':str(path)})
 assert session.calls==[expected]
