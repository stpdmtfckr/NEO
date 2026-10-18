"""NEO 171: find a named thing on the screen in ANY visible window, on any monitor.

Before 171 «нажми X» looked only at the one window under NEO. With Task Manager and a
game both open, NEO searched Task Manager, found nothing (it runs as administrator,
so Windows hides its contents from normal programs) and gave up — the game was never
searched, and games draw their own buttons, which UI Automation cannot see.

Now:
1. Candidate windows = every visible, normal window, in z-order (the window you worked
   in last comes first), with its process and monitor. A phrase can narrow them:
   «на втором мониторе», «на левом экране», «в игре», «в диспетчере задач», «в окне Steam».
2. In each window the name is looked up through UI Automation; where UI Automation
   sees no elements (games, custom-drawn interfaces), by reading the window's text
   with the Windows OCR engine and taking the word boxes.
3. Decision:
   * found in exactly one window → press it there;
   * found in several windows → ask which one (with monitors), or use the window NEO
     acted in just before;
   * not found → say where NEO looked; a window running as administrator is named
     explicitly, because Windows does not let other programs press inside it.
Nothing is pressed in a window that is covered by another one at that point.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import logging
import os
import re
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)
_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


@dataclass(frozen=True)
class Monitor:
    index: int                       # 1..N, numbered left to right
    rect: tuple[int, int, int, int]
    primary: bool = False


@dataclass
class Window:
    hwnd: int
    title: str
    process: str
    pid: int
    rect: tuple[int, int, int, int]
    monitor: int = 1
    z: int = 0                       # 0 = top of the z-order
    protected: bool = False          # runs as administrator while NEO does not
    game: bool = False

    def label(self) -> str:
        name = self.title or self.process
        return name if len(name) <= 48 else name[:45] + '…'


@dataclass
class Hit:
    window: Window
    name: str
    rect: tuple[int, int, int, int]
    method: str                      # uia | ocr
    score: float = 1.0
    element: Any = None


@dataclass
class Scope:
    monitor: int | None = None
    side: str = ''                   # left | right
    primary: bool = False
    game: bool = False
    app: str = ''

    def empty(self) -> bool:
        return self.monitor is None and not self.side and not self.primary and not self.game and not self.app


@dataclass
class Search:
    hits: list[Hit] = field(default_factory=list)
    searched: list[Window] = field(default_factory=list)
    protected: list[Window] = field(default_factory=list)
    monitors: int = 1


# ------------------------------------------------------------------ phrases
_MONITOR_WORDS = r'(?:монитор\w*|экран\w*|display|monitor|screen|bildschirm\w*|écran|pantalla|schermo|ecrã|tela)'
_SIDES = {'left': r'(?:лев\w+|left|linke\w*|gauche|izquierd\w+|sinistr\w+|esquerd\w+)',
          'right': r'(?:прав\w+|right|rechte\w*|droit\w*|derech\w+|destr\w+|direit\w+)'}
_PRIMARY = r'(?:основн\w+|главн\w+|main|primary|haupt\w*|principal\w*|principale)'
_PREP = r'(?:на|в|on|in|the|auf|dem|sur|l\'|le|la|en|el|sul|nel|no|na|do|da)'
_GAME = {'ru': r'в\s+игр[еу]', 'en': r'in\s+(?:the\s+)?game', 'de': r'im\s+spiel', 'fr': r'dans\s+le\s+jeu',
         'es': r'en\s+el\s+juego', 'it': r'nel\s+gioco', 'pt': r'no\s+jogo'}
# Well-known apps -> process names.
APPS = {
    'диспетчер задач': ('taskmgr.exe',), 'диспетчере задач': ('taskmgr.exe',), 'task manager': ('taskmgr.exe',),
    'task-manager': ('taskmgr.exe',), 'gestionnaire des tâches': ('taskmgr.exe',), 'administrador de tareas': ('taskmgr.exe',),
    'браузер': ('chrome.exe', 'msedge.exe', 'firefox.exe', 'opera.exe', 'browser.exe', 'brave.exe', 'yandex.exe'),
    'браузере': ('chrome.exe', 'msedge.exe', 'firefox.exe', 'opera.exe', 'browser.exe', 'brave.exe', 'yandex.exe'),
    'browser': ('chrome.exe', 'msedge.exe', 'firefox.exe', 'opera.exe', 'browser.exe', 'brave.exe'),
    'дискорд': ('discord.exe',), 'дискорде': ('discord.exe',), 'discord': ('discord.exe',),
    'стим': ('steam.exe', 'steamwebhelper.exe'), 'стиме': ('steam.exe', 'steamwebhelper.exe'), 'steam': ('steam.exe', 'steamwebhelper.exe'),
    'телеграм': ('telegram.exe',), 'телеграме': ('telegram.exe',), 'telegram': ('telegram.exe',),
    'проводник': ('explorer.exe',), 'проводнике': ('explorer.exe',), 'explorer': ('explorer.exe',),
    'блокнот': ('notepad.exe',), 'блокноте': ('notepad.exe',), 'notepad': ('notepad.exe',),
}
_APP_PHRASE = {
    'ru': r'в\s+(?:окне|приложении|программе)\s+(?P<app>.+)|в\s+(?P<known>' + '|'.join(re.escape(k) for k in APPS if re.search('[а-я]', k)) + r')',
    'en': r'in\s+(?:the\s+)?(?:(?P<app2>.+?)\s+(?:window|app)|(?:window|app)\s+(?P<app>.+)|(?P<known>task manager|the browser|browser|discord|steam|telegram|explorer|notepad))',
    'de': r'im\s+(?:fenster|programm)\s+(?P<app>.+)|im\s+(?P<known>task manager|browser)',
    'fr': r'dans\s+(?:la\s+fenêtre|l\'application)\s+(?P<app>.+)|dans\s+le\s+(?P<known>gestionnaire des tâches|navigateur)',
    'es': r'en\s+(?:la\s+ventana|la\s+aplicación)\s+(?P<app>.+)|en\s+el\s+(?P<known>administrador de tareas|navegador)',
    'it': r'nella\s+finestra\s+(?P<app>.+)|nel\s+(?P<known>browser)',
    'pt': r'na\s+janela\s+(?P<app>.+)|no\s+(?P<known>navegador)',
}


def parse_scope(text: str, code: str) -> tuple[str, Scope]:
    """«Продолжить на втором мониторе» -> («Продолжить», Scope(monitor=2)). Only a trailing place phrase is taken."""
    from jarvis.runtime.input168 import parse_ordinal
    value = ' '.join(str(text or '').split())
    scope = Scope()
    changed = True
    while changed:
        changed = False
        found = re.search(rf'(?:\s+{_PREP})+\s+(?P<word>\S+)\s+{_MONITOR_WORDS}\s*$', value, re.I) or \
            re.search(rf'(?:\s+{_PREP})+\s+{_MONITOR_WORDS}\s+(?P<word>\S+)\s*$', value, re.I)
        if found:
            word = found.group('word')
            if re.fullmatch(_SIDES['left'], word, re.I):
                scope.side = 'left'
            elif re.fullmatch(_SIDES['right'], word, re.I):
                scope.side = 'right'
            elif re.fullmatch(_PRIMARY, word, re.I):
                scope.primary = True
            else:
                number = parse_ordinal(word, code)
                if number is None and word.isdigit():
                    number = int(word)
                if number is None:
                    break
                scope.monitor = number
            value = value[:found.start()].strip()
            changed = True
            continue
        game = re.search(r'\s+' + _GAME.get(code, _GAME['en']) + r'\s*$', value, re.I)
        if game:
            scope.game = True
            value = value[:game.start()].strip()
            changed = True
            continue
        pattern = _APP_PHRASE.get(code)
        if pattern:
            app = re.search(r'\s+(?:' + pattern + r')\s*$', value, re.I)
            if app:
                name = next((app.group(g) for g in ('known', 'app', 'app2') if g in app.groupdict() and app.group(g)), '')
                scope.app = name.strip(' «»"“”').casefold().removeprefix('the ')
                value = value[:app.start()].strip()
                changed = True
    return value.strip(' ,'), scope


# ----------------------------------------------------------------- selection
def select(windows: list[Window], scope: Scope, monitors: list[Monitor]) -> list[Window]:
    chosen = [w for w in windows if not scope.game or w.game]
    if scope.monitor is not None:
        chosen = [w for w in chosen if w.monitor == scope.monitor]
    if scope.side and monitors:
        ordered = sorted(monitors, key=lambda m: (m.rect[0], m.rect[1]))
        target = ordered[0] if scope.side == 'left' else ordered[-1]
        chosen = [w for w in chosen if w.monitor == target.index]
    if scope.primary:
        primary = next((m.index for m in monitors if m.primary), 1)
        chosen = [w for w in chosen if w.monitor == primary]
    if scope.app:
        processes = APPS.get(scope.app, ())
        chosen = [w for w in chosen if (processes and w.process.casefold() in processes)
                  or scope.app in w.title.casefold() or scope.app in w.process.casefold().removesuffix('.exe')]
    return sorted(chosen, key=lambda w: w.z)


def _norm(value: str) -> str:
    return ' '.join(re.sub(r'[^\w\s]', ' ', str(value or '').casefold().replace('ё', 'е')).split())


def ocr_hit(lines: list[Any], name: str, scale: float, origin: tuple[int, int]) -> tuple[tuple[int, int, int, int], float, str] | None:
    """The best run of OCR words that spells ``name``: (screen rect, score, text)."""
    wanted = _norm(name)
    if not wanted:
        return None
    size = len(wanted.split())
    best = None
    for line in lines:
        words = list(getattr(line, 'words', ()) or ())
        for start in range(len(words)):
            for length in (size, size + 1, max(1, size - 1)):
                chunk = words[start:start + length]
                if len(chunk) != length:
                    continue
                text = _norm(' '.join(w.text for w in chunk))
                if not text:
                    continue
                score = 1.0 if text == wanted else difflib.SequenceMatcher(None, text, wanted).ratio()
                if score >= 0.84 and (best is None or score > best[1]):
                    left = min(w.x for w in chunk)
                    top = min(w.y for w in chunk)
                    right = max(w.x + w.width for w in chunk)
                    bottom = max(w.y + w.height for w in chunk)
                    rect = (int(origin[0] + left / scale), int(origin[1] + top / scale),
                            int(origin[0] + right / scale), int(origin[1] + bottom / scale))
                    best = (rect, score, ' '.join(w.text for w in chunk))
    return best


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    width = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = width * height
    area = lambda r: max(1, (r[2] - r[0]) * (r[3] - r[1]))
    return inter / float(min(area(a), area(b)))


def distinct_hits(hits: list['Hit']) -> list['Hit']:
    """Wrappers of ONE physical element (same place) collapse; different places stay apart."""
    out: list[Hit] = []
    for hit in hits:
        if not any(_overlap(hit.rect, kept.rect) >= 0.8 for kept in out):
            out.append(hit)
    return out


_POS = {
    'ru': (('вверху', 'посередине', 'внизу'), ('слева', 'в центре', 'справа')),
    'en': (('top', 'middle', 'bottom'), ('left', 'centre', 'right')),
    'de': (('oben', 'mittig', 'unten'), ('links', 'in der Mitte', 'rechts')),
    'fr': (('en haut', 'au milieu', 'en bas'), ('à gauche', 'au centre', 'à droite')),
    'es': (('arriba', 'en medio', 'abajo'), ('a la izquierda', 'en el centro', 'a la derecha')),
    'it': (('in alto', 'al centro', 'in basso'), ('a sinistra', 'al centro', 'a destra')),
    'pt': (('em cima', 'ao meio', 'em baixo'), ('à esquerda', 'ao centro', 'à direita')),
}


def position_label(hit: 'Hit', code: str) -> str:
    """«вверху слева» from the element's place inside its window (no screen content is stored)."""
    rows, cols = _POS.get(code, _POS['en'])
    left, top, right, bottom = hit.window.rect
    cx, cy = (hit.rect[0] + hit.rect[2]) / 2, (hit.rect[1] + hit.rect[3]) / 2
    width, height = max(1, right - left), max(1, bottom - top)
    col = min(2, max(0, int(3 * (cx - left) / width)))
    row = min(2, max(0, int(3 * (cy - top) / height)))
    return f'{rows[row]} {cols[col]}'


