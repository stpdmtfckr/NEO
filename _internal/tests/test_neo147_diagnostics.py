"""Diagnostic export/timeout contracts; never starts the live desktop app."""
from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest

from jarvis.runtime.diagnostics import collect_logs, redact, write_report
from jarvis.runtime.http_timeouts import request_timeouts
from tools.startup_test import bundle_reports, read_new_log, network_probe


@pytest.mark.parametrize('duration, expected',[(35,(5,35)),(12,(5,12)),(2.5,(2.5,2.5)),(.1,(.1,.1))])
def test_connect_timeout_short_read_timeout_preserved(duration,expected):
    assert request_timeouts(duration)==expected


@pytest.mark.parametrize('duration',[0,-1,float('nan'),float('inf')])
def test_invalid_timeout_is_not_silently_infinite(duration):
    with pytest.raises(ValueError):request_timeouts(duration)


def test_token_redaction_includes_bare_discord_and_proxy_userinfo():
    token='A'*24+'.'+'b'*6+'.'+'C'*30
    result=redact('RuntimeError: '+token+' https://username:private@proxy.test:443/')
    assert token not in result and 'private' not in result and 'username' not in result
    assert 'proxy.test:443' in result


def test_generic_collect_logs_contains_readiness_but_not_proof_or_settings(tmp_path):
    run=tmp_path/'bootstrap/runs'/('a'*32)
    run.mkdir(parents=True)
    (run/'ready.json').write_text(json.dumps({'pid':99,'proof':'private-launch-signature'}))
    (run/'startup.log').write_text('current startup evidence')
    (tmp_path/'settings.json').write_text('{"token":"SENSITIVE"}')
    with zipfile.ZipFile(collect_logs(tmp_path)) as archive:
        text='\n'.join(archive.read(x).decode() for x in archive.namelist())
        assert 'current startup evidence' in text
        assert 'private-launch-signature' not in text and 'SENSITIVE' not in text
        assert not any('settings.json' in x for x in archive.namelist())


def test_startup_bundle_allowlist_excludes_arbitrary_profile_files(tmp_path):
    reports=tmp_path/'reports';reports.mkdir()
    write_report(reports/'report.json',{'token':'never export','status':'fixture'})
    (reports/'settings.json').write_text('PRIVATE')
    (reports/'secret.sqlite3').write_text('PRIVATE')
    (reports/'screenshot.png').write_text('PRIVATE')
    with zipfile.ZipFile(bundle_reports(reports,tmp_path/'report.zip')) as archive:
        assert set(archive.namelist())=={'report.json','manifest.json'}
        assert 'never export' not in archive.read('report.json').decode()


def test_previous_log_session_not_in_new_interval(tmp_path):
    path=tmp_path/'application.log';path.write_text('OLD SESSION\n')
    offset=path.stat().st_size
    with path.open('a') as stream:stream.write('NEW SESSION\n')
    assert read_new_log(path,offset)=='NEW SESSION\n'


def test_log_read_is_bounded_and_symlink_not_followed(tmp_path):
    from tools.startup_test import MAX_FILE
    target=tmp_path/'other.log';target.write_text('X'*(MAX_FILE+100))
    assert len(read_new_log(target,0))==MAX_FILE
    link=tmp_path/'alias.log'
    try:link.symlink_to(target)
    except OSError:pytest.skip('Symlink creation unavailable in this test environment')
    assert read_new_log(link,0)==''


def test_telegram_probe_uses_public_url_no_auth_or_netrc(monkeypatch):
    import requests
    session=Mock();session.__enter__=Mock(return_value=session);session.__exit__=Mock()
    response=Mock(status_code=302);response.__enter__=Mock(return_value=response);response.__exit__=Mock()
    session.get.return_value=response
    monkeypatch.setattr(requests,'Session',lambda:session)
    monkeypatch.setattr(requests.utils,'get_environ_proxies',lambda url:{})
    result=network_probe('telegram')
    assert result['authenticated'] is False and result['http_status']==302
    assert session.trust_env is False
    assert session.get.call_args.args==('https://api.telegram.org/',)
    assert 'auth' not in session.get.call_args.kwargs


