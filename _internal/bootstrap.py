"""Managed source launcher and explicit safe diagnostics; standard library fast path."""
from __future__ import annotations
import argparse
import json
import os
import secrets
from pathlib import Path
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local"))) / "NeoJarvis"


def check_release_files(project: Path) -> bool:
    """Compatibility check only when optional legacy metadata is present."""
    from jarvis.testing.build_integrity156 import verify_release
    result = verify_release(project)
    # Current distributions intentionally carry no extra checksum manifest.
    # An explicitly supplied legacy manifest still must not mask a mismatch.
    if result['status'] in {'PASS', 'NOT_PRESENT'}:
        return True
    if sys.stdout is not None:
        print('NEO: файлы сборки не совпадают. Распакуйте весь архив в новую папку. / '
              'Release files do not match. Extract the complete archive into a new folder.')
        print('BUILD_INTEGRITY=' + json.dumps(result, ensure_ascii=False))
    return False


class ReadOnlyConfig:
    """Diagnostics must not run configuration migrations or alter user settings."""
    def __init__(self):
        from jarvis.settings_storage184 import read_settings_path
        self.layers = []
        settings = read_settings_path(DATA)
        for path in (ROOT / "config" / "default_settings.json", settings):
            try:
                self.layers.append(json.loads(path.read_text(encoding="utf-8-sig")))
            except (OSError, ValueError):
                self.layers.append({})
        self.paths = SimpleNamespace(root=DATA, settings=settings, logs=DATA / "logs")
    def get(self, key, default=None):
        for layer in reversed(self.layers):
            value = layer
            for part in key.split("."):
                if not isinstance(value, dict) or part not in value:
                    break
                value = value[part]
            else:
                return value
        return default


def _hidden_options(detached=False):
    if os.name != "nt":
        return {"start_new_session": detached}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | (subprocess.DETACHED_PROCESS if detached else subprocess.CREATE_NO_WINDOW)
    return {"creationflags": flags, "startupinfo": info}


def _wait_for_ready(proc, ready: Path, run_id: str, *, timeout: float = 90.0,
                    secret: str = "", source_app: Path | None = None,
                    launched_at: float | None = None) -> dict:
    """Observe the source UI, not just the Windows venv redirector PID."""
    from jarvis.runtime.startup_handshake import validate_ready
    deadline = time.monotonic() + timeout
    rejected = "ready_file_missing"
    observed_pid = None
    while time.monotonic() < deadline:
        try:
            if ready.stat().st_size > 16384:
                rejected = "ready_file_oversized"
            else:
                result = json.loads(ready.read_text(encoding="utf-8"))
                accepted, rejected = validate_ready(result, spawned_pid=proc.pid,
                    run_id=run_id, secret=secret, source_app=source_app, launched_at=launched_at)
                observed_pid = result.get("pid") if isinstance(result, dict) else None
                if accepted:
                    # A UI which has already exited is not a usable interface.
                    # Secondary launch acknowledgements are intentionally short-lived.
                    code = proc.poll()
                    if secret and result["state"] == "ui_event_loop_ready" and code is not None:
                        return {"ok": False, "state": "exited_after_ready", "child_pid": proc.pid,
                                "ui_pid": observed_pid, "exit_code": code}
                    output = {"ok": True, "state": result["state"], "child_pid": proc.pid}
                    if secret:
                        output.update(ui_pid=observed_pid, ready_relation=rejected,
                                      ready_at=result.get("ready_at"))
                    return output
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError):
            rejected = "ready_file_unreadable_or_invalid"
        code = proc.poll()
        if code is not None:
            return {"ok": False, "state": "exited_before_ready", "child_pid": proc.pid,
                    "exit_code": code, "ready_rejection": rejected, "reported_pid": observed_pid}
        time.sleep(0.1)
    return {"ok": False, "state": "ready_timeout", "child_pid": proc.pid,
            "ready_rejection": rejected, "reported_pid": observed_pid}


