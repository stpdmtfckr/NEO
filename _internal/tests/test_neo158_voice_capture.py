"""Production audio-worker replay: PCM and timestamps, not a mocked VAD flag."""

import queue
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from jarvis import audio_backend as backend
from jarvis.voice import RecordingResult


def replay(
    monkeypatch,
    *,
    pause=None,
    delay=0.7,
    has_tail=False,
    cancel=False,
    click=False,
    wake_end=0.8,
    captured=0.78,
    candidate_resume=False,
):
    commands = queue.Queue()
    events = []
    clock = [100.0]
    sent = [False]
    opened = []
    resume = [None]
    commands.put(
        {
            "cmd": "start",
            "mode": "wake",
            "session": 1,
            "target_rate": 16000,
            "wake_silence": 0.12,
            "partial_max_updates": 0,
        }
    )
    command_start = wake_end + pause if pause is not None else None
    command_end = command_start + 0.65 if command_start is not None else None
    promote_at = 100 + captured + delay

    class Stream:
        def read(self, size):
            clock[0] = round(clock[0] + 0.02, 8)
            elapsed = clock[0] - 100
            if resume[0] is not None and clock[0] >= resume[0]:
                commands.put(
                    {
                        "cmd": "start",
                        "mode": "command",
                        "session": 3,
                        "resume_from_last_finish": True,
                        "partial_max_updates": 0,
                    }
                )
                resume[0] = None
            if not sent[0] and clock[0] >= promote_at:
                commands.put(
                    {
                        "cmd": "promote",
                        "session": 2,
                        "capture_started_at": 100.0,
                        "wake_captured_at": 100 + captured,
                        "wake_has_command": has_tail,
                        "completed_utterance": has_tail,
                        "start_timeout": 6.0,
                        "partial_max_updates": 0,
                    }
                )
                sent[0] = True
            if cancel and promote_at + 0.5 <= clock[0] < promote_at + 0.52:
                commands.put({"cmd": "stop", "session": 2, "emit_result": False})
            if elapsed > 11:
                commands.put({"cmd": "shutdown"})
            # Separate frequencies let us verify that the real final PCM contains
            # the command and not just the old wake clip or all-zero padding.
            frequency = 310.0
            amplitude = 0.1 if 0.2 <= elapsed <= wake_end else 0.0
            if command_start is not None and command_start <= elapsed <= command_end:
                frequency = 730.0
                amplitude = 0.18
            data = (
                np.sin((np.arange(320) / 16000 + elapsed) * 2 * np.pi * frequency)
                * amplitude
            )
            if click and 2.0 <= elapsed < 2.02:
                data[120] = 0.9
            return data.astype(np.float32).reshape(-1, 1), False

        def abort(self):
            pass

        def close(self):
            pass

    class Events:
        def put(self, event, **kw):
            events.append(event)
            if event["type"] == "finished":
                if (
                    candidate_resume
                    and event["session"] == 2
                    and event["wake_candidate"]
                ):
                    resume[0] = clock[0] + 2.2
                else:
                    commands.put({"cmd": "shutdown"})

        put_nowait = put

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace())
    monkeypatch.setattr(
        backend,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda _: None),
    )
    monkeypatch.setattr(
        backend,
        "_resolve_input_device",
        lambda *a, **kw: (0, "fixture", "token", 16000, "fake"),
    )

    def open_stream(*args):
        opened.append(True)
        return Stream(), 16000, 320, "float32"

    monkeypatch.setattr(backend, "_open_stream", open_stream)
    backend._audio_worker_main(commands, Events())
    assert not [e for e in events if e["type"] == "error"]
    assert len(opened) == 1, "wake handoff must not reopen PortAudio"
    return events, promote_at, command_start, command_end


@pytest.mark.parametrize("pause", [0.0, 0.15, 0.5, 1.2, 2.5, 4.5])
def test_wake_and_command_survive_oneflow_and_thinking_pause(monkeypatch, pause):
    events, promotion, _command_start, command_end = replay(monkeypatch, pause=pause)
    finals = [e for e in events if e["type"] == "finished"]
    assert len(finals) == 1
    final = finals[0]
    assert final["captured_at"] >= 100 + command_end
    assert final["captured_at"] < max(promotion, 100 + command_end) + 1.2
    assert final["empty_reason"] == ""
    pcm = np.frombuffer(final["audio"], np.float32)
    assert (np.abs(pcm) > 0.12).sum() > 4800, "command samples were cut off"
    assert (np.abs(pcm) > 0.07).sum() > 5000, "wake plus command should be retained"


