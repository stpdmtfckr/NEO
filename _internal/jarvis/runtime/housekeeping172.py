"""NEO 172: keep NEO's own disk use bounded — once a day, in the background.

* ``cache``: files not used for ``cache_days`` days are removed, then the oldest ones until
  the folder is under ``cache_max_mb``.
* ``backups``: of the settings copies made before a settings migration (``settings-before-*.json``)
  the newest ``keep_backups`` stay. Backups of the user's files (for undoing a delete) are never touched.
* temporary screenshots NEO made for «что за ошибка?» (``neo_help_*.png``) that survived a crash.

Never touched: logs (they rotate themselves), the user's screenshots, notes, memory,
settings, models.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
DAY = 86400.0


def _files(folder: Path) -> list[tuple[float, int, Path]]:
    rows = []
    for root, _dirs, names in os.walk(folder):
        for name in names:
            path = Path(root) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append((max(stat.st_atime, stat.st_mtime), stat.st_size, path))
    return rows


def clean_cache(folder: Path, *, days: float = 14, max_mb: int = 500, now: float | None = None) -> tuple[int, int]:
    """(files removed, bytes freed)."""
    folder = Path(folder)
    if not folder.is_dir():
        return 0, 0
    now = time.time() if now is None else now
    removed = freed = 0
    rows = sorted(_files(folder))
    keep = []
    for used, size, path in rows:
        if now - used > days * DAY:
            try:
                path.unlink()
                removed, freed = removed + 1, freed + size
                continue
            except OSError:
                pass
        keep.append((used, size, path))
    total = sum(size for _u, size, _p in keep)
    limit = max(50, int(max_mb)) * 1024 * 1024
    for used, size, path in keep:           # oldest first
        if total <= limit:
            break
        try:
            path.unlink()
            removed, freed, total = removed + 1, freed + size, total - size
        except OSError:
            continue
    for root, dirs, _names in os.walk(folder, topdown=False):
        for name in dirs:
            try:
                (Path(root) / name).rmdir()
            except OSError:
                pass
    return removed, freed


def clean_backups(folder: Path, *, keep: int = 10) -> int:
    folder = Path(folder)
    if not folder.is_dir():
        return 0
    files = sorted(folder.glob('settings-before-*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for path in files[max(1, keep):]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def clean_temp(now: float | None = None) -> int:
    now = time.time() if now is None else now
    removed = 0
    for path in Path(tempfile.gettempdir()).glob('neo_help_*.png'):
        try:
            if now - path.stat().st_mtime > 3600:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def run(config: Any, *, force: bool = False) -> dict[str, int] | None:
    paths = getattr(config, 'paths', None)
    root = getattr(paths, 'root', None)
    if root is None:
        return None
    stamp = Path(root) / 'housekeeping.stamp'
    try:
        if not force and stamp.exists() and time.time() - stamp.stat().st_mtime < DAY:
            return None
    except OSError:
        pass
    result = {'cache_files': 0, 'cache_bytes': 0, 'backups': 0, 'temp': 0}
    cache = getattr(paths, 'cache', None)
    if cache:
        result['cache_files'], result['cache_bytes'] = clean_cache(
            Path(cache), days=float(config.get('storage.cache_days', 14) or 14), max_mb=int(config.get('storage.cache_max_mb', 500) or 500))
    backups = getattr(paths, 'backups', None)
    if backups:
        result['backups'] = clean_backups(Path(backups), keep=int(config.get('storage.keep_backups', 10) or 10))
    result['temp'] = clean_temp()
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(time.strftime('%Y-%m-%d %H:%M:%S'), encoding='utf-8')
    except OSError:
        pass
    logger.info('Housekeeping: %s', result)
    return result


def start_background(config: Any) -> threading.Thread:
    """Run once, 2 minutes after start, in a low-priority background thread (never on the UI thread)."""
    def work() -> None:
        time.sleep(120)
        try:
            run(config)
        except Exception:
            logger.debug('Housekeeping failed', exc_info=True)
    thread = threading.Thread(target=work, name='NeoJarvis-Housekeeping', daemon=True)
    thread.start()
    return thread


__all__ = ['clean_backups', 'clean_cache', 'clean_temp', 'run', 'start_background']
