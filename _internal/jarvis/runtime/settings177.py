"""NEO 177 settings (missing keys only; nothing the user chose is changed).

* learning.remember_resolved — a typo or phrasing NEO understood once is answered at once
  next time (only short commands and greetings; Settings → «Запоминать понятые опечатки»).
* integrations.yandex_music.network_recovery_seconds — after this many seconds without the
  network a started Yandex station gives up instead of starting much later on its own.
* ui.robot_accessory_cigarette — the new cigarette option next to the coffee (off).
"""
from __future__ import annotations

from typing import Any

SETTINGS_VERSION = 177


def _section(data: dict[str, Any], *path: str) -> dict[str, Any]:
    node = data
    for key in path:
        value = node.get(key)
        if not isinstance(value, dict):
            value = node[key] = {}
        node = value
    return node


def migrate(data: dict[str, Any], version: int) -> None:
    del version
    _section(data, 'learning').setdefault('remember_resolved', True)
    _section(data, 'integrations', 'yandex_music').setdefault('network_recovery_seconds', 60)
    ui = _section(data, 'ui')
    ui.setdefault('robot_accessory_cigarette', False)
    data['settings_version'] = max(int(data.get('settings_version', 0) or 0), SETTINGS_VERSION)
