"""Run-bound readiness regressions, including real redirected subprocesses."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

import bootstrap
from jarvis.runtime import startup_handshake as handshake

ROOT = Path(__file__).resolve().parents[1]


def receipt(pid=17, parent_pid=10, **changes):
    value = {"version": 2, "run_id": "current", "pid": pid, "parent_pid": parent_pid,
             "state": "ui_event_loop_ready", "ready_at": time.time(),
             "source_app": str(ROOT / "app.py")}
    value.update(changes)
    value["proof"] = handshake._signature(value, "test-secret")
    return value


def validate(payload, *, spawned=17):
    return handshake.validate_ready(payload, spawned_pid=spawned, run_id="current",
                                    secret="test-secret", source_app=ROOT / "app.py", launched_at=time.time()-5)


def test_current_direct_signed_receipt():
    assert validate(receipt()) == (True, "direct_process")


@pytest.mark.parametrize("changes, reason", [
    ({"run_id": "old"}, "run_id_mismatch"),
    ({"state": []}, "invalid_ready_state"),
    ({"version": 1}, "unsupported_receipt_version"),
    ({"ready_at": 10**1000}, "invalid_ready_time"),
    ({"state": "process_running"}, "invalid_ready_state"),
    ({"pid": 0}, "invalid_pid"), ({"pid": True}, "invalid_pid"),
    ({"source_app": str(ROOT / "other.py")}, "source_mismatch"),
    ({"ready_at": time.time()-1000}, "ready_time_mismatch"),
    ({"ready_at": time.time()+1000}, "ready_time_mismatch"),
])
def test_wrong_signed_context_rejected(changes, reason):
    assert validate(receipt(**changes)) == (False, reason)


@pytest.mark.parametrize("value", [None, [], "ready", 1])
def test_non_objects_rejected(value):
    assert validate(value) == (False, "invalid_payload_type")


@pytest.mark.parametrize("proof", [None, "bad", "", "я", 12])
def test_invalid_signature_rejected(proof):
    row = receipt()
    row["proof"] = proof
    assert not validate(row)[0]


def test_post_signing_pid_edit_is_not_accepted():
    value=receipt(); value["pid"]=99
    assert validate(value) == (False, "proof_mismatch")


def test_signed_false_parent_cannot_authorize_unrelated_live_process():
    value=receipt(pid=os.getpid(), parent_pid=987654)
    assert validate(value, spawned=987654) == (False, "parent_pid_mismatch")


def test_legacy_readiness_does_not_get_a_permissive_descendant_fallback():
    row = {"run_id":"current", "pid":99, "parent_pid":17, "state":"ui_event_loop_ready"}
    assert not handshake.validate_ready(row, spawned_pid=17, run_id="current")[0]


def test_dead_ordinary_ui_is_not_ready_even_with_signed_proof():
    import psutil
    nonexistent = max(psutil.pids()) + 1000000
    assert validate(receipt(pid=nonexistent, parent_pid=17)) == (False, "ui_process_exited")


def test_completed_signed_existing_instance_ack_is_accepted():
    import psutil
    nonexistent = max(psutil.pids()) + 1000000
    value=receipt(pid=nonexistent, parent_pid=17, state="existing_instance_acknowledged")
    assert validate(value) == (True, "venv_redirector_completed_ack")


def test_publish_error_is_diagnostic_and_does_not_raise(tmp_path, monkeypatch, caplog):
    run=tmp_path/'current'; run.mkdir()
    monkeypatch.setenv('NEO_LAUNCH_RUN_ID','current')
    monkeypatch.setenv('NEO_LAUNCH_READY_PATH',str(run/'ready.json'))
    monkeypatch.setattr(Path, 'replace', Mock(side_effect=PermissionError('blocked fixture')))
    assert not handshake.publish_ready()
    assert 'readiness write failed' in caplog.text


def test_secret_removed_before_other_workers_inherit_environment(monkeypatch):
    monkeypatch.setattr(handshake, '_SECRET', '')
    monkeypatch.setenv('NEO_LAUNCH_READY_SECRET','ephemeral-test-secret')
    handshake.capture_launch_secret()
    assert 'NEO_LAUNCH_READY_SECRET' not in os.environ
    assert handshake._SECRET == 'ephemeral-test-secret'


def test_timeout_reports_mismatched_pids_without_killing_anything(tmp_path):
    path=tmp_path/'ready.json'; path.write_text(json.dumps(receipt(pid=99)))
    proc=Mock(pid=17);proc.poll.return_value=None
    result=bootstrap._wait_for_ready(proc,path,'current',timeout=.02)
    assert result['state']=='ready_timeout'
    assert result['ready_rejection']=='pid_mismatch' and result['reported_pid']==99
    proc.kill.assert_not_called();proc.terminate.assert_not_called()


def _real_run(tmp_path, *, redirected, existing=False, managed=False):
    run_id='a'*32;run=tmp_path/run_id;run.mkdir();ready=run/'ready.json'
    env=dict(os.environ, PYTHONPATH=str(ROOT), NEO_LAUNCH_RUN_ID=run_id,
             NEO_LAUNCH_READY_PATH=str(ready), NEO_LAUNCH_READY_SECRET='local-test-proof')
    # Raw base Python avoids adding extra Windows redirectors to the simulated
    # wrapper case. The separate managed case tests the real venv executable.
    python=sys.executable if managed else getattr(sys,'_base_executable',sys.executable)
    worker=("from jarvis.runtime.startup_handshake import publish_ready;import time;"
            f"assert publish_ready(existing_instance={existing!r});time.sleep({0 if existing else 1.0})")
    command=[python,'-c',worker]
    if redirected:
        command=[python,'-c',"import subprocess,sys;raise SystemExit(subprocess.call([sys.executable,'-c',sys.argv[1]]))",worker]
    started=time.time()
    proc=subprocess.Popen(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        if existing:
            proc.wait(timeout=10)  # Receiver sees the receipt after publisher exit.
        result=bootstrap._wait_for_ready(proc,ready,run_id,timeout=10,secret='local-test-proof',
                                         source_app=ROOT/'app.py',launched_at=started)
        out,err=proc.communicate(timeout=10)
        assert proc.returncode==0, (out,err)
        assert result['ok'], result
        return result
    finally:
        if proc.poll() is None:
            proc.wait(timeout=10)


def test_live_redirected_publisher_passes_without_relaxing_run_binding(tmp_path):
    result=_real_run(tmp_path, redirected=True)
    assert result['ui_pid'] != result['child_pid']
    assert result['ready_relation']=='venv_redirector_child'


def test_exited_redirected_secondary_launch_ack(tmp_path):
    result=_real_run(tmp_path, redirected=True, existing=True)
    assert result['state']=='existing_instance_acknowledged'


def test_actual_managed_python_startup_contract(tmp_path):
    result=_real_run(tmp_path, redirected=False, managed=True)
    assert result['ready_relation'] in {'direct_process','venv_redirector_child'}


def test_app_publishes_only_from_owned_event_loop_timer():
    source=(ROOT/'app.py').read_text(encoding='utf-8')
    assert 'startup_ready_timer = QTimer(window)' in source
    assert 'startup_ready_timer.timeout.connect(_publish_ui_ready)' in source
    assert source.index('capture_launch_secret()') < source.index('from PySide6.QtCore')
    assert source.index('startup_ready_timer.start(0)') < source.index('QTimer.singleShot(2200, telegram_remote.start)')


def test_stale_launch_metadata_pruning_keeps_live_run_and_unknown_files(tmp_path):
    now=time.time();runs=tmp_path/'runs';runs.mkdir()
    old=runs/('a'*32);old.mkdir()
    (old/'ready.json').write_text(json.dumps({'pid':2147483647}))
    (old/'startup.log').write_text('old launcher-owned log')
    active=runs/('b'*32);active.mkdir()
    (active/'ready.json').write_text(json.dumps({'pid':os.getpid()}))
    unknown=runs/('c'*32);unknown.mkdir()
    (unknown/'user-note.txt').write_text('must stay')
    for path in (old,active,unknown):os.utime(path,(now-9*86400,now-9*86400))
    bootstrap._prune_launch_runs(tmp_path,now=now)
    assert not old.exists()
    assert active.is_dir() and (unknown/'user-note.txt').read_text() == 'must stay'


def test_run_pruning_rejects_non_run_and_symlink_directories(tmp_path):
    now=time.time();runs=tmp_path/'runs';runs.mkdir()
    ordinary=runs/'personal';ordinary.mkdir()
    (ordinary/'startup.log').write_text('not launcher-owned')
    external=tmp_path/'outside';external.mkdir()
    (external/'ready.json').write_text('{}')
    os.utime(ordinary,(now-9*86400,now-9*86400))
    link=runs/('d'*32)
    try:link.symlink_to(external, target_is_directory=True)
    except OSError:link=None
    bootstrap._prune_launch_runs(tmp_path,now=now,keep=0)
    assert ordinary.is_dir() and (external/'ready.json').exists()
