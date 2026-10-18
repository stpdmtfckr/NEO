# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Official Discord bot background integration for NEO.

This module never authenticates as the user's Discord account.  It uses a bot
token, Discord's Gateway/voice API and optional Yandex stream URLs.  Therefore
it does not need the Discord desktop window, foreground focus, mouse or
keyboard input.
"""

import asyncio
import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable

from jarvis.runtime.media_levels import publish_audio_packet, clear_media_levels
from jarvis.integrations.youtube_audio import YouTubeAudioResolver, normalize_youtube_url
from jarvis.integrations.media_state import MediaRequestSuperseded

from jarvis.secrets import discord_bot_token

logger = logging.getLogger(__name__)

_ALL_BOTS: "weakref.WeakSet[DiscordBackgroundBot]" = weakref.WeakSet()
_ALL_BOTS_LOCK = threading.RLock()

_CONTROL_MARKER = "[NEO-JARVIS-BUS/1]"


def bind_bus_ingress188(payload: dict[str, Any], message: Any, client_bot_id: str) -> dict[str, Any] | None:
    """Bind one received bus packet to where and by whom it actually arrived.

    Human users can never forge queue-control packets: only a Discord bot account
    is accepted, and a request's requester_bot_id must match the real author
    (two PCs may share one bot token). HIST-027: ``kind`` is normalised once for
    every later check; a packet is accepted only inside a server channel, and a
    declared guild that differs from the guild the message arrived in is refused,
    so the declared fields can never select another server's profile."""
    if not isinstance(payload, dict):
        return None
    author = getattr(message, "author", None)
    author_id = str(getattr(author, "id", "") or "").strip()
    if not author_id or not bool(getattr(author, "bot", False)):
        return None
    target_bot_id = str(payload.get("target_bot_id", "") or "").strip()
    if target_bot_id and target_bot_id != str(client_bot_id):
        return None
    kind = str(payload.get("kind", "") or "").strip().casefold()
    if kind == "request":
        declared = str(payload.get("requester_bot_id", "") or "").strip()
        if declared and declared != author_id:
            return None
    guild = getattr(message, "guild", None)
    actual_guild = str(getattr(guild, "id", "") or "").strip()
    if not actual_guild:
        return None                                  # bus packets live in a server channel, never in DMs
    declared_guild = str(payload.get("guild_id", "") or "").strip()
    if declared_guild and declared_guild != actual_guild:
        return None
    bound = dict(payload)
    bound["kind"] = kind
    bound["guild_id"] = actual_guild
    bound["_message_author_bot_id"] = author_id
    bound["_message_author_name"] = str(getattr(author, "display_name", "") or getattr(author, "name", "") or "")
    bound["_ingress_guild_id"] = actual_guild
    bound["_ingress_channel_id"] = str(getattr(getattr(message, "channel", None), "id", "") or "")
    bound["_ingress_client_bot_id"] = str(client_bot_id)
    return bound


def _public_control_data(value: Any, depth: int = 0) -> Any:
    """Queue ACKs travel through a visible channel, so export metadata only."""
    allowed = {
        "server", "guild_id", "channel", "channel_id", "voice_channel", "voice_channel_id",
        "current", "queue", "queue_length", "history", "history_length", "title", "artist",
        "track_id", "query", "media_source", "source_mode", "source_title", "source", "id",
        "expected_duration_ms", "duration_ms", "position_ms", "volume", "volume_percent",
        "shuffle", "repeat_mode", "playing", "paused", "connected", "levels", "state",
        "audio_levels_real", "audio_levels_kind", "verified", "method", "shared_queue",
        "host", "host_instance", "liked", "disliked", "count", "added", "remaining",
        "cancelled", "superseded", "playback_scope", "playback_generation",
        "requester", "queue_source", "media_event_id", "shared_state_pending", "stopped",
        # NEO 187 (REC-DPN-01): typed queue-add ACK fields read by the slash /
        # panel consumer. item is filtered recursively by this same allowlist.
        "queue_started_immediately", "queued", "item", "position", "play_now", "next_up",
        "added_count", "ack_contract", "transport_connected", "transport_state",
    }
    if depth > 5:
        return None
    if isinstance(value, dict):
        return {key: _public_control_data(item, depth + 1) for key, item in value.items() if key in allowed}
    if isinstance(value, (list, tuple)):
        return [_public_control_data(item, depth + 1) for item in value[:96]]
    if isinstance(value, str):
        # Titles and queries can themselves contain local paths or signed URLs.
        value = re.sub(r"https?://\S+|(?:[A-Za-z]:[\\/]|\\\\)[^\n]+|(?<!\w)/(?:home|Users|tmp|workspace)/\S+", "[private source]", value)
        return value[:240]
    return value if isinstance(value, (bool, int, float)) or value is None else None


def _as_snowflake(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    try:
        return int(text)
    except Exception:
        return None


def _norm(value: str) -> str:
    return " ".join(str(value or "").casefold().replace("#", " ").replace("!", " ").split())


class _PlaybackGuardIdentity:
    """Identity key retaining the guard, including unhashable callable objects."""
    def __init__(self, callback: Callable[[], None]):
        self.callback = callback

    def __hash__(self) -> int:
        return id(self.callback)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, _PlaybackGuardIdentity) and self.callback is other.callback


