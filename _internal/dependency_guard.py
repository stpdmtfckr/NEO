# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Fast launcher guard for the persistent NEO JARVIS Python runtime.

HOTFIX60 fixes a subtle release-update slowdown: older builds stored
`.runtime_state.json` next to the unpacked release. Every new HOTFIX therefore
looked like a brand-new dependency contract and ran the full pip/bootstrap path,
even though the actual Python runtime lives persistently under LOCALAPPDATA.

The marker now lives *inside the managed runtime itself*. Normal launches use
`importlib.util.find_spec` instead of importing heavyweight packages. If the
runtime already contains the required modules but has no HOTFIX60 marker, the
first check adopts it in milliseconds instead of reinstalling everything.
"""

import argparse
import os
import hashlib
import importlib.util
import importlib.metadata
import json
import platform
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQ = ROOT / "requirements.txt"
RUNTIME_ROOT = Path(os.environ["NEO_RUNTIME_ROOT"]) if os.environ.get("NEO_PACKAGE_ROOT") else Path(sys.executable).resolve().parent.parent
STATE = RUNTIME_ROOT / ".neo_jarvis_runtime_state.json"
LEGACY_STATE = ROOT / ".runtime_state.json"

CRITICAL_IMPORTS = (
    "PySide6", "requests", "psutil", "numpy", "sounddevice",
    "faster_whisper", "mss", "PIL", "yandex_music", "discord", "nacl", "davey", "imageio_ffmpeg",
    "yt_dlp", "yt_dlp_ejs",
    "packaging", "watchdog",
)
WINDOWS_CRITICAL_IMPORTS = (
    "pywinauto", "winsdk", "comtypes", "pycaw", "pythoncom",
)


def runtime_python_ok() -> bool:
    return sys.version_info[:2] == (3, 12) and struct.calcsize("P") * 8 == 64


def requirements_hash() -> str:
    try:
        return hashlib.sha256(REQ.read_bytes()).hexdigest()
    except OSError:
        return ""


def required_names() -> list[str]:
    names = list(CRITICAL_IMPORTS)
    if platform.system().casefold() == "windows":
        names.extend(WINDOWS_CRITICAL_IMPORTS)
    return names


def missing_specs() -> list[str]:
    """Cheap presence check; deliberately does not import heavy native modules."""
    missing: list[str] = []
    for name in required_names():
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except Exception:
            missing.append(name)
    return missing


def incompatible_versions() -> list[str]:
    """Read wheel metadata without importing Qt/audio DLLs on the launch fast path."""
    try:
        from packaging.requirements import Requirement
    except ImportError:
        return ["packaging is missing"]
    failures = []
    for raw in REQ.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            version = importlib.metadata.version(requirement.name)
            if requirement.specifier and version not in requirement.specifier:
                failures.append(f"{requirement.name} {version} does not satisfy {requirement.specifier}")
        except importlib.metadata.PackageNotFoundError:
            failures.append(f"{requirement.name} is not installed")
    return failures


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_state(current: str, *, adopted: bool = False) -> int:
    payload = {
        "requirements_sha256": current,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "runtime_root": str(RUNTIME_ROOT),
        "verified_at": time.time(),
        "adopted_existing_runtime": bool(adopted),
    }
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"Could not write persistent runtime marker: {exc}")
        return 4
    return 0


def check() -> int:
    if not runtime_python_ok():
        print(f"NEO runtime: Python {sys.version.split()[0]} is not managed Python 3.12 x64; repair is required.")
        return 5

    current = requirements_hash()
    if not current:
        print("NEO runtime: requirements.txt is unavailable; repair is required.")
        return 3

    missing = missing_specs() + incompatible_versions()
    if missing:
        print("NEO runtime: missing modules: " + ", ".join(missing))
        return 2

    persistent = _read_state(STATE)
    if persistent:
        if persistent.get("requirements_sha256") == current:
            print("NEO runtime: dependencies are ready (persistent fast marker).")
            return 0
        # A persistent marker from the same managed runtime means the dependency
        # contract really changed, not merely the unpacked HOTFIX directory.
        print("NEO runtime: dependency contract changed; repair is required once.")
        return 3

    legacy = _read_state(LEGACY_STATE)
    # First HOTFIX60 launch over an already healthy v4.0.20 runtime. Either
    # migrate the old release-local marker or, if the archive has no marker,
    # adopt the discoverable installed modules. No pip/network work is needed.
    adopted = bool(legacy.get("requirements_sha256") == current) or not persistent
    rc = write_state(current, adopted=adopted)
    if rc == 0:
        print("NEO runtime: existing runtime adopted; full dependency reinstall skipped.")
    return rc


def mark_ok() -> int:
    if not runtime_python_ok():
        print(f"Cannot mark runtime ready; Python {sys.version.split()[0]} is not Python 3.12 x64.")
        return 5
    missing = missing_specs() + incompatible_versions()
    if missing:
        print("Cannot mark runtime ready; missing modules: " + ", ".join(missing))
        return 2
    rc = write_state(requirements_hash(), adopted=False)
    if rc == 0:
        print("NEO runtime: persistent dependency marker updated.")
    return rc


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--mark-ok", action="store_true")
    args = parser.parse_args()
    return check() if args.check else mark_ok()


if __name__ == "__main__":
    raise SystemExit(main())
