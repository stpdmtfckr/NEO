# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Process-wide music ownership hints shared by all JARVIS Agents/transports.

Telegram intentionally owns a separate Agent instance.  Provider activity therefore
must not live only in Agent._last_music_target: a Telegram-started Yandex track must
still be controllable by the desktop quick command ``дальше``.  This module stores
only a tiny, non-secret provider/state hint in memory; authoritative tool status is
still used as fallback by the UI.
"""

import threading
import time
from typing import Any

_LOCK = threading.RLock()
_STATE: dict[str, Any] = {"owner": "", "state": "", "updated": 0.0}
_OWNERS: dict[str, dict[str, Any]] = {}


def _set(owner: str, state: str) -> None:
    with _LOCK:
        updated = time.monotonic()
        _STATE.update(owner=str(owner or ""), state=str(state or ""), updated=updated)
        if owner in {"yandex_music", "spotify", "discord"}:
            _OWNERS[owner] = {"state": str(state or ""), "updated": updated}


def _clear_if(owner: str) -> None:
    with _LOCK:
        _OWNERS.pop(owner, None)
        if str(_STATE.get("owner", "")) == owner:
            _STATE.update(owner="", state="", updated=time.monotonic())


def note_tool_execution(tool_name: str, arguments: dict[str, Any] | None, success: bool, data: Any = None) -> None:
    if not success:
        return
    tool = str(tool_name or "").casefold()
    args = dict(arguments or {})
    action = str(args.get("action", "") or "").casefold()
    action = {
        "play_wave": "my_wave", "play_my_wave": "my_wave", "wave": "my_wave",
        "play_my_tracks": "my_tracks", "play_song": "play_track",
        "play_music": "play", "start_track": "play_track",
        "play_playlist": "playlist",
    }.get(action, action)

    if tool in {"yandex_music", "spotify", "music"}:
        payload = data if isinstance(data, dict) else {}
        owner = str(payload.get("provider", "") or payload.get("active_provider", "") or tool).casefold()
        if owner == "music":
            owner = ""
        if owner not in {"yandex_music", "spotify"}:
            return
        if action in {"my_wave", "my_tracks", "playlist", "play_track", "play_song", "play_music", "play", "next", "previous", "queue_add", "queue_next"}:
            _set(owner, "playing")
        elif action == "pause":
            _set(owner, "paused")
        elif action == "play_pause":
            state = str(payload.get("state", "") or (payload.get("player", {}) or {}).get("state", "") if isinstance(payload.get("player"), dict) else "").casefold()
            _set(owner, "paused" if state == "paused" else "playing")
        elif action == "stop":
            _set(owner, "stopped")
        elif action in {"status", "background_status", "queue_status"}:
            player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
            state = str(payload.get("state", "") or player.get("state", "") or "").casefold()
            owns = bool(payload.get("owns_track") or payload.get("current") or payload.get("title") or player.get("title"))
            if state in {"playing", "buffering", "starting"}:
                _set(owner, "playing")
            elif state == "paused" or owns:
                _set(owner, "paused" if state == "paused" else "present")
        return

    if tool == "discord":
        if action in {"music_my_wave", "music_my_tracks", "music_playlist", "music_skip", "music_previous", "music_resume", "music_bot_play", "music_youtube_url"}:
            _set("discord", "playing")
        elif action == "music_pause":
            _set("discord", "paused")
        elif action in {"music_stop", "bot_leave_voice"}:
            _clear_if("discord")


def note_media_observation(owner: str, *, playing: bool, paused: bool = False) -> None:
    """Update process-wide ownership from passive playback observation.

    Unlike :func:`note_tool_execution`, this path is used when another JARVIS
    instance/user already owns the shared Discord queue.  A passive observer must
    still teach desktop quick-media routing which provider is audibly active.
    """
    provider = str(owner or "").strip().casefold()
    if not provider:
        return
    if bool(playing):
        _set(provider, "playing")
    elif bool(paused):
        _set(provider, "paused")
    else:
        with _LOCK:
            stopped = _OWNERS.get(provider, {}).get("state") == "stopped"
        if not stopped:
            _clear_if(provider)


def recent_music_owner(max_age_seconds: float = 7200.0) -> str:
    with _LOCK:
        owner = str(_STATE.get("owner", "") or "")
        updated = float(_STATE.get("updated", 0.0) or 0.0)
    if not owner or updated <= 0.0:
        return ""
    if time.monotonic() - updated > max(1.0, float(max_age_seconds)):
        return ""
    return owner


def snapshot() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATE)


def recent_music_states(max_age_seconds: float = 30.0) -> dict[str, str]:
    """Return fresh observed transport states without IO or starting a player.

    A last owner alone cannot represent simultaneous playback. These bounded
    per-owner hints reconcile a generic pause/resume after the first transport
    has already reacted, without probing a dormant Discord or Spotify session.
    Presence is deliberately retained as presence, never inferred as paused.
    """
    now = time.monotonic()
    with _LOCK:
        return {owner: row["state"] for owner, row in _OWNERS.items()
                if now - row["updated"] <= max(1.0, float(max_age_seconds))}
