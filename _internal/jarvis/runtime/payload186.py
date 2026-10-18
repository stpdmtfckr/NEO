"""Payload provenance for routed commands (QA185-PAYLOAD-PROVENANCE-01).

Routing works on a normalised, lower-cased internal form; the *data* of a
command (a reminder's label, a note's text, a quoted phrase to translate, a
city, a date, a duration) is taken back from the words the user actually said.
Everything here is pure text work: no model, no device, no network.

``structured_route`` claims a few unambiguous grammars before the generic
routes can turn them into something else (an app name, Task Manager, a
dictation). ``repair`` completes the arguments of an already chosen tool.
"""
from __future__ import annotations

import math
import re
from typing import Any

from jarvis.runtime.duration import parse_duration, parse_timer_command
from jarvis.runtime.natural_commands import strip_request_shell

LANGS = ("ru", "en", "de", "fr", "es", "it", "pt")


def _code(code: str) -> str:
    return code if code in LANGS else "ru"


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n,;:—–-«»\"“”").rstrip(".")


def _shell(raw: str, code: str) -> str:
    try:
        value = strip_request_shell(raw, code)
    except Exception:
        value = str(raw or "")
    value = re.sub(r"^\s*(?:нео|neo)\s*[,:]\s*", "", value, flags=re.I)
    return value.strip()


# ---------------------------------------------------------------- reminders

_REMIND = {
    "ru": r"(?:напомни(?:те)?|поставь\s+напоминание|создай\s+напоминание)(?:\s+(?:мне|нам))?",
    "en": r"(?:remind\s+me|set\s+(?:a\s+)?reminder|create\s+(?:a\s+)?reminder)",
    "de": r"(?:erinnere\s+mich|erinner\s+mich|stelle\s+eine\s+erinnerung)",
    "fr": r"(?:rappelle[- ]moi|rappelez[- ]moi|crée\s+un\s+rappel)",
    "es": r"(?:recu[eé]rdame|recu[eé]rdeme|crea\s+un\s+recordatorio)",
    "it": r"(?:ricordami|crea\s+un\s+promemoria)",
    "pt": r"(?:lembra[- ]me|lembre[- ]me|cria\s+um\s+lembrete)",
}
_IN = {"ru": r"через", "en": r"in", "de": r"in", "fr": r"dans", "es": r"en|dentro\s+de",
       "it": r"tra|fra", "pt": r"em|daqui\s+a|dentro\s+de"}
_LINK = {"ru": r"что(?:бы)?|о|об|про", "en": r"to|that|about", "de": r"dass|daran|an|zu",
         "fr": r"de|d['’]|que|qu['’]", "es": r"que|de", "it": r"di|che", "pt": r"de|que|para"}
_UNIT_WORD = re.compile(r"секунд|минут|час|second|minute|hour|sekunde|minuten|stunde|seconde|heure|segundo|minuto|hora|secondo|or[ae]\b|\bmin\b|\bh\b", re.I)
_FRACTION_WORD = re.compile(r"половин|четверт|полтор|half|quarter|halb|viertel|demi|quart|medi[oa]|cuarto|mezz|quarto|mei[oa]", re.I)


def _label(text: str, code: str) -> str:
    text = re.sub(r"^\s*[,:;—–-]?\s*", "", text)
    text = re.sub(r"^(?:" + _LINK[code] + r")(?:\s+|(?<=['’]))", "", text, count=1, flags=re.I)
    return _clean(text)


def reminder(raw: str, code: str) -> dict[str, Any] | None:
    """``{"seconds", "label"}`` for «remind me in <duration>: <label>`` (either
    order), ``{"clarify": True}`` for a relative duration that cannot be read
    completely, ``None`` when the phrase is not a relative reminder."""
    code = _code(code)
    value = _shell(raw, code)
    head = re.match(r"^(?:" + _REMIND[code] + r")\s*[,:]?\s+", value, re.I)
    if not head:
        return None
    rest = value[head.end():].strip()
    prep = re.match(r"^(?:" + _IN[code] + r")\s+", rest, re.I)
    if prep:
        tail = rest[prep.end():]
        candidates: list[tuple[str, str]] = []
        sep = re.search(r"\s*(?::|—|–|\s-\s)\s*", tail)
        if sep:
            candidates.append((tail[:sep.start()], tail[sep.end():]))
        words = list(re.finditer(r"\S+", tail))
        for index in range(len(words), 0, -1):
            end = words[index - 1].end()
            candidates.append((tail[:end], tail[end:]))
        for duration, label in candidates:
            seconds = parse_duration(duration.strip(" ,"), code, allow_clock=False)
            if seconds:
                return {"seconds": float(seconds), "label": _label(label, code)}
        if _UNIT_WORD.search(tail) and (_FRACTION_WORD.search(tail) or re.search(r"\d", tail)):
            return {"clarify": True}
        return None
    # «напомни выпить воды через 20 минут» — the label first, the duration last.
    for match in reversed(list(re.finditer(r"\s+(?:" + _IN[code] + r")\s+", rest, re.I))):
        duration = rest[match.end():].strip(" .!?")
        seconds = parse_duration(duration, code, allow_clock=False)
        if seconds:
            return {"seconds": float(seconds), "label": _label(rest[:match.start()], code)}
        if _UNIT_WORD.search(duration) and _FRACTION_WORD.search(duration):
            return {"clarify": True}
    return None


REMINDER_CLARIFY = {
    "ru": "Не понял срок напоминания целиком. Скажи его иначе, например: «через 1 час 15 минут».",
    "en": "I couldn't read the whole reminder time. Please say it another way, for example “in 1 hour 15 minutes”.",
    "de": "Ich habe die Erinnerungszeit nicht vollständig verstanden. Sag sie bitte anders, zum Beispiel „in 1 Stunde 15 Minuten“.",
    "fr": "Je n’ai pas compris tout le délai du rappel. Dis-le autrement, par exemple « dans 1 heure 15 minutes ».",
    "es": "No entendí el plazo completo del recordatorio. Dilo de otra forma, por ejemplo «en 1 hora 15 minutos».",
    "it": "Non ho capito tutto il tempo del promemoria. Dillo in un altro modo, per esempio «tra 1 ora 15 minuti».",
    "pt": "Não percebi o prazo completo do lembrete. Diz de outra forma, por exemplo «daqui a 1 hora 15 minutos».",
}


