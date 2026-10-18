"""Selected-language wake, real PCM prefix and queue delivery regressions."""
from __future__ import annotations

import queue
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from jarvis.voice import RecordingResult, SpeechRecognizer, _transcription_options


def config(**values):
    settings = {'wake_word.phrase': 'Jarvis', 'voice.command_language': 'ru',
                'voice.language': 'auto', 'app.language': 'ru'}
    settings.update(values)
    return SimpleNamespace(get=lambda key, default=None: settings.get(key, default))


def recording(seconds=2.2):
    return RecordingResult(np.sin(np.arange(int(seconds * 16000), dtype=np.float32) * .1) * .1,
                           16000, correlation_id='utterance', captured_at=10)


@pytest.mark.parametrize('language', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_completed_wake_respects_selected_language_in_actual_requests(language):
    service = Mock()
    service.transcribe.return_value = 'Jarvis'
    service._lock = threading.RLock()
    recognizer = SpeechRecognizer(config(**{'voice.command_language': language}), service, purpose='wake')
    recognizer.transcribe(recording())
    request = service.transcribe.call_args.kwargs
    assert request['language'] == language
    options = _transcription_options(request['purpose'], request['language'])
    assert options['language'] == language and options['multilingual'] is False
    assert options['beam_size'] == 2
    assert recognizer._wake_retry_languages() == (('en',) if language != 'en' else ())
    recognizer.transcribe_fast_wake_probe(recording(.7))
    assert service.transcribe.call_args.kwargs['language'] == 'en'


@pytest.mark.parametrize('values,expected,retries', [
    ({'voice.command_language': 'auto'}, 'auto', ('en',)),
    ({'voice.command_language': 'auto', 'wake_word.phrase': 'Джарвис'}, 'auto', ('ru',)),
    ({'voice.language': 'de'}, 'de', ('en',)),
    ({'wake_word.phrase': 'Джарвис'}, 'ru', ()),
    ({'voice.command_language': 'en', 'wake_word.phrase': 'Джарвис'}, 'en', ('ru',)),
    ({'wake_word.fast_probe_language': 'pt'}, 'ru', ('pt',)),
])
def test_explicit_language_auto_and_custom_wake_preserved(values, expected, retries):
    recognizer = SpeechRecognizer(config(**values), Mock(), purpose='wake')
    assert recognizer._wake_language() == expected
    assert recognizer._wake_retry_languages() == retries


def window(transcripts):
    from jarvis.ui.main_window import MainWindow
    cfg = config()
    service = Mock()
    service._lock = threading.RLock()
    service.transcribe = Mock(side_effect=transcripts)
    rec = SpeechRecognizer(cfg, service, purpose='wake')
    w = SimpleNamespace(config=cfg, wake_recognizer=rec, _last_wake_reject_log_at=0,
                        _acoustic_wake_enabled_for_current_phrase=lambda: False,
                        _response_active=lambda: False, _strict_stop_phrase=lambda value: False,
                        _voice_unlock_phrase_matches=lambda value: False,
                        _wakeless_quick_media_allowed=lambda value: False,
                        _language_code=lambda: 'ru')
    w._wake_hypothesis_needs_verification = lambda text, recording: MainWindow._wake_hypothesis_needs_verification(w, text, recording)
    return MainWindow, w, service


def test_completed_long_wake_uses_one_selected_decode_preserving_tail():
    MainWindow, w, service = window(['Джарвис, включи музыку'])
    result = MainWindow._transcribe_wake_robust(w, recording())
    assert result['matched'] is True and result['source'] == 'whisper-ru'
    assert 'включи музыку' in result['remainder']
    assert service.transcribe.call_count == 1
    assert service.transcribe.call_args.kwargs['language'] == 'ru'


def test_complete_short_fast_match_does_not_wait_for_robust_decode():
    MainWindow, w, service = window(['Джарвис'])
    result = MainWindow._transcribe_wake_robust(w, recording(.74))
    assert result['matched'] is True and result['source'] == 'whisper-fast-complete'
    assert service.transcribe.call_count == 1
    assert service.transcribe.call_args.kwargs['purpose'] == 'wake_fast'


def test_short_partial_failure_uses_selected_language_not_auto():
    MainWindow, w, service = window(['', 'Джарвис'])
    result = MainWindow._transcribe_wake_robust(w, recording(.74))
    assert result['matched'] is True
    assert [call.kwargs['language'] for call in service.transcribe.call_args_list] == ['ru', 'ru']
    assert [call.kwargs['purpose'] for call in service.transcribe.call_args_list] == ['wake_fast', 'wake']


@pytest.mark.parametrize('text', ['как там погода', 'включи музыку', 'service', 'сервис', 'спасибо', 'garrows', 'Garris', 'garbage', 'garden'])
def test_unrelated_speech_does_not_trigger_language_retry_or_wake(text):
    MainWindow, w, service = window([text])
    result = MainWindow._transcribe_wake_robust(w, recording())
    assert result['matched'] is False
    assert service.transcribe.call_count == 1


def test_rejected_log_distinguishes_disabled_detector_and_language(caplog):
    MainWindow, w, _ = window(['доброе утро'])
    with caplog.at_level('INFO'):
        MainWindow._transcribe_wake_robust(w, recording())
    assert 'acoustic_enabled=False' in caplog.text
    assert 'language=ru' in caplog.text and 'model=tiny' in caplog.text
    assert 'decodes=1' in caplog.text and 'correlation=utterance' in caplog.text


def test_probe_timing_survives_real_ui_mailbox_dispatch():
    from jarvis.ui.main_window import MainWindow
    payload = {'text': 'Jarvis', 'decode_ms': 320.0, 'started_at': 20.0, 'decoded_at': 20.32}
    rec = recording(.7)
    w = SimpleNamespace(_wake_result_queue=queue.Queue(), _fast_wake_probe_transcribed=Mock())
    w._wake_result_queue.put(('probe_result', payload, 4, rec))
    MainWindow._poll_wake_results(w)
    w._fast_wake_probe_transcribed.assert_called_once_with(payload, 4, rec)


def test_late_second_probe_keeps_actual_first_phoneme_pcm(monkeypatch):
    """Run production capture with real samples; old trailing crop lost the marker."""
    from jarvis import audio_backend as backend
    commands, events = queue.Queue(), []
    now = [100.0]
    commands.put({'cmd': 'start', 'mode': 'wake', 'session': 1, 'target_rate': 16000,
                  'wake_silence': .12, 'wake_probe_enabled': True,
                  'wake_probe_interval': .9, 'wake_probe_min_voice_seconds': .15,
                  'wake_probe_window_seconds': .72, 'wake_probe_max_per_utterance': 2})

    class Stream:
        def read(self, size):
            now[0] = round(now[0] + .02, 8)
            elapsed = now[0] - 100
            if elapsed > 2.5:
                commands.put({'cmd': 'shutdown'})
            # A slow 1.1-second name has distinct first and final phonemes.
            amplitude = (.38 if .20 <= elapsed <= .36 else
                         (.29 if 1.05 <= elapsed <= 1.30 else
                          (.10 if .36 < elapsed <= 2.0 else 0)))
            data = np.sin((np.arange(320) / 16000 + elapsed) * 2 * np.pi * 310) * amplitude
            return data.astype(np.float32).reshape(-1, 1), False
        def abort(self): pass
        def close(self): pass

    class Events:
        def put(self, value, **kwargs): events.append(value)
        put_nowait = put

    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace())
    monkeypatch.setattr(backend, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=lambda _: None))
    monkeypatch.setattr(backend, '_resolve_input_device', lambda *a, **kw: (0, 'fixture', 'token', 16000, 'fake'))
    monkeypatch.setattr(backend, '_open_stream', lambda *a: (Stream(), 16000, 320, 'float32'))
    backend._audio_worker_main(commands, Events())
    assert not [event for event in events if event['type'] == 'error']
    probes = [event for event in events if event['type'] == 'wake_probe']
    assert len(probes) == 2
    assert probes[1]['captured_at'] - probes[0]['captured_at'] >= .89
    for probe in probes:
        samples = np.frombuffer(probe['audio'], np.float32)
        assert 0 < samples.size <= int(16000 * 1.40)
        assert (np.abs(samples) > .30).sum() >= 500, 'the leading phoneme was cropped'
        assert probe['capture_started_at'] <= 100.2
    first = np.frombuffer(probes[0]['audio'], np.float32)
    second = np.frombuffer(probes[1]['audio'], np.float32)
    assert second.size > first.size
    # The final phoneme was unavailable to the early probe, but must reach the
    # later one together with the name's first consonants.
    tail = second[int((101.05 - probes[1]['capture_started_at']) * 16000):]
    assert (np.abs(tail) > .25).sum() > 800
    complete = next(event for event in events if event['type'] == 'wake_chunk')
    assert len(complete['audio']) > len(probes[-1]['audio'])
    assert complete['correlation_id'] == probes[-1]['correlation_id']


