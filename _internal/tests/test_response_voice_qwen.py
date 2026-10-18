"""Inert Qwen voice checks: no network, dependencies, real model, or audio."""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from jarvis.testing.isolation import external_io_blocked
from jarvis.response_voice.model_store import ModelStore as ProductionStore, MODEL_ID, REVISION, INTEGRITY_POLICY, VoiceModelError, Cancelled, load_manifest, validate_manifest, safe_path
from jarvis.response_voice.service import VoiceService, options


@pytest.fixture(autouse=True)
def isolated():
    with external_io_blocked(): yield


@pytest.fixture(scope='module')
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class Config:
    def __init__(self, root, **settings):
        self.paths=SimpleNamespace(root=root)
        self.data={'response_voice':dict(enabled=True,idle_unload_seconds=0,**settings),'app':{'language':'en'}}
    def get(self,key,default=None):
        data=self.data
        for part in key.split('.'):
            if not isinstance(data,dict) or part not in data: return default
            data=data[part]
        return data


def manifest(blob=b'correct model'):
    return {'schema_version':2,'integrity_policy':INTEGRITY_POLICY,'model_id':MODEL_ID,'revision':REVISION,'total_bytes':len(blob),
            'files':[{'path':'model.safetensors','size':len(blob),'storage':'lfs','integrity':{'algorithm':'sha256','digest':hashlib.sha256(blob).hexdigest()}}]}


class ModelStore(ProductionStore):
    """Synthetic identities keep lifecycle tests inert; production pins are never changed."""
    def __init__(self, profile, manifest=None, transport=None):
        super().__init__(profile, manifest, transport)
        self.fixture_anchors = None if manifest is None else {
            f['path']:(f['size'],f['storage'],f['integrity']['algorithm'],f['integrity']['digest'])
            for f in manifest['files']}
    def _validate(self):
        if self.fixture_anchors is None:
            return super()._validate()
        validate_manifest(self.manifest, expected_files=self.fixture_anchors)
        for entry in self.manifest['files']:
            safe_path(self.root, entry['path'])


def ready_store(tmp_path):
    store=ModelStore(tmp_path,manifest())
    store.root.mkdir(parents=True)
    (store.root/'model.safetensors').write_bytes(b'correct model')
    store.check()
    return store


class Response(io.BytesIO):
    def __init__(self,data,status=200,headers=None):
        super().__init__(data);self.status=status;self.headers=headers or {}


class FakeDownload:
    def __init__(self,blob): self.blob=blob;self.calls=[]
    def open(self,url,offset):
        self.calls.append((url,offset))
        return Response(self.blob[offset:],206 if offset else 200,{'Content-Range':f'bytes {offset}-{len(self.blob)-1}/{len(self.blob)}'})


def until(predicate, seconds=2):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if predicate(): return
        time.sleep(.005)
    assert predicate(), 'condition timed out'


def test_shipped_mixed_manifest_and_schema1_never_falls_back(tmp_path):
    data=load_manifest();assert data['revision']==REVISION
    assert data['schema_version']==2 and data['integrity_policy']==INTEGRITY_POLICY
    assert len(data['files'])==12 and data['total_bytes']==2498386873
    assert len([f for f in data['files'] if f['storage']=='lfs'])==2
    assert len([f for f in data['files'] if f['storage']=='git'])==10
    assert data['voices']=={'male':'Aiden','female':'Serena'}
    assert set(data['languages'])=={'ru','en','de','fr','es','it','pt'}
    network=Mock();store=ProductionStore(tmp_path,transport=network)
    assert store.verification_error() is None
    with pytest.raises(VoiceModelError,match='missing_or_damaged'):store.check()
    assert not store.ready_cached();network.open.assert_not_called()
    # Existing schema 1/null-SHA256 metadata must never acquire an implicit fallback.
    old=dict(data,schema_version=1)
    old['files']=[dict(f,sha256=None,official_git_blob_id='a'*40) for f in data['files']]
    invalid=ProductionStore(tmp_path,old,network)
    with pytest.raises(VoiceModelError,match='manifest_invalid'):invalid.download(threading.Event(),explicit=True)
    from prepare_response_voice import prepare
    install=Mock();result=prepare(tmp_path,store=invalid,install=install)
    assert result['state']=='manifest_invalid' and result['voice_ready'] is False
    assert result['failure']['stage']=='model_manifest'
    install.assert_not_called();network.open.assert_not_called()
    assert not store.root.exists()


