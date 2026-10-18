# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Isolated Windows system-audio loopback capture for ambient learning.

This helper is deliberately separate from the Qt process.  It uses SoundCard's
WASAPI loopback microphone, performs only cheap VAD/segmentation, and emits
float32 speech-like chunks over JSONL.  No audio is written to disk.
"""

import base64
import json
import os
import sys
import time
from collections import deque
from typing import Any

import numpy as np

from jarvis.audio_backend import _looks_speechlike_block, _resample
from jarvis.runtime.audio_provenance import compact_audio_envelope


def _emit(payload: dict[str, Any], audio: np.ndarray | None = None) -> None:
    row = dict(payload)
    if audio is not None:
        row["audio_b64"] = base64.b64encode(np.asarray(audio, dtype=np.float32).tobytes()).decode("ascii")
    sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _select_loopback(sc: Any, device: str = "auto") -> Any:
    speaker = sc.default_speaker()
    if speaker is None:
        raise RuntimeError("Default Windows speaker not found")
    candidates = list(sc.all_microphones(include_loopback=True))
    requested = str(device or "auto").strip().casefold()
    speaker_name = str(getattr(speaker, "name", "") or "").casefold()
    speaker_id = str(getattr(speaker, "id", "") or "").casefold()
    ranked: list[tuple[int, Any]] = []
    for mic in candidates:
        try:
            if not bool(getattr(mic, "isloopback", False)):
                continue
        except Exception:
            continue
        name = str(getattr(mic, "name", "") or "").casefold()
        ident = str(getattr(mic, "id", "") or "").casefold()
        if requested not in {"", "auto", "default"}:
            if requested not in {name, ident}:
                continue
            ranked.append((0, mic))
            continue
        score = 3
        if speaker_id and speaker_id in ident:
            score = 0
        elif speaker_name and speaker_name in name:
            score = 1
        elif speaker_name and any(part and part in name for part in speaker_name.split() if len(part) >= 4):
            score = 2
        ranked.append((score, mic))
    if not ranked:
        raise RuntimeError("No WASAPI loopback microphone exposed by SoundCard")
    ranked.sort(key=lambda x: x[0])
    return ranked[0][1]


def _capture_settings(argv: list[str]) -> dict[str, Any]:
    """Conservative IPC defaults: echo signatures only, never opt-in by accident."""
    raw = json.loads(argv[0]) if argv else {}
    if not isinstance(raw, dict):
        raise ValueError("Loopback settings must be an object")
    return {
        "transcription_enabled": bool(raw.get("transcription_enabled", False)),
        "echo_enabled": bool(raw.get("echo_enabled", True)),
        "device": str(raw.get("device", "auto") or "auto"),
        "silence_seconds": max(0.25, min(3.0, float(raw.get("silence_seconds", 0.85)))),
        "max_seconds": max(2.0, min(15.0, float(raw.get("max_seconds", 8.0)))),
        "min_seconds": max(0.3, min(3.0, float(raw.get("min_seconds", 1.2)))),
    }


def main() -> int:
    if os.name != "nt":
        _emit({"type": "error", "message": "System loopback is Windows-only"})
        return 2
    try:
        import soundcard as sc
    except BaseException as exc:
        _emit({"type": "error", "message": f"SoundCard unavailable: {type(exc).__name__}: {exc}"})
        return 3

    try:
        settings = _capture_settings(sys.argv[1:])
        transcription_enabled = settings["transcription_enabled"]
        echo_enabled = settings["echo_enabled"]
        if not transcription_enabled and not echo_enabled:
            return 0
        mic = _select_loopback(sc, settings["device"])
        initial_speaker_id = str(getattr(sc.default_speaker(), "id", "") or "")
        name = str(getattr(mic, "name", "System loopback") or "System loopback")
        source_rate = 48000
        target_rate = 16000
        block_frames = 2048
        silence_seconds = settings["silence_seconds"]
        max_seconds = settings["max_seconds"]
        min_seconds = min(settings["min_seconds"], max_seconds)
        noise_floor = 0.00035
        started = False
        voiced_run = 0
        chunks: list[np.ndarray] = []
        speech_started_at = 0.0
        last_voice_at = 0.0
        reference_chunks: deque[np.ndarray] = deque()
        reference_samples = 0
        last_reference_emit = 0.0
        last_device_check = 0.0
        _emit({"type": "started", "device": name, "sample_rate": source_rate, "provenance": "wasapi_loopback"})

        with mic.recorder(samplerate=source_rate, blocksize=block_frames) as recorder:
            while True:
                data = recorder.record(numframes=block_frames)
                block = np.asarray(data, dtype=np.float32)
                if block.ndim > 1:
                    block = np.mean(block, axis=1, dtype=np.float32)
                block = block.reshape(-1)
                if block.size == 0:
                    continue
                np.nan_to_num(block, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
                rms = float(np.sqrt(np.mean(np.square(block), dtype=np.float64)))
                peak = float(np.max(np.abs(block)))
                now = time.monotonic()
                if settings["device"].casefold() in {"auto", "default", ""} and now - last_device_check >= 3.0:
                    last_device_check = now
                    current_speaker_id = str(getattr(sc.default_speaker(), "id", "") or "")
                    if current_speaker_id != initial_speaker_id:
                        _emit({"type": "device_changed"})
                        return 0  # Owner restarts capture against the new endpoint.
                trigger = max(0.0014, noise_floor * 2.15)
                shape = _looks_speechlike_block(block, source_rate) if transcription_enabled else False
                voiced = shape and (rms >= trigger or peak >= trigger * 3.0)

                # Keep only a short in-memory loopback reference.  Every ~140 ms
                # emit a 48-float envelope signature, never raw PCM.  The desktop
                # wake path compares this with the microphone candidate to reject
                # words played by the computer's own speakers.
                if echo_enabled:
                    reference_chunks.append(block.copy())
                    reference_samples += int(block.size)
                reference_limit = int(source_rate * 1.10)
                while reference_chunks and reference_samples > reference_limit:
                    removed = reference_chunks.popleft()
                    reference_samples -= int(removed.size)
                if echo_enabled and now - last_reference_emit >= 0.14 and reference_samples >= int(source_rate * 0.34):
                    last_reference_emit = now
                    if peak >= 0.0010 or rms >= 0.00035:
                        ref_audio = np.concatenate(tuple(reference_chunks)).astype(np.float32, copy=False)
                        signature = compact_audio_envelope(ref_audio, source_rate)
                        if np.any(signature):
                            _emit({
                                "type": "echo_reference",
                                "provenance": "wasapi_loopback",
                                "captured_at": time.monotonic(),
                                "signature": [round(float(x), 6) for x in signature],
                                "rms": round(rms, 7),
                            })

                # Reference-only operation never collects speech chunks, sends
                # raw PCM through IPC, or starts a Whisper model.
                if not transcription_enabled:
                    continue

                if not started:
                    if voiced:
                        voiced_run += block.size
                    else:
                        voiced_run = max(0, voiced_run - block.size * 2)
                        floor_sample = min(rms, max(noise_floor * 2.0, 0.0025))
                        noise_floor = min(0.012, noise_floor * 0.975 + floor_sample * 0.025)
                    if voiced_run >= int(source_rate * 0.11):
                        started = True
                        chunks = [block.copy()]
                        speech_started_at = now
                        last_voice_at = now
                    continue

                chunks.append(block.copy())
                if voiced:
                    last_voice_at = now
                ended = now - last_voice_at >= silence_seconds
                too_long = now - speech_started_at >= max_seconds
                if not (ended or too_long):
                    continue

                audio = np.concatenate(chunks).astype(np.float32, copy=False) if chunks else np.array([], dtype=np.float32)
                duration = audio.size / float(source_rate)
                if duration >= min_seconds:
                    down = _resample(audio, source_rate, target_rate)
                    _emit({"type": "ambient_chunk", "sample_rate": target_rate}, down)
                started = False
                voiced_run = 0
                chunks = []
                speech_started_at = 0.0
                last_voice_at = 0.0
                noise_floor = max(0.00025, noise_floor * 0.93)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    except BaseException as exc:
        try:
            _emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
