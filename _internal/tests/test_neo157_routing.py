"""Production-Agent music, everyday and original native atlas regressions."""
from pathlib import Path
import json
import pytest
from jarvis.testing.atlas_contracts149 import prepare_fixtures
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from tools.atlas_all_test import check_phrase
from jarvis.runtime.command_contracts150 import complete
from jarvis.runtime.music_requests157 import bare_transport157, parse_music_request157

ROOT=Path(__file__).resolve().parents[1]
CASES=json.loads((ROOT/'config/regression157_routes.json').read_text(encoding='utf-8'))
@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        s=AtlasSession(tmp_path_factory.mktemp('routing157')/'run')
        prepare_fixtures(s)
        yield s
        s.finish()

@pytest.mark.parametrize('case',CASES,ids=lambda r:r['locale']+':'+r['phrase'])
def test_real_agent_routes_native_request_without_web_fallback(session,case):
    session.reset_case(case['locale'])
    result=check_phrase(session,case)
    assert result['status'] in {'ROUTE_PASS','ARGUMENTS_PASS','CONFIRMATION_REQUIRED'}, result
    assert all(c['tool'] not in {'web','web_search','search_api','knowledge'} for c in result['calls']),result

@pytest.mark.parametrize('phrase,code',[
 ('не переключай музыку','ru'),('do not skip this song','en'),
 ('spiele das vorherige lied nicht','de'),("n'arrête pas la musique",'fr'),
 ('no detengas la música','es'),('non fermare la musica','it'),('não pare a música','pt'),
 ('найди песню "какие треки дальше"','ru'),('what songs were popular in 1980','en'),
 ('какие треки лучше для бега','ru'),('расскажи про очередь в программировании','ru'),
 ('что дальше','ru'),('what comes next','en'),('слово дальше запиши в заметку','ru'),
 ('next song in Spotify and Discord','en')])
def test_owned_grammar_does_not_capture_negation_quotes_topics_or_conflicting_providers(phrase,code):
    assert parse_music_request157(phrase,code) is None

@pytest.mark.parametrize('phrase,code,action', [('дальше','ru','next'),('назад','ru','previous'),('стоп','ru','stop'),('продолжить','ru','play'),('next','en','next'),('zurück','de','previous'),('suivante','fr','next'),('siguiente','es','next'),('indietro','it','previous'),('retoma','pt','play')])
def test_bare_transport_is_a_context_request_not_unconditional_local_playback(phrase,code,action):
    assert bare_transport157(phrase,code)==action
    assert parse_music_request157(phrase,code) is None

@pytest.mark.parametrize('code',['ru','en','de','fr','es','it','pt'])
def test_local_clock_returns_language_response_and_no_network(code,tmp_path):
    from jarvis.testing.atlas_session import AtlasConfig
    from jarvis.tools.integrations import _date_info
    cfg=AtlasConfig(tmp_path/code,code)
    time=_date_info(cfg,'current_time'); date=_date_info(cfg,'today')
    assert time.success and time.data['source']=='local_clock'
    assert date.success and date.data['source']=='local_clock'
    assert ':' in time.text and len(date.data['date'])==10

@pytest.mark.parametrize('code',['ru','en','de','fr','es','it','pt'])
def test_spotify_queue_read_only_real_titles(monkeypatch,code):
    from jarvis.integrations.spotify import SpotifyAdapter
    from jarvis.runtime.spotify_queue157 import format_spotify_queue157
    adapter=object.__new__(SpotifyAdapter); calls=[]
    track=lambda i:{'id':str(i),'name':'Track '+str(i),'artists':[{'name':'Artist'}],'duration_ms':100}
    def request(method,path,**kwargs):
        calls.append((method,path));return {'currently_playing':track(1),'queue':[track(i) for i in range(2,9)]}
    monkeypatch.setattr(adapter,'_request',request)
    data=adapter.queue_status();text=format_spotify_queue157(data,code)
    assert calls==[('GET','/me/player/queue')]
    assert data['playback_changed'] is False
    assert len(data['queue'])==5 and 'Track 1' in text and 'Track 2' in text and 'Track 7' not in text

