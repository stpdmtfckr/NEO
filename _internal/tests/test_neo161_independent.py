"""Hand-authored acceptance cases independent of the repair algorithm/corpus.

Expectations describe user intent and preserved opaque arguments. These tests run
actual Agent routing with its registry execution guard, without network/devices.
"""
from pathlib import Path
import pytest
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.atlas_contracts149 import prepare_fixtures
from jarvis.testing.isolation import external_io_blocked

@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        s=AtlasSession(tmp_path_factory.mktemp('independent161')/'run')
        prepare_fixtures(s)
        yield s
        s.finish()


def route(session,code,phrase):
    session.reset_case(code)
    result=session.agent.handle(phrase)
    pending=session.agent.conversations.get('desktop').pending_intent
    return result,list(session.calls),pending


@pytest.mark.parametrize('phrase',[
    'включимая волна', 'включимояволна', 'включи моюволну',
    'включи мая волна', 'включи мою влону', 'включи мою волуну',
])
def test_user_reported_wave_and_nearby_errors_reach_music(session,phrase):
    _,calls,pending=route(session,'ru',phrase)
    assert len(calls)==1 and calls[0]['tool']=='music', (phrase,calls,pending)
    assert calls[0]['arguments']=={'action':'my_wave'},(phrase,calls,pending)


@pytest.mark.parametrize('phrase,tool,args,slot',[
 ('откройпапку','desktop',{'action':'open','hint':'folder'},'query'),
 ('открой пакпу','desktop',{'action':'open','hint':'folder'},'query'),
 ('запустиигру','application',{'action':'open','kind':'game'},'name'),
 ('запусти иргу','application',{'action':'open','kind':'game'},'name'),
 ('запусти игрру','application',{'action':'open','kind':'game'},'name'),
 ('запусти приложеине','application',{'action':'open'},'name'),
])
def test_typo_object_is_a_missing_object_question_not_an_app_name(session,phrase,tool,args,slot):
    _,calls,pending=route(session,'ru',phrase)
    assert not calls,(phrase,calls)
    assert pending and (pending.tool,pending.arguments,pending.missing().name)==(tool,args,slot),(phrase,pending)
    session.agent.handle('Open Road')
    assert len(session.calls)==1 and session.calls[0]['tool']==tool
    assert session.calls[0]['arguments'][slot]=='Open Road',session.calls


@pytest.mark.parametrize('phrase,action',[
 ('покажипроцессор','cpu'),('покажи процсесор','cpu'),
 ('покажи процссор','cpu'),
])
def test_literal_system_object_typos_never_launch_an_application(session,phrase,action):
    _,calls,pending=route(session,'ru',phrase)
    assert len(calls)==1 and calls[0]['tool']=='system_monitor',(phrase,calls,pending)
    assert calls[0]['arguments']['action']==action,(phrase,calls,pending)


@pytest.mark.parametrize('code,phrase,action',[
 ('ru','очередьмузыки','queue_status'),
 ('ru','какие тркеи дальше','queue_status'),
 ('ru','следующийтрек','next'),
 ('en','showthe music queue','queue_status'),
 ('en','show the music qeueu','queue_status'),
 ('en','playthe next track','next'),
 ('de','zeige die Musikwarteschalnge','queue_status'),
 ('de','spieleden nächsten Titel','next'),
 ('fr','passeau morceau suivant','next'),
 ('fr','affiche la file d’attente musciale','queue_status'),
 ('es','muestra la cola de múscia','queue_status'),
 ('es','ponla siguiente canción','next'),
 ('it','mostra la coda musciale','queue_status'),
 ('it','riproduciil brano successivo','next'),
 ('pt','mostra a fila de múscias','queue_status'),
 ('pt','toca a próximamúsica','next'),
])
def test_fused_and_misspelled_fixed_literals_in_seven_languages(session,code,phrase,action):
    _,calls,pending=route(session,code,phrase)
    assert len(calls)==1 and calls[0]['tool']=='music',(code,phrase,calls,pending)
    assert calls[0]['arguments']=={'action':action},(code,phrase,calls,pending)


