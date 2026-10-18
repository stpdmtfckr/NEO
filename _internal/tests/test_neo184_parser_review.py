"""Reviewed command contracts, inert end-to-end routing and independent oracles."""
import copy
import pytest
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution
from jarvis.tools.apps_tool173 import parse as app_parse
from jarvis.runtime.winsettings178 import _when

@pytest.fixture(scope='module')
def session(tmp_path_factory):
 with external_io_blocked():
  s=AtlasSession(tmp_path_factory.mktemp('review184')/'session')
  for d in s.agent.tools._tools.values():d.handler=lambda a:ToolExecution(True,'QA inert',{})
  s.agent.tools.set_execution_guard(lambda n,a,r:s.calls.append((n,copy.deepcopy(a))) or None)
  yield s;s.finish()

@pytest.mark.parametrize('code,text,tool,args',[
 ('en','clear the clipboard','clipboard',{'action':'clear'}),
 ('en','Please, clear the clipboard','clipboard',{'action':'clear'}),
 ('en','clear clipboard, please','clipboard',{'action':'clear'}),
 ('de','bitte leere Zwischenablage','clipboard',{'action':'clear'}),
 ('en','clear clipboard history','winsettings',{'action':'clear_clipboard_history'}),
 ('de','leere Zwischenablageverlauf','winsettings',{'action':'clear_clipboard_history'}),
 ('ru','Покажи сообщения от qa731@example.invalid в Telegram','telegram_messages',{'action':'from_sender','sender':'qa731@example.invalid'}),
 ('en','Show Telegram messages from @NeoAudit731','telegram_messages',{'action':'from_sender','sender':'@NeoAudit731'}),
 ('fr','Affiche les messages Telegram de qa731@example.invalid','telegram_messages',{'action':'from_sender','sender':'qa731@example.invalid'}),
 ('es','Muéstrame los mensajes de qa731@example.invalid en Telegram','telegram_messages',{'action':'from_sender','sender':'qa731@example.invalid'}),
 ('pt','Mostra as mensagens de qa731@example.invalid no Telegram','telegram_messages',{'action':'from_sender','sender':'qa731@example.invalid'}),
 ('ru','включи мою волну в дискорде','discord',{'action':'music_my_wave'}),
 ('ru','включи песню "мою волну" в дискорде','discord',{'action':'music_bot_play','query':'мою волну'}),
])
def test_exact_reviewed_routes(session,code,text,tool,args):
 session.reset_case(code);turn=session.agent.handle(text)
 if turn.pending:
  assert not session.calls
  assert (turn.pending.tool_name,turn.pending.arguments)==(tool,args)
 else:assert session.calls==[(tool,args)]

@pytest.mark.parametrize('text', ['Mais uma coisa: Por favor, coloca o computador em suspensão','Mais uma coisa: pôr o computador em suspensão','Mais uma coisa: colocar o computador em suspensão','Mais uma coisa: coloca o computador em suspensão','Mais uma coisa: põe o computador em suspensão'])
def test_pt_sleep_never_changes_timeout(session,text):
 session.reset_case('pt');turn=session.agent.handle(text)
 assert not session.calls
 assert turn.pending and turn.pending.tool_name=='system' and turn.pending.arguments=={'action':'sleep'}

@pytest.mark.parametrize('text',['crea una carpeta nueva','crea una carpeta nueva, por favor'])
def test_spanish_missing_folder_name(session,text):
 session.reset_case('es');session.agent.handle(text)
 pending=session.agent.conversations.get('desktop').pending_intent
 assert not session.calls and pending.tool=='filesystem' and pending.arguments=={'action':'create_folder'}
 assert 'name' in [s.name for s in pending.slots]

def test_complete_telegram_send_retains_target_and_body(session):
 session.reset_case('ru');turn=session.agent.handle('Напиши в Telegram для QA_Target_731: QA Text 731')
 assert not session.calls
 assert turn.pending.tool_name=='telegram_messages'
 assert turn.pending.arguments=={'action':'send','target':'QA_Target_731','text':'QA Text 731'}

def test_incomplete_telegram_send_keeps_dialogue(session):
 session.reset_case('ru');session.agent.handle('напиши в telegram для')
 p=session.agent.conversations.get('desktop').pending_intent
 assert not session.calls and p.tool=='telegram_messages' and p.missing().name=='target'

@pytest.mark.parametrize('text,expected',[('mai',0),('never sleep',0),('niemals',0),('Mais uma coisa',None),('mail',None),('sleep after 15 minutes',15)])
def test_time_words_have_boundaries(text,expected):assert _when(text)==expected

@pytest.mark.parametrize('text,name', [('crea una carpeta "nueva"','nueva'),('crea una carpeta llamada nueva','nueva'),('crea una carpeta Archivo, por favor','Archivo'),('crea una carpeta "Archivo, por favor"','Archivo, por favor')])
def test_folder_literal_names_preserved(text,name):
 assert app_parse(text,'es')==('apps',{'action':'create_folder','name':name,'folder':'context'})

@pytest.mark.parametrize('text,expected', [('Open Telegram @NeoAudit731',('apps',{'action':'telegram','page':'chat','username':'NeoAudit731'})),('Open Telegram qa731@example.invalid',None),('Open Telegram @example.invalid',None)])
def test_only_standalone_telegram_usernames(text,expected):assert app_parse(text,'en')==expected

@pytest.mark.parametrize('preferred',['firefox','chrome','edge','opera','yandex'])
@pytest.mark.parametrize('code,text',[('ru','открой браузер'),('ru','пожалуйста, открой браузер'),('en','open browser'),('de','öffne den Browser'),('fr','ouvre le navigateur'),('es','abre el navegador'),('pt','abre o navegador')])
def test_generic_browser_launch_uses_configured_choice(session,preferred,code,text):
 session.reset_case(code);session.agent.config.data.setdefault('desktop',{})['preferred_browser']=preferred
 session.agent.handle(text)
 assert session.calls==[('application',{'action':'open','name':preferred,'kind':'app'})]

@pytest.mark.parametrize('text,name',[('открой firefox','firefox'),('open chrome','chrome'),('open notepad','notepad')])
def test_named_application_preserved(session,text,name):
 session.reset_case('ru' if text.startswith('открой') else 'en');session.agent.config.data.setdefault('desktop',{})['preferred_browser']='edge'
 session.agent.handle(text)
 assert session.calls==[('application',{'action':'open','name':name,'kind':'app'})]
