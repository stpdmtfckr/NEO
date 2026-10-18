"""Missing native slots through the real Agent, including cross-domain ambiguity."""
from pathlib import Path
import pytest
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.atlas_contracts149 import prepare_fixtures
from jarvis.testing.isolation import external_io_blocked
from jarvis.runtime.short_peripheral160 import partial, EXPLICIT


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        value=AtlasSession(tmp_path_factory.mktemp('peripheral160')/'run')
        prepare_fixtures(value)
        yield value
        value.finish()


CASES = [
 ('en','Analyze the screen:', 'screen', {'action':'analyze'}, ['explain the error dialog'], {'action':'analyze','instruction':'explain the error dialog'}),
 ('de','Wecke mich um','timer',{'action':'alarm'},['09:45'],{'action':'alarm','text':'09:45'}),
 ('pt','crie uma nota','shelf',{'action':'create','kind':'note'},['Shopping','Milk and bread'],{'action':'create','kind':'note','title':'Shopping','text':'Milk and bread'}),
 ('en','make folder','filesystem',{'action':'create_folder'},['C:\\Temp','Screenshots'],{'action':'create_folder','destination':'C:\\Temp\\Screenshots'}),
 ('fr','lance le mini-navigateur avec','mini_browser',{'action':'open'},['https://example.org'],{'action':'open','url':'https://example.org'}),
 ('fr','passe à la fenêtre','window',{'action':'focus'},['Notepad'],{'action':'focus','title':'Notepad'}),
 ('it','passa alla finestra','window',{'action':'focus'},['Notepad'],{'action':'focus','title':'Notepad'}),
 ('fr','active la fenêtre numéro','window_advanced',{'action':'focus_instance'},['Notepad','2'],{'action':'focus_instance','query':'Notepad','index':2}),
 ('de','schließe Punkt','shelf',{'action':'check','checked':True},['NEO QA Checklist','1'],{'action':'check','checked':True,'title':'NEO QA Checklist','index':0}),
]

@pytest.mark.parametrize('code,head,tool,args,answers,expected',CASES)
def test_native_head_collects_slots_then_executes_only_its_contract(session,code,head,tool,args,answers,expected):
    session.reset_case(code)
    turn=session.agent.handle(head)
    pending=session.agent.conversations.get('desktop').pending_intent
    assert turn.outcome=='clarification' and pending,(head,turn,pending,session.calls)
    assert not session.calls
    assert (pending.tool,pending.arguments)==(tool,args)
    for answer in answers:
        turn=session.agent.handle(answer)
    if tool=='shelf' and args.get('action')=='check':
        from jarvis.ui.shelf_store import ShelfStore
        row=next(x for x in ShelfStore(session.config.paths.root).list() if x['title']=='NEO QA Checklist')
        expected={**expected,'id':row['id'],'version':row['version']}
    assert len(session.calls)==1,(head,turn,session.calls)
    assert session.calls[0]['tool']==tool and session.calls[0]['arguments']==expected,(head,turn,session.calls)


@pytest.mark.parametrize('code,head',[
 ('ru','Переведи на'),('en','Translate into'),('de','Übersetze auf'),
 ('fr','Traduis en'),('es','Traduce al'),('it','Traduci in'),('pt','Traduz para'),
])
def test_translation_does_not_translate_its_own_incomplete_request(session,code,head):
    session.reset_case(code)
    session.agent.handle(head)
    pending=session.agent.conversations.get('desktop').pending_intent
    assert pending and pending.tool=='translate' and not session.calls
    assert pending.missing().name=='target_language'
    session.agent.handle('de')
    assert not session.calls
    session.agent.handle('Open the wrongg file')
    assert session.calls==[{'tool':'translate','arguments':{'target_language':'de','text':'Open the wrongg file'},'risk':'safe'}]


@pytest.mark.parametrize('code,head,action',[
 ('pt','Quando nasce o sol em','sunrise'),('pt','Quando se põe o sol em','sunset'),
 ('es','Cuándo anochece en','sunset'),('es','Qué tiempo hace en','summary'),
 ('fr','Va-t-il pleuvoir à','rain'),('it','Pioverà a','rain'),('pt','Vai chover em','rain'),
])
def test_dangling_city_preposition_gets_city_slot_not_search_or_period_garbage(session,code,head,action):
    session.reset_case(code)
    session.agent.handle(head)
    pending=session.agent.conversations.get('desktop').pending_intent
    assert pending and pending.tool=='weather' and pending.missing().name=='city',(head,pending,session.calls)
    assert not session.calls
    session.agent.handle('Krasnoyarsk')
    assert session.calls==[{'tool':'weather','arguments':{'action':action,'city':'Krasnoyarsk'},'risk':'safe'}]


