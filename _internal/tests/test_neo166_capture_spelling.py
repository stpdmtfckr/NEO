"""NEO 166: NVIDIA clips/recording, quick capture words, misheard-word correction."""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.integrations import nvidia_capture as nv
from jarvis.runtime import spoken_spelling165 as spelling
from jarvis.runtime.natural_commands import LANGUAGES
from jarvis.runtime.quick_capture165 import parse_quick_capture
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked

ROOT = Path(__file__).resolve().parents[1]


def config(language='ru', **values):
    data = {'app.language': language, **values}
    return SimpleNamespace(get=lambda key, default=None: data.get(key, default))


def controller(language='ru', *, overlay=True, found=None, **values):
    presses = []
    ctl = nv.NvidiaCapture(config(language, **values), press=presses.append, overlay=lambda: overlay,
                           is_windows=True, wait=lambda roots, since, timeout: found)
    ctl._folders = lambda: [Path('C:/Users/me/Videos')]
    return ctl, presses


def test_clip_presses_instant_replay_and_reports_the_file():
    ctl, presses = controller(found=Path('C:/Users/me/Videos/Game/Game 2026.mp4'))
    ok, text, data = ctl.run('clip')
    assert ok and presses == ['alt+f10']
    assert 'Game 2026.mp4' in text and text.startswith('Клип сохранён')
    assert data['path'].endswith('Game 2026.mp4')


def test_clip_without_a_new_file_names_the_folder():
    ctl, _ = controller('en')
    ok, text, data = ctl.run('clip')
    assert not ok and text.startswith('Sent the NVIDIA save-clip command') and 'Videos' in text
    assert data['verified'] is False


def test_recording_start_stop_needs_live_state_and_never_toggles_blindly():
    ctl, presses = controller(found=Path('C:/Users/me/Videos/rec.mp4'))
    for action in ('record_stop', 'record_start'):
        ok, text, _ = ctl.run(action)
        assert not ok and presses == [] and 'ALT+F9' in text
    observed = [False]
    ctl._recording_reader = lambda: observed[0]
    ctl._sleep = lambda _s: None
    def press(key):
        presses.append(key)
        observed[0] = not observed[0]
    ctl._press = press
    assert ctl.run('record_start')[0] and presses == ['alt+f9']
    assert ctl.run('record_start')[1].startswith('Запись уже идёт') and presses == ['alt+f9']
    ok, text, data = ctl.run('record_stop')
    assert ok and presses == ['alt+f9', 'alt+f9'] and 'rec.mp4' in text and not ctl.state.recording


def test_capture_refuses_without_overlay_windows_or_valid_hotkey():
    ctl, presses = controller(overlay=False)
    assert not ctl.run('clip')[0] and presses == []
    ctl, presses = controller(**{'capture.nvidia_clip_hotkey': 'alt+banana'})
    ok, text, _ = ctl.run('clip')
    assert not ok and 'alt+banana' in text and presses == []
    other = nv.NvidiaCapture(config('de'), press=lambda _k: None, is_windows=False)
    assert other.run('clip')[1].startswith('NVIDIA-Clips')


@pytest.mark.parametrize('code', LANGUAGES)
def test_every_capture_message_exists_in_every_language(code):
    for key in nv.MESSAGES:
        assert nv.message(key, code, path='p', folder='f', hotkey='h')


def test_hotkeys_and_new_video_detection(tmp_path):
    assert nv.parse_hotkey('Alt+F10') == (0x12, 0x79)
    with pytest.raises(ValueError):
        nv.parse_hotkey('alt+nothing')
    since = time.time()
    folder = tmp_path / 'Videos' / 'Game'
    folder.mkdir(parents=True)
    old = folder / 'old.mp4'
    old.write_bytes(b'x')
    os.utime(old, (since - 100, since - 100))
    (folder / 'clip.mp4').write_bytes(b'video')
    assert nv.wait_for_new_video([tmp_path / 'Videos'], since, 2, sleep=lambda _s: None).name == 'clip.mp4'
    assert nv.wait_for_new_video([tmp_path / 'nothing'], since, 0, sleep=lambda _s: None) is None


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        value = AtlasSession(tmp_path_factory.mktemp('capture166') / 'run')
        yield value
        value.finish()


def route(session, text, code='ru'):
    session.reset_case(code)
    turn = session.agent.handle(text)
    call = session.calls[0] if session.calls else None
    return turn, (call['tool'] + '.' + call['arguments'].get('action', '')) if call else ''


def _phrases():
    canonical = json.loads((ROOT / 'config/canonical_phrases/capture.json').read_text(encoding='utf-8'))
    variants = json.loads((ROOT / 'config/command_variants/capture.json').read_text(encoding='utf-8'))
    rows = [(LANGUAGES[i], phrases[i], intent) for intent, phrases in canonical.items() for i in range(7)]
    rows += [(code, phrase, intent) for intent, langs in variants.items() for code, phrases in langs.items() for phrase in phrases]
    return rows


