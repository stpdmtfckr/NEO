"""Entry points for the compiled NEO host. Never execute arbitrary Python CLI."""
from __future__ import annotations
import importlib
import os
from pathlib import Path
import sys

WORKERS = frozenset({
    'jarvis.audio_worker', 'jarvis.stt_worker', 'jarvis.wake_worker',
    'jarvis.ambient_loopback_worker', 'jarvis.integrations.background.local_player_worker',
    'jarvis.runtime.discord_audio_meter_worker',
})
HELPERS = frozenset({
    'neo_prepare_dependencies164', 'dependency_guard', 'prepare_speech_models',
    'prepare_youtube_runtime',
})


def dispatch(argv):
    values = list(argv)
    if '--neo-runtime' in values:
        i=values.index('--neo-runtime')
        if i+1 >= len(values):return 64
        del values[i:i+2]
    if values and values[0] == '-u':values.pop(0)
    if values == ['--prepare-data']:
        print(os.environ['NEO_PACKAGE_ROOT'], flush=True)
        return 0
    if values == ['--check-native']:
        import json
        for name in ('jarvis.config', 'jarvis.runtime.file_search', 'PySide6.QtCore', 'psutil', 'sounddevice', 'faster_whisper'):
            importlib.import_module(name)
        print(json.dumps({'status':'ready','version':'4.0.20.164','host':Path(sys.executable).name}),flush=True)
        return 0
    if values == ['--check-ffmpeg']:
        import imageio_ffmpeg, subprocess
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-version'], check=True, timeout=20,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0), stdout=subprocess.DEVNULL)
        return 0
    if values and values[0] in {'-m','--neo-run'}:
        if len(values)<2:return 64
        command,module = values[:2]
        if module not in (WORKERS if command=='-m' else HELPERS):return 64
        sys.argv=[module,*values[2:]]
        return importlib.import_module(module).main() or 0
    if values and values not in (['--show'], ['--autostart'], ['--remote-start']):
        return 64
    sys.argv=[sys.executable]
    import app
    return app.main()


def main():
    try:
        return dispatch(sys.argv[1:])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code,int) else (0 if exc.code is None else 1)
