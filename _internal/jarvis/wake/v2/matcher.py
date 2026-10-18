# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Two-stage wake matcher with adaptive thresholding.

Design
------
**Stage 1 (cheap gate).**  Runs on every partial ASR hypothesis.  Compares the
first 1-2 tokens only, using the consonant skeleton.  The matcher itself costs
only a few microseconds; the expensive part is producing repeated tiny-Whisper
ASR hypotheses. V7.6 therefore spaces repeated probes while keeping the first
voiced probe immediate.

**Stage 2 (confirm).**  Only for candidates that pass stage 1.  Runs the full
phonetic score plus position and command-context rules.

**Adaptive threshold.**  A fixed 0.92 ``difflib`` ratio cannot be right for
every microphone, room and speaker.  The gate keeps a short window of outcomes
and nudges the threshold: repeated user-corrected false accepts raise it,
repeated near-misses that the user immediately retries lower it.  Movement is
bounded so the gate can never drift open.

Positional rules matter as much as the score: a wake word is almost always the
first thing in an utterance, or follows a filler ("эй", "ok").  Accepting it
mid-sentence is how "сервис" and "Jeremy" got through in the shipped build.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .phonetics import _ONSET_EQUIV, CONFUSABLE_WORDS, fold, phonetic_score, skeleton

__all__ = ["WakeDecision", "WakeMatcher", "WakeConfig"]

_FILLERS = frozenset({
    "эй", "hey", "хей", "ok", "okay", "окей", "слушай", "привет", "hi", "hello",
})


@dataclass(slots=True)
class WakeConfig:
    phrase: str = "джарвис"
    # Irregular renderings that phonetics genuinely cannot reach. Keep this
    # SHORT -- if it grows past ~20 entries the phonetic model needs fixing,
    # not the list. (The shipped build had ~530.)
    exceptions: tuple[str, ...] = ()
    # Calibrated by tools/calibrate_wake.py against the 256 unique aliases in
    # the shipped default_settings.json (positives) and 6 788 real ASR lines
    # harvested from neo_jarvis.log (negatives).
    #
    #   thr 0.64 -> 2 false accepts / 6 788
    #   thr 0.66 -> 0 false accepts / 6 788, 59% of the alias list covered
    #
    # 0.66 is the lowest threshold with zero false accepts on real data. A
    # false wake opens the mic and can fire a command; a miss costs one repeat,
    # so the asymmetry is deliberate. Re-run the tool on your own log to retune.
    base_threshold: float = 0.66
    min_threshold: float = 0.60
    max_threshold: float = 0.85
    # Stage-1 gate is deliberately loose; stage 2 does the real work.
    gate_threshold: float = 0.55
    max_lead_tokens: int = 2
    adapt: bool = True
    adapt_window: int = 40


@dataclass(slots=True)
class WakeDecision:
    accepted: bool
    score: float
    threshold: float
    matched: str = ""
    reason: str = ""
    command_tail: str = ""
    elapsed_us: float = 0.0


