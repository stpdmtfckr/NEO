"""No real HTTP, microphone, screenshots or user documents in these tests."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest
import requests

from jarvis.game_web.searchapi import SearchAPIGoogleProvider
from jarvis.game_web.cache import GameWebCache
from jarvis.integrations.local_translation import (
    SearchAPITranslationAdapter, _language_code, looks_like_translation_request,
    parse_translation_request,
)
from jarvis.ollama_client import OllamaClient, OllamaError
from jarvis.runtime.screenshot_storage import screenshot_path, safe_application_name


class Config:
    def __init__(self, tmp_path=None, **values):
        self.values = values
        self.paths = SimpleNamespace(cache=tmp_path or Path('.'))

    def get(self, key, default=None):
        return self.values.get(key, default)


def provider(monkeypatch, payload, *, status=200):
    monkeypatch.setenv('SEARCHAPI_API_KEY', 'test-key-not-real')
    session = Mock()
    response = Mock(status_code=status, content=b'{}', text='')
    response.json.return_value = payload
    session.get.return_value = response
    return SearchAPIGoogleProvider(Config(), session=session), session


def test_information_uses_raw_google_never_generated_answer(monkeypatch):
    p, session = provider(monkeypatch, {
        'markdown': 'invented generated answer', 'ai_overview': {'text': 'invented'},
        'text_blocks': [{'answer': 'invented'}],
        'organic_results': [{'title': 'Primary source', 'link': 'https://example.org/source', 'snippet': 'Actual excerpt.'}],
    })
    result = p.search('question')
    assert session.get.call_args.kwargs['params']['engine'] == 'google'
    assert result.direct_answer == 'Actual excerpt.'
    assert result.results[0].url == 'https://example.org/source'
    assert result.direct_answer_kind == 'source_excerpts'
    assert 'api_key' not in session.get.call_args.kwargs['params']
    assert session.get.call_args.kwargs['headers']['Authorization'] == 'Bearer test-key-not-real'


def test_answer_card_provenance_and_unsafe_links(monkeypatch):
    p, _ = provider(monkeypatch, {
        'answer_box': {'answer': '42', 'link': 'https://example.org/answer'},
        'organic_results': [{'title': 'Bad', 'link': 'javascript:alert(1)', 'snippet': 'bad'}],
    })
    result = p.search('question')
    assert result.direct_answer == '42'
    assert [r.url for r in result.results] == ['https://example.org/answer']


def test_search_errors_never_expose_key(monkeypatch):
    p, _ = provider(monkeypatch, {'error': 'bad test-key-not-real'}, status=401)
    result = p.search('question')
    assert result.error == 'searchapi_auth_error'
    assert 'test-key-not-real' not in json.dumps(result.attempts)


def test_offline_clear_no_local_fallback(monkeypatch):
    p, session = provider(monkeypatch, {})
    session.get.side_effect = requests.ConnectionError('offline')
    result = p.search('question')
    assert result.error == 'searchapi_network_error:ConnectionError'
    assert not result.direct_answer
    assert session.get.call_count == 1


@pytest.mark.parametrize('language,command,payload,target,source', [
    ('ru', 'переведи Hello world на русский', 'Hello world', 'ru', ''),
    ('ru', 'переведи с английского на немецкий Hello', 'Hello', 'de', 'en'),
    ('en', 'translate hello world from English to German', 'hello world', 'de', 'en'),
    ('en', 'how do you say good morning in Spanish', 'good morning', 'es', ''),
    ('de', 'übersetze guten Morgen ins Englische', 'guten Morgen', 'en', ''),
    ('de', 'wie sagt man guten Tag auf Spanisch', 'guten Tag', 'es', ''),
    ('fr', 'traduis bonjour en anglais', 'bonjour', 'en', ''),
    ('fr', 'comment dit-on bonjour en allemand', 'bonjour', 'de', ''),
    ('es', 'traduce hola al inglés', 'hola', 'en', ''),
    ('es', 'cómo se dice buenos días en alemán', 'buenos días', 'de', ''),
    ('it', 'traduci buongiorno in inglese', 'buongiorno', 'en', ''),
    ('it', 'come si dice buongiorno in tedesco', 'buongiorno', 'de', ''),
    ('pt', 'traduz bom dia para inglês', 'bom dia', 'en', ''),
    ('pt', 'como se diz bom dia em espanhol', 'bom dia', 'es', ''),
    ('ru', 'переведи «открой музыку» на английский', 'открой музыку', 'en', ''),
])
def test_translation_language_spaces(language, command, payload, target, source):
    assert looks_like_translation_request(command)
    assert parse_translation_request(command, ui_language=language) == (payload, target, source)


def test_language_alias_does_not_match_substring():
    assert _language_code('French') == 'fr'  # 'en' inside French was previously English
    assert _language_code('Portuguese') == 'pt'
    assert not looks_like_translation_request('перейди на следующую вкладку')


def test_translation_only_remote_generated_lane(monkeypatch):
    p, session = provider(monkeypatch, {'text_blocks': [{'type': 'paragraph', 'answer': 'Guten Morgen'}]})
    adapter = SearchAPITranslationAdapter(Config())
    adapter.provider = p
    translated, meta = adapter.translate('good morning', 'de', 'en')
    assert translated == 'Guten Morgen'
    assert meta['generated_translation'] is True
    assert meta['source'] == 'en' and meta['target'] == 'de'
    params = session.get.call_args.kwargs['params']
    assert params['engine'] == 'google_ai_mode'
    assert 'Translate the text' in params['q']
    assert 'images' not in params and 'url' not in params
    assert session.get.call_count == 1


def test_missing_translation_not_substituted_by_snippet(monkeypatch):
    p, _ = provider(monkeypatch, {'organic_results': [{'snippet': 'not a translation'}]})
    adapter = SearchAPITranslationAdapter(Config())
    adapter.provider = p
    value, meta = adapter.translate('good morning', 'de')
    assert not value and meta['error'] == 'translation_unavailable'


@pytest.mark.parametrize('legacy_enabled', [True, False])
def test_ollama_rejects_text_before_any_http(legacy_enabled):
    client = OllamaClient(Config(**{'ollama.requests_enabled': legacy_enabled}))
    client._session = Mock()
    with pytest.raises(OllamaError):
        client.chat([{'role': 'user', 'content': 'tell me a joke'}])
    with pytest.raises(OllamaError):
        client.chat_stream([{'role': 'user', 'content': 'tell me a joke'}])
    with pytest.raises(OllamaError):
        client.generate_text('hello')
    client.warmup()
    client._session.get.assert_not_called()
    client._session.post.assert_not_called()


def test_ollama_vision_gate_and_unload():
    client = OllamaClient(Config(**{'ollama.vision_enabled': True, 'ollama.requests_enabled': False}))
    client.resolve_model = Mock(return_value='vision-model')
    client._session = Mock()
    client._session.post.return_value.json.return_value = {'message': {'content': 'Screen description'}}
    result = client.chat([{'role': 'user', 'content': 'Describe', 'images': ['aW1hZ2U=']}], num_predict_override=512)
    assert result['message']['content'] == 'Screen description'
    assert client._session.post.call_args.kwargs['json']['keep_alive'] == 0
    with pytest.raises(OllamaError):
        client.chat([{'role': 'user', 'images': ['aW1hZ2U=']}], tools=[{'name': 'execute'}])


def test_vision_disabled_never_contacts_server():
    client = OllamaClient(Config(**{'ollama.vision_enabled': False}))
    client._session = Mock()
    with pytest.raises(OllamaError):
        client.chat([{'role': 'user', 'images': ['aW1hZ2U=']}])
    client._session.post.assert_not_called()


def test_screenshot_never_sent_to_remote_ollama():
    client = OllamaClient(Config(**{'ollama.base_url': 'https://example.org'}))
    client._session = Mock()
    with pytest.raises(OllamaError):
        client.chat([{'role': 'user', 'images': ['aW1hZ2U=']}])
    client._session.post.assert_not_called()


def test_quoted_languages_are_payload_not_target():
    assert parse_translation_request('переведи «go to English class» на русский') == ('go to English class', 'ru', '')


def test_hidden_server_starts_cli_without_desktop(monkeypatch):
    from jarvis import ollama_runtime
    monkeypatch.setattr(ollama_runtime, '_LAST_ATTEMPT', 0)
    monkeypatch.setattr(ollama_runtime, 'probe_ollama', lambda *a, **kw: (False, ''))
    monkeypatch.setattr(ollama_runtime, 'find_ollama_executable', lambda: Path('/fake/ollama'))
    monkeypatch.setattr(ollama_runtime, '_wait_for_api', lambda *a, **kw: (True, 'test'))
    popen = Mock()
    monkeypatch.setattr(ollama_runtime.subprocess, 'Popen', popen)
    result = ollama_runtime.ensure_ollama_running(prefer_tray_app=True)
    assert result['mode'] == 'serve_hidden'
    assert Path(popen.call_args.args[0][0]) == Path('/fake/ollama')
    assert popen.call_args.args[0][1:] == ['serve']
    assert popen.call_args.kwargs['env']['OLLAMA_KEEP_ALIVE'] == '0'


def test_screenshot_folder_and_unique_names(tmp_path):
    first = screenshot_path(tmp_path, 'Game.exe')
    second = screenshot_path(tmp_path, 'Game.exe')
    assert first.parent == tmp_path / 'Game'
    assert first != second
    assert safe_application_name('../CON.exe') == 'app_CON'
    assert safe_application_name('a:b?c.exe') == 'a_b_c'


def test_web_cache_languages_and_non_fuzzy_queries(tmp_path):
    config = Config(tmp_path, **{'app.language': 'ru'})
    cache = GameWebCache(config)
    cache.put('game', 'Game', 'item 12 location', search_query='item 12 location', answer='12 location', sources=[], provider='searchapi_google')
    assert cache.get('game', 'item 12 location')
    assert cache.get('game', 'item 13 location') is None
    config.values['app.language'] = 'es'
    assert cache.get('game', 'item 12 location') is None
