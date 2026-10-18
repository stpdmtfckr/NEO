"""Packaging and truthful typed-clarification diagnostics for release 157."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.testing.atlas_contracts149 import evaluate, pending_call
from jarvis.testing.build_integrity156 import source_snapshot, verify_release
from jarvis.testing.release_version import release_number
from jarvis.testing.test_contract151 import outcome


def test_source_snapshots_do_not_require_additional_product_files(tmp_path):
    internal = tmp_path / '_internal'
    internal.mkdir()
    (internal / 'VERSION.txt').write_text('NEO v4.0.20.157-VOICE-MEDIA-CONTEXT\n')
    source = internal / 'example.py'
    source.write_text('version = 1\n')
    before = source_snapshot(tmp_path)
    assert verify_release(tmp_path)['status'] == 'NOT_PRESENT'
    assert source_snapshot(tmp_path) == before
    source.write_text('version = 2\n')
    assert source_snapshot(tmp_path) != before
    assert not list(tmp_path.glob('*.json'))


@pytest.mark.parametrize(('label', 'expected'), [
    ('NEO v4.0.20.157-VOICE-MEDIA-CONTEXT\n', '157'),
    ('NEO v4.0.20.158\nold history', '158'),
    ('', 'unknown'), ('NEO v4.0.20.157/../other', 'unknown'),
])
def test_diagnostic_release_number_comes_from_single_version_file(tmp_path, label, expected):
    (tmp_path / 'VERSION.txt').write_text(label)
    assert release_number(tmp_path) == expected


def test_version_number_is_shared_by_both_report_runners():
    from tools import atlas_all_test, test_all
    assert test_all.RELEASE == release_number()
    assert atlas_all_test.VERSION == f'ATLAS-ALL-{release_number()}.1'


def test_no_manifest_snapshot_is_not_misreported_as_failure_or_release_verification():
    report = {'steps': {'ui': {'status': 'READY'}, 'settings': {},
                       'safe_tests': {'exit_code': 0, 'tests_recorded': 1},
                       'integrations': {},
                       'atlas': {'exit_code': 0},
                       'build_integrity': {'status': 'BASELINE_RECORDED', 'packaged_reference_checked': False}}}
    assert outcome(report, True) == (0, [])
    report['steps']['build_integrity']['status'] = 'MISMATCH'
    assert outcome(report, True) == (1, ['build_integrity:MISMATCH'])


@pytest.mark.parametrize('kind', ['', 'clarification'])
@pytest.mark.parametrize(('pending', 'shell', 'expected_status'), [
    (('filesystem', 'open'), None, 'correct'),
    (('browser', 'search'), None, 'REACTION_MISMATCH'),
    (None, 'open', 'REACTION_MISMATCH'),
    (None, None, 'NO_ROUTE'),
])
def test_unrelated_question_does_not_count_as_correct_routing(kind, pending, shell, expected_status):
    state = SimpleNamespace(pending_intent=(SimpleNamespace(tool=pending[0], arguments={'action': pending[1]})
                                            if pending else None), pending_shell=shell)
    case = {'expected_tool': 'filesystem', 'expected_action': 'open', 'expected_kind': kind}
    expected = {'tool': 'filesystem', 'arguments': {'action': 'open'}}
    turn = SimpleNamespace(pending=None, text='Which object?')
    status, _, _ = evaluate(case, turn, [], [], state, None, expected)
    if expected_status == 'correct':
        expected_status = 'CLARIFICATION_PASS' if kind else 'NEEDS_INPUT'
    elif kind and expected_status == 'NO_ROUTE':
        expected_status = 'REACTION_MISMATCH'
    assert status == expected_status


def test_application_choice_proves_only_application_open():
    state = SimpleNamespace(pending_intent=None, pending_shell=None)
    menu = {'kind': 'app', 'options': [{'label': 'Example App'}]}
    assert pending_call(state, menu) == {'tool': 'application', 'arguments': {'action': 'open', 'kind': 'app'}}
    turn = SimpleNamespace(pending=None, text='Choose an application')
    case = {'expected_tool': 'filesystem', 'expected_action': 'open'}
    assert evaluate(case, turn, [], [], state, menu, {'tool': 'filesystem', 'arguments': {'action': 'open'}})[0] == 'REACTION_MISMATCH'


def test_only_existing_main_and_test_cmd_are_shipped():
    root = Path(__file__).resolve().parents[2]
    assert {p.name for p in root.glob('*.cmd')} == {'NEO.cmd', 'NEO_TEST.cmd'}
    assert not list(root.glob('ПРОЧИТАЙ*.txt'))
    assert not (root / 'RELEASE_MANIFEST.json').exists()


@pytest.mark.parametrize(('tool', 'arguments'), [
    ('translate', {'text': 'Hello', 'target_language': 'ru'}),
    ('powershell', {'script': 'Write-Output NEO_QA_157'}),
])
def test_actionless_tools_still_check_tool_and_literal_arguments(tool, arguments):
    from jarvis.testing.atlas_contracts149 import render
    case = {'expected_tool': tool, 'expected_action': '', 'phrase': 'test', 'expected_args': arguments}
    _, _, expected, _ = render(case, Path('/owned'))
    state = SimpleNamespace(pending_intent=None, pending_shell=None)
    turn = SimpleNamespace(pending=None, text='reviewed')
    assert evaluate(case, turn, [{'tool': tool, 'arguments': arguments}], [], state, None, expected)[0] == 'ARGUMENTS_PASS'
    changed = {key: 'wrong' for key in arguments}
    assert evaluate(case, turn, [{'tool': tool, 'arguments': changed}], [], state, None, expected)[0] == 'ARGUMENT_MISMATCH'


def test_explicit_explorer_fixture_restores_snapshot_and_never_closes_real_window(tmp_path, monkeypatch):
    from dataclasses import asdict
    from unittest.mock import Mock
    from jarvis.runtime import folder_context156
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    from tools.atlas_all_test import Phrase, check_phrase
    windows = [{'hwnd': 157, 'pid': 157, 'created': 1., 'title': 'NEO QA Folder'}]
    real_snapshot = folder_context156.snapshot
    close = Mock(side_effect=AssertionError('No real window closure is permitted'))
    monkeypatch.setattr(folder_context156, 'close_selected', close)
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'folder-atlas')
        session.config.data['security']['confirm_medium_risk'] = False
        try:
            case = Phrase('ru', 'закрой папку', 'desktop', 'close_folder',
                          expected_args={'query': folder_context156.encode(windows[0])}, folder_windows_fixture=windows)
            result = check_phrase(session, asdict(case))
            assert result['status'] == 'ARGUMENTS_PASS', result
            assert result['execution'] == 'SIMULATED_EXPLORER_SNAPSHOT_ACTION_BLOCKED'
            assert folder_context156.snapshot is real_snapshot
            close.assert_not_called()
        finally:
            session.finish()