# ---------------------------------------------------------------- unit conversion

_UNITS: dict[str, tuple[str, float, str]] = {
    # id: (dimension, factor to the base unit, regex of spoken forms)
    "mm": ("length", .001, r"мм|миллиметр\w*|mm|millimet\w*|millimètre\w*|milímetro\w*|millimetr\w*"),
    "cm": ("length", .01, r"см|сантиметр\w*|cm|centimet\w*|centimètre\w*|centímetro\w*|zentimeter\w*|centimetr\w*"),
    "km": ("length", 1000., r"км|километр\w*|km|kilomet\w*|kilomètre\w*|kilómetro\w*|chilometr\w*|quilómetro\w*|quilômetro\w*"),
    "m": ("length", 1., r"м|метр\w*|m|met(?:er|re)s?|meter\w*|mètre\w*|metro\w*"),
    "in": ("length", .0254, r"дюйм\w*|inch(?:es)?|zoll|pouces?|pulgadas?|pollic[ei]|polegadas?"),
    "ft": ("length", .3048, r"фут\w*|f(?:oo|ee)t|ft|fuß|pieds?|pies?|piedi|pés?"),
    "yd": ("length", .9144, r"ярд\w*|yards?|yd"),
    "mi": ("length", 1609.344, r"мил[яиьюе]\w*|miles?|meilen?|millas?|miglia|milhas?|mi"),
    "mg": ("mass", 1e-6, r"мг|миллиграмм\w*|mg|milligram\w*|milligramm\w*|milligramme\w*|miligramo\w*"),
    "kg": ("mass", 1., r"кг|килограмм\w*|kg|kilos?|kilogram\w*|kilogramm\w*|kilogramme\w*|kilogramo\w*|chilogramm\w*|chili|quilogramas?|quilos?"),
    "g": ("mass", .001, r"г|грамм\w*|g|grams?|gramm\w*|grammes?|gramos?|grammi|grammo|gramas?"),
    "t": ("mass", 1000., r"т|тонн\w*|tons?|tonnes?|tonnen?|toneladas?|tonnellat\w*"),
    "oz": ("mass", .028349523125, r"унци\w*|ounces?|oz|unzen?|onces?|onzas?|once|onças?"),
    "lb": ("mass", .45359237, r"фунт\w*|pounds?|lbs?|pfund|livres?|libras?|libbre|libbra"),
    "ml": ("volume", .001, r"мл|миллилитр\w*|ml|millilit\w*|mililitro\w*|millilitr\w*"),
    "l": ("volume", 1., r"л|литр\w*|l|lit(?:er|re)s?|liter\w*|litros?|litri|litro"),
    "gal": ("volume", 3.785411784, r"галлон\w*|gallons?|gallonen?|galón|galones|galloni?|galão|galões"),
    "kmh": ("speed", 1 / 3.6, r"км/ч|километр\w*\s+в\s+час|km/h|kmh|kilomet\w*\s+per\s+hour|stundenkilometer"),
    "mph": ("speed", .44704, r"mph|мил[ьи]\s+в\s+час|miles?\s+per\s+hour"),
    "ms": ("speed", 1., r"м/с|метр\w*\s+в\s+секунду|m/s"),
    "c": ("temp", 0., r"°\s*c|градус\w*\s+цельси\w*|цельси\w*|celsius|grad\s+celsius|degrés?\s+celsius|grados?\s+celsius|gradi\s+celsius|graus?\s+celsius"),
    "f": ("temp", 0., r"°\s*f|градус\w*\s+фаренгейт\w*|фаренгейт\w*|fahrenheit"),
    "k": ("temp", 0., r"кельвин\w*|kelvin"),
    "day": ("time", 86400., r"сут\w*|дн[еяи]\w*|день|days?|tage?n?|jours?|días?|giorn[io]|dias?"),
    "h": ("time", 3600., r"час\w*|hours?|stunden?|heures?|horas?|or[ae]"),
    "min": ("time", 60., r"минут\w*|minutes?|minuten?|minutos?|minut[io]"),
    "s": ("time", 1., r"секунд\w*|seconds?|sekunden?|secondes?|segundos?|second[io]"),
}
_SYMBOLS = {
    "ru": {"mm": "мм", "cm": "см", "m": "м", "km": "км", "in": "дюйм.", "ft": "фут.", "yd": "ярд.", "mi": "мил.",
           "mg": "мг", "g": "г", "kg": "кг", "t": "т", "oz": "унц.", "lb": "фунт.", "ml": "мл", "l": "л", "gal": "гал.",
           "kmh": "км/ч", "mph": "миль/ч", "ms": "м/с", "c": "°C", "f": "°F", "k": "K",
           "day": "сут.", "h": "ч", "min": "мин", "s": "с"},
}
_LATIN = {"mm": "mm", "cm": "cm", "m": "m", "km": "km", "in": "in", "ft": "ft", "yd": "yd", "mi": "mi",
          "mg": "mg", "g": "g", "kg": "kg", "t": "t", "oz": "oz", "lb": "lb", "ml": "ml", "l": "l", "gal": "gal",
          "kmh": "km/h", "mph": "mph", "ms": "m/s", "c": "°C", "f": "°F", "k": "K",
          "day": "d", "h": "h", "min": "min", "s": "s"}
_CONVERT_SHELL = (r"(?:переведи|переведите|конвертируй|сконвертируй|преобразуй|сколько\s+будет|convert|translate|"
                  r"how\s+much\s+is|rechne|umrechnen|konvertiere|convertis|convertir|convierte|converti|converte|traduis|traduce|traduci|traduz)")
_TO = r"(?:в|во|to|into|in|nach|en|a|in|em|para|à)"


def _unit_id(text: str) -> str | None:
    value = re.sub(r"\s+", " ", str(text or "").casefold().replace("ё", "е")).strip(" .?!")
    for unit, (_dim, _factor, pattern) in _UNITS.items():
        if re.fullmatch(pattern, value, re.I):
            return unit
    return None


