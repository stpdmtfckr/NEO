"""Real Agent → strict registry → registered media handlers, with inert adapters."""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jarvis.agent import Agent
from jarvis.runtime.conversation import ConversationStore
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.domains.browser_media import register_browser_media_tools
from jarvis.integrations.discord_navigation import ReliableDiscordAdapter


@pytest.fixture
def media_stack(tmp_path, monkeypatch):
    values = {"app.language": "ru", "integrations.yandex_music.enabled": True, "integrations.youtube.enabled": True}
    config = SimpleNamespace(paths=SimpleNamespace(root=tmp_path), get=lambda key, default=None: values.get(key, default))
    calls, opened, searched = [], [], []
    def audio(action, **kwargs):
        calls.append((action, kwargs))
        return {"verified": True, "title": "Actual Track", "artist": "Artist", "queued": kwargs.get("queue", False), "play_now": not kwargs.get("queue", False)}
    def video(query, **kwargs):
        searched.append((query, kwargs))
        return {"url": query, "title": "Actual Video"}
    registry = object.__new__(ToolRegistry)
    registry.config = config
    registry.database = Mock()
    registry.context_memory = Mock()
    registry._tools = {}
    registry.execution_guard = None
    registry.execution_transport = "local"
    register_browser_media_tools(registry, config=config, yandex_adapter=SimpleNamespace(execute=audio), spotify_adapter=Mock(), youtube_adapter=SimpleNamespace(search_video=video), discord_adapter=SimpleNamespace(execute=audio))
    from jarvis.tools.domains import browser_media
    monkeypatch.setattr(browser_media.webbrowser, "open", lambda url: opened.append(url) or True)
    # Both browser transports are intercepted, including the Windows executable
    # branch. No test may spawn the user's real Firefox merely because it exists.
    monkeypatch.setattr(browser_media.subprocess, "Popen", lambda argv, **kwargs: opened.append(argv[-1]) or Mock())
    agent = object.__new__(Agent)
    agent.config = config
    agent.tools = registry
    agent._lock = threading.RLock()
    agent.conversations = ConversationStore()
    agent._conversation_key = "desktop"
    agent.history = []
    agent.context_memory = Mock()
    agent._dialogue_preflight = lambda text, code: (text, None)
    agent._record_assistant_context = lambda text: None
    agent._user_address_context = lambda text: None
    agent._next_user_address_prefix = lambda context: ""
    agent._failure_user_address_prefix = lambda: ""
    agent._apply_user_address = lambda text, prefix: text
    return agent, registry, calls, opened, searched, values


@pytest.mark.parametrize("text,queued,next_up", [
    ("включи музыку на компьютере https://youtu.be/AbCdEfGhI12", False, False),
    ("добавь музыку https://youtu.be/AbCdEfGhI12 в очередь", True, False),
    ("поставь музыку https://youtu.be/AbCdEfGhI12 следующим треком в очереди", True, True),
])
def test_agent_local_audio_reaches_registered_adapter_with_queue_flags(media_stack, text, queued, next_up):
    agent, registry, calls, opened, searched, values = media_stack
    turn = agent.handle_stream(text)
    assert len(calls) == 1, turn.text
    assert calls[0][0] == "youtube_url"
    assert calls[0][1]["queue"] is queued and calls[0][1]["next_up"] is next_up
    assert opened == [] and searched == []
    assert agent._last_music_target == "yandex_music"
    if not queued:
        assert "Actual Track" in turn.text


@pytest.mark.parametrize("text", [
    "https://youtu.be/AbCdEfGhI12",
    "открой видео https://youtu.be/AbCdEfGhI12 в браузере",
])
def test_agent_ordinary_video_does_not_start_local_audio(media_stack, text):
    agent, registry, calls, opened, searched, values = media_stack
    turn = agent.handle_stream(text)
    assert calls == [] and len(opened) == 1 and len(searched) == 1, turn.text
    assert agent._last_media_target == "youtube"


def test_browser_queue_is_not_silently_opened_or_claimed_success(media_stack):
    _, registry, calls, opened, searched, _ = media_stack
    result = registry.execute("media", {"action": "play_search", "query": "https://youtu.be/AbCdEfGhI12", "queue": True})
    assert not result.success and result.data["requires_local_audio"]
    assert calls == [] and opened == [] and searched == []


