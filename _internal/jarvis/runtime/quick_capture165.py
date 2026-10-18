"""Wake-less quick capture commands: clip, record, stop recording, screenshot.

Like the quick media transport words, these run on the permanent wake stream,
so only a complete short utterance counts («клип», «запись», «стоп запись»,
«скриншот»); a longer sentence that merely contains the word never matches.
Small misspellings from the tiny wake model («клипп», «скриншод») are tolerated
with the same keyboard/phonetic edit costs as typed typos. The result is the
canonical command of the selected language, which then takes the ordinary
Agent path (ownership, risk policy, localized reply).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re

from jarvis.runtime.natural_commands import LANGUAGES, norm

_ROOT = Path(__file__).resolve().parents[2]

QUICK = {
    "clip": {
        "ru": ("клип", "клипани", "клипни", "сделай клип", "сохрани клип", "клип нвидиа", "клип nvidia"),
        "en": ("clip", "clip it", "clip that", "save clip", "save a clip", "make a clip"),
        "de": ("clip", "clip speichern", "mach einen clip"),
        "fr": ("clip", "fais un clip", "sauvegarde le clip"),
        "es": ("clip", "haz un clip", "guarda el clip"),
        "it": ("clip", "fai una clip", "salva la clip"),
        "pt": ("clip", "faz um clip", "guarda o clip"),
    },
    "record_start": {
        "ru": ("запись", "начни запись", "записывай", "включи запись", "старт запись"),
        "en": ("record", "recording", "start recording"),
        "de": ("aufnahme", "aufnahme starten", "nimm auf", "starte die aufnahme"),
        "fr": ("enregistrement", "lance l’enregistrement", "commence à enregistrer"),
        "es": ("graba", "grabar", "grabación", "inicia la grabación"),
        "it": ("registra", "registrazione", "avvia la registrazione"),
        "pt": ("grava", "gravar", "gravação", "inicia a gravação"),
    },
    "record_stop": {
        "ru": ("стоп запись", "запись стоп", "останови запись", "выключи запись", "закончи запись", "хватит записывать"),
        "en": ("stop recording", "end recording", "recording off"),
        "de": ("aufnahme stoppen", "stopp aufnahme", "aufnahme beenden", "stoppe die aufnahme"),
        "fr": ("stop enregistrement", "arrête l’enregistrement", "arrête d’enregistrer"),
        "es": ("stop grabación", "detén la grabación", "para la grabación", "deja de grabar"),
        "it": ("stop registrazione", "ferma la registrazione", "smetti di registrare"),
        "pt": ("stop gravação", "para a gravação", "para de gravar"),
    },
    "screenshot": {
        "ru": ("скриншот", "скрин", "сделай скрин", "сделай скриншот", "заскринь", "скриншот окна"),
        "en": ("screenshot", "take a screenshot", "screenshot this"),
        "de": ("screenshot", "mach einen screenshot"),
        "fr": ("capture d’écran", "capture", "fais une capture"),
        "es": ("captura", "captura de pantalla", "haz una captura"),
        "it": ("screenshot", "fai uno screenshot"),
        "pt": ("captura de ecrã", "captura de tela", "screenshot"),
    },
}
_INTENT = {"clip": "capture.clip", "record_start": "capture.record_start",
           "record_stop": "capture.record_stop", "screenshot": "screen.capture_window"}
_FILLERS = {
    "ru": "ну давай пожалуйста плиз эй быстро сейчас джарвис jarvis нео neo",
    "en": "please hey now quick jarvis neo",
    "de": "bitte hey jetzt schnell jarvis neo",
    "fr": "s’il te plaît stp hé vite jarvis neo",
    "es": "por favor oye ahora rápido jarvis neo",
    "it": "per favore ehi adesso subito jarvis neo",
    "pt": "por favor ei agora rápido jarvis neo",
}


@dataclass(frozen=True)
class QuickCapture:
    action: str   # clip / record_start / record_stop / screenshot
    command: str  # canonical command in the selected language
    heard: str


def _clean(text: str, code: str) -> str:
    value = norm(str(text or "")).replace("'", "’")
    value = re.sub(r"[^\w’\s]+", " ", value)
    fillers = set(_FILLERS.get(code, "").split())
    words = [w for w in value.split() if w not in fillers]
    return " ".join(words)


@lru_cache(maxsize=7)
def _phrases(code: str) -> tuple[tuple[str, str], ...]:
    return tuple((_clean(p, code), action) for action, rows in QUICK.items() for p in rows.get(code, ()))


@lru_cache(maxsize=1)
def _canonical() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for name in ("capture.json", "productivity.json"):
        path = _ROOT / "config" / "canonical_phrases" / name
        rows.update(json.loads(path.read_text(encoding="utf-8")))
    return rows


def canonical_command(action: str, code: str) -> str:
    return _canonical()[_INTENT[action]][LANGUAGES.index(code)]


def parse_quick_capture(text: str, code: str) -> QuickCapture | None:
    if code not in LANGUAGES:
        return None
    value = _clean(text, code)
    if not value or len(value) > 40:
        return None
    exact = {phrase: action for phrase, action in _phrases(code)}
    action = exact.get(value)
    if action is None:
        # A misheard short command: one clear winner within the typo budget.
        from jarvis.runtime.command_typos165 import budget, distance
        limit = min(budget(value.replace(" ", "")), 1.0)
        scored = sorted((distance(value, phrase, code, limit), action) for phrase, action in _phrases(code)
                        if abs(len(phrase) - len(value)) <= 2)
        scored = [row for row in scored if row[0] <= limit]
        if not scored or len(value.replace(" ", "")) < 4:
            return None
        if len(scored) > 1 and scored[1][1] != scored[0][1] and scored[1][0] - scored[0][0] < 0.3:
            return None
        action = scored[0][1]
    return QuickCapture(action, canonical_command(action, code), str(text or "").strip())
