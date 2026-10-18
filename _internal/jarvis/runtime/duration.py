"""Strict native duration grammar, shared by timers and typed dialogue slots.

Only the selected language is consulted. Names/payloads elsewhere are never
rewritten. This module does not start a timer, access a device, or load a model.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache
from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell
from jarvis.runtime.spoken_numbers import parse_spoken_number

MAX_TIMER_SECONDS = 7 * 24 * 3600

# Each unit is paired with its actual scale: no guessing from an English prefix.
_UNITS = {
    'ru': ((r'секунд(?:а|у|ы)?|сек|с', 1), (r'минут(?:а|у|ы)?|мин', 60), (r'час(?:а|ов)?|ч', 3600)),
    'en': ((r'seconds?|secs?|s', 1), (r'minutes?|mins?', 60), (r'hours?|hrs?|h', 3600)),
    'de': ((r'sekunden?|sek|s', 1), (r'minuten?|min', 60), (r'stunden?|std|h', 3600)),
    'fr': ((r'secondes?|sec|s', 1), (r'minutes?|min', 60), (r'heures?|h', 3600)),
    'es': ((r'segundos?|seg|s', 1), (r'minutos?|min', 60), (r'horas?|h', 3600)),
    'it': ((r'second[oi]|sec|s', 1), (r'minut[oi]|min', 60), (r'or[ae]|h', 3600)),
    'pt': ((r'segundos?|seg|s', 1), (r'minutos?|min', 60), (r'horas?|h', 3600)),
}
_CONNECTORS = {'ru': r'и', 'en': r'and', 'de': r'und', 'fr': r'et', 'es': r'y', 'it': r'e', 'pt': r'e'}
_PREPOSITIONS = {'ru': r'на', 'en': r'for', 'de': r'auf|für', 'fr': r'de|pour', 'es': r'de|por|para', 'it': r'di|da|per', 'pt': r'de|por|para'}
_SHORTCUTS = {
    'ru': {'полминуты': 30, 'полчаса': 1800, 'четверть часа': 900, 'полтора часа': 5400},
    'en': {'half a minute': 30, 'half an hour': 1800, 'a quarter of an hour': 900, 'a minute': 60, 'an hour': 3600},
    'de': {'eine halbe minute': 30, 'eine halbe stunde': 1800, 'eine viertelstunde': 900},
    'fr': {'une demi-minute': 30, 'une demi-heure': 1800, "un quart d'heure": 900},
    'es': {'medio minuto': 30, 'media hora': 1800, 'un cuarto de hora': 900},
    'it': {'mezzo minuto': 30, "mezz'ora": 1800, "un quarto d'ora": 900},
    'pt': {'meio minuto': 30, 'meia hora': 1800, 'um quarto de hora': 900},
}

# Complete native noun/verb shells only. An isolated duration is a timer ONLY
# while filling a duration slot, never a command in the general router.
_TIMER_SHELLS = {
    'ru': r'(?:(?:поставь|поставить|установи|установить|запусти|запустить|включи|добавь|создай)\s+)?таймер(?:\s+на)?',
    'en': r'(?:(?:set|start|add|create)\s+)?(?:a\s+|the\s+)?timer(?:\s+for)?',
    'de': r'(?:(?:stelle|starte|setze)\s+)?(?:(?:einen|den)\s+)?timer(?:\s+(?:auf|für))?',
    'fr': r'(?:(?:lance|mets|règle|régle|démarre)\s+)?(?:(?:un|le)\s+)?minuteur(?:\s+(?:de|pour))?',
    'es': r'(?:(?:pon|inicia|establece|activa)\s+)?(?:(?:un|el)\s+)?temporizador(?:\s+(?:de|por|para))?',
    'it': r'(?:(?:imposta|avvia|metti)\s+)?(?:(?:un|il)\s+)?timer(?:\s+(?:di|da|per))?',
    'pt': r'(?:(?:define|inicia|põe|coloca)\s+)?(?:(?:um|o)\s+)?temporizador(?:\s+(?:de|por|para))?',
}
_ELAPSED_SHELLS = {'ru': r'засеки(?:\s+мне)?', 'en': r'count down(?:\s+for)?', 'de': r'countdown(?:\s+für)?', 'fr': r'compte à rebours(?:\s+de)?', 'es': r'cuenta atrás(?:\s+de)?', 'it': r'conto alla rovescia(?:\s+di)?', 'pt': r'contagem decrescente(?:\s+de)?'}


def _number(value: str, code: str) -> float | None:
    value = value.strip()
    if re.fullmatch(r'\d+(?:[.,]\d+)?', value):
        result = float(value.replace(',', '.'))
    else:
        # Articles used as the number one, not a global text substitution.
        ones = {'ru': {'одну', 'одна', 'один'}, 'en': {'a', 'an', 'one'},
                'de': {'eine', 'einen', 'ein'}, 'fr': {'un', 'une'},
                'es': {'un', 'una'}, 'it': {'un', 'una', "un'"}, 'pt': {'um', 'uma'}}
        result = 1 if norm(value) in ones.get(code, set()) else parse_spoken_number(value, code)
    return float(result) if result is not None and math.isfinite(float(result)) and float(result) >= 0 else None


@lru_cache(maxsize=7)
def _segment_pattern(code: str) -> re.Pattern:
    units = '|'.join('(?:' + unit + ')' for unit, _ in _UNITS[code])
    return re.compile(r'(?P<amount>.+?)\s*(?P<unit>' + units + r')(?=\s|$|[,;])', re.I)


# TIMER185-FRACTIONAL-DURATION-01: «час с четвертью», «one and a half minutes»,
# «un minuto e mezzo» are one whole duration, never only their first number.
_FRACTION_SUFFIX = {
    'ru': ((r'с\s+половиной', .5), (r'с\s+четвертью', .25)),
    'en': ((r'and\s+a\s+half', .5), (r'and\s+a\s+quarter', .25)),
    'de': ((r'und\s+(?:eine?\s+)?halbe?', .5), (r'und\s+(?:ein\s+)?viertel', .25)),
    'fr': ((r'et\s+demie?', .5), (r'et\s+(?:un\s+)?quart', .25)),
    'es': ((r'y\s+medi[oa]', .5), (r'y\s+(?:un\s+)?cuarto', .25)),
    'it': ((r'e\s+mezz[oa]', .5), (r'e\s+(?:un\s+)?quarto', .25)),
    'pt': ((r'e\s+mei[oa]', .5), (r'e\s+(?:um\s+)?quarto', .25)),
}
_FRACTION_PREFIX = {
    'ru': ((r'полтора|полторы', 1.5),),
    'en': (),
    'de': ((r'anderthalb|eineinhalb', 1.5), (r'eineinviertel', 1.25)),
    'fr': (), 'es': (), 'it': (), 'pt': (),
}
_EN_INNER_FRACTION = r'(?P<amount>.+?)\s+and\s+a\s+(?P<frac>half|quarter)'
_BARE_UNIT_ONE = {'ru': {'час': 3600, 'минуту': 60, 'минута': 60, 'секунду': 1}}


def _unit_scale(unit: str, code: str) -> float | None:
    return next((scale for pattern, scale in _UNITS[code] if re.fullmatch(pattern, unit, re.I)), None)


def _fractional_duration(value: str, code: str) -> float | None:
    units = '|'.join('(?:' + unit + ')' for unit, _ in _UNITS[code])
    for suffix, fraction in _FRACTION_SUFFIX.get(code, ()):
        match = re.fullmatch(r'(?:(?P<amount>.+?)\s+)?(?P<unit>' + units + r')\s+' + suffix, value, re.I)
        if match:
            amount = _number(match['amount'], code) if match['amount'] else 1.0
            scale = _unit_scale(match['unit'], code)
            if amount is not None and scale is not None:
                return (amount + fraction) * scale
    for prefix, amount in _FRACTION_PREFIX.get(code, ()):
        match = re.fullmatch(r'(?:' + prefix + r')\s+(?P<unit>' + units + r')', value, re.I)
        if match and _unit_scale(match['unit'], code) is not None:
            return amount * _unit_scale(match['unit'], code)
    if code == 'en':
        match = re.fullmatch(_EN_INNER_FRACTION + r'\s+(?P<unit>' + units + r')', value, re.I)
        if match:
            amount = _number(match['amount'], code)
            scale = _unit_scale(match['unit'], code)
            if amount is not None and scale is not None:
                return (amount + (.5 if match['frac'].casefold() == 'half' else .25)) * scale
    return None


def parse_duration(text: str, code: str, *, allow_clock: bool = True) -> float | None:
    """Consume the ENTIRE duration; reject missing units, negative/overflow input."""
    if code not in LANGUAGES or len(str(text)) > 250:
        return None
    raw = str(text).strip()
    if re.match(r'^[.,]\d', raw):
        raw = '0' + raw          # NEO 187: «.5 minutes» is half a minute, not five
    value = norm(raw).replace('’', "'").strip(' .!?')
    value = re.sub(r'^(?:' + _PREPOSITIONS[code] + r')\s+', '', value, flags=re.I)
    shortcut = _SHORTCUTS[code].get(value)
    if shortcut is not None:
        return float(shortcut)
    bare = _BARE_UNIT_ONE.get(code, {}).get(value)
    if bare is not None:
        return float(bare)
    fractional = _fractional_duration(value, code)
    if fractional is not None:
        return fractional if 1 <= fractional <= MAX_TIMER_SECONDS else None
    if allow_clock and re.fullmatch(r'\d{1,3}:\d{2}(?::\d{2})?', value):
        parts = [int(part) for part in value.split(':')]
        if any(part >= 60 for part in parts[1:]):
            return None
        result = sum(part * (60 ** i) for i, part in enumerate(reversed(parts)))
        return float(result) if 1 <= result <= MAX_TIMER_SECONDS else None
    total, consumed, scales = 0.0, 0, set()
    for match in _segment_pattern(code).finditer(value):
        gap = value[consumed:match.start()].strip(' ,;')
        if gap and not re.fullmatch(_CONNECTORS[code], gap):
            return None
        amount_text = re.sub(r'^(?:' + _CONNECTORS[code] + r')\s+', '', match['amount'].strip(' ,;'), flags=re.I)
        amount = _number(amount_text, code)
        scale = next((scale for unit, scale in _UNITS[code] if re.fullmatch(unit, match['unit'], re.I)), None)
        if amount is None or scale is None or scale in scales:
            return None
        scales.add(scale)
        total += amount * scale
        consumed = match.end()
    if not consumed or value[consumed:].strip(' .!?') or not 1 <= total <= MAX_TIMER_SECONDS:
        return None
    return total


def timer_payload(text: str, code: str) -> str | None:
    """Return even an empty/invalid payload so dialogue can ask about duration."""
    if code not in LANGUAGES:
        return None
    value = strip_request_shell(text, code).strip(' .!?¿¡')
    match = re.fullmatch(r'(?:' + _TIMER_SHELLS[code] + '|' + _ELAPSED_SHELLS[code] + r')(?:\s+(?P<duration>.*))?', value, re.I)
    if match:
        return str(match.group('duration') or '').strip()
    if code == 'en':
        match = re.fullmatch(r'(?:(?:set|start|add|create)\s+)?(?:a\s+)?(?P<duration>.+?)\s+timer', value, re.I)
        if match:
            # Hyphens between a number and unit are syntactic, not subtraction.
            return re.sub(r'(?<=\d)-(?=[a-z])', ' ', match['duration'])
    return None


def parse_timer_command(text: str, code: str) -> tuple[str, dict] | None:
    payload = timer_payload(text, code)
    seconds = parse_duration(payload, code) if payload is not None else None
    return ('timer', {'action': 'timer', 'seconds': seconds}) if seconds is not None else None


def format_duration(seconds: float, code: str) -> str:
    """Natural duration for replies, using units rather than an internal timer ID."""
    rounded = max(1, int(math.ceil(seconds)))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    units = {
        'ru': (('час', 'часа', 'часов'), ('минута', 'минуты', 'минут'), ('секунда', 'секунды', 'секунд')),
        'en': (('hour', 'hours'), ('minute', 'minutes'), ('second', 'seconds')),
        'de': (('Stunde', 'Stunden'), ('Minute', 'Minuten'), ('Sekunde', 'Sekunden')),
        'fr': (('heure', 'heures'), ('minute', 'minutes'), ('seconde', 'secondes')),
        'es': (('hora', 'horas'), ('minuto', 'minutos'), ('segundo', 'segundos')),
        'it': (('ora', 'ore'), ('minuto', 'minuti'), ('secondo', 'secondi')),
        'pt': (('hora', 'horas'), ('minuto', 'minutos'), ('segundo', 'segundos')),
    }.get(code)
    if units is None:
        units = (('hour', 'hours'), ('minute', 'minutes'), ('second', 'seconds'))
    parts = []
    for amount, row in zip((hours, minutes, secs), units):
        if not amount:
            continue
        if code == 'ru':
            form = 0 if amount % 10 == 1 and amount % 100 != 11 else 1 if 2 <= amount % 10 <= 4 and not 12 <= amount % 100 <= 14 else 2
            # All consumers use "на …"; feminine singular takes the accusative.
            word = {'минута': 'минуту', 'секунда': 'секунду'}.get(row[form], row[form])
        else:
            word = row[0 if amount == 1 else 1]
        parts.append(f'{amount} {word}')
    return ' '.join(parts)
