"""Lossless NEO settings relocation, atomic replacement and local rollback.

Only NEO-owned profile files are touched. A legacy settings.json is never removed
or overwritten by migration. Canonical settings always win after migration.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

SETTINGS_FOLDER = "настройки"


def valid_settings(path: Path) -> bool:
    try:
        return isinstance(json.loads(path.read_text(encoding="utf-8-sig")), dict)
    except (OSError, ValueError, UnicodeError):
        return False


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="settings.", suffix=".tmp", dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def canonical_settings_path(root: Path) -> Path:
    return Path(root) / SETTINGS_FOLDER / "settings.json"


def read_settings_path(root: Path) -> Path:
    """Read-only tools use the same precedence without creating or migrating files."""
    canonical = canonical_settings_path(root)
    if valid_settings(canonical):
        return canonical
    for candidate in (canonical.with_name("settings.last-good.json"),
                      Path(root) / "settings.json",
                      canonical.with_name("settings.migration-backup.json")):
        if valid_settings(candidate):
            return candidate
    return canonical


def prepare_settings(root: Path, defaults: Path, *, fresh_model_offer: bool = False) -> Path:
    """Copy legacy data exactly once; preserve both sides of a conflict.

    If the new directory is unavailable, keep using a valid legacy profile. Never
    replace that legacy profile with defaults merely because migration failed.
    """
    target = canonical_settings_path(root)
    legacy = Path(root) / "settings.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return target
        last_good = target.with_name("settings.last-good.json")
        if valid_settings(last_good):
            atomic_write(target, last_good.read_bytes())
            return target
        if valid_settings(legacy):
            original = legacy.read_bytes()
            backup = target.with_name("settings.migration-backup.json")
            if not backup.exists():
                atomic_write(backup, original)
            atomic_write(target, original)
        elif valid_settings(target.with_name("settings.migration-backup.json")):
            atomic_write(target, target.with_name("settings.migration-backup.json").read_bytes())
        elif legacy.exists():
            # Copy malformed bytes too, so recovery can quarantine the original
            # rather than pretending the old profile did not exist.
            atomic_write(target, legacy.read_bytes())
        else:
            content = defaults.read_bytes()
            # Only a truly new profile receives onboarding. Recovery copies,
            # including an old chosen model, must never be treated as new users.
            if fresh_model_offer and not any(target.parent.glob("settings.*.json")):
                data = json.loads(content.decode("utf-8-sig"))
                data.setdefault("local_chat", {})["setup_offer_pending184"] = True
                content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            atomic_write(target, content)
        return target
    except OSError:
        if valid_settings(legacy):
            return legacy
        raise


def snapshot_settings(path: Path) -> Path | None:
    if not valid_settings(path):
        return None
    backup = path.with_name("settings.last-good.json")
    atomic_write(backup, path.read_bytes())
    return backup


def _read_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def recover_settings(path: Path, root: Path, defaults: Path, *, observed: bytes | None = None,
                     check_generation: bool = False) -> tuple[str, str]:
    """Quarantine broken bytes, then restore the newest valid local recovery copy.

    186: with ``check_generation`` the caller passes the bytes it failed to load
    (``observed``). When a later writer has already replaced them, the newer
    profile is kept and ``("", str(path))`` is returned; the restore itself is
    re-checked against the generation just before it replaces the primary.
    """
    damaged = _read_or_none(path)
    if check_generation and damaged != observed:
        return "", str(path)
    quarantine = path.with_name(f"settings.damaged-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}.json")
    # Copy rather than move first: a failed recovery leaves the original intact.
    if damaged is not None:
        atomic_write(quarantine, damaged)
    else:
        quarantine = None
    candidates = (path.with_name("settings.last-good.json"), Path(root) / "settings.json",
                  path.with_name("settings.migration-backup.json"), defaults)
    for source in candidates:
        if source != path and valid_settings(source):
            payload = source.read_bytes()
            if check_generation and _read_or_none(path) != damaged:
                return str(quarantine or ""), str(path)
            atomic_write(path, payload)
            return str(quarantine or ""), str(source)
    raise ValueError("No valid NEO settings recovery source")


def rollback_settings(path: Path, backup: Path | None = None) -> Path:
    """Explicit rollback; keep the displaced current profile for undo/diagnosis."""
    source = Path(backup) if backup is not None else path.with_name("settings.last-good.json")
    if not valid_settings(source):
        raise ValueError("The settings backup is missing or invalid")
    if source.resolve() == path.resolve():
        raise ValueError("Choose a settings backup, not the active settings file")
    payload = source.read_bytes()
    if path.exists():
        atomic_write(path.with_name(f"settings.before-rollback-{time.time_ns()}.json"), path.read_bytes())
    atomic_write(path, payload)
    return path