def _number_text(value: float, code: str) -> str:
    if abs(value - round(value)) < 1e-9 and abs(value) < 1e15:
        text = str(int(round(value)))
    else:
        text = f"{value:.6g}"
        if "e" in text:
            text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") if code != "en" else text


def unit_conversion(raw: str, code: str) -> str | None:
    """A local answer for «переведи 5 km в метры» (never a language translation)."""
    code = _code(code)
    value = _shell(raw, code)
    match = re.fullmatch(_CONVERT_SHELL + r"\s+(?P<amount>-?\d+(?:[.,]\d+)?)\s*(?P<src>[^\d].*?)\s+" + _TO
                         + r"\s+(?P<dst>.+?)\s*[?.!]*", value, re.I)
    if not match:
        return None
    src, dst = _unit_id(match["src"]), _unit_id(match["dst"])
    if not src or not dst:
        return None
    amount = float(match["amount"].replace(",", "."))
    symbols = _SYMBOLS.get(code, _LATIN)
    if _UNITS[src][0] != _UNITS[dst][0]:
        return {
            "ru": "Нельзя перевести {a} в {b}: это разные величины.",
            "en": "{a} can't be converted to {b}: they measure different things.",
            "de": "{a} lässt sich nicht in {b} umrechnen: Das sind verschiedene Größen.",
            "fr": "Impossible de convertir {a} en {b} : ce sont des grandeurs différentes.",
            "es": "No se puede convertir {a} en {b}: son magnitudes distintas.",
            "it": "Non si può convertire {a} in {b}: sono grandezze diverse.",
            "pt": "Não é possível converter {a} em {b}: são grandezas diferentes.",
        }[code].format(a=symbols[src], b=symbols[dst])
    if _UNITS[src][0] == "temp":
        celsius = amount if src == "c" else (amount - 32) * 5 / 9 if src == "f" else amount - 273.15
        result = celsius if dst == "c" else celsius * 9 / 5 + 32 if dst == "f" else celsius + 273.15
    else:
        result = amount * _UNITS[src][1] / _UNITS[dst][1]
    if not math.isfinite(result):
        return None
    shown = float(f"{result:.6g}")
    sign = "=" if abs(shown - result) <= 1e-9 * max(1.0, abs(result)) else "≈"
    return f"{_number_text(amount, code)} {symbols[src]} {sign} {_number_text(shown, code)} {symbols[dst]}."


# ---------------------------------------------------------------- weather

_CITY_PREP = {"ru": r"в|во", "en": r"in|for", "de": r"in|für", "fr": r"à|a|au|en", "es": r"en|para",
              "it": r"a|ad|in|per", "pt": r"em|no|na|para"}
_TIME_WORDS = {
    "now", "right", "today", "tonight", "tomorrow", "jetzt", "heute", "morgen", "gerade", "maintenant",
    "aujourd", "demain", "ahora", "hoy", "mañana", "adesso", "ora", "oggi", "domani", "agora", "hoje",
    "amanhã", "сейчас", "сегодня", "завтра", "вечером", "утром", "днём", "ночью",
}
_CAP = r"[A-ZÀ-ÖØ-ÞА-ЯЁ][\w’'\-]*"
_WEATHER_WORD = r"(?i:weather|wetter|météo|meteo|tiempo|clima|tempo|погода|погоду)"


def weather_city(raw: str, code: str) -> str | None:
    """The explicitly named place («Helsinki»), or ``None`` when none was said."""
    code = _code(code)
    text = str(raw or "")
    found = re.search(r"(?<!\w)(?i:" + _CITY_PREP[code] + r")\s+(?P<city>" + _CAP + r"(?:[\s\-]" + _CAP + r")*)", text)
    if not found:
        found = re.search(r"^\s*(?P<city>" + _CAP + r"(?:\s" + _CAP + r")*)\s+" + _WEATHER_WORD, text)
    if not found:
        return None
    words = [w for w in found["city"].split() if w.casefold().strip("’'") not in _TIME_WORDS]
    city = " ".join(words).strip(" ,.?!")
    if not city or city.casefold() in {"what", "wie", "quel", "quelle", "qué", "que", "come", "como", "какая", "какой"}:
        return None
    return city


# ---------------------------------------------------------------- dates

_CURRENT_DATE = re.compile(
    r"текущ\w*\s+дат|сегодняшн\w*\s+дат|какое\s+сегодня\s+число|какое\s+число\s+сегодня|current\s+date|today'?s\s+date|"
    r"what\s+(?:is\s+the\s+)?date\s+(?:is\s+it\s+)?today|aktuelle\w*\s+datum|heutige\w*\s+datum|date\s+(?:d['’]aujourd['’]hui|actuelle)|"
    r"fecha\s+(?:actual|de\s+hoy)|data\s+(?:di\s+oggi|attuale|de\s+hoje|atual)", re.I)


_WEEKDAY_QUESTION = re.compile(
    r"(?:в\s+)?как(?:ой|ую)\s+день\s+недели|(?:what|which)\s+day\s+of\s+the\s+week|(?:an\s+)?welche[mn]?\s+wochentag|"
    r"quel\s+jour\s+de\s+la\s+semaine|qu[ée]\s+d[ií]a\s+de\s+la\s+semana|(?:in\s+)?che\s+giorno\s+della\s+settimana|"
    r"(?:que|qual\s+(?:o\s+)?)\s*dia\s+da\s+semana", re.I)


def date_route(raw: str) -> tuple[str, dict[str, Any]] | None:
    """NEW185-DATE-SEMANTIC-PAYLOAD-LOSS: «qué día de la semana es el 15 de noviembre
    de 2026» keeps its date; the question goes to the offline date tool."""
    text = str(raw or "").strip()
    if _WEEKDAY_QUESTION.search(text) and re.search(r"\d", text):
        return "date_info", {"action": "date_info", "text": text}
    return None


def date_text(raw: str) -> str:
    return "today" if _CURRENT_DATE.search(str(raw or "")) else str(raw or "").strip()


# ---------------------------------------------------------------- calendar range