def _record_launch_status(bootstrap_dir: Path, run_id: str, **details) -> None:
    from jarvis.runtime.diagnostics import write_report
    try:
        build = (ROOT / "VERSION.txt").read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        build = "NEO (version unavailable)"
    payload = {"build": build, "run_id": run_id, "launcher_pid": os.getpid(),
               "updated_at": time.time(), **details}
    write_report(bootstrap_dir / "launch_status.json", payload)
    run_dir = bootstrap_dir / "runs" / run_id
    if run_dir.is_dir():
        write_report(run_dir / "launch_status.json", payload)


def _prune_launch_runs(state: Path, *, now: float | None = None, keep: int = 8) -> None:
    """Bound old run logs without removing active runs or unknown user files."""
    directory = state / "runs"
    if not directory.is_dir() or directory.is_symlink():
        return
    try:
        import psutil
        runs = [p for p in directory.iterdir() if p.is_dir() and not p.is_symlink()
                and len(p.name) == 32 and all(c in "0123456789abcdef" for c in p.name)]
        runs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except (ImportError, OSError):
        return
    now = time.time() if now is None else now
    known = {"ready.json", "ready.tmp", "startup_progress.json", "startup_progress.tmp",
             "launch_status.json", "launch_status.json.tmp", "startup.log"}
    for index, run in enumerate(runs):
        try:
            if index < keep and now - run.stat().st_mtime <= 7 * 86400:
                continue
            children = list(run.iterdir())
            if any(p.name not in known or p.is_symlink() or not p.is_file() for p in children):
                continue
            pids = []
            for filename in ("launch_status.json", "ready.json", "startup_progress.json"):
                path = run / filename
                if not path.exists():
                    continue
                if path.stat().st_size > 16384:
                    raise ValueError("Run metadata oversized")
                info = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(info, dict):
                    raise ValueError("Invalid run metadata")
                pids.extend(info.get(key) for key in ("pid", "child_pid", "ui_pid", "reported_pid"))
            if any(type(pid) is int and pid > 0 and psutil.pid_exists(pid) for pid in pids):
                continue
            for path in children:
                path.unlink()
            run.rmdir()
        except (OSError, ValueError, TypeError, OverflowError):
            continue


def _remove_old_voices():
    """NEO 179 speaks no more: Piper, its voices and their status (models/tts) are deleted."""
    if os.environ.get('NEO_TEST_MODE') == '1': return
    try:
        from jarvis.runtime.settings179 import remove_old_voices
        if remove_old_voices(DATA):
            print('Old voice files removed (NEO no longer speaks answers).')
    except Exception as exc:print('Old voice files kept:',type(exc).__name__)


