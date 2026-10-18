"""NEO 172: everyday help — one entry point the agent asks before the usual routing.

Handles, in 7 languages:
* «что за ошибка?», «как это исправить?», «как пройти этот уровень?» — the window under NEO
  is read locally, NEO asks before sending the search text (screen_help172);
* «объясни / перескажи / переведи / прочитай выделенное (страницу, документ, экран)» (explain172);
* «покажи напоминания», «удали напоминание 2», «каждый день в 9 открывай почту»,
  «по будням в 8:30 напоминай выпить таблетки» (timers);
* «разбери загрузки», «найди большие файлы», «верни как было», «открой папку номер 2» (tidy172);
* «запомни, что …», and after a statement about the user an offer «Запомнить: …?»;
* «не то» — the next command becomes the correction of the previous one;
* «как ты меня понимаешь», «удали собранные фразы» (learning172), «проверь себя».

Everything else returns None and goes through the usual routes.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import logging
import os
import re
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)
_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def _code(code: str) -> str:
    code = str(code or 'ru').casefold().split('-')[0]
    return code if code in _LANGS else 'en'


def _pick(row: tuple[str, ...], code: str) -> str:
    return row[_LANGS.index(_code(code))]


def _n(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '').casefold().replace('ё', 'е')).strip(' .,!?:;¿¡«»"\'')


@dataclass(slots=True)
class Reply:
    text: str
    detail: str
    streamed: bool = False
    outcome: str = ''
    run: str = ''                     # a NEO command to run as the answer (e.g. the corrected command)


_YES = re.compile(r'^(?:да|ага|угу|давай|конечно|хорошо|ок(?:ей)?|запомни|yes|yeah|yep|sure|ok(?:ay)?|please|ja|gerne|oui|s[iíì]|claro|vale|certo|sim)(?:\s+\w+)?$', re.I)
_NO = re.compile(r'^(?:нет|неа|не\s+надо|не\s+нужно|не\s+запоминай|отмена|no|nope|no\s+thanks|nein|non|n[aã]o)(?:\s+\w+)?$', re.I)
# NEO 187 HREQ-014: «да да», «да, верно», «это то, что я имел в виду» agree;
# «да нет», «не знаю» and negations never become consent.
from jarvis.runtime.confirm_words187 import guard_no as _guard_no187, guard_yes as _guard_yes187
_YES, _NO = _guard_yes187(_YES), _guard_no187(_NO)

# ---- reminders and schedules -------------------------------------------------------------------

_LIST_REMINDERS = re.compile(
    r'^(?:покажи|какие|список|мои|перечисли|что\s+у\s+меня)\s+(?:у\s+меня\s+|мои\s+|все\s+)?(?:напоминани\w*|таймер\w*|расписани\w*|будильник\w*|по\s+расписанию)(?:\s+есть)?$|'
    r'^(?:show|list)\s+(?:my\s+|all\s+)?(?:reminders|timers|schedules?)$|^what\s+reminders\s+do\s+i\s+have$|'
    r'^(?:zeig|liste)\s+(?:meine\s+)?(?:erinnerungen|timer|zeitpl[aä]ne)$|^(?:montre|affiche)\s+(?:mes\s+)?(?:rappels|minuteurs|programmes)$|'
    r'^(?:muestra|mu[eé]strame)\s+(?:mis\s+)?(?:recordatorios|temporizadores)$|^(?:mostra|mostrami)\s+(?:i\s+miei\s+)?(?:promemoria|timer)$|'
    r'^(?:mostra|mostra-me)\s+(?:os\s+meus\s+)?(?:lembretes|temporizadores)$', re.I)
_CANCEL_REMINDER = re.compile(
    r'^(?:удали|отмени|убери|сотри)\s+(?:напоминани\w*|таймер\w*|расписани\w*|будильник\w*)\s+(?:номер\s+)?(\d{1,2})$|'
    r'^(?:delete|cancel|remove)\s+(?:reminder|timer|schedule)\s+(?:number\s+)?(\d{1,2})$|'
    r'^(?:l[oö]sche|entferne)\s+(?:erinnerung|timer|zeitplan)\s+(?:nummer\s+)?(\d{1,2})$|^(?:supprime|annule)\s+(?:le\s+)?(?:rappel|minuteur)\s+(?:num[eé]ro\s+)?(\d{1,2})$|'
    r'^(?:borra|elimina|cancela)\s+(?:el\s+)?(?:recordatorio|temporizador)\s+(?:n[uú]mero\s+)?(\d{1,2})$|^(?:elimina|cancella)\s+(?:il\s+)?(?:promemoria|timer)\s+(?:numero\s+)?(\d{1,2})$|'
    r'^(?:apaga|cancela)\s+(?:o\s+)?(?:lembrete|temporizador)\s+(?:n[uú]mero\s+)?(\d{1,2})$', re.I)

_WHEN = {
    'daily': r'(?:каждый\s+день|ежедневно|каждое\s+утро|каждый\s+вечер|every\s+day|daily|every\s+morning|every\s+evening|jeden\s+tag|t[aä]glich|jeden\s+morgen|'
             r'tous\s+les\s+jours|chaque\s+jour|chaque\s+matin|todos\s+los\s+d[ií]as|cada\s+d[ií]a|cada\s+ma[nñ]ana|ogni\s+giorno|ogni\s+mattina|tutti\s+i\s+giorni|'
             r'todos\s+os\s+dias|cada\s+dia|todas\s+as\s+manh[aã]s)',
    'weekdays': r'(?:по\s+будням|в\s+будни|каждый\s+будний\s+день|on\s+weekdays|every\s+weekday|werktags|an\s+wochentagen|en\s+semaine|les\s+jours\s+de\s+semaine|'
                r'entre\s+semana|los\s+d[ií]as\s+laborables|nei\s+giorni\s+feriali|dal\s+luned[iì]\s+al\s+venerd[iì]|nos\s+dias\s+[uú]teis|durante\s+a\s+semana)',
    'weekends': r'(?:по\s+выходным|в\s+выходные|on\s+weekends|every\s+weekend|am\s+wochenende|le\s+week-end|los\s+fines\s+de\s+semana|nel\s+fine\s+settimana|aos\s+fins\s+de\s+semana)',
}
_WEEKDAYS = (
    ('mon', r'(?:понедельник\w*|monday|montag|lundi|lunes|luned[iì]|segunda)'), ('tue', r'(?:вторник\w*|tuesday|dienstag|mardi|martes|marted[iì]|ter[cç]a)'),
    ('wed', r'(?:сред[уаы]|средам|wednesday|mittwoch|mercredi|mi[eé]rcoles|mercoled[iì]|quarta)'), ('thu', r'(?:четверг\w*|thursday|donnerstag|jeudi|jueves|gioved[iì]|quinta)'),
    ('fri', r'(?:пятниц\w*|friday|freitag|vendredi|viernes|venerd[iì]|sexta)'), ('sat', r'(?:суббот\w*|saturday|samstag|samedi|s[aá]bado)'),
    ('sun', r'(?:воскресень\w*|sunday|sonntag|dimanche|domingo|domenica)'),
)
_EVERY_DAY_OF = r'(?:кажд\w+|по|every|on|jeden|jeweils|chaque|tous\s+les|cada|todos\s+los|ogni|tutti\s+i|todas?\s+as?|todos\s+os)\s+'
_AT = r'(?:в|во|at|um|à|a\s+las?|alle|às|as)\s+(\d{1,2})(?:[:.](\d{2}))?\s*(утра|вечера|дня|ночи|am|pm|uhr|h|horas?)?'
_REMIND_VERBS = r'(?:напоминай|напомни|напоминать|remind\s+me(?:\s+to)?|erinnere\s+mich(?:\s+an)?|rappelle-moi(?:\s+de)?|recu[eé]rdame|ricordami(?:\s+di)?|lembra-me(?:\s+de)?)'
# habitual → the command form NEO knows («открывай почту» → «открой почту»)
_IMPERATIVE = {
    'открывай': 'открой', 'включай': 'включи', 'запускай': 'запусти', 'показывай': 'покажи', 'ставь': 'поставь', 'проверяй': 'проверь',
    'выключай': 'выключи', 'закрывай': 'закрой', 'делай': 'сделай', 'присылай': 'пришли', 'говори': 'скажи', 'читай': 'прочитай',
}


@dataclass(slots=True)
class Schedule:
    hour: int
    minute: int
    repeat: str
    reminder: str = ''
    command: str = ''


def parse_schedule(text: str) -> Schedule | None:
    raw = ' '.join(str(text or '').split()).strip(' .!?')
    low = raw.casefold().replace('ё', 'е')
    repeat, when_end = '', -1
    for key, pattern in _WHEN.items():
        match = re.match(pattern, low)
        if match:
            repeat, when_end = key, match.end()
            break
    if not repeat:
        days = []
        match = re.match(_EVERY_DAY_OF, low)
        position = match.end() if match else -1
        while position >= 0:
            for key, pattern in _WEEKDAYS:
                day = re.match(pattern, low[position:])
                if day:
                    days.append(key)
                    position += day.end()
                    joiner = re.match(r'\s*(?:,|и|and|und|et|y|e)\s*', low[position:])
                    position = position + joiner.end() if joiner else -1
                    break
            else:
                position = -1
        if days:
            repeat = ','.join(dict.fromkeys(days))
            ends = [m.end() for key, pattern in _WEEKDAYS for m in [re.search(pattern, low)] if m and key in days]
            when_end = max(ends)
    if not repeat:
        return None
    rest = low[when_end:].strip(' ,')
    at = re.match(_AT, rest)
    if not at:
        return None
    hour, minute = int(at.group(1)), int(at.group(2) or 0)
    part = (at.group(3) or '').casefold()
    if part in {'вечера', 'дня', 'pm'} and hour < 12:
        hour += 12
    if part in {'ночи', 'am'} and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    action_raw = raw[len(raw) - len(rest) + at.end():].strip(' ,') if len(rest) else ''
    action = action_raw.casefold()
    remind = re.match(_REMIND_VERBS + r'\s*', action)
    if remind:
        label = action_raw[remind.end():].strip(' ,')
        label = re.sub(r'^(?:мне\s+|что\s+|о\s+|об\s+|про\s+)+', '', label, flags=re.I)
        return Schedule(hour, minute, repeat, reminder=label or action_raw) if label or action_raw else None
    if not action:
        return None
    first, _, tail = action_raw.partition(' ')
    command = (_IMPERATIVE.get(first.casefold(), first) + (' ' + tail if tail else '')).strip()
    return Schedule(hour, minute, repeat, command=command)


_REPEAT_TEXT = {
    'daily': ('каждый день', 'every day', 'jeden Tag', 'tous les jours', 'todos los días', 'ogni giorno', 'todos os dias'),
    'weekdays': ('по будням', 'on weekdays', 'werktags', 'en semaine', 'entre semana', 'nei giorni feriali', 'nos dias úteis'),
    'weekends': ('по выходным', 'on weekends', 'am Wochenende', 'le week-end', 'los fines de semana', 'nel fine settimana', 'aos fins de semana'),
}
_DAY_TEXT = {
    'mon': ('пн', 'Mon', 'Mo', 'lun', 'lun', 'lun', 'seg'), 'tue': ('вт', 'Tue', 'Di', 'mar', 'mar', 'mar', 'ter'),
    'wed': ('ср', 'Wed', 'Mi', 'mer', 'mié', 'mer', 'qua'), 'thu': ('чт', 'Thu', 'Do', 'jeu', 'jue', 'gio', 'qui'),
    'fri': ('пт', 'Fri', 'Fr', 'ven', 'vie', 'ven', 'sex'), 'sat': ('сб', 'Sat', 'Sa', 'sam', 'sáb', 'sab', 'sáb'),
    'sun': ('вс', 'Sun', 'So', 'dim', 'dom', 'dom', 'dom'),
}


def repeat_text(repeat: str, code: str) -> str:
    if repeat in _REPEAT_TEXT:
        return _pick(_REPEAT_TEXT[repeat], code)
    return ', '.join(_pick(_DAY_TEXT[d], code) for d in repeat.split(',') if d in _DAY_TEXT)


# ---- files -------------------------------------------------------------------------------------

_TIDY = re.compile(
    r'^(?:разбери|разложи|рассортируй|отсортируй|упорядочи|наведи\s+порядок\s+в|приберись\s+в|почисти)\s+(?:папку\s+)?(?:мои\s+)?(загрузк\w*|скачанн\w*|рабоч\w+\s+стол\w*|документ\w*)$|'
    r'^(?:sort|tidy(?:\s+up)?|organi[sz]e|clean\s+up)\s+(?:my\s+)?(downloads|desktop|documents)(?:\s+folder)?$|'
    r'^(?:sortiere|ordne|r[aä]ume)\s+(?:meine\s+|die\s+|den\s+|meinen\s+)?(downloads|schreibtisch|dokumente)(?:\s+auf)?$|'
    r'^(?:range|trie|organise)\s+(?:mes\s+|le\s+dossier\s+)?(t[eé]l[eé]chargements|bureau|documents)$|'
    r'^(?:ordena|organiza)\s+(?:mis\s+|la\s+carpeta\s+de\s+)?(descargas|escritorio|documentos)$|'
    r'^(?:riordina|organizza|sistema)\s+(?:i\s+miei\s+|i\s+|la\s+cartella\s+(?:dei\s+)?|la\s+)?(download|scaricati|scrivania|documenti)$|'
    r'^(?:organiza|arruma)\s+(?:as\s+minhas\s+|as\s+|os\s+|a\s+pasta\s+(?:de\s+)?|o\s+)?(transfer[eê]ncias|downloads|ambiente\s+de\s+trabalho|documentos)$', re.I)
_TIDY_UNDO = re.compile(
    r'^(?:верни|положи|сделай)\s+(?:все\s+|файлы\s+)?(?:как\s+было|обратно|на\s+место|назад)(?:\s+в\s+\w+)?$|^отмени\s+(?:сортировку|разбор)(?:\s+\w+)?$|'
    r'^(?:put\s+(?:it|them|everything)\s+back|undo\s+(?:the\s+)?(?:sorting|tidy(?:ing)?))$|^(?:mach\s+es\s+r[uü]ckg[aä]ngig|leg\s+alles\s+zur[uü]ck)$|'
    r'^remets(?:\s+tout)?\s+comme\s+avant$|^d[eé]jalo\s+como\s+estaba$|^rimetti(?:\s+tutto)?\s+com[\'’]era$|^volta\s+(?:a\s+p[oô]r\s+)?como\s+estava$', re.I)
_BIG = re.compile(
    r'^(?:найди|покажи)\s+(?:самые\s+)?(?:больш\w+|тяжел\w+|крупн\w+)\s+файл\w*(?:\s+(?:в|на)\s+(.+))?$|^что\s+занимает\s+(?:больше\s+всего\s+)?место(?:\s+на\s+диске)?$|'
    r'^(?:find|show)\s+(?:the\s+)?(?:largest|biggest|big|large)\s+files(?:\s+in\s+(.+))?$|^what\s+takes\s+up\s+(?:the\s+most\s+)?space$|'
    r'^(?:finde|zeig)\s+(?:die\s+)?gr[oö](?:ß|ss)ten\s+dateien(?:\s+in\s+(.+))?$|^(?:trouve|montre)\s+les\s+(?:plus\s+)?gros\s+fichiers(?:\s+dans\s+(.+))?$|'
    r'^(?:busca|encuentra|muestra)\s+los\s+archivos\s+m[aá]s\s+grandes(?:\s+en\s+(.+))?$|^(?:trova|mostra)\s+i\s+file\s+pi[uù]\s+grandi(?:\s+in\s+(.+))?$|'
    r'^(?:encontra|mostra)\s+os\s+(?:maiores\s+ficheiros|ficheiros\s+maiores|maiores\s+arquivos)(?:\s+em\s+(.+))?$', re.I)
_OPEN_BIG = re.compile(r'^(?:открой|покажи)\s+(?:папку\s+)?(?:с\s+файлом\s+)?(?:номер\s+)?(\d{1,2})$|^(?:open|show)\s+(?:the\s+)?folder\s+(?:number\s+)?(\d{1,2})$|'
                       r'^(?:[oö]ffne|zeig)\s+(?:den\s+)?ordner\s+(?:nummer\s+)?(\d{1,2})$|^(?:ouvre|montre)\s+le\s+dossier\s+(?:num[eé]ro\s+)?(\d{1,2})$|'
                       r'^(?:abre|muestra)\s+la\s+carpeta\s+(?:n[uú]mero\s+)?(\d{1,2})$|^(?:apri|mostra)\s+la\s+cartella\s+(?:numero\s+)?(\d{1,2})$|'
                       r'^(?:abre|mostra)\s+a\s+pasta\s+(?:n[uú]mero\s+)?(\d{1,2})$', re.I)

# ---- memory, corrections, learning, self-check ------------------------------------------------

_REMEMBER = re.compile(
    r'^(?:запомни|запиши\s+в\s+память|сохрани\s+в\s+памяти)[,:]?\s+(?:что\s+|то,?\s+что\s+)?(?P<v>.{3,200})$|^remember\s+(?:that\s+)?(?P<v2>.{3,200})$|'
    r'^(?:merk\s+dir,?\s+(?:dass\s+)?|merke\s+dir,?\s+dass\s+|merken\s+sie\s+sich,?\s+dass\s+)(?P<v3>.{3,200})$|^(?:retiens|souviens-toi)\s+(?:que\s+)?(?P<v4>.{3,200})$|^recuerda\s+(?:que\s+)?(?P<v5>.{3,200})$|'
    r'^ricorda(?:ti)?\s+(?:che\s+)?(?P<v6>.{3,200})$|^lembra-te\s+(?:de\s+que\s+|que\s+)?(?P<v7>.{3,200})$', re.I)

# Explicit fact shells own all remaining text, independent of payload length or
# line breaks. The shared note store, not a routing regex, validates the body.
_FACT_SHELL184 = {
    'ru': (r'(?:запомни|запиши\s+в\s+память|сохрани\s+в\s+памяти)', r'(?:то,?\s+)?что', r'пожалуйста'),
    'en': (r'remember', r'that', r'please'),
    'de': (r'(?:merk(?:e)?\s+dir|merken\s+sie\s+sich)', r'dass', r'bitte'),
    'fr': (r'(?:retiens|souviens-toi)', r'que', r"s['’]il\s+(?:te|vous)\s+pla[îi]t"),
    'es': (r'recuerda', r'que', r'por\s+favor'),
    'it': (r'ricorda(?:ti)?', r'che', r'per\s+favore'),
    'pt': (r'lembra-te', r'(?:de\s+)?que', r'por\s+favor'),
}

# Used only when an observed utterance differs from a fact-shaped transport
# rewrite. Native negative forms are not all in the generic action catalogue.
_NEGATED_FACT_VERB184 = {
    'ru': r'не\s+(?:запомни|запоминай|запиши\s+в\s+память|записывай\s+в\s+память|сохрани\s+в\s+памяти|сохраняй\s+в\s+памяти)',
    'en': r'(?:do\s+not|don[\'’]t|dont|never)\s+remember',
    'de': r'(?:merk(?:e)?\s+dir|merken\s+sie\s+sich)\s+nicht',
    'fr': r'ne\s+(?:retiens\s+pas|te\s+souviens\s+pas)',
    'es': r'no\s+recuerdes',
    'it': r'non\s+(?:ricordare|ricordarti|ricorda(?:ti)?)',
    'pt': r'(?:não|nao)\s+(?:te\s+lembres|lembres|lembra-te)',
}


def negated_fact_shell184(text: str, code: str) -> bool:
    """Recognize a native denied fact shell, never a denial inside its body."""
    row = _FACT_SHELL184.get(code)
    if row is None:
        return False
    _verb, conjunction, polite = row
    return bool(re.match(r'^(?:' + polite + r'\s*[,!:]?\s+)?' + _NEGATED_FACT_VERB184[code]
                         + r'\s*[:,]?\s+' + conjunction + r'(?=$|\s)', str(text or '').lstrip(), re.I))


def remember_fact_payload184(text: str, code: str, addresses=()) -> str | None:
    """Slice an explicit native fact without normalizing any captured data."""
    row = _FACT_SHELL184.get(code)
    if row is None:
        return None
    verb, conjunction, polite = row
    value = str(text or '').lstrip()
    # One separator belongs to the introducer. Everything after it, including
    # further whitespace, quotes, case, misspellings and CR/LF, is literal data.
    pattern = (r'^(?:' + polite + r'\s*[,!:]?\s+)?' + verb
               + r'\s*[:,]?\s+' + conjunction + r'(?:\r\n|\s|$)')
    shell = re.match(pattern, value, re.I)
    if shell is None and addresses:
        from jarvis.runtime.utterance_purpose import _address_pattern
        from jarvis.wake_matcher import wake_alias_requires_standalone
        aliases = tuple(sorted({str(name).strip() for name in addresses
                                if str(name).strip() and not wake_alias_requires_standalone(name)},
                               key=lambda name: (-len(name), name)))
        # The shared anchored address grammar is applied only to explicitly
        # configured names/aliases, never to a substring inside the fact body.
        address = _address_pattern(aliases).match(value) if aliases else None
        if address is not None:
            value = value[address.end():]
            shell = re.match(pattern, value, re.I)
    return value[shell.end():] if shell else None


def remember_fact_reply184(agent: Any, text: str, code: str, *, payload: str | None = None) -> Reply | None:
    body = payload if payload is not None else remember_fact_payload184(text, code)
    if body is None:
        return None
    clear_offer()
    # A fresh fact supersedes previous questions without interpreting its body
    # as an answer to a translation install, screen lookup, tidy plan or web offer.
    from jarvis.runtime import screen_help172, tidy172, translation184, web_answers171
    questions = (screen_help172, tidy172, translation184, web_answers171)
    owned_tokens = [(module, module.owned_question_token()) for module in questions]
    for module, token in owned_tokens:
        module.forget_if_owned(token)
    if not body.strip():
        from jarvis.runtime.quality_memory_text184 import text as memory_text
        return Reply(memory_text('empty_value', code), 'memory.remember_that', outcome='clarification')
    from jarvis.runtime import memory169
    try:
        ok, message = memory169.note(agent.database, agent.config, body, code)
    except Exception:
        # Storage may have committed before reporting an error. Do not claim
        # success or unchanged records, retry the write, or reinterpret the fact.
        logger.exception('Could not confirm explicit fact storage')
        return Reply(t('remember_failed', code), 'memory.remember_failed', outcome='internal_bug')
    return Reply(message, 'memory.remember_that', outcome='' if ok else 'user_state')

_ABOUT_ME = re.compile(
    r'^(?:я\s+(?:работаю|живу|учусь|люблю|обожаю|предпочитаю|играю|занимаюсь|пользуюсь)|меня\s+зовут|у\s+меня\s+(?:есть|дома|на\s+работе)|мой\s+любимый|моя\s+любимая|'
    r'мое\s+любимое|мне\s+нравится|я\s+(?:программист|дизайнер|студент|учитель|врач|инженер|геймер))\s+.{2,120}$|'
    r'^(?:i\s+(?:work|live|study|love|prefer|play|use)|my\s+name\s+is|i\s+have\s+a|my\s+favou?rite|i\s+(?:am|\'m)\s+an?\s+\w+)\b.{2,120}$|'
    r'^(?:ich\s+(?:arbeite|wohne|studiere|liebe|spiele|benutze)|ich\s+hei(?:ß|ss)e|mein\s+lieblings\w*|ich\s+bin\s+\w+in?\b).{2,120}$|'
    r'^(?:je\s+(?:travaille|vis|[eé]tudie|joue|pr[eé]f[eè]re|m[\'’]appelle)|j[\'’](?:habite|aime|utilise)|mon\s+\w+\s+pr[eé]f[eé]r[eé]).{2,120}$|'
    r'^(?:trabajo|vivo|estudio|me\s+llamo|me\s+encanta|prefiero|juego\s+a|mi\s+\w+\s+favorit[oa]).{2,120}$|'
    r'^(?:lavoro|vivo|abito|studio|mi\s+chiamo|adoro|preferisco|gioco\s+a|il\s+mio\s+\w+\s+preferit[oa]).{2,120}$|'
    r'^(?:trabalho|moro|vivo|estudo|chamo-me|adoro|prefiro|jogo|o\s+meu\s+\w+\s+favorit[oa]).{2,120}$', re.I)
_TRANSIENT = re.compile(r'(?:сейчас|сегодня|вчера|завтра|болит|устал|грустно|скучно|now|today|yesterday|tomorrow|tired|heute|gerade|maintenant|aujourd|hoy|ahora|oggi|adesso|hoje|agora)', re.I)
_NOT_THAT = re.compile(r'^(?:не\s+то|не\s+это|не\s+так|неправильно|неверно|ты\s+не\s+понял|ты\s+неправильно\s+понял|не\s+то\s+сделал|'
                       r'wrong|not\s+that|that\W?s\s+not\s+it|that\W?s\s+wrong|you\s+misunderstood|falsch|nicht\s+das|das\s+war\s+falsch|'
                       r'pas\s+[cç]a|ce\s+n[\'’]est\s+pas\s+[cç]a|c[\'’]est\s+faux|no\s+es\s+eso|eso\s+no|mal|non\s+[eè]\s+questo|sbagliato|n[aã]o\s+[eé]\s+isso|errado)$', re.I)
_REPORT = re.compile(r'^(?:как\s+ты\s+меня\s+понимаешь|насколько\s+ты\s+меня\s+понимаешь|отч[её]т\s+об?\s+обучени\w*|как\s+идет\s+обучение|'
                     r'how\s+well\s+do\s+you\s+understand\s+me|learning\s+report|wie\s+gut\s+verstehst\s+du\s+mich|lernbericht|'
                     r'tu\s+me\s+comprends\s+bien|rapport\s+d[\'’]apprentissage|qu[eé]\s+tan\s+bien\s+me\s+entiendes|informe\s+de\s+aprendizaje|'
                     r'quanto\s+mi\s+capisci|rapporto\s+di\s+apprendimento|quanto\s+me\s+percebes|relat[oó]rio\s+de\s+aprendizagem)$', re.I)
_CLEAR_PHRASES = re.compile(r'^(?:удали|очисти|сотри)\s+(?:все\s+)?собранные\s+фразы$|^(?:delete|clear)\s+(?:the\s+)?collected\s+phrases$|'
                            r'^l[oö]sche\s+die\s+gesammelten\s+s[aä]tze$|^(?:supprime|efface)\s+les\s+phrases\s+collect[eé]es$|'
                            r'^(?:borra|elimina)\s+las\s+frases\s+recogidas$|^(?:elimina|cancella)\s+le\s+frasi\s+raccolte$|^apaga\s+as\s+frases\s+recolhidas$', re.I)
_DEV_REPORT = re.compile(r'^(?:(?:сделай|собери|создай|сохрани)\s+)?отч[её]т\s+(?:для\s+разработчика|разработчику|для\s+проверки)$|^(?:make|create|save)?\s*(?:a\s+)?(?:developer|diagnostic)\s+report$|'
                         r'^(?:erstelle\s+)?(?:einen\s+)?entwicklerbericht$|^(?:cr[eé]e\s+)?(?:un\s+)?rapport\s+(?:pour\s+le\s+)?d[eé]veloppeur$|^(?:crea\s+)?(?:un\s+)?informe\s+(?:para\s+el\s+)?desarrollador$|'
                         r'^(?:crea\s+)?(?:un\s+)?rapporto\s+(?:per\s+lo\s+)?sviluppatore$|^(?:cria\s+)?(?:um\s+)?relat[oó]rio\s+(?:para\s+o\s+)?programador$', re.I)
_SELF_TEST = re.compile(r'^(?:проверь\s+себя|самопроверка|проведи\s+самопроверку|проверь\s+(?:все\s+)?свои\s+системы|check\s+yourself|run\s+a\s+self[-\s]test|self[-\s]test|'
                        r'pr[uü]f\s+dich\s+selbst|selbsttest|v[eé]rifie-toi|autotest|compru[eé]bate|autoprueba|controllati|autotest|verifica-te|autoteste)$', re.I)

_TEXT = {
    'remember_failed': ('Не удалось подтвердить сохранение сведений. Проверьте сохранённую память перед повторной попыткой.',
                        "I could not confirm that the fact was saved. Check saved memory before trying again.",
                        'Das Speichern der Angabe konnte nicht bestätigt werden. Prüfe die gespeicherten Erinnerungen vor einem erneuten Versuch.',
                        "Impossible de confirmer l’enregistrement de cette information. Vérifiez la mémoire enregistrée avant de réessayer.",
                        'No se pudo confirmar que el dato se guardó. Revisa la memoria guardada antes de intentarlo de nuevo.',
                        'Non è stato possibile confermare il salvataggio del dato. Controlla la memoria salvata prima di riprovare.',
                        'Não foi possível confirmar que o dado foi guardado. Verifique a memória guardada antes de tentar novamente.'),
    'reminders': ('Напоминания и расписание:\n{rows}\nУдалить: «удали напоминание 2».', 'Reminders and schedules:\n{rows}\nTo delete one: “delete reminder 2”.',
                  'Erinnerungen und Zeitpläne:\n{rows}\nLöschen: „lösche Erinnerung 2“.', 'Rappels et programmes :\n{rows}\nPour en supprimer un : « supprime le rappel 2 ».',
                  'Recordatorios y programaciones:\n{rows}\nPara borrar uno: «borra el recordatorio 2».', 'Promemoria e programmazioni:\n{rows}\nPer eliminarne uno: «elimina il promemoria 2».',
                  'Lembretes e agendamentos:\n{rows}\nPara apagar um: «apaga o lembrete 2».'),
    'no_reminders': ('Напоминаний и расписаний нет.', 'No reminders or schedules.', 'Keine Erinnerungen oder Zeitpläne.', 'Aucun rappel ni programme.',
                     'No hay recordatorios ni programaciones.', 'Nessun promemoria o programmazione.', 'Não há lembretes nem agendamentos.'),
    'removed': ('Удалил: {what}.', 'Deleted: {what}.', 'Gelöscht: {what}.', 'Supprimé : {what}.', 'Borrado: {what}.', 'Eliminato: {what}.', 'Apagado: {what}.'),
    'no_number': ('Нет пункта с номером {n}. Скажите «покажи напоминания».', 'There is no item {n}. Say “show reminders”.', 'Es gibt keinen Eintrag {n}. Sag „zeig Erinnerungen“.',
                  'Il n’y a pas d’élément {n}. Dites « montre mes rappels ».', 'No hay elemento {n}. Di «muestra mis recordatorios».',
                  'Non c’è la voce {n}. Di’ «mostra i promemoria».', 'Não há o item {n}. Diz «mostra os lembretes».'),
    'scheduled': ('Готово: {when} в {time} — {what}. Выполняю, только пока NEO запущен; список — «покажи напоминания».',
                  'Done: {when} at {time} — {what}. It runs while NEO is running; see “show reminders”.',
                  'Erledigt: {when} um {time} — {what}. Läuft, solange NEO läuft; Liste: „zeig Erinnerungen“.',
                  'C’est fait : {when} à {time} — {what}. Tant que NEO est lancé ; liste : « montre mes rappels ».',
                  'Hecho: {when} a las {time} — {what}. Funciona mientras NEO esté abierto; lista: «muestra mis recordatorios».',
                  'Fatto: {when} alle {time} — {what}. Funziona finché NEO è avviato; elenco: «mostra i promemoria».',
                  'Feito: {when} às {time} — {what}. Funciona enquanto o NEO estiver aberto; lista: «mostra os lembretes».'),
    'remind_what': ('напоминание «{text}»', 'reminder “{text}”', 'Erinnerung „{text}“', 'rappel « {text} »', 'recordatorio «{text}»', 'promemoria «{text}»', 'lembrete «{text}»'),
    'run_what': ('выполнить «{text}»', 'run “{text}”', '„{text}“ ausführen', 'exécuter « {text} »', 'ejecutar «{text}»', 'eseguire «{text}»', 'executar «{text}»'),
    'unknown_command': ('Не могу поставить это по расписанию: «{text}» — не команда NEO. Скажите, например, «каждый день в 9 открывай почту».',
                        'I can’t schedule that: “{text}” is not a NEO command. Say, for example, “every day at 9 open mail”.',
                        'Das kann ich nicht planen: „{text}“ ist kein NEO-Befehl. Sag zum Beispiel „jeden Tag um 9 öffne Mail“.',
                        'Je ne peux pas programmer ça : « {text} » n’est pas une commande NEO. Dites par exemple « tous les jours à 9 h ouvre la messagerie ».',
                        'No puedo programarlo: «{text}» no es un comando de NEO. Di, por ejemplo, «todos los días a las 9 abre el correo».',
                        'Non posso programmarlo: «{text}» non è un comando NEO. Di’ ad esempio «ogni giorno alle 9 apri la posta».',
                        'Não posso agendar isso: «{text}» não é um comando do NEO. Diz, por exemplo, «todos os dias às 9 abre o correio».'),
    'risky_command': ('«{text}» меняет что-то важное, поэтому по расписанию я его не выполняю: такие действия — только по вашей команде в тот момент.',
                      '“{text}” changes something important, so I don’t run it on a schedule: such actions only on your command at that moment.',
                      '„{text}“ ändert etwas Wichtiges, daher führe ich es nicht nach Zeitplan aus – nur auf deinen Befehl.',
                      '« {text} » modifie quelque chose d’important : je ne l’exécute pas selon un programme, seulement sur votre commande.',
                      '«{text}» cambia algo importante, así que no lo ejecuto programado: solo cuando tú lo pidas.',
                      '«{text}» modifica qualcosa di importante, quindi non lo eseguo a orario: solo su tuo comando.',
                      '«{text}» altera algo importante, por isso não o executo agendado: só quando o pedires.'),
    'remember_offer': ('Запомнить: «{text}»?', 'Shall I remember: “{text}”?', 'Soll ich mir merken: „{text}“?', 'Je retiens : « {text} » ?',
                       '¿Lo recuerdo: «{text}»?', 'Lo ricordo: «{text}»?', 'Guardo na memória: «{text}»?'),
    'not_remembered': ('Хорошо, не запоминаю.', 'OK, I won’t remember it.', 'Okay, ich merke es mir nicht.', 'D’accord, je ne le retiens pas.',
                       'De acuerdo, no lo guardo.', 'Va bene, non lo memorizzo.', 'Está bem, não guardo.'),
    'which_command': ('Что вы имели в виду? Скажите команду так, как нужно, — я выполню её и запомню, что «{text}» значит это.',
                      'What did you mean? Say the command the right way — I’ll run it and remember that “{text}” means that.',
                      'Was meintest du? Sag den Befehl richtig – ich führe ihn aus und merke mir, dass „{text}“ das bedeutet.',
                      'Que vouliez-vous dire ? Dites la bonne commande : je l’exécute et je retiens que « {text} » veut dire ça.',
                      '¿Qué querías decir? Di la orden correcta: la ejecuto y recuerdo que «{text}» significa eso.',
                      'Cosa intendevi? Di’ il comando giusto: lo eseguo e ricordo che «{text}» significa quello.',
                      'O que querias dizer? Diz o comando certo: executo-o e lembro que «{text}» significa isso.'),
    'nothing_before': ('Не помню предыдущей команды, которую можно исправить. Просто скажите, что сделать.',
                       'I don’t have a previous command to correct. Just tell me what to do.',
                       'Ich habe keinen vorherigen Befehl zum Korrigieren. Sag einfach, was ich tun soll.',
                       'Je n’ai pas de commande précédente à corriger. Dites simplement quoi faire.',
                       'No tengo una orden anterior que corregir. Dime qué hacer.', 'Non ho un comando precedente da correggere. Dimmi cosa fare.',
                       'Não tenho um comando anterior para corrigir. Diz só o que fazer.'),
}


_CORRECT_FACT184 = re.compile(
    r'^(?:(?:нет|no|nein|non|não)[,!]?\s+)?(?:исправление|поправка|correction|korrektur|corrección|correzione|correção)\s*[:,]?\s*'
    r'(?P<v>(?:мой|моя|моё|мое|мои|my|mein|meine|mon|ma|mes|mi|mis|il\s+mio|la\s+mia|i\s+miei|o\s+meu|a\s+minha)\s+\S.{1,200}?)[.!]?$', re.I)
_REPEAT_ANSWER184 = re.compile(
    r'^(?:повтори|скажи\s+ещё\s+раз|скажи\s+еще\s+раз)\s+(?:(?:свой|твой|мне)\s+)?(?:предыдущий|последний|прошлый)?\s*ответ$|'
    r'^(?:repeat|say\s+again)\s+(?:your\s+)?(?:previous|last)\s+(?:answer|reply)$|^wiederhole\s+(?:deine\s+)?(?:letzte|vorherige)\s+antwort$|'
    r'^r[eé]p[eè]te\s+(?:ta\s+)?(?:derni[eè]re\s+r[eé]ponse|r[eé]ponse\s+pr[eé]c[eé]dente)$|^repite\s+(?:tu\s+)?(?:[uú]ltima\s+respuesta|respuesta\s+anterior)$|'
    r'^ripeti\s+(?:la\s+tua\s+)?(?:ultima\s+risposta|risposta\s+precedente)$|^repete\s+(?:a\s+tua\s+)?(?:[uú]ltima\s+resposta|resposta\s+anterior)$', re.I)
_TEXT['nothing_to_repeat184'] = ('Я ещё ничего не отвечал в этом разговоре.', 'I have not answered anything in this conversation yet.',
                                 'Ich habe in diesem Gespräch noch nichts geantwortet.', 'Je n’ai encore rien répondu dans cette conversation.',
                                 'Todavía no he respondido nada en esta conversación.', 'Non ho ancora risposto nulla in questa conversazione.',
                                 'Ainda não respondi nada nesta conversa.')


def t(key: str, code: str, /, **values: Any) -> str:
    return _pick(_TEXT[key], code).format(**values)


# ---- per-conversation state -----------------------------------------------------------------

_lock = threading.Lock()
_state: dict[str, Any] = {'listing': []}
_MEMORY_OFFER_TTL = 120.0
_MEMORY_SCOPE_TTL = 1800.0
_MEMORY_SCOPE_LIMIT = 32
_MEMORY_DECLINE_LIMIT = 200


@dataclass
class _MemoryOfferState:
    offer: str = ''
    offered_at: float = 0.0
    touched: float = 0.0
    declined: OrderedDict[str, None] = field(default_factory=OrderedDict)


_memory_states: OrderedDict[str, _MemoryOfferState] = OrderedDict()


def _memory_scope(scope: str | None = None) -> str:
    # The journal defaults to desktop outside a scoped Agent turn, preserving
    # standalone callers while isolating desktop and each remote conversation.
    from jarvis.runtime.conversation_journal import current_scope
    return current_scope() if scope is None else str(scope)


def _memory_state_locked(scope: str | None, *, create: bool = False) -> _MemoryOfferState | None:
    """Called only with _lock held; no empty remote read allocates a new state."""
    now = time.monotonic()
    for key, state in list(_memory_states.items()):
        if now - state.touched > _MEMORY_SCOPE_TTL:
            del _memory_states[key]
    key = _memory_scope(scope)
    state = _memory_states.pop(key, None)
    if state is None and not create:
        return None
    if state is None:
        state = _MemoryOfferState()
    state.touched = now
    if state.offer and now - state.offered_at > _MEMORY_OFFER_TTL:
        state.offer, state.offered_at = '', 0.0
    _memory_states[key] = state
    while len(_memory_states) > _MEMORY_SCOPE_LIMIT:
        _memory_states.popitem(last=False)
    return state


def _offer_pending(*, scope: str | None = None) -> str:
    with _lock:
        state = _memory_state_locked(scope)
        return state.offer if state is not None else ''


def _take_offer(*, scope: str | None = None) -> str:
    """Consume only the current conversation's offer, atomically and once."""
    with _lock:
        state = _memory_state_locked(scope)
        if state is None:
            return ''
        text, state.offer, state.offered_at = state.offer, '', 0.0
        return text


