"""NEO 169: multi-step tasks built from registered NEO commands, with a check after every step.

«открой Блокнот, затем напечатай привет, затем нажми Enter»
1. The request is split at explicit sequence words («затем», «потом», «после этого», «then», …).
2. Every part must be an existing NEO command; otherwise nothing runs.
3. NEO shows the short plan, then runs the steps one by one through the normal
   executor (tool checks, permissions, confirmations).
4. After each step the observable result is checked (the window appeared, the
   clipboard changed, the input was sent to the verified target…). A step that
   raised no error but cannot be confirmed stops the task with an explanation.
5. Only safe, repeatable steps are retried (once), and only after checking the
   step has not already happened; sending, deleting or paying is never repeated.
6. «стоп» / «отмени задачу» cancels the task between or during steps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

SEPARATORS = {
    'ru': r'\s*,?\s+(?:а\s+)?(?:затем|потом|после\s+этого|и\s+затем|и\s+потом|после\s+чего)\s+',
    'en': r'\s*,?\s+(?:and\s+)?(?:then|after\s+that|afterwards)\s+',
    'de': r'\s*,?\s+(?:und\s+)?(?:dann|danach|anschließend)\s+',
    'fr': r'\s*,?\s+(?:et\s+)?(?:puis|ensuite|après\s+ça)\s+',
    'es': r'\s*,?\s+(?:y\s+)?(?:luego|después|a\s+continuación)\s+',
    'it': r'\s*,?\s+(?:e\s+)?(?:poi|dopo|quindi)\s+',
    'pt': r'\s*,?\s+(?:e\s+)?(?:depois|a\s+seguir|em\s+seguida)\s+',
}
# Steps that can be repeated without a second side effect (after checking they did not happen).
REPEATABLE = {('application', 'open'), ('window', 'focus'), ('browser_nav', 'reload'), ('browser_nav', 'address'),
              ('ui', 'hints'), ('ui', 'read'), ('screen', 'analyze')}
# Local, deterministic tools whose own success already reports the observable state.
SELF_VERIFYING = {'memory', 'knowledge', 'timer', 'shelf', 'overlay', 'plugins', 'neo_model', 'scenario', 'weather', 'date_info',
                  'chance', 'system_monitor', 'briefing', 'translate', 'history', 'diagnostics', 'clipboard', 'copy', 'screen'}
MAX_STEPS = 8

_TEXT = {
    'plan': ('План ({count} шага):', 'Plan ({count} steps):', 'Plan ({count} Schritte):', 'Plan ({count} étapes) :', 'Plan ({count} pasos):',
             'Piano ({count} passi):', 'Plano ({count} passos):'),
    'ok': ('✓', '✓', '✓', '✓', '✓', '✓', '✓'),
    'done': ('Готово: все шаги выполнены и проверены.', 'Done: every step was carried out and checked.', 'Fertig: alle Schritte ausgeführt und geprüft.',
             'Terminé : toutes les étapes ont été faites et vérifiées.', 'Hecho: todos los pasos se hicieron y comprobaron.',
             'Fatto: tutti i passi eseguiti e verificati.', 'Feito: todos os passos executados e verificados.'),
    'failed': ('Остановился на шаге {n} («{step}»): {reason}. Остальные шаги не выполнялись.',
               'Stopped at step {n} (“{step}”): {reason}. The remaining steps were not run.',
               'Bei Schritt {n} („{step}“) angehalten: {reason}. Die übrigen Schritte liefen nicht.',
               'Arrêt à l’étape {n} (« {step} ») : {reason}. Les étapes suivantes n’ont pas été faites.',
               'Me detuve en el paso {n} («{step}»): {reason}. Los demás pasos no se hicieron.',
               'Fermato al passo {n} («{step}»): {reason}. Gli altri passi non sono stati eseguiti.',
               'Parei no passo {n} («{step}»): {reason}. Os restantes passos não foram feitos.'),
    'unverified': ('действие отправлено, но я не смог убедиться, что оно сработало', 'the action was sent, but I could not confirm it worked',
                   'die Aktion wurde gesendet, aber die Wirkung ist nicht bestätigt', 'l’action a été envoyée mais son effet n’est pas confirmé',
                   'la acción se envió, pero no pude confirmar que funcionó', 'l’azione è stata inviata ma non ho potuto verificarla',
                   'a ação foi enviada, mas não consegui confirmar que funcionou'),
    'cancelled': ('Задача отменена после шага {n}.', 'Task cancelled after step {n}.', 'Aufgabe nach Schritt {n} abgebrochen.',
                  'Tâche annulée après l’étape {n}.', 'Tarea cancelada tras el paso {n}.', 'Compito annullato dopo il passo {n}.',
                  'Tarefa cancelada após o passo {n}.'),
    'confirm': ('Шаг {n} («{step}») требует подтверждения — выполните его отдельной командой. Задача остановлена перед ним.',
                'Step {n} (“{step}”) needs confirmation — run it as a separate command. The task stopped before it.',
                'Schritt {n} („{step}“) braucht eine Bestätigung — bitte einzeln ausführen. Die Aufgabe hielt davor an.',
                'L’étape {n} (« {step} ») demande une confirmation — lancez-la séparément. La tâche s’est arrêtée avant.',
                'El paso {n} («{step}») necesita confirmación: ejecútalo por separado. La tarea se detuvo antes.',
                'Il passo {n} («{step}») richiede conferma: eseguilo separatamente. Il compito si è fermato prima.',
                'O passo {n} («{step}») precisa de confirmação: execute-o à parte. A tarefa parou antes dele.'),
    'unknown_step': ('Не понял шаг «{step}». В сценарии могут быть только команды, которые NEO уже знает — скажите этот шаг иначе.',
                     'I did not understand the step “{step}”. A routine can only contain commands NEO knows — say that step differently.',
                     'Den Schritt „{step}“ verstehe ich nicht. Eine Routine enthält nur bekannte Befehle.',
                     'Je n’ai pas compris l’étape « {step} ». Une routine ne contient que des commandes connues.',
                     'No entendí el paso «{step}». Una rutina solo puede tener comandos conocidos.',
                     'Non ho capito il passo «{step}». Una routine può contenere solo comandi noti.',
                     'Não percebi o passo «{step}». Uma rotina só pode ter comandos conhecidos.'),
    'retried': ('(повторил один раз)', '(retried once)', '(einmal wiederholt)', '(répété une fois)', '(repetido una vez)', '(ripetuto una volta)', '(repetido uma vez)'),
}
_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def t(key: str, code: str, /, **values: Any) -> str:
    return _TEXT[key][_CODES.index(code) if code in _CODES else 1].format(**values)


@dataclass
class Step:
    text: str
    tool: str
    arguments: dict[str, Any]
    status: str = 'planned'       # planned | done | failed | skipped
    detail: str = ''
    attempts: int = 0


@dataclass
class TaskResult:
    steps: list[Step] = field(default_factory=list)
    ok: bool = False
    text: str = ''


def split(text: str, code: str) -> list[str]:
    pattern = SEPARATORS.get(code)
    if not pattern:
        return [text]
    parts = [p.strip(' ,.;') for p in re.split(pattern, str(text or '').strip(), flags=re.I)]
    return [p for p in parts if p]


# NEO 170: «открой браузер, загугли X и перейди по первой ссылке» — a comma or «и»
# separates steps only when the next words start a command (a known command verb);
# «найди Том и Джерри» or «напиши привет, как дела» stay one step.
SOFT_SEPARATORS = {'ru': r'\s*,\s*|\s+и\s+', 'en': r'\s*,\s*|\s+and\s+', 'de': r'\s*,\s*|\s+und\s+', 'fr': r'\s*,\s*|\s+et\s+',
                   'es': r'\s*,\s*|\s+y\s+', 'it': r'\s*,\s*|\s+e\s+', 'pt': r'\s*,\s*|\s+e\s+'}


# NEW185-COMPOUND-POLITENESS-BOUNDARY: courtesy words are neither a step of their
# own nor a reason to keep two commands together.
_POLITE = (r"(?:please|pls|bitte|пожалуйста|плиз|s['’]il\s+te\s+pla[îi]t|s['’]il\s+vous\s+pla[îi]t|por\s+favor|"
           r"per\s+favore|then|and\s+then|потом|затем|dann|danach|puis|ensuite|luego|después|poi|dopo|depois)")
_POLITE_ONLY = re.compile(r"^\s*(?:" + _POLITE + r"[\s,.!]*)+$", re.I)
_POLITE_LEAD = re.compile(r"^\s*(?:" + _POLITE + r")\s*,?\s+", re.I)


def split_soft(text: str, code: str, verbs: set[str]) -> list[str]:
    result: list[str] = []
    for part in split(text, code):
        pieces = [p.strip(' ,.;') for p in re.split(SOFT_SEPARATORS.get(code, r'\s*,\s*'), part, flags=re.I)]
        joiners = re.findall(SOFT_SEPARATORS.get(code, r'\s*,\s*'), part, flags=re.I)
        current = pieces[0]
        for joiner, piece in zip(joiners, pieces[1:]):
            bare = _POLITE_LEAD.sub('', piece, count=1)
            first = bare.split()[0].casefold() if bare.split() else ''
            if first and first in verbs and current and not _POLITE_ONLY.match(current):
                result.append(current)
                current = bare
            elif current and _POLITE_ONLY.match(current) and first in verbs:
                current = bare
            else:
                current = f'{current}{joiner}{piece}' if piece else current
        if current:
            result.append(current)
    return [p for p in result if p]


def _first_index(value: Any, code: str) -> bool:
    try:
        from jarvis.runtime import input168
        return input168.parse_ordinal(str(value), code) == 1
    except Exception:
        return str(value).strip() in {'1', 'first'}


def compact(steps: list[Step], code: str) -> list[Step]:
    """Merge steps that one NEO command already does better.

    * «открой браузер» right before a search or a URL: the search opens the browser itself;
    * «загугли X» + «перейди по первой ссылке» -> open the first Google result for X
      (the first link of the results page, not the page's own navigation links).
    """
    from jarvis.runtime.web_steps170 import is_browser_open
    out: list[Step] = []
    for step in steps:
        action = str(step.arguments.get('action', ''))
        if (out and out[-1].tool == 'browser' and out[-1].arguments.get('action') == 'search'
                and step.tool == 'ui' and action == 'click_link' and _first_index(step.arguments.get('index', 1), code)):
            previous = out.pop()
            out.append(Step(f'{previous.text}, {step.text}', 'browser',
                            {'action': 'search_first', 'value': previous.arguments.get('value', '')}))
            continue
        out.append(step)
    result: list[Step] = []
    for index, step in enumerate(out):
        following = out[index + 1] if index + 1 < len(out) else None
        if (following is not None and is_browser_open(step.tool, step.arguments) and following.tool == 'browser'
                and following.arguments.get('action') in {'search', 'search_first', 'open_url', 'open'}
                and str(following.arguments.get('value', '') or '').strip()):
            continue
        result.append(step)
    return result


def plan(text: str, code: str, parse: Callable[[str], tuple[str, dict[str, Any]] | None],
         verbs: set[str] | None = None) -> list[Step] | None:
    """Steps when the request is a sequence of known commands; None otherwise (nothing runs)."""
    candidates = [split(text, code)]
    if verbs:
        soft = split_soft(text, code, verbs)
        if soft != candidates[0]:
            candidates.insert(0, soft)
    for parts in candidates:
        if len(parts) < 2 or len(parts) > MAX_STEPS:
            continue
        steps = []
        for part in parts:
            parsed = parse(part)
            if parsed is None:
                steps = None
                break
            steps.append(Step(part, parsed[0], dict(parsed[1])))
        if steps:
            return compact(steps, code)
    return None


# -------------------------------------------------------------- verification
def _window_titles() -> list[str]:
    try:
        import pygetwindow
        return [w.title for w in pygetwindow.getAllWindows() if getattr(w, 'visible', True) and w.title]
    except Exception:
        return []


def _wait(predicate: Callable[[], bool], timeout: float, cancelled: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancelled():
            return False
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def verify(step: Step, result: Any, cancelled: Callable[[], bool] = lambda: False) -> bool | None:
    """True: the observable result is confirmed; False: it did not happen; None: cannot be checked."""
    data = result.data if isinstance(getattr(result, 'data', None), dict) else {}
    if not getattr(result, 'success', False):
        return False
    if step.tool in SELF_VERIFYING:
        return True
    if any(key.startswith('verified') and value for key, value in data.items()):
        return True
    action = str(step.arguments.get('action', ''))
    if step.tool == 'application' and action == 'open':
        name = str(step.arguments.get('name', '')).casefold()
        if not name:
            return None
        return _wait(lambda: any(name in title.casefold() for title in _window_titles()), 8.0, cancelled) or None
    if step.tool == 'window' and action in {'focus', 'activate'}:
        return True if data.get('title') or data.get('hwnd') else None
    if step.tool == 'browser' and action == 'search_first':
        # The tool itself clicked the first result and reports the page it opened.
        return True if (data.get('title') or data.get('url') or data.get('href')) else None
    if step.tool == 'browser' and action in {'search', 'open_url', 'open'}:
        # The browser window title shows the results page («котики - Поиск в Google») or the site.
        value = str(step.arguments.get('value', '') or '').casefold()
        if action == 'search':
            words = [w for w in re.findall(r'\w{3,}', value)][:3]
        else:
            host = re.sub(r'^[a-z]+://', '', value).split('/')[0]
            words = [part for part in host.split('.') if len(part) > 2 and part not in {'www', 'com', 'org', 'net'}][:1]
        if not words:
            return None
        return _wait(lambda: any(all(w in title.casefold() for w in words) for title in _window_titles()), 10.0, cancelled) or None
    return None


def already_done(step: Step) -> bool:
    """Before a retry: is the result already there (so repeating would duplicate it)?"""
    if step.tool == 'application' and step.arguments.get('action') == 'open':
        name = str(step.arguments.get('name', '')).casefold()
        return bool(name) and any(name in title.casefold() for title in _window_titles())
    return False


def run(steps: list[Step], code: str, execute: Callable[[Step], Any], *, needs_confirmation: Callable[[Step], bool],
        cancelled: Callable[[], bool]) -> TaskResult:
    result = TaskResult(steps)
    lines = [t('plan', code, count=len(steps))] + [f'{i}. {s.text}' for i, s in enumerate(steps, 1)]
    if len(steps) == 1:
        lines = ['', f'1. {steps[0].text}']      # no «План (1 шага)» header for a single command
    for number, step in enumerate(steps, 1):
        if cancelled():
            lines.append(t('cancelled', code, n=number - 1))
            result.text = '\n'.join(lines)
            return result
        if needs_confirmation(step):
            step.status = 'skipped'
            lines.append(t('confirm', code, n=number, step=step.text))
            result.text = '\n'.join(lines)
            return result
        outcome = None
        while step.attempts < (2 if (step.tool, str(step.arguments.get('action', ''))) in REPEATABLE else 1):
            if step.attempts and already_done(step):
                outcome = True
                break
            step.attempts += 1
            execution = execute(step)
            step.detail = str(getattr(execution, 'text', '') or '')[:300]
            outcome = verify(step, execution, cancelled)
            if outcome:
                break
            if cancelled():
                break
        if cancelled():
            lines.append(t('cancelled', code, n=number - 1))
            result.text = '\n'.join(lines)
            return result
        if not outcome:
            step.status = 'failed'
            reason = t('unverified', code) if outcome is None else (step.detail or '—')
            lines.append(t('failed', code, n=number, step=step.text, reason=reason))
            result.text = '\n'.join(lines)
            return result
        step.status = 'done'
        lines[number] = f"{number}. {step.text} {t('ok', code)}" + (f" {t('retried', code)}" if step.attempts > 1 else '')
    result.ok = True
    if len(steps) == 1 and steps[0].detail:
        # One command after merging («загугли X и открой первую ссылку»): its own answer is the report.
        result.text = steps[0].detail
        return result
    lines.append(t('done', code))
    result.text = '\n'.join(lines)
    return result
