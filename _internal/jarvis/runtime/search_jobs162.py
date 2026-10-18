"""One bounded local search job shared by both public lookup tools."""
from __future__ import annotations

from pathlib import Path
import time

from jarvis.runtime.file_search import FileSearchResults, search_files, search_content, format_search_results

_START = {
    'ru': 'Ищу локальные файлы и папки…', 'en': 'Searching local files and folders…',
    'de': 'Ich suche lokale Dateien und Ordner…', 'fr': 'Je cherche les fichiers et dossiers locaux…',
    'es': 'Buscando archivos y carpetas locales…', 'it': 'Cerco file e cartelle locali…',
    'pt': 'A procurar ficheiros e pastas locais…',
}
_CONTINUE = {'ru': 'Продолжаю поиск…', 'en': 'Continuing the search…',
             'de': 'Die Suche wird fortgesetzt…', 'fr': 'Je poursuis la recherche…',
             'es': 'Continúo la búsqueda…', 'it': 'Continuo la ricerca…', 'pt': 'A continuar a pesquisa…'}


def run_search(query, *, action='files', roots, language='ru', limit=50,
               recursive=True, literal=False, glob=False, budget=12.0):
    """Continue paced scans within one command; never start an idle disk walk."""
    from jarvis.runtime.cancellation import cancellation_manager
    from jarvis.runtime.briefing_stream import emit
    cancel = cancellation_manager().raise_if_cancelled
    deadline = time.monotonic() + max(.01, min(12.0, budget))
    rows = FileSearchResults(incomplete=True, continuable=True, roots=roots)
    cancel()
    emit('local_files', _START.get(language, _START['en']), 'ready')
    last_paths = None
    while time.monotonic() < deadline:
        cancel()
        seconds = min(.75, max(.01, deadline - time.monotonic()))
        if action == 'content':
            rows = search_content(query, roots=roots, limit=limit, max_seconds=seconds)
        else:
            rows = search_files(query, roots=roots, limit=limit, max_seconds=seconds,
                                include_dirs=True, recursive=recursive, literal=literal, glob=glob)
        cancel()
        paths = tuple(row['path'] for row in rows)
        if paths != last_paths:
            if getattr(rows, 'continuable', False):
                progress = (format_search_results(FileSearchResults(rows), language) + '\n' if rows else '')
                progress += _CONTINUE.get(language, _CONTINUE['en'])
            else:
                progress = format_search_results(rows, language)
            emit('local_files', progress, 'ready')
            last_paths = paths
        if not getattr(rows, 'continuable', False):
            break
        # An exact named object can be shown immediately. The result still says
        # coverage is partial: an identically named object may exist elsewhere.
        exact = action == 'files' and any(Path(row['path']).name.casefold() == str(query).casefold() for row in rows)
        if exact or len(rows) >= min(limit, 10):
            break
    cancel()
    emit('local_files', format_search_results(rows, language), 'ready')
    return rows