def set_offer(text: str, *, scope: str | None = None) -> None:
    with _lock:
        state = _memory_state_locked(scope, create=True)
        state.offer, state.offered_at = str(text), time.monotonic()


def clear_offer(*, scope: str | None = None) -> None:
    with _lock:
        state = _memory_state_locked(scope)
        if state is not None:
            state.offer, state.offered_at = '', 0.0


def clear_memory_state(*, scope: str | None = None) -> None:
    """Reset pending offers and declined-offer suppression for one conversation."""
    with _lock:
        _memory_states.pop(_memory_scope(scope), None)


def _decline_offer(text: str) -> None:
    with _lock:
        state = _memory_state_locked(None, create=True)
        key = _n(text)
        state.declined.pop(key, None)
        state.declined[key] = None
        while len(state.declined) > _MEMORY_DECLINE_LIMIT:
            state.declined.popitem(last=False)


def about_me(text: str, config: Any) -> str:
    """A statement about the user worth offering to remember («я работаю в фотошопе»), or ''."""
    raw = ' '.join(str(text or '').split()).strip(' .!')
    if not raw or '?' in raw or len(raw.split()) > 14 or not bool(config.get('memory.propose', True)):
        return ''
    if not bool(config.get('memory.enabled', True)):
        return ''
    if _TRANSIENT.search(raw) or not _ABOUT_ME.match(raw.casefold().replace('ё', 'е')):
        return ''
    with _lock:
        state = _memory_state_locked(None)
        if state is not None and _n(raw) in state.declined:
            return ''
    return raw


