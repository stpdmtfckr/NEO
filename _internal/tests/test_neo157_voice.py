"""Real Qt command-worker lifecycles plus owned STT cancellation regressions."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PySide6.QtCore import QObject, QThread, QThreadPool, QRunnable, QTimer
from PySide6.QtWidgets import QApplication

from jarvis.ui.command_asr157 import CommandAsrRunner
from jarvis.ui.main_window import MainWindow
from jarvis.voice import RecordingResult, TranscriptionResult, TranscriptionService, _SttWorkerCrashed
from jarvis.quick_media_commands import QuickMediaCommand


@pytest.fixture(scope='module')
def qt():
    app = QApplication.instance() or QApplication([])
    yield app


def pump(qt, predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        qt.processEvents()
        time.sleep(.003)
    assert predicate()


class BlockingPoolJob(QRunnable):
    def __init__(self, started, release):
        super().__init__()
        self.started, self.release = started, release
    def run(self):
        self.started.set()
        self.release.wait(3)


def test_command_runs_with_network_pool_saturated_and_delivery_on_qt(qt):
    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    started, release = threading.Event(), threading.Event()
    pool.start(BlockingPoolJob(started, release))
    assert started.wait(1)
    runner = CommandAsrRunner()
    replies, errors, callback_threads, worker_threads = [], [], [], []
    def work(cancelled):
        worker_threads.append(threading.get_ident())
        return 'привет'
    def result(value):
        replies.append(value)
        callback_threads.append(QThread.currentThread())
    try:
        assert runner.start(work, result=result, error=errors.append, timeout=1, abort=lambda:None)
        pump(qt, lambda:bool(replies))
        assert replies == ['привет'] and not errors
        assert callback_threads == [qt.thread()]
        assert worker_threads != [threading.get_ident()]
        assert not release.is_set()
    finally:
        release.set();pool.waitForDone(1000);runner.cancel()


def test_timeout_cleans_up_once_and_late_result_cannot_execute(qt):
    runner = CommandAsrRunner()
    release = threading.Event()
    replies, errors = [], []
    abort = Mock()
    assert runner.start(lambda cancelled: (release.wait(2), 'late')[1], result=replies.append,
                        error=errors.append, timeout=.05, abort=abort)
    pump(qt, lambda:bool(errors))
    assert errors == ['CommandRecognitionTimeout'] and not replies
    abort.assert_called_once()
    # No unbounded replacement threads if an extension ignores cancellation.
    assert not runner.start(lambda c:'next', result=replies.append, error=errors.append, timeout=1, abort=abort)
    release.set()
    pump(qt, lambda:not runner._thread.is_alive())
    qt.processEvents()
    assert not replies
    assert runner.start(lambda c:'fresh', result=replies.append, error=errors.append, timeout=1, abort=abort)
    pump(qt, lambda:bool(replies))
    assert replies == ['fresh']


def test_explicit_cancel_has_no_timeout_or_late_callback(qt):
    runner = CommandAsrRunner()
    release = threading.Event()
    calls = []
    abort = Mock(side_effect=release.set)
    runner.start(lambda c:(release.wait(1),'late')[1], result=calls.append,error=calls.append,timeout=.2,abort=abort)
    runner.cancel()
    pump(qt, lambda:not runner._thread.is_alive())
    qt.processEvents()
    assert calls == []
    abort.assert_called_once()


class VoiceHarness(QObject):
    _transcribe_command = MainWindow._transcribe_command
    _deliver_command_asr155 = MainWindow._deliver_command_asr155
    _command_transcription_ready = MainWindow._command_transcription_ready
    _voice_recognized = MainWindow._voice_recognized
    _send_recognized_text = MainWindow._send_recognized_text
    _command_asr_failed157 = MainWindow._command_asr_failed157
    _cancel_command_asr157 = MainWindow._cancel_command_asr157
    _normalize_recognized_command = MainWindow._normalize_recognized_command
    _looks_like_linguistic_text = staticmethod(MainWindow._looks_like_linguistic_text)
    _match_wake_phrase = MainWindow._match_wake_phrase
    def __init__(self, text='Джарвис, привет'):
        super().__init__()
        self.config = SimpleNamespace(get=lambda key, default=None: {'voice.fast_media_enabled':False,'wake_word.phrase':'Джарвис'}.get(key,default))
        self.agent = None
        self._language_code = lambda:'ru'
        self._command_asr_runner157 = CommandAsrRunner(self)
        self.transcription_service = SimpleNamespace(interrupt=Mock())
        self.recognizer = SimpleNamespace(transcribe_detailed=Mock(return_value=TranscriptionResult(text,[])))
        self.speech_understanding = SimpleNamespace(resolve=lambda text,**kw:SimpleNamespace(text=text,changed=False), contextual_rescue_terms=lambda *a,**kw:[])
        self._heard_target_text=self._heard_display_text=''
        self._command_asr_generation=4;self._agent_generation=0;self._partial_generation=0
        self._quitting=self._app_mic_muted=self._continuous_voice_session=False
        self._voice_mode='';self._busy=True;self._live_user_bubble=None
        self._heard_timer = QTimer(self)
        self.input=Mock();self.mic_button=Mock()
        self._probable_system_playback_echo=lambda *a,**kw:False
        self._voice_unlock_phrase_matches=lambda text:False
        self._restore_listening_audio=Mock();self._clear_listening_bubble=Mock()
        self._append_assistant=Mock();self._rearm_voice_after_answer=Mock()
        self._finalize_live_user_bubble=Mock();self._send_text=Mock()
    def _set_busy(self,busy,*a,**kw):
        self._busy=busy


@pytest.mark.parametrize('spoken',['Джарвис, привет','привет'])
def test_actual_final_audio_to_voice_handler_to_queued_command(qt,spoken):
    w=VoiceHarness(spoken)
    w._transcribe_command(RecordingResult(np.ones(48000,dtype=np.float32),16000))
    pump(qt, lambda:w._send_text.called)
    assert not w._busy and not w._command_asr_pending157
    w._send_text.assert_called_once_with('привет',user_already_shown=True,input_source='voice',raw_text=spoken)
    w._finalize_live_user_bubble.assert_called_once_with('привет')


def test_actual_decode_exception_clears_thinking_and_rearms(qt):
    w=VoiceHarness()
    w.recognizer.transcribe_detailed.side_effect=RuntimeError('test native decoder failure')
    w._transcribe_command(RecordingResult(np.ones(48000,dtype=np.float32),16000))
    pump(qt, lambda:w._rearm_voice_after_answer.called)
    assert not w._busy and not w._command_asr_pending157
    w._send_text.assert_not_called()
    assert 'Повторите' in w._append_assistant.call_args.args[0] or 'повторите' in w._append_assistant.call_args.args[0]
    w._clear_listening_bubble.assert_called_once()
    w._restore_listening_audio.assert_called_once()


def test_stale_decode_does_not_run_after_cancel(qt):
    w=VoiceHarness()
    release=threading.Event()
    w.recognizer.transcribe_detailed.side_effect=lambda rec:(release.wait(1),TranscriptionResult('привет',[]))[1]
    w._transcribe_command(RecordingResult(np.ones(48000,dtype=np.float32),16000))
    w._cancel_command_asr157()
    release.set()
    pump(qt, lambda:not w._command_asr_runner157._thread.is_alive())
    qt.processEvents()
    w._send_text.assert_not_called()
    assert not w._command_asr_pending157


@pytest.mark.parametrize('action', ['next','previous','pause','resume'])
def test_stop_does_not_disable_following_owned_player_control(action):
    w=SimpleNamespace(_post_interrupt_require_wake=True, _cached_quick_media_owner=lambda **kw:'yandex_music', config=SimpleNamespace(get=lambda k,d=None:d))
    assert MainWindow._wakeless_quick_media_allowed(w,QuickMediaCommand(action,'auto','command'))
    w._cached_quick_media_owner=lambda **kw:''
    assert not MainWindow._wakeless_quick_media_allowed(w,QuickMediaCommand(action,'auto','command'))
    assert MainWindow._wakeless_quick_media_allowed(w,QuickMediaCommand(action,'yandex_music','command'))


def request(service, timeout=.1):
    return service.transcribe(RecordingResult(np.ones(320,dtype=np.float32),16000),model_name='tiny',language='ru',device='cpu',compute_type='int8',timeout_seconds=timeout)


def test_service_deadline_includes_waiting_for_preload_lock():
    service=TranscriptionService()
    started,release=threading.Event(),threading.Event()
    def hold():
        with service._lock:
            started.set();release.wait(2)
    t=threading.Thread(target=hold);t.start();assert started.wait(1)
    before=time.monotonic()
    try:
        with pytest.raises(TimeoutError):request(service,.1)
        assert time.monotonic()-before < .7
        assert service._process is None
    finally:
        release.set();t.join(1)


def test_cancelled_native_worker_is_not_restarted_but_future_requests_work(monkeypatch):
    service=TranscriptionService()
    calls=[]
    def once(req, timeout,**kw):
        calls.append(req)
        service.interrupt()
        raise _SttWorkerCrashed('intentional cancellation')
    monkeypatch.setattr(service,'_request_once_locked',once)
    with pytest.raises(TimeoutError):request(service)
    assert len(calls)==1
    monkeypatch.setattr(service,'_request_once_locked',lambda *a,**k:'fresh')
    assert request(service)=='fresh'


def test_partial_and_wake_use_same_native_model_key():
    from jarvis.voice import SpeechRecognizer
    config=SimpleNamespace(get=lambda key,default=None:{'voice.stt_device':'cuda','voice.stt_compute_type':'float16','voice.stt_cpu_threads':4,'wake_word.cpu_threads':2}.get(key,default))
    service=TranscriptionService()
    wake=SpeechRecognizer(config,service,purpose='wake')
    partial=SpeechRecognizer(config,service,model_name='tiny',purpose='partial')
    assert (wake.model_name,wake._device_policy(),wake._cpu_threads())==(partial.model_name,partial._device_policy(),partial._cpu_threads())


def test_real_qt_window_static_noise_gate_remains_bound_correctly(qt):
    from PySide6.QtWidgets import QMainWindow
    window=MainWindow.__new__(MainWindow)
    QMainWindow.__init__(window)
    assert window._looks_like_linguistic_text('привет')
    assert not window._looks_like_linguistic_text('...')
    window.deleteLater()
    qt.processEvents()


@pytest.mark.parametrize('action',['next','previous','pause','resume','like','dislike'])
def test_wakeless_bare_controls_do_not_start_configured_inactive_provider(action):
    owner=Mock(return_value='')
    w=SimpleNamespace(_post_interrupt_require_wake=False,_cached_quick_media_owner=owner,
        config=SimpleNamespace(get=lambda key,default=None: 'spotify' if key=='music.provider' else default))
    assert not MainWindow._wakeless_quick_media_allowed(w,QuickMediaCommand(action,'auto','command'))
    owner.assert_called_once_with(allow_historical=False)
    assert MainWindow._wakeless_quick_media_allowed(w,QuickMediaCommand(action,'spotify','command'))
    owner.return_value='spotify'
    assert MainWindow._wakeless_quick_media_allowed(w,QuickMediaCommand(action,'auto','command'))
