"""NEO 173: faster services (shared connections, warm-up, short caches, timings) and the NEO model lifecycle."""
from __future__ import annotations

import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from jarvis.runtime import http_pool173 as pool
from jarvis.runtime import model_life173 as life

ROOT = Path(__file__).resolve().parents[1]


def cfg(**values):
    return SimpleNamespace(get=lambda k, d=None: values.get(k, d), data=values)


def test_one_session_per_service_for_the_whole_process():
    assert pool.session('gmail') is pool.session('gmail') and pool.session('gmail') is not pool.session('calendar')
    old = pool.session('weather')
    assert pool.reset('weather') is not old and pool.session('weather') is not old


def test_gmail_and_calendar_clients_share_the_connection(monkeypatch):
    from jarvis.integrations.google_calendar import GoogleCalendarClient
    from jarvis.integrations.mail_services import GmailClient
    config = cfg()
    assert GmailClient(config)._session is GmailClient(config)._session is pool.session('gmail')
    assert GoogleCalendarClient(config)._session is GoogleCalendarClient(config)._session is pool.session('calendar')


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.content = body, status, json.dumps(body).encode()

    def json(self):
        return self.body


class Session:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return Response({'messages': [{'id': str(len(self.calls))}]})


def test_gmail_reads_are_reused_for_seconds_and_any_change_clears_them(monkeypatch):
    from jarvis.integrations import mail_services
    from jarvis.integrations.mail_services import GmailClient
    mail_services._GMAIL_READS.clear()
    client = GmailClient(cfg())
    client._session = Session()
    monkeypatch.setattr(pool, 'session', lambda service: client._session)     # the shared NEO session
    client.token = lambda: 'token'
    first = client._request('GET', '/users/me/messages', params={'q': 'is:unread'})
    again = client._request('GET', '/users/me/messages', params={'q': 'is:unread'})
    assert first == again and len(client._session.calls) == 1
    client._request('GET', '/users/me/messages', params={'q': 'newer_than:1d'})
    assert len(client._session.calls) == 2                                  # another question is asked
    client._request('POST', '/users/me/messages/1/modify')                  # mark read
    client._request('GET', '/users/me/messages', params={'q': 'is:unread'})
    assert len(client._session.calls) == 4                                  # the change cleared the memory
    assert [r['stage'] for r in pool.timings('gmail')][-3:] == ['GET', 'POST', 'GET']


def test_a_status_check_with_its_own_session_is_never_answered_from_memory():
    from jarvis.integrations import mail_services
    from jarvis.integrations.mail_services import GmailClient
    mail_services._GMAIL_READS.clear()
    client = GmailClient(cfg())
    client._session = Session()
    client.token = lambda: 'token'
    client._request('GET', '/users/me/profile')
    client._request('GET', '/users/me/profile')
    assert len(client._session.calls) == 2


def test_calendar_events_are_reused_and_a_new_event_clears_them():
    from jarvis.integrations import google_calendar
    from jarvis.integrations.google_calendar import GoogleCalendarClient
    google_calendar._CALENDAR_READS.clear()
    client = GoogleCalendarClient(cfg(**{'integrations.google_calendar.enabled': True}))
    calls = []
    client._request_uncached = lambda method, path, params=None, json_body=None: (calls.append(method), {'items': [len(calls)]})[1]
    client._oauth = lambda: ('id', 'secret', 'refresh-token-xyz', 'access', 0.0)
    assert client._request('GET', '/calendars/primary/events', params={'maxResults': 5}) == {'items': [1]}
    assert client._request('GET', '/calendars/primary/events', params={'maxResults': 5}) == {'items': [1]}
    client._request('POST', '/calendars/primary/events', json_body={'summary': 'x'})
    assert client._request('GET', '/calendars/primary/events', params={'maxResults': 5}) == {'items': [3]}


def test_response_cache_expires():
    cache = pool.ResponseCache(ttl=0.05)
    cache.put('k', 1)
    assert cache.get('k') == 1
    time.sleep(0.08)
    assert cache.get('k') is None


def test_warm_up_opens_only_enabled_services_once_a_minute_without_any_request():
    pool._WARMED.clear()
    config = cfg(**{'gaming.web_knowledge.enabled': True, 'gaming.web_knowledge.searchapi_api_key_encrypted': 'dpapi:x',
                    'integrations.gmail.enabled': True, 'integrations.gmail.refresh_token_encrypted': 'dpapi:y',
                    'integrations.spotify.enabled': False})
    opened = []
    scheduled = pool.prewarm(config, opener=lambda session, host: opened.append(host) or True, background=False)
    assert opened == ['https://www.searchapi.io', 'https://gmail.googleapis.com', 'https://oauth2.googleapis.com']
    assert [s for s, _h in scheduled] == ['searchapi', 'gmail', 'gmail']
    assert pool.prewarm(config, opener=lambda s, h: opened.append(h), background=False) == []      # not again within a minute
    assert pool.prewarm(cfg(**{'performance.prewarm_connections': False, 'integrations.spotify.enabled': True}), background=False) == []
    pool._WARMED.clear()


def test_warm_up_opens_a_tls_connection_and_sends_nothing():
    """The opener only connects a pooled connection: no HTTP request is written."""
    events = []

    class Conn:
        sock = None

        def connect(self):
            events.append('connect')

    class Pool:
        def _get_conn(self, timeout=None):
            return Conn()

        def _put_conn(self, conn):
            events.append('returned')

    class Adapter:
        poolmanager = SimpleNamespace(connection_from_url=lambda url: Pool())
    session = SimpleNamespace(get_adapter=lambda url: Adapter())
    assert pool._open_connection(session, 'https://www.searchapi.io')
    assert events == ['connect', 'returned']


