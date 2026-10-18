# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Silent Windows startup helpers for NEO.

Standard startup uses the per-user Startup folder. Priority startup uses one
interactive Task Scheduler ONLOGON task at the highest available run level.  The
task itself points to a stable hidden WScript trampoline in LOCALAPPDATA, while
the trampoline always launches the package's source-built NEO.exe. A new archive
can refresh its executable path without recreating the elevated task on every update.
"""

import json
import os
import subprocess
from pathlib import Path

TASK_NAME = r"NEO Priority"
LEGACY_TASK_NAMES = (r"NEO JARVIS Priority",)

PROJECT_SIGNATURE = "s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r"


def _clear_zone_identifier(path: Path) -> bool:
    """Best-effort removal of Windows Mark-of-the-Web from one project file."""
    if os.name != "nt":
        return False
    try:
        ads = str(path) + ":Zone.Identifier"
        os.remove(ads)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _unblock_project_tree(gui_launcher: Path) -> None:
    """Compatibility hook: preserve Windows download-origin/security metadata."""
    return


def _hidden_kwargs() -> dict:
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 1))
            si.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
            kwargs["startupinfo"] = si
        except Exception:
            pass
    return kwargs


def _startup_root() -> Path:
    root = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "NeoJarvis" / "startup"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _marker_path() -> Path:
    return _startup_root() / "priority_task.json"


def _launcher_script_path() -> Path:
    return _startup_root() / "priority_launch.vbs"


def _write_marker(target: str) -> None:
    try:
        _marker_path().write_text(
            json.dumps({"target": target, "task": TASK_NAME}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _write_hidden_launcher(gui_launcher: Path) -> Path:
    _unblock_project_tree(gui_launcher)
    target = str(gui_launcher.resolve()).replace('"', '""')
    script = _launcher_script_path()
    script.write_text(
        'Set WshShell = CreateObject("WScript.Shell")\n'
        'WshShell.Environment("PROCESS")("NEO_JARVIS_AUTOSTART") = "1"\n'
        f'WshShell.Run """{target}""", 0, False\n',
        encoding="utf-16",  # WScript requires a Unicode BOM for non-ANSI paths.
    )
    _write_marker(str(gui_launcher.resolve()))
    return script


def _task_exists(name: str = TASK_NAME) -> bool:
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", str(name)],
            check=False,
            timeout=6,
            **_hidden_kwargs(),
        )
        return completed.returncode == 0
    except Exception:
        return False


def _run_schtasks(args: list[str], *, elevated_fallback: bool) -> bool:
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["schtasks.exe", *args], check=False, timeout=20, **_hidden_kwargs()
        )
        if completed.returncode == 0:
            return True
    except Exception:
        pass
    if not elevated_fallback:
        return False

    # Creating an RL=HIGHEST task from a normal desktop process can require one
    # UAC consent. PowerShell itself is hidden; only Windows' secure UAC dialog is
    # allowed to appear.
    try:
        ps_args = ",".join("'" + part.replace("'", "''") + "'" for part in args)
        script = (
            "$p=Start-Process -FilePath 'schtasks.exe' -ArgumentList @(" + ps_args + ") "
            "-Verb RunAs -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode"
        )
        completed = subprocess.run(
            [
                "powershell.exe", "-NoLogo", "-NoProfile", "-WindowStyle", "Hidden",
                "-ExecutionPolicy", "Bypass", "-Command", script,
            ],
            check=False,
            timeout=60,
            **_hidden_kwargs(),
        )
        return completed.returncode == 0
    except Exception:
        return False


def refresh_priority_launcher(gui_launcher: Path) -> bool:
    """Refresh the current archive path without creating/elevating a task."""
    if os.name != "nt":
        return False
    if not _task_exists() and not any(_task_exists(name) for name in LEGACY_TASK_NAMES):
        return False
    try:
        _write_hidden_launcher(gui_launcher)
        return True
    except Exception:
        return False


def ensure_priority_task(gui_launcher: Path) -> bool:
    """Create the high-priority ONLOGON task or cheaply refresh its launcher."""
    if os.name != "nt":
        return False
    try:
        script = _write_hidden_launcher(gui_launcher)
    except Exception:
        return False
    if _task_exists():
        for legacy in LEGACY_TASK_NAMES:
            if _task_exists(legacy):
                _run_schtasks(["/Delete", "/F", "/TN", legacy], elevated_fallback=False)
        return True

    system_root = Path(os.getenv("SystemRoot", r"C:\Windows"))
    wscript = system_root / "System32" / "wscript.exe"
    wscript_exe = str(wscript if wscript.is_file() else Path("wscript.exe"))
    task_run = subprocess.list2cmdline([wscript_exe, str(script)])
    args = [
        "/Create", "/F", "/TN", TASK_NAME,
        "/TR", task_run,
        "/SC", "ONLOGON",
        "/RL", "HIGHEST",
        "/IT",
    ]
    created = _run_schtasks(args, elevated_fallback=True)
    if created:
        for legacy in LEGACY_TASK_NAMES:
            if _task_exists(legacy):
                _run_schtasks(["/Delete", "/F", "/TN", legacy], elevated_fallback=False)
    return created


def remove_priority_task() -> bool:
    if os.name != "nt":
        return False
    ok = True
    if _task_exists():
        ok = _run_schtasks(["/Delete", "/F", "/TN", TASK_NAME], elevated_fallback=True)
    for legacy in LEGACY_TASK_NAMES:
        if _task_exists(legacy):
            ok = _run_schtasks(["/Delete", "/F", "/TN", legacy], elevated_fallback=True) and ok
    if ok:
        for path in (_marker_path(), _launcher_script_path()):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
    return ok


WAKE_STARTUP_NAME = "NEO_WAKE.vbs"
LEGACY_WAKE_STARTUP_NAMES = ("NEO_JARVIS_WAKE.vbs",)
WAKE_MARKER_NAME = "wake_service.json"
WAKE_TASK_NAME = r"NEO Wake Service"
LEGACY_WAKE_TASK_NAMES = (r"NEO JARVIS Wake Service",)

def _standard_startup_dir() -> Path:
    return Path(os.getenv("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"

def _wake_marker_path() -> Path:
    return _startup_root() / WAKE_MARKER_NAME

def _wake_startup_path() -> Path:
    return _standard_startup_dir() / WAKE_STARTUP_NAME

def _write_wake_marker(wake_executable: Path, gui_launcher: Path) -> None:
    try:
        _wake_marker_path().write_text(json.dumps({
            "target": str(gui_launcher.resolve()),
            "wake_executable": str(wake_executable.resolve()),
            "project_signature": PROJECT_SIGNATURE,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def ensure_wake_service(wake_executable: Path, gui_launcher: Path, *, enabled: bool) -> bool:
    """Install/refresh the tiny owner-only Telegram wake companion.

    The wake process is separate from the heavy JARVIS runtime. It never consumes
    Bot API updates while the primary JARVIS instance mutex exists; after JARVIS
    shuts down it exposes only Start/Stop confirmation buttons to the paired owner.
    A stable LOCALAPPDATA marker lets an already-running old wake process launch
    the newest extracted archive after an update.
    """
    if os.name != "nt":
        return False
    try:
        startup = _standard_startup_dir()
        startup.mkdir(parents=True, exist_ok=True)
        script = _wake_startup_path()
        if not enabled:
            script.unlink(missing_ok=True)
            for legacy_name in LEGACY_WAKE_STARTUP_NAMES:
                try:
                    (startup / legacy_name).unlink(missing_ok=True)
                except Exception:
                    pass
            try:
                _run_schtasks(["/Delete", "/F", "/TN", WAKE_TASK_NAME], elevated_fallback=False)
                for legacy_task in LEGACY_WAKE_TASK_NAMES:
                    _run_schtasks(["/Delete", "/F", "/TN", legacy_task], elevated_fallback=False)
            except Exception:
                pass
            _wake_marker_path().unlink(missing_ok=True)
            # The checkbox means "inactive", not merely "do not autostart next
            # time". Terminate the already-running singleton companion as well.
            # taskkill is intentionally best-effort: no process is also success.
            try:
                subprocess.run(
                    ["taskkill.exe", "/F", "/IM", wake_executable.name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=4.0, check=False, **_hidden_kwargs(),
                )
            except Exception:
                pass
            return True
        if not wake_executable.is_file() or not gui_launcher.is_file():
            return False
        _unblock_project_tree(gui_launcher)
        _clear_zone_identifier(wake_executable.resolve())
        _write_wake_marker(wake_executable, gui_launcher)
        target = str(wake_executable.resolve()).replace('"', '""')
        # Prefer a normal per-user ONLOGON task so the wake companion is alive
        # even before Explorer/Startup-folder processing. It is deliberately
        # LIMITED (not elevated) and needs no UAC. Startup VBS remains a fallback
        # on systems where Task Scheduler creation is restricted.
        task_run = subprocess.list2cmdline([str(wake_executable.resolve())])
        task_ready = _run_schtasks([
            "/Create", "/F", "/TN", WAKE_TASK_NAME, "/TR", task_run,
            "/SC", "ONLOGON", "/RL", "LIMITED", "/IT",
        ], elevated_fallback=False)
        if task_ready:
            script.unlink(missing_ok=True)
            for legacy_name in LEGACY_WAKE_STARTUP_NAMES:
                try:
                    (startup / legacy_name).unlink(missing_ok=True)
                except Exception:
                    pass
            for legacy_task in LEGACY_WAKE_TASK_NAMES:
                try:
                    _run_schtasks(["/Delete", "/F", "/TN", legacy_task], elevated_fallback=False)
                except Exception:
                    pass
        else:
            script.write_text(
                'Set WshShell = CreateObject("WScript.Shell")\n'
                f'WshShell.Run """{target}""", 0, False\n',
                encoding="utf-16",
            )
        # Start it immediately for the current Windows session. A stable native
        # mutex makes this idempotent when Settings are saved repeatedly.
        try:
            subprocess.Popen([str(wake_executable.resolve())], cwd=str(wake_executable.resolve().parent), **_hidden_kwargs())
        except Exception:
            pass
        return True
    except Exception:
        return False
