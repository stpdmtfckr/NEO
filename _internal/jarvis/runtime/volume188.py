"""NEO 188 HREQ-014 / PARAM-04: the system volume in seven languages.

«громкость 40», «поставь громкость на 40», «сделай звук 40», «убавь громкость
на 10», «set the volume to 40», «Lautstärke auf 40»… Only the system (master)
volume with an explicit level or a bare up/down request is parsed here.
Anything naming another owner (music, Discord, a game, an application, the
microphone) does not match and keeps its existing route. Proposals only: the
audio tool validates and executes them.
"""
from __future__ import annotations

import re
from functools import lru_cache

from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell
from jarvis.runtime.spoken_numbers import parse_spoken_number

DEFAULT_STEP = 10

_SET = {
    'ru': r'(?:поставь(?:те)?|установи(?:те)?|сделай(?:те)?|выставь(?:те)?|выкрути|измени|включи)',
    'en': r'(?:set|put|change|make|turn)',
    'de': r'(?:stell(?:e)?|setz(?:e)?|mach(?:e)?|änder(?:e)?|aender(?:e)?|dreh(?:e)?)',
    'fr': r'(?:mets|met|règle|régle|regle|change|passe)',
    'es': r'(?:pon|ajusta|cambia|deja|establece)',
    'it': r'(?:metti|imposta|regola|porta|cambia)',
    'pt': r'(?:põe|poe|ponha|coloca|define|ajusta|muda|deixa)',
}
_OBJ = {
    'ru': r'(?:(?:общую|системную)\s+)?(?:громкость|звук)(?:\s+(?:системы|компьютера|пк|windows|виндовс))?',
    'en': r'(?:the\s+)?(?:(?:system|overall|master)\s+)?(?:volume|sound)',
    'de': r'(?:die\s+)?(?:system)?(?:lautstärke|lautstaerke)',
    'fr': r'(?:le\s+)?(?:volume|son)(?:\s+(?:général|general|du\s+système|du\s+systeme))?',
    'es': r'(?:el\s+)?(?:volumen|sonido)(?:\s+(?:general|del\s+sistema))?',
    'it': r'(?:il\s+)?(?:volume|audio)(?:\s+(?:generale|di\s+sistema))?',
    'pt': r'(?:o\s+)?(?:volume|som)(?:\s+(?:geral|do\s+sistema))?',
}
_PREP = {'ru': r'(?:на|в|до)', 'en': r'(?:to|at)', 'de': r'(?:auf)', 'fr': r'(?:à|a|sur)',
         'es': r'(?:a|al|en)', 'it': r'(?:a|al)', 'pt': r'(?:para|a|em|no|ao)'}
_BY = {'ru': r'(?:на)', 'en': r'(?:by)', 'de': r'(?:um)', 'fr': r'(?:de)', 'es': r'(?:en|un)',
       'it': r'(?:di)', 'pt': r'(?:em|de)'}
_PCT = (r'(?:\s*%|\s*процент\w*|\s+percent|\s+per\s*cent|\s+prozent|\s+pour\s*cent|\s+por\s*ciento'
        r'|\s+per\s*cento|\s+por\s*cento)?')
_N = r'(?P<n>\d{1,3}|[^\W\d_][\w\'’-]*(?:\s+[^\W\d_][\w\'’-]*){0,2})'
_UP = {
    'ru': r'(?:прибавь|увеличь|увеличи|повысь|подними|добавь)',
    'en': r'(?:raise|increase|boost)',
    'de': r'(?:erhöhe|erhoehe)',
    'fr': r'(?:monte|augmente)',
    'es': r'(?:sube|aumenta)',
    'it': r'(?:alza|aumenta)',
    'pt': r'(?:aumenta|sobe)',
}
_DOWN = {
    'ru': r'(?:убавь|уменьши|понизь|снизь|опусти|приглуши)',
    'en': r'(?:lower|decrease|reduce)',
    'de': r'(?:verringere|senke|reduziere)',
    'fr': r'(?:baisse|diminue|réduis|reduis)',
    'es': r'(?:baja|disminuye|reduce)',
    'it': r'(?:abbassa|diminuisci|riduci)',
    'pt': r'(?:baixa|diminui|reduz)',
}
# «сделай громче на 20», «louder by 20», «lauter um 20»…
_LOUDER = {
    'ru': (r'(?:сделай(?:те)?\s+)?(?:по)?громче', r'(?:сделай(?:те)?\s+)?(?:по)?тише'),
    'en': (r'(?:make\s+it\s+)?louder', r'(?:make\s+it\s+)?quieter'),
    'de': (r'(?:mach(?:e)?\s+)?(?:es\s+)?lauter', r'(?:mach(?:e)?\s+)?(?:es\s+)?leiser'),
    'fr': (r'plus\s+fort', r'moins\s+fort'),
    'es': (r'más\s+alto|mas\s+alto', r'más\s+bajo|mas\s+bajo'),
    'it': (r'più\s+forte|piu\s+forte', r'più\s+piano|piu\s+piano'),
    'pt': (r'mais\s+alto', r'mais\s+baixo'),
}
_EN_PHRASAL = r'^(?:turn|crank)\s+(?:(?P<a>up|down)\s+(?:the\s+)?(?:volume|sound)|(?:the\s+)?(?:volume|sound)\s+(?P<b>up|down))(?:\s+by\s+' + _N + _PCT + r')?$'
_DE_PHRASAL = r'^(?:dreh(?:e)?\s+)?(?:die\s+)?(?:lautstärke|lautstaerke)\s+(?P<a>hoch|runter|rauf|auf|ab|erhöhen|erhoehen|verringern|senken|reduzieren)(?:\s+um\s+' + _N + _PCT + r')?$'
_RU_VOLUME_WORD = r'(?:громкость|звук)'