def test_telegram_api_splits_connection_and_polling_read_timeout(tmp_path):
    from jarvis.integrations.telegram_remote import TelegramRemoteController
    controller=TelegramRemoteController.__new__(TelegramRemoteController)
    controller._base_url_cache='https://api.telegram.org/botTEST/'
    controller._bot_token_cache='TEST'
    session=Mock();response=Mock(status_code=200)
    response.json.return_value={'ok':True,'result':[]}
    session.post.return_value=response;controller._session=session
    assert controller._api('getUpdates',timeout=35)==[]
    assert session.post.call_args.kwargs['timeout']==(5.0,35.0)


def test_win32_hotkey_api_has_pointer_sized_hwnd_and_reports_actual_error(monkeypatch):
    import ctypes
    from ctypes import wintypes
    from jarvis.runtime.windows_hotkeys import register_hotkey, unregister_hotkey
    user32=Mock();user32.RegisterHotKey.return_value=0;user32.UnregisterHotKey.return_value=1
    monkeypatch.setattr(ctypes,'WinDLL',lambda *a,**k:user32,raising=False)
    monkeypatch.setattr(ctypes,'set_last_error',lambda value:None,raising=False)
    monkeypatch.setattr(ctypes,'get_last_error',lambda:1409,raising=False)
    hwnd=0x123456789AB
    assert register_hotkey(hwnd,4,3,77)==(False,1409)
    assert user32.RegisterHotKey.argtypes[0] is wintypes.HWND
    assert user32.RegisterHotKey.call_args.args[0]==hwnd
    assert unregister_hotkey(hwnd,4)


def test_startup_cmd_refuses_existing_primary_and_exports_report(tmp_path, monkeypatch, capsys):
    import bootstrap
    from tools import startup_test
    from jarvis.runtime.instance_coordinator import InstanceCoordinator
    monkeypatch.setattr(startup_test, '_is_windows', lambda: True)
    monkeypatch.setattr(bootstrap, 'DATA', tmp_path)
    monkeypatch.setattr(startup_test, '_environment', lambda: {'platform': 'mock-windows'})
    monkeypatch.setattr(InstanceCoordinator, 'has_primary', lambda self: True)
    monkeypatch.setattr(InstanceCoordinator, 'primary_pid', lambda self: 99)
    launch = Mock(side_effect=AssertionError('Must not create a duplicate live app'))
    helper = Mock(side_effect=AssertionError('No tests/probes needed when blocked'))
    monkeypatch.setattr(bootstrap, 'launch', launch)
    monkeypatch.setattr(startup_test, 'run_owned_helper', helper)
    assert startup_test.main() == 2
    launch.assert_not_called()
    helper.assert_not_called()
    output = capsys.readouterr().out
    assert 'SEND_THIS_ZIP=' in output
    archives = list((tmp_path/'diagnostics').glob(f'NEO_startup_{startup_test.DIAGNOSTIC_REVISION}_*.zip'))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        report = json.loads(archive.read('report.json'))
        assert report['status'] == 'BLOCKED_EXISTING_INSTANCE'
        assert report['existing_pid'] == 99
        assert 'application_interval.log' not in archive.namelist()


@pytest.mark.parametrize('launch_exit,ping,test_exit,network_status,expected_status,exit_code', [
    (0, True, 0, 'HTTP_RESPONSE', 'UI_READY', 0),
    (0, False, 0, 'HTTP_RESPONSE', 'NEEDS_ANALYSIS', 1),
    (1, False, 0, 'HTTP_RESPONSE', 'NEEDS_ANALYSIS', 1),
    (0, True, 1, 'UNAVAILABLE', 'UI_READY', 1),
    (0, True, 0, 'UNAVAILABLE', 'UI_READY', 0),
    (0, True, 1, 'HTTP_RESPONSE', 'UI_READY', 1)])
