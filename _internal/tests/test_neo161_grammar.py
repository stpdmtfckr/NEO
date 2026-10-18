"""Literal grammar, speech correction boundary and shipped spelling atlas."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from jarvis.runtime.speech_understanding import SpeechUnderstandingEngine
from jarvis.runtime.command_repair161 import candidates, repair
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.atlas_contracts149 import prepare_fixtures
from jarvis.testing.isolation import external_io_blocked

@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        value=AtlasSession(tmp_path_factory.mktemp('grammar161')/'run');prepare_fixtures(value)
        yield value
        value.finish()

@pytest.mark.parametrize('original,expected',[
 ('включимая волна','включи моя волна'),
 ('включимоюволну','включи Мою волну'),
 ('включимояволна','включи моя волна'),
 ('покажи очреедь музыки','покажи очередь музыки'),
 ('открой пакпу','открой папку'),
 ('запусти иргу','запусти игру'),
 ('про анализируй экран: Example Text','Проанализируй экран: Example Text'),
])
def test_speech_uses_fixed_grammar_without_scanning_entities(session,original,expected):
    session.reset_case('ru')
    engine=SpeechUnderstandingEngine(session.config,agent=session.agent)
    def forbidden():raise AssertionError('No process or filesystem vocabulary scan for a fixed command')
    engine._refresh_entities=forbidden
    result=engine.resolve(original,language='ru',record=False)
    assert result.text==expected,(original,result)
    assert result.raw==original
    assert result.corrections[0].reason=='fixed_command_grammar161'
    assert not session.calls

@pytest.mark.parametrize('text',[
 'включимая волна', 'Джарвис, Джарвис, включимая волна',
 'Открйо мир', 'https://example.invalid/включимаяволна?q=открйо',
])
def test_pending_voice_name_is_literal_before_any_spoken_cleanup(session,text):
    session.reset_case('ru');session.agent.handle('запусти игру')
    engine=SpeechUnderstandingEngine(session.config,agent=session.agent)
    result=engine.resolve(text,language='ru',record=False)
    assert result.text==text and not result.corrections
    assert not session.calls

@pytest.mark.parametrize('phrase',[
 'прочитай фаил "C:\\Files\\Otkryo?.txt"',
 'открой пакпу "Моя воля"',
 'включи трке Nirvnaa?!',
 'включитрек "Включимая волна"',
])
def test_repaired_fixed_words_preserve_every_payload_character(phrase):
    rows=candidates(phrase,'ru');assert rows,phrase
    payload=phrase[phrase.index(' ')+1:] if phrase.startswith('включитрек') else phrase.split(' ',2)[2]
    assert all(row.text.endswith(payload) for row in rows),(phrase,rows)

@pytest.mark.parametrize('code,phrase',[
 ('ru','включи моя воля'),('ru','не включимая волна'),('ru','выклучи компьютер'),
 ('ru','удлаи файл C:\\data.txt'),('ru','включивмузыку'),
 ('en','play the next tack'),('en','openedfolder'),
])
def test_real_words_negation_risky_spelling_are_not_guessed(code,phrase):
    assert not candidates(phrase,code)

@pytest.mark.parametrize('phrase,tool,action',[
 ('удалифайл','filesystem','delete'),
 ('закройприложение','application','close'),
 ('выключимузыку','music','stop'),
])
def test_exact_spacing_repair_preserves_risk_and_missing_target(session,phrase,tool,action):
    session.reset_case('ru');result=session.agent.handle(phrase)
    p=session.agent.conversations.get('desktop').pending_intent
    if p is not None:
        assert p.tool==tool and p.arguments['action']==action,(result,p)
        assert not session.calls
    elif result.pending:
        assert result.pending.tool_name==tool
        assert result.pending.arguments['action']==action
    else:
        assert tool=='music' and len(session.calls)==1 and session.calls[0]['arguments']['action']=='stop',session.calls

@pytest.mark.parametrize('code',['ru','en','de','fr','es','it','pt'])
def test_one_click_atlas_contains_all_canonical_functions_and_typo_classes(code):
    import json
    from jarvis.testing.typo_cases161 import cases, mutations
    root=Path(__file__).resolve().parents[1]
    canonical={owner:phrases for p in (root/'config/canonical_phrases').glob('*.json') for owner,phrases in json.loads(p.read_text()).items()}
    rows=list(cases(root,code))
    assert len(canonical)==529  # registry actions plus reviewed aliases (NEO 169: memory, knowledge, plugins, command model, screen, instant replay; 170: assistant model; 179: minus 6 speech actions)
    assert len(rows)>=1500
    assert {row['source'] for row in rows}=={'typo161_swap','typo161_insert','typo161_delete','typo161_fusion'}
    assert all(row['expected_tool']+'.'+row['expected_action'] in canonical for row in rows)
    # Complete expected owners come from source contracts; no call into repair
    # candidates to choose a convenient expected destination for the test.
    assert len({row['expected_tool']+'.'+row['expected_action'] for row in rows})>=400


def test_existing_reschedule_missing_slots_outrank_schedule_spelling(session):
    session.reset_case('en')
    first=session.agent.handle('reschedule timer')
    state=session.agent.conversations.get('desktop')
    pending=state.pending_intent
    assert pending and pending.tool=='timer' and pending.arguments=={'action':'reschedule'}
    assert [(item.name,item.kind) for item in pending.slots]==[('id','name'),('seconds','duration')]
    assert not session.calls
    session.agent.handle('NEO QA')
    assert state.pending_intent.arguments=={'action':'reschedule','id':'NEO QA'}
    assert state.pending_intent.missing().name=='seconds'
    assert not session.calls
    session.agent.handle('15 minutes')
    assert session.calls==[{'tool':'timer','arguments':{'action':'reschedule','id':'NEO QA','seconds':900.0},'risk':'safe'}]
    assert state.pending_intent is None


def test_speech_keeps_an_already_owned_reschedule_request(session):
    session.reset_case('en')
    engine=SpeechUnderstandingEngine(session.config,agent=session.agent)
    result=engine.resolve('reschedule timer',language='en',record=False)
    assert result.text=='reschedule timer'
    assert not result.corrections
    assert not session.calls


def test_generic_file_source_question_does_not_capture_clipboard_typo(session):
    session.reset_case('en')
    session.agent.handle('copy to clibpoard My Original Text')
    assert len(session.calls)==1 and session.calls[0]['tool']=='clipboard',session.calls
    assert session.calls[0]['arguments']=={'action':'set','text':'My Original Text'}
    assert session.agent.conversations.get('desktop').pending_intent is None
