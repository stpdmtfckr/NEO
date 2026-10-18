# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import os
import stat
import fnmatch
from collections import OrderedDict
import re
import threading
import time
import zipfile
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from pathlib import Path

from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
from jarvis.runtime.cancellation import OperationCancelled
from typing import Iterable

_TEXT_EXTS = {".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv", ".log", ".xml", ".html", ".css", ".ps1", ".bat", ".sql"}
_OFFICE_EXTS = {".docx", ".xlsx", ".pptx"}


class FileSearchResults(list):
    """List-compatible results with explicit, truthful scan coverage."""
    def __init__(self, rows=(), *, incomplete=False, indexing=False, continuable=False, roots=(), unreadable=False):
        super().__init__(rows)
        self.incomplete = bool(incomplete)
        self.indexing = bool(indexing)
        self.continuable = bool(continuable)
        self.roots = [str(root) for root in roots]
        self.unreadable = bool(unreadable)


_FOLDER_NAMES = {
    'Desktop': ('desktop', 'рабочий стол', 'рабочем столе', 'bureau', 'escritorio', 'scrivania', 'ambiente de trabalho', 'área de trabalho'),
    'Documents': ('documents', 'документы', 'документах', 'dokumente', 'documentos', 'documenti'),
    'Downloads': ('downloads', 'download', 'загрузки', 'загрузках', 'загрузок', 'téléchargements', 'telechargements', 'descargas', 'scaricati', 'transferências', 'transferencias'),
    'Pictures': ('pictures', 'изображения', 'изображениях', 'картинки', 'bilder', 'images', 'imágenes', 'imagenes', 'immagini', 'imagens'),
    'Videos': ('videos', 'видео', 'vidéos'),
    'Music': ('music', 'музыка', 'музыке', 'musik', 'musique', 'música', 'musica'),
}


def resolve_search_roots(root='', config=None) -> list[Path]:
    """Resolve an explicit local scope; an invalid scope never broadens search."""
    from jarvis.runtime.file_index import known_folders
    from jarvis.runtime.location_scan158 import is_link, local_drives
    raw = str(root or '').strip().strip('"«»')
    if raw:
        key = raw.casefold().replace('ё', 'е')
        folder = next((name for name, aliases in _FOLDER_NAMES.items() if key in aliases), '')
        if folder:
            selected = known_folders(folder)
            if not selected:
                raise FileNotFoundError(raw)
        elif key in {'home', 'profile', 'профиль', 'домашняя папка', 'benutzerordner', 'dossier personnel', 'carpeta personal', 'cartella personale', 'pasta pessoal'}:
            selected = [Path.home()]
        else:
            path = Path(os.path.expandvars(raw)).expanduser()
            if raw.startswith(('\\', '//')) or not path.is_absolute():
                raise ValueError('Search requires an existing local absolute folder or a known folder name')
            selected = [path]
        for path in selected:
            if not path.is_dir() or is_link(path):
                raise FileNotFoundError(str(path))
        return list(dict.fromkeys(path.resolve(strict=False) for path in selected))
    custom = config.get('file_index.roots', []) if config is not None else []
    if isinstance(custom, (str, Path)):
        custom = [custom]
    selected = list(known_folders())
    for value in custom if isinstance(custom, (list, tuple)) else []:
        selected.extend(resolve_search_roots(str(value)))
    # Common folders first; remaining profile/local volumes advance only while
    # an explicit command has a time budget. There is no idle whole-disk scan.
    selected.extend([Path.home(), *local_drives()])
    return list(dict.fromkeys(p.resolve(strict=False) for p in selected
                             if p.is_dir() and not str(p).startswith(('\\', '//')) and not is_link(p)))


def _roots() -> list[Path]:
    return resolve_search_roots()


_LIVE = OrderedDict()
_LIVE_LOCK = threading.Lock()