def test_startup_diagnostic_end_to_end_mocked_no_live_actions(
        tmp_path, monkeypatch, capsys, launch_exit, ping, test_exit, network_status, expected_status, exit_code):
    import bootstrap
    from tools import startup_test
    from jarvis.runtime.instance_coordinator import InstanceCoordinator
    monkeypatch.setattr(startup_test, '_is_windows', lambda: True)
    monkeypatch.setattr(bootstrap, 'DATA', tmp_path)
    monkeypatch.setattr(startup_test, '_environment', lambda: {'platform': 'mock-windows'})
    monkeypatch.setattr(InstanceCoordinator, 'has_primary', lambda self: False)
    monkeypatch.setattr(InstanceCoordinator, 'primary_pid', lambda self: 888)
    requests = []
    def request(self, action, **kwargs):
        requests.append(action)
        assert action == 'ping'
        return ping
    monkeypatch.setattr(InstanceCoordinator, 'request', request)
    monkeypatch.setattr(startup_test.time, 'sleep', lambda _: None)
    helpers=[]
    def run_helper(command, *, env=None, timeout=15):
        helpers.append(command)
        if 'pytest' in command:
            assert env['NEO_TEST_MODE'] == '1' and env['LOCALAPPDATA'] != str(tmp_path)
            return {'exit_code': test_exit, 'output': 'fixture test result\r\n'}
        assert '--network-probe' in command
        return {'exit_code': 0, 'output': json.dumps({'target': command[-1], 'status': network_status, 'authenticated': False})}
    monkeypatch.setattr(startup_test, 'run_owned_helper', run_helper)
    app_log=tmp_path/'logs/neo_jarvis.log';app_log.parent.mkdir(parents=True)
    app_log.write_text('HISTORICAL SESSION SHOULD NOT APPEAR\n')
    def launch(*, run_id):
        source=tmp_path/'bootstrap/runs'/run_id;source.mkdir(parents=True)
        (source/'ready.json').write_text(json.dumps({'pid':888,'proof':'PRIVATE_SIGNATURE','run_id':run_id}))
        (source/'launch_status.json').write_text(json.dumps({'state': 'ready' if not launch_exit else 'ready_timeout'}))
        (source/'startup.log').write_text('current startup only')
        with app_log.open('a') as stream:stream.write('CURRENT SESSION\n')
        return launch_exit
    monkeypatch.setattr(bootstrap, 'launch', launch)
    assert startup_test.main() == exit_code
    assert len(helpers) == 4
    assert requests == (['ping'] if launch_exit == 0 else [])
    assert 'SEND_THIS_ZIP=' in capsys.readouterr().out
    archive_path=next((tmp_path/'diagnostics').glob(f'NEO_startup_{startup_test.DIAGNOSTIC_REVISION}_*.zip'))
    with zipfile.ZipFile(archive_path) as archive:
        report=json.loads(archive.read('report.json'))
        assert report['status'] == expected_status
        assert report['exit_code'] == exit_code
        assert report['focused_tests_exit_code'] == test_exit
        assert ('focused_tests_failed' in report['summary']['failure_reasons']) == (test_exit != 0)
        assert archive.read('focused_tests.log') == b'fixture test result\n'
        if launch_exit == 0 and ping and test_exit == 0:
            assert report['summary']['overall_status'] == ('PASS' if network_status == 'HTTP_RESPONSE' else 'PASS_WITH_NETWORK_WARNINGS')
        alltext='\n'.join(archive.read(name).decode() for name in archive.namelist())
        assert 'CURRENT SESSION' in alltext and 'PRIVATE_SIGNATURE' not in alltext
        assert 'HISTORICAL SESSION' not in alltext
        assert json.loads(archive.read('network_discord.json'))['authenticated'] is False


def test_startup_bundle_read_is_bounded_before_reading_whole_large_file(tmp_path, monkeypatch):
    from tools.startup_test import MAX_FILE
    report_dir=tmp_path/'reports';report_dir.mkdir()
    (report_dir/'startup.log').write_bytes(b'x'*(MAX_FILE+99))
    monkeypatch.setattr(Path, 'read_bytes', Mock(side_effect=AssertionError('Unbounded read_bytes prohibited')))
    with zipfile.ZipFile(bundle_reports(report_dir,tmp_path/'report.zip')) as archive:
        assert len(archive.read('startup.log')) == MAX_FILE


