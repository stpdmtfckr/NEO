# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import logging
import os
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
import psutil

logger = logging.getLogger(__name__)


class SessionGuard:
    """Detects an unclean previous shutdown and persists a compact recovery record."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.marker = self.root / "session_active.json"
        self.recovery = self.root / "last_crash_recovery.json"
        self.previous_crash: dict[str, Any] | None = None
        self.session_id = uuid.uuid4().hex
        self.started_at = time.time()

    @contextmanager
    def _locked(self):
        """Serialize replacement and ownership checks across overlapping processes."""
        from jarvis.runtime.instance_coordinator import InstanceCoordinator
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "session_marker.lock").open("a+b", buffering=0) as handle:
            if handle.seek(0, 2) == 0:
                handle.write(b"\0")
            deadline = time.monotonic() + 1.0
            while not InstanceCoordinator._lock(handle, blocking=False):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Session marker is busy")
                time.sleep(0.01)
            try:
                yield
            finally:
                InstanceCoordinator._unlock(handle)

    def _read_marker(self) -> dict[str, Any] | None:
        try:
            raw = self.marker.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            value = json.loads(raw)
            return value if isinstance(value, dict) else {"invalid_marker": True}
        except (ValueError, TypeError):
            return {"raw": raw[:4000]}

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(path.name + "." + self.session_id + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _owns(self, marker: dict[str, Any] | None) -> bool:
        return bool(marker and marker.get("session_id") == self.session_id and marker.get("pid") == os.getpid())

    @staticmethod
    def _is_live_shutdown(marker: dict[str, Any]) -> bool:
        if marker.get("state") != "shutting_down" or marker.get("exception"):
            return False
        try:
            process = psutil.Process(int(marker["pid"]))
            expected = float(marker["process_started_at"])
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE and abs(process.create_time() - expected) < 0.1
        except (KeyError, ValueError, TypeError, OSError, psutil.Error):
            return False

    def start(self) -> dict[str, Any] | None:
        self.previous_crash = None
        payload = {"pid": os.getpid(), "session_id": self.session_id,
                   "started_at": self.started_at, "process_started_at": psutil.Process().create_time(),
                   "state": "active", "clean": False}
        try:
            with self._locked():
                old = self._read_marker()
                if old and not self._owns(old) and not self._is_live_shutdown(old):
                    self.previous_crash = {
                        "detected_at": time.time(), "previous": old,
                        "message": "Предыдущая сессия завершилась аварийно или без корректного shutdown.",
                    }
                    self._write(self.recovery, self.previous_crash)
                self._write(self.marker, payload)
        except OSError:
            logger.warning("Session recovery marker unavailable", exc_info=True)
        return self.previous_crash

    def mark_shutting_down(self) -> None:
        """Record a handoff before releasing the singleton; this is not 'clean'."""
        try:
            with self._locked():
                payload = self._read_marker()
                if self._owns(payload):
                    payload.update(state="shutting_down", shutdown_started_at=time.time())
                    self._write(self.marker, payload)
        except OSError:
            logger.warning("Could not record session shutdown", exc_info=True)


    def enrich_previous(self, actions: list[dict[str, Any]]) -> None:
        if self.previous_crash is None:
            return
        self.previous_crash["last_actions"] = list(actions)[-100:]
        try:
            self._write(self.recovery, self.previous_crash)
        except OSError:
            pass

    def mark_exception(self, exc: BaseException) -> None:
        try:
            with self._locked():
                payload = self._read_marker()
                if self._owns(payload):
                    payload.update(time=time.time(), exception=repr(exc),
                        traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-30000:])
                    self._write(self.marker, payload)
        except OSError:
            pass

    def clean_shutdown(self) -> None:
        try:
            with self._locked():
                if self._owns(self._read_marker()):
                    self.marker.unlink(missing_ok=True)
        except OSError:
            pass
