"""Actual Agent → GameWeb → SearchAPI HTTP boundary regressions (no real account)."""
from __future__ import annotations

import time

import pytest
import requests

from jarvis.game_web.searchapi import SearchAPIGoogleProvider, SEARCHAPI_SEARCH_URL
from jarvis.secrets import protect_secret
from tests.test_game_web import FakeHTTPResponse, FakeHTTPSession


def response(answer='В игре 30 уровней.'):
    return FakeHTTPResponse(200, {'answer_box': {'answer': answer, 'link': 'https://example.test/official-game'},
        'search_metadata': {'status': 'Success'}})


@pytest.fixture
def chain(tmp_path, monkeypatch):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'search')
        config = session.config
        config.data['gaming']['web_knowledge'].update(enabled=True, suggest_followup=False,
            searchapi_api_key_encrypted=protect_secret('fixture-key'))
        monkeypatch.setattr('jarvis.game_web.service.active_game_context', lambda **_: None)
        monkeypatch.setattr('jarvis.game_web.service.game_presence_service', lambda *_: type('Presence', (), {'snapshot': lambda *_a, **_k: None})())
        yield session
        session.finish()


def use_http(chain, *responses, error=None):
    http = FakeHTTPSession(responses, exc=error)
    chain.agent.game_web.searcher.close()
    chain.agent.game_web.searcher = SearchAPIGoogleProvider(chain.config, http)
    return http


def active(monkeypatch, name='Warhammer 40,000: Darktide', game_id='darktide'):
    monkeypatch.setattr('jarvis.game_web.service.active_game_context',
        lambda **_: {'game_name': name, 'game_id': game_id})


def test_real_agent_active_game_question_reaches_http_and_returns_answer(chain, monkeypatch):
    active(monkeypatch)
    http = use_http(chain, response())
    turn = chain.agent.handle('сколько уровней в игре')
    assert '30 уровней' in turn.text
    assert turn.tool_details == ['game_web.darktide.google_ai_search']
    assert not chain.calls
    assert len(http.calls) == 1
    endpoint, request = http.calls[0]
    assert endpoint == SEARCHAPI_SEARCH_URL
    assert 'Darktide' in request['params']['q'] and 'сколько уровней' in request['params']['q']
    assert request['params']['engine'] == 'google'
    assert request['headers'] == {'Authorization': 'Bearer fixture-key'}
    assert 'api_key' not in request['params']
    assert request['timeout'][0] <= 4 and request['timeout'][1] <= 60


def test_unknown_active_game_asks_then_reuses_original_question(chain):
    http = use_http(chain, response())
    first = chain.agent.handle('сколько уровней в игре')
    assert 'О какой игре' in first.text
    assert not http.calls
    turn = chain.agent.handle('Darktide')
    assert '30 уровней' in turn.text
    assert len(http.calls) == 1
    query = http.calls[0][1]['params']['q']
    assert 'Darktide' in query and 'сколько уровней' in query
    assert not chain.calls


def test_pending_unknown_title_does_not_hijack_local_command(chain):
    http = use_http(chain)
    chain.agent.handle('сколько уровней в игре')
    turn = chain.agent.handle('открой папку Downloads')
    assert any(row['tool'] == 'desktop' and row['arguments'] == {'action':'open','query':'downloads','hint':'folder'} for row in chain.calls)
    assert not http.calls and not chain.agent.game_web._missing_game[0]
    assert 'игре' not in turn.text


def test_game_title_clarification_expires_and_is_scoped(chain):
    http = use_http(chain)
    chain.agent.handle('сколько уровней в игре')
    service = chain.agent.game_web
    service._missing_game = (service._missing_game[0], time.monotonic() - 1)
    assert service.resume_pending_followup('Darktide') == ''
    chain.agent.handle('сколько уровней в игре')
    service.begin_turn('telegram:other-user', 'ru', 'Darktide')
    assert service.resume_pending_followup('Darktide') == ''
    assert not http.calls


