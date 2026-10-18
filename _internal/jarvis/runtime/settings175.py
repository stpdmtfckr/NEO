"""NEO 175 settings.

* Speech recognition goes back to the configured CPU threads (3): with 174's automatic
  6-8 threads a command decode showed as a 70% CPU spike. The faster decoding of 174
  (no repeated pass that cannot give a different text) stays.
"""
from __future__ import annotations

from typing import Any

SETTINGS_VERSION = 175


def migrate(data: dict[str, Any], version: int) -> None:
    voice = data.get('voice')
    if not isinstance(voice, dict):
        voice = data['voice'] = {}
    if int(version or 0) < SETTINGS_VERSION:
        voice['stt_threads_auto'] = False         # 174 turned it on by default; nobody chose it by hand
    voice.setdefault('stt_threads_auto', False)
    data['settings_version'] = max(int(data.get('settings_version', 0) or 0), SETTINGS_VERSION)
