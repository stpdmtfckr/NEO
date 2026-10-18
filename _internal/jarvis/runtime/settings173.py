"""NEO 173 settings: faster services, the NEO model only while it is needed, app skills, context.

``migrate`` adds missing keys. One default changes for users who kept it: the NEO model
stays in memory 5 minutes after the last answer instead of 15 (it is loaded again at the
wake word, while the user is still speaking).
"""
from __future__ import annotations

from typing import Any

SETTINGS_VERSION = 173

NEW_DEFAULTS: dict[str, dict[str, Any]] = {
    'performance': {
        'prewarm_connections': True,     # open connections of enabled services at the wake word (no data sent)
        'preload_on_wake': True,         # load the NEO model at the wake word
        'unload_models_in_games': True,  # free the memory for the game
    },
    'context': {'enabled': True, 'minutes': 10},   # «это», «туда», «тот файл»: in memory only
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
    if int(version or 0) < SETTINGS_VERSION:
        agent = data.get('llm_agent')
        if isinstance(agent, dict) and str(agent.get('keep_alive', '')) == '15m':
            agent['keep_alive'] = '5m'
        chat = data.get('local_chat')
        if isinstance(chat, dict) and str(chat.get('keep_alive', '')) == '10m':
            chat['keep_alive'] = '5m'
    data['settings_version'] = max(int(data.get('settings_version', 0) or 0), SETTINGS_VERSION)