def _matches(name, parent, words, suffix, pattern='', literal_name=''):
    if literal_name:
        return literal_name.casefold() in name.casefold()
    if pattern:
        return fnmatch.fnmatchcase(name.casefold(), pattern.casefold())
    if suffix and not name.casefold().endswith(suffix):
        return False
    hay = (name + ' ' + str(parent)).casefold().replace('ё', 'е')
    return all(word in hay for word in words)


def _live_metadata(roots, words, suffix, *, deadline, limit, temporal=None,
                   include_dirs=False, recursive=True, pattern='', literal_name=''):
    from jarvis.runtime.file_scan162 import MetadataWalker, ScanPacer, relative_parent
    from jarvis.runtime.cancellation import cancellation_manager
    cancel = cancellation_manager().raise_if_cancelled
    key = (tuple(str(p) for p in roots), tuple(words), suffix, temporal, include_dirs, recursive, pattern, literal_name)
    # One scan at a time for overlapping desktop/Telegram commands. Waiting is
    # cancellable and uses the caller's original deadline, never a busy loop.
    while not _LIVE_LOCK.acquire(timeout=.02):
        cancel()
        if time.monotonic() >= deadline:
            return [], False, True
    try:
        state = _LIVE.get(key)
        if state and time.monotonic() - state['touched'] > (3. if state['walker'].complete else 300.):
            state['walker'].close()
            state = None
        if state is None:
            state = {'walker': MetadataWalker(roots, recursive=recursive), 'rows': {}, 'touched': time.monotonic()}
            _LIVE[key] = state
        _LIVE.move_to_end(key)
        while len(_LIVE) > 8:
            _, old = _LIVE.popitem(last=False)
            old['walker'].close()
        walker = state['walker']
        pace = ScanPacer(cancelled=cancel)
        while not walker.complete and time.monotonic() < deadline and len(state['rows']) < limit:
            cancel()
            pace.checkpoint()
            try:
                row = walker.next()
            except StopIteration:
                break
            if row is None:
                continue
            path, info, directory = row
            if (directory and not include_dirs) or not _matches(path.name, relative_parent(path,roots), words, suffix, pattern, literal_name):
                continue
            if temporal is not None and not temporal.matches(info.st_mtime):
                continue
            state['rows'][str(path)] = {'path': str(path), 'name': path.name, 'size': info.st_size,
                                       'mtime': info.st_mtime, 'kind': 'folder' if directory else 'file'}
        state['touched'] = time.monotonic()
        complete = walker.complete and not (walker.inaccessible or walker.truncated)
        rows = list(state['rows'].values())
        if walker.complete and not rows:
            _LIVE.pop(key, None)  # Never cache a negative lookup over new files.
        return rows, complete, not walker.complete and len(rows) < limit
    finally:
        _LIVE_LOCK.release()


@dataclass(frozen=True)
class FileTimeFilter:
    """Local-calendar constraints; an absent/quoted word is never a date filter."""
    start: date | None = None
    end: date | None = None  # exclusive
    hours: tuple[int, int] | None = None
    recent: bool = False

    @property
    def constrained(self):
        return self.start is not None or self.end is not None or self.hours is not None

    def matches(self, timestamp: float) -> bool:
        # Convert each timestamp in the OS local zone (including that date's DST
        # offset), rather than comparing UTC days or a hard-coded 24-hour span.
        modified = datetime.fromtimestamp(timestamp).astimezone()
        return not (
            (self.start is not None and modified.date() < self.start)
            or (self.end is not None and modified.date() >= self.end)
            or (self.hours is not None and not self.hours[0] <= modified.hour < self.hours[1])
        )


