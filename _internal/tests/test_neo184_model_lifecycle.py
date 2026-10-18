from types import SimpleNamespace
from unittest.mock import patch
import requests
from jarvis.runtime import model_life173 as life

class Config:
    def get(self,key,default=None):
        return {'ollama.chat_model':'qwen2.5:0.5b','ollama.vision_model':'vision:4b'}.get(key,default)

class Response:
    def __init__(self,fail=False):self.fail=fail;self.closed=False
    def raise_for_status(self):
        if self.fail:raise requests.HTTPError('500')
    def close(self):self.closed=True

class Session:
    def __init__(self,fail=False):self.calls=[];self.fail=fail
    def post(self,url,**kw):self.calls.append(kw['json']);return Response(self.fail)


def test_release_never_unloads_another_size():
    session=Session();ollama=SimpleNamespace(config=Config(),_session=session,agent_model_candidates=lambda:[])
    with patch.object(life,'loaded_models',return_value={'qwen2.5:0.5b','qwen2.5:7b','unrelated:latest'}):
        assert life.release(ollama)==1
    assert session.calls==[{'model':'qwen2.5:0.5b','keep_alive':0}]


def test_unavailable_backend_does_not_start_multiple_unload_timeouts():
    session=Session();ollama=SimpleNamespace(config=Config(),_session=session)
    with patch.object(life,'loaded_models',side_effect=requests.ConnectionError):assert life.release(ollama)==0
    assert session.calls==[]


def test_failed_unload_not_reported_successful():
    session=Session(True);ollama=SimpleNamespace(config=Config(),_session=session)
    with patch.object(life,'loaded_models',return_value={'qwen2.5:0.5b'}):assert life.release(ollama)==0


def test_preload_singleflight_even_after_rate_limit():
    life._PRELOAD_INFLIGHT[0]=False;life._LAST_PRELOAD[0]=0
    ollama=SimpleNamespace(config=Config())
    class HeldThread:
        def __init__(self,**kw):self.target=kw['target']
        def start(self):pass
    try:
        with patch.object(life.threading,'Thread',HeldThread):
            assert life.preload(ollama,now=100)
            assert not life.preload(ollama,now=200)
    finally:life._PRELOAD_INFLIGHT[0]=False;life._LAST_PRELOAD[0]=0


def test_release_singleflight_and_start_error_cleanup():
    life._RELEASE_INFLIGHT[0]=False
    ollama=SimpleNamespace(config=Config())
    with patch.object(life.threading,'Thread') as thread:
        assert life.release_for_game(ollama)
        assert not life.release_for_game(ollama)
        life._RELEASE_INFLIGHT[0]=False;thread.return_value.start.side_effect=RuntimeError('closed')
        assert not life.release_for_game(ollama)
        assert not life._RELEASE_INFLIGHT[0]

def test_cancel_does_not_block_ui_on_slow_response_close():
    import threading,time
    from jarvis.ollama_client import OllamaClient
    entered=threading.Event();release=threading.Event()
    class Slow:
        def close(self):entered.set();release.wait(2)
    client=OllamaClient(Config());response=Slow();client._active_streams[id(response)]=response
    started=time.perf_counter()
    try:
        assert client.cancel_active()==1
        assert time.perf_counter()-started<.2
        assert entered.wait(1)
        thread=client._cancel_cleanup_thread
        client.cancel_active()
        assert client._cancel_cleanup_thread is thread
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()

def test_cancelled_transport_attribute_error_is_normalized():
    from jarvis.ollama_client import OllamaClient
    from jarvis.runtime.cancellation import OperationCancelled,cancellation_manager
    import pytest
    client=OllamaClient(Config());client.resolve_chat_model=lambda:'qwen2.5:0.5b'
    class ClosedResponse:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def raise_for_status(self):pass
        def close(self):pass
        def iter_lines(self,**kw):
            client.cancel_active()
            raise AttributeError("'NoneType' object has no attribute 'readline'")
            yield b''
    client._session=SimpleNamespace(post=lambda *a,**k:ClosedResponse(),close=lambda:None)
    cancellation_manager().reset()
    with pytest.raises(OperationCancelled):client.chat_text([{'role':'user','content':'hello'}],on_delta=lambda x:None)
