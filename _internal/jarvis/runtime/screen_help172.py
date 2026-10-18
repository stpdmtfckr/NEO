"""NEO 172: «что за ошибка?», «как пройти этот уровень?» — help with what is on the screen.

1. The window the user is looking at (the one under NEO) is captured and read *locally*:
   Windows OCR first; only when it finds nothing useful, the installed screen model
   describes the problem in one line (and is unloaded right after).
2. From that text NEO builds a short search query: the program, the error line(s), and
   «как исправить» / «как пройти». Personal data is removed from it first (e-mail
   addresses, user names in paths, long numbers, keys).
3. The screenshot never leaves the computer. Before the query is sent to SearchAPI.io
   NEO shows it and waits for «да» (unless the user switched that off).
4. The answer is the short, step-by-step web answer of NEO 171.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
PENDING_TTL = 120.0


def _code(code: str) -> str:
    code = str(code or 'ru').casefold().split('-')[0]
    return code if code in _LANGS else 'en'


def _pick(row: tuple[str, ...], code: str) -> str:
    return row[_LANGS.index(_code(code))]


def _n(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '').casefold().replace('ё', 'е')).strip(' .,!?:;¿¡«»"\'')


_B, _E = r'(?<![\w-])', r'(?![\w-])'
_HERE = (r'(?:на\s+экране|здесь|тут|это|этот|эта|эту|этой|этом|в\s+этом\s+окне|on\s+(?:the\s+)?screen|here|this|'
         r'auf\s+dem\s+bildschirm|hier|das|dieser?|dieses|à\s+l[\'’]écran|ici|ce|cette|cet|en\s+(?:la\s+)?pantalla|aquí|aqui|este|esta|esto|'
         r'sullo\s+schermo|qui|questo|questa|na\s+tela|no\s+ecr[aã]|isto|essa|esse)')

# Asking about something visible: an error, a problem, «how do I get past this».
_ASK_SCREEN = re.compile(_B + r'(?:'
    r'что\s+(?:это\s+)?за\s+(?:ошибк\w*|проблем\w*|окно|сообщени\w*)|что\s+(?:тут|здесь)\s+(?:не\s+так|случилось|за\s+ошибк\w*)|'
    r'почему\s+(?:это\s+)?(?:не\s+работает|вылетает|выдает\s+ошибку)|как\s+(?:это\s+)?(?:исправить|починить|решить)(?:\s+(?:это|эту\s+ошибку|ошибку))?|'
    r'помоги\s+(?:с\s+(?:этим|этой\s+ошибкой|ошибкой|этим\s+окном))|что\s+мне\s+(?:тут|здесь)\s+делать|'
    r'как\s+(?:пройти|пройду|победить|решить)\s+(?:этот|эту|это)\s+\w+|'
    r'what(?:\s+is|\'s)\s+(?:this|that)\s+(?:error|problem|message)|what\s+does\s+this\s+error\s+mean|why\s+(?:is\s+this|does\s+this)\s+\w+|'
    r'how\s+do\s+i\s+fix\s+(?:this|that)|help\s+(?:me\s+)?with\s+this(?:\s+error)?|how\s+do\s+i\s+(?:beat|pass|get\s+past)\s+this\s+\w+|'
    r'was\s+ist\s+das\s+für\s+ein\s+fehler|was\s+bedeutet\s+dieser\s+fehler|wie\s+behebe\s+ich\s+das|hilf\s+mir\s+damit|'
    r'c[\'’]est\s+quoi\s+cette\s+erreur|que\s+veut\s+dire\s+cette\s+erreur|comment\s+(?:je\s+)?(?:corrige|répare)\s+(?:ça|cela)|aide-moi\s+avec\s+(?:ça|cette\s+erreur)|'
    r'qu[eé]\s+(?:es\s+)?(?:este|ese)\s+error|c[oó]mo\s+(?:arreglo|soluciono)\s+esto|ay[uú]dame\s+con\s+esto|'
    r'che\s+errore\s+[eè]\s+questo|come\s+(?:risolvo|sistemo)\s+questo|aiutami\s+con\s+questo|'
    r'que\s+erro\s+[eé]\s+este|como\s+(?:corrijo|resolvo)\s+isto|ajuda-me\s+com\s+isto'
    r')' + _E, re.I)
_EXPLICIT_SCREEN = re.compile(_B + _HERE + _E, re.I)


def asks_about_screen(text: str) -> bool:
    """A request for help with what is visible right now (not a general how-to question)."""
    t = _n(text)
    if not t or len(t.split()) > 12 or ':' in t:
        return False            # «Help me with this: put the URL on the clipboard» is a request, not a question about the screen
    match = _ASK_SCREEN.search(t)
    if match is None or match.start() > 0:
        return False
    tail = t[match.end():].strip(' ,')
    return not tail or bool(re.fullmatch(r'(?:' + _HERE + r'\s*){1,3}', tail, re.I)) or len(tail.split()) <= 2


# ---- what is on the screen -------------------------------------------------------------------

_ERROR_WORDS = re.compile(
    r'(?:ошибк\w*|не\s+удалось|не\s+найден\w*|отказано|сбой|неизвестн\w+\s+ошибк\w*|прекращен\w*|не\s+отвечает|'
    r'error|failed|failure|cannot|can\'t|could\s+not|unable\s+to|not\s+found|missing|denied|exception|crash\w*|fatal|invalid|timed?\s*out|'
    r'fehler|fehlgeschlagen|nicht\s+gefunden|erreur|échec|introuvable|impossible|fallo|no\s+se\s+(?:pudo|encontr)|errore|impossibile|erro|falhou|'
    r'0x[0-9a-f]{6,8}|\bcode\s*[:#]?\s*\d{2,}|код\s*[:#]?\s*\d{2,}|\.dll\b|\.exe\b|\bhresult\b)', re.I)
_NOISE = re.compile(r'^(?:ok|ок|отмена|cancel|да|нет|yes|no|закрыть|close|справка|help|подробнее|details|\W+)$', re.I)
_OWN_WINDOW = re.compile(r'^(?:neo|jarvis)$', re.I)


@dataclass(slots=True)
class Reading:
    app: str = ''
    title: str = ''
    lines: list[str] = field(default_factory=list)     # the problem lines, most relevant first
    all_lines: int = 0
    method: str = ''                                  # ocr | vision | none
    game: bool = False


def app_name(process: str, title: str) -> str:
    """A readable program name: from the window title's last part, else the process."""
    parts = [p.strip() for p in re.split(r'\s+[-—–|]\s+', title or '') if p.strip()]
    candidate = parts[-1] if parts else ''
    if not candidate or len(candidate) > 40 or _OWN_WINDOW.match(candidate):
        candidate = re.sub(r'\.exe$', '', str(process or ''), flags=re.I)
    return candidate[:40]