@dataclass
class Decision:
    kind: str                        # click | ask_window | ask_element | none
    hit: Hit | None = None
    options: list[Hit] = field(default_factory=list)


def decide(search: Search, *, last_hwnd: int = 0) -> Decision:
    if not search.hits:
        return Decision('none')
    by_window: dict[int, list[Hit]] = {}
    for hit in search.hits:
        by_window.setdefault(hit.window.hwnd, []).append(hit)
    best_per_window = {hwnd: max(hits, key=lambda h: h.score) for hwnd, hits in by_window.items()}
    exact = {hwnd: hit for hwnd, hit in best_per_window.items() if hit.score >= 0.99}
    pool = exact or best_per_window
    if len(pool) == 1:
        hwnd, hit = next(iter(pool.items()))
        same = distinct_hits([h for h in by_window[hwnd] if h.score >= hit.score - 0.001 and h.method == hit.method])
        # NEO 188 NEO-DESKTOP-001: two different elements with the SAME caption are a
        # choice too — never «the first one enumerated».
        if len(same) > 1:
            return Decision('ask_element', options=same)
        return Decision('click', hit=hit)
    if last_hwnd and last_hwnd in pool:
        return Decision('click', hit=pool[last_hwnd])      # the window NEO worked in just before
    options = sorted(pool.values(), key=lambda h: h.window.z)
    return Decision('ask_window', options=options)


