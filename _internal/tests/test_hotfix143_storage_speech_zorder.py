from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from jarvis.config import ConfigManager
from jarvis.runtime.speech_understanding import SpeechUnderstandingEngine, _Entity
from jarvis.stt_worker import _allow_cross_language_rescue, _prefer_adaptive_candidate
from jarvis.voice import _transcription_options

ROOT = Path(__file__).resolve().parents[1]


from tests.release_contracts import assert_existing_window_visibility

class _Config:
    def __init__(self, values=None):
        self.values = values or {}
    def get(self, key, default=None):
        return self.values.get(key, default)


class Hotfix143Tests(unittest.TestCase):
    def _engine(self):
        cfg = _Config({
            "voice.speech_understanding_enabled": True,
            "voice.speech_understanding_contextual_entities": True,
            "voice.speech_understanding_entity_threshold": 0.86,
            "voice.speech_understanding_entity_margin": 0.07,
            "voice.speech_adaptive_rewrite_enabled": False,
        })
        engine = SpeechUnderstandingEngine(cfg)
        engine._refresh_entities = lambda: [
            _Entity("вон", ("вон", "волна"), "ambient", False, True, "learned"),
            _Entity("NEO", ("neo", "нео"), "installed", False, False, "application"),
            _Entity("Telegram", ("telegram", "телеграм"), "builtin", False, False, "media"),
            _Entity("Discord", ("discord", "дискорт", "дискорд"), "builtin", False, False, "media"),
        ]
        return engine

    def test_correct_primary_media_payload_is_never_rewritten_by_context(self):
        engine = self._engine()
        for text in ("Включи моя волна.", "Включи мои треки.", "Включи музыку потише"):
            with self.subTest(text=text):
                self.assertEqual(engine.resolve(text, language="ru", record=False).text, text)

    def test_explicit_provider_can_still_be_canonicalized(self):
        result = self._engine().resolve("включи моя волна в дискорт", language="ru", record=False)
        self.assertEqual(result.text, "включи моя волна в Discord")

    def test_generic_application_command_is_constrained_but_play_payload_is_not(self):
        engine = self._engine()
        self.assertIn("application", engine._slot_kinds("открой дискорд", "ru"))
        self.assertEqual(engine._slot_kinds("включи моя волна", "ru"), set())

    def test_russian_plain_command_never_triggers_english_rescue(self):
        self.assertFalse(_allow_cross_language_rescue("включи моя волна", "ru"))
        self.assertFalse(_allow_cross_language_rescue("включи My Wave", "ru"))
        primary = ("включи моя волна", 0.10, True, {"weird_ratio": 0.0, "command_vocab": 0.5})
        english = ("play my wave", 0.25, False, {"weird_ratio": 0.0, "command_vocab": 0.9})
        self.assertFalse(_prefer_adaptive_candidate(primary, english, primary_language="ru"))

    def test_command_decode_restores_quality_baseline(self):
        options = _transcription_options("command", "ru", 55)
        self.assertEqual(options["beam_size"], 7)
        self.assertEqual(options["best_of"], 5)
        self.assertGreaterEqual(options["patience"], 1.14)

    def test_hotkey_is_z_order_only_not_hide_or_tray(self):
        # Real hide stops paint work; the existing HWND and audio remain alive.
        assert_existing_window_visibility(self, (ROOT / "jarvis/ui/main_window.py").read_text(encoding="utf-8"))

    def test_shipped_storage_is_one_local_root(self):
        data = json.loads((ROOT / "config" / "default_settings.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(data["settings_version"], 162)
        self.assertEqual(data["storage"]["data_dir"], "")
        self.assertLessEqual(float(data["wake_word"]["fast_asr_probe_min_audio_seconds"]), 0.16)
        self.assertLessEqual(float(data["wake_word"]["fast_asr_probe_interval_seconds"]), 0.15)

    def test_roaming_tree_migrates_into_local_root(self):
        defaults = ROOT / "config" / "default_settings.json"
        old_appdata, old_local = os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp); roaming = base / "roaming"; local = base / "local"
                legacy = roaming / "NeoJarvis"; (legacy / "logs").mkdir(parents=True)
                (local / "NeoJarvis" / "runtime").mkdir(parents=True)
                (legacy / "settings.json").write_text('{"settings_version":161,"assistant_name":"MIGRATED"}', encoding="utf-8")
                (legacy / "logs" / "legacy.log").write_text("ok", encoding="utf-8")
                os.environ["APPDATA"] = str(roaming); os.environ["LOCALAPPDATA"] = str(local)
                cfg = ConfigManager(defaults)
                self.assertEqual(cfg.paths.root, local / "NeoJarvis")
                self.assertEqual(cfg.get("assistant_name"), "MIGRATED")
                self.assertTrue((cfg.paths.logs / "legacy.log").exists())
                self.assertFalse(legacy.exists())
        finally:
            if old_appdata is None: os.environ.pop("APPDATA", None)
            else: os.environ["APPDATA"] = old_appdata
            if old_local is None: os.environ.pop("LOCALAPPDATA", None)
            else: os.environ["LOCALAPPDATA"] = old_local


if __name__ == "__main__":
    unittest.main()
