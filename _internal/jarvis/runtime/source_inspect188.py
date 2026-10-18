"""NEO 188 NEO-DOC-002 / TZ-ANSWER-SOURCE-EXCERPT-INSPECTION-01.

Every answer from local documents keeps, per source number, the trusted file
identity from retrieval (never a path taken from the model's answer or the
file text), the known page/section/lines and EXACTLY the excerpt that was
passed to the model (the local model gets at most six blocks of 1200
characters). «покажи источник 2» shows that excerpt; «открой источник 2»
opens the exact original — only inside a still-allowed folder, only a
supported document type, never an executable.

«Источник использован» is not «утверждение доказано»: the view says so.
"""
from __future__ import annotations

import os
import re
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from jarvis.runtime.natural_commands import LANGUAGES, strip_request_shell
from jarvis.runtime.spoken_numbers import parse_spoken_number

MAX_BLOCKS = 6          # local_chat169: sources[:6]
MAX_CHARS = 1200        # local_chat169: text[:1200]
_TTL = 30 * 60
_LOCK = threading.Lock()
_LAST: dict[str, dict[str, Any]] = {}


def excerpts(sources: list[dict[str, Any]], *, passed_to_model: bool) -> list[dict[str, Any]]:
    """The structured identity + exact excerpt of each numbered source of ONE answer."""
    rows = []
    limit = MAX_BLOCKS if passed_to_model else len(sources)
    for number, source in enumerate(sources[:limit], 1):
        path = str(source.get('file', '') or '')
        label, _, real = path.partition(': ') if re.match(r'^[AB]: ', path) else ('', '', path)
        try:
            info = Path(real).stat()
            identity = (info.st_mtime, info.st_size)
        except OSError:
            identity = None
        rows.append({'n': number, 'side': label, 'file': real, 'location': str(source.get('location', '') or ''),
                     'excerpt': str(source.get('text', '') or '')[:MAX_CHARS if passed_to_model else 300],
                     'identity': identity, 'passed_to_model': passed_to_model})
    return rows


def remember(scope: str, rows: list[dict[str, Any]]) -> None:
    with _LOCK:
        _LAST[str(scope or 'desktop')] = {'rows': list(rows), 'at': time.monotonic()}


def recall(scope: str = 'desktop') -> list[dict[str, Any]]:
    with _LOCK:
        item = _LAST.get(str(scope or 'desktop'))
    if not item or time.monotonic() - item['at'] > _TTL:
        return []
    return list(item['rows'])


def forget(scope: str = 'desktop') -> None:
    with _LOCK:
        _LAST.pop(str(scope or 'desktop'), None)


def state(row: dict[str, Any], allowed_folders: list[str]) -> str:
    """'ok' | 'changed' | 'missing' | 'revoked' for the CURRENT original."""
    path = str(row.get('file', '') or '')
    key = lambda p: str(p).replace('/', '\\').rstrip('\\').casefold()
    inside = any(key(path).startswith(key(folder) + '\\') for folder in allowed_folders)
    if not inside:
        return 'revoked'
    try:
        resolved = Path(path).resolve(strict=True)
        info = resolved.stat()
    except OSError:
        return 'missing'
    if not any(key(str(resolved)).startswith(key(str(Path(folder).resolve(strict=False))) + '\\') or
               key(str(resolved)).startswith(key(folder) + '\\') for folder in allowed_folders):
        return 'revoked'          # a symlink/reparse point leading outside the granted folder
    if row.get('identity') and tuple(row['identity']) != (info.st_mtime, info.st_size):
        return 'changed'
    return 'ok'


