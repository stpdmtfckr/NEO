"""NEO 188 NEO-UX-001 / TZ-PARAMETER-CLARIFICATION-CORRECTION-01.

Clarify or correct ONE parameter of the same operation without repeating the
whole command:

* «Таймер на двадцать» → «Двадцать секунд, минут или часов?» → «Минут» → one
  timer for 20 minutes (the number is kept, only the unit is asked).
* «не двадцать, двенадцать» → the SAME timer (same id) is rescheduled to 12
  minutes from now; no second timer. The old value «двадцать» must match the
  timer's real duration, otherwise nothing is changed and NEO says why.
* Two timers that both match → NEO asks which one; it never picks the last.
* «не 50, 30» after a volume change sets the same target to 30 (absolute).

This module only parses and proposes; it never executes. Cancelled, expired,
finished or replaced operations are never revived: a reschedule of a timer
that no longer exists is refused by the timer tool itself, and a pending
question disappears on cancel through the ordinary dialogue rules.
"""
from __future__ import annotations

import re

from jarvis.runtime.natural_commands import LANGUAGES, norm
from jarvis.runtime.spoken_numbers import parse_spoken_number

SCALES = (1, 60, 3600)
MAX_SECONDS = 7 * 24 * 3600

_UNITS = {
    'ru': ((r'секунд(?:а|у|ы|ах)?|сек|с', 1), (r'минут(?:а|у|ы|ах)?|мин', 60), (r'час(?:а|ов|ах)?|ч', 3600)),
    'en': ((r'seconds?|secs?|s', 1), (r'minutes?|mins?|m', 60), (r'hours?|hrs?|h', 3600)),
    'de': ((r'sekunden?|sek|s', 1), (r'minuten?|min', 60), (r'stunden?|std|h', 3600)),
    'fr': ((r'secondes?|sec|s', 1), (r'minutes?|min', 60), (r'heures?|h', 3600)),
    'es': ((r'segundos?|seg|s', 1), (r'minutos?|min', 60), (r'horas?|h', 3600)),
    'it': ((r'second[oi]|sec|s', 1), (r'minut[oi]|min', 60), (r'or[ae]|h', 3600)),
    'pt': ((r'segundos?|seg|s', 1), (r'minutos?|min', 60), (r'horas?|h', 3600)),
}
# Full unit words for a bounded typo check («минуут», «minuts»): distance ≤ 1.
_UNIT_WORDS = {
    'ru': (('секунд', 1), ('секунды', 1), ('минут', 60), ('минуты', 60), ('часов', 3600), ('часа', 3600)),
    'en': (('seconds', 1), ('minutes', 60), ('hours', 3600)),
    'de': (('sekunden', 1), ('minuten', 60), ('stunden', 3600)),
    'fr': (('secondes', 1), ('minutes', 60), ('heures', 3600)),
    'es': (('segundos', 1), ('minutos', 60), ('horas', 3600)),
    'it': (('secondi', 1), ('minuti', 60)),
    'pt': (('segundos', 1), ('minutos', 60), ('horas', 3600)),
}
_IN = {'ru': r'(?:в|на)', 'en': r'(?:in|for)', 'de': r'(?:in|für)', 'fr': r'(?:en|pour)', 'es': r'(?:en|de|por)',
       'it': r'(?:in|di|per)', 'pt': r'(?:em|de|por)'}
