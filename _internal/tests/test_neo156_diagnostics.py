"""Release-copy and asset-download regressions observed in the user's logs."""
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from jarvis.testing import build_integrity156 as integrity
from jarvis.testing.test_contract151 import outcome


def release(tmp_path):
    (tmp_path / '_internal/config').mkdir(parents=True)
    (tmp_path / '_internal/VERSION.txt').write_text('NEO v4.0.20.156\n')
    (tmp_path / '_internal/app.py').write_text('print("source")\n')
    (tmp_path / '_internal/config/dialogue.json').write_text('{"answers": []}\n')
    (tmp_path / 'NEO.cmd').write_text('@echo off\n')
    integrity.write_manifest(tmp_path)
    return tmp_path


def test_release_complete_copy_and_profile_exclusion(tmp_path):
    root = release(tmp_path)
    assert integrity.verify_release(root)['status'] == 'PASS'
    (root / 'profile.json').write_text('{"token":"private"}')
    (root / '_internal/__pycache__').mkdir()
    (root / '_internal/__pycache__/cache.py').write_text('cache')
    assert integrity.verify_release(root)['status'] == 'PASS'
    assert all('private' not in str(v) for v in integrity.source_snapshot(root).values())


@pytest.mark.parametrize('fault', ['changed', 'missing', 'unexpected'])
def test_release_identifies_mixed_source_and_catalog(tmp_path, fault):
    root = release(tmp_path)
    source = root / '_internal/config/dialogue.json'
    if fault == 'changed':
        source.write_text('{"responses": []}\n')
    elif fault == 'missing':
        source.unlink()
    else:
        (root / '_internal/extra.py').write_text('new_code = True')
    result = integrity.verify_release(root)
    assert result['status'] == 'MISMATCH' and len(result[fault]) == 1


@pytest.mark.parametrize('name', ['../profile.json', '/private.json', r'_internal\app.py', 'C:/private.json'])
def test_release_invalid_manifest_paths_rejected(tmp_path, name):
    root = release(tmp_path)
    path = root / integrity.MANIFEST
    data = json.loads(path.read_text())
    data['files'][name] = '0' * 64
    path.write_text(json.dumps(data))
    assert integrity.verify_release(root)['status'] == 'INVALID'


def test_missing_manifest_cannot_be_successful_diagnostics():
    report = {'steps': {'ui': {'status': 'READY'}, 'settings': {},
                       'safe_tests': {'exit_code': 0, 'tests_recorded': 1},
                       'integrations': {},
                       'atlas': {'exit_code': 0}, 'build_integrity': {'status': 'NOT_PRESENT'}}}
    assert outcome(report, True) == (1, ['build_integrity:NOT_PRESENT'])


@pytest.mark.parametrize('code', ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt'))
def test_full_atlas_context_pairs_use_production_agent(tmp_path, code):
    from collections import Counter, defaultdict
    from dataclasses import asdict
    from tools.atlas_all_test import dialogue_followup_cases, check_phrase, load_cases
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.atlas_contracts149 import prepare_fixtures
    from jarvis.testing.isolation import external_io_blocked
    internal = Path(__file__).resolve().parents[1]
    pairs = list(dialogue_followup_cases(internal, code))
    # Keep the original six/eight branches as well as the reviewed 160 pack.
    # Exact per-source counts detect a silently dropped old pack, duplicated
    # openings, or a locale missing its new contextual replies.
    expected_branches = {'context155': 6, 'context156': 8,
                         'context160': 14 if code == 'ru' else 9}
    assert Counter(row.source for row in pairs) == {
        source: count * 2 for source, count in expected_branches.items()}
    assert Counter(row.source for row in pairs if row.keep_context) == expected_branches
    scenarios = defaultdict(list)
    for row in pairs:
        assert row.locale == code
        scenarios[row.scenario_id].append(row)
    assert len(scenarios) == sum(expected_branches.values())
    for opening, followup in scenarios.values():
        assert opening.source == followup.source
        assert not opening.keep_context and followup.keep_context
        assert opening.expected_social_topic and followup.expected_social_topic
        assert followup.expected_responses
    complete = list(load_cases(internal, code))
    assert all(row in complete for row in pairs)
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'run')
        prepare_fixtures(session)
        session.reset_case(code)
        try:
            for row in pairs:
                result = check_phrase(session, asdict(row))
                assert result['status'] == ('CONTEXT_RESPONSE_PASS' if row.keep_context else 'DIALOGUE_RESPONSE'), result
                assert result['observed_social_topic'] == row.expected_social_topic
                assert not result['calls'] and result['reply_received']
            wrong_contract = asdict(pairs[-1])
            wrong_contract['expected_responses'] = ['__not_a_reviewed_answer__']
            assert check_phrase(session, wrong_contract)['status'] == 'DIALOGUE_MISMATCH'
        finally:
            session.finish()
