# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Foreground desktop context helpers.

The assistant must never guess which document the user means when a destructive or
mutating command contains words like "сюда", "этот файл" or "в него".  This module
builds a small, read-only snapshot of the foreground Windows application and tries
to identify an actually open text document with explicit evidence.
"""

import ctypes
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import psutil


_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".csv", ".tsv", ".xml", ".yaml", ".yml",
    ".ini", ".cfg", ".conf", ".toml", ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".html",
    ".htm", ".css", ".scss", ".less", ".sql", ".bat", ".cmd", ".ps1", ".sh", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".java", ".kt", ".go", ".rs", ".lua", ".log", ".rtf",
}

_EDITOR_PROCESSES = {
    "notepad.exe", "notepad++.exe", "code.exe", "codium.exe", "cursor.exe", "pycharm64.exe",
    "idea64.exe", "sublime_text.exe", "atom.exe", "wordpad.exe", "devenv.exe", "windsurf.exe",
}


def _norm(value: str) -> str:
    return re.sub(r"[^a-zа-яё0-9._+-]+", " ", str(value or "").casefold()).strip()


def _looks_text_file(path: Path) -> bool:
    suffix = path.suffix.casefold()
    return suffix in _TEXT_EXTENSIONS or (not suffix and path.is_file())


@dataclass(frozen=True)
class ForegroundContext:
    handle: int = 0
    pid: int = 0
    process_name: str = ""
    title: str = ""
    file_path: str = ""
    folder_path: str = ""
    file_confidence: float = 0.0
    file_source: str = ""
    editor_like: bool = False
    process_exe: str = ""
    observed_at: float = 0.0
    files_pending: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


class _OpenFileSnapshots:
    """One bounded worker for slow Windows handle inspection.

    psutil documents that open_files can stall on Windows handles. Never call it
    on the UI/command thread and never spawn another worker if it is stuck.
    A stale handle snapshot is not evidence for selecting a document.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = OrderedDict()
        self._busy = None

    def read(self, proc, *, wait=.075):
        try:
            key = (int(proc.pid), float(proc.create_time()))
        except (psutil.Error, OSError):
            return [], '', False
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] <= 1.5:
                return list(cached[1]), cached[2], False
            event = threading.Event()
            if self._busy is None:
                self._busy = (key, event)
                def collect():
                    opened, executable = [], ''
                    try:
                        executable = str(proc.exe() or '')
                        opened = list(proc.open_files())[:256]
                    except (psutil.Error, OSError):
                        pass
                    finally:
                        with self._lock:
                            self._cache[key] = (time.monotonic(), opened, executable)
                            self._cache.move_to_end(key)
                            while len(self._cache) > 8:
                                self._cache.popitem(last=False)
                            self._busy = None
                            event.set()
                threading.Thread(target=collect, name='NEO-foreground-files', daemon=True).start()
            elif self._busy[0] == key:
                event = self._busy[1]
            else:
                return [], '', True
        event.wait(max(0., min(.1, wait)))
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] <= 1.5:
                return list(cached[1]), cached[2], False
        return [], '', True


_open_file_snapshots = _OpenFileSnapshots()


def _foreground_window():
    if os.name != 'nt':
        return 0, 0, '', '', None
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    handle = int(user32.GetForegroundWindow() or 0)
    if not handle:
        return 0, 0, '', '', None
    pid_value = wintypes.DWORD()
    user32.GetWindowThreadProcessId(handle, ctypes.byref(pid_value))
    pid = int(pid_value.value)
    proc = psutil.Process(pid)
    # The top-level caption is available directly. UI Automation can block on
    # an unresponsive application and is unnecessary for this small snapshot.
    length = min(4096, int(user32.GetWindowTextLengthW(handle)))
    buf = ctypes.create_unicode_buffer(max(1, length + 1))
    user32.GetWindowTextW(handle, buf, len(buf))
    return handle, pid, str(proc.name() or ''), str(buf.value or '').strip(), proc


