"""Wake backlog and native decoder state regressions; no microphone accuracy claim."""
from __future__ import annotations

import base64
import io
import json
import math
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from jarvis.runtime.wake_queue160 import prefer_recording, select_pending
from jarvis.voice import RecordingResult


def pcm(identity, at, duration=.6):
    return RecordingResult(
        (np.sin(np.arange(int(16000 * duration), dtype=np.float32) * .11) * .1),
        16000, correlation_id=identity, captured_at=at,
    )


def window():
    from jarvis.ui.main_window import MainWindow
    w = SimpleNamespace(
        _wake_generation=4, _wake_worker_active=False, _wake_probe_worker_active=False,
        _pending_wake_recording=None, _pending_wake_probe_recording=None,
        _quitting=False, _busy=False, _voice_mode="wake", _app_mic_muted=False,
        _wake_guard=SimpleNamespace(accepts=lambda *args: True),
        _wake_rescue_pending_generation=-1, _wake_rescue_pending_at=0,
        _response_active=lambda: False, _acoustic_wake_enabled_for_current_phrase=lambda: False,
        config=SimpleNamespace(get=lambda key, default=None: default),
        _wake_result_queue=queue.Queue(),
        _wake_result_timer=SimpleNamespace(isActive=lambda: True, stop=Mock()),
        _launch_wake_probe=Mock(), _launch_wake_transcription=Mock(),
    )
    return MainWindow, w


def test_new_short_wake_replaces_old_long_probe_in_actual_ingress():
    MainWindow, w = window()
    w._wake_worker_active = True
    old, new = pcm("old", 10, 1.2), pcm("new", 11, .35)
    w._pending_wake_probe_recording = old, 4, False
    MainWindow._audio_wake_probe_ready(w, new)
    assert w._pending_wake_probe_recording[0] is new
    assert not w._launch_wake_probe.called


def test_out_of_order_old_long_recording_does_not_replace_fresh_short_one():
    MainWindow, w = window()
    w._wake_worker_active = True
    new, old = pcm("new", 11, .35), pcm("old", 10, 1.2)
    w._pending_wake_probe_recording = new, 4, False
    MainWindow._audio_wake_probe_ready(w, old)
    assert w._pending_wake_probe_recording[0] is new


@pytest.mark.parametrize("new_complete", [True, False])
def test_latest_utterance_wins_across_complete_and_partial_mailboxes(new_complete):
    MainWindow, w = window()
    complete = pcm("complete", 12 if new_complete else 10, 1.2)
    probe = pcm("probe", 10 if new_complete else 12, .35)
    w._pending_wake_recording = complete, 4
    w._pending_wake_probe_recording = probe, 4, False
    MainWindow._wake_probe_finished(w, 4)
    if new_complete:
        w._launch_wake_transcription.assert_called_once_with(complete, 4)
        w._launch_wake_probe.assert_not_called()
    else:
        w._launch_wake_probe.assert_called_once_with(probe, 4, verify_rescue=False)
        w._launch_wake_transcription.assert_not_called()
    assert w._pending_wake_recording is None and w._pending_wake_probe_recording is None


def test_complete_pcm_consumes_own_pending_probe_once_after_decode_finishes():
    MainWindow, w = window()
    complete, probe = pcm("same", 12, 1.2), pcm("same", 11, .6)
    w._pending_wake_recording = complete, 4
    w._pending_wake_probe_recording = probe, 4, False
    MainWindow._wake_probe_finished(w, 4)
    w._launch_wake_transcription.assert_called_once_with(complete, 4)
    MainWindow._wake_worker_finished(w, 4)
    w._launch_wake_probe.assert_not_called()
    assert w._wake_result_timer.stop.called


@pytest.mark.parametrize("mode", ["muted_generation", "quit", "busy"])
def test_cancelled_stale_or_busy_queue_does_not_restart_capture(mode):
    MainWindow, w = window()
    w._pending_wake_recording = pcm("cancelled", 12), 4
    w._pending_wake_probe_recording = pcm("cancelled", 11), 4, False
    if mode == "muted_generation":
        w._wake_generation = 5
    elif mode == "quit":
        w._quitting = True
    else:
        w._busy = True
    MainWindow._wake_worker_finished(w, 4)
    w._launch_wake_probe.assert_not_called()
    w._launch_wake_transcription.assert_not_called()


def test_active_native_decode_prevents_a_second_parallel_decode():
    MainWindow, w = window()
    pending = pcm("new", 12), 4
    w._pending_wake_recording = pending
    w._wake_worker_active = True
    assert MainWindow._start_pending_wake160(w) is False
    assert w._pending_wake_recording is pending
    w._launch_wake_transcription.assert_not_called()


