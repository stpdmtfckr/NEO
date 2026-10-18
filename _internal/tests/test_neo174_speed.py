"""NEO 174: requests with typos without the agent model, faster command decoding, warm-up."""
import base64
import io
import json
import re
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

from jarvis.runtime import speed174
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        value = AtlasSession(tmp_path_factory.mktemp('speed174') / 'run')
        value.model_calls = []
        value.agent._llm_agent170 = lambda text, code, on_text, multi=False: (value.model_calls.append(text), None)[1]
        yield value
        value.finish()


def handle(session, text):
    session.reset_case('ru')
    session.model_calls.clear()
    turn = session.agent.handle(text)
    call = session.calls[0] if session.calls else None
    return turn, (call['tool'] + '.' + str(call['arguments'].get('action', ''))) if call else ''


@pytest.mark.parametrize('text,expected', [
    ('сделй скриншот', 'screen.capture'), ('открйо калькулятор', 'application.open'),
    ('вклюи музыку', 'music.play_music'), ('выклчи звук', 'audio.mute'),
])
def test_a_command_with_a_typo_never_waits_for_the_agent_model(session, text, expected):
    turn, route = handle(session, text)
    assert route == expected and session.model_calls == [], (turn.tool_details, session.model_calls)


@pytest.mark.parametrize('text', ['как у тебя дела', 'расскжи анекдот'])
def test_conversation_uses_selected_chat_without_an_action_planner(session, text, monkeypatch):
    from jarvis.runtime import local_chat169
    chat_calls = []
    monkeypatch.setattr(local_chat169, 'answer', lambda *a, **kw: chat_calls.append(a[2]) or 'QA selected chat')
    turn, route = handle(session, text)
    assert route == '' and chat_calls and not session.model_calls, turn.tool_details
    assert turn.text == 'QA selected chat'


def test_the_early_repair_can_be_turned_off(session):
    session.agent.config.set('routing.typo_before_model', False) if hasattr(session.agent.config, 'set') else \
        session.agent.config.data.setdefault('routing', {}).__setitem__('typo_before_model', False)
    try:
        turn, route = handle(session, 'сделй скриншот')
        assert session.model_calls == ['сделй скриншот']
    finally:
        session.agent.config.data['routing']['typo_before_model'] = True


def test_a_repaired_turn_never_ends_empty(session):
    agent = session.agent
    original = agent._typo_candidate
    agent._typo_candidate = lambda text, source, raw='': text + ' ещё'       # keeps "repairing" forever
    try:
        turn, _ = handle(session, 'фывапролд')
        assert turn.text.strip()
    finally:
        agent._typo_candidate = original


def test_regex_cache_is_large_enough_for_neo():
    assert speed174.enlarge_regex_cache() >= 4096 and re._MAXCACHE >= 4096


def test_warm_up_only_parses_and_makes_the_first_typo_fast(tmp_path):
    with external_io_blocked():
        s = AtlasSession(tmp_path / 'run')
        try:
            timings = speed174.warm_up(s.agent, 'ru')
            assert {'atlas.exact', 'apps', 'typos', 'neo_model'} <= set(timings)
            assert s.calls == []                                   # nothing was executed
            s.agent._llm_agent170 = lambda *a, **k: None
            s.reset_case('ru')
            started = time.perf_counter()
            s.agent.handle('сделй скриншот')
            assert s.calls and (time.perf_counter() - started) < 1.5
        finally:
            s.finish()


def test_command_decoding_uses_more_cores_but_never_fewer(monkeypatch):
    from jarvis import voice
    import psutil
    monkeypatch.setattr(psutil, 'cpu_count', lambda logical=True: 8 if not logical else 16)
    assert voice.auto_command_threads() == 6
    monkeypatch.setattr(psutil, 'cpu_count', lambda logical=True: 2 if not logical else 4)
    assert voice.auto_command_threads() == 3
    monkeypatch.setattr(psutil, 'cpu_count', lambda logical=True: 32 if not logical else 64)
    assert voice.auto_command_threads() == 8


