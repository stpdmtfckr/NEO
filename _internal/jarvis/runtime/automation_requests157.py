"""Bounded, deterministic task-list input with reviewed child operations.

No models, arbitrary JSON plans, destructive operations, send actions or nested
plans. Every clause must be understood before anything can run. The ordinary
registry still applies schemas, ownership, transport policy and cancellation.
"""
from __future__ import annotations

import re
from jarvis.tools.argument_validation import normalize_tool_call

MAX_TASKS = 8
READ_ACTIONS = {
    'hardware': {'status'},
    'system_monitor': {'summary', 'cpu', 'gpu', 'memory', 'disks', 'network', 'processes'},
    'filesystem': {'list', 'info', 'exists', 'disk_usage', 'tree', 'hash', 'read_text', 'list_drives'},
    'application': {'list', 'list_active', 'info', 'find'},
    'search': {'files', 'content'},
    'email': {'status', 'unread', 'latest', 'search', 'read'},
    'window': {'active', 'list'},
    'browser': {'context', 'list_tabs'},
    'shelf': {'list', 'read', 'get'},
    'timer': {'list'},
    'briefing': {'current_clock', 'current_date'},
    'weather': {'summary', 'forecast'},
    'music': {'background_status', 'queue_status'},
    'yandex_music': {'background_status', 'queue_status'},
    'spotify': {'background_status', 'queue_status'},
}
QUEUE_OPEN_ACTIONS = {'application': {'open'}, 'desktop': {'open'}, 'browser': {'open_url'}}
REQUIRED = {
    ('application', 'open'): ('name',), ('application', 'info'): ('name',),
    ('application', 'find'): ('name',), ('desktop', 'open'): ('query',),
    ('browser', 'open_url'): ('value',), ('search', 'files'): ('query',),
    ('search', 'content'): ('query',), ('email', 'search'): ('query',),
    ('email', 'read'): ('message_id',), ('shelf', 'read'): ('title',),
    ('shelf', 'get'): ('title',),
}
for _action in ('info', 'exists', 'tree', 'hash', 'read_text'):
    REQUIRED[('filesystem', _action)] = ('source',)

SEPARATORS = {
    'ru': r'(?:и\s+)?(?:затем|потом)|после этого',
    'en': r'(?:and\s+)?then|after that',
    'de': r'(?:und\s+)?dann|danach',
    'fr': r'(?:et\s+)?puis|ensuite',
    'es': r'(?:y\s+)?(?:luego|después)|a continuación',
    'it': r'(?:e\s+)?poi|dopo di che',
    'pt': r'(?:e\s+)?depois|em seguida',
}


def reviewed_call(tool: str, arguments: dict, action: str = 'queue') -> bool:
    if action not in {'queue', 'parallel'} or not isinstance(arguments, dict):
        return False
    child_action = arguments.get('action')
    allowed = child_action in READ_ACTIONS.get(tool, set())
    if action == 'queue':
        allowed = allowed or child_action in QUEUE_OPEN_ACTIONS.get(tool, set())
    if not allowed:
        return False
    if any(not str(arguments.get(name, '')).strip() for name in REQUIRED.get((tool, child_action), ())):
        return False
    if tool == 'browser' and child_action == 'open_url':
        from urllib.parse import urlsplit
        try:
            url = urlsplit(str(arguments.get('value', '')))
            return url.scheme in {'http', 'https'} and bool(url.hostname) and not url.username and not url.password
        except ValueError:
            return False
    return True