_TIME_PHRASES = (
    ("before_yesterday", "позавчера|day before yesterday|vorgestern|avant-hier|anteayer|l'altro ieri|l’altro ieri|anteontem"),
    ("last_week", "на прошлой неделе|за прошлую неделю|прошлой неделе|last week|letzte woche|letzten woche|semaine dernière|semaine derniere|semana pasada|settimana scorsa|semana passada"),
    ("this_week", "на этой неделе|за эту неделю|этой неделе|this week|diese woche|dieser woche|cette semaine|esta semana|questa settimana"),
    ("past_week", "за последнюю неделю|за последние семь дней|за последние 7 дней|за неделю|past week|last seven days|last 7 days|past seven days|past 7 days|letzten sieben tagen|letzten 7 tagen|sept derniers jours|7 derniers jours|dernière semaine|derniere semaine|últimos siete días|ultimos siete dias|últimos 7 días|ultimos 7 dias|ultimi sette giorni|ultimi 7 giorni|últimos sete dias|ultimos sete dias|últimos 7 dias|ultimos 7 dias"),
    ("today", "сегодня|today|heute|aujourd'hui|aujourd’hui|hoy|oggi|hoje"),
    ("yesterday", "вчера|yesterday|gestern|hier|ayer|ieri|ontem"),
    ("morning", "этим утром|утром|утро|this morning|in the morning|morning|am morgen|morgens|ce matin|matin|esta mañana|esta manana|por la mañana|por la manana|stamattina|di mattina|mattina|esta manhã|esta manha|de manhã|de manha"),
    ("evening", "вечером|вечер|this evening|in the evening|evening|am abend|abends|ce soir|soir|esta noche|por la noche|stasera|di sera|sera|esta noite|à noite"),
    ("recent", "последний|последние|последнюю|последнее|недавний|недавние|недавнюю|недавно|latest|newest|recent|recently|neueste|neuesten|neuster|kürzlich|kürzliche|récent|récents|récente|récentes|recentes|dernier|derniers|dernière|dernières|reciente|recientes|último|últimos|última|últimas|ultimo|ultimi|ultima|ultime|recente|recenti|recentemente"),
)
_TIME_RULES = tuple((kind, re.compile(r"(?<![\w.\\/-])(?:" + "|".join(re.escape(x) for x in phrases.split("|")) + r")(?![\w.\\/-])", re.I)) for kind, phrases in _TIME_PHRASES)
_QUOTED = re.compile(r'"[^"\n]*"|«[^»\n]*»|“[^”\n]*”|(?<!\w)\x27[^\x27\n]*\x27(?!\w)')
_QUERY_SHELL_WORDS = set("найди файл файлы документ документы который которую где лежит тот про за от с на в из find file files document documents where the from in on this of modified changed created last suche such finde datei dateien dokument dokumente vom von am der die das im den letzte trouve trouver cherche fichier fichiers document où le la les du de des d en ce modifié cree créé busca buscar encuentra archivo archivos documento documentos donde el la los las del de en por modificado criado crea trova cerca file documento documenti dove il lo gli le di del della da nel modificato procura encontre arquivo arquivos ficheiro ficheiros documento documentos onde o a os as do da dos das em no na alterado".split())


def parse_file_time(phrase: str, *, now: datetime | None = None) -> tuple[str, FileTimeFilter]:
    """Extract only standalone time phrases outside quoted literal file names.

    Date and time-of-day constraints compose, e.g. 'yesterday evening'. Compound
    file names and paths such as yesterday.txt / reports/today remain untouched.
    """
    phrase = str(phrase or "")
    masked = list(phrase)
    for quoted in _QUOTED.finditer(phrase):
        masked[quoted.start():quoted.end()] = " " * (quoted.end() - quoted.start())
    today = (now or datetime.now().astimezone()).date()
    start = end = hours = None
    recent = False
    removed = []
    for kind, pattern in _TIME_RULES:
        for match in list(pattern.finditer("".join(masked))):
            removed.append((match.start(), match.end()))
            masked[match.start():match.end()] = " " * (match.end() - match.start())
            day_start = day_end = None
            if kind in {"today", "yesterday", "before_yesterday"}:
                day_start = today - timedelta(days={"today": 0, "yesterday": 1, "before_yesterday": 2}[kind])
                day_end = day_start + timedelta(days=1)
            elif kind in {"last_week", "this_week"}:
                day_start = today - timedelta(days=today.weekday() + (7 if kind == "last_week" else 0))
                day_end = day_start + timedelta(days=7)
            elif kind == "past_week":
                day_start, day_end = today - timedelta(days=6), today + timedelta(days=1)
            elif kind in {"morning", "evening"}:
                selected = (5, 13) if kind == "morning" else (17, 24)
                hours = selected if hours is None else (max(hours[0], selected[0]), min(hours[1], selected[1]))
            elif kind == "recent":
                recent = True
            if day_start is not None:
                start = day_start if start is None else max(start, day_start)
                end = day_end if end is None else min(end, day_end)
    clean = list(phrase)
    for first, last in removed:
        clean[first:last] = " " * (last - first)
    return "".join(clean), FileTimeFilter(start, end, hours, recent)