_SHOW = {
    'ru': r'(?:покажи|открой\s+текст|какой)\s+(?:источник|фрагмент|цитат[уа])\s*(?:номер\s*)?(?P<n>\S+)',
    'en': r'(?:show|display)\s+(?:the\s+)?(?:source|excerpt|fragment)\s*(?:number\s*)?(?P<n>\S+)',
    'de': r'zeig(?:e)?\s+(?:die\s+)?(?:quelle|fundstelle)\s*(?:nummer\s*)?(?P<n>\S+)',
    'fr': r'montre\s+(?:la\s+)?(?:source|l[’\']extrait)\s*(?:numéro\s*)?(?P<n>\S+)',
    'es': r'muestra\s+(?:la\s+)?(?:fuente|el\s+fragmento)\s*(?:número\s*)?(?P<n>\S+)',
    'it': r'mostra\s+(?:la\s+)?(?:fonte|il\s+frammento)\s*(?:numero\s*)?(?P<n>\S+)',
    'pt': r'mostra\s+(?:a\s+)?(?:fonte|o\s+fragmento)\s*(?:número\s*)?(?P<n>\S+)',
}
_OPEN = {
    'ru': r'открой\s+(?:источник|оригинал\s+источника|файл\s+источника)\s*(?:номер\s*)?(?P<n>\S+)(?P<reveal>\s+в\s+папке)?',
    'en': r'open\s+(?:the\s+)?source\s*(?:number\s*)?(?P<n>\S+)(?P<reveal>\s+in\s+(?:the\s+)?folder)?',
    'de': r'öffne\s+(?:die\s+)?quelle\s*(?:nummer\s*)?(?P<n>\S+)(?P<reveal>\s+im\s+ordner)?',
    'fr': r'ouvre\s+(?:la\s+)?source\s*(?:numéro\s*)?(?P<n>\S+)(?P<reveal>\s+dans\s+le\s+dossier)?',
    'es': r'abre\s+(?:la\s+)?fuente\s*(?:número\s*)?(?P<n>\S+)(?P<reveal>\s+en\s+la\s+carpeta)?',
    'it': r'apri\s+(?:la\s+)?fonte\s*(?:numero\s*)?(?P<n>\S+)(?P<reveal>\s+nella\s+cartella)?',
    'pt': r'abre\s+(?:a\s+)?fonte\s*(?:número\s*)?(?P<n>\S+)(?P<reveal>\s+na\s+pasta)?',
}


@lru_cache(maxsize=7)
def _patterns(code: str):
    return re.compile(r'^' + _SHOW[code] + r'$', re.I), re.compile(r'^' + _OPEN[code] + r'$', re.I)


def _number(raw: str, code: str) -> int | None:
    value = str(raw or '').strip(' .!?№#[]')
    if value.isdigit():
        return int(value)
    spoken = parse_spoken_number(value, code)
    return int(spoken) if spoken is not None and spoken == int(spoken) else None


def parse(text: str, code: str) -> tuple[str, dict] | None:
    if code not in LANGUAGES:
        return None
    value = strip_request_shell(str(text or ''), code).strip(' .!?¿¡')
    show, open_ = _patterns(code)
    match = open_.fullmatch(value)
    if match and _number(match.group('n'), code):
        return 'knowledge', {'action': 'reveal_source' if match.group('reveal') else 'open_source', 'index': _number(match.group('n'), code)}
    match = show.fullmatch(value)
    if match and _number(match.group('n'), code):
        return 'knowledge', {'action': 'show_source', 'index': _number(match.group('n'), code)}
    return None


