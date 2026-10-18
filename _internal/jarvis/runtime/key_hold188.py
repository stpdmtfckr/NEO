"""NEO 188 atlas (proposed.input.*): holding keys is deliberately not implemented.

«зажми F5» used to become a single F5 press, «hold F5 for 5 minutes» offered to
pause the music. Holding keys stays out of scope, so these requests get one
honest answer and no input event at all; «нажми F5» keeps working as before.
"""
from __future__ import annotations

from functools import lru_cache
import re

_KEY_WORDS = (r"f(?:[1-9]|1[0-9]|2[0-4])|[a-zа-я0-9]|ctrl|control|strg|shift|alt|altgr|space|spacebar|enter|return|tab|esc|escape|"
              r"win|windows|backspace|delete|del|insert|home|end|pageup|pagedown|up|down|left|right|пробел|шифт|контрол|альт|"
              r"ввод|энтер|таб|эскейп|бэкспейс|leertaste|umschalt|eingabe|espace|entrée|entree|maj|espacio|intro|mayús|mayus|"
              r"spazio|invio|maiusc|espaço|espaco")
_KEY = rf"[«\"“]?(?:{_KEY_WORDS})(?:\s*\+\s*(?:{_KEY_WORDS}))*[»\"”]?"
_NOUN = (r"(?:(?:the\s+)?(?:клавиш[уиа]|кнопк[уи]|key|die\s+taste|taste|la\s+touche|touche|la\s+tecla|tecla|il\s+tasto|tasto|"
         r"a\s+tecla)\s+)?")
