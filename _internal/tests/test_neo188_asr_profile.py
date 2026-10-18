"""NEO 188 NEO-ASR-001 / TZ-ASR-EXPECTED-SLOT-PROFILE-01 (fake transcription service)."""
from types import SimpleNamespace

import numpy as np
import pytest

from jarvis.runtime.asr_profile188 import profile_for
from jarvis.runtime.dialogue_tasks import Slot
from jarvis.voice import RecordingResult, SpeechRecognizer, TranscriptionResult


class Service:
    def __init__(self):
        self.calls = []

    def transcribe_detailed(self, recording, **kw):
        self.calls.append(kw)
        return TranscriptionResult('ok', [])


class Config:
    def get(self, key, default=None):
        return {'voice.command_language': 'ru'}.get(key, default)


def test_asr01_free_text_and_song_title_are_neutral():
    for slot in (Slot('text', 'text', 'text'), Slot('query', 'name', 'query')):
        profile = profile_for(slot, 'ru')
        assert profile['profile'] == 'neutral' and profile['hotwords'] == '' and profile['initial_prompt'] == ''


def test_asr02_duration_and_confirmation_get_their_words():
    assert 'минут' in profile_for(Slot('seconds', 'duration', 'duration'), 'ru')['hotwords']
    assert 'Minuten' in profile_for(Slot('seconds', 'unit188', 'unit188'), 'de')['hotwords']
    assert 'oui' in profile_for(Slot('_confirm', 'confirm', 'confirm_correction'), 'fr')['hotwords']


def test_asr03_ordinary_commands_keep_the_command_profile():
    assert profile_for(None, 'ru') is None
    assert profile_for(Slot('folder', 'path', 'destination'), 'ru') is None


def test_asr04_profile_is_per_request_and_does_not_leak():
    service = Service()
    recognizer = SpeechRecognizer(Config(), service, model_name='small', purpose='command', initial_prompt='CMD', hotwords='open play')
    recognizer._effective_initial_prompt = lambda: 'CMD'
    rec = RecordingResult(np.ones(160, dtype=np.float32), 16000)
    recognizer.transcribe_detailed(rec, profile={'initial_prompt': '', 'hotwords': ''})
    recognizer.transcribe_detailed(rec)
    assert service.calls[0]['hotwords'] == '' and service.calls[0]['initial_prompt'] == ''
    assert service.calls[1]['hotwords'] == 'open play' and service.calls[1]['initial_prompt'] == 'CMD'
    assert recognizer.hotwords == 'open play'


def test_stale_answer_detects_replaced_or_expired_question():
    import time as _t
    from types import SimpleNamespace as NS
    from jarvis.runtime.asr_profile188 import stale_answer, stale_text
    from jarvis.runtime.voice_followup156 import pending_question
    from jarvis.runtime.dialogue_tasks import Slot

    class Pending:
        def __init__(self, ttl):
            self.language, self.expires_at = 'ru', _t.monotonic() + ttl
        def missing(self):
            return Slot('text', 'text', 'text')
    state = NS(pending_intent=Pending(30))
    agent = NS(conversations=NS(get=lambda scope: state), game_web=None)
    asked = pending_question(agent, 'ru')
    assert asked is not None and not stale_answer(asked, agent, 'ru')
    state.pending_intent = Pending(30)                      # replaced by another question
    assert stale_answer(asked, agent, 'ru')
    asked = pending_question(agent, 'ru')
    state.pending_intent = None                             # cancelled
    assert stale_answer(asked, agent, 'ru')
    state.pending_intent = Pending(-1)                      # expired
    assert stale_answer(asked, agent, 'ru')
    assert not stale_answer(None, agent, 'ru')
    assert all(stale_text(c) for c in ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt'))


def test_profiles_are_bounded_and_do_not_touch_shared_recognizer():
    from types import SimpleNamespace as NS
    from jarvis.runtime.asr_profile188 import profile_for
    from jarvis.runtime.dialogue_tasks import Slot
    for code in ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt'):
        for slot in (Slot('text', 'text', 'text'), Slot('title', 'text', 'title'), Slot('duration', 'duration', 'duration'),
                     Slot('confirm', 'confirm', 'confirm'), Slot('application', 'name', 'application')):
            prof = profile_for(slot, code, NS(get=lambda k, d=None: d))
            assert prof is not None and len(prof['hotwords']) <= 400 and prof['initial_prompt'] == ''
    assert profile_for(None, 'ru') is None
