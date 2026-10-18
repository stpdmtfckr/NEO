"""Bounded local metadata index with OS notifications; no document ingestion."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from jarvis.runtime.resource_budget import resource_budget

logger = logging.getLogger(__name__)
_policy_config = None
_SKIP_DIRS = {"node_modules", "venv", "__pycache__", "$recycle.bin", "system volume information"}


def index_enabled() -> bool:
    return bool(_policy_config.get("file_index.enabled", True)) if _policy_config is not None else True


def configure_index_policy(config):
    global _policy_config
    _policy_config = config
    if not index_enabled():
        close_indexes()


def known_folders(name: str = "") -> list[Path]:
    names = (name,) if name else ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")
    registry_names = {"Documents": "Personal", "Pictures": "My Pictures", "Videos": "My Video", "Music": "My Music", "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
                      "Saved Games":"{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}"}
    roots = []
    for folder in names:
        path = Path.home() / folder
        if os.name == "nt":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                    raw = winreg.QueryValueEx(key, registry_names.get(folder, folder))[0]
                    path = Path(os.path.expandvars(raw))
            except OSError:
                pass
        candidates = [path]
        for variable in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            if os.environ.get(variable):
                candidates.append(Path(os.environ[variable]) / folder)
        roots.extend(p.resolve(strict=False) for p in candidates if p.is_dir())
    return list(dict.fromkeys(roots))


def directory_names(base: str, names: list[str]) -> list[str]:
    from jarvis.runtime.location_scan158 import is_link
    return [name for name in names if not name.startswith(".") and name.casefold() not in _SKIP_DIRS
            and not is_link(Path(base) / name)]


def _literal_like(value):
    return str(value).replace(chr(92), chr(92)*2).replace("%", chr(92)+"%").replace("_", chr(92)+"_")


@contextmanager
def _database(path, timeout=3):
    db = sqlite3.connect(path, timeout=timeout)
    try:
        with db:
            yield db
    finally:
        db.close()


class FileIndex:
    def __init__(self, path: Path, roots: list[Path]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.roots = list(dict.fromkeys(p.resolve(strict=False) for p in roots))
        self._lock = threading.Lock()
        self._schedule_lock = threading.Lock()
        self._thread = self._observer = self._event_thread = None
        self._events = queue.Queue(maxsize=2048)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._complete = False
        self._needs_refresh = False
        self.last_refresh = 0.0
        self._scan = None
        self._scan_generation = 0.0
        self._scan_batch = []
        self.query_incomplete = False
        self._query_lock = threading.Lock()
        self._queries = OrderedDict()
        self._event_revision = 0
        self._directory_stamps = {}
        self._validation = None
        self.freshness_pending = False
        with _database(self.path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,name TEXT,search TEXT,size INTEGER,mtime REAL,seen REAL)")
            db.execute("CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime)")
            db.execute("CREATE TABLE IF NOT EXISTS directories(path TEXT PRIMARY KEY,name TEXT,search TEXT,size INTEGER,mtime REAL,seen REAL)")
            db.execute("CREATE INDEX IF NOT EXISTS directories_mtime ON directories(mtime)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT)")
            previous = db.execute("SELECT value FROM metadata WHERE key='complete'").fetchone()
            self._complete = bool(previous and previous[0] == "1")
            if self._complete:
                self._ready.set()

    @property
    def incomplete(self):
        return not self._complete or self._needs_refresh

    def wait_ready(self, timeout):
        return self._ready.wait(max(0.0, timeout))

    def _search_text(self, path):
        from jarvis.runtime.file_scan162 import relative_parent
        return (path.name+" "+relative_parent(path,self.roots)).casefold().replace("ё","е")

    def _entry(self, path, seen):
        try:
            if path.is_symlink() or not path.is_file() or path.resolve(strict=False) == self.path.resolve(strict=False):
                return None
            stat = path.stat()
            return (str(path), path.name, self._search_text(path), stat.st_size, stat.st_mtime, seen)
        except OSError:
            return None

    def _flush_scan_batch(self, db):
        # Keep uncommitted records with the resumable cursor. Cancellation or a
        # failed SQLite transaction must never advance past files without rows.
        for table, directory in (('files', False), ('directories', True)):
            rows = [entry for is_dir, entry in self._scan_batch if is_dir == directory]
            if rows:
                db.executemany('INSERT OR REPLACE INTO '+table+' VALUES(?,?,?,?,?,?)', rows)
        db.commit()
        self._scan_batch.clear()

    @property
    def needs_refresh(self):
        return self._needs_refresh

    def validate_freshness(self, max_seconds=.04):
        """Revalidate cached directories only on a user's next lookup.

        This replaces recursive whole-volume watchers. A large tree is checked
        in resumable, paced slices and stays explicitly partial until verified.
        """
        from jarvis.runtime.file_scan162 import ScanPacer
        from jarvis.runtime.cancellation import cancellation_manager
        cancel = cancellation_manager().raise_if_cancelled
        if not self._lock.acquire(blocking=False):
            self.freshness_pending = True
            return False
        try:
            if self._scan is not None or self._needs_refresh:
                return False
            if not self._directory_stamps and self.roots:
                self._needs_refresh = True
                self._complete = False
                return False
            if self._validation is None:
                self._validation = iter(self._directory_stamps.items())
            self.freshness_pending = True
            deadline = time.monotonic() + max(0., float(max_seconds))
            pace = ScanPacer(cancelled=cancel)
            while time.monotonic() < deadline:
                cancel()
                pace.checkpoint()
                try:
                    raw, expected = next(self._validation)
                except StopIteration:
                    self._validation = None
                    self.freshness_pending = False
                    return True
                try:
                    info = Path(raw).stat(follow_symlinks=False)
                    actual = (info.st_mtime_ns, info.st_ctime_ns)
                except OSError:
                    actual = None
                if actual != expected:
                    self._needs_refresh = True
                    self._complete = False
                    self._validation = None
                    self.freshness_pending = False
                    return False
            return False
        finally:
            self._lock.release()

    @property
    def continuable(self):
        return self._scan is not None and not self._scan.complete

    def refresh(self, max_seconds=120, *, correctness_required=False):
        """Advance one scan generation, preserving its cursor across time slices.

        Commands call this synchronously on their worker instead of starting an
        index scan and a second simultaneous fallback traversal of the same tree.
        """
        from jarvis.runtime.file_scan162 import MetadataWalker, ScanPacer
        from jarvis.runtime.cancellation import cancellation_manager
        if self._stop.is_set() or not index_enabled() or not self._lock.acquire(blocking=False):
            return
        deadline = time.monotonic() + max(0., float(max_seconds))
        cancel = cancellation_manager().raise_if_cancelled if correctness_required else None
        pace = ScanPacer(stop=self._stop, cancelled=cancel)
        finished = False
        try:
            if self._scan is None:
                self._scan = MetadataWalker(self.roots)
                self._scan_generation = time.time()
                self._validation = None
                self.freshness_pending = False
            scan, generation = self._scan, self._scan_generation
            self._complete = False
            self._needs_refresh = True
            with _database(self.path) as db:
                db.execute("INSERT OR REPLACE INTO metadata VALUES('complete','0')")
                while time.monotonic() < deadline and not self._stop.is_set() and index_enabled():
                    if resource_budget().should_defer_heavy_background(correctness_required=correctness_required):
                        break
                    if not pace.checkpoint():
                        break
                    try:
                        row = scan.next()
                    except StopIteration:
                        finished = True
                        break
                    if row is None:
                        continue
                    path, info, directory = row
                    if path == self.path or str(path) in {str(self.path)+'-wal', str(self.path)+'-shm'}:
                        continue
                    self._scan_batch.append((directory, (str(path), path.name, self._search_text(path),
                                  info.st_size, info.st_mtime, generation)))
                    if len(self._scan_batch) >= 100:
                        self._flush_scan_batch(db)
                if self._scan_batch:
                    self._flush_scan_batch(db)
                # Do not delete prior rows if an inaccessible directory prevented
                # a complete observation. Pausing must never discard good rows.
                complete = finished and not (scan.inaccessible or scan.truncated)
                if complete:
                    db.execute("DELETE FROM files WHERE seen<?", (generation,))
                    db.execute("DELETE FROM directories WHERE seen<?", (generation,))
                db.execute("INSERT OR REPLACE INTO metadata VALUES('complete',?)", ('1' if complete else '0',))
            self._complete = complete
            self._needs_refresh = not finished
            if finished:
                self._directory_stamps = scan.directory_stamps
                scan.close()
                self._scan = None
                self.last_refresh = time.monotonic()
        except (OSError, sqlite3.Error):
            self._complete = False
            self._needs_refresh = True
            logger.debug("File index scan deferred", exc_info=True)
        finally:
            self._ready.set()
            self._lock.release()

    def notify(self, kind, path, destination="", *, is_directory=False):
        if self._stop.is_set() or not index_enabled():
            return
        if is_directory and kind == "modified":
            return
        try:
            self._events.put_nowait((kind,path,destination,is_directory))
        except queue.Full:
            self._needs_refresh = True

    def _apply_events(self, events):
        self._event_revision += 1
        with _database(self.path) as db:
            now = time.time()
            for kind, raw, destination, is_directory in events:
                path = Path(raw).resolve(strict=False)
                if not any(path.is_relative_to(root) for root in self.roots):
                    continue
                if kind in {"deleted","moved"}:
                    db.execute("DELETE FROM files WHERE path=?", (str(path),))
                    db.execute("DELETE FROM directories WHERE path=?", (str(path),))
                    if is_directory:
                        prefix = str(path)+os.sep
                        db.execute("DELETE FROM files WHERE substr(path,1,?)=?", (len(prefix),prefix))
                        db.execute("DELETE FROM directories WHERE substr(path,1,?)=?", (len(prefix),prefix))
                if is_directory:
                    self._needs_refresh = True
                    continue
                target = Path(destination).resolve(strict=False) if destination else path
                if kind != "deleted" and any(target.is_relative_to(root) for root in self.roots):
                    entry = self._entry(target,now)
                    if entry:
                        db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?)",entry)

    def _event_loop(self):
        while not self._stop.is_set():
            try:
                first = self._events.get(timeout=1.0)
            except queue.Empty:
                continue
            batch = [first]
            if self._stop.is_set() or not index_enabled():
                break
            for _ in range(127):
                try:
                    batch.append(self._events.get_nowait())
                except queue.Empty:
                    break
            try:
                self._apply_events(list(dict.fromkeys(batch)))
            except (OSError,sqlite3.Error):
                self._needs_refresh = True
                logger.debug("File index event update deferred",exc_info=True)

    def _start_watcher(self):
        if self._observer is not None or self._stop.is_set():
            return
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
            owner = self
            class Handler(FileSystemEventHandler):
                def on_any_event(self,event):
                    if event.event_type in {"created","modified","moved","deleted"}:
                        owner.notify(event.event_type,event.src_path,getattr(event,"dest_path",""),is_directory=event.is_directory)
            observer = Observer(timeout=1.0)
            for root in self.roots:
                if root.is_dir():
                    observer.schedule(Handler(),str(root),recursive=True)
            observer.start()
            self._observer = observer
            self._event_thread = threading.Thread(target=self._event_loop,name="NEO-file-index-events",daemon=True)
            self._event_thread.start()
        except (ImportError,OSError,RuntimeError):
            logger.debug("Metadata watcher unavailable; bounded live search remains active",exc_info=True)

    def request_refresh(self,max_seconds=120):
        if self._stop.is_set() or not index_enabled():
            return
        with self._schedule_lock:
            if self._thread and self._thread.is_alive():
                return
            if not self._needs_refresh and time.monotonic()-self.last_refresh<300:
                return
            self._ready.clear()
            def scan_and_watch():
                self.refresh(max_seconds)
                self._start_watcher()
            self._thread = threading.Thread(target=scan_and_watch,name="NEO-file-index",daemon=True)
            self._thread.start()

    def search(self, words, *, suffix="", limit=1000, require_all=False,
               max_seconds=3.0, include_dirs=False):
        """Read bounded rowid pages, retaining progress when a deadline expires.

        A LIKE query with ORDER BY used to restart a complete SQLite scan on
        every slice; rare/old matches on large indexes could never be returned.
        No transaction/cursor is retained between calls or across threads.
        """
        from jarvis.runtime.file_scan162 import ScanPacer
        from jarvis.runtime.cancellation import cancellation_manager
        cancel = cancellation_manager().raise_if_cancelled
        deadline = time.monotonic() + max(0., float(max_seconds))
        words = tuple(str(w).casefold().replace("ё", "е") for w in words[:32])
        limit = min(5000, max(1, limit))
        key = (words, suffix.casefold(), limit, require_all, include_dirs,
               self._scan_generation, self._event_revision)
        self.query_incomplete = True
        while not self._query_lock.acquire(timeout=.005):
            cancel()
            if time.monotonic() >= deadline:
                return []
        try:
            cancel()
            state = self._queries.get(key)
            if state is None:
                state = {'table': 0, 'cursor': 0, 'rows': {}}
                self._queries[key] = state
            self._queries.move_to_end(key)
            while len(self._queries) > 4:
                self._queries.popitem(last=False)
            tables = ('files', 'directories') if include_dirs else ('files',)
            pace = ScanPacer(cancelled=cancel)
            with _database(self.path, timeout=min(.1, max(.001, float(max_seconds)))) as db:
                db.row_factory = sqlite3.Row
                while state['table'] < len(tables) and time.monotonic() < deadline:
                    cancel()
                    table = tables[state['table']]
                    # rowid is indexed; this fetch cannot scan/sort the whole DB.
                    page = db.execute('SELECT rowid AS cursor,path,name,search,size,mtime FROM '
                                      + table + ' WHERE rowid>? ORDER BY rowid LIMIT 128',
                                      (state['cursor'],)).fetchall()
                    if not page:
                        state['table'] += 1
                        state['cursor'] = 0
                        continue
                    for row in page:
                        cancel()
                        pace.checkpoint()
                        if time.monotonic() >= deadline:
                            break
                        matches = [word in row['search'] for word in words]
                        if ((not words or (all(matches) if require_all else any(matches)))
                                and (not suffix or row['name'].casefold().endswith(suffix.casefold()))):
                            state['rows'][row['path']] = {k: row[k] for k in ('path','name','size','mtime')}
                            state['rows'][row['path']]['kind'] = 'folder' if table == 'directories' else 'file'
                        state['cursor'] = row['cursor']
                    # Bound retained matches even for a request for every file.
                    if len(state['rows']) > limit * 2:
                        best = sorted(state['rows'].values(), key=lambda r: (-r['mtime'], r['path']))[:limit]
                        state['rows'] = {r['path']: r for r in best}
            self.query_incomplete = state['table'] < len(tables)
            result = sorted(state['rows'].values(), key=lambda r: (-r['mtime'], r['path']))[:limit]
            if not self.query_incomplete:
                self._queries.pop(key, None)
            return result
        finally:
            self._query_lock.release()

    def close(self):
        self._stop.set()
        # First finish the scanning thread, which may be about to attach a watcher.
        # SQLite calls themselves have a bounded 3s timeout; allow them to unwind.
        for thread in (self._thread,):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=5)
        if self._scan is not None and not (self._thread and self._thread.is_alive()):
            self._scan.close()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        for thread in (self._event_thread,):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=5)
        if any(thread and thread.is_alive() for thread in (self._thread, self._event_thread)):
            logger.warning("File index shutdown is still pending; do not remove its database yet")


_indexes = OrderedDict()
_indexes_lock = threading.Lock()


def close_indexes():
    with _indexes_lock:
        values = list(_indexes.values())
        _indexes.clear()
    for index in values:
        index.close()


def get_index(roots):
    roots = list(dict.fromkeys(p.resolve(strict=False) for p in roots))
    identity = json.dumps(["scope-relative-directories-v162", *[str(p) for p in roots]],ensure_ascii=False)
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    evicted = None
    with _indexes_lock:
        if key not in _indexes:
            data = Path(os.getenv("LOCALAPPDATA",str(Path.home()/".local"/"share")))/"NEO"/"cache"
            _indexes[key] = FileIndex(data/("files-"+key+".sqlite3"),roots)
            if len(_indexes)>4:
                _,evicted = _indexes.popitem(last=False)
        _indexes.move_to_end(key)
        index = _indexes[key]
    if evicted:
        evicted.close()
    return index