@pytest.mark.parametrize('code,canonical,broken',[
 ('ru','открой приложение Open Road','откройприложение Open Road'),
 ('en','open application Open Road','openapplication Open Road'),
 ('de','öffne das Programm Open Road','öffnedas Programm Open Road'),
 ('fr',"ouvre l'application Open Road","ouvrel'application Open Road"),
 ('es','abre la aplicación Open Road','abrela aplicación Open Road'),
 ('it',"apri l'applicazione Open Road","april'applicazione Open Road"),
 ('pt','abre a aplicação Open Road','abrea aplicação Open Road'),
 ('ru','включи трек Включимая волна','включитрек Включимая волна'),
 ('en','play track Open My Wave','playtrack Open My Wave'),
 ('ru','открой сайт https://example.invalid/OpenRoad?q=включимаяволна','откройсайт https://example.invalid/OpenRoad?q=включимаяволна'),
 ('en','open website https://example.invalid/OpenRoad?opne=MyWave','openwebsite https://example.invalid/OpenRoad?opne=MyWave'),
])
def test_command_boundary_repair_preserves_payload_exactly_like_canonical_route(session,code,canonical,broken):
    _,expected,pending=route(session,code,canonical)
    assert len(expected)==1 and pending is None,(canonical,expected,pending)
    _,actual,pending=route(session,code,broken)
    assert actual==expected,(broken,expected,actual,pending)


@pytest.mark.parametrize('code,phrase',[
 ('ru','не включимояволна'),('ru','я включимая волна'),
 ('ru','я сказал включимая волна'),('ru','"включимая волна"'),
 ('ru','включил музыку'),('ru','включив музыку'),
 ('ru','включивмузыку'),('ru','открылпапку'),('ru','запустилиигру'),
 ('en','opened folder'),('en','openedfolder'),('en','playedmusic'),
 ('de','zeigte Datei'),('fr','lancé un jeu'),('es','abrió carpeta'),
 ('it','aperto file'),('pt','abriu pasta'),
])
def test_narrative_negation_and_quotes_are_not_new_actions(session,code,phrase):
    _,calls,pending=route(session,code,phrase)
    assert not calls,(code,phrase,calls)
    assert pending is None,(code,phrase,pending)


@pytest.mark.parametrize('phrase',[
 'удлаи файл C:\\QA\\save.txt', 'удалифайл C:\\QA\\save.txt',
 'отправб сообщение Alice: включимая волна',
 'отправьсообщение Alice: включимая волна',
 'перезагурзи компьютер','выключикомпьютер',
])
def test_risky_corrupt_commands_cannot_cross_registry_without_confirmation(session,phrase):
    result,calls,pending=route(session,'ru',phrase)
    read_actions={'music_queue_status','background_status','status'}
    assert not [c for c in calls if c['arguments'].get('action') not in read_actions],(phrase,result,calls,pending)


@pytest.mark.parametrize('phrase,tool,key,payload',[
 ('открой приложение Включимая Волна','application','name','Включимая Волна'),
 ('включи трек Включимая волна','music','query','Включимая волна'),
 ('открой приложение Open Road','application','name','Open Road'),
 ('включи трек очередьмузыки','music','query','очередьмузыки'),
])
def test_existing_explicit_payload_is_not_repaired_as_atlas_words(session,phrase,tool,key,payload):
    _,calls,pending=route(session,'ru',phrase)
    assert len(calls)==1 and calls[0]['tool']==tool,(phrase,calls,pending)
    assert calls[0]['arguments'][key]==payload,(phrase,calls,pending)


@pytest.mark.parametrize('phrase,action',[
 ('очередь музыка','queue_status'),('какие треки дальше','queue_status'),
 ('дальше','next'),('назад','previous'),('стоп','stop'),('продолжить','play'),
])
def test_existing_short_music_commands_preserve_observed_music_context(session,phrase,action):
    session.reset_case('ru')
    import time
    state=session.agent.conversations.get('desktop')
    state.media_owner157={'tool':'music','language':'ru','at':time.monotonic(),
                          'arguments':{'action':'my_wave'}}
    session.agent.handle(phrase)
    assert len(session.calls)==1 and session.calls[0]['tool']=='music',session.calls
    assert session.calls[0]['arguments']=={'action':action},session.calls

@pytest.mark.parametrize('phrase',['!!!','...','?','—','👍','{}','🤔 ?'])
def test_punctuation_only_input_cannot_crash_repair_or_execute(session,phrase):
    result,calls,pending=route(session,'ru',phrase)
    assert result is not None
    assert not calls and pending is None,(phrase,result,calls,pending)
