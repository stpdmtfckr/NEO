"""NEO 187 HREQ-013: shipped wake forms with case/punctuation, and ordinary
speech that must stay silent. (Behaviour already present in 186; this pins it.)"""
import json
from pathlib import Path

import pytest

from jarvis.wake_matcher import match_wake_phrase

_WAKE = json.loads((Path(__file__).resolve().parents[1] / "config" / "default_settings.json").read_text(encoding="utf-8"))["wake_word"]


def _match(text):
    return match_wake_phrase(text, _WAKE["phrase"], aliases=_WAKE["aliases"])


@pytest.mark.parametrize("text", ["нел", "нел!", "Нел.", "нел?", "Ну...", "Наю!", "НЕЛО", "нЕлО", "НЕО", "Нео!!!",
                                  "Neo", "NEO?", "нео…"])
def test_standalone_wake_forms(text):
    assert _match(text)[0]


@pytest.mark.parametrize("text,command", [("нео, открой браузер", "открой браузер"), ("Нел, открой браузер", "открой браузер"),
                                          ("НЕЛО включи музыку", "включи музыку"), ("Наю! открой браузер", "открой браузер"),
                                          ("НеО, привет", "привет")])
def test_wake_with_command(text, command):
    hit, rest, _score = _match(text)
    assert hit and rest == command


@pytest.mark.parametrize("text", ["ну и что", "ну давай", "ну, пошли", "нелегко было", "наушники", "новости", "нелли", "наюшка"])
def test_ordinary_speech_is_not_a_wake(text):
    assert not _match(text)[0]
