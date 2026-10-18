"""NEO 169: local search in the folders the user explicitly allowed (retrieval for answers).

* Only folders listed in ``knowledge.folders`` are read; NEO never scans the disk on its own.
* The index (SQLite FTS5, ``<data>/knowledge/index.sqlite3``) is updated incrementally:
  a file is re-read only when its size or modification time changed.
* Indexing runs in one background thread, throttled, outside the voice command path.
* Every answer names the file and, where known, the page, section or lines. When the
  index has nothing relevant, NEO says so instead of inventing content.
* File text is data for the answer; it never becomes a command.
"""
from __future__ import annotations

from dataclasses import dataclass
import html
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator
import zipfile

logger = logging.getLogger(__name__)

TEXT_SUFFIXES = {'.txt', '.md', '.markdown', '.rst', '.csv', '.tsv', '.json', '.log', '.ini', '.cfg', '.toml', '.yaml', '.yml',
                 '.py', '.js', '.ts', '.html', '.htm', '.xml', '.bat', '.ps1', '.sql', '.srt'}
DOC_SUFFIXES = {'.docx', '.pdf'}
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '$recycle.bin', 'appdata', '.cache'}
MAX_FILE_BYTES = 20 * 1024 * 1024
CHUNK_CHARS = 700
_WORD = re.compile(r'\w{2,}', re.U)


@dataclass
class Chunk:
    location: str
    text: str


