"""NEO 175: two typo repairs found by the phrasing/typo analysis of all commands.

* Short function words typed with swapped letters: «больше ен включай» (не), «оп ссылке»
  (по), «зи буфера» (из), «иом треки» (мои). The worst case lost the negation and turned
  «больше не включай эту песню» into «play». A token is changed only when it is not a
  real word of the language and exactly one function word matches it.
* Service and app names with a typo: «dscord», «spotiy», «янекс музыке». The command
  then went to the default player instead of the named service.

Both run for typed text only (speech recognition does not swap letters), before routing,
and never inside quotes, after a colon (dictation) or in paths and links.
"""
from __future__ import annotations

import re

FUNCTION_WORDS = {
    'ru': ('не', 'по', 'из', 'на', 'во', 'до', 'от', 'за', 'мои', 'мою', 'мой', 'моя', 'мне', 'для', 'при', 'без', 'про',
           'над', 'под', 'как', 'что', 'это', 'все', 'всё', 'так', 'уже', 'еще', 'ещё'),
    'en': ('to', 'in', 'of', 'on', 'at', 'by', 'for', 'the', 'my', 'me', 'it', 'is', 'and', 'not', 'up', 'off', 'all'),
    'de': ('in', 'zu', 'an', 'auf', 'im', 'am', 'um', 'den', 'der', 'die', 'das', 'mit', 'von', 'aus', 'bei', 'mein', 'und',
           'ist', 'ein', 'vom', 'zum', 'zur'),
    'fr': ('de', 'le', 'la', 'les', 'un', 'une', 'en', 'au', 'du', 'sur', 'pas', 'mon', 'ma', 'mes', 'que', 'qui', 'des', 'aux',
           'par', 'est'),
    'es': ('de', 'el', 'la', 'los', 'las', 'un', 'en', 'con', 'por', 'mi', 'mis', 'no', 'al', 'del', 'que', 'lo', 'una', 'sin'),
    'it': ('di', 'il', 'la', 'le', 'un', 'in', 'su', 'con', 'per', 'mio', 'mia', 'non', 'al', 'del', 'nel', 'sul', 'nei', 'sui',
           'che', 'lo', 'gli', 'dal', 'una', 'uno'),
    'pt': ('de', 'os', 'as', 'um', 'em', 'no', 'na', 'com', 'por', 'meu', 'não', 'ao', 'do', 'da', 'que', 'nos', 'nas', 'dos',
           'das', 'uma'),
}
# Common short words that are never a typo (some are missing from the typo lexicon).
COMMON_SHORT = {
    'ru': set('не но на он ну мы вы ты да до за из от по со ко во об же ли бы то её ее их им ей ой ах эх ох ага ага угу ок '
              'где кто как что чем там тут вот уже еще ещё нет вон мой моя мне меня тебе тебя его ему оно они'.split()),
    'en': set('no on is it at so or an as be by do go he if me my of ok oh up us we am hi to in all and not the for you are '
              'was can has had her him his how its let may new now old one our out own see she too two use way who why yes '
              'yet get got put run set off top any few'.split()),
    'de': set('in an im am um zu da du er es ja wo so ob ab bis den der die das dem des ein mit von aus bei auf ist was wie '
              'wer und nie nun hat ich sie wir ihr mir dir'.split()),
    'fr': set('de le la en un au du sur pas mon ma mes il et ou on je tu ne se ce sa ta si ni oui non est une les des '
              'qui que moi toi lui eux'.split()),
    'es': set('de el la en un no al del con por mi mis tu te se si ya yo lo le me es os las los una que más mas sin '
              'hay muy'.split()),
    'it': set('di il la le un in su con per mio mia non al del lo gli ma se si ci ne io tu lui noi voi che è ho ha '
              'una uno'.split()),
    'pt': set('de os as um em no na com por meu não ao do da se eu tu ele ela me te lhe já há uma uns que é'.split()),
}
NAMES = ('discord', 'spotify', 'youtube', 'telegram', 'steam', 'twitch', 'whatsapp', 'chrome', 'firefox', 'yandex',
         'notepad', 'explorer', 'bluetooth', 'яндекс', 'дискорд', 'дискорде', 'спотифай', 'ютуб', 'ютубе', 'телеграм',
         'телеграме', 'телеграмм', 'телеграмме', 'стиме', 'блютуз', 'музыке', 'музыку', 'музыка')
