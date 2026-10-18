"""NEO 169 settings: local conversation, memory, file search, plugins, the NEO command model, tasks.

``migrate`` only adds missing keys; values the user changed are kept. Before the
first migration to 169 the old settings file is copied to backups/ (restore by
copying it back as settings.json).
"""
from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any

SETTINGS_VERSION = 169

NEW_DEFAULTS: dict[str, dict[str, Any]] = {
    'local_chat': {'enabled': True, 'questions': 'local', 'model': '', 'keep_alive': '10m', 'max_tokens': 600,
                   'num_ctx': 4096, 'temperature': 0.4, 'timeout_seconds': 120},
    'memory': {'enabled': True},
    'knowledge': {'folders': [], 'rescan_minutes': 30, 'pause_ms': 10, 'max_sources': 5},
    'plugins': {'approved': {}, 'settings': {}},
    'neo_model': {'enabled': True, 'execute_threshold': 0.9, 'suggest_threshold': 0.6, 'question_threshold': 0.97,
                  'epochs': 5, 'train_on_corrections': True,
                  'schedule': {'enabled': False, 'hour': 3, 'idle_minutes': 20}},
    'diagnostics': {'log_content': False},
    'gaming': {'launch_greetings_enabled': True,
               'notifications': {'session_start': False, 'session_end': False, 'replay_off_warning': True,
                                 'replay_unknown_warning': False, 'replay_changes': True},
               'instant_replay': {'toggle_hotkey': 'alt+shift+f10'}},
}


def _merge(target: dict[str, Any], defaults: dict[str, Any]) -> None:
    for key, value in defaults.items():
        if key not in target:
            target[key] = value if not isinstance(value, dict) else dict(value)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            _merge(target[key], value)


def migrate(data: dict[str, Any], version: int, *, settings_path: Path | None = None, backups: Path | None = None) -> None:
    if version < SETTINGS_VERSION and settings_path is not None and backups is not None and Path(settings_path).exists():
        backup = Path(backups) / f'settings-before-v169-v{version}.json'
        if not backup.exists():
            Path(backups).mkdir(parents=True, exist_ok=True)
            shutil.copy2(settings_path, backup)
    for section, values in NEW_DEFAULTS.items():
        current = data.get(section)
        if not isinstance(current, dict):
            current = data[section] = {}
        _merge(current, values)
    data['settings_version'] = max(int(data.get('settings_version', 0) or 0), SETTINGS_VERSION)
