"""Context-local briefing events and bounded read-only section collection.

Callbacks are NOT tool arguments; permissions still enter through ToolRegistry.
Timed-out reads may finish in daemon threads, but cannot publish a late UI update.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
import queue
import re
import threading
import time

_CONTEXT = ContextVar('neo_briefing_stream', default=None)
# A hung third-party read must not create another unbounded set of threads on
# every repeated "briefing" request. Slots live until the actual read exits.
_READ_SLOTS = threading.BoundedSemaphore(8)
_READ_LOCK = threading.Lock()
_ACTIVE_READS = set()
_READ_IDENTITIES = {'service.gmail': 'mail.gmail', 'service.mailru': 'mail.mailru',
                    'service.google_calendar': 'calendar', 'service.openweather': 'weather'}
RETRY_DELAYS = (1.0, 2.0)
BRIEFING_TIMEOUT_SECONDS = 48.0
STARTUP_TIMEOUT_MS = int((BRIEFING_TIMEOUT_SECONDS + 3) * 1000)


def section_failure(exc):
    """Classify legacy adapter exceptions without exposing credentials or URLs."""
    text = str(exc).casefold()
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None)
    match = re.search(r'\bhttp\s+(\d{3})\b', text)
    if status is None and match:
        status = int(match.group(1))
    configuration = (status in {400, 401, 403, 404} or any(marker in text for marker in (
        'not configured', 'invalid api key', 'unauthorized', 'invalid_grant',
        'invalid_client', 'unauthorized_client', 'authenticationfailed',
        'authentication failed', 'не настроен', 'не авторизован',
        'отключён в настройках', 'api не включён', 'api отключён', 'не подключены',
        'пустой access token', 'не указан', 'укажите адрес и пароль')))
    # Safe categories make a one-attempt OAuth/configuration failure diagnosable
    # without storing provider text, account identifiers, URLs or credentials.
    failure_kind = ('authentication' if status == 401 or any(x in text for x in ('invalid_grant', 'unauthorized', 'authentication failed', 'authenticationfailed'))
                    else 'forbidden' if status == 403 else 'rate_limited' if status == 429
                    else 'configuration' if configuration
                    else 'network' if isinstance(exc, (TimeoutError, ConnectionError, OSError)) or type(exc).__name__ in {'ReadTimeout', 'ConnectTimeout', 'SSLError', 'ConnectionError'}
                    else 'unavailable')
    result = {'error_type': type(exc).__name__, 'retryable': not configuration,
              'reason': 'configuration' if configuration else 'unavailable',
              'failure_kind': failure_kind}
    if isinstance(status, int):
        result['http_status'] = status
    return result


def validate_section_result(value):
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return {'error_type': 'InvalidSectionResult', 'retryable': True,
                'reason': 'unavailable'}
    payload = value[1]
    if isinstance(payload, dict) and payload.get('partial') and payload.get('unavailable_folders'):
        return {'error_type': 'PartialSectionResult', 'retryable': True,
                'reason': 'unavailable', 'partial_result': value}
    if isinstance(payload, dict) and 'service' in payload:
        from jarvis.integrations.service_checks161 import validation
        return validation(value)
    return None

@contextmanager
def stream_to(callback, cancel=None):
    token = _CONTEXT.set((callback, cancel))
    try:
        yield
    finally:
        _CONTEXT.reset(token)

def emit(section: str, text: str, state: str = 'ready', *, partial_text=None) -> None:
    context = _CONTEXT.get()
    if context and not (context[1] is not None and context[1].is_set()):
        event = {'section': section, 'text': text, 'state': state}
        if partial_text is not None:
            event['partial_text'] = str(partial_text)
        context[0](event)

def collect(tasks, *, timeout: float = 15.0, max_attempts: int = 1,
            retry_delays=None, scope=None, validate=None, on_attempt=None):
    """Read sections independently; retry completed failures within a total budget.

    A blocked read retains both its semaphore slot and its provider identity even
    after cancellation. Neither a retry nor a later briefing overlaps that read.
    Waiting between attempts belongs to this scheduler, never the Qt thread.
    """
    context = _CONTEXT.get()
    cancel = context[1] if context else None
    results = queue.Queue()
    slots = _READ_SLOTS
    pending = set(tasks)
    deadline = time.monotonic() + max(.01, timeout)
    attempts = dict.fromkeys(tasks, 0)
    due = dict.fromkeys(tasks, time.monotonic())
    running = set()
    failures = {}
    delays = RETRY_DELAYS if retry_delays is None else tuple(retry_delays)
    max_attempts = max(1, min(3, int(max_attempts)))
    def note(name, state, **extra):
        if on_attempt is not None:
            on_attempt(name, state, attempts[name], extra)
    def work(name, function, key):
        try:
            try:
                value = function()
                failure = validate(value) if validate is not None else None
                result = ('error', failure) if failure else ('ready', value)
            except Exception as exc:
                result = ('error', section_failure(exc))
        finally:
            if key is not None:
                with _READ_LOCK:
                    _ACTIVE_READS.discard(key)
            slots.release()
        results.put((name, *result))
    while pending:
        if cancel is not None and cancel.is_set():
            for name in sorted(pending):
                yield name, 'cancelled', None
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            for name in sorted(pending):
                yield name, 'timeout', dict(failures.get(name, {}),
                    error_type='DeadlineExceeded', attempts=attempts[name])
            return
        for name in tasks:
            if name not in pending or name in running or time.monotonic() < due[name]:
                continue
            if cancel is not None and cancel.is_set():
                break
            key = (scope, _READ_IDENTITIES.get(name, name)) if scope is not None else None
            with _READ_LOCK:
                available = key is None or key not in _ACTIVE_READS
                acquired = available and slots.acquire(blocking=False)
                if acquired and key is not None:
                    _ACTIVE_READS.add(key)
            if not acquired:
                if max_attempts == 1 and scope is None:
                    running.add(name)
                    results.put((name, 'error', {'error_type': 'ReadStillRunning',
                                               'retryable': False}))
                continue
            attempts[name] += 1
            running.add(name)
            note(name, 'started')
            try:
                threading.Thread(target=work, args=(name, tasks[name], key), daemon=True,
                                 name='NEO-Briefing-' + name).start()
            except Exception:
                with _READ_LOCK:
                    _ACTIVE_READS.discard(key)
                slots.release()
                results.put((name, 'error', {'error_type': 'ReadUnavailable', 'retryable': True}))
        try:
            name, status, value = results.get(timeout=min(.05, remaining))
        except queue.Empty:
            continue
        if cancel is not None and cancel.is_set():
            continue
        if name in pending:
            running.discard(name)
            if status == 'error':
                previous_partial = failures.get(name, {}).get('partial_result')
                if previous_partial and not value.get('partial_result'):
                    value = dict(value, partial_result=previous_partial)
                failures[name] = value
            note(name, status, **({k: value.get(k) for k in ('error_type', 'reason', 'retryable', 'http_status', 'failure_kind')} if status == 'error' else {}))
            if (status == 'error' and value.get('retryable', True)
                    and attempts[name] < max_attempts):
                delay = max(0.0, float(delays[min(attempts[name] - 1, len(delays) - 1)])) if delays else 0.0
                due[name] = time.monotonic() + delay
                yield name, 'retrying', dict(value, attempts=attempts[name], next_attempt=attempts[name] + 1)
                continue
            pending.remove(name)
            if status == 'error':
                value = dict(value, attempts=attempts[name])
            yield name, status, value

_NAMES = {
    'services': ('Проверка сервисов', 'Service check', 'Dienstprüfung', 'Vérification des services', 'Comprobación de servicios', 'Verifica servizi', 'Verificação de serviços'),
    'briefing': ('Сводка', 'Briefing', 'Tagesübersicht', 'Résumé du jour', 'Resumen del día', 'Riepilogo del giorno', 'Resumo do dia'),
    'locations.search': ('Поиск файлов', 'File search', 'Dateisuche', 'Recherche de fichiers', 'Búsqueda de archivos', 'Ricerca di file', 'Pesquisa de ficheiros'),
    'weather': ('Погода', 'Weather', 'Wetter', 'Météo', 'Tiempo', 'Meteo', 'Tempo'),
    'system': ('Система', 'System', 'System', 'Système', 'Sistema', 'Sistema', 'Sistema'),
    'processes': ('Процессы', 'Processes', 'Prozesse', 'Processus', 'Procesos', 'Processi', 'Processos'),
    'devices': ('Устройства', 'Devices', 'Geräte', 'Périphériques', 'Dispositivos', 'Dispositivi', 'Dispositivos'),
    'bluetooth': ('Bluetooth',)*7,
    'mail': ('Почта', 'Mail', 'E-Mail', 'Courrier', 'Correo', 'Posta', 'Correio'),
    'mail.gmail': ('Gmail',)*7,
    'mail.mailru': ('Mail.ru',)*7,
    'calendar': ('Календарь', 'Calendar', 'Kalender', 'Calendrier', 'Calendario', 'Calendario', 'Calendário'),
    'telegram': ('Telegram',)*7,
}
_CODES = ('ru','en','de','fr','es','it','pt')
_WAIT = ('Получаю информацию: {name}…', 'Getting {name}…', '{name} wird geladen…', 'Chargement : {name}…', 'Consultando: {name}…', 'Caricamento: {name}…', 'A obter: {name}…')
_ERROR = ('{name}: данные сейчас недоступны.', '{name}: data unavailable.', '{name}: Daten nicht verfügbar.', '{name} : données indisponibles.', '{name}: datos no disponibles.', '{name}: dati non disponibili.', '{name}: dados indisponíveis.')
_RETRY = ('Получаю информацию: {name}… Повторная попытка {attempt} из 3.', 'Getting {name}… Retry {attempt} of 3.', '{name} wird geladen… Versuch {attempt} von 3.', 'Chargement : {name}… Tentative {attempt} sur 3.', 'Consultando: {name}… Intento {attempt} de 3.', 'Caricamento: {name}… Tentativo {attempt} di 3.', 'A obter: {name}… Tentativa {attempt} de 3.')
_CONFIGURATION = ('{name}: проверьте подключение и авторизацию сервиса в настройках.', '{name}: check service setup and authorization in Settings.', '{name}: Einrichtung und Anmeldung des Dienstes in den Einstellungen prüfen.', '{name} : vérifiez la configuration et l’autorisation du service dans les paramètres.', '{name}: comprueba la configuración y autorización del servicio en los ajustes.', '{name}: controlla la configurazione e l’autorizzazione del servizio nelle impostazioni.', '{name}: verifique a configuração e a autorização do serviço nas definições.')
_INCOMPLETE = ('{name}: часть данных сейчас недоступна.', '{name}: some data is currently unavailable.', '{name}: einige Daten sind derzeit nicht verfügbar.', '{name} : certaines données sont indisponibles pour le moment.', '{name}: algunos datos no están disponibles ahora.', '{name}: alcuni dati non sono al momento disponibili.', '{name}: alguns dados estão indisponíveis de momento.')
_EMPTY = ('Нет записей.', 'No entries.', 'Keine Einträge.', 'Aucune entrée.', 'Sin entradas.', 'Nessuna voce.', 'Sem entradas.')
_BLUETOOTH_EMPTY = ('Подключённых устройств не обнаружено.', 'No connected devices detected.', 'Keine verbundenen Geräte erkannt.', 'Aucun appareil connecté détecté.', 'No se detectaron dispositivos conectados.', 'Nessun dispositivo connesso rilevato.', 'Nenhum dispositivo ligado detetado.')
_COMPLETE = ('Сводка готова.', 'Briefing complete.', 'Tagesübersicht fertig.', 'Résumé du jour terminé.', 'Resumen del día listo.', 'Riepilogo del giorno pronto.', 'Resumo do dia concluído.')
_PARTIAL = ('Сводка завершена. Часть сведений сейчас недоступна.', 'Briefing complete. Some information is currently unavailable.', 'Tagesübersicht beendet. Einige Informationen sind derzeit nicht verfügbar.', 'Résumé terminé. Certaines informations sont indisponibles pour le moment.', 'Resumen terminado. Parte de la información no está disponible ahora.', 'Riepilogo terminato. Alcune informazioni non sono al momento disponibili.', 'Resumo concluído. Algumas informações estão indisponíveis de momento.')

def is_service_section(section: str) -> bool:
    """API availability lines ('service.<key>', or 'services' when none are enabled)."""
    return section == 'services' or section.startswith('service.')

def empty_section_text(section: str, code: str) -> str:
    index = _CODES.index(code) if code in _CODES else 1
    value = _BLUETOOTH_EMPTY[index] if section == 'bluetooth' else _EMPTY[index]
    return section_name(section, code) + ': ' + value

def completion_text(code: str, *, partial: bool = False) -> str:
    index = _CODES.index(code) if code in _CODES else 1
    return (_PARTIAL if partial else _COMPLETE)[index]

def section_name(section: str, code: str) -> str:
    index = _CODES.index(code) if code in _CODES else 1
    if section.startswith('service.'):
        from jarvis.integrations.service_checks161 import SERVICES
        key = section.removeprefix('service.')
        return next((x.name for x in SERVICES if x.key == key), 'Discord #' + key.split('.')[-1] if key.startswith('discord.') else 'API')
    return _NAMES.get(section, (section,)*7)[index]

def status_text(section: str, code: str, *, failed=False, attempt=0, configuration=False, partial=False) -> str:
    index = _CODES.index(code) if code in _CODES else 1
    name = section_name(section, code)
    template = _CONFIGURATION if configuration else _INCOMPLETE if partial else _ERROR if failed else _RETRY if attempt else _WAIT
    return template[index].format(name=name, attempt=attempt)


class BriefingDocument:
    """One chat message: completed sections, then the pending reads.

    A section has one identity. Completion replaces its placeholder, while the
    already visible sections stay in their arrival order. This small model is
    independent of Qt and used by startup and user-requested briefings alike.
    """

    def __init__(self, code: str):
        self.code = code
        self.sections = {}
        self.ready_order = []
        self.partial_text = {}
        self.closed = False

    def update(self, section: str, text: str, state: str = 'ready', *, partial_text=None) -> bool:
        if self.closed or not section or not str(text).strip():
            return False
        old = self.sections.get(section)
        if old is not None and old[1] != 'waiting' and state == 'waiting':
            return False
        self.sections[section] = (str(text), state)
        if state == 'waiting' and partial_text is not None:
            self.partial_text[section] = str(partial_text)
        elif state != 'waiting':
            self.partial_text.pop(section, None)
        if state != 'waiting' and section not in self.ready_order:
            self.ready_order.append(section)
        return True

    def finish(self, *, cancelled: bool = False) -> None:
        for section, (_text, state) in tuple(self.sections.items()):
            if state == 'waiting':
                partial = self.partial_text.get(section, '')
                text = status_text(section, self.code, failed=True, partial=bool(partial))
                if partial:
                    text = partial + '\n' + text
                self.update(section, text,
                            'cancelled' if cancelled else 'error')
        self.closed = True

    def render(self) -> str:
        ready = [self.sections[name][0] for name in self.ready_order
                 if not is_service_section(name) and name != 'completion']
        pending = [text for name, (text, state) in self.sections.items()
                   if state == 'waiting' and not is_service_section(name)]
        if pending:
            ready.append('\n'.join(pending))
        # API service checks form one block at the end, one line per service,
        # in the order they were queued (not the order they answered).
        services = [text for name, (text, _state) in self.sections.items() if is_service_section(name)]
        if services:
            ready.append('\n'.join(services))
        if 'completion' in self.sections:
            ready.append(self.sections['completion'][0])
        return '\n\n━━━━━━━━━━━━━━━━━━━━━━━━\n'.join(ready)
