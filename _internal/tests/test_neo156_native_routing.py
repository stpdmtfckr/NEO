"""Production-Agent regressions from the user's 154 phrase test report."""
from dataclasses import asdict
from pathlib import Path
import json

import pytest

from jarvis.runtime.command_contracts150 import complete, partial
from jarvis.runtime.natural_commands import LANGUAGES
from jarvis.testing.atlas_contracts149 import prepare_fixtures
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked
from tools.atlas_all_test import Phrase, check_phrase

INTERNAL = Path(__file__).resolve().parents[1]
CASES = json.loads((INTERNAL / 'config/regression156_routes.json').read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        value = AtlasSession(tmp_path_factory.mktemp('native156') / 'run')
        prepare_fixtures(value)
        yield value
        value.finish()


@pytest.mark.parametrize('case', [r for r in CASES if r['source'] == 'short156'],
                         ids=lambda r: r['locale'] + ':' + r['phrase'])
def test_compact_domain_requests_run_the_named_read(session, case):
    session.reset_case(case['locale'])
    result = check_phrase(session, case)
    assert result['status'] == 'ROUTE_PASS', result
    assert len(result['calls']) == 1


def _provider_heads():
    # One previously misrouted surface for each provider/action and language.
    found = {}
    for row in CASES:
        if row['source'] == 'feedback156_missing_provider':
            found.setdefault((row['locale'], row['expected_tool'], row['expected_action']), row)
    return list(found.values())


@pytest.mark.parametrize('case', _provider_heads(), ids=lambda r: r['locale'] + ':' + r['phrase'])
def test_missing_music_title_asks_then_preserves_literal_answer(session, case):
    session.reset_case(case['locale'])
    result = check_phrase(session, case)
    assert result['status'] == 'CLARIFICATION_PASS', result
    assert not result['calls'], result
    field = 'playlist' if case['expected_action'] == 'playlist' else 'query'
    payload = 'https://www.youtube.com/watch?v=NEO_QA_156' if case['expected_action'] == 'music_youtube_url' else 'No Surprises — NEO QA'
    continuation = asdict(Phrase(case['locale'], payload, case['expected_tool'], case['expected_action'],
                                keep_context=True, expected_args={field: payload}))
    result = check_phrase(session, continuation)
    assert result['status'] == 'ARGUMENTS_PASS', result


@pytest.mark.parametrize('text', ['status', 'show status', 'покажи', 'список', 'öffne', 'ouvre'])
def test_bare_ambiguous_fragments_are_not_complete_reads(text):
    for code in LANGUAGES:
        assert complete(text, code) is None


def test_new_short_catalogue_does_not_reassign_existing_contracts():
    from jarvis.runtime.command_contracts150 import _short_index, _target
    from jarvis.runtime.natural_commands import norm
    for version in (153, 155):
        data = json.loads((INTERNAL / 'config' / f'short_commands{version}.json').read_text(encoding='utf-8'))
        for code, phrases in data['languages'].items():
            for text, target in phrases.items():
                assert _target(*_short_index(code)[norm(text)]) == _target(*target)


@pytest.mark.parametrize('text,code', [
    ('не показывай состояние компьютера', 'ru'),
    ('do not show computer status', 'en'),
    ('zeige den Computerstatus nicht', 'de'),
    ('ne montre pas l’état de l’ordinateur', 'fr'),
    ('no muestra el estado del ordenador', 'es'),
    ('non mostrare lo stato del computer', 'it'),
    ('não mostre o estado do computador', 'pt'),
])
def test_negative_requests_do_not_become_exact_reads(text, code):
    assert complete(text, code) is None


@pytest.mark.parametrize('code', LANGUAGES)
def test_named_titles_are_not_interpreted_as_missing_placeholders(code):
    # Slot extraction is exercised through actual production patterns; native
    # titles, quotes and explicit negation inside the title remain user data.
    utterances = {
        'ru': 'включи плейлист "No Surprises" в Яндекс Музыке',
        'en': 'play the playlist "No Surprises" in Yandex Music',
        'de': 'spiele die Playlist "No Surprises" in Yandex Music',
        'fr': 'joue la playlist "No Surprises" dans Yandex Music',
        'es': 'reproduce la playlist "No Surprises" en Yandex Music',
        'it': 'riproduci la playlist "No Surprises" in Yandex Music',
        'pt': 'reproduza a playlist "No Surprises" no Yandex Music',
    }
    assert partial(utterances[code], code) is None
    assert complete(utterances[code], code) == ('yandex_music', {'action': 'playlist', 'playlist': 'No Surprises'})
