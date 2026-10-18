# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from typing import Any, Callable

from jarvis.integrations import discord as dc
from jarvis.integrations.background.discord_bot import DiscordBackgroundBot, _CONTROL_MARKER, _public_control_data
from jarvis.integrations.yandex_music import YandexMusicAdapter
from jarvis.integrations.youtube_audio import YouTubeAudioResolver, standalone_youtube_url, explicit_playlist_url
from jarvis.runtime.media_events import publish_now_playing, is_player_facing_media_title, latest_now_playing, stale_playback_observation
from jarvis.runtime.media_levels import media_levels
from jarvis.integrations.media_state import PlaybackSessionStore, PREFETCH, latest_transport, transport_checkpoint, transport_guard, cancel_transport_requests, MediaRequestSuperseded, pin_cache, cache_is_pinned

logger = logging.getLogger(__name__)


def premature_finish188(elapsed: float, expected_ms: int, seen_active: bool) -> bool:
    """Was this finish a transport break rather than the natural end of the track?

    Unknown duration keeps the conservative 10 s floor. A known duration scales
    the floor (8 %, 10–22 s) but never above 80 % of the track itself, so a
    known 5-second jingle that ends after 6 s is a normal end (HIST-020), while
    a 1-second stop of the same jingle is still a real early break."""
    if not seen_active:
        return False
    expected_s = max(0, int(expected_ms or 0)) / 1000.0
    if expected_s <= 0:
        return elapsed < 10.0
    floor = min(22.0, max(10.0, expected_s * 0.08), expected_s * 0.8)
    return elapsed < floor

def _valid_generation188(value: Any) -> int | None:
    """HIST-018: a playback generation is a non-negative integer (absent means 0).

    Anything else ("invalid", -1, 1.5, True) is rejected before it can enter the
    status cache, where it would block every later valid update of that route."""
    if isinstance(value, bool):
        return None
    if value is None or value == "":
        return 0
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        number = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


DiscordPlaybackCancelled = MediaRequestSuperseded


# V5.4: desktop UI, Telegram and other transports may each create their own
# Agent/ToolRegistry, but Discord voice/Gateway state must remain process-wide.
# Keep one ReliableDiscordAdapter per ConfigManager object so those Agents share
# the same queues, Gateway clients, VoiceClient and now-playing state instead of
# racing the same bot token against itself.
_SHARED_ADAPTERS_LOCK = threading.RLock()
_SHARED_ADAPTERS: dict[int, "ReliableDiscordAdapter"] = {}


def _quick_switch_relaxed(prefix: str, query: str) -> dict[str, Any]:
    """Open Discord Quick Switcher and select a result without screenshot gating.

    The proven Discord music implementation is intentionally left untouched in
    ``discord.py``.  This wrapper only replaces navigation confirmation: a small
    screenshot delta after Enter is not treated as failure when the switcher is
    already closed.  Target verification happens afterwards against the Discord
    title/UIA state.
    """
    query = str(query or "").strip()
    if not query:
        raise ValueError("Не указано название для Discord Quick Switcher")

    window = dc.focus_discord_window()
    method, opened_frame = dc._open_quick_switcher_verified(window)
    dc._paste(f"{prefix}{query}")
    time.sleep(0.75)

    editor = dc._message_editor(window)
    if editor is not None:
        value = dc._n(dc._editor_value(editor))
        if dc._n(query) and dc._n(query) in value and not dc._quick_switcher_uia_visible(window):
            dc._clear_editor(editor)
            raise RuntimeError(
                "Ctrl+K не удержал Quick Switcher: поисковый текст попал в поле сообщения; Enter не отправлялся"
            )

    dc._discord_press("enter")
    time.sleep(0.35)
    closed_frame = dc._discord_visual_snapshot(bottom=False)
    close_delta = dc._visual_delta(opened_frame, closed_frame)
    still_open = dc._quick_switcher_uia_visible(window)
    if still_open:
        time.sleep(0.35)
        closed_frame = dc._discord_visual_snapshot(bottom=False)
        close_delta = max(close_delta, dc._visual_delta(opened_frame, closed_frame))
        still_open = dc._quick_switcher_uia_visible(window)

    logger.info(
        "Discord navigation wrapper selected prefix=%r query=%r method=%s close_delta=%.2f still_open=%s",
        prefix, query, method, close_delta, still_open,
    )
    if still_open:
        try:
            dc._discord_press("esc")
        except Exception:
            pass
        raise RuntimeError(f"Discord Quick Switcher не закрылся после выбора {prefix}{query!s}")

    return {
        "method": method,
        "query": query,
        "prefix": prefix,
        "switcher_closed": True,
        "visual_delta": close_delta,
    }


