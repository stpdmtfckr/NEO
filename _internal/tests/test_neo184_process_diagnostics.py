"""Pure process-attribution boundary tests: no Qt, audio or subprocess startup."""
import ast
import io
import logging
from pathlib import Path
import sys

import pytest

from jarvis.runtime import build_identity as identity
from jarvis.runtime import process_diagnostics184 as diagnostic
from jarvis.runtime.diagnostics import RedactingFormatter


class Config:
    def __init__(self, values=None):
        self.values = values or {}
        self.reads = []

    def get(self, key, default=None):
        self.reads.append(key)
        return self.values.get(key, default)


@pytest.fixture(autouse=True)
def isolated_context(monkeypatch):
    monkeypatch.setattr(diagnostic, "_context", {
        "build_revision": "unavailable", "parent_subset_sha256": "unavailable",
        "parent_subset_status": "unavailable"})


def rows(caplog):
    return [dict(piece.split("=", 1) for piece in record.getMessage().split(": ", 1)[1].split())
            for record in caplog.records if record.name == diagnostic.__name__]


def test_gui_identity_and_spawn_policy_are_parent_only(caplog, capsys):
    config = Config({"voice.sample_rate": 44100, "voice.stt_cpu_threads": 3,
                     "wake_word.enabled": True})
    with caplog.at_level(logging.INFO):
        diagnostic.record_gui_start(config, {"packaged_revision": "checkpoint21",
            "local_subset_sha256": "a" * 64, "subset_status": "mismatch"})
        diagnostic.record_spawn("microphone_capture", 9101, 1, configured_target_rate_hz=44100)
        diagnostic.record_spawn("system_loopback", 9102, 1,
            configured_echo_enabled=True, configured_system_transcription_enabled=False)
    gui, microphone, loopback = rows(caplog)
    assert gui["role"] == "gui" and gui["coverage"] == "core_owned_only"
    assert gui["fast_media_owner"] == gui["partial_asr_owner"] == "stt_wake"
    assert gui["configured_wake_enabled"] == "true"
    assert gui["configured_target_rate_hz"] == microphone["configured_target_rate_hz"] == "44100"
    assert microphone["capture_block_policy"] == "2048_first_ge_32000_else_1024"
    assert loopback["configured_capture_rate_hz"] == "48000"
    assert loopback["configured_capture_block_frames"] == "2048"
    assert loopback["configured_system_transcription_enabled"] == "false"
    for row in (gui, microphone, loopback):
        assert row["build_revision"] == "checkpoint21"
        assert row["parent_subset_sha256"] == "a" * 64
        assert row["parent_subset_status"] == "mismatch"
        assert row["readiness"] == "not_checked"
        assert row["identity_scope"] == "parent_startup_subset"
    for row in (microphone, loopback):
        assert row["owner_pid"] == gui["pid"]
        assert row["actual_capture_rate_hz"] == row["actual_capture_block_frames"] == "unknown"
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("bad", ["private/path\nforged=true", "api_key=PRIVATE_SENTINEL", [], {}, True, -1, 10**20])
def test_fields_reject_raw_settings_names_and_protocol_content(bad, caplog):
    with caplog.at_level(logging.INFO):
        diagnostic.record_gui_start(Config({"voice.sample_rate": bad}), {
            "packaged_revision": bad, "local_subset_sha256": bad, "subset_status": bad,
            "argv": "PRIVATE_SENTINEL", "env": "PRIVATE_SENTINEL"})
        diagnostic.record_spawn("stt_command", 3101, 1, argv=bad, env=bad,
            model_path=bad, device_name=bad, text=bad, configured_target_rate_hz=bad)
    gui, child = rows(caplog)
    assert gui["configured_target_rate_hz"] == child["configured_target_rate_hz"] == "unknown"
    assert gui["build_revision"] == gui["parent_subset_sha256"] == gui["parent_subset_status"] == "unavailable"
    output = "\n".join(record.getMessage() for record in caplog.records)
    assert "PRIVATE_SENTINEL" not in output and "forged=" not in output and "private/path" not in output
    assert not ({"argv", "env", "model_path", "device_name", "text"} & child.keys())


