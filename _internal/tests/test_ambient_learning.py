from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis.adaptation.ambient import AmbientMemory, AmbientResourceGovernor
from jarvis.voice import _transcription_options


class _Config:
    def __init__(self, root: Path):
        self.paths = SimpleNamespace(root=root)
        self.data = {
            "ambient_learning.enabled": True,
            "ambient_learning.learned_term_min_count": 2,
            "ambient_learning.max_background_cpu_percent": 99.0,
            "ambient_learning.pause_while_assistant_busy": True,
            "ambient_learning.pause_during_game": False,
            "routing.semantic_dimension": 384,
        }

    def get(self, key, default=None):
        return self.data.get(key, default)


class AmbientLearningTests(unittest.TestCase):
    def test_repeated_new_word_becomes_learned_term(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _Config(Path(td))
            memory = AmbientMemory(cfg)
            self.assertTrue(memory.observe("Сегодня снова работаю над Phoenix backend", source="microphone", context={"folder": r"D:\\Projects\\Phoenix"}))
            # Different minute fingerprints are not needed because text/context differ.
            self.assertTrue(memory.observe("В Phoenix надо переделать авторизацию", source="microphone", context={"folder": r"D:\\Projects\\Phoenix"}))
            terms = {row["term"] for row in memory.learned_terms(limit=100)}
            self.assertIn("phoenix", terms)

    def test_relevant_context_returns_related_observation(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _Config(Path(td))
            memory = AmbientMemory(cfg)
            memory.observe("В проекте Phoenix сейчас проблема с авторизацией OAuth", source="microphone", context={"project": "Phoenix"})
            memory.observe("Сегодня обсуждали музыку и новый альбом", source="microphone", context={})
            result = memory.relevant_context("что думаешь про авторизацию Phoenix", limit=3)
            self.assertIn("Phoenix", result)
            self.assertIn("авториза", result.casefold())

    def test_duplicate_same_minute_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _Config(Path(td))
            memory = AmbientMemory(cfg)
            self.assertTrue(memory.observe("Мы обсуждаем Phoenix backend", source="microphone", context={}))
            self.assertFalse(memory.observe("Мы обсуждаем Phoenix backend", source="microphone", context={}))

    def test_ambient_decoder_is_cheap(self):
        opts = _transcription_options("ambient", "auto")
        self.assertEqual(opts.get("beam_size"), 1)
        self.assertEqual(opts.get("best_of"), 1)
        self.assertLessEqual(int(opts.get("max_new_tokens", 999)), 80)

    def test_communication_style_is_statistical(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _Config(Path(td))
            memory = AmbientMemory(cfg)
            for i in range(9):
                memory.observe(f"Ну давай обсудим Phoenix нормально раз {i}", source="microphone", context={})
            hint = memory.communication_style_hint()
            self.assertIn("Пользователь обычно говорит", hint)
            self.assertIn("разговор", hint)

    def test_busy_governor_denies_heavy_work(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = _Config(Path(td))
            governor = AmbientResourceGovernor(cfg, busy_provider=lambda: True)
            decision = governor.allow_heavy()
            self.assertFalse(decision.allow)
            self.assertEqual(decision.reason, "assistant_busy")


if __name__ == "__main__":
    unittest.main()
