"""Typed dialogue, exact evidence and reference lifecycle through real Agent."""
import json
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from jarvis.runtime.conversation import ConversationState
from jarvis.runtime.dialogue_tasks import Slot, _validate, prepare_dialogue, record_attempt
from jarvis.runtime.object_context157 import remember, resolve, identity, TTL
from jarvis.runtime.clarification_contracts156 import partial
from jarvis.runtime.natural_commands import LANGUAGES
from jarvis.tools.registry import ToolExecution

@pytest.fixture(scope='module')
def session(tmp_path_factory):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        s=AtlasSession(tmp_path_factory.mktemp('context157')/'run')
        yield s
        s.finish()

@pytest.mark.parametrize('kind,bad,good,expected',[
 ('int157:1:20','21','12',12),('number157:0.5:2','NaN','1,5',1.5),
 ('number157:-100:100','-101','-15',-15),('date157','2026-02-31','2026-09-26','2026-09-26'),
 ('host157','-n 1000','example.invalid','example.invalid'),('hotkey157','ctrl+unknown','ctrl+shift+s',['ctrl','shift','s']),
 ('mouse_button157','random','левая','left')])
def test_new_slot_validation(kind,bad,good,expected):
    slot=Slot('fixture',kind,'field157:query')
    assert _validate(slot,bad,'ru') is None
    assert _validate(slot,good,'ru')==expected

@pytest.mark.parametrize('phrase,tool,action',[
 ('установи громкость аудио','audio','set_volume'),('измени сейчас событие календаря','google_calendar','update'),
 ('отправь письмо','email','send'),('вставь файлы','filesystem','insert_text'),
 ('измени размер шрифта панели','overlay','style'),('выполни задачи по очереди','automation','queue'),
 ('громкость','audio','set_volume'),('будильник','timer','alarm'),('напомни','timer','reminder'),
 ('выполни скрипт PowerShell','powershell','')])
def test_actual_agent_head_has_typed_owner(session,phrase,tool,action):
    session.reset_case('ru')
    result=session.agent.handle(phrase)
    pending=session.agent.conversations.get('desktop').pending_intent
    assert result.outcome=='clarification' and pending,(result.text,session.calls)
    assert (pending.tool,pending.arguments.get('action',''))==(tool,action)
    assert not session.calls


def test_payload_now_and_titles_are_not_normalized(session):
    session.reset_case('en')
    session.agent.handle('set the clipboard')
    result=session.agent.handle('Play now; do not change Now')
    assert session.calls[-1]['arguments']['text']=='Play now; do not change Now',result.text


def seed_file(agent,path,*,now=None,success=True):
    state=agent.conversations.get('desktop')
    record_attempt(state,'filesystem',{'action':'info','source':str(path)},ToolExecution(success,'File exists',{'path':str(path),'kind':'file','verified':True}),now=now)
    return state


def test_actual_agent_read_verified_file_pronoun(session,tmp_path):
    session.reset_case('ru');p=tmp_path/'report.txt';p.write_text('hello')
    seed_file(session.agent,p)
    result=session.agent.handle('прочитай его')
    assert len(session.calls)==1,result.text
    assert session.calls[0]['tool']=='filesystem'
    assert session.calls[0]['arguments']=={'action':'read_text','source':str(p)}


def test_actual_agent_multiple_file_references_ask_select(session,tmp_path):
    session.reset_case('ru')
    paths=[tmp_path/'a.txt',tmp_path/'b.txt']
    for p in paths:p.write_text(p.name)
    state=session.agent.conversations.get('desktop')
    record_attempt(state,'filesystem',{'action':'list','source':str(tmp_path)},ToolExecution(True,'Two files',[{'path':str(p)} for p in paths]))
    first=session.agent.handle('прочитай его')
    assert 'a.txt' in first.text and 'b.txt' in first.text and not session.calls
    second=session.agent.handle('2')
    assert session.calls[-1]['arguments']=={'action':'read_text','source':str(paths[1])},second.text

@pytest.mark.parametrize('kind',['edited','replaced','removed','expired','failed'])
def test_stale_or_unsuccessful_file_context_does_not_execute(session,tmp_path,kind):
    session.reset_case('ru');p=tmp_path/'source.txt';p.write_text('initial')
    seed_file(session.agent,p,now=time.monotonic()-TTL-1 if kind=='expired' else None,success=kind!='failed')
    if kind=='edited':p.write_text('new content')
    if kind=='replaced':p.unlink();p.write_text('another')
    if kind=='removed':p.unlink()
    result=session.agent.handle('прочитай его')
    assert not session.calls,result.text
    assert result.outcome=='clarification'


