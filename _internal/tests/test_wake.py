# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Tests for wake detection V2.

Run with::

    python3 -m tests.test_wake
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.wake.v2 import (  # noqa: E402
    CONFUSABLE_WORDS, WakeConfig, WakeMatcher, migrate_alias_list,
    fold, phonetic_score, skeleton, syllables,
)

PHRASE = "джарвис"

# Real ASR renderings taken from the shipped wake_word.aliases list.
REAL_RENDERINGS = [
    "Джарвис", "Jarvis", "Dzharvis", "Джарвиз", "Жарвис", "Jervis", "Jarviz",
    "Dżarwis", "Džarvis", "Zharvis", "Гарвис", "Дорвис", "Джарвиса",
    "Джар вис", "Dzh arvis", "Jarves", "Jarwis", "джервис",
]

# Utterances that must never wake NEO. The French cluster is not invented:
# replaying 6 717 asr=[...] lines from neo_jarvis.log, every false accept
# before the span penalty was a "Je vous ..." phrase.
MUST_NOT_WAKE = [
    "Service", "Jeremy", "Janice", "Doris", "сервис",
    "Je vous dis à tous les jours.", "Je vous parlez en ce moment.",
    "Já vi, essa é a prebego.", "J'ai fait",
    "Warframe", "какая погода", "поставь музыку", "Tamsızlı gösteriyor",
    "включи следующий трек", "There it is", "открой дискорд",
]


class TestPhonetics(unittest.TestCase):
    def test_alphabets_converge(self):
        self.assertEqual(fold("Джарвис"), fold("Jarvis"))
        self.assertEqual(fold("Dzharvis"), fold("Джарвис"))

    def test_skeleton_drops_vowels(self):
        self.assertEqual(skeleton("Jarvis"), skeleton("Jorvos"))
        self.assertNotIn("a", skeleton("Jarvis"))

    def test_syllable_count(self):
        self.assertEqual(syllables("Jarvis"), 2)
        self.assertEqual(syllables("Джарвис"), 2)

    def test_confusables_score_zero(self):
        for word in ("Service", "Jeremy", "Doris"):
            self.assertEqual(phonetic_score(word, PHRASE), 0.0)


class TestMatcher(unittest.TestCase):
    def setUp(self) -> None:
        self.matcher = WakeMatcher(WakeConfig(phrase=PHRASE))

    def test_accepts_real_asr_renderings(self):
        missed = [r for r in REAL_RENDERINGS if not self.matcher.check(r).accepted]
        self.assertEqual(missed, [], f"missed: {missed}")

    def test_rejects_everything_that_must_not_wake(self):
        woke = [t for t in MUST_NOT_WAKE if self.matcher.check(t).accepted]
        self.assertEqual(woke, [], f"false accepts: {woke}")

    def test_command_tail_is_extracted(self):
        decision = self.matcher.check("Джарвис, включи музыку")
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.command_tail, "включи музыку")

    def test_leading_filler_is_allowed(self):
        decision = self.matcher.check("эй джарвис открой дискорд")
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.command_tail, "открой дискорд")

    def test_midsentence_wake_is_rejected_by_default(self):
        # "поставь музыку джарвис" is a command that happens to name NEO, not
        # a wake. Accepting it mid-sentence is how ordinary words slipped
        # through the shipped alias list.
        self.assertFalse(self.matcher.match("поставь музыку джарвис").accepted)
        self.assertTrue(
            self.matcher.match("поставь музыку джарвис", allow_midsentence=True).accepted
        )

    def test_split_token_rendering_still_accepted(self):
        for text in ("джар вис", "Dzh arvis"):
            self.assertTrue(self.matcher.check(text).accepted, text)

    def test_gate_never_rejects_something_stage2_would_accept(self):
        """A stage-1 false negative is unrecoverable, so it must not happen."""
        loose = WakeMatcher(WakeConfig(phrase=PHRASE))
        for text in REAL_RENDERINGS:
            with self.subTest(text=text):
                if loose.match(text).accepted:
                    self.assertTrue(loose.gate(text), f"gate dropped {text!r}")

    def test_gate_discards_ordinary_speech_before_stage_2(self):
        """MUST_NOT_WAKE is adversarial -- most entries deliberately share the
        /dʒ/ onset class, so the gate can only drop ~25% of it. On the 6 717
        real ASR lines harvested from neo_jarvis.log the gate drops 50%, which
        is what halves stage-2 cost in production."""
        matcher = WakeMatcher(WakeConfig(phrase=PHRASE))
        ordinary = [
            "включи следующий трек", "какая погода", "открой дискорд",
            "поставь музыку погромче", "сколько времени", "закрой браузер",
            "сделай скриншот", "температура процессора",
        ]
        for text in ordinary * 5:
            matcher.check(text)
        self.assertGreater(matcher.stats()["gate_rejection_rate"], 0.5)


class TestAdaptation(unittest.TestCase):
    def test_threshold_moves_and_stays_bounded(self):
        matcher = WakeMatcher(WakeConfig(phrase=PHRASE))
        start = matcher.threshold
        for _ in range(50):
            matcher.record_false_accept()
        self.assertGreater(matcher.threshold, start)
        self.assertLessEqual(matcher.threshold, matcher.config.max_threshold)
        for _ in range(200):
            matcher.record_missed_wake()
        self.assertGreaterEqual(matcher.threshold, matcher.config.min_threshold)

    def test_adaptation_can_be_disabled(self):
        matcher = WakeMatcher(WakeConfig(phrase=PHRASE, adapt=False))
        start = matcher.threshold
        for _ in range(20):
            matcher.record_false_accept()
        self.assertEqual(matcher.threshold, start)


class TestMigration(unittest.TestCase):
    def test_report_flags_dangerous_aliases(self):
        report = migrate_alias_list(
            ["Джарвис", "Jarvis", "Jeremy", "Doris", "Ciao tout"], PHRASE
        )
        self.assertIn("Jeremy", report["should_be_removed"])
        self.assertIn("Doris", report["should_be_removed"])
        self.assertGreaterEqual(report["covered_by_phonetics"], 2)

    def test_shipped_alias_list_shrinks_substantially(self):
        settings = Path(__file__).resolve().parents[1] / "config" / "default_settings.json"
        if not settings.is_file():
            self.skipTest("shipped settings not available in this checkout")
        import json
        aliases = json.loads(settings.read_text(encoding="utf-8"))["wake_word"]["aliases"]
        # NEO 178: ~800 Jarvis spellings were replaced by a short NEO list.
        self.assertLess(len(aliases), 100)
        self.assertFalse([a for a in aliases if "arv" in a.casefold() or "арв" in a.casefold()])


if __name__ == "__main__":
    unittest.main(verbosity=2)
