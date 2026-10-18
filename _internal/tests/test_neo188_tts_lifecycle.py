"""NEO 188 NEO-TTS-001 / TZ-TTS-UTTERANCE-LIFECYCLE-01 (fake helper process, no audio device)."""
import queue
import threading
import time

import pytest

from jarvis.response_voice.service import VoiceService
from jarvis.response_voice.system_store import SystemVoiceStore


class Config:
    def __init__(self, root):
        self.paths = type('P', (), {'root': root})()
        self.data = {'response_voice': {'tier': 'light', 'enabled': True}}

    def get(self, key, default=None):
        node = self.data
        for part in key.split('.'):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def wait(predicate, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setattr(SystemVoiceStore, 'verification_error', lambda self: None)
    services = []

    def build(script, gate=None):
        class Fake:
            def __init__(self, *a):
                self.events = queue.Queue()
            def send(self, request):
                def feed():
                    for state in script(request):
                        if gate is not None and state == 'GATE':
                            gate.wait(5)
                            continue
                        self.events.put({'state': state})
                threading.Thread(target=feed, daemon=True).start()
            def receive(self, timeout):
                return self.events.get(timeout=timeout)
            def close(self): pass
            def reap(self): pass
        service = VoiceService(Config(tmp_path))
        service._process_factory = Fake
        service._runtime_check = lambda root: True
        outcomes = []
        service.add_terminal_listener(lambda uid, outcome: outcomes.append((uid, outcome)))
        services.append(service)
        return service, outcomes
    yield build
    for service in services:
        service.close()


def test_tts_l01_synthesis_without_playback_is_not_completed(make):
    service, outcomes = make(lambda r: ('synthesizing', 'done'))
    uid = service.submit('Привет', 'ru')
    assert wait(lambda: outcomes)
    assert outcomes == [(uid, 'finished_without_playback')]


def test_full_playback_completes_exactly_once(make):
    service, outcomes = make(lambda r: ('synthesizing', 'playing', 'done', 'done'))
    uid = service.submit('Привет', 'ru')
    assert wait(lambda: outcomes) and time.sleep(0.2) is None
    assert outcomes == [(uid, 'completed')]


def test_tts_l02_queued_answer_cancelled_before_start_is_never_played(make):
    gate = threading.Event()
    service, outcomes = make(lambda r: ('synthesizing', 'GATE', 'playing', 'done'), gate)
    first = service.submit('Первый', 'ru')
    assert wait(lambda: (service.utterance_status(first) or {}).get('status') == 'synthesizing')
    second = service.submit('Второй', 'ru')
    service.cancel()
    gate.set()
    assert wait(lambda: len(outcomes) == 2)
    assert dict(outcomes) == {first: 'cancelled_before_start', second: 'cancelled_before_start'}
    third = service.submit('Третий', 'ru')                     # a new, independent utterance still works
    assert third and service.utterance_status(third)['status'] in {'queued', 'synthesizing', 'playing', 'completed'}


def test_tts_l03_stop_while_speaking_is_interrupted_and_late_events_do_not_finish_a_new_one(make):
    gate = threading.Event()
    service, outcomes = make(lambda r: ('synthesizing', 'playing', 'GATE', 'done'), gate)
    first = service.submit('Длинная речь', 'ru')
    assert wait(lambda: (service.utterance_status(first) or {}).get('status') == 'playing')
    service.cancel()
    gate.set()
    assert wait(lambda: outcomes)
    time.sleep(0.2)
    assert outcomes == [(first, 'interrupted')]


def test_tts_l05_failure_before_and_after_start_are_distinct(make):
    service, outcomes = make(lambda r: ('synthesizing', 'synthesis_failed'))
    uid = service.submit('A', 'ru')
    assert wait(lambda: outcomes) and outcomes == [(uid, 'failed_before_start')]
    service2, outcomes2 = make(lambda r: ('synthesizing', 'playing', 'process_exited'))
    uid2 = service2.submit('B', 'ru')
    assert wait(lambda: outcomes2) and outcomes2 == [(uid2, 'failed_after_start')]


def test_tts_l07_volume_zero_submits_nothing(make):
    service, outcomes = make(lambda r: ('playing', 'done'))
    service.config.data['response_voice']['volume'] = 0
    assert not service.submit('Тихо', 'ru') and outcomes == []
