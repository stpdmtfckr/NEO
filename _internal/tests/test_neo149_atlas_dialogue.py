"""Independent NEO149 contracts. Real Agent; OS/network execution denied.

The original 114 cases are public atlas phrases, not copied user settings.
Expected arguments and reaction types are fixed, never learned from the output.
"""
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from difflib import SequenceMatcher
import importlib.util
import json
import re
import pytest

INTERNAL = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('neo149_runner_tests', INTERNAL/'tools/atlas_all_test.py')
runner = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.atlas_contracts149 import prepare_fixtures, render, evaluate, canonical_expected
from jarvis.testing.atlas_scenarios149 import cases, outcome_cases
from jarvis.testing.isolation import external_io_blocked
from jarvis.runtime.natural_commands import LANGUAGES, norm
from jarvis.runtime.social_atlas import dialogue_additions, catalog, response_options
from jarvis.runtime.command_contracts149 import parse_music_request, polite_tail
from jarvis.runtime import cached_re
from jarvis.tools.registry import ToolExecution

@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        value = AtlasSession(tmp_path_factory.mktemp('neo149')/'run')
        prepare_fixtures(value)
        yield value
        value.finish()

@pytest.mark.parametrize('code', LANGUAGES)
def test_multiturn_negative_and_outcome_contracts(session, code):
    session.reset_case(code)
    for row in list(cases(code)) + list(outcome_cases(code)):
        result = runner.check_phrase(session, asdict(runner.Phrase(**row)))
        assert result['status'] in {'CLARIFICATION_PASS','ARGUMENTS_PASS','NEGATIVE_PASS','OUTCOME_PASS'}, result
        assert not result.get('localization_warning'), result
        if row.get('outcome_fixture'):
            assert result['submitted'] is False and result['reply_received'] is False
        else:
            assert result['submitted'] and result['reply_received']

ORIGINAL = json.loads((INTERNAL/'tests/fixtures/neo149/invalid148_cases.json').read_text(encoding='utf-8'))
@pytest.mark.parametrize('case', ORIGINAL, ids=lambda r: r['locale']+':'+r['phrase'])
def test_original_schema_failures_have_valid_route_or_specific_question(session, case):
    session.reset_case(case['locale'])
    result = runner.check_phrase(session, asdict(runner.Phrase(**case)))
    assert result['status'] == ('CLARIFICATION_PASS' if case['expected_kind'] else 'ROUTE_PASS'), result
    assert not result.get('localization_warning'), result

HELP = [(code, group['family'], text) for group in dialogue_additions()
        for code, phrases in group['phrases'].items() for text in phrases]
@pytest.mark.parametrize('code,family,text', HELP)
def test_new_dialogue_has_native_reply_without_actions(session, code, family, text):
    session.reset_case(code)
    result = runner.check_phrase(session, asdict(runner.Phrase(code, text, source='social_atlas')))
    assert result['status'] == 'DIALOGUE_RESPONSE', result
    assert not result.get('localization_warning'), result
    assert len(response_options(family,code)) >= 2

@pytest.mark.parametrize('code,text,tool,action,expected', [
 ('ru','добавь таймер','timer','timer','clarification'),
 ('ru','покажи активные таймеры и напоминания','timer','list',''),
 ('ru','какие приложения сейчас активны','application','list_active',''),
 ('fr','Prochaine tâche: peux-tu lancer un dé','chance','roll_die',''),
 ('es','Siguiente tarea: puedes lanzar un dado','chance','roll_die',''),
 ('ru','открой приложение, пожалуйста','application','open','clarification'),
 ('en','open now application','application','open','clarification'),
 ('fr','ouvre maintenant application','application','open','clarification'),
 ('pt','abre agora a aplicação','application','open','clarification'),
])
def test_reported_semantic_defects(session,code,text,tool,action,expected):
    session.reset_case(code)
    r=runner.check_phrase(session,asdict(runner.Phrase(code,text,tool,action,expected_kind=expected)))
    assert r['status']==('CLARIFICATION_PASS' if expected else 'ROUTE_PASS'),r

