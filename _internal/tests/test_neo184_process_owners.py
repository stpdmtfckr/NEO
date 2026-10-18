"""Execute production owner classes with inert OS/Qt boundaries.

AST extraction avoids importing Qt, audio drivers, ASR libraries or the ambient
learning stack. Class method bodies are the actual source, not test replicas.
These tests establish parent ownership/protocol invariants, not Windows capture
or actual Qt event-loop behavior.
"""
import ast
import base64
from contextlib import contextmanager, nullcontext
import io
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from jarvis.runtime import process_diagnostics184 as diagnostic

ROOT = Path(__file__).resolve().parents[1]


class Signal:
    def __init__(self, *args): self.values = []
    def connect(self, callback): self.callback = callback
    def emit(self, value): self.values.append(value)


class Timer:
    def __init__(self, *args): self.timeout = Signal(); self.running = False
    def setTimerType(self, value): pass
    def setInterval(self, value): self.interval = value
    def start(self): self.running = True
    def stop(self): self.running = False


class Object:
    def __init__(self, *args): pass


class QuietThread:
    def __init__(self, *args, **kwargs): self.kwargs = kwargs; self.started = False
    def start(self): self.started = True
    def is_alive(self): return self.started


class Pipe(io.StringIO):
    def close(self): self.was_closed = True


class Process:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        self.stdin, self.stdout, self.stderr = Pipe(), Pipe(), Pipe()
        self.terminated = self.killed = 0
    def poll(self): return self.returncode
    def terminate(self): self.terminated += 1; self.returncode = -15
    def kill(self): self.killed += 1; self.returncode = -9
    def wait(self, **kwargs): self.returncode = self.returncode or 0; return self.returncode


class Spawn:
    def __init__(self): self.processes = []; self.calls = []; self.error = None; self.after = None
    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error: raise self.error
        process = Process(2000 + len(self.processes))
        self.processes.append(process)
        if self.after: self.after(process)
        return process


class Config:
    def __init__(self, values=None): self.values = values or {}
    def get(self, key, default=None): return self.values.get(key, default)


def load_owner(path, class_name, spawn):
    source = ROOT / path
    tree = ast.parse(source.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "__file__": str(source), "__name__": "inert_parent_owner", "os": os,
        "Path": Path, "json": json, "base64": base64, "queue": queue, "time": time,
        "contextmanager": contextmanager, "nullcontext": nullcontext,
        "logger": logging.getLogger("inert_parent_owner"), "subprocess": SimpleNamespace(
            Popen=spawn, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
            TimeoutExpired=subprocess.TimeoutExpired),
        "threading": SimpleNamespace(Thread=QuietThread, RLock=threading.RLock, Event=threading.Event),
        "worker_command": lambda module, **kwargs: ["inert-helper", module],
        "hidden_process_kwargs": lambda: {}, "record_spawn": diagnostic.record_spawn,
        "stt_role": diagnostic.stt_role, "QObject": Object, "QTimer": Timer,
        "Qt": SimpleNamespace(CoarseTimer=1), "Signal": Signal,
        "_SttWorkerCrashed": type("_SttWorkerCrashed", (RuntimeError,), {}),
    }
    exec(compile(module, str(source), "exec"), namespace)
    return namespace[class_name]


def rows(caplog):
    return [dict(piece.split("=", 1) for piece in record.getMessage().split(": ", 1)[1].split())
            for record in caplog.records if record.name == diagnostic.__name__]


@pytest.mark.parametrize("name,role", [
    ("NeoJarvis-STT-Command", "stt_command"), ("NeoJarvis-STT-Wake", "stt_wake"),
    ("NeoJarvis-STT-AmbientSystem", "stt_ambient_system"), ("PRIVATE_SENTINEL", "stt_unspecified")])
