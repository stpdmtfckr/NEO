"""NEO 173: what «это», «туда», «тот файл» mean — from what just happened on this computer.

Nothing is recorded to disk and nothing is watched in the background. At the moment a
command needs it, NEO looks at:

1. things NEO itself just did (opened or created a folder, downloaded or unpacked a file) —
   kept in memory for ``context.minutes`` (10 by default);
2. the File Explorer window the user is working in: its folder («туда») and its selection
   («это»);
3. the newest file in Downloads (for «скачанный файл») and the Windows «Recent» list of the
   user's last opened files (for «тот файл»), no older than ``context.minutes``.

When nothing fits, NEO asks instead of guessing.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable

_LOCK = threading.Lock()
_EVENTS: list[tuple[float, str, str]] = []        # (monotonic time, kind: folder|file, path)
LIMIT = 30


def _minutes(config: Any) -> float:
    try:
        return max(1.0, min(120.0, float(config.get('context.minutes', 10) or 10)))
    except (TypeError, ValueError):
        return 10.0


def enabled(config: Any) -> bool:
    return bool(config.get('context.enabled', True))


def note(kind: str, path: str | os.PathLike) -> None:
    """NEO did something with this folder/file (kept in memory only)."""
    if kind not in {'folder', 'file'} or not str(path):
        return
    with _LOCK:
        _EVENTS.append((time.monotonic(), kind, str(path)))
        del _EVENTS[:-LIMIT]


def forget() -> None:
    with _LOCK:
        _EVENTS.clear()


def recent(kind: str, config: Any, now: float | None = None) -> str:
    now = time.monotonic() if now is None else now
    limit = _minutes(config) * 60
    with _LOCK:
        rows = list(_EVENTS)
    for at, k, path in reversed(rows):
        if k == kind and now - at <= limit and Path(path).exists():
            return path
    return ''


# ---- File Explorer (Windows) -------------------------------------------------------------------

def explorer_state() -> tuple[str, list[str]]:
    """(folder, selected items) of the foremost File Explorer window — asked from Explorer itself."""
    if os.name != 'nt':
        return '', []
    try:
        import pythoncom  # noqa: F401  (COM for this thread)
        import win32com.client
        import win32gui
        pythoncom.CoInitialize()
        shell = win32com.client.Dispatch('Shell.Application')
        order: dict[int, int] = {}
        hwnd = win32gui.GetTopWindow(0)
        index = 0
        while hwnd:
            order[int(hwnd)] = index
            index += 1
            hwnd = win32gui.GetWindow(hwnd, 2)          # GW_HWNDNEXT: z-order, front first
        best = None
        for window in shell.Windows():
            try:
                if not str(window.FullName).casefold().endswith('explorer.exe'):
                    continue
                rank = order.get(int(window.HWND), 10**9)
                if best is None or rank < best[0]:
                    best = (rank, window)
            except Exception:
                continue
        if best is None:
            return '', []
        document = best[1].Document
        folder = str(document.Folder.Self.Path)
        selected = [str(item.Path) for item in document.SelectedItems()]
        return folder, selected
    except Exception:
        return '', []


def recent_files(config: Any, *, now: float | None = None) -> list[str]:
    """The user's last opened files from the Windows «Recent» list (shortcuts resolved), newest first."""
    if os.name != 'nt':
        return []
    folder = Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Recent'
    if not folder.is_dir():
        return []
    now = time.time() if now is None else now
    limit = _minutes(config) * 60
    try:
        links = sorted((p for p in folder.glob('*.lnk')), key=lambda p: p.stat().st_mtime, reverse=True)[:15]
    except OSError:
        return []
    out = []
    try:
        import win32com.client
        shell = win32com.client.Dispatch('WScript.Shell')
    except Exception:
        return []
    for link in links:
        try:
            if now - link.stat().st_mtime > limit:
                break
            target = str(shell.CreateShortcut(str(link)).TargetPath)
            if target and Path(target).is_file():
                out.append(target)
        except Exception:
            continue
    return out


