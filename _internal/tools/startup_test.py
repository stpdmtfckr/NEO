"""One-click startup diagnosis; never dispatches atlas actions into the live PC.

Starts the normal NEO only after checking that no previous instance owns its
lock. Leaves the UI running. Probes public API endpoints without bot tokens,
keeps old sessions separate, and exports a bounded allowlisted diagnostic ZIP.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis.runtime.diagnostics import redact, write_report

MAX_FILE = 2 * 1024 * 1024
MAX_BUNDLE = 12 * 1024 * 1024
OBSERVE_SECONDS = 45
DIAGNOSTIC_REVISION = "149"


def network_probe(target: str) -> dict:
    """Endpoint reachability only: HTTP success is NOT bot authentication."""
    urls = {"telegram": "https://api.telegram.org/", "discord": "https://discord.com/api/v10/gateway",
            "local_vision": "http://127.0.0.1:11434/api/version"}
    url = urls[target]
    started = time.monotonic()
    result = {"target": target, "url": url, "authenticated": False,
              "meaning": "public HTTP reachability, not Gateway/voice/bot login"}
    try:
        if target == "discord":
            import asyncio
            import aiohttp
            async def fetch() -> int:
                timeout = aiohttp.ClientTimeout(total=7, connect=4)
                async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                    async with session.get(url, allow_redirects=False) as response:
                        return response.status
            result["transport"] = "aiohttp_direct_as_used_by_discord_client"
            result["http_status"] = asyncio.run(fetch())
        else:
            import requests
            with requests.Session() as session:
                # Match requests' proxy route without reading ~/.netrc credentials.
                proxies = requests.utils.get_environ_proxies(url) if target == "telegram" else {}
                session.trust_env = False
                verify = (os.getenv("REQUESTS_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE") or True)
                result["proxy_configured"] = bool(proxies)
                result["transport"] = "requests_environment_proxy_no_netrc" if target == "telegram" else "requests_loopback_direct"
                with session.get(url, timeout=(4, 5), stream=True, allow_redirects=False,
                                 proxies=proxies, verify=verify) as response:
                    result["http_status"] = response.status_code
        result["status"] = "HTTP_RESPONSE"
    except Exception as exc:
        result.update(status="UNAVAILABLE", error_type=type(exc).__name__, detail=redact(str(exc))[:1600])
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


def run_owned_helper(command: list[str], *, env: dict | None = None, timeout: float = 15) -> dict:
    """Only this function's own diagnostic helper may be terminated on timeout."""
    from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
    proc = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            **hidden_process_kwargs())
    try:
        output, _ = proc.communicate(timeout=timeout)
        return {"exit_code": proc.returncode, "output": redact(output.decode("utf-8", errors="replace"))[-MAX_FILE:]}
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        # Windows venv python.exe may be a redirector. Drain only its verified
        # live descendants; never search by name or touch the normal NEO process.
        children = []
        try:
            import psutil
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            _, alive = psutil.wait_procs(children, timeout=1)
            for child in alive:
                try:
                    child.kill()
                except psutil.Error:
                    pass
        except (ImportError, OSError):
            pass
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()
        try:
            output, _ = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            output = b"Helper output unavailable after timeout"
        if isinstance(exc, KeyboardInterrupt):
            raise
        return {"exit_code": 124, "output": redact(output.decode("utf-8", errors="replace"))[-MAX_FILE:],
                "timed_out": True}


def _read_json(path: Path) -> dict:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_FILE:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}



