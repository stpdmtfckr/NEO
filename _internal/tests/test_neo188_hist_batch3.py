"""NEO 188: historical records HIST-013 … (ChatView lifetime, tidy journal, Discord cache/dashboard/short tracks, STT owner)."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def _qt_app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_hist013_deferred_width_pass_after_chat_view_deletion_is_dropped():
    app = _qt_app()
    import sys
    from PySide6.QtCore import QCoreApplication, QEvent
    from jarvis.ui.chat_view import ChatView
    errors = []
    old_hook = sys.excepthook
    sys.excepthook = lambda *exc: errors.append(exc)
    try:
        for _ in range(3):
            view = ChatView()
            view.add_user("привет")
            view.set_font_size(15)                 # queues more deferred width passes
            view.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        for _ in range(5):
            app.processEvents()
    finally:
        sys.excepthook = old_hook
    assert errors == []


def _folder(tmp_path):
    folder = tmp_path / 'Downloads'
    folder.mkdir()
    for name in ('a.jpg', 'b.pdf', 'c.zip'):
        (folder / name).write_bytes(name.encode())
    return folder


def test_hist014_failed_final_journal_write_still_allows_undo(tmp_path, monkeypatch):
    from jarvis.runtime import tidy172
    folder, data = _folder(tmp_path), tmp_path / 'data'
    plan = tidy172.plan(folder, 'en')
    real_replace = tidy172.os.replace
    calls = []

    def replace(src, dst):
        calls.append(1)
        if len(calls) > 1:                                   # the write AFTER the moves fails
            raise OSError(5, 'disk error')
        return real_replace(src, dst)
    monkeypatch.setattr(tidy172.os, 'replace', replace)
    moved, errors = tidy172.apply(plan, data, 'en')
    assert moved == 3 and any('journal' in e for e in errors)
    monkeypatch.setattr(tidy172.os, 'replace', real_replace)
    restored, errors, where = tidy172.undo(data, 'en')       # the temp/«moving» record is used, not lost
    assert restored == 3 and errors == [] and where == str(folder)
    assert sorted(p.name for p in folder.iterdir()) == ['a.jpg', 'b.pdf', 'c.zip']
    assert tidy172.undo(data) == (0, [], '') and not list((data / 'tidy').glob('*.tmp'))


def test_hist014_journal_is_written_before_any_file_moves(tmp_path, monkeypatch):
    from jarvis.runtime import tidy172
    folder, data = _folder(tmp_path), tmp_path / 'data'
    plan = tidy172.plan(folder, 'en')
    monkeypatch.setattr(tidy172, '_write_journal', lambda path, history: False)
    moved, errors = tidy172.apply(plan, data, 'de')
    assert moved == 0 and errors and 'Journal' in errors[0]
    assert sorted(p.name for p in folder.iterdir()) == ['a.jpg', 'b.pdf', 'c.zip']   # nothing moved without a record


def test_hist014_crash_between_moves_and_completion(tmp_path, monkeypatch):
    from jarvis.runtime import tidy172
    folder, data = _folder(tmp_path), tmp_path / 'data'
    plan = tidy172.plan(folder, 'en')
    real_move, done = tidy172.shutil.move, []

    def move(src, dst):
        if len(done) == 2:
            raise KeyboardInterrupt                          # the process dies after two moves
        done.append(src)
        return real_move(src, dst)
    monkeypatch.setattr(tidy172.shutil, 'move', move)
    with pytest.raises(KeyboardInterrupt):
        tidy172.apply(plan, data, 'en')
    monkeypatch.setattr(tidy172.shutil, 'move', real_move)
    restored, errors, _ = tidy172.undo(data, 'en')
    assert restored == 2 and errors == []                     # the unmoved third file is not reported missing
    assert sorted(p.name for p in folder.iterdir()) == ['a.jpg', 'b.pdf', 'c.zip']


def test_hist015_failed_restore_row_stays_for_a_bounded_retry(tmp_path, monkeypatch):
    from jarvis.runtime import tidy172
    folder, data = _folder(tmp_path), tmp_path / 'data'
    tidy172.apply(tidy172.plan(folder, 'en'), data, 'en')
    hidden = next(folder.rglob('b.pdf'))
    aside = tmp_path / 'aside.pdf'
    hidden.rename(aside)                                      # the target is temporarily unavailable
    restored, errors, _ = tidy172.undo(data, 'ru')
    assert restored == 2 and len(errors) == 1 and 'нет на месте' in errors[0]
    assert tidy172.has_pending_undo(data)
    aside.rename(hidden)                                      # available again
    restored, errors, _ = tidy172.undo(data, 'ru')
    assert restored == 1 and errors == [] and (folder / 'b.pdf').read_bytes() == b'b.pdf'
    assert not tidy172.has_pending_undo(data)
    tidy172.apply(tidy172.plan(folder, 'en'), data, 'en')
    gone = next(folder.rglob('c.zip'))
    gone.rename(tmp_path / 'away.zip')
    for _ in range(tidy172.UNDO_ATTEMPTS):
        tidy172.undo(data, 'en')
    assert not tidy172.has_pending_undo(data)                 # bounded: abandoned after the limit, still recorded
    entry = json.loads(tidy172.journal_path(data).read_text(encoding='utf-8'))[-1]
    assert entry['status'] == 'abandoned' and entry['moved'][0]['reason'] == 'missing'


def _discord_adapter(monkeypatch):
    from jarvis.integrations import discord_navigation
    from jarvis.integrations.discord_navigation import ReliableDiscordAdapter
    monkeypatch.setattr(discord_navigation, "publish_now_playing", lambda *a, **k: None)
    adapter = ReliableDiscordAdapter.__new__(ReliableDiscordAdapter)
    adapter._remote_status_cache = {}
    return adapter


@pytest.mark.parametrize("bad", ["invalid", -1, 1.5, True, "7x"])
def test_hist018_invalid_generation_never_enters_the_cold_cache(monkeypatch, bad):
    adapter = _discord_adapter(monkeypatch)
    profile = {"guild_id": "123", "name": "Guild"}
    assert adapter._cache_remote_snapshot(profile, {"playback_generation": bad, "current": {"track_id": "X"}}) == {}
    assert adapter._remote_status_cache == {}
    valid = adapter._cache_remote_snapshot(profile, {"playback_generation": "4", "playback_scope": "s",
                                                     "current": {"track_id": "A"}})
    assert valid["playback_generation"] == 4 and adapter._remote_status_cache["123"][1]["current"]["track_id"] == "A"


def test_hist018_invalid_record_from_an_older_build_is_replaced_not_compared(monkeypatch):
    adapter = _discord_adapter(monkeypatch)
    profile = {"guild_id": "123", "name": "Guild"}
    adapter._cache_remote_snapshot(profile, {"playback_generation": 3, "playback_scope": "s", "current": {"track_id": "A"}})
    adapter._remote_status_cache["123"][1]["playback_generation"] = "invalid"   # a poisoned record already cached
    for track, generation in (("B", 5), ("C", 6)):                               # before and after the TTL
        result = adapter._cache_remote_snapshot(profile, {"playback_generation": generation, "playback_scope": "s",
                                                           "current": {"track_id": track}})
        assert result["current"]["track_id"] == track
    stale = adapter._cache_remote_snapshot(profile, {"playback_generation": 2, "playback_scope": "s", "current": {"track_id": "old"}})
    assert stale["current"]["track_id"] == "C"                                  # real staleness is still refused


def _dashboard_bot(edit):
    import asyncio
    from jarvis.integrations.background.discord_bot import DiscordBackgroundBot
    bot = DiscordBackgroundBot.__new__(DiscordBackgroundBot)
    channel = SimpleNamespace(send=None)
    message = SimpleNamespace(id=55, edit=edit)
    guild = SimpleNamespace(id=7, voice_client=SimpleNamespace(channel=object()), me=None)

    async def resolve(profile):
        return guild

    async def ensure(g):
        return channel

    async def find(c):
        return message

    async def nothing(*a, **k):
        return None
    bot.admission = lambda: 1
    bot._resolve_guild, bot._ensure_music_channel, bot._find_music_dashboard = resolve, ensure, find
    bot._cleanup_music_channel_clutter = bot._append_played_track_history = nothing
    bot._music_profiles, bot._music_dashboard_locks, bot._music_last_state = {}, {}, {}
    bot._music_current_meta, bot._music_current_key, bot._music_track_history = {}, {}, {}
    bot._music_dashboard_messages, bot._music_dashboard_ids, bot._music_dashboard_signatures = {}, {}, {}
    bot._music_dashboard_text = lambda state, gid=0: "text " + str(state.get("n"))
    bot._music_view = lambda profile, state: None
    return bot, asyncio


def test_hist019_failed_edit_is_reported_as_failed_with_its_reason():
    async def broken(**kw):
        raise RuntimeError("HTTP 500")
    bot, asyncio = _dashboard_bot(broken)
    assert asyncio.run(bot._publish_music_state({}, {"n": 1, "current": {"title": "A"}})) == "failed"
    assert "HTTP 500" in bot.music_dashboard_outcome(7)["reason"]


def test_hist019_slow_edit_is_pending_then_completed():
    import asyncio as aio
    async def slow(**kw):
        await aio.sleep(1.6)
    bot, asyncio = _dashboard_bot(slow)

    async def scenario():
        first = await bot._publish_music_state({}, {"n": 1, "current": {"title": "A"}})
        await aio.sleep(0.6)
        return first, bot.music_dashboard_outcome(7)["status"]
    assert asyncio.run(scenario()) == ("pending", "completed")


def test_hist019_waited_publication_is_true_only_for_a_completion(monkeypatch):
    from jarvis.integrations.background.discord_bot import DiscordBackgroundBot
    import threading
    bot = DiscordBackgroundBot.__new__(DiscordBackgroundBot)
    bot._closing = threading.Event()
    bot._lock = threading.RLock()
    bot.ensure_ready = lambda: None
    import asyncio
    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    bot._loop = loop
    try:
        for outcome, expected in (("completed", True), ("unchanged", True), ("pending", False), ("failed", False),
                                  ("skipped", False)):
            async def publish(profile, state, reanchor=False, value=outcome):
                return value
            bot._publish_music_state = publish
            assert bot.publish_music_state({}, {}, wait=True) is expected
            assert bot.publish_music_state({}, {}, wait=False) is True       # non-blocking means accepted
    finally:
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), loop).result(2)   # let accepted updates finish
        loop.call_soon_threadsafe(loop.stop)
        worker.join(2)
        loop.close()


@pytest.mark.parametrize("elapsed, expected_ms, premature", [
    (6.0, 5000, False),          # the card: a known 5 s track ends naturally after 6 s
    (5.2, 5000, False),
    (1.0, 5000, True),           # a real early break of the same short track
    (10.0, 12000, False),        # past 80 % of a 12 s track
    (3.0, 12000, True),
    (12.0, 200000, True),        # long track: the 16 s transport-hiccup floor is kept
    (30.0, 200000, False),
    (5.0, 0, True),              # unknown duration: conservative 10 s floor
    (11.0, 0, False),
])
def test_hist020_premature_finish_follows_the_known_duration(elapsed, expected_ms, premature):
    from jarvis.integrations.discord_navigation import premature_finish188
    assert premature_finish188(elapsed, expected_ms, True) is premature
    assert premature_finish188(elapsed, expected_ms, False) is False


def _capture(monkeypatch):
    _qt_app()
    from jarvis.ui.audio_capture import AudioCaptureController
    cfg = SimpleNamespace(get=lambda k, d=None: d)
    capture = AudioCaptureController(cfg)
    monkeypatch.setattr(capture, "_cleanup_process_locked", lambda force: None)
    failures = []
    capture.failed.connect(failures.append)
    return capture, failures


def test_hist021_error_of_a_replaced_worker_does_not_end_the_new_capture(monkeypatch):
    capture, failures = _capture(monkeypatch)
    old_generation, old_session = capture._process_generation, capture._session
    capture.restart_backend()                                     # STT switch during capture
    capture._recording, capture._mode = True, "command"           # the new owner's capture
    capture._handle_event({"type": "error", "message": "old stream died", "session": old_session,
                           "_worker_generation188": old_generation})
    capture._handle_event({"type": "backend_error", "message": "old backend", "_worker_generation188": old_generation})
    assert failures == [] and capture._recording is True and capture._mode == "command"
    capture._handle_event({"type": "error", "message": "real", "session": capture._session,
                           "_worker_generation188": capture._process_generation})
    assert failures == ["real"] and capture._recording is False   # a matching error still ends its owner
    capture.close()


def test_hist021_stt_switch_ends_the_capture_owner_and_rearms():
    from jarvis.ui.main_window import MainWindow
    timer = SimpleNamespace(stop=lambda: None)
    resumed = []
    window = SimpleNamespace(_voice_mode="command", _wake_command_ui_pending=True, _interrupt_wake_active=False,
                             _command_asr_generation=4, _partial_generation=2, _pending_partial_recording=object(),
                             _wake_generation=1, _pending_wake_recording=None, _heard_timer=timer,
                             _restore_listening_audio=lambda: None, _clear_listening_bubble=lambda: None,
                             _live_user_bubble=None, mic_button=SimpleNamespace(setText=lambda t: None),
                             mic_level=SimpleNamespace(hide=lambda: None), _quitting=False,
                             _continuous_voice_session=False, _busy=False,
                             _resume_wake_listening=lambda delay_ms=0: resumed.append(delay_ms))
    assert MainWindow._end_capture_owner188(window) is True
    assert window._voice_mode == "" and window._command_asr_generation == 5 and window._partial_generation == 3
    assert window._pending_partial_recording is None and resumed == [250]
    assert MainWindow._end_capture_owner188(window) is False      # idempotent: nothing owned any more
