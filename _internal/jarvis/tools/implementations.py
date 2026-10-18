# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import ctypes
import difflib
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
import pyperclip
from send2trash import send2trash

from jarvis.desktop_context import inspect_foreground_context
from jarvis.gaming import (
    GameManager, GameSnapshot, recent_game_launch, record_game_launch, update_game_launch,
)
from jarvis.gaming.history import GameSessionHistoryStore
from jarvis.gaming.telemetry import LiveGamePanelService
from jarvis.desktop_resolver import (
    WEBSITE_ALIASES,
    FOLDER_ALIASES,
    ResolvedTarget,
    application_candidate_is_exact,
    application_candidates,
    find_user_path,
    find_user_path_exact_candidates,
    find_user_path_in_root,
    normalize as normalize_target,
    resolve_basic_target,
    strip_target_words,
    target_similarity,
)
from jarvis.overlay_state import OverlayStore, publish_overlay_refresh
from jarvis.integrations.yandex_music import YandexMusicAdapter
from jarvis.integrations.spotify import SpotifyAdapter
from jarvis.integrations.youtube_api import YouTubeAPIAdapter
from jarvis.integrations.discord_navigation import shared_reliable_discord_adapter
from jarvis.security import Risk
from jarvis.runtime.undo import undo_manager
from jarvis.runtime.semantics import normalize_entity_phrase
from jarvis.tools.registry import ToolDefinition, ToolExecution


logger = logging.getLogger(__name__)
SELECTED_APP_CANDIDATE185: ContextVar[dict | None] = ContextVar("SELECTED_APP_CANDIDATE185", default=None)


def _capture_native_window_placement(hwnd: int) -> dict[str, Any] | None:
    """Capture exact Win32 show-state + restore rectangle for reversible actions.

    ``pygetwindow`` is useful for discovery, but its geometry/state properties can
    lag immediately after maximize/restore.  WINDOWPLACEMENT is the authoritative
    Win32 representation and, importantly, preserves the normal (restored)
    rectangle even while a window is maximized.
    """
    if os.name != "nt" or not hwnd:
        return None
    try:
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG),
            ]

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.UINT), ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT), ("ptMinPosition", POINT),
                ("ptMaxPosition", POINT), ("rcNormalPosition", RECT),
                ("rcDevice", RECT),
            ]

        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if not ctypes.windll.user32.GetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(wp)):
            return None
        return {
            "hwnd": int(hwnd), "showCmd": int(wp.showCmd), "flags": int(wp.flags),
            "min_x": int(wp.ptMinPosition.x), "min_y": int(wp.ptMinPosition.y),
            "max_x": int(wp.ptMaxPosition.x), "max_y": int(wp.ptMaxPosition.y),
            "normal_left": int(wp.rcNormalPosition.left), "normal_top": int(wp.rcNormalPosition.top),
            "normal_right": int(wp.rcNormalPosition.right), "normal_bottom": int(wp.rcNormalPosition.bottom),
            "device_left": int(wp.rcDevice.left), "device_top": int(wp.rcDevice.top),
            "device_right": int(wp.rcDevice.right), "device_bottom": int(wp.rcDevice.bottom),
        }
    except Exception:
        return None


def _apply_native_window_placement(hwnd: int, snap: dict[str, Any]) -> bool:
    """Restore a WINDOWPLACEMENT snapshot captured by the helper above."""
    if os.name != "nt" or not hwnd or not snap:
        return False
    try:
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG),
            ]

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.UINT), ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT), ("ptMinPosition", POINT),
                ("ptMaxPosition", POINT), ("rcNormalPosition", RECT),
                ("rcDevice", RECT),
            ]

        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        wp.flags = int(snap.get("flags", 0) or 0)
        wp.showCmd = int(snap.get("showCmd", 1) or 1)
        wp.ptMinPosition = POINT(int(snap.get("min_x", -1)), int(snap.get("min_y", -1)))
        wp.ptMaxPosition = POINT(int(snap.get("max_x", -1)), int(snap.get("max_y", -1)))
        wp.rcNormalPosition = RECT(
            int(snap.get("normal_left", 0)), int(snap.get("normal_top", 0)),
            int(snap.get("normal_right", 1000)), int(snap.get("normal_bottom", 700)),
        )
        wp.rcDevice = RECT(
            int(snap.get("device_left", 0)), int(snap.get("device_top", 0)),
            int(snap.get("device_right", 0)), int(snap.get("device_bottom", 0)),
        )
        return bool(ctypes.windll.user32.SetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(wp)))
    except Exception:
        return False


def _native_window_state(hwnd: int) -> tuple[bool, bool]:
    """Return (maximized, minimized) using User32 instead of cached wrappers."""
    if os.name != "nt" or not hwnd:
        return False, False
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        return bool(user32.IsZoomed(wintypes.HWND(hwnd))), bool(user32.IsIconic(wintypes.HWND(hwnd)))
    except Exception:
        return False, False


def _native_show_window(hwnd: int, action: str) -> bool:
    """Set a Win32 window state without keyboard shortcuts or mouse input."""
    if os.name != "nt" or not hwnd:
        return False
    # WinUser.h / ShowWindow nCmdShow values. Keep them named so this path is
    # auditable and cannot be confused with fullscreen keyboard shortcuts.
    SW_MAXIMIZE = 3
    SW_MINIMIZE = 6
    SW_RESTORE = 9
    command = {
        "maximize": SW_MAXIMIZE,
        "minimize": SW_MINIMIZE,
        "restore": SW_RESTORE,
    }.get(str(action or "").casefold())
    if command is None:
        return False
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        # ShowWindowAsync posts the state change to the owning GUI thread. This is
        # more reliable for Electron/Chromium windows than pygetwindow alone and
        # avoids blocking if the target thread is temporarily busy.
        started = bool(user32.ShowWindowAsync(wintypes.HWND(hwnd), int(command)))
        if not started:
            started = bool(user32.ShowWindow(wintypes.HWND(hwnd), int(command)))
        return started
    except Exception:
        return False


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _enum(values: list[str], description: str) -> dict[str, Any]:
    return {"type": "string", "enum": values, "description": description}


def _guid_from_string(value: str):
    """Create a Windows GUID struct without requiring pywin32."""
    import uuid
    u = uuid.UUID(value.strip('{}'))
    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]
    data4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])
    return GUID(u.time_low, u.time_mid, u.time_hi_version, data4)


def _windows_known_folder(guid_text: str) -> Path | None:
    """Resolve redirected Windows Known Folders via SHGetKnownFolderPath."""
    if os.name != "nt":
        return None
    try:
        guid = _guid_from_string(guid_text)
        out = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        # Microsoft documents SHGetKnownFolderPath as a COM-aware Shell API.
        # Initialise COM on this calling thread when possible; RPC_E_CHANGED_MODE
        # simply means another component already chose the apartment model, in
        # which case the existing apartment remains usable.
        init_hr = int(ole32.CoInitializeEx(None, 0x2))  # COINIT_APARTMENTTHREADED
        did_init = init_hr in (0, 1)  # S_OK / S_FALSE both require CoUninitialize
        try:
            hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
            if hr != 0 or not out.value:
                return None
            try:
                return Path(out.value).resolve(strict=False)
            finally:
                ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))
        finally:
            if did_init:
                ole32.CoUninitialize()
    except Exception:
        return None