def _run_worker(monkeypatch, request, logprob=-1.3):
    from jarvis import stt_worker
    calls = []

    class Whisper:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            calls.append(kwargs)
            return [SimpleNamespace(text='выключи книжная полка', avg_logprob=logprob, no_speech_prob=.01)], None
    audio = (np.sin(np.linspace(0, 900, 16000 * 3)) * 0.2).astype(np.float32)
    row = {'id': '1', 'audio_b64': base64.b64encode(audio.tobytes()).decode(), 'sample_rate': 16000, 'model_name': 'small',
           'device': 'cpu', 'compute_type': 'int8', 'purpose': 'command', 'language': 'ru', **request}
    output = io.StringIO()
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=Whisper))
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(row)))
    monkeypatch.setattr(sys, 'stdout', output)
    assert stt_worker.main() == 0
    reply = json.loads(output.getvalue().splitlines()[-1])
    assert reply['ok'] and reply['text']
    return calls


def test_no_repeated_decode_that_would_give_the_same_text(monkeypatch):
    calls = _run_worker(monkeypatch, {'noise_rejection_percent': 55})
    assert len(calls) == 1 and calls[0]['beam_size'] == 7          # quality of the one decode unchanged


def test_a_really_different_rescue_decode_still_runs(monkeypatch):
    with_vad = _run_worker(monkeypatch, {'noise_rejection_percent': 85})
    assert len(with_vad) >= 2 and with_vad[0]['vad_filter'] and not with_vad[1]['vad_filter']
    biased = _run_worker(monkeypatch, {'noise_rejection_percent': 55, 'hotwords': 'Книжная полка'})
    assert len(biased) == 2 and 'hotwords' in biased[0] and 'hotwords' not in biased[1]


def test_a_confident_decode_is_never_repeated(monkeypatch):
    assert len(_run_worker(monkeypatch, {'noise_rejection_percent': 85, 'hotwords': 'x'}, logprob=-0.1)) == 1


def test_settings_174_migration_adds_keys_only():
    from jarvis.runtime import settings174
    data = {'voice': {'stt_cpu_threads': 2}, 'settings_version': 173}
    settings174.migrate(data, 173)
    assert data['voice'] == {'stt_cpu_threads': 2, 'stt_threads_auto': False}      # off since 175 (CPU)
    assert data['routing']['typo_before_model'] is True and data['settings_version'] == 174


def test_speech_recognition_uses_the_configured_threads_again():
    from jarvis.runtime import settings175
    from jarvis.voice import SpeechRecognizer
    data = {'voice': {'stt_cpu_threads': 3, 'stt_threads_auto': True}, 'settings_version': 174}
    settings175.migrate(data, 174)
    assert data['voice']['stt_threads_auto'] is False and data['settings_version'] == 175
    recognizer = SpeechRecognizer.__new__(SpeechRecognizer)
    recognizer.config = type('C', (), {'get': lambda self, key, default=None: data['voice'].get(key.split('.', 1)[1], default)
                                       if key.startswith('voice.') else default})()
    recognizer.purpose = 'command'
    assert recognizer._cpu_threads() == 3


def test_yandex_music_reuses_one_connection_with_the_same_error_handling(monkeypatch):
    pytest.importorskip('yandex_music')
    from jarvis.integrations import yandex_pool174
    from jarvis.runtime import http_pool173
    from yandex_music.exceptions import NetworkError

    class Response:
        def __init__(self, code, body=b'{"result": 1}'):
            self.status_code, self.content = code, body

    class Session:
        def __init__(self):
            import requests
            self.cookies = requests.cookies.RequestsCookieJar()
            self.calls = []

        def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            if kwargs.get('url', args[1] if len(args) > 1 else '').endswith('/fail'):
                import requests
                raise requests.ConnectionError('down')
            return Response(200)
    shared = Session()
    monkeypatch.setattr(http_pool173, 'session', lambda service, **k: shared)
    request = yandex_pool174.make_request(None, timeout=3)
    request.client = None                                                  # normally set by yandex_music.Client
    assert request.get('https://api.music.yandex.net/account/status') == 1
    assert request.get('https://api.music.yandex.net/rotor/station/user:onyourwave/tracks') == 1
    assert len(shared.calls) == 2                                          # both through the one session
    assert shared.calls[0][1]['headers']['User-Agent'] and shared.calls[0][1]['timeout'] == 3
    with pytest.raises(NetworkError):
        request.get('https://api.music.yandex.net/fail')
    assert getattr(shared, '_neo_no_cookies', False)                       # no cookies kept, as before


def test_pooled_yandex_can_be_turned_off():
    pytest.importorskip('yandex_music')
    from jarvis.integrations import yandex_pool174
    from yandex_music.utils.request import Request
    class Off:
        def get(self, key, default=None):
            return False if key == 'performance.pooled_yandex' else default
    off = yandex_pool174.make_request(Off(), timeout=3)
    assert type(off) is Request
