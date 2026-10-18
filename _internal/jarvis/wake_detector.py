# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import base64
import json
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np

from jarvis.voice import RecordingResult
from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
from jarvis.runtime.distribution164 import worker_command

logger = logging.getLogger(__name__)


class _WakeWorkerCrashed(RuntimeError):
    pass


class HybridWakeDetector:
    """Crash-isolated acoustic Jarvis detector.

    RC7 loaded openWakeWord/ONNX Runtime inside the Qt process. ONNX is native code;
    on Windows an access violation (0xC0000005) therefore killed the whole assistant.
    RC8 keeps *all* openWakeWord and ONNX state in ``jarvis.wake_worker`` and talks
    to it over JSON lines. Calls are serialized, so rolling probes and utterance
    verification can never touch the same ONNX model concurrently.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=16)
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._closed = False
        self._native_crashes = 0
        self._disabled_until = 0.0

    @staticmethod
    def _project_root() -> Path:
        return Path(__file__).resolve().parents[1]

    @classmethod
    def _environment(cls) -> dict[str, str]:
        env = os.environ.copy()
        root = cls._project_root()
        old_path = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(root) + (os.pathsep + old_path if old_path else "")
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("OMP_NUM_THREADS", "1")
        env.setdefault("MKL_NUM_THREADS", "1")
        return env

    def _start_locked(self) -> None:
        if self._closed:
            raise RuntimeError("Wake detector already closed")
        process = self._process
        if process is not None and process.poll() is None:
            return
        self._cleanup_locked(force=True)
        self._responses = queue.Queue(maxsize=16)
        self._process = subprocess.Popen(
            worker_command("jarvis.wake_worker"),
            cwd=str(self._project_root()),
            env=self._environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **hidden_process_kwargs(),
        )
        process = self._process
        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(process, self._responses),
            name="NeoJarvis-Wake-responses",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process,),
            name="NeoJarvis-Wake-logs",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self, process: subprocess.Popen[str], responses: queue.Queue[dict[str, Any]]) -> None:
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
                    logger.warning("Wake worker returned invalid protocol data: %s", line[:240])
                    continue
                if isinstance(payload, dict):
                    try:
                        responses.put(payload, timeout=0.4)
                    except queue.Full:
                        logger.debug("Wake response queue full; dropping stale response")
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
                    logger.info("WAKE | %s", text)
        except (OSError, ValueError):
            return

    def _cleanup_locked(self, *, force: bool) -> None:
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
                process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                force = True
        if process.poll() is None and force:
            try:
                process.terminate()
                process.wait(timeout=1.0)
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
        self._responses = queue.Queue(maxsize=16)
        self._stdout_thread = None
        self._stderr_thread = None

    def _request(self, command: str, recording: RecordingResult | None, timeout: float) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        payload: dict[str, Any] = {"cmd": command, "id": request_id}
        if recording is not None:
            audio = np.asarray(recording.audio, dtype=np.float32).reshape(-1)
            payload.update(
                {
                    "audio_b64": base64.b64encode(audio.tobytes()).decode("ascii"),
                    "sample_rate": int(recording.sample_rate),
                }
            )
        last_error: BaseException | None = None
        with self._lock:
            for attempt in range(2):
                try:
                    self._start_locked()
                    process = self._process
                    if process is None or process.stdin is None:
                        raise _WakeWorkerCrashed("wake process did not start")
                    process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    process.stdin.flush()
                    deadline = time.monotonic() + max(0.5, float(timeout))
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise _WakeWorkerCrashed(
                                f"wake worker exited with code {process.returncode}"
                            )
                        try:
                            response = self._responses.get(timeout=0.025)
                        except queue.Empty:
                            continue
                        if str(response.get("id", "")) != request_id:
                            continue
                        if not response.get("ok"):
                            raise RuntimeError(str(response.get("error", "wake detector error")))
                        self._native_crashes = 0
                        return response
                    raise TimeoutError("wake detector timed out")
                except (_WakeWorkerCrashed, BrokenPipeError, OSError, ValueError) as exc:
                    last_error = exc
                    self._native_crashes += 1
                    exit_code = self._process.poll() if self._process is not None else None
                    logger.warning(
                        "Acoustic wake worker crashed/closed (exit=%s, attempt=%s): %s. "
                        "JARVIS stays alive and will use Whisper fallback.",
                        exit_code,
                        attempt + 1,
                        exc,
                    )
                    self._cleanup_locked(force=True)
                    if attempt == 0:
                        continue
                except TimeoutError as exc:
                    last_error = exc
                    logger.warning("Acoustic wake worker timeout; using Whisper fallback")
                    self._cleanup_locked(force=True)
                    break
                except Exception as exc:
                    last_error = exc
                    logger.debug("Acoustic wake detector request failed: %s", exc)
                    # Missing models/providers must not be retried on every
                    # microphone chunk. The Whisper path remains available.
                    self._disabled_until = time.monotonic() + 60.0
                    self._cleanup_locked(force=True)
                    break
        if self._native_crashes >= 2:
            # Avoid an endless respawn loop if this PC/ORT combination is unstable.
            self._disabled_until = time.monotonic() + 60.0
        if last_error:
            raise RuntimeError(str(last_error)) from last_error
        raise RuntimeError("wake detector request failed")

    def preload(self) -> bool:
        if self._closed or time.monotonic() < self._disabled_until:
            return False
        try:
            response = self._request("preload", None, 30.0)
            model = str(response.get("model", "") or "")
            if model:
                logger.info("Acoustic wake detector loaded in isolated process: %s", model)
            return True
        except Exception as exc:
            logger.warning("Acoustic wake detector unavailable; Whisper fallback remains active: %s", exc)
            return False

    def score(self, recording: RecordingResult) -> float:
        if self._closed or time.monotonic() < self._disabled_until:
            return 0.0
        audio = np.asarray(recording.audio, dtype=np.float32).reshape(-1)
        if not audio.size:
            return 0.0
        try:
            response = self._request("score", recording, 4.0)
            score = float(response.get("score", 0.0) or 0.0)
            return max(0.0, min(1.0, score)) if math.isfinite(score) else 0.0
        except Exception as exc:
            logger.debug("Acoustic wake score unavailable; Whisper fallback will decide: %s", exc)
            return 0.0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            self._cleanup_locked(force=False)