# ---- the entry point -----------------------------------------------------------------------------

def handle(agent: Any, text: str, code: str, on_text: Callable[[str], None] | None = None) -> Reply | None:
    code = _code(code)
    t_ = _n(text)
    if not t_:
        return None
    config = agent.config
    from jarvis.runtime import screen_help172, tidy172

    # 1. answers to NEO's own questions
    from jarvis.runtime import translation184
    answered = translation184.answer_question(agent, text, code)       # NEO 184: «Установить модель перевода?»
    if answered is not None:
        return Reply(answered[0], answered[1])
    command = translation184.handle_command(agent, text, code)       # «установи модель перевода» …
    if command is not None:
        return Reply(command[0], command[1])
    if screen_help172.pending():
        query = screen_help172.resolve(text)
        if query == '':
            return Reply(screen_help172.t('cancelled', code), 'help.cancelled')
        if query:
            return _search(agent, query, code, 'help.search')
        screen_help172.forget()
    plan = tidy172.pending_plan()
    if plan is not None:
        if _YES.match(t_):
            tidy172.forget_plan()
            moved, errors = tidy172.apply(plan, _data_root(config), code)
            answer = tidy172.t('done', code, n=moved, folder=plan.folder)
            if errors:
                answer += tidy172.t('errors', code, n=len(errors), items='; '.join(errors[:3]))
            return Reply(answer, 'files.tidy_done')
        if _NO.match(t_):
            tidy172.forget_plan()
            return Reply(tidy172.t('cancelled', code), 'files.tidy_cancelled')
        tidy172.forget_plan()
    offer = _take_offer()
    if offer:
        if _YES.match(t_):
            from jarvis.runtime import memory169
            _ok, message = memory169.note(agent.database, config, offer, code)
            return Reply(message, 'memory.offer_saved')
        if _NO.match(t_):
            _decline_offer(offer)
            return Reply(t('not_remembered', code), 'memory.offer_declined')

    # 2. requests
    match = _CORRECT_FACT184.match(text.strip())
    if match and bool(config.get('memory.enabled', True)):
        from jarvis.runtime import memory169
        _ok, message = memory169.correct_note(agent.database, config, match.group('v'), code)
        return Reply(message, 'memory.corrected')
    if _REPEAT_ANSWER184.match(t_):
        # NEO 184: «повтори свой предыдущий ответ» shows the answer again (it is never copied anywhere).
        previous = str(getattr(agent, '_last_visible_answer184', '') or '')
        return Reply(previous or t('nothing_to_repeat184', code), 'dialogue.repeat_answer')
    if screen_help172.asks_about_screen(text):
        return _screen_help(agent, text, code)
    request = _explain_request(text)
    if request is not None:
        return _explain(agent, request, code, on_text)
    if _LIST_REMINDERS.match(t_):
        return Reply('', 'timer.list', run='timer.list')
    match = _CANCEL_REMINDER.match(t_)
    if match:
        return _cancel_reminder(config, code, int(next(g for g in match.groups() if g)))
    schedule = parse_schedule(text)
    if schedule is not None:
        return _schedule(agent, schedule, code)
    match = _TIDY.match(t_)
    if match:
        return _tidy(config, next(g for g in match.groups() if g), code)
    if _TIDY_UNDO.match(t_):
        restored, errors, folder = tidy172.undo(_data_root(config), code)
        if not folder:
            return Reply(tidy172.t('nothing_to_undo', code), 'files.tidy_undo')
        answer = tidy172.t('undone', code, n=restored, folder=folder)
        if errors:
            answer += tidy172.t('errors', code, n=len(errors), items='; '.join(errors[:3]))
            if tidy172.has_pending_undo(_data_root(config)):
                answer += tidy172.t('undo_kept', code)
        return Reply(answer, 'files.tidy_undo')
    match = _BIG.match(t_)
    if match:
        where = next((g for g in match.groups() if g), '')
        return _big(config, where, code)
    match = _OPEN_BIG.match(t_)
    if match:
        row = tidy172.big_row(int(next(g for g in match.groups() if g)))
        if row is not None:
            _show_in_explorer(row[1])
            from pathlib import Path
            return Reply(tidy172.t('opened', code, name=Path(row[1]).name), 'files.open_folder')
    fact = remember_fact_reply184(agent, text, code)
    if fact is not None:
        return fact
    match = _REMEMBER.match(text.strip(' .!'))
    if match:
        # Explicit native key:value syntax keeps its existing preference route.
        # All pending-question cleanup above still runs for this new request.
        tools = getattr(agent, 'tools', None)
        if tools is not None:
            from jarvis.runtime.canonical_commands import parse_canonical_command
            native = parse_canonical_command(text, code, lambda name: tools.get(name).parameters)
            if (native is not None and not native.error and native.tool == 'memory'
                    and native.arguments.get('action') == 'remember'):
                return None
        value = next(v for v in match.groupdict().values() if v).strip(' .!')
        from jarvis.runtime import memory169
        _ok, message = memory169.note(agent.database, config, value, code)
        return Reply(message, 'memory.remember_that')
    if _NOT_THAT.match(t_):
        learned = getattr(agent, '_learned_prev177', None)
        if learned:
            # NEO 177: the remembered meaning of that phrase was wrong: forget it.
            from jarvis.runtime import learned177
            learned177.forget(config, learned, code)
            agent._learned_prev177 = None
        previous = getattr(agent, '_last_executed172', None) or getattr(agent, '_last_command169', None)
        if not previous:
            return Reply(t('nothing_before', code), 'learning.not_that')
        agent._correction_wait172 = (previous, time.monotonic())
        return Reply(t('which_command', code, text=previous[0]), 'learning.not_that', outcome='clarification')
    if _REPORT.match(t_):
        from jarvis.runtime import learning172
        return Reply(learning172.report_text(config, code), 'learning.report')
    if _CLEAR_PHRASES.match(t_):
        from jarvis.runtime import learning172
        from jarvis.runtime import learned177
        return Reply(learning172.cleared_text(learning172.clear(config) + learned177.clear(config), code), 'learning.cleared')
    if _DEV_REPORT.match(t_):
        return _developer_report(agent, code)
    if _SELF_TEST.match(t_):
        return Reply('', 'diagnostics.self_test', run='diagnostics.self_test')

    # 3. a statement about the user: offered to be remembered after the normal answer
    statement = about_me(text, config)
    if statement:
        set_offer(statement)
        agent._memory_offer172 = t('remember_offer', code, text=statement)
    return None