def test_resume_uses_pinned_url_verifies_whole_file(tmp_path):
    blob=b'correct model';network=FakeDownload(blob);store=ModelStore(tmp_path,manifest(blob),network)
    store.root.mkdir(parents=True);(store.root/'model.safetensors.part').write_bytes(blob[:4])
    events=[];result=store.download(threading.Event(),lambda **kw:events.append(kw),explicit=True)
    assert result['state']=='verified' and store.ready_cached()
    assert network.calls==[(f'https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/model.safetensors',4)]
    assert events[-1]['done']==len(blob)
    assert not (store.root/'model.safetensors.part').exists()


def test_server_ignoring_range_restarts_safely(tmp_path):
    blob=b'correct model';transport=SimpleNamespace(open=lambda *a:Response(blob))
    store=ModelStore(tmp_path,manifest(blob),transport);store.root.mkdir(parents=True)
    (store.root/'model.safetensors.part').write_bytes(blob[:4])
    store.download(threading.Event());assert (store.root/'model.safetensors').read_bytes()==blob


def test_bad_hash_never_becomes_installed(tmp_path):
    store=ModelStore(tmp_path,manifest(),FakeDownload(b'incorrect foo'))
    with pytest.raises(VoiceModelError,match='checksum_failed'): store.download(threading.Event())
    assert not store.ready_cached() and not (store.root/'model.safetensors').exists()
    assert not (store.root/'model.safetensors.part').exists()


def test_cancel_keeps_partial_and_resumes(tmp_path):
    blob=b'correct model';cancel=threading.Event();network=FakeDownload(blob)
    store=ModelStore(tmp_path,manifest(blob),network)
    def progress(**kw):
        if kw['state']=='downloading': cancel.set()
    with pytest.raises(Cancelled): store.download(cancel,progress)
    assert (store.root/'model.safetensors.part').read_bytes()==blob
    assert not store.ready_cached()
    store.download(threading.Event());assert store.ready_cached()
    assert len(network.calls)==1


def test_tombstone_survives_bootstrap_and_requires_explicit_restore(tmp_path):
    store=ready_store(tmp_path);store.delete();assert store.tombstone.exists()
    from prepare_response_voice import prepare
    install=Mock()
    assert prepare(tmp_path,store=store,install=install)['state']=='deleted'
    install.assert_not_called()
    with pytest.raises(VoiceModelError,match='deleted'): store.download(threading.Event())
    store.transport=FakeDownload(b'correct model')
    store.download(threading.Event(),explicit=True)
    assert not store.tombstone.exists() and store.ready_cached()


def test_incomplete_manifest_does_not_clear_tombstone(tmp_path):
    data=load_manifest();data['files'][0]['integrity']['digest']=None
    store=ProductionStore(tmp_path,data);tmp_path.mkdir(exist_ok=True);store.tombstone.write_text('{}')
    with pytest.raises(VoiceModelError,match='manifest_incomplete'): store.download(threading.Event(),explicit=True)
    assert store.tombstone.exists()


def test_path_escape_and_symlink_refused(tmp_path):
    data=manifest();data['files'][0]['path']='../escape'
    store=ModelStore(tmp_path,data)
    with pytest.raises(VoiceModelError,match='manifest_invalid'): store.download(threading.Event())
    store=ModelStore(tmp_path,manifest());store.root.parent.mkdir(parents=True)
    outside=tmp_path/'outside';outside.mkdir();(outside/'keep').write_text('keep')
    store.root.symlink_to(outside,target_is_directory=True)
    with pytest.raises(VoiceModelError,match='unsafe_path'): store.delete()
    assert (outside/'keep').read_text()=='keep' and store.tombstone.exists()


def test_cross_process_lock_blocks_second_mutation(tmp_path):
    first=ModelStore(tmp_path,manifest());second=ModelStore(tmp_path,manifest())
    with first.operation_lock():
        with pytest.raises(VoiceModelError,match='busy'): second.delete()
    assert not (tmp_path/'response_voice.install.lock').exists()


