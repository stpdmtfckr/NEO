"""R040: approved personal aliases reach the same saved YouTube URL resolver."""
from __future__ import annotations

import copy
import time
from types import SimpleNamespace

import pytest

from .test_neo184_youtube_playlists import isolated, URL, COMMANDS
from .test_neo184_conversation_recovery_integrity import CASES
from jarvis.runtime.cancellation import cancellation_manager, OperationCancelled
from jarvis.runtime.dialogue_tasks import PendingIntent, Slot
from jarvis.testing.atlas_session import AtlasSession
from jarvis.tools.domains.browser_media import register_browser_media_tools
from jarvis.tools.registry import ToolExecution


@pytest.fixture
def session(tmp_path):
    value = AtlasSession(tmp_path / "alias_playlist")
    value.config.data["local_chat"]["live_small_talk"] = False
    for definition in value.agent.tools._tools.values():
        definition.handler = lambda _: ToolExecution(True, "QA inert", {"qa_simulated": True})
    value.agent.tools.set_execution_guard(lambda name, args, risk: value.calls.append((name, copy.deepcopy(args))) or None)
    value.transports = []
    def adapter(owner):
        def execute(action, **kwargs):
            value.transports.append((owner, action, copy.deepcopy(kwargs)))
            return {"verified": True, "submitted": True, "play_now": True, "state": "playing", "title": "Fixture song",
                    "kind": "playlist", "playlist_title": "Fixture list", "added_count": 2}
        return SimpleNamespace(execute=execute, local_youtube_transport_allowed=lambda _: True)
    registry = value.agent.tools
    replacement = SimpleNamespace(register=lambda definition: registry._tools.__setitem__(definition.name, definition))
    register_browser_media_tools(replacement, config=value.config, yandex_adapter=adapter("local"),
        discord_adapter=adapter("discord"), spotify_adapter=adapter("spotify"), youtube_adapter=SimpleNamespace())
    value.config.data["integrations"]["youtube"].update(music_playlists=[{"name": "Focus", "url": URL}], api_key_encrypted="")
    value.config.data["integrations"]["music"]["active_provider"] = "spotify"
    value.config.data["integrations"]["spotify"]["enabled"] = True
    value.config.data["integrations"]["discord"]["enabled"] = True
    yield value
    value.finish()


def save_alias(session, case, command, alias="Blue Hour"):
    code, template, _, yes, *_ = case
    session.config.data["app"]["language"] = code
    turn = session.agent.handle_stream(template.format(alias=alias, command=command))
    state = session.agent.conversations.get("desktop")
    assert state.pending_alias is not None, turn
    assert state.pending_alias["command"] == command
    assert session.calls == [] and session.transports == []
    session.agent.handle_stream(yes)
    assert session.agent.personal_aliases.resolve(alias, code) == command
    assert session.calls == [] and session.transports == []


@pytest.mark.parametrize("case", CASES, ids=lambda case: case[0])
@pytest.mark.parametrize("variant,owner", [(0, "local"), (1, "discord")])
def test_save_then_invoke_playlist_alias_all_languages(session, case, variant, owner):
    code = case[0]
    command = COMMANDS[code][variant]
    save_alias(session, case, command)
    turn = session.agent.handle_stream("Blue Hour")
    assert turn.success, turn
    assert len(session.transports) == 1 and session.transports[0][0] == owner
    assert session.transports[0][1] == ("music_youtube_url" if owner == "discord" else "youtube_url")
    assert session.transports[0][2]["query"] == URL
    assert session.agent._last_music_target == ("discord" if owner == "discord" else "yandex_music")
    assert not session.config.get("integrations.youtube.api_key_encrypted")


@pytest.mark.parametrize("case,cancel", [(CASES[0], "отмена"), (CASES[1], "cancel"), (CASES[2], "abbrechen"),
    (CASES[3], "annuler"), (CASES[4], "cancela"), (CASES[5], "annulla"), (CASES[6], "cancelar")])
def test_cancelled_alias_proposal_never_plays_or_saves(session, case, cancel):
    code, template, _, yes, *_ = case
    session.config.data["app"]["language"] = code
    session.agent.handle_stream(template.format(alias="Blue Hour", command=COMMANDS[code][0]))
    assert session.agent.conversations.get("desktop").pending_alias
    session.agent.handle_stream(cancel)
    session.agent.handle_stream(yes)
    assert not session.agent.personal_aliases.resolve("Blue Hour", code)
    assert session.transports == []


@pytest.mark.parametrize("case,verb", [(CASES[0], "напечатай"), (CASES[1], "type"), (CASES[2], "tippe"),
    (CASES[3], "écris"), (CASES[4], "escribe"), (CASES[5], "scrivi"), (CASES[6], "escreve")])
def test_saved_alias_in_literal_payload_does_not_expand(session, case, verb):
    save_alias(session, case, COMMANDS[case[0]][0])
    session.agent.handle_stream(verb + ' "Blue Hour"')
    assert session.transports == []
    # Existing dictation syntax consumes the surrounding quotation delimiters.
    assert len(session.calls) == 1 and session.calls[0][1]["text"] == "Blue Hour"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case[0])
