"""Training completion updates only its setting and subprocesses keep the selected endpoint."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from jarvis.config import ConfigManager
from jarvis.neo_llm.pipeline import Run
from jarvis.testing.isolation import external_io_blocked

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    for key in ('HOME','LOCALAPPDATA','APPDATA','XDG_CACHE_HOME'):
        monkeypatch.setenv(key,str(tmp_path/key))
    monkeypatch.setattr(ConfigManager,'apply_autostart',lambda *args:None)
    monkeypatch.setattr(ConfigManager,'apply_wake_service',lambda *args:None)
    with external_io_blocked():yield


def test_training_result_merges_latest_settings():
    defaults=Path(__file__).resolve().parents[1]/'config/default_settings.json'
    stale=ConfigManager(defaults)
    stale.data['user_name']='Original';stale.save(stale.data)
    current=ConfigManager(defaults)
    current.update(lambda data:data.update(user_name='New choice',custom_extension={'keep':True}))
    worker=Run.__new__(Run);worker.config=stale
    worker._save_setting('fine_tuned','synthetic-model')
    saved=json.loads(stale.paths.settings.read_text())
    assert saved['user_name']=='New choice'
    assert saved['custom_extension']=={'keep':True}
    assert saved['llm_agent']['fine_tuned']=='synthetic-model'

@pytest.mark.parametrize('local_only',[False,True])
def test_every_training_subprocess_uses_captured_endpoint(tmp_path,monkeypatch,local_only):
    from jarvis.neo_llm import pipeline
    monkeypatch.setenv('OLLAMA_HOST','http://127.0.0.1:22222')
    monkeypatch.setenv('NEO_TEST_PRESERVED','yes')
    seen=[]
    worker=Run.__new__(Run);worker.root=tmp_path;worker.local_only=local_only;worker.base_url='http://127.0.0.1:33333'
    fake=SimpleNamespace(wait=lambda **kwargs:0)
    monkeypatch.setattr(pipeline.subprocess,'Popen',lambda *args,**kwargs:seen.append(kwargs) or fake)
    assert worker._run(['synthetic-ollama','cp','candidate','trained'])==0
    env=seen[0]['env']
    assert env['OLLAMA_HOST']==worker.base_url
    assert env['NEO_TEST_PRESERVED']=='yes'
    assert os.environ['OLLAMA_HOST']=='http://127.0.0.1:22222'
    if local_only:
        assert env['HF_HUB_OFFLINE']=='1' and env['NO_PROXY']=='*'

def test_independent_writer_locks_preserve_new_ui_save(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import jarvis.config as module
    defaults=Path(__file__).resolve().parents[1]/'config/default_settings.json'
    stale=ConfigManager(defaults);stale.data['user_name']='Original';stale.save(stale.data)
    current=ConfigManager(defaults)
    entered=threading.Event();release=threading.Event();ui_done=threading.Event()
    worker=Run.__new__(Run);worker.config=stale
    original=stale._save_locked182
    def pause(data):
        entered.set();assert release.wait(2);return original(data)
    monkeypatch.setattr(stale,'_save_locked182',pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(worker._save_setting,'fine_tuned','synthetic-model')
        assert entered.wait(2)
        # A separate module/process has its own thread lock, but shares the OS lock.
        monkeypatch.setattr(module,'_SAVE_LOCK182',threading.RLock())
        def save_ui():
            data=dict(current.data);data['user_name']='New choice'
            current.save(data);ui_done.set()
        second=pool.submit(save_ui)
        try:
            assert not ui_done.wait(.06)
        finally:
            release.set()
        first.result(timeout=3);second.result(timeout=3)
    assert json.loads(stale.paths.settings.read_text())['user_name']=='New choice'


def test_profile_lock_timeout_and_release(tmp_path):
    from jarvis.settings_write_lock import profile_write_lock
    path=tmp_path/'settings.json'
    with profile_write_lock(path):
        with pytest.raises(TimeoutError):
            with profile_write_lock(path,timeout=.03):
                pytest.fail('Contending writer entered transaction')
    with pytest.raises(ValueError):
        with profile_write_lock(path):raise ValueError('synthetic save error')
    with profile_write_lock(path,timeout=0):pass


def test_windows_byte_lock_uses_matching_range(tmp_path,monkeypatch):
    import sys
    from jarvis import settings_write_lock as module
    calls=[]
    fake=SimpleNamespace(LK_NBLCK=1,LK_UNLCK=2,locking=lambda fd,mode,count:calls.append((mode,count)))
    monkeypatch.setitem(sys.modules,'msvcrt',fake)
    monkeypatch.setattr(module,'os',SimpleNamespace(name='nt',SEEK_END=os.SEEK_END))
    with module.profile_write_lock(tmp_path/'settings.json'):pass
    assert calls==[(1,1),(2,1)]

def test_rollback_waits_for_active_writer(monkeypatch,tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import jarvis.config as module
    defaults=Path(__file__).resolve().parents[1]/'config/default_settings.json'
    stale=ConfigManager(defaults);stale.data['user_name']='Original';stale.save(stale.data)
    current=ConfigManager(defaults);backup=tmp_path/'restore.json';backup.write_text('{"user_name":"Restored choice"}')
    entered=threading.Event();release=threading.Event();rolled=threading.Event()
    original=stale._save_locked182
    def pause(data):
        entered.set();assert release.wait(2);return original(data)
    monkeypatch.setattr(stale,'_save_locked182',pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(stale.update,lambda data:data.update(training_marker=True))
        assert entered.wait(2)
        monkeypatch.setattr(module,'_SAVE_LOCK182',threading.RLock())
        def restore():current.rollback_settings(backup);rolled.set()
        second=pool.submit(restore)
        try:assert not rolled.wait(.06)
        finally:release.set()
        first.result(timeout=3);second.result(timeout=3)
    assert json.loads(stale.paths.settings.read_text())['user_name']=='Restored choice'

@pytest.mark.parametrize('operation',['save','update'])
def test_native_helpers_run_after_profile_lock_release(monkeypatch,operation):
    from jarvis.settings_write_lock import profile_write_lock
    defaults=Path(__file__).resolve().parents[1]/'config/default_settings.json'
    config=ConfigManager(defaults);observed=[]
    def wake():
        with profile_write_lock(config.paths.settings,timeout=0):observed.append('lock released')
    monkeypatch.setattr(config,'apply_wake_service',wake)
    if operation=='save':config.save(config.data)
    else:config.update(lambda data:data.update(user_name='Updated'))
    assert observed==['lock released']