class FakeProcess:
    def __init__(self,*args): self.events=queue.Queue();self.sent=[];self.closed=False;self.reaped=False
    def send(self,request): self.sent.append(request);self.events.put({'state':'synthesizing'})
    def receive(self,timeout): return self.events.get(timeout=timeout)
    def close(self): self.closed=True
    def reap(self): self.close();self.reaped=True


def test_lazy_off_thread_bounded_queue_cancel_and_unload(tmp_path):
    store=ready_store(tmp_path);made=[];creator_threads=[]
    def factory(*args):
        creator_threads.append(threading.get_ident());p=FakeProcess();made.append(p);return p
    service=VoiceService(Config(tmp_path),store=store,process_factory=factory,runtime_check=lambda p:True)
    assert not made
    start=time.monotonic();assert service.submit('Hello','en');assert time.monotonic()-start<.1
    until(lambda:bool(made) and made[0].sent)
    assert creator_threads[0]!=threading.get_ident()
    for i in range(10): service.submit(f'Queued {i}','en')
    assert service._queue.qsize()<=2
    service.cancel();until(lambda:made[0].reaped)
    assert not service.active and made[0].closed
    service.close()


def test_playback_echo_survives_delayed_asr_and_idle_unloads(tmp_path):
    made=[]
    def factory(*args): p=FakeProcess();made.append(p);return p
    service=VoiceService(Config(tmp_path),store=ready_store(tmp_path),process_factory=factory,runtime_check=lambda p:True)
    assert service.submit('Hello','en');until(lambda:bool(made) and made[0].sent)
    made[0].events.put({'state':'playing'});until(lambda:service.snapshot()['playing'])
    record=SimpleNamespace(capture_started_at=time.monotonic(),captured_at=time.monotonic()+.01)
    assert service.rejects_recording(record)
    made[0].events.put({'state':'done'});until(lambda:made[0].reaped)
    service._echo_until=0
    assert service.rejects_recording(record)
    assert not service.rejects_recording(SimpleNamespace(capture_started_at=time.monotonic()+2,captured_at=time.monotonic()+3))
    service.close()


def test_cancel_then_new_answer_uses_new_process(tmp_path):
    made=[]
    def factory(*args): p=FakeProcess();made.append(p);return p
    service=VoiceService(Config(tmp_path),store=ready_store(tmp_path),process_factory=factory,runtime_check=lambda p:True)
    service.submit('First','en');until(lambda:made and made[0].sent)
    service.cancel();service.submit('Second','en')
    until(lambda:len(made)==2 and made[1].sent)
    assert made[1].sent[0]['text']=='Second' and made[0].reaped
    service.close();until(lambda:made[1].reaped)


def test_worker_supported_api_omits_style_and_clone():
    from jarvis.response_voice.worker import generate
    model=Mock();model.generate_custom_voice.return_value=(['fake'],24000)
    for language in ('ru','en','de','fr','es','it','pt'):
        for speaker in ('Aiden','Serena'):
            generate(model,{'text':'sample','language':language,'speaker':speaker})
            kwargs=model.generate_custom_voice.call_args.kwargs
            assert set(kwargs)=={'text','language','speaker','max_new_tokens','do_sample'}
    with pytest.raises(ValueError): generate(model,{'text':'x','language':'xx','speaker':'Aiden'})


def test_settings_page_localized_close_reopen_is_safe(app,tmp_path):
    from jarvis.ui.response_voice_page import ResponseVoicePage
    from jarvis.response_voice.text import ROWS,CODES,text
    for row in ROWS.values(): assert len(row)==7 and all(row)
    for code in CODES:
        config=Config(tmp_path/code);config.data['app']['language']=code
        store=ModelStore(config.paths.root,manifest(),FakeDownload(b'correct model'))
        service=VoiceService(config,store=store,runtime_check=lambda p:False,runtime_installer=Mock())
        page=ResponseVoicePage(config,service=service)
        assert page.enabled.text()==text('enabled',code)
        assert page.speaker.count()==2 and page.speaker.itemData(1)=='Serena'
        assert text('unchecked',code) in page.status.text()
        page._operation('download')
        until(lambda:not page.service.snapshot()['running']);page.refresh()
        assert text('verified',code) in page.status.text()
        assert text('runtime_missing',code) in page.status.text()
        service.close()
        page.close_operations();page.close_operations();page.deleteLater();app.processEvents()
    assert len(ROWS['help'][0])>500


