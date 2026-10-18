"""Voice failures remain useful without logging private URLs or raw pip output."""
from __future__ import annotations
import errno
import io
import json
import logging
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError,URLError
from unittest.mock import Mock
import pytest
from jarvis.testing.isolation import external_io_blocked
from jarvis.response_voice import diagnostics,runtime
from jarvis.response_voice.diagnostics import RuntimeFailure,OutputClassifier,summarize_failure,record_failure
from jarvis.response_voice.model_store import Cancelled
from jarvis.response_voice.service import VoiceService
from tests.test_response_voice_qwen import Config,ready_store,until

SECRET='super_private_token_123'
SIGNED='https://user:password@example.invalid/model?X-Amz-Signature='+SECRET


@pytest.fixture(autouse=True)
def inert():
    with external_io_blocked():yield


@pytest.mark.parametrize('error,category,numbers',[
    (HTTPError(SIGNED,403,'secret '+SECRET,{},None),'http_error',{'http_status':403}),
    (URLError(TimeoutError(SECRET)),'timeout',{}),
    (PermissionError(errno.EACCES,SECRET,SIGNED),'permission_denied',{'errno':13}),
    (OSError(errno.ENOSPC,SECRET,SIGNED),'disk_full',{'errno':28}),
    (RuntimeFailure(stage='runtime_torch',category='no_matching_distribution',returncode=1),'no_matching_distribution',{'returncode':1}),
    (MemoryError(SECRET),'memory_error',{}),
    (RuntimeError(SIGNED),'unknown',{}),
])
def test_failure_metadata_is_bounded_allowlisted_and_secret_free(error,category,numbers):
    result=summarize_failure(error,action='download',stage='model_download')
    assert result['category']==category
    assert all(result[key]==value for key,value in numbers.items())
    encoded=json.dumps(result)
    assert SECRET not in encoded and 'https' not in encoded and 'password' not in encoded
    assert len(encoded)<450


def test_failure_logs_rotate_and_keep_no_raw_text(tmp_path,monkeypatch,caplog):
    monkeypatch.setattr(diagnostics,'MAX_LOG_BYTES',512)
    with caplog.at_level(logging.WARNING,logger=diagnostics.__name__):
        for _ in range(12):record_failure(tmp_path,HTTPError(SIGNED,403,SECRET,{},None),action='download',stage='model_download')
    paths=list((tmp_path/'logs').glob('response_voice_failures.jsonl*'))
    assert len(paths)==2 and all(path.stat().st_size<=512 for path in paths)
    for path in paths:
        for line in path.read_text().splitlines():
            value=json.loads(line);assert value['http_status']==403 and value['stage']=='model_download'
    assert SECRET not in caplog.text and SIGNED not in caplog.text and 'Traceback' not in caplog.text
    assert all(SECRET not in path.read_text() for path in paths)


def test_logging_failure_cannot_replace_original_error(tmp_path,monkeypatch):
    monkeypatch.setattr(diagnostics,'safe_path',Mock(side_effect=PermissionError(SECRET)))
    result=record_failure(tmp_path,HTTPError(SIGNED,503,SECRET,{},None),action='download',stage='model_download')
    assert result['http_status']==503
    assert not (tmp_path/'logs').exists()


