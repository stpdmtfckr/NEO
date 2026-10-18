"""Model maintenance with synthetic HTTP responses; no installed model touched."""
from unittest.mock import patch
import json
import pytest
from jarvis.model_management import ModelOperation, exact_model, local_endpoint

class Response:
    def __init__(self, data=None, *, status=200, lines=None):
        self.data, self.status_code, self.lines = data, status, lines or []
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))
    def json(self): return self.data
    def iter_lines(self, **_): return iter(self.lines)

class Session:
    def __init__(self, before=None, after=None, lines=None):
        self.before, self.after = before or [], after or []
        self.lines = lines or []
        self.calls = []
        self.closed = False
    def get(self, url, **kw):
        self.calls.append(('get', url, kw))
        models = self.after if any(x[0] in ('post','delete') for x in self.calls) else self.before
        return Response({'models':[{'name':m} for m in models]})
    def post(self, url, **kw):
        self.calls.append(('post',url,kw));return Response(lines=self.lines)
    def delete(self, url, **kw):
        self.calls.append(('delete',url,kw));return Response()
    def close(self): self.closed=True

def run(op, session):
    results, errors, progress = [], [], []
    op.completed.connect(results.append);op.failed.connect(errors.append)
    op.progress.connect(lambda text,pct:progress.append((text,pct)))
    with patch('jarvis.model_management.requests.Session',return_value=session):op._run()
    return results, errors, progress

@pytest.mark.parametrize('url',['https://example.org','http://localhost.evil.org','http://127.0.0.1@evil.org','http://u:p@localhost','http://localhost?secret=x','http://localhost/api','file:///tmp/x'])
def test_only_credential_free_loopback(url):
    with pytest.raises(ValueError): local_endpoint(url)

def test_exact_selected_tag():
    assert not exact_model('qwen2.5:0.5b',['qwen2.5:7b'])
    assert not exact_model('qwen2.5',['qwen2.5:7b'])
    assert exact_model('qwen2.5',['qwen2.5:latest'])=='qwen2.5:latest'

def test_check_does_not_mutate_or_substitute():
    session=Session(['qwen2.5:7b'])
    result, errors, _=run(ModelOperation('check','qwen2.5:0.5b','http://localhost:11434'),session)
    assert result[0]['status']=='missing' and not errors
    assert [c[0] for c in session.calls]==['get'] and session.closed

def test_download_verifies_selected_model():
    session=Session(after=['qwen2.5:0.5b'],lines=[json.dumps({'status':'pulling','total':400,'completed':200}).encode(),b'{"status":"success"}'])
    result,errors,progress=run(ModelOperation('download','qwen2.5:0.5b','http://localhost:11434'),session)
    assert not errors and result[0]['status']=='installed'
    assert session.calls[1][2]['json']['model']=='qwen2.5:0.5b'
    assert any(pct==50 for _,pct in progress)

def test_success_message_without_installed_model_is_error():
    result,errors,_=run(ModelOperation('download','qwen2.5:0.5b','http://localhost'),Session(lines=[b'{"status":"success"}']))
    assert not result and errors

def test_delete_targets_only_requested_model_and_verifies():
    session=Session(before=['qwen2.5:0.5b','keep:4b'],after=['keep:4b'])
    result, errors,_=run(ModelOperation('delete','qwen2.5:0.5b','http://localhost'),session)
    assert result[0]['status']=='deleted' and not errors
    assert session.calls[1][2]['json']=={'model':'qwen2.5:0.5b'}

def test_delete_unconfirmed_is_error():
    session=Session(before=['m:1'],after=['m:1'])
    result,errors,_=run(ModelOperation('delete','m:1','http://localhost'),session)
    assert not result and errors

def test_cancel_before_start_never_touches_network():
    session=Session();op=ModelOperation('download','m:1','http://localhost');op.cancel()
    result,errors,_=run(op,session)
    assert result[0]['status']=='cancelled' and not session.calls and not errors

def test_redirect_rejected():
    with pytest.raises(ValueError):ModelOperation._validate_response(Response(status=302))

def test_conversation_model_selection_does_not_prefer_agent_or_wrong_size():
    from jarvis.ollama_client import OllamaClient, OllamaError
    class Config:
        def get(self,key,default=None):return {'ollama.chat_model':'qwen2.5:0.5b','llm_agent.enabled':True,'llm_agent.model':'neo-agent'}.get(key,default)
    client=OllamaClient(Config())
    with patch.object(client,'_list_models',return_value=['neo-agent:latest','qwen2.5:7b']):
        with pytest.raises(OllamaError):client.resolve_chat_model()
    with patch.object(client,'_list_models',return_value=['neo-agent:latest','qwen2.5:0.5b']):
        assert client.resolve_chat_model()=='qwen2.5:0.5b'


def test_readiness_retries_and_caches_empty_model_list():
    from jarvis import ollama_status
    import requests
    ollama_status.reset_status()
    session=Session()
    calls=[requests.ConnectTimeout(),Response({'models':[]})]
    def get(*a,**kw):
        value=calls.pop(0)
        if isinstance(value,Exception):raise value
        return value
    session.get=get;session.__class__.__enter__=lambda self:self;session.__class__.__exit__=lambda self,*a:self.close()
    with patch('jarvis.ollama_status.requests.Session',return_value=session),patch('jarvis.ollama_status.time.sleep'):
        first=ollama_status.readiness('http://localhost:11434')
        second=ollama_status.readiness('http://localhost:11434')
    assert first['ready'] and second['ready'] and not calls and first['models']==[]
    ollama_status.reset_status()


