from __future__ import annotations

import json
from pathlib import Path

from jarvis.wake_matcher import (
    match_wake_phrase,
    wake_alias_is_strong,
    wake_rescue_match,
)

ROOT = Path(__file__).resolve().parents[1]


def _settings() -> dict:
    return json.loads((ROOT / "config" / "default_settings.json").read_text(encoding="utf-8"))


def test_hotfix121_direct_aliases_are_phonetic_not_semantic_hallucinations():
    for value in ("Jarvis", "Jervis", "Dzharvis", "Жарвис", "Doorvis"):
        assert wake_alias_is_strong(value, "Jarvis"), value
    for value in (
        "There it is", "Thereitis", "Service", "Choice", "Coice",
        "Jeremy", "Doris", "Janice", "Let's go do this",
    ):
        assert not wake_alias_is_strong(value, "Jarvis"), value


def test_hotfix121_shipped_observations_are_partitioned_before_runtime_match():
    data = _settings()
    wake = data["wake_word"]
    all_aliases = list(wake.get("aliases", []))
    scoped = wake.get("language_aliases", {}) or {}
    all_aliases.extend(scoped.get("ru", []) or [])
    # NEO 178: the shipped wake word is NEO; the Jarvis corpus is retired.
    direct = [item for item in all_aliases if wake_alias_is_strong(item, wake.get("phrase", "NEO"))]
    assert any(str(x).strip(".!? ").casefold() == "нео" for x in direct)
    for bad in ("There it is", "Thereitis", "Service", "Choice", "Jeremy", "Doris", "не-а", "Неа", "не о том", "Jarvis"):
        matched, _tail, _score = match_wake_phrase(bad, wake.get("phrase", "NEO"), aliases=direct)
        assert not matched, bad


def test_hotfix121_weak_alias_is_only_a_rescue_signal():
    rescue = ["There it is", "Service", "Choice"]
    assert wake_rescue_match("There it is!", rescue)
    assert wake_rescue_match("service", rescue)
    assert not wake_rescue_match("Jarvis", rescue)


def test_hotfix121_keeps_fast_path_and_uses_existing_second_probe_for_verification():
    data = _settings()
    wake = data["wake_word"]
    assert float(wake["fast_asr_probe_min_audio_seconds"]) <= 0.22
    assert int(wake["fast_asr_probe_max_per_utterance"]) == 2

    source = (ROOT / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert "wake_alias_is_strong(value, phrase)" in source
    assert "_wake_rescue_pending_generation" in source
    assert 'language_override="en" if verify_rescue else None' in source
    assert "Suspicious wake ASR held for cross-language verification" in source
    assert '"source": "whisper-rescue-en"' in source
    assert '"text": phrase' in source


def test_hotfix121_fast_wake_api_supports_conditional_language_verification():
    source = (ROOT / "jarvis" / "voice.py").read_text(encoding="utf-8")
    assert "def transcribe_fast_wake_probe(self, recording: RecordingResult, *, language_override: str | None = None)" in source
    assert 'str(language_override).strip().casefold()' in source