_DAYS_RANGE = re.compile(
    r"(?:следующ\w*|ближайш\w*)\s+(?P<a>\d{1,2})\s+(?:дн\w*|сут\w*)|(?:next|coming|upcoming)\s+(?P<b>\d{1,2})\s+days?|"
    r"(?:nächsten|kommenden)\s+(?P<c>\d{1,2})\s+tage?n?|(?P<d>\d{1,2})\s+(?:prochains|jours\s+à\s+venir)|"
    r"(?:próximos|siguientes)\s+(?P<e>\d{1,2})\s+d[ií]as|(?:prossimi)\s+(?P<f>\d{1,2})\s+giorni", re.I)


def calendar_days(raw: str) -> int | None:
    match = _DAYS_RANGE.search(str(raw or ""))
    if not match:
        return None
    value = next(int(v) for v in match.groups() if v)
    return value if 1 <= value <= 31 else None


# ---------------------------------------------------------------- game locations

_KIND_WORDS = (
    ("saves", r"сохранени\w*|сейв\w*|\bsaves?\b|save\s*games?|savegames?|spielstände|sauvegardes?|partidas\s+guardadas|salvataggi|jogos\s+(?:salvos|guardados)"),
    ("install", r"папк\w*\s+установк\w*|установочн\w*\s+папк\w*|install(?:ation)?\s+(?:folder|directory|dir)|installationsordner|dossier\s+d['’]installation|carpeta\s+de\s+instalación|cartella\s+di\s+installazione|pasta\s+de\s+instala[çc][ãa]o"),
    ("screenshots", r"скриншот\w*|screenshots?|captures?\s+d['’]écran|capturas?\s+de\s+pantalla|schermate|capturas?\s+de\s+ecrã"),
    ("logs", r"\bлоги\b|журнал\w*\s+игры|\blogs?\b|protokolle|journaux|registros|registri|registos"),
    ("config", r"конфиг\w*|файл\w*\s+настро\w*|config(?:uration)?\s+files?|konfigurationsdateien"),
)


def game_kind(raw: str) -> str | None:
    text = str(raw or "")
    for kind, pattern in _KIND_WORDS:
        if re.search(pattern, text, re.I):
            return kind
    return None


# ---------------------------------------------------------------- notes / checklists

_CHECKLIST_CREATE = {
    "ru": r"(?:создай|сделай)\s+(?:новый\s+)?(?:чек[ -]?лист|список\s+(?:задач|дел))\s+(?:(?:с\s+названием|под\s+названием|под\s+именем)\s+)?(?P<title>.+?)\s+с\s+пункт(?:ом|ами)\s+(?P<items>.+)",
    "en": r"(?:create|make)\s+(?:a\s+)?(?:new\s+)?(?:checklist|to-?do\s+list|task\s+list)\s+(?:(?:called|named)\s+)?(?P<title>.+?)\s+with\s+(?:the\s+)?(?:items?\s+)?(?P<items>.+)",
    "de": r"(?:erstelle|mach|mache)\s+(?:eine\s+)?(?:neue\s+)?(?:checkliste|aufgabenliste|to-?do-?liste)\s+(?:(?:namens|mit\s+dem\s+namen)\s+)?(?P<title>.+?)\s+mit\s+(?:(?:dem\s+punkt|den\s+punkten)\s+)?(?P<items>.+)",
    "fr": r"(?:crée|fais)\s+(?:une\s+)?(?:nouvelle\s+)?(?:liste\s+de\s+tâches|checklist|liste)\s+(?:(?:appelée|nommée|intitulée)\s+)?(?P<title>.+?)\s+avec\s+(?:(?:l['’]élément|les\s+éléments)\s+)?(?P<items>.+)",
    "es": r"(?:crea|haz)\s+(?:una\s+)?(?:nueva\s+)?(?:lista\s+de\s+tareas|checklist)\s+(?:llamada\s+)?(?P<title>.+?)\s+con\s+(?:(?:el\s+elemento|los\s+elementos)\s+)?(?P<items>.+)",
    "it": r"(?:crea|fai)\s+(?:una\s+)?(?:nuova\s+)?(?:lista\s+di\s+(?:attività|cose\s+da\s+fare|compiti)|checklist)\s+(?:chiamata\s+)?(?P<title>.+?)\s+con\s+(?:(?:la\s+voce|le\s+voci)\s+)?(?P<items>.+)",
    "pt": r"(?:cria|crie|faz|faça)\s+(?:uma\s+)?(?:nova\s+)?(?:lista\s+de\s+tarefas|checklist)\s+(?:chamada\s+)?(?P<title>.+?)\s+com\s+(?:(?:o\s+item|os\s+itens)\s+)?(?P<items>.+)",
}
_AND = {"ru": r"и", "en": r"and", "de": r"und", "fr": r"et", "es": r"y", "it": r"e", "pt": r"e"}


def _items(text: str, code: str) -> list[str]:
    parts = re.split(r"\s*[;,]\s*|\s+(?:" + _AND[code] + r")\s+(?=\S)", text.strip(" .!?"))
    return [_clean(p) for p in parts if _clean(p)]


def checklist_create(raw: str, code: str) -> tuple[str, list[str]] | None:
    code = _code(code)
    match = re.fullmatch(_CHECKLIST_CREATE[code], _shell(raw, code).strip(" .!?"), re.I | re.S)
    if not match:
        return None
    title = _clean(match["title"])
    items = _items(match["items"], code)
    return (title, items) if title and items else None


_ADD_TARGET = {
    "ru": (r"добавь\s+в\s+(?:заметку|заметки)", r"добавь\s+в\s+(?:чек[ -]?лист|список\s+(?:задач|дел))"),
    "en": (r"add\s+to\s+(?:the\s+)?note", r"add\s+to\s+(?:the\s+)?(?:checklist|to-?do\s+list|task\s+list)"),
    "de": (r"füge\s+(?:zur|der)\s+notiz", r"füge\s+(?:zur|der)\s+(?:checkliste|aufgabenliste)"),
    "fr": (r"ajoute\s+(?:à\s+la|a\s+la)\s+note", r"ajoute\s+(?:à\s+la|a\s+la)\s+(?:liste\s+de\s+tâches|checklist)"),
    "es": (r"(?:añade|agrega)\s+a\s+la\s+nota", r"(?:añade|agrega)\s+a\s+la\s+(?:lista\s+de\s+tareas|checklist)"),
    "it": (r"aggiungi\s+alla\s+nota", r"aggiungi\s+alla\s+(?:lista\s+di\s+(?:attività|cose\s+da\s+fare)|checklist)"),
    "pt": (r"adiciona\s+(?:à|a)\s+nota", r"adiciona\s+(?:à|a)\s+(?:lista\s+de\s+tarefas|checklist)"),
}