def test_settings_close_during_operation_does_not_touch_deleted_widget(app,tmp_path):
    from jarvis.ui.response_voice_page import ResponseVoicePage
    entered=threading.Event();release=threading.Event();store=ModelStore(tmp_path,manifest())
    def delayed(cancel, progress):
        entered.set();release.wait(2)
        if cancel.is_set(): raise Cancelled('cancelled')
        return {'state':'verified'}
    store.check=delayed
    service=VoiceService(Config(tmp_path),store=store,runtime_check=lambda p:False)
    page=ResponseVoicePage(service.config,service=service)
    page._operation('check');assert entered.wait(1)
    page.close_operations();page.deleteLater();app.processEvents();release.set()
    until(lambda:not service.snapshot()['running']);assert service.snapshot()['state']=='cancelled'
    page2=ResponseVoicePage(service.config,service=service)
    assert not page2._closed;page2.close_operations();service.close()


def test_settings_invalid_values_are_bounded(tmp_path):
    config=Config(tmp_path,volume='bad',cpu_threads=999,max_characters=-1,speaker='actor')
    data=options(config)
    assert data['volume']==80 and data['cpu_threads']==4 and data['max_characters']==100 and data['speaker']=='Aiden'


def test_dedicated_section_persists_without_changing_microphone(app,tmp_path,monkeypatch):
    from jarvis.config import ConfigManager
    from jarvis.ui.settings_dialog import SettingsDialog, QMessageBox
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    monkeypatch.setenv('APPDATA',str(tmp_path/'roaming'))
    monkeypatch.setattr(SettingsDialog,'_refresh_microphones_async',lambda *_:None)
    monkeypatch.setattr(SettingsDialog,'_start_game_cache_stats158',lambda *_:None)
    monkeypatch.setattr(ConfigManager,'apply_autostart',lambda *_:None)
    monkeypatch.setattr(ConfigManager,'apply_wake_service',lambda *_:None)
    errors=[];monkeypatch.setattr(QMessageBox,'critical',lambda *a:errors.append(a))
    config=ConfigManager(Path(__file__).resolve().parents[1]/'config/default_settings.json')
    config.data['app']['language']='ru'
    dialog=SettingsDialog(config)
    assert dialog._settings_ready160 and dialog._settings_pages160[-1][1]=='Голос'
    assert dialog._settings_pages160[0][1]=='Микрофон и wake word'
    microphone=dialog.voice_enabled.isChecked()
    dialog.response_voice_page.enabled.setChecked(True)
    dialog.response_voice_page.speaker.setCurrentIndex(1)
    dialog._save(close=False)
    assert not errors
    assert config.get('response_voice.enabled') is True and config.get('response_voice.speaker')=='Serena'
    assert config.get('voice.enabled')==microphone
    dialog.reject();dialog.deleteLater();app.processEvents()


@pytest.mark.parametrize('header',[
    'bytes 4-999/1000', 'bytes 4-12/1000', 'bytes 4-999/13',
    'bytes 3-12/13', 'bytes 4-11/13', 'bytes 4-12/*',
    'bytes 4-12/13 extra', 'bytes 4-3/13', '',
])
def test_resume_rejects_every_inconsistent_range_before_write(tmp_path, header):
    blob=b'correct model'
    transport=SimpleNamespace(open=lambda *a:Response(blob[4:],206,{'Content-Range':header}))
    store=ModelStore(tmp_path,manifest(blob),transport)
    store.root.mkdir(parents=True)
    part=store.root/'model.safetensors.part';part.write_bytes(blob[:4])
    with pytest.raises(VoiceModelError,match='invalid_range'):
        store.download(threading.Event())
    assert part.read_bytes()==blob[:4]
    assert not (store.root/'model.safetensors').exists()
    assert not store.ready_cached()


def test_automatic_download_rechecks_deletion_after_lock_acquisition(tmp_path):
    from contextlib import contextmanager
    downloader=ModelStore(tmp_path,manifest(),FakeDownload(b'correct model'))
    deleter=ModelStore(tmp_path,manifest())
    real_lock=downloader.operation_lock
    @contextmanager
    def deletion_wins_race():
        # Another process deletes after download's initial check, before mutation ownership.
        deleter.delete()
        with real_lock(): yield
    downloader.operation_lock=deletion_wins_race
    with pytest.raises(VoiceModelError,match='deleted'):
        downloader.download(threading.Event())
    assert downloader.tombstone.exists()
    assert not downloader.root.exists()
    assert not downloader.transport.calls


