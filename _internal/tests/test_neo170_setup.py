"""NEO 170: installing the agent model through the Ollama HTTP API (against a local fake Ollama server)."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

import pytest


class FakeOllama:
    def __init__(self, *, old_create_api=False, bad_json=False, pull_error=''):
        self.models = []
        self.old_create_api, self.bad_json, self.pull_error = old_create_api, bad_json, pull_error
        self.requests = []


def _server(state: FakeOllama):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, *, lines=None):
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            if lines is not None:
                for line in lines:
                    self.wfile.write((json.dumps(line) + '\n').encode())
            else:
                self.wfile.write(json.dumps(body).encode())

        def do_GET(self):
            if self.path == '/api/tags':
                self._send(200, {'models': [{'name': m} for m in state.models]})
            else:
                self._send(404, {'error': 'not found'})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            state.requests.append((self.path, body))
            if self.path == '/api/pull':
                if state.pull_error:
                    self._send(200, None, lines=[{'status': 'pulling manifest'}, {'error': state.pull_error}])
                    return
                state.models.append(body['model'])
                self._send(200, None, lines=[{'status': 'pulling manifest'}, {'status': 'x', 'total': 100, 'completed': 50},
                                             {'status': 'x', 'total': 100, 'completed': 100}, {'status': 'success'}])
            elif self.path == '/api/create':
                if state.old_create_api and 'modelfile' not in body:
                    self._send(400, {'error': 'unknown field "from"'})
                    return
                name = body.get('model') or body.get('name')
                state.models.append(name + ':latest')
                self._send(200, {'status': 'success'})
            elif self.path == '/api/chat':
                content = 'not json' if state.bad_json else json.dumps({'do': ['open the first search result for cats']})
                self._send(200, {'message': {'role': 'assistant', 'content': content}})
            else:
                self._send(404, {'error': 'not found'})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture()
def ollama(monkeypatch):
    servers = []

    def make(**kw):
        state = FakeOllama(**kw)
        server = _server(state)
        servers.append(server)
        config = SimpleNamespace(get=lambda k, d=None: f'http://127.0.0.1:{server.server_port}' if k == 'ollama.base_url' else d)
        return state, config

    import jarvis.ollama_runtime as runtime
    monkeypatch.setattr(runtime, 'ensure_ollama_running', lambda *a, **k: {'ready': True})
    yield make
    for server in servers:
        server.shutdown()


def test_pull_create_and_check(ollama):
    from jarvis.neo_llm.setup import ensure
    state, config = ollama()
    lines = []
    assert ensure('1.5b', config=config, write_settings=False, log=lines.append) == 0
    assert 'qwen2.5:1.5b' in state.models and 'neo-agent:latest' in state.models
    paths = [p for p, _ in state.requests]
    assert paths == ['/api/pull', '/api/create', '/api/chat']
    create = state.requests[1][1]
    assert create['from'] == 'qwen2.5:1.5b' and create['model'] == 'neo-agent' and 'NEO' in create['system']
    chat = state.requests[2][1]
    assert chat['format']['properties'].keys() >= {'do', 'reply', 'ask'} and chat['model'] == 'neo-agent'
    assert any('100%' in line for line in lines) and any('answers in the NEO format' in line for line in lines)


def test_second_run_does_not_download_again(ollama):
    from jarvis.neo_llm.setup import ensure
    state, config = ollama()
    state.models.append('qwen2.5:0.5b')
    assert ensure('0.5b', config=config, write_settings=False, log=lambda _l: None) == 0
    assert [p for p, _ in state.requests] == ['/api/create', '/api/chat']
    assert state.requests[0][1]['from'] == 'qwen2.5:0.5b'


def test_older_ollama_gets_a_modelfile(ollama):
    from jarvis.neo_llm.setup import ensure
    state, config = ollama(old_create_api=True)
    assert ensure('1.5b', config=config, write_settings=False, log=lambda _l: None) == 0
    bodies = [b for p, b in state.requests if p == '/api/create']
    assert len(bodies) == 2 and bodies[1]['modelfile'].startswith('FROM qwen2.5:1.5b\nSYSTEM """')
    assert 'PARAMETER temperature 0.2' in bodies[1]['modelfile']


def test_failures_have_their_own_exit_codes(ollama):
    from jarvis.neo_llm.setup import ensure
    _state, config = ollama(pull_error='disk full')
    assert ensure('1.5b', config=config, write_settings=False, log=lambda _l: None) == 3
    _state, config = ollama(bad_json=True)
    assert ensure('1.5b', config=config, write_settings=False, log=lambda _l: None) == 5
    unreachable = SimpleNamespace(get=lambda k, d=None: 'http://127.0.0.1:9' if k == 'ollama.base_url' else d)
    assert ensure('1.5b', config=unreachable, write_settings=False, log=lambda _l: None) == 2


def test_check_only(ollama):
    from jarvis.neo_llm.setup import ensure
    state, config = ollama()
    assert ensure(config=config, check_only=True, log=lambda _l: None) == 4
    state.models.append('neo-agent:latest')
    assert ensure(config=config, check_only=True, log=lambda _l: None) == 0
    assert all(p == '/api/tags' for p, _ in state.requests) or state.requests == []
