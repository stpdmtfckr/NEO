"""Real Settings construction never waits for its optional cache statistics."""
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from jarvis.config import ConfigManager
from jarvis.game_web.cache import CACHE_PROVENANCE, CACHE_SCHEMA, GameWebCache
from jarvis.ui.settings_dialog import SettingsDialog
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(SettingsDialog, "_refresh_microphones_async", lambda self: None)
    return ConfigManager(Path(__file__).parents[1] / "config" / "default_settings.json")


def pump(app, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)
    assert predicate()


def test_slow_cache_does_not_hold_constructor_or_qt_and_updates_on_qt(app, config, monkeypatch):
    gate = threading.Event()
    workers, updates, beats = [], [], []
    main = threading.get_ident()

    def scan(_config):
        workers.append(threading.get_ident())
        gate.wait(5)
        return {"entries": 43, "bytes": 1048576}

    original = SettingsDialog._status_text

    def status(self, widget, template, **values):
        if widget is getattr(self, "game_web_cache_stats", None):
            updates.append(threading.get_ident())
        return original(self, widget, template, **values)

    monkeypatch.setattr(GameWebCache, "read_stats", staticmethod(scan))
    monkeypatch.setattr(SettingsDialog, "_status_text", status)
    dialog = SettingsDialog(config)
    heartbeat = QTimer()
    heartbeat.setInterval(5)
    heartbeat.timeout.connect(lambda: beats.append(True))
    heartbeat.start()
    try:
        # The cache is still blocked after construction returns.
        pump(app, lambda: bool(workers) and len(beats) >= 5)
        assert not gate.is_set() and workers == [workers[0]] and workers[0] != main
        assert "43" not in dialog.game_web_cache_stats.text()
        gate.set()
        pump(app, lambda: "43" in dialog.game_web_cache_stats.text())
        assert updates and set(updates) == {main}
    finally:
        gate.set()
        heartbeat.stop()
        dialog.reject()
        dialog.deleteLater()
        app.processEvents()


def test_close_discards_late_stats_without_widget_updates(app, config, monkeypatch):
    gate = threading.Event()

    def scan(_config):
        gate.wait(5)
        return {"entries": 917, "bytes": 0}

    monkeypatch.setattr(GameWebCache, "read_stats", staticmethod(scan))
    dialog = SettingsDialog(config)
    before = dialog.game_web_cache_stats.text()
    try:
        dialog.reject()
        assert dialog._game_cache_stats_closed158
        gate.set()
        pump(app, lambda: dialog._game_cache_stats_worker158 is None)
        assert dialog.game_web_cache_stats.text() == before
        dialog._refresh_game_web_cache_stats()
        assert dialog._game_cache_stats_worker158 is None
    finally:
        gate.set()
        dialog.deleteLater()
        app.processEvents()


def test_refreshes_coalesce_and_drop_snapshot_taken_before_cache_clear(app, config, monkeypatch):
    gate = threading.Event()
    calls = []

    def scan(_config):
        calls.append(True)
        if len(calls) == 1:
            gate.wait(5)
            return {"entries": 999, "bytes": 1}
        return {"entries": 321, "bytes": 0}

    monkeypatch.setattr(GameWebCache, "read_stats", staticmethod(scan))
    dialog = SettingsDialog(config)
    try:
        pump(app, lambda: len(calls) == 1)
        for _ in range(100):
            dialog._refresh_game_web_cache_stats()
        assert len(calls) == 1
        gate.set()
        pump(app, lambda: dialog._game_cache_stats_worker158 is None)
        assert len(calls) == 2
        assert "321" in dialog.game_web_cache_stats.text()
        assert "999" not in dialog.game_web_cache_stats.text()
    finally:
        gate.set()
        dialog.reject()
        dialog.deleteLater()
        app.processEvents()


def test_read_only_cache_stats_never_migrates_or_deletes_files(tmp_path):
    config = SimpleNamespace(paths=SimpleNamespace(cache=tmp_path / "cache"))
    assert GameWebCache.read_stats(config) == {"entries": 0, "bytes": 0}
    assert not config.paths.cache.exists()
    base = config.paths.cache / "game_web"
    answers = base / "answers" / "example"
    answers.mkdir(parents=True)
    valid = answers / "valid.json"
    valid.write_text(json.dumps({"schema": CACHE_SCHEMA, "provenance": CACHE_PROVENANCE,
                                 "provider": "searchapi_google", "answer": "Cached fact"}))
    invalid = answers / "old.json"
    invalid.write_text("{old cache")
    legacy = base / "google_ai_profile" / "local-data"
    legacy.parent.mkdir()
    legacy.write_text("legacy file")
    before = {path: path.read_bytes() for path in base.rglob("*") if path.is_file()}
    assert GameWebCache.read_stats(config) == {"entries": 1, "bytes": valid.stat().st_size}
    assert {path: path.read_bytes() for path in base.rglob("*") if path.is_file()} == before