# ------------------------------------------------------------------ messages
_TEXT = {
    'ask_window': ('«{name}» есть в нескольких окнах. Где нажать?', '“{name}” is in several windows. Where should I click?',
                   '„{name}“ gibt es in mehreren Fenstern. Wo soll ich klicken?', '« {name} » est dans plusieurs fenêtres. Où cliquer ?',
                   '«{name}» está en varias ventanas. ¿Dónde hago clic?', '«{name}» è in più finestre. Dove clicco?',
                   '«{name}» está em várias janelas. Onde clico?'),
    'option': ('{title} — экран {monitor}', '{title} — screen {monitor}', '{title} — Bildschirm {monitor}', '{title} — écran {monitor}',
               '{title} — pantalla {monitor}', '{title} — schermo {monitor}', '{title} — ecrã {monitor}'),
    'none': ('Не нашёл «{name}» в окнах: {windows}.', 'I could not find “{name}” in these windows: {windows}.',
             '„{name}“ habe ich in diesen Fenstern nicht gefunden: {windows}.', 'Je n’ai pas trouvé « {name} » dans : {windows}.',
             'No encontré «{name}» en estas ventanas: {windows}.', 'Non ho trovato «{name}» in queste finestre: {windows}.',
             'Não encontrei «{name}» nestas janelas: {windows}.'),
    'none_scope': ('Не нашёл подходящего окна{where}.', 'I found no matching window{where}.', 'Kein passendes Fenster{where}.',
                   'Aucune fenêtre correspondante{where}.', 'No encontré ninguna ventana{where}.', 'Nessuna finestra adatta{where}.',
                   'Não encontrei nenhuma janela{where}.'),
    'protected': ('«{title}» запущен от имени администратора: Windows не даёт другим программам нажимать в таком окне. '
                  'Чтобы NEO мог там нажимать, запустите NEO тоже от имени администратора.',
                  '“{title}” runs as administrator: Windows does not let other programs click inside such a window. '
                  'Run NEO as administrator too if NEO should click there.',
                  '„{title}“ läuft als Administrator: Windows lässt andere Programme dort nicht klicken. Starte NEO ebenfalls als Administrator.',
                  '« {title} » s’exécute en administrateur : Windows empêche les autres programmes d’y cliquer. Lancez aussi NEO en administrateur.',
                  '«{title}» se ejecuta como administrador: Windows no deja que otros programas hagan clic ahí. Ejecuta también NEO como administrador.',
                  '«{title}» è avviato come amministratore: Windows non consente ad altri programmi di cliccare lì. Avvia anche NEO come amministratore.',
                  '«{title}» está a correr como administrador: o Windows não deixa outros programas clicar aí. Execute também o NEO como administrador.'),
    'which_screen': ('Скажите, на каком экране или в каком окне искать: например «… на втором мониторе» или «… в игре».',
                     'Tell me which screen or window to look in, e.g. “… on the second monitor” or “… in the game”.',
                     'Sag mir, auf welchem Bildschirm oder in welchem Fenster: z. B. „… auf dem zweiten Monitor“.',
                     'Dites-moi sur quel écran ou dans quelle fenêtre : par ex. « … sur le deuxième écran ».',
                     'Dime en qué pantalla o ventana: por ejemplo «… en el segundo monitor».',
                     'Dimmi su quale schermo o finestra: ad es. «… sul secondo monitor».',
                     'Diz-me em que ecrã ou janela: por ex. «… no segundo monitor».'),
    'covered': ('«{name}» в окне «{title}» сейчас закрыт другим окном — не нажимаю вслепую.',
                '“{name}” in “{title}” is covered by another window right now — I won’t click blindly.',
                '„{name}“ in „{title}“ ist gerade verdeckt – ich klicke nicht blind.', '« {name} » dans « {title} » est masqué — je ne clique pas à l’aveugle.',
                '«{name}» en «{title}» está tapado por otra ventana: no hago clic a ciegas.', '«{name}» in «{title}» è coperto: non clicco alla cieca.',
                '«{name}» em «{title}» está tapado por outra janela: não clico às cegas.'),
    'clicked_in': ('Нажал «{name}» в окне «{title}».', 'Clicked “{name}” in “{title}”.', '„{name}“ in „{title}“ geklickt.',
                   'J’ai cliqué sur « {name} » dans « {title} ».', 'Hice clic en «{name}» en «{title}».', 'Ho cliccato «{name}» in «{title}».',
                   'Cliquei em «{name}» em «{title}».'),
    'found_in': ('Нашёл «{name}» в окне «{title}» (экран {monitor}) и отметил номером 1. Нажать — скажите «нажми 1».',
                 'Found “{name}” in “{title}” (screen {monitor}) and marked it 1. Say “click 1” to press it.',
                 '„{name}“ in „{title}“ (Bildschirm {monitor}) gefunden und mit 1 markiert. Sag „klicke 1“.',
                 '« {name} » trouvé dans « {title} » (écran {monitor}), marqué 1. Dites « clique 1 ».',
                 'Encontré «{name}» en «{title}» (pantalla {monitor}) y lo marqué con 1. Di «haz clic en 1».',
                 'Trovato «{name}» in «{title}» (schermo {monitor}), segnato con 1. Di «clicca 1».',
                 'Encontrei «{name}» em «{title}» (ecrã {monitor}) e marquei com 1. Diz «clica 1».'),
    'same_caption': ('В окне «{title}» несколько элементов «{name}», отметил их номерами:\n{options}\nКакой нажать? Скажите, например, «нажми 2».',
                     'In “{title}” there are several “{name}” elements; I marked them with numbers:\n{options}\nWhich one? Say, for example, “click 2”.',
                     'In „{title}“ gibt es mehrere „{name}“; ich habe sie nummeriert:\n{options}\nWelches? Sag z. B. „klicke 2“.',
                     'Dans « {title} », il y a plusieurs « {name} » ; je les ai numérotés :\n{options}\nLequel ? Dites par ex. « clique 2 ».',
                     'En «{title}» hay varios «{name}»; los he numerado:\n{options}\n¿Cuál? Di, por ejemplo, «haz clic en 2».',
                     'In «{title}» ci sono più «{name}»; li ho numerati:\n{options}\nQuale? Di’ per esempio «clicca 2».',
                     'Em «{title}» há vários «{name}»; numerei-os:\n{options}\nQual? Diz, por exemplo, «clica 2».'),
    'same_caption_ocr': ('В окне «{title}» несколько надписей «{name}»: {options}. Ничего не нажимаю: уточните, какую (например, «… внизу справа»).',
                         'In “{title}” there are several “{name}” labels: {options}. I am not clicking: tell me which one (e.g. “… bottom right”).',
                         'In „{title}“ gibt es mehrere Beschriftungen „{name}“: {options}. Ich klicke nicht: Welche?',
                         'Dans « {title} », plusieurs libellés « {name} » : {options}. Je ne clique pas : lequel ?',
                         'En «{title}» hay varias etiquetas «{name}»: {options}. No hago clic: ¿cuál?',
                         'In «{title}» ci sono più scritte «{name}»: {options}. Non clicco: quale?',
                         'Em «{title}» há várias legendas «{name}»: {options}. Não clico: qual?'),
    'which_element': ('В окне «{title}» похожи: {options}. Уточните название.', 'In “{title}” these look similar: {options}. Which one?',
                      'In „{title}“ ähneln sich: {options}. Welches?', 'Dans « {title} », ceux-ci se ressemblent : {options}. Lequel ?',
                      'En «{title}» se parecen: {options}. ¿Cuál?', 'In «{title}» si somigliano: {options}. Quale?',
                      'Em «{title}» parecem-se: {options}. Qual?'),
}


