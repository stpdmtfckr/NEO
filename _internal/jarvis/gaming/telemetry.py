# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Live game -> overlay synchronization.

The service is intentionally Qt-free.  It polls verified processes/adapters in a
daemon thread and writes only meaningful state changes to OverlayStore.  The GUI
thread renders the one-second clock locally from ``binding.session_started_at`` so
we do not rewrite JSON once per second merely to advance a timer.
"""

import json
import logging
import threading
import time
from copy import deepcopy
from typing import Any

import psutil

from jarvis.gaming.history import GameSessionHistoryStore
from jarvis.gaming.manager import GameManager, GameSnapshot
from jarvis.overlay_state import OverlayStore, publish_overlay_refresh

logger = logging.getLogger(__name__)


def snapshot_signature(snapshot: GameSnapshot) -> str:
    data = snapshot.as_dict()
    # Elapsed seconds is rendered dynamically by Qt and must not turn every poll
    # into a disk write/refresh event.
    data.pop("session_seconds", None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


class LiveGamePanelService:
    def __init__(
        self,
        manager: GameManager,
        overlays: OverlayStore,
        history: GameSessionHistoryStore,
        *,
        refresh_seconds: float = 2.0,
        history_touch_seconds: float = 30.0,
    ) -> None:
        self.manager = manager
        self.overlays = overlays
        self.history = history
        self.refresh_seconds = max(0.5, min(30.0, float(refresh_seconds or 2.0)))
        self.history_touch_seconds = max(5.0, min(300.0, float(history_touch_seconds or 30.0)))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._signatures: dict[str, str] = {}
        self._last_history_touch: dict[str, float] = {}
        # Verified process lifetimes are tracked even when no session overlay is
        # visible, so the local session history remains accurate.
        self._auto_sessions: dict[int, dict[str, Any]] = {}

    def ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                self._wake.set()
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="jarvis-game-telemetry", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _bound_panels(self) -> list[dict[str, Any]]:
        data = self.overlays.load()
        panels = data.get("panels", {}) if isinstance(data, dict) else {}
        return [
            deepcopy(panel) for panel in panels.values()
            if isinstance(panel, dict)
            and isinstance(panel.get("binding"), dict)
            and str(panel["binding"].get("type", "")).casefold() == "game_session"
            and str(panel["binding"].get("status", "running")).casefold() in {"running", "starting", "waiting", "ended"}
        ]

    @staticmethod
    def _pid(panel: dict[str, Any]) -> int:
        try:
            return int((panel.get("binding") or {}).get("pid") or 0)
        except Exception:
            return 0

    def _snapshot_for_panel(self, panel: dict[str, Any], pid: int) -> GameSnapshot | None:
        snap = self.manager.snapshot(preferred_pid=pid, require_preferred=True)
        if snap is not None:
            return snap
        binding = panel.get("binding", {}) if isinstance(panel.get("binding"), dict) else {}
        bound_game_id = str(binding.get("game_id") or "").casefold()

        # Store games often transition from a launcher/bootstrap EXE to the real
        # client.  Do not declare the session ended merely because the originally
        # bound PID disappeared: if the process-only detector still sees the same
        # verified game identity, rebind the panel and preserve its original session
        # start so the visible timer does not jump backwards to zero.
        if bound_game_id and bound_game_id != "generic":
            replacement = self.manager.snapshot()
            if replacement is not None and str(replacement.game_id or "").casefold() == bound_game_id:
                try:
                    original_start = float(binding.get("session_started_at") or 0.0)
                except (TypeError, ValueError):
                    original_start = 0.0
                if original_start > 0.0:
                    replacement.session_started_at = min(original_start, float(replacement.session_started_at or original_start))
                    replacement.session_seconds = max(0, int(time.time() - replacement.session_started_at))
                return replacement
            return None

        if bound_game_id != "generic":
            return None
        # Generic games are accepted only when the panel already contains a PID
        # verified by JARVIS' launcher.  We never discover arbitrary processes here.
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                return None
            started = float(proc.create_time())
            try:
                executable = str(proc.exe() or "")
            except (psutil.Error, OSError):
                executable = ""
            return GameSnapshot(
                game_id="generic",
                game_name=str(binding.get("game_name") or panel.get("game_name") or proc.name() or "Игра"),
                pid=pid,
                process_name=str(proc.name() or ""),
                executable=executable,
                session_started_at=started,
                session_seconds=max(0, int(time.time() - started)),
                capabilities=["session"],
                notes=["Доступна только проверенная длительность сессии."],
            )
        except (psutil.Error, OSError, ValueError):
            return None

    def _refresh_panel(self, panel: dict[str, Any]) -> None:
        binding = panel.get("binding", {}) if isinstance(panel.get("binding"), dict) else {}
        name = str(panel.get("name") or "Сессия")
        status = str(binding.get("status", "running") or "running").casefold()
        pid = self._pid(panel)

        # A session panel is a persistent *slot*, not a one-game tombstone.  Once
        # a game closes we clear it and keep it waiting; when another verified game
        # appears the same panel immediately starts tracking the new process.
        if status in {"waiting", "ended"}:
            try:
                snap = self.manager.snapshot()
            except Exception:
                snap = None
            if snap is None:
                if status != "waiting" or str(panel.get("text") or "").strip():
                    waiting_binding = dict(binding)
                    waiting_binding.update({
                        "type": "game_session", "status": "waiting", "pid": 0,
                        "game_id": "", "game_name": "", "session_started_at": 0.0,
                        "last_telemetry_at": time.time(),
                    })
                    self.overlays.apply({
                        "action": "set_text", "name": name, "title": "", "text": "",
                        "binding": waiting_binding, "live_state": None,
                        "purpose": "game_session", "game_id": "", "game_name": "",
                        "aliases": panel.get("aliases") or ["сессия", "игровая сессия", "сессия по игре", "информация по игре"],
                    })
                self._signatures.pop(name, None)
                return
            # Fresh game found: fall through into the normal update path with a
            # fresh PID/start time and no stale game identity from the previous run.
            binding = {
                "type": "game_session", "status": "running", "pid": snap.pid,
                "game_id": snap.game_id, "game_name": snap.game_name,
                "session_started_at": snap.session_started_at,
                "simple_mode": bool(binding.get("simple_mode", True)),
                "last_telemetry_at": time.time(),
            }
            pid = snap.pid
        elif pid <= 0:
            # A "starting" placeholder is owned by the dedicated launch watcher;
            # do not accidentally bind it to some other game already running.
            return
        else:
            snap = self._snapshot_for_panel(panel, pid)

        if snap is None:
            started_at = float(binding.get("session_started_at") or time.time())
            if pid > 0:
                try:
                    self.history.finish(
                        game_id=str(binding.get("game_id") or "generic"),
                        pid=pid, started_at=started_at,
                    )
                except Exception:
                    logger.debug("Could not finish visible game session", exc_info=True)
            waiting_binding = dict(binding)
            waiting_binding.update({
                "type": "game_session", "status": "waiting", "pid": 0,
                "game_id": "", "game_name": "", "session_seconds": 0,
                "session_started_at": 0.0, "ended_at": time.time(),
            })
            # User-requested lifecycle: when the game closes, clear the panel
            # instead of leaving stale name/timer/details from the old title.
            self.overlays.apply({
                "action": "set_text", "name": name, "title": "", "text": "",
                "binding": waiting_binding, "live_state": None,
                "purpose": "game_session", "game_id": "", "game_name": "",
                "aliases": panel.get("aliases") or ["сессия", "игровая сессия", "сессия по игре", "информация по игре"],
            })
            self._signatures.pop(name, None)
            return

        state = snap.as_dict()
        now = time.time()
        key = f"{snap.game_id}:{snap.pid}:{int(snap.session_started_at)}"
        if now - float(self._last_history_touch.get(key, 0.0)) >= self.history_touch_seconds:
            self.history.touch(state)
            self._last_history_touch[key] = now
        signature = snapshot_signature(snap)
        if self._signatures.get(name) == signature:
            # No persistent game-state field changed, but the elapsed session
            # clock is presentation state. Publish a cheap heartbeat so even a
            # recreated/temporarily delayed GUI timer gets a fresh render without
            # rewriting overlays.json every couple of seconds. Roughly once per
            # history-touch interval persist a telemetry heartbeat as well so
            # built-in diagnostics can distinguish a healthy quiet adapter from a dead
            # telemetry worker.
            last_telemetry = float(binding.get("last_telemetry_at") or 0.0)
            if now - last_telemetry >= self.history_touch_seconds:
                heartbeat_binding = dict(binding)
                heartbeat_binding.update({
                    "type": "game_session", "status": "running", "pid": snap.pid,
                    "game_id": snap.game_id, "game_name": snap.game_name,
                    "session_started_at": snap.session_started_at,
                    "last_telemetry_at": now,
                })
                render_text = GameManager.simple_session_text(snap)
                self.overlays.apply({
                    "action": "set_text", "name": name, "text": str(panel.get("text") or render_text),
                    "binding": heartbeat_binding,
                    "purpose": panel.get("purpose") or "game_session",
                    "game_id": panel.get("game_id") or snap.game_id,
                    "game_name": panel.get("game_name") or snap.game_name,
                    "aliases": panel.get("aliases") or [],
                })
            else:
                publish_overlay_refresh(name)
            return
        self._signatures[name] = signature
        new_binding = dict(binding)
        new_binding.update({
            "type": "game_session", "status": "running", "pid": snap.pid,
            "game_id": snap.game_id, "game_name": snap.game_name,
            "session_started_at": snap.session_started_at,
            "last_telemetry_at": now,
        })
        self.overlays.apply({
            "action": "set_text", "name": name,
            "title": "",
            "text": GameManager.simple_session_text(snap),
            "binding": new_binding,
            "live_state": snap.as_dict(),
            "purpose": "game_session", "game_id": snap.game_id, "game_name": snap.game_name,
            "aliases": ["сессия", "игровая сессия", "сессия по игре", "информация по игре"],
        })

    def _track_supported_game_lifecycle(self) -> None:
        now = time.time()
        try:
            snap = self.manager.snapshot()
        except Exception:
            snap = None
        if snap is not None:
            state = snap.as_dict()
            key = f"auto:{snap.game_id}:{snap.pid}:{int(snap.session_started_at)}"
            if now - float(self._last_history_touch.get(key, 0.0)) >= self.history_touch_seconds:
                self.history.touch(state)
                self._last_history_touch[key] = now
            self._auto_sessions[snap.pid] = {
                "game_id": snap.game_id, "pid": snap.pid,
                "started_at": snap.session_started_at, "last_seen": now,
            }
        for pid, item in list(self._auto_sessions.items()):
            try:
                alive = psutil.pid_exists(int(pid)) and psutil.Process(int(pid)).is_running()
            except (psutil.Error, OSError, ValueError):
                alive = False
            if alive:
                continue
            try:
                self.history.finish(
                    game_id=str(item.get("game_id") or "generic"),
                    pid=int(pid), started_at=float(item.get("started_at") or now), ended_at=now,
                )
            except Exception:
                logger.debug("Could not finish automatic game session", exc_info=True)
            self._auto_sessions.pop(pid, None)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._track_supported_game_lifecycle()
            except Exception:
                logger.debug("Automatic game history tracking failed", exc_info=True)
            panels = self._bound_panels()
            for panel in panels:
                if self._stop.is_set():
                    break
                try:
                    self._refresh_panel(panel)
                except Exception:
                    logger.debug("Live game panel refresh failed", exc_info=True)
            self._wake.wait(self.refresh_seconds)
            self._wake.clear()
