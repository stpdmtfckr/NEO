import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import time

import pytest

from jarvis.runtime.conversation import ConversationStore
from jarvis.runtime.dialogue_tasks import PendingIntent, Slot
from jarvis.runtime.voice_followup156 import pending_question


def agent():
    result = SimpleNamespace(conversations=ConversationStore())
    result.conversations.get('desktop').pending_intent = PendingIntent(
        'filesystem', {'action':'open_folder'}, (Slot('path','path','folder'),), 'ru', time.monotonic()+60)
    return result


def ui_method():
    path = Path(__file__).resolve().parents[1]/'jarvis/ui/main_window.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_arm_clarification_listen156')
    scope = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), scope)
    return scope[method.name]


def window():
    return SimpleNamespace(agent=agent(), _language_code=lambda:'ru', _agent_generation=1,
        _clarification_voice_origin156=True, _continuous_voice_session=False,
        _app_mic_muted=False, _quitting=False, _busy=False,
        _response_active=lambda:False, config=SimpleNamespace(get=lambda key, default:default),
        _start_command_capture=Mock())


def test_question_expiry_language_and_remote_scope():
    a = agent()
    assert pending_question(a,'ru')
    assert pending_question(a,'en') is None
    assert pending_question(a,'ru',now=time.monotonic()+61) is None
    a.conversations.get('telegram:chat').pending_intent = a.conversations.get('desktop').pending_intent
    a.conversations.get('desktop').pending_intent = None
    assert pending_question(a,'ru') is None


def test_one_capture_per_question_turn_no_continuous_mode():
    w = window(); method = ui_method()
    assert method(w)
    w._start_command_capture.assert_called_once_with(seamless=False)
    assert not method(w)
    assert not w._continuous_voice_session
    # An invalid answer may produce a new question for the same missing slot.
    w._agent_generation += 1
    assert method(w)
    w.agent.conversations.get('desktop').pending_intent = None
    assert not method(w)


@pytest.mark.parametrize('attr,value', [('_clarification_voice_origin156',False),
    ('_app_mic_muted',True),('_quitting',True),('_busy',True),
    ('_continuous_voice_session',True),('_response_active',lambda:True)])
def test_no_followup_capture_outside_authorized_voice_question(attr,value):
    w=window();setattr(w,attr,value)
    assert not ui_method()(w)
    w._start_command_capture.assert_not_called()


def extracted(name, **scope):
    path = Path(__file__).resolve().parents[1]/'jarvis/ui/main_window.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    scope.update(Any=object, QuickMediaCommand=object, RecordingResult=object)
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), scope)
    return scope[name]


@pytest.mark.parametrize('answer', ['2', '35', 'пауза', 'Джарвис, привет', 'No Surprises'])
def test_question_answers_survive_real_voice_handler(answer):
    bus = Mock()
    w = window()
    w.config = SimpleNamespace(get=lambda key, default: 'Джарвис' if key=='wake_word.phrase' else default)
    w._voice_unlock_phrase_matches = Mock(side_effect=AssertionError('literal must not unlock'))
    w._normalize_recognized_command = Mock(side_effect=AssertionError('literal must not be rewritten'))
    w._looks_like_linguistic_text = Mock(side_effect=AssertionError('slot validation belongs to Agent'))
    w._finalize_live_user_bubble = Mock()
    w.input = Mock()
    w._send_recognized_text = Mock()
    callback = SimpleNamespace(singleShot=lambda delay, fn: fn())
    extracted('_voice_recognized', developer_bus=lambda:bus, QTimer=callback)(w,answer)
    w._send_recognized_text.assert_called_once_with(answer, raw_text=answer)


def test_pending_literal_cannot_take_direct_media_lane():
    from jarvis.quick_media_commands import QuickMediaCommand
    w=window();w._voice_recognized=Mock()
    w._quick_media_action_enabled=Mock(side_effect=AssertionError('must not reach transport'))
    extracted('_dispatch_quick_media_command')(w, QuickMediaCommand(action='pause',target='auto',heard='пауза'))
    w._voice_recognized.assert_called_once_with('пауза')


def test_full_asr_used_for_pending_literal_even_when_fast_media_enabled():
    w=window()
    w.recognizer=SimpleNamespace(transcribe_detailed=Mock(return_value=SimpleNamespace(text='пауза',hypotheses=[])))
    w.fast_media_recognizer=SimpleNamespace(transcribe_fast_control_probe=Mock(side_effect=AssertionError('fast media is not a slot decoder')))
    record=SimpleNamespace(correlation_id='test-slot')
    fn=extracted('_timed_command_asr', self=w, time=time, duration=1.0,
        awaiting_slot=Slot('text','text','text'), latency_profiler=lambda:Mock(), developer_bus=lambda:Mock())
    result=fn(record)
    assert result['kind']=='text' and result['text']=='пауза'
    # NEO 188 NEO-ASR-001: a free-text answer is decoded with a neutral per-request profile.
    w.recognizer.transcribe_detailed.assert_called_once_with(record, profile={'initial_prompt': '', 'hotwords': '', 'profile': 'neutral'})