_POLITE = r'(?:пожалуйста|please|bitte|s\'il te plaît|s\'il te plait|por favor|per favore)'
_YES_LEAD = r'(?:да|yes|ja|oui|sí|si|sì|sim|ок|ok|okay)'
_NOT = {
    'ru': r'(?:нет[,\s]+)?не\s+(?P<x>[^,]+?)\s*(?:,\s*(?:а\s+)?|\s+а\s+)(?P<y>.+)',
    'en': r'(?:no[,\s]+)?not\s+(?P<x>[^,]+?)\s*(?:,\s*(?:but\s+)?|\s+but\s+)(?P<y>.+)',
    'de': r'(?:nein[,\s]+)?nicht\s+(?P<x>[^,]+?)\s*(?:,\s*(?:sondern\s+)?|\s+sondern\s+)(?P<y>.+)',
    'fr': r'(?:non[,\s]+)?pas\s+(?P<x>[^,]+?)\s*(?:,\s*(?:mais\s+)?|\s+mais\s+)(?P<y>.+)',
    'es': r'(?:no[,\s]+)?no\s+(?P<x>[^,]+?)\s*(?:,\s*(?:sino\s+)?|\s+sino\s+)(?P<y>.+)',
    'it': r'(?:no[,\s]+)?non\s+(?P<x>[^,]+?)\s*(?:,\s*(?:ma\s+)?|\s+ma\s+)(?P<y>.+)',
    'pt': r'(?:não[,\s]+)?não\s+(?P<x>[^,]+?)\s*(?:,\s*(?:mas\s+)?|\s+mas\s+)(?P<y>.+)',
}

_TEXT = {
    'unit_question': ('{n} секунд, минут или часов?', '{n} seconds, minutes or hours?', '{n} Sekunden, Minuten oder Stunden?',
                      '{n} secondes, minutes ou heures ?', '¿{n} segundos, minutos u horas?', '{n} secondi, minuti o ore?',
                      '{n} segundos, minutos ou horas?'),
    'unit_question_one': ('{n} секунду, минуту или час?', '{n} second, minute or hour?', '{n} Sekunde, Minute oder Stunde?',
                          '{n} seconde, minute ou heure ?', '¿{n} segundo, minuto u hora?', '{n} secondo, minuto o ora?',
                          '{n} segundo, minuto ou hora?'),
    'pending_mismatch': ('В команде сейчас {current}, а не {old}. {question}', 'The request says {current}, not {old}. {question}',
                         'In der Anfrage steht {current}, nicht {old}. {question}', 'La demande indique {current}, pas {old}. {question}',
                         'La petición dice {current}, no {old}. {question}', 'La richiesta dice {current}, non {old}. {question}',
                         'O pedido diz {current}, não {old}. {question}'),
    'unit_invalid': ('Не понял единицу времени. {question}', 'I didn’t catch the unit. {question}',
                     'Die Zeiteinheit habe ich nicht verstanden. {question}', 'Je n’ai pas compris l’unité. {question}',
                     'No entendí la unidad. {question}', 'Non ho capito l’unità. {question}', 'Não percebi a unidade. {question}'),
    'timer_mismatch': ('Сейчас таймер стоит на {current}, а не на {old}. Ничего не менял — скажите новое время целиком, например «12 минут».',
                       'The timer is set to {current}, not {old}. I changed nothing — say the new time in full, for example “12 minutes”.',
                       'Der Timer steht auf {current}, nicht auf {old}. Ich habe nichts geändert – nenne die neue Zeit vollständig, z. B. „12 Minuten“.',
                       'Le minuteur est réglé sur {current}, pas sur {old}. Je n’ai rien changé — dites la nouvelle durée en entier, par exemple « 12 minutes ».',
                       'El temporizador está en {current}, no en {old}. No he cambiado nada: di el nuevo tiempo completo, por ejemplo «12 minutos».',
                       'Il timer è impostato su {current}, non su {old}. Non ho cambiato nulla: di’ il nuovo tempo per intero, per esempio «12 minuti».',
                       'O temporizador está em {current}, não em {old}. Não mudei nada: diz o novo tempo completo, por exemplo «12 minutos».'),
    'timer_none': ('Не нашёл активный таймер на {old}. Ничего не менял и новый таймер не создавал.',
                   'I found no running timer for {old}. I changed nothing and created no new timer.',
                   'Ich habe keinen laufenden Timer mit {old} gefunden. Nichts geändert, kein neuer Timer.',
                   'Aucun minuteur en cours de {old}. Je n’ai rien changé ni créé de nouveau minuteur.',
                   'No encontré ningún temporizador activo de {old}. No he cambiado nada ni creado uno nuevo.',
                   'Non ho trovato un timer attivo di {old}. Non ho cambiato nulla né creato un nuovo timer.',
                   'Não encontrei nenhum temporizador ativo de {old}. Não mudei nada nem criei um novo.'),
    'which_timer': ('Подходят несколько таймеров. Какой изменить?\n{choices}', 'Several timers match. Which one should I change?\n{choices}',
                    'Mehrere Timer passen. Welchen soll ich ändern?\n{choices}', 'Plusieurs minuteurs correspondent. Lequel dois-je modifier ?\n{choices}',
                    'Coinciden varios temporizadores. ¿Cuál cambio?\n{choices}', 'Più timer corrispondono. Quale devo modificare?\n{choices}',
                    'Vários temporizadores correspondem. Qual devo alterar?\n{choices}'),
    'volume_mismatch': ('Сейчас громкость {current}%, а не {old}%. Ничего не менял.', 'The volume is {current}%, not {old}%. I changed nothing.',
                        'Die Lautstärke ist {current} %, nicht {old} %. Ich habe nichts geändert.',
                        'Le volume est à {current} %, pas à {old} %. Je n’ai rien changé.',
                        'El volumen está en {current} %, no en {old} %. No he cambiado nada.',
                        'Il volume è al {current}%, non al {old}%. Non ho cambiato nulla.',
                        'O volume está em {current}%, não em {old}%. Não mudei nada.'),
    'volume_range': ('Громкость — число от 0 до 100. Ничего не менял.', 'Volume is a number from 0 to 100. I changed nothing.',
                     'Die Lautstärke ist eine Zahl von 0 bis 100. Nichts geändert.', 'Le volume est un nombre de 0 à 100. Rien n’a changé.',
                     'El volumen es un número de 0 a 100. No he cambiado nada.', 'Il volume è un numero da 0 a 100. Non ho cambiato nulla.',
                     'O volume é um número de 0 a 100. Não mudei nada.'),
}