def pick_lines(lines: list[str], *, limit: int = 3) -> list[str]:
    """The lines that look like the problem: error words and codes first, then the longest plain sentences."""
    clean = [' '.join(str(l).split()) for l in lines if str(l).strip()]
    clean = [l for l in clean if len(l) >= 4 and not _NOISE.match(l)]
    errors = [l for l in clean if _ERROR_WORDS.search(l)]
    if errors:
        # keep the order on screen: the headline usually comes first
        return [scrub(l)[:160] for l in errors[:limit]]   # scrub before cutting
    sentences = sorted((l for l in clean if len(l.split()) >= 3), key=len, reverse=True)
    return [scrub(l)[:160] for l in sentences[:1]]


_PRIVATE = (
    (re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+'), '<e-mail>'),
    (re.compile(r'([A-Za-z]:\\(?:Users|Пользователи|Documents and Settings)\\)[^\\\s"]+', re.I), r'\1…'),
    (re.compile(r'(/home/|/Users/)[^/\s"]+'), r'\1…'),
    (re.compile(r'\b(?:[A-Za-z0-9+/]{32,}|[A-Fa-f0-9]{32,})\b'), '<key>'),
    (re.compile(r'(?<![\w.])\d[\d \-]{9,}\d(?![\w.])'), '<number>'),
    (re.compile(r'(?:https?://)?\b(?:\d{1,3}\.){3}\d{1,3}\b'), '<ip>'),
)


def scrub(text: str) -> str:
    """Remove personal data before anything leaves the computer."""
    value = str(text or '')
    for pattern, replacement in _PRIVATE:
        value = pattern.sub(replacement, value)
    # REC-PRIVACY-SOURCE-DATA-01 R3: the general credential policy (api_key=…,
    # bearer, bot tokens, passwords) applies here too, on the whole text.
    try:
        from jarvis.runtime.diagnostics import redact_credentials
        value = redact_credentials(value)
    except Exception:
        pass
    return value


_FIX = ('как исправить', 'how to fix', 'wie beheben', 'comment corriger', 'cómo solucionar', 'come risolvere', 'como corrigir')
_PASS = ('как пройти', 'how to beat', 'wie schafft man', 'comment réussir', 'cómo pasar', 'come superare', 'como passar')
_PASS_WORDS = re.compile(r'(?:пройти|пройду|победить|beat|pass|get\s+past|schaffe|réussir|pasar|superare|passar)', re.I)


def build_query(reading: Reading, question: str, code: str) -> str:
    """«<program> <problem text> как исправить» — personal data removed, at most ~200 characters."""
    wants_pass = bool(_PASS_WORDS.search(_n(question))) or reading.game
    tail = _pick(_PASS if wants_pass else _FIX, code)
    body = ' '.join(reading.lines[:2]) if reading.lines else ''
    body = re.sub(r'[A-Za-z]:\\(?:[^\\\s"()]+\\)+([^\\\s"()]+)', r'\1', body)      # a full path → only the file name
    body = re.sub(r'\s+', ' ', body).strip(' .,:;')
    app = reading.app
    if app and body and app.casefold() in body.casefold():
        app = ''
    query = ' '.join(x for x in (app, scrub(body)[:170], tail) if x)
    return scrub(query).strip()


# ---- capture (Windows) ---------------------------------------------------------------------

def _target_window():
    from jarvis.runtime import screen_targets171 as targets
    rows = targets.windows(limit=6)
    return rows[0] if rows else None


def read_screen(*, ollama: Any = None, vision: bool = True,
                grab: Callable[..., Any] | None = None, lines_of: Callable[[Any], list[str]] | None = None,
                window: Any = None) -> Reading:
    """Read the window under NEO. OCR first; the screen model only when OCR found nothing useful."""
    from jarvis.runtime import screen_ocr168
    grab = grab or screen_ocr168.grab
    lines_of = lines_of or (lambda image: screen_ocr168.text_lines(image, limit=120))
    window = window if window is not None else _target_window()
    if window is None:
        return Reading(method='none')
    left, top, right, bottom = window.rect
    image = grab(left, top, max(1, right - left), max(1, bottom - top))
    lines = lines_of(image)
    reading = Reading(app_name(window.process, window.title), window.title, pick_lines(lines), len(lines), 'ocr', bool(window.game))
    if reading.lines or not vision or ollama is None:
        if not reading.lines:
            reading.method = 'none'
        return reading
    described = describe_with_model(ollama, image)
    if described:
        reading.lines = [described]
        reading.method = 'vision'
    else:
        reading.method = 'none'
    return reading


_VISION_PROMPT = ('Look at this window. If it shows an error, a problem or a task the user may be stuck on, write ONE short line '
                  'that could be typed into a search engine to find the fix (program name, error text or code, level or task name). '
                  'Write only that line, without quotes. If nothing is wrong, write NONE.')


def describe_with_model(ollama: Any, image: Any) -> str:
    """One search-like line from the installed screen model; it is unloaded right after (keep_alive 0)."""
    if not hasattr(ollama, 'analyze_image'):
        return ''
    path = Path(tempfile.gettempdir()) / f'neo_help_{int(time.time() * 1000)}.png'
    try:
        image.save(path)
        raw = str(ollama.analyze_image(path, _VISION_PROMPT, num_predict=60, keep_alive=0) or '').strip()
    except Exception:
        logger.info('Screen model could not describe the window', exc_info=True)
        return ''
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    line = raw.splitlines()[0].strip(' "\'«»') if raw else ''
    return '' if not line or line.upper().startswith('NONE') else scrub(line)[:160]


# ---- the question before sending --------------------------------------------------------------

_TEXT = {
    'confirm': ('Вижу в окне «{app}»: {what}\nОтправлю в поиск: «{query}» (сам снимок экрана не отправляется). Искать?',
                'In “{app}” I see: {what}\nI’ll search for: “{query}” (the screenshot itself is not sent). Search?',
                'Im Fenster „{app}“ sehe ich: {what}\nIch suche nach: „{query}“ (der Screenshot selbst wird nicht gesendet). Suchen?',
                'Dans « {app} », je vois : {what}\nJe vais chercher : « {query} » (la capture elle-même n’est pas envoyée). Je cherche ?',
                'En «{app}» veo: {what}\nBuscaré: «{query}» (la captura en sí no se envía). ¿Busco?',
                'In «{app}» vedo: {what}\nCercherò: «{query}» (lo screenshot non viene inviato). Cerco?',
                'Em «{app}» vejo: {what}\nVou pesquisar: «{query}» (a captura em si não é enviada). Pesquiso?'),
    'nothing': ('Не нашёл на экране текста ошибки в окне «{app}». Скажите, что именно не получается, — я поищу решение.',
                'I found no error text in “{app}”. Tell me what isn’t working and I’ll look for a fix.',
                'Im Fenster „{app}“ finde ich keinen Fehlertext. Sag mir, was nicht klappt, dann suche ich eine Lösung.',
                'Je ne trouve pas de texte d’erreur dans « {app} ». Dites-moi ce qui ne marche pas et je chercherai une solution.',
                'No encuentro texto de error en «{app}». Dime qué no funciona y buscaré una solución.',
                'Non trovo testo di errore in «{app}». Dimmi cosa non funziona e cercherò una soluzione.',
                'Não encontro texto de erro em «{app}». Diz-me o que não funciona e procuro uma solução.'),
    'no_window': ('Не вижу окна, о котором речь. Откройте его и спросите ещё раз.', 'I can’t see the window you mean. Bring it up and ask again.',
                  'Ich sehe das Fenster nicht. Hol es nach vorn und frag noch einmal.', 'Je ne vois pas la fenêtre. Affichez-la et redemandez.',
                  'No veo la ventana. Ábrela y vuelve a preguntar.', 'Non vedo la finestra. Portala in primo piano e chiedi di nuovo.',
                  'Não vejo a janela. Abre-a e pergunta de novo.'),
    'cancelled': ('Хорошо, ничего не отправляю.', 'OK, nothing is sent.', 'Okay, ich sende nichts.', 'D’accord, je n’envoie rien.',
                  'De acuerdo, no envío nada.', 'Va bene, non invio nulla.', 'Está bem, não envio nada.'),
    'unsupported': ('Снимок окна доступен только в Windows.', 'Reading a window works only on Windows.', 'Fenster lesen geht nur unter Windows.',
                    'La lecture d’une fenêtre ne fonctionne que sous Windows.', 'Leer una ventana solo funciona en Windows.',
                    'La lettura di una finestra funziona solo su Windows.', 'Ler uma janela só funciona no Windows.'),
}
_YES = re.compile(r'^(?:да|ага|угу|давай|ищи|ок(?:ей)?|конечно|отправляй|yes|yeah|yep|sure|ok(?:ay)?|go(?:\s+ahead)?|search|ja|such|oui|vas-y|cherche|s[ií]|busca|dale|certo|cerca|sim|procura)(?:\s+\w+)?$', re.I)
_NO = re.compile(r'^(?:нет|не\s+надо|не\s+нужно|не\s+отправляй|отмена|no|nope|don\'t|cancel|nein|non|annule|n[aã]o|cancela|annulla)(?:\s+\w+)?$', re.I)
# NEO 187 HREQ-014: «да да», «да, верно», «это то, что я имел в виду» agree;
# «да нет», «не знаю» and negations never become consent.
from jarvis.runtime.confirm_words187 import guard_no as _guard_no187, guard_yes as _guard_yes187
_YES, _NO = _guard_yes187(_YES), _guard_no187(_NO)


def t(key: str, code: str, /, **values: Any) -> str:
    return _pick(_TEXT[key], code).format(**values)


@dataclass(slots=True)
class _Pending:
    query: str = ''
    at: float = 0.0
    owner_scope: str = ''
    question_token: object | None = None


_lock = threading.Lock()
_pending = _Pending()


def ask(query: str) -> None:
    global _pending
    from jarvis.runtime.conversation_journal import current_scope
    with _lock:
        _pending = _Pending(query, time.monotonic(), current_scope(), object())


def pending() -> bool:
    with _lock:
        return bool(_pending.query) and time.monotonic() - _pending.at <= PENDING_TTL


def forget() -> None:
    global _pending
    with _lock:
        _pending = _Pending()


def owned_question_token() -> object | None:
    """Snapshot a live question created in this conversation; never infer its owner."""
    from jarvis.runtime.conversation_journal import current_scope
    with _lock:
        if (_pending.owner_scope == current_scope() and bool(_pending.query)
                and time.monotonic() - _pending.at <= PENDING_TTL):
            return _pending.question_token
    return None


def forget_if_owned(token: object | None) -> bool:
    """Clear only the same owned generation, preserving any replacement question."""
    global _pending
    if token is None:
        return False
    from jarvis.runtime.conversation_journal import current_scope
    with _lock:
        if (_pending.owner_scope != current_scope() or _pending.question_token is not token
                or not (bool(_pending.query))):
            return False
        _pending = _Pending()
        return True


def resolve(text: str) -> str | None:
    """'send' + query → the query; 'no' → ''; anything else → None (a new request, the question is dropped)."""
    if not pending():
        return None
    t_ = _n(text)
    with _lock:
        query = _pending.query
    if _YES.match(t_):
        forget()
        return query
    if _NO.match(t_):
        forget()
        return ''
    return None


__all__ = ['Reading', 'app_name', 'ask', 'asks_about_screen', 'build_query', 'describe_with_model', 'forget', 'forget_if_owned', 'owned_question_token', 'pending',
           'pick_lines', 'read_screen', 'resolve', 'scrub', 't']