@pytest.mark.parametrize('code,find,play',[
 ('ru','найди трек Imagine в Spotify','включи трек Imagine в Spotify'),
 ('en','find the song Imagine on Spotify','play the song Imagine on Spotify'),
 ('de','finde das Lied Imagine auf Spotify','spiele das Lied Imagine auf Spotify'),
 ('fr','trouve la chanson Imagine sur Spotify','joue la chanson Imagine sur Spotify'),
 ('es','busca la canción Imagine en Spotify','reproduce la canción Imagine en Spotify'),
 ('it','trova la canzone Imagine su Spotify','riproduci la canzone Imagine su Spotify'),
 ('pt','procura a música Imagine no Spotify','toca a música Imagine no Spotify')])
def test_find_and_play_remain_distinct_with_exact_title(session,code,find,play):
    for text,action in [(find,'search_track'),(play,'play_track')]:
        session.reset_case(code)
        r=runner.check_phrase(session,asdict(runner.Phrase(code,text,'spotify',action,expected_args={'query':'Imagine'})))
        assert r['status']=='ARGUMENTS_PASS',r

@pytest.mark.parametrize('action', ['search_track','find_track','find_song'])
def test_spotify_lookup_never_sends_playback_command(action):
    from jarvis.integrations.spotify import SpotifyAdapter
    obj=SpotifyAdapter.__new__(SpotifyAdapter)
    obj.search_track=Mock(return_value={'title':'NEO QA','artist':'QA Artist','uri':'spotify:track:TEST'})
    obj._choose_device=Mock(side_effect=AssertionError('lookup must not select a player'))
    obj._request=Mock(side_effect=AssertionError('lookup must not mutate playback'))
    result=obj._execute(action,query='NEO QA')
    assert result['search_only'] and result['playback_changed'] is False
    obj.search_track.assert_called_once_with('NEO QA')
    obj._request.assert_not_called()

@pytest.mark.parametrize('action', ['search_track','find_track','find_song'])
def test_yandex_lookup_never_plays_and_does_not_leak_internal_object(action):
    from jarvis.integrations.yandex_music import YandexMusicAdapter
    obj=YandexMusicAdapter.__new__(YandexMusicAdapter)
    obj.resolve_track=Mock(return_value={'title':'NEO QA','artist':'QA Artist','id':'QA','track':object()})
    obj.play_track=Mock(side_effect=AssertionError('lookup must not play'))
    obj.transport=Mock(side_effect=AssertionError('lookup must not control transport'))
    result=obj._execute(action,query='NEO QA')
    assert result['search_only'] and result['playback_changed'] is False
    assert 'track' not in result and 'track' not in result['matches'][0]
    obj.play_track.assert_not_called()

@pytest.mark.parametrize('pattern,text,flags',[(r'(\w+)', 'abc ABC 123',0),
    (r'(\w+)', 'é À NEO',re.I), (r'^a.+$', 'abc\naba',re.M),
    (r'\bNEO\b', 'neo NEO Neo',re.I), (b'(ab+)',b'ab abb abbb',0)])
def test_scoped_regex_cache_matches_stdlib(pattern,text,flags):
    for operation in ('findall','split','sub','subn'):
        args=(pattern,text) if operation in {'findall','split'} else (pattern,b'X' if isinstance(text,bytes) else 'X',text)
        assert getattr(cached_re,operation)(*args,flags=flags)==getattr(re,operation)(*args,flags=flags)
    for operation in ('search','match','fullmatch'):
        left=getattr(cached_re,operation)(pattern,text,flags)
        right=getattr(re,operation)(pattern,text,flags)
        assert ((left.span(),left.groups()) if left else None) == ((right.span(),right.groups()) if right else None)
    assert [m.span() for m in cached_re.finditer(pattern,text,flags)]==[m.span() for m in re.finditer(pattern,text,flags)]


