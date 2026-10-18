"""Isolated scenario contract tests: all OS launches/subprocesses are fakes."""
from __future__ import annotations
import io
import json
import threading
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from jarvis.runtime import scenarios as runtime
from jarvis.runtime.scenario_schema import validate_scenario
from jarvis.runtime.cancellation import cancellation_manager
from jarvis.ui.scenario_editor import ScenarioEditor, ScenarioStepDialog
from jarvis.ui.scenario_strings import LANGS, _ROWS, st


class Config:
    def __init__(self, rows=None, language="en"):
        self.data = {"scenarios": {"items": deepcopy(rows or [])}, "app": {"language": language}}
        self.paths = SimpleNamespace(root=Path("/disposable/scenario-test") / str(id(self)))
    def get(self, dotted, default=None):
        value = self.data
        for key in dotted.split("."):
            if not isinstance(value, dict): return default
            value = value.get(key, default)
        return value


def item(steps=None, **fields):
    return {"id": "test-id", "name": "Work", "phrase": "work mode", "aliases": ["office"],
            "steps": steps if steps is not None else [{"type": "wait", "seconds": 0}], **fields}


@pytest.fixture(autouse=True)
def no_actions(monkeypatch):
    cancellation_manager().reset()
    monkeypatch.setattr(runtime, "_open_path", Mock(side_effect=AssertionError("Unexpected OS open")))
    monkeypatch.setattr(runtime.subprocess, "Popen", Mock(side_effect=AssertionError("Unexpected process")))
    yield
    for run in runtime.active_scenarios(): runtime.cancel_scenario(run["run_id"])
    cancellation_manager().reset()


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def editor(app):
    widget = ScenarioEditor(Config([item()]))
    yield widget
    widget.close(); widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.mark.parametrize("step", [
    {"type": "wait", "seconds": float("nan")}, {"type": "wait", "seconds": float("inf")},
    {"type": "wait", "seconds": -1}, {"type": "wait", "seconds": 31},
    {"type": "open", "path": "a", "monitor": -1}, {"type": "open", "path": "a", "monitor": 1.5},
    {"type": "open", "path": "a\x00b"}, {"type": "open", "path": ""},
    {"type": "command", "command": []}, {"type": "command", "command": [1]},
    {"type": "command", "command": ["exe"], "timeout": 121}, {"type": "unknown"},
    {"type": "wait", "seconds": True}, {"type": "wait", "enabled": "false"},
])
def test_invalid_steps_never_run(step):
    config = Config([item([step])])
    assert not runtime.dry_run_scenario(config, "test-id")["valid"]
    result = runtime.run_scenario(config, "test-id")
    assert not result.success and result.status == "invalid"
    runtime._open_path.assert_not_called()
    runtime.subprocess.Popen.assert_not_called()


def test_limits_empty_and_disabled():
    for steps in ([], [{"type": "wait", "seconds": 0}] * 101):
        assert not validate_scenario(item(steps)).valid
    assert not validate_scenario(item([{ "type": "wait", "seconds": 2}], max_seconds=1)).valid
    result = runtime.run_scenario(Config([item(enabled=False)]), "office")
    assert result.status == "disabled" and not result.success


def test_dry_run_is_pure_even_dangerous_and_missing_paths(monkeypatch):
    raw = item([{"type": "open", "path": "/missing/file.exe", "monitor": 32},
                {"type": "command", "command": ["cmd.exe", "/c", "anything"]}])
    config = Config([raw]); before = deepcopy(config.data)
    monkeypatch.setattr(Path, "exists", Mock(side_effect=AssertionError("no probe")))
    events = Mock()
    runtime.subscribe_scenarios(events)
    try:
        report = runtime.dry_run_scenario(config, "test-id")
        assert report["valid"] and report["dry_run"]
        assert config.data == before
        events.assert_not_called()
    finally: runtime.unsubscribe_scenarios(events)


def test_risk_case_and_legacy_command():
    from jarvis.security import Risk
    for step in ({"type": " COMMAND ", "command": "tool.exe"},
                 {"path": "C:\\scripts\\do.PS1"}, {"path": "script.PY"}):
        config = Config([item([step])])
        assert runtime.scenario_risk(config, {"action": "run", "id": "test-id"}) == Risk.HIGH
    assert runtime.scenario_risk(config, {"action": "validate", "id": "test-id"}) == Risk.SAFE


