"""Prepare only speech models selected by the current profile, once at setup."""
from __future__ import annotations
import gc
from pathlib import Path
import os
from bootstrap import ReadOnlyConfig


def required_models(config) -> list[str]:
    values = [config.get("voice.stt_model", "small"), config.get("wake_word.stt_model", "tiny")]
    if int(config.get("voice.live_partial_max_updates", 0) or 0) > 0:
        values.append(config.get("voice.partial_stt_model", "tiny"))
    if bool(config.get("ambient.enabled", False)) and bool(config.get("ambient.system_audio_transcription_enabled", False)):
        values.append(config.get("ambient.stt_model", "tiny"))
    return list(dict.fromkeys(str(x).strip() for x in values if str(x or "").strip()))


def main() -> int:
    from faster_whisper import WhisperModel
    config = ReadOnlyConfig()
    for name in required_models(config):
        print(f"Preparing configured faster-whisper model: {name}")
        model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=min(2, os.cpu_count() or 1), num_workers=1)
        del model
        gc.collect()
    if bool(config.get("wake_word.acoustic_enabled", False)):
        try:
            import openwakeword
            from openwakeword import utils
            models_dir = Path(openwakeword.__file__).resolve().parent / "resources" / "models"
            if not list(models_dir.glob("*jarvis*.onnx")):
                utils.download_models(models=["hey_jarvis"])
        except Exception as exc:
            print(f"Optional acoustic model unavailable; configured Whisper wake remains usable: {exc}")
    print("Configured speech models are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
