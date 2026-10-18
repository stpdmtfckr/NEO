"""NEO 178: the wake word is «NEO» (Нео, Нио, Нэо, Neo, Nio…), Jarvis is retired.

Every spelling exists bare and with «.», «!» and «?» at the end, because speech
recognition writes the name with any of them. Matching (``jarvis.wake_matcher``)
accepts only whole words of this list; «не-а», «неа», «не», «нет», «но» never wake NEO.
"""
from __future__ import annotations

from typing import Any

PHRASE = "NEO"
SUFFIXES = ("", ".", "!", "?")
# Spellings speech recognition produces for «NEO» in the seven languages.
BASES = (
    "NEO", "Нео", "Нио", "Нэо", "Нейо", "Нийо", "Нёо", "Ньо", "Ніо",
    "Neo", "Nio", "Néo", "Neyo", "Nejo", "Nyo", "Neio", "Neoh",
    "Нел", "Ну", "Наю", "Нело",
)
OLD_PHRASES = {"", "jarvis", "джарвис", "neo jarvis", "нео джарвис"}


def aliases() -> list[str]:
    """Every alias: bare and with «.», «!», «?» at the end."""
    return [base + suffix for base in BASES for suffix in SUFFIXES]


def with_punctuation(values: list[str]) -> list[str]:
    """User aliases expanded with «.», «!», «?» (exact spellings kept, no duplicates)."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        base = str(value or "").strip().rstrip(".!?").strip()
        if not base:
            continue
        for suffix in SUFFIXES:
            item = base + suffix
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def migrate(data: dict[str, Any], version: int) -> None:
    """Replace the Jarvis wake word and its ~800 spellings with NEO (once, from 177 down)."""
    if int(version or 0) >= 178:
        return
    wake = data.setdefault("wake_word", {})
    if not isinstance(wake, dict):
        wake = data["wake_word"] = {}
    wake["phrase"] = PHRASE
    wake["acoustic_model_phrase"] = PHRASE
    wake["acoustic_enabled"] = False
    wake["fast_probe_language"] = "auto"     # the user's language, see voice.py
    wake["aliases"] = aliases()
    wake["language_aliases"] = {code: [] for code in ("ru", "en", "de", "fr", "es", "it", "pt")}
    for section in ("speech", "voice"):
        node = data.get(section)
        if isinstance(node, dict) and "wake_aliases" in node:
            node["wake_aliases"] = aliases()
    name = str(data.get("assistant_name", "") or "").strip()
    if name.casefold() in OLD_PHRASES:
        data["assistant_name"] = "NEO"
    data["settings_version"] = max(int(data.get("settings_version", 0) or 0), 178)


__all__ = ["BASES", "PHRASE", "aliases", "migrate", "with_punctuation"]