_ORDER = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_TEXT = {
    'none': ('Нет недавнего ответа по вашим файлам, к которому относился бы источник.', 'There is no recent answer from your files with sources.',
             'Es gibt keine aktuelle Antwort aus Ihren Dateien mit Quellen.', 'Aucune réponse récente tirée de vos fichiers avec des sources.',
             'No hay ninguna respuesta reciente de tus archivos con fuentes.', 'Non c’è una risposta recente dai tuoi file con fonti.',
             'Não há nenhuma resposta recente dos teus ficheiros com fontes.'),
    'bad_index': ('В последнем ответе источников: {count}. Источника №{n} нет.', 'The last answer has {count} sources; there is no source {n}.',
                  'Die letzte Antwort hat {count} Quellen; Quelle {n} gibt es nicht.', 'La dernière réponse a {count} sources ; pas de source {n}.',
                  'La última respuesta tiene {count} fuentes; no hay fuente {n}.', 'L’ultima risposta ha {count} fonti; la fonte {n} non esiste.',
                  'A última resposta tem {count} fontes; não há fonte {n}.'),
    'view': ('Источник {n}: {file}{where}\nФрагмент, переданный модели при ответе:\n«{excerpt}»\n{note}',
             'Source {n}: {file}{where}\nExcerpt passed to the model for the answer:\n“{excerpt}”\n{note}',
             'Quelle {n}: {file}{where}\nAn das Modell übergebener Ausschnitt:\n„{excerpt}“\n{note}',
             'Source {n} : {file}{where}\nExtrait transmis au modèle pour la réponse :\n« {excerpt} »\n{note}',
             'Fuente {n}: {file}{where}\nFragmento enviado al modelo para la respuesta:\n«{excerpt}»\n{note}',
             'Fonte {n}: {file}{where}\nFrammento passato al modello per la risposta:\n«{excerpt}»\n{note}',
             'Fonte {n}: {file}{where}\nFragmento enviado ao modelo para a resposta:\n«{excerpt}»\n{note}'),
    'view_snippet': ('Источник {n}: {file}{where}\nНайденный фрагмент (модель не использовалась):\n«{excerpt}»\n{note}',
                     'Source {n}: {file}{where}\nRetrieved fragment (no model was used):\n“{excerpt}”\n{note}',
                     'Quelle {n}: {file}{where}\nGefundener Ausschnitt (ohne Modell):\n„{excerpt}“\n{note}',
                     'Source {n} : {file}{where}\nFragment trouvé (sans modèle) :\n« {excerpt} »\n{note}',
                     'Fuente {n}: {file}{where}\nFragmento encontrado (sin modelo):\n«{excerpt}»\n{note}',
                     'Fonte {n}: {file}{where}\nFrammento trovato (senza modello):\n«{excerpt}»\n{note}',
                     'Fonte {n}: {file}{where}\nFragmento encontrado (sem modelo):\n«{excerpt}»\n{note}'),
    'note_ok': ('Это фрагмент, на который опирался ответ; наличие источника не доказывает каждое утверждение ответа.',
                'This is the fragment the answer relied on; a cited source does not prove every statement of the answer.',
                'Auf diesen Ausschnitt stützte sich die Antwort; eine Quelle beweist nicht jede Aussage.',
                'C’est l’extrait sur lequel s’appuie la réponse ; une source citée ne prouve pas chaque affirmation.',
                'Es el fragmento en el que se apoyó la respuesta; una fuente citada no prueba cada afirmación.',
                'È il frammento su cui si basava la risposta; una fonte citata non prova ogni affermazione.',
                'É o fragmento em que a resposta se apoiou; uma fonte citada não prova cada afirmação.'),
    'note_changed': ('Файл изменился после ответа: фрагмент показан таким, каким он был использован.',
                     'The file changed after the answer: the excerpt is shown as it was used.',
                     'Die Datei wurde nach der Antwort geändert: Der Ausschnitt wird so gezeigt, wie er verwendet wurde.',
                     'Le fichier a changé après la réponse : l’extrait est montré tel qu’il a été utilisé.',
                     'El archivo cambió tras la respuesta: el fragmento se muestra tal como se usó.',
                     'Il file è cambiato dopo la risposta: il frammento è mostrato com’era stato usato.',
                     'O ficheiro mudou depois da resposta: o fragmento é mostrado como foi usado.'),
    'note_missing': ('Оригинал сейчас недоступен (удалён, перемещён или не читается).', 'The original is not available now (deleted, moved or unreadable).',
                     'Das Original ist gerade nicht verfügbar (gelöscht, verschoben oder unlesbar).', 'L’original n’est pas disponible (supprimé, déplacé ou illisible).',
                     'El original no está disponible (borrado, movido o ilegible).', 'L’originale non è disponibile (eliminato, spostato o illeggibile).',
                     'O original não está disponível (apagado, movido ou ilegível).'),
    'revoked': ('Доступ к папке этого источника отозван — не показываю и не открываю его.', 'Access to this source’s folder was revoked — I won’t show or open it.',
                'Der Zugriff auf den Ordner dieser Quelle wurde entzogen – ich zeige oder öffne sie nicht.',
                'L’accès au dossier de cette source a été retiré : je ne l’affiche ni ne l’ouvre.',
                'Se retiró el acceso a la carpeta de esta fuente: no la muestro ni la abro.',
                'L’accesso alla cartella di questa fonte è stato revocato: non la mostro né la apro.',
                'O acesso à pasta desta fonte foi retirado: não a mostro nem abro.'),
    'opened': ('Открыл оригинал: {file}. Переход к нужной странице или строке не поддерживается — найдите место вручную{where}.',
               'Opened the original: {file}. Jumping to the page or line is not supported — find it manually{where}.',
               'Original geöffnet: {file}. Zur Seite/Zeile springen geht nicht – bitte manuell suchen{where}.',
               'Original ouvert : {file}. Aller à la page ou la ligne n’est pas pris en charge — cherchez-la vous-même{where}.',
               'Abrí el original: {file}. Ir a la página o línea no está soportado: búscala a mano{where}.',
               'Ho aperto l’originale: {file}. Andare alla pagina o riga non è supportato: cercala a mano{where}.',
               'Abri o original: {file}. Ir para a página ou linha não é suportado: procura-a manualmente{where}.'),
    'revealed': ('Показал файл в папке: {file}.', 'Showed the file in its folder: {file}.', 'Datei im Ordner gezeigt: {file}.',
                 'Fichier montré dans son dossier : {file}.', 'Mostré el archivo en su carpeta: {file}.', 'Ho mostrato il file nella cartella: {file}.',
                 'Mostrei o ficheiro na pasta: {file}.'),
    'unsupported': ('Этот тип файла NEO не открывает автоматически: {file}.', 'NEO does not open this file type automatically: {file}.',
                    'Diesen Dateityp öffnet NEO nicht automatisch: {file}.', 'NEO n’ouvre pas ce type de fichier automatiquement : {file}.',
                    'NEO no abre este tipo de archivo automáticamente: {file}.', 'NEO non apre automaticamente questo tipo di file: {file}.',
                    'O NEO não abre este tipo de ficheiro automaticamente: {file}.'),
    'open_failed': ('Не удалось открыть оригинал: {file}.', 'Could not open the original: {file}.', 'Original konnte nicht geöffnet werden: {file}.',
                    'Impossible d’ouvrir l’original : {file}.', 'No se pudo abrir el original: {file}.', 'Impossibile aprire l’originale: {file}.',
                    'Não foi possível abrir o original: {file}.'),
    'hint': ('Фрагменты источников: «покажи источник 1».', 'Source excerpts: “show source 1”.', 'Quellenausschnitte: „zeige Quelle 1“.',
             'Extraits des sources : « montre la source 1 ».', 'Fragmentos de las fuentes: «muestra la fuente 1».',
             'Frammenti delle fonti: «mostra la fonte 1».', 'Fragmentos das fontes: «mostra a fonte 1».'),
}


def t(key: str, code: str, **values) -> str:
    return _TEXT[key][_ORDER.index(code) if code in _ORDER else 1].format(**values)


OPENABLE = {'.txt', '.md', '.markdown', '.rst', '.csv', '.tsv', '.log', '.docx', '.pdf'}