def launch(*, run_id: str | None = None) -> int:
    if os.name != "nt":
        print("NEO desktop launch requires Windows 10/11 x64. Diagnostics remain available.")
        return 2
    from jarvis.runtime.diagnostics import redact
    state = DATA / "bootstrap"
    state.mkdir(parents=True, exist_ok=True)
    _prune_launch_runs(state)
    run_id = run_id or uuid.uuid4().hex
    if len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
        raise ValueError("Invalid launch run id")
    run_dir = state / "runs" / run_id
    run_dir.mkdir(parents=True)
    ready = run_dir / "ready.json"
    _record_launch_status(state, run_id, state="preparing_source_app")
    secret = secrets.token_urlsafe(32)
    launched_at = time.time()
    env = dict(os.environ, NEO_LAUNCH_READY_SECRET=secret, NEO_LAUNCH_RUN_ID=run_id, NEO_LAUNCH_READY_PATH=str(ready), PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    interpreter = Path(sys.executable).with_name("pythonw.exe")
    try:  # NEO 171: run the assistant as «NEO» (name and icon) instead of «Python».
        from jarvis.runtime.process_host171 import install as _install_host
        _host = _install_host(Path(sys.prefix))
        if _host is not None and _host.is_file():
            interpreter = _host
    except Exception:
        pass
    if not interpreter.is_file():
        _record_launch_status(state, run_id, state="managed_pythonw_missing", ok=False)
        print("Managed pythonw.exe is missing. Run NEO.cmd --repair.")
        return 2
    log_path = run_dir / "startup.log"
    if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
        log_path.replace(log_path.with_suffix(".previous.log"))
    with log_path.open("ab", buffering=0) as log:
        log.write(f"\n=== NEO managed source launch run={run_id} ===\n".encode())
        try:
            proc = subprocess.Popen([str(interpreter), str(ROOT / "app.py"), "--detached-launch"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, **_hidden_options(True))
        except OSError as exc:
            _record_launch_status(state, run_id, state="spawn_failed", ok=False, exception_type=type(exc).__name__)
            print("NEO could not start its managed desktop process. See bootstrap/launch_status.json.")
            return 1
        _record_launch_status(state, run_id, state="waiting_for_ui", child_pid=proc.pid)
        result = _wait_for_ready(proc, ready, run_id, secret=secret,
                                 source_app=ROOT / "app.py", launched_at=launched_at)
        _record_launch_status(state, run_id, **result)
        if result["ok"]:
            print("NEO is ready (" + result["state"] + ").")
            _remove_old_voices()
            return 0
        if result["state"] == "ready_timeout":
            print("NEO did not confirm UI readiness within 90 seconds. The process has not been killed.")
            print("Readiness diagnostic: " + str(result.get("ready_rejection")) +
                  "; spawned PID=" + str(proc.pid) + "; reported PID=" + str(result.get("reported_pid")))
        else:
            print(f"NEO exited before readiness (code {result.get('exit_code')}).")
    try:
        with log_path.open("rb") as stream:
            stream.seek(max(0, log_path.stat().st_size - 6000))
            print(redact(stream.read().decode("utf-8", errors="replace")))
    except OSError:
        pass
    print("Current launch log: " + str(log_path))
    print("Run NEO_TEST.cmd or NEO.cmd --collect-logs.")
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="NEO launcher and diagnostics")
    group = parser.add_mutually_exclusive_group()
    for arg in ("doctor", "self-test", "collect-logs", "tests", "integration-test", "atlas-test", "performance-test", "startup-test", "test-all", "nvidia-diagnostic"):
        group.add_argument("--" + arg, action="store_true")
    args = parser.parse_args(argv)
    if args.nvidia_diagnostic:
        from jarvis.integrations.nvidia_state import main as nvidia_main
        return nvidia_main(["--diagnose"], config=ReadOnlyConfig())
    if args.test_all:
        from tools.test_all import main as all_main
        return all_main([])
    if args.doctor:
        import dependency_guard
        result = {"python": sys.version, "runtime": str(Path(sys.executable).parent.parent), "source_app": str(ROOT / "app.py"), "platform": sys.platform, "missing_modules": dependency_guard.missing_specs(), "incompatible_versions": dependency_guard.incompatible_versions(), "windows_runtime": os.name == "nt", "installation_changed": False}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result["missing_modules"] and not result["incompatible_versions"] else 2
    if args.self_test:
        from jarvis.runtime.self_test import run_self_test
        DATA.mkdir(parents=True, exist_ok=True)
        result = run_self_test(ReadOnlyConfig(), quick=False)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    if args.collect_logs:
        from jarvis.runtime.diagnostics import collect_logs
        print(collect_logs(DATA))
        return 0
    if args.tests:
        from safe_test_runner import main as tests_main
        return tests_main()
    if args.atlas_test:
        from atlas_test_runner import main as atlas_main
        return atlas_main([])
    if args.startup_test:
        from tools.startup_test import main as startup_main
        return startup_main()
    if args.performance_test:
        from tools.benchmark_windows import main as performance_main
        return performance_main([])
    if args.integration_test:
        from integration_test_runner import main as integration_main
        return integration_main()
    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
