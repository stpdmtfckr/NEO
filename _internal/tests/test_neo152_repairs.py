"""Field failures: explicit independent oracles, isolated tools, no account actions."""
from dataclasses import asdict
from pathlib import Path
import json
import sqlite3
import zipfile
import pytest
from tools.atlas_all_test import Phrase,check_phrase
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.atlas_contracts149 import prepare_fixtures
from jarvis.testing.isolation import external_io_blocked

@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        s=AtlasSession(tmp_path_factory.mktemp('neo152')/'run');prepare_fixtures(s)
        yield s
        s.finish()

CASES=[
 ('de','zeige die Anzahl ungelesener E-Mails der letzten 24 Stunden','email','unread_recent24h',{},''),
 ('de','bitte zeige die Anzahl ungelesener E-Mails der letzten 24 Stunden','email','unread_recent24h',{},''),
 ('de','zeige die E-Mail-Anzahl der letzten 24 Stunden','email','recent24h',{},''),
 ('de','bitte zeige die E-Mail-Anzahl der letzten 24 Stunden','email','recent24h',{},''),
 ('en','show information about the selected application','application','info',{},'clarification'),
 ('ru','поставь песню NEO QA следующей в очереди в Discord','discord','music_queue_next',{'query':'NEO QA'},''),
 ('ru','включи мои любимые треки в Яндекс Музыке','yandex_music','my_tracks',{},''),
 ('ru','установи размер текста панели NEO QA 14','overlay','style',{'name':'NEO QA','font_size':14},''),
 ('de','suche und spiele den Song NEO QA in Spotify','spotify','play_track',{'query':'NEO QA'},''),
 ('fr','cherche et lance le titre NEO QA sur Spotify','spotify','play_track',{'query':'NEO QA'},''),
 ('de','suche und spiele den Song NEO QA in Yandex Music','yandex_music','play_track',{'query':'NEO QA'},''),
 ('fr','cherche et lance le titre NEO QA sur Yandex Music','yandex_music','play_track',{'query':'NEO QA'},''),
]
@pytest.mark.parametrize('code,text,tool,action,args,kind',CASES)
def test_user_report152(session,code,text,tool,action,args,kind):
    session.reset_case(code)
    result=check_phrase(session,asdict(Phrase(code,text,tool,action,expected_args=args,expected_kind=kind)))
    assert result['status'] in ({'CLARIFICATION_PASS'} if kind else {'ROUTE_PASS','ARGUMENTS_PASS'}),result
    assert not result.get('localization_warning'),result
    assert result['timings_ms']['agent']<5000,result  # No relaxed 30s timeout workaround.

@pytest.mark.parametrize('code,text,tool',[
 ('ru','найди и включи композицию NEO QA в Discord','discord'),
 ('ru','найди и включи композицию NEO QA в Spotify','spotify'),
 ('ru','найди и включи композицию NEO QA в Яндекс Музыке','yandex_music'),
 ('it','cerca e riproduci il pezzo NEO QA su Discord','discord'),
 ('it','cerca e riproduci il pezzo NEO QA su Spotify','spotify'),
 ('it','cerca e riproduci il pezzo NEO QA su Yandex Music','yandex_music'),
])
def test_compound_song_noun_is_not_part_of_title(session,code,text,tool):
    session.reset_case(code)
    action='music_bot_play' if tool=='discord' else 'play_track'
    result=check_phrase(session,asdict(Phrase(code,text,tool,action,expected_args={'query':'NEO QA'})))
    assert result['status']=='ARGUMENTS_PASS',result

@pytest.mark.parametrize('code,text,title',[
 ('ru','включи "Композицию NEO QA" в Spotify','Композицию NEO QA'),
 ('it','riproduci "Il pezzo NEO QA" su Spotify','Il pezzo NEO QA'),
])
def test_quoted_title_is_not_stripped(session,code,text,title):
    session.reset_case(code)
    result=check_phrase(session,asdict(Phrase(code,text,'spotify','play_track',expected_args={'query':title})))
    assert result['status']=='ARGUMENTS_PASS',result

@pytest.mark.parametrize('code,text',[('de','suche den Song NEO QA in Spotify'),('fr','cherche le titre NEO QA sur Spotify')])
def test_search_does_not_acquire_play_side_effect(session,code,text):
    session.reset_case(code)
    r=check_phrase(session,asdict(Phrase(code,text,'spotify','search_track',expected_args={'query':'NEO QA'})))
    assert r['status']=='ARGUMENTS_PASS',r

