"""NEO 172 settings: help with what is on the screen, streaming answers, memory offers, measured learning.

``migrate`` only adds missing keys; values the user changed are kept.
"""
from __future__ import annotations

from typing import Any

SETTINGS_VERSION = 172

NEW_DEFAULTS: dict[str, dict[str, Any]] = {
    # «что за ошибка?»: the search text is shown and sent only after «да»; the screenshot never leaves the PC.
    'help': {'confirm_before_search': True, 'use_screen_model': True},
    'local_chat': {'stream': True},
    'memory': {'propose': True},
    # Off until the user agrees: only executed commands and confirmed corrections, stored locally.
    'learning': {'collect_phrases': False},
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
