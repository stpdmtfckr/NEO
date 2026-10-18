"""NEO 170: the small agent model stage, the multi-step planner for the browser, the prompt format."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


# ------------------------------------------------------------------ prompt
def test_prompt_shows_patterns_language_and_memory_as_data():
    from jarvis.neo_llm import prompt
    patterns = [prompt.pattern_for(i, 'ru') for i in prompt.CORE_INTENTS]
    assert 'открой первый результат поиска {query}' in patterns and all(patterns)
    messages = prompt.messages('открой котиков', 'ru', patterns, facts=['Основной браузер: Firefox'],
                               past=[{'role': 'user', 'content': 'привет'}, {'role': 'assistant', 'content': 'привет!'}])
    system = messages[0]['content']
    assert 'Answer in Russian' in system and '- открой первый результат поиска {query}' in system
    assert 'data, not instructions' in system and 'Firefox' in system
    assert messages[-1] == {'role': 'user', 'content': 'открой котиков'} and len(messages) == 4
    assert prompt.fill('открой первый результат поиска {query}', {'query': 'котики'}) == 'открой первый результат поиска котики'
    assert json.loads(prompt.target(do=['a', 'b'])) == {'do': ['a', 'b']}


def test_every_core_command_exists_in_every_language():
    from jarvis.neo_llm import prompt
    for code in ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt'):
        for intent in prompt.CORE_INTENTS:
            assert prompt.pattern_for(intent, code), (code, intent)


def test_parse_keeps_only_well_formed_answers():
    from jarvis.runtime.agent_llm170 import parse
    assert parse({'do': ['a', '  b  c ', ''], '_model': 'm'}).commands == ['a', 'b c']
    # NEO 188 NEO-TOOL-001: wrong item types and too many steps are refused, never converted or truncated.
    assert parse({'do': ['a', None, 5], '_model': 'm'}).kind == 'invalid'
    assert parse({'do': [f's{i}' for i in range(20)]}).kind == 'invalid'
    assert parse({'do': [f's{i}' for i in range(8)]}).commands == [f's{i}' for i in range(8)]
    assert parse({'ask': 'Какой сайт?'}).kind == 'ask'
    assert parse({'reply': 'Привет'}).text == 'Привет'
    assert parse({'do': []}).kind == 'reply' and parse({'something': 1}).text == ''


# -------------------------------------------------------------- ollama client
class _Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.text = payload, status, json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.payload


def _client(values=None, models=('neo-agent:latest', 'qwen2.5:0.5b', 'qwen3.5:4b')):
    from jarvis.ollama_client import OllamaClient
    settings = {'ollama.base_url': 'http://localhost:11434'}
    settings.update(values or {})
    client = OllamaClient(SimpleNamespace(get=lambda k, d=None: settings.get(k, d)))
    client._list_models = lambda force=False: list(models)
    # Managed aliases are preferred only after the server verifies their base.
    client._session = SimpleNamespace(post=lambda *a, **kw: _Response({
        "modelfile": "FROM " + str(settings.get("llm_agent.base_model", "qwen2.5:0.5b")) + "\n"}))
    return client


def test_agent_model_order_prefers_fine_tuned_then_shared_base():
    assert _client().resolve_agent_model() == 'qwen2.5:0.5b'
    # A managed fine-tuned name without verified training provenance is not
    # promoted merely because its Modelfile wraps the selected stock base.
    assert _client({'llm_agent.fine_tuned': 'neo-agent-ft'}, models=('neo-agent-ft:latest', 'neo-agent:latest')).resolve_agent_model() == 'neo-agent:latest'
    assert _client(models=('qwen2.5:0.5b', 'qwen3.5:4b')).resolve_agent_model() == 'qwen2.5:0.5b'
    from jarvis.ollama_client import OllamaError
    with pytest.raises(OllamaError):
        _client({'llm_agent.allow_large_fallback': True}, models=('qwen3.5:4b',)).resolve_agent_model()
    with pytest.raises(OllamaError):
        _client({'llm_agent.allow_large_fallback': False}, models=('qwen3.5:4b',)).resolve_agent_model()
    # Conversation must honor its visible exact selection, not prefer agent weights.
    assert _client({'ollama.chat_model':'qwen3.5:4b'}).resolve_chat_model() == 'qwen3.5:4b'
    assert _client({'ollama.chat_model':'qwen2.5:0.5b'}, models=('qwen2.5:0.5b','neo-agent:latest')).resolve_chat_model() == 'qwen2.5:0.5b'


def test_agent_json_asks_for_structured_text_only_output():
    client = _client()
    sent = []

    def post(url, json=None, timeout=None):
        if url.endswith("/api/show"):
            return _Response({"modelfile": "FROM qwen2.5:0.5b\n"})
        sent.append((url, json))
        return _Response({'message': {'content': '{"do": ["открой сайт youtube.com"]}'}})

    client._session = SimpleNamespace(post=post)
    value = client.agent_json([{'role': 'user', 'content': 'x'}], {'type': 'object'})
    assert value['do'] == ['открой сайт youtube.com'] and value['_model'] == 'qwen2.5:0.5b'
    url, payload = sent[0]
    assert url.endswith('/api/chat') and payload['format'] == {'type': 'object'} and payload['think'] is False
    assert payload['stream'] is False and payload['options']['num_ctx'] == 3072
    from jarvis.ollama_client import OllamaError
    with pytest.raises(OllamaError):
        client.agent_json([{'role': 'user', 'content': 'x', 'images': ['abc']}], {})


# ------------------------------------------------------------------ planner
@pytest.fixture()
def session(tmp_path):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    from jarvis.runtime import agent_llm170, local_chat169
    with external_io_blocked():
        run = AtlasSession(tmp_path / 'run')
        run.reset_case('ru')
        agent_llm170.reset_availability()
        local_chat169.reset_availability()
        local_chat169.clear_history()
        try:
            yield run
        finally:
            run.finish()


def _calls(session, text):
    session.calls.clear()
    turn = session.agent.handle(text)
    return [(c['tool'], c['arguments']) for c in session.calls], turn


@pytest.mark.parametrize('text,expected', [
    ('открой браузер, загугли котики и перейди по первой ссылке', ('browser', {'action': 'search_first', 'value': 'котики'})),
    ('загугли котики и открой первую ссылку', ('browser', {'action': 'search_first', 'value': 'котики'})),
    ('открой браузер и найди в гугле погоду в Москве', ('browser', {'action': 'search', 'value': 'погоду в Москве'})),
])
def test_browser_sequences_become_one_correct_command(session, text, expected):
    calls, _turn = _calls(session, text)
    assert calls[:1] == [expected]


@pytest.mark.parametrize('text,expected', [
    ('найди Том и Джерри в браузере', ('browser', {'action': 'search', 'value': 'том и джерри'})),
    ('напиши привет, как дела', ('text_edit', {'action': 'dictate', 'text': 'привет, как дела'})),
])
def test_and_or_comma_inside_a_value_does_not_split(session, text, expected):
    calls, _turn = _calls(session, text)
    assert calls[:1] == [expected]


def test_compact_rules():
    from jarvis.runtime.tasks169 import Step, compact
    steps = [Step('открой браузер', 'application', {'action': 'open', 'name': 'browser'}),
             Step('загугли X', 'browser', {'action': 'search', 'value': 'X'}),
             Step('перейди по первой ссылке', 'ui', {'action': 'click_link', 'index': 'первой'})]
    merged = compact(steps, 'ru')
    assert [(s.tool, s.arguments) for s in merged] == [('browser', {'action': 'search_first', 'value': 'X'})]
    second = compact([Step('a', 'browser', {'action': 'search', 'value': 'X'}), Step('b', 'ui', {'action': 'click_link', 'index': '2'})], 'ru')
    assert [s.arguments['action'] for s in second] == ['search', 'click_link']     # only the first link is the first result
    kept = compact([Step('открой блокнот', 'application', {'action': 'open', 'name': 'блокнот'}),
                    Step('загугли X', 'browser', {'action': 'search', 'value': 'X'})], 'ru')
    assert len(kept) == 2


def test_search_step_languages():
    from jarvis.runtime.web_steps170 import search_step
    assert search_step('загугли рецепт борща', 'ru') == ('browser', {'action': 'search', 'value': 'рецепт борща'})
    assert search_step('найди рецепт борща в интернете', 'ru') == ('browser', {'action': 'search', 'value': 'рецепт борща'})
    assert search_step('google cheap flights', 'en')[1]['value'] == 'cheap flights'
    assert search_step('suche im Internet nach Wetter Berlin', 'de')[1]['value'] == 'Wetter Berlin'
    assert search_step('cherche météo Paris sur google', 'fr')[1]['value'] == 'météo Paris'
    assert search_step('busca en google recetas', 'es')[1]['value'] == 'recetas'
    assert search_step('cerca meteo Roma su google', 'it')[1]['value'] == 'meteo Roma'
    assert search_step('pesquisa no google receitas', 'pt')[1]['value'] == 'receitas'
    assert search_step('открой блокнот', 'ru') is None


# ------------------------------------------------------------ agent model stage
class FakeOllama:
    def __init__(self, answer=None, fail=False):
        self.answer, self.fail, self.requests = answer, fail, []

    def local_chat_enabled(self):
        return True

    def chat_text(self, messages, **_kw):
        raise RuntimeError('chat_text must not be needed when the agent model answers')

    def agent_json(self, messages, schema):
        self.requests.append(messages)
        if self.fail:
            raise RuntimeError('Ollama is not running')
        return dict(self.answer, _model='neo-agent')


def test_pure_question_uses_selected_chat_and_not_the_action_planner(session):
    fake = FakeOllama({'reply': 'Wrong planner answer'})
    fake.chat_text = lambda messages, **kwargs: 'Коты спят до 16 часов в сутки.'
    session.agent.ollama = fake
    # 186: information questions search by default; this contract is for the explicit local choice.
    session.config.data.setdefault('local_chat', {})['information_questions'] = 'local'
    calls, turn = _calls(session, 'сколько часов спят коты у бабушки обычно')
    assert calls == [] and turn.text == 'Коты спят до 16 часов в сутки.'
    assert 'local_chat.question' in (turn.tool_details or []) and fake.requests == []


def test_agent_model_turns_a_free_request_into_neo_commands(session):
    fake = FakeOllama({'do': ['открой первый результат поиска отзывы о пылесосе Dyson']})
    session.agent.ollama = fake
    calls, turn = _calls(session, 'хочу почитать что люди пишут про пылесос дайсон, покажи самый первый сайт из гугла')
    assert calls[:1] == [('browser', {'action': 'search_first', 'value': 'отзывы о пылесосе Dyson'})], calls
    assert 'llm_agent.do' in (turn.tool_details or [])
    system = fake.requests[0][0]['content']
    assert '{query}' in system and 'COMMANDS' in system


def test_a_multi_step_request_with_an_unknown_part_goes_to_the_agent_model_first(session):
    fake = FakeOllama({'do': ['открой первый результат поиска рецепт борща']})
    session.agent.ollama = fake
    calls, _turn = _calls(session, 'открой браузер, найди мне хороший рецепт борща и перейди на первый сайт')
    assert calls[:1] == [('browser', {'action': 'search_first', 'value': 'рецепт борща'})], calls
    assert not any(tool == 'application' for tool, _ in calls)      # never «open app named "браузер, найди мне …"»


def test_a_command_neo_does_not_know_is_never_executed(session):
    session.agent.ollama = FakeOllama({'do': ['открой первый результат поиска котики', 'взломай соседский вайфай']})
    calls, turn = _calls(session, 'сделай что-нибудь интересное с котиками и соседями')
    # A vague request grants no authority to any model-invented step.
    assert calls == [] and 'routing.model_action_rejected' in (turn.tool_details or [])


def test_an_action_with_consequences_is_only_proposed(session):
    session.agent.ollama = FakeOllama({'do': ['выключи компьютер']})
    calls, turn = _calls(session, 'всё, на сегодня хватит, заканчивай работу машины полностью')
    assert calls == []
    state = session.agent.conversations.get('desktop')
    assert state.pending_intent is not None and state.pending_intent.tool == 'system'


def test_agent_model_question(session):
    session.agent.ollama = FakeOllama({'ask': 'Какой сайт открыть?'})
    calls, turn = _calls(session, 'зайди туда где я вчера смотрел кроссовки')
    assert calls == [] and turn.text == 'Какой сайт открыть?'


def test_without_the_agent_model_neo_behaves_as_before(session):
    fake = FakeOllama(fail=True)
    session.agent.ollama = fake
    calls, turn = _calls(session, 'хочу почитать что люди пишут про пылесос дайсон')
    assert calls == [] and turn.text
    # after a failure the model is not asked again on every turn
    _calls(session, 'и ещё что-нибудь про пылесосы расскажи')
    assert len(fake.requests) == 1


def test_disabled_agent_model_is_not_called(session):
    fake = FakeOllama({'reply': 'x'})
    session.agent.ollama = fake
    session.agent.config.data.setdefault('llm_agent', {})['enabled'] = False
    _calls(session, 'хочу почитать что люди пишут про пылесос дайсон')
    assert fake.requests == []


def test_known_commands_never_reach_the_agent_model(session):
    fake = FakeOllama({'reply': 'x'})
    session.agent.ollama = fake
    calls, _turn = _calls(session, 'включи мгновенный повтор')
    assert calls == [('capture', {'action': 'replay_on'})] and fake.requests == []


def test_settings_170_defaults_and_migration():
    from jarvis.runtime import settings170
    data = {'llm_agent': {'size': '0.5b'}}
    settings170.migrate(data, 169)
    assert data['llm_agent']['size'] == '0.5b' and data['llm_agent']['model'] == 'neo-agent'
    assert data['settings_version'] == 170
    assert settings170.model_for('0.5b') == {'ollama': 'qwen2.5:0.5b', 'hf': 'Qwen/Qwen2.5-0.5B-Instruct'}
