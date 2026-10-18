"""Launcher/vision regressions without Windows installation or external requests."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock
import json
import os
import subprocess
import sys

import pytest

import bootstrap
from jarvis.runtime import vision_startup as vision
from jarvis.runtime.vision_policy import migrate_vision_startup
from jarvis.runtime.diagnostics import redact


@pytest.fixture(autouse=True)
def reset_ollama_readiness_snapshot():
    from jarvis.ollama_status import reset_status
    reset_status()
    yield
    reset_status()


def config_at(root, **values):
    values = {"ollama.vision_enabled": True, "ollama.auto_start": True,
              "ollama.auto_install": True, **values}
    return SimpleNamespace(paths=SimpleNamespace(root=root), get=lambda key, default=None: values.get(key, default))


@pytest.mark.parametrize("version", [163, 164])
def test_real_legacy_config_repairs_vision_once_preserves_stt_and_creates_backup(tmp_path, monkeypatch, version):
    from jarvis.config import ConfigManager
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    data_root = tmp_path / "local/NeoJarvis"
    data_root.mkdir(parents=True)
    original = {"settings_version": version, "voice": {"stt_model": "base"},
                "wake_word": {"stt_model": "tiny"}, "ollama": {
                    "vision_enabled": True, "auto_start": False, "auto_install": False,
                    "requests_enabled": True, "preload_model": True}}
    (data_root / "settings.json").write_text(json.dumps(original))
    defaults = Path(__file__).resolve().parents[1] / "config/default_settings.json"
    config = ConfigManager(defaults)
    assert config.get("settings_version") >= 173
    assert not config.get("ollama.auto_start")  # explicit opt-out survives migration
    assert not config.get("ollama.auto_install")  # legacy flag retained; launcher ignores it
    assert not config.get("ollama.requests_enabled") and not config.get("ollama.preload_model")
    assert config.get("voice.stt_model") == "base"
    assert config.get("wake_word.stt_model") == "tiny"
    backup = data_root / f"backups/settings-before-v165-v{version}.json"
    assert json.loads(backup.read_text()) == original
    config.data["ollama"]["auto_start"] = False
    config.data["ollama"]["auto_install"] = False
    config.save(config.data)
    reloaded = ConfigManager(defaults)
    assert not reloaded.get("ollama.auto_start")
    assert not reloaded.get("ollama.auto_install")
    assert reloaded.get("voice.stt_model") == "base"


def test_explicit_vision_opt_out_is_never_migrated_to_enabled():
    data = {"vision_enabled": False, "auto_start": False, "auto_install": False}
    migrate_vision_startup(data, settings_version=164)
    assert not data["vision_enabled"] and not data["auto_start"] and not data["auto_install"]
    assert data["startup_policy_version"] == 145
    assert not data["requests_enabled"] and data["vision_keep_alive"] == 0


def test_self_test_bypasses_ambient_proxy_and_only_lists_installed_models(tmp_path, monkeypatch):
    from jarvis.runtime.self_test import run_self_test
    session = MagicMock()
    session.__enter__.return_value = session
    response = session.get.return_value
    response.__enter__.return_value = response
    response.status_code = 200
    response.json.return_value = {"models": [{"name": "vision-test:1"}]}
    monkeypatch.setattr("requests.Session", lambda: session)
    config = config_at(tmp_path, **{"ollama.base_url": "http://127.0.0.1:11434", "ollama.vision_model": "vision-test:1"})
    result = run_self_test(config)
    checks = {row["name"]: row for row in result["checks"]}
    assert checks["ollama"]["status"] == checks["ollama_model"]["status"] == "PASS"
    assert session.trust_env is False
    session.get.assert_called_once_with("http://127.0.0.1:11434/api/tags", timeout=(1.0, 2.0), allow_redirects=False)
    session.post.assert_not_called()


def test_v165_migration_disables_only_generic_boss_address(tmp_path, monkeypatch):
    from jarvis.config import ConfigManager
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    root = tmp_path / "local/NeoJarvis"
    root.mkdir(parents=True)
    defaults = Path(__file__).resolve().parents[1] / "config/default_settings.json"
    for index, (name, expected) in enumerate((("Boss", False), ("босс", False), ("Алекс", True))):
        # Each legacy fixture is a distinct installation; canonical data wins
        # over later legacy edits once a profile has already migrated.
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / f"local-{index}"))
        root = tmp_path / f"local-{index}" / "NeoJarvis"
        root.mkdir(parents=True)
        original = {"settings_version": 164, "user_name": name, "user_name_enabled": True}
        (root / "settings.json").write_text(json.dumps(original))
        config = ConfigManager(defaults)
        assert config.get("user_name_enabled") is expected
        assert config.get("user_name") == name


def test_overlapping_real_process_shutdown_cannot_remove_new_session_marker(tmp_path):
    from jarvis.runtime.crash_recovery import SessionGuard
    old = SessionGuard(tmp_path)
    assert old.start() is None
    old.mark_shutting_down()
    code = "from jarvis.runtime.crash_recovery import SessionGuard;from pathlib import Path;import sys,json,os;g=SessionGuard(Path(sys.argv[1]));print(json.dumps({'previous':g.start(),'id':g.session_id,'pid':os.getpid()}),flush=True);sys.stdin.readline()"
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        ready = json.loads(child.stdout.readline())
        assert ready["previous"] is None  # previous PID is genuinely alive and stopping
        old.clean_shutdown()
        old.mark_exception(RuntimeError("late old-process error"))
        current = json.loads(old.marker.read_text())
        assert current["session_id"] == ready["id"] and current["pid"] == ready["pid"]
        assert "exception" not in current
    finally:
        child.communicate(input="exit\n", timeout=5)
    successor = SessionGuard(tmp_path)
    report = successor.start()
    assert report["previous"]["pid"] == ready["pid"]  # actual unclean exit is still detected
    successor.clean_shutdown()
    assert not successor.marker.exists()


def test_dead_process_marked_shutting_down_is_still_reported_unclean(tmp_path):
    code = "from jarvis.runtime.crash_recovery import SessionGuard;from pathlib import Path;import sys;g=SessionGuard(Path(sys.argv[1]));g.start();g.mark_shutting_down()"
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True, timeout=5)
    from jarvis.runtime.crash_recovery import SessionGuard
    current = SessionGuard(tmp_path)
    assert current.start()["previous"]["state"] == "shutting_down"
    current.clean_shutdown()


def test_shutdown_pid_reuse_and_recorded_exception_are_not_hidden(tmp_path):
    import psutil
    from jarvis.runtime.crash_recovery import SessionGuard
    marker = {"pid": os.getpid(), "state": "shutting_down", "process_started_at": psutil.Process().create_time() - 100}
    assert not SessionGuard._is_live_shutdown(marker)
    marker.update(process_started_at=psutil.Process().create_time(), exception="failure")
    assert not SessionGuard._is_live_shutdown(marker)


@pytest.mark.parametrize("state", ["ui_event_loop_ready", "existing_instance_acknowledged"])
def test_real_child_readiness_and_existing_instance_ack(tmp_path, state):
    import time
    run_id = "run-145"
    ready = tmp_path / run_id / "ready.json"
    ready.parent.mkdir()
    secret = "owned-test-secret-not-a-user-key"
    source = Path(bootstrap.__file__).resolve().parent / "app.py"
    env = dict(os.environ, NEO_LAUNCH_RUN_ID=run_id, NEO_LAUNCH_READY_PATH=str(ready),
               NEO_LAUNCH_READY_SECRET=secret)
    code = "from jarvis.runtime.startup_handshake import publish_ready;import sys;publish_ready(existing_instance=sys.argv[1]=='existing_instance_acknowledged');sys.stdin.readline()"
    started = time.time()
    proc = subprocess.Popen([sys.executable, "-c", code, state], stdin=subprocess.PIPE, env=env)
    try:
        result = bootstrap._wait_for_ready(proc, ready, run_id, timeout=5, secret=secret,
                                          source_app=source, launched_at=started)
        assert result['ok'] and result['state'] == state
        assert result['child_pid'] == proc.pid
        assert result['ui_pid'] == json.loads(ready.read_text())['pid']
        assert result['ready_relation'] in {'direct_process','venv_redirector_child'}
    finally:
        proc.communicate(input=b"exit\n", timeout=5)


def test_stale_ready_cannot_hide_real_startup_failure(tmp_path):
    ready = tmp_path / "ready.json"
    ready.write_text(json.dumps({"run_id": "old", "pid": 77, "state": "ui_event_loop_ready"}))
    process = Mock(pid=77)
    process.poll.return_value = 3
    result = bootstrap._wait_for_ready(process, ready, "current", timeout=1)
    assert not result["ok"] and result["exit_code"] == 3
    process.terminate.assert_not_called()


def test_ready_timeout_never_kills_process_and_status_is_exportable(tmp_path):
    process = Mock(pid=77)
    result = bootstrap._wait_for_ready(process, tmp_path / "missing", "run", timeout=0)
    assert result["state"] == "ready_timeout"
    process.kill.assert_not_called()
    process.terminate.assert_not_called()
    bootstrap._record_launch_status(tmp_path, "run", **result)
    report = json.loads((tmp_path / "launch_status.json").read_text())
    expected_build = (Path(bootstrap.__file__).resolve().parent / "VERSION.txt").read_text(encoding="utf-8").splitlines()[0]
    assert report["build"] == expected_build and report["child_pid"] == 77
    assert report["ok"] is False


def test_vision_start_is_queued_once_without_http_on_ui_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "_is_windows", lambda: True)
    monkeypatch.setattr(vision, "_INFLIGHT", set())
    thread = Mock()
    factory = Mock(return_value=thread)
    monkeypatch.setattr(vision.threading, "Thread", factory)
    probe = Mock(side_effect=AssertionError("No UI-thread network"))
    monkeypatch.setattr(vision, "_probe_local_version", probe)
    config = config_at(tmp_path, **{"ollama.auto_install": False})
    assert vision.start_vision_backend(config)
    assert vision.start_vision_backend(config)
    factory.assert_called_once()
    thread.start.assert_called_once()
    assert "-NoInstall" not in factory.call_args.kwargs["args"][2]  # installation always enabled
    probe.assert_not_called()
    assert json.loads((tmp_path / "bootstrap/vision_status.json").read_text())["state"] == "queued"


@pytest.mark.parametrize("values,reason", [
    ({"ollama.vision_enabled": False, "local_chat.enabled": False}, "local_models_disabled"),
    ({"ollama.auto_start": False}, "automatic_start_disabled"),
    ({"ollama.base_url": "https://example.org:11434"}, "invalid_local_endpoint"),
])
def test_vision_skip_reason_is_visible_and_never_spawns(tmp_path, monkeypatch, values, reason):
    monkeypatch.setattr(vision, "_is_windows", lambda: True)
    factory = Mock()
    monkeypatch.setattr(vision.threading, "Thread", factory)
    assert not vision.start_vision_backend(config_at(tmp_path, **values))
    assert json.loads((tmp_path / "bootstrap/vision_status.json").read_text())["reason"] == reason
    factory.assert_not_called()


def test_existing_server_reused_without_installer_or_model_request(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "_probe_local_version", Mock(return_value=(True, "0.34.0")))
    spawn = Mock()
    monkeypatch.setattr(vision.subprocess, "Popen", spawn)
    vision._bootstrap_worker(config_at(tmp_path), "http://127.0.0.1:11434", [], "key")
    spawn.assert_not_called()
    report = json.loads((tmp_path / "bootstrap/vision_status.json").read_text())
    assert report["state"] == "ready" and report["reason"] == "existing_server_reused"
    assert report["model_loaded_by_startup"] is False


@pytest.mark.parametrize("ready,exit_code", [(True, 0), (False, 2)])
def test_hidden_bootstrap_verifies_actual_api_not_process_spawn(tmp_path, monkeypatch, ready, exit_code):
    monkeypatch.setattr(vision, "_probe_local_version", Mock(side_effect=[(False, ""), (ready, "0.34.0" if ready else "")]))
    process = Mock(pid=88)
    process.wait.return_value = exit_code
    spawn = Mock(return_value=process)
    monkeypatch.setattr(vision.subprocess, "Popen", spawn)
    vision._bootstrap_worker(config_at(tmp_path), "http://127.0.0.1:11434", ["powershell.exe", "safe.ps1"], "key")
    assert spawn.call_args.kwargs["creationflags"] & 0x08000000
    assert spawn.call_args.kwargs["stdin"] == subprocess.DEVNULL
    report = json.loads((tmp_path / "bootstrap/vision_status.json").read_text())
    assert report["state"] == ("ready" if ready else "unavailable")
    assert report["bootstrap_exit"] == exit_code


def test_proxy_environment_is_ignored_for_local_vision_and_recreated_session(monkeypatch):
    from jarvis.ollama_client import OllamaClient
    monkeypatch.setenv("HTTP_PROXY", "http://must-not-receive-images.invalid:8888")
    client = OllamaClient(config_at(Path(".")))
    assert client._session.trust_env is False
    client.cancel_active()
    assert client._session.trust_env is False
    client._session.close()
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.get.return_value.json.return_value = {"version": "0.34.0"}
    monkeypatch.setattr("requests.Session", Mock(return_value=session))
    assert vision._probe_local_version("http://127.0.0.1:11434") == (True, "0.34.0")
    assert session.trust_env is False
    session.get.assert_called_once_with("http://127.0.0.1:11434/api/version", timeout=(0.6, 1.0))
    session.post.assert_not_called()


def test_cmd_detaches_normal_launch_but_keeps_diagnostics_attached():
    root = Path(__file__).resolve().parents[2]
    source = (root / "NEO.cmd").read_text()
    normal, diagnostics = source.split(":diagnostics", 1)
    assert 'start "" "%~dp0NEO.exe"' in normal
    assert "exit /b %ERRORLEVEL%" in normal
    assert 'launch.ps1" -Mode "%NEO_MODE%"' in diagnostics
    assert "Uninstall.exe" not in source


def test_escaped_profile_paths_are_redacted_in_tracebacks():
    assert "PrivatePerson" not in redact(r"FileNotFoundError: 'C:\\Users\\PrivatePerson\\Downloads\\project\\settings.json'")
