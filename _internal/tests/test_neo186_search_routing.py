"""186: SEARCH/CHAT/CLARIFY routing (52 starter scenarios) and search quality SQ-01…SQ-10."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from jarvis.game_web.models import SearchResponse
from jarvis.runtime.web_relevance186 import canonical_question, is_echo, related_is_useful
from jarvis.game_web.searchapi import SearchAPIGoogleProvider
from jarvis.runtime import everyday172, local_chat169, reply_memory168
from jarvis.runtime.search_intent186 import decide, merge_topic
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked

SEARCH, CHAT, ACTION = "search", "chat", "action"
STARTER = [
    ("ru", "Как сделать бумажный самолётик?", SEARCH), ("ru", "Как ты думаешь, как сделать бумажный самолётик?", SEARCH),
    ("ru", "Как ты думаешь, мы хорошо общаемся?", CHAT), ("ru", "Открой браузер", ACTION),
    ("en", "How do I make a paper airplane?", SEARCH), ("en", "In your opinion, how should I make a paper airplane?", SEARCH),
    ("en", "What do you think of our conversation?", CHAT), ("en", "Open the browser", ACTION),
    ("de", "Wie bastle ich einen Papierflieger?", SEARCH), ("de", "Was meinst du, wie bastle ich einen Papierflieger?", SEARCH),
    ("de", "Was hältst du von unserem Gespräch?", CHAT), ("de", "Öffne den Browser", ACTION),
    ("fr", "Comment faire un avion en papier ?", SEARCH), ("fr", "À ton avis, comment faire un avion en papier ?", SEARCH),
    ("fr", "Que penses-tu de notre conversation ?", CHAT), ("fr", "Ouvre le navigateur", ACTION),
    ("es", "¿Cómo hago un avión de papel?", SEARCH), ("es", "¿Qué opinas, cómo puedo hacer un avión de papel?", SEARCH),
    ("es", "¿Qué te parece nuestra conversación?", CHAT), ("es", "Abre el navegador", ACTION),
    ("it", "Come faccio un aeroplanino di carta?", SEARCH), ("it", "Secondo te, come posso fare un aeroplanino di carta?", SEARCH),
    ("it", "Che ne pensi della nostra conversazione?", CHAT), ("it", "Apri il browser", ACTION),
    ("pt", "Como faço um avião de papel?", SEARCH), ("pt", "Na tua opinião, como posso fazer um avião de papel?", SEARCH),
    ("pt", "O que achas da nossa conversa?", CHAT), ("pt", "Abre o navegador", ACTION),
    ("ru", "Как сделать бумажнеый самолётик?", SEARCH), ("en", "How do I make a paper airplnae?", SEARCH),
    ("de", "Wie bastle ich einen Papierfliegr?", SEARCH), ("fr", "Comment faire un avino en papier ?", SEARCH),
    ("es", "¿Cómo hago un avón de papel?", SEARCH), ("it", "Come faccio un aeroplanio di carta?", SEARCH),
    ("pt", "Como faço um aviao de papel?", SEARCH),
    ("ru", "Из бумаги самолётик как сделать, подскажи, пожалуйста?", SEARCH),
    ("en", "A paper airplane: how can I make one, please?", SEARCH),
    ("de", "Einen Papierflieger würde ich gerne basteln. Wie geht das?", SEARCH),
    ("fr", "Un avion en papier, comment est-ce que je peux en faire un, s’il te plaît ?", SEARCH),
    ("es", "Un avión de papel, ¿cómo lo puedo hacer, por favor?", SEARCH),
    ("it", "Un aeroplanino di carta, come posso farlo, per favore?", SEARCH),
    ("pt", "Um avião de papel, como é que o posso fazer, por favor?", SEARCH),
    ("ru", "Почему бумажный самолётик летит?", SEARCH), ("ru", "Как открыть браузер?", SEARCH),
    ("ru", "подскажи способ сложить самолётик из бумаги", SEARCH), ("ru", "как у тебя дела?", CHAT),
    ("ru", "заггули что такое береза", SEARCH), ("ru", "загуглить что такое береза", SEARCH),
]


@pytest.mark.parametrize("code,text,route", STARTER)
def test_pure_decision_follows_whole_intent(code, text, route):
    decision = decide(text, code)
    if route == ACTION:
        assert decision.route == ""
    else:
        assert decision.route == route
    if route == SEARCH:
        assert decision.query and "загугл" not in decision.query.casefold()


def test_opinion_lead_in_is_not_a_chat_marker_and_query_is_minimal():
    assert decide("Как ты думаешь, как сделать бумажный самолётик?", "ru").query.casefold().startswith("как сделать")
    assert decide("Ответь без поиска: как сделать бумажный самолётик?", "ru").route == "no_search"
    assert decide("Как это сделать?", "ru").route == "clarify_topic"
    assert merge_topic("Как это сделать?", "Бумажный самолётик") == "Как сделать Бумажный самолётик"
    assert merge_topic("А как это сделать?", "Я хочу сложить бумажный самолётик") == "как сложить бумажный самолётик"


@pytest.fixture
def session(tmp_path):
    with external_io_blocked():
        everyday172.clear_offer(); reply_memory168.clear(); local_chat169.clear_history()
        value = AtlasSession(tmp_path / "run")
        value.config.data["local_chat"]["live_small_talk"] = False
        searches: list[str] = []
        value.searches = searches

        def fake(query, **_kwargs):
            searches.append(query)
            return SearchResponse(query=query, provider="searchapi_google", error="searchapi_no_results")

        value.agent.game_web.searcher.search = fake
        yield value
        value.finish()
        everyday172.clear_offer(); reply_memory168.clear(); local_chat169.clear_history()


def _turn(session, code, text):
    with external_io_blocked():
        session.reset_case(code)
        before = len(session.searches)
        turn = session.agent.handle(text)
        return turn, session.searches[before:], list(session.calls)


@pytest.mark.parametrize("code,text,route", [row for row in STARTER if row[0] in {"ru", "en", "it"}])
def test_agent_route_matches_starter_corpus(session, code, text, route):
    turn, searches, calls = _turn(session, code, text)
    if route == SEARCH:
        assert len(searches) == 1 and not calls
    elif route == CHAT:
        assert searches == [] and not calls
    else:
        assert searches == [] and calls and calls[0]["tool"] == "application"


def _dialog(session, code, *texts):
    with external_io_blocked():
        session.reset_case(code)
        out = []
        for text in texts:
            before = len(session.searches)
            turn = session.agent.handle(text)
            out.append((turn, session.searches[before:]))
        return out


def test_c2_how_to_open_browser_searches_but_timer_status_stays_local(session):
    (turn, searches), = _dialog(session, "ru", "Как открыть браузер?")
    assert searches == ["Как открыть браузер?"] and not session.calls
    rows = _dialog(session, "ru", "поставь таймер на 5 минут", "Сколько осталось на таймере?")
    assert rows[1][1] == [] and session.calls[-1]["tool"] == "timer"


def test_c3_no_search_then_explicit_web_search_of_the_same_subject(session):
    rows = _dialog(session, "ru", "Ответь без поиска: как сделать бумажный самолётик?", "Теперь найди инструкцию в интернете")
    assert rows[0][1] == [] and "без поиска" in rows[0][0].text
    assert rows[1][1] == ["как сделать бумажный самолётик?"]


def test_c4_unknown_subject_is_asked_then_searched(session):
    rows = _dialog(session, "ru", "Как это сделать?", "Бумажный самолётик")
    assert rows[0][1] == [] and rows[0][0].text == "Что именно ты хочешь сделать?"
    assert rows[1][1] == ["Как сделать Бумажный самолётик"]


def test_c5_known_subject_needs_no_question(session):
    rows = _dialog(session, "ru", "Я хочу сложить бумажный самолётик", "А как это сделать?")
    assert rows[1][1] == ["как сложить бумажный самолётик"]


def test_c7_cancel_invalidates_pending_clarification(session):
    rows = _dialog(session, "ru", "Как это сделать?", "Отмена. Давай просто поговорим", "Да")
    assert all(not searches for _turn, searches in rows)


def test_c10_referent_uses_safe_topic(session):
    session.agent._conversation_key = "desktop"
    rows = _dialog(session, "ru", "Как сделать бумажный самолётик?", "найди инструкцию к этому")
    assert rows[1][1] == ["Как сделать бумажный самолётик?"]


def test_c10_unknown_referent_asks_without_sending_context(session):
    rows = _dialog(session, "ru", "найди инструкцию к этому")
    assert rows[0][1] == [] and "Что именно" in rows[0][0].text


@pytest.mark.parametrize("error,needle", [
    ("searchapi_no_results", "не вернул полезных"), ("searchapi_key_missing", ""),
    ("searchapi_network_error:ConnectionError", "сет"),
])
def test_c9_limits_are_honest_and_never_model_answers(session, error, needle):
    session.agent.game_web.searcher.search = lambda q, **k: SearchResponse(query=q, provider="searchapi_google", error=error)
    turn, _searches, _calls = _turn(session, "ru", "Почему бумажный самолётик летит?")
    assert "игр" not in turn.text.casefold()
    if needle:
        assert needle in turn.text.casefold()


def test_c9_disabled_search_is_reported(session):
    session.config.data["gaming"]["web_knowledge"]["enabled"] = False
    turn, searches, _ = _turn(session, "ru", "Почему бумажный самолётик летит?")
    assert searches == [] and "выключен" in turn.text


# ---------------------------------------------------------------- search quality

BIRCH = {
    "answer_box": {"snippet": "Карельская берёза — это разновидность березы повислой с узорчатой древесиной."},
    "organic_results": [
        {"link": "https://forum.example/t1", "snippet": "Она вообще на куст больше похожа! Кто сажал?"},
        {"link": "https://shop.example/b", "snippet": "Купить саженцы берёзы со скидкой, доставка по России."},
        {"link": "https://forum.example/t2", "snippet": "Подскажите, когда лучше пересаживать?"},
        {"link": "https://ru.wikipedia.org/wiki/Береза", "snippet": "Берёза — род листопадных деревьев семейства Берёзовые."},
        {"link": "https://ru.wikipedia.org/wiki/Береза", "snippet": "Берёза — род листопадных деревьев семейства Берёзовые."},
    ],
}


def _provider(payload):
    config = SimpleNamespace(get=lambda key, default=None: default)
    provider = SearchAPIGoogleProvider.__new__(SearchAPIGoogleProvider)
    provider.config = config
    provider._request = lambda params, stage="google": (payload, {"provider": "searchapi_google", "stage": stage}, "")
    provider._params = lambda query, language: {"q": query}
    provider.api_key_with_source = lambda: ("k", "test")
    return provider


def test_sq01_general_definition_wins_over_variety_forum_ads_and_duplicates():
    response = _provider(BIRCH).search("загугли что такое береза")
    assert response.direct_answer.startswith("Берёза — род")
    assert "Карельская" not in response.direct_answer and "Купить" not in response.direct_answer
    assert response.direct_answer.count("род листопадных") == 1
    assert response.metadata["answer_sources"] == ["https://ru.wikipedia.org/wiki/Береза"]


def test_sq03_echo_is_not_an_answer():
    payload = {"answer_box": {"answer": "что такое береза"}, "organic_results": []}
    response = _provider(payload).search("что такое береза")
    assert response.direct_answer == "" and response.error
    assert is_echo("Что такое берёза?", "загугли что такое береза")


@pytest.mark.parametrize("sources", [None, 42, True])
def test_sq06_malformed_answer_box_sources_do_not_raise(sources):
    payload = {"answer_box": {"sources": sources, "snippet": "Берёза — дерево."}, "organic_results": []}
    response = _provider(payload).search("что такое береза")
    assert response.direct_answer == "Берёза — дерево."


@pytest.mark.parametrize("snippet", [{"x": 1}, ["a"], True, 42])
def test_sq06_non_text_snippets_are_not_facts(snippet):
    payload = {"organic_results": [{"link": "https://example.org", "snippet": snippet}, {"link": "http://[bad", "snippet": "Берёза — дерево."}]}
    response = _provider(payload).search("что такое береза")
    assert response.direct_answer == "" and response.error


def test_sq04_followup_must_be_on_topic_and_new():
    question = "загугли что такое береза"
    assert not related_is_useful("Как купить новый смартфон?", question)
    assert not related_is_useful("Что такое берёза?", question)
    assert related_is_useful("Где растёт берёза?", question)
    assert canonical_question("Загугли, что такое Берёза?") == canonical_question("что такое береза")


def test_sq05_caveats_survive_shortening():
    from jarvis.runtime.web_answers171 import structure
    text, _ = structure("сколько стоит устройство", "Стоимость устройства составляет 500 рублей. Обратите внимание, что информация может отличаться от окончательной цены. За основу взят старый прайс.", "ru")
    assert "может отличаться" in text


def test_sq02_irrelevant_fragments_do_not_become_key_points():
    from jarvis.runtime.web_answers171 import structure
    answer = ("Берёза — род листопадных деревьев и кустарников семейства берёзовые. Распространена в Северном полушарии. "
              "Гараж можно построить за неделю. Автобус приходит в 8 утра. Шины зимние со скидкой, купить с доставкой. "
              "Реклама: лучшие цены на окна. Кора берёзы белая.")
    text, _ = structure("загугли что такое береза", answer, "ru")
    assert text.startswith("Берёза — род")
    assert "Гараж" not in text and "Автобус" not in text and "Шины" not in text


def test_sq07_trimmed_point_marks_answer_shortened():
    from jarvis.runtime.web_answers171 import structure
    long_point = "Очень длинный пункт про берёзу " * 12 + "КОНЕЦ ВАЖНОГО УСЛОВИЯ"
    text, shortened = structure("что такое береза", "Короткое вступление про берёзу.\n- " + long_point + "\n- Второй пункт про берёзу.", "ru")
    assert shortened and "КОНЕЦ ВАЖНОГО УСЛОВИЯ" not in text


def test_sq10_formatter_keeps_code_and_file_names():
    from jarvis.runtime.web_answers171 import readable, structure
    source = "Вычислите 2**3 и вызовите __init__ в файле data__2026.csv, это **важно**."
    assert readable(source) == "Вычислите 2**3 и вызовите __init__ в файле data__2026.csv, это важно."
    text, _ = structure("python", source, "ru")
    assert "2**3" in text and "__init__" in text and "data__2026.csv" in text


def test_sq08_cache_keys_keep_significant_punctuation(tmp_path):
    from jarvis.game_web.cache import GameWebCache
    config = SimpleNamespace(paths=SimpleNamespace(cache=tmp_path), get=lambda key, default=None: default)
    cache = GameWebCache(config)
    cache.put("__general_web__", "Интернет", "загугли что такое C++", search_query="C++", answer="C++ answer",
              sources=[], provider="searchapi_google")
    assert cache.get("__general_web__", "загугли что такое C#") is None
    assert cache.get("__general_web__", "что такое C++?")["answer"] == "C++ answer"


@pytest.mark.parametrize("question", ["quel temps fait-il aujourd’hui", "найди в интернете курс доллара",
                                      "найди в интернете погоду в Москве", "wie ist das Wetter heute", "notizie di oggi"])
def test_sq09_temporal_and_volatile_questions_get_short_ttl(tmp_path, question):
    from jarvis.game_web.cache import GameWebCache
    config = SimpleNamespace(paths=SimpleNamespace(cache=tmp_path), get=lambda key, default=None: default)
    assert GameWebCache(config).ttl_seconds(question) <= 3600


def test_sq09_today_answer_expires_at_local_midnight(tmp_path):
    import datetime as dt
    from jarvis.game_web.cache import GameWebCache
    created = dt.datetime(2026, 10, 8, 23, 50).timestamp()
    boundary = GameWebCache.day_boundary("какое сегодня число", created)
    assert boundary == dt.datetime(2026, 10, 9, 0, 0).timestamp()
    assert GameWebCache.day_boundary("what is the capital of France", created) == 0.0
    assert time.time() > 0
