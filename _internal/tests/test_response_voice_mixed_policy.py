"""Explicit pinned mixed identities. Every test is offline and uses synthetic bytes."""
from __future__ import annotations
import copy
import hashlib
import io
import threading
from types import SimpleNamespace
from urllib.request import Request
from unittest.mock import Mock
import pytest
from jarvis.testing.isolation import external_io_blocked
from jarvis.response_voice.model_store import (
    ModelStore as ProductionStore, VoiceModelError, Cancelled, MODEL_ID, REVISION,
    INTEGRITY_POLICY, CHUNK_BYTES, HTTPTransport, HTTPSOnlyRedirectHandler,
    load_manifest, validate_manifest, stream_digest, verify_file,
)
from tests.test_response_voice_qwen import ModelStore, Response, FakeDownload


@pytest.fixture(autouse=True)
def no_external_io():
    with external_io_blocked():yield


# Independently obtained with installed `git hash-object --stdin`, no -w and no repository writes.
# Values were checked on 2026-10-05 before freezing this source-only change.
GIT_VECTORS = (
    ('','e69de29bb2d1d6434b8b29ae775ad8c2e48c5391'),
    ('68656c6c6f0a','ce013625030ba8dba906f756967f9e9ca394464a'),
    ('d09fd180d0b8d0b2d0b5d1822c204e454f21','45a3fb7360814f44489ab8badc6d29a5b9d15bff'),
    ('00017f80ff0a','bf1ff9b256b627f98e95436ff4aabba83eb76e2a'),
    ('610a620a','422c2b7ab3b3c668038da977e4e93a5fc623169c'),
    ('610d0a620d0a','c30dea8a3641ea99b125d04d599d843712292759'),
)


@pytest.mark.parametrize('encoded,expected',GIT_VECTORS)
def test_git_object_vectors_match_git_hash_object(encoded,expected):
    blob=bytes.fromhex(encoded)
    assert stream_digest(io.BytesIO(blob),len(blob),'git-blob-sha1')==expected
    assert hashlib.sha1(blob).hexdigest()!=expected


def synthetic_manifest(blob,algorithm):
    name='config.json' if algorithm=='git-blob-sha1' else 'model.safetensors'
    digest=hashlib.sha1(b'blob '+str(len(blob)).encode()+b'\0'+blob).hexdigest() if algorithm=='git-blob-sha1' else hashlib.sha256(blob).hexdigest()
    return {'schema_version':2,'integrity_policy':INTEGRITY_POLICY,'model_id':MODEL_ID,'revision':REVISION,
            'total_bytes':len(blob),'files':[{'path':name,'size':len(blob),'storage':'git' if algorithm=='git-blob-sha1' else 'lfs',
                                            'integrity':{'algorithm':algorithm,'digest':digest}}]}


def test_shipped_policy_is_exactly_ten_git_and_two_lfs_identities():
    data=load_manifest();files=validate_manifest(data)
    git=[f for f in files if f['storage']=='git'];weights=[f for f in files if f['storage']=='lfs']
    assert len(files)==12 and len(git)==10 and len(weights)==2
    assert sum(f['size'] for f in git)==4467205
    assert sum(f['size'] for f in weights)==2493919668
    assert data['total_bytes']==2498386873
    assert {f['path'] for f in weights}=={'model.safetensors','speech_tokenizer/model.safetensors'}
    assert all(f['integrity']['algorithm']=='sha256' for f in weights)
    assert all(f['integrity']['algorithm']=='git-blob-sha1' for f in git)