@pytest.mark.parametrize("delay", [0.05, 0.7, 2.3])
def test_command_already_finished_while_wake_asr_running_is_kept(monkeypatch, delay):
    events, *_ = replay(monkeypatch, pause=0.0, delay=delay)
    final = next(e for e in events if e["type"] == "finished")
    assert np.max(np.abs(np.frombuffer(final["audio"], np.float32))) > 0.17


def test_whole_phrase_inside_wake_probe_uses_only_onset_hint(monkeypatch):
    events, *_ = replay(monkeypatch, pause=0.0, captured=1.55, has_tail=True)
    final = next(e for e in events if e["type"] == "finished")
    assert np.max(np.abs(np.frombuffer(final["audio"], np.float32))) > 0.17


@pytest.mark.parametrize("click", [False, True])
def test_wake_only_and_keyboard_click_wait_without_decoding_old_wake(
    monkeypatch, click
):
    events, promote, *_ = replay(monkeypatch, click=click)
    final = next(e for e in events if e["type"] == "finished")
    assert promote + 6 <= final["captured_at"] < promote + 6.1
    assert final["audio"] == b""
    assert final["empty_reason"] == "wake_start_timeout"
    assert not [e for e in events if e["type"] == "partial"]


def test_cancel_during_postwake_wait_discards_buffer(monkeypatch):
    events, *_ = replay(monkeypatch, pause=3.0, cancel=True)
    assert not [e for e in events if e["type"] == "finished"]
    assert len([e for e in events if e["type"] == "stopped"]) == 1


def test_controller_carries_wake_boundary_and_hint_without_executing_text(monkeypatch):
    import threading

    from jarvis.ui.audio_capture import AudioCaptureController

    sent = []
    obj = SimpleNamespace(
        _io_lock=threading.RLock(),
        _recording=True,
        _mode="wake",
        _session=1,
        _force_safe_host=False,
        _safe_host_index=0,
        config=SimpleNamespace(get=lambda key, default=None: default),
        _send_locked=sent.append,
        failed=SimpleNamespace(emit=lambda value: pytest.fail(value)),
    )
    rec = RecordingResult(
        np.zeros(16000, np.float32),
        16000,
        captured_at=40.0,
        capture_started_at=39.0,
        wake_has_command=True,
    )
    assert AudioCaptureController.promote_to_command(obj, rec)
    assert sent[0]["wake_captured_at"] == 40.0
    assert sent[0]["wake_has_command"] is True
    assert sent[0]["start_timeout"] == 6.0
    assert "text" not in sent[0]


def test_empty_capture_does_not_flash_thinking_or_queue_asr():
    from jarvis.ui.main_window import MainWindow

    calls = []
    obj = SimpleNamespace(
        _voice_mode="command",
        _partial_generation=0,
        _show_thinking_bubble=lambda: calls.append("thinking"),
        mic_button=SimpleNamespace(setText=lambda _: None),
        mic_level=SimpleNamespace(hide=lambda: None),
        _transcribe_command=lambda recording: calls.append(
            ("empty", recording.empty_reason)
        ),
    )
    MainWindow._audio_finished(
        obj,
        RecordingResult(
            np.array([], np.float32), 16000, empty_reason="wake_start_timeout"
        ),
    )
    assert calls == [("empty", "wake_start_timeout")]


def test_early_probe_wake_tail_stays_listening_and_retains_later_command(monkeypatch):
    events, *_ = replay(
        monkeypatch,
        pause=2.0,
        captured=0.30,
        delay=0.3,
        wake_end=1.10,
        candidate_resume=True,
    )
    finals = [e for e in events if e["type"] == "finished"]
    assert len(finals) == 2
    assert finals[0]["wake_candidate"] is True
    assert finals[0]["captured_at"] < 103.1
    assert np.max(np.abs(np.frombuffer(finals[0]["audio"], np.float32))) < 0.11
    assert np.max(np.abs(np.frombuffer(finals[1]["audio"], np.float32))) > 0.17