def parse_calls(value: str, code: str, parse_complete, *, action: str = 'queue') -> list[dict] | None:
    """Interpret only explicit list boundaries, preserving each literal payload."""
    if code not in SEPARATORS or action not in {'queue', 'parallel'}:
        return None
    value = str(value).strip()
    if not value or len(value) > 4000 or '\x00' in value:
        return None
    # A quoted command is reported text, not an executable task list. This also
    # prevents a separator inside a filename/title being treated as a new task.
    if any(mark in value for mark in ('"', '«', '»', '“', '”')):
        return None
    separator = r'\s*(?:;|\n|,?\s+\b(?:' + SEPARATORS[code] + r')\b\s+)\s*'
    parts = re.split(separator, value, flags=re.I)
    if not 2 <= len(parts) <= MAX_TASKS or any(not part.strip() for part in parts):
        return None
    calls = []
    from jarvis.runtime.natural_commands import negated_action
    for part in parts:
        part = part.strip()
        if negated_action(part, code):
            return None
        parsed = parse_complete(part)
        if not parsed or not isinstance(parsed[1], dict):
            return None
        tool, arguments = normalize_tool_call(parsed[0], dict(parsed[1]))
        if not reviewed_call(tool, arguments, action):
            return None
        calls.append({'tool': tool, 'arguments': arguments})
    return calls


def execute_reviewed_child(registry, name, arguments):
    """Bind one preflighted child, including in a new parallel worker thread."""
    from jarvis.routing.ownership_firewall import OwnershipClaim, ownership_claim_scope
    from jarvis.security import Risk
    if not reviewed_call(name, arguments) or registry.risk_for(name, arguments) > Risk.MEDIUM:
        raise ValueError('Unsupported automation child')
    claim = OwnershipClaim(owner='native:' + name, confidence=1.0,
        reason='reviewed bounded automation child', allowed_tools=(name,), schema_tools=(name,), enforced=True)
    with ownership_claim_scope(claim):
        return registry.execute(name, arguments)


def plan_error(code, *, unsupported=False):
    rows = {
        'ru': ('Не удалось проверить весь список задач. Ни одна задача не запущена.', 'В очереди поддерживаются чтение и открытие, параллельно — только чтение. Остальные действия выполни отдельными командами.'),
        'en': ('I could not validate the entire task list. No tasks were started.', 'Queues support reading and opening; parallel tasks support reading only. Request other actions separately.'),
        'de': ('Ich konnte nicht die gesamte Aufgabenliste prüfen. Keine Aufgabe wurde gestartet.', 'Nacheinander sind Lesen und Öffnen möglich, parallel nur Lesen. Andere Aktionen bitte einzeln anfordern.'),
        'fr': ('Je n’ai pas pu vérifier toute la liste. Aucune tâche n’a démarré.', 'La séquence accepte la lecture et l’ouverture ; le mode parallèle accepte seulement la lecture. Demande les autres actions séparément.'),
        'es': ('No pude validar toda la lista. No se inició ninguna tarea.', 'La secuencia admite lectura y apertura; en paralelo, solo lectura. Solicita las demás acciones por separado.'),
        'it': ('Non ho potuto verificare l’intero elenco. Nessuna attività è stata avviata.', 'La sequenza supporta lettura e apertura; in parallelo, solo lettura. Richiedi le altre azioni separatamente.'),
        'pt': ('Não consegui verificar toda a lista. Nenhuma tarefa foi iniciada.', 'A sequência permite leitura e abertura; em paralelo, apenas leitura. Pede as outras ações separadamente.'),
    }
    base, detail = rows.get(code, rows['en'])
    return base + (' ' + detail if unsupported else '')


def result_text(code, action, results, planned):
    labels = {
        'ru': ('Последовательность', 'Параллельные задачи', 'выполнено'),
        'en': ('Sequence', 'Parallel tasks', 'completed'),
        'de': ('Reihenfolge', 'Parallele Aufgaben', 'abgeschlossen'),
        'fr': ('Séquence', 'Tâches parallèles', 'terminées'),
        'es': ('Secuencia', 'Tareas en paralelo', 'completadas'),
        'it': ('Sequenza', 'Attività parallele', 'completate'),
        'pt': ('Sequência', 'Tarefas paralelas', 'concluídas'),
    }
    queue, parallel, completed = labels.get(code, labels['en'])
    title = queue if action == 'queue' else parallel
    count = sum(bool(row['success']) for row in results)
    lines = [f'{title}: {completed} {count}/{planned}.']
    lines.extend(f'{i}. {row["message"]}' for i, row in enumerate(results, 1) if row.get('message'))
    return '\n'.join(lines)
