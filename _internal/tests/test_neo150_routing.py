"""Field-log regressions, independent expected reactions and literal payloads."""
from dataclasses import asdict
from pathlib import Path
import json
import pytest
from tools.atlas_all_test import Phrase,check_phrase
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.atlas_contracts149 import prepare_fixtures
from jarvis.testing.isolation import external_io_blocked
from jarvis.runtime.natural_commands import LANGUAGES

@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        s=AtlasSession(tmp_path_factory.mktemp('neo150')/'run');prepare_fixtures(s)
        yield s
        s.finish()

CASES=[
('ru','очередь дискорд','discord','music_queue_status','',{}),
('ru','очеред дискорд','discord','music_queue_status','',{}),
('ru','история песен дискорд','discord','music_queue_status','',{}),
('ru','добавь сейчас таймер','timer','timer','clarification',{}),
('ru','добавь будильник','timer','alarm','clarification',{}),
('ru','добавь черновик письма','email','draft','clarification',{}),
('ru','отметь письмо как прочитанное','email','mark_read','clarification',{}),
('ru','текущий буфер обмена','clipboard','current','',{}),
('ru','установи буфер обмена','clipboard','set','clarification',{}),
('ru','создай новую папку','filesystem','create_folder','clarification',{}),
('ru','создай новый текстовый файл','filesystem','create_text_file','clarification',{}),
('ru','создай сейчас чек-лист','overlay','checklist_create','clarification',{}),
('ru','отметь пункт 2 в чек-листе панели NEO QA','overlay','checklist_check','',{'name':'NEO QA','item':'2'}),
('ru','включи сейчас музыку','music','play_music','',{}),
('ru','включи песню','music','play_track','clarification',{}),
('ru','подключись к каналу Discord','discord','connect_channel','clarification',{}),
('ru','включи плейлист в Discord','discord','music_playlist','clarification',{}),
('ru','подключи Discord-бота к голосовому каналу','discord','bot_join_voice','',{}),
('ru','отключи Discord-бота от голосового канала','discord','bot_leave_voice','',{}),
('ru','проверь доступность автоматизации пользовательского аккаунта Discord','discord','user_account_automation_unavailable','',{}),
('ru','отправь содержимое буфера обмена в Discord','transfer','clipboard_to_discord','',{}),
('ru','отправь выбранный файл Проводника в Discord','transfer','selected_explorer_file_to_discord','',{}),
('ru','перезагрузи компьютер, пожалуйста','system','restart','',{}),
('ru','пожалуйста, перезагрузи компьютер','system','restart','',{}),
('en','open the specified URL in the browser','browser','open_url','clarification',{}),
('de','spiele das Lied Imagine auf Spotify','spotify','play_track','',{'query':'Imagine'}),
('fr','lis une playlist par son nom dans Yandex Music','yandex_music','playlist','clarification',{}),
('pt','inicie Minha Onda no Discord, por favor','discord','music_my_wave','',{}),
('pt','limpe a fila do Discord, por favor','discord','music_queue_clear','',{}),
('pt','pare a reprodução no Discord, por favor','discord','music_stop','',{}),
('pt','por favor, coloque uma faixa como próxima na fila do Discord','discord','music_queue_next','clarification',{}),
('pt','mostre o uso de CPU GPU RAM e disco por processo','system','top_processes','',{}),
]
@pytest.mark.parametrize('code,text,tool,action,kind,args',CASES)
def test_reported_cases(session,code,text,tool,action,kind,args):
    session.reset_case(code)
    r=check_phrase(session,asdict(Phrase(code,text,tool,action,expected_kind=kind,expected_args=args)))
    wanted={'CLARIFICATION_PASS'} if kind else {'ARGUMENTS_PASS','ROUTE_PASS','CONFIRMATION_REQUIRED'}
    assert r['status'] in wanted,r
    assert not r.get('localization_warning'),r

@pytest.mark.parametrize('code,word,forbidden',[('pt','por','закат'),('es','por','утро'),('fr','son','приглуши')])
def test_idiom_words_are_not_standalone_commands(code,word,forbidden):
    from jarvis.runtime.multilingual_commands import _index
    exact,_=_index(code)
    assert exact.get(word)!=forbidden

