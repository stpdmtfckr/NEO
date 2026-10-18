# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Central low-overhead resource policy for NEO JARVIS.

The policy is intentionally advisory: correctness-critical work may still run,
but maintenance/indexing/freshness work can defer while an AAA game is active.
No polling thread lives here; game presence publishes state into the singleton.
"""

from dataclasses import dataclass, asdict
from contextlib import contextmanager
import threading
import time
from typing import Any


@dataclass(frozen=True)
class ResourcePolicy:
    game_active: bool
    game_id: str
    cpu_mode: str
    gpu_mode: str
    background_io: str
    qwen_policy: str
    heavy_maintenance_allowed: bool
    updated_at: float


class ResourceBudget:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._config: Any | None = None
        self._game_active = False
        self._game_id = ""
        self._updated_at = time.time()
        self._foreground_work = 0

    def configure(self, config: Any | None) -> None:
        with self._lock:
            self._config = config

    def set_game_active(self, active: bool, game_id: str = "") -> None:
        with self._lock:
            self._game_active = bool(active)
            self._game_id = str(game_id or "").casefold() if active else ""
            self._updated_at = time.time()

    def _get_bool(self, key: str, default: bool) -> bool:
        cfg = self._config
        if cfg is None:
            return default
        try:
            return bool(cfg.get(key, default))
        except Exception:
            return default

    def policy(self) -> ResourcePolicy:
        with self._lock:
            active = self._game_active
            game_id = self._game_id
            updated_at = self._updated_at
        # Heavy background work is throttled while a verified game is active.
        # User-triggered SearchAPI Google AI lookups remain lightweight/on-demand.
        allow_heavy = not active or self._get_bool("runtime.resource_budget.allow_heavy_maintenance_in_game", False)
        return ResourcePolicy(
            game_active=active,
            game_id=game_id,
            cpu_mode="low" if active else "normal",
            gpu_mode="minimal" if active else "normal",
            background_io="throttled" if active else "normal",
            qwen_policy="on_demand" if active else "normal",
            heavy_maintenance_allowed=allow_heavy,
            updated_at=updated_at,
        )

    def should_defer_heavy_background(self, *, correctness_required: bool = False) -> bool:
        if correctness_required:
            return False
        with self._lock:
            foreground_busy = self._foreground_work > 0
        return foreground_busy or not self.policy().heavy_maintenance_allowed

    @contextmanager
    def foreground_work(self):
        """Publish a running user/audio job; no sampler thread or CPU polling."""
        with self._lock:
            self._foreground_work += 1
        try:
            yield
        finally:
            with self._lock:
                self._foreground_work = max(0, self._foreground_work - 1)

    def snapshot(self) -> dict[str, Any]:
        result = asdict(self.policy())
        with self._lock:
            result["foreground_work"] = self._foreground_work
        return result


_SINGLETON = ResourceBudget()


def resource_budget() -> ResourceBudget:
    return _SINGLETON


__all__ = ["ResourcePolicy", "ResourceBudget", "resource_budget"]