def test_ambiguous_probe_never_changes_listening_bubble_to_thinking():
    from jarvis.ui.main_window import MainWindow

    calls = []
    obj = SimpleNamespace(
        _voice_mode="command",
        _partial_generation=0,
        _show_thinking_bubble=lambda: calls.append("thinking"),
        mic_button=SimpleNamespace(setText=lambda _: None),
        mic_level=SimpleNamespace(hide=lambda: None),
        _transcribe_command=lambda recording: calls.append("validate"),
    )
    MainWindow._audio_finished(
        obj, RecordingResult(np.ones(320, np.float32), 16000, wake_candidate=True)
    )
    assert calls == ["validate"] and obj._wake_candidate_pending158
    assert obj._voice_mode == "command_pending"
    # While validation runs, a watchdog must not replace the buffering stream.
    MainWindow._start_interrupt_wake_monitor(obj, 0)


@pytest.mark.parametrize("transcript", ["Джарвис", "Джарвис, привет", ""])
def test_candidate_real_asr_delivery_retains_listening_or_dispatches_once(transcript):
    from unittest.mock import Mock

    from PySide6.QtWidgets import QApplication

    from tests.test_neo157_voice import VoiceHarness, pump

    qt = QApplication.instance() or QApplication([])
    w = VoiceHarness(transcript)
    w._start_command_after_acoustic_wake = Mock()
    w._set_busy = Mock()
    w._voice_mode = "command_pending"
    w._wake_candidate_pending158 = True
    w._transcribe_command(
        RecordingResult(np.ones(32000, np.float32), 16000, wake_candidate=True)
    )
    pump(qt, lambda: w._send_text.called or w._start_command_after_acoustic_wake.called)
    assert all("thinking" not in call.args for call in w._set_busy.call_args_list)
    if transcript == "Джарвис, привет":
        w._send_text.assert_called_once_with(
            "привет", user_already_shown=True, input_source="voice", raw_text=transcript
        )
        w._start_command_after_acoustic_wake.assert_not_called()
    else:
        w._send_text.assert_not_called()
        w._start_command_after_acoustic_wake.assert_called_once()
    assert not w._wake_candidate_pending158


def test_mute_or_cancel_stops_worker_buffer_even_after_candidate_finished():
    import base64
    import io
    import json

    from jarvis.ui.audio_capture import AudioCaptureController
    from PySide6.QtWidgets import QApplication

    qt = QApplication.instance() or QApplication([])
    capture = AudioCaptureController(SimpleNamespace())
    capture._poll_timer.stop()
    pipe = io.StringIO()
    capture._process = SimpleNamespace(poll=lambda: None, stdin=pipe)
    delivered = []
    capture.finished.connect(delivered.append)
    capture._handle_event(
        {
            "type": "finished",
            "session": 0,
            "wake_candidate": True,
            "audio_b64": base64.b64encode(np.ones(320, np.float32).tobytes()).decode(),
        }
    )
    assert delivered[0].wake_candidate and not capture.is_recording
    assert capture._command_waiting158
    capture.stop(emit_result=False)
    assert json.loads(pipe.getvalue()) == {
        "cmd": "stop",
        "session": 0,
        "emit_result": False,
    }
    assert not capture._command_waiting158
    capture._process = None
    capture.deleteLater()
    qt.processEvents()


def test_asr_failure_replaces_thinking_in_same_real_qt_row():
    from jarvis.ui.chat_view import ChatView
    from PySide6.QtWidgets import QApplication

    from tests.test_neo157_voice import VoiceHarness, pump

    app = QApplication.instance() or QApplication([])
    window = VoiceHarness()
    window.chat = ChatView()
    bubble = window.chat.add_assistant_pending("Думаю")
    window._listening_bubble = bubble
    window.recognizer.transcribe_detailed.side_effect = RuntimeError(
        "fixture decoder error"
    )
    window._transcribe_command(RecordingResult(np.ones(32000, np.float32), 16000))
    pump(app, lambda: window._rearm_voice_after_answer.called)
    assert "повторите" in bubble.text
    assert len([item for item in window.chat.items if item.kind == "assistant"]) == 1
    assert window._listening_bubble is None
    window._clear_listening_bubble.assert_not_called()
    window._append_assistant.assert_not_called()
    window.chat.deleteLater()
    window.deleteLater()
    app.processEvents()