def t(key: str, code: str, /, **values: Any) -> str:
    return _TEXT[key][_CODES.index(code) if code in _CODES else 1].format(**values)


def scope_text(scope: Scope, code: str) -> str:
    parts = []
    if scope.monitor is not None:
        parts.append({'ru': f' на экране {scope.monitor}'}.get(code, f' on screen {scope.monitor}'))
    if scope.side:
        parts.append({'ru': ' на левом экране' if scope.side == 'left' else ' на правом экране'}.get(code, f' on the {scope.side} screen'))
    if scope.game:
        parts.append({'ru': ' с игрой'}.get(code, ' with a game'))
    if scope.app:
        parts.append({'ru': f' «{scope.app}»'}.get(code, f' “{scope.app}”'))
    return ''.join(parts)


# ------------------------------------------------------------------ Windows
def monitors() -> list[Monitor]:
    if os.name != 'nt':
        return [Monitor(1, (0, 0, 1920, 1080), True)]
    import ctypes
    from ctypes import wintypes
    found: list[tuple[tuple[int, int, int, int], bool]] = []

    class MONITORINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT), ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]

    proc_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def callback(handle, _hdc, _rect, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(((r.left, r.top, r.right, r.bottom), bool(info.dwFlags & 1)))
        return 1

    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc_type(callback), 0)
    ordered = sorted(found, key=lambda item: (item[0][0], item[0][1]))
    return [Monitor(i, rect, primary) for i, (rect, primary) in enumerate(ordered, 1)] or [Monitor(1, (0, 0, 1920, 1080), True)]