class DiscordBackgroundBot:
    def __init__(self, config: Any, *, token_encrypted: str = "") -> None:
        self.config = config
        self._profile_token_encrypted = str(token_encrypted or "").strip()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any | None = None
        self._ready = threading.Event()
        self._failed = threading.Event()
        # Full-shutdown fence. Once set, no observer/probe/dashboard thread may
        # submit work to a Proactor loop that is closing. The native crash log
        # showed run_coroutine_threadsafe() racing loop teardown on Windows.
        self._closing = threading.Event()
        self._error = ""
        self._admission_generation = 0
        self._lock = threading.RLock()
        self._pending_operations: dict[tuple[Any, ...], Any] = {}
        self._pending_play_limit = 8
        self._terminal_play_limit = 32
        self._mutation_submit_lock = threading.RLock()
        self._network_changed: asyncio.Event | None = None
        self._token_fingerprint = ""
        self._playback_generation: dict[int, int] = {}
        self._last_finished_generation: dict[int, int] = {}
        self._last_finished_at: dict[int, float] = {}
        self._last_finished_error: dict[int, str] = {}
        self._active_local_sources: dict[int, str] = {}
        self._playback_errors: dict[int, str] = {}
        # Canonical transport metadata lives beside the actual VoiceClient.
        # The queue adapter can be reconstructed/restarted independently; this
        # lets passive observers recover title/artist from the transport owner
        # even when its queue entry is temporarily only a TrackShort/URL.
        self._playback_metadata: dict[int, dict[str, Any]] = {}
        self._control_handler: Any | None = None
        self._music_control_handler: Any | None = None
        self._music_profiles: dict[int, dict[str, Any]] = {}
        self._music_channel_ids: dict[int, int] = {}
        self._sync_channel_ids: dict[int, int] = {}
        # HOTFIX95: explicit user-selected writable text destination per guild.
        # Persist it outside settings.json so a voice/text attachment survives
        # an application restart without turning a transient runtime choice into
        # a global configuration rewrite from the Discord worker thread.
        self._attached_text_channel_ids: dict[int, int] = self._load_text_channel_attachments()
        # Voice connects/moves are serialized per guild. A second Agent, button
        # callback or queue transition must never overlap discord.py's handshake
        # for the same bot account.
        self._voice_connect_locks: dict[int, asyncio.Lock] = {}
        self._music_dashboard_ids: dict[int, int] = {}
        self._music_dashboard_messages: dict[int, Any] = {}
        # Dashboard edits are serialized per guild. Its Discord message id is
        # stable for the lifetime of the channel; normal track/history updates
        # never recreate the control message.
        self._music_dashboard_locks: dict[int, Any] = {}
        self._music_last_state: dict[int, dict[str, Any]] = {}
        # HOTFIX97: visual dashboard state is deduplicated independently from
        # transport telemetry (levels/position).  The old code PATCHed the same
        # message for duplicate snapshots and could recreate it several times
        # during a burst of chat messages, eventually hitting Discord 429.
        self._music_dashboard_signatures: dict[int, str] = {}
        self._music_channel_locks: dict[int, asyncio.Lock] = {}
        # HOTFIX125: per-guild Discord voice volume. PCM playback is wrapped in
        # PCMVolumeTransformer so Telegram +/- buttons can change volume without
        # restarting the current song. Opus fallback keeps the requested value for
        # the next PCM source and reports that live adjustment was unavailable.
        self._voice_volume_percent: dict[int, int] = {}
        # Playback history is kept as normal persistent channel messages.  The
        # canonical dashboard itself is intentionally limited to Now Playing +
        # five upcoming tracks + controls, so it never grows over long sessions.
        self._music_track_history: dict[int, list[str]] = {}
        self._music_current_key: dict[int, str] = {}
        # Keep the last concrete track independently from transient queue snapshots.
        # Voice reconnect/buffering can briefly publish current={}, and using only
        # _music_last_state would then lose the previous title before the next song
        # arrives. This metadata survives those empty snapshots so history remains
        # intact during multi-hour sessions.
        self._music_current_meta: dict[int, dict[str, Any]] = {}
        self._timer_summaries: dict[int, list[str]] = {}
        # pending / delivered / error / unknown per guild timer panel.
        self._timer_delivery: dict[int, str] = {}
        # Timers use their own single live message instead of being appended to
        # the music dashboard.  This preserves the earlier "timers stay visible"
        # behavior while keeping the music console strictly music-only.
        self._timer_dashboard_ids: dict[int, int] = {}
        self._timer_dashboard_messages: dict[int, Any] = {}
        self._timer_dashboard_signatures: dict[int, str] = {}
        self._persistent_music_view_registered = False
        self._empty_voice_tasks: dict[int, Any] = {}
        self._voice_owner_ids: dict[int, int] = {}
        self._owner_voice_channels: dict[int, int | None] = {}
        self._voice_cleanup_committed: set[int] = set()
        self._ffmpeg_verified_path = ""
        self._ffmpeg_rejected_paths: set[str] = set()
        # HOTFIX117: direct Discord YouTube playback is a Discord transport
        # capability.  It owns its resolver here and never consults the optional
        # YouTube Data API integration/API key used for title-based video search.
        self._youtube_audio = YouTubeAudioResolver(config)
        with _ALL_BOTS_LOCK:
            _ALL_BOTS.add(self)

    def _text_channel_attachment_path(self) -> Path:
        try:
            root = Path(getattr(getattr(self.config, "paths", None), "root"))
        except Exception:
            root = Path(os.getenv("LOCALAPPDATA", Path.home())) / "NeoJarvis"
        return root / "discord_text_channels.json"

    def _load_text_channel_attachments(self) -> dict[int, int]:
        path = self._text_channel_attachment_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            return {}
        items = raw.get("guilds", raw) if isinstance(raw, dict) else {}
        out: dict[int, int] = {}
        if isinstance(items, dict):
            for guild_id, channel_id in items.items():
                try:
                    gid, cid = int(guild_id), int(channel_id)
                except Exception:
                    continue
                if gid > 0 and cid > 0:
                    out[gid] = cid
        return out

    def _save_text_channel_attachments(self) -> None:
        path = self._text_channel_attachment_path()
        payload = {"version": 1, "guilds": {str(k): str(v) for k, v in sorted(self._attached_text_channel_ids.items()) if k > 0 and v > 0}}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)
        except Exception:
            logger.debug("Could not persist Discord text-channel attachment state", exc_info=True)

    def _enabled(self) -> bool:
        # Both the Discord master switch and the background-bot switch must be
        # on: turning either off stops admission, reconnects and late results.
        enabled = bool(self.config.get("integrations.discord.enabled", True)) and bool(
            self.config.get("integrations.discord.background_bot_enabled", True)
        )
        if not enabled:
            # Every observed "off" starts a new admission generation, so work
            # admitted before a disable never revives after re-enabling.
            self._admission_generation = getattr(self, "_admission_generation", 0) + 1
        return enabled

    def admission(self) -> int | None:
        """Current admission generation, or None while Discord may not act."""
        if self._closing.is_set() or not self._enabled():
            return None
        return int(getattr(self, "_admission_generation", 0))

    def revoke_admission(self) -> None:
        """Invalidate every admitted operation (settings changed to disabled)."""
        self._admission_generation = getattr(self, "_admission_generation", 0) + 1

    def _require_admitted(self, admission: int | None) -> None:
        """Raise when the operation lost its permission after an await."""
        current = self.admission()
        if current is None or admission is None or current != admission:
            raise MediaRequestSuperseded("Discord отключён в настройках или операция устарела")

    def _inactive(self, client: Any) -> bool:
        return self._closing.is_set() or self._client is not client or not self._enabled()

    def _token(self) -> str:
        profile = {"bot_token_encrypted": self._profile_token_encrypted} if self._profile_token_encrypted else None
        return discord_bot_token(self.config, profile)

    def available(self) -> dict[str, Any]:
        token = self._token()
        return {
            "enabled": self._enabled(),
            "token": bool(token),
            "ready": self._ready.is_set() and not self._failed.is_set(),
            "error": self._error,
            "phase": getattr(self, "_gateway_phase", "not_started"),
            "attempts": getattr(self, "_gateway_attempts", 0),
        }

    def on_network_changed(self, event: Any = None, *_args: Any) -> None:
        if isinstance(event, dict) and event.get("online") is False:
            return
        with self._lock:
            loop, wake = self._loop, self._network_changed
        if loop is not None and wake is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                pass

    async def _run_gateway(self, client_factory: Any, token: str) -> None:
        from jarvis.integrations.background.gateway_recovery import run_gateway
        await run_gateway(self, client_factory, token)

    def _loop_exception_handler(self, loop: Any, context: dict[str, Any]) -> None:
        exception = context.get("exception")
        task = context.get("task") or context.get("future")
        name = str(getattr(task, "get_name", lambda: "")())
        is_voice = "voice" in name.casefold()
        known_disconnect = type(exception).__name__ in {"ConnectionClosed", "ClientConnectionResetError", "ConnectionResetError"}
        if is_voice and known_disconnect:
            code = getattr(exception, "code", "")
            with self._lock:
                for guild_id in self._playback_generation:
                    self._playback_errors[guild_id] = f"Discord voice connection lost ({code or type(exception).__name__})"
                    self._last_finished_error[guild_id] = self._playback_errors[guild_id]
            # Retrieve the failure and publish it to the transport observer. Do
            # not discard unexpected task errors or pretend playback succeeded.
            if task is not None and getattr(task, "done", lambda: False)():
                try:
                    task.exception()
                except (asyncio.CancelledError, Exception):
                    pass
            logger.warning("Discord voice task disconnected; playback observer will reconcile: %s", code or type(exception).__name__)
            return
        loop.default_exception_handler(context)

    def _create_gateway_client(self) -> Any:
        """Bind a fresh public client, command tree and event callbacks per attempt."""
        import discord
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        # Guild message events are a standard intent. The control bus never
        # asks for privileged MESSAGE_CONTENT: bus packets explicitly mention
        # the destination bot (or are messages from the same app), both of
        # which Discord exposes without the privileged content intent.
        intents.guild_messages = True
        client = discord.Client(intents=intents)
        tree = discord.app_commands.CommandTree(client)

        async def neo_play(interaction: Any, query: str, mode: str = "last") -> None:
            """NEO 180: /play now | next | last — what anyone in the server can do without NEO."""
            if self._inactive(client):
                return
            handler = self._music_control_handler
            if handler is None:
                await interaction.response.send_message("NEO: музыкальная очередь ещё не готова.", ephemeral=True)
                return
            try:
                # Slash command acknowledgement is temporary and removed after
                # the queue update so it cannot sit below the music dashboard.
                await interaction.response.defer(ephemeral=True, thinking=True)
            except Exception:
                return
            guild = getattr(interaction, "guild", None)
            profile = {
                "guild_id": str(getattr(guild, "id", "") or ""),
                "name": str(getattr(guild, "name", "") or ""),
            }
            user = getattr(interaction, "user", None)
            requester = str(getattr(user, "display_name", "") or getattr(user, "name", "") or getattr(user, "id", "") or "Discord")
            try:
                await asyncio.to_thread(handler, profile, "play", {"query": str(query or "").strip(), "requester": requester, "mode": mode})
                try:
                    await interaction.delete_original_response()
                except Exception:
                    pass
            except Exception as exc:
                try:
                    await interaction.edit_original_response(content=f"Не удалось добавить трек: {exc}"[:1800])
                except Exception:
                    pass

        # Discord shows these as «/play now», «/play next» and «/play last».
        play_group = discord.app_commands.Group(name="play", description="Музыка NEO: включить сейчас, следующей или в конец очереди")

        @play_group.command(name="now", description="Включить сейчас: текущая песня пропускается")
        @discord.app_commands.describe(query="Название и исполнитель или ссылка")
        async def neo_play_now(interaction: Any, query: str) -> None:
            await neo_play(interaction, query, "now")

        @play_group.command(name="next", description="Поставить следующей в очереди")
        @discord.app_commands.describe(query="Название и исполнитель или ссылка")
        async def neo_play_next(interaction: Any, query: str) -> None:
            await neo_play(interaction, query, "next")

        @play_group.command(name="last", description="Добавить в конец очереди")
        @discord.app_commands.describe(query="Название и исполнитель или ссылка")
        async def neo_play_last(interaction: Any, query: str) -> None:
            await neo_play(interaction, query, "last")

        tree.add_command(play_group)

        @client.event
        async def on_disconnect() -> None:
            if self._client is client:
                self._ready.clear()

        @client.event
        async def on_resumed() -> None:
            if not self._closing.is_set() and self._client is client:
                self._failed.clear()
                self._error = ""
                self._gateway_phase = "ready"
                self._gateway_ready_at = time.monotonic()
                self._ready.set()

        @client.event
        async def on_ready() -> None:
            if self._closing.is_set() or self._client is not client:
                return
            logger.info("Discord background bot ready as %s", client.user)
            self._error = ""
            self._failed.clear()
            self._gateway_phase = "ready"
            self._gateway_ready_at = time.monotonic()
            self._ready.set()
            # Register the custom_ids as a persistent view on every fresh
            # Gateway process. Without Client.add_view(), buttons attached to
            # a dashboard from the previous NEO session render in Discord but
            # have no callback until the message happens to be edited again.
            if not self._persistent_music_view_registered:
                try:
                    client.add_view(self._music_view({}, {"current": {"title": "persistent"}}))
                    self._persistent_music_view_registered = True
                except Exception as exc:
                    logger.debug("Discord persistent music controls registration failed: %s", exc)
            # Best-effort user-facing music channel. Failure never blocks
            # Gateway readiness; servers without Manage Channels simply keep
            # using the existing control transport until permission is granted.
            for guild in list(getattr(client, "guilds", []) or []):
                if self._closing.is_set() or self._client is not client:
                    return
                try:
                    # Guild-scoped sync makes /play available immediately on
                    # servers where this NEO bot is installed, without the
                    # delay of a global Discord command rollout.
                    try:
                        guild_obj = discord.Object(id=int(guild.id))
                        tree.copy_global_to(guild=guild_obj)
                        await tree.sync(guild=guild_obj)
                    except Exception as exc:
                        logger.info("Discord /play sync skipped for %s: %s", getattr(guild, "name", ""), exc)
                    # V5.5: the temporary V5.4 #neo-jarvis-sync transport is
                    # no longer part of the product. Remove only channels that
                    # carry NEO's own internal-sync topic marker; never delete a
                    # user-created channel merely because its name is similar.
                    await self._cleanup_legacy_sync_channels(guild)
                    channel = await self._ensure_music_channel(guild)
                    if channel is not None:
                        profile = {
                            "guild_id": str(getattr(guild, "id", "") or ""),
                            "name": str(getattr(guild, "name", "") or ""),
                        }
                        gid = int(getattr(guild, "id", 0) or 0)
                        voice = getattr(guild, "voice_client", None)
                        member_voice = getattr(getattr(guild, "me", None), "voice", None)
                        connected = (
                            (voice is not None and getattr(voice, "channel", None) is not None)
                            or (member_voice is not None and getattr(member_voice, "channel", None) is not None)
                        )
                        if connected:
                            existing = await self._find_music_dashboard(channel)
                            if existing is not None:
                                self._music_dashboard_messages[gid] = existing
                                self._music_dashboard_ids[gid] = int(existing.id)
                                await self._cleanup_music_channel_clutter(channel, keep_message_id=int(existing.id))
                            else:
                                await self._publish_music_state(profile, {
                                    "current": {}, "queue": [], "queue_length": 0,
                                    "playing": False, "paused": False, "connected": True,
                                    "verified": True, "shared_queue": True,
                                })
                        else:
                            # No voice session = no controls. Old/crash-stale
                            # dashboard is removed while ordinary history stays.
                            await self._remove_music_dashboard(guild, channel)
                except Exception as exc:
                    logger.info("Discord music channel setup skipped for %s: %s", getattr(guild, "name", ""), exc)

        @client.event
        async def on_guild_join(guild: Any) -> None:
            if self._closing.is_set() or self._client is not client:
                return
            try:
                try:
                    guild_obj = discord.Object(id=int(guild.id))
                    tree.copy_global_to(guild=guild_obj)
                    await tree.sync(guild=guild_obj)
                except Exception as exc:
                    logger.info("Discord /play sync skipped for joined guild %s: %s", getattr(guild, "name", ""), exc)
                await self._cleanup_legacy_sync_channels(guild)
                channel = await self._ensure_music_channel(guild)
                if channel is not None:
                    profile = {"guild_id": str(getattr(guild, "id", "") or ""), "name": str(getattr(guild, "name", "") or "")}
                    gid = int(getattr(guild, "id", 0) or 0)
                    voice = getattr(guild, "voice_client", None)
                    member_voice = getattr(getattr(guild, "me", None), "voice", None)
                    connected = (
                        (voice is not None and getattr(voice, "channel", None) is not None)
                        or (member_voice is not None and getattr(member_voice, "channel", None) is not None)
                    )
                    if connected:
                        existing = await self._find_music_dashboard(channel)
                        if existing is not None:
                            self._music_dashboard_messages[gid] = existing
                            self._music_dashboard_ids[gid] = int(existing.id)
                            await self._cleanup_music_channel_clutter(channel, keep_message_id=int(existing.id))
                        else:
                            await self._publish_music_state(
                                profile,
                                {"current": {}, "queue": [], "queue_length": 0, "playing": False, "paused": False, "connected": True, "verified": True, "shared_queue": True},
                            )
                    else:
                        await self._remove_music_dashboard(guild, channel)
            except Exception as exc:
                logger.info("Discord music channel setup skipped for joined guild %s: %s", getattr(guild, "name", ""), exc)

        @client.event
        async def on_voice_state_update(member: Any, before: Any, after: Any) -> None:
            if self._closing.is_set() or self._client is not client:
                return
            await self._handle_voice_state_update(member, before, after)

        @client.event
        async def on_message(message: Any) -> None:
            # Server-wide NEO queue coordination. Packets are deliberately
            # bot-authored and address a concrete bot account with a mention,
            # so MESSAGE_CONTENT privileged intent is not required.
            if self._inactive(client):
                return
            try:
                content = str(getattr(message, "content", "") or "")
                marker_at = content.find(_CONTROL_MARKER)
                if marker_at < 0 or client.user is None:
                    # HOTFIX130: never recreate the music control dashboard
                    # merely because another ordinary/history/timer message
                    # appeared below it. Re-anchor was the source of Discord's
                    # white/skeleton rows: send new dashboard -> discover it ->
                    # delete old dashboard. The canonical control message now
                    # stays at one message id for its whole lifetime and only
                    # receives in-place edits.
                    return
                payload_text = content[marker_at + len(_CONTROL_MARKER):].strip()
                if payload_text.endswith("||"):
                    payload_text = payload_text[:-2].rstrip()
                if not payload_text:
                    return
                payload = json.loads(payload_text)
                if not isinstance(payload, dict):
                    return
                payload = bind_bus_ingress188(payload, message, str(client.user.id))
                if payload is None:
                    return
                handler = self._control_handler
                if handler is None:
                    return
                threading.Thread(
                    target=handler, args=(payload,),
                    name="jarvis-discord-control-packet", daemon=True,
                ).start()
            except Exception as exc:
                logger.debug("Discord control packet ignored: %s", exc)

        return client

    def _thread_main(self, token: str) -> None:
        loop: asyncio.AbstractEventLoop | None = None
        try:
            loop = asyncio.new_event_loop()
            loop.set_exception_handler(self._loop_exception_handler)
            asyncio.set_event_loop(loop)
            with self._lock:
                self._loop = loop

            async def runner() -> None:
                try:
                    await self._run_gateway(self._create_gateway_client, token)
                except Exception as exc:
                    from jarvis.integrations.background.gateway_recovery import safe_error, safe_traceback
                    self._error = safe_error(exc, token)
                    self._failed.set()
                    self._ready.set()
                    logger.error("Discord background bot failed: %s", safe_traceback(exc, token))
                finally:
                    try:
                        client = self._client
                        if client is not None and not client.is_closed():
                            await asyncio.wait_for(client.close(), timeout=5.0)
                    except Exception:
                        pass
                    loop.stop()

            loop.create_task(runner())
            loop.run_forever()
        except Exception as exc:
            from jarvis.integrations.background.gateway_recovery import safe_error, safe_traceback
            self._error = safe_error(exc, token)
            self._failed.set()
            self._ready.set()
            logger.error("Discord background bot thread failed: %s", safe_traceback(exc, token))
        finally:
            # Do not leave aiohttp/discord Tasks to be destroyed by GC after
            # the event loop thread exits. If graceful client.close() timed out,
            # cancel and drain the remaining tasks for a short bounded interval
            # before closing the loop itself.
            if loop is not None and not loop.is_closed():
                try:
                    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        gathered = asyncio.gather(*pending, return_exceptions=True)
                        loop.run_until_complete(asyncio.wait_for(gathered, timeout=0.8))
                except Exception:
                    logger.debug("Discord event-loop pending-task drain incomplete", exc_info=True)
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                try:
                    loop.close()
                except Exception:
                    pass
            with self._lock:
                self._loop = None
                self._client = None

    def close(self, timeout: float = 3.0) -> bool:
        """Disconnect voice/Gateway and stop the bot event-loop thread.

        Returns ``True`` only when the Gateway thread is confirmed stopped.  A
        false result keeps the application's full-shutdown watchdog armed rather
        than allowing CPython/native-library teardown to race a live Discord
        Proactor thread.
        """
        self._closing.set()
        with self._lock:
            pending_operations = list(self._pending_operations.values())
            self._pending_operations.clear()
            loop = self._loop
            client = self._client
            thread = self._thread
        for pending in pending_operations:
            pending.cancel()

        if loop is not None and not loop.is_closed():
            async def _close_all() -> None:
                if client is None:
                    return
                try:
                    for voice in list(getattr(client, "voice_clients", []) or []):
                        try:
                            if getattr(voice, "is_playing", lambda: False)() or getattr(voice, "is_paused", lambda: False)():
                                voice.stop()
                        except Exception:
                            pass
                        # History is permanent, but a stopped/disconnected bot
                        # must not leave dead control buttons behind.
                        try:
                            guild = getattr(voice, "guild", None)
                            if guild is not None:
                                await self._remove_music_dashboard(guild)
                        except Exception:
                            pass
                        try:
                            await voice.disconnect(force=True)
                        except Exception:
                            pass
                finally:
                    try:
                        if not client.is_closed():
                            await client.close()
                    except Exception:
                        pass

            try:
                future = asyncio.run_coroutine_threadsafe(_close_all(), loop)
                future.result(timeout=max(0.5, float(timeout)))
            except Exception:
                logger.debug("Discord background bot graceful close timed out", exc_info=True)
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.5, float(timeout)))
            # One final loop-stop nudge covers the case where client.close()
            # completed exactly as the first join expired.
            if thread.is_alive() and loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except Exception:
                    pass
                thread.join(timeout=min(1.5, max(0.5, float(timeout))))

        stopped = thread is None or not thread.is_alive()
        if not stopped:
            logger.error("Discord background thread is still alive after graceful shutdown; keeping process watchdog armed")
            # Keep references intact: clearing them while the worker still owns
            # native/asyncio resources made shutdown races harder to diagnose.
            return False

        with self._lock:
            self._thread = None
            self._client = None
            self._loop = None
            self._ready.clear()
            self._failed.clear()
        return True

    def ensure_ready(self, timeout: float = 18.0) -> None:
        if self._closing.is_set():
            raise RuntimeError("Discord background bot завершает работу")
        if not self._enabled():
            raise RuntimeError("Фоновый Discord-бот отключён в настройках")
        token = self._token()
        if not token:
            raise RuntimeError(
                "Для этого Discord-сервера не сохранён BOT token. Укажите его в той же строке сервера "
                "в Настройки → Медиа → Discord. User token использовать не нужно."
            )
        fingerprint = f"{len(token)}:{token[:4]}:{token[-4:]}"
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            if alive and self._token_fingerprint == fingerprint and self._ready.is_set() and not self._failed.is_set():
                return
            if not alive or self._token_fingerprint != fingerprint:
                self._ready.clear()
                self._failed.clear()
                self._error = ""
                self._token_fingerprint = fingerprint
                self._thread = threading.Thread(
                    target=self._thread_main,
                    args=(token,),
                    name="jarvis-discord-background-bot",
                    daemon=True,
                )
                self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("Discord background bot не успел подключиться к Discord Gateway")
        if self._failed.is_set():
            raise RuntimeError(f"Discord background bot не подключился: {self._error or 'unknown error'}")

    def _submit(self, coroutine: Any, timeout: float = 25.0) -> Any:
        name = str(getattr(getattr(coroutine, "cr_code", None), "co_name", ""))
        if name in {"_play", "_send", "_send_control", "_voice_action"}:
            with self._mutation_submit_lock:
                return self._submit_once(coroutine, timeout)
        return self._submit_once(coroutine, timeout)

    def _prune_superseded_play_operations(self) -> None:
        # Never evict an active operation with unknown completion. Completed
        # guarded requests are disposable only once their own guard forbids any
        # retry, so removing them cannot replay an old user request.
        with self._lock:
            candidates = [(key, future) for key, future in self._pending_operations.items()
                          if key[0] == "_play" and future.done()
                          and isinstance(key[-1], _PlaybackGuardIdentity)]
        for key, future in candidates:
            try:
                key[-1].callback()
            except MediaRequestSuperseded:
                with self._lock:
                    if self._pending_operations.get(key) is future and future.done():
                        self._pending_operations.pop(key, None)
            except Exception:
                # An unrelated guard error is not proof the original mutation
                # is safe to forget. Keep its admission slot and result.
                pass

    def _submit_once(self, coroutine: Any, timeout: float = 25.0) -> Any:
        admitted = self.admission()
        if admitted is None:
            try:
                coroutine.close()
            except Exception:
                pass
            raise RuntimeError("Фоновый Discord-бот отключён в настройках")
        frame = getattr(coroutine, "cr_frame", None)
        values = frame.f_locals if frame is not None else {}
        name = str(getattr(getattr(coroutine, "cr_code", None), "co_name", ""))
        profile = values.get("profile") or {}
        scope = str(profile.get("guild_id") or profile.get("name") or "") if isinstance(profile, dict) else ""
        # Mutating calls with unknown completion cannot be submitted a second
        # time. A retry joins the original Future, including a late completion.
        mutation = name in {"_play", "_send", "_send_control", "_voice_action"}
        key = (name, scope, str(values.get("action", "")))
        if name == "_play":
            before_start = values.get("before_start")
            if before_start is not None:
                try:
                    before_start()
                except BaseException:
                    coroutine.close()
                    raise
            self._prune_superseded_play_operations()
            # A timeout leaves this exact submission's outcome unknown. A new
            # song (or new request for the same song) must never inherit that
            # old result merely because it uses the same guild and method.
            key += (
                str(values.get("url", "")), str(values.get("title", "")),
                str(values.get("artist", "")), str(values.get("channel", "")),
                bool(values.get("fast_start")), bool(values.get("prefer_current_voice")),
                tuple(sorted(dict(values.get("http_headers") or {}).items())),
                _PlaybackGuardIdentity(before_start) if before_start is not None else None,
            )
        if mutation:
            with self._lock:
                pending = self._pending_operations.get(key)
            if pending is not None:
                coroutine.close()
                try:
                    result = pending.result(timeout=timeout)
                except TimeoutError as exc:
                    raise TimeoutError("Discord: результат предыдущей операции ещё проверяется; повтор не отправлен") from exc
                finally:
                    if pending.done():
                        with self._lock:
                            self._pending_operations.pop(key, None)
                return result
        if name == "_play":
            with self._lock:
                plays = [future for operation_key, future in self._pending_operations.items()
                         if operation_key[0] == "_play"]
                full = (len(plays) >= self._terminal_play_limit
                        or sum(not future.done() for future in plays) >= self._pending_play_limit)
            if full:
                coroutine.close()
                raise RuntimeError("Discord: слишком много неподтверждённых операций воспроизведения; повтор не отправлен")
        if self._closing.is_set():
            try:
                coroutine.close()
            except Exception:
                pass
            raise RuntimeError("Discord background bot завершает работу")
        try:
            self.ensure_ready()
        except Exception:
            coroutine.close()
            raise
        # The readiness wait can take seconds; Discord may have been switched
        # off meanwhile. Re-check the same admission right before queueing.
        if self.admission() != admitted:
            try:
                coroutine.close()
            except Exception:
                pass
            raise RuntimeError("Фоновый Discord-бот отключён в настройках")
        with self._lock:
            loop = self._loop
            closing = self._closing.is_set()
        if closing or loop is None or loop.is_closed():
            try:
                coroutine.close()
            except Exception:
                pass
            raise RuntimeError("Цикл Discord background bot недоступен или закрывается")
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except Exception:
            try:
                coroutine.close()
            except Exception:
                pass
            raise
        if mutation:
            with self._lock:
                if self._closing.is_set():
                    future.cancel()
                else:
                    self._pending_operations[key] = future
            if name == "_play":
                future.add_done_callback(lambda _future: self._prune_superseded_play_operations())
        try:
            result = future.result(timeout=timeout)
        except TimeoutError as exc:
            if mutation:
                raise TimeoutError("Discord: время ожидания истекло, результат операции пока неизвестен; автоматический повтор запрещён") from exc
            future.cancel()
            raise
        except BaseException:
            if mutation:
                with self._lock:
                    if self._pending_operations.get(key) is future:
                        self._pending_operations.pop(key, None)
            raise
        else:
            if mutation:
                with self._lock:
                    if self._pending_operations.get(key) is future:
                        self._pending_operations.pop(key, None)
            return result

    @staticmethod
    def _profile_server_name(profile: dict[str, Any]) -> str:
        return str(profile.get("name", "") or profile.get("server", "") or "").strip()

    async def _resolve_guild(self, profile: dict[str, Any]) -> Any:
        client = self._client
        if client is None:
            raise RuntimeError("Discord client не готов")

        guild_id = _as_snowflake(profile.get("guild_id"))
        if guild_id:
            guild = client.get_guild(guild_id)
            if guild is not None:
                return guild
            logger.warning("Configured Discord Guild ID %s is not visible to the bot", guild_id)

        wanted = _norm(self._profile_server_name(profile))
        if wanted:
            exact = next((g for g in client.guilds if _norm(g.name) == wanted), None)
            if exact is not None:
                return exact

        # A personal NEO bot is commonly installed on exactly one guild.  In
        # that case a renamed server or a stale local profile must not prevent
        # hands-free playback.  The choice is unambiguous and does not touch UI.
        if len(client.guilds) == 1:
            guild = client.guilds[0]
            if wanted or guild_id:
                logger.warning(
                    "Discord profile did not match configured guild; using the bot's only guild %r (%s)",
                    guild.name, guild.id,
                )
            return guild

        available = ", ".join(f"{g.name} ({g.id})" for g in client.guilds[:12])
        raise RuntimeError(
            f"Фоновый NEO Bot не смог однозначно выбрать Discord-сервер «{self._profile_server_name(profile)}». "
            f"Доступные серверы: {available or 'нет'}. Выберите сервер по имени в Настройки → Медиа → Discord; числовой Guild ID не требуется."
        )

    @staticmethod
    def _voice_permissions_ok(guild: Any, channel: Any) -> bool:
        try:
            member = guild.me
            if member is None:
                return True
            perms = channel.permissions_for(member)
            return bool(
                getattr(perms, "view_channel", True)
                and getattr(perms, "connect", True)
                and getattr(perms, "speak", True)
            )
        except Exception:
            return True

    async def _resolve_voice(self, guild: Any, profile: dict[str, Any], channel: str = "") -> Any:
        import discord

        all_channels = list(getattr(guild, "voice_channels", []) or []) + list(getattr(guild, "stage_channels", []) or [])
        connectable = [item for item in all_channels if self._voice_permissions_ok(guild, item)]
        auto_follow = bool(self.config.get("integrations.discord.auto_follow_user_voice", True))

        # HOTFIX96: an explicitly named voice channel always wins over automatic
        # user-follow. Previously the command "подключись к каналу X" entered
        # auto-follow first and failed if the owner was not currently in voice,
        # even though X was visible and uniquely named.
        explicit_name = str(channel or "").strip()
        if explicit_name:
            channel_id = _as_snowflake(explicit_name)
            if channel_id:
                found = guild.get_channel(channel_id)
                if found in all_channels and self._voice_permissions_ok(guild, found):
                    return found
            wanted_explicit = _norm(explicit_name)
            exact = [item for item in connectable if _norm(getattr(item, "name", "")) == wanted_explicit]
            if len(exact) == 1:
                return exact[0]
            available = ", ".join(str(getattr(item, "name", "")) for item in connectable[:20])
            raise RuntimeError(f"Голосовой канал «{explicit_name}» не найден или недоступен NEO Bot. Доступные: {available or 'нет'}")

        # In hands-free mode the voice destination follows the Discord user, just
        # like a normal music bot invoked by that user.  The configured text
        # channel is unrelated to voice selection and is intentionally not used
        # here.  VoiceChannel.voice_states lets discord.py resolve this without
        # depending on the full guild-member cache.
        if auto_follow:
            user_id = _as_snowflake(profile.get("discord_user_id"))
            if user_id:
                for item in connectable:
                    try:
                        states = getattr(item, "voice_states", None)
                        if isinstance(states, dict) and user_id in states:
                            logger.info(
                                "Discord voice auto-follow selected user %s in %r (%s)",
                                user_id, item.name, item.id,
                            )
                            return item
                    except Exception:
                        pass
                    # Compatibility path for discord.py/channel mocks where
                    # voice_states is not exposed but members are cached.
                    for member in list(getattr(item, "members", []) or []):
                        if _as_snowflake(getattr(member, "id", None)) == user_id:
                            logger.info(
                                "Discord voice auto-follow selected cached user %s in %r (%s)",
                                user_id, item.name, item.id,
                            )
                            return item
                available = ", ".join(f"{item.name} ({item.id})" for item in connectable[:20])
                raise RuntimeError(
                    f"Сохранённая совместимая привязка Discord {user_id} сейчас не найдена ни в одном доступном голосовом канале. "
                    "NEO продолжит использовать автоматическое определение; зайдите в нужный voice-канал и повторите команду."
                    + (f" Доступные каналы: {available}." if available else "")
                )

            # First-run convenience: when the owner ID has not been filled in yet,
            # an unambiguous occupied channel is still safe to follow.
            occupied: list[Any] = []
            for item in connectable:
                members = list(getattr(item, "members", []) or [])
                humans = [member for member in members if not bool(getattr(member, "bot", False))]
                if humans:
                    occupied.append(item)
            if len(occupied) == 1:
                logger.info(
                    "Discord voice auto-follow selected the only human-occupied channel %r (%s)",
                    occupied[0].name, occupied[0].id,
                )
                return occupied[0]

            voice_client = getattr(guild, "voice_client", None)
            if voice_client is not None and getattr(voice_client, "channel", None) is not None:
                # Once NEO is already connected, keep the current channel if
                # owner ID is still absent rather than failing because another
                # unrelated channel later became occupied.
                return voice_client.channel

            occupied_text = ", ".join(f"{item.name} ({item.id})" for item in occupied[:20])
            if len(occupied) > 1:
                raise RuntimeError(
                    "На сервере одновременно занято несколько голосовых каналов, поэтому без ручных ID нельзя безопасно угадать нужный. "
                    "Перейдите в нужный voice-канал и оставьте его единственным активным каналом для первого подключения NEO; "
                    "после подключения он удерживает выбранный канал автоматически. "
                    f"Занятые каналы: {occupied_text}."
                )
            raise RuntimeError(
                "NEO не видит пользователя ни в одном голосовом канале. Зайдите в voice-канал и повторите команду. "
                "Для точного авто-входа укажите свой Discord User ID в Настройки → Медиа → Discord."
            )

        # Fixed-channel compatibility mode.  It is used only when the user
        # explicitly disables auto-follow in Settings.
        explicit = bool(
            str(channel or "").strip()
            or str(profile.get("voice_channel_id", "") or "").strip()
            or str(profile.get("voice_channel", "") or "").strip()
        )
        voice_client = getattr(guild, "voice_client", None)
        if voice_client is not None and getattr(voice_client, "channel", None) is not None and not explicit:
            return voice_client.channel

        channel_id = _as_snowflake(profile.get("voice_channel_id")) or _as_snowflake(channel)
        if channel_id:
            found = guild.get_channel(channel_id)
            if isinstance(found, (discord.VoiceChannel, discord.StageChannel)):
                if not self._voice_permissions_ok(guild, found):
                    raise RuntimeError(
                        f"NEO Bot видит голосовой канал «{found.name}» ({found.id}), но не имеет прав View Channel/Connect/Speak."
                    )
                return found
            logger.warning("Configured Voice Channel ID %s was not found in guild %s", channel_id, guild.id)

        wanted = _norm(channel or str(profile.get("voice_channel", "") or ""))
        if wanted:
            for item in connectable:
                if _norm(item.name) == wanted:
                    return item

        if len(connectable) == 1:
            return connectable[0]
        available = ", ".join(f"{item.name} ({item.id})" for item in connectable[:20])
        detail = f" Доступные каналы: {available}." if available else " У бота нет доступных голосовых каналов."
        raise RuntimeError(
            "Авто-вход к пользователю выключен, а резервный голосовой канал не найден автоматически. "
            "Включите авто-вход в Настройки → Медиа → Discord или используйте однозначный voice-канал." + detail
        )

    @staticmethod
    def _text_permissions_ok(guild: Any, channel: Any) -> bool:
        try:
            member = guild.me
            if member is None:
                return True
            perms = channel.permissions_for(member)
            return bool(
                getattr(perms, "view_channel", True)
                and getattr(perms, "send_messages", True)
            )
        except Exception:
            return True

    async def _resolve_text(self, guild: Any, profile: dict[str, Any], channel: str = "") -> Any:
        """Resolve a writable guild text channel without exposing numeric IDs.

        HOTFIX50 uses a short-lived bot-authored control packet to coordinate the
        one shared Discord music host across multiple PCs. A user should not need
        to know a Guild ID, User ID or Text Channel ID for that internal bus.
        Explicit legacy values remain compatible, but automatic discovery is the
        normal path.
        """
        import discord
        # A channel explicitly supplied by the command wins. Otherwise a
        # user-attached HOTFIX95 destination wins over legacy profile defaults;
        # this is what makes “подключись к каналу …” affect subsequent short
        # “напиши в Discord …” commands. Internal music control does not use this
        # blank-channel path (it targets #neo-music directly).
        explicit_channel = bool(str(channel or "").strip())
        if not explicit_channel:
            attached_id = int(self._attached_text_channel_ids.get(int(getattr(guild, "id", 0) or 0), 0) or 0)
            if attached_id:
                attached = guild.get_channel(attached_id)
                if isinstance(attached, discord.TextChannel) and self._text_permissions_ok(guild, attached):
                    return attached
                self._attached_text_channel_ids.pop(int(getattr(guild, "id", 0) or 0), None)
                self._save_text_channel_attachments()
        channel_id = _as_snowflake(profile.get("text_channel_id")) or _as_snowflake(channel)
        if channel_id:
            found = guild.get_channel(channel_id)
            if isinstance(found, discord.TextChannel) and self._text_permissions_ok(guild, found):
                return found
        wanted = _norm(channel or str(profile.get("music_text_channel", "") or ""))
        if wanted:
            for item in guild.text_channels:
                if _norm(item.name) == wanted and self._text_permissions_ok(guild, item):
                    return item

        # Prefer the guild's system channel, then the first visible writable text
        # channel in Discord's own position order. The player-facing #neo-music
        # dashboard is deliberately NOT used for ephemeral JSON bus traffic when
        # any other writable channel exists. If it is literally the only channel
        # the bot can use, it remains a last-resort fallback so coordination does
        # not silently stop.
        music_name = _norm(self._music_channel_name())
        system_channel = getattr(guild, "system_channel", None)
        if (isinstance(system_channel, discord.TextChannel)
                and self._text_permissions_ok(guild, system_channel)
                and _norm(getattr(system_channel, "name", "")) != music_name):
            logger.info(
                "Discord control bus auto-selected system text channel %r (%s)",
                system_channel.name, system_channel.id,
            )
            return system_channel
        candidates = [
            item for item in list(getattr(guild, "text_channels", []) or [])
            if isinstance(item, discord.TextChannel) and self._text_permissions_ok(guild, item)
        ]
        candidates.sort(key=lambda item: (int(getattr(item, "position", 0) or 0), int(getattr(item, "id", 0) or 0)))
        normal_candidates = [item for item in candidates if _norm(getattr(item, "name", "")) != music_name]
        if normal_candidates:
            candidates = normal_candidates
        if candidates:
            selected = candidates[0]
            logger.info(
                "Discord control bus auto-selected writable text channel %r (%s)",
                selected.name, selected.id,
            )
            return selected
        raise RuntimeError(
            "NEO Bot не нашёл ни одного текстового канала, куда у него есть права View Channel и Send Messages. "
            "Числовые Discord ID вводить не нужно — достаточно выдать боту эти права хотя бы в одном текстовом канале."
        )

    def set_control_handler(self, handler: Any | None) -> None:
        """Register a thread-safe callback for NEO control-bus packets."""
        self._control_handler = handler

    def set_music_control_handler(self, handler: Any | None) -> None:
        """Register the canonical shared-queue action handler for Discord buttons."""
        self._music_control_handler = handler

    async def _cleanup_legacy_sync_channels(self, guild: Any) -> None:
        """Remove NEO-created ``#neo-jarvis-sync`` channels from older builds.

        Internal queue coordination must not create a visible server channel. Only
        channels carrying NEO's exact internal-sync topic marker are removed, so a
        user-created channel with a similar name is never touched.
        """
        import discord
        gid = int(getattr(guild, "id", 0) or 0)
        self._sync_channel_ids.pop(gid, None)
        for channel in list(getattr(guild, "text_channels", []) or []):
            if not isinstance(channel, discord.TextChannel):
                continue
            name = str(getattr(channel, "name", "") or "").strip().casefold()
            topic = str(getattr(channel, "topic", "") or "").casefold()
            if name != "neo-jarvis-sync" or "neo jarvis internal sync" not in topic:
                continue
            try:
                await channel.delete(reason="NEO no longer uses a dedicated sync channel")
                logger.info("Removed legacy Discord sync channel guild=%s channel=%s", gid, getattr(channel, "id", ""))
            except Exception as exc:
                logger.debug("Could not remove legacy Discord sync channel: %s", exc)

    async def _ensure_sync_channel(self, guild: Any) -> Any | None:
        """Compatibility shim: dedicated sync channels are intentionally disabled."""
        self._sync_channel_ids.pop(int(getattr(guild, "id", 0) or 0), None)
        return None

    def _music_channel_name(self) -> str:
        raw = str(self.config.get("integrations.discord.music_control_channel", "neo-music") or "neo-music").strip().casefold()
        cleaned = "-".join(part for part in re.sub(r"[^a-z0-9а-яё_-]+", "-", raw, flags=re.I).split("-") if part)
        return (cleaned or "neo-music")[:90]

    async def _ensure_music_channel(self, guild: Any) -> Any | None:
        """Find/create one dedicated user-facing music channel per guild.

        Several PCs may legitimately run the same NEO bot token.  A local lock
        prevents duplicate creates inside one process; a post-create reconciliation
        makes the operation effectively idempotent across processes as well.
        """
        import discord
        gid = int(getattr(guild, "id", 0) or 0)
        lock = self._music_channel_locks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._music_channel_locks[gid] = lock
        async with lock:
            cached_id = self._music_channel_ids.get(gid)
            if cached_id:
                cached = guild.get_channel(cached_id)
                if isinstance(cached, discord.TextChannel) and self._text_permissions_ok(guild, cached):
                    return cached

            wanted = self._music_channel_name()
            marker = "NEO music control"

            def matching(channels: list[Any]) -> list[Any]:
                found: list[Any] = []
                for channel in channels:
                    if not isinstance(channel, discord.TextChannel):
                        continue
                    topic = str(getattr(channel, "topic", "") or "").casefold()
                    if (_norm(channel.name) == _norm(wanted) or marker.casefold() in topic) and self._text_permissions_ok(guild, channel):
                        found.append(channel)
                found.sort(key=lambda c: int(getattr(c, "id", 0) or 0))
                return found

            existing = matching(list(getattr(guild, "text_channels", []) or []))
            if existing:
                channel = existing[0]
                self._music_channel_ids[gid] = int(channel.id)
                return channel

            member = getattr(guild, "me", None)
            perms = getattr(member, "guild_permissions", None) if member is not None else None
            if not bool(getattr(perms, "manage_channels", False)):
                logger.warning(
                    "Discord bot lacks Manage Channels in %s; cannot create #%s",
                    getattr(guild, "name", gid), wanted,
                )
                return None

            created = await guild.create_text_channel(
                wanted,
                topic="NEO music control · now playing · shared queue",
                reason="NEO music control channel",
            )

            # Another installation using the same bot token may have raced us.
            # Let Discord's cache settle, then choose the oldest matching channel;
            # delete only the duplicate created by *this* call, never a user's
            # pre-existing channel.
            await asyncio.sleep(0.12)
            candidates = matching(list(getattr(guild, "text_channels", []) or []))
            if not candidates:
                try:
                    fetched = await guild.fetch_channels()
                    candidates = matching(list(fetched or []))
                except Exception:
                    candidates = []
            canonical = candidates[0] if candidates else created
            if int(getattr(canonical, "id", 0) or 0) != int(getattr(created, "id", 0) or 0):
                try:
                    await created.delete(reason="Duplicate NEO music channel created concurrently")
                except Exception:
                    pass
            else:
                try:
                    await canonical.send(
                        "🎧 **NEO Music**\nКанал создан. Здесь будут текущий трек, изменения очереди и управление воспроизведением.",
                        silent=True,
                    )
                except TypeError:
                    await canonical.send("🎧 **NEO Music**\nКанал создан. Здесь будут текущий трек, изменения очереди и управление воспроизведением.")
                except Exception:
                    pass
            self._music_channel_ids[gid] = int(canonical.id)
            return canonical

    async def _clear_music_channel_history(self, guild: Any) -> int:
        """Forget transient dashboard state without deleting Discord history.

        HOTFIX130: played-track messages are the durable history of ``#neo-music``
        and must live for as long as the channel exists.  Older revisions deleted
        every message when leaving voice; that is no longer permitted.
        """
        gid = int(getattr(guild, "id", 0) or 0)
        self._music_last_state.pop(gid, None)
        self._music_dashboard_signatures.pop(gid, None)
        self._music_track_history.pop(gid, None)
        self._music_current_key.pop(gid, None)
        self._music_current_meta.pop(gid, None)
        logger.info("Discord music history preserved guild=%s", gid)
        return 0

    @staticmethod
    def _message_has_music_controls(message: Any) -> bool:
        for row in list(getattr(message, "components", []) or []):
            for child in list(getattr(row, "children", []) or []):
                if str(getattr(child, "custom_id", "") or "").startswith("neo_music:"):
                    return True
        return False

    async def _find_music_dashboard(self, channel: Any) -> Any | None:
        """Recover the canonical dashboard after restart/multi-PC attach."""
        client_user = getattr(self._client, "user", None)
        bot_id = int(getattr(client_user, "id", 0) or 0)
        if not bot_id:
            return None
        matches: list[Any] = []
        try:
            async for message in channel.history(limit=200):
                if int(getattr(getattr(message, "author", None), "id", 0) or 0) != bot_id:
                    continue
                if self._message_has_music_controls(message):
                    matches.append(message)
        except Exception:
            return None
        if not matches:
            return None
        # Prefer the newest canonical dashboard. This keeps the controls at the
        # bottom of DJ NEO's own message history after migrations/restarts.
        matches.sort(key=lambda m: int(getattr(m, "id", 0) or 0), reverse=True)
        return matches[0]

    async def _remove_music_dashboard(self, guild: Any, channel: Any | None = None) -> None:
        """Delete only the live control menu; persistent played-track history stays."""
        gid = int(getattr(guild, "id", 0) or 0)
        if channel is None:
            cached_id = int(self._music_channel_ids.get(gid, 0) or 0)
            if cached_id:
                try:
                    channel = guild.get_channel(cached_id)
                except Exception:
                    channel = None
            if channel is None:
                wanted = _norm(self._music_channel_name())
                for candidate in list(getattr(guild, "text_channels", []) or []):
                    topic = str(getattr(candidate, "topic", "") or "").casefold()
                    if _norm(str(getattr(candidate, "name", "") or "")) == wanted or "neo music control" in topic:
                        channel = candidate
                        break
        message = self._music_dashboard_messages.get(gid)
        if message is None and channel is not None:
            message = await self._find_music_dashboard(channel)
        if message is not None:
            try:
                await message.delete()
            except Exception as exc:
                logger.debug("Discord music dashboard delete failed guild=%s: %s", gid, exc)
        self._music_dashboard_messages.pop(gid, None)
        self._music_dashboard_ids.pop(gid, None)
        self._music_dashboard_signatures.pop(gid, None)
        logger.info("Discord music controls removed; played history preserved guild=%s", gid)

    async def _cleanup_music_channel_clutter(self, channel: Any, *, keep_message_id: int = 0) -> None:
        """Remove only internal bus packets and duplicate control dashboards.

        HOTFIX130 deliberately preserves ordinary played-track messages forever.
        They are user-facing history, not transport clutter.
        """
        client_user = getattr(self._client, "user", None)
        bot_id = int(getattr(client_user, "id", 0) or 0)
        if not bot_id:
            return
        try:
            async for message in channel.history(limit=80):
                if int(getattr(message, "id", 0) or 0) == int(keep_message_id or 0):
                    continue
                if int(getattr(getattr(message, "author", None), "id", 0) or 0) != bot_id:
                    continue
                content = str(getattr(message, "content", "") or "")
                folded = content.strip().casefold()
                is_bus = _CONTROL_MARKER.casefold() in folded
                is_extra_dashboard = self._message_has_music_controls(message)
                if not (is_bus or is_extra_dashboard):
                    continue
                try:
                    await message.delete()
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def _music_entry_key(entry: dict[str, Any] | None) -> str:
        raw = dict(entry or {})
        return "|".join((
            str(raw.get("track_id", "") or ""),
            str(raw.get("title", "") or raw.get("query", "") or "").casefold(),
            str(raw.get("artist", "") or "").casefold(),
        ))

    @staticmethod
    def _music_label(entry: dict[str, Any] | None) -> str:
        raw = dict(entry or {})
        title = str(raw.get("title", "") or raw.get("query", "") or "Неизвестный трек").strip()
        artist = str(raw.get("artist", "") or "").strip()
        return title + (f" — {artist}" if artist else "")

    def _music_queue_label(self, entry: dict[str, Any] | None) -> str:
        raw = dict(entry or {})
        label = self._music_label(raw)
        requester = str(raw.get("added_by", "") or raw.get("requester", "") or "").strip()
        if requester and requester.casefold() not in {"discord", "neo jarvis", "jarvis"}:
            label += f" · 👤 {requester}"
        return label

    def _music_view(self, profile: dict[str, Any], state: dict[str, Any]) -> Any:
        import discord
        view = discord.ui.View(timeout=None)
        paused = bool(state.get("paused", False))
        has_current = bool(state.get("current"))
        shuffle_enabled = bool(state.get("shuffle", False))
        repeat_mode = str(state.get("repeat_mode", "off") or "off").casefold()
        buttons = [
            ("⏮ Назад", "previous", discord.ButtonStyle.secondary, 0, True),
            ("▶️ Продолжить" if paused else "⏸ Пауза", "toggle", discord.ButtonStyle.primary, 0, True),
            ("⏭ Далее", "next", discord.ButtonStyle.secondary, 0, True),
            ("🧹 Очистить очередь", "clear", discord.ButtonStyle.danger, 0, True),
            ("🔀 Shuffle ON" if shuffle_enabled else "🔀 Shuffle OFF", "shuffle", discord.ButtonStyle.success if shuffle_enabled else discord.ButtonStyle.secondary, 1, False),
            ("🔂 Повтор трека", "repeat_track", discord.ButtonStyle.success if repeat_mode == "track" else discord.ButtonStyle.secondary, 1, False),
            ("🔁 Повтор плейлиста", "repeat_playlist", discord.ButtonStyle.success if repeat_mode == "playlist" else discord.ButtonStyle.secondary, 1, False),
        ]
        for label, action, style, row, needs_current in buttons:
            button = discord.ui.Button(
                label=label, style=style, custom_id=f"neo_music:{action}", row=row,
                disabled=bool(needs_current and not has_current),
            )
            async def callback(interaction: Any, _action: str = action) -> None:
                if not self._enabled():
                    return
                # With the same bot token on multiple PCs, only the Gateway
                # session that successfully acknowledges the interaction owns it.
                try:
                    # Component acknowledgement without a visible ephemeral reply.
                    # Successful clicks are reflected by the dashboard itself, so
                    # no extra "Готово" message can appear below the controls.
                    await interaction.response.defer()
                except Exception:
                    return
                handler = self._music_control_handler
                if handler is None:
                    try:
                        await interaction.followup.send("NEO: управление очередью ещё не готово.", ephemeral=True)
                    except Exception:
                        pass
                    return
                interaction_guild = getattr(interaction, "guild", None)
                guild_id = int(getattr(interaction_guild, "id", 0) or 0)
                live_profile = dict(self._music_profiles.get(guild_id) or profile or {})
                if not live_profile and interaction_guild is not None:
                    # Persistent views may be clicked before the first queue-state
                    # publication after restart. Guild identity is enough for the
                    # canonical queue adapter to resolve the configured profile.
                    live_profile = {
                        "guild_id": str(guild_id),
                        "name": str(getattr(interaction_guild, "name", "") or ""),
                    }
                try:
                    await asyncio.to_thread(handler, live_profile, _action)
                    # Success stays silent: the canonical music dashboard is the
                    # feedback surface and remains the bottom-most visible message.
                except Exception as exc:
                    try:
                        await interaction.followup.send(f"Не удалось выполнить команду: {exc}"[:1800], ephemeral=True)
                    except Exception:
                        pass
            button.callback = callback
            view.add_item(button)
        return view

    async def _append_played_track_history(self, channel: Any, entry: dict[str, Any] | None) -> None:
        """Append one permanent, ordinary Discord message for a finished track.

        The message intentionally has no buttons/markers used by dashboard cleanup,
        so reconnects, leave/join cycles and future NEO launches never erase it.
        """
        label = self._music_label(entry)
        if not label:
            return
        try:
            await channel.send(f"🕘 **Проиграно:** {label}"[:1900])
        except Exception as exc:
            logger.debug("Discord played-track history append failed: %s", exc)

    def _music_dashboard_text(self, state: dict[str, Any], gid: int = 0) -> str:
        current = dict(state.get("current") or {})
        if current:
            # The buttons already show pause/shuffle/repeat state. Keep the text
            # area focused only on the track identity, as a compact Now Playing.
            head = f"🎵 **Сейчас играет**\n{self._music_label(current)}"
        else:
            head = "🎵 **Сейчас ничего не играет**"
        queue = list(state.get("queue") or [])
        if queue:
            # The music console always shows at most the next five tracks.  A
            # larger prefetch cushion may exist internally, but it is deliberately
            # hidden from the control message.
            lines = [f"{idx}. {self._music_queue_label(item)}" for idx, item in enumerate(queue[:5], 1)]
            queue_text = "\n\n📋 **Следующие 5**\n" + "\n".join(lines)
        else:
            queue_text = ""
        # No previous-track history and no timer rows in this message. Played
        # tracks are normal persistent channel messages; timers have their own
        # live panel. This keeps controls stable and prevents giant edits.
        return (head + queue_text)[:1900]

    async def _publish_music_state(self, profile: dict[str, Any], state: dict[str, Any], *, reanchor: bool = False) -> str:
        # ``reanchor`` remains in the public signature for compatibility with old
        # peers. HOTFIX133 keeps normal state changes as in-place edits, moves the
        # controls to the bottom only once after a permanent played-track history
        # append, and removes controls entirely when NEO is no longer in voice.
        # HIST-019: the result names what actually happened to the dashboard:
        # completed / unchanged / removed / pending (slow edit still running) /
        # failed / skipped. Only the first three are an observed completion.
        admitted = self.admission()
        if admitted is None:
            return "skipped"
        guild = await self._resolve_guild(profile)
        gid = int(guild.id)
        self._music_profiles[gid] = dict(profile)
        channel = await self._ensure_music_channel(guild)
        if channel is None or self.admission() != admitted:
            return self._dashboard_outcome188(gid, "skipped", "no_channel_or_admission")
        # HOTFIX133 invariant: never trust a delayed telemetry snapshot more than
        # Discord's live voice state. A queued state update can arrive after the
        # bot has left voice; without this guard it could recreate dead controls.
        voice = getattr(guild, "voice_client", None)
        member_voice = getattr(getattr(guild, "me", None), "voice", None)
        live_connected = (
            (voice is not None and getattr(voice, "channel", None) is not None)
            or (member_voice is not None and getattr(member_voice, "channel", None) is not None)
        )
        if not live_connected:
            self._music_last_state[gid] = dict(state)
            await self._remove_music_dashboard(guild, channel)
            return self._dashboard_outcome188(gid, "removed")
        if "connected" in state and not bool(state.get("connected")):
            self._music_last_state[gid] = dict(state)
            await self._remove_music_dashboard(guild, channel)
            return self._dashboard_outcome188(gid, "removed")
        lock = self._music_dashboard_locks.get(gid)
        if lock is None:
            lock = asyncio.Lock()
            self._music_dashboard_locks[gid] = lock
        async with lock:
            # HOTFIX97: queue/track state updates edit one canonical message in
            # place. Moving it to the bottom is a separate, debounced operation
            # triggered by actual chat activity. This avoids edit->send->delete
            # churn when several state broadcasts arrive around the same track
            # transition.
            history_appended = False
            current_for_history = dict(state.get("current") or {})
            current_key = str(current_for_history.get("track_id", "") or "").strip()
            if not current_key and current_for_history:
                current_key = "|".join((
                    str(current_for_history.get("artist", "") or "").strip().casefold(),
                    str(current_for_history.get("title", "") or current_for_history.get("query", "") or "").strip().casefold(),
                ))
            previous_state = dict(self._music_last_state.get(gid) or {})
            previous_current = dict(self._music_current_meta.get(gid) or previous_state.get("current") or {})
            previous_key = str(self._music_current_key.get(gid, "") or "")
            if previous_key and current_key and previous_key != current_key and previous_current:
                previous_label = self._music_label(previous_current)
                if previous_label:
                    rows = self._music_track_history.setdefault(gid, [])
                    if not rows or rows[-1] != previous_label:
                        rows.append(previous_label)
                        del rows[:-8]
                        # Persistent history is a normal Discord message, not part
                        # of the editable control dashboard. Never delete it later.
                        await self._append_played_track_history(channel, previous_current)
                        history_appended = True
            if current_key:
                self._music_current_key[gid] = current_key
                self._music_current_meta[gid] = dict(current_for_history)
            text = self._music_dashboard_text(state, gid)
            paused = bool(state.get("paused", False))
            has_current = bool(state.get("current"))
            shuffle_enabled = bool(state.get("shuffle", False))
            repeat_mode = str(state.get("repeat_mode", "off") or "off").casefold()
            # Button colors/labels are part of the rendered state even though the
            # compact text no longer repeats Shuffle/Repeat status.
            signature = (
                f"{text}\n<paused={int(paused)};current={int(has_current)};"
                f"shuffle={int(shuffle_enabled)};repeat={repeat_mode}>"
            )
            view = self._music_view(profile, state)
            message = self._music_dashboard_messages.get(gid)
            if message is None:
                message = await self._find_music_dashboard(channel)

            if history_appended and message is not None:
                # Re-anchor exactly once per finished-track history append. This
                # keeps history above the controls without the old per-update
                # recreate churn that caused Discord white/skeleton rows.
                try:
                    await message.delete()
                except Exception:
                    pass
                message = None
                self._music_dashboard_messages.pop(gid, None)
                self._music_dashboard_ids.pop(gid, None)
                self._music_dashboard_signatures.pop(gid, None)

            signature_changed = self._music_dashboard_signatures.get(gid) != signature
            slow_edit = False
            created_or_recovered = False
            initial_creation = message is None

            if message is not None and signature_changed:
                # HOTFIX131: Discord may spend several seconds editing a message
                # during a track transition.  Never interpret a transient edit
                # timeout as "dashboard vanished" and create a second control
                # message: that recreate/delete churn is what users see as white
                # skeleton rows.  Let a slow edit finish in the background and
                # invalidate the cached signature only if it eventually fails.
                edit_task = asyncio.create_task(message.edit(content=text, view=view))
                done, _pending = await asyncio.wait({edit_task}, timeout=1.25)
                if edit_task in done:
                    try:
                        edit_task.result()
                    except Exception as exc:
                        logger.debug("Discord dashboard in-place edit failed; keeping canonical message: %r", exc)
                        # The message object/id is still canonical. Do not create a
                        # replacement on a network/API failure; the next state
                        # publication will retry the same message in place.
                        self._music_dashboard_signatures.pop(gid, None)
                        self._music_last_state[gid] = dict(state)
                        return self._dashboard_outcome188(gid, "failed", repr(exc)[:200])
                else:
                    expected_signature = signature

                    def _finish_slow_dashboard_edit(task: Any) -> None:
                        try:
                            task.result()
                        except Exception as exc:
                            logger.debug("Discord slow dashboard edit eventually failed: %r", exc)
                            if self._music_dashboard_signatures.get(gid) == expected_signature:
                                self._music_dashboard_signatures.pop(gid, None)
                            self._dashboard_outcome188(gid, "failed", repr(exc)[:200])
                        else:
                            self._dashboard_outcome188(gid, "completed")

                    edit_task.add_done_callback(_finish_slow_dashboard_edit)
                    slow_edit = True

            if message is None:
                # Creation is allowed only when no canonical dashboard exists at
                # all (first run/recovery after a deleted channel message).
                message = await channel.send(text, view=view)
                created_or_recovered = True

            if message is None:
                return self._dashboard_outcome188(gid, "failed", "send_returned_nothing")
            self._music_dashboard_messages[gid] = message
            self._music_dashboard_ids[gid] = int(message.id)
            self._music_last_state[gid] = dict(state)
            self._music_dashboard_signatures[gid] = signature
            # Cleanup is allowed only on first creation and removes internal bus
            # packets/duplicate *control* messages, never played-track history.
            if initial_creation and created_or_recovered:
                await self._cleanup_music_channel_clutter(channel, keep_message_id=int(message.id))
            if slow_edit:
                return self._dashboard_outcome188(gid, "pending", "edit_in_progress")
            return self._dashboard_outcome188(gid, "completed" if signature_changed or created_or_recovered else "unchanged")

    def _dashboard_outcome188(self, gid: int, status: str, reason: str = "") -> str:
        outcomes = self.__dict__.setdefault("_music_dashboard_outcomes188", {})
        outcomes[int(gid)] = {"status": status, "reason": reason, "at": time.monotonic()}
        return status

    def music_dashboard_outcome(self, gid: int) -> dict[str, Any]:
        """The last observed dashboard result for one guild (status and failure reason)."""
        return dict(self.__dict__.get("_music_dashboard_outcomes188", {}).get(int(gid)) or {})

    def publish_music_state(
        self, profile: dict[str, Any], state: dict[str, Any], *,
        wait: bool = False, reanchor: bool = False,
    ) -> bool:
        """Publish the canonical #neo-music dashboard state.

        Playback transitions use ``wait=True`` from their own background helper so
        the message is guaranteed to reflect the actual track before the command
        is acknowledged. Passive observer refreshes remain non-blocking.
        """
        if self._closing.is_set():
            return False
        try:
            self.ensure_ready()
            with self._lock:
                loop = self._loop
            if loop is None or loop.is_closed():
                return False
            future = asyncio.run_coroutine_threadsafe(
                self._publish_music_state(dict(profile), dict(state), reanchor=bool(reanchor)), loop
            )
            if wait:
                # HIST-019: a waited publication is True only for an observed
                # completion, not for a caught failure or a still-running edit.
                # (A None outcome comes from minimal hosts that report nothing.)
                outcome = future.result(timeout=2.2)
                return outcome is None or outcome in {"completed", "unchanged", "removed"}
            return True                    # non-blocking: the update was accepted
        except Exception as exc:
            logger.warning("Discord music dashboard update failed: %s: %r", type(exc).__name__, exc)
            return False

    async def _voice_presence(self, profile: dict[str, Any]) -> dict[str, Any]:
        guild = await self._resolve_guild(profile)
        try:
            target = await self._resolve_voice(guild, profile, "")
        except Exception as original_exc:
            # A freshly started NEO may not know this installation's Discord
            # user/channel yet, while another NEO bot is already playing in a
            # voice channel. Discover a *unique* existing NEO host instead of
            # reporting no music until the first local Discord command.
            configured = str(profile.get("jarvis_bot_name", "") or "").strip().casefold()
            host_channels = []
            for channel in list(getattr(guild, "voice_channels", []) or []) + list(getattr(guild, "stage_channels", []) or []):
                members = list(getattr(channel, "members", []) or [])
                bot_members = [m for m in members if bool(getattr(m, "bot", False))]
                likely = []
                for member in bot_members:
                    name = str(getattr(member, "display_name", "") or getattr(member, "name", "") or "")
                    if re.search(r"(?:jarvis|neo\s*assistant|джарвис)", name, re.I) or (configured and name.casefold() == configured):
                        likely.append(member)
                if likely:
                    host_channels.append(channel)
            # Prefer the explicitly configured voice channel if it contains a host.
            wanted_id = _as_snowflake(profile.get("voice_channel_id"))
            wanted_name = _norm(str(profile.get("voice_channel", "") or ""))
            preferred = [c for c in host_channels if (wanted_id and int(getattr(c, "id", 0) or 0) == wanted_id) or (wanted_name and _norm(getattr(c, "name", "")) == wanted_name)]
            if len(preferred) == 1:
                target = preferred[0]
            elif len(host_channels) == 1:
                target = host_channels[0]
                logger.info("Discord observer discovered existing NEO music host in %r (%s)", target.name, target.id)
            else:
                raise original_exc
        local_voice = getattr(guild, "voice_client", None)
        local_connected = bool(
            local_voice is not None
            and getattr(local_voice, "is_connected", lambda: False)()
        )
        client = self._client
        self_id = int(getattr(getattr(client, "user", None), "id", 0) or 0)
        candidates: list[dict[str, Any]] = []
        seen: set[int] = set()
        for member in list(getattr(target, "members", []) or []):
            mid = int(getattr(member, "id", 0) or 0)
            if not mid or mid in seen or not bool(getattr(member, "bot", False)):
                continue
            seen.add(mid)
            candidates.append({"id": str(mid), "name": str(getattr(member, "display_name", "") or getattr(member, "name", "") or mid)})
        states = getattr(target, "voice_states", None)
        if isinstance(states, dict):
            for raw_id in states:
                try:
                    mid = int(raw_id)
                except Exception:
                    continue
                if mid in seen:
                    continue
                member = guild.get_member(mid) if hasattr(guild, "get_member") else None
                if member is not None and bool(getattr(member, "bot", False)):
                    seen.add(mid)
                    candidates.append({"id": str(mid), "name": str(getattr(member, "display_name", "") or getattr(member, "name", "") or mid)})
        # When the same bot token is used by two NEO instances, another
        # process may own this account's voice state even though this process has
        # no local VoiceClient. Keep our own ID as a probe candidate in that case.
        if self_id and self_id not in seen and isinstance(states, dict) and self_id in states:
            candidates.append({"id": str(self_id), "name": str(getattr(getattr(client, "user", None), "name", "") or self_id)})
        return {
            "server": str(getattr(guild, "name", "") or ""),
            "guild_id": str(getattr(guild, "id", "") or ""),
            "target_voice_channel": str(getattr(target, "name", "") or ""),
            "target_voice_channel_id": str(getattr(target, "id", "") or ""),
            "self_bot_id": str(self_id or ""),
            "local_connected": local_connected,
            "bot_candidates": candidates,
            "verified": True,
        }

    def voice_presence(self, profile: dict[str, Any]) -> dict[str, Any]:
        return dict(self._submit(self._voice_presence(profile), timeout=12.0))

    async def _send_control(
        self, profile: dict[str, Any], payload: dict[str, Any], *,
        target_bot_id: str, channel: str = "",
    ) -> dict[str, Any]:
        admitted = self.admission()
        self._require_admitted(admitted)
        guild = await self._resolve_guild(profile)
        # Internal shared-queue traffic never gets its own server channel. An
        # explicitly supplied transport channel is honored; otherwise the short-
        # lived spoiler packet travels through the existing #neo-music channel and
        # is deleted immediately after Gateway peers have observed it.
        if str(channel or "").strip():
            target = await self._resolve_text(guild, profile, channel)
        else:
            target = await self._ensure_music_channel(guild)
            if target is None:
                raise RuntimeError("NEO не может открыть #neo-music для общей очереди Discord")
        packet = dict(payload or {})
        if isinstance(packet.get("data"), dict):
            packet["data"] = _public_control_data(packet["data"])
        if packet.get("error"):
            # Provider exceptions may embed credentials or local cache paths.
            packet["error"] = "The remote NEO operation failed; inspect its local diagnostics."
        packet["target_bot_id"] = str(target_bot_id or "").strip()
        mention = f"<@{packet['target_bot_id']}> " if packet["target_bot_id"] else ""
        # Discord's Message Content policy exposes bot-authored control packets
        # reliably when this application is mentioned.  Shared-queue broadcasts
        # intentionally have no target_bot_id because every NEO instance using
        # the same bot token must consume them.  Mention the *shared bot account*
        # without converting the packet into a directed message.  This fixes the
        # cold-attach case where another user started music before this NEO sent
        # its first Discord command.
        kind = str(packet.get("kind", "") or "").strip().casefold()
        if not mention and kind in {"queue_state", "queue_probe"}:
            client_user = getattr(self._client, "user", None)
            self_bot_id = str(getattr(client_user, "id", "") or "").strip()
            if self_bot_id:
                mention = f"<@{self_bot_id}> "
                packet["broadcast_mention_bot_id"] = self_bot_id
        raw_body = mention + _CONTROL_MARKER + " " + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
        # Keep transport data visually hidden even in the tiny interval before
        # deletion. The mention stays inside the spoiler and still makes the
        # application-authored packet observable to peer NEO instances.
        body = "||" + raw_body + "||"
        self._require_admitted(admitted)
        # Discord messages are limited to 2000 characters. Passive queue-state
        # broadcasts are best-effort metadata, so trim only their preview rather
        # than failing the entire observer path when titles happen to be long.
        if len(body) > 1900 and str(packet.get("kind", "") or "").casefold() == "queue_state":
            data = packet.get("data") if isinstance(packet.get("data"), dict) else {}
            queue_preview = list(data.get("queue") or [])
            while queue_preview and len(body) > 1900:
                queue_preview.pop()
                data["queue"] = queue_preview
                packet["data"] = data
                raw_body = mention + _CONTROL_MARKER + " " + json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
                body = "||" + raw_body + "||"
        try:
            message = await target.send(body, silent=True)
        except TypeError:
            message = await target.send(body)

        async def cleanup() -> None:
            # Packet transport is not user content. Peers receive the Gateway
            # event immediately; remove the message almost at once so #neo-music
            # remains visually clean and the dashboard becomes the final bot post.
            await asyncio.sleep(0.12)
            try:
                await message.delete()
            except Exception:
                pass
        asyncio.create_task(cleanup())
        return {
            "server": guild.name, "guild_id": str(guild.id),
            "channel": target.name, "text_channel_id": str(target.id),
            "message_id": str(message.id), "verified": True,
            "method": "discord_jarvis_control_bus", "non_invasive": True,
        }

    def send_control(
        self, profile: dict[str, Any], payload: dict[str, Any], *,
        target_bot_id: str = "", channel: str = "",
    ) -> dict[str, Any]:
        # Empty target_bot_id is an intentional broadcast.  Queue-state/probe
        # packets are read-only and every NEO instance in the control channel
        # must be able to observe them without waiting for a request/ACK pair.
        return dict(self._submit(
            self._send_control(profile, payload, target_bot_id=str(target_bot_id or ""), channel=channel),
            timeout=15.0,
        ))

    def _ffmpeg_candidates(self) -> list[str]:
        """Return executable candidates in reliability order.

        The packaged imageio-ffmpeg binary is preferred over arbitrary PATH
        shims. A user-configured real file remains highest priority.
        """
        values: list[str] = []
        configured = str(self.config.get("integrations.discord.ffmpeg_path", "") or "").strip()
        if configured and os.path.isfile(configured):
            values.append(os.path.abspath(configured))
        try:
            import imageio_ffmpeg
            candidate = str(imageio_ffmpeg.get_ffmpeg_exe() or "").strip()
            if candidate and os.path.isfile(candidate):
                values.append(os.path.abspath(candidate))
        except Exception as exc:
            logger.debug("imageio-ffmpeg candidate unavailable: %s", exc)
        system = shutil.which("ffmpeg")
        if system and os.path.isfile(system):
            values.append(os.path.abspath(system))
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            key = os.path.normcase(os.path.abspath(value))
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    @staticmethod
    def _hidden_subprocess_kwargs() -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
            if startupinfo_cls is not None:
                try:
                    startupinfo = startupinfo_cls()
                    startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 1))
                    startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
                    kwargs["startupinfo"] = startupinfo
                except Exception:
                    pass
        return kwargs

    def _ffmpeg_self_test(self, executable: str) -> bool:
        kwargs = self._hidden_subprocess_kwargs()
        try:
            completed = subprocess.run(
                [executable, "-version"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5.0, check=False, **kwargs,
            )
            return int(completed.returncode or 0) == 0
        except OSError as exc:
            # discord.py itself uses CREATE_NO_WINDOW for FFmpegAudio. The user's
            # field log shows Windows returning WinError 50 for that request even
            # though the Discord voice handshake succeeds. Verify that ffmpeg can
            # still spawn without the optional no-console creation flag.
            text = str(exc or "").casefold()
            if "winerror 50" in text or "запрос не поддерживается" in text or "request is not supported" in text:
                try:
                    fallback_kwargs = dict(kwargs)
                    fallback_kwargs.pop("creationflags", None)
                    completed = subprocess.run(
                        [executable, "-version"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=5.0, check=False, **fallback_kwargs,
                    )
                    if int(completed.returncode or 0) == 0:
                        logger.warning("Discord FFmpeg self-test required WinError50 compatibility spawn: %s", executable)
                        return True
                except Exception as retry_exc:
                    logger.warning("Discord FFmpeg compatibility self-test failed %s: %s", executable, retry_exc)
            logger.warning("Discord FFmpeg candidate rejected %s: %s", executable, exc)
            return False
        except Exception as exc:
            logger.warning("Discord FFmpeg candidate rejected %s: %s", executable, exc)
            return False

    def _ffmpeg_executables(self, *, fast: bool = False) -> list[str]:
        candidates = self._ffmpeg_candidates()
        verified: list[str] = []
        preferred = str(self._ffmpeg_verified_path or "").strip()
        if preferred and preferred in candidates and os.path.normcase(os.path.abspath(preferred)) not in self._ffmpeg_rejected_paths:
            verified.append(preferred)
        if fast and verified:
            # The verified executable has already passed both startup self-test
            # and real playback.  Do not run ``ffmpeg -version`` subprocesses on
            # the latency-critical next/previous path. Runtime fallback below can
            # still try the remaining candidates if this one unexpectedly fails.
            for candidate in candidates:
                norm = os.path.normcase(os.path.abspath(candidate))
                if candidate not in verified and norm not in self._ffmpeg_rejected_paths:
                    verified.append(candidate)
            return verified
        for candidate in candidates:
            norm = os.path.normcase(os.path.abspath(candidate))
            if candidate in verified or norm in self._ffmpeg_rejected_paths:
                continue
            if self._ffmpeg_self_test(candidate):
                verified.append(candidate)
        if not verified:
            raise RuntimeError(
                "FFmpeg не найден или не запускается. Восстановление зависимостей NEO автоматически переустанавливает imageio-ffmpeg; "
                "перезапустите NEO либо укажите рабочий ffmpeg.exe в Настройки → Медиа → Discord."
            )
        self._ffmpeg_verified_path = verified[0]
        return verified

    def _mark_ffmpeg_runtime_failure(self, executable: str, exc: Exception) -> None:
        text = str(exc or "").casefold()
        # WinError 50 is the field failure from the attached log. Treat spawn-like
        # OS failures as executable/runtime-specific and immediately try the next
        # verified ffmpeg candidate instead of failing the entire Discord command.
        if isinstance(exc, OSError) or "winerror 50" in text or "request is not supported" in text or "запрос не поддерживается" in text:
            norm = os.path.normcase(os.path.abspath(executable))
            self._ffmpeg_rejected_paths.add(norm)
            if os.path.normcase(os.path.abspath(self._ffmpeg_verified_path or executable)) == norm:
                self._ffmpeg_verified_path = ""

    def _ffmpeg_executable(self) -> str:
        return self._ffmpeg_executables()[0]

    @staticmethod
    def _managed_cache_roots() -> list[str]:
        configured = str(os.getenv("NEOJARVIS_CACHE_DIR", "") or "").strip()
        if configured:
            base = os.path.abspath(configured)
        else:
            local = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or ""
            if not local:
                return []
            base = os.path.join(local, "NeoJarvis", "cache")
        return [
            os.path.normcase(os.path.abspath(os.path.join(base, "yandex_music"))),
            os.path.normcase(os.path.abspath(os.path.join(base, "youtube_audio"))),
        ]

    @classmethod
    def _managed_local_source(cls, source: str) -> str:
        source = str(source or "").strip()
        if not source or not os.path.isfile(source):
            return ""
        candidate = os.path.normcase(os.path.abspath(source))
        for root in cls._managed_cache_roots():
            try:
                if os.path.commonpath([root, candidate]) == root:
                    return os.path.abspath(source)
            except Exception:
                continue
        return ""

    def _delete_file_with_retries(self, path: str) -> None:
        if not path:
            return
        normalized = os.path.normcase(os.path.abspath(path))
        for delay in (0.12, 0.35, 0.8, 1.5, 2.5):
            if delay:
                time.sleep(delay)
            try:
                with self._lock:
                    # Cleanup can sleep while a newer generation reuses this
                    # same cache file. Test ownership at deletion time, not only
                    # when the old after-callback scheduled the cleanup thread.
                    if any(os.path.normcase(os.path.abspath(active)) == normalized
                           for active in self._active_local_sources.values()):
                        return
                    if not os.path.exists(path):
                        return
                    os.remove(path)
                logger.info("Discord temporary managed media track removed: %s", path)
                return
            except PermissionError:
                continue
            except FileNotFoundError:
                return
            except Exception as exc:
                logger.debug("Could not remove Discord temporary track %s: %s", path, exc)
                return
        logger.warning("Discord temporary managed media track is still locked and will be cleaned later: %s", path)

    def _finish_playback(self, guild_id: int, generation: int, local_path: str, error: Exception | None) -> None:
        # Successful cached tracks are retained by DiscordNavigation's five-entry
        # history so Previous is instant. Failed sources are safe to discard.
        cleanup = bool(local_path and error is not None)
        with self._lock:
            current_generation = self._playback_generation.get(guild_id, 0)
            current_path = self._active_local_sources.get(guild_id, "")
            if error is not None and generation == current_generation:
                self._playback_errors[guild_id] = str(error)
            if generation == current_generation:
                self._last_finished_generation[guild_id] = int(generation)
                self._last_finished_at[guild_id] = time.monotonic()
                self._last_finished_error[guild_id] = str(error or "")
                self._active_local_sources.pop(guild_id, None)
                # Hold the ownership lock through publication: a new _play may
                # otherwise reserve its generation before this old callback
                # clears that newer player's levels.
                clear_media_levels("discord")
            elif local_path and current_path and os.path.normcase(current_path) == os.path.normcase(local_path):
                # The user requested the same track again before the previous
                # FFmpeg instance finished shutting down. The newer player owns
                # the file now, so the older callback must not delete it.
                cleanup = False
        if error is not None:
            logger.warning("Discord audio player finished with error: %s", error)
        else:
            logger.info("Discord audio player finished for guild %s", guild_id)
        if cleanup:
            threading.Thread(
                target=self._delete_file_with_retries, args=(local_path,),
                name="jarvis-discord-track-cleanup", daemon=True,
            ).start()

    async def _connect_voice_bounded(
        self, guild: Any, target: Any, *, before_start: Callable[[], None] | None = None,
    ) -> Any:
        """Connect with a short deterministic retry instead of a 30 s hang.

        Field logs showed the first discord.py handshake reaching a media endpoint
        immediately, then timing out 30 seconds later; a second connect completed
        in under a second.  A late cleanup from that first attempt could also
        invalidate the successful VoiceClient.  We fully dispose a failed client
        before one bounded retry.
        """
        try:
            timeout = float(self.config.get("integrations.discord.voice_connect_timeout_seconds", 6.0) or 6.0)
        except Exception:
            timeout = 6.0
        timeout = max(3.0, min(10.0, timeout))
        last_error: BaseException | None = None
        for attempt in range(2):
            try:
                if self._closing.is_set():
                    raise MediaRequestSuperseded("Discord background bot завершает работу")
                if before_start is not None:
                    before_start()
                voice = await target.connect(self_deaf=True, timeout=timeout, reconnect=False)
                if self._closing.is_set():
                    raise MediaRequestSuperseded("Discord background bot завершает работу")
                if before_start is not None:
                    before_start()
                return voice
            except (MediaRequestSuperseded, asyncio.CancelledError):
                raise
            except BaseException as exc:
                if before_start is not None:
                    before_start()
                last_error = exc
                logger.warning(
                    "Discord voice connect attempt %d/2 failed after <=%.1fs: %s",
                    attempt + 1, timeout, exc or type(exc).__name__,
                )
                stale = getattr(guild, "voice_client", None)
                if stale is not None:
                    try:
                        await stale.disconnect(force=True)
                    except Exception:
                        pass
                # Let discord.py remove its voice-client registration completely
                # before the retry, preventing the old timeout cleanup from later
                # deleting the newly connected client.
                await asyncio.sleep(0.28 if attempt == 0 else 0.05)
        raise RuntimeError(
            "Discord не смог стабильно подключить NEO Bot к голосовому каналу"
            + (f": {last_error}" if str(last_error or "").strip() else " (voice handshake timeout)")
        )

    def _bind_voice_owner(self, guild: Any, profile: dict[str, Any]) -> None:
        """Owner identity is explicit; an arbitrary voice participant is not it."""
        gid = int(getattr(guild, "id", 0) or 0)
        owner = _as_snowflake(profile.get("discord_user_id"))
        if owner:
            self._voice_owner_ids[gid] = owner
            # Never infer an owner from who happened to join last.
        elif "discord_user_id" in profile:
            self._voice_owner_ids.pop(gid, None)
            self._owner_voice_channels.pop(gid, None)
        self._music_profiles[gid] = {**self._music_profiles.get(gid, {}), **profile}

    @staticmethod
    def _member_is_in_channel(channel: Any, member_id: int) -> bool:
        states = getattr(channel, "voice_states", None)
        if isinstance(states, dict):
            return member_id in states or str(member_id) in states
        return any(int(getattr(member, "id", 0) or 0) == member_id
                   for member in list(getattr(channel, "members", []) or []))

    async def _handle_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        """Debounced owner-leave and empty-room cleanup; other humans may remain."""
        try:
            client = self._client
            if self._closing.is_set() or client is None or client.user is None:
                return
            guild = getattr(member, "guild", None)
            if guild is None:
                return
            gid = int(getattr(guild, "id", 0) or 0)
            member_id = int(getattr(member, "id", 0) or 0)
            before_id = int(getattr(getattr(before, "channel", None), "id", 0) or 0)
            after_id = int(getattr(getattr(after, "channel", None), "id", 0) or 0)
            if before_id == after_id:
                return  # Mute/deafen/video changes do not reset the leave debounce.
            if member_id == int(getattr(client.user, "id", 0) or 0):
                if getattr(before, "channel", None) is not None and getattr(after, "channel", None) is None:
                    self._playback_metadata.pop(gid, None)
                    clear_media_levels("discord")
                    await self._remove_music_dashboard(guild)
                return
            voice = getattr(guild, "voice_client", None)
            channel = getattr(voice, "channel", None) if voice else None
            if channel is None:
                return
            channel_id = int(getattr(channel, "id", 0) or 0)
            profile = self._music_profiles.get(gid, {})
            owner = self._voice_owner_ids.get(gid) or _as_snowflake(profile.get("discord_user_id"))
            if before_id != channel_id and after_id != channel_id:
                return  # Unrelated voice rooms cannot affect this session.
            if owner and member_id == owner:
                self._owner_voice_channels[gid] = after_id or None
            if gid in self._voice_cleanup_committed:
                return  # A to_thread cleanup cannot be cancelled by cancelling its waiter.
            pending = self._empty_voice_tasks.get(gid)
            if pending is not None and not pending.done() and member_id != owner:
                return  # Other users cannot indefinitely postpone an owner's departure.
            previous_task = self._empty_voice_tasks.pop(gid, None)
            if previous_task is not None and not previous_task.done():
                previous_task.cancel()

            async def settle_and_leave() -> None:
                task = asyncio.current_task()
                try:
                    await asyncio.sleep(1.25)
                    live_voice = getattr(guild, "voice_client", None)
                    live_channel = getattr(live_voice, "channel", None) if live_voice else None
                    if live_channel is None or int(getattr(live_channel, "id", 0) or 0) != channel_id:
                        return  # A newer explicit connect/move owns the session.
                    # Fence next/connect against late cleanup. Re-evaluate AFTER
                    # acquiring the lock; the owner might have returned meanwhile.
                    lock = self._voice_connect_locks.setdefault(gid, asyncio.Lock())
                    async with lock:
                        if getattr(guild, "voice_client", None) is not live_voice:
                            return
                        if int(getattr(getattr(live_voice, "channel", None), "id", 0) or 0) != channel_id:
                            return
                        humans = [m for m in list(getattr(live_channel, "members", []) or [])
                                  if not bool(getattr(m, "bot", False))]
                        owner_present = bool(owner and self._member_is_in_channel(live_channel, owner))
                        owner_left = bool(owner and gid in self._owner_voice_channels
                            and self._owner_voice_channels[gid] != channel_id
                            and not owner_present
                            and bool(self.config.get("integrations.discord.leave_with_owner", True)))
                        if (humans or owner_present) and not owner_left:
                            return
                        reason = "owner_left" if owner_left else "voice_empty"
                        self._voice_cleanup_committed.add(gid)
                        try:
                            if live_voice.is_playing() or live_voice.is_paused():
                                live_voice.stop()
                        except Exception:
                            logger.debug("Discord stop before leave failed", exc_info=True)
                        if self._music_control_handler is not None:
                            clean_profile = {**profile, "guild_id": str(gid), "name": str(getattr(guild, "name", "") or "")}
                            try:
                                await asyncio.to_thread(self._music_control_handler, clean_profile, "voice_empty", {"reason": reason})
                            except Exception:
                                logger.debug("Discord auto-leave queue cleanup failed", exc_info=True)
                        await live_voice.disconnect(force=True)
                    self._playback_metadata.pop(gid, None)
                    clear_media_levels("discord")
                    from jarvis.runtime.media_events import publish_now_playing
                    publish_now_playing("discord", "", "", "", playing=False, reason=reason)
                    await self._remove_music_dashboard(guild)
                    self._owner_voice_channels.pop(gid, None)
                    logger.info("Discord voice auto-left: reason=%s guild=%s", reason, gid)
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.debug("Discord owner/empty-room auto-leave failed", exc_info=True)
                finally:
                    self._voice_cleanup_committed.discard(gid)
                    if self._empty_voice_tasks.get(gid) is task:
                        self._empty_voice_tasks.pop(gid, None)
            self._empty_voice_tasks[gid] = asyncio.create_task(settle_and_leave())
        except Exception:
            logger.debug("Discord voice-state observer failed", exc_info=True)

    async def _ensure_voice(
        self, profile: dict[str, Any], channel: str = "", *, prefer_current: bool = False,
        before_start: Callable[[], None] | None = None,
    ) -> tuple[Any, Any, Any]:
        admitted = self.admission()

        def check_current() -> None:
            if self._closing.is_set():
                raise MediaRequestSuperseded("Discord background bot завершает работу")
            self._require_admitted(admitted)
            if before_start is not None:
                before_start()

        check_current()
        guild = await self._resolve_guild(profile)
        check_current()
        gid = int(getattr(guild, "id", 0) or 0)
        connect_lock = self._voice_connect_locks.get(gid)
        if connect_lock is None:
            connect_lock = asyncio.Lock()
            self._voice_connect_locks[gid] = connect_lock
        async with connect_lock:
            check_current()
            self._bind_voice_owner(guild, profile)
            voice = guild.voice_client
            if voice is not None:
                try:
                    if not bool(voice.is_connected()):
                        check_current()
                        await voice.disconnect(force=True)
                        check_current()
                        await asyncio.sleep(0.06)
                        check_current()
                        voice = None
                except (MediaRequestSuperseded, asyncio.CancelledError):
                    raise
                except Exception:
                    voice = None
            if prefer_current and not str(channel or "").strip() and voice is not None:
                current_channel = getattr(voice, "channel", None)
                if current_channel is not None:
                    return guild, current_channel, voice
            target = await self._resolve_voice(guild, profile, channel)
            check_current()
            if voice is None:
                if before_start is None:
                    voice = await self._connect_voice_bounded(guild, target)
                else:
                    voice = await self._connect_voice_bounded(guild, target, before_start=before_start)
                check_current()
            elif getattr(getattr(voice, "channel", None), "id", None) != target.id:
                check_current()
                await voice.move_to(target)
                check_current()
            owner = self._voice_owner_ids.get(gid)
            if owner and self._member_is_in_channel(target, owner):
                self._owner_voice_channels[gid] = int(target.id)
            return guild, target, voice

    async def _play(
        self, profile: dict[str, Any], url: str, title: str, artist: str, channel: str,
        *, fast_start: bool = False, prefer_current_voice: bool = False,
        http_headers: dict[str, str] | None = None,
        before_start: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        import discord

        class _BoundedFFmpegStderr:
            def __init__(self, limit: int = 24576) -> None:
                self._limit = max(4096, int(limit))
                self._data = bytearray()
                self._guard = threading.RLock()

            def write(self, data: Any) -> int:
                raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else bytes(data or b"")
                with self._guard:
                    self._data.extend(raw)
                    if len(self._data) > self._limit:
                        del self._data[:-self._limit]
                return len(raw)

            def flush(self) -> None:
                return None

            def text(self) -> str:
                with self._guard:
                    raw = bytes(self._data)
                return raw.decode("utf-8", errors="replace").strip()

        class _Win50SpawnMixin:
            """Spawn FFmpeg without ever surfacing a console window on Windows."""
            @staticmethod
            def _hide_kwargs(values: dict[str, Any]) -> dict[str, Any]:
                kwargs = dict(values)
                if os.name == "nt":
                    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
                    if startupinfo_cls is not None and "startupinfo" not in kwargs:
                        try:
                            startupinfo = startupinfo_cls()
                            startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 1))
                            startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
                            kwargs["startupinfo"] = startupinfo
                        except Exception:
                            pass
                return kwargs

            def _spawn_process(self, args: Any, **subprocess_kwargs: Any) -> subprocess.Popen:
                hidden_kwargs = self._hide_kwargs(subprocess_kwargs)
                try:
                    return super()._spawn_process(args, **hidden_kwargs)
                except OSError as exc:
                    text = str(exc or "").casefold()
                    if not ("winerror 50" in text or "запрос не поддерживается" in text or "request is not supported" in text):
                        raise
                    logger.warning("discord.py FFmpeg CREATE_NO_WINDOW failed with WinError50; retrying compatible hidden spawn")
                    # A few Windows audio/driver stacks reject CREATE_NO_WINDOW for
                    # FFmpeg. Remove only that optional flag; keep SW_HIDE so the
                    # compatibility retry still cannot flash a console.
                    kwargs = self._hide_kwargs(subprocess_kwargs)
                    kwargs.pop("creationflags", None)
                    return subprocess.Popen(args, **kwargs)

        class _ResilientFFmpegOpusAudio(_Win50SpawnMixin, discord.FFmpegOpusAudio):
            def read(self) -> bytes:
                data = super().read()
                if data:
                    publish_audio_packet("discord", data, opus=True)
                return data

        class _ResilientFFmpegPCMAudio(_Win50SpawnMixin, discord.FFmpegPCMAudio):
            def read(self) -> bytes:
                data = super().read()
                if data:
                    publish_audio_packet("discord", data, opus=False)
                return data

        if self._closing.is_set():
            raise MediaRequestSuperseded("Discord background bot завершает работу")
        admitted = self.admission()
        self._require_admitted(admitted)
        if before_start is not None:
            before_start()
        if before_start is None:
            guild, target, voice = await self._ensure_voice(profile, channel, prefer_current=prefer_current_voice)
        else:
            guild, target, voice = await self._ensure_voice(
                profile, channel, prefer_current=prefer_current_voice, before_start=before_start,
            )
        if self._closing.is_set():
            raise MediaRequestSuperseded("Discord background bot завершает работу")
        # Voice resolution awaited the Gateway; Discord may be off by now.
        self._require_admitted(admitted)
        if before_start is not None:
            before_start()

        executables = self._ffmpeg_executables(fast=fast_start)
        local_path = self._managed_local_source(url)
        if local_path:
            # reconnect/reconnect_streamed are HTTP protocol options. Supplying
            # them for a completed local file makes FFmpeg terminate before any
            # Discord audio can be sent.
            before_options = "-nostdin"
            source_input = local_path
            source_method = "cached_local_file"
        else:
            before_options = "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
            # yt-dlp may return a signed YouTube CDN URL together with the HTTP
            # identity expected by that URL. Pass only the two headers FFmpeg
            # commonly needs, using native protocol options instead of a raw
            # arbitrary header blob. Cookies/auth headers are deliberately never
            # accepted here.
            safe_headers = dict(http_headers or {})
            user_agent = str(safe_headers.get("User-Agent", "") or safe_headers.get("user-agent", "") or "").replace('"', '').replace("\r", " ").replace("\n", " ").strip()
            referer = str(safe_headers.get("Referer", "") or safe_headers.get("referer", "") or "").replace('"', '').replace("\r", " ").replace("\n", " ").strip()
            if user_agent:
                before_options += f' -user_agent "{user_agent}"'
            if referer:
                before_options += f' -referer "{referer}"'
            source_input = url
            source_method = "direct_stream"

        guild_id = int(guild.id)
        generation: int | None = None

        def check_owner() -> None:
            if self._closing.is_set():
                raise MediaRequestSuperseded("Discord background bot завершает работу")
            self._require_admitted(admitted)
            if before_start is not None:
                before_start()
            if generation is not None:
                with self._lock:
                    if self._playback_generation.get(guild_id, 0) != generation:
                        raise MediaRequestSuperseded("A newer Discord playback request owns the transport")

        # Reserve the new transport generation *before* stopping the previous
        # FFmpeg player.  discord.py may deliver the old after-callback several
        # seconds late; if generation is advanced only after voice.stop(), that
        # stale callback can be mistaken for the newly skipped-to track and cause
        # pause/restart/retry behaviour.
        check_owner()
        clear_media_levels("discord")
        with self._lock:
            generation = int(self._playback_generation.get(guild_id, 0)) + 1
            self._playback_generation[guild_id] = generation
            self._playback_errors.pop(guild_id, None)
            self._playback_metadata[guild_id] = {
                "title": str(title or "").strip(),
                "artist": str(artist or "").strip(),
                "playback_generation": generation,
                "started_at": time.monotonic(),
                "source_method": source_method,
            }
            if local_path:
                self._active_local_sources[guild_id] = local_path
            else:
                self._active_local_sources.pop(guild_id, None)

        if voice.is_playing() or voice.is_paused():
            check_owner()
            voice.stop()
            # Give the previous Discord/FFmpeg transport a short drain window.
            # The old callback now belongs to an older generation and cannot
            # terminate/retry this new track.
            await asyncio.sleep(0.006 if fast_start else 0.35)
            check_owner()

        # 120FPS spectrum: prefer PCM so the visualizers receive real frequency
        # bands from the exact samples sent to Discord. Opus remains the
        # compatibility fallback when local PCM/encoder support is unavailable.
        # Retry across verified executables instead of
        # turning a healthy Discord voice session into a hard assistant error.
        source = None
        selected_executable = ""
        selected_source_kind = ""
        selected_stderr: Any | None = None
        last_spawn_error: Exception | None = None
        attempts: list[str] = []
        for executable in executables:
            for source_kind in ("pcm", "opus"):
                try:
                    check_owner()
                    stderr_collector = _BoundedFFmpegStderr()
                    if source_kind == "opus":
                        candidate_source = _ResilientFFmpegOpusAudio(
                            source_input,
                            executable=executable,
                            before_options=before_options,
                            options="-vn -loglevel warning",
                            # discord.py FFmpegOpusAudio interprets explicit
                            # "libopus" as an already-Opus/copy hint. Yandex
                            # sources are commonly MP3/AAC, so leave codec unset
                            # and let discord.py ask FFmpeg to transcode to Opus.
                            codec=None,
                            # discord.py 2.7.x treats an object without fileno()
                            # as a destination for its internal stderr forwarding
                            # thread. subprocess.DEVNULL is an integer sentinel,
                            # so passing it here makes _pipe_reader call .write()
                            # on an int and kills otherwise healthy playback.
                            # A bounded write-capable collector triggers discord.py's
                            # pipe forwarding path safely and lets NEO surface the
                            # real FFmpeg HTTP/codec error if startup dies.
                            stderr=stderr_collector,
                        )
                    else:
                        pcm_source = _ResilientFFmpegPCMAudio(
                            source_input,
                            executable=executable,
                            before_options=before_options,
                            options="-vn -loglevel warning",
                            stderr=stderr_collector,
                        )
                        initial_volume = max(0, min(100, int(self._voice_volume_percent.get(guild_id, 100) or 100))) / 100.0
                        candidate_source = discord.PCMVolumeTransformer(pcm_source, volume=initial_volume)
                    try:
                        check_owner()
                        voice.play(
                            candidate_source,
                            after=lambda error, gid=guild_id, gen=generation, path=local_path:
                                self._finish_playback(gid, gen, path, error),
                        )
                    except Exception:
                        try:
                            candidate_source.cleanup()
                        except Exception:
                            pass
                        raise
                    source = candidate_source
                    selected_executable = executable
                    selected_source_kind = source_kind
                    selected_stderr = stderr_collector
                    self._ffmpeg_verified_path = executable
                    logger.info(
                        "Discord FFmpeg transport started source=%s executable=%s method=%s",
                        source_kind, executable, source_method,
                    )
                    break
                except MediaRequestSuperseded:
                    raise
                except Exception as exc:
                    last_spawn_error = exc
                    attempts.append(f"{source_kind}:{os.path.basename(executable)}={exc}")
                    self._mark_ffmpeg_runtime_failure(executable, exc)
                    logger.warning(
                        "Discord FFmpeg transport attempt failed source=%s executable=%s: %s",
                        source_kind, executable, exc,
                    )
                    # PCM failures caused by a missing local opus encoder are not
                    # executable-specific; the next executable can still be tried.
                    continue
            if source is not None:
                break

        if source is None:
            if local_path:
                threading.Thread(
                    target=self._delete_file_with_retries, args=(local_path,),
                    name="jarvis-discord-track-cleanup", daemon=True,
                ).start()
            detail = "; ".join(attempts[-6:])
            raise RuntimeError(
                "Discord voice подключён, но FFmpeg transport не запустился"
                + (f": {detail}" if detail else f": {last_spawn_error or 'unknown error'}")
            )

        # HOTFIX42.2: do not report a Discord track as successfully started
        # until it survives the short failure window seen in field logs. Audio
        # already starts immediately; this only delays the success/now-playing
        # acknowledgement, preventing phantom "Сейчас играет" events when the
        # transport dies a second later.
        # Natural/automatic queue advances keep the longer startup verification
        # from HOTFIX42.2. A user-initiated next/previous uses a short immediate
        # verification because the queue monitor + generation circuit breaker will
        # still catch a later transport failure without making the user wait 2.6 s.
        poll_interval = 0.012 if fast_start else 0.20
        if fast_start:
            deadline = time.monotonic() + 0.05
        else:
            # Preserve the proven HOTFIX42.2 health gate for automatic playback.
            deadline = time.monotonic() + 2.6
        playing = bool(voice.is_playing())
        paused = bool(voice.is_paused())
        try:
            while time.monotonic() < deadline and (playing or paused):
                await asyncio.sleep(poll_interval)
                check_owner()
                playing = bool(voice.is_playing())
                paused = bool(voice.is_paused())
            check_owner()
        except MediaRequestSuperseded:
            # Shutdown tears the transport down itself; only a settings
            # disable must silence the track it just started.
            if not self._closing.is_set() and self.admission() != admitted:
                # Disabled while the start was being verified: nothing may keep
                # playing and no verified result is reported.
                try:
                    voice.stop()
                except Exception:
                    pass
            raise
        if not playing and not paused:
            with self._lock:
                detail = self._playback_errors.get(guild_id, "")
                finished_error = self._last_finished_error.get(guild_id, "")
            detail = detail or finished_error
            ffmpeg_detail = ""
            try:
                ffmpeg_detail = str(selected_stderr.text() if selected_stderr is not None else "").strip()
            except Exception:
                ffmpeg_detail = ""
            return_code = None
            try:
                process = getattr(source, "_process", None)
                return_code = process.poll() if process is not None else None
            except Exception:
                return_code = None
            parts = [item for item in (detail, ffmpeg_detail) if item]
            if return_code is not None:
                parts.append(f"FFmpeg code={return_code}")
            detail = " | ".join(parts)
            if detail:
                logger.warning("Discord FFmpeg immediate-exit details: %s", detail)
            raise RuntimeError(
                "FFmpeg завершил воспроизведение сразу после запуска"
                + (f": {detail}" if detail else ". Проверьте аудиоисточник/FFmpeg в логе NEO.")
            )

        return {
            "server": guild.name,
            "guild_id": str(guild.id),
            "channel": target.name,
            "voice_channel_id": str(target.id),
            "title": title,
            "artist": artist,
            "playing": playing,
            "playback_generation": generation,
            "verified": playing or paused,
            "submitted": True,
            "source_method": source_method,
            "ffmpeg_source_kind": selected_source_kind,
            "ffmpeg_executable": selected_executable,
            "temporary_file": bool(local_path),
            "method": "discord_official_bot_voice",
            "non_invasive": True,
        }

    def play_url(
        self, profile: dict[str, Any], url: str, *, title: str = "", artist: str = "", channel: str = "",
        fast_start: bool = False, prefer_current_voice: bool = False,
        http_headers: dict[str, str] | None = None,
        before_start: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        return dict(self._submit(
            self._play(
                profile, url, title, artist, channel,
                fast_start=fast_start, prefer_current_voice=prefer_current_voice,
                http_headers=http_headers,
                before_start=before_start,
            ),
            timeout=15.0 if fast_start else 35.0,
        ))

    def play_youtube_url(
        self, profile: dict[str, Any], url: str, *, title: str = "", artist: str = "", channel: str = "",
        fast_start: bool = False, prefer_current_voice: bool = False,
        before_start: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Accept a raw YouTube page URL and play it through the Discord bot.

        This path is deliberately independent from ``integrations.youtube`` and
        the YouTube Data API key.  The URL is handed to the Discord transport,
        which resolves a public audio representation locally and feeds the
        resulting file to Discord voice.  No YouTube OAuth/API integration is
        required for an ordinary public URL.
        """
        clean = normalize_youtube_url(str(url or ""))
        if not clean:
            raise ValueError("Нужна прямая ссылка YouTube")
        if before_start is not None:
            before_start()
        if before_start is None:
            resolved = self._youtube_audio.download_audio(clean)
        else:
            resolved = self._youtube_audio.download_audio(clean, check_cancelled=before_start)
        if before_start is not None:
            before_start()
        local_path = str(resolved.get("local_path", "") or "").strip()
        if not local_path or not os.path.isfile(local_path):
            raise RuntimeError("Discord Music не смог подготовить аудио по ссылке YouTube")
        data = self.play_url(
            profile, local_path,
            title=str(resolved.get("title", "") or title or "YouTube"),
            artist=str(resolved.get("artist", "") or artist or "YouTube"),
            channel=channel, fast_start=fast_start,
            prefer_current_voice=prefer_current_voice,
            http_headers=dict(resolved.get("http_headers") or {}),
            before_start=before_start,
        )
        return {
            **data,
            "track_id": str(resolved.get("video_id", "") or ""),
            "title": str(resolved.get("title", "") or title or "YouTube"),
            "artist": str(resolved.get("artist", "") or artist or "YouTube"),
            "expected_duration_ms": int(resolved.get("duration_ms", 0) or 0),
            "webpage_url": str(resolved.get("webpage_url", "") or clean),
            "source_method": "discord_bot_youtube_url_handoff",
            "download_profile": str(resolved.get("download_profile", "") or ""),
            "api_used": False,
            "youtube_integration_used": False,
        }

    async def _send(self, profile: dict[str, Any], text: str, channel: str) -> dict[str, Any]:
        admitted = self.admission()
        self._require_admitted(admitted)
        guild = await self._resolve_guild(profile)
        target = await self._resolve_text(guild, profile, channel)
        self._require_admitted(admitted)
        message = await target.send(text)
        return {
            "server": guild.name,
            "guild_id": str(guild.id),
            "channel": target.name,
            "text_channel_id": str(target.id),
            "message_id": str(message.id),
            "verified": True,
            "submitted": True,
            "method": "discord_official_bot_message",
            "non_invasive": True,
        }

    def send_message(self, profile: dict[str, Any], text: str, *, channel: str = "") -> dict[str, Any]:
        text = str(text or "").strip()
        if not text:
            raise ValueError("Пустое сообщение Discord")
        return dict(self._submit(self._send(profile, text, channel)))

    async def _attach_text_channel(self, profile: dict[str, Any], channel: str) -> dict[str, Any]:
        guild = await self._resolve_guild(profile)
        wanted = _norm(channel)
        if not wanted:
            raise ValueError("Не указан текстовый канал Discord")
        target = None
        for item in list(getattr(guild, "text_channels", []) or []):
            if _norm(getattr(item, "name", "")) == wanted and self._text_permissions_ok(guild, item):
                target = item; break
        if target is None:
            available = [str(getattr(x, "name", "") or "") for x in list(getattr(guild, "text_channels", []) or []) if self._text_permissions_ok(guild, x)]
            raise RuntimeError("Текстовый канал не найден или у NEO Bot нет права Send Messages. Доступные: " + ", ".join(available[:20]))
        gid = int(guild.id)
        self._attached_text_channel_ids[gid] = int(target.id)
        self._save_text_channel_attachments()
        return {"server": guild.name, "guild_id": str(guild.id), "channel": target.name, "text_channel_id": str(target.id), "attached": True, "verified": True, "method": "discord_official_bot_text_attach", "non_invasive": True}

    def attach_text_channel(self, profile: dict[str, Any], channel: str) -> dict[str, Any]:
        return dict(self._submit(self._attach_text_channel(profile, channel)))

    async def _detach_text_channel(self, profile: dict[str, Any]) -> dict[str, Any]:
        guild = await self._resolve_guild(profile)
        old = int(self._attached_text_channel_ids.pop(int(guild.id), 0) or 0)
        self._save_text_channel_attachments()
        channel = guild.get_channel(old) if old else None
        return {"server": guild.name, "guild_id": str(guild.id), "channel": str(getattr(channel, "name", "") or ""), "attached": False, "verified": True, "method": "discord_official_bot_text_detach", "non_invasive": True}

    def detach_text_channel(self, profile: dict[str, Any]) -> dict[str, Any]:
        return dict(self._submit(self._detach_text_channel(profile)))

    async def _text_channel_status(self, profile: dict[str, Any]) -> dict[str, Any]:
        guild = await self._resolve_guild(profile)
        cid = int(self._attached_text_channel_ids.get(int(guild.id), 0) or 0)
        channel = guild.get_channel(cid) if cid else None
        return {"server": guild.name, "guild_id": str(guild.id), "channel": str(getattr(channel, "name", "") or ""), "text_channel_id": str(cid or ""), "attached": bool(channel), "verified": True, "method": "discord_official_bot_text_status", "non_invasive": True}

    def text_channel_status(self, profile: dict[str, Any]) -> dict[str, Any]:
        return dict(self._submit(self._text_channel_status(profile)))

    async def _list_accessible_channels(self, profile: dict[str, Any]) -> dict[str, Any]:
        guild = await self._resolve_guild(profile)
        voice=[]
        for item in list(getattr(guild, "voice_channels", []) or []) + list(getattr(guild, "stage_channels", []) or []):
            if self._voice_permissions_ok(guild, item):
                voice.append({"name":str(getattr(item,"name","") or ""),"id":str(getattr(item,"id","") or ""),"kind":"voice"})
        text=[]
        for item in list(getattr(guild, "text_channels", []) or []):
            if self._text_permissions_ok(guild, item):
                text.append({"name":str(getattr(item,"name","") or ""),"id":str(getattr(item,"id","") or ""),"kind":"text"})
        return {"server":guild.name,"guild_id":str(guild.id),"voice_channels":voice,"text_channels":text,"verified":True,"method":"discord_gateway_channel_inventory"}

    def list_accessible_channels(self, profile: dict[str, Any]) -> dict[str, Any]:
        return dict(self._submit(self._list_accessible_channels(profile), timeout=12.0))

    async def _connect_named_channel(self, profile: dict[str, Any], channel: str) -> dict[str, Any]:
        guild = await self._resolve_guild(profile)
        wanted=_norm(str(channel or ""))
        if not wanted:
            raise ValueError("Не указано название Discord-канала")
        voice=[x for x in list(getattr(guild,"voice_channels",[]) or [])+list(getattr(guild,"stage_channels",[]) or []) if self._voice_permissions_ok(guild,x) and _norm(getattr(x,"name",""))==wanted]
        text=[x for x in list(getattr(guild,"text_channels",[]) or []) if self._text_permissions_ok(guild,x) and _norm(getattr(x,"name",""))==wanted]
        if len(voice)==1 and not text:
            _guild,target,vc=await self._ensure_voice(profile,str(getattr(voice[0],"name","")))
            return {"server":guild.name,"guild_id":str(guild.id),"channel":target.name,"channel_kind":"voice","connected":bool(vc.is_connected()),"verified":True,"method":"discord_official_bot_auto_channel"}
        if len(text)==1 and not voice:
            gid=int(guild.id); target=text[0]; self._attached_text_channel_ids[gid]=int(target.id); self._save_text_channel_attachments()
            return {"server":guild.name,"guild_id":str(guild.id),"channel":target.name,"channel_kind":"text","text_channel_id":str(target.id),"attached":True,"verified":True,"method":"discord_official_bot_auto_channel"}
        if voice and text:
            raise RuntimeError(f"На сервере есть и голосовой, и текстовый канал с названием «{channel}». Уточните: голосовой или текстовый.")
        inv=await self._list_accessible_channels(profile)
        vn=", ".join(x["name"] for x in inv["voice_channels"][:12]); tn=", ".join(x["name"] for x in inv["text_channels"][:12])
        raise RuntimeError(f"Канал «{channel}» не найден. Голосовые: {vn or 'нет'}. Текстовые: {tn or 'нет'}.")

    def connect_named_channel(self, profile: dict[str, Any], channel: str) -> dict[str, Any]:
        return dict(self._submit(self._connect_named_channel(profile, channel), timeout=15.0))

    async def _playback_status(self, profile: dict[str, Any]) -> dict[str, Any]:
        guild = await self._resolve_guild(profile)
        voice = guild.voice_client
        guild_id = int(guild.id)
        with self._lock:
            transport_meta = dict(self._playback_metadata.get(guild_id) or {})
        if voice is None:
            return {
                "server": guild.name, "guild_id": str(guild.id), "connected": False,
                "playing": False, "paused": False,
                "playback_generation": int(self._playback_generation.get(int(guild.id), 0) or 0),
                "last_finished_generation": int(self._last_finished_generation.get(int(guild.id), 0) or 0),
                "last_finished_at": float(self._last_finished_at.get(int(guild.id), 0.0) or 0.0),
                "last_finished_error": str(self._last_finished_error.get(int(guild.id), "") or ""),
                "current": transport_meta,
                "title": str(transport_meta.get("title", "") or ""),
                "artist": str(transport_meta.get("artist", "") or ""),
                "verified": True,
                "method": "discord_official_bot_voice", "non_invasive": True,
            }
        channel = getattr(voice, "channel", None)
        return {
            "server": guild.name, "guild_id": str(guild.id),
            "channel": str(getattr(channel, "name", "") or ""),
            "voice_channel_id": str(getattr(channel, "id", "") or ""),
            "connected": bool(getattr(voice, "is_connected", lambda: False)()),
            "playing": bool(getattr(voice, "is_playing", lambda: False)()),
            "paused": bool(getattr(voice, "is_paused", lambda: False)()),
            "playback_generation": int(self._playback_generation.get(int(guild.id), 0) or 0),
            "last_finished_generation": int(self._last_finished_generation.get(int(guild.id), 0) or 0),
            "last_finished_at": float(self._last_finished_at.get(int(guild.id), 0.0) or 0.0),
            "last_finished_error": str(self._last_finished_error.get(int(guild.id), "") or ""),
            "current": transport_meta if (bool(getattr(voice, "is_playing", lambda: False)()) or bool(getattr(voice, "is_paused", lambda: False)())) else {},
            "title": str(transport_meta.get("title", "") or ""),
            "artist": str(transport_meta.get("artist", "") or ""),
            "verified": True, "method": "discord_official_bot_voice", "non_invasive": True,
        }

    def playback_status(self, profile: dict[str, Any]) -> dict[str, Any]:
        return dict(self._submit(self._playback_status(profile), timeout=12.0))

    async def _voice_action(self, profile: dict[str, Any], action: str, channel: str = "") -> dict[str, Any]:
        admitted = self.admission()
        # Stop/leave stay possible so a disabled Discord can always go quiet.
        if action not in {"stop", "leave"}:
            self._require_admitted(admitted)
        guild = await self._resolve_guild(profile)
        if action not in {"stop", "leave"}:
            self._require_admitted(admitted)
        if action == "join":
            _guild, target, voice = await self._ensure_voice(profile, channel)
            return {
                "server": guild.name, "channel": target.name, "connected": voice.is_connected(),
                "verified": voice.is_connected(), "method": "discord_official_bot_voice", "non_invasive": True,
            }
        voice = guild.voice_client
        if voice is None:
            if action in {"stop", "leave"}:
                return {"server": guild.name, "verified": True, "already_in_requested_state": True, "method": "discord_official_bot_voice", "non_invasive": True}
            raise RuntimeError("NEO Bot сейчас не подключён к голосовому каналу")
        before_playing = bool(getattr(voice, "is_playing", lambda: False)())
        before_paused = bool(getattr(voice, "is_paused", lambda: False)())
        already = False
        if action == "pause":
            if before_paused:
                already = True
            elif not before_playing:
                raise RuntimeError("Музыка Discord сейчас не воспроизводится")
            else:
                voice.pause()
        elif action == "resume":
            if before_playing and not before_paused:
                already = True
            elif not before_paused:
                raise RuntimeError("Музыка Discord сейчас не стоит на паузе")
            else:
                voice.resume()
        elif action == "stop":
            if not before_playing and not before_paused:
                already = True
            else:
                voice.stop()
        elif action == "leave":
            await voice.disconnect(force=False)
        else:
            raise ValueError(action)
        await asyncio.sleep(0.006 if action == "stop" else 0.06)
        return {
            "server": guild.name,
            "action": action,
            "playing": bool(getattr(voice, "is_playing", lambda: False)()),
            "paused": bool(getattr(voice, "is_paused", lambda: False)()),
            "already_in_requested_state": already,
            "verified": True,
            "method": "discord_official_bot_voice",
            "non_invasive": True,
        }

    async def _find_timer_dashboard(self, channel: Any) -> Any | None:
        client_user = getattr(self._client, "user", None)
        bot_id = int(getattr(client_user, "id", 0) or 0)
        if not bot_id:
            return None
        try:
            async for message in channel.history(limit=40):
                if int(getattr(getattr(message, "author", None), "id", 0) or 0) != bot_id:
                    continue
                if str(getattr(message, "content", "") or "").startswith("⏱ **Активные таймеры NEO**"):
                    return message
        except Exception:
            return None
        return None

    async def _set_timer_summaries(self, profile: dict[str, Any], rows: list[str]) -> bool:
        admitted = self.admission()
        self._require_admitted(admitted)
        guild = await self._resolve_guild(profile)
        self._require_admitted(admitted)
        gid = int(getattr(guild, "id", 0) or 0)
        clean = [str(x or "").strip()[:160] for x in list(rows or []) if str(x or "").strip()][:6]
        # Only a confirmed delivery may short-circuit a repeat. A failed or
        # unknown attempt leaves the cache untouched, so the same rows retry.
        if self._timer_summaries.get(gid) == clean and self._timer_delivery.get(gid) == "delivered":
            return True
        if not clean and gid not in self._timer_summaries and gid not in self._timer_dashboard_messages:
            # Nothing was ever published for this guild: no channel lookup.
            self._timer_summaries[gid] = []
            self._timer_delivery[gid] = "delivered"
            return True
        self._timer_delivery[gid] = "pending"
        channel = await self._ensure_music_channel(guild)
        self._require_admitted(admitted)
        if channel is None:
            self._timer_delivery[gid] = "error"
            return False
        message = self._timer_dashboard_messages.get(gid)
        if message is None:
            message = await self._find_timer_dashboard(channel)
            self._require_admitted(admitted)
        if not clean:
            if message is not None:
                try:
                    await message.delete()
                except Exception as exc:
                    logger.debug("Discord timer panel delete failed: %s", exc)
                    self._timer_delivery[gid] = "error"
                    return False
            self._timer_dashboard_messages.pop(gid, None)
            self._timer_dashboard_ids.pop(gid, None)
            self._timer_dashboard_signatures.pop(gid, None)
            self._timer_summaries[gid] = clean
            self._timer_delivery[gid] = "delivered"
            return True
        text = "⏱ **Активные таймеры NEO**\n" + "\n".join(f"• {row}" for row in clean)
        if self._timer_dashboard_signatures.get(gid) == text and message is not None:
            self._timer_summaries[gid] = clean
            self._timer_delivery[gid] = "delivered"
            return True
        self._require_admitted(admitted)
        if message is None:
            try:
                message = await channel.send(text[:1900])
            except Exception as exc:
                logger.debug("Discord timer panel create failed: %s", exc)
                self._timer_delivery[gid] = "error"
                return False
        else:
            try:
                await message.edit(content=text[:1900])
            except Exception as exc:
                logger.debug("Discord timer panel edit failed: %s", exc)
                # The cached message may be gone; look it up again next time.
                self._timer_dashboard_messages.pop(gid, None)
                self._timer_delivery[gid] = "error"
                return False
        message_id = int(getattr(message, "id", 0) or 0)
        if not message_id:
            self._timer_delivery[gid] = "unknown"
            return False
        self._timer_dashboard_messages[gid] = message
        self._timer_dashboard_ids[gid] = message_id
        self._timer_dashboard_signatures[gid] = text
        self._timer_summaries[gid] = clean
        self._timer_delivery[gid] = "delivered"
        return True

    def set_timer_summaries(self, profile: dict[str, Any], rows: list[str]) -> bool:
        return bool(self._submit(self._set_timer_summaries(profile, list(rows or []))))

    async def _set_voice_volume(self, profile: dict[str, Any], percent: int) -> dict[str, Any]:
        guild = await self._resolve_guild(profile)
        gid = int(getattr(guild, "id", 0) or 0)
        value = max(0, min(100, int(percent)))
        self._voice_volume_percent[gid] = value
        voice = getattr(guild, "voice_client", None)
        applied = False
        if voice is not None:
            source = getattr(voice, "source", None)
            if source is not None and hasattr(source, "volume"):
                try:
                    source.volume = value / 100.0
                    applied = True
                except Exception:
                    applied = False
        return {
            "server": str(getattr(guild, "name", "") or profile.get("name", "")),
            "volume": value, "verified": True, "applied_to_current": applied,
            "playing": bool(voice and getattr(voice, "is_playing", lambda: False)()),
            "paused": bool(voice and getattr(voice, "is_paused", lambda: False)()),
            "method": "discord_pcm_volume_transformer" if applied else "discord_volume_next_pcm_source",
        }

    def set_voice_volume(self, profile: dict[str, Any], percent: int) -> dict[str, Any]:
        return dict(self._submit(self._set_voice_volume(profile, int(percent))))

    def voice_volume(self, profile: dict[str, Any]) -> dict[str, Any]:
        guild = self._submit(self._resolve_guild(profile))
        gid = int(getattr(guild, "id", 0) or 0)
        return {"volume": max(0, min(100, int(self._voice_volume_percent.get(gid, 100) or 100))), "verified": True}

    def voice_action(self, profile: dict[str, Any], action: str, *, channel: str = "") -> dict[str, Any]:
        return dict(self._submit(self._voice_action(profile, action, channel)))


def stop_disabled_discord_bots(timeout_per_bot: float = 2.5) -> int:
    """Close running bots whose Discord master/background switch is now off."""
    with _ALL_BOTS_LOCK:
        bots = list(_ALL_BOTS)
    stopped = 0
    for bot in bots:
        try:
            if bot._enabled() or bot._thread is None:
                continue
            if bot.close(timeout=timeout_per_bot):
                bot._closing.clear()  # may be enabled again later
                stopped += 1
        except Exception:
            logger.debug("Disabled Discord bot shutdown failed", exc_info=True)
    return stopped


def shutdown_all_discord_bots(timeout_per_bot: float = 2.5) -> bool:
    """Close every Discord client and report whether every Gateway thread stopped."""
    with _ALL_BOTS_LOCK:
        bots = list(_ALL_BOTS)
    clean = True
    for bot in bots:
        try:
            clean = bool(bot.close(timeout=timeout_per_bot)) and clean
        except Exception:
            clean = False
            logger.debug("Discord bot shutdown failed", exc_info=True)
    return clean