@pytest.mark.parametrize('code,head',[
 ('ru','заверши пункт'),('en','tick item'),('fr','coche l’élément'),
 ('es','marca el punto'),('it','spunta la voce'),('pt','marque o item'),
])
@pytest.mark.parametrize('choice', [1,2])
def test_bare_checklist_item_asks_which_domain_and_never_closes_application(session,code,head,choice):
    session.reset_case(code)
    session.agent.handle(head)
    pending=session.agent.conversations.get('desktop').pending_intent
    assert pending and pending.tool=='command_choice160' and not session.calls,(head,pending,session.calls)
    session.agent.handle(str(choice))
    pending=session.agent.conversations.get('desktop').pending_intent
    assert pending and pending.tool==('shelf' if choice==1 else 'overlay') and not session.calls,(head,pending,session.calls)
    session.agent.handle('NEO QA Checklist' if choice==1 else 'Shopping')
    session.agent.handle('2')
    assert len(session.calls)==1,session.calls
    expected = {'action':'check','checked':True,'title':'NEO QA Checklist','index':1} if choice==1 else {'action':'checklist_check','name':'Shopping','item':'2'}
    if choice==1:
        from jarvis.ui.shelf_store import ShelfStore
        row=next(x for x in ShelfStore(session.config.paths.root).list() if x['title']=='NEO QA Checklist')
        expected={**expected,'id':row['id'],'version':row['version']}
    assert session.calls[0]['arguments']==expected


@pytest.mark.parametrize('code', ['ru','en','de','fr','es','it','pt'])
def test_explicit_shelf_item_validates_human_index_and_uses_zero_based_api(session,code):
    session.reset_case(code)
    phrase=EXPLICIT['shelf.check'][['ru','en','de','fr','es','it','pt'].index(code)]
    session.agent.handle(phrase)
    session.agent.handle('NEO QA Checklist')
    session.agent.handle('0')
    assert not session.calls
    pending=session.agent.conversations.get('desktop').pending_intent
    assert pending and pending.missing().name=='index'
    session.agent.handle('1')
    assert session.calls[-1]['arguments']['index']==0


@pytest.mark.parametrize('choice,action',[(1,'checklist_complete_all'),(2,'checklist_uncheck_all')])
def test_ambiguous_panel_whole_list_does_not_guess_checked_state(session,choice,action):
    session.reset_case('es')
    session.agent.handle('marca toda la lista del panel')
    pending=session.agent.conversations.get('desktop').pending_intent
    assert pending and pending.tool=='command_choice160' and not session.calls
    session.agent.handle(str(choice))
    session.agent.handle('Tasks')
    assert session.calls==[{'tool':'overlay','arguments':{'action':action,'name':'Tasks'},'risk':'safe'}]


def test_missing_file_view_disambiguates_opening_and_explorer_location(session):
    session.reset_case('es')
    session.agent.handle('mostrar el archivo')
    pending=session.agent.conversations.get('desktop').pending_intent
    assert pending and pending.tool=='command_choice160' and not session.calls
    session.agent.handle('2')
    session.agent.handle('C:\\Temp\\report.txt')
    assert session.calls==[{'tool':'filesystem','arguments':{'action':'reveal','source':'C:\\Temp\\report.txt'},'risk':'safe'}]


@pytest.mark.parametrize('code,phrase',[
 ('ru','переведи на немецкий: открой папку'),('en','Translate into de: open folder'),
 ('en','Analyze the screen: describe this button'),('pt','Quando nasce o sol em Lisboa'),
 ('en','show file C:\\Temp\\r.txt in Explorer'),('en','show the weather'),
 ('ru','погода'),('en','bring up'),('ru','не заверши пункт'),
 ('en','I said tick item'),('ru','"заверши пункт"'),
])
def test_partial_heads_do_not_capture_complete_arguments_or_free_prose(code,phrase):
    assert partial(phrase,code) is None


@pytest.mark.parametrize('code', ['ru','en','de','fr','es','it','pt'])
def test_existing_declared_defaults_remain_complete_not_new_partial_questions(session,code):
    import json
    from jarvis.runtime.natural_commands import strip_request_shell
    path=Path(__file__).resolve().parents[1]/'jarvis/locales/commands'/(code+'.json')
    rows=json.loads(path.read_text(encoding='utf-8'))['action_phrases']
    for intent in ('weather.summary','screen.analyze','mini_browser.open'):
        phrases=[text for text in rows.get(intent,[]) if '{' not in text]
        for text in phrases:
            assert partial(text,code) is None,(code,intent,text)
        if phrases:
            session.reset_case(code)
            session.agent.handle(phrases[0])
            assert session.agent.conversations.get('desktop').pending_intent is None,(intent,phrases[0])
            assert session.calls and session.calls[-1]['tool']==intent.split('.',1)[0],(intent,phrases[0],session.calls)


def test_new_peripheral_atlas_scenarios_use_real_agent_and_expected_arguments(session):
    import json
    from tools.atlas_all_test import check_phrase
    path=Path(__file__).resolve().parents[1]/'config/regression160_peripheral.json'
    for row in json.loads(path.read_text(encoding='utf-8')):
        if not row.get('keep_context'):
            session.reset_case(row['locale'])
        result=check_phrase(session,row)
        assert result['status'] in {'ARGUMENTS_PASS','ROUTE_PASS','CLARIFICATION_PASS'},result
