"""Bounded local Steam title evidence; no process, registry or network discovery.

An installation folder is not a product title. Only a unique, well-formed
AppState record in that installation's own steamapps directory can name it.
This deliberately does not import runtime.game_locations and its scan services.
"""
from __future__ import annotations

import os
import re
import stat
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PureWindowsPath

MAX_MANIFEST_BYTES = 64_000
MAX_TOTAL_BYTES = 1_024_000
MAX_MANIFESTS = 256
MAX_ENTRIES = 512
MAX_CACHED_LIBRARIES = 16
CACHE_SECONDS = 30.0
MIN_SCAN_SECONDS = 2.0
_MANIFEST = re.compile(r"appmanifest_([1-9][0-9]{0,15})\.acf", re.I)
_TOKEN = re.compile(r'\s+|//[^\r\n]*|"((?:[^"\\]|\\[\\"nt])*)"|([{}])')
_WINDOWS = os.name == "nt"
_LOCAL_DRIVE_TYPES = frozenset({2, 3, 5, 6})  # removable, fixed, optical, RAM
_DIRECTORY_ATTRIBUTE = 0x10
_UNSAFE_ATTRIBUTES = 0x400 | 0x40 | 0x1000 | 0x40000 | 0x400000
# Reparse points, devices and files requiring offline/remote recall are excluded.
_DOS_DEVICE = re.compile(r"(?:con|prn|aux|nul|clock\$|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?", re.I)


@dataclass(frozen=True)
class StoreTitle:
    name: str = ""
    source: str = "installation_directory"
    status: str = "metadata_missing"
    app_id: str = ""


def _manifest_fields(raw: bytes, *, ownership_only=False) -> dict | None:
    """Strict bounded KeyValues subset; reject duplicates and incomplete input."""
    if len(raw) > MAX_MANIFEST_BYTES:
        return None
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeError:
        return None
    tokens = []
    position = 0
    while position < len(text):
        token = _TOKEN.match(text, position)
        if token is None or len(tokens) >= 8192:
            return None
        position = token.end()
        if token.group(1) is not None:
            value = re.sub(r'\\([\\"nt])', lambda m: {"n": "\n", "t": "\t"}.get(m[1], m[1]), token.group(1))
            tokens.append(("string", value))
        elif token.group(2):
            tokens.append((token.group(2), ""))
    index = 0
    duplicate = False

    def object_values(depth=0):
        nonlocal index, duplicate
        if depth > 16:
            raise ValueError("depth")
        values = {}
        while index < len(tokens) and tokens[index][0] != "}":
            kind, key = tokens[index]
            index += 1
            key = key.casefold()
            if kind != "string" or index >= len(tokens):
                raise ValueError("key")
            kind, value = tokens[index]
            index += 1
            if kind == "{":
                value = object_values(depth + 1)
                if index >= len(tokens) or tokens[index][0] != "}":
                    raise ValueError("close")
                index += 1
            elif kind != "string":
                raise ValueError("value")
            if key in values:
                duplicate = True
                values[key] = None  # A duplicated ownership field proves nothing.
            else:
                values[key] = value
        return values

    try:
        root = object_values()
        if index != len(tokens) or set(root) != {"appstate"}:
            return None
        if duplicate and not ownership_only:
            return None
        return root["appstate"] if isinstance(root["appstate"], dict) else None
    except (ValueError, RecursionError):
        return None


def _plain_text(value, maximum):
    return (isinstance(value, str) and 1 <= len(value) <= maximum and value == value.strip()
            and not any(unicodedata.category(c).startswith("C") or unicodedata.category(c) in {"Zl", "Zp"}
                        for c in value))


def _valid_folder(folder):
    return _plain_text(folder, 255) and folder not in {".", ".."} and not any(c in folder for c in '\\/:')


def parse_steam_manifest(raw: bytes, filename: str) -> tuple[str, str, str] | None:
    match = _MANIFEST.fullmatch(filename)
    values = _manifest_fields(raw)
    if not match or values is None:
        return None
    app_id, name, folder = (values.get(key) for key in ("appid", "name", "installdir"))
    if app_id != match[1] or not _plain_text(name, 256) or not _valid_folder(folder):
        return None
    return app_id, name, folder


