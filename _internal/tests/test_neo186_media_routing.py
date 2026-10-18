"""186: typed media phrases, negation scope and literal playlist names (inert tools)."""
from __future__ import annotations

import pytest

from jarvis.runtime import everyday172, local_chat169, reply_memory168
from jarvis.runtime.media_grammar186 import canonical_media_command
from jarvis.runtime.multilingual_commands import normalize_multilingual_command
from jarvis.runtime.natural_commands import negated_action
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    with external_io_blocked():
        everyday172.clear_offer(); reply_memory168.clear(); local_chat169.clear_history()
        value = AtlasSession(tmp_path_factory.mktemp("media186") / "run")
        value.config.data["local_chat"]["live_small_talk"] = False
        yield value
        value.finish()
        everyday172.clear_offer(); reply_memory168.clear(); local_chat169.clear_history()


def _calls(session, code, text):
    with external_io_blocked():
        session.reset_case(code)
        session.agent.handle(text)
        return [(c["tool"], dict(c["arguments"])) for c in session.calls]


@pytest.mark.parametrize("code,text,tool,action", [
    ("ru", "включи пермешивание", "music", "shuffle_on"),
    ("ru", "перемешай плейлист на компьютере", "music", "shuffle_on"),
    ("ru", "включи повтор плейлиста на ПК", "music", "repeat_playlist"),
    ("en", "repeat this song", "music", "repeat_track"),
    ("en", "repeat the playlist", "music", "repeat_playlist"),
    ("en", "Take this song off pause", "music", "play"),
    ("en", "Go back to the last song please", "music", "previous"),
    ("en", "play the previous song", "music", "previous"),
    ("es", "Pasa a la canción siguiente, por favor", "music", "next"),
    ("pt", "Passe para a próxima música, por favor", "music", "next"),
    ("de", "spiel das nächste Lied", "music", "next"),
    ("fr", "passe à la chanson suivante", "music", "next"),
    ("it", "passa alla canzone successiva", "music", "next"),
    ("it", "Mostrami la coda", "music", "queue_status"),
    ("pt", "Mostra a fila", "music", "queue_status"),
    ("it", "Metti in loop tutta la playlist", "music", "repeat_playlist"),
    ("fr", "active la lecture aléatoire", "music", "shuffle_on"),
    ("en", "shuffle the playlist on Yandex Music", "yandex_music", "shuffle_on"),
    ("es", "activa el modo aleatorio en Spotify", "spotify", "shuffle_on"),
    ("ru", "включи перемешивание в спотифай", "spotify", "shuffle_on"),
])
def test_u03_native_media_phrase_keeps_action(session, code, text, tool, action):
    calls = _calls(session, code, text)
    assert calls and calls[0][0] == tool and calls[0][1]["action"] == action
    assert "query" not in calls[0][1]


@pytest.mark.parametrize("code,text", [
    ("de", "setze das Video fort"), ("de", "bitte, setze das Video fort"),
    ("ru", "поставь видео на YouTube на паузу"), ("ru", "переключи полноэкранный режим YouTube"),
])
def test_u03_video_commands_keep_youtube_target(session, code, text):
    calls = _calls(session, code, text)
    assert calls and calls[0][0] == "media" and calls[0][1].get("target") == "youtube"


@pytest.mark.parametrize("text", ["In Yandex Music, play playlist Night and Day",
                                  "Play playlist Night and Day in Yandex Music"])
def test_u03_unquoted_playlist_name_stays_literal(session, text):
    calls = _calls(session, "en", text)
    assert calls and calls[0][0] == "yandex_music" and calls[0][1]["action"] == "playlist"
    assert (calls[0][1].get("playlist") or calls[0][1].get("query")) == "Night and Day"


@pytest.mark.parametrize("text", ["n’active pas la lecture aléatoire", "n'active pas la lecture aléatoire",
                                  "ne lance pas la musique"])
def test_u08_french_negation_with_any_apostrophe_writes_nothing(session, text):
    assert _calls(session, "fr", text) == []
    assert negated_action(text, "fr")


@pytest.mark.parametrize("word", ["nihct", "nichtt", "niicht", "nicht"])
def test_de_negation_typos_block_launch(session, word):
    text = f"öffne die Anwendung QA_Negation_731 {word}"
    assert negated_action(text, "de")
    assert _calls(session, "de", text) == []


@pytest.mark.parametrize("text", ["spiele Gute Nacht", "öffne die Anwendung Notepad", "spiele nichts"])
def test_de_real_words_near_nicht_are_not_negations(text):
    assert not negated_action(text, "de")


@pytest.mark.parametrize("code,text", [("es", "pasa"), ("de", "setze"), ("pt", "passe")])
def test_u03_real_words_are_not_fuzzy_rewritten(code, text):
    normalized = normalize_multilingual_command(text + " algo", code)
    assert normalized.split()[0].casefold() == text