def inspect_foreground_context(recent_paths: Iterable[str] = ()) -> ForegroundContext:
    """Return a conservative snapshot of the Windows foreground application.

    A file is returned only when there is strong evidence: the process has the
    exact path open, or the foreground title names one of JARVIS' recent exact
    paths.  Ambiguous candidates fail closed.
    """

    handle = 0
    pid = 0
    process_name = ""
    title = ""
    try:
        handle, pid, process_name, title, proc = _foreground_window()
        if proc is None:
            return ForegroundContext()
    except Exception:
        return ForegroundContext(handle=handle, pid=pid, process_name=process_name, title=title)

    title_n = _norm(title)
    proc_n = process_name.casefold()
    editor_like = proc_n in _EDITOR_PROCESSES or any(
        token in title_n for token in ("visual studio code", "notepad", "блокнот", "pycharm", "sublime", "cursor")
    )

    candidates: list[tuple[float, Path, str]] = []
    opened, process_exe, pending = _open_file_snapshots.read(proc)
    observed_at = time.time()

    for item in opened:
        try:
            if str(item.path).startswith(('\\\\', '//')):
                continue
            path = Path(str(item.path)).expanduser().resolve(strict=False)
            if not path.is_file() or not _looks_text_file(path):
                continue
            name_n = _norm(path.name)
            stem_n = _norm(path.stem)
            score = 0.60
            source = "process_open_file"
            if name_n and name_n in title_n:
                score = 1.0
                source = "process_open_file+window_title"
            elif stem_n and len(stem_n) >= 3 and stem_n in title_n:
                score = 0.94
                source = "process_open_file+window_title_stem"
            candidates.append((score, path, source))
        except (OSError, ValueError):
            continue

    # GUI editors such as VS Code do not always keep the visible file as a normal
    # Win32 open handle.  A recent exact path whose basename is in the *foreground*
    # editor title is still strong evidence, and much safer than a global fuzzy search.
    seen_recent: set[str] = set()
    for raw in recent_paths:
        raw = str(raw or "").strip()
        if not raw or raw in seen_recent:
            continue
        if raw.startswith(('\\\\', '//')):
            continue
        seen_recent.add(raw)
        try:
            path = Path(raw).expanduser().resolve(strict=False)
            if not path.is_file() or not _looks_text_file(path):
                continue
            name_n = _norm(path.name)
            stem_n = _norm(path.stem)
            if name_n and name_n in title_n:
                candidates.append((0.98, path, "recent_exact_path+window_title"))
            elif editor_like and stem_n and len(stem_n) >= 4 and stem_n in title_n:
                candidates.append((0.90, path, "recent_exact_path+window_title_stem"))
        except (OSError, ValueError):
            continue

    # Collapse duplicate paths while retaining strongest evidence.
    best_by_path: dict[str, tuple[float, Path, str]] = {}
    for score, path, source in candidates:
        key = str(path).casefold()
        old = best_by_path.get(key)
        if old is None or score > old[0]:
            best_by_path[key] = (score, path, source)
    ranked = sorted(best_by_path.values(), key=lambda x: x[0], reverse=True)

    selected: tuple[float, Path, str] | None = None
    if ranked:
        # If two different paths are equally plausible, do not guess.
        if len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.08:
            if ranked[0][0] >= 0.88:
                selected = ranked[0]
        elif ranked[0][0] >= 0.99 and ranked[1][0] < 0.99:
            selected = ranked[0]

    if selected is None:
        return ForegroundContext(
            handle=handle,
            pid=pid,
            process_name=process_name,
            title=title,
            editor_like=editor_like,
            process_exe=process_exe,
            observed_at=observed_at,
            files_pending=pending,
        )

    score, path, source = selected
    return ForegroundContext(
        handle=handle,
        pid=pid,
        process_name=process_name,
        title=title,
        file_path=str(path),
        folder_path=str(path.parent),
        file_confidence=round(float(score), 3),
        file_source=source,
        editor_like=editor_like,
        process_exe=process_exe,
        observed_at=observed_at,
        files_pending=pending,
    )