def _known_folder(value: str) -> Path:
    cleaned = os.path.expandvars(str(value or "").strip().strip('\"'))
    key = cleaned.casefold().replace('ё', 'е')
    # Share the desktop resolver's exact alias table so system folders such as
    # Program Files (x86), ProgramData and redirected Downloads resolve the same
    # way in *every* filesystem workflow.  v3.4 had two separate alias tables,
    # which is how an "open folder" command and "create in folder" could point
    # at different places.
    alias_keys = {normalize_target(cleaned), normalize_target(strip_target_words(cleaned))}
    for alias, path in FOLDER_ALIASES.items():
        if normalize_target(alias) in alias_keys:
            return Path(path).expanduser().resolve(strict=False)
    # KNOWNFOLDERIDs keep working when Desktop/Documents/Downloads have been
    # redirected to OneDrive or another disk. Falling back to Path.home keeps
    # the project testable outside Windows.
    known = {
        "desktop": ("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "Desktop"),
        "рабочий стол": ("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "Desktop"),
        "documents": ("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "Documents"),
        "документы": ("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "Documents"),
        "downloads": ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
        "загрузки": ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
        "pictures": ("{33E28130-4E1E-4676-835A-98395C3BC3BB}", "Pictures"),
        "изображения": ("{33E28130-4E1E-4676-835A-98395C3BC3BB}", "Pictures"),
        "videos": ("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "Videos"),
        "видео": ("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "Videos"),
        "music": ("{4BD8D571-6D19-48D3-BE97-422220080E43}", "Music"),
        "музыка": ("{4BD8D571-6D19-48D3-BE97-422220080E43}", "Music"),
    }
    if key in {"home", "домашняя папка", "профиль", "profile"}:
        return Path.home().resolve(strict=False)
    item = known.get(key)
    if item:
        resolved = _windows_known_folder(item[0])
        return resolved or (Path.home() / item[1]).resolve(strict=False)
    return Path(cleaned).expanduser().resolve(strict=False)


def _resolve_named_folder(
    value: str,
    *,
    allow_fuzzy: bool = True,
    mutation: bool = False,
    max_seconds: float = 6.5,
) -> tuple[Path | None, float, list[Path]]:
    """Resolve a spoken folder name without silently choosing a bad neighbour.

    Exact paths/known-folder aliases/literal basename matches always win.  Fuzzy
    recovery is only a final ASR aid, with a deliberately high threshold; for a
    mutating operation it is stricter again.  The returned candidates allow the
    caller to ask for clarification instead of modifying an arbitrary directory.
    """
    raw = os.path.expandvars(str(value or "").strip().strip('\"'))
    if not raw:
        return None, 0.0, []

    # 1) An explicit existing path is authoritative.
    try:
        direct = Path(raw).expanduser()
        if direct.exists() and direct.is_dir():
            return direct.resolve(strict=False), 1.0, []
    except OSError:
        pass

    # 2) Exact Windows/user-folder aliases.  Do not use fuzzy alias matching here.
    qkeys = {normalize_target(raw), normalize_target(strip_target_words(raw))}
    for alias, path in FOLDER_ALIASES.items():
        if normalize_target(alias) in qkeys:
            candidate = Path(path).expanduser().resolve(strict=False)
            if candidate.exists() and candidate.is_dir():
                return candidate, 1.0, []

    # 3) Literal basename search.  Ambiguity is surfaced to the caller.
    exact = find_user_path_exact_candidates(raw, kind="folder", limit=5, max_seconds=max_seconds)
    if len(exact) == 1:
        return exact[0].resolve(strict=False), 1.0, []
    if len(exact) > 1:
        return None, 1.0, [Path(item).resolve(strict=False) for item in exact]

    if not allow_fuzzy:
        return None, 0.0, []

    # 4) High-confidence typo recovery only.  The old 0.62/0.72 acceptance was
    # far too permissive for names such as Important and could open/write into an
    # unrelated folder.  Mutation deliberately requires near-exact confidence.
    fuzzy = find_user_path(raw, kind="folder", max_seconds=max_seconds)
    if fuzzy is None or not fuzzy.exists() or not fuzzy.is_dir():
        return None, 0.0, []
    score = target_similarity(strip_target_words(raw), fuzzy.name)
    threshold = 0.92 if mutation else 0.86
    if score >= threshold:
        return fuzzy.resolve(strict=False), score, []
    return None, score, []


def _open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _copy_any(source: Path, destination: Path) -> None:
    if source.is_dir():
        if destination.exists():
            destination = destination / source.name
        shutil.copytree(source, destination)
    else:
        if destination.is_dir():
            destination = destination / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_file_atomic184(source, destination)


def _copy_file_atomic184(source: Path, destination: Path) -> None:
    """NEO 184: copy into a temporary file next to the target, check it, then swap it in.

    An interrupted copy (full disk, device error, cancelled) leaves an existing target
    exactly as it was; the temporary file is removed."""
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".neo-tmp", dir=str(destination.parent))
    os.close(fd)
    temp = Path(temp_name)
    try:
        shutil.copy2(source, temp)
        if temp.stat().st_size != source.stat().st_size:
            raise OSError("Размер скопированного файла не совпадает с исходным")
        os.replace(temp, destination)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _resolve_destination(source: Path, destination: Path) -> Path:
    """Return the actual destination path for file/folder copy/move semantics."""
    if destination.exists() and destination.is_dir():
        return destination / source.name
    return destination


def _verify_copy(source: Path, target: Path) -> None:
    if not target.exists():
        raise RuntimeError(f"Операция не подтверждена: объект не появился по адресу {target}")
    if source.is_file() and target.is_file() and source.stat().st_size != target.stat().st_size:
        raise RuntimeError("Размер скопированного файла не совпадает с исходным")


def _safe_extract_zip(archive: Path, destination: Path) -> list[str]:
    destination = destination.resolve(strict=False)
    extracted: list[str] = []
    with zipfile.ZipFile(archive, 'r') as zf:
        for info in zf.infolist():
            candidate = (destination / info.filename).resolve(strict=False)
            try:
                candidate.relative_to(destination)
            except ValueError as exc:
                raise RuntimeError(f"Небезопасный путь внутри архива: {info.filename}") from exc
        zf.extractall(destination)
        extracted = [str((destination / info.filename).resolve(strict=False)) for info in zf.infolist()]
    return extracted


def _drive_inventory() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for part in psutil.disk_partitions(all=False):
        mount = str(part.mountpoint)
        key = mount.casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            usage = psutil.disk_usage(mount)
            items.append({
                "mount": mount, "device": str(part.device), "filesystem": str(part.fstype),
                "total": int(usage.total), "used": int(usage.used), "free": int(usage.free),
                "percent": float(usage.percent),
            })
        except (PermissionError, OSError):
            items.append({"mount": mount, "device": str(part.device), "filesystem": str(part.fstype)})
    return items


def _backup_file(path: Path, backup_root: Path) -> Path | None:
    if not path.exists() or not path.is_file():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root.mkdir(parents=True, exist_ok=True)
    # Every mutation needs its own immutable snapshot. Timestamp-only folders
    # overwrote earlier backups when several edits reached the same file within
    # a second, making successive undo operations restore the wrong version.
    target_dir = Path(tempfile.mkdtemp(prefix=timestamp + "_", dir=str(backup_root)))
    target = target_dir / path.name
    shutil.copy2(path, target)
    return target



def _mutation_journal_path(backup_root: Path) -> Path:
    return backup_root / "file_mutations.jsonl"


def _record_file_mutation(
    backup_root: Path,
    path: Path,
    *,
    backup: Path | None,
    existed_before: bool,
    operation: str,
) -> None:
    record = {
        "type": "mutation",
        "id": f"{time.time_ns()}-{os.getpid()}",
        "created_at": datetime.now().isoformat(),
        "path": str(path),
        "backup": str(backup) if backup else "",
        "existed_before": bool(existed_before),
        "operation": operation,
    }
    # NEO 183: remember exactly what NEO left on disk, so undo can tell whether the file
    # is still NEO's version (and not a different object put at that path since).
    try:
        if Path(path).is_file() and not Path(path).is_symlink():
            data = Path(path).read_bytes()
            record["after_sha256"] = hashlib.sha256(data).hexdigest()
            record["after_size"] = len(data)
    except OSError:
        pass
    journal = _mutation_journal_path(backup_root)
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _undo_last_file_mutation(backup_root: Path, security: Any = None) -> tuple[Path, str]:
    journal = _mutation_journal_path(backup_root)
    if not journal.exists():
        raise RuntimeError("История изменений файлов пока пуста")
    records: list[dict[str, Any]] = []
    for line in journal.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    undone = {str(x.get("mutation_id", "")) for x in records if x.get("type") == "undo"}
    mutation = None
    for item in reversed(records):
        if item.get("type") == "mutation" and str(item.get("id", "")) not in undone:
            mutation = item
            break
    if mutation is None:
        raise RuntimeError("Нет изменения файла, которое можно отменить")
    raw_target = Path(str(mutation.get("path", ""))).expanduser()
    target = raw_target.resolve(strict=False)
    # NEO 183: the restore is a new write, so it obeys today's path policy, never follows a link
    # that appeared at the path, and does not overwrite a file that changed after NEO's edit.
    if raw_target.is_symlink() or Path(os.path.abspath(raw_target)) != target:
        raise RuntimeError("Отмена не выполнена: по этому пути теперь ссылка на другой объект")
    if security is not None:
        target = security.assert_mutation_path_allowed(target)
    expected = str(mutation.get("after_sha256", "") or "")
    if expected and target.is_file():
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Отмена не выполнена: файл изменился после правки NEO, новые изменения не трогаю")
    backup_value = str(mutation.get("backup", "") or "")
    backup = Path(backup_value) if backup_value else None
    existed_before = bool(mutation.get("existed_before", False))
    if backup is not None and backup.is_file():
        restored = backup.read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes183(target, restored)
        try:
            text = restored.decode("utf-8")
            _edit_active_text_window(target, text, append=False)
        except Exception:
            pass
        result = "восстановлена предыдущая версия"
    elif not existed_before:
        if target.exists():
            if target.is_dir():
                raise RuntimeError("Последнее изменение относится к папке; автоматический откат не применяется")
            try:
                send2trash(str(target))
            except Exception:
                target.unlink(missing_ok=True)
        result = "созданный файл удалён"
    else:
        raise RuntimeError("Резервная копия последнего изменения не найдена")
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "undo",
            "mutation_id": str(mutation.get("id", "")),
            "created_at": datetime.now().isoformat(),
        }, ensure_ascii=False) + "\n")
    return target, result

def _require_path_value(value: str | Path | None, *, label: str = "path") -> Path:
    """Resolve an explicit filesystem target without ever falling back to CWD."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"Не указан {label}")
    return Path(raw).expanduser().resolve(strict=False)


def _atomic_write_bytes183(path: Path, data: bytes) -> None:
    """Write next to the target and swap it in; on any error the original stays untouched."""
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".neo-tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _write_text_verified(path: Path, content: str, *, encoding: str = "utf-8", bom: bytes = b"") -> None:
    """Write the authoritative file on disk, flush it, then verify exact content.

    UI automation is deliberately secondary. A text editor may display a stale
    buffer, but NEO must never report success until the requested file path on
    disk contains exactly the requested text.
    """
    # NEO 182: check the codec and encode BEFORE touching the file, then write a temporary
    # file next to it and swap it in. A bad/unknown encoding or an unencodable character
    # used to truncate the existing file to 0 bytes.
    import codecs
    import tempfile
    try:
        codecs.lookup(encoding)
    except LookupError as exc:
        raise ValueError(f"Неизвестная кодировка: {encoding}") from exc
    try:
        data = bom + content.encode(encoding)
    except UnicodeEncodeError as exc:
        raise ValueError(f"Текст нельзя сохранить в кодировке {encoding}: символ {content[exc.start:exc.end]!r}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes183(path, data)
    # NEO 183: compare the bytes — reading text back normalised CRLF to LF and made a correct
    # CRLF write look like a failure.
    if path.read_bytes() != data:
        raise RuntimeError(f"Windows не подтвердил запись в файл: {path}")


def _find_open_text_window(path: Path):
    """Return a *strongly verified* editor window for ``path``.

    A matching basename in a title is not enough for a mutating UI action: two
    projects can both contain ``config.json``.  We require either an OS-reported
    open-file handle for the exact path or that the matching editor is currently
    foreground. This prevents the live-editor synchronization step from typing
    into an unrelated document while the disk write itself remains authoritative.
    """
    if os.name != "nt":
        return None
    try:
        from pywinauto import Desktop
        target = path.resolve(strict=False)
        filename = target.name.casefold()
        stem = target.stem.casefold()
        try:
            foreground_handle = int(ctypes.windll.user32.GetForegroundWindow())
        except Exception:
            foreground_handle = 0
        known_editors = {
            "notepad.exe", "notepad++.exe", "code.exe", "codium.exe", "cursor.exe",
            "pycharm64.exe", "idea64.exe", "sublime_text.exe", "devenv.exe",
            "windsurf.exe",
        }
        matches: list[tuple[int, Any]] = []
        for window in Desktop(backend="uia").windows(visible_only=True):
            try:
                title = str(window.window_text() or "").casefold()
                if not title or (filename not in title and stem not in title):
                    continue
                pid = int(window.element_info.process_id)
                proc = psutil.Process(pid)
                process = str(proc.name() or "").casefold()
                if process in {"explorer.exe", "python.exe", "pythonw.exe", "neo.exe"}:
                    continue
                score = 4 if filename in title else 2
                exact_open = False
                try:
                    opened = [Path(item.path).resolve(strict=False) for item in proc.open_files()]
                    exact_open = target in opened
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                    opened = []
                handle = int(getattr(window, "handle", 0) or 0)
                is_foreground = bool(foreground_handle and handle == foreground_handle)
                if exact_open:
                    score += 10
                elif is_foreground and process in known_editors and filename in title:
                    score += 6
                else:
                    # Do not let a title-only background match receive Ctrl+A/V.
                    continue
                matches.append((score, window))
            except Exception:
                continue
        if not matches:
            return None
        matches.sort(key=lambda x: x[0], reverse=True)
        if len(matches) > 1 and matches[0][0] - matches[1][0] < 3:
            return None
        return matches[0][1]
    except Exception:
        return None


def _sync_open_text_window(path: Path, desired_text: str) -> bool:
    """Replace an open editor document with the exact desired content and verify disk."""
    window = _find_open_text_window(path)
    if window is None:
        return False
    try:
        import pyautogui
        previous = pyperclip.paste()
        try:
            window.set_focus()
            time.sleep(0.08)
            # Focus the actual editor surface, not a sidebar/tree/toolbox that may
            # have retained keyboard focus when the top-level window was activated.
            editor = None
            candidates = []
            for control_type in ("Document", "Edit"):
                try:
                    candidates.extend(window.descendants(control_type=control_type))
                except Exception:
                    pass
            best_area = -1
            for candidate in candidates[:300]:
                try:
                    if not candidate.is_visible() or not candidate.is_enabled():
                        continue
                    rect = candidate.rectangle()
                    area = max(0, int(rect.right - rect.left)) * max(0, int(rect.bottom - rect.top))
                    if area > best_area:
                        editor, best_area = candidate, area
                except Exception:
                    continue
            if editor is None:
                return False
            try:
                editor.set_focus()
            except Exception:
                return False
            time.sleep(0.05)
            pyperclip.copy(desired_text)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.03)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.05)
            pyautogui.hotkey("ctrl", "s")
            deadline = time.monotonic() + 0.8
            while time.monotonic() < deadline:
                try:
                    current = path.read_text(encoding="utf-8", errors="replace")
                    if current == desired_text:
                        return True
                except Exception:
                    pass
                time.sleep(0.05)
        finally:
            try:
                pyperclip.copy(previous)
            except Exception:
                pass
    except Exception:
        return False
    return False


def _edit_active_text_window(path: Path, content: str, *, append: bool) -> bool:
    # Compatibility wrapper used by undo code. New writes compute exact desired
    # content first so an open Notepad tab and the file on disk cannot diverge.
    try:
        old = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    except Exception:
        old = ""
    desired = old + content if append else content
    return _sync_open_text_window(path, desired)


def _hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    kwargs: dict[str, Any] = {}
    try:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        si.wShowWindow = 0
        kwargs["startupinfo"] = si
    except Exception:
        pass
    return kwargs


def _run(command: list[str], *, timeout: int = 30, shell: bool = False) -> subprocess.CompletedProcess[str]:
    # HIST-064: native tools (ping, ipconfig, route…) print in the console code
    # page, not necessarily UTF-8. Bytes are decoded by the checked policy
    # (strict UTF-8 first, then the least damaged OEM/ANSI candidate) instead of
    # a forced UTF-8 with silent replacement characters.
    proc = subprocess.run(
        command,
        capture_output=True,
        text=False,
        timeout=timeout,
        shell=shell,
        check=False,
        **_hidden_subprocess_kwargs(),
    )
    if os.name == "nt":
        decode = _decode_windows_console_output
    else:
        def decode(raw: bytes | None) -> str:
            return bytes(raw or b"").decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(proc.args, proc.returncode, decode(proc.stdout), decode(proc.stderr))


def _decode_windows_console_output(raw: bytes | bytearray | None) -> str:
    """Decode localized legacy Windows console output without mojibake.

    Utilities such as powercfg still commonly emit OEM/ANSI bytes instead of
    UTF-8. Choose the candidate with the least replacement/control damage and
    a preference for readable alphabetic text.
    """
    data = bytes(raw or b"")
    if not data:
        return ""
    # If Windows is configured for the UTF-8 system locale, preserve genuine
    # UTF-8 output before trying legacy OEM/ANSI pages. Otherwise a valid UTF-8
    # Russian string can also be decoded as cp1251 into plausible-looking
    # mojibake and a heuristic may prefer the wrong candidate.
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    candidates: list[str] = []
    encodings = ["mbcs", "cp866", "cp1251", "cp850", "cp437"]
    try:
        import locale
        pref = locale.getpreferredencoding(False)
        if pref:
            encodings.insert(1, pref)
    except Exception:
        pass
    seen: set[str] = set()
    for encoding in encodings:
        key = str(encoding).casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            text = data.decode(encoding, errors="replace")
        except LookupError:
            continue
        candidates.append(text)
    if not candidates:
        return data.decode("utf-8", errors="replace")
    def score(text: str) -> tuple[int, int, int]:
        replacement = text.count("\ufffd")
        controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\r\n\t")
        letters = sum(1 for ch in text if ch.isalpha())
        return (replacement * 100 + controls * 20, -letters, len(text))
    return min(candidates, key=score)


def _run_windows_console(command: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        command, capture_output=True, text=False, timeout=timeout, shell=False,
        check=False, **_hidden_subprocess_kwargs(),
    )
    return subprocess.CompletedProcess(
        proc.args, proc.returncode,
        _decode_windows_console_output(proc.stdout),
        _decode_windows_console_output(proc.stderr),
    )


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "_", name, flags=re.UNICODE).strip(" .")
    return cleaned or "project"


def register_all(registry: Any) -> None:
    config = registry.config
    security = registry.security
    db = registry.database
    ollama = registry.ollama
    yandex_adapter = YandexMusicAdapter(config)
    spotify_adapter = SpotifyAdapter(config)
    youtube_adapter = YouTubeAPIAdapter(config)
    discord_adapter = shared_reliable_discord_adapter(config)
    # MEDIA/DISCORD reliability: expose the one adapter instance created by the
    # built-in plugin registrar. MainWindow uses this exact instance for Gateway
    # prewarm and passive now-playing observation; creating a second adapter
    # would split the shared-queue instance id and recreate the old "invisible"
    # Discord state until the first explicit command.
    owner_registry = getattr(registry, "_registry", registry)
    try:
        setattr(owner_registry, "discord_adapter", discord_adapter)
        setattr(owner_registry, "yandex_adapter", yandex_adapter)
        setattr(owner_registry, "spotify_adapter", spotify_adapter)
        setattr(owner_registry, "youtube_adapter", youtube_adapter)
    except Exception:
        pass

    # FILESYSTEM
    filesystem_actions = [
        "open",
        "list",
        "find",
        "create_folder",
        "copy",
        "move",
        "rename",
        "delete",
        "delete_permanent",
        "read_text",
        "write_text",
        "replace_text",
        "create_text_file",
        "create_file",
        "append_text",
        "insert_text",
        "search_text",
        "exists",
        "info",
        "zip",
        "unzip",
        "list_drives",
        "disk_usage",
        "tree",
        "hash",
        "undo_last_change",
        "reveal",
    ]

    def filesystem_risk(args: dict[str, Any]) -> Risk:
        action = args.get("action")
        if action in {"open", "list", "find", "read_text", "search_text", "exists", "info", "list_drives", "disk_usage", "tree", "hash", "reveal"}:
            return Risk.SAFE
        if action in {"create_folder", "copy", "move", "rename", "zip", "unzip"}:
            return Risk.MEDIUM
        if action in {"write_text", "append_text", "insert_text", "replace_text", "create_text_file", "create_file", "undo_last_change"}:
            return Risk.MEDIUM
        if action == "delete":
            # `delete` is Recycle Bin and therefore reversible.
            return Risk.MEDIUM
        return Risk.CRITICAL

    def filesystem(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "") or "").strip().casefold()
        # Local models occasionally use a natural synonym even though the schema
        # contains a strict enum. Normalize those variants instead of surfacing an
        # unhelpful “unknown filesystem action” error.
        action_aliases = {
            "open_file": "open", "open_folder": "open", "launch": "open", "execute": "open",
            "new_file": "create_file",
            "edit_text": "write_text", "edit_file": "write_text",
            "replace": "replace_text", "replace_in_file": "replace_text",
            "trash": "delete", "recycle": "delete", "delete_file": "delete",
            "move_file": "move", "rename_file": "rename", "copy_file": "copy",
        }
        action = action_aliases.get(action, action)
        if not action:
            if args.get("folder") and (args.get("name") or args.get("new_name")):
                action = "create_text_file"
            elif args.get("source"):
                action = "open"
        if str(args.get("action", "") or "").casefold() == "append_text":
            args = dict(args)
            args["append"] = True
        raw_source = str(args.get("source", "") or "").strip()
        raw_folder_arg = str(args.get("folder", "") or "").strip()
        if action == "find":
            # Use the same resumable, paced scanner as the dedicated search tool.
            # Scope resolution is exact: an unknown folder must not trigger a
            # second global fuzzy walk before the requested search even starts.
            from jarvis.runtime.file_search import resolve_search_roots, format_search_results
            from jarvis.runtime.search_jobs162 import run_search
            from jarvis.language import language_code
            roots = resolve_search_roots(raw_source or str(Path.home()), config=config)
            query = str(args.get("query", "*") or "*").strip() or "*"
            limit = max(1, min(int(args.get("limit", config.get("security.max_search_results", 100))), 500))
            rows = run_search(query, roots=roots, limit=limit, language=language_code(config),
                              recursive=bool(args.get("recursive", True)), literal=True, glob=True)
            paths = [str(row["path"]) for row in rows]
            incomplete = bool(getattr(rows, "incomplete", False))
            data = {"results": paths, "complete": False,
                    "continuable": bool(getattr(rows, "continuable", False))} if incomplete else paths
            return ToolExecution(bool(rows) or incomplete, format_search_results(rows, language_code(config)), data)
        source = _known_folder(raw_source) if raw_source else None
        # A bare filename such as ``test.txt`` must never resolve relative to the
        # NEO installation directory.  More importantly, a *mutating* action
        # must never fuzzy-search the whole profile for a same/similar filename:
        # that used to be the main way an edit intended for the foreground editor
        # could land in an unrelated file.  Mutation therefore requires an exact
        # path (normally supplied by Agent foreground context) or an explicitly
        # named folder. Read-only/open operations may still use user-folder search.
        exact_source_actions = {
            "write_text", "append_text", "replace_text", "insert_text",
            "delete", "delete_permanent", "move", "rename", "copy",
        }
        if raw_source and not re.search(r"[\\/]", raw_source) and not re.match(r"^[A-Za-z]:", raw_source):
            bound_folder: Path | None = None
            if raw_folder_arg:
                bound_folder, _folder_score, ambiguous = _resolve_named_folder(
                    raw_folder_arg, allow_fuzzy=True, mutation=action in exact_source_actions
                )
                if ambiguous:
                    raise ValueError(
                        f"Нашёл несколько папок с именем «{raw_folder_arg}». Уточните путь: "
                        + "; ".join(str(item) for item in ambiguous[:4])
                    )
                if bound_folder is not None and bound_folder.is_dir():
                    source = (bound_folder / raw_source).resolve(strict=False)
            elif action in exact_source_actions:
                # Fail closed.  Agent should resolve deictic phrases ("этот файл",
                # "сюда") from the actual foreground editor before reaching here.
                source = None
            elif action not in {"create_text_file", "create_file", "create_folder"}:
                kind = "folder" if action in {"list"} else "file" if Path(raw_source).suffix else "any"
                found = find_user_path(raw_source, kind=kind, max_seconds=2.4)
                if found is not None:
                    source = found
                elif source is not None and str(source).startswith(str(Path(__file__).resolve().parents[2])):
                    # Do not accidentally operate on files inside the assistant
                    # source tree merely because the current working directory is it.
                    source = Path.home() / raw_source
        # Natural-language locations are resolved across common user folders.
        # This lets commands such as "открой папку Important" work without a
        # full C:\Users\... path. Mutating operations still pass through the
        # normal security policy after resolution.
        if raw_source and (source is None or not source.exists()) and action in {"open", "list", "find", "read_text", "reveal"}:
            hint = "folder" if action in {"open", "list", "find"} else "file"
            resolved_source = find_user_path(raw_source, kind=hint)
            if resolved_source is not None:
                source = resolved_source
        destination = (
            _known_folder(str(args.get("destination", ""))) if args.get("destination") else None
        )

        if action == "list_drives":
            data = _drive_inventory()
            return ToolExecution(True, f"Доступных дисков/томов: {len(data)}", data)

        if action == "disk_usage":
            target = source or Path.home()
            anchor = target if target.is_dir() else target.parent
            usage = psutil.disk_usage(str(anchor))
            data = {"path": str(anchor), "total": int(usage.total), "used": int(usage.used), "free": int(usage.free), "percent": float(usage.percent)}
            return ToolExecution(True, f"Свободно {round(usage.free / (1024**3), 1)} ГБ из {round(usage.total / (1024**3), 1)} ГБ на {anchor}", data)

        if action == "exists":
            if source is None:
                raise ValueError("Не указан source")
            return ToolExecution(True, f"{'Существует' if source.exists() else 'Не существует'}: {source}", {"path": str(source), "exists": source.exists()})

        if action == "info":
            if source is None or not source.exists():
                raise FileNotFoundError(source)
            stat = source.stat()
            data = {
                "path": str(source), "name": source.name, "kind": "folder" if source.is_dir() else "file",
                "size": int(stat.st_size), "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "created": datetime.fromtimestamp(stat.st_ctime).isoformat(), "suffix": source.suffix if source.is_file() else "",
            }
            return ToolExecution(True, f"Информация: {source}", data)

        if action == "open":
            if source is None or not source.exists():
                raise FileNotFoundError(f"Путь не найден: {source}")
            _open_path(source)
            kind = "folder" if source.is_dir() else "file"
            label = "Папка" if kind == "folder" else "Файл"
            return ToolExecution(True, f"{label} открыт: {source}", {"path": str(source), "kind": kind, "action": "open"})

        if action == "reveal":
            if source is None or not source.exists():
                raise FileNotFoundError(f"Путь не найден: {source}")
            if os.name == "nt":
                if source.is_file():
                    subprocess.Popen(["explorer.exe", f'/select,{source}'])
                else:
                    subprocess.Popen(["explorer.exe", str(source)])
            else:
                _open_path(source.parent if source.is_file() else source)
            return ToolExecution(True, f"Показано в проводнике: {source}", {"path": str(source)})

        if action == "list":
            folder = source or Path.home()
            if not folder.is_dir():
                raise NotADirectoryError(folder)
            limit = min(int(args.get("limit", 100)), 500)
            items = []
            try:
                raw_items = list(folder.iterdir())
            except PermissionError as exc:
                raise PermissionError(f"Нет доступа к папке: {folder}") from exc
            def _sort_key(item: Path):
                try:
                    is_dir = item.is_dir()
                except OSError:
                    is_dir = False
                return (not is_dir, item.name.casefold())
            for item in sorted(raw_items, key=_sort_key)[:limit]:
                try:
                    is_dir = item.is_dir()
                    size = item.stat().st_size if item.is_file() else None
                except (PermissionError, OSError):
                    is_dir = False
                    size = None
                items.append({"name": item.name, "type": "folder" if is_dir else "file", "size": size, "path": str(item)})
            return ToolExecution(True, f"Найдено объектов: {len(items)}", items)

        if action == "create_folder":
            target = security.assert_mutation_path_allowed(_require_path_value(destination or source, label="путь новой папки"))
            target.mkdir(parents=True, exist_ok=True)
            if not target.is_dir():
                raise RuntimeError("Папка не появилась после создания")
            return ToolExecution(True, f"Папка создана: {target}", {"path": str(target), "kind": "folder", "action": "create_folder", "verified": True})

        if action == "copy":
            if source is None or destination is None:
                raise ValueError("Для копирования нужны source и destination")
            if not source.exists():
                raise FileNotFoundError(source)
            target = security.assert_mutation_path_allowed(destination)
            kind = "folder" if source.is_dir() else "file"
            final_target = _resolve_destination(source, target)
            # NEO 183: the object that will really be written must be allowed too, and a link
            # placed at that name is never followed (it could point outside the allowed roots).
            if final_target.is_symlink():
                raise PermissionError(f"Копирование отменено: {final_target} — ссылка на другой объект")
            security.assert_mutation_path_allowed(final_target)
            existed_before = final_target.exists()
            backup = _backup_file(final_target, config.paths.backups) if kind == "file" else None
            _copy_any(source, target)
            _verify_copy(source, final_target)
            if kind == "file":
                _record_file_mutation(config.paths.backups, final_target, backup=backup, existed_before=existed_before, operation="copy")
            label = "Папка" if kind == "folder" else "Файл"
            return ToolExecution(True, f"{label} скопирован{'а' if kind == 'folder' else ''}: {source} → {final_target}", {"path": str(final_target), "source": str(source), "kind": kind, "action": "copy", "verified": True})

        if action == "move":
            if source is None or destination is None:
                raise ValueError("Для перемещения нужны source и destination")
            if not source.exists():
                raise FileNotFoundError(source)
            # NEO 182: a move deletes the source, so the source must be inside the allowed
            # roots too — and so must the final path after a folder destination is resolved.
            security.assert_mutation_path_allowed(source)
            target = security.assert_mutation_path_allowed(destination)
            security.assert_mutation_path_allowed(_resolve_destination(source, target))
            target.parent.mkdir(parents=True, exist_ok=True)
            kind = "folder" if source.is_dir() else "file"
            final = Path(shutil.move(str(source), str(target))).resolve(strict=False)
            if not final.exists() or source.exists():
                raise RuntimeError("Windows не подтвердил завершение перемещения")
            label = "Папка" if kind == "folder" else "Файл"
            return ToolExecution(True, f"{label} перемещён{'а' if kind == 'folder' else ''}: {source} → {final}", {"path": str(final), "source": str(source), "kind": kind, "action": "move", "verified": True})

        if action == "rename":
            if source is None or not source.exists():
                raise FileNotFoundError(source)
            new_name = str(args.get("new_name", "")).strip().strip('"')
            if not new_name:
                raise ValueError("Не задано new_name")
            if source.is_file() and source.suffix and not Path(new_name).suffix:
                new_name += source.suffix
            new_name = _safe_filename(new_name)
            target = security.assert_mutation_path_allowed(source.with_name(new_name))
            kind = "folder" if source.is_dir() else "file"
            source.rename(target)
            if not target.exists() or source.exists():
                raise RuntimeError("Windows не подтвердил переименование")
            label = "Папка" if kind == "folder" else "Файл"
            return ToolExecution(True, f"{label} переименован{'а' if kind == 'folder' else ''}: {target}", {"path": str(target), "kind": kind, "action": "rename", "verified": True})

        if action == "delete":
            if source is None or not source.exists():
                raise FileNotFoundError(source)
            security.assert_mutation_path_allowed(source)
            kind = "folder" if source.is_dir() else "file"
            backup = _backup_file(source, config.paths.backups) if source.is_file() else None
            send2trash(str(source))
            if source.exists():
                raise RuntimeError("Windows не подтвердил перемещение объекта в Корзину")
            if kind == "file":
                _record_file_mutation(config.paths.backups, source, backup=backup, existed_before=True, operation="recycle_delete")
            label = "Папка" if kind == "folder" else "Файл"
            return ToolExecution(True, f"{label} перемещён{'а' if kind == 'folder' else ''} в Корзину: {source}", {"path": str(source), "kind": kind, "action": "recycle", "deleted": True, "verified": True, "backup": str(backup) if backup else ""})

        if action == "delete_permanent":
            if source is None or not source.exists():
                raise FileNotFoundError(source)
            security.assert_mutation_path_allowed(source)
            if source.is_dir():
                shutil.rmtree(source)
            else:
                source.unlink()
            if source.exists():
                raise RuntimeError("Windows не подтвердил безвозвратное удаление")
            return ToolExecution(True, f"Удалено безвозвратно: {source}", {"path": str(source), "verified": True})

        if action == "read_text":
            if source is None or not source.is_file():
                raise FileNotFoundError(source)
            max_chars = min(int(args.get("max_chars", 50000)), 200000)
            text = source.read_text(encoding=str(args.get("encoding", "utf-8")), errors="replace")
            return ToolExecution(True, f"Прочитан файл: {source}", text[:max_chars])

        if action == "write_text":
            from jarvis.language import language_code as _lang183
            from jarvis.runtime import text_encoding183 as te
            code183 = _lang183(config)
            raw_target = destination or source
            target = security.assert_mutation_path_allowed(_require_path_value(raw_target, label="файл"))
            target.parent.mkdir(parents=True, exist_ok=True)
            existed_before = target.exists()
            append = bool(args.get("append", False))
            content = str(args.get("content", ""))
            explicit_encoding = bool(str(args.get("encoding", "") or "").strip())
            encoding = str(args.get("encoding", "") or "utf-8")
            bom = b""
            old_text = ""
            # NEO 183: the existing file decides its own encoding (BOM, UTF-8, Cyrillic CP1251);
            # binary or undecidable files are refused before anything is touched.
            if existed_before and target.is_file():
                try:
                    old_bytes = target.read_bytes()
                except PermissionError:
                    return ToolExecution(False, te.message("locked", code183), {"path": str(target), "outcome": "locked"})
                if explicit_encoding:
                    try:
                        old_text = old_bytes.decode(encoding) if append else ""
                    except (LookupError, UnicodeDecodeError):
                        return ToolExecution(False, te.message("unknown", code183), {"path": str(target), "outcome": "unknown_encoding"})
                else:
                    detected = te.detect(old_bytes)
                    if not detected.ok:
                        if append or detected.reason == "binary":
                            return ToolExecution(False, te.message(detected.reason or "unknown", code183), {"path": str(target), "outcome": "binary_file" if detected.reason == "binary" else "unknown_encoding"})
                    else:
                        encoding, bom = str(detected.codec), detected.bom
                        old_text = te.decode(old_bytes, detected) if append else ""
                        if append:
                            content = te.match_newlines(content, detected.newline)
            import codecs as _codecs
            try:
                _codecs.lookup(encoding)
                content.encode(encoding)
            except (LookupError, UnicodeEncodeError):
                return ToolExecution(False, te.message("invalid_encoding", code183, encoding=encoding), {"path": str(target), "outcome": "invalid_encoding"})
            backup = _backup_file(target, config.paths.backups)
            desired_text = old_text + content if append else content

            # Disk is authoritative. Never let UI automation decide whether a
            # filesystem operation succeeded.
            try:
                _write_text_verified(target, desired_text, encoding=encoding, bom=bom)
            except PermissionError:
                return ToolExecution(False, te.message("locked", code183), {"path": str(target), "outcome": "locked"})
            except OSError as exc:
                if getattr(exc, "errno", None) == 28:
                    return ToolExecution(False, te.message("no_space", code183), {"path": str(target), "outcome": "no_space"})
                raise
            # From here the file IS changed: journal it first, so undo always reaches it.
            _record_file_mutation(config.paths.backups, target, backup=backup, existed_before=existed_before, operation="write_text")
            edited_live = _sync_open_text_window(target, desired_text)
            if target.read_bytes() != bom + desired_text.encode(encoding):
                raise RuntimeError("Редактор изменил файл после проверки; операция отменена как неподтверждённая")
            message = f"Текст записан: {target}"
            if edited_live:
                message += ". Открытый редактор синхронизирован"
            if backup:
                message += f". Резервная копия: {backup}"
            return ToolExecution(True, message, {"path": str(target), "kind": "file", "action": "write_text", "append": append,
                                                 "edited_live": edited_live, "verified": True, "encoding": encoding})

        if action == "create_text_file":
            raw_folder = str(args.get("folder", "") or args.get("destination", "") or "").strip()
            if raw_folder:
                folder, folder_score, ambiguous = _resolve_named_folder(raw_folder, allow_fuzzy=True, mutation=True)
                if ambiguous:
                    raise ValueError(
                        f"Нашёл несколько папок с именем «{raw_folder}». Уточните путь перед созданием файла: "
                        + "; ".join(str(item) for item in ambiguous[:4])
                    )
                if folder is None:
                    raise FileNotFoundError(
                        f"Папка не найдена достаточно точно: {raw_folder}"
                        + (f" (лучшее совпадение {folder_score:.2f})" if folder_score else "")
                    )
            else:
                folder = (Path.home() / "Documents").resolve(strict=False)
            if not folder.exists() or not folder.is_dir():
                raise FileNotFoundError(f"Папка не найдена: {raw_folder or folder}")
            file_name = str(args.get("name", "") or args.get("new_name", "") or "note.txt").strip()
            if not Path(file_name).suffix:
                file_name += ".txt"
            target = security.assert_mutation_path_allowed(folder / _safe_filename(file_name))
            target.parent.mkdir(parents=True, exist_ok=True)
            existed_before = target.exists()
            backup = _backup_file(target, config.paths.backups)
            initial_content = str(args.get("content", ""))
            encoding = str(args.get("encoding", "utf-8"))
            _write_text_verified(target, initial_content, encoding=encoding)
            if not target.is_file():
                raise RuntimeError("Созданный файл не появился на диске")
            message = f"Файл создан: {target}"
            if backup:
                message += f". Предыдущая версия сохранена: {backup}"
            _record_file_mutation(config.paths.backups, target, backup=backup, existed_before=existed_before, operation="create_text_file")
            return ToolExecution(True, message, {"path": str(target), "folder": str(folder), "kind": "file", "action": "create_text_file", "verified": True})

        if action == "replace_text":
            target = security.assert_mutation_path_allowed(_require_path_value(source or destination, label="файл"))
            if not target.is_file():
                raise FileNotFoundError(target)
            old = str(args.get("old_text", ""))
            new = str(args.get("content", ""))
            if not old:
                raise ValueError("Для replace_text нужен old_text")
            text = target.read_text(encoding=str(args.get("encoding", "utf-8")), errors="replace")
            if old not in text:
                raise ValueError("Указанный фрагмент не найден в файле")
            existed_before = target.exists()
            backup = _backup_file(target, config.paths.backups)
            count = int(args.get("count", 1) or 1)
            text = text.replace(old, new, max(1, count))
            encoding = str(args.get("encoding", "utf-8"))
            _write_text_verified(target, text, encoding=encoding)
            edited_live = _sync_open_text_window(target, text)
            if target.read_text(encoding=encoding, errors="replace") != text:
                raise RuntimeError("Windows не подтвердил изменение файла")
            message = f"Файл изменён{' прямо в открытом редакторе' if edited_live else ''}: {target}"
            if backup:
                message += f". Резервная копия: {backup}"
            _record_file_mutation(config.paths.backups, target, backup=backup, existed_before=existed_before, operation="replace_text")
            return ToolExecution(True, message, {"path": str(target), "kind": "file", "action": "replace_text", "edited_live": edited_live})

        if action == "create_file":
            raw_folder = str(args.get("folder", "") or args.get("destination", "") or "").strip()
            if raw_folder:
                folder, folder_score, ambiguous = _resolve_named_folder(raw_folder, allow_fuzzy=True, mutation=True)
                if ambiguous:
                    raise ValueError(
                        f"Нашёл несколько папок с именем «{raw_folder}». Уточните путь перед созданием файла: "
                        + "; ".join(str(item) for item in ambiguous[:4])
                    )
                if folder is None:
                    raise FileNotFoundError(
                        f"Папка не найдена достаточно точно: {raw_folder}"
                        + (f" (лучшее совпадение {folder_score:.2f})" if folder_score else "")
                    )
            else:
                folder = source.parent if source and source.suffix else (source if source and source.is_dir() else Path.home() / "Documents")
            if not folder.exists() or not folder.is_dir():
                raise FileNotFoundError(f"Папка не найдена: {raw_folder or folder}")
            name = str(args.get("name", "") or args.get("new_name", "") or "new_file").strip()
            target = security.assert_mutation_path_allowed(folder / _safe_filename(name))
            existed_before = target.exists()
            backup = _backup_file(target, config.paths.backups)
            content = str(args.get("content", "") or "")
            encoding = str(args.get("encoding", "utf-8"))
            _write_text_verified(target, content, encoding=encoding)
            if not target.exists():
                raise RuntimeError("Файл не появился после создания")
            _record_file_mutation(config.paths.backups, target, backup=backup, existed_before=existed_before, operation="create_file")
            return ToolExecution(True, f"Файл создан: {target}", {"path": str(target), "kind": "file", "action": "create_file", "verified": True})

        if action == "append_text":
            nested = dict(args)
            nested["action"] = "write_text"
            nested["append"] = True
            return filesystem(nested)

        if action == "search_text":
            if source is None or not source.is_file():
                raise FileNotFoundError(source)
            query = str(args.get("query", "") or args.get("old_text", "") or "")
            if not query:
                raise ValueError("Не указан текст для поиска")
            text = source.read_text(encoding=str(args.get("encoding", "utf-8")), errors="replace")
            exact = bool(args.get("case_sensitive", False))
            needle = query if exact else query.casefold()
            lines = []
            for number, line in enumerate(text.splitlines(), start=1):
                hay = line if exact else line.casefold()
                if needle in hay:
                    lines.append({"line": number, "text": line})
                    if len(lines) >= min(int(args.get("limit", 100) or 100), 500):
                        break
            return ToolExecution(True, f"Совпадений в {source.name}: {len(lines)}", {"path": str(source), "matches": lines})

        if action == "insert_text":
            target = security.assert_mutation_path_allowed(_require_path_value(source or destination, label="файл"))
            if not target.is_file():
                raise FileNotFoundError(target)
            encoding = str(args.get("encoding", "utf-8"))
            text = target.read_text(encoding=encoding, errors="replace")
            content = str(args.get("content", ""))
            before = str(args.get("before_text", "") or "")
            after = str(args.get("after_text", "") or "")
            line_no = int(args.get("line", 0) or 0)
            if before:
                pos = text.find(before)
                if pos < 0:
                    raise ValueError("Фрагмент before_text не найден")
                desired = text[:pos] + content + text[pos:]
            elif after:
                pos = text.find(after)
                if pos < 0:
                    raise ValueError("Фрагмент after_text не найден")
                pos += len(after)
                desired = text[:pos] + content + text[pos:]
            elif line_no > 0:
                lines = text.splitlines(keepends=True)
                pos = max(0, min(len(lines), line_no - 1))
                lines.insert(pos, content)
                desired = ''.join(lines)
            else:
                raise ValueError("Для insert_text укажите before_text, after_text или line")
            backup = _backup_file(target, config.paths.backups)
            _write_text_verified(target, desired, encoding=encoding)
            edited_live = _sync_open_text_window(target, desired)
            if target.read_text(encoding=encoding, errors="replace") != desired:
                raise RuntimeError("Не удалось подтвердить вставку текста")
            _record_file_mutation(config.paths.backups, target, backup=backup, existed_before=True, operation="insert_text")
            return ToolExecution(True, f"Текст вставлен: {target}", {"path": str(target), "edited_live": edited_live, "verified": True})

        if action == "tree":
            folder = source or Path.home()
            if not folder.exists() or not folder.is_dir():
                raise NotADirectoryError(folder)
            max_depth = max(0, min(8, int(args.get("depth", 2) or 2)))
            limit = max(1, min(1000, int(args.get("limit", 200) or 200)))
            root_depth = len(folder.parts)
            items: list[dict[str, Any]] = []
            skip_names = {"$recycle.bin", "system volume information", ".git", ".venv", "venv", "__pycache__"}
            def onerror(_exc):
                return None
            for dirpath, dirnames, filenames in os.walk(folder, onerror=onerror):
                here = Path(dirpath)
                depth = len(here.parts) - root_depth
                dirnames[:] = [d for d in dirnames if d.casefold() not in skip_names]
                if depth >= max_depth:
                    dirnames[:] = []
                for name in sorted(dirnames):
                    items.append({"path": str(here / name), "name": name, "type": "folder", "depth": depth + 1})
                    if len(items) >= limit:
                        break
                if len(items) >= limit:
                    break
                for name in sorted(filenames):
                    item = here / name
                    try:
                        size = item.stat().st_size
                    except OSError:
                        size = None
                    items.append({"path": str(item), "name": name, "type": "file", "depth": depth + 1, "size": size})
                    if len(items) >= limit:
                        break
                if len(items) >= limit:
                    break
            return ToolExecution(True, f"Дерево папки получено: {folder} ({len(items)} объектов)", {"path": str(folder), "items": items, "depth": max_depth})

        if action == "hash":
            if source is None or not source.is_file():
                raise FileNotFoundError(source)
            algorithm = str(args.get("algorithm", "sha256") or "sha256").casefold()
            if algorithm not in {"sha256", "sha1", "md5"}:
                raise ValueError("Поддерживаются sha256, sha1 и md5")
            digest = hashlib.new(algorithm)
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            value = digest.hexdigest()
            return ToolExecution(True, f"{algorithm.upper()} {source.name}: {value}", {"path": str(source), "algorithm": algorithm, "hash": value})

        if action == "zip":
            if source is None or not source.exists():
                raise FileNotFoundError(source)
            target = destination or source.with_suffix('.zip')
            if target.suffix.casefold() != '.zip':
                target = target / f"{source.name}.zip" if target.exists() and target.is_dir() else target.with_suffix('.zip')
            target = security.assert_mutation_path_allowed(target)
            # NEO 183: an archive can never be its own source — that used to wipe the original ZIP.
            if target.resolve(strict=False) == source.resolve(strict=False) or (target.exists() and source.exists() and os.path.samefile(target, source)):
                return ToolExecution(False, "Архив не создан: архив назначения совпадает с исходным файлом. Укажите другое имя.", {"path": str(target), "outcome": "same_file"})
            target.parent.mkdir(parents=True, exist_ok=True)
            existed_before = target.exists()
            backup = _backup_file(target, config.paths.backups)
            fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".neo-tmp", dir=str(target.parent))
            os.close(fd)
            try:
                with zipfile.ZipFile(temp_name, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                    if source.is_file():
                        zf.write(source, arcname=source.name)
                    else:
                        skip = {target.resolve(strict=False), Path(temp_name).resolve(strict=False)}
                        for item in source.rglob('*'):
                            if item.is_file() and item.resolve(strict=False) not in skip:
                                zf.write(item, arcname=str(item.relative_to(source.parent)))
                os.replace(temp_name, target)
            except BaseException:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
            if not target.is_file() or target.stat().st_size <= 0:
                raise RuntimeError("Архив не был создан")
            _record_file_mutation(config.paths.backups, target, backup=backup, existed_before=existed_before, operation="zip")
            return ToolExecution(True, f"Архив создан: {target}", {"path": str(target), "source": str(source), "verified": True})

        if action == "unzip":
            if source is None or not source.is_file():
                raise FileNotFoundError(source)
            if not zipfile.is_zipfile(source):
                raise ValueError("Файл не является ZIP-архивом")
            target = security.assert_mutation_path_allowed(destination or (source.parent / source.stem))
            target.mkdir(parents=True, exist_ok=True)
            extracted = _safe_extract_zip(source, target)
            return ToolExecution(True, f"Архив распакован: {source} → {target}", {"path": str(target), "items": extracted[:200], "verified": True})

        if action == "undo_last_change":
            target, result = _undo_last_file_mutation(config.paths.backups, security)
            return ToolExecution(True, f"Последнее изменение файла отменено: {target} — {result}.", {"path": str(target), "undone": True})

        raise ValueError(f"Неизвестное действие filesystem: {action}")

    registry.register(
        ToolDefinition(
            "filesystem",
            "Полное голосовое управление пользовательскими файлами, папками, архивами и дисками Windows: открыть/найти/прочитать/создать/изменить/скопировать/переместить/переименовать/отправить в Корзину, дерево папки, хеш, ZIP, поиск текста и сведения о дисках. Диск является источником истины: после изменения результат перечитывается и проверяется; системные пути и безвозвратное удаление защищены.",
            _object_schema(
                {
                    "action": _enum(filesystem_actions, "Операция"),
                    "source": {"type": "string", "description": "Исходный путь или папка"},
                    "destination": {"type": "string", "description": "Путь назначения"},
                    "new_name": {"type": "string"},
                    "folder": {"type": "string", "description": "Папка по пути или обычному названию"},
                    "name": {"type": "string", "description": "Имя создаваемого файла"},
                    "query": {"type": "string", "description": "Имя или маска поиска"},
                    "recursive": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    "content": {"type": "string"},
                    "old_text": {"type": "string", "description": "Точный фрагмент, который нужно заменить"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 100},
                    "append": {"type": "boolean", "default": False},
                    "encoding": {"type": "string", "default": "utf-8"},
                    "max_chars": {"type": "integer"},
                    "line": {"type": "integer", "minimum": 1},
                    "before_text": {"type": "string"},
                    "after_text": {"type": "string"},
                    "case_sensitive": {"type": "boolean", "default": False},
                    "depth": {"type": "integer", "minimum": 0, "maximum": 8, "description": "Глубина дерева папки"},
                    "algorithm": _enum(["sha256", "sha1", "md5"], "Алгоритм хеша файла"),
                },
                ["action"],
            ),
            filesystem,
            filesystem_risk,
        )
    )

    # APPLICATIONS
    def application_risk(args: dict[str, Any]) -> Risk:
        if args.get("action") == "kill":
            return Risk.HIGH
        return Risk.MEDIUM if args.get("action") == "close" else Risk.SAFE

    def _normalized_app_name(value: str) -> str:
        return re.sub(r"[^a-zа-яё0-9]+", " ", value.casefold()).strip()

    _CRITICAL_PROCESSES = {
        "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
        "services.exe", "lsass.exe", "fontdrvhost.exe",
    }

    def _process_matches(name: str, *, threshold: float = 0.72) -> list[tuple[float, psutil.Process, dict[str, Any]]]:
        needle = _normalized_app_name(name)
        if not needle:
            return []
        matches: list[tuple[float, psutil.Process, dict[str, Any]]] = []
        own_pid = os.getpid()
        for proc in psutil.process_iter(["pid", "name", "exe", "status", "username", "memory_percent", "cpu_percent", "create_time"]):
            try:
                pid = int(proc.info.get("pid") or -1)
                if pid == own_pid:
                    continue
                raw_name = str(proc.info.get("name") or "")
                exe = str(proc.info.get("exe") or "")
                candidates = [raw_name, Path(raw_name).stem, Path(exe).name if exe else "", Path(exe).stem if exe else ""]
                score = 0.0
                for candidate in candidates:
                    c = _normalized_app_name(candidate)
                    if not c:
                        continue
                    if c == needle:
                        score = max(score, 1.0)
                    elif needle in c or c in needle:
                        score = max(score, 0.93)
                    else:
                        score = max(score, difflib.SequenceMatcher(None, needle, c).ratio())
                if score >= threshold:
                    matches.append((score, proc, dict(proc.info)))
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        matches.sort(key=lambda x: (-x[0], int(x[2].get("pid") or 0)))
        return matches

    def _assert_process_not_critical(proc: psutil.Process, info: dict[str, Any]) -> None:
        raw = str(info.get("name") or "").casefold()
        if raw in _CRITICAL_PROCESSES or int(info.get("pid") or -1) in {0, 4}:
            raise PermissionError(f"Критический процесс Windows защищён: {raw or proc.pid}")

    def _find_start_menu_link(name: str) -> Path | None:
        if os.name != "nt":
            return None
        roots = [
            Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.getenv("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path.home() / "Desktop",
            Path(os.getenv("PUBLIC", "C:/Users/Public")) / "Desktop",
        ]
        needle = _normalized_app_name(name)
        best: tuple[float, Path] | None = None
        for root in roots:
            if not root.exists():
                continue
            for pattern in ("*.lnk", "*.url", "*.appref-ms"):
                for link in root.rglob(pattern):
                    stem = _normalized_app_name(link.stem)
                    if not stem:
                        continue
                    if stem == needle:
                        return link
                    score = difflib.SequenceMatcher(None, needle, stem).ratio()
                    if needle in stem or stem in needle:
                        score = max(score, 0.86)
                    if best is None or score > best[0]:
                        best = (score, link)
        return best[1] if best and best[0] >= 0.68 else None

    def _steam_library_roots() -> list[Path]:
        roots: list[Path] = []
        if os.name == "nt":
            try:
                import winreg

                for hive, key_path in (
                    (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                ):
                    try:
                        with winreg.OpenKey(hive, key_path) as key:
                            value, _ = winreg.QueryValueEx(key, "SteamPath" if hive == winreg.HKEY_CURRENT_USER else "InstallPath")
                            roots.append(Path(str(value)))
                    except OSError:
                        pass
            except Exception:
                pass
        roots.extend([Path("C:/Program Files (x86)/Steam"), Path("C:/Program Files/Steam")])
        expanded: list[Path] = []
        for root in roots:
            if root.exists() and root not in expanded:
                expanded.append(root)
            vdf = root / "steamapps" / "libraryfolders.vdf"
            if vdf.exists():
                try:
                    text = vdf.read_text(encoding="utf-8", errors="replace")
                    for match in re.finditer(r'"path"\s+"([^"]+)"', text):
                        library = Path(match.group(1).replace("\\\\", "\\"))
                        if library.exists() and library not in expanded:
                            expanded.append(library)
                except OSError:
                    pass
        return expanded

    def _find_steam_game(name: str) -> tuple[str, str, Path, float] | None:
        needle = _normalized_app_name(name)
        best: tuple[float, str, str, Path] | None = None
        for root in _steam_library_roots():
            manifests = root / "steamapps"
            if not manifests.exists():
                continue
            for manifest in manifests.glob("appmanifest_*.acf"):
                try:
                    text = manifest.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                name_match = re.search(r'"name"\s+"([^"]+)"', text)
                appid_match = re.search(r'"appid"\s+"(\d+)"', text)
                installdir_match = re.search(r'"installdir"\s+"([^"]+)"', text)
                if not name_match or not appid_match:
                    continue
                game_name = name_match.group(1)
                normalized_game = _normalized_app_name(game_name)
                score = target_similarity(name, game_name)
                if needle in normalized_game or normalized_game in needle:
                    score = max(score, 0.90)
                # Use the same transliteration-aware scoring as StartApps/EXE
                # discovery so Russian speech can match English store manifests.
                if best is None or score > best[0]:
                    install_dir = manifests / "common" / (installdir_match.group(1) if installdir_match else game_name)
                    best = (score, appid_match.group(1), game_name, install_dir)
        if best and best[0] >= 0.66:
            return best[1], best[2], best[3], best[0]
        return None

    def _find_epic_game(name: str) -> tuple[str, Path, float] | None:
        if os.name != "nt":
            return None
        root = Path(os.getenv("PROGRAMDATA", "C:/ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
        if not root.exists():
            return None
        needle = _normalized_app_name(name)
        best: tuple[float, str, Path] | None = None
        for item in root.glob("*.item"):
            try:
                data = json.loads(item.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            display = str(data.get("DisplayName", "") or "").strip()
            install = Path(str(data.get("InstallLocation", "") or ""))
            launch = str(data.get("LaunchExecutable", "") or "").strip()
            if not display or not install or not launch:
                continue
            exe = install / launch
            if not exe.is_file():
                continue
            normalized_game = _normalized_app_name(display)
            score = target_similarity(name, display)
            if needle in normalized_game or normalized_game in needle:
                score = max(score, 0.90)
            if best is None or score > best[0]:
                best = (score, display, exe)
        if best and best[0] >= 0.66:
            return best[1], best[2], best[0]
        return None

    def _process_running_from_executable(executable: str | Path) -> tuple[bool, int]:
        raw = str(executable or "").strip()
        is_absolute = Path(raw).is_absolute()
        try:
            wanted = os.path.normcase(os.path.abspath(raw)) if is_absolute else ""
        except Exception:
            wanted = ""
        wanted_name = Path(raw).name.casefold()
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                exe = str(proc.info.get("exe") or "")
                proc_name = str(proc.info.get("name") or "").casefold()
                if wanted and exe and os.path.normcase(os.path.abspath(exe)) == wanted:
                    return True, int(proc.info.get("pid") or proc.pid)
                # System commands such as notepad.exe/cmd.exe may be returned by
                # an alias without an absolute path. In that case exact basename
                # identity is a safer post-condition than fabricating cwd/abspath.
                if not is_absolute and wanted_name:
                    exe_name = Path(exe).name.casefold() if exe else ""
                    if wanted_name in {proc_name, exe_name}:
                        return True, int(proc.info.get("pid") or proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return False, 0

    def _win32_top_level_windows(*, include_hidden: bool = False) -> list[dict[str, Any]]:
        """Enumerate real top-level application windows with bounded Win32 calls.

        HOTFIX52 can optionally include hidden top-level windows. Many tray apps do
        not destroy their main window: they simply call ``ShowWindow(SW_HIDE)``.
        Looking only at ``IsWindowVisible`` therefore makes a perfectly restorable
        app look as if it has no window. Tool windows are kept as metadata but are
        never preferred for tray restoration.
        """
        if os.name != "nt":
            return []
        try:
            user32 = ctypes.windll.user32
            results: list[dict[str, Any]] = []
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            GW_OWNER = 4
            DWMWA_CLOAKED = 14

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            @WNDENUMPROC
            def callback(hwnd, _lparam):
                try:
                    visible = bool(user32.IsWindowVisible(hwnd))
                    if not visible and not include_hidden:
                        return True
                    length = int(user32.GetWindowTextLengthW(hwnd) or 0)
                    title = ""
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(min(length + 1, 4096))
                        user32.GetWindowTextW(hwnd, buf, len(buf))
                        title = str(buf.value or "").strip()
                    # Visible untitled windows are rarely useful for semantic
                    # activation. Hidden untitled top-level HWNDs, however, are a
                    # common tray/minimize implementation and remain useful when we
                    # already know the owning PID/executable.
                    if not title and not include_hidden:
                        return True
                    pid = ctypes.c_ulong(0)
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    ex_style = int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) or 0)
                    owner = int(user32.GetWindow(hwnd, GW_OWNER) or 0)
                    cls_buf = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hwnd, cls_buf, len(cls_buf))
                    rect = RECT()
                    got_rect = bool(user32.GetWindowRect(hwnd, ctypes.byref(rect)))
                    left = int(rect.left) if got_rect else 0
                    top = int(rect.top) if got_rect else 0
                    right = int(rect.right) if got_rect else 0
                    bottom = int(rect.bottom) if got_rect else 0
                    cloaked = False
                    try:
                        cloaked_value = ctypes.c_int(0)
                        hr = int(ctypes.windll.dwmapi.DwmGetWindowAttribute(
                            hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked_value), ctypes.sizeof(cloaked_value)
                        ))
                        if hr == 0:
                            cloaked = bool(cloaked_value.value)
                    except Exception:
                        pass
                    results.append({
                        "hwnd": int(hwnd), "pid": int(pid.value), "title": title,
                        "visible": visible, "tool_window": bool(ex_style & WS_EX_TOOLWINDOW),
                        "owner": owner, "class_name": str(cls_buf.value or ""),
                        "left": left, "top": top, "right": right, "bottom": bottom,
                        "width": max(0, right - left), "height": max(0, bottom - top),
                        "cloaked": cloaked,
                    })
                except Exception:
                    pass
                return True

            user32.EnumWindows(callback, 0)
            return results
        except Exception:
            return []

    def _window_item_is_user_facing(
        item: dict[str, Any], *, allow_hidden: bool = False,
        min_width: int = 96, min_height: int = 64, allow_untitled: bool = True,
    ) -> bool:
        """Reject message/helper HWNDs before they can masquerade as an app window."""
        if not isinstance(item, dict):
            return False
        if bool(item.get("tool_window", False)) or bool(item.get("cloaked", False)):
            return False
        if bool(item.get("owner", 0)):
            return False
        if not allow_hidden and not bool(item.get("visible", False)):
            return False
        try:
            width = int(item.get("width", 0) or 0)
            height = int(item.get("height", 0) or 0)
        except Exception:
            return False
        if width < int(min_width) or height < int(min_height):
            return False
        if not allow_untitled and not str(item.get("title", "") or "").strip():
            return False
        return True

    def _window_is_presented(handle: int, *, min_width: int = 96, min_height: int = 64) -> bool:
        """Verify that an HWND is a real on-screen window, not a hidden tray helper."""
        if os.name != "nt" or not handle:
            return False
        try:
            user32 = ctypes.windll.user32
            if not bool(user32.IsWindow(handle)) or not bool(user32.IsWindowVisible(handle)):
                return False
            if bool(user32.IsIconic(handle)):
                return False

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            rect = RECT()
            if not bool(user32.GetWindowRect(handle, ctypes.byref(rect))):
                return False
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width < int(min_width) or height < int(min_height):
                return False

            # A visible HWND may still be DWM-cloaked or parked outside the
            # virtual desktop. Neither state satisfies the user's "show app"
            # command, so do not report success for it.
            try:
                DWMWA_CLOAKED = 14
                cloaked_value = ctypes.c_int(0)
                hr = int(ctypes.windll.dwmapi.DwmGetWindowAttribute(
                    handle, DWMWA_CLOAKED, ctypes.byref(cloaked_value), ctypes.sizeof(cloaked_value)
                ))
                if hr == 0 and bool(cloaked_value.value):
                    return False
            except Exception:
                pass

            SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
            SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
            vx = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
            vy = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
            vw = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
            vh = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
            if vw > 0 and vh > 0:
                ix = max(0, min(int(rect.right), vx + vw) - max(int(rect.left), vx))
                iy = max(0, min(int(rect.bottom), vy + vh) - max(int(rect.top), vy))
                if ix < 24 or iy < 24:
                    return False
            return True
        except Exception:
            return False

    def _activate_hwnd(handle: int) -> bool:
        if os.name != "nt" or not handle:
            return False
        try:
            user32 = ctypes.windll.user32
            SW_RESTORE = 9
            SW_SHOW = 5
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_SHOWWINDOW = 0x0040
            HWND_TOP = 0
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            GA_ROOT = 2

            # Child/owned HWNDs sometimes leak into activation paths through app
            # frameworks. Always operate on the root top-level window if Windows
            # can resolve one.
            try:
                root = int(user32.GetAncestor(handle, GA_ROOT) or 0)
                if root:
                    handle = root
            except Exception:
                pass

            user32.ShowWindowAsync(handle, SW_RESTORE)
            user32.ShowWindow(handle, SW_SHOW)
            user32.SetWindowPos(handle, HWND_TOP, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.BringWindowToTop(handle)
            user32.SetForegroundWindow(handle)
            for _ in range(3):
                if int(user32.GetForegroundWindow()) == int(handle) and _window_is_presented(handle):
                    return True
                time.sleep(0.05)
                user32.BringWindowToTop(handle)
                user32.SetForegroundWindow(handle)

            # Windows' foreground-lock policy can reject SetForegroundWindow even
            # after a legitimate voice command. Temporarily attach our input queue
            # to the current foreground thread, which is the documented Win32
            # mechanism used by desktop applications that need to activate an
            # already-existing window in response to user intent.
            try:
                current_tid = int(ctypes.windll.kernel32.GetCurrentThreadId() or 0)
                fg = int(user32.GetForegroundWindow() or 0)
                fg_pid = ctypes.c_ulong(0)
                fg_tid = int(user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid)) or 0) if fg else 0
                target_pid = ctypes.c_ulong(0)
                target_tid = int(user32.GetWindowThreadProcessId(handle, ctypes.byref(target_pid)) or 0)
                attached_fg = False
                attached_target = False
                if current_tid and fg_tid and current_tid != fg_tid:
                    attached_fg = bool(user32.AttachThreadInput(current_tid, fg_tid, True))
                if current_tid and target_tid and current_tid != target_tid:
                    attached_target = bool(user32.AttachThreadInput(current_tid, target_tid, True))
                try:
                    user32.ShowWindowAsync(handle, SW_RESTORE)
                    user32.BringWindowToTop(handle)
                    user32.SetForegroundWindow(handle)
                    user32.SetActiveWindow(handle)
                    user32.SetFocus(handle)
                finally:
                    if attached_target:
                        user32.AttachThreadInput(current_tid, target_tid, False)
                    if attached_fg:
                        user32.AttachThreadInput(current_tid, fg_tid, False)
            except Exception:
                pass

            if int(user32.GetForegroundWindow() or 0) == int(handle) and _window_is_presented(handle):
                return True
            # Explicit voice command is user intent; the short ALT pulse mirrors
            # the previous implementation but without constructing a UIA wrapper.
            VK_MENU = 0x12
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.SetWindowPos(handle, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.SetForegroundWindow(handle)
            user32.SetWindowPos(handle, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.06)
            if int(user32.GetForegroundWindow() or 0) == int(handle) and _window_is_presented(handle):
                return True

            # Appearing on screen is the actual post-condition of "open/show the
            # app". Some Windows configurations still forbid stealing keyboard
            # focus, even though ShowWindow/SetWindowPos successfully restored the
            # tray-hidden window. Accept that visible, non-minimized result instead
            # of falsely reporting failure.
            return _window_is_presented(handle)
        except Exception:
            return False

    def _process_snapshot() -> dict[int, dict[str, str]]:
        out: dict[int, dict[str, str]] = {}
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                pid = int(proc.info.get("pid") or proc.pid)
                out[pid] = {
                    "name": str(proc.info.get("name") or ""),
                    "exe": str(proc.info.get("exe") or ""),
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
                continue
        return out

    def _focus_process_window(pid: int, *, include_hidden: bool = False) -> bool:
        if os.name != "nt" or not pid:
            return False
        windows = [
            item for item in _win32_top_level_windows(include_hidden=include_hidden)
            if int(item.get("pid", 0) or 0) == int(pid)
            and _window_item_is_user_facing(item, allow_hidden=include_hidden)
        ]
        # Prefer a visible, unowned normal window. A hidden normal window is the
        # expected shape of an app that was closed/minimized to the notification
        # area; _activate_hwnd will SHOW+RESTORE it before focusing.
        windows.sort(
            key=lambda item: (
                bool(item.get("visible", False)),
                not bool(item.get("tool_window", False)),
                not bool(item.get("owner", 0)),
                len(str(item.get("title", "") or "")),
            ),
            reverse=True,
        )
        for item in windows:
            if bool(item.get("tool_window", False)):
                continue
            if _activate_hwnd(int(item.get("hwnd", 0) or 0)):
                return True
        return False

    def _focus_application_window(
        label: str, query: str = "", executable: str = "", *, min_score: float = 0.72,
        include_hidden: bool = False,
    ) -> tuple[bool, dict[str, Any]]:
        """Focus a user-facing app window with bounded Win32 enumeration.

        Unlike the v3.7 implementation this never calls ``Desktop(...).windows()``
        or ``descendants()``.  That removes the unbounded UIA wait that could leave
        an otherwise trivial "open Steam" command spinning for minutes.
        """
        if os.name != "nt":
            return False, {}
        wanted_exe = ""
        wanted_parent = ""
        try:
            if executable:
                wanted_exe = os.path.normcase(os.path.abspath(executable))
                wanted_parent = os.path.normcase(os.path.dirname(wanted_exe))
        except Exception:
            pass

        proc_map = _process_snapshot()
        ranked: list[tuple[float, dict[str, Any]]] = []
        for item in _win32_top_level_windows(include_hidden=include_hidden):
            if not _window_item_is_user_facing(item, allow_hidden=include_hidden):
                continue
            pid = int(item.get("pid", 0) or 0)
            title = str(item.get("title", "") or "")
            pinfo = proc_map.get(pid, {})
            proc_name = str(pinfo.get("name", "") or "")
            proc_exe = str(pinfo.get("exe", "") or "")
            proc_stem = Path(proc_name).stem if proc_name else ""
            score = max(
                target_similarity(label, title),
                target_similarity(query, title),
                target_similarity(label, proc_stem),
                target_similarity(query, proc_stem),
            )
            if wanted_exe and proc_exe:
                try:
                    norm_proc = os.path.normcase(os.path.abspath(proc_exe))
                    if norm_proc == wanted_exe:
                        score = max(score, 1.0)
                    elif wanted_parent and (
                        norm_proc.startswith(wanted_parent.rstrip("\\/") + os.sep)
                        or os.path.dirname(norm_proc) == wanted_parent
                    ):
                        score = max(score, 0.93)
                except Exception:
                    pass
            # Hidden tool/owned popups are usually tray menus, tooltip windows or
            # Chromium helpers rather than the user-facing app. Only restore normal
            # top-level candidates.
            if include_hidden and not bool(item.get("visible", False)):
                if bool(item.get("tool_window", False)) or bool(item.get("owner", 0)):
                    continue
            if score >= float(min_score):
                ranked.append((score, {**item, "process_name": proc_name, "exe": proc_exe}))

        ranked.sort(
            key=lambda pair: (
                bool(pair[1].get("visible", False)),
                not bool(pair[1].get("tool_window", False)),
                not bool(pair[1].get("owner", 0)),
                pair[0],
            ),
            reverse=True,
        )
        for score, item in ranked[:8]:
            if _activate_hwnd(int(item.get("hwnd", 0) or 0)):
                info = dict(item)
                info["score"] = round(float(score), 3)
                return True, info
        return False, {}

    def _restore_running_application_window(
        executable: str, label: str, query: str, pid: int = 0, *, relaunch: bool = True,
        timeout: float = 2.8,
    ) -> tuple[bool, dict[str, Any]]:
        """Bring a tray-hidden app back without claiming success prematurely.

        Strategy: visible HWND -> hidden HWND in the exact process -> hidden
        semantic window (important for Electron helper processes) -> one normal
        ShellExecute-style re-open -> bounded verification. This mirrors what a
        user does by clicking an app icon while it is already resident in tray.
        """
        info: dict[str, Any] = {}
        if pid and _focus_process_window(pid):
            return True, {"pid": pid, "method": "visible_pid"}
        focused, info = _focus_application_window(label, query, executable, min_score=0.68)
        if focused:
            info["method"] = "visible_semantic"
            return True, info

        if pid and _focus_process_window(pid, include_hidden=True):
            return True, {"pid": pid, "method": "hidden_pid_restored"}
        focused, info = _focus_application_window(
            label, query, executable, min_score=0.66, include_hidden=True
        )
        if focused:
            info["method"] = "hidden_semantic_restored"
            return True, info

        if not relaunch or os.name != "nt":
            return False, info

        logger.info("Application tray restore: re-opening %r to request its main window", executable)
        try:
            # os.startfile uses ShellExecute, which is the normal Windows activation
            # path and is more appropriate for single-instance GUI/tray apps than
            # spawning their executable as a console-style child process.
            os.startfile(executable)  # type: ignore[attr-defined]
        except Exception:
            try:
                subprocess.Popen(
                    [executable],
                    cwd=str(Path(executable).parent) if Path(executable).is_absolute() else None,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                )
            except Exception as exc:
                logger.warning("Application tray restore relaunch failed for %r: %s", executable, exc)
                return False, info

        deadline = time.monotonic() + max(0.8, float(timeout))
        while time.monotonic() < deadline:
            if pid and _focus_process_window(pid, include_hidden=True):
                return True, {"pid": pid, "method": "relaunch_pid"}
            focused, info = _focus_application_window(
                label, query, executable, min_score=0.64, include_hidden=True
            )
            if focused:
                info["method"] = "relaunch_semantic"
                return True, info
            time.sleep(0.10)
        return False, info

    def _steam_main_window_candidates(executable: str, *, include_hidden: bool = True) -> list[dict[str, Any]]:
        """Return only real Steam client windows, never steam.exe message HWNDs.

        Steam's tray resident process can own hidden/untitled helper windows. HOTFIX52
        treated one of those as a restored UI because ShowWindow made the HWND
        technically visible. The actual client UI is normally hosted by
        steamwebhelper/Chromium, so rank those meaningful top-level windows by
        geometry/title/class instead of trusting the bootstrap steam.exe PID.
        """
        if os.name != "nt":
            return []
        wanted_root = ""
        try:
            wanted_root = os.path.normcase(os.path.abspath(str(Path(executable).parent)))
        except Exception:
            pass
        proc_map = _process_snapshot()
        ranked: list[tuple[tuple[int, int, int, int, int], dict[str, Any]]] = []
        for item in _win32_top_level_windows(include_hidden=include_hidden):
            if not _window_item_is_user_facing(
                item, allow_hidden=include_hidden, min_width=260, min_height=150, allow_untitled=True
            ):
                continue
            pid = int(item.get("pid", 0) or 0)
            pinfo = proc_map.get(pid, {})
            proc_name = str(pinfo.get("name", "") or "")
            proc_exe = str(pinfo.get("exe", "") or "")
            stem = Path(proc_name).stem.casefold() if proc_name else ""
            proc_in_steam_root = False
            if wanted_root and proc_exe:
                try:
                    norm_proc = os.path.normcase(os.path.abspath(proc_exe))
                    proc_in_steam_root = norm_proc == os.path.normcase(os.path.abspath(executable)) or norm_proc.startswith(wanted_root.rstrip("\\/") + os.sep)
                except Exception:
                    pass
            if stem not in {"steam", "steamwebhelper"}:
                continue
            if wanted_root and proc_exe and not proc_in_steam_root:
                continue

            title = str(item.get("title", "") or "").strip()
            title_cf = title.casefold()
            cls = str(item.get("class_name", "") or "").strip()
            cls_cf = cls.casefold()
            chrome_widget = cls_cf.startswith("chrome_widgetwin")
            exact_steam_title = title_cf == "steam"
            steam_title = "steam" in title_cf
            # Untitled steam.exe HWNDs are message/bootstrap windows, not the
            # client surface. Untitled Chromium windows are accepted only as a
            # last-resort candidate because the hidden main Steam UI can briefly
            # lose its title while being recreated.
            if not title and not (stem == "steamwebhelper" and chrome_widget):
                continue
            if title and not (steam_title or stem == "steamwebhelper" or chrome_widget):
                continue
            area = int(item.get("width", 0) or 0) * int(item.get("height", 0) or 0)
            rank = (
                1 if bool(item.get("visible", False)) else 0,
                1 if exact_steam_title else 0,
                1 if steam_title else 0,
                1 if chrome_widget else 0,
                area,
            )
            ranked.append((rank, {**item, "process_name": proc_name, "exe": proc_exe}))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _rank, item in ranked[:10]]

    def _focus_steam_main_window(executable: str, *, include_hidden: bool = True) -> tuple[bool, dict[str, Any]]:
        candidates = _steam_main_window_candidates(executable, include_hidden=include_hidden)
        for item in candidates:
            hwnd = int(item.get("hwnd", 0) or 0)
            if hwnd and _activate_hwnd(hwnd) and _window_is_presented(hwnd, min_width=260, min_height=150):
                info = dict(item)
                info["verified_presented"] = True
                return True, info
        return False, {"candidate_count": len(candidates), "candidates": [
            {k: item.get(k) for k in ("pid", "title", "class_name", "visible", "width", "height")}
            for item in candidates[:4]
        ]}

    def _restore_steam_main_window(executable: str, label: str, query: str, pid: int = 0) -> tuple[bool, dict[str, Any]]:
        """Restore the *real* Steam client window and verify it is on-screen."""
        focused, info = _focus_steam_main_window(executable, include_hidden=True)
        if focused:
            info["method"] = "steam_existing_client_window"
            return True, info
        if os.name != "nt":
            return False, info

        # Give the Steam process permission to foreground its newly-created client
        # window when Windows allows our current process to grant it. Failure is
        # harmless; our own verified activation path still runs afterwards.
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY
        except Exception:
            pass

        last_info = info
        for uri in ("steam://open/main", "steam://open/games"):
            logger.info("Steam tray restore: requesting real client window via %s", uri)
            try:
                os.startfile(uri)  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning("Steam tray restore URI failed %s: %s", uri, exc)
                continue
            deadline = time.monotonic() + 2.6
            while time.monotonic() < deadline:
                focused, last_info = _focus_steam_main_window(executable, include_hidden=True)
                if focused:
                    last_info["method"] = "steam_uri_verified"
                    last_info["uri"] = uri
                    logger.info(
                        "Steam tray restore verified hwnd=%s title=%r class=%r size=%sx%s via %s",
                        last_info.get("hwnd"), last_info.get("title"), last_info.get("class_name"),
                        last_info.get("width"), last_info.get("height"), uri,
                    )
                    return True, last_info
                time.sleep(0.12)

        # Final single-instance activation mirrors clicking Steam's Start/taskbar
        # entry, but success is still withheld until a meaningful client HWND is
        # visible and intersects the virtual desktop.
        try:
            logger.info("Steam tray restore: final ShellExecute activation %r", executable)
            os.startfile(executable)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("Steam final activation failed for %r: %s", executable, exc)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            focused, last_info = _focus_steam_main_window(executable, include_hidden=True)
            if focused:
                last_info["method"] = "steam_executable_verified"
                return True, last_info
            time.sleep(0.12)

        logger.warning("Steam tray restore verification failed: %s", last_info)
        return False, last_info

    def _verify_executable_launch(executable: str | Path, *, timeout: float = 4.5) -> tuple[bool, int]:
        deadline = time.monotonic() + max(0.5, float(timeout))
        while time.monotonic() < deadline:
            running, pid = _process_running_from_executable(executable)
            if running:
                return True, pid
            time.sleep(0.12)
        return False, 0

    def _verify_foreground_application(label: str, query: str = "", *, timeout: float = 2.5) -> tuple[bool, dict[str, Any]]:
        """Verify launcher-style/UWP apps by the window that actually became active.

        Some commands (notably Calculator and launcher stubs) spawn a different
        child executable and immediately exit, so exact PID/exe verification alone
        would create a false failure. We accept the foreground window only when its
        title/process is a strong semantic match to the requested app.
        """
        deadline = time.monotonic() + max(0.5, float(timeout))
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                ctx = inspect_foreground_context([]).as_dict()
            except Exception:
                ctx = {}
            last = ctx if isinstance(ctx, dict) else {}
            title = str(last.get("title", "") or "")
            process = str(last.get("process_name", "") or "")
            process_stem = Path(process).stem if process else ""
            score = max(
                target_similarity(label, title),
                target_similarity(query, title),
                target_similarity(label, process_stem),
                target_similarity(query, process_stem),
            )
            if score >= 0.76:
                last = dict(last)
                last["verification_score"] = round(score, 3)
                return True, last
            time.sleep(0.12)
        return False, last

    def _verify_process_under_folder(folder: Path, *, timeout: float = 8.0) -> tuple[bool, str, int]:
        try:
            root = os.path.normcase(os.path.abspath(str(folder)))
        except Exception:
            root = os.path.normcase(str(folder))
        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            for proc in psutil.process_iter(["pid", "name", "exe"]):
                try:
                    exe = str(proc.info.get("exe") or "")
                    if not exe:
                        continue
                    normalized = os.path.normcase(os.path.abspath(exe))
                    if normalized == root or normalized.startswith(root.rstrip("\\/") + os.sep):
                        return True, exe, int(proc.info.get("pid") or proc.pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    continue
            time.sleep(0.16)
        return False, "", 0

    def _launch_shortcut(path: Path) -> None:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            _open_path(path)

    def _candidate_options(candidates) -> list[dict[str, Any]]:
        result = []
        for item in candidates[:4]:
            result.append({"label": item.label, "kind": item.kind, "value": item.value, "score": round(float(item.score), 3)})
        return result

    def _game_launch_wait_seconds() -> float:
        # Games with anti-cheat/bootstrap launchers routinely need tens of seconds
        # before the real executable exists. Keep a long asynchronous watcher but
        # do not freeze the voice command for the whole interval.
        try:
            return max(30.0, min(600.0, float(config.get("gaming.launch_wait_seconds", 240) or 240)))
        except Exception:
            return 240.0

    def _start_game_launch_watcher(
        query: str, label: str, *, install_dir: Path | None = None, executable: Path | None = None
    ) -> None:
        watcher_started_wall = time.time()
        from jarvis.gaming.runtime import current_launch_token
        token = current_launch_token()

        def worker() -> None:
            deadline = time.monotonic() + _game_launch_wait_seconds()
            manager = GameManager(enabled=True, config=config)
            expected = manager.adapter_for_query(label or query)
            while time.monotonic() < deadline:
                if current_launch_token() != token:
                    return          # a newer launch superseded this watcher
                try:
                    if expected is not None:
                        found = manager.wait_for_game(label or query, timeout=0.01)
                        if found is not None:
                            adapter, proc = found
                            update_game_launch(token=token,
                                status="running", verified=True, game_id=adapter.game_id,
                                label=adapter.display_name, pid=proc.pid, executable=proc.exe,
                                process_name=proc.name, detected_at=time.time(),
                            )
                            return
                    elif executable is not None:
                        running, pid = _process_running_from_executable(executable)
                        if running:
                            update_game_launch(token=token, status="running", verified=True, pid=pid, executable=str(executable), detected_at=time.time())
                            return
                    elif install_dir is not None:
                        running, exe, pid = _verify_process_under_folder(install_dir, timeout=0.05)
                        if running:
                            update_game_launch(token=token, status="running", verified=True, pid=pid, executable=exe, process_name=Path(exe).name if exe else "", detected_at=time.time())
                            return
                    else:
                        # Shortcuts/StartApps sometimes hide their real executable.
                        # In that case accept only a newly-created, strongly name-matched
                        # process after the launch request; never reuse an old unrelated PID.
                        for score, proc, info in _process_matches(label or query, threshold=0.74):
                            try:
                                created = float(info.get("create_time") or proc.create_time())
                            except (psutil.Error, OSError, ValueError, TypeError):
                                continue
                            if created + 2.0 < watcher_started_wall:
                                continue
                            exe = str(info.get("exe") or "")
                            update_game_launch(token=token,
                                status="running", verified=True, pid=int(info.get("pid") or proc.pid),
                                executable=exe, process_name=str(info.get("name") or ""),
                                detected_at=time.time(), verification="new_process_name_match", score=round(float(score), 3),
                            )
                            return
                except Exception:
                    logger.debug("Game launch watcher iteration failed", exc_info=True)
                time.sleep(0.35)
            update_game_launch(token=token, status="timeout", verified=False, timed_out_at=time.time())

        threading.Thread(target=worker, name="jarvis-game-launch-watch", daemon=True).start()

    def _initial_supported_game_wait(query: str, label: str, timeout: float = 4.5):
        manager = GameManager(enabled=True, config=config)
        if manager.adapter_for_query(label or query) is None:
            return None
        return manager.wait_for_game(label or query, timeout=timeout)

    def application(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "capture") or "capture").casefold().strip()
        name = str(args.get("name", "")).strip()
        aliases = {
            "проводник": "explorer.exe",
            "explorer": "explorer.exe",
            "блокнот": "notepad.exe",
            "notepad": "notepad.exe",
            "диспетчер задач": "taskmgr.exe",
            "калькулятор": "calc.exe",
            "cmd": "cmd.exe",
            "powershell": "powershell.exe",
            "терминал": "wt.exe",
        }
        if action == "open":
            if not name:
                raise ValueError("Не задано имя приложения")
            requested_kind = str(args.get("kind", "app") or "app").casefold()
            if requested_kind not in {"app", "game"}:
                requested_kind = "app"
            # Object words are semantic type markers, not part of the application
            # name. This is deliberately stricter than the generic desktop resolver.
            query = re.sub(
                r"^(?:приложен\w*|программ\w*|app|application|program|игр\w*|game)\s+",
                "", name, flags=re.IGNORECASE,
            ).strip(' "«»')
            if not query:
                raise ValueError("Не услышал название приложения или игры")
            # NEO is already this process. Never resolve its name through
            # stale shortcuts or another extracted HOTFIX path; restore the
            # existing tray-hidden window instead.
            jarvis_names = {
                "jarvis", "neo jarvis", "neojarvis", "джарвис", "нео джарвис",
                "жарвис", "джервис", "jervis", "jarvi",
            }
            q_self = re.sub(r"[^0-9a-zа-яё]+", " ", query.casefold(), flags=re.IGNORECASE).strip()
            if requested_kind == "app" and q_self in jarvis_names:
                from jarvis.runtime.ui_commands import request_show_main_window
                request_show_main_window()
                return ToolExecution(
                    True, "Открыл уже запущенное окно NEO.",
                    {"kind": "existing_instance", "verified": True, "restored_from_tray": True},
                )
            # User-maintained recognition dictionary has higher priority than fuzzy
            # discovery, but only for an explicit app/folder entry with an existing
            # path.  This prevents an alias from hijacking unrelated commands.
            personal_entries = config.get("personal_dictionary.entries", []) or []
            # HIST-045: a choice the user just confirmed in NEO's own menu is not
            # replaced by a dictionary entry for the same words.
            chosen188 = SELECTED_APP_CANDIDATE185.get() or {}
            if (str(chosen188.get("label", "") or "").strip().casefold() == str(query or "").strip().casefold()
                    and str(chosen188.get("value", "") or "").strip()):
                personal_entries = []
            qn = _normalized_app_name(query)
            for entry in personal_entries:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("kind", "app") or "app").casefold() not in {"app", "game"}:
                    continue
                keys = [str(entry.get("name", "") or ""), *[str(x) for x in (entry.get("aliases", []) or [])]]
                if qn not in {_normalized_app_name(x) for x in keys if str(x).strip()}:
                    continue
                explicit_path = Path(str(entry.get("path", "") or "")).expanduser()
                if explicit_path.is_file():
                    if os.name == "nt":
                        os.startfile(str(explicit_path))  # type: ignore[attr-defined]
                    else:
                        subprocess.Popen([str(explicit_path)])
                    return ToolExecution(True, f"Запущено по персональному словарю: {entry.get('name') or query}", {"kind": "personal_dictionary", "path": str(explicit_path), "verified": True})
            if os.name == "nt" and query.casefold() in {"корзина", "recycle bin", "recyclebin"}:
                # Recycle Bin is a Windows shell folder, not an application. Keep
                # backward compatibility only when no app/game type was specified.
                if requested_kind == "app" and not re.search(r"^(?:прилож|программ|app|application)", name, re.I):
                    subprocess.Popen(["explorer.exe", "shell:RecycleBinFolder"])
                    return ToolExecution(True, "Открыта корзина Windows", {"kind": "shell_folder", "verified": True})

            # Game stores are authoritative and must be considered before any fuzzy
            # folder search. A request for a game therefore cannot open its install
            # directory by accident.
            if requested_kind == "game":
                steam_game = _find_steam_game(query)
                if steam_game and os.name == "nt":
                    appid, game_name, install_dir, score = steam_game
                    if score < 0.74:
                        return ToolExecution(False, f"Я нашёл в Steam «{game_name}», но совпадение с «{query}» неуверенное. Я правильно понял название?", {"requires_clarification": True, "candidate": game_name, "score": score})
                    record_game_launch(
                        query=query, label=game_name, store="steam", appid=appid,
                        install_dir=str(install_dir), status="launch_requested", verified=False,
                    )
                    os.startfile(f"steam://rungameid/{appid}")  # type: ignore[attr-defined]
                    update_game_launch(status="starting", command_sent_at=time.time())
                    found = _initial_supported_game_wait(query, game_name, timeout=float(config.get("gaming.initial_launch_wait_seconds", 8) or 8))
                    if found is not None:
                        adapter, proc = found
                        update_game_launch(status="running", verified=True, game_id=adapter.game_id, pid=proc.pid, executable=proc.exe, process_name=proc.name, detected_at=time.time())
                        _focus_process_window(proc.pid)
                        return ToolExecution(True, f"Запустил игру {game_name} и подтвердил реальный процесс {proc.name}.", {"kind": "game", "store": "steam", "appid": appid, "label": game_name, "executable": proc.exe, "pid": proc.pid, "verified": True, "score": score})
                    _start_game_launch_watcher(query, game_name, install_dir=install_dir)
                    return ToolExecution(
                        True,
                        f"Передал Steam запуск «{game_name}». Игра ещё загружается; NEO продолжит искать её настоящий процесс до {int(_game_launch_wait_seconds())} секунд и запомнил, какую игру вы запустили.",
                        {"kind": "game", "store": "steam", "appid": appid, "label": game_name, "verified": False, "launch_pending": True, "score": score},
                    )

                epic_game = _find_epic_game(query)
                if epic_game:
                    game_name, exe, score = epic_game
                    if score < 0.74:
                        return ToolExecution(False, f"Я нашёл Epic-игру «{game_name}», но не уверен, что вы имели в виду её. Я правильно понял?", {"requires_clarification": True, "candidate": game_name, "score": score})
                    running, pid = _process_running_from_executable(exe)
                    if running:
                        _focus_process_window(pid)
                        return ToolExecution(True, f"Игра уже запущена; переключился на {game_name}.", {"kind": "game", "store": "epic", "executable": str(exe), "pid": pid, "verified": True, "already_running": True})
                    record_game_launch(query=query, label=game_name, store="epic", executable=str(exe), status="launch_requested", verified=False)
                    subprocess.Popen([str(exe)], cwd=str(exe.parent))
                    update_game_launch(status="starting", command_sent_at=time.time())
                    found = _initial_supported_game_wait(query, game_name, timeout=float(config.get("gaming.initial_launch_wait_seconds", 8) or 8))
                    if found is not None:
                        adapter, proc = found
                        update_game_launch(status="running", verified=True, game_id=adapter.game_id, pid=proc.pid, executable=proc.exe, process_name=proc.name, detected_at=time.time())
                        _focus_process_window(proc.pid)
                        return ToolExecution(True, f"Запустил игру {game_name}.", {"kind": "game", "store": "epic", "executable": proc.exe, "pid": proc.pid, "verified": True})
                    _start_game_launch_watcher(query, game_name, executable=exe)
                    return ToolExecution(True, f"Запуск {game_name} отправлен. Игра ещё стартует; NEO продолжит отслеживать её процесс до {int(_game_launch_wait_seconds())} секунд.", {"kind": "game", "store": "epic", "executable": str(exe), "verified": False, "launch_pending": True})

            # NB35: the candidate the user picked from NEO's own menu is passed by
            # the Agent out of band; tool arguments (model-visible) cannot carry it.
            selected185 = SELECTED_APP_CANDIDATE185.get() or {}
            if str(selected185.get("label", "") or "").strip().casefold() != str(query or "").strip().casefold():
                selected185 = {}
            forced_kind = str(selected185.get("kind", "") or "").strip().casefold()
            forced_value = str(selected185.get("value", "") or "").strip()
            forced_label = str(selected185.get("label", "") or "").strip() or query
            forced_score = float(selected185.get("score", 1.0) or 1.0)
            forced_candidate = bool(forced_value and forced_kind in {"app", "shortcut", "shell"})
            resolve_started = time.monotonic()
            from jarvis.runtime.cancellation import cancellation_manager
            launch_origin188 = cancellation_manager().cancel_count()

            def launch_cancelled188(label: str) -> ToolExecution | None:
                # HIST-038: a «стоп» during the resolver never lets the old request
                # reach its first native launch (an already started launch is not undone).
                if not cancellation_manager().cancelled_since(launch_origin188):
                    return None
                return ToolExecution(False, f"Запуск отменён: «{label}» не открыт.",
                                     {"kind": requested_kind, "query": query, "cancelled": True, "verified": False})
            logger.info("Application launch: resolving %r kind=%s forced=%s", query, requested_kind, forced_candidate)
            if forced_candidate:
                candidates = [ResolvedTarget(forced_kind, forced_value, forced_label, forced_score)]
            else:
                candidates = application_candidates(query, limit=5)
            logger.info(
                "Application launch candidates for %r resolved in %.2fs: %s",
                query, time.monotonic() - resolve_started,
                [(item.label, item.kind, round(float(item.score), 3), str(item.value)) for item in candidates],
            )
            if not candidates:
                # HOTFIX16: voice routing can still occasionally classify a bare
                # game title as a generic application (for example «запусти
                # DarkTide»). Before reporting failure, consult the authoritative
                # Steam/Epic game indexes and, on a strong match, rerun the exact
                # same launcher through the verified game path. This cannot recurse
                # because the retry uses kind=game.
                if requested_kind == "app" and os.name == "nt":
                    steam_probe = _find_steam_game(query)
                    epic_probe = _find_epic_game(query)
                    steam_score = float(steam_probe[3]) if steam_probe else 0.0
                    epic_score = float(epic_probe[2]) if epic_probe else 0.0
                    if max(steam_score, epic_score) >= 0.82:
                        logger.info(
                            "Application launch fallback promoted %r app->game (steam=%.3f epic=%.3f)",
                            query, steam_score, epic_score,
                        )
                        retry = dict(args)
                        retry["kind"] = "game"
                        retry["name"] = query
                        return application(retry)
                return ToolExecution(False, f"Не удалось найти исполняемое приложение для «{query}». Я не открывал похожие папки или файлы.", {"kind": requested_kind, "query": query, "verified": False, "app_not_found": True})

            top = candidates[0]
            cancelled188 = launch_cancelled188(top.label)
            if cancelled188 is not None:
                return cancelled188
            second = candidates[1] if len(candidates) > 1 else None
            # Low-confidence and near-tie results are not executed blindly. The
            # assistant must ask the user instead of opening a random application.
            top_exact = application_candidate_is_exact(query, top)
            near_tie = bool(
                second
                and second.label.casefold() != top.label.casefold()
                and top.score - second.score < 0.045
                and second.score >= 0.72
                and not top_exact
            )
            if not forced_candidate and (top.score < 0.74 or near_tie):
                labels = ", ".join(f"«{item.label}»" for item in candidates[:3])
                return ToolExecution(
                    False,
                    f"Я не хочу угадывать. По запросу «{query}» наиболее похожи: {labels}. Какой вариант открыть?",
                    {"requires_clarification": True, "query": query, "options": _candidate_options(candidates), "verified": False},
                )

            # A StartApps entry is launched through the Windows AppsFolder. A .lnk
            # is launched as a shortcut. Neither branch can degrade into opening a
            # directory because application_candidates is type-locked.
            if top.kind == "app":
                executable = str(top.value)
                running, pid = _process_running_from_executable(executable)
                if running:
                    steam_identity = normalize_target(query) == "steam" or normalize_target(top.label) == "steam" or Path(executable).stem.casefold() == "steam"
                    if steam_identity:
                        focused, focus_info = _restore_steam_main_window(executable, top.label, query, pid)
                    else:
                        focused, focus_info = _restore_running_application_window(
                            executable, top.label, query, pid, relaunch=True, timeout=2.8
                        )

                    # A resident process is not the requested post-condition. The
                    # voice command means "show/open the application", so success is
                    # reported only after a real top-level window was restored.
                    if not focused:
                        return ToolExecution(
                            False,
                            f"{top.label} уже работает в фоне/трее, но его видимое окно не удалось восстановить. Я не буду сообщать, что приложение открыто, пока окно не подтверждено.",
                            {
                                "kind": requested_kind, "label": top.label, "executable": executable,
                                "pid": pid, "verified": False, "already_running": True,
                                "focused": False, "focus_window": focus_info, "score": top.score,
                            },
                        )

                    return ToolExecution(
                        True,
                        f"{top.label} уже было запущено; восстановил окно из фона/трея и вывел его на экран.",
                        {
                            "kind": requested_kind, "label": top.label, "executable": executable,
                            "pid": pid, "verified": True, "already_running": True,
                            "focused": True, "focus_window": focus_info, "score": top.score,
                        },
                    )
                cancelled188 = launch_cancelled188(top.label)
                if cancelled188 is not None:
                    return cancelled188
                try:
                    if requested_kind == "game":
                        record_game_launch(query=query, label=top.label, store="executable", executable=executable, status="launch_requested", verified=False)
                    subprocess.Popen([executable], cwd=str(Path(executable).parent) if Path(executable).is_absolute() else None)
                    if requested_kind == "game":
                        update_game_launch(status="starting", command_sent_at=time.time())
                except Exception as exc:
                    return ToolExecution(False, f"Нашёл исполняемый файл {executable}, но Windows не смогла его запустить: {exc}", {"kind": requested_kind, "label": top.label, "executable": executable, "verified": False, "score": top.score})
                verified, pid = _verify_executable_launch(executable)
                if verified:
                    focused = _focus_process_window(pid)
                    focus_info: dict[str, Any] = {}
                    if not focused:
                        focused, focus_info = _focus_application_window(top.label, query, executable)
                    if requested_kind == "game":
                        update_game_launch(status="running", verified=True, pid=pid, executable=executable, process_name=Path(executable).name, detected_at=time.time())
                    return ToolExecution(True, f"Запустил {top.label}.", {"kind": requested_kind, "label": top.label, "executable": executable, "pid": pid, "verified": True, "focused": focused, "focus_window": focus_info, "score": top.score})
                foreground_ok, fg = _verify_foreground_application(top.label, query, timeout=2.0)
                if foreground_ok:
                    if requested_kind == "game":
                        fg_pid = int((fg or {}).get("pid", 0) or 0) if isinstance(fg, dict) else 0
                        update_game_launch(status="running", verified=True, pid=fg_pid or None, executable=executable, process_name=str((fg or {}).get("process_name", "") if isinstance(fg, dict) else ""), detected_at=time.time())
                    return ToolExecution(True, f"Запустил {top.label} и подтвердил открывшееся окно.", {"kind": requested_kind, "label": top.label, "executable": executable, "verified": True, "verification": "foreground", "foreground": fg, "score": top.score})
                if requested_kind == "game":
                    _start_game_launch_watcher(query, top.label, executable=Path(executable) if Path(executable).is_absolute() else None)
                    return ToolExecution(True, f"Запуск игры {top.label} отправлен. Её основной процесс ещё не появился; NEO продолжит отслеживать запуск в фоне.", {"kind": "game", "label": top.label, "executable": executable, "verified": False, "launch_pending": True, "score": top.score})
                return ToolExecution(False, f"Нашёл и запустил {executable}, но не смог подтвердить ни процесс, ни окно приложения. Я не буду утверждать, что {top.label} открылось. Это было нужное приложение?", {"kind": requested_kind, "label": top.label, "executable": executable, "verified": False, "score": top.score, "requires_confirmation_of_result": True})

            if top.kind == "shortcut":
                shortcut = Path(top.value)
                # If the shortcut target is already open, behave like clicking its
                # taskbar button: raise the existing user-facing window instead of
                # creating another instance.  Use a high semantic threshold here
                # because shortcut metadata does not always expose the target EXE.
                already_focused, focus_info = _focus_application_window(top.label, query, min_score=0.88, include_hidden=True)
                if already_focused:
                    return ToolExecution(True, f"{top.label} уже запущено; переключился на окно.", {"kind": requested_kind, "label": top.label, "shortcut": str(shortcut), "verified": True, "already_running": True, "focused": True, "focus_window": focus_info, "score": top.score})
                cancelled188 = launch_cancelled188(top.label)
                if cancelled188 is not None:
                    return cancelled188
                if requested_kind == "game":
                    record_game_launch(query=query, label=top.label, store="shortcut", shortcut=str(shortcut), status="launch_requested", verified=False)
                _launch_shortcut(shortcut)
                if requested_kind == "game":
                    update_game_launch(status="starting", command_sent_at=time.time())
                # Shortcut targets are not always exposed by Windows; verify by a
                # fuzzy process match rather than declaring success immediately.
                deadline = time.monotonic() + 5.0
                matched = None
                while time.monotonic() < deadline:
                    matches = _process_matches(top.label, threshold=0.62)
                    if matches:
                        matched = matches[0]
                        break
                    time.sleep(0.12)
                if matched:
                    pid = int(matched[2].get("pid") or matched[1].pid)
                    focused = _focus_process_window(pid, include_hidden=True)
                    focus_info: dict[str, Any] = {}
                    if not focused:
                        focused, focus_info = _focus_application_window(top.label, query, include_hidden=True)
                    if requested_kind == "game":
                        info = matched[2] if isinstance(matched[2], dict) else {}
                        update_game_launch(status="running", verified=True, pid=pid, executable=str(info.get("exe") or ""), process_name=str(info.get("name") or ""), detected_at=time.time())
                    if not focused and requested_kind != "game":
                        return ToolExecution(False, f"{top.label} работает, но после запуска ярлыка видимое окно не появилось.", {"kind": requested_kind, "label": top.label, "shortcut": str(shortcut), "pid": pid, "verified": False, "focused": False, "focus_window": focus_info, "score": top.score})
                    return ToolExecution(True, f"Запустил {top.label} через ярлык и вывел окно на экран." if focused else f"Запустил {top.label} через ярлык.", {"kind": requested_kind, "label": top.label, "shortcut": str(shortcut), "pid": pid, "verified": True, "focused": focused, "focus_window": focus_info, "score": top.score})
                if requested_kind == "game":
                    _start_game_launch_watcher(query, top.label)
                    return ToolExecution(True, f"Передал запуск игры «{top.label}» через ярлык. Основной процесс ещё не подтверждён; NEO продолжит искать его в фоне.", {"kind": "game", "label": top.label, "shortcut": str(shortcut), "verified": False, "launch_pending": True, "score": top.score})
                return ToolExecution(False, f"Открыл ярлык «{top.label}», но не смог подтвердить запуск приложения. Я правильно понял, что нужно было открыть именно его?", {"kind": requested_kind, "label": top.label, "shortcut": str(shortcut), "verified": False, "score": top.score})

            if top.kind == "shell":
                if os.name != "nt":
                    raise OSError("Windows StartApps доступны только в Windows")
                # StartApps entries (GOG Galaxy, Store/UWP apps, some classic apps)
                # may already have a window hosted by another process.  Surface it
                # first; only ask Explorer/AppsFolder to launch when no confident
                # existing window can be found.
                already_focused, focus_info = _focus_application_window(top.label, query, min_score=0.88, include_hidden=True)
                if already_focused:
                    return ToolExecution(True, f"{top.label} уже запущено; переключился на окно.", {"kind": requested_kind, "label": top.label, "appid": top.value, "verified": True, "already_running": True, "focused": True, "focus_window": focus_info, "score": top.score})
                cancelled188 = launch_cancelled188(top.label)
                if cancelled188 is not None:
                    return cancelled188
                if requested_kind == "game":
                    record_game_launch(query=query, label=top.label, store="startapps", appid=str(top.value), status="launch_requested", verified=False)
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{top.value}"])
                if requested_kind == "game":
                    update_game_launch(status="starting", command_sent_at=time.time())
                deadline = time.monotonic() + 5.0
                matched = None
                while time.monotonic() < deadline:
                    matches = _process_matches(top.label, threshold=0.60)
                    if matches:
                        matched = matches[0]
                        break
                    time.sleep(0.12)
                if matched:
                    pid = int(matched[2].get("pid") or matched[1].pid)
                    focused = _focus_process_window(pid, include_hidden=True)
                    focus_info: dict[str, Any] = {}
                    if not focused:
                        focused, focus_info = _focus_application_window(top.label, query, include_hidden=True)
                    if requested_kind == "game":
                        info = matched[2] if isinstance(matched[2], dict) else {}
                        update_game_launch(status="running", verified=True, pid=pid, executable=str(info.get("exe") or ""), process_name=str(info.get("name") or ""), detected_at=time.time())
                    if not focused and requested_kind != "game":
                        return ToolExecution(False, f"{top.label} запущено, но видимое окно не удалось восстановить/подтвердить.", {"kind": requested_kind, "label": top.label, "appid": top.value, "pid": pid, "verified": False, "focused": False, "focus_window": focus_info, "score": top.score})
                    return ToolExecution(True, f"Открыл приложение {top.label}." if not focused else f"Открыл приложение {top.label} и вывел его окно на экран.", {"kind": requested_kind, "label": top.label, "appid": top.value, "pid": pid, "verified": True, "focused": focused, "focus_window": focus_info, "score": top.score})
                focused, focus_info = _focus_application_window(top.label, query)
                if focused:
                    return ToolExecution(True, f"Открыл приложение {top.label} и вывел его окно на передний план.", {"kind": requested_kind, "label": top.label, "appid": top.value, "verified": True, "verification": "semantic_window", "focus_window": focus_info, "score": top.score})
                # Some UWP apps are hosted by generic processes, so verify the
                # newly foregrounded semantic window instead of trusting Explorer.
                foreground_ok, fg = _verify_foreground_application(top.label, query, timeout=2.0)
                if foreground_ok:
                    if requested_kind == "game":
                        fg_pid = int((fg or {}).get("pid", 0) or 0) if isinstance(fg, dict) else 0
                        update_game_launch(status="running", verified=True, pid=fg_pid or None, process_name=str((fg or {}).get("process_name", "") if isinstance(fg, dict) else ""), detected_at=time.time())
                    return ToolExecution(True, f"Открыл приложение {top.label}.", {"kind": requested_kind, "label": top.label, "appid": top.value, "verified": True, "verification": "foreground", "foreground": fg, "score": top.score})
                if requested_kind == "game":
                    _start_game_launch_watcher(query, top.label)
                    return ToolExecution(True, f"Windows получила команду запустить игру «{top.label}». Основной процесс ещё не подтверждён; NEO продолжит отслеживать запуск в фоне.", {"kind": "game", "label": top.label, "appid": top.value, "verified": False, "launch_pending": True, "score": top.score})
                return ToolExecution(False, f"Windows получила команду открыть «{top.label}», но я не смог подтвердить результат. Я правильно понял приложение? Если нет — выберите другой вариант.", {"kind": requested_kind, "label": top.label, "appid": top.value, "verified": False, "requires_confirmation_of_result": True, "score": top.score, "options": _candidate_options(candidates)})

            return ToolExecution(False, f"Кандидат «{top.label}» не является исполняемым приложением; я его не открывал.", {"kind": requested_kind, "candidate_kind": top.kind, "verified": False})

        if action in {"find", "info"}:
            if not name:
                raise ValueError("Не задано имя процесса или приложения")
            matches = _process_matches(name)
            if not matches:
                return ToolExecution(False, f"Процесс не найден: {name}", [])
            data = []
            for score, _proc, info in matches[:30]:
                item = {
                    "pid": int(info.get("pid") or 0),
                    "name": str(info.get("name") or ""),
                    "exe": str(info.get("exe") or ""),
                    "status": str(info.get("status") or ""),
                    "username": str(info.get("username") or ""),
                    "memory_percent": float(info.get("memory_percent") or 0.0),
                    "cpu_percent": float(info.get("cpu_percent") or 0.0),
                    "score": round(float(score), 3),
                }
                data.append(item)
            if action == "info":
                return ToolExecution(True, f"Процесс найден: {data[0]['name']} (PID {data[0]['pid']})", data[0])
            return ToolExecution(True, f"Найдено процессов: {len(data)}", data)

        if action == "kill":
            if not name:
                raise ValueError("Не задано имя процесса")
            matches = _process_matches(name, threshold=0.78)
            if not matches:
                return ToolExecution(False, f"Процесс не найден: {name}")
            best = matches[0][0]
            selected = [item for item in matches if item[0] >= max(0.86, best - 0.03)]
            killed: list[str] = []
            for _score, proc, info in selected:
                _assert_process_not_critical(proc, info)
                try:
                    proc.kill()
                    killed.append(f"{info.get('name') or proc.pid} ({proc.pid})")
                except psutil.NoSuchProcess:
                    killed.append(f"{info.get('name') or proc.pid} ({proc.pid})")
                except psutil.AccessDenied:
                    continue
            if not killed:
                return ToolExecution(False, f"Не удалось принудительно завершить процесс: {name}")
            _, alive = psutil.wait_procs([item[1] for item in selected], timeout=2.5)
            alive_pids = {p.pid for p in alive}
            if alive_pids:
                return ToolExecution(False, "Некоторые процессы всё ещё работают: " + ", ".join(str(x) for x in sorted(alive_pids)), {"killed": killed, "verified": False})
            return ToolExecution(True, "Принудительно завершено и проверено: " + ", ".join(killed), {"killed": killed, "verified": True})

        if action == "close":
            if not name:
                raise ValueError("Не задано имя приложения или процесса")
            human = re.sub(r"^(?:процесс|приложение|программу|программа|process|app)\s+", "", name, flags=re.IGNORECASE).strip()
            human_norm = _normalized_app_name(human)
            needles = {human_norm, Path(human_norm).stem}
            alias_exe = aliases.get(human.casefold())
            if alias_exe:
                needles.add(_normalized_app_name(alias_exe))
                needles.add(_normalized_app_name(Path(alias_exe).stem))
            try:
                resolved = resolve_basic_target(human, hint="app")
                if resolved is not None and resolved.kind == "app":
                    exe_name = Path(resolved.value).name
                    needles.add(_normalized_app_name(exe_name))
                    needles.add(_normalized_app_name(Path(exe_name).stem))
            except Exception:
                pass

            matches: list[tuple[float, psutil.Process, str]] = []
            own_pid = os.getpid()
            for proc in psutil.process_iter(["pid", "name", "exe"]):
                if int(proc.info.get("pid") or -1) == own_pid:
                    continue
                raw_name = str(proc.info.get("name") or "")
                proc_norm = _normalized_app_name(raw_name)
                stem_norm = _normalized_app_name(Path(raw_name).stem)
                if not proc_norm:
                    continue
                score = 0.0
                for needle in needles:
                    if not needle:
                        continue
                    if needle in {proc_norm, stem_norm}:
                        score = max(score, 1.0)
                    elif needle in proc_norm or proc_norm in needle or needle in stem_norm or stem_norm in needle:
                        score = max(score, 0.92)
                    else:
                        score = max(score, difflib.SequenceMatcher(None, needle, stem_norm).ratio())
                if score >= 0.74:
                    matches.append((score, proc, raw_name))
            if not matches:
                return ToolExecution(False, f"Процесс или приложение не найдено: {name}")
            best_score = max(item[0] for item in matches)
            # If we found an exact/near-exact app identity, close all of its helper
            # processes. Otherwise keep only candidates close to the best score.
            selected = [item for item in matches if item[0] >= max(0.82, best_score - 0.05)]
            from jarvis.runtime.graceful_close import request_close
            # A folder belongs to one Explorer window, never the shared shell.
            if any(raw.casefold()=="explorer.exe" for _,_,raw in selected):
                return ToolExecution(False,"Укажите конкретную папку или окно Проводника; весь процесс Explorer не закрывается.")
            result=request_close({proc.pid for _,proc,_ in selected})
            if not result['requested']:
                return ToolExecution(False,"Не найдено окно для штатного закрытия. Принудительное завершение — отдельная команда.",result)
            if result['pending']:
                return ToolExecution(False,"Команда закрытия передана. Приложение ещё открыто: возможно, требуется сохранить документ.",result)
            return ToolExecution(True,"Окна приложения закрыты: "+name,{**result,'verified':True})

        if action == "list_active":
            # User-facing applications are defined by real visible top-level UI,
            # not by every helper/background process. This naturally includes
            # games, Explorer folder windows and normal desktop applications.
            rows: list[dict[str, Any]] = []
            seen: set[tuple[int, str]] = set()
            if os.name == "nt":
                for item in _win32_top_level_windows(include_hidden=False):
                    if not _window_item_is_user_facing(item, allow_hidden=False, allow_untitled=False):
                        continue
                    pid = int(item.get("pid", 0) or 0)
                    title = str(item.get("title", "") or "").strip()
                    if not pid or not title:
                        continue
                    key = (pid, title.casefold())
                    if key in seen:
                        continue
                    seen.add(key)
                    proc_name = ""
                    exe = ""
                    try:
                        proc = psutil.Process(pid)
                        proc_name = str(proc.name() or "")
                        try: exe = str(proc.exe() or "")
                        except Exception: exe = ""
                    except Exception:
                        pass
                    rows.append({
                        "title": title, "process": proc_name, "pid": pid, "exe": exe,
                        "class_name": str(item.get("class_name", "") or ""),
                    })
            if not rows:
                # Non-Windows/degraded diagnostic fallback: still return running
                # process names, explicitly marked as such instead of pretending
                # they are visible UI windows.
                seen_names: set[str] = set()
                for proc in psutil.process_iter(["pid", "name"]):
                    try:
                        name0 = str(proc.info.get("name") or "").strip()
                        if not name0 or name0.casefold() in seen_names:
                            continue
                        seen_names.add(name0.casefold())
                        rows.append({"title": name0, "process": name0, "pid": int(proc.info.get("pid") or proc.pid), "degraded": True})
                    except Exception:
                        continue
            rows.sort(key=lambda x: (str(x.get("title", "")).casefold(), int(x.get("pid", 0) or 0)))
            lines = []
            for row in rows[:80]:
                title = str(row.get("title", "") or row.get("process", "") or "Приложение")
                proc = str(row.get("process", "") or "")
                pid = int(row.get("pid", 0) or 0)
                suffix = f" — {proc}" if proc and proc.casefold() not in title.casefold() else ""
                lines.append(f"• {title}{suffix} · PID {pid}")
            msg = "Активные приложения и окна:\n" + "\n".join(lines) if lines else "Активных пользовательских окон не найдено."
            return ToolExecution(True, msg, rows[:200])

        if action == "list":
            seen: dict[str, int] = {}
            for proc in psutil.process_iter(["name"]):
                proc_name = proc.info.get("name") or ""
                if proc_name:
                    seen[proc_name] = seen.get(proc_name, 0) + 1
            data = [{"name": name, "instances": count} for name, count in sorted(seen.items())]
            return ToolExecution(True, f"Запущенных наименований процессов: {len(data)}", data[:200])
        raise ValueError(f"Неизвестное действие application: {action}")

    registry.register(
        ToolDefinition(
            "application",
            "Умный запуск приложений и игр, управление ими и список реальных пользовательских UI-окон (включая игры и окна Проводника); никогда не подменяет приложение похожей папкой или файлом.",
            _object_schema(
                {
                    "action": _enum(["open", "close", "kill", "list", "list_active", "find", "info"], "Операция"),
                    "name": {"type": "string", "description": "Название приложения/игры, exe или точный путь к exe"},
                    "kind": _enum(["app", "game"], "Тип запускаемой цели"),
                },
                ["action"],
            ),
            application,
            application_risk,
        )
    )

    # SMART DESKTOP RESOLVER
    def desktop(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "open") or "open")
        query = str(args.get("query", "") or "").strip()
        hint = str(args.get("hint", "auto") or "auto").strip().casefold()
        if action == "close_folder":
            from jarvis.runtime.folder_context156 import close_selected
            from jarvis.runtime.dialogue_messages import message
            from jarvis.language import language_code
            success, key, label = close_selected(query)
            return ToolExecution(success, message(key, language_code(config), label=label), {"verified":success, "completed":success, "kind":"folder_window", "title":label})
        if not query:
            raise ValueError("Не указано, что нужно найти или открыть")

        # Windows virtual shell folders do not have ordinary filesystem paths.
        # Resolve them explicitly while keeping them in the filesystem-folder
        # domain.  In particular, This PC must never fall through to application
        # discovery where fuzzy EXE matching could choose an unrelated program.
        shell_key = normalize_target(strip_target_words(query))
        this_pc_keys = {"этот компьютер", "мой компьютер", "this pc", "my computer", "computer"}
        if action == "open" and shell_key in this_pc_keys:
            if os.name != "nt":
                return ToolExecution(False, "«Этот компьютер» доступен как системная папка только в Windows", {"kind": "shell", "value": "shell:MyComputerFolder", "verified": False})
            subprocess.Popen(["explorer.exe", "shell:MyComputerFolder"])
            return ToolExecution(True, "Открыт раздел «Этот компьютер»", {"kind": "shell", "value": "shell:MyComputerFolder", "label": "Этот компьютер", "verified": True})

        # The Recycle Bin is a Shell namespace object, not a normal directory on
        # disk. Searching the filesystem for "Корзина" therefore produces bogus
        # neighbours. Open the canonical shell target explicitly.
        recycle_key = shell_key
        if action == "open" and recycle_key in {"корзина", "корзину", "recycle bin", "recyclebin"}:
            if os.name != "nt":
                return ToolExecution(False, "Корзина Windows доступна только в Windows", {"kind": "shell", "value": "shell:RecycleBinFolder", "verified": False})
            subprocess.Popen(["explorer.exe", "shell:RecycleBinFolder"])
            return ToolExecution(True, "Открыта Корзина", {"kind": "shell", "value": "shell:RecycleBinFolder", "label": "Корзина", "verified": True})

        # Type-locked app/game opens go through the application launcher so they
        # receive executable-only resolution and post-launch verification.
        if action == "open" and hint in {"app", "game"}:
            return application({"action": "open", "name": query, "kind": "game" if hint == "game" else "app"})

        # Explicit file/folder language is literal. If the same literal basename
        # exists in several places, do not pick whichever directory happened to be
        # scanned first: ask for the path/location just as a human assistant would.
        if hint in {"file", "folder", "directory"}:
            path_kind = "file" if hint == "file" else "folder"

            # Windows Known Folders have localized spoken names but usually an
            # English basename on disk ("загрузки" -> Downloads).  v3.3's
            # exact-only branch accidentally bypassed these aliases entirely.
            known_folder = None
            if path_kind == "folder":
                qkeys = {normalize_target(query), normalize_target(strip_target_words(query))}
                for alias, path in FOLDER_ALIASES.items():
                    if normalize_target(alias) in qkeys and Path(path).exists():
                        known_folder = Path(path).resolve(strict=False)
                        break
            if known_folder is not None:
                resolved = ResolvedTarget("path", str(known_folder), known_folder.name or str(known_folder), 1.0)
            else:
                exact_matches = find_user_path_exact_candidates(query, kind=path_kind, limit=5)
                if len(exact_matches) > 1:
                    paths = [str(item) for item in exact_matches]
                    label = "файлов" if path_kind == "file" else "папок"
                    return ToolExecution(False, f"Нашёл несколько {label} с точным именем «{query}». Не буду выбирать наугад. Назовите папку или более полный путь: " + "; ".join(paths[:4]), {"kind": path_kind, "query": query, "requires_clarification": True, "options": paths[:5], "verified": False})
                if exact_matches:
                    only = exact_matches[0]
                    resolved = ResolvedTarget("path", str(only), only.name or str(only), 1.0)
                elif path_kind == "folder":
                    # Opening a folder is non-destructive, so recover the older
                    # human-friendly fuzzy lookup for ASR errors such as
                    # Important/Improtant. Require a good score before acting.
                    fuzzy = find_user_path(query, kind="folder", max_seconds=6.5)
                    score = target_similarity(query, fuzzy.name) if fuzzy is not None else 0.0
                    if fuzzy is not None and score >= 0.86:
                        resolved = ResolvedTarget("path", str(fuzzy), fuzzy.name or str(fuzzy), score)
                    else:
                        return ToolExecution(False, f"Папка «{query}» не найдена. Если она находится в нестандартном месте, назовите путь точнее.", {"kind": path_kind, "query": query, "verified": False, "best_score": round(score, 3)})
                else:
                    return ToolExecution(False, f"Файл с точным именем «{query}» не найден. Если он находится в нестандартном месте, назовите путь точнее.", {"kind": path_kind, "query": query, "verified": False})
        else:
            resolved = resolve_basic_target(query, hint=hint)
        if resolved is None:
            # Steam titles are not ordinary shortcuts on every machine. Reuse the
            # application resolver as the final open fallback.
            if action == "open":
                return application({"action": "open", "name": query, "kind": "game" if hint == "game" else "app"})
            return ToolExecution(False, f"Не удалось определить цель: {query}")
        data = {"kind": resolved.kind, "value": resolved.value, "label": resolved.label, "score": resolved.score}
        if action == "resolve":
            return ToolExecution(True, f"Нашёл: {resolved.label}", data)
        if action != "open":
            raise ValueError(f"Неизвестное действие desktop: {action}")
        if resolved.kind == "url":
            opened = owner_registry.execute("browser", {"action": "open_url", "value": resolved.value})
            if not opened.success:
                return opened
            data["browser"] = opened.data
            return ToolExecution(True, f"Открыто: {resolved.label}", data)
        if resolved.kind == "path":
            _open_path(Path(resolved.value))
            return ToolExecution(True, f"Открыто: {resolved.label}", data)
        if resolved.kind in {"app", "shell", "shortcut"}:
            # Even an auto-resolved application must pass through the verified
            # launcher. Otherwise a generic "открой Chrome" path could still
            # claim success immediately after Popen/AppsFolder.
            return application({"action": "open", "name": query, "kind": "app"})
        raise ValueError(f"Неизвестный тип цели: {resolved.kind}")

    registry.register(
        ToolDefinition(
            "desktop",
            "Найти или открыть цель по человеческому названию без точного пути: приложение, ярлык, пользовательскую папку/файл или известный сайт. Особенно полезно для слов 'папка Important', 'Яндекс Музыка', 'страница ВКонтакте'.",
            _object_schema(
                {
                    "action": _enum(["resolve", "open", "close_folder"], "Операция"),
                    "query": {"type": "string", "description": "Обычное название цели"},
                    "hint": _enum(["auto", "app", "game", "folder", "file", "web"], "Предполагаемый тип цели"),
                },
                ["action", "query"],
            ),
            desktop,
            lambda args: Risk.MEDIUM if args.get("action") == "close_folder" else Risk.SAFE,
        )
    )

    # INFORMATION OVERLAYS / CHECKLISTS
    # The tool is Qt-free and therefore safe to call from Agent worker threads.
    # It atomically persists the desired panel state; MainWindow renders it on
    # the GUI thread via OverlayManager. This same API can later render game HUDs.
    overlay_defaults = {
        "monitor": int(config.get("desktop.overlay.monitor", 1) or 1),
        "position": str(config.get("desktop.overlay.position", "top_left") or "top_left"),
        "style": {
            "font_family": config.get("desktop.overlay.font_family", "Segoe UI"),
            "font_size": int(config.get("desktop.overlay.font_size", 18) or 18),
            "text_color": config.get("desktop.overlay.text_color", "#F2F6FF"),
            "background_color": config.get("desktop.overlay.background_color", "#0A0F18"),
            "border_color": config.get("desktop.overlay.border_color", "#57D7FF"),
            "opacity": int(config.get("desktop.overlay.opacity", 72) or 72),
            "background_opacity": int(config.get("desktop.overlay.background_opacity", config.get("desktop.overlay.opacity", 72)) or 0),
            "border_opacity": int(config.get("desktop.overlay.border_opacity", 100) or 0),
            "drag_enabled": bool(config.get("desktop.overlay.drag_enabled", True)),
            "width": int(config.get("desktop.overlay.width", 420) or 420),
            "height": int(config.get("desktop.overlay.height", 0) or 0),
            "click_through": bool(config.get("desktop.overlay.click_through", True)),
        },
    }
    overlay_root = Path(getattr(config.paths, "root", getattr(config.paths, "backups", Path.home() / ".neo_jarvis")))
    overlay_store = OverlayStore(overlay_root / "overlays.json", defaults=overlay_defaults)

    def overlay(args: dict[str, Any]) -> ToolExecution:
        text, data = overlay_store.apply(args)
        # Individual overlay moves are persisted by OverlayStore and carry a
        # per-panel placement_override.  Do not rewrite category defaults here:
        # doing so used to move every notes/music/checklist panel together.
        # A failed unambiguous checklist lookup is intentionally reported as a
        # tool failure so the model does not claim that it checked the wrong item.
        failed = False
        if isinstance(data, dict):
            if "matches" in data and int(data.get("matches", 0)) != 1:
                failed = True
            if data.get("found") is False:
                failed = True
            if str(args.get("action", "")).casefold() == "delete" and data.get("deleted") is False:
                failed = True
            if str(args.get("action", "")).casefold() == "rename" and data.get("renamed") is False:
                failed = True
        return ToolExecution(not failed, text, data)

    registry.register(
        ToolDefinition(
            "overlay",
            "Управление панелями/оверлеями NEO: показать, скрыть, закрыть, переместить, заметки, чек-листы и музыкальная панель. Слова «панель», «оверлей» и «окно» допустимы как синонимы для этих дополнительных панелей.",
            _object_schema(
                {
                    "action": _enum([
                        "show", "hide", "close", "delete", "rename", "list", "hide_all", "delete_all", "set_text", "append_text", "remove_text", "clear_text", "move",
                        "checklist_create", "checklist_add", "checklist_check", "checklist_uncheck", "checklist_remove", "checklist_clear",
                        "checklist_complete_all", "checklist_uncheck_all", "style",
                    ], "Операция"),
                    "name": {"type": "string", "description": "Имя/алиас панели, например 'Музыка', 'Заметки', 'Чек-лист'"},
                    "new_name": {"type": "string", "description": "Новое имя окна для операции rename"},
                    "title": {"type": "string", "description": "Заголовок, показываемый в окне"},
                    "kind": _enum(["text", "checklist", "music"], "Тип содержимого"),
                    "text": {"type": "string", "description": "Текст информационного окна"},
                    "items": {"type": "array", "items": {"type": "string"}, "description": "Пункты нового чек-листа"},
                    "item": {"type": "string", "description": "Один пункт для добавления/отметки/удаления"},
                    "monitor": {"type": "integer", "minimum": 1, "maximum": 16, "description": "Номер монитора, начиная с 1"},
                    "position": _enum([
                        "top_left", "top_center", "top_right", "center_left", "center", "center_right",
                        "bottom_left", "bottom_center", "bottom_right",
                    ], "Позиция на выбранном мониторе"),
                    "offset_x": {"type": "integer", "minimum": -5000, "maximum": 5000},
                    "offset_y": {"type": "integer", "minimum": -5000, "maximum": 5000},
                    "font_size": {"type": "integer", "minimum": 8, "maximum": 96, "description": "Размер шрифта конкретной панели"},
                    "font_family": {"type": "string", "description": "Семейство шрифта конкретной панели"},
                    "theme_preset": {"type": "string", "description": "Название общего визуального пресета панели"},
                    "text_color": {"type": "string", "description": "Цвет текста панели #RRGGBB"},
                    "background_color": {"type": "string", "description": "Цвет фона панели #RRGGBB"},
                    "border_color": {"type": "string", "description": "Цвет рамки панели #RRGGBB"},
                    "background_opacity": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Прозрачность/непрозрачность фона панели в процентах"},
                    "control_color": {"type": "string", "description": "Цвет иконок кнопок музыкальной панели"},
                    "control_background_color": {"type": "string", "description": "Цвет фона кнопок музыкальной панели"},
                    "control_border_color": {"type": "string", "description": "Цвет рамки кнопок музыкальной панели"},
                    "control_background_opacity": {"type": "integer", "minimum": 0, "maximum": 100},
                    "control_border_opacity": {"type": "integer", "minimum": 0, "maximum": 100},
                    "width": {"type": "integer", "minimum": 160, "maximum": 1600},
                    "height": {"type": "integer", "minimum": 0, "maximum": 1400},
                    "purpose": {"type": "string", "description": "Семантическая роль окна: game_session/checklist/notes/music/etc"},
                    "game_id": {"type": "string"},
                    "game_name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "binding": {"type": ["object", "null"], "description": "Live binding окна к источнику состояния"},
                    "live_state": {"type": ["object", "null"]},
                },
                ["action"],
            ),
            overlay,
            lambda _: Risk.SAFE,
        )
    )

    # OPTIONAL GAME SESSION TRACKER
    # Process-only: no save/log/plugin parsing and no process-memory access.
    game_manager = GameManager(
        enabled=bool(config.get("gaming.enabled", True)), config=config
    )
    game_history = GameSessionHistoryStore(overlay_root / "game_sessions.json")
    live_game_panels = LiveGamePanelService(
        game_manager,
        overlay_store,
        game_history,
        refresh_seconds=float(config.get("gaming.telemetry_refresh_seconds", 2.0) or 2.0),
        history_touch_seconds=float(config.get("gaming.history_touch_seconds", 30.0) or 30.0),
    )
    # Do not start a background gaming service merely because NEO launched.
    # A live session worker is started only after the user explicitly asks for a
    # game-session panel, and stale session panels are removed at UI startup.

    def _foreground_pid() -> int | None:
        try:
            ctx = inspect_foreground_context()
            pid = int(ctx.get("pid", 0) or 0) if isinstance(ctx, dict) else int(getattr(ctx, "pid", 0) or 0)
            return pid or None
        except Exception:
            return None

    def _generic_snapshot_from_launch_context(pending: dict[str, Any] | None):
        """Build a basic session for an unsupported game NEO itself launched.

        Universal gaming mode must still know *something* about a title that has no
        dedicated adapter yet.  We only trust a PID/executable previously verified by
        our launcher, never guess from an arbitrary foreground application.
        """
        if not isinstance(pending, dict) or not pending:
            return None
        if str(pending.get("status", "")).casefold() != "running" or not bool(pending.get("verified", False)):
            return None
        try:
            pid = int(pending.get("pid", 0) or 0)
        except Exception:
            pid = 0
        if pid <= 0:
            return None
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                return None
            name = str(proc.name() or pending.get("process_name") or "")
            try:
                exe = str(proc.exe() or pending.get("executable") or "")
            except (psutil.AccessDenied, psutil.Error, OSError):
                exe = str(pending.get("executable") or "")
            try:
                started = float(proc.create_time())
            except (psutil.Error, OSError, ValueError):
                started = float(pending.get("detected_at") or pending.get("command_sent_at") or pending.get("requested_at") or time.time())
        except (psutil.Error, OSError, ValueError):
            return None
        label = str(pending.get("label") or pending.get("query") or Path(exe).stem or name or "Игра").strip()
        return GameSnapshot(
            game_id="generic", game_name=label, pid=pid, process_name=name, executable=exe,
            session_started_at=started, session_seconds=max(0, int(time.time() - started)),
            capabilities=["session"],
            notes=["Доступна только подтверждённая длительность сессии."],
        )

    def _current_game_snapshot(*, require_foreground: bool = False, wait_for_pending: bool = True):
        # Config can change while the app is open, so honor the latest value on
        # every user command instead of requiring a restart.
        game_manager.enabled = bool(config.get("gaming.enabled", True))
        snap = game_manager.snapshot(
            preferred_pid=_foreground_pid(), require_preferred=require_foreground
        )
        if snap is not None:
            return snap

        pending = recent_game_launch(max_age_seconds=float(config.get("gaming.launch_context_seconds", 1200) or 1200))
        generic = _generic_snapshot_from_launch_context(pending)
        if generic is not None and not require_foreground:
            return generic
        if require_foreground or not wait_for_pending:
            return None

        # A storefront can return control long before the game EXE exists.  Keep
        # the launch intent and give the real process another bounded window on a
        # follow-up such as “show my current game session”.
        if not pending:
            return None
        query = str(pending.get("label") or pending.get("query") or "").strip()
        try:
            wait_seconds = max(0.0, min(60.0, float(config.get("gaming.followup_wait_seconds", 45) or 45)))
        except Exception:
            wait_seconds = 45.0
        snap = game_manager.wait_for_snapshot(query, timeout=wait_seconds)
        if snap is not None:
            update_game_launch(
                status="running", verified=True, game_id=snap.game_id, label=snap.game_name,
                pid=snap.pid, executable=snap.executable, process_name=snap.process_name,
                detected_at=time.time(),
            )
        return snap

    def _session_panel_args(snap, args: dict[str, Any]) -> dict[str, Any]:
        panel_name = "Игровая сессия"
        state = snap.as_dict()
        panel_args: dict[str, Any] = {
            "action": "set_text",
            "name": panel_name,
            "title": "",
            "text": GameManager.simple_session_text(snap),
            "purpose": "game_session",
            "game_id": snap.game_id,
            "game_name": snap.game_name,
            "aliases": ["сессия", "игровая сессия", "сессия по игре", "информация по игре", "статистика по игре"],
            "live_state": state,
            "binding": {
                "type": "game_session",
                "status": "running",
                "game_id": snap.game_id,
                "game_name": snap.game_name,
                "pid": int(snap.pid),
                "session_started_at": float(snap.session_started_at),
                "refresh_seconds": float(config.get("gaming.telemetry_refresh_seconds", 2.0) or 2.0),
                "last_telemetry_at": time.time(),
                "simple_mode": True,
            },
        }
        for key in ("monitor", "position", "offset_x", "offset_y"):
            if args.get(key) is not None:
                panel_args[key] = args[key]
        return panel_args

    def _start_pending_game_session_watcher(args: dict[str, Any], pending: dict[str, Any]) -> None:
        """Populate a requested session panel as soon as the real EXE appears."""
        frozen_args = dict(args)
        frozen_pending = dict(pending)

        def worker() -> None:
            query = str(frozen_pending.get("label") or frozen_pending.get("query") or "").strip()
            try:
                snap = game_manager.wait_for_snapshot(query, timeout=_game_launch_wait_seconds())
            except Exception:
                logger.debug("Pending game session panel watcher failed", exc_info=True)
                snap = None
            if snap is None:
                overlay_store.apply({
                    "action": "set_text", "name": "Игровая сессия",
                    "title": f"{str(frozen_pending.get('label') or 'Игра')} · запуск",
                    "text": "Основной процесс игры так и не появился в отведённое время. Скажите «покажи сессию» после запуска игры, чтобы проверить снова.",
                })
                return
            update_game_launch(
                status="running", verified=True, game_id=snap.game_id, label=snap.game_name,
                pid=snap.pid, executable=snap.executable, process_name=snap.process_name,
                detected_at=time.time(),
            )
            overlay_store.apply(_session_panel_args(snap, frozen_args))
            live_game_panels.ensure_started()

        threading.Thread(target=worker, name="jarvis-game-session-watch", daemon=True).start()

    def game(args: dict[str, Any]) -> ToolExecution:
        action = str(args.get("action", "status") or "status").strip().casefold()
        allowed = {"status", "state", "session_info", "show_session"}
        if action not in allowed:
            return ToolExecution(
                False,
                "Игровой слой поддерживает только определение запущенной игры и отображение сессии.",
                {"supported_actions": sorted(allowed)},
            )

        # Showing a panel during a slow Steam/Epic/launcher startup must return
        # immediately.  The background watcher replaces the placeholder once the
        # real game EXE appears.  Status questions may spend a bounded follow-up
        # wait because they have no persistent panel to update later.
        snap = _current_game_snapshot(
            require_foreground=False,
            wait_for_pending=action != "show_session",
        )
        if snap is None:
            pending = recent_game_launch(max_age_seconds=float(config.get("gaming.launch_context_seconds", 1200) or 1200))
            if pending and str(pending.get("status", "")).casefold() in {"launch_requested", "starting"}:
                label = str(pending.get("label") or pending.get("query") or "игра")
                if action == "show_session":
                    panel_name = "Игровая сессия"
                    placeholder: dict[str, Any] = {
                        "action": "set_text",
                        "name": panel_name,
                        "title": "",
                        "text": f"Игра: {label}\nЗапущена: ожидаю процесс…\nСессия: 0 с",
                        "purpose": "game_session",
                        "game_id": str(pending.get("game_id") or "generic"),
                        "game_name": label,
                        "aliases": ["сессия", "игровая сессия", "сессия по игре", "информация по игре"],
                        "binding": {
                            "type": "game_session", "status": "starting",
                            "game_id": str(pending.get("game_id") or "generic"),
                            "game_name": label, "pid": 0,
                            "session_started_at": 0.0, "simple_mode": True,
                        },
                    }
                    for key in ("monitor", "position", "offset_x", "offset_y"):
                        if args.get(key) is not None:
                            placeholder[key] = args[key]
                    panel_text, panel_data = overlay_store.apply(placeholder)
                    _start_pending_game_session_watcher(args, pending)
                    return ToolExecution(
                        True,
                        f"{panel_text} {label} ещё запускается; окно обновится автоматически, когда появится основной процесс.",
                        {"launch_pending": True, "overlay": panel_data, **pending},
                    )
                return ToolExecution(
                    False,
                    f"{label} всё ещё запускается. NEO продолжает ждать основной процесс.",
                    {"launch_pending": True, **pending},
                )
            return ToolExecution(False, "Сейчас не обнаружена запущенная игра.")

        data = snap.as_dict()
        if action in {"status", "state", "session_info"}:
            return ToolExecution(True, GameManager.simple_session_text(snap), data)

        panel_args = _session_panel_args(snap, args)
        panel_text, panel_data = overlay_store.apply(panel_args)
        live_game_panels.ensure_started()
        return ToolExecution(
            True,
            f"{panel_text} Панель сессии обновляется автоматически.",
            {"game": data, "overlay": panel_data},
        )

    registry.register(
        ToolDefinition(
            "game",
            "Универсальный игровой трекер NEO: определить запущенную игру и показать текущую сессию. Не читает память процесса и не получает скрытые игровые данные.",
            _object_schema(
                {
                    "action": _enum(["status", "state", "session_info", "show_session"], "Операция"),
                    "game_id": {"type": "string", "description": "ID игры; обычно определяется автоматически"},
                    "game_name": {"type": "string", "description": "Отображаемое имя игры"},
                    "panel_name": {"type": "string", "description": "Имя панели сессии"},
                    "monitor": {"type": "integer", "minimum": 1, "maximum": 16, "description": "Монитор для панели сессии"},
                    "position": _enum([
                        "top_left", "top_center", "top_right", "center_left", "center", "center_right",
                        "bottom_left", "bottom_center", "bottom_right",
                    ], "Позиция overlay"),
                    "offset_x": {"type": "integer", "minimum": -5000, "maximum": 5000},
                    "offset_y": {"type": "integer", "minimum": -5000, "maximum": 5000},
                },
                ["action"],
            ),
            game,
            lambda _: Risk.SAFE,
        )
    )

    # WINDOWS
    def window_risk(args: dict[str, Any]) -> Risk:
        return Risk.MEDIUM if args.get("action") in {"close", "move_resize"} else Risk.SAFE

    def _window_score(query: str, title: str) -> float:
        q = normalize_target(normalize_entity_phrase(query))
        t = normalize_target(title)
        if q in {"browser", "браузер", "браузер окно", "browser window"}:
            return 1.0 if any(x in t for x in ("mozilla firefox", "google chrome", "chrome", "opera", "microsoft edge", "yandex", "brave", "vivaldi")) else 0.0
        q = re.sub(r"\b(?:окно|браузер|browser|приложение|application)\b", " ", q).strip()
        if not q:
            return 0.0
        if q == t:
            return 1.0
        aliases = {
            "мозила": "mozilla firefox", "мозилла": "mozilla firefox", "firefox": "mozilla firefox",
            "фаерфокс": "mozilla firefox", "хром": "google chrome", "chrome": "google chrome",
            "опера": "opera", "яндекс браузер": "yandex", "эдж": "microsoft edge", "edge": "microsoft edge",
            "yandex music": "яндекс музыка", "андекс музыка": "яндекс музыка", "ендекс музыка": "яндекс музыка",
            "дискорд": "discord", "стим": "steam",
        }
        q = aliases.get(q, q)
        if q == "яндекс музыка" and "яндекс музыка" in t:
            return 0.99
        if q == "discord" and "discord" in t:
            return 0.99
        if q == "steam" and "steam" in t:
            return 0.99
        q_tokens = q.split()
        t_tokens = t.split()
        token_cover = 0.0
        if q_tokens and t_tokens:
            token_cover = sum(max(difflib.SequenceMatcher(None, x, y).ratio() for y in t_tokens) for x in q_tokens) / len(q_tokens)
        seq = difflib.SequenceMatcher(None, q, t).ratio()
        if q in t or t in q:
            seq = max(seq, 0.90)
        return max(seq, token_cover)

    def _window_hwnd(candidate: Any) -> int:
        for attr in ("_hWnd", "hWnd", "hwnd", "handle"):
            try:
                value = int(getattr(candidate, attr, 0) or 0)
            except Exception:
                value = 0
            if value:
                return value
        return 0

    def _window_process_name(candidate: Any) -> str:
        """Return the owning executable name without enumerating the UIA tree."""
        hwnd = _window_hwnd(candidate)
        if not hwnd or os.name != "nt":
            return ""
        try:
            from ctypes import wintypes
            pid = wintypes.DWORD(0)
            ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
            if not pid.value:
                return ""
            return str(psutil.Process(int(pid.value)).name() or "").casefold()
        except Exception:
            return ""

    def _window_class_name(candidate: Any) -> str:
        hwnd = _window_hwnd(candidate)
        if not hwnd or os.name != "nt":
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetClassNameW(hwnd, buffer, len(buffer))
            return str(buffer.value or "").casefold()
        except Exception:
            return ""

    def _monitor_rects() -> list[dict[str, int]]:
        """Return monitors in the same numbering Windows Settings uses.

        mss' list order is not guaranteed to match DISPLAY1/DISPLAY2/... on every
        machine.  Voice commands explicitly say "second/third monitor", so prefer
        the native DISPLAY<n> mapping and only fall back to mss when User32 is not
        available.
        """
        try:
            from jarvis.display_mapping import windows_monitor_geometries
            native = windows_monitor_geometries()
            if native:
                return [
                    {
                        "number": int(number), "left": int(rect[0]), "top": int(rect[1]),
                        "width": int(rect[2] - rect[0]), "height": int(rect[3] - rect[1]),
                        "right": int(rect[2]), "bottom": int(rect[3]),
                    }
                    for number, rect, _device in native
                ]
        except Exception:
            logger.debug("Native monitor enumeration failed; using mss order", exc_info=True)
        import mss
        with mss.mss() as sct:
            result: list[dict[str, int]] = []
            for index, raw in enumerate(sct.monitors[1:], start=1):
                item = dict(raw)
                item["number"] = index
                item["right"] = int(item["left"] + item["width"])
                item["bottom"] = int(item["top"] + item["height"])
                result.append(item)
            return result

    def _resolve_monitor_number(monitors: list[dict[str, int]], requested: int) -> tuple[dict[str, int] | None, int | None, str]:
        """Resolve either a native DISPLAY<n> id or a human 1..N ordinal.

        Windows may keep native device ids such as DISPLAY6/7/8 after old monitors
        were disconnected. Voice commands like "третий экран" are human ordinals,
        so an exact native id wins first and otherwise 1..N addresses the current
        connected-monitor list.
        """
        requested = int(requested or 0)
        exact = next((item for item in monitors if int(item.get("number", 0)) == requested), None)
        if exact is not None:
            return exact, int(exact.get("number", requested)), "native"
        if 1 <= requested <= len(monitors):
            item = monitors[requested - 1]
            return item, int(item.get("number", 0) or 0), "ordinal"
        return None, None, "missing"

    def _window_rect_tuple(candidate: Any) -> tuple[int, int, int, int] | None:
        try:
            left = int(candidate.left); top = int(candidate.top)
            width = int(candidate.width); height = int(candidate.height)
            return left, top, left + max(1, width), top + max(1, height)
        except Exception:
            pass
        try:
            rect = candidate.box
            return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        except Exception:
            return None

    def _window_monitor_number(candidate: Any, monitors: list[dict[str, int]]) -> int | None:
        wr = _window_rect_tuple(candidate)
        if wr is None:
            return None
        best_number = None
        best_area = 0
        for monitor in monitors:
            mr = (
                int(monitor["left"]), int(monitor["top"]),
                int(monitor.get("right", monitor["left"] + monitor["width"])),
                int(monitor.get("bottom", monitor["top"] + monitor["height"])),
            )
            left = max(wr[0], mr[0]); top = max(wr[1], mr[1])
            right = min(wr[2], mr[2]); bottom = min(wr[3], mr[3])
            area = max(0, right - left) * max(0, bottom - top)
            if area > best_area:
                best_area = area
                best_number = int(monitor["number"])
        return best_number

    def _find_window(title: str, *, source_monitor: int = 0, kind: str = ""):
        import pygetwindow as gw
        title = normalize_entity_phrase(title) or title
        query = normalize_target(title)
        if not title or query in {"active", "активное", "текущее", "current"}:
            active = gw.getActiveWindow()
            if active is None:
                return None
            if source_monitor:
                monitors = _monitor_rects()
                source_item, source_native, _source_mode = _resolve_monitor_number(monitors, source_monitor)
                if source_item is None or _window_monitor_number(active, monitors) != source_native:
                    return None
            return active

        windows = [w for w in gw.getAllWindows() if str(getattr(w, "title", "") or "").strip()]
        monitors = _monitor_rects() if source_monitor else []
        if source_monitor:
            source_item, source_native, _source_mode = _resolve_monitor_number(monitors, source_monitor)
            if source_item is None:
                return None
            windows = [w for w in windows if _window_monitor_number(w, monitors) == int(source_native)]
        if not windows:
            return None

        kind_n = normalize_target(kind)
        browser_words = {"browser", "браузер", "вкладка", "browser window", "окно браузера",
                         "navigateur", "navegador", "fenêtre du navigateur", "ventana del navegador",
                         "finestra del browser", "janela do navegador", "browserfenster"}
        folder_words = {"folder", "папка", "проводник", "explorer", "file explorer", "окно папки"}
        # NEO 188 atlas: «minimize the browser», «minimiere den Browser», «minimiza o navegador».
        bare = re.sub(r"^(?:the|den|das|die|le|la|el|il|lo|o|a)\s+", "", query)
        if bare != query and bare in browser_words | folder_words:
            query = bare
        if query in browser_words:
            kind_n = "browser"
        elif query in folder_words:
            kind_n = "folder"

        browser_processes = {
            "chrome.exe", "firefox.exe", "msedge.exe", "opera.exe", "opera_gx.exe",
            "browser.exe", "yandex.exe", "vivaldi.exe", "brave.exe", "arc.exe",
        }
        preferred = normalize_target(str(config.get("desktop.preferred_browser", "") or ""))
        preferred_proc = {
            "chrome": "chrome.exe", "google chrome": "chrome.exe", "firefox": "firefox.exe",
            "mozilla firefox": "firefox.exe", "edge": "msedge.exe", "microsoft edge": "msedge.exe",
            "opera": "opera.exe", "yandex": "browser.exe", "яндекс": "browser.exe",
        }.get(preferred, "")

        active = None
        try:
            active = gw.getActiveWindow()
        except Exception:
            pass

        ranked: list[tuple[float, Any, str]] = []
        for candidate in windows:
            candidate_title = str(getattr(candidate, "title", "") or "")
            process_name = _window_process_name(candidate)
            class_name = _window_class_name(candidate)
            if kind_n == "browser" and process_name not in browser_processes:
                continue
            if kind_n == "folder":
                if process_name != "explorer.exe":
                    continue
                # Explorer owns the desktop and taskbar too. Only actual folder
                # windows use CabinetWClass/ExploreWClass; never try to move
                # Progman/Shell_TrayWnd just because the user said “окно папки”.
                if class_name and class_name not in {"cabinetwclass", "explorewclass"}:
                    continue

            if kind_n == "browser" and query in browser_words:
                score = 1.0
            elif kind_n == "folder" and query in folder_words:
                score = 1.0
            else:
                score = _window_score(title, candidate_title)
                if process_name:
                    proc_label = process_name.removesuffix(".exe").replace("_", " ")
                    score = max(score, _window_score(title, proc_label))

            if preferred_proc and process_name == preferred_proc and kind_n == "browser":
                score += 0.04
            try:
                if active is not None and _window_hwnd(active) == _window_hwnd(candidate):
                    score += 0.03
            except Exception:
                pass
            ranked.append((score, candidate, process_name))

        if not ranked:
            return None
        ranked.sort(key=lambda x: x[0], reverse=True)
        best_score, best, process_name = ranked[0]
        logger.info(
            "Window resolve: query=%r kind=%s source_monitor=%s -> title=%r process=%s score=%.3f",
            title, kind_n or "any", source_monitor or "any", str(getattr(best, "title", "") or ""), process_name, best_score,
        )
        threshold = 0.50 if kind_n in {"browser", "folder"} else 0.56
        return best if best_score >= threshold else None

    def window(args: dict[str, Any]) -> ToolExecution:
        if os.name != "nt":
            raise OSError("Управление окнами реализовано для Windows")
        from jarvis.runtime.delayed_actions184 import current_binding, execute_bound_close
        delayed_target = current_binding('close_browser')
        if delayed_target is not None:
            if args.get('action') != 'close':
                raise ValueError('Invalid bound window action')
            data = execute_bound_close(delayed_target)
            return ToolExecution(data.get('verified') is True, 'Scheduled browser close', data)
        import pygetwindow as gw

        action = str(args.get("action", ""))
        title = str(args.get("title", "")).strip()
        if action == "active":
            active = gw.getActiveWindow()
            return ToolExecution(True, "Активное окно получено", active.title if active else "")
        if action == "list":
            values = [str(w.title) for w in gw.getAllWindows() if str(getattr(w, "title", "") or "").strip()]
            return ToolExecution(True, f"Открытых окон: {len(values)}", values[:100])
        source_monitor = int(args.get("source_monitor", 0) or 0)
        kind = str(args.get("kind", "") or "").strip()
        title_key = normalize_target(title)
        if title_key in {"", "active", "current", "текущий", "текущее", "текущее окно", "активное окно"}:
            target = gw.getActiveWindow()
        else:
            target = _find_window(title, source_monitor=source_monitor, kind=kind)
        if target is None:
            suffix = f" на мониторе {source_monitor}" if source_monitor else ""
            return ToolExecution(False, f"Окно не найдено по смыслу: {title}{suffix}")

        # v4 reversible desktop actions. Capture the exact HWND + geometry before
        # changing the window. Closing a window is intentionally not advertised as
        # reversible, but moving/minimizing/maximizing/restoring is.
        _undo_snapshot = None
        if action in {"minimize", "maximize", "restore", "move_resize", "move_to_monitor"}:
            try:
                hwnd_before = _window_hwnd(target)
                native = _capture_native_window_placement(hwnd_before)
                is_max, is_min = _native_window_state(hwnd_before)
                _undo_snapshot = {
                    "hwnd": hwnd_before,
                    "title": str(getattr(target, "title", "") or ""),
                    "left": int(getattr(target, "left", 0)), "top": int(getattr(target, "top", 0)),
                    "width": max(100, int(getattr(target, "width", 1000) or 1000)),
                    "height": max(100, int(getattr(target, "height", 700) or 700)),
                    "maximized": bool(is_max or getattr(target, "isMaximized", False)),
                    "minimized": bool(is_min or getattr(target, "isMinimized", False)),
                    "native_placement": native or {},
                }
            except Exception:
                _undo_snapshot = None

        def _push_window_undo(label: str) -> None:
            snap = dict(_undo_snapshot or {})
            if not snap:
                return
            def _restore_previous_window_state():
                import pygetwindow as _gw
                candidate = None
                hwnd = int(snap.get("hwnd", 0) or 0)
                for item in _gw.getAllWindows():
                    try:
                        if hwnd and _window_hwnd(item) == hwnd:
                            candidate = item; break
                    except Exception:
                        continue
                if candidate is None:
                    wanted = str(snap.get("title", "") or "")
                    ranked = sorted(
                        [x for x in _gw.getAllWindows() if str(getattr(x, "title", "") or "").strip()],
                        key=lambda x: _window_score(wanted, str(getattr(x, "title", "") or "")),
                        reverse=True,
                    )
                    candidate = ranked[0] if ranked and _window_score(wanted, str(getattr(ranked[0], "title", "") or "")) >= 0.80 else None
                if candidate is None:
                    raise RuntimeError("Окно для Undo больше не найдено")
                # Exact Win32 placement is the primary restore path.  It stores
                # both show state and rcNormalPosition, which is precisely what
                # "отмени последнее действие" needs after maximize/restore.
                native = snap.get("native_placement") if isinstance(snap.get("native_placement"), dict) else {}
                candidate_hwnd = _window_hwnd(candidate)
                if native and _apply_native_window_placement(candidate_hwnd, native):
                    time.sleep(0.10)
                    return {
                        "title": str(getattr(candidate, "title", "") or ""),
                        "restored": True,
                        "method": "SetWindowPlacement",
                    }
                try: candidate.restore()
                except Exception: pass
                time.sleep(0.04)
                candidate.moveTo(int(snap["left"]), int(snap["top"]))
                candidate.resizeTo(int(snap["width"]), int(snap["height"]))
                if bool(snap.get("maximized")):
                    candidate.maximize()
                elif bool(snap.get("minimized")):
                    candidate.minimize()
                else:
                    try: candidate.restore()
                    except Exception: pass
                return {"title": str(getattr(candidate, "title", "") or ""), "restored": True}
            undo_manager().push(label, "window", snap, _restore_previous_window_state)

        if action == "focus":
            target.activate()
        elif action == "minimize":
            hwnd = _window_hwnd(target)
            if not _native_show_window(hwnd, "minimize"):
                target.minimize()
        elif action == "maximize":
            hwnd = _window_hwnd(target)
            if not _native_show_window(hwnd, "maximize"):
                target.maximize()
        elif action == "restore":
            hwnd = _window_hwnd(target)
            if not _native_show_window(hwnd, "restore"):
                target.restore()
        elif action == "close":
            target.close()
        elif action == "move_resize":
            target.moveTo(int(args.get("x", 0)), int(args.get("y", 0)))
            target.resizeTo(int(args.get("width", 1000)), int(args.get("height", 700)))
        elif action == "move_to_monitor":
            monitors = _monitor_rects()
            monitor_number = int(args.get("monitor", 1) or 1)
            monitor, native_monitor_number, monitor_resolution = _resolve_monitor_number(monitors, monitor_number)
            if monitor is None:
                native_available = ", ".join(str(item.get("number")) for item in monitors) or "нет"
                ordinals = f"1–{len(monitors)}" if monitors else "нет"
                raise ValueError(
                    f"Монитор {monitor_number} не найден. Доступны экранные номера: {ordinals}; "
                    f"native DISPLAY id: {native_available}"
                )
            was_maximized = bool(getattr(target, "isMaximized", False))
            try:
                if was_maximized:
                    target.restore()
                    time.sleep(0.10)
            except Exception:
                pass
            width = max(320, min(int(getattr(target, "width", 1000) or 1000), int(monitor["width"] * 0.92)))
            height = max(240, min(int(getattr(target, "height", 700) or 700), int(monitor["height"] * 0.92)))
            x = int(monitor["left"] + (monitor["width"] - width) / 2)
            y = int(monitor["top"] + (monitor["height"] - height) / 2)
            target.moveTo(x, y)
            if not was_maximized:
                target.resizeTo(width, height)
            else:
                time.sleep(0.10)
                target.maximize()
            try:
                target.activate()
            except Exception:
                pass
            _push_window_undo(f"Вернуть окно «{target.title}» на прежнее место")
            return ToolExecution(True, f"Окно «{target.title}» перенесено на монитор {monitor_number}", {
                "title": str(target.title), "monitor": monitor_number,
                "native_monitor": native_monitor_number, "monitor_resolution": monitor_resolution,
                "x": x, "y": y,
            })
        else:
            raise ValueError(f"Неизвестное действие window: {action}")

        # Do not report success merely because a wrapper method returned. Verify
        # the actual User32 state for desired-state commands.
        if action in {"minimize", "maximize", "restore"}:
            hwnd_after = _window_hwnd(target)
            deadline = time.monotonic() + 1.25
            is_max = is_min = False
            while time.monotonic() < deadline:
                is_max, is_min = _native_window_state(hwnd_after)
                if action == "maximize" and is_max:
                    break
                if action == "minimize" and is_min:
                    break
                if action == "restore" and not is_max and not is_min:
                    break
                time.sleep(0.05)
            if action == "maximize" and not is_max:
                return ToolExecution(False, f"Windows не подтвердил разворачивание окна: {target.title}")
            if action == "minimize" and not is_min:
                return ToolExecution(False, f"Windows не подтвердил сворачивание окна: {target.title}")
            if action == "restore" and (is_max or is_min):
                return ToolExecution(False, f"Windows не подтвердил возврат окна в обычный размер: {target.title}")
        if action in {"minimize", "maximize", "restore", "move_resize"}:
            _push_window_undo(f"Отменить действие {action} для окна «{target.title}»")
        if action == "restore":
            return ToolExecution(True, f"Окно «{target.title}» возвращено в обычный размер")
        return ToolExecution(True, f"Операция {action} выполнена для окна: {target.title}")

    registry.register(
        ToolDefinition(
            "window",
            "Управление окнами Windows: найти окно по обычному названию, фокус/свернуть/развернуть/закрыть, изменить размер и перенести на конкретный монитор.",
            _object_schema(
                {
                    "action": _enum(
                        ["active", "list", "focus", "minimize", "maximize", "restore", "close", "move_resize", "move_to_monitor"],
                        "Операция",
                    ),
                    "title": {"type": "string", "description": "Обычное название или часть заголовка окна, например Mozilla, Chrome, Яндекс Музыка"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "width": {"type": "integer", "minimum": 100},
                    "height": {"type": "integer", "minimum": 100},
                    "monitor": {"type": "integer", "minimum": 1, "maximum": 16, "description": "Номер экрана/монитора назначения, начиная с 1"},
                    "source_monitor": {"type": "integer", "minimum": 1, "maximum": 16, "description": "Если указан — искать окно только на этом исходном мониторе"},
                    "kind": _enum(["", "browser", "folder", "any"], "Тип окна для надёжного поиска; browser=любой браузер, folder=Проводник"),
                },
                ["action"],
            ),
            window,
            window_risk,
        )
    )

    # BROWSER / MEDIA / DISCORD / YANDEX — split registrar.
    from jarvis.tools.domains.browser_media import register_browser_media_tools
    _send_media_key, _run_async = register_browser_media_tools(
        registry, config=config, yandex_adapter=yandex_adapter, spotify_adapter=spotify_adapter,
        youtube_adapter=youtube_adapter, discord_adapter=discord_adapter
    )

    # AUDIO — split registrar. Heavy Core Audio/session logic is loaded only when built-ins are registered.
    from jarvis.tools.domains.audio import register_audio_tools
    register_audio_tools(registry, config=config, send_media_key=_send_media_key, run_command=_run)

    # SYSTEM / SCREEN / INPUT — split interaction registrar.
    from jarvis.tools.domains.system_interaction import register_system_interaction_tools
    register_system_interaction_tools(
        registry, config=config, ollama=ollama, run_command=_run, run_async=_run_async,
        run_windows_console=_run_windows_console, window_hwnd=_window_hwnd,
        window_process_name=_window_process_name,
    )

    # NETWORK / MEMORY / DEVELOPMENT / POWERSHELL — split support registrar.
    from jarvis.tools.domains.support import register_support_tools
    register_support_tools(
        registry, config=config, db=db, ollama=ollama, run_command=_run,
        hidden_subprocess_kwargs=_hidden_subprocess_kwargs, known_folder=_known_folder,
        require_path_value=_require_path_value, safe_filename=_safe_filename,
    )