@pytest.mark.parametrize('phrase', ['Jarvis', 'Джарвис'])
def test_hard_g_guard_preserves_real_rendering_and_explicit_exception(phrase):
    from jarvis.wake_matcher import match_wake_phrase
    from jarvis.wake.v2 import WakeConfig, WakeMatcher
    assert match_wake_phrase('Гарвис', phrase)[0]
    assert not match_wake_phrase('garrows', phrase)[0]
    matcher = WakeMatcher(WakeConfig(phrase=phrase, exceptions=('garrows',)))
    assert matcher.match('garrows').accepted


@pytest.mark.parametrize('phrase', ['Garris', 'garrows'])
def test_user_selected_custom_wake_phrase_remains_exact(phrase):
    from jarvis.wake_matcher import match_wake_phrase
    assert match_wake_phrase(phrase, phrase)[0]


@pytest.mark.parametrize('pause', [0.0, .5, 4.5])
def test_grown_slow_wake_probe_preserves_continuous_or_paused_command(monkeypatch, pause):
    from tests.test_neo158_voice_capture import replay
    events, _, _, command_end = replay(monkeypatch, pause=pause, wake_end=1.3,
                                       captured=1.38, delay=.4, has_tail=False)
    finals = [event for event in events if event['type'] == 'finished']
    assert len(finals) == 1
    assert finals[0]['captured_at'] >= 100 + command_end
    samples = np.frombuffer(finals[0]['audio'], np.float32)
    assert (np.abs(samples) > .12).sum() > 4800