def _developer_report(agent: Any, code: str) -> Reply:
    from jarvis.runtime import fieldkit173
    ollama = getattr(agent, 'ollama', None)

    def ollama_state() -> dict[str, Any]:
        from jarvis.runtime import model_life173
        state: dict[str, Any] = {}
        try:
            state['installed'] = [m for m in ollama._list_models(force=True) if m]
        except Exception as exc:
            state['installed_error'] = type(exc).__name__
        try:
            state['loaded'] = sorted(model_life173.loaded_models(agent.config, ollama._session))
        except Exception as exc:
            state['loaded_error'] = type(exc).__name__
        return state

    def self_test() -> dict[str, Any]:
        from jarvis.runtime.self_test import run_self_test
        return run_self_test(agent.config, quick=True)
    version = ''
    try:
        from pathlib import Path
        version = (Path(__file__).resolve().parents[2] / 'VERSION.txt').read_text(encoding='utf-8').strip()
    except OSError:
        pass
    path, _text = fieldkit173.report(agent.config, version=version, ollama_state=ollama_state if ollama is not None else None,
                                     self_test=self_test)
    if path is None:
        return Reply(_text[:3000], 'diagnostics.developer_report')
    _show_in_explorer(str(path))
    return Reply(fieldkit173.saved_text(path, code), 'diagnostics.developer_report')


