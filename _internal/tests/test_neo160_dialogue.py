"""Real dialogue turns: complete greetings, semantic replies, scoped followups.

External calls are denied: these replies must stay local and must never hide an
explicit command after a greeting. Expected response families are fixed here.
"""
import json
from pathlib import Path
import random
import re

import pytest
from jarvis.runtime import social_atlas as social
from jarvis.runtime.natural_commands import LANGUAGES

DATA = json.loads((Path(__file__).resolve().parents[1] / 'config/social_dialogue160.json').read_text(encoding='utf-8'))
GROUPS = {row['family']: row for row in DATA['groups']}
GREETINGS = {
    'ru': r'^(?:привет|здравствуй)[!.,]', 'en': r'^(?:hi|hello)[!.,]',
    'de': r'^hallo[!.,]', 'fr': r'^(?:salut|bonjour)\s*[!.,]',
    'es': r'^¡?hola[!.,]', 'it': r'^ciao[!.,]', 'pt': r'^olá[!.,]',
}

@pytest.fixture(scope='module')
def agent_session(tmp_path_factory):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = AtlasSession(tmp_path_factory.mktemp('dialogue160') / 'run')
        yield session
        session.finish()


@pytest.mark.parametrize('code', LANGUAGES)
@pytest.mark.parametrize('style', ['friendly', 'business', 'military', 'street'])
def test_every_greeting_starts_with_native_salutation_in_every_style(code, style):
    atlas = social.SocialAtlas(rng=random.Random(160))
    phrase = GROUPS['hello']['phrases'][code][0]
    options = social.response_options('hello', code)
    replies = [atlas.reply(phrase, code, style=style) for _ in options]
    assert len(set(replies)) == len(options)
    for reply in replies:
        assert re.search(GREETINGS[code], reply.casefold()), reply
        assert len(reply.split()) >= 4


@pytest.mark.parametrize('code', LANGUAGES)
@pytest.mark.parametrize('family', ['hello', 'well', 'thanks', 'bye', 'greeting_how160'])
def test_actual_agent_uses_complete_native_family_without_model_or_tools(agent_session, code, family):
    session = agent_session
    session.reset_case(code)
    session.config.data['user_name_enabled'] = False
    phrase = GROUPS[family]['phrases'][code][0]
    options = social.response_options(family, code)
    for _ in range(len(options) * 2):
        turn = session.agent.handle(phrase)
        assert turn.text in options, (code, phrase, turn.text)
        assert turn.pending is None and not session.calls
        if family in {'hello', 'greeting_how160'}:
            assert re.search(GREETINGS[code], turn.text.casefold())
        if family in {'well', 'greeting_how160'}:
            assert '?' in turn.text and len(turn.text.split()) >= 8


@pytest.mark.parametrize('code', LANGUAGES)
def test_personal_name_has_one_greeting_in_actual_agent(agent_session, code):
    session = agent_session
    session.reset_case(code)
    session.config.data.update(user_name_enabled=True, user_name='Alex')
    try:
        for family in ('hello', 'greeting_how160'):
            for _ in social.response_options(family, code):
                turn = session.agent.handle(GROUPS[family]['phrases'][code][0])
                assert turn.text.count('Alex') == 1
                assert re.search(GREETINGS[code], turn.text.casefold())
                assert not re.search(r'Alex\.\s*(?:Привет|Здравствуй|Hi|Hello|Hallo|Salut|Bonjour|¡Hola|Ciao|Olá)\b', turn.text)
                assert not session.calls
    finally:
        session.config.data['user_name_enabled'] = False


@pytest.mark.parametrize('code', LANGUAGES)
def test_recent_three_answers_do_not_echo_across_rotation_boundaries(code):
    atlas = social.SocialAtlas(rng=random.Random(160))
    for family in ('hello', 'well', 'thanks', 'bye'):
        count = len(social.response_options(family, code))
        phrase = GROUPS[family]['phrases'][code][0]
        replies = [atlas.reply(phrase, code) for _ in range(count * 6)]
        for i, answer in enumerate(replies):
            assert answer not in replies[max(0, i-3):i]
        for i in range(0, len(replies), count):
            assert len(set(replies[i:i+count])) == count


