"""145 regressions: internal noise, legitimate downloads and scoped evidence."""
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis.runtime.observation_policy import ObservationPolicy, normalized_path
from jarvis.runtime.event_store import EventStore, read_observed_history
from jarvis.runtime.desktop_services import DownloadTracker, DesktopServices
from jarvis.runtime.system_timeline import SystemContextTracker


def config(root, **values):
    data={'activity.enabled':True, **values}
    return SimpleNamespace(paths=SimpleNamespace(root=root),get=lambda key,default=None:data.get(key,default))


def make(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(b'payload')
    return str(path)


@pytest.mark.parametrize('relative',['runtime/.venv/Lib/local_player_worker.cpython-312.pyc','cache/yandex/song.mp3',
                                  'models/whisper/model.bin','logs/neo.log','data/history.sqlite3-wal'])
def test_owned_paths_never_start_download_or_activity(tmp_path,relative):
    store=EventStore(tmp_path,config(tmp_path))
    path=make(tmp_path/relative)
    tracker=DownloadTracker(store)
    tracker.on_file(path)
    tracker.provider_state('browser-id',path,'downloading',source='chrome.exe')
    tracker.reconcile(now=time.monotonic()+20)
    assert not tracker.active and not store.downloads() and not store.pending()
    assert not store.record('activity.file.created',{'path':path})


@pytest.mark.parametrize('name',['local_player_worker.cpython-312.pyc','hello.py','model.bin','song.mp3','setup.exe'])
def test_same_extension_legitimate_user_download_survives(tmp_path,name):
    store=EventStore(tmp_path,config(tmp_path))
    path=make(tmp_path/'Downloads'/name)
    tracker=DownloadTracker(store)
    tracker.on_file(path)
    tracker.provider_state('id',path,'downloading',source='chrome.exe',started=time.time(),app='chrome.exe')
    tracker.provider_state('id',path,'completed',source='chrome.exe',started=time.time(),app='chrome.exe')
    assert [r['message_key'] for r in store.pending()]==['download.started','download.completed']
    assert store.record('activity.file.created',{'path':path,'origin':'neo.tool','pid':os.getpid()})


def test_normalized_windows_managed_root_not_prefix_sibling(tmp_path):
    cfg=config(tmp_path,**{'paths.runtime_dir':r'C:\Users\USER\Downloads\NEO Runtime'})
    policy=ObservationPolicy(cfg)
    assert not policy.allows_path(r'c:/users/user/downloads/NEO Runtime/cache/../file.py')
    assert policy.allows_path(r'C:\Users\user\Downloads\NEO Runtime Extra\file.py')
    assert normalized_path(r'C:\a\..\b')=='c:/b'


def test_environment_model_cache_and_browser_profile_not_all_browser_downloads(tmp_path,monkeypatch):
    monkeypatch.setenv('OLLAMA_MODELS',str(tmp_path/'my-models'))
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'Local'))
    policy=ObservationPolicy(config(tmp_path))
    assert not policy.allows_path(tmp_path/'my-models'/'a.bin')
    assert not policy.allows_path(tmp_path/'Local/Google/Chrome/User Data/Default/History-wal')
    assert policy.allows_event('download.started',{'path':str(tmp_path/'Downloads/a.py'),'app':'chrome.exe'})
    assert not policy.allows_event('download.started',{'path':str(tmp_path/'Downloads/a.py'),'origin':'neo.bootstrap'})


def test_old_queued_noise_persistently_suppressed_without_starving_user_event(tmp_path):
    cfg=config(tmp_path)
    store=EventStore(tmp_path,cfg)
    with store.connect() as db:
        for i in range(220):
            db.execute('INSERT INTO events VALUES(?,?,?,?,0)',(str(i),'download.started',i,json.dumps({'path':str(tmp_path/'cache'/f'{i}.pyc')})))
        db.execute('INSERT INTO events VALUES(?,?,?,?,0)',('user','download.started',300,json.dumps({'path':str(tmp_path/'Downloads/a.pyc')})))
    assert [x['event_id'] for x in store.pending(limit=1)]==['user']
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM events WHERE delivered=2').fetchone()[0]==220
    assert [x['id'] for x in EventStore(tmp_path,cfg).query()]==['user']
    store.acknowledge('0')
    with store.connect() as db:
        assert db.execute('SELECT delivered FROM events WHERE id="0"').fetchone()[0]==2


def test_foreground_own_process_filtered_external_notepad_retained(tmp_path):
    cfg=config(tmp_path)
    tracker=SystemContextTracker(cfg,memory=SimpleNamespace())
    assert not tracker.observation_allowed('pythonw.exe','NEO',pid=os.getpid())
    assert not tracker.observation_allowed('neo.exe','Settings',pid=100)
    assert tracker.observation_allowed('notepad.exe','User script.py',str(tmp_path/'Documents'),pid=100)
    services=DesktopServices(cfg)
    services.observe_activity({'process_name':'pythonw.exe','title':'NEO','pid':os.getpid()})
    services.observe_activity({'process_name':'notepad.exe','title':'User script.py','pid':100,'origin':'neo.tool'})
    assert [r['data']['process_name'] for r in services.store.query()]==['notepad.exe']


def test_scoped_calendar_history_dedup_and_private_filter(tmp_path):
    cfg=config(tmp_path)
    store=EventStore(tmp_path,cfg)
    midnight=datetime.now().replace(hour=0,minute=0,second=0,microsecond=0)
    yesterday=(midnight-timedelta(hours=1)).timestamp()
    with store.connect() as db:
        payload=json.dumps({'process_name':'notepad.exe','title':'Report','folder_path':str(tmp_path/'Documents')})
        for event_id,stamp in [('a',yesterday),('duplicate',yesterday+.5),('today',midnight.timestamp()+1)]:
            db.execute('INSERT INTO events VALUES(?,?,?,?,1)',(event_id,'activity.window',stamp,payload))
        db.execute('INSERT INTO events VALUES(?,?,?,?,1)',('private','activity.window',yesterday,json.dumps({'process_name':'chrome.exe','title':'InPrivate'})))
    result=read_observed_history(cfg,scope='desktop',period='yesterday')
    assert result['available'] and result['recording_enabled'] and result['incomplete']
    assert len(result['rows'])==1 and result['rows'][0]['id']=='duplicate'
    assert result['since']==(midnight-timedelta(days=1)).timestamp()
    assert result['until']==midnight.timestamp()
    assert not read_observed_history(cfg,scope='telegram:123',period='yesterday')['rows']
    assert not read_observed_history(config(tmp_path/'missing'),scope='desktop')['available']
    assert not (tmp_path/'missing').exists()
    # Legacy settings without the key use the observer's enabled default too.
    legacy=SimpleNamespace(paths=SimpleNamespace(root=tmp_path),get=lambda key,default=None:default)
    assert read_observed_history(legacy,scope='desktop')['recording_enabled']
    assert not read_observed_history(config(tmp_path,**{'activity.enabled':False}),scope='desktop')['recording_enabled']
