"""Bounded startup source identity, separate from the declared archive identity.

This is a diagnostic, not an authenticity check. Only SUBSET_FILES are freshly
read at startup; the rest of the archive is never claimed to be verified here.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import stat

METADATA_NAME = "BUILD_IDENTITY.json"
PAYLOAD_SCOPE = "archive-payload-v1"
SUBSET_SCOPE = "startup-critical-v1"
SUBSET_FILES = (
    "app.py",
    "jarvis/runtime/build_identity.py",
    "jarvis/runtime/windows_hotkeys.py",
    "jarvis/ui/main_window.py",
    "jarvis/voice.py",
    "jarvis/wake_matcher.py",
)
MAX_METADATA_BYTES = 16 * 1024
MAX_SOURCE_FILE_BYTES = 1024 * 1024
MAX_SUBSET_BYTES = 4 * 1024 * 1024
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_FIELDS = {"schema", "revision", "payload_scope", "payload_sha256",
           "subset_scope", "subset_files"}
_SAFE_DIR_FD = (os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")
                and hasattr(os, "O_DIRECTORY"))


class SafeOpenUnavailable(OSError):
    """This platform cannot currently open the diagnostic source safely."""


class _WindowsFileApi:
    """Small read-only Win32 bridge; no handle is read before validation."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        import msvcrt
        self.ctypes, self.msvcrt = ctypes, msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        def bind(name, arguments, result):
            function = getattr(kernel, name)
            function.argtypes, function.restype = arguments, result
            return function
        handle, dword, pointer = wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p
        self.create = bind("CreateFileW", [wintypes.LPCWSTR, dword, dword, pointer, dword, dword, handle], handle)
        self.close = bind("CloseHandle", [handle], wintypes.BOOL)
        self.file_type = bind("GetFileType", [handle], dword)
        self.information = bind("GetFileInformationByHandleEx", [handle, ctypes.c_int, pointer, dword], wintypes.BOOL)
        self.final_name = bind("GetFinalPathNameByHandleW", [handle, wintypes.LPWSTR, dword, dword], dword)
        self.invalid_handle = ctypes.c_void_p(-1).value

    def open(self, path):
        # GENERIC_READ, share read/write/delete, OPEN_EXISTING; open the final
        # reparse point itself so it can be rejected without following it.
        handle = self.create(path, 0x80000000, 7, None, 3, 0x02200000, None)
        if handle == self.invalid_handle:
            if self.ctypes.get_last_error() in {2, 3}:
                raise FileNotFoundError("unavailable")
            raise OSError("unavailable")
        return handle

    def regular(self, handle):
        # FILE_ATTRIBUTE_TAG_INFO is two DWORDs; FileAttributeTagInfo == 9.
        attributes = (self.ctypes.c_uint32 * 2)()
        if self.file_type(handle) != 1 or not self.information(handle, 9, attributes, 8):
            return False
        return not attributes[0] & (0x400 | 0x10)  # reparse point or directory

    def final_path(self, handle):
        buffer = self.ctypes.create_unicode_buffer(32768)
        length = self.final_name(handle, buffer, len(buffer), 0)
        if not 0 < length < len(buffer):
            raise OSError("unavailable")
        return buffer.value

    def to_fd(self, handle):
        # Successful conversion transfers handle ownership to the descriptor.
        return self.msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)


def _windows_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normpath(value)


def _open_windows_file(path: str, api=None) -> int:
    if api is None:
        try:
            api = _WindowsFileApi()
        except (AttributeError, ImportError) as exc:
            raise SafeOpenUnavailable("safe_open_unavailable") from exc
    handle = api.open(path)
    try:
        # Compare the exact normalized final path, not a prefix or unverified
        # pathname. Redirected ancestors are detected before content is read.
        if not api.regular(handle) or _windows_path(api.final_path(handle)) != _windows_path(path):
            raise ValueError("unavailable")
        descriptor = api.to_fd(handle)
        handle = None
        return descriptor
    finally:
        if handle is not None:
            api.close(handle)


def manifest_fingerprint(files: dict[str, str]) -> str:
    """Digest sorted UTF-8 relative paths plus their lowercase SHA256s.

    Packaging excludes the identity metadata itself to avoid circular hashes.
    Callers validate the path/digest map before using this shared encoding.
    """
    digest = hashlib.sha256(b"NEO-source-manifest-v1\n")
    for name, sha256 in sorted(files.items()):
        digest.update(name.encode("utf-8") + b"\0" + sha256.encode("ascii") + b"\n")
    return digest.hexdigest()


