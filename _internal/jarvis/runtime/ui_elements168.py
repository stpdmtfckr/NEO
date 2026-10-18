"""NEO 168: find, number and click elements of the active window (UI Automation).

«Покажи номера» puts a small numbered badge on every link/button/field of the
active window; «нажми 7» clicks that element. «Нажми на первую ссылку» /
«третью кнопку» pick by reading order without showing numbers. In a browser
only the web page is considered (not tabs or the address bar), and for links the
search-page navigation («Картинки», «Войти», …) is skipped, so «первая ссылка»
on a results page is the first result.

UI Automation reads the accessibility tree the application itself publishes —
no screenshot, no neural network, a few hundred milliseconds even on a slow CPU.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import threading
import time
from typing import Any

KIND_TYPES = {
    'link': ('Hyperlink',),
    'button': ('Button', 'SplitButton', 'MenuItem', 'CheckBox', 'RadioButton'),
    'tab': ('TabItem',),
    'field': ('Edit', 'ComboBox'),
    'item': ('ListItem', 'TreeItem', 'DataItem'),
}
KIND_TYPES['all'] = tuple(t for kind in ('link', 'button', 'tab', 'field', 'item') for t in KIND_TYPES[kind])
BROWSER_PROCESSES = frozenset({'chrome.exe', 'msedge.exe', 'opera.exe', 'opera_gx.exe', 'firefox.exe',
                               'browser.exe', 'yandex.exe', 'brave.exe', 'vivaldi.exe'})
_WINDOW_BUTTONS = frozenset({'minimize', 'maximize', 'restore', 'close', 'свернуть', 'развернуть',
                             'восстановить', 'закрыть', 'minimieren', 'maximieren', 'schließen', 'réduire',
                             'agrandir', 'fermer', 'minimizar', 'maximizar', 'cerrar', 'riduci a icona',
                             'ingrandisci', 'chiudi', 'fechar'})
# Search-page navigation that is never «the first link».
_NAV_WORDS = ('google', 'поиск', 'search', 'картинки', 'images', 'видео', 'videos', 'новости', 'news',
              'карты', 'maps', 'инструменты', 'tools', 'войти', 'sign in', 'настройки', 'settings',
              'справка', 'help', 'кэш', 'cached', 'bilder', 'anmelden', 'einstellungen', 'connexion',
              'paramètres', 'iniciar sesión', 'configuración', 'accedi', 'impostazioni', 'fazer login',
              'configurações', 'яндекс', 'yandex', 'bing', 'duckduckgo', 'ещё', 'more', 'mehr', 'plus', 'más', 'altro', 'mais')


@dataclass
class Element:
    name: str
    control_type: str
    rect: tuple[int, int, int, int]            # left, top, right, bottom (physical pixels)
    wrapper: Any = field(default=None, repr=False, compare=False)
    number: int = 0

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2


# ------------------------------------------------------------------ pure part
def reading_order(elements: list[Element], *, row_tolerance: int = 12) -> list[Element]:
    """Top-to-bottom rows, left-to-right inside a row (like reading a page)."""
    rows: list[list[Element]] = []
    for element in sorted(elements, key=lambda e: (e.rect[1], e.rect[0])):
        if rows and abs(element.rect[1] - rows[-1][0].rect[1]) <= row_tolerance:
            rows[-1].append(element)
        else:
            rows.append([element])
    return [element for row in rows for element in sorted(row, key=lambda e: e.rect[0])]


def usable(elements: list[Element], window: tuple[int, int, int, int], *, top_floor: int | None = None,
           need_name: bool = True) -> list[Element]:
    """Visible, reasonably sized, inside the window, below the browser chrome, no duplicates."""
    left, top, right, bottom = window
    out: list[Element] = []
    seen: set[tuple] = set()
    for element in elements:
        l, t, r, b = element.rect
        if r - l < 6 or b - t < 6 or r <= left or l >= right or b <= top or t >= bottom:
            continue
        if top_floor is not None and t < top_floor:
            continue
        name = ' '.join(str(element.name or '').split())
        if need_name and not name:
            continue
        if name.casefold() in _WINDOW_BUTTONS and t < top + 40:
            continue
        key = (name.casefold(), element.control_type, l // 4, t // 4)
        if key in seen:
            continue
        seen.add(key)
        element.name = name
        out.append(element)
    return out


def page_links(elements: list[Element]) -> list[Element]:
    """Links that look like page content: not search-page navigation, one per result."""
    out: list[Element] = []
    for element in elements:
        text = ' '.join(element.name.casefold().replace('›', ' ').split())
        if len(text) < 3:
            continue
        if any(text == word or text.startswith(word + ' ') for word in _NAV_WORDS):
            continue
        if 'google com' in text.replace('.', ' ') and len(text.split()) <= 5:
            continue
        if element.rect[2] - element.rect[0] < 18:
            continue
        # A result's title and its breadcrumb often form two adjacent links.
        if out:
            prev = out[-1]
            overlaps = element.rect[1] < prev.rect[3] + 4 and abs(element.rect[0] - prev.rect[0]) < 40
            if overlaps and (element.name.casefold() in prev.name.casefold() or prev.name.casefold() in element.name.casefold()):
                continue
        out.append(element)
    return out


def pick(elements: list[Element], index: int) -> Element | None:
    """1-based index; -1 is the last, -2 the one before."""
    if not elements or index == 0:
        return None
    position = index - 1 if index > 0 else len(elements) + index
    return elements[position] if 0 <= position < len(elements) else None


def _norm_name(value: str) -> str:
    return ' '.join(re.sub(r'[^\w\s]', ' ', str(value or '').casefold().replace('ё', 'е')).split())


def match_by_name(elements: list[Element], name: str) -> tuple[Element | None, list[Element]]:
    """(the one element whose name matches, or None; the close candidates when unsure)."""
    import difflib
    wanted = _norm_name(name)
    if not wanted:
        return None, []
    scored: list[tuple[float, Element]] = []
    for element in elements:
        label = _norm_name(element.name)
        if not label:
            continue
        if label == wanted:
            score = 1.0
        elif label.startswith(wanted + ' ') or label.endswith(' ' + wanted) or f' {wanted} ' in f' {label} ':
            score = 0.9
        else:
            score = difflib.SequenceMatcher(None, wanted, label).ratio()
        scored.append((score, element))
    scored.sort(key=lambda pair: -pair[0])
    good = [pair for pair in scored if pair[0] >= 0.78]
    if not good:
        return None, [e for score, e in scored[:3] if score >= 0.72]
    exact = [e for score, e in good if score == 1.0]
    if len(exact) == 1:
        return exact[0], []
    if len(good) == 1 or (good[0][0] - good[1][0] >= 0.08 and not exact):
        return good[0][1], []
    return None, [e for _, e in good[:4]]


# ------------------------------------------------------------ numbered hints
@dataclass
class HintSet:
    hwnd: int
    elements: list[Element]
    created: float


_HINTS: HintSet | None = None
_LOCK = threading.Lock()


def remember(hwnd: int, elements: list[Element]) -> None:
    global _HINTS
    for number, element in enumerate(elements, 1):
        element.number = number
    with _LOCK:
        _HINTS = HintSet(int(hwnd), list(elements), time.monotonic())


def forget() -> None:
    global _HINTS
    with _LOCK:
        _HINTS = None


def hinted(number: int, *, max_age: float = 180.0) -> tuple[int, Element | None]:
    with _LOCK:
        hints = _HINTS
    if hints is None or time.monotonic() - hints.created > max_age:
        return 0, None
    element = next((e for e in hints.elements if e.number == int(number)), None)
    return hints.hwnd, element


# -------------------------------------------------------------- Windows part
def foreground() -> tuple[int, str, Any]:
    """(hwnd, process name, pywinauto window) of the active window."""
    if os.name != 'nt':
        raise OSError('Screen elements are available on Windows only')
    import ctypes
    from ctypes import wintypes
    import psutil
    from pywinauto import Desktop
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        raise RuntimeError('no active window')
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        process = psutil.Process(int(pid.value)).name().casefold()
    except Exception:
        process = ''
    return hwnd, process, Desktop(backend='uia').window(handle=hwnd)


def _user32():
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    user32.GetWindow.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    return user32


def _pid(user32: Any, hwnd: Any) -> int:
    import ctypes
    from ctypes import wintypes
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def neo_is_foreground() -> bool:
    if os.name != 'nt':
        return False
    user32 = _user32()
    hwnd = user32.GetForegroundWindow()
    return bool(hwnd) and _pid(user32, hwnd) == os.getpid()


def window_behind_neo() -> int:
    """The first normal window under NEO in z-order: where the user was working."""
    user32 = _user32()
    hwnd = user32.GetForegroundWindow()
    for _ in range(300):
        hwnd = user32.GetWindow(hwnd, 2)          # GW_HWNDNEXT
        if not hwnd:
            return 0
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or not user32.GetWindowTextLengthW(hwnd):
            continue
        if _pid(user32, hwnd) == os.getpid():
            continue
        rect = _rect_of(hwnd)
        if rect is None or rect[2] - rect[0] < 120 or rect[3] - rect[1] < 80:
            continue
        return int(hwnd)
    return 0


def _rect_of(hwnd: Any) -> tuple[int, int, int, int] | None:
    import ctypes
    from ctypes import wintypes
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def ensure_target(timeout: float = 0.8) -> int:
    """Make the user's working window active before input.

    When the command was typed into NEO's own chat, NEO is the active window; keys,
    typing and element search must go to the window the user was working in, not
    into NEO. Returns that window, or 0 when there is no window to act on.
    """
    if os.name != 'nt':
        return 0
    user32 = _user32()
    current = user32.GetForegroundWindow()
    if current and _pid(user32, current) != os.getpid():
        return int(current)
    target = window_behind_neo()
    if not target:
        return 0
    user32.SetForegroundWindow(target)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if int(user32.GetForegroundWindow() or 0) == target:
            time.sleep(0.05)
            return target
        time.sleep(0.03)
    return target if int(user32.GetForegroundWindow() or 0) == target else 0


def _rect(wrapper: Any) -> tuple[int, int, int, int] | None:
    try:
        r = wrapper.rectangle()
        return int(r.left), int(r.top), int(r.right), int(r.bottom)
    except Exception:
        return None


def _name(wrapper: Any) -> str:
    for getter in (lambda: wrapper.window_text(), lambda: wrapper.element_info.name,
                   lambda: wrapper.element_info.help_text):
        try:
            value = ' '.join(str(getter() or '').split())
            if value:
                return value
        except Exception:
            continue
    return ''


def browser_top_floor(window_rect: tuple[int, int, int, int]) -> int:
    height = window_rect[3] - window_rect[1]
    return window_rect[1] + min(150, max(95, int(height * 0.12)))


def window_by_handle(hwnd: int) -> tuple[int, str, Any]:
    """(hwnd, process name, pywinauto window) for any top-level window (NEO 171)."""
    import psutil
    from pywinauto import Desktop
    user32 = _user32()
    try:
        process = psutil.Process(_pid(user32, hwnd)).name().casefold()
    except Exception:
        process = ''
    return int(hwnd), process, Desktop(backend='uia').window(handle=int(hwnd))


def collect(kind: str = 'all', *, limit: int = 60, hwnd: int = 0) -> tuple[int, str, list[Element]]:
    """Elements of ``kind`` in the active window (or in window ``hwnd``), in reading order."""
    hwnd, process, window = window_by_handle(hwnd) if hwnd else foreground()
    window_rect = _rect(window) or (0, 0, 10 ** 5, 10 ** 5)
    browser = process in BROWSER_PROCESSES
    raw: list[Element] = []
    for control_type in KIND_TYPES.get(kind, KIND_TYPES['all']):
        try:
            found = window.descendants(control_type=control_type)
        except Exception:
            continue
        for wrapper in found[:1500]:
            try:
                if getattr(wrapper.element_info, 'visible', True) is False:
                    continue
            except Exception:
                pass
            rect = _rect(wrapper)
            if rect is None:
                continue
            raw.append(Element(_name(wrapper), control_type, rect, wrapper))
    elements = usable(raw, window_rect, top_floor=browser_top_floor(window_rect) if browser else None,
                      need_name=kind not in {'field', 'all'})
    ordered = reading_order(elements)
    if kind == 'link' and browser:
        ordered = page_links(ordered)
    return hwnd, process, ordered[:limit]


def click(element: Element) -> str:
    """Invoke the element (the way a screen reader does); fall back to a real click."""
    for method in ('invoke', 'click_input', 'click'):
        try:
            getattr(element.wrapper, method)()
            return method
        except Exception:
            continue
    raise RuntimeError(f'could not press «{element.name}»')