def test_stt_spawn_reuse_interrupt_restart_and_cancel(name, role, caplog):
    spawn = Spawn()
    owner = load_owner("jarvis/voice.py", "TranscriptionService", spawn)(name)
    with caplog.at_level(logging.INFO):
        owner._start_locked()
        first_queue = owner._responses
        first_process = owner._process
        owner._start_locked()
        assert len(rows(caplog)) == len(spawn.processes) == 1
        assert first_process.stdin.getvalue() == ""  # Logging does not write to IPC.
        owner.interrupt()
        assert first_process.killed == 1
        owner._start_locked()
        assert owner._responses is not first_queue
        assert owner._process is not first_process
        owner.cancel()
        with pytest.raises(RuntimeError): owner._start_locked()
    result = rows(caplog)
    assert [row["role"] for row in result] == [role, role]
    assert [row["pid"] for row in result] == ["2000", "2001"]
    assert [row["process_generation"] for row in result] == ["1", "2"]
    assert all(row["owner_pid"] == str(os.getpid()) and row["readiness"] == "not_checked" for row in result)
    assert "PRIVATE_SENTINEL" not in "\n".join(record.getMessage() for record in caplog.records)
    # Old reader remains bound to its old response queue after replacement.
    first_process.stdout = Pipe('{"id":"old","ok":true}\n')
    owner._read_stdout(first_process, first_queue)
    assert first_queue.get_nowait()["id"] == "old" and owner._responses.empty()


def test_spawn_failure_does_not_claim_pid_or_consume_generation(caplog):
    spawn = Spawn(); spawn.error = OSError("PRIVATE_SENTINEL")
    owner = load_owner("jarvis/voice.py", "TranscriptionService", spawn)()
    with caplog.at_level(logging.INFO):
        with pytest.raises(OSError): owner._start_locked()
        assert owner._process_generation == 0 and not rows(caplog)
        spawn.error = None
        owner._start_locked()
    assert rows(caplog)[0]["process_generation"] == "1"


def test_logger_error_does_not_strand_stt_child_or_skip_reader_start(monkeypatch):
    spawn = Spawn()
    owner = load_owner("jarvis/voice.py", "TranscriptionService", spawn)()
    def fail(*args, **kwargs): raise OSError("PRIVATE_SENTINEL")
    monkeypatch.setattr(diagnostic.logger, "info", fail)
    owner._start_locked()
    assert owner._process is spawn.processes[0]
    assert owner._stdout_thread.started and owner._stderr_thread.started
    owner.cancel()
    assert spawn.processes[0].terminated == 1


def test_stt_request_and_response_remain_single_json_lines(caplog, capsys):
    spawn = Spawn()
    owner = load_owner("jarvis/voice.py", "TranscriptionService", spawn)("NeoJarvis-STT-Command")
    with caplog.at_level(logging.INFO):
        owner._start_locked()
        response = {"id": "request-1", "ok": True, "text": "PRIVATE_SENTINEL"}
        owner._process.stdout = Pipe(json.dumps(response) + "\n")
        owner._read_stdout(owner._process, owner._responses)
        request = {"id": "request-1", "cancel_serial": 0, "cmd": "transcribe", "purpose": "command"}
        assert owner._request_once_locked(request, 1) == "PRIVATE_SENTINEL"
    assert owner._process.stdin.getvalue() == json.dumps(request, ensure_ascii=False) + "\n"
    assert owner._responses.empty()
    assert "PRIVATE_SENTINEL" not in "\n".join(record.getMessage() for record in caplog.records)
    assert capsys.readouterr().out == ""


def test_audio_spawn_reuse_restart_close_and_unchanged_event_payload(caplog):
    spawn = Spawn()
    owner = load_owner("jarvis/ui/audio_capture.py", "AudioCaptureController", spawn)(Config())
    owner.worker_event = Signal()
    owner._last_start_payload = {"target_rate": 16000, "device": "PRIVATE_SENTINEL"}
    with caplog.at_level(logging.INFO):
        owner._start_worker_locked()
        owner._start_worker_locked()
        assert len(spawn.processes) == 1
        event = {"type": "started", "session": 1, "sample_rate": 48000, "device": "PRIVATE_SENTINEL"}
        owner._process.stdout = Pipe(json.dumps(event) + "\n")
        owner._read_stdout(owner._process)
        assert owner.worker_event.values == [event]
        first = owner._process
        first.returncode = -1
        owner._start_worker_locked()
        owner.close()
        with pytest.raises(RuntimeError): owner._start_worker_locked()
    result = rows(caplog)
    assert [row["process_generation"] for row in result] == ["1", "2"]
    assert [row["pid"] for row in result] == ["2000", "2001"]
    assert result[0]["configured_target_rate_hz"] == "16000"
    assert all(row["actual_capture_rate_hz"] == "unknown" for row in result)
    assert "PRIVATE_SENTINEL" not in "\n".join(record.getMessage() for record in caplog.records)
    assert not owner._poll_timer.running