def _human_time_match(path: Path, phrase: str) -> bool:
    try:
        return parse_file_time(phrase)[1].matches(path.stat().st_mtime)
    except (OSError, OverflowError, ValueError):
        return False


def search_files(query: str, *, roots: Iterable[Path] | None = None, limit: int = 50,
                 max_seconds: float = 3.0, include_dirs: bool = False,
                 recursive: bool = True, literal: bool = False, glob: bool = False) -> list[dict]:
    from jarvis.runtime.file_index import get_index, index_enabled
    from jarvis.runtime.cancellation import cancellation_manager
    query = str(query or '').strip()
    limit = min(500, max(1, int(limit)))
    deadline = time.monotonic() + max(0., min(30., float(max_seconds)))
    selected_roots = list(roots) if roots is not None else _roots()
    name_query, temporal = (query, FileTimeFilter()) if literal else parse_file_time(query)
    quoted_ranges = [(m.start(), m.end()) for m in _QUOTED.finditer(name_query)]
    words = []
    natural_shell = temporal.constrained or temporal.recent or bool(re.match(r"^(?:найди|find|suche|finde|trouve|cherche|busca|encuentra|trova|cerca|procura|encontre)\s", name_query, re.I))
    for match in re.finditer(r"[\w.-]+", name_query, re.UNICODE):
        word = match.group().casefold().replace('ё', 'е')
        quoted = any(start < match.start() < end for start, end in quoted_ranges)
        if quoted or literal or not natural_shell or word not in _QUERY_SHELL_WORDS:
            words.append(word)
    words = words[:32]
    suffix = next((x for x in ('.xlsx', '.xls', '.pdf', '.docx', '.pptx', '.txt', '.png', '.jpg', '.zip')
                   if re.search(r'(?<![\w])' + re.escape(x.lstrip('.')) + r'(?![\w])', query.casefold())), '')
    pattern = ''
    literal_name = query if literal and not glob else ''
    if glob or (not literal and any(ch in query for ch in '*?[')):
        pattern = query if any(ch in query for ch in '*?[') else '*' + query + '*'
    candidate_limit = min(5000, max(200, limit * 20))
    cancellation_manager().raise_if_cancelled()
    records = []
    incomplete = continuable = False
    index = None
    refreshed = False
    if index_enabled() and not (pattern or literal_name or not recursive) and time.monotonic() < deadline:
        index = get_index(selected_roots)
        if not index.continuable and index.last_refresh and not index.needs_refresh:
            index.validate_freshness(max_seconds=min(.04, max(0., deadline-time.monotonic())*.2))
        if index.continuable or index.needs_refresh or not index.last_refresh or time.monotonic() - index.last_refresh >= 300:
            index.refresh(max_seconds=max(0., deadline-time.monotonic())*.8, correctness_required=True)
            refreshed = True
        records = index.search(words, suffix=suffix, limit=candidate_limit, require_all=True,
                               max_seconds=max(0., deadline-time.monotonic()), include_dirs=include_dirs)
        incomplete = index.incomplete or index.query_incomplete or index.freshness_pending or len(records) >= candidate_limit
        continuable = index.continuable or index.freshness_pending or index.query_incomplete
    # A new file can arrive before a watcher event. Only a previously complete
    # index with no match needs a live check: never duplicate an in-flight scan.
    if index is None or (not records and not refreshed and not continuable):
        records, complete, continuable = _live_metadata(selected_roots, words, suffix,
            deadline=deadline, limit=candidate_limit, temporal=temporal,
            include_dirs=include_dirs, recursive=recursive, pattern=pattern, literal_name=literal_name)
        incomplete = not complete
    from jarvis.runtime.file_scan162 import ScanPacer
    validation_pace = ScanPacer(cancelled=cancellation_manager().raise_if_cancelled)
    scored = []
    for row in records:
        cancellation_manager().raise_if_cancelled()
        validation_pace.checkpoint()
        path = Path(row['path'])
        try:
            info = path.stat()
        except OSError:
            continue
        is_expected_kind = stat.S_ISDIR(info.st_mode) if row.get('kind') == 'folder' else stat.S_ISREG(info.st_mode)
        if not is_expected_kind or not temporal.matches(info.st_mtime):
            continue
        normalized = path.name.casefold().replace('ё', 'е')
        score = sum(word in normalized for word in words) / max(1, len(words))
        if normalized == name_query.strip('"«»').casefold().replace('ё', 'е'):
            score += 2.
        if temporal.recent:
            score += max(0., 1.-(time.time()-info.st_mtime)/(7*86400))*.5
        scored.append((score, info.st_mtime, {'path': str(path), 'name': path.name,
            'modified': datetime.fromtimestamp(info.st_mtime).isoformat(), 'size': info.st_size,
            'kind': row.get('kind', 'file'), 'score': round(score, 3)}))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]['path'].casefold()))
    out = [row[2] for row in scored[:limit]]
    return FileSearchResults(out, incomplete=incomplete or len(scored)>limit,
        indexing=False, continuable=continuable, roots=selected_roots)