def test_context_move_follows_normal_confirmation_and_changed_identity(session,tmp_path):
    session.reset_case('ru');p=tmp_path/'move.txt';p.write_text('old')
    seed_file(session.agent,p)
    first=session.agent.handle('перемести его')
    assert first.outcome=='clarification' and not session.calls
    p.write_text('changed meanwhile')
    result=session.agent.handle(str(tmp_path/'target'))
    assert not session.calls and result.outcome=='clarification'


def test_context_delete_confirmation_rechecks_identity(session,tmp_path):
    from jarvis.security import Risk
    session.reset_case('ru');p=tmp_path/'delete.txt';p.write_text('old')
    seed_file(session.agent,p)
    session.agent._confirmation_policy=lambda tool,args,risk:True
    try:
        proposal=session.agent.handle('удали его')
        assert proposal.pending and not session.calls
        p.write_text('changed after confirmation requested')
        result=session.agent.confirm_pending(True)
        assert not session.calls and result.outcome=='clarification'
        assert p.exists()
    finally:session.agent._confirmation_policy=None


def test_reference_locale_session_and_reset_isolation(session,tmp_path):
    session.reset_case('ru');p=tmp_path/'private.txt';p.write_text('local')
    state=seed_file(session.agent,p)
    foreign=prepare_dialogue('read it','en',state,lambda _:None)
    assert foreign.feature is None
    remote=session.agent.handle('прочитай его',input_source='telegram',conversation_id='other')
    assert not session.calls,remote.text
    session.agent.reset()
    assert not session.agent.conversations.get('telegram:other').objects157

@pytest.mark.parametrize('code,phrase',zip(LANGUAGES,('прочитай его','read it','lies es','lis le','lee lo','leggi lo','lê isso')))
def test_all_locales_reference_verified_identity(code,phrase,tmp_path):
    p=tmp_path/'file.txt';p.write_text('hello');state=ConversationState(language=code)
    remember(state,'filesystem',{'action':'info'},ToolExecution(True,'ok',{'path':str(p)}))
    result=prepare_dialogue(phrase,code,state,lambda _:None)
    assert result.feature==('filesystem',{'action':'read_text','source':str(p)})


def test_missing_note_rename_cannot_become_content_overwrite():
    state=ConversationState()
    result=prepare_dialogue('переименуй эту заметку','ru',state,lambda _:None)
    assert result.feature is None and state.pending_intent is None


def test_note_rename_binds_id_and_version(session):
    from jarvis.ui.shelf_store import ShelfStore
    session.reset_case('ru');store=ShelfStore(session.config.paths.root)
    row=store.create('note',title='Original',content='keep body')
    record_attempt(session.agent.conversations.get('desktop'),'shelf',{'action':'read','id':row['id']},ToolExecution(True,'keep body',row))
    first=session.agent.handle('переименуй эту заметку')
    assert first.outcome=='clarification' and not session.calls
    final=session.agent.handle('New note')
    assert session.calls[-1]['arguments']=={'action':'update','id':row['id'],'version':row['version'],'title':'New note'},final.text


def test_note_version_change_during_followup_blocks_mutation(session):
    from jarvis.ui.shelf_store import ShelfStore
    session.reset_case('ru');store=ShelfStore(session.config.paths.root)
    row=store.create('note',title='Stable ID',content='old')
    record_attempt(session.agent.conversations.get('desktop'),'shelf',{'action':'read','id':row['id']},ToolExecution(True,'old',row))
    session.agent.handle('перепиши эту заметку')
    store.update(row['id'],row['version'],content='edited externally')
    final=session.agent.handle('new request text')
    assert not session.calls and final.outcome=='clarification'
    assert store.get(row['id'])['content']=='edited externally'

