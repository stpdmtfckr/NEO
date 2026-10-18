"""NEO 184 settings: the translation section (missing keys only; the settings version stays)."""
from __future__ import annotations

from typing import Any


def migrate(data: dict[str, Any], version: int = 0) -> None:
    from jarvis.runtime.translation184 import DEFAULTS
    section = data.get('translation')
    if not isinstance(section, dict):
        section = data['translation'] = {}
    for key, value in DEFAULTS.items():
        section.setdefault(key, value)

    gaming = data.get('gaming')
    if not isinstance(gaming, dict):
        gaming = data['gaming'] = {}
    notifications = gaming.get('notifications')
    if not isinstance(notifications, dict):
        notifications = gaming['notifications'] = {}
    notifications.setdefault('nvidia_enabled', True)
    notifications.setdefault('nvidia_startup', True)
    replay = gaming.get('instant_replay')
    if not isinstance(replay, dict):
        replay = gaming['instant_replay'] = {}
    replay.setdefault('auto_launch_app', False)
    # Retire the old registry probe; keep hotkeys, notices and automatic choices.
    replay.pop('registry_values', None)

    streaming = data.get('streaming')
    if not isinstance(streaming, dict):
        streaming = data['streaming'] = {}
    for key, value in {
        'enabled': False,
        'host': '127.0.0.1',
        'port': 4455,
        'password_encrypted': '',
        'executable': '',
        'auto_launch_on_game': False,
        'auto_replay_on_game': False,
        'microphone_input': '',
        'preferred_scene': '',
    }.items():
        streaming.setdefault(key, value)