def test_many_phrases_per_language():
    rows = _phrases()
    for code in LANGUAGES:
        for intent in ('capture.clip', 'capture.record_start', 'capture.record_stop'):
            assert sum(1 for c, _, i in rows if c == code and i == intent) >= 8, (code, intent)


@pytest.mark.parametrize('code,phrase,intent', _phrases())
def test_every_capture_phrase_routes_to_its_function(session, code, phrase, intent):
    _, tool = route(session, phrase, code)
    assert tool == intent, (code, phrase, session.calls)


@pytest.mark.parametrize('text,intent', [
    ('Джарвис, сделай клип', 'capture.clip'),
    ('Джарвис начни запись', 'capture.record_start'),
    ('сделай клипп', 'capture.clip'),
    ('начни запиьс', 'capture.record_start'),
    ('останови запсиь', 'capture.record_stop'),
    ('выключи запись экрана', 'capture.record_stop'),   # was routed to system power before
    ('скриншот активного окна', 'screen.capture_window'),
])
def test_capture_with_address_and_typos(session, text, intent):
    _, tool = route(session, text)
    assert tool == intent, (text, session.calls)


@pytest.mark.parametrize('text,code,action', [
    ('Клип.', 'ru', 'clip'), ('Джарвис, клип!', 'ru', 'clip'), ('клипп', 'ru', 'clip'),
    ('Запись', 'ru', 'record_start'), ('стоп запись', 'ru', 'record_stop'), ('скриншот', 'ru', 'screenshot'),
    ('скриншод', 'ru', 'screenshot'), ('Clip that.', 'en', 'clip'), ('stop recording', 'en', 'record_stop'),
    ('Aufnahme stoppen', 'de', 'record_stop'), ("capture d'écran", 'fr', 'screenshot'),
    ('grabación', 'es', 'record_start'), ('registra', 'it', 'record_start'), ('para a gravação', 'pt', 'record_stop'),
])
def test_quick_capture_words(text, code, action):
    quick = parse_quick_capture(text, code)
    assert quick is not None and quick.action == action
    assert quick.command  # canonical command of the selected language


@pytest.mark.parametrize('text', ['стоп', 'пауза', 'музыка', 'я смотрел клип вчера', 'запиши это в заметку'])
def test_quick_capture_ignores_other_speech(text):
    assert parse_quick_capture(text, 'ru') is None


@pytest.mark.parametrize('code', LANGUAGES)
def test_quick_capture_commands_are_real_commands(session, code):
    from jarvis.runtime.quick_capture165 import canonical_command
    for action, intent in (('clip', 'capture.clip'), ('record_start', 'capture.record_start'),
                           ('record_stop', 'capture.record_stop'), ('screenshot', 'screen.capture_window')):
        _, tool = route(session, canonical_command(action, code), code)
        assert tool == intent, (code, action, session.calls)


@pytest.mark.parametrize('heard,written,code', [
    ('превет, как дела?', 'привет, как дела?', 'ru'),
    ('пачему не работает', 'почему не работает', 'ru'),
    ('bonjoir comment ça va', 'bonjour comment ça va', 'fr'),
    ('see you tomorow', 'see you tomorrow', 'en'),
])
def test_misheard_words_are_written_correctly(heard, written, code):
    assert spelling.correct_transcript(heard, code, wait=True)[0] == written


@pytest.mark.parametrize('text,code', [
    ('включи трек Скриптонит', 'ru'), ('открой Discord', 'ru'), ('поставь песню кино группа крови', 'ru'),
    ('я включил музыку вчера', 'ru'), ('перезагрузившемуся компьютеру', 'ru'), ('I am going to Brakk', 'en'),
    ('öffne den Ordner Downloads', 'de'), ('удлаи файл', 'ru'),
])
def test_correct_speech_and_names_stay_unchanged(text, code):
    corrected, changes = spelling.correct_transcript(text, code, wait=True)
    assert not any(new in {'удали'} for _, new in changes)
    if code != 'ru' or 'удлаи' not in text:
        assert corrected == text and not changes, (text, changes)


def test_spelling_never_blocks_the_caller_before_warm_up(monkeypatch):
    monkeypatch.setattr(spelling, '_CACHE', {})
    monkeypatch.setattr(spelling, '_LOADING', {'it'})  # a warm-up is already running
    assert spelling.correct_transcript('ciaoo', 'it') == ('ciaoo', ())


@pytest.mark.parametrize('code', LANGUAGES)
def test_spelling_data_for_every_language_is_compact(code):
    raw = json.loads(spelling.data_path(code).read_text(encoding='utf-8'))
    assert raw['language'] == code and len(raw['targets'].split('\n')) > 10000
    assert spelling.data_path(code).stat().st_size < 3_000_000


def test_capture_results_are_already_localized():
    from jarvis.runtime.localized_text import render_action_result
    text = nv.message('clip_saved', 'en', path='C:/v.mp4')
    assert render_action_result('capture', {'action': 'clip'}, True, text, {'path': 'C:/v.mp4'}, 'en') == text
