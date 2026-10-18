"""NEO 172: the answer is shown and spoken while it is generated; «запомнить?»; the wake word interrupts speech."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis.ollama_client import OllamaClient, ReplyStream


@pytest.mark.parametrize('value', ['Привет! Как дела?', 'Кавычки "внутри" и \\ слэш', 'Строка\nвторая\tтаб', 'Эмодзи 😀 и ё', ''])
def test_reply_stream_decodes_the_reply_piece_by_piece(value):
    raw = json.dumps({'reply': value}, ensure_ascii=True)
    for size in (1, 2, 5, 13):
        stream = ReplyStream()
        assert ''.join(stream.feed(raw[i:i + size]) for i in range(0, len(raw), size)) == value.lstrip()


def test_reply_stream_shows_nothing_of_commands():
    raw = json.dumps({'do': ['открой браузер']}, ensure_ascii=False)
    stream = ReplyStream()
    assert ''.join(stream.feed(raw[i:i + 3]) for i in range(0, len(raw), 3)) == ''
    ask = ReplyStream()
    assert ask.feed('{"ask": "Какой сайт') == 'Какой сайт' and ask.feed(' открыть?"}') == ' открыть?'


class Config:
    def __init__(self, **values):
        self.values = {'llm_agent.enabled': True, 'ollama.base_url': 'http://127.0.0.1:11434', **values}

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeResponse:
    def __init__(self, lines, status=200):
        self.lines, self.status_code, self.encoding, self.text = lines, status, None, ''

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self, chunk_size=64, decode_unicode=True):
        yield from self.lines

    def json(self):
        return {'message': {'content': ''.join(json.loads(l).get('message', {}).get('content', '') for l in self.lines)}}


class FakeSession:
    def __init__(self, pieces):
        self.pieces, self.payloads = pieces, []

    def post(self, url, json=None, timeout=None, stream=False):
        import json as _json
        self.payloads.append(dict(json))
        lines = [_json.dumps({'message': {'content': p}, 'done': False}) for p in self.pieces] + [_json.dumps({'done': True})]
        return FakeResponse(lines)


def _client(pieces, **config):
    client = OllamaClient(Config(**config))
    client._session = FakeSession(pieces)
    client.resolve_agent_model = lambda: 'neo-agent:latest'
    client.resolve_chat_model = lambda: 'neo-agent:latest'
    client.agent_model_candidates = lambda: ['neo-agent', 'qwen2.5:1.5b']
    client._runner_resource_options = lambda: {'num_thread': 4}
    return client


def test_agent_json_streams_the_reply_and_still_returns_the_whole_object():
    raw = json.dumps({'reply': 'Коты спят до 16 часов. Это нормально.'}, ensure_ascii=False)
    client = _client([raw[i:i + 4] for i in range(0, len(raw), 4)])
    shown = []
    value = client.agent_json([{'role': 'user', 'content': 'x'}], {'type': 'object'}, on_reply=shown.append)
    assert ''.join(shown) == 'Коты спят до 16 часов. Это нормально.' and len(shown) > 3
    assert value['reply'] == 'Коты спят до 16 часов. Это нормально.' and value['_model'] == 'neo-agent:latest'
    assert client._session.payloads[0]['stream'] is True


def test_chat_uses_the_agent_context_so_ollama_does_not_reload_the_model():
    client = _client(['Привет', '!'], **{'local_chat.enabled': True, 'local_chat.num_ctx': 4096, 'llm_agent.num_ctx': 3072,
                                        'llm_agent.keep_alive': '15m', 'local_chat.keep_alive': '10m'})
    client.local_chat_enabled = lambda: True
    shown = []
    assert client.chat_text([{'role': 'user', 'content': 'привет'}], on_delta=shown.append) == 'Привет!'
    payload = client._session.payloads[0]
    assert shown == ['Привет', '!'] and payload['options']['num_ctx'] == 3072 and payload['keep_alive'] == '15m'
    other = _client(['ok'], **{'local_chat.num_ctx': 4096})
    other.local_chat_enabled = lambda: True
    other.resolve_chat_model = lambda: 'qwen3.5:4b'
    other.chat_text([{'role': 'user', 'content': 'x'}])
    assert other._session.payloads[0]['options']['num_ctx'] == 4096      # a different model keeps its own settings


class StreamingOllama:
    """The agent model streams a conversational reply."""

    def __init__(self, reply):
        self.reply = reply

    def local_chat_enabled(self):
        return True

    def agent_json(self, messages, schema, *, on_reply=None):
        raw = json.dumps({'reply': self.reply}, ensure_ascii=False)
        stream = ReplyStream()
        for i in range(0, len(raw), 5):
            piece = stream.feed(raw[i:i + 5])
            if piece and on_reply:
                on_reply(piece)
        return {'reply': self.reply, '_model': 'neo-agent'}

    def chat_text(self, messages, *, on_delta=None, **_kw):
        # Conversation uses the selected chat role, not the planner's JSON reply.
        if on_delta:
            for i in range(0, len(self.reply), 5):
                on_delta(self.reply[i:i + 5])
        return self.reply


def _session(tmp_path, ollama):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.runtime import agent_llm170, local_chat169
    session = AtlasSession(tmp_path / 'run')
    session.reset_case('ru')
    agent_llm170.reset_availability()
    local_chat169.reset_availability()
    session.agent.ollama = ollama
    return session


def test_the_answer_arrives_in_pieces_and_is_not_repeated(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = _session(tmp_path, StreamingOllama('Коты много спят, потому что охотятся ночью. Это их природа.'))
        # 186: a cause question searches by default; the streaming contract is for the local choice.
        session.config.data.setdefault('local_chat', {})['information_questions'] = 'local'
        try:
            pieces = []
            turn = session.agent.handle_stream('почему коты столько спят днём', on_text=pieces.append)
            assert turn.text == 'Коты много спят, потому что охотятся ночью. Это их природа.'
            assert len(pieces) > 3 and ''.join(pieces) == turn.text              # streamed, each character once
        finally:
            session.finish()


def test_statement_about_me_gets_the_answer_then_an_offer_to_remember(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    from jarvis.runtime import memory169
    with external_io_blocked():
        session = _session(tmp_path, StreamingOllama('Здорово! Фотошоп — мощный инструмент.'))
        try:
            pieces = []
            turn = session.agent.handle_stream('я работаю в фотошопе', on_text=pieces.append)
            assert turn.text == 'Здорово! Фотошоп — мощный инструмент.\n\nЗапомнить: «я работаю в фотошопе»?'
            assert ''.join(pieces) == turn.text
            saved = session.agent.handle('да')
            assert saved.tool_details == ['memory.offer_saved']
            assert any('я работаю в фотошопе' in fact for fact in memory169.facts_for_prompt(session.agent.database, session.agent.config))
            session.agent.handle('я живу в Казани')
            assert session.agent.handle('нет').text == 'Хорошо, не запоминаю.'
            assert 'Запомнить' not in session.agent.handle('я живу в Казани').text      # not asked again
        finally:
            session.finish()


def test_no_offer_for_passing_states_questions_or_when_switched_off(tmp_path):
    from jarvis.runtime import everyday172 as ev
    config = type('C', (), {'get': lambda self, k, d=None: {'memory.propose': True, 'memory.enabled': True}.get(k, d)})()
    assert ev.about_me('я работаю в фотошопе', config) == 'я работаю в фотошопе'
    assert ev.about_me('I work at a bakery', config) == 'I work at a bakery'
    for text in ('у меня сегодня болит голова', 'я работаю в фотошопе, как сделать слой прозрачным?', 'открой фотошоп'):
        assert ev.about_me(text, config) == ''
    off = type('C', (), {'get': lambda self, k, d=None: {'memory.propose': False}.get(k, d)})()
    assert ev.about_me('я работаю в фотошопе', off) == ''


def test_remember_that_saves_right_away(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    from jarvis.runtime import memory169
    with external_io_blocked():
        session = _session(tmp_path, None)
        try:
            turn = session.agent.handle('запомни, что я люблю тёмную тему')
            assert turn.tool_details == ['memory.remember_that']
            assert any('я люблю тёмную тему' in fact for fact in memory169.facts_for_prompt(session.agent.database, session.agent.config))
        finally:
            session.finish()


def test_offer_is_dropped_when_the_turn_was_a_command(tmp_path):
    from jarvis.runtime import everyday172 as ev
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = _session(tmp_path, None)
        try:
            session.agent._memory_offer172 = ''
            ev.set_offer('я работаю в фотошопе')
            session.agent._memory_offer172 = 'Запомнить: «я работаю в фотошопе»?'
            session.agent.handle('включи музыку')
            assert ev._offer_pending() == ''                   # a later «да» saves nothing
        finally:
            session.finish()


def test_voice_stop_is_checked_first_and_nothing_is_spoken():
    # NEO 179: answers are text only — there is no speech to talk over; «стоп» still interrupts a running answer.
    source = (Path(__file__).resolve().parents[1] / 'jarvis' / 'ui' / 'main_window.py').read_text(encoding='utf-8')
    start = source.index('def _wake_transcribed')
    block = source[start:source.index('if generation != self._wake_generation:', start)]
    assert '_output_stop_from_voice' in block
    assert 'wake-over-speech' not in source and '_speech_active' not in source and '_say(' not in source