@pytest.mark.parametrize('phase',['launch','send'])
@pytest.mark.parametrize('action',['cancel','close'])
def test_gui_actions_never_wait_for_launch_or_pipe_write(tmp_path,phase,action):
    entered=threading.Event();release=threading.Event();made=[]
    class BlockedProcess(FakeProcess):
        def send(self,request):
            if phase=='send':
                entered.set();release.wait(3)
            # Model the OS process killed while writing: never accept post-cancel text.
            if self.closed: raise BrokenPipeError()
            super().send(request)
    def factory(*args):
        process=BlockedProcess();made.append(process)
        if phase=='launch':
            entered.set();release.wait(3)
        return process
    service=VoiceService(Config(tmp_path),store=ready_store(tmp_path),process_factory=factory,runtime_check=lambda p:True)
    try:
        assert service.submit('Old request','en') and entered.wait(1)
        # Run GUI operations on another test thread so a regression cannot hang pytest.
        completed=threading.Event();results=[]
        def gui_actions():
            results.append(service.snapshot())
            getattr(service,action)()
            results.append(service.snapshot())
            completed.set()
        caller=threading.Thread(target=gui_actions,daemon=True);caller.start()
        assert completed.wait(.25), f'{action}/snapshot waited for blocked {phase}'
        assert not results[-1]['active']
        if phase=='send': assert made[0].closed
        release.set();until(lambda:made[0].reaped)
        assert not made[0].sent
        if action=='cancel':
            # A cancelled generation must not steal the next request or overwrite its state.
            service.submit('New request','en')
            until(lambda:len(made)==2 and made[1].sent)
            assert made[1].sent[0]['text']=='New request'
    finally:
        release.set();service.close()
        if made: until(lambda:all(p.reaped for p in made))


class StrictProcess(FakeProcess):
    def __init__(self,*args):
        super().__init__(*args);self.reap_count=0
    def send(self,request):
        if self.closed: raise BrokenPipeError('helper was already killed')
        super().send(request)
    def reap(self):
        self.reap_count+=1;super().reap()


class GatedIdleQueue(queue.Queue):
    """Freeze exactly the idle get entered after the first answer completes."""
    def __init__(self):
        super().__init__(maxsize=2)
        self.arm=threading.Event();self.entered=threading.Event();self.release=threading.Event()
    def get(self,*args,**kwargs):
        if self.arm.is_set():
            self.arm.clear();self.entered.set()
            if not self.release.wait(3): raise AssertionError('test did not release idle queue')
        return super().get(*args,**kwargs)


def retained_service(tmp_path):
    config=Config(tmp_path);config.data['response_voice']['idle_unload_seconds']=15
    made=[]
    def factory(*args): process=StrictProcess();made.append(process);return process
    service=VoiceService(config,store=ready_store(tmp_path),process_factory=factory,runtime_check=lambda p:True)
    gate=GatedIdleQueue();service._queue=gate
    return service,made,gate


def enter_idle_get(service,made,gate):
    assert service.submit('First','en')
    until(lambda:made and made[0].sent)
    gate.arm.set();made[0].events.put({'state':'done'})
    assert gate.entered.wait(1)
    assert service.snapshot()['voice_state']=='idle'


@pytest.mark.parametrize('mode',['cancel','repeated_cancel','disable_reenable'])
def test_idle_cancel_revokes_helper_before_immediate_new_reply(tmp_path,mode):
    service,made,gate=retained_service(tmp_path)
    try:
        enter_idle_get(service,made,gate)
        service.cancel()
        if mode=='repeated_cancel':
            for _ in range(4): service.cancel()
        if mode=='disable_reenable':
            service.config.data['response_voice']['enabled']=False
            service.cancel()
            assert not service.submit('Must not speak while disabled','en')
            service.config.data['response_voice']['enabled']=True
        assert service._process is None and len(service._retired_processes)==1
        assert made[0].closed and not made[0].reaped
        assert service.submit('Second','en')
        gate.release.set()
        until(lambda:len(made)==2 and made[1].sent)
        assert made[0].reap_count==1 and made[0].sent[0]['text']=='First'
        assert len(made[0].sent)==1 and made[1].sent[0]['text']=='Second'
        assert not service._retired_processes
    finally:
        gate.release.set();service.close()
        until(lambda:all(p.reaped for p in made))


