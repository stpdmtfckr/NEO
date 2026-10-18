"""NEO 173: checking NEO on the user's own computer.

* ``report(...)`` — «отчёт для разработчика»: one text file the user can send. It holds NEO and
  Windows versions, which services and models are set up (yes/no, never keys or tokens),
  the self-test, how long each web service took, recent route timings, and the *kinds* of
  recent errors. It never holds the text of commands, answers, documents, mail, the
  clipboard or file names.
* ``startup_problems(...)`` — a quiet check a minute after start. When something needed is
  missing (Ollama does not answer, the NEO model is not installed, Windows text recognition
  is missing, SearchAPI is on without a key) NEO says so once in the chat, with the fix.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import platform
import re
import sys
from typing import Any, Callable

_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def _code(code: str) -> str:
    code = str(code or 'ru').casefold().split('-')[0]
    return code if code in _LANGS else 'en'


def _pick(row: tuple[str, ...], code: str) -> str:
    return row[_LANGS.index(_code(code))]


# ---- what is set up (booleans only) --------------------------------------------------------------

_SWITCHES = (
    ('local_chat', 'local_chat.enabled'), ('neo_agent_model', 'llm_agent.enabled'), ('neo_command_model', 'neo_model.enabled'),
    ('web_search', 'gaming.web_knowledge.enabled'), ('gmail', 'integrations.gmail.enabled'), ('calendar', 'integrations.google_calendar.enabled'),
    ('spotify', 'integrations.spotify.enabled'), ('yandex_music', 'integrations.yandex_music.enabled'), ('weather', 'integrations.openweather.enabled'),
    ('telegram', 'integrations.telegram.enabled'), ('discord', 'integrations.discord.enabled'), ('memory', 'memory.enabled'),
    ('phrase_collection', 'learning.collect_phrases'), ('prewarm', 'performance.prewarm_connections'), ('preload_model', 'performance.preload_on_wake'),
    ('streaming', 'local_chat.stream'), ('help_confirm', 'help.confirm_before_search'), ('context', 'context.enabled'),
)
_KEYS_PRESENT = (('searchapi_key', 'gaming.web_knowledge.searchapi_api_key_encrypted'), ('openweather_key', 'integrations.openweather.api_key_encrypted'),
                 ('gmail_authorized', 'integrations.gmail.refresh_token_encrypted'))


def setup_summary(config: Any) -> dict[str, Any]:
    out: dict[str, Any] = {name: bool(config.get(key, False)) for name, key in _SWITCHES}
    out.update({name: bool(str(config.get(key, '') or '').strip()) for name, key in _KEYS_PRESENT})
    out['language'] = str(config.get('app.language', '') or '')
    out['agent_model_size'] = str(config.get('llm_agent.size', '') or '')
    out['keep_alive'] = str(config.get('llm_agent.keep_alive', '') or '')
    out['settings_version'] = config.get('settings_version', 0)
    return out


_ERROR_LINE = re.compile(r'^(?P<time>\S+ \S+)\s+(?:\S+\s+)?(?:ERROR|CRITICAL)\s+(?P<logger>[\w.]+)', re.M)
_EXCEPTION = re.compile(r'^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Timeout|Refused|Failed|Warning))\b', re.M)


def error_kinds(log_text: str, limit: int = 30) -> list[str]:
    """«logger: ExceptionType» of recent errors — no messages (they could contain user text or paths)."""
    rows = []
    loggers = [m.group('logger') for m in _ERROR_LINE.finditer(log_text)]
    types = [m.group('type') for m in _EXCEPTION.finditer(log_text)]
    for index, name in enumerate(loggers[-limit:]):
        kind = types[-limit:][index] if index < len(types[-limit:]) else ''
        rows.append(f'{name}: {kind}' if kind else name)
    return rows


def report(config: Any, *, version: str = '', ollama_state: Callable[[], dict[str, Any]] | None = None,
           self_test: Callable[[], dict[str, Any]] | None = None, log_text: str | None = None, now: datetime | None = None) -> tuple[Path | None, str]:
    """Write the report; returns (path, text)."""
    from jarvis.runtime import http_pool173
    now = now or datetime.now()
    data: dict[str, Any] = {
        'created': now.isoformat(timespec='seconds'),
        'neo_version': version,
        'windows': platform.platform(),
        'python': sys.version.split()[0],
        'cpu_count': os.cpu_count(),
        'setup': setup_summary(config),
        'service_timings_ms': http_pool173.summary(),
    }
    try:
        import psutil
        data['ram_total_gb'] = round(psutil.virtual_memory().total / 1024 ** 3, 1)
        process = psutil.Process()
        data['neo_ram_mb'] = round(process.memory_info().rss / 1024 ** 2)
        data['neo_processes'] = sorted({p.info['name'] for p in psutil.process_iter(['name'])
                                        if str(p.info.get('name') or '').casefold() in {'neo.exe', 'neo_wake_service.exe'}})
    except Exception:
        pass
    if ollama_state is not None:
        try:
            data['ollama'] = ollama_state()
        except Exception as exc:
            data['ollama'] = {'error': type(exc).__name__}
    if self_test is not None:
        try:
            result = self_test()
            data['self_test'] = [{'name': c.get('name'), 'status': c.get('status'), 'ok': c.get('ok')} for c in result.get('checks', [])]
        except Exception as exc:
            data['self_test'] = {'error': type(exc).__name__}
    try:
        from jarvis.runtime.latency import latency_profiler
        data['latency_ms'] = latency_profiler().statistics()        # per stage: p50/p95/p99 — no command details
    except Exception:
        pass
    if log_text is None:
        log_text = _read_log(config)
    data['error_kinds'] = error_kinds(log_text)
    text = json.dumps(data, ensure_ascii=False, indent=1, default=str)
    root = getattr(getattr(config, 'paths', None), 'root', None)
    if root is None:
        return None, text
    folder = Path(root) / 'reports'
    folder.mkdir(parents=True, exist_ok=True)
    # HIST-080: every finished report gets its own file. Exclusive creation
    # ('x') with a counter never replaces a report made in the same second.
    stem = f'neo_report_{now:%Y%m%d_%H%M%S}'
    for attempt in range(1000):
        path = folder / (stem + ('' if attempt == 0 else f'_{attempt}') + '.txt')
        try:
            with open(path, 'x', encoding='utf-8') as handle:
                handle.write(text)
            return path, text
        except FileExistsError:
            continue
    return None, text


def _read_tail187(path: Path, limit: int) -> str:
    """HIST-081: read at most ``limit`` bytes from the end, never the whole file.

    The cut may land inside a multi-byte UTF-8 character: the broken leading
    bytes of that one character are dropped (not replaced by «�»).
    """
    with open(path, 'rb') as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - int(limit))
        handle.seek(start)
        data = handle.read(int(limit))
    if start > 0:
        skip = 0
        while skip < min(3, len(data)) and (data[skip] & 0xC0) == 0x80:
            skip += 1
        data = data[skip:]
    return data.decode('utf-8', errors='replace')


def _read_log(config: Any) -> str:
    logs = getattr(getattr(config, 'paths', None), 'logs', None)
    if not logs:
        return ''
    try:
        newest = max(Path(logs).glob('*.log'), key=lambda p: p.stat().st_mtime)
        return _read_tail187(newest, 2_000_000)
    except (ValueError, OSError):
        return ''


# ---- a quiet check after start --------------------------------------------------------------------

_PROBLEMS = {
    'ollama': ('Ollama не отвечает: разговор и модель NEO сейчас не работают. Запустите NEO.cmd --install-deps или перезапустите Ollama.',
               'Ollama is not answering: conversation and the NEO model don’t work right now. Run NEO.cmd --install-deps or restart Ollama.',
               'Ollama antwortet nicht: Gespräch und NEO-Modell funktionieren gerade nicht. Starte NEO.cmd --install-deps oder Ollama neu.',
               'Ollama ne répond pas : la conversation et le modèle NEO ne fonctionnent pas. Lancez NEO.cmd --install-deps ou redémarrez Ollama.',
               'Ollama no responde: la conversación y el modelo de NEO no funcionan. Ejecuta NEO.cmd --install-deps o reinicia Ollama.',
               'Ollama non risponde: conversazione e modello NEO non funzionano. Esegui NEO.cmd --install-deps o riavvia Ollama.',
               'O Ollama não responde: a conversa e o modelo do NEO não funcionam. Executa NEO.cmd --install-deps ou reinicia o Ollama.'),
    'agent_model': ('Модель NEO не установлена в Ollama. Скажите «установи модель ассистента» — она скачается в фоне.',
                    'The NEO model is not installed in Ollama. Say “install the assistant model” and it downloads in the background.',
                    'Das NEO-Modell ist nicht in Ollama installiert. Sag „installiere das Assistenzmodell“.',
                    'Le modèle NEO n’est pas installé dans Ollama. Dites « installe le modèle d’assistant ».',
                    'El modelo de NEO no está instalado en Ollama. Di «instala el modelo del asistente».',
                    'Il modello NEO non è installato in Ollama. Di’ «installa il modello dell’assistente».',
                    'O modelo do NEO não está instalado no Ollama. Diz «instala o modelo do assistente».'),
    'ocr': ('Распознавание текста Windows недоступно: нажатие по надписям в играх и «что за ошибка?» работать не будут. '
            'Добавьте язык с распознаванием текста в Параметры → Время и язык → Язык.',
            'Windows text recognition is unavailable: clicking text in games and “what’s this error?” won’t work. '
            'Add a language with text recognition in Settings → Time & language → Language.',
            'Die Windows-Texterkennung fehlt: Klicken auf Text in Spielen und „was ist das für ein Fehler?“ gehen nicht. Füge eine Sprache mit Texterkennung hinzu.',
            'La reconnaissance de texte Windows manque : le clic sur du texte dans les jeux et « c’est quoi cette erreur ? » ne marcheront pas. Ajoutez une langue avec reconnaissance de texte.',
            'Falta el reconocimiento de texto de Windows: pulsar texto en juegos y «¿qué es este error?» no funcionarán. Añade un idioma con reconocimiento de texto.',
            'Manca il riconoscimento del testo di Windows: clic sul testo nei giochi e «che errore è questo?» non funzioneranno. Aggiungi una lingua con riconoscimento del testo.',
            'Falta o reconhecimento de texto do Windows: clicar em texto nos jogos e «que erro é este?» não funcionarão. Adiciona um idioma com reconhecimento de texto.'),
    'searchapi_key': ('Поиск в интернете включён, но ключ SearchAPI.io не задан: Настройки → API-сервисы.',
                      'Internet search is on but no SearchAPI.io key is set: Settings → API services.',
                      'Internetsuche ist an, aber kein SearchAPI.io-Schlüssel gesetzt: Einstellungen → API-Dienste.',
                      'La recherche internet est active mais aucune clé SearchAPI.io : Paramètres → Services API.',
                      'La búsqueda en internet está activa pero falta la clave de SearchAPI.io: Ajustes → Servicios API.',
                      'La ricerca internet è attiva ma manca la chiave SearchAPI.io: Impostazioni → Servizi API.',
                      'A pesquisa na internet está ligada mas falta a chave SearchAPI.io: Definições → Serviços API.'),
    'head': ('NEO проверил себя после запуска:', 'NEO checked itself after start:', 'NEO hat sich nach dem Start geprüft:', 'NEO s’est vérifié après le démarrage :',
             'NEO se revisó tras el inicio:', 'NEO si è controllato dopo l’avvio:', 'O NEO verificou-se após o arranque:'),
}


def startup_problems(config: Any, *, ollama_ok: Callable[[], bool], agent_model_ok: Callable[[], bool], ocr_ok: Callable[[], bool]) -> list[str]:
    problems = []
    needs_ollama = bool(config.get('llm_agent.enabled', True)) or bool(config.get('local_chat.enabled', True))
    if needs_ollama:
        if not ollama_ok():
            problems.append('ollama')
        elif bool(config.get('llm_agent.enabled', True)) and not agent_model_ok():
            problems.append('agent_model')
    if os.name == 'nt' and not ocr_ok():
        problems.append('ocr')
    if bool(config.get('gaming.web_knowledge.enabled', True)) and not str(config.get('gaming.web_knowledge.searchapi_api_key_encrypted', '') or '').strip():
        problems.append('searchapi_key')
    return problems


def not_told_recently(config: Any, problems: list[str], *, days: float = 7.0, now: float | None = None) -> list[str]:
    """The same problem is said at most once a week (remembered in the NEO data folder)."""
    import time
    root = getattr(getattr(config, 'paths', None), 'root', None)
    if root is None or not problems:
        return problems
    path = Path(root) / 'selfcheck_told.json'
    now = time.time() if now is None else now
    try:
        told = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        told = {}
    fresh = [p for p in problems if p not in told or now - float(told[p] or 0) >= days * 86400]
    for problem in fresh:
        told[problem] = now
    try:
        path.write_text(json.dumps(told), encoding='utf-8')
    except OSError:
        pass
    return fresh


def problems_text(problems: list[str], code: str) -> str:
    if not problems:
        return ''
    return _pick(_PROBLEMS['head'], code) + '\n' + '\n'.join('• ' + _pick(_PROBLEMS[p], code) for p in problems)


_TEXT = {
    'saved': ('Отчёт для разработчика сохранён: {path}\nВ нём нет текста ваших команд, ответов, писем, документов, буфера обмена и имён файлов — только версии, настройки «да/нет», время работы сервисов и виды ошибок. Показал файл в Проводнике.',
              'Developer report saved: {path}\nIt has no text of your commands, answers, mail, documents, clipboard or file names — only versions, yes/no settings, service timings and kinds of errors. Showed the file in File Explorer.',
              'Entwicklerbericht gespeichert: {path}\nEr enthält keine Texte von Befehlen, Antworten, Mails, Dokumenten, der Zwischenablage oder Dateinamen – nur Versionen, Ja/Nein-Einstellungen, Dienstzeiten und Fehlerarten.',
              'Rapport pour le développeur enregistré : {path}\nIl ne contient aucun texte de vos commandes, réponses, mails, documents, presse-papiers ni noms de fichiers : seulement versions, réglages oui/non, temps des services et types d’erreurs.',
              'Informe para el desarrollador guardado: {path}\nNo contiene texto de tus órdenes, respuestas, correos, documentos, portapapeles ni nombres de archivo: solo versiones, ajustes sí/no, tiempos de servicios y tipos de errores.',
              'Rapporto per lo sviluppatore salvato: {path}\nNon contiene testo di comandi, risposte, email, documenti, appunti o nomi di file: solo versioni, impostazioni sì/no, tempi dei servizi e tipi di errore.',
              'Relatório para o programador guardado: {path}\nNão tem texto dos teus comandos, respostas, correio, documentos, área de transferência nem nomes de ficheiros: só versões, definições sim/não, tempos dos serviços e tipos de erros.'),
}


def saved_text(path: Path, code: str) -> str:
    return _pick(_TEXT['saved'], code).format(path=path)


__all__ = ['error_kinds', 'not_told_recently', 'problems_text', 'report', 'saved_text', 'setup_summary', 'startup_problems']
