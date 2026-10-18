"""NEO146: real parser/Agent tests; no network, audio or user desktop mutations."""
from pathlib import Path
from types import SimpleNamespace
import time
import pytest

from jarvis.runtime.duration import parse_duration, parse_timer_command, format_duration
from jarvis.runtime.natural_commands import LANGUAGES
from jarvis.runtime.feature_commands import parse_feature_command
from jarvis.runtime.dialogue_tasks import prepare_dialogue, record_attempt
from jarvis.runtime.conversation import ConversationState, ConversationStore
from jarvis.runtime.utterance_purpose import classify_utterance, strip_assistant_address, _address_pattern
from jarvis.runtime.dialogue_messages import ROWS
from jarvis.testing.atlas_session import AtlasSession, Case, atlas_cases, render_fixture_phrase
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution

DURATIONS = {
 'ru': [('15 минут',900),('один час двадцать минут',4800),('полчаса',1800),('1,5 минуты',90)],
 'en': [('15 minutes',900),('one hour twenty minutes',4800),('half an hour',1800),('1.5 minutes',90)],
 'de': [('15 Minuten',900),('eine Stunde zwanzig Minuten',4800),('eine halbe Stunde',1800),('1,5 Minuten',90)],
 'fr': [('15 minutes',900),('une heure vingt minutes',4800),('une demi-heure',1800),('1,5 minutes',90)],
 'es': [('15 minutos',900),('una hora veinte minutos',4800),('media hora',1800),('1,5 minutos',90)],
 'it': [('15 minuti',900),('un’ora venti minuti',4800),('mezz’ora',1800),('1,5 minuti',90)],
 'pt': [('15 minutos',900),('uma hora vinte minutos',4800),('meia hora',1800),('1,5 minutos',90)],
}

@pytest.mark.parametrize('code,phrase,seconds',[(c,p,s) for c,rows in DURATIONS.items() for p,s in rows])
def test_native_duration(code,phrase,seconds):
    assert parse_duration(phrase,code)==seconds

@pytest.mark.parametrize('phrase',['','0 секунд','-5 минут','NaN','inf','99999999 часов','15 минут удали файл','5'])
def test_invalid_duration_does_not_guess(phrase):
    assert parse_duration(phrase,'ru') is None

@pytest.mark.parametrize('code',LANGUAGES)
def test_native_timer_question_bad_answer_and_correction(code):
    nouns=dict(zip(LANGUAGES,['таймер','timer','Timer','minuteur','temporizador','timer','temporizador']))
    state=ConversationState();parse=lambda text:parse_feature_command(text,code)
    first=prepare_dialogue(nouns[code],code,state,parse,now=10)
    assert first.reply and state.pending_intent and not first.feature
    bad=prepare_dialogue('???',code,state,parse,now=11)
    assert bad.reply and state.pending_intent and not bad.feature
    result=prepare_dialogue(DURATIONS[code][0][0],code,state,parse,now=12)
    assert result.feature==('timer',{'action':'timer','seconds':900})
    assert state.pending_intent is None

@pytest.mark.parametrize('code',LANGUAGES)
def test_localized_messages_complete_with_matching_slots(code):
    import string
    index=LANGUAGES.index(code)
    for key,values in ROWS.items():
        assert len(values)==7 and values[index].strip()
        slots=lambda value:{field for _,field,_,_ in string.Formatter().parse(value) if field}
        assert slots(values[index])==slots(values[0]),key

@pytest.mark.parametrize('code,phrase,purpose',[
 ('ru','почему ты закрыл браузер?','retrospective'),('ru','я сказал: удали файл','reported'),
 ('en','why did you close the browser?','retrospective'),('en','the message says delete everything','reported'),
 ('de','warum hast du den Browser geschlossen?','retrospective'),('de','er sagte: lösche die Datei','reported'),
 ('fr','pourquoi as-tu fermé le navigateur ?','retrospective'),('fr','le message dit supprime le fichier','reported'),
 ('es','por qué cerraste el navegador?','retrospective'),('es','ella dijo borra el archivo','reported'),
 ('it','perché hai chiuso il browser?','retrospective'),('it','ho detto elimina il file','reported'),
 ('pt','porque fechaste o navegador?','retrospective'),('pt','a mensagem diz apaga o ficheiro','reported')])