def test_close_while_idle_reaps_retired_helper_and_rejects_new_work(tmp_path):
    service,made,gate=retained_service(tmp_path)
    try:
        enter_idle_get(service,made,gate)
        service.close();service.close()
        assert service._process is None and made[0].closed
        assert not service.submit('After close','en')
        assert not service.snapshot()['active']
        gate.release.set();until(lambda:made[0].reaped and service._thread is None)
        assert made[0].reap_count==1 and len(made)==1
    finally:
        gate.release.set();service.close()


def test_active_repeated_cancel_discards_stale_result_then_recovers(tmp_path):
    service,made,gate=retained_service(tmp_path)
    try:
        assert service.submit('First','en');until(lambda:made and made[0].sent)
        made[0].events.put({'state':'playing'});until(lambda:service.snapshot()['playing'])
        service.cancel();service.cancel();service.cancel()
        made[0].events.put({'state':'playing'});made[0].events.put({'state':'done'})
        assert service.submit('Second','en')
        until(lambda:len(made)==2 and made[1].sent)
        until(lambda:service.snapshot()['voice_state']=='synthesizing')
        assert made[0].reap_count==1 and not service.snapshot()['playing']
        assert made[1].sent[0]['text']=='Second'
        made[1].events.put({'state':'playing'});until(lambda:service.snapshot()['playing'])
        made[1].events.put({'state':'done'});until(lambda:service.snapshot()['voice_state']=='idle')
    finally:
        gate.release.set();service.close();until(lambda:all(p.reaped for p in made))


def test_failed_helper_is_retired_before_next_response(tmp_path):
    service,made,gate=retained_service(tmp_path)
    try:
        assert service.submit('First','en');until(lambda:made and made[0].sent)
        made[0].events.put({'state':'synthesis_failed'})
        until(lambda:made[0].reaped and service.snapshot()['voice_state']=='synthesis_failed')
        assert service._process is None
        assert service.submit('Second','en')
        until(lambda:len(made)==2 and made[1].sent)
        assert made[0].reap_count==1 and made[1].sent[0]['text']=='Second'
    finally:
        gate.release.set();service.close();until(lambda:all(p.reaped for p in made))


@pytest.mark.parametrize('action',['cancel','close','new_generation'])
def test_cancelled_cache_probe_cannot_publish_checking_or_leave_active(tmp_path,action):
    entered=threading.Event();release=threading.Event();calls=[];made=[]
    store=ready_store(tmp_path)
    original_check=store.check;store.check=Mock(wraps=original_check)
    def gated_cache():
        calls.append(1)
        if len(calls)==1:
            entered.set();release.wait(3);return False
        return True
    store.ready_cached=gated_cache
    def factory(*args):process=StrictProcess();made.append(process);return process
    service=VoiceService(Config(tmp_path),store=store,process_factory=factory,runtime_check=lambda p:True)
    try:
        assert service.submit('Old','en') and entered.wait(1)
        if action=='close': service.close()
        else: service.cancel()
        if action=='new_generation': assert service.submit('New','en')
        release.set()
        if action=='new_generation':
            until(lambda:made and made[0].sent)
            assert made[0].sent[0]['text']=='New'
            made[0].events.put({'state':'done'})
        until(lambda:service._thread is None)
        snapshot=service.snapshot()
        assert snapshot['voice_state']=='idle' and not snapshot['active']
        store.check.assert_not_called()
        assert len(made)==(1 if action=='new_generation' else 0)
    finally:
        release.set();service.close()