def _data_root(config: Any):
    from pathlib import Path
    root = getattr(getattr(config, 'paths', None), 'root', None)
    return Path(root) if root else Path.home() / '.neo'


def _search(agent: Any, query: str, code: str, detail: str) -> Reply:
    from jarvis.routing.ownership_firewall import override_turn_ownership
    override_turn_ownership('web_information', reason='accepted screen-help search', text=query)
    try:
        _owned, text, _meta = agent.game_web.try_general_answer(query, force=True)
    except Exception:
        logger.exception('Screen-help search failed')
        text = agent.game_web.unavailable_message('searchapi_response_error')
    return Reply(text, detail)


def _screen_help(agent: Any, text: str, code: str) -> Reply:
    from jarvis.runtime import screen_help172
    if os.name != 'nt' and not getattr(agent, '_screen_reader172', None):
        return Reply(screen_help172.t('unsupported', code), 'help.unsupported')
    config = agent.config
    reader = getattr(agent, '_screen_reader172', None) or screen_help172.read_screen
    try:
        reading = reader(ollama=getattr(agent, 'ollama', None), vision=bool(config.get('help.use_screen_model', True)))
    except Exception:
        logger.exception('Reading the window failed')
        return Reply(screen_help172.t('no_window', code), 'help.no_window')
    if reading.method == 'none' and not reading.app:
        return Reply(screen_help172.t('no_window', code), 'help.no_window')
    if not reading.lines:
        return Reply(screen_help172.t('nothing', code, app=reading.app or reading.title), 'help.nothing')
    query = screen_help172.build_query(reading, text, code)
    what = ' / '.join(screen_help172.scrub(line) for line in reading.lines[:2])
    if bool(config.get('help.confirm_before_search', True)):
        screen_help172.ask(query)
        return Reply(screen_help172.t('confirm', code, app=reading.app or reading.title, what=what, query=query), 'help.confirm',
                     outcome='clarification')
    return _search(agent, query, code, 'help.search')


