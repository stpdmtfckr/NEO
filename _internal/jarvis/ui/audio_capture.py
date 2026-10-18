# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, QTimer, Qt, Signal

from jarvis.audio_backend import _audio_worker_main, _input_devices, _resample
from jarvis.config import ConfigManager
from jarvis.voice import RecordingResult
from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
from jarvis.runtime.distribution164 import worker_command
from jarvis.runtime.process_diagnostics184 import record_spawn

logger = logging.getLogger(__name__)


class AudioCaptureController(QObject):
    """Qt proxy for an isolated, restartable PortAudio subprocess.

    Both sounddevice/PortAudio and faster-whisper use native libraries. v0.9.3
    isolated Whisper but still ran PortAudio in the Qt process, so a Windows heap
    corruption (0xC0000374) terminated the whole app. This controller never imports
    sounddevice in the UI process and communicates with a helper over JSON lines.
    """

    finished = Signal(object)
    partial_ready = Signal(object)
    wake_chunk_ready = Signal(object)
    wake_probe_ready = Signal(object)
    failed = Signal(str)
    level_changed = Signal(int)
    started = Signal(str)
    recovered = Signal(str)
    worker_event = Signal(object)

    def __init__(self, config: ConfigManager, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self._process: subprocess.Popen[str] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._io_lock = threading.RLock()
        self._session = 0
        self._recording = False
        self._command_waiting158 = False
        self._mode = ""
        self._closed = False
        self._reported_failure = False
        self._last_start_payload: dict[str, Any] | None = None
        self._restart_attempts = 0
        self._last_event_at = time.monotonic()
        self._stall_recovery_at = 0.0
        # Start with the exact Windows/default endpoint. Safe MME/DirectSound
        # remapping is a *fallback*, not the normal path. RC3 enabled safe mode
        # immediately, which could select a silent duplicate endpoint even though
        # the correct microphone was listed in Settings.
        self._force_safe_host = False
        self._safe_host_index = 0
        self._process_generation = 0
        # V7.7: stdout reader blocks in the OS and emits Qt events only when the
        # isolated audio worker actually produced something. This removes the old
        # permanent 40-Hz Queue polling wake-up while preserving/improving event
        # latency. A slow watchdog remains solely for native-process crash recovery.
        self.worker_event.connect(self._handle_event)
        self._poll_timer = QTimer(self)
        self._poll_timer.setTimerType(Qt.CoarseTimer)
        self._poll_timer.setInterval(400)
        self._poll_timer.timeout.connect(self._poll_events)
        self._poll_timer.start()

    @staticmethod
    def _project_root() -> Path:
        return Path(__file__).resolve().parents[2]

    @classmethod
    def _worker_environment(cls) -> dict[str, str]:
        env = os.environ.copy()
        project_root = cls._project_root()
        old_path = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(project_root) + (os.pathsep + old_path if old_path else "")
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    @classmethod
    def available_input_devices(cls) -> list[tuple[str, str]]:
        """Query PortAudio in a throwaway child so native faults cannot kill Qt."""

        try:
            result = subprocess.run(
                worker_command("jarvis.audio_worker", args=("--list-devices",)),
                cwd=str(cls._project_root()),
                env=cls._worker_environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15.0,
                check=False,
                **hidden_process_kwargs(),
            )
        except Exception as exc:
            raise RuntimeError(f"Не удалось запустить проверку микрофонов: {exc}") from exc

        payload: dict[str, Any] | None = None
        for line in reversed(result.stdout.splitlines()):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                payload = parsed
                break
        if payload is None or not payload.get("ok"):
            detail = ""
            if payload is not None:
                detail = str(payload.get("error", ""))
            if not detail:
                detail = result.stderr.strip() or f"код завершения {result.returncode}"
            raise RuntimeError(f"Не удалось получить список микрофонов: {detail}")

        output: list[tuple[str, str]] = []
        for item in payload.get("devices", []):
            if not isinstance(item, dict):
                continue
            token = str(item.get("token", "") or "")
            name = str(item.get("name", "Микрофон") or "Микрофон")
            host = str(item.get("host_name", "") or "")
            label = f"{name} — {host}" if host else name
            if token:
                output.append((token, label))
        return output

    @classmethod
    def available_inputs(cls) -> list[str]:
        return [label for _token, label in cls.available_input_devices()]

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def mode(self) -> str:
        return self._mode

    def _start_worker_locked(self) -> None:
        if self._closed:
            raise RuntimeError("Аудиосервис уже завершён.")
        process = self._process
        if process is not None and process.poll() is None:
            return
        self._cleanup_process_locked(force=True)

        self._process = subprocess.Popen(
            worker_command("jarvis.audio_worker"),
            cwd=str(self._project_root()),
            env=self._worker_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **hidden_process_kwargs(),
        )
        self._process_generation += 1
        process = self._process
        record_spawn("microphone_capture", process.pid, self._process_generation,
                     configured_target_rate_hz=(self._last_start_payload or {}).get("target_rate"))
        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(process, self._process_generation),
            name="NeoJarvis-Audio-responses",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process,),
            name="NeoJarvis-Audio-logs",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._reported_failure = False

    def _read_stdout(
        self,
        process: subprocess.Popen[str],
        generation: int | None = None,
    ) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Audio worker returned invalid protocol data: %s", line[:240])
                    continue
                if not isinstance(payload, dict):
                    continue
                # Emitting a Qt signal from this Python reader thread is queued
                # safely to AudioCaptureController's GUI thread. No busy polling.
                if generation is not None:
                    payload["_worker_generation188"] = generation
                self.worker_event.emit(payload)
        except (OSError, ValueError):
            return

    @staticmethod
    def _read_stderr(process: subprocess.Popen[str]) -> None:
        stream = process.stderr
        if stream is None:
            return
        try:
            for line in stream:
                text = line.strip()
                if text:
                    logger.info("AUDIO | %s", text)
        except (OSError, ValueError):
            return

    def _send_locked(self, payload: dict[str, Any]) -> None:
        self._start_worker_locked()
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("Не удалось открыть канал аудиопроцесса.")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise RuntimeError(f"Аудиопроцесс неожиданно завершился: {exc}") from exc

    def _start(self, mode: str, *, _retry: bool = False, replace_current: bool = False, **extra: Any) -> None:
        try:
            with self._io_lock:
                if not _retry:
                    # During wake -> command promotion keep the same native input
                    # stream alive and only switch the worker's logical mode. This
                    # avoids a rapid stop/start handshake at the exact moment a wake
                    # word is detected (the last remaining native-crash hotspot).
                    if self._recording and not replace_current:
                        self.stop(emit_result=False)
                    self._session += 1
                    self._restart_attempts = 0
                self._recording = True
                self._command_waiting158 = False
                self._mode = mode
                payload = {
                    "cmd": "start",
                    "session": self._session,
                    "mode": mode,
                    "device": str(self.config.get("voice.input_device", "") or ""),
                    "target_rate": int(self.config.get("voice.sample_rate", 16000)),
                    "sensitivity": int(self.config.get("voice.sensitivity", 90)),
                    "noise_rejection_percent": int(self.config.get("voice.noise_rejection_percent", 55)),
                    "safe_mode": bool(self._force_safe_host),
                    "host_fallback": int(self._safe_host_index),
                }
                payload.update(extra)
                self._last_start_payload = dict(payload)
                self._last_event_at = time.monotonic()
                self._send_locked(payload)
        except Exception as exc:
            self._recording = False
            self._mode = ""
            self.failed.emit(f"Не удалось запустить микрофон: {exc}")

    def start_command(self, *, seamless: bool = False, resume_from_last_finish: bool = False) -> None:
        self._start(
            "command",
            replace_current=bool(seamless),
            resume_from_last_finish=bool(resume_from_last_finish),
            max_seconds=float(self.config.get("voice.max_record_seconds", 16.0)),
            start_timeout=float(self.config.get("voice.start_timeout_seconds", 6.0)),
            silence_seconds=float(self.config.get("voice.silence_seconds", 1.25)),
            partial_interval=float(self.config.get("voice.live_partial_seconds", 2.0)),
            partial_max_updates=int(self.config.get("voice.live_partial_max_updates", 1)),
            command_min_voice_ms=int(self.config.get("voice.command_min_voice_ms", 120)),
            ignore_nonlinguistic_noise=bool(self.config.get("voice.ignore_nonlinguistic_noise", True)),
            adaptive_endpoint=bool(self.config.get("voice.adaptive_endpoint_enabled", True)),
            short_silence_seconds=float(self.config.get("voice.short_command_silence_seconds", 0.40)),
            normal_silence_seconds=float(self.config.get("voice.normal_command_silence_seconds", 0.68)),
            preroll_seconds=float(self.config.get("wake_word.preroll_seconds", 1.15)),
        )

    def promote_to_command(self, recording: RecordingResult | None = None) -> bool:
        """Atomically switch an existing wake stream into command mode.

        This never closes/reopens PortAudio. The helper increments the logical
        session so stale wake events are ignored while the native InputStream stays
        untouched. It is the stable path used directly after a wake-word match.
        """
        try:
            with self._io_lock:
                if not self._recording or self._mode != "wake":
                    return False
                self._session += 1
                self._restart_attempts = 0
                self._recording = True
                self._mode = "command"
                start_payload = {
                    "cmd": "start",
                    "session": self._session,
                    "mode": "command",
                    "device": str(self.config.get("voice.input_device", "") or ""),
                    "target_rate": int(self.config.get("voice.sample_rate", 16000)),
                    "sensitivity": int(self.config.get("voice.sensitivity", 90)),
                    "noise_rejection_percent": int(self.config.get("voice.noise_rejection_percent", 55)),
                    "safe_mode": bool(self._force_safe_host),
                    "host_fallback": int(self._safe_host_index),
                    "max_seconds": float(self.config.get("voice.max_record_seconds", 16.0)),
                    "start_timeout": max(3.0, min(12.0, float(self.config.get("voice.wake_command_start_timeout_seconds", 6.0)))),
                    "silence_seconds": float(self.config.get("voice.silence_seconds", 1.25)),
                    "partial_interval": float(self.config.get("voice.live_partial_seconds", 2.0)),
                    "partial_max_updates": int(self.config.get("voice.live_partial_max_updates", 1)),
                    "command_min_voice_ms": int(self.config.get("voice.command_min_voice_ms", 120)),
                    "ignore_nonlinguistic_noise": bool(self.config.get("voice.ignore_nonlinguistic_noise", True)),
                    "adaptive_endpoint": bool(self.config.get("voice.adaptive_endpoint_enabled", True)),
                    "short_silence_seconds": float(self.config.get("voice.short_command_silence_seconds", 0.40)),
                    "normal_silence_seconds": float(self.config.get("voice.normal_command_silence_seconds", 0.68)),
                    "preroll_seconds": float(self.config.get("wake_word.preroll_seconds", 1.15)),
                }
                self._last_start_payload = dict(start_payload)
                self._last_event_at = time.monotonic()
                promote_payload = dict(start_payload)
                promote_payload["cmd"] = "promote"
                if recording is not None:
                    promote_payload["capture_started_at"] = recording.capture_started_at
                    promote_payload["completed_utterance"] = recording.completed_utterance
                    promote_payload["wake_captured_at"] = recording.captured_at
                    promote_payload["wake_has_command"] = recording.wake_has_command
                self._send_locked(promote_payload)
            return True
        except Exception as exc:
            self._recording = False
            self._mode = ""
            self.failed.emit(f"Не удалось переключить микрофон после wake word: {exc}")
            return False

    def start_wake_monitor(
        self,
        *,
        window_seconds: float = 3.20,
        emit_interval: float = 0.58,
    ) -> None:
        acoustic_probe = bool(self.config.get("wake_word.acoustic_enabled", False))
        fast_asr_probe = bool(self.config.get("wake_word.fast_asr_probe_enabled", True))
        probe_interval = float(
            self.config.get(
                "wake_word.probe_interval_seconds" if acoustic_probe else "wake_word.fast_asr_probe_interval_seconds",
                0.18 if acoustic_probe else 0.46,
            )
        )
        self._start(
            "wake",
            wake_window=float(window_seconds),
            wake_silence=float(emit_interval),
            wake_probe_enabled=bool(acoustic_probe or fast_asr_probe),
            wake_probe_interval=probe_interval,
            wake_probe_min_voice_seconds=float(self.config.get("wake_word.fast_asr_probe_min_audio_seconds", 0.22)),
            wake_probe_max_per_utterance=int(self.config.get("wake_word.fast_asr_probe_max_per_utterance", 1)),
            wake_probe_window_seconds=float(self.config.get("wake_word.fast_asr_probe_window_seconds", 0.78)),
            preroll_seconds=float(self.config.get("wake_word.preroll_seconds", 1.15)),
            max_seconds=24 * 60 * 60,
        )

    def start_fixed(self, seconds: float) -> None:
        self._start("fixed", fixed_seconds=max(0.8, float(seconds)))

    def stop(self, *, emit_result: bool = True) -> None:
        if not self._recording and not getattr(self, "_command_waiting158", False):
            return
        self._command_waiting158 = False
        try:
            with self._io_lock:
                process = self._process
                if process is not None and process.poll() is None and process.stdin is not None:
                    process.stdin.write(
                        json.dumps(
                            {
                                "cmd": "stop",
                                "session": self._session,
                                "emit_result": emit_result,
                            }
                        )
                        + "\n"
                    )
                    process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        if not emit_result:
            self._recording = False
            self._mode = ""
            self.level_changed.emit(0)

    def restart_backend(self) -> None:
        self._recording = False
        self._mode = ""
        self._last_start_payload = None
        self._restart_attempts = 0
        self._safe_host_index = 0
        # Start with the exact Windows/default endpoint. Safe MME/DirectSound
        # remapping is a *fallback*, not the normal path. RC3 enabled safe mode
        # immediately, which could select a silent duplicate endpoint even though
        # the correct microphone was listed in Settings.
        self._force_safe_host = False
        with self._io_lock:
            self._cleanup_process_locked(force=True)
            # HIST-021: everything still queued from the replaced worker or its
            # session is stale now (generation/session are compared on arrival).
            self._process_generation += 1
            self._session += 1

    def _restart_after_native_crash(self, payload: dict[str, Any], generation: int) -> None:
        if self._closed or generation != self._process_generation:
            return
        if not self._recording or int(payload.get("session", -1)) != self._session:
            return
        try:
            with self._io_lock:
                retry_payload = dict(payload)
                retry_payload["safe_mode"] = True
                retry_payload["host_fallback"] = int(self._safe_host_index)
                self._last_start_payload = retry_payload
                self._send_locked(retry_payload)
            self.recovered.emit(
                "Аудиодрайвер перезапущен в безопасном режиме (MME/DirectSound)."
            )
        except Exception as exc:
            self._recording = False
            self._mode = ""
            self.failed.emit(f"Не удалось восстановить микрофон после сбоя: {exc}")

    def _poll_events(self) -> None:
        process = self._process
        if process is not None:
            exit_code = process.poll()
            if exit_code is not None and not self._closed:
                was_recording = self._recording
                payload = dict(self._last_start_payload or {})
                with self._io_lock:
                    self._cleanup_process_locked(force=True)
                if was_recording and payload and self._restart_attempts < 1:
                    self._restart_attempts += 1
                    self._force_safe_host = True
                    self._safe_host_index += 1
                    # Generation changes when the replacement process starts. Capture
                    # the expected value after cleanup and before restart.
                    expected_generation = self._process_generation
                    logger.warning(
                        "Audio worker crashed with code %s; retrying in safe host mode",
                        exit_code,
                    )
                    QTimer.singleShot(
                        350,
                        lambda p=payload, g=expected_generation: self._restart_after_native_crash(p, g),
                    )
                else:
                    self._recording = False
                    self._mode = ""
                    self.level_changed.emit(0)
                    if not self._reported_failure:
                        self._reported_failure = True
                        self.failed.emit(
                            "Нативный аудиодрайвер аварийно завершился "
                            f"(код {exit_code}). Приложение продолжает работать. "
                            "Выберите другой вариант этого же микрофона в настройках."
                        )
                return

            # A native PortAudio read can occasionally remain alive while no
            # samples/events reach Python.  Since every active wake/command mode
            # emits meter events, silence itself is not a reason for an empty
            # protocol. Self-heal a genuinely stalled worker instead of requiring
            # an application restart after hours of use.
            if self._recording and exit_code is None:
                now = time.monotonic()
                if now - self._last_event_at > 4.5 and now - self._stall_recovery_at > 12.0:
                    payload = dict(self._last_start_payload or {})
                    if payload:
                        self._stall_recovery_at = now
                        try:
                            with self._io_lock:
                                self._cleanup_process_locked(force=True)
                                self._recording = True
                                self._mode = str(payload.get("mode", self._mode) or self._mode)
                                self._last_event_at = time.monotonic()
                                self._send_locked(payload)
                            logger.warning("Audio worker produced no events for 4.5s; restarted in-place")
                            self.recovered.emit("Аудиопоток автоматически восстановлен после зависания.")
                        except Exception as exc:
                            logger.warning("Audio stalled-worker recovery failed: %s", exc)

    def _handle_event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        event_type = str(event.get("type", ""))
        worker_generation = event.get("_worker_generation188")
        if worker_generation is not None and int(worker_generation) != self._process_generation:
            # HIST-021: a queued event of a replaced worker (e.g. its error after an
            # STT switch) neither ends nor holds the capture of the new worker.
            logger.debug("Ignoring %s event of replaced audio worker generation %s", event_type, worker_generation)
            return
        if event_type == "backend_error":
            self._recording = False
            self._mode = ""
            self._reported_failure = True
            self.failed.emit(str(event.get("message", "Ошибка аудиомодуля")))
            return
        if event_type == "warning":
            logger.debug("Audio backend warning: %s", event.get("message", ""))
            return

        event_session = int(event.get("session", -1))
        if event_session != self._session:
            return
        self._last_event_at = time.monotonic()
        if event_type == "started":
            host = str(event.get("host", "") or "")
            device = str(event.get("device", "Микрофон") or "Микрофон")
            description = f"{device} [{host}]" if host else device
            self.started.emit(description)
            generation = self._process_generation
            QTimer.singleShot(10000, lambda g=generation: self._mark_worker_stable(g))
        elif event_type == "level":
            self.level_changed.emit(int(event.get("value", 0)))
        elif event_type in {"partial", "wake_probe", "wake_chunk", "finished"}:
            encoded = str(event.get("audio_b64", "") or "")
            try:
                raw = base64.b64decode(encoded, validate=False) if encoded else b""
            except (ValueError, TypeError):
                raw = b""
            audio = np.frombuffer(raw, dtype=np.float32).copy()
            recording = RecordingResult(audio, int(event.get("sample_rate", 16000)), str(event.get("correlation_id", "")), float(event.get("captured_at", 0.0)), float(event.get("speech_ended_at", 0.0)), float(event.get("capture_started_at", 0.0)), bool(event.get("completed_utterance", False)), empty_reason=str(event.get("empty_reason", "")), wake_candidate=bool(event.get("wake_candidate", False)), end_reason=str(event.get("end_reason", "")))
            if event_type == "partial":
                self.partial_ready.emit(recording)
            elif event_type == "wake_probe":
                self.wake_probe_ready.emit(recording)
            elif event_type == "wake_chunk":
                self.wake_chunk_ready.emit(recording)
            else:
                self._recording = False
                self._command_waiting158 = recording.wake_candidate
                self._mode = ""
                self._restart_attempts = 0
                self.level_changed.emit(0)
                self.finished.emit(recording)
        elif event_type == "stopped":
            self._command_waiting158 = False
            self._recording = False
            self._mode = ""
            self._restart_attempts = 0
            self.level_changed.emit(0)
        elif event_type == "error":
            message = str(event.get("message", "Ошибка записи с микрофона"))
            if self._retry_after_stream_error(message):
                return
            self._recording = False
            self._mode = ""
            self.level_changed.emit(0)
            self.failed.emit(message)

    def _retry_after_stream_error(self, message: str) -> bool:
        payload = dict(self._last_start_payload or {})
        if self._closed or not payload or self._restart_attempts >= 2:
            return False
        self._restart_attempts += 1
        self._force_safe_host = True
        self._safe_host_index += 1
        retry_payload = dict(payload)
        retry_payload["safe_mode"] = True
        retry_payload["host_fallback"] = int(self._safe_host_index)
        self._last_start_payload = dict(retry_payload)
        try:
            with self._io_lock:
                self._recording = True
                self._mode = str(retry_payload.get("mode", self._mode) or self._mode)
                self._send_locked(retry_payload)
            logger.warning(
                "Audio stream error (%s); retrying with Windows host fallback #%s",
                message,
                self._safe_host_index,
            )
            self.recovered.emit(
                "Микрофон автоматически переключён на резервный Windows-аудиохост."
            )
            return True
        except Exception as exc:
            logger.warning("Audio fallback retry failed: %s", exc)
            return False

    def _mark_worker_stable(self, generation: int) -> None:
        process = self._process
        if (
            not self._closed
            and generation == self._process_generation
            and process is not None
            and process.poll() is None
        ):
            self._restart_attempts = 0

    def _cleanup_process_locked(self, *, force: bool) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None and not force:
            try:
                if process.stdin is not None:
                    process.stdin.write('{"cmd":"shutdown"}\n')
                    process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                force = True
        if process.poll() is None and force:
            try:
                process.terminate()
                process.wait(timeout=1.5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._stdout_thread = None
        self._stderr_thread = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._poll_timer.stop()
        self._recording = False
        self._mode = ""
        with self._io_lock:
            self._cleanup_process_locked(force=False)
