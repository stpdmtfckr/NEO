"""NEO 179: speaking answers aloud is gone (no Piper, no voices).

The old ``voice.tts`` section and the «say the match-ready notice aloud» switch are
removed from existing settings. The downloaded voices and Piper's own Python live in
``<NEO data>/models/tts``; bootstrap deletes that folder (``remove_old_voices``).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


def migrate(data: dict[str, Any], version: int) -> None:
    voice = data.get("voice")
    if isinstance(voice, dict):
        voice.pop("tts", None)
    match_ready = (data.get("gaming") or {}).get("match_ready") if isinstance(data.get("gaming"), dict) else None
    if isinstance(match_ready, dict):
        match_ready.pop("speak", None)


def remove_old_voices(data_root: str | Path) -> bool:
    """Delete Piper, its voices and their download status; True if something was removed."""
    folder = Path(data_root) / "models" / "tts"
    if not folder.exists():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    models = folder.parent
    try:
        if models.is_dir() and not any(models.iterdir()):
            models.rmdir()
    except OSError:
        pass
    return not folder.exists()


__all__ = ["migrate", "remove_old_voices"]
