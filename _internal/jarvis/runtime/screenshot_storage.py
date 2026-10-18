"""Native-quality screenshots grouped by process, with collision-safe filenames."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import uuid


def safe_application_name(process_name: str) -> str:
    name = str(process_name or "Desktop").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"\.exe$", "", name, flags=re.I)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" ._")[:72] or "Desktop"
    if re.fullmatch(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])", name, re.I):
        name = "app_" + name
    return name


def screenshot_path(root: Path, process_name: str = "", *, label: str = "screen") -> Path:
    folder = Path(root) / safe_application_name(process_name)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    clean_label = safe_application_name(label)
    return folder / f"NEO_{clean_label}_{stamp}_{uuid.uuid4().hex[:8]}.png"


# HIST-066: one placement/lookup contract for every consumer. Producers write to
# <root>/<Application>/; older builds wrote to <root>/ directly (kept as is, no
# migration). Lookups therefore cover the root plus one level of app folders.
_AUTO_PREFIXES = ("NEO_screen_", "JARVIS_screen_", "screen_")


def screenshot_files(root: Path, pattern: str = "*.png") -> list[Path]:
    base = Path(root)
    found: list[Path] = []
    try:
        found.extend(p for p in base.glob(pattern) if p.is_file())
        for folder in base.iterdir():
            if folder.is_dir() and not folder.name.startswith("."):
                found.extend(p for p in folder.glob(pattern) if p.is_file())
    except OSError:
        pass
    return found


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def latest_screenshot(root: Path) -> Path | None:
    shots = screenshot_files(root)
    return max(shots, key=_mtime) if shots else None


def prune_auto_screenshots(root: Path, keep: int = 30) -> int:
    """Keep only the newest ``keep`` automatic screen captures (root and app folders)."""
    shots = [p for p in screenshot_files(root) if p.name.startswith(_AUTO_PREFIXES)]
    shots.sort(key=_mtime, reverse=True)
    removed = 0
    for old in shots[max(0, int(keep)):]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed
