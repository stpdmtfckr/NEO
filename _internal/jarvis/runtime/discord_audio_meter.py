# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Crash-isolated Discord output meter.

Windows CoreAudio/COM session enumeration lives in a *child Python process*.
This is deliberate: some pycaw/comtypes/driver combinations can raise a native
access violation that Python cannot catch.  A meter failure must never take the
NEO JARVIS GUI down.  The parent only reads normalized peak values from stdout
and falls back to the shared Discord host spectrum when the helper is unavailable.
"""

import math
import os
import subprocess
import sys
import threading
import time
from typing import TextIO

from jarvis.runtime.media_levels import publish_spectrum, clear_media_levels
from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
from jarvis.runtime.distribution164 import worker_command
# Windows helper policy: CREATE_NO_WINDOW + STARTUPINFO(SW_HIDE).


class DiscordSessionMeter:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active = False
        self._stop = threading.Event()
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._restart_after = 0.0
        self._generation = 0

    @staticmethod
    def _bars(peak: float, width: int = 64) -> list[float]:
        p = max(0.0, min(1.0, float(peak)))
        if p <= 0.0005:
            return [0.0] * width
        out: list[float] = []
        for i in range(width):
            phase = i * 1.61803398875
            weight = (
                0.46
                + 0.34 * (0.5 + 0.5 * math.sin(phase))
                + 0.20 * (0.5 + 0.5 * math.sin(phase * 0.43 + 1.2))
            )
            out.append(max(0.0, min(1.0, (p ** 0.72) * weight)))
        return out

    def set_active(self, active: bool) -> None:
        value = bool(active) and os.name == "nt"
        with self._lock:
            self._active = value
            if value:
                self._ensure_helper_locked()
            else:
                clear_media_levels("discord_session")
                self._stop_helper_locked()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._active = False
            self._stop_helper_locked()
        clear_media_levels("discord_session")

    def _ensure_helper_locked(self) -> None:
        if self._stop.is_set() or not self._active or os.name != "nt":
            return
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return
        if time.monotonic() < self._restart_after:
            return
        self._generation += 1
        generation = self._generation
        try:
            proc = subprocess.Popen(
                worker_command("jarvis.runtime.discord_audio_meter_worker"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **hidden_process_kwargs(),
            )
        except Exception:
            self._proc = None
            self._restart_after = time.monotonic() + 30.0
            clear_media_levels("discord_session")
            return
        self._proc = proc
        stream = proc.stdout
        if stream is None:
            self._restart_after = time.monotonic() + 30.0
            self._stop_helper_locked()
            return
        self._reader = threading.Thread(
            target=self._read_helper,
            args=(generation, proc, stream),
            name="NeoJarvis-DiscordMeterReader",
            daemon=True,
        )
        self._reader.start()

    def _read_helper(self, generation: int, proc: subprocess.Popen[str], stream: TextIO) -> None:
        got_sample = False
        try:
            for raw in stream:
                if self._stop.is_set():
                    break
                try:
                    peak = float(str(raw).strip())
                except Exception:
                    continue
                got_sample = True
                if peak > 0.0005:
                    publish_spectrum("discord_session", self._bars(peak, 64))
                else:
                    clear_media_levels("discord_session")
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass
            with self._lock:
                if generation != self._generation:
                    return
                if self._proc is proc:
                    self._proc = None
                # A native helper crash or immediate failure is isolated here.
                # Back off instead of spawning a crash loop.
                self._restart_after = time.monotonic() + (12.0 if got_sample else 45.0)
            clear_media_levels("discord_session")

    def _stop_helper_locked(self) -> None:
        proc = self._proc
        self._proc = None
        self._generation += 1
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=0.8)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass


_GLOBAL = DiscordSessionMeter()


def discord_session_meter() -> DiscordSessionMeter:
    return _GLOBAL
