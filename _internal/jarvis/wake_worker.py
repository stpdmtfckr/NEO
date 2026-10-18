# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import base64
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np


def _respond(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class _JarvisModel:
    def __init__(self) -> None:
        self.model = None
        self.model_path = ""

    def load(self):
        if self.model is not None:
            return self.model
        import openwakeword
        from openwakeword.model import Model

        package_root = Path(openwakeword.__file__).resolve().parent
        candidates = list((package_root / "resources" / "models").glob("*jarvis*.onnx"))
        if not candidates:
            raise FileNotFoundError("A local openWakeWord Jarvis model is required; live wake capture never downloads models")
        self.model_path = str(candidates[0])
        # All ONNX/openWakeWord native state intentionally lives only in this
        # helper process. A native crash here can no longer terminate Qt/JARVIS.
        self.model = Model(
            wakeword_models=[self.model_path],
            inference_framework="onnx",
            vad_threshold=0.0,
        )
        return self.model

    def score(self, audio: np.ndarray, sample_rate: int) -> float:
        model = self.load()
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        samples = np.nan_to_num(samples, copy=True, nan=0.0, posinf=0.0, neginf=0.0)
        if not samples.size:
            return 0.0
        if int(sample_rate) != 16000:
            x_old = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
            new_len = max(1, int(round(samples.size * 16000 / max(1, int(sample_rate)))))
            x_new = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
            samples = np.interp(x_new, x_old, samples).astype(np.float32)
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
        frame = 1280
        try:
            model.reset()
        except Exception:
            pass
        best = 0.0
        try:
            for start in range(0, pcm.size, frame):
                chunk = pcm[start : start + frame]
                if chunk.size < frame:
                    chunk = np.pad(chunk, (0, frame - chunk.size))
                prediction = model.predict(chunk)
                if isinstance(prediction, dict):
                    for key, value in prediction.items():
                        if "jarvis" in str(key).casefold():
                            try:
                                score = float(value)
                                if math.isfinite(score):
                                    best = max(best, score)
                            except (TypeError, ValueError):
                                pass
        finally:
            try:
                model.reset()
            except Exception:
                pass
        return max(0.0, min(1.0, best))


def main() -> int:
    detector = _JarvisModel()
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
        command = str(request.get("cmd", ""))
        request_id = str(request.get("id", ""))
        if command == "shutdown":
            return 0
        try:
            if command == "preload":
                detector.load()
                _respond({"id": request_id, "ok": True, "score": 0.0, "model": detector.model_path})
                continue
            if command != "score":
                raise ValueError(f"Unknown wake command: {command}")
            raw = base64.b64decode(str(request.get("audio_b64", "")), validate=False)
            audio = np.frombuffer(raw, dtype=np.float32).copy()
            score = detector.score(audio, int(request.get("sample_rate", 16000)))
            _respond({"id": request_id, "ok": True, "score": score, "model": detector.model_path})
        except KeyboardInterrupt:
            return 0
        except BaseException as exc:
            _respond({"id": request_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