class WakeMatcher:
    def __init__(self, config: WakeConfig | None = None) -> None:
        self.config = config or WakeConfig()
        self._threshold = self.config.base_threshold
        self._outcomes: deque[bool] = deque(maxlen=self.config.adapt_window)
        self._target_fold = fold(self.config.phrase)
        self._target_skel = skeleton(self.config.phrase)
        self._exception_folds = {fold(e) for e in self.config.exceptions if e.strip()}
        self.stage1_calls = 0
        self.stage2_calls = 0

    # -- stage 1 ------------------------------------------------------------ #

    def gate(self, partial_text: str) -> bool:
        """Microsecond pre-filter for high-frequency partial hypotheses.

        Two cheap conditions, in order:

        1. **Position.** A wake word is the first thing said, or follows a
           filler ("эй", "ok"). Scanning the whole utterance is what let
           ordinary Russian commands reach stage 2, since сколько / следующий /
           дискорд all begin with an onset in the /dʒ/ confusion class.
        2. **Skeleton shape.** Similar consonant length, and either a matching
           tail or a plausible onset.

        The gate must stay *loose* within those positions: anything it passes
        is scored properly by stage 2, so a false positive costs microseconds,
        while a false negative is a missed wake nothing downstream can recover.
        """
        self.stage1_calls += 1
        tokens = str(partial_text or "").split()
        if not tokens:
            return False

        # Candidate wake positions: index 0, plus any index reached through an
        # unbroken run of fillers.
        positions = [0]
        for i, token in enumerate(tokens[: self.config.max_lead_tokens + 1]):
            if token.casefold().strip(".,!?«»\"'") in _FILLERS:
                positions.append(i + 1)
            else:
                break

        target = self._target_skel
        tail = target[-2:]
        for pos in positions:
            if pos >= len(tokens):
                continue
            skel = skeleton(tokens[pos])
            if skel and self._gate_skeleton(skel, target, tail):
                return True
            # Two-token renderings ("джар вис", "Dzh arvis").
            if pos + 1 < len(tokens):
                joined = skeleton(tokens[pos] + tokens[pos + 1])
                if joined and self._gate_skeleton(joined, target, tail):
                    return True
        return False

    @staticmethod
    def _gate_skeleton(skel: str, target: str, tail: str) -> bool:
        if abs(len(skel) - len(target)) > 2:
            return False
        # The tail of the consonant skeleton is the most stable part across
        # languages: Jarvis / Dorvis / Garvis / Zarwis all end in the same
        # consonants even when the onset is mangled.
        if tail and skel.endswith(tail):
            return True
        # Otherwise require an onset from the class ASR confuses with /dʒ/.
        return bool(skel) and skel[0] in _ONSET_EQUIV

    # -- stage 2 ------------------------------------------------------------ #

    def match(self, text: str, *, allow_midsentence: bool = False) -> WakeDecision:
        started = time.perf_counter()
        self.stage2_calls += 1
        raw = str(text or "").strip()
        tokens = raw.split()
        if not tokens:
            return self._decision(False, 0.0, reason="empty", started=started)

        threshold = self._threshold
        best_score = 0.0
        best_match = ""
        best_end = 0

        limit = len(tokens) if allow_midsentence else min(
            len(tokens), self.config.max_lead_tokens + 2
        )
        for start in range(limit):
            token_clean = tokens[start].casefold().strip(".,!?«»\"'")
            if start and token_clean in _FILLERS:
                continue
            if not allow_midsentence and start > 0:
                # Only a filler may precede the wake word.
                preceding = [t.casefold().strip(".,!?") for t in tokens[:start]]
                if any(p not in _FILLERS for p in preceding):
                    break
            if token_clean in CONFUSABLE_WORDS:
                continue
            for span in (1, 2):
                if start + span > len(tokens):
                    continue
                candidate = " ".join(tokens[start : start + span])
                if fold(candidate) in self._exception_folds:
                    score = 0.97
                else:
                    score = phonetic_score(candidate, self.config.phrase)
                    if span > 1:
                        # A wake word is one word. Two-token renderings do
                        # occur ("джар вис"), but joining tokens also lets
                        # unrelated word pairs accumulate a plausible skeleton:
                        # replaying 6 717 real ASR lines, every false accept
                        # was a French "Je vous ..." collapsing to jvs~jrvs.
                        # A small span penalty removes that class while leaving
                        # genuine split renderings comfortably above threshold.
                        score *= 0.92
                if score > best_score:
                    best_score, best_match, best_end = score, candidate, start + span

        accepted = best_score >= threshold
        tail = " ".join(tokens[best_end:]).strip() if accepted else ""
        return self._decision(
            accepted, best_score, threshold=threshold, matched=best_match,
            reason="phonetic" if accepted else "below_threshold",
            command_tail=tail, started=started,
        )

    # -- combined ----------------------------------------------------------- #

    def check(self, text: str) -> WakeDecision:
        """Full pipeline: gate, then confirm. Use this from the audio worker."""
        if not self.gate(text):
            return WakeDecision(False, 0.0, self._threshold, reason="gated")
        return self.match(text)

    # -- adaptation --------------------------------------------------------- #

    def record_false_accept(self) -> None:
        """The user indicated NEO woke when it should not have."""
        self._outcomes.append(False)
        self._adapt(+0.02)

    def record_missed_wake(self) -> None:
        """The user had to repeat the wake word."""
        self._outcomes.append(False)
        self._adapt(-0.015)

    def record_correct(self) -> None:
        self._outcomes.append(True)

    def _adapt(self, delta: float) -> None:
        if not self.config.adapt:
            return
        self._threshold = max(
            self.config.min_threshold,
            min(self.config.max_threshold, self._threshold + delta),
        )

    @property
    def threshold(self) -> float:
        return round(self._threshold, 4)

    def stats(self) -> dict[str, float | int]:
        total = len(self._outcomes)
        good = sum(1 for o in self._outcomes if o)
        return {
            "threshold": self.threshold,
            "window": total,
            "accuracy": round(good / total, 3) if total else 0.0,
            "stage1_calls": self.stage1_calls,
            "stage2_calls": self.stage2_calls,
            "gate_rejection_rate": (
                round(1 - self.stage2_calls / self.stage1_calls, 3)
                if self.stage1_calls else 0.0
            ),
        }

    # -- helpers ------------------------------------------------------------ #

    def _decision(
        self, accepted: bool, score: float, *, threshold: float | None = None,
        matched: str = "", reason: str = "", command_tail: str = "",
        started: float = 0.0,
    ) -> WakeDecision:
        return WakeDecision(
            accepted=accepted,
            score=round(score, 4),
            threshold=round(threshold if threshold is not None else self._threshold, 4),
            matched=matched,
            reason=reason,
            command_tail=command_tail,
            elapsed_us=round((time.perf_counter() - started) * 1e6, 1) if started else 0.0,
        )


def migrate_alias_list(aliases: Iterable[str], phrase: str, threshold: float = 0.78) -> dict:
    """Report which of an existing alias list the phonetic model already covers.

    Run this against ``wake_word.aliases`` from ``default_settings.json`` to see
    how much of the hand-maintained list can simply be deleted.
    """
    covered: list[str] = []
    needed: list[str] = []
    dangerous: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        base = str(alias or "").strip().strip(".!?")
        if not base:
            continue
        key = base.casefold()
        if key in seen:
            continue
        seen.add(key)
        if key in CONFUSABLE_WORDS:
            dangerous.append(base)
            continue
        if phonetic_score(base, phrase) >= threshold:
            covered.append(base)
        else:
            needed.append(base)
    return {
        "total_unique": len(seen),
        "covered_by_phonetics": len(covered),
        "still_needed": needed,
        "should_be_removed": dangerous,
        "reduction_percent": round(100 * len(covered) / max(len(seen), 1), 1),
    }
