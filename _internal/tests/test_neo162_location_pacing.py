"""Bound search slices even when every directory entry is excluded."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.runtime import location_scan158 as locations
from jarvis.runtime.cancellation import cancellation_manager, OperationCancelled


class Entries:
    def __init__(self, root, count=1000):
        self.root, self.limit, self.visited = root, count, 0
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def __iter__(self):
        return self
    def __next__(self):
        if self.visited == self.limit:
            raise StopIteration
        self.visited += 1
        return SimpleNamespace(path=str(self.root / str(self.visited)))


def test_excluded_entries_respect_slice_and_resume_without_restarting(tmp_path, monkeypatch):
    cancellation_manager().reset()
    entries = Entries(tmp_path)
    monkeypatch.setattr(locations.os, 'scandir', lambda _: entries)
    monkeypatch.setattr(locations, 'is_link', lambda path, entry=None: entry is not None)
    state = locations.Scan((str(tmp_path),), frozenset({'darktide'}))
    try:
        state.advance(seconds=1, entries=5)
        assert entries.visited == 5 and not state.complete
        state.advance(seconds=1, entries=5)
        assert entries.visited == 10 and not state.complete
        assert state.checked == 0  # Excluded links never become result candidates.
    finally:
        state.iterator.close()
        cancellation_manager().reset()


def test_cooperative_pacer_runs_for_skipped_entries_and_honours_cancel(tmp_path, monkeypatch):
    from jarvis.runtime import file_scan162
    manager = cancellation_manager()
    manager.reset()
    entries = Entries(tmp_path)
    checkpoints = []
    class Pacer:
        def __init__(self, **kwargs):
            assert kwargs.get('cancelled') is None
        def checkpoint(self):
            checkpoints.append(True)
            if len(checkpoints) == 5:
                manager.cancel('test cancellation')
            return True
    monkeypatch.setattr(file_scan162, 'ScanPacer', Pacer)
    monkeypatch.setattr(locations.os, 'scandir', lambda _: entries)
    monkeypatch.setattr(locations, 'is_link', lambda path, entry=None: entry is not None)
    state = locations.Scan((str(tmp_path),), frozenset({'darktide'}))
    try:
        with pytest.raises(OperationCancelled):
            state.advance(seconds=1, entries=1000)
        assert len(checkpoints) == 5 and entries.visited <= 4
        previous = entries.visited
        manager.reset()
        state.advance(seconds=1, entries=5)
        assert entries.visited == previous + 5
        assert not state.complete  # Cancel did not close the saved generator.
    finally:
        state.iterator.close()
        manager.reset()
