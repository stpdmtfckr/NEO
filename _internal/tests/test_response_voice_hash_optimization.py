"""One hash pass per download operation, with unchanged-file identity checks."""
from __future__ import annotations
import copy
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from jarvis.testing.isolation import external_io_blocked
from jarvis.response_voice import model_store
from jarvis.response_voice.model_store import VoiceModelError,Cancelled
from tests.test_response_voice_qwen import ModelStore,Response
from tests.test_response_voice_mixed_policy import synthetic_manifest


@pytest.fixture(autouse=True)
def inert():
    with external_io_blocked():yield


def mixed_store(tmp_path):
    payloads={'config.json':b'{"value":true}','model.safetensors':b'fake weights'}
    left=synthetic_manifest(payloads['config.json'],'git-blob-sha1')
    right=synthetic_manifest(payloads['model.safetensors'],'sha256')
    data=copy.deepcopy(left);data['files']+=right['files'];data['total_bytes']+=right['total_bytes']
    def download(url,offset):
        blob=payloads[url.rsplit('/',1)[-1]]
        return Response(blob[offset:],206 if offset else 200,{'Content-Range':f'bytes {offset}-{len(blob)-1}/{len(blob)}'})
    return ModelStore(tmp_path,data,SimpleNamespace(open=download)),payloads


def test_new_and_existing_downloads_hash_each_file_once_and_explicit_check_remains_full(tmp_path,monkeypatch):
    store,payloads=mixed_store(tmp_path);real=model_store.stream_digest;count=[]
    def measured(stream,size,algorithm,cancel=None):
        count.append((size,algorithm));return real(stream,size,algorithm,cancel)
    monkeypatch.setattr(model_store,'stream_digest',measured)
    result=store.download(threading.Event())
    assert result['verification_mode']=='download_file_hashes' and store.ready_cached()
    assert len(count)==2 and sum(size for size,_ in count)==sum(map(len,payloads.values()))
    count.clear();store.download(threading.Event())
    assert len(count)==2 and sum(size for size,_ in count)==sum(map(len,payloads.values()))
    count.clear();result=store.check()
    assert result['verification_mode']=='full_check' and len(count)==2
    another=ModelStore(tmp_path,copy.deepcopy(store.manifest),store.transport)
    assert not another.ready_cached(), 'No process-to-process cached receipt may imply a fresh check'


def test_changed_earlier_file_invalidates_download_verification(tmp_path,monkeypatch):
    store,payloads=mixed_store(tmp_path);store.download(threading.Event())
    real=model_store.verify_file
    def verify_then_mutate(path,entry,cancel=None):
        identity=real(path,entry,cancel)
        if entry['path']=='model.safetensors':
            first=store.root/'config.json';first.write_bytes(b'X'+payloads['config.json'][1:])
        return identity
    monkeypatch.setattr(model_store,'verify_file',verify_then_mutate)
    with pytest.raises(VoiceModelError,match='changed_during_check'):store.download(threading.Event())
    assert not store.ready_cached()


def test_replaced_file_with_same_size_and_mtime_is_detected(tmp_path,monkeypatch):
    import os
    store,payloads=mixed_store(tmp_path);store.download(threading.Event())
    real=model_store.verify_file
    def verify_then_replace(path,entry,cancel=None):
        identity=real(path,entry,cancel)
        if entry['path']=='model.safetensors':
            first=store.root/'config.json';stat=first.stat();replacement=store.root/'replacement'
            replacement.write_bytes(b'X'+payloads['config.json'][1:]);os.utime(replacement,ns=(stat.st_atime_ns,stat.st_mtime_ns));os.replace(replacement,first)
        return identity
    monkeypatch.setattr(model_store,'verify_file',verify_then_replace)
    with pytest.raises(VoiceModelError,match='changed_during_check'):store.download(threading.Event())
    assert not store.ready_cached()


def test_part_changed_after_hash_never_promotes(tmp_path,monkeypatch):
    store,_=mixed_store(tmp_path);real=model_store.verify_file
    def corrupt_after_hash(path,entry,cancel=None):
        identity=real(path,entry,cancel)
        if path.name.endswith('.part'):path.write_bytes(b'X'+path.read_bytes()[1:])
        return identity
    monkeypatch.setattr(model_store,'verify_file',corrupt_after_hash)
    with pytest.raises(VoiceModelError,match='changed_during_check'):store.download(threading.Event())
    assert not (store.root/'config.json').exists() and not store.ready_cached()


def test_late_cancellation_never_marks_files_verified(tmp_path,monkeypatch):
    store,_=mixed_store(tmp_path);store.download(threading.Event())
    cancel=threading.Event();real=model_store.verify_file
    def verify_then_cancel(path,entry,event=None):
        identity=real(path,entry,event)
        if entry['path']=='model.safetensors':cancel.set()
        return identity
    monkeypatch.setattr(model_store,'verify_file',verify_then_cancel)
    with pytest.raises(Cancelled):store.download(cancel)
    assert not store.ready_cached()


def test_missing_file_at_final_identity_check_never_marks_ready(tmp_path,monkeypatch):
    store,_=mixed_store(tmp_path);store.download(threading.Event())
    real=model_store.verify_file
    def verify_then_remove(path,entry,cancel=None):
        identity=real(path,entry,cancel)
        if entry['path']=='model.safetensors':(store.root/'config.json').unlink()
        return identity
    monkeypatch.setattr(model_store,'verify_file',verify_then_remove)
    with pytest.raises(VoiceModelError,match='changed_during_check'):store.download(threading.Event())
    assert not store.ready_cached() and store._verified_signature is None