def monitor_of(rect: tuple[int, int, int, int], screens: list[Monitor]) -> int:
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    for screen in screens:
        if screen.rect[0] <= cx < screen.rect[2] and screen.rect[1] <= cy < screen.rect[3]:
            return screen.index
    best = max(screens, key=lambda s: max(0, min(rect[2], s.rect[2]) - max(rect[0], s.rect[0])) *
               max(0, min(rect[3], s.rect[3]) - max(rect[1], s.rect[1])), default=None)
    return best.index if best else 1


def _elevated(pid: int) -> bool | None:
    """True/False for the process token's elevation; None when Windows refuses to tell (treated as protected)."""
    import ctypes
    from ctypes import wintypes
    kernel32, advapi32 = ctypes.windll.kernel32, ctypes.windll.advapi32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))            # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):   # TOKEN_QUERY
            return None
        elevation = wintypes.DWORD()
        size = wintypes.DWORD()
        if not advapi32.GetTokenInformation(token, 20, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(size)):
            return None
        return bool(elevation.value)
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(handle)


def windows(*, limit: int = 14) -> list[Window]:
    """Visible normal windows in z-order (top first), NEO's own excluded."""
    if os.name != 'nt':
        return []
    import ctypes
    from ctypes import wintypes
    import psutil
    user32 = ctypes.windll.user32
    screens = monitors()
    me_elevated = bool(_elevated(os.getpid()))
    game_pid = 0
    try:
        from jarvis.gaming.runtime import active_game_context
        game_pid = int((active_game_context(max_age_seconds=120.0) or {}).get('pid', 0) or 0)
    except Exception:
        pass
    result: list[Window] = []
    proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _data):
        if len(result) >= limit or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x00000080:          # WS_EX_TOOLWINDOW
            return True
        cloaked = wintypes.DWORD()
        try:
            ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        except Exception:
            cloaked.value = 0
        if cloaked.value:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        box = (rect.left, rect.top, rect.right, rect.bottom)
        if box[2] - box[0] < 120 or box[3] - box[1] < 80:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if int(pid.value) == os.getpid():
            return True
        try:
            process = psutil.Process(int(pid.value)).name().casefold()
        except Exception:
            process = ''
        if process in {'neo.exe', 'textinputhost.exe', 'shellexperiencehost.exe', 'searchhost.exe', 'startmenuexperiencehost.exe'}:
            return True
        elevated = _elevated(int(pid.value))
        result.append(Window(int(hwnd), buffer.value, process, int(pid.value), box, monitor_of(box, screens),
                             len(result), protected=(not me_elevated and elevated is not False),
                             game=bool(game_pid and int(pid.value) == game_pid)))
        return True

    user32.EnumWindows(proc_type(callback), 0)
    return result