@pytest.mark.parametrize('action',['cancel','close','new_generation','model_operation'])
def test_stale_voice_verification_progress_cannot_overwrite_owner(tmp_path,action):
    entered=threading.Event();release=threading.Event();callback_delivered=threading.Event()
    model_entered=threading.Event();model_release=threading.Event()
    callbacks=[];made=[];cache_calls=[]
    store=ready_store(tmp_path)
    def cache():cache_calls.append(1);return len(cache_calls)>1
    store.ready_cached=cache
    def check(cancel,progress):
        if threading.current_thread().name=='NEO-Voice-Model':
            progress(state='checking',done=5,total=13)
            model_entered.set();model_release.wait(3)
            return {'state':'verified','done':13,'total':13}
        callbacks.append(progress);entered.set();release.wait(3)
        # Deliberately deliver late even though cancellation already occurred.
        progress(state='checking',done=999,total=1000)
        callback_delivered.set()
        raise Cancelled('cancelled')
    store.check=check
    def factory(*args):process=StrictProcess();made.append(process);return process
    service=VoiceService(Config(tmp_path),store=store,process_factory=factory,runtime_check=lambda p:True)
    try:
        assert service.submit('Old','en') and entered.wait(1)
        if action=='close':service.close()
        elif action=='model_operation':
            assert service.model_operation('check') and model_entered.wait(1)
        else:service.cancel()
        if action=='new_generation': assert service.submit('New','en')
        expected=service.snapshot()
        release.set();assert callback_delivered.wait(1)
        if action=='new_generation':
            until(lambda:made and made[0].sent)
            # Stored callbacks remain bound to the old generation after the loop advances.
            callbacks[0](state='checking',done=777,total=1000)
            assert made[0].sent[0]['text']=='New'
            made[0].events.put({'state':'done'})
        until(lambda:service._thread is None)
        snapshot=service.snapshot()
        assert snapshot['done']==expected['done'] and snapshot['state']==expected['state']
        assert snapshot['voice_state']=='idle' and not snapshot['active']
        if action=='model_operation':
            assert snapshot['running'] and snapshot['done']==5
            model_release.set();until(lambda:not service.snapshot()['running'])
            assert service.snapshot()['state']=='verified'
    finally:
        release.set();model_release.set();service.close()


def test_cancelled_model_progress_cannot_replace_cancelling_status(tmp_path):
    entered=threading.Event();release=threading.Event();late=threading.Event();finish=threading.Event()
    store=ready_store(tmp_path)
    def check(cancel,progress):
        entered.set();release.wait(3)
        progress(state='checking',done=12,total=13)
        late.set();finish.wait(3)
        raise Cancelled('cancelled')
    store.check=check
    service=VoiceService(Config(tmp_path),store=store,runtime_check=lambda p:True)
    try:
        assert service.model_operation('check') and entered.wait(1)
        service.cancel_operation();release.set();assert late.wait(1)
        assert service.snapshot()['state']=='cancelling' and service.snapshot()['done']==0
        finish.set();until(lambda:not service.snapshot()['running'])
        assert service.snapshot()['state']=='cancelled'
    finally:
        release.set();finish.set();service.close()


def test_completed_model_callback_is_retired_before_new_operation(tmp_path):
    callbacks=[];entered=threading.Event();release=threading.Event()
    store=ready_store(tmp_path)
    def check(cancel,progress):
        callbacks.append(progress)
        if len(callbacks)==2:
            progress(state='checking',done=4,total=13)
            entered.set();release.wait(3)
        return {'state':'verified','done':13,'total':13}
    store.check=check
    service=VoiceService(Config(tmp_path),store=store,runtime_check=lambda p:True)
    try:
        assert service.model_operation('check')
        until(lambda:not service.snapshot()['running'])
        callbacks[0](state='checking',done=999,total=1000)
        assert service.snapshot()['state']=='verified' and service.snapshot()['done']==13
        assert service.model_operation('check') and entered.wait(1)
        callbacks[0](state='checking',done=777,total=1000)
        assert service.snapshot()['done']==4
        release.set();until(lambda:not service.snapshot()['running'])
    finally:
        release.set();service.close()


def test_live_verification_publishes_terminal_model_status(tmp_path):
    store=ready_store(tmp_path);store._verified_signature=None
    made=[]
    def factory(*args):process=StrictProcess();made.append(process);return process
    service=VoiceService(Config(tmp_path),store=store,process_factory=factory,runtime_check=lambda p:True)
    try:
        assert service.submit('Current','en');until(lambda:made and made[0].sent)
        assert service.snapshot()['state']=='verified' and service.snapshot()['done']==13
        made[0].events.put({'state':'done'});until(lambda:service._thread is None)
        assert service.snapshot()['state']=='verified' and not service.active
    finally:
        service.close()