def test_id_and_alias_ambiguity():
    raw = item()
    config = Config([raw, {**raw, "id": "second"}])
    assert runtime.find_scenario(config, "test-id")["id"] == "test-id"
    with pytest.raises(ValueError): runtime.find_scenario(config, "office")
    config.data["scenarios"]["items"][1]["id"] = "test-id"
    with pytest.raises(ValueError): runtime.find_scenario(config, "test-id")


@pytest.mark.parametrize("policy,required,expected", [("continue", None, 2), ("stop", None, 1), ("continue", True, 1), ("stop", False, 2)])
def test_failure_policy_legacy_required(monkeypatch, policy, required, expected):
    step = {"type": "open", "path": "missing.exe"}
    if required is not None: step["required"] = required
    raw = item([step, {"type": "wait", "seconds": 0}], on_failure=policy)
    monkeypatch.setattr(runtime, "_open_path", lambda _: {"success": False, "error": "missing"})
    events = []
    result = runtime.run_scenario(Config([raw]), "office", on_event=events.append)
    assert result.status == "failed" and len(result.steps) == expected
    assert events[0]["state"] == "started" and events[-1]["state"] == "failed"


def test_disabled_step_is_skipped(monkeypatch):
    raw = item([{"type": "open", "path": "skip.exe", "enabled": False}, {"type": "wait", "seconds": 0}])
    result = runtime.run_scenario(Config([raw]), "test-id")
    assert result.success and result.steps[0]["skipped"]
    runtime._open_path.assert_not_called()


def test_wait_cancel_and_duplicate_guard():
    config = Config([item([{"type": "wait", "seconds": 10}])])
    started, finished = threading.Event(), []
    def worker():
        cancellation_manager().reset()
        finished.append(runtime.run_scenario(config, "office", on_event=lambda e: started.set() if e["state"] == "step_started" else None))
    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(2)
    active = runtime.active_scenarios(config)
    assert len(active) == 1
    assert runtime.run_scenario(config, "test-id").status == "busy"
    t = time.monotonic()
    assert runtime.cancel_scenario(active[0]["run_id"])
    thread.join(2)
    assert not thread.is_alive() and time.monotonic() - t < 1
    assert finished[0].status == "cancelled" and not finished[0].success
    assert runtime.active_scenarios(config) == []
    assert not runtime.cancel_scenario(active[0]["run_id"])


class FakeProcess:
    def __init__(self, *, completed=False):
        self.stdout, self.stderr = io.BytesIO(b"x" * 100000), io.BytesIO(b"warning")
        self.returncode = 0 if completed else None
        self.terminated = False
    def poll(self): return self.returncode
    def terminate(self): self.terminated = True; self.returncode = -15
    def kill(self): self.returncode = -9
    def wait(self, timeout=None): return self.returncode


def test_command_argv_no_shell_bounded_output(monkeypatch):
    process = FakeProcess(completed=True)
    popen = Mock(return_value=process)
    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    raw = item([{"type": "command", "command": ["program.exe", "a b", "& literal"]}])
    result = runtime.run_scenario(Config([raw]), "test-id")
    assert result.success
    assert popen.call_args.args[0] == ["program.exe", "a b", "& literal"]
    assert popen.call_args.kwargs["shell"] is False
    assert len(result.steps[0]["stdout"]) == 2000


def test_owned_command_is_terminated_on_cancel(monkeypatch):
    process = FakeProcess()
    started = threading.Event()
    def launch(*args, **kwargs): started.set(); return process
    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    config = Config([item([{"type": "command", "command": "program.exe"}])])
    results = []
    def worker():
        cancellation_manager().reset()
        results.append(runtime.run_scenario(config, "test-id"))
    thread = threading.Thread(target=worker); thread.start()
    assert started.wait(2)
    assert runtime.cancel_scenario(runtime.active_scenarios(config)[0]["run_id"])
    thread.join(2)
    assert not thread.is_alive() and process.terminated
    assert results[0].status == "cancelled"


def test_observer_failure_does_not_interrupt_cleanup():
    def failed(_): raise RuntimeError("closed UI")
    config = Config([item()])
    runtime.subscribe_scenarios(failed)
    try:
        assert runtime.run_scenario(config, "test-id", on_event=failed).success
        assert not runtime.active_scenarios(config)
    finally: runtime.unsubscribe_scenarios(failed)


