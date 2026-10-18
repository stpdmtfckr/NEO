# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Hard provenance boundary between executable user input and passive observations.

Only explicit user-control transports may enter Agent/tool execution.  Ambient
microphone/system observations remain data for memory/understanding and can never
be promoted into commands merely by passing a different source string.
"""

EXECUTABLE_INPUT_SOURCES = frozenset({
    "text",
    "voice",
    "telegram",
    "ui",
    "hotkey",
})

NON_EXECUTABLE_INPUT_SOURCES = frozenset({
    "ambient",
    "ambient_microphone",
    "system_audio",
    "system_audio_activity",
    "system_context",
    "loopback",
    "wasapi_loopback",
})


def normalize_input_source(source: str | None) -> str:
    value = str(source or "text").strip().casefold().replace("-", "_")
    return value or "text"


def is_executable_input_source(source: str | None) -> bool:
    value = normalize_input_source(source)
    if value in NON_EXECUTABLE_INPUT_SOURCES:
        return False
    # Unknown sources fail closed. New control transports must be consciously
    # added to EXECUTABLE_INPUT_SOURCES instead of silently gaining tool access.
    return value in EXECUTABLE_INPUT_SOURCES


def is_passive_observation_source(source: str | None) -> bool:
    return normalize_input_source(source) in NON_EXECUTABLE_INPUT_SOURCES


__all__ = [
    "EXECUTABLE_INPUT_SOURCES",
    "NON_EXECUTABLE_INPUT_SOURCES",
    "normalize_input_source",
    "is_executable_input_source",
    "is_passive_observation_source",
]
