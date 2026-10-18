# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Telegram Remote UI for NEO.

HOTFIX73 keeps the phone-first remote-control surface visually at the bottom
without removing it while an action is running. Telegram remains an untrusted
transport: private-chat + numeric owner allow-listing is enforced before any
action reaches the desktop tool registry.

The bot uses inline keyboards (not BotFather's command menu) as the primary UI.
It keeps a local ledger of confirmed bot-owned message IDs. Startup/shutdown
cleanup uses only those IDs; it never infers ownership from neighbouring IDs.
"""

import hmac
import html
import json
import logging
import os
import platform
import queue
import re
import threading
import time
import uuid
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import psutil
import requests

from jarvis.agent import Agent, AgentTurn
from jarvis.runtime.media_events import MediaNowPlayingEvent, subscribe_now_playing, unsubscribe_now_playing
from jarvis.runtime.media_activity import recent_music_owner
from jarvis.runtime.timers import timer_manager
from jarvis.quick_media_commands import parse_quick_media_command
from jarvis.integrations.bluetooth_devices import (
    list_bluetooth_devices,
    list_pairable_bluetooth_devices,
    pair_bluetooth_device,
    set_device_connection,
)
from jarvis.integrations.telegram_keys import execute_remote_key_input
from jarvis.integrations.telegram_mouse import monitor_inventory, capture_coordinate_grid, click_global
from jarvis.integrations.google_calendar import GoogleCalendarClient, format_calendar_events, event_start_datetime
from jarvis.integrations.music_provider import active_music_provider, provider_label
from jarvis.integrations.youtube_audio import extract_youtube_url
from jarvis.integrations.telegram_messages import telegram_message_center, format_notification as format_telegram_message_notification
from jarvis.language import language_code
from jarvis.runtime.localized_text import localize_runtime_text
from jarvis.overlay_state import OverlayStore, publish_overlay_refresh
from jarvis.ui.theme_presets import THEME_PRESETS
from jarvis.security import Risk
from jarvis.secrets import (
    telegram_bot_token,
    telegram_critical_phrase,
    telegram_pairing_secret,
)

logger = logging.getLogger(__name__)


BOT_MENU_COMMANDS: list[dict[str, str]] = []


# NEO 183: a Telegram conversation is shown in the desktop chat as the user's command and NEO's
# reply, in the interface language. The marker tells the main window the text is already localized.
TELEGRAM_DIALOGUE_MARKER = "\u2063tg183\u2063"
# Local-only control event: emitted once when a user command is accepted.
TELEGRAM_SUBMISSION_MARKER = "\u2063tg-submit184\u2063"
_TELEGRAM_DIALOGUE = {
    "ru": ("Telegram команда", "Я ответил"), "en": ("Telegram command", "I replied"),
    "de": ("Telegram-Befehl", "Ich habe geantwortet"), "fr": ("Commande Telegram", "J’ai répondu"),
    "es": ("Comando de Telegram", "Respondí"), "it": ("Comando Telegram", "Ho risposto"),
    "pt": ("Comando do Telegram", "Respondi"),
}


def telegram_dialogue_event(config: Any, command: str, reply: str) -> str:
    asked, answered = _TELEGRAM_DIALOGUE.get(language_code(config), _TELEGRAM_DIALOGUE["en"])
    return f'{TELEGRAM_DIALOGUE_MARKER}{asked} "{str(command or "").strip()}"\n{answered} "{str(reply or "").strip()}"'


@dataclass
class _TextInput:
    kind: str
    user_id: str
    chat_id: str
    created_at: float
    expires_at: float
    panel_message_id: int = 0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class _SecurePending:
    nonce: str
    kind: str  # tool / agent / unlock
    user_id: str
    chat_id: str
    created_at: float
    expires_at: float
    label: str
    panel_message_id: int = 0
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    return_view: str = "home"
    confirmation_id: str = ""


@dataclass
class _ToolJob:
    chat_id: str
    tool_name: str
    arguments: dict[str, Any]
    label: str
    panel_message_id: int = 0
    return_view: str = "home"
    recreate_panel: bool = True
    quiet: bool = False
    cancellation_generation: int | None = None
    consume_agent_pending: bool = False


class TelegramRemotePolicy:
    """Transport-specific deny/confirmation policy for the Telegram Agent."""

    DEFAULT_BLOCKED_TOOLS = {"powershell", "developer", "email", "transfer"}

    def __init__(self, config: Any) -> None:
        self.config = config

    def _blocked_tools(self) -> set[str]:
        raw = self.config.get(
            "integrations.telegram.blocked_tools", sorted(self.DEFAULT_BLOCKED_TOOLS)
        ) or []
        if not isinstance(raw, list):
            raw = list(self.DEFAULT_BLOCKED_TOOLS)
        return {str(item or "").strip().casefold() for item in raw if str(item or "").strip()}

    def execution_guard(self, tool_name: str, arguments: dict[str, Any], risk: Risk) -> str | None:
        del risk
        name = str(tool_name or "").strip().casefold()
        action = str(arguments.get("action", "") or "").strip().casefold()
        if name == "scenario" and action in {"list", "run"}:
            # HOTFIX57: only scenarios that already exist in the local NEO
            # settings may be started remotely. No arbitrary command payload is
            # accepted from Telegram here; the selected name must match exactly.
            if action == "run":
                requested = str(arguments.get("name", "") or "").casefold().strip()
                allowed = set()
                for raw in self.config.get("scenarios.items", []) or []:
                    if not isinstance(raw, dict):
                        continue
                    for value in [raw.get("name", ""), raw.get("phrase", ""), *(raw.get("aliases", []) or [])]:
                        value = str(value or "").casefold().strip()
                        if value:
                            allowed.add(value)
                if not requested or requested not in allowed:
                    return "Удалённый запуск неизвестного сценария запрещён."
            return None
        if name in self._blocked_tools():
            return (
                f"Удалённый доступ к инструменту «{name}» заблокирован Telegram-политикой безопасности."
            )
        # Full remote power-off remains disabled by the Telegram transport
        # policy. Sleep/hibernate/restart are available behind the critical phrase.
        if name == "system" and action == "shutdown" and not bool(
            self.config.get("integrations.telegram.allow_remote_shutdown", False)
        ):
            return "Удалённое выключение отключено в Telegram Remote."
        return None

    def confirmation_override(
        self, tool_name: str, arguments: dict[str, Any], risk: Risk
    ) -> bool | None:
        name = str(tool_name or "").strip().casefold()
        action = str(arguments.get("action", "") or "").strip().casefold()
        if name in self._blocked_tools():
            return False
        if risk >= Risk.HIGH:
            return True
        if name == "filesystem" and action == "delete_permanent":
            return True
        if bool(self.config.get("integrations.telegram.confirm_medium_risk", False)) and risk == Risk.MEDIUM:
            return True
        return None


class TelegramRemoteController:
    def __init__(self, config: Any, event_callback: Callable[[str], None] | None = None) -> None:
        self.config = config
        self.policy = TelegramRemotePolicy(config)
        self._event_callback = event_callback
        self._agent: Agent | None = None
        # HOTFIX103: long free-form Agent turns must never block Telegram getUpdates.
        # The polling thread only validates/enqueues; one dedicated worker preserves
        # per-owner command order while buttons/status callbacks stay responsive.
        self._agent_jobs: queue.Queue[tuple[str, str, str, str, int, str]] = queue.Queue(maxsize=32)
        self._agent_thread: threading.Thread | None = None
        # HOTFIX105: ACK Telegram callbacks before provider work. Sleeping, bounded
        # lanes preserve order per stateful provider without adding busy-loop CPU.
        self._tool_lane_queues: dict[str, queue.Queue[_ToolJob]] = {
            "music": queue.Queue(maxsize=48),
            "discord": queue.Queue(maxsize=48),
            "desktop": queue.Queue(maxsize=48),
            "api": queue.Queue(maxsize=48),
        }
        self._tool_lane_threads: dict[str, threading.Thread] = {}
        # Desktop ToolRegistry is optionally shared only for stateful media button actions.
        # Telegram keeps its own Agent/pending-confirmation context for transport safety,
        # but like/dislike/queue must operate on the exact player instance used by desktop NEO.
        self._desktop_tools: Any | None = None

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._session: requests.Session | None = None
        # Callback acknowledgements are UI transport work, not a prerequisite for
        # the requested desktop action. Keep them on a tiny independent lane so a
        # Telegram network RTT can never sit in front of pause/next/etc.
        self._callback_ack_jobs: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=64)
        self._callback_ack_thread: threading.Thread | None = None
        self._state_path = Path(self.config.paths.root) / "telegram_remote_state.json"
        state = self._load_state()
        self._last_update_id = max(0, int(state.get("last_update_id", 0) or 0))
        raw_messages = state.get("messages", [])
        self._tracked_messages: list[dict[str, Any]] = raw_messages if isinstance(raw_messages, list) else []
        self._last_cleanup_at = float(state.get("last_cleanup_at", 0) or 0)
        # NEO 188 NEO-TG-001: one cleanup pass at a time, an honest last report,
        # a known-gap marker and a send barrier after the final shutdown cleanup.
        self._cleanup_lock = threading.Lock()
        self._send_barrier = False
        self._cleanup_retry_at = float(state.get("cleanup_retry_at", 0) or 0)
        raw_report = state.get("cleanup_report", {})
        self._cleanup_report: dict[str, Any] = raw_report if isinstance(raw_report, dict) else {}
        self._coverage_gap_at = float(state.get("coverage_gap_at", 0) or 0)
        if state and int(state.get("ledger_version", 0) or 0) < 188:
            # Older ledgers never recorded the user's own messages: their IDs are unknown.
            self._coverage_gap_at = max(self._coverage_gap_at, time.time())

        self._input: _TextInput | None = None
        self._secure_pending: _SecurePending | None = None
        self._lock = threading.RLock()
        self._rate: dict[str, float] = {}
        self._pair_failures: dict[str, int] = {}
        self._auth_failures: dict[str, tuple[int, float]] = {}
        self._connected = threading.Event()
        self._last_error = ""
        # Keep the last working token in memory so Settings -> disable/remove
        # can still take the existing Telegram keyboard offline after config has
        # already been saved without a token. It is never persisted here.
        self._bot_token_cache = telegram_bot_token(self.config)
        self._base_url_cache = (f"https://api.telegram.org/bot{self._bot_token_cache}/" if self._bot_token_cache else "")
        self._message_center = telegram_message_center(self.config)
        self._business_setup_warning_sent = False
        self._shutdown_ui_prepared = False
        # One live inline-keyboard panel per private chat. Result messages/photos
        # are sent first and the panel is recreated afterwards, keeping controls
        # at the visual bottom of the Telegram dialog.
        raw_panels = state.get("panels", {}) if isinstance(state.get("panels", {}), dict) else {}
        self._panel_by_chat: dict[str, int] = {}
        self._view_by_chat: dict[str, str] = {}
        for _chat, _panel in raw_panels.items():
            if not isinstance(_panel, dict):
                continue
            _mid = int(_panel.get("message_id", 0) or 0)
            if _mid > 0:
                self._panel_by_chat[str(_chat)] = _mid
                self._view_by_chat[str(_chat)] = str(_panel.get("view", "home") or "home")
        self._panel_roll_epoch: dict[str, int] = {}
        self._panel_roll_in_progress: set[str] = set()
        self._panel_locks_guard = threading.RLock()
        self._panel_locks: dict[str, threading.RLock] = {}
        # HOTFIX105: coalesce bottom-panel refreshes per chat. Previous builds
        # spawned one short-lived thread after every result/photo, which was cheap
        # individually but created avoidable scheduler churn during rapid media use.
        self._panel_bottom_timers: dict[str, threading.Timer] = {}
        self._panel_bottom_guard = threading.RLock()
        # Temporary phone-side UI state. Window names never enter callback_data
        # (Telegram limits callback_data to 64 bytes); the selected target is kept
        # locally until the owner chooses a monitor.
        self._screen_move_target_by_chat: dict[str, str] = {}
        # Audio endpoint catalogs stay local to the paired Telegram session.
        # Callback payloads contain only a short list index; stable Windows
        # endpoint IDs/names never need to fit Telegram's 64-byte callback limit.
        self._audio_device_catalog_by_chat: dict[str, dict[str, Any]] = {}
        self._bluetooth_catalog_by_chat: dict[str, list[dict[str, Any]]] = {}
        self._bluetooth_pair_catalog_by_chat: dict[str, list[dict[str, Any]]] = {}
        self._discord_channel_catalog_by_chat: dict[str, dict[str, Any]] = {}
        self._calendar_event_catalog_by_chat: dict[str, list[dict[str, Any]]] = {}
        self._remote_mouse_context_by_chat: dict[str, dict[str, Any]] = {}
        # HOTFIX98: one click button preference per paired Telegram chat. Selecting
        # a monitor immediately starts the X -> Y wizard; the optional right-click
        # toggle lives on the monitor picker instead of adding an extra blocking step.
        self._remote_mouse_button_by_chat: dict[str, str] = {}
        # Callback payloads stay tiny; full Core Audio session metadata lives only
        # in the desktop process while the user selects an application.
        self._app_volume_catalog_by_chat: dict[str, list[dict[str, Any]]] = {}
        # HOTFIX106 phone-side catalogs for destructive browser cleanup,
        # close-confirmation pickers and overlay controls. Callback payloads keep
        # only short list indices; names/paths never enter Telegram callback_data.
        self._browser_cleanup_catalog_by_chat: dict[str, list[dict[str, Any]]] = {}
        self._close_catalog_by_chat: dict[str, list[dict[str, Any]]] = {}
        self._close_pending_by_chat: dict[str, dict[str, Any]] = {}
        # Checklist item text can be long, while Telegram callback_data is capped
        # at 64 bytes. Keep the live item catalog locally and put only indices in
        # inline buttons for mark/delete pickers.
        self._checklist_catalog_by_chat: dict[str, dict[str, Any]] = {}
        # HOTFIX110: callback payloads remain short while font names and filesystem
        # paths live only in the paired desktop process. Explorer authorization is
        # granted only after the critical phrase and is revoked when Home is opened.
        self._overlay_font_catalog_by_chat: dict[str, list[str]] = {}
        self._explorer_by_chat: dict[str, dict[str, Any]] = {}
        # HOTFIX107: lightweight yes/no confirmation is deliberately separate
        # from the critical passphrase flow. It protects actions where an
        # accidental phone tap should be reversible before any tool executes.
        self._plain_confirm_by_chat: dict[str, dict[str, Any]] = {}
        # HOTFIX89: media events are multicast. Telegram owns a tiny notification
        # queue so automatic/desktop-side track changes are visible on the phone
        # without stealing the event from MainWindow.
        self._media_notifications: queue.Queue[MediaNowPlayingEvent] = queue.Queue(maxsize=64)
        self._media_thread: threading.Thread | None = None
        self._media_subscribed = False
        self._media_last_key = ""
        self._media_last_at = 0.0
        self._media_ack_until: dict[str, float] = {}
        self._media_result_event_ids: set[str] = set()
        self._media_inflight_sources: dict[str, int] = {}
        self._seen_callback_ids: dict[str, float] = {}
        # Latest event per source is also used to render the Discord media panel
        # with the same title every local NEO sees.  This is cache-only: opening
        # a Telegram keyboard never blocks on Discord/network I/O.
        self._latest_media_by_source: dict[str, MediaNowPlayingEvent] = {}
        # HOTFIX125: retain a tiny process-lifetime previous-track list per source.
        # It is updated directly from the shared media event bus, so a long-running
        # phone session does not depend on the Telegram Agent's own transient state.
        self._media_history_by_source: dict[str, list[str]] = {}
        # Desktop -> Telegram transcript mirror. One bounded worker serializes
        # writes so UI rendering never waits for Telegram network latency.
        self._assistant_mirror_queue: queue.Queue[str] = queue.Queue(maxsize=256)
        self._assistant_mirror_thread: threading.Thread | None = None
        self._assistant_mirror_last: tuple[str, float] = ("", 0.0)

    def _enqueue_media_notification(self, event: MediaNowPlayingEvent) -> None:
        source_key = str(getattr(event, "source", "") or "").casefold().strip()
        reason = str(getattr(event, "reason", "track_started") or "track_started").casefold()
        if reason in {"queue_updated", "network_unavailable"}:
            # These are desktop delivery updates, not a new playback state.
            # Never turn a delayed queue/network notice into "now playing".
            return
        if source_key and reason not in {"queue_added", "queue_removed"}:
            previous = self._latest_media_by_source.get(source_key)
            if previous is not None and previous.key != event.key:
                label = str(getattr(previous, "title", "") or "").strip()
                artist = str(getattr(previous, "artist", "") or "").strip()
                if label:
                    row = label + (f" — {artist}" if artist else "")
                    history = self._media_history_by_source.setdefault(source_key, [])
                    if not history or history[-1] != row:
                        history.append(row)
                        del history[:-8]
            self._latest_media_by_source[source_key] = event
        if self._stop.is_set():
            return
        # Always queue state changes so an open Discord Music panel can refresh
        # its now-playing line even when popup notifications are disabled.
        try:
            self._media_notifications.put_nowait(event)
        except queue.Full:
            try:
                self._media_notifications.get_nowait()
            except queue.Empty:
                pass
            try:
                self._media_notifications.put_nowait(event)
            except queue.Full:
                pass

    @staticmethod
    def _media_event_key_from_data(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        current = data.get("current") if isinstance(data.get("current"), dict) else {}
        title = str(data.get("title", "") or current.get("title", "") or "").strip()
        artist = str(data.get("artist", "") or current.get("artist", "") or "").strip()
        track_id = str(data.get("track_id", "") or current.get("track_id", "") or "").strip()
        source = "discord" if str(data.get("method", "") or "").startswith("discord") or data.get("shared_queue") else str(data.get("content_provider") or ("youtube" if data.get("media_source") == "YouTube" else "yandex_music"))
        if track_id:
            return f"{source}:{track_id}"
        if title:
            return f"{source}:{title.casefold()}|{artist.casefold()}"
        return ""

    def _ack_media_result(self, data: Any) -> None:
        # Button actions already return the new title in their Telegram result.
        # Suppress only that exact event for a brief window; desktop/automatic
        # changes still generate a dedicated now-playing message.
        key = self._media_event_key_from_data(data)
        if key:
            self._media_ack_until[key] = time.monotonic() + 1.8
        event_id = str(data.get("media_event_id", "")) if isinstance(data, dict) else ""
        if event_id:
            if len(self._media_result_event_ids) >= 256:
                self._media_result_event_ids.clear()
            self._media_result_event_ids.add(event_id)

    def _media_notification_loop(self) -> None:
        while not self._stop.is_set():
            try:
                event = self._media_notifications.get(timeout=0.45)
            except queue.Empty:
                continue
            # Let a synchronous Telegram button result acknowledge its own event
            # before we decide whether a second message is necessary.
            time.sleep(0.18)
            deadline = time.monotonic() + 35
            owner = "yandex_music" if event.source == "youtube" else event.source
            while self._media_inflight_sources.get(owner, 0) and time.monotonic() < deadline:
                if self._stop.wait(0.05):
                    return
            if getattr(event, "event_id", "") in self._media_result_event_ids:
                self._media_result_event_ids.discard(event.event_id)
                continue
            now = time.monotonic()
            self._media_ack_until = {k: v for k, v in self._media_ack_until.items() if v > now}
            if self._media_ack_until.get(event.key, 0.0) > now:
                continue
            if event.key == self._media_last_key and now - self._media_last_at < 3.0:
                continue
            self._media_last_key = event.key
            self._media_last_at = now
            source = {"discord": "Discord", "yandex_music": "Яндекс Музыка", "youtube": "YouTube"}.get(str(event.source or "").casefold(), str(event.source or "Музыка"))
            track = f"«{event.title}»" + (f" — {event.artist}" if event.artist else "")
            reason = str(getattr(event, "reason", "track_started") or "track_started").casefold()
            if reason == "queue_added":
                requester = str(getattr(event, "requester", "") or "Discord").strip()
                position = int(getattr(event, "queue_position", 0) or 0)
                suffix = f" · позиция {position}" if position > 0 else ""
                message = f"➕ Discord: {requester} добавил в очередь {track}{suffix}."
            else:
                queue_suffix = self._media_queue_suffix(str(event.source or ""))
                message = f"🎵 Сейчас играет ({source}): {track}.{queue_suffix}"
            send_popup = bool(self.config.get("integrations.telegram.media_now_playing_notifications", True))
            for chat_id in sorted(self._allowed_chat_ids()):
                if self._stop.is_set():
                    break
                try:
                    old_panel = int(self._panel_by_chat.get(str(chat_id), 0) or 0)
                    view = str(self._view_by_chat.get(str(chat_id), "home") or "home")
                    should_send = bool(getattr(event, "announce", True)) and (send_popup or reason == "queue_added")
                    if should_send:
                        self._send_message(str(chat_id), message)
                    # Queue additions and actual track changes both keep the one
                    # canonical menu visually last. The strict panel lock prevents
                    # notification/result races from creating a second keyboard.
                    if old_panel > 0 and should_send:
                        self._roll_panel_to_bottom(str(chat_id), view, old_panel)
                    elif old_panel > 0 and view == "md":
                        # NEO 187 HREQ-028: nothing new was posted below the
                        # menu, so it is still last: update it in place. Deleting
                        # and re-sending it on every track made the menu blink.
                        self._show_view(str(chat_id), view, old_panel)
                except Exception:
                    logger.debug("Telegram now-playing notification failed", exc_info=True)

    # ---------------------------------------------------------------- lifecycle
    def set_event_callback(self, callback: Callable[[str], None] | None) -> None:
        self._event_callback = callback

    def _notify_desktop(self, text: str, *, respect_action_mirror: bool = True) -> None:
        # Personal-message notifications are their own privacy setting and must
        # not disappear merely because the user disabled mirroring of Remote
        # Control actions. The mirror gate therefore applies only to ordinary
        # remote-control/status events.
        if respect_action_mirror and not bool(self.config.get("integrations.telegram.mirror_actions_to_desktop", True)):
            return
        callback = self._event_callback
        if callback is None:
            return
        try:
            callback(str(text or ""))
        except Exception:
            logger.debug("Telegram desktop event callback failed", exc_info=True)

    def mirror_assistant_message(self, text: str) -> None:
        """Asynchronously duplicate one desktop assistant message to Telegram."""
        value = str(text or "").strip()
        if not value or not self.enabled():
            return
        # Ephemeral capture acknowledgements are UI chrome, not chat history.
        if value in {"Слушаю.", "Listening."}:
            return
        # Telegram already subscribes to the same now-playing event and renders a
        # queue-aware music notification. Avoid a second duplicate mirrored copy.
        if value.startswith("Сейчас играет (") and bool(self.config.get("integrations.telegram.media_now_playing_notifications", True)):
            return
        last_text, last_at = self._assistant_mirror_last
        now = time.monotonic()
        if value == last_text and now - last_at < 1.2:
            return
        self._assistant_mirror_last = (value, now)
        try:
            self._assistant_mirror_queue.put_nowait(value)
        except queue.Full:
            try:
                self._assistant_mirror_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._assistant_mirror_queue.put_nowait(value)
            except queue.Full:
                pass

    def notify_with_buttons(self, text: str, rows: list[list[tuple[str, str]]]) -> None:
        """Send one standalone notice (e.g. «match found») to the paired chats."""
        value = str(text or "").strip()
        if not value or not self.enabled():
            return

        def send() -> None:
            for chat_id in sorted(self._allowed_chat_ids()):
                try:
                    markup = {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]} if rows else None
                    self._send_message(str(chat_id), value, reply_markup=markup)
                except Exception as exc:
                    logger.debug("Telegram notice failed for chat %s: %s", chat_id, exc)

        threading.Thread(target=send, name="NEO Telegram notice", daemon=True).start()

    def _assistant_mirror_loop(self) -> None:
        while not self._stop.is_set():
            try:
                value = self._assistant_mirror_queue.get(timeout=0.35)
            except queue.Empty:
                continue
            # Pairing may complete after the desktop message was produced. Hold
            # a queued startup/briefing message briefly rather than losing it.
            deadline = time.monotonic() + 12.0
            while not self._stop.is_set() and not self._allowed_chat_ids() and time.monotonic() < deadline:
                self._stop.wait(0.35)
            for chat_id in sorted(self._allowed_chat_ids()):
                try:
                    chat_key = str(chat_id)
                    old_panel = int(self._panel_by_chat.get(chat_key, 0) or 0)
                    view = str(self._view_by_chat.get(chat_key, "home") or "home")
                    self._send_message(chat_key, f"🤖 NEO: {value}")
                    if old_panel > 0:
                        self._roll_panel_to_bottom(chat_key, view, old_panel)
                except Exception as exc:
                    logger.debug("Telegram assistant mirror failed for chat %s: %s", chat_id, exc)

    def enabled(self) -> bool:
        return bool(self.config.get("integrations.telegram.enabled", False) or self.config.get("integrations.telegram.messages_enabled", False))

    def start(self) -> bool:
        if not self.enabled():
            logger.info("Telegram remote transport is disabled")
            return False
        token = telegram_bot_token(self.config)
        if not token:
            logger.warning("Telegram remote transport is enabled but no bot token is configured")
            return False
        self._bot_token_cache = token
        self._base_url_cache = f"https://api.telegram.org/bot{token}/"
        remote_enabled = bool(self.config.get("integrations.telegram.remote_control_enabled", False))
        with self._lock:
            # HOTFIX119: the Bot API transport can already be alive in a
            # Secretary-only/first-setup state. Enabling Remote Control in the
            # same process must still build the Remote Agent and worker lanes;
            # returning here used to make the first pairing attempt require a
            # full application restart.
            transport_alive = self._thread is not None and self._thread.is_alive()
            # Secretary-only mode needs only the Bot API update transport. Do not
            # construct a second Agent or media/mirror workers unless Telegram
            # Remote Control itself is enabled; this keeps personal-message
            # notifications effectively idle between getUpdates responses.
            if remote_enabled and self._agent is None:
                self._agent = Agent(self.config, confirmation_policy=self.policy.confirmation_override)
                self._agent.tools.set_execution_guard(self.policy.execution_guard)
                self._agent.tools.execution_transport = "telegram"
            self._shutdown_ui_prepared = False
            self._send_barrier = False
            self._stop.clear()
            if remote_enabled and (self._agent_thread is None or not self._agent_thread.is_alive()):
                self._agent_thread = threading.Thread(
                    target=self._agent_job_loop,
                    name="NeoJarvis-TelegramAgentJobs",
                    daemon=True,
                )
                self._agent_thread.start()
            if remote_enabled:
                for lane in self._tool_lane_queues:
                    thread = self._tool_lane_threads.get(lane)
                    if thread is not None and thread.is_alive():
                        continue
                    thread = threading.Thread(
                        target=self._tool_lane_loop, args=(lane,),
                        name=f"NeoJarvis-TelegramTool-{lane}", daemon=True,
                    )
                    self._tool_lane_threads[lane] = thread
                    thread.start()
            if remote_enabled and not self._media_subscribed:
                subscribe_now_playing(self._enqueue_media_notification)
                self._media_subscribed = True
            if remote_enabled and (self._media_thread is None or not self._media_thread.is_alive()):
                self._media_thread = threading.Thread(
                    target=self._media_notification_loop,
                    name="NeoJarvis-TelegramMediaEvents",
                    daemon=True,
                )
                self._media_thread.start()
            if remote_enabled and (self._assistant_mirror_thread is None or not self._assistant_mirror_thread.is_alive()):
                self._assistant_mirror_thread = threading.Thread(
                    target=self._assistant_mirror_loop,
                    name="NeoJarvis-TelegramAssistantMirror",
                    daemon=True,
                )
                self._assistant_mirror_thread.start()
            if remote_enabled and (self._callback_ack_thread is None or not self._callback_ack_thread.is_alive()):
                self._callback_ack_thread = threading.Thread(
                    target=self._callback_ack_loop,
                    name="NeoJarvis-TelegramCallbackAck",
                    daemon=True,
                )
                self._callback_ack_thread.start()
            if transport_alive:
                return True
            self._thread = threading.Thread(
                target=self._run,
                name="NeoJarvis-TelegramRemote",
                daemon=True,
            )
            self._thread.start()
        return True

    def on_network_changed(self, *_args: Any) -> None:
        session = self._session
        if session is not None:
            session.close()
        self._connected.clear()
        # Polling owns its own session and bounded retry loop. Do not start a
        # second getUpdates owner or replay already-accepted jobs here.

    def prepare_shutdown_ui(self) -> None:
        """Best-effort removal of live Telegram controls before transport stop.

        Inline keyboards live on Telegram servers, so killing the local process
        cannot retroactively remove them.  On every *graceful* stop we therefore
        delete the active control-panel message first.  If Telegram refuses the
        deletion, fall back to removing reply_markup and marking the panel offline.
        This method is intentionally bounded and uses independent HTTP sessions.
        """
        if self._shutdown_ui_prepared:
            return
        self._shutdown_ui_prepared = True
        if not (self._bot_token_cache or telegram_bot_token(self.config)):
            return

        chats = set(self._allowed_chat_ids())
        chats.update(str(x) for x in self._panel_by_chat.keys())
        for chat_id in sorted(x for x in chats if x):
            panel_id = int(self._panel_by_chat.get(str(chat_id), 0) or 0)
            if panel_id <= 0:
                continue
            deleted = False
            try:
                self._api(
                    "deleteMessage",
                    {"chat_id": str(chat_id), "message_id": panel_id},
                    timeout=2.5, fresh_session=True,
                )
                deleted = True
                self._forget_message(str(chat_id), panel_id)
            except Exception:
                logger.debug("Could not delete Telegram panel during shutdown", exc_info=True)

            if not deleted:
                # Deleting is preferable because the chat is configured to clean
                # itself, but an empty keyboard is the important safety/UX fallback.
                try:
                    self._api(
                        "editMessageText",
                        {
                            "chat_id": str(chat_id),
                            "message_id": panel_id,
                            "text": "🔴 NEO offline\n\nУдалённое управление отключено.",
                            "reply_markup": {"inline_keyboard": []},
                        },
                        timeout=2.5, fresh_session=True,
                    )
                except Exception:
                    try:
                        self._api(
                            "editMessageReplyMarkup",
                            {
                                "chat_id": str(chat_id),
                                "message_id": panel_id,
                                "reply_markup": {"inline_keyboard": []},
                            },
                            timeout=2.5, fresh_session=True,
                        )
                    except Exception:
                        logger.debug("Could not disable Telegram panel during shutdown", exc_info=True)
                self._panel_by_chat.pop(str(chat_id), None)
                self._view_by_chat.pop(str(chat_id), None)
        self._save_state()

    def stop(self) -> None:
        # Always take the visible remote controls offline first.  app.py also
        # calls this explicitly before releasing the instance lock; keeping it
        # here makes Settings -> disable Telegram behave the same way.
        self.prepare_shutdown_ui()
        # HOTFIX101: clear every message tracked by this session before stopping
        # the transport. Failed deletions remain in the ledger and are retried on
        # the next startup recovery sweep.
        if bool(self.config.get("integrations.telegram.cleanup_on_shutdown", True)):
            try:
                chats = set(self._allowed_chat_ids())
                chats.update(str(x.get("chat_id", "") or "") for x in self._tracked_messages)
                for chat_id in sorted(x for x in chats if x):
                    self._cleanup_chat(chat_id, recreate_home=False, aggressive=False, best_effort=True)
            except Exception:
                logger.debug("Telegram pre-stop tracked-history cleanup failed", exc_info=True)
        self._send_barrier = True     # NEO 188: no Telegram message after the final cleanup
        # Shutdown must be bounded. HOTFIX56 could run the expensive backfill
        # sweep here after the tray icon had already disappeared, leaving the
        # Python process alive (and therefore keeping the single-instance lock)
        # while Telegram network requests were still running. Backfill belongs at
        # startup; shutdown only needs to remove messages observed in this session.
        self._stop.set()
        # HOTFIX105: queued work belongs to the current transport session.
        # Drop anything that has not started yet so disabling/re-enabling Telegram
        # cannot replay stale media/API/Agent commands from a previous session.
        for jobs in self._tool_lane_queues.values():
            while True:
                try:
                    jobs.get_nowait()
                except queue.Empty:
                    break
                else:
                    jobs.task_done()
        while True:
            try:
                self._agent_jobs.get_nowait()
            except queue.Empty:
                break
            else:
                self._agent_jobs.task_done()
        while True:
            try:
                self._callback_ack_jobs.get_nowait()
            except queue.Empty:
                break
            else:
                self._callback_ack_jobs.task_done()
        if self._media_subscribed:
            unsubscribe_now_playing(self._enqueue_media_notification)
            self._media_subscribed = False
        media_thread = self._media_thread
        if media_thread is not None and media_thread.is_alive() and media_thread is not threading.current_thread():
            media_thread.join(timeout=0.8)
        self._media_thread = None
        mirror_thread = self._assistant_mirror_thread
        if mirror_thread is not None and mirror_thread.is_alive() and mirror_thread is not threading.current_thread():
            mirror_thread.join(timeout=0.8)
        self._assistant_mirror_thread = None
        ack_thread = self._callback_ack_thread
        if ack_thread is not None and ack_thread.is_alive() and ack_thread is not threading.current_thread():
            ack_thread.join(timeout=0.6)
        self._callback_ack_thread = None
        # HOTFIX103: the long-running Agent worker is separate from getUpdates so
        # polling stays responsive.  Join it separately on shutdown as well.
        # The join is deliberately bounded: an in-flight local-model request may
        # still be finishing, but queued jobs will not start after _stop is set.
        agent_thread = self._agent_thread
        if agent_thread is not None and agent_thread.is_alive() and agent_thread is not threading.current_thread():
            agent_thread.join(timeout=1.2)
        self._agent_thread = None
        for lane_thread in list(self._tool_lane_threads.values()):
            if lane_thread is not None and lane_thread.is_alive() and lane_thread is not threading.current_thread():
                lane_thread.join(timeout=0.8)
        self._tool_lane_threads.clear()
        session = self._session
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.8)
        self._thread = None
        self._session = None
        self._save_state()

    def set_desktop_tools(self, tools: Any | None) -> None:
        """Attach the desktop ToolRegistry for stateful media callbacks.

        The Telegram transport deliberately keeps a separate Agent so remote
        confirmation state cannot confirm a desktop/voice action.  Media adapters,
        however, hold live queue/current-track state; Telegram buttons must execute
        against the same registry as the desktop player.
        """
        self._desktop_tools = tools

    def _tools_for_direct_action(self, tool_name: str) -> Any:
        if str(tool_name or "").strip().casefold() in {"yandex_music", "spotify", "music", "discord", "media", "email", "translate", "google_calendar", "telegram_messages", "weather", "shelf", "downloads", "activity", "search", "game_locations", "mini_browser", "desktop_features"}:
            shared = self._desktop_tools
            if shared is not None:
                return shared
        return self.agent.tools

    def _try_shared_media_phrase(self, chat_id: str, user_id: str, text: str) -> bool:
        """Execute tiny Telegram media follow-ups on the desktop media state.

        Free-form Telegram keeps an isolated Agent for security, but a second
        Yandex adapter cannot know the desktop current track. Exact media
        follow-ups therefore use the shared desktop registry deterministically.
        """
        t = re.sub(r"\s+", " ", str(text or "").strip().casefold().replace("ё", "е"))
        if not t:
            return False

        # HOTFIX120: tiny transport phrases must not pay the full Telegram Agent /
        # local-LLM latency. Reuse the same conservative parser as desktop voice
        # and route against the process-wide current media owner. Only the four
        # reversible transport actions use this path; ambiguous free-form media
        # queries still fall through to the Agent.
        quick = parse_quick_media_command(text, language_code(self.config))
        if quick is not None and quick.action in {"pause", "resume", "next", "previous"}:
            owner = str(quick.target or "auto").strip().casefold()
            if owner in {"auto", "music"}:
                owner = recent_music_owner(max_age_seconds=24 * 60 * 60) or active_music_provider(self.config)
            if owner in {"yandex_music", "spotify", "discord"}:
                if owner == "discord":
                    action = {
                        "pause": "music_pause", "resume": "music_resume",
                        "next": "music_skip", "previous": "music_previous",
                    }[quick.action]
                    args: dict[str, Any] = {"action": action}
                    server = str(self.config.get("integrations.discord.default_server", "") or "").strip()
                    if server:
                        args["server"] = server
                    label = f"Discord · {action}"
                else:
                    action = "play" if quick.action == "resume" else quick.action
                    args = {"action": action}
                    label = f"{provider_label(owner, language_code(self.config))} · {action}"
                self._execute_tool_now(
                    chat_id, owner, args, label,
                    panel_message_id=int(self._panel_by_chat.get(str(chat_id), 0) or 0),
                    return_view=self._view_by_chat.get(str(chat_id), "home"),
                )
                return True

        is_discord = bool(re.search(r"\b(?:discord|дискорд\w*|дискорт\w*)\b", t, re.I))
        stripped = re.sub(r"\b(?:discord|дискорд\w*|дискорт\w*)\b", "", t, flags=re.I).strip(" ,.-")
        action = ""
        if re.fullmatch(r"(?:лайк|лайкни|поставь лайк|like)", stripped):
            action = "music_like" if is_discord else "like"
        elif re.fullmatch(r"(?:дизлайк|дислайк|дизлайкни|поставь дизлайк|dislike)", stripped):
            action = "music_dislike" if is_discord else "dislike"
        elif re.search(r"\b(?:покажи|назови|перечисли|что\s+в)\w*.*\bочеред\w*", stripped, re.I):
            action = "music_queue_status" if is_discord else "queue_status"
        elif re.search(r"\b(?:очисти|сбрось|удали)\w*.*\bочеред\w*", stripped, re.I):
            action = "music_queue_clear" if is_discord else "queue_clear"
        elif not is_discord and re.fullmatch(r"(?:включи\s+|запусти\s+|поставь\s+)?(?:мою\s+волну|моя\s+волна)", stripped):
            action = "my_wave"
        if not action:
            return False
        tool_name = "discord" if is_discord else "yandex_music"
        args: dict[str, Any] = {"action": action}
        if is_discord:
            server = str(self.config.get("integrations.discord.default_server", "") or "").strip()
            if server:
                args["server"] = server
        self._execute_tool_now(
            chat_id, tool_name, args, f"{'Discord' if is_discord else 'Яндекс Музыка'} · {action}",
            panel_message_id=int(self._panel_by_chat.get(str(chat_id), 0) or 0),
            return_view=self._view_by_chat.get(str(chat_id), "home"),
        )
        return True

    def _media_queue_suffix(self, source: str, *, limit: int = 5) -> str:
        tools = self._desktop_tools
        if tools is None:
            return ""
        src = str(source or "").strip().casefold()
        try:
            if src == "discord":
                args: dict[str, Any] = {"action": "music_queue_status"}
                server = str(self.config.get("integrations.discord.default_server", "") or "").strip()
                if server:
                    args["server"] = server
                result = tools.execute("discord", args)
            elif src in {"yandex_music", "yandex", "youtube"}:
                result = tools.execute("yandex_music", {"action": "queue_status"})
            else:
                return ""
            data = result.data if getattr(result, "success", False) and isinstance(result.data, dict) else {}
            rows = list(data.get("queue") or [])
        except Exception:
            return ""
        from jarvis.runtime.media_notice156 import queue_labels
        labels = queue_labels(rows, "ru", max(1, int(limit)))
        return ("\nОчередь дальше:\n" + "\n".join(labels)) if labels else "\nОчередь: пуста."

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def agent(self) -> Agent:
        if self._agent is None:
            raise RuntimeError("Telegram remote Agent is not started")
        return self._agent

    @property
    def agent_or_none(self) -> Agent | None:
        """Return the Remote Agent without turning a normal first-start state into an error."""
        return self._agent

    # -------------------------------------------------------------- state/ledger
    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self) -> bool:
        """HIST-002: the consistent snapshot and its commit happen under one lock, through a
        temp file owned by this write; an older snapshot can never land after a newer one."""
        lock = self.__dict__.get("_state_save_lock")
        if lock is None:
            lock = self.__dict__.setdefault("_state_save_lock", threading.RLock())
        with lock:
            tmp = None
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                self._state_seq = int(getattr(self, "_state_seq", 0) or 0) + 1
                payload = {
                    "last_update_id": int(self._last_update_id),
                    "messages": self._tracked_messages[-2000:],
                    "last_cleanup_at": float(self._last_cleanup_at),
                    "ledger_version": 188,
                    "state_seq": self._state_seq,
                    "cleanup_report": dict(self._cleanup_report),
                    "coverage_gap_at": float(self._coverage_gap_at),
                    "cleanup_retry_at": float(self._cleanup_retry_at),
                    "panels": {
                        str(chat): {
                            "message_id": int(mid),
                            "view": str(self._view_by_chat.get(str(chat), "home") or "home"),
                        }
                        for chat, mid in self._panel_by_chat.items() if int(mid or 0) > 0
                    },
                }
                encoded = json.dumps(payload, ensure_ascii=False)
                tmp = self._state_path.with_name(f"{self._state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
                tmp.write_text(encoded, encoding="utf-8")
                for attempt in (0, 1):
                    try:
                        os.replace(tmp, self._state_path)
                        tmp = None
                        return True
                    except PermissionError:
                        # Windows sharing violation (WinError 32): one bounded retry, then report.
                        if attempt:
                            raise
                        time.sleep(0.05)
                return False
            except Exception:
                logger.exception("Could not persist Telegram remote state")
                return False
            finally:
                if tmp is not None:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

    def _commit_update_id(self, update_id: int) -> bool:
        value = max(self._last_update_id, int(update_id or 0))
        self._last_update_id = value
        return self._save_state()

    def _bot_identity(self) -> str:
        """The public numeric bot id (the part of the token before «:»), never the secret."""
        head = str(self._bot_token_cache or "").split(":", 1)[0]
        return head if head.isdigit() else ""

    def _track_message(self, chat_id: str, message_id: int, *, timestamp: float | None = None, owned: bool = True, kind: str = "") -> None:
        # NEO 188 NEO-TG-001: the user's own commands/media in the paired private
        # chat are recorded too (origin «user»), before the update offset moves on,
        # so the cleanup can remove them. Only IDs, date, origin and bot id are kept.
        if not chat_id or int(message_id or 0) <= 0:
            return
        item = {"chat_id": str(chat_id), "message_id": int(message_id), "ts": float(timestamp or time.time()),
                "owned": bool(owned), "origin": "bot" if owned else "user", "bot": self._bot_identity()}
        if kind:
            item["kind"] = str(kind)[:16]
        with self._lock:
            key = (item["chat_id"], item["message_id"])
            if not any((str(x.get("chat_id")), int(x.get("message_id", 0) or 0)) == key for x in self._tracked_messages[-500:]):
                self._tracked_messages.append(item)
                if len(self._tracked_messages) > 2000:
                    dropped = self._tracked_messages[:-2000]
                    if any(time.time() - float(x.get("ts", 0) or 0) < 48 * 3600 for x in dropped):
                        self._coverage_gap_at = time.time()   # still-deletable IDs fell out of the bounded ledger
                    self._tracked_messages = self._tracked_messages[-2000:]
        self._save_state()

    def _forget_message(self, chat_id: str, message_id: int) -> None:
        with self._lock:
            self._tracked_messages = [
                item for item in self._tracked_messages
                if not (str(item.get("chat_id")) == str(chat_id) and int(item.get("message_id", 0) or 0) == int(message_id))
            ]
            if int(self._panel_by_chat.get(str(chat_id), 0) or 0) == int(message_id or 0):
                self._panel_by_chat.pop(str(chat_id), None)
                self._view_by_chat.pop(str(chat_id), None)
        self._save_state()

    def _panel_lock(self, chat_id: str) -> threading.RLock:
        key = str(chat_id)
        with self._panel_locks_guard:
            lock = self._panel_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._panel_locks[key] = lock
            return lock

    def _remember_panel(self, chat_id: str, message_id: int, view: str) -> int:
        if int(message_id or 0) > 0:
            self._panel_by_chat[str(chat_id)] = int(message_id)
            self._view_by_chat[str(chat_id)] = str(view or "home")
            self._save_state()
        return int(message_id or 0)

    def _drop_panel(self, chat_id: str, message_id: int = 0) -> None:
        chat_key = str(chat_id)
        target = int(message_id or self._panel_by_chat.get(chat_key, 0) or 0)
        if target > 0:
            self._delete_one(chat_key, target)
        self._panel_by_chat.pop(chat_key, None)
        self._view_by_chat.pop(chat_key, None)

    def _schedule_panel_bottom(self, chat_id: str, view: str, old_message_id: int, epoch: int, delay: float = 0.035) -> None:
        """Coalesce bottom-panel refreshes so rapid Telegram media actions stay cheap."""
        chat_key = str(chat_id)
        with self._panel_bottom_guard:
            previous = self._panel_bottom_timers.pop(chat_key, None)
            if previous is not None:
                try: previous.cancel()
                except Exception: pass
            def run() -> None:
                try:
                    if self._stop.is_set(): return
                    if int(self._panel_by_chat.get(chat_key, 0) or 0) != int(old_message_id or 0): return
                    if int(self._panel_roll_epoch.get(chat_key, 0) or 0) != int(epoch or 0): return
                    self._roll_panel_to_bottom(chat_key, str(view or "home"), int(old_message_id or 0))
                finally:
                    with self._panel_bottom_guard:
                        current = self._panel_bottom_timers.get(chat_key)
                        if current is timer:
                            self._panel_bottom_timers.pop(chat_key, None)
            timer = threading.Timer(max(0.0, float(delay)), run)
            timer.name = f"NeoJarvis-TelegramPanelBottom-{chat_key}"
            timer.daemon = True
            self._panel_bottom_timers[chat_key] = timer
            timer.start()

    def _roll_panel_to_bottom(self, chat_id: str, view: str, old_message_id: int = 0) -> int:
        """Recreate the single active inline keyboard at the visual bottom.

        HOTFIX102 enforces a hard one-menu invariant. Telegram cannot move a
        message, so the old panel is deleted *before* its replacement is sent.
        This deliberately removes the old no-gap handoff which could leave two
        live keyboards when concurrent result/notification threads raced or a
        delete failed after creating the replacement.
        """
        chat_key = str(chat_id)
        with self._panel_lock(chat_key):
            self._panel_roll_epoch[chat_key] = int(self._panel_roll_epoch.get(chat_key, 0) or 0) + 1
            old_id = int(old_message_id or self._panel_by_chat.get(chat_key, 0) or 0)
            if self._stop.is_set():
                return old_id
            self._panel_roll_in_progress.add(chat_key)
            try:
                if old_id > 0:
                    self._delete_one(chat_key, old_id)
                self._panel_by_chat.pop(chat_key, None)
                self._view_by_chat.pop(chat_key, None)
                self._save_state()
                self._show_view(chat_key, str(view or "home"), 0)
                return int(self._panel_by_chat.get(chat_key, 0) or 0)
            except Exception:
                logger.debug("Could not roll Telegram panel to bottom", exc_info=True)
                # Do not resurrect the deleted panel id. A later command or /launch
                # will create one clean panel instead of compounding duplicates.
                return int(self._panel_by_chat.get(chat_key, 0) or 0)
            finally:
                self._panel_roll_in_progress.discard(chat_key)

    # ------------------------------------------------------------------- config
    def _telegram_cfg(self) -> dict[str, Any]:
        value = self.config.data.setdefault("integrations", {}).setdefault("telegram", {})
        if not isinstance(value, dict):
            value = {}
            self.config.data.setdefault("integrations", {})["telegram"] = value
        return value

    def _allowed_user_ids(self) -> set[str]:
        raw = self.config.get("integrations.telegram.allowed_user_ids", []) or []
        if not isinstance(raw, list):
            raw = []
        return {str(x).strip() for x in raw if str(x).strip()}

    def _allowed_chat_ids(self) -> set[str]:
        raw = self.config.get("integrations.telegram.allowed_chat_ids", []) or []
        if not isinstance(raw, list):
            raw = []
        return {str(x).strip() for x in raw if str(x).strip()}

    def _is_authorized(self, user_id: str, chat_id: str, chat_type: str) -> bool:
        return (
            str(chat_type or "").casefold() == "private"
            and user_id in self._allowed_user_ids()
            and chat_id in self._allowed_chat_ids()
        )

    def _persist_pairing(self, user_id: str, chat_id: str) -> None:
        self.config.reload()
        cfg = self._telegram_cfg()
        cfg["allowed_user_ids"] = [user_id]
        cfg["allowed_chat_ids"] = [chat_id]
        cfg["pairing_enabled"] = False
        cfg["pairing_secret_encrypted"] = ""
        cfg["pairing_expires_at"] = 0
        self.config.save(self.config.data)
        self.config.reload()

    # ---------------------------------------------------------------------- API
    def _base_url(self) -> str:
        cached = str(getattr(self, "_base_url_cache", "") or "")
        if cached:
            return cached
        token = self._bot_token_cache or telegram_bot_token(self.config)
        if not token:
            raise RuntimeError("Telegram bot token is not configured")
        self._bot_token_cache = token
        self._base_url_cache = f"https://api.telegram.org/bot{token}/"
        return self._base_url_cache

    def _safe_network_error(self, exc: BaseException) -> str:
        text = str(exc or "Telegram network error")
        try:
            token = self._bot_token_cache or telegram_bot_token(self.config)
            if token:
                text = text.replace(token, "<redacted-token>")
        except Exception:
            pass
        return text

    def _api(
        self, method: str, payload: dict[str, Any] | None = None, *,
        timeout: float = 20.0, fresh_session: bool = False,
        session_override: requests.Session | None = None,
    ) -> Any:
        # Shutdown UI cleanup must not share the long-polling Session with the
        # worker thread.  A fresh short-lived Session avoids a race with
        # getUpdates and lets us remove Telegram controls before transport stop.
        owned = (session_override is None) and (bool(fresh_session) or self._session is None)
        if session_override is not None:
            session = session_override
        else:
            session = requests.Session() if fresh_session else (self._session or requests.Session())
        try:
            from jarvis.runtime.http_timeouts import request_timeouts
            response = session.post(self._base_url() + method, json=payload or {},
                                    timeout=request_timeouts(timeout))
        except requests.RequestException as exc:
            raise RuntimeError(self._safe_network_error(exc)) from None
        finally:
            if owned:
                try:
                    session.close()
                except Exception:
                    pass
        try:
            data = response.json()
        except Exception:
            data = {}
        if response.status_code >= 400 or not bool(data.get("ok")):
            description = str(data.get("description") or f"HTTP {response.status_code}")
            raise RuntimeError(f"Telegram API {method}: {description}")
        return data.get("result")

    def _send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        track: bool = True,
        parse_mode: str = "",
    ) -> list[int]:
        if self._send_barrier:
            # NEO 188 TGHC04: nothing is sent after the final shutdown cleanup.
            logger.debug("Telegram send skipped after the final cleanup barrier")
            return []
        value = str(text or "").strip() or "Готово."
        chunks = [value[i : i + 3800] for i in range(0, len(value), 3800)] or ["Готово."]
        # HOTFIX133: ordinary output never recreates the inline keyboard. The
        # existing canonical panel is edited in place only, which guarantees that
        # PC-assistant mirroring/screenshots cannot spawn extra Telegram menus.
        ids: list[int] = []
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            if index == len(chunks) - 1 and reply_markup is not None:
                payload["reply_markup"] = reply_markup
            try:
                result = self._api("sendMessage", payload, timeout=8.0)
            except RuntimeError as exc:
                if not str(exc).startswith("Telegram API"):
                    # Delivered or not is unknown: never resend just to clean it, but
                    # the cleanup report must not claim full coverage (TGHC12).
                    self._coverage_gap_at = time.time()
                raise
            if isinstance(result, dict):
                mid = int(result.get("message_id", 0) or 0)
                if mid:
                    ids.append(mid)
                    if track:
                        self._track_message(chat_id, mid, timestamp=float(result.get("date", time.time()) or time.time()))
        return ids

    def _send_photo(self, chat_id: str, path: str, caption: str = "") -> int:
        if self._send_barrier:
            return 0
        file_path = Path(str(path or ""))
        if not file_path.exists() or not file_path.is_file():
            raise FileNotFoundError(str(file_path))
        session = self._session or requests.Session()
        owned = self._session is None
        try:
            with file_path.open("rb") as handle:
                response = session.post(
                    self._base_url() + "sendPhoto",
                    data={"chat_id": chat_id, "caption": str(caption or "")[:900]},
                    files={"photo": handle},
                    timeout=30.0,
                )
        except requests.RequestException as exc:
            self._coverage_gap_at = time.time()
            raise RuntimeError(self._safe_network_error(exc)) from None
        finally:
            if owned:
                try:
                    session.close()
                except Exception:
                    pass
        try:
            data = response.json()
        except Exception:
            data = {}
        if response.status_code >= 400 or not bool(data.get("ok")):
            raise RuntimeError(str(data.get("description") or f"Telegram sendPhoto HTTP {response.status_code}"))
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        mid = int(result.get("message_id", 0) or 0)
        if mid:
            self._track_message(chat_id, mid, timestamp=float(result.get("date", time.time()) or time.time()))
        return mid

    def _callback_ack_loop(self) -> None:
        while not self._stop.is_set():
            try:
                callback_id, text = self._callback_ack_jobs.get(timeout=0.35)
            except queue.Empty:
                continue
            try:
                self._api(
                    "answerCallbackQuery",
                    {"callback_query_id": callback_id, "text": str(text or "")[:180]},
                    timeout=2.5,
                )
            except Exception:
                logger.debug("Could not answer Telegram callback", exc_info=True)
            finally:
                self._callback_ack_jobs.task_done()

    def _answer_callback(self, callback_id: str, text: str = "") -> None:
        """Acknowledge a button without delaying the requested action."""
        item = (str(callback_id or ""), str(text or ""))
        if not item[0]:
            return
        try:
            self._callback_ack_jobs.put_nowait(item)
        except queue.Full:
            # ACK is cosmetic; never block or reject the actual desktop command.
            logger.debug("Telegram callback ACK queue is full; action continues")

    def _edit_panel(self, chat_id: str, message_id: int, text: str, keyboard: dict[str, Any]) -> bool:
        if not message_id:
            return False
        try:
            self._api(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": int(message_id),
                    "text": str(text or "")[:3900],
                    "reply_markup": keyboard,
                },
                timeout=6.0,
            )
            return True
        except Exception as exc:
            # "message is not modified" is harmless and should not spawn a duplicate panel.
            if "not modified" in str(exc).casefold():
                return True
            logger.debug("Could not edit Telegram panel", exc_info=True)
            return False

    def _delete_one(self, chat_id: str, message_id: int) -> bool:
        try:
            self._api("deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)}, timeout=8.0)
            self._forget_message(chat_id, message_id)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------- chat cleanup
    def _aggressive_cleanup_recent(self, chat_id: str) -> None:
        """Best-effort sweep of recent private-chat message ids.

        HOTFIX56 only knew ids recorded by the current ledger, which meant a
        HOTFIX55/older-session message could survive forever. We create a tiny
        anchor message to learn the current id, delete it together with a bounded
        backward id window, and stop if Telegram reaches the undeletable age
        boundary. This never enumerates another chat; it is used only for the
        already paired private chat.
        """
        limit = max(200, min(10000, int(self.config.get(
            "integrations.telegram.cleanup_backfill_message_ids", 3000
        ) or 3000)))
        try:
            anchor_ids = self._send_message(chat_id, self._tg_message("Очистка истории…"), track=False)
            anchor = int(anchor_ids[-1] if anchor_ids else 0)
        except Exception:
            logger.debug("Could not create Telegram cleanup anchor", exc_info=True)
            return
        if anchor <= 0:
            return
        floor = max(1, anchor - limit + 1)
        cursor = anchor
        while cursor >= floor and not self._stop.is_set():
            lo = max(floor, cursor - 99)
            batch = list(range(lo, cursor + 1))
            try:
                self._api("deleteMessages", {"chat_id": chat_id, "message_ids": batch}, timeout=12.0)
            except Exception as exc:
                # Once an inferred range reaches Telegram's age boundary, older
                # ids will not become deletable; stop rather than hammering API.
                logger.debug("Telegram backfill cleanup stopped at %s..%s: %s", lo, cursor, exc)
                if cursor == anchor:
                    try:
                        self._delete_one(chat_id, anchor)
                    except Exception:
                        pass
                break
            cursor = lo - 1
            time.sleep(0.04)
        with self._lock:
            self._tracked_messages = [
                item for item in self._tracked_messages
                if str(item.get("chat_id")) != str(chat_id)
            ]
            self._panel_by_chat.pop(str(chat_id), None)
            self._view_by_chat.pop(str(chat_id), None)

    def _cleanup_chat(self, chat_id: str, *, recreate_home: bool, aggressive: bool = False, best_effort: bool = False) -> None:
        """NEO 188 NEO-TG-001: one bounded, honest pass over the known IDs of this chat."""
        from jarvis.integrations.telegram_cleanup188 import ORDINARY_WINDOW, run, status
        acquired = self._cleanup_lock.acquire(timeout=3.0) if best_effort else self._cleanup_lock.acquire(blocking=False)
        if not acquired:
            logger.debug("Telegram cleanup already running; this pass is skipped")
            return
        try:
            now = time.time()
            chat_key = str(chat_id)
            allowed = chat_key in {str(x) for x in self._allowed_chat_ids()}
            bot = self._bot_identity()
            with self._lock:
                mine = [dict(item) for item in self._tracked_messages if str(item.get("chat_id")) == chat_key]
            scope: list[dict[str, Any]] = []
            foreign: set[int] = set()
            for item in mine:
                item_bot = str(item.get("bot", "") or "")
                if item_bot and bot and item_bot != bot:
                    foreign.add(int(item.get("message_id", 0) or 0))   # another bot cannot delete them
                    continue
                if item.get("owned") is not True and not allowed:
                    continue          # the user's messages only in the currently paired private chat
                scope.append(item)
            timeout = 2.8 if best_effort else 12.0
            result = run(
                scope, now,
                lambda batch: self._api("deleteMessages", {"chat_id": chat_key, "message_ids": batch},
                                        timeout=timeout, fresh_session=best_effort),
                lambda mid: self._api("deleteMessage", {"chat_id": chat_key, "message_id": int(mid)}, timeout=8.0),
                stop=self._stop.is_set, quick=best_effort,
            )
            result.not_allowed.update(x for x in foreign if x > 0)
            done = result.finished_ids()
            with self._lock:
                # Entries added after the snapshot stay for the next pass (TGHC13).
                self._tracked_messages = [
                    item for item in self._tracked_messages
                    if not (str(item.get("chat_id")) == chat_key and int(item.get("message_id", 0) or 0) in done)
                ]
                if int(self._panel_by_chat.get(chat_key, 0) or 0) in done:
                    self._panel_by_chat.pop(chat_key, None)
                    self._view_by_chat.pop(chat_key, None)
            gap = bool(self._coverage_gap_at) and (now - self._coverage_gap_at) < ORDINARY_WINDOW
            report: dict[str, Any] = dict(result.counts())
            report.update(status=status(result, coverage_gap=gap), at=now)
            self._cleanup_report = report
            self._last_cleanup_at = now
            self._cleanup_retry_at = (now + max(60.0, min(900.0, result.retry_after or 120.0))) if result.pending else 0.0
            self._save_state()
            logger.info("Telegram cleanup %s %s", report["status"], result.counts())
        finally:
            self._cleanup_lock.release()
        if recreate_home and not self._stop.is_set() and bool(
            self.config.get("integrations.telegram.show_home_after_cleanup", True)
        ):
            try:
                self._show_home(chat_id)
            except Exception:
                logger.debug("Could not recreate Telegram home panel after cleanup", exc_info=True)

    def cleanup_summary(self, code: str = "ru") -> str:
        from jarvis.integrations.telegram_cleanup188 import summary
        return summary(self._cleanup_report, code)

    def _cleanup_due(self) -> bool:
        if not bool(self.config.get("integrations.telegram.chat_cleanup_enabled", True)):
            return False
        if self._cleanup_retry_at and time.time() >= self._cleanup_retry_at:
            return True       # a bounded retry of IDs left pending by a temporary failure
        minutes = max(5, min(240, int(self.config.get("integrations.telegram.chat_cleanup_interval_minutes", 30) or 30)))
        return (time.time() - float(self._last_cleanup_at or 0)) >= minutes * 60

    # ------------------------------------------------------------------- worker
    def _run(self) -> None:
        self._session = requests.Session()
        # Keep long polling physically separate from action/edit/send traffic. A
        # getUpdates request can stay open for 25 s, but it must not occupy the
        # connection/session used by a just-pressed media button.
        poll_session = requests.Session()
        action_adapter = requests.adapters.HTTPAdapter(pool_connections=6, pool_maxsize=20, max_retries=0, pool_block=False)
        poll_adapter = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=2, max_retries=0, pool_block=False)
        self._session.mount("https://", action_adapter)
        poll_session.mount("https://", poll_adapter)
        self._session.headers.update({"Connection": "keep-alive", "User-Agent": "NEO-JARVIS-HOTFIX120"})
        poll_session.headers.update({"Connection": "keep-alive", "User-Agent": "NEO-JARVIS-HOTFIX120-POLL"})
        failure_delay = 2.0
        bootstrapped = False
        previous_poll_error = ""
        last_poll_error_at = 0.0
        suppressed_poll_errors = 0
        try:
            try:
                me = self._api("getMe", timeout=12.0)
                # Inline keyboards are the complete remote UI. Keep Telegram's
                # client-side command menu empty so it does not duplicate controls.
                try:
                    self._api("setMyCommands", {"commands": BOT_MENU_COMMANDS}, timeout=8.0)
                    self._api("setChatMenuButton", {"menu_button": {"type": "commands"}}, timeout=8.0)
                except Exception:
                    logger.debug("Telegram command-menu sync failed", exc_info=True)
                self._connected.set()
                self._last_error = ""
                logger.info("Telegram remote connected as @%s", str((me or {}).get("username", "") or ""))
                try:
                    webhook = self._api("getWebhookInfo", timeout=8.0) or {}
                    webhook_url = str((webhook or {}).get("url", "") or "").strip() if isinstance(webhook, dict) else ""
                    pending = int((webhook or {}).get("pending_update_count", 0) or 0) if isinstance(webhook, dict) else 0
                    allowed = list((webhook or {}).get("allowed_updates") or []) if isinstance(webhook, dict) else []
                    logger.info("Telegram transport diagnostic webhook=%s pending=%s allowed_updates=%s", bool(webhook_url), pending, allowed)
                    if webhook_url:
                        # NEO owns this bot transport and uses getUpdates. Telegram
                        # explicitly forbids getUpdates while a webhook is active, so
                        # normalize the dedicated NEO bot back to long polling instead
                        # of only warning and then failing forever. Pending updates are
                        # intentionally preserved.
                        self._api("deleteWebhook", {"drop_pending_updates": False}, timeout=10.0)
                        logger.warning("Telegram webhook was active and has been disabled for local NEO getUpdates: %s", webhook_url)
                except Exception as exc:
                    logger.warning("Telegram webhook/getUpdates preparation failed: %s", self._safe_network_error(exc))
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("Telegram remote initial getMe failed: %s", exc)

            while not self._stop.is_set():
                try:
                    if not bootstrapped and self._connected.is_set():
                        bootstrapped = True
                        for chat_id in sorted(self._allowed_chat_ids()):
                            if bool(self.config.get("integrations.telegram.cleanup_on_startup", True)):
                                self._cleanup_chat(chat_id, recreate_home=True, aggressive=False)
                            else:
                                self._show_home(chat_id)
                        if bool(self.config.get("integrations.telegram.messages_enabled", False)) and not self._message_center.connection_status().get("connected") and not self._business_setup_warning_sent:
                            self._business_setup_warning_sent = True
                            logger.warning("Telegram Secretary Mode enabled in NEO but no Business connection has been observed. BotFather toggle alone is insufficient: connect the bot in Telegram account Business/Chatbots settings, choose chats and grant reply rights.")
                    # Connectivity recovery must not wait behind startup cleanup
                    # or attempts to send/recreate panels while the API is offline.
                    if self._connected.is_set() and self._cleanup_due():
                        for chat_id in sorted(self._allowed_chat_ids()):
                            # Keep the user's current submenu across the periodic
                            # 30-minute cleanup. Capture it before deleting the
                            # old panel because deletion intentionally forgets the
                            # panel/view mapping. Startup still begins at Home.
                            current_view = str(self._view_by_chat.get(str(chat_id), "home") or "home")
                            self._cleanup_chat(chat_id, recreate_home=False)
                            if not self._stop.is_set():
                                self._show_view(chat_id, current_view)

                    result = self._api(
                        "getUpdates",
                        {
                            "offset": self._last_update_id + 1,
                            "timeout": 25,
                            "allowed_updates": ["message", "callback_query", "my_chat_member", "business_connection", "business_message", "edited_business_message", "deleted_business_messages"],
                        },
                        timeout=35.0,
                        session_override=poll_session,
                    )
                    self._connected.set()
                    self._last_error = ""
                    failure_delay = 2.0
                    if previous_poll_error:
                        logger.info("Telegram polling recovered; suppressed_errors=%s", suppressed_poll_errors)
                    previous_poll_error = ""
                    suppressed_poll_errors = 0
                    for update in result or []:
                        if self._stop.is_set():
                            break
                        try:
                            update_id = int(update.get("update_id", 0) or 0)
                            if update_id <= self._last_update_id:
                                continue
                            # HOTFIX98: never acknowledge an update before its
                            # payload has been handled successfully. Business
                            # messages must remain retryable after a transient
                            # parser/storage/notification failure.
                            self._handle_update(update)
                            if not self._commit_update_id(update_id):
                                logger.warning("Telegram update handled but update_id commit failed id=%s", update_id)
                        except Exception:
                            logger.exception("Telegram update handling failed")
                except Exception as exc:
                    if self._stop.is_set():
                        break
                    self._connected.clear()
                    self._last_error = self._safe_network_error(exc)
                    key = re.sub(r"0x[0-9a-fA-F]+", "<address>", self._last_error)
                    now = time.monotonic()
                    if key != previous_poll_error or now - last_poll_error_at >= 60.0:
                        logger.warning("Telegram polling error: %s; retry_delay=%.1fs suppressed=%s",
                                       self._last_error, failure_delay, suppressed_poll_errors)
                        last_poll_error_at = now
                        suppressed_poll_errors = 0
                    else:
                        suppressed_poll_errors += 1
                    previous_poll_error = key
                    self._stop.wait(failure_delay)
                    failure_delay = min(30.0, failure_delay * 1.7)
        finally:
            self._connected.clear()
            try:
                poll_session.close()
            except Exception:
                pass
            session = self._session
            self._session = None
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def _handle_update(self, update: dict[str, Any]) -> None:
        business_connection = update.get("business_connection")
        if isinstance(business_connection, dict):
            row = self._message_center.record_connection(business_connection)
            self._business_setup_warning_sent = False
            logger.info("Telegram business connection updated id=%s enabled=%s can_reply=%s", row.get("id", ""), row.get("is_enabled", False), row.get("can_reply", False))
            return
        business_message = update.get("business_message")
        if isinstance(business_message, dict):
            connection_id = str(business_message.get("business_connection_id", "") or "").strip()
            if connection_id and not self._message_center.connection(connection_id):
                # Telegram may deliver the first business_message after a restart
                # without a fresh business_connection update. Recover the official
                # connection object by ID so incoming/outgoing ownership is known.
                try:
                    recovered = self._api("getBusinessConnection", {"business_connection_id": connection_id}, timeout=12.0)
                    if isinstance(recovered, dict):
                        self._message_center.record_connection(recovered)
                        self._business_setup_warning_sent = False
                        logger.info("Telegram business connection recovered from first message id=%s", connection_id)
                except Exception as exc:
                    logger.warning("Telegram getBusinessConnection recovery failed id=%s: %s", connection_id, exc)
            row = self._message_center.record_message(business_message)
            logger.info("Telegram business message observed connection=%s sender=%s incoming=%s", connection_id, row.get("sender_name", ""), row.get("incoming", True))
            if bool(row.get("incoming", True)) and bool(self.config.get("integrations.telegram.messages_enabled", False)) and bool(self.config.get("integrations.telegram.messages_notifications_enabled", True)):
                self._notify_desktop(format_telegram_message_notification(self.config, row), respect_action_mirror=False)
            return
        edited = update.get("edited_business_message")
        if isinstance(edited, dict):
            self._message_center.record_message(edited)
            return
        deleted = update.get("deleted_business_messages")
        if isinstance(deleted, dict):
            self._message_center.delete_messages(deleted)
            return
        my_chat_member = update.get("my_chat_member")
        if isinstance(my_chat_member, dict):
            chat = my_chat_member.get("chat") if isinstance(my_chat_member.get("chat"), dict) else {}
            row = self._message_center.record_bot_group(chat)
            if row:
                logger.info("Telegram group discovered title=%s chat_id=%s", row.get("title", ""), row.get("chat_id", ""))
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            self._handle_callback(callback)
            return
        message = update.get("message")
        if isinstance(message, dict):
            self._handle_message(message)

    # ------------------------------------------------------------------ pairing
    def _handle_pair(self, user_id: str, chat_id: str, chat_type: str, text: str, message_id: int) -> bool:
        if chat_type.casefold() != "private":
            return True
        parts = text.strip().split(maxsplit=1)
        if not parts or parts[0].split("@", 1)[0].casefold() != "/pair":
            return False
        self._track_message(chat_id, message_id, owned=False)
        if not bool(self.config.get("integrations.telegram.pairing_enabled", False)):
            self._send_message(chat_id, self._tg_message("Режим привязки выключен. Открой NEO → Настройки → Удалённое подключение и создай новый код."))
            return True
        expires = float(self.config.get("integrations.telegram.pairing_expires_at", 0) or 0)
        if expires and time.time() > expires:
            self._send_message(chat_id, self._tg_message("Код привязки истёк. Создай новый в настройках NEO."))
            return True
        supplied = parts[1].strip() if len(parts) > 1 else ""
        expected = telegram_pairing_secret(self.config)
        if not expected or not supplied or not hmac.compare_digest(supplied, expected):
            failures = self._pair_failures.get(user_id, 0) + 1
            self._pair_failures[user_id] = failures
            if failures >= 3:
                time.sleep(min(2.5, 0.35 * failures))
            self._send_message(chat_id, self._tg_message("Неверный или отсутствующий код привязки."))
            return True
        self._persist_pairing(user_id, chat_id)
        self._pair_failures.pop(user_id, None)
        self._notify_desktop("📱 Telegram · владелец успешно привязан к удалённому управлению.")
        self._cleanup_chat(chat_id, recreate_home=False, aggressive=True)
        self._send_message(chat_id, self._tg_message("✅ Telegram привязан к этому NEO."))
        self._show_home(chat_id)
        logger.info("Telegram remote paired user_id=%s chat_id=%s", user_id, chat_id)
        return True

    # HOTFIX101: Telegram chrome follows the selected application language.
    _TG_LABELS: dict[str, dict[str, str]] = {
        "Переключаю":{"en":"Switching","de":"Ich wechsle den Titel","fr":"Je change de piste","es":"Cambiando de pista","it":"Cambio traccia","pt":"A mudar de faixa"},
        "Настройки":{"en":"Settings","de":"Einstellungen","fr":"Paramètres","es":"Ajustes","it":"Impostazioni","pt":"Configurações"},
        "Дополнительно":{"en":"Advanced","de":"Erweitert","fr":"Avancé","es":"Avanzado","it":"Avanzate","pt":"Avançado"},
        "Окно":{"en":"Window","de":"Fenster","fr":"Fenêtre","es":"Ventana","it":"Finestra","pt":"Janela"},
        "Робот":{"en":"Robot","de":"Roboter","fr":"Robot","es":"Robot","it":"Robot","pt":"Robô"},
        "Книжная полка":{"en":"Shelf","de":"Ablage","fr":"Étagère","es":"Estantería","it":"Scaffale","pt":"Estante"},
        "Виджеты":{"en":"Widgets","de":"Widgets","fr":"Widgets","es":"Widgets","it":"Widget","pt":"Widgets"},
        "Содержимое полки":{"en":"Shelf contents","de":"Ablageinhalt","fr":"Contenu de l’étagère","es":"Contenido de la estantería","it":"Contenuto dello scaffale","pt":"Conteúdo da estante"},
        "История действий":{"en":"Activity history","de":"Aktivitätsverlauf","fr":"Historique d’activité","es":"Historial de actividad","it":"Cronologia attività","pt":"Histórico de atividade"},
        "Найти файл":{"en":"Find a file","de":"Datei suchen","fr":"Trouver un fichier","es":"Buscar archivo","it":"Trova file","pt":"Encontrar arquivo"},
        "Данные игры":{"en":"Game data","de":"Spieldaten","fr":"Données du jeu","es":"Datos del juego","it":"Dati del gioco","pt":"Dados do jogo"},
        "Мини-браузер":{"en":"Mini browser","de":"Mini-Browser","fr":"Mini navigateur","es":"Mini navegador","it":"Mini browser","pt":"Mini navegador"},
        "Новая заметка":{"en":"New note","de":"Neue Notiz","fr":"Nouvelle note","es":"Nueva nota","it":"Nuova nota","pt":"Nova nota"},
        "Музыка":{"en":"Music","de":"Musik","fr":"Musique","es":"Música","it":"Musica","pt":"Música"},
        "Discord":{"en":"Discord","de":"Discord","fr":"Discord","es":"Discord","it":"Discord","pt":"Discord"},
        "Приложения":{"en":"Apps","de":"Apps","fr":"Applications","es":"Aplicaciones","it":"App","pt":"Aplicativos"},
        "Проводник":{"en":"Explorer","de":"Explorer","fr":"Explorateur","es":"Explorador","it":"Esplora file","pt":"Explorador"},
        "Очистить историю":{"en":"Clear history","de":"Verlauf löschen","fr":"Effacer l’historique","es":"Borrar historial","it":"Cancella cronologia","pt":"Limpar histórico"},
        "Включить все":{"en":"Enable all","de":"Alle aktivieren","fr":"Tout activer","es":"Activar todo","it":"Attiva tutto","pt":"Ativar tudo"},
        "Выключить все":{"en":"Disable all","de":"Alle deaktivieren","fr":"Tout désactiver","es":"Desactivar todo","it":"Disattiva tutto","pt":"Desativar tudo"},
        "Общие настройки":{"en":"General settings","de":"Allgemeine Einstellungen","fr":"Réglages généraux","es":"Ajustes generales","it":"Impostazioni generali","pt":"Configurações gerais"},
        "Настройки вида":{"en":"Appearance settings","de":"Darstellung","fr":"Réglages d’apparence","es":"Ajustes de apariencia","it":"Aspetto","pt":"Aparência"},
        "Пресет":{"en":"Preset","de":"Preset","fr":"Préréglage","es":"Preajuste","it":"Preset","pt":"Predefinição"},
        "Пользовательский":{"en":"Custom","de":"Benutzerdefiniert","fr":"Personnalisé","es":"Personalizado","it":"Personalizzato","pt":"Personalizado"},
        "Размер шрифта":{"en":"Font size","de":"Schriftgröße","fr":"Taille de police","es":"Tamaño de fuente","it":"Dimensione carattere","pt":"Tamanho da fonte"},
        "Шрифт":{"en":"Font","de":"Schrift","fr":"Police","es":"Fuente","it":"Carattere","pt":"Fonte"},
        "Прозрачность фона":{"en":"Background transparency","de":"Hintergrundtransparenz","fr":"Transparence du fond","es":"Transparencia del fondo","it":"Trasparenza sfondo","pt":"Transparência do fundo"},
        "Диски":{"en":"Drives","de":"Laufwerke","fr":"Lecteurs","es":"Unidades","it":"Unità","pt":"Unidades"},
        "Открыть":{"en":"Open","de":"Öffnen","fr":"Ouvrir","es":"Abrir","it":"Apri","pt":"Abrir"},
        "Открыть эту папку":{"en":"Open this folder","de":"Diesen Ordner öffnen","fr":"Ouvrir ce dossier","es":"Abrir esta carpeta","it":"Apri questa cartella","pt":"Abrir esta pasta"},
        "Вверх":{"en":"Up","de":"Nach oben","fr":"Dossier parent","es":"Arriba","it":"Su","pt":"Acima"},
        "Папка":{"en":"Folder","de":"Ordner","fr":"Dossier","es":"Carpeta","it":"Cartella","pt":"Pasta"},
        "Мышь":{"en":"Mouse","de":"Maus","fr":"Souris","es":"Ratón","it":"Mouse","pt":"Mouse"},
        "Браузер":{"en":"Browser","de":"Browser","fr":"Navigateur","es":"Navegador","it":"Browser","pt":"Navegador"},
        "Поиск информации":{"en":"Information search","de":"Informationssuche","fr":"Recherche d’informations","es":"Buscar información","it":"Ricerca informazioni","pt":"Pesquisar informações"},
        "Ввод":{"en":"Input","de":"Eingang","fr":"Entrée","es":"Entrada","it":"Ingresso","pt":"Entrada"},
        "Вывод":{"en":"Output","de":"Ausgang","fr":"Sortie","es":"Salida","it":"Uscita","pt":"Saída"},
        "Нажать мышью":{"en":"Mouse click","de":"Mausklick","fr":"Clic souris","es":"Clic del ratón","it":"Clic mouse","pt":"Clique do mouse"},
        "Нажать клавишу":{"en":"Press key","de":"Taste drücken","fr":"Appuyer sur une touche","es":"Pulsar tecla","it":"Premi tasto","pt":"Pressionar tecla"},
        "Система":{"en":"System","de":"System","fr":"Système","es":"Sistema","it":"Sistema","pt":"Sistema"},
        "Скриншот":{"en":"Screenshot","de":"Screenshot","fr":"Capture d’écran","es":"Captura de pantalla","it":"Screenshot","pt":"Captura de tela"},
        "Сведения ПК":{"en":"PC info","de":"PC-Info","fr":"Infos PC","es":"Información del PC","it":"Info PC","pt":"Informações do PC"},
        "Процессы":{"en":"Processes","de":"Prozesse","fr":"Processus","es":"Procesos","it":"Processi","pt":"Processos"},
        "Сеть":{"en":"Network","de":"Netzwerk","fr":"Réseau","es":"Red","it":"Rete","pt":"Rede"},
        "Почта":{"en":"Mail","de":"E-Mail","fr":"Courrier","es":"Correo","it":"Posta","pt":"E-mail"},
        "Расписание":{"en":"Schedule","de":"Kalender","fr":"Planning","es":"Agenda","it":"Agenda","pt":"Agenda"},
        "Погода":{"en":"Weather","de":"Wetter","fr":"Météo","es":"Tiempo","it":"Meteo","pt":"Clima"},
        "Сценарии":{"en":"Scenarios","de":"Szenarien","fr":"Scénarios","es":"Escenarios","it":"Scenari","pt":"Cenários"},
        "Сводка дня":{"en":"Daily briefing","de":"Tagesübersicht","fr":"Résumé du jour","es":"Resumen diario","it":"Riepilogo giornaliero","pt":"Resumo do dia"},
        "Показать сводку сейчас":{"en":"Show briefing now","de":"Übersicht jetzt zeigen","fr":"Afficher le résumé","es":"Mostrar resumen ahora","it":"Mostra riepilogo ora","pt":"Mostrar resumo agora"},
        "Назад":{"en":"Back","de":"Zurück","fr":"Retour","es":"Atrás","it":"Indietro","pt":"Voltar"},
        "Домой":{"en":"Home","de":"Start","fr":"Accueil","es":"Inicio","it":"Home","pt":"Início"},
        "Отмена":{"en":"Cancel","de":"Abbrechen","fr":"Annuler","es":"Cancelar","it":"Annulla","pt":"Cancelar"},
        "Плейлист":{"en":"Playlist","de":"Playlist","fr":"Playlist","es":"Lista","it":"Playlist","pt":"Playlist"},
        "Трек":{"en":"Track","de":"Titel","fr":"Titre","es":"Pista","it":"Brano","pt":"Faixa"},
        "Назад трек":{"en":"Previous","de":"Zurück","fr":"Précédent","es":"Anterior","it":"Precedente","pt":"Anterior"},
        "Далее":{"en":"Next","de":"Weiter","fr":"Suivant","es":"Siguiente","it":"Avanti","pt":"Próxima"},
        "Пауза":{"en":"Pause","de":"Pause","fr":"Pause","es":"Pausa","it":"Pausa","pt":"Pausa"},
        "Продолжить":{"en":"Resume","de":"Fortsetzen","fr":"Reprendre","es":"Reanudar","it":"Riprendi","pt":"Retomar"},
        "Стоп":{"en":"Stop","de":"Stopp","fr":"Stop","es":"Detener","it":"Stop","pt":"Parar"},
        "Статус":{"en":"Status","de":"Status","fr":"État","es":"Estado","it":"Stato","pt":"Status"},
        "Громкость музыки":{"en":"Music volume","de":"Musiklautstärke","fr":"Volume de la musique","es":"Volumen de música","it":"Volume musica","pt":"Volume da música"},
        "Моя волна":{"en":"My Wave","de":"Meine Welle","fr":"Ma Vague","es":"Mi Onda","it":"La mia Onda","pt":"Minha Onda"},
        "Мои треки":{"en":"My tracks","de":"Meine Titel","fr":"Mes titres","es":"Mis pistas","it":"I miei brani","pt":"Minhas faixas"},
        "Лайк":{"en":"Like","de":"Gefällt mir","fr":"J’aime","es":"Me gusta","it":"Mi piace","pt":"Curtir"},
        "Дизлайк":{"en":"Dislike","de":"Gefällt mir nicht","fr":"Je n’aime pas","es":"No me gusta","it":"Non mi piace","pt":"Não curtir"},
        "Очередь":{"en":"Queue","de":"Warteschlange","fr":"File d’attente","es":"Cola","it":"Coda","pt":"Fila"},
        "Очистить":{"en":"Clear","de":"Leeren","fr":"Effacer","es":"Limpiar","it":"Svuota","pt":"Limpar"},
        "Поиск видео":{"en":"Search video","de":"Video suchen","fr":"Rechercher une vidéo","es":"Buscar vídeo","it":"Cerca video","pt":"Pesquisar vídeo"},
        "На весь экран":{"en":"Fullscreen","de":"Vollbild","fr":"Plein écran","es":"Pantalla completa","it":"Schermo intero","pt":"Tela cheia"},
        "Растянутый":{"en":"Theater mode","de":"Kinomodus","fr":"Mode cinéma","es":"Modo cine","it":"Modalità cinema","pt":"Modo cinema"},
        "Перейти на время":{"en":"Go to time","de":"Zu Zeit springen","fr":"Aller au temps","es":"Ir al tiempo","it":"Vai al tempo","pt":"Ir para o tempo"},
        "Скорость":{"en":"Speed","de":"Geschwindigkeit","fr":"Vitesse","es":"Velocidad","it":"Velocità","pt":"Velocidade"},
        "Качество":{"en":"Quality","de":"Qualität","fr":"Qualité","es":"Calidad","it":"Qualità","pt":"Qualidade"},
        "Предыдущее видео":{"en":"Previous video","de":"Vorheriges Video","fr":"Vidéo précédente","es":"Vídeo anterior","it":"Video precedente","pt":"Vídeo anterior"},
        "Следующее видео":{"en":"Next video","de":"Nächstes Video","fr":"Vidéo suivante","es":"Vídeo siguiente","it":"Video successivo","pt":"Próximo vídeo"},
        "Предыдущий Shorts":{"en":"Previous Short","de":"Vorheriger Short","fr":"Short précédent","es":"Short anterior","it":"Short precedente","pt":"Short anterior"},
        "Следующий Shorts":{"en":"Next Short","de":"Nächster Short","fr":"Short suivant","es":"Short siguiente","it":"Short successivo","pt":"Próximo Short"},
        "Открыть URL":{"en":"Open URL","de":"URL öffnen","fr":"Ouvrir URL","es":"Abrir URL","it":"Apri URL","pt":"Abrir URL"},
        "Вкладка №":{"en":"Tab #","de":"Tab Nr.","fr":"Onglet n°","es":"Pestaña n.º","it":"Scheda n.","pt":"Aba nº"},
        "Вкладка по имени":{"en":"Tab by name","de":"Tab nach Name","fr":"Onglet par nom","es":"Pestaña por nombre","it":"Scheda per nome","pt":"Aba por nome"},
        "Новая вкладка":{"en":"New tab","de":"Neuer Tab","fr":"Nouvel onglet","es":"Nueva pestaña","it":"Nuova scheda","pt":"Nova aba"},
        "Закрыть вкладку":{"en":"Close tab","de":"Tab schließen","fr":"Fermer l’onglet","es":"Cerrar pestaña","it":"Chiudi scheda","pt":"Fechar aba"},
        "Активные приложения":{"en":"Active apps","de":"Aktive Apps","fr":"Applications actives","es":"Aplicaciones activas","it":"App attive","pt":"Aplicativos ativos"},
        "Громкость приложения":{"en":"App volume","de":"App-Lautstärke","fr":"Volume de l’application","es":"Volumen de aplicación","it":"Volume app","pt":"Volume do aplicativo"},
        "Запустить приложение":{"en":"Launch app","de":"App starten","fr":"Lancer l’application","es":"Abrir aplicación","it":"Avvia app","pt":"Abrir aplicativo"},
        "Закрыть приложение":{"en":"Close app","de":"App schließen","fr":"Fermer l’application","es":"Cerrar aplicación","it":"Chiudi app","pt":"Fechar aplicativo"},
        "Запустить игру":{"en":"Launch game","de":"Spiel starten","fr":"Lancer le jeu","es":"Abrir juego","it":"Avvia gioco","pt":"Abrir jogo"},
        "Закрыть игру":{"en":"Close game","de":"Spiel schließen","fr":"Fermer le jeu","es":"Cerrar juego","it":"Chiudi gioco","pt":"Fechar jogo"},
        "Работа с окнами":{"en":"Window control","de":"Fenstersteuerung","fr":"Gestion des fenêtres","es":"Control de ventanas","it":"Gestione finestre","pt":"Controle de janelas"},
        "Переместить окно":{"en":"Move window","de":"Fenster verschieben","fr":"Déplacer la fenêtre","es":"Mover ventana","it":"Sposta finestra","pt":"Mover janela"},
        "Свернуть окно":{"en":"Minimize window","de":"Fenster minimieren","fr":"Réduire la fenêtre","es":"Minimizar ventana","it":"Riduci finestra","pt":"Minimizar janela"},
        "Окна по экранам":{"en":"Windows by screen","de":"Fenster nach Bildschirm","fr":"Fenêtres par écran","es":"Ventanas por pantalla","it":"Finestre per schermo","pt":"Janelas por tela"},
        "Все экраны":{"en":"All screens","de":"Alle Bildschirme","fr":"Tous les écrans","es":"Todas las pantallas","it":"Tutti gli schermi","pt":"Todas as telas"},
        "Обновить":{"en":"Refresh","de":"Aktualisieren","fr":"Actualiser","es":"Actualizar","it":"Aggiorna","pt":"Atualizar"},
        "Текущий уровень":{"en":"Current level","de":"Aktueller Wert","fr":"Niveau actuel","es":"Nivel actual","it":"Livello attuale","pt":"Nível atual"},
        "Другая громкость":{"en":"Custom volume","de":"Andere Lautstärke","fr":"Autre volume","es":"Otro volumen","it":"Altro volume","pt":"Outro volume"},
        "Микрофон по умолчанию":{"en":"Default microphone","de":"Standardmikrofon","fr":"Microphone par défaut","es":"Micrófono predeterminado","it":"Microfono predefinito","pt":"Microfone padrão"},
        "Устройство вывода":{"en":"Output device","de":"Ausgabegerät","fr":"Périphérique de sortie","es":"Dispositivo de salida","it":"Dispositivo di uscita","pt":"Dispositivo de saída"},
        "Другое":{"en":"Other","de":"Andere","fr":"Autre","es":"Otro","it":"Altro","pt":"Outro"},
        "Авто":{"en":"Auto","de":"Auto","fr":"Auto","es":"Auto","it":"Auto","pt":"Auto"},
        "15 с":{"en":"15 s","de":"15 s","fr":"15 s","es":"15 s","it":"15 s","pt":"15 s"},
        "15 с ⏩":{"en":"15 s ⏩","de":"15 s ⏩","fr":"15 s ⏩","es":"15 s ⏩","it":"15 s ⏩","pt":"15 s ⏩"},
        "30 с":{"en":"30 s","de":"30 s","fr":"30 s","es":"30 s","it":"30 s","pt":"30 s"},
        "30 с ⏩":{"en":"30 s ⏩","de":"30 s ⏩","fr":"30 s ⏩","es":"30 s ⏩","it":"30 s ⏩","pt":"30 s ⏩"},
        "1 мин":{"en":"1 min","de":"1 Min.","fr":"1 min","es":"1 min","it":"1 min","pt":"1 min"},
        "1 мин ⏩":{"en":"1 min ⏩","de":"1 Min. ⏩","fr":"1 min ⏩","es":"1 min ⏩","it":"1 min ⏩","pt":"1 min ⏩"},
        "7 дней":{"en":"7 days","de":"7 Tage","fr":"7 jours","es":"7 días","it":"7 giorni","pt":"7 dias"},
        "Баланс":{"en":"Balanced","de":"Ausbalanciert","fr":"Équilibré","es":"Equilibrado","it":"Bilanciato","pt":"Equilibrado"},
        "Включить":{"en":"Enable","de":"Aktivieren","fr":"Activer","es":"Activar","it":"Attiva","pt":"Ativar"},
        "Всё расписание":{"en":"Full schedule","de":"Gesamter Kalender","fr":"Tout l’agenda","es":"Agenda completa","it":"Calendario completo","pt":"Agenda completa"},
        "Выключить":{"en":"Disable","de":"Deaktivieren","fr":"Désactiver","es":"Desactivar","it":"Disattiva","pt":"Desativar"},
        "Выключить дисплеи":{"en":"Turn off displays","de":"Bildschirme ausschalten","fr":"Éteindre les écrans","es":"Apagar pantallas","it":"Spegni schermi","pt":"Desligar telas"},
        "Гибернация":{"en":"Hibernate","de":"Ruhezustand","fr":"Hibernation","es":"Hibernar","it":"Iberna","pt":"Hibernar"},
        "Добавить":{"en":"Add","de":"Hinzufügen","fr":"Ajouter","es":"Añadir","it":"Aggiungi","pt":"Adicionar"},
        "За 24 часа":{"en":"Last 24 hours","de":"Letzte 24 Stunden","fr":"Dernières 24 h","es":"Últimas 24 horas","it":"Ultime 24 ore","pt":"Últimas 24 horas"},
        "Заблокировать":{"en":"Lock","de":"Sperren","fr":"Verrouiller","es":"Bloquear","it":"Blocca","pt":"Bloquear"},
        "Завтра":{"en":"Tomorrow","de":"Morgen","fr":"Demain","es":"Mañana","it":"Domani","pt":"Amanhã"},
        "Компьютер":{"en":"Computer","de":"Computer","fr":"Ordinateur","es":"Ordenador","it":"Computer","pt":"Computador"},
        "Нет":{"en":"No","de":"Nein","fr":"Non","es":"No","it":"No","pt":"Não"},
        "Нет доступных каналов":{"en":"No channels available","de":"Keine Kanäle verfügbar","fr":"Aucun canal disponible","es":"No hay canales disponibles","it":"Nessun canale disponibile","pt":"Nenhum canal disponível"},
        "Обе почты":{"en":"Both mailboxes","de":"Beide Postfächer","fr":"Les deux boîtes mail","es":"Ambos buzones","it":"Entrambe le caselle","pt":"Ambas as caixas"},
        "Обновить погоду":{"en":"Refresh weather","de":"Wetter aktualisieren","fr":"Actualiser la météo","es":"Actualizar clima","it":"Aggiorna meteo","pt":"Atualizar clima"},
        "Перезагрузка":{"en":"Restart","de":"Neustart","fr":"Redémarrer","es":"Reiniciar","it":"Riavvia","pt":"Reiniciar"},
        "План питания":{"en":"Power plan","de":"Energiesparplan","fr":"Mode d’alimentation","es":"Plan de energía","it":"Piano energia","pt":"Plano de energia"},
        "Последние":{"en":"Recent","de":"Neueste","fr":"Récents","es":"Recientes","it":"Recenti","pt":"Recentes"},
        "Правый Shift":{"en":"Right Shift","de":"Rechte Umschalttaste","fr":"Maj droite","es":"Mayús derecha","it":"Maiusc destro","pt":"Shift direito"},
        "Пробел":{"en":"Space","de":"Leertaste","fr":"Espace","es":"Espacio","it":"Spazio","pt":"Espaço"},
        "Производительность":{"en":"Performance","de":"Leistung","fr":"Performances","es":"Rendimiento","it":"Prestazioni","pt":"Desempenho"},
        "Разблокировать":{"en":"Unlock","de":"Entsperren","fr":"Déverrouiller","es":"Desbloquear","it":"Sblocca","pt":"Desbloquear"},
        "Редактировать":{"en":"Edit","de":"Bearbeiten","fr":"Modifier","es":"Editar","it":"Modifica","pt":"Editar"},
        "Своё / текст / комбинация":{"en":"Custom / text / combination","de":"Eigene / Text / Kombination","fr":"Personnalisé / texte / combinaison","es":"Personalizado / texto / combinación","it":"Personalizzato / testo / combinazione","pt":"Personalizado / texto / combinação"},
        "Сегодня":{"en":"Today","de":"Heute","fr":"Aujourd’hui","es":"Hoy","it":"Oggi","pt":"Hoje"},
        "Скриншоты":{"en":"Screenshots","de":"Screenshots","fr":"Captures","es":"Capturas","it":"Screenshot","pt":"Capturas"},
        "Сон":{"en":"Sleep","de":"Standby","fr":"Veille","es":"Suspender","it":"Sospensione","pt":"Suspender"},
        "Сопряжение":{"en":"Pairing","de":"Kopplung","fr":"Appairage","es":"Emparejamiento","it":"Associazione","pt":"Pareamento"},
        "Статистика":{"en":"Statistics","de":"Statistik","fr":"Statistiques","es":"Estadísticas","it":"Statistiche","pt":"Estatísticas"},
        "Текущий":{"en":"Current","de":"Aktuell","fr":"Actuel","es":"Actual","it":"Attuale","pt":"Atual"},
        "Удалить":{"en":"Delete","de":"Löschen","fr":"Supprimer","es":"Eliminar","it":"Elimina","pt":"Excluir"},
        "Устройства пока не найдены":{"en":"No devices found","de":"Keine Geräte gefunden","fr":"Aucun appareil trouvé","es":"No se encontraron dispositivos","it":"Nessun dispositivo trovato","pt":"Nenhum dispositivo encontrado"},
        "Энергосбережение":{"en":"Power saver","de":"Energiesparen","fr":"Économie d’énergie","es":"Ahorro de energía","it":"Risparmio energetico","pt":"Economia de energia"},
        "Яркость":{"en":"Brightness","de":"Helligkeit","fr":"Luminosité","es":"Brillo","it":"Luminosità","pt":"Brilho"},
    }

    _TG_MESSAGES: dict[str, dict[str, str]] = {
        "✅ Telegram привязан к этому NEO.": {"en":"✅ Telegram is paired with this NEO.","de":"✅ Telegram ist mit diesem NEO gekoppelt.","fr":"✅ Telegram est associé à ce NEO.","es":"✅ Telegram está vinculado a este NEO.","it":"✅ Telegram è associato a questo NEO.","pt":"✅ O Telegram está vinculado a este NEO."},
        "Неизвестное действие": {"en":"Unknown action","de":"Unbekannte Aktion","fr":"Action inconnue","es":"Acción desconocida","it":"Azione sconosciuta","pt":"Ação desconhecida"},
        "Очистка истории…": {"en":"Clearing history…","de":"Verlauf wird gelöscht…","fr":"Effacement de l’historique…","es":"Limpiando historial…","it":"Pulizia della cronologia…","pt":"Limpando histórico…"},
        "Режим привязки выключен. Открой NEO → Настройки → Удалённое подключение и создай новый код.": {"en":"Pairing mode is disabled. Open NEO → Settings → Remote connection and create a new code.","de":"Der Kopplungsmodus ist deaktiviert. Öffne NEO → Einstellungen → Remote-Verbindung und erstelle einen neuen Code.","fr":"Le mode d’association est désactivé. Ouvrez NEO → Paramètres → Connexion à distance et créez un nouveau code.","es":"El modo de vinculación está desactivado. Abre NEO → Ajustes → Conexión remota y crea un código nuevo.","it":"La modalità di associazione è disattivata. Apri NEO → Impostazioni → Connessione remota e crea un nuovo codice.","pt":"O modo de vinculação está desativado. Abra NEO → Configurações → Conexão remota e crie um novo código."},
        "Код привязки истёк. Создай новый в настройках NEO.": {"en":"The pairing code has expired. Create a new one in NEO settings.","de":"Der Kopplungscode ist abgelaufen. Erstelle in den NEO-Einstellungen einen neuen.","fr":"Le code d’association a expiré. Créez-en un nouveau dans les paramètres de NEO.","es":"El código de vinculación ha caducado. Crea uno nuevo en los ajustes de NEO.","it":"Il codice di associazione è scaduto. Creane uno nuovo nelle impostazioni di NEO.","pt":"O código de vinculação expirou. Crie um novo nas configurações do NEO."},
        "Неверный или отсутствующий код привязки.": {"en":"The pairing code is missing or incorrect.","de":"Der Kopplungscode fehlt oder ist falsch.","fr":"Le code d’association est absent ou incorrect.","es":"Falta el código de vinculación o es incorrecto.","it":"Il codice di associazione manca o non è corretto.","pt":"O código de vinculação está ausente ou incorreto."},
        "🔐 Критическое действие заблокировано: сначала задай кодовую фразу в NEO → Настройки → Удалённое подключение.": {"en":"🔐 Critical action blocked: first set a passphrase in NEO → Settings → Remote connection.","de":"🔐 Kritische Aktion blockiert: Lege zuerst eine Codephrase unter NEO → Einstellungen → Remote-Verbindung fest.","fr":"🔐 Action critique bloquée : définissez d’abord une phrase secrète dans NEO → Paramètres → Connexion à distance.","es":"🔐 Acción crítica bloqueada: primero configura una frase de seguridad en NEO → Ajustes → Conexión remota.","it":"🔐 Azione critica bloccata: imposta prima una frase di sicurezza in NEO → Impostazioni → Connessione remota.","pt":"🔐 Ação crítica bloqueada: primeiro defina uma frase de segurança em NEO → Configurações → Conexão remota."},
        "Слишком быстро. Повтори команду через секунду.": {"en":"Too fast. Try the command again in a second.","de":"Zu schnell. Wiederhole den Befehl in einer Sekunde.","fr":"Trop rapide. Réessayez la commande dans une seconde.","es":"Demasiado rápido. Repite el comando dentro de un segundo.","it":"Troppo veloce. Ripeti il comando tra un secondo.","pt":"Rápido demais. Repita o comando em um segundo."},
        "Неизвестная команда. Используй кнопки или обычную фразу Phrase Atlas.": {"en":"Unknown command. Use the buttons or a normal Phrase Atlas command.","de":"Unbekannter Befehl. Verwende die Schaltflächen oder einen normalen Phrase-Atlas-Befehl.","fr":"Commande inconnue. Utilisez les boutons ou une commande Phrase Atlas normale.","es":"Comando desconocido. Usa los botones o un comando normal de Phrase Atlas.","it":"Comando sconosciuto. Usa i pulsanti o un normale comando Phrase Atlas.","pt":"Comando desconhecido. Use os botões ou um comando normal do Phrase Atlas."},
        "Пустое значение не принято.": {"en":"An empty value is not accepted.","de":"Ein leerer Wert wird nicht akzeptiert.","fr":"Une valeur vide n’est pas acceptée.","es":"No se acepta un valor vacío.","it":"Un valore vuoto non è accettato.","pt":"Um valor vazio não é aceito."},
        "Нет критического действия, ожидающего кодовую фразу.": {"en":"There is no critical action waiting for a passphrase.","de":"Es wartet keine kritische Aktion auf eine Codephrase.","fr":"Aucune action critique n’attend de phrase secrète.","es":"No hay ninguna acción crítica esperando una frase de seguridad.","it":"Non ci sono azioni critiche in attesa di una frase di sicurezza.","pt":"Não há nenhuma ação crítica aguardando uma frase de segurança."},
        "Время подтверждения истекло.": {"en":"The confirmation time has expired.","de":"Die Bestätigungsfrist ist abgelaufen.","fr":"Le délai de confirmation a expiré.","es":"El tiempo de confirmación ha caducado.","it":"Il tempo di conferma è scaduto.","pt":"O tempo de confirmação expirou."},
        "Доступ запрещён": {"en":"Access denied","de":"Zugriff verweigert","fr":"Accès refusé","es":"Acceso denegado","it":"Accesso negato","pt":"Acesso negado"},
        "ℹ️ Управление обычным Discord user-аккаунтом удалено. Discord запрещает self-bot/user-bot автоматизацию; NEO использует только официальный NeoBot для музыки.": {"en":"ℹ️ Control of a regular Discord user account has been removed. Discord prohibits self-bot/user-bot automation; NEO uses only the official NeoBot for music.","de":"ℹ️ Die Steuerung eines normalen Discord-Benutzerkontos wurde entfernt. Discord verbietet Self-Bot/User-Bot-Automatisierung; NEO verwendet für Musik nur den offiziellen NeoBot.","fr":"ℹ️ Le contrôle d’un compte utilisateur Discord ordinaire a été supprimé. Discord interdit l’automatisation self-bot/user-bot ; NEO utilise uniquement le NeoBot officiel pour la musique.","es":"ℹ️ Se eliminó el control de una cuenta de usuario normal de Discord. Discord prohíbe la automatización self-bot/user-bot; NEO usa únicamente el NeoBot oficial para música.","it":"ℹ️ Il controllo di un normale account utente Discord è stato rimosso. Discord vieta l’automazione self-bot/user-bot; NEO usa solo il NeoBot ufficiale per la musica.","pt":"ℹ️ O controle de uma conta de usuário comum do Discord foi removido. O Discord proíbe automação self-bot/user-bot; o NEO usa apenas o NeoBot oficial para música."},
        "Список аудиосессий устарел. Открой его заново.": {"en":"The audio-session list is stale. Open it again.","de":"Die Liste der Audiositzungen ist veraltet. Öffne sie erneut.","fr":"La liste des sessions audio est périmée. Rouvrez-la.","es":"La lista de sesiones de audio está desactualizada. Ábrela de nuevo.","it":"L’elenco delle sessioni audio non è più aggiornato. Riaprilo.","pt":"A lista de sessões de áudio está desatualizada. Abra-a novamente."},
        "Событие устарело или уже удалено. Обнови список.": {"en":"The event is stale or has already been deleted. Refresh the list.","de":"Der Termin ist veraltet oder bereits gelöscht. Aktualisiere die Liste.","fr":"L’événement est périmé ou a déjà été supprimé. Actualisez la liste.","es":"El evento está desactualizado o ya fue eliminado. Actualiza la lista.","it":"L’evento non è più aggiornato o è già stato eliminato. Aggiorna l’elenco.","pt":"O evento está desatualizado ou já foi excluído. Atualize a lista."},
        "Список аудиоустройств устарел. Обнови его и выбери устройство ещё раз.": {"en":"The audio-device list is stale. Refresh it and select the device again.","de":"Die Liste der Audiogeräte ist veraltet. Aktualisiere sie und wähle das Gerät erneut.","fr":"La liste des périphériques audio est périmée. Actualisez-la et sélectionnez de nouveau le périphérique.","es":"La lista de dispositivos de audio está desactualizada. Actualízala y vuelve a elegir el dispositivo.","it":"L’elenco dei dispositivi audio non è più aggiornato. Aggiornalo e seleziona di nuovo il dispositivo.","pt":"A lista de dispositivos de áudio está desatualizada. Atualize-a e selecione o dispositivo novamente."},
        "Старое подтверждение недействительно": {"en":"Old confirmation is no longer valid","de":"Die alte Bestätigung ist nicht mehr gültig","fr":"L’ancienne confirmation n’est plus valide","es":"La confirmación anterior ya no es válida","it":"La conferma precedente non è più valida","pt":"A confirmação anterior não é mais válida"},
        "Действие отменено.": {"en":"Action cancelled.","de":"Aktion abgebrochen.","fr":"Action annulée.","es":"Acción cancelada.","it":"Azione annullata.","pt":"Ação cancelada."},
        "Ожидающее действие отменено.": {"en":"Pending action cancelled.","de":"Ausstehende Aktion abgebrochen.","fr":"Action en attente annulée.","es":"Acción pendiente cancelada.","it":"Azione in attesa annullata.","pt":"Ação pendente cancelada."},
        "Очередь быстрых команд занята. Подождите завершения текущего действия и повторите команду.": {"en":"The quick-command queue is busy. Wait for the current action to finish and try again.","de":"Die Schnellbefehlswarteschlange ist belegt. Warte auf das Ende der aktuellen Aktion und versuche es erneut.","fr":"La file des commandes rapides est occupée. Attendez la fin de l’action en cours puis réessayez.","es":"La cola de comandos rápidos está ocupada. Espera a que termine la acción actual y vuelve a intentarlo.","it":"La coda dei comandi rapidi è occupata. Attendi il termine dell’azione corrente e riprova.","pt":"A fila de comandos rápidos está ocupada. Aguarde a ação atual terminar e tente novamente."},
        "Очередь команд Telegram заполнена. Дождись завершения текущих запросов и повтори команду.": {"en":"The Telegram command queue is full. Wait for current requests to finish and try again.","de":"Die Telegram-Befehlswarteschlange ist voll. Warte auf das Ende der aktuellen Anfragen und versuche es erneut.","fr":"La file de commandes Telegram est pleine. Attendez la fin des requêtes en cours puis réessayez.","es":"La cola de comandos de Telegram está llena. Espera a que terminen las solicitudes actuales y vuelve a intentarlo.","it":"La coda dei comandi Telegram è piena. Attendi il termine delle richieste in corso e riprova.","pt":"A fila de comandos do Telegram está cheia. Aguarde as solicitações atuais terminarem e tente novamente."},
        "⛔ Этот Telegram-аккаунт не привязан к NEO.": {"en":"⛔ This Telegram account is not paired with NEO.","de":"⛔ Dieses Telegram-Konto ist nicht mit NEO gekoppelt.","fr":"⛔ Ce compte Telegram n’est pas associé à NEO.","es":"⛔ Esta cuenta de Telegram no está vinculada a NEO.","it":"⛔ Questo account Telegram non è associato a NEO.","pt":"⛔ Esta conta do Telegram não está vinculada ao NEO."},
        "Время ввода истекло.": {"en":"Input time expired.","de":"Die Eingabezeit ist abgelaufen.","fr":"Le délai de saisie a expiré.","es":"El tiempo de entrada ha caducado.","it":"Il tempo di inserimento è scaduto.","pt":"O tempo de entrada expirou."},
        "✅ Подтверждено. Очищаю чат и выполняю системное действие…": {"en":"✅ Confirmed. Clearing the chat and executing the system action…","de":"✅ Bestätigt. Chat wird bereinigt und die Systemaktion ausgeführt…","fr":"✅ Confirmé. Nettoyage du chat et exécution de l’action système…","es":"✅ Confirmado. Limpiando el chat y ejecutando la acción del sistema…","it":"✅ Confermato. Pulizia della chat ed esecuzione dell’azione di sistema…","pt":"✅ Confirmado. Limpando o chat e executando a ação do sistema…"},
        "Список Discord-каналов устарел. Выбери сервер заново.": {"en":"The Discord-channel list is stale. Select the server again.","de":"Die Discord-Kanalliste ist veraltet. Wähle den Server erneut.","fr":"La liste des salons Discord est périmée. Sélectionnez de nouveau le serveur.","es":"La lista de canales de Discord está desactualizada. Vuelve a seleccionar el servidor.","it":"L’elenco dei canali Discord non è più aggiornato. Seleziona nuovamente il server.","pt":"A lista de canais do Discord está desatualizada. Selecione o servidor novamente."},
        "Сначала выбери «Переместить окно» и укажи название окна.": {"en":"First choose “Move window” and enter the window name.","de":"Wähle zuerst „Fenster verschieben“ und gib den Fensternamen an.","fr":"Choisissez d’abord « Déplacer la fenêtre » et indiquez le nom de la fenêtre.","es":"Primero elige «Mover ventana» e indica el nombre de la ventana.","it":"Prima scegli “Sposta finestra” e indica il nome della finestra.","pt":"Primeiro escolha “Mover janela” e informe o nome da janela."},
        "ℹ️ Эта команда удалена: официальный Discord API не разрешает NEO автоматизировать обычный пользовательский аккаунт. Музыкальный NeoBot продолжает работать отдельно.": {"en":"ℹ️ This command was removed: the official Discord API does not allow NEO to automate a normal user account. The music NeoBot continues to work separately.","de":"ℹ️ Dieser Befehl wurde entfernt: Die offizielle Discord-API erlaubt NEO nicht, ein normales Benutzerkonto zu automatisieren. Der Musik-NeoBot arbeitet weiterhin separat.","fr":"ℹ️ Cette commande a été supprimée : l’API officielle de Discord n’autorise pas NEO à automatiser un compte utilisateur normal. Le NeoBot musical continue de fonctionner séparément.","es":"ℹ️ Este comando se eliminó: la API oficial de Discord no permite que NEO automatice una cuenta de usuario normal. El NeoBot de música sigue funcionando por separado.","it":"ℹ️ Questo comando è stato rimosso: l’API ufficiale di Discord non consente a NEO di automatizzare un normale account utente. Il NeoBot musicale continua a funzionare separatamente.","pt":"ℹ️ Este comando foi removido: a API oficial do Discord não permite que o NEO automatize uma conta de usuário comum. O NeoBot de música continua funcionando separadamente."},
        "Неизвестный режим ввода.": {"en":"Unknown input mode.","de":"Unbekannter Eingabemodus.","fr":"Mode de saisie inconnu.","es":"Modo de entrada desconocido.","it":"Modalità di input sconosciuta.","pt":"Modo de entrada desconhecido."},
    }

    _TG_HOTFIX106_LABELS: dict[str, dict[str, str]] = {
        "Запустить NEO":{"en":"Start NEO","de":"NEO starten","fr":"Démarrer NEO","es":"Iniciar NEO","it":"Avvia NEO","pt":"Iniciar NEO"},
        "Да, запустить":{"en":"Yes, start","de":"Ja, starten","fr":"Oui, démarrer","es":"Sí, iniciar","it":"Sì, avvia","pt":"Sim, iniciar"},
        "Да, выключить":{"en":"Yes, shut down","de":"Ja, beenden","fr":"Oui, arrêter","es":"Sí, apagar","it":"Sì, spegni","pt":"Sim, desligar"},
        "Подтвердить":{"en":"Confirm","de":"Bestätigen","fr":"Confirmer","es":"Confirmar","it":"Conferma","pt":"Confirmar"},
        "Не удалять":{"en":"Keep","de":"Nicht löschen","fr":"Ne pas supprimer","es":"No eliminar","it":"Non eliminare","pt":"Não excluir"},
        "Не очищать":{"en":"Do not clear","de":"Nicht leeren","fr":"Ne pas effacer","es":"No limpiar","it":"Non cancellare","pt":"Não limpar"},
        "Выключить ПК":{"en":"Shut down PC","de":"PC herunterfahren","fr":"Éteindre le PC","es":"Apagar PC","it":"Spegni PC","pt":"Desligar PC"},
        "Панели": {"en":"Panels","de":"Panels","fr":"Panneaux","es":"Paneles","it":"Pannelli","pt":"Painéis"},
        "Панели NEO": {"en":"NEO panels","de":"NEO-Panels","fr":"Panneaux NEO","es":"Paneles NEO","it":"Pannelli NEO","pt":"Painéis NEO"},
        "Выбери панель для настройки.": {"en":"Choose a panel to configure.","de":"Wähle ein Panel zum Konfigurieren.","fr":"Choisissez un panneau à configurer.","es":"Elige un panel para configurar.","it":"Scegli un pannello da configurare.","pt":"Escolha um painel para configurar."},
        "Игровой оверлей": {"en":"Game overlay","de":"Spiel-Overlay","fr":"Overlay de jeu","es":"Overlay de juego","it":"Overlay di gioco","pt":"Overlay de jogo"},
        "Музыкальный оверлей": {"en":"Music overlay","de":"Musik-Overlay","fr":"Overlay musical","es":"Overlay musical","it":"Overlay musicale","pt":"Overlay de música"},
        "Чек-лист": {"en":"Checklist","de":"Checkliste","fr":"Liste de contrôle","es":"Lista de verificación","it":"Checklist","pt":"Checklist"},
        "Заметки": {"en":"Notes","de":"Notizen","fr":"Notes","es":"Notas","it":"Note","pt":"Notas"},
        "Экран": {"en":"Screen","de":"Bildschirm","fr":"Écran","es":"Pantalla","it":"Schermo","pt":"Tela"},
        "Расположение": {"en":"Position","de":"Position","fr":"Position","es":"Posición","it":"Posizione","pt":"Posição"},
        "Шрифт −2": {"en":"Font −2","de":"Schrift −2","fr":"Police −2","es":"Fuente −2","it":"Carattere −2","pt":"Fonte −2"},
        "Шрифт +2": {"en":"Font +2","de":"Schrift +2","fr":"Police +2","es":"Fuente +2","it":"Carattere +2","pt":"Fonte +2"},
        "Добавить пункт": {"en":"Add item","de":"Punkt hinzufügen","fr":"Ajouter un élément","es":"Añadir elemento","it":"Aggiungi voce","pt":"Adicionar item"},
        "Зачеркнуть пункт": {"en":"Complete item","de":"Punkt abhaken","fr":"Cocher l’élément","es":"Marcar elemento","it":"Spunta voce","pt":"Concluir item"},
        "Удалить пункт": {"en":"Delete item","de":"Punkt löschen","fr":"Supprimer l’élément","es":"Eliminar elemento","it":"Elimina voce","pt":"Excluir item"},
        "Название": {"en":"Title","de":"Titel","fr":"Titre","es":"Título","it":"Titolo","pt":"Título"},
        "Дополнить": {"en":"Append","de":"Ergänzen","fr":"Ajouter","es":"Añadir","it":"Aggiungi","pt":"Acrescentar"},
        "Очистить историю": {"en":"Clear history","de":"Verlauf löschen","fr":"Effacer l’historique","es":"Borrar historial","it":"Cancella cronologia","pt":"Limpar histórico"},
        "Полный снос данных": {"en":"Full data cleanup","de":"Alle Profildaten löschen","fr":"Nettoyage complet des données","es":"Limpieza total de datos","it":"Pulizia completa dei dati","pt":"Limpeza total dos dados"},
        "Запустить NEO": {"en":"Start NEO","de":"NEO starten","fr":"Démarrer NEO","es":"Iniciar NEO","it":"Avvia NEO","pt":"Iniciar NEO"},
        "Выключить NEO": {"en":"Shut down NEO","de":"NEO beenden","fr":"Arrêter NEO","es":"Apagar NEO","it":"Spegni NEO","pt":"Desligar NEO"},
        "Закрыть": {"en":"Close","de":"Schließen","fr":"Fermer","es":"Cerrar","it":"Chiudi","pt":"Fechar"},
        "Не закрывать": {"en":"Keep open","de":"Nicht schließen","fr":"Ne pas fermer","es":"No cerrar","it":"Non chiudere","pt":"Não fechar"},
        "Активные игры": {"en":"Active games","de":"Aktive Spiele","fr":"Jeux actifs","es":"Juegos activos","it":"Giochi attivi","pt":"Jogos ativos"},
        "Выбери экран для панели.": {"en":"Choose a screen for the panel.","de":"Wähle einen Bildschirm für das Panel.","fr":"Choisissez un écran pour le panneau.","es":"Elige una pantalla para el panel.","it":"Scegli uno schermo per il pannello.","pt":"Escolha uma tela para o painel."},
        "Выбери положение панели на экране.": {"en":"Choose the panel position on screen.","de":"Wähle die Panelposition auf dem Bildschirm.","fr":"Choisissez la position du panneau à l’écran.","es":"Elige la posición del panel en pantalla.","it":"Scegli la posizione del pannello sullo schermo.","pt":"Escolha a posição do painel na tela."},
        "Полный снос локальных данных браузера": {"en":"Full browser local-data cleanup","de":"Lokale Browserdaten vollständig löschen","fr":"Suppression complète des données locales du navigateur","es":"Eliminación total de datos locales del navegador","it":"Pulizia completa dei dati locali del browser","pt":"Limpeza completa dos dados locais do navegador"},
        "Удаляются профили, cookies, пароли, история и локальные настройки. Сам браузер останется установлен. Потребуется кодовая фраза.": {"en":"Profiles, cookies, passwords, history and local settings will be deleted. The browser itself stays installed. A passphrase is required.","de":"Profile, Cookies, Passwörter, Verlauf und lokale Einstellungen werden gelöscht. Der Browser bleibt installiert. Eine Codephrase ist erforderlich.","fr":"Les profils, cookies, mots de passe, historique et réglages locaux seront supprimés. Le navigateur reste installé. Une phrase secrète est requise.","es":"Se eliminarán perfiles, cookies, contraseñas, historial y ajustes locales. El navegador seguirá instalado. Se requiere una frase de seguridad.","it":"Verranno eliminati profili, cookie, password, cronologia e impostazioni locali. Il browser resterà installato. È richiesta una frase di sicurezza.","pt":"Perfis, cookies, senhas, histórico e configurações locais serão excluídos. O navegador continuará instalado. É necessária uma frase de segurança."},
        "Очистить историю браузера": {"en":"Clear browser history","de":"Browserverlauf löschen","fr":"Effacer l’historique du navigateur","es":"Borrar el historial del navegador","it":"Cancella la cronologia del browser","pt":"Limpar o histórico do navegador"},
        "Выбери браузер. Потребуется кодовая фраза. Браузер должен быть полностью закрыт.": {"en":"Choose a browser. A passphrase is required. The browser must be fully closed.","de":"Wähle einen Browser. Eine Codephrase ist erforderlich. Der Browser muss vollständig geschlossen sein.","fr":"Choisissez un navigateur. Une phrase secrète est requise. Le navigateur doit être complètement fermé.","es":"Elige un navegador. Se requiere una frase de seguridad. El navegador debe estar completamente cerrado.","it":"Scegli un browser. È richiesta una frase di sicurezza. Il browser deve essere completamente chiuso.","pt":"Escolha um navegador. É necessária uma frase de segurança. O navegador deve estar totalmente fechado."},
        "Поддерживаемые локальные профили не найдены.": {"en":"No supported local browser profiles were found.","de":"Keine unterstützten lokalen Browserprofile gefunden.","fr":"Aucun profil local de navigateur pris en charge n’a été trouvé.","es":"No se encontraron perfiles locales compatibles.","it":"Nessun profilo locale supportato trovato.","pt":"Nenhum perfil local compatível foi encontrado."},
        "Выбери цель. Перед закрытием NEO покажет отдельное подтверждение.": {"en":"Choose a target. NEO will ask for confirmation before closing it.","de":"Wähle ein Ziel. NEO fragt vor dem Schließen noch einmal nach.","fr":"Choisissez une cible. NEO demandera une confirmation avant de la fermer.","es":"Elige un objetivo. NEO pedirá confirmación antes de cerrarlo.","it":"Scegli un elemento. NEO chiederà conferma prima di chiuderlo.","pt":"Escolha um alvo. O NEO pedirá confirmação antes de fechá-lo."},
        "Напиши название приложения или процесса.": {"en":"Enter the application or process name.","de":"Gib den Namen der Anwendung oder des Prozesses ein.","fr":"Saisissez le nom de l’application ou du processus.","es":"Escribe el nombre de la aplicación o del proceso.","it":"Inserisci il nome dell’applicazione o del processo.","pt":"Digite o nome do aplicativo ou processo."},
        "Введи новый пункт чек-листа.": {"en":"Enter a new checklist item.","de":"Gib einen neuen Checklistenpunkt ein.","fr":"Saisissez un nouvel élément de la liste.","es":"Introduce un nuevo elemento de la lista.","it":"Inserisci una nuova voce della checklist.","pt":"Digite um novo item da checklist."},
        "Введи пункт, который нужно зачеркнуть.": {"en":"Enter the checklist item to complete.","de":"Gib den Checklistenpunkt ein, der abgehakt werden soll.","fr":"Saisissez l’élément à cocher.","es":"Introduce el elemento que quieres marcar.","it":"Inserisci la voce da spuntare.","pt":"Digite o item que deve ser concluído."},
        "Введи пункт, который нужно удалить.": {"en":"Enter the checklist item to delete.","de":"Gib den Checklistenpunkt ein, der gelöscht werden soll.","fr":"Saisissez l’élément à supprimer.","es":"Introduce el elemento que quieres eliminar.","it":"Inserisci la voce da eliminare.","pt":"Digite o item que deve ser excluído."},
        "Выбери пункт, который нужно зачеркнуть.": {"en":"Choose the checklist item to complete.","de":"Wähle den Checklistenpunkt zum Abhaken.","fr":"Choisissez l’élément à cocher.","es":"Elige el elemento que quieres marcar.","it":"Scegli la voce da spuntare.","pt":"Escolha o item a concluir."},
        "Выбери пункт, который нужно удалить.": {"en":"Choose the checklist item to delete.","de":"Wähle den Checklistenpunkt zum Löschen.","fr":"Choisissez l’élément à supprimer.","es":"Elige el elemento que quieres eliminar.","it":"Scegli la voce da eliminare.","pt":"Escolha o item a excluir."},
        "Нет незачеркнутых пунктов чек-листа.": {"en":"There are no unchecked checklist items.","de":"Es gibt keine offenen Checklistenpunkte.","fr":"Il n’y a aucun élément non coché.","es":"No hay elementos sin marcar.","it":"Non ci sono voci non spuntate.","pt":"Não há itens não concluídos."},
        "В чек-листе нет пунктов.": {"en":"The checklist has no items.","de":"Die Checkliste enthält keine Punkte.","fr":"La liste ne contient aucun élément.","es":"La lista no tiene elementos.","it":"La checklist non contiene voci.","pt":"A checklist não tem itens."},
        "Введи новый текст заметок.": {"en":"Enter the new notes text.","de":"Gib den neuen Notiztext ein.","fr":"Saisissez le nouveau texte des notes.","es":"Introduce el nuevo texto de las notas.","it":"Inserisci il nuovo testo delle note.","pt":"Digite o novo texto das notas."},
        "Введи текст, который нужно дописать к заметкам.": {"en":"Enter the text to append to the notes.","de":"Gib den Text ein, der an die Notizen angehängt werden soll.","fr":"Saisissez le texte à ajouter aux notes.","es":"Introduce el texto que quieres añadir a las notas.","it":"Inserisci il testo da aggiungere alle note.","pt":"Digite o texto que deve ser acrescentado às notas."},
        "Введи новое название панели.": {"en":"Enter the new panel title.","de":"Gib den neuen Paneltitel ein.","fr":"Saisissez le nouveau titre du panneau.","es":"Introduce el nuevo título del panel.","it":"Inserisci il nuovo titolo del pannello.","pt":"Digite o novo título do painel."},
        "Запустить NEO?": {"en":"Start NEO?","de":"NEO starten?","fr":"Démarrer NEO ?","es":"¿Iniciar NEO?","it":"Avviare NEO?","pt":"Iniciar o NEO?"},
        "Выключить NEO?": {"en":"Shut down NEO?","de":"NEO beenden?","fr":"Arrêter NEO ?","es":"¿Apagar NEO?","it":"Spegnere NEO?","pt":"Desligar o NEO?"},
        "NEO уже запущен.": {"en":"NEO is already running.","de":"NEO läuft bereits.","fr":"NEO est déjà actif.","es":"NEO ya está en ejecución.","it":"NEO è già in esecuzione.","pt":"O NEO já está em execução."},
        "Подтвердить действие?": {"en":"Confirm this action?","de":"Aktion bestätigen?","fr":"Confirmer cette action ?","es":"¿Confirmar esta acción?","it":"Confermare questa azione?","pt":"Confirmar esta ação?"},
        "Удалить пункт чек-листа?": {"en":"Delete this checklist item?","de":"Diesen Checklistenpunkt löschen?","fr":"Supprimer cet élément de la liste ?","es":"¿Eliminar este elemento de la lista?","it":"Eliminare questa voce della checklist?","pt":"Excluir este item da checklist?"},
        "Очистить заметки?": {"en":"Clear the notes?","de":"Notizen leeren?","fr":"Effacer les notes ?","es":"¿Vaciar las notas?","it":"Cancellare le note?","pt":"Limpar as notas?"},
        "Продолжить очистку браузера?": {"en":"Continue with browser cleanup?","de":"Browser-Bereinigung fortsetzen?","fr":"Poursuivre le nettoyage du navigateur ?","es":"¿Continuar con la limpieza del navegador?","it":"Continuare con la pulizia del browser?","pt":"Continuar com a limpeza do navegador?"},
        "Подтвердить действие питания Windows?": {"en":"Confirm the Windows power action?","de":"Windows-Energieaktion bestätigen?","fr":"Confirmer l’action d’alimentation Windows ?","es":"¿Confirmar la acción de energía de Windows?","it":"Confermare l’azione di alimentazione Windows?","pt":"Confirmar a ação de energia do Windows?"},
        "Выбери панель, внешний вид которой нужно настроить.": {"en":"Choose the panel whose appearance you want to configure.","de":"Wähle das Panel, dessen Darstellung du konfigurieren möchtest.","fr":"Choisissez le panneau dont vous voulez régler l’apparence.","es":"Elige el panel cuya apariencia quieres configurar.","it":"Scegli il pannello di cui vuoi configurare l’aspetto.","pt":"Escolha o painel cuja aparência você deseja configurar."},
        "Выбери визуальный пресет.": {"en":"Choose a visual preset.","de":"Wähle ein visuelles Preset.","fr":"Choisissez un préréglage visuel.","es":"Elige un preajuste visual.","it":"Scegli un preset visivo.","pt":"Escolha uma predefinição visual."},
        "Выбери семейство шрифта.": {"en":"Choose a font family.","de":"Wähle eine Schriftfamilie.","fr":"Choisissez une famille de polices.","es":"Elige una familia de fuentes.","it":"Scegli una famiglia di caratteri.","pt":"Escolha uma família de fontes."},
        "Выбери размер шрифта.": {"en":"Choose a font size.","de":"Wähle eine Schriftgröße.","fr":"Choisissez une taille de police.","es":"Elige un tamaño de fuente.","it":"Scegli una dimensione del carattere.","pt":"Escolha um tamanho de fonte."},
        "Выбери прозрачность фона. 0% — фон прозрачный, 100% — полностью плотный.": {"en":"Choose background transparency. 0% is transparent; 100% is fully opaque.","de":"Wähle die Hintergrundtransparenz. 0 % ist transparent, 100 % vollständig deckend.","fr":"Choisissez la transparence du fond. 0 % = transparent ; 100 % = totalement opaque.","es":"Elige la transparencia del fondo. 0 % es transparente y 100 % totalmente opaco.","it":"Scegli la trasparenza dello sfondo. 0% è trasparente, 100% completamente opaco.","pt":"Escolha a transparência do fundo. 0% é transparente e 100% totalmente opaco."},
        "Выбери диск.": {"en":"Choose a drive.","de":"Wähle ein Laufwerk.","fr":"Choisissez un lecteur.","es":"Elige una unidad.","it":"Scegli un’unità.","pt":"Escolha uma unidade."},
        "На этом уровне нет доступных папок.": {"en":"There are no accessible folders at this level.","de":"Auf dieser Ebene gibt es keine zugänglichen Ordner.","fr":"Aucun dossier accessible à ce niveau.","es":"No hay carpetas accesibles en este nivel.","it":"Non ci sono cartelle accessibili a questo livello.","pt":"Não há pastas acessíveis neste nível."},
        "Сеанс Проводника завершён. Войди снова через кодовую фразу.": {"en":"The Explorer session ended. Enter it again using the passphrase.","de":"Die Explorer-Sitzung ist beendet. Öffne sie erneut mit der Codephrase.","fr":"La session de l’Explorateur est terminée. Entrez à nouveau avec la phrase secrète.","es":"La sesión del Explorador terminó. Entra de nuevo con la frase de seguridad.","it":"La sessione Esplora file è terminata. Rientra usando la frase di sicurezza.","pt":"A sessão do Explorador terminou. Entre novamente usando a frase de segurança."},
        "Не удалось открыть папку.": {"en":"Could not open the folder.","de":"Der Ordner konnte nicht geöffnet werden.","fr":"Impossible d’ouvrir le dossier.","es":"No se pudo abrir la carpeta.","it":"Impossibile aprire la cartella.","pt":"Não foi possível abrir a pasta."},
    }

    def _tg_message(self, text: str) -> str:
        source = str(text or "")
        code = language_code(self.config)
        if code == "ru" or not source:
            return source
        direct = self._TG_MESSAGES.get(source, {}).get(code) or self._TG_HOTFIX106_LABELS.get(source, {}).get(code)
        if direct:
            return direct
        patterns = {
            "en": ((r"^Команда слишком длинная\. Максимум: (\d+) символов\.$", "Command is too long. Maximum: {0} characters."),(r"^🔒 Проверка временно заблокирована\. Повтори через (\d+) сек\.$", "🔒 Verification is temporarily locked. Try again in {0} s."),(r"^❌ Неверная кодовая фраза\. Осталось попыток: (\d+)\.$", "❌ Incorrect passphrase. Attempts remaining: {0}."),(r"^⛔ Кодовая фраза неверна\. Действие отменено, новые попытки заблокированы на (\d+) сек\.$", "⛔ Incorrect passphrase. Action cancelled; new attempts are locked for {0} s."),(r"^Telegram user ID: (.+)\nChat ID: (.+)\nТип чата: (.+)$", "Telegram user ID: {0}\nChat ID: {1}\nChat type: {2}"),(r"^✅ Нажал (.+) кнопкой мыши на X=(\d+), Y=(\d+) \(экран (.+)\)\.$", "✅ Clicked {0} with the mouse at X={1}, Y={2} (screen {3}).")),
            "de": ((r"^Команда слишком длинная\. Максимум: (\d+) символов\.$", "Befehl ist zu lang. Maximum: {0} Zeichen."),(r"^🔒 Проверка временно заблокирована\. Повтори через (\d+) сек\.$", "🔒 Prüfung vorübergehend gesperrt. In {0} s erneut versuchen."),(r"^❌ Неверная кодовая фраза\. Осталось попыток: (\d+)\.$", "❌ Falsche Codephrase. Verbleibende Versuche: {0}."),(r"^⛔ Кодовая фраза неверна\. Действие отменено, новые попытки заблокированы на (\d+) сек\.$", "⛔ Falsche Codephrase. Aktion abgebrochen; neue Versuche sind für {0} s gesperrt."),(r"^Telegram user ID: (.+)\nChat ID: (.+)\nТип чата: (.+)$", "Telegram user ID: {0}\nChat ID: {1}\nChat-Typ: {2}"),(r"^✅ Нажал (.+) кнопкой мыши на X=(\d+), Y=(\d+) \(экран (.+)\)\.$", "✅ {0} mit der Maus bei X={1}, Y={2} angeklickt (Bildschirm {3}).")),
            "fr": ((r"^Команда слишком длинная\. Максимум: (\d+) символов\.$", "Commande trop longue. Maximum : {0} caractères."),(r"^🔒 Проверка временно заблокирована\. Повтори через (\d+) сек\.$", "🔒 Vérification temporairement verrouillée. Réessayez dans {0} s."),(r"^❌ Неверная кодовая фраза\. Осталось попыток: (\d+)\.$", "❌ Phrase secrète incorrecte. Tentatives restantes : {0}."),(r"^⛔ Кодовая фраза неверна\. Действие отменено, новые попытки заблокированы на (\d+) сек\.$", "⛔ Phrase secrète incorrecte. Action annulée ; nouvelles tentatives bloquées pendant {0} s."),(r"^Telegram user ID: (.+)\nChat ID: (.+)\nТип чата: (.+)$", "Telegram user ID: {0}\nChat ID: {1}\nType de chat : {2}"),(r"^✅ Нажал (.+) кнопкой мыши на X=(\d+), Y=(\d+) \(экран (.+)\)\.$", "✅ Clic souris {0} à X={1}, Y={2} (écran {3}).")),
            "es": ((r"^Команда слишком длинная\. Максимум: (\d+) символов\.$", "El comando es demasiado largo. Máximo: {0} caracteres."),(r"^🔒 Проверка временно заблокирована\. Повтори через (\d+) сек\.$", "🔒 Verificación bloqueada temporalmente. Inténtalo de nuevo en {0} s."),(r"^❌ Неверная кодовая фраза\. Осталось попыток: (\d+)\.$", "❌ Frase de seguridad incorrecta. Intentos restantes: {0}."),(r"^⛔ Кодовая фраза неверна\. Действие отменено, новые попытки заблокированы на (\d+) сек\.$", "⛔ Frase de seguridad incorrecta. Acción cancelada; nuevos intentos bloqueados durante {0} s."),(r"^Telegram user ID: (.+)\nChat ID: (.+)\nТип чата: (.+)$", "Telegram user ID: {0}\nChat ID: {1}\nTipo de chat: {2}"),(r"^✅ Нажал (.+) кнопкой мыши на X=(\d+), Y=(\d+) \(экран (.+)\)\.$", "✅ Clic {0} con el ratón en X={1}, Y={2} (pantalla {3}).")),
            "it": ((r"^Команда слишком длинная\. Максимум: (\d+) символов\.$", "Il comando è troppo lungo. Massimo: {0} caratteri."),(r"^🔒 Проверка временно заблокирована\. Повтори через (\d+) сек\.$", "🔒 Verifica temporaneamente bloccata. Riprova tra {0} s."),(r"^❌ Неверная кодовая фраза\. Осталось попыток: (\d+)\.$", "❌ Frase di sicurezza errata. Tentativi rimasti: {0}."),(r"^⛔ Кодовая фраза неверна\. Действие отменено, новые попытки заблокированы на (\d+) сек\.$", "⛔ Frase di sicurezza errata. Azione annullata; nuovi tentativi bloccati per {0} s."),(r"^Telegram user ID: (.+)\nChat ID: (.+)\nТип чата: (.+)$", "Telegram user ID: {0}\nChat ID: {1}\nTipo di chat: {2}"),(r"^✅ Нажал (.+) кнопкой мыши на X=(\d+), Y=(\d+) \(экран (.+)\)\.$", "✅ Clic {0} con il mouse a X={1}, Y={2} (schermo {3}).")),
            "pt": ((r"^Команда слишком длинная\. Максимум: (\d+) символов\.$", "O comando é longo demais. Máximo: {0} caracteres."),(r"^🔒 Проверка временно заблокирована\. Повтори через (\d+) сек\.$", "🔒 Verificação temporariamente bloqueada. Tente novamente em {0} s."),(r"^❌ Неверная кодовая фраза\. Осталось попыток: (\d+)\.$", "❌ Frase de segurança incorreta. Tentativas restantes: {0}."),(r"^⛔ Кодовая фраза неверна\. Действие отменено, новые попытки заблокированы на (\d+) сек\.$", "⛔ Frase de segurança incorreta. Ação cancelada; novas tentativas bloqueadas por {0} s."),(r"^Telegram user ID: (.+)\nChat ID: (.+)\nТип чата: (.+)$", "Telegram user ID: {0}\nChat ID: {1}\nTipo de chat: {2}"),(r"^✅ Нажал (.+) кнопкой мыши на X=(\d+), Y=(\d+) \(экран (.+)\)\.$", "✅ Clique {0} do mouse em X={1}, Y={2} (tela {3}).")),
        }
        for pat, repl in patterns.get(code, ()):
            m = re.match(pat, source, flags=re.I | re.S)
            if m:
                return repl.format(*m.groups())
        return self._tg_runtime_text(source)

    def _tg_text(self, text: str) -> str:
        code = language_code(self.config)
        source = str(text or "")
        if code == "ru" or not source:
            return source
        # Translate line by line while preserving icons, percentages, device names
        # and user-authored entity payloads.
        out_lines: list[str] = []
        for line in source.splitlines():
            raw = line.strip()
            if not raw:
                out_lines.append(""); continue
            prefix = ""
            body = raw
            m = re.match(r"^([^A-Za-zА-Яа-яЁё0-9]*)(.*)$", raw)
            if m:
                prefix, body = m.group(1), m.group(2).strip()
            # Handle numeric seek captions and screen labels first.
            if body.startswith("Экран "):
                word = {"en":"Screen","de":"Bildschirm","fr":"Écran","es":"Pantalla","it":"Schermo","pt":"Tela"}[code]
                out_lines.append(prefix + word + body[len("Экран"):]); continue
            translated = self._TG_LABELS.get(body, {}).get(code) or self._TG_HOTFIX106_LABELS.get(body, {}).get(code)
            if translated:
                out_lines.append(prefix + translated); continue
            status_match = re.match(r"^Состояние: (включена|выключена) · экран (\d+) · ([a-z_]+) · шрифт (\d+)$", body, flags=re.I)
            if status_match:
                on = status_match.group(1).casefold() == "включена"
                screen_no, pos, size = status_match.group(2), status_match.group(3), status_match.group(4)
                words = {
                    "en":("State", "on" if on else "off", "screen", "font"),
                    "de":("Status", "an" if on else "aus", "Bildschirm", "Schrift"),
                    "fr":("État", "activé" if on else "désactivé", "écran", "police"),
                    "es":("Estado", "activado" if on else "desactivado", "pantalla", "fuente"),
                    "it":("Stato", "attivo" if on else "disattivo", "schermo", "carattere"),
                    "pt":("Estado", "ativado" if on else "desativado", "tela", "fonte"),
                }[code]
                out_lines.append(prefix + f"{words[0]}: {words[1]} · {words[2]} {screen_no} · {pos} · {words[3]} {size}"); continue
            close_match = re.match(r"^Закрыть (игру|приложение) «(.+)»\?$", body, flags=re.I)
            if close_match:
                kind, entity = close_match.group(1).casefold(), close_match.group(2)
                template = {
                    "en":"Close {kind} «{entity}»?", "de":"{kind} «{entity}» schließen?", "fr":"Fermer {kind} «{entity}» ?",
                    "es":"¿Cerrar {kind} «{entity}»?", "it":"Chiudere {kind} «{entity}»?", "pt":"Fechar {kind} «{entity}»?",
                }[code]
                kword = {
                    "en":"game" if kind=="игру" else "app", "de":"Spiel" if kind=="игру" else "App", "fr":"le jeu" if kind=="игру" else "l’application",
                    "es":"el juego" if kind=="игру" else "la aplicación", "it":"il gioco" if kind=="игру" else "l’app", "pt":"o jogo" if kind=="игру" else "o aplicativo",
                }[code]
                out_lines.append(prefix + template.format(kind=kword, entity=entity)); continue
            # Common suffix/prefix captions.
            replacements = {
                "ru":(),
                "en":(("Назад","Back"),("Домой","Home"),("Текущая","Current"),("Текущий","Current"),("Обновить","Refresh"),("Включить","Enable"),("Выключить","Disable")),
                "de":(("Назад","Zurück"),("Домой","Start"),("Текущая","Aktuell"),("Текущий","Aktuell"),("Обновить","Aktualisieren"),("Включить","Aktivieren"),("Выключить","Deaktivieren")),
                "fr":(("Назад","Retour"),("Домой","Accueil"),("Текущая","Actuelle"),("Текущий","Actuel"),("Обновить","Actualiser"),("Включить","Activer"),("Выключить","Désactiver")),
                "es":(("Назад","Atrás"),("Домой","Inicio"),("Текущая","Actual"),("Текущий","Actual"),("Обновить","Actualizar"),("Включить","Activar"),("Выключить","Desactivar")),
                "it":(("Назад","Indietro"),("Домой","Home"),("Текущая","Attuale"),("Текущий","Attuale"),("Обновить","Aggiorna"),("Включить","Attiva"),("Выключить","Disattiva")),
                "pt":(("Назад","Voltar"),("Домой","Início"),("Текущая","Atual"),("Текущий","Atual"),("Обновить","Atualizar"),("Включить","Ativar"),("Выключить","Desativar")),
            }
            candidate = body
            for a,b in replacements.get(code,()): candidate = candidate.replace(a,b)
            if re.search(r"[А-Яа-яЁё]", candidate):
                # Do not leak Russian Telegram chrome. Tool/user payload is not sent
                # through this helper, so a neutral localized fallback is safe here.
                candidate = self._tg_runtime_text(body)
            out_lines.append(prefix + candidate)
        return "\n".join(out_lines)

    def _tg_runtime_text(self, text: str) -> str:
        """Localize deterministic tool/error text with the same gate as desktop."""
        return localize_runtime_text(str(text or ""), language_code(self.config))

    # --------------------------------------------------------------- UI builders
    def _kb(self, rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": self._tg_text(text), "callback_data": data} for text, data in row]
                for row in rows
            ]
        }

    def _home_keyboard(self) -> dict[str, Any]:
        return self._kb([
            [("🎵 Музыка", "ui:mm"), ("🎧 Discord", "ui:md")],
            [("🧩 Приложения", "ui:apps"), ("📁 Проводник", "ui:explorer"), ("🖥 Система", "ui:sys")],
            [("🌐 Браузер", "ui:br"), ("▶️ YouTube", "ui:yt"), ("🔎 Поиск информации", "i:web:q")],
            [("🎙 Ввод", "ui:mic"), ("🖱 Мышь", "ui:mouse"), ("🎧 Вывод", "ui:vol")],
            [("ℹ️ Сведения ПК", "a:info:sys"), ("📋 Процессы", "a:info:proc")],
            [("📸 Скриншот", "ui:shot"), ("🧱 Панели", "ui:panels")],
            [("📡 Сеть", "ui:net"), ("🔵 Bluetooth", "ui:bt")],
            [("📧 Почта", "ui:mail"), ("📰 Сводка дня", "ui:brief"), ("📅 Расписание", "ui:cal")],
            [("🌦 Погода", "ui:weather"), ("🎬 Сценарии", "ui:sc:0")],
            [("⌨️ Нажать клавишу", "ui:key"), ("🖱 Нажать мышью", "ui:rmouse")],
            [("🧹 Очистить историю", "a:tg:clearhistory"), ("⚙️ Настройки", "ui:settings"), ("⏱ Таймер", "ui:timer")],
            [("⏻ Выключить NEO", "a:jarvis:off")],
        ])

    def _panel(self, chat_id: str, message_id: int, title: str, rows: list[list[tuple[str, str]]], *, view: str = "home") -> int:
        chat_key = str(chat_id)
        with self._panel_lock(chat_key):
            title = self._tg_text(title)
            keyboard = self._kb(rows)
            tracked = int(self._panel_by_chat.get(chat_key, 0) or 0)
            requested = int(message_id or 0)

            # A callback from a stale menu may arrive after a delayed result. Delete
            # the currently tracked keyboard first, then reuse the clicked message
            # if it still exists. At no point do we intentionally publish a second
            # keyboard while another tracked one is alive.
            if requested > 0 and tracked > 0 and requested != tracked:
                # The user clicked a stale duplicate from an older race/build.
                # Delete *both* known keyboards and publish one fresh panel at the
                # bottom instead of reviving the stale message higher in history.
                self._delete_one(chat_key, tracked)
                self._delete_one(chat_key, requested)
                tracked = 0
                requested = 0
            target = requested or tracked
            if target and self._edit_panel(chat_key, target, title, keyboard):
                return self._remember_panel(chat_key, target, view)
            if target > 0:
                self._delete_one(chat_key, target)
            ids = self._send_message(chat_key, title, reply_markup=keyboard)
            return self._remember_panel(chat_key, ids[-1] if ids else 0, view)

    def _show_home(self, chat_id: str, message_id: int = 0) -> int:
        chat_key = str(chat_id)
        with self._panel_lock(chat_key):
            title = self._tg_text("NEO Remote")
            tracked = int(self._panel_by_chat.get(chat_key, 0) or 0)
            requested = int(message_id or 0)
            if requested > 0 and tracked > 0 and requested != tracked:
                # The user clicked a stale duplicate from an older race/build.
                # Delete *both* known keyboards and publish one fresh panel at the
                # bottom instead of reviving the stale message higher in history.
                self._delete_one(chat_key, tracked)
                self._delete_one(chat_key, requested)
                tracked = 0
                requested = 0
            target = requested or tracked
            if target and self._edit_panel(chat_key, target, title, self._home_keyboard()):
                return self._remember_panel(chat_key, target, "home")
            if target > 0:
                self._delete_one(chat_key, target)
            ids = self._send_message(chat_key, title, reply_markup=self._home_keyboard())
            return self._remember_panel(chat_key, ids[-1] if ids else 0, "home")

    def _show_view(self, chat_id: str, view: str, message_id: int = 0) -> None:
        value = str(view or "home")
        if value.startswith("ncfg:"):
            parts = value.split(":")
            if len(parts) == 3 and parts[2].isdigit():
                self._show_remote_settings(chat_id, message_id, parts[1], int(parts[2]))
            return
        if value == "settings": self._show_settings_menu(chat_id, message_id); return
        if value == "brief": self._show_daily_briefing_menu(chat_id, message_id); return
        if value == "timer": self._show_timer_menu(chat_id, message_id); return
        if value == "screen": self._show_screen_menu(chat_id, message_id); return
        if value == "win": self._show_windows_menu(chat_id, message_id); return
        if value == "shot": self._show_screenshot_menu(chat_id, message_id); return
        if value == "info": self._show_system_menu(chat_id, message_id); return  # legacy HOTFIX60/61 panel
        if value == "weather": self._show_weather_menu(chat_id, message_id); return
        if value == "proc": self._show_process_menu(chat_id, message_id); return
        if value == "med": self._show_media_menu(chat_id, message_id); return
        if value == "md": self._show_discord_media(chat_id, message_id); return
        if value == "mc": self._show_computer_media(chat_id, message_id); return
        if value == "mm": self._show_music_menu(chat_id, message_id); return
        if value in {"mv", "yt"}: self._show_youtube_menu(chat_id, message_id); return
        if value == "ytq": self._show_youtube_quality_menu(chat_id, message_id); return
        if value == "yts": self._show_youtube_speed_menu(chat_id, message_id); return
        if value == "br": self._show_browser_menu(chat_id, message_id); return
        if value == "panels": self._show_panels_menu(chat_id, message_id); return
        if value == "ovsettings": self._show_overlay_settings_hub(chat_id, message_id); return
        if value.startswith("ovcfg:"):
            self._show_overlay_style_menu(chat_id, message_id, value.split(":", 1)[1]); return
        if value == "explorer":
            state = self._explorer_by_chat.get(str(chat_id), {})
            if bool(state.get("authorized", False)):
                if str(state.get("current_path", "") or ""):
                    self._show_explorer_folder(chat_id, message_id, page=int(state.get("page", 0) or 0)); return
                self._show_explorer_drives(chat_id, message_id); return
            self._show_home(chat_id, message_id); return
        if value.startswith("ov:"):
            self._show_overlay_menu(chat_id, message_id, value.split(":", 1)[1]); return
        if value == "apps": self._show_apps_menu(chat_id, message_id); return
        if value.startswith("appvol:"):
            try: page = int(value.split(":", 1)[1])
            except Exception: page = 0
            self._show_app_volume_menu(chat_id, message_id, page=page); return
        if value == "sys": self._show_system_menu(chat_id, message_id); return
        if value == "net": self._show_network_menu(chat_id, message_id); return
        if value == "bt": self._show_bluetooth_menu(chat_id, message_id); return
        if value == "btpair": self._show_bluetooth_pair_menu(chat_id, message_id); return
        if value == "key": self._show_key_menu(chat_id, message_id); return
        if value == "bright": self._show_brightness_menu(chat_id, message_id); return
        if value == "power": self._show_power_menu(chat_id, message_id); return
        if value == "vol": self._show_volume_menu(chat_id, message_id); return
        if value == "mouse": self._show_mouse_menu(chat_id, message_id); return
        if value == "mic": self._show_microphone_menu(chat_id, message_id); return
        if value == "mail": self._show_mail_menu(chat_id, message_id); return
        if value.startswith("mail:"): self._show_mail_provider_menu(chat_id, message_id, value.split(":",1)[1]); return
        if value == "rmouse": self._show_remote_mouse_menu(chat_id, message_id); return
        if value == "cal": self._show_calendar_menu(chat_id, message_id); return
        if value.startswith("micdev:"):
            try: page = int(value.split(":", 1)[1])
            except Exception: page = 0
            self._show_audio_device_menu(chat_id, message_id, device_type="input", page=page); return
        if value.startswith("outdev:"):
            try: page = int(value.split(":", 1)[1])
            except Exception: page = 0
            self._show_audio_device_menu(chat_id, message_id, device_type="output", page=page); return
        if value.startswith("sc:"):
            try: page = int(value.split(":", 1)[1])
            except Exception: page = 0
            self._show_scenarios_menu(chat_id, message_id, page=page); return
        self._show_home(chat_id, message_id)

    def _show_telegram_messages_menu(self, chat_id: str, message_id: int) -> None:
        status = self._message_center.connection_status()
        st = self._message_center.stats()
        title = f"📨 Telegram Messages\nSecretary: {'подключён' if status.get('connected') else 'не подключён'} · сообщений учтено: {st.get('total', 0)}"
        self._panel(chat_id, message_id, title, [
            [("📊 Статистика", "a:tgmsg:status"), ("🕐 За 24 часа", "a:tgmsg:24")],
            [("💬 Последние", "a:tgmsg:recent")],
            [("⬅️ Назад", "ui:h")],
        ], view="tgmsg")

    def _show_mail_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "📧 Почта", [
            [("Gmail", "ui:mail:gmail"), ("Mail.ru", "ui:mail:mailru")],
            [("📊 Обе почты", "a:mail:all:status")],
            [("⬅️ Назад", "ui:h")],
        ], view="mail")

    def _show_mail_provider_menu(self, chat_id: str, message_id: int, provider: str) -> None:
        provider = "gmail" if str(provider).casefold() == "gmail" else "mailru"
        label = "Gmail" if provider == "gmail" else "Mail.ru"
        self._panel(chat_id, message_id, f"📧 {label}", [
            [("📊 Информация", f"a:mail:{provider}:status"), ("🕐 Последние письма", f"a:mail:{provider}:latest")],
            [("✉️ Отправить письмо", f"i:mail:{provider}:send")],
            [("⬅️ Назад", "ui:mail"), ("🏠 Домой", "ui:h")],
        ], view=f"mail:{provider}")

    def _show_calendar_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "📅 Google Calendar", [
            [("Сегодня", "a:cal:today"), ("Завтра", "a:cal:tomorrow")],
            [("7 дней", "a:cal:week"), ("📋 Всё расписание", "a:cal:all")],
            [("➕ Добавить", "i:cal:add")],
            [("✏️ Редактировать", "ui:caledit:0"), ("🗑 Удалить", "ui:caldel:0")],
            [("⬅️ Назад", "ui:h")],
        ], view="cal")

    def _show_remote_mouse_menu(self, chat_id: str, message_id: int) -> None:
        try:
            monitors = monitor_inventory()
            button = str(self._remote_mouse_button_by_chat.get(str(chat_id), "left") or "left")
            if button not in {"left", "right"}:
                button = "left"
            rows: list[list[tuple[str, str]]] = [[
                (("🖱 Левая ✓" if button == "left" else "🖱 Левая"), "a:rmouse:setbtn:left"),
                (("🖱 Правая ✓" if button == "right" else "🖱 Правая"), "a:rmouse:setbtn:right"),
            ]]
            for row in monitors:
                idx = int(row["index"])
                left, top = int(row["left"]), int(row["top"])
                right = left + int(row["width"]) - 1
                bottom = top + int(row["height"]) - 1
                rows.append([(
                    f"🖥 Экран {idx} · {row['width']}×{row['height']} · X {left}…{right} · Y {top}…{bottom}",
                    f"a:rmouse:mon:{idx}",
                )])
            rows.append([("⬅️ Назад", "ui:h")])
            self._panel(
                chat_id, message_id,
                "🖱 Удалённый клик мышью\n"
                "Выбери кнопку (по умолчанию левая), затем экран. После скриншота сразу введи X, затем Y. "
                "Координаты на сетке глобальные и могут быть отрицательными.",
                rows, view="rmouse",
            )
        except Exception as exc:
            self._panel(chat_id, message_id, f"🖱 Управление мышью недоступно: {exc}", [[("⬅️ Назад", "ui:h")]], view="rmouse")

    def _calendar_rows(self) -> list[dict[str, Any]]:
        client = GoogleCalendarClient(self.config)
        return client.upcoming(days=365, limit=100)

    def _show_calendar_event_picker(self, chat_id: str, message_id: int, mode: str, page: int = 0) -> None:
        mode = "del" if mode == "del" else "edit"
        try:
            rows = self._calendar_rows()
            self._calendar_event_catalog_by_chat[str(chat_id)] = rows
        except Exception as exc:
            self._panel(chat_id, message_id, f"📅 Не удалось получить расписание: {exc}", [[("⬅️ Назад", "ui:cal")]], view="cal")
            return
        per = 7
        page = max(0, int(page))
        start = page * per
        chunk = rows[start:start + per]
        buttons: list[list[tuple[str, str]]] = []
        for idx, event in enumerate(chunk, start):
            dt = event_start_datetime(event)
            when = dt.strftime("%d.%m %H:%M") if dt else "без времени"
            title = str(event.get("summary", "") or "(без названия)")
            buttons.append([(f"{when} · {title[:36]}", f"a:cal:{mode}:{idx}")])
        nav: list[tuple[str, str]] = []
        if page > 0:
            nav.append(("◀️", f"ui:cal{mode}:{page - 1}"))
        if start + per < len(rows):
            nav.append(("▶️", f"ui:cal{mode}:{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([("⬅️ Назад", "ui:cal")])
        title = "🗑 Выбери событие для удаления" if mode == "del" else "✏️ Выбери событие для редактирования"
        if not rows:
            title += "\nСобытий нет."
        self._panel(chat_id, message_id, title, buttons, view=f"cal{mode}:{page}")

    def _monitor_count(self) -> int:
        try:
            import mss
            with mss.mss() as sct:
                return max(1, len(sct.monitors) - 1)
        except Exception:
            return 1

    def _show_screen_menu(self, chat_id: str, message_id: int) -> None:
        count = self._monitor_count()
        self._panel(chat_id, message_id, f"🖥 Экран\nПодключено мониторов: {count}.", [
            [("📸 Скриншоты", "ui:shot"), ("🪟 Работа с окнами", "ui:win")],
            [("⬅️ Назад", "ui:h")],
        ], view="screen")

    def _show_windows_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "🪟 Работа с окнами", [
            [("↔️ Переместить окно", "i:win:move"), ("➖ Свернуть окно", "i:win:min")],
            [("📋 Окна по экранам", "a:screen:list")],
            [("⬅️ Назад", "ui:apps"), ("🏠 Домой", "ui:h")],
        ], view="win")

    def _show_info_menu(self, chat_id: str, message_id: int) -> None:
        # Compatibility with stale HOTFIX60/61 callbacks. The old “Сведения”
        # hub was folded into System; weather is now a top-level phone button.
        self._show_system_menu(chat_id, message_id)

    def _show_weather_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "🌦 Погода\nАктуальная сводка показана сообщением выше.", [
            [("🔄 Обновить погоду", "a:info:weather")],
            [("⬅️ Назад", "ui:h")],
        ], view="weather")

    def _show_process_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "📋 Сведения о процессах\nТаблица выше показывает реальные процессы: CPU, GPU, RAM, диск и PID.", [
            [("🔄 Обновить", "a:info:proc")],
            [("⬅️ Назад", "ui:sys"), ("🏠 Домой", "ui:h")],
        ], view="proc")

    def _show_move_monitor_menu(self, chat_id: str, message_id: int, target: str) -> None:
        count = self._monitor_count()
        rows: list[list[tuple[str, str]]] = []
        row: list[tuple[str, str]] = []
        for index in range(1, count + 1):
            row.append((f"🖥 Экран {index}", f"a:mvwin:{index}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([("⬅️ Назад", "ui:win"), ("🏠 Домой", "ui:h")])
        safe = target if len(target) <= 70 else target[:67] + "…"
        self._panel(chat_id, message_id, f"🪟 Переместить окно\nЦель: {safe}\nКуда переместить?", rows, view="win")

    def _show_screenshot_menu(self, chat_id: str, message_id: int) -> None:
        count = self._monitor_count()
        rows: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        for index in range(1, count + 1):
            current.append((f"🖥 Экран {index}", f"a:shot:{index}"))
            if len(current) == 2:
                rows.append(current); current = []
        if current: rows.append(current)
        if count > 1: rows.append([("🖼 Все экраны", "a:shot:all")])
        rows.append([("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")])
        self._panel(chat_id, message_id, f"📸 Скриншот\nПодключено экранов: {count}. Какой экран снять?", rows, view="shot")

    # ------------------------------------------------------- HOTFIX106 panels
    def _overlay_panels(self) -> list[dict[str, Any]]:
        try:
            tools = self._tools_for_direct_action("overlay")
            result = tools.execute("overlay", {"action": "list"})
            return list(result.data or []) if result.success and isinstance(result.data, list) else []
        except Exception:
            logger.debug("Could not read overlay catalog for Telegram", exc_info=True)
            return []

    @staticmethod
    def _overlay_defaults_for(category: str) -> tuple[str, str, str]:
        key = str(category or "").casefold()
        if key == "game": return "Игровая сессия", "text", "game_session"
        if key == "music": return "Музыка", "music", "music"
        if key == "checklist": return "Чек-лист", "checklist", "checklist"
        return "Заметки", "text", "notes"

    def _overlay_panel(self, category: str) -> dict[str, Any] | None:
        _name, kind, purpose = self._overlay_defaults_for(category)
        for row in self._overlay_panels():
            if not isinstance(row, dict):
                continue
            row_purpose = str(row.get("purpose", "") or "").casefold()
            row_kind = str(row.get("kind", "") or "").casefold()
            if purpose == "game_session" and row_purpose.startswith("game_session"):
                return row
            if row_purpose == purpose:
                return row
            if category == "checklist" and row_kind == "checklist":
                return row
        return None

    def _overlay_panel_name(self, category: str) -> str:
        row = self._overlay_panel(category)
        if isinstance(row, dict) and str(row.get("name", "") or "").strip():
            return str(row.get("name")).strip()
        return self._overlay_defaults_for(category)[0]

    def _ensure_overlay_panel_for_remote(self, chat_id: str, category: str) -> None:
        """Create a hidden panel shell before move/rename-only Telegram actions.

        OverlayState intentionally refuses ``move``/``rename`` on an unknown
        panel to avoid accidental voice-created windows.  Telegram, however,
        exposes monitor/position controls even while a panel is disabled.
        Queueing a quiet ``style`` shell first preserves the global fail-closed
        contract while making the explicit Telegram control deterministic.
        Both jobs use the same desktop lane, so order is preserved.
        """
        if self._overlay_panel(category) is not None:
            return
        name, kind, purpose = self._overlay_defaults_for(category)
        self._execute_tool_now(
            chat_id, "overlay",
            {"action": "style", "name": name, "kind": kind, "purpose": purpose},
            "Панель · подготовить", recreate_panel=False, quiet=True,
        )

    def _overlay_font_size(self, category: str, panel: dict[str, Any] | None) -> int:
        if isinstance(panel, dict):
            style = panel.get("style") if isinstance(panel.get("style"), dict) else {}
            try:
                if style.get("font_size") is not None:
                    return max(8, min(96, int(style.get("font_size"))))
            except Exception:
                pass
        cfg_cat = {"game":"session", "music":"music", "checklist":"checklist", "notes":"notes"}.get(category, "default")
        try:
            return max(8, min(96, int(self.config.get(f"desktop.overlay.styles.{cfg_cat}.font_size", 18) or 18)))
        except Exception:
            return 18

    @staticmethod
    def _briefing_ui_text(code: str, key: str) -> str:
        rows = {
            "title":{"ru":"Сводка дня","en":"Daily briefing","de":"Tagesübersicht","fr":"Résumé du jour","es":"Resumen diario","it":"Riepilogo giornaliero","pt":"Resumo do dia"},
            "subtitle":{"ru":"Автоматическая сводка при запуске звучит только один раз в день. Ручной запуск доступен всегда.","en":"The automatic startup briefing is delivered only once per day. Manual playback is always available.","de":"Die automatische Startübersicht wird nur einmal pro Tag ausgegeben. Der manuelle Aufruf ist jederzeit möglich.","fr":"Le résumé automatique au démarrage n’est présenté qu’une fois par jour. Le lancement manuel reste toujours disponible.","es":"El resumen automático al iniciar se presenta solo una vez al día. El inicio manual siempre está disponible.","it":"Il riepilogo automatico all’avvio viene presentato una sola volta al giorno. L’avvio manuale è sempre disponibile.","pt":"O resumo automático no arranque é apresentado apenas uma vez por dia. O início manual está sempre disponível."},
        }
        row = rows.get(key, {})
        return row.get(code) or row.get("en") or key

    def _show_daily_briefing_menu(self, chat_id: str, message_id: int) -> None:
        code = language_code(self.config)
        title = "🗓 " + self._briefing_ui_text(code, "title") + "\n" + self._briefing_ui_text(code, "subtitle")
        rows = [
            [("▶️ Показать сводку сейчас", "a:brief:now")],
            [("⬅️ Назад", "ui:home")],
        ]
        self._panel(chat_id, message_id, title, rows, view="brief")

    def _show_settings_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "⚙️ Настройки", [
            [("Дополнительно", "ui:neoset:advanced"), ("Окно", "ui:neoset:window")],
            [("Робот", "ui:neoset:robot"), ("Виджеты", "ui:neoset:shelf")],
            [("Безопасность", "ui:neoset:privacy")],
            [("Smart Downloads", "ui:neoset:downloads"), ("📡 Сеть", "ui:net")],
            [("Содержимое полки", "a:neo:shelf_list"), ("Новая заметка", "i:neo:note")],
            [("Найти файл", "i:neo:file"), ("Данные игры", "i:neo:game")],
            [("История действий", "a:neo:activity"), ("Мини-браузер", "i:neo:browser")],
            [("🧱 Панели", "ui:panels"), ("⬅️ Назад", "ui:home")],
        ], view="settings")

    def _show_remote_settings(self, chat_id: str, message_id: int, group: str, page: int = 0) -> None:
        from jarvis.integrations.telegram_settings import SETTINGS, display_value, message
        labels = {"advanced": "Дополнительно", "window": "Окно", "robot": "Робот", "shelf": "Виджеты", "privacy": "Безопасность"}
        if group not in labels:
            return
        code = language_code(self.config)
        specs = [spec for spec in SETTINGS.values() if spec.group == group]
        pages = max(1, (len(specs) + 6) // 7)
        page = max(0, min(pages - 1, page))
        rows = []
        for spec in specs[page * 7:(page + 1) * 7]:
            current = display_value(spec, self.config.get(spec.path, spec.default), code).replace("\n", "; ")
            rows.append([(f"{spec.label(code)}: {current[:38]}", f"ui:ncfgitem:{spec.id}")])
        nav = []
        if page:
            nav.append(("◀️", f"ui:ncfg:{group}:{page - 1}"))
        if page + 1 < pages:
            nav.append(("▶️", f"ui:ncfg:{group}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("⬅️ " + message("back", code), "ui:settings")])
        self._panel(chat_id, message_id, "⚙️ " + self._tg_text(labels[group]) + f" · {page + 1}/{pages}", rows, view=f"ncfg:{group}:{page}")

    def _show_remote_setting(self, chat_id: str, user_id: str, message_id: int, sid: str) -> None:
        from jarvis.integrations.telegram_settings import SETTINGS, display_value, message
        spec = SETTINGS.get(sid)
        if spec is None:
            return
        code = language_code(self.config)
        value = self.config.get(spec.path, spec.default)
        title = spec.label(code) + "\n" + message("current", code) + ": " + display_value(spec, value, code)
        back = f"ncfg:{spec.group}:0"
        if spec.kind in {"enum", "bool"}:
            values = spec.choices if spec.kind == "enum" else (False, True)
            rows = [[(display_value(spec, choice, code), f"a:ncfg:{sid}:{index}")] for index, choice in enumerate(values)]
            rows.append([("⬅️ " + message("back", code), "ui:" + back)])
            self._panel(chat_id, message_id, title, rows, view=back)
        else:
            hint = message("enter", code)
            if spec.kind in {"int", "float"}:
                hint += f": {spec.minimum:g}–{spec.maximum:g}"
            elif spec.kind == "color":
                hint += ": #RRGGBB"
            elif spec.kind == "lines":
                hint += ". " + message("lines", code)
            elif spec.kind == "hotkey":
                hint += ": Ctrl+Alt+B / -"
            self._prompt_input(chat_id, user_id, message_id, "remote_setting", title + "\n\n" + hint, payload={"setting_id": sid}, return_view=back)

    def _save_remote_setting(self, chat_id: str, message_id: int, sid: str, raw: str) -> None:
        from jarvis.integrations.telegram_settings import SETTINGS, apply_setting, display_value, message
        code = language_code(self.config)
        try:
            spec, value, queued = apply_setting(self.config, sid, raw)
        except (TypeError, ValueError, KeyError):
            self._send_message(chat_id, message("invalid", code))
            return
        except Exception:
            logger.exception("Telegram public setting save failed: %s", sid)
            self._send_message(chat_id, message("failed", code))
            return
        code = language_code(self.config)
        self._send_message(chat_id, spec.label(code) + ": " + display_value(spec, value, code) + "\n" + message("saved" if queued else "deferred", code))
        group_items = [item for item in SETTINGS.values() if item.group == spec.group]
        self._show_remote_settings(chat_id, message_id, spec.group, group_items.index(spec) // 7)

    def _show_panels_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "🧱 Панели NEO\nВыбери панель для настройки.", [
            [("🎮 Игровой оверлей", "ui:ov:game"), ("🎵 Музыкальный оверлей", "ui:ov:music")],
            [("☑️ Чек-лист", "ui:ov:checklist"), ("📝 Заметки", "ui:ov:notes")],
            [("🟢 Включить все", "a:ovall:on"), ("⚫ Выключить все", "a:ovall:off")],
            [("⚙️ Общие настройки", "ui:ovsettings")],
            [("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")],
        ], view="panels")

    def _show_overlay_menu(self, chat_id: str, message_id: int, category: str) -> None:
        category = category if category in {"game","music","checklist","notes"} else "notes"
        panel = self._overlay_panel(category)
        visible = bool(panel.get("visible", False)) if isinstance(panel, dict) else False
        size = self._overlay_font_size(category, panel)
        monitor = int((panel or {}).get("monitor", 1) or 1) if isinstance(panel, dict) else 1
        position = str((panel or {}).get("position", "top_left") or "top_left") if isinstance(panel, dict) else "top_left"
        labels = {"game":"🎮 Игровой оверлей", "music":"🎵 Музыкальный оверлей", "checklist":"☑️ Чек-лист", "notes":"📝 Заметки"}
        rows: list[list[tuple[str,str]]] = [
            [("🟢 Выключить" if visible else "⚪ Включить", f"a:ovtoggle:{category}")],
            [("🖥 Экран", f"ui:ovmon:{category}"), ("📍 Расположение", f"ui:ovpos:{category}")],
            [("⚙️ Настройки вида", f"ui:ovcfg:{category}")],
            [("➖ Шрифт −2", f"a:ovfont:{category}:-2"), ("➕ Шрифт +2", f"a:ovfont:{category}:2")],
        ]
        if category == "checklist":
            rows.extend([
                [("➕ Добавить пункт", "i:ovcheck:add"), ("✅ Зачеркнуть пункт", "i:ovcheck:mark")],
                [("🗑 Удалить пункт", "i:ovcheck:delete"), ("✏️ Название", "i:ov:rename:checklist")],
            ])
        elif category == "notes":
            rows.extend([
                [("✏️ Редактировать", "i:ovnote:set"), ("➕ Дополнить", "i:ovnote:append")],
                [("🧹 Очистить", "a:ovnote:clear"), ("🏷 Название", "i:ov:rename:notes")],
            ])
        rows.append([("⬅️ Панели", "ui:panels"), ("🏠 Домой", "ui:h")])
        status = "включена" if visible else "выключена"
        self._panel(chat_id, message_id,
                    f"{labels[category]}\nСостояние: {status} · экран {monitor} · {position} · шрифт {size}",
                    rows, view=f"ov:{category}")

    @staticmethod
    def _overlay_style_key(category: str) -> str:
        return {"game":"session", "music":"music", "checklist":"checklist", "notes":"notes"}.get(str(category or "").casefold(), "notes")

    @staticmethod
    def _overlay_category_label(category: str) -> str:
        return {"game":"🎮 Игровой оверлей", "music":"🎵 Музыкальный оверлей", "checklist":"☑️ Чек-лист", "notes":"📝 Заметки"}.get(str(category or "").casefold(), "📝 Заметки")

    def _overlay_config_style(self, category: str) -> dict[str, Any]:
        value = self.config.get(f"desktop.overlay.styles.{self._overlay_style_key(category)}", {}) or {}
        return dict(value) if isinstance(value, dict) else {}

    def _save_overlay_config_style(self, category: str, **changes: Any) -> None:
        key = self._overlay_style_key(category)
        data = self.config.data
        desktop = data.setdefault("desktop", {})
        overlay = desktop.setdefault("overlay", {})
        styles = overlay.setdefault("styles", {})
        current = styles.setdefault(key, {})
        if not isinstance(current, dict):
            current = {}; styles[key] = current
        for name, value in changes.items():
            current[name] = value
        # Settings is the canonical persistent store; ConfigManager atomically
        # writes/reloads it so desktop Settings and Telegram stay in sync.
        self.config.save(data)

    def _apply_overlay_style_setting(self, chat_id: str, category: str, **changes: Any) -> tuple[bool, str]:
        category = category if category in {"game","music","checklist","notes"} else "notes"
        try:
            # Appearance selected in Telegram is a category-level Setting, exactly
            # like Desktop Settings.  Do not send an overlay ``style`` command:
            # that would create a per-window style_override and could later mask
            # changes made in Desktop Settings.  Persist once and publish a GUI
            # refresh; hidden/not-yet-created panels pick the same config up when
            # they are next shown.
            self._save_overlay_config_style(category, **changes)
            # If this profile already contains an older per-panel style override,
            # rejoin that panel to the category settings the user just chose.
            refreshed = False
            try:
                paths = getattr(self.config, "paths", None)
                root = getattr(paths, "root", None)
                if root:
                    refreshed = bool(
                        OverlayStore(Path(root) / "overlays.json").clear_category_style_overrides(
                            self._overlay_style_key(category)
                        )
                    )
            except Exception:
                logger.debug("Could not clear legacy overlay style override", exc_info=True)
            if not refreshed:
                publish_overlay_refresh("")
            return True, "Настройки панели применены."
        except Exception as exc:
            logger.exception("Could not apply Telegram overlay style")
            return False, str(exc)

    def _show_overlay_settings_hub(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "⚙️ " + self._tg_message("Выбери панель, внешний вид которой нужно настроить."), [
            [("🎮 Игровой оверлей", "ui:ovcfg:game"), ("🎵 Музыкальный оверлей", "ui:ovcfg:music")],
            [("☑️ Чек-лист", "ui:ovcfg:checklist"), ("📝 Заметки", "ui:ovcfg:notes")],
            [("⬅️ Панели", "ui:panels"), ("🏠 Домой", "ui:h")],
        ], view="ovsettings")

    def _show_overlay_style_menu(self, chat_id: str, message_id: int, category: str) -> None:
        category = category if category in {"game","music","checklist","notes"} else "notes"
        cfg = self._overlay_config_style(category)
        preset = str(cfg.get("theme_preset", "NEO Cyan") or "NEO Cyan")
        font = str(cfg.get("font_family", "Segoe UI") or "Segoe UI")
        size = max(8, min(96, int(cfg.get("font_size", 18) or 18)))
        opacity = max(0, min(100, int(cfg.get("background_opacity", cfg.get("opacity", 72)) or 0)))
        preset_label = self._tg_text("Пользовательский") if preset == "Custom" else preset
        title = (
            f"⚙️ {self._tg_text(self._overlay_category_label(category).split(' ', 1)[1]) if ' ' in self._overlay_category_label(category) else self._overlay_category_label(category)}\n"
            f"{self._tg_text('Пресет')}: {preset_label}\n"
            f"{self._tg_text('Шрифт')}: {font} · {size}\n"
            f"{self._tg_text('Прозрачность фона')}: {opacity}%"
        )
        self._panel(chat_id, message_id, title, [
            [("🎨 Пресет", f"ui:ovpreset:{category}:0")],
            [("🔤 Шрифт", f"ui:ovfontfam:{category}:0"), ("🔠 Размер шрифта", f"ui:ovsize:{category}")],
            [("◐ Прозрачность фона", f"ui:ovopacity:{category}")],
            [("⬅️ Назад", f"ui:ov:{category}"), ("⚙️ Все панели", "ui:ovsettings")],
            [("🏠 Домой", "ui:h")],
        ], view=f"ovcfg:{category}")

    def _show_overlay_preset_picker(self, chat_id: str, message_id: int, category: str, page: int = 0) -> None:
        category = category if category in {"game","music","checklist","notes"} else "notes"
        names = sorted(THEME_PRESETS.keys(), key=lambda value: value.casefold()) + ["Custom"]
        current = str(self._overlay_config_style(category).get("theme_preset", "NEO Cyan") or "NEO Cyan")
        per_page = 8
        pages = max(1, (len(names) + per_page - 1) // per_page)
        page = max(0, min(pages - 1, int(page or 0)))
        rows: list[list[tuple[str, str]]] = []
        for idx in range(page * per_page, min(len(names), (page + 1) * per_page)):
            name = names[idx]
            shown = self._tg_text("Пользовательский") if name == "Custom" else name
            rows.append([(("✅ " if name == current else "🎨 ") + shown, f"a:ovpreset:{category}:{idx}")])
        if pages > 1:
            nav: list[tuple[str, str]] = []
            if page > 0: nav.append(("⬅️", f"ui:ovpreset:{category}:{page-1}"))
            nav.append((f"{page+1}/{pages}", "noop"))
            if page + 1 < pages: nav.append(("➡️", f"ui:ovpreset:{category}:{page+1}"))
            rows.append(nav)
        rows.append([("⬅️ Назад", f"ui:ovcfg:{category}"), ("🏠 Домой", "ui:h")])
        self._panel(chat_id, message_id, "🎨 " + self._tg_message("Выбери визуальный пресет."), rows, view=f"ovcfg:{category}")

    @staticmethod
    def _fallback_font_families() -> list[str]:
        return ["Arial", "Calibri", "Cambria", "Cascadia Code", "Consolas", "Georgia", "Segoe UI", "Segoe UI Variable", "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana"]

    def _installed_font_families(self) -> list[str]:
        names: set[str] = set()
        if os.name == "nt":
            try:
                import winreg
                for hive, key_path in (
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
                    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
                ):
                    try:
                        with winreg.OpenKey(hive, key_path) as key:
                            count = winreg.QueryInfoKey(key)[1]
                            for i in range(count):
                                raw = str(winreg.EnumValue(key, i)[0] or "").strip()
                                raw = re.sub(r"\s*\((?:TrueType|OpenType|All res)\)\s*$", "", raw, flags=re.I)
                                # Registry values can be faces. Trim common style
                                # suffixes so the phone shows mostly usable families.
                                raw = re.sub(r"\s+(?:Bold|Italic|Oblique|Regular|Light|Semilight|Semibold|Medium|Black)(?:\s+Italic)?$", "", raw, flags=re.I).strip()
                                if 1 < len(raw) <= 80: names.add(raw)
                    except OSError:
                        continue
            except Exception:
                logger.debug("Could not enumerate Windows fonts", exc_info=True)
        names.update(self._fallback_font_families())
        for category in ("game", "music", "checklist", "notes"):
            current = str(self._overlay_config_style(category).get("font_family", "") or "").strip()
            if current: names.add(current)
        return sorted(names, key=lambda value: value.casefold())

    def _show_overlay_font_picker(self, chat_id: str, message_id: int, category: str, page: int = 0) -> None:
        category = category if category in {"game","music","checklist","notes"} else "notes"
        catalog = self._installed_font_families()
        self._overlay_font_catalog_by_chat[str(chat_id)] = catalog
        current = str(self._overlay_config_style(category).get("font_family", "Segoe UI") or "Segoe UI")
        per_page = 8
        pages = max(1, (len(catalog) + per_page - 1) // per_page)
        page = max(0, min(pages - 1, int(page or 0)))
        rows: list[list[tuple[str, str]]] = []
        for idx in range(page * per_page, min(len(catalog), (page + 1) * per_page)):
            name = catalog[idx]
            rows.append([(("✅ " if name.casefold() == current.casefold() else "🔤 ") + name[:48], f"a:ovfontpick:{category}:{idx}")])
        if pages > 1:
            nav: list[tuple[str, str]] = []
            if page > 0: nav.append(("⬅️", f"ui:ovfontfam:{category}:{page-1}"))
            nav.append((f"{page+1}/{pages}", "noop"))
            if page + 1 < pages: nav.append(("➡️", f"ui:ovfontfam:{category}:{page+1}"))
            rows.append(nav)
        rows.append([("⬅️ Назад", f"ui:ovcfg:{category}"), ("🏠 Домой", "ui:h")])
        self._panel(chat_id, message_id, "🔤 " + self._tg_message("Выбери семейство шрифта."), rows, view=f"ovcfg:{category}")

    def _show_overlay_font_size_picker(self, chat_id: str, message_id: int, category: str) -> None:
        category = category if category in {"game","music","checklist","notes"} else "notes"
        current = max(8, min(96, int(self._overlay_config_style(category).get("font_size", 18) or 18)))
        values = [10, 12, 14, 16, 18, 20, 22, 24, 28, 32, 36, 42, 48, 56, 64, 72]
        if current not in values: values.append(current); values.sort()
        rows: list[list[tuple[str, str]]] = []
        for start in range(0, len(values), 4):
            row=[]
            for value in values[start:start+4]:
                row.append((("✅ " if value == current else "") + str(value), f"a:ovsize:{category}:{value}"))
            rows.append(row)
        rows.append([("⬅️ Назад", f"ui:ovcfg:{category}"), ("🏠 Домой", "ui:h")])
        self._panel(chat_id, message_id, "🔠 " + self._tg_message("Выбери размер шрифта."), rows, view=f"ovcfg:{category}")

    def _show_overlay_opacity_picker(self, chat_id: str, message_id: int, category: str) -> None:
        category = category if category in {"game","music","checklist","notes"} else "notes"
        cfg = self._overlay_config_style(category)
        current = max(0, min(100, int(cfg.get("background_opacity", cfg.get("opacity", 72)) or 0)))
        values = list(range(0, 101, 10))
        if current not in values: values.append(current); values.sort()
        rows: list[list[tuple[str, str]]] = []
        for start in range(0, len(values), 3):
            rows.append([(("✅ " if value == current else "") + f"{value}%", f"a:ovopacity:{category}:{value}") for value in values[start:start+3]])
        rows.append([("⬅️ Назад", f"ui:ovcfg:{category}"), ("🏠 Домой", "ui:h")])
        self._panel(chat_id, message_id, "◐ " + self._tg_message("Выбери прозрачность фона. 0% — фон прозрачный, 100% — полностью плотный."), rows, view=f"ovcfg:{category}")

    def _set_all_overlay_visibility(self, chat_id: str, visible: bool) -> None:
        try:
            tools = self._tools_for_direct_action("overlay")
            if not visible:
                tools.execute("overlay", {"action":"hide_all"})
                return
            # Explicitly show/create persistent panels. Game session is special:
            # it is shown only when a real tracked game exists.
            for category in ("music", "checklist", "notes"):
                panel = self._overlay_panel(category)
                name, kind, purpose = self._overlay_defaults_for(category)
                args = {"action":"show", "name":self._overlay_panel_name(category) or name, "title":self._overlay_panel_name(category) or name, "kind":kind, "purpose":purpose}
                if category == "checklist" and panel is None:
                    args.update({"action":"checklist_create", "items":[]})
                tools.execute("overlay", args)
            try:
                self._tools_for_direct_action("game").execute("game", {"action":"show_session"})
            except Exception:
                pass
        except Exception:
            logger.exception("Could not toggle all overlays from Telegram")

    # --------------------------------------------------- secure phone Explorer
    def _leave_explorer(self, chat_id: str) -> None:
        state = self._explorer_by_chat.pop(str(chat_id), None)
        del state

    def _explorer_state(self, chat_id: str) -> dict[str, Any] | None:
        state = self._explorer_by_chat.get(str(chat_id))
        return state if isinstance(state, dict) and bool(state.get("authorized", False)) else None

    def _explorer_drive_catalog(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        try:
            result = self._tools_for_direct_action("filesystem").execute("filesystem", {"action":"list_drives"})
            data = list(result.data or []) if result.success and isinstance(result.data, list) else []
        except Exception:
            data = []
        for row in data:
            if not isinstance(row, dict): continue
            raw = str(row.get("mount") or row.get("path") or row.get("mountpoint") or row.get("device") or "").strip()
            if not raw: continue
            try: path = str(Path(raw))
            except Exception: path = raw
            key = os.path.normcase(os.path.normpath(path))
            if key in seen: continue
            seen.add(key)
            label = str(row.get("label") or row.get("name") or row.get("device") or raw).strip()
            if os.name == "nt" and re.match(r"^[A-Za-z]:", raw):
                label = raw[:2] + (f" · {label}" if label and label.casefold() != raw.casefold() else "")
            rows.append({"path": raw, "label": label or raw})
        if not rows:
            fallback = str(Path.home().anchor or Path.home())
            rows.append({"path": fallback, "label": fallback})
        return rows[:32]

    def _show_explorer_drives(self, chat_id: str, message_id: int) -> None:
        state = self._explorer_state(chat_id)
        if state is None:
            self._send_message(chat_id, self._tg_message("Сеанс Проводника завершён. Войди снова через кодовую фразу.")); self._show_home(chat_id, message_id); return
        drives = self._explorer_drive_catalog()
        state.update({"drives":drives, "current_path":"", "folders":[], "page":0})
        rows: list[list[tuple[str,str]]] = []
        pair: list[tuple[str,str]] = []
        for idx,row in enumerate(drives):
            pair.append(("💽 " + str(row.get("label", row.get("path", "")))[:32], f"a:exp:drive:{idx}"))
            if len(pair) == 2: rows.append(pair); pair=[]
        if pair: rows.append(pair)
        rows.append([("🏠 Домой", "ui:h")])
        self._panel(chat_id, message_id, "📁 " + self._tg_text("Проводник") + "\n" + self._tg_message("Выбери диск."), rows, view="explorer")

    def _explorer_list_folders(self, path_text: str) -> tuple[list[dict[str,str]], str]:
        try:
            base = Path(path_text)
            if not base.exists() or not base.is_dir():
                return [], "path_not_found"
            rows=[]
            for child in base.iterdir():
                try:
                    if child.is_dir(): rows.append({"name":child.name or str(child), "path":str(child)})
                except OSError:
                    continue
            rows.sort(key=lambda row: str(row.get("name", "")).casefold())
            return rows[:1000], ""
        except PermissionError:
            return [], "access_denied"
        except OSError as exc:
            return [], str(exc)

    def _show_explorer_folder(self, chat_id: str, message_id: int, *, page: int = 0) -> None:
        state = self._explorer_state(chat_id)
        if state is None:
            self._send_message(chat_id, self._tg_message("Сеанс Проводника завершён. Войди снова через кодовую фразу.")); self._show_home(chat_id, message_id); return
        path_text = str(state.get("current_path", "") or "")
        if not path_text:
            self._show_explorer_drives(chat_id, message_id); return
        folders, error = self._explorer_list_folders(path_text)
        state["folders"] = folders
        per_page = 8
        pages=max(1,(len(folders)+per_page-1)//per_page)
        page=max(0,min(pages-1,int(page or 0))); state["page"]=page
        rows: list[list[tuple[str,str]]] = []
        for idx in range(page*per_page, min(len(folders),(page+1)*per_page)):
            name=str(folders[idx].get("name", "") or "")
            rows.append([("📁 " + (name[:46] + "…" if len(name)>47 else name), f"a:exp:dir:{idx}")])
        if pages>1:
            nav=[]
            if page>0: nav.append(("⬅️",f"ui:exp:page:{page-1}"))
            nav.append((f"{page+1}/{pages}","noop"))
            if page+1<pages: nav.append(("➡️",f"ui:exp:page:{page+1}"))
            rows.append(nav)
        if not folders:
            rows.append([("— " + self._tg_message("На этом уровне нет доступных папок."), "noop")])
        rows.append([("⬆️ Вверх", "a:exp:up"), ("💽 Диски", "a:exp:drives")])
        rows.append([("📂 Открыть эту папку", "a:exp:open")])
        rows.append([("🏠 Домой", "ui:h")])
        extra = ("\n⚠️ " + self._tg_runtime_text(error)) if error and error not in {"access_denied","path_not_found"} else ""
        title = f"📁 {self._tg_text('Проводник')}\n{self._tg_text('Папка')}: {path_text}{extra}"
        self._panel(chat_id, message_id, title, rows, view="explorer")

    def _show_overlay_monitor_menu(self, chat_id: str, message_id: int, category: str) -> None:
        rows: list[list[tuple[str,str]]] = []
        pair: list[tuple[str,str]] = []
        for monitor in range(1, self._monitor_count()+1):
            pair.append((f"🖥 Экран {monitor}", f"a:ovmon:{category}:{monitor}"))
            if len(pair) == 2: rows.append(pair); pair=[]
        if pair: rows.append(pair)
        rows.append([("⬅️ Назад", f"ui:ov:{category}"), ("🏠 Домой", "ui:h")])
        self._panel(chat_id, message_id, "🖥 Выбери экран для панели.", rows, view=f"ov:{category}")

    def _show_overlay_position_menu(self, chat_id: str, message_id: int, category: str) -> None:
        items = [
            ("↖️", "top_left"), ("⬆️", "top_center"), ("↗️", "top_right"),
            ("⬅️", "center_left"), ("⏺", "center"), ("➡️", "center_right"),
            ("↙️", "bottom_left"), ("⬇️", "bottom_center"), ("↘️", "bottom_right"),
        ]
        rows = [[(items[i+j][0], f"a:ovpos:{category}:{items[i+j][1]}") for j in range(3)] for i in range(0,9,3)]
        rows.append([("⬅️ Назад", f"ui:ov:{category}"), ("🏠 Домой", "ui:h")])
        self._panel(chat_id, message_id, "📍 Выбери положение панели на экране.", rows, view=f"ov:{category}")

    def _show_checklist_item_picker(self, chat_id: str, message_id: int, action: str, *, page: int = 0) -> None:
        action = "mark" if str(action or "").casefold() == "mark" else "delete"
        panel = self._overlay_panel("checklist")
        raw_items = list((panel or {}).get("items") or []) if isinstance(panel, dict) else []
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text", "") or "").strip()
            if not text:
                continue
            # Completing an already completed row is not useful; deletion still
            # exposes every existing row.
            if action == "mark" and bool(raw.get("checked", False)):
                continue
            items.append({"text": text, "checked": bool(raw.get("checked", False))})
        if not items:
            message = self._tg_message("Нет незачеркнутых пунктов чек-листа.") if action == "mark" else self._tg_message("В чек-листе нет пунктов.")
            self._panel(chat_id, message_id, message, [[("⬅️ Назад", "ui:ov:checklist"), ("🏠 Домой", "ui:h")]], view="ov:checklist")
            return

        self._checklist_catalog_by_chat[str(chat_id)] = {"action": action, "items": items}
        per_page = 8
        page_count = max(1, (len(items) + per_page - 1) // per_page)
        page = max(0, min(page_count - 1, int(page or 0)))
        start = page * per_page
        shown = items[start:start + per_page]
        rows: list[list[tuple[str, str]]] = []
        for offset, item in enumerate(shown):
            index = start + offset
            prefix = "☑" if bool(item.get("checked")) else "☐"
            text = str(item.get("text", "") or "")
            label = f"{prefix} {text}"
            if len(label) > 52:
                label = label[:49] + "…"
            rows.append([(label, f"a:ovcheck:{action}:{index}")])
        if page_count > 1:
            nav: list[tuple[str, str]] = []
            if page > 0:
                nav.append(("⬅️", f"ui:ovcheck:{action}:{page-1}"))
            nav.append((f"{page+1}/{page_count}", "noop"))
            if page + 1 < page_count:
                nav.append(("➡️", f"ui:ovcheck:{action}:{page+1}"))
            rows.append(nav)
        rows.append([("⬅️ Назад", "ui:ov:checklist"), ("🏠 Домой", "ui:h")])
        title = self._tg_message("Выбери пункт, который нужно зачеркнуть.") if action == "mark" else self._tg_message("Выбери пункт, который нужно удалить.")
        self._panel(chat_id, message_id, title, rows, view="ov:checklist")

    def _show_browser_cleanup_picker(self, chat_id: str, message_id: int, mode: str) -> None:
        try:
            result = self._tools_for_direct_action("browser").execute("browser", {"action":"list_browsers"})
            rows_data = list(result.data or []) if result.success and isinstance(result.data, list) else []
        except Exception:
            rows_data = []
        self._browser_cleanup_catalog_by_chat[str(chat_id)] = rows_data
        rows: list[list[tuple[str,str]]] = []
        for idx, row in enumerate(rows_data[:12]):
            rows.append([(str(row.get("label", row.get("key", "Браузер"))), f"a:brclean:{mode}:{idx}")])
        rows.append([("⬅️ Назад", "ui:br"), ("🏠 Домой", "ui:h")])
        if mode == "f":
            title = "💣 Полный снос локальных данных браузера\nУдаляются профили, cookies, пароли, история и локальные настройки. Сам браузер останется установлен. Потребуется кодовая фраза."
        else:
            title = "🧹 Очистить историю браузера\nВыбери браузер. Потребуется кодовая фраза. Браузер должен быть полностью закрыт."
        if not rows_data:
            title += "\n\nПоддерживаемые локальные профили не найдены."
        self._panel(chat_id, message_id, title, rows, view="br")

    def _show_close_picker(self, chat_id: str, message_id: int, kind: str) -> None:
        items: list[dict[str, Any]] = []
        if kind == "game":
            # Show every process-verified running game, not merely the currently
            # selected game context.  This keeps Telegram close-game semantics
            # distinct from the singular game-session tracker.
            try:
                from jarvis.gaming.manager import GameManager
                manager = GameManager(enabled=bool(self.config.get("gaming.enabled", True)), config=self.config)
                seen: set[int] = set()
                for adapter, proc in manager.detect_all():
                    pid = int(getattr(proc, "pid", 0) or 0)
                    if pid and pid in seen:
                        continue
                    if pid:
                        seen.add(pid)
                    label = str(getattr(adapter, "display_name", "") or getattr(proc, "name", "") or f"PID {pid}").strip()
                    target = str(getattr(proc, "name", "") or Path(str(getattr(proc, "exe", "") or "")).name or label).strip()
                    if target:
                        items.append({"label": label, "target": target, "pid": pid})
            except Exception:
                pass
            # Fallback to the current tracker snapshot if process discovery is
            # unavailable for a custom/unsupported game.
            if not items:
                try:
                    result = self._tools_for_direct_action("game").execute("game", {"action":"status"})
                    if result.success and isinstance(result.data, dict):
                        data = dict(result.data)
                        label = str(data.get("game_name", "") or data.get("name", "") or data.get("process_name", "") or data.get("executable", "") or "Игра")
                        target = str(data.get("process_name", "") or Path(str(data.get("executable", "") or "")).name or label)
                        if target:
                            items.append({"label": label, "target": target, "pid": data.get("pid")})
                except Exception:
                    pass
        else:
            try:
                result = self._tools_for_direct_action("application").execute("application", {"action":"list_active"})
                raw = list(result.data or []) if result.success and isinstance(result.data, list) else []
                seen: set[int] = set()
                for row in raw:
                    if not isinstance(row, dict): continue
                    pid = int(row.get("pid",0) or 0)
                    if pid and pid in seen: continue
                    if pid: seen.add(pid)
                    title = str(row.get("title","") or "").strip()
                    proc = str(row.get("process","") or "").strip()
                    label = title or proc or f"PID {pid}"
                    target = proc or title
                    if target: items.append({"label": label, "target": target, "pid": pid})
            except Exception:
                pass
        self._close_catalog_by_chat[str(chat_id)] = items[:12]
        rows: list[list[tuple[str,str]]] = []
        for idx, row in enumerate(items[:12]):
            label = str(row.get("label", "") or "")
            if len(label) > 44: label = label[:41] + "…"
            rows.append([(label, f"a:closepick:{kind}:{idx}")])
        rows.append([("➕ Другое", f"i:closeother:{kind}")])
        rows.append([("⬅️ Назад", "ui:apps"), ("🏠 Домой", "ui:h")])
        title = "⛔ Активные игры" if kind == "game" else "🛑 Активные приложения"
        title += "\nВыбери цель. Перед закрытием NEO покажет отдельное подтверждение."
        self._panel(chat_id, message_id, title, rows, view="apps")

    def _show_close_confirmation(self, chat_id: str, message_id: int, kind: str, target: str, label: str = "") -> None:
        self._close_pending_by_chat[str(chat_id)] = {"kind":kind, "target":target, "label":label or target}
        human = label or target
        self._panel(chat_id, message_id, f"⚠️ Закрыть {'игру' if kind=='game' else 'приложение'} «{human}»?", [
            [("✅ Закрыть", "a:closeconfirm:yes"), ("❌ Не закрывать", "a:closeconfirm:no")],
            [("⬅️ Назад", f"ui:close:{kind}")],
        ], view="apps")

    def _show_plain_confirmation(
        self, chat_id: str, message_id: int, title: str, pending: dict[str, Any],
        *, yes_label: str = "✅ Подтвердить", no_label: str = "❌ Отмена", view: str = "home",
    ) -> None:
        payload = dict(pending or {})
        payload["created_at"] = time.time()
        payload["return_view"] = str(payload.get("return_view", view) or view)
        self._plain_confirm_by_chat[str(chat_id)] = payload
        self._panel(chat_id, message_id, title, [
            [(yes_label, "a:plainconfirm:yes"), (no_label, "a:plainconfirm:no")],
            [("⬅️ Назад", "a:plainconfirm:no")],
        ], view=str(view or "home"))

    def _resolve_plain_confirmation(self, chat_id: str, user_id: str, message_id: int, *, accepted: bool) -> None:
        pending = self._plain_confirm_by_chat.pop(str(chat_id), None) or {}
        return_view = str(pending.get("return_view", "home") or "home")
        if not accepted:
            self._show_view(chat_id, return_view, message_id)
            return
        ttl = max(15.0, float(self.config.get("integrations.telegram.confirmation_ttl_seconds", 90) or 90))
        if not pending or time.time() - float(pending.get("created_at", 0) or 0) > ttl:
            self._send_message(chat_id, self._tg_message("Время подтверждения истекло."))
            self._show_view(chat_id, return_view, message_id)
            return
        mode = str(pending.get("mode", "") or "")
        if mode == "jarvis_start":
            # Online NEO never exposes Start. This branch only handles a stale
            # keyboard from an older build/session safely.
            self._send_message(chat_id, self._tg_message("NEO уже запущен."))
            self._show_home(chat_id, message_id)
            return
        tool_name = str(pending.get("tool_name", "") or "")
        arguments = dict(pending.get("arguments", {}) or {})
        label = str(pending.get("label", "Действие") or "Действие")
        if mode == "secure_tool":
            self._execute_tool(chat_id, user_id, tool_name, arguments, label, panel_message_id=message_id, return_view=return_view)
            return
        if mode == "direct_tool":
            is_jarvis_shutdown = tool_name == "jarvis" and str(arguments.get("action", "") or "").casefold() in {"shutdown", "quit", "exit"}
            if is_jarvis_shutdown:
                # Acknowledge before arming Qt shutdown. The old path started the
                # shutdown timer first and then tried to send/edit Telegram UI,
                # which could race controller teardown and surface spurious errors.
                self._send_message(chat_id, self._tg_message("✅ NEO завершает работу."))
            self._execute_tool_now(
                chat_id, tool_name, arguments, label, panel_message_id=message_id,
                return_view=return_view, recreate_panel=not is_jarvis_shutdown,
                quiet=is_jarvis_shutdown,
            )
            return
        self._show_view(chat_id, return_view, message_id)

    def _show_timer_menu(self, chat_id: str, message_id: int) -> None:
        manager = timer_manager(self.config.paths.root, language_code(self.config))
        rows_now = list(manager.list())
        now = time.time()
        lines = ["⏱ Таймеры"]
        if rows_now:
            for item in rows_now[:8]:
                remaining = max(0, int(__import__("math").ceil(manager.remaining(item))))
                hours, rem = divmod(remaining, 3600)
                minutes, seconds = divmod(rem, 60)
                stamp = f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"
                lines.append(f"• {item.label} · {stamp}")
        else:
            lines.append("Активных таймеров нет.")
        buttons: list[list[tuple[str, str]]] = [[("➕ Создать таймер", "i:timer:create")]]
        for item in rows_now[:6]:
            buttons.append([(f"🗑 {item.label[:34]}", f"a:timer:del:{item.id}")])
        if rows_now:
            buttons.append([("🧹 Удалить ближайший", "a:timer:delnext")])
        buttons.append([("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")])
        self._panel(chat_id, message_id, "\n".join(lines), buttons, view="timer")

    def _show_media_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "🎵 Медиа\nГде управляем воспроизведением?", [
            [("🎧 Discord", "ui:md"), ("💻 Компьютер", "ui:mc")],
            [("⬅️ Назад", "ui:h")],
        ], view="med")

    def _media_history_suffix(self, source: str, *, limit: int = 5) -> str:
        rows = list(self._media_history_by_source.get(str(source or "").casefold(), []) or [])[-max(1, int(limit)):]
        if not rows:
            return ""
        return "\n🕘 Недавно:\n" + "\n".join(f"• {row}" for row in rows)

    def _show_discord_media(self, chat_id: str, message_id: int) -> None:
        latest = self._latest_media_by_source.get("discord")
        if latest is not None and str(getattr(latest, "title", "") or "").strip():
            title = str(latest.title).strip()
            artist = str(getattr(latest, "artist", "") or "").strip()
            now_line = f"\n🎵 Сейчас: {title}" + (f" — {artist}" if artist else "")
        else:
            now_line = "\n🎵 Сейчас: состояние ещё не получено"
        history_line = self._media_history_suffix("discord")
        self._panel(chat_id, message_id, "🎧 Discord Music" + now_line + history_line, [
            [("🌊 Моя волна", "a:dc:wave"), ("❤️ Мои треки", "a:dc:tracks")],
            [("📂 Плейлист", "i:dc:pl"), ("🔎 Трек", "i:dc:track")],
            [("▶️ YouTube URL", "i:dc:youtube")],
            [("⏮ Назад", "a:dc:prev"), ("⏭ Далее", "a:dc:next")],
            # Pause was redundant in the phone UI. “Стоп” is the pause-state
            # command here, so “Продолжить” can always resume the same track/queue.
            [("▶️ Продолжить", "a:dc:play"), ("⏹ Стоп", "a:dc:stop")],
            [("🔀 Shuffle ON", "a:dc:sh1"), ("🔀 Shuffle OFF", "a:dc:sh0")],
            [("🔂 Повтор трека", "a:dc:rpt"), ("🔁 Повтор плейлиста", "a:dc:rpl")],
            [("🚫 Повтор OFF", "a:dc:rp0")],
            [("👍 Лайк", "a:dc:like"), ("👎 Дизлайк", "a:dc:dislike")],
            [("📋 Очередь", "a:dc:queue"), ("🧹 Очистить", "a:dc:qclear")],
            [("🔊 +10", "a:dcvolrel:+10"), ("🔉 +5", "a:dcvolrel:+5"), ("🔉 −5", "a:dcvolrel:-5"), ("🔈 −10", "a:dcvolrel:-10")],
            [("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")],
        ], view="md")

    def _discord_server_names(self) -> list[str]:
        rows = self.config.get("integrations.discord.servers", []) or []
        names = [str(x.get("name", "") or "").strip() for x in rows if isinstance(x, dict) and str(x.get("name", "") or "").strip()]
        if not names:
            default = str(self.config.get("integrations.discord.default_server", "") or "").strip()
            if default:
                names = [default]
        return names

    def _show_discord_server_picker(self, chat_id: str, message_id: int, mode: str) -> None:
        mode = "voice" if mode == "voice" else "text"
        names = self._discord_server_names()
        rows: list[list[tuple[str, str]]] = []
        for idx, name in enumerate(names):
            rows.append([(f"🏰 {name}", f"a:dcsel:{mode}:s:{idx}")])
        rows.append([("⬅️ Назад", "ui:md")])
        title = (
            "🔊 Выбери Discord-сервер для входа в голосовой чат"
            if mode == "voice" else
            "💬 Выбери Discord-сервер для отправки сообщения"
        )
        if not names:
            title += "\nСначала добавь поддерживаемый сервер в Настройки → Медиа → Discord."
        self._panel(chat_id, message_id, title, rows, view="md")

    def _show_discord_channel_picker(self, chat_id: str, message_id: int, mode: str, server_index: int) -> None:
        mode = "voice" if mode == "voice" else "text"
        names = self._discord_server_names()
        if not (0 <= server_index < len(names)):
            self._show_discord_server_picker(chat_id, message_id, mode)
            return
        server = names[server_index]
        try:
            result = self.agent.tools.execute("discord", {"action": "list_channels", "server": server})
            if not result.success:
                raise RuntimeError(result.text)
            data = dict(result.data or {})
            channels = list(data.get("voice_channels" if mode == "voice" else "text_channels") or [])
        except Exception as exc:
            self._panel(chat_id, message_id, f"Discord · не удалось получить каналы: {exc}", [[("⬅️ Назад", "ui:md")]], view="md")
            return
        key = f"{mode}:{server_index}"
        self._discord_channel_catalog_by_chat[str(chat_id)] = {"key": key, "server": server, "channels": channels}
        rows: list[list[tuple[str, str]]] = []
        for idx, item in enumerate(channels[:30]):
            rows.append([(str(item.get("name", "") or "канал")[:52], f"a:dcsel:{mode}:c:{server_index}:{idx}")])
        if not channels:
            rows.append([("Нет доступных каналов", "ui:md")])
        rows.append([("⬅️ Назад", "ui:md")])
        self._panel(
            chat_id, message_id,
            f"{'🔊 Голосовые' if mode == 'voice' else '💬 Текстовые'} каналы · {server}",
            rows, view="md",
        )

    def _show_computer_media(self, chat_id: str, message_id: int) -> None:
        # Legacy callback from pre-HOTFIX101 panels: open music controls directly.
        self._show_music_menu(chat_id, message_id)

    def _show_music_menu(self, chat_id: str, message_id: int) -> None:
        provider = active_music_provider(self.config)
        language = language_code(self.config)
        volume_rows = [[("🔊 +10", "a:musicvolrel:+10"), ("🔉 +5", "a:musicvolrel:+5"), ("🔉 −5", "a:musicvolrel:-5"), ("🔈 −10", "a:musicvolrel:-10")],
                       [("🔉 25%", "a:musicvol:25"), ("🔊 50%", "a:musicvol:50"), ("🔊 75%", "a:musicvol:75"), ("🔊 100%", "a:musicvol:100")],
                       [("✍️ Другая громкость", "i:music:vol")]]
        if provider == "spotify":
            rows = [
                [("📂 Плейлист", "i:music:pl"), ("🔎 Трек", "i:music:track")],
                [("▶️ YouTube URL", "i:music:youtube")],
                [("⏮ Назад", "a:music:prev"), ("⏭ Далее", "a:music:next")],
                [("⏸ Пауза", "a:music:pause"), ("▶️ Продолжить", "a:music:play")],
                [("🔀 Shuffle ON", "a:music:sh1"), ("🔀 Shuffle OFF", "a:music:sh0")],
                [("🔂 Повтор трека", "a:music:rpt"), ("🔁 Повтор плейлиста", "a:music:rpl")],
                [("🚫 Повтор OFF", "a:music:rp0")],
                [("ℹ️ Статус", "a:music:status")], *volume_rows,
                [("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")],
            ]
            self._panel(chat_id, message_id, "🎵 Музыка · Spotify" + self._media_history_suffix("spotify"), rows, view="mm"); return
        if provider == "yandex_music":
            rows = [
                [("🌊 Моя волна", "a:ym:wave"), ("❤️ Мои треки", "a:ym:tracks")],
                [("📂 Плейлист", "i:music:pl"), ("🔎 Трек", "i:music:track")],
                [("▶️ YouTube URL", "i:music:youtube")],
                [("⏮ Назад", "a:music:prev"), ("⏭ Далее", "a:music:next")],
                [("⏸ Пауза", "a:music:pause"), ("▶️ Продолжить", "a:music:play")],
                [("🔀 Shuffle ON", "a:music:sh1"), ("🔀 Shuffle OFF", "a:music:sh0")],
                [("🔂 Повтор трека", "a:music:rpt"), ("🔁 Повтор плейлиста", "a:music:rpl")],
                [("🚫 Повтор OFF", "a:music:rp0")],
                [("👍 Лайк", "a:ym:like"), ("👎 Дизлайк", "a:ym:dislike")],
                [("📋 Очередь", "a:ym:queue"), ("🧹 Очистить", "a:ym:qclear")], *volume_rows,
                [("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")],
            ]
            latest_yandex = self._latest_media_by_source.get("yandex_music")
            latest_youtube = self._latest_media_by_source.get("youtube")
            # Keep the provider controls, but identify the latest local content.
            latest = max([event for event in (latest_yandex, latest_youtube) if event is not None],
                         key=lambda event: getattr(event, "created_at", 0), default=None)
            source = str(getattr(latest, "source", "yandex_music") or "yandex_music")
            source_label = "YouTube" if source == "youtube" else "Яндекс Музыка"
            self._panel(chat_id, message_id, "🎵 Музыка · " + source_label + self._media_history_suffix(source), rows, view="mm"); return
        msg = "🎵 Музыка\nМузыкальный сервис не выбран. Включи Spotify в Настройки → Медиа."
        if language == "ru": msg = "🎵 Музыка\nМузыкальный сервис не выбран. Включи Яндекс Музыку или Spotify в Настройки → Медиа."
        self._panel(chat_id, message_id, msg, [
            [("▶️ YouTube URL", "i:music:youtube")],
            [("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")],
        ], view="mm")

    def _show_youtube_menu(self, chat_id: str, message_id: int) -> None:
        # Transport remains background-first. Fullscreen/theater are explicit
        # visual-layout commands and use verified UI Automation on the exact
        # YouTube tab (HOTFIX105).
        self._panel(chat_id, message_id, "▶️ YouTube · управление", [
            [("🔎 Поиск видео", "i:yt:search")],
            [("⏸ Пауза", "a:yt:pause"), ("▶️ Продолжить", "a:yt:play")],
            [("🖥 На весь экран", "a:yt:fullscreen"), ("↔️ Растянутый", "a:yt:theater")],
            [("⏪ −15 с", "a:yt:back15"), ("+15 с ⏩", "a:yt:fwd15")],
            [("⏪ −30 с", "a:yt:back30"), ("+30 с ⏩", "a:yt:fwd30")],
            [("⏪ −1 мин", "a:yt:back60"), ("+1 мин ⏩", "a:yt:fwd60")],
            [("🕐 Перейти на время", "i:yt:seek")],
            [("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")],
        ], view="yt")

    def _show_youtube_quality_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "⚙️ YouTube · качество", [
            [("Авто", "a:ytq:auto"), ("360p", "a:ytq:360"), ("480p", "a:ytq:480")],
            [("720p", "a:ytq:720"), ("1080p", "a:ytq:1080")],
            [("1440p", "a:ytq:1440"), ("2160p", "a:ytq:2160")],
            [("⬅️ Назад", "ui:yt"), ("🏠 Домой", "ui:h")],
        ], view="ytq")

    def _show_youtube_speed_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "🎚 YouTube · скорость", [
            [("0.25×", "a:yts:0.25"), ("0.5×", "a:yts:0.5"), ("0.75×", "a:yts:0.75")],
            [("1×", "a:yts:1"), ("1.25×", "a:yts:1.25"), ("1.5×", "a:yts:1.5")],
            [("1.75×", "a:yts:1.75"), ("2×", "a:yts:2")],
            [("⬅️ Назад", "ui:yt"), ("🏠 Домой", "ui:h")],
        ], view="yts")

    def _show_browser_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "🌐 Браузер", [
            [("🔎 Google", "i:br:q"), ("🔗 Открыть URL", "i:br:url")],
            [("🔢 Вкладка №", "i:br:tabn"), ("📝 Вкладка по имени", "i:br:tabt")],
            [("➕ Новая вкладка", "a:br:new"), ("❌ Закрыть вкладку", "i:br:close")],
            [("🧹 Очистить историю", "ui:brclean:h"), ("💣 Полный снос данных", "ui:brclean:f")],
            [("⬅️ Назад", "ui:h")],
        ], view="br")

    def _show_apps_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "🧩 Приложения и игры", [
            [("📋 Активные приложения", "a:apps:active"), ("🔊 Громкость приложения", "ui:appvol:0")],
            [("🪟 Работа с окнами", "ui:win")],
            [("🚀 Запустить приложение", "i:app:open")],
            [("🛑 Закрыть приложение", "ui:close:app")],
            [("🎮 Запустить игру", "i:game:open")],
            [("⛔ Закрыть игру", "ui:close:game")],
            [("⬅️ Назад", "ui:h")],
        ], view="apps")

    def _show_app_volume_menu(self, chat_id: str, message_id: int, *, page: int = 0) -> None:
        try:
            tools = self._tools_for_direct_action("audio")
            result = tools.execute("audio", {"action": "list_sessions"})
            data = getattr(result, "data", None)
            sessions = list(data or []) if isinstance(data, list) else []
            self._app_volume_catalog_by_chat[str(chat_id)] = sessions
        except Exception as exc:
            self._panel(chat_id, message_id, f"🔊 Не удалось получить аудиосессии приложений: {exc}", [[("⬅️ Назад", "ui:apps")]], view="apps")
            return
        per = 8
        pages = max(1, (len(sessions) + per - 1) // per)
        page = max(0, min(pages - 1, int(page)))
        start = page * per
        rows: list[list[tuple[str, str]]] = []
        for idx in range(start, min(len(sessions), start + per)):
            row = sessions[idx]
            label = str(row.get("display", "") or row.get("process", "") or f"Приложение {idx+1}")
            volume = int(round(float(row.get("volume_percent", 0) or 0)))
            muted = " · mute" if bool(row.get("muted", False)) else ""
            rows.append([(f"🔊 {label} · {volume}%{muted}", f"a:appvol:{idx}")])
        nav: list[tuple[str, str]] = []
        if page > 0: nav.append(("◀️", f"ui:appvol:{page-1}"))
        if page + 1 < pages: nav.append(("▶️", f"ui:appvol:{page+1}"))
        if nav: rows.append(nav)
        rows.append([("⬅️ Назад", "ui:apps"), ("🏠 Домой", "ui:h")])
        suffix = f" · {page+1}/{pages}" if pages > 1 else ""
        title = f"🔊 Громкость приложения{suffix}\n" + ("Выбери приложение, затем введи громкость 0–100%." if sessions else "Сейчас нет активных пользовательских аудиосессий.")
        self._panel(chat_id, message_id, title, rows, view=f"appvol:{page}")

    def _scenario_items(self) -> list[dict[str, Any]]:
        from copy import deepcopy
        return [deepcopy(x) for x in (self.config.get("scenarios.items", []) or []) if isinstance(x, dict) and x.get("enabled", True) is True]

    def _show_scenarios_menu(self, chat_id: str, message_id: int, *, page: int = 0) -> None:
        items = self._scenario_items()
        per_page = 8
        pages = max(1, (len(items) + per_page - 1) // per_page)
        page = max(0, min(pages - 1, int(page)))
        start = page * per_page
        rows: list[list[tuple[str, str]]] = []
        for index in range(start, min(len(items), start + per_page)):
            raw = items[index]
            name = str(raw.get("name", "") or raw.get("phrase", "") or f"Сценарий {index + 1}").strip()
            label = name if len(name) <= 42 else name[:39] + "…"
            from jarvis.runtime.scenarios import scenario_fingerprint
            token = scenario_fingerprint(raw)[:32]
            rows.append([(f"▶️ {label}", f"a:sc:{index}:{token}")])
        nav: list[tuple[str, str]] = []
        if page > 0: nav.append(("◀️", f"ui:sc:{page-1}"))
        if page + 1 < pages: nav.append(("▶️", f"ui:sc:{page+1}"))
        if nav: rows.append(nav)
        rows.append([("⬅️ Назад", "ui:h")])
        suffix = f" · {page+1}/{pages}" if pages > 1 else ""
        from jarvis.ui.scenario_strings import st
        from jarvis.language import language_code
        title = f"🎬 Сценарии{suffix}\n" + (st("menu_hint", language_code(self.config)) if items else "В NEO пока нет сохранённых сценариев.")
        self._panel(chat_id, message_id, title, rows, view=f"sc:{page}")

    def _show_system_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "🖥 Система", [
            [("🔒 Заблокировать", "a:sys:lock"), ("🔓 Разблокировать", "a:sys:unlock")],
            [("💤 Сон", "a:sys:sleep"), ("🧊 Гибернация", "a:sys:hib")],
            [("🔄 Перезагрузка", "a:sys:restart"), ("⏻ Выключить", "a:sys:off")],
            [("⚡ План питания", "ui:power"), ("💡 Яркость", "ui:bright")],
            [("⬅️ Назад", "ui:h")],
        ], view="sys")

    def _show_power_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "⚡ План питания Windows", [
            [("⚖️ Баланс", "a:power:bal"), ("🚀 Производительность", "a:power:perf")],
            [("🔋 Энергосбережение", "a:power:save"), ("ℹ️ Текущий", "a:power:get")],
            [("⬅️ Назад", "ui:sys"), ("🏠 Домой", "ui:h")],
        ], view="power")

    def _show_network_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "📡 Сеть", [
            [("📶 Wi‑Fi ON", "a:wifi:1"), ("📵 Wi‑Fi OFF", "a:wifi:0")],
            [("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")],
        ], view="net")

    def _show_bluetooth_menu(self, chat_id: str, message_id: int, *, force: bool = False) -> None:
        # Main Bluetooth view contains paired products only. Windows service/GATT
        # children are filtered by bluetooth_devices.py and never reach Telegram.
        devices = list_bluetooth_devices(force=force)
        self._bluetooth_catalog_by_chat[str(chat_id)] = devices
        rows: list[list[tuple[str, str]]] = [
            [("🔵 Bluetooth ON", "a:bt:1"), ("⚫ Bluetooth OFF", "a:bt:0")],
            [("➕ Сопряжение", "ui:btpair")],
        ]
        for index, device in enumerate(devices[:10]):
            name = str(device.get("name", "Bluetooth device") or "Bluetooth device")
            connected = bool(device.get("connected", False))
            battery = device.get("battery")
            suffix = " · подключено" if connected else " · сопряжено"
            if isinstance(battery, int):
                suffix += f" · {battery}%"
            label = (("🔌 " if connected else "🔗 ") + name + suffix)[:56]
            rows.append([(label, f"a:btdev:{'off' if connected else 'on'}:{index}")])
        rows.append([("🔄 Обновить", "a:bt:refresh")])
        rows.append([("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")])
        hint = "Сопряжённые устройства. Нажми устройство, чтобы подключить/отключить."
        if not devices:
            hint = "Сопряжённых устройств не найдено. Для нового устройства открой «Сопряжение»."
        self._panel(chat_id, message_id, f"🔵 Bluetooth\n{hint}", rows, view="bt")

    def _show_bluetooth_pair_menu(self, chat_id: str, message_id: int, *, force: bool = False) -> None:
        devices = list_pairable_bluetooth_devices(force=force)
        self._bluetooth_pair_catalog_by_chat[str(chat_id)] = devices
        rows: list[list[tuple[str, str]]] = []
        for index, device in enumerate(devices[:12]):
            name = str(device.get("name", "Bluetooth device") or "Bluetooth device")
            label = ("➕ " + name)[:56]
            rows.append([(label, f"a:btpair:{index}")])
        if not devices:
            rows.append([("ℹ️ Устройства пока не найдены", "a:btpair:noop")])
        rows.append([("🔄 Обновить", "a:btpair:refresh")])
        rows.append([("⬅️ Назад", "ui:bt"), ("🏠 Домой", "ui:h")])
        self._panel(
            chat_id, message_id,
            "➕ Сопряжение Bluetooth\nПереведи устройство в режим pairing и выбери его из списка.",
            rows, view="btpair",
        )

    def _show_key_menu(self, chat_id: str, message_id: int) -> None:
        # Telegram-only surface: this helper is deliberately not registered as a
        # desktop/voice Agent tool. Callback payloads contain only fixed presets.
        self._panel(chat_id, message_id, "⌨️ Нажатие клавиши\nВыбери клавишу или введи своё значение. Для одновременной комбинации используй +, например Ctrl+Backspace+Enter.", [
            [("Ctrl", "a:key:ctrl"), ("Shift", "a:key:shift"), ("Alt", "a:key:alt"), ("Tab", "a:key:tab")],
            [("Caps Lock", "a:key:capslock"), ("Escape", "a:key:esc")],
            [("Backspace", "a:key:backspace"), ("Enter", "a:key:enter")],
            [("←", "a:key:left"), ("↑", "a:key:up"), ("↓", "a:key:down"), ("→", "a:key:right")],
            [("Fn", "a:key:fn"), ("Control", "a:key:control"), ("Правый Shift", "a:key:right_shift")],
            [("Пробел", "a:key:space"), ("Delete", "a:key:delete")],
            [("Page Up", "a:key:page_up"), ("Page Down", "a:key:page_down"), ("Win", "a:key:win")],
            [("✍️ Своё / текст / комбинация", "i:key:custom")],
            [("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")],
        ], view="key")

    def _show_brightness_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "💡 Яркость", [
            [("25%", "a:bright:25"), ("50%", "a:bright:50"), ("75%", "a:bright:75"), ("100%", "a:bright:100")],
            [("✍️ Другое", "i:bright:set")],
            [("🖥 Выключить дисплеи", "a:display:off")],
            [("⬅️ Назад", "ui:sys"), ("🏠 Домой", "ui:h")],
        ], view="bright")

    def _show_volume_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "🎧 Вывод Windows / наушники", [
            [("0%", "a:vol:0"), ("25%", "a:vol:25"), ("50%", "a:vol:50")],
            [("75%", "a:vol:75"), ("100%", "a:vol:100")],
            [("🔇 Mute", "a:mute:1"), ("🔊 Unmute", "a:mute:0")],
            [("🎧 Устройство вывода", "ui:outdev:0")],
            [("✍️ Другая громкость", "i:vol:set")],
            [("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")],
        ], view="vol")

    def _show_mouse_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id,
            "🖱 Мышь · DPI-профиль указателя Windows\nЭто эффективный профиль скорости Windows; аппаратный DPI сенсора мыши программно не меняется.", [
            [("400 DPI", "a:mouse:400"), ("800 DPI", "a:mouse:800"), ("1200 DPI", "a:mouse:1200")],
            [("1600 DPI", "a:mouse:1600"), ("2400 DPI", "a:mouse:2400"), ("3200 DPI", "a:mouse:3200")],
            [("ℹ️ Текущий уровень", "a:mouse:get"), ("✍️ Другое", "i:mouse:set")],
            [("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")],
        ], view="mouse")

    @staticmethod
    def _dpi_to_windows_speed(dpi: int) -> int:
        presets = {400:4, 800:8, 1200:10, 1600:12, 2400:16, 3200:20}
        dpi = max(200, min(6400, int(dpi or 800)))
        if dpi in presets: return presets[dpi]
        nearest = min(presets, key=lambda x: abs(x-dpi))
        return presets[nearest]

    def _show_microphone_menu(self, chat_id: str, message_id: int) -> None:
        self._panel(chat_id, message_id, "🎙 Системный микрофон Windows", [
            [("🔇 Выключить", "a:mic:mute"), ("🎙 Включить", "a:mic:unmute")],
            [("25%", "a:micvol:25"), ("50%", "a:micvol:50"), ("75%", "a:micvol:75"), ("100%", "a:micvol:100")],
            [("🎙 Микрофон по умолчанию", "ui:micdev:0")],
            [("ℹ️ Текущий уровень", "a:micvol:get"), ("✍️ Другая громкость", "i:mic:set")],
            [("⬅️ Назад", "ui:h"), ("🏠 Домой", "ui:h")],
        ], view="mic")

    def _audio_devices(self, chat_id: str, *, force: bool = False) -> list[dict[str, Any]]:
        key = str(chat_id)
        cached = self._audio_device_catalog_by_chat.get(key) or {}
        if not force and time.monotonic() - float(cached.get("at", 0.0) or 0.0) < 8.0:
            raw = cached.get("devices")
            if isinstance(raw, list):
                return [dict(x) for x in raw if isinstance(x, dict)]
        try:
            result = self.agent.tools.execute("audio", {"action": "list_devices"})
            raw = list(getattr(result, "data", None) or []) if bool(getattr(result, "success", False)) else []
            devices = [dict(x) for x in raw if isinstance(x, dict)]
        except Exception as exc:
            logger.debug("Telegram audio endpoint enumeration failed: %s", exc)
            devices = []
        self._audio_device_catalog_by_chat[key] = {"at": time.monotonic(), "devices": devices}
        return devices

    def _audio_menu_choice188(self, chat_id: str, token: str, index: int, revision: int) -> tuple[str, str] | None:
        """HIST-057: the device behind button ``index`` of menu ``revision`` — or None when stale."""
        kind = "input" if token == "i" else "output"
        snapshot = (self.__dict__.get("_audio_device_menus188") or {}).get((str(chat_id), token)) or {}
        shown = list(snapshot.get("items") or []) if snapshot.get("rev") == revision else []
        if not 0 <= index < len(shown) or not shown[index][0]:
            return None
        fresh = [x for x in self._audio_devices(chat_id, force=True) if str(x.get("type", "")).casefold() == kind]
        if shown[index][0] not in {str(x.get("device_id", "") or "") for x in fresh}:
            return None                       # the device is gone: ask again instead of picking a neighbour
        return shown[index][0], shown[index][1]

    def _show_audio_device_menu(
        self, chat_id: str, message_id: int, *, device_type: str, page: int = 0, force: bool = False
    ) -> None:
        kind = "input" if str(device_type).casefold() == "input" else "output"
        devices = [x for x in self._audio_devices(chat_id, force=force) if str(x.get("type", "")).casefold() == kind]
        page_size = 6
        pages = max(1, (len(devices) + page_size - 1) // page_size)
        page = max(0, min(int(page), pages - 1))
        start = page * page_size
        rows: list[list[tuple[str, str]]] = []
        token = "i" if kind == "input" else "o"
        # HIST-057: the buttons belong to THIS listing; a press resolves its stable
        # device ID from the same snapshot, never from a re-enumerated index.
        menus = self.__dict__.setdefault("_audio_device_menus188", {})
        revision = int(self.__dict__.get("_audio_device_menu_rev188", 0) or 0) + 1
        self._audio_device_menu_rev188 = revision
        menus[(str(chat_id), token)] = {"rev": revision,
                                        "items": [(str(x.get("device_id", "") or ""), str(x.get("name", "") or "")) for x in devices]}
        for index, item in enumerate(devices[start:start + page_size], start=start):
            name = str(item.get("name", "") or item.get("device_id", "") or f"Устройство {index + 1}").strip()
            label = name if len(name) <= 43 else name[:40] + "…"
            prefix = "✅ " if bool(item.get("is_default", False)) else ""
            rows.append([(prefix + label, f"a:adev:{token}:{index}:{revision}")])
        if not devices:
            rows.append([("🔄 Повторить поиск", f"a:adev:refresh:{token}:0")])
        else:
            nav: list[tuple[str, str]] = []
            view_prefix = "micdev" if kind == "input" else "outdev"
            if page > 0:
                nav.append(("◀️", f"ui:{view_prefix}:{page-1}"))
            if page + 1 < pages:
                nav.append(("▶️", f"ui:{view_prefix}:{page+1}"))
            if nav:
                rows.append(nav)
            rows.append([("🔄 Обновить список", f"a:adev:refresh:{token}:{page}")])
        back = "ui:mic" if kind == "input" else "ui:vol"
        rows.append([("⬅️ Назад", back), ("🏠 Домой", "ui:h")])
        title_kind = "🎙 Микрофон по умолчанию" if kind == "input" else "🎧 Устройство вывода по умолчанию"
        suffix = f" · {page+1}/{pages}" if pages > 1 else ""
        hint = "Нажми устройство — Windows сделает его системным по умолчанию."
        self._panel(chat_id, message_id, f"{title_kind}{suffix}\n{hint}", rows, view=f"{'micdev' if kind == 'input' else 'outdev'}:{page}")

    # ------------------------------------------------------------- text prompts
    def _prompt_input(
        self,
        chat_id: str,
        user_id: str,
        message_id: int,
        kind: str,
        prompt: str,
        *,
        payload: dict[str, Any] | None = None,
        return_view: str = "home",
    ) -> None:
        ttl = 180.0
        self._input = _TextInput(
            kind=kind,
            user_id=user_id,
            chat_id=chat_id,
            created_at=time.monotonic(),
            expires_at=time.monotonic() + ttl,
            panel_message_id=message_id,
            payload={**(payload or {}), "return_view": return_view},
        )
        back_data = "ui:h" if return_view == "home" else (f"ui:{return_view}" if return_view.startswith("sc:") else f"ui:{return_view}")
        self._panel(chat_id, message_id, prompt, [[("⬅️ Назад", back_data), ("❌ Отмена", "ui:cancel")]], view=return_view)

    def _critical_phrase_ready(self) -> bool:
        return bool(telegram_critical_phrase(self.config))

    def _request_secure_action(
        self,
        *,
        kind: str,
        chat_id: str,
        user_id: str,
        label: str,
        panel_message_id: int = 0,
        tool_name: str = "",
        arguments: dict[str, Any] | None = None,
        return_view: str = "home",
        confirmation_id: str = "",
    ) -> None:
        phrase = telegram_critical_phrase(self.config)
        if not phrase:
            if kind == "agent":
                try:
                    self.agent.confirm_pending(False, input_source="telegram", conversation_id=str(chat_id),
                        **({"confirmation_id": confirmation_id} if confirmation_id else {}))
                except Exception:
                    pass
            self._send_message(chat_id, self._tg_message("🔐 Критическое действие заблокировано: сначала задай кодовую фразу в NEO → Настройки → Удалённое подключение."))
            self._notify_desktop(f"📱 Telegram · критическое действие «{label}» отклонено: кодовая фраза не настроена.")
            if panel_message_id:
                self._roll_panel_to_bottom(chat_id, return_view, panel_message_id)
            return
        ttl = max(20.0, min(300.0, float(self.config.get("integrations.telegram.critical_phrase_ttl_seconds", 90) or 90)))
        pending = _SecurePending(
            nonce=uuid.uuid4().hex[:20],
            kind=kind,
            user_id=user_id,
            chat_id=chat_id,
            created_at=time.monotonic(),
            expires_at=time.monotonic() + ttl,
            label=label,
            panel_message_id=panel_message_id,
            tool_name=tool_name,
            arguments=dict(arguments or {}),
            return_view=str(return_view or "home"),
            confirmation_id=str(confirmation_id or ""),
        )
        with self._lock:
            self._secure_pending = pending
            self._input = _TextInput(
                kind="critical_phrase",
                user_id=user_id,
                chat_id=chat_id,
                created_at=time.monotonic(),
                expires_at=pending.expires_at,
                panel_message_id=panel_message_id,
                payload={"return_view": str(return_view or "home")},
            )
        self._notify_desktop(f"📱 Telegram · запрошено подтверждение критического действия: {label}.")
        prompt = (
            f"🔐 Критическое действие: {label}\n\n"
            "Введи кодовую фразу. Сообщение с кодовой фразой NEO удалит сразу после проверки."
        )
        if panel_message_id:
            self._panel(chat_id, panel_message_id, prompt, [[("❌ Отмена", "ui:cancel"), ("🏠 Домой", "ui:h")]], view=return_view)
        else:
            self._send_message(chat_id, prompt, reply_markup=self._kb([[("❌ Отмена", "ui:cancel"), ("🏠 Домой", "ui:h")]]))

    # -------------------------------------------------------------- tool helpers
    @staticmethod
    def _tool_lane_name(tool_name: str, arguments: dict[str, Any]) -> str:
        name = str(tool_name or "").strip().casefold()
        action = str((arguments or {}).get("action", "") or "").strip().casefold()
        if name in {"yandex_music", "spotify", "music"}:
            return "music"
        if name == "discord":
            return "discord"
        if name in {"email", "google_calendar", "telegram_messages", "weather", "search", "translate"}:
            return "api"
        if name == "browser" and action in {"search", "open_url"}:
            return "api"
        return "desktop"

    def _tool_lane_loop(self, lane: str) -> None:
        jobs = self._tool_lane_queues[lane]
        while not self._stop.is_set():
            try:
                job = jobs.get(timeout=0.35)
            except queue.Empty:
                continue
            if self._stop.is_set():
                jobs.task_done()
                continue
            try:
                if job.cancellation_generation is not None:
                    from jarvis.runtime.cancellation import cancellation_manager
                    from jarvis.runtime.conversation_journal import conversation_scope
                    from jarvis.tools.registry import suppress_execution_learning184
                    with cancellation_manager().bound_generation(job.cancellation_generation), conversation_scope("telegram:" + str(job.chat_id)), suppress_execution_learning184():
                        self._execute_tool_now_blocking(job)
                else:
                    self._execute_tool_now_blocking(job)
            except Exception:
                logger.exception("Telegram provider-lane job failed: lane=%s tool=%s", lane, job.tool_name)
            finally:
                jobs.task_done()

    def _queue_tool_job(self, job: _ToolJob) -> bool:
        if job.tool_name == "scenario" and job.cancellation_generation is None:
            from jarvis.runtime.cancellation import cancellation_manager
            job.cancellation_generation = cancellation_manager().reset()
        lane = self._tool_lane_name(job.tool_name, job.arguments)
        try:
            self._tool_lane_queues[lane].put_nowait(job)
            return True
        except queue.Full:
            self._send_message(job.chat_id, self._tg_runtime_text("Ошибка: очередь быстрых команд занята."))
            return False

    def _needs_phrase(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        try:
            risk = self.agent.tools.risk_for(tool_name, arguments)
        except Exception:
            return True
        if risk >= Risk.HIGH:
            return True
        return bool(self.config.get("integrations.telegram.confirm_medium_risk", False)) and risk == Risk.MEDIUM

    def _execute_tool(
        self,
        chat_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        label: str,
        *,
        panel_message_id: int = 0,
        return_view: str = "home",
    ) -> None:
        if tool_name == "scenario":
            from jarvis.runtime.scenario_authority184 import bind_scenario_arguments
            try:
                bind_scenario_arguments(self.config, arguments)
            except (ValueError, TypeError):
                self._send_message(chat_id, self._tg_runtime_text("Сценарий не найден."))
                return
        if self._needs_phrase(tool_name, arguments):
            self._request_secure_action(
                kind="tool", chat_id=chat_id, user_id=user_id, label=label,
                panel_message_id=panel_message_id, tool_name=tool_name,
                arguments=arguments, return_view=return_view,
            )
            return
        self._execute_tool_now(
            chat_id, tool_name, arguments, label, panel_message_id=panel_message_id,
            return_view=return_view,
        )

    def _execute_tool_now(
        self,
        chat_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        label: str,
        *,
        panel_message_id: int = 0,
        return_view: str = "home",
        recreate_panel: bool = True,
        quiet: bool = False,
        consume_agent_pending: bool = False,
    ) -> None:
        self._queue_tool_job(_ToolJob(
            chat_id=str(chat_id), tool_name=str(tool_name), arguments=dict(arguments or {}),
            label=str(label), panel_message_id=int(panel_message_id or 0),
            return_view=str(return_view or "home"), recreate_panel=bool(recreate_panel), quiet=bool(quiet),
            consume_agent_pending=bool(consume_agent_pending),
        ))

    def _execute_tool_now_blocking(self, job: _ToolJob) -> None:
        chat_id, tool_name, arguments, label = job.chat_id, job.tool_name, job.arguments, job.label
        panel_message_id, return_view = job.panel_message_id, job.return_view
        recreate_panel, quiet = job.recreate_panel, job.quiet
        media_source = {"yandex_music": "yandex_music", "music": "yandex_music", "discord": "discord", "spotify": "spotify"}.get(tool_name, "")
        if media_source:
            with self._lock:
                self._media_inflight_sources[media_source] = self._media_inflight_sources.get(media_source, 0) + 1
        try:
            authorization = None
            if tool_name == "scenario":
                from jarvis.runtime.cancellation import cancellation_manager
                cancellation_manager().raise_if_cancelled()
                if job.consume_agent_pending:
                    authorization = self.agent.consume_pending_call184(tool_name, arguments, input_source="telegram", conversation_id=str(chat_id), return_authorization=True)
                    if not authorization:
                        raise ValueError("Scenario confirmation changed or expired")
            if arguments.get("action") in {"next", "previous", "music_skip", "music_previous"} and media_source:
                self._send_message(chat_id, self._tg_text("Переключаю") + "…")
            selected_tools = self._tools_for_direct_action(tool_name)
            if tool_name in {"shelf", "downloads", "activity", "search", "game_locations", "mini_browser", "desktop_features"}:
                denial = self.policy.execution_guard(tool_name, arguments, selected_tools.risk_for(tool_name, arguments))
                if denial:
                    raise PermissionError(denial)
            from contextlib import nullcontext
            from jarvis.routing.ownership_firewall import ownership_claim_scope
            from jarvis.runtime.object_context157 import valid_guard
            scope = ownership_claim_scope(authorization.get("ownership_claim")) if authorization is not None else nullcontext()
            with scope:
                if authorization is not None and not valid_guard(authorization.get("context_guard157")):
                    raise ValueError("Scenario object context changed")
                result = selected_tools.execute(tool_name, arguments)
            if result.success and tool_name in {"discord", "yandex_music", "spotify", "music", "media"}:
                self._ack_media_result(getattr(result, "data", None))
            icon = "✅" if result.success else "⚠️"
            localized_result = self._tg_runtime_text(str(result.text or ""))
            if not quiet:
                self._send_message(chat_id, f"{icon} {localized_result}")
            content_source = result.data.get("media_source") if isinstance(result.data, dict) else ""
            display_label = str(content_source or label)
            self._notify_desktop(f"📱 Telegram · {display_label}: {result.text}")
        except Exception as exc:
            logger.exception("Telegram button tool failed: %s", label)
            self._send_message(chat_id, self._tg_runtime_text(f"Ошибка: {exc}"))
            self._notify_desktop(f"📱 Telegram · ошибка действия «{label}»: {exc}")
        finally:
            if media_source:
                with self._lock:
                    self._media_inflight_sources[media_source] = max(0, self._media_inflight_sources.get(media_source, 1) - 1)
        if recreate_panel and not self._stop.is_set():
            self._roll_panel_to_bottom(chat_id, return_view, panel_message_id)

    def _agent_job_loop(self) -> None:
        while not self._stop.is_set():
            try:
                chat_id, user_id, text, label, panel_message_id, return_view = self._agent_jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            if self._stop.is_set():
                self._agent_jobs.task_done()
                continue
            try:
                self._execute_agent_text(
                    chat_id, user_id, text, label=label,
                    panel_message_id=panel_message_id, return_view=return_view,
                )
            except Exception:
                logger.exception("Telegram queued Agent job failed")
            finally:
                self._agent_jobs.task_done()

    def _queue_agent_text(
        self, chat_id: str, user_id: str, text: str, *, label: str,
        panel_message_id: int = 0, return_view: str = "home",
    ) -> bool:
        job = (str(chat_id), str(user_id), str(text), str(label), int(panel_message_id or 0), str(return_view or "home"))
        try:
            self._agent_jobs.put_nowait(job)
            self._notify_desktop(TELEGRAM_SUBMISSION_MARKER)
            return True
        except queue.Full:
            self._send_message(chat_id, self._tg_message("Очередь команд Telegram заполнена. Дождись завершения текущих запросов и повтори команду."))
            return False

    def _execute_agent_text(
        self, chat_id: str, user_id: str, text: str, *, label: str = "Команда",
        panel_message_id: int = 0, return_view: str = "home"
    ) -> None:
        # Telegram cannot repaint a local bubble, so one response message is
        # edited at a conservative rate instead of sending a message per token.
        # HOTFIX120 deliberately does *not* send a blocking "…" placeholder
        # before Agent.handle_stream(): that Bot API round-trip used to delay every
        # local tool/LLM command. The first real chunk lazily creates the message.
        stream_message_id = 0
        stream_text = ""
        stream_last_edit = 0.0
        stream_last_chars = 0
        try:
            def edit_stream(value: str, *, force: bool = False) -> None:
                nonlocal stream_message_id, stream_last_edit, stream_last_chars
                visible = str(value or "")
                if not visible:
                    return
                now = time.monotonic()
                if not stream_message_id:
                    try:
                        ids = self._send_message(chat_id, visible[:3900])
                        stream_message_id = int(ids[0]) if ids else 0
                        if stream_message_id:
                            stream_last_edit = now
                            stream_last_chars = min(len(visible), 3900)
                    except Exception:
                        logger.debug("Could not create Telegram response stream", exc_info=True)
                    return
                if not force:
                    # First visible chunk is immediate; subsequent updates are
                    # capped to ~1.4 edits/s and require meaningful progress.
                    if stream_last_edit and now - stream_last_edit < 0.70:
                        return
                    if stream_last_chars and len(visible) - stream_last_chars < 18:
                        return
                preview = visible[:3900]
                try:
                    self._api(
                        "editMessageText",
                        {"chat_id": chat_id, "message_id": stream_message_id, "text": preview},
                        timeout=10.0,
                    )
                    stream_last_edit = now
                    stream_last_chars = len(visible)
                except Exception as exc:
                    if "not modified" not in str(exc).casefold():
                        logger.debug("Could not update Telegram response stream", exc_info=True)

            def on_chunk(chunk: str) -> None:
                nonlocal stream_text
                stream_text += str(chunk or "")
                edit_stream(stream_text)

            turn = self.agent.handle_stream(text, on_text=on_chunk, input_source="telegram", conversation_id=str(chat_id))
            final_text = str(turn.text or "Готово.")
            # Always reconcile the final message with AgentTurn.text so filtering,
            # localization or a last tiny delta cannot leave Telegram truncated.
            if stream_message_id:
                edit_stream(final_text, force=True)
                if len(final_text) > 3900:
                    self._send_message(chat_id, final_text[3900:])
            else:
                self._send_message(chat_id, final_text)
            if turn.pending is not None:
                self._request_secure_action(
                    kind="agent", chat_id=chat_id, user_id=user_id,
                    label=turn.pending.summary or label, panel_message_id=panel_message_id,
                    tool_name=turn.pending.tool_name, arguments=turn.pending.arguments,
                    confirmation_id=getattr(turn.pending, "confirmation_id", ""),
                    return_view=return_view,
                )
                return
            self._notify_desktop(telegram_dialogue_event(self.config, text, final_text))
        except Exception as exc:
            logger.exception("Telegram Agent command failed")
            error_text = self.agent.personalize_external_response(f"Ошибка выполнения команды: {exc}")
            if stream_message_id:
                try:
                    self._api(
                        "editMessageText",
                        {"chat_id": chat_id, "message_id": stream_message_id, "text": error_text[:3900]},
                        timeout=10.0,
                    )
                except Exception:
                    self._send_message(chat_id, error_text)
            else:
                self._send_message(chat_id, error_text)
            self._notify_desktop(telegram_dialogue_event(self.config, text, error_text))
        if not self._stop.is_set():
            self._roll_panel_to_bottom(chat_id, return_view, panel_message_id)

    def _power_action(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        return tool_name == "system" and str(arguments.get("action", "")).casefold() in {
            "sleep", "hibernate", "shutdown", "restart"
        }

    # -------------------------------------------------------------- message flow
    def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat_id = str(chat.get("id", "") or "")
        user_id = str(sender.get("id", "") or "")
        chat_type = str(chat.get("type", "") or "")
        text = str(message.get("text", "") or "").strip()
        message_id = int(message.get("message_id", 0) or 0)
        msg_ts = float(message.get("date", time.time()) or time.time())
        if chat_type.casefold() in {"group", "supergroup"}:
            row = self._message_center.record_bot_group(chat)
            if row:
                logger.debug("Telegram group observed title=%s chat_id=%s", row.get("title", ""), row.get("chat_id", ""))
            # Remote-control commands are private-owner-only by design. Group
            # updates are used solely to discover safe allow-listed send targets.
            return
        if not text and chat_id and user_id and message_id > 0 and chat_type.casefold() == "private":
            # NEO 188 NEO-TG-001: photos, voice notes, stickers and dice from the owner are
            # part of the control chat history too; record them so the cleanup can remove them.
            try:
                if self._is_authorized(user_id, chat_id, chat_type):
                    self._track_message(chat_id, message_id, timestamp=msg_ts, owned=False,
                                        kind="dice" if isinstance(message.get("dice"), dict) else "media")
            except Exception:
                logger.debug("Could not record a Telegram media message for cleanup", exc_info=True)
        if not chat_id or not user_id or not text:
            return

        remote_enabled = bool(self.config.get("integrations.telegram.remote_control_enabled", self.config.get("integrations.telegram.enabled", False)))
        if not remote_enabled:
            return

        if self._handle_pair(user_id, chat_id, chat_type, text, message_id):
            return

        command = text.split(maxsplit=1)[0].split("@", 1)[0].casefold()
        if command == "/id":
            self._track_message(chat_id, message_id, timestamp=msg_ts, owned=False)
            self._send_message(chat_id, self._tg_message(f"Telegram user ID: {user_id}\nChat ID: {chat_id}\nТип чата: {chat_type}"))
            return

        if not self._is_authorized(user_id, chat_id, chat_type):
            if chat_type.casefold() == "private":
                self._send_message(chat_id, self._tg_message("⛔ Этот Telegram-аккаунт не привязан к NEO."))
            return

        # Critical code phrases are deliberately never logged or mirrored. Delete
        # the Telegram message first, then compare locally.
        current_input = self._input
        if (
            current_input is not None
            and current_input.kind == "critical_phrase"
            and current_input.user_id == user_id
            and current_input.chat_id == chat_id
        ):
            self._delete_one(chat_id, message_id)
            self._handle_critical_phrase(chat_id, user_id, text)
            return

        self._track_message(chat_id, message_id, timestamp=msg_ts, owned=False)

        now = time.monotonic()
        min_interval = max(0.10, float(self.config.get("integrations.telegram.min_command_interval_seconds", 0.35) or 0.35))
        previous = self._rate.get(user_id, 0.0)
        if now - previous < min_interval:
            self._send_message(chat_id, self._tg_message("Слишком быстро. Повтори команду через секунду."))
            return
        self._rate[user_id] = now

        if command == "/start":
            self._input = None
            self._leave_explorer(chat_id)
            self._cleanup_chat(chat_id, recreate_home=True, aggressive=True)
            return
        if command == "/home":
            self._input = None
            self._leave_explorer(chat_id)
            self._drop_panel(chat_id)
            self._show_home(chat_id)
            return
        if command == "/help":
            self._send_message(chat_id, self._help_text())
            self._show_home(chat_id)
            return
        if command == "/status":
            self._drop_panel(chat_id)
            self._send_status(chat_id)
            self._show_info_menu(chat_id, 0)
            return
        if command == "/screen":
            self._drop_panel(chat_id)
            self._show_screen_menu(chat_id, 0)
            return
        if command == "/cancel":
            self._cancel_all(chat_id, user_id)
            self._show_home(chat_id)
            return
        if command == "/settings":
            self._send_message(chat_id, self._settings_text())
            self._show_home(chat_id)
            return
        if command.startswith("/"):
            self._send_message(chat_id, self._tg_message("Неизвестная команда. Используй кнопки или обычную фразу Phrase Atlas."))
            self._show_home(chat_id)
            return

        if self._input is not None and self._input.user_id == user_id and self._input.chat_id == chat_id:
            if time.monotonic() > self._input.expires_at:
                panel = self._input.panel_message_id
                self._input = None
                self._send_message(chat_id, self._tg_message("Время ввода истекло."))
                if panel:
                    self._roll_panel_to_bottom(chat_id, "home", panel)
                return
            state = self._input
            self._input = None
            self._handle_text_input(state, text)
            return

        max_chars = max(64, min(4000, int(self.config.get("integrations.telegram.max_command_chars", 1500) or 1500)))
        if len(text) > max_chars:
            self._send_message(chat_id, self._tg_message(f"Команда слишком длинная. Максимум: {max_chars} символов."))
            return

        logger.info("Telegram command user_id=%s chars=%s", user_id, len(text))
        if self._try_shared_media_phrase(chat_id, user_id, text):
            return
        self._queue_agent_text(
            chat_id, user_id, text, label=f"Команда «{text[:120]}»",
            panel_message_id=int(self._panel_by_chat.get(chat_id, 0) or 0),
            return_view=self._view_by_chat.get(chat_id, "home"),
        )
        # The polling thread returns immediately; the Agent worker recreates the
        # same view after a non-pending action.

    def _handle_text_input(self, state: _TextInput, text: str) -> None:
        chat_id, user_id, panel = state.chat_id, state.user_id, state.panel_message_id
        value = str(text or "").strip()
        return_view = str(state.payload.get("return_view", "home") or "home")
        if not value:
            self._send_message(chat_id, self._tg_message("Пустое значение не принято."))
            self._roll_panel_to_bottom(chat_id, return_view, panel)
            return
        try:
            if state.kind == "remote_setting":
                self._save_remote_setting(chat_id, panel, str(state.payload.get("setting_id", "")), value)
            elif state.kind.startswith("neo_"):
                actions = {
                    "neo_file": ("search", {"action": "files", "query": value, "limit": 20}),
                    "neo_game": ("game_locations", {"action": "find", "name": value, "kind": "all"}),
                    "neo_browser": ("mini_browser", {"action": "open", "url": value}),
                    "neo_note": ("shelf", {"action": "create", "kind": "note", "title": value.splitlines()[0][:80], "content": value}),
                }
                tool, arguments = actions[state.kind]
                self._execute_tool(chat_id, user_id, tool, arguments, value[:100], panel_message_id=panel, return_view="settings")
            elif state.kind == "ym_playlist":
                self._execute_tool_now(chat_id, "yandex_music", {"action": "playlist", "playlist": value}, f"Яндекс Музыка · плейлист {value}", panel_message_id=panel, return_view=return_view)
            elif state.kind == "ym_track":
                self._execute_tool_now(chat_id, "yandex_music", {"action": "play_track", "query": value}, f"Яндекс Музыка · трек {value}", panel_message_id=panel, return_view=return_view)
            elif state.kind == "music_playlist":
                self._execute_tool_now(chat_id, "music", {"action": "playlist", "playlist": value}, f"Музыка · плейлист {value}", panel_message_id=panel, return_view=return_view)
            elif state.kind == "music_track":
                self._execute_tool_now(chat_id, "music", {"action": "play_track", "query": value}, f"Музыка · трек {value}", panel_message_id=panel, return_view=return_view)
            elif state.kind == "music_youtube_url":
                url = extract_youtube_url(value)
                if not url:
                    self._send_message(chat_id, self._tg_message("Нужна прямая ссылка YouTube (youtube.com или youtu.be)."))
                    self._roll_panel_to_bottom(chat_id, return_view, panel)
                    return
                self._execute_tool_now(chat_id, "media", {"action":"play_search", "query":url, "target":"youtube"}, "YouTube · URL", panel_message_id=panel, return_view=return_view)
            elif state.kind == "youtube_search_title":
                payload = dict(state.payload); payload["title"] = value
                self._prompt_input(chat_id, user_id, panel, "youtube_search_author", "👤 Введи автора/название канала. Если автор не нужен — отправь «-».", payload=payload, return_view="yt")
            elif state.kind == "youtube_search_author":
                payload = dict(state.payload)
                author = "" if value.casefold() in {"-", "нет", "пропустить", "skip", "не знаю"} else value
                title = str(payload.get("title", "") or "").strip()
                self._execute_tool_now(chat_id, "media", {"action":"play_search", "title":title, "query":title, "author":author, "target":"youtube"}, f"YouTube · {title}" + (f" · {author}" if author else ""), panel_message_id=panel, return_view="yt")
            elif state.kind == "youtube_seek_time":
                text_n = value.casefold().replace("ё","е").replace(",", ".")
                seconds = 0.0
                if ":" in text_n:
                    parts = [float(x) for x in text_n.split(":")]
                    if len(parts) == 2: seconds = parts[0]*60 + parts[1]
                    elif len(parts) == 3: seconds = parts[0]*3600 + parts[1]*60 + parts[2]
                else:
                    h = re.search(r"(\d+(?:\.\d+)?)\s*(?:ч|час)", text_n)
                    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:м|мин)", text_n)
                    sec = re.search(r"(\d+(?:\.\d+)?)\s*(?:с|сек)", text_n)
                    if h or m or sec:
                        seconds = (float(h.group(1))*3600 if h else 0) + (float(m.group(1))*60 if m else 0) + (float(sec.group(1)) if sec else 0)
                    else:
                        seconds = float(text_n)
                self._execute_tool_now(chat_id, "media", {"action":"seek_to", "target":"youtube", "position_seconds":seconds, "seconds":seconds}, f"YouTube · перейти на {seconds:.0f} с", panel_message_id=panel, return_view="yt")
            elif state.kind == "dc_playlist":
                self._execute_tool_now(chat_id, "discord", {"action": "music_playlist", "playlist": value}, f"Discord · плейлист {value}", panel_message_id=panel, return_view=return_view)
            elif state.kind == "dc_track":
                self._execute_tool_now(chat_id, "discord", {"action": "music_bot_play", "query": value}, f"Discord · трек {value}", panel_message_id=panel, return_view=return_view)
            elif state.kind == "dc_youtube_url":
                url = extract_youtube_url(value)
                if not url:
                    self._send_message(chat_id, self._tg_message("Нужна прямая ссылка YouTube (youtube.com или youtu.be)."))
                    self._roll_panel_to_bottom(chat_id, return_view, panel)
                    return
                # The Discord adapter resolves the requester/guild and joins the
                # voice channel where the paired user is present.  No browser
                # ownership is involved in this direct Telegram tool path.
                self._execute_tool_now(chat_id, "discord", {"action":"music_youtube_url", "query":url, "play_now":True}, "Discord Music · YouTube", panel_message_id=panel, return_view=return_view)
            elif state.kind == "browser_search":
                self._execute_tool_now(chat_id, "browser", {"action": "search", "value": value}, f"Поиск Google · {value}", panel_message_id=panel, return_view=return_view)
            elif state.kind == "web_search":
                self._queue_agent_text(chat_id, user_id, f"загугли {value}", label=f"Поиск информации · {value}", panel_message_id=panel, return_view=return_view)
            elif state.kind == "browser_url":
                self._execute_tool_now(chat_id, "browser", {"action": "open_url", "value": value}, f"Открыть URL · {value}", panel_message_id=panel, return_view=return_view)
            elif state.kind in {"browser_tab_index", "browser_close_index"}:
                index = int(value)
                action = "activate_tab" if state.kind == "browser_tab_index" else "close_tab"
                self._execute_tool_now(chat_id, "browser", {"action": action, "index": index}, f"Браузер · вкладка {index}", panel_message_id=panel, return_view=return_view)
            elif state.kind == "browser_tab_title":
                self._execute_tool_now(chat_id, "browser", {"action": "activate_tab_title", "value": value}, f"Браузер · вкладка {value}", panel_message_id=panel, return_view=return_view)
            elif state.kind in {"app_open", "app_close", "game_open", "game_close"}:
                is_game = state.kind.startswith("game_")
                action = "open" if state.kind.endswith("_open") else "close"
                args = {"action": action, "name": value, "kind": "game" if is_game else "app"}
                self._execute_tool(chat_id, user_id, "application", args, f"{'Игра' if is_game else 'Приложение'} · {action} · {value}", panel_message_id=panel, return_view=return_view)
            elif state.kind == "close_other":
                kind = str(state.payload.get("kind", "app") or "app")
                self._show_close_confirmation(chat_id, panel, kind, value, value)
            elif state.kind in {"overlay_check_add", "overlay_check_mark", "overlay_check_delete", "overlay_note_set", "overlay_note_append", "overlay_rename"}:
                category = str(state.payload.get("category", "notes") or "notes")
                name = self._overlay_panel_name(category)
                if state.kind == "overlay_check_add":
                    args = {"action":"checklist_add", "name":name, "item":value, "purpose":"checklist"}
                elif state.kind == "overlay_check_mark":
                    args = {"action":"checklist_check", "name":name, "item":value}
                elif state.kind == "overlay_check_delete":
                    args = {"action":"checklist_remove", "name":name, "item":value}
                    self._show_plain_confirmation(
                        chat_id, panel, self._tg_message("Удалить пункт чек-листа?") + f"\n{value}",
                        {"mode":"direct_tool", "tool_name":"overlay", "arguments":args, "label":"Панель · удалить пункт", "return_view":f"ov:{category}"},
                        yes_label="✅ Удалить", no_label="❌ Не удалять", view=f"ov:{category}",
                    )
                    return
                elif state.kind == "overlay_note_set":
                    args = {"action":"set_text", "name":name, "text":value, "purpose":"notes", "kind":"text"}
                elif state.kind == "overlay_note_append":
                    args = {"action":"append_text", "name":name, "text":value, "purpose":"notes", "kind":"text"}
                else:
                    args = {"action":"rename", "name":name, "new_name":value}
                self._execute_tool_now(chat_id, "overlay", args, "Панель · изменить", panel_message_id=panel, return_view=f"ov:{category}")
            elif state.kind == "volume_set":
                level = max(0, min(100, int(float(value.replace(",", ".")))))
                self._execute_tool_now(chat_id, "audio", {"action": "set_volume", "value": level}, f"Громкость {level}%", panel_message_id=panel, return_view=return_view)
            elif state.kind == "music_volume_set":
                level = max(0, min(100, int(float(value.replace(",", ".")))))
                self._execute_tool_now(chat_id, "music", {"action": "set_volume", "volume": level}, f"Громкость музыки {level}%", panel_message_id=panel, return_view="mm", quiet=True)
            elif state.kind == "timer_create":
                self._execute_tool_now(chat_id, "timer", {"action": "timer", "text": f"таймер на {value}"}, "Создать таймер", panel_message_id=panel, return_view="timer", quiet=True)
            elif state.kind == "app_volume_set":
                level = max(0, min(100, int(float(value.replace(",", ".")))))
                target = str(state.payload.get("target", "") or "").strip()
                if not target:
                    raise ValueError("Не выбрано приложение")
                self._execute_tool_now(chat_id, "audio", {"action": "set_volume", "target": target, "value": level}, f"Громкость {target} · {level}%", panel_message_id=panel, return_view=return_view)
            elif state.kind == "brightness_set":
                level = max(0, min(100, int(float(value.replace(",", ".")))))
                self._execute_tool(chat_id, user_id, "system", {"action": "set_brightness", "value": level}, f"Яркость {level}%", panel_message_id=panel, return_view=return_view)
            elif state.kind == "screen_move_window":
                self._screen_move_target_by_chat[chat_id] = value
                self._show_move_monitor_menu(chat_id, panel, value)
            elif state.kind == "screen_minimize_window":
                self._execute_tool_now(chat_id, "window", {"action": "minimize", "title": value, "kind": "any"}, f"Свернуть окно · {value}", panel_message_id=panel, return_view=return_view)
            elif state.kind == "mouse_sensitivity_set":
                level = max(1, min(20, int(float(value.replace(",", ".")))))
                self._execute_tool(chat_id, user_id, "system", {"action": "set_mouse_sensitivity", "value": level}, f"Мышь · {level}/20", panel_message_id=panel, return_view=return_view)
            elif state.kind == "microphone_volume_set":
                level = max(0, min(100, int(float(value.replace(",", ".")))))
                self._execute_tool(chat_id, user_id, "audio", {"action": "set_input_volume", "value": level}, f"Микрофон · {level}%", panel_message_id=panel, return_view=return_view)
            elif state.kind == "mail_send_to":
                payload=dict(state.payload); payload["to"]=value
                self._prompt_input(chat_id,user_id,panel,"mail_send_subject","✉️ Введи тему письма.",payload=payload,return_view=return_view)
            elif state.kind == "mail_send_subject":
                payload=dict(state.payload); payload["subject"]=value
                self._prompt_input(chat_id,user_id,panel,"mail_send_body","✉️ Введи содержимое письма.",payload=payload,return_view=return_view)
            elif state.kind == "mail_send_body":
                payload=dict(state.payload)
                self._execute_tool(chat_id,user_id,"email",{"action":"send","provider":str(payload.get("provider","auto")),"to":str(payload.get("to","")),"subject":str(payload.get("subject","")),"body":value},"Отправка письма",panel_message_id=panel,return_view=return_view)
            elif state.kind == "discord_text_body":
                self._send_message(chat_id, self._tg_message("ℹ️ Эта команда удалена: официальный Discord API не разрешает NEO автоматизировать обычный пользовательский аккаунт. Музыкальный NeoBot продолжает работать отдельно."))
                self._show_discord_media(chat_id, panel)
            elif state.kind in {"remote_mouse_xy", "remote_mouse_x", "remote_mouse_y"}:
                payload = dict(state.payload)
                monitor = int(payload.get("monitor", 0) or 0)
                button = str(payload.get("button", "left") or "left")
                match = re.fullmatch(r"\s*(-?\d+)\s+(-?\d+)\s*", value)
                if not match:
                    self._prompt_input(
                        chat_id, user_id, panel, "remote_mouse_xy",
                        "🖱 Введи две координаты одной строкой: X Y. Например: 1250 640",
                        payload={"monitor": monitor, "button": button}, return_view="rmouse",
                    )
                    return
                x, y = int(match.group(1)), int(match.group(2))
                click_global(monitor, x, y, button)
                # Give the clicked application a moment to repaint before the
                # result screenshot is captured.
                time.sleep(0.18)
                label = "левой" if button == "left" else "правой"
                self._notify_desktop(f"📱 Telegram · удалённый клик {button}: X={x}, Y={y}, экран {monitor}")
                # Show the real post-click result as a fresh screenshot. The
                # canonical control panel remains a single edited message.
                path, _bounds = capture_coordinate_grid(self.config, monitor)
                self._send_photo(
                    chat_id, path,
                    f"✅ Клик {label} кнопкой: X={x}, Y={y} · экран {monitor}\nРезультат после нажатия",
                )
                self._show_remote_mouse_menu(chat_id, panel)
            elif state.kind == "calendar_add_date":
                payload=dict(state.payload); payload["date"]=value
                self._prompt_input(chat_id,user_id,panel,"calendar_add_time","🕐 Введи время, например 18:30.",payload=payload,return_view=return_view)
            elif state.kind == "calendar_add_time":
                payload=dict(state.payload); payload["time"]=value
                self._prompt_input(chat_id,user_id,panel,"calendar_add_title","📅 Введи название события.",payload=payload,return_view=return_view)
            elif state.kind == "calendar_add_title":
                payload=dict(state.payload)
                self._execute_tool(chat_id,user_id,"google_calendar",{"action":"add","title":value,"date":str(payload.get("date","")),"time":str(payload.get("time","")),"duration_minutes":60},"Добавление события",panel_message_id=panel,return_view=return_view)
            elif state.kind == "calendar_edit_title":
                payload = dict(state.payload)
                payload["new_title"] = "" if value == "-" else value
                self._prompt_input(
                    chat_id, user_id, panel, "calendar_edit_date",
                    f"📅 Новая дата. Сейчас: {payload.get('old_date') or 'не указана'}. Введи дату или - чтобы оставить без изменений.",
                    payload=payload, return_view="cal",
                )
            elif state.kind == "calendar_edit_date":
                payload = dict(state.payload)
                payload["new_date"] = "" if value == "-" else value
                self._prompt_input(
                    chat_id, user_id, panel, "calendar_edit_time",
                    f"🕐 Новое время. Сейчас: {payload.get('old_time') or 'не указано'}. Введи время или - чтобы оставить без изменений.",
                    payload=payload, return_view="cal",
                )
            elif state.kind == "calendar_edit_time":
                payload = dict(state.payload)
                new_time = "" if value == "-" else value
                self._execute_tool(
                    chat_id, user_id, "google_calendar",
                    {
                        "action": "update",
                        "event_id": str(payload.get("event_id", "") or ""),
                        "title": str(payload.get("new_title", "") or ""),
                        "date": str(payload.get("new_date", "") or ""),
                        "time": new_time,
                    },
                    "Редактирование события", panel_message_id=panel, return_view="cal",
                )
            elif state.kind == "remote_key":
                ok, result_text = execute_remote_key_input(value)
                self._send_message(chat_id, ("✅ " if ok else "⚠️ ") + self._tg_runtime_text(result_text))
                self._notify_desktop(f"📱 Telegram · клавиатура: {result_text}")
                self._roll_panel_to_bottom(chat_id, return_view, panel)
            else:
                self._send_message(chat_id, self._tg_message("Неизвестный режим ввода."))
                self._roll_panel_to_bottom(chat_id, return_view, panel)
        except Exception as exc:
            self._send_message(chat_id, self._tg_message(f"Не удалось обработать значение: {exc}"))
            self._roll_panel_to_bottom(chat_id, return_view, panel)

    # ------------------------------------------------------------- code phrase
    def _discard_agent_confirmation184(self, pending, chat_id: str) -> None:
        if pending.tool_name == "scenario":
            self.agent.consume_pending_call184("scenario", pending.arguments, input_source="telegram", conversation_id=str(chat_id))
        else:
            self.agent.confirm_pending(False, input_source="telegram", conversation_id=str(chat_id),
                **({"confirmation_id": pending.confirmation_id} if getattr(pending, "confirmation_id", "") else {}))

    def _handle_critical_phrase(self, chat_id: str, user_id: str, supplied: str) -> None:
        pending = self._secure_pending
        self._input = None
        if pending is None or pending.user_id != user_id or pending.chat_id != chat_id:
            self._send_message(chat_id, self._tg_message("Нет критического действия, ожидающего кодовую фразу."))
            return
        if time.monotonic() > pending.expires_at:
            self._secure_pending = None
            if pending.kind == "agent":
                try:
                    self._discard_agent_confirmation184(pending, chat_id)
                except Exception:
                    pass
            self._send_message(chat_id, self._tg_message("Время подтверждения истекло."))
            self._notify_desktop(f"📱 Telegram · подтверждение «{pending.label}» истекло.")
            self._roll_panel_to_bottom(chat_id, pending.return_view, pending.panel_message_id)
            return

        count, lockout_until = self._auth_failures.get(user_id, (0, 0.0))
        if time.monotonic() < lockout_until:
            wait = max(1, int(lockout_until - time.monotonic()))
            self._send_message(chat_id, self._tg_message(f"🔒 Проверка временно заблокирована. Повтори через {wait} сек."))
            return

        expected = telegram_critical_phrase(self.config)
        # hmac.compare_digest(str, str) only accepts ASCII text. A Telegram
        # critical phrase is explicitly allowed to be Russian/Unicode, so compare
        # normalized UTF-8 bytes instead. This also avoids logging or echoing the
        # secret while preserving constant-time comparison semantics.
        supplied_bytes = unicodedata.normalize("NFKC", str(supplied or "")).strip().encode("utf-8")
        expected_bytes = unicodedata.normalize("NFKC", str(expected or "")).strip().encode("utf-8")
        ok = bool(expected_bytes) and hmac.compare_digest(supplied_bytes, expected_bytes)
        if not ok:
            count += 1
            max_attempts = max(1, min(10, int(self.config.get("integrations.telegram.critical_phrase_max_attempts", 3) or 3)))
            if count >= max_attempts:
                lock_seconds = max(15, min(600, int(self.config.get("integrations.telegram.critical_phrase_lockout_seconds", 60) or 60)))
                self._auth_failures[user_id] = (0, time.monotonic() + lock_seconds)
                self._secure_pending = None
                if pending.kind == "agent":
                    try:
                        self._discard_agent_confirmation184(pending, chat_id)
                    except Exception:
                        pass
                self._send_message(chat_id, self._tg_message(f"⛔ Кодовая фраза неверна. Действие отменено, новые попытки заблокированы на {lock_seconds} сек."))
                self._notify_desktop(f"📱 Telegram · критическое действие «{pending.label}» отменено после неверной кодовой фразы.")
                self._roll_panel_to_bottom(chat_id, pending.return_view, pending.panel_message_id)
                return
            self._auth_failures[user_id] = (count, 0.0)
            self._secure_pending = pending
            self._input = _TextInput(
                kind="critical_phrase", user_id=user_id, chat_id=chat_id,
                created_at=time.monotonic(), expires_at=pending.expires_at,
                panel_message_id=pending.panel_message_id,
            )
            self._send_message(chat_id, self._tg_message(f"❌ Неверная кодовая фраза. Осталось попыток: {max_attempts - count}."))
            return

        self._auth_failures.pop(user_id, None)
        self._secure_pending = None
        self._notify_desktop(f"📱 Telegram · критическое действие подтверждено: {pending.label}.")

        # Clean the remote chat immediately before suspend/hibernate/restart.
        # The phrase message itself has already been deleted. For a power action
        # we deliberately do not send a post-action Telegram message or recreate
        # the home panel, otherwise the supposedly-cleared chat would get a new
        # trace just before Windows leaves the interactive session.
        is_power_action = pending.kind in {"tool", "agent"} and self._power_action(
            pending.tool_name, pending.arguments
        )
        if is_power_action:
            try:
                self._send_message(chat_id, self._tg_message("✅ Подтверждено. Очищаю чат и выполняю системное действие…"))
                self._cleanup_chat(chat_id, recreate_home=False, aggressive=True)
            except Exception:
                logger.debug("Pre-power Telegram cleanup failed", exc_info=True)

        try:
            if pending.kind in {"tool", "agent"} and pending.tool_name == "scenario":
                # Consume the Agent pending slot without doing its long work on
                # the Telegram polling thread, then enqueue the exact approved call.
                self._execute_tool_now(chat_id, pending.tool_name, dict(pending.arguments), pending.label,
                                       panel_message_id=pending.panel_message_id, return_view=pending.return_view,
                                       consume_agent_pending=(pending.kind == "agent"))
                return
            if pending.kind == "unlock":
                from jarvis.windows_voice_unlock import attempt_voice_unlock
                result = attempt_voice_unlock(self.config)
                success = bool((result or {}).get("ok", False))
                text = str((result or {}).get("status", "") or (result or {}).get("message", "") or ("Компьютер разблокирован." if success else "Разблокировка не выполнена."))
                self._send_message(chat_id, ("✅ " if success else "⚠️ ") + self._tg_runtime_text(text))
                self._notify_desktop(f"📱 Telegram · разблокировка: {text}")
            elif pending.kind == "agent":
                turn = self.agent.confirm_pending(True, input_source="telegram", conversation_id=str(chat_id),
                    **({"confirmation_id": pending.confirmation_id} if getattr(pending, "confirmation_id", "") else {}))
                if not is_power_action:
                    self._send_message(chat_id, turn.text)
                self._notify_desktop(f"📱 Telegram · {pending.label}: {turn.text}")
            elif pending.kind == "explorer":
                self._explorer_by_chat[str(chat_id)] = {"authorized": True, "current_path": "", "drives": [], "folders": [], "page": 0}
                self._show_explorer_drives(chat_id, pending.panel_message_id)
            else:
                result = self.agent.tools.execute(pending.tool_name, pending.arguments)
                if not is_power_action:
                    self._send_message(chat_id, ("✅ " if result.success else "⚠️ ") + self._tg_runtime_text(str(result.text or "")))
                self._notify_desktop(f"📱 Telegram · {pending.label}: {result.text}")
        except Exception as exc:
            logger.exception("Telegram critical action failed")
            if not is_power_action:
                self._send_message(chat_id, self._tg_message(f"Ошибка критического действия: {exc}"))
            self._notify_desktop(f"📱 Telegram · ошибка критического действия «{pending.label}»: {exc}")
        if not is_power_action and pending.kind != "explorer" and not self._stop.is_set():
            self._roll_panel_to_bottom(chat_id, pending.return_view, pending.panel_message_id)

    # ---------------------------------------------------------------- callbacks
    def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id", "") or "")
        sender = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        user_id = str(sender.get("id", "") or "")
        chat_id = str(chat.get("id", "") or "")
        chat_type = str(chat.get("type", "") or "")
        data = str(callback.get("data", "") or "")
        message_id = int(message.get("message_id", 0) or 0)
        if message_id:
            self._track_message(chat_id, message_id, timestamp=float(message.get("date", time.time()) or time.time()))

        if not self._is_authorized(user_id, chat_id, chat_type):
            self._answer_callback(callback_id, self._tg_message("Доступ запрещён"))
            return
        self._answer_callback(callback_id)

        if callback_id:
            with self._lock:
                if callback_id in self._seen_callback_ids:
                    return
                self._seen_callback_ids[callback_id] = time.monotonic()
                if len(self._seen_callback_ids) > 512:
                    self._seen_callback_ids.pop(next(iter(self._seen_callback_ids)))

        if data.startswith("ui:ncfg:"):
            parts = data.split(":")
            if len(parts) == 4 and parts[3].isdigit():
                self._show_remote_settings(chat_id, message_id, parts[2], int(parts[3]))
            return
        if data.startswith("ui:ncfgitem:"):
            self._show_remote_setting(chat_id, user_id, message_id, data.split(":", 2)[2])
            return
        if data.startswith("a:ncfg:"):
            parts = data.split(":")
            if len(parts) == 4:
                self._save_remote_setting(chat_id, message_id, parts[2], parts[3])
            return
        if data.startswith("ui:neoset:"):
            section = data.rsplit(":", 1)[1]
            if section in {"advanced", "window", "robot", "shelf", "privacy"}:
                self._show_remote_settings(chat_id, message_id, section)
            elif section == "downloads":
                self._execute_tool_now(chat_id, "downloads", {"action": "status"}, "Smart Downloads", panel_message_id=message_id, return_view="settings")
            return
        if data in {"a:neo:shelf_list", "a:neo:activity"}:
            tool = "shelf" if data.endswith("shelf_list") else "activity"
            self._execute_tool_now(chat_id, tool, {"action": "list"}, tool, panel_message_id=message_id, return_view="settings")
            return
        if data.startswith("i:neo:"):
            kind = data.rsplit(":", 1)[1]
            if kind in {"file", "game", "browser", "note"}:
                labels = {"file": "Найти файл", "game": "Данные игры", "browser": "Мини-браузер", "note": "Новая заметка"}
                self._prompt_input(chat_id, user_id, message_id, "neo_" + kind, self._tg_text(labels[kind]) + ":", payload={}, return_view="settings")
            return

        if data == "a:match:accept":
            # The owner pressed «Accept» under a match-found notice.
            self._execute_tool_now(chat_id, "match", {"action": "accept"}, "Match", panel_message_id=0)
            return
        if data == "a:plainconfirm:no":
            self._resolve_plain_confirmation(chat_id, user_id, message_id, accepted=False); return
        if data == "a:plainconfirm:yes":
            self._resolve_plain_confirmation(chat_id, user_id, message_id, accepted=True); return

        if data == "ui:cancel":
            self._cancel_all(chat_id, user_id, panel_message_id=message_id)
            return
        if data == "ui:h":
            self._input = None
            self._plain_confirm_by_chat.pop(str(chat_id), None)
            self._cancel_secure_silently(user_id, chat_id)
            self._leave_explorer(chat_id)
            self._show_home(chat_id, message_id); return
        if data == "ui:explorer":
            # Every fresh entry from Home requires the critical phrase; once inside,
            # directory navigation/open actions stay authorized until Home is used.
            self._leave_explorer(chat_id)
            self._request_secure_action(kind="explorer", chat_id=chat_id, user_id=user_id, label="Открыть Проводник", panel_message_id=message_id, return_view="home")
            return
        if data == "ui:ovsettings": self._show_overlay_settings_hub(chat_id, message_id); return
        if data.startswith("ui:ovcfg:"):
            self._show_overlay_style_menu(chat_id, message_id, data.split(":",2)[2]); return
        if data.startswith("ui:ovpreset:"):
            try: _u,_tag,category,page_s=data.split(":",3); page=int(page_s)
            except Exception: category,page="notes",0
            self._show_overlay_preset_picker(chat_id,message_id,category,page); return
        if data.startswith("ui:ovfontfam:"):
            try: _u,_tag,category,page_s=data.split(":",3); page=int(page_s)
            except Exception: category,page="notes",0
            self._show_overlay_font_picker(chat_id,message_id,category,page); return
        if data.startswith("ui:ovsize:"):
            self._show_overlay_font_size_picker(chat_id,message_id,data.split(":",2)[2]); return
        if data.startswith("ui:ovopacity:"):
            self._show_overlay_opacity_picker(chat_id,message_id,data.split(":",2)[2]); return
        if data.startswith("ui:exp:page:"):
            try: page=int(data.rsplit(":",1)[1])
            except Exception: page=0
            self._show_explorer_folder(chat_id,message_id,page=page); return
        if data == "ui:settings": self._show_settings_menu(chat_id, message_id); return
        if data == "ui:brief": self._show_daily_briefing_menu(chat_id, message_id); return
        if data == "ui:timer": self._show_timer_menu(chat_id, message_id); return
        if data == "ui:screen": self._show_screen_menu(chat_id, message_id); return
        if data == "ui:win": self._show_windows_menu(chat_id, message_id); return
        if data == "ui:shot": self._show_screenshot_menu(chat_id, message_id); return
        if data == "ui:panels": self._show_panels_menu(chat_id, message_id); return
        if data.startswith("ui:ov:"):
            self._show_overlay_menu(chat_id, message_id, data.split(":",2)[2]); return
        if data.startswith("ui:ovmon:"):
            self._show_overlay_monitor_menu(chat_id, message_id, data.split(":",2)[2]); return
        if data.startswith("ui:ovpos:"):
            self._show_overlay_position_menu(chat_id, message_id, data.split(":",2)[2]); return
        if data == "ui:brclean:h": self._show_browser_cleanup_picker(chat_id, message_id, "h"); return
        if data == "ui:brclean:f": self._show_browser_cleanup_picker(chat_id, message_id, "f"); return
        if data.startswith("ui:close:"):
            self._show_close_picker(chat_id, message_id, data.rsplit(":",1)[1]); return
        if data == "ui:info": self._show_system_menu(chat_id, message_id); return  # legacy panel
        if data == "ui:weather":
            self._execute_tool_now(chat_id, "weather", {"action": "summary"}, "Погода", panel_message_id=message_id, return_view="weather")
            return
        if data == "ui:proc": self._show_process_menu(chat_id, message_id); return
        if data == "ui:med": self._show_media_menu(chat_id, message_id); return
        if data == "ui:md": self._show_discord_media(chat_id, message_id); return
        if data == "ui:mc": self._show_music_menu(chat_id, message_id); return
        if data == "ui:mm": self._show_music_menu(chat_id, message_id); return
        if data in {"ui:mv", "ui:yt"}: self._show_youtube_menu(chat_id, message_id); return
        if data == "ui:ytq": self._show_youtube_quality_menu(chat_id, message_id); return
        if data == "ui:yts": self._show_youtube_speed_menu(chat_id, message_id); return
        if data == "ui:br": self._show_browser_menu(chat_id, message_id); return
        if data == "ui:apps": self._show_apps_menu(chat_id, message_id); return
        if data.startswith("ui:appvol:"):
            try: page = int(data.rsplit(":", 1)[1])
            except Exception: page = 0
            self._show_app_volume_menu(chat_id, message_id, page=page); return
        # Telegram Messages intentionally has no Home-menu button in HOTFIX96;
        # stale callbacks from an older panel are still accepted for compatibility.
        if data == "ui:tgmsg": self._show_telegram_messages_menu(chat_id, message_id); return
        if data == "ui:mail": self._show_mail_menu(chat_id, message_id); return
        if data.startswith("ui:mail:"): self._show_mail_provider_menu(chat_id, message_id, data.split(":",2)[2]); return
        if data == "ui:rmouse": self._show_remote_mouse_menu(chat_id, message_id); return
        if data == "ui:cal": self._show_calendar_menu(chat_id, message_id); return
        if data.startswith("ui:caldel:"):
            try: page = int(data.rsplit(":", 1)[1])
            except Exception: page = 0
            self._show_calendar_event_picker(chat_id, message_id, "del", page); return
        if data.startswith("ui:caledit:"):
            try: page = int(data.rsplit(":", 1)[1])
            except Exception: page = 0
            self._show_calendar_event_picker(chat_id, message_id, "edit", page); return
        if data in {"ui:dcvoice", "ui:dctext"}:
            self._send_message(chat_id, self._tg_message("ℹ️ Управление обычным Discord user-аккаунтом удалено. Discord запрещает self-bot/user-bot автоматизацию; NEO использует только официальный NeoBot для музыки."))
            self._show_discord_media(chat_id, message_id); return
        if data.startswith("ui:sc:"):
            try: page = int(data.rsplit(":", 1)[1])
            except Exception: page = 0
            self._show_scenarios_menu(chat_id, message_id, page=page); return
        if data == "ui:sys": self._show_system_menu(chat_id, message_id); return
        if data == "ui:net": self._show_network_menu(chat_id, message_id); return
        if data == "ui:bt": self._show_bluetooth_menu(chat_id, message_id); return
        if data == "ui:btpair": self._show_bluetooth_pair_menu(chat_id, message_id); return
        if data == "ui:key": self._show_key_menu(chat_id, message_id); return
        if data == "ui:bright": self._show_brightness_menu(chat_id, message_id); return
        if data == "ui:power": self._show_power_menu(chat_id, message_id); return
        if data == "ui:vol": self._show_volume_menu(chat_id, message_id); return
        if data == "ui:mouse": self._show_mouse_menu(chat_id, message_id); return
        if data == "ui:mic": self._show_microphone_menu(chat_id, message_id); return
        if data.startswith("ui:micdev:"):
            try: page = int(data.rsplit(":", 1)[1])
            except Exception: page = 0
            self._show_audio_device_menu(chat_id, message_id, device_type="input", page=page); return
        if data.startswith("ui:outdev:"):
            try: page = int(data.rsplit(":", 1)[1])
            except Exception: page = 0
            self._show_audio_device_menu(chat_id, message_id, device_type="output", page=page); return
        if data == "ui:stat":
            self._send_status(chat_id)
            self._roll_panel_to_bottom(chat_id, "sys", message_id); return

        if data == "a:apps:active":
            self._execute_tool_now(chat_id, "application", {"action": "list_active"}, "Активные приложения", panel_message_id=message_id, return_view="apps"); return
        if data.startswith("a:appvol:"):
            try: idx = int(data.rsplit(":", 1)[1])
            except Exception: idx = -1
            rows = self._app_volume_catalog_by_chat.get(str(chat_id), [])
            if 0 <= idx < len(rows):
                row = rows[idx]
                target = str(row.get("target", "") or row.get("process", "") or row.get("display", "") or "").strip()
                label = str(row.get("display", "") or target)
                self._prompt_input(chat_id, user_id, message_id, "app_volume_set", f"🔊 {label}. Введи громкость 0–100%.", payload={"target": target}, return_view=self._view_by_chat.get(str(chat_id), "appvol:0")); return
            self._send_message(chat_id, self._tg_message("Список аудиосессий устарел. Открой его заново."))
            self._show_app_volume_menu(chat_id, message_id, page=0); return

        if data.startswith("a:tgmsg:"):
            key=data.rsplit(":",1)[1]
            action={"status":"status","24":"status","recent":"recent"}.get(key,"status")
            args={"action":action,"recent24h": key=="24", "limit":10}
            self._execute_tool_now(chat_id,"telegram_messages",args,"Telegram Messages",panel_message_id=message_id,return_view="tgmsg"); return
        if data.startswith("a:mail:"):
            _a,_m,provider,action=data.split(":",3)
            args={"action": "status" if action=="status" else "latest", "provider": "" if provider=="all" else provider, "limit":5}
            self._execute_tool_now(chat_id,"email",args,"Почта",panel_message_id=message_id,return_view=("mail" if provider=="all" else f"mail:{provider}")); return
        # Owner-only remote mouse. Selecting a monitor immediately starts the
        # X -> Y wizard; a persistent per-chat button toggle defaults to left.
        if data.startswith("a:rmouse:setbtn:"):
            button = data.rsplit(":", 1)[1] if data.rsplit(":", 1)[1] in {"left", "right"} else "left"
            self._remote_mouse_button_by_chat[str(chat_id)] = button
            self._show_remote_mouse_menu(chat_id, message_id); return
        if data.startswith("a:rmouse:mon:"):
            try:
                monitor = int(data.rsplit(":", 1)[1])
                path, bounds = capture_coordinate_grid(self.config, monitor)
                button = str(self._remote_mouse_button_by_chat.get(str(chat_id), "left") or "left")
                if button not in {"left", "right"}: button = "left"
                self._remote_mouse_context_by_chat[str(chat_id)] = {"monitor": monitor, "bounds": dict(bounds), "button": button}
                left, top = int(bounds["left"]), int(bounds["top"])
                right = left + int(bounds["width"]) - 1
                bottom = top + int(bounds["height"]) - 1
                self._send_photo(chat_id, path, f"Экран {monitor} · глобальные координаты\nX: {left} … {right}\nY: {top} … {bottom}")
                self._prompt_input(
                    chat_id, user_id, message_id, "remote_mouse_xy",
                    "🖱 На скриншоте видны X/Y. Введи X и Y одной строкой через пробел, например: 1250 640.",
                    payload={"monitor": monitor, "button": button}, return_view="rmouse",
                )
            except Exception as exc:
                self._send_message(chat_id, self._tg_message(f"Не удалось получить экран: {exc}"))
                self._show_remote_mouse_menu(chat_id, message_id)
            return
        # Legacy HOTFIX96 callback remains harmless for already-open old keyboards.
        if data.startswith("a:rmouse:btn:"):
            parts = data.split(":")
            if len(parts) >= 5:
                try: monitor = int(parts[3])
                except Exception: monitor = 0
                button = parts[4] if parts[4] in {"left", "right"} else "left"
                self._remote_mouse_button_by_chat[str(chat_id)] = button
                if monitor > 0:
                    self._prompt_input(chat_id, user_id, message_id, "remote_mouse_xy", "🖱 Введи X и Y одной строкой через пробел.", payload={"monitor": monitor, "button": button}, return_view="rmouse"); return
            self._show_remote_mouse_menu(chat_id, message_id); return

        # Calendar CRUD with a deliberate second confirmation for deletion.
        if data.startswith("a:cal:delconfirm:"):
            try: idx = int(data.rsplit(":", 1)[1])
            except Exception: idx = -1
            rows = self._calendar_event_catalog_by_chat.get(str(chat_id), [])
            if 0 <= idx < len(rows):
                event = rows[idx]
                event_id = str(event.get("id", "") or "")
                title = str(event.get("summary", "") or "(без названия)")
                if event_id:
                    self._execute_tool(
                        chat_id, user_id, "google_calendar", {"action": "delete", "event_id": event_id},
                        f"Удаление события · {title}", panel_message_id=message_id, return_view="cal",
                    )
                    return
            self._send_message(chat_id, self._tg_message("Событие устарело или уже удалено. Обнови список."))
            self._show_calendar_menu(chat_id, message_id); return
        if data.startswith("a:cal:del:"):
            try: idx = int(data.rsplit(":", 1)[1])
            except Exception: idx = -1
            rows = self._calendar_event_catalog_by_chat.get(str(chat_id), [])
            if 0 <= idx < len(rows):
                event = rows[idx]
                dt = event_start_datetime(event)
                when = dt.strftime("%d.%m.%Y %H:%M") if dt else "без даты"
                title = str(event.get("summary", "") or "(без названия)")
                self._panel(
                    chat_id, message_id,
                    f"🗑 Точно удалить событие?\n{when} · {title}",
                    [[("✅ Да, удалить", f"a:cal:delconfirm:{idx}"), ("❌ Нет", "ui:cal")]], view="cal",
                )
                return
            self._show_calendar_event_picker(chat_id, message_id, "del", 0); return
        if data.startswith("a:cal:edit:"):
            try: idx = int(data.rsplit(":", 1)[1])
            except Exception: idx = -1
            rows = self._calendar_event_catalog_by_chat.get(str(chat_id), [])
            if 0 <= idx < len(rows):
                event = rows[idx]
                dt = event_start_datetime(event)
                payload = {
                    "event_id": str(event.get("id", "") or ""),
                    "old_title": str(event.get("summary", "") or ""),
                    "old_date": dt.strftime("%d.%m.%Y") if dt else "",
                    "old_time": dt.strftime("%H:%M") if dt else "",
                }
                self._prompt_input(
                    chat_id, user_id, message_id, "calendar_edit_title",
                    f"✏️ Новое название события.\nСейчас: {payload['old_title'] or '(без названия)'}\nОтправь - чтобы оставить без изменений.",
                    payload=payload, return_view="cal",
                )
                return
            self._show_calendar_event_picker(chat_id, message_id, "edit", 0); return

        # Discord server/channel selection in the Telegram Remote menu.
        if data.startswith("a:dcsel:"):
            parts = data.split(":")
            # a:dcsel:<voice|text>:s:<server-index>
            if len(parts) == 5 and parts[3] == "s":
                mode = parts[2]
                try: server_index = int(parts[4])
                except Exception: server_index = -1
                self._show_discord_channel_picker(chat_id, message_id, mode, server_index); return
            # a:dcsel:<voice|text>:c:<server-index>:<channel-index>
            if len(parts) == 6 and parts[3] == "c":
                mode = parts[2]
                try:
                    server_index = int(parts[4]); channel_index = int(parts[5])
                except Exception:
                    server_index = channel_index = -1
                names = self._discord_server_names()
                catalog = self._discord_channel_catalog_by_chat.get(str(chat_id), {})
                expected_key = f"{mode}:{server_index}"
                channels = list(catalog.get("channels") or []) if str(catalog.get("key", "")) == expected_key else []
                if 0 <= server_index < len(names) and 0 <= channel_index < len(channels):
                    server = names[server_index]
                    channel = str(channels[channel_index].get("name", "") or "").strip()
                    if mode == "voice":
                        self._execute_tool(
                            chat_id, user_id, "discord", {"action": "bot_join_voice", "server": server, "channel": channel},
                            f"Discord · войти в {server} / {channel}", panel_message_id=message_id, return_view="md",
                        )
                    else:
                        self._prompt_input(
                            chat_id, user_id, message_id, "discord_text_body",
                            f"💬 Напиши текст сообщения для Discord.\nСервер: {server}\nКанал: #{channel}",
                            payload={"server": server, "channel": channel}, return_view="md",
                        )
                    return
                self._send_message(chat_id, self._tg_message("Список Discord-каналов устарел. Выбери сервер заново."))
                self._show_discord_server_picker(chat_id, message_id, mode); return

        if data.startswith("a:cal:"):
            key = data.rsplit(":", 1)[1]
            args = {"action": {"today": "today", "tomorrow": "tomorrow", "week": "upcoming", "all": "all"}.get(key, "today")}
            if key == "week":
                args["days"] = 7
            self._execute_tool_now(chat_id, "google_calendar", args, "Google Calendar", panel_message_id=message_id, return_view="cal"); return
        if data.startswith("i:mail:") and data.endswith(":send"):
            provider=data.split(":")[2]
            self._prompt_input(chat_id,user_id,message_id,"mail_send_to","✉️ Введи адрес получателя.",payload={"provider":provider},return_view=f"mail:{provider}"); return
        if data == "i:cal:add":
            self._prompt_input(chat_id,user_id,message_id,"calendar_add_date","📅 Введи дату события, например 26.08.2026 или завтра.",payload={},return_view="cal"); return

        if data.startswith("i:closeother:"):
            kind = data.rsplit(":",1)[1]
            self._prompt_input(chat_id, user_id, message_id, "close_other", "Напиши название приложения или процесса.", payload={"kind":kind}, return_view="apps"); return
        if data in {"i:ovcheck:mark", "i:ovcheck:delete"}:
            self._show_checklist_item_picker(chat_id, message_id, "mark" if data.endswith(":mark") else "delete")
            return
        if data.startswith("ui:ovcheck:"):
            parts = data.split(":")
            if len(parts) == 4 and parts[2] in {"mark", "delete"}:
                try:
                    page = int(parts[3])
                except Exception:
                    page = 0
                self._show_checklist_item_picker(chat_id, message_id, parts[2], page=page)
                return
        if data.startswith("a:ovcheck:"):
            parts = data.split(":")
            if len(parts) == 4 and parts[2] in {"mark", "delete"}:
                action = parts[2]
                try:
                    index = int(parts[3])
                except Exception:
                    index = -1
                catalog = self._checklist_catalog_by_chat.get(str(chat_id), {})
                items = list(catalog.get("items") or []) if str(catalog.get("action", "")) == action else []
                if 0 <= index < len(items):
                    item = str(items[index].get("text", "") or "").strip()
                    name = self._overlay_panel_name("checklist")
                    if action == "mark":
                        self._execute_tool_now(
                            chat_id, "overlay", {"action":"checklist_check", "name":name, "item":item},
                            "Панель · зачеркнуть пункт", panel_message_id=message_id, return_view="ov:checklist",
                        )
                    else:
                        args = {"action":"checklist_remove", "name":name, "item":item}
                        self._show_plain_confirmation(
                            chat_id, message_id, self._tg_message("Удалить пункт чек-листа?") + f"\n{item}",
                            {"mode":"direct_tool", "tool_name":"overlay", "arguments":args, "label":"Панель · удалить пункт", "return_view":"ov:checklist"},
                            yes_label="✅ Удалить", no_label="❌ Не удалять", view="ov:checklist",
                        )
                    return
                self._show_checklist_item_picker(chat_id, message_id, action)
                return

        if data in {"i:ovcheck:add","i:ovnote:set","i:ovnote:append"} or data.startswith("i:ov:rename:"):
            mapping = {
                "i:ovcheck:add": ("overlay_check_add", "➕ Введи новый пункт чек-листа.", "checklist"),
                "i:ovnote:set": ("overlay_note_set", "✏️ Введи новый текст заметок.", "notes"),
                "i:ovnote:append": ("overlay_note_append", "➕ Введи текст, который нужно дописать к заметкам.", "notes"),
            }
            if data.startswith("i:ov:rename:"):
                category = data.rsplit(":",1)[1]; kind="overlay_rename"; prompt="🏷 Введи новое название панели."
                self._ensure_overlay_panel_for_remote(chat_id, category)
            else:
                kind,prompt,category=mapping[data]
                self._ensure_overlay_panel_for_remote(chat_id, category)
            self._prompt_input(chat_id, user_id, message_id, kind, prompt, payload={"category":category}, return_view=f"ov:{category}"); return

        # Text-input buttons.
        prompts = {
            "i:ym:pl": ("ym_playlist", "📂 Напиши название плейлиста Яндекс Музыки."),
            "i:ym:track": ("ym_track", "🔎 Напиши название трека или исполнителя."),
            "i:music:pl": ("music_playlist", "📂 Напиши название плейлиста активного музыкального сервиса."),
            "i:music:track": ("music_track", "🔎 Напиши название трека или исполнителя."),
            "i:music:youtube": ("music_youtube_url", "▶️ Вставь прямую ссылку YouTube (youtube.com или youtu.be)."),
            "i:yt:search": ("youtube_search_title", "🔎 Введи название видео."),
            "i:yt:seek": ("youtube_seek_time", "🕐 Введи точное время: например 1:30, 90 или 2 мин 15 сек."),
            "i:dc:pl": ("dc_playlist", "📂 Напиши название плейлиста для Discord Music."),
            "i:dc:track": ("dc_track", "🔎 Напиши название трека для Discord Music."),
            "i:dc:youtube": ("dc_youtube_url", "▶️ Вставь ссылку YouTube — NEO включит её в голосовом канале Discord, где ты находишься."),
            "i:br:q": ("browser_search", "🔎 Что найти в браузере?"),
            "i:web:q": ("web_search", "🔎 Какую информацию найти через Google AI / SearchAPI.io?"),
            "i:br:url": ("browser_url", "🔗 Напиши URL или домен сайта."),
            "i:br:tabn": ("browser_tab_index", "🔢 Напиши номер вкладки, начиная с 1."),
            "i:br:tabt": ("browser_tab_title", "📝 Напиши часть названия вкладки."),
            "i:br:close": ("browser_close_index", "❌ Напиши номер вкладки, которую закрыть."),
            "i:app:open": ("app_open", "🚀 Напиши название приложения для запуска."),
            "i:app:close": ("app_close", "🛑 Напиши название приложения для закрытия."),
            "i:game:open": ("game_open", "🎮 Напиши название игры для запуска."),
            "i:game:close": ("game_close", "⛔ Напиши название игры для закрытия."),
            "i:vol:set": ("volume_set", "🔊 Напиши громкость от 0 до 100."),
            "i:bright:set": ("brightness_set", "💡 Напиши яркость от 0 до 100."),
            "i:screen:move": ("screen_move_window", "🪟 Напиши название приложения или окна, которое нужно переместить. Например: Opera, Steam, Discord."),
            "i:win:move": ("screen_move_window", "🪟 Напиши название приложения или окна, которое нужно переместить. Например: Opera, Steam, Discord."),
            "i:win:min": ("screen_minimize_window", "➖ Напиши название приложения или окна, которое нужно свернуть."),
            "i:mouse:set": ("mouse_sensitivity_set", "🖱 Введи DPI-профиль указателя Windows, например 800, 1600 или 3200. Аппаратный DPI сенсора не меняется."),
            "i:music:vol": ("music_volume_set", "🔊 Введи громкость активного музыкального сервиса от 0 до 100%."),
            "i:timer:create": ("timer_create", "⏱ Введи длительность таймера, например: 5 минут, 90 секунд или 1 час."),
            "i:mic:set": ("microphone_volume_set", "🎙 Напиши громкость системного микрофона от 0 до 100."),
            "i:key:custom": ("remote_key", "⌨️ Напиши клавишу, текст или комбинацию. Примеры: Enter · где · Ctrl+Backspace+Enter · Ctrl, Shift, Enter."),
        }
        if data in prompts:
            kind, prompt = prompts[data]
            return_views = {
                "i:ym:pl": "mm", "i:ym:track": "mm", "i:music:pl": "mm", "i:music:track": "mm", "i:music:youtube": "mm",
                "i:yt:search": "yt", "i:yt:seek": "yt",
                "i:dc:pl": "md", "i:dc:track": "md", "i:dc:youtube": "md",
                "i:br:q": "br", "i:web:q": "home", "i:br:url": "br", "i:br:tabn": "br",
                "i:br:tabt": "br", "i:br:close": "br",
                "i:app:open": "apps", "i:app:close": "apps",
                "i:game:open": "apps", "i:game:close": "apps",
                "i:vol:set": "vol", "i:bright:set": "bright",
                "i:screen:move": "win", "i:win:move": "win", "i:win:min": "win",
                "i:mouse:set": "mouse", "i:music:vol": "mm", "i:timer:create": "timer", "i:mic:set": "mic", "i:key:custom": "key",
            }
            self._prompt_input(chat_id, user_id, message_id, kind, prompt, return_view=return_views.get(data, "home"))
            return

        if data == "a:screen:list":
            self._send_message(chat_id, self._window_screen_text())
            self._notify_desktop("📱 Telegram · запрошен список окон по экранам.")
            self._roll_panel_to_bottom(chat_id, "win", message_id)
            return

        # HOTFIX110 Telegram history / overlay shared settings / secure Explorer.
        if data == "a:tg:clearhistory":
            self._leave_explorer(chat_id)
            # Clear every tracked/stale panel first, then create exactly one new
            # Home keyboard. Recreating from inside cleanup used to race with the
            # callback's old message and could leave two menus in chat.
            self._cleanup_chat(chat_id, recreate_home=False, aggressive=True)
            self._show_home(chat_id, 0)
            # NEO 188 NEO-TG-001: report what was really removed, not «all history».
            self._notify_desktop("📱 Telegram · " + self.cleanup_summary(language_code(self.config)))
            return
        if data == "a:timer:delnext":
            manager = timer_manager(self.config.paths.root, language_code(self.config))
            manager.cancel("")
            self._show_timer_menu(chat_id, message_id); return
        if data.startswith("a:timer:del:"):
            timer_id = data.split(":", 3)[3]
            manager = timer_manager(self.config.paths.root, language_code(self.config))
            manager.cancel(timer_id)
            self._show_timer_menu(chat_id, message_id); return
        if data.startswith("a:ovall:"):
            self._set_all_overlay_visibility(chat_id, data.rsplit(":",1)[1] == "on")
            self._show_panels_menu(chat_id, message_id); return
        if data.startswith("a:ovpreset:"):
            try: _a,_tag,category,idx_s=data.split(":",3); idx=int(idx_s)
            except Exception: category,idx="notes",-1
            names=sorted(THEME_PRESETS.keys(),key=lambda value:value.casefold())+["Custom"]
            if 0 <= idx < len(names):
                preset_name=names[idx]
                changes={"theme_preset":preset_name}
                palette=THEME_PRESETS.get(preset_name,{}).get("window",{}) if preset_name != "Custom" else {}
                if palette:
                    changes.update({
                        "text_color":str(palette.get("text_color","#F2F6FF")),
                        "background_color":str(palette.get("panel_color","#0A0F18")),
                        "border_color":str(palette.get("border_color","#57D7FF")),
                    })
                    if category == "music":
                        changes.update({
                            "control_color":str(palette.get("text_color","#F2F6FF")),
                            "control_background_color":str(palette.get("element_color",palette.get("panel_color","#0A0F18"))),
                            "control_border_color":str(palette.get("border_color","#57D7FF")),
                        })
                ok,text=self._apply_overlay_style_setting(chat_id,category,**changes)
                if not ok: self._send_message(chat_id,"⚠️ "+self._tg_runtime_text(text))
            self._show_overlay_style_menu(chat_id,message_id,category); return
        if data.startswith("a:ovfontpick:"):
            try: _a,_tag,category,idx_s=data.split(":",3); idx=int(idx_s)
            except Exception: category,idx="notes",-1
            catalog=self._overlay_font_catalog_by_chat.get(str(chat_id),[])
            if 0 <= idx < len(catalog):
                ok,text=self._apply_overlay_style_setting(chat_id,category,font_family=str(catalog[idx]))
                if not ok: self._send_message(chat_id,"⚠️ "+self._tg_runtime_text(text))
            self._show_overlay_style_menu(chat_id,message_id,category); return
        if data.startswith("a:ovsize:"):
            try: _a,_tag,category,value_s=data.split(":",3); value=max(8,min(96,int(value_s)))
            except Exception: category,value="notes",18
            ok,text=self._apply_overlay_style_setting(chat_id,category,font_size=value)
            if not ok: self._send_message(chat_id,"⚠️ "+self._tg_runtime_text(text))
            self._show_overlay_style_menu(chat_id,message_id,category); return
        if data.startswith("a:ovopacity:"):
            try: _a,_tag,category,value_s=data.split(":",3); value=max(0,min(100,int(value_s)))
            except Exception: category,value="notes",72
            ok,text=self._apply_overlay_style_setting(chat_id,category,background_opacity=value,opacity=value)
            if not ok: self._send_message(chat_id,"⚠️ "+self._tg_runtime_text(text))
            self._show_overlay_style_menu(chat_id,message_id,category); return
        if data.startswith("a:exp:drive:"):
            state=self._explorer_state(chat_id)
            if state is None:
                self._send_message(chat_id,self._tg_message("Сеанс Проводника завершён. Войди снова через кодовую фразу.")); self._show_home(chat_id,message_id); return
            try: idx=int(data.rsplit(":",1)[1])
            except Exception: idx=-1
            drives=list(state.get("drives") or [])
            if 0 <= idx < len(drives):
                path=str(drives[idx].get("path","") or "")
                if path: state["current_path"]=path; state["page"]=0; self._show_explorer_folder(chat_id,message_id,page=0); return
            self._show_explorer_drives(chat_id,message_id); return
        if data.startswith("a:exp:dir:"):
            state=self._explorer_state(chat_id)
            if state is None:
                self._send_message(chat_id,self._tg_message("Сеанс Проводника завершён. Войди снова через кодовую фразу.")); self._show_home(chat_id,message_id); return
            try: idx=int(data.rsplit(":",1)[1])
            except Exception: idx=-1
            folders=list(state.get("folders") or [])
            if 0 <= idx < len(folders):
                path=str(folders[idx].get("path","") or "")
                if path: state["current_path"]=path; state["page"]=0; self._show_explorer_folder(chat_id,message_id,page=0); return
            self._show_explorer_folder(chat_id,message_id,page=int(state.get("page",0) or 0)); return
        if data == "a:exp:up":
            state=self._explorer_state(chat_id)
            if state is None:
                self._show_home(chat_id,message_id); return
            current=str(state.get("current_path","") or "")
            if not current: self._show_explorer_drives(chat_id,message_id); return
            try:
                parent=Path(current).parent
                if str(parent) == str(Path(current)) or not str(parent): state["current_path"]=""; self._show_explorer_drives(chat_id,message_id); return
                state["current_path"]=str(parent); state["page"]=0
            except Exception:
                state["current_path"]=""
            self._show_explorer_folder(chat_id,message_id,page=0) if state.get("current_path") else self._show_explorer_drives(chat_id,message_id); return
        if data == "a:exp:drives":
            state=self._explorer_state(chat_id)
            if state is None: self._show_home(chat_id,message_id); return
            state["current_path"]=""; self._show_explorer_drives(chat_id,message_id); return
        if data == "a:exp:open":
            state=self._explorer_state(chat_id)
            if state is None:
                self._show_home(chat_id,message_id); return
            current=str(state.get("current_path","") or "")
            if current:
                try:
                    result=self._tools_for_direct_action("filesystem").execute("filesystem",{"action":"open","source":current})
                    if not result.success: self._send_message(chat_id,"⚠️ "+self._tg_runtime_text(str(result.text or self._tg_message("Не удалось открыть папку."))))
                    else: self._notify_desktop(f"📱 Telegram · Проводник открыл папку: {current}")
                except Exception as exc:
                    logger.exception("Telegram Explorer open failed")
                    self._send_message(chat_id,"⚠️ "+self._tg_message("Не удалось открыть папку.")+f" {exc}")
            self._show_explorer_folder(chat_id,message_id,page=int(state.get("page",0) or 0)); return

        # HOTFIX106 overlay controls.
        if data.startswith("a:ovtoggle:"):
            category = data.rsplit(":",1)[1]
            panel = self._overlay_panel(category)
            name = self._overlay_panel_name(category)
            if isinstance(panel, dict) and bool(panel.get("visible", False)):
                self._execute_tool_now(chat_id, "overlay", {"action":"hide", "name":name}, "Панель · выключить", panel_message_id=message_id, return_view=f"ov:{category}")
            elif category == "game":
                self._execute_tool_now(chat_id, "game", {"action":"show_session"}, "Игровой оверлей · включить", panel_message_id=message_id, return_view=f"ov:{category}")
            else:
                default_name, kind, purpose = self._overlay_defaults_for(category)
                args = {"action":"show", "name":name or default_name, "title":name or default_name, "kind":kind, "purpose":purpose}
                if category == "checklist" and panel is None:
                    args.update({"action":"checklist_create", "items":[]})
                self._execute_tool_now(chat_id, "overlay", args, "Панель · включить", panel_message_id=message_id, return_view=f"ov:{category}")
            return
        if data.startswith("a:ovfont:"):
            try:
                _a, _b, category, delta_s = data.split(":",3)
                delta = int(delta_s)
            except Exception:
                category, delta = "notes", 0
            panel = self._overlay_panel(category)
            name = self._overlay_panel_name(category)
            size = max(8, min(96, self._overlay_font_size(category, panel) + delta))
            ok, text = self._apply_overlay_style_setting(chat_id, category, font_size=size)
            if not ok:
                self._send_message(chat_id, "⚠️ " + self._tg_runtime_text(text))
            self._show_overlay_menu(chat_id, message_id, category)
            return
        if data.startswith("a:ovmon:"):
            try:
                _a, _b, category, monitor_s = data.split(":",3); monitor = max(1, int(monitor_s))
            except Exception:
                category, monitor = "notes", 1
            self._ensure_overlay_panel_for_remote(chat_id, category)
            self._execute_tool_now(chat_id, "overlay", {"action":"move", "name":self._overlay_panel_name(category), "monitor":monitor}, f"Панель · экран {monitor}", panel_message_id=message_id, return_view=f"ov:{category}")
            return
        if data.startswith("a:ovpos:"):
            try: _a, _b, category, position = data.split(":",3)
            except Exception: category, position = "notes", "top_left"
            self._ensure_overlay_panel_for_remote(chat_id, category)
            self._execute_tool_now(chat_id, "overlay", {"action":"move", "name":self._overlay_panel_name(category), "position":position}, f"Панель · {position}", panel_message_id=message_id, return_view=f"ov:{category}")
            return
        if data == "a:ovnote:clear":
            self._ensure_overlay_panel_for_remote(chat_id, "notes")
            self._show_plain_confirmation(
                chat_id, message_id, self._tg_message("Очистить заметки?"),
                {"mode":"direct_tool", "tool_name":"overlay", "arguments":{"action":"clear_text", "name":self._overlay_panel_name("notes")}, "label":"Заметки · очистить", "return_view":"ov:notes"},
                yes_label="✅ Очистить", no_label="❌ Не очищать", view="ov:notes",
            )
            return
        if data.startswith("a:brclean:"):
            try:
                _a, _tag, mode, idx_s = data.split(":",3); idx = int(idx_s)
            except Exception:
                mode, idx = "h", -1
            rows = self._browser_cleanup_catalog_by_chat.get(str(chat_id), [])
            if 0 <= idx < len(rows):
                row = rows[idx]; key = str(row.get("key","") or ""); label = str(row.get("label",key) or key)
                action = "full_cleanup" if mode == "f" else "clear_history"
                warning = f"Полный снос локальных данных {label}" if mode == "f" else f"Очистить историю {label}"
                self._show_plain_confirmation(
                    chat_id, message_id, self._tg_message("Продолжить очистку браузера?") + f"\n{warning}",
                    {"mode":"secure_tool", "tool_name":"browser", "arguments":{"action":action, "browser":key}, "label":warning, "return_view":"br"},
                    yes_label="✅ Продолжить", no_label="❌ Отмена", view="br",
                )
            else:
                self._show_browser_menu(chat_id, message_id)
            return
        if data.startswith("a:closepick:"):
            try:
                _a, _tag, kind, idx_s = data.split(":",3); idx = int(idx_s)
            except Exception:
                kind, idx = "app", -1
            rows = self._close_catalog_by_chat.get(str(chat_id), [])
            if 0 <= idx < len(rows):
                row=rows[idx]; self._show_close_confirmation(chat_id, message_id, kind, str(row.get("target","") or ""), str(row.get("label","") or ""))
            else:
                self._show_close_picker(chat_id, message_id, kind)
            return
        if data == "a:closeconfirm:no":
            pending = self._close_pending_by_chat.pop(str(chat_id), {})
            self._show_close_picker(chat_id, message_id, str(pending.get("kind","app") or "app")); return
        if data == "a:closeconfirm:yes":
            pending = self._close_pending_by_chat.pop(str(chat_id), {})
            target = str(pending.get("target","") or "").strip(); kind = str(pending.get("kind","app") or "app")
            if target:
                self._execute_tool(chat_id, user_id, "application", {"action":"close", "name":target, "kind":"game" if kind=="game" else "app"}, f"Закрыть {target}", panel_message_id=message_id, return_view="apps")
            else:
                self._show_apps_menu(chat_id, message_id)
            return
        if data == "a:brief:now":
            self._execute_tool_now(chat_id, "briefing", {"action":"summary"}, "Сводка дня", panel_message_id=message_id, return_view="brief")
            return
        if data == "a:jarvis:on":
            # Stale callback from a previous/offline panel. If this controller is
            # handling it, the primary NEO mutex is already online, so Start is
            # not a valid action anymore.
            self._send_message(chat_id, self._tg_message("NEO уже запущен."))
            self._show_home(chat_id, message_id)
            return
        if data == "a:jarvis:off":
            self._show_plain_confirmation(
                chat_id, message_id, self._tg_message("Выключить NEO?"),
                {"mode":"direct_tool", "tool_name":"jarvis", "arguments":{"action":"shutdown"}, "label":"Полностью выключить NEO", "return_view":"home"},
                yes_label="✅ Да, выключить", no_label="❌ Нет", view="home",
            )
            return

        # Information hub. Keep result above a fresh sticky submenu.
        if data == "a:info:sys":
            current_view = str(self._view_by_chat.get(str(chat_id), "home") or "home")
            self._send_status(chat_id)
            self._roll_panel_to_bottom(chat_id, current_view, message_id)
            return
        if data == "a:info:weather":
            self._execute_tool_now(chat_id, "weather", {"action": "summary"}, "Погода", panel_message_id=message_id, return_view="weather")
            return
        if data == "a:info:proc":
            current_view = str(self._view_by_chat.get(str(chat_id), "home") or "home")
            self._send_process_table(chat_id)
            self._roll_panel_to_bottom(chat_id, current_view, message_id)
            return

        # Window-to-monitor workflow. The window name is stored locally, never in
        # callback_data, and the existing NEO window tool performs the move.
        if data.startswith("a:mvwin:"):
            target = str(self._screen_move_target_by_chat.get(chat_id, "") or "").strip()
            if not target:
                self._send_message(chat_id, self._tg_message("Сначала выбери «Переместить окно» и укажи название окна."))
                self._show_windows_menu(chat_id, message_id)
                return
            try:
                monitor = max(1, int(data.rsplit(":", 1)[1]))
            except Exception:
                monitor = 1
            self._execute_tool(chat_id, user_id, "window", {"action": "move_to_monitor", "title": target, "monitor": monitor, "kind": "any"}, f"Экран · {target} → монитор {monitor}", panel_message_id=message_id, return_view="win")
            return

        # Screenshots.
        if data.startswith("a:shot:"):
            target = data.split(":", 2)[2]
            self._screen(chat_id, target)
            self._notify_desktop(f"📱 Telegram · отправлен скриншот экрана {target}.")
            self._roll_panel_to_bottom(chat_id, "shot", message_id)
            return

        # Locally configured NEO scenarios. Selection is by index, while the
        # tool receives the exact saved name/phrase; Telegram cannot inject a
        # scenario command body through callback_data.
        if data.startswith("a:sc:"):
            try:
                from jarvis.runtime.scenarios import scenario_fingerprint
                token = data.rsplit(":", 1)[1]
                if not re.fullmatch(r"[0-9a-f]{32}", token):
                    raise ValueError("Scenario menu changed; refresh it")
                items = self._scenario_items()
                matches = [(i, item) for i, item in enumerate(items) if scenario_fingerprint(item)[:32] == token]
                if len(matches) != 1:
                    raise ValueError("Scenario menu changed; refresh it")
                index, raw = matches[0]
                name = str(raw.get("name", "") or raw.get("phrase", "") or "").strip()
                page = max(0, index // 8)
                if not name:
                    raise ValueError("У сценария нет названия")
                self._execute_tool(
                    chat_id, user_id, "scenario", {"action": "run", "name": name, "id": str(raw.get("id") or name), "fingerprint": scenario_fingerprint(raw)},
                    f"Сценарий · {name}", panel_message_id=message_id,
                    return_view=f"sc:{page}",
                )
            except Exception as exc:
                self._send_message(chat_id, self._tg_message(f"Не удалось запустить сценарий: {exc}"))
                self._roll_panel_to_bottom(chat_id, "sc:0", message_id)
            return

        # Yandex Music.
        ym_actions = {
            "wave": "my_wave", "tracks": "my_tracks", "prev": "previous", "pause": "pause",
            "next": "next", "play": "play", "stop": "stop", "sh1": "shuffle_on",
            "sh0": "shuffle_off", "like": "like", "dislike": "dislike", "queue": "queue_status", "qclear": "queue_clear",
        }
        if data.startswith("a:ym:"):
            key = data.split(":", 2)[2]
            action = ym_actions.get(key)
            if action:
                self._execute_tool_now(chat_id, "yandex_music", {"action": action}, f"Яндекс Музыка · {action}", panel_message_id=message_id, return_view="mm")
            return

        # Active local music provider (Yandex or Spotify).
        music_actions = {
            "prev":"previous", "next":"next", "pause":"pause", "play":"play", "status":"status",
            "sh1":"shuffle_on", "sh0":"shuffle_off",
            "rpt":"repeat_track", "rpl":"repeat_playlist", "rp0":"repeat_off",
        }
        if data.startswith("a:music:"):
            key = data.split(":", 2)[2]
            action = music_actions.get(key)
            if action:
                self._execute_tool_now(chat_id, "music", {"action":action}, f"Музыка · {action}", panel_message_id=message_id, return_view="mm")
            return

        # Discord Music.
        dc_actions = {
            "wave": "music_my_wave", "tracks": "music_my_tracks", "prev": "music_previous",
            "pause": "music_pause", "next": "music_skip", "play": "music_resume", "stop": "music_pause",
            "sh1": "music_shuffle_on", "sh0": "music_shuffle_off",
            "rpt": "music_repeat_track", "rpl": "music_repeat_playlist", "rp0": "music_repeat_off",
            "like": "music_like",
            "dislike": "music_dislike", "queue": "music_queue_status", "qclear": "music_queue_clear",
        }
        if data.startswith("a:dc:"):
            key = data.split(":", 2)[2]
            action = dc_actions.get(key)
            if action:
                self._execute_tool_now(chat_id, "discord", {"action": action}, f"Discord Music · {action}", panel_message_id=message_id, return_view="md")
            return

        if data.startswith("a:vid:"):
            legacy = {"pause":"pause", "play":"play", "next":"next", "prev":"previous"}.get(data.split(":",2)[2])
            if legacy:
                self._execute_tool_now(chat_id, "media", {"action":legacy, "target":"youtube"}, f"YouTube · {legacy}", panel_message_id=message_id, return_view="yt", recreate_panel=False, quiet=True)
            return

        if data.startswith("a:ytq:"):
            quality = data.split(":",2)[2]
            self._execute_tool_now(chat_id, "media", {"action":"set_quality", "target":"youtube", "quality":quality}, f"YouTube · качество {quality}", panel_message_id=message_id, return_view="ytq", recreate_panel=False, quiet=True)
            return
        if data.startswith("a:yts:"):
            speed = float(data.split(":",2)[2])
            self._execute_tool_now(chat_id, "media", {"action":"set_speed", "target":"youtube", "speed":speed}, f"YouTube · скорость {speed:g}×", panel_message_id=message_id, return_view="yts", recreate_panel=False, quiet=True)
            return
        if data.startswith("a:yt:"):
            key = data.split(":",2)[2]
            actions = {
                "pause": ("pause", {}), "play": ("play", {}), "next": ("next", {}), "prev": ("previous", {}),
                "fullscreen": ("fullscreen", {}), "theater": ("theater", {}),
                "shortnext": ("shorts_next", {}), "shortprev": ("shorts_previous", {}),
                "back15": ("seek_backward", {"seconds":15}), "fwd15": ("seek_forward", {"seconds":15}),
                "back30": ("seek_backward", {"seconds":30}), "fwd30": ("seek_forward", {"seconds":30}),
                "back60": ("seek_backward", {"seconds":60}), "fwd60": ("seek_forward", {"seconds":60}),
            }
            action_row = actions.get(key)
            if action_row:
                action, extra = action_row
                self._execute_tool_now(chat_id, "media", {"action":action, "target":"youtube", **extra}, f"YouTube · {action}", panel_message_id=message_id, return_view="yt", recreate_panel=False, quiet=True)
            return

        if data == "a:br:new":
            self._execute_tool_now(chat_id, "browser", {"action": "new_tab"}, "Браузер · новая вкладка", panel_message_id=message_id, return_view="br"); return

        if data.startswith("a:adev:refresh:"):
            parts = data.split(":")
            token = parts[3] if len(parts) > 3 else "i"
            try: page = int(parts[4]) if len(parts) > 4 else 0
            except Exception: page = 0
            self._audio_device_catalog_by_chat.pop(str(chat_id), None)
            self._show_audio_device_menu(chat_id, message_id, device_type="input" if token == "i" else "output", page=page, force=True)
            return
        if data.startswith("a:adev:"):
            parts = data.split(":")
            if len(parts) >= 4 and parts[2] in {"i", "o"}:
                token = parts[2]
                try: index = int(parts[3])
                except Exception: index = -1
                try: revision = int(parts[4]) if len(parts) > 4 else -1
                except Exception: revision = -1
                kind = "input" if token == "i" else "output"
                choice = self._audio_menu_choice188(chat_id, token, index, revision)
                if choice is not None:
                    device_id, device_name = choice
                    page = index // 6
                    self._audio_device_catalog_by_chat.pop(str(chat_id), None)
                    self._execute_tool(
                        chat_id, user_id, "audio",
                        {"action": "set_default", "device_type": kind, "device_id": device_id, "device_name": device_name},
                        f"{'Микрофон' if kind == 'input' else 'Устройство вывода'} по умолчанию · {device_name}",
                        panel_message_id=message_id, return_view=f"{'micdev' if kind == 'input' else 'outdev'}:{page}",
                    )
                    return
            self._send_message(chat_id, self._tg_message("Список аудиоустройств устарел. Обнови его и выбери устройство ещё раз."))
            self._audio_device_catalog_by_chat.pop(str(chat_id), None)
            self._show_audio_device_menu(chat_id, message_id, device_type="input" if ":i:" in data else "output", page=0, force=True)
            return

        if data.startswith("a:vol:"):
            level = int(data.rsplit(":", 1)[1])
            self._execute_tool_now(chat_id, "audio", {"action": "set_volume", "value": level}, f"Громкость {level}%", panel_message_id=message_id, return_view="vol"); return
        if data.startswith("a:mute:"):
            muted = data.endswith(":1")
            self._execute_tool_now(chat_id, "audio", {"action": "mute", "muted": muted}, "Mute" if muted else "Unmute", panel_message_id=message_id, return_view="vol"); return

        if data.startswith("a:mouse:"):
            raw = data.rsplit(":", 1)[1]
            if raw == "get":
                self._execute_tool_now(chat_id, "system", {"action": "get_mouse_sensitivity"}, "Текущая чувствительность мыши", panel_message_id=message_id, return_view="mouse", recreate_panel=False, quiet=True)
                self._show_mouse_menu(chat_id, message_id); return
            dpi = max(200, min(6400, int(raw)))
            level = self._dpi_to_windows_speed(dpi)
            self._execute_tool_now(chat_id, "system", {"action": "set_mouse_sensitivity", "value": level}, f"Мышь · {dpi} DPI", panel_message_id=message_id, return_view="mouse", recreate_panel=False, quiet=True)
            self._show_mouse_menu(chat_id, message_id); return
        if data.startswith("a:musicvolrel:"):
            delta = int(data.rsplit(":", 1)[1])
            action = "volume_up" if delta > 0 else "volume_down"
            self._execute_tool_now(chat_id, "music", {"action": action, "step": abs(delta)}, f"Громкость музыки {delta:+d}%", panel_message_id=message_id, return_view="mm", quiet=True)
            return
        if data.startswith("a:dcvolrel:"):
            delta = int(data.rsplit(":", 1)[1])
            action = "music_volume_up" if delta > 0 else "music_volume_down"
            args: dict[str, Any] = {"action": action, "step": abs(delta)}
            server = str(self.config.get("integrations.discord.default_server", "") or "").strip()
            if server:
                args["server"] = server
            self._execute_tool_now(chat_id, "discord", args, f"Discord Music {delta:+d}%", panel_message_id=message_id, return_view="md", quiet=True)
            return
        if data.startswith("a:musicvol:"):
            level = max(0, min(100, int(data.rsplit(":",1)[1])))
            self._execute_tool_now(chat_id, "music", {"action":"set_volume", "volume":level}, f"Громкость музыки {level}%", panel_message_id=message_id, return_view="mm", quiet=True)
            return
        if data == "a:mic:mute":
            self._execute_tool(chat_id, user_id, "audio", {"action": "set_input_mute", "muted": True}, "Выключить системный микрофон", panel_message_id=message_id, return_view="mic"); return
        if data == "a:mic:unmute":
            self._execute_tool(chat_id, user_id, "audio", {"action": "set_input_mute", "muted": False}, "Включить системный микрофон", panel_message_id=message_id, return_view="mic"); return
        if data.startswith("a:micvol:"):
            raw = data.rsplit(":", 1)[1]
            if raw == "get":
                self._execute_tool_now(chat_id, "audio", {"action": "get_input_volume"}, "Текущий уровень микрофона", panel_message_id=message_id, return_view="mic"); return
            level = max(0, min(100, int(raw)))
            self._execute_tool(chat_id, user_id, "audio", {"action": "set_input_volume", "value": level}, f"Микрофон · {level}%", panel_message_id=message_id, return_view="mic"); return

        if data.startswith("a:key:"):
            preset = data.rsplit(":", 1)[1]
            values = {
                "ctrl": "Ctrl", "control": "Control", "shift": "Shift", "alt": "Alt", "tab": "Tab",
                "capslock": "Caps Lock", "esc": "Escape", "backspace": "Backspace", "enter": "Enter",
                "left": "Left", "up": "Up", "down": "Down", "right": "Right", "fn": "Fn",
                "right_shift": "Right Shift", "space": "Space", "delete": "Delete",
                "page_up": "Page Up", "page_down": "Page Down", "win": "Win",
            }
            value = values.get(preset)
            if value:
                ok, result_text = execute_remote_key_input(value)
                self._send_message(chat_id, ("✅ " if ok else "⚠️ ") + self._tg_runtime_text(result_text))
                self._notify_desktop(f"📱 Telegram · клавиатура: {result_text}")
            self._roll_panel_to_bottom(chat_id, "key", message_id); return

        if data.startswith("a:wifi:"):
            enabled = data.endswith(":1")
            self._execute_tool(chat_id, user_id, "system", {"action": "set_wifi", "enabled": enabled}, f"Wi‑Fi {'ON' if enabled else 'OFF'}", panel_message_id=message_id, return_view="net"); return
        if data == "a:bt:refresh":
            self._show_bluetooth_menu(chat_id, message_id, force=True); return
        if data == "a:btpair:refresh":
            self._show_bluetooth_pair_menu(chat_id, message_id, force=True); return
        if data == "a:btpair:noop":
            self._show_bluetooth_pair_menu(chat_id, message_id); return
        if data.startswith("a:btpair:"):
            try:
                index = int(data.rsplit(":", 1)[1])
                catalog = self._bluetooth_pair_catalog_by_chat.get(str(chat_id), [])
                device = catalog[index]
                ok, text = pair_bluetooth_device(str(device.get("device_id", "") or ""), str(device.get("name", "") or ""))
                self._send_message(chat_id, ("✅ " if ok else "⚠️ ") + self._tg_runtime_text(text))
                if ok:
                    self._bluetooth_pair_catalog_by_chat.pop(str(chat_id), None)
                    self._bluetooth_catalog_by_chat.pop(str(chat_id), None)
                    self._roll_panel_to_bottom(chat_id, "bt", message_id)
                else:
                    self._roll_panel_to_bottom(chat_id, "btpair", message_id)
            except Exception as exc:
                self._send_message(chat_id, "⚠️ " + self._tg_runtime_text(f"Bluetooth: {exc}"))
                self._roll_panel_to_bottom(chat_id, "btpair", message_id)
            return
        if data.startswith("a:btdev:"):
            try:
                _a, _kind, desired, raw_index = data.split(":", 3)
                index = int(raw_index)
                catalog = self._bluetooth_catalog_by_chat.get(str(chat_id), [])
                device = catalog[index]
                ok, text = set_device_connection(str(device.get("name", "") or ""), desired == "on")
                self._send_message(chat_id, ("✅ " if ok else "⚠️ ") + self._tg_runtime_text(text))
            except Exception as exc:
                self._send_message(chat_id, "⚠️ " + self._tg_runtime_text(f"Bluetooth: {exc}"))
            self._roll_panel_to_bottom(chat_id, "bt", message_id); return
        if data.startswith("a:bt:"):
            enabled = data.endswith(":1")
            self._execute_tool(chat_id, user_id, "system", {"action": "set_bluetooth", "enabled": enabled}, f"Bluetooth {'ON' if enabled else 'OFF'}", panel_message_id=message_id, return_view="bt"); return
        if data.startswith("a:bright:"):
            level = int(data.rsplit(":", 1)[1])
            self._execute_tool(chat_id, user_id, "system", {"action": "set_brightness", "value": level}, f"Яркость {level}%", panel_message_id=message_id, return_view="bright"); return
        if data == "a:display:off":
            self._execute_tool(chat_id, user_id, "system", {"action": "turn_off_displays"}, "Выключить дисплеи", panel_message_id=message_id, return_view="bright"); return

        if data == "a:sys:taskmgr":
            self._execute_tool_now(chat_id, "system", {"action": "open_task_manager"}, "Открыть диспетчер задач", panel_message_id=message_id, return_view="sys"); return
        if data.startswith("a:power:"):
            key = data.rsplit(":", 1)[1]
            if key == "get":
                self._execute_tool_now(chat_id, "system", {"action": "get_power_plan"}, "Текущий план питания", panel_message_id=message_id, return_view="power"); return
            plans = {"bal": "balanced", "perf": "high performance", "save": "power saver"}
            if key in plans:
                self._execute_tool(chat_id, user_id, "system", {"action": "set_power_plan", "power_plan": plans[key]}, f"План питания · {plans[key]}", panel_message_id=message_id, return_view="power"); return

        if data == "a:sys:lock":
            self._execute_tool(chat_id, user_id, "system", {"action": "lock"}, "Заблокировать компьютер", panel_message_id=message_id, return_view="sys"); return
        if data == "a:sys:unlock":
            self._request_secure_action(kind="unlock", chat_id=chat_id, user_id=user_id, label="Разблокировать компьютер", panel_message_id=message_id, return_view="sys"); return
        if data == "a:sys:sleep":
            self._show_plain_confirmation(chat_id, message_id, self._tg_message("Подтвердить действие питания Windows?"), {"mode":"secure_tool","tool_name":"system","arguments":{"action":"sleep"},"label":"Перевести компьютер в сон","return_view":"sys"}, yes_label="✅ Сон", no_label="❌ Отмена", view="sys"); return
        if data == "a:sys:hib":
            self._show_plain_confirmation(chat_id, message_id, self._tg_message("Подтвердить действие питания Windows?"), {"mode":"secure_tool","tool_name":"system","arguments":{"action":"hibernate"},"label":"Перевести компьютер в гибернацию","return_view":"sys"}, yes_label="✅ Гибернация", no_label="❌ Отмена", view="sys"); return
        if data == "a:sys:restart":
            self._show_plain_confirmation(chat_id, message_id, self._tg_message("Подтвердить действие питания Windows?"), {"mode":"secure_tool","tool_name":"system","arguments":{"action":"restart","delay_seconds":10},"label":"Перезагрузить компьютер","return_view":"sys"}, yes_label="✅ Перезагрузить", no_label="❌ Отмена", view="sys"); return
        if data == "a:sys:off":
            self._show_plain_confirmation(chat_id, message_id, self._tg_message("Подтвердить действие питания Windows?"), {"mode":"secure_tool","tool_name":"system","arguments":{"action":"shutdown","delay_seconds":10},"label":"Выключить компьютер","return_view":"sys"}, yes_label="✅ Выключить ПК", no_label="❌ Отмена", view="sys"); return

        # Obsolete HOTFIX54/55 confirmation buttons fail closed.
        if data.startswith("jr:"):
            self._answer_callback(callback_id, self._tg_message("Старое подтверждение недействительно"))
            self._show_home(chat_id, message_id)
            return

        self._answer_callback(callback_id, self._tg_message("Неизвестное действие"))

    # ------------------------------------------------------------- cancellation
    def _cancel_secure_silently(self, user_id: str, chat_id: str) -> None:
        pending = self._secure_pending
        if pending is None or pending.user_id != user_id or pending.chat_id != chat_id:
            return
        self._secure_pending = None
        self._input = None
        if pending.kind == "agent":
            try:
                self._discard_agent_confirmation184(pending, chat_id)
            except Exception:
                pass

    def _cancel_all(self, chat_id: str, user_id: str, panel_message_id: int = 0) -> None:
        pending = self._secure_pending
        return_view = pending.return_view if pending is not None else self._view_by_chat.get(str(chat_id), "home")
        self._input = None
        self._secure_pending = None
        self._plain_confirm_by_chat.pop(str(chat_id), None)
        if pending is not None and pending.user_id == user_id and pending.chat_id == chat_id:
            if pending.kind == "agent":
                try:
                    self._discard_agent_confirmation184(pending, chat_id)
                except Exception:
                    pass
            self._send_message(chat_id, self._tg_message("Действие отменено."))
            self._notify_desktop(f"📱 Telegram · действие «{pending.label}» отменено.")
        else:
            self._send_message(chat_id, self._tg_message("Ожидающее действие отменено."))
        if panel_message_id:
            self._roll_panel_to_bottom(chat_id, "home", panel_message_id)

    # ------------------------------------------------------------- special UI
    def _screen(self, chat_id: str, monitor: str) -> None:
        try:
            result = self.agent.tools.execute("screen", {"action": "capture", "monitor": monitor})
            if not result.success:
                self._send_message(chat_id, result.text)
                return
            data = result.data if isinstance(result.data, dict) else {}
            path = str(data.get("path", "") or data.get("screenshot", "") or "")
            if not path:
                self._send_message(chat_id, result.text)
                return
            label = "всех экранов" if monitor == "all" else f"экрана {monitor}"
            self._send_photo(chat_id, path, f"Скриншот {label} · NEO")
        except Exception as exc:
            logger.exception("Telegram screen capture failed")
            self._send_message(chat_id, self._tg_message(f"Не удалось отправить скриншот: {exc}"))

    def _window_screen_text(self) -> str:
        if os.name != "nt":
            return "Список окон по экранам доступен только в Windows."
        try:
            import pygetwindow as gw
            from jarvis.display_mapping import windows_monitor_geometries
            native = windows_monitor_geometries()
            rows: list[tuple[int, str]] = []
            seen: set[str] = set()
            for window in gw.getAllWindows():
                title = str(getattr(window, "title", "") or "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                try:
                    cx = float(window.left) + max(1.0, float(window.width)) / 2.0
                    cy = float(window.top) + max(1.0, float(window.height)) / 2.0
                except Exception:
                    continue
                number = 0
                for ordinal, (_win_number, rect, _device) in enumerate(native, start=1):
                    left, top, right, bottom = [float(x) for x in rect]
                    if left <= cx < right and top <= cy < bottom:
                        number = ordinal; break
                if number:
                    rows.append((number, title))
            rows = rows[:28]
            if not rows:
                return "🖥 Открытые окна по экранам не найдены."
            lines = ["🖥 Окна по экранам"]
            current = None
            for number, title in sorted(rows, key=lambda x: (x[0], x[1].casefold())):
                if number != current:
                    current = number
                    lines.append(f"\nЭкран {number}:")
                lines.append("• " + (title if len(title) <= 70 else title[:67] + "…"))
            return "\n".join(lines)
        except Exception as exc:
            return f"Не удалось получить окна по экранам: {exc}"

    @staticmethod
    def _friendly_process_name(name: str) -> str:
        base = str(name or "process").strip()
        key = base.casefold().removesuffix(".exe")
        aliases = {
            "opera": "Opera", "opera_gx_splash": "Opera GX", "firefox": "Mozilla Firefox",
            "chrome": "Google Chrome", "msedge": "Microsoft Edge", "discord": "Discord",
            "steam": "Steam", "steamwebhelper": "Steam", "telegram": "Telegram",
            "spotify": "Spotify", "code": "Visual Studio Code", "explorer": "Проводник",
            "obs64": "OBS Studio", "vlc": "VLC", "python": "Python", "pythonw": "NEO",
        }
        return aliases.get(key, base.removesuffix(".exe") or "process")

    def _process_table_text(self) -> str:
        """Return real processes, not grouped/friendly application aliases."""
        try:
            result = self.agent.tools.execute("system", {"action": "top_processes", "sort_by": "memory", "limit": 40})
            raw = result.data if result.success and isinstance(result.data, list) else []
        except Exception as exc:
            return f"Не удалось получить сведения о процессах: {exc}"
        logical = max(1, int(psutil.cpu_count(logical=True) or 1))
        rows: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            cpu = min(100.0, float(item.get("cpu_percent", 0) or 0) / logical)
            gpu_raw = item.get("gpu_percent", None)
            gpu = None if gpu_raw is None else max(0.0, min(100.0, float(gpu_raw or 0)))
            ram = float(item.get("memory_rss_mb", 0) or 0)
            disk_raw = item.get("disk_mbps", None)
            # NEO 187 (TPM03): unavailable I/O is not a measured zero.
            disk = None if disk_raw is None else max(0.0, float(disk_raw or 0))
            pid = int(item.get("pid", 0) or 0)
            name = str(item.get("name", "") or "process")
            score = cpu * 28.0 + (gpu or 0.0) * 30.0 + min(16000.0, ram) / 6.0 + (disk or 0.0) * 5.0
            rows.append({"name": name, "cpu": cpu, "gpu": gpu, "ram": ram, "disk": disk, "pid": pid, "score": score})
        rows.sort(key=lambda row: float(row["score"]), reverse=True)
        rows = rows[:18]
        if not rows:
            return "Нет доступных процессов."

        # Telegram controls bubble width itself. A PRE block gives a horizontal,
        # monospace table where desktop clients can show/scroll a wider row. Keep
        # headers compact so long executable names still receive 22 characters.
        lines = [
            "ПРОЦЕСС                 CPU   GPU    RAM   ДИСК    PID",
            "────────────────────── ───── ───── ────── ────── ──────",
        ]
        for row in rows:
            name = str(row["name"])
            if len(name) > 22:
                name = name[:21] + "…"
            ram_mb = float(row["ram"])
            ram = f"{ram_mb/1024:.1f}G" if ram_mb >= 1024 else f"{ram_mb:.0f}M"
            disk = "—" if row["disk"] is None else (f"{float(row['disk']):.1f}M" if float(row["disk"]) >= 0.05 else "0")
            gpu = "—" if row["gpu"] is None else f"{float(row['gpu']):.0f}%"
            lines.append(
                f"{name:<22} {float(row['cpu']):>4.1f}% {gpu:>5} {ram:>6} {disk:>6} {int(row['pid']):>6}"
            )
        vm = psutil.virtual_memory()
        lines.extend(["", f"RAM: {vm.used/1024**3:.1f}/{vm.total/1024**3:.1f} GB · {vm.percent:.0f}%", "GPU = загрузка процесса, если драйвер NVIDIA её сообщает."])
        return "\n".join(lines)

    def _send_process_table(self, chat_id: str) -> None:
        table = self._process_table_text()
        body = "<b>📋 Основные процессы</b>\n<pre>" + html.escape(table) + "</pre>"
        self._send_message(chat_id, body, parse_mode="HTML")
        self._notify_desktop("📱 Telegram · запрошены сведения о процессах.")

    def _send_status(self, chat_id: str) -> None:
        self._send_message(chat_id, self._status_text())
        self._notify_desktop("📱 Telegram · запрошены сведения о системе.")

    def _status_text(self) -> str:
        try:
            result = self.agent.tools.execute("system", {"action": "status"})
            if result.success:
                parts = [str(result.text or "Состояние компьютера")]
            else:
                parts = [f"Сведения о системе недоступны: {result.text}"]
            parts.append(f"Windows host: {platform.node() or 'PC'}")
            parts.append(f"Telegram: {'подключён' if self.connected else 'переподключение'}")
            if os.name == "nt":
                try:
                    from jarvis.windows_voice_unlock import is_current_session_locked
                    parts.append("Сессия: 🔒 заблокирована" if is_current_session_locked() else "Сессия: 🔓 разблокирована")
                except Exception:
                    pass
            try:
                battery = psutil.sensors_battery()
                if battery is not None:
                    parts.append(f"Батарея: {battery.percent:.0f}%{' · питание от сети' if battery.power_plugged else ''}")
            except Exception:
                pass
            return "\n\n".join(parts)
        except Exception as exc:
            return f"NEO работает, но подробные сведения о системе недоступны: {exc}"

    def _settings_text(self) -> str:
        users = ", ".join(sorted(self._allowed_user_ids())) or "—"
        blocked = ", ".join(sorted(self.policy._blocked_tools())) or "—"
        cleanup = bool(self.config.get("integrations.telegram.chat_cleanup_enabled", True))
        interval = int(self.config.get("integrations.telegram.chat_cleanup_interval_minutes", 30) or 30)
        return (
            "🔐 Telegram Remote\n"
            f"Владелец user ID: {users}\n"
            "Чат: только private\n"
            f"Кодовая фраза: {'настроена' if self._critical_phrase_ready() else 'НЕ настроена'}\n"
            f"Автоочистка: {'ON' if cleanup else 'OFF'}" + (f" · каждые {interval} мин" if cleanup else "") + "\n"
            f"Зеркало действий в NEO: {'ON' if bool(self.config.get('integrations.telegram.mirror_actions_to_desktop', True)) else 'OFF'}\n"
            f"Заблокированные инструменты: {blocked}"
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "🤖 NEO Telegram Remote\n\n"
            "Основное управление — кнопками в сообщении-панели. Кнопка «Домой» возвращает в начало. "
            "Текстовые команды Phrase Atlas тоже доступны.\n\n"
            "/home — открыть панель\n"
            "/status — состояние ПК\n"
            "/screen — выбор экрана\n"
            "/cancel — отменить ввод/критическое действие\n"
            "/settings — состояние Telegram Remote\n\n"
            "Критические действия требуют локально настроенную кодовую фразу."
        )
