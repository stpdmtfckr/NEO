# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
from jarvis.runtime.distribution164 import worker_command


class BackgroundLocalPlayer:
    """Controller for the headless Qt Multimedia worker.

    No foreground window is created and no input event is synthesized.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._port = 0
        self._status_guard = threading.Lock()
        self._status_cache: dict[str, Any] = {}
        self._status_at = 0.0
        self._status_requested_at = 0.0
        self._status_inflight = False
        self._status_epoch = 0
        self._status_failures = 0
        self._worker_generation = 0
        atexit.register(self.close)

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _alive(self) -> bool:
        return self._process is not None and self._process.poll() is None and self._port > 0

    def _ensure(self) -> None:
        with self._lock:
            if self._alive():
                return
            self._port = self._free_port()
            root = Path(__file__).resolve().parents[3]
            self._worker_generation += 1
            self._process = subprocess.Popen(
                worker_command("jarvis.integrations.background.local_player_worker",
                               args=("--port", str(self._port), "--parent-lifeline")),
                cwd=str(root),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **hidden_process_kwargs(),
            )
        deadline = time.monotonic() + 6.0
        last_error = ""
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise RuntimeError(f"Фоновый плеер NEO завершился с кодом {self._process.returncode}")
            try:
                self._request({"action": "status"}, ensure=False, timeout=0.5)
                return
            except Exception as exc:
                last_error = str(exc)
                time.sleep(0.08)
        raise RuntimeError(f"Фоновый плеер NEO не запустился: {last_error or 'timeout'}")

    def _request(self, payload: dict[str, Any], *, ensure: bool = True, timeout: float = 3.5,
                 before_send: Callable[[], None] | None = None) -> dict[str, Any]:
        # Serialize only command submission. Verification/status polling must not
        # hold this lane: a user pause can interrupt seven-second play verification.
        # A source's generation check and its play IPC are one ordered operation
        # relative to pause/stop IPC, closing the check-then-send race.
        if payload.get("action") == "status":
            return self._request_unlocked(payload, ensure=ensure, timeout=timeout)
        if ensure:
            self._ensure()
        with self._lock:
            if before_send is not None:
                before_send()
            return self._request_unlocked(payload, ensure=False, timeout=timeout)

    def _request_unlocked(self, payload: dict[str, Any], *, ensure: bool = True, timeout: float = 3.5) -> dict[str, Any]:
        if ensure:
            self._ensure()
        with self._status_guard:
            if payload.get("action") != "status":
                self._status_epoch += 1
            epoch = self._status_epoch
        if not self._alive():
            raise RuntimeError("Фоновый плеер NEO недоступен")
        with socket.create_connection(("127.0.0.1", self._port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            file = sock.makefile("rb")
            line = file.readline(1024 * 1024)
        if not line:
            raise RuntimeError("Фоновый плеер NEO не вернул ответ")
        data = json.loads(line.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("Некорректный ответ фонового плеера")
        if not data.get("ok", False):
            raise RuntimeError(str(data.get("error", "Ошибка фонового плеера")))
        with self._status_guard:
            self._status_failures = 0
            if epoch == self._status_epoch:
                self._status_cache = dict(data)
                self._status_at = time.monotonic()
        return data

    def cached_status(self, *, max_age: float = 2.5) -> dict[str, Any]:
        """RAM-only UI snapshot. At most one short passive probe in flight.

        Does not start the player. A failed probe is UNKNOWN, not a claim that
        audio was successfully stopped. Old probes cannot overwrite a command.
        """
        now = time.monotonic()
        with self._status_guard:
            if not self._alive():
                self._status_cache = {}
                return {"state": "stopped", "worker_alive": False}
            result = dict(self._status_cache)
            age = now - self._status_at
            if age > max_age:
                result = {"state": "unknown", "worker_alive": True, "stale": True}
            interval = 0.2 if result.get("state") == "playing" else 0.75
            launch = not self._status_inflight and now - self._status_requested_at >= interval
            if launch:
                self._status_inflight = True
                self._status_requested_at = now
        if launch:
            def probe() -> None:
                try:
                    self._request({"action": "status"}, ensure=False, timeout=0.4)
                except (OSError, RuntimeError, ValueError):
                    pass  # Snapshot ages into UNKNOWN; never block Qt on a retry.
                finally:
                    with self._status_guard:
                        self._status_inflight = False
            threading.Thread(target=probe, name="NEO-MediaSnapshot", daemon=True).start()
        result["snapshot_age_seconds"] = age
        return result

    def play(self, url: str, *, title: str = "", artist: str = "", fast_verify: bool = False,
             before_start: Callable[[], None] | None = None) -> dict[str, Any]:
        self._request({"action": "play", "url": url, "title": title, "artist": artist}, before_send=before_start)
        # HOTFIX45: a spoken next/previous command must not wait several seconds
        # for Qt's coarse positionChanged cadence before the command returns.
        # In fast mode we still require a real Playing state and either a small
        # timeline movement or a stable loaded/buffered media state, but cap the
        # hot-path verification window aggressively. Automatic playback keeps the
        # conservative seven-second verification used for recovery.
        started = time.monotonic()
        deadline = started + (2.60 if fast_verify else 7.0)
        last: dict[str, Any] = {}
        first_play_position: int | None = None
        while time.monotonic() < deadline:
            last = self.status()
            if last.get("error"):
                error_type = ConnectionError if last.get("error_kind") == "network" else RuntimeError
                raise error_type(str(last["error"]))
            media = str(last.get("media_status", "") or "").casefold()
            if "invalid" in media:
                raise RuntimeError("Qt Multimedia отклонил аудиопоток")
            if fast_verify and time.monotonic() - started >= 1.15 and not last.get("error"):
                # Qt can remain Stopped+LoadingMedia for >1 s while replacing a
                # remote HTTPS source. If the worker already reports the exact
                # requested metadata, this is the same selected song starting,
                # not permission to pop another queue item.
                same_title = not title or str(last.get("title", "") or "").strip() == str(title).strip()
                same_artist = not artist or str(last.get("artist", "") or "").strip() == str(artist).strip()
                if same_title and same_artist and any(token in media for token in ("loading", "loaded", "buffering", "buffered")):
                    return {**last, "verified": True, "starting": True, "timeline_verified": False, "fast_verified": True, "non_invasive": True}
            if str(last.get("state", "")) == "playing":
                pos = int(last.get("position_ms", 0) or 0)
                if first_play_position is None:
                    first_play_position = pos
                # PlaybackState alone can be optimistic while the backend has not
                # actually started consuming audio. Require observable timeline
                # progress before claiming success to the user.
                if pos >= max(40, first_play_position + 35):
                    return {**last, "verified": True, "timeline_verified": True, "non_invasive": True,
                        "unchanged": False}
                if fast_verify:
                    media = str(last.get("media_status", "") or "").casefold()
                    if any(token in media for token in ("loaded", "buffered", "buffering")):
                        return {**last, "verified": True, "timeline_verified": False, "fast_verified": True, "non_invasive": True}
            time.sleep(0.020 if fast_verify else 0.025)
        # If Qt already entered Playing but has not emitted a position tick yet,
        # accept that state for a manual transport command. The station monitor
        # still guards against a dead/ended source and can recover separately.
        if fast_verify and str(last.get("state", "") or "").casefold() == "playing" and not last.get("error"):
            return {**last, "verified": True, "timeline_verified": False, "fast_verified": True, "non_invasive": True}
        return {**last, "verified": False, "requested": True, "timeline_verified": False, "non_invasive": True}

    def _wait_for_state(self, expected: str, *, timeout: float = 2.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.1, float(timeout))
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self._request({"action": "status"}, ensure=False, timeout=0.8)
            if str(last.get("state", "") or "").casefold() == expected.casefold():
                return {**last, "verified": True, "non_invasive": True}
            if last.get("error"):
                raise RuntimeError(str(last.get("error")))
            time.sleep(0.05)
        return {**last, "verified": False, "requested": True, "non_invasive": True}

    def pause(self, *, before_send: Callable[[], None] | None = None) -> dict[str, Any]:
        sent = self._request({"action": "pause"}, before_send=before_send)
        state = self._wait_for_state("paused")
        return {**state, "unchanged": bool(state.get("verified") and sent.get("state_before") == "paused")}

    def resume(self, *, before_send: Callable[[], None] | None = None) -> dict[str, Any]:
        before = self.status_if_running() or {}
        before_pos = int(before.get("position_ms", 0) or 0)
        self._request({"action": "resume"}, before_send=before_send)
        state = self._wait_for_state("playing", timeout=2.4)
        if not bool(state.get("verified")):
            return state
        deadline = time.monotonic() + 1.6
        last = state
        while time.monotonic() < deadline:
            last = self.status()
            if int(last.get("position_ms", 0) or 0) >= before_pos + 70:
                return {**last, "verified": True, "timeline_verified": True, "non_invasive": True}
            time.sleep(0.06)
        return {**last, "verified": False, "timeline_verified": False, "requested": True, "non_invasive": True}

    def stop(self, *, before_send: Callable[[], None] | None = None) -> dict[str, Any]:
        sent = self._request({"action": "stop"}, before_send=before_send)
        state = self._wait_for_state("stopped")
        return {**state, "unchanged": bool(state.get("verified") and sent.get("state_before") == "stopped")}

    def guarded_transport(self, action: str, *, before_send: Callable[[], None]) -> dict[str, Any]:
        if action not in {"pause", "resume", "stop"}:
            raise ValueError("Unsupported guarded transport")
        return getattr(self, action)(before_send=before_send)

    def seek(self, position_ms: int, *, before_send: Callable[[], None] | None = None) -> dict[str, Any]:
        """Restore a recovery position without issuing another play command."""
        return self._request({"action": "seek", "position_ms": max(0, int(position_ms))}, before_send=before_send)


    def guarded_volume(self, percent: int | float, *, before_send: Callable[[], None]) -> dict[str, Any]:
        return self.set_volume(percent, before_send=before_send)

    def set_volume(self, percent: int | float, *, before_send: Callable[[], None] | None = None) -> dict[str, Any]:
        value = max(0.0, min(100.0, float(percent)))
        data = self._request({"action": "set_volume", "volume": value / 100.0}, before_send=before_send)
        return {**data, "volume_percent": int(round(float(data.get("volume", value / 100.0)) * 100.0)), "verified": True, "non_invasive": True}

    def refresh_output_device(self) -> dict[str, Any] | None:
        """Move an already-running hidden player to the new OS default output.

        A device switch must be a no-op when JARVIS has not started background
        playback; probing it must never spawn the worker.
        """
        if not self._alive():
            return None
        before = self.status_if_running() or {}
        data = self._request({"action": "refresh_output"}, ensure=False, timeout=1.5)
        return {
            **data,
            "verified": bool(data.get("rebound", False)),
            "non_invasive": True,
            "state_before": str(before.get("state", "") or ""),
            "position_before_ms": int(before.get("position_ms", 0) or 0),
        }

    def status(self) -> dict[str, Any]:
        return self._request({"action": "status"})

    def status_if_running(self) -> dict[str, Any] | None:
        """Return worker state without spawning the worker.

        Transport routing uses this method to distinguish JARVIS' own hidden
        player from an installed desktop media session. A harmless status check
        must never create a new process or steal ownership of future commands.
        """
        if not self._alive():
            return None
        try:
            return self._request({"action": "status"}, ensure=False, timeout=1.0)
        except Exception:
            with self._status_guard:
                self._status_failures += 1
            return None

    def recreate_if_unresponsive(self, *, before_restart: Callable[[], None] | None = None) -> bool:
        """Retire only this exact owned worker after two failed Qt IPC probes.

        CDN errors with responsive IPC do not qualify. A different Popen object
        or generation invalidates the diagnosis; no process-name/global kill is
        used. The following play call starts a replacement only if still wanted.
        """
        with self._lock:
            process, generation = self._process, self._worker_generation
        if process is None or process.poll() is not None:
            return False
        for _ in range(2):
            if before_restart is not None:
                before_restart()
            if self.status_if_running() is not None:
                return False
            with self._status_guard:
                failures = self._status_failures
            if failures >= 2:
                break
        with self._lock:
            if process is not self._process or generation != self._worker_generation or process.poll() is not None:
                return False
            with self._status_guard:
                if self._status_failures < 2:
                    return False
            if before_restart is not None:
                before_restart()
            self.close()  # Popen child handle, never an unrelated PID or process name.
            with self._status_guard:
                self._status_failures = 0
                self._status_cache = {}
            if before_restart is not None:
                before_restart()
            return True

    def running(self) -> bool:
        return self._alive()

    def active(self) -> bool:
        # A status probe must not *start* the worker. This keeps ordinary GSMTC
        # commands for the installed Yandex Music app instant when JARVIS has
        # not used its own background player in this session.
        status = self.status_if_running()
        return bool(status and str(status.get("state", "")) in {"playing", "paused"})

    def close(self) -> None:
        with self._lock:
            process = self._process
            if process is None:
                return
            try:
                if process.poll() is None and self._port:
                    self._request({"action": "shutdown"}, ensure=False, timeout=0.8)
            except Exception:
                pass
            try:
                process.wait(timeout=1.5)
            except Exception:
                try:
                    process.terminate()
                    process.wait(timeout=0.8)
                except Exception:
                    try:
                        process.kill()
                        process.wait(timeout=0.8)
                    except Exception:
                        pass
            stream = getattr(process, "stdin", None)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
            self._process = None
            self._port = 0


_INSTANCE: BackgroundLocalPlayer | None = None
_INSTANCE_LOCK = threading.Lock()


def local_player() -> BackgroundLocalPlayer:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = BackgroundLocalPlayer()
        return _INSTANCE
