"""Fixture-only SQLite tests; never open a real user browser profile or URL."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jarvis.runtime.browser_downloads import BrowserDownloadObserver, BrowserProfile, read_downloads, sanitize_source_url, WINDOWS_EPOCH_OFFSET
from jarvis.runtime.desktop_services import DownloadTracker
from jarvis.runtime.event_store import EventStore


class Fixture:
    def __init__(self, root):
        self.root = root
        self.files = root/'Downloads'
        self.files.mkdir()
        self.history = root/'History'
        with sqlite3.connect(self.history) as db:
            db.executescript('''CREATE TABLE downloads(id INTEGER PRIMARY KEY,guid TEXT,target_path TEXT,current_path TEXT,start_time INTEGER,received_bytes INTEGER,total_bytes INTEGER,state INTEGER,interrupt_reason INTEGER,transient INTEGER);
                CREATE TABLE downloads_url_chains(id INTEGER,chain_index INTEGER,url TEXT,PRIMARY KEY(id,chain_index));
                CREATE TABLE urls(url TEXT,secret TEXT);
                INSERT INTO urls VALUES('https://private.invalid/never-read','private browsing text');''')
        self.profile = BrowserProfile('Chrome','chrome.exe',self.history)
        self.observer = BrowserDownloadObserver([self.profile])
        self.store = EventStore(root)
        self.tracker = DownloadTracker(self.store)
        self.values = {'security.smart_downloads_enabled':True,'downloads.directories':[str(self.files)]}
        self.config = SimpleNamespace(get=lambda key, default=None:self.values.get(key,default))

    def add(self, ident=1, *, state=0, name='file.zip', started=None, size=1, current=None):
        target = self.files/name
        timestamp = started if started is not None else time.time()
        raw = round((timestamp+WINDOWS_EPOCH_OFFSET)*1_000_000)
        with sqlite3.connect(self.history) as db:
            db.execute('INSERT INTO downloads VALUES(?,?,?,?,?,?,?,?,?,?)',(ident,f'guid-{ident}',str(target),str(current or target),raw,size,20,state,0,0))
            db.execute('INSERT INTO downloads_url_chains VALUES(?,?,?)',(ident,0,'https://user:password@example.org/file.zip?token=secret#private'))
        return target

    def update(self, ident=1, *, state=1, size=20):
        with sqlite3.connect(self.history) as db:
            db.execute('UPDATE downloads SET state=?,received_bytes=? WHERE id=?',(state,size,ident))

    def poll(self):
        self.observer._next_poll = 0
        return self.observer.poll(self.config,self.tracker)


@pytest.fixture
def fixture(tmp_path):
    return Fixture(tmp_path)


@pytest.mark.parametrize('native,expected',[(0,'downloading'),(1,'completed'),(2,'cancelled'),(3,'unknown'),(4,'failed')])
def test_verified_history_enum_not_runtime_downloaditem_enum(fixture,native,expected):
    fixture.add(state=native)
    rows=read_downloads(fixture.profile)
    assert rows[0]['state']==expected
    assert rows[0]['app']=='chrome.exe'
    assert rows[0]['source_url']=='https://example.org/file.zip'


def test_read_only_download_queries_preserve_entire_database(fixture):
    fixture.add()
    before=hashlib.sha256(fixture.history.read_bytes()).digest()
    statements=[]
    connect=sqlite3.connect
    def traced(*args,**kwargs):
        assert args[0].endswith('?mode=ro') and kwargs['uri'] is True
        db=connect(*args,**kwargs)
        db.set_trace_callback(statements.append)
        return db
    with patch('jarvis.runtime.browser_downloads.sqlite3.connect',side_effect=traced):
        assert len(read_downloads(fixture.profile))==1
    assert hashlib.sha256(fixture.history.read_bytes()).digest()==before
    assert not any('FROM urls' in q or 'SELECT *' in q or 'ATTACH' in q.upper() for q in statements)
    assert 'PRAGMA query_only=ON' in statements


def test_schema_variation_without_guid_or_url_chain(tmp_path):
    path=tmp_path/'History'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE downloads(id INTEGER PRIMARY KEY,state INTEGER,start_time INTEGER,received_bytes INTEGER,full_path TEXT,url TEXT)')
        db.execute('INSERT INTO downloads VALUES(1,1,?,?,?,?)',(int(time.time()),6,str(tmp_path/'a.zip'),'https://example.org/a.zip?password=secret'))
    rows=read_downloads(BrowserProfile('Brave','brave.exe',path))
    assert len(rows)==1 and rows[0]['state']=='completed'
    assert rows[0]['source_url']=='https://example.org/a.zip'


def test_missing_schema_is_unavailable_not_empty_valid_history(tmp_path):
    path=tmp_path/'History'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE downloads(id INTEGER PRIMARY KEY)')
    assert read_downloads(BrowserProfile('Edge','msedge.exe',path)) is None


def test_unrelated_browsing_table_cannot_be_accessed_even_through_a_view(tmp_path):
    path=tmp_path/'History'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE urls(id INTEGER,state INTEGER,start_time INTEGER,received_bytes INTEGER,target_path TEXT)')
        db.execute('CREATE VIEW downloads AS SELECT * FROM urls')
    assert read_downloads(BrowserProfile('Chrome','chrome.exe',path)) is None


def test_expired_budget_does_not_open_database(fixture):
    with patch('jarvis.runtime.browser_downloads.sqlite3.connect') as connect:
        assert read_downloads(fixture.profile,deadline=time.monotonic()-1) is None
    connect.assert_not_called()


def test_locked_database_fails_fast_without_copy_or_retry(fixture):
    fixture.add()
    lock=sqlite3.connect(fixture.history)
    lock.execute('BEGIN EXCLUSIVE')
    started=time.monotonic()
    try:
        assert read_downloads(fixture.profile) is None
        assert time.monotonic()-started < .75
    finally:
        lock.rollback();lock.close()
    assert sorted(p.name for p in fixture.root.iterdir())==['Downloads','History','data']


def test_disabled_observer_reads_neither_profiles_nor_databases(fixture):
    fixture.values['security.smart_downloads_enabled']=False
    with patch('jarvis.runtime.browser_downloads.read_downloads') as read,patch('jarvis.runtime.browser_downloads.discover_profiles') as discover:
        assert not fixture.poll()['enabled']
    read.assert_not_called();discover.assert_not_called()


def test_initial_history_and_deleted_recent_rows_never_create_notification_storm(fixture):
    for i in range(1,90):fixture.add(i,state=1,name=f'{i}.zip',started=time.time()-86400)
    fixture.poll()
    assert fixture.store.pending()==[] and fixture.store.downloads()==[]
    with sqlite3.connect(fixture.history) as db:db.execute('DELETE FROM downloads WHERE id>=40')
    fixture.poll()
    assert fixture.store.pending()==[] and fixture.store.downloads()==[]


def test_new_download_has_one_start_one_terminal_and_sanitized_metadata(fixture):
    fixture.poll()
    fixture.add()
    fixture.poll();fixture.poll()
    fixture.update(state=2)
    fixture.poll();fixture.poll()
    assert [r['message_key'] for r in fixture.store.pending()]==['download.started','download.cancelled']
    row=fixture.store.downloads()[0]
    assert row['source_url']=='https://example.org/file.zip' and row['app']=='chrome.exe'
    assert row['heuristic']==0 and not fixture.tracker.active


def test_quick_complete_download_between_polls_gets_exactly_one_lifecycle(fixture):
    fixture.poll()
    fixture.add(state=1)
    fixture.poll()
    assert [r['message_key'] for r in fixture.store.pending()]==['download.started','download.completed']


def test_watcher_then_browser_adopts_same_id_across_restart(fixture):
    path=fixture.files/'file.zip'
    path.write_bytes(b'part')
    fixture.tracker.on_file(str(path))
    original=fixture.store.downloads()[0]['id']
    fixture.add(started=time.time()-1)
    fixture.poll()
    assert len(fixture.store.downloads())==1
    assert fixture.store.downloads()[0]['id']==original
    fixture.tracker=DownloadTracker(fixture.store)
    fixture.observer=BrowserDownloadObserver([fixture.profile])
    fixture.update(state=4)
    fixture.poll()
    assert [r['message_key'] for r in fixture.store.pending()]==['download.started','download.failed']
    assert fixture.store.downloads()[0]['id']==original


def test_browser_adopts_unconfirmed_temporary_path_and_later_rename(fixture):
    temporary=fixture.files/'Unconfirmed 123.crdownload'
    temporary.write_bytes(b'part')
    fixture.tracker.on_file(str(temporary))
    did=fixture.store.downloads()[0]['id']
    final=fixture.add(current=temporary,started=time.time()-1)
    fixture.poll()
    assert len(fixture.store.downloads())==1
    assert fixture.store.downloads()[0]['id']==did
    temporary.rename(final)
    fixture.tracker.on_file(str(temporary),destination=str(final),event_type='moved')
    assert len(fixture.tracker.active)==1
    assert next(iter(fixture.tracker.active.values()))['provider']
    fixture.update(state=1)
    fixture.poll()
    assert len(fixture.store.pending())==2


def test_reused_same_path_is_a_new_download_not_old_alias(fixture):
    fixture.poll()
    fixture.add(1,started=time.time()-10)
    fixture.poll();fixture.update(1,state=1);fixture.poll()
    first=fixture.store.downloads()[0]['id']
    fixture.add(2,started=time.time()+1)
    fixture.poll();fixture.update(2,state=4);fixture.poll()
    rows=fixture.store.downloads()
    assert len(rows)==2 and len({r['id'] for r in rows})==2
    assert any(r['id']==first and r['state']=='completed' for r in rows)
    assert len(fixture.store.pending())==4


def test_old_heuristic_completion_cannot_absorb_a_later_download_with_same_name(fixture):
    path=fixture.files/'file.zip';path.write_bytes(b'original')
    fixture.tracker.on_file(str(path),now=0);fixture.tracker.reconcile(now=8)
    old_id=fixture.store.downloads()[0]['id']
    fixture.poll()
    fixture.add(started=time.time()+1)
    fixture.poll()
    assert len(fixture.store.downloads())==2
    assert any(row['id']==old_id and row['state']=='completed' for row in fixture.store.downloads())


def test_authoritative_correction_edits_existing_terminal_card_and_revision_ack(fixture):
    path=fixture.files/'file.zip';path.write_bytes(b'part')
    fixture.tracker.on_file(str(path),now=0)
    fixture.tracker.reconcile(now=8)
    terminal=next(x for x in fixture.store.pending() if x['message_key']=='download.completed')
    fixture.store.acknowledge(terminal['event_id'],terminal['revision'])
    fixture.add(state=2,started=time.time()-1)
    fixture.poll()
    rows=fixture.store.query(kind='download.')
    assert len(rows)==2 and fixture.store.downloads()[0]['state']=='cancelled'
    corrected=next(x for x in fixture.store.pending() if x['event_id']==terminal['event_id'])
    assert corrected['message_key']=='download.cancelled' and corrected['correction']
    assert corrected['revision']>terminal['revision']
    fixture.store.acknowledge(terminal['event_id'],terminal['revision'])
    assert any(x['event_id']==terminal['event_id'] for x in fixture.store.pending())
    fixture.store.acknowledge(corrected['event_id'],corrected['revision'])
    assert not any(x['event_id']==terminal['event_id'] for x in fixture.store.pending())


def test_multiple_unacknowledged_revisions_remain_corrections(fixture):
    fixture.poll();fixture.add();fixture.poll()
    start=fixture.store.pending()[0]
    fixture.store.acknowledge(start['event_id'],start['revision'])
    fixture.update(state=0,size=5);fixture.poll()
    first=fixture.store.pending()[0]
    assert first['correction']
    fixture.update(state=0,size=10);fixture.poll()
    second=fixture.store.pending()[0]
    assert second['correction'] and second['revision']>first['revision']
    assert second['event_id']==start['event_id']


def test_download_outside_configured_roots_is_not_recorded(fixture):
    fixture.poll()
    fixture.add(name='../outside.zip')
    fixture.poll()
    assert fixture.store.downloads()==[]


@pytest.mark.parametrize('value,expected',[
    ('https://u:p@example.com/x?api_key=abc#token','https://example.com/x'),
    ('data:image/png;base64,private',''),('file:///secret/file',''),
    ('https://example.com/token=abc/file','https://example.com/[redacted]/file'),
    ('https://example.com/'+('A'*100),'https://example.com/[redacted]'),
])
def test_source_url_never_contains_query_credentials_or_nonweb_schemes(value,expected):
    assert sanitize_source_url(value)==expected