def add_to_object(raw: str, code: str) -> tuple[str, str, str] | None:
    """(kind, target, literal) for «добавь в заметку Trip731: Blue mug 731»."""
    code = _code(code)
    value = _shell(raw, code)
    for kind, verb in zip(("note", "checklist"), _ADD_TARGET[code]):
        match = re.fullmatch(verb + r"\s+(?P<target>[^:]+?)\s*(?:\s+hinzu)?\s*:\s*(?P<text>.+)", value, re.I | re.S)
        if match:
            target, text = _clean(match["target"]), match["text"].strip()
            if target and text:
                return kind, target, text
    return None


_NOTE_CREATE = {
    "ru": (r"(?:создай|сделай|добавь)\s+(?:новую\s+)?заметку\s+(?:(?:с\s+названием|под\s+названием)\s+)?(?P<title>.+?)\s+с\s+текстом\s*:?\s*(?P<content>.+)",
           r"запиши\s+(?P<content>.+?)\s+(?:в|как)\s+(?:новую\s+)?заметку\s+(?:(?:с\s+названием|под\s+названием)\s+)?(?P<title>.+)"),
    "en": (r"(?:create|make|add)\s+(?:a\s+)?(?:new\s+)?note\s+(?:(?:named|called)\s+)?(?P<title>.+?)\s+with\s+(?:the\s+)?text\s*:?\s*(?P<content>.+)",
           r"(?:write\s+down|note\s+down|jot\s+down)\s+(?P<content>.+?)\s+(?:as|in(?:to)?)\s+(?:a\s+)?new\s+note\s+(?:named|called)\s+(?P<title>.+)"),
    "de": (r"(?:erstelle|mach|mache)\s+(?:eine\s+)?(?:neue\s+)?notiz\s+(?:(?:namens|mit\s+dem\s+namen)\s+)?(?P<title>.+?)\s+mit\s+(?:dem\s+)?text\s*:?\s*(?P<content>.+)",
           r"(?:schreib|schreibe|notiere)\s+(?P<content>.+?)\s+(?:als|in\s+eine)\s+neue\s+notiz\s+(?:namens|mit\s+dem\s+namen)\s+(?P<title>.+)"),
    "fr": (r"(?:crée|fais)\s+(?:une\s+)?(?:nouvelle\s+)?note\s+(?:(?:appelée|nommée|intitulée)\s+)?(?P<title>.+?)\s+avec\s+(?:le\s+)?texte\s*:?\s*(?P<content>.+)",
           r"(?:note|écris)\s+(?P<content>.+?)\s+(?:comme|dans)\s+(?:une\s+)?nouvelle\s+note\s+(?:appelée|nommée|intitulée)\s+(?P<title>.+)"),
    "es": (r"(?:crea|haz)\s+(?:una\s+)?(?:nueva\s+)?nota\s+(?:llamada\s+)?(?P<title>.+?)\s+con\s+(?:el\s+)?texto\s*:?\s*(?P<content>.+)",
           r"(?:apunta|anota|escribe)\s+(?P<content>.+?)\s+(?:como|en)\s+(?:una\s+)?nueva\s+nota\s+llamada\s+(?P<title>.+)"),
    "it": (r"(?:crea|fai)\s+(?:una\s+)?(?:nuova\s+)?nota\s+(?:(?:chiamata|intitolata)\s+)?(?P<title>.+?)\s+con\s+(?:il\s+)?testo\s*:?\s*(?P<content>.+)",
           r"(?:annota|scrivi)\s+(?P<content>.+?)\s+(?:come|in)\s+(?:una\s+)?nuova\s+nota\s+(?:chiamata|intitolata)\s+(?P<title>.+)"),
    "pt": (r"(?:cria|crie|faz)\s+(?:uma\s+)?(?:nova\s+)?nota\s+(?:chamada\s+)?(?P<title>.+?)\s+com\s+(?:o\s+)?texto\s*:?\s*(?P<content>.+)",
           r"(?:anota|escreve|escreva)\s+(?P<content>.+?)\s+(?:como|numa|em\s+uma)\s+(?:uma\s+)?nova\s+nota\s+chamada\s+(?P<title>.+)"),
}


def note_create(raw: str, code: str) -> tuple[str, str] | None:
    code = _code(code)
    value = _shell(raw, code).strip()
    for pattern in _NOTE_CREATE[code]:
        match = re.fullmatch(pattern, value, re.I | re.S)
        if match:
            title, content = _clean(match["title"]), match["content"].strip().rstrip(".")
            if title and content:
                return title, content
    return None


# ---------------------------------------------------------------- information namespace

