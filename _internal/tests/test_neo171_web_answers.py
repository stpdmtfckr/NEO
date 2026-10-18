"""NEO 171: talk locally or search; short, focused SearchAPI answers ending with an offer that works."""
from __future__ import annotations

import pytest

from jarvis.game_web.models import SearchResponse, SearchResult
from jarvis.runtime import web_answers171 as web

BMW = """Технические характеристики BMW M5 2020 года (F90) впечатляют и делают его одним из самых быстрых седанов.

### Двигатель
* **Тип:** 4.4-литровый V8 с двумя турбинами.
* **Мощность:** 600 л.с. (625 л.с. в версии Competition).
* **Крутящий момент:** 750 Нм.

### Динамика
* **Разгон 0–100 км/ч:** 3,4 с (3,3 с у Competition).
* **Максимальная скорость:** 250 км/ч (305 км/ч с пакетом M Driver's Package).

### Прочее
* Полный привод M xDrive с режимом 2WD.
* Расход топлива в смешанном цикле около 10,6 л/100 км.
* Салон отделан кожей Merino, есть массаж сидений.
* Модель продаётся в нескольких цветах кузова.
* История модели M5 начинается в 1984 году.

Если вам нужно сравнить её с конкурентами, обратитесь к официальному дилеру.
"""


@pytest.fixture(autouse=True)
def _clean():
    web.forget()
    yield
    web.forget()


@pytest.mark.parametrize('text', [
    'найди характеристики bmw m5 2020 года', 'характеристики bmw m5 f90', 'какой расход у тойоты камри 2019', 'сколько стоит iphone 15',
    'кто такой илон маск', 'какие отзывы о kia rio', 'найди в интернете погоду в москве', 'найди rtx 4090',
    'what are the specs of the tesla model 3', 'wie viel kostet ein iPhone 15', 'combien coûte une Renault Clio',
    'cuánto cuesta un iPhone 15', 'quanto costa una Fiat 500', 'quanto custa um iPhone 15',
])
def test_facts_go_to_the_web(text):
    assert web.classify(text) == 'web'


@pytest.mark.parametrize('text', [
    'как дела', 'что ты думаешь о котах', 'расскажи анекдот', 'как ты думаешь, bmw m5 хорошая машина', 'мне скучно',
    'придумай стих про осень', 'how are you', 'tell me a joke', 'what do you think about cats', 'wie geht es dir',
    'qu’en penses-tu', 'cuéntame un chiste', 'raccontami una barzelletta', 'conta-me uma piada',
])
def test_conversation_stays_local(text):
    assert web.classify(text) == 'chat'


@pytest.mark.parametrize('text', ['что такое рекурсия', 'почему небо голубое', 'характеристики моего компьютера', 'найди файл отчет'])
def test_general_or_local_things_are_not_forced_to_the_web(text):
    assert web.classify(text) == 'either'


def test_subject_and_year():
    assert web.subject('найди характеристики bmw m5 2020 года') == ('bmw m5', '2020')
    assert web.subject('какая мощность у ауди а6 2018 года') == ('ауди а6', '2018')
    assert web.subject('what are the specs of the tesla model 3') == ('tesla model 3', '')
    assert web.subject('кто такой илон маск') == ('илон маск', '')


def test_structure_keeps_the_lead_and_the_points_that_answer_the_question():
    body, shortened = web.structure('найди характеристики bmw m5 2020 года', BMW, 'ru')
    lines = body.splitlines()
    assert lines[0].startswith('Технические характеристики BMW M5 2020 года')
    assert 'Главное:' in lines
    points = [l for l in lines if l.startswith('• ')]
    assert 3 <= len(points) <= 6
    assert any('600 л.с.' in p for p in points) and any('3,4 с' in p for p in points)
    assert not any('цветах кузова' in p or '1984' in p for p in points)      # not what was asked
    assert 'дилеру' not in body and '**' not in body and '###' not in body
    assert shortened
    for sentence in (p[2:].split(': ', 1)[-1] for p in points):            # nothing invented: every point is the provider's text
        assert sentence.rstrip('…') in BMW.replace('**', '') or sentence[:-1] in BMW.replace('**', '')


def test_short_answers_are_left_as_they_are():
    body, shortened = web.structure('кто такой илон маск', 'Илон Маск — предприниматель, основатель SpaceX и глава Tesla.', 'ru')
    assert body == 'Илон Маск — предприниматель, основатель SpaceX и глава Tesla.' and not shortened