def test_discord_volume_and_step_survive_registered_wrapper(media_stack):
    _, registry, calls, _, _, _ = media_stack
    result = registry.execute("discord", {"action": "music_set_volume", "volume": 0})
    assert result.success and calls[-1][1]["volume"] == 0
    result = registry.execute("discord", {"action": "music_volume_up", "step": 7})
    assert result.success and calls[-1][1]["step"] == 7


def test_discord_adapter_zero_and_relative_volume_preserve_zero(tmp_path, monkeypatch):
    config = SimpleNamespace(paths=SimpleNamespace(root=tmp_path), get=lambda key, default=None: default)
    adapter = ReliableDiscordAdapter(config)
    values = []
    bot = SimpleNamespace(voice_volume=lambda profile: {"volume": 0}, set_voice_volume=lambda profile, value: values.append(value) or {"verified": True, "volume": value})
    monkeypatch.setattr(adapter, "_background_profile", lambda server: {})
    monkeypatch.setattr(adapter, "_background_for_profile", lambda profile: bot)
    assert adapter.execute("music_set_volume", volume=0)["volume"] == 0
    assert adapter.execute("music_volume_up", step=7)["volume"] == 7
    assert adapter.execute("music_volume_down", step=7)["volume"] == 0
    assert values == [0, 7, 0]


def test_disabled_yandex_only_allows_actual_local_youtube_transport(media_stack, monkeypatch):
    from jarvis.integrations import yandex_music
    agent, registry, calls, opened, searched, values = media_stack
    values["integrations.yandex_music.enabled"] = False
    adapter = yandex_music.YandexMusicAdapter(agent.config)
    player = SimpleNamespace(status_if_running=lambda: {"state": "paused"}, pause=lambda: {"verified": True, "state": "paused"})
    monkeypatch.setattr(yandex_music, "local_player", lambda: player)
    monkeypatch.setattr(adapter, "_start_station_monitor", lambda: None)
    adapter._current_track_obj = {"_source_type": "youtube", "id": "video1", "title": "Local song"}
    adapter._current_track_meta = {"id": "video1", "title": "Local song"}
    registry._tools.clear()
    register_browser_media_tools(registry, config=agent.config, yandex_adapter=adapter, spotify_adapter=Mock(), youtube_adapter=Mock(), discord_adapter=Mock())
    assert registry.execute("yandex_music", {"action": "pause"}).success
    assert not registry.execute("yandex_music", {"action": "my_wave"}).success
    adapter._station_queue = [{"_source_type": "yandex_saved", "id": "catalogue1"}]
    assert not adapter.local_youtube_transport_allowed("next")
    adapter._station_queue = [{"_source_type": "youtube", "id": "video2"}]
    assert adapter.local_youtube_transport_allowed("next")
    adapter._current_track_obj = {"_source_type": "yandex_saved", "id": "catalogue1"}
    assert not registry.execute("yandex_music", {"action": "pause"}).success


@pytest.mark.parametrize("language", ["ru", "en", "de", "fr", "es", "it", "pt"])
@pytest.mark.parametrize("action", ["next", "previous", "play", "pause", "stop", "status", "queue_status", "shuffle_on", "shuffle_off", "repeat_on", "repeat_track", "repeat_playlist", "repeat_off"])
def test_content_source_survives_real_registry_projection(media_stack, language, action):
    agent, registry, calls, _, _, values = media_stack
    values["app.language"] = language
    payload = {"verified": True, "content_provider": "youtube", "transport_owner": "yandex_music",
               "media_source": "YouTube", "title": "Actual Track", "artist": "Artist", "state": "playing",
               "queue": [{"title": "Next YouTube", "content_provider": "youtube"}]}
    registry._tools.clear()
    register_browser_media_tools(registry, config=agent.config,
        yandex_adapter=SimpleNamespace(execute=lambda *a, **kw: dict(payload), queue_status=lambda **kw: dict(payload)),
        spotify_adapter=Mock(), youtube_adapter=Mock(), discord_adapter=Mock())
    result = registry.execute("yandex_music", {"action": action})
    assert result.success and "YouTube" in result.text
    assert "Yandex" not in result.text and "Яндекс" not in result.text
    assert result.data["transport_owner"] == "yandex_music"
    # 146: stopping/pausing must not claim that the old title is still playing.
    # Provenance remains in structured data for resume, history and dashboards.
    assert result.data["title"] == "Actual Track"
    if action not in {"stop", "pause"}:
        assert "Actual Track" in result.text
    else:
        assert "Actual Track" not in result.text


