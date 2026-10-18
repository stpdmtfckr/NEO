"""Exercise the real Qt-handler body without touching a desktop/audio device."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import time

import pytest

from jarvis.runtime.media_notice156 import now_playing_notice, queue_notice, queue_added_notice

LANGUAGES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


@pytest.mark.parametrize('language', LANGUAGES)
def test_native_notices_preserve_track_metadata(language):
    track = '«L’été / No Surprises — 音楽»'
    queue = queue_notice(['1. Artist — Track'], language)
    text = now_playing_notice('Yandex Music', track, queue, language)
    assert track in text and '1. Artist — Track' in text
    assert 'Alex' in queue_added_notice('Alex', track, 2, language)
    assert queue_notice([], language).strip()
    assert queue_notice(None, language) != queue_notice([], language)
    if language != 'ru':
        assert 'Сейчас играет' not in text and 'Очередь' not in queue


def handler():
    source = Path(__file__).resolve().parents[1]/'jarvis/ui/main_window.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_drain_media_now_playing_events')
    namespace = {'time': time, 'localized_source_name': lambda source, language: 'Yandex Music', 'logger': Mock()}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


@pytest.mark.parametrize('language', LANGUAGES)
@pytest.mark.parametrize('announce', (True, False))
def test_one_command_result_owns_chat_but_overlay_still_updates(language, announce):
    event = SimpleNamespace(source='yandex_music', title='Song', artist='Artist', key='track1', event_id='physical-event-1', announce=announce)
    namespace = handler()
    namespace['drain_now_playing'] = lambda limit: [event]
    window = SimpleNamespace(
        config=SimpleNamespace(get=lambda key, default: default),
        _language_code=lambda: language, _last_now_playing_by_source={},
        _media_notice_suppressed_until={}, _now_playing_queue_preview=lambda source: '',
        _append_assistant=Mock(), _refresh_robot_stage_info=Mock(), _remember_media_bubble157=Mock(),
    )
    namespace['_drain_media_now_playing_events'](window)
    assert window._music_overlay_last_event['title'] == 'Song'
    assert window._last_now_playing_by_source['yandex_music'] == 'track1'
    assert window._append_assistant.call_count == int(announce)
    # Replay of the same physical event must not produce an additional bubble.
    namespace['_drain_media_now_playing_events'](window)
    assert window._append_assistant.call_count == int(announce)
