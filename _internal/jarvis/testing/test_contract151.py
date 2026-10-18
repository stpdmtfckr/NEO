"""Completion evidence for the unified test; a launcher exit code is not proof.

Receipts are local metadata with relative filenames only, never settings or keys.
They protect against accidental dispatch/stale results, not a hostile local user.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import zipfile

READY_UI = {"READY", "REUSED_READY"}
OK_API = {"AUTHENTICATED", "API_REQUEST_OK", "NOT_CONFIGURED", "DISABLED"}


def read_object(path: Path, *, limit: int = 8 * 1024 * 1024) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Missing, oversized or linked JSON report")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def atlas_complete(summary: dict, child_exit: int, archive_path: Path | None) -> bool:
    """Require actual counts, replies and a finished archive, even for exit 0."""
    planned, done = summary.get("planned"), summary.get("completed")
    expected, replies = summary.get("agent_replies_expected"), summary.get("agent_replies_received")
    if not (child_exit in (0, 1) and summary.get("exit_code") == child_exit
            and summary.get("status") == ("FINISHED" if child_exit == 0 else "FINISHED_WITH_FINDINGS")
            and type(planned) is int and planned > 0 and done == planned
            and type(expected) is int and expected > 0 and replies == expected
            and summary.get("all_cases_recorded") is True
            and summary.get("all_expected_agent_replies_received") is True
            and not summary.get("stopped") and not summary.get("fatal_error")):
        return False
    try:
        if not archive_path or archive_path.is_symlink() or not archive_path.is_file():
            return False
        with zipfile.ZipFile(archive_path) as archive:
            entry = archive.getinfo("summary.json")
            if entry.file_size > 8 * 1024 * 1024:
                return False
            stored = json.loads(archive.read("summary.json"))
            for key in ("started", "planned", "completed", "counts", "exit_code", "status"):
                if key not in summary or stored.get(key) != summary[key]:
                    return False
            return archive.testzip() is None
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return False


def outcome(report: dict, atlas_ok: bool) -> tuple[int, list[str]]:
    """Missing phases/receipts cannot be converted to success by subprocess code 0."""
    steps = report.get("steps", {})
    # NEO 179: no spoken answers, so the field test has no voice phase any more.
    missing = [name for name in ("ui", "safe_tests", "settings", "integrations", "atlas")
               if name not in steps]
    incomplete = ["missing_step:" + name for name in missing]
    if not atlas_ok:
        incomplete.append("atlas_completion_unconfirmed")
    safe = steps.get("safe_tests", {})
    if safe.get("exit_code") not in (0, 1) or not safe.get("tests_recorded"):
        incomplete.append("safe_tests_incomplete")
    if incomplete:
        return 2, incomplete
    findings = []
    integrity_status = steps.get("build_integrity", {}).get("status")
    if integrity_status not in (None, "PASS", "BASELINE_RECORDED"):
        findings.append("build_integrity:" + str(integrity_status))
    speech_status = steps.get("speech_quality", {}).get("status")
    if speech_status not in (None, "PASS", "NOT_RUN"):
        findings.append("speech_quality:" + str(speech_status))
    if safe.get("exit_code"):
        findings.append("safe_tests_failed")
    if steps["settings"].get("type_review_count", 0):
        findings.append("settings_type_review")
    if steps["ui"].get("status") not in READY_UI:
        findings.append("ui_not_verified")
    for provider, status in steps["integrations"].items():
        if status not in OK_API:
            findings.append("integration:" + str(provider) + ":" + str(status))
    if steps["atlas"].get("exit_code"):
        findings.append("atlas_findings")
    if report.get("offline_test") or report.get("limited_atlas"):
        findings.append("limited_developer_run")
    return (1 if findings else 0), findings


def _safe_id(invocation_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", invocation_id):
        raise ValueError("Invalid invocation id")
    return invocation_id


def receipt_path(diagnostics: Path, invocation_id: str) -> Path:
    return diagnostics / "test_receipts" / (_safe_id(invocation_id) + ".json")


def write_receipt(diagnostics: Path, invocation_id: str, archive: Path, code: int, source: Path) -> Path:
    from jarvis.runtime.diagnostics import write_report
    if archive.resolve().parent != diagnostics.resolve() or archive.is_symlink():
        raise ValueError("Unexpected archive location")
    path = receipt_path(diagnostics, invocation_id)
    if path.exists():
        raise FileExistsError("Receipt already exists for this invocation")
    return write_report(path, {
        "schema": 1, "invocation_id": invocation_id, "archive_name": archive.name,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "exit_code": code,
    })


def verify_receipt(diagnostics: Path, invocation_id: str, provided: Path, child_exit: int, source: Path) -> int:
    """Validate current invocation, source, completed ZIP and code without profile I/O."""
    try:
        expected = receipt_path(diagnostics, invocation_id)
        if provided.resolve() != expected.resolve() or provided.is_symlink():
            raise ValueError("Unexpected receipt path")
        data = read_object(provided, limit=16384)
        if data.get("schema") != 1 or data.get("invocation_id") != invocation_id:
            raise ValueError("Receipt belongs to another invocation")
        if data.get("source_sha256") != hashlib.sha256(source.read_bytes()).hexdigest():
            raise ValueError("Receipt belongs to another runner revision")
        name = data.get("archive_name", "")
        if (not isinstance(name, str) or '/' in name or '\\' in name or ':' in name
                or not re.fullmatch(r"NEO_test_[0-9]{3}_[A-Za-z0-9_-]+_RESULTS\.zip", name)):
            raise ValueError("Invalid result filename")
        archive_path = diagnostics / name
        if archive_path.is_symlink() or not archive_path.is_file():
            raise ValueError("Result ZIP missing")
        if hashlib.sha256(archive_path.read_bytes()).hexdigest() != data.get("archive_sha256"):
            raise ValueError("Result ZIP digest mismatch")
        with zipfile.ZipFile(archive_path) as archive:
            if archive.getinfo("reports/report.json").file_size > 8 * 1024 * 1024 or archive.testzip():
                raise ValueError("Result ZIP is invalid")
            report = json.loads(archive.read("reports/report.json"))
        if (type(child_exit) is not int or child_exit not in (0, 1, 2)
                or data.get("exit_code") != child_exit or report.get("exit_code") != child_exit
                or report.get("invocation_id") != invocation_id):
            raise ValueError("Exit code or invocation differs from packed report")
        if child_exit in (0, 1):
            computed, _ = outcome(report, report.get("atlas_completion_verified") is True)
            expected_status = "FINISHED" if child_exit == 0 else "FINISHED_WITH_FINDINGS"
            if computed != child_exit or report.get("status") != expected_status:
                raise ValueError("Completion claim not supported by phase evidence")
        print("TEST_REPORT_VERIFIED=" + archive_path.name, flush=True)
        return child_exit
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        print("TEST_RESULT_UNCONFIRMED: the current test did not produce a matching verified report."
              " A successful assistant startup is not a successful test.", flush=True)
        print("REPORT_DIRECTORY=" + str(diagnostics), flush=True)
        return 2