def newest_download(config: Any, downloads: Path | None = None, *, now: float | None = None) -> str:
    from jarvis.runtime import tidy172
    folder = Path(downloads) if downloads else tidy172.known_folder('downloads')
    now = time.time() if now is None else now
    limit = _minutes(config) * 60
    try:
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix.casefold() not in {'.crdownload', '.part', '.partial', '.tmp'}
                 and not p.name.startswith(('.', '~$')) and p.name.casefold() != 'desktop.ini']
    except OSError:
        return ''
    if not files:
        return ''
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return str(newest) if now - newest.stat().st_mtime <= limit else ''


# ---- words ----------------------------------------------------------------------------------------

_THIS_FILE = r'(?:это|этот\s+файл|эту\s+папку|его|ее|её|их|выделенн\w*|this(?:\s+file)?|it|that\s+file|das|diese\s+datei|es|[cç]a|ce\s+fichier|le|esto|este\s+archivo|lo|questo(?:\s+file)?|lo|isto|este\s+ficheiro|o)'
_THAT_FILE = r'(?<![\w])(?:тот\s+файл|последний\s+файл|файл,?\s+который\s+я\s+открывал|that\s+file|the\s+last\s+file|die\s+letzte\s+datei|le\s+dernier\s+fichier|el\s+[uú]ltimo\s+archivo|l[\'’]ultimo\s+file|o\s+[uú]ltimo\s+ficheiro)'
_DOWNLOADED = r'(?<![\w])(?:скачанн\w+\s+файл|то,?\s+что\s+(?:я\s+)?скачал\w*|последн\w+\s+загрузк\w*|the\s+downloaded\s+file|what\s+i\s+downloaded|die\s+heruntergeladene\s+datei|le\s+fichier\s+t[eé]l[eé]charg[eé]|el\s+archivo\s+descargado|il\s+file\s+scaricato|o\s+ficheiro\s+transferido)'
_THERE = r'(?:туда|в\s+(?:эту|ту|ту\s+же)\s+папку|сюда|there|here|into\s+(?:this|that)\s+folder|dorthin|hierher|in\s+diesen\s+ordner|l[aà]-bas|ici|dans\s+ce\s+dossier|all[ií]|aqu[ií]|en\s+esta\s+carpeta|l[iì]|qui|in\s+questa\s+cartella|a[ií]|aqui|nesta\s+pasta)'


@dataclass(slots=True)
class Found:
    path: str
    source: str        # explorer_selection | explorer_folder | neo | downloads | recent


def resolve_file(which: str, config: Any, *, explorer: Callable[[], tuple[str, list[str]]] = explorer_state,
                 downloads: Callable[[Any], str] = newest_download, recent_list: Callable[[Any], list[str]] = recent_files) -> Found | None:
    """``which``: this | that | downloaded."""
    if not enabled(config):
        return None
    if which == 'downloaded':
        path = recent('file', config) or downloads(config)
        return Found(path, 'neo' if path == recent('file', config) and path else 'downloads') if path else None
    if which == 'this':
        _folder, selected = explorer()
        if len(selected) == 1:
            return Found(selected[0], 'explorer_selection')
        path = recent('file', config)
        return Found(path, 'neo') if path else None
    path = recent('file', config)
    if path:
        return Found(path, 'neo')
    rows = recent_list(config)
    return Found(rows[0], 'recent') if rows else None


def resolve_folder(config: Any, *, explorer: Callable[[], tuple[str, list[str]]] = explorer_state) -> Found | None:
    if not enabled(config):
        return None
    path = recent('folder', config)
    folder, _selected = explorer()
    if folder and Path(folder).is_dir():
        return Found(folder, 'explorer_folder')
    return Found(path, 'neo') if path else None


def which_file(text: str) -> str:
    low = str(text or '').casefold().replace('ё', 'е')
    if re.search(_DOWNLOADED, low):
        return 'downloaded'
    if re.search(_THAT_FILE, low):
        return 'that'
    return 'this'


__all__ = ['Found', 'enabled', 'explorer_state', 'forget', 'newest_download', 'note', 'recent', 'recent_files',
           'resolve_file', 'resolve_folder', 'which_file']