def test_compilation_cache_is_bounded_and_preserves_errors():
    for i in range(2100): cached_re.compile('neo_bound_'+str(i))
    assert cached_re.compile.cache_info().currsize <= 2048
    p=re.compile('NEO')
    assert cached_re.compile(p) is p
    with pytest.raises(ValueError):cached_re.compile(p,re.I)
    with pytest.raises(re.error):cached_re.compile('[')
    cached_re.compile.cache_clear()


def naive_game(adapters,text):
    """Original exact+fuzzy algorithm without pruning, independent reference."""
    t=norm(text);words=t.split();best=None
    for adapter in adapters:
        score=0.0
        for alias in [adapter.display_name,adapter.game_id.replace('_',' '),*adapter.aliases]:
            a=norm(alias)
            if not a:continue
            if re.search(rf'(?<!\w){re.escape(a)}(?!\w)',t):score=max(score,1.0);continue
            aw=a.split()
            if len(aw)==1:
                for w in words:
                    if len(w)>=4 and len(a)>=4:score=max(score,SequenceMatcher(None,w,a).ratio())
            else:
                for width in {max(1,len(aw)-1),len(aw),len(aw)+1}:
                    for i in range(max(0,len(words)-width+1)):
                        score=max(score,SequenceMatcher(None,' '.join(words[i:i+width]),a).ratio())
        if best is None or score>best[0]:best=(score,adapter)
    return {'game_id':best[1].game_id,'game_name':best[1].display_name,'explicit_confidence':best[0]} if best and best[0]>=0.8 else {}

@pytest.mark.parametrize('text',['Elden Ring','elden rign','Skyrim','Skyriim','Minecraft','minecrfat','Terraria',
 'unknown request','show game list','покажи статус музыки','start NEO QA', 'play a song',
 'мне нужна помощь с elden ring','tell me about skyirm','минекрафт','скайрим','terraria guide','elden ring quest'])
def test_game_fuzzy_upper_bound_pruning_preserves_results(text):
    from jarvis.game_web.service import GameWebKnowledgeService
    service=GameWebKnowledgeService.__new__(GameWebKnowledgeService)
    adapters=[SimpleNamespace(game_id='elden_ring',display_name='Elden Ring',aliases=['элден ринг']),
              SimpleNamespace(game_id='skyrim',display_name='Skyrim',aliases=['скайрим']),
              SimpleNamespace(game_id='minecraft',display_name='Minecraft',aliases=['майнкрафт']),
              SimpleNamespace(game_id='terraria',display_name='Terraria',aliases=[])]
    service.manager=SimpleNamespace(adapters=adapters)
    assert service._explicit_game(text)==naive_game(adapters,text)


def test_fixture_values_cover_all_atlas_templates(session):
    unknown=[];templates=0
    for code in LANGUAGES:
        for case in runner.load_cases(INTERNAL,code):
            if '{' not in case.phrase:continue
            templates+=1
            text,used,expected,missing=render(asdict(case),session.workspace)
            if missing:unknown.append((code,case.phrase,missing))
            assert not missing or '{' in text
    assert templates>17000
    assert unknown==[]
    assert session.config.get('scenarios.items')[0]['name']=='NEO QA'

@pytest.mark.parametrize('phrase,tool,action,field,wanted',[
 ('set volume {value}','audio','set_volume','value',25),
 ('copy {source} to {destination}','filesystem','copy','destination','path'),
 ('hotkey {keys}','input_control','hotkey','keys',['ctrl','shift','k']),
 ('activate {topic}','browser_advanced','activate_by_topic','query','NEO QA')])
def test_oracle_detects_wrong_args_even_when_action_matches(tmp_path,phrase,tool,action,field,wanted):
    case=asdict(runner.Phrase('en',phrase,tool,action))
    text,used,expected,missing=render(case,tmp_path)
    assert missing==[] and '{' not in text
    args=dict(expected['arguments']);args[field]='WRONG'
    turn=SimpleNamespace(pending=None,text='text')
    state=SimpleNamespace(pending_intent=None,pending_shell=None)
    result,diffs,checked=evaluate(case,turn,[{'tool':expected['tool'],'arguments':args}],[],state,None,expected)
    assert result=='ARGUMENT_MISMATCH' and any(d['parameter']==field for d in diffs)


