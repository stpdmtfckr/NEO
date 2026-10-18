"""Typo tolerance 165: keyboard layout, weighted word repair and last-chance rescue."""
import json
from pathlib import Path

import pytest

from jarvis.runtime import command_typos165 as typos
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.atlas_contracts149 import prepare_fixtures
from jarvis.testing.isolation import external_io_blocked

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        value = AtlasSession(tmp_path_factory.mktemp('typos165') / 'run')
        prepare_fixtures(value)
        yield value
        value.finish()


def route(session, text, code='ru'):
    session.reset_case(code)
    turn = session.agent.handle(text)
    call = session.calls[0] if session.calls else None
    return turn, (call['tool'] + '.' + str(call['arguments'].get('action', ''))) if call else ''


def test_edit_costs_follow_real_typing_mistakes():
    assert typos.distance('открйо', 'открой', 'ru') == 0.5          # swapped letters
    assert typos.distance('пауза', 'паущза', 'ru') < 1.0             # neighbouring key
    assert typos.distance('пакажи', 'покажи', 'ru') == 0.35          # unstressed vowel
    assert typos.distance('музыкуу', 'музыку', 'ru') == 0.4          # key pressed twice
    assert typos.distance('ouvre', 'óuvre', 'fr') == 0.2             # diacritic
    assert typos.distance('открой', 'закрой', 'ru') > 1.0            # a different word


@pytest.mark.parametrize('typed,expected', [
    ('открйо', 'открой'), ('пакпу', 'папку'), ('вклюи', 'включи'), ('сделй', 'сделай'),
    ('паущу', 'паузу'), ('откройпапку', 'открой папку'),
])
def test_unknown_words_find_the_nearest_command_word(typed, expected):
    assert typos.word_options(typed, 'ru')[0][1] == expected


@pytest.mark.parametrize('word', ['включивмузыку', 'открылпапку', 'запустилиигру'])
def test_a_report_with_a_lost_space_is_not_split_into_a_command(word):
    assert not typos.word_options(word, 'ru', True, True)


@pytest.mark.parametrize('word', ['выклюич', 'вылкючи', 'выкоючи'])
def test_a_typo_of_a_protected_verb_never_becomes_its_opposite(word):
    assert not typos.word_options(word, 'ru', True, True)


@pytest.mark.parametrize('word', ['удлаи', 'закрйо', 'отпрвь', 'не', 'ен'])
def test_a_guess_never_creates_a_risky_verb_or_negation(word):
    targets = {target for _, target in typos.word_options(word, 'ru', True, True)}
    assert not targets & {'удали', 'закрой', 'отправь', 'не'}


def test_real_word_forms_are_protected():
    data = json.loads((ROOT / 'config/typo_lexicon165.json').read_text(encoding='utf-8'))
    assert data['schema'] == 2 and data['hash'] == 'crc32-utf8'
    for word in ('включил', 'запустил', 'поставил'):
        assert typos.is_stable(word, 'ru')
        assert not typos.word_options(word, 'ru', True, True)
    assert typos.is_real_word('пагода', 'ru') and not typos.is_stable('пагода', 'ru')
    assert not typos.is_real_word('открйо', 'ru')


def test_lexicon_is_compact_in_memory():
    soft, stable = typos._tables('ru')
    assert soft.itemsize == 4 and len(soft) + len(stable) > 30000
    assert (len(soft) + len(stable)) * soft.itemsize < 400_000


@pytest.mark.parametrize('typed,expected', [
    ('jnrhjq gfgre', 'открой папку'),
    ('rfrfz gjujlf', 'какая погода'),
    ('открой gfgre', 'открой папку'),
    ('Jnrhjq gfgre pfuheprb', 'Открой папку загрузки'),
    ('ghbdtn', 'привет'),
    ('jnrhjq gfgre/', 'открой папку.'),
])
def test_wrong_keyboard_layout_is_undone(typed, expected):
    assert typos.fix_layout(typed, 'ru') == expected


@pytest.mark.parametrize('text', [
    'открой Steam', 'запусти discord', 'open discord', 'открой https://example.com',
    'прочитай C:\\docs\\report.txt', 'включи трек "Nirvana"', 'hello world',
])
def test_layout_leaves_real_latin_text_alone(text):
    assert typos.fix_layout(text, 'ru') == text


def test_english_mode_undoes_russian_layout():
    assert typos.fix_layout('щзут discord', 'en') == 'open discord'


