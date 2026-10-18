"""Conversation-local offline variety: complete bags, language and data integrity."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import random

import pytest

from jarvis.runtime import social_atlas as social
from jarvis.runtime.natural_commands import LANGUAGES


def _utterance(code, family):
    return next(phrase for phrase, (_, current) in social.catalog(code).items() if current == family)


@pytest.mark.parametrize("code", LANGUAGES)
def test_every_family_completes_each_bag_and_never_repeats_at_boundary(code):
    atlas = social.SocialAtlas(rng=random.Random(145))
    for family in social._RESPONSES:
        options = social.response_options(family, code)
        phrase = _utterance(code, family)
        results = [atlas.reply(phrase, code, session="phone:42") for _ in range(len(options) * 4)]
        for start in range(0, len(results), len(options)):
            assert set(results[start:start+len(options)]) == set(options), family
        assert all(a != b for a, b in zip(results, results[1:])), family


def test_aliases_share_a_bag_but_languages_and_sessions_do_not():
    atlas = social.SocialAtlas(rng=random.Random(42))
    options = social.response_options("thanks", "en")
    phrases = [p for p, (_, family) in social.catalog("en").items() if family == "thanks"]
    results = [atlas.reply(phrases[i % len(phrases)], "en", session="chat1") for i in range(len(options))]
    assert len(set(results)) == len(options)
    atlas.reply(phrases[0], "en", session="chat2")
    atlas.reply(_utterance("de", "thanks"), "de", session="chat1")
    assert len(atlas._bags) == 3
    assert len(atlas._bags[("chat1", "en", "thanks")][1]) == 0
    assert len(atlas._bags[("chat2", "en", "thanks")][1]) == len(options) - 1


def test_bags_are_randomized_bounded_and_safe_for_simultaneous_callers():
    atlas = social.SocialAtlas(rng=random.Random(17))
    phrase = _utterance("en", "thanks")
    options = social.response_options("thanks", "en")
    with ThreadPoolExecutor(max_workers=6) as pool:
        answers = list(pool.map(lambda _: atlas.reply(phrase, "en", session="same"), range(len(options))))
    assert len(set(answers)) == len(options)
    for i in range(600):
        atlas.reply(phrase, "en", session=str(i))
    assert len(atlas._bags) == 512
    assert ("same", "en", "thanks") not in atlas._bags
    if len(options) >= 6:
        second = social.SocialAtlas(rng=random.Random(91))
        assert [second.reply(phrase, "en") for _ in options] != answers


@pytest.mark.parametrize("style", ("friendly", "business", "military", "street"))
@pytest.mark.parametrize("code", LANGUAGES)
def test_styles_keep_variation_and_do_not_change_commands(style, code):
    atlas = social.SocialAtlas(rng=random.Random(12))
    phrase = _utterance(code, "presence")
    answers = [atlas.reply(phrase, code, style=style) for _ in range(12)]
    assert all(a and a != b for a,b in zip(answers, answers[1:]))
    assert not any("boss" in answer.casefold() or "босс" in answer.casefold() for answer in answers)
    for text in ("hello open Steam", "привет удали файл", "open application Steam", "what song is playing"):
        assert atlas.reply(text, code, style=style) is None


def test_loader_keeps_base_responses_when_additions_are_malformed(monkeypatch):
    # Isolate the legacy additions loader from the independently tested 149 pack.
    monkeypatch.setattr(social, "dialogue_additions", lambda: ())
    monkeypatch.setattr(social, "_response_additions", lambda: {"en": {"hello": [None, 9, "", "x"*1201, "A new hello.", "A new hello."]}})
    social.response_options.cache_clear()
    try:
        result = social.response_options("hello", "en")
        assert result == (*social._RESPONSES["hello"][1], "A new hello.")
    finally:
        social.response_options.cache_clear()


def test_shipped_catalogue_contains_four_reviewed_additions_for_every_family_and_language():
    path = Path(social.__file__).resolve().parents[2] / "config" / "social_responses.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert set(data["languages"]) == set(LANGUAGES)
    for index, language in enumerate(LANGUAGES):
        rows = data["languages"][language]
        assert set(rows) == set(social._RESPONSES)
        for family, additions in rows.items():
            assert len(additions) >= 4
            assert all(isinstance(text, str) and 0 < len(text.strip()) <= 1200 for text in additions)
            full = (*social._RESPONSES[family][index], *additions)
            assert len(full) == len(set(full)) >= 6, (language, family)
