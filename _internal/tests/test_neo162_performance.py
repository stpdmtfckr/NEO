"""Bounded-work regressions; these measure operations, not a Windows CPU trace."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from jarvis.adaptation.maintenance import AdaptiveMaintenance
from jarvis.gaming.manager import GameAdapter, GameManager, GamePresenceService
from jarvis.integrations import system_monitor as monitor
from jarvis.runtime.desktop_services import DesktopServices
from jarvis.runtime.event_store import EventStore


def test_game_scan_queries_command_line_only_for_ambiguous_candidates(monkeypatch):
    processes = [SimpleNamespace(pid=i, info={"pid": i, "name": "service.exe", "exe": "C:/service.exe", "create_time": i+1}, cmdline=Mock(return_value=["service"])) for i in range(1, 201)]
    java = SimpleNamespace(pid=300, info={"pid": 300, "name": "javaw.exe", "exe": "C:/javaw.exe", "create_time": 42}, cmdline=Mock(return_value=["javaw", "net.minecraft.client.main.Main"]))
    game = SimpleNamespace(pid=301, info={"pid": 301, "name": "Darktide.exe", "exe": "C:/Darktide.exe", "create_time": 43}, cmdline=Mock())
    processes.extend((java, game))
    attrs_seen = []
    def iterate(attrs):
        attrs_seen.append(attrs)
        return iter(processes)
    monkeypatch.setattr("jarvis.gaming.manager.psutil.process_iter", iterate)
    manager = GameManager()
    manager.adapters = [GameAdapter("minecraft", "Minecraft", ("javaw.exe",), process_markers=("minecraft",), marker_required_processes=("javaw.exe",)), *[a for a in manager.adapters if a.game_id != "minecraft"]]
    matches = manager.detect_all()
    assert {adapter.game_id for adapter, _ in matches} == {"minecraft", "darktide"}
    assert "cmdline" not in attrs_seen[0]
    assert sum(proc.cmdline.call_count for proc in processes) == 1
    print("game scan workload: 202 process cmdline reads before; 1 required candidate read now")


def test_missing_command_line_marker_fails_closed(monkeypatch):
    import psutil
    proc = SimpleNamespace(pid=3, info={"pid": 3, "name": "java.exe", "exe": "C:/java.exe", "create_time": 10}, cmdline=Mock(side_effect=psutil.AccessDenied(3)))
    monkeypatch.setattr("jarvis.gaming.manager.psutil.process_iter", lambda attrs: [proc])
    manager = GameManager()
    manager.adapters = [GameAdapter("game", "Game", ("java.exe",), process_markers=("game",), marker_required_processes=("java.exe",))]
    assert manager.detect_all() == []


def test_equivalent_game_managers_keep_the_shared_presence_cache(monkeypatch):
    monkeypatch.setattr("jarvis.gaming.manager._foreground_pid", lambda: None)
    calls = []
    monkeypatch.setattr(GameManager, "detect_all", lambda self: calls.append(1) or [])
    first = GameManager()
    service = GamePresenceService(first)
    assert service.snapshot() is None
    service.adopt_manager(first)
    service.snapshot()
    service.adopt_manager(GameManager())
    service.snapshot()
    assert len(calls) == 1
    changed = GameManager()
    changed.adapters = []
    service.adopt_manager(changed)
    service.snapshot()
    assert len(calls) == 2


def test_missing_sensor_provider_is_cached_and_retried(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(monitor, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(monitor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(monitor, "_PROBES", monitor._ProbeCache())
    run = Mock(return_value=None)
    monkeypatch.setattr(monitor, "_run_hidden", run)
    for _ in range(20):
        assert monitor._lhm_sensors() == []
        clock[0] += .25
    assert run.call_count == 1
    clock[0] += 31
    monitor._lhm_sensors()
    assert run.call_count == 2
    print("missing LHM workload: 20 PowerShell starts before; 1 start per negative-cache window now")


def test_native_probe_success_cache_is_short_and_defensive(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(monitor.time, "monotonic", lambda: clock[0])
    cache = monitor._ProbeCache()
    probe = Mock(return_value=[{"temperature": 40}])
    first = cache.read("sensor", probe)
    first[0]["temperature"] = -1
    assert cache.read("sensor", probe)[0]["temperature"] == 40
    clock[0] += 2.1
    cache.read("sensor", probe)
    assert probe.call_count == 2


def test_concurrent_status_requests_share_one_measurement(monkeypatch):
    monkeypatch.setattr(monitor, "_SNAPSHOTS", monitor._ProbeCache())
    barrier = threading.Barrier(8)
    count = []
    def collect():
        count.append(1)
        time.sleep(.025)  # Simulates a native measurement, no actual device I/O.
        return {"cpu": {"percent": 12.0}}
    monkeypatch.setattr(monitor, "_collect_snapshot", collect)
    def caller(_):
        barrier.wait(timeout=2)
        return monitor.snapshot()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(caller, range(8)))
    assert len(count) == 1
    assert len(results) == 8
    results[0]["cpu"]["percent"] = 99
    assert results[1]["cpu"]["percent"] == 12
    print("simultaneous status requests: 8 full probes before; 1 full probe now")


def test_gpu_identity_reuses_static_result_and_negative_nvidia(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(monitor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(monitor, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(monitor, "_PROBES", monitor._ProbeCache())
    nvidia = Mock(return_value=[])
    identity = Mock(return_value=[{"name": "Integrated GPU"}])
    monkeypatch.setattr(monitor, "_read_nvidia_gpus", nvidia)
    monkeypatch.setattr(monitor, "_read_windows_gpu_identity", identity)
    for _ in range(10):
        assert monitor._gpu_snapshot([])[0]["name"] == "Integrated GPU"
        clock[0] += 1
    assert nvidia.call_count == identity.call_count == 1
    clock[0] += 31
    monitor._gpu_snapshot([])
    assert nvidia.call_count == 2
    assert identity.call_count == 1


def test_cpu_sensor_is_not_reported_as_gpu(monkeypatch):
    monkeypatch.setattr(monitor, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(monitor, "_PROBES", monitor._ProbeCache())
    monkeypatch.setattr(monitor, "_read_nvidia_gpus", lambda: [])
    assert monitor._gpu_snapshot([{"Name": "CPU Package", "SensorType": "Temperature", "Value": 40, "Parent": "/intelcpu/0"}]) == []


def make_service(tmp_path):
    # Avoid watcher startup and index configuration: exercise the actual drain
    # and persistent store against a temporary observed directory.
    service = DesktopServices.__new__(DesktopServices)
    service.config = SimpleNamespace(get=lambda key, default=None: {"activity.enabled": True}.get(key, default))
    service.store = EventStore(tmp_path / "profile")
    service._queue = queue.Queue()
    service._stop = threading.Event()
    service._activity_roots = [tmp_path / "observed"]
    service._activity_roots[0].mkdir()
    service._download_roots = []
    service.downloads = Mock()
    return service


def test_watcher_burst_is_sliced_and_committed_once(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    for i in range(128):
        service._queue.put(("created", str(service._activity_roots[0] / f"file{i}.txt"), ""))
    actual_connect = service.store.connect
    commits = []
    @contextmanager
    def counted_connect():
        commits.append(1)
        with actual_connect() as db:
            yield db
    monkeypatch.setattr(service.store, "connect", counted_connect)
    assert service._drain_file_events({}, budget_seconds=10) == 24
    assert service._queue.qsize() == 104
    assert len(commits) == 1
    assert len(service.store.query(kind="activity.file.", limit=200)) == 24
    print("watcher burst: 128 writes in one turn before; max 24 events / 8ms work slice and 1 activity commit now")


def test_watcher_time_budget_yields_without_dropping_remainder(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    for i in range(8):
        service._queue.put(("created", str(service._activity_roots[0] / f"file{i}.txt"), ""))
    clock = [100.0]
    def tick():
        clock[0] += .005
        return clock[0]
    monkeypatch.setattr("jarvis.runtime.desktop_services.time.monotonic", tick)
    assert service._drain_file_events({}) == 1
    assert service._queue.qsize() == 7


def test_watcher_stop_does_not_drain_pending_queue(tmp_path):
    service = make_service(tmp_path)
    service._queue.put(("created", str(service._activity_roots[0] / "one.txt"), ""))
    service._stop.set()
    assert service._drain_file_events({}) == 0
    assert service._queue.qsize() == 1


def test_batched_activity_retains_privacy_and_notification_boundary(tmp_path, monkeypatch):
    store = EventStore(tmp_path)
    monkeypatch.setattr(store.policy, "allows_event", lambda kind, payload: payload.get("path") != "private.txt")
    assert store.record_activity_batch([
        ("activity.file.created", {"path": "allowed.txt"}),
        ("activity.file.modified", {"path": "private.txt"}),
        ("download.completed", {"path": "allowed.txt"}),
    ]) == 1
    assert len(store.query(kind="activity.file.")) == 1
    assert store.pending() == []


def test_async_maintenance_reserves_before_thread_launch(monkeypatch):
    store = SimpleNamespace(available=True, pending_events=lambda **kwargs: [], decay=Mock(return_value=1), prune=Mock(return_value=1))
    maintenance = AdaptiveMaintenance(store)
    created = []
    class DeferredThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(self)
        def start(self):
            pass  # The competing request arrives before the worker is scheduled.
    monkeypatch.setattr("jarvis.adaptation.maintenance.threading.Thread", DeferredThread)
    assert maintenance.maybe_run_async(force=True)
    assert not maintenance.maybe_run_async(force=True)
    assert maintenance.run(force=True) == {"ran": False, "reason": "already_running"}
    assert len(created) == 1
    created[0].kwargs["target"](*created[0].kwargs["args"])
    assert store.prune.call_count == store.decay.call_count == 1
    assert not maintenance._running


def test_maintenance_thread_creation_failure_releases_reservation(monkeypatch):
    maintenance = AdaptiveMaintenance(SimpleNamespace(available=True))
    monkeypatch.setattr("jarvis.adaptation.maintenance.threading.Thread", Mock(side_effect=RuntimeError("no thread")))
    try:
        maintenance.maybe_run_async(force=True)
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected launch failure")
    assert not maintenance._running


def test_activity_batch_retries_once_without_replaying_downloads(tmp_path, monkeypatch):
    import sqlite3
    service = make_service(tmp_path)
    service.config = SimpleNamespace(get=lambda key, default=None: {"activity.enabled": True, "security.smart_downloads_enabled": True}.get(key, default))
    service._download_roots = service._activity_roots
    service._queue.put(("created", str(service._activity_roots[0] / "one.txt"), ""))
    clock = [100.0]
    monkeypatch.setattr("jarvis.runtime.desktop_services.time.monotonic", lambda: clock[0])
    original = service.store.record_activity_batch
    calls = []
    def flaky(records, **kwargs):
        calls.append(kwargs["event_ids"])
        if len(calls) == 1:
            raise sqlite3.OperationalError("database locked")
        return original(records, **kwargs)
    monkeypatch.setattr(service.store, "record_activity_batch", flaky)
    assert service._drain_file_events({}) == 1
    assert service._activity_retry is not None
    for _ in range(20):
        assert service._drain_file_events({}) == 0
    assert len(calls) == service.downloads.on_file.call_count == 1
    clock[0] += 2.1
    assert service._drain_file_events({}) == 0
    assert len(calls) == 2 and calls[0] == calls[1]
    assert service.downloads.on_file.call_count == 1
    assert service._activity_retry is None
    assert len(service.store.query(kind="activity.file.")) == 1


def test_activity_retry_is_idempotent_after_ambiguous_commit(tmp_path, monkeypatch):
    import sqlite3
    service = make_service(tmp_path)
    service._queue.put(("created", str(service._activity_roots[0] / "one.txt"), ""))
    clock = [100.0]
    monkeypatch.setattr("jarvis.runtime.desktop_services.time.monotonic", lambda: clock[0])
    original = service.store.record_activity_batch
    attempts = []
    def flaky(records, **kwargs):
        original(records, **kwargs)
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("commit result unavailable")
    monkeypatch.setattr(service.store, "record_activity_batch", flaky)
    service._drain_file_events({})
    clock[0] += 2.1
    service._drain_file_events({})
    assert len(attempts) == 2
    assert len(service.store.query(kind="activity.file.")) == 1


def test_activity_retry_rechecks_current_privacy_policy(tmp_path, monkeypatch):
    import sqlite3
    service = make_service(tmp_path)
    service._queue.put(("created", str(service._activity_roots[0] / "private.txt"), ""))
    original = service.store.record_activity_batch
    monkeypatch.setattr(service.store, "record_activity_batch", Mock(side_effect=sqlite3.OperationalError("locked")))
    service._drain_file_events({})
    service._activity_retry["retry_at"] = 0
    monkeypatch.setattr(service.store, "record_activity_batch", original)
    monkeypatch.setattr(service.store.policy, "allows_event", lambda kind, payload: False)
    assert service._flush_activity_batch()
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert service._activity_retry is None


def test_failed_activity_retries_are_bounded_and_report_loss(tmp_path, monkeypatch, caplog):
    import sqlite3
    service = make_service(tmp_path)
    for i in range(128):
        service._queue.put(("created", str(service._activity_roots[0] / f"file{i}.txt"), ""))
    clock = [100.0]
    monkeypatch.setattr("jarvis.runtime.desktop_services.time.monotonic", lambda: clock[0])
    failing = Mock(side_effect=sqlite3.OperationalError("locked"))
    monkeypatch.setattr(service.store, "record_activity_batch", failing)
    assert service._drain_file_events({}) == 24
    assert len(service._activity_retry["records"]) == 24
    assert service._queue.qsize() == 104
    for _ in range(20):
        service._drain_file_events({})
    assert failing.call_count == 1
    clock[0] += 2.1
    service._drain_file_events({})
    clock[0] += 4.1
    service._drain_file_events({})
    assert failing.call_count == 3
    assert service._activity_retry is None
    assert service._queue.qsize() == 104
    assert "history dropped after 3 failed storage attempts: 24 events" in caplog.text


def test_download_callback_failure_keeps_accumulated_history(tmp_path):
    import sqlite3
    service = make_service(tmp_path)
    service.config = SimpleNamespace(get=lambda key, default=None: {"activity.enabled": True, "security.smart_downloads_enabled": True}.get(key, default))
    service._download_roots = service._activity_roots
    for i in range(3):
        service._queue.put(("created", str(service._activity_roots[0] / f"file{i}.txt"), ""))
    service.downloads.on_file.side_effect = [None, sqlite3.OperationalError("download store locked")]
    try:
        service._drain_file_events({})
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("expected failed download storage")
    assert len(service.store.query(kind="activity.file.")) == 2
    assert service._queue.qsize() == 1
    assert service.downloads.on_file.call_count == 2