def test_loopback_echo_only_spawn_restart_does_not_enable_asr(caplog):
    spawn = Spawn()
    owner = load_owner("jarvis/adaptation/ambient.py", "SystemAudioSpeechMonitor", spawn)(
        Config({"ambient_learning.system_audio_device": "PRIVATE_SENTINEL"}), SimpleNamespace())
    with caplog.at_level(logging.INFO):
        first = owner._start_soundcard_worker()
        first.returncode = -1
        second = owner._start_soundcard_worker()
    assert first.pid != second.pid and owner._stt is None
    result = rows(caplog)
    assert [row["process_generation"] for row in result] == ["1", "2"]
    assert all(row["configured_echo_enabled"] == "true" and row["configured_system_transcription_enabled"] == "false" for row in result)
    assert "PRIVATE_SENTINEL" not in "\n".join(record.getMessage() for record in caplog.records)
    assert first.stdin.getvalue() == second.stdin.getvalue() == ""


def test_loopback_cancel_during_spawn_reaps_candidate_without_readiness_claim(caplog):
    class Stop:
        stopped = False
        def wait(self, timeout): return self.stopped
        def is_set(self): return self.stopped
        def set(self): self.stopped = True
    spawn = Spawn()
    owner = load_owner("jarvis/adaptation/ambient.py", "SystemAudioSpeechMonitor", spawn)(Config(), SimpleNamespace())
    owner._stop = Stop()
    spawn.after = lambda process: owner._stop.set()
    with caplog.at_level(logging.INFO): owner._run()
    assert owner._process is None and len(spawn.processes) == 1
    assert spawn.processes[0].terminated == 1
    assert rows(caplog)[0]["readiness"] == "not_checked"
    assert not any("System echo capture started" in record.getMessage() for record in caplog.records)


@pytest.mark.skipif(os.environ.get("NEO_PROCESS_DIAGNOSTICS_QT_TESTS") != "1",
                    reason="Actual Qt construction requires the separately approved quiet slot")
def test_actual_qt_audio_owner_keeps_json_signal_and_restart_framing(monkeypatch, caplog):
    from PySide6.QtCore import QCoreApplication
    from jarvis.ui import audio_capture
    application = QCoreApplication.instance() or QCoreApplication([])
    spawn = Spawn()
    monkeypatch.setattr(audio_capture.subprocess, "Popen", spawn)
    monkeypatch.setattr(audio_capture.threading, "Thread", QuietThread)
    monkeypatch.setattr(audio_capture, "worker_command", lambda *args, **kwargs: ["inert-helper"])
    owner = audio_capture.AudioCaptureController(Config())
    owner._poll_timer.stop()
    owner._last_start_payload = {"target_rate": 16000}
    levels = []
    owner.level_changed.connect(levels.append)
    try:
        with caplog.at_level(logging.INFO):
            owner._start_worker_locked()
            event = {"type": "level", "session": owner._session, "value": 42}
            owner._process.stdout = Pipe(json.dumps(event) + "\n")
            owner._read_stdout(owner._process)
            assert levels == [42]
            assert owner._process.stdin.getvalue() == ""
            owner._process.returncode = -1
            owner._start_worker_locked()
        assert [row["process_generation"] for row in rows(caplog)] == ["1", "2"]
        assert all(row["role"] == "microphone_capture" for row in rows(caplog))
    finally:
        owner.close()
    assert application is not None and not owner._poll_timer.isActive()