def uia_hits(window: Window, name: str) -> tuple[list[Hit], int]:
    """(hits, number of elements UI Automation exposes in this window)."""
    from jarvis.runtime import ui_elements168 as elements
    try:
        _hwnd, _process, found = elements.collect('all', limit=400, hwnd=window.hwnd)
    except Exception:
        logger.debug('UIA collect failed for %s', window.process, exc_info=True)
        return [], 0
    element, candidates = elements.match_by_name(found, name)
    if element is not None:
        return [Hit(window, element.name, element.rect, 'uia', 1.0 if _norm(element.name) == _norm(name) else 0.9, element)], len(found)
    return [Hit(window, c.name, c.rect, 'uia', 0.86, c) for c in candidates[:4]], len(found)


def ocr_hits(window: Window, name: str) -> list[Hit]:
    from jarvis.runtime import screen_ocr168
    left, top, right, bottom = window.rect
    screens = monitors()
    screen = next((s for s in screens if s.index == window.monitor), screens[0])
    left, top = max(left, screen.rect[0]), max(top, screen.rect[1])
    right, bottom = min(right, screen.rect[2]), min(bottom, screen.rect[3])
    if right - left < 40 or bottom - top < 30:
        return []
    try:
        image = screen_ocr168.grab(left, top, right - left, bottom - top)
        lines, scale = screen_ocr168.recognize(image, max_edge=2400)
    except Exception:
        logger.debug('OCR failed for %s', window.process, exc_info=True)
        return []
    found = ocr_hit(lines, name, scale, (left, top))
    if found is None:
        return []
    rect, score, text = found
    return [Hit(window, text, rect, 'ocr', score)]