def _explain_request(text: str):
    from jarvis.runtime import explain172
    return explain172.parse(text)


def _explain(agent: Any, request: Any, code: str, on_text) -> Reply:
    from jarvis.runtime import explain172
    taker = getattr(agent, '_text_taker172', None) or explain172.take
    try:
        taken = taker(request.source)
    except Exception:
        logger.exception('Taking the text failed')
        taken = None
    if taken is None or not taken.text.strip():
        return Reply(explain172.nothing_text(request.source, code), f'explain.{request.op}.nothing')
    answer, streamed = explain172.run(request.op, taken, code, ollama=getattr(agent, 'ollama', None), config=agent.config,
                                      target=request.target_language, on_text=on_text)
    return Reply(answer, f'explain.{request.op}.{taken.where}', streamed=streamed)


def _timers(config: Any):
    from jarvis.runtime.timers import timer_manager
    from jarvis.language import language_code
    return timer_manager(_data_root(config), language_code(config))


def listing_text(rows: list[Any], code: str) -> str:
    """The numbered list of reminders, timers and schedules (also used by the timer tool)."""
    from datetime import datetime
    if not rows:
        with _lock:
            _state['listing'] = []
        return t('no_reminders', code)
    lines = []
    for i, item in enumerate(rows, 1):
        if getattr(item, 'repeat', ''):
            label = item.label or item.command
            what = t('run_what', code, text=item.command) if item.command else t('remind_what', code, text=label)
            lines.append(f'{i}. {repeat_text(item.repeat, code)} {item.hour:02d}:{item.minute:02d} — {what}')
        else:
            from jarvis.runtime.timers import timer_manager
            shown = timer_manager().display_due(item)        # HIST-030: a duration shows its monotonic projection
            lines.append(f'{i}. {datetime.fromtimestamp(shown):%d.%m %H:%M} — {item.label}')
    with _lock:
        _state['listing'] = [item.id for item in rows]
    return t('reminders', code, rows='\n'.join(lines))