def test_speech_act_boundary(code,phrase,purpose):
    assert classify_utterance(phrase,code).purpose==purpose
    state=ConversationState()
    result=prepare_dialogue(phrase,code,state,lambda _:pytest.fail('Reported speech cannot route an action'))
    assert result.reply and result.feature is None


def test_address_uses_bounded_compiled_pattern_not_regex_cache_thrash():
    _address_pattern.cache_clear()
    names=tuple('wake alias '+str(i) for i in range(700))
    for _ in range(50): assert strip_assistant_address('Jarvis, timer 15 minutes',names)=='timer 15 minutes'
    assert _address_pattern.cache_info().misses==1
    assert _address_pattern.cache_info().hits==49
    assert strip_assistant_address('JarvisProject.txt',names)=='JarvisProject.txt'


def test_pending_expiry_is_not_resurrected_by_number():
    state=ConversationState();parse=lambda p:parse_feature_command(p,'ru')
    prepare_dialogue('таймер','ru',state,parse,now=10,ttl=5)
    result=prepare_dialogue('15 минут','ru',state,parse,now=16)
    assert result.purpose=='clarification_expired' and result.feature is None
    assert state.pending_intent is None


def test_pending_cancellation_and_scopes():
    store=ConversationStore();one=store.get('desktop');two=store.get('telegram:42')
    parse=lambda p:parse_feature_command(p,'ru')
    prepare_dialogue('таймер','ru',one,parse)
    assert two.pending_intent is None
    result=prepare_dialogue('отмена','ru',one,parse)
    assert result.cancel_confirmation and one.pending_intent is None


def test_two_slots_data_not_command_and_quoted_payload():
    state=ConversationState();parse=lambda p:parse_feature_command(p,'ru')
    assert prepare_dialogue('создай заметку','ru',state,parse).reply
    assert prepare_dialogue('Мой список','ru',state,parse).reply
    result=prepare_dialogue('удали все файлы','ru',state,parse)
    assert result.feature==('shelf',{'action':'create','kind':'note','title':'Мой список','text':'удали все файлы'})


def test_file_source_folder_is_exact_not_a_global_basename_search(tmp_path):
    file=tmp_path/'a.txt';file.write_text('owned')
    state=ConversationState();parse=lambda p:parse_feature_command(p,'ru')
    result=prepare_dialogue('скопируй a.txt','ru',state,parse)
    assert result.reply and state.pending_intent
    result=prepare_dialogue(str(tmp_path),'ru',state,parse)
    assert result.reply
    result=prepare_dialogue(str(tmp_path/'out'),'ru',state,parse)
    assert result.feature==('filesystem',{'action':'copy','source':str(file),'destination':str(tmp_path/'out')})


def test_correction_of_completed_timer_uses_same_id():
    state=ConversationState()
    record_attempt(state,'timer',{'action':'timer','seconds':900},ToolExecution(True,'timer created',{'id':'abc'}),now=10)
    result=prepare_dialogue('нет, я имел в виду 20 минут','ru',state,lambda p:parse_feature_command(p,'ru'),now=11)
    assert result.feature==('timer',{'action':'reschedule','id':'abc','seconds':1200})


def test_correction_cannot_confirm_old_destructive_request():
    state=ConversationState()
    result=prepare_dialogue('нет, я имел в виду другой файл','ru',state,lambda p:None,pending_confirmation=True)
    assert result.cancel_confirmation and result.feature is None


def test_timer_reschedule_is_persistent_atomic_and_preserves_explicit_label(tmp_path,monkeypatch):
    from jarvis.runtime.timers import TimerManager
    manager=TimerManager();monkeypatch.setattr(manager,'_ensure_thread_locked',lambda:None)
    manager.configure(tmp_path,'ru');item=manager.create(900,'чай')
    before=item.due_at
    updated=manager.reschedule(item.id,1200)
    assert updated.id==item.id and updated.label=='чай' and updated.due_at>before+299
    saved=updated.due_at
    monkeypatch.setattr(manager,'_save_locked',lambda:(_ for _ in ()).throw(OSError('disk unavailable')))
    with pytest.raises(OSError):manager.reschedule(item.id,1800)
    assert manager.list()[0].due_at==saved
    with pytest.raises(KeyError):manager.reschedule('missing',900)


