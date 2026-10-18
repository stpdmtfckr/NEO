"""Large-index continuation and shared native hardware provider regressions."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import time

import pytest

from jarvis.runtime import file_index, file_search, hardware
from jarvis.runtime.file_requests162 import parse_file_request
from jarvis.runtime.cancellation import cancellation_manager
from jarvis.integrations import system_monitor as monitor


@pytest.fixture(autouse=True)
def reset():
    cancellation_manager().reset()
    yield
    file_index.close_indexes()


def test_large_index_resumes_instead_of_restarting_rare_match(tmp_path):
    index = file_index.FileIndex(tmp_path/'index.db', [])
    try:
        with sqlite3.connect(index.path) as db:
            db.executemany('INSERT INTO files VALUES(?,?,?,?,?,?)',
                ((str(i), 'needle.txt' if i==19999 else 'noise.txt',
                  'needle.txt' if i==19999 else 'noise.txt', 0, i, 1) for i in range(20000)))
        rows = index.search(['needle'], max_seconds=.00001)
        assert index.query_incomplete
        cursors = []
        for _ in range(300):
            rows = index.search(['needle'], max_seconds=.015)
            if not index.query_incomplete:
                break
            cursors.append(next(iter(index._queries.values()))['cursor'])
        assert not index.query_incomplete
        assert [r['name'] for r in rows] == ['needle.txt']
        assert len(cursors) > 1
        assert max(cursors) > min(cursors)
    finally:
        index.close()


def test_query_timeout_propagates_as_continuable(tmp_path, monkeypatch):
    fake = SimpleNamespace(continuable=False, last_refresh=time.monotonic(), needs_refresh=False,
        incomplete=False, query_incomplete=True, freshness_pending=False,
        validate_freshness=lambda **_: True, search=lambda *a, **kw: [])
    monkeypatch.setattr(file_index, 'get_index', lambda _: fake)
    monkeypatch.setattr(file_index, 'index_enabled', lambda: True)
    rows = file_search.search_files('needle', roots=[tmp_path])
    assert rows.incomplete and rows.continuable


def test_paged_index_sorts_limits_and_includes_directories(tmp_path):
    index = file_index.FileIndex(tmp_path/'index.db', [])
    try:
        with sqlite3.connect(index.path) as db:
            db.executemany('INSERT INTO files VALUES(?,?,?,?,?,?)',
                ((str(i), 'match.txt', 'match.txt', 0, i, 1) for i in range(800)))
            db.execute('INSERT INTO directories VALUES(?,?,?,?,?,?)', ('folder','match','match',0,1000,1))
        rows = index.search(['match'], limit=3, include_dirs=True)
        assert [r['path'] for r in rows] == ['folder', '799', '798']
        assert rows[0]['kind'] == 'folder'
        assert not index.query_incomplete
    finally:
        index.close()


@pytest.mark.parametrize('text,query,root', [
    ('найди отчёт.pdf', 'отчёт.pdf', None),
    ('найди "годовой отчёт.pdf" в загрузках', 'годовой отчёт.pdf', 'загрузках'),
    ('find report.pdf in Downloads', 'report.pdf', 'Downloads'),
    ('найди config.json в папке C:\\Project', 'config.json', 'C:\\Project'),
])
def test_filename_without_object_word(text, query, root):
    result = parse_file_request(text, 'en' if text.startswith('find') else 'ru')
    assert result and result[1]['query'] == query
    assert result[1].get('root') == root


@pytest.mark.parametrize('text', ['найди example.com', 'найди как открыть pdf', 'найди новости', 'не найди отчёт.pdf'])
def test_filename_shorthand_does_not_steal_other_queries(text):
    assert parse_file_request(text) is None


def test_hardware_uses_same_native_gpu_cache_as_system_status(monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, '_PROBES', monitor._ProbeCache())
    def read():
        calls.append(1)
        return [{'name':'GPU','utilization_percent':42,'temperature_c':50,'power_w':70,
                 'clock_mhz':1000,'memory_used_mb':200,'memory_total_mb':1000}]
    monkeypatch.setattr(monitor, '_read_nvidia_gpus', read)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: hardware._nvidia_smi(), range(16)))
    assert monitor._gpu_snapshot([])[0]['name'] == 'GPU'
    assert len(calls) == 1
    assert all(row['gpu_load_percent']==42 for row in rows)
    rows[0]['gpu_load_percent'] = -1
    assert hardware._nvidia_smi()['gpu_load_percent'] == 42


def test_absent_gpu_does_not_restart_native_process_for_each_sample(monkeypatch):
    calls=[]
    monkeypatch.setattr(monitor, '_PROBES', monitor._ProbeCache())
    monkeypatch.setattr(monitor, '_read_nvidia_gpus', lambda: calls.append(1) or [])
    for _ in range(8):
        assert hardware._nvidia_smi() == {}
    assert len(calls)==1


def test_short_filename_routes_through_feature_router():
    from jarvis.runtime.feature_commands import parse_feature_command
    assert parse_feature_command('найди отчёт.pdf', 'ru') == (
        'search', {'action':'files', 'query':'отчёт.pdf'})


def test_cancelled_index_query_keeps_unprocessed_records(tmp_path, monkeypatch):
    from jarvis.runtime.cancellation import OperationCancelled
    from jarvis.runtime import file_scan162
    index = file_index.FileIndex(tmp_path/'index.db', [])
    try:
        with sqlite3.connect(index.path) as db:
            db.executemany('INSERT INTO files VALUES(?,?,?,?,?,?)',
                ((str(i), 'match.txt', 'match.txt', 0, i, 1) for i in range(30)))
        original = file_scan162.ScanPacer.checkpoint
        calls = []
        def interrupt(self, **kwargs):
            calls.append(1)
            if len(calls) == 10:
                raise OperationCancelled('cancel test')
            return original(self, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(file_scan162.ScanPacer, 'checkpoint', interrupt)
            with pytest.raises(OperationCancelled):
                index.search(['match'])
        rows = index.search(['match'])
        assert len(rows) == 30
        assert not index.query_incomplete
    finally:
        index.close()