def _validate_metadata(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("invalid_metadata")
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise ValueError("invalid_metadata")
    revision = value["revision"]
    payload_sha256 = value["payload_sha256"]
    if (not isinstance(revision, str) or not _REVISION.fullmatch(revision)
            or not isinstance(payload_sha256, str) or not _SHA256.fullmatch(payload_sha256)
            or value["payload_scope"] != PAYLOAD_SCOPE
            or value["subset_scope"] != SUBSET_SCOPE):
        raise ValueError("invalid_metadata")
    files = value["subset_files"]
    if not isinstance(files, dict) or set(files) != set(SUBSET_FILES):
        raise ValueError("invalid_metadata")
    if any(not isinstance(item, str) or not _SHA256.fullmatch(item) for item in files.values()):
        raise ValueError("invalid_metadata")
    return value


def metadata_bytes(revision: str, payload_files: dict[str, str]) -> bytes:
    """Packager API: build metadata from hashes of the actual archived bytes."""
    if "_internal/" + METADATA_NAME in payload_files:
        raise ValueError("identity_metadata_must_be_excluded")
    value = {
        "schema": 1,
        "revision": revision,
        "payload_scope": PAYLOAD_SCOPE,
        "payload_sha256": manifest_fingerprint(payload_files),
        "subset_scope": SUBSET_SCOPE,
        "subset_files": {name: payload_files["_internal/" + name] for name in SUBSET_FILES},
    }
    _validate_metadata(value)
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _open_beneath(root: Path, name: str) -> int:
    """Open safely with Win32 validation or POSIX descriptor-relative traversal."""
    if os.name == "nt":
        return _open_windows_file(os.path.abspath(root / name))
    if not _SAFE_DIR_FD:
        raise SafeOpenUnavailable("safe_open_unavailable")
    absolute = Path(os.path.abspath(root))
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(absolute.anchor, directory_flags)
    try:
        for part in (*absolute.parts[1:], *Path(name).parts[:-1]):
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(Path(name).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=directory)
    finally:
        os.close(directory)


def _read_bounded(root: Path, name: str, limit: int) -> bytes:
    """Validate the opened descriptor before reading at most limit + 1 bytes."""
    descriptor = _open_beneath(root, name)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("unavailable")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
    finally:
        os.close(descriptor)
    if len(data) > limit:
        raise ValueError("unavailable")
    return data


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("invalid_metadata")
        value[key] = item
    return value


def read_startup_identity(internal_root: Path) -> dict:
    """Return only allowlisted fields and fixed error codes, never file content."""
    root = Path(internal_root)
    result = {
        "packaged_status": "missing",
        "packaged_revision": "unavailable",
        "packaged_payload_sha256": "unavailable",
        "payload_verification": "not_checked_at_startup",
        "subset_scope": SUBSET_SCOPE,
        "subset_status": "unavailable",
        "local_subset_sha256": "unavailable",
        "subset_files": 0,
        "subset_bytes": 0,
    }
    metadata = None
    try:
        raw = _read_bounded(root, METADATA_NAME, MAX_METADATA_BYTES)
        metadata = _validate_metadata(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object))
        result.update(packaged_status="declared", packaged_revision=metadata["revision"],
                      packaged_payload_sha256=metadata["payload_sha256"])
    except FileNotFoundError:
        pass
    except SafeOpenUnavailable:
        result["packaged_status"] = "unavailable"
    except (OSError, ValueError, TypeError, RecursionError):
        result["packaged_status"] = "invalid"
    actual = {}
    try:
        for name in SUBSET_FILES:
            remaining = MAX_SUBSET_BYTES - result["subset_bytes"]
            data = _read_bounded(root, name, min(MAX_SOURCE_FILE_BYTES, remaining))
            actual[name] = hashlib.sha256(data).hexdigest()
            result["subset_bytes"] += len(data)
            result["subset_files"] += 1
        result["local_subset_sha256"] = manifest_fingerprint(actual)
        result["subset_status"] = ("match" if actual == metadata["subset_files"] else "mismatch") if metadata else "uncompared"
    except (OSError, ValueError):
        pass
    return result


def startup_identity_receipt(internal_root: Path) -> tuple[dict, str]:
    """One bounded read shared by startup text and parent process attribution."""
    try:
        result = read_startup_identity(internal_root)
        return result, "NEO source identity: " + " ".join(f"{key}={value}" for key, value in result.items())
    except Exception:
        return {}, "NEO source identity: packaged_status=unavailable subset_status=unavailable reason=diagnostic_error"


def startup_identity_line(internal_root: Path) -> str:
    """One startup line; an unexpected diagnostic error must not stop NEO."""
    return startup_identity_receipt(internal_root)[1]