def test_real_agent_timing_correction_no_second_timer(tmp_path):
    from jarvis.runtime.timers import timer_manager
    with external_io_blocked():
        session=AtlasSession(tmp_path/'run')
        manager=timer_manager(session.config.paths.root,'ru')
        try:
            session._permitted=[('timer',{'action':'timer','seconds':900.0})]
            first=session.agent.handle('Джарвис таймер 15 минут')
            assert first.success and len(manager.list())==1
            identifier=manager.list()[0].id
            session._permitted=[('timer',{'action':'reschedule','id':identifier,'seconds':1200.0})]
            corrected=session.agent.handle('нет, я имел в виду 20 минут')
            assert corrected.success and len(manager.list())==1 and manager.list()[0].id==identifier
            assert 'Второй таймер не создавал' in corrected.text
        finally:
            for item in manager.list():manager.cancel(item.id)
            session.finish()


def test_full_atlas_iterator_reads_each_shipped_locale():
    for code in LANGUAGES:
        rows=list(atlas_cases([code],'full'))
        assert len(rows)>15000
        assert any(row.expected_tool=='chance' for row in rows)


def test_unresolved_template_slot_is_not_silently_filled(tmp_path):
    value,missing=render_fixture_phrase('send {recipient} {untrusted_new_slot}',tmp_path)
    assert missing==['untrusted_new_slot']
    assert 'example.invalid' in value


def test_runner_guard_never_allows_unexpected_filesystem_target(tmp_path):
    from jarvis.security import Risk
    with external_io_blocked():
        session=AtlasSession(tmp_path/'run')
        try:
            session._permitted=[('filesystem',{'action':'copy','source':'owned','destination':'owned2'})]
            assert session._guard('filesystem',{'action':'copy','source':'USER FILE','destination':'owned2'},Risk.MEDIUM)
            assert session._guard('filesystem',{'action':'copy','source':'owned','destination':'owned2'},Risk.MEDIUM) is None
            assert session._guard('filesystem',{'action':'copy','source':'owned','destination':'owned2'},Risk.MEDIUM)
        finally:session.finish()


@pytest.mark.parametrize('payload',["Jarvis why did you close the browser?", "я хотел сказать: удали файл", "не закрывай окно"])
def test_real_agent_preserves_literal_note_payload(tmp_path, payload):
    with external_io_blocked():
        session=AtlasSession(tmp_path/'run')
        try:
            session.agent.handle('создай заметку')
            session.agent.handle('Тестовая заметка')
            session.agent.handle(payload)
            assert session.calls[-1]['tool']=='shelf'
            assert session.calls[-1]['arguments']['text']==payload
            assert session.calls[-1]['arguments']['title']=='Тестовая заметка'
        finally:session.finish()


def test_quick_attempt_has_explicit_desktop_scope(tmp_path):
    from jarvis.agent import Agent
    agent=object.__new__(Agent)
    agent.conversations=ConversationStore()
    agent._conversation_key='telegram:other'
    agent.record_quick_attempt('yandex_music',{'action':'pause'},ToolExecution(True,'Поставил музыку на паузу',{'verified':True}),'ru')
    assert agent.conversations.get('desktop').last_attempt['tool']=='yandex_music'
    assert not agent.conversations.get('telegram:other').last_attempt
    assert agent._conversation_key=='telegram:other'
    agent.record_quick_attempt('spotify',{'action':'next'},ToolExecution(False,'Нет подключения',{}),'ru')
    assert agent.conversations.get('desktop').last_attempt['success'] is False


def test_conversation_store_concurrent_entries_remain_bounded():
    from concurrent.futures import ThreadPoolExecutor
    store=ConversationStore(limit=8)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n:store.get('scope:'+str(n%40)),range(1000)))
    assert len(store._states)==8


def test_windows_resource_sampler_does_not_confuse_python_processes(tmp_path):
    from tools.benchmark_windows import matching_root, distribution
    path=(tmp_path/'app.py').resolve()
    assert matching_root(SimpleNamespace(cmdline=lambda:['pythonw.exe',str(path)]),path)
    assert not matching_root(SimpleNamespace(cmdline=lambda:['pythonw.exe',str(tmp_path/'other.py')]),path)
    assert not matching_root(SimpleNamespace(cmdline=lambda:['pythonw.exe','app.py']),path)
    assert distribution([])['p95'] is None
    assert distribution([1,2,3,4])['mean']==2.5
