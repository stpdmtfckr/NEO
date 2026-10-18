"""NEO 188 NEO-SCREEN-001: «подробнее» continues the same screen observation honestly."""
import pytest

from jarvis.runtime import screen_card168 as card
from jarvis.runtime import screen_followup188 as sf

CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
DATA = {'app': 'Chrome — YouTube', 'summary': 'Открыт ролик', 'key_points': [f'пункт {i}' for i in range(8)],
        'visible_text': [f'текст {i}' for i in range(9)], 'problems': [], 'next_steps': ['шаг 1', 'шаг 2', 'шаг 3', 'шаг 4']}


@pytest.fixture(autouse=True)
def clean():
    sf.forget()
    yield
    sf.forget()


def observe(**kw):
    values = dict(hwnd=101, window='Chrome — YouTube', title='YouTube', area='window', monitor='', data=DATA,
                  shown=card.render(DATA, 'ru'), ocr=['Подписаться', 'Комментарии 12'], now=1000.0)
    values.update(kw)
    return sf.remember(**values)


def test_sc01_details_come_from_the_same_snapshot_and_window():
    obs = observe()
    text = sf.continue_text(obs, 'ru', now=1005.0, state='same')
    assert '«Chrome — YouTube»' in text
    assert 'пункт 5' in text and 'пункт 7' in text and 'пункт 0' not in text      # only what the card left out
    assert '«текст 6»' in text and 'шаг 4' in text and 'Подписаться' in text
    assert 'пункт' in text and 'другое окно' not in text


def test_sc01_continuation_only_right_after_its_description():
    obs = observe()
    assert sf.follows(obs, obs.shown)
    assert not sf.follows(obs, 'Таймер на 5 минут поставлен.')
    again = sf.continue_text(obs, 'ru', now=1001.0, state='same')
    assert sf.follows(obs, again)                                              # «а ещё подробнее?»


def test_sc02_binding_is_the_window_handle_not_the_title():
    obs = observe()
    assert 'привязка к нему утрачена' in sf.continue_text(obs, 'ru', now=1001.0, state='gone')
    assert sf.window_state(sf.remember(hwnd=0, window='x', title='x', shown='x')) == 'unknown'


def test_sc03_partial_view_and_partial_ocr_are_stated():
    obs = observe(ocr=[f'строка {i}' for i in range(60)])
    text = sf.continue_text(obs, 'ru', now=1001.0, state='same')
    assert 'Я видел только окно' in text and 'первые 60 строк' in text
    monitor = observe(area='monitor', monitor='2')
    assert 'монитора 2' in sf.continue_text(monitor, 'ru', now=1001.0, state='same')


def test_sc04_changed_or_old_snapshot_is_the_previous_state():
    obs = observe()
    assert 'изменилось' in sf.continue_text(obs, 'ru', now=1001.0, state='changed')
    old = sf.continue_text(obs, 'ru', now=1000.0 + sf.FRESH + 30, state='same')
    assert 'прежнее состояние' in old and 'что на экране' in old
    assert not any(k in vars(obs) for k in ('x', 'y', 'coordinates'))        # nothing to click with


def test_sc05_expired_cleared_or_disallowed_observation_is_not_rebuilt():
    observe()
    assert sf.current(now=1000.0 + sf.TTL + 1) is None
    assert 'больше нет' in sf.continue_text(None, 'ru')
    obs = observe()

    class Config:
        def get(self, key, default=None):
            return False if key == 'vision.enabled' else default
    text = sf.continue_text(obs, 'ru', config=Config(), now=1001.0, state='same')
    assert 'больше нет' in text and 'пункт 5' not in text


def test_sc06_a_newer_observation_replaces_the_old_one():
    first = observe(window='Окно A')
    second = observe(window='Окно B', hwnd=202)
    assert sf.current(now=1001.0) is second and not sf.follows(sf.current(now=1001.0), first.shown.replace('YouTube', 'zzz') + 'x')


def test_sc07_continuation_does_not_confirm_or_click_anything():
    obs = observe()
    text = sf.continue_text(obs, 'ru', now=1001.0, state='same')
    assert 'нажал' not in text.casefold() and 'выполн' not in text.casefold()


def test_sc08_all_languages_keep_the_same_facts():
    obs = observe()
    for code in CODES:
        text = sf.continue_text(obs, code, now=1001.0, state='same')
        assert 'пункт 5' in text and 'Подписаться' in text and 'Chrome — YouTube' in text


def test_nothing_more_when_the_card_already_showed_everything():
    small = {'app': 'Notepad', 'summary': 'Пустой документ', 'key_points': ['один'], 'next_steps': []}
    obs = sf.remember(hwnd=1, window='Notepad', title='Notepad', area='window', data=small, shown=card.render(small, 'ru'), now=1.0)
    assert 'Больше подробностей в этом снимке нет' in sf.continue_text(obs, 'ru', now=2.0, state='same')


@pytest.mark.parametrize('code,phrase', [('ru', 'подробнее'), ('ru', 'А подробнее?'), ('ru', 'расскажи подробнее'), ('ru', 'что ещё там видно'),
                                         ('en', 'tell me more'), ('en', 'more details please'), ('de', 'mehr Details'), ('de', 'erzähl mir mehr'),
                                         ('fr', 'plus de détails'), ('fr', "dis-m'en plus"), ('es', 'más detalles'), ('es', 'cuéntame más'),
                                         ('it', 'più dettagli'), ('it', 'dimmi di più'), ('pt', 'mais detalhes'), ('pt', 'conta-me mais')])
def test_continuation_phrases(code, phrase):
    assert sf.is_more(phrase, code)


@pytest.mark.parametrize('code,phrase', [('ru', 'подробнее о погоде в Москве'), ('ru', 'открой браузер'), ('en', 'more volume'),
                                         ('ru', 'нажми подробнее'), ('en', 'click more')])
def test_other_requests_are_not_continuations(code, phrase):
    assert not sf.is_more(phrase, code)


def test_agent_continues_only_right_after_the_screen_answer(tmp_path):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'run')
        session.reset_case('ru')
        obs = observe(now=None)          # the agent checks the TTL against the real monotonic clock
        session.agent._append_history({'role': 'user', 'content': 'что на экране'})
        session.agent._append_history({'role': 'assistant', 'content': obs.shown})
        turn = session.agent.handle('подробнее')
        assert turn.tool_details == ['screen.observation_details188'] and 'пункт 5' in turn.text
        session.agent._append_history({'role': 'assistant', 'content': 'Другой ответ'})
        other = session.agent.handle('подробнее')
        assert 'screen.observation_details188' not in (other.tool_details or [])
        session.finish()