# --------------------------------------------------------------- extraction
def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ('utf-8-sig', 'cp1251', 'latin-1'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ''


def _paragraph_chunks(paragraphs: Iterator[tuple[str, str]]) -> list[Chunk]:
    """Merge (location, paragraph) pairs into ~CHUNK_CHARS chunks that keep their first location."""
    chunks: list[Chunk] = []
    buffer, where = [], ''
    for location, paragraph in paragraphs:
        paragraph = ' '.join(paragraph.split())
        if not paragraph:
            continue
        if buffer and (sum(len(x) for x in buffer) + len(paragraph) > CHUNK_CHARS or location != where):
            chunks.append(Chunk(where, ' '.join(buffer)))
            buffer = []
        if not buffer:
            where = location
        while len(paragraph) > CHUNK_CHARS * 2:
            chunks.append(Chunk(location, paragraph[:CHUNK_CHARS * 2]))
            paragraph = paragraph[CHUNK_CHARS * 2:]
        buffer.append(paragraph)
    if buffer:
        chunks.append(Chunk(where, ' '.join(buffer)))
    return chunks


def _text_paragraphs(text: str, markdown: bool) -> Iterator[tuple[str, str]]:
    heading, start, lines = '', 1, []
    for number, line in enumerate(text.splitlines(), 1):
        if markdown and re.match(r'^\s{0,3}#{1,6}\s+\S', line):
            if lines:
                yield (f'h:{heading}' if heading else f'l:{start}-{number - 1}'), '\n'.join(lines)
            heading, start, lines = line.strip('# \t'), number + 1, []
            continue
        if not line.strip():
            if lines:
                yield (f'h:{heading}' if heading else f'l:{start}-{number - 1}'), '\n'.join(lines)
            start, lines = number + 1, []
            continue
        lines.append(line)
    if lines:
        yield (f'h:{heading}' if heading else f'l:{start}-{start + len(lines) - 1}'), '\n'.join(lines)


def _docx_paragraphs(path: Path) -> Iterator[tuple[str, str]]:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read('word/document.xml').decode('utf-8', 'replace')
    heading = ''
    for paragraph in re.findall(r'<w:p[ >].*?</w:p>', xml, re.S):
        text = html.unescape(re.sub(r'<[^>]+>', '', re.sub(r'<w:tab/>', ' ', paragraph))).strip()
        if not text:
            continue
        if re.search(r'<w:pStyle w:val="(?:Heading|Title|Заголовок)[^"]*"', paragraph):
            heading = text[:80]
            continue
        yield (f'h:{heading}' if heading else ''), text


def _pdf_paragraphs(path: Path) -> Iterator[tuple[str, str]]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return
    reader = PdfReader(str(path))
    for number, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ''
        except Exception:
            continue
        for block in re.split(r'\n\s*\n', text):
            yield f'p:{number}', block


def extract(path: Path) -> list[Chunk]:
    suffix = path.suffix.casefold()
    if suffix == '.docx':
        return _paragraph_chunks(_docx_paragraphs(path))
    if suffix == '.pdf':
        return _paragraph_chunks(_pdf_paragraphs(path))
    text = _read_text(path)
    if suffix in {'.html', '.htm', '.xml'}:
        text = html.unescape(re.sub(r'<[^>]+>', ' ', re.sub(r'(?is)<(script|style).*?</\1>', ' ', text)))
    return _paragraph_chunks(_text_paragraphs(text, suffix in {'.md', '.markdown'}))


# -------------------------------------------------------------------- index
class KnowledgeIndex:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, size INTEGER, chunks INTEGER, error TEXT)')
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path UNINDEXED, location UNINDEXED, text, tokenize='unicode61 remove_diacritics 2')")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # ---- updates
    def update_folders(self, folders: list[str], *, cancel: Callable[[], bool] = lambda: False,
                       pause: float = 0.01, max_files: int = 20000) -> dict[str, int]:
        """Incremental update: new/changed files are read, removed files are dropped."""
        stats = {'seen': 0, 'indexed': 0, 'removed': 0, 'skipped': 0, 'errors': 0}
        seen: set[str] = set()
        with self._lock, self._connect() as db:
            known = {row['path']: (row['mtime'], row['size']) for row in db.execute('SELECT path, mtime, size FROM files')}
        for folder in folders:
            root = Path(folder)
            if not root.is_dir():
                continue
            for path in self._walk(root):
                if cancel() or stats['seen'] >= max_files:
                    return stats
                stats['seen'] += 1
                key = str(path)
                seen.add(key)
                try:
                    info = path.stat()
                except OSError:
                    continue
                if info.st_size > MAX_FILE_BYTES:
                    stats['skipped'] += 1
                    continue
                if known.get(key) == (info.st_mtime, info.st_size):
                    continue
                self._index_file(path, info)
                stats['indexed'] += 1
                if pause:
                    time.sleep(pause)   # stay light: never saturate the disk or a CPU core
        allowed = [str(Path(f)) for f in folders]
        with self._lock, self._connect() as db:
            for key in list(known):
                inside = any(key == root or key.startswith(root.rstrip('\\/') + os.sep) for root in allowed)
                if key not in seen or not inside:
                    db.execute('DELETE FROM chunks WHERE path = ?', (key,))
                    db.execute('DELETE FROM files WHERE path = ?', (key,))
                    stats['removed'] += 1
        return stats

    @staticmethod
    def _walk(root: Path) -> Iterator[Path]:
        for current, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d.casefold() not in SKIP_DIRS and not d.startswith('.')]
            for name in files:
                suffix = os.path.splitext(name)[1].casefold()
                if suffix in TEXT_SUFFIXES or suffix in DOC_SUFFIXES:
                    yield Path(current) / name

    def _index_file(self, path: Path, info: os.stat_result) -> None:
        error = ''
        try:
            chunks = extract(path)
        except Exception as exc:
            chunks, error = [], type(exc).__name__
        with self._lock, self._connect() as db:
            db.execute('DELETE FROM chunks WHERE path = ?', (str(path),))
            db.executemany('INSERT INTO chunks(path, location, text) VALUES(?, ?, ?)',
                           [(str(path), c.location, c.text) for c in chunks[:5000]])
            db.execute('INSERT OR REPLACE INTO files(path, mtime, size, chunks, error) VALUES(?, ?, ?, ?, ?)',
                       (str(path), info.st_mtime, info.st_size, len(chunks), error))

    def drop_folder(self, folder: str) -> int:
        prefix = str(Path(folder)).rstrip('\\/') + os.sep
        with self._lock, self._connect() as db:
            paths = [row['path'] for row in db.execute('SELECT path FROM files')
                     if row['path'].startswith(prefix)]
            for key in paths:
                db.execute('DELETE FROM chunks WHERE path = ?', (key,))
                db.execute('DELETE FROM files WHERE path = ?', (key,))
        return len(paths)

    # ---- search
    @staticmethod
    def _query(text: str) -> str:
        terms = []
        for word in _WORD.findall(str(text or '').casefold()):
            if len(word) < 3 or word in _STOP:
                continue
            # crude stemming for inflected languages: match the first 6 letters as a prefix
            stem = word[:6] if len(word) > 6 else word
            terms.append('"' + stem.replace('"', '') + '"*')
        return ' OR '.join(dict.fromkeys(terms))

    def search(self, text: str, limit: int = 5, *, files: list[str] | None = None) -> list[dict[str, Any]]:
        """FTS search. ``files`` (NEO 188 NEO-DOC-001) restricts candidates BEFORE ranking and LIMIT."""
        query = self._query(text)
        if not query or (files is not None and not files):
            return []
        with self._lock, self._connect() as db:
            try:
                if files is not None:
                    marks = ','.join('?' * len(files))
                    rows = db.execute('SELECT path, location, text, bm25(chunks) AS score FROM chunks WHERE chunks MATCH ? '
                                      f'AND path IN ({marks}) ORDER BY score LIMIT ?', (query, *files, limit)).fetchall()
                else:
                    rows = db.execute('SELECT path, location, text, bm25(chunks) AS score FROM chunks WHERE chunks MATCH ? '
                                      'ORDER BY score LIMIT ?', (query, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [{'file': row['path'], 'location': row['location'], 'text': row['text'], 'score': float(row['score'])} for row in rows]

    def files(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            return [dict(row) for row in db.execute('SELECT path, mtime, size, chunks, error FROM files')]

    def first_chunks(self, path: str, limit: int = 3) -> list[dict[str, Any]]:
        """Opening fragments of ONE file (a comparison without a topic)."""
        with self._lock, self._connect() as db:
            rows = db.execute('SELECT path, location, text FROM chunks WHERE path = ? LIMIT ?', (path, limit)).fetchall()
        return [{'file': row['path'], 'location': row['location'], 'text': row['text'], 'score': 0.0} for row in rows]

    def stats(self) -> dict[str, int]:
        with self._lock, self._connect() as db:
            files = db.execute('SELECT COUNT(*) FROM files').fetchone()[0]
            chunks = db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
        return {'files': int(files), 'chunks': int(chunks)}


_STOP = {w for w in '''что как где когда это эта этот мой моя мои мне про для или при над под все всё был была были есть
the and for with that this what where when from about have has are was were you your into
der die das und mit von für ist sind ein eine les des une pour avec dans que qui est los las una para con por che per
una del della sono não uma para com por que'''.split()}


# ------------------------------------------------------------ service layer
class KnowledgeService:
    """Owns the index for one NEO profile and its single background indexer."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.index = KnowledgeIndex(Path(config.paths.root) / 'knowledge' / 'index.sqlite3')
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_scan = 0.0
        self.last_stats: dict[str, int] = {}

    def folders(self) -> list[str]:
        return [str(x) for x in (self.config.get('knowledge.folders', []) or []) if str(x).strip()]

    def _save_folders(self, folders: list[str]) -> None:
        import json
        data = json.loads(json.dumps(self.config.data))
        data.setdefault('knowledge', {})['folders'] = folders
        self.config.save(data)

    def add_folder(self, folder: Path) -> list[str]:
        folders = self.folders()
        value = str(Path(folder).resolve())
        if value not in folders:
            folders.append(value)
            self._save_folders(folders)
        self.refresh(force=True)
        return folders

    def resolve_folders(self, folder: str) -> list[str]:
        """NEO 188 NEO-PRIVACY-001: the exact grant(s) a request names — never the first of several.

        A full path names exactly one grant; a short name names every grant with
        that folder name (the caller asks which one when there are several)."""
        import ntpath
        value = str(folder or '').strip().strip('"«»“”').rstrip('\\/')
        if not value:
            return []
        folders = self.folders()
        key = lambda p: str(p).replace('/', '\\').rstrip('\\').casefold()
        exact = [f for f in folders if key(f) == key(value)]
        if exact:
            return exact[:1]
        if any(sep in value for sep in ('\\', '/')):
            return []                    # a path that is not granted is never matched by its last part
        name = value.casefold()
        return [f for f in folders if ntpath.basename(str(f).replace('/', '\\').rstrip('\\')).casefold() == name]

    def revoke(self, path: str) -> str:
        """Revoke ONE exact grant: 'removed' | 'missing' | 'save_failed' | 'index_failed'."""
        folders = self.folders()
        if path not in folders:
            return 'missing'
        folders.remove(path)
        try:
            self._save_folders(folders)
        except Exception:
            logger.exception('Knowledge folder revocation could not be saved')
            return 'save_failed'
        try:
            self.index.drop_folder(path)
        except Exception:
            logger.exception('Knowledge index entries of a revoked folder could not be removed')
            return 'index_failed'
        return 'removed'

    def remove_folder(self, folder: str) -> bool:
        matches = self.resolve_folders(folder)
        if len(matches) != 1:
            return False                 # nothing or ambiguous: no grant changes
        return self.revoke(matches[0]) in {'removed', 'index_failed'}

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def refresh(self, *, force: bool = False, wait: bool = False) -> None:
        """Start an incremental update in the background (at most one at a time)."""
        interval = float(self.config.get('knowledge.rescan_minutes', 30) or 30) * 60
        if self.busy() or (not force and time.monotonic() - self._last_scan < interval) or not self.folders():
            if wait and self._thread is not None:
                self._thread.join()
            return
        self._last_scan = time.monotonic()
        self._stop.clear()
        pause = max(0.0, float(self.config.get('knowledge.pause_ms', 10) or 0) / 1000.0)

        def work() -> None:
            started = time.perf_counter()
            try:
                self.last_stats = self.index.update_folders(self.folders(), cancel=self._stop.is_set, pause=pause)
            except Exception:
                logger.exception('Knowledge indexing failed')
                return
            elapsed = (time.perf_counter() - started) * 1000
            logger.info('Knowledge index %.0f ms: %s', elapsed, self.last_stats)
            try:
                from jarvis.runtime.developer import developer_bus
                developer_bus().emit('latency', 'knowledge_index', data={**self.last_stats, 'duration_ms': elapsed}, duration_ms=elapsed)
            except Exception:
                pass

        self._thread = threading.Thread(target=work, name='neo-knowledge-index', daemon=True)
        self._thread.start()
        if wait:
            self._thread.join()

    def stop(self) -> None:
        self._stop.set()

    def search(self, text: str, limit: int = 5) -> list[dict[str, Any]]:
        self.refresh()   # cheap unless the rescan interval passed; never blocks the answer
        return self.index.search(text, limit)


_SERVICES: dict[str, KnowledgeService] = {}
_SERVICES_LOCK = threading.Lock()


def service(config: Any) -> KnowledgeService:
    key = str(Path(config.paths.root))
    with _SERVICES_LOCK:
        if key not in _SERVICES:
            _SERVICES[key] = KnowledgeService(config)
        return _SERVICES[key]


_WHERE = {'p': ('стр. {}', 'p. {}', 'S. {}', 'p. {}', 'pág. {}', 'p. {}', 'pág. {}'),
          'h': ('раздел «{}»', 'section “{}”', 'Abschnitt „{}“', 'section « {} »', 'sección «{}»', 'sezione «{}»', 'secção «{}»'),
          'l': ('строки {}', 'lines {}', 'Zeilen {}', 'lignes {}', 'líneas {}', 'righe {}', 'linhas {}')}
_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def location_text(location: str, code: str) -> str:
    kind, _, value = str(location or '').partition(':')
    if kind not in _WHERE or not value:
        return ''
    return _WHERE[kind][_CODES.index(code) if code in _CODES else 1].format(value.replace('-', '–'))


def cite(sources: list[dict[str, Any]], code: str = 'en') -> str:
    lines = []
    for number, source in enumerate(sources, 1):
        where = location_text(source.get('location', ''), code)
        lines.append(f"[{number}] {Path(source['file']).name}" + (f', {where}' if where else ''))
    return '\n'.join(lines)