def _cancel_reminder(config: Any, code: str, number: int) -> Reply:
    manager = _timers(config)
    with _lock:
        listing = list(_state['listing'])
    ids = listing or [item.id for item in manager.list()]
    if not 1 <= number <= len(ids):
        return Reply(t('no_number', code, n=number), 'timer.cancel')
    item = next((x for x in manager.list() if x.id == ids[number - 1]), None)
    if item is None or not manager.cancel(item.id):
        return Reply(t('no_number', code, n=number), 'timer.cancel')
    return Reply(t('removed', code, what=item.label or item.command), 'timer.cancel')


def _schedule(agent: Any, schedule: Schedule, code: str) -> Reply:
    manager = _timers(agent.config)
    clock = f'{schedule.hour:02d}:{schedule.minute:02d}'
    when = repeat_text(schedule.repeat, code)
    if schedule.reminder:
        manager.create_repeating(schedule.hour, schedule.minute, schedule.repeat, schedule.reminder)
        return Reply(t('scheduled', code, when=when, time=clock, what=t('remind_what', code, text=schedule.reminder)), 'timer.repeat_reminder')
    parsed = None
    for name in ('_task_parse169', '_complete_command'):
        if parsed is None and hasattr(agent, name):
            try:
                parsed = getattr(agent, name)(schedule.command, code)
            except Exception:
                parsed = None
    if parsed is None:
        return Reply(t('unknown_command', code, text=schedule.command), 'timer.schedule_unknown', outcome='clarification')
    tool, arguments = parsed
    try:
        risky = agent._needs_confirmation(tool, arguments, agent.tools.risk_for(tool, arguments))
    except Exception:
        risky = True
    if risky:
        return Reply(t('risky_command', code, text=schedule.command), 'timer.schedule_refused')
    manager.create_repeating(schedule.hour, schedule.minute, schedule.repeat, schedule.command, command=schedule.command)
    return Reply(t('scheduled', code, when=when, time=clock, what=t('run_what', code, text=schedule.command)), 'timer.schedule')


