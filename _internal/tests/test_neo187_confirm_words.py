"""NEO 187 HREQ-014: consent phrases, negations and mixed replies."""
import pytest

from jarvis.runtime.confirm_words187 import NO, UNCLEAR, YES, classify


@pytest.mark.parametrize("code,text", [
    ("ru", "да да"), ("ru", "Да, верно."), ("ru", "это то, что я имел в виду"), ("ru", "Это то, что я имела в виду!"),
    ("ru", "да, именно"), ("ru", "так точно"), ("ru", "да да да"), ("ru", "всё верно"),
    ("en", "yes yes"), ("en", "yes, that's right"), ("en", "that's what I meant"), ("en", "exactly"),
    ("de", "ja genau"), ("de", "das meinte ich"), ("fr", "oui, c'est ça"), ("fr", "c'est ce que je voulais dire"),
    ("es", "sí, eso es"), ("es", "a eso me refería"), ("it", "sì, esatto"), ("it", "è quello che intendevo"),
    ("pt", "sim, isso mesmo"), ("pt", "era isso que eu queria dizer"),
])
def test_consent(code, text):
    assert classify(text, code) == YES


@pytest.mark.parametrize("code,text", [
    ("ru", "нет, не то"), ("ru", "это не то, что я имел в виду"), ("ru", "нет нет"), ("ru", "не надо"),
    ("en", "no, that's not what I meant"), ("de", "nein, nicht das"), ("fr", "non, pas ça"),
    ("es", "no, eso no"), ("it", "no, non quello"), ("pt", "não, não era isso"),
])
def test_negation_is_never_consent(code, text):
    assert classify(text, code) in {NO, None}
    assert classify(text, code) != YES


@pytest.mark.parametrize("text", ["да нет", "да нет, наверное", "не знаю", "ну не знаю", "maybe", "sì no", "talvez"])
def test_mixed_or_unsure_reply_is_unclear(text):
    assert classify(text) == UNCLEAR


def test_longer_requests_are_left_to_their_owners():
    assert classify("да, найди рецепт борща") is None
    assert classify("установи модель") is None


def test_correction_question_accepts_meaning_reply():
    from jarvis.runtime.dialogue_tasks import Slot, _validate
    slot = Slot('_confirm', 'confirm', 'confirm_correction', 'открой загрузки')
    assert _validate(slot, "это то, что я имел в виду", "ru") is True
    assert _validate(slot, "да, верно", "ru") is True
    assert _validate(slot, "да нет", "ru") is None
    assert _validate(slot, "нет, не то", "ru") is None


def test_obs_live_confirmation_reply():
    from jarvis.runtime.streaming_commands184 import confirmation_reply
    assert confirmation_reply("да да", "ru") is True
    assert confirmation_reply("да, верно", "ru") is True
    assert confirmation_reply("нет, не надо", "ru") is False
    assert confirmation_reply("да нет", "ru") is None
    assert confirmation_reply("yes, exactly", "en") is True


def test_legacy_offer_patterns_do_not_take_mixed_reply_as_yes():
    from jarvis.runtime import everyday172, screen_help172, web_answers171
    for module in (everyday172, screen_help172, web_answers171):
        assert not module._YES.match("да нет")
        assert module._YES.match("да верно")
    assert everyday172._YES.match("да запомни")  # older own rule kept
    assert screen_help172._YES.match("ищи")
