"""NEO 188 NEO-UX-001 / TZ-PARAMETER-CLARIFICATION-CORRECTION-01 and system volume grammar."""
from types import SimpleNamespace

import pytest

from jarvis.runtime.conversation import ConversationState
from jarvis.runtime.dialogue_tasks import prepare_dialogue, record_attempt
from jarvis.runtime.feature_commands import parse_feature_command
from jarvis.runtime import param_repair188 as pr
from jarvis.runtime import volume188
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution


def parse(code):
    return lambda value: parse_feature_command(value, code)


def turn(text, code, state, *, now, timers=None):
    return prepare_dialogue(text, code, state, parse(code), now=now,
                            timer_context=(lambda: timers) if timers is not None else None)


@pytest.mark.parametrize('code,first,unit,question', [
    ('ru', 'Таймер на двадцать', 'Минут', 'Двадцать секунд, минут или часов?'),
    ('ru', 'поставь таймер на 20', 'минуты', '20 секунд, минут или часов?'),
    ('en', 'set a timer for twenty', 'minutes', 'Twenty seconds, minutes or hours?'),
    ('de', 'stelle einen Timer auf zwanzig', 'Minuten', 'Zwanzig Sekunden, Minuten oder Stunden?'),
    ('fr', 'lance un minuteur de vingt', 'minutes', 'Vingt secondes, minutes ou heures ?'),
    ('es', 'pon un temporizador de veinte', 'minutos', '¿Veinte segundos, minutos u horas?'),
    ('it', 'imposta un timer di venti', 'minuti', 'Venti secondi, minuti o ore?'),
    ('pt', 'define um temporizador de vinte', 'minutos', 'Vinte segundos, minutos ou horas?'),
])
def test_param01_number_kept_only_unit_asked(code, first, unit, question):
    state = ConversationState()
    asked = turn(first, code, state, now=0)
    assert asked.feature is None and asked.reply == question
    done = turn(unit, code, state, now=1)
    assert done.feature == ('timer', {'action': 'timer', 'seconds': 1200.0})
    assert state.pending_intent is None
    again = turn(unit, code, state, now=2)                       # PARAM-14: a repeated answer creates nothing
    assert again.feature is None


def test_russian_plural_of_the_unit_question():
    assert pr.unit_question(1, '1', 'ru') == '1 секунду, минуту или час?'
    assert pr.unit_question(3, 'три', 'ru') == 'Три секунды, минуты или часа?'
    assert pr.unit_question(21, '21', 'ru') == '21 секунду, минуту или час?'
    assert pr.unit_question(12, '12', 'ru') == '12 секунд, минут или часов?'


def test_param03_pending_number_corrected_before_execution():
    state = ConversationState()
    turn('Таймер на двадцать', 'ru', state, now=0)
    changed = turn('не двадцать, двенадцать', 'ru', state, now=1)
    assert changed.feature is None and changed.reply == 'Двенадцать секунд, минут или часов?'
    assert state.pending_intent.arguments['_amount188'] == 12
    done = turn('часов', 'ru', state, now=2)
    assert done.feature == ('timer', {'action': 'timer', 'seconds': 43200.0})


def test_param03_mismatched_old_value_changes_nothing():
    state = ConversationState()
    turn('Таймер на двадцать', 'ru', state, now=0)
    reply = turn('не тридцать, двенадцать', 'ru', state, now=1)
    assert reply.feature is None and 'а не тридцать' in reply.reply
    assert state.pending_intent.arguments['_amount188'] == 20


def test_param07_cancel_then_late_unit_does_not_revive():
    state = ConversationState()
    turn('Таймер на двадцать', 'ru', state, now=0)
    cancelled = turn('отмена', 'ru', state, now=1)
    assert cancelled.purpose == 'cancelled' and state.pending_intent is None
    late = turn('минут', 'ru', state, now=2)
    assert late.feature is None


def test_param08_expired_question_is_not_completed():
    state = ConversationState()
    prepare_dialogue('Таймер на двадцать', 'ru', state, parse('ru'), now=0, ttl=30)
    late = prepare_dialogue('минут', 'ru', state, parse('ru'), now=31, ttl=30)
    assert late.feature is None


def test_param13_new_command_replaces_the_question():
    state = ConversationState()
    turn('Таймер на двадцать', 'ru', state, now=0)
    other = turn('громкость 40', 'ru', state, now=1)
    assert other.feature in (None, ('audio', {'action': 'set_volume', 'value': 40}))
    assert state.pending_intent is None


def _created(state, seconds, timer_id='t1', now=10):
    record_attempt(state, 'timer', {'action': 'timer', 'seconds': seconds}, ToolExecution(True, 'ok', {'id': timer_id}), now=now)


def test_param02_same_timer_rescheduled_from_now():
    state = ConversationState()
    turn('Таймер на двадцать', 'ru', state, now=0)
    turn('Минут', 'ru', state, now=1)
    _created(state, 1200.0, now=2)
    rows = [{'id': 't1', 'label': 'Таймер · 20 минут', 'kind': 'timer', 'seconds': 1200.0}]
    corrected = turn('не двадцать, двенадцать', 'ru', state, now=3, timers=rows)
    assert corrected.feature == ('timer', {'action': 'reschedule', 'id': 't1', 'seconds': 720.0})