# NEO 148: byte-exact fixtures reproduce Windows text I/O on any test host.
@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
def test_log_newlines_keep_byte_offset_and_source_unchanged(tmp_path, ending):
    path = tmp_path / "application.log"
    old = "Previous session: кириллица ".encode("utf-8") + ending
    new = "Current session: ответ".encode("utf-8") + ending
    path.write_bytes(old + new)
    assert read_new_log(path, len(old)) == "Current session: ответ\n"
    assert path.read_bytes() == old + new


def test_windows_text_append_reproduces_user_failure_on_every_platform(tmp_path):
    path = tmp_path / "application.log"
    # Explicit CRLF makes the fixture equivalent to native Windows text output.
    path.write_text("OLD SESSION\n", encoding="utf-8", newline="\r\n")
    offset = path.stat().st_size
    with path.open("a", encoding="utf-8", newline="\r\n") as stream:
        stream.write("NEW SESSION\n")
    assert read_new_log(path, offset) == "NEW SESSION\n"
    assert path.read_bytes() == b"OLD SESSION\r\nNEW SESSION\r\n"


def test_export_has_no_double_cr_and_preserves_blank_lines(tmp_path):
    from tools.startup_test import write_log_report
    path = tmp_path / "export.log"
    write_log_report(path, "First\r\n\r\nSecond\nLast\r")
    assert path.read_bytes() == b"First\n\nSecond\nLast\n"


def test_normalized_export_keeps_token_redaction(tmp_path):
    from tools.startup_test import write_log_report
    path = tmp_path / "export.log"
    write_log_report(path, 'token="synthetic-secret"\r\nOther text\r\n')
    data = path.read_bytes()
    assert b"synthetic-secret" not in data
    assert b"Other text\n" in data and b"\r" not in data


@pytest.mark.parametrize("test_exit,ping,network,overall,reasons", [
    (1, True, "UNAVAILABLE", "NEEDS_ANALYSIS", ["focused_tests_failed"]),
    (0, True, "UNAVAILABLE", "PASS_WITH_NETWORK_WARNINGS", []),
    (0, True, "HTTP_RESPONSE", "PASS", []),
    (0, False, "HTTP_RESPONSE", "NEEDS_ANALYSIS", ["ui_ping_unconfirmed"]),
])
def test_diagnostic_summary_separates_real_user_failure_from_network(test_exit, ping, network, overall, reasons):
    from tools.startup_test import diagnostic_summary
    report = {"status": "UI_READY" if ping else "NEEDS_ANALYSIS", "launch_exit_code": 0,
              "ui_ping": ping, "focused_tests_exit_code": test_exit,
              "exit_code": 0 if ping and test_exit == 0 else 1,
              "network_summary": {target: network for target in ("telegram", "discord", "local_vision")}}
    summary = diagnostic_summary(report)
    assert summary["overall_status"] == overall
    assert summary["failure_reasons"] == reasons
    assert summary["ui_status"] == ("READY" if ping else "PING_UNCONFIRMED")
    assert report["exit_code"] == (0 if ping and test_exit == 0 else 1)


def test_summary_of_blocked_run_does_not_claim_passed_tests():
    from tools.startup_test import diagnostic_summary
    summary = diagnostic_summary({"status": "BLOCKED_EXISTING_INSTANCE", "exit_code": 2})
    assert summary["safe_tests"] == "NOT_RUN"
    assert summary["ui_status"] == "NOT_CHECKED"
    assert summary["overall_status"] == "BLOCKED_EXISTING_INSTANCE"


def test_console_summary_names_failed_stage_without_claiming_ui_failure(capsys):
    from tools.startup_test import diagnostic_summary, print_diagnostic_summary
    report = {"status": "UI_READY", "launch_exit_code": 0, "ui_ping": True,
              "focused_tests_exit_code": 1, "exit_code": 1,
              "network_summary": {"telegram": "UNAVAILABLE", "discord": "UNAVAILABLE", "local_vision": "HTTP_RESPONSE"}}
    report["summary"] = diagnostic_summary(report)
    print_diagnostic_summary(report)
    output = capsys.readouterr().out
    assert "UI_STATUS=READY" in output and "SAFE_TESTS=FAIL" in output
    assert "DIAGNOSTIC_REASONS=focused_tests_failed" in output
    assert "NETWORK_TELEGRAM=UNAVAILABLE" in output
    assert "DIAGNOSTIC_EXIT_CODE=1" in output
