"""NEO 171 settings: automatic Instant Replay at game start, structured web answers.

``migrate`` only adds missing keys; values the user changed are kept.
"""
from __future__ import annotations

from typing import Any

SETTINGS_VERSION = 171

NEW_DEFAULTS: dict[str, dict[str, Any]] = {
    # Off until the user ticks it in Settings → Games: NEO presses the NVIDIA overlay
    # toggle only when NVIDIA reports Instant Replay as off, and always says so in the chat.
    'gaming': {'instant_replay': {'auto_enable': False},
               # Web answers: the key points for the question, the full text on «подробнее».
               'web_knowledge': {'structured_answers': True}},
}


def _merge(target: dict[str, Any], defaults: dict[str, Any]) -> None:
    for key, value in defaults.items():
        if key not in target:
            target[key] = value if not isinstance(value, dict) else dict(value)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            _merge(target[key], value)


def migrate(data: dict[str, Any], version: int) -> None:
    for section, values in NEW_DEFAULTS.items():
        current = data.get(section)
        if not isinstance(current, dict):
            current = data[section] = {}
        _merge(current, values)
    data['settings_version'] = max(int(data.get('settings_version', 0) or 0), SETTINGS_VERSION)
