"""NEO 168 tool handlers: on-screen control (``ui``) and browser navigation (``browser_nav``).

Registered in jarvis/tools/integrations.py. Every action runs locally, in the
active window, only when the user asks for it; replies say exactly what was
done. UI Automation is used first (it reads the elements an application
publishes), the mouse only as a fallback.
"""
from __future__ import annotations

import logging
import re
import os
import time
from typing import Any

from jarvis.runtime import input168
from jarvis.runtime import ui_elements168 as elements
from jarvis.tools.registry import ToolExecution
from jarvis.tools.ui_messages168 import msg

logger = logging.getLogger(__name__)

_KIND_ICON = {'Hyperlink': '🔗', 'Button': '🔘', 'SplitButton': '🔘', 'MenuItem': '☰', 'CheckBox': '☑', 'RadioButton': '◉',
              'TabItem': '📑', 'Edit': '✏️', 'ComboBox': '▾', 'ListItem': '▫️', 'TreeItem': '▫️', 'DataItem': '▫️'}


def _code(config: Any) -> str:
    return str(config.get('app.language', 'ru') or 'ru')


def _metric(name: str, started: float, **data: Any) -> None:
    """Timings go to the regular NEO log and the developer bus."""
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    logger.info('ui168 %s: %.1f ms %s', name, elapsed_ms, data)
    try:
        from jarvis.runtime.developer import developer_bus
        developer_bus().emit('latency', f'ui168 {name}', data={**data, 'duration_ms': elapsed_ms}, duration_ms=elapsed_ms)
    except Exception:
        pass


def _still_there(element: elements.Element, tolerance: int = 10) -> bool:
    """Re-read the element's position right before clicking: never click a moved target."""
    current = elements._rect(element.wrapper)
    if current is None:
        return False
    return all(abs(a - b) <= tolerance for a, b in zip(current, element.rect))


# The last input action NEO performed in this session (in memory only), for «ещё раз».
_LAST: dict[str, Any] = {}


def _remember(kind: str, **data: Any) -> None:
    _LAST.clear()
    _LAST.update(kind=kind, **data)


def last_action() -> dict[str, Any]:
    return dict(_LAST)


def _press_element(element: elements.Element, how: str = 'click') -> str:
    if how == 'double':
        element.wrapper.click_input(double=True)
        return 'click_input(double)'
    if how == 'right':
        element.wrapper.click_input(button='right')
        return 'click_input(right)'
    return elements.click(element)


def _target(code: str) -> ToolExecution | None:
    """Act on the user's working window, never on NEO's own chat (NEO 169)."""
    try:
        if elements.ensure_target() or not elements.neo_is_foreground():
            return None
    except Exception:
        logger.debug('Target window check failed', exc_info=True)
        return None
    return ToolExecution(False, msg('no_target', code), {'safe_failure': True})


def _window_title() -> str:
    try:
        return ' '.join(str(elements.foreground()[2].window_text() or '').split())[:60]
    except Exception:
        return ''


def _search171(config: Any, name: str, window: Any = 0):
    """NEO 171: look for ``name`` in every visible window (or one chosen window), across monitors."""
    from jarvis.runtime import screen_targets171 as targets
    code = _code(config)
    name, scope = targets.parse_scope(name, code)
    name = button_name(name)
    restrict = int(window) if str(window or '').isdigit() else 0
    started = time.perf_counter()
    found = targets.search(name, scope, restrict_hwnd=restrict)
    _metric('find_by_name', started, windows=len(found.searched), hits=len(found.hits),
            protected=len(found.protected), monitors=found.monitors, scope=not scope.empty())
    last = int(_LAST.get('hwnd', 0) or 0) if time.monotonic() - float(_LAST.get('time', 0.0) or 0.0) < 120 else 0
    return name, scope, found, targets.decide(found, last_hwnd=last)


