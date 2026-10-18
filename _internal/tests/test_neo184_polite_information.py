"""Ordinary courtesy and object-first requests preserve their specific intent."""
import copy
import pytest
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution
CASES=[
 ('покажите информацию о приложении QA_Example_731','application',{'action':'info','name':'QA_Example_731'}),
 ('откройте первый результат поиска QA_Query_731','browser',{'action':'search_first','value':'QA_Query_731'}),
 ('покажите сведения о сети','network',{'action':'info'}),
 ('покажите каналы сервера QA_Server_731 в Discord','discord',{'action':'list_channels','server':'QA_Server_731'}),
 ('покажите мои таймеры и напоминания','timer',{'action':'list'}),
 ('покажите недавние сообщения в Telegram','telegram_messages',{'action':'recent'}),
 ('покажите сообщения от qa731@example.invalid в Telegram','telegram_messages',{'action':'from_sender','sender':'qa731@example.invalid'}),
 ('на окно QA_Title_731 переключись','window',{'action':'focus','title':'QA_Title_731'}),
]
@pytest.fixture(scope='module')
def session(tmp_path_factory):
 with external_io_blocked():
  s=AtlasSession(tmp_path_factory.mktemp('polite_info184')/'session')
  for d in s.agent.tools._tools.values():d.handler=lambda a:ToolExecution(True,'QA inert',{})
  s.agent.tools.set_execution_guard(lambda n,a,r:s.calls.append((n,copy.deepcopy(a))) or None)
  yield s;s.finish()
@pytest.mark.parametrize('phrase,tool,args',CASES)
def test_polite_information_preserves_action_and_filter(session,phrase,tool,args):
 session.reset_case('ru');turn=session.agent.handle(phrase)
 calls=list(session.calls)
 if turn.pending:calls.append((turn.pending.tool_name,turn.pending.arguments))
 assert calls==[(tool,args)]
@pytest.mark.parametrize('phrase',['не откройте первый результат поиска QA','не покажите сведения о сети','не переключись на окно QA','на окно QA не переключись','Что значит команда «покажите сведения о сети»?'])
def test_negative_and_quoted_requests_do_not_act(session,phrase):
 session.reset_case('ru');turn=session.agent.handle(phrase)
 assert not session.calls and not turn.pending

def test_only_head_changes_not_name(session):
 from jarvis.runtime.canonical_commands import parse_canonical_command
 p=parse_canonical_command('покажите информацию о приложении Покажите CASE','ru',lambda t:session.agent.tools.get(t).parameters)
 assert p.arguments['name']=='Покажите CASE'

@pytest.mark.parametrize('phrase',['окно QA не закрой','на окно QA не переключись','папку QA не открой'])
def test_object_first_negation_blocks_action(session,phrase):
 session.reset_case('ru');turn=session.agent.handle(phrase)
 assert not session.calls and not turn.pending

def test_object_first_refusal_then_valid_action(session):
 session.reset_case('ru');session.agent.handle('на окно QA не переключись, открой приложение Calculator')
 assert session.calls==[('application',{'action':'open','name':'Calculator'})]

def test_negative_words_inside_explicit_dictation_remain_data(session):
 session.reset_case('ru');session.agent.handle('напиши: на окно QA не переключись')
 assert session.calls==[('text_edit',{'action':'dictate','text':'на окно QA не переключись'})]
