"""NEO 188 NEO-VOICE-002 / TZ-VOICE-PENDING-SILENCE-RETRY-01 (fake window, no microphone)."""
import time
from types import SimpleNamespace

import pytest

from jarvis.runtime import voice_followup156 as followup
from jarvis.runtime.conversation import ConversationStore
from jarvis.runtime.dialogue_tasks import PendingIntent, Slot


class Window:
    def __init__(self, slot_kind='duration', purpose='clarification', ttl=60):
        self.config = SimpleNamespace(get=lambda key, default=None: {'voice.silence_retry_delay_ms': 1}.get(key, default))
        self.agent = SimpleNamespace(conversations=ConversationStore(), _pending_context=None, game_web=None)
        state = self.agent.conversations.get('desktop')
        state.pending_intent = PendingIntent('timer', {'action': 'timer'}, (Slot('seconds', slot_kind, 'duration'),), 'ru',
                                             time.monotonic() + ttl, purpose=purpose)
        self._clarification_voice_origin156 = True
        self._continuous_voice_session = False
        self._app_mic_muted = False
        self._quitting = False
        self._busy = False
        self._agent_generation = 3
        self.messages, self.captures, self.wake = [], [], []
        question = followup.pending_question(self.agent, 'ru')
        self._clarification_armed156 = (self._agent_generation, question)

    def _language_code(self):
        return 'ru'

    def _clear_listening_bubble(self):
        pass

    def _set_busy(self, *args):
        pass

    def _restore_listening_audio(self):
        pass

    def _append_assistant(self, text, **kw):
        self.messages.append(text)

    def _start_command_capture(self, seamless=False):
        self.captures.append(seamless)

    def _resume_wake_listening(self, delay_ms=0):
        self.wake.append(delay_ms)


@pytest.fixture(autouse=True)
def immediate_timer(monkeypatch):
    import PySide6.QtCore as qtcore
    monkeypatch.setattr(qtcore.QTimer, 'singleShot', staticmethod(lambda ms, fn: fn()))


SILENCE = SimpleNamespace(end_reason='no_speech_timeout')


def test_vsr01_confirmed_silence_repeats_the_same_question_once():
    window = Window()
    assert followup.silence_retry188(window, SILENCE)
    assert window.messages == ['На какое время поставить таймер? Например, на 15 минут.'] and window.captures == [False]
    assert not followup.silence_retry188(window, SILENCE)        # the second silence returns to wake/text
    assert len(window.captures) == 1


@pytest.mark.parametrize('reason', ['stopped', '', 'endpoint', 'max_duration'])
def test_vsr02_stop_or_unknown_end_never_retries(reason):
    window = Window()
    assert not followup.silence_retry188(window, SimpleNamespace(end_reason=reason))
    assert window.captures == []


@pytest.mark.parametrize('change', ['mute', 'continuous', 'quitting', 'text_origin', 'confirmation_pending', 'other_question'])
def test_vsr03_state_changes_cancel_the_retry(change):
    window = Window()
    if change == 'mute':
        window._app_mic_muted = True
    elif change == 'continuous':
        window._continuous_voice_session = True
    elif change == 'quitting':
        window._quitting = True
    elif change == 'text_origin':
        window._clarification_voice_origin156 = False
    elif change == 'confirmation_pending':
        window.agent._pending_context = {'call': {}}
    else:
        window._clarification_armed156 = (window._agent_generation, ('other',))
    assert not followup.silence_retry188(window, SILENCE) and window.captures == []


@pytest.mark.parametrize('kind,purpose', [('confirm', 'clarification'), ('text', 'clarification'), ('choice', 'neo_model169')])
def test_vsr04_confirmations_and_literal_text_are_excluded(kind, purpose):
    window = Window(slot_kind=kind, purpose=purpose)
    assert not followup.silence_retry188(window, SILENCE)


def test_vsr05_expired_question_is_not_retried_and_ttl_is_not_extended():
    window = Window(ttl=0.01)
    expires = window.agent.conversations.get('desktop').pending_intent.expires_at
    time.sleep(0.02)
    assert not followup.silence_retry188(window, SILENCE)
    assert window.agent.conversations.get('desktop').pending_intent.expires_at == expires


def test_vsr06_task_changed_before_the_microphone_reopens(monkeypatch):
    import PySide6.QtCore as qtcore
    window = Window()
    queued = []
    monkeypatch.setattr(qtcore.QTimer, 'singleShot', staticmethod(lambda ms, fn: queued.append(fn)))
    assert followup.silence_retry188(window, SILENCE)
    window._agent_generation += 1                                # a new task started meanwhile
    queued[0]()
    assert window.captures == [] and window.wake


def test_capture_events_carry_the_end_reason():
    source = open('jarvis/audio_backend.py', encoding='utf-8').read()
    assert 'reason="no_speech_timeout"' in source and 'reason="stopped"' in source
    from jarvis.voice import RecordingResult
    import numpy as np
    assert RecordingResult(np.array([], dtype=np.float32), 16000).end_reason == ''


def test_vsr07_microphone_opens_only_after_the_spoken_question(monkeypatch):
    import PySide6.QtCore as qtcore
    window = Window()
    queued = []
    monkeypatch.setattr(qtcore.QTimer, 'singleShot', staticmethod(lambda ms, fn: queued.append(fn)))
    window._response_voice_service = SimpleNamespace(active=True, echo_guard_active=False)
    assert followup.silence_retry188(window, SILENCE)
    queued.pop(0)()
    assert window.captures == [] and queued                       # still speaking: re-check later
    window._response_voice_service.active = False
    queued.pop(0)()
    assert window.captures == [False]