def format_search_results(rows, language='ru') -> str:
    """Render actual full paths in chat; structured data alone is not visible."""
    texts = {
      'ru': ('Найдено: {n}.', 'В проверенных папках совпадений не найдено.', 'Поиск охватил часть папок. Повторите запрос, чтобы продолжить с места остановки.', 'Часть папок недоступна или достигнут лимит результатов. Уточните папку или название.'),
      'en': ('Found: {n}.', 'No matches in the checked folders.', 'Some folders remain. Repeat the request to continue from this point.', 'Some folders were unavailable or the result limit was reached. Refine the folder or name.'),
      'de': ('Gefunden: {n}.', 'Keine Treffer in den geprüften Ordnern.', 'Einige Ordner fehlen noch. Wiederhole die Anfrage, um die Suche fortzusetzen.', 'Einige Ordner waren nicht zugänglich oder das Ergebnislimit wurde erreicht. Grenze Ordner oder Namen ein.'),
      'fr': ('Résultats : {n}.', 'Aucun résultat dans les dossiers vérifiés.', 'Certains dossiers restent à vérifier. Répétez la demande pour poursuivre la recherche.', 'Certains dossiers étaient inaccessibles ou la limite de résultats a été atteinte. Précisez le dossier ou le nom.'),
      'es': ('Resultados: {n}.', 'No hay coincidencias en las carpetas revisadas.', 'Quedan carpetas por revisar. Repite la petición para continuar desde este punto.', 'Algunas carpetas no estaban disponibles o se alcanzó el límite. Especifica la carpeta o el nombre.'),
      'it': ('Risultati: {n}.', 'Nessuna corrispondenza nelle cartelle controllate.', 'Restano cartelle da controllare. Ripeti la richiesta per continuare da qui.', 'Alcune cartelle non erano accessibili o è stato raggiunto il limite. Precisa la cartella o il nome.'),
      'pt': ('Resultados: {n}.', 'Sem correspondências nas pastas verificadas.', 'Ainda faltam pastas. Repete o pedido para continuar a partir deste ponto.', 'Algumas pastas estavam inacessíveis ou foi atingido o limite. Especifica a pasta ou o nome.'),
    }
    lineset = texts.get(str(language).split('-')[0], texts['en'])
    lines = [lineset[0].format(n=len(rows)) if rows else lineset[1]]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. {row['path']}")
        if row.get('snippet'):
            lines.append('   '+str(row['snippet']))
    if getattr(rows, 'incomplete', False):
        lines.append(lineset[2] if getattr(rows, 'continuable', False) else lineset[3])
    if getattr(rows, 'unreadable', False):
        unread = {
          'ru': 'Содержимое некоторых документов прочитать не удалось; отсутствие совпадений в них не подтверждено.',
          'en': 'Some documents could not be read; matches in them cannot be ruled out.',
          'de': 'Einige Dokumente konnten nicht gelesen werden; Treffer darin sind weiterhin möglich.',
          'fr': 'Certains documents n’ont pas pu être lus ; ils peuvent contenir des résultats.',
          'es': 'No se pudieron leer algunos documentos; podrían contener coincidencias.',
          'it': 'Alcuni documenti non sono stati letti; potrebbero contenere corrispondenze.',
          'pt': 'Não foi possível ler alguns documentos; podem conter correspondências.',
        }
        lines.append(unread.get(str(language).split('-')[0], unread['en']))
    return '\n'.join(lines)


