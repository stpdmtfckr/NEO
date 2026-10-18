"""Behavioral durability, idempotency and bounded-index regression tests."""
from __future__ import annotations
import os
import socket
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.runtime.desktop_services import DownloadTracker, DesktopServices, event_text
from jarvis.runtime.event_store import EventStore
from jarvis.runtime.file_index import FileIndex, close_indexes, configure_index_policy
from jarvis.runtime.file_search import search_files
from jarvis.runtime.resource_budget import resource_budget
from jarvis.runtime.cancellation import cancellation_manager


class DesktopEventsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.files = self.root / "files"
        self.files.mkdir()
        self.store = EventStore(self.root)
        self.indexes = []
        configure_index_policy(None)
        resource_budget().set_game_active(False)
        cancellation_manager().reset()

    def tearDown(self):
        for index in self.indexes:
            index.close()
        close_indexes()
        configure_index_policy(None)
        resource_budget().set_game_active(False)
        self.temp.cleanup()

    def config(self, values=None):
        data = {"file_index.enabled": False, **(values or {})}
        return types.SimpleNamespace(paths=types.SimpleNamespace(root=self.root),get=lambda key,default=None:data.get(key,default))

    def make_index(self):
        index = FileIndex(self.root / "metadata.sqlite3", [self.files])
        self.indexes.append(index)
        return index

    def test_download_start_once_through_repeat_events_completion_and_restart(self):
        path = self.files / "example.zip"
        path.write_bytes(b"abc")
        tracker = DownloadTracker(self.store)
        for tick in range(30):
            tracker.on_file(str(path), now=tick * .01)
        tracker.reconcile(now=10)
        tracker = DownloadTracker(self.store)
        path.write_bytes(b"user edited the old file")
        tracker.on_file(str(path), event_type="modified", now=11)
        tracker.on_file(str(path), event_type="created", now=12)
        tracker.reconcile(now=30)
        keys = [row["message_key"] for row in self.store.pending()]
        self.assertEqual(keys, ["download.started", "download.completed"])

    def test_temporary_rename_preserves_one_session(self):
        temporary, final = self.files / "archive.zip.crdownload", self.files / "archive.zip"
        temporary.write_bytes(b"payload")
        tracker = DownloadTracker(self.store)
        tracker.on_file(str(temporary), now=0)
        old_id = self.store.downloads()[0]["id"]
        temporary.rename(final)
        tracker.on_file(str(temporary), destination=str(final), event_type="moved", now=1)
        tracker.reconcile(now=8)
        self.assertEqual(self.store.downloads()[0]["id"], old_id)
        self.assertEqual(len(self.store.pending()), 2)

    def test_restart_recovers_actual_temporary_name(self):
        temporary = self.files / "archive.part"
        temporary.write_bytes(b"partial")
        tracker = DownloadTracker(self.store)
        tracker.on_file(str(temporary))
        restored = DownloadTracker(self.store)
        self.assertEqual(os.path.normcase(str(Path(next(iter(restored.active.values()))["actual"]).resolve())), os.path.normcase(str(temporary.resolve())))
        restored.reconcile()
        self.assertEqual(self.store.downloads()[0]["state"], "downloading")

    def test_missing_file_is_unknown_not_fabricated_failure_or_cancel(self):
        temporary = self.files / "gone.part"
        temporary.write_bytes(b"partial")
        tracker = DownloadTracker(self.store)
        tracker.on_file(str(temporary), now=0)
        temporary.unlink()
        tracker.on_file(str(temporary), event_type="deleted", now=1)
        tracker.reconcile(now=3)
        self.assertEqual(self.store.downloads()[0]["state"], "downloading")
        tracker.reconcile(now=8)
        self.assertEqual(self.store.downloads()[0]["state"], "unknown")
        self.assertFalse(tracker.active)
        event = self.store.pending()[-1]
        self.assertIn("неизвестно", event_text(event, "ru"))

    def test_provider_cancel_and_failure_are_durable_once(self):
        tracker = DownloadTracker(self.store)
        for index, terminal in enumerate(("cancelled", "failed")):
            path = str(self.files / f"{index}.zip")
            tracker.provider_state(str(index), path, "downloading", source="test-browser")
            tracker.provider_state(str(index), path, terminal, source="test-browser")
            tracker.provider_state(str(index), path, terminal, source="test-browser")
            tracker.provider_state(str(index), path, "downloading", source="test-browser")
        self.assertFalse(tracker.active)
        self.assertEqual([row["message_key"] for row in self.store.pending()].count("download.cancelled"), 1)
        self.assertEqual([row["message_key"] for row in self.store.pending()].count("download.failed"), 1)

    def test_provider_paused_file_is_not_marked_complete_by_stability(self):
        path = self.files / "still.zip"
        path.write_bytes(b"part")
        tracker = DownloadTracker(self.store)
        tracker.provider_state("a", str(path), "paused", source="browser")
        tracker.reconcile(now=time.monotonic()+100)
        self.assertEqual(self.store.downloads()[0]["state"], "paused")

    def test_disabled_events_do_not_starve_enabled_notifications(self):
        for index in range(55):
            self.store.record("download.started", {"name":str(index)}, notify=True)
        self.store.record("network.restored", {}, notify=True)
        rows = self.store.pending(prefixes=("network.restored",))
        self.assertEqual(len(rows), 1)
        self.store.acknowledge(rows[0]["event_id"])
        self.assertEqual(self.store.pending(prefixes=("network.restored",)), [])
        self.assertEqual(self.store.pending(prefixes=()), [])

    def test_installation_requires_observed_process_not_just_exe_file(self):
        path = self.files / "installer.exe"
        path.write_bytes(b"not a real program")
        tracker = DownloadTracker(self.store)
        tracker.on_file(str(path), now=0)
        tracker.reconcile(now=8)
        service = DesktopServices(self.config())
        with patch("psutil.process_iter", return_value=[]), patch("psutil.pid_exists", return_value=False):
            service._observe_installers()
        self.assertFalse(self.store.query(kind="installation."))
        process = types.SimpleNamespace(info={"pid":123,"exe":str(path),"create_time":42.0})
        with patch("psutil.process_iter", return_value=[process]), patch("psutil.pid_exists", return_value=True):
            service._observe_installers()
            service._observe_installers()
        with patch("psutil.process_iter", return_value=[]), patch("psutil.pid_exists", return_value=False):
            service._observe_installers()
            service._observe_installers()
        events = self.store.query(kind="installation.")
        self.assertEqual(len(events), 2)
        ended = next(row for row in events if row["kind"] == "installation.ended")
        self.assertIsNone(ended["data"]["success"])

    def test_passive_network_change_survives_notification_optout_without_tcp(self):
        events = []
        service = DesktopServices(self.config(), events.append)
        stats = {"Ethernet":types.SimpleNamespace(isup=True)}
        first = {"Ethernet":[types.SimpleNamespace(family=socket.AF_INET,address="10.0.0.1")]}
        second = {"Ethernet":[types.SimpleNamespace(family=socket.AF_INET,address="10.0.0.2")]}
        with patch("psutil.net_if_stats", return_value=stats), patch("psutil.net_if_addrs", side_effect=[first,second,second]), patch("socket.create_connection") as tcp:
            service._network_probe()
            service._network_probe()
            self.assertEqual(events, [])  # transient adapter churn is not a reconnect
            service._network_probe()
        self.assertEqual([row["type"] for row in events], ["network_changed"])
        tcp.assert_not_called()

    def test_file_index_applies_create_rename_delete_without_full_rescan(self):
        index = self.make_index()
        index.refresh()
        source, target = self.files / "new.txt", self.files / "renamed.txt"
        source.write_text("test")
        index._apply_events([("created",str(source),"",False)])
        self.assertEqual(len(index.search(["new.txt"])), 1)
        source.rename(target)
        index._apply_events([("moved",str(source),str(target),False)])
        self.assertEqual(index.search(["new.txt"]), [])
        self.assertEqual(len(index.search(["renamed.txt"])), 1)
        target.unlink()
        index._apply_events([("deleted",str(target),"",False)])
        self.assertEqual(index.search(["renamed.txt"]), [])

    def test_index_obeys_game_admission_and_preserves_old_rows_on_partial_scan(self):
        path = self.files / "existing.txt"
        path.write_text("one")
        index = self.make_index()
        index.refresh()
        resource_budget().set_game_active(True,"game")
        index.refresh()
        self.assertTrue(index.incomplete)
        self.assertEqual(len(index.search(["existing.txt"])), 1)

    def test_index_disabled_preserves_live_search_without_creating_database(self):
        path = self.files / "report.txt"
        path.write_text("one")
        configure_index_policy(self.config())
        with patch("jarvis.runtime.file_index.get_index", side_effect=AssertionError("index disabled")):
            result = search_files("report.txt", roots=[self.files])
        self.assertEqual([row["name"] for row in result], ["report.txt"])
        self.assertFalse(result.incomplete)

    def test_immediate_new_file_is_found_before_watch_event(self):
        (self.files / "first.txt").write_text("one")
        index = self.make_index()
        index.refresh()
        (self.files / "second.txt").write_text("two")
        with patch("jarvis.runtime.file_index.get_index", return_value=index):
            result = search_files("second.txt", roots=[self.files])
        self.assertEqual([row["name"] for row in result], ["second.txt"])

    def test_zero_budget_search_reports_incomplete_not_absence(self):
        configure_index_policy(self.config())
        result = search_files("missing", roots=[self.files], max_seconds=0)
        self.assertEqual(result, [])
        self.assertTrue(result.incomplete)


if __name__ == "__main__":
    unittest.main()
