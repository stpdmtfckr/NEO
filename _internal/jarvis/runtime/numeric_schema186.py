"""One numeric contract for voice timing settings (SETTINGS185-RANGE-01).

The Settings widget, ConfigManager save and ConfigManager load use the same
range and precision, so the value shown after Apply is the value NEO uses and
reads back. Ranges follow what the recogniser accepts (audio_backend keeps at
least 0.45 s of end-of-speech silence); they were not widened arbitrarily.
"""
from __future__ import annotations

import math

# key: (minimum, maximum, decimals, step, default)
VOICE_NUMBERS: dict[str, tuple[float, float, int, float, float]] = {
    "voice.silence_seconds": (0.45, 1.80, 2, 0.05, 1.25),
    "wake_word.chunk_seconds": (0.75, 4.50, 2, 0.05, 2.2),
    "wake_word.silence_seconds": (0.12, 0.95, 2, 0.01, 0.30),
}


def clamp(key: str, value, default: float | None = None) -> float:
    low, high, decimals, _step, fallback = VOICE_NUMBERS[key]
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(fallback if default is None else default)
    if not math.isfinite(number):
        number = float(fallback if default is None else default)
    return round(max(low, min(high, number)), decimals)


def configure_spinbox(widget, key: str) -> None:
    low, high, decimals, step, _default = VOICE_NUMBERS[key]
    widget.setDecimals(decimals)
    widget.setRange(low, high)
    widget.setSingleStep(step)