_PROTECTED = re.compile(r'[«»“”"]|:\s|[\\/]|https?://|www\.', re.I)
_TOKEN = re.compile(r"[^\W\d_]+", re.U)


def _known(word: str, code: str) -> bool:
    try:
        from jarvis.runtime.command_typos165 import known
        return known(word, code)
    except Exception:
        return True


def _swaps(word: str) -> set[str]:
    out = {word[::-1]}
    for i in range(len(word) - 1):
        out.add(word[:i] + word[i + 1] + word[i] + word[i + 2:])
    out.discard(word)
    return out


def _distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (adjacent transposition) distance."""
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            rows[i][j] = min(rows[i - 1][j] + 1, rows[i][j - 1] + 1, rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[-1][-1]


def _short_word(token: str, code: str) -> str:
    low = token.casefold()
    words = FUNCTION_WORDS.get(code, ())
    if not 2 <= len(low) <= 3 or low in words or low in COMMON_SHORT.get(code, ()) or _known(low, code):
        return token
    matches = sorted(w for w in _swaps(low) if w in words)
    return matches[0] if len(matches) == 1 else token


def _name(token: str, code: str) -> str:
    low = token.casefold()
    if len(low) < 5 or low in NAMES or _known(low, code):
        return token
    limit = 1 if len(low) <= 7 else 2
    scored = sorted((_distance(low, name), name) for name in NAMES if abs(len(name) - len(low)) <= limit)
    if not scored or scored[0][0] > limit or (len(scored) > 1 and scored[1][0] == scored[0][0] and scored[1][1] != scored[0][1]):
        return token
    return scored[0][1]


def repair(text: str, code: str) -> str:
    """The text with both repairs applied (unchanged when nothing is certain)."""
    value = str(text or '')
    if not value.strip() or len(value) > 300 or _PROTECTED.search(value):
        return value

    def fix(match: re.Match) -> str:
        token = match.group(0)
        fixed = _short_word(token, code)
        if fixed == token:
            fixed = _name(token, code)
        return fixed
    return _TOKEN.sub(fix, value)


# NEO 175: a guess must never turn a request into its opposite («opn … mini browser» -> close).
_OPEN_WORDS = ('open', 'start', 'launch', 'show', 'enable', 'resume', 'play', 'открой', 'запусти', 'покажи', 'включи',
               'продолжи', 'öffne', 'starte', 'zeige', 'ouvre', 'lance', 'affiche', 'abre', 'inicia', 'muestra', 'apri',
               'avvia', 'mostra', 'abra', 'inicie')
_CLOSE_WORDS = ('close', 'stop', 'exit', 'quit', 'hide', 'disable', 'pause', 'закрой', 'выключи', 'останови', 'скрой',
                'отключи', 'выйди', 'schließe', 'schliesse', 'beende', 'stoppe', 'ferme', 'arrête', 'quitte', 'cierra',
                'detén', 'para', 'chiudi', 'ferma', 'esci', 'fecha', 'pare')
_CLOSE_ACTION = re.compile(r'close|stop|exit|quit|hide|off|disable|pause|disconnect|end', re.I)
_OPEN_ACTION = re.compile(r'open|start|launch|show|on$|enable|resume|play|connect', re.I)


def _near(words: tuple[str, ...], text: str) -> bool:
    for token in _TOKEN.findall(str(text or '').casefold()):
        if len(token) >= 3 and any(_distance(token, word) <= (1 if len(word) <= 6 else 2) for word in words):
            return True
    return False


# NEO 188 atlas: Russian infinitive commands («включить микрофон») count as the same verbs.
_OPEN_WORDS188 = _OPEN_WORDS + ('включить', 'открыть', 'запустить', 'показать', 'продолжить', 'unmute', 'размуть')
_CLOSE_WORDS188 = _CLOSE_WORDS + ('выключить', 'отключить', 'закрыть', 'остановить', 'скрыть', 'mute', 'заглуши')


def opposite_guess(action: str, text: str, arguments: dict | None = None) -> bool:
    """True when a guessed action is the opposite of what the words say.

    The polarity can also live in the arguments (``muted``/``enabled``): a guess
    «mute the microphone» for «включить микрофон» is the opposite request too.
    """
    action = str(action or '')
    args = arguments if isinstance(arguments, dict) else {}
    closing = bool(_CLOSE_ACTION.search(action)) or args.get('muted') is True or args.get('enabled') is False
    opening = (bool(_OPEN_ACTION.search(action)) and not _CLOSE_ACTION.search(action)) or args.get('muted') is False \
        or args.get('enabled') is True
    tokens = set(_TOKEN.findall(str(text or '').casefold()))
    exact_open, exact_close = bool(tokens & set(_OPEN_WORDS188)), bool(tokens & set(_CLOSE_WORDS188))
    if exact_open != exact_close:
        # An exactly spelled verb decides; «включить» is not a typo of «выключить».
        says_open, says_close = exact_open, exact_close
    else:
        says_open, says_close = _near(_OPEN_WORDS188, text), _near(_CLOSE_WORDS188, text)
    if closing and not opening and says_open and not says_close:
        return True
    if opening and not closing and says_close and not says_open:
        return True
    return False


# NEO 188 atlas (contract 5/6): a typo repair never turns an «on» verb into an «off» verb
# or back («riattiva» → «disattiva», «включить» → «выключи»).
_ON_VERBS188 = frozenset("""
    включи включить включите открой открыть запусти запустить покажи покажись верни размуть размьють
    enable activate start open show unmute resume play
    aktiviere aktivieren einschalten anschalten öffne offne starte zeige zeig
    active activer allume allumer ouvre lance affiche réactive reactive réactiver reactiver
    activa activar enciende encender abre inicia muestra reactiva reactivar
    attiva attivare accendi accendere apri avvia mostra riattiva riattivare
    ativa ative ativar liga ligue ligar abra inicie mostre reativa reative reativar