def text(key: str, code: str, **values) -> str:
    row = _TEXT[key]
    index = LANGUAGES.index(code) if code in LANGUAGES else 1
    return (row[index] if index < len(row) else row[0]).format(**values)


def _lev1(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _clean(value: str, code: str) -> str:
    value = norm(value)
    value = re.sub(r'^(?:' + _YES_LEAD + r')[,\s]+', '', value)
    value = re.sub(r'^' + _POLITE + r'[,\s]+|[,\s]+' + _POLITE + r'$', '', value).strip(' ,.!?')
    return value


def amount_words(value: str, code: str) -> str:
    """The spoken number itself («двадцать» from «на двадцать»), for the question."""
    raw = _clean(value, code)
    return re.sub(r'^(?:' + _IN[code] + r')\s+', '', raw) if code in LANGUAGES else raw


def unit_scale(answer: str, code: str) -> int | None:
    """60 for «минут», «в минутах», «minutes», «Minuten»…; None otherwise."""
    if code not in LANGUAGES or any(c.isdigit() for c in str(answer or '')):
        return None
    value = _clean(answer, code)
    value = re.sub(r'^(?:' + _IN[code] + r')\s+', '', value)
    for pattern, scale in _UNITS[code]:
        if re.fullmatch(pattern, value, re.I):
            return scale
    if len(value) >= 5:
        for word, scale in _UNIT_WORDS.get(code, ()):
            if _lev1(value, word):
                return scale
    return None


def bare_amount(value: str, code: str) -> float | None:
    """A number without unit («двадцать», «20», «на 20»), else None."""
    if code not in LANGUAGES:
        return None
    raw = _clean(value, code)
    raw = re.sub(r'^(?:' + _IN[code] + r')\s+', '', raw)
    if not raw or len(raw) > 60:
        return None
    if re.fullmatch(r'\d{1,6}(?:[.,]\d{1,2})?', raw):
        number = float(raw.replace(',', '.'))
    else:
        number = parse_spoken_number(raw, code)
    if number is None or not 0 <= float(number) <= MAX_SECONDS:
        return None
    return float(number)


def quantity(value: str, code: str) -> dict | None:
    """{'amount', 'scale', 'seconds'} for «двадцать», «20 минут», «1 час 20 минут»."""
    amount = bare_amount(value, code)
    if amount is not None:
        return {'amount': amount, 'scale': None, 'seconds': None, 'raw': amount_words(value, code)}
    raw = _clean(value, code)
    units = '|'.join('(?:' + unit + ')' for unit, _ in _UNITS[code])
    single = re.fullmatch(r'(?P<a>.+?)\s*(?P<u>' + units + r')', raw, re.I)
    if single:
        number = bare_amount(single.group('a'), code)
        scale = next((s for pattern, s in _UNITS[code] if re.fullmatch(pattern, single.group('u'), re.I)), None)
        if number is not None and scale is not None:
            return {'amount': number, 'scale': scale, 'seconds': number * scale, 'raw': raw}
    from jarvis.runtime.duration import parse_duration
    seconds = parse_duration(raw, code)
    if seconds is not None:
        return {'amount': None, 'scale': None, 'seconds': float(seconds), 'raw': raw}
    return None


def not_x_y(value: str, code: str) -> tuple[dict, dict, str] | None:
    """«не двадцать, двенадцать» → (old quantity, new quantity, old text)."""
    raw = str(value or '').strip()
    if code not in LANGUAGES or not raw or len(raw) > 120 or any(c in raw for c in '"«»“”\n'):
        return None
    match = re.fullmatch(_NOT[code], norm(raw).strip(' .!?'), re.I)
    if not match:
        return None
    old, new = quantity(match.group('x'), code), quantity(match.group('y'), code)
    if old is None or new is None:
        return None
    return old, new, match.group('x').strip()


def amount_label(amount: float, spoken: str, code: str) -> str:
    """The user's own word («Двадцать») when given, digits otherwise."""
    words = str(spoken or '').strip()
    if words and not any(c.isdigit() for c in words):
        return words[:1].upper() + words[1:]
    return str(int(amount)) if float(amount).is_integer() else str(amount).replace('.', ',' if code != 'en' else '.')


def unit_question(amount: float, spoken: str, code: str) -> str:
    label = amount_label(amount, spoken, code)
    whole = float(amount).is_integer()
    n = int(amount) if whole else None
    if code == 'ru':
        if n is not None and n % 10 == 1 and n % 100 != 11:
            return text('unit_question_one', code, n=label)
        if n is None or (n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14)):
            return '{n} секунды, минуты или часа?'.format(n=label)  # 2–4 and fractions
        return text('unit_question', code, n=label)
    if n == 1:
        return text('unit_question_one', code, n=label)
    return text('unit_question', code, n=label)


def seconds_in_range(seconds: float | None) -> bool:
    return seconds is not None and 1 <= float(seconds) <= MAX_SECONDS


def old_matches(old: dict, current_seconds: float) -> list[int]:
    """Scales under which the stated old value equals the timer's real duration."""
    if old.get('seconds') is not None:
        if abs(float(old['seconds']) - current_seconds) <= 1:
            return [int(old['scale'] or 1)]
        return []
    return [s for s in SCALES if abs(float(old['amount']) * s - current_seconds) <= 1]


def new_seconds(new: dict, scale: int | None) -> float | None:
    if new.get('seconds') is not None:
        return float(new['seconds'])
    if scale is None:
        return None
    return float(new['amount']) * scale
