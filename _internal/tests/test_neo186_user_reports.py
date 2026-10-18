"""186: user-reported items USER185-02 (launch freeze), USER185-04 (STT switch), USER185-11 (reflection)."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ------------------------------------------------------------ USER185-02

class _Config:
    def get(self, key, default=None):
        return default


def test_vocabulary_scan_never_runs_on_the_gui_thread(monkeypatch):
    import jarvis.runtime.speech_understanding as su
    calls: list[str] = []
    started = threading.Event()

    def slow_installed(limit):
        calls.append(threading.current_thread().name)
        started.set()
        time.sleep(0.3)
        return ["QA Installed App"]
    monkeypatch.setattr(su, "installed_application_terms", slow_installed)
    real_os = su.os

    class _WindowsOs:                       # only this module sees a Windows os.name
        name = "nt"

        def __getattr__(self, item):
            return getattr(real_os, item)
    monkeypatch.setattr(su, "os", _WindowsOs())
    monkeypatch.setattr(su.SpeechUnderstandingEngine, "_running_names", lambda self, limit=120: [])
    engine = su.SpeechUnderstandingEngine(_Config())
    began = time.perf_counter()
    quick = engine._refresh_entities()                         # main thread: immediate, no disk walk
    assert time.perf_counter() - began < 0.2
    assert not any(e.canonical == "QA Installed App" for e in quick)
    assert started.wait(2.0) and calls and calls[0] != threading.main_thread().name
    engine._entity_refresh_thread186.join(2.0)
    assert any(e.canonical == "QA Installed App" for e in engine._refresh_entities())


def test_speech_journal_is_written_off_the_gui_thread():
    import jarvis.runtime.speech_understanding as su
    seen: list[str] = []
    done = threading.Event()

    class Memory:
        def record_speech_resolution(self, payload):
            seen.append(threading.current_thread().name); done.set()
    engine = su.SpeechUnderstandingEngine(_Config(), agent=type("A", (), {"context_memory": Memory()})())
    result = su.SpeechResolution(raw="открой qa", text="открой qa")
    engine._record_resolution(result)
    assert engine.last_resolution is result
    assert done.wait(2.0) and seen[0] != threading.main_thread().name


# ------------------------------------------------------------ USER185-11

def test_glass_reflection_follows_the_head_relative_to_the_light():
    from PySide6.QtWidgets import QApplication
    from jarvis.ui.robot_widget import RobotWidget
    QApplication.instance() or QApplication([])
    robot = RobotWidget()
    neutral = robot._reflection_geometry186(0.0, 0.0, 0.0)
    right = robot._reflection_geometry186(12.0, 0.0, 0.0)
    left = robot._reflection_geometry186(-12.0, 0.0, 0.0)
    tilted = robot._reflection_geometry186(0.0, 0.0, 6.0)
    assert right[0] < neutral[0] < left[0]                     # the band slides opposite to the head
    assert left[3] > neutral[3] > right[3]                     # facing the light (upper left) is brighter
    assert tilted[2] == -6.0                                   # world orientation is kept under tilt
    assert robot._reflection_geometry186(3.0, -2.0, 1.0) == robot._reflection_geometry186(3.0, -2.0, 1.0)
    robot.resize(420, 300)
    robot.grab()                                               # the clipped band paints without errors
    assert getattr(robot, "_reflection186", None) is not None
    robot.deleteLater()


# ------------------------------------------------------------ USER185-04

def test_stt_switch_reports_only_the_latest_generation():
    source = (ROOT / "jarvis/ui/main_window.py").read_text(encoding="utf-8")
    block = source.split("def _start_stt_switch186", 1)[1].split("\n    def _preload_stt_models", 1)[0]
    assert "max(300.0" in block and "stt_switch_signal186.emit(generation" in block
    assert "generation != int(getattr(self, \"_stt_switch_generation186\", 0))" in block
    assert "self._start_stt_switch186()" in source.split("speech_changed = ", 1)[1].split("def _reset_conversation", 1)[0]
