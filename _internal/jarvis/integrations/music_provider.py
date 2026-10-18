# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

from typing import Any

MUSIC_PROVIDERS = ("yandex_music", "spotify")


def active_music_provider(config: Any) -> str:
    """Return the one enabled desktop music provider.

    Settings UI keeps the flags mutually exclusive. This runtime helper also
    fail-closes hand-edited profiles: if several flags are true, only a valid
    enabled ``integrations.music.active_provider`` is accepted. Ambiguous
    profiles without such an explicit winner return an empty provider.
    """
    configured = str(config.get("integrations.music.active_provider", "") or "").strip().casefold()
    enabled = [name for name in MUSIC_PROVIDERS if bool(config.get(f"integrations.{name}.enabled", False))]
    if configured in MUSIC_PROVIDERS and configured in enabled:
        return configured
    if len(enabled) == 1:
        return enabled[0]
    if configured in MUSIC_PROVIDERS:
        return configured if bool(config.get(f"integrations.{configured}.enabled", False)) else ""
    if len(enabled) > 1:
        return ""
    return enabled[0] if enabled else ""


def provider_label(provider: str, code: str = "ru") -> str:
    language = str(code or "ru").casefold().split("-")[0]
    music = {"ru": "Музыка", "en": "Music", "de": "Musik", "fr": "Musique", "es": "Música", "it": "Musica", "pt": "Música"}
    return {
        "yandex_music": "Яндекс Музыка" if language == "ru" else "Yandex Music",
        "spotify": "Spotify",
    }.get(str(provider or "").casefold(), str(provider or music.get(language, "Music")))


def provider_disabled_message(provider: str, code: str = "ru") -> str:
    language = str(code or "ru").casefold().split("-")[0]
    label = provider_label(provider, language)
    templates = {
        "ru": "Интеграция {label} не включена. Включите её в Настройки → Медиа → {label}.",
        "en": "The {label} integration is disabled. Enable it in Settings → Media → {label}.",
        "de": "Die Integration von {label} ist deaktiviert. Aktiviere sie unter Einstellungen → Medien → {label}.",
        "fr": "L’intégration {label} est désactivée. Active-la dans Paramètres → Médias → {label}.",
        "es": "La integración de {label} está desactivada. Actívala en Ajustes → Medios → {label}.",
        "it": "L’integrazione di {label} è disattivata. Attivala in Impostazioni → Media → {label}.",
        "pt": "A integração do {label} está desativada. Ativa-a em Definições → Multimédia → {label}.",
    }
    return templates.get(language, templates["en"]).format(label=label)