def test_editor_preserves_complete_draft_and_isolated_config(app):
    raw = item([{"path": "C:/app.exe", "custom": {"keep": 1}}, {"type": "wait", "seconds": 0},
                {"type": "command", "command": ["tool", "a b"], "required": True}], extension=[1, 2])
    config = Config([raw]); widget = ScenarioEditor(config)
    assert widget.items() == [raw]
    widget.name.setText("Renamed")
    assert config.get("scenarios.items") == [raw]
    saved = widget.items()[0]
    assert saved["name"] == "Renamed"
    assert saved["id"] == raw["id"] and saved["aliases"] == raw["aliases"]
    assert saved["steps"] == raw["steps"] and saved["extension"] == [1, 2]
    widget.deleteLater()


def test_editor_create_duplicate_reorder_and_confirm_delete(editor, monkeypatch):
    from jarvis.ui import scenario_editor as ui
    editor.duplicate_scenario()
    assert len(editor.items()) == 2
    assert editor.items()[1]["id"] != editor.items()[0]["id"]
    assert editor.items()[1]["aliases"] == [] and editor.items()[1]["phrase"] == ""
    monkeypatch.setattr(ui, "_confirm", lambda *a: False)
    editor.delete_scenario(); assert len(editor.items()) == 2
    monkeypatch.setattr(ui, "_confirm", lambda *a: True)
    editor.delete_scenario(); assert len(editor.items()) == 1
    editor._raw()["steps"].append({"type": "wait", "seconds": 2})
    editor._rebuild_steps(0)
    editor.move_step(1)
    assert [s["seconds"] for s in editor.items()[0]["steps"]] == [2, 0]
    editor.delete_step()
    assert len(editor.items()[0]["steps"]) == 1


def test_editor_invalid_validation_never_launches(editor):
    editor._raw()["steps"] = [{"type": "open", "path": ""}]
    assert not editor.validate_current()
    with pytest.raises(ValueError): editor.items()
    runtime._open_path.assert_not_called(); runtime.subprocess.Popen.assert_not_called()
    editor.enabled.setChecked(False)
    assert editor.items()[0]["enabled"] is False


def test_step_dialog_json_cancel_validation_and_unknown_fields(app):
    original = {"type": "command", "command": "C:/app.exe", "extra": {"keep": True}, "required": True}
    dialog = ScenarioStepDialog(original)
    assert json.loads(dialog.command.toPlainText()) == ["C:/app.exe"]
    dialog.command.setPlainText("__import__('os').system('no')")
    dialog.accept(); assert dialog.result() != QDialog.Accepted
    dialog.command.setPlainText('["C:/app.exe", "a b", "& literal"]')
    dialog.accept(); assert dialog.result() == QDialog.Accepted
    assert dialog.result_step["extra"] == original["extra"] and dialog.result_step["required"]
    assert original["command"] == "C:/app.exe"
    dialog.deleteLater()


@pytest.mark.parametrize("language", LANGS)
def test_seven_language_editor_and_long_text(app, language):
    assert all(len(row) == 7 and all(row) for row in _ROWS.values())
    raw = item(name="ユーザー Ω длинное имя " * 6)
    config = Config([raw], language)
    widget = ScenarioEditor(config)
    widget.resize(850, 780); widget.show(); app.processEvents()
    assert widget.new_button.text() == st("new", language)
    assert widget.steps.headerItem().text(0) == st("step", language)
    assert widget.items()[0]["name"] == raw["name"]
    assert widget.name.geometry().width() > 50
    for key in ("name", "phrase", "aliases"):
        label = next(x for x in widget.findChildren(QLabel) if x.text() == st(key, language))
        assert label.width() > 20 and label.height() > 10
    assert widget.steps.geometry().width() > 100
    widget.name.setFocus(); QTest.keyClick(widget.name, Qt.Key_A, Qt.ControlModifier)
    QTest.keyClicks(widget.name, "Keyboard rename")
    assert widget.items()[0]["name"] == "Keyboard rename"
    widget.close(); widget.deleteLater()


def test_live_retranslate_keeps_draft_fields(editor):
    before = editor.items()
    for language in LANGS:
        editor.config.data["app"]["language"] = language
        editor.retranslate()
        assert editor.new_button.text() == st("new", language)
        assert editor.items() == before


def test_cancel_at_step_event_prevents_side_effect():
    config = Config([item([{"type": "open", "path": "must-not-open.exe"}])])
    def observer(event):
        if event["state"] == "step_started": runtime.cancel_scenario(event["run_id"])
    result = runtime.run_scenario(config, "test-id", on_event=observer)
    assert result.status == "cancelled"
    runtime._open_path.assert_not_called()


