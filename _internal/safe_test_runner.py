"""Run the maintained safe regressions with isolated user state and no GUI actions."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent


def main() -> int:
    data_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local"))) / "NeoJarvis"
    output = data_root / "diagnostics"
    output.mkdir(parents=True, exist_ok=True)
    target = output / "test_results_latest.xml"
    target.unlink(missing_ok=True)
    # The complete suite also includes migrated historical source contracts.
    # This bounded local command focuses on current safe behavior regressions.
    tests = sorted((ROOT / "tests").glob("test_neo*.py"))
    tests.extend(sorted((ROOT / "tests").glob("test_145_*.py")))
    tests.extend(ROOT / "tests" / name for name in ("test_wake.py", "test_ambient_learning.py", "test_audio_provenance_boundary.py", "test_search_vision_translation_policy.py", "test_speech_understanding.py", "test_action_result_localization.py", "test_media_reliability.py", "test_media_agent_boundary.py", "test_desktop_ui_features.py", "test_game_locations.py", "test_telegram_remote_settings.py", "test_file_search_dates.py", "test_browser_downloads.py", "test_widgets145.py", "test_social_response_variation.py"))
    tests = [str(p) for p in tests if p.is_file()]
    # Keep the live service-behaviour regressions, including native stale-cache
    # notices, without pulling every historical literal source contract into
    # this user-facing command.
    tests.append(str(ROOT / "tests" / "test_game_web.py") + "::GameWebServiceTests")
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="neo-self-test-") as directory:
        env = dict(os.environ, LOCALAPPDATA=str(Path(directory) / "Local"), APPDATA=str(Path(directory) / "Roaming"), QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT), NEO_TEST_MODE="1")
        env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
        # Test cases use in-memory registries/mocks and temporary files. Never
        # dispatch the atlas into the user's live application.
        command = [sys.executable, "-m", "pytest", *tests, "-q", "--tb=short", "--continue-on-collection-errors", "--disable-warnings", f"--junitxml={target}"]
        try:
            result = subprocess.run(command, env=env, cwd=ROOT, timeout=1800, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            code, stdout = result.returncode, result.stdout
        except subprocess.TimeoutExpired as exc:
            code, stdout = 124, (exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")) + "\nSafe test suite exceeded 1800 seconds and was terminated."
    from jarvis.runtime.diagnostics import redact, write_report
    print(redact(stdout))
    rows = []
    if target.exists():
        try:
            for case in ET.parse(target).iter("testcase"):
                failure = case.find("failure")
                error = case.find("error")
                skipped = case.find("skipped")
                rows.append({"test": case.attrib.get("classname", "") + "." + case.attrib.get("name", ""), "status": "FAIL" if failure is not None else "ERROR" if error is not None else "SKIP" if skipped is not None else "PASS", "seconds": case.attrib.get("time", "0")})
        except ET.ParseError:
            pass
    report = write_report(output / "test_results_latest.json", {"exit_code": code, "elapsed_seconds": time.time() - started, "host": sys.platform, "scope": "maintained safe behavior/phrase regressions; isolated user state", "not_tested": ["live external APIs", "audio accuracy", "Windows installer", "game performance"], "tests": rows})
    # XML can contain source snippets/full assertions; the supported export uses
    # the compact sanitized JSON instead.
    print(f"NEO test report: {report}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
