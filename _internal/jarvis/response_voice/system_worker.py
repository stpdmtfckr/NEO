"""NEO 185: light-tier helper process. Windows SAPI voices + the robot colour.

Same JSON-lines protocol as worker.py. Runs in NEO's own Python (no torch, no download).
"""
from __future__ import annotations
import contextlib
import json
import sys

# Primary language ids (LCID & 0x3FF) of NEO's seven languages.
LANGS = {'ru': 0x19, 'en': 0x09, 'de': 0x07, 'fr': 0x0C, 'es': 0x0A, 'it': 0x10, 'pt': 0x16}
SAFT22kHz16BitMono = 22


def pick_voice(voices, language, speaker):
    """Return the first voice for the language, preferring the requested gender."""
    want = 'female' if speaker == 'Serena' else 'male'
    matches = []
    for voice in voices:
        lcids = []
        for part in str(voice.get('language', '')).split(';'):
            try:
                lcids.append(int(part.strip(), 16))
            except ValueError:
                continue
        if any((lcid & 0x3FF) == LANGS.get(language, -1) for lcid in lcids):
            matches.append(voice)
    matches.sort(key=lambda v: 0 if str(v.get('gender', '')).casefold() == want else 1)
    return matches[0] if matches else None


def synthesize(text, language, speaker):
    import comtypes.client
    import numpy as np
    engine = comtypes.client.CreateObject('SAPI.SpVoice')
    tokens = engine.GetVoices()
    voices = []
    for index in range(tokens.Count):
        token = tokens.Item(index)
        voices.append({'token': token, 'language': token.GetAttribute('Language') or '',
                       'gender': token.GetAttribute('Gender') or ''})
    chosen = pick_voice(voices, language, speaker)
    if chosen is None:
        raise LookupError('no_system_voice')
    engine.Voice = chosen['token']
    stream = comtypes.client.CreateObject('SAPI.SpMemoryStream')
    audio_format = comtypes.client.CreateObject('SAPI.SpAudioFormat')
    audio_format.Type = SAFT22kHz16BitMono
    stream.Format = audio_format
    engine.AudioOutputStream = stream
    engine.Speak(str(text)[:2000], 0)
    raw = bytes(stream.GetData())
    return np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0, 22050


def serve():
    protocol = sys.stdout

    def emit(state):
        protocol.write(json.dumps({'state': state}) + '\n'); protocol.flush()
    with contextlib.redirect_stdout(sys.stderr):
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if request.get('language') not in LANGS:
                    raise ValueError('unsupported_language')
                emit('synthesizing')
                samples, rate = synthesize(request['text'], request['language'], request.get('speaker', 'Aiden'))
                import numpy as np
                import sounddevice as audio
                from jarvis.response_voice.robot import robotize
                samples = robotize(samples, rate, float(request.get('robot', 0)) / 100.0)
                samples = np.clip(samples, -1.0, 1.0) * max(0, min(100, int(request['volume']))) / 100.0
                emit('playing')
                audio.play(samples, rate, blocking=True)
                emit('done')
            except Exception:
                emit('synthesis_failed')
                return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(serve())
