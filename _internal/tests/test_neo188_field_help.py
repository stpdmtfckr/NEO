"""NEO 188 NEO-UX-002 / TZ-PENDING-FIELD-EXPLANATION-01: explain the open field, keep the command."""
import copy

import pytest

from jarvis.runtime.conversation import ConversationState
from jarvis.runtime.dialogue_tasks import prepare_dialogue
from jarvis.runtime.feature_commands import parse_feature_command
from jarvis.runtime.field_help188 import explain


def run(text, code, state, now, **kw):
    return prepare_dialogue(text, code, state, lambda v: parse_feature_command(v, code), now=now, **kw)


def test_help01_path_question_keeps_the_same_pending_object():
    state = ConversationState()
    asked = run('скопируй файл', 'ru', state, 0)
    pending = state.pending_intent
    before = copy.deepcopy(pending.snapshot())
    explained = run('Что такое полный путь?', 'ru', state, 5)
    assert explained.feature is None and explained.purpose == 'clarification'
    assert explained.reply.startswith('Полный путь — это адрес файла')
    assert explained.reply.endswith(asked.reply)                     # the same question again
    assert state.pending_intent is pending and pending.snapshot() == before   # TTL/created_at not moved


@pytest.mark.parametrize('code,question', [
    ('ru', 'что это значит?'), ('ru', 'в каком формате?'), ('ru', 'например?'), ('ru', 'а что такое путь'),
    ('en', 'what is a full path?'), ('en', 'what does that mean?'), ('en', 'for example?'),
    ('de', 'was ist ein vollständiger Pfad?'), ('de', 'was bedeutet das?'),
    ('fr', "qu'est-ce qu'un chemin complet ?"), ('fr', "qu'est-ce que ça veut dire ?"),
    ('es', '¿qué es una ruta completa?'), ('es', '¿qué significa eso?'),
    ('it', "cos'è un percorso completo?"), ('it', 'cosa significa?'),
    ('pt', 'o que é um caminho completo?'), ('pt', 'o que significa isso?'),
])
def test_help02_languages(code, question):
    assert explain(question, code, 'absolute_source', 'source') is not None


@pytest.mark.parametrize('code,question', [
    ('ru', 'что такое квантовая физика?'), ('ru', 'что такое любовь'), ('en', 'what is the weather like?'),
    ('de', 'was ist das Wetter morgen?'), ('ru', 'отмена'), ('ru', 'C:\\Users\\a.txt'),
])
def test_unrelated_question_is_not_field_help(code, question):
    assert explain(question, code, 'absolute_source', 'source') is None


def test_help03_no_effect_from_command_words_in_the_question():
    state = ConversationState()
    run('скопируй файл', 'ru', state, 0)
    reply = run('что это значит? например удали всё', 'ru', state, 1)
    assert reply.feature is None
    assert state.pending_intent is None or state.pending_intent.missing().name == 'source'


def test_help04_cancel_still_wins():
    state = ConversationState()
    run('скопируй файл', 'ru', state, 0)
    cancelled = run('отмена', 'ru', state, 1)
    assert cancelled.purpose == 'cancelled' and state.pending_intent is None


def test_help05_expired_question_is_not_resumed_by_help():
    state = ConversationState()
    prepare_dialogue('скопируй файл', 'ru', state, lambda v: parse_feature_command(v, 'ru'), now=0, ttl=30)
    late = prepare_dialogue('что это значит?', 'ru', state, lambda v: parse_feature_command(v, 'ru'), now=31, ttl=30)
    assert late.feature is None and 'Полный путь' not in (late.reply or '')


def test_help06_text_slot_keeps_the_question_as_literal_data():
    state = ConversationState()
    run('создай заметку', 'ru', state, 0)
    run('Покупки', 'ru', state, 1)
    saved = run('что такое полный путь?', 'ru', state, 2)
    assert saved.feature == ('shelf', {'action': 'create', 'kind': 'note', 'title': 'Покупки', 'text': 'что такое полный путь?'})


def test_help07_pending_confirmation_is_not_captured():
    state = ConversationState()
    run('скопируй файл', 'ru', state, 0)
    reply = run('что это значит?', 'ru', state, 1, pending_confirmation=True)
    assert reply.reply is None or 'Полный путь' not in reply.reply


def test_volume_and_duration_fields_have_their_own_explanation():
    state = ConversationState()
    run('поставь громкость', 'ru', state, 0)
    reply = run('что это значит?', 'ru', state, 1)
    assert 'от 0 до 100' in reply.reply and state.pending_intent is not None
    done = run('40', 'ru', state, 2)
    assert done.feature == ('audio', {'action': 'set_volume', 'value': 40})
    state = ConversationState()
    run('Таймер на двадцать', 'ru', state, 0)
    unit = run('что это значит?', 'ru', state, 1)
    assert 'секунды, минуты или часы' in unit.reply and unit.reply.endswith('Двадцать секунд, минут или часов?')