@pytest.mark.parametrize('message,expected',[
    ('ERROR: No matching distribution found for torch','no_matching_distribution'),
    ('ResolutionImpossible: conflicting dependencies','dependency_conflict'),
    ('OSError: [Errno 28] No space left on device','disk_full'),
    ('ImportError: DLL load failed: [WinError 126]','missing_native_library'),
    ('OSError: PortAudio library not found','missing_native_library'),
    ('ModuleNotFoundError: No module named qwen_tts','missing_module'),
    ('SSL: CERTIFICATE_VERIFY_FAILED','certificate_error'),
    ('Read timed out while downloading','read_timeout'),
    ('ProxyError: Cannot connect to proxy; HTTP Error 407','proxy_error'),
    ('getaddrinfo failed: temporary failure in name resolution','dns_error'),
    ('ConnectTimeoutError: connection timed out','connect_timeout'),
    ('Package requires a different Python: 3.12 not in >=3.13','incompatible_python'),
    ('is not a supported wheel on this platform','incompatible_wheel'),
    ('Microsoft Visual C++ 14.0 or greater is required','missing_build_tool'),
    ('rust compiler not found; Failed building wheel','missing_build_tool'),
    ('[WinError 1455] The paging file is too small for this operation','memory_error'),
    ('[WinError 1114] A DLL initialization routine failed','missing_native_library'),
    ('SSLError: TLSV1_ALERT_PROTOCOL_VERSION','tls_error'),
])
def test_output_classifier_retains_enum_only(message,expected):
    classifier=OutputClassifier()
    data=(SIGNED+'\n'+message+'\n'+SECRET).encode()
    for index in range(0,len(data),7):
        classifier.feed(data[index:index+7]);assert len(classifier._tail)<=256
    assert classifier.category==expected
    classifier.finish();assert classifier._tail==b''
    assert SECRET not in repr(classifier.__dict__)


class FakePipe(io.BytesIO):
    def __init__(self,data=b'',pending=False,on_read=None):super().__init__(data);self.pending=pending;self.on_read=on_read
    def fileno(self):return 919191
    def read(self,n):
        assert n<=4096
        if self.on_read:self.on_read()
        value=super().read(n)
        return None if not value and self.pending else value


class FakeProcess:
    def __init__(self,pipe,code=0):self.stdout=pipe;self.returncode=code;self.killed=False;self.waited=False
    def poll(self):return self.returncode
    def kill(self):self.killed=True;self.returncode=-9
    def wait(self,timeout):self.waited=True;return self.returncode


def test_nonblocking_runtime_failure_reports_stage_exit_and_classification(monkeypatch):
    monkeypatch.setattr(runtime.os,'set_blocking',lambda fd,flag:None)
    pipe=FakePipe((SIGNED+'\n'+'padding\n'*10000+'ERROR: No matching distribution found for torch\n').encode())
    process=FakeProcess(pipe,1);spawn=Mock(return_value=process)
    with pytest.raises(RuntimeFailure) as caught:
        runtime._run(['fake-python','-m','pip',SIGNED],threading.Event(),stage='runtime_torch',spawn=spawn)
    error=caught.value
    assert error.stage=='runtime_torch' and error.returncode==1 and error.category=='no_matching_distribution'
    assert pipe.closed and process.waited
    assert spawn.call_args.kwargs['stdout']==runtime.subprocess.PIPE
    assert spawn.call_args.kwargs['stderr']==runtime.subprocess.STDOUT
    assert SECRET not in str(error)


def test_nonblocking_runtime_cancel_and_timeout_reap_owned_child(monkeypatch):
    monkeypatch.setattr(runtime.os,'set_blocking',lambda fd,flag:None)
    cancel=threading.Event();process=FakeProcess(FakePipe(pending=True,on_read=cancel.set),None)
    with pytest.raises(Cancelled):runtime._run(['fake'],cancel,spawn=lambda *a,**k:process)
    assert process.killed and process.waited and process.stdout.closed
    process=FakeProcess(FakePipe(pending=True),None)
    start=time.monotonic()
    with pytest.raises(RuntimeFailure,match='runtime_timeout'):
        runtime._run(['fake'],threading.Event(),spawn=lambda *a,**k:process,timeout_seconds=.01)
    assert time.monotonic()-start<.5 and process.killed and process.stdout.closed


def test_runtime_uses_existing_base_python_and_publishes_exact_stage(tmp_path,monkeypatch):
    from jarvis.runtime import process_host171
    real_python=tmp_path/'base'/'python.exe';real_python.parent.mkdir();real_python.write_bytes(b'fake')
    monkeypatch.setattr(process_host171,'base_python',lambda:real_python)
    commands=[];events=[]
    def runner(command,cancel):
        commands.append(command)
        if len(commands)==1:Path(command[-1]).mkdir(parents=True)
        elif len(commands)==2:raise PermissionError(errno.EACCES,SECRET,SIGNED)
    with pytest.raises(RuntimeFailure) as caught:
        runtime.ensure_runtime(tmp_path,threading.Event(),lambda **e:events.append(e),run=runner)
    assert commands[0][:3]==[str(real_python),'-m','venv']
    assert caught.value.stage=='runtime_torch' and caught.value.category=='permission_denied'
    assert caught.value.errno==13
    assert [event['stage'] for event in events]==['runtime_python','runtime_venv','runtime_torch']
    assert not (runtime.runtime_python(tmp_path).parent.parent/'installed.json').exists()


