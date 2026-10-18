"""Real filesystem regressions for search results, scope and paced continuation."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import os
import time

import pytest

from jarvis.runtime import file_search, file_index
from jarvis.runtime.cancellation import cancellation_manager, OperationCancelled
from jarvis.runtime.file_scan162 import MetadataWalker, ScanPacer
from jarvis.runtime.resource_budget import resource_budget


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'profile'))
    cancellation_manager().reset()
    file_index.configure_index_policy(None)
    resource_budget().set_game_active(False)
    yield
    file_index.close_indexes()
    file_index.configure_index_policy(None)
    resource_budget().set_game_active(False)
    with file_search._LIVE_LOCK:
        for state in file_search._LIVE.values():
            state['walker'].close()
        file_search._LIVE.clear()


def test_actual_indexed_results_require_entire_name_not_one_common_word(tmp_path):
    for name in ('report final.txt', 'report draft.txt', 'notes final.txt'):
        (tmp_path/name).write_text(name)
    rows = file_search.search_files('report final', roots=[tmp_path])
    assert [r['name'] for r in rows] == ['report final.txt']
    assert str(tmp_path/'report final.txt') in file_search.format_search_results(rows)


@pytest.mark.parametrize('name', ['file', 'from', 'document', 'in'])
def test_filename_that_is_a_shell_word_is_not_an_empty_query(tmp_path, name):
    (tmp_path/(name+'.txt')).write_text('wanted')
    (tmp_path/'unrelated.txt').write_text('other')
    rows = file_search.search_files(name, roots=[tmp_path])
    assert [r['name'] for r in rows] == [name+'.txt']


def test_scope_alias_and_invalid_scope_never_falls_back_to_global(tmp_path):
    with patch('jarvis.runtime.file_index.known_folders', return_value=[tmp_path]) as known:
        assert file_search.resolve_search_roots('téléchargements') == [tmp_path]
        known.assert_called_once_with('Downloads')
    with pytest.raises(ValueError):
        file_search.resolve_search_roots('misspelled folder')
    with pytest.raises(FileNotFoundError):
        file_search.resolve_search_roots(str(tmp_path/'missing'))
    with pytest.raises(ValueError):
        file_search.resolve_search_roots(r'\\server\share')


def test_explicit_root_stays_local_and_preserves_unicode(tmp_path):
    root = tmp_path/'Рабочие материалы'
    root.mkdir()
    target = root/'Отчёт 2026.pdf'
    target.write_bytes(b'not parsed for metadata')
    (tmp_path/'Отчёт 2026.pdf').write_bytes(b'outside')
    rows = file_search.search_files('Отчёт 2026.pdf', roots=file_search.resolve_search_roots(str(root)))
    assert [r['path'] for r in rows] == [str(target)]


def test_legacy_glob_folder_and_nonrecursive_contract(tmp_path):
    folder = tmp_path/'Project Today'
    folder.mkdir()
    (folder/'nested.txt').write_text('inside')
    (tmp_path/'outside.txt').write_text('outside')
    rows = file_search.search_files('project*', roots=[tmp_path], include_dirs=True, literal=True, glob=True)
    assert [(r['name'],r['kind']) for r in rows] == [('Project Today','folder')]
    rows = file_search.search_files('*.txt', roots=[tmp_path], include_dirs=True, literal=True, glob=True, recursive=False)
    assert [r['name'] for r in rows] == ['outside.txt']


def test_index_pauses_and_resumes_same_generation_without_restarting(tmp_path):
    folder = tmp_path/'tree'; folder.mkdir()
    for n in range(500):
        (folder/f'file-{n:04d}.txt').write_text('data')
    index = file_index.FileIndex(tmp_path/'index.sqlite3', [folder])
    try:
        index.refresh(max_seconds=.003, correctness_required=True)
        assert index.continuable
        walker = index._scan
        checked = walker.checked
        generation = index._scan_generation
        resource_budget().set_game_active(True, 'game')
        index.refresh(max_seconds=.01)
        assert index._scan is walker and walker.checked == checked
        resource_budget().set_game_active(False)
        for _ in range(100):
            index.refresh(max_seconds=.005, correctness_required=True)
            if not index.continuable:
                break
            assert index._scan is walker
            assert index._scan_generation == generation
        assert not index.continuable
        assert len(index.search(['file-'], limit=600)) == 500
    finally:
        index.close()


def test_search_does_not_spawn_second_background_scan(tmp_path):
    (tmp_path/'target.txt').write_text('data')
    with patch.object(file_index.FileIndex, 'request_refresh', side_effect=AssertionError('duplicate background')):
        with patch.object(file_search, '_live_metadata', side_effect=AssertionError('duplicate live scan')):
            assert file_search.search_files('target.txt', roots=[tmp_path])[0]['name'] == 'target.txt'


def test_new_and_moved_files_found_without_native_watcher(tmp_path):
    folder=tmp_path/'tree'; folder.mkdir()
    path=folder/'report.txt'; path.write_text('one')
    assert file_search.search_files('report.txt', roots=[folder])[0]['path']==str(path)
    child=folder/'moved'; child.mkdir()
    path.rename(child/path.name)
    rows=file_search.search_files('report.txt', roots=[folder])
    assert rows[0]['path']==str(child/path.name)


def test_live_search_resumes_to_late_files_when_index_disabled(tmp_path):
    for n in range(300):
        (tmp_path/f'report-{n:03d}.txt').write_text('data')
    config=SimpleNamespace(get=lambda key,default=None: False if key=='file_index.enabled' else default)
    file_index.configure_index_policy(config)
    rows=file_search.search_files('report', roots=[tmp_path], limit=500, max_seconds=.002)
    assert rows.incomplete
    for _ in range(100):
        rows=file_search.search_files('report', roots=[tmp_path], limit=500, max_seconds=.005)
        if not rows.continuable:
            break
    assert len(rows)==300
    assert not rows.incomplete


def test_content_and_short_literal_terms_return_full_paths(tmp_path):
    (tmp_path/'wanted.txt').write_text('An XY value appears in the second paragraph.')
    (tmp_path/'other.txt').write_text('There is no match here.')
    rows=file_search.search_content('XY', roots=[tmp_path])
    assert [r['name'] for r in rows]==['wanted.txt']
    for language in ('ru','en','de','fr','es','it','pt'):
        assert str(tmp_path/'wanted.txt') in file_search.format_search_results(rows,language)


def test_zero_budget_is_partial_and_cancelled_scan_never_proceeds(tmp_path):
    (tmp_path/'target.txt').write_text('value')
    rows=file_search.search_files('target', roots=[tmp_path], max_seconds=0)
    assert not rows and rows.incomplete and rows.continuable
    cancellation_manager().cancel()
    with pytest.raises(OperationCancelled):
        file_search.search_files('target', roots=[tmp_path])


def test_links_do_not_escape_requested_tree(tmp_path):
    root=tmp_path/'root'; root.mkdir()
    outside=tmp_path/'outside'; outside.mkdir()
    (outside/'private.txt').write_text('data')
    try:
        (root/'linked').symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('host cannot create symlinks')
    assert not file_search.search_files('private',roots=[root],include_dirs=True)


def test_pacer_waits_after_cpu_quantum_and_checks_cancellation():
    waits=[]
    stop=SimpleNamespace(wait=lambda duration: waits.append(duration) or False, is_set=lambda: False)
    calls=[]
    pace=ScanPacer(stop=stop,cancelled=lambda:calls.append(True))
    pace.since=0.
    pace.clock=lambda:.004
    pace.checkpoint()
    assert waits and sum(waits)==pytest.approx(.012)
    assert len(calls)>=2


def test_quoted_brackets_are_literal_never_glob_patterns(tmp_path):
    for name in ('report[2026].txt','report2.txt','yesterday.txt'):
        (tmp_path/name).write_text('data')
    rows=file_search.search_files('report[2026].txt',roots=[tmp_path],literal=True)
    assert [r['name'] for r in rows]==['report[2026].txt']
    assert [r['name'] for r in file_search.search_files('yesterday',roots=[tmp_path],literal=True)]==['yesterday.txt']


def test_cancelled_index_retains_uncommitted_files_after_resume(tmp_path):
    folder=tmp_path/'tree';folder.mkdir()
    for n in range(30):
        (folder/f'match-{n:02d}.txt').write_text('data')
    index=file_index.FileIndex(tmp_path/'metadata.sqlite3',[folder])
    try:
        original=ScanPacer.checkpoint
        ticks=[0]
        def cancel_mid_batch(self,**kw):
            ticks[0]+=1
            if ticks[0]==12:
                cancellation_manager().cancel()
            return original(self,**kw)
        with patch.object(ScanPacer,'checkpoint',cancel_mid_batch):
            with pytest.raises(OperationCancelled):
                index.refresh(correctness_required=True)
        assert index._scan_batch
        cancellation_manager().reset()
        index.refresh(correctness_required=True)
        assert not index.incomplete
        assert len(index.search(['match-']))==30
    finally:
        index.close()


def test_content_cancellation_keeps_pending_document(tmp_path):
    target=tmp_path/'report.txt';target.write_text('wanted needle')
    original=file_search.read_searchable_text
    def cancelled_read(*args,**kwargs):
        result=original(*args,**kwargs)
        cancellation_manager().cancel()
        return result
    with patch.object(file_search,'read_searchable_text',cancelled_read):
        with pytest.raises(OperationCancelled):
            file_search.search_content('needle',roots=[tmp_path])
    cancellation_manager().reset()
    rows=file_search.search_content('needle',roots=[tmp_path])
    assert [r['path'] for r in rows]==[str(target)]
    assert not rows.incomplete


def test_unreadable_document_is_partial_not_false_no_matches(tmp_path):
    (tmp_path/'report.pdf').write_bytes(b'PDF placeholder')
    with patch.object(file_search,'read_searchable_text',return_value=''):
        rows=file_search.search_content('needle',roots=[tmp_path])
    assert not rows and rows.incomplete and rows.unreadable
    assert 'документов' in file_search.format_search_results(rows,'ru')


def test_next_query_finds_new_match_even_when_cached_query_already_has_hits(tmp_path):
    root=tmp_path/'root'; nested=root/'project'; nested.mkdir(parents=True)
    first=nested/'report-one.txt'; first.write_text('one')
    rows=file_search.search_files('report',roots=[root])
    assert [r['path'] for r in rows]==[str(first)]
    assert not rows.incomplete
    second=nested/'report-two.txt'; second.write_text('two')
    # Make the directory timestamp distinct even on coarse test filesystems.
    info=nested.stat()
    os.utime(nested,ns=(info.st_atime_ns,info.st_mtime_ns+1_000_000_000))
    with patch.object(file_index.FileIndex,'_start_watcher',side_effect=AssertionError('whole-tree watcher')):
        rows=file_search.search_files('report',roots=[root])
    assert {r['path'] for r in rows}=={str(first),str(second)}
    assert not rows.incomplete


def test_directory_validation_budget_is_resumable_and_explicitly_partial(tmp_path):
    folder=tmp_path/'tree'; folder.mkdir()
    for n in range(20):
        (folder/f'child{n}').mkdir()
    index=file_index.FileIndex(tmp_path/'metadata.sqlite3',[folder])
    try:
        index.refresh(correctness_required=True)
        assert index.validate_freshness(max_seconds=0) is False
        assert index.freshness_pending
        iterator=index._validation
        assert index.validate_freshness(max_seconds=1) is True
        assert not index.freshness_pending
        assert iterator is not None and index._validation is None
    finally:
        index.close()


def test_explicit_directory_event_requires_refresh(tmp_path):
    root=tmp_path/'root'; root.mkdir()
    first=root/'report-one.txt';first.write_text('one')
    file_search.search_files('report',roots=[root])
    index=file_index.get_index([root])
    nested=root/'new';nested.mkdir()
    second=nested/'report-two.txt';second.write_text('two')
    index._apply_events([('created',str(nested),'',True)])
    rows=file_search.search_files('report',roots=[root])
    assert {r['path'] for r in rows}=={str(first),str(second)}


def test_calendar_filter_checks_current_mtime_of_cached_matches(tmp_path):
    first=tmp_path/'report-one.txt'; first.write_text('old')
    second=tmp_path/'report-two.txt'; second.write_text('current')
    previous=time.time()-2*86400
    os.utime(first,(previous,previous))
    file_search.search_files('report',roots=[tmp_path])
    # Updating an existing file does not normally change parent directory mtime.
    current=time.time()
    os.utime(first,(current,current))
    rows=file_search.search_files('report today',roots=[tmp_path])
    assert {r['path'] for r in rows}=={str(first),str(second)}