def _signature(path: Path):
    if _WINDOWS and not _windows_preflight(str(path)):
        raise OSError("Unverified local metadata path")
    info = path.stat(follow_symlinks=False)
    if _WINDOWS and getattr(info, "st_file_attributes", 0) & _UNSAFE_ATTRIBUTES:
        raise OSError("Unverified metadata attributes")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _windows_path(executable: str):
    """Lexical only: never let a namespace/device path reach a filesystem API."""
    if not isinstance(executable, str) or not executable or len(executable) > 4096:
        return None
    normalized = executable.replace("/", "\\")
    if not re.match(r"^[A-Za-z]:\\", normalized):
        return None
    components = normalized[3:].split("\\")
    if normalized[3:] == "":
        components = []  # A drive root is valid for internal parent preflight.
    if len(components) > 63:
        return None
    for component in components:
        if (not component or component in {".", ".."} or component.endswith((" ", "."))
                or any(c in component for c in '<>:"|?*')
                or any(not c.isprintable() for c in component)
                or _DOS_DEVICE.fullmatch(component)):
            return None
    return PureWindowsPath(normalized)


@lru_cache(maxsize=1)
def _windows_functions():
    # Loaded only on native Windows. Tests replace the two small query wrappers;
    # no fixture invokes a Windows DLL or uses an actual Windows/user path.
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    drive_type = kernel.GetDriveTypeW
    drive_type.argtypes = (ctypes.c_wchar_p,)
    drive_type.restype = ctypes.c_uint
    attributes = kernel.GetFileAttributesW
    attributes.argtypes = (ctypes.c_wchar_p,)
    attributes.restype = ctypes.c_uint32
    return drive_type, attributes


def _windows_drive_type(root: str):
    return _windows_functions()[0](root)


def _windows_attributes(path: str):
    return _windows_functions()[1](path)


def _windows_preflight(raw: str, *, directory: bool | None = None) -> bool:
    """Check a local drive and each parent before traversing the next component.

    This rejects observed unsafe namespaces; it does not lock the namespace or
    provide a hard I/O timeout. A concurrent remap/rename can still race a check.
    """
    path = _windows_path(raw)
    if path is None:
        return False
    try:
        drive_type = _windows_drive_type(path.anchor)
        if type(drive_type) is not int or drive_type not in _LOCAL_DRIVE_TYPES:
            return False
        parts = path.parts
        for index in range(1, len(parts) + 1):
            # Root-to-leaf order is essential: never query below an observed
            # junction/reparse point, including one above steamapps.
            prefix = str(PureWindowsPath(*parts[:index]))
            attributes = _windows_attributes(prefix)
            if (type(attributes) is not int or not 0 < attributes < 0xFFFFFFFF
                    or attributes & _UNSAFE_ATTRIBUTES):
                return False
            is_directory = bool(attributes & _DIRECTORY_ATTRIBUTE)
            if index < len(parts) and not is_directory:
                return False
            if index == len(parts) and directory is not None and is_directory != directory:
                return False
        return True
    except Exception:
        # Unknown API availability/results must not fall through to traversal.
        return False


def _installation(executable: str):
    if not executable or len(executable) > 4096 or executable.startswith(("\\\\", "//")) or "\x00" in executable:
        return None
    path = _windows_path(executable) if _WINDOWS else Path(executable)
    if path is None:
        return None
    parts = path.parts
    if not path.is_absolute() or len(parts) > 64 or ".." in parts:
        return None
    markers = [i for i in range(len(parts) - 3)
               if parts[i].casefold() == "steamapps" and parts[i + 1].casefold() == "common"]
    if len(markers) != 1:
        return None
    index = markers[0]
    path_type = PureWindowsPath if _WINDOWS else Path
    return path, path_type(*parts[:index + 1]), path_type(*parts[:index + 3])


@dataclass
class _Inventory:
    checked: float
    signature: tuple | None
    rows: dict
    status: str