@pytest.mark.parametrize('algorithm',['git-blob-sha1','sha256'])
def test_same_length_corruption_and_raw_git_sha1_never_verify(tmp_path,algorithm):
    blob=b'publisher bytes';entry=synthetic_manifest(blob,algorithm)['files'][0]
    path=tmp_path/entry['path'];path.write_bytes(blob)
    verify_file(path,entry)
    path.write_bytes(b'X'+blob[1:])
    with pytest.raises(VoiceModelError,match='checksum_failed'):verify_file(path,entry)
    path.write_bytes(blob)
    if algorithm=='git-blob-sha1':
        entry=copy.deepcopy(entry);entry['integrity']['digest']=hashlib.sha1(blob).hexdigest()
        with pytest.raises(VoiceModelError,match='checksum_failed'):verify_file(path,entry)


@pytest.mark.parametrize('algorithm',['git-blob-sha1','sha256'])
@pytest.mark.parametrize('corrupt_prefix',[False,True])
def test_resume_verifies_complete_reconstructed_file(tmp_path,algorithm,corrupt_prefix):
    blob=b'publisher bytes';data=synthetic_manifest(blob,algorithm);network=FakeDownload(blob)
    store=ModelStore(tmp_path,data,network);store.root.mkdir(parents=True)
    name=data['files'][0]['path'];part=store.root/(name+'.part')
    part.write_bytes((b'X'+blob[1:4]) if corrupt_prefix else blob[:4])
    if corrupt_prefix:
        with pytest.raises(VoiceModelError,match='checksum_failed'):store.download(threading.Event())
        assert not part.exists() and not (store.root/name).exists() and not store.ready_cached()
    else:
        store.download(threading.Event());assert (store.root/name).read_bytes()==blob and store.ready_cached()
        calls=len(network.calls);store.download(threading.Event());assert len(network.calls)==calls
    assert network.calls[0][1]==4


@pytest.mark.parametrize('algorithm',['git-blob-sha1','sha256'])
def test_truncated_download_is_resumable_but_not_installed(tmp_path,algorithm):
    blob=b'publisher bytes';data=synthetic_manifest(blob,algorithm)
    short=SimpleNamespace(open=lambda *a:Response(blob[:5]))
    store=ModelStore(tmp_path,data,short);name=data['files'][0]['path']
    with pytest.raises(VoiceModelError,match='size_mismatch'):store.download(threading.Event())
    assert (store.root/(name+'.part')).read_bytes()==blob[:5]
    assert not (store.root/name).exists() and not store.ready_cached()
    resumed=FakeDownload(blob);store.transport=resumed;store.download(threading.Event())
    assert resumed.calls[0][1]==5 and store.ready_cached()


@pytest.mark.parametrize('algorithm',['git-blob-sha1','sha256'])
def test_stream_byte_cap_truncation_cancellation_and_unknown_algorithms(algorithm):
    blob=b'a'*(CHUNK_BYTES*2+7);sizes=[]
    class BoundedStream(io.BytesIO):
        def read(self,n):sizes.append(n);return super().read(n)
    stream_digest(BoundedStream(blob),len(blob),algorithm)
    assert max(sizes)<=CHUNK_BYTES
    with pytest.raises(VoiceModelError,match='size_mismatch'):stream_digest(io.BytesIO(blob[:-1]),len(blob),algorithm)
    with pytest.raises(VoiceModelError,match='size_mismatch'):stream_digest(io.BytesIO(blob+b'X'),len(blob),algorithm)
    cancel=threading.Event()
    class CancellingStream(io.BytesIO):
        def read(self,n):cancel.set();return super().read(n)
    with pytest.raises(Cancelled):stream_digest(CancellingStream(blob),len(blob),algorithm,cancel)
    with pytest.raises(VoiceModelError,match='manifest_invalid'):stream_digest(io.BytesIO(blob),len(blob),'sha1')


