"""Per-launch readiness, including the Windows venv redirector's real UI child.

The launcher never treats a running process or an old flag as readiness. A v2
proof binds run, source, PID, parent PID and timestamp to an ephemeral launch
secret. The secret is removed before importing UI code or spawning its workers.
This is stale/cross-run protection, not an OS boundary against the same user.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import time
from typing import Any

logger = logging.getLogger(__name__)
_SECRET = ""
_STATES = {"ui_event_loop_ready", "existing_instance_acknowledged"}


def capture_launch_secret() -> None:
    global _SECRET
    _SECRET = os.environ.pop("NEO_LAUNCH_READY_SECRET", "")


def _signature(payload: dict[str, Any], secret: str) -> str:
    content = {key: value for key, value in payload.items() if key != "proof"}
    message = json.dumps(content, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _target() -> tuple[str, Path] | None:
    run_id = os.environ.get("NEO_LAUNCH_RUN_ID", "")
    raw = os.environ.get("NEO_LAUNCH_READY_PATH", "")
    if not run_id or not raw:
        return None
    path = Path(raw)
    if path.name != "ready.json" or path.parent.name != run_id or not path.parent.is_dir():
        return None
    return run_id, path


def publish_progress(stage: str) -> bool:
    target = _target()
    if target is None:
        return False
    run_id, ready = target
    payload = {"run_id": run_id, "pid": os.getpid(), "parent_pid": os.getppid(),
               "stage": stage, "updated_at": time.time()}
    try:
        path = ready.with_name("startup_progress.json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
        return True
    except OSError as exc:
        logger.warning("Startup progress write failed: stage=%s type=%s errno=%s",
                       stage, type(exc).__name__, getattr(exc, "errno", None))
        return False


def publish_ready(*, existing_instance: bool = False) -> bool:
    target = _target()
    if target is None:
        logger.warning("Startup readiness target is missing or invalid")
        return False
    run_id, path = target
    payload = {"version": 2, "run_id": run_id, "pid": os.getpid(),
               "parent_pid": os.getppid(), "ready_at": time.time(),
               "source_app": str(Path(__file__).resolve().parents[2] / "app.py"),
               "state": "existing_instance_acknowledged" if existing_instance else "ui_event_loop_ready"}
    secret = _SECRET or os.environ.get("NEO_LAUNCH_READY_SECRET", "")
    if secret:
        payload["proof"] = _signature(payload, secret)
    try:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        logger.warning("Startup readiness write failed: run=%s pid=%s type=%s errno=%s",
                       run_id, os.getpid(), type(exc).__name__, getattr(exc, "errno", None))
        publish_progress("ready_write_failed")
        return False
    publish_progress(payload["state"])
    logger.info("Startup readiness published: run=%s pid=%s parent_pid=%s state=%s",
                run_id, payload["pid"], payload["parent_pid"], payload["state"])
    return True


def _same_source(left: str, right: Path) -> bool:
    try:
        return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(right.resolve()))
    except (OSError, ValueError, TypeError):
        return False


def validate_ready(payload: Any, *, spawned_pid: int, run_id: str, secret: str = "",
                   source_app: Path | None = None, launched_at: float | None = None) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "invalid_payload_type"
    if payload.get("run_id") != run_id:
        return False, "run_id_mismatch"
    if not isinstance(payload.get("state"), str) or payload["state"] not in _STATES:
        return False, "invalid_ready_state"
    pid = payload.get("pid")
    if type(pid) is not int or pid <= 0:
        return False, "invalid_pid"
    if secret:
        if payload.get("version") != 2:
            return False, "unsupported_receipt_version"
        try:
            proof = payload.get("proof")
            if not isinstance(proof, str) or not hmac.compare_digest(proof, _signature(payload, secret)):
                return False, "proof_mismatch"
        except (TypeError, ValueError, OverflowError):
            return False, "invalid_proof_payload"
        if source_app is None or not _same_source(payload.get("source_app", ""), source_app):
            return False, "source_mismatch"
        at = payload.get("ready_at")
        if type(at) not in (int, float):
            return False, "invalid_ready_time"
        try:
            finite = math.isfinite(at)
        except OverflowError:
            finite = False
        if not finite:
            return False, "invalid_ready_time"
        if launched_at is None or at < launched_at - 1 or at > time.time() + 5:
            return False, "ready_time_mismatch"
    if pid == spawned_pid:
        return True, "direct_process"
    # Only the direct OS child of the launched venv redirector is accepted. No
    # permissive scan for some other NEO/Python process on the machine.
    if not secret or type(payload.get("parent_pid")) is not int or payload["parent_pid"] != spawned_pid:
        return False, "pid_mismatch"
    try:
        import psutil
        child = psutil.Process(pid)
        if child.ppid() != spawned_pid:
            return False, "parent_pid_mismatch"
        if launched_at is not None and child.create_time() < launched_at - 2:
            return False, "process_age_mismatch"
        return True, "venv_redirector_child"
    except ImportError:
        return False, "process_verifier_unavailable"
    except psutil.NoSuchProcess:
        # A successful secondary launch ACKs the existing instance and exits
        # before the 100-ms observer poll. Its signed, current-run receipt is
        # sufficient; this exception never applies to ordinary UI readiness.
        if payload["state"] == "existing_instance_acknowledged":
            return True, "venv_redirector_completed_ack"
        return False, "ui_process_exited"
    except (psutil.Error, OSError, ValueError):
        return False, "process_verification_failed"


def acknowledge_existing_instance() -> bool:
    return publish_ready(existing_instance=True)
