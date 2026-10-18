"""Real deterministic dialogues and Explorer identities; no user desktop actions."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from jarvis.runtime.conversation import ConversationState
from jarvis.runtime.dialogue_tasks import prepare_dialogue
from jarvis.runtime.feature_commands import parse_feature_command
from jarvis.runtime.natural_commands import LANGUAGES

OPEN = ('открой папку','open a folder','öffne einen Ordner','ouvre un dossier','abre una carpeta','apri una cartella','abre uma pasta')
CLOSE = ('закрой папку','close a folder','schließe einen Ordner','ferme un dossier','cierra una carpeta','chiudi una cartella','fecha uma pasta')
CANCEL = ('отмена','cancel','abbrechen','annuler','cancela','annulla','cancelar')
SAMPLES = {'name':'NEO fixture','path':'Downloads','absolute_source':r'C:\NEO_QA\fixture.txt','text':'literal: close everything and send it','percent156':'35','bounded_index':'2','duration':'900 seconds'}
ROWS = [{'hwnd':11,'pid':7,'created':10.0,'title':'Downloads'},{'hwnd':12,'pid':7,'created':10.0,'title':'Documents'}]

@pytest.mark.parametrize('code',LANGUAGES)
def test_folder_open_then_literal_downloads(code):
    state=ConversationState();parse=lambda p:parse_feature_command(p,code)
    first=prepare_dialogue(OPEN[LANGUAGES.index(code)],code,state,parse,now=10)
    assert first.reply and first.feature is None
    second=prepare_dialogue('Downloads',code,state,parse,now=11)
    assert second.feature==('desktop',{'action':'open','hint':'folder','query':'Downloads'})

@pytest.mark.parametrize('code',LANGUAGES)
@pytest.mark.parametrize('count',[0,1,2])
def test_close_folder_snapshot_no_guess(code,count):
    state=ConversationState();p=lambda text:None
    first=prepare_dialogue(CLOSE[LANGUAGES.index(code)],code,state,p,now=10,folder_context=lambda:ROWS[:count])
    if count==0:assert first.reply and not first.feature and not state.pending_intent
    elif count==1:
        assert first.feature[0]=='desktop' and first.feature[1]['action']=='close_folder'
        assert json.loads(first.feature[1]['query'])==ROWS[0]
    else:
        assert first.reply and 'Downloads' in first.reply and 'Documents' in first.reply and not first.feature
        second=prepare_dialogue('2',code,state,p,now=11)
        assert json.loads(second.feature[1]['query'])==ROWS[1]

@pytest.mark.parametrize('answer',['Downloads','1'])
def test_close_folder_exact_label_and_index(answer):
    state=ConversationState()
    prepare_dialogue(CLOSE[0],'ru',state,lambda p:None,now=10,folder_context=lambda:ROWS)
    result=prepare_dialogue(answer,'ru',state,lambda p:None,now=11)
    assert json.loads(result.feature[1]['query'])==ROWS[0]

@pytest.mark.parametrize('answer',['Dowloads','да','все','3'])
def test_close_folder_ambiguous_answer_does_not_close(answer):
    state=ConversationState()
    prepare_dialogue(CLOSE[0],'ru',state,lambda p:None,now=10,folder_context=lambda:ROWS)
    result=prepare_dialogue(answer,'ru',state,lambda p:None,now=11)
    assert result.feature is None and result.reply

@pytest.mark.parametrize('code',LANGUAGES)
def test_folder_context_cancellation_and_expiry(code):
    state=ConversationState();parse=lambda p:None
    prepare_dialogue(CLOSE[LANGUAGES.index(code)],code,state,parse,now=10,folder_context=lambda:ROWS)
    result=prepare_dialogue(CANCEL[LANGUAGES.index(code)],code,state,parse,now=11)
    assert result.feature is None and state.pending_intent is None
    prepare_dialogue(CLOSE[LANGUAGES.index(code)],code,state,parse,now=20,ttl=2,folder_context=lambda:ROWS)
    result=prepare_dialogue('1',code,state,parse,now=23)
    assert result.purpose=='clarification_expired' and result.feature is None


def test_folder_context_new_command_and_language_do_not_inherit():
    state=ConversationState();parse=lambda p:('music',{'action':'status'}) if p=='что играет' else None
    prepare_dialogue(CLOSE[0],'ru',state,parse,now=10,folder_context=lambda:ROWS)
    prepare_dialogue('что играет','ru',state,parse,now=11)
    assert state.pending_intent is None
    prepare_dialogue(CLOSE[0],'ru',state,parse,now=12,folder_context=lambda:ROWS)
    result=prepare_dialogue('1','en',state,lambda p:None,now=13)
    assert result.feature is None and state.pending_intent is None


def test_social_reply_never_becomes_folder_name():
    state=ConversationState();parse=lambda p:None
    prepare_dialogue(OPEN[0],'ru',state,parse,now=10)
    result=prepare_dialogue('спасибо','ru',state,parse,now=11)
    assert result.feature is None and state.pending_intent is not None
    result=prepare_dialogue('Downloads','ru',state,parse,now=12)
    assert result.feature[1]['query']=='Downloads'

@pytest.mark.parametrize('value',[r'{"hwnd":11}', 'null','[]','NaN','{}','not JSON'])
def test_folder_close_malformed_selection(value,monkeypatch):
    import jarvis.runtime.folder_context156 as module
    monkeypatch.setattr(module,'snapshot',lambda:pytest.fail('Malformed selection must not inspect desktop'))
    assert module.close_selected(value)[0] is False

@pytest.mark.parametrize('change',['hwnd','pid','created','title'])
def test_folder_close_reused_or_changed_identity_never_closes(change,monkeypatch):
    import jarvis.runtime.folder_context156 as module
    changed=dict(ROWS[0]);changed[change]='changed' if change=='title' else changed[change]+1
    monkeypatch.setattr(module,'snapshot',lambda:[changed])
    assert module.close_selected(module.encode(ROWS[0]))[1]=='folder_changed156'


def test_folder_close_verified_exact_handle(monkeypatch):
    import jarvis.runtime.folder_context156 as module
    monkeypatch.setattr(module,'snapshot',lambda:[dict(ROWS[0])])
    post=Mock(return_value=1);exists=Mock(return_value=0)
    monkeypatch.setattr(module.ctypes,'windll',SimpleNamespace(user32=SimpleNamespace(PostMessageW=post,IsWindow=exists)),raising=False)
    success,key,label=module.close_selected(module.encode(ROWS[0]))
    assert success and key=='folder_closed156' and label=='Downloads'
    post.assert_called_once_with(11,0x10,0,0)

@pytest.mark.parametrize('code',LANGUAGES)
def test_volume_missing_number_validated_in_native_language(code):
    from jarvis.runtime.clarification_contracts156 import ALIASES
    state=ConversationState();phrase=ALIASES['audio.set_volume'][LANGUAGES.index(code)]
    first=prepare_dialogue(phrase,code,state,lambda p:None,now=10)
    assert first.reply and state.pending_intent is not None
    bad=prepare_dialogue('150',code,state,lambda p:None,now=11)
    assert bad.feature is None and state.pending_intent
    good=prepare_dialogue('35',code,state,lambda p:None,now=12)
    assert good.feature==('audio',{'action':'set_volume','value':35})

@pytest.fixture(scope='module')
def session(tmp_path_factory):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        value=AtlasSession(tmp_path_factory.mktemp('clarifications156')/'run')
        yield value
        value.finish()

@pytest.mark.parametrize('code',LANGUAGES)
def test_real_agent_open_folder_continuation_schema_and_gate(session,code):
    session.reset_case(code)
    first=session.agent.handle(OPEN[LANGUAGES.index(code)])
    assert not session.calls and first.outcome=='clarification'
    second=session.agent.handle('Downloads')
    assert len(session.calls)==1,second.text
    assert session.calls[0]['tool']=='desktop' and session.calls[0]['arguments']=={'action':'open','hint':'folder','query':'Downloads'}

@pytest.mark.parametrize('code',LANGUAGES)
def test_real_agent_folder_choice_retains_risk_confirmation(session,code,monkeypatch):
    import jarvis.runtime.folder_context156 as module
    from jarvis.security import Risk
    session.reset_case(code)
    monkeypatch.setattr(module,'snapshot',lambda:ROWS)
    first=session.agent.handle(CLOSE[LANGUAGES.index(code)])
    assert not session.calls and first.outcome=='clarification'
    session.agent._confirmation_policy=lambda tool,args,risk: risk==Risk.MEDIUM
    try:
        second=session.agent.handle('Downloads')
        assert second.pending is not None,second.text
        assert second.pending.tool_name=='desktop' and second.pending.arguments['action']=='close_folder'
        assert not session.calls
    finally:session.agent._confirmation_policy=None

@pytest.mark.parametrize('code',LANGUAGES)
def test_real_agent_typed_volume_follows_existing_execution_guard(session,code):
    from jarvis.runtime.clarification_contracts156 import ALIASES
    session.reset_case(code)
    first=session.agent.handle(ALIASES['audio.set_volume'][LANGUAGES.index(code)])
    assert first.outcome=='clarification' and not session.calls,first.text
    result=session.agent.handle('35')
    assert len(session.calls)==1,result.text
    assert session.calls[0]['tool']=='audio' and session.calls[0]['arguments']=={'action':'set_volume','value':35}


def test_colloquial_close_and_duplicate_titles_ask_index():
    state=ConversationState()
    rows=[dict(ROWS[0]),{**ROWS[1],'title':'Downloads'}]
    first=prepare_dialogue('закрою папку','ru',state,lambda p:None,now=10,folder_context=lambda:rows)
    assert first.reply and 'Downloads [1]' in first.reply and 'Downloads [2]' in first.reply
    unsure=prepare_dialogue('Downloads','ru',state,lambda p:None,now=11)
    assert unsure.feature is None
    second=prepare_dialogue('2','ru',state,lambda p:None,now=12)
    assert json.loads(second.feature[1]['query'])['hwnd']==12


def test_unavailable_folder_snapshot_does_not_fall_back_to_active_window():
    def unavailable():raise OSError('unavailable')
    result=prepare_dialogue(CLOSE[0],'ru',ConversationState(),lambda p:None,folder_context=unavailable)
    assert result.reply and result.feature is None


def test_pending_menu_is_conversation_scoped():
    from jarvis.runtime.conversation import ConversationStore
    states=ConversationStore()
    prepare_dialogue(CLOSE[0],'ru',states.get('desktop'),lambda p:None,now=10,folder_context=lambda:ROWS)
    result=prepare_dialogue('1','ru',states.get('telegram:other'),lambda p:None,now=11)
    assert result.feature is None and states.get('desktop').pending_intent is not None

@pytest.mark.parametrize('code,phrase,tool,action',[
 ('ru','скопируй заметку','shelf','copy'),('en','copy the note','shelf','copy'),
 ('de','kopiere die Notiz','shelf','copy'),('fr','copie la note','shelf','copy'),
 ('es','copia la nota','shelf','copy'),('it','copia la nota','shelf','copy'),
 ('pt','copia a nota','shelf','copy'),('en','move to tab','browser','activate_tab'),
 ('en','move to the tab named','browser','activate_tab_title'),
 ('en','show the directory tree of','filesystem','tree'),
])
def test_reviewed_exact_contract_precedes_legacy_catchall(session,code,phrase,tool,action):
    session.reset_case(code)
    result=session.agent.handle(phrase)
    state=session.agent.conversations.get('desktop')
    assert result.outcome=='clarification' and state.pending_intent is not None,result.text
    assert (state.pending_intent.tool,state.pending_intent.arguments['action'])==(tool,action)
    assert not session.calls

@pytest.mark.parametrize('phrase,code',[
 ('перемести окно','ru'),('move window','en'),('verschiebe das Fenster','de'),
 ('déplace le curseur dans le champ','fr')
])
def test_other_domain_partial_does_not_turn_into_file_mutation(phrase,code):
    state=ConversationState()
    result=prepare_dialogue(phrase,code,state,lambda p:parse_feature_command(p,code),now=10)
    assert not (state.pending_intent and state.pending_intent.tool=='filesystem')
    assert result.feature is None

from pathlib import Path
SEQUENCES=json.loads((Path(__file__).parent/'fixtures/clarification_sequences156.json').read_text(encoding='utf-8'))

@pytest.mark.parametrize('case',SEQUENCES,ids=lambda row:row['language']+':'+row['expected_tool']+'.'+row['expected_arguments'].get('action',''))
def test_reviewed_sequence_and_final_native_schema(session,case):
    from jarvis.runtime.canonical_commands import parse_canonical_command
    from jarvis.tools.argument_validation import validate
    code=case['language'];state=ConversationState(language=code)
    def parse(text):
        feature=parse_feature_command(text,code)
        if feature:return feature
        native=parse_canonical_command(text,code,lambda tool:session.agent.tools.get(tool).parameters)
        return (native.tool,dict(native.arguments or {})) if native and not native.error else None
    result=prepare_dialogue(case['request'],code,state,parse,now=10)
    assert result.reply and not result.feature and state.pending_intent
    for step,answer in enumerate(case['replies']):
        result=prepare_dialogue(answer,code,state,parse,now=11+step)
    assert result.feature==(case['expected_tool'],case['expected_arguments'])
    assert not validate(result.feature[1],session.agent.tools.get(result.feature[0]).parameters)
    assert state.pending_intent is None


@pytest.mark.parametrize('code,phrase',[
 ('ru','можешь закрыть папку'),('en','could you close a folder'),
 ('de','kannst du einen Ordner schließen'),
 ('fr','peux-tu fermer un dossier'),('es','puedes cerrar una carpeta'),
 ('it','puoi chiudere una cartella'),('pt','pode fechar uma pasta')
])
def test_native_polite_folder_request_keeps_explicit_ambiguity(code,phrase):
    state=ConversationState()
    result=prepare_dialogue(phrase,code,state,lambda p:None,folder_context=lambda:ROWS)
    assert result.feature is None and state.pending_intent and result.reply

@pytest.mark.parametrize('code,phrase',[
 ('ru','папку'),('en','folder'),('de','Ordner'),('fr','dossier'),
 ('es','carpeta'),('it','cartella'),('pt','pasta'),('ru','закрою')])
def test_weak_fragment_does_not_invent_action(code,phrase):
    result=prepare_dialogue(phrase,code,ConversationState(),lambda p:None,folder_context=lambda:ROWS)
    assert result.feature is None

OVERLAY_RENAME=('переименуй панель','rename the panel','benenne die Anzeige','renomme le panneau','cambia el nombre del panel','rinomina il pannello','renomeie o painel')
OVERLAY_MOVE=('перемести панель','move panel','verschiebe die Anzeige','déplace le panneau','mueve el panel','sposta il pannello','mova o painel')

@pytest.mark.parametrize('code',LANGUAGES)
@pytest.mark.parametrize('action',['rename','move'])
def test_real_agent_overlay_missing_slots_stay_in_overlay_domain(session,code,action):
    from jarvis.runtime.clarification_contracts156 import _POSITION_LABELS156
    session.reset_case(code)
    phrase=(OVERLAY_RENAME if action=='rename' else OVERLAY_MOVE)[LANGUAGES.index(code)]
    first=session.agent.handle(phrase)
    assert first.outcome=='clarification' and not session.calls
    state=session.agent.conversations.get('desktop')
    assert state.pending_intent.tool=='overlay' and state.pending_intent.arguments['action']==action
    second=session.agent.handle('NEO fixture')
    assert second.outcome=='clarification' and not session.calls
    if action=='move':
        invalid=session.agent.handle('150')
        assert invalid.outcome=='clarification' and not session.calls
    third=session.agent.handle('New panel name' if action=='rename' else _POSITION_LABELS156[code][2])
    assert len(session.calls)==1,third.text
    expected={'action':action,'name':'NEO fixture',('new_name' if action=='rename' else 'position'):('New panel name' if action=='rename' else 'top_right')}
    assert session.calls[0]['tool']=='overlay' and session.calls[0]['arguments']==expected

@pytest.mark.parametrize('phrase',[
 'переименуй панель','можешь переименовать панель','пожалуйста переименуй панель',
 'перемести панель','пожалуйста перемести панель','можешь переместить панель'])
def test_ru_overlay_heads_never_propose_file_mutation(phrase):
    state=ConversationState()
    result=prepare_dialogue(phrase,'ru',state,lambda p:None)
    assert result.reply and state.pending_intent.tool=='overlay'
    assert not result.feature

@pytest.mark.parametrize('code',LANGUAGES)
def test_all_nine_native_overlay_positions_are_exact_schema_values(code):
    from jarvis.runtime.clarification_contracts156 import _POSITION_KEYS156,_POSITION_LABELS156,overlay_position156
    assert [overlay_position156(value,code) for value in _POSITION_LABELS156[code]]==list(_POSITION_KEYS156)
    assert overlay_position156('somewhere probably on the right',code) is None
