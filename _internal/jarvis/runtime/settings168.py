"""NEO 168 settings: new keys for on-screen control and performance profiles.

``migrate(data, version)`` only adds missing keys, so values the user already
changed are kept. A performance profile (Low / Balanced / Fast / Maximum /
Auto) writes its values once, when the user picks it; after that each value
can still be changed by hand until another profile is chosen.
"""
from __future__ import annotations

import os
from typing import Any

SETTINGS_VERSION = 168

NEW_DEFAULTS: dict[str, dict[str, Any]] = {
    "vision": {"card_format": True, "ocr_assist": True, "warm_seconds": 120, "max_edge": 1344,
               "card_max_tokens": 700, "analyze_scope": "active_monitor"},
    "ollama": {"vision_low_priority": True},
    "input": {"typing_delay_ms": 12},
    "ui": {"hints_limit": 60, "hints_seconds": 30},
    "wake_word": {"bounded_decode_window": True, "conversation_backoff": True},
    "performance": {"profile": "balanced", "applied_profile": ""},
}

PROFILES: dict[str, dict[str, Any]] = {
    # Weak laptop or a game running: listen less often, smaller pictures, unload the vision model at once.
    "low": {"wake_word.chunk_seconds": 1.2, "vision.max_edge": 1024, "vision.card_max_tokens": 450,
            "vision.warm_seconds": 0, "ollama.vision_low_priority": True, "ui.hints_limit": 40},
    "balanced": {"wake_word.chunk_seconds": 0.95, "vision.max_edge": 1344, "vision.card_max_tokens": 700,
                 "vision.warm_seconds": 120, "ollama.vision_low_priority": True, "ui.hints_limit": 60},
    "fast": {"wake_word.chunk_seconds": 0.8, "vision.max_edge": 1344, "vision.card_max_tokens": 700,
             "vision.warm_seconds": 600, "ollama.vision_low_priority": True, "ui.hints_limit": 80},
    # Strong PC: sharper pictures, longer answers, the vision model stays loaded for 30 minutes.
    "maximum": {"wake_word.chunk_seconds": 0.7, "vision.max_edge": 1792, "vision.card_max_tokens": 1000,
                "vision.warm_seconds": 1800, "ollama.vision_low_priority": False, "ui.hints_limit": 100},
}
PROFILE_NAMES = ("low", "balanced", "fast", "maximum", "auto")


def _memory_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().total / 1024 ** 3
    except Exception:
        return 0.0


def auto_profile(cpu_count: int | None = None, memory_gb: float | None = None) -> str:
    """Pick a profile from the number of CPU threads and the installed memory."""
    cpus = int(cpu_count if cpu_count is not None else (os.cpu_count() or 4))
    memory = float(memory_gb if memory_gb is not None else _memory_gb())
    if cpus <= 4 or (0 < memory < 8):
        return "low"
    if cpus >= 16 and memory >= 32:
        return "maximum"
    if cpus >= 8 and memory >= 16:
        return "fast"
    return "balanced"


def resolve(profile: str, **machine: Any) -> str:
    name = str(profile or "balanced").strip().casefold()
    if name == "auto":
        return auto_profile(**machine)
    return name if name in PROFILES else "balanced"


def apply_profile(data: dict[str, Any], **machine: Any) -> str | None:
    """Write the chosen profile's values once; returns the applied profile or None if nothing changed."""
    performance = data.setdefault("performance", {})
    chosen = str(performance.get("profile", "balanced") or "balanced").casefold()
    if chosen not in PROFILE_NAMES:
        chosen = performance["profile"] = "balanced"
    if performance.get("applied_profile") == chosen and chosen != "auto":
        return None
    name = resolve(chosen, **machine)
    if chosen == "auto" and performance.get("applied_profile") == f"auto:{name}":
        return None
    for dotted, value in PROFILES[name].items():
        section, key = dotted.split(".", 1)
        data.setdefault(section, {})[key] = value
    performance["applied_profile"] = f"auto:{name}" if chosen == "auto" else chosen
    return name


def migrate(data: dict[str, Any], version: int) -> None:
    for section, values in NEW_DEFAULTS.items():
        target = data.setdefault(section, {})
        if not isinstance(target, dict):
            target = data[section] = {}
        for key, value in values.items():
            target.setdefault(key, value)
    if version < SETTINGS_VERSION:
        # Existing installs keep their tuned values: record Balanced as applied instead of overwriting.
        data["performance"].setdefault("applied_profile", "")
        if not data["performance"]["applied_profile"] and data["performance"].get("profile", "balanced") == "balanced":
            data["performance"]["applied_profile"] = "balanced"
    apply_profile(data)
    data["settings_version"] = max(int(data.get("settings_version", 0) or 0), SETTINGS_VERSION)