_INFO = (
    ("system_monitor", {"action": "network"},
     r"(?:покажи\s+|какое\s+|какая\s+)?(?:состояние|статус|скорость)\s+(?:сети|интернета|подключения)|"
     r"(?:show\s+(?:me\s+)?)?(?:the\s+)?(?:network|internet|connection)\s+(?:status|state|speed)|"
     r"(?:zeige\s+(?:mir\s+)?)?(?:den\s+)?(?:netzwerkstatus|netzwerkzustand|status\s+des\s+netzwerks)|"
     r"(?:montre\s+(?:moi\s+)?)?l['’]état\s+du\s+réseau|état\s+du\s+réseau|"
     r"(?:muestra\s+(?:me\s+)?)?(?:el\s+)?estado\s+de\s+la\s+red|"
     r"(?:mostra(?:mi)?\s+)?lo\s+stato\s+della\s+rete|stato\s+della\s+rete|"
     r"(?:mostra\s+)?o\s+estado\s+da\s+rede|estado\s+da\s+rede"),
    ("system_monitor", {"action": "battery"},
     r"(?:покажи\s+)?(?:уровень\s+)?заряд\w*\s+(?:батаре\w*|аккумулятор\w*)|сколько\s+(?:осталось\s+)?заряда(?:\s+(?:батаре\w*|аккумулятор\w*))?|"
     r"какой\s+заряд\s+(?:батаре\w*|аккумулятор\w*)|"
     r"(?:show\s+(?:me\s+)?)?(?:the\s+)?battery\s+(?:level|status|charge)|how\s+much\s+battery\s+(?:is\s+)?left|what(?:'s|\s+is)\s+the\s+battery\s+level|"
     r"(?:zeige\s+(?:mir\s+)?)?(?:den\s+)?(?:akkustand|ladestand(?:\s+des\s+akkus)?|akku\s*stand)|wie\s+voll\s+ist\s+der\s+akku|"
     r"(?:montre\s+(?:moi\s+)?)?(?:le\s+)?niveau\s+de\s+(?:la\s+)?batterie|"
     r"(?:muestra(?:me)?\s+)?(?:el\s+)?nivel\s+de\s+(?:la\s+)?batería|cuánta\s+batería\s+queda|"
     r"(?:mostra(?:mi)?\s+)?(?:il\s+)?livello\s+(?:della\s+)?batteria|"
     r"(?:mostra\s+)?o\s+nível\s+da\s+bateria|nível\s+da\s+bateria"),
    ("timer", {"action": "list"},
     r"(?:покажи\s+)?(?:состояние|статус)\s+таймер\w*|какие\s+(?:у\s+меня\s+)?таймеры\s+(?:запущены|идут|работают|стоят)|"
     r"(?:show\s+(?:me\s+)?)?(?:the\s+)?timer\s+status|what\s+timers\s+are\s+(?:running|set)|"
     r"(?:zeige\s+(?:mir\s+)?)?(?:den\s+)?timerstatus|welche\s+timer\s+laufen|"
     r"(?:montre\s+(?:moi\s+)?)?l['’]état\s+des\s+minuteurs|quels\s+minuteurs\s+sont\s+en\s+cours|"
     r"(?:muestra(?:me)?\s+)?el\s+estado\s+de\s+los\s+temporizadores|qué\s+temporizadores\s+(?:hay|están\s+activos)|"
     r"(?:mostra(?:mi)?\s+)?lo\s+stato\s+dei\s+timer|quali\s+timer\s+sono\s+attivi|"
     r"(?:mostra\s+)?o\s+estado\s+dos\s+temporizadores|que\s+temporizadores\s+estão\s+ativos"),
)


def information_route(raw: str, code: str) -> tuple[str, dict[str, Any]] | None:
    value = re.sub(r"\s+", " ", _shell(raw, code).casefold().replace("ё", "е")).strip(" .?!¿¡")
    for tool, args, pattern in _INFO:
        if re.fullmatch(pattern, value, re.I):
            return tool, dict(args)
    return None


_HELP_EXAMPLE = re.compile(
    r"(?:покажи|дай|приведи|скажи)\s+(?:мне\s+)?(?:какой-нибудь\s+|любой\s+)?пример\w*\s+команд\w*(?:\s+(?:для\s+)?(?:нео|neo))?|"
    r"(?:show|give|tell)\s+(?:me\s+)?(?:an?\s+|some\s+)?example\s+commands?(?:\s+for\s+neo)?|"
    r"(?:zeige|gib)\s+(?:mir\s+)?(?:einen?\s+)?beispiel(?:befehl|e?\s+für\s+(?:einen\s+)?befehl)\w*(?:\s+für\s+neo)?|"
    r"(?:montre|donne)(?:[- ]moi)?\s+(?:un\s+)?exemple\s+de\s+commandes?(?:\s+(?:pour\s+)?neo)?|"
    r"(?:muestra|dame)(?:me)?\s+(?:un\s+)?ejemplo\s+de\s+(?:comando|orden)(?:\s+para\s+neo)?|"
    r"(?:mostra|dammi)(?:mi)?\s+(?:un\s+)?esempio\s+di\s+comando(?:\s+per\s+neo)?|"
    r"(?:mostra|dá)(?:-me)?\s+(?:um\s+)?exemplo\s+de\s+comando(?:\s+para\s+(?:o\s+)?neo)?", re.I)

HELP_EXAMPLE_TEXT = {
    "ru": "Например: «включи музыку», «поставь таймер на 5 минут», «напомни через 20 минут: выпить воды», «открой браузер», «какая погода завтра». Полный список — в «Справке по командам».",
    "en": "For example: “play music”, “set a timer for 5 minutes”, “remind me in 20 minutes: drink water”, “open the browser”, “what's the weather tomorrow”. The full list is in the command help.",
    "de": "Zum Beispiel: „spiel Musik“, „stelle einen Timer auf 5 Minuten“, „erinnere mich in 20 Minuten: Wasser trinken“, „öffne den Browser“, „wie wird das Wetter morgen“. Die vollständige Liste steht in der Befehlshilfe.",
    "fr": "Par exemple : « mets de la musique », « mets un minuteur de 5 minutes », « rappelle-moi dans 20 minutes : boire de l’eau », « ouvre le navigateur », « quel temps fera-t-il demain ». La liste complète est dans l’aide des commandes.",
    "es": "Por ejemplo: «pon música», «pon un temporizador de 5 minutos», «recuérdame en 20 minutos: beber agua», «abre el navegador», «qué tiempo hará mañana». La lista completa está en la ayuda de comandos.",
    "it": "Per esempio: «metti la musica», «imposta un timer di 5 minuti», «ricordami tra 20 minuti: bere acqua», «apri il browser», «che tempo farà domani». L’elenco completo è nella guida dei comandi.",
    "pt": "Por exemplo: «põe música», «define um temporizador de 5 minutos», «lembra-me daqui a 20 minutos: beber água», «abre o navegador», «como vai estar o tempo amanhã». A lista completa está na ajuda de comandos.",
}


def help_example(raw: str, code: str) -> str | None:
    value = _shell(raw, code).strip(" .?!¿¡")
    return HELP_EXAMPLE_TEXT[_code(code)] if _HELP_EXAMPLE.fullmatch(value) else None


