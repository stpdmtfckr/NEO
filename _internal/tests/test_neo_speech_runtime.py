"""Deterministic speech IPC tests; synthetic PCM is not an acoustic benchmark."""
from __future__ import annotations

import base64
import io
import json
import queue
import threading
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np

from jarvis import stt_worker
from jarvis.ambient_loopback_worker import _capture_settings
from jarvis import ambient_loopback_worker
from jarvis.runtime.resource_budget import ResourceBudget
from jarvis.voice import (
    RecordingResult, SpeechRecognizer, TranscriptionService,
    _SttWorkerCrashed, _transcription_options,
)
from jarvis.wake_detector import HybridWakeDetector


class SpeechRuntimeTests(unittest.TestCase):
    @staticmethod
    def request(ident, audio, **overrides):
        result = {"id": ident, "audio_b64": base64.b64encode(np.asarray(audio, dtype=np.float32).tobytes()).decode(),
                  "sample_rate": 16000, "device": "cpu", "compute_type": "int8",
                  "purpose": "command", "language": "en", "return_hypotheses": True}
        result.update(overrides)
        return result

    def run_worker(self, requests, model):
        output = io.StringIO()
        module = types.ModuleType("faster_whisper")
        module.WhisperModel = model
        with patch.dict("sys.modules", {"faster_whisper": module}), patch("sys.stdin", io.StringIO(
            "\n".join(json.dumps(x) for x in requests))), patch("sys.stdout", output):
            self.assertEqual(stt_worker.main(), 0)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_silence_nonfinite_and_empty_do_not_load_model(self):
        constructor = Mock(side_effect=AssertionError("silence must not load Whisper"))
        requests = [self.request(str(i), audio) for i, audio in enumerate(
            [[], np.zeros(1600), np.full(1600, np.nan), np.full(1600, np.inf), np.full(1600, 1e-9)])]
        results = self.run_worker(requests, constructor)
        self.assertEqual(len(results), 5)
        for result in results:
            self.assertTrue(result["ok"])
            self.assertEqual((result["text"], result["score"], result["hypotheses"], result["decode_count"]), ("", 0.0, [], 0))
            self.assertFalse(result["suspicious"])
        constructor.assert_not_called()

    def test_silence_after_success_does_not_reuse_quality_or_hypotheses(self):
        model = Mock()
        model.transcribe.return_value = ([types.SimpleNamespace(text="open folder", avg_logprob=-0.2, no_speech_prob=0.01)], None)
        audio = np.sin(np.arange(6400, dtype=np.float32) * 0.11) * 0.1
        results = self.run_worker([self.request("first", audio), self.request("second", np.zeros(1600))], Mock(return_value=model))
        self.assertEqual(results[0]["text"], "open folder")
        self.assertEqual(results[0]["decode_count"], 1)
        self.assertEqual(results[1]["text"], "")
        self.assertEqual(results[1]["score"], 0.0)
        self.assertEqual(results[1]["hypotheses"], [])
        self.assertEqual(model.transcribe.call_count, 1)

    def test_protocol_rejects_non_object_and_truncated_float_without_crashing(self):
        constructor = Mock()
        results = self.run_worker([[], {"id": "broken", "audio_b64": "AA=="}], constructor)
        self.assertTrue(all(not x["ok"] for x in results))
        constructor.assert_not_called()

    def test_sample_rate_conversion_uses_actual_rate(self):
        source = np.sin(np.arange(8000, dtype=np.float32) * 0.11) * 0.1
        converted = stt_worker._request_audio(self.request("r", source, sample_rate=8000), "command")
        self.assertEqual(converted.size, 16000)
        self.assertTrue(np.all(np.isfinite(converted)))

    def test_native_crash_restarts_once_and_preserves_audio(self):
        service = TranscriptionService()
        service._cleanup_locked = Mock()
        service._request_once_locked = Mock(side_effect=[_SttWorkerCrashed("native exit"), "hello"])
        result = service.transcribe(RecordingResult(np.ones(10, dtype=np.float32), 16000),
                                    model_name="tiny", language="en", device="cpu", compute_type="int8")
        self.assertEqual(result, "hello")
        self.assertEqual(service._request_once_locked.call_count, 2)
        service._cleanup_locked.assert_called_once_with(force=True)
        self.assertEqual(service._request_once_locked.call_args_list[0].args[0], service._request_once_locked.call_args_list[1].args[0])

    def test_reader_is_bound_to_old_queue_after_restart(self):
        service = TranscriptionService()
        old_queue = queue.Queue()
        process = types.SimpleNamespace(stdout=io.StringIO('{"id":"old","ok":true}\n'))
        service._read_stdout(process, old_queue)
        self.assertEqual(old_queue.get_nowait()["id"], "old")
        self.assertTrue(service._responses.empty())

    def test_timeout_terminates_owned_worker_before_next_request(self):
        service = TranscriptionService()
        service._start_locked = Mock()
        service._cleanup_locked = Mock()
        service._process = types.SimpleNamespace(stdin=io.StringIO(), poll=lambda: None)
        with patch("jarvis.voice.time.monotonic", side_effect=[0.0, 0.0, 2.0]):
            with self.assertRaises(TimeoutError):
                service._request_once_locked({"id": "timeout"}, 1.0)
        service._cleanup_locked.assert_called_once_with(force=True)

    def test_wake_cpu_policy_applies_to_all_decode_paths(self):
        settings = {"voice.stt_device": "cuda", "voice.stt_compute_type": "float16",
                    "voice.stt_cpu_threads": 4, "wake_word.cpu_threads": 1, "wake_word.force_cpu": True}
        config = types.SimpleNamespace(get=lambda key, default=None: settings.get(key, default))
        service = Mock()
        service._lock = threading.RLock()
        service.transcribe.return_value = "Jarvis"
        recognizer = SpeechRecognizer(config, service, purpose="wake")
        recording = RecordingResult(np.ones(160, dtype=np.float32), 16000)
        recognizer.preload()
        recognizer.transcribe(recording)
        recognizer.transcribe_fast_wake_probe(recording)
        recognizer.transcribe_fast_control_probe(recording)
        recognizer.transcribe_with_language(recording, "ru")
        for call in [service.preload.call_args, *service.transcribe.call_args_list]:
            self.assertEqual((call.kwargs["device"], call.kwargs["compute_type"], call.kwargs["cpu_threads"]), ("cpu", "int8", 1))
        settings["wake_word.force_cpu"] = False
        self.assertEqual(recognizer._device_policy(), ("cuda", "float16"))

    def test_wake_override_does_not_reduce_command_quality(self):
        config = types.SimpleNamespace(get=lambda key, default=None: {"voice.stt_device": "cuda", "wake_word.force_cpu": True}.get(key, default))
        recognizer = SpeechRecognizer(config, Mock(), purpose="command")
        self.assertEqual(recognizer._device_policy()[0], "cuda")
        self.assertEqual(_transcription_options("command", "de")["beam_size"], 7)

    def test_nonfinite_acoustic_score_never_activates_wake(self):
        detector = HybridWakeDetector()
        detector._request = Mock(return_value={"ok": True, "score": float("nan")})
        self.assertEqual(detector.score(RecordingResult(np.ones(160, dtype=np.float32), 16000)), 0.0)

    def test_cuda_only_compute_type_is_safe_when_auto_falls_back_to_cpu(self):
        with patch.object(stt_worker, "_cuda_runtime_ready", return_value=False):
            self.assertEqual(stt_worker._runtime_target("auto", "float16"), ("cpu", "int8"))

    def test_foreground_work_defers_background_and_releases_after_error(self):
        budget = ResourceBudget()
        self.assertFalse(budget.should_defer_heavy_background())
        with self.assertRaises(ValueError):
            with budget.foreground_work():
                self.assertTrue(budget.should_defer_heavy_background())
                self.assertFalse(budget.should_defer_heavy_background(correctness_required=True))
                raise ValueError("simulated cancellation")
        self.assertFalse(budget.should_defer_heavy_background())

    def test_loopback_defaults_to_reference_only_and_bounds_opt_in(self):
        self.assertFalse(_capture_settings([])["transcription_enabled"])
        settings = _capture_settings(['{"transcription_enabled":true,"max_seconds":900,"silence_seconds":0.01}'])
        self.assertTrue(settings["transcription_enabled"])
        self.assertEqual((settings["max_seconds"], settings["silence_seconds"]), (15.0, 0.25))

    def test_reference_only_worker_emits_no_raw_audio_or_speech_chunks(self):
        class Recorder:
            def __enter__(self):
                self.calls = 0
                return self
            def __exit__(self, *args):
                return False
            def record(self, numframes):
                self.calls += 1
                if self.calls > 22:
                    raise BrokenPipeError("end of synthetic capture")
                return (np.sin(np.arange(numframes, dtype=np.float32) * 0.06) * (0.1 + self.calls * 0.002))[:, None]
        endpoint = types.SimpleNamespace(name="Speakers", id="speaker", isloopback=True, recorder=lambda **kwargs: Recorder())
        soundcard = types.ModuleType("soundcard")
        soundcard.default_speaker = lambda: endpoint
        soundcard.all_microphones = lambda **kwargs: [endpoint]
        clock = iter(np.arange(0.0, 100.0, 0.1))
        with patch.dict("sys.modules", {"soundcard": soundcard}), patch.object(ambient_loopback_worker.os, "name", "nt"), \
             patch("sys.argv", ["worker", '{"transcription_enabled":false}']), \
             patch.object(ambient_loopback_worker.time, "monotonic", side_effect=lambda: next(clock)), \
             patch.object(ambient_loopback_worker, "_looks_speechlike_block", side_effect=AssertionError("unneeded speech DSP")), \
             patch.object(ambient_loopback_worker, "_emit") as emit:
            self.assertEqual(ambient_loopback_worker.main(), 0)
        events = [call.args[0] for call in emit.call_args_list]
        self.assertTrue(any(event["type"] == "echo_reference" for event in events))
        self.assertFalse(any(event["type"] == "ambient_chunk" for event in events))
        self.assertTrue(all(len(call.args) == 1 for call in emit.call_args_list))

    def test_echo_monitor_starts_even_when_transcription_is_disabled(self):
        from jarvis.adaptation.ambient import SystemAudioSpeechMonitor
        config = types.SimpleNamespace(get=lambda key, default=None: {
            "ambient_learning.enabled": False,
            "ambient_learning.system_audio_transcription_enabled": False,
            "ambient_learning.system_echo_guard_enabled": True,
        }.get(key, default))
        monitor = SystemAudioSpeechMonitor(config, Mock())
        with patch("jarvis.adaptation.ambient.os.name", "nt"), patch("jarvis.adaptation.ambient.threading.Thread") as thread:
            monitor.start()
            thread.return_value.start.assert_called_once()
        monitor._ensure_stt = Mock(side_effect=AssertionError("transcription disabled"))
        monitor._transcribe(RecordingResult(np.ones(32000, dtype=np.float32), 16000))
        monitor._ensure_stt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
