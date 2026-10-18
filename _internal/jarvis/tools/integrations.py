# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import math
import logging
import re
import time
import uuid
import psutil
from datetime import datetime, timedelta, timezone
from typing import Any

from jarvis.integrations.openweather import OpenWeatherAdapter
from jarvis.integrations.bluetooth_devices import list_bluetooth_devices, list_present_devices
from jarvis.integrations.system_monitor import format_snapshot, snapshot
from jarvis.integrations.mail_services import format_mail_briefing
from jarvis.integrations.google_calendar import GoogleCalendarClient, format_calendar_events, parse_calendar_datetime, calendar_patch_changes
from jarvis.integrations.telegram_messages import telegram_message_center, format_briefing as format_telegram_briefing
from jarvis.integrations.daily_briefing import greeting_and_wish
from jarvis.language import accepted_command_languages, language_code
from jarvis.runtime.timers import timer_manager
from jarvis.security import Risk
from jarvis.tools.registry import ToolDefinition, ToolExecution

logger = logging.getLogger(__name__)


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def _enum(values: list[str], description: str) -> dict[str, Any]:
    return {"type": "string", "enum": values, "description": description}


_TEXT: dict[str, dict[str, str]] = {
    "timer_none": {
        "ru": "Активных таймеров и напоминаний нет.", "en": "There are no active timers or reminders.",
        "de": "Es gibt keine aktiven Timer oder Erinnerungen.", "fr": "Il n'y a aucun minuteur ni rappel actif.",
        "es": "No hay temporizadores ni recordatorios activos.", "it": "Non ci sono timer o promemoria attivi.",
        "pt": "Não há temporizadores nem lembretes ativos.",
    },
    "timer_cancelled": {
        "ru": "Ближайший таймер или напоминание отменён.", "en": "The next timer or reminder was cancelled.",
        "de": "Der nächste Timer oder die nächste Erinnerung wurde abgebrochen.", "fr": "Le prochain minuteur ou rappel a été annulé.",
        "es": "Se canceló el siguiente temporizador o recordatorio.", "it": "Il prossimo timer o promemoria è stato annullato.",
        "pt": "O próximo temporizador ou lembrete foi cancelado.",
    },
    "timer_nothing_cancelled": {
        "ru": "Отменять нечего: активных таймеров нет.", "en": "There is nothing to cancel: no active timers.",
        "de": "Es gibt nichts abzubrechen: keine aktiven Timer.", "fr": "Rien à annuler : aucun minuteur actif.",
        "es": "No hay nada que cancelar: no hay temporizadores activos.", "it": "Non c'è nulla da annullare: nessun timer attivo.",
        "pt": "Não há nada para cancelar: nenhum temporizador ativo.",
    },
}


def _t(config: Any, key: str) -> str:
    code = language_code(config)
    row = _TEXT.get(key, {})
    return row.get(code) or row.get("en") or key


_WEEKDAYS = {
    "ru": ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"],
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
    "fr": ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
    "es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
    "it": ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"],
    "pt": ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"],
}

_MONTHS = {
    "ru": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"],
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"],
    "it": ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"],
    "pt": ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"],
}


def _fmt_date(config: Any, value: datetime) -> str:
    code = language_code(config)
    wd = _WEEKDAYS.get(code, _WEEKDAYS["en"])[value.weekday()]
    month = _MONTHS.get(code, _MONTHS["en"])[value.month - 1]
    if code == "en":
        return f"{wd}, {month} {value.day}, {value.year}"
    if code == "de":
        return f"{wd}, {value.day}. {month} {value.year}"
    return f"{wd}, {value.day} {month} {value.year}"


def _dateparser_languages(config: Any) -> list[str]:
    # Command-language isolation is enforced before routing. The multilingual
    # normalizer rewrites accepted date words to the canonical Russian surface,
    # so dateparser also needs Russian in addition to the selected/English inputs.
    values = list(accepted_command_languages(language_code(config)))
    if "ru" not in values:
        values.insert(0, "ru")
    return values


def _fallback_search_dates(text: str) -> list[tuple[str, datetime]]:
    """Small dependency-free parser for common date/time phrases.

    dateparser remains the broad multilingual parser; this fallback keeps date
    questions, timers and reminders deterministic while dependencies are repaired.
    """
    raw=str(text or ""); low=raw.casefold().replace("ё","е"); now=datetime.now()
    months={
        "января":1,"январь":1,"февраля":2,"февраль":2,"марта":3,"март":3,"апреля":4,"апрель":4,
        "мая":5,"май":5,"июня":6,"июнь":6,"июля":7,"июль":7,"августа":8,"август":8,
        "сентября":9,"сентябрь":9,"октября":10,"октябрь":10,"ноября":11,"ноябрь":11,"декабря":12,"декабрь":12,
        "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
    }
    target_date=None; matched=""
    relative=[
        (r"\b(?:послезавтра|day\s+after\s+tomorrow)\b",2),
        (r"\b(?:завтра|tomorrow)\b",1),
        (r"\b(?:сегодня|today)\b",0),
    ]
    for pat,days in relative:
        m=re.search(pat,low,re.I)
        if m:
            target_date=(now+timedelta(days=days)).date(); matched=raw[m.start():m.end()]; break
    if target_date is None:
        month_alt="|".join(sorted((re.escape(x) for x in months),key=len,reverse=True))
        m=re.search(rf"\b(\d{{1,2}})\s+({month_alt})(?:\s+(\d{{4}}))?\b",low,re.I)
        if m:
            day=int(m.group(1)); month=months[m.group(2).casefold()]; explicit_year=int(m.group(3)) if m.group(3) else None
            year=explicit_year or now.year
            try:
                candidate=datetime(year,month,day).date()
                if explicit_year is None and candidate < now.date(): candidate=datetime(year+1,month,day).date()
                target_date=candidate; matched=raw[m.start():m.end()]
            except ValueError:
                target_date=None
    if target_date is None:
        # Numeric DMY date, with optional year.
        m=re.search(r"\b(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?\b",low)
        if m:
            day,month=int(m.group(1)),int(m.group(2)); yr=m.group(3)
            year=(2000+int(yr) if yr and len(yr)==2 else int(yr)) if yr else now.year
            try:
                candidate=datetime(year,month,day).date()
                if not yr and candidate < now.date(): candidate=datetime(year+1,month,day).date()
                target_date=candidate; matched=raw[m.start():m.end()]
            except ValueError: target_date=None
    if target_date is None: return []

    hour=0; minute=0
    tm=re.search(r"\b(?:в|at)?\s*([01]?\d|2[0-3])[:.]([0-5]\d)\b",low,re.I)
    if tm:
        hour=int(tm.group(1)); minute=int(tm.group(2))
    else:
        tm=re.search(r"\b(?:в|at)\s+([01]?\d|2[0-3])\s*(?:час(?:а|ов)?|h(?:ours?)?)\b",low,re.I)
        if tm: hour=int(tm.group(1)); minute=0
    return [(matched, datetime.combine(target_date, datetime.min.time()).replace(hour=hour,minute=minute))]


def _search_dates(config: Any, text: str) -> list[tuple[str, datetime]]:
    raw=str(text or "")
    try:
        from dateparser.search import search_dates
    except Exception:
        return _fallback_search_dates(raw)
    settings = {
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": datetime.now(),
        "RETURN_AS_TIMEZONE_AWARE": False,
        "DATE_ORDER": "DMY",
    }
    try:
        rows=search_dates(raw, languages=_dateparser_languages(config), settings=settings) or []
        return rows or _fallback_search_dates(raw)
    except Exception:
        return _fallback_search_dates(raw)


def _extract_duration_seconds(text: str) -> float | None:
    t = str(text or "").casefold().replace(",", ".")
    total = 0.0
    found = False
    units = [
        (r"(\d+(?:\.\d+)?)\s*(?:час(?:а|ов)?|ч\.?|hours?|hrs?|stunden?|heures?|horas?|ore|hora)", 3600),
        (r"(\d+(?:\.\d+)?)\s*(?:минут(?:а|ы)?|мин\.?|minutes?|mins?|minuten?|minutos?|minuti?)", 60),
        (r"(\d+(?:\.\d+)?)\s*(?:секунд(?:а|ы)?|сек\.?|seconds?|secs?|sekunden?|secondes?|segundos?|secondi?)", 1),
    ]
    for pattern, multiplier in units:
        for match in re.finditer(pattern, t, flags=re.I):
            # NEO 187: «.5 minutes», «-5 minutes», «v1.5 minutes» — the number's
            # boundary is part of its meaning; never read 5 out of them.
            before = t[max(0, match.start(1) - 1):match.start(1)]
            if before and (before in ".-−–" or before.isalnum()):
                continue
            try:
                total += float(match.group(1)) * multiplier
                found = True
            except Exception:
                pass
    return total if found and total > 0 else None