# ---------------------------------------------------------------- settings sections

_SETTINGS_SECTION = (
    ("widgets", r"книжн\w*\s+полк\w*|полк\w*|виджет\w*|bookshelf|shelf|widgets?|regal\w*|étagère|widgets?|estantería|scaffale|estante"),
    ("advanced", r"дополнительн\w*|advanced|erweitert\w*|avancés?|avanzad\w*|avanzat\w*|avançad\w*"),
    ("window", r"окн[ао]|внешн\w*\s+вид\w*|window|fenster|fenêtre|ventana|finestra|janela"),
    ("robot", r"робот\w*|robot\w*|roboter"),
    ("security", r"безопасност\w*|конфиденциальност\w*|security|privacy|sicherheit|datenschutz|sécurité|confidentialité|seguridad|privacidad|sicurezza|segurança|privacidade"),
    ("scenarios", r"сценари\w*|scenarios?|szenarien|scénarios?|escenarios?|scenari|cenários?"),
    ("hotkeys", r"горяч\w*\s+клавиш\w*|хоткеев|хоткеи|hotkeys?|shortcuts?|tastenkürzel|raccourcis|atajos|scorciatoie|atalhos"),
    ("translation", r"перевод\w*|translation|übersetzung|traduction|traducción|traduzione|tradução"),
    ("microphone", r"микрофон\w*|wake\s*word|microphone|mikrofon|micrófono|microfono|microfone"),
    ("media", r"медиа|музык\w*|spotify|media|music|musik|musique|música|musica"),
    ("games", r"игр\w*|games?|spiele|jeux|juegos|giochi|jogos"),
    ("api", r"api(?:-сервис\w*)?|api\s+services?"),
    ("remote", r"удалённ\w*|удаленн\w*|telegram|remote"),
    ("voice", r"голос\w*|озвучк\w*|voice|stimme|voix|voz|voce"),
    ("streaming", r"стрим\w*|obs|streaming"),
    ("developer", r"разработчик\w*|developer|entwickler|développeur|desarrollador|sviluppatore|programador"),
)
_SETTINGS_SHELL = re.compile(
    r"^(?:открой|покажи|зайди\s+в)\s+(?:настройки|раздел\s+настроек)\s+(?P<a>.+)$|^(?:открой|покажи)\s+(?P<b>.+?)\s+настройки$|"
    r"^(?:open|show)\s+(?:the\s+)?(?P<c>.+?)\s+settings$|^(?:open|show)\s+(?:the\s+)?settings\s+(?:for|of)\s+(?:the\s+)?(?P<d>.+)$|"
    r"^(?:öffne|zeige)\s+(?:die\s+)?(?P<e>.+?)[- ]?einstellungen$|^(?:öffne|zeige)\s+(?:die\s+)?einstellungen\s+(?:für|von|des|der)\s+(?P<f>.+)$|"
    r"^(?:ouvre|montre)\s+les\s+paramètres\s+(?:de\s+(?:la\s+|l['’])?|du\s+|des\s+)?(?P<g>.+)$|"
    r"^(?:abre|muestra)\s+(?:los\s+)?ajustes\s+(?:de\s+(?:la\s+|el\s+)?|del\s+)?(?P<h>.+)$|"
    r"^(?:apri|mostra)\s+le\s+impostazioni\s+(?:del(?:la|lo|l['’])?\s*|di\s+)?(?P<i>.+)$|"
    r"^(?:abre|mostra)\s+as\s+definições\s+(?:d[aoe]s?\s+)?(?P<j>.+)$", re.I)


def settings_section(raw: str, code: str) -> str | None:
    """The stable NEO settings page ID named in «открой настройки робота»."""
    value = re.sub(r"\s+", " ", _shell(raw, code).strip(" .!?")).strip()
    match = _SETTINGS_SHELL.match(value)
    if not match:
        return None
    subject = next(v for v in match.groups() if v)
    subject = re.sub(r"^(?:neo|нео)\s+|\s+(?:(?:в|in|von|de|en|di|do|da)\s+)?(?:neo|нео)$", "", subject.strip(), flags=re.I)
    if re.search(r"\b(?:windows|виндовс|винды|игры\s+\S+|game\s+\S+)\b", subject, re.I):
        return None
    neo_named = bool(re.search(r"\b(?:neo|нео)\b", value, re.I))
    for page, pattern in _SETTINGS_SECTION:
        if re.fullmatch(r"(?:" + pattern + r")", subject, re.I):
            # Sound, microphone and gaming pages also exist in Windows: without
            # «NEO» those words stay with the Windows Settings route.
            if page in _SHARED_WITH_WINDOWS and not neo_named:
                return None
            return page
    return None


_SHARED_WITH_WINDOWS = {"microphone", "voice", "media", "games", "translation"}


# ---------------------------------------------------------------- NEO widgets

_WIDGET = {
    "ru": r"(?P<verb>покажи|открой|скрой|спрячь|закрой)\s+(?:мне\s+)?(?:(?:нео|neo)\s+)?виджет\s+(?:(?:нео|neo)\s+)?(?:(?:информации|информационный|информационную|с\s+информацией)\s+(?=\S))?(?P<name>.+)",
    "en": r"(?P<verb>show|open|display|hide|close)\s+(?:me\s+)?(?:the\s+)?(?:neo\s+)?widget\s+(?:neo\s+)?(?:(?:info|information|informational)\s+(?=\S))?(?P<name>.+)",
    "de": r"(?P<verb>zeige|zeig|öffne|verstecke|verberge|schließe)\s+(?:mir\s+)?(?:das\s+)?(?:neo[- ]?)?widget\s+(?:(?:info|informations)\s+(?=\S))?(?P<name>.+?)(?:\s+(?:an|aus|ein))?",
    "fr": r"(?P<verb>montre|affiche|ouvre|masque|cache|ferme)(?:[- ]moi)?\s+(?:le\s+)?widget\s+(?:(?:de\s+)?neo\s+)?(?:(?:d['’]informations?|informatif)\s+(?=\S))?(?P<name>.+)",
    "es": r"(?P<verb>muestra|enseña|abre|oculta|cierra)(?:me)?\s+(?:el\s+)?widget\s+(?:(?:de\s+)?neo\s+)?(?:(?:informativo|de\s+información)\s+(?=\S))?(?P<name>.+)",
    "it": r"(?P<verb>mostra|apri|nascondi|chiudi)(?:mi)?\s+(?:il\s+)?widget\s+(?:(?:di\s+)?neo\s+)?(?:(?:informativo|di\s+informazioni)\s+(?=\S))?(?P<name>.+)",
    "pt": r"(?P<verb>mostra|mostre|abre|esconde|oculta|fecha)(?:-me)?\s+(?:o\s+)?widget\s+(?:(?:do\s+)?neo\s+)?(?:(?:informativo|de\s+informação)\s+(?=\S))?(?P<name>.+)",
}
_HIDE_VERBS = {"скрой", "спрячь", "закрой", "hide", "close", "verstecke", "verberge", "schließe", "masque", "cache", "ferme",
               "oculta", "cierra", "nascondi", "chiudi", "esconde", "fecha"}