def test_mixed_check_reuse_and_promotion_share_verifier(tmp_path):
    ordinary=b'{"exact":true}\r\n';weight=b'synthetic weights'
    left=synthetic_manifest(ordinary,'git-blob-sha1');right=synthetic_manifest(weight,'sha256')
    data=copy.deepcopy(left);data['files']+=right['files'];data['total_bytes']+=right['total_bytes']
    payloads={'config.json':ordinary,'model.safetensors':weight};calls=[]
    def download(url,offset):
        name=url.rsplit('/',1)[-1];blob=payloads[name];calls.append((name,offset))
        return Response(blob[offset:],206 if offset else 200,{'Content-Range':f'bytes {offset}-{len(blob)-1}/{len(blob)}'})
    store=ModelStore(tmp_path,data,SimpleNamespace(open=download))
    store.download(threading.Event());assert store.check()['state']=='verified'
    assert len(calls)==2;store.download(threading.Event());assert len(calls)==2
    target=store.root/'config.json';target.write_bytes(b'X'+ordinary[1:])
    with pytest.raises(VoiceModelError,match='checksum_failed'):store.check()
    store.download(threading.Event());assert len(calls)==3 and calls[-1][0]=='config.json'


MALFORMED = (
    'schema1','unknown_schema','boolean_schema','wrong_policy','wrong_revision','wrong_model',
    'omitted','duplicate','extra','parent_path','absolute_path','windows_path','backslash_path',
    'empty_component','dot_component','trailing_slash','non_string_path','wrong_size','boolean_size',
    'negative_size','float_size','wrong_total','boolean_total','missing_digest','uppercase_digest',
    'wrong_digest','null_digest','short_digest','unknown_algorithm','raw_sha1','unknown_storage',
    'weight_downgrade','tokenizer_weight_downgrade','git_upgrade','legacy_digest','legacy_complete',
)


@pytest.mark.parametrize('case',MALFORMED)
def test_malformed_production_policy_fails_before_network_or_tombstone_change(tmp_path,case):
    data=copy.deepcopy(load_manifest());first=data['files'][0]
    if case=='schema1':data['schema_version']=1
    elif case=='unknown_schema':data['schema_version']=3
    elif case=='boolean_schema':data['schema_version']=True
    elif case=='wrong_policy':data['integrity_policy']='sha1-fallback'
    elif case=='wrong_revision':data['revision']='0'*40
    elif case=='wrong_model':data['model_id']='Qwen/other'
    elif case=='omitted':data['files'].pop()
    elif case=='duplicate':data['files'][-1]=copy.deepcopy(first)
    elif case=='extra':data['files'].append(copy.deepcopy(first))
    elif case in ('parent_path','absolute_path','windows_path','backslash_path','empty_component','dot_component','trailing_slash','non_string_path'):
        first['path']={'parent_path':'../README.md','absolute_path':'/README.md','windows_path':'C:/README.md',
                       'backslash_path':'speech_tokenizer\\config.json','empty_component':'speech_tokenizer//config.json',
                       'dot_component':'./README.md','trailing_slash':'README.md/','non_string_path':None}[case]
    elif case in ('wrong_size','boolean_size','negative_size','float_size'):
        first['size']={'wrong_size':3264,'boolean_size':True,'negative_size':-1,'float_size':3263.0}[case]
    elif case=='wrong_total':data['total_bytes']+=1
    elif case=='boolean_total':data['total_bytes']=True
    elif case=='missing_digest':first['integrity'].pop('digest')
    elif case=='uppercase_digest':first['integrity']['digest']=first['integrity']['digest'].upper()
    elif case=='wrong_digest':first['integrity']['digest']='0'*40
    elif case=='null_digest':first['integrity']['digest']=None
    elif case=='short_digest':first['integrity']['digest']='a'*39
    elif case=='unknown_algorithm':first['integrity']['algorithm']='auto'
    elif case=='raw_sha1':first['integrity']['algorithm']='sha1'
    elif case=='unknown_storage':first['storage']='auto'
    elif case in ('weight_downgrade','tokenizer_weight_downgrade'):
        name='model.safetensors' if case=='weight_downgrade' else 'speech_tokenizer/model.safetensors'
        entry=next(f for f in data['files'] if f['path']==name)
        entry['storage']='git';entry['integrity']={'algorithm':'git-blob-sha1','digest':'a'*40}
    elif case=='git_upgrade':first['storage']='lfs';first['integrity']={'algorithm':'sha256','digest':'a'*64}
    elif case=='legacy_digest':first['sha256']=None
    elif case=='legacy_complete':data['hashes_complete']=True
    else:raise AssertionError(case)
    transport=Mock();store=ProductionStore(tmp_path,data,transport);store.tombstone.write_text('{}')
    with pytest.raises(VoiceModelError):store.download(threading.Event(),explicit=True)
    assert store.tombstone.exists() and not store.root.exists() and not store.ready_cached()
    transport.open.assert_not_called()
    from prepare_response_voice import prepare
    install=Mock();result=prepare(tmp_path,explicit=True,store=store,install=install)
    assert result['voice_ready'] is False and result['state'].startswith('manifest_')
    install.assert_not_called();transport.open.assert_not_called()


