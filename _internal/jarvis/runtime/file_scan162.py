"""Streaming, cooperative directory traversal for user-requested local searches.

No idle scanner lives here. Traversal can be resumed after a bounded slice and
never materialises a whole directory before yielding to cancellation/pacing.
"""
from __future__ import annotations

from collections import deque
import os
from pathlib import Path
import threading
import time

_SKIP = {'windows', '$recycle.bin', 'system volume information', 'winsxs',
         'node_modules', 'venv', '.venv', '__pycache__', '.git', '.hg', '.svn',
         'cache', 'caches', 'gpucache', 'code cache'}


def relative_parent(path, roots):
    """Scope labels/user names must not accidentally match every file in scope."""
    for root in roots:
        try:
            return str(path.parent.relative_to(root))
        except ValueError:
            continue
    return str(path.parent)


class ScanPacer:
    """Limit sustained Python scan work; this is not an OS CPU usage guarantee."""
    def __init__(self, *, stop=None, cancelled=None, quantum=.004, duty=.25):
        self.stop = stop or threading.Event()
        self.cancelled = cancelled
        self.quantum = quantum
        self.ratio = (1. - duty) / duty
        self.clock = getattr(time, 'thread_time', time.process_time)
        self.since = self.clock()
        self.sleeps = 0

    def checkpoint(self, *, force=False):
        if self.cancelled:
            self.cancelled()
        used = self.clock() - self.since
        if used >= self.quantum or (force and used > 0):
            # Each wait remains short so Stop and app shutdown remain responsive.
            remaining = min(.04, used * self.ratio)
            while remaining > 0:
                pause = min(.012, remaining)
                if self.stop.wait(pause):
                    break
                if self.cancelled:
                    self.cancelled()
                remaining -= pause
            self.sleeps += 1
            self.since = self.clock()
        return not self.stop.is_set()


class MetadataWalker:
    def __init__(self, roots, *, recursive=True, max_pending=20000):
        self.pending = deque((Path(root), 0) for root in roots)
        self.seen = set()
        self.directory_stamps = {}
        self.recursive = recursive
        self.max_pending = max_pending
        self.entries = None
        self.depth = 0
        self.checked = 0
        self.inaccessible = 0
        self.excluded = 0
        self.complete = False
        self.truncated = False

    def close(self):
        if self.entries is not None:
            self.entries.close()
            self.entries = None

    def next(self):
        """Return one filesystem entry or None; inspect at most one per call."""
        from jarvis.runtime.location_scan158 import is_link
        if self.complete:
            raise StopIteration
        if self.entries is None:
            if not self.pending:
                self.complete = True
                raise StopIteration
            folder, self.depth = self.pending.popleft()
            key = os.path.normcase(str(folder))
            if key in self.seen:
                return None
            self.seen.add(key)
            if is_link(folder):
                self.excluded += 1
                return None
            try:
                info = folder.stat(follow_symlinks=False)
                self.directory_stamps[str(folder)] = (info.st_mtime_ns, info.st_ctime_ns)
                self.entries = os.scandir(folder)
            except OSError:
                self.inaccessible += 1
            return None
        try:
            entry = next(self.entries)
        except StopIteration:
            self.close()
            return None
        except OSError:
            self.inaccessible += 1
            self.close()
            return None
        self.checked += 1
        path = Path(entry.path)
        try:
            info = entry.stat(follow_symlinks=False)
            if is_link(path, entry):
                self.excluded += 1
                return None
            directory = entry.is_dir(follow_symlinks=False)
            if directory:
                if entry.name.casefold() in _SKIP or entry.name.startswith('.') or self.depth >= 32:
                    self.excluded += 1
                    return None
                if self.recursive:
                    if len(self.pending) < self.max_pending:
                        self.pending.append((path, self.depth + 1))
                    else:
                        self.truncated = True
                return path, info, True
            if not entry.is_file(follow_symlinks=False):
                return None
            return path, info, False
        except OSError:
            self.inaccessible += 1
            return None
