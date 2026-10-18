"""Regression coverage for false-success launcher, receipts and test orchestration.
All desktop, API and subprocess boundaries are stubbed in main() tests.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest

INTERNAL = Path(__file__).resolve().parents[1]
PROJECT = INTERNAL.parent


def atlas_evidence(directory, code=0):
    summary = {'started': 1, 'planned': 7, 'completed': 7, 'agent_replies_expected': 7,
               'agent_replies_received': 7, 'all_cases_recorded': True,
               'all_expected_agent_replies_received': True, 'stopped': False, 'fatal_error': '',
               'counts': {'ROUTE_PASS': 7} if not code else {'NO_ROUTE': 7},
               'status': 'FINISHED' if code == 0 else 'FINISHED_WITH_FINDINGS', 'exit_code': code}
    directory.mkdir(parents=True, exist_ok=True)
    (directory/'summary.json').write_text(json.dumps(summary), encoding='utf-8')
    archive = directory.parent/(directory.name+'_RESULTS.zip')
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('summary.json', json.dumps(summary))
    return summary, archive


def good_report():
    return {'steps': {'ui': {'status': 'READY'}, 'settings': {'type_review_count': 0},
                     'safe_tests': {'exit_code': 0, 'tests_recorded': 10},
                     'integrations': {'Telegram': 'AUTHENTICATED'},
                     'atlas': {'exit_code': 0}}}


def test_cmd_direct_named_mode_and_no_normal_launcher_fallback():
    cmd = (PROJECT/'NEO_TEST.cmd').read_text()
    assert '-File "%~dp0_internal\\launch.ps1" -Mode test-all' in cmd
    assert 'call "%~dp0NEO.cmd"' not in cmd
    assert '/s /k' in cmd
    assert {p.name for p in PROJECT.glob('*.cmd')} == {'NEO.cmd', 'NEO_TEST.cmd'}


def test_powershell_checks_unbound_args_before_startup_and_verifies_receipt():
    script = (INTERNAL/'launch.ps1').read_text(encoding='utf-8-sig')
    assert script.index('if ($args.Count -gt 0)') < script.index('$stateRoot =')
    assert 'INVALID_LAUNCH_MODE' in script
    branch = script[script.index("if ($Mode -eq '--test-all') {"):]
    assert "'tools\\test_all.py'" in branch
    assert '--invocation-id $runId --receipt $receipt' in branch
    assert '--verify-receipt $receipt' in branch
    assert branch.index('--verify-receipt') < branch.index("if ($Mode) {")


@pytest.mark.parametrize('mode,entry,module', [
    ('--test-all', 'all_main', 'tools.test_all'), ('--atlas-test', 'atlas_main', 'atlas_test_runner'),
    ('--startup-test', 'startup_main', 'tools.startup_test'),
])
def test_python_dispatch_is_not_a_desktop_launch(monkeypatch, mode, entry, module):
    import bootstrap
    target = Mock(return_value=1)
    monkeypatch.setitem(sys.modules, module, SimpleNamespace(main=target))
    launch = Mock(side_effect=AssertionError('Diagnostic must not call normal launch'))
    monkeypatch.setattr(bootstrap, 'launch', launch)
    assert bootstrap.main([mode]) == 1
    target.assert_called_once()
    launch.assert_not_called()


def test_unknown_python_mode_fails_instead_of_launch(monkeypatch):
    import bootstrap
    launch = Mock()
    monkeypatch.setattr(bootstrap, 'launch', launch)
    with pytest.raises(SystemExit) as exc:
        bootstrap.main(['--unknown-test-mode'])
    assert exc.value.code == 2
    launch.assert_not_called()


@pytest.mark.parametrize('change', [
    {'planned': 0, 'completed': 0}, {'completed': 6}, {'agent_replies_received': 6},
    {'all_cases_recorded': False}, {'all_expected_agent_replies_received': False},
    {'status': 'RUNNING'}, {'stopped': True}, {'fatal_error': 'stopped'}, {'exit_code': 2},
])
def test_zero_child_code_does_not_prove_all_atlas_requests(change, tmp_path):
    from jarvis.testing.test_contract151 import atlas_complete
    summary, archive = atlas_evidence(tmp_path/'atlas')
    summary.update(change)
    assert not atlas_complete(summary, 0, archive)


@pytest.mark.parametrize('code', [0, 1])
def test_complete_atlas_with_matching_archive_is_accepted(tmp_path, code):
    from jarvis.testing.test_contract151 import atlas_complete
    summary, archive = atlas_evidence(tmp_path/'atlas', code)
    assert atlas_complete(summary, code, archive)


def test_missing_zip_cannot_be_success(tmp_path):
    from jarvis.testing.test_contract151 import atlas_complete
    summary, archive = atlas_evidence(tmp_path/'atlas')
    archive.unlink()
    assert not atlas_complete(summary, 0, archive)


def test_stale_zip_different_counts_is_rejected(tmp_path):
    from jarvis.testing.test_contract151 import atlas_complete
    summary, archive = atlas_evidence(tmp_path/'atlas')
    summary['counts'] = {'ROUTE_PASS': 6, 'NO_ROUTE': 1}
    assert not atlas_complete(summary, 0, archive)


def test_outcome_requires_all_phases():
    from jarvis.testing.test_contract151 import outcome
    assert outcome({'steps': {'ui': {'status': 'READY'}}}, False)[0] == 2
    assert outcome(good_report(), True) == (0, [])


@pytest.mark.parametrize('step,change,expected', [
    ('safe_tests', {'exit_code': 1, 'tests_recorded': 7}, 1),
    ('safe_tests', {'exit_code': 0, 'tests_recorded': 0}, 2),
    ('safe_tests', {'exit_code': 124, 'tests_recorded': 7}, 2),
    ('ui', {'status': 'OTHER_INSTANCE_NOT_TESTED'}, 1),
    ('integrations', {'Discord': 'TIMEOUT'}, 1), ('integrations', {'Discord': 'NOT_RUN'}, 1),
    ('settings', {'type_review_count': 1}, 1), ('atlas', {'exit_code': 1}, 1),
])
def test_non_successful_phase_not_hidden(step, change, expected):
    from jarvis.testing.test_contract151 import outcome
    report = good_report()
    report['steps'][step] = change
    assert outcome(report, True)[0] == expected


def receipt_fixture(tmp_path, monkeypatch, code=0):
    from tools.test_all import pack
    from jarvis.testing.test_contract151 import write_receipt
    invocation = 'a'*32
    output = tmp_path/'NEO_test_154_example'
    output.mkdir()
    source = tmp_path/'source.py'
    source.write_text('version = 151')
    report = good_report()
    if code:
        report['steps']['atlas']['exit_code'] = 1
    report.update(invocation_id=invocation, exit_code=code, atlas_completion_verified=True,
                  status='FINISHED' if code == 0 else 'FINISHED_WITH_FINDINGS')
    archive = pack(output, report, None)
    receipt = write_receipt(tmp_path, invocation, archive, code, source)
    return invocation, archive, receipt, source


@pytest.mark.parametrize('code', [0, 1])
def test_valid_receipt_preserves_findings_exitcode(tmp_path, monkeypatch, code):
    from jarvis.testing.test_contract151 import verify_receipt
    invocation, archive, receipt, source = receipt_fixture(tmp_path, monkeypatch, code)
    assert verify_receipt(tmp_path, invocation, receipt, code, source) == code


@pytest.mark.parametrize('fault', ['absent', 'different_id', 'tampered_zip', 'changed_source', 'wrong_exit', 'path_traversal', 'false_phase_success'])
def test_bad_receipt_never_accepts_zero(tmp_path, monkeypatch, fault):
    from jarvis.testing.test_contract151 import verify_receipt
    invocation, archive, receipt, source = receipt_fixture(tmp_path, monkeypatch)
    if fault == 'absent': receipt.unlink()
    if fault == 'different_id': invocation = 'b'*32
    if fault == 'tampered_zip': archive.write_bytes(b'broken')
    if fault == 'changed_source': source.write_text('version = 152')
    if fault == 'path_traversal':
        data = json.loads(receipt.read_text());data['archive_name']='../settings.json'
        receipt.write_text(json.dumps(data))
    if fault == 'false_phase_success':
        # Even a digest-matched receipt cannot bless GUI-only evidence.
        import hashlib
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('reports/report.json', json.dumps({'invocation_id':invocation, 'exit_code':0, 'steps':{'ui':{'status':'READY'}}}))
        data=json.loads(receipt.read_text());data['archive_sha256']=hashlib.sha256(archive.read_bytes()).hexdigest()
        receipt.write_text(json.dumps(data))
    assert verify_receipt(tmp_path, invocation, receipt, 1 if fault=='wrong_exit' else 0, source) == 2


@pytest.mark.parametrize('fault,expected', [('none',0),('atlas_silent_zero',2),('safe_silent_zero',2),('api_unavailable',1),('sampler_failure',2),('atlas_findings',1)])
def test_main_executes_all_stages_and_always_packs(tmp_path, monkeypatch, capsys, fault, expected):
    import bootstrap
    from tools import test_all as tool
    from jarvis.testing import resource_sample150, integration_probes150
    from jarvis.testing.test_contract151 import verify_receipt
    cfg = SimpleNamespace(get=lambda key, default=None: default)
    monkeypatch.setattr(bootstrap, 'DATA', tmp_path)
    monkeypatch.setattr(bootstrap, 'ReadOnlyConfig', lambda: cfg)
    ui=Mock(return_value={'status':'READY'})
    monkeypatch.setattr(tool, 'check_ui', ui)
    monkeypatch.setattr(tool, 'settings_inventory', lambda _: {'declared_count':1, 'rows':[{'key':'safe','status':'TYPE_OK'}]})
    sampler=Mock();sampler.finish.return_value={'status':'COMPLETE'}
    if fault=='sampler_failure':sampler.finish.side_effect=RuntimeError('sample failure')
    monkeypatch.setattr(resource_sample150, 'Sampler', lambda _: sampler)
    monkeypatch.setattr(integration_probes150, 'targets', lambda _: [('Telegram', None, None)])
    dispatched=[]
    def helper(cmd, timeout, env=None, **kw):
        dispatched.append(list(cmd))
        if any(str(x).endswith('safe_test_runner.py') for x in cmd):
            if fault!='safe_silent_zero':
                path=Path(env['LOCALAPPDATA'])/'NeoJarvis/diagnostics/test_results_latest.json'
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps({'exit_code':0,'tests':[{'test':'safe','status':'PASS'}]}))
            return {'exit_code':0,'seconds':0,'output':'safe completed'}
        if 'jarvis.testing.integration_probes150' in cmd:
            status='TIMEOUT' if fault=='api_unavailable' else 'AUTHENTICATED'
            return {'exit_code':0,'seconds':0,'output':json.dumps({'provider':'Telegram','status':status})}
        assert any(str(x).endswith('atlas_all_test.py') for x in cmd)
        assert '--headless' in cmd
        directory=Path(cmd[cmd.index('--output')+1])
        code=1 if fault=='atlas_findings' else 0
        if fault!='atlas_silent_zero':atlas_evidence(directory, code)
        return {'exit_code':code,'seconds':0,'output':'atlas completed'}
    monkeypatch.setattr(tool,'run_helper',helper)
    from jarvis.testing import build_integrity156
    monkeypatch.setattr(build_integrity156, 'verify_release', lambda *a, **kw: {'status': 'PASS'})
    ident='c'*32
    assert tool.main(['--invocation-id',ident]) == expected
    output=capsys.readouterr().out
    assert 'UNIFIED_TEST_STARTED='+ident in output
    for i in range(1,6):assert f'[{i}/5]' in output  # NEO 179: no voice phase
    assert output.count('SEND_THIS_ZIP=') == 1
    assert len(dispatched)==3
    ui.assert_called_once();sampler.start.assert_called_once()
    diagnostics=tmp_path/'diagnostics'
    receipt=diagnostics/'test_receipts'/(ident+'.json')
    assert verify_receipt(diagnostics,ident,receipt,expected,Path(tool.__file__)) == expected
    archive_path=next(diagnostics.glob('*_RESULTS.zip'))
    with zipfile.ZipFile(archive_path) as z:
        report=json.loads(z.read('reports/report.json'))
        assert report['exit_code']==expected
        assert 'reports/safe_tests_summary.json' in z.namelist()
        assert not any('settings.json' in name or '.wav' in name or 'sqlite' in name for name in z.namelist())


@pytest.mark.skipif(shutil.which('powershell.exe') is None and shutil.which('pwsh') is None, reason='PowerShell runtime not present')
def test_real_powershell_mode_binding_in_temporary_harness(tmp_path):
    # Only the argument parser is executed: no launcher, network, keys or GUI.
    shell=shutil.which('powershell.exe') or shutil.which('pwsh')
    text=(INTERNAL/'launch.ps1').read_text(encoding='utf-8-sig')
    preamble=text[:text.index('$internalRoot =')]
    path=tmp_path/'mode_harness.ps1'
    path.write_text(preamble+'\nWrite-Output ("RESULT_MODE="+$Mode)\nexit 0\n',encoding='utf-8-sig')
    for args in (['-Mode','test-all'],['--test-all'],['-Mode','--test-all']):
        result=subprocess.run([shell,'-NoLogo','-NoProfile','-File',str(path),*args],capture_output=True,text=True,timeout=20)
        assert result.returncode==0,(args,result.stdout,result.stderr)
        assert 'RESULT_MODE=--test-all' in result.stdout
    result=subprocess.run([shell,'-NoLogo','-NoProfile','-File',str(path),'--unknown-test'],capture_output=True,text=True,timeout=20)
    assert result.returncode==2
    assert 'RESULT_MODE' not in result.stdout