_HOLD = {
    'ru': (rf"(?:зажми|удерживай|удержи|зажать|удерживать)\s+{_NOUN}{_KEY}(?:\s+(?:на|в\s+течение)\s+.+)?",),
    'en': (rf"(?:hold(?:\s+down)?|press\s+and\s+hold)\s+(?:the\s+)?{_NOUN}{_KEY}(?:\s+key)?(?:\s+(?:down|for\s+.+))?",
           rf"keep\s+(?:the\s+)?{_NOUN}{_KEY}(?:\s+key)?\s+(?:pressed|held|down)(?:\s+for\s+.+)?",
           rf"(?:press|push)\s+(?:the\s+)?{_NOUN}{_KEY}\s+for\s+.+"),
    'de': (rf"halte\s+{_NOUN}{_KEY}(?:\s+für\s+.+?)?\s+(?:gedrückt|fest)", rf"drücke\s+{_NOUN}{_KEY}\s+für\s+.+"),
    'fr': (rf"(?:maintiens|garde)\s+{_NOUN}{_KEY}(?:\s+(?:enfoncée|enfoncee|appuyée|appuyee))?(?:\s+pendant\s+.+)?",
           rf"appuie\s+sur\s+{_NOUN}{_KEY}\s+pendant\s+.+"),
    'es': (rf"mantén\s+(?:pulsada\s+|presionada\s+)?{_NOUN}{_KEY}(?:\s+(?:pulsada|presionada))?(?:\s+durante\s+.+)?",
           rf"(?:presiona|pulsa)\s+{_NOUN}{_KEY}\s+durante\s+.+"),
    'it': (rf"(?:tieni|mantieni)\s+(?:premuto\s+)?{_NOUN}{_KEY}(?:\s+premuto)?(?:\s+per\s+.+)?", rf"premi\s+{_NOUN}{_KEY}\s+per\s+.+"),
    'pt': (rf"(?:mantém|mantem|mantenha|segura|segure)\s+{_NOUN}{_KEY}(?:\s+(?:premida|pressionada))?(?:\s+(?:durante|por)\s+.+)?",
           rf"(?:pressiona|pressione|carrega)\s+{_NOUN}{_KEY}\s+(?:durante|por)\s+.+"),
}
_RELEASE = {
    'ru': (rf"(?:разожми|отпусти)\s+{_NOUN}{_KEY}", rf"перестань\s+(?:удерживать|держать)\s+{_NOUN}{_KEY}",
           r"(?:разожми|отпусти)\s+(?:все|всё)(?:\s+зажатые)?\s+(?:клавиши|кнопки)", r"перестань\s+(?:удерживать|держать)\s+(?:все\s+)?клавиши"),
    'en': (rf"release\s+(?:the\s+)?{_NOUN}{_KEY}(?:\s+key)?", rf"stop\s+holding\s+(?:the\s+)?{_NOUN}{_KEY}(?:\s+key)?",
           r"release\s+all(?:\s+held)?\s+keys", r"stop\s+holding\s+(?:any|all|the)\s+keys", r"let\s+go\s+of\s+all\s+keys"),
    'de': (rf"lass\s+{_NOUN}{_KEY}\s+los", rf"halte\s+{_NOUN}{_KEY}\s+nicht\s+mehr\s+gedrückt",
           r"lass\s+alle(?:\s+gehaltenen)?\s+tasten\s+los", r"halte\s+keine\s+tasten\s+mehr\s+gedrückt"),
    'fr': (rf"relâche\s+{_NOUN}{_KEY}", rf"arrête\s+de\s+maintenir\s+{_NOUN}{_KEY}",
           r"relâche\s+toutes\s+les\s+touches(?:\s+maintenues)?", r"arrête\s+de\s+maintenir\s+les\s+touches"),
    'es': (rf"suelta\s+{_NOUN}{_KEY}", rf"deja\s+de\s+mantener\s+{_NOUN}{_KEY}(?:\s+pulsada)?",
           r"suelta\s+todas\s+las\s+teclas(?:\s+mantenidas)?", r"deja\s+de\s+mantener\s+las\s+teclas(?:\s+pulsadas)?"),
    'it': (rf"rilascia\s+{_NOUN}{_KEY}", rf"smetti\s+di\s+tenere\s+premuto\s+{_NOUN}{_KEY}",
           r"rilascia\s+tutti\s+i\s+tasti(?:\s+mantenuti)?", r"smetti\s+di\s+tenere\s+premuti\s+i\s+tasti"),
    'pt': (rf"solta\s+{_NOUN}{_KEY}", rf"deixe\s+de\s+manter\s+{_NOUN}{_KEY}(?:\s+pressionada)?",
           r"solta\s+todas\s+as\s+teclas(?:\s+mantidas)?", r"pare\s+de\s+manter\s+as\s+teclas(?:\s+pressionadas)?"),
}
_TEXT = {
    'hold': ('Удерживать клавиши NEO не умеет: эта функция намеренно не реализована, ничего не нажато. Однократное нажатие работает: скажите «нажми F5».',
             'NEO does not hold keys: this is intentionally not implemented, and nothing was pressed. A single press works: say “press F5”.',
             'NEO hält keine Tasten gedrückt: Das ist bewusst nicht umgesetzt, es wurde nichts gedrückt. Ein einzelner Druck geht: Sag „drücke F5“.',
             'NEO ne maintient pas de touches : ce n’est volontairement pas prévu, rien n’a été appuyé. Un appui simple fonctionne : dis « appuie sur F5 ».',
             'NEO no mantiene teclas pulsadas: no está implementado a propósito y no se pulsó nada. Una pulsación sí funciona: di «pulsa F5».',
             'NEO non tiene premuti i tasti: non è previsto di proposito e non è stato premuto nulla. Una singola pressione funziona: di’ «premi F5».',
             'O NEO não mantém teclas premidas: isto não foi implementado de propósito e nada foi premido. Um toque simples funciona: diz «carrega em F5».'),
    'release': ('NEO не удерживает клавиши, поэтому отпускать нечего. Если клавиша залипла, нажмите её ещё раз на клавиатуре.',
                'NEO never holds keys, so there is nothing to release. If a key seems stuck, press it once more on the keyboard.',
                'NEO hält nie Tasten gedrückt, es gibt also nichts loszulassen. Wenn eine Taste hängt, drücke sie noch einmal auf der Tastatur.',
                'NEO ne maintient jamais de touches, il n’y a donc rien à relâcher. Si une touche semble bloquée, appuie de nouveau dessus.',
                'NEO nunca mantiene teclas, así que no hay nada que soltar. Si una tecla parece atascada, púlsala otra vez en el teclado.',
                'NEO non tiene mai premuti i tasti, quindi non c’è nulla da rilasciare. Se un tasto sembra bloccato, premilo di nuovo sulla tastiera.',
                'O NEO nunca mantém teclas premidas, por isso não há nada para soltar. Se uma tecla parecer presa, carrega nela outra vez no teclado.'),
}
_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


@lru_cache(maxsize=7)
def _patterns(code: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    whole = lambda forms: re.compile(r"^(?:" + "|".join(forms) + r")$", re.I)
    return whole(_HOLD[code]), whole(_RELEASE[code])


def classify(text: str, code: str) -> str:
    """'hold', 'release' or '' for a whole request in the selected language."""
    from jarvis.runtime.natural_commands import strip_request_shell
    if code not in _HOLD:
        return ''
    value = re.sub(r"\s+", " ", strip_request_shell(str(text or ""), code).strip().strip(" .!?¡¿,"))
    if not value or len(value) > 80:
        return ''
    hold, release = _patterns(code)
    if hold.match(value):
        return 'hold'
    if release.match(value):
        return 'release'
    return ''


def reply(kind: str, code: str) -> str:
    return _TEXT[kind][_CODES.index(code if code in _CODES else 'en')]
