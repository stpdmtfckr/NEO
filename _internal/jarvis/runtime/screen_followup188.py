"""NEO 188 NEO-SCREEN-001 / TZ-SCREEN-OBSERVATION-CONTINUATION-01.

«Подробнее» right after a screen description continues THAT observation:

* the details come from the same snapshot (all card items the short card left
  out and the OCR text of that snapshot) — no new capture, no other window;
* the observation is bound to the window handle captured with it; a window
  with the same title is not «the same window»; a closed window is reported as
  a lost binding;
* the visible boundary is stated: only that window / that monitor was seen and
  only a part of the text may have been recognized — absence in the snapshot is
  not absence on the computer;
* a changed or old snapshot is labelled as the previous state, and the current
  state needs a new allowed observation; stored positions never become click
  coordinates (nothing here clicks or confirms);
* an expired, cleared or no longer allowed observation is not rebuilt from
  guesses.

The observation lives only in memory for a short time (it contains screen text)
and is never written to logs or disk.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

TTL = 15 * 60.0            # an observation is forgotten after 15 minutes
FRESH = 120.0              # older snapshots are labelled as the previous state
MAX_ITEMS = 40             # bounded details

_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


@dataclass
class Observation:
    ident: int
    hwnd: int
    window: str
    title: str
    area: str                    # window | monitor
    monitor: str
    at: float
    data: dict | None
    shown: str
    ocr: list[str] = field(default_factory=list)
    ocr_limit: int = 60
    image_key: str = ''
    continued: str = ''


_STATE: dict[str, Any] = {'last': None, 'next': 1}
_BY_IMAGE: dict[str, dict] = {}


def remember(*, hwnd: int = 0, window: str = '', title: str = '', area: str = 'monitor', monitor: str = '',
             data: dict | None = None, shown: str = '', ocr: list[str] | None = None, ocr_limit: int = 60,
             image_key: str = '', now: float | None = None) -> Observation:
    """The newest allowed observation replaces the previous one."""
    if data is None and image_key and image_key in _BY_IMAGE:
        data = _BY_IMAGE[image_key].get('data')
        ocr = ocr if ocr else list(_BY_IMAGE[image_key].get('ocr') or [])
    if image_key:
        if len(_BY_IMAGE) > 16:
            _BY_IMAGE.clear()
        _BY_IMAGE[image_key] = {'data': data, 'ocr': list(ocr or [])}
    obs = Observation(int(_STATE['next']), int(hwnd or 0), ' '.join(str(window or '').split())[:160],
                      ' '.join(str(title or '').split())[:160], 'window' if area == 'window' else 'monitor',
                      str(monitor or ''), time.monotonic() if now is None else float(now),
                      dict(data) if isinstance(data, dict) else None, str(shown or ''),
                      [' '.join(str(x).split()) for x in (ocr or []) if str(x).strip()][:200], int(ocr_limit or 60), image_key)
    _STATE['next'] = obs.ident + 1
    _STATE['last'] = obs
    return obs


def forget() -> None:
    _STATE['last'] = None
    _BY_IMAGE.clear()


def current(*, now: float | None = None) -> Observation | None:
    obs = _STATE.get('last')
    if obs is None:
        return None
    now = time.monotonic() if now is None else float(now)
    if now - obs.at > TTL:
        forget()
        return None
    return obs


_MORE = {
    'ru': (r'(?:а\s+)?подробнее', r'(?:расскажи|опиши|объясни)\s+(?:это\s+|его\s+|её\s+)?подробнее', r'(?:давай\s+)?больше\s+(?:деталей|подробностей)',
           r'(?:а\s+)?что\s+(?:там\s+)?ещё(?:\s+там)?(?:\s+(?:видно|есть|на\s+экране|в\s+окне))?', r'(?:ещё|еще)\s+подробности', r'продолжай(?:\s+описание)?',
           r'(?:а\s+)?что\s+(?:там\s+)?еще(?:\s+там)?(?:\s+(?:видно|есть|на\s+экране|в\s+окне))?'),
    'en': (r'(?:tell\s+me\s+)?more(?:\s+details?)?', r'in\s+more\s+detail', r'(?:describe|explain)\s+(?:it|that|this)\s+in\s+more\s+detail',
           r'more\s+detail(?:s)?\s+please', r'what\s+else\s+(?:is\s+there|is\s+on\s+(?:the\s+)?(?:screen|window)|do\s+you\s+see)', r'go\s+on', r'details'),
    'de': (r'mehr\s+details', r'genauer', r'erzähl(?:\s+mir)?\s+mehr', r'beschreib(?:e)?\s+(?:es|das)\s+genauer', r'was\s+(?:ist|gibt\s+es)\s+(?:da\s+)?noch',
           r'weiter', r'details'),
    'fr': (r'plus\s+de\s+détails', r'en\s+détail', r'dis[- ]m[’\']en\s+plus', r'quoi\s+d[’\']autre', r'décris(?:[- ]le)?\s+plus\s+en\s+détail',
           r'qu[’\']y\s+a[- ]t[- ]il\s+d[’\']autre', r'continue', r'détails'),
    'es': (r'más\s+detalles', r'cuéntame\s+más', r'qué\s+más\s+hay', r'descríbelo\s+con\s+más\s+detalle', r'con\s+más\s+detalle', r'sigue', r'detalles'),
    'it': (r'più\s+dettagli', r'dimmi\s+di\s+più', r'cos[’\']altro\s+c[’\']è', r'che\s+altro\s+c[’\']è', r'descrivilo\s+meglio', r'più\s+nel\s+dettaglio',
           r'continua', r'dettagli'),
    'pt': (r'mais\s+detalhes', r'conta(?:-me)?\s+mais', r'diz[- ]me\s+mais', r'o\s+que\s+mais\s+(?:há|tem)', r'descreve\s+com\s+mais\s+detalhe',
           r'com\s+mais\s+detalhe', r'continua', r'detalhes'),
}
_MORE_RE = {code: re.compile(r'^(?:' + '|'.join(rows) + r')$', re.I) for code, rows in _MORE.items()}
_POLITE = re.compile(r'^(?:пожалуйста|please|bitte|s[’\']il\s+te\s+plaît|por\s+favor|per\s+favore|neo|нео)[,\s]+|[,\s]+(?:пожалуйста|please|bitte|s[’\']il\s+te\s+plaît|por\s+favor|per\s+favore)$', re.I)


def is_more(text: str, code: str) -> bool:
    pattern = _MORE_RE.get(code)
    if pattern is None:
        return False
    value = ' '.join(str(text or '').replace('ё', 'е').replace('Ё', 'Е').split()).strip(' ?!.¿¡…')
    value = _POLITE.sub('', value).strip(' ,?!.')
    value_yo = ' '.join(str(text or '').split()).strip(' ?!.¿¡…')
    value_yo = _POLITE.sub('', value_yo).strip(' ,?!.')
    return bool(pattern.fullmatch(value) or pattern.fullmatch(value_yo))


# NEO 188 atlas (proposed.screen.continue_observation): the request names the snapshot itself,
# so it continues that observation even after other turns — or says honestly that it is gone.
_SAME_RE = {code: re.compile(pattern, re.I) for code, pattern in {
    'ru': r'(?:продолжи|продолжай)(?: описание)? (?:по|с) (?:тому же|этому же|тем же|этим же) (?:снимку|снимком|скриншоту|скриншотом)(?: экрана)?',
    'en': r'(?:continue|go on|keep going)(?: describing)? (?:using|with|from) (?:the same|that same|this same) (?:screenshot|screen capture|snapshot)',
    'de': r'(?:fahre|mach) mit (?:demselben|dem gleichen|diesem) (?:bildschirmfoto|screenshot) (?:fort|weiter)',
    'fr': r"continue (?:avec|sur) (?:la même|la meme|cette même) (?:capture d['’]écran|capture d['’]ecran|capture)",
    'es': r'(?:continúa|continua|sigue) (?:usando|con) (?:la misma|esa misma) captura(?: de pantalla)?',
    'it': r'continua (?:usando|con) (?:la stessa|questa stessa) (?:schermata|cattura(?: dello schermo)?)',
    'pt': r'continua (?:com|usando) a mesma captura(?: de ecrã| de ecra| de tela)?',
}.items()}


def names_same_snapshot(text: str, code: str) -> bool:
    pattern = _SAME_RE.get(code)
    if pattern is None:
        return False
    value = ' '.join(str(text or '').replace('ё', 'е').split()).strip(' ?!.¿¡…')
    value = _POLITE.sub('', value).strip(' ,?!.')
    return bool(pattern.fullmatch(value))


def follows(obs: Observation | None, last_assistant: str) -> bool:
    """The continuation belongs to the observation only right after its description."""
    if obs is None or not obs.shown.strip():
        return False
    last = str(last_assistant or '').strip()
    head = obs.shown.strip()[:120]
    again = obs.continued.strip()[:120]
    return bool(last) and (head in last or last[:120] in obs.shown or bool(again and again in last))


def window_state(obs: Observation) -> str:
    """same | changed | gone | unknown — the bound window now (by handle, never by title)."""
    if not obs.hwnd or os.name != 'nt':
        return 'unknown'
    try:
        import ctypes
        user32 = ctypes.windll.user32
        if not user32.IsWindow(int(obs.hwnd)):
            return 'gone'
        length = int(user32.GetWindowTextLengthW(int(obs.hwnd)))
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(int(obs.hwnd), buffer, length + 1)
        title = ' '.join(buffer.value.split())[:160]
        return 'same' if not obs.title or title == obs.title else 'changed'
    except Exception:
        return 'unknown'


_T = {
    'head_window': ('🔎 Подробнее о том же снимке окна «{w}» ({age}):', '🔎 More about the same snapshot of the window “{w}” ({age}):',
                    '🔎 Mehr zum selben Bild des Fensters „{w}“ ({age}):', '🔎 Plus de détails sur la même capture de la fenêtre « {w} » ({age}) :',
                    '🔎 Más detalles de la misma captura de la ventana «{w}» ({age}):', '🔎 Più dettagli sulla stessa istantanea della finestra «{w}» ({age}):',
                    '🔎 Mais detalhes da mesma captura da janela «{w}» ({age}):'),
    'head_monitor': ('🔎 Подробнее о том же снимке экрана{m}, активное окно — «{w}» ({age}):', '🔎 More about the same screen snapshot{m}, active window “{w}” ({age}):',
                     '🔎 Mehr zum selben Bildschirmfoto{m}, aktives Fenster „{w}“ ({age}):', '🔎 Plus de détails sur la même capture d’écran{m}, fenêtre active « {w} » ({age}) :',
                     '🔎 Más detalles de la misma captura de pantalla{m}, ventana activa «{w}» ({age}):', '🔎 Più dettagli sulla stessa istantanea dello schermo{m}, finestra attiva «{w}» ({age}):',
                     '🔎 Mais detalhes da mesma captura do ecrã{m}, janela ativa «{w}» ({age}):'),
    'monitor': (' монитора {n}', ' of monitor {n}', ' von Monitor {n}', ' du moniteur {n}', ' del monitor {n}', ' del monitor {n}', ' do monitor {n}'),
    'now': ('только что', 'just now', 'gerade eben', 'à l’instant', 'hace un momento', 'poco fa', 'agora mesmo'),
    'sec': ('{n} с назад', '{n} s ago', 'vor {n} s', 'il y a {n} s', 'hace {n} s', '{n} s fa', 'há {n} s'),
    'min': ('{n} мин назад', '{n} min ago', 'vor {n} Min.', 'il y a {n} min', 'hace {n} min', '{n} min fa', 'há {n} min'),
    'summary': ('Что видно', 'What is visible', 'Was zu sehen ist', 'Ce qui est visible', 'Lo que se ve', 'Cosa si vede', 'O que se vê'),
    'ocr': ('📝 Распознанный текст этого снимка', '📝 Recognized text of this snapshot', '📝 Erkannter Text dieses Bildes',
            '📝 Texte reconnu sur cette capture', '📝 Texto reconocido en esta captura', '📝 Testo riconosciuto in questa istantanea',
            '📝 Texto reconhecido nesta captura'),
    'nothing_more': ('Больше подробностей в этом снимке нет — всё, что я на нём увидел, уже перечислено.',
                     'There are no more details in this snapshot — I already listed everything I saw on it.',
                     'Dieses Bild enthält keine weiteren Details – ich habe bereits alles aufgezählt, was ich darauf gesehen habe.',
                     'Cette capture ne contient pas plus de détails : j’ai déjà listé tout ce que j’y ai vu.',
                     'No hay más detalles en esta captura: ya enumeré todo lo que vi en ella.',
                     'In questa istantanea non ci sono altri dettagli: ho già elencato tutto ciò che vi ho visto.',
                     'Não há mais detalhes nesta captura: já listei tudo o que vi nela.'),
    'bound_window': ('Я видел только окно «{w}»; того, чего нет на этом снимке, может не быть в окне — или оно находится за пределами видимой области или в других окнах.',
                     'I only saw the window “{w}”; something missing from this snapshot may still be outside the visible area or in other windows.',
                     'Ich habe nur das Fenster „{w}“ gesehen; was auf diesem Bild fehlt, kann außerhalb des sichtbaren Bereichs oder in anderen Fenstern sein.',
                     'Je n’ai vu que la fenêtre « {w} » ; ce qui manque sur cette capture peut se trouver hors de la zone visible ou dans d’autres fenêtres.',
                     'Solo vi la ventana «{w}»; lo que no aparece en esta captura puede estar fuera del área visible o en otras ventanas.',
                     'Ho visto solo la finestra «{w}»; ciò che manca in questa istantanea può trovarsi fuori dall’area visibile o in altre finestre.',
                     'Só vi a janela «{w}»; o que não aparece nesta captura pode estar fora da área visível ou noutras janelas.'),
    'bound_monitor': ('Я видел только этот снимок экрана{m}; того, чего на нём нет, может быть в свёрнутых окнах, на другом мониторе или за пределами видимой области.',
                      'I only saw this screen snapshot{m}; something missing from it may be in minimized windows, on another monitor or outside the visible area.',
                      'Ich habe nur dieses Bildschirmfoto{m} gesehen; was fehlt, kann in minimierten Fenstern, auf einem anderen Monitor oder außerhalb des sichtbaren Bereichs sein.',
                      'Je n’ai vu que cette capture d’écran{m} ; ce qui n’y figure pas peut être dans des fenêtres réduites, sur un autre moniteur ou hors de la zone visible.',
                      'Solo vi esta captura de pantalla{m}; lo que no aparece puede estar en ventanas minimizadas, en otro monitor o fuera del área visible.',
                      'Ho visto solo questa istantanea dello schermo{m}; ciò che manca può essere in finestre ridotte, su un altro monitor o fuori dall’area visibile.',
                      'Só vi esta captura do ecrã{m}; o que não aparece pode estar em janelas minimizadas, noutro monitor ou fora da área visível.'),
    'ocr_partial': ('Текст распознан не полностью — только первые {n} строк; мелкий или неразборчивый текст мог не попасть.',
                    'The text was recognized only partly — the first {n} lines; small or illegible text may be missing.',
                    'Der Text wurde nur teilweise erkannt – die ersten {n} Zeilen; kleiner oder unleserlicher Text kann fehlen.',
                    'Le texte n’a été reconnu qu’en partie — les {n} premières lignes ; un texte petit ou illisible peut manquer.',
                    'El texto se reconoció solo en parte: las primeras {n} líneas; puede faltar texto pequeño o ilegible.',
                    'Il testo è stato riconosciuto solo in parte: le prime {n} righe; testo piccolo o illeggibile può mancare.',
                    'O texto foi reconhecido só em parte — as primeiras {n} linhas; texto pequeno ou ilegível pode faltar.'),
    'old': ('⏳ Это прежнее состояние (снимок {age}) — с тех пор окно могло измениться. Чтобы узнать, что там сейчас, скажите «что на экране».',
            '⏳ This is the previous state (snapshot {age}) — the window may have changed since. Say “what is on the screen” to see what is there now.',
            '⏳ Das ist der frühere Zustand (Bild {age}) – das Fenster kann sich seitdem geändert haben. Sag „was ist auf dem Bildschirm“, um den aktuellen Stand zu sehen.',
            '⏳ C’est l’état précédent (capture {age}) — la fenêtre a pu changer depuis. Dites « qu’est-ce qu’il y a à l’écran » pour voir l’état actuel.',
            '⏳ Es el estado anterior (captura {age}): la ventana puede haber cambiado. Di «qué hay en la pantalla» para ver lo actual.',
            '⏳ È lo stato precedente (istantanea {age}): la finestra potrebbe essere cambiata. Di’ «cosa c’è sullo schermo» per vedere lo stato attuale.',
            '⏳ É o estado anterior (captura {age}) — a janela pode ter mudado. Diga «o que está no ecrã» para ver o estado atual.'),
    'changed': ('⏳ Содержимое окна с тех пор изменилось — ниже прежнее состояние, не текущее. Для текущего скажите «что на экране».',
                '⏳ The window content has changed since — below is the previous state, not the current one. Say “what is on the screen” for the current one.',
                '⏳ Der Fensterinhalt hat sich seitdem geändert – unten steht der frühere Zustand, nicht der aktuelle. Für den aktuellen sag „was ist auf dem Bildschirm“.',
                '⏳ Le contenu de la fenêtre a changé depuis — ci-dessous l’état précédent, pas l’actuel. Pour l’actuel, dites « qu’est-ce qu’il y a à l’écran ».',
                '⏳ El contenido de la ventana cambió: abajo está el estado anterior, no el actual. Para el actual di «qué hay en la pantalla».',
                '⏳ Il contenuto della finestra è cambiato: sotto c’è lo stato precedente, non quello attuale. Per quello attuale di’ «cosa c’è sullo schermo».',
                '⏳ O conteúdo da janela mudou — abaixo está o estado anterior, não o atual. Para o atual diga «o que está no ecrã».'),
    'gone': ('⚠️ Окно «{w}» уже закрыто — привязка к нему утрачена. Ниже только то, что было видно на снимке {age}; это не текущее состояние.',
             '⚠️ The window “{w}” is closed — the binding to it is lost. Below is only what was visible in the snapshot {age}; it is not the current state.',
             '⚠️ Das Fenster „{w}“ ist geschlossen – die Zuordnung ist verloren. Unten steht nur, was auf dem Bild {age} sichtbar war; das ist nicht der aktuelle Zustand.',
             '⚠️ La fenêtre « {w} » est fermée — le lien avec elle est perdu. Ci-dessous seulement ce qui était visible sur la capture {age} ; ce n’est pas l’état actuel.',
             '⚠️ La ventana «{w}» está cerrada: se perdió el vínculo. Abajo solo lo que se veía en la captura {age}; no es el estado actual.',
             '⚠️ La finestra «{w}» è chiusa: il collegamento è perso. Sotto solo ciò che era visibile nell’istantanea {age}; non è lo stato attuale.',
             '⚠️ A janela «{w}» está fechada — a ligação perdeu-se. Abaixo só o que era visível na captura {age}; não é o estado atual.'),
    'unavailable': ('Этого наблюдения экрана у меня больше нет, поэтому подробности по памяти не восстанавливаю. Скажите «что на экране», чтобы сделать новый снимок.',
                    'I no longer have that screen observation, so I won’t rebuild details from memory. Say “what is on the screen” to take a new snapshot.',
                    'Diese Bildschirmbeobachtung habe ich nicht mehr, daher rekonstruiere ich keine Details aus dem Gedächtnis. Sag „was ist auf dem Bildschirm“ für ein neues Bild.',
                    'Je n’ai plus cette observation de l’écran, je ne reconstruis donc pas de détails de mémoire. Dites « qu’est-ce qu’il y a à l’écran » pour une nouvelle capture.',
                    'Ya no tengo esa observación de la pantalla, así que no reconstruyo detalles de memoria. Di «qué hay en la pantalla» para una nueva captura.',
                    'Non ho più quell’osservazione dello schermo, quindi non ricostruisco dettagli a memoria. Di’ «cosa c’è sullo schermo» per una nuova istantanea.',
                    'Já não tenho essa observação do ecrã, por isso não reconstruo detalhes de memória. Diga «o que está no ecrã» para uma nova captura.'),
    'no_window': ('без названия', 'untitled', 'ohne Titel', 'sans titre', 'sin título', 'senza titolo', 'sem título'),
}


def t(key: str, code: str, **values: Any) -> str:
    row = _T[key]
    return row[_CODES.index(code) if code in _CODES else 1].format(**values)


def _age(seconds: float, code: str) -> str:
    seconds = max(0, int(seconds))
    if seconds < 10:
        return t('now', code)
    if seconds < 120:
        return t('sec', code, n=seconds)
    return t('min', code, n=seconds // 60)


def _items(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = ' '.join(str(item or '').split()).strip(' •-*')
        if text and text.casefold() not in {x.casefold() for x in out}:
            out.append(text)
    return out


def details(obs: Observation, code: str) -> list[str]:
    """Facts of this snapshot that the short card left out (bounded)."""
    from jarvis.runtime import screen_card168 as card
    shown = obs.shown.casefold()
    lines: list[str] = []
    data = obs.data or {}
    for key in ('key_points', 'visible_text', 'problems', 'next_steps'):
        # The short card showed the first items of each list (screen_card168 limits).
        extra = _items(data.get(key))[card._LIMITS[key]:] if obs.data else []
        if extra:
            lines += ['', card.heading(key, code)]
            for item in extra[:MAX_ITEMS]:
                lines.append('• ' + ('«' + item.strip('«»"“”') + '»' if key == 'visible_text' else item))
    ocr = [x for x in obs.ocr if x.casefold() not in shown]
    if ocr:
        lines += ['', t('ocr', code)] + ['• ' + x for x in ocr[:MAX_ITEMS]]
        if len(ocr) > MAX_ITEMS:
            lines.append('• …')
    return lines


def continue_text(obs: Observation | None, code: str, *, config: Any = None, now: float | None = None,
                  state: str | None = None) -> str:
    if obs is None or (config is not None and not bool(config.get('vision.enabled', True))):
        return t('unavailable', code)
    now = time.monotonic() if now is None else float(now)
    age_s = now - obs.at
    age = _age(age_s, code)
    window = obs.window or obs.title or t('no_window', code)
    state = window_state(obs) if state is None else state
    out: list[str] = []
    if state == 'gone':
        out.append(t('gone', code, w=window, age=age))
    elif state == 'changed':
        out.append(t('changed', code))
    elif age_s > FRESH:
        out.append(t('old', code, age=age))
    monitor = t('monitor', code, n=obs.monitor) if obs.area == 'monitor' and str(obs.monitor).isdigit() else ''
    out.append(t('head_window', code, w=window, age=age) if obs.area == 'window'
               else t('head_monitor', code, m=monitor, w=window, age=age))
    body = details(obs, code)
    if body:
        out += body
    else:
        out += ['', t('nothing_more', code)]
    out += ['', t('bound_window', code, w=window) if obs.area == 'window' else t('bound_monitor', code, m=monitor)]
    if obs.ocr and len(obs.ocr) >= obs.ocr_limit:
        out.append(t('ocr_partial', code, n=obs.ocr_limit))
    text = '\n'.join(out).strip()
    obs.continued = text
    return text
