"""Actual Agent + owned registry boundary: missing slots and word-head repairs."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from jarvis.runtime.command_repair160 import candidates, repair, distance
from jarvis.runtime.dialogue_tasks import prepare_dialogue
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.atlas_contracts149 import prepare_fixtures
from jarvis.testing.isolation import external_io_blocked

GAME = [('ru','запусти игру'),('en','launch a game'),('de','starte ein Spiel'),('fr','lance un jeu'),('es','inicia un juego'),('it','avvia un gioco'),('pt','inicia um jogo')]
OPEN = [('ru','открйо приложение'),('en','opne application'),('de','öffne die Anwendung'),('fr','ouv er'),('es','ab re'),('it','apir applicazione'),('pt','ab ra')]

@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        s=AtlasSession(tmp_path_factory.mktemp('routing160')/'run');prepare_fixtures(s)
        yield s
        s.finish()

@pytest.mark.parametrize('code,phrase',GAME)
def test_game_missing_title_asks_then_launches_the_literal_game(session,code,phrase):
    session.reset_case(code)
    turn=session.agent.handle(phrase)
    assert turn.outcome=='clarification',turn
    assert not session.calls
    p=session.agent.conversations.get('desktop').pending_intent
    assert (p.tool,p.arguments,p.missing().name)==('application',{'action':'open','kind':'game'},'name')
    session.agent.handle('Darktide')
    assert session.calls==[{'tool':'application','arguments':{'action':'open','kind':'game','name':'Darktide'},'risk':'safe'}],session.calls

@pytest.mark.parametrize('phrase',['открой папку','Джарвис, открой папку','открйо папку','пожалуйста открйо папку','откр папку'])
def test_folder_head_then_downloads_never_routes_literal_word_folder(session,phrase):
    session.reset_case('ru')
    first=session.agent.handle(phrase)
    assert first.outcome=='clarification',first
    assert not session.calls
    second=session.agent.handle('Downloads')
    assert session.calls and session.calls[-1]['tool']=='desktop',(second,session.calls)
    assert session.calls[-1]['arguments']=={'action':'open','query':'Downloads','hint':'folder'}

@pytest.mark.parametrize('code,phrase',[
 ('ru','запсутти игру'),('en','launhc a game'),('de','staret ein Spiel'),
 ('fr','lanec un jeu'),('es','inicai un juego'),('it','aviva un gioco'),('pt','incia um jogo')])
def test_typo_launch_uses_native_game_question(session,code,phrase):
    session.reset_case(code);turn=session.agent.handle(phrase)
    assert turn.outcome=='clarification',(code,turn)
    p=session.agent.conversations.get('desktop').pending_intent
    assert p and p.arguments.get('kind')=='game',(code,turn,p)
    assert not session.calls

@pytest.mark.parametrize('kind,value',[
 ('game','"Открйо мир"'),('game','Otkryo Arena'),('app','"Open Road"')])
def test_pending_name_remains_literal(session,kind,value):
    session.reset_case('ru');session.agent.handle('запусти игру' if kind=='game' else 'запусти приложение')
    session.agent.handle(value)
    assert session.calls[-1]['arguments']['name']==value.strip('"'),session.calls

@pytest.mark.parametrize('replacement',['отмена','стоп','привет','покажи процессор'])
def test_new_turn_drops_game_question(session,replacement):
    session.reset_case('ru');session.agent.handle('запусти игру');session.agent.handle(replacement)
    assert session.agent.conversations.get('desktop').pending_intent is None
    assert not any(x['tool']=='application' for x in session.calls)

@pytest.mark.parametrize('phrase',[
 'не открйо папку','я сказал открйо папку','"открйо папку"',
 'переведи открйо папку','найди песню Открйо дверь','удлаи файл C:\\Games\\save.dat',
 'отправь сообщение открйо папку','закрйо папку','включи трек Stop',
 'https://example.org/открйо','C:\\открйо\\save.dat','открыто окно','ошибка при открытии папки',
])
def test_only_unknown_leading_safe_action_word_may_be_repaired(phrase):
    assert candidates(phrase,'ru')==()


def test_ambiguity_is_not_silently_resolved():
    # Portuguese two-word prefix: only the existing grammar can disambiguate
    # alternative imperatives. Fake proposals isolate the tie decision.
    text='most object'
    result,choices=repair(text,'pt',lambda candidate: None if candidate==text else ('domain',candidate.split()[0]))
    assert result==text and len(choices)>=2
    result,choices=repair(text,'pt',lambda candidate: None if candidate==text else ('domain','same action'))
    assert result!='most object' and not choices


def test_swapped_final_letters_count_as_one_error_and_prefix_is_bounded():
    assert distance('открйо','открой')==1
    assert candidates('откр папку','ru')==('открой папку',)
    assert not candidates('отк папку','ru')
    assert not candidates('open folder','ru')

@pytest.mark.parametrize('head',['где находятся файлы игры','покажи файлы приложения','найди папки игры'])
def test_incomplete_locations_do_not_search_for_the_word_game(session,head):
    session.reset_case('ru');first=session.agent.handle(head)
    assert first.outcome=='clarification',first
    assert not session.calls
    session.agent.handle('Darktide')
    assert session.calls[-1]['tool']=='game_locations',session.calls
    assert session.calls[-1]['arguments']=={'action':'find','name':'Darktide','kind':'all'}

@pytest.mark.parametrize('phrase,code', [('запустил игру','ru'),('включил музыку','ru'),('прочитал письмо','ru'),('поставил музыку','ru'),('пока музыка','ru'),('plays music','en'),('opened folder','en'),('zeigte Datei','de'),('lancé un jeu','fr'),('abrió carpeta','es'),('aperto file','it'),('abriu pasta','pt'),('поищу файл','ru')])
def test_valid_statements_are_not_spelling_repairs(phrase,code):
    assert candidates(phrase,code)==()

@pytest.mark.parametrize('phrase',['запустил игру','включил музыку','поставил музыку','пока музыка','запустим игру','включит музыку','прочитает файл','поищу файл'])
def test_actual_agent_does_not_execute_completed_action_statement(session,phrase):
    session.reset_case('ru');session.agent.handle(phrase)
    assert not session.calls,session.calls


def test_new_atlas_scenarios_use_actual_agent_registry_and_typed_slots(session):
    from tools.atlas_all_test import check_phrase
    rows=json.loads((Path(__file__).resolve().parents[1]/'config/regression160_routing.json').read_text(encoding='utf-8'))
    for row in rows:
        if not row.get('keep_context'):
            session.reset_case(row['locale'])
        result=check_phrase(session,row)
        assert result['status'] in {'ARGUMENTS_PASS','ROUTE_PASS','CLARIFICATION_PASS'},result

@pytest.mark.parametrize('kind',['name','choice','duration','expired','foreign','confirmation'])
def test_folder_replaces_old_nontext_question_and_never_dispatches_generic_folder(session,kind):
    from jarvis.runtime.dialogue_tasks import PendingIntent,Slot
    import time
    session.reset_case('ru');state=session.agent.conversations.get('desktop')
    state.pending_intent=PendingIntent('application',{'action':'open','kind':'game'},
        (Slot('name',kind if kind in {'name','choice','duration'} else 'name','application',choices=(('One','One'),('Two','Two')) if kind=='choice' else ()),),
        'en' if kind=='foreign' else 'ru',time.monotonic()+(-1 if kind=='expired' else 60))
    if kind=='confirmation': session.agent._pending_context={'tool':'filesystem','arguments':{'action':'delete'},'created_at':time.monotonic()}
    first=session.agent.handle('открой папку')
    assert first.outcome=='clarification',first
    assert state.pending_intent.tool=='desktop' and state.pending_intent.arguments['hint']=='folder'
    assert not session.calls

@pytest.mark.parametrize('code,head',[('ru','добавь трек'),('en','queue the song'),('de','reihe das Lied'),('fr','ajoute le morceau'),('es','añade la pista'),('it','aggiungi il brano'),('pt','adiciona a faixa')])
def test_queue_position_is_asked_before_any_mutation(session,code,head):
    for option,action in [('1','queue_add'),('2','queue_next')]:
        session.reset_case(code);turn=session.agent.handle(head)
        p=session.agent.conversations.get('desktop').pending_intent
        assert p and p.tool=='music' and p.missing().name=='action',(head,turn,p)
        assert {v for _,v in p.missing().choices}=={'queue_add','queue_next'}
        assert not session.calls
        session.agent.handle(option);assert not session.calls
        session.agent.handle('NEO Song')
        assert session.calls[-1]['tool']=='music' and session.calls[-1]['arguments']=={'action':action,'query':'NEO Song'},session.calls

def test_navigation_intent_choice_does_not_guess_youtube(session):
    for choice,answer,tool,args in [('1','45','media',{'action':'seek_to','target':'youtube','position_seconds':45.}),('2','NEO Tab','browser',{'action':'activate_tab_title','value':'NEO Tab'}),('3','Notepad','window',{'action':'focus','title':'Notepad'})]:
        session.reset_case('ru');session.agent.handle('перейди к')
        p=session.agent.conversations.get('desktop').pending_intent
        assert p.tool=='command_choice160' and len(p.missing().choices)==3
        assert not session.calls
        session.agent.handle(choice);assert not session.calls
        session.agent.handle(answer)
        assert session.calls[-1]['tool']==tool and session.calls[-1]['arguments']==args,session.calls

@pytest.mark.parametrize('code,phrase',[('ru','открой браузер'),('en','open the browser'),('de','öffne den Browser'),('fr','ouvre le navigateur'),('es','abre el navegador'),('it','apri il browser'),('pt','abre o navegador')])
@pytest.mark.parametrize('preferred',[None,'chrome'])
def test_bare_browser_uses_configured_client_without_article_or_malformed_url(session,code,phrase,preferred):
    session.reset_case(code)
    desktop=session.agent.config.data.setdefault('desktop',{})
    if preferred is None:desktop.pop('preferred_browser',None)
    else:desktop['preferred_browser']=preferred
    session.agent.handle(phrase)
    assert session.calls==[{'tool':'application','arguments':{'action':'open','name':preferred or 'firefox','kind':'app'},'risk':'safe'}],session.calls

@pytest.mark.parametrize('phrase',['включив музыку','прочитав письмо','прочитали письмо','запустили игру','поставив музыку','продолжив музыку','переключив музыку','развернули приложение'])
def test_productive_past_and_converb_forms_never_trigger_new_repair(session,phrase):
    assert candidates(phrase,'ru')==()
    session.reset_case('ru');session.agent.handle(phrase)
    assert not session.calls,session.calls
    assert session.agent.conversations.get('desktop').pending_intent is None


def test_italian_missing_seek_target_asks_music_or_video(session):
    for choice,tool,key in [('1','music','seconds'),('2','media','position_seconds')]:
        session.reset_case('it');session.agent.handle('vai al secondo')
        pending=session.agent.conversations.get('desktop').pending_intent
        assert pending.tool=='command_choice160'
        assert not session.calls
        session.agent.handle(choice);assert not session.calls
        session.agent.handle('45')
        assert session.calls[-1]['tool']==tool and session.calls[-1]['arguments'][key]==45

@pytest.mark.parametrize('phrase',['Покажешь процессор?','покажешь процессор'])
def test_polite_second_person_question_preserves_existing_cpu_action(session,phrase):
    from jarvis.runtime.command_repair160 import descriptive_statement
    assert not descriptive_statement(phrase,'ru')
    session.reset_case('ru');session.agent.handle(phrase)
    assert session.calls==[{'tool':'system_monitor','arguments':{'action':'cpu'},'risk':'safe'}]

@pytest.mark.parametrize('phrase',['Включишь музыку?','откроешь папку'])
def test_second_person_is_not_mislabelled_as_a_report(phrase):
    from jarvis.runtime.command_repair160 import descriptive_statement
    assert not descriptive_statement(phrase,'ru')

@pytest.mark.parametrize('wrapper',['{phrase}','por favor {phrase}','podes {phrase}','poderia {phrase}'])
def test_valid_portuguese_reproduz_keeps_transport_and_discord_url(session,wrapper):
    cases=[('reproduz a faixa anterior','music',{'action':'previous'}),('reproduz no Discord a música no endereço do YouTube https://www.youtube.com/watch?v=NEO_QA12345a','discord',{'action':'music_youtube_url','query':'https://www.youtube.com/watch?v=NEO_QA12345a'})]
    for phrase,tool,args in cases:
        session.reset_case('pt');session.agent.handle(wrapper.format(phrase=phrase))
        assert len(session.calls)==1 and session.calls[0]['tool']==tool,session.calls
        assert session.calls[0]['arguments']==args,session.calls

def test_an_already_owned_command_always_outranks_new_repair():
    assert repair('most object','pt',lambda _:('existing',{'action':'inspect'}))==('most object',())