def _office_text(path: Path, max_chars: int = 2_000_000) -> str:
    members = {".docx": ("word/",), ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/"), ".pptx": ("ppt/slides/",)}
    prefixes = members.get(path.suffix.casefold(), ())
    chunks: list[str] = []
    with zipfile.ZipFile(path) as zf:
        remaining=max_chars*4
        for name in zf.namelist():
            if any(name.startswith(prefix) or name == prefix for prefix in prefixes) and name.endswith(".xml"):
                if remaining<=0:
                    break
                with zf.open(name) as member:
                    data=member.read(min(remaining,2_000_000))
                remaining-=len(data)
                raw = data.decode("utf-8", errors="ignore")
                chunks.append(re.sub(r"<[^>]+>", " ", raw))
    return " ".join(chunks)


def _pdf_text(path: Path, max_chars: int = 2_000_000, timeout_seconds: float = 8.0) -> str:
    # Prefer pdftotext when present; do not add a heavyweight PDF dependency to the
    # assistant runtime just for indexing.
    import subprocess
    if max_chars <= 0 or timeout_seconds <= 0:
        return ""
    process = None
    chunks = []
    try:
        process = subprocess.Popen(["pdftotext", "-layout", str(path), "-"], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, **hidden_process_kwargs())
        def read_bounded():
            remaining = max_chars * 4
            try:
                while remaining > 0:
                    data = process.stdout.read(min(65536, remaining))
                    if not data:
                        return
                    chunks.append(data)
                    remaining -= len(data)
                if process.poll() is None:
                    process.terminate()
            except (OSError, ValueError):
                pass
        reader = threading.Thread(target=read_bounded, name="NEO-PDF-text", daemon=True)
        reader.start()
        from jarvis.runtime.cancellation import cancellation_manager
        deadline = time.monotonic() + max(.05, timeout_seconds)
        while process.poll() is None:
            cancellation_manager().raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait(timeout=1)
                break
            try:
                process.wait(timeout=min(.1, remaining))
            except subprocess.TimeoutExpired:
                continue
        reader.join(timeout=1)
        return b"".join(chunks).decode("utf-8", errors="replace")[:max_chars]
    except OperationCancelled:
        raise
    except Exception:
        pass
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass
    return ""


def read_searchable_text(path: Path, max_chars: int = 2_000_000, *, timeout_seconds: float = 8.0) -> str:
    ext = path.suffix.casefold()
    try:
        if ext in _TEXT_EXTS:
            with path.open(encoding="utf-8",errors="replace") as stream:
                return stream.read(max_chars)
        if ext in _OFFICE_EXTS:
            return _office_text(path,max_chars)[:max_chars]
        if ext == ".pdf":
            return _pdf_text(path,max_chars,timeout_seconds)
    except OperationCancelled:
        raise
    except Exception:
        return ""
    return ""


def search_content(query: str, *, roots: Iterable[Path] | None = None, limit: int = 30, max_seconds: float = 8.0) -> list[dict]:
    from jarvis.runtime.file_scan162 import MetadataWalker, ScanPacer
    from jarvis.runtime.cancellation import cancellation_manager
    q = str(query or '').strip().strip('"«»').casefold().replace('ё', 'е')
    if not q:
        return FileSearchResults()
    tokens = [x for x in re.findall(r"[\w.-]+", q, re.UNICODE)] or [q]
    selected = list(roots) if roots is not None else _roots()
    limit = min(100, max(1, int(limit)))
    deadline = time.monotonic() + max(0., min(30., float(max_seconds)))
    supported = _TEXT_EXTS | _OFFICE_EXTS | {'.pdf'}
    cancel = cancellation_manager().raise_if_cancelled
    key = ('content', tuple(str(p) for p in selected), q)
    while not _LIVE_LOCK.acquire(timeout=.02):
        cancel()
        if time.monotonic() >= deadline:
            return FileSearchResults(incomplete=True, continuable=True, roots=selected)
    try:
        state = _LIVE.get(key)
        if state and time.monotonic() - state['touched'] > (3. if state['walker'].complete else 300.):
            state['walker'].close()
            state = None
        if state is None:
            state = {'walker': MetadataWalker(selected), 'rows': {}, 'touched': time.monotonic(), 'truncated': False, 'unreadable': False, 'pending': None}
            _LIVE[key] = state
        _LIVE.move_to_end(key)
        while len(_LIVE) > 8:
            _, old = _LIVE.popitem(last=False)
            old['walker'].close()
        walker = state['walker']
        pace = ScanPacer(cancelled=cancel)
        while (not walker.complete or state.get('pending')) and time.monotonic() < deadline and len(state['rows']) < limit:
            cancel()
            pace.checkpoint()
            try:
                row = state.get('pending') or walker.next()
            except StopIteration:
                break
            if row is None:
                continue
            path, info, directory = row
            if directory or path.suffix.casefold() not in supported:
                continue
            # Bound decoding/decompression per file too, not only directory I/O.
            maximum = 262144
            state['pending'] = row
            text = read_searchable_text(path, maximum, timeout_seconds=max(.01, deadline-time.monotonic()))
            if len(text) >= maximum or (path.suffix.casefold() in _TEXT_EXTS and info.st_size > maximum):
                state['truncated'] = True
            cancel()
            pace.checkpoint(force=True)
            state['pending'] = None
            if not text:
                if info.st_size:
                    state['unreadable'] = True
                continue
            norm = text.casefold().replace('ё', 'е')
            if not all(token in norm for token in tokens):
                continue
            first = min((norm.find(token) for token in tokens), default=0)
            snippet = re.sub(r"\s+", ' ', text[max(0,first-120):first+420]).strip()
            state['rows'][str(path)] = {'path': str(path), 'name': path.name, 'hits': tokens,
                                      'snippet': snippet, 'kind': 'file'}
        state['touched'] = time.monotonic()
        out = [row for row in state['rows'].values() if Path(row['path']).is_file()]
        complete = walker.complete and not (walker.inaccessible or walker.truncated or state['truncated'] or state['unreadable'])
        if walker.complete and not out:
            _LIVE.pop(key, None)
        return FileSearchResults(out, incomplete=not complete,
            continuable=(not walker.complete or bool(state.get('pending'))) and len(out)<limit, roots=selected, unreadable=state['unreadable'])
    finally:
        _LIVE_LOCK.release()