def _number(raw: str | None, code: str) -> int | None:
    if raw is None:
        return None
    value = str(raw).strip()
    if value.isdigit():
        number = int(value)
    else:
        spoken = parse_spoken_number(value, code)
        if spoken is None or spoken != int(spoken):
            return None
        number = int(spoken)
    return number if 0 <= number <= 100 else None


@lru_cache(maxsize=7)
def _patterns(code: str):
    obj, prep, verb, by = _OBJ[code], _PREP[code], _SET[code], _BY[code]
    level = r'(?:\s+' + prep + r')?\s+' + _N + _PCT
    return (
        re.compile(r'^(?:' + verb + r'\s+)?' + obj + level + r'$', re.I),                  # (поставь) громкость (на) 40
        re.compile(r'^' + obj + level + r'\s+' + verb + r'$', re.I),                       # громкость 50 поставь
    )


@lru_cache(maxsize=7)
def _relative(code: str):
    obj, by = _OBJ[code], _BY[code]
    amount = r'(?:\s+' + by + r'\s+' + _N + _PCT + r')?'
    up, down = _LOUDER[code]
    return (
        (re.compile(r'^' + _UP[code] + r'\s+' + obj + amount + r'$', re.I), 1),
        (re.compile(r'^' + _DOWN[code] + r'\s+' + obj + amount + r'$', re.I), -1),
        (re.compile(r'^(?:' + up + r')\s+' + by + r'\s+' + _N + _PCT + r'$', re.I), 1),
        (re.compile(r'^(?:' + down + r')\s+' + by + r'\s+' + _N + _PCT + r'$', re.I), -1),
    )


def _clean(text: str, code: str) -> str:
    value = norm(strip_request_shell(str(text or ''), code)).strip(' .!?¿¡')
    return re.sub(r'\s+', ' ', value.replace('ё', 'е') if code == 'ru' else value)


def parse(text: str, code: str) -> tuple[str, dict] | None:
    """('audio', {...}) for a complete system-volume command, else None."""
    if code not in LANGUAGES or len(str(text or '')) > 120 or any(c in str(text or '') for c in '"«»“”'):
        return None
    value = _clean(text, code)
    if not value:
        return None
    for pattern in _patterns(code):
        match = pattern.fullmatch(value)
        if match:
            number = _number(match.group('n'), code)
            return ('audio', {'action': 'set_volume', 'value': number}) if number is not None else None
    for pattern, sign in _relative(code):
        match = pattern.fullmatch(value)
        if match:
            raw = match.groupdict().get('n')
            number = DEFAULT_STEP if raw is None else _number(raw, code)
            if not number:
                return None
            return 'audio', {'action': 'change_volume', 'value': sign * number}
    phrasal = _EN_PHRASAL if code == 'en' else _DE_PHRASAL if code == 'de' else None
    if phrasal is not None:
        match = re.fullmatch(phrasal, value, re.I)
        if match:
            word = (match.groupdict().get('a') or match.groupdict().get('b') or '').casefold()
            sign = 1 if word in {'up', 'hoch', 'rauf', 'auf', 'erhöhen', 'erhoehen'} else -1
            raw = match.groupdict().get('n')
            number = DEFAULT_STEP if raw is None else _number(raw, code)
            if not number:
                return None
            return 'audio', {'action': 'change_volume', 'value': sign * number}
    return None


def partial(text: str, code: str) -> tuple[str, dict, tuple] | None:
    """«поставь громкость» without a level: ask for it (0–100), never guess."""
    if code not in LANGUAGES or any(c in str(text or '') for c in '"«»“”'):
        return None
    value = _clean(text, code)
    if re.fullmatch(r'(?:' + _SET[code] + r')\s+' + _OBJ[code], value, re.I):
        return 'audio', {'action': 'set_volume'}, (('value', 'percent156', 'percent156'),)
    return None