@pytest.mark.parametrize('code,text',[('ru','не очищай очередь дискорд'),('en','do not clear the Discord queue'),('ru','мой друг сказал: очисти очередь дискорд'),('ru','почему ты отправил письмо'),('en','why did you send the message')])
def test_no_execution_from_negation_or_reported_speech(session,code,text):
    session.reset_case(code)
    r=check_phrase(session,asdict(Phrase(code,text,expected_kind='no_action')))
    assert r['status']=='NEGATIVE_PASS',r

OLD_SPEECH179 = {'ru': ('включи озвучку', 'выключи озвучку', 'статус озвучки'), 'en': ('enable speech', 'disable voice', 'voice status'),
                 'de': ('aktiviere die sprachausgabe', 'status der sprachausgabe'), 'fr': ('active la voix', 'état de la voix'),
                 'es': ('activa la voz', 'estado de la voz'), 'it': ('attiva la voce', 'stato della voce'), 'pt': ('ativa a voz', 'estado da voz')}

@pytest.mark.parametrize('code',LANGUAGES)
def test_speech_output_commands_are_gone(session,code):
    """NEO 179 does not speak: the old voice-output phrases reach no «speech» tool."""
    for text in OLD_SPEECH179[code]:
        session.reset_case(code)
        session.agent.handle(text)
        assert all(call['tool'] != 'speech' for call in session.calls), (text, session.calls)

@pytest.mark.parametrize('answer,kind,valid',[('25:61','clock',False),('14:30','clock',True),('not-an-address','email',False),('qa@example.invalid','email',True),('javascript:alert(1)','url',False),('https://example.invalid/','url',True),('file:///C:/private','url',False),('2','positive_integer',True),('0','positive_integer',False)])
def test_new_slot_types(answer,kind,valid):
    from jarvis.runtime.dialogue_tasks import Slot,_validate
    assert (_validate(Slot('v',kind,'v'),answer,'ru') is not None)==valid

def test_folder_completion_constructs_destination(tmp_path):
    from jarvis.runtime.dialogue_tasks import PendingIntent,_complete
    p=PendingIntent('filesystem',{'action':'create_folder','folder':str(tmp_path),'name':'Reports'},(),'en',9999999999)
    tool,args=_complete(p)
    assert tool=='filesystem' and args=={'action':'create_folder','destination':str(tmp_path/'Reports')}

PAYLOAD_CASES=json.loads((Path(__file__).parent/'fixtures/provider_payload150.json').read_text(encoding='utf-8'))
@pytest.mark.parametrize('case',PAYLOAD_CASES)
def test_provider_payload_is_not_command_shell(session,case):
    session.reset_case(case['locale'])
    r=check_phrase(session,case)
    assert r['status']=='ARGUMENTS_PASS',r

@pytest.mark.parametrize('phrase,answer,tool,arguments',[
 ('включи песню','NEO QA Song','music',{'action':'play_track','query':'NEO QA Song'}),
 ('подключись к каналу Discord','NEO QA Voice','discord',{'action':'connect_channel','channel':'NEO QA Voice'}),
])
def test_missing_explicit_target_keeps_question_and_literal_followup(session,phrase,answer,tool,arguments):
    session.reset_case('ru')
    first=session.agent.handle(phrase)
    state=session.agent.conversations.get('desktop')
    assert first.outcome=='clarification' and not session.calls
    assert state.pending_intent.tool==tool
    assert state.pending_intent.arguments['action']==arguments['action']
    expected_slot='query' if tool=='music' else 'channel'
    assert state.pending_intent.missing().name==expected_slot
    final=session.agent.handle(answer)
    assert len(session.calls)==1,final.text
    assert session.calls[0]['tool']==tool and session.calls[0]['arguments']==arguments


def test_no_speech_tool_is_registered(session):
    import pytest as _pytest
    with _pytest.raises(Exception):
        session.agent.tools.get('speech')