@pytest.mark.parametrize('destination',['http://cdn.example/file','ftp://cdn.example/file','file:///tmp/file','https://user:secret@cdn.example/file'])
def test_redirect_downgrade_and_credential_urls_rejected(destination):
    request=Request('https://huggingface.co/source',headers={'Range':'bytes=4-'})
    handler=HTTPSOnlyRedirectHandler()
    with pytest.raises(VoiceModelError,match='https_required'):
        handler.redirect_request(request,None,302,'Found',{},destination)


def test_https_redirect_keeps_range_and_transport_checks_initial_and_final_urls():
    request=Request('https://huggingface.co/source',headers={'Range':'bytes=4-'})
    redirected=HTTPSOnlyRedirectHandler().redirect_request(request,None,302,'Found',{},'https://cdn.example/file')
    assert redirected.full_url=='https://cdn.example/file' and redirected.get_header('Range')=='bytes=4-'
    opener=Mock();transport=HTTPTransport(opener)
    with pytest.raises(VoiceModelError,match='https_required'):transport.open('http://huggingface.co/source',0)
    opener.open.assert_not_called()
    response=Response(b'data');response.geturl=lambda:'http://cdn.example/file';opener.open.return_value=response
    with pytest.raises(VoiceModelError,match='https_required'):transport.open('https://huggingface.co/source',4)
    assert response.closed
    response=Response(b'data',headers={'Content-Encoding':'gzip'});response.geturl=lambda:'https://cdn.example/file';opener.open.return_value=response
    with pytest.raises(VoiceModelError,match='unexpected_encoding'):transport.open('https://huggingface.co/source',4)
    assert response.closed


def test_delete_marker_symlink_is_refused_without_touching_target(tmp_path):
    outside=tmp_path/'keep';outside.write_text('unchanged')
    (tmp_path/'response_voice.deleted.tmp').symlink_to(outside)
    store=ProductionStore(tmp_path)
    with pytest.raises(VoiceModelError,match='unsafe_path'):store.delete()
    assert outside.read_text()=='unchanged'


def test_model_junction_is_refused_before_network(tmp_path,monkeypatch):
    from pathlib import Path
    store=ProductionStore(tmp_path,transport=Mock())
    monkeypatch.setattr(Path,'is_junction',lambda path:path==store.root,raising=False)
    with pytest.raises(VoiceModelError,match='unsafe_path'):store.download(threading.Event())
    store.transport.open.assert_not_called()


def test_all_languages_describe_weaker_mixed_integrity_accurately():
    from jarvis.response_voice.text import ROWS,POLICY_HELP
    assert len(POLICY_HELP)==7
    for help_text,policy in zip(ROWS['help'],POLICY_HELP):
        assert policy in help_text
        assert 'SHA-1' in policy and 'SHA-256' in policy and '10' in policy and '2' in policy
    for key in ('manifest_incomplete','manifest_invalid','checking'):
        assert len(ROWS[key])==7 and all(ROWS[key])