def test_editor_preserves_unchanged_unsupported_legacy_steps(app):
    original = item([{"type": "future", "private_extension": [1, 2]}, {"type": "command", "text": "old value"}])
    widget = ScenarioEditor(Config([original]))
    assert widget.items() == [original]
    assert not widget.validate_current()
    assert widget.items() == [original]
    widget.name.setText("Edited")
    with pytest.raises(ValueError): widget.items()
    widget.enabled.setChecked(False)
    assert widget.items()[0]["steps"] == original["steps"]
    widget.deleteLater()


def test_run_snapshot_is_not_changed_by_ui_edits(monkeypatch):
    config = Config([item([{"type": "wait", "seconds": 0}, {"type": "wait", "seconds": 0}])])
    def observer(event):
        if event["state"] == "started":
            config.data["scenarios"]["items"][0]["steps"][1] = {"type": "open", "path": "not-in-snapshot.exe"}
    result = runtime.run_scenario(config, "test-id", on_event=observer)
    assert result.success and [x["type"] for x in result.steps] == ["wait", "wait"]
    runtime._open_path.assert_not_called()


def test_editor_event_delivery_and_cancel_button(app):
    config = Config([item([{"type": "wait", "seconds": 10}])])
    widget = ScenarioEditor(config)
    running, results = threading.Event(), []
    def worker():
        cancellation_manager().reset()
        results.append(runtime.run_scenario(config, "test-id", on_event=lambda e: running.set() if e["state"] == "step_started" else None))
    thread = threading.Thread(target=worker); thread.start()
    assert running.wait(2)
    app.processEvents()
    assert widget.cancel_button.isEnabled()
    widget.cancel_button.click()
    thread.join(2); app.processEvents()
    assert results[0].status == "cancelled"
    assert not widget.cancel_button.isEnabled()
    assert st("cancelled", widget.language) in widget.log.toPlainText()
    widget.deleteLater()


def test_fingerprint_is_canonical_and_rejects_changed_before_action():
    raw = item([{"type": "open", "path": "approved.exe"}])
    fingerprint = runtime.scenario_fingerprint(raw)
    reordered = {key: raw[key] for key in reversed(raw)}
    assert runtime.scenario_fingerprint(reordered) == fingerprint
    config = Config([raw])
    config.data["scenarios"]["items"][0]["steps"][0]["path"] = "changed.exe"
    result = runtime.run_scenario(config, "test-id", expected_fingerprint=fingerprint)
    assert result.status == "changed" and not result.success
    runtime._open_path.assert_not_called()


def test_fingerprint_matches_the_snapshot_that_executes(monkeypatch):
    raw = item([{"type": "open", "path": "approved.exe"}])
    config = Config([raw]); seen = []
    monkeypatch.setattr(runtime, "_open_path", lambda path: seen.append(path) or {"success": True})
    def observer(event):
        if event["state"] == "started":
            config.data["scenarios"]["items"][0]["steps"][0]["path"] = "unapproved.exe"
    result = runtime.run_scenario(config, "test-id", expected_fingerprint=runtime.scenario_fingerprint(raw), on_event=observer)
    assert result.success and seen == ["approved.exe"]


def test_run_button_requires_saved_identical_valid_draft(editor):
    requests = []
    editor.run_requested.connect(requests.append)
    assert editor.run_button.isEnabled()
    editor.run_button.click()
    assert requests == ["test-id"]
    runtime._open_path.assert_not_called(); runtime.subprocess.Popen.assert_not_called()
    editor.name.setText("Unsaved rename")
    assert not editor.run_button.isEnabled()
    editor.request_run()
    assert requests == ["test-id"]
    editor.config.data["scenarios"]["items"] = editor.items()
    editor.retranslate()
    assert editor.run_button.isEnabled()
    editor.run_button.click()
    assert requests == ["test-id", "test-id"]


def test_global_cancel_generation_does_not_resume_old_scenario():
    config = Config([item([{"type": "wait", "seconds": 10}])])
    started, results = threading.Event(), []
    def worker():
        cancellation_manager().reset()
        results.append(runtime.run_scenario(config, "test-id", on_event=lambda e: started.set() if e["state"] == "step_started" else None))
    thread = threading.Thread(target=worker); thread.start()
    assert started.wait(2)
    cancellation_manager().cancel("QA stop")
    cancellation_manager().reset()
    thread.join(2)
    assert not thread.is_alive() and results[0].status == "cancelled"
    assert runtime.run_scenario(Config([item()]), "test-id").success


