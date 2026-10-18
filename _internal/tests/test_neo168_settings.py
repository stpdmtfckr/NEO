"""NEO 168: settings migration and performance profiles."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from jarvis.runtime import settings168

ROOT = Path(__file__).resolve().parents[1]


class MigrationTests(unittest.TestCase):
    def test_old_install_keeps_its_values_and_gets_new_keys(self):
        data = {"settings_version": 165, "vision": {"max_edge": 900}, "wake_word": {"chunk_seconds": 1.1}}
        settings168.migrate(data, 165)
        self.assertEqual(data["vision"]["max_edge"], 900)
        self.assertEqual(data["wake_word"]["chunk_seconds"], 1.1)
        self.assertTrue(data["vision"]["card_format"])
        self.assertEqual(data["ui"]["hints_limit"], 60)
        self.assertEqual(data["input"]["typing_delay_ms"], 12)
        self.assertEqual(data["performance"]["applied_profile"], "balanced")
        self.assertGreaterEqual(data["settings_version"], 168)

    def test_choosing_a_profile_applies_it_once(self):
        data = {"performance": {"profile": "maximum", "applied_profile": "balanced"}}
        settings168.migrate(data, 168)
        self.assertEqual(data["vision"]["max_edge"], 1792)
        self.assertFalse(data["ollama"]["vision_low_priority"])
        data["vision"]["max_edge"] = 1500           # a manual tweak survives the next load
        settings168.migrate(data, 168)
        self.assertEqual(data["vision"]["max_edge"], 1500)

    def test_unknown_profile_falls_back_to_balanced(self):
        data = {"performance": {"profile": "turbo"}}
        settings168.apply_profile(data)
        self.assertEqual(data["performance"]["profile"], "balanced")

    def test_auto_profile_follows_the_machine(self):
        self.assertEqual(settings168.auto_profile(4, 16), "low")
        self.assertEqual(settings168.auto_profile(8, 6), "low")
        self.assertEqual(settings168.auto_profile(6, 16), "balanced")
        self.assertEqual(settings168.auto_profile(8, 16), "fast")
        self.assertEqual(settings168.auto_profile(16, 32), "maximum")
        data = {"performance": {"profile": "auto"}}
        self.assertEqual(settings168.apply_profile(data, cpu_count=4, memory_gb=8), "low")
        self.assertEqual(data["performance"]["applied_profile"], "auto:low")
        self.assertIsNone(settings168.apply_profile(data, cpu_count=4, memory_gb=8))

    def test_profiles_cover_the_same_keys(self):
        keys = {frozenset(values) for values in settings168.PROFILES.values()}
        self.assertEqual(len(keys), 1)

    def test_packaged_defaults_match_the_migration(self):
        defaults = json.loads((ROOT / "config" / "default_settings.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(defaults["settings_version"], settings168.SETTINGS_VERSION)
        for section in ("vision", "input", "performance"):
            for key, value in settings168.NEW_DEFAULTS[section].items():
                if key != "applied_profile":
                    self.assertEqual(defaults[section][key], value, f"{section}.{key}")


if __name__ == "__main__":
    unittest.main()