@pytest.mark.parametrize("code,text", [("en", "next"), ("de", "weiter"), ("it", "indietro"), ("en", "play Night and Day")])
def test_grammar_leaves_bare_words_and_titles_to_existing_routes(code, text):
    assert canonical_media_command(text, code) is None


def test_u13_equal_playlists_ask_instead_of_api_order(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from jarvis.integrations import yandex_music as ym
    monkeypatch.setattr(ym.YandexMusicAdapter, "clear_transient_cache_on_startup_once", lambda *a: {})
    config = SimpleNamespace(paths=SimpleNamespace(root=tmp_path), get=lambda key, default=None: default, data={})
    adapter = ym.YandexMusicAdapter(config)
    a = SimpleNamespace(title="Evening and Rain", kind="1", uid="u", playlist_uuid="A")
    b = SimpleNamespace(title="Evening and Rain", kind="2", uid="u", playlist_uuid="B")
    for order in ([a, b], [b, a]):
        monkeypatch.setattr(adapter, "_user_playlists", lambda order=order: list(order))
        with pytest.raises(ym.PlaylistAmbiguous):
            adapter.resolve_playlist("Evening and Rain")
    piano = SimpleNamespace(title="Evening Piano", kind="3", uid="u", playlist_uuid="C")
    rain = SimpleNamespace(title="Evening Rain", kind="4", uid="u", playlist_uuid="D")
    monkeypatch.setattr(adapter, "_user_playlists", lambda: [piano, rain])
    with pytest.raises(ym.PlaylistAmbiguous):
        adapter.resolve_playlist("Evening")
    night = SimpleNamespace(title="Night and Day", kind="5", uid="u", playlist_uuid="E")
    day = SimpleNamespace(title="Day", kind="6", uid="u", playlist_uuid="F")
    for order in ([day, night], [night, day]):
        monkeypatch.setattr(adapter, "_user_playlists", lambda order=order: list(order))
        assert adapter.resolve_playlist("Night and Day")["kind"] == "5"
    rock = SimpleNamespace(title="Rock and Roll", kind="7", uid="u", playlist_uuid="G")
    monkeypatch.setattr(adapter, "_user_playlists", lambda: [rock, day])
    assert adapter.resolve_playlist("Rock and Roll")["kind"] == "7"


@pytest.mark.parametrize("info,process,title,expected", [
    ({"source": "firefox.exe", "title": ""}, "firefox.exe", "Clip - YouTube", True),
    ({"source": "firefox.exe", "title": "Clip - YouTube"}, "firefox.exe", "Clip - YouTube", True),
    ({"source": "vlc.exe", "title": "Film"}, "firefox.exe", "Clip - YouTube", False),            # S07
    ({"source": "chrome.exe", "title": ""}, "firefox.exe", "Clip - YouTube", False),             # other browser
    ({"source": "firefox.exe", "title": "Other Song", "artist": "Band"}, "firefox.exe", "Clip - YouTube", False),  # S08
    ({"source": "Opaque.App.Id", "title": ""}, "chrome.exe", "Clip - YouTube", False),           # unknown family
])
def test_video_selector_lone_session_needs_positive_identity(info, process, title, expected):
    from jarvis.browser_control import _lone_session_is_tab_identity
    assert _lone_session_is_tab_identity(info, process, title) is expected


@pytest.mark.parametrize("code", ["ru", "en", "de", "fr", "es", "it", "pt"])
def test_quick_video_replies_name_the_video(code):
    from jarvis.ui.quick_media_messages import ROWS, acknowledgement, message
    music_words = {"ru": "музык", "en": "music", "de": "musik", "fr": "musique", "es": "música", "it": "musica", "pt": "música"}
    for key in ("pause_video", "resume_video", "failed_video"):
        text = message(key, code, source="YouTube").casefold()
        assert music_words[code] not in text
    assert music_words[code] not in acknowledgement("pause", "stop video", code, "video").casefold()
    # Music commands keep their music wording.
    assert acknowledgement("pause", "pause", code) == message("heard", code, phrase="pause", task=message("working_pause", code))
    assert all(len(row) == 7 for row in ROWS.values())


@pytest.mark.parametrize("second,action", [("стоп видео", "pause"), ("продолжи видео", "play")])
def test_explicit_video_command_supersedes_provider_choice(tmp_path, second, action):
    with external_io_blocked():
        local = AtlasSession(tmp_path / "choice")
        try:
            local.reset_case("ru")
            first = local.agent.handle("пауза")
            assert local.calls == [] and "Видео YouTube" in first.text
            local.agent.handle(second)
            assert local.calls and local.calls[-1]["tool"] == "media"
            assert local.calls[-1]["arguments"] == {"action": action, "target": "youtube"}
        finally:
            local.finish()