def test_command_timeout_terminates_owned_process(monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: process)
    tick = iter([0, 0, 0, 2])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(tick, 2))
    with pytest.raises(TimeoutError):
        runtime._command({"command": ["fake"], "timeout": 1}, lambda: None, 300)
    assert process.terminated


def test_legacy_command_string_is_single_literal_executable(monkeypatch):
    process = FakeProcess(completed=True); launch = Mock(return_value=process)
    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    result = runtime.run_scenario(Config([item([{"type": "command", "command": "program with spaces.exe"}])]), "test-id")
    assert result.success and launch.call_args.args[0] == ["program with spaces.exe"]


def test_step_dialog_uses_typed_monitor_and_keeps_cancel_draft(app):
    original = {"type": "open", "path": "C:/a.exe", "monitor": 2, "extension": 123}
    dialog = ScenarioStepDialog(original)
    dialog.fields["monitor"].setText("3.5")
    dialog.accept(); assert dialog.result() != QDialog.Accepted
    dialog.fields["monitor"].setText("3")
    dialog.accept(); assert dialog.result_step["monitor"] == 3 and dialog.result_step["extension"] == 123
    assert original["monitor"] == 2
    dialog.deleteLater()


def test_owned_child_cleanup_if_output_reader_cannot_start(monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError("thread exhausted")))
    with pytest.raises(RuntimeError):
        runtime._command({"command": ["fake"]}, lambda: None, time.monotonic() + 10)
    assert process.terminated and process.stdout.closed and process.stderr.closed


def test_validation_text_and_error_reports_are_bounded():
    raw = item([{"type": "command", "command": ["program", *("x" * 8192 for _ in range(127))]}])
    report = validate_scenario(raw)
    assert not report.valid and any(x.code == "size" for x in report.issues)
    many = item([{"type": "command", "command": [None] * 128}] * 100)
    assert len(validate_scenario(many).issues) <= 100


def test_unconfirmed_owned_process_termination_is_not_reported_cancelled(monkeypatch):
    class Unstoppable(FakeProcess):
        def terminate(self): raise OSError("denied")
        def kill(self): raise OSError("denied")
        def wait(self, timeout=None): raise RuntimeError("still active")
    process = Unstoppable()
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: process)
    def fail(): raise runtime.OperationCancelled("stop")
    checks = iter([None, fail])
    def check():
        callback = next(checks, fail)
        if callback: callback()
    with pytest.raises(runtime.ScenarioCleanupError):
        runtime._command({"command": ["fake"]}, check, time.monotonic() + 10)


def test_different_scenario_ids_cannot_run_concurrently_in_one_profile():
    config = Config([item([{"type": "wait", "seconds": 10}]), item(id="second", name="Second", phrase="", aliases=[])])
    ready = threading.Event()
    def worker():
        cancellation_manager().reset()
        runtime.run_scenario(config, "test-id", on_event=lambda e: ready.set() if e["state"] == "step_started" else None)
    thread = threading.Thread(target=worker); thread.start()
    assert ready.wait(2)
    assert runtime.run_scenario(config, "second").status == "busy"
    runtime.cancel_scenario(runtime.active_scenarios(config)[0]["run_id"])
    thread.join(2)
    assert not thread.is_alive()


def test_global_run_capacity_is_bounded_even_across_profiles():
    configs, threads = [], []
    try:
        for _ in range(runtime.MAX_ACTIVE_TOTAL):
            config = Config([item([{"type": "wait", "seconds": 10}])]); configs.append(config)
            ready = threading.Event()
            def worker(c=config, event=ready):
                cancellation_manager().reset()
                runtime.run_scenario(c, "test-id", on_event=lambda e: event.set() if e["state"] == "step_started" else None)
            thread = threading.Thread(target=worker); thread.start(); threads.append(thread)
            assert ready.wait(2)
        assert len(runtime.active_scenarios()) == runtime.MAX_ACTIVE_TOTAL
        assert runtime.run_scenario(Config([item()]), "test-id").status == "busy"
    finally:
        for config in configs:
            for run in runtime.active_scenarios(config): runtime.cancel_scenario(run["run_id"])
        for thread in threads: thread.join(2)
    assert all(not t.is_alive() for t in threads)