@pytest.mark.parametrize('code',LANGUAGES)
def test_bare_transport_uses_recent_owned_local_music_without_network_probes(session,code):
    from jarvis.runtime.music_requests157 import BARE
    for action,forms in BARE.items():
        session.reset_case(code)
        state=session.agent.conversations.get('desktop');state.language=code
        record_attempt(state,'music',{'action':'my_wave'},ToolExecution(True,'playing',{'playing':True}))
        result=session.agent.handle(forms[LANGUAGES.index(code)].split('|')[0])
        assert len(session.calls)==1,result.text
        assert session.calls[0]['tool']=='music'
        assert session.calls[0]['arguments']=={'action':action}


def test_bare_transport_preserves_discord_owner_only_in_its_session(session):
    session.reset_case('ru')
    state=session.agent.conversations.get('desktop')
    record_attempt(state,'discord',{'action':'music_bot_play','server':'Local server'},ToolExecution(True,'playing',{'playing':True}))
    result=session.agent.handle('дальше')
    assert session.calls[-1]['tool']=='discord' and session.calls[-1]['arguments']=={'action':'music_skip','server':'Local server'},result.text
    session.calls.clear()
    result=session.agent.handle('дальше',input_source='telegram',conversation_id='other')
    assert not session.calls and result.outcome=='clarification',result.text

@pytest.mark.parametrize('phrase,tool,arguments',[
 ('make window NEO QA 640 wide and 480 high','window',{'action':'move_resize','title':'NEO QA','width':640,'height':480}),
 ('mache das Fenster NEO QA 640 breit und 480 hoch','window',{'action':'move_resize','title':'NEO QA','width':640,'height':480}),
 ('hilf mir, NEO QA auf diesem Computer zu finden','desktop',{'action':'resolve','query':'NEO QA'}),
 ('mache den Text der Anzeige NEO QA 14 Punkt groß','overlay',{'action':'style','name':'NEO QA','font_size':14}),
 ('NEO QA target leiser machen','audio',{'action':'change_target_volume','target':'NEO QA target','value':-15}),
 ('NEO QA: Keep exact text merken','memory',{'action':'remember','key':'NEO QA','value':'Keep exact text'}),
])
def test_real_agent_complete_native_objects_preserve_payload(session,phrase,tool,arguments):
    session.reset_case('en' if phrase.startswith('make ') else 'de')
    result=session.agent.handle(phrase)
    assert len(session.calls)==1,result.text
    assert session.calls[0]['tool']==tool and session.calls[0]['arguments']==arguments

@pytest.mark.parametrize('answer',['1','Музыка'])
def test_bare_media_without_owner_clarifies_then_executes_original_action(session,answer):
    session.reset_case('ru')
    first=session.agent.handle('дальше')
    assert not session.calls and first.outcome=='clarification'
    result=session.agent.handle(answer)
    assert len(session.calls)==1,result.text
    assert session.calls[0]['tool']=='music' and session.calls[0]['arguments']=={'action':'next'}

@pytest.mark.parametrize('wrapper',[
 '{}','Nächste Aufgabe: {}','Mach bitte Folgendes: {}','Hilf mir damit: {}','Ich habe eine Bitte: {}',
 'Das brauche ich jetzt: {}','Erledige bitte diese Aufgabe: {}','Noch etwas: {}','Zum Anfang: {}'])
@pytest.mark.parametrize('phrase,tool,arguments',[
 ('speichere in der Zwischenablage: Literal Data','clipboard',{'action':'set','text':'Literal Data'}),
 ('Speichere einen Entwurf an qa@example.invalid mit dem Betreff Update: Literal Data','email',{'action':'draft','to':'qa@example.invalid','subject':'Update','body':'Literal Data'}),
 ('speichere den Eintrag Profile: Literal Data','memory',{'action':'remember','key':'Profile','value':'Literal Data'}),
])
def test_german_save_request_specific_owner_before_generic_memory(session,wrapper,phrase,tool,arguments):
    session.reset_case('de')
    result=session.agent.handle(wrapper.format(phrase))
    assert len(session.calls)==1,result.text
    assert session.calls[0]['tool']==tool and session.calls[0]['arguments']==arguments


def test_memory_value_containing_clipboard_instruction_stays_literal(session):
    session.reset_case('de')
    result=session.agent.handle('speichere Ort: speichere in der Zwischenablage: Literal Data')
    assert len(session.calls)==1,result.text
    assert session.calls[0]['tool']=='memory'
    assert session.calls[0]['arguments']=={'action':'remember','key':'Ort','value':'speichere in der Zwischenablage: Literal Data'}
