# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import collections
import math
import queue
import time
import uuid
from typing import Any

import numpy as np


def _queue_event(channel: Any, payload: dict[str, Any], *, critical: bool = False) -> None:
    """Send an event without blocking the microphone loop indefinitely."""

    try:
        if critical:
            channel.put(payload, timeout=1.0)
        else:
            channel.put_nowait(payload)
    except (queue.Full, BrokenPipeError, EOFError, OSError):
        return


def _resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    mono = np.asarray(audio, dtype=np.float32).reshape(-1)
    if mono.size <= 1 or source_rate == target_rate:
        return np.array(mono, dtype=np.float32, copy=True)

    source_rate = max(1, int(source_rate))
    target_rate = max(1, int(target_rate))
    # The overwhelmingly common Windows path is 48 kHz -> 16 kHz. Avoid two
    # large linspace allocations + np.interp for every wake probe/utterance. A
    # short box-filter decimator is sufficient for speech recognition and also
    # suppresses high-frequency aliasing better than simply taking every Nth sample.
    if source_rate > target_rate and source_rate % target_rate == 0:
        ratio = source_rate // target_rate
        if 1 < ratio <= 6:
            usable = (mono.size // ratio) * ratio
            if usable >= ratio:
                reduced = mono[:usable].reshape(-1, ratio).mean(axis=1, dtype=np.float32)
                return np.asarray(reduced, dtype=np.float32)

    duration = mono.size / source_rate
    target_count = max(1, int(round(duration * target_rate)))
    # Index-domain interpolation avoids constructing a full source coordinate
    # array. This is a secondary path (44.1 kHz devices etc.).
    positions = np.arange(target_count, dtype=np.float64) * (source_rate / target_rate)
    left = np.floor(positions).astype(np.int64)
    left = np.clip(left, 0, mono.size - 1)
    right = np.minimum(left + 1, mono.size - 1)
    frac = (positions - left).astype(np.float32)
    return (mono[left] * (1.0 - frac) + mono[right] * frac).astype(np.float32, copy=False)


def _safe_token(host_name: str, device_name: str) -> str:
    return f"sd:{host_name.strip()}|{device_name.strip()}"


def _host_rank(host_name: str) -> int:
    """Prefer conservative Windows hosts and keep WDM-KS as a last resort."""

    name = str(host_name or "").casefold()
    if "mme" in name:
        return 0
    if "directsound" in name or "direct sound" in name:
        return 1
    if "wasapi" in name:
        return 2
    if "wdm-ks" in name or "kernel streaming" in name:
        return 9
    return 5


_SYSTEM_LOOPBACK_NAME_HINTS = (
    "stereo mix", "what u hear", "what you hear", "wave out", "loopback",
    "monitor of", "output capture", "стерео микс", "стереомикс",
    "mixage stéréo", "mezcla estéreo", "stereomix",
)


def _is_system_loopback_input_name(name: str) -> bool:
    folded = str(name or "").strip().casefold()
    return bool(folded and any(hint in folded for hint in _SYSTEM_LOOPBACK_NAME_HINTS))


def _input_devices(sd: Any) -> list[dict[str, Any]]:
    devices = list(sd.query_devices())
    hostapis = list(sd.query_hostapis())
    result: list[dict[str, Any]] = []
    for index, info in enumerate(devices):
        if int(info.get("max_input_channels", 0) or 0) <= 0:
            continue
        host_index = int(info.get("hostapi", -1) or -1)
        host_name = ""
        if 0 <= host_index < len(hostapis):
            host_name = str(hostapis[host_index].get("name", "") or "")
        name = str(info.get("name", f"Микрофон #{index}") or f"Микрофон #{index}")
        # HOTFIX124: wake/command capture is microphone-only. Never expose
        # Stereo Mix / What-U-Hear / loopback endpoints as selectable command
        # inputs; system playback has its own true-WASAPI worker.
        if _is_system_loopback_input_name(name):
            continue
        result.append(
            {
                "index": index,
                "name": name,
                "host_name": host_name,
                "token": _safe_token(host_name, name),
                "sample_rate": int(
                    round(float(info.get("default_samplerate", 48000) or 48000))
                ),
                "host_rank": _host_rank(host_name),
            }
        )
    return result


def _requested_device_name(raw: str) -> str:
    value = str(raw or "").strip()
    if value.casefold().startswith("sd:") and "|" in value:
        return value.split("|", 1)[1].strip()
    return value.removeprefix("name:").strip()


def _resolve_input_device(
    sd: Any,
    requested: str,
    *,
    safe_mode: bool = False,
    fallback_index: int = 0,
) -> tuple[int, str, str, int, str]:
    catalog = _input_devices(sd)
    if not catalog:
        raise RuntimeError("Windows не обнаружила доступный микрофон.")

    raw = str(requested or "").strip()
    folded = raw.casefold()
    wanted_name = _requested_device_name(raw).casefold()
    if safe_mode and not wanted_name:
        try:
            default_input = sd.default.device[0]
            if default_input is not None and int(default_input) >= 0:
                for item in catalog:
                    if item["index"] == int(default_input):
                        wanted_name = item["name"].strip().casefold()
                        break
        except Exception:
            pass

    # Numeric indices from old builds are accepted only when still valid and not
    # pointing to the fragile WDM-KS host.
    if folded.startswith("index:") and not safe_mode:
        try:
            wanted_index = int(folded.split(":", 1)[1])
            for item in catalog:
                if item["index"] == wanted_index and int(item["host_rank"]) < 9:
                    return (
                        item["index"],
                        item["name"],
                        item["token"],
                        item["sample_rate"],
                        item["host_name"],
                    )
        except (TypeError, ValueError):
            pass

    if folded.startswith("sd:") and not safe_mode:
        for item in catalog:
            if item["token"].casefold() == folded and int(item["host_rank"]) < 9:
                return (
                    item["index"],
                    item["name"],
                    item["token"],
                    item["sample_rate"],
                    item["host_name"],
                )

    # In safe mode retain the physical microphone name but remap it to MME or
    # DirectSound. This is used after a native PortAudio heap-corruption exit.
    if wanted_name and not folded.startswith("qt:"):
        exact = [item for item in catalog if item["name"].strip().casefold() == wanted_name]
        partial = [item for item in catalog if wanted_name in item["name"].casefold()]
        candidates = exact or partial
        if candidates:
            ordered = sorted(candidates, key=lambda x: (int(x["host_rank"]), int(x["index"])))
            item = ordered[min(max(0, int(fallback_index)), len(ordered) - 1)]
            return (
                item["index"],
                item["name"],
                item["token"],
                item["sample_rate"],
                item["host_name"],
            )

    if not safe_mode:
        try:
            default_input = sd.default.device[0]
            if default_input is not None and int(default_input) >= 0:
                wanted_index = int(default_input)
                for item in catalog:
                    if item["index"] == wanted_index and int(item["host_rank"]) < 9:
                        return (
                            item["index"],
                            item["name"],
                            item["token"],
                            item["sample_rate"],
                            item["host_name"],
                        )
        except Exception:
            pass

    # A conservative host order is more stable on Windows USB microphones than
    # selecting WDM-KS or WASAPI unconditionally.
    ordered = sorted(catalog, key=lambda x: (int(x["host_rank"]), int(x["index"])))
    item = ordered[min(max(0, int(fallback_index)), len(ordered) - 1)]
    return (
        item["index"],
        item["name"],
        item["token"],
        item["sample_rate"],
        item["host_name"],
    )


def _open_stream(sd: Any, device_index: int, native_rate: int):
    """Open a blocking input stream.

    A Python callback crossing PortAudio threads was the remaining native crash
    surface in v0.9.3. Blocking reads keep all sounddevice calls on one worker
    process thread and make stream shutdown deterministic.
    """

    last_error: Exception | None = None
    rates: list[int] = []
    for rate in (native_rate, 48000, 44100, 16000):
        rate = int(rate)
        if rate > 0 and rate not in rates:
            rates.append(rate)

    for rate in rates:
        # At the normal 44.1/48 kHz microphone rates, 2048 samples is only
        # ~43-46 ms of latency but halves Python/NumPy wake-loop iterations
        # versus 1024. Keep 1024 first for low-rate 16 kHz devices where a
        # 2048 block would add an unnecessary ~128 ms.
        blocksizes = (2048, 1024) if rate >= 32000 else (1024, 2048)
        for blocksize in blocksizes:
            for dtype in ("float32", "int16"):
                try:
                    stream = sd.InputStream(
                        device=device_index,
                        channels=1,
                        samplerate=rate,
                        dtype=dtype,
                        blocksize=blocksize,
                        latency="high",
                    )
                    stream.start()
                    return stream, rate, blocksize, dtype
                except Exception as exc:
                    last_error = exc
    raise RuntimeError(f"Не удалось открыть выбранный микрофон: {last_error}")


def _pack_recording(audio: np.ndarray, source_rate: int, target_rate: int) -> tuple[bytes, int]:
    normalized = _resample(audio, source_rate, target_rate)
    return np.asarray(normalized, dtype=np.float32).tobytes(), target_rate


def _speech_shape_metrics(block: np.ndarray, sample_rate: int) -> tuple[float, float, float, float]:
    """Return cheap speech/noise shape metrics for one microphone block.

    The command endpoint must not treat keyboard/mouse clicks or breathing as
    continued speech after the user has finished talking. Energy alone cannot make
    that distinction, so HOTFIX12 also considers crest factor, zero-crossing rate
    and how much spectral energy lives in the ordinary speech band.

    This runs only while a command is being captured; the 1024-point FFT is tiny
    compared with Whisper inference and does not create another background loop.
    """
    x = np.asarray(block, dtype=np.float32).reshape(-1)
    if x.size < 32:
        return 99.0, 1.0, 0.0, 1.0
    x = x - float(np.mean(x))
    rms = float(np.sqrt(np.mean(np.square(x), dtype=np.float64)))
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    crest = peak / max(rms, 1e-7)
    zcr = float(np.mean((x[:-1] >= 0) != (x[1:] >= 0))) if x.size > 1 else 0.0
    try:
        window = np.hanning(x.size).astype(np.float32)
        power = np.square(np.abs(np.fft.rfft(x * window))).astype(np.float64)
        freqs = np.fft.rfftfreq(x.size, d=1.0 / max(1, int(sample_rate)))
        useful = (freqs >= 70.0) & (freqs <= min(9000.0, sample_rate * 0.49))
        total = float(np.sum(power[useful])) + 1e-18
        speech_band = (freqs >= 90.0) & (freqs <= min(4600.0, sample_rate * 0.49))
        high_band = freqs >= min(6200.0, sample_rate * 0.42)
        speech_ratio = float(np.sum(power[speech_band])) / total
        high_ratio = float(np.sum(power[high_band])) / total if np.any(high_band) else 0.0
    except Exception:
        speech_ratio = 0.5
        high_ratio = 0.0
    return crest, zcr, speech_ratio, high_ratio


def _looks_speechlike_block(block: np.ndarray, sample_rate: int) -> bool:
    """Conservative post-speech continuation detector.

    Isolated transients should *not* extend the utterance. Real syllables normally
    occupy several adjacent 20 ms frames, so the caller still requires a sustained
    run before updating the endpoint timer.
    """
    crest, zcr, speech_ratio, high_ratio = _speech_shape_metrics(block, sample_rate)
    return (
        crest <= 12.5
        and zcr <= 0.40
        and speech_ratio >= 0.40
        and high_ratio <= 0.46
    )


def _audio_worker_main(command_queue: Any, event_queue: Any) -> None:
    """Run all PortAudio work inside the dedicated audio subprocess."""

    try:
        import sounddevice as sd
    except BaseException as exc:
        _queue_event(
            event_queue,
            {
                "type": "backend_error",
                "message": (
                    "Не удалось загрузить sounddevice. Запустите setup.ps1. "
                    f"Причина: {type(exc).__name__}: {exc}"
                ),
            },
            critical=True,
        )
        return

    from jarvis.runtime.audio_history155 import AudioHistory
    from jarvis.runtime.wake_onset158 import PostWakeOnset
    history = AudioHistory(12.0)
    capture_started_at = 0.0
    last_finished_at = 0.0
    promotion_deadline = 0.0
    post_wake_onset = None
    stream: Any | None = None
    stream_token = ""
    stream_blocksize = 1024
    stream_dtype = "float32"
    source_rate = 48000
    device_name = "Микрофон"
    host_name = ""

    mode = "idle"
    session = 0
    target_rate = 16000
    sensitivity = 90
    noise_rejection_percent = 55
    max_seconds = 16.0
    start_timeout = 6.0
    silence_seconds = 1.25
    fixed_seconds = 3.0
    partial_interval = 2.0
    partial_max_updates = 1
    wake_window_seconds = 2.40
    wake_silence_seconds = 0.12
    wake_probe_interval_seconds = 0.14
    wake_probe_enabled = False
    wake_probe_max_per_utterance = 2
    wake_probe_min_voice_seconds = 0.15
    wake_probe_window_seconds = 0.72
    global_preroll_seconds = 1.15
    command_min_voice_ms = 120
    ignore_nonlinguistic_noise = True
    adaptive_endpoint = True
    short_silence_seconds = 0.40
    normal_silence_seconds = 0.68

    chunks: list[np.ndarray] = []
    rolling: collections.deque[np.ndarray] = collections.deque()
    rolling_samples = 0
    global_preroll: collections.deque[np.ndarray] = collections.deque()
    global_preroll_samples = 0
    started_at = 0.0
    last_voice_at = 0.0
    last_strong_voice_at = 0.0
    last_partial_at = 0.0
    last_meter_at = 0.0
    last_wake_probe_at = 0.0
    wake_speech_started_at = 0.0
    wake_probe_count = 0
    speech_started = False
    partial_updates = 0
    wake_voiced_run_samples = 0
    command_voiced_run_samples = 0
    command_continuation_run_samples = 0
    noise_floor = 0.00012
    base_threshold = 0.0010
    shutting_down = False
    correlation_id = ""

    def reset_state(new_mode: str, now: float, include_preroll: bool) -> None:
        nonlocal mode, chunks, rolling, rolling_samples, started_at, last_voice_at, last_strong_voice_at
        nonlocal last_partial_at, speech_started, noise_floor, partial_updates
        nonlocal wake_voiced_run_samples, command_voiced_run_samples, command_continuation_run_samples, last_wake_probe_at
        nonlocal wake_speech_started_at, wake_probe_count
        nonlocal correlation_id, capture_started_at, promotion_deadline, post_wake_onset
        post_wake_onset = None
        promotion_deadline = 0.0
        if not (mode == "wake" and new_mode == "command" and correlation_id):
            correlation_id = uuid.uuid4().hex
        mode = new_mode
        chunks = []
        rolling = collections.deque()
        rolling_samples = 0
        if include_preroll and global_preroll:
            chunks = [np.array(block, dtype=np.float32, copy=True) for block in global_preroll]
        started_at = now
        capture_started_at = now - sum(x.size for x in chunks) / source_rate
        last_voice_at = now
        last_strong_voice_at = now
        last_partial_at = now
        speech_started = new_mode == "fixed"
        partial_updates = 0
        wake_voiced_run_samples = 0
        command_voiced_run_samples = 0
        command_continuation_run_samples = 0
        last_wake_probe_at = 0.0
        wake_speech_started_at = 0.0
        wake_probe_count = 0
        noise_floor = 0.00012

    def close_stream() -> None:
        nonlocal stream, stream_token, last_finished_at
        history.clear()
        last_finished_at = 0.0
        current = stream
        stream = None
        stream_token = ""
        if current is not None:
            try:
                current.abort()
            except Exception:
                try:
                    current.stop()
                except Exception:
                    pass
            try:
                current.close()
            except Exception:
                pass

    def ensure_stream(requested_device: str, safe_mode: bool, fallback_index: int) -> None:
        nonlocal stream, stream_token, source_rate, device_name, host_name
        nonlocal stream_blocksize, stream_dtype
        index, description, token, native_rate, selected_host = _resolve_input_device(
            sd, requested_device, safe_mode=safe_mode, fallback_index=fallback_index
        )
        if stream is not None and token == stream_token:
            return
        close_stream()
        opened, rate, blocksize, dtype = _open_stream(sd, index, native_rate)
        stream = opened
        stream_token = token
        source_rate = rate
        stream_blocksize = blocksize
        stream_dtype = dtype
        device_name = description
        host_name = selected_host

    def emit_finished(current_session: int, *, trim: bool = True, reason: str = "") -> None:
        nonlocal mode, chunks, last_finished_at
        if promotion_deadline and time.monotonic() < promotion_deadline:
            return
        last_finished_at = time.monotonic()
        # If command-mode VAD never saw speech, do not send the rolling pre-roll
        # (usually room silence) to Whisper. Older builds transcribed that silence,
        # which caused hallucinated commands and unnecessary STT timeouts.
        if trim and not speech_started:
            audio = np.array([], dtype=np.float32)
        elif chunks:
            audio = np.concatenate(chunks).astype(np.float32, copy=False)
        else:
            audio = np.array([], dtype=np.float32)
        if trim and audio.size:
            frame = max(80, int(source_rate * 0.02))
            usable = audio.size - audio.size % frame
            if usable >= frame:
                frames = audio[:usable].reshape(-1, frame)
                rms = np.sqrt(np.mean(np.square(frames), axis=1, dtype=np.float64))
                # Be deliberately conservative at the tail. Users naturally drop
                # volume on the final one or two words; trimming at the same energy
                # margin used by VAD can erase those syllables before Whisper sees
                # them. A slightly lower trim gate plus ~0.8 s tail costs almost no
                # CPU but materially improves command endings.
                threshold = max(base_threshold * 0.24, noise_floor * 0.72, 0.000075)
                active = np.flatnonzero(rms >= threshold)
                if active.size:
                    start = max(0, int(active[0]) - 24) * frame
                    end = min(frames.shape[0], int(active[-1]) + 58) * frame
                    audio = audio[start : min(audio.size, end)].copy()
        raw, rate = _pack_recording(audio, source_rate, target_rate)
        _queue_event(
            event_queue,
            {"type": "finished", "session": current_session, "audio": raw, "sample_rate": rate,
             "correlation_id": correlation_id, "captured_at": time.monotonic(),
             "speech_ended_at": last_voice_at, "capture_started_at": capture_started_at,
             "completed_utterance": True,
             "empty_reason": "wake_start_timeout" if post_wake_onset is not None and not speech_started else "",
             "end_reason": str(reason or ""),
             "wake_candidate": bool(post_wake_onset is not None and speech_started
                                    and not post_wake_onset.separated)},
            critical=True,
        )
        # If a rolling probe caught the address before its last syllable, that
        # trailing voice may be either wake tail or a one-flow command. Validate
        # it while still buffering later speech and displaying "Listening".
        mode = "command_wait" if post_wake_onset is not None and speech_started and not post_wake_onset.separated else "idle"
        chunks = []

    while not shutting_down:
        for _ in range(16):
            try:
                command = command_queue.get_nowait()
            except queue.Empty:
                break
            except (EOFError, OSError, KeyboardInterrupt):
                shutting_down = True
                break
            if not isinstance(command, dict):
                continue
            kind = str(command.get("cmd", ""))
            if kind == "shutdown":
                shutting_down = True
                break
            if kind == "restart":
                close_stream()
                mode = "idle"
                continue
            if kind == "stop":
                last_finished_at = 0.0
                requested_session = int(command.get("session", session))
                if requested_session == session and mode != "idle":
                    if bool(command.get("emit_result", False)) and mode in {"command", "fixed"}:
                        emit_finished(session, trim=mode == "command", reason="stopped")
                    else:
                        mode = "idle"
                        chunks = []
                        rolling.clear()
                        rolling_samples = 0
                        _queue_event(event_queue, {"type": "stopped", "session": session})
                continue
            if kind == "promote":
                # Keep the existing PortAudio InputStream open. Only reset the
                # logical capture state and session so the UI can move from wake
                # to command mode without any native stop/start at the transition.
                try:
                    if stream is None or mode != "wake":
                        raise RuntimeError("Нет активного wake-аудиопотока для переключения")
                    session = int(command.get("session", session + 1))
                    target_rate = int(command.get("target_rate", target_rate))
                    sensitivity = max(1, min(100, int(command.get("sensitivity", sensitivity))))
                    noise_rejection_percent = max(0, min(100, int(command.get("noise_rejection_percent", noise_rejection_percent))))
                    base_threshold = 10 ** (-(44.0 + sensitivity * 0.24) / 20.0)
                    max_seconds = max(3.0, float(command.get("max_seconds", 16.0)))
                    start_timeout = max(1.5, float(command.get("start_timeout", 6.0)))
                    silence_seconds = max(0.45, float(command.get("silence_seconds", 1.25)))
                    command_min_voice_ms = max(35, min(240, int(command.get("command_min_voice_ms", 120))))
                    ignore_nonlinguistic_noise = bool(command.get("ignore_nonlinguistic_noise", True))
                    adaptive_endpoint = bool(command.get("adaptive_endpoint", True))
                    short_silence_seconds = max(0.30, min(0.75, float(command.get("short_silence_seconds", 0.40))))
                    normal_silence_seconds = max(0.45, min(1.15, float(command.get("normal_silence_seconds", 0.68))))
                    global_preroll_seconds = max(0.75, min(1.80, float(command.get("preroll_seconds", global_preroll_seconds))))
                    partial_interval = max(1.2, min(4.0, float(command.get("partial_interval", 2.0))))
                    partial_max_updates = max(0, min(2, int(command.get("partial_max_updates", 1))))
                    now = time.monotonic()
                    onset = float(command.get("capture_started_at", 0.0) or 0.0)
                    if not onset:
                        onset = wake_speech_started_at - 0.45 if wake_speech_started_at else now - 1.15
                    seed = history.since(onset, now)
                    reset_state("command", now, include_preroll=False)
                    chunks = seed
                    capture_started_at = onset if seed else now
                    # The retained address is not command onset. In 157 any seed
                    # armed the 400 ms endpoint, so saying only "Jarvis" closed
                    # capture immediately and transcribed the address again.
                    speech_started = bool(seed) and bool(command.get("wake_has_command", False))
                    if not speech_started:
                        boundary = float(command.get("wake_captured_at", 0.0) or now)
                        if not math.isfinite(boundary) or not 0 <= now - boundary <= 11.0:
                            boundary = now
                        post_wake_onset = PostWakeOnset(boundary)
                        reject = noise_rejection_percent / 100.0
                        onset_threshold = max(base_threshold * (1.25 + .55 * reject),
                                              noise_floor * (1.80 + 1.20 * reject),
                                              .00035 + .00018 * reject)
                        # ASR can finish after the user has already spoken the
                        # command. Replay only audio newer than its input probe;
                        # keeping the whole seed must not itself imply speech.
                        suffix = history.since(boundary, now)
                        cursor = now - sum(x.size for x in suffix) / source_rate
                        for block in suffix:
                            cursor += block.size / source_rate
                            post_wake_onset.observe(block, source_rate, cursor,
                                                    threshold=onset_threshold,
                                                    shape=_looks_speechlike_block)
                        speech_started = post_wake_onset.confirmed
                        if speech_started:
                            last_voice_at = post_wake_onset.last_voice_at
                            last_strong_voice_at = last_voice_at
                    last_finished_at = 0.0
                    if speech_started and bool(command.get("completed_utterance", False)):
                        promotion_deadline = now + 0.25
                    _queue_event(
                        event_queue,
                        {
                            "type": "started",
                            "session": session,
                            "mode": "command",
                            "device": device_name,
                            "host": host_name,
                            "token": stream_token,
                            "sample_rate": source_rate,
                            "safe_mode": bool(command.get("safe_mode", False)),
                        },
                        critical=True,
                    )
                except BaseException as exc:
                    _queue_event(
                        event_queue,
                        {
                            "type": "error",
                            "session": int(command.get("session", session)),
                            "message": f"{type(exc).__name__}: {exc}",
                        },
                        critical=True,
                    )
                continue
            if kind != "start":
                continue

            try:
                requested_device = str(command.get("device", "") or "")
                safe_mode = bool(command.get("safe_mode", False))
                fallback_index = max(0, int(command.get("host_fallback", 0)))
                ensure_stream(requested_device, safe_mode, fallback_index)
                session = int(command.get("session", session + 1))
                target_rate = int(command.get("target_rate", 16000))
                sensitivity = max(1, min(100, int(command.get("sensitivity", 90))))
                noise_rejection_percent = max(0, min(100, int(command.get("noise_rejection_percent", 55))))
                base_threshold = 10 ** (-(44.0 + sensitivity * 0.24) / 20.0)
                max_seconds = max(3.0, float(command.get("max_seconds", 16.0)))
                start_timeout = max(1.5, float(command.get("start_timeout", 6.0)))
                silence_seconds = max(0.45, float(command.get("silence_seconds", 1.25)))
                command_min_voice_ms = max(35, min(240, int(command.get("command_min_voice_ms", 120))))
                ignore_nonlinguistic_noise = bool(command.get("ignore_nonlinguistic_noise", True))
                adaptive_endpoint = bool(command.get("adaptive_endpoint", True))
                short_silence_seconds = max(0.30, min(0.75, float(command.get("short_silence_seconds", 0.40))))
                normal_silence_seconds = max(0.45, min(1.15, float(command.get("normal_silence_seconds", 0.68))))
                global_preroll_seconds = max(0.75, min(1.80, float(command.get("preroll_seconds", global_preroll_seconds))))
                fixed_seconds = max(0.5, float(command.get("fixed_seconds", 3.0)))
                partial_interval = max(
                    1.2, min(4.0, float(command.get("partial_interval", 2.0)))
                )
                partial_max_updates = max(
                    0, min(2, int(command.get("partial_max_updates", 1)))
                )
                wake_window_seconds = max(
                    1.6, min(4.5, float(command.get("wake_window", 2.40)))
                )
                wake_silence_seconds = max(
                    0.12, min(1.2, float(command.get("wake_silence", 0.16)))
                )
                wake_probe_interval_seconds = max(
                    0.12, min(2.0, float(command.get("wake_probe_interval", 0.14)))
                )
                wake_probe_enabled = bool(command.get("wake_probe_enabled", False))
                wake_probe_max_per_utterance = max(1, min(3, int(command.get("wake_probe_max_per_utterance", 2))))
                wake_probe_min_voice_seconds = max(0.14, min(0.80, float(command.get("wake_probe_min_voice_seconds", 0.15))))
                wake_probe_window_seconds = max(0.50, min(1.40, float(command.get("wake_probe_window_seconds", 0.72))))
                requested_mode = str(command.get("mode", "command"))
                now = time.monotonic()
                resume_at = last_finished_at if command.get("resume_from_last_finish") else 0.0
                reset_state(requested_mode, now, include_preroll=requested_mode == "command")
                if requested_mode == "command" and command.get("resume_from_last_finish"):
                    chunks = history.since(resume_at, now)
                    capture_started_at = resume_at if chunks else now
                    speech_started = any(float(np.max(np.abs(x))) > base_threshold * 2 for x in chunks if x.size)
                last_finished_at = 0.0
                _queue_event(
                    event_queue,
                    {
                        "type": "started",
                        "session": session,
                        "mode": requested_mode,
                        "device": device_name,
                        "host": host_name,
                        "token": stream_token,
                        "sample_rate": source_rate,
                        "safe_mode": safe_mode,
                    },
                    critical=True,
                )
            except BaseException as exc:
                mode = "idle"
                _queue_event(
                    event_queue,
                    {
                        "type": "error",
                        "session": int(command.get("session", -1)),
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                    critical=True,
                )

        if shutting_down:
            break

        if stream is None:
            time.sleep(0.04)
            continue

        try:
            data, overflowed = stream.read(stream_blocksize)
            if overflowed:
                _queue_event(
                    event_queue,
                    {"type": "warning", "message": "Переполнение входного аудиобуфера"},
                )
        except BaseException as exc:
            close_stream()
            failed_session = session
            mode = "idle"
            _queue_event(
                event_queue,
                {
                    "type": "error",
                    "session": failed_session,
                    "message": f"Ошибка аудиопотока: {type(exc).__name__}: {exc}",
                },
                critical=True,
            )
            continue

        now = time.monotonic()
        block = np.asarray(data)
        if block.ndim > 1:
            block = block[:, 0]
        if stream_dtype == "int16":
            block = block.astype(np.float32) / 32768.0
        else:
            # sounddevice returns a fresh array for every blocking read, so a
            # second float32 copy is unnecessary. Keeping the view alive in the
            # short deques avoids ~47 heap allocations/sec on a 48 kHz/1024 stream.
            block = block.astype(np.float32, copy=False)
        if not block.size:
            continue

        history.append(now, block, source_rate)
        if mode != "ambient":
            global_preroll.append(block)
            global_preroll_samples += block.size
        # HOTFIX43: keep enough audio to bridge a streaming Jarvis hit directly
        # into the following command ("Джарвис, открой ...") without forcing the
        # user to pause.  The buffer is still short enough to avoid dragging old
        # room conversation into command STT.
        if mode != "ambient":
            max_preroll = max(1, int(source_rate * global_preroll_seconds))
            while global_preroll and global_preroll_samples > max_preroll:
                removed = global_preroll.popleft()
                global_preroll_samples -= removed.size

        if mode == "idle":
            continue

        rms = float(np.linalg.norm(block) / math.sqrt(max(1, block.size)))
        peak = float(np.max(np.abs(block))) if block.size else 0.0
        if not math.isfinite(rms):
            rms = 0.0
        if not math.isfinite(peak):
            peak = 0.0
        # Audio DSP remains independent from UI FPS.  Wake standby publishes a
        # modest 12.5 Hz target stream: enough samples for a visually fluid 30 Hz
        # interpolated meter without resurrecting the old 40/120 Hz audio/UI poll.
        # Command capture stays slightly denser because endpointing is active.
        meter_interval = 0.055 if mode in {"command", "fixed"} else 0.080
        if mode != "ambient" and now - last_meter_at >= meter_interval:
            last_meter_at = now
            meter = min(
                100,
                int(100 * math.sqrt(min(1.0, max(rms, peak * 0.25) / 0.10))),
            )
            _queue_event(event_queue, {"type": "level", "session": session, "value": meter})

        if mode in {"wake", "ambient"}:
            # CPU-optimized two-stage wake path. The PortAudio loop and energy/VAD
            # checks are cheap and stay continuous. Expensive tiny-Whisper probes are
            # emitted only after sustained voice is confirmed and are strictly bounded
            # per utterance, instead of firing every few hundred milliseconds whenever
            # ambient sound crosses an energy threshold.
            reject = max(0.0, min(1.0, noise_rejection_percent / 100.0))

            trigger = max(
                base_threshold * (0.40 + 0.12 * reject),
                noise_floor * (1.30 + 0.38 * reject),
                0.000120 + 0.000055 * reject,
            )
            continuation = max(
                base_threshold * (0.26 + 0.06 * reject),
                noise_floor * (1.00 + 0.10 * reject),
                0.000080 + 0.000025 * reject,
            )
            voiced = rms >= trigger or peak >= trigger * 3.0
            continuing = rms >= continuation or peak >= continuation * 3.0

            if not speech_started:
                rolling.append(block)
                rolling_samples += block.size
                preroll_limit = max(1, int(source_rate * 0.68))
                while rolling and rolling_samples > preroll_limit:
                    removed = rolling.popleft()
                    rolling_samples -= removed.size
                if not voiced:
                    # Long-running microphones used to let a fan/game/keyboard
                    # slowly ratchet the adaptive floor upward.  Winsorise each
                    # sample and apply a hard, sensitivity-relative ceiling so a
                    # short command cannot become progressively harder to hear.
                    floor_sample = min(rms, max(noise_floor * 2.4, base_threshold * 2.2, 0.0025))
                    floor_ceiling = max(0.0022, base_threshold * 5.0)
                    noise_floor = min(floor_ceiling, noise_floor * 0.972 + floor_sample * 0.028)
                    wake_voiced_run_samples = 0
                    wake_probe_count = 0
                    wake_speech_started_at = 0.0
                else:
                    wake_voiced_run_samples += block.size
                    required_voice = int(source_rate * (0.055 + 0.055 * reject))
                    if wake_voiced_run_samples >= required_voice:
                        speech_started = True
                        correlation_id = uuid.uuid4().hex
                        wake_speech_started_at = now
                        wake_probe_count = 0
                        # Blocks are immutable after read and the list keeps them
                        # alive after the deque is cleared; copying each block here
                        # only creates avoidable wake-standby allocation churn.
                        chunks = list(rolling)
                        rolling.clear()
                        rolling_samples = 0
                        last_voice_at = now
            else:
                chunks.append(block)
                if continuing:
                    last_voice_at = now

                # First streaming probe starts after a short stable speech prefix
                # (~220 ms by default). A second rescue probe is allowed only
                # for a longer continuing phrase. This typically changes 3-10 tiny
                # Whisper runs per utterance into 1-2 without losing same-utterance
                # "Jarvis, open ..." promotion.
                active_voice_seconds = max(0.0, now - wake_speech_started_at)
                probe_due = (
                    mode == "wake"
                    and wake_probe_enabled
                    and wake_probe_count < wake_probe_max_per_utterance
                    and active_voice_seconds >= wake_probe_min_voice_seconds
                    and now - last_wake_probe_at >= wake_probe_interval_seconds
                )
                if probe_due:
                    # The rescue probe should be later than the first, not merely the
                    # next PortAudio block after the cooldown.
                    if wake_probe_count == 0 or active_voice_seconds >= max(0.58, wake_probe_min_voice_seconds + 0.30):
                        try:
                            # Keep the name's onset, even when a configured
                            # late/third probe arrives after the rolling window.
                            # Cropping the LAST global-preroll samples could feed
                            # only "...vis, turn on" to the binary wake decoder.
                            from jarvis.runtime.wake_audio161 import wake_probe_prefix
                            utterance_audio = np.concatenate(chunks).astype(np.float32, copy=False)
                            onset_guard = 0.055 + 0.055 * reject
                            # A later rescue must include newly spoken phonemes,
                            # not repeatedly decode the same fixed first slice.
                            # Grow only that existing second/third probe, bounded
                            # by the same 1.4 s cap; no extra native requests.
                            probe_window = wake_probe_window_seconds
                            if wake_probe_count > 0:
                                probe_window = min(1.40, max(
                                    probe_window, active_voice_seconds + onset_guard + 0.10,
                                ))
                            probe_audio, probe_start = wake_probe_prefix(
                                utterance_audio, source_rate, now=now,
                                stable_onset=wake_speech_started_at,
                                window_seconds=probe_window,
                                onset_guard_seconds=onset_guard,
                            )
                            probe_raw, probe_rate = _pack_recording(probe_audio, source_rate, target_rate)
                            _queue_event(
                                event_queue,
                                {
                                    "type": "wake_probe",
                                    "session": session,
                                    "audio": probe_raw,
                                    "sample_rate": probe_rate,
                                    "correlation_id": correlation_id,
                                    "captured_at": now,
                                    "capture_started_at": probe_start,
                                },
                            )
                        finally:
                            wake_probe_count += 1
                            last_wake_probe_at = now

                utterance_seconds = active_voice_seconds
                ended = now - last_voice_at >= wake_silence_seconds
                too_long = utterance_seconds >= wake_window_seconds
                if ended or too_long:
                    audio = np.concatenate(chunks).astype(np.float32, copy=False)
                    # Keep the leading wake word and its preroll. Cropping to
                    # the LAST window discarded "Jarvis" in "Jarvis, open ...".
                    # Memory stays bounded by one wake window plus capture preroll.
                    wanted = int(source_rate * (wake_window_seconds + 0.68))
                    if audio.size > wanted:
                        audio = audio[:wanted]
                    packed, rate = _pack_recording(audio, source_rate, target_rate)
                    _queue_event(
                        event_queue,
                        {
                            "type": "wake_chunk" if mode == "wake" else "ambient_chunk",
                            "session": session,
                            "audio": packed,
                            "sample_rate": rate,
                            "correlation_id": correlation_id,
                            "captured_at": now,
                            "speech_ended_at": last_voice_at,
                            "capture_started_at": now - audio.size / source_rate,
                            "completed_utterance": bool(ended),
                        },
                    )
                    chunks = []
                    rolling.clear()
                    rolling_samples = 0
                    speech_started = False
                    wake_voiced_run_samples = 0
                    wake_probe_count = 0
                    wake_speech_started_at = 0.0
                    noise_floor = max(0.00010, noise_floor * 0.90)
                    started_at = now

        elif mode in {"command", "fixed"}:
            chunks.append(block)
            if mode == "command":
                reject = max(0.0, min(1.0, noise_rejection_percent / 100.0))
                quiet_limit = max(
                    base_threshold * (1.45 + 0.45 * reject),
                    noise_floor * (1.45 + 0.70 * reject),
                )
                if not speech_started and rms < quiet_limit and peak < quiet_limit * 3.2:
                    floor_sample = min(rms, max(noise_floor * 2.6, base_threshold * 2.6, 0.0030))
                    floor_ceiling = max(0.0030, base_threshold * 6.0)
                    noise_floor = min(floor_ceiling, noise_floor * 0.95 + floor_sample * 0.05)
                # Keyboard/mouse clicks have high peaks but very short duration.
                # Noise rejection raises both the adaptive energy margin and the
                # required sustained voiced run, while sensitivity still controls
                # how quietly the user may speak.
                trigger = max(
                    base_threshold * (1.25 + 0.55 * reject),
                    noise_floor * (1.80 + 1.20 * reject),
                    0.00035 + 0.00018 * reject,
                )
                voiced = rms >= trigger or peak >= trigger * (3.4 + 0.8 * reject)
                if not speech_started and post_wake_onset is not None:
                    speech_started = post_wake_onset.observe(
                        block, source_rate, now, threshold=trigger,
                        shape=_looks_speechlike_block,
                    )
                    if speech_started:
                        last_voice_at = post_wake_onset.last_voice_at
                        last_strong_voice_at = last_voice_at
                        command_continuation_run_samples = 0
                elif not speech_started:
                    if voiced:
                        command_voiced_run_samples += block.size
                    else:
                        command_voiced_run_samples = max(0, command_voiced_run_samples - block.size * 2)
                    required_ms = command_min_voice_ms + int(110 * reject)
                    required_samples = max(1, int(source_rate * required_ms / 1000.0))
                    if command_voiced_run_samples >= required_samples:
                        speech_started = True
                        last_voice_at = now
                        last_strong_voice_at = now
                        command_continuation_run_samples = 0
                else:
                    # Once real speech has started, follow quiet endings more
                    # sensitively than the initial trigger. This prevents the last
                    # softly-spoken words from looking like silence while keeping
                    # the stricter start gate for keyboard/mouse noise.
                    continuation = max(
                        base_threshold * (0.38 + 0.10 * reject),
                        noise_floor * (0.98 + 0.14 * reject),
                        0.00010 + 0.000035 * reject,
                    )
                    # HOTFIX9: after real speech starts, do not let isolated keyboard
                    # clicks, mouse clicks or breathing keep extending the command
                    # forever. Quiet final syllables are still accepted for a short
                    # grace window after a clearly speech-like block.
                    speechlike_shape = (
                        True
                        if not ignore_nonlinguistic_noise
                        else _looks_speechlike_block(block, source_rate)
                    )
                    strong_continuation = speechlike_shape and (
                        rms >= continuation * 1.18
                        or (rms >= continuation * 0.78 and peak >= continuation * 2.15)
                    )
                    weak_continuation = speechlike_shape and (
                        rms >= continuation
                        or (rms >= continuation * 0.58 and peak >= continuation * 2.35)
                    )
                    if strong_continuation or weak_continuation:
                        # HOTFIX12: no single frame is allowed to extend a command.
                        # A click can be loud enough to beat any energy threshold;
                        # real speech, however, persists across adjacent frames.
                        command_continuation_run_samples += block.size
                        required = max(
                            1,
                            int(source_rate * (0.060 if strong_continuation else 0.085)),
                        )
                        if command_continuation_run_samples >= required:
                            last_voice_at = now
                            if strong_continuation:
                                last_strong_voice_at = now
                            # Keep a little history instead of resetting to zero so
                            # continuous speech does not repeatedly pay the full gate.
                            command_continuation_run_samples = required // 2
                    else:
                        # Keyboard/mouse/breathing noise must never refresh
                        # ``last_voice_at``. Once the last speech frame is old enough,
                        # the ordinary silence timer finishes the command even while
                        # non-linguistic noise continues.
                        command_continuation_run_samples = 0

                elapsed = now - started_at
                if (
                    partial_updates < partial_max_updates
                    and elapsed >= partial_interval
                    and speech_started
                    and now - last_partial_at >= partial_interval
                ):
                    last_partial_at = now
                    partial_updates += 1
                    audio = np.concatenate(chunks).astype(np.float32, copy=False)
                    packed, rate = _pack_recording(audio, source_rate, target_rate)
                    _queue_event(
                        event_queue,
                        {
                            "type": "partial",
                            "session": session,
                            "audio": packed,
                            "sample_rate": rate,
                            "correlation_id": correlation_id,
                            "captured_at": now,
                        },
                    )

        if mode == "command":
            elapsed = now - started_at
            if elapsed >= max_seconds:
                emit_finished(session, trim=True, reason="max_duration")
            elif not speech_started and elapsed >= start_timeout:
                emit_finished(session, trim=True, reason="no_speech_timeout")
            elif speech_started:
                endpoint_silence = silence_seconds
                if adaptive_endpoint:
                    # Short transport commands should close almost immediately,
                    # while longer natural requests keep a wider pause budget so
                    # an ordinary thinking pause does not split the sentence.
                    active_for = max(0.0, now - started_at)
                    if active_for <= 1.65:
                        endpoint_silence = min(endpoint_silence, short_silence_seconds)
                    elif active_for <= 4.5:
                        endpoint_silence = min(endpoint_silence, normal_silence_seconds)
                    else:
                        endpoint_silence = min(endpoint_silence, max(normal_silence_seconds, 0.72))
                if now - last_voice_at >= endpoint_silence:
                    emit_finished(session, trim=True, reason="endpoint")
        elif mode == "fixed" and now - started_at >= fixed_seconds:
            emit_finished(session, trim=False, reason="fixed")

    close_stream()