@pytest.mark.parametrize('code,phrase', [
    ('ru', 'не двадцать, а двенадцать'), ('ru', 'нет, не 20, 12'), ('en', 'not twenty, twelve'), ('en', 'not 20 but 12'),
    ('de', 'nicht zwanzig, sondern zwölf'), ('fr', 'pas vingt, douze'), ('es', 'no veinte, sino doce'),
    ('it', 'non venti, ma dodici'), ('pt', 'não vinte, mas doze'),
])
def test_param15_languages(code, phrase):
    state = ConversationState()
    _created(state, 1200.0)
    rows = [{'id': 't1', 'label': '', 'kind': 'timer', 'seconds': 1200.0}]
    corrected = turn(phrase, code, state, now=11, timers=rows)
    assert corrected.feature == ('timer', {'action': 'reschedule', 'id': 't1', 'seconds': 720.0})


def test_param06_two_matching_timers_ask_which():
    state = ConversationState()
    _created(state, 1200.0, 't2')
    rows = [{'id': 't1', 'label': 'чай', 'kind': 'timer', 'seconds': 1200.0},
            {'id': 't2', 'label': 'паста', 'kind': 'timer', 'seconds': 1200.0}]
    asked = turn('не двадцать, двенадцать', 'ru', state, now=11, timers=rows)
    assert asked.feature is None and 'Какой изменить' in asked.reply and 'чай' in asked.reply and 'паста' in asked.reply
    chosen = turn('1', 'ru', state, now=12, timers=rows)
    assert chosen.feature == ('timer', {'action': 'reschedule', 'id': 't1', 'seconds': 720.0})


def test_param06_old_value_not_matching_changes_nothing():
    state = ConversationState()
    _created(state, 1200.0)
    rows = [{'id': 't1', 'label': '', 'kind': 'timer', 'seconds': 1200.0}]
    reply = turn('не тридцать, двенадцать', 'ru', state, now=11, timers=rows)
    assert reply.feature is None and '20 минут' in reply.reply and 'не на тридцать' in reply.reply


def test_param07_finished_or_cancelled_timer_is_not_recreated():
    state = ConversationState()
    _created(state, 1200.0)
    reply = turn('не двадцать, двенадцать', 'ru', state, now=11, timers=[])
    assert reply.feature is None and 'новый таймер не создавал' in reply.reply


def test_param14_repeated_correction_is_not_applied_twice():
    state = ConversationState()
    _created(state, 1200.0)
    first = turn('не двадцать, двенадцать', 'ru', state, now=11, timers=[{'id': 't1', 'kind': 'timer', 'seconds': 1200.0}])
    record_attempt(state, 'timer', dict(first.feature[1]), ToolExecution(True, 'ok', {'id': 't1'}), now=12)
    second = turn('не двадцать, двенадцать', 'ru', state, now=13, timers=[{'id': 't1', 'kind': 'timer', 'seconds': 720.0}])
    assert second.feature is None and '12 минут' in second.reply


def test_bare_number_correction_uses_the_unit_chosen_for_this_timer():
    state = ConversationState()
    turn('Таймер на двадцать', 'ru', state, now=0)
    turn('Минут', 'ru', state, now=1)
    _created(state, 1200.0, now=2)
    corrected = turn('нет, двенадцать', 'ru', state, now=3)
    assert corrected.feature == ('timer', {'action': 'reschedule', 'id': 't1', 'seconds': 720.0})


def test_bare_number_correction_without_known_unit_asks_for_it():
    state = ConversationState()
    _created(state, 3600.0)
    asked = turn('нет, тридцать', 'ru', state, now=11)
    assert asked.feature is None and asked.reply == 'Тридцать секунд, минут или часов?'
    done = turn('минут', 'ru', state, now=12)
    assert done.feature == ('timer', {'action': 'reschedule', 'id': 't1', 'seconds': 1800.0})


def test_param16_invalid_range_keeps_previous_state():
    state = ConversationState()
    turn('Таймер на двадцать', 'ru', state, now=0)
    reply = turn('часов', 'ru', SimpleNamespace(**vars(state)), now=1)
    assert reply.feature == ('timer', {'action': 'timer', 'seconds': 72000.0})
    state2 = ConversationState()
    turn('таймер на 900', 'ru', state2, now=0)
    too_long = turn('часов', 'ru', state2, now=1)
    assert too_long.feature is None and '7 дней' in too_long.reply