@pytest.mark.parametrize('code,text',[('ru','не включай мои любимые треки в Яндекс Музыке'),('de','starte keine Anwendung'),('en','why did you open the application'),('ru','мой друг сказал: поставь песню следующей в очереди')])
def test_new_read_and_queue_rules_keep_intent_boundary(session,code,text):
    session.reset_case(code)
    r=check_phrase(session,asdict(Phrase(code,text,expected_kind='no_action')))
    assert r['status']=='NEGATIVE_PASS',r

def test_sqlite_context_closes_and_rolls_back(tmp_path):
    from jarvis.runtime.file_index import _database
    path=tmp_path/'db.sqlite3'
    with _database(path) as db:db.execute('create table t (v integer)')
    with pytest.raises(sqlite3.ProgrammingError):db.execute('select 1')
    with pytest.raises(RuntimeError):
        with _database(path) as db:
            db.execute('insert into t values (1)')
            raise RuntimeError('abort transaction')
    with _database(path) as other:assert other.execute('select count(*) from t').fetchone()[0]==0
    path.unlink()  # Also checks removal with Windows locking when run there.

def test_file_index_releases_every_connection(tmp_path,monkeypatch):
    from jarvis.runtime import file_index
    connections=[];original=sqlite3.connect
    def capture(*a,**k):
        c=original(*a,**k);connections.append(c);return c
    monkeypatch.setattr(file_index.sqlite3,'connect',capture)
    index=file_index.FileIndex(tmp_path/'index.db',[])
    index.close()
    assert connections
    for c in connections:
        with pytest.raises(sqlite3.ProgrammingError):c.execute('select 1')

def test_json_export_retains_syntax_and_removes_sensitive_value(tmp_path):
    from jarvis.runtime.diagnostics import write_report
    from tools.test_all import pack
    run=tmp_path/'run';run.mkdir()
    value={'tests':[{'name':'case[https://example.invalid/?api_key=FAKE_VALUE]','status':'PASS'}],
           'token':'PRIVATE_FAKE_VALUE', 'first_token_timeout_seconds':12.5}
    write_report(run/'safe_tests_summary.json',value)
    dest=pack(run,{'status':'TEST_FIXTURE'},None)
    with zipfile.ZipFile(dest) as z:
        for n in z.namelist():
            if n.endswith('.json'):json.loads(z.read(n))
        body=z.read('reports/safe_tests_summary.json').decode()
        assert 'FAKE_VALUE' not in body and 'PRIVATE_FAKE_VALUE' not in body
        assert json.loads(body)['first_token_timeout_seconds']==12.5

def test_settings_numeric_timeout_not_confused_with_credential():
    from jarvis.runtime.diagnostics import sensitive_key
    from tools.test_all import settings_inventory
    from types import SimpleNamespace
    import tools.test_all as t
    defaults=json.loads((t.ROOT/'config/default_settings.json').read_text())
    def get(key,default=None):
        x=defaults
        for name in key.split('.'):x=x.get(name,default) if isinstance(x,dict) else default
        return float(x) if key=='ollama.first_token_timeout_seconds' else x
    value=settings_inventory(SimpleNamespace(get=get))
    assert not sensitive_key('ollama.first_token_timeout_seconds')
    assert sensitive_key('integrations.google.access_token')
    assert all(row['status']=='TYPE_OK' for row in value['rows']),value

@pytest.mark.parametrize('code',('ru','en','de','fr','es','it','pt'))
def test_email_read_catalogue_stays_in_email(code):
    from jarvis.runtime.command_contracts150 import complete
    root=Path(__file__).resolve().parents[1]
    data=json.loads((root/'jarvis/locales/commands'/f'{code}.json').read_text())
    for action in ('unread_recent24h','recent24h','today','total'):
        for phrase in data['action_phrases']['email.'+action]:
            if '{' in phrase:continue
            assert complete(phrase,code)==('email',{'action':action}),phrase

def test_catalogue_migration_is_only_explicit_find_and_play():
    from jarvis.runtime.catalogue_migration152 import migrate
    d={'spotify.search_track':['suche den Song {query} in Spotify','suche und spiele den Song {query} in Spotify']}
    rows=migrate(d,'de')
    assert len(rows)==1 and len(d['spotify.search_track'])==1
    assert len(d['spotify.play_track'])==1
    assert not migrate(d,'de')