@pytest.mark.parametrize("language,phrase", [
    ("ru", "включи музыку на компьютере"), ("en", "play music"), ("de", "spiele Musik"),
    ("fr", "joue la musique"), ("es", "reproduce música"), ("it", "riproduci musica"), ("pt", "toca música"),
])
def test_agent_youtube_audio_source_in_each_language(media_stack, language, phrase):
    agent, _, calls, opened, _, values = media_stack
    values["app.language"] = language
    turn = agent.handle_stream(phrase + " https://youtu.be/AbCdEfGhI12")
    assert calls and calls[0][0] == "youtube_url", turn.text
    assert "YouTube" in turn.text and "Actual Track" in turn.text
    assert "Yandex" not in turn.text and "Яндекс" not in turn.text
    assert not opened


def test_youtube_queue_provenance_does_not_relabel_current_yandex(media_stack, monkeypatch):
    from jarvis.integrations import yandex_music
    agent, _, _, _, _, _ = media_stack
    adapter = yandex_music.YandexMusicAdapter(agent.config)
    adapter._current_track_obj = {"_source_type": "yandex_saved", "id": "old", "title": "Current Yandex"}
    monkeypatch.setattr(adapter, "_execute", lambda *a, **kw: {"verified": True, "queued": True})
    result = adapter.execute("youtube_url", queue=True)
    assert result["content_provider"] == "youtube"
    assert result["active_content_provider"] == "yandex_music"
    adapter._current_track_obj = {"_source_type": "youtube", "id": "video", "title": "Current YouTube"}
    result = adapter.execute("next")
    assert result["content_provider"] == result["active_content_provider"] == "youtube"


