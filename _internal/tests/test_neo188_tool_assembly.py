"""NEO 188 NEO-TOOL-001: only a whole, completed, valid model object reaches the executor.

TA04-J2 (exact large integers) is not applicable: the agent object carries only
strings (``do`` steps, ``reply``, ``ask``); numbers inside a step stay literal text.
"""
from types import SimpleNamespace
import json

import pytest
import requests

import jarvis.ollama_client as oc
from jarvis.runtime import agent_llm170
from jarvis.runtime.agent_llm170 import MAX_COMMAND_CHARS, parse
from jarvis.neo_llm.prompt import MAX_STEPS
from jarvis.runtime.cancellation import cancellation_manager
from jarvis.testing.isolation import external_io_blocked


class Config:
    def __init__(self, **values):
        self.values = {'app.language': 'en', 'ollama.base_url': 'http://127.0.0.1:11434', 'ollama.auto_start': False,
                       'llm_agent.enabled': True, 'llm_agent.model': 'planner:1b', 'ollama.vision_model': 'vision:1b', **values}

    def get(self, key, default=None):
        return self.values.get(key, default)


class Raw:
    def __init__(self, chunks):
        self.chunks, self.closed, self.released = chunks, False, False

    def stream(self, amt, decode_content=True):
        yield from self.chunks

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


def wire(records, width=5):
    data = b''.join(json.dumps(r, ensure_ascii=False).encode('utf-8') + b'\n' for r in records)
    return [data[i:i + width] for i in range(0, len(data), width)]


@pytest.fixture(autouse=True)
def isolated():
    cancellation_manager().reset()
    with external_io_blocked():
        yield
    cancellation_manager().reset()


def client_for(records, width=5):
    client = oc.OllamaClient(Config())
    sent = []

    def post(url, **kw):
        sent.append(kw.get('json'))
        result = requests.Response()
        result.status_code = 200
        result.raw = Raw(wire(records, width))
        return result
    client._session = SimpleNamespace(post=post, close=lambda: None)
    client.resolve_agent_model = lambda: 'planner:1b'
    client.resolve_model = lambda *a, **k: 'vision:1b'
    client._runner_resource_options = lambda: {}
    return client, sent


def pieces(text, size=3):
    return [{'message': {'content': text[i:i + size]}, 'done': False} for i in range(0, len(text), size)]


def test_ta01_ta02_parts_then_empty_terminal_record_give_one_whole_object():
    raw = json.dumps({'do': ['открой браузер', 'найди погоду в Москве']}, ensure_ascii=False)
    client, sent = client_for(pieces(raw) + [{'message': {'content': ''}, 'done': True, 'done_reason': 'stop'}])
    shown = []
    value = client.agent_json([{'role': 'user', 'content': 'x'}], {'type': 'object'}, on_reply=shown.append)
    assert value['do'] == ['открой браузер', 'найди погоду в Москве'] and shown == [] and len(sent) == 1


@pytest.mark.parametrize('ending', [[], [{'done': True, 'done_reason': 'length'}]])
def test_ta04_stream_closed_without_terminal_or_cut_runs_nothing(ending):
    raw = json.dumps({'do': ['удали файл отчёт.docx']}, ensure_ascii=False)
    client, _ = client_for(pieces(raw) + ending)
    with pytest.raises(oc.IncompleteModelOutput):
        client.agent_json([{'role': 'user', 'content': 'x'}], {'type': 'object'}, on_reply=lambda _d: None)


def test_ta04_non_stream_answer_cut_by_token_limit_is_incomplete():
    client = oc.OllamaClient(Config())
    client.resolve_agent_model = lambda: 'planner:1b'
    client._runner_resource_options = lambda: {}
    body = {'message': {'content': '{"do": ["открой браузер"]}'}, 'done': True, 'done_reason': 'length'}
    client._session = SimpleNamespace(post=lambda url, **kw: SimpleNamespace(status_code=200, text='', json=lambda: body,
                                                                            raise_for_status=lambda: None))
    with pytest.raises(oc.IncompleteModelOutput):
        client.agent_json([{'role': 'user', 'content': 'x'}], {'type': 'object'})


@pytest.mark.parametrize('content', ['{"do":["открой браузер"],"do":["удали файл"]}',
                                     '{"do":["открой браузер"],"\\u0064o":["удали файл"]}'])
def test_ta04_j1_conflicting_names_are_ambiguous_not_silently_chosen(content):
    client, _ = client_for(pieces(content) + [{'done': True}])
    with pytest.raises(oc.OllamaError) as failure:
        client.agent_json([{'role': 'user', 'content': 'x'}], {'type': 'object'}, on_reply=lambda _d: None)
    assert 'ambiguous' in str(failure.value)


