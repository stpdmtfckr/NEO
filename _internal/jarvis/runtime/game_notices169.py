"""Game-session notices controlled from Settings → Games (NEO 169)."""
from __future__ import annotations

from typing import Any

_LANGUAGES = ("ru", "en", "de", "fr", "es", "it", "pt")

MESSAGES = {
    "session_start": ("🎮 Игровая сессия началась: {game}. Таймер идёт.", "🎮 Game session started: {game}. The timer is running.",
                      "🎮 Spielsitzung gestartet: {game}. Der Timer läuft.", "🎮 Session de jeu commencée : {game}. Le chrono tourne.",
                      "🎮 Sesión de juego iniciada: {game}. El temporizador está en marcha.", "🎮 Sessione di gioco iniziata: {game}. Il timer è partito.",
                      "🎮 Sessão de jogo iniciada: {game}. O cronómetro está a contar."),
    "session_end": ("🎮 Игровая сессия {game} завершена: {duration}.", "🎮 {game} session ended: {duration}.",
                    "🎮 Sitzung {game} beendet: {duration}.", "🎮 Session {game} terminée : {duration}.",
                    "🎮 Sesión de {game} terminada: {duration}.", "🎮 Sessione di {game} terminata: {duration}.",
                    "🎮 Sessão de {game} terminada: {duration}."),
}
_UNITS = {
    "ru": ("{h} ч {m} мин", "{m} мин"), "en": ("{h} h {m} min", "{m} min"), "de": ("{h} Std. {m} Min.", "{m} Min."),
    "fr": ("{h} h {m} min", "{m} min"), "es": ("{h} h {m} min", "{m} min"), "it": ("{h} h {m} min", "{m} min"),
    "pt": ("{h} h {m} min", "{m} min"),
}


def code_of(config: Any) -> str:
    code = str(config.get("app.language", "ru") or "ru").casefold().split("-")[0]
    return code if code in _LANGUAGES else "en"


def duration(seconds: int, code: str) -> str:
    minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    long_form, short_form = _UNITS.get(code, _UNITS["en"])
    return long_form.format(h=hours, m=minutes) if hours else short_form.format(m=minutes)


def message(key: str, code: str, /, **values: Any) -> str:
    row = MESSAGES[key]
    return row[_LANGUAGES.index(code) if code in _LANGUAGES else 1].format(**values)


def enabled(config: Any, key: str, default: bool) -> bool:
    return bool(config.get("gaming.enabled", True)) and bool(config.get("gaming.notifications." + key, default))