def test_unknown_role_never_echoes_caller_label(caplog):
    assert diagnostic.stt_role("NeoJarvis-STT-Wake") == "stt_wake"
    assert diagnostic.stt_role("private user text") == "stt_unspecified"
    assert diagnostic.stt_role([]) == "stt_unspecified"
    with caplog.at_level(logging.INFO):
        diagnostic.record_spawn("private user text", 100, 1)
        diagnostic.record_spawn("stt_command", True, 1)
        diagnostic.record_spawn("stt_command", 100, -1)
    assert not rows(caplog)


def test_gui_with_unavailable_parent_pid_still_records_its_own_identity(monkeypatch, caplog):
    monkeypatch.setattr(diagnostic.os, "getppid", lambda: 0)
    with caplog.at_level(logging.INFO):
        diagnostic.record_gui_start(Config(), {})
    assert rows(caplog)[0]["owner_pid"] == "unknown"


def test_logger_failure_cannot_interrupt_parent_spawn(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("PRIVATE_SENTINEL")
    monkeypatch.setattr(diagnostic.logger, "info", fail)
    diagnostic.record_gui_start(Config(), {})
    diagnostic.record_spawn("stt_wake", 100, 1)


def test_actual_parent_formatter_preserves_safe_attribution(monkeypatch):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    monkeypatch.setattr(diagnostic.logger, "handlers", [handler])
    monkeypatch.setattr(diagnostic.logger, "propagate", False)
    monkeypatch.setattr(diagnostic.logger, "level", logging.INFO)
    diagnostic.record_spawn("stt_wake", 900, 2, text="Bearer PRIVATE_SENTINEL",
                            model_path="/home/private/PRIVATE_SENTINEL")
    output = stream.getvalue()
    assert "role=stt_wake pid=900" in output and "process_generation=2" in output
    assert "PRIVATE_SENTINEL" not in output
    assert output.count("\n") == 1


def test_actual_app_fragment_reads_build_identity_once_and_children_reuse_it(monkeypatch, caplog, capsys, tmp_path):
    count = []
    expected = {"packaged_revision": "checkpoint21", "local_subset_sha256": "b" * 64,
                "subset_status": "uncompared"}
    def read(root):
        count.append(root)
        return dict(expected)
    monkeypatch.setattr(identity, "read_startup_identity", read)
    path = Path(__file__).resolve().parents[1] / "app.py"
    main = next(node for node in ast.parse(path.read_text()).body
                if isinstance(node, ast.FunctionDef) and node.name == "main")
    start = next(i for i, node in enumerate(main.body) if isinstance(node, ast.ImportFrom)
                 and node.module == "jarvis.runtime.build_identity")
    end = next(i for i, node in enumerate(main.body) if isinstance(node, ast.Expr)
               and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
               and node.value.func.id == "record_gui_start")
    fragment = ast.Module(body=main.body[start:end + 1], type_ignores=[])
    with caplog.at_level(logging.INFO):
        exec(compile(fragment, str(path), "exec"), {"root": tmp_path, "sys": sys,
             "logging": logging, "config": Config(), "__name__": "parent-startup-test"})
        for generation in range(1, 4):
            diagnostic.record_spawn("stt_wake", 500 + generation, generation)
    assert count == [tmp_path]
    assert capsys.readouterr().out.splitlines() == ["NEO source identity: " + " ".join(f"{key}={value}" for key, value in expected.items())]
    assert len(rows(caplog)) == 4
    assert all(row["parent_subset_sha256"] == "b" * 64 for row in rows(caplog))


def test_failed_identity_receipt_keeps_existing_fixed_message(monkeypatch, tmp_path):
    def fail(*args):
        raise OSError("PRIVATE_SENTINEL")
    monkeypatch.setattr(identity, "read_startup_identity", fail)
    receipt, line = identity.startup_identity_receipt(tmp_path)
    assert receipt == {}
    assert line == "NEO source identity: packaged_status=unavailable subset_status=unavailable reason=diagnostic_error"
