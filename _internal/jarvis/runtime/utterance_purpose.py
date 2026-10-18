"""Conservative speech-act boundary: quoted/past actions are not instructions.

This classifies only explicit surface constructions. Unknown speech is left to
existing local routing; a guessed purpose never authorises a tool call.
"""
from __future__ import annotations
from dataclasses import dataclass
from functools import lru_cache
import re
from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell

_DEFAULT_ADDRESSES = ('jarvis', 'джарвис', 'джервис', 'neo', 'нео', 'néo')


@lru_cache(maxsize=8)
def _address_pattern(names: tuple[str, ...]):
    # One cached expression, not hundreds of re.match calls per turn. The
    # personalized wake atlas can exceed Python's regex-cache capacity; the old
    # loop evicted the command grammar and recompiled both on every utterance.
    alternatives = "|".join(re.escape(name) for name in names)
    return re.compile(r"^(?:(?:hey|эй|ok|okay|окей)\s+)?(?:" + alternatives +
                      r")(?:\s*[,!:—-]\s*|[.?…]+(?:\s+|$)|\s+)", re.I)


def strip_assistant_address(text: str, names=()) -> str:
    from jarvis.wake_matcher import wake_alias_requires_standalone
    value = str(text).strip()
    aliases = tuple(sorted({*_DEFAULT_ADDRESSES, *(str(n).strip() for n in names if str(n).strip() and not wake_alias_requires_standalone(n))},
                           key=lambda name: (-len(name), name)))
    match = _address_pattern(aliases).match(value)
    return value[match.end():].strip() if match else value


_CORRECTIONS = {
    'ru': r'(?:нет[, ]+)?(?:я (?:имел|имела) в виду|я (?:хотел|хотела) сказать|поправка|исправление)|нет[, ]+(?:не это[, ]+а|не так[, ]+а|лучше|вместо этого)|(?:точнее|вернее)',
    'en': r'(?:no[, ]+)?(?:i meant|i mean|correction|i wanted to say)|no[, ]+(?:instead|rather)|rather',
    'de': r'(?:nein[, ]+)?(?:ich meinte|ich meine|korrektur)|nein[, ]+(?:stattdessen|lieber)',
    'fr': r'(?:non[, ]+)?(?:je voulais dire|je veux dire|je parlais de|correction)|non[, ]+plutôt',
    'es': r'(?:no[, ]+)?(?:quise decir|quería decir|quiero decir|me refería a(?:l)?|me refiero a(?:l)?|corrección)|no[, ]+mejor',
    'it': r'(?:no[, ]+)?(?:intendevo|intendo|volevo dire|correzione)|no[, ]+piuttosto',
    'pt': r'(?:não[, ]+)?(?:quis dizer|queria dizer|quero dizer|referia-me a|estava a falar de|correção)|não[, ]+antes',
}
_RETROSPECTIVE = {
    'ru': r'(?:почему|зачем)\s+ты\s+(?:открыл|открыла|закрыл|закрыла|включил|выключил|удалил|переместил|поставил|запустил|переключил|остановил|сделал|выбрал|отправил|отправила)|ты\s+(?:уже\s+)?(?:открыл|закрыл|удалил|поставил|сделал|переключил|остановил)\b|что\s+ты\s+(?:сделал|выполнил)\b',
    'en': r'(?:why|when)\s+did\s+you\b|(?:have you|did you)\s+(?:already\s+)?(?:open|opened|close|closed|delete|deleted|start|started|stop|stopped|play|played|set|switch|switched)\b',
    'de': r'(?:warum|weshalb|wann)\s+hast\s+du\b|hast du\s+(?:schon|bereits)\b',
    'fr': r'(?:pourquoi|quand)\s+(?:as-tu|avez-vous|tu as)\b|(?:as-tu|tu as)\s+déjà\b',
    'es': r'(?:por qué|cuándo)\s+(?:has\s+)?(?:abierto|cerrado|borrado|iniciado|parado|cambiado|hecho|abriste|cerraste|borraste|iniciaste|paraste|cambiaste|hiciste)\b|(?:ya\s+)?has\s+(?:abierto|cerrado|borrado|iniciado|hecho)\b',
    'it': r'(?:perché|quando)\s+hai\b|hai\s+già\b',
    'pt': r'(?:por que|porque|porquê|quando)\s+(?:(?:tu|você)\s+)?(?:abriste|abriu|fechaste|fechou|apagaste|apagou|fizeste|fez|iniciaste|iniciou|mudaste|mudou|paraste|parou)\b|já\s+(?:abriste|fechaste|apagaste|fizeste)\b',
}
_REPORTED = {
    'ru': r'(?:я (?:сказал|сказала|просил|просила)|(?:мой друг|моя подруга|друг|подруга|коллега|он|она) (?:сказал|сказала|написал|написала)|в сообщении написано|цитата)\b',
    'en': r'(?:i said|i asked|he said|she said|my friend said|my colleague said|the message says|quote)\b',
    'de': r'(?:ich sagte|ich habe gesagt|er sagte|sie sagte|mein freund sagte|meine freundin sagte|in der nachricht steht|zitat)\b',
    'fr': r'(?:j[\x27’]ai dit|il a dit|elle a dit|mon ami a dit|mon amie a dit|le message dit|citation)\b',
    'es': r'(?:yo dije|dije|él dijo|ella dijo|mi amigo dijo|mi amiga dijo|el mensaje dice|cita)\b',
    'it': r'(?:ho detto|lui ha detto|lei ha detto|il mio amico ha detto|la mia amica ha detto|il messaggio dice|citazione)\b',
    'pt': r'(?:eu disse|ele disse|ela disse|o meu amigo disse|a minha amiga disse|a mensagem diz|citação)\b',
}
_CANCEL = {
    'ru': {'отмена','отмени','не надо','забудь','забудь эту команду','нет'},
    'en': {'cancel','never mind','nevermind','forget it','no'},
    'de': {'abbrechen','abbruch','vergiss es','nein'},
    'fr': {'annule','annuler','laisse tomber','non'},
    'es': {'cancela','cancelar','olvídalo','no'},
    'it': {'annulla','annullare','lascia perdere','no'},
    'pt': {'cancela','cancelar','esquece','não'},
}


