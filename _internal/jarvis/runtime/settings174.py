"""NEO 174 settings: faster speech recognition and understanding (missing keys only)."""
from __future__ import annotations

from typing import Any

SETTINGS_VERSION = 174

NEW_DEFAULTS: dict[str, dict[str, Any]] = {
    'voice': {
        # Decode a command on more CPU cores when the computer has them (never fewer than
        # ``stt_cpu_threads``). Only while decoding; idle load is unchanged.
        'stt_threads_auto': False,       # NEO 175: off (CPU spikes); the configured thread count is used
    },
    'performance': {
        'pooled_yandex': True,           # Yandex Music API over one kept-alive connection
    },
    'routing': {
        'typo_before_model': True,       # a typo in a command is repaired locally before the agent model
    },
    'local_chat': {
        'direct_chat': True,             # plain conversation goes straight to the streaming chat
        'direct_confidence': 0.9,        # ... or when the command model is this sure it is no command
        'live_small_talk': True,         # feelings and chit-chat answered live when the chat is ready
    },
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
