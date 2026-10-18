# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


class GameWebContextStore:
    """Tiny JSON journal of the detected game. No gameplay knowledge is stored."""

    def __init__(self, config: Any) -> None:
        self.path = Path(config.paths.cache) / "game_web" / "active_game.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def record(self, data: dict[str, Any], *, status: str = "running") -> None:
        row = {k: v for k, v in dict(data or {}).items() if v not in (None, "")}
        row["status"] = str(status or "running")
        row["observed_at"] = time.time()
        with self._lock:
            fd, temp_name = tempfile.mkstemp(prefix="active_game.", suffix=".tmp", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(row, fh, ensure_ascii=False, indent=2)
                    fh.flush(); os.fsync(fh.fileno())
                os.replace(temp_name, self.path)
            finally:
                try:
                    if os.path.exists(temp_name): os.remove(temp_name)
                except OSError:
                    pass

    def read(self) -> dict[str, Any]:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                return raw if isinstance(raw, dict) else {}
            except Exception:
                return {}