def test_searchapi_uses_the_warm_session_and_records_its_time(monkeypatch):
    from jarvis.game_web.searchapi import SearchAPIGoogleProvider
    provider = SearchAPIGoogleProvider(cfg())
    assert provider.session is pool.session('searchapi')
    provider.close()
    assert pool.session('searchapi') is provider.session                   # closing a provider keeps the shared session


class Ollama:
    def __init__(self, loaded=(), **config):
        self.config = cfg(**{'ollama.base_url': 'http://127.0.0.1:11434', 'llm_agent.keep_alive': '5m', **config})
        self.posts, self.loaded = [], set(loaded)
        outer = self

        class S:
            def get(self, url, timeout=None):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'models': [{'name': m} for m in outer.loaded]})

            def post(self, url, json=None, timeout=None):
                outer.posts.append(json)
                if json.get('keep_alive') == 0:
                    outer.loaded.discard(json['model'])
                else:
                    outer.loaded.add(json['model'])
                return SimpleNamespace(ok=True, raise_for_status=lambda: None, close=lambda: None)
        self._session = S()

    def resolve_agent_model(self):
        return 'neo-agent:latest'

    def agent_model_candidates(self):
        return ['neo-agent', 'qwen2.5:1.5b']


def test_wake_word_loads_the_model_only_when_it_is_not_in_memory():
    life._LAST_PRELOAD[0] = 0.0
    ollama = Ollama()
    assert life.preload(ollama, background=False, now=1000.0)
    assert ollama.posts == [{'model': 'neo-agent:latest', 'keep_alive': '5m'}]
    assert not life.preload(ollama, background=False, now=1005.0)                  # not again within 20 s
    loaded = Ollama(loaded={'neo-agent:latest'})
    assert life.preload(loaded, background=False, now=2000.0) and loaded.posts == []      # already loaded: nothing sent
    off = Ollama(**{'performance.preload_on_wake': False})
    assert not life.preload(off, background=False, now=3000.0)


def test_release_unloads_the_neo_model_too():
    ollama = Ollama(loaded={'neo-agent:latest', 'qwen3.5:4b', 'llama3:8b'}, **{'ollama.vision_model': 'qwen3.5:4b'})
    assert life.release(ollama) == 2
    assert ollama.loaded == {'llama3:8b'}                                          # other programs' models stay


def test_game_start_frees_the_memory_unless_switched_off(monkeypatch):
    started = []
    monkeypatch.setattr(life.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=lambda: started.append(target)))
    life.register(Ollama())
    assert life.release_for_game() and len(started) == 1
    life.register(Ollama(**{'performance.unload_models_in_games': False}))
    assert not life.release_for_game()
    source = (ROOT / 'jarvis' / 'gaming' / 'memory.py').read_text(encoding='utf-8')
    assert 'model_life173.release_for_game()' in source


def test_settings_173_shorter_keep_alive_for_the_default_only():
    from jarvis.runtime import settings173
    data = {'llm_agent': {'keep_alive': '15m'}, 'local_chat': {'keep_alive': '10m'}, 'settings_version': 172}
    settings173.migrate(data, 172)
    assert data['llm_agent']['keep_alive'] == '5m' and data['local_chat']['keep_alive'] == '5m'
    assert data['performance'] == {'prewarm_connections': True, 'preload_on_wake': True, 'unload_models_in_games': True}
    assert data['context'] == {'enabled': True, 'minutes': 10} and data['settings_version'] >= 173
    custom = {'llm_agent': {'keep_alive': '30m'}, 'settings_version': 172}
    settings173.migrate(custom, 172)
    assert custom['llm_agent']['keep_alive'] == '30m'                               # the user's own choice stays
    defaults = json.loads((ROOT / 'config' / 'default_settings.json').read_text(encoding='utf-8'))
    assert defaults['settings_version'] >= 173 and defaults['llm_agent']['keep_alive'] == '5m'


def test_wake_triggers_warm_up_in_the_main_window():
    source = (ROOT / 'jarvis' / 'ui' / 'main_window.py').read_text(encoding='utf-8')
    assert source.count('self._wake_heard173()') >= 2          # wake word, acoustic probe (+ typing, NEO 174)
    block = source[source.index('def _wake_heard173'):source.index('def _weekly_learning_report172')]
    assert 'http_pool173.prewarm(self.config)' in block and 'model_life173.preload(ollama)' in block


def test_status_says_searching_for_web_questions():
    pytest.importorskip('PySide6')
    from jarvis.ui.main_window import MainWindow
    assert MainWindow._intent_animation('сколько стоит iphone 15') == ('searching', 'Ищу в интернете…')
    assert MainWindow._intent_animation('сколько непрочитанных писем') == ('searching', 'Спрашиваю сервис…')
    assert MainWindow._intent_animation('как дела') == ('thinking', 'Думаю…')


def test_dpapi_values_are_decrypted_once(monkeypatch):
    from jarvis import secrets
    calls = []
    monkeypatch.setattr(secrets, '_unprotect', lambda value: (calls.append(value), 'plain')[1])
    secrets._UNPROTECTED.clear()
    assert secrets.unprotect_secret('dpapi:AAAA') == 'plain' and secrets.unprotect_secret('dpapi:AAAA') == 'plain'
    assert calls == ['dpapi:AAAA']
    secrets._UNPROTECTED.clear()