def test_soft_repair_needs_a_typed_command_and_keeps_payload():
    seen = []

    def proposal(text):
        seen.append(text)
        return ('music', {'action': 'play_track'}) if text.startswith('включи трек') else None

    assert typos.repair('вклюи трек кино группа крови', 'ru', proposal)[0] == 'включи трек кино группа крови'
    seen.clear()
    # Correct command words and real payload words: nothing to try, parser not asked.
    assert typos.repair('включи трек кино группа крови', 'ru', proposal) == ('включи трек кино группа крови', ())
    assert not seen


def test_soft_repair_keeps_a_parsed_original_and_statements():
    assert typos.repair('вклюи музыку', 'ru', lambda text: ('music', {})) == ('вклюи музыку', ())
    assert typos.repair('я включил музыку', 'ru', lambda text: ('music', {})) == ('я включил музыку', ())


def test_real_word_is_fixed_only_into_a_fully_literal_command():
    def proposal(text):
        return ('weather', {'action': 'summary'}) if text == 'какая погода' else None
    assert typos.repair('какая пагода', 'ru', proposal)[0] == 'какая погода'


def test_distinct_equally_cheap_repairs_are_asked_not_guessed():
    options = {'включи звук': ('audio', {'action': 'unmute'}), 'включи зум': ('application', {'name': 'zoom'})}
    rows = [typos.Repair('включи звук', 0.5, (('звкк', 'звук'),)), typos.Repair('включи зум', 0.5, (('звкк', 'зум'),))]
    chosen, alternatives = typos._choose(rows, options.get, limit=4)
    assert chosen is None and set(alternatives) == set(options)


def test_rescue_allows_short_words_but_not_statements():
    def check(text):
        return ('music', {'action': 'pause'}) if text == 'поставь на паузу' else None
    assert typos.rescue('поставь ан паузу', 'ru', check).text == 'поставь на паузу'
    assert typos.rescue('я включил музыку', 'ru', lambda text: ('music', {})) is None
    assert typos.rescue('не вклюи музыку', 'ru', lambda text: ('music', {})) is None


@pytest.mark.parametrize('text,expected', [
    ('jnrhjq gfgre pfuheprb', 'desktop.open'),
    ('откройпапку загрузки', 'desktop.open'),
    ('rfrfz gjujlf', 'weather.summary'),
    ('какая пагода', 'weather.summary'),
    ('включи следущий трек', 'music.next'),
])
def test_agent_understands_typos(session, text, expected):
    _, tool = route(session, text)
    assert tool == expected, (text, session.calls)


@pytest.mark.parametrize('text', [
    'я включил музыку вчера', 'спасибо большое', 'не вклюи музыку',
    'удлаи файл C:\\data.txt', 'выклучи компьютер',
])
def test_agent_never_turns_statements_or_risky_typos_into_actions(session, text):
    turn, tool = route(session, text)
    assert tool in {'', 'system.shutdown'} and not (tool and turn.pending is None), (text, session.calls)
    if tool:
        assert turn.pending is not None  # a risky action still requires confirmation


def test_rescue_is_recorded_and_history_has_one_user_row(session):
    session.reset_case('ru')
    turn = session.agent.handle('откройпапку загрузки')
    users = [row for row in session.agent.history if row['role'] == 'user']
    assert 'typo.rescue' in (turn.tool_details or []) or session.calls
    assert len(users) == 1


def test_voice_input_does_not_flip_layout(session):
    session.reset_case('ru')
    session.agent.handle('jnrhjq gfgre', input_source='voice')
    assert not session.calls


def test_voice_rescue_uses_another_whisper_hypothesis(session):
    session.reset_case('ru')
    agent = session.agent
    agent.remember_voice_hypotheses('блабла бла', [
        {'text': 'блабла бла', 'primary': True},
        {'text': 'удали файл C:\\x.txt', 'score': -0.1},
        {'text': 'какая погода', 'score': -0.3},
    ])
    turn = agent.handle_stream('блабла бла', input_source='voice', raw_user_text='блабла бла')
    assert [c['tool'] for c in session.calls] == ['weather'], (turn, session.calls)
    assert 'typo.rescue' in turn.tool_details


def test_stale_voice_hypotheses_are_ignored(session):
    session.reset_case('ru')
    agent = session.agent
    agent.remember_voice_hypotheses('другая фраза', [{'text': 'какая погода'}])
    agent.handle_stream('блабла бла', input_source='voice', raw_user_text='блабла бла')
    assert not session.calls