def test_missing_or_branded_only_base_interpreter_is_explicit(tmp_path,monkeypatch):
    from jarvis.runtime import process_host171
    host=tmp_path/'NEO.exe';host.write_bytes(b'fake')
    monkeypatch.setattr(process_host171,'base_python',lambda:host)
    with pytest.raises(RuntimeFailure,match='runtime_python_missing'):runtime.creation_python()


def test_gui_http_failure_is_observable_and_retry_clears_old_code(tmp_path):
    store=ready_store(tmp_path)
    store.download=Mock(side_effect=HTTPError(SIGNED,403,SECRET,{},None))
    service=VoiceService(Config(tmp_path),store=store,runtime_check=lambda p:True,runtime_installer=Mock())
    try:
        assert service.model_operation('download');until(lambda:not service.snapshot()['running'])
        state=service.snapshot();assert state['failure']['http_status']==403
        assert state['failure']['stage']=='model_download' and state['state']=='operation_failed'
        assert (tmp_path/'logs/response_voice_failures.jsonl').is_file()
        assert service.model_operation('check');until(lambda:not service.snapshot()['running'])
        assert service.snapshot()['state']=='verified' and service.snapshot()['failure'] is None
    finally:service.close()


def test_cmd_generic_failure_is_structured_without_traceback(tmp_path):
    from prepare_response_voice import prepare
    store=ready_store(tmp_path);store.download=Mock(side_effect=URLError(SIGNED))
    result=prepare(tmp_path,store=store,install=Mock())
    assert result['voice_ready'] is False and result['failure']['stage']=='model_download'
    assert result['failure']['category']=='connection_error'
    assert SECRET not in json.dumps(result)


def test_cancel_is_not_logged_as_failure(tmp_path):
    store=ready_store(tmp_path);store.download=Mock(side_effect=Cancelled('cancelled'))
    service=VoiceService(Config(tmp_path),store=store,runtime_check=lambda p:True)
    assert service.model_operation('download');until(lambda:not service.snapshot()['running'])
    assert service.snapshot()['state']=='cancelled' and not (tmp_path/'logs').exists()
    service.close()


def test_all_locales_render_safe_stage_code_and_resource_limits():
    from jarvis.response_voice.text import CODES,RESOURCE_HELP,diagnostic_text
    failure=summarize_failure(HTTPError(SIGNED,403,SECRET,{},None),action='download',stage='model_download')
    for language in CODES:
        value=diagnostic_text(failure,language)
        assert 'model_download' in value and 'HTTP 403' in value and SECRET not in value
    assert len(RESOURCE_HELP)==7 and all('FP32' in value for value in RESOURCE_HELP)
    assert not diagnostic_text({'stage':SIGNED,'code':SECRET},'en')


def test_windows_native_and_proxy_codes_survive_without_raw_context():
    classifier=OutputClassifier()
    classifier.feed(('[WinError 126] DLL load failed at C:/private/'+SECRET+'\nHTTP Error 407 '+SIGNED).encode())
    assert classifier.numbers=={'winerror':126,'http_status':407}
    error=RuntimeFailure(stage='runtime_probe',category=classifier.category,returncode=1,**classifier.numbers)
    result=summarize_failure(error,action='download',stage='runtime')
    assert result['winerror']==126 and result['http_status']==407 and result['returncode']==1
    assert SECRET not in json.dumps(result)
    proxy=summarize_failure(URLError('Tunnel connection failed: HTTP Error 407 '+SIGNED),action='download',stage='model_download')
    assert proxy['category']=='proxy_error' and proxy['http_status']==407