def _timer_label(text: str, kind: str) -> str:
    value = str(text or "").strip()
    # Prefer everything after common reminder-content markers.
    markers = [
        r"\b(?:про|о|об)\s+(.+)$", r"\b(?:about|to)\s+(.+)$", r"\b(?:daran|an)\s+(.+)$",
        r"\b(?:de|à)\s+(.+)$", r"\b(?:sobre|para)\s+(.+)$", r"\b(?:di|su)\s+(.+)$",
    ]
    # NEO 172: «напомни через 20 минут выпить воды» keeps «выпить воды» (the when-part is removed, not the rest).
    value = _without_when172(value)
    if kind == "reminder":
        for pattern in markers:
            m = re.search(pattern, value, flags=re.I)
            if m:
                candidate = m.group(1).strip(" .,!?:;")
                if candidate:
                    return candidate[:160]
    cleaned = re.sub(
        r"\b(?:поставь|создай|запусти|таймер|напомни|напоминание|будильник|set|create|start|timer|remind(?:er)?|alarm|"
        r"stelle|erinnere|wecker|minuteur|rappelle|alarme|temporizador|recu[eé]rdame|alarma|timer|ricordami|sveglia|lembre|alarme)\b",
        " ", value, flags=re.I,
    )
    cleaned = re.sub(r"^(?:\s*(?:мне|нам|me|mir|moi|mi|que|что|чтобы|to|dass|de|di|про|о|об|about)\b)+", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,!?:;")
    # NEO 182: no name → empty; TimerManager picks a generic name in the user's language.
    return cleaned[:160]


_WHEN172 = (
    r"\b(?:через|in|nach|dans|en|tra|fra|daqui\s+a)\s+(?:[\w.,]+\s+){0,2}?(?:сек\w*|мин\w*|час\w*|полчаса|ч\.?|дн\w*|день|недел\w*|"
    r"seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|sekunden?|minuten?|stunden?|tagen?|wochen?|secondes?|heures?|jours?|"
    r"semaines?|segundos?|minutos?|horas?|d[ií]as?|semanas?|secondi|minuti|ore|giorni|settimane|dias)\b",
    r"\b(?:в|во|at|um|à|a\s+las|alle|às)\s+\d{1,2}(?:[:.]\d{2})?(?:\s*(?:час\w*|утра|вечера|дня|ночи|am|pm|uhr|h))?\b",
    r"\b(?:послезавтра|завтра|сегодня|tomorrow|today|übermorgen|morgen|heute|après-demain|demain|aujourd[\'’]hui|pasado\s+mañana|mañana|hoy|"
    r"dopodomani|domani|oggi|depois\s+de\s+amanhã|amanhã|hoje)\b",
)


def _without_when172(value: str) -> str:
    for pattern in _WHEN172:
        value = re.sub(pattern, " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip(" .,!?")


# ---- NEO 182: explicit dates are validated as a whole; differences between two dates ----------
_MONTHS182 = (
    ("январ", "jan", "januar", "janvier", "enero", "gennaio", "janeiro"),
    ("феврал", "feb", "februar", "février", "fevrier", "febrero", "febbraio", "fevereiro"),
    ("март", "mar", "märz", "maerz", "mars", "marzo", "março", "marco"),
    ("апрел", "apr", "april", "avril", "abril", "aprile"),
    ("мая", "май", "мае", "may", "mai", "mayo", "maggio", "maio"),
    ("июн", "jun", "juni", "juin", "junio", "giugno", "junho"),
    ("июл", "jul", "juli", "juillet", "julio", "luglio", "julho"),
    ("август", "aug", "august", "août", "aout", "agosto"),
    ("сентябр", "sep", "september", "septembre", "septiembre", "settembre", "setembro"),
    ("октябр", "oct", "okt", "oktober", "octobre", "octubre", "ottobre", "outubro"),
    ("ноябр", "nov", "november", "novembre", "noviembre", "novembro"),
    ("декабр", "dec", "dez", "dezember", "décembre", "decembre", "diciembre", "dicembre", "dezembro"),
)
_MONTH_WORD182 = r"([^\W\d_]{3,12})"
_DAY_FIRST182 = re.compile(r"\b(\d{1,2})(?:-?(?:го|е|er|º|°|st|nd|rd|th)|\.)?\s+(?:de\s+)?" + _MONTH_WORD182 +
                           r"(?:\s+(?:de\s+)?(\d{4}))?", re.I)
_MONTH_FIRST182 = re.compile(r"\b" + _MONTH_WORD182 + r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", re.I)
_NUMERIC182 = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")
_ISO183 = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_COUNTDOWN183 = re.compile(r"(?:сколько\s+дней|how\s+many\s+days|wie\s+viele\s+tage|combien\s+de\s+jours|cu[aá]ntos\s+d[ií]as|"
                           r"quanti\s+giorni|quantos\s+dias|days?\s+(?:left\s+)?until|tage\s+bis|jours\s+avant|d[ií]as\s+para|giorni\s+a|dias\s+para)", re.I)
_BETWEEN182 = re.compile(r"\b(?:между|between|zwischen|entre|tra|fra|от|from|von|du|desde|da|de)\b", re.I)
_DIFF_WORDS182 = re.compile(r"(?:сколько\s+дней|how\s+many\s+days|wie\s+viele\s+tage|combien\s+de\s+jours|cu[aá]ntos\s+d[ií]as|"
                            r"quanti\s+giorni|quantos\s+dias|разниц|difference|unterschied|différence|diferencia|differenza|diferença)", re.I)


def _month182(word: str) -> int | None:
    word = word.casefold()
    for index, stems in enumerate(_MONTHS182, start=1):
        for stem in stems:
            if len(stem) <= 3 and word.isascii():
                if word == stem or (word.startswith(stem) and len(word) >= len(stem)):
                    return index
            elif word.startswith(stem):
                return index
    # NEO 183: abbreviations — «31 фев», «3 sept.», «окт»
    if len(word) >= 3:
        for index, stems in enumerate(_MONTHS182, start=1):
            if any(len(stem) > len(word) and stem.startswith(word) for stem in stems):
                return index
    return None


def _numeric_order183(a: int, b: int, preferred: str) -> tuple[int, int]:
    """(day, month) for «a/b/yyyy»: unambiguous combinations decide themselves, the rest follow
    the date format (``mdy`` or ``dmy``)."""
    if a > 12 >= b:
        return a, b
    if b > 12 >= a:
        return b, a
    return (b, a) if preferred == "mdy" else (a, b)


def _explicit_dates182(text: str, order: str = "dmy") -> list[tuple[int, int, int | None, str]]:
    """(day, month, year or None, matched text) for every date written out in ``text``, in order."""
    found: list[tuple[int, int, int, int | None, str]] = []
    taken: list[tuple[int, int]] = []
    preferred = order
    for pattern, order in ((_ISO183, "iso"), (_NUMERIC182, "num"), (_DAY_FIRST182, "dm"), (_MONTH_FIRST182, "md")):
        for m in pattern.finditer(text):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            if order == "iso":
                year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif order == "num":
                day, month = _numeric_order183(int(m.group(1)), int(m.group(2)), preferred)
                year = int(m.group(3))
            elif order == "dm":
                month = _month182(m.group(2))
                if month is None:
                    continue
                day, year = int(m.group(1)), int(m.group(3)) if m.group(3) else None
            else:
                month = _month182(m.group(1))
                if month is None:
                    continue
                day, year = int(m.group(2)), int(m.group(3)) if m.group(3) else None
            taken.append((m.start(), m.end()))
            found.append((m.start(), day, month, year, m.group(0)))
    found.sort()
    return [(day, month, year, matched) for _start, day, month, year, matched in found]


def _real_date182(day: int, month: int, year: int | None, now: datetime) -> datetime | None:
    if not 1 <= month <= 12:
        return None
    if year is None:
        for candidate_year in range(now.year, now.year + 9):
            try:
                candidate = datetime(candidate_year, month, day)
            except ValueError:
                continue
            if candidate.date() >= now.date():
                return candidate
        return None
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


_DATE_TEXT182 = {
    "invalid": {"ru": "Такой даты нет: {date}.", "en": "There is no such date: {date}.", "de": "Dieses Datum gibt es nicht: {date}.",
                "fr": "Cette date n'existe pas : {date}.", "es": "Esa fecha no existe: {date}.", "it": "Questa data non esiste: {date}.",
                "pt": "Essa data não existe: {date}."},
    "until": {"ru": "До {date}: {days} дн.", "en": "{days} days until {date}.", "de": "Bis {date}: {days} Tage.",
              "fr": "Jusqu’au {date} : {days} jours.", "es": "Faltan {days} días para el {date}.", "it": "Mancano {days} giorni al {date}.",
              "pt": "Faltam {days} dias para {date}."},
    "since": {"ru": "С {date} прошло {days} дн.", "en": "{days} days have passed since {date}.", "de": "Seit {date} sind {days} Tage vergangen.",
              "fr": "{days} jours se sont écoulés depuis le {date}.", "es": "Han pasado {days} días desde el {date}.",
              "it": "Sono passati {days} giorni dal {date}.", "pt": "Passaram {days} dias desde {date}."},
    "today": {"ru": "{date} — это сегодня.", "en": "{date} is today.", "de": "{date} ist heute.", "fr": "Le {date}, c’est aujourd’hui.",
              "es": "El {date} es hoy.", "it": "Il {date} è oggi.", "pt": "{date} é hoje."},
    "between": {"ru": "Между {a} и {b}: {days} дн.", "en": "Between {a} and {b}: {days} days.", "de": "Zwischen {a} und {b}: {days} Tage.",
                "fr": "Entre {a} et {b} : {days} jours.", "es": "Entre {a} y {b}: {days} días.", "it": "Tra {a} e {b}: {days} giorni.",
                "pt": "Entre {a} e {b}: {days} dias."},
}


def _date_phrase183(config: Any, value: datetime) -> str:
    """«10 октября 2026 (суббота)» — reads well after «до / между / since» in every language."""
    full = _fmt_date(config, value)
    weekday, _, rest = full.partition(", ")
    return f"{rest} ({weekday})" if rest else full


def _date_info(config: Any, text: str) -> ToolExecution:
    raw = str(text or "")
    low = raw.casefold().replace("ё", "е")
    now = datetime.now()
    if low == "current_time":
        stamp = now.strftime("%H:%M")
        templates = {"ru":"Сейчас {time}.", "en":"It is {time}.",
            "de":"Es ist {time} Uhr.", "fr":"Il est {time}.",
            "es":"Son las {time}.", "it":"Sono le {time}.", "pt":"São {time}."}
        return ToolExecution(True, templates[language_code(config)].format(time=stamp),
                             {"time":now.isoformat(timespec="minutes"), "source":"local_clock"})
    if low in {"today", "сегодня"}:
        return ToolExecution(True, _fmt_date(config, now), {"date":now.date().isoformat(), "source":"local_clock"})
    # NEO 182: a date that is written out is checked as a whole («31 февраля» is not 2 February),
    # and two dates give the number of days between them.
    code182 = language_code(config)
    # NEO 183: «12/31/2026» and the like follow the date format (Settings value or the language)
    order = str(config.get("date_info.date_order", "auto") or "auto").casefold()
    if order not in {"dmy", "mdy"}:
        order = "mdy" if code182 == "en" else "dmy"
    explicit = _explicit_dates182(low, order)
    for day, month, year, matched in explicit:
        if _real_date182(day, month, year, now) is None:
            return ToolExecution(False, _DATE_TEXT182["invalid"][code182].format(date=matched.strip()),
                                 {"outcome": "invalid_date", "date_text": matched.strip()})
    # Holiday lookup uses the configured weather country when available. This is
    # local/offline after installation and avoids another API key.
    if any(x in low for x in ("праздник", "holiday", "feiertag", "jour férié", "jour ferie", "festivo", "festività", "festivita", "feriado")):
        if explicit:
            target = _real_date182(explicit[0][0], explicit[0][1], explicit[0][2], now) or now
        else:
            matches = _search_dates(config, raw)
            target = matches[0][1] if matches else now
        country = str(config.get("integrations.openweather.country_code", "") or "").strip().upper()
        if not country:
            country = {"ru":"RU","en":"US","de":"DE","fr":"FR","es":"ES","it":"IT","pt":"PT"}.get(language_code(config), "US")
        try:
            import holidays as holidays_lib
            table = holidays_lib.country_holidays(country, years=[target.year])
            name = table.get(target.date())
        except Exception:
            name = None
        if name:
            code=language_code(config)
            msg={
                "ru":f"{_fmt_date(config, target)} — {name}.","en":f"{_fmt_date(config, target)} — {name}.",
                "de":f"{_fmt_date(config, target)} — {name}.","fr":f"{_fmt_date(config, target)} — {name}.",
                "es":f"{_fmt_date(config, target)} — {name}.","it":f"{_fmt_date(config, target)} — {name}.",
                "pt":f"{_fmt_date(config, target)} — {name}.",
            }[code]
            return ToolExecution(True, msg, {"date":target.date().isoformat(),"holiday":str(name),"country":country})
        msg={
            "ru":f"Для {country} на {_fmt_date(config, target)} официальный праздник не найден.",
            "en":f"No public holiday was found for {country} on {_fmt_date(config, target)}.",
            "de":f"Für {country} wurde am {_fmt_date(config, target)} kein gesetzlicher Feiertag gefunden.",
            "fr":f"Aucun jour férié n'a été trouvé pour {country} le {_fmt_date(config, target)}.",
            "es":f"No se encontró un festivo oficial para {country} el {_fmt_date(config, target)}.",
            "it":f"Nessuna festività pubblica trovata per {country} il {_fmt_date(config, target)}.",
            "pt":f"Nenhum feriado oficial foi encontrado para {country} em {_fmt_date(config, target)}.",
        }[language_code(config)]
        return ToolExecution(True,msg,{"date":target.date().isoformat(),"holiday":None,"country":country})
    if len(explicit) >= 2 and (_BETWEEN182.search(low) or _DIFF_WORDS182.search(low)):
        (d1, m1, y1, _a), (d2, m2, y2, _b) = explicit[:2]
        first = _real_date182(d1, m1, y1 if y1 is not None else (y2 or now.year), now)
        second = _real_date182(d2, m2, y2 if y2 is not None else (y1 or now.year), now)
        if first is not None and second is not None:
            days = abs((second.date() - first.date()).days)
            text182 = _DATE_TEXT182["between"][code182].format(a=_date_phrase183(config, first), b=_date_phrase183(config, second), days=days)
            return ToolExecution(True, text182, {"from": first.date().isoformat(), "to": second.date().isoformat(), "days": days})
    if len(explicit) == 1 and _COUNTDOWN183.search(low):
        day, month, year, _matched = explicit[0]
        target = _real_date182(day, month, year, now)
        days = (target.date() - now.date()).days
        key = "until" if days > 0 else ("today" if days == 0 else "since")
        text183 = _DATE_TEXT182[key][code182].format(date=_date_phrase183(config, target), days=abs(days))
        return ToolExecution(True, text183, {"date": target.date().isoformat(), "days": days})
    if len(explicit) == 1 and explicit[0][2] is not None:
        day, month, year, _matched = explicit[0]
        target = _real_date182(day, month, year, now)
        return ToolExecution(True, _fmt_date(config, target), {"date": target.date().isoformat()})
    # New Year countdown (NEO 182: any case form — «до Нового года», «к новому году»).
    if re.search(r"нов\w*\s+год\w*", low) or any(x in low for x in ("new year", "neujahr", "nouvel an", "año nuevo", "ano nuevo", "anno nuovo", "capodanno", "ano novo", "réveillon")):
        if now.month == 1 and now.day == 1:
            target, days = datetime(now.year, 1, 1), 0          # NEO 183: today's New Year, not next year's
        else:
            target = datetime(now.year + 1, 1, 1)
            days = (target.date() - now.date()).days
        code = language_code(config)
        template = {
            "ru": f"До Нового года осталось {days} дн.", "en": f"There are {days} days until New Year.",
            "de": f"Bis Neujahr sind es noch {days} Tage.", "fr": f"Il reste {days} jours avant le Nouvel An.",
            "es": f"Faltan {days} días para Año Nuevo.", "it": f"Mancano {days} giorni a Capodanno.",
            "pt": f"Faltam {days} dias para o Ano Novo.",
        }
        return ToolExecution(True, template[code], {"date": target.date().isoformat(), "days": days})
    # Explicit next weekday grammar, reliable even when dateparser is absent.
    weekday_aliases = {
        0:("понедель", "monday", "montag", "lundi", "lunes", "luned", "segunda"),
        1:("вторник", "tuesday", "dienstag", "mardi", "martes", "marted", "terça"),
        2:("сред", "wednesday", "mittwoch", "mercredi", "miércoles", "mercoled", "quarta"),
        3:("четверг", "thursday", "donnerstag", "jeudi", "jueves", "gioved", "quinta"),
        4:("пятниц", "friday", "freitag", "vendredi", "viernes", "venerd", "sexta"),
        5:("суббот", "saturday", "samstag", "samedi", "sábado", "sabato"),
        6:("воскрес", "sunday", "sonntag", "dimanche", "domingo", "domenica"),
    }
    for wd, aliases in weekday_aliases.items():
        if any(x in low for x in aliases):
            delta = (wd - now.weekday()) % 7
            if delta == 0 or any(x in low for x in ("следующ", "next", "näch", "prochain", "próxim", "prossim")):
                delta = delta or 7
            target = now + timedelta(days=delta)
            return ToolExecution(True, _fmt_date(config, target), {"date": target.date().isoformat(), "weekday": wd})
    matches = _search_dates(config, raw)
    if matches:
        target = matches[0][1]
        return ToolExecution(True, _fmt_date(config, target), {"date": target.date().isoformat()})
    # Bare day number like "какой день недели будет 14" -> next 14th.
    m = re.search(r"\b([12]?\d|3[01])\b", low)
    if m:
        day = int(m.group(1)); year, month = now.year, now.month
        for _ in range(14):
            try:
                candidate = datetime(year, month, day)
                if candidate.date() >= now.date():
                    return ToolExecution(True, _fmt_date(config, candidate), {"date": candidate.date().isoformat()})
            except ValueError:
                pass
            month += 1
            if month > 12: month = 1; year += 1
    raise ValueError("Не удалось определить дату из запроса.")



def register_integrations(registry: Any) -> None:
    config = registry.config
    from jarvis.tools.obs_tool184 import register as register_obs184
    register_obs184(registry, config)
    weather_adapter = OpenWeatherAdapter(config)

    def weather(args: dict[str, Any]) -> ToolExecution:
        if not bool(config.get("integrations.openweather.enabled", False)):
            return ToolExecution(False, "Интеграция OpenWeather не включена. Включите её в Настройки → API-сервисы → OpenWeather и укажите API key/город.", {"integration_disabled": True})
        text, data = weather_adapter.answer(
            str(args.get("action", "summary") or "summary"),
            period=str(args.get("period", "") or ""),
            city=str(args.get("city", "") or ""),
            country=str(args.get("country", "") or ""),
        )
        return ToolExecution(True, text, data)

    registry.register(ToolDefinition(
        "weather",
        "OpenWeather current conditions and 5-day forecast. Use for temperature, rain, wind, humidity, sunrise/sunset and daypart forecasts.",
        _schema({
            "action": _enum(["summary","current","temperature","forecast","rain","precipitation","humidity","wind","sunrise","sunset"], "Weather query"),
            "period": {"type":"string","description":"Natural period such as tonight, tomorrow morning or the original utterance"},
            "city": {"type":"string"}, "country": {"type":"string"},
        }, ["action"]),
        weather,
        lambda _args: Risk.SAFE,
    ))

    def chance(args: dict[str, Any]) -> ToolExecution:
        import secrets
        code = language_code(config)
        action = args["action"]
        if action == "roll_die":
            value = secrets.randbelow(6) + 1
            text = {"ru": "Бросил кубик. Выпало {value}.", "en": "I rolled a die. It came up {value}.",
                    "de": "Ich habe gewürfelt: {value}.", "fr": "J’ai lancé le dé : {value}.",
                    "es": "He tirado el dado. Ha salido {value}.", "it": "Ho lanciato il dado. È uscito {value}.",
                    "pt": "Lancei o dado. Saiu {value}."}[code].format(value=value)
            return ToolExecution(True, text, {"kind": "die", "value": value, "sides": 6})
        side = "heads" if secrets.randbelow(2) == 0 else "tails"
        words = {"ru": ("Орёл", "Решка"), "en": ("Heads", "Tails"), "de": ("Kopf", "Zahl"),
                 "fr": ("Face", "Pile"), "es": ("Cara", "Cruz"), "it": ("Testa", "Croce"),
                 "pt": ("Cara", "Coroa")}[code]
        return ToolExecution(True, words[side == "tails"] + ".", {"kind": "coin", "side": side})

    registry.register(ToolDefinition(
        "chance", "Roll one six-sided die or toss a virtual coin, locally.",
        _schema({"action": _enum(["roll_die", "flip_coin"], "Random action")}, ["action"]),
        chance, lambda _args: Risk.SAFE,
    ))

    def date_info(args: dict[str, Any]) -> ToolExecution:
        return _date_info(config, str(args.get("text", "") or ""))

    registry.register(ToolDefinition(
        "date_info",
        "Offline date arithmetic, weekday and public-holiday questions.",
        _schema({"action": _enum(["date_info"], "Date query"), "text": {"type": "string"}}, ["action"]),
        date_info,
        lambda _args: Risk.SAFE,
    ))

    def timer_tool(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "timer") or "timer").casefold()
        manager = timer_manager(config.paths.root, language_code(config))
        if action == "list":
            rows = manager.list()
            if not rows:
                return ToolExecution(True, _t(config, "timer_none"), {"timers": []})
            # NEO 172: one numbered list with repeating reminders and schedules («удали напоминание 2»).
            from jarvis.runtime.everyday172 import listing_text
            return ToolExecution(True, listing_text(rows[:20], language_code(config)), {"timers": [x.__dict__ for x in rows]})
        if action == "cancel_all":
            code_t = language_code(config)
            try:
                gone = manager.cancel_kind("timer")
            except OSError:
                from jarvis.runtime.timer_cancel187 import _say
                return ToolExecution(False, _say("all_failed", code_t), {"removed": 0})
            from jarvis.runtime.timer_cancel187 import _say
            return ToolExecution(True, _say("all", code_t, n=len(gone)), {"removed": len(gone), "cancelled": gone, "localized186": True})
        if action == "cancel":
            # PHRASE185-TIMER-01: the reply names what was really cancelled. A named
            # timer is not «the next one», and a missing ID is not «no timers at all».
            requested = str(args.get("id", "") or "")
            before = {item.id: item for item in manager.list()}
            removed = manager.cancel(requested)
            code_t = language_code(config)
            if removed:
                gone = [before[key] for key in before if key not in {x.id for x in manager.list()}]
                label = str(getattr(gone[0], "label", "") or "") if gone else ""
                if requested and label:
                    text = {"ru": "Отменил «{label}».", "en": "Cancelled «{label}».", "de": "«{label}» abgebrochen.",
                            "fr": "«{label}» annulé.", "es": "He cancelado «{label}».", "it": "Ho annullato «{label}».",
                            "pt": "Cancelei «{label}»."}[code_t].format(label=label)
                elif label:
                    text = {"ru": "Отменил ближайший: «{label}».", "en": "Cancelled the next one: «{label}».",
                            "de": "Den nächsten abgebrochen: «{label}».", "fr": "Le prochain est annulé : «{label}».",
                            "es": "He cancelado el siguiente: «{label}».", "it": "Ho annullato il prossimo: «{label}».",
                            "pt": "Cancelei o próximo: «{label}»."}[code_t].format(label=label)
                else:
                    text = _t(config, "timer_cancelled")
                return ToolExecution(True, text, {"removed": removed, "cancelled": [x.id for x in gone], "requested": requested, "localized186": True})
            if requested and before:
                text = {"ru": "Такого таймера уже нет; остальные ({n}) не трогал.", "en": "That timer no longer exists; I left the other {n} untouched.",
                        "de": "Diesen Timer gibt es nicht mehr; die anderen {n} bleiben unverändert.",
                        "fr": "Ce minuteur n’existe plus ; je n’ai pas touché aux {n} autres.",
                        "es": "Ese temporizador ya no existe; no he tocado los otros {n}.",
                        "it": "Quel timer non esiste più; gli altri {n} non li ho toccati.",
                        "pt": "Esse temporizador já não existe; não mexi nos outros {n}."}[code_t].format(n=len(before))
                return ToolExecution(False, text, {"removed": 0, "requested": requested, "remaining": len(before), "timer_missing": True})
            return ToolExecution(True, _t(config, "timer_nothing_cancelled"), {"removed": removed})
        if action == "reschedule":
            from jarvis.runtime.duration import format_duration
            code = language_code(config)
            try:
                item = manager.reschedule(str(args.get("id", "")), float(args.get("seconds", 0)))
            except KeyError:
                text = {"ru":"Этот таймер уже завершён или отменён. Новый таймер не создавал.", "en":"That timer has finished or was cancelled. I did not create a new one.",
                        "de":"Dieser Timer ist beendet oder abgebrochen. Ich habe keinen neuen erstellt.", "fr":"Ce minuteur est terminé ou annulé. Je n’en ai pas créé de nouveau.",
                        "es":"Ese temporizador ha terminado o se ha cancelado. No he creado uno nuevo.", "it":"Il timer è terminato o è stato annullato. Non ne ho creato uno nuovo.",
                        "pt":"Esse temporizador terminou ou foi cancelado. Não criei um novo."}[code]
                return ToolExecution(False, text, {"timer_missing":True})
            duration = format_duration(float(args["seconds"]), code)
            text = {"ru":"Исправил: таймер теперь на {duration} от этого момента. Второй таймер не создавал.", "en":"Corrected: the timer is now {duration} from now. I did not create a second timer.",
                    "de":"Korrigiert: Der Timer läuft ab jetzt {duration}. Es wurde kein zweiter Timer erstellt.", "fr":"Corrigé : le minuteur est réglé sur {duration} à partir de maintenant. Aucun second minuteur n’a été créé.",
                    "es":"Corregido: el temporizador es de {duration} a partir de ahora. No he creado otro.", "it":"Corretto: il timer è di {duration} da questo momento. Non ne ho creato un secondo.",
                    "pt":"Corrigido: o temporizador é de {duration} a partir de agora. Não criei um segundo temporizador."}[code].format(duration=duration)
            return ToolExecution(True, text, dict(item.__dict__))
        raw = str(args.get("text", "") or "")
        kind = "reminder" if action == "reminder" else ("alarm" if action == "alarm" else "timer")
        explicit_label = str(args.get("label", "") or "").strip()
        seconds = float(args.get("seconds", 0) or 0)
        if seconds <= 0:
            seconds = _extract_duration_seconds(raw) or 0
        label = explicit_label or _timer_label(raw, kind)
        # A plain duration phrase ("таймер на 5 минут") is not a useful
        # human-facing name. Leave it empty so TimerManager can generate a stable
        # localized duration label such as "Таймер на 5 мин". Explicit labels
        # and real reminder content are preserved.
        if kind == "timer" and not explicit_label and seconds > 0:
            normalized_label = re.sub(r"[\s.,!?]+", " ", label.casefold()).strip()
            if not normalized_label or _extract_duration_seconds(label):
                label = ""
        import math
        from jarvis.runtime.timers import MAX_SECONDS
        code184 = language_code(config)
        if not math.isfinite(seconds) or seconds > MAX_SECONDS:
            # NEO 184: refused before anything is stored (no «ghost» timer is left behind).
            return ToolExecution(False, {
                "ru": "Такой срок поставить нельзя: таймер или напоминание — не дольше 10 лет.",
                "en": "I can't set that: a timer or reminder can be at most 10 years away.",
                "de": "Das geht nicht: Ein Timer oder eine Erinnerung darf höchstens 10 Jahre entfernt sein.",
                "fr": "Impossible : un minuteur ou un rappel ne peut pas dépasser 10 ans.",
                "es": "No puedo: un temporizador o recordatorio puede estar como mucho a 10 años.",
                "it": "Non posso: un timer o un promemoria può essere al massimo a 10 anni.",
                "pt": "Não posso: um temporizador ou lembrete pode estar no máximo a 10 anos.",
            }[code184], {"error": "out_of_range"})
        saved_error = {
            "ru": "Не удалось сохранить таймер: файл таймеров занят или недоступен. Ничего не изменено.",
            "en": "Could not save the timer: the timers file is busy or unavailable. Nothing was changed.",
            "de": "Der Timer konnte nicht gespeichert werden: Die Timer-Datei ist belegt oder nicht verfügbar. Nichts wurde geändert.",
            "fr": "Impossible d’enregistrer le minuteur : le fichier des minuteurs est occupé ou indisponible. Rien n’a été modifié.",
            "es": "No se pudo guardar el temporizador: el archivo de temporizadores está ocupado o no disponible. No se cambió nada.",
            "it": "Impossibile salvare il timer: il file dei timer è occupato o non disponibile. Non è cambiato nulla.",
            "pt": "Não foi possível guardar o temporizador: o ficheiro de temporizadores está ocupado ou indisponível. Nada foi alterado.",
        }[code184]
        if seconds > 0:
            try:
                item = manager.create(seconds, label, kind)
            except OSError:
                return ToolExecution(False, saved_error, {"error": "save_failed"})
        else:
            parsed = _search_dates(config, raw)
            if not parsed:
                raise ValueError("Не удалось определить длительность или время. Пример: таймер на 15 минут / напомни завтра в 18:00.")
            due = parsed[0][1]
            if due.tzinfo is not None:
                due_ts = due.timestamp()
            else:
                due_ts = time.mktime(due.timetuple())
            try:
                item = manager.create_at(due_ts, label, kind)
            except OSError:
                return ToolExecution(False, saved_error, {"error": "save_failed"})
        label = item.label
        due_dt = datetime.fromtimestamp(item.due_at)
        code = language_code(config)
        msg = {
            "ru": f"Готово. {label}: {due_dt:%d.%m %H:%M}.", "en": f"Done. {label}: {due_dt:%Y-%m-%d %H:%M}.",
            "de": f"Erledigt. {label}: {due_dt:%d.%m. %H:%M}.", "fr": f"C'est fait. {label} : {due_dt:%d/%m %H:%M}.",
            "es": f"Hecho. {label}: {due_dt:%d/%m %H:%M}.", "it": f"Fatto. {label}: {due_dt:%d/%m %H:%M}.",
            "pt": f"Pronto. {label}: {due_dt:%d/%m %H:%M}.",
        }[code]
        if kind == "timer" and seconds > 0:
            from jarvis.runtime.duration import format_duration
            duration = format_duration(seconds, code)
            msg = {"ru": "Готово, поставил таймер на {duration}.", "en": "Done, I set a timer for {duration}.",
                   "de": "Erledigt, der Timer läuft für {duration}.", "fr": "C’est fait, le minuteur est réglé sur {duration}.",
                   "es": "Hecho, he puesto un temporizador de {duration}.", "it": "Fatto, ho impostato un timer di {duration}.",
                   "pt": "Pronto, defini um temporizador de {duration}."}[code].format(duration=duration)
        # The reply is already in the UI language; its label is user data (any alphabet).
        return ToolExecution(True, msg, {**dict(item.__dict__), "localized186": True})

    registry.register(ToolDefinition(
        "timer",
        "Persistent local timers, reminders and alarms. Warns in chat 15/10/5 minutes before due when applicable, and sends system notifications at 1 minute and due time.",
        _schema({
            "action": _enum(["timer","reminder","alarm","list","cancel","cancel_all","reschedule"], "Timer action"),
            "text": {"type":"string"}, "seconds": {"type":"number","minimum":1}, "label": {"type":"string"}, "id": {"type":"string"},
        }, ["action"]),
        timer_tool,
        lambda _args: Risk.SAFE,
    ))

    def monitor(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "summary") or "summary")
        if action == "battery":
            # ORDINARY185-INFORMATION-NAMESPACE: a read-only battery level, never an app launch.
            code = language_code(config)
            try:
                battery = psutil.sensors_battery()
            except Exception:
                battery = None
            if battery is None:
                text = {"ru": "Батарея не найдена: похоже, компьютер работает от сети без аккумулятора.",
                        "en": "No battery found: this computer seems to run on mains power without one.",
                        "de": "Kein Akku gefunden: Dieser Computer läuft offenbar ohne Akku am Netz.",
                        "fr": "Aucune batterie détectée : cet ordinateur semble fonctionner sur secteur sans batterie.",
                        "es": "No se encontró batería: este equipo parece funcionar conectado a la red sin batería.",
                        "it": "Nessuna batteria trovata: il computer sembra alimentato dalla rete senza batteria.",
                        "pt": "Não foi encontrada bateria: o computador parece estar ligado à corrente sem bateria."}[code]
                return ToolExecution(True, text, {"battery": None})
            plugged = bool(getattr(battery, "power_plugged", False))
            percent = float(getattr(battery, "percent", 0) or 0)
            tail = {"ru": (" · заряжается от сети", ""), "en": (" · plugged in", ""), "de": (" · am Netz", ""),
                    "fr": (" · sur secteur", ""), "es": (" · conectado a la red", ""), "it": (" · collegato alla rete", ""),
                    "pt": (" · ligado à corrente", "")}[code][0 if plugged else 1]
            head = {"ru": "Заряд батареи", "en": "Battery level", "de": "Akkustand", "fr": "Niveau de batterie",
                    "es": "Nivel de batería", "it": "Livello batteria", "pt": "Nível da bateria"}[code]
            return ToolExecution(True, f"{head}: {percent:.0f}%{tail}.", {"battery": {"percent": percent, "plugged": plugged}})
        data = snapshot()
        return ToolExecution(True, format_snapshot(data, action, language_code(config)), data)

    registry.register(ToolDefinition(
        "system_monitor",
        "Read-only PC telemetry: CPU, GPU, RAM, disks, current network traffic/link speed and available power/temperature sensors. Collection runs without visible console windows.",
        _schema({"action": _enum(["summary","cpu","gpu","memory","disks","network","battery"], "Telemetry category")}, ["action"]),
        monitor,
        lambda _args: Risk.SAFE,
    ))

    def google_calendar(args: dict[str, Any]) -> ToolExecution:
        if not bool(config.get("integrations.google_calendar.enabled", False)):
            return ToolExecution(False, "Google Calendar не подключён. Включите его в Настройки → API и сервисы → Gmail и Календарь.", {})
        action = str(args.get("action", "today") or "today").casefold().strip()
        client = GoogleCalendarClient(config)
        from jarvis.integrations.google_calendar import calendar_connected, calendar_heading
        cal_lang = language_code(config)

        def listing(rows, key, days=None):
            return format_calendar_events(rows, heading=calendar_heading(key, cal_lang, days), lang=cal_lang)
        if action == "status":
            rows = client.upcoming(days=7, limit=3)
            return ToolExecution(True, calendar_connected(cal_lang) + "\n" + listing(rows, "upcoming"), {"connected": True, "events": rows, "localized186": True})
        if action == "today":
            rows = client.today(offset=0, limit=int(args.get("limit", 50) or 50))
            return ToolExecution(True, listing(rows, "today"), {"events": rows, "localized186": True})
        if action == "tomorrow":
            rows = client.today(offset=1, limit=int(args.get("limit", 50) or 50))
            return ToolExecution(True, listing(rows, "tomorrow"), {"events": rows, "localized186": True})
        if action == "all":
            rows = client.list_events(start=datetime.now().astimezone(), limit=int(args.get("limit", 100) or 100))
            return ToolExecution(True, listing(rows, "all"), {"events": rows, "localized186": True})
        if action in {"upcoming", "list"}:
            days = max(1, min(31, int(args.get("days", 7) or 7)))
            rows = client.upcoming(days=days, limit=int(args.get("limit", 50) or 50))
            return ToolExecution(True, listing(rows, "days", days), {"events": rows, "days": days, "localized186": True})
        if action == "next":
            row = client.next_event()
            rows = [row] if isinstance(row, dict) else []
            return ToolExecution(True, listing(rows, "next"), {"event": row, "localized186": True})
        if action == "add":
            title = str(args.get("title", "") or "").strip()
            start = parse_calendar_datetime(str(args.get("date", "") or ""), str(args.get("time", "") or ""))
            row = client.insert_event(title=title, start=start, duration_minutes=int(args.get("duration_minutes", 60) or 60), description=str(args.get("description", "") or ""), location=str(args.get("location", "") or ""))
            return ToolExecution(True, f"Событие добавлено: {title} — {start:%d.%m.%Y %H:%M}.", {"event": row})
        if action == "delete":
            event_id = str(args.get("event_id", "") or "").strip()
            if not event_id:
                query = str(args.get("query", "") or "").strip()
                rows = client.upcoming(days=31, limit=100)
                found = next((x for x in rows if query.casefold() in str(x.get("summary", "") or "").casefold()), None)
                if not found:
                    return ToolExecution(False, "Событие для удаления не найдено.", {})
                event_id = str(found.get("id", "") or "")
            client.delete_event(event_id)
            return ToolExecution(True, "Событие удалено.", {"event_id": event_id})
        if action == "update":
            event_id = str(args.get("event_id", "") or "").strip()
            if not event_id:
                return ToolExecution(False, "Не указан event_id для редактирования.", {})
            rows=client.upcoming(days=365, limit=100)
            event=next((x for x in rows if str(x.get("id","") or "")==event_id), None)
            if not event:
                return ToolExecution(False, "Событие для редактирования не найдено.", {})
            changes=calendar_patch_changes(event,title=str(args.get("title","") or ""),date_text=str(args.get("date","") or ""),time_text=str(args.get("time","") or ""))
            if not changes:
                return ToolExecution(True, "Изменений в событии нет.", {"event":event})
            row=client.update_event(event_id,changes)
            return ToolExecution(True, f"Событие обновлено: {str(row.get('summary','') or event.get('summary',''))}.", {"event":row})
        return ToolExecution(False, f"Неизвестное действие Google Calendar: {action}", {})

    registry.register(ToolDefinition(
        "google_calendar",
        "Official Google Calendar: list schedule, show next event and add/update/delete events.",
        _schema({
            "action": _enum(["status","today","tomorrow","upcoming","list","all","next","add","update","delete"], "Calendar action"),
            "title": {"type":"string"}, "date": {"type":"string"}, "time": {"type":"string"},
            "duration_minutes": {"type":"integer","minimum":5,"maximum":1440}, "description": {"type":"string"},
            "location": {"type":"string"}, "query": {"type":"string"}, "event_id": {"type":"string"},
            "days": {"type":"integer","minimum":1,"maximum":31}, "limit": {"type":"integer","minimum":1,"maximum":100},
        }, ["action"]), google_calendar,
        lambda args: Risk.MEDIUM if str(args.get("action", "")).casefold() in {"add","update","delete"} else Risk.SAFE,
    ))

    def telegram_messages(args: dict[str, Any]) -> ToolExecution:
        if not bool(config.get("integrations.telegram.messages_enabled", False)):
            return ToolExecution(False, "Telegram Messages не включён. Включите Secretary Mode и интеграцию сообщений в настройках Telegram.", {})
        center = telegram_message_center(config)
        action = str(args.get("action", "status") or "status").casefold().strip()
        sender = str(args.get("sender", "") or "").strip()
        recent24 = bool(args.get("recent24h", False))
        if action == "diagnostic":
            status = center.connection_status()
            st = center.stats()
            latest = center.latest()
            if status.get("connected"):
                connections = status.get("connections") or []
                conn = connections[0] if connections else {}
                message = f"Telegram Business подключён: {conn.get('owner_name') or conn.get('id')}. Локально сохранено входящих сообщений: {int(st.get('total',0) or 0)}."
            else:
                message = (
                    "Telegram Business connection не наблюдался. BotFather Secretary Mode недостаточно: "
                    "подключите бота в Telegram Business → Chatbots, разрешите нужные existing/new chats (или выбранных получателей) и право Reply. "
                    "Telegram Bot API не выдаёт старую личную историю задним числом."
                )
            if latest:
                message += f" Последнее сохранённое сообщение: {latest.get('sender_name','Telegram')} — {str(latest.get('text',''))[:220]}"
            return ToolExecution(True, message, {"connection": status, "stats": st, "latest": latest or {}})
        if action in {"status", "count"}:
            st = center.stats(sender=sender, recent24h=recent24)
            label = f" от {sender}" if sender else ""
            period = " за последние 24 часа" if recent24 else ""
            lines = [f"Telegram: сообщений{label}{period} — {int(st.get('total', 0) or 0)}."]
            if bool(config.get("integrations.telegram.query_show_senders", True)) and not sender:
                by_sender = dict(st.get("by_sender") or {})
                lines.extend(f"• {name}: {count}" for name, count in list(by_sender.items())[:12])
            return ToolExecution(True, "\n".join(lines), st)
        if action in {"latest", "from_sender", "recent"}:
            limit = 1 if action in {"latest","from_sender"} else max(1, min(20, int(args.get("limit", 10) or 10)))
            rows = center.recent(sender=sender, limit=limit, recent24h=recent24)
            if not rows:
                status = center.connection_status()
                if not status.get("connected"):
                    return ToolExecution(True, "NEO ещё не получил ни одного Telegram Business-сообщения и не видит активного Business connection. Проверьте Telegram Business → Chatbots. Старую личную историю Bot API задним числом получить не может.", {"messages": [], "connection": status})
                return ToolExecution(True, "В локальной истории NEO сообщений Telegram по этому запросу нет. Последнее сообщение без фильтра времени сохраняется после того, как NEO впервые получил его через Business update.", {"messages": [], "connection": status})
            lines = []
            for row in rows:
                when = datetime.fromtimestamp(int(row.get("date", 0) or 0)).strftime("%d.%m %H:%M")
                who = str(row.get("sender_name", "") or "Неизвестный отправитель")
                text = str(row.get("text", "") or "(без текста)")
                lines.append(f"• {when} · {who}: {text}")
            return ToolExecution(True, "Telegram\n" + "\n".join(lines), {"messages": rows})
        if action == "reply":
            body = str(args.get("text", "") or "").strip()
            data = center.reply(body, sender=sender, reply_to_message_id=int(args.get("reply_to_message_id", 0) or 0))
            return ToolExecution(True, f"Ответ отправлен в Telegram: {data.get('sender') or sender} — {body}", data)
        if action == "send":
            body=str(args.get("text","") or "").strip(); target=str(args.get("target","") or sender).strip(); kind=str(args.get("target_kind","person") or "person").casefold()
            data=center.send(target,body,kind="group" if kind=="group" else "person")
            return ToolExecution(True, f"Сообщение отправлено в Telegram: {target} — {body}", data)
        return ToolExecution(False, f"Неизвестное действие Telegram Messages: {action}", {})

    registry.register(ToolDefinition(
        "telegram_messages",
        "Personal Telegram messages received through official Connected Business Bot / Secretary Mode: counts, recent/latest messages and contextual replies.",
        _schema({
            "action": _enum(["status","count","recent","latest","from_sender","diagnostic","reply","send"], "Telegram messages action"),
            "sender": {"type":"string"}, "target": {"type":"string"}, "target_kind": _enum(["person","group"], "Recipient type"), "text": {"type":"string"}, "limit": {"type":"integer","minimum":1,"maximum":20},
            "recent24h": {"type":"boolean"}, "reply_to_message_id": {"type":"integer","minimum":0},
        }, ["action"]), telegram_messages,
        lambda args: Risk.MEDIUM if str(args.get("action", "")).casefold() in {"reply","send"} else Risk.SAFE,
    ))

    def briefing(_args: dict[str, Any]) -> ToolExecution:
        from jarvis.runtime.briefing_stream import (emit, collect, status_text, section_name,
            empty_section_text, completion_text, validate_section_result, BRIEFING_TIMEOUT_SECONDS)
        code = language_code(config)
        action = str(_args.get("action", "summary") or "summary").casefold()
        run_id = uuid.uuid4().hex[:8]
        started = time.monotonic()
        logger.info('Briefing started: run=%s action=%s', run_id, action)
        now = datetime.now()
        # Startup visibility belongs to the window, not a dated cache. A prior
        # tool run (including tests or an interrupted UI) cannot hide today's
        # briefing. Only the UI records delivery after applying the text.
        # Publish local date/time BEFORE any network, device enumeration or mail.
        greeting = greeting_and_wish(config, now)
        emit('greeting', greeting)
        data = {"time": now.isoformat(timespec="minutes"), "section_status": {}, "section_attempts": {}}
        sections = [greeting]
        tasks = {}
        if config.get('briefing.include_weather', True) and config.get('integrations.openweather.enabled', False) and str(config.get('integrations.openweather.city', '')).strip():
            tasks['weather'] = lambda: weather_adapter.answer('summary')
        if config.get('briefing.include_system', True):
            def system_section():
                value = snapshot()
                return format_snapshot(value, 'summary', code), value
            tasks['system'] = system_section
        if config.get('briefing.include_processes', False):
            def process_section():
                rows = []
                for proc in psutil.process_iter(['name', 'memory_info']):
                    try:
                        name = str(proc.info.get('name') or '').removesuffix('.exe')
                        rss = float(getattr(proc.info.get('memory_info'), 'rss', 0) or 0)
                        if name and rss > 0: rows.append((rss, name))
                    except (psutil.Error, OSError):
                        pass
                rows.sort(reverse=True)
                names = [name for _, name in rows[:5]]
                return '\n'.join(names), names
            tasks['processes'] = process_section
        if config.get('briefing.include_devices', True):
            def device_section():
                value = list_present_devices(14)
                text = section_name('devices', code) + '\n' + '\n'.join('• ' + str(x) for x in value) if value else empty_section_text('devices', code)
                return text, value
            tasks['devices'] = device_section
        if config.get('briefing.include_bluetooth', True):
            def bluetooth_section():
                value = [x for x in list_bluetooth_devices(connected_only=True) if x.get('name')]
                text = section_name('bluetooth', code) + '\n' + '\n'.join('• ' + str(x['name']) + (f" ({x['battery']}%)" if isinstance(x.get('battery'), int) else '') for x in value) if value else empty_section_text('bluetooth', code)
                return text, value
            tasks['bluetooth'] = bluetooth_section
        # Each mail provider owns a read slot and its own retry budget. The
        # aggregate mail collector hides exceptions in payloads and spawns more
        # threads; using the client here also preserves already-ready mail.
        for provider in ('gmail', 'mailru'):
            if not (config.get(f'integrations.{provider}.enabled', False)
                    and config.get(f'integrations.{provider}.include_in_briefing', True)):
                continue
            def mail_section(p=provider):
                from jarvis.integrations.mail_services import provider_client
                client = provider_client(config, p)
                try:
                    if not client.configured():
                        raise RuntimeError('Mail service not configured')
                    value = client.status(include_latest=5).compact()
                    return format_mail_briefing(config, p, value), value
                finally:
                    session = getattr(client, '_session', None)
                    if session is not None:
                        session.close()
            tasks['mail.' + provider] = mail_section
        if config.get('integrations.google_calendar.enabled', False) and config.get('integrations.google_calendar.include_in_briefing', False):
            def calendar_section():
                client = GoogleCalendarClient(config)
                try:
                    period = str(config.get('integrations.google_calendar.briefing_period', 'today'))
                    value = client.upcoming(days=3 if period == '3_days' else 31, limit=100) if period in {'3_days','month'} else client.today(offset=0, limit=50)
                    return format_calendar_events(value, heading='Google Calendar', lang=language_code(config)), value
                finally:
                    client._session.close()
            tasks['calendar'] = calendar_section
        if config.get('integrations.telegram.messages_enabled', False) and config.get('integrations.telegram.include_in_briefing', False):
            def telegram_section():
                center = telegram_message_center(config)
                return format_telegram_briefing(config, center), center.stats()
            tasks['telegram'] = telegram_section
        from jarvis.integrations import service_checks161 as service_checks
        checked_services = service_checks.enabled_services(config) if config.get('briefing.include_services', False) else []
        # Service availability is shown last, as one block with a line per service.
        service_lines = {}
        if config.get('briefing.include_services', False):
            data['service_checks'] = {}
            if not checked_services:
                empty_services = service_checks.empty_text(code)
                service_lines['services'] = empty_services
                emit('services', empty_services)
            for service in checked_services:
                # A ready briefing can come from cache or an empty local folder
                # selection. Only a fresh, validated read proves API health.
                tasks['service.' + service.key] = service_checks.task(config, service, code)
        for section in tasks:
            emit(section, status_text(section, code), 'waiting')
        logger.info('Briefing sections queued: run=%s sections=%s', run_id, ','.join(tasks))
        cancelled = False
        def record_attempt(section, state, attempt, extra):
            data['section_attempts'][section] = attempt
            logger.info('Briefing section attempt: run=%s section=%s attempt=%s state=%s error_type=%s reason=%s retryable=%s http_status=%s failure_kind=%s',
                        run_id, section, attempt, state, extra.get('error_type') or '-',
                        extra.get('reason') or '-', extra.get('retryable'), extra.get('http_status'), extra.get('failure_kind') or '-')
        for section, state, value in collect(tasks, timeout=BRIEFING_TIMEOUT_SECONDS,
                max_attempts=3, scope=str(config.paths.root), validate=validate_section_result,
                on_attempt=record_attempt):
            if state == 'retrying':
                retry_text = status_text(section, code, attempt=value['next_attempt'])
                partial_value = value.get('partial_result')
                if partial_value:
                    retry_text = str(partial_value[0]) + '\n' + retry_text
                emit(section, retry_text, 'waiting',
                     partial_text=str(partial_value[0]) if partial_value else None)
                continue
            # A malformed provider must not abort delivery of unrelated reads.
            if state == 'ready' and (not isinstance(value, (tuple, list)) or len(value) != 2):
                state, value = 'error', {'error_type': 'InvalidSectionResult'}
            data['section_status'][section] = state
            logger.info('Briefing section finished: run=%s section=%s state=%s elapsed_ms=%s',
                        run_id, section, state, round((time.monotonic() - started) * 1000))
            if state == 'cancelled':
                cancelled = True
                continue
            if section.startswith('service.'):
                key = section.removeprefix('service.')
                check_result = value[1] if state == 'ready' else value.get('service_result') or {
                    'service': key, 'status': 'network' if state == 'timeout' and value.get('attempts', 0) else 'unavailable', 'scope': 'read-only API response'}
                data['service_checks'][key] = check_result
                text = service_checks.format_status(key, check_result['status'], code)
                service_lines[section] = text
                emit(section, text, state)
                continue
            if state == 'ready':
                text, payload = value
                if section.startswith('mail.'):
                    data.setdefault('mail', {})[section.split('.', 1)[1]] = payload
                else:
                    data[section] = payload
                # Empty device lists need a truthful localized value, not a
                # spinner that remains forever or "service failed".
                if not str(text).strip():
                    text = empty_section_text(section, code)
            else:
                data[section + '_error'] = value or {'error_type': 'DeadlineExceeded'}
                partial_value = value.get('partial_result') if isinstance(value, dict) else None
                text = status_text(section, code, failed=True,
                                   partial=bool(partial_value),
                                   configuration=isinstance(value, dict) and value.get('reason') == 'configuration')
                if partial_value:
                    text = str(partial_value[0]) + '\n' + text
                    if section.startswith('mail.'):
                        data.setdefault('mail', {})[section.split('.', 1)[1]] = partial_value[1]
            sections.append(str(text))
            emit(section, str(text), state)
        ordered_lines = [service_lines[name] for name in ['services'] + ['service.' + s.key for s in checked_services] if name in service_lines]
        if ordered_lines:
            sections.append('\n'.join(ordered_lines))
        data['startup'] = action == 'startup_summary'
        data['cancelled'] = cancelled
        data['partial'] = any(s != 'ready' for s in data['section_status'].values())
        if not cancelled:
            finished_text = completion_text(code, partial=data['partial'])
            sections.append(finished_text)
            emit('completion', finished_text)
        logger.info('Briefing finished: run=%s partial=%s cancelled=%s elapsed_ms=%s',
                    run_id, data['partial'], cancelled, round((time.monotonic() - started) * 1000))
        divider = "━━━━━━━━━━━━━━━━━━━━━━━━"
        combined = greeting + ('\n\n' + divider + '\n' + (f'\n{divider}\n').join(sections[1:]) if len(sections) > 1 else '')
        return ToolExecution(True, combined, data)

    registry.register(ToolDefinition(
        "briefing",
        "Configurable multilingual daily briefing from local and enabled integration status.",
        _schema({"action": _enum(["summary","startup_summary"], "Briefing action")}, ["action"]),
        briefing,
        lambda _args: Risk.SAFE,
    ))

    from jarvis.integrations.nvidia_capture import NvidiaCapture
    nvidia = NvidiaCapture(config)

    from jarvis.integrations.instant_replay169 import shared as instant_replay

    def capture(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "") or "")
        if action.startswith("replay_"):
            success, text, data = instant_replay(config).run(action)
            return ToolExecution(success, text, data)
        success, text, data = nvidia.run(action)
        return ToolExecution(success, text, data)

    from jarvis.gaming.match_ready import match_ready_watcher
    match_watch = match_ready_watcher(config)

    def match(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "") or "").casefold()
        if action == "accept":
            success, text = match_watch.accept()
            return ToolExecution(success, text, {"accepted": success})
        if action == "arm":
            return ToolExecution(True, match_watch.arm(args.get("minutes")), {"armed": True})
        if action == "disarm":
            return ToolExecution(True, match_watch.disarm(), {"armed": False})
        return ToolExecution(True, match_watch.status(), {"armed": match_watch.armed()})

    registry.register(ToolDefinition(
        "match",
        "Found game match: accept it now (clicks the game's Accept button or uses the League client API), "
        "accept automatically when the next match is found, turn auto-accept off, or report status.",
        _schema({"action": _enum(["accept", "arm", "disarm", "status"], "Match action"),
                 "minutes": {"type": "integer", "minimum": 1, "maximum": 240}}, ["action"]),
        match,
        lambda args: Risk.SAFE,
    ))

    # NEO 168: on-screen control, browser navigation, copy & paste (local, on request only).
    from jarvis.tools import copy_tool168, ui_control168

    ui_actions = ["type", "press", "hints", "hints_off", "click_number", "double_click_number", "right_click_number",
                  "click_link", "click_button", "click_item", "click_tab", "scroll_down", "scroll_up", "scroll_down_more",
                  "scroll_up_more", "scroll_left", "scroll_right", "scroll_top", "scroll_bottom", "read", "again",
                  "find", "click_name", "focus_field"]

    def ui(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "") or "")
        if action == "type":
            return ui_control168.type_text(config, str(args.get("text", "") or ""))
        if action == "press":
            return ui_control168.press_key(config, registry, str(args.get("key", "") or ""))
        if action == "hints":
            return ui_control168.show_hints(config)
        if action == "hints_off":
            return ui_control168.hide_hints(config)
        if action in {"click_number", "double_click_number", "right_click_number"}:
            how = {"click_number": "click", "double_click_number": "double", "right_click_number": "right"}[action]
            return ui_control168.click_number(config, args.get("number", ""), how)
        if action in {"click_link", "click_button", "click_item", "click_tab"}:
            return ui_control168.click_nth(config, action.split("_", 1)[1], args.get("index", ""))
        if action.startswith("scroll_"):
            return ui_control168.scroll(config, action)
        if action == "read":
            return ui_control168.read_screen(config)
        if action == "again":
            return ui_control168.again(config, registry)
        if action == "find":
            return ui_control168.find_by_name(config, str(args.get("name", "") or ""), args.get("window", 0))
        if action == "click_name":
            return ui_control168.click_by_name(config, str(args.get("name", "") or ""), args.get("window", 0))
        if action == "focus_field":
            return ui_control168.focus_field(config, args.get("index", ""))
        raise ValueError(f"unknown ui action: {action}")

    registry.register(ToolDefinition(
        "ui",
        "Control the active window like the user would: type dictated text, press a key or shortcut, number the "
        "clickable elements and click one by number, click the N-th link/button/item/tab, scroll, read the window text.",
        _schema({"action": _enum(ui_actions, "UI action"), "text": {"type": "string"}, "key": {"type": "string"},
                 "number": {"type": "string"}, "index": {"type": "string"}, "name": {"type": "string"},
                 "window": {"type": "string"}}, ["action"]),
        ui,
        lambda args: Risk.SAFE if str(args.get("action", "")) in {"hints", "hints_off", "read", "find"} or str(args.get("action", "")).startswith("scroll_") else Risk.MEDIUM,
    ))

    browser_actions = ["back", "forward", "reload", "stop", "next_tab", "previous_tab", "find", "find_next", "zoom_in",
                       "zoom_out", "zoom_reset", "address", "open_url", "next_field", "previous_field"]

    def browser_nav(args: dict[str, Any]) -> ToolExecution:
        return ui_control168.browser_nav(config, str(args.get("action", "") or ""), str(args.get("text", "") or ""))

    registry.register(ToolDefinition(
        "browser_nav",
        "Browser page navigation: back, forward, reload, stop loading, next/previous tab, find on page, next match, "
        "zoom, address bar, open an address, next/previous form field.",
        _schema({"action": _enum(browser_actions, "Browser navigation action"), "text": {"type": "string"}}, ["action"]),
        browser_nav,
        lambda _args: Risk.SAFE,
    ))

    copy_actions = ["reply", "previous_reply", "selection", "page", "file_text", "file", "folder", "url", "link",
                    "screenshot", "window_screenshot", "last_screenshot", "paste", "paste_window", "paste_field",
                    "paste_line", "paste_end", "paste_file", "paste_folder", "what", "paste_reply"]

    def copy(args: dict[str, Any]) -> ToolExecution:
        return copy_tool168.run(config, registry, args)

    registry.register(ToolDefinition(
        "copy",
        "Copy and paste: NEO's last answer, the selection, all page text, a file's text, files/folders, the page "
        "address, the N-th link, screenshots; paste into the active window, a named window/field, a line, the end, "
        "a text file or a folder; tell what is on the clipboard.",
        _schema({"action": _enum(copy_actions, "Copy/paste action"), "name": {"type": "string"},
                 "index": {"type": "string"}}, ["action"]),
        copy,
        # Appending to an existing file changes it: always asks first. Other pastes go into the active window.
        lambda args: Risk.HIGH if str(args.get("action", "")) == "paste_file"
        else Risk.MEDIUM if str(args.get("action", "")).startswith("paste") else Risk.SAFE,
    ))

    # NEO 173: exact skills for everyday programs (their own links and shortcuts) and verified chains.
    from jarvis.tools import apps_tool173

    def apps(args: dict[str, Any]) -> ToolExecution:
        if str(args.get("action", "")) == "chain":
            from jarvis.runtime import chain173
            steps = [s for s in str(args.get("steps", "")).split(",") if s in {"download", "unpack", "open"}]
            result, text = chain173.run(steps, language_code(config), url=str(args.get("url", "") or ""), source=str(args.get("source", "") or ""))
            return ToolExecution(result.ok, text, {"steps": [s.step for s in result.steps], "failed_step": result.failed_step})
        return apps_tool173.run(config, args)

    registry.register(ToolDefinition(
        "apps",
        "Everyday programs through their own mechanisms: File Explorer folders, a new folder, Windows Settings pages, "
        "Steam pages and store search, Discord, Telegram settings or a chat by @username (a message is typed, never sent), "
        "browser history/downloads/bookmarks/private window/reopen tab; and a verified chain download → unpack → open.",
        _schema({"action": _enum(["folder", "create_folder", "settings", "steam", "discord", "telegram", "browser", "chain"], "Apps action"),
                 "folder": {"type": "string"}, "name": {"type": "string"}, "page": {"type": "string"}, "query": {"type": "string"},
                 "username": {"type": "string"}, "text": {"type": "string"}, "key": {"type": "string"},
                 "steps": {"type": "string"}, "url": {"type": "string"}, "source": {"type": "string"}}, ["action"]),
        apps,
        lambda args: Risk.SAFE,
    ))

    # NEO 178: Windows Settings — every page, and switches of the user's own account.
    from jarvis.runtime import winsettings178

    def winsettings(args: dict[str, Any]) -> ToolExecution:
        result = winsettings178.run(config, args, language_code(config),
                                    resolve_image=lambda what: winsettings178.find_image(what, config))
        return ToolExecution(result.ok, result.text, result.data)

    registry.register(ToolDefinition(
        "winsettings",
        "Windows Settings: open any page (system, display, sound, notifications, focus, power, storage, nearby sharing, "
        "multitasking, activation, troubleshoot, recovery, projecting, remote desktop, clipboard, components, about, Bluetooth, "
        "network, personalization, apps, accounts, time & language, gaming, accessibility, privacy, Windows Update); switch dark "
        "mode, night light, do not disturb, game mode, clipboard history and more; power mode and timeouts; PC info and disks.",
        _schema({"action": _enum(["open", "toggle", "power_mode", "screen_timeout", "sleep_timeout", "about", "storage_info",
                                  "activation_status", "check_updates", "clear_clipboard_history", "wallpaper"], "Settings action"),
                 "page": {"type": "string"}, "what": {"type": "string"}, "enabled": {"type": "boolean"},
                 "mode": {"type": "string"}, "minutes": {"type": "integer", "minimum": 0, "maximum": 600},
                 "refused": {"type": "string"}}, ["action"]),
        winsettings,
        lambda args: Risk.MEDIUM if winsettings178.risk(args) == "medium" else Risk.SAFE,
    ))

    # NEO 178: Word, Excel and PowerPoint documents (.docx / .xlsx / .pptx).
    from jarvis.runtime import office178

    def office(args: dict[str, Any]) -> ToolExecution:
        if str(args.get("action", "")) == "chart":
            from jarvis.runtime import office_chart188
            ok, text, data = office_chart188.create(config, args, language_code(config))
            return ToolExecution(ok, text, data)
        if str(args.get("action", "")) == "copy_formula":
            from jarvis.runtime import office_formula188
            ok, text, data = office_formula188.apply(config, args, language_code(config))
            return ToolExecution(ok, text, data)
        result = office178.run(config, args, language_code(config), ollama=getattr(registry, "ollama", None))
        return ToolExecution(result.ok, result.text, result.data)

    registry.register(ToolDefinition(
        "office",
        "Word, Excel and PowerPoint documents: create a document, spreadsheet or presentation (also from a topic), read or "
        "summarize one, add text/headings/rows/sheets/slides, write or read a cell, sum/average a column, replace text, "
        "save, export to PDF or CSV, undo NEO's last change. Works on open Office documents and on files.",
        _schema({"action": _enum(["create", "read", "summarize", "append", "heading", "replace", "set_cell", "get_cell",
                                  "column_stat", "add_row", "add_sheet", "add_slide", "save", "export", "undo", "open", "chart", "copy_formula"], "Office action"),
                 "source_range": {"type": "string"}, "target": {"type": "string"},
                 "sheet": {"type": "string"}, "range": {"type": "string"}, "chart": {"type": "string", "enum": ["bar", "line"]},
                 "fingerprint": {"type": "string"}, "destination": {"type": "string"}, "path": {"type": "string"},
                 "kind": {"type": "string"}, "name": {"type": "string"}, "title": {"type": "string"}, "text": {"type": "string"},
                 "topic": {"type": "string"}, "count": {"type": "integer"}, "cell": {"type": "string"}, "value": {"type": "string"},
                 "column": {"type": "string"}, "stat": {"type": "string"}, "values": {"type": "string"}, "find": {"type": "string"},
                 "format": {"type": "string"}, "items": {"type": "string"}}, ["action"]),
        office,
        lambda args: Risk.MEDIUM if office178.risk(args, config) == "medium" else Risk.SAFE,
    ))

    # NEO 169: answers from the user's own files (only folders the user allowed).
    from jarvis.tools import knowledge_tool169

    def knowledge(args: dict[str, Any]) -> ToolExecution:
        return knowledge_tool169.run(config, registry.ollama, args)

    registry.register(ToolDefinition(
        "knowledge",
        "Search the user's own files in the folders they explicitly allowed and answer with file and page/section "
        "citations; add or remove an allowed folder; list folders; update the index.",
        _schema({"action": _enum(["ask", "add_folder", "remove_folder", "folders", "reindex", "ask_file", "compare", "show_source", "open_source", "reveal_source"], "Knowledge action"),
                 "query": {"type": "string"}, "folder": {"type": "string"}, "file": {"type": "string"},
                 "file_a": {"type": "string"}, "file_b": {"type": "string"},
                 "index": {"type": "integer", "minimum": 1, "maximum": 20}}, ["action"]),
        knowledge,
        lambda args: _knowledge_risk188(config, args),
    ))

    # NEO 169: user plugins — list, show permissions, approve, turn off, remove, reload.
    from jarvis.tools import plugins_tool169

    def plugins(args: dict[str, Any]) -> ToolExecution:
        return plugins_tool169.run(config, getattr(registry, "_registry", registry), args)

    registry.register(ToolDefinition(
        "plugins",
        "User plugins: list them, describe one (purpose and permissions), enable (shows permissions first), approve "
        "after that, disable, remove, reload the plugin list.",
        _schema({"action": _enum(["list", "describe", "enable", "approve", "disable", "remove", "reload"], "Plugin action"),
                 "name": {"type": "string"}}, ["action"]),
        plugins,
        lambda args: Risk.HIGH if str(args.get("action", "")) == "remove" else Risk.SAFE,
    ))

    # NEO 169: the NEO command model — status, retraining (background, checked before use), versions, rollback.
    from jarvis.tools import neo_model_tool169

    def _notify169(text: str) -> None:
        try:
            from jarvis.ui.desktop_events import request_desktop
            request_desktop("notify", text=text)
        except Exception:
            pass

    def neo_model(args: dict[str, Any]) -> ToolExecution:
        return neo_model_tool169.run(config, args, notify=_notify169)

    registry.register(ToolDefinition(
        "neo_model",
        "The NEO command model: show its status and quality, retrain it locally on confirmed corrections "
        "(runs in the background; used only if not worse), list versions, roll back to the previous version.",
        _schema({"action": _enum(["status", "train", "versions", "rollback"], "Model action"), "version": {"type": "string"}}, ["action"]),
        neo_model,
        lambda args: Risk.MEDIUM if str(args.get("action", "")) in {"train", "rollback"} else Risk.SAFE,
    ))

    # NEO 170: the small NEO agent model — status, install, local fine-tuning, stop, rollback.
    from jarvis.tools import assistant_model_tool170

    def assistant_model(args: dict[str, Any]) -> ToolExecution:
        return assistant_model_tool170.run(config, args, ollama=getattr(registry, "ollama", None))

    registry.register(ToolDefinition(
        "assistant_model",
        "The small NEO assistant model (Qwen2.5 via Ollama): show its status and fine-tuning progress, install it, "
        "fine-tune it locally on NEO's commands (background, paused while the user works; used only if better), "
        "stop fine-tuning, roll back to the previous version.",
        _schema({"action": _enum(["status", "install", "train", "stop", "rollback"], "Assistant model action")}, ["action"]),
        assistant_model,
        lambda args: Risk.MEDIUM if str(args.get("action", "")) in {"train", "rollback", "install"} else Risk.SAFE,
    ))

    registry.register(ToolDefinition(
        "capture",
        "NVIDIA overlay capture: save an Instant Replay clip (last minutes), start or stop a recording, report where the video was saved; "
        "turn NVIDIA Instant Replay on or off, toggle it, or report whether it is on.",
        _schema({"action": _enum(["clip", "record_start", "record_stop", "replay_on", "replay_off", "replay_toggle", "replay_status"], "Capture action")}, ["action"]),
        capture,
        lambda _args: Risk.SAFE,
    ))


def _knowledge_risk188(config: Any, args: dict[str, Any]) -> Risk:
    """NEO 188 NEO-PRIVACY-001: a revocation that names no single grant changes nothing (no confirmation)."""
    action = str(args.get("action", "") or "")
    if action == "remove_folder":
        try:
            from jarvis.runtime import knowledge169
            if len(knowledge169.service(config).resolve_folders(str(args.get("folder", "") or ""))) != 1:
                return Risk.SAFE
        except Exception:
            return Risk.MEDIUM
        return Risk.MEDIUM
    return Risk.MEDIUM if action == "add_folder" else Risk.SAFE