def test_offer_for_car_specs_is_reviews_or_another_year():
    items = web.offers('найди характеристики bmw m5 2020 года', 'ru')
    assert web.offer_text(items, 'ru') == 'Хотите, чтобы я нашёл отзывы владельцев об этой модели или характеристики этой модели другого года?'
    assert items[0].query == 'отзывы владельцев bmw m5'


@pytest.mark.parametrize('reply,expected', [
    ('отзывы', {'query': 'отзывы владельцев bmw m5', 'kind': 'reviews'}),
    ('давай отзывы', {'query': 'отзывы владельцев bmw m5', 'kind': 'reviews'}),
    ('первое', {'query': 'отзывы владельцев bmw m5', 'kind': 'reviews'}),
    ('2018', {'query': 'характеристики bmw m5 2018 года', 'kind': 'other_year'}),
    ('характеристики 2017 года', {'query': 'характеристики bmw m5 2017 года', 'kind': 'other_year'}),
])
def test_replies_to_the_offer(reply, expected):
    web.remember('найди характеристики bmw m5 2020 года', web.offers('найди характеристики bmw m5 2020 года', 'ru'), 'ru')
    assert web.resolve(reply) == expected


def test_yes_asks_which_and_another_year_asks_the_year():
    question = 'найди характеристики bmw m5 2020 года'
    web.remember(question, web.offers(question, 'ru'), 'ru')
    assert web.resolve('да')['reply'] == 'Что найти: отзывы владельцев об этой модели или характеристики этой модели другого года?'
    assert web.resolve('другого года') == {'reply': 'Какого года?', 'kind': 'year'}
    assert web.resolve('2016 года') == {'query': 'характеристики bmw m5 2016 года', 'kind': 'other_year'}


def test_no_more_and_new_requests():
    question = 'сколько стоит iphone 15'
    web.remember(question, web.offers(question, 'ru'), 'ru', full='Полный ответ.')
    assert web.resolve('открой браузер и включи музыку на ютубе пожалуйста сейчас') is None
    assert web.resolve('подробнее') == {'reply': 'Полный ответ.', 'kind': 'more'}
    assert web.resolve('нет') == {'reply': 'Хорошо.', 'kind': 'dismiss'}
    assert not web.pending()


def test_one_offer_and_yes():
    web.remember('кто такой илон маск', web.offers('кто такой илон маск', 'ru'), 'ru')
    assert web.resolve('да') == {'query': 'последние новости илон маск', 'kind': 'news'}


def test_offers_in_other_languages():
    for code, question, text in [
        ('en', 'what are the specs of the tesla model 3', 'Want me to find owner reviews of this model or the specs of this model from another year?'),
        ('de', 'wie viel kostet ein iPhone 15', 'Soll ich Bewertungen dazu oder die technischen Daten suchen?'),
    ]:
        assert web.offer_text(web.offers(question, code), code) == text


class _Searcher:
    def __init__(self):
        self.queries = []

    def search(self, query, *, language='ru', **_):
        self.queries.append(query)
        if query.startswith('отзывы'):
            answer = 'Владельцы хвалят BMW M5 за динамику и полный привод. Среди минусов — дорогое обслуживание и расход топлива.'
        else:
            answer = BMW
        return SearchResponse(query, 'searchapi_google', [SearchResult('bmw.ru', 'https://www.bmw.ru/m5')], answer)

    def close(self):
        pass


def test_whole_dialogue_through_the_agent(tmp_path):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'run')
        try:
            session.reset_case('ru')
            searcher = _Searcher()
            session.agent.game_web.searcher = searcher
            session.agent.game_web.cache.get = lambda *a, **k: None
            first = session.agent.handle('найди характеристики bmw m5 2020 года')
            assert searcher.queries == ['найди характеристики bmw m5 2020 года']
            assert first.text.startswith('Нашёл через SearchAPI.io (Google):\n\nТехнические характеристики BMW M5 2020 года')
            assert 'Главное:' in first.text and 'https://' not in first.text        # sources only when enabled
            assert first.text.endswith('Хотите, чтобы я нашёл отзывы владельцев об этой модели или характеристики этой модели другого года?')
            second = session.agent.handle('давай отзывы')
            assert searcher.queries[-1] == 'отзывы владельцев bmw m5'
            assert 'Среди минусов' in second.text and second.tool_details == ['web_intelligence.followup.reviews']
            third = session.agent.handle('как дела')                                # conversation: not a web search
            assert len(searcher.queries) == 2 and 'SearchAPI' not in third.text
        finally:
            web.forget()
            session.finish()
