"""NEO 187 HREQ-026: next/previous VIDEO (not a seek, not music)."""
import asyncio

_REAL_SLEEP = asyncio.sleep
from types import SimpleNamespace

import pytest

from jarvis.runtime.video_nav187 import parse, text


@pytest.mark.parametrize("code,phrase,action", [
    ("ru", "следующее видео", "next"), ("ru", "включи следующее видео", "next"), ("ru", "переключи на следующее видео", "next"),
    ("ru", "предыдущее видео", "previous"), ("ru", "прошлое видео", "previous"), ("ru", "вернись к предыдущему видео", "previous"),
    ("ru", "Нео, следующий ролик, пожалуйста", "next"), ("ru", "пропусти видео", "next"),
    ("en", "next video", "next"), ("en", "go back to the previous video", "previous"), ("en", "skip this video", "next"),
    ("de", "nächstes Video", "next"), ("de", "zurück zum vorherigen Video", "previous"),
    ("fr", "vidéo suivante", "next"), ("fr", "la vidéo précédente", "previous"),
    ("es", "siguiente video", "next"), ("es", "vuelve al video anterior", "previous"),
    ("it", "video successivo", "next"), ("it", "torna al video precedente", "previous"),
    ("pt", "próximo vídeo", "next"), ("pt", "vídeo anterior", "previous"),
])
def test_navigation_phrases(code, phrase, action):
    assert parse(phrase, code) == action


@pytest.mark.parametrize("code,phrase", [
    ("ru", "следующее видео про котов"), ("ru", "следующий трек"), ("ru", "перемотай видео вперёд"),
    ("ru", "не включай следующее видео"), ("ru", "«следующее видео»"), ("en", "next song"), ("en", "what is the next video"),
])
def test_other_requests_keep_their_routes(code, phrase):
    assert parse(phrase, code) is None


class FakeSession:
    def __init__(self, titles, *, next_enabled=True, previous_enabled=True, accepts=True):
        self.titles, self.accepts = list(titles), accepts
        self.controls = SimpleNamespace(is_next_enabled=next_enabled, is_previous_enabled=previous_enabled)
        self.skipped = []

    def get_playback_info(self):
        return SimpleNamespace(controls=self.controls)

    async def try_get_media_properties_async(self):
        return SimpleNamespace(title=self.titles[0])

    async def try_skip_next_async(self):
        self.skipped.append("next")
        if len(self.titles) > 1:
            self.titles.pop(0)
        return self.accepts

    async def try_skip_previous_async(self):
        self.skipped.append("previous")
        if len(self.titles) > 1:
            self.titles.pop(0)
        return self.accepts


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_skip_is_proven_by_changed_title(monkeypatch):
    from jarvis import browser_control as bc
    monkeypatch.setattr(bc.asyncio, "sleep", lambda *_: _REAL_SLEEP(0))
    data = _run(bc._skip_media_session187(FakeSession(["A", "B"]), "next", {}))
    assert data["verified"] is True and data["title"] == "B" and data["input_stolen"] is False


def test_player_without_previous_video_is_reported_not_faked(monkeypatch):
    from jarvis import browser_control as bc
    session = FakeSession(["A", "B"], previous_enabled=False)
    with pytest.raises(bc.VideoNavigationUnavailable):
        _run(bc._skip_media_session187(session, "previous", {}))
    assert session.skipped == []  # nothing was sent


def test_same_title_after_skip_is_unverified(monkeypatch):
    from jarvis import browser_control as bc
    monkeypatch.setattr(bc.asyncio, "sleep", lambda *_: _REAL_SLEEP(0))
    with pytest.raises(bc.MediaVerificationError):
        _run(bc._skip_media_session187(FakeSession(["A"]), "previous", {}))


def _media_tool(monkeypatch, behaviour, code="ru"):
    from jarvis.tools.domains import browser_media
    registered = {}

    class Registry:
        def register(self, definition):
            registered[definition.name] = definition

    config = SimpleNamespace(get=lambda key, default=None: code if key == "app.language" else default)
    monkeypatch.setattr(browser_media, "control_service_media", behaviour)
    browser_media.register_browser_media_tools(Registry(), config=config, yandex_adapter=None, spotify_adapter=None,
                                               youtube_adapter=None, discord_adapter=None)
    definition = registered["media"]
    return getattr(definition, "handler", None) or getattr(definition, "function", None) or definition.callback


def test_media_tool_reports_missing_previous_video_in_user_language(monkeypatch):
    from jarvis import browser_control as bc
    from jarvis.runtime.localized_text import render_action_result

    def unavailable(*_a, **_k):
        raise bc.VideoNavigationUnavailable("previous")
    for code in ("ru", "en", "de"):
        handler = _media_tool(monkeypatch, unavailable, code)
        result = handler({"action": "previous", "target": "youtube"})
        assert result.success is False and result.data["unavailable"] is True
        assert result.text == text("previous_unavailable", code)
        assert render_action_result("media", {"action": "previous"}, False, result.text, result.data, code) == result.text


def test_media_tool_next_success_names_the_new_video(monkeypatch):
    handler = _media_tool(monkeypatch, lambda *_a, **_k: {"verified": True, "title": "Video B"})
    result = handler({"action": "next", "target": "youtube"})
    assert result.success is True and result.text == "Включил следующее видео YouTube: Video B."
