"""NEO 185: the three voice tiers. Facts shown in Settings and in the installer come from here."""
from __future__ import annotations

TIERS = ('light', 'standard', 'heavy')
DEFAULT_TIER = 'standard'


def tier(config) -> str:
    value = str((config.get('response_voice', {}) or {}).get('tier', DEFAULT_TIER) or DEFAULT_TIER)
    return value if value in TIERS else DEFAULT_TIER


def store_for(profile, name):
    if name == 'light':
        from .system_store import SystemVoiceStore
        return SystemVoiceStore(profile)
    if name == 'heavy':
        from .model_store import HeavyModelStore
        return HeavyModelStore(profile)
    from .model_store import ModelStore
    return ModelStore(profile)