class ReliableDiscordAdapter(dc.DiscordAdapter):
    """Discord adapter with per-server official bots and a hands-free music queue."""

    def __init__(self, config: Any):
        super().__init__(config)
        self._background_bots: dict[str, DiscordBackgroundBot] = {}
        self._background_bots_lock = threading.RLock()
        self._yandex = YandexMusicAdapter(config)
        self._youtube = YouTubeAudioResolver(config)
        self._queue_lock = threading.RLock()
        self._queues: dict[str, dict[str, Any]] = {}
        self._session_stores: dict[str, PlaybackSessionStore] = {}
        self._instance_id = uuid.uuid4().hex[:16]
        self._bus_lock = threading.RLock()
        self._bus_pending: dict[str, dict[str, Any]] = {}
        self._bus_context = threading.local()
        self._host_cache: dict[str, tuple[float, dict[str, str]]] = {}
        self._remote_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._remote_snapshot_routes: dict[str, tuple[dict[str, Any], int]] = {}
        self._queue_probe_at: dict[str, float] = {}
        self._queue_probe_inflight: set[str] = set()
        # Snapshot admission owns no transport lock. Directed replies have a
        # finite lane separate from the replaceable unsolicited latest state.
        self._snapshot_condition = threading.Condition(threading.RLock())
        self._snapshot_owners: dict[str, dict[str, Any]] = {}
        self._snapshot_retiring: dict[str, threading.Thread] = {}
        self._snapshot_stops: set[str] = set()
        self._snapshot_epochs: dict[str, int] = {}
        self._snapshot_outcomes: dict[str, dict[str, Any]] = {}
        self._snapshot_probes: dict[str, dict[str, Any]] = {}
        self._snapshot_closed = False

    def on_network_changed(self, event: Any = None, *_args: Any) -> None:
        self._yandex.on_network_changed()
        self._host_cache.clear()
        self._remote_status_cache.clear()
        with self._background_bots_lock:
            bots = list(self._background_bots.values())
        for bot in bots:
            bot.on_network_changed(event)
        if isinstance(event, dict) and event.get("online") is False:
            return
        # discord.py already owns Gateway reconnect. Only a previously faulted
        # music session needs one controlled reconciliation attempt.
        with self._queue_lock:
            states = [(key, dict(state.get("profile") or {}), int(state.get("generation", 0)))
                      for key, state in self._queues.items()
                      if state.get("transport_faulted") and state.get("items") and not state.get("manual_paused")]
        for key, profile, generation in states:
            def recover(key: str = key, profile: dict[str, Any] = profile, generation: int = generation) -> None:
                with self._queue_lock:
                    state = self._queues.get(key) or {}
                    if not state.get("transport_faulted") or state.get("manual_paused") or int(state.get("generation", 0)) != generation:
                        return
                try:
                    live = self._background_for_profile(profile).playback_status(profile)
                    if live.get("paused"):
                        return
                    if live.get("playing"):
                        with self._queue_lock:
                            state["transport_faulted"] = False
                            state["transport_fault_reason"] = ""
                        self._start_queue_monitor(profile)
                        return
                    self.music_skip(str(profile.get("name", "")))
                except Exception:
                    logger.info("Discord playback recovery deferred until next user action", exc_info=True)
            PREFETCH.submit("discord-reconnect:" + key, recover)

    def prewarm_background(self, timeout_per_bot: float = 8.0) -> dict[str, Any]:
        """Connect configured Discord Gateway clients before the first command.

        Voice playback remains lazy; this only establishes the cheap Gateway
        observer/control connection so a fresh NEO can immediately discover a
        shared music host that another user already started.
        """
        if not self._background_enabled():
            return {"enabled": False, "ready": 0, "errors": []}
        ready = 0
        errors: list[str] = []
        seen: set[int] = set()
        for profile in self._candidate_profiles(""):
            if not str(profile.get("bot_token_encrypted", "") or "").strip():
                continue
            bot = self._background_for_profile(profile)
            ident = id(bot)
            if ident in seen:
                continue
            seen.add(ident)
            try:
                bot.ensure_ready(timeout=max(2.0, float(timeout_per_bot)))
                ready += 1
                # Do NOT send a control-bus probe during application startup. On
                # Windows this used run_coroutine_threadsafe while the Discord
                # Proactor loop was still stabilising and could trigger a native
                # access violation. Passive broadcasts and the existing lazy probe
                # on Queue/Like/Dislike remain available after startup.
            except Exception as exc:
                errors.append(f"{profile.get('name','Discord')}: {exc}")
                logger.info("Discord Gateway prewarm skipped for %r: %s", profile.get("name", ""), exc)
        return {"enabled": True, "ready": ready, "errors": errors}

    def _invalidate_remote_cache(self, profile: dict[str, Any]) -> None:
        key = self._profile_key(profile)
        self._host_cache.pop(key, None)

    @staticmethod
    def _compact_shared_entry(entry: dict[str, Any] | None) -> dict[str, Any]:
        raw = dict(entry or {})
        result: dict[str, Any] = {}
        title = str(raw.get("title", "") or "").strip()
        artist = str(raw.get("artist", "") or "").strip()
        query = str(raw.get("query", "") or "").strip()
        track_id = str(raw.get("track_id", "") or "").strip()
        if title:
            result["title"] = title[:96]
        elif query:
            result["query"] = query[:112]
        if artist:
            result["artist"] = artist[:96]
        if track_id:
            result["track_id"] = track_id[:80]
        for key in ("media_source", "source_mode", "source_title"):
            value = str(raw.get(key, "") or "").strip()
            if value:
                result[key] = value[:48]
        for key in ("expected_duration_ms", "duration_ms"):
            if raw.get(key) not in (None, ""):
                try:
                    result[key] = int(raw.get(key) or 0)
                except Exception:
                    pass
        return result

    def _cache_remote_snapshot(
        self, profile: dict[str, Any], data: dict[str, Any], *,
        _probe: dict[str, Any] | None = None, _legacy_probe: bool = False,
    ) -> dict[str, Any]:
        snapshot = dict(data or {})
        key = self._profile_key(profile)
        generation = _valid_generation188(snapshot.get("playback_generation", 0))
        if generation is None:
            logger.debug("Discord status snapshot rejected: invalid playback generation %r", snapshot.get("playback_generation"))
            return {}
        snapshot["playback_generation"] = generation
        # Minimal embedding adapters may not use publisher admission. Full
        # adapters commit cache/probe/provenance together, then invoke subscribers
        # outside every queue/admission lock so reentrant controls stay safe.
        condition = getattr(self, "_snapshot_condition", None)
        with condition if condition is not None else threading.RLock():
            epoch = getattr(self, "_snapshot_epochs", {}).get(key, 0)
            if condition is not None and (self._snapshot_closed or not self._snapshot_profile_current(profile)):
                return {}
            if _probe is not None and (self._snapshot_probes.get(key) is not _probe
                    or _probe["status"] != "pending" or _probe["epoch"] != epoch):
                return {}
            if _probe is not None and time.monotonic() >= _probe["expires"]:
                _probe.update(status="failed", error="snapshot_timeout")
                return {}
            routes = getattr(self, "_remote_snapshot_routes", None)
            if routes is None:
                routes = self._remote_snapshot_routes = {}
            previous = self._remote_status_cache.get(key)
            provenance = routes.get(key)
            if (previous and provenance and provenance[1] == epoch
                    and self._snapshot_route(provenance[0]) == self._snapshot_route(profile)):
                old = previous[1]
                old_generation = _valid_generation188(old.get("playback_generation", 0))
                # An invalid cached record (older build) is replaced, never compared.
                if old_generation is not None and stale_playback_observation(
                        str(snapshot.get("playback_scope", "") or ""), generation,
                        current_scope=str(old.get("playback_scope", "") or ""),
                        current_generation=old_generation):
                    return dict(old)
            self._remote_status_cache[key] = (time.monotonic(), snapshot)
            routes[key] = (dict(profile), epoch)
            probe = _probe or (getattr(self, "_snapshot_probes", {}).get(key) if _legacy_probe else None)
            if (probe is not None and probe["status"] == "pending" and probe["epoch"] == epoch
                    and self._snapshot_route(probe["profile"]) == self._snapshot_route(profile)
                    and time.monotonic() < probe["expires"] and not snapshot.get("shared_state_pending")
                    and snapshot.get("verified") is True):
                # Legacy hosts cannot echo a request ID. A fresh authenticated
                # snapshot satisfies the read, without claiming a correlated ACK.
                probe.update(status="sent", error="", via="legacy_queue_state" if _legacy_probe else "ack")
        if not self._remote_snapshot_current(profile, snapshot):
            return {}
        current = dict(snapshot.get("current") or {})
        title = self._prefer_media_title(current.get("title", ""), current.get("query", ""))
        if title and is_player_facing_media_title(title):
            publish_now_playing(
                "discord", title, str(current.get("artist", "") or ""),
                str(current.get("track_id", "") or ""),
                duration_ms=int(current.get("expected_duration_ms", 0) or current.get("duration_ms", 0) or 0),
                playing=bool(snapshot.get("playing", True)) and not bool(snapshot.get("paused", False)),
                reason="shared_queue_state", observation=True,
                playback_generation=int(snapshot.get("playback_generation", 0) or 0),
                playback_scope=str(snapshot.get("playback_scope", "") or ""),
            )
        return snapshot

    def _remote_snapshot_current(self, profile: dict[str, Any], snapshot: dict[str, Any]) -> bool:
        key = self._profile_key(profile)
        condition = getattr(self, "_snapshot_condition", None)
        with condition if condition is not None else threading.RLock():
            cached = self._remote_status_cache.get(key)
            provenance = getattr(self, "_remote_snapshot_routes", {}).get(key)
            return bool(cached and cached[1] is snapshot and provenance
                        and self._snapshot_route(provenance[0]) == self._snapshot_route(profile)
                        and provenance[1] == getattr(self, "_snapshot_epochs", {}).get(key, 0)
                        and not getattr(self, "_snapshot_closed", False)
                        and (condition is None or self._snapshot_profile_current(profile)))

    def _cached_shared_snapshot(self, server: str = "", *, max_age: float = 600.0) -> tuple[dict[str, Any], dict[str, Any]] | None:
        newest: tuple[float, dict[str, Any], dict[str, Any]] | None = None
        for profile in self._candidate_profiles(server):
            key = self._profile_key(profile)
            cached = self._remote_status_cache.get(key)
            if not cached:
                continue
            if not self._remote_snapshot_current(profile, cached[1]):
                continue
            age = time.monotonic() - float(cached[0])
            if age > max(0.1, float(max_age)):
                continue
            if newest is None or float(cached[0]) > newest[0]:
                newest = (float(cached[0]), profile, dict(cached[1] or {}))
        if newest is None:
            return None
        return newest[1], newest[2]

    def _wait_cached_shared_snapshot(self, server: str = "", *, timeout: float = 0.65) -> tuple[dict[str, Any], dict[str, Any]] | None:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            cached = self._cached_shared_snapshot(server, max_age=30 * 60)
            if cached is not None:
                return cached
            time.sleep(0.04)
        return self._cached_shared_snapshot(server, max_age=30 * 60)

    @staticmethod
    def _snapshot_route(profile: dict[str, Any]) -> tuple[str, ...]:
        # This tuple is private and must never be logged: it includes encrypted
        # routing credentials so a changed token cannot inherit an old ticket.
        return tuple(str(profile.get(key, "") or "").strip() for key in (
            "name", "guild_id", "bot_token_encrypted", "discord_user_id",
            "voice_channel", "voice_channel_id", "music_text_channel", "text_channel_id"))

    def _snapshot_profile_current(self, profile: dict[str, Any]) -> bool:
        try:
            route = self._snapshot_route(profile)
            return any(self._snapshot_route(saved) == route for saved in self._candidate_profiles(""))
        except Exception:
            return False

    def _prune_snapshot_state_locked(self, valid_keys: set[str]) -> None:
        for key, worker in list(self._snapshot_retiring.items()):
            if not worker.is_alive():
                self._snapshot_retiring.pop(key, None)
        for mapping in (self._snapshot_probes, self._snapshot_outcomes):
            for key in list(mapping):
                if key not in valid_keys:
                    mapping.pop(key, None)
        for key in list(self._snapshot_epochs):
            if key not in valid_keys and key not in self._snapshot_owners and key not in self._snapshot_retiring:
                self._snapshot_epochs.pop(key, None)

    def _snapshot_ticket_current(self, key: str, ticket: dict[str, Any]) -> bool:
        with self._snapshot_condition:
            return (not self._snapshot_closed and not ticket.get("cancelled")
                    and ticket["epoch"] == self._snapshot_epochs.get(key, 0))

    def _snapshot_result_locked(self, key: str, ticket: dict[str, Any], code: str) -> None:
        # A late provider result cannot replace an already-established terminal
        # cancellation, including its precise profile/close reason.
        ticket["outcome"] = ticket.get("cancel_reason") or code
        self._snapshot_outcomes[key] = {"code": code, "request_id": ticket.get("request_id", ""),
                                        "at": time.monotonic()}
        self._snapshot_outcomes[key]["code"] = ticket["outcome"]

    def _snapshot_error_locked(self, key: str, owner: dict[str, Any], ticket: dict[str, Any], code: str) -> bool:
        if self._snapshot_closed or len(owner["errors"]) >= 2 or ticket.get("cancelled"):
            return False
        error = dict(ticket, kind="error", error=code, attempts=0,
                     due=time.monotonic(), expires=time.monotonic() + 60.0, outcome="pending")
        owner["errors"].append(error)
        self._snapshot_condition.notify_all()
        return True

    def _broadcast_queue_snapshot(
        self, profile: dict[str, Any], *, target_bot_id: str = "", delay: float = 0.0,
        reanchor: bool = False, request_id: str = "", target_instance: str = "",
    ) -> dict[str, Any]:
        """Publish canonical queue state without a request/ACK dependency.

        The actual transport owner broadcasts small snapshots whenever the track
        changes.  Other NEO instances can therefore show Now Playing, Queue,
        Like and Dislike even if a one-shot control-bus ACK is lost.
        """
        self._persist_queue(profile)
        key = self._profile_key(profile)
        profile = dict(profile)
        target = str(target_bot_id or "").strip()
        now = time.monotonic()
        if request_id and (not target or not target_instance or len(str(request_id)) > 96
                           or len(str(target_instance)) > 64):
            return {"accepted": False, "error": "snapshot_invalid_request", "refusal_queued": False}
        if not self._snapshot_profile_current(profile):
            return {"accepted": False, "error": "snapshot_profile_changed", "refusal_queued": False}
        valid_keys = {self._profile_key(saved) for saved in self._candidate_profiles("")}
        launch = None
        with self._snapshot_condition:
            if self._snapshot_closed:
                return {"accepted": False, "error": "snapshot_closed", "refusal_queued": False}
            self._prune_snapshot_state_locked(valid_keys)
            owner = self._snapshot_owners.get(key)
            if owner is None:
                owner = {"profile": profile, "normal": None, "targets": [], "errors": [],
                         "current": None, "thread": None, "starting": True,
                         "revision": 0, "last_lane": "target", "error_turn": True,
                         "suspended": key in self._snapshot_stops,
                         "predecessor": self._snapshot_retiring.get(key)}
                self._snapshot_owners[key] = owner
                launch = owner
            owner["profile"] = profile
            if target and request_id:
                for pending in owner["targets"]:
                    if (not pending.get("cancelled") and pending["target"] == target
                            and pending["target_instance"] == target_instance
                            and pending["request_id"] == request_id
                            and pending["epoch"] == self._snapshot_epochs.get(key, 0)):
                        return {"accepted": True, "duplicate": True, "ticket": pending}
            if not target:
                owner["revision"] += 1
            ticket = {"kind": "target" if target else "normal", "profile": profile,
                      "epoch": self._snapshot_epochs.get(key, 0), "target": target,
                      "request_id": str(request_id or "")[:96],
                      "target_instance": str(target_instance or "")[:64],
                      "reanchor": bool(reanchor), "due": now + min(0.5, max(0.0, float(delay))),
                      "expires": now + 60.0, "attempts": 0, "outcome": "pending"}
            if target and len(owner["targets"]) >= 8:
                self._snapshot_result_locked(key, ticket, "snapshot_busy")
                queued = self._snapshot_error_locked(key, owner, ticket, "snapshot_busy")
                return {"accepted": False, "error": "snapshot_busy", "refusal_queued": queued,
                        "ticket": ticket}
            if target:
                owner["targets"].append(ticket)
            else:
                pending = owner["normal"]
                if pending is not None:
                    ticket["reanchor"] = ticket["reanchor"] or pending["reanchor"]
                    ticket["due"] = max(ticket["due"], pending["due"])
                    self._snapshot_result_locked(key, pending, "superseded")
                owner["normal"] = ticket
            self._snapshot_condition.notify_all()
        if launch is not None and not self._start_snapshot_owner(key, launch):
            return {"accepted": False, "error": ticket["outcome"], "refusal_queued": False,
                    "ticket": ticket}
        return {"accepted": True, "ticket": ticket}

    def _start_snapshot_owner(self, key: str, owner: dict[str, Any]) -> bool:
        worker = None
        try:
            worker = threading.Thread(target=self._run_snapshot_owner, args=(key, owner),
                                      name="jarvis-discord-queue-state", daemon=True)
            with self._snapshot_condition:
                owner["thread"] = worker
            worker.start()
            with self._snapshot_condition:
                owner["starting"] = False
                self._snapshot_condition.notify_all()
            return True
        except Exception:
            if worker is not None and worker.is_alive():
                with self._snapshot_condition:
                    owner["starting"] = False
                    self._snapshot_condition.notify_all()
                return True
            with self._snapshot_condition:
                for ticket in [owner["normal"], *owner["targets"], *owner["errors"]]:
                    if ticket is not None:
                        self._snapshot_result_locked(key, ticket, "snapshot_start_failed")
                if self._snapshot_owners.get(key) is owner:
                    self._snapshot_owners.pop(key, None)
                owner["starting"] = False
                self._snapshot_condition.notify_all()
            logger.warning("Discord queue-state publisher could not start")
            return False

    def _next_snapshot_ticket_locked(self, owner: dict[str, Any]) -> dict[str, Any] | None:
        if owner["errors"] and owner["error_turn"]:
            owner["error_turn"] = False
            return owner["errors"][0]
        normal = owner["normal"]
        targets = owner["targets"]
        if normal is not None and (not targets or owner["last_lane"] == "target"):
            owner["normal"] = None
            owner["last_lane"] = "normal"
            owner["error_turn"] = True
            return normal
        if targets:
            owner["last_lane"] = "target"
            owner["error_turn"] = True
            return targets[0]
        if owner["errors"]:
            return owner["errors"][0]
        return None

    def _run_snapshot_owner(self, key: str, owner: dict[str, Any]) -> None:
        try:
            predecessor = owner.get("predecessor")
            if predecessor is not None:
                # At most one exiting predecessor and one successor can exist.
                # The successor performs no work before the old OS thread exits.
                # This join is on our own return-only tail, outside every lock.
                predecessor.join()
                with self._snapshot_condition:
                    if self._snapshot_retiring.get(key) is predecessor:
                        self._snapshot_retiring.pop(key, None)
                    owner["predecessor"] = None
            while True:
                with self._snapshot_condition:
                    ticket = self._next_snapshot_ticket_locked(owner)
                    if ticket is None:
                        if self._snapshot_owners.get(key) is owner:
                            self._snapshot_retiring[key] = owner["thread"]
                            self._snapshot_owners.pop(key, None)
                        self._snapshot_condition.notify_all()
                        return
                    owner["current"] = ticket
                    while not self._snapshot_closed and not ticket.get("cancelled"):
                        remaining = ticket["due"] - time.monotonic()
                        if not owner["suspended"] and remaining <= 0:
                            break
                        self._snapshot_condition.wait(None if owner["suspended"] else max(0.0, remaining))
                    revision = owner["revision"]
                code = "snapshot_cancelled"
                if self._snapshot_ticket_current(key, ticket):
                    if time.monotonic() >= ticket["expires"]:
                        code = "snapshot_timeout"
                    else:
                        ticket["attempts"] += 1
                        try:
                            code = self._send_snapshot_ticket(key, owner, ticket, revision)
                        except Exception:
                            code = "snapshot_send_failed"
                            logger.warning("Discord queue-state publication failed")
                with self._snapshot_condition:
                    owner["current"] = None
                    code = ticket.get("cancel_reason") or code
                    current = (not self._snapshot_closed and not ticket.get("cancelled")
                               and ticket["epoch"] == self._snapshot_epochs.get(key, 0))
                    stale = code == "snapshot_superseded" and current
                    retry = (code == "snapshot_send_failed" and current
                             and ticket["attempts"] < 2 and time.monotonic() < ticket["expires"])
                    if stale or retry:
                        if stale:
                            # A resample is not another failed-send attempt.
                            ticket["attempts"] = max(0, ticket["attempts"] - 1)
                        if ticket["kind"] == "normal":
                            newer = owner["normal"]
                            if newer is None:
                                owner["normal"] = ticket
                            else:
                                newer["reanchor"] = newer["reanchor"] or ticket["reanchor"]
                                newer["due"] = max(newer["due"], ticket["due"])
                                self._snapshot_result_locked(key, ticket, "superseded")
                        continue
                    lane = owner["targets"] if ticket["kind"] == "target" else owner["errors"]
                    if ticket["kind"] != "normal":
                        lane[:] = [item for item in lane if item is not ticket]
                    self._snapshot_result_locked(key, ticket, code)
                    if ticket["kind"] == "target" and code not in {"sent", "snapshot_cancelled"}:
                        self._snapshot_error_locked(key, owner, ticket, code)
                    self._snapshot_condition.notify_all()
                if code not in {"sent", "snapshot_cancelled"}:
                    logger.warning("Discord snapshot completed without confirmed delivery: %s", code)
        finally:
            with self._snapshot_condition:
                # Keep an unexpected failed owner from stranding accepted work.
                if self._snapshot_owners.get(key) is owner:
                    for ticket in [owner["normal"], owner["current"], *owner["targets"], *owner["errors"]]:
                        if ticket is not None and ticket.get("outcome") == "pending":
                            self._snapshot_result_locked(key, ticket, "snapshot_worker_failed")
                    self._snapshot_owners.pop(key, None)
                    self._snapshot_retiring[key] = owner["thread"]
                self._snapshot_condition.notify_all()

    def _send_snapshot_ticket(self, key: str, owner: dict[str, Any], ticket: dict[str, Any], revision: int) -> str:
        profile = ticket["profile"]
        if not self._snapshot_profile_current(profile) or not self._snapshot_ticket_current(key, ticket):
            return "snapshot_cancelled"
        if ticket["kind"] == "error":
            packet = {"kind": "ack" if ticket["request_id"] else "queue_probe_error",
                      "sender_instance": self._instance_id, "target_instance": ticket["target_instance"],
                      "request_id": ticket["request_id"], "guild_id": str(profile.get("guild_id", "") or ""),
                      "server": str(profile.get("name", "") or ""), "ok": False,
                      "error": ticket["error"], "snapshot_error": ticket["error"]}
            packet = self._bounded_snapshot_reply(packet, ticket["target"])
            if packet is None:
                return "snapshot_payload_too_large"
            try:
                self._background_for_profile(profile).send_control(profile, packet, target_bot_id=ticket["target"])
            except Exception:
                # The send may have reached Discord before its result was lost.
                # Never replay an ambiguous reply under the same request ID.
                return "snapshot_reply_unconfirmed"
            return "sent"
        snapshot = self._local_queue_snapshot(profile, _publication_ticket=ticket)
        if snapshot.get("shared_state_pending"):
            return "snapshot_send_failed"
        if not bool(snapshot.get("connected")) and not snapshot.get("current"):
            return "snapshot_unavailable"
        if not self._snapshot_ticket_current(key, ticket) or not self._snapshot_profile_current(profile):
            return "snapshot_cancelled"
        if time.monotonic() >= ticket["expires"]:
            return "snapshot_timeout"
        with self._snapshot_condition:
            if revision != owner["revision"]:
                return "snapshot_superseded"
        packet = {
                    "kind": "queue_state", "sender_instance": self._instance_id,
                    "guild_id": str(profile.get("guild_id", "") or ""),
                    "server": str(profile.get("name", "") or ""),
                    "data": {
                        "server": snapshot.get("server", ""),
                        "guild_id": snapshot.get("guild_id", ""),
                        "playback_scope": snapshot.get("playback_scope", ""),
                        "playback_generation": snapshot.get("playback_generation", 0),
                        "current": self._compact_shared_entry(dict(snapshot.get("current") or {})),
                        "queue": [self._compact_shared_entry(dict(x or {})) for x in list(snapshot.get("queue") or [])[:8]],
                        "queue_length": int(snapshot.get("queue_length", 0) or 0),
                        "history": [self._compact_shared_entry(dict(x or {})) for x in list(snapshot.get("history") or [])[-5:]],
                        "history_length": int(snapshot.get("history_length", 0) or 0),
                        "source_mode": snapshot.get("source_mode", ""),
                        "source_title": snapshot.get("source_title", ""),
                        "shuffle": bool(snapshot.get("shuffle", False)),
                        "repeat_mode": str(snapshot.get("repeat_mode", "off") or "off"),
                        "playing": bool(snapshot.get("playing", False)),
                        "paused": bool(snapshot.get("paused", False)),
                        "connected": bool(snapshot.get("connected", False)),
                        "levels": list(snapshot.get("levels") or [])[:96],
                        "audio_levels_real": bool(snapshot.get("audio_levels_real", False)),
                        "audio_levels_kind": str(snapshot.get("audio_levels_kind", "") or "")[:24],
                        "verified": True, "method": "discord_shared_queue_broadcast",
                        "shared_queue": True,
                    },
                }
        bot = self._background_for_profile(profile)
        if bot.publish_music_state(profile, dict(packet["data"]), wait=True, reanchor=ticket["reanchor"]) is False:
            return "snapshot_send_failed"
        if not self._snapshot_ticket_current(key, ticket) or not self._snapshot_profile_current(profile):
            return "snapshot_cancelled"
        if time.monotonic() >= ticket["expires"]:
            return "snapshot_timeout"
        with self._snapshot_condition:
            if revision != owner["revision"]:
                return "snapshot_superseded"
        if ticket["request_id"]:
            packet.update(kind="ack", ok=True, request_id=ticket["request_id"],
                          target_instance=ticket["target_instance"])
            packet = self._bounded_snapshot_reply(packet, ticket["target"])
            if packet is None:
                return "snapshot_payload_too_large"
        try:
            bot.send_control(profile, packet, target_bot_id=ticket["target"])
        except Exception:
            return "snapshot_reply_unconfirmed"
        return "sent"

    @staticmethod
    def _bounded_snapshot_reply(packet: dict[str, Any], target: str) -> dict[str, Any] | None:
        """Fit the unchanged bot's complete addressed envelope, not just data.

        Dashboard state was already published in full. Only the wire preview is
        compacted; recipient, receipt/session identity and true totals survive.
        UTF-16 units conservatively cover astral metadata as well as JSON escapes.
        """
        result = dict(packet)
        data = _public_control_data(dict(result.get("data") or {})) if "data" in result else None
        if data is not None:
            result["data"] = data

        def fits() -> bool:
            wire = dict(result, target_bot_id=str(target or "").strip())
            if wire.get("error"):
                wire["error"] = "The remote NEO operation failed; inspect its local diagnostics."
            mention = f"<@{wire['target_bot_id']}> " if wire["target_bot_id"] else ""
            body = "||" + mention + _CONTROL_MARKER + " " + json.dumps(wire, ensure_ascii=False, separators=(",", ":")) + "||"
            return len(body.encode("utf-16-le")) // 2 <= 1900

        if fits():
            return result
        if data is not None:
            data["levels"] = []
            data["audio_levels_real"] = False
            data["audio_levels_kind"] = ""
            queue = data.get("queue") or []
            history = data.get("history") or []
            while not fits() and (queue or history):
                if queue and len(queue) >= len(history):
                    queue.pop()
                elif history:
                    history.pop(0)
            for optional in ("source_title", "server", "method", "levels", "audio_levels_kind", "audio_levels_real"):
                if fits():
                    return result
                data.pop(optional, None)
            current = data.get("current") or {}
            for optional in ("query", "source_title", "source_mode", "media_source", "duration_ms", "expected_duration_ms"):
                if fits():
                    return result
                current.pop(optional, None)
        if not fits():
            result.pop("server", None)
        return result if fits() else None

    def cancel_snapshot_session(self, profile: dict[str, Any], *, code: str = "snapshot_cancelled") -> None:
        key = self._profile_key(profile)
        with self._queue_lock:
            with self._snapshot_condition:
                self._snapshot_epochs[key] = self._snapshot_epochs.get(key, 0) + 1
                owner = self._snapshot_owners.get(key)
                if owner is not None:
                    for ticket in [owner["normal"], owner["current"], *owner["targets"], *owner["errors"]]:
                        if ticket is not None:
                            ticket["cancelled"] = True
                            ticket.setdefault("cancel_reason", code)
                            self._snapshot_result_locked(key, ticket, code)
                    owner["normal"] = None
                    active = owner["current"]
                    owner["targets"][:] = [active] if active is not None and active["kind"] == "target" else []
                    owner["errors"][:] = [active] if active is not None and active["kind"] == "error" else []
                    owner["revision"] += 1
                probe = self._snapshot_probes.get(key)
                if probe is not None and probe["status"] == "pending":
                    probe.update(status="failed", error=code)
                self._remote_status_cache.pop(key, None)
                self._remote_snapshot_routes.pop(key, None)
                self._snapshot_condition.notify_all()

    def _suspend_snapshot_stop(self, profile: dict[str, Any], suspended: bool) -> None:
        key = self._profile_key(profile)
        with self._snapshot_condition:
            owner = self._snapshot_owners.get(key)
            if suspended:
                self._snapshot_stops.add(key)
                self._snapshot_epochs[key] = self._snapshot_epochs.get(key, 0) + 1
                probe = self._snapshot_probes.get(key)
                if probe is not None and probe["status"] == "pending":
                    probe.update(status="failed", error="snapshot_cancelled")
            else:
                self._snapshot_stops.discard(key)
            if owner is not None:
                owner["suspended"] = suspended
                if suspended:
                    owner["revision"] += 1
                    # An admitted directed read survives Stop and resamples the
                    # stopped state. Its old already-built payload cannot send.
                    for ticket in owner["targets"]:
                        ticket["epoch"] = self._snapshot_epochs[key]
                    pending = owner["normal"]
                    if pending is not None:
                        pending["epoch"] = self._snapshot_epochs[key]
                self._snapshot_condition.notify_all()

    def refresh_snapshot_profiles(self) -> None:
        valid_keys = {self._profile_key(profile) for profile in self._candidate_profiles("")}
        with self._snapshot_condition:
            profiles = [dict(owner["profile"]) for owner in self._snapshot_owners.values()]
            profiles += [dict(probe["profile"]) for probe in self._snapshot_probes.values()]
            profiles += [dict(provenance[0]) for provenance in self._remote_snapshot_routes.values()]
        for profile in profiles:
            if not self._snapshot_profile_current(profile):
                self.cancel_snapshot_session(profile, code="snapshot_profile_changed")
                self._remote_status_cache.pop(self._profile_key(profile), None)
        with self._snapshot_condition:
            self._prune_snapshot_state_locked(valid_keys)

    def close_snapshot_publication(self, timeout: float = 0.5) -> bool:
        with self._snapshot_condition:
            self._snapshot_closed = True
            profiles = [dict(owner["profile"]) for owner in self._snapshot_owners.values()]
            for probe in self._snapshot_probes.values():
                if probe["status"] == "pending":
                    probe.update(status="failed", error="snapshot_closed")
            self._snapshot_condition.notify_all()
        for profile in profiles:
            self.cancel_snapshot_session(profile, code="snapshot_closed")
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._snapshot_condition:
            owners = list(self._snapshot_owners.values())
            workers = list(self._snapshot_retiring.values())
            workers += [owner["thread"] for owner in owners if owner.get("thread") is not None and not owner.get("starting")]
        for worker in set(workers):
            if worker is not threading.current_thread():
                worker.join(max(0.0, deadline - time.monotonic()))
        with self._snapshot_condition:
            for key, worker in list(self._snapshot_retiring.items()):
                if not worker.is_alive():
                    self._snapshot_retiring.pop(key, None)
            return not self._snapshot_owners and not self._snapshot_retiring

    def _snapshot_probe_outcome(self, profile: dict[str, Any]) -> dict[str, Any]:
        key = self._profile_key(profile)
        with self._snapshot_condition:
            probe = self._snapshot_probes.get(key)
            if probe is None:
                return {}
            if probe["status"] == "pending" and time.monotonic() >= probe["expires"]:
                probe.update(status="failed", error="snapshot_timeout")
            if probe["status"] == "failed":
                probe["reported"] = True
                return {"verified": False, "shared_state_pending": False,
                        "shared_state_error": probe["error"], "snapshot_request_id": probe["request_id"]}
            return {}

    def _snapshot_terminal_result(self, server: str, result: dict[str, Any]) -> dict[str, Any]:
        for profile in self._candidate_profiles(server):
            failure = self._snapshot_probe_outcome(profile)
            if failure:
                if (result.get("verified") is True and not result.get("shared_state_pending")
                        and (result.get("current") or result.get("connected"))):
                    return {**result, "snapshot_refresh_error": failure["shared_state_error"]}
                return {**result, **failure}
        return result

    def _receive_snapshot_reply(self, payload: dict[str, Any]) -> bool:
        request_id = str(payload.get("request_id", "") or "")
        if not request_id.startswith("snapshot:"):
            return False
        if str(payload.get("target_instance", "") or "") != self._instance_id:
            return True
        accepted = None
        with self._snapshot_condition:
            for key, probe in self._snapshot_probes.items():
                if probe["request_id"] != request_id:
                    continue
                if (self._snapshot_closed or probe["status"] != "pending"
                        or probe["epoch"] != self._snapshot_epochs.get(key, 0)):
                    return True
                if time.monotonic() >= probe["expires"]:
                    probe.update(status="failed", error="snapshot_timeout")
                    return True
                profile = dict(probe["profile"])
                if not self._snapshot_profile_current(profile):
                    probe.update(status="failed", error="snapshot_profile_changed")
                    return True
                if (str(payload.get("guild_id", "") or "") != str(profile.get("guild_id", "") or "")
                        or not str(payload.get("_message_author_bot_id", "") or "")):
                    return True
                data = payload.get("data")
                if (bool(payload.get("ok")) and isinstance(data, dict)
                        and data.get("verified") is True and not data.get("shared_state_pending")):
                    accepted = (profile, dict(payload["data"]), probe)
                elif bool(payload.get("ok")):
                    probe.update(status="failed", error="snapshot_unavailable")
                else:
                    allowed = {"snapshot_busy", "snapshot_send_failed", "snapshot_reply_unconfirmed",
                               "snapshot_timeout", "snapshot_unavailable", "snapshot_cancelled",
                               "snapshot_profile_changed", "snapshot_closed", "snapshot_start_failed",
                               "snapshot_payload_too_large"}
                    code = str(payload.get("snapshot_error", "") or "")
                    probe.update(status="failed", error=code if code in allowed else "snapshot_send_failed")
                break
        if accepted is not None:
            profile, snapshot, probe = accepted
            snapshot = self._cache_remote_snapshot(profile, snapshot, _probe=probe)
            # Preserve the legacy directed queue_state receiver's dashboard
            # mirror, after releasing queue/admission locks. Bot async edit
            # ownership remains unchanged and outside this admission fix.
            if snapshot and self._remote_snapshot_current(profile, snapshot):
                try:
                    self._background_for_profile(profile).publish_music_state(profile, dict(snapshot))
                except Exception:
                    logger.debug("Discord snapshot reply dashboard mirror failed", exc_info=True)
        return True

    def _request_queue_snapshot(self, profile: dict[str, Any], *, force: bool = False) -> None:
        """Ask the current shared Discord host for a passive queue snapshot.

        A failed cold-start probe must not poison the 15 s throttle.  V4.1 could
        start this helper before Discord Gateway was actually READY, record the
        throttle timestamp, fail the send and then stay blind to an already
        playing remote track until the user issued an explicit Discord command.

        Keep only one probe per guild in flight and commit the throttle timestamp
        *after* the broadcast was submitted successfully.  ``force`` is reserved
        for the post-Gateway cold-attach handshake.
        """
        key = self._profile_key(profile)
        now = time.monotonic()
        with self._snapshot_condition:
            if self._snapshot_closed:
                return
            previous = self._snapshot_probes.get(key)
            if previous is not None and previous["status"] == "pending":
                if now < previous["expires"]:
                    return
                previous.update(status="failed", error="snapshot_timeout")
            if previous is not None and previous["status"] == "failed" and not previous.get("reported"):
                return
        with self._bus_lock:
            last = float(self._queue_probe_at.get(key, 0.0) or 0.0)
            if key in self._queue_probe_inflight:
                return
            if not force and now - last < 15.0:
                return
            self._queue_probe_inflight.add(key)
        with self._snapshot_condition:
            epoch = self._snapshot_epochs.get(key, 0)
            request_id = f"snapshot:{self._instance_id}:{epoch}:{uuid.uuid4().hex}"
            probe = {"request_id": request_id, "profile": dict(profile), "epoch": epoch,
                     "expires": now + 60.0, "status": "pending", "error": "", "reported": False}
            self._snapshot_probes[key] = probe

        def _probe() -> None:
            try:
                packet = {
                    "kind": "queue_probe", "sender_instance": self._instance_id,
                    "guild_id": str(profile.get("guild_id", "") or ""),
                    "server": str(profile.get("name", "") or ""),
                    "request_id": request_id,
                }
                with self._snapshot_condition:
                    if self._snapshot_closed or probe["status"] != "pending" or epoch != self._snapshot_epochs.get(key, 0):
                        if probe["status"] == "pending":
                            probe.update(status="failed", error="snapshot_cancelled")
                        return
                if not self._snapshot_profile_current(profile):
                    with self._snapshot_condition:
                        probe.update(status="failed", error="snapshot_profile_changed")
                    return
                self._background_for_profile(profile).send_control(profile, packet, target_bot_id="")
                with self._bus_lock:
                    self._queue_probe_at[key] = time.monotonic()
                logger.debug("Discord queue-state probe submitted for %s force=%s", key, bool(force))
            except Exception as exc:
                with self._snapshot_condition:
                    if probe["status"] == "pending":
                        probe.update(status="failed", error="snapshot_reply_unconfirmed")
                logger.debug("Discord queue-state probe skipped: %s", exc)
            finally:
                with self._bus_lock:
                    self._queue_probe_inflight.discard(key)
        try:
            threading.Thread(target=_probe, name="jarvis-discord-queue-probe", daemon=True).start()
        except Exception:
            with self._snapshot_condition:
                probe.update(status="failed", error="snapshot_start_failed")
            with self._bus_lock:
                self._queue_probe_inflight.discard(key)
            logger.warning("Discord queue-state probe could not start")

    def observe_shared_playback(
        self, server: str = "", *, force_probe: bool = False, wait: float = 0.7,
    ) -> dict[str, Any]:
        """Cold-attach to Discord playback that another NEO already owns.

        This is a read-only observer operation.  It never joins voice and never
        starts/stops media.  It is safe to call after Gateway READY and is used by
        the desktop at startup/reconnect so an already playing Discord track is
        reflected in the robot waveform and now-playing history without waiting
        for the user's first media command.
        """
        cached = self._cached_shared_snapshot(server, max_age=30 * 60)
        if cached is not None:
            profile, snapshot = cached
            current = dict(snapshot.get("current") or {})
            if current:
                # Refresh in the background but return immediately with the last
                # authoritative host broadcast so UI attachment has no latency.
                self._request_queue_snapshot(profile, force=bool(force_probe))
                return self._snapshot_terminal_result(server, {**snapshot, "cached_shared_snapshot": True})

        profiles = self._candidate_profiles(server)
        for profile in profiles:
            try:
                bot = self._background_for_profile(profile)
                bot.ensure_ready(timeout=4.0)
            except Exception:
                continue
            try:
                local = self._local_queue_snapshot(profile)
                if bool(local.get("connected")) or local.get("current"):
                    return local
            except Exception:
                pass
            self._request_queue_snapshot(profile, force=bool(force_probe))

        waited = self._wait_cached_shared_snapshot(server, timeout=max(0.0, min(2.0, float(wait))))
        if waited is not None:
            return {**waited[1], "shared_snapshot_after_probe": True}

        # Direct host election is the final cold-start fallback.  It is bounded
        # and still read-only; successful queue_status also populates the passive
        # snapshot cache for later 5 s observer ticks.
        try:
            remote = self._remote_for_any_profile(server)
            if remote:
                profile, host = remote
                return self._remote_request(profile, host, "queue_status", timeout=1.6)
        except Exception:
            pass
        return self._snapshot_terminal_result(server, {
            "server": str(server or ""), "current": {}, "queue": [], "queue_length": 0,
            "playing": False, "paused": False, "verified": True, "shared_queue": True,
            "shared_state_pending": True, "method": "discord_cold_attach_pending",
        })

    def _remote_request(
        self, profile: dict[str, Any], host: dict[str, str], action: str,
        payload: dict[str, Any] | None = None, *, timeout: float = 4.0,
    ) -> dict[str, Any]:
        """Re-elect only for read-only probes; mutation timeout is ambiguous.

        A host may have advanced the track before its ACK was lost. Replaying a
        mutation with another UUID (or another host) would advance it twice.
        """
        key = self._profile_key(profile)
        last: Exception | None = None
        candidate = dict(host)
        for attempt in range(2):
            try:
                data = self._bus_request(
                    profile, candidate["bot_id"], action, payload, timeout=timeout,
                    requester_bot_id=candidate.get("requester_bot_id", ""),
                )
                if action == "queue_status" and isinstance(data, dict):
                    data = self._cache_remote_snapshot(profile, dict(data))
                return data
            except Exception as exc:
                last = exc
                self._invalidate_remote_cache(profile)
                if attempt == 0 and action in {"queue_status", "ping"}:
                    try:
                        fresh = self._remote_host(profile)
                    except Exception:
                        fresh = None
                    if fresh:
                        candidate = fresh
                        continue
                break
        if action == "queue_status":
            cached = self._remote_status_cache.get(key)
            if cached and time.monotonic() - cached[0] <= 6.0:
                return {**dict(cached[1]), "stale_shared_snapshot": True}
        if last is not None:
            raise last
        raise RuntimeError("Не удалось связаться с владельцем общей Discord-очереди")

    def _background_enabled(self) -> bool:
        return bool(self.config.get("integrations.discord.enabled", True)) and bool(
            self.config.get("integrations.discord.background_bot_enabled", True)
        )

    def _never_steal_input(self) -> bool:
        # v4.0.11: Discord integration is background/API-only. Desktop UI
        # automation is intentionally unreachable.
        return True

    def _ui_fallback(self) -> bool:
        return False

    def _background_profile(self, server: str = "") -> dict[str, str]:
        """Return the full saved profile including its per-server encrypted token."""
        requested = str(server or "").strip()
        default_name = str(self.config.get("integrations.discord.default_server", "") or "").strip()
        wanted = (requested or default_name).casefold().strip()
        raw = self.config.get("integrations.discord.servers", []) or []
        candidates: list[dict[str, Any]] = [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

        selected: dict[str, Any] | None = None
        # NEO 187 (REC-DISCORD-GUILD-NAME-OVERRIDES-ID-01): a known guild ID is
        # the stable identity; a display name is only a label.
        if requested.isdigit():
            selected = next((item for item in candidates if str(item.get("guild_id", "") or "").strip() == requested), None)
        if selected is None and wanted:
            selected = next((item for item in candidates if str(item.get("name", "") or "").casefold().strip() == wanted), None)
        if selected is None and not requested and len(candidates) == 1:
            selected = candidates[0]
        if selected is None and requested and candidates:
            legacy = dict(self.resolve_profile(requested))
            legacy_name = str(legacy.get("name", "") or "").casefold().strip()
            selected = next((item for item in candidates if str(item.get("name", "") or "").casefold().strip() == legacy_name), None)
        if selected is None:
            selected = dict(self.resolve_profile(requested))

        return {
            "name": str(selected.get("name", "") or requested or default_name).strip(),
            "bot_token_encrypted": str(selected.get("bot_token_encrypted", "") or "").strip(),
            "guild_id": str(selected.get("guild_id", "") or "").strip(),
            "discord_user_id": str(selected.get("discord_user_id", "") or "").strip(),
            "voice_channel": str(selected.get("voice_channel", "") or self.config.get("integrations.discord.default_voice_channel", "") or "").strip(),
            "voice_channel_id": str(selected.get("voice_channel_id", "") or "").strip(),
            "music_text_channel": str(selected.get("music_text_channel", "") or self.config.get("integrations.discord.music_text_channel", "") or "").strip(),
            "text_channel_id": str(selected.get("text_channel_id", "") or "").strip(),
            "jarvis_bot_name": str(selected.get("jarvis_bot_name", "") or "").strip(),
            "music_bot_name": str(selected.get("music_bot_name", "") or "").strip(),
        }

    def _candidate_profiles(self, server: str = "") -> list[dict[str, str]]:
        """Profiles to try for a hands-free command.

        With an explicit server we never guess. Without one, the configured
        default is tried first and then other server rows. This lets a user move
        to another Discord guild with a different NEO bot/token and simply
        speak the same command; the profile whose bot can actually see the
        configured Discord User ID in voice wins.
        """
        requested = str(server or "").strip()
        if requested:
            return [self._background_profile(requested)]
        raw = self.config.get("integrations.discord.servers", []) or []
        names = [
            str(item.get("name", "") or "").strip()
            for item in raw if isinstance(item, dict) and str(item.get("name", "") or "").strip()
        ] if isinstance(raw, list) else []
        default_name = str(self.config.get("integrations.discord.default_server", "") or "").strip()
        ordered: list[str] = []
        for name in [default_name, *names]:
            if name and name.casefold() not in {x.casefold() for x in ordered}:
                ordered.append(name)
        return [self._background_profile(name) for name in ordered] or [self._background_profile("")]

    @staticmethod
    def _profile_retryable_error(exc: Exception) -> bool:
        text = str(exc or "").casefold()
        return any(key in text for key in (
            "сейчас не найден ни в одном", "не видит пользователя", "зайдите в voice-канал",
            "не сохранён bot token", "не нашёл сервер", "не подключился", "gateway", "token",
        ))

    @staticmethod
    def _profile_key(profile: dict[str, Any]) -> str:
        guild_id = str(profile.get("guild_id", "") or "").strip()
        name = str(profile.get("name", "") or "").casefold().strip()
        return guild_id or name or "default"

    def _background_for_profile(self, profile: dict[str, Any]) -> DiscordBackgroundBot:
        encrypted = str(profile.get("bot_token_encrypted", "") or "").strip()
        # Do not expose the encrypted blob in dictionary/debug output; only use a
        # short one-way fingerprint to select the correct long-lived bot client.
        fingerprint = hashlib.sha256(encrypted.encode("utf-8", errors="ignore")).hexdigest()[:20] if encrypted else "legacy"
        # One Discord client per distinct bot token. If the same bot is installed
        # on several guilds, all those server rows reuse one Gateway connection;
        # different tokens create independent bot clients.
        key = fingerprint
        with self._background_bots_lock:
            bot = self._background_bots.get(key)
            if bot is None:
                bot = DiscordBackgroundBot(self.config, token_encrypted=encrypted)
                bot.set_control_handler(self._handle_bus_message)
                bot.set_music_control_handler(self._handle_music_panel_action)
                self._background_bots[key] = bot
            else:
                bot.set_control_handler(self._handle_bus_message)
                bot.set_music_control_handler(self._handle_music_panel_action)
            return bot

    def _handle_music_panel_action(self, profile: dict[str, Any], action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute Discord message-component actions through the canonical queue.

        Persistent Discord views can be clicked immediately after restart, before
        a fresh queue snapshot has repopulated ``_music_profiles``.  In that case
        the callback may know only the guild id.  Resolve that id back to the
        configured server row instead of accidentally falling through to an
        unrelated default guild.
        """
        server = str(profile.get("name", "") or "").strip()
        guild_id = str(profile.get("guild_id", "") or "").strip()
        if guild_id:
            # REC-DISCORD-GUILD-NAME-OVERRIDES-ID-01: the callback's guild ID
            # selects the configured row; its (possibly renamed or duplicated)
            # display name never switches the action to another guild.
            server = self._server_key_for_guild(guild_id, server)
        # HOTFIX97: persistent Discord component callbacks may arrive with only
        # guild/name identity. Resolve that identity back to the configured row
        # before queue/status publication so _background_for_profile never opens
        # a second "legacy" Discord client without this server's encrypted token.
        if server:
            configured_profile = self._background_profile(server)
            if configured_profile.get("bot_token_encrypted") or not profile.get("bot_token_encrypted"):
                profile = configured_profile
        action = str(action or "").strip().casefold()
        payload = dict(payload or {})
        if action == "voice_empty":
            self.cancel_snapshot_session(profile)
            cancel_transport_requests(self)
            state = self._queue_state(profile)
            with self._queue_lock:
                state["generation"] = int(state.get("generation", 0) or 0) + 1
                state["manual_action_serial"] = int(state.get("manual_action_serial", 0)) + 1
                state["source_epoch"] = int(state.get("source_epoch", 0)) + 1
                state["items"] = []
                state["current"] = None
                state["history"] = []
                state["source_mode"] = ""
                state["source_title"] = ""
                state["source_data"] = {}
                state["transport_faulted"] = False
                state["transport_fault_reason"] = ""
            self._host_cache.pop(self._profile_key(profile), None)
            return {"verified": True, "message": "Владелец вышел — NEO отключился и очистил свою очередь." if payload.get("reason") == "owner_left" else "Голосовой канал пуст — NEO вышел и очистил локальную очередь.", "panel_action": action}
        if action == "play":
            query = str(payload.get("query", "") or "").strip()
            requester = str(payload.get("requester", "") or "Discord").strip()
            if not query:
                raise ValueError("После /play укажите название песни и исполнителя или ссылку")
            status = self.music_queue_status(server)
            ready = bool(status.get("connected")) or bool(status.get("playing")) or bool(status.get("paused"))
            if not ready and status.get("shared_state_pending"):
                # NEO 187 (REC-DPN-02): a transitional A→B snapshot is not a
                # disconnected transport. Admit only on a fresh transport sample
                # of this same profile; otherwise say the queue is updating.
                if not bool(status.get("transport_connected")):
                    raise RuntimeError("Очередь Discord сейчас обновляется (идёт переключение трека). Повторите /play через пару секунд.")
                ready = True
            if not ready:
                raise RuntimeError("Сначала владелец NEO должен запустить музыку и подключить бота к голосовому каналу")
            mode = str(payload.get("mode", "last") or "last").strip().casefold()
            if mode not in {"now", "next", "last"}:
                mode = "last"
            # NEO 180: /play next goes right after the current song, /play now replaces it.
            data = self.music_queue_add(query, server=server, requester=requester, next_up=mode in {"now", "next"})
            ack = dict(data or {}) if isinstance(data, dict) else {}
            # NEO 187 (REC-DPN-01): an ACK without the typed start flag is
            # unknown, not "not started" — never skip blindly after it.
            started_known = "queue_started_immediately" in ack or ack.get("queued") is True
            started = bool(ack.get("queue_started_immediately"))
            unknown_start = mode == "now" and not started_known
            queue_changed188 = False
            if mode == "now" and started_known and not started:
                guard_of = getattr(self, "_play_now_guard188", None)
                guard = guard_of(server, ack) if callable(guard_of) else True
                if guard is False:
                    queue_changed188 = True
                else:
                    skipped = self.music_skip(server) if guard is True else self.music_skip(server, expect_front=guard)
                    if skipped.get("cancelled"):
                        return {**skipped, "message": "Предыдущая команда заменена новым запросом.", "panel_action": action}
                    queue_changed188 = bool(skipped.get("queue_changed"))
            item = dict((data or {}).get("item") or {})
            title = str(item.get("title", "") or query).strip()
            artist = str(item.get("artist", "") or "").strip()
            label = title + (f" — {artist}" if artist else "")
            position = 0 if mode == "now" else int((data or {}).get("position", 0) or 0)
            # A human used Discord /play directly. Publish this as a distinct
            # activity event so the owner's desktop assistant and Telegram know
            # who added which song without confusing it with Now Playing.
            try:
                publish_now_playing(
                    "discord", title, artist, str(item.get("track_id", "") or ""),
                    reason="queue_added", playing=False, requester=requester,
                    queue_position=position,
                )
            except Exception:
                logger.debug("Could not publish external Discord queue-add event", exc_info=True)
            if unknown_start:
                message = (f"Поставил первым в очередь: {label}. Подтверждение немедленного запуска не получено — "
                           "NEO не переключал трек повторно.")
            elif queue_changed188:
                message = (f"Поставил первым в очередь: {label}. Пока он готовился, очередь изменилась — "
                           "NEO не переключал трек, чтобы не пропустить чужую песню.")
            elif mode == "now" or started:
                message = f"Включаю сейчас: {label}"
            elif mode == "next":
                message = f"Следующей в очереди: {label}"
            else:
                message = f"Добавлено в очередь: {label}" + (f" · позиция {position}" if position else "")
        elif action == "previous":
            data = self.music_previous(server)
            message = "Переключил на предыдущий трек."
        elif action == "next":
            data = self.music_skip(server)
            message = "Переключил на следующий трек."
        elif action == "toggle":
            status = self.music_queue_status(server)
            if bool(status.get("paused", False)):
                data = self.execute("music_resume", server=server)
                message = "Продолжил воспроизведение."
            else:
                data = self.execute("music_pause", server=server)
                message = "Поставил воспроизведение на паузу."
        elif action == "clear":
            data = self.music_queue_clear(server)
            removed = int((data or {}).get("removed", 0) or 0) if isinstance(data, dict) else 0
            message = f"Очередь очищена. Удалено треков: {removed}." if removed else "Очередь уже пуста."
        elif action == "shuffle":
            status = self.music_queue_status(server)
            enabled = not bool(status.get("shuffle", False))
            data = self.music_shuffle(server, enabled)
            message = "Включил перемешивание." if enabled else "Выключил перемешивание."
        elif action in {"repeat_track", "repeat_playlist"}:
            status = self.music_queue_status(server)
            requested = "track" if action == "repeat_track" else "playlist"
            current_mode = str(status.get("repeat_mode", "off") or "off").casefold()
            target_mode = "off" if current_mode == requested else requested
            data = self.music_repeat(server, target_mode)
            if target_mode == "track":
                message = "Включил повтор текущего трека."
            elif target_mode == "playlist":
                message = "Включил повтор плейлиста."
            else:
                message = "Выключил повтор."
        else:
            raise ValueError(f"Неизвестная команда Discord music panel: {action}")
        if isinstance(data, dict) and data.get("cancelled"):
            return {**data, "verified": False, "message": "Предыдущая команда заменена новым запросом.", "panel_action": action}
        try:
            latest = self.music_queue_status(server)
            # HOTFIX129: transport/button actions must edit the one canonical
            # dashboard in place. Recreate+delete produces visible Discord
            # skeleton/white rows on every track switch. Bottom anchoring remains
            # reserved for actual ordinary chat messages in #neo-music.
            self._background_for_profile(profile).publish_music_state(
                profile, latest, reanchor=False
            )
        except Exception:
            pass
        return {**dict(data or {}), "message": message, "panel_action": action}

    def _bus_bypass(self) -> bool:
        return bool(getattr(self._bus_context, "active", False))

    def _shared_queue_enabled(self, profile: dict[str, Any]) -> bool:
        if self._bus_bypass():
            return False
        # HOTFIX50: no manual Guild/User/Text channel IDs are required. The bot
        # already resolves an available guild/text channel and voice presence.
        # Enabling the control bus is what prevents two PCs from each creating a
        # different personalized queue when one user merely presses Next.
        return bool(self.config.get("integrations.discord.shared_music_queue_enabled", True))

    def _server_key_for_guild(self, guild_id: str, server_name: str = "") -> str:
        """One stable selector for a guild: its ID when configured, else the name.

        A name that belongs to a *different* configured guild is a conflict and
        fails closed instead of acting in someone else's server.
        """
        gid = str(guild_id or "").strip()
        name = str(server_name or "").strip()
        raw = self.config.get("integrations.discord.servers", []) or []
        rows = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        if gid and any(str(item.get("guild_id", "") or "").strip() == gid for item in rows):
            return gid
        if gid and name:
            other = [item for item in rows if str(item.get("name", "") or "").casefold().strip() == name.casefold()
                     and str(item.get("guild_id", "") or "").strip() not in {"", gid}]
            if other:
                raise RuntimeError("Этот сервер Discord не настроен в NEO: его имя совпадает с другим сервером. "
                                   "Добавьте сервер в настройках Discord; NEO ничего не выполнял.")
        return name

    def _profile_for_bus(self, guild_id: str, server_name: str = "") -> dict[str, str]:
        return self._background_profile(self._server_key_for_guild(guild_id, server_name))

    def _bus_ack(self, profile: dict[str, Any], request: dict[str, Any], *, ok: bool, data: Any = None, error: str = "") -> None:
        requester_bot_id = str(request.get("requester_bot_id", "") or "").strip()
        if not requester_bot_id:
            return
        packet = {
            "kind": "ack", "request_id": str(request.get("request_id", "") or ""),
            "sender_instance": self._instance_id,
            "target_instance": str(request.get("sender_instance", "") or ""),
            "guild_id": str(request.get("guild_id", "") or ""),
            "server": str(request.get("server", "") or ""),
            "ok": bool(ok), "data": data if isinstance(data, dict) else {}, "error": str(error or ""),
        }
        try:
            self._background_for_profile(profile).send_control(profile, packet, target_bot_id=requester_bot_id)
        except Exception as exc:
            logger.debug("Discord shared queue ACK failed: %s", exc)

    def _handle_bus_message(self, payload: dict[str, Any]) -> None:
        try:
            if not isinstance(payload, dict):
                return
            kind = str(payload.get("kind", "") or "").strip().casefold()
            sender_instance = str(payload.get("sender_instance", "") or "")
            if kind == "queue_state":
                if sender_instance == self._instance_id:
                    return
                profile = self._profile_for_bus(
                    str(payload.get("guild_id", "") or ""), str(payload.get("server", "") or "")
                )
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                if data:
                    legacy_probe = bool(str(payload.get("_message_author_bot_id", "") or "")) and (
                        str(payload.get("guild_id", "") or "") == str(profile.get("guild_id", "") or ""))
                    data = self._cache_remote_snapshot(profile, dict(data), _legacy_probe=legacy_probe)
                    if not data or not self._remote_snapshot_current(profile, data):
                        return
                    # V5.3: every observer mirrors the authoritative shared state
                    # into the same server dashboard.  This closes the cold-attach
                    # hole where #neo-music existed but stayed empty until this
                    # particular PC issued its own music command.
                    try:
                        self._background_for_profile(profile).publish_music_state(profile, dict(data))
                    except Exception:
                        logger.debug("Discord remote dashboard mirror skipped", exc_info=True)
                return
            if kind == "queue_probe":
                if sender_instance == self._instance_id:
                    return
                profile = self._profile_for_bus(
                    str(payload.get("guild_id", "") or ""), str(payload.get("server", "") or "")
                )
                try:
                    status = self._background_for_profile(profile).playback_status(profile)
                except Exception:
                    status = {}
                if bool(status.get("connected")):
                    # Reply to the author's bot account when possible; with the
                    # same token on two PCs sender_instance still prevents loops.
                    author = str(payload.get("_message_author_bot_id", "") or "").strip()
                    if not author:
                        return
                    admission = self._broadcast_queue_snapshot(
                        profile, target_bot_id=author,
                        request_id=str(payload.get("request_id", "") or ""),
                        target_instance=sender_instance,
                    )
                    if not admission["accepted"]:
                        logger.warning("Discord snapshot request refused: %s; error reply queued=%s",
                                       admission["error"], bool(admission.get("refusal_queued")))
                return
            if kind == "ack":
                if self._receive_snapshot_reply(payload):
                    return
                if str(payload.get("target_instance", "") or "") != self._instance_id:
                    return
                request_id = str(payload.get("request_id", "") or "")
                with self._bus_lock:
                    pending = self._bus_pending.get(request_id)
                    if pending is not None:
                        pending["response"] = dict(payload)
                        pending["event"].set()
                return
            if kind != "request" or sender_instance == self._instance_id:
                return
            profile = self._profile_for_bus(str(payload.get("guild_id", "") or ""), str(payload.get("server", "") or ""))
            action = str(payload.get("action", "") or "").strip().casefold()
            bot = self._background_for_profile(profile)
            try:
                status = bot.playback_status(profile)
            except Exception:
                status = {}
            # Only the process that actually owns a local Discord VoiceClient is
            # allowed to mutate the canonical shared queue. Non-host instances
            # hear the packet too but stay silent.
            if not bool(status.get("connected")):
                return
            if action == "ping":
                self._bus_ack(profile, payload, ok=True, data={
                    "host": True, "server": profile.get("name", ""),
                    "host_instance": self._instance_id,
                })
                return
            self._bus_context.active = True
            try:
                args = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
                if action == "play":
                    data = self.music_bot_play(str(args.get("query", "") or ""), str(profile.get("name", "") or ""))
                elif action == "youtube_url":
                    data = self.music_youtube_url(
                        str(args.get("url", "") or args.get("query", "") or ""),
                        str(profile.get("name", "") or ""),
                        requester=str(args.get("requester", "") or ""),
                        next_up=bool(args.get("next_up", False)),
                        play_now=bool(args.get("play_now", False)),
                    )
                elif action == "queue_add":
                    data = self.music_queue_add(str(args.get("query", "") or ""), str(profile.get("name", "") or ""), next_up=False, requester=str(args.get("requester", "") or ""))
                elif action == "queue_next":
                    data = self.music_queue_add(str(args.get("query", "") or ""), str(profile.get("name", "") or ""), next_up=True, requester=str(args.get("requester", "") or ""))
                elif action == "queue_status":
                    data = self.music_queue_status(str(profile.get("name", "") or ""))
                elif action == "my_wave":
                    data = self.music_my_wave(str(profile.get("name", "") or ""), requester=str(args.get("requester", "") or ""))
                elif action == "my_tracks":
                    data = self.music_my_tracks(str(profile.get("name", "") or ""), requester=str(args.get("requester", "") or ""))
                elif action == "playlist":
                    data = self.music_playlist(str(args.get("playlist", "") or args.get("query", "") or ""), str(profile.get("name", "") or ""), requester=str(args.get("requester", "") or ""))
                elif action == "queue_clear":
                    data = self.music_queue_clear(str(profile.get("name", "") or ""))
                elif action == "skip":
                    data = self.music_skip(str(profile.get("name", "") or ""))
                elif action == "previous":
                    data = self.music_previous(str(profile.get("name", "") or ""))
                elif action == "shuffle_on":
                    data = self.music_shuffle(str(profile.get("name", "") or ""), True)
                elif action == "shuffle_off":
                    data = self.music_shuffle(str(profile.get("name", "") or ""), False)
                elif action == "repeat_track":
                    data = self.music_repeat(str(profile.get("name", "") or ""), "track")
                elif action == "repeat_playlist":
                    data = self.music_repeat(str(profile.get("name", "") or ""), "playlist")
                elif action == "repeat_off":
                    data = self.music_repeat(str(profile.get("name", "") or ""), "off")
                elif action in {"pause", "resume", "stop"}:
                    data = self.execute("music_" + action, server=str(profile.get("name", "") or ""))
                elif action == "yield":
                    data = self._yield_local(profile)
                else:
                    raise ValueError(f"Неизвестное действие общей очереди: {action}")
                self._bus_ack(profile, payload, ok=True, data=data)
            except Exception as exc:
                self._bus_ack(profile, payload, ok=False, error=str(exc))
            finally:
                self._bus_context.active = False
        except Exception as exc:
            logger.debug("Discord shared queue packet processing failed: %s", exc)

    def _bus_request(
        self, profile: dict[str, Any], host_bot_id: str, action: str, payload: dict[str, Any] | None = None, *, timeout: float = 3.0, requester_bot_id: str = "",
    ) -> dict[str, Any]:
        bot = self._background_for_profile(profile)
        if not requester_bot_id:
            presence = bot.voice_presence(profile)
            requester_bot_id = str(presence.get("self_bot_id", "") or "")
        request_id = uuid.uuid4().hex
        event = threading.Event()
        with self._bus_lock:
            self._bus_pending[request_id] = {"event": event, "response": None}
        packet = {
            "kind": "request", "request_id": request_id, "sender_instance": self._instance_id,
            "requester_bot_id": requester_bot_id, "guild_id": str(profile.get("guild_id", "") or ""),
            "server": str(profile.get("name", "") or ""), "action": str(action), "payload": dict(payload or {}),
        }
        try:
            bot.send_control(profile, packet, target_bot_id=str(host_bot_id))
            if not event.wait(max(0.35, float(timeout))):
                raise TimeoutError("Другой NEO не подтвердил команду общей Discord-очереди")
            with self._bus_lock:
                response = dict((self._bus_pending.get(request_id) or {}).get("response") or {})
            if not bool(response.get("ok")):
                raise RuntimeError(str(response.get("error", "") or "Удалённый NEO не выполнил команду"))
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            return {**data, "shared_queue": True, "shared_host_bot_id": str(host_bot_id)}
        finally:
            with self._bus_lock:
                self._bus_pending.pop(request_id, None)

    def _remote_host(self, profile: dict[str, Any]) -> dict[str, str] | None:
        if not self._shared_queue_enabled(profile):
            return None
        key = self._profile_key(profile)
        now = time.monotonic()
        cached = self._host_cache.get(key)
        if cached and cached[0] > now:
            return dict(cached[1])
        bot = self._background_for_profile(profile)
        presence = bot.voice_presence(profile)
        local_connected = bool(presence.get("local_connected"))
        self_bot_id = str(presence.get("self_bot_id", "") or "")
        candidates = list(presence.get("bot_candidates") or [])
        configured_name = str(profile.get("jarvis_bot_name", "") or "").strip().casefold()
        likely: list[dict[str, Any]] = []
        for candidate in candidates:
            cid = str(candidate.get("id", "") or "").strip()
            cname = str(candidate.get("name", "") or "").strip()
            cname_key = cname.casefold()
            is_jarvis = (
                bool(re.search(r"(?:jarvis|neo\s*assistant|джарвис)", cname, re.I))
                or (configured_name and cname_key == configured_name)
            )
            # If this process is not connected, our own account ID may be the
            # remote host when two PCs intentionally use the same BOT token.
            same_account_remote = bool(not local_connected and self_bot_id and cid == self_bot_id)
            # If we are already connected, never ping our own account: a single
            # installation would otherwise wait for an ACK from itself on every
            # command. Different NEO bot accounts are probed for deterministic
            # host election after an accidental simultaneous start.
            if same_account_remote or (is_jarvis and (not local_connected or cid != self_bot_id)):
                likely.append(candidate)
        likely.sort(key=lambda item: (str(item.get("id", "") or ""), str(item.get("name", "") or "")))

        for candidate in likely[:4]:
            host_id = str(candidate.get("id", "") or "").strip()
            if not host_id:
                continue
            try:
                reply = self._bus_request(profile, host_id, "ping", timeout=0.9, requester_bot_id=self_bot_id)
                if not reply.get("host"):
                    continue
                info = {
                    "bot_id": host_id,
                    "requester_bot_id": self_bot_id,
                    "host_instance": str(reply.get("host_instance", "") or ""),
                }
                if local_connected:
                    # Two different NEO bot accounts can race into the same
                    # voice channel. Elect the smaller bot ID as canonical host;
                    # the losing process yields its VoiceClient immediately.
                    try:
                        remote_wins = int(host_id) < int(self_bot_id or "0")
                    except Exception:
                        remote_wins = host_id < self_bot_id
                    if remote_wins:
                        logger.info(
                            "Discord shared queue host election: yielding local bot %s to remote bot %s",
                            self_bot_id, host_id,
                        )
                        self._yield_local(profile)
                        self._host_cache[key] = (now + 8.0, info)
                        return info
                    continue
                self._host_cache[key] = (now + 8.0, info)
                return info
            except Exception:
                continue
        if local_connected:
            self._host_cache.pop(key, None)
        return None

    def _yield_local(self, profile: dict[str, Any]) -> dict[str, Any]:
        self.cancel_snapshot_session(profile)
        state = self._queue_state(profile)
        with self._queue_lock:
            state["items"] = []
            state["current"] = None
            state["source_mode"] = ""
            state["generation"] = int(state.get("generation", 0)) + 1
        bot = self._background_for_profile(profile)
        try:
            bot.voice_action(profile, "stop")
        except Exception:
            pass
        try:
            bot.voice_action(profile, "leave")
        except Exception:
            pass
        self._host_cache.pop(self._profile_key(profile), None)
        return {"server": profile.get("name", ""), "yielded": True, "verified": True, "shared_queue": True}

    def background_status(self, server: str = "") -> dict[str, Any]:
        profile = self._background_profile(server)
        return {
            **self._background_for_profile(profile).available(),
            "server": profile.get("name", ""),
            "per_server_token": bool(profile.get("bot_token_encrypted")),
            "never_steal_input": self._never_steal_input(),
            "ui_fallback": self._ui_fallback(),
        }

    def send_message(self, text: str, channel: str = "", server: str = "") -> dict[str, Any]:
        if not self._background_enabled():
            raise RuntimeError("Официальный Discord Bot отключён в настройках; desktop/UI fallback удалён.")
        profile = self._background_profile(server)
        try:
            return self._background_for_profile(profile).send_message(profile, text, channel=channel)
        except Exception as exc:
            logger.warning("Discord official-bot message path failed: %s", exc)
            raise RuntimeError(
                f"Не удалось отправить сообщение через официальный NEO Bot: {exc}. "
                "Desktop Discord не фокусировался и клавиши не отправлялись."
            ) from exc

    def _supersede_music_request(self) -> None:
        # Invalidate queue-monitor preparation as well as the previous manual
        # request, before waiting for the transition lock.
        with self._queue_lock:
            for state in self._queues.values():
                state["manual_action_serial"] = int(state.get("manual_action_serial", 0)) + 1

    def _play_query(self, query: str, profile: dict[str, Any], channel: str = "") -> dict[str, Any]:
        check_request = transport_guard(self)
        check_request()
        streams = self._yandex.resolve_playback_candidates(query, cache_first=True)
        transport_checkpoint(self)
        if not streams:
            raise RuntimeError(
                f"Не удалось получить аудио для «{query}» из Яндекс Музыки. "
                "OAuth может быть исправен; повторите команду или уточните название/исполнителя."
            )
        bot = self._background_for_profile(profile)
        requested_channel = str(channel or "").strip()
        last_error: Exception | None = None
        for stream in streams[:4]:
            try:
                data = bot.play_url(
                    profile, str(stream.get("source") or stream["url"]),
                    title=str(stream.get("title", "") or query),
                    artist=str(stream.get("artist", "") or ""),
                    channel=requested_channel,
                    before_start=check_request,
                )
                transport_checkpoint(self)
                result = {
                    **data, "query": query, "track_id": str(stream.get("id", "") or ""),
                    "title": str(stream.get("title", "") or query),
                    "artist": str(stream.get("artist", "") or ""),
                    "codec": stream.get("codec"), "bitrate_kbps": stream.get("bitrate_kbps"),
                    "source_method": stream.get("source_method", "direct_stream"),
                    "api_used": True, "mode": "jarvis_bot",
                }
                result["media_event_id"] = publish_now_playing(
                    "discord", result["title"], result.get("artist", ""), result.get("track_id", ""),
                    duration_ms=int(stream.get("expected_duration_ms", 0) or 0),
                    playback_generation=int(data.get("playback_generation", 0) or 0),
                    playback_scope=self._media_playback_scope(profile),
                )
                self._broadcast_queue_snapshot(profile, delay=0.08)
                return result
            except DiscordPlaybackCancelled:
                raise
            except Exception as stream_exc:
                last_error = stream_exc
                text = str(stream_exc).casefold()
                if any(key in text for key in (
                    "голосов", "voice channel", "guild", "сервер", "connect", "speak",
                    "permission", "gateway", "token",
                )):
                    raise
                logger.warning(
                    "Discord stream candidate failed codec=%s bitrate=%s: %s",
                    stream.get("codec"), stream.get("bitrate_kbps"), stream_exc,
                )
        raise RuntimeError(f"Discord не смог воспроизвести доступные потоки Яндекс Музыки: {last_error or 'unknown error'}")

    def _queue_state(self, profile: dict[str, Any]) -> dict[str, Any]:
        key = self._profile_key(profile)
        with self._queue_lock:
            state = self._queues.get(key)
            if state is None:
                store = self._session_stores.setdefault(key, PlaybackSessionStore(self.config, "discord:" + key))
                restored = store.load()
                state = {
                    "profile": dict(profile), "items": list(restored.get("upcoming") or [])[:5], "current": None,
                    "monitor": None, "generation": 0, "history": list(restored.get("previous") or [])[-5:],
                    "source_mode": str(restored.get("source_mode", "")), "source_title": "", "source_data": {},
                    "restored_queue": bool(restored.get("upcoming")), "source_epoch": 0,
                    "shuffle": bool(self.config.get("integrations.discord.music_shuffle_enabled", False)),
                    "repeat_mode": str(self.config.get("integrations.discord.music_repeat_mode", "off") or "off").casefold(),
                    # Transport faults must stop auto-advance. Otherwise a dead
                    # FFmpeg/Discord transport can consume an entire playlist
                    # while announcing phantom track changes every few seconds.
                    "transport_faulted": False, "transport_fault_reason": "",
                }
                self._queues[key] = state
            else:
                state["profile"] = dict(profile)
                state.setdefault("source_mode", "")
                state.setdefault("source_title", "")
                state.setdefault("source_data", {})
                state.setdefault("history", [])
                state.setdefault("shuffle", bool(self.config.get("integrations.discord.music_shuffle_enabled", False)))
                state.setdefault("repeat_mode", str(self.config.get("integrations.discord.music_repeat_mode", "off") or "off").casefold())
                if str(state.get("repeat_mode", "off") or "off") not in {"off", "track", "playlist"}:
                    state["repeat_mode"] = "off"
                state.setdefault("transport_faulted", False)
                state.setdefault("transport_fault_reason", "")
            return state

    @staticmethod
    def _stable_entry(entry: dict[str, Any] | None) -> dict[str, Any]:
        raw = dict(entry or {})
        return {k: raw[k] for k in ("query", "track_id", "title", "artist", "added_by", "media_source",
                                   "_source_type", "_source_url", "_expected_duration_ms", "played_at")
                if k in raw and isinstance(raw[k], (str, int, float, bool))}

    def _persist_queue(self, profile: dict[str, Any]) -> None:
        try:
            key = self._profile_key(profile)
            with self._queue_lock:
                state = self._queues.get(key)
                if not state:
                    return
                current = state.get("current") or {}
                if current.get("starting"):
                    return
                payload = {"previous": [self._stable_entry(x) for x in state.get("history", [])[-5:]],
                           "current": self._stable_entry(current),
                           "upcoming": [self._stable_entry(x) for x in state.get("items", [])[:5]],
                           "source_mode": str(state.get("source_mode", ""))}
                store = self._session_stores.setdefault(key, PlaybackSessionStore(self.config, "discord:" + key))
                paths: set[str] = set()
                for entry in [current, *state.get("history", [])[-5:], *state.get("items", [])[:5]]:
                    paths.update(self._entry_cached_paths(entry))
                pin_cache(f"discord:{id(self)}:{key}", paths)
            store.save(payload)
        except Exception:
            logger.warning("Could not save Discord playback history", exc_info=True)

    @staticmethod
    def _entry_cached_paths(entry: dict[str, Any] | None) -> set[str]:
        paths: set[str] = set()
        if not isinstance(entry, dict):
            return paths
        youtube_path = str(entry.get("_youtube_cached_path", "") or "").strip()
        if youtube_path:
            paths.add(youtube_path)
        for raw in list(entry.get("_prepared_streams") or []):
            if not isinstance(raw, dict):
                continue
            method = str(raw.get("source_method", "") or "").casefold()
            source = str(raw.get("source") or raw.get("url") or "").strip()
            if source and method.startswith("cached_local_file"):
                paths.add(source)
        return paths

    @staticmethod
    def _delete_history_cache_file(path: str) -> None:
        try:
            if path and os.path.isfile(path) and not cache_is_pinned(path):
                os.remove(path)
                logger.debug("Released Discord history cache file: %s", path)
        except PermissionError:
            # A late FFmpeg handle can outlive its history entry by a fraction of
            # a second. Startup cleanup remains the final bounded fallback.
            logger.debug("Discord history cache file still in use: %s", path)
        except Exception:
            logger.debug("Could not release Discord history cache file: %s", path, exc_info=True)

    def _release_dropped_history_cache(self, state: dict[str, Any], dropped: list[dict[str, Any]]) -> None:
        if not dropped:
            return
        referenced: set[str] = set()
        referenced.update(self._entry_cached_paths(state.get("current") if isinstance(state.get("current"), dict) else {}))
        for item in list(state.get("items") or []):
            referenced.update(self._entry_cached_paths(item if isinstance(item, dict) else {}))
        for item in list(state.get("history") or []):
            referenced.update(self._entry_cached_paths(item if isinstance(item, dict) else {}))
        stale: set[str] = set()
        for item in dropped:
            stale.update(self._entry_cached_paths(item))
        for path in sorted(stale - referenced):
            threading.Thread(
                target=self._delete_history_cache_file, args=(path,),
                name="jarvis-discord-history-cache-prune", daemon=True,
            ).start()

    def _push_history(self, state: dict[str, Any], entry: dict[str, Any] | None) -> None:
        """Keep five previous tracks *and their prepared local audio*.

        Previous therefore starts from the same cached source instead of doing a
        fresh Yandex lookup/download. When a sixth history entry arrives, only the
        oldest unreferenced cache is released.
        """
        if not entry or bool(entry.get("starting")):
            return
        clean = dict(entry)
        clean.setdefault("played_at", time.time())
        history = state.setdefault("history", [])
        if history:
            a = str(history[-1].get("track_id", "") or history[-1].get("query", ""))
            b = str(clean.get("track_id", "") or clean.get("query", ""))
            if a and a == b:
                history[-1] = clean
                dropped = [dict(x) for x in history[:-5] if isinstance(x, dict)]
                del history[:-5]
                self._release_dropped_history_cache(state, dropped)
                return
        history.append(clean)
        dropped = [dict(x) for x in history[:-5] if isinstance(x, dict)]
        del history[:-5]
        self._release_dropped_history_cache(state, dropped)

    @staticmethod
    def _is_transport_failure(exc: BaseException | str) -> bool:
        if isinstance(exc, TimeoutError):
            return True
        text = str(exc or "").casefold()
        return any(token in text for token in (
            "ffmpeg", "audio player", "voice transport", "transport не",
            "voice handshake", "voice connect", "голосов", "не подключён",
            "winerror", "opus", "pipe_reader", "pipe reader",
            "завершил воспроизведение сразу", "discord voice подключён",
        ))

    @staticmethod
    def _looks_internal_media_value(value: str) -> bool:
        raw = str(value or "").strip()
        if not raw:
            return True
        low = raw.casefold()
        if low in {"трек", "track", "unknown", "неизвестно", "youtube video", "метаданные загружаются"}:
            return True
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", low):
            return True
        if re.fullmatch(r"[0-9a-f]{24,64}", low):
            return True
        # Yandex TrackShort objects can expose only their numeric track id until
        # materialized. Never render that implementation id as a song title.
        if re.fullmatch(r"\d{5,}", low):
            return True
        if re.match(r"^https?://", low):
            return True
        return False

    @classmethod
    def _prefer_media_title(cls, *values: Any) -> str:
        for value in values:
            raw = str(value or "").strip()
            if raw and not cls._looks_internal_media_value(raw):
                return raw
        return ""

    @classmethod
    def _queue_title_is_placeholder(cls, entry: dict[str, Any] | None, title: str = "") -> bool:
        """Detect TrackShort/internal IDs without rejecting legitimate numeric songs.

        Yandex sometimes exposes a short object's numeric id as ``title`` before
        materialization (for example ``6124``). Numeric-only real song titles do
        exist, so treat a numeric title as internal only when it equals this queue
        entry's track/query id.
        """
        raw = dict(entry or {})
        value = str(title or raw.get("title", "") or "").strip()
        if not value:
            return True
        if cls._looks_internal_media_value(value):
            return True
        if value.isdigit():
            identifiers = {
                str(raw.get("track_id", "") or "").strip(),
                str(raw.get("id", "") or "").strip(),
            }
            identifiers.discard("")
            if value in identifiers:
                return True
            # In-memory TrackShort placeholders can temporarily expose the same
            # numeric id as both title and query before ``track_id`` is copied.
            if raw.get("_track_obj") is not None and value == str(raw.get("query", "") or "").strip():
                return True
        return False

    @classmethod
    def _public_queue_entry(cls, entry: dict[str, Any] | None) -> dict[str, Any]:
        public = {k: v for k, v in dict(entry or {}).items() if not str(k).startswith("_")}
        title = cls._prefer_media_title(public.get("title", ""), public.get("query", ""))
        if title and cls._queue_title_is_placeholder(public, title):
            title = ""
        if title:
            public["title"] = title
        elif public.get("media_source"):
            public["title"] = f"{public.get('media_source')} — метаданные загружаются"
        else:
            public["title"] = "Метаданные загружаются"
        # Raw UUIDs/URLs are useful internally but make the Telegram queue almost
        # unreadable. Keep them in adapter state, not in user-facing snapshots.
        if cls._looks_internal_media_value(str(public.get("query", "") or "")):
            public.pop("query", None)
        return public

    @staticmethod
    def _shuffle_entry_token(item: dict[str, Any] | None) -> str:
        raw = dict(item or {})
        return str(raw.get("track_id") or raw.get("id") or raw.get("query") or raw.get("title") or "").strip().casefold()

    @classmethod
    def _shuffle_pending_items(cls, state: dict[str, Any]) -> bool:
        """Randomize the pending Discord queue as a no-repeat shuffle bag.

        Explicit "play next" entries stay at the front and keep their relative
        order; every ordinary pending track is Fisher-Yates shuffled in place.
        HOTFIX40.2 additionally prevents the first ordinary item from remaining
        identical to the old sequential next item when alternatives exist. That
        makes a just-enabled shuffle audibly obvious on the very next transition.
        """
        items = list(state.get("items") or [])
        if len(items) < 2:
            return False
        forced = [item for item in items if isinstance(item, dict) and bool(item.get("_force_next"))]
        ordinary = [item for item in items if not (isinstance(item, dict) and bool(item.get("_force_next")))]
        randomized = len(ordinary) > 1
        if randomized:
            old_first = cls._shuffle_entry_token(ordinary[0])
            rng = random.SystemRandom()
            rng.shuffle(ordinary)
            if old_first and cls._shuffle_entry_token(ordinary[0]) == old_first and len(ordinary) > 1:
                swap_index = rng.randrange(1, len(ordinary))
                ordinary[0], ordinary[swap_index] = ordinary[swap_index], ordinary[0]
        state["items"] = forced + ordinary
        return randomized

    @classmethod
    def _pop_next_queue_entry(
        cls, state: dict[str, Any], *, removed: list[tuple[int, dict[str, Any]]] | None = None,
    ) -> dict[str, Any] | None:
        items = state.get("items") or []
        if not items:
            return None
        # Explicit play-next remains authoritative even with shuffle enabled.
        for index, item in enumerate(items):
            if isinstance(item, dict) and bool(item.get("_force_next")):
                item = items.pop(index)
                if removed is not None:
                    removed.append((index, item))
                chosen = dict(item)
                chosen.pop("_force_next", None)
                return chosen

        # My Wave must never replay the current/recent recommendation merely
        # because Rotor returned overlapping batches. Consume duplicate IDs or
        # identical title+artist fingerprints before selecting the next item.
        if str(state.get("source_mode", "") or "") == "wave":
            history = list((state.get("source_data") or {}).get("recent_ids", []))
            if not state.get("wave_strict_repeat", True):
                history = history[-1:]
            recent_ids = {str(x or "").strip() for x in history if str(x or "").strip()}
            current = dict(state.get("current") or {})
            current_fp = "|".join((str(current.get("title", "") or "").casefold().strip(), str(current.get("artist", "") or "").casefold().strip())).strip("|")
            while items:
                item = items.pop(0)
                if removed is not None:
                    removed.append((len(removed), item))
                raw = dict(item)
                raw.pop("_force_next", None)
                tid = str(raw.get("track_id", "") or raw.get("id", "") or "").strip()
                fp = "|".join((str(raw.get("title", "") or "").casefold().strip(), str(raw.get("artist", "") or "").casefold().strip())).strip("|")
                if tid and tid in recent_ids:
                    logger.debug("Discord My Wave dropped repeated track id=%s", tid)
                    continue
                if fp and current_fp and fp == current_fp:
                    logger.debug("Discord My Wave dropped repeated recording %s", fp)
                    continue
                return raw
            return None

        item = items.pop(0)
        if removed is not None:
            removed.append((0, item))
        chosen = dict(item)
        chosen.pop("_force_next", None)
        return chosen

    def _queue_entry(self, query: str, *, requester: str = "") -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("Не указана песня для очереди Discord")
        try:
            resolved = self._yandex.resolve_track(query) or {}
        except Exception as exc:
            logger.debug("Discord queue metadata lookup failed for %r: %s", query, exc)
            resolved = {}
        track_obj = resolved.get("object")
        return {
            "query": query,
            "track_id": str(resolved.get("id", "") or ""),
            "title": str(resolved.get("title", "") or query),
            "artist": str(resolved.get("artist", "") or ""),
            "added_by": str(requester or "").strip(),
            # Private in-memory values; stripped from user-visible queue/status.
            "_track_obj": track_obj,
            "_expected_duration_ms": int(getattr(track_obj, "duration_ms", 0) or resolved.get("duration_ms", 0) or 0),
            "_retry_count": 0,
        }

    @staticmethod
    def _youtube_queue_entry(item: dict[str, Any], *, requester: str = "") -> dict[str, Any]:
        url = str(item.get("webpage_url", "") or "").strip()
        title = str(item.get("title", "") or "YouTube video").strip()
        artist = str(item.get("artist", "") or "YouTube").strip()
        return {
            "query": url or title,
            "track_id": str(item.get("video_id", "") or "").strip(),
            "title": title,
            "artist": artist,
            "added_by": str(requester or "").strip(),
            "media_source": "YouTube",
            "_source_type": "youtube",
            "_source_url": url,
            "_expected_duration_ms": int(item.get("duration_ms", 0) or 0),
            "_retry_count": 0,
        }

    def _ordered_profiles_for_text_media(self, server: str = "") -> list[dict[str, str]]:
        profiles = self._candidate_profiles(server)
        if str(server or "").strip() or len(profiles) <= 1:
            return profiles
        # Prefer the local profile that already owns the mixed Discord queue.
        active: list[dict[str, str]] = []
        rest: list[dict[str, str]] = []
        with self._queue_lock:
            for profile in profiles:
                state = self._queues.get(self._profile_key(profile)) or {}
                (active if state.get("current") else rest).append(profile)
        if active:
            return active + rest
        # After a restart the voice client can still be connected before an
        # in-memory queue item exists. Prefer that connected profile next.
        connected: list[dict[str, str]] = []
        tail: list[dict[str, str]] = []
        for profile in profiles:
            try:
                status = self._background_for_profile(profile).playback_status(profile)
                (connected if status.get("connected") else tail).append(profile)
            except Exception:
                tail.append(profile)
        return connected + tail

    def music_youtube_url(self, url: str, server: str = "", *, requester: str = "", next_up: bool = False, play_now: bool = False) -> dict[str, Any]:
        method = self._music_youtube_url_now if play_now else self._music_youtube_url_impl
        return method(url, server, requester=requester, next_up=next_up, play_now=play_now)

    @latest_transport
    def _music_youtube_url_now(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._music_youtube_url_impl(*args, **kwargs)

    def _music_youtube_url_impl(
        self, url: str, server: str = "", *, requester: str = "", next_up: bool = False,
        play_now: bool = False,
    ) -> dict[str, Any]:
        """Play or queue an explicit YouTube video/playlist URL in Discord.

        HOTFIX75 separates ``play now`` from ``add to queue``. URL routing is
        performed before the LLM, so a YouTube URL can never fall through to the
        Yandex catalog resolver merely because the surrounding command is Russian.
        """
        clean_url = standalone_youtube_url(str(url or ""))
        if not clean_url:
            raise ValueError("Нужна отдельная ссылка YouTube на видео или плейлист")
        if not self._background_enabled():
            raise RuntimeError("Официальный Discord Bot отключён в настройках")

        remote = self._remote_for_any_profile(server)
        if remote:
            profile, host = remote
            who = requester or str(profile.get("discord_user_id", "") or "")
            return self._remote_request(
                profile, host, "youtube_url",
                {"url": clean_url, "requester": who, "next_up": bool(next_up), "play_now": bool(play_now)},
                timeout=45.0,
            )

        limit = max(1, min(500, int(self.config.get("integrations.discord.youtube_playlist_limit", 200) or 200)))
        # HOTFIX117: a normal watch/short/live/youtu.be URL must be handed to
        # Discord Music as-is.  Do not perform an upfront YouTube metadata/API
        # lookup: it is unrelated to the optional YouTube integration and, in
        # field logs, YouTube's anti-bot page rejected that preflight before the
        # Discord bot ever received the URL.  A real /playlist URL still needs
        # expansion because it represents multiple queue entries.
        if explicit_playlist_url(clean_url):
            expanded = self._youtube.expand(clean_url, limit=limit)
        else:
            expanded = {
                "kind": "video",
                "title": "YouTube",
                "items": [{
                    "webpage_url": clean_url, "video_id": "",
                    "title": "YouTube", "artist": "YouTube", "duration_ms": 0,
                }],
                "truncated": False,
            }
        transport_checkpoint(self)
        raw_items = [item for item in list(expanded.get("items") or []) if isinstance(item, dict)]
        if not raw_items:
            raise RuntimeError("YouTube-ссылка не вернула доступных элементов")

        explicit_server = bool(str(server or "").strip())
        last_error: Exception | None = None
        for profile in self._ordered_profiles_for_text_media(server):
            who = requester or str(profile.get("discord_user_id", "") or "")
            entries = [self._youtube_queue_entry(item, requester=who) for item in raw_items]
            state = self._queue_state(profile)
            key = self._profile_key(profile)
            with self._queue_lock:
                current = dict(state.get("current") or {})
                pending = list(state.get("items") or [])

            # Explicit "включи ... [в Discord]" is play-now, exactly like an
            # ordinary named Discord song. Keep a finite manual queue behind it,
            # but drop generated My Wave remainder so the requested URL is heard
            # immediately. A playlist contributes its remaining videos before the
            # already-manual pending queue.
            if play_now:
                first = dict(entries[0])
                rest = [dict(item) for item in entries[1:]]
                with self._queue_lock:
                    state = self._queues.get(key) or state
                    old_current = dict(state.get("current") or {})
                    old_pending = list(state.get("items") or [])
                    if state.get("restored_queue") or str(state.get("source_mode", "") or "") in {"wave", "my_tracks", "playlist"}:
                        old_pending = []
                    state["source_epoch"] = int(state.get("source_epoch",0)) + 1
                    state["restored_queue"] = False
                    state["wave_background_refill_active"] = False
                    self._push_history(state, old_current)
                    state["items"] = rest + old_pending
                    state["source_mode"] = ""
                    state["source_title"] = ""
                    state["source_data"] = {}
                    state["transport_faulted"] = False
                    state["transport_fault_reason"] = ""
                    state["generation"] = int(state.get("generation", 0)) + 1
                    generation = int(state["generation"])
                    state["current"] = {**first, "generation": generation, "started_at": time.monotonic(), "seen_active": False, "starting": True}
                try:
                    data = self._play_queue_entry(first, profile, fast_transition=bool(old_current))
                except Exception:
                    with self._queue_lock:
                        live = self._queues.get(key) or state
                        if int(((live.get("current") or {}).get("generation", -1))) == generation:
                            live["current"] = old_current or None
                            live["items"] = old_pending
                    raise
                with self._queue_lock:
                    live = self._queues.get(key) or state
                    if int(((live.get("current") or {}).get("generation", -1))) == generation:
                        live["current"] = {
                            **first,
                            "track_id": str(data.get("track_id", "") or first.get("track_id", "")),
                            "title": self._prefer_media_title(data.get("title", ""), first.get("title", ""), first.get("query", "")),
                            "artist": str(data.get("artist", "") or first.get("artist", "")),
                            "generation": generation, "started_at": time.monotonic(), "seen_active": False,
                            "_transport_generation": int(data.get("playback_generation", 0) or 0),
                            "_expected_duration_ms": int(data.get("expected_duration_ms", 0) or first.get("_expected_duration_ms", 0) or 0),
                            "_retry_count": 0,
                        }
                self._prefetch_next_queue_item(live)
                self._start_queue_monitor(profile)
                return {
                    **data,
                    "kind": str(expanded.get("kind", "video") or "video"),
                    "playlist_title": str(expanded.get("title", "") or ""),
                    "added_count": len(entries), "queued_count": len(state.get("items") or []),
                    "queued": bool(state.get("items")), "queue_started_immediately": True,
                    "play_now": True, "truncated": bool(expanded.get("truncated")),
                    "shared_queue": bool(self.config.get("integrations.discord.shared_music_queue_enabled", True)),
                }

            # If anything is already playing/queued, YouTube becomes an ordinary
            # mixed queue item alongside Yandex tracks. During My Wave it is a
            # priority request: play it next, then continue the same infinite wave.
            if current or pending:
                with self._queue_lock:
                    state = self._queues.get(key) or state
                    items = state.setdefault("items", [])
                    wave_active = str(state.get("source_mode", "") or "") == "wave"
                    if next_up:
                        staged = [dict(entry, _force_next=True) for entry in entries]
                        state["items"] = staged + list(items)
                        position = 1
                    elif wave_active:
                        # Preserve FIFO between ordinary /play requests while still
                        # keeping all of them ahead of Rotor-generated Wave items.
                        staged = [dict(entry, _force_next=True) for entry in entries]
                        insert_at = 0
                        while insert_at < len(items) and bool(items[insert_at].get("_force_next")):
                            insert_at += 1
                        state["items"] = list(items[:insert_at]) + staged + list(items[insert_at:])
                        position = insert_at + 1
                    else:
                        start_len = len(items)
                        items.extend(entries)
                        if bool(state.get("shuffle")):
                            self._shuffle_pending_items(state)
                            items = state.setdefault("items", [])
                            # Position is informational after shuffle; the mixed
                            # queue itself remains authoritative.
                            position = max(1, start_len + 1)
                        else:
                            position = start_len + 1
                    snapshot = [self._public_queue_entry(item) for item in state.get("items", [])]
                self._start_queue_monitor(profile)
                if not current:
                    self._advance_queue(key)
                return {
                    "server": profile.get("name", ""),
                    "kind": str(expanded.get("kind", "video") or "video"),
                    "playlist_title": str(expanded.get("title", "") or ""),
                    "added_count": len(entries), "queued": True, "position": position,
                    "title": str(entries[0].get("title", "") or ""),
                    "artist": str(entries[0].get("artist", "") or ""),
                    "queue_started_immediately": False, "queue": snapshot,
                    "truncated": bool(expanded.get("truncated")),
                    "verified": True, "submitted": True,
                    "method": "discord_youtube_mixed_queue", "non_invasive": True,
                    "shared_queue": bool(self.config.get("integrations.discord.shared_music_queue_enabled", True)),
                }

            # Empty queue: start the first YouTube item immediately and keep the
            # rest as ordinary mixed queue entries. Try other configured Discord
            # profiles only for the same retryable active-voice errors as music.
            first = dict(entries[0])
            rest = [dict(item) for item in entries[1:]]
            with self._queue_lock:
                state["transport_faulted"] = False
                state["transport_fault_reason"] = ""
                state["items"] = rest
                state["source_mode"] = ""
                state["source_title"] = ""
                state["source_data"] = {}
                state["generation"] = int(state.get("generation", 0)) + 1
                generation = int(state["generation"])
                state["current"] = {**first, "generation": generation, "started_at": time.monotonic(), "seen_active": False, "starting": True}
            try:
                data = self._play_queue_entry(first, profile)
            except Exception as exc:
                with self._queue_lock:
                    live = self._queues.get(key) or state
                    if int(((live.get("current") or {}).get("generation", -1))) == generation:
                        live["current"] = None
                        live["items"] = []
                last_error = exc
                if explicit_server or not self._profile_retryable_error(exc):
                    raise
                logger.info("YouTube Discord profile %r unavailable; trying next profile: %s", profile.get("name", ""), exc)
                continue

            with self._queue_lock:
                live = self._queues.get(key) or state
                if int(((live.get("current") or {}).get("generation", -1))) == generation:
                    live["current"] = {
                        **first,
                        "track_id": str(data.get("track_id", "") or first.get("track_id", "")),
                        "title": str(data.get("title", "") or first.get("title", "")),
                        "artist": str(data.get("artist", "") or first.get("artist", "")),
                        "generation": generation, "started_at": time.monotonic(), "seen_active": False,
                        "_transport_generation": int(data.get("playback_generation", 0) or 0),
                        "_expected_duration_ms": int(data.get("expected_duration_ms", 0) or first.get("_expected_duration_ms", 0) or 0),
                        "_retry_count": 0,
                    }
            self._prefetch_next_queue_item(live)
            self._start_queue_monitor(profile)
            return {
                **data,
                "kind": str(expanded.get("kind", "video") or "video"),
                "playlist_title": str(expanded.get("title", "") or ""),
                "added_count": len(entries), "queued_count": len(rest),
                "queued": bool(rest), "queue_started_immediately": True,
                "truncated": bool(expanded.get("truncated")),
                "shared_queue": bool(self.config.get("integrations.discord.shared_music_queue_enabled", True)),
            }

        raise last_error or RuntimeError("Не найден доступный Discord-сервер для YouTube audio")

    def _remember_wave_id(self, track_id: str, *, title: str = "", artist: str = "") -> None:
        """Share Discord My Wave history with durable ID + recording identity."""
        tid = str(track_id or "").strip()
        if not tid and not str(title or "").strip():
            return
        try:
            remember_track = getattr(self._yandex, "remember_wave_track", None)
            if callable(remember_track) and (title or artist):
                remember_track({"track_id": tid, "title": str(title or ""), "artist": str(artist or "")})
                return
            remember = getattr(self._yandex, "remember_wave_track_id", None)
            if callable(remember) and tid:
                remember(tid)
        except Exception as exc:
            logger.debug("Could not persist Discord My Wave track %s: %s", tid, exc)

    def _wave_avoid_history(self, recent: list[str]) -> list[str]:
        strict = getattr(self._yandex, "wave_strict_repeat_policy", lambda: True)()
        return list(recent if strict else recent[-1:])

    @staticmethod
    def _wave_pause_clock(state: dict[str, Any], *, paused: bool) -> None:
        current = state.get("current") or {}
        if not current:
            return
        now = time.monotonic()
        if paused and not current.get("_pause_started_at"):
            current["_pause_started_at"] = now
        elif not paused and current.get("_pause_started_at"):
            current["_paused_seconds"] = float(current.get("_paused_seconds") or 0) + max(0.0, now-float(current.pop("_pause_started_at")))

    def _wave_event(self, state: dict[str, Any], entry: dict[str, Any], type_: str) -> None:
        """Submit only real, confirmed Wave transitions once per generation."""
        if state.get("source_mode") != "wave" or not entry or entry.get("starting"):
            return
        tid = str(entry.get("track_id") or "")
        if not tid:
            return
        source = state.setdefault("source_data", {})
        key = (int(state.get("source_epoch", 0)), int(entry.get("generation", 0)), tid,
               "completed" if type_ in {"skip", "trackFinished"} else type_)
        keys = source.setdefault("_feedback_keys", [])
        if key in keys:
            return
        keys.append(key)
        del keys[:-32]
        if type_ in {"skip", "trackFinished"}:
            source["last_completed_id"] = tid
            state["wave_refill_retry_after"] = 0.0
        seconds = 0.0
        if type_ in {"skip", "trackFinished"}:
            endpoint = float(entry.get("_pause_started_at") or time.monotonic())
            seconds = max(0.0, endpoint - float(entry.get("started_at", endpoint))
                          - float(entry.get("_paused_seconds") or 0.0))
            duration = int(entry.get("_expected_duration_ms") or 0) / 1000.0
            if duration > 0:
                seconds = min(duration, seconds)
        submit = getattr(self._yandex, "submit_wave_feedback", None)
        if callable(submit):
            try:
                submit(type_, tid, batch_id=str(entry.get("_wave_batch_id") or source.get("batch_id") or ""), seconds=seconds)
            except Exception as exc:
                # Ancillary feedback must never turn audible success into replay.
                logger.debug("Discord Rotor feedback enqueue failed: %s", type(exc).__name__)

    def _wave_entries(self, wave: dict[str, Any], *, requester: str = "") -> list[dict[str, Any]]:
        entries = []
        for track in list(wave.get("tracks") or []):
            entry = self._entry_from_track(track, requester=requester)
            batch = str(wave.get("batch_id") or "")
            batch_for = getattr(self._yandex, "_batch_for_track", None)
            if callable(batch_for):
                batch = batch_for(track, "user:onyourwave", batch)
            entry["_wave_batch_id"] = batch
            entries.append(entry)
        return entries

    @staticmethod
    def _wave_fingerprint(entry: dict[str, Any]) -> str:
        title = str(entry.get("title") or "").strip().casefold()
        artist = str(entry.get("artist") or "").strip().casefold()
        return title + "|" + artist if title else ""

    def _entry_from_track(self, track: Any, *, query: str = "", requester: str = "") -> dict[str, Any]:
        # Yandex can return Track, TrackShort, dictionaries or a wrapper with an
        # embedded Track. Collect display metadata from every lightweight layer
        # before falling back to an internal ID. Never expose placeholder "Трек".
        candidates: list[Any] = [track]
        embedded = track.get("track") if isinstance(track, dict) else getattr(track, "track", None)
        if embedded is not None and embedded is not track:
            candidates.append(embedded)

        def value(item: Any, *keys: str) -> Any:
            if isinstance(item, dict):
                for key in keys:
                    v = item.get(key)
                    if v not in (None, ""):
                        return v
                return ""
            for key in keys:
                v = getattr(item, key, None)
                if v not in (None, ""):
                    return v
            return ""

        title = ""
        artist = ""
        track_id = ""
        durations: list[int] = []
        for item in candidates:
            if not title:
                title = self._prefer_media_title(value(item, "title", "name"))
            if not track_id:
                track_id = str(value(item, "id", "track_id") or "").strip()
            try:
                durations.append(int(value(item, "duration_ms") or 0))
            except Exception:
                pass
            if artist:
                continue
            raw_artists = value(item, "artists") or []
            if isinstance(raw_artists, str):
                artist = raw_artists.strip()
            elif isinstance(raw_artists, (list, tuple)):
                names: list[str] = []
                for a in raw_artists:
                    name = str((a.get("name", "") if isinstance(a, dict) else getattr(a, "name", "")) or "").strip()
                    if name:
                        names.append(name)
                artist = ", ".join(names)
            if not artist and not isinstance(item, dict):
                try:
                    fn = getattr(item, "artists_name", None)
                    if callable(fn):
                        artist = str(fn() or "").strip()
                except Exception:
                    pass
            if not artist and isinstance(item, dict):
                artist = str(item.get("artist", "") or "").strip()

        explicit_query = str(query or "").strip()
        human_query = self._prefer_media_title(explicit_query)
        if not title:
            title = human_query
        playback_query = explicit_query or (f"{title} {artist}" if title and artist else title) or track_id
        return {
            "query": str(playback_query or "").strip(),
            "track_id": track_id,
            "title": title,
            "artist": artist,
            "added_by": str(requester or "").strip(),
            "_track_obj": track,
            "_expected_duration_ms": max(durations or [0]),
            "_retry_count": 0,
        }

    def _prefetch_queue_entry(self, entry: dict[str, Any] | None) -> None:
        """Prepare the *actual* Discord source while the current song is playing.

        HOTFIX43.1 warmed only the audio cache.  A later manual ``дальше`` still
        repeated Track materialisation + ``get_download_info()`` before Discord
        could even stop the old FFmpeg process, which accounts for several seconds
        of the field latency.  Store the complete candidate list on the queue item
        itself so the hot path can normally start from an already-downloaded file.
        """
        if not entry:
            return
        if str(entry.get("_source_type", "") or "").casefold() == "youtube":
            # HOTFIX78: prefetch the actual audio file, not an expiring signed CDN
            # URL. yt-dlp remains the HTTP client and the eventual FFmpeg handoff is
            # a stable local path.
            source_url = str(entry.get("_source_url", "") or "").strip()
            if not source_url:
                return
            if entry.get("_youtube_prefetch_started"):
                event = entry.get("_youtube_prefetch_event")
                failed_at = float(entry.get("_youtube_prefetch_finished_at", 0) or 0)
                path = str(entry.get("_youtube_cached_path", "") or "")
                if not isinstance(event, threading.Event) or not event.is_set():
                    return
                if path and os.path.isfile(path):
                    return
                if entry.get("_youtube_prefetch_error") and time.monotonic() - failed_at < 15.0:
                    return
                entry.pop("_youtube_prefetch_error", None)
                entry.pop("_youtube_prefetch_started", None)
            entry["_youtube_prefetch_started"] = True
            ready = threading.Event()
            entry["_youtube_prefetch_event"] = ready

            def prepare_youtube() -> None:
                try:
                    prepared = self._youtube.download_audio(source_url)
                    path = str(prepared.get("local_path", "") or "").strip()
                    if path and os.path.isfile(path):
                        entry["_youtube_cached_path"] = path
                        entry["_youtube_cached_meta"] = dict(prepared)
                        logger.debug("Discord YouTube prefetch ready for %r", entry.get("title") or source_url)
                    else:
                        raise ConnectionError("YouTube не вернул готовый аудиофайл")
                except Exception as exc:
                    entry["_youtube_prefetch_error"] = str(exc)
                    logger.debug("Discord YouTube prefetch failed: %s", exc)
                finally:
                    entry["_youtube_prefetch_finished_at"] = time.monotonic()
                    ready.set()

            if not PREFETCH.submit(f"discord-youtube:{id(entry)}", prepare_youtube):
                entry.pop("_youtube_prefetch_started", None)
                ready.set()
            return
        track_obj = entry.get("_track_obj")
        if track_obj is None:
            return
        if entry.get("_external_prefetch_started"):
            return
        entry["_external_prefetch_started"] = True
        ready = threading.Event()
        entry["_external_prefetch_event"] = ready

        def prepare() -> None:
            try:
                streams = self._yandex.prepare_playback_candidates_for_track(
                    track_obj,
                    title=str(entry.get("title", "") or ""),
                    artist=str(entry.get("artist", "") or ""),
                    cache_first=True,
                    wait_for_cache=True,
                )
                if streams:
                    entry["_prepared_streams"] = [dict(item) for item in streams]
                    logger.debug(
                        "Discord external prefetch ready for %r (%d candidates)",
                        entry.get("title") or entry.get("query"), len(streams),
                    )
            except Exception as exc:
                entry["_external_prefetch_error"] = str(exc)
                logger.debug("Discord queue external prefetch failed: %s", exc)
            finally:
                ready.set()

        if not PREFETCH.submit(f"discord-yandex:{id(entry)}", prepare):
            entry.pop("_external_prefetch_started", None)
            ready.set()

    @staticmethod
    def _prepared_queue_streams(entry: dict[str, Any]) -> list[dict[str, Any]]:
        usable: list[dict[str, Any]] = []
        for raw in list(entry.get("_prepared_streams") or []):
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            source = str(item.get("source") or item.get("url") or "").strip()
            if not source:
                continue
            method = str(item.get("source_method", "") or "").casefold()
            # The five-entry history normally retains managed files. A stale path
            # can still exist after startup cleanup or a failed transfer; never hand
            # such a path to FFmpeg.
            if method.startswith("cached_local_file") and not os.path.isfile(source):
                continue
            usable.append(item)
        return usable

    def _prefetch_next_queue_item(self, state: dict[str, Any] | None) -> None:
        if not state:
            return
        with self._queue_lock:
            items = list(state.get("items") or [])
        # HOTFIX125: keep five upcoming queue items warm. Each entry carries its
        # own one-shot prefetch flag, so repeated observer/dashboard refreshes do
        # not spawn duplicate downloads. Five warm items cover repeated rapid
        # next/previous commands while keeping the look-ahead bounded.
        for entry in items[:5]:
            self._prefetch_queue_entry(entry)

    def _play_queue_entry(
        self, entry: dict[str, Any], profile: dict[str, Any], channel: str = "", *, fast_transition: bool = False,
    ) -> dict[str, Any]:
        request_serial = entry.get("_request_serial", getattr(getattr(self, "_transport_request_local", None), "serial", None))
        def check_current() -> None:
            if request_serial is not None and request_serial != getattr(self, "_transport_request_serial", 0):
                raise DiscordPlaybackCancelled()
            expected = entry.get("_manual_start_serial")
            if expected is not None:
                with self._queue_lock:
                    state = self._queue_state(profile)
                    if (int(state.get("manual_action_serial", 0)) != expected
                            or int(state.get("source_epoch", 0)) != int(entry.get("_manual_start_epoch", state.get("source_epoch", 0)))):
                        raise DiscordPlaybackCancelled()
        def check_played_owner(bot: Any, data: dict[str, Any]) -> None:
            # The guarded bot commit owns cancellation. A status-then-stop here
            # has a TOCTOU race and could stop a newer transport after the read.
            check_current()
        check_current()
        if str(entry.get("_source_type", "") or "").casefold() == "youtube":
            source_url = str(entry.get("_source_url", "") or "").strip()
            if not source_url:
                raise RuntimeError("Элемент YouTube-очереди потерял исходную ссылку")
            cached_path = str(entry.get("_youtube_cached_path", "") or "").strip()
            prepared_meta = entry.get("_youtube_cached_meta") if isinstance(entry.get("_youtube_cached_meta"), dict) else {}
            if not cached_path or not os.path.isfile(cached_path):
                # A foreground request must join existing bounded preparation,
                # not wait on the same cache lock and repeat every failed profile.
                self._prefetch_queue_entry(entry)
                event = entry.get("_youtube_prefetch_event")
                deadline = time.monotonic() + (20.0 if fast_transition else 45.0)
                while isinstance(event, threading.Event) and not event.is_set():
                    check_current()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Подготовка YouTube затянулась; выберите другой трек или источник")
                    event.wait(timeout=0.05)
                check_current()
                cached_path = str(entry.get("_youtube_cached_path", "") or "").strip()
                prepared_meta = entry.get("_youtube_cached_meta") if isinstance(entry.get("_youtube_cached_meta"), dict) else {}
                if not cached_path or not os.path.isfile(cached_path):
                    raise ConnectionError("YouTube не предоставил аудио. Выберите другой трек или источник и повторите позже")
            bot = self._background_for_profile(profile)
            resolved = dict(prepared_meta or {})
            resolved["local_path"] = cached_path
            check_current()
            data = bot.play_url(
                profile, cached_path,
                title=str(resolved.get("title", "") or entry.get("title", "") or "YouTube"),
                artist=str(resolved.get("artist", "") or entry.get("artist", "") or "YouTube"),
                channel=str(channel or "").strip(),
                fast_start=bool(fast_transition),
                prefer_current_voice=bool(fast_transition),
                http_headers=dict(resolved.get("http_headers") or {}),
                before_start=check_current,
            )
            check_played_owner(bot, data)
            result = {
                **data,
                "query": str(entry.get("query", "") or source_url),
                "track_id": str(resolved.get("video_id", "") or resolved.get("track_id", "") or entry.get("track_id", "")),
                "title": str(resolved.get("title", "") or entry.get("title", "") or "YouTube"),
                "artist": str(resolved.get("artist", "") or entry.get("artist", "") or "YouTube"),
                "source_method": str(data.get("source_method", "") or "discord_bot_youtube_url_handoff"),
                "expected_duration_ms": int(resolved.get("duration_ms", 0) or resolved.get("expected_duration_ms", 0) or entry.get("_expected_duration_ms", 0) or 0),
                "api_used": False, "youtube_integration_used": False,
                "mode": "jarvis_bot", "media_source": "YouTube",
                "webpage_url": str(resolved.get("webpage_url", "") or source_url),
            }
            result["media_event_id"] = publish_now_playing(
                "discord", result["title"], result.get("artist", ""), result.get("track_id", ""),
                duration_ms=int(result.get("expected_duration_ms", 0) or 0),
                playback_generation=int(data.get("playback_generation", 0) or 0),
                playback_scope=self._media_playback_scope(profile),
            )
            self._broadcast_queue_snapshot(profile, delay=0.08)
            return result

        track_obj = entry.get("_track_obj")
        streams: list[dict[str, Any]] = self._prepared_queue_streams(entry)
        # If the whole-song prefetch is just finishing, a very short wait is much
        # cheaper than repeating Yandex API discovery on the foreground command.
        if not streams and fast_transition:
            event = entry.get("_external_prefetch_event")
            if isinstance(event, threading.Event):
                event.wait(timeout=0.02)
                streams = self._prepared_queue_streams(entry)
        if track_obj is not None and not streams:
            try:
                streams = self._yandex.prepare_playback_candidates_for_track(
                    track_obj, title=str(entry.get("title", "") or ""),
                    artist=str(entry.get("artist", "") or ""), cache_first=True, wait_for_cache=False,
                )
            except TypeError:
                # Plugin/older-adapter compatibility.
                streams = self._yandex.prepare_playback_candidates_for_track(
                    track_obj, title=str(entry.get("title", "") or ""),
                    artist=str(entry.get("artist", "") or ""), cache_first=False,
                )
        if not streams:
            check_current()
            if entry.get("_manual_start_serial") is not None:
                raise RuntimeError("Не удалось подготовить трек «Моей волны». Попробуйте ещё раз.")
            # Compatibility for queue entries created before v4.0.13 or metadata
            # lookups that genuinely failed: resolve once from the original query.
            return self._play_query(str(entry.get("query", "") or ""), profile, channel)

        bot = self._background_for_profile(profile)
        last_error: Exception | None = None
        for stream in streams[:4]:
            try:
                check_current()
                data = bot.play_url(
                    profile, str(stream.get("source") or stream.get("url") or ""),
                    title=str(stream.get("title", "") or entry.get("title", "") or entry.get("query", "")),
                    artist=str(stream.get("artist", "") or entry.get("artist", "") or ""),
                    channel=str(channel or "").strip(),
                    fast_start=bool(fast_transition),
                    prefer_current_voice=bool(fast_transition),
                    before_start=check_current,
                )
                check_played_owner(bot, data)
                result = {
                    **data, "query": str(entry.get("query", "") or ""),
                    "track_id": str(stream.get("id", "") or entry.get("track_id", "")),
                    "title": self._prefer_media_title(stream.get("title", ""), entry.get("title", ""), entry.get("query", "")),
                    "artist": str(stream.get("artist", "") or entry.get("artist", "") or ""),
                    "source_method": stream.get("source_method", "direct_stream"),
                    "expected_duration_ms": int(stream.get("expected_duration_ms", 0) or entry.get("_expected_duration_ms", 0) or 0),
                    "api_used": True, "mode": "jarvis_bot",
                }
                result["media_event_id"] = publish_now_playing(
                    "discord", result["title"], result.get("artist", ""), result.get("track_id", ""),
                    duration_ms=int(result.get("expected_duration_ms", 0) or 0),
                    playback_generation=int(data.get("playback_generation", 0) or 0),
                    playback_scope=self._media_playback_scope(profile),
                )
                self._broadcast_queue_snapshot(profile, delay=0.08)
                return result
            except DiscordPlaybackCancelled:
                raise
            except Exception as exc:
                last_error = exc
                text = str(exc).casefold()
                if isinstance(exc, TimeoutError) or any(key in text for key in (
                    "голосов", "voice channel", "voice handshake", "voice connect", "guild", "сервер",
                    "connect", "speak", "permission", "gateway", "token"
                )):
                    raise
                logger.warning("Discord queued source failed for %r: %s", entry.get("title") or entry.get("query"), exc)
        raise RuntimeError(f"Discord не смог воспроизвести подготовленный трек очереди: {last_error or 'unknown error'}")

    def _start_queue_monitor(self, profile: dict[str, Any]) -> None:
        key = self._profile_key(profile)
        with self._queue_lock:
            state = self._queue_state(profile)
            monitor = state.get("monitor")
            if isinstance(monitor, threading.Thread) and monitor.is_alive():
                return
            thread = threading.Thread(
                target=self._queue_monitor, args=(key,),
                name=f"jarvis-discord-queue-{key[-12:]}", daemon=True,
            )
            state["monitor"] = thread
            thread.start()

    def _queue_monitor(self, key: str) -> None:
        # Polling keeps queue ownership in the adapter (where Yandex search is
        # available) while DiscordBackgroundBot remains a small voice transport.
        while True:
            time.sleep(0.65)
            with self._queue_lock:
                state = self._queues.get(key)
                if not state:
                    return
                if bool(state.get("transport_faulted")):
                    return
                current = dict(state.get("current") or {})
                profile = dict(state.get("profile") or {})
                queued = list(state.get("items") or [])
            if not current:
                if not queued and state.get("source_mode") != "wave":
                    return
                if state.get("manual_paused"):
                    continue
                self._advance_queue(key)
                continue
            # HOTFIX44: after an explicit next/previous, ignore stale completion
            # samples for a short bounded guard window even if discord.py delivers
            # the old FFmpeg callback late. This makes one spoken command consume
            # exactly one queue item.
            if time.monotonic() < float(state.get("manual_guard_until", 0.0) or 0.0):
                continue
            # A manual next/previous owns the queue while its new FFmpeg transport
            # is still being created.  Polling the old voice generation during
            # this window was the race that consumed a *second* queue item from
            # one spoken «дальше».
            if bool(current.get("starting")):
                continue
            try:
                status = self._background_for_profile(profile).playback_status(profile)
            except Exception as exc:
                logger.debug("Discord queue status poll failed for %s: %s", key, exc)
                continue
            transport_generation = int(current.get("_transport_generation", 0) or 0)
            status_generation = int(status.get("playback_generation", 0) or 0)
            # Status and completion events must describe the exact FFmpeg
            # generation that owns this queue item.  This rejects late callbacks
            # from the track stopped by a manual «дальше»/skip command.
            if transport_generation and status_generation and status_generation != transport_generation:
                continue
            if status.get("playing") or status.get("paused"):
                should_broadcast = False
                with self._queue_lock:
                    state = self._queues.get(key)
                    if state and state.get("current") and (not status_generation or int((state["current"] or {}).get("_transport_generation", 0) or 0) == status_generation):
                        state["current"]["seen_active"] = True
                        self._wave_pause_clock(state, paused=bool(status.get("paused")))
                        now = time.monotonic()
                        last_shared = float(state.get("last_shared_broadcast_at", 0.0) or 0.0)
                        # Heartbeat the compact now-playing snapshot while a
                        # remote NEO owns Discord voice.  A NEO that starts
                        # in the middle of somebody else's song can therefore
                        # attach passively even if its first queue_probe was lost
                        # during Gateway startup/reconnect.  V4.7 uses a self-
                        # mention on broadcast packets, so this heartbeat remains
                        # readable without privileged MESSAGE_CONTENT intent.
                        if now - last_shared >= 7.0:
                            state["last_shared_broadcast_at"] = now
                            should_broadcast = True
                if should_broadcast:
                    self._broadcast_queue_snapshot(profile)
                continue
            started = float(current.get("started_at", 0.0) or 0.0)
            elapsed = max(0.0, time.monotonic() - started)
            if not current.get("seen_active") and elapsed < 2.0:
                continue

            # HOTFIX9: trust the Discord voice transport's playback generation, not
            # a transient is_playing()==False sample. Manual skip stops the old
            # FFmpeg process and starts a new generation; the old completion must
            # never make the queue advance again.
            finished_generation = int(status.get("last_finished_generation", 0) or 0)
            if transport_generation and finished_generation != transport_generation:
                continue

            expected_ms = int(current.get("_expected_duration_ms", 0) or 0)
            retry_count = int(current.get("_retry_count", 0) or 0)
            # A full Yandex track unexpectedly stopping a few seconds after a
            # manual skip is almost certainly a transport hiccup, not a natural
            # song end. Retry the *same* item once instead of silently skipping a
            # second song. Unknown-duration items get a conservative 10 s floor.
            expected_s = expected_ms / 1000.0 if expected_ms > 0 else 0.0
            premature = premature_finish188(elapsed, expected_ms, bool(current.get("seen_active")))
            if premature and retry_count < 1:
                with self._queue_lock:
                    state = self._queues.get(key)
                    if not state:
                        return
                    live = dict(state.get("current") or {})
                    if int(live.get("generation", -1)) != int(current.get("generation", -2)):
                        continue
                    retry_entry = {
                        k: v for k, v in live.items()
                        if k not in {"generation", "started_at", "seen_active", "starting", "_transport_generation"}
                    }
                    retry_entry["_retry_count"] = retry_count + 1
                    state["current"] = None
                    state.setdefault("items", []).insert(0, retry_entry)
                logger.warning(
                    "Discord queue playback ended prematurely after %.1fs (expected=%s); retrying same track once: %r",
                    elapsed, f"{expected_s:.1f}s" if expected_s else "unknown", current.get("title") or current.get("query"),
                )
                self._advance_queue(key)
                continue

            if premature and retry_count >= 1:
                # Two very short finishes in a row indicate a broken transport,
                # not two naturally completed songs. Freeze the queue instead of
                # burning through hundreds of tracks and emitting fake now-playing
                # events. The same item is kept at the front for an explicit retry.
                with self._queue_lock:
                    state = self._queues.get(key)
                    if not state:
                        return
                    live = dict(state.get("current") or {})
                    if int(live.get("generation", -1)) != int(current.get("generation", -2)):
                        continue
                    retry_entry = {
                        k: v for k, v in live.items()
                        if k not in {"generation", "started_at", "seen_active", "starting", "_transport_generation"}
                    }
                    retry_entry["_retry_count"] = 0
                    state["current"] = None
                    state.setdefault("items", []).insert(0, retry_entry)
                    state["transport_faulted"] = True
                    state["transport_fault_reason"] = (
                        f"Discord audio transport остановился через {elapsed:.1f}s "
                        f"вместо ожидаемых {expected_s:.1f}s" if expected_s else
                        f"Discord audio transport остановился через {elapsed:.1f}s"
                    )
                    fault_reason = str(state["transport_fault_reason"])
                logger.error(
                    "Discord queue halted after repeated premature playback: %s", fault_reason,
                )
                return

            with self._queue_lock:
                state = self._queues.get(key)
                if not state:
                    return
                # Only finish the same current item we inspected above.
                if int((state.get("current") or {}).get("generation", -1)) != int(current.get("generation", -2)):
                    continue
                finished = dict(state.get("current") or {})
                self._wave_event(state, finished, "trackFinished")
                repeat_mode = str(state.get("repeat_mode", "off") or "off").casefold()
                if finished and repeat_mode == "track":
                    repeated = {
                        k: v for k, v in finished.items()
                        if k not in {"generation", "started_at", "seen_active", "starting", "_transport_generation"}
                    }
                    repeated["_retry_count"] = 0
                    state.setdefault("items", []).insert(0, repeated)
                elif finished:
                    self._push_history(state, finished)
                state["current"] = None
            self._advance_queue(key)

    def _advance_queue(self, key: str) -> None:
        while True:
            need_wave_refill = False
            with self._queue_lock:
                state = self._queues.get(key)
                if not state:
                    return
                epoch = int(state.get("source_epoch",0))
                if bool(state.get("transport_faulted")) or state.get("manual_paused"):
                    return
                if state.get("current"):
                    return
                if str(state.get("source_mode", "") or "") == "wave" and len(state.get("items") or []) > 12:
                    del state["items"][12:]
                items = state.get("items") or []
                if not items:
                    source_mode = str(state.get("source_mode", "") or "")
                    repeat_mode = str(state.get("repeat_mode", "off") or "off").casefold()
                    # Playlist repeat is a real queue wrap, not a decorative UI
                    # flag. The source seed is private in-memory state and is
                    # rebuilt only after the finite playlist reaches its end.
                    if source_mode == "playlist" and repeat_mode == "playlist":
                        seed = [dict(item) for item in list((state.get("source_data") or {}).get("_repeat_entries", []) or []) if isinstance(item, dict)]
                        if seed:
                            state["items"] = seed
                            if bool(state.get("shuffle")):
                                self._shuffle_pending_items(state)
                            items = state.get("items") or []
                    if not items:
                        need_wave_refill = source_mode == "wave"
                        if need_wave_refill and float(state.get("wave_refill_retry_after", 0.0) or 0.0) > time.monotonic():
                            return
                        if not need_wave_refill:
                            # A finite playlist is finished. Do not silently fall back
                            # to My Wave or another local source when repeat is off.
                            if source_mode == "playlist":
                                state["source_mode"] = ""
                                state["source_title"] = ""
                                state["source_data"] = {}
                            return
                if not need_wave_refill:
                    entry = self._pop_next_queue_entry(state)
                    if entry is None:
                        # Duplicate suppression may consume the remainder of a
                        # Rotor batch. My Wave is infinite, so refill immediately
                        # instead of leaving current=None with a dead monitor.
                        if str(state.get("source_mode", "") or "") == "wave":
                            need_wave_refill = True
                            profile = dict(state.get("profile") or {})
                            shuffle_enabled = False
                            remaining_count = 0
                        else:
                            return
                    else:
                        profile = dict(state.get("profile") or {})
                        state["generation"] = int(state.get("generation", 0)) + 1
                        generation = int(state["generation"])
                        entry["_manual_start_serial"] = int(state.get("manual_action_serial", 0))
                        entry["_manual_start_epoch"] = int(state.get("source_epoch", 0))
                        state["current"] = {**entry, "generation": generation, "started_at": time.monotonic(), "seen_active": False, "starting": True}
                        shuffle_enabled = bool(state.get("shuffle"))
                        remaining_count = len(state.get("items") or [])
                else:
                    shuffle_enabled = False
                    remaining_count = 0
            if not need_wave_refill and entry is not None:
                logger.info(
                    "Discord queue selected %r shuffle=%s remaining=%d",
                    str(entry.get("title") or entry.get("query") or ""), shuffle_enabled, remaining_count,
                )
            if need_wave_refill:
                last_refill_error: Exception | None = None
                refill_succeeded = False
                for refill_attempt in range(1, 5):
                    try:
                        with self._queue_lock:
                            live = self._queues.get(key) or {}
                            source_data = live.get("source_data") or {}
                            requester = str(source_data.get("requester", "") or "")
                            recent_ids = list(source_data.get("recent_ids", []) or [])
                            queue_hint = str(recent_ids[-1] if recent_ids else "")
                        try:
                            wave = self._yandex.wave_tracks(
                                queue_track_id=str(source_data.get("last_completed_id") or queue_hint),
                                avoid_ids=self._wave_avoid_history(recent_ids), fresh_start=False,
                            )
                        except TypeError:
                            wave = self._yandex.wave_tracks()
                        entries = self._wave_entries(wave, requester=requester)
                        if not entries:
                            last_refill_error = RuntimeError("Яндекс Музыка пока не вернула следующие треки «Моей волны»")
                            break  # Repeating the same context cannot advance Rotor.
                        with self._queue_lock:
                            state = self._queues.get(key)
                            if not state or str(state.get("source_mode", "") or "") != "wave" or int(state.get("source_epoch",0)) != epoch:
                                return
                            state.setdefault("items", []).extend(entries)
                            if bool(state.get("shuffle")):
                                self._shuffle_pending_items(state)
                            # Infinite Wave queue is a rolling window, not an
                            # ever-growing playlist. Hard-cap stale/overlapping
                            # refills even if a provider unexpectedly returns a
                            # very large batch.
                            if len(state.get("items") or []) > 12:
                                del state["items"][12:]
                            state.setdefault("source_data", {})["batch_id"] = str(wave.get("batch_id", "") or "")
                            state["wave_refill_retry_after"] = 0.0
                        refill_succeeded = True
                        break
                    except Exception as exc:
                        last_refill_error = exc
                        logger.warning("Discord My Wave refill attempt %s/4 failed on %s: %s", refill_attempt, key, exc)
                        if refill_attempt < 4:
                            time.sleep(0.25 * refill_attempt)
                if refill_succeeded:
                    # Continue the outer queue loop immediately and start exactly
                    # one fresh item. HOTFIX88 accidentally continued the retry
                    # loop after success and could fetch several overlapping
                    # batches before returning without starting the next song.
                    continue
                with self._queue_lock:
                    state = self._queues.get(key)
                    # NEO 187: only the source that failed may set its own cooldown;
                    # a newer manual source (another epoch) keeps its admission.
                    if (state and str(state.get("source_mode", "") or "") == "wave"
                            and int(state.get("source_epoch", 0)) == epoch):
                        state["wave_refill_retry_after"] = time.monotonic() + 10.0
                logger.warning("Discord My Wave refill deferred on %s: %s", key, last_refill_error)
                return
            try:
                self._prefetch_queue_entry(entry)
                with self._queue_lock:
                    live = self._queues.get(key) or {}
                    if int(live.get("source_epoch",0)) != epoch or int((live.get("current") or {}).get("generation",-1)) != generation:
                        return
                data = self._play_queue_entry(entry, profile)
                with self._queue_lock:
                    state = self._queues.get(key)
                    current = (state or {}).get("current") or {}
                    if state and int(current.get("generation", -1)) == generation:
                        state["current"] = {
                            **entry,
                            "track_id": str(data.get("track_id", "") or entry.get("track_id", "")),
                            "title": self._prefer_media_title(data.get("title", ""), entry.get("title", ""), entry.get("query", "")),
                            "artist": str(data.get("artist", "") or entry.get("artist", "")),
                            "generation": generation, "started_at": time.monotonic(), "seen_active": False,
                            "_transport_generation": int(data.get("playback_generation", 0) or 0),
                            "_expected_duration_ms": int(data.get("expected_duration_ms", 0) or entry.get("_expected_duration_ms", 0) or 0),
                            "_retry_count": int(entry.get("_retry_count", 0) or 0),
                        }
                        state["current"].pop("_request_serial", None)
                        state["current"].pop("_manual_start_serial", None)
                        state["current"].pop("_manual_start_epoch", None)
                        state["current"].pop("_pause_started_at", None)
                        state["current"].pop("_paused_seconds", None)
                        if str(state.get("source_mode", "") or "") == "wave":
                            tid = str(state["current"].get("track_id", "") or "")
                            recent = state.setdefault("source_data", {}).setdefault("recent_ids", [])
                            if tid: recent[:] = ([x for x in recent if x != tid] + [tid])[-300:]
                            self._remember_wave_id(
                                tid, title=str(state["current"].get("title", "") or ""),
                                artist=str(state["current"].get("artist", "") or ""),
                            )
                            self._wave_event(state, state["current"], "trackStarted")
                self._prefetch_next_queue_item(state)
                if str((state or {}).get("source_mode", "") or "") == "wave":
                    self._ensure_wave_queue_depth_background(profile, 8)
                self._broadcast_queue_snapshot(profile)
                logger.info("Discord queue started %r on %s", entry.get("title") or entry["query"], key)
                return
            except DiscordPlaybackCancelled:
                with self._queue_lock:
                    live = self._queues.get(key) or {}
                    if int((live.get("current") or {}).get("generation", -1)) == generation:
                        live["current"] = None
                        live.setdefault("items", []).insert(0, entry)
                        live["transport_faulted"] = True
                        live["transport_fault_reason"] = "Подготовка заменена новой командой; требуется новый запрос"
                return
            except Exception as exc:
                logger.warning("Discord queued track %r failed: %s", entry.get("query"), exc)
                transport_failure = self._is_transport_failure(exc) or isinstance(exc, (ConnectionError, TimeoutError))
                with self._queue_lock:
                    state = self._queues.get(key)
                    if state and int(((state.get("current") or {}).get("generation", -1))) == generation:
                        state["current"] = None
                        if transport_failure:
                            retry_entry = dict(entry)
                            retry_entry["_retry_count"] = 0
                            state.setdefault("items", []).insert(0, retry_entry)
                            state["transport_faulted"] = True
                            state["transport_fault_reason"] = str(exc)
                if transport_failure:
                    logger.error("Discord queue halted because voice transport failed: %s", exc)
                    return
                # A genuinely unavailable individual track can be skipped, but a
                # broken audio transport must never consume the rest of the queue.
                continue

    def _remote_for_any_profile(self, server: str = "") -> tuple[dict[str, Any], dict[str, str]] | None:
        """Find the NEO process that currently owns Discord voice for a profile."""
        explicit = bool(str(server or "").strip())
        last_error: Exception | None = None
        for profile in self._candidate_profiles(server):
            try:
                host = self._remote_host(profile)
                if host:
                    return profile, host
                # If our own process already owns this profile, it is local and
                # must not be forwarded.
                try:
                    if self._background_for_profile(profile).playback_status(profile).get("connected"):
                        return None
                except Exception:
                    pass
            except Exception as exc:
                last_error = exc
                if explicit:
                    raise
        if explicit and last_error:
            raise last_error
        return None

    def _ensure_wave_queue_depth_background(self, profile: dict[str, Any], minimum: int = 5) -> None:
        """Read one real continuation in the background; never restart the radio."""
        minimum = max(5, min(12, int(minimum or 5)))
        key = self._profile_key(profile)
        with self._queue_lock:
            state = self._queues.get(key)
            if not state or state.get("source_mode") != "wave":
                return
            if (len(state.get("items") or []) >= minimum or state.get("wave_background_refill_active")
                    or float(state.get("wave_refill_retry_after", 0.0)) > time.monotonic()):
                return
            epoch = int(state.get("source_epoch", 0))
            state["wave_background_refill_active"] = True

        def worker() -> None:
            try:
                with self._queue_lock:
                    live = self._queues.get(key) or {}
                    if live.get("source_mode") != "wave" or int(live.get("source_epoch", 0)) != epoch:
                        return
                    source = dict(live.get("source_data") or {})
                    old_completed = str(source.get("last_completed_id") or "")
                    avoid = set(self._wave_avoid_history(list(source.get("recent_ids", []))))
                    avoid.update(str(x.get("track_id") or "") for x in live.get("items", []))
                    avoid.add(str((live.get("current") or {}).get("track_id") or ""))
                try:
                    wave = self._yandex.wave_tracks(queue_track_id=old_completed, avoid_ids=avoid,
                                                   fresh_start=False, fast_start=False)
                except TypeError:
                    wave = self._yandex.wave_tracks()
                entries = self._wave_entries(wave, requester=str(source.get("requester") or ""))
                with self._queue_lock:
                    live = self._queues.get(key) or {}
                    if live.get("source_mode") != "wave" or int(live.get("source_epoch", 0)) != epoch:
                        return
                    seen = {str(x.get("track_id") or "") for x in live.get("items", [])}
                    seen.update(self._wave_avoid_history(list(live.get("source_data", {}).get("recent_ids", []))))
                    seen.add(str((live.get("current") or {}).get("track_id") or ""))
                    items = live.setdefault("items", [])
                    seen_recordings = {self._wave_fingerprint(x) for x in [*items, live.get("current") or {}]}
                    for entry in entries:
                        tid = str(entry.get("track_id") or "")
                        recording = self._wave_fingerprint(entry)
                        if tid and tid not in seen and (not recording or recording not in seen_recordings) and len(items) < 12:
                            items.append(entry)
                            seen.add(tid)
                            if recording:
                                seen_recordings.add(recording)
                    # A newer actual completion deserves an immediate new context read.
                    if str(live.get("source_data", {}).get("last_completed_id") or "") == old_completed:
                        live["wave_refill_retry_after"] = time.monotonic() + (5.0 if entries else 20.0)
                self._broadcast_queue_snapshot(profile, reanchor=False)
                self._prefetch_next_queue_item(live)
            except Exception as exc:
                with self._queue_lock:
                    live = self._queues.get(key) or {}
                    if int(live.get("source_epoch", 0)) == epoch:
                        live["wave_refill_retry_after"] = time.monotonic() + 10.0
                logger.debug("Discord My Wave background queue refill failed: %s", type(exc).__name__)
            finally:
                with self._queue_lock:
                    live = self._queues.get(key)
                    if live is not None and int(live.get("source_epoch", 0)) == epoch:
                        live["wave_background_refill_active"] = False
        threading.Thread(target=worker, name="neo-discord-wave-refill", daemon=True).start()

    def _begin_personal_source(self, profile: dict[str, Any], entries: list[dict[str, Any]], *, mode: str, title: str, requester: str = "", source_data: dict[str, Any] | None = None, expected_manual_serial: int | None = None) -> dict[str, Any]:
        if not entries:
            raise RuntimeError(f"Источник «{title}» не вернул ни одного трека")
        state = self._queue_state(profile)
        pool = [dict(item) for item in entries]
        # HOTFIX132: My Wave is infinite, so keeping the complete generated
        # Rotor stream in RAM is both useless and dangerous. Older long sessions
        # were observed with ~963 pending Discord entries. Keep a bounded rolling
        # window; the background refiller replenishes it as tracks are consumed.
        if mode == "wave":
            pool = pool[:12]
        with self._queue_lock:
            shuffle_enabled = bool(state.get("shuffle"))
        if shuffle_enabled and len(pool) > 1:
            random.SystemRandom().shuffle(pool)
        first = dict(pool.pop(0))
        rest = pool
        # Personalized source replaces the complete shared queue by design.
        with self._queue_lock:
            if expected_manual_serial is not None:
                if int(state.get("manual_action_serial", 0)) != expected_manual_serial:
                    return {"verified": False, "cancelled": True, "superseded": True, "non_invasive": True}
                first["_manual_start_serial"] = expected_manual_serial
                first["_request_serial"] = getattr(self, "_transport_request_serial", 0)
            state["items"] = rest
            state["source_epoch"] = int(state.get("source_epoch", 0)) + 1
            if expected_manual_serial is not None:
                first["_manual_start_epoch"] = int(state["source_epoch"])
            state["restored_queue"] = False
            state["wave_background_refill_active"] = False
            state["source_mode"] = mode
            state["wave_strict_repeat"] = getattr(self._yandex, "wave_strict_repeat_policy", lambda: True)()
            state["source_title"] = title
            state["source_data"] = {**dict(source_data or {}), "requester": str(requester or "")}
            if mode == "playlist":
                state["source_data"]["_repeat_entries"] = [dict(item) for item in entries]
            state["source_data"].setdefault("recent_ids", [])
            state["history"] = []
            state["transport_faulted"] = False
            state["transport_fault_reason"] = ""
            state["manual_paused"] = False
            state["generation"] = int(state.get("generation", 0)) + 1
            generation = int(state["generation"])
            state["current"] = {**first, "generation": generation, "started_at": time.monotonic(), "seen_active": False, "starting": True}
        # Start the first item immediately from its direct API URL, while warming
        # a complete local copy in parallel for retry/recovery.
        self._prefetch_queue_entry(first)
        try:
            data = self._play_queue_entry(first, profile)
        except DiscordPlaybackCancelled:
            with self._queue_lock:
                if int((state.get("current") or {}).get("generation", -1)) == generation:
                    state["current"] = None
                    state.setdefault("items", []).insert(0, first)
                    state["transport_faulted"] = True
                    state["transport_fault_reason"] = "Подготовка заменена новой командой; требуется новый запрос"
            raise
        except Exception as exc:
            with self._queue_lock:
                if int((state.get("current") or {}).get("generation", -1)) == generation:
                    state["current"] = None
                    state.setdefault("items", []).insert(0, first)
                    state["transport_faulted"] = True
                    state["transport_fault_reason"] = str(exc)
            raise
        with self._queue_lock:
            if expected_manual_serial is not None and int(state.get("manual_action_serial", 0)) != expected_manual_serial:
                return {"verified": False, "cancelled": True, "superseded": True, "non_invasive": True}
            current = state.get("current") or {}
            if int(current.get("generation", -1)) == generation:
                state["current"] = {
                    **first,
                    "track_id": str(data.get("track_id", "") or first.get("track_id", "")),
                    "title": str(data.get("title", "") or first.get("title", "")),
                    "artist": str(data.get("artist", "") or first.get("artist", "")),
                    "generation": generation, "started_at": time.monotonic(), "seen_active": False,
                    "_transport_generation": int(data.get("playback_generation", 0) or 0),
                    "_expected_duration_ms": int(data.get("expected_duration_ms", 0) or first.get("_expected_duration_ms", 0) or 0),
                    "_retry_count": int(first.get("_retry_count", 0) or 0),
                }
                state["current"].pop("_request_serial", None)
                state["current"].pop("_manual_start_serial", None)
                state["current"].pop("_manual_start_epoch", None)
                state["current"].pop("_pause_started_at", None)
                state["current"].pop("_paused_seconds", None)
                if mode == "wave":
                    tid = str(state["current"].get("track_id", "") or "")
                    recent = state.setdefault("source_data", {}).setdefault("recent_ids", [])
                    if tid:
                        recent[:] = ([x for x in recent if x != tid] + [tid])[-300:]
                    self._remember_wave_id(
                        tid, title=str(state["current"].get("title", "") or ""),
                        artist=str(state["current"].get("artist", "") or ""),
                    )
                    self._wave_event(state, state["current"], "trackStarted")
        self._host_cache.pop(self._profile_key(profile), None)
        self._prefetch_next_queue_item(state)
        self._start_queue_monitor(profile)
        if mode == "wave":
            self._ensure_wave_queue_depth_background(profile, 8)
        self._broadcast_queue_snapshot(profile, reanchor=False)
        return {
            **data, "source_mode": mode, "source_title": title,
            "queue_replaced": True, "queue_length": len(rest), "server": profile.get("name", ""),
            "shared_queue": bool(self.config.get("integrations.discord.shared_music_queue_enabled", True)),
        }

    @latest_transport
    def music_my_tracks(self, server: str = "", *, requester: str = "") -> dict[str, Any]:
        """Play the requester's Yandex «Мои треки» through the Discord bot."""
        remote = self._remote_for_any_profile(server)
        profile = self._background_profile(server)
        if remote:
            remote_profile, host = remote
            who = requester or str(remote_profile.get("discord_user_id", "") or "")
            return self._remote_request(remote_profile, host, "my_tracks", {"requester": who}, timeout=18.0)
        likes = list(self._yandex.liked_tracks() or [])
        if not likes:
            raise RuntimeError("В «Моих треках» Яндекс Музыки не найдено доступных композиций")
        who = requester or str(profile.get("discord_user_id", "") or "")
        entries = [self._entry_from_track(track, requester=who) for track in likes]
        transport_checkpoint(self)
        return self._begin_personal_source(
            profile, entries, mode="my_tracks", title="Мои треки", requester=who,
            source_data={"library": "likes"},
        )

    @latest_transport
    def music_my_wave(self, server: str = "", *, requester: str = "") -> dict[str, Any]:
        # My Wave reuses the process that already owns the shared VoiceClient.
        # Forward the source request to that host instead of forcing leave/rejoin;
        # track transitions stay inside the same Discord voice connection.
        remote = self._remote_for_any_profile(server)
        profile = self._background_profile(server)
        if remote:
            remote_profile, host = remote
            who = requester or str(remote_profile.get("discord_user_id", "") or "")
            return self._remote_request(remote_profile, host, "my_wave", {"requester": who}, timeout=18.0)
        state = self._queue_state(profile)
        with self._queue_lock:
            same_wave = str(state.get("source_mode", "") or "") == "wave"
            previous_id = str((state.get("current") or {}).get("track_id", "") or "") if same_wave else ""
            old_recent = list((state.get("source_data") or {}).get("recent_ids", []) or []) if same_wave else []
            manual_serial = int(state.get("manual_action_serial", 0))
            original_epoch = int(state.get("source_epoch", 0))
        if same_wave and previous_id:
            snapshot = self._local_queue_snapshot(profile)
            if snapshot.get("playing") and not snapshot.get("transport_faulted"):
                current = snapshot.get("current") or {}
                return {**snapshot, "unchanged": True, "title": current.get("title", ""),
                        "artist": current.get("artist", ""), "track_id": previous_id}
            if snapshot.get("paused"):
                with self._queue_lock:
                    if int(state.get("manual_action_serial", 0)) != manual_serial:
                        return {"verified": False, "cancelled": True, "superseded": True, "non_invasive": True}
                resumed = self._background_for_profile(profile).voice_action(profile, "resume")
                with self._queue_lock:
                    cancelled = int(state.get("manual_action_serial", 0)) != manual_serial
                    same_source = int(state.get("source_epoch", 0)) == original_epoch
                if cancelled:
                    if same_source:
                        self._background_for_profile(profile).voice_action(profile, "pause")
                    return {"verified": False, "cancelled": True, "superseded": True, "non_invasive": True}
                with self._queue_lock:
                    state["manual_paused"] = False
                    self._wave_pause_clock(state, paused=False)
                current = snapshot.get("current") or {}
                return {**snapshot, **resumed, "paused": False, "playing": True, "unchanged": True,
                        "title": current.get("title", ""), "artist": current.get("artist", ""), "track_id": previous_id}

        # V5.4 latency path: Discord voice negotiation and Yandex Rotor lookup are
        # independent. Run them in parallel so a cold My Wave start pays roughly
        # max(voice, Rotor) instead of voice + Rotor. The voice helper itself has
        # a bounded two-attempt handshake and never loops indefinitely.
        transport_checkpoint(self)
        join_box: dict[str, Any] = {}
        join_done = threading.Event()
        bot = self._background_for_profile(profile)
        def _prejoin() -> None:
            try:
                join_box["data"] = bot.voice_action(profile, "join")
            except Exception as exc:
                join_box["error"] = exc
            finally:
                join_done.set()
        if self._local_voice_is_connected(profile):
            join_done.set()
        else:
            threading.Thread(target=_prejoin, name="jarvis-discord-my-wave-prejoin", daemon=True).start()

        try:
            wave = self._yandex.wave_tracks(
                queue_track_id=previous_id, avoid_ids=self._wave_avoid_history(old_recent), fresh_start=True, fast_start=True
            )
        except TypeError:
            # Compatibility for injected/older adapters used by plugins/tests.
            wave = self._yandex.wave_tracks()
        transport_checkpoint(self)
        entries = self._wave_entries(wave, requester=requester or str(profile.get("discord_user_id", "") or ""))
        if not entries:
            if wave.get("network_unavailable"):
                raise ConnectionError("Обнаружена высокая задержка или нарушение работы сети.")
            raise RuntimeError("Яндекс Музыка пока не вернула доступные новые треки «Моей волны». Попробуйте немного позже.")
        # HOTFIX104: as soon as Rotor returns the first personalized batch, start
        # resolving/downloading its first track while Discord is still finishing
        # the voice handshake. This removes another serial wait from cold starts.
        if entries:
            self._prefetch_queue_entry(entries[0])
        join_deadline = time.monotonic() + 13.0
        while not join_done.wait(timeout=0.05) and time.monotonic() < join_deadline:
            transport_checkpoint(self)
        transport_checkpoint(self)
        join_error = join_box.get("error")
        if join_error is not None and not self._local_voice_is_connected(profile):
            raise RuntimeError(f"Discord voice не подготовился для «Моей волны»: {join_error}") from join_error
        with self._queue_lock:
            if int(state.get("manual_action_serial", 0)) != manual_serial:
                return {"verified": False, "cancelled": True, "superseded": True, "non_invasive": True}
        return self._begin_personal_source(
            profile, entries, mode="wave", title="Моя волна",
            requester=requester or str(profile.get("discord_user_id", "") or ""),
            source_data={"batch_id": str(wave.get("batch_id", "") or "")},
            expected_manual_serial=manual_serial,
        )

    @latest_transport
    def music_playlist(self, playlist: str, server: str = "", *, requester: str = "") -> dict[str, Any]:
        name = str(playlist or "").strip()
        if not name:
            raise ValueError("Не указано название плейлиста для Discord")
        remote = self._remote_for_any_profile(server)
        profile = self._background_profile(server)
        if remote:
            remote_profile, host = remote
            who = requester or str(remote_profile.get("discord_user_id", "") or "")
            return self._remote_request(remote_profile, host, "playlist", {"playlist": name, "requester": who}, timeout=18.0)
        info = self._yandex.playlist_tracks(name)
        actual = str(info.get("title", "") or name)
        who = requester or str(profile.get("discord_user_id", "") or "")
        entries = [self._entry_from_track(track, requester=who) for track in list(info.get("tracks") or [])]
        transport_checkpoint(self)
        return self._begin_personal_source(
            profile, entries, mode="playlist", title=actual, requester=who,
            source_data={"kind": str(info.get("kind", "") or ""), "playlist_uuid": str(info.get("playlist_uuid", "") or "")},
        )

    def _select_single_source(self, profile):
        """Invalidate generated/restored tails before a new explicit play request."""
        state = self._queue_state(profile)
        with self._queue_lock:
            state["source_epoch"] = int(state.get("source_epoch", 0)) + 1
            if state.get("restored_queue") or state.get("source_mode") in {"wave", "playlist", "my_tracks"}:
                state["items"] = []
            state.update(source_mode="single", source_title="", source_data={}, restored_queue=False,
                         wave_background_refill_active=False, manual_guard_until=time.monotonic()+20)
        return state

    @latest_transport
    def music_bot_play(self, query: str, server: str = "", channel: str = "") -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("Не указана музыка для Discord")
        youtube_url = standalone_youtube_url(query)
        if youtube_url:
            return self.music_youtube_url(youtube_url, server=server, play_now=True)
        if not self._background_enabled():
            raise RuntimeError("Официальный Discord Bot отключён в настройках; desktop/UI fallback удалён.")
        try:
            # A normal play request belongs to the current shared host. Unlike
            # personalized My Wave/playlist it does not move voice ownership.
            remote = self._remote_for_any_profile(server)
            if remote:
                profile, host = remote
                return self._remote_request(profile, host, "play", {"query": query}, timeout=12.0)

            explicit_server = bool(str(server or "").strip())
            last_error: Exception | None = None
            selected_profile: dict[str, Any] | None = None
            data: dict[str, Any] | None = None
            for profile in self._candidate_profiles(server):
                try:
                    self._select_single_source(profile)
                    data = self._play_query(query, profile, channel)
                    selected_profile = profile
                    break
                except Exception as candidate_exc:
                    last_error = candidate_exc
                    if explicit_server or not self._profile_retryable_error(candidate_exc):
                        raise
                    logger.info(
                        "Discord profile %r is not the active/usable voice destination; trying the next configured server: %s",
                        profile.get("name", ""), candidate_exc,
                    )
            if data is None or selected_profile is None:
                raise last_error or RuntimeError("Не найден доступный Discord-сервер с NEO Bot")
            profile = selected_profile
            state = self._queue_state(profile)
            with self._queue_lock:
                state["transport_faulted"] = False
                state["transport_fault_reason"] = ""
                state["manual_guard_until"] = 0
                # Any explicit song cancels a continuous/personal source and its
                # generated remainder. Manual queue items are intentionally kept
                # only in ordinary queue mode.
                if str(state.get("source_mode", "") or "") in {"wave", "playlist", "my_tracks"}:
                    state["items"] = []
                    state["source_mode"] = ""
                    state["source_title"] = ""
                    state["source_data"] = {}
                state["generation"] = int(state.get("generation", 0)) + 1
                generation = int(state["generation"])
                state["current"] = {
                    "query": query,
                    "track_id": str(data.get("track_id", "") or ""),
                    "title": str(data.get("title", "") or query),
                    "artist": str(data.get("artist", "") or ""),
                    "added_by": str(profile.get("discord_user_id", "") or ""),
                    "generation": generation, "started_at": time.monotonic(), "seen_active": False,
                    "_transport_generation": int(data.get("playback_generation", 0) or 0),
                    "_expected_duration_ms": int(data.get("expected_duration_ms", 0) or 0),
                    "_retry_count": 0,
                }
            self._host_cache.pop(self._profile_key(profile), None)
            self._start_queue_monitor(profile)
            self._broadcast_queue_snapshot(profile, reanchor=False)
            return data
        except DiscordPlaybackCancelled:
            raise
        except Exception as exc:
            logger.warning("Discord background music path failed: %s", exc)
            raise RuntimeError(
                f"Фоновое воспроизведение Discord не выполнено: {exc}. "
                "NEO не переключал Discord, не двигал мышь и не отправлял клавиши."
            ) from exc

    def _play_now_guard188(self, server: str, ack: dict) -> Any:
        """HIST-001: what a «play now» skip must still find first in the queue.

        Local queue → the exact entry object (checked under the queue lock by music_skip);
        a shared host → True only when its first item is the acknowledged track, else False.
        """
        try:
            profile = self._background_profile(server)
            entry = (self.__dict__.get("_play_now_entries188") or {}).pop(self._profile_key(profile), None)
        except Exception:
            entry = None
        if entry is not None:
            return entry
        item = dict(ack.get("item") or {})
        try:
            status = self.music_queue_status(server)
        except Exception:
            return True                     # no evidence either way: the established behaviour stays
        rows = status.get("queue") or status.get("items") or []
        front = dict(rows[0]) if rows and isinstance(rows[0], dict) else {}
        if not front or not item:
            return True
        if item.get("track_id") and front.get("track_id"):
            return str(front["track_id"]) == str(item["track_id"])
        if item.get("title") and front.get("title"):
            return str(front["title"]).casefold() == str(item["title"]).casefold()
        return True

    def music_queue_add(self, query: str, server: str = "", *, next_up: bool = False, requester: str = "") -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("Не указана песня для очереди Discord")
        youtube_url = standalone_youtube_url(query)
        if youtube_url:
            return self.music_youtube_url(youtube_url, server=server, requester=requester, next_up=next_up)
        remote = self._remote_for_any_profile(server)
        if remote:
            profile, host = remote
            who = requester or str(profile.get("discord_user_id", "") or "")
            return self._remote_request(
                profile, host, "queue_next" if next_up else "queue_add",
                {"query": query, "requester": who}, timeout=12.0,
            )
        profile = self._background_profile(server)
        state = self._queue_state(profile)
        who = requester or str(profile.get("discord_user_id", "") or "")
        with self._queue_lock:
            current = state.get("current")
        if not current:
            data = self.music_bot_play(query, server=server)
            return {**data, "queue_started_immediately": True, "queued": False}
        entry = self._queue_entry(query, requester=who)
        with self._queue_lock:
            items = state.setdefault("items", [])
            # Manual requests during My Wave are priority inserts, not a source
            # replacement. The requested song plays next, then Rotor continues
            # from the preserved wave queue/refill state.
            wave_active = str(state.get("source_mode", "") or "") == "wave"
            if next_up:
                entry["_force_next"] = True
                items.insert(0, entry)
                position = 1
                # HIST-001: «play now» may skip only to THIS entry (identity, not title).
                self.__dict__.setdefault("_play_now_entries188", {})[self._profile_key(profile)] = entry
            elif wave_active:
                entry["_force_next"] = True
                insert_at = 0
                while insert_at < len(items) and bool(items[insert_at].get("_force_next")):
                    insert_at += 1
                items.insert(insert_at, entry)
                position = insert_at + 1
            else:
                items.append(entry)
                if bool(state.get("shuffle")):
                    self._shuffle_pending_items(state)
                    items = state.setdefault("items", [])
                    position = next((idx + 1 for idx, item in enumerate(items) if item is entry), len(items))
                else:
                    position = len(items)
            snapshot = [self._public_queue_entry(item) for item in items]
        self._start_queue_monitor(profile)
        self._broadcast_queue_snapshot(profile, reanchor=False)
        return {
            "server": profile.get("name", ""), "queued": True, "next_up": next_up,
            "queue_started_immediately": False, "ack_contract": 1,
            "position": position, "item": self._public_queue_entry(entry), "queue": snapshot,
            "verified": True, "submitted": True, "method": "discord_music_queue", "non_invasive": True,
            "shared_queue": bool(self.config.get("integrations.discord.shared_music_queue_enabled", True)),
        }

    def _display_queue_entry(self, entry: dict[str, Any] | None) -> dict[str, Any]:
        """Return a user-facing queue item and resolve lightweight metadata lazily.

        Large Yandex libraries intentionally keep TrackShort objects in memory.
        Their title can temporarily be only a numeric id. Queue/status views show
        only a handful of items, so materialize those items here instead of doing
        hundreds of network calls while building the queue.
        """
        raw = dict(entry or {})
        public = self._public_queue_entry(raw)
        title = str(public.get("title", "") or "").strip()
        if title and not self._queue_title_is_placeholder(raw, title):
            return public
        track_obj = raw.get("_track_obj")
        if track_obj is None:
            return public
        try:
            materialize = getattr(self._yandex, "_materialize_track", None)
            full = materialize(track_obj) if callable(materialize) else track_obj
            enriched = self._entry_from_track(
                full, query=str(raw.get("query", "") or ""), requester=str(raw.get("added_by", "") or "")
            )
            for key in ("media_source", "source_title", "source_mode", "webpage_url", "kind"):
                if raw.get(key) not in (None, ""):
                    enriched[key] = raw.get(key)
            result = self._public_queue_entry(enriched)
            if str(result.get("title", "") or "").strip() and not self._looks_internal_media_value(str(result.get("title", ""))):
                return result
        except Exception as exc:
            logger.debug("Queue display metadata materialization failed: %s", exc)
        return public

    def _media_playback_scope(self, profile: dict[str, Any]) -> str:
        state = self._queue_state(profile)
        with self._queue_lock:
            epoch = int(state.get("source_epoch", 0))
        return f"{self._instance_id}:{self._profile_key(profile)}:{epoch}"

    def _local_queue_snapshot(self, profile: dict[str, Any], *, _publication_ticket: dict[str, Any] | None = None) -> dict[str, Any]:
        # Queue ownership cannot stay locked while an IPC/status read waits. Read
        # optimistically and validate it after the transport sample; a crossing
        # Next/source change must never label the old queue with the new track.
        def entry_stamp(entry):
            row = entry or {}
            return tuple(str(row.get(key, "") or "") for key in (
                "track_id", "title", "artist", "query", "generation",
                "_transport_generation", "starting", "media_source"))

        def state_stamp(state):
            return (int(state.get("source_epoch", 0)), int(state.get("generation", 0)),
                    int(state.get("manual_action_serial", 0)), entry_stamp(state.get("current")),
                    tuple(entry_stamp(row) for row in state.get("items", []) if isinstance(row, dict)),
                    str(state.get("source_mode", "") or ""), str(state.get("source_title", "") or ""),
                    bool(state.get("shuffle")), str(state.get("repeat_mode", "off") or "off"))

        last_sample: dict[str, Any] = {}
        for _attempt in range(2):
            with self._queue_lock:
                if _publication_ticket is not None and (
                        not self._snapshot_ticket_current(self._profile_key(profile), _publication_ticket)
                        or not self._snapshot_profile_current(profile)):
                    return {"shared_state_pending": True}
                state = self._queue_state(profile)
                stamp = state_stamp(state)
                raw_current = dict(state.get("current") or {})
                raw_items = [dict(item) for item in state.get("items", []) if isinstance(item, dict)]
                raw_history = [dict(item) for item in list(state.get("history") or [])[-5:] if isinstance(item, dict)]
                source_mode = str(state.get("source_mode", "") or "")
                source_title = str(state.get("source_title", "") or "")
                shuffle_enabled = bool(state.get("shuffle"))
                repeat_mode = str(state.get("repeat_mode", "off") or "off").casefold()
                transport_faulted = bool(state.get("transport_faulted"))
                transport_fault_reason = str(state.get("transport_fault_reason", "") or "")
                scope = f"{self._instance_id}:{self._profile_key(profile)}:{int(state.get('source_epoch', 0))}"
            try:
                playback = self._background_for_profile(profile).playback_status(profile)
            except Exception:
                playback = {}
            last_sample = dict(playback) if isinstance(playback, dict) else {}
            with self._queue_lock:
                if _publication_ticket is not None and (
                        not self._snapshot_ticket_current(self._profile_key(profile), _publication_ticket)
                        or not self._snapshot_profile_current(profile)):
                    return {"shared_state_pending": True}
                if state_stamp(self._queue_state(profile)) != stamp:
                    continue
            generation = int(playback.get("playback_generation", 0) or 0)
            expected_generation = int(raw_current.get("_transport_generation", 0) or 0)
            if raw_current.get("starting") or (generation and expected_generation and generation != expected_generation):
                continue
            playback_current = dict(playback.get("current") or {}) if isinstance(playback.get("current"), dict) else {}
            raw_title = self._prefer_media_title(raw_current.get("title", ""), raw_current.get("query", ""))
            transport_title = self._prefer_media_title(playback_current.get("title", ""), playback.get("title", ""))
            transport_active = bool(playback.get("playing")) or bool(playback.get("paused"))
            raw_id = str(raw_current.get("track_id", "") or "")
            transport_id = str(playback_current.get("track_id", "") or "")
            if transport_active and ((raw_id and transport_id and raw_id != transport_id)
                    or (not generation or not expected_generation) and (not raw_id or not transport_id)
                    and is_player_facing_media_title(raw_title)
                    and transport_title and raw_title != transport_title):
                continue
            # Resolve placeholders only after proving queue and transport belong
            # to the same sampled playback. Metadata recovery cannot splice queues.
            if transport_title and (transport_active or not raw_title or not is_player_facing_media_title(raw_title)):
                raw_current.update({
                    "title": transport_title,
                    "artist": str(playback_current.get("artist", "") or playback.get("artist", "") or raw_current.get("artist", "") or ""),
                    "track_id": transport_id or raw_id,
                })
            # Absence is not an unresolved track. Formatting an empty mapping
            # creates a truthy metadata placeholder and can incorrectly bypass
            # remote probe/terminal-outcome handling as if local media existed.
            current = self._display_queue_entry(raw_current) if raw_current else {}
            items = [self._display_queue_entry(item) if idx < 7 else self._public_queue_entry(item) for idx, item in enumerate(raw_items)]
            levels, levels_real = media_levels("discord", max_age=0.9, width=96)
            history_items = [self._display_queue_entry(item) for item in raw_history]
            return {
                "server": profile.get("name", ""), "current": current, "queue": items,
                "guild_id": str(profile.get("guild_id", "") or ""),
                "playback_scope": scope, "playback_generation": generation,
                "queue_length": len(items), "history": history_items, "history_length": len(history_items),
                "source_mode": source_mode, "source_title": source_title,
                "shuffle": shuffle_enabled, "repeat_mode": repeat_mode,
                "transport_faulted": transport_faulted, "transport_fault_reason": transport_fault_reason,
                "playing": bool(playback.get("playing")), "paused": bool(playback.get("paused")),
                "connected": bool(playback.get("connected")),
                "levels": levels, "audio_levels_real": bool(levels_real),
                "audio_levels_kind": "spectrum" if levels_real else "",
                "verified": True, "method": "discord_music_queue", "non_invasive": True,
                "shared_queue": bool(self.config.get("integrations.discord.shared_music_queue_enabled", True)),
            }
        return {
            "server": profile.get("name", ""), "guild_id": str(profile.get("guild_id", "") or ""),
            "current": {}, "queue": [], "queue_length": 0, "shared_state_pending": True,
            "playing": False, "paused": False, "verified": False,
            # NEO 187 (REC-DPN-02): queue metadata is still settling, but the
            # transport sample taken during this very call is reported apart
            # so "pending" is never mistaken for "disconnected".
            "transport_state": "pending",
            "transport_connected": bool(last_sample.get("connected")),
            "method": "discord_music_queue_pending", "non_invasive": True,
        }

    def music_queue_status(self, server: str = "") -> dict[str, Any]:
        # Fast path: a passive state broadcast is authoritative enough for UI,
        # Telegram queue display and preference buttons. Do not make read-only
        # status depend on a request/ACK round trip.
        cached = self._cached_shared_snapshot(server, max_age=10 * 60)
        if cached is not None:
            profile, snapshot = cached
            # If this process owns the actual VoiceClient, prefer its live state.
            try:
                local_playback = self._background_for_profile(profile).playback_status(profile)
            except Exception:
                local_playback = {}
            if not bool(local_playback.get("connected")):
                self._request_queue_snapshot(profile)
                return self._snapshot_terminal_result(server, {**snapshot, "cached_shared_snapshot": True})

        profile = self._background_profile(server)
        try:
            local = self._local_queue_snapshot(profile)
            if local.get("shared_state_pending"):
                return self._snapshot_terminal_result(server, local)
            if bool(local.get("connected")) or local.get("current"):
                return local
        except Exception:
            local = {}

        # Cold-start fallback: discover the host once. If its ACK is lost, return
        # any cached broadcast instead of surfacing a Telegram error.
        try:
            remote = self._remote_for_any_profile(server)
            if remote:
                remote_profile, host = remote
                try:
                    return self._remote_request(remote_profile, host, "queue_status", timeout=1.8)
                except Exception:
                    cached = self._cached_shared_snapshot(server, max_age=30 * 60)
                    if cached is not None:
                        self._request_queue_snapshot(cached[0])
                        return self._snapshot_terminal_result(server, {**cached[1], "stale_shared_snapshot": True})
                    self._request_queue_snapshot(remote_profile)
                    waited = self._wait_cached_shared_snapshot(server, timeout=0.65)
                    if waited is not None:
                        return {**waited[1], "shared_snapshot_after_probe": True}
                    return self._snapshot_terminal_result(server, {
                        "server": remote_profile.get("name", ""), "current": {}, "queue": [], "queue_length": 0,
                        "playing": False, "paused": False, "verified": True, "shared_queue": True,
                        "shared_state_pending": True, "method": "discord_shared_queue_probe_pending",
                    })
        except Exception:
            cached = self._cached_shared_snapshot(server, max_age=30 * 60)
            if cached is not None:
                self._request_queue_snapshot(cached[0])
                return self._snapshot_terminal_result(server, {**cached[1], "stale_shared_snapshot": True})
            for candidate in self._candidate_profiles(server):
                self._request_queue_snapshot(candidate)
            waited = self._wait_cached_shared_snapshot(server, timeout=0.65)
            if waited is not None:
                return {**waited[1], "shared_snapshot_after_probe": True}
            return self._snapshot_terminal_result(server, {
                "server": str(server or ""), "current": {}, "queue": [], "queue_length": 0,
                "playing": False, "paused": False, "verified": True, "shared_queue": True,
                "shared_state_pending": True, "method": "discord_shared_queue_probe_pending",
            })
        return self._snapshot_terminal_result(server, local or self._local_queue_snapshot(profile))

    def music_shuffle(self, server: str = "", enabled: bool = True) -> dict[str, Any]:
        remote = self._remote_for_any_profile(server)
        if remote:
            profile, host = remote
            return self._remote_request(profile, host, "shuffle_on" if enabled else "shuffle_off", timeout=4.0)
        profile = self._background_profile(server)
        state = self._queue_state(profile)
        value = bool(enabled)
        randomized = False
        preview: list[str] = []
        with self._queue_lock:
            state["shuffle"] = value
            if value:
                randomized = self._shuffle_pending_items(state)
            queue_length = len(state.get("items", []) or [])
            current = self._public_queue_entry(state.get("current") or {})
            preview = [str((item or {}).get("title") or (item or {}).get("query") or "") for item in (state.get("items") or [])[:6]]
        try:
            integrations = self.config.data.setdefault("integrations", {})
            integrations.setdefault("discord", {})["music_shuffle_enabled"] = value
            self.config.save(self.config.data)
        except Exception as exc:
            logger.debug("Discord shuffle state persistence failed: %s", exc)
        if value:
            self._prefetch_next_queue_item(state)
        logger.info(
            "Discord shuffle %s for %s; pending=%d randomized=%s preview=%s",
            "enabled" if value else "disabled", profile.get("name", ""), queue_length, randomized, preview,
        )
        return {
            "server": profile.get("name", ""), "shuffle": value, "queue_length": queue_length,
            "queue_randomized": randomized,
            "current": current, "verified": True, "method": "discord_music_queue_shuffle_bag",
            "non_invasive": True,
            "shared_queue": bool(self.config.get("integrations.discord.shared_music_queue_enabled", True)),
        }

    def music_repeat(self, server: str = "", mode: str = "off") -> dict[str, Any]:
        requested = str(mode or "off").strip().casefold()
        aliases = {"on": "track", "one": "track", "song": "track", "context": "playlist", "queue": "playlist", "none": "off", "false": "off"}
        requested = aliases.get(requested, requested)
        if requested not in {"off", "track", "playlist"}:
            raise ValueError("Режим повтора Discord должен быть: off, track или playlist")
        remote = self._remote_for_any_profile(server)
        if remote:
            profile, host = remote
            return self._remote_request(profile, host, {"track": "repeat_track", "playlist": "repeat_playlist", "off": "repeat_off"}[requested], timeout=4.0)
        profile = self._background_profile(server)
        state = self._queue_state(profile)
        with self._queue_lock:
            state["repeat_mode"] = requested
            queue_length = len(state.get("items", []) or [])
            current = self._public_queue_entry(state.get("current") or {})
        try:
            integrations = self.config.data.setdefault("integrations", {})
            integrations.setdefault("discord", {})["music_repeat_mode"] = requested
            self.config.save(self.config.data)
        except Exception as exc:
            logger.debug("Discord repeat state persistence failed: %s", exc)
        self._broadcast_queue_snapshot(profile, reanchor=False)
        return {
            "server": profile.get("name", ""), "repeat_mode": requested,
            "repeat_track": requested == "track", "repeat_playlist": requested == "playlist",
            "queue_length": queue_length, "current": current, "verified": True,
            "method": "discord_music_queue_repeat_mode", "non_invasive": True,
            "shared_queue": bool(self.config.get("integrations.discord.shared_music_queue_enabled", True)),
        }

    def _current_track_for_local_preference(self, server: str = "") -> tuple[dict[str, Any], str]:
        # Like/dislike mutates THIS user's Yandex account. A control-bus ACK from
        # the Discord host is not part of that transaction, so resolve the track
        # from the passive snapshot first and only use host discovery as a cold
        # fallback.
        current: dict[str, Any] = {}
        profile = self._background_profile(server)
        try:
            local = self._local_queue_snapshot(profile)
            # A player-facing current track is useful even while VoiceClient is
            # reconnecting. Requiring `connected` made Telegram preference
            # actions lose a track that the desktop HUD already knew about.
            candidate = dict(local.get("current") or {})
            if is_player_facing_media_title(str(candidate.get("title", "") or "")):
                current = candidate
        except Exception:
            pass

        if not current:
            try:
                playback = self._background_for_profile(profile).playback_status(profile)
                candidate = dict(playback.get("current") or {}) if isinstance(playback.get("current"), dict) else {}
                if not candidate and is_player_facing_media_title(str(playback.get("title", "") or "")):
                    candidate = {"title": playback.get("title", ""), "artist": playback.get("artist", ""), "track_id": playback.get("track_id", "")}
                if is_player_facing_media_title(str(candidate.get("title", "") or "")):
                    current = candidate
            except Exception:
                pass

        if not current:
            event = latest_now_playing("discord", max_age_seconds=2 * 60 * 60)
            if event is not None:
                current = {"title": event.title, "artist": event.artist, "track_id": event.track_id}

        if not current:
            cached = self._cached_shared_snapshot(server, max_age=30 * 60)
            if cached is not None:
                current = dict((cached[1] or {}).get("current") or {})

        if not current:
            # Fire a passive probe immediately; do not wait for its ACK. A short
            # direct status attempt is retained only for the very first click on a
            # completely cold installation.
            for profile in self._candidate_profiles(server):
                self._request_queue_snapshot(profile)
            waited = self._wait_cached_shared_snapshot(server, timeout=0.65)
            if waited is not None:
                current = dict((waited[1] or {}).get("current") or {})
            try:
                remote = self._remote_for_any_profile(server) if not current else None
                if remote:
                    try:
                        status = self._remote_request(remote[0], remote[1], "queue_status", timeout=1.2)
                        current = dict(status.get("current") or {})
                    except Exception:
                        pass
            except Exception:
                pass

        if not current:
            raise RuntimeError(
                "NEO пока не получил состояние текущего Discord-трека. "
                "Откройте панель Discord Music ещё раз через секунду — запрос состояния уже отправлен."
            )
        if str(current.get("media_source", "") or "").casefold() == "youtube":
            raise RuntimeError("Лайк/дизлайк Яндекс Музыки недоступен для YouTube-аудио в Discord")
        track_id = str(current.get("track_id", "") or "").strip()
        if not track_id:
            query = " ".join(
                x for x in (str(current.get("title", "") or ""), str(current.get("artist", "") or "")) if x
            ).strip()
            resolved = self._yandex.resolve_track(query) if query else None
            track_id = str((resolved or {}).get("id", "") or "").strip()
        if not track_id:
            raise RuntimeError("Не удалось сопоставить текущий Discord-трек с Яндекс Музыкой")
        return current, track_id

    def music_like(self, server: str = "") -> dict[str, Any]:
        current, track_id = self._current_track_for_local_preference(server)
        pref = self._yandex.set_track_preference(track_id, "like")
        return {
            "server": str(server or ""), "current": current, "track_id": track_id,
            "title": str(current.get("title", "") or ""), "artist": str(current.get("artist", "") or ""),
            "liked": True, "verified": bool(pref.get("verified", False)),
            "method": "discord_current_track_local_yandex_like", "non_invasive": True,
            "local_yandex_account": True,
        }

    def music_dislike(self, server: str = "") -> dict[str, Any]:
        current, track_id = self._current_track_for_local_preference(server)
        # The preference belongs to THIS NEO user's Yandex account and is the
        # primary operation. A transient shared-queue ACK failure must not turn a
        # successfully written dislike into a Telegram error.
        pref = self._yandex.set_track_preference(track_id, "dislike")
        skipped: dict[str, Any] = {}
        skip_error = ""
        try:
            skipped = self.music_skip(server)
        except Exception as exc:
            skip_error = str(exc)
            logger.warning("Discord dislike was saved but shared skip failed: %s", exc)
        new_current = dict(skipped.get("current") or {}) if isinstance(skipped, dict) else {}
        if not new_current and isinstance(skipped, dict) and skipped.get("title"):
            new_current = {"title": skipped.get("title", ""), "artist": skipped.get("artist", ""), "track_id": skipped.get("track_id", "")}
        return {
            **(skipped if isinstance(skipped, dict) else {}),
            "previous": current, "disliked_track_id": track_id, "disliked": True,
            "preference_verified": bool(pref.get("verified", False)),
            "skip_error": skip_error, "skip_verified": not bool(skip_error),
            "verified": bool(pref.get("verified", False)),
            "method": "discord_current_track_local_yandex_dislike_then_best_effort_skip",
            "non_invasive": True, "local_yandex_account": True,
            "current": new_current or dict((skipped or {}).get("current") or {}),
        }

    def music_queue_clear(self, server: str = "") -> dict[str, Any]:
        remote = self._remote_for_any_profile(server)
        if remote:
            profile, host = remote
            return self._remote_request(profile, host, "queue_clear", timeout=4.0)
        profile = self._background_profile(server)
        state = self._queue_state(profile)
        with self._queue_lock:
            removed = len(state.get("items", []) or [])
            state["queue_clear_serial"] = int(state.get("queue_clear_serial", 0)) + 1
            state["items"] = []
            state["source_mode"] = ""
            state["source_title"] = ""
            state["source_data"] = {}
        # Keep #neo-music and passive multi-PC observers authoritative after a
        # voice/Telegram clear command, not only after dashboard-button clicks.
        self._broadcast_queue_snapshot(profile, reanchor=False)
        return {
            "server": profile.get("name", ""), "removed": removed, "current_continues": bool(state.get("current")),
            "verified": True, "method": "discord_music_queue", "non_invasive": True,
            "shared_queue": bool(self.config.get("integrations.discord.shared_music_queue_enabled", True)),
        }

    def _local_voice_is_connected(self, profile: dict[str, Any]) -> bool:
        """Cheap authoritative host check used by hot transport commands."""
        try:
            status = self._background_for_profile(profile).playback_status(profile)
            return bool(status.get("connected"))
        except Exception:
            return False

    @staticmethod
    def _restore_skipped_items(
        state: dict[str, Any], original: list[dict[str, Any]],
        removed: list[tuple[int, dict[str, Any]]], clear_serial: int,
    ) -> None:
        """Undo this pop under _queue_lock without undoing later queue edits."""
        if int(state.get("queue_clear_serial", 0)) != clear_serial:
            return
        # Index tokens distinguish repeated references as well as equal songs.
        # Add can replace the list, so only surviving entry occurrences anchor
        # restoration; the live list and its new entries remain authoritative.
        removed_indexes = {index for index, _ in removed}
        survivors: dict[int, list[int]] = {}
        for index, item in enumerate(original):
            if index not in removed_indexes:
                survivors.setdefault(id(item), []).append(index)
        items = []
        for item in state.get("items") or []:
            tokens = survivors.get(id(item)) or []
            items.append((tokens.pop(0) if tokens else None, item))
        for index, item in reversed(removed):
            following = min((token for token, _ in items if token is not None and token > index), default=None)
            preceding = max((token for token, _ in items if token is not None and token < index), default=None)
            if following is not None:
                insert_at = next(i for i, (token, _) in enumerate(items) if token == following)
            elif preceding is not None:
                insert_at = next(i + 1 for i, (token, _) in enumerate(items) if token == preceding)
            else:
                # No old anchor remains: new play-next requests stay ahead,
                # while ordinary additions remain after the restored entries.
                insert_at = 0
                while insert_at < len(items) and bool(items[insert_at][1].get("_force_next")):
                    insert_at += 1
            items.insert(insert_at, (index, item))
        state["items"] = [item for _, item in items]

    @latest_transport
    def music_skip(self, server: str = "", *, expect_front: Any = None) -> dict[str, Any]:
        profile = self._background_profile(server)
        # Hot path: if this process owns the VoiceClient, never pay for shared-host
        # discovery.  Next must feel like a media key, not a network RPC.
        if not self._local_voice_is_connected(profile):
            remote = self._remote_for_any_profile(server)
            if remote:
                remote_profile, host = remote
                return self._remote_request(remote_profile, host, "skip", timeout=1.4)
        key = self._profile_key(profile)
        state = self._queue_state(profile)
        with self._queue_lock:
            original_history = [dict(x) for x in state.get("history", [])]
            original_current = dict(state.get("current") or {})
            original_manual_paused = bool(state.get("manual_paused"))
            original_pause_control_serial = int(state.get("pause_control_serial", 0))
            state["manual_paused"] = False
            # A manual skip is an explicit recovery attempt after a previous
            # transport circuit-breaker stop.
            state["transport_faulted"] = False
            state["transport_fault_reason"] = ""
        # Preparation is transactional: the existing song stays audible until
        # the new source reaches the guarded bot transport commit.
        stopped_previous = False
        last_error: Exception | None = None
        while True:
            with self._queue_lock:
                state = self._queues.get(key) or state
                items = state.get("items") or []
                if expect_front is not None:
                    # HIST-001: a «play now» skip is valid only while its own entry is still
                    # first; checked under the same lock that pops it, so Next/Stop/another
                    # request in between can never make it skip someone else's song.
                    if not items or items[0] is not expect_front:
                        state["manual_paused"] = original_manual_paused
                        return {"server": profile.get("name", ""), "skipped": False, "queue_changed": True,
                                "verified": False, "method": "discord_music_queue", "non_invasive": True}
                    expect_front = None
                if not items:
                    # My Wave is infinite: a manual skip at a batch boundary should
                    # request a new personalized batch rather than stopping.
                    if str(state.get("source_mode", "") or "") == "wave":
                        old_current = dict(state.get("current") or {})
                        if old_current and not old_current.get("starting"):
                            self._push_history(state, old_current)
                        state["current"] = None
                        break
                    state["current"] = None
                    state["source_mode"] = ""
                    state["source_title"] = ""
                    state["source_data"] = {}
                    stopped = True
                    try:
                        self._background_for_profile(profile).voice_action(profile, "stop")
                    except Exception as exc:
                        # The stop itself is unconfirmed: never report verified.
                        stopped = False
                        last_error = last_error or exc
                    return {
                        "server": profile.get("name", ""), "skipped": True, "queue_empty": True,
                        "verified": stopped, "method": "discord_music_queue", "non_invasive": True,
                        "last_error": str(last_error or ""),
                    }
                old_current = dict(state.get("current") or {})
                original_items = list(items)
                clear_serial = int(state.get("queue_clear_serial", 0))
                removed_items = []
                entry = self._pop_next_queue_entry(state, removed=removed_items)
                if entry is None:
                    continue
                state["generation"] = int(state.get("generation", 0)) + 1
                generation = int(state["generation"])
                entry["_manual_start_serial"] = int(state.get("manual_action_serial", 0))
                entry["_manual_start_epoch"] = int(state.get("source_epoch", 0))
                state["manual_guard_until"] = time.monotonic() + 2.4
                state["current"] = {**entry, "generation": generation, "started_at": time.monotonic(), "seen_active": False, "starting": True}
            try:
                # Audible reaction first: stop the old Discord transport immediately
                # while the prefetched source is selected. If prefetch is already
                # warm, the new FFmpeg source follows almost at once.
                self._prefetch_queue_entry(entry)
                data = self._play_queue_entry(entry, profile, fast_transition=True)
                with self._queue_lock:
                    state = self._queues.get(key)
                    if state and int(((state.get("current") or {}).get("generation", -1))) == generation:
                        if old_current and not old_current.get("starting"):
                            self._push_history(state, old_current)
                        state["current"] = {
                            **entry, "track_id": str(data.get("track_id", "") or entry.get("track_id", "")),
                            "title": self._prefer_media_title(data.get("title", ""), entry.get("title", ""), entry.get("query", "")),
                            "artist": str(data.get("artist", "") or entry.get("artist", "")),
                            "generation": generation, "started_at": time.monotonic(), "seen_active": False,
                            "_transport_generation": int(data.get("playback_generation", 0) or 0),
                            "_expected_duration_ms": int(data.get("expected_duration_ms", 0) or entry.get("_expected_duration_ms", 0) or 0),
                            "_retry_count": int(entry.get("_retry_count", 0) or 0),
                        }
                        state["current"].pop("_request_serial", None)
                        state["current"].pop("_manual_start_serial", None)
                        state["current"].pop("_manual_start_epoch", None)
                        state["current"].pop("_pause_started_at", None)
                        state["current"].pop("_paused_seconds", None)
                        if str(state.get("source_mode", "") or "") == "wave":
                            tid = str(state["current"].get("track_id", "") or "")
                            recent = state.setdefault("source_data", {}).setdefault("recent_ids", [])
                            if tid: recent[:] = ([x for x in recent if x != tid] + [tid])[-300:]
                            self._remember_wave_id(
                                tid, title=str(state["current"].get("title", "") or ""),
                                artist=str(state["current"].get("artist", "") or ""),
                            )
                            if not stopped_previous:
                                self._wave_event(state, original_current, "skip")
                            self._wave_event(state, state["current"], "trackStarted")
                # REC-DISCORD-POSTCOMMIT-MONITOR-ROLLBACK-01: the transport has
                # acknowledged B and RAM is committed. A late auxiliary failure
                # (monitor, prefetch, snapshot, broadcast) is a degradation to
                # report, never a reason to restore A/B,C in RAM.
                return self._skip_postcommit(profile, state, data)
            except DiscordPlaybackCancelled:
                with self._queue_lock:
                    live = self._queues.get(key) or {}
                    if int((live.get("current") or {}).get("generation", -1)) == generation:
                        live["current"] = original_current or None
                        self._restore_skipped_items(live, original_items, removed_items, clear_serial)
                        live["history"] = original_history
                        # A later Pause/Resume owns its flag even if the current generation is unchanged.
                        if int(live.get("pause_control_serial", 0)) == original_pause_control_serial:
                            live["manual_paused"] = original_manual_paused
                raise
            except Exception as exc:
                last_error = exc
                logger.warning("Discord skip could not start queued track %r: %s", entry.get("query"), exc)
                transport_failure = self._is_transport_failure(exc) or isinstance(exc, (ConnectionError, TimeoutError))
                with self._queue_lock:
                    state = self._queues.get(key)
                    if state and int(((state.get("current") or {}).get("generation", -1))) == generation:
                        state["current"] = original_current or None
                        self._restore_skipped_items(state, original_items, removed_items, clear_serial)
                        state["history"] = original_history
                        # A later Pause/Resume owns its flag even if the current generation is unchanged.
                        if int(state.get("pause_control_serial", 0)) == original_pause_control_serial:
                            state["manual_paused"] = original_manual_paused
                        state["transport_faulted"] = True
                        state["transport_fault_reason"] = str(exc)
                self._persist_queue(profile)
                error_type = TimeoutError if isinstance(exc, TimeoutError) else ConnectionError if isinstance(exc, ConnectionError) else RuntimeError
                raise error_type(
                    "Discord не подтвердил переключение; очередь и история сохранены. "
                    f"Автоматический повтор не выполнялся. Причина: {exc}"
                ) from exc
        # Wave refill path after releasing the queue lock.
        self._advance_queue(key)
        with self._queue_lock:
            live = self._queues.get(key) or {}
            has_current = bool(live.get("current"))
            if live.get("current") and not live["current"].get("starting"):
                self._wave_event(live, original_current, "skip")
            current = self._public_queue_entry((self._queues.get(key) or {}).get("current") or {}) if has_current else {}
        self._broadcast_queue_snapshot(profile, reanchor=False)
        if not has_current:
            # REC-WAVE-DEFERRED-SKIP-SOURCE-01: the refill is still in its
            # retry pause, so no successor started. Accepted ≠ completed.
            return {
                "server": profile.get("name", ""), "skipped": True, "from_queue": True, "current": {},
                "source_mode": "wave", "verified": False, "pending": True, "queue_refilling": True,
                "method": "discord_music_queue", "non_invasive": True,
            }
        return {
            "server": profile.get("name", ""), "skipped": True, "from_queue": True,
            "title": current.get("title", ""), "artist": current.get("artist", ""), "current": current,
            "source_mode": "wave", "verified": True, "method": "discord_music_queue", "non_invasive": True,
        }

    def _skip_postcommit(self, profile: dict[str, Any], state: dict[str, Any] | None, data: dict[str, Any]) -> dict[str, Any]:
        degraded: list[str] = []

        def phase(name: str, step: Callable[[], Any]) -> Any:
            try:
                return step()
            except DiscordPlaybackCancelled:
                raise
            except Exception as exc:
                degraded.append(name)
                logger.warning("Discord skip committed; post-commit %s failed: %s", name, type(exc).__name__)
                return None

        phase("prefetch", lambda: self._prefetch_next_queue_item(state))
        if str((state or {}).get("source_mode", "") or "") == "wave":
            phase("wave_refill", lambda: self._ensure_wave_queue_depth_background(profile, 8))
        phase("monitor", lambda: self._start_queue_monitor(profile))
        snapshot = phase("snapshot", lambda: self._local_queue_snapshot(profile)) or {}
        phase("broadcast", lambda: self._broadcast_queue_snapshot(profile, reanchor=False))
        result = {
            **data, "skipped": True, "from_queue": True,
            "current": dict(snapshot.get("current") or {}),
            "queue": list(snapshot.get("queue") or [])[:8],
            "queue_length": int(snapshot.get("queue_length", 0) or 0),
            "history": list(snapshot.get("history") or [])[-5:],
        }
        if degraded:
            result["postcommit_degraded"] = degraded
        return result

    @latest_transport
    def music_previous(self, server: str = "") -> dict[str, Any]:
        profile = self._background_profile(server)
        if not self._local_voice_is_connected(profile):
            remote = self._remote_for_any_profile(server)
            if remote:
                remote_profile, host = remote
                return self._remote_request(remote_profile, host, "previous", timeout=1.4)
        key = self._profile_key(profile)
        state = self._queue_state(profile)
        with self._queue_lock:
            state = self._queues.get(key) or state
            history = state.setdefault("history", [])
            if not history:
                raise RuntimeError("В истории музыки Discord ещё нет предыдущего трека")
            original_history = [dict(x) for x in history]
            previous = dict(history.pop())
            original_manual_paused = bool(state.get("manual_paused"))
            original_pause_control_serial = int(state.get("pause_control_serial", 0))
            state["manual_paused"] = False
            previous["_manual_start_serial"] = int(state.get("manual_action_serial", 0))
            previous["_manual_start_epoch"] = int(state.get("source_epoch", 0))
            current = dict(state.get("current") or {})
            if current:
                state.setdefault("items", []).insert(0, current)
            state["generation"] = int(state.get("generation", 0)) + 1
            generation = int(state["generation"])
            state["manual_guard_until"] = time.monotonic() + 2.4
            state["current"] = {**previous, "generation": generation, "started_at": time.monotonic(), "seen_active": False, "starting": True}
        try:
            self._prefetch_queue_entry(previous)
            data = self._play_queue_entry(previous, profile, fast_transition=True)
        except DiscordPlaybackCancelled:
            with self._queue_lock:
                live = self._queues.get(key) or {}
                if int((live.get("current") or {}).get("generation", -1)) == generation:
                    live["current"] = current or None
                    live["items"] = [item for item in live.get("items", []) if item is not current]
                    live["history"] = original_history
                    # A later Pause/Resume owns its flag even if the current generation is unchanged.
                    if int(live.get("pause_control_serial", 0)) == original_pause_control_serial:
                        live["manual_paused"] = original_manual_paused
            raise
        except Exception:
            with self._queue_lock:
                live = self._queues.get(key) or state
                if int(((live.get("current") or {}).get("generation", -1))) == generation:
                    live["history"] = original_history
                    # A later Pause/Resume owns its flag even if the current generation is unchanged.
                    if int(live.get("pause_control_serial", 0)) == original_pause_control_serial:
                        live["manual_paused"] = original_manual_paused
                    live["items"] = [item for item in live.get("items", []) if item is not current]
                    live["current"] = current or None
                    live["transport_faulted"] = True
                    live["transport_fault_reason"] = "Previous track did not start; queue/history restored"
            self._persist_queue(profile)
            raise
        with self._queue_lock:
            live = self._queues.get(key) or state
            if int(((live.get("current") or {}).get("generation", -1))) == generation:
                live["current"] = {**previous,
                    "track_id": str(data.get("track_id", "") or previous.get("track_id", "")),
                    "title": str(data.get("title", "") or previous.get("title", previous.get("query", ""))),
                    "artist": str(data.get("artist", "") or previous.get("artist", "")),
                    "generation": generation, "started_at": time.monotonic(), "seen_active": False,
                    "_transport_generation": int(data.get("playback_generation", 0) or 0),
                    "_expected_duration_ms": int(data.get("expected_duration_ms", 0) or previous.get("_expected_duration_ms", 0) or 0),
                    "_retry_count": int(previous.get("_retry_count", 0) or 0)}
                live["current"].pop("_request_serial", None)
                live["current"].pop("_manual_start_serial", None)
                live["current"].pop("_manual_start_epoch", None)
                live["current"].pop("_pause_started_at", None)
                live["current"].pop("_paused_seconds", None)
                self._wave_event(live, current, "skip")
                self._wave_event(live, live["current"], "trackStarted")
        self._prefetch_next_queue_item(live)
        if str((live or {}).get("source_mode", "") or "") == "wave":
            self._ensure_wave_queue_depth_background(profile, 8)
        self._start_queue_monitor(profile)
        snapshot = self._local_queue_snapshot(profile)
        self._broadcast_queue_snapshot(profile, reanchor=False)
        return {
            **data, "previous": True, "from_history": True,
            "current": dict(snapshot.get("current") or {}),
            "queue": list(snapshot.get("queue") or [])[:8],
            "queue_length": int(snapshot.get("queue_length", 0) or 0),
            "history": list(snapshot.get("history") or [])[-5:],
        }

    def sync_timer_summaries(self, rows: list[str]) -> int:
        synced = 0
        failed = 0
        for profile in self._candidate_profiles(""):
            # Do not pre-filter by a guessed token location: profiles may inherit
            # the global Discord token from ConfigManager.  The background adapter
            # is the authoritative availability check.
            try:
                if self._background_for_profile(profile).set_timer_summaries(profile, list(rows or [])):
                    synced += 1
            except Exception:
                failed += 1
                logger.debug("Discord timer dashboard sync failed for %r", profile.get("name", ""), exc_info=True)
        if failed and not synced:
            # NEO 187: a known delivery failure is reported, so the caller does
            # not treat this timer set as already published.
            raise RuntimeError("Discord timer dashboard was not updated")
        return synced

    def music_set_volume(self, server: str = "", percent: int = 100) -> dict[str, Any]:
        profile = self._background_profile(server)
        return self._background_for_profile(profile).set_voice_volume(profile, max(0, min(100, int(percent))))

    def music_change_volume(self, server: str = "", delta: int = 0) -> dict[str, Any]:
        profile = self._background_profile(server)
        bot = self._background_for_profile(profile)
        raw_current = (bot.voice_volume(profile) or {}).get("volume", 100)
        current = int(100 if raw_current is None else raw_current)
        return bot.set_voice_volume(profile, max(0, min(100, current + int(delta))))

    def execute(self, action: str, **kwargs: Any) -> dict[str, Any]:
        action = str(action or "").strip().casefold()
        server = str(kwargs.get("server", "") or "")
        if action == "background_status":
            return self.background_status(server)
        if action == "music_bot_play":
            return self.music_bot_play(str(kwargs.get("query", "") or ""), server, str(kwargs.get("channel", "") or ""))
        if action == "music_youtube_url":
            return self.music_youtube_url(
                str(kwargs.get("query", "") or kwargs.get("url", "") or ""), server,
                requester=str(kwargs.get("requester", "") or ""),
                next_up=bool(kwargs.get("next_up", False)),
                play_now=bool(kwargs.get("play_now", False)),
            )
        if action == "music_my_tracks":
            return self.music_my_tracks(server, requester=str(kwargs.get("requester", "") or ""))
        if action == "music_my_wave":
            return self.music_my_wave(server, requester=str(kwargs.get("requester", "") or ""))
        if action == "music_playlist":
            return self.music_playlist(str(kwargs.get("playlist", "") or kwargs.get("query", "") or ""), server, requester=str(kwargs.get("requester", "") or ""))
        if action == "music_queue_next":
            return self.music_queue_add(str(kwargs.get("query", "") or ""), server, next_up=True, requester=str(kwargs.get("requester", "") or ""))
        if action == "music_queue_add":
            return self.music_queue_add(str(kwargs.get("query", "") or ""), server, next_up=False, requester=str(kwargs.get("requester", "") or ""))
        if action == "music_queue_status":
            return self.music_queue_status(server)
        if action == "music_queue_clear":
            return self.music_queue_clear(server)
        if action == "music_like":
            return self.music_like(server)
        if action == "music_dislike":
            return self.music_dislike(server)
        if action == "music_skip":
            return self.music_skip(server)
        if action == "music_previous":
            return self.music_previous(server)
        if action == "music_set_volume":
            value = kwargs.get("volume", 100)
            return self.music_set_volume(server, int(100 if value is None else value))
        if action == "music_volume_up":
            return self.music_change_volume(server, abs(int(kwargs.get("step", 10) or 10)))
        if action == "music_volume_down":
            return self.music_change_volume(server, -abs(int(kwargs.get("step", 10) or 10)))
        if action == "music_shuffle_on":
            return self.music_shuffle(server, True)
        if action == "music_shuffle_off":
            return self.music_shuffle(server, False)
        if action == "music_repeat_track":
            return self.music_repeat(server, "track")
        if action == "music_repeat_playlist":
            return self.music_repeat(server, "playlist")
        if action == "music_repeat_off":
            return self.music_repeat(server, "off")
        if action in {"music_pause", "music_resume", "music_stop", "bot_join_voice", "bot_leave_voice"}:
            mapped = {
                "music_pause": "pause", "music_resume": "resume", "music_stop": "stop",
                "bot_join_voice": "join", "bot_leave_voice": "leave",
            }[action]
            # Local-first transport.  V5.3 queried the shared bus even when this
            # process already owned the VoiceClient, adding latency and making a
            # transient bus miss turn a perfectly valid Stop into "unknown media".
            profile = self._background_profile(server)
            if action in {"music_pause", "music_resume", "music_stop"} and not self._local_voice_is_connected(profile):
                remote = self._remote_for_any_profile(server)
                if remote:
                    remote_profile, host = remote
                    return self._remote_request(remote_profile, host, mapped, timeout=1.3)
            cancel_transport_requests(self)
            state = self._queue_state(profile)
            if action == "bot_leave_voice":
                self.cancel_snapshot_session(profile)
            if action == "music_stop":
                self._suspend_snapshot_stop(profile, True)
            preserve_wave_stop = False
            with self._queue_lock:
                current_before = self._public_queue_entry(state.get("current") or {})
                state["manual_action_serial"] = int(state.get("manual_action_serial", 0)) + 1
                # Unlike the adapter-wide request serial, this token belongs only
                # to explicit controls of this queue's pause state.
                state["pause_control_serial"] = int(state.get("pause_control_serial", 0)) + 1
                state["manual_paused"] = action in {"music_pause", "music_stop", "bot_leave_voice"}
                preserve_wave_stop = (action == "music_stop" and state.get("source_mode") == "wave"
                                      and bool(state.get("current")) and not state["current"].get("starting"))
                if action == "music_stop" and not preserve_wave_stop:
                    state["items"] = []
                    state["current"] = None
                    state["source_mode"] = ""
                    state["source_title"] = ""
                    state["source_data"] = {}
                    state["generation"] = int(state.get("generation", 0)) + 1
            # Stopping an active infinite My Wave is transport-pause semantics.
            # This makes an accidental/ambiguous stop reversible and, crucially,
            # never destroys the personalized Rotor source or its pending queue.
            transport_action = "pause" if preserve_wave_stop else mapped
            try:
                result = self._background_for_profile(profile).voice_action(profile, transport_action, channel=str(kwargs.get("channel", "") or ""))
            finally:
                if action == "music_stop":
                    self._suspend_snapshot_stop(profile, False)
            with self._queue_lock:
                if action in {"music_pause", "music_stop", "bot_leave_voice", "music_resume"}:
                    self._wave_pause_clock(state, paused=action != "music_resume")
            if preserve_wave_stop:
                result = {**result, "requested_action": "stop", "wave_preserved": True}
            if current_before and action in {"music_pause", "music_resume", "music_stop"}:
                result = {**result, "current": current_before, "title": current_before.get("title", ""), "artist": current_before.get("artist", "")}
            if action in {"music_pause", "music_resume", "music_stop"}:
                self._broadcast_queue_snapshot(profile, reanchor=False)
            return result
        if action == "list_channels":
            profile = self._background_profile(server)
            return self._background_for_profile(profile).list_accessible_channels(profile)
        if action == "connect_channel":
            profile = self._background_profile(server)
            return self._background_for_profile(profile).connect_named_channel(profile, str(kwargs.get("channel", "") or ""))
        if action == "attach_text_channel":
            profile = self._background_profile(server)
            return self._background_for_profile(profile).attach_text_channel(profile, str(kwargs.get("channel", "") or ""))
        if action == "detach_text_channel":
            profile = self._background_profile(server)
            return self._background_for_profile(profile).detach_text_channel(profile)
        if action == "text_channel_status":
            profile = self._background_profile(server)
            return self._background_for_profile(profile).text_channel_status(profile)
        if action == "send_message":
            return self.send_message(str(kwargs.get("text", "") or ""), str(kwargs.get("channel", "") or ""), server)
        raise RuntimeError(
            f"Discord action {action!r} относится к удалённому desktop/UI режиму. "
            "Discord работает только через официальный NEO Bot/API."
        )

    def open_text_channel(self, channel: str, server: str = "") -> dict[str, Any]:
        channel = str(channel or "").strip()
        resolved = self.defaults(server)
        server = str(resolved["server"] or server or "").strip()
        if not channel:
            raise ValueError("Не указан текстовый канал Discord")

        window = dc.focus_discord_window()
        if dc._current_text_channel_matches(window, channel, server):
            return {
                "server": server,
                "channel": channel.lstrip("#"),
                "kind": "text",
                "verified": True,
                "method": "discord_already_in_channel",
            }

        # Do not reopen the same guild when Discord's title already proves it.
        if server and not dc._current_server_matches(window, server):
            logger.info("Discord reliable navigation: server=%r", server)
            _quick_switch_relaxed("*", server)
            deadline = time.monotonic() + 2.2
            while time.monotonic() < deadline:
                time.sleep(0.12)
                window = dc.focus_discord_window()
                if dc._current_server_matches(window, server):
                    break
            # Electron may hide selected-state UIA.  Do not abort yet: final
            # channel verification below also checks the guild context.
            if not dc._current_server_matches(window, server):
                logger.warning("Discord did not expose server selection immediately; continuing to channel verification")
        elif server:
            logger.info("Discord reliable navigation: already on server=%r", server)

        raw_candidates = dc._channel_query_candidates(channel)
        ordered: list[str] = []
        for value in list(reversed(raw_candidates)) + raw_candidates:
            value = str(value or "").strip()
            if value and value not in ordered:
                ordered.append(value)
        if not ordered:
            ordered = [channel.lstrip("#").strip()]

        last_query = ordered[0]
        for attempt, search_channel in enumerate(ordered, start=1):
            last_query = search_channel
            logger.info(
                "Discord reliable navigation: channel=%r attempt=%s/%s",
                search_channel, attempt, len(ordered),
            )
            _quick_switch_relaxed("#", search_channel)

            deadline = time.monotonic() + 2.8
            while time.monotonic() < deadline:
                time.sleep(0.12)
                window = dc.focus_discord_window()
                try:
                    if dc._post_navigation_channel_verified(window, search_channel, server):
                        return {
                            "server": server,
                            "channel": channel.lstrip("#"),
                            "kind": "text",
                            "verified": True,
                            "method": "discord_reliable_quick_switcher",
                            "query_used": search_channel,
                        }
                except Exception:
                    pass

            # A global # search can occasionally land in a same-named channel in
            # another guild.  Restore the requested guild before the literal-name
            # retry rather than sending /play to the wrong place.
            window = dc.focus_discord_window()
            if server and not dc._current_server_matches(window, server):
                _quick_switch_relaxed("*", server)
                time.sleep(0.45)

        raise RuntimeError(
            f"Discord открыл Quick Switcher, но не подтвердил переход в текстовый канал «{channel}» "
            f"(последний запрос: #{last_query}). /play не отправлялся."
        )


def shared_reliable_discord_adapter(config: Any) -> ReliableDiscordAdapter:
    """Return the process-wide Discord adapter for one ConfigManager instance.

    Telegram intentionally owns a separate Agent, but it must never own a second
    Discord Gateway/VoiceClient.  Sharing this adapter keeps transport and queue
    ownership coherent while preserving separate Agent conversation state.
    """
    key = id(config)
    with _SHARED_ADAPTERS_LOCK:
        adapter = _SHARED_ADAPTERS.get(key)
        if adapter is None or getattr(adapter, "config", None) is not config:
            adapter = ReliableDiscordAdapter(config)
            _SHARED_ADAPTERS[key] = adapter
        return adapter