def test_readiness_does_not_claim_model_ready_when_api_is_missing():
    from jarvis import ollama_status
    import requests
    ollama_status.reset_status()
    session=Session();session.get=lambda *a,**k:(_ for _ in ()).throw(requests.ConnectTimeout())
    session.__class__.__enter__=lambda self:self;session.__class__.__exit__=lambda self,*a:self.close()
    with patch('jarvis.ollama_status.requests.Session',return_value=session),patch('jarvis.ollama_status.time.sleep'):
        result=ollama_status.readiness('http://localhost:11434')
    assert not result['ready'] and result['status']=='unavailable' and 'Запустите' in result['message']
    ollama_status.reset_status()

def test_cancel_during_stream_closes_without_false_download_success():
    op=ModelOperation('download','m:1','http://localhost');session=Session()
    class CancelResponse(Response):
        def iter_lines(self,**kw):
            yield b'{"status":"pulling","total":100,"completed":10}'
            op.cancel()
            yield b'{"status":"success"}'
    def post(url,**kw):session.calls.append(('post',url,kw));return CancelResponse()
    session.post=post
    result,errors,_=run(op,session)
    assert result[0]['status']=='cancelled' and not errors and session.closed
    assert [c[0] for c in session.calls]==['get','post']


def test_cancelled_conversation_does_not_disable_next_turn():
    from jarvis.runtime import local_chat169
    from jarvis.runtime.cancellation import OperationCancelled
    class C:
        def get(self,key,default=None):return default
    class O:
        def local_chat_enabled(self):return True
        def chat_text(self,*a,**k):raise OperationCancelled('stop')
    local_chat169.reset_availability()
    with pytest.raises(OperationCancelled):local_chat169.answer(O(),C(),'hello','en')
    assert local_chat169._UNAVAILABLE_UNTIL[0]==0

def test_startup_opt_out_survives_legacy_migration():
    from jarvis.runtime.vision_policy import migrate_vision_startup
    data={'vision_enabled':True,'auto_start':False}
    migrate_vision_startup(data,settings_version=100)
    assert data['auto_start'] is False and data['auto_pull_models'] is False


def test_disabled_autostart_never_creates_bootstrap_worker(tmp_path):
    from jarvis.runtime import vision_startup
    class C:
        paths=type('P',(),{'root':tmp_path})()
        def get(self,key,default=None):return {'ollama.auto_start':False,'ollama.vision_enabled':True,'local_chat.enabled':True}.get(key,default)
    with patch.object(vision_startup,'_is_windows',return_value=True),patch.object(vision_startup.threading,'Thread') as thread:
        assert vision_startup.start_vision_backend(C()) is False
        thread.assert_not_called()


def test_missing_service_is_one_actionable_notice_not_two_model_failures(tmp_path):
    from jarvis.runtime.self_test import run_self_test
    class C:
        paths=type('P',(),{'root':tmp_path})()
        def get(self,key,default=None):return {'ollama.vision_enabled':True,'local_chat.enabled':True}.get(key,default)
    with patch('jarvis.ollama_status.readiness',return_value={'ready':False,'models':[],'message':'Запустите Ollama.','status':'unavailable'}) as readiness:
        result=run_self_test(C())
    readiness.assert_called_once()
    rows=[x for x in result['checks'] if x['name'].startswith('ollama')]
    assert len([x for x in rows if x['status']=='UNAVAILABLE'])==1
    assert all(x['status']=='SKIP' for x in rows if x['name'].endswith('_model'))
    assert not any('ConnectTimeout' in x['detail'] for x in rows)

def test_delete_network_loss_after_cancel_is_uncertain_not_cancelled():
    import requests
    session=Session(before=['m:1']);op=ModelOperation('delete','m:1','http://localhost')
    def delete(*a,**kw):op.cancel();raise requests.ConnectionError('connection lost after commit')
    session.delete=delete
    result,errors,_=run(op,session)
    assert not result and errors and 'не подтверждён' in errors[0]

def test_app_shutdown_cancels_owned_operation():
    from PySide6.QtCore import QObject, Signal
    class AppOwner(QObject):
        aboutToQuit=Signal()
    app=AppOwner()
    with patch('jarvis.model_management.QCoreApplication.instance',return_value=app):
        op=ModelOperation('download','m:1','http://localhost')
    assert op.parent() is app
    app.aboutToQuit.emit()
    assert op._cancelled.is_set()

@pytest.mark.parametrize('language',['en','de','fr','es','it','pt'])
def test_model_diagnostics_are_localized_and_distinguish_vision(language):
    import re
    from jarvis.runtime.self_test_localization import render_check
    image=render_check({'name':'ollama_model','status':'UNAVAILABLE','detail':'Image model is not installed: vision:4b'},language)
    chat=render_check({'name':'ollama_chat_model','status':'UNAVAILABLE','detail':'Conversation model is not installed: qwen2.5:0.5b'},language)
    assert not re.search('[А-Яа-я]',image+chat)
    assert 'vision:4b' in image and '0.5B' in image and 'qwen2.5:0.5b' in chat

def test_server_error_is_redacted_before_chat_progress():
    result,errors,_=run(ModelOperation('download','m:1','http://localhost'),Session(lines=[b'{"error":"proxy api_key=private123 unavailable"}']))
    assert not result and errors and 'private123' not in errors[0]
