from jarvis.runtime.speech_understanding import SpeechUnderstandingEngine, _Entity
from tests.test_speech_understanding import Config, engine


def test_all_localized_app_verbs_keep_entity_slot():
    obj = SpeechUnderstandingEngine(Config())
    obj._snapshot_at = 10**12
    obj._entities = [_Entity("Telegram", ("telegam", "telegram"), "builtin", kind="application")]
    for language, verb in (("de", "öffne"), ("fr", "ouvre"), ("es", "abre"), ("it", "apri"), ("pt", "abra")):
        assert obj.resolve(f"{verb} telegam", language=language, record=False).text == f"{verb} Telegram"


def test_localized_music_payload_remains_verbatim():
    obj = SpeechUnderstandingEngine(Config())
    obj._refresh_entities = lambda: [_Entity("Telegram", ("telegam",), "builtin", kind="application")]
    for language, text in (("de", "spiele telegam"), ("fr", "joue telegam"), ("es", "reproduce telegam"), ("it", "riproduci telegam"), ("pt", "reproduza telegam")):
        assert obj.resolve(text, language=language, record=False).text == text


def test_poor_acoustic_alternate_cannot_override_primary():
    result = engine().resolve("открой с ним", alternatives=[{"text": "открой стим", "score": -1.2, "source": "same_language_rescue"}], language="ru", record=False)
    assert result.text == "открой с ним"


def test_known_primary_application_cannot_be_changed_by_rescue():
    result = engine().resolve("открой Telegram", alternatives=[{"text": "открой стим", "score": -0.1, "source": "same_language_rescue"}], language="ru", record=False)
    assert result.text == "открой Telegram"


def test_adaptive_rewrite_stays_disabled_by_default():
    class Adaptive:
        def resolve(self, *args, **kwargs):
            raise AssertionError("Disabled rewriting must not invoke adaptive resolver")
    agent = type("Agent", (), {"adaptation": Adaptive()})()
    obj = SpeechUnderstandingEngine(Config(), agent=agent)
    obj._refresh_entities = lambda: []
    assert obj.resolve("открой брак", language="ru", record=False).text == "открой брак"
