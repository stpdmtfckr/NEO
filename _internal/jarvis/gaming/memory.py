# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Low-frequency process-verified gaming long-term memory tracker."""

import logging
from collections import deque
import os
import threading
import time
from typing import Any, Callable

import psutil

from jarvis.gaming.manager import GameManager, game_presence_service
from jarvis.gaming.runtime import record_active_game, clear_active_game
from jarvis.game_web.context import GameWebContextStore
from jarvis.runtime.context_memory import ContextMemory
from jarvis.language import game_started_message, language_code
from jarvis.runtime import game_notices169 as notices169

logger = logging.getLogger(__name__)


class GamingContextTracker:
    def __init__(
        self,
        config: Any,
        memory: ContextMemory | None = None,
        *,
        notice_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.memory = memory or ContextMemory(config)
        self.manager = GameManager(enabled=bool(config.get("gaming.enabled", True)), config=config)
        self.web_context = GameWebContextStore(config)
        self.notice_callback = notice_callback
        self._started_monotonic = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: dict[str, Any] | None = None
        self._notified_processes: set[tuple[int, float]] = set()
        self._notice_order: deque[tuple[int, float]] = deque()
        self._notice_variant = 0
        self._session_noticed: set[tuple[int, float]] = set()
        self._replay_watch = None
        self._obs_service184 = None
        # HIST-010: start/stop generation. A detection path begun before stop()
        # never admits replay/OBS/notices for the stopped owner.
        self._generation188 = 0

    def _admitted188(self, generation: int | None) -> bool:
        if self._stop.is_set():
            return False
        return generation is None or generation == self._generation188

    def _notice(self, text: str) -> None:
        if self.notice_callback is None:
            return
        try:
            self.notice_callback(str(text))
        except Exception:
            logger.debug("Gaming tracker notice callback failed", exc_info=True)

    def _replay(self):
        if self._replay_watch is None:
            from jarvis.integrations.instant_replay169 import ReplayWatch
            self._replay_watch = ReplayWatch(self.config)
        return self._replay_watch

    def start(self) -> None:
        if os.name != "nt" or not bool(self.config.get("gaming.enabled", True)):
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._generation188 += 1
        self._thread = threading.Thread(target=self._run, name="NeoJarvis-GamingMemory", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._generation188 += 1
        if self._obs_service184 is not None:
            self._obs_service184.game_ended()
        if self._replay_watch is not None:
            self._replay_watch.game_ended()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _finish_if_dead(self) -> None:
        current = self._current
        if not current:
            return
        pid = int(current.get("pid", 0) or 0)
        started = float(current.get("session_started_at", 0.0) or 0.0)
        try:
            process = psutil.Process(pid) if pid > 0 else None
            alive = bool(process and process.is_running() and abs(process.create_time() - started) < 0.01)
        except psutil.AccessDenied:
            # Lack of permission is not proof that an observed session ended.
            return
        except (psutil.NoSuchProcess, psutil.ZombieProcess, OSError, ValueError):
            alive = False
        if alive:
            return
        try:
            if started:
                import time
                current["session_seconds"] = max(int(current.get("session_seconds", 0) or 0), int(time.time() - started))
            if self.memory.enabled():
                self.memory.record_game_session_event(current, status="ended")
        except Exception:
            logger.debug("Gaming memory session finish failed", exc_info=True)
        try:
            self.web_context.record(current, status="ended")
        except Exception:
            logger.debug("Could not persist ended game web context", exc_info=True)
        clear_active_game(pid=pid or None)
        self._current = None
        if self._obs_service184 is not None:
            self._obs_service184.game_ended()
        self._session_noticed.discard((pid, started))
        if self._replay_watch is not None:
            self._replay_watch.game_ended()
        if notices169.enabled(self.config, "session_end", False) and current.get("game_name"):
            code = notices169.code_of(self.config)
            self._notice(notices169.message("session_end", code, game=str(current.get("game_name")),
                                            duration=notices169.duration(int(current.get("session_seconds", 0) or 0), code)))

    def _on_new_game(self, data: dict[str, Any], generation: int | None = None) -> None:
        """Accept only a process-verified snapshot, once per process lifetime."""
        if not self._admitted188(generation):
            return
        game_name = str(data.get("game_name") or "").strip()
        key = (int(data.get("pid", 0) or 0), float(data.get("session_started_at", 0.0) or 0.0))
        if not game_name or key[0] <= 0 or key[1] <= 0 or key in self._notified_processes:
            return
        self._notified_processes.add(key)
        self._notice_order.append(key)
        while len(self._notice_order) > 256:
            self._notified_processes.discard(self._notice_order.popleft())
        logger.info("Active game session detected game=%s pid=%s", game_name, key[0])
        try:
            # NEO 173: the game gets the memory NEO's models were using (they load again on the wake word).
            from jarvis.runtime import model_life173
            model_life173.release_for_game()
        except Exception:
            logger.debug("Model release at game start failed", exc_info=True)
        # Preserve an explicit notification opt-out without depending on whether
        # durable context-memory recording is enabled.
        if bool(self.config.get("gaming.launch_greetings_enabled", True)):
            text = game_started_message(game_name, language_code(self.config), variant=self._notice_variant)
            self._notice_variant += 1
            self._notice(text)
        if not self._admitted188(generation):
            return
        try:
            warning = self._replay().game_started(game_name, notify=self._notice, session=key)
        except Exception:
            warning = None
            logger.debug("Instant Replay check at game start failed", exc_info=True)
        if warning:
            self._notice(warning)
        if self.config.get("streaming.enabled", False) is True and self._admitted188(generation):
            from jarvis.integrations.obs_service184 import shared
            self._obs_service184 = shared(self.config)
            self._obs_service184.game_started(key, notify=self._notice)

    def _on_session_start(self, data: dict[str, Any]) -> None:
        """The game window came to the front for the first time in this process."""
        key = (int(data.get("pid", 0) or 0), float(data.get("session_started_at", 0.0) or 0.0))
        if not data.get("foreground_verified") or key in self._session_noticed:
            return
        self._session_noticed.add(key)
        if notices169.enabled(self.config, "session_start", False):
            self._notice(notices169.message("session_start", notices169.code_of(self.config), game=str(data.get("game_name") or "")))

    def _observe_snapshot(self, data: dict[str, Any], generation: int | None = None) -> None:
        """Consume one already verified process snapshot without extra polling."""
        if not self._admitted188(generation):
            return
        new_key = (int(data.get("pid", 0) or 0), float(data.get("session_started_at", 0.0) or 0.0))
        if new_key[0] <= 0 or new_key[1] <= 0 or not str(data.get("game_name") or "").strip():
            return
        old = self._current or {}
        old_key = (int(old.get("pid", 0) or 0), float(old.get("session_started_at", 0.0) or 0.0))
        is_new = not old or old_key != new_key
        if old and old_key != new_key:
            self._finish_if_dead()
        self._current = data
        record_active_game(**data)
        # A failed history write must not consume a new session before its UI
        # greeting. Notice delivery and optional durable stores are independent.
        if is_new:
            self._on_new_game(data, generation)
        self._on_session_start(data)
        try:
            if self.memory.enabled():
                self.memory.record_game_session_event(data, status="running")
        except Exception:
            logger.debug("Could not persist active game memory", exc_info=True)
        if is_new:
            try:
                self.web_context.record(data, status="running")
            except Exception:
                logger.debug("Could not persist active game web context", exc_info=True)

    def _run(self) -> None:
        try:
            interval = float(self.config.get("context_memory.gaming_poll_seconds", 4.0) or 4.0)
        except Exception:
            interval = 4.0
        interval = max(3.0, min(30.0, interval))
        # Publish a live game snapshot immediately. The presence service is
        # process-global and TTL-cached, so Desktop/Telegram/query routing share
        # one inexpensive process view.
        presence = game_presence_service(self.manager)
        generation = self._generation188
        while not self._stop.is_set() and generation == self._generation188:
            try:
                self.manager.enabled = bool(self.config.get("gaming.enabled", True))
                snap = presence.snapshot(enabled=self.manager.enabled, max_age_seconds=min(2.0, interval), force=False)
                if snap is None:
                    self._finish_if_dead()
                else:
                    self._observe_snapshot(snap.as_dict(), generation)
                # Alt+Z moves foreground away from the game. Keep observing
                # replay while its already verified process remains alive.
                if self._current is not None and self._admitted188(generation):
                    change = self._replay().poll()
                    if change:
                        self._notice(change)
            except Exception:
                logger.debug("Gaming long-term memory poll failed", exc_info=True)
            if self._stop.wait(interval):
                break