def test_reader_capacity_failure_never_launches_and_releases_partial_slot(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(runtime, "_OUTPUT_SLOTS", slots)
    with pytest.raises(RuntimeError, match="output limit"):
        runtime._command({"command": ["fake"]}, lambda: None, time.monotonic()+10)
    runtime.subprocess.Popen.assert_not_called()
    assert slots.acquire(blocking=False)
    slots.release()


def test_spawn_failure_releases_output_slots(monkeypatch):
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(runtime, "_OUTPUT_SLOTS", slots)
    monkeypatch.setattr(runtime.subprocess, "Popen", Mock(side_effect=OSError("missing executable")))
    with pytest.raises(OSError): runtime._command({"command": ["fake"]}, lambda: None, time.monotonic()+10)
    assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
    slots.release(); slots.release()


@pytest.mark.parametrize("suffix", [".lnk", ".url", ".hta", ".pif", ".scr", ".reg", ".msi", ".msp", ".cpl", ".com", ".ps1", ".vbe", ".jse"])
def test_script_shortcut_installer_containers_always_high_risk(suffix):
    from jarvis.security import Risk
    for path in ("C:/user/name"+suffix, "C:/user/name"+suffix.upper(), " C:/user/name"+suffix+" . "):
        config = Config([item([{"type": "open", "path": path}])])
        assert runtime.scenario_risk(config, {"action": "run", "id": "test-id"}) == Risk.HIGH
        assert runtime.dry_run_scenario(config, "test-id")["valid"]
    runtime._open_path.assert_not_called()


def test_ordinary_executable_open_remains_medium_and_control_path_rejected():
    from jarvis.security import Risk
    config = Config([item([{"type": "open", "path": "C:/apps/ordinary.EXE"}])])
    assert runtime.scenario_risk(config, {"action": "run", "id": "test-id"}) == Risk.MEDIUM
    config.data["scenarios"]["items"][0]["steps"][0]["path"] = "C:/script.cmd\n"
    assert runtime.scenario_risk(config, {"action": "run", "id": "test-id"}) == Risk.HIGH
    assert runtime.run_scenario(config,"test-id").status == "invalid"
    runtime._open_path.assert_not_called()


def test_scenario_atomic_config_roundtrip_preserves_extensions(tmp_path, monkeypatch):
    from jarvis.config import ConfigManager
    for key in ("HOME", "LOCALAPPDATA", "APPDATA"):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setattr(ConfigManager, "apply_autostart", lambda *_: None)
    monkeypatch.setattr(ConfigManager, "apply_wake_service", lambda *_: None)
    config = ConfigManager(Path(__file__).parents[1] / 'config/default_settings.json')
    raw = item([{"path": "C:/open.exe", "required": True, "extra": {"a": 1}},
                {"type": "wait", "seconds": 0}, {"type": "command", "command": ["fake.exe", "a b"]}], custom={"preserve": [1, 2]})
    data = deepcopy(config.data)
    data.setdefault("scenarios", {}).update(items=[raw], future_option="keep")
    config.save(data)
    assert config.reload()["scenarios"]["items"] == [raw]
    assert config.get("scenarios.future_option") == "keep"
    original = config.paths.settings.read_bytes()
    import os
    replace = os.replace
    def fail(source, destination):
        if Path(destination) == config.paths.settings:
            raise OSError("QA atomic replacement failure")
        return replace(source, destination)
    monkeypatch.setattr(os, "replace", fail)
    changed = deepcopy(config.data); changed["scenarios"]["items"][0]["name"] = "Unsaved"
    with pytest.raises(OSError): config.save(changed)
    assert config.paths.settings.read_bytes() == original
    assert json.loads(original)["scenarios"]["items"] == [raw]


@pytest.mark.parametrize("language", LANGS)
def test_editor_narrow_layout_wraps_without_rebuilding(app, language):
    from jarvis.ui.responsive178 import WrapCheckBox
    widget = ScenarioEditor(Config([item()], language))
    original_controls = (widget.name, widget.steps, widget.list)
    widget.resize(450, 850); widget.show(); app.processEvents()
    assert widget.width() <= 470
    assert widget.splitter.orientation() == Qt.Vertical
    assert isinstance(widget.enabled, WrapCheckBox)
    assert widget.enabled.height() >= widget.enabled.heightForWidth(widget.enabled.width()) - 1
    for size in ((1000, 850), (450, 850), (980, 850)):
        widget.resize(*size); app.processEvents()
        assert (widget.name, widget.steps, widget.list) == original_controls
    assert widget.splitter.orientation() == Qt.Horizontal
    widget.close(); widget.deleteLater()
