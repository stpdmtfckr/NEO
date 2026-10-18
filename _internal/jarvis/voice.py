# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import numpy as np

from jarvis.config import ConfigManager
from jarvis.language import command_transcription_prompt
from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
from jarvis.runtime.distribution164 import worker_command
from jarvis.runtime.resource_budget import resource_budget
from jarvis.runtime.process_diagnostics184 import record_spawn, stt_role

logger = logging.getLogger(__name__)

FASTER_WHISPER_MODEL = "small"
FASTER_WHISPER_DISPLAY_NAME = "faster-whisper"


@dataclass
class RecordingResult:
    """Normalized mono 16 kHz audio used by the speech recognizer."""

    audio: np.ndarray
    sample_rate: int
    # Optional capture metadata travels with a request without storing raw audio.
    correlation_id: str = ""
    captured_at: float = 0.0
    # Monotonic last-VAD-voice timestamp, an endpoint proxy, not acoustic ground truth.
    speech_ended_at: float = 0.0
    capture_started_at: float = 0.0
    completed_utterance: bool = False
    # Wake text is only an onset hint; final command still uses original PCM.
    wake_has_command: bool = False
    empty_reason: str = ""
    wake_candidate: bool = False
    # NEO 188 NEO-VOICE-002: why the capture ended — «no_speech_timeout» (natural
    # silence, nothing said), «endpoint», «max_duration», «stopped», «fixed» or «».
    end_reason: str = ""


@dataclass
class TranscriptionResult:
    """Selected transcript plus bounded alternate decodes from the STT worker."""

    text: str
    hypotheses: list[dict[str, Any]]