_REPORT_VERBS182 = (
    r'сказал[аи]?|написал[аи]?|спросил[аи]?|прислал[аи]?|ответил[аи]?|пишет|говорит|'
    r'said|wrote|asked|texted|sent|replied|says|writes|'
    r'sagte|schrieb|fragte|hat (?:gesagt|geschrieben|gefragt)|schreibt|'
    r'a (?:dit|écrit|demandé|envoyé|répondu)|ont (?:dit|écrit)|écrit|dit|'
    r'dijo|escribió|preguntó|envió|respondió|ha (?:dicho|escrito)|escribe|'
    r'ha (?:detto|scritto|chiesto)|disse|scrisse|scrive|'
    r'escreveu|perguntou|enviou|respondeu|disse|escreve'
)
# NEO 183: the reporting verb needs a subject before it («Mon ami a écrit : …», «Начальник написал: …»);
# at the very start «escribe "Hola"» / «escreve "Olá"» is an imperative — a request to type.
_QUOTED_REPORT182 = re.compile(
    r'\S\s+(?:' + _REPORT_VERBS182 + r')\s*(?:(?:что|that|dass|que|che)\s*)?(?:[:,]?\s*[«"“„\'‹]|:\s*\S)', re.I)
# «На экране написано: …», «the message says: …», «Na mensagem está escrito: …» — text somebody/something shows.
_IMPERSONAL_REPORT183 = re.compile(
    r'(?:^|\s)(?:написано|сказано|говорится|it says|says|is written|steht(?:\s+geschrieben)?|est écrit|on lit|'
    r'está escrito|pone|dice|c[\'’]è scritto|è scritto|diz)\s*(?::|[«"“„‹])', re.I)
# A request that starts with its own command verb carries the rest as opaque data: «type My boss
# wrote: "Hello"» types that text, «copy to clipboard …» copies it.
_EXPLICIT_COMMAND183 = re.compile(
    r'^(?:напиши|напечатай|введи|скопируй|вставь|запиши|отправь|продиктуй|открой|type|write|copy|paste|send|put|dictate|open|öffne|ouvre|abre|apri|'
    r'schreib\w*|tipp\w*|kopier\w*|füg\w*|écris|tape|copie|colle|envoie|escribe|escriba|teclea|copia|pega|envía|envia|'
    r'scrivi|digita|incolla|invia|escreve|escreva|digite|cola)\b', re.I)


@dataclass(frozen=True)
class Utterance:
    purpose: str
    text: str


@lru_cache(maxsize=7)
def _patterns(code: str):
    return tuple(re.compile(r'^(?:' + rows[code] + r')', re.I | re.S) for rows in (_CORRECTIONS, _RETROSPECTIVE, _REPORTED))


def classify_utterance(text: str, code: str) -> Utterance:
    value = str(text).strip()
    if code not in LANGUAGES:
        return Utterance('unknown', value)
    examples = {
        'ru': r'(?:это\s+(?:не команда|(?:только\s+)?пример)|пример команды|не выполняй[, :])',
        'en': r'(?:this is (?:an example|not a command)|example command|do not execute[, :])',
        'de': r'(?:dies ist (?:ein Beispiel|kein Befehl)|Beispielbefehl)',
        'fr': r'(?:ceci est (?:un exemple|pas une commande)|exemple de commande)',
        'es': r'(?:esto es (?:un ejemplo|no una orden)|ejemplo de comando)',
        'it': r'(?:questo è (?:un esempio|non un comando)|esempio di comando)',
        'pt': r'(?:isto é (?:um exemplo|não um comando)|exemplo de comando)',
    }
    if re.match(examples[code], value, re.I):
        return Utterance('reported', value)
    correction, retrospective, reported = _patterns(code)
    match = correction.match(value)
    if match and (match.end() == len(value) or value[match.end()] in ' \t,:—-'):
        return Utterance('correction', value[match.end():].strip(' \t,:—-'))
    no = {'ru':'нет', 'en':'no', 'de':'nein', 'fr':'non', 'es':'no', 'it':'no', 'pt':'não'}[code]
    explicit_no = re.match(r'^' + no + r'\s*[,;:]\s*(.+)$', value, re.I | re.S)
    if explicit_no:
        return Utterance('correction', explicit_no.group(1).strip())
    if retrospective.match(value):
        return Utterance('retrospective', value)
    if _EXPLICIT_COMMAND183.match(value):
        return Utterance('unknown', value)            # its own command; the payload is not inspected
    if reported.match(value):
        return Utterance('reported', value)
    # NEO 182: «<someone> wrote/said/asked: "<command>"» in any of the seven languages is a report
    # of someone else's words, never an instruction — whoever the subject is.
    if _QUOTED_REPORT182.search(value) or _IMPERSONAL_REPORT183.search(value):
        return Utterance('reported', value)
    if norm(value) in _CANCEL[code]:
        return Utterance('cancel', value)
    return Utterance('unknown', value)
