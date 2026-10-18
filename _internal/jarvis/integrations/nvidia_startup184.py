"""Opt-in launch of an already installed NVIDIA capture application.

Only fixed executable locations below Windows Program Files are considered;
no PATH search, downloaded code, service control, elevation, registry writes,
or undocumented NVIDIA command-line switches. Process presence is not proof
of overlay readiness or recording state. STARTUPINFO requests a minimized,
nonactivating first window; NVIDIA may choose different subsequent behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Callable

APP_PROCESSES = frozenset({"nvidia app.exe", "nvidia overlay.exe"})
INSTALL_PATHS = (
    ("NVIDIA App", Path("NVIDIA Corporation/NVIDIA App/CEF/NVIDIA app.exe")),
)


@dataclass(frozen=True)
class InstalledApp:
    name: str
    executable: Path


@dataclass(frozen=True)
class StartupResult:
    status: str
    app: str = ""


def application_running() -> bool | None:
    """Unknown is distinct from absent: never launch on a failed process read."""
    import psutil
    try:
        unknown = False
        for process in psutil.process_iter(["name"]):
            name = process.info.get("name")
            if name is None:
                unknown = True
            elif str(name).casefold() in APP_PROCESSES:
                return True
        return None if unknown else False
    except Exception:
        return None


def installed_app() -> InstalledApp | None:
    roots = []
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        value = os.environ.get(variable)
        if value:
            root = Path(value)
            if root.is_absolute() and root not in roots:
                roots.append(root)
    # Only the installed NVIDIA App is eligible for automatic launch.
    for name, relative in INSTALL_PATHS:
        for root in roots:
            candidate = root / relative
            if candidate.is_file():
                return InstalledApp(name, candidate)
    return None


def launch_minimized(app: InstalledApp) -> None:
    # Microsoft STARTUPINFO/ShowWindow: 7 = SW_SHOWMINNOACTIVE. This is a
    # request to the GUI application, not a guarantee about NVIDIA windows.
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 7
    subprocess.Popen([str(app.executable)], cwd=str(app.executable.parent),
                     startupinfo=startup, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     shell=False, close_fds=True)


class CaptureAppStartup:
    def __init__(self, *, running: Callable[[], bool | None] = application_running,
                 installed: Callable[[], InstalledApp | None] = installed_app,
                 launch: Callable[[InstalledApp], None] = launch_minimized,
                 is_windows: bool | None = None) -> None:
        self._running, self._installed, self._launch = running, installed, launch
        self._windows = os.name == "nt" if is_windows is None else is_windows

    def ensure_running(self, *, guard: Callable[[], bool]) -> StartupResult:
        if not guard():
            return StartupResult("cancelled")
        if not self._windows:
            return StartupResult("unsupported")
        try:
            running = self._running()
            if running is True:
                return StartupResult("already_running")
            if running is not False:
                return StartupResult("probe_unknown")
            app = self._installed()
            if app is None:
                return StartupResult("not_installed")
            # Recheck after discovery: a user/vendor startup may have won.
            running = self._running()
            if running is True:
                return StartupResult("already_running")
            if running is not False:
                return StartupResult("probe_unknown")
            if not guard():
                return StartupResult("cancelled")
            self._launch(app)
            return StartupResult("launch_requested", app.name)
        except Exception:
            # Paths and OS exception text need not enter user notifications.
            return StartupResult("launch_failed")


def launch_for_commands_enabled(config) -> bool:
    """NEO 185: the user allowed NEO to start NVIDIA App in the background for replay/clip/record commands."""
    return bool(config.get("gaming.enabled", True)) and bool(config.get("gaming.instant_replay.launch_for_commands", False))


def ensure_overlay_for_command(config, overlay: Callable[[], bool | None], *, startup: "CaptureAppStartup | None" = None,
                               sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
                               timeout: float | None = None) -> bool:
    """Start the installed NVIDIA App (minimised) when allowed and wait until its overlay runs.

    Returns True only when the overlay process is observed. Nothing happens unless the
    «launch for commands» setting is on; no install, no elevation, no NVIDIA switches.
    """
    import time
    if not launch_for_commands_enabled(config):
        return False
    result = (startup or CaptureAppStartup()).ensure_running(guard=lambda: True)
    if result.status not in {"already_running", "launch_requested"}:
        return False
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    limit = float(timeout if timeout is not None else config.get("gaming.instant_replay.overlay_wait_seconds", 25) or 25)
    deadline = clock() + max(1.0, min(90.0, limit))
    while True:
        try:
            if overlay() is True:
                return True
        except Exception:
            pass
        if clock() >= deadline:
            return False
        sleep(1.0)
