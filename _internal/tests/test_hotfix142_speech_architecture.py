from __future__ import annotations

import json
import unittest
from pathlib import Path

from jarvis.runtime.speech_understanding import SpeechUnderstandingEngine
from jarvis.voice import SpeechRecognizer, _transcription_options


ROOT = Path(__file__).resolve().parents[1]


class _Config:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class _NoEntityScanEngine(SpeechUnderstandingEngine):
    def _refresh_entities(self):  # pragma: no cover - failure path is the assertion
        raise AssertionError("dynamic entity scan must not run for free conversation")


class Hotfix142SpeechArchitectureTests(unittest.TestCase):
    def test_shipped_wake_and_endpoint_defaults_are_cpu_bounded_and_quality_safe(self):
        data = json.loads((ROOT / "config" / "default_settings.json").read_text(encoding="utf-8"))
        wake = data["wake_word"]
        voice = data["voice"]
        self.assertGreaterEqual(data["settings_version"], 162)
        self.assertGreaterEqual(float(wake["fast_asr_probe_interval_seconds"]), 0.12)
        self.assertGreaterEqual(float(wake["fast_asr_probe_min_audio_seconds"]), 0.14)
        self.assertLessEqual(int(wake["fast_asr_probe_max_per_utterance"]), 2)
        self.assertGreaterEqual(float(voice["short_command_silence_seconds"]), 0.38)
        self.assertGreaterEqual(float(voice["normal_command_silence_seconds"]), 0.65)

    def test_normal_command_decode_uses_bounded_primary_beam(self):
        options = _transcription_options("command", "ru", 55)
        self.assertLessEqual(int(options["beam_size"]), 7)
        self.assertGreaterEqual(int(options["beam_size"]), 5)
        self.assertLessEqual(int(options["best_of"]), 5)
        self.assertFalse(bool(options["vad_filter"]))

    def test_fast_wake_language_follows_wake_phrase_not_command_language(self):
        recognizer = object.__new__(SpeechRecognizer)
        recognizer.config = _Config({
            "wake_word.fast_probe_language": "auto",
            "wake_word.phrase": "Jarvis",
            "voice.command_language": "ru",
        })
        self.assertEqual(recognizer._fast_wake_language(), "en")
        recognizer.config.values["wake_word.phrase"] = "Джарвис"
        self.assertEqual(recognizer._fast_wake_language(), "ru")

    def test_my_wave_truncation_is_repaired_only_in_command_shape(self):
        engine = SpeechUnderstandingEngine(_Config({"voice.speech_understanding_enabled": True}))
        result = engine.resolve("включи моя вон", language="ru", record=False)
        self.assertEqual(result.text.casefold(), "включи моя волна")
        self.assertTrue(any(x.reason == "known_media_phrase" for x in result.corrections))
        untouched = engine.resolve("она вон возле окна", language="ru", record=False)
        self.assertEqual(untouched.text, "она вон возле окна")
        valid_word = engine.resolve("включи моя воля", language="ru", record=False)
        self.assertEqual(valid_word.text, "включи моя воля")
        slang = engine.resolve("вруби моя вон", language="ru", record=False)
        self.assertEqual(slang.text, "вруби моя волна")

    def test_free_conversation_does_not_enumerate_dynamic_entities(self):
        engine = _NoEntityScanEngine(_Config({"voice.speech_understanding_enabled": True}))
        result = engine.resolve("как у тебя дела сегодня", language="ru", record=False)
        self.assertEqual(result.text, "как у тебя дела сегодня")

    def test_command_hotwords_are_balanced_not_phrase_specific(self):
        source = (ROOT / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
        block = source[source.index("def _command_hotwords"):source.index("def _rgba") ]
        self.assertNotIn("priority = (", block)
        self.assertIn("multilingual_hotwords(lang, 88)", block)
        self.assertIn("return (personal +", block)

    def test_wake_probe_path_keeps_one_fresh_pending_probe(self):
        source = (ROOT / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn("self._pending_wake_probe_recording", source)
        self.assertIn("if self._wake_probe_worker_active or self._wake_worker_active:", source)
        self.assertIn("self._launch_wake_probe(recording, pending_generation", source)
        # Regression guard: do not restore the old early return that silently
        # discarded a second probe while the first tiny-Whisper call was active.
        probe_block = source[source.index("def _audio_wake_probe_ready"):source.index("def _enqueue_wake_result")]
        guard_prefix = probe_block.split("if not self._wake_guard.accepts", 1)[0]
        self.assertNotIn("or self._wake_probe_worker_active", guard_prefix)


if __name__ == "__main__":
    unittest.main()