def _tidy(config: Any, folder_word: str, code: str) -> Reply:
    from jarvis.runtime import tidy172
    key = tidy172.which_folder(folder_word)
    folder = tidy172.known_folder(key)
    p = tidy172.plan(folder, code, desktop=key == 'desktop')
    if not p.moves:
        return Reply(tidy172.t('empty', code, folder=str(folder)), 'files.tidy_plan')
    tidy172.remember_plan(p)
    return Reply(tidy172.plan_text(p, code), 'files.tidy_plan', outcome='clarification')


def _big(config: Any, where: str, code: str) -> Reply:
    from jarvis.runtime import tidy172
    if where:
        key = tidy172.which_folder(where)
        folders = [tidy172.known_folder(key)]
    else:
        folders = [tidy172.known_folder('downloads'), tidy172.known_folder('desktop'), tidy172.known_folder('documents')]
        from pathlib import Path
        for extra in ('Videos', 'Music', 'Pictures'):
            path = Path.home() / extra
            if path.is_dir():
                folders.append(path)
    rows = tidy172.big_files([f for f in folders if f.is_dir()])
    where_text = ', '.join(f.name for f in folders)
    tidy172.remember_big(rows)
    if not rows:
        return Reply(tidy172.t('no_big', code, size=tidy172.size_text(50 * 1024 * 1024, code), where=where_text), 'files.big')
    return Reply(tidy172.big_text(rows, code, where_text), 'files.big')


def _show_in_explorer(path: str) -> None:
    if os.name != 'nt':
        return
    import subprocess
    subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])


__all__ = ['Reply', 'Schedule', 'about_me', 'clear_offer', 'clear_memory_state', 'handle', 'parse_schedule', 'repeat_text', 'set_offer', 't']