def _prepare_audio_for_whisper(audio: np.ndarray, purpose: str) -> np.ndarray:
    """Clean and normalize microphone audio without destroying short consonants."""

    prepared = np.asarray(audio, dtype=np.float32).reshape(-1).copy()
    if prepared.size == 0:
        return prepared
    np.nan_to_num(prepared, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    prepared -= float(np.mean(prepared, dtype=np.float64))
    peak = float(np.max(np.abs(prepared)))
    rms = float(np.sqrt(np.mean(np.square(prepared), dtype=np.float64)))
    if not np.isfinite(peak) or not np.isfinite(rms) or peak < 1e-5 or rms < 1e-6:
        return np.array([], dtype=np.float32) if purpose in {"command", "wake", "wake_fast"} else prepared

    # Do not turn microphone self-noise into full-volume "speech".  Older builds
    # boosted any tiny RMS by up to 12x before Whisper, which is exactly the
    # condition under which silence hallucinations such as stock video-outro text
    # can appear.  Real close-field speech is comfortably above these conservative
    # floors; sub-floor clips are discarded before STT.
    silence_rms = 0.00024 if purpose == "command" else 0.00018
    silence_peak = 0.0015 if purpose == "command" else 0.0012
    if purpose in {"command", "wake", "wake_fast"} and rms < silence_rms and peak < silence_peak:
        return np.array([], dtype=np.float32)

    target_rms = 0.082 if purpose in {"wake", "wake_fast"} else (0.078 if purpose == "command" else 0.060)
    max_gain = 5.0 if purpose in {"command", "wake", "wake_fast"} else 8.0
    # Very quiet but non-empty clips are intentionally not normalized to the old
    # target RMS. Silero VAD below gets the original SNR rather than amplified hum.
    if purpose in {"command", "wake", "wake_fast"} and rms < 0.0012:
        max_gain = min(max_gain, 1.5)
    gain = min(max_gain, max(1.0, target_rms / max(rms, 1e-6)))
    gain = min(gain, 0.97 / max(peak, 1e-6))
    if gain > 1.01:
        prepared *= gain
    return np.clip(prepared, -1.0, 1.0).astype(np.float32, copy=False)


# Set from wake_word.bounded_decode_window when the wake recognizer is created.
BOUNDED_WAKE_WINDOW = True


def auto_command_threads() -> int:
    """Physical cores minus two, between 3 and 8 (CTranslate2 gains little beyond 8)."""
    try:
        import psutil
        cores = int(psutil.cpu_count(logical=False) or 0)
    except Exception:
        cores = 0
    if cores <= 0:
        cores = max(1, (os.cpu_count() or 2) // 2)
    return max(3, min(8, cores - 2))


def _transcription_options(purpose: str, language: str | None, noise_rejection_percent: int = 55) -> dict[str, Any]:
    """Return decoding settings tuned for Russian commands and a short wake word."""

    options: dict[str, Any] = {
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "without_timestamps": True,
        "suppress_blank": True,
        # For a forced Russian command decode, do not re-detect the language on
        # every segment. Segment-level language switching was a major source of
        # Polish/English-looking distortions for otherwise Russian phrases. A
        # suspicious result still gets one multilingual auto-language rescue pass.
        "multilingual": False,
        "repetition_penalty": 1.06,
        "no_repeat_ngram_size": 3,
        "hallucination_silence_threshold": 1.2,
        # FeatureExtractor retains nb_max_frames after a per-call override.
        # The wake/short-control lane shares a model: reset the normal window
        # explicitly after a 2-second probe, otherwise completed speech is split
        # into extra native decodes (and multilingual detection runs again).
        "chunk_length": 30,
    }
    lang = str(language or "").strip().casefold()
    auto_language = not lang or lang in {"auto", "detect", "automatic"}
    if not auto_language:
        options["language"] = language
    # Wake and explicit rescue passes may need free language detection. The main
    # Russian command pass stays language-locked but still preserves Latin names.
    options["multilingual"] = bool(auto_language)
    if purpose == "partial":
        options.update(
            {
                "vad_filter": False,
                "beam_size": 1,
                "best_of": 1,
                "no_speech_threshold": 0.52,
            }
        )
    elif purpose == "wake":
        # Wake audio is emitted only after an energy-segmented utterance has ended,
        # therefore Whisper receives the complete word and does not need Silero VAD.
        options.update(
            {
                "vad_filter": False,
                "beam_size": 2,
                "best_of": 2,
                "patience": 1.0,
                "no_speech_threshold": 0.38,
                "log_prob_threshold": -1.05,
            }
        )
    elif purpose == "ambient":
        # Lifelong/background learning is intentionally much cheaper than the
        # command decoder. Audio has already passed the isolated capture VAD; one
        # beam and a bounded decoder window are sufficient for topic/entity
        # extraction and keep command/wake latency dominant.
        options.update(
            {
                "vad_filter": False,
                "beam_size": 1,
                "best_of": 1,
                "no_speech_threshold": 0.48,
                "log_prob_threshold": -1.20,
                "chunk_length": 12,
                "max_new_tokens": 80,
            }
        )
    elif purpose == "context_rescue":
        # Conditional second look for a contextually suspicious proper name.
        # This runs only after the normal command decode could not confidently
        # bind a constrained local entity, so keep it much cheaper than the
        # normal beam-7 command pass.
        options.update(
            {
                "vad_filter": False,
                "beam_size": 2,
                "best_of": 2,
                "patience": 1.0,
                "no_speech_threshold": 0.34,
                "log_prob_threshold": -1.10,
                "max_new_tokens": 64,
            }
        )
    elif purpose == "wake_fast":
        # Rolling probe: prioritize latency over a polished transcript.  The text
        # is used only to answer the binary question "did the user say Jarvis?";
        # the full command is captured and decoded after promotion.
        options.update(
            {
                "vad_filter": False,
                "beam_size": 1,
                "best_of": 1,
                "no_speech_threshold": 0.30,
                "log_prob_threshold": -1.10,
                # Limit the probe's segmentation and generated text. The native
                # encoder still pads its input to the model's supported shape;
                # this is not a change to Whisper positional embeddings.
                "chunk_length": 2,
                "max_new_tokens": 8,
            }
        )
    else:
        # Command mode gets a second, model-based speech gate.  The capture VAD is
        # deliberately fast and energy-based; Silero VAD independently rejects
        # stationary room noise before Whisper decoding.  Short commands are kept
        # by using a small minimum speech duration and modest speech padding.
        reject = max(0.0, min(1.0, float(noise_rejection_percent) / 100.0))
        # Command capture already has an energy VAD and a deliberate post-speech
        # hangover.  Running Silero VAD aggressively a second time was the main
        # reason quiet final Russian words disappeared at low noise-rejection
        # settings.  At <=25% the user explicitly asked for minimal filtering, so
        # preserve the complete captured utterance.  Above that, keep a gentler
        # model VAD with generous padding instead of the old short 150-220 ms cut.
        # HOTFIX119: the capture layer already performs endpoint/VAD. Running a
        # second Silero cut at the shipped 55% setting clipped quiet consonants and
        # short endings on real microphones. Keep the full captured utterance for
        # normal settings and enable the extra model VAD only for deliberately
        # aggressive noise rejection.
        use_model_vad = reject > 0.70
        command_options = {
            "vad_filter": use_model_vad,
            "beam_size": 7,
            "best_of": 5,
            "patience": 1.14,
            "no_speech_threshold": 0.34,
            "log_prob_threshold": -0.95,
            "compression_ratio_threshold": 2.6,
        }
        if use_model_vad:
            command_options["vad_parameters"] = {
                "threshold": round(0.31 + 0.12 * reject, 3),
                "min_speech_duration_ms": int(60 + 55 * reject),
                "min_silence_duration_ms": int(360 + 170 * reject),
                "speech_pad_ms": int(360 - 70 * reject),
            }
        options.update(command_options)
    return options


class _SttWorkerCrashed(RuntimeError):
    pass


def _looks_like_stt_context_overflow_error(exc: BaseException | str) -> bool:
    text = str(exc or "").casefold()
    return (
        "position encodings are defined for positions" in text
        or "position 448" in text
        or "position 449" in text
        or "maximum decoding length" in text
        or ("max_length" in text and "448" in text)
    )


class TranscriptionService:
    """One restartable faster-whisper model isolated from the Qt process.

    CTranslate2 is a native library. An access violation inside an in-process Python
    thread terminates the whole application, which is exactly what Windows reports as
    ``0xC0000005``. This service starts one ordinary subprocess (not
    ``multiprocessing.spawn``), keeps requested Whisper models there, serializes its
    requests, and restarts the worker if native code exits.
    """

    def __init__(self, process_name: str = "NeoJarvis-STT", *, process_priority: str = "normal", background_cpu_cores: float = 0.0) -> None:
        self._process_priority = str(process_priority or "normal").casefold()
        self._background_cpu_cores = max(0.0, min(1.0, float(background_cpu_cores)))
        self._background_cpu_job = None
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=16)
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._worker_name = process_name
        self._process_generation = 0
        self._closed = False
        self._cancel_serial = 0
        self._inflight188: dict[str, Any] | None = None

    def _cancelled188(self, request: dict[str, Any]) -> bool:
        """HIST-022: a waiter's cancellation never cancels shared work (model preload).

        Shared requests end only when the service closes; every other request
        ends when the cancel serial it was issued under is no longer current."""
        if self._closed:
            return True
        if request.get("shared"):
            return False
        return int(request.get("cancel_serial", self._cancel_serial)) != self._cancel_serial

    @contextmanager
    def _request_lock(self, deadline: float, serial: int | None):
        """The timeout includes waiting behind preload and provisional decodes."""
        acquired = self._lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not acquired:
            raise TimeoutError("Распознавание речи занято. Повторите фразу.")
        try:
            if serial is not None and serial != self._cancel_serial:
                raise TimeoutError("Распознавание речи отменено.")
            yield
        finally:
            self._lock.release()

    def interrupt(self) -> None:
        """Abort this service's current child without closing future requests.

        Called from the GUI deadline/stop path; never wait for the decoder lock.
        Requests queued before this cancellation cannot respawn the old work.
        """
        self._cancel_serial += 1
        inflight = self._inflight188
        if inflight is not None and inflight.get("shared"):
            # HIST-022: the timed-out waiter is cancelled (serial), the shared
            # preload that other owners wait for keeps running.
            return
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def _start_locked(self) -> None:
        if self._closed:
            raise RuntimeError("Сервис распознавания уже завершён.")
        process = self._process
        if process is not None and process.poll() is None:
            return
        self._cleanup_locked(force=True)

        project_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        old_path = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(project_root) + (os.pathsep + old_path if old_path else "")
        env.setdefault("OMP_NUM_THREADS", "1")
        env.setdefault("MKL_NUM_THREADS", "1")
        self._responses = queue.Queue(maxsize=16)
        self._process = subprocess.Popen(
            worker_command("jarvis.stt_worker"),
            cwd=str(project_root),
            env=env,
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
        record_spawn(stt_role(self._worker_name), self._process.pid, self._process_generation)
        if self._process_priority in {"below_normal", "idle"}:
            try:
                import psutil
                if os.name == "nt" and self._process is not None:
                    priority = psutil.IDLE_PRIORITY_CLASS if self._process_priority == "idle" else psutil.BELOW_NORMAL_PRIORITY_CLASS
                    psutil.Process(self._process.pid).nice(priority)
            except Exception:
                logger.debug("Could not lower STT worker process priority", exc_info=True)
        if self._background_cpu_cores > 0:
            from jarvis.runtime.background_cpu164 import limit_owned_child
            self._background_cpu_job = limit_owned_child(self._process, self._background_cpu_cores)
        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(self._process, self._responses),
            name=f"{self._worker_name}-responses",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(self._process,),
            name=f"{self._worker_name}-logs",
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
                    logger.warning("STT worker returned invalid protocol data: %s", line[:240])
                    continue
                try:
                    # Bind this reader to its worker generation. A dying native
                    # worker must never enqueue into a replacement's queue.
                    if not isinstance(payload, dict):
                        continue
                    responses.put(payload, timeout=0.5)
                except queue.Full:
                    logger.warning("STT response queue is full; dropping stale response")
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
                    logger.info("STT | %s", text)
        except (OSError, ValueError):
            return

    def preload(
        self,
        *,
        model_name: str,
        device: str,
        compute_type: str,
        cpu_threads: int = 4,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._request(
            RecordingResult(np.array([], dtype=np.float32), 16000),
            model_name=model_name,
            language="auto",
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            timeout_seconds=timeout_seconds,
            purpose="preload",
            initial_prompt="",
            hotwords="",
        )

    def transcribe(
        self,
        recording: RecordingResult,
        *,
        model_name: str,
        language: str,
        device: str,
        compute_type: str,
        cpu_threads: int = 4,
        timeout_seconds: float = 60.0,
        purpose: str = "command",
        initial_prompt: str = "",
        hotwords: str = "",
        noise_rejection_percent: int = 55,
        adaptive_decode_retry: bool = True,
    ) -> str:
        if recording.audio.size == 0:
            return ""
        return str(self._request(
            recording,
            model_name=model_name,
            language=language,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            timeout_seconds=timeout_seconds,
            purpose=purpose,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            noise_rejection_percent=noise_rejection_percent,
            adaptive_decode_retry=adaptive_decode_retry,
            return_payload=False,
        ))

    def transcribe_detailed(
        self,
        recording: RecordingResult,
        *,
        model_name: str,
        language: str,
        device: str,
        compute_type: str,
        cpu_threads: int = 4,
        timeout_seconds: float = 60.0,
        purpose: str = "command",
        initial_prompt: str = "",
        hotwords: str = "",
        noise_rejection_percent: int = 55,
        adaptive_decode_retry: bool = True,
    ) -> TranscriptionResult:
        if recording.audio.size == 0:
            return TranscriptionResult("", [])
        payload = self._request(
            recording,
            model_name=model_name,
            language=language,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            timeout_seconds=timeout_seconds,
            purpose=purpose,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            noise_rejection_percent=noise_rejection_percent,
            adaptive_decode_retry=adaptive_decode_retry,
            return_payload=True,
        )
        if not isinstance(payload, dict):
            return TranscriptionResult(str(payload or ""), [])
        return TranscriptionResult(
            str(payload.get("text", "") or "").strip(),
            [x for x in (payload.get("hypotheses", []) or []) if isinstance(x, dict)][:5],
        )

    def _request(
        self,
        recording: RecordingResult,
        *,
        model_name: str,
        language: str,
        device: str,
        compute_type: str,
        cpu_threads: int,
        timeout_seconds: float,
        purpose: str,
        initial_prompt: str,
        hotwords: str,
        noise_rejection_percent: int = 55,
        adaptive_decode_retry: bool = True,
        return_payload: bool = False,
    ) -> str | dict[str, Any]:
        audio = np.asarray(recording.audio, dtype=np.float32)
        requested_at = time.monotonic()
        deadline = requested_at + max(0.05, float(timeout_seconds))
        cancel_serial = self._cancel_serial
        shared = purpose == "preload"
        request = {
            "cmd": "transcribe",
            "cancel_serial": cancel_serial,
            "shared": shared,
            "id": uuid.uuid4().hex,
            "audio_b64": base64.b64encode(audio.tobytes()).decode("ascii"),
            "sample_rate": int(recording.sample_rate),
            "model_name": str(model_name or FASTER_WHISPER_MODEL),
            "language": language,
            "device": device,
            "compute_type": compute_type,
            "cpu_threads": max(1, int(cpu_threads)),
            "purpose": purpose,
            # NEO 168: the full wake decode encodes only its own audio length.
            "bounded_window": bool(purpose == "wake" and BOUNDED_WAKE_WINDOW),
            "initial_prompt": initial_prompt,
            "hotwords": hotwords,
            "noise_rejection_percent": max(0, min(100, int(noise_rejection_percent))),
            "adaptive_decode_retry": bool(adaptive_decode_retry),
            "return_hypotheses": bool(return_payload and purpose == "command"),
            "correlation_id": recording.correlation_id or uuid.uuid4().hex,
            "captured_at": float(recording.captured_at),
            "purge_cuda": str(device).casefold() == "cpu",
            # Count quality retries separately from exceptional CUDA/context
            # recovery. Keep both existing quality passes where needed.
            "max_quality_retries": 2 if adaptive_decode_retry else 0,
        }
        foreground = resource_budget().foreground_work() if purpose != "ambient" else nullcontext()
        with foreground, self._request_lock(deadline, None if shared else cancel_serial):
            logger.debug("ASR request correlation=%s purpose=%s queued_ms=%.1f device=%s threads=%s audio_s=%.3f",
                         request["correlation_id"], purpose,
                         (time.monotonic() - requested_at) * 1000.0, device, cpu_threads,
                         audio.size / max(1, int(recording.sample_rate)))
            last_error: Exception | None = None
            for attempt in range(2):
                if self._cancelled188(request) or time.monotonic() >= deadline:
                    raise TimeoutError("Распознавание речи отменено или заняло слишком много времени.")
                try:
                    return self._request_once_locked(request, max(0.05, deadline - time.monotonic()), return_payload=return_payload)
                except _SttWorkerCrashed as exc:
                    # This is a RuntimeError subclass: it must be handled before
                    # the generic RuntimeError branch, otherwise recovery is dead.
                    last_error = exc
                    if self._cancelled188(request):
                        raise TimeoutError("Распознавание речи отменено.") from exc
                    logger.warning("STT worker crashed; restarting once: %s", exc)
                    self._cleanup_locked(force=True)
                    if attempt == 0:
                        continue
                except RuntimeError as exc:
                    # Belt-and-suspenders HOTFIX6 guard.  HOTFIX5 already retries
                    # inside stt_worker, but if an older/stale worker or library
                    # path still returns Whisper's 448-position overflow, resend
                    # the exact same audio once with no hotwords and a tiny prompt.
                    if purpose == "command" and _looks_like_stt_context_overflow_error(exc):
                        logger.warning("STT context overflow escaped worker; retrying with minimal hints")
                        safe_request = dict(request)
                        safe_request["id"] = uuid.uuid4().hex
                        safe_request["initial_prompt"] = command_transcription_prompt(str(language or "ru"))
                        safe_request["hotwords"] = ""
                        return self._request_once_locked(safe_request, max(0.05, deadline - time.monotonic()), return_payload=return_payload)
                    raise
            raise RuntimeError(
                "Процесс распознавания речи аварийно завершился дважды. "
                f"Последняя ошибка: {last_error}"
            )

    def _request_once_locked(
        self, request: dict[str, Any], timeout_seconds: float, *, return_payload: bool = False
    ) -> str | dict[str, Any]:
        # HIST-003: a request cancelled before it starts never spawns a new worker for its owner.
        if self._cancelled188(request):
            raise TimeoutError("Распознавание речи отменено.")
        self._start_locked()
        if self._cancelled188(request):
            # The worker prepared for a cancelled owner is released at once.
            self._cleanup_locked(force=True)
            raise TimeoutError("Распознавание речи отменено.")
        requested_at = time.monotonic()
        process = self._process
        if process is None or process.stdin is None:
            raise _SttWorkerCrashed("не удалось открыть канал к STT-процессу")
        try:
            process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise _SttWorkerCrashed(f"канал STT закрыт: {exc}") from exc

        request_id = str(request["id"])
        deadline = time.monotonic() + max(0.05, float(timeout_seconds))
        self._inflight188 = request
        try:
            return self._await_response188(request, request_id, process, requested_at, deadline, return_payload)
        finally:
            self._inflight188 = None

    def _await_response188(self, request, request_id, process, requested_at, deadline, return_payload):
        while time.monotonic() < deadline:
            if self._cancelled188(request):
                self._cleanup_locked(force=True)
                raise TimeoutError("Распознавание речи отменено.")
            exit_code = process.poll()
            if exit_code is not None:
                raise _SttWorkerCrashed(f"код завершения {exit_code}")
            try:
                response = self._responses.get(timeout=0.10)
            except queue.Empty:
                continue
            if str(response.get("id", "")) != request_id:
                continue
            if self._cancelled188(request):
                # HIST-004: cancelled while the answer was on its way — it is not returned.
                raise TimeoutError("Распознавание речи отменено.")
            if response.get("ok"):
                emit = logger.info if request.get("purpose") == "command" else logger.debug
                emit("ASR result correlation=%s purpose=%s wall_ms=%.1f decode_ms=%s decodes=%s device=%s model=%s language=%s suspicious=%s",
                     request.get("correlation_id", request_id), request.get("purpose"),
                     (time.monotonic() - requested_at) * 1000.0,
                     response.get("decode_ms", 0.0), response.get("decode_count", 0),
                     response.get("device", "unknown"), request.get("model_name", "unknown"),
                     request.get("language", "auto"), bool(response.get("suspicious", False)))
                if return_payload:
                    return response
                return str(response.get("text", "")).strip()
            raise RuntimeError(str(response.get("error", "Ошибка распознавания речи")))
        # The decoder is native and cannot be cancelled safely in-process. Kill
        # only our owned helper so the next command cannot sit behind stale work.
        self._cleanup_locked(force=True)
        raise TimeoutError("Распознавание речи заняло слишком много времени.")

    def _cleanup_locked(self, *, force: bool) -> None:
        process = self._process
        self._process = None
        job, self._background_cpu_job = self._background_cpu_job, None
        if process is None:
            if job is not None:
                job.close()
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
        self._responses = queue.Queue(maxsize=16)
        if job is not None:
            job.close()

    def close(self) -> None:
        with self._lock:
            if self._closed and self._process is None:
                return
            self._closed = True
            self._cleanup_locked(force=False)

    def cancel(self) -> None:
        """Cancel an owned native decoder before joining a background consumer.

        Do not wait for the request lock: the blocked request owns it. Terminating
        its child wakes that request, and _closed prevents automatic respawn.
        """
        self._closed = True
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass


class SpeechRecognizer:
    def __init__(
        self,
        config: ConfigManager,
        service: TranscriptionService,
        model_name: str | None = None,
        *,
        purpose: str = "command",
        initial_prompt: str = "",
        hotwords: str = "",
    ) -> None:
        self.config = config
        self.service = service
        if model_name:
            self.model_name = str(model_name)
        elif purpose == "wake":
            self.model_name = str(config.get("wake_word.stt_model", "tiny"))
            global BOUNDED_WAKE_WINDOW
            BOUNDED_WAKE_WINDOW = bool(config.get("wake_word.bounded_decode_window", True))
        elif purpose == "partial":
            self.model_name = str(config.get("voice.partial_stt_model", "tiny"))
        else:
            self.model_name = str(config.get("voice.stt_model", FASTER_WHISPER_MODEL))
        self.purpose = purpose
        self.initial_prompt = initial_prompt
        self.hotwords = hotwords

    def _cpu_threads(self) -> int:
        command_threads = max(1, int(self.config.get("voice.stt_cpu_threads", 4) or 4))
        if self.purpose in {"wake", "partial"}:
            return max(1, min(command_threads, int(self.config.get("wake_word.cpu_threads", 2) or 2)))
        if bool(self.config.get("voice.stt_threads_auto", False)):
            # NEO 174 (off by default since 175): more cores per decode. CTranslate2 keeps
            # every thread busy while it decodes, so 6-8 threads showed as a 70% CPU spike;
            # the configured 3 threads keep NEO's usual load.
            return max(command_threads, auto_command_threads())
        return command_threads

    def _device_policy(self) -> tuple[str, str]:
        device = str(self.config.get("voice.stt_device", "auto"))
        compute = str(self.config.get("voice.stt_compute_type", "auto"))
        if self.purpose in {"wake", "partial"} and bool(self.config.get("wake_word.force_cpu", True)):
            # A wake override must also remove CUDA-only compute types.
            return "cpu", compute if compute in {"int8", "float32", "int8_float32"} else "int8"
        return device, compute

    def preload(self) -> None:
        self.service.preload(
            model_name=self.model_name,
            device=self._device_policy()[0],
            compute_type=self._device_policy()[1],
            cpu_threads=self._cpu_threads(),
            timeout_seconds=float(self.config.get("voice.transcription_timeout_seconds", 60)),
        )

    def _effective_initial_prompt(self) -> str:
        # V4.5: bias only command transcription with compact vocabulary from the
        # currently active game context. Wake STT intentionally stays un-biased.
        if self.purpose != "command":
            return self.initial_prompt
        try:
            from jarvis.gaming.speech_hints import command_game_initial_prompt
            return command_game_initial_prompt(self.config, self.initial_prompt)
        except Exception:
            return self.initial_prompt

    def _wake_language(self) -> str:
        """Use the selected speech language before open-ended language detection.

        An isolated proper name is poor evidence for detecting a language. The
        rolling binary probe still follows the configured wake spelling; the
        completed utterance follows the selected command language so its media
        controls and command tail remain intelligible. Explicit voice.language
        overrides are preserved, as is deliberate automatic command language.
        """
        explicit = str(self.config.get("voice.language", "auto") or "auto").strip().casefold()
        if explicit not in {"", "auto", "detect", "automatic"}:
            return explicit
        selected = str(self.config.get("voice.command_language", "auto") or "auto").strip().casefold()
        return selected if selected not in {"", "auto", "detect", "automatic"} else "auto"

    def _wake_retry_languages(self) -> tuple[str, ...]:
        """Only the configured wake spelling needs another language, once."""
        primary = self._wake_language()
        wake = self._fast_wake_language()
        return (wake,) if wake not in {primary, "auto"} else ()

    def transcribe(self, recording: RecordingResult) -> str:
        timeout_default = 40 if self.purpose != "command" else 70
        return self.service.transcribe(
            recording,
            model_name=self.model_name,
            language=str(
                self.config.get("voice.command_language", "ru")
                if self.purpose in {"command", "partial"}
                else self._wake_language() if self.purpose == "wake"
                else self.config.get("voice.language", "auto")
            ),
            device=self._device_policy()[0],
            compute_type=self._device_policy()[1],
            cpu_threads=self._cpu_threads(),
            timeout_seconds=float(
                self.config.get("voice.transcription_timeout_seconds", timeout_default)
            ),
            purpose=self.purpose,
            initial_prompt=self._effective_initial_prompt(),
            hotwords=self.hotwords,
            noise_rejection_percent=int(self.config.get("voice.noise_rejection_percent", 55)),
            adaptive_decode_retry=bool(self.config.get("voice.adaptive_decode_retry", True)),
        )

    def transcribe_detailed(self, recording: RecordingResult, *, profile: dict | None = None) -> TranscriptionResult:
        """Return the chosen command text and bounded alternate ASR decodes.

        NEO 188 NEO-ASR-001: ``profile`` overrides the hints for THIS request only."""
        timeout_default = 40 if self.purpose != "command" else 70
        initial_prompt = self._effective_initial_prompt() if profile is None else str(profile.get("initial_prompt", "") or "")
        hotwords = self.hotwords if profile is None else str(profile.get("hotwords", "") or "")
        return self.service.transcribe_detailed(
            recording,
            model_name=self.model_name,
            language=str(
                self.config.get("voice.command_language", "ru")
                if self.purpose in {"command", "partial"}
                else self._wake_language() if self.purpose == "wake"
                else self.config.get("voice.language", "auto")
            ),
            device=self._device_policy()[0],
            compute_type=self._device_policy()[1],
            cpu_threads=self._cpu_threads(),
            timeout_seconds=float(
                self.config.get("voice.transcription_timeout_seconds", timeout_default)
            ),
            purpose=self.purpose,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            noise_rejection_percent=int(self.config.get("voice.noise_rejection_percent", 55)),
            adaptive_decode_retry=bool(self.config.get("voice.adaptive_decode_retry", True)),
        )

    def transcribe_context_rescue(
        self, recording: RecordingResult, *, candidates: list[str], language: str = ""
    ) -> dict[str, Any]:
        """One cheap, conditional re-decode biased toward local entity names.

        The normal command decode remains the hot path. This method is called only
        when the contextual resolver found a plausible-but-below-threshold local
        entity, so ordinary commands pay zero extra inference cost.
        """
        terms = []
        seen = set()
        for raw in candidates[:6]:
            value = str(raw or "").strip()
            key = value.casefold()
            if value and key not in seen:
                seen.add(key); terms.append(value)
        if not terms or recording.audio.size == 0:
            return {"text": "", "score": -9.0, "source": "context_rescue"}
        chosen_language = str(language or self.config.get("voice.command_language", "ru") or "ru").strip().casefold()
        if chosen_language in {"", "auto", "detect", "automatic"}:
            chosen_language = "auto"
        hint = "Likely local proper names: " + ", ".join(terms) + ". Transcribe the spoken command verbatim; preserve the matching proper name exactly. Do not translate."
        payload = self.service._request(
            recording,
            model_name=self.model_name,
            language=chosen_language,
            device=self._device_policy()[0],
            compute_type=self._device_policy()[1],
            cpu_threads=self._cpu_threads(),
            timeout_seconds=min(28.0, float(self.config.get("voice.transcription_timeout_seconds", 70) or 70)),
            purpose="context_rescue",
            initial_prompt=hint,
            hotwords=", ".join(terms),
            noise_rejection_percent=int(self.config.get("voice.noise_rejection_percent", 55)),
            adaptive_decode_retry=False,
            return_payload=True,
        )
        if not isinstance(payload, dict):
            return {"text": str(payload or "").strip(), "score": -0.5, "source": "context_rescue"}
        return {
            "text": str(payload.get("text", "") or "").strip(),
            "score": float(payload.get("score", -0.5) or -0.5),
            "suspicious": bool(payload.get("suspicious", False)),
            "source": "context_rescue",
        }

    def _fast_wake_language(self, language_override: str | None = None) -> str:
        """Choose a deterministic language for the binary wake probe.

        Auto language detection is unnecessary work for a known configured wake
        phrase and can turn the proper name ``Jarvis`` into unrelated multilingual
        fragments. Infer from the wake phrase script first; fall back to the
        selected command language only for genuinely mixed/unknown phrases.
        """
        override = str(language_override).strip().casefold() if language_override is not None else ""
        if override:
            return override
        configured = str(self.config.get("wake_word.fast_probe_language", "auto") or "auto").strip().casefold()
        if configured not in {"", "auto", "detect", "automatic"}:
            return configured
        phrase = str(self.config.get("wake_word.phrase", "NEO") or "NEO")
        try:
            from jarvis.wake_matcher import is_short_wake_phrase
            short = is_short_wake_phrase(phrase)
        except Exception:
            short = False
        if short:
            # NEO 178: «NEO» is written in Latin letters but said in the user's language.
            # Decoding Russian speech as English blurs «Нео» with «не-а»; use the
            # selected command language so «не-а» stays «не-а».
            selected = str(self.config.get("voice.command_language", "auto") or "auto").strip().casefold()
            return selected if selected not in {"", "auto", "detect", "automatic"} else "auto"
        has_cyrillic = bool(re.search(r"[а-яё]", phrase, re.IGNORECASE))
        has_latin = bool(re.search(r"[a-z]", phrase, re.IGNORECASE))
        if has_latin and not has_cyrillic:
            return "en"
        if has_cyrillic and not has_latin:
            return "ru"
        selected = str(self.config.get("voice.command_language", "auto") or "auto").strip().casefold()
        return selected if selected not in {"", "auto", "detect", "automatic"} else "auto"

    def transcribe_fast_wake_probe(self, recording: RecordingResult, *, language_override: str | None = None) -> str:
        """Ultra-light rolling wake probe using the already warm wake model.

        The robust wake decoder and the rolling probe share one isolated STT
        subprocess.  A fast probe is disposable, so never queue it behind an
        already-running robust/command transcription.  Real Windows crash dumps
        showed both call paths present during a native heap-corruption restart;
        skipping stale probes also removes needless model churn and latency.
        """
        lock = self.service._lock
        if not lock.acquire(blocking=False):
            return ""
        try:
            return self.service.transcribe(
                recording,
                model_name=self.model_name,
                # Jarvis is an English proper name.  Constraining only the rolling
                # binary wake probe avoids multilingual detours such as Polish/Korean
                # transcriptions while the full command recognizer remains multilingual.
                language=self._fast_wake_language(language_override),
                device=self._device_policy()[0],
                compute_type=self._device_policy()[1],
                cpu_threads=self._cpu_threads(),
                timeout_seconds=min(20.0, float(self.config.get("voice.transcription_timeout_seconds", 40) or 40)),
                purpose="wake_fast",
                initial_prompt=self.initial_prompt,
                hotwords=self.hotwords,
                noise_rejection_percent=int(self.config.get("voice.noise_rejection_percent", 55)),
                adaptive_decode_retry=False,
            )
        finally:
            lock.release()

    def _control_language(self) -> str:
        language = str(self.config.get("voice.command_language", "ru") or "ru").strip().casefold()
        if language in {"", "auto", "detect", "automatic"}:
            language = str(self.config.get("voice.language", "auto") or "auto").strip().casefold()
        return language

    def transcribe_fast_control_probe(self, recording: RecordingResult, *, timeout_seconds: float = 20.0, wait_for_slot: bool = True) -> str:
        """Decode a completed short control phrase with the warm tiny model.

        Unlike the binary wake probe this follows the selected command language,
        so Russian transport words such as ``дальше`` / ``продолжить`` /
        ``предыдущий`` do not pay for the robust beam-2 wake decoder.  It reuses
        the existing wake STT process/model and therefore adds no third native STT
        worker or extra model residency.
        """
        language = self._control_language()
        # The final command has an accurate decoder of its own. An optional
        # transport shortcut must skip a busy wake/partial lane immediately.
        acquired = False
        if not wait_for_slot:
            acquired = self.service._lock.acquire(blocking=False)
            if not acquired:
                return ""
        try:
            return self.service.transcribe(
                recording,
                model_name=self.model_name,
                language=language,
                device=self._device_policy()[0],
                compute_type=self._device_policy()[1],
                cpu_threads=self._cpu_threads(),
                timeout_seconds=min(float(timeout_seconds), float(self.config.get("voice.transcription_timeout_seconds", 40) or 40)),
                purpose="wake_fast",
                initial_prompt="",
                hotwords="",
                noise_rejection_percent=int(self.config.get("voice.noise_rejection_percent", 55)),
                adaptive_decode_retry=False,
            )
        finally:
            if acquired:
                self.service._lock.release()

    def transcribe_with_language(self, recording: RecordingResult, language: str) -> str:
        """Decode the same clip with a forced language for short wake-word recovery."""
        timeout_default = 40 if self.purpose != "command" else 70
        return self.service.transcribe(
            recording,
            model_name=self.model_name,
            language=str(language or "auto"),
            device=self._device_policy()[0],
            compute_type=self._device_policy()[1],
            cpu_threads=self._cpu_threads(),
            timeout_seconds=float(self.config.get("voice.transcription_timeout_seconds", timeout_default)),
            purpose=self.purpose,
            initial_prompt=self._effective_initial_prompt(),
            hotwords=self.hotwords,
            noise_rejection_percent=int(self.config.get("voice.noise_rejection_percent", 55)),
            adaptive_decode_retry=False,
        )