""".split())
_OFF_VERBS188 = frozenset("""
    выключи выключить выключите отключи отключить закрой закрыть останови остановить скрой спрячь заглуши замуть замьють
    disable deactivate stop close hide mute pause
    deaktiviere deaktivieren ausschalten schließe schliesse stoppe beende verstecke
    désactive desactive désactiver desactiver coupe couper éteins eteins ferme arrête arrete masque
    desactiva desactivar apaga apagar cierra detén deten silencia silenciar oculta
    disattiva disattivare spegni spegnere chiudi ferma silenzia silenziare nascondi
    desativa desative desativar desliga desligue desligar fecha feche esconde
""".split())


def polarity_flip188(original: str, repaired: str) -> bool:
    """True when the repaired request swapped an on-verb for an off-verb (or back)."""
    def words(value):
        return [w.strip(' ,.!?;:«»"').casefold() for w in str(value or '').split()]
    before, after = words(original), words(repaired)
    on_before, off_before = any(w in _ON_VERBS188 for w in before), any(w in _OFF_VERBS188 for w in before)
    on_after, off_after = any(w in _ON_VERBS188 for w in after), any(w in _OFF_VERBS188 for w in after)
    return (on_before and not off_before and off_after and not on_after) or \
        (off_before and not on_before and on_after and not off_after)