def test_pending_literal_slot_owns_saved_alias(session, case):
    save_alias(session, case, COMMANDS[case[0]][0])
    state = session.agent.conversations.get("desktop")
    state.pending_intent = PendingIntent("text_edit", {"action": "dictate"}, (Slot("text", "text", "text"),), case[0], time.monotonic() + 60)
    session.agent.handle_stream("Blue Hour")
    assert session.transports == []
    assert session.calls == [("text_edit", {"action": "dictate", "text": "Blue Hour"})]


@pytest.mark.parametrize("changed", [[], [{"name": "Focus", "url": URL + "_rebound"}]])
@pytest.mark.parametrize("variant", [0, 1])
def test_alias_admitted_url_survives_configuration_mutation(session, changed, variant):
    save_alias(session, CASES[1], COMMANDS["en"][variant])
    original_execute = session.agent.tools.execute
    def mutate_then_execute(tool, arguments):
        session.config.data["integrations"]["youtube"]["music_playlists"][:] = changed
        return original_execute(tool, arguments)
    session.agent.tools.execute = mutate_then_execute
    turn = session.agent.handle_stream("Blue Hour")
    assert turn.success and len(session.transports) == 1
    assert session.transports[0][2]["query"] == URL


@pytest.mark.parametrize("code", ["ru", "en"])
def test_corrupt_matching_bookmark_from_alias_fails_without_transport(session, code):
    case = next(row for row in CASES if row[0] == code)
    save_alias(session, case, COMMANDS[code][0])
    session.config.data["integrations"]["youtube"]["music_playlists"][0]["url"] = "bad"
    turn = session.agent.handle_stream("Blue Hour")
    assert turn.success is False and session.transports == []


@pytest.mark.parametrize("code", ["ru", "en"])
def test_cancelled_execution_generation_never_dispatches_alias(session, code):
    case = next(row for row in CASES if row[0] == code)
    save_alias(session, case, COMMANDS[code][0])
    cancel = cancellation_manager()
    generation = cancel.reset()
    cancel.cancel("QA cancellation")
    with cancel.bound_generation(generation):
        try:
            turn = session.agent.handle_stream("Blue Hour")
        except OperationCancelled:
            pass
        else:
            assert turn.success is False or turn.outcome == "cancelled"
    assert session.transports == []


@pytest.mark.parametrize("command", ["play music Metallica", "play playlist Favorites on Spotify"])
def test_ordinary_service_alias_keeps_legacy_catalogue_route(session, command):
    save_alias(session, CASES[1], command)
    turn = session.agent.handle_stream("Blue Hour")
    assert session.transports and all(item[0] == "spotify" for item in session.transports)
    assert not any(item[2].get("query") == URL for item in session.transports)


@pytest.mark.parametrize("action,key", [("append_text", "text"), ("checklist_add", "item")])
def test_legacy_note_keeps_literal_alias_phrase(session, action, key):
    save_alias(session, CASES[1], COMMANDS["en"][0])
    session.agent._pending_overlay_followup = {"action": action, "name": "Example", "expires_at": time.monotonic() + 60}
    session.agent.handle_stream("Blue Hour")
    assert session.transports == [] and len(session.calls) == 1
    assert session.calls[0][0] == "overlay" and session.calls[0][1][key] == "Blue Hour"


@pytest.mark.parametrize("code,name,command", [
    ("de", "Focus nicht", "spiele Playlist Focus nicht"),
    ("en", "Focus, no, don't do anything", "play playlist Focus, no, don't do anything"),
    ("en", "Focus", '"play playlist Focus"'),
])
def test_preexisting_expanded_alias_cannot_bypass_nonexecution_guards(session, code, name, command):
    session.config.data["app"]["language"] = code
    session.config.data["integrations"]["youtube"]["music_playlists"] = [{"name": name, "url": URL}]
    session.agent.personal_aliases.add("Blue Hour", command, code, confirmed=True)
    turn = session.agent.handle_stream("Blue Hour")
    assert session.transports == []
    assert not turn.success


@pytest.mark.parametrize("case", CASES, ids=lambda case: case[0])
def test_alias_phrase_cannot_shadow_named_playlist_request(session, case):
    code, template, command, yes, *_ = case
    session.config.data["app"]["language"] = code
    phrase = COMMANDS[code][0]
    session.agent.handle_stream(template.format(alias=phrase, command=command))
    assert session.agent.conversations.get("desktop").pending_alias is None
    session.agent.handle_stream(yes)
    assert session.agent.personal_aliases.resolve(phrase, code) == ""
    assert session.transports == []


