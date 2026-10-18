"""One-time migration from legacy all-model switches to image-only startup."""
from __future__ import annotations


def migrate_vision_startup(ollama: dict, *, settings_version: int) -> None:
    """Repair old 163/foreign 164 profiles, retaining an explicit vision opt-out.

    Older all-model ``auto_start=False`` settings also disabled the newly split
    image service. Once this migration is recorded, a new deliberate startup
    choice survives subsequent reloads. Speech model settings are never touched.
    """
    ollama.setdefault("vision_enabled", True)
    ollama.update(requests_enabled=False, preload_model=False, vision_keep_alive=0,
                  start_tray_app=False, auto_pull_models=False)
    try:
        migrated = int(ollama.get("startup_policy_version", 0) or 0)
    except (TypeError, ValueError):
        migrated = 0
    if settings_version < 165 and migrated < 145 and bool(ollama["vision_enabled"]):
        # Never overwrite an explicit startup opt-out, even in legacy profiles.
        ollama.setdefault("auto_start", True)
        ollama.setdefault("auto_install", True)
    ollama["startup_policy_version"] = max(145, migrated)