@pytest.mark.parametrize('code', LANGUAGES)
def test_how_are_you_answer_has_scoped_natural_continuation(agent_session, code):
    session = agent_session
    for phrase, raw in DATA['followups'][code].items():
        session.reset_case(code)
        atlas = session.agent.social_atlas
        atlas.clear_context()
        assert atlas.reply(phrase, code, session='unrelated') is None
        first = session.agent.handle(GROUPS['well']['phrases'][code][0])
        assert first.text in social.response_options('well', code)
        replies = [session.agent.handle(phrase).text for _ in raw['answers']]
        assert set(replies) == set(raw['answers']), (code, phrase, replies)
        assert not session.calls
        assert atlas.reply(phrase, code, session='unrelated') is None


@pytest.mark.parametrize('code', LANGUAGES)
def test_followup_expires_and_cannot_cross_language_or_command(code, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(social.time, 'monotonic', lambda: now[0])
    atlas = social.SocialAtlas()
    phrase = next(iter(DATA['followups'][code]))
    intro = GROUPS['well']['phrases'][code][0]
    atlas.reply(intro, code)
    assert atlas.context_topic(code=code) == 'well'
    now[0] = 221.0
    assert atlas.reply(phrase, code) is None
    atlas.reply(intro, code)
    assert atlas.reply('open file report.txt', code) is None
    assert atlas.reply(phrase, code) is None


@pytest.mark.parametrize('code,phrase', [
 ('ru', 'привет, открой приложение Steam'),
 ('en', 'hello, open application Steam'),
 ('de', 'hallo, öffne die Anwendung Steam'),
 ('fr', 'bonjour, ouvre l’application Steam'),
 ('es', 'hola, abre la aplicación Steam'),
 ('it', 'ciao, apri l’applicazione Steam'),
 ('pt', 'olá, abre a aplicação Steam'),
])
def test_greeting_does_not_swallow_real_command(agent_session, code, phrase):
    session = agent_session
    session.reset_case(code)
    assert social.SocialAtlas().reply(phrase, code) is None
    session.agent.handle(phrase)
    assert len(session.calls) == 1
    assert session.calls[0]['tool'] == 'application'
    assert session.calls[0]['arguments'] == {'action': 'open', 'name': 'Steam'}


def test_native_russian_reply_quality_replaces_reported_dry_strings():
    hello = social.response_options('hello', 'ru')
    well = social.response_options('well', 'ru')
    assert len(hello) >= 8 and len(well) >= 8
    assert 'Чем займёмся?' not in hello
    assert all(not answer.startswith(('Готов помогать.', 'Всё готово к работе.')) for answer in well)
    # A conversational response is not an unverified system-health report.
    assert all('всё работает' not in answer.casefold() for answer in well)


@pytest.mark.parametrize('code,phrase', [
 ('ru', 'привет пожалуйста'), ('en', 'hello please'), ('de', 'hallo bitte'),
 ('fr', 'bonjour jarvis'), ('es', 'hola jarvis'), ('it', 'ciao jarvis'), ('pt', 'olá jarvis'),
])
def test_legacy_fallback_also_keeps_greeting_without_user_name(code, phrase):
    from jarvis.runtime.addressing import fast_social_reply, address_prefix, AddressContext, merge_social_address160
    reply = fast_social_reply(phrase, code)
    assert reply and re.search(GREETINGS[code], reply.casefold())
    named = merge_social_address160(reply, address_prefix(AddressContext('greeting'), code, 'Alex'))
    assert named.count('Alex') == 1
    assert re.search(GREETINGS[code], named.casefold())
    assert not re.search(r'Alex\.\s*(?:Привет|Hi|Hello|Hallo|Bonjour|Hola|Ciao|Olá)\b', named)


@pytest.mark.parametrize('code', LANGUAGES)
def test_maintained_atlas_checks_160_followup_topics_and_exact_answers(agent_session, code):
    from dataclasses import asdict
    from tests.test_neo149_atlas_dialogue import runner
    internal = Path(__file__).resolve().parents[1]
    agent_session.reset_case(code)
    rows = [row for row in runner.dialogue_followup_cases(internal, code) if row.source == 'context160']
    assert len(rows) == len(DATA['followups'][code]) * 2
    for row in rows:
        result = runner.check_phrase(agent_session, asdict(row))
        assert result['status'] == ('CONTEXT_RESPONSE_PASS' if row.keep_context else 'DIALOGUE_RESPONSE'), result
        assert not result.get('localization_warning')
