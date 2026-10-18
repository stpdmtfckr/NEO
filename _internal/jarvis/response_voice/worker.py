"""Private CPU helper. Nothing imports torch in the desktop GUI process."""
from __future__ import annotations
import contextlib
import json
import os
import sys

LANGUAGES = {'ru':'Russian','en':'English','de':'German','fr':'French','es':'Spanish','it':'Italian','pt':'Portuguese'}
VOICES = ('Aiden', 'Serena')


def load_model(path, threads):
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                      OMP_NUM_THREADS=str(threads), MKL_NUM_THREADS=str(threads), OPENBLAS_NUM_THREADS=str(threads))
    try:
        if os.name == 'nt':
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
        else:
            os.nice(10)
    except OSError:
        pass
    import torch
    from qwen_tts import Qwen3TTSModel
    torch.set_num_threads(threads)
    torch.set_num_interop_threads(1)
    return Qwen3TTSModel.from_pretrained(str(path), device_map='cpu', dtype=torch.float32,
                                       attn_implementation='eager', local_files_only=True, trust_remote_code=False)


def generate(model, request):
    voice = request['speaker']
    code = request['language']
    if voice not in VOICES or code not in LANGUAGES:
        raise ValueError('unsupported_voice_or_language')
    # 0.6B CustomVoice has no instruction/style support. Never pass instruct or clone inputs.
    return model.generate_custom_voice(text=str(request['text'])[:2000], language=LANGUAGES[code],
                                       speaker=voice, max_new_tokens=1024, do_sample=False)


def serve(model_path, threads):
    protocol = sys.stdout
    def emit(state):
        protocol.write(json.dumps({'state': state}) + '\n'); protocol.flush()
    model = None
    # Redirect package prints away from the JSON-lines protocol.
    with contextlib.redirect_stdout(sys.stderr):
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if model is None:
                    emit('loading')
                    model = load_model(model_path, threads)
                emit('synthesizing')
                wavs, sample_rate = generate(model, request)
                import numpy as np
                import sounddevice as audio
                samples = np.asarray(wavs[0], dtype=np.float32)
                if samples.ndim != 1 or not np.isfinite(samples).all() or not 8000 <= int(sample_rate) <= 96000:
                    raise ValueError('invalid_audio')
                from jarvis.response_voice.robot import robotize
                samples = robotize(samples, int(sample_rate), float(request.get('robot', 0)) / 100.0)
                samples = np.clip(samples, -1.0, 1.0) * max(0, min(100, int(request['volume']))) / 100.0
                emit('playing')
                audio.play(samples, int(sample_rate), blocking=True)
                emit('done')
            except Exception:
                emit('synthesis_failed')
                return 1
    return 0


def main():
    from pathlib import Path
    path = Path(sys.argv[1])
    if not path.is_absolute() or not path.is_dir():
        return 2
    return serve(path, max(1, min(4, int(sys.argv[2]))))


if __name__ == '__main__':
    raise SystemExit(main())
