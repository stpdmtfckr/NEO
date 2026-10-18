# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import unicodedata
import base64
import ctypes
import json
import os
import re
import sys
import time
from collections import OrderedDict
from typing import Any

import numpy as np

from jarvis.voice import _prepare_audio_for_whisper, _transcription_options
from jarvis.runtime.command_variations import command_vocabulary_score
from jarvis.runtime.multilingual_commands import command_vocabulary_score_for_language


def _respond(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _can_load_library(names: tuple[str, ...]) -> bool:
    loader = ctypes.WinDLL if os.name == "nt" else ctypes.CDLL
    for name in names:
        try:
            loader(name)
            return True
        except OSError:
            continue
    return False


def _cuda_runtime_ready() -> bool:
    """Return True only when CTranslate2's CUDA dependencies are actually loadable.

    ctranslate2.get_cuda_device_count() can report a CUDA GPU even when cuBLAS/cuDNN
    are missing from PATH. In that state model construction may succeed and the
    first transcription crashes with ``cublas64_12.dll is not found``. We verify
    the runtime libraries before choosing CUDA and otherwise use CPU immediately.
    """

    try:
        import ctranslate2

        if int(ctranslate2.get_cuda_device_count()) <= 0:
            return False
    except Exception:
        return False

    if os.name == "nt":
        # Current faster-whisper/CTranslate2 Windows CUDA builds use CUDA 12 and
        # cuDNN 9. Accept a few compatible names to avoid unnecessary CPU fallback.
        has_cublas = _can_load_library(("cublas64_12.dll",))
        has_cudnn = _can_load_library(("cudnn64_9.dll", "cudnn64_8.dll"))
        return has_cublas and has_cudnn
    return True


def _runtime_target(device: str, compute_type: str, *, cuda_disabled: bool = False) -> tuple[str, str]:
    requested_device = str(device or "auto").strip().casefold()
    requested_compute = str(compute_type or "auto").strip().casefold()

    if requested_device == "cpu":
        return "cpu", "int8" if requested_compute in {"auto", "float16", "int8_float16"} else requested_compute

    if requested_device == "cuda":
        if cuda_disabled or not _cuda_runtime_ready():
            # Explicit CUDA should still degrade gracefully for a desktop helper.
            return "cpu", "int8"
        return "cuda", "float16" if requested_compute == "auto" else requested_compute

    if not cuda_disabled and _cuda_runtime_ready():
        return "cuda", "float16" if requested_compute == "auto" else requested_compute
    return "cpu", "int8" if requested_compute in {"auto", "float16", "int8_float16"} else requested_compute


def _looks_like_cuda_runtime_failure(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".casefold()
    needles = (
        "cublas",
        "cudnn",
        "cuda",
        "nvcuda",
        "cufft",
        "cudart",
        "cannot be loaded",
        "not found or cannot be loaded",
    )
    return any(item in text for item in needles)




def _looks_like_prompt_context_overflow(exc: BaseException) -> bool:
    """Detect Whisper decoder-context overflow without depending on one library version."""
    text = f"{type(exc).__name__}: {exc}".casefold()
    return (
        "position encodings are defined for positions" in text
        or "position 448" in text
        or "position 449" in text
        or "maximum decoding length" in text
        or ("max_length" in text and "448" in text)
    )


def _clip_hint_text(model: Any, text: str, max_tokens: int) -> str:
    """Token-aware clipping for initial_prompt/hotwords.

    Whisper's decoder context is finite (448 positions in faster-whisper). Character
    limits are unreliable for mixed Cyrillic/Latin text, so use the model tokenizer
    when available and fall back to a conservative word cap.
    """
    value = str(text or "").strip()
    if not value or max_tokens <= 0:
        return ""
    tokenizer = getattr(model, "hf_tokenizer", None)
    try:
        if tokenizer is not None:
            encoded = tokenizer.encode(value)
            ids = list(getattr(encoded, "ids", []) or [])
            if len(ids) <= max_tokens:
                return value
            # Preserve the beginning: command hints are deliberately priority-ordered.
            return str(tokenizer.decode(ids[:max_tokens])).strip()
    except Exception:
        pass
    # Mixed RU/EN Whisper tokenization is often >1 token per word. A 2x word/token
    # safety factor keeps the fallback comfortably below the requested token budget.
    words = value.split()
    return " ".join(words[: max(1, max_tokens // 2)]).strip()


def _bounded_hint_kwargs(model: Any, kwargs: dict[str, Any], purpose: str) -> dict[str, Any]:
    """Reserve ample decoder space for the actual utterance."""
    safe = dict(kwargs)
    if purpose != "command":
        return safe
    # 96 + 112 hint tokens leaves more than half of Whisper's 448 decoder positions
    # available for control/output tokens. This is intentionally conservative.
    if safe.get("initial_prompt"):
        safe["initial_prompt"] = _clip_hint_text(model, str(safe["initial_prompt"]), 96)
    if safe.get("hotwords"):
        safe["hotwords"] = _clip_hint_text(model, str(safe["hotwords"]), 112)
    # Bound generated text as a second guard. Desktop commands are far shorter than
    # 128 decoder tokens, while this prevents prompt+generation from exhausting context.
    safe["max_new_tokens"] = min(int(safe.get("max_new_tokens") or 128), 128)
    return safe



def _decoded_text_and_quality(segments: Any, audio_seconds: float, language: str = "ru") -> tuple[str, float, bool, dict[str, float]]:
    items = list(segments)
    text = " ".join(str(getattr(segment, "text", "") or "").strip() for segment in items).strip()
    logprobs = [float(getattr(segment, "avg_logprob", -1.0) if getattr(segment, "avg_logprob", -1.0) is not None else -1.0) for segment in items]
    no_speech = [float(getattr(segment, "no_speech_prob", 0.0) or 0.0) for segment in items]
    avg_logprob = sum(logprobs) / len(logprobs) if logprobs else -2.0
    max_no_speech = max(no_speech) if no_speech else 0.0
    letters = [ch for ch in text if ch.isalpha()]
    weird = [ch for ch in letters if not any(script in unicodedata.name(ch, '') for script in ('LATIN', 'CYRILLIC'))]
    weird_ratio = len(weird) / max(1, len(letters))
    words = [w for w in re.split(r"\s+", text) if w]
    code = str(language or "ru").casefold()
    try:
        vocab = command_vocabulary_score_for_language(text, code)
    except Exception:
        vocab = command_vocabulary_score(text)
    suspicious = bool(
        avg_logprob < -0.82
        or max_no_speech > 0.72
        or weird_ratio > 0.08
        or (audio_seconds >= 3.2 and len(words) <= 1)
        or (audio_seconds >= 1.8 and not text)
        or (audio_seconds >= 1.4 and len(words) <= 5 and vocab < 0.08 and avg_logprob < -0.62)
    )
    # Acoustic confidence dominates; command-vocabulary coverage only helps choose
    # between two plausible decodes of the *same* audio. It never rewrites payloads.
    score = (
        avg_logprob
        - weird_ratio * 1.6
        - max(0.0, max_no_speech - 0.45) * 0.7
        + min(len(words), 8) * 0.012
        + vocab * 0.22
    )
    return text, score, suspicious, {
        "avg_logprob": avg_logprob,
        "max_no_speech": max_no_speech,
        "weird_ratio": weird_ratio,
        "word_count": float(len(words)),
        "command_vocab": vocab,
    }


def _script_profile(text: str) -> tuple[int, int]:
    value = str(text or "")
    return (
        len(re.findall(r"[а-яё]", value, flags=re.IGNORECASE)),
        len(re.findall(r"[a-z]", value, flags=re.IGNORECASE)),
    )


def _prefer_adaptive_candidate(
    primary: tuple[str, float, bool, dict[str, float]],
    alternate: tuple[str, float, bool, dict[str, float]],
    *, primary_language: str = "auto",
) -> bool:
    """Choose a rescue decode only when acoustics clearly justify it.

    Context/vocabulary is a tiebreaker, not permission to translate the user's
    sentence.  In particular, a Russian command shell must not become an English
    phrase merely because an English rescue recognizes a product-like word.
    """
    p_text, p_score, _p_suspicious, p_meta = primary
    a_text, a_score, _a_suspicious, a_meta = alternate
    if not a_text:
        return False
    if not p_text:
        return True
    code = str(primary_language or "auto").casefold()
    p_cyr, p_lat = _script_profile(p_text)
    a_cyr, a_lat = _script_profile(a_text)
    if code == "ru" and p_cyr >= 2:
        # English proper names inside Russian are fine, but an alternate that
        # discards the Cyrillic command shell is a translation/reinterpretation.
        if a_cyr == 0 and a_lat >= 2:
            return False
        if a_cyr + 2 < p_cyr and a_score < p_score + 0.22:
            return False
        # If the first Russian-language pass produced a suspicious hybrid with
        # ordinary Latin words, a same-language rescue that restores a coherent
        # Cyrillic command may win on near-equal acoustics.  This is a general
        # language-consistency rule, not a phrase dictionary.
        if p_lat >= 2 and a_cyr >= p_cyr + 3 and a_lat < p_lat and a_score > p_score - 0.04:
            return True
    if a_meta.get("weird_ratio", 0.0) + 0.08 < p_meta.get("weird_ratio", 0.0) and a_score > p_score - 0.02:
        return True
    # Vocabulary can help only with essentially equal acoustics; it no longer
    # overrules a clearly better primary decode.
    if a_meta.get("command_vocab", 0.0) > p_meta.get("command_vocab", 0.0) + 0.30 and a_score > p_score + 0.02:
        return True
    return a_score > p_score + 0.12


def _allow_cross_language_rescue(text: str, primary_language: str) -> bool:
    """Cross-language command rescue is disabled for Russian command mode.

    faster-whisper with ``language=ru`` can still preserve real English proper
    names when prompted to do so. A second English decoder instead tended to
    *translate/reinterpret* ordinary Russian payloads (the observed “My Wave”
    regression). Other language modes keep the old narrow Latin-script escape.
    """
    code = str(primary_language or "").casefold()
    if code in {"en", "ru"}:
        return False
    _cyr, latin = _script_profile(text)
    return latin >= 2


def _needs_same_language_consistency_rescue(text: str, primary_language: str) -> bool:
    code = str(primary_language or "").casefold()
    cyr, latin = _script_profile(text)
    return code == "ru" and cyr >= 3 and latin >= 2


def _request_audio(request: dict[str, Any], purpose: str) -> np.ndarray:
    """Validate the IPC boundary and convert supported PCM rates to Whisper's 16k."""
    raw = base64.b64decode(str(request.get("audio_b64", "")), validate=True)
    if len(raw) % 4:
        raise ValueError("Audio payload must contain complete float32 samples")
    sample_rate = int(request.get("sample_rate", 16000))
    if not 8000 <= sample_rate <= 192000:
        raise ValueError("Unsupported audio sample rate")
    audio = np.frombuffer(raw, dtype=np.float32).copy()
    if audio.size > sample_rate * 120:
        raise ValueError("Audio request exceeds 120 seconds")
    if audio.size and sample_rate != 16000:
        old_indices = np.arange(audio.size, dtype=np.float64)
        new_indices = np.arange(int(round(audio.size * 16000.0 / sample_rate)), dtype=np.float64) * (sample_rate / 16000.0)
        audio = np.interp(new_indices, old_indices, audio).astype(np.float32)
    return _prepare_audio_for_whisper(audio, purpose)


def main() -> int:
    # The persistent STT subprocess normally needs only the full command model and
    # the tiny partial/wake model. An unbounded dict retained every model/device/
    # thread combination ever selected in Settings and could consume gigabytes.
    models: OrderedDict[tuple[str, str, str, int], Any] = OrderedDict()
    max_cached_models = 2
    cuda_disabled = False

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            _respond({"id": "", "ok": False, "error": f"JSONDecodeError: {exc}"})
            continue
        if not isinstance(request, dict):
            _respond({"id": "", "ok": False, "error": "Request must be an object"})
            continue
        if request.get("cmd") == "shutdown":
            return 0
        request_id = str(request.get("id", ""))

        try:
            request_started_at = time.monotonic()
            purpose = str(request.get("purpose", "command"))
            audio = np.array([], dtype=np.float32) if purpose == "preload" else _request_audio(request, purpose)
            # Silence should neither load a model nor reuse the previous request's
            # tuple in this persistent process. This also covers NaN/Inf input.
            if purpose != "preload" and not audio.size:
                _respond({"id": request_id, "ok": True, "text": "", "score": 0.0,
                          "suspicious": False, "hypotheses": [], "decode_count": 0,
                          "decode_ms": 0.0, "device": "none", "compute_type": "none",
                          "cuda_fallback": cuda_disabled,
                          "correlation_id": str(request.get("correlation_id", request_id))})
                continue
            from faster_whisper import WhisperModel

            model_name = str(request.get("model_name", "small") or "small")
            req_device = str(request.get("device", "auto"))
            req_compute = str(request.get("compute_type", "auto"))
            cpu_threads = max(1, int(request.get("cpu_threads", 6)))
            device, compute_type = _runtime_target(
                req_device, req_compute, cuda_disabled=cuda_disabled
            )

            # A worker may have preloaded a CUDA Whisper model before a game was
            # launched. Switching later requests to CPU is not enough: the old
            # CTranslate2 CUDA model can remain cached and keep VRAM allocated.
            # Gameplay-safe requests explicitly evict those cached CUDA models.
            if device == "cpu" and bool(request.get("purge_cuda", False)):
                cuda_keys = [key for key in models if len(key) > 1 and key[1] == "cuda"]
                if cuda_keys:
                    import gc
                    for key in cuda_keys:
                        models.pop(key, None)
                    gc.collect()

            def get_model(target_device: str, target_compute: str):
                key = (model_name, target_device, target_compute, cpu_threads)
                model = models.get(key)
                if model is not None:
                    models.move_to_end(key)
                    return model

                def release(keep: int) -> None:
                    # 186: the worker serves one request at a time, so every cached
                    # model here is idle; free the least recently used ones *before*
                    # a new heavy model is built instead of only after it succeeded.
                    evicted = False
                    while len(models) > max(0, keep):
                        _old_key, old_model = models.popitem(last=False)
                        del old_model
                        evicted = True
                    if evicted:
                        import gc
                        gc.collect()

                def build():
                    return WhisperModel(
                        model_name,
                        device=target_device,
                        compute_type=target_compute,
                        cpu_threads=cpu_threads,
                        num_workers=1,
                    )

                release(max_cached_models - 1)
                try:
                    model = build()
                except MemoryError:
                    # Out of memory with idle residents left: release them all and
                    # try once more; a second failure is reported honestly.
                    if not models:
                        raise
                    release(0)
                    model = build()
                models[key] = model
                models.move_to_end(key)
                return model

            try:
                model = get_model(device, compute_type)
            except Exception as exc:
                if device != "cuda":
                    raise
                cuda_disabled = True
                device, compute_type = "cpu", "int8"
                model = get_model(device, compute_type)

            if purpose == "preload":
                _respond(
                    {
                        "id": request_id,
                        "ok": True,
                        "text": "",
                        "device": device,
                        "compute_type": compute_type,
                        "cuda_fallback": cuda_disabled,
                    }
                )
                continue

            decoded_candidates: list[dict[str, Any]] = []
            primary: tuple[str, float, bool, dict[str, float]] = ("", 0.0, False, {})
            decode_count = 0
            decode_seconds = 0.0
            quality_retries = max(0, min(2, int(request.get("max_quality_retries", 2))))
            if audio.size == 0:
                text = ""
            else:
                kwargs = _transcription_options(
                    purpose, request.get("language", "auto"),
                    int(request.get("noise_rejection_percent", 55) or 55),
                )
                prompt = str(request.get("initial_prompt", "") or "").strip()
                if prompt:
                    kwargs["initial_prompt"] = prompt
                hotwords = str(request.get("hotwords", "") or "").strip()
                if hotwords:
                    kwargs["hotwords"] = hotwords
                kwargs = _bounded_hint_kwargs(model, kwargs, purpose)
                audio_seconds = float(audio.size) / 16000.0
                if purpose == "wake" and bool(request.get("bounded_window", False)):
                    # NEO 168: a ~3 s wake utterance no longer pays for a 30 s encoder window.
                    from jarvis.runtime.wake_budget168 import decode_window_seconds
                    kwargs["chunk_length"] = decode_window_seconds(audio_seconds)

                def decode_with(target_model: Any, options: dict[str, Any]) -> tuple[str, float, bool, dict[str, float]]:
                    nonlocal decode_count, decode_seconds
                    decode_started_at = time.monotonic()
                    decode_count += 1
                    try:
                        segments, _info = target_model.transcribe(audio, **options)
                        return _decoded_text_and_quality(segments, audio_seconds, str(options.get("language", request.get("language", "ru")) or "ru"))
                    finally:
                        decode_seconds += time.monotonic() - decode_started_at

                def remember_candidate(source: str, value: tuple[str, float, bool, dict[str, float]]) -> None:
                    text_value, score_value, suspicious_value, meta_value = value
                    if not text_value or any(str(x.get("text", "")) == text_value for x in decoded_candidates):
                        return
                    decoded_candidates.append({
                        "text": text_value,
                        "score": float(score_value),
                        "suspicious": bool(suspicious_value),
                        "meta": dict(meta_value),
                        "source": source,
                    })

                try:
                    primary = decode_with(model, kwargs)
                    remember_candidate("primary_decode", primary)
                except BaseException as exc:
                    if _looks_like_prompt_context_overflow(exc):
                        retry_kwargs = dict(kwargs)
                        retry_kwargs.pop("hotwords", None)
                        retry_kwargs["initial_prompt"] = "Transcribe the spoken command verbatim. Preserve proper names. Do not translate."
                        retry_kwargs["max_new_tokens"] = 128
                        primary = decode_with(model, retry_kwargs)
                        remember_candidate("prompt_overflow_rescue", primary)
                    else:
                        if device != "cuda" or not _looks_like_cuda_runtime_failure(exc):
                            raise
                        cuda_disabled = True
                        import gc
                        for stale_key in [key for key in models if key[1] == "cuda"]:
                            models.pop(stale_key, None)
                        gc.collect()
                        device, compute_type = "cpu", "int8"
                        model = get_model(device, compute_type)
                        kwargs = _bounded_hint_kwargs(model, kwargs, purpose)
                        try:
                            primary = decode_with(model, kwargs)
                            remember_candidate("cuda_cpu_fallback", primary)
                        except BaseException as cpu_exc:
                            if not _looks_like_prompt_context_overflow(cpu_exc):
                                raise
                            retry_kwargs = dict(kwargs)
                            retry_kwargs.pop("hotwords", None)
                            retry_kwargs["initial_prompt"] = "Transcribe the spoken command verbatim. Preserve proper names. Do not translate."
                            retry_kwargs["max_new_tokens"] = 128
                            primary = decode_with(model, retry_kwargs)
                            remember_candidate("cpu_prompt_overflow_rescue", primary)

                # HOTFIX43: keep the normal path fast (one decode), but when the
                # acoustic/log-probability checks say the transcript is suspicious,
                # first retry in the *same* selected language with a different VAD
                # strategy.  This is much more useful for quiet Russian endings than
                # immediately asking an English decoder to reinterpret the phrase.
                # A final English rescue remains available for mixed proper names.
                primary_language = str(request.get("language", "ru") or "ru").casefold()
                needs_consistency_rescue = _needs_same_language_consistency_rescue(primary[0], primary_language)
                if purpose == "command" and quality_retries > 0 and bool(request.get("adaptive_decode_retry", True)) and (primary[2] or needs_consistency_rescue):
                    prompt = str(request.get("initial_prompt", "") or "").strip()
                    hotwords = str(request.get("hotwords", "") or "").strip()
                    # NEO 174: a rescue pass is worth its time only when it decodes differently.
                    # Without model VAD in the first pass (the default up to 70% noise rejection)
                    # the old same-language rescue repeated the first decode with one more beam,
                    # doubling the wait for nearly the same text. It now runs only when the
                    # first pass cut the audio with VAD; the unbiased pass only when a prompt or
                    # hotwords biased the first one.
                    if bool(kwargs.get("vad_filter", False)):
                        quality_retries -= 1
                        rescue_kwargs = _transcription_options(
                            "command", primary_language, int(request.get("noise_rejection_percent", 55) or 55)
                        )
                        rescue_kwargs["vad_filter"] = False
                        rescue_kwargs["beam_size"] = 8
                        rescue_kwargs["best_of"] = 5
                        rescue_kwargs["patience"] = 1.15
                        if prompt:
                            rescue_kwargs["initial_prompt"] = prompt
                        if hotwords:
                            rescue_kwargs["hotwords"] = hotwords
                        rescue_kwargs = _bounded_hint_kwargs(model, rescue_kwargs, "command")
                        try:
                            rescued = decode_with(model, rescue_kwargs)
                            remember_candidate("same_language_rescue", rescued)
                            if _prefer_adaptive_candidate(primary, rescued, primary_language=primary_language):
                                primary = rescued
                        except BaseException:
                            pass

                    biased = bool(prompt or hotwords)
                    # A suspicious text may come from the prompt/hotwords bias itself: one
                    # unbiased pass is the useful second opinion (it replaces the old repeat).
                    if quality_retries > 0 and biased and (primary[2] or needs_consistency_rescue):
                        quality_retries -= 1
                        alt_kwargs = _transcription_options(
                            "command", primary_language, int(request.get("noise_rejection_percent", 55) or 55)
                        )
                        alt_kwargs.pop("initial_prompt", None)
                        alt_kwargs.pop("hotwords", None)
                        alt_kwargs = _bounded_hint_kwargs(model, alt_kwargs, "command")
                        try:
                            alternate = decode_with(model, alt_kwargs)
                            remember_candidate("unbiased_same_language_rescue", alternate)
                            if _prefer_adaptive_candidate(primary, alternate, primary_language=primary_language):
                                primary = alternate
                        except BaseException:
                            # Quality retry is opportunistic; never turn a valid primary
                            # transcript into an error because the alternate pass failed.
                            pass
                remember_candidate("selected", primary)
                text = primary[0]

            hypotheses: list[dict[str, Any]] = []
            if bool(request.get("return_hypotheses", False)) and purpose == "command":
                try:
                    # Put the actually selected text first, then keep only a few
                    # bounded alternatives for the contextual speech resolver.
                    chosen = [x for x in decoded_candidates if str(x.get("text", "")) == text]
                    others = [x for x in decoded_candidates if str(x.get("text", "")) != text]
                    others.sort(key=lambda x: float(x.get("score", -9.0)), reverse=True)
                    hypotheses = (chosen[:1] + others)[:4]
                except Exception:
                    hypotheses = []

            _respond(
                {
                    "id": request_id,
                    "ok": True,
                    "text": text,
                    "device": device,
                    "compute_type": compute_type,
                    "cuda_fallback": cuda_disabled,
                    "score": float(primary[1]) if purpose != "preload" else 0.0,
                    "suspicious": bool(primary[2]) if purpose != "preload" else False,
                    "hypotheses": hypotheses,
                    "decode_count": decode_count,
                    "decode_ms": round(decode_seconds * 1000.0, 3),
                    "worker_ms": round((time.monotonic() - request_started_at) * 1000.0, 3),
                    "correlation_id": str(request.get("correlation_id", request_id)),
                }
            )
        except KeyboardInterrupt:
            return 0
        except BaseException as exc:
            _respond({"id": request_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