@pytest.mark.parametrize('status, expected', [(401, 'отклонил авторизацию'), (429, 'ограничил запросы'), (402, 'доступные запросы'), (503, 'сбой сети')])
def test_real_agent_http_failure_is_specific_and_retry_can_succeed(chain, status, expected):
    http = use_http(chain, FakeHTTPResponse(status, {'error': 'fixture rejection'}), response('Найден ответ после восстановления.'))
    turn = chain.agent.handle('сколько уровней в игре Darktide')
    assert expected in turn.text
    assert 'безопасная обработка' not in turn.text
    assert len(http.calls) == 1, 'Do not retry billable searches invisibly'
    next_turn = chain.agent.handle('сколько уровней в игре Darktide')
    assert 'после восстановления' in next_turn.text
    assert len(http.calls) == 2


@pytest.mark.parametrize('exception, expected', [(requests.Timeout(), 'не успел ответить'), (requests.ConnectionError(), 'сбой сети')])
def test_real_agent_network_failure_recovers_without_restarting(chain, exception, expected):
    http = use_http(chain, error=exception)
    turn = chain.agent.handle('сколько уровней в игре Darktide')
    assert expected in turn.text
    assert 'API key' not in turn.text
    http.exc = None
    http.responses.append(response('Информация снова доступна.'))
    assert 'снова доступна' in chain.agent.handle('сколько уровней в игре Darktide').text


def test_key_changes_are_read_live_and_auth_failure_is_not_cached(chain):
    http = use_http(chain, FakeHTTPResponse(401, {}), response())
    assert 'отклонил авторизацию' in chain.agent.handle('сколько уровней в игре Darktide').text
    chain.config.data['gaming']['web_knowledge']['searchapi_api_key_encrypted'] = protect_secret('new-fixture-key')
    assert '30 уровней' in chain.agent.handle('сколько уровней в игре Darktide').text
    assert http.calls[-1][1]['headers']['Authorization'] == 'Bearer new-fixture-key'


def test_settings_refresh_closes_old_transport(chain):
    http = use_http(chain)
    old = chain.agent.game_web
    chain.agent.refresh_runtime_settings()
    assert http.closed and chain.agent.game_web is not old
    assert chain.agent.game_web.searcher.api_key() == 'fixture-key'


def test_disabled_search_reports_toggle_without_claiming_missing_key(chain):
    chain.config.data['gaming']['web_knowledge']['enabled'] = False
    http = use_http(chain)
    for question in ('сколько уровней в игре Darktide', 'загугли что такое DLSS'):
        turn = chain.agent.handle(question)
        assert 'выключен' in turn.text
        assert 'API key' not in turn.text and 'безопасная обработка' not in turn.text
    assert not http.calls


@pytest.mark.parametrize('question', ['покажи очередь треков', 'дальше', 'сколько свободной памяти', 'открой папку Downloads'])
def test_active_game_never_steals_local_functions(chain, monkeypatch, question):
    active(monkeypatch)
    http = use_http(chain)
    chain.agent.handle(question)
    assert not http.calls


@pytest.mark.parametrize('timeout', [None, 'invalid', float('nan'), float('inf'), 999, -1])
def test_bad_timeout_setting_still_has_a_bounded_http_request(chain, timeout):
    chain.config.data['gaming']['web_knowledge']['searchapi_timeout_seconds'] = timeout
    http = use_http(chain, response())
    assert '30 уровней' in chain.agent.handle('сколько уровней в игре Darktide').text
    assert 6 <= http.calls[0][1]['timeout'][1] <= 60


def test_general_software_question_is_not_mistaken_for_missing_game(chain):
    # This is a web-boundary test; select web policy explicitly.
    chain.config.data.setdefault('local_chat', {})['questions'] = 'web'
    http = use_http(chain, response('Классы объединяют данные и методы.'))
    turn = chain.agent.handle('что такое классы в Python')
    assert 'объединяют' in turn.text and len(http.calls) == 1


@pytest.mark.parametrize('code, question', [
    ('en', 'How many levels are in the game?'), ('de', 'Wie viele Level gibt es im Spiel?'),
    ('fr', 'Combien de niveaux y a-t-il dans le jeu ?'), ('es', '¿Cuántos niveles hay en el juego?'),
    ('it', 'Quanti livelli ci sono nel gioco?'), ('pt', 'Quantos níveis existem no jogo?')])