@pytest.mark.parametrize("language", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_network_timeout_never_relabels_saved_oauth_as_auth_failure(media_stack, language):
    agent, registry, _, _, _, values = media_stack
    values["app.language"] = language
    def failed(*a, **kw):
        raise RuntimeError("OAuth-токен сохранён. Timed out")
    registry._tools.clear()
    register_browser_media_tools(registry, config=agent.config, yandex_adapter=SimpleNamespace(execute=failed),
        spotify_adapter=Mock(), youtube_adapter=Mock(), discord_adapter=Mock())
    result = registry.execute("yandex_music", {"action": "my_wave"})
    assert not result.success and result.data["error_kind"] == "network"
    assert "OAuth" not in result.text and "RuntimeError" not in result.text


@pytest.mark.parametrize("language", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_end_of_youtube_queue_does_not_claim_another_track(media_stack, monkeypatch, language):
    from jarvis.integrations import yandex_music
    agent, registry, _, _, _, values = media_stack
    values["app.language"] = language
    adapter = yandex_music.YandexMusicAdapter(agent.config)
    adapter._current_track_obj = {"_source_type": "youtube", "id": "video1", "title": "Finished title"}
    adapter._current_track_meta = {"id": "video1", "title": "Finished title", "media_source": "YouTube"}
    adapter._station = "mixed:youtube"
    player = SimpleNamespace(status_if_running=lambda: {"state": "playing"}, stop=lambda: {"verified": True, "state": "stopped"})
    monkeypatch.setattr(yandex_music, "local_player", lambda: player)
    monkeypatch.setattr(adapter, "_api_client", lambda: pytest.fail("YouTube queue end must not probe Yandex"))
    registry._tools.clear()
    register_browser_media_tools(registry, config=agent.config, yandex_adapter=adapter,
        spotify_adapter=Mock(), youtube_adapter=Mock(), discord_adapter=Mock())
    for _ in range(2):
        result = registry.execute("yandex_music", {"action": "next"})
        assert result.success and result.data["playlist_finished"], result.text
        assert "YouTube" in result.text and "Yandex" not in result.text and "Яндекс" not in result.text
        assert "Finished title" not in result.text


def test_api_initialization_shares_failed_attempts_and_can_recover(media_stack, monkeypatch, caplog):
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from jarvis.integrations import yandex_music
    agent, _, _, _, _, _ = media_stack
    adapter = yandex_music.YandexMusicAdapter(agent.config)
    monkeypatch.setattr(adapter, "_api_enabled", lambda: True)
    monkeypatch.setattr(yandex_music, "yandex_music_token", lambda cfg: "test-token")
    attempts = []
    fail = [True]
    class Client:
        def __init__(self, token, request):
            assert token == "test-token"
        def init(self):
            attempts.append(1)
            if fail[0]:
                raise TimeoutError("Timed out")
            return self
    monkeypatch.setitem(sys.modules, "yandex_music", SimpleNamespace(Client=Client))
    monkeypatch.setitem(sys.modules, "yandex_music.utils.request", SimpleNamespace(Request=lambda **kw: kw))
    monkeypatch.setattr(yandex_music.time, "sleep", lambda delay: None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: adapter._api_client(), range(2)))
    assert results == [None, None] and len(attempts) == 2
    assert "1/2" in caplog.text and "2/2" in caplog.text and "2/3" not in caplog.text
    fail[0] = False
    adapter.on_network_changed()
    assert adapter._api_client() is not None and len(attempts) == 3


def test_passive_provenance_never_waits_for_transport_lock(media_stack):
    from concurrent.futures import ThreadPoolExecutor
    from jarvis.integrations import yandex_music
    agent, _, _, _, _, _ = media_stack
    adapter = yandex_music.YandexMusicAdapter(agent.config)
    adapter._current_track_obj = {"_source_type": "youtube", "id": "video"}
    assert adapter.playback_provenance()["content_provider"] == "youtube"
    with adapter._playback_lock:
        with ThreadPoolExecutor(max_workers=1) as pool:
            snapshot = pool.submit(adapter.playback_provenance).result(timeout=0.5)
    assert snapshot["content_provider"] == "youtube"


@pytest.mark.parametrize("language", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_upstream_internal_error_is_human_failure_not_json(media_stack, language):
    agent, registry, _, _, _, values = media_stack
    values["app.language"] = language
    def failed(*a, **kw):
        raise RuntimeError('{"error": "internalError", "request_id": "secret-debug-id"}')
    registry._tools.clear()
    register_browser_media_tools(registry, config=agent.config, yandex_adapter=SimpleNamespace(execute=failed),
        spotify_adapter=Mock(), youtube_adapter=Mock(), discord_adapter=Mock())
    result = registry.execute("yandex_music", {"action": "my_wave"})
    assert not result.success and result.data["error_kind"] == "service"
    assert "{" not in result.text and "internalError" not in result.text and "secret-debug-id" not in result.text


def test_relative_discord_volume_zero_is_not_reported_as_one_hundred(media_stack):
    agent, registry, _, _, _, _ = media_stack
    registry._tools.clear()
    register_browser_media_tools(registry, config=agent.config, yandex_adapter=Mock(),
        spotify_adapter=Mock(), youtube_adapter=Mock(),
        discord_adapter=SimpleNamespace(execute=lambda *a, **kw: {"verified": True, "volume": 0}))
    result = registry.execute("discord", {"action": "music_volume_down", "step": 10})
    assert result.success and "0%" in result.text and "100%" not in result.text


@pytest.mark.parametrize("language", ["ru", "en", "de", "fr", "es", "it", "pt"])
@pytest.mark.parametrize("yandex,spotify,selected,expected", [
    (True, False, "", "yandex_music"),
    (True, False, "yandex_music", "yandex_music"),
    (False, True, "spotify", "spotify"),
    (True, True, "yandex_music", "yandex_music"),
    (True, True, "spotify", "spotify"),
    (True, True, "", ""),
    (True, True, "unknown", ""),
    (False, False, "yandex_music", ""),
])
def test_music_provider_selection_is_language_independent_and_read_only(language, yandex, spotify, selected, expected):
    from jarvis.integrations.music_provider import active_music_provider
    values = {"app.language": language, "integrations.yandex_music.enabled": yandex,
              "integrations.spotify.enabled": spotify, "integrations.music.active_provider": selected}
    saved = dict(values)
    config = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
    assert active_music_provider(config) == expected
    assert values == saved


@pytest.mark.parametrize("language", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_generic_music_reaches_enabled_yandex_without_language_switching_accounts(media_stack, language):
    from jarvis.integrations.music_provider import provider_disabled_message, provider_label
    agent, registry, calls, _, _, values = media_stack
    values["app.language"] = language
    values["integrations.music.active_provider"] = "yandex_music"
    saved = dict(values)
    result = registry.execute("music", {"action": "play"})
    assert result.success and calls[-1][0] == "play", result.text
    assert result.data["active_provider"] == "yandex_music"
    assert values == saved
    expected_label = "Яндекс Музыка" if language == "ru" else "Yandex Music"
    assert provider_label("yandex_music", language) == expected_label
    assert expected_label in provider_disabled_message("yandex_music", language)
    if language != "ru":
        assert "Яндекс" not in result.text
        assert "Включите" not in provider_disabled_message("yandex_music", language)
