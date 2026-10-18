"""On-demand, resumable metadata-only location search on local disks.

One daemon performs all broad scans, never the UI thread. A command requests one
bounded slice; repeated requests continue the same traversal. There is no idle
whole-disk indexing, no network volume traversal and no document ingestion.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import os
from pathlib import Path
import queue
import stat
import threading
import time

from jarvis.runtime.cancellation import cancellation_manager, OperationCancelled

_SKIP = {'windows', '$recycle.bin', 'system volume information', 'node_modules',
         '__pycache__', '.git', 'venv', '.venv', 'winsxs'}
_UTILITY = {'uninstall', 'unins', 'crash', 'launcher', 'reporter', 'redist',
            'cheatengine', 'trainer', 'benchmark', 'modmanager', 'setup',
            'anticheat', 'installer', 'updater'}
_EXECUTABLE_SUFFIXES = {'x64', 'x86', 'x8664', 'i386', 'amd64', 'arm64', 'win64', 'win32', 'dx11', 'dx12',
                        'win64shipping', 'win32shipping'}
_KINDS = {'saves': {'saves', 'save', 'savegames', 'savedgames'},
          'config': {'config', 'configs', 'settings'},
          'videos': {'videos', 'video', 'recordings', 'captures'},
          'screenshots': {'screenshots', 'screenshot'}, 'logs': {'logs', 'log'},
          'mods': {'mods', 'mod'}}


def norm(value):
    return ''.join(c for c in str(value).casefold().replace('ё', 'е') if c.isalnum())


def is_link(path, entry=None):
    """Reject all Windows reparse points too (junctions on Python < 3.12)."""
    try:
        info = entry.stat(follow_symlinks=False) if entry else path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
    except OSError:
        return True


def local_drives():
    if os.name != 'nt':
        return []
    import ctypes
    kernel = ctypes.windll.kernel32
    return [Path(letter + ':\\') for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
            if kernel.GetDriveTypeW(letter + ':\\') == 3]


def scan_roots(config, priority=()):
    raw = config.get('gaming.locations_scan_roots', None)
    roots = raw if raw is not None else [*priority, *local_drives()]
    if isinstance(roots, (str, Path)):
        roots = [roots]
    result = []
    for value in roots if isinstance(roots, (list, tuple)) else []:
        if not isinstance(value, (str, Path)) or str(value).startswith(('\\\\', '//')):
            continue
        path = Path(os.path.expandvars(str(value))).expanduser()
        if path.is_absolute() and path.is_dir() and not is_link(path) and path not in result:
            result.append(path)
    return result[:64]


@dataclass
class Scan:
    roots: tuple
    terms: frozenset
    pending: deque = field(default_factory=deque)
    seen: set = field(default_factory=set)
    rows: dict = field(default_factory=dict)
    checked: int = 0
    inaccessible: int = 0
    excluded: int = 0
    complete: bool = False
    touched: float = field(default_factory=time.monotonic)
    iterator: object = None
    busy: bool = False
    ready: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        self.pending.extend((Path(root), str(root) if self.matches(Path(root).name) else '', 0)
                            for root in self.roots)

    def matches(self, value):
        name = norm(value)
        return not any(token in name and not any(token in term for term in self.terms)
                       for token in _UTILITY) and any(
            name == term or (len(term) >= 5 and term in name) for term in self.terms)

    def executable_matches(self, value):
        # A game's directory also contains GPU probes, patchers, shader builders
        # and launchers. Directory ownership alone cannot identify its game exe.
        # Generic names explicitly declared by a launcher manifest are handled
        # separately by game_locations with stronger, verified evidence.
        name = norm(value)
        return self.matches(value) and any(
            name == term or (name.startswith(term) and name[len(term):] in _EXECUTABLE_SUFFIXES)
            for term in self.terms)

    def traversal(self):
        from jarvis.runtime.file_scan162 import ScanPacer
        # Cancellation is raised by advance(), outside this generator. Raising
        # inside would close its saved cursor and falsely finish a resumed scan.
        # Each entry yields; cooperative waits are bounded to at most 40 ms.
        pace = ScanPacer()
        while self.pending:
            pace.checkpoint()
            folder, owner, depth = self.pending.popleft()
            key = os.path.normcase(str(folder))
            if key in self.seen or is_link(folder):
                yield None, False, False, ''
                continue
            self.seen.add(key)
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        # Excluded/reparse entries also cost CPU. Yield control
                        # for each so a slice/cancel cannot hide inside next().
                        pace.checkpoint()
                        path = Path(entry.path)
                        if is_link(path, entry):
                            yield None, False, False, ''
                            continue
                        try:
                            directory = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            self.inaccessible += 1
                            yield None, False, False, ''
                            continue
                        match = self.matches(path.stem if not directory else path.name)
                        location_owner = str(path) if match and directory else owner
                        if directory:
                            if path.name.casefold() in _SKIP or depth >= 32:
                                self.excluded += 1
                                yield None, False, False, ''
                                continue
                            self.pending.append((path, location_owner, depth + 1))
                        yield path, directory, match, location_owner
            except OSError:
                self.inaccessible += 1
            # A tree containing only empty/unreadable directories is bounded
            # too, without charging another entry for every normal directory.
            if time.monotonic() >= getattr(self, '_slice_deadline', float('inf')):
                yield None, False, False, ''
        self.complete = True

    def advance(self, *, seconds=1.5, entries=12000, generation=None):
        manager = cancellation_manager()
        if generation is not None:
            manager.bind(generation)
        if self.iterator is None:
            self.iterator = self.traversal()
        deadline = time.monotonic() + seconds
        self._slice_deadline = deadline
        for count in range(entries):
            manager.raise_if_cancelled()
            if time.monotonic() >= deadline:
                break
            try:
                path, directory, match, owner = next(self.iterator)
            except StopIteration:
                self.complete = True
                break
            if path is None:
                continue
            self.checked += 1
            kind = ''
            if directory:
                components = {norm(p) for p in path.parts[-4:]}
                kind = next((k for k, names in _KINDS.items()
                             if (owner or match) and components & names), '')
                if match and not kind:
                    kind = 'related'
            elif path.suffix.casefold() == '.exe' and self.executable_matches(path.stem):
                kind = 'exe'
            if kind and len(self.rows) < 500:
                self.rows[(kind, str(path))] = {'kind': kind, 'path': str(path),
                    'exists': True, 'source': 'local_directory_search',
                    'confidence': 'inferred', 'confirmed': False,
                    'evidence': 'existing_name_match' if match else 'existing_named_child',
                    'path_type': 'directory' if directory else 'file'}
        self.touched = time.monotonic()


def existing_rows(rows):
    """Validate each published path once, cooperatively, with one lstat call.

    A caller polling an active scan may defer this to final publication. A new
    command and the final result always validate again, so deleted paths never
    survive solely because they were present in a previous query's cache.
    """
    from jarvis.runtime.file_scan162 import ScanPacer
    pace = ScanPacer(cancelled=cancellation_manager().raise_if_cancelled)
    result = []
    for row in rows:
        pace.checkpoint()
        try:
            info = Path(row['path']).lstat()
        except OSError:
            continue
        if stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400):
            continue
        if row.get('path_type') == 'directory' and not stat.S_ISDIR(info.st_mode):
            continue
        if row.get('path_type') == 'file' and not stat.S_ISREG(info.st_mode):
            continue
        result.append(dict(row))
    return result


class LocationScanner:
    def __init__(self):
        self._lock = threading.Lock()
        self._scans = OrderedDict()
        self._tasks = queue.Queue(maxsize=8)
        self._thread = None

    def _run(self):
        while True:
            scan, generation = self._tasks.get()
            try:
                with scan.lock:
                    scan.advance(generation=generation)
            except (OperationCancelled, OSError):
                pass
            finally:
                with self._lock:
                    scan.busy = False
                    scan.ready.set()
                self._tasks.task_done()

    def search(self, roots, terms, *, wait=.45, validate=True):
        key = (tuple(str(p) for p in roots), tuple(sorted(terms)))
        with self._lock:
            scan = self._scans.get(key)
            if scan is None or (not scan.busy and time.monotonic() - scan.touched > 300):
                if scan is not None and scan.iterator:
                    scan.iterator.close()
                scan = self._scans[key] = Scan(key[0], frozenset(terms))
            self._scans.move_to_end(key)
            for old_key, old in list(self._scans.items()):
                if len(self._scans) <= 8:
                    break
                if not old.busy and old_key != key:
                    if old.iterator:
                        old.iterator.close()
                    del self._scans[old_key]
            if not scan.complete and not scan.busy:
                scan.busy = True
                scan.ready.clear()
                try:
                    self._tasks.put_nowait((scan, cancellation_manager().current_generation()))
                except queue.Full:
                    scan.busy = False
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, name='NEO-location-scan', daemon=True)
                    self._thread.start()
        scan.ready.wait(max(0., min(.75, wait)))
        # No waiting on the worker's lock: copy-on-read is safe under the GIL and
        # additions are whole dict entries. The command validates the first
        # snapshot and final publication; busy polling needs no repeated stat.
        rows = list(scan.rows.copy().values())
        rows = existing_rows(rows) if validate else [dict(row) for row in rows]
        return rows, {'incomplete': not scan.complete or bool(scan.inaccessible or scan.excluded) or len(scan.rows) >= 500,
                      'continuable': not scan.complete, 'searching': scan.busy,
                      'checked_entries': scan.checked, 'inaccessible': scan.inaccessible,
                      'excluded_directories': scan.excluded,
                      'roots': list(key[0]), 'result_limit_reached': len(scan.rows) >= 500}


_scanner = LocationScanner()


def search_locations(config, terms, *, priority=(), wait=.45, validate=True):
    cancellation_manager().raise_if_cancelled()
    roots = scan_roots(config, priority)
    return _scanner.search(roots, terms, wait=wait, validate=validate)
