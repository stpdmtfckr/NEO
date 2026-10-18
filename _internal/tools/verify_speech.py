"""Run an explicit local WAV corpus without launching or executing assistant tools.

Usage: python tools/verify_speech.py --corpus my_corpus.json --output speech_report.json
Manifest: [{"file":"wake.wav","language":"ru","purpose":"wake","wake":true},
           {"file":"command.wav","language":"de","expected":"öffne den ordner"}]
WAV PCM 8/16/24/32-bit supported; files are never uploaded or changed. A local
Whisper model directory is required, so this diagnostic never downloads models.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import platform
import re
import statistics
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.voice import RecordingResult, SpeechRecognizer, TranscriptionService
from jarvis.wake_matcher import match_wake_phrase


def read_wav(path: Path) -> RecordingResult:
    with wave.open(str(path), "rb") as source:
        if source.getcomptype() != "NONE":
            raise ValueError("Only uncompressed PCM WAV is supported")
        rate, channels, width = source.getframerate(), source.getnchannels(), source.getsampwidth()
        if source.getnframes() > rate * 120:
            raise ValueError("A diagnostic clip must be no longer than 120 seconds")
        raw = source.readframes(source.getnframes())
    if not raw:
        raise ValueError("WAV has no recorded audio frames")
    if width == 1:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        triples = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
        values = (values ^ 0x800000) - 0x800000
        samples = values.astype(np.float32) / 8388608.0
    elif width == 4:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported PCM width: {width}")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return RecordingResult(samples, rate)


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold(), flags=re.UNICODE).split())


def word_error_rate(reference: str, hypothesis: str) -> float:
    expected, actual = normalize(reference).split(), normalize(hypothesis).split()
    previous = list(range(len(actual) + 1))
    for i, left in enumerate(expected, 1):
        current = [i]
        for j, right in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1] / max(1, len(expected))


def score_transcript(item, text):
    checks = {}
    row = {}
    if 'expected' in item:
        maximum = float(item.get('max_wer', 0))
        if not math.isfinite(maximum) or not 0 <= maximum <= 1:
            raise ValueError('max_wer must be between 0 and 1')
        row['word_error_rate'] = word_error_rate(str(item['expected']), text)
        row['reference_words'] = len(normalize(str(item['expected'])).split())
        row['word_errors'] = round(row['word_error_rate'] * max(1, row['reference_words']))
        checks['transcript'] = row['word_error_rate'] <= maximum
    if 'wake' in item or 'expected_command' in item:
        matched, tail, _score = match_wake_phrase(text, str(item.get('wake_phrase', 'Jarvis')))
        row['wake_detected'] = matched
        if 'wake' in item:
            if type(item['wake']) is not bool:
                raise ValueError('wake must be a JSON boolean')
            checks['wake'] = matched == item['wake']
        if 'expected_command' in item:
            checks['command_tail'] = matched and normalize(tail) == normalize(str(item['expected_command']))
    row['checks'] = checks
    row['passed'] = all(checks.values()) if checks else None
    return row


def summarize(records):
    latencies = [r['asr_ms'] for r in records if 'asr_ms' in r]
    warm = [r['asr_ms'] for r in records if 'asr_ms' in r and not r.get('cold_model_call')]
    words = sum(r.get('reference_words', 0) for r in records)
    duration = sum(r.get('audio_seconds', 0) for r in records)
    return {'passed': sum(r.get('passed') is True for r in records),
            'failed': sum(r.get('passed') is False for r in records),
            'unscored': sum(r.get('passed') is None for r in records),
            'corpus_word_error_rate': sum(r.get('word_errors', 0) for r in records)/words if words else None,
            'median_asr_ms': statistics.median(latencies) if latencies else None,
            'p95_asr_ms': float(np.percentile(latencies, 95)) if latencies else None,
            'warm_p95_asr_ms': float(np.percentile(warm, 95)) if warm else None,
            'aggregate_real_time_factor': sum(latencies)/1000/duration if duration else None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True, help="Existing local faster-whisper model directory (no download)")
    parser.add_argument("--wake-model", type=Path, help="Optional existing tiny model directory for wake clips")
    parser.add_argument("--output", type=Path, default=Path("speech_report.json"))
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for model in (args.model, args.wake_model or args.model):
        if not model.is_dir() or not (model / "model.bin").is_file():
            parser.error(f"Existing local model.bin directory required: {model}")
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    if not isinstance(corpus, list) or not corpus:
        parser.error("Corpus must be a non-empty JSON array")
    report = {"schema_version": 1, "platform": platform.platform(), "python": platform.python_version(),
              "model": str(args.model), "wake_model": str(args.wake_model or args.model),
              "scope": "Local prerecorded WAV ASR only; no end-to-end microphone, VAD, UI, routing, GPU or game benchmark",
              "records": []}
    service = TranscriptionService("NEO-SpeechVerification")
    seen_models = set()
    try:
        for iteration in range(max(1, min(10, args.repeat))):
            for index, item in enumerate(corpus):
                row = {"index": index, "iteration": iteration, "file": str(item.get("file", ""))}
                try:
                    source = (args.corpus.parent / item["file"]).resolve()
                    recording = read_wav(source)
                    recording.correlation_id = f"speech-verification-{iteration}-{index}"
                    language = str(item.get("language", "ru"))
                    purpose = "wake" if item.get("purpose") == "wake" else "command"
                    settings = {"voice.stt_device": "cpu", "voice.stt_compute_type": "int8",
                                "voice.stt_cpu_threads": max(1, min(8, args.threads)), "wake_word.cpu_threads": 1,
                                "wake_word.force_cpu": True, "voice.command_language": language, "voice.language": language}
                    config = SimpleNamespace(get=lambda key, default=None: settings.get(key, default))
                    recognizer = SpeechRecognizer(config, service, str(args.wake_model or args.model) if purpose == "wake" else str(args.model), purpose=purpose)
                    model_key = str(args.wake_model or args.model) if purpose == 'wake' else str(args.model)
                    row['cold_model_call'] = model_key not in seen_models
                    began = time.perf_counter()
                    text = recognizer.transcribe(recording)
                    seen_models.add(model_key)
                    row.update({"text": text, "language": language, "purpose": purpose,
                                "asr_ms": round((time.perf_counter() - began) * 1000.0, 2),
                                "audio_seconds": recording.audio.size / recording.sample_rate})
                    row.update(score_transcript(item, text))
                except Exception as exc:
                    row.update({"passed": False, "error": f"{type(exc).__name__}: {exc}"})
                report["records"].append(row)
    finally:
        service.close()
    report['summary'] = summarize(report['records'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