def _not_found171(config: Any, name: str, scope: Any, found: Any) -> ToolExecution:
    from jarvis.runtime import screen_targets171 as targets
    code = _code(config)
    if not found.searched:
        return ToolExecution(False, targets.t('none_scope', code, where=targets.scope_text(scope, code)), {'requires_clarification': True})
    lines = []
    looked = [w for w in found.searched if not w.protected]
    if looked:
        lines.append(targets.t('none', code, name=name, windows=', '.join(
            f'«{w.label()}»' + (f' ({w.monitor})' if found.monitors > 1 else '') for w in looked)))
    for window in found.protected[:2]:
        lines.append(targets.t('protected', code, title=window.label()))
    if found.monitors > 1 and scope.empty():
        lines.append(targets.t('which_screen', code))
    return ToolExecution(False, '\n'.join(lines), {'requires_clarification': True, 'found': False,
                                                  'protected': [w.process for w in found.protected]})


def click_by_name(config: Any, name: str, window: Any = 0) -> ToolExecution:
    """«нажми (на) кнопку Сохранить [в игре / на втором мониторе]»: find it in any window and press it there."""
    from jarvis.runtime import screen_targets171 as targets
    code = _code(config)
    try:
        name, scope, found, decision = _search171(config, name, window)
    except OSError:
        return ToolExecution(False, msg('no_target', code), {'safe_failure': True})
    if decision.kind == 'none':
        return _not_found171(config, name, scope, found)
    if decision.kind == 'ask_window':
        return ToolExecution(False, targets.ask(name, decision.options, code), {'requires_clarification': True,
                             'choices': [h.window.label() for h in decision.options]})
    if decision.kind == 'ask_element':
        return _ask_element188(config, name, decision.options)
    hit = decision.hit
    targets.bring_to_front(hit.window)
    if hit.method == 'uia':
        if not _still_there(hit.element):
            return ToolExecution(False, msg('element_moved', code, name=hit.name), {'safe_failure': True})
        method = _press_element(hit.element)
    else:
        if targets.covered(hit):
            return ToolExecution(False, targets.t('covered', code, name=hit.name, title=hit.window.label()), {'safe_failure': True})
        input168.click_at((hit.rect[0] + hit.rect[2]) // 2, (hit.rect[1] + hit.rect[3]) // 2)
        method = 'mouse(ocr)'
    _remember('click', hwnd=hit.window.hwnd, element=hit.element, how='click', time=time.monotonic())
    return ToolExecution(True, targets.t('clicked_in', code, name=hit.name, title=hit.window.label()),
                         {'method': method, 'window': hit.window.process, 'monitor': hit.window.monitor, 'verified_target': True})


def _ask_element188(config: Any, name: str, options: list[Any]) -> ToolExecution:
    """NEO 188 NEO-DESKTOP-001: a numbered choice bound to the exact elements shown; nothing is pressed."""
    from jarvis.runtime import screen_targets171 as targets
    code = _code(config)
    options = list(options)[:9]
    window = options[0].window
    distinct_names = len({targets._norm(h.name) for h in options}) > 1
    label = lambda i, h: f'{i}. «{h.name[:40]}» — {targets.position_label(h, code)}' if distinct_names else f'{i}. {targets.position_label(h, code)}'
    if all(h.element is not None for h in options):
        elements.remember(window.hwnd, [h.element for h in options])    # «нажми 2» presses exactly this element
        try:
            from jarvis.ui.desktop_events import request_desktop
            request_desktop('ui.hints', marks=[{'n': i, 'x': h.rect[0], 'y': h.rect[1]} for i, h in enumerate(options, 1)],
                            ttl=float(config.get('ui.hints_seconds', 30) or 30))
        except Exception:
            pass
        text = targets.t('same_caption', code, title=window.label(), name=name,
                         options='\n'.join(label(i, h) for i, h in enumerate(options, 1)))
        return ToolExecution(False, text, {'requires_clarification': True, 'choices': len(options), 'hinted': True})
    text = targets.t('same_caption_ocr', code, title=window.label(), name=name,
                     options=', '.join(targets.position_label(h, code) for h in options))
    return ToolExecution(False, text, {'requires_clarification': True, 'choices': len(options), 'hinted': False})


def find_by_name(config: Any, name: str, window: Any = 0) -> ToolExecution:
    """«найди кнопку Продолжить»: locate it in any window and mark it with number 1 — nothing is clicked."""
    from jarvis.runtime import screen_targets171 as targets
    code = _code(config)
    try:
        name, scope, found, decision = _search171(config, name, window)
    except OSError:
        return ToolExecution(False, msg('no_target', code), {'safe_failure': True})
    if decision.kind == 'none':
        return _not_found171(config, name, scope, found)
    if decision.kind == 'ask_window':
        return ToolExecution(False, targets.ask(name, decision.options, code, action='find'), {'requires_clarification': True})
    if decision.kind == 'ask_element':
        return _ask_element188(config, name, decision.options)      # no single «found» element is claimed
    hit = decision.hit or decision.options[0]
    if hit.element is not None:
        elements.remember(hit.window.hwnd, [hit.element])
    try:
        from jarvis.ui.desktop_events import request_desktop
        request_desktop('ui.hints', marks=[{'n': 1, 'x': hit.rect[0], 'y': hit.rect[1]}],
                        ttl=float(config.get('ui.hints_seconds', 30) or 30))
    except Exception:
        pass
    return ToolExecution(True, targets.t('found_in', code, name=hit.name, title=hit.window.label(), monitor=hit.window.monitor),
                         {'found': True, 'method': hit.method, 'rect': list(hit.rect), 'window': hit.window.process})


def focus_field(config: Any, index: Any) -> ToolExecution:
    """«выбери второе поле»: put the cursor into the N-th input field of the working window."""
    code = _code(config)
    value = input168.parse_ordinal(str(index), code)
    if value is None or value == 0:
        return ToolExecution(False, msg('bad_index', code, value=index), {'requires_clarification': True})
    blocked = _target(code)
    if blocked is not None:
        return blocked
    hwnd, process, fields = elements.collect('field', limit=100)
    element = elements.pick(fields, value)
    window = _window_title()
    if element is None:
        return ToolExecution(False, msg('no_field_numbered', code, window=window, index=value, count=len(fields)), {'count': len(fields)})
    if not _still_there(element):
        return ToolExecution(False, msg('element_moved', code, name=element.name or element.control_type), {'safe_failure': True})
    try:
        element.wrapper.set_focus()
        method = 'set_focus'
    except Exception:
        element.wrapper.click_input()
        method = 'click_input'
    _remember('click', hwnd=hwnd, element=element, how='click')
    return ToolExecution(True, msg('focused_field', code, index=value, name=element.name or '—'),
                         {'index': value, 'method': method, 'verified_target': True})


# ------------------------------------------------------------ typing, keys
def type_text(config: Any, text: str) -> ToolExecution:
    code = _code(config)
    value = str(text or '').strip()
    if not value:
        return ToolExecution(False, msg('empty_text', code), {'requires_clarification': True})
    value = value[:2000]
    blocked = _target(code)
    if blocked is not None:
        return blocked
    started = time.perf_counter()
    delay = max(0.0, min(0.1, float(config.get('input.typing_delay_ms', 12) or 0) / 1000.0))
    typed = input168.type_text(value, delay=delay)
    _metric('type', started, characters=typed)
    _remember('type', text=value)
    shown = value if len(value) <= 80 else value[:79] + '…'
    return ToolExecution(True, msg('typed', code, text=shown), {'characters': typed, 'verified_input_sent': True})


_BUTTON_WORDS = re.compile(
    r'^(?:(?:на|по|а|в|on|the|auf|die|den|das|sur|le|la|l’|l\'|en|el|su|il|lo|em|no|na|o|a)\s+)*'
    r'(?:кнопк[уаеи]|клавиш[уаеи]|надпис[ьи]|текст|иконк[уае]|значок|ссылк[уае]|пункт|вкладк[уае]|элемент|'
    r'button|key|link|icon|label|text|tab|schaltfläche|taste|knopf|bouton|touche|lien|botón|boton|tecla|enlace|pulsante|tasto|botão|botao)\s+',
    re.I)


_WHERE_WORDS = re.compile(
    r'\s+(?:в\s+(?:этом|активном|текущем)\s+окне|здесь|тут|(?:in|on)\s+(?:this|the\s+active|the\s+current)\s+window|here|'
    r'im\s+(?:aktiven|diesem)\s+fenster|hier|dans\s+(?:cette|la)\s+fenêtre(?:\s+active)?|ici|en\s+(?:esta|la)\s+ventana(?:\s+activa)?|aquí|'
    r'in\s+questa\s+finestra|nella\s+finestra\s+attiva|qui|nesta\s+janela|na\s+janela\s+ativa|aqui)\s*$', re.I)


def button_name(text: str) -> str:
    """«на кнопку «Отправить» в активном окне» -> «Отправить» (the button's own name)."""
    value = str(text or '').strip().strip('«»"“”')
    value = _WHERE_WORDS.sub('', value).strip()
    return _BUTTON_WORDS.sub('', value, count=1).strip().strip('«»"“”') or value


def press_key(config: Any, registry: Any, key: str) -> ToolExecution:
    code = _code(config)
    spec = input168.parse_key_spec(key, code)
    if spec is None and input168.requests_timed_hold(key):
        # TIMED_PARSE_NAMED_CONTROL_FALLBACK: a timed hold is not a button name.
        return ToolExecution(False, msg('hold_unsupported', code), {'hold_unsupported': True, 'verified_input_sent': False})
    if spec is None:
        # Not a key name: «нажми Сохранить» / «нажми на кнопку Сохранить» means the on-screen button of that name.
        return click_by_name(config, button_name(key))
    blocked = _target(code)
    if blocked is not None:
        return blocked
    started = time.perf_counter()
    input168.press(spec)
    _metric('press', started, keys=spec.keys, count=spec.count)
    _remember('press', key=key)
    text = msg('pressed', code, key=spec.label) if spec.count == 1 else msg('pressed_times', code, key=spec.label, count=spec.count)
    return ToolExecution(True, text, {'keys': list(spec.keys), 'count': spec.count, 'verified_input_sent': True})


# ------------------------------------------------------- numbered elements
def show_hints(config: Any) -> ToolExecution:
    code = _code(config)
    blocked = _target(code)
    if blocked is not None:
        return blocked
    started = time.perf_counter()
    hwnd, process, found = elements.collect('all', limit=int(config.get('ui.hints_limit', 60) or 60))
    _metric('hints', started, method='uia', found=len(found), process=process)
    if not found:
        return ToolExecution(False, msg('hints_none', code), {'method': 'uia'})
    elements.remember(hwnd, found)
    marks = [{'n': e.number, 'x': e.rect[0], 'y': e.rect[1]} for e in found]
    try:
        from jarvis.ui.desktop_events import request_desktop
        request_desktop('ui.hints', marks=marks, ttl=float(config.get('ui.hints_seconds', 30) or 30))
    except Exception:
        logger.debug('Hint overlay request failed', exc_info=True)
    lines = [msg('hints_shown', code, count=len(found))]
    for element in found[:25]:
        label = element.name or element.control_type
        lines.append(f"{element.number} · {_KIND_ICON.get(element.control_type, '•')} {label[:60]}")
    if len(found) > 25:
        lines.append('…')
    return ToolExecution(True, '\n'.join(lines), {'count': len(found), 'method': 'uia'})


def hide_hints(config: Any) -> ToolExecution:
    elements.forget()
    try:
        from jarvis.ui.desktop_events import request_desktop
        accepted = bool(request_desktop('ui.hints_clear'))
    except Exception:
        accepted = False
    if not accepted:
        # HIST-016: a refused request is not «numbers removed».
        return ToolExecution(False, msg('hints_off_refused', _code(config)), {'accepted': False})
    return ToolExecution(True, msg('hints_off', _code(config)), {'accepted': True, 'desktop_action': 'ui.hints_clear'})


def click_number(config: Any, number: Any, how: str = 'click') -> ToolExecution:
    code = _code(config)
    value = input168.parse_ordinal(str(number), code)
    if value is None or value < 1:
        return ToolExecution(False, msg('bad_index', code, value=number), {'requires_clarification': True})
    hwnd, element = elements.hinted(value)
    if not hwnd:
        return ToolExecution(False, msg('no_hints', code), {'requires_clarification': True})
    if element is None:
        with elements._LOCK:
            total = len(elements._HINTS.elements) if elements._HINTS else 0
        return ToolExecution(False, msg('hint_missing', code, number=value, count=total), {})
    if elements.foreground()[0] != hwnd:
        return ToolExecution(False, msg('window_changed', code), {'safe_failure': True})
    if not _still_there(element):
        return ToolExecution(False, msg('element_moved', code, name=element.name or element.control_type), {'safe_failure': True})
    started = time.perf_counter()
    method = _press_element(element, how)
    _metric('click_number', started, method=method, number=value)
    _remember('click', hwnd=hwnd, element=element, how=how)
    hide_hints(config)
    return ToolExecution(True, msg('clicked', code, name=element.name or element.control_type),
                         {'number': value, 'method': method, 'control_type': element.control_type, 'verified_target': True})


_ORDINAL_KIND = {'link': ('link', 'no_link'), 'button': ('button', 'no_button'), 'item': ('item', 'no_item'), 'tab': ('tab', 'no_item')}


def click_nth(config: Any, kind: str, index: Any) -> ToolExecution:
    """«нажми первую ссылку», «вторую кнопку», «выбери третий пункт» — by reading order."""
    code = _code(config)
    value = input168.parse_ordinal(str(index), code)
    if value is None or value == 0:
        return ToolExecution(False, msg('bad_index', code, value=index), {'requires_clarification': True})
    collect_kind, missing_key = _ORDINAL_KIND.get(kind, ('link', 'no_link'))
    blocked = _target(code)
    if blocked is not None:
        return blocked
    started = time.perf_counter()
    hwnd, process, found = elements.collect(collect_kind, limit=200)
    element = elements.pick(found, value)
    _metric('find_nth', started, kind=kind, index=value, found=len(found), method='uia', process=process)
    if element is None:
        return ToolExecution(False, msg(missing_key, code, index=value, count=len(found)), {'count': len(found)})
    if elements.foreground()[0] != hwnd or not _still_there(element):
        return ToolExecution(False, msg('element_moved', code, name=element.name or element.control_type), {'safe_failure': True})
    clicked = time.perf_counter()
    method = _press_element(element)
    _metric('click_nth', clicked, method=method)
    _remember('click', hwnd=hwnd, element=element, how='click')
    return ToolExecution(True, msg('clicked', code, name=element.name or element.control_type),
                         {'kind': kind, 'index': value, 'method': method, 'verified_target': True})


def again(config: Any, registry: Any) -> ToolExecution:
    """«Нажми ещё раз» / «do it again»: repeat the last click, key or typing, with the same checks."""
    code = _code(config)
    last = last_action()
    kind = last.get('kind')
    if kind == 'type':
        return type_text(config, last['text'])
    if kind == 'press':
        return press_key(config, registry, last['key'])
    if kind == 'click':
        element = last['element']
        name = element.name or element.control_type
        # Same window, element still where it was: otherwise nothing is clicked.
        if elements.foreground()[0] != last['hwnd'] or not _still_there(element):
            return ToolExecution(False, msg('element_moved', code, name=name), {'safe_failure': True})
        started = time.perf_counter()
        method = _press_element(element, last.get('how', 'click'))
        _metric('click_again', started, method=method)
        return ToolExecution(True, msg('clicked', code, name=name), {'method': method, 'repeated': True, 'verified_target': True})
    return ToolExecution(False, msg('nothing_to_repeat', code), {'requires_clarification': True})


# --------------------------------------------------------------- scrolling
_SCROLL = {'scroll_down': -5, 'scroll_up': 5, 'scroll_down_more': -15, 'scroll_up_more': 15}


def scroll(config: Any, action: str) -> ToolExecution:
    code = _code(config)
    blocked = _target(code)
    if blocked is not None:
        return blocked
    started = time.perf_counter()
    if action in _SCROLL:
        input168.scroll(_SCROLL[action])
        key = 'scroll_down' if _SCROLL[action] < 0 else 'scroll_up'
    elif action in {'scroll_left', 'scroll_right'}:
        input168.press(input168.KeySpec(('left' if action == 'scroll_left' else 'right',), 8))
        key = action
    elif action in {'scroll_top', 'scroll_bottom'}:
        input168.press(input168.KeySpec(('ctrl', 'home' if action == 'scroll_top' else 'end')))
        key = action
    else:
        raise ValueError(action)
    _metric(action, started)
    return ToolExecution(True, msg(key, code), {'action': action, 'verified_input_sent': True})


# ----------------------------------------------------- fast text read (OCR)
def read_screen(config: Any) -> ToolExecution:
    """Window text via Windows OCR in well under a second — no vision model."""
    from jarvis.runtime import screen_card168, screen_ocr168
    code = _code(config)
    blocked = _target(code)
    if blocked is not None:
        return blocked
    hwnd, process, window = elements.foreground()
    rect = elements._rect(window)
    started = time.perf_counter()
    image = screen_ocr168.grab(rect[0], rect[1], max(1, rect[2] - rect[0]), max(1, rect[3] - rect[1])) if rect else None
    lines = screen_ocr168.text_lines(image) if image is not None else []
    _metric('read_screen', started, method='ocr', lines=len(lines), process=process)
    try:
        title = ' '.join(str(window.window_text() or '').split())
    except Exception:
        title = ''
    label = ' — '.join(x for x in (process, title[:100]) if x)
    return ToolExecution(True, screen_card168.read_card(label, lines, code), {'lines': len(lines), 'method': 'ocr'})


# ------------------------------------------------------ browser navigation
_BROWSER_KEYS = {
    'back': ('alt', 'left'), 'forward': ('alt', 'right'), 'reload': ('f5',), 'stop': ('esc',),
    'next_tab': ('ctrl', 'tab'), 'previous_tab': ('ctrl', 'shift', 'tab'), 'find_next': ('enter',),
    'zoom_in': ('ctrl', '='), 'zoom_out': ('ctrl', '-'), 'zoom_reset': ('ctrl', '0'), 'address': ('ctrl', 'l'),
    'next_field': ('tab',), 'previous_field': ('shift', 'tab'),
}


def _focus_browser() -> bool:
    try:
        from jarvis.browser_control import _foreground_browser_window
        window = _foreground_browser_window()
    except Exception:
        return False
    if window is None:
        return False
    try:
        window.set_focus()
        time.sleep(0.08)
    except Exception:
        pass
    return True


def browser_nav(config: Any, action: str, text: str = '') -> ToolExecution:
    code = _code(config)
    if not _focus_browser():
        return ToolExecution(False, msg('no_browser', code), {'safe_failure': True})
    started = time.perf_counter()
    if action == 'find':
        value = str(text or '').strip()
        if not value:
            return ToolExecution(False, msg('empty_text', code), {'requires_clarification': True})
        input168.press(input168.KeySpec(('ctrl', 'f')))
        time.sleep(0.15)
        input168.type_text(value[:200], delay=0.004)
        input168.press(input168.KeySpec(('enter',)))
        _metric('browser_find', started)
        return ToolExecution(True, msg('nav_find', code, text=value[:80]), {'action': action})
    if action == 'open_url':
        url = str(text or '').strip()
        if not url:
            return ToolExecution(False, msg('empty_text', code), {'requires_clarification': True})
        input168.press(input168.KeySpec(('ctrl', 'l')))
        time.sleep(0.12)
        input168.type_text(url[:500], delay=0.004)
        input168.press(input168.KeySpec(('enter',)))
        _metric('browser_open_url', started)
        return ToolExecution(True, msg('nav_open_url', code, url=url[:80]), {'action': action, 'url': url})
    keys = _BROWSER_KEYS.get(action)
    if keys is None:
        raise ValueError(action)
    input168.press(input168.KeySpec(keys))
    _metric('browser_' + action, started)
    return ToolExecution(True, msg('nav_' + action, code), {'action': action, 'verified_input_sent': True})
