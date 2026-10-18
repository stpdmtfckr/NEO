"""NEO 168: keyboard and mouse input by voice — typing text, pressing a key, scrolling.

Windows ``SendInput`` is used directly, exactly like an on-screen keyboard:

* text is typed character by character as Unicode key events, so it works in
  fields that ignore Ctrl+V and never touches the clipboard;
* a spoken key («пробел», «Enter», «Ctrl+S») is pressed once or a few times in
  the active window;
* scrolling sends wheel notches to the foreground window.

Spoken key names, small repeat counts («3 раза») and ordinal words («первую»,
«third», «dritte») are parsed here for all seven languages. The parsing half is
pure and unit-tested; the Win32 half only runs on Windows.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
import re
import time
import unicodedata

# ----------------------------------------------------------------- key table
# name -> (scan code, extended?)  Scan code set 1.
_SCAN = {
    'esc': (0x01, False), '1': (0x02, False), '2': (0x03, False), '3': (0x04, False), '4': (0x05, False),
    '5': (0x06, False), '6': (0x07, False), '7': (0x08, False), '8': (0x09, False), '9': (0x0A, False),
    '0': (0x0B, False), '-': (0x0C, False), '=': (0x0D, False), 'backspace': (0x0E, False), 'tab': (0x0F, False),
    'q': (0x10, False), 'w': (0x11, False), 'e': (0x12, False), 'r': (0x13, False), 't': (0x14, False),
    'y': (0x15, False), 'u': (0x16, False), 'i': (0x17, False), 'o': (0x18, False), 'p': (0x19, False),
    '[': (0x1A, False), ']': (0x1B, False), 'enter': (0x1C, False), 'ctrl': (0x1D, False),
    'a': (0x1E, False), 's': (0x1F, False), 'd': (0x20, False), 'f': (0x21, False), 'g': (0x22, False),
    'h': (0x23, False), 'j': (0x24, False), 'k': (0x25, False), 'l': (0x26, False), ';': (0x27, False),
    "'": (0x28, False), '`': (0x29, False), 'shift': (0x2A, False), '\\': (0x2B, False),
    'z': (0x2C, False), 'x': (0x2D, False), 'c': (0x2E, False), 'v': (0x2F, False), 'b': (0x30, False),
    'n': (0x31, False), 'm': (0x32, False), ',': (0x33, False), '.': (0x34, False), '/': (0x35, False),
    'alt': (0x38, False), 'space': (0x39, False), 'capslock': (0x3A, False),
    'f1': (0x3B, False), 'f2': (0x3C, False), 'f3': (0x3D, False), 'f4': (0x3E, False), 'f5': (0x3F, False),
    'f6': (0x40, False), 'f7': (0x41, False), 'f8': (0x42, False), 'f9': (0x43, False), 'f10': (0x44, False),
    'f11': (0x57, False), 'f12': (0x58, False),
    'home': (0x47, True), 'up': (0x48, True), 'pageup': (0x49, True), 'left': (0x4B, True), 'right': (0x4D, True),
    'end': (0x4F, True), 'down': (0x50, True), 'pagedown': (0x51, True), 'insert': (0x52, True), 'delete': (0x53, True),
    'win': (0x5B, True), 'printscreen': (0x37, True),
}
MODIFIERS = frozenset({'ctrl', 'shift', 'alt', 'win'})

# Spoken / written names of keys -> canonical names in _SCAN.
_KEY_WORDS = {
    # English
    'space': 'space', 'spacebar': 'space', 'space bar': 'space', 'enter': 'enter', 'return': 'enter',
    'escape': 'esc', 'esc': 'esc', 'tab': 'tab', 'shift': 'shift', 'ctrl': 'ctrl', 'control': 'ctrl',
    'alt': 'alt', 'backspace': 'backspace', 'back space': 'backspace', 'delete': 'delete', 'del': 'delete',
    'insert': 'insert', 'home': 'home', 'end': 'end', 'page up': 'pageup', 'pageup': 'pageup',
    'page down': 'pagedown', 'pagedown': 'pagedown', 'up': 'up', 'down': 'down', 'left': 'left', 'right': 'right',
    'arrow up': 'up', 'up arrow': 'up', 'arrow down': 'down', 'down arrow': 'down', 'arrow left': 'left',
    'left arrow': 'left', 'arrow right': 'right', 'right arrow': 'right', 'caps lock': 'capslock',
    'capslock': 'capslock', 'windows': 'win', 'win': 'win', 'print screen': 'printscreen',
    'double u': 'w', 'double you': 'w',
    # Russian
    'пробел': 'space', 'пробелы': 'space', 'энтер': 'enter', 'ентер': 'enter', 'интер': 'enter', 'ввод': 'enter',
    'эскейп': 'esc', 'эскейпа': 'esc', 'эск': 'esc', 'таб': 'tab', 'табуляция': 'tab', 'табуляцию': 'tab',
    'шифт': 'shift', 'контрол': 'ctrl', 'контрл': 'ctrl', 'ктрл': 'ctrl', 'альт': 'alt', 'бэкспейс': 'backspace',
    'бекспейс': 'backspace', 'делит': 'delete', 'делете': 'delete', 'инсерт': 'insert', 'хоум': 'home',
    'энд': 'end', 'пейдж ап': 'pageup', 'пейдж даун': 'pagedown', 'капслок': 'capslock', 'капс лок': 'capslock',
    'вверх': 'up', 'вниз': 'down', 'влево': 'left', 'вправо': 'right',
    'стрелка вверх': 'up', 'стрелку вверх': 'up', 'стрелка вниз': 'down', 'стрелку вниз': 'down',
    'стрелка влево': 'left', 'стрелку влево': 'left', 'стрелка вправо': 'right', 'стрелку вправо': 'right',
    'виндовс': 'win', 'вин': 'win',
    # German
    'leertaste': 'space', 'leerzeichen': 'space', 'eingabe': 'enter', 'eingabetaste': 'enter', 'umschalt': 'shift',
    'umschalttaste': 'shift', 'strg': 'ctrl', 'steuerung': 'ctrl', 'rücktaste': 'backspace', 'entfernen': 'delete',
    'entf': 'delete', 'einfügen': 'insert', 'pos1': 'home', 'ende': 'end', 'bild hoch': 'pageup',
    'bild runter': 'pagedown', 'pfeil hoch': 'up', 'pfeil nach oben': 'up', 'pfeil runter': 'down',
    'pfeil nach unten': 'down', 'pfeil links': 'left', 'pfeil rechts': 'right', 'tabulator': 'tab', 'feststelltaste': 'capslock',
    # French
    'espace': 'space', 'barre d’espace': 'space', "barre d'espace": 'space', 'entrée': 'enter', 'entree': 'enter',
    'échap': 'esc', 'echap': 'esc', 'échappement': 'esc', 'tabulation': 'tab', 'maj': 'shift', 'majuscule': 'shift',
    'retour arrière': 'backspace', 'suppr': 'delete', 'supprimer': 'delete', 'inser': 'insert', 'début': 'home',
    'fin': 'end', 'page précédente': 'pageup', 'page suivante': 'pagedown', 'flèche haut': 'up', 'flèche du haut': 'up',
    'flèche bas': 'down', 'flèche du bas': 'down', 'flèche gauche': 'left', 'flèche droite': 'right', 'verr maj': 'capslock',
    # Spanish
    'espacio': 'space', 'barra espaciadora': 'space', 'intro': 'enter', 'tabulador': 'tab', 'mayúsculas': 'shift',
    'mayus': 'shift', 'mayús': 'shift', 'control izquierdo': 'ctrl', 'retroceso': 'backspace', 'suprimir': 'delete',
    'insertar': 'insert', 'inicio': 'home', 'fin de línea': 'end', 're pág': 'pageup', 'av pág': 'pagedown',
    'flecha arriba': 'up', 'flecha abajo': 'down', 'flecha izquierda': 'left', 'flecha derecha': 'right', 'bloq mayús': 'capslock',
    # Italian
    'spazio': 'space', 'barra spaziatrice': 'space', 'invio': 'enter', 'maiusc': 'shift', 'maiuscolo': 'shift',
    'canc': 'delete', 'cancella': 'delete', 'ins': 'insert', 'fine': 'end', 'pag su': 'pageup', 'pag giù': 'pagedown',
    'freccia su': 'up', 'freccia giù': 'down', 'freccia sinistra': 'left', 'freccia destra': 'right', 'bloc maiusc': 'capslock',
    # Portuguese
    'espaço': 'space', 'barra de espaço': 'space', 'retrocesso': 'backspace', 'apagar': 'delete', 'inserir': 'insert',
    'seta para cima': 'up', 'seta cima': 'up', 'seta para baixo': 'down', 'seta baixo': 'down',
    'seta para a esquerda': 'left', 'seta esquerda': 'left', 'seta para a direita': 'right', 'seta direita': 'right',
    'maiúsculas': 'shift',
}
# How Russian speakers name Latin letters (and single Cyrillic letters Whisper writes).
_RU_LETTERS = {
    'а': 'a', 'эй': 'a', 'бэ': 'b', 'би': 'b', 'б': 'b', 'цэ': 'c', 'си': 'c', 'ц': 'c', 'дэ': 'd', 'ди': 'd', 'д': 'd',
    'е': 'e', 'э': 'e', 'и': 'e', 'эф': 'f', 'ф': 'f', 'гэ': 'g', 'джи': 'g', 'г': 'g', 'аш': 'h', 'эйч': 'h', 'х': 'h',
    'ай': 'i', 'джей': 'j', 'жи': 'j', 'ка': 'k', 'кей': 'k', 'кэй': 'k', 'к': 'k', 'эл': 'l', 'эль': 'l', 'л': 'l',
    'эм': 'm', 'м': 'm', 'эн': 'n', 'н': 'n', 'о': 'o', 'оу': 'o', 'пэ': 'p', 'пи': 'p', 'п': 'p', 'ку': 'q', 'кью': 'q',
    'эр': 'r', 'ар': 'r', 'р': 'r', 'эс': 's', 'с': 's', 'тэ': 't', 'ти': 't', 'т': 't', 'у': 'u', 'ю': 'u',
    'вэ': 'v', 'ви': 'v', 'в': 'v', 'дабл ю': 'w', 'дабл-ю': 'w', 'дабл в': 'w', 'икс': 'x', 'экс': 'x',
    'игрек': 'y', 'уай': 'y', 'зэт': 'z', 'зет': 'z', 'зед': 'z', 'з': 'z',
}
_NUMBER_WORDS = {
    'ru': ('ноль', 'один', 'два', 'три', 'четыре', 'пять', 'шесть', 'семь', 'восемь', 'девять', 'десять', 'одиннадцать', 'двенадцать'),
    'en': ('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine', 'ten', 'eleven', 'twelve'),
    'de': ('null', 'eins', 'zwei', 'drei', 'vier', 'fünf', 'sechs', 'sieben', 'acht', 'neun', 'zehn', 'elf', 'zwölf'),
    'fr': ('zéro', 'un', 'deux', 'trois', 'quatre', 'cinq', 'six', 'sept', 'huit', 'neuf', 'dix', 'onze', 'douze'),
    'es': ('cero', 'uno', 'dos', 'tres', 'cuatro', 'cinco', 'seis', 'siete', 'ocho', 'nueve', 'diez', 'once', 'doce'),
    'it': ('zero', 'uno', 'due', 'tre', 'quattro', 'cinque', 'sei', 'sette', 'otto', 'nove', 'dieci', 'undici', 'dodici'),
    'pt': ('zero', 'um', 'dois', 'três', 'quatro', 'cinco', 'seis', 'sete', 'oito', 'nove', 'dez', 'onze', 'doze'),
}
_KEY_FILLERS = {'клавишу', 'клавиша', 'кнопку', 'кнопка', 'key', 'the', 'button', 'taste', 'die', 'la', 'touche',
                'tecla', 'il', 'tasto', 'на', 'on'}


def _norm(text: str) -> str:
    text = unicodedata.normalize('NFC', str(text or '')).casefold().replace('ё', 'е')
    text = text.replace('«', ' ').replace('»', ' ').replace('"', ' ').replace('“', ' ').replace('”', ' ')
    return ' '.join(text.strip(' .,!?:;').split())


def _small_number(token: str, code: str) -> int | None:
    token = token.strip()
    if token.isdigit():
        return int(token)
    for words in ([_NUMBER_WORDS.get(code, ())] + [w for c, w in _NUMBER_WORDS.items() if c != code]):
        if token in words:
            return words.index(token)
    extra = {'одну': 1, 'одна': 1, 'две': 2, 'uma': 1, 'una': 1, 'une': 1, 'ein': 1, 'eine': 1, 'duas': 2}
    return extra.get(token)


@dataclass(frozen=True)
class KeySpec:
    keys: tuple[str, ...]          # canonical names; modifiers first
    count: int = 1
    hold_seconds: float = 0.0

    @property
    def label(self) -> str:
        pretty = {'space': 'Space', 'enter': 'Enter', 'esc': 'Esc', 'ctrl': 'Ctrl', 'shift': 'Shift', 'alt': 'Alt',
                  'win': 'Win', 'up': '↑', 'down': '↓', 'left': '←', 'right': '→', 'pageup': 'PageUp',
                  'pagedown': 'PageDown', 'backspace': 'Backspace', 'delete': 'Delete', 'tab': 'Tab'}
        return '+'.join(pretty.get(k, k.upper()) for k in self.keys)


_COUNT_RE = re.compile(
    r'\s+(?:(\d+|[a-zà-ÿа-я]+)\s+(?:раз|раза|times|mal|fois|veces|vez|volte|volta|vezes)'
    r'|(дважды|трижды|twice|thrice|zweimal|dreimal|deux fois|trois fois|dos veces|tres veces|due volte|tre volte|duas vezes|três vezes))$')
_HOLD_RE = re.compile(
    r'\s+(?:на|for|für|pendant|durante|per|por)?\s*(\d+(?:[.,]\d+)?|[a-zà-ÿа-я]+)\s*'
    r'(?:секунд[ыуа]?|сек|s|sec|secs|seconds?|sekunden?|secondes?|segundos?|secondi|secondo)$')
_MULTI = {'дважды': 2, 'twice': 2, 'zweimal': 2, 'deux fois': 2, 'dos veces': 2, 'due volte': 2, 'duas vezes': 2,
          'трижды': 3, 'thrice': 3, 'dreimal': 3, 'trois fois': 3, 'tres veces': 3, 'tre volte': 3, 'três vezes': 3}


def _one_key(phrase: str, code: str) -> str | None:
    phrase = phrase.strip()
    if not phrase:
        return None
    if phrase in _KEY_WORDS:
        return _KEY_WORDS[phrase]
    if phrase in _SCAN:
        return phrase
    if len(phrase) == 1 and phrase.isascii() and phrase.isalnum():
        return phrase
    if code == 'ru' or re.search('[а-я]', phrase):
        if phrase in _RU_LETTERS:
            return _RU_LETTERS[phrase]
    match = re.fullmatch(r'(?:f|ф|эф)\s*-?\s*(\d{1,2}|[a-zà-ÿа-я]+)', phrase)
    if match:
        number = _small_number(match.group(1), code)
        if number is not None and 1 <= number <= 12:
            return f'f{number}'
    number = _small_number(phrase, code)
    if number is not None and 0 <= number <= 9:
        return str(number)
    return None


def parse_key_spec(text: str, code: str = 'ru') -> KeySpec | None:
    """«пробел», «E 3 раза», «ctrl+s», «контрол эс», «W на 2 секунды», «F5» -> KeySpec; else None."""
    phrase = _norm(text)
    if not phrase:
        return None
    count, hold = 1, 0.0
    match = _COUNT_RE.search(' ' + phrase)
    if match:
        if match.group(2):
            count = _MULTI.get(match.group(2), 1)
        else:
            count = _small_number(match.group(1), code) or 0
            if count <= 0:
                return None
        phrase = phrase[: len(phrase) - len(match.group(0)) + 1].strip()
    match = _HOLD_RE.search(' ' + phrase)
    if match:
        value = match.group(1).replace(',', '.')
        try:
            hold = float(value)
        except ValueError:
            hold = float(_small_number(value, code) or 0)
        if hold <= 0:
            return None
        phrase = phrase[: len(phrase) - len(match.group(0)) + 1].strip()
    words = [w for w in re.split(r'\s*\+\s*|\s+(?:плюс|plus|und|et|y|e|и|and)\s+|\s+', phrase) if w]
    words = [w for w in words if w not in _KEY_FILLERS] or words
    keys: list[str] = []
    i = 0
    while i < len(words):
        for size in (3, 2, 1):
            chunk = ' '.join(words[i:i + size])
            if size > 1 and i + size > len(words):
                continue
            key = _one_key(chunk, code)
            if key is not None:
                keys.append(key)
                i += size
                break
        else:
            return None
    if not keys or len(keys) > 4:
        return None
    mods = [k for k in keys if k in MODIFIERS]
    rest = [k for k in keys if k not in MODIFIERS]
    if len(rest) > 1:
        return None
    ordered = tuple(dict.fromkeys(mods + rest))
    if hold:
        return None  # timed holds are not supported: one press or a few presses only
    return KeySpec(ordered, max(1, min(10, count)), 0.0)


def requests_timed_hold(text: str) -> bool:
    """NEO 187: «пробел на 10 секунд» names a key *hold*. Holds are not offered,
    so the caller must say so instead of searching for a button with that name."""
    return bool(_HOLD_RE.search(' ' + str(text or '').casefold()))


# ------------------------------------------------------------------ ordinals
_ORDINALS = {
    'ru': [('перв', 1), ('втор', 2), ('трет', 3), ('четверт', 4), ('пят', 5), ('шест', 6), ('седьм', 7),
           ('восьм', 8), ('девят', 9), ('десят', 10), ('последн', -1), ('предпоследн', -2)],
    'en': [('first', 1), ('second', 2), ('third', 3), ('fourth', 4), ('fifth', 5), ('sixth', 6), ('seventh', 7),
           ('eighth', 8), ('ninth', 9), ('tenth', 10), ('last', -1)],
    'de': [('erst', 1), ('zweit', 2), ('dritt', 3), ('viert', 4), ('fünft', 5), ('sechst', 6), ('siebt', 7),
           ('acht', 8), ('neunt', 9), ('zehnt', 10), ('letzt', -1)],
    'fr': [('premi', 1), ('deuxième', 2), ('second', 2), ('troisième', 3), ('quatrième', 4), ('cinquième', 5),
           ('sixième', 6), ('septième', 7), ('huitième', 8), ('neuvième', 9), ('dixième', 10), ('derni', -1)],
    'es': [('primer', 1), ('segund', 2), ('tercer', 3), ('cuart', 4), ('quint', 5), ('sext', 6), ('séptim', 7),
           ('septim', 7), ('octav', 8), ('noven', 9), ('décim', 10), ('decim', 10), ('últim', -1), ('ultim', -1)],
    'it': [('prim', 1), ('second', 2), ('terz', 3), ('quart', 4), ('quint', 5), ('sest', 6), ('settim', 7),
           ('ottav', 8), ('non', 9), ('decim', 10), ('ultim', -1)],
    'pt': [('primeir', 1), ('segund', 2), ('terceir', 3), ('quart', 4), ('quint', 5), ('sext', 6), ('sétim', 7),
           ('setim', 7), ('oitav', 8), ('non', 9), ('décim', 10), ('decim', 10), ('últim', -1), ('ultim', -1)],
}


def parse_ordinal(text: str, code: str = 'ru') -> int | None:
    """«первую», «3-ю», «номер 5», «third», «dritte», «última» -> 1-based index (-1 = last)."""
    phrase = _norm(text)
    phrase = re.sub(r'^(?:номер|number|nummer|numéro|numero|número|no\.?|nr\.?|№|n°)(?=\s|\d|$)\s*', '', phrase)
    match = re.fullmatch(r'(\d{1,3})(?:\s*-?\s*(?:[а-я]{1,3}|st|nd|rd|th|er|e|ème|º|ª|o|a))?', phrase)
    if match:
        value = int(match.group(1))
        return value if value >= 1 else None
    tokens = phrase.split()
    for token in tokens[:2]:
        for lang in [code] + [c for c in _ORDINALS if c != code]:
            for stem, value in _ORDINALS[lang]:
                if token.startswith(stem) and (len(token) - len(stem) <= 5):
                    # «non» (it) must be the whole ordinal stem «nono/nona», not «non».
                    if stem == 'non' and not re.fullmatch(r'non[oa]', token):
                        continue
                    return value
    try:
        from jarvis.runtime.spoken_numbers import parse_spoken_number
        number = parse_spoken_number(phrase, code)
        if number is not None and float(number).is_integer() and 1 <= int(number) <= 999:
            return int(number)
    except Exception:
        pass
    return None


# ----------------------------------------------------------------- Win32 side
@lru_cache(maxsize=1)
def _ctypes():
    import ctypes
    from ctypes import wintypes

    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD),
                    ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                    ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD), ('wParamH', wintypes.WORD)]

    class _U(ctypes.Union):
        _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [('type', wintypes.DWORD), ('u', _U)]

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT
    return ctypes, INPUT, KEYBDINPUT, MOUSEINPUT, user32


_KEYEVENTF_EXTENDEDKEY, _KEYEVENTF_KEYUP, _KEYEVENTF_UNICODE, _KEYEVENTF_SCANCODE = 0x1, 0x2, 0x4, 0x8
_VK_RETURN, _VK_TAB = 0x0D, 0x09


def _require_windows() -> None:
    if os.name != 'nt':
        raise OSError('Keyboard and mouse input is available on Windows only')


def _send(events) -> None:
    ctypes, INPUT, _K, _M, user32 = _ctypes()
    array = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise OSError(ctypes.get_last_error(), 'SendInput was blocked (is the target window elevated?)')


def _key_event(scan: int, extended: bool, up: bool):
    _c, INPUT, KEYBDINPUT, _M, _u = _ctypes()
    flags = _KEYEVENTF_SCANCODE | (_KEYEVENTF_EXTENDEDKEY if extended else 0) | (_KEYEVENTF_KEYUP if up else 0)
    event = INPUT(type=1)
    event.u.ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return event


def press(spec: KeySpec, *, gap: float = 0.06) -> None:
    """Press a key (or combo) ``count`` times; hold it for ``hold_seconds`` if set."""
    _require_windows()
    codes = [_SCAN[k] for k in spec.keys]
    hold = spec.hold_seconds if spec.hold_seconds > 0 else 0.045
    for index in range(spec.count):
        try:
            _send([_key_event(scan, ext, False) for scan, ext in codes])
            time.sleep(hold)
        finally:
            # NEO 187 (PRESS_EXCEPTION_CLEANUP): whatever happened after key-down,
            # the same keys are released in reverse order; never leave one held.
            _send([_key_event(scan, ext, True) for scan, ext in reversed(codes)])
        if index + 1 < spec.count:
            time.sleep(gap)


def type_text(text: str, *, delay: float = 0.015, submit: bool = False) -> int:
    """Type ``text`` as real Unicode keystrokes (no clipboard). Returns characters typed."""
    _require_windows()
    _c, INPUT, KEYBDINPUT, _M, _u = _ctypes()

    def vk(code: int):
        down, up = INPUT(type=1), INPUT(type=1)
        down.u.ki = KEYBDINPUT(wVk=code, wScan=0, dwFlags=0, time=0, dwExtraInfo=0)
        up.u.ki = KEYBDINPUT(wVk=code, wScan=0, dwFlags=_KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)
        return [down, up]

    typed = 0
    for char in str(text or '').replace('\r\n', '\n'):
        if char == '\n':
            events = vk(_VK_RETURN)
        elif char == '\t':
            events = vk(_VK_TAB)
        else:
            events = []
            data = char.encode('utf-16-le')
            for i in range(0, len(data), 2):
                unit = int.from_bytes(data[i:i + 2], 'little')
                down, up = INPUT(type=1), INPUT(type=1)
                down.u.ki = KEYBDINPUT(wVk=0, wScan=unit, dwFlags=_KEYEVENTF_UNICODE, time=0, dwExtraInfo=0)
                up.u.ki = KEYBDINPUT(wVk=0, wScan=unit, dwFlags=_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)
                events += [down, up]
        _send(events)
        typed += 1
        if delay > 0:
            time.sleep(delay)
    if submit:
        time.sleep(0.05)
        _send(vk(_VK_RETURN))
    return typed


def scroll(notches: int) -> None:
    """Mouse wheel on the foreground window: positive = up, negative = down."""
    _require_windows()
    ctypes, INPUT, _K, MOUSEINPUT, user32 = _ctypes()
    from ctypes import wintypes
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    hwnd = user32.GetForegroundWindow()
    rect, cursor = wintypes.RECT(), wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(cursor))
    moved = False
    if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        inside = rect.left <= cursor.x < rect.right and rect.top <= cursor.y < rect.bottom
        if not inside:
            # The wheel goes to the window under the cursor: aim at the foreground window.
            user32.SetCursorPos((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
            moved = True
    step = 1 if notches > 0 else -1
    for _ in range(abs(int(notches))):
        event = INPUT(type=0)
        event.u.mi = MOUSEINPUT(dx=0, dy=0, mouseData=ctypes.c_uint32(120 * step).value, dwFlags=0x0800, time=0, dwExtraInfo=0)
        _send([event])
        time.sleep(0.012)
    if moved:
        time.sleep(0.05)
        user32.SetCursorPos(cursor.x, cursor.y)


def click_at(x: int, y: int, *, button: str = 'left', double: bool = False) -> None:
    """NEO 171: a real mouse click at screen pixel (x, y) — used for text found by OCR (games, custom UIs)."""
    _require_windows()
    ctypes, INPUT, _K, MOUSEINPUT, user32 = _ctypes()
    user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    down, up = {'left': (0x0002, 0x0004), 'right': (0x0008, 0x0010)}.get(button, (0x0002, 0x0004))
    for _ in range(2 if double else 1):
        events = []
        for flag in (down, up):
            event = INPUT(type=0)
            event.u.mi = MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag, time=0, dwExtraInfo=0)
            events.append(event)
        _send(events)
        time.sleep(0.05)