@pytest.mark.parametrize("kind", ["probe", "complete"])
def test_old_probe_completion_releases_lane_for_new_generation(kind):
    MainWindow, w = window()
    w._wake_generation = 5
    w._wake_probe_worker_active = True
    recording = pcm("new-session", 15, .4)
    if kind == "probe":
        w._pending_wake_probe_recording = recording, 5, False
    else:
        w._pending_wake_recording = recording, 5
    MainWindow._wake_probe_finished(w, 4)
    if kind == "probe":
        w._launch_wake_probe.assert_called_once_with(recording, 5, verify_rescue=False)
    else:
        w._launch_wake_transcription.assert_called_once_with(recording, 5)
    w._wake_result_timer.stop.assert_not_called()
    assert w._pending_wake_probe_recording is None and w._pending_wake_recording is None


@pytest.mark.parametrize("state", ["muted", "command", "quitting", "busy"])
def test_old_probe_completion_cannot_restart_disallowed_current_state(state):
    MainWindow, w = window()
    w._wake_generation = 5
    w._wake_probe_worker_active = True
    w._pending_wake_probe_recording = pcm("new-session", 15, .4), 5, False
    if state == "muted":
        w._app_mic_muted = True
    elif state == "command":
        w._voice_mode = "command"
    elif state == "quitting":
        w._quitting = True
    else:
        w._busy = True
    MainWindow._wake_probe_finished(w, 4)
    w._launch_wake_probe.assert_not_called()
    w._launch_wake_transcription.assert_not_called()
    assert w._pending_wake_probe_recording is None


def test_legacy_recording_without_metadata_keeps_completed_clip():
    complete, probe = pcm("", 0, 1.2), pcm("", 0, .6)
    assert select_pending(4, (complete, 4), (probe, 4, False))[0] == "complete"
    assert prefer_recording(complete, probe)


def test_qt_keeps_dispatching_while_native_decode_is_blocked(monkeypatch):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    MainWindow, w = window()
    blocker, entered = threading.Event(), threading.Event()
    w._enqueue_wake_result = w._wake_result_queue.put_nowait
    def blocking_decode(recording):
        entered.set()
        blocker.wait(2)
        return {"matched": False, "text": ""}
    w._transcribe_wake_robust = blocking_decode
    ticks = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start()
    try:
        before = time.monotonic()
        MainWindow._launch_wake_transcription(w, pcm("blocking", before), 4)
        assert time.monotonic() - before < .2
        assert entered.wait(.5)
        until = time.monotonic() + .1
        while time.monotonic() < until:
            app.processEvents()
            time.sleep(.001)
        assert len(ticks) >= 4
    finally:
        blocker.set()
        timer.stop()
        w._wake_thread.join(1)
    result = w._wake_result_queue.get_nowait()
    assert result[0] == "result" and result[1]["decode_ms"] >= 70


def test_shared_whisper_model_restores_full_window_after_fast_probe(monkeypatch):
    """Stateful backend emulates upstream FeatureExtractor's persistent window.

    Verify real IPC request dispatch and options across successive calls, including
    the resulting number of windows. Actual Whisper inference is not faked as a
    timing benchmark: this specifically guards state leaking between purposes.
    """
    import sys
    from jarvis import stt_worker
    calls = []
    class StatefulWhisper:
        def __init__(self, *args, **kwargs):
            self.frames = 3000
        def transcribe(self, audio, **kwargs):
            if kwargs.get("chunk_length") is not None:
                self.frames = int(kwargs["chunk_length"] * 100)
            windows = math.ceil(audio.size / 160 / self.frames)
            calls.append((audio.size, kwargs, windows))
            segment = SimpleNamespace(text="Jarvis", avg_logprob=-.1, no_speech_prob=.01)
            return [segment], None
    requests = []
    for index, (purpose, seconds) in enumerate([("wake_fast", .6), ("wake", 2.6), ("partial", 3.2), ("wake_fast", .6), ("command", 3.5)]):
        requests.append({
            "id": str(index), "audio_b64": base64.b64encode(pcm("", 0, seconds).audio.tobytes()).decode(),
            "sample_rate": 16000, "model_name": "tiny", "device": "cpu", "compute_type": "int8",
            "purpose": purpose, "language": "en", "adaptive_decode_retry": False,
        })
    output = io.StringIO()
    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=StatefulWhisper))
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n".join(json.dumps(row) for row in requests)))
    monkeypatch.setattr(sys, "stdout", output)
    assert stt_worker.main() == 0
    replies = [json.loads(row) for row in output.getvalue().splitlines()]
    assert all(row["ok"] for row in replies), replies
    assert len(calls) == 5
    assert [row[1]["chunk_length"] for row in calls] == [2, 30, 30, 2, 30]
    assert [row[2] for row in calls] == [1] * 5
    assert calls[1][1]["beam_size"] == 2 and calls[-1][1]["beam_size"] == 7