def widget_command(raw: str, code: str) -> tuple[str, str] | None:
    """NEW185-WIDGET-MULTILINGUAL-NAMESPACE: «zeige das Widget Notes» is a NEO panel."""
    code = _code(code)
    match = re.fullmatch(_WIDGET[code], _shell(raw, code).strip(" .!?"), re.I)
    if not match:
        return None
    name = _clean(match["name"])
    if not name:
        return None
    return ("hide" if match["verb"].casefold() in _HIDE_VERBS else "show"), name


# ---------------------------------------------------------------- public API

def structured_route(raw: str, code: str) -> tuple[str, dict[str, Any]] | None:
    """Whole-phrase grammars whose meaning another route would distort."""
    code = _code(code)
    info = information_route(raw, code) or date_route(raw)
    if info is not None:
        return info
    created = checklist_create(raw, code)
    if created is not None:
        title, items = created
        return "overlay", {"action": "checklist_create", "name": title, "title": title, "items": items,
                           "aliases": [title.casefold()]}
    note = note_create(raw, code)
    if note is not None:
        title, content = note
        return "shelf", {"action": "create", "kind": "note", "title": title, "content": content}
    added = add_to_object(raw, code)
    if added is not None:
        kind, target, text = added
        if kind == "checklist":
            return "overlay", {"action": "checklist_add", "name": target, "item": text}
        return "overlay", {"action": "append_text", "name": target, "text": text}
    section = settings_section(raw, code)
    if section is not None:
        return "desktop_features", {"action": "settings", "section": section}
    widget = widget_command(raw, code)
    if widget is not None:
        return "overlay", {"action": widget[0], "name": widget[1]}
    return None


# Generic catch-alls a whole-phrase grammar above may override.
OVERRIDABLE = {"text_edit", "application", "system", "game_locations"}


def local_answer(raw: str, code: str) -> str | None:
    """A complete local reply that needs no tool (unit conversion, command example)."""
    return unit_conversion(raw, code) or help_example(raw, code)


_LITERAL_TOOLS = {"overlay", "shelf", "timer", "text_edit", "notes", "memory"}
_LITERAL_KEYS = ("text", "content", "body", "item", "label", "title", "name", "new_name")


def restore_case(value: str, raw: str) -> str:
    """A lower-cased copy of something the user said is replaced by that span."""
    if not isinstance(value, str) or not value.strip() or value != value.casefold() or not raw:
        return value
    found = re.search(r"(?<!\w)" + re.escape(value.strip()) + r"(?!\w)", raw, re.I)
    return found.group(0) if found and found.group(0) != value.strip() else value


def repair(tool: str, args: dict[str, Any], raw: str, code: str) -> dict[str, Any] | None:
    """Complete ``args`` in place from the original phrase. Returns
    ``{"clarify": text}`` when the phrase must not be executed as understood."""
    code = _code(code)
    raw = str(raw or "").strip()
    if not raw:
        return None
    action = str(args.get("action", "") or "").casefold()
    if tool == "timer" and action == "reminder":
        parsed = reminder(raw, code)
        if parsed and parsed.get("clarify"):
            return {"clarify": REMINDER_CLARIFY[code]}
        if parsed:
            args["seconds"] = parsed["seconds"]
            if parsed["label"]:
                args["label"] = parsed["label"]
    elif tool == "timer" and action in {"timer", ""}:
        parsed = parse_timer_command(raw, code)
        if parsed is not None and parsed[1].get("seconds"):
            args["seconds"] = float(parsed[1]["seconds"])
    elif tool == "translate":
        from jarvis.integrations.local_translation import looks_like_translation_request
        if looks_like_translation_request(raw):
            args["text"] = raw
    elif tool == "date_info":
        if not str(args.get("text", "") or "").strip():
            args["text"] = date_text(raw)
    elif tool == "weather":
        city = weather_city(raw, code)
        current = str(args.get("city", "") or "").strip()
        if city:
            if not (code == "ru" and current and current.casefold()[:4] == city.casefold()[:4] and current.casefold() != city.casefold()):
                args["city"] = city
        elif current.casefold() in _TIME_WORDS:
            args["city"] = ""
    elif tool == "google_calendar" and action in {"next", "all", "upcoming", "list"}:
        days = calendar_days(raw)
        if days:
            args["action"] = "upcoming"
            args["days"] = days
    elif tool == "game_locations" and action in {"find", "open", "query"}:
        kind = game_kind(raw)
        if kind and str(args.get("kind", "") or "all") in {"", "all"}:
            args["kind"] = kind
    elif tool == "desktop_features" and action == "settings":
        if not str(args.get("section", "") or "").strip():
            args["section"] = settings_section(raw, code) or "widgets"
    if tool == "overlay" and action == "checklist_create":
        created = checklist_create(raw, code)
        if created is not None:
            args["name"] = args["title"] = created[0]
            args["items"] = created[1]
    if tool in _LITERAL_TOOLS:
        for key in _LITERAL_KEYS:
            if isinstance(args.get(key), str):
                args[key] = restore_case(args[key], raw)
        if isinstance(args.get("items"), list):
            args["items"] = [restore_case(x, raw) if isinstance(x, str) else x for x in args["items"]]
    return None
