# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Persistent session accounting for JARVIS gaming mode.

This store deliberately tracks only facts JARVIS can verify itself: process start,
process end and elapsed time.
"""

import json
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()


class GameSessionHistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "sessions": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            if not isinstance(data.get("sessions"), dict):
                data["sessions"] = {}
            data.setdefault("version", 1)
            return data
        except Exception:
            return {"version": 1, "sessions": {}}

    def _save_unlocked(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _key(game_id: str, pid: int, started_at: float) -> str:
        return f"{str(game_id or 'generic')}:{int(pid or 0)}:{int(float(started_at or 0))}"

    def touch(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        game_id = str(snapshot.get("game_id") or "generic")
        pid = int(snapshot.get("pid") or 0)
        started_at = float(snapshot.get("session_started_at") or time.time())
        key = self._key(game_id, pid, started_at)
        now = time.time()
        with _LOCK:
            data = self._load_unlocked()
            sessions = data.setdefault("sessions", {})
            item = sessions.get(key)
            if not isinstance(item, dict):
                item = {
                    "key": key,
                    "game_id": game_id,
                    "game_name": str(snapshot.get("game_name") or game_id),
                    "pid": pid,
                    "process_name": str(snapshot.get("process_name") or ""),
                    "executable": str(snapshot.get("executable") or ""),
                    "started_at": started_at,
                    "first_seen_at": now,
                    "ended_at": None,
                    "duration_seconds": 0,
                    "status": "running",
                }
                sessions[key] = item
            item["last_seen_at"] = now
            item["status"] = "running"
            item["ended_at"] = None
            item["duration_seconds"] = max(0, int(now - started_at))
            self._save_unlocked(data)
            return deepcopy(item)

    def finish(self, *, game_id: str, pid: int, started_at: float, ended_at: float | None = None) -> dict[str, Any] | None:
        key = self._key(game_id, pid, started_at)
        now = float(ended_at or time.time())
        with _LOCK:
            data = self._load_unlocked()
            item = data.setdefault("sessions", {}).get(key)
            if not isinstance(item, dict):
                return None
            item["status"] = "ended"
            item["ended_at"] = now
            item["last_seen_at"] = now
            item["duration_seconds"] = max(0, int(now - float(item.get("started_at") or started_at)))
            self._save_unlocked(data)
            return deepcopy(item)

    def all_sessions(self) -> list[dict[str, Any]]:
        """Return a newest-first read-only snapshot for diagnostics/UI."""
        with _LOCK:
            data = self._load_unlocked()
            items = [deepcopy(v) for v in data.get("sessions", {}).values() if isinstance(v, dict)]
        items.sort(key=lambda item: float(item.get("started_at") or 0.0), reverse=True)
        return items

    def summary_all(self) -> dict[str, dict[str, Any]]:
        """Aggregate process-verified session time per game without inventing outcomes."""
        game_ids = sorted({str(x.get("game_id") or "generic") for x in self.all_sessions()})
        return {game_id: self.summary(game_id) for game_id in game_ids}

    def summary(self, game_id: str, *, include_running_started_at: float | None = None) -> dict[str, Any]:
        gid = str(game_id or "generic")
        with _LOCK:
            data = self._load_unlocked()
            items = [
                deepcopy(v) for v in data.get("sessions", {}).values()
                if isinstance(v, dict) and str(v.get("game_id") or "") == gid
            ]
        completed = [x for x in items if str(x.get("status")) == "ended"]
        running = [x for x in items if str(x.get("status")) == "running"]
        total = sum(max(0, int(x.get("duration_seconds") or 0)) for x in completed)
        now = time.time()
        for item in running:
            total += max(0, int(now - float(item.get("started_at") or now)))
        # Avoid double-counting a newly created snapshot which has not yet been
        # persisted by the background service.
        if include_running_started_at is not None and not running:
            total += max(0, int(now - float(include_running_started_at)))
        return {
            "sessions": len(items),
            "completed_sessions": len(completed),
            "running_sessions": len(running),
            "total_seconds": total,
        }
