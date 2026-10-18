"""NEO 175: lead-in words, word order, short-word and name typos, small talk, weather city."""
import pytest

from jarvis.runtime import typos175
from jarvis.runtime.integration_phrases import extract_weather_location
from jarvis.runtime.natural_commands import strip_request_shell, verb_first_ru
from jarvis.runtime.web_answers171 import classify


@pytest.mark.parametrize('text,code,expected', [
    ('слушай, открой калькулятор', 'ru', 'открой калькулятор'),
    ('а теперь включи bluetooth', 'ru', 'включи bluetooth'),
    ('ну открой браузер', 'ru', 'открой браузер'),
    ('так, открой браузер', 'ru', 'открой браузер'),
    ('hey, open calculator', 'en', 'open calculator'),
    ('oye, abre la calculadora', 'es', 'abre la calculadora'),
    ('senti, apri la calcolatrice', 'it', 'apri la calcolatrice'),
])
def test_lead_in_words_are_removed(text, code, expected):
    assert strip_request_shell(text, code) == expected


@pytest.mark.parametrize('text,code', [('слушай музыку', 'ru'), ('next track', 'en'), ('now playing', 'en'), ('так открой браузер', 'ru')])
def test_words_that_start_real_commands_stay_without_a_comma(text, code):
    assert strip_request_shell(text, code) == text


@pytest.mark.parametrize('text,expected', [
    ('калькулятор открой', 'открой калькулятор'),
    ('громкость 50 поставь', 'поставь громкость 50'),
    ('компьютер в гибернацию переведи', 'переведи компьютер в гибернацию'),
    ('компьютер не выключи', 'компьютер не выключи'),          # negation: never reordered
    ('напиши маме: я дома', 'напиши маме: я дома'),
])
def test_verb_said_last_moves_to_the_front(text, expected):
    assert verb_first_ru(text) == expected


@pytest.mark.parametrize('text,code,expected', [
    ('больше ен включай текущую песню', 'ru', 'больше не включай текущую песню'),
    ('включи иом любимые треки', 'ru', 'включи мои любимые треки'),
    ('вставь зи буфера обмена', 'ru', 'вставь из буфера обмена'),
    ('включи песню тест в dscord', 'ru', 'включи песню тест в discord'),
    ('включи плейлист в янекс музыке', 'ru', 'включи плейлист в яндекс музыке'),
    ('play it ni discord', 'en', 'play it in discord'),
    ('open crome', 'en', 'open chrome'),
])
def test_short_word_and_name_typos(text, code, expected):
    assert typos175.repair(text, code) == expected


@pytest.mark.parametrize('text,code', [
    ('no, I meant 20 minutes', 'en'), ('what about no network access', 'en'), ('я не знаю', 'ru'), ('ну и ладно', 'ru'),
    ('напиши маме: ен приду', 'ru'), ('открой https://dscord.com', 'ru'), ('открой окно', 'ru'),
    ('wie geht es dir', 'de'), ('qué tal', 'es'),
])
def test_real_words_dictation_and_links_are_never_changed(text, code):
    assert typos175.repair(text, code) == text


@pytest.mark.parametrize('text', ["what's up", 'whats up', "you're awesome", 'good job', 'как жизнь', 'что делаешь',
                                  'ты лучший', 'wie gehts', 'ça va', 'qué tal', 'come stai', 'tudo bem'])
def test_small_talk_is_chat(text):
    assert classify(text) == 'chat'


@pytest.mark.parametrize('text', ["what's the weather", "what's new in the world", 'какая погода', 'найди рецепт борща'])
def test_real_requests_are_not_small_talk(text):
    assert classify(text) != 'chat'


@pytest.mark.parametrize('text,city', [
    ('покажи погода на завтра', ''), ('погода на неделю', ''), ('погода в москве', 'москве'),
    ('погода москва завтра', 'москва'), ('погода в нижнем новгороде на выходные', 'нижнем новгороде'),
    ('weather for tomorrow', ''), ('weather in berlin', 'berlin'),
])
def test_weather_city_is_never_a_preposition(text, city):
    assert extract_weather_location(text)[0] == city