@pytest.mark.parametrize('response',[{},None,{'queue':'unavailable'},{'queue':[None]}])
def test_spotify_malformed_queue_is_not_reported_as_empty(monkeypatch,response):
    from jarvis.integrations.spotify import SpotifyAdapter,SpotifyAPIError
    adapter=object.__new__(SpotifyAdapter)
    monkeypatch.setattr(adapter,'_request',lambda *a,**k:response)
    with pytest.raises(SpotifyAPIError):adapter.queue_status()

@pytest.mark.parametrize('code',['ru','en','de','fr','es','it','pt'])
def test_powershell_literal_script_keeps_normal_critical_confirmation(session,code):
    from jarvis.runtime.script_requests157 import HEADS
    from jarvis.runtime.natural_commands import LANGUAGES
    phrase=HEADS[LANGUAGES.index(code)][1]+": Write-Output 'NEO QA literal'"
    session.reset_case(code)
    result=check_phrase(session,{'locale':code,'phrase':phrase,'expected_tool':'powershell','expected_action':'','expected_args':{'script':"Write-Output 'NEO QA literal'"}})
    assert result['status']=='CONFIRMATION_REQUIRED',result
    assert len(result['calls'])==1 and result['calls'][0]['confirmation_required']
    assert result['calls'][0]['risk']=='critical'

def test_french_pure_search_catalogue_migration_never_labels_playback():
    from jarvis.runtime.catalogue_migration157 import migrate_search_heads157
    rows={'music.play_track':["s’il te plaît, recherche une piste sur le service musical actif",'trouve et joue une chanson','joue une chanson']}
    moved=migrate_search_heads157(rows,'fr')
    assert len(moved)==1 and moved[0]['to']=='music.search_track'
    assert rows['music.play_track']==['trouve et joue une chanson','joue une chanson']
    assert migrate_search_heads157(rows,'fr')==[]

@pytest.mark.parametrize('tool,label',[('music','sur le service musical actif'),('spotify','dans Spotify'),('yandex_music','dans Yandex Music')])
def test_french_missing_search_title_stays_read_only_after_clarification(session,tool,label):
    session.reset_case('fr')
    first={'locale':'fr','phrase':'s’il te plaît, recherche une piste '+label,'expected_tool':tool,'expected_action':'search_track','expected_kind':'clarification','scenario_id':'search157'+tool}
    result=check_phrase(session,first)
    assert result['status']=='CLARIFICATION_PASS',result
    result=check_phrase(session,{'locale':'fr','phrase':'NEO QA literal title','expected_tool':tool,'expected_action':'search_track','expected_args':{'query':'NEO QA literal title'},'scenario_id':first['scenario_id'],'keep_context':True})
    assert result['status']=='ARGUMENTS_PASS',result
    assert all(c['arguments'].get('action')=='search_track' for c in result['calls'])

@pytest.mark.parametrize('code,phrase',[
 ('fr','ouvre maintenant bureau'),('es','abre ahora escritorio'),
 ('it','apri adesso desktop'),('pt','abra agora área de trabalho')])
def test_desktop_now_means_known_folder_not_application_named_now(session,code,phrase):
    session.reset_case(code)
    result=check_phrase(session,{'locale':code,'phrase':phrase,'expected_tool':'desktop','expected_action':'open','expected_args':{'query':'Desktop','hint':'folder'}})
    assert result['status']=='ARGUMENTS_PASS',result
    assert len(result['calls'])==1
    assert result['calls'][0]['tool']=='desktop'
    assert result['calls'][0]['arguments']=={'action':'open','query':'Desktop','hint':'folder'}

@pytest.mark.parametrize('code,phrase,title',[
 ('fr','ouvre le dossier «maintenant bureau»','maintenant bureau'),
 ('es','abre la carpeta «ahora escritorio»','ahora escritorio'),
 ('it','apri la cartella «adesso desktop»','adesso desktop'),
 ('pt','abra a pasta «agora área de trabalho»','agora área de trabalho')])
def test_desktop_now_fix_keeps_literal_folder_names(session,code,phrase,title):
    from jarvis.runtime.everyday_requests157 import everyday_request157
    assert everyday_request157(phrase,code) is None
    session.reset_case(code)
    result=check_phrase(session,{'locale':code,'phrase':phrase,'expected_tool':'desktop','expected_action':'open','expected_args':{'query':title,'hint':'folder'}})
    assert result['status']=='ARGUMENTS_PASS',result
    assert len(result['calls'])==1 and result['calls'][0]['arguments']['query']==title