def test_param04_volume_level_corrected_absolute_same_target():
    state = ConversationState()
    record_attempt(state, 'audio', {'action': 'set_volume', 'value': 50, 'target': 'youtube'}, ToolExecution(True, 'ok', {}), now=10)
    corrected = turn('не 50, 30', 'ru', state, now=11)
    assert corrected.feature == ('audio', {'action': 'set_volume', 'value': 30, 'target': 'youtube'})
    mismatch = turn('не 70, 30', 'ru', state, now=12)
    assert mismatch.feature is None and 'громкость 50%' in mismatch.reply
    out_of_range = turn('не 50, 300', 'ru', state, now=13)
    assert out_of_range.feature is None and 'от 0 до 100' in out_of_range.reply
    bare = turn('нет, 20', 'ru', state, now=14)
    assert bare.feature == ('audio', {'action': 'set_volume', 'value': 20, 'target': 'youtube'})


def test_param12_completed_external_action_is_not_replayed():
    state = ConversationState()
    record_attempt(state, 'filesystem', {'action': 'delete', 'source': r'C:\a.txt'}, ToolExecution(True, 'deleted', {}), now=10)
    reply = turn('не 50, 30', 'ru', state, now=11)
    assert reply.feature is None


def test_quoted_text_is_data_not_a_correction():
    assert pr.not_x_y('не «двадцать», двенадцать', 'ru') is None
    assert pr.not_x_y('не включай музыку, потом', 'ru') is None


@pytest.mark.parametrize('code,phrase,expected', [
    ('ru', 'громкость 40', ('audio', {'action': 'set_volume', 'value': 40})),
    ('ru', 'поставь громкость на 40', ('audio', {'action': 'set_volume', 'value': 40})),
    ('ru', 'громкость 50 поставь', ('audio', {'action': 'set_volume', 'value': 50})),
    ('ru', 'звук на сорок', ('audio', {'action': 'set_volume', 'value': 40})),
    ('ru', 'поставь громкость 40%', ('audio', {'action': 'set_volume', 'value': 40})),
    ('ru', 'убавь громкость', ('audio', {'action': 'change_volume', 'value': -10})),
    ('ru', 'прибавь громкость на 20', ('audio', {'action': 'change_volume', 'value': 20})),
    ('ru', 'сделай тише на 15', ('audio', {'action': 'change_volume', 'value': -15})),
    ('en', 'set volume to 40', ('audio', {'action': 'set_volume', 'value': 40})),
    ('en', 'turn the volume down by 20', ('audio', {'action': 'change_volume', 'value': -20})),
    ('de', 'Lautstärke auf 40', ('audio', {'action': 'set_volume', 'value': 40})),
    ('de', 'mach lauter um 10', ('audio', {'action': 'change_volume', 'value': 10})),
    ('fr', 'mets le volume à 30', ('audio', {'action': 'set_volume', 'value': 30})),
    ('fr', 'baisse le volume de 10', ('audio', {'action': 'change_volume', 'value': -10})),
    ('es', 'pon el volumen al 30', ('audio', {'action': 'set_volume', 'value': 30})),
    ('it', 'abbassa il volume di 20', ('audio', {'action': 'change_volume', 'value': -20})),
    ('pt', 'põe o volume a 30', ('audio', {'action': 'set_volume', 'value': 30})),
])
def test_system_volume_grammar(code, phrase, expected):
    assert parse_feature_command(phrase, code) == expected


@pytest.mark.parametrize('code,phrase', [
    ('ru', 'громкость 150'), ('ru', 'не ставь громкость 40'), ('ru', 'громкость музыки 40'),
    ('ru', 'громкость микрофона 40'), ('en', 'volume of discord 40'), ('ru', 'поставь звук на паузу'),
])
def test_system_volume_grammar_leaves_other_owners(code, phrase):
    assert volume188.parse(phrase, code) is None


def test_partial_volume_asks_level():
    state = ConversationState()
    asked = turn('поставь громкость', 'ru', state, now=0)
    assert asked.feature is None and 'от 0 до 100' in asked.reply
    done = turn('50', 'ru', state, now=1)
    assert done.feature == ('audio', {'action': 'set_volume', 'value': 50})


def test_real_agent_unit_question_then_correction_keeps_one_timer(tmp_path):
    from jarvis.runtime.timers import timer_manager
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'run')
        manager = timer_manager(session.config.paths.root, 'ru')
        try:
            asked = session.agent.handle('Таймер на двадцать')
            assert 'секунд, минут или часов' in asked.text and not manager.list()
            session._permitted = [('timer', {'action': 'timer', 'seconds': 1200.0})]
            created = session.agent.handle('Минут')
            assert created.success and len(manager.list()) == 1
            identifier = manager.list()[0].id
            session._permitted = [('timer', {'action': 'reschedule', 'id': identifier, 'seconds': 720.0})]
            corrected = session.agent.handle('не двадцать, двенадцать')
            assert corrected.success and len(manager.list()) == 1 and manager.list()[0].id == identifier
            assert 'от этого момента' in corrected.text and 'Второй таймер не создавал' in corrected.text
            item = manager.list()[0]
            assert abs((item.due_at - item.created_at) - 720) < 1
        finally:
            for item in manager.list():
                manager.cancel(item.id)
            session.finish()


def test_lone_unit_without_question_is_not_fuzzed_into_a_command():
    state = ConversationState()
    reply = turn('минут', 'ru', state, now=0)
    assert reply.feature is None and 'команды' in reply.reply
