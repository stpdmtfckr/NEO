"""NEO 188 atlas contrast «seek vs next»: relative seeking of the current video.

«перемотай видео на десять секунд», «seek forward 10 seconds», «avance de dix
secondes» move the playing video by a duration — never the next video and never
a search. A whole phrase with a number (digits or words) and a unit is required;
the result is the internal Russian command the existing media route already owns.
"""
from __future__ import annotations

from functools import lru_cache
import re

_UNIT = {
    'ru': r'(?P<u>секунд\w*|сек|минут\w*|мин)', 'en': r'(?P<u>seconds?|secs?|minutes?|mins?)',
    'de': r'(?P<u>sekunden?|minuten?)', 'fr': r'(?P<u>secondes?|minutes?)', 'es': r'(?P<u>segundos?|minutos?)',
    'it': r'(?P<u>secondi|secondo|minuti|minuto)', 'pt': r'(?P<u>segundos?|minutos?)',
}
_N = r'(?P<n>\d{1,5}|[^\W\d_]+(?:[\s-][^\W\d_]+){0,3})'
_VIDEO = {'ru': r'(?:\s+(?:видео|ролик|видос)(?:\s+(?:на|в)\s+ютубе?)?)?', 'en': r'(?:\s+the\s+video)?', 'de': r'(?:\s+das\s+video)?',
          'fr': r'(?:\s+la\s+vidéo)?', 'es': r'(?:\s+el\s+(?:vídeo|video))?', 'it': r'(?:\s+il\s+video)?', 'pt': r'(?:\s+o\s+(?:vídeo|video))?'}
_FORMS = {
    'ru': (r'перемотай{v}(?:\s+(?P<d>вперёд|вперед|назад))?\s+на\s+{n}\s+{u}', r'(?P<d>вперёд|вперед|назад)\s+на\s+{n}\s+{u}'),
    'en': (r'(?:seek|skip|jump|fast\s+forward|go|move){v}\s+(?P<d>forward|ahead|back|backward|backwards)(?:\s+by)?\s+{n}\s+{u}',
           r'(?P<d>rewind){v}(?:\s+by)?\s+{n}\s+{u}', r'(?P<d>fast\s+forward){v}(?:\s+by)?\s+{n}\s+{u}'),
    'de': (r'spule{v}(?:\s+um)?\s+{n}\s+{u}\s+(?P<d>vor|zurück|zurueck)', r'spule{v}\s+(?P<d>vor|zurück|zurueck)\s+um\s+{n}\s+{u}'),
    'fr': (r'(?P<d>avance|recule){v}\s+de\s+{n}\s+{u}',),
    'es': (r'(?P<d>avanza|adelanta|retrocede|atrasa){v}\s+{n}\s+{u}',),
    'it': (r'(?P<d>avanza|vai\s+avanti|manda\s+avanti|torna\s+indietro|riavvolgi){v}\s+di\s+{n}\s+{u}',),
    'pt': (r'(?P<d>avança|avanca|adianta|recua|retrocede){v}\s+{n}\s+{u}',),
}
_BACK = re.compile(r'назад|back|backward|backwards|rewind|zurück|zurueck|recule|retrocede|atrasa|torna\s+indietro|riavvolgi|recua', re.I)


@lru_cache(maxsize=7)
def _patterns(code: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(r'^' + form.format(v=_VIDEO[code], n=_N, u=_UNIT[code]) + r'$', re.I) for form in _FORMS[code])


def seek_command188(text: str, code: str) -> str | None:
    """«перемотай видео вперёд/назад на N секунд» for a whole relative-seek request, else None."""
    from jarvis.runtime.natural_commands import strip_request_shell
    from jarvis.runtime.spoken_numbers import parse_spoken_number
    if code not in _FORMS:
        return None
    value = re.sub(r'\s+', ' ', strip_request_shell(str(text or ''), code)).strip(' .!?¡¿,').casefold()
    if not value or len(value) > 90:
        return None
    for pattern in _patterns(code):
        match = pattern.fullmatch(value)
        if not match:
            continue
        raw = match.group('n')
        number = float(raw) if raw.isdigit() else parse_spoken_number(raw, code)
        if number is None or not number > 0:
            return None
        seconds = int(round(number * (60 if re.match(r'(?:мин|min)', match.group('u'), re.I) else 1)))
        if not 1 <= seconds <= 86400:
            return None
        direction = 'назад' if match.group('d') and _BACK.fullmatch(match.group('d')) else 'вперёд'
        return f'перемотай видео {direction} на {seconds} секунд'
    return None