def normalize_log_newlines(text: str) -> str:
    """Canonical text only; offsets and source files remain byte-based.

    Binary capture preserves Windows CRLF. Re-exporting it with an implicit
    text-mode newline adds a second CR on Windows. Normalize only AFTER reading
    the bounded byte interval, and write the diagnostic copy with explicit LF.
    Empty lines and all non-newline characters are retained.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def write_log_report(path: Path, text: str) -> None:
    """Write a redacted diagnostic copy without platform newline translation."""
    path.write_text(normalize_log_newlines(redact(text)), encoding="utf-8", newline="\n")


def diagnostic_summary(report: dict) -> dict:
    """Separate UI readiness, test failures and unauthenticated network probes.

    A network warning is never promoted to successful bot authentication. Nor
    does a failed regression test imply that the normal UI failed to start.
    The caller's actual exit code is retained, not recomputed or suppressed.
    """
    status = report.get("status", "UNKNOWN")
    test_exit = report.get("focused_tests_exit_code")
    tests = "NOT_RUN" if test_exit is None else "PASS" if test_exit == 0 else "FAIL"
    if report.get("launch_exit_code") == 0:
        ui = "READY" if report.get("ui_ping") is True else "PING_UNCONFIRMED"
    elif report.get("launch_exit_code") is not None:
        ui = "NOT_READY"
    else:
        ui = "NOT_CHECKED"
    reasons = []
    if tests == "FAIL":
        reasons.append("focused_tests_failed")
    if ui == "NOT_READY":
        reasons.append("launch_not_ready")
    elif ui == "PING_UNCONFIRMED":
        reasons.append("ui_ping_unconfirmed")
    if status in {"DIAGNOSTIC_ERROR", "INTERRUPTED", "BLOCKED_EXISTING_INSTANCE"}:
        reasons.append(status.lower())
    network = report.get("network_summary", {})
    warnings = [target for target in ("telegram", "discord", "local_vision")
                if network.get(target, "NOT_RUN") not in {"HTTP_RESPONSE", "NOT_RUN", "NOT_CHECKED", "SKIPPED"}]
    if report.get("exit_code") == 0 and ui == "READY" and tests == "PASS":
        overall = "PASS_WITH_NETWORK_WARNINGS" if warnings else "PASS"
    else:
        overall = status if status in {"DIAGNOSTIC_ERROR", "INTERRUPTED", "BLOCKED_EXISTING_INSTANCE"} else "NEEDS_ANALYSIS"
    return {"ui_status": ui, "safe_tests": tests, "overall_status": overall,
            "failure_reasons": reasons, "network_warning_targets": warnings}


def print_diagnostic_summary(report: dict) -> None:
    summary = report["summary"]
    print("\n=== NEO startup diagnostic summary ===", flush=True)
    print("UI_STATUS=" + summary["ui_status"], flush=True)
    print("SAFE_TESTS=" + summary["safe_tests"], flush=True)
    for target in ("telegram", "discord", "local_vision"):
        print("NETWORK_" + target.upper() + "=" + report.get("network_summary", {}).get(target, "NOT_RUN"), flush=True)
    print("OVERALL_STATUS=" + summary["overall_status"], flush=True)
    print("DIAGNOSTIC_REASONS=" + (",".join(summary["failure_reasons"]) or "none"), flush=True)
    print("DIAGNOSTIC_EXIT_CODE=" + str(report["exit_code"]), flush=True)
    if summary["safe_tests"] == "FAIL":
        print("Regression check failed: see focused_tests.log. UI status is independent.", flush=True)
    if summary["network_warning_targets"]:
        print("Network warnings are separate from UI/tests. HTTP_RESPONSE does not verify bot login or playback.", flush=True)


def read_new_log(path: Path, offset: int, identity: tuple[int, int] | None = None) -> str:
    """Read this interval only, with bounded tail and rotation awareness."""
    try:
        if not path.is_file() or path.is_symlink():
            return ""
        stat = path.stat()
        if identity is not None and (stat.st_dev, stat.st_ino) != identity:
            offset = 0
        start = max(0, min(offset, stat.st_size), stat.st_size - MAX_FILE)
        with path.open("rb") as stream:
            stream.seek(start)
            return redact(normalize_log_newlines(stream.read(MAX_FILE).decode("utf-8", errors="replace")))
    except OSError as exc:
        return "Log unavailable: " + type(exc).__name__


def bundle_reports(report_dir: Path, destination: Path) -> Path:
    """Only explicitly generated report files, not arbitrary profile contents."""
    allowed = {"report.json", "environment.json", "launch_status.json", "startup_progress.json",
               "ready_receipt.json", "startup.log", "application_interval.log", "focused_tests.log",
               "network_telegram.json", "network_discord.json", "network_local_vision.json", "README.txt"}
    destination.parent.mkdir(parents=True, exist_ok=True)
    entries, size = [], 0
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(allowed):
            path = report_dir / name
            if not path.is_file() or path.is_symlink():
                continue
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE)
            if size + len(raw) > MAX_BUNDLE:
                continue
            archive.writestr(name, raw)
            entries.append({"file": name, "bytes": len(raw)})
            size += len(raw)
        archive.writestr("manifest.json", json.dumps({"version": 1, "files": entries,
            "excluded": ["settings", "databases", "audio", "screenshots", "environment values", "bot tokens", "readiness secret"],
            "note": "Logs can contain command text. Review before sharing."}, indent=2))
    return destination


def _environment() -> dict:
    versions = {}
    for package in ("PySide6", "discord.py", "aiohttp", "requests", "psutil", "pytest", "PyNaCl"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not_installed"
    return {"python": sys.version, "platform": sys.platform, "packages": versions,
            "build": (ROOT / "VERSION.txt").read_text(encoding="utf-8").splitlines()[0],
            "proxy_variables_present": {name: bool(os.getenv(name)) for name in
                ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "REQUESTS_CA_BUNDLE")}}


def _is_windows() -> bool:
    return os.name == "nt"


def main() -> int:
    if not _is_windows():
        print("The live startup test requires Windows. Unit tests can run on other systems.")
        return 2
    import bootstrap
    from jarvis.runtime.instance_coordinator import InstanceCoordinator
    data = bootstrap.DATA
    run_id = uuid.uuid4().hex
    report_dir = data / "diagnostics" / "startup_runs" / run_id
    report_dir.mkdir(parents=True)
    source_run = data / "bootstrap" / "runs" / run_id
    destination = data / "diagnostics" / (time.strftime("NEO_startup_149_%Y%m%d_%H%M%S_") + run_id[:8] + ".zip")
    report = {"run_id": run_id, "started_at": time.time(), "diagnostic_revision": DIAGNOSTIC_REVISION,
              "network_summary": {}, "scope": "startup and public connectivity only",
              "status": "RUNNING", "no_live_atlas_actions": True, "assistant_left_running": True}
    app_log = data / "logs" / "neo_jarvis.log"
    try:
        stat = app_log.stat()
        offset, identity = stat.st_size, (stat.st_dev, stat.st_ino)
    except OSError:
        offset, identity = 0, None
    exit_code = 1
    try:
        write_report(report_dir / "environment.json", _environment())
        coordinator = InstanceCoordinator()
        if coordinator.has_primary():
            report.update(status="BLOCKED_EXISTING_INSTANCE", existing_pid=coordinator.primary_pid())
            print("Close the previous NEO via its tray menu, then run this CMD again. No process was killed.")
            exit_code = 2
        else:
            print("[1/4] Safe regression tests in a temporary profile (no real PC actions)...", flush=True)
            with tempfile.TemporaryDirectory(prefix="neo-startup-test-") as temporary:
                env = dict(os.environ, LOCALAPPDATA=str(Path(temporary) / "Local"),
                           APPDATA=str(Path(temporary) / "Roaming"), NEO_TEST_MODE="1",
                           PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1",
                           PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
                command = [sys.executable, "-m", "pytest", "-q", "--tb=short",
                    str(ROOT / "tests/test_neo147_startup.py"), str(ROOT / "tests/test_neo147_gateway.py"),
                    str(ROOT / "tests/test_neo147_diagnostics.py")]
                test_result = run_owned_helper(command, env=env, timeout=90)
                write_log_report(report_dir / "focused_tests.log", test_result["output"])
                report["focused_tests_exit_code"] = test_result["exit_code"]
            print("[2/4] Starting normal NEO; waiting for authenticated UI readiness...", flush=True)
            start = time.monotonic()
            launch_exit = bootstrap.launch(run_id=run_id)
            report["launch_exit_code"] = launch_exit
            report["launch_elapsed_seconds"] = round(time.monotonic() - start, 3)
            if launch_exit == 0:
                print(f"[3/4] Observing normal startup for {OBSERVE_SECONDS}s; no commands will be injected...", flush=True)
                time.sleep(OBSERVE_SECONDS)
                ready = _read_json(source_run / "ready.json")
                same_primary = coordinator.primary_pid() == ready.get("pid")
                report["ui_ping"] = bool(same_primary and coordinator.request("ping", wait_ack=True, timeout=3))
            else:
                print("[3/4] Startup not confirmed. Collecting the failure; the UI process is left untouched.", flush=True)
                report["ui_ping"] = None
            print("[4/4] Public connectivity probes; no bot tokens, no VPN/firewall changes...", flush=True)
            for target in ("telegram", "discord", "local_vision"):
                result = run_owned_helper([sys.executable, str(Path(__file__).resolve()), "--network-probe", target], timeout=12)
                try:
                    value = json.loads(result["output"])
                except (ValueError, TypeError):
                    value = {"target": target, "status": "PROBE_FAILED", "helper_exit_code": result["exit_code"],
                             "detail": result["output"][:1600]}
                if not isinstance(value, dict):
                    value = {"target": target, "status": "PROBE_FAILED", "detail": "Probe returned non-object JSON"}
                report["network_summary"][target] = str(value.get("status", "PROBE_FAILED"))
                write_report(report_dir / f"network_{target}.json", value)
            report["status"] = "UI_READY" if launch_exit == 0 and report.get("ui_ping") else "NEEDS_ANALYSIS"
            exit_code = 0 if report["status"] == "UI_READY" and test_result["exit_code"] == 0 else 1
    except KeyboardInterrupt:
        report["status"] = "INTERRUPTED"
        exit_code = 130
    except Exception as exc:
        report.update(status="DIAGNOSTIC_ERROR", error_type=type(exc).__name__, detail=redact(str(exc))[:1600])
    finally:
        for name in ("launch_status.json", "startup_progress.json", "ready.json"):
            payload = _read_json(source_run / name)
            if payload:
                payload.pop("proof", None)
                write_report(report_dir / ("ready_receipt.json" if name == "ready.json" else name), payload)
        if (source_run / "startup.log").is_file():
            write_log_report(report_dir / "startup.log", read_new_log(source_run / "startup.log", 0))
        if report["status"] != "BLOCKED_EXISTING_INSTANCE":
            write_log_report(report_dir / "application_interval.log", read_new_log(app_log, offset, identity))
        report.update(finished_at=time.time(), exit_code=exit_code)
        report["summary"] = diagnostic_summary(report)
        write_report(report_dir / "report.json", report)
        (report_dir / "README.txt").write_text(
            "NEO 149 startup diagnosis. UI readiness, public API reachability and bot authentication are distinct.\n"
            "This test does not execute atlas actions, test wake recognition, or verify music playback.\n"
            "The ordinary assistant remains running with its existing settings and normal integrations.\n"
            "Review logs before sharing: patterns of credentials are redacted, but command text can remain.\n", encoding="utf-8")
        bundle_reports(report_dir, destination)
        print_diagnostic_summary(report)
        print("\nSEND_THIS_ZIP=" + str(destination), flush=True)
        print("NEO was not killed. Do not include settings.json or databases.", flush=True)
    return exit_code


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--network-probe":
        print(json.dumps(network_probe(sys.argv[2]), ensure_ascii=True))
    else:
        raise SystemExit(main())
