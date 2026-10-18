"""Named YouTube playlists: inert Agent → strict registry → fake transport."""
from __future__ import annotations

import os
import copy
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from jarvis.agent import Agent
from jarvis.runtime.conversation import ConversationStore
from jarvis.runtime.youtube_playlists184 import (
    SETTINGS_KEY, name_key, parse_request, playlist_url, resolve_request, rows_for_ui, validate_playlists,
)
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.domains.browser_media import register_browser_media_tools
from jarvis.tools.registry import ToolRegistry, ToolExecution


URL = "https://www.youtube.com/playlist?list=PL_NEO_test"
COMMANDS = {
    "ru": ("включи плейлист Focus", "поставь мой плейлист Focus в дискорде", "запусти музыку Focus на компьютере", "включи Focus"),
    "en": ("play playlist Focus", "start my YouTube playlist Focus on Discord", "put on music Focus locally", "play Focus"),
    "de": ("spiele die Wiedergabeliste Focus", "starte meine Playlist Focus auf Discord", "spiel Musik Focus lokal ab", "starte Focus"),
    "fr": ("joue la playlist Focus", "lance ma playlist YouTube Focus sur Discord", "mets musique Focus en local", "joue Focus"),
    "es": ("reproduce la lista de reproducción Focus", "pon mi playlist Focus en Discord", "inicia música Focus localmente", "pon Focus"),
    "it": ("riproduci la playlist Focus", "avvia la mia playlist Focus su Discord", "metti musica Focus localmente", "riproduci Focus"),
    "pt": ("reproduza a lista de reprodução Focus", "toca a minha playlist Focus no Discord", "coloque música Focus localmente", "toque Focus"),
}
NEGATIVES = {
    "ru": ("не включай плейлист Focus", "что означает включи плейлист Focus", 'напиши "включи плейлист Focus"'),
    "en": ("do not play playlist Focus", "what does play playlist Focus mean", 'type "play playlist Focus"'),
    "de": ("spiele die Playlist Focus nicht", "was bedeutet spiele Playlist Focus", 'schreibe "spiele Playlist Focus"'),
    "fr": ("ne joue pas la playlist Focus", "que signifie joue playlist Focus", 'écris "joue playlist Focus"'),
    "es": ("no reproduzcas la playlist Focus", "qué significa reproduce playlist Focus", 'escribe "reproduce playlist Focus"'),
    "it": ("non riprodurre la playlist Focus", "cosa significa riproduci playlist Focus", 'scrivi "riproduci playlist Focus"'),
    "pt": ("não reproduza a playlist Focus", "o que significa reproduza playlist Focus", 'escreve "reproduza playlist Focus"'),
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key in ("HOME", "LOCALAPPDATA", "APPDATA", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    from jarvis.runtime import media_activity
    monkeypatch.setattr(media_activity, "_STATE", {"owner": "", "state": "", "updated": 0.0})
    monkeypatch.setattr(media_activity, "_OWNERS", {})
    with external_io_blocked(), patch.object(socket.socket, "sendto", side_effect=PermissionError("QA blocks datagrams")), patch.object(os, "system", side_effect=PermissionError("QA blocks shell")):
        yield


@pytest.fixture
def stack(tmp_path):
    values = {"app.language": "en", "integrations.youtube.enabled": True,
              "integrations.youtube.api_key_encrypted": "", SETTINGS_KEY: [{"name": "Focus", "url": URL}],
              "integrations.music.active_provider": "spotify", "integrations.spotify.enabled": True,
              "integrations.yandex_music.enabled": False}
    config = SimpleNamespace(paths=SimpleNamespace(root=tmp_path), get=lambda key, default=None: values.get(key, default))
    calls = []
    payload = {"verified": True, "submitted": True, "play_now": True, "state": "playing", "title": "Fixture Track",
               "kind": "playlist", "added_count": 4, "playlist_title": "Fixture Playlist", "queue": []}
    def transport(owner):
        def execute(action, **kwargs):
            calls.append((owner, action, kwargs))
            return dict(payload)
        return SimpleNamespace(execute=execute, local_youtube_transport_allowed=lambda _: True)
    registry = object.__new__(ToolRegistry)
    registry.config, registry.database, registry.context_memory = config, Mock(), Mock()
    registry._tools, registry.execution_guard, registry.execution_transport = {}, None, "local"
    register_browser_media_tools(registry, config=config, yandex_adapter=transport("local"),
                                 spotify_adapter=transport("spotify"), youtube_adapter=Mock(), discord_adapter=transport("discord"))
    agent = object.__new__(Agent)
    agent.config, agent.tools, agent._lock = config, registry, threading.RLock()
    agent.conversations, agent._conversation_key = ConversationStore(), "desktop"
    agent.history, agent.context_memory = [], Mock()
    agent._record_assistant_context = lambda _: None
    agent._user_address_context = lambda _: None
    agent._next_user_address_prefix = lambda _: ""
    agent._failure_user_address_prefix = lambda: ""
    agent._apply_user_address = lambda text, _: text
    agent._confirmation_policy = None
    return agent, registry, calls, values, payload


@pytest.mark.parametrize("code", list(COMMANDS))
@pytest.mark.parametrize("variant", range(4))
def test_multilingual_commands_reach_only_requested_transport(stack, code, variant):
    agent, registry, calls, values, _ = stack
    values["app.language"] = code
    pieces = []
    turn = agent.handle_stream(COMMANDS[code][variant], on_text=pieces.append)
    assert turn.success, (turn, calls)
    owner = "discord" if variant == 1 else "local"
    assert len(calls) == 1 and calls[0][0] == owner
    assert calls[0][1] == ("music_youtube_url" if owner == "discord" else "youtube_url")
    assert calls[0][2]["query"] == URL
    assert agent._last_music_target == ("discord" if owner == "discord" else "yandex_music")
    state = agent.conversations.get("desktop")
    assert state.media_owner157["tool"] == agent._last_music_target
    assert "Focus" in turn.text and "".join(pieces) == turn.text
    if code != "ru":
        assert "Включил" not in turn.text
    assert not registry._tools["media"].handler.__name__ == "browser"


@pytest.mark.parametrize("code", list(COMMANDS))
def test_unknown_playlist_gives_localized_help_without_transport(stack, code):
    agent, _, calls, values, _ = stack
    values["app.language"] = code
    turn = agent.handle_stream(COMMANDS[code][0].replace("Focus", "YouTube Unknown"))
    assert turn.success is False and turn.outcome == "clarification"
    assert calls == [] and "YouTube" in turn.text
    if code != "ru":
        assert "найден" not in turn.text


@pytest.fixture(scope="module")
def full_session(tmp_path_factory):
    from jarvis.testing.atlas_session import AtlasSession
    with external_io_blocked():
        session = AtlasSession(tmp_path_factory.mktemp("playlist_boundaries") / "session")
        for definition in session.agent.tools._tools.values():
            definition.handler = lambda _: ToolExecution(True, "QA inert", {"qa_simulated": True})
        session.agent.tools.set_execution_guard(lambda name, args, risk: session.calls.append((name, copy.deepcopy(args))) or None)
        yield session
        session.finish()


@pytest.mark.parametrize("code", list(COMMANDS))
def test_no_execution_from_negation_quotes_or_reported_commands(full_session, code):
    session = full_session
    for text in (*NEGATIVES[code][:2], '"' + COMMANDS[code][0] + '"'):
        session.reset_case(code)
        session.config.data["integrations"]["youtube"]["music_playlists"] = [{"name": "Focus", "url": URL}]
        turn = session.agent.handle_stream(text)
        assert not any(tool in {"music", "media", "discord", "yandex_music", "spotify"} for tool, _ in session.calls), (text, turn, session.calls)
    for text in NEGATIVES[code][1:]:
        assert resolve_request(text, code, [{"name": "Focus", "url": URL}]) is None


@pytest.mark.parametrize("code,verb", [("ru", "напечатай"), ("en", "type"), ("de", "tippe"), ("fr", "écris"),
                                     ("es", "escribe"), ("it", "scrivi"), ("pt", "escreve")])
def test_playlist_command_in_literal_dictation_is_only_data(full_session, code, verb):
    session = full_session
    session.reset_case(code)
    session.config.data["integrations"]["youtube"]["music_playlists"] = [{"name": "Focus", "url": URL}]
    command = COMMANDS[code][0]
    session.agent.handle_stream(verb + " " + command)
    assert len(session.calls) == 1
    tool, args = session.calls[0]
    assert tool in {"ui", "text_edit", "input_control"} and args.get("text") == command


@pytest.mark.parametrize("code,opening", [("ru", "создай заметку"), ("en", "create a note"), ("de", "erstelle eine Notiz"),
    ("fr", "crée une note"), ("es", "crea una nota"), ("it", "crea una nota"), ("pt", "cria uma nota")])
def test_pending_note_body_owns_playlist_command(full_session, code, opening):
    session = full_session
    session.reset_case(code)
    session.config.data["integrations"]["youtube"]["music_playlists"] = [{"name": "Focus", "url": URL}]
    session.agent.handle_stream(opening)
    session.agent.handle_stream("Playlist test note")
    session.agent.handle_stream(COMMANDS[code][0])
    assert len(session.calls) == 1
    assert session.calls[0][0] == "shelf" and session.calls[0][1]["text"] == COMMANDS[code][0]


@pytest.mark.parametrize("code", list(COMMANDS))
def test_other_command_language_does_not_match(code):
    other = "en" if code != "en" else "ru"
    assert parse_request(COMMANDS[code][0], other) is None


@pytest.mark.parametrize("change", [{"verified": False}, {"verified": None}, {"starting": True},
                                     {"state": "paused"}, {"state": "stopped"}, {"pending": True}, {"playing": False}])
@pytest.mark.parametrize("variant", [0, 1])
def test_unverified_or_pending_never_claims_playback_or_replaces_owner(stack, change, variant):
    agent, _, calls, _, payload = stack
    payload.update(change)
    agent._last_music_target = "prior_owner"
    turn = agent.handle_stream(COMMANDS["en"][variant])
    assert turn.success is False
    assert "Started" not in turn.text and "Playing" not in turn.text
    assert agent._last_music_target == "prior_owner"
    assert len(calls) == 1
    assert not agent.conversations.get("desktop").media_owner157


def test_discord_submission_must_be_confirmed(stack):
    agent, _, calls, _, payload = stack
    payload["submitted"] = False
    turn = agent.handle_stream(COMMANDS["en"][1])
    assert turn.success is False and len(calls) == 1


def test_remote_policy_denial_is_not_success(stack):
    agent, registry, calls, _, _ = stack
    registry.execution_guard = lambda *_: "Blocked by remote policy"
    turn = agent.handle_stream(COMMANDS["en"][0])
    assert turn.success is False and calls == []


@pytest.mark.parametrize("source", ["system_audio", "ambient", "loopback"])
def test_passive_input_cannot_play(stack, source):
    agent, _, calls, _, _ = stack
    agent.handle_stream(COMMANDS["en"][0], input_source=source)
    assert calls == []


def test_bad_saved_configuration_is_not_searched_or_executed(stack):
    agent, _, calls, values, _ = stack
    values[SETTINGS_KEY] = [{"name": "Focus", "url": "https://evil.example/playlist?list=PL_test"}]
    turn = agent.handle_stream(COMMANDS["en"][0])
    assert turn.success is False and calls == [] and "YouTube" in turn.text


def test_literal_title_is_not_typo_rewritten(stack, monkeypatch):
    agent, _, calls, values, _ = stack
    values[SETTINGS_KEY][0]["name"] = "Enter Now?"
    from jarvis.runtime import typos175, command_typos165
    monkeypatch.setattr(typos175, "repair", lambda *_: (_ for _ in ()).throw(AssertionError("rewrote title")))
    monkeypatch.setattr(command_typos165, "fix_layout", lambda *_: (_ for _ in ()).throw(AssertionError("rewrote title")))
    turn = agent.handle_stream('play playlist "Enter Now?"')
    assert turn.success and len(calls) == 1


def test_unrelated_launch_retains_existing_typo_repair(stack, monkeypatch):
    agent, _, _, _, _ = stack
    from jarvis.runtime import command_typos165
    seen = []
    class ReachedExistingRepair(Exception):
        pass
    def repair(text, code):
        seen.append(text)
        raise ReachedExistingRepair
    monkeypatch.setattr(command_typos165, "fix_layout", repair)
    with pytest.raises(ReachedExistingRepair):
        agent.handle_stream("start Firfox")
    assert seen == ["start Firfox"]


def test_explicit_target_conflict_executes_nothing(stack):
    agent, _, calls, _, _ = stack
    turn = agent.handle_stream("play playlist Focus on Discord locally")
    assert turn.success is False and calls == []


@pytest.mark.parametrize("url", [URL, "https://music.youtube.com/playlist?list=PL_NEO_test&si=tracked",
    "http://m.youtube.com/watch?v=AbCdEfGhI12&list=PL_NEO_test&index=7", "https://youtu.be/AbCdEfGhI12?list=PL_NEO_test"])
def test_url_forms_canonicalize_without_track_seed(url):
    assert playlist_url(url) == URL


@pytest.mark.parametrize("url", ["", "https://www.youtube.com/watch?v=AbCdEfGhI12", "https://youtube.com/playlist?list=",
    "https://youtube.com.evil.example/playlist?list=PL_test", "https://evil.example/playlist?list=PL_test",
    "https://name:password@youtube.com/playlist?list=PL_test", "https://youtube.com:443/playlist?list=PL_test",
    "file:///playlist?list=PL_test", "https://youtube.com/playlist?list=PL_one&list=PL_two", "https://youtube.com/playlist?list=PL%20bad",
    "https://youtube.com/channel?list=PL_test", "https://youtube.com/playlist?list=PL_test\nhttps://evil.example"])
def test_invalid_urls_fail_closed(url):
    with pytest.raises(ValueError):
        playlist_url(url)


@pytest.mark.parametrize("rows", [None, {}, ["text"], [{"name": "", "url": URL}], [{"name": "x" * 101, "url": URL}],
    [{"name": "Café", "url": URL}, {"name": " CAFE  ", "url": URL + "_2"}],
    [{"name": "Focus", "url": URL}, {"name": "Other", "url": "https://youtube.com/watch?v=id&list=PL_NEO_test"}]])
def test_invalid_configuration_fails_closed(rows):
    with pytest.raises(ValueError):
        validate_playlists(rows)


def test_case_accents_and_space_matching_preserve_saved_display_name():
    rows = validate_playlists([{"name": "  Café   Calme  ", "url": URL}])
    request, entry = resolve_request("play playlist CAFE calme", "en", rows)
    assert entry == {"name": "Café Calme", "url": URL}
    assert rows_for_ui([{"name": "broken", "url": "wrong"}]) == [{"name": "broken", "url": "wrong"}]
    assert name_key(" Café  Calme ") == "cafe calme"


def test_unrelated_short_command_is_not_claimed_even_with_bad_storage():
    assert resolve_request("start Firefox", "en", {}) is None


@pytest.mark.parametrize("text", ["play music Metallica", "play music on Spotify", "play playlist Favorites on Spotify",
                                "play playlist Favorites", "play Spotify playlist Favorites"])
def test_other_music_routes_are_preserved(text):
    assert resolve_request(text, "en", []) is None
    assert resolve_request(text, "en", [{"name": "Focus", "url": URL}]) is None
    assert resolve_request(text, "en", [{"name": "Focus", "url": "bad"}]) is None


@pytest.mark.parametrize("changed", [[], [{"name": "Focus", "url": URL + "_changed"}]])
def test_admitted_url_is_immutable_if_name_changes_before_dispatch(stack, changed):
    agent, registry, calls, values, _ = stack
    original_execute = registry.execute
    def mutate_then_execute(tool, arguments):
        values[SETTINGS_KEY][:] = changed
        return original_execute(tool, arguments)
    registry.execute = mutate_then_execute
    turn = agent.handle_stream(COMMANDS["en"][0])
    assert turn.success and calls[0][2]["query"] == URL


def test_duplicate_persisted_names_are_not_resolved_by_first_match(stack):
    agent, _, calls, values, _ = stack
    values[SETTINGS_KEY].append({"name": " FOCUS ", "url": URL + "_2"})
    turn = agent.handle_stream(COMMANDS["en"][0])
    assert turn.success is False and calls == [] and "settings" in turn.text.casefold()


@pytest.mark.parametrize("flag", ["paused", "cancelled", "superseded", "outcome_pending", "outcome_unknown"])
def test_contradictory_evidence_does_not_report_success(stack, flag):
    agent, _, calls, _, payload = stack
    payload.pop("state")
    payload[flag] = True
    turn = agent.handle_stream(COMMANDS["en"][0])
    assert turn.success is False and len(calls) == 1


@pytest.mark.parametrize("action", ["append_text", "checklist_add"])
def test_legacy_pending_overlay_owns_playlist_command(full_session, action):
    session = full_session
    session.reset_case("en")
    session.config.data["integrations"]["youtube"]["music_playlists"] = [{"name": "Focus", "url": URL}]
    session.agent._pending_overlay_followup = {"action": action, "name": "Example", "expires_at": time.monotonic() + 60}
    session.agent.handle_stream(COMMANDS["en"][0])
    assert len(session.calls) == 1
    tool, args = session.calls[0]
    assert tool == "overlay" and args["action"] == action
    assert args.get("text", args.get("item")) == COMMANDS["en"][0]


@pytest.mark.parametrize("code", list(COMMANDS))
@pytest.mark.parametrize("variant,owner", [(0, "local"), (1, "discord")])
def test_consecutive_followups_keep_bookmarks_actual_player(stack, code, variant, owner):
    from jarvis.runtime.music_requests157 import FORMS, BARE
    from jarvis.runtime.natural_commands import LANGUAGES
    agent, _, calls, values, _ = stack
    values["app.language"] = code
    assert agent.handle_stream(COMMANDS[code][variant]).success
    index = LANGUAGES.index(code)
    for action in ("pause", "play", "next", "previous", "stop"):
        for phrase in (FORMS[action][index].split("|")[0], BARE[action][index].split("|")[0]):
            calls.clear()
            turn = agent.handle_stream(phrase)
            assert calls and all(call[0] == owner for call in calls), (code, phrase, turn, calls)
            assert agent.conversations.get("desktop").media_owner157["named_youtube_playlist"]


def test_explicit_provider_followup_overrides_bookmark_owner(stack):
    agent, _, calls, _, _ = stack
    assert agent.handle_stream(COMMANDS["en"][1]).success
    calls.clear()
    agent.handle_stream("pause music on Spotify")
    assert calls and all(call[0] == "spotify" for call in calls)


@pytest.mark.parametrize("change", [{"verified": None}, {"starting": True}, {"pending": True}, {"playing": False},
    {"state": "paused"}, {"cancelled": True}])
@pytest.mark.parametrize("variant", [0, 1])
def test_unconfirmed_real_wrapper_keeps_global_owner(stack, change, variant):
    from jarvis.runtime.media_activity import note_media_observation, recent_music_owner
    agent, _, calls, _, payload = stack
    note_media_observation("spotify", playing=True)
    payload.update(change)
    if change.get("verified", True) is None:
        payload.pop("verified")
    turn = agent.handle_stream(COMMANDS["en"][variant])
    assert turn.success is False and len(calls) == 1
    assert recent_music_owner() == "spotify"


@pytest.mark.parametrize("variant,target", [(0, "yandex_music"), (1, "discord")])
def test_confirmed_real_wrapper_updates_global_owner(stack, variant, target):
    from jarvis.runtime.media_activity import note_media_observation, recent_music_owner
    agent, _, _, _, _ = stack
    note_media_observation("spotify", playing=True)
    assert agent.handle_stream(COMMANDS["en"][variant]).success
    assert recent_music_owner() == target


@pytest.mark.parametrize("opening,closing", [('"', '"'), ("'", "'"), ('‘', '’'), ('«', '»'), ('“', '”'), ('„', '“')])
def test_whole_command_quotes_never_start_a_playlist(full_session, opening, closing):
    session = full_session
    session.reset_case("en")
    session.config.data["integrations"]["youtube"]["music_playlists"] = [{"name": "Focus", "url": URL}]
    turn = session.agent.handle_stream(opening + COMMANDS["en"][0] + closing)
    assert session.calls == [] and turn.success is False


@pytest.mark.parametrize("command,action", [("back", "back"), ("next", "forward")])
def test_browser_navigation_after_playlist_keeps_browser_priority(full_session, command, action):
    session = full_session
    session.reset_case("en")
    state = session.agent.conversations.get("desktop")
    state.language = "en"
    state.last_tool = "browser"
    state.media_owner157 = {"tool": "discord", "named_youtube_playlist": True, "language": "en", "at": time.monotonic(),
                            "arguments": {"action": "music_youtube_url"}}
    session.agent.handle_stream(command)
    assert session.calls == [("browser_nav", {"action": action})]