def search(name: str, scope: Scope, *, restrict_hwnd: int = 0, max_windows: int = 8,
           uia: Callable[[Window, str], tuple[list[Hit], int]] = uia_hits,
           ocr: Callable[[Window, str], list[Hit]] = ocr_hits,
           listing: Callable[[], list[Window]] = windows,
           screens: Callable[[], list[Monitor]] = monitors) -> Search:
    all_windows = listing()
    displays = screens()
    candidates = [w for w in all_windows if w.hwnd == restrict_hwnd] if restrict_hwnd else select(all_windows, scope, displays)
    result = Search(monitors=len(displays))
    for window in candidates[:max_windows]:
        result.searched.append(window)
        if window.protected:
            result.protected.append(window)
            continue
        hits, exposed = uia(window, name)
        if not hits and (exposed < 5 or window.game):
            hits = ocr(window, name)             # games and custom-drawn interfaces: read the text on screen
        result.hits.extend(hits)
        if hits and max(h.score for h in hits) >= 0.99 and window.z == 0 and scope.empty() and not restrict_hwnd:
            break                                # exactly there in the window you are working in: no need to look further
    return result


def covered(hit: Hit) -> bool:
    """Would a click at the centre of the hit land in another window?"""
    if os.name != 'nt':
        return False
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    x, y = (hit.rect[0] + hit.rect[2]) // 2, (hit.rect[1] + hit.rect[3]) // 2
    user32.WindowFromPoint.argtypes = (wintypes.POINT,)
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.restype = wintypes.HWND
    at = user32.WindowFromPoint(wintypes.POINT(x, y))
    root = user32.GetAncestor(at, 2) if at else None                # GA_ROOT
    return bool(root) and int(root) != int(hit.window.hwnd)