def test_game_scope_in_supported_languages_reaches_real_http(chain, monkeypatch, code, question):
    chain.config.data['app']['language'] = code
    active(monkeypatch)
    http = use_http(chain, response('Fixture answer with 30 levels.'))
    turn = chain.agent.handle(question)
    assert 'Fixture answer' in turn.text
    assert len(http.calls) == 1 and 'Darktide' in http.calls[0][1]['params']['q']
    assert http.calls[0][1]['params']['hl'] == code


def test_chat_reset_discards_unanswered_game_question(chain):
    http = use_http(chain)
    chain.agent.handle('сколько уровней в игре')
    chain.agent.reset()
    assert not chain.agent.game_web._missing_game[0]
    assert chain.agent.game_web.resume_pending_followup('Darktide') == ''
    assert not http.calls


def test_verified_active_game_callback_supplies_game_when_tracker_snapshot_is_empty(chain):
    chain.agent.game_web.active_game_provider = lambda: 'darktide'
    http = use_http(chain, response())
    assert '30 уровней' in chain.agent.handle('какой максимальный уровень').text
    assert 'Darktide' in http.calls[0][1]['params']['q']


@pytest.mark.parametrize('code, phrase', [
    ('ru', 'сколько свободной памяти'), ('en', 'how much ram is available'),
    ('de', 'wie viel arbeitsspeicher ist frei'), ('fr', 'combien de mémoire est disponible'),
    ('es', 'cuánta memoria queda libre'), ('it', 'quanta memoria è libera'),
    ('pt', 'quanta memória está livre')])
def test_available_memory_is_local_in_each_supported_language(chain, code, phrase):
    chain.config.data['app']['language'] = code
    http = use_http(chain)
    chain.agent.handle(phrase)
    assert not http.calls
    assert any(call['tool']=='system_monitor' and call['arguments']=={'action':'memory'} for call in chain.calls)


def test_title_question_opens_one_voice_followup_without_media_shortcut(chain):
    from jarvis.runtime.voice_followup156 import pending_question, pending_slot
    http = use_http(chain, response())
    first = chain.agent.handle('сколько уровней в игре', input_source='voice')
    assert first.outcome == 'clarification'
    assert pending_slot(chain.agent, 'ru').name == 'game_title'
    assert pending_slot(chain.agent, 'ru').kind == 'name'
    stamp = pending_question(chain.agent, 'ru')
    assert stamp and pending_question(chain.agent, 'ru') == stamp
    assert pending_question(chain.agent, 'en') is None
    assert pending_question(chain.agent, 'ru', now=stamp[-1]+1) is None
    assert '30 уровней' in chain.agent.handle('Darktide', input_source='voice').text
    assert pending_question(chain.agent, 'ru') is None
    assert len(http.calls) == 1


def test_distinct_cyrillic_titles_never_share_search_cache(chain):
    http = use_http(chain, response('Первый ответ: 30.'), response('Второй ответ: 50.'))
    chain.agent.handle('сколько уровней в игре')
    first = chain.agent.handle('Смута')
    assert 'Первый ответ' in first.text
    chain.agent.reset()
    chain.agent.handle('сколько уровней в игре')
    second = chain.agent.handle('Космические рейнджеры')
    assert 'Второй ответ' in second.text
    assert len(http.calls) == 2
    assert 'Смута' in http.calls[0][1]['params']['q']
    assert 'Космические рейнджеры' in http.calls[1][1]['params']['q']


@pytest.mark.parametrize('phrase', ['привет', 'спасибо', 'мне скучно', 'Джарвис, привет'])
def test_social_reply_abandons_game_title_before_early_return(chain, phrase):
    from jarvis.runtime.voice_followup156 import pending_question
    http = use_http(chain)
    chain.agent.handle('сколько уровней в игре', input_source='voice')
    second = chain.agent.handle(phrase, input_source='voice')
    assert second.text and not http.calls
    assert chain.agent.game_web.pending_game_title('ru') is None
    assert pending_question(chain.agent, 'ru') is None
    assert not chain.agent.game_web._forced.game_id