class SteamTitleResolver:
    """One cached bounded inventory per library, shared by its running games.

    Directory or selected-manifest changes invalidate evidence immediately;
    rescans are throttled to two seconds. Other in-place manifest edits are
    discovered within 30 seconds. Positive and negative caches are bounded.
    """
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._cache = OrderedDict()
        self._lock = threading.RLock()

    def title_for(self, executable: str) -> StoreTitle:
        installation = _installation(executable)
        if installation is None:
            return StoreTitle(status="unsupported_install_path")
        exe, library, install = installation
        if _WINDOWS:
            # Run before cache signatures, resolve/stat, enumeration or file open.
            # Do not cache a safe classification: a drive/parent may have changed.
            if not _windows_preflight(str(exe), directory=False):
                return StoreTitle(status="unverified_install_root")
            exe, library, install = (Path(str(path)) for path in (exe, library, install))
        else:
            # Preserve the existing non-Windows synthetic/local path policy.
            try:
                if library.resolve(strict=True) != library or install.resolve(strict=True) != install:
                    return StoreTitle(status="unverified_install_root")
                resolved_exe = exe.resolve(strict=True)
                resolved_exe.relative_to(install)
                if not resolved_exe.is_file():
                    return StoreTitle(status="unverified_install_root")
            except (OSError, ValueError, RuntimeError):
                return StoreTitle(status="unverified_install_root")
        with self._lock:
            now = self._clock()
            try:
                signature = _signature(library)
            except OSError:
                signature = None
            cached = self._cache.get(library)
            selected = cached.rows.get(os.path.normcase(install.name)) if cached else None
            changed = cached is None or signature != cached.signature
            if selected and selected[0].name:
                try:
                    changed = changed or _signature(selected[1]) != selected[2]
                except OSError:
                    changed = True
            if cached is not None and changed and now - cached.checked < MIN_SCAN_SECONDS:
                return StoreTitle(status="metadata_changed")
            if cached is None or changed or now - cached.checked >= CACHE_SECONDS:
                cached = self._scan(library, signature, now)
                self._cache[library] = cached
                while len(self._cache) > MAX_CACHED_LIBRARIES:
                    self._cache.popitem(last=False)
            self._cache.move_to_end(library)
            row = cached.rows.get(os.path.normcase(install.name))
            return row[0] if row else StoreTitle(status=cached.status)

    def _scan(self, library: Path, signature, now: float) -> _Inventory:
        rows = {}
        total = 0
        count = 0

        def failed(status):
            # Incomplete ownership evidence cannot establish uniqueness.
            return _Inventory(now, signature, {}, status)

        try:
            if _WINDOWS and not _windows_preflight(str(library), directory=True):
                return failed("metadata_unavailable")
            with os.scandir(library) as entries:
                for scanned, entry in enumerate(entries):
                    if scanned >= MAX_ENTRIES:
                        return failed("metadata_scan_limit")
                    if not (entry.name.casefold().startswith("appmanifest_") and entry.name.casefold().endswith(".acf")):
                        continue
                    count += 1
                    if count > MAX_MANIFESTS:
                        return failed("metadata_scan_limit")
                    manifest = Path(entry.path)
                    if _WINDOWS and not _windows_preflight(str(manifest), directory=False):
                        return failed("invalid_manifest")
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        return failed("invalid_manifest")
                    before = _signature(manifest)
                    if before[2] > MAX_MANIFEST_BYTES:
                        return failed("oversized_manifest")
                    if total + before[2] > MAX_TOTAL_BYTES:
                        return failed("metadata_scan_limit")
                    with manifest.open("rb") as handle:
                        opened = os.fstat(handle.fileno())
                        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != before[:2]:
                            return failed("metadata_changed")
                        raw = handle.read(min(MAX_MANIFEST_BYTES, MAX_TOTAL_BYTES - total) + 1)
                    total += len(raw)
                    if total > MAX_TOTAL_BYTES or len(raw) > MAX_MANIFEST_BYTES:
                        return failed("metadata_scan_limit")
                    if _signature(manifest) != before:
                        return failed("metadata_changed")
                    values = parse_steam_manifest(raw, entry.name)
                    if values is None:
                        # A bad title/AppID for a structurally unambiguous other
                        # installation must not erase this installation's title.
                        fields = _manifest_fields(raw, ownership_only=True)
                        folder = fields.get("installdir") if fields else None
                        if not _valid_folder(folder):
                            return failed("invalid_manifest")
                        title = StoreTitle(status="invalid_manifest")
                    else:
                        app_id, name, folder = values
                        title = StoreTitle(name, "steam_manifest", "verified", app_id)
                    key = os.path.normcase(folder)
                    if key in rows:
                        title = StoreTitle(status="ambiguous_manifest")
                    rows[key] = (title, manifest, before)
            if _signature(library) != signature:
                return failed("metadata_changed")
        except (OSError, ValueError):
            return failed("metadata_unavailable")
        return _Inventory(now, signature, rows, "metadata_missing")


_RESOLVER = SteamTitleResolver()


def steam_title_for(executable: str) -> StoreTitle:
    return _RESOLVER.title_for(executable)
