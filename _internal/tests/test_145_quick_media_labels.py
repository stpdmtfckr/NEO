"""Display provenance and truthful outcomes; fake transport, no playback/network."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from types import SimpleNamespace
import pytest
from jarvis.quick_media_commands import QuickMediaCommand
from jarvis.ui.main_window import MainWindow
from jarvis.ui.quick_media_messages import LANGUAGES, message


def window(execute, code="ru", provenance=None):
    tools = SimpleNamespace(execute=execute)
    if provenance:
        tools.yandex_adapter = SimpleNamespace(playback_provenance=lambda: provenance)
    return SimpleNamespace(
        agent=SimpleNamespace(tools=tools), _language_code=lambda: code,
        _quick_media_server=lambda: "", _cached_quick_media_owner=lambda: "yandex_music",
        config=SimpleNamespace(get=lambda key, default=None: default),
    )


@pytest.mark.parametrize("code", LANGUAGES)
def test_next_preserves_content_source_payload_and_transport(code):
    calls = []
    def execute(tool, args):
        calls.append((tool, args["action"]))
        return SimpleNamespace(success=True, text="", data={"title": "Мир Yes Save", "artist": "Artist", "track_id": "xyz", "content_provider": "youtube", "media_source": "YouTube"})
    result = MainWindow._execute_quick_media_command(window(execute, code), QuickMediaCommand("next", "yandex_music", "next"))
    assert result["ok"] and result["labels"] == ["YouTube"]
    assert "Мир Yes Save" in result["text"] and "Artist" in result["text"]
    assert result["now_playing"][0]["source"] == "yandex_music"
    assert result["now_playing"][0]["content_provider"] == "youtube"
    assert calls == [("yandex_music", "next")]  # No artificial pause before switching.


def test_empty_metadata_preserves_actual_provider_outcome():
    execute = lambda *args: SimpleNamespace(success=True, text="YouTube: preparing audio…", data={"content_provider": "youtube"})
    result = MainWindow._execute_quick_media_command(window(execute), QuickMediaCommand("next", "yandex_music", "дальше"))
    assert result["text"] == "YouTube: preparing audio…"
    assert result["now_playing"] == []


def test_empty_success_uses_passive_provenance_and_acceptance_only():
    execute = lambda *args: SimpleNamespace(success=True, text="", data={})
    result = MainWindow._execute_quick_media_command(window(execute, provenance={"media_source": "YouTube", "content_provider": "youtube"}), QuickMediaCommand("next", "yandex_music", "дальше"))
    assert result["text"] == message("accepted", "ru", source="YouTube")
    assert result["now_playing"] == []


def test_current_track_provenance_is_not_overwritten_by_cached_transport():
    execute = lambda *args: SimpleNamespace(success=True, text="", data={"current": {"title": "Current", "media_source": "YouTube"}})
    fake = window(execute, provenance={"media_source": "Yandex Music", "content_provider": "yandex_music"})
    result = MainWindow._execute_quick_media_command(fake, QuickMediaCommand("next", "yandex_music", "next"))
    assert result["labels"] == ["YouTube"]
    assert result["now_playing"][0]["content_provider"] == "youtube"


def test_rejected_command_preserves_provider_error():
    execute = lambda *args: SimpleNamespace(success=False, text="Provider rejected request: 403", data={})
    result = MainWindow._execute_quick_media_command(window(execute), QuickMediaCommand("next", "yandex_music", "дальше"))
    assert not result["ok"]
    assert result["text"] == "Provider rejected request: 403"


def test_failed_pause_does_not_freeze_playing_overlay():
    state = {"source": "yandex_music", "playing": True, "paused": False, "position_seconds": 10.0}
    fake = SimpleNamespace(_quick_media_trace=None, _music_overlay_last_event=state.copy(),
        _listening_bubble=None, _last_now_playing_by_source={}, _busy=True,
        _refresh_robot_stage_info=lambda: None)
    MainWindow._quick_media_finished(fake, {"ok": False, "action": "pause", "target": "yandex_music"})
    assert fake._music_overlay_last_event == state


def test_quick_ack_deduplicates_by_actual_content_source():
    fake = SimpleNamespace(_quick_media_trace=None, _music_overlay_last_event={},
        _listening_bubble=None, _last_now_playing_by_source={}, _busy=True,
        _refresh_robot_stage_info=lambda: None)
    MainWindow._quick_media_finished(fake, {"ok": True, "action": "next", "now_playing": [{"source": "yandex_music", "content_provider": "youtube", "label": "YouTube", "title": "Track", "track_id": "xyz"}]})
    assert fake._last_now_playing_by_source == {"youtube": "youtube:xyz"}
    assert fake._music_overlay_last_event["source"] == "yandex_music"
    assert fake._music_overlay_last_event["source_label"] == "YouTube"


def test_pending_resume_does_not_invent_playing_state():
    state = {"source": "yandex_music", "playing": False, "paused": True, "position_seconds": 10.0}
    fake = SimpleNamespace(_quick_media_trace=None, _music_overlay_last_event=state.copy(),
        _listening_bubble=None, _last_now_playing_by_source={}, _busy=True,
        _refresh_robot_stage_info=lambda: None)
    MainWindow._quick_media_finished(fake, {"ok": True, "starting": True, "action": "resume", "target": "yandex_music"})
    assert fake._music_overlay_last_event == state