@pytest.mark.parametrize("case", [CASES[0], CASES[1]], ids=lambda case: case[0])
@pytest.mark.parametrize("variant", [0, 1])
def test_deleted_bound_bookmark_never_falls_back_and_recreation_uses_new_url(session, case, variant):
    code = case[0]
    command = COMMANDS[code][variant]
    save_alias(session, case, command)
    record = session.agent.personal_aliases.resolve_record("Blue Hour", code)
    assert record["phrase"] == "Blue Hour" and record["command"] == command
    assert record["target"] == {"kind": "youtube_playlist", "name": "Focus"}
    session.config.data["integrations"]["youtube"]["music_playlists"] = []
    turn = session.agent.handle_stream("Blue Hour")
    assert turn.success is False and turn.outcome == "clarification"
    assert session.calls == [] and session.transports == []
    assert "YouTube" in turn.text
    new_url = URL + "_recreated"
    session.config.data["integrations"]["youtube"]["music_playlists"] = [{"name": "Focus", "url": new_url}]
    turn = session.agent.handle_stream("Blue Hour")
    assert turn.success and len(session.transports) == 1
    assert session.transports[0][2]["query"] == new_url


def test_legacy_generic_alias_remains_generic_even_when_name_matches_bookmark(session):
    session.config.data["app"]["language"] = "en"
    session.agent.personal_aliases.add("Blue Hour", "play playlist Focus", "en", confirmed=True)
    assert "target" not in session.agent.personal_aliases.resolve_record("Blue Hour", "en")
    session.agent.handle_stream("Blue Hour")
    assert session.transports and all(row[0] == "spotify" for row in session.transports)
    assert not any(row[2].get("query") == URL for row in session.transports)


def test_binding_survives_fresh_alias_reader_and_keeps_exact_bytes(session):
    from jarvis.runtime.conversation import PersonalAliases
    aliases = session.agent.personal_aliases
    phrase, command = " Blue Hour!? ", " play playlist Focus "
    binding = {"kind": "youtube_playlist", "name": "Focus"}
    aliases.add(phrase, command, "en", confirmed=True, target=binding)
    binding["name"] = "Changed outside"
    fresh = PersonalAliases(session.agent.database)
    record = fresh.resolve_record(phrase, "en")
    assert record["phrase"] == phrase and record["command"] == command
    assert record["target"] == {"kind": "youtube_playlist", "name": "Focus"}
    record["target"]["name"] = "Changed snapshot"
    assert fresh.resolve_record(phrase, "en")["target"]["name"] == "Focus"


def test_failed_binding_write_does_not_publish_cache_or_overwrite_alias(session, monkeypatch):
    aliases = session.agent.personal_aliases
    aliases.add("Blue Hour", "play music Metallica", "en", confirmed=True)
    before = copy.deepcopy(aliases._cache)
    raw_before = copy.deepcopy(aliases._cache_raw)
    monkeypatch.setattr(session.agent.database, "set_memory", lambda *_: (_ for _ in ()).throw(OSError("QA disk failure")))
    with pytest.raises(OSError):
        aliases.add("Blue Hour", "play playlist Focus", "en", confirmed=True, target={"kind": "youtube_playlist", "name": "Focus"})
    assert aliases._cache == before and aliases._cache_raw == raw_before
    assert aliases.resolve("Blue Hour", "en") == "play music Metallica"


@pytest.mark.parametrize("target", [{}, {"kind": "other", "name": "Focus"}, {"kind": "youtube_playlist", "name": ""},
    {"kind": "youtube_playlist", "name": None}, {"kind": "youtube_playlist", "name": "Focus", "url": URL}, "bad"])
def test_invalid_alias_binding_rejected_without_mutation(session, target):
    aliases = session.agent.personal_aliases
    before = aliases.rows("en")
    with pytest.raises(ValueError):
        aliases.add("Blue Hour", "play playlist Focus", "en", confirmed=True, target=target)
    assert aliases.rows("en") == before


@pytest.mark.parametrize("target", [None, {}, {"kind": "other", "name": "Focus"}, {"kind": "youtube_playlist", "name": None},
    {"kind": "youtube_playlist", "name": " " * 101 + "Focus"}, {"kind": "youtube_playlist", "name": "Focus" + "\u0301" * 101}])
def test_corrupt_persisted_binding_fails_closed(session, target):
    save_alias(session, CASES[1], COMMANDS["en"][0])
    rows = session.agent.personal_aliases.rows("en")
    rows["blue hour"]["target"] = target
    session.agent.personal_aliases._save("en", rows)
    turn = session.agent.handle_stream("Blue Hour")
    assert turn.success is False and session.calls == [] and session.transports == []


@pytest.mark.parametrize("opening,closing", [("'", "'"), ('“', '”'), ('"', '"')])
def test_padded_quoted_legacy_alias_expansion_is_not_executable(session, opening, closing):
    session.config.data["app"]["language"] = "en"
    command = "  " + opening + "play playlist Focus" + closing + "  "
    session.agent.personal_aliases.add("Blue Hour", command, "en", confirmed=True)
    turn = session.agent.handle_stream("Blue Hour")
    assert session.transports == [] and session.calls == []
    assert turn.success is False
    assert session.agent.personal_aliases.resolve("Blue Hour", "en") == command
