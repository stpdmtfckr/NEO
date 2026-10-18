"""Native entry points, typed outcomes and scoped durable memory with inert tools."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json
import sys
import threading
import time
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jarvis.agent import Agent
from jarvis.runtime.conversation import ConversationStore
from jarvis.runtime.conversation_journal import ConversationJournal,local_period
from jarvis.runtime.feature_commands import parse_feature_command
from jarvis.runtime.canonical_commands import parse_canonical_command
from jarvis.runtime.grounded_dialogue import grounded_reply
from jarvis.security import Risk
from jarvis.tools.registry import ToolDefinition,ToolRegistry,ToolExecution
from tests.test_neo_canonical_commands import SCHEMAS

class Config:
    def __init__(self,root,code='ru'):
        self.paths=SimpleNamespace(root=root)
        self.values={'app.language':code,'activity.enabled':True,'dialogue.memory.enabled':True}
    def get(self,key,default=None):return self.values.get(key,default)

def agent_fixture(tmp_path,code='ru',*,success=True):
    config=Config(tmp_path,code)
    registry=object.__new__(ToolRegistry)
    registry.config=config;registry.database=Mock();registry.context_memory=Mock()
    registry.execution_guard=None;registry.execution_transport='local'
    registry.conversation_journal=ConversationJournal(config)
    calls=[]
    def handler(name):
        def run(args):
            calls.append((name,dict(args)))
            return ToolExecution(success,'Actual completed result' if success else 'Object missing',{'name':args.get('name','')})
        return run
    registry._tools={name:ToolDefinition(name,'inert',schema,handler(name),lambda a:Risk.SAFE) for name,schema in SCHEMAS.items()}
    agent=object.__new__(Agent);agent.config=config;agent._lock=threading.RLock()
    agent.tools=registry;agent.context_memory=registry.context_memory;agent.conversation_journal=registry.conversation_journal
    agent.conversations=ConversationStore();agent._conversation_key='desktop';agent.history=[]
    agent._needs_confirmation=lambda *a:False;agent._remember_execution_context=Mock()
    agent._last_file_path='';agent._last_folder_path='';agent._last_game_name='';agent._pending_context=None
    return agent,calls

@pytest.mark.parametrize('code,open_text,close_text',[
 ('ru','открыть книжный шкаф','закрыть книжную полку'),('en','open the bookcase','close the bookshelf'),
 ('de','das Bücherregal öffnen','das Bücherregal schließen'),('fr','ouvrir la bibliothèque','fermer la bibliothèque'),
 ('es','abrir la estantería','cerrar la estantería'),('it','aprire la libreria','chiudere la libreria'),
 ('pt','abrir a estante de livros','fechar a estante de livros')])
def test_bookcase_alias_enters_real_agent_and_registry_not_process(tmp_path,code,open_text,close_text):
    agent,calls=agent_fixture(tmp_path,code)
    for text,action in [(open_text,'show'),(close_text,'hide')]:
        turn=agent.handle(text)
        assert turn.success is True
        assert calls[-1]==('shelf',{'action':action})

@pytest.mark.parametrize('code,phrase', [('ru','открой папку Книжный шкаф'),('en','open application Bookshelf'),('de','öffne die Datei Bücherregal.txt'),('fr','ouvre le fichier bibliothèque.txt'),('es','abre la aplicación Librería'),('it','apri il file libreria.txt'),('pt','abre a aplicação Estante')])
def test_bookcase_named_entity_does_not_steal_shelf(code,phrase):
    assert parse_feature_command(phrase,code) is None

def test_failed_result_has_no_success_ack_or_forced_boss(tmp_path):
    agent,calls=agent_fixture(tmp_path,success=False)
    agent.config.values.update({'user_name_enabled':True,'user_name':'Босс'})
    chunks=[];turn=agent.handle_stream('закрой книжную полку',on_text=chunks.append)
    assert turn.success is False and calls
    # NEO 183: the box means «in every answer», «Босс» included — but a failure is never acknowledged
    assert 'принял' not in turn.text.casefold() and 'так точно' not in turn.text.casefold()
    assert ''.join(chunks)==turn.text

@pytest.mark.parametrize('code,phrase', [('ru','как дела, какая погода сегодня'),('en','how are you, how is the weather today'),('de','wie geht es dir, wie ist das wetter heute'),('fr',"comment ça va, quel temps fait-il aujourd'hui"),('es','cómo estás, qué tiempo hace hoy'),('it','come stai, che tempo fa oggi'),('pt','como estás, como está o tempo hoje')])
def test_weather_chat_calls_actual_weather_handler(tmp_path,code,phrase):
    agent,calls=agent_fixture(tmp_path,code)
    agent.handle(phrase)
    assert calls==[('weather',{'action':'summary'})]

def test_journal_survives_restart_but_not_across_scopes(tmp_path):
    config=Config(tmp_path);journal=ConversationJournal(config)
    journal.record_turn('desktop','open Steam','Opened Steam')
    journal.record_turn('telegram:7','hello','Hi')
    journal.record_execution('desktop','application',{'action':'open','name':'Steam'},ToolExecution(True,'Opened',{'name':'Steam'}))
    restored=ConversationJournal(config)
    assert restored.recent('desktop')[0]['text']=='open Steam'
    assert restored.recent('telegram:7')[0]['text']=='hello'
    assert restored.objects('telegram:7')==[]
    assert restored.objects('desktop')[0]['data']['name']=='Steam'

def test_privacy_is_enforced_before_persist_and_dynamically_on_read(tmp_path):
    config=Config(tmp_path);config.values.update({'activity.excluded_paths':[r'C:\Private'],'activity.excluded_apps':['SecretApp']})
    journal=ConversationJournal(config)
    for private in ['password=abc123','мой пароль abc123','Bearer abc123',r'open C:\Private\work.txt','open SecretApp']:
        journal.record_turn('desktop',private,'ok')
    journal.record_execution('desktop','filesystem',{'action':'open','source':r'C:\Private\work.txt'},ToolExecution(True,'ok'))
    assert not journal.path.exists()
    journal.record_turn('desktop','open PublicApp','ok')
    config.values['activity.excluded_apps'].append('PublicApp')
    assert not any('PublicApp' in row['text'] for row in journal.recent('desktop'))
    config.values['activity.paused']=True
    journal.record_turn('desktop','unrecorded turn','ok')
    assert journal.recent('desktop')==[]
    config.values['activity.paused']=False
    assert not any('unrecorded' in row['text'] for row in journal.recent('desktop'))

def test_queued_failed_and_unconfirmed_operations_are_not_opened_objects(tmp_path):
    journal=ConversationJournal(Config(tmp_path))
    for result in [ToolExecution(False,'failed'),ToolExecution(True,'requested',{'queued':True}),ToolExecution(True,'pending',{'requires_confirmation':True})]:
        journal.record_execution('desktop','application',{'action':'open','name':'Steam'},result)
    assert journal.objects('desktop')==[]

def test_yesterday_recall_uses_recorded_requests_not_fabricated_completion(tmp_path):
    agent,_=agent_fixture(tmp_path);start,end=local_period(agent.config,'yesterday')
    agent.conversation_journal.record_turn('desktop','open MissingApp','Could not open',now=start+120)
    reply,outcome=grounded_reply(agent,'что мы делали вчера','ru')
    assert 'Запрос в диалоге: open MissingApp' in reply and 'Открыто' not in reply
    agent._conversation_key='telegram:9'
    reply,_=grounded_reply(agent,'что мы делали вчера','ru')
    assert 'MissingApp' not in reply and 'нет подходящих записей' in reply

def test_clear_memory_does_not_reinsert_the_clear_request(tmp_path):
    agent,_=agent_fixture(tmp_path)
    agent.conversation_journal.record_turn('desktop','hello','hi')
    turn=agent.handle('очисти память диалога')
    assert turn.outcome=='memory_cleared'
    assert agent.conversation_journal.recent('desktop')==[]

def test_retention_and_bound(tmp_path):
    config=Config(tmp_path);config.values['dialogue.memory.max_turns']=10
    journal=ConversationJournal(config)
    journal.record_turn('desktop','too old','answer',now=time.time()-8*86400)
    for i in range(12):journal.record_turn('desktop','request '+str(i),'answer')
    rows=journal.recent('desktop',limit=200)
    assert len(rows)==10 and all('too old' not in row['text'] for row in rows)

@pytest.mark.parametrize('code,phrase,word',[('ru','установи громкость на {} процентов','четырнадцать'),('en','set the volume to {} percent','fourteen'),('de','stelle die Lautstärke auf {} Prozent','vierzehn'),('fr','régle le volume à {} pour cent','quatorze'),('es','ajusta el volumen al {} por ciento','catorce'),('it','imposta il volume al {} per cento','quattordici'),('pt','define o volume para {} por cento','catorze')])
def test_spoken_numbers_are_bound_only_in_numeric_slots(code,phrase,word):
    parsed=parse_canonical_command(phrase.format(word),code,SCHEMAS.__getitem__)
    assert parsed.arguments['value']==14
    assert parse_canonical_command('open file fourteen.txt','en',SCHEMAS.__getitem__).arguments['source']=='fourteen.txt'

def test_curly_apostrophes_preserve_payload_and_do_not_crash():
    parsed=parse_canonical_command('ouvre l’application Don’t Stop','fr',SCHEMAS.__getitem__)
    assert parsed and parsed.arguments['name']=='Don’t Stop'

@pytest.mark.parametrize('code,phrase',[('ru','не активируй Bluetooth'),('en','never enable Bluetooth'),('de','nicht Bluetooth aktivieren'),('fr','ne mets en marche le Bluetooth'),('es','no habilita Bluetooth'),('it','non abilitare il Bluetooth'),('pt','não habilita o Bluetooth')])
def test_expanded_negative_lexemes_cannot_execute(tmp_path,code,phrase):
    agent,calls=agent_fixture(tmp_path,code)
    turn=agent.handle(phrase)
    assert not calls and turn.tool_details==['routing.negated']

def test_activity_scope_is_derived_from_transport_not_desktop_defaults(tmp_path):
    agent,calls=agent_fixture(tmp_path,'en')
    agent.handle('show activity history',input_source='telegram',conversation_id='42')
    assert calls==[('activity',{'action':'list','scope':'telegram:42'})]

@pytest.mark.parametrize('code,phrase',[('ru','поставь таймер на пять минут'),('en','set a timer for five minutes'),('de','stelle einen Timer auf fünf Minuten'),('fr','lance un minuteur de cinq minutes'),('es','pon un temporizador de cinco minutos'),('it','imposta un timer di cinque minuti'),('pt','define um temporizador de cinco minutos')])
def test_natural_minutes_reach_real_timer_contract(tmp_path,code,phrase):
    agent,calls=agent_fixture(tmp_path,code)
    agent.handle(phrase)
    assert calls==[('timer',{'action':'timer','seconds':300})]
