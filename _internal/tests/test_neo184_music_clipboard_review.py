"""Independent action and payload contracts for meaningful phrase mutations."""
import copy
import pytest
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution

@pytest.fixture(scope='module')
def session(tmp_path_factory):
 with external_io_blocked():
  s=AtlasSession(tmp_path_factory.mktemp('musicreview184')/'session')
  for d in s.agent.tools._tools.values():d.handler=lambda a:ToolExecution(True,'QA inert',{})
  s.agent.tools.set_execution_guard(lambda n,a,r:s.calls.append((n,copy.deepcopy(a))) or None)
  yield s;s.finish()

@pytest.mark.parametrize('sep',[' ','  ','\t'])
@pytest.mark.parametrize('code,template,tool,action',[
 ('ru','поставь песню {q} следующей в очереди в Discord','discord','music_queue_next'),
 ('fr',"mets la chanson {q} en tête de la file d'attente dans Discord",'discord','music_queue_next'),
 ('fr',"mets la chanson {q} en tête de la file d'attente",'music','queue_next'),
 ('es','pon la canción {q} como la siguiente en la cola en Discord','discord','music_queue_next'),
 ('it','metti la canzone {q} come prossima in coda su Discord','discord','music_queue_next'),
 ('es','pon la lista de reproducción {q} en Discord','discord','music_playlist'),
 ('es','pon la lista de reproducción {q} en Spotify','spotify','playlist'),
])
def test_command_whitespace_keeps_action_and_literal_payload(session,sep,code,template,tool,action):
 phrase=template.replace(' ',sep).format(q='"QA  Next\tTrack"')
 session.reset_case(code);session.agent.handle(phrase)
 field='playlist' if action in {'playlist','music_playlist'} else 'query'
 assert session.calls==[(tool,{'action':action,field:'QA  Next\tTrack'})]

@pytest.mark.parametrize('verb',['найди','найдите','поищи','поищите'])
@pytest.mark.parametrize('suffix,tool',[('','music'),(' в Яндекс Музыке','yandex_music'),(' в Spotify','spotify')])
def test_search_does_not_play(session,verb,suffix,tool):
 session.reset_case('ru');session.agent.handle(verb+' песню QA_Query_731'+suffix)
 assert session.calls==[(tool,{'action':'search_track','query':'QA_Query_731'})]

@pytest.mark.parametrize('verb',['скоируй','скоипруй','скоппируй','скоаируй'])
def test_unknown_copy_verb_never_pastes_old_contents(session,verb):
 session.reset_case('ru');turn=session.agent.handle(verb+' в буфер обмена QA Text 731')
 assert all((tool,args)==('clipboard',{'action':'set','text':'QA Text 731'}) for tool,args in session.calls)
 assert not (turn.pending and turn.pending.arguments.get('action') in {'paste','paste_history'})

@pytest.mark.parametrize('phrase,tool,action',[('вставь из буфера обмена','copy','paste'),('очисти буфер обмена','clipboard','clear'),('скопируй в буфер обмена QA Text 731','clipboard','set')])
def test_explicit_clipboard_actions_preserved(session,phrase,tool,action):
 session.reset_case('ru');session.agent.handle(phrase)
 assert len(session.calls)==1 and session.calls[0][0]==tool and session.calls[0][1]['action']==action
