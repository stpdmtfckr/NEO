# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any

from jarvis.audio.control_worker import audio_control_executor
from jarvis.audio.providers.windows_core_audio import WindowsCoreAudioOutputProvider


@dataclass
class SessionVolume:
    pid: int
    process: str
    volume: float
    muted: bool


class AudioFocusManager:
    """Best-effort per-session ducking with one serialized Core Audio owner.

    The public manager can be called from UI, Telegram and agent worker
    threads, but pycaw/comtypes objects never leave ``jarvis-audio-control``.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stack: list[list[SessionVolume]] = []

    @staticmethod
    def _process_name(session: Any) -> str:
        try:
            return str(session.Process.name() if session.Process else "system")
        except Exception:
            return "unknown"

    def _duck_native(self, factor: float, except_norm: set[str]) -> tuple[int, list[SessionVolume]]:
        if os.name != "nt":
            return 0, []
        WindowsCoreAudioOutputProvider._imports()
        from pycaw.pycaw import AudioUtilities

        snapshot: list[SessionVolume] = []
        changed = 0
        try:
            sessions = list(AudioUtilities.GetAllSessions())
        except Exception:
            sessions = []
        for session in sessions:
            simple = getattr(session, "SimpleAudioVolume", None)
            if simple is None:
                continue
            name = self._process_name(session)
            if name.casefold() in except_norm:
                continue
            try:
                pid = int(getattr(getattr(session, "Process", None), "pid", 0) or 0)
                volume = float(simple.GetMasterVolume())
                muted = bool(simple.GetMute())
                snapshot.append(SessionVolume(pid, name, volume, muted))
                if not muted:
                    simple.SetMasterVolume(max(0.0, min(1.0, volume * factor)), None)
                    changed += 1
            except Exception:
                continue
        return changed, snapshot

    def duck(self, factor: float = 0.18, *, except_processes: set[str] | None = None) -> int:
        factor = max(0.0, min(1.0, float(factor)))
        except_norm = {x.casefold() for x in (except_processes or set())}
        changed, snapshot = audio_control_executor().submit(
            self._duck_native, factor, except_norm, timeout=8.0
        )
        with self._lock:
            self._stack.append(list(snapshot))
        return int(changed)

    def _restore_native(self, snapshot: list[SessionVolume]) -> int:
        if os.name != "nt":
            return 0
        WindowsCoreAudioOutputProvider._imports()
        from pycaw.pycaw import AudioUtilities
        try:
            sessions = list(AudioUtilities.GetAllSessions())
        except Exception:
            sessions = []
        changed = 0
        for old in snapshot:
            for session in sessions:
                try:
                    pid = int(getattr(getattr(session, "Process", None), "pid", 0) or 0)
                    name = self._process_name(session)
                    if (old.pid and pid == old.pid) or (not old.pid and name == old.process):
                        simple = session.SimpleAudioVolume
                        simple.SetMasterVolume(old.volume, None)
                        simple.SetMute(1 if old.muted else 0, None)
                        changed += 1
                        break
                except Exception:
                    continue
        return changed

    def restore(self) -> int:
        with self._lock:
            if not self._stack:
                return 0
            snapshot = self._stack.pop()
        return int(audio_control_executor().submit(self._restore_native, snapshot, timeout=8.0))


_GLOBAL = AudioFocusManager()


def audio_focus_manager() -> AudioFocusManager:
    return _GLOBAL
