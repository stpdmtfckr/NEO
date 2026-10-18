"""NEO 188 NEO-ASR-002: recognition events → at most one logical command per capture."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from jarvis.runtime.recognition_events188 import DispatchGuard, UtteranceAssembler, live_covers, MAX_SEGMENTS
from jarvis.voice import RecordingResult

S, C = 'desktop', 'cap-1'


def make(generation=0):
    return UtteranceAssembler(S, C, generation)


def test_partial_revision_does_not_dispatch_and_end_hands_over_once():
    u = make()
    assert u.partial('поставь таймер на двадцать', session=S, capture=C) == 'поставь таймер на двадцать'
    assert u.partial('поставь таймер на двенадцать минут', session=S, capture=C) == 'поставь таймер на двенадцать минут'
    assert u.text() == ''                                    # nothing final yet → nothing to run
    u.final('поставь таймер на двенадцать минут', session=S, capture=C)
    out = u.end(session=S, capture=C)
    assert out.status == 'dispatch' and out.text == 'поставь таймер на двенадцать минут' and 'двадцать' not in out.text
    assert u.end(session=S, capture=C).status == 'duplicate'


def test_shorter_partial_really_removes_the_tail():
    u = make()
    u.partial('открой папку загрузки и удали', session=S, capture=C)
    assert u.partial('открой папку загрузки', session=S, capture=C) == 'открой папку загрузки'
    u.final('открой папку загрузки', session=S, capture=C)
    assert u.end(session=S, capture=C).text == 'открой папку загрузки'


def test_two_final_segments_with_changing_tail_neither_lost_nor_duplicated():
    u = make()
    u.final('поставь таймер', session=S, capture=C, index=0)
    assert u.text() == 'поставь таймер'                       # first final is not the whole command
    u.partial('на два', session=S, capture=C, index=1)
    u.partial('на двенадцать', session=S, capture=C, index=1)
    assert u.preview() == 'поставь таймер на двенадцать'
    u.final('на двенадцать минут', session=S, capture=C, index=1)
    u.final('на двенадцать минут', session=S, capture=C, index=1)  # repeated delivery of the same result
    assert u.end(session=S, capture=C).text == 'поставь таймер на двенадцать минут'


def test_result_index_shrinking_list_drops_later_results():
    u = make()
    u.final('один', session=S, capture=C, index=0)
    u.final('два', session=S, capture=C, index=1)
    u.partial('три', session=S, capture=C, index=1)           # engine revised from result 1 on
    assert u.preview() == 'один три'


def test_sequential_segments_and_cumulative_word_arrays_give_same_text():
    a, b = make(), make()
    a.final('поставь таймер', session=S, capture=C)
    a.final('на пять минут', session=S, capture=C)
    b.words(['поставь'], session=S, capture=C, index=0)
    b.words(['поставь', 'таймер'], session=S, capture=C, index=0)
    b.words(['поставь', 'таймер'], session=S, capture=C, index=0, final=True)
    b.words(['на', 'пять'], session=S, capture=C, index=1)
    b.words(['на', 'пять', 'минут'], session=S, capture=C, index=1, final=True)
    assert a.end(session=S, capture=C).text == b.end(session=S, capture=C).text == 'поставь таймер на пять минут'


def test_duplicate_delivery_is_one_command_new_capture_with_same_words_is_accepted():
    g = DispatchGuard()
    assert g.claim('desktop', 'cap-1', 'поставь таймер на 5 минут', now=1.0)
    assert not g.claim('desktop', 'cap-1', 'поставь  таймер на 5 минут', now=2.0)   # re-delivered
    assert g.claim('desktop', 'cap-2', 'поставь таймер на 5 минут', now=3.0)       # new own capture
    assert g.claim('telegram:1', 'cap-1', 'поставь таймер на 5 минут', now=3.0)    # other session
    assert g.claim('desktop', '', 'x') and g.claim('desktop', '', 'x')             # unknown ownership: no fake duplicate


def test_guard_is_bounded_and_expires():
    g = DispatchGuard(size=4, ttl=10.0)
    for i in range(50):
        g.claim('desktop', f'c{i}', 'текст', now=float(i) / 100)
    assert len(g) == 4
    assert g.claim('desktop', 'c49', 'текст', now=100.0)        # expired entry is accepted again


def test_cancel_discards_and_late_events_of_old_generation_are_refused():
    u = make(generation=3)
    u.final('удали файл', session=S, capture=C, generation=3)
    assert u.cancel().status == 'cancelled'
    assert not u.final('удали файл', session=S, capture=C, generation=3)
    assert u.end(session=S, capture=C, generation=3).status == 'cancelled'
    fresh = make(generation=4)
    assert not fresh.final('старый результат', session=S, capture=C, generation=3)
    assert fresh.partial('старый', session=S, capture=C, generation=3) is None
    assert fresh.text() == ''


def test_sessions_with_same_text_and_indices_do_not_mix():
    desk, remote = UtteranceAssembler('desktop', 'c', 0), UtteranceAssembler('telegram:9', 'c', 0)
    assert not desk.final('открой браузер', session='telegram:9', capture='c', index=0)
    remote.final('открой браузер', session='telegram:9', capture='c', index=0)
    assert desk.end(session='desktop', capture='c').status == 'no_result'
    assert desk.end(session='telegram:9', capture='c').status == 'refused'


def test_stream_broken_after_final_is_not_a_complete_command():
    u = make()
    u.final('поставь будильник', session=S, capture=C)
    out = u.disconnect()
    assert out.status == 'incomplete' and out.status != 'dispatch'
    assert make().disconnect().status == 'no_result'
    assert make().end(session=S, capture=C).status == 'no_result'


def test_stability_and_speaker_never_authorize_and_confidence_is_not_invented():
    u = make()
    u.partial('выключи компьютер', session=S, capture=C, stability=0.99, speaker=1)
    assert u.text() == ''
    assert u.end(session=S, capture=C).status == 'no_result'
    v = make()
    v.final('привет', session=S, capture=C)
    assert v.end(session=S, capture=C).confidence is None
    w = make()
    w.final('привет', session=S, capture=C, confidence=float('nan'))
    w.final('мир', session=S, capture=C, confidence=0.4)
    assert w.end(session=S, capture=C).confidence == 0.4


def test_rapid_revisions_keep_bounded_latest_state():
    u = make()
    for i in range(5000):
        u.partial(f'вариант {i}', session=S, capture=C, index=0)
    assert u.preview() == 'вариант 4999' and u.revisions == 5000
    for i in range(MAX_SEGMENTS + 20):
        u.final(f's{i}', session=S, capture=C, index=i)
    assert len(u.text().split()) == MAX_SEGMENTS


def test_live_preview_only_replaces_final_decode_when_it_covered_the_speech():
    rate = 16000
    assert live_covers(rate * 3, rate * 4, rate, 1.0)              # only endpoint silence left
    assert not live_covers(rate * 2, rate * 4, rate, 1.0)          # missed the last spoken words
    assert not live_covers(None, rate * 4, rate, 1.0)


def _method(name, **scope):
    path = Path(__file__).resolve().parents[1] / 'jarvis/ui/main_window.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    scope.setdefault('Any', object)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
    return scope[name]


def _window():
    return SimpleNamespace(agent=None, _language_code=lambda: 'ru', _probable_system_playback_echo=lambda *a, **k: False,
                           _voice_recognized=Mock(), _continuous_voice_session=False, _last_voice_correlation='')


def _recording(cid, wake=False):
    return RecordingResult(np.zeros(1600, dtype=np.float32), 16000, cid, 0.0, 0.0, wake_candidate=wake)


def test_main_window_dispatches_a_redelivered_final_result_once():
    from jarvis.quick_media_commands import QuickMediaCommand
    import logging, time
    fn = _method('_command_transcription_ready', RecordingResult=RecordingResult, QuickMediaCommand=QuickMediaCommand,
                 time=time, latency_profiler=lambda: Mock(), logger=logging.getLogger('t'))
    w = _window()
    rec = _recording('cap-a')
    payload = {'kind': 'text', 'text': 'поставь таймер на 12 минут', 'alternatives': [], 'recording': rec}
    fn(w, payload)
    fn(w, dict(payload))
    assert w._voice_recognized.call_count == 1
    fn(w, {'kind': 'text', 'text': 'поставь таймер на 12 минут', 'alternatives': [], 'recording': _recording('cap-b')})
    assert w._voice_recognized.call_count == 2                     # new independent capture, same words


def test_wake_candidate_text_does_not_block_the_promoted_command():
    from jarvis.quick_media_commands import QuickMediaCommand
    import logging, time
    fn = _method('_command_transcription_ready', RecordingResult=RecordingResult, QuickMediaCommand=QuickMediaCommand,
                 time=time, latency_profiler=lambda: Mock(), logger=logging.getLogger('t'))
    w = _window()
    w._start_command_after_acoustic_wake = Mock()
    fn(w, {'kind': 'text', 'text': 'NEO', 'alternatives': [], 'recording': _recording('cap-w', wake=True)})
    fn(w, {'kind': 'text', 'text': 'NEO', 'alternatives': [], 'recording': _recording('cap-w')})
    assert w._voice_recognized.call_count == 2


def test_heard_preview_replacement_drops_vanished_tail():
    fn = _method('_set_heard_target')
    w = SimpleNamespace(_heard_display_text='поставь таймер на двадцать', _heard_target_text='поставь таймер на двадцать',
                        _heard_timer=SimpleNamespace(isActive=lambda: True, start=Mock()))
    fn(w, 'поставь таймер на две')
    assert w._heard_target_text == 'поставь таймер на две'
    assert w._heard_display_text == 'поставь таймер на дв'          # common prefix; the old tail is gone