def test_unfilled_unknown_slot_never_reaches_agent(session):
    session.reset_case('en')
    case=asdict(runner.Phrase('en','open {not_a_known_slot}','application','open'))
    r=runner.check_phrase(session,case)
    assert r['status']=='NEEDS_FIXTURE' and not r['submitted'] and not session.calls


def test_no_action_contract_detects_extra_action():
    case={'expected_kind':'no_action','expected_tool':''}
    turn=SimpleNamespace(pending=None,text='Okay')
    state=SimpleNamespace(pending_intent=None,pending_shell=None)
    status,_,_=evaluate(case,turn,[{'tool':'application','arguments':{'action':'open','name':'NEO QA'}}],[],state,None,{'tool':'','arguments':{}})
    assert status=='UNEXPECTED_ACTION'


def test_search_alias_is_not_equivalent_to_play():
    assert canonical_expected('spotify',{'action':'find_song'})[1]['action']=='search_track'
    assert canonical_expected('spotify',{'action':'play_song'})[1]['action']=='play_track'


def test_politeness_normalization_preserves_literal_quoted_title():
    assert polite_tail('open "please"','en')=='open "please"'
    assert polite_tail('write note: hello, please','en')=='write note: hello, please'
    assert polite_tail('open application, please','en')=='open application'

@pytest.mark.parametrize('code,text,action,extra',[
 ('en','pin note NEO QA to the desktop','pin',{}),
 ('de','lies die Notiz NEO QA vor','read',{}),
 ('de','fixiere die Notiz NEO QA auf dem Desktop','pin',{}),
 ('it','fissa la nota NEO QA sul desktop','pin',{}),
 ('es','añade una nota nueva NEO QA a la estantería: NEO QA text','create',{'content':'NEO QA text'}),
])
def test_native_shelf_suffixes_are_not_part_of_title(session,code,text,action,extra):
    session.reset_case(code)
    row=runner.check_phrase(session,asdict(runner.Phrase(code,text,'shelf',action,expected_args={'title':'NEO QA',**extra})))
    assert row['status']=='ARGUMENTS_PASS',row

@pytest.mark.parametrize('code,text,action',[
 ('pt','toca a canção que estava antes desta no Spotify','previous'),
 ('pt','toca a canção atual em ciclo no Spotify','repeat_track'),
 ('pt','põe a música mais alta no Spotify','volume_up'),
 ('it','riproduci il brano successivo su Spotify','next'),
 ('en','turn shuffle on in Spotify','shuffle_on'),
])
def test_known_transport_phrases_are_not_song_queries(session,code,text,action):
    session.reset_case(code)
    row=runner.check_phrase(session,asdict(runner.Phrase(code,text,'spotify',action)))
    assert row['status']=='ROUTE_PASS',row
    assert all('query' not in c['arguments'] for c in row['calls'])


def test_single_cmd_contains_the_exact_checked_runner():
    # 150 consolidates entrypoints; no embedded stale copy of the Python runner.
    path=INTERNAL.parent/'NEO_TEST.cmd'
    text=path.read_text(encoding='utf-8-sig')
    assert '/s /k' in text and '-Mode test-all' in text
    assert {p.name.casefold() for p in INTERNAL.parent.glob('*.cmd') if p.name.casefold() in {'test.cmd', 'neo_test.cmd'}} == {'neo_test.cmd'}
    unified=(INTERNAL/'tools/test_all.py').read_text(encoding='utf-8')
    assert 'tools/atlas_all_test.py' in unified and 'integration_probes150' in unified
    compile(unified,str(INTERNAL/'tools/test_all.py'),'exec')


def test_fixture_translate_default_operation_is_only_oracle_compatibility():
    tool,args=canonical_expected('translate',{'target_language':'en','text':'NEO QA text'})
    assert tool=='translate' and args['action']=='translate'
    source={'target_language':'en','text':'NEO QA text'}
    canonical_expected('translate',source)
    assert 'action' not in source