def bring_to_front(window: Window, timeout: float = 0.8) -> bool:
    if os.name != 'nt':
        return False
    import ctypes
    user32 = ctypes.windll.user32
    user32.SetForegroundWindow(window.hwnd)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if int(user32.GetForegroundWindow() or 0) == window.hwnd:
            time.sleep(0.05)
            return True
        time.sleep(0.03)
    return False


# ------------------------------------------------------- «где нажать?» answers
_PENDING: dict[str, Any] = {}
_ANSWER_TTL = 120.0


def ask(name: str, options: list[Hit], code: str, *, action: str = 'click_name') -> str:
    """Remember the choice and return the question with numbered windows and their screens."""
    _PENDING.clear()
    _PENDING.update(name=name, action=action, time=time.monotonic(),
                    options=[(h.window.hwnd, h.window.title, h.window.process, h.window.monitor, h.window.game) for h in options])
    lines = [t('ask_window', code, name=name)]
    for number, hit in enumerate(options, 1):
        lines.append(f'{number}. ' + t('option', code, title=hit.window.label(), monitor=hit.window.monitor))
    return '\n'.join(lines)


def pending() -> bool:
    return bool(_PENDING) and time.monotonic() - float(_PENDING.get('time', 0.0)) <= _ANSWER_TTL


def resolve(text: str, code: str) -> dict[str, Any] | None:
    """The user's answer to «где нажать?»: a number, «в игре», «на втором экране», or a window name."""
    if not pending():
        _PENDING.clear()
        return None
    from jarvis.runtime.input168 import parse_ordinal
    options = list(_PENDING['options'])
    value = ' '.join(str(text or '').split()).strip(' .!?')
    chosen = None
    number = parse_ordinal(value.split()[-1], code) if value.split() else None
    if value.isdigit():
        number = int(value)
    if number is not None and 1 <= number <= len(options) and len(value.split()) <= 3:
        chosen = options[number - 1]
    if chosen is None:
        _rest, scope = parse_scope('x ' + value, code)
        if not scope.empty():
            fake = [Window(h, title, process, 0, (0, 0, 1, 1), monitor, i, game=game)
                    for i, (h, title, process, monitor, game) in enumerate(options)]
            picked = select(fake, scope, monitors())
            if len(picked) == 1:
                chosen = next(o for o in options if o[0] == picked[0].hwnd)
    if chosen is None:
        key = _norm(value)
        named = [o for o in options if key and len(key) >= 3 and (key in _norm(o[1]) or key in _norm(o[2]))]
        if len(named) == 1:
            chosen = named[0]
    if chosen is None:
        return None
    args = {'action': _PENDING['action'], 'name': _PENDING['name'], 'window': str(chosen[0])}
    _PENDING.clear()
    return args


def forget_pending() -> None:
    _PENDING.clear()
