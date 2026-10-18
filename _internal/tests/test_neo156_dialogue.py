"""Bounded dialogue data, state and old 155 schema regression.

These tests exercise native payloads and the real SocialAtlas. The all-language
Agent route tests live in test_neo149_atlas_dialogue and load this pack as well.
"""
import copy
import json
from pathlib import Path
import random

import pytest

from jarvis.runtime import social_atlas as social
from jarvis.runtime.natural_commands import LANGUAGES

ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((ROOT / 'config/social_dialogue156.json').read_text(encoding='utf-8'))
ROWS = [(code, phrase, row) for code, rows in DATA['followups'].items() for phrase, row in rows.items()]


def intro(code, family):
    return next(group['phrases'][code][0] for group in DATA['groups'] if group['family'] == family)


@pytest.mark.parametrize('name', social._DIALOGUE_FILES)
def test_each_shipped_pack_is_complete_in_every_native_language(name):
    payload = json.loads((ROOT / 'config' / name).read_text(encoding='utf-8'))
    normalized = social.validate_dialogue_catalogue(payload, source=name)
    assert len(normalized['groups']) == len(payload['groups'])
    for group in normalized['groups']:
        assert set(group['answers']) == set(LANGUAGES)
        assert len(social.response_options(group['family'], 'ru')) >= 2


def test_early_155_responses_schema_does_not_raise_answers_key_error(monkeypatch):
    legacy = copy.deepcopy(json.loads((ROOT / 'config/social_dialogue155.json').read_text(encoding='utf-8')))
    for group in legacy['groups']:
        group['responses'] = group.pop('answers')
        group.pop('replace_responses', None)
    normalized = social.validate_dialogue_catalogue(legacy, source='legacy155')
    monkeypatch.setattr(social, 'dialogue_additions', lambda: normalized['groups'])
    social.response_options.cache_clear()
    try:
        for group in legacy['groups']:
            for code in LANGUAGES:
                assert social.response_options(group['family'], code) == tuple(group['responses'][code])
    finally:
        social.response_options.cache_clear()


@pytest.mark.parametrize('damage', ['answer_key', 'language', 'empty', 'wrong_type', 'followup_scope'])
def test_invalid_pack_rejected_with_diagnostic_before_any_reply(damage):
    broken = copy.deepcopy(DATA)
    if damage == 'answer_key': del broken['groups'][0]['answers']
    if damage == 'language': del broken['groups'][0]['answers']['pt']
    if damage == 'empty': broken['groups'][0]['answers']['ru'] = ['']
    if damage == 'wrong_type': broken['groups'][0]['answers']['ru'] = 'not a list'
    if damage == 'followup_scope': next(iter(broken['followups']['ru'].values()))['requires'] = []
    with pytest.raises(ValueError, match='fixture156'):
        social.validate_dialogue_catalogue(broken, source='fixture156')


@pytest.mark.parametrize('code,phrase,row', ROWS)
def test_new_followups_require_the_right_topic_and_rotate_answers(code, phrase, row):
    atlas = social.SocialAtlas(rng=random.Random(156))
    assert atlas.reply(phrase, code, session='one') is None
    atlas.reply(intro(code, row['requires'][0]), code, session='one')
    assert atlas.reply(phrase, code, session='two') is None
    first = atlas.reply(phrase, code, session='one')
    second = atlas.reply(phrase, code, session='one')
    assert {first, second} == set(row['answers'])
    other = next(group['family'] for group in DATA['groups'] if group['family'] != row['requires'][0])
    atlas.reply(intro(code, other), code, session='wrong')
    assert atlas.reply(phrase, code, session='wrong') is None


@pytest.mark.parametrize('code', LANGUAGES)
def test_courtesy_preserves_topic_without_extending_expiry(monkeypatch, code):
    now = [100.0]
    monkeypatch.setattr(social.time, 'monotonic', lambda: now[0])
    atlas = social.SocialAtlas()
    phrase, row = next(iter(DATA['followups'][code].items()))
    thanks = next(text for text, (_, family) in social.catalog(code).items() if family == 'thanks')
    atlas.reply(intro(code, row['requires'][0]), code)
    now[0] = 150.0
    assert atlas.context_topic(code=code) == row['requires'][0]
    atlas.reply(thanks, code)
    assert atlas.reply(phrase, code) in row['answers']
    now[0] = 200.0
    atlas.reply(intro(code, row['requires'][0]), code)
    now[0] = 310.0
    atlas.reply(thanks, code)
    now[0] = 321.0
    assert atlas.context_topic(code=code) is None
    assert atlas.reply(phrase, code) is None


@pytest.mark.parametrize('code', LANGUAGES)
def test_context_does_not_cross_session_language_or_new_command(code):
    atlas = social.SocialAtlas()
    phrase, row = next(iter(DATA['followups'][code].items()))
    atlas.reply(intro(code, row['requires'][0]), code, session='remote:one')
    assert atlas.reply(phrase, code, session='remote:two') is None
    assert atlas.reply('open folder Downloads', code, session='remote:one') is None
    assert atlas.reply(phrase, code, session='remote:one') is None
    atlas.reply(intro(code, row['requires'][0]), code, session='one')
    atlas.reply(intro(code, row['requires'][0]), code, session='two')
    atlas.clear_context('one')
    assert atlas.reply(phrase, code, session='one') is None
    assert atlas.reply(phrase, code, session='two') in row['answers']


@pytest.mark.parametrize('code', LANGUAGES)
def test_new_conversation_intro_connects_to_reviewed_activity_branch(code):
    atlas = social.SocialAtlas()
    atlas.reply(intro(code, 'conversation156'), code)
    phrase, row = next((p, r) for p, r in social._followups()[code].items() if r['topic'] == 'work')
    assert atlas.reply(phrase, code) in row['answers']


def test_unknown_open_ended_fact_is_not_fabricated_by_social_dialogue():
    atlas = social.SocialAtlas()
    assert atlas.reply('сколько сейчас человек находится на вокзале', 'ru') is None
    assert atlas.reply('who won the match that just ended', 'en') is None
    assert atlas.reply('повтори неизвестное действие на всех файлах', 'ru') is None


@pytest.fixture(scope='module')
def agent_session(tmp_path_factory):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = AtlasSession(tmp_path_factory.mktemp('dialogue156') / 'run')
        yield session
        session.finish()


@pytest.mark.parametrize('code,phrase,row', ROWS)
def test_contextual_followup_reaches_real_agent_without_tool_calls(agent_session, code, phrase, row):
    session = agent_session
    session.reset_case(code)
    first = session.agent.handle(intro(code, row['requires'][0]))
    assert first.text and not first.pending and not session.calls
    second = session.agent.handle(phrase)
    assert session.agent.social_atlas.context_topic(code=code) == row['topic']
    assert second.text in row['answers']
    assert second.pending is None and session.calls == []