def test_ta04_j1_control_escaped_name_alone_has_the_same_meaning():
    plain, _ = client_for(pieces('{"do":["открой браузер"]}') + [{'done': True}])
    escaped, _ = client_for(pieces('{"\\u0064o":["открой браузер"]}') + [{'done': True}])
    a = plain.agent_json([{'role': 'user', 'content': 'x'}], {}, on_reply=lambda _d: None)
    b = escaped.agent_json([{'role': 'user', 'content': 'x'}], {}, on_reply=lambda _d: None)
    assert a['do'] == b['do'] == ['открой браузер']


@pytest.mark.parametrize('value', [{'do': None}, {'do': 'открой браузер'}, {'do': ['открой браузер', 5]},
                                   {'do': ['a', None]}, {'do': [['nested']]}, {'reply': None}, {'ask': 7},
                                   {'reply': 'ok', 'do': None}])
def test_ta04_v1_null_wrong_type_never_reach_the_executor(value):
    assert parse(value).kind == 'invalid'


def test_ta04_v1_missing_field_keeps_its_own_meaning():
    assert parse({}).kind == 'reply' and parse({}).text == ''
    assert parse({'do': []}).kind == 'reply'
    assert parse({'ask': 'Какой сайт?'}).kind == 'ask'


def test_ta04_v2_exact_bounds():
    assert parse({'do': [f'шаг {i}' for i in range(MAX_STEPS)]}).kind == 'do'
    assert parse({'do': [f'шаг {i}' for i in range(MAX_STEPS + 1)]}).kind == 'invalid'
    assert parse({'do': ['x' * MAX_COMMAND_CHARS]}).commands == ['x' * MAX_COMMAND_CHARS]
    assert parse({'do': ['x' * (MAX_COMMAND_CHARS + 1)]}).kind == 'invalid'      # never silently truncated


def test_ta03_same_function_for_different_targets_stays_separate():
    d = parse({'do': ['открой папку Загрузки', 'открой папку Документы', 'открой папку Загрузки']})
    assert d.commands == ['открой папку Загрузки', 'открой папку Документы', 'открой папку Загрузки']


def test_decide_runs_nothing_for_incomplete_output_and_keeps_model_available():
    agent_llm170.reset_availability()

    class Ollama:
        def agent_json(self, messages, schema, on_reply=None):
            raise oc.IncompleteModelOutput('cut')
    agent = SimpleNamespace(config=Config(), ollama=Ollama(), database=None, tools=None, _conversation_key='desktop')
    assert agent_llm170.decide(agent, 'открой браузер', 'ru') is None
    assert agent_llm170._UNAVAILABLE_UNTIL[0] == 0.0

    class Bad:
        def agent_json(self, messages, schema, on_reply=None):
            return {'do': ['открой браузер', 5], '_model': 'm'}
    agent.ollama = Bad()
    assert agent_llm170.decide(agent, 'открой браузер', 'ru') is None


def _image_stream(records):
    client, _ = client_for(records, width=7)
    return client.chat_stream([{'role': 'user', 'content': 'x', 'images': ['aW5lcnQ=']}], on_content=lambda _d: None)


def test_ta07_repeated_delivery_of_one_call_is_not_a_second_call():
    call = {'id': 'call_1', 'function': {'name': 'noop', 'arguments': {'p': 1}}}
    value = _image_stream([{'message': {'content': 'a', 'tool_calls': [call]}}, {'message': {'content': 'b', 'tool_calls': [call]}},
                           {'done': True}])
    assert len(value['message']['tool_calls']) == 1


def test_ta07_new_call_with_the_same_text_is_kept():
    call = {'function': {'name': 'noop', 'arguments': {'p': 1}}}
    value = _image_stream([{'message': {'content': 'a', 'tool_calls': [call]}}, {'message': {'content': 'b', 'tool_calls': [dict(call)]}},
                           {'done': True}])
    assert len(value['message']['tool_calls']) == 2


def test_ta07_records_after_terminal_and_unterminated_calls_are_not_handed_over():
    call = {'id': 'c', 'function': {'name': 'noop', 'arguments': {}}}
    after = _image_stream([{'message': {'content': 'a'}}, {'done': True}, {'message': {'content': 'late', 'tool_calls': [call]}}])
    assert 'tool_calls' not in after['message'] and after['message']['content'] == 'a'
    cut = _image_stream([{'message': {'content': 'a', 'tool_calls': [call]}}])
    assert 'tool_calls' not in cut['message'] and cut['incomplete_tool_calls'] == 1


def test_ta08_ordinary_complete_object_path_still_works():
    raw = json.dumps({'reply': 'Привет!'}, ensure_ascii=False)
    client, _ = client_for(pieces(raw) + [{'done': True}])
    shown = []
    value = client.agent_json([{'role': 'user', 'content': 'x'}], {}, on_reply=shown.append)
    assert value['reply'] == 'Привет!' and ''.join(shown) == 'Привет!'
