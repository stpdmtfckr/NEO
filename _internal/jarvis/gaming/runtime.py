# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Small in-process runtime context shared by the launcher and game adapters.

A storefront/launcher can accept a game launch long before the real game EXE
exists.  The assistant keeps that intent so a follow-up such as "show my game
session" can continue waiting for the game instead of forgetting what was just
started or trying to resolve the whole sentence as an application name.
"""

import threading
import time
import os
from copy import deepcopy
from typing import Any

from jarvis.runtime.resource_budget import resource_budget

_LOCK = threading.RLock()
_LAST_LAUNCH: dict[str, Any] = {}


def record_game_launch(**values: Any) -> dict[str, Any]:
    import uuid
    with _LOCK:
        _LAST_LAUNCH.clear()
        _LAST_LAUNCH.update({k: v for k, v in values.items() if v not in (None, "")})
        _LAST_LAUNCH.setdefault("requested_at", time.time())
        # REC-APPLICATION-FREEZE-SOURCE-01: every launch owns a token; a late
        # watcher result of an older launch can no longer mutate this record.
        _LAST_LAUNCH["launch_token"] = uuid.uuid4().hex
        return deepcopy(_LAST_LAUNCH)


def current_launch_token() -> str:
    with _LOCK:
        return str(_LAST_LAUNCH.get("launch_token", "") or "")


def update_game_launch(*, token: str | None = None, **values: Any) -> dict[str, Any]:
    with _LOCK:
        if token is not None and str(_LAST_LAUNCH.get("launch_token", "") or "") != str(token):
            return deepcopy(_LAST_LAUNCH)        # stale owner: no write
        if not _LAST_LAUNCH:
            _LAST_LAUNCH["requested_at"] = time.time()
        _LAST_LAUNCH.update({k: v for k, v in values.items() if v not in (None, "") and k != "launch_token"})
        return deepcopy(_LAST_LAUNCH)


def recent_game_launch(max_age_seconds: float = 300.0) -> dict[str, Any] | None:
    with _LOCK:
        if not _LAST_LAUNCH:
            return None
        requested = float(_LAST_LAUNCH.get("requested_at", 0.0) or 0.0)
        if requested <= 0 or time.time() - requested > max(5.0, float(max_age_seconds)):
            return None
        return deepcopy(_LAST_LAUNCH)


def clear_game_launch() -> None:
    with _LOCK:
        _LAST_LAUNCH.clear()

_ACTIVE_GAME: dict[str, Any] = {}


def record_active_game(**values: Any) -> dict[str, Any]:
    """Publish the process-verified active game without another psutil scan.

    The low-frequency gaming tracker owns process detection.  Agent routing reads
    this tiny in-process snapshot so ordinary voice turns do not enumerate every
    Windows process just to discover the current game.
    """
    with _LOCK:
        _ACTIVE_GAME.clear()
        _ACTIVE_GAME.update({k: v for k, v in values.items() if v not in (None, "")})
        _ACTIVE_GAME["observed_at"] = time.time()
        resource_budget().set_game_active(True, str(_ACTIVE_GAME.get("game_id") or ""))
        return deepcopy(_ACTIVE_GAME)


def process_identity_matches(snapshot: dict) -> bool:
    """A reused PID is not the observed game process."""
    try:
        import psutil
        pid=int(snapshot.get('pid',0) or 0)
        created=float(snapshot.get('session_started_at',snapshot.get('create_time',0)) or 0)
        if not pid or not created:
            return False
        process=psutil.Process(pid)
        return process.is_running() and abs(float(process.create_time())-created)<.01
    except (OSError, ValueError, TypeError):
        return False
    except Exception:
        return False


def active_game_context(max_age_seconds: float = 45.0) -> dict[str, Any] | None:
    with _LOCK:
        if not _ACTIVE_GAME:
            return None
        observed = float(_ACTIVE_GAME.get("observed_at", 0.0) or 0.0)
        if observed <= 0 or time.time() - observed > max(5.0, float(max_age_seconds)):
            return None
        snapshot=deepcopy(_ACTIVE_GAME)
    from jarvis.gaming.manager import _foreground_pid
    foreground=_foreground_pid()
    if foreground and foreground not in {int(snapshot.get('pid',0) or 0),os.getpid()}:
        return None
    return snapshot if process_identity_matches(snapshot) else None


def clear_active_game(pid: int | None = None) -> None:
    with _LOCK:
        if pid is not None and _ACTIVE_GAME and int(_ACTIVE_GAME.get("pid", 0) or 0) != int(pid):
            return
        _ACTIVE_GAME.clear()
        resource_budget().set_game_active(False)