@pytest.mark.parametrize('phrase', ['стоп', 'отмена', 'не надо'])
def test_bare_cancel_consumes_game_title_without_starting_media_question(chain, phrase):
    from jarvis.runtime.voice_followup156 import pending_question
    http = use_http(chain)
    chain.agent.handle('сколько уровней в игре', input_source='voice')
    turn = chain.agent.handle(phrase, input_source='voice')
    assert turn.outcome == 'cancelled'
    assert not http.calls and not chain.calls
    assert pending_question(chain.agent, 'ru') is None
    assert chain.agent.conversations.get('desktop').pending_intent is None


def test_explicit_media_stop_is_not_consumed_as_game_title_cancel(chain):
    http = use_http(chain)
    chain.agent.handle('сколько уровней в игре')
    chain.agent.handle('стоп музыка')
    assert not http.calls
    assert any(call['tool'] == 'music' and call['arguments']['action'] == 'stop' for call in chain.calls)
    assert chain.agent.game_web.pending_game_title('ru') is None


def test_new_web_question_clears_abandoned_title(chain):
    from jarvis.runtime.voice_followup156 import pending_question
    chain.config.data.setdefault('local_chat', {})['questions'] = 'web'
    http = use_http(chain, response('Python is a programming language.'))
    chain.agent.handle('сколько уровней в игре')
    result = chain.agent.handle('что такое Python')
    assert 'programming' in result.text and len(http.calls) == 1
    assert pending_question(chain.agent, 'ru') is None
    assert http.calls[0][1]['params']['q'] == 'что такое Python'


@pytest.mark.parametrize('foreground, created, expected', [(1002,10,'warframe'), (777,10,''), ('neo',10,'darktide'), ('neo',999,''), ('neo',0,'')])
def test_persisted_game_never_overrides_foreground_or_pid_identity(chain, monkeypatch, foreground, created, expected):
    import os
    from types import SimpleNamespace
    from jarvis.gaming.manager import GameAdapter, GameManager, GamePresenceService, GameProcess
    manager = GameManager()
    games = [
        (GameAdapter('darktide','Darktide',('darktide.exe',)), GameProcess(1001,'darktide.exe','C:/Games/darktide.exe',10)),
        (GameAdapter('warframe','Warframe',('warframe.exe',)), GameProcess(1002,'warframe.exe','C:/Games/warframe.exe',20)),
    ]
    monkeypatch.setattr(manager, 'detect_all', lambda: games)
    presence = GamePresenceService(manager)
    monkeypatch.setattr('jarvis.game_web.service.game_presence_service', lambda *_: presence)
    monkeypatch.setattr('jarvis.gaming.manager._foreground_pid', lambda: os.getpid() if foreground=='neo' else foreground)
    monkeypatch.setattr('psutil.Process', lambda pid: SimpleNamespace(is_running=lambda: True, create_time=lambda: {1001:10,1002:20}[pid]))
    chain.config.data['gaming']['enabled'] = True
    chain.agent.game_web.context_store.read = lambda: {'game_id':'darktide','game_name':'Darktide','pid':1001,'session_started_at':created,'status':'running'}
    result = chain.agent.game_web._runtime_game()
    assert result.get('game_id','') == expected


@pytest.mark.parametrize('question', ['что такое Python', 'что такое классы в Python'])
def test_missing_selected_local_model_never_silently_falls_back_to_searchapi(chain, monkeypatch, question):
    from jarvis.runtime import local_chat169
    chain.config.data.setdefault('local_chat', {})['questions'] = 'local'
    # 186: information questions search by default; this user explicitly chose local answers.
    chain.config.data['local_chat']['information_questions'] = 'local'
    local_chat169.reset_availability()
    monkeypatch.setattr(chain.agent.ollama, '_list_models', lambda force=False: [])
    http = use_http(chain, response('This cloud answer must never be requested.'))
    turn = chain.agent.handle(question)
    assert not http.calls
    assert 'local_chat.no_model' in (turn.tool_details or []) and turn.text
    assert not chain.calls and not turn.pending
