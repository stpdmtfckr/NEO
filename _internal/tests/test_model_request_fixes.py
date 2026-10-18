"""Inert regressions for request identity, model provenance and cancellation."""
from types import SimpleNamespace
from unittest.mock import patch
import json
import threading
import time
import pytest
import requests
import jarvis.ollama_client as oc
from jarvis.model_management import ModelOperation
from jarvis.runtime.cancellation import OperationCancelled, cancellation_manager

class Config:
    def __init__(self, values=None):
        self.values={'ollama.base_url':'http://127.0.0.1:11434','ollama.auto_start':False,
            'local_chat.enabled':True,'local_chat.model':'chat:0.5b','ollama.chat_model':'chat:0.5b',
            'ollama.vision_enabled':True,'ollama.vision_model':'vision:1b',
            'llm_agent.enabled':True,'llm_agent.model':'neo-agent','llm_agent.base_model':'base:3b',
            'llm_agent.allow_large_fallback':False}
        self.values.update(values or {})
    def get(self,key,default=None):return self.values.get(key,default)

class Response:
    status_code=200;text='';encoding='utf-8'
    def __init__(self,data=None,lines=None,hook=None,status=200):
        self.data=data or {};self.lines=lines or [];self.hook=hook;self.status_code=status
        self.text=json.dumps(self.data)
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def raise_for_status(self):
        if self.status_code>=400:raise requests.HTTPError(str(self.status_code))
    def json(self):
        if self.hook:self.hook()
        return self.data
    def iter_lines(self,**kw):yield from (self.lines() if callable(self.lines) else self.lines)
    def close(self):pass

class Session:
    def __init__(self):self.calls=[];self.on_get=None;self.on_post=None;self.on_delete=None
    def get(self,url,**kw):
        self.calls.append(('get',url,kw))
        if self.on_get:return self.on_get(url,kw)
        return Response({'models':[{'name':x} for x in ('chat:0.5b','vision:1b','neo-agent:latest','base:3b')]})
    def post(self,url,**kw):
        self.calls.append(('post',url,kw))
        if self.on_post:return self.on_post(url,kw)
        if url.endswith('/api/show'):return Response({'modelfile':'FROM base:3b\n'})
        content='{"reply":"old answer"}'
        return Response({'message':{'content':content}},[json.dumps({'message':{'content':content},'done':True})])
    def delete(self,url,**kw):
        self.calls.append(('delete',url,kw))
        if self.on_delete:return self.on_delete(url,kw)
        return Response()
    def close(self):pass
    def generations(self):return [x for x in self.calls if x[1].endswith('/api/chat')]

@pytest.fixture
def client(monkeypatch):
    cancellation_manager().reset()
    cfg=Config();session=Session();c=oc.OllamaClient(cfg);c._session=session
    monkeypatch.setattr(oc.requests,'Session',lambda:session)
    return cfg,c,session

MODES=('text','text_stream','planner','planner_stream','image','image_stream')
def invoke(c,mode,callback=None):
    msgs=[{'role':'user','content':'inert request'}]
    if mode.startswith('text'):return c.chat_text(msgs,on_delta=(callback or (lambda _:None)) if mode.endswith('stream') else None)
    if mode.startswith('planner'):return c.agent_json(msgs,{},on_reply=(callback or (lambda _:None)) if mode.endswith('stream') else None)
    msgs[0]['images']=['aW5lcnQ=']
    return c.chat_stream(msgs,on_content=callback) if mode.endswith('stream') else c.chat(msgs)

def change(cfg,c,mode,what):
    if what=='server':cfg.values['ollama.base_url']='http://127.0.0.1:22434'
    elif what=='model':cfg.values['local_chat.model' if mode.startswith('text') else 'llm_agent.base_model' if mode.startswith('planner') else 'ollama.vision_model']='replacement:7b'
    elif what=='role':cfg.values['local_chat.enabled' if mode.startswith('text') else 'llm_agent.enabled' if mode.startswith('planner') else 'ollama.vision_enabled']=False
    elif what=='cancel':c.cancel_active()
    elif what=='manager_cancel':cancellation_manager().cancel('audit cancel')

@pytest.mark.parametrize('mode',MODES)
@pytest.mark.parametrize('what',('server','model','role','cancel','manager_cancel'))
def test_change_while_queued_blocks_every_generation(client,mode,what):
    cfg,c,s=client
    with patch.object(oc,'_acquire_generation_slot',side_effect=lambda:change(cfg,c,mode,what)),patch.object(oc,'_release_generation_slot'):
        with pytest.raises(OperationCancelled):invoke(c,mode)
    assert s.generations()==[]

@pytest.mark.parametrize('mode',MODES)
def test_unchanged_requests_work_and_release_generation_slot(client,mode):
    _,c,s=client;seen=[]
    result=invoke(c,mode,seen.append)
    assert result and len(s.generations())==1
    assert oc._GENERATION_ACTIVE==0 and not oc._GENERATION_LOCK.locked()
    if mode.endswith('stream'):assert seen

@pytest.mark.parametrize('mode',('text','planner','image'))
@pytest.mark.parametrize('what',('server','model','role','cancel'))
def test_changed_nonstream_response_is_rejected(client,mode,what):
    cfg,c,s=client
    default=s.post
    def post(url,kw):
        if url.endswith('/api/show'):return Response({'modelfile':'FROM base:3b'})
        return Response({'message':{'content':'{"reply":"old answer"}'}},hook=lambda:change(cfg,c,mode,what))
    s.on_post=post
    with pytest.raises(OperationCancelled):invoke(c,mode)
    assert len(s.generations())==1

@pytest.mark.parametrize('mode',('text_stream','planner_stream','image_stream'))
@pytest.mark.parametrize('what',('server','model','role','cancel'))
@pytest.mark.parametrize('when',('before_chunk','last_callback','after_stream'))
def test_stream_checks_before_chunks_after_callbacks_and_eof(client,mode,what,when):
    cfg,c,s=client;seen=[]
    def lines():
        if when=='before_chunk':change(cfg,c,mode,what)
        piece='{"reply":"old answer"}' if mode.startswith('planner') else 'old answer'
        yield json.dumps({'message':{'content':piece}})
        if when=='after_stream':change(cfg,c,mode,what)
    def post(url,kw):
        if url.endswith('/api/show'):return Response({'modelfile':'FROM base:3b'})
        return Response(lines=lines)
    s.on_post=post
    def callback(piece):
        seen.append(piece)
        if when=='last_callback':change(cfg,c,mode,what)
    with pytest.raises(OperationCancelled):invoke(c,mode,callback)
    assert (not seen) if when=='before_chunk' else seen

@pytest.mark.parametrize('mode',('planner','planner_stream'))
@pytest.mark.parametrize('what',('server','model','role','cancel'))
def test_think_retry_checks_identity_before_second_send(client,mode,what):
    cfg,c,s=client
    def post(url,kw):
        if url.endswith('/api/show'):return Response({'modelfile':'FROM base:3b'})
        change(cfg,c,mode,what)
        return Response({'error':'think not supported'},status=400)
    s.on_post=post
    with pytest.raises(OperationCancelled):invoke(c,mode)
    assert len(s.generations())==1

@pytest.mark.parametrize('what',('server','model','role','cancel'))
def test_resolution_failure_after_change_is_cancellation_not_cooldown(client,what):
    from jarvis.runtime import local_chat169
    cfg,c,s=client;local_chat169.reset_availability()
    def get(url,kw):
        change(cfg,c,'text',what)
        raise requests.ConnectionError('inert failure')
    s.on_get=get
    with pytest.raises(OperationCancelled):local_chat169.answer(c,cfg,'hello','en')
    assert local_chat169._UNAVAILABLE_UNTIL[0]==0 and not s.generations()
    cancellation_manager().reset();cfg.values.update({'local_chat.enabled':True,'ollama.base_url':'http://127.0.0.1:11434','local_chat.model':'chat:0.5b'})
    s.on_get=None
    assert local_chat169.answer(c,cfg,'hello','en')


def test_inventory_cache_owner_and_inflight_invalidation(client):
    cfg,c,s=client
    assert c._list_models()
    cfg.values['ollama.base_url']='http://127.0.0.1:22434'
    s.on_get=lambda *a:Response({'models':[{'name':'new:1b'}]})
    assert c._list_models()==['new:1b']
    assert len(s.calls)==2
    def get(url,kw):
        cfg.values['ollama.base_url']='http://127.0.0.1:33434';c._model_cache=(0,[])
        return Response({'models':[{'name':'old:1b'}]})
    s.on_get=get
    with pytest.raises(OperationCancelled):c._list_models(force=True)
    assert c._model_cache==(0,[])


def test_capabilities_cache_owner_changes_with_endpoint(client):
    cfg,c,s=client
    s.on_post=lambda url,kw:Response({'capabilities':['vision'] if '11434' in url else ['completion']})
    assert c.model_capabilities('same:1b')==['vision']
    cfg.values['ollama.base_url']='http://127.0.0.1:22434'
    assert c.model_capabilities('same:1b')==['completion']
    assert len(s.calls)==2


def test_cancelled_inventory_failure_never_starts_service(client):
    cfg,c,s=client;cfg.values['ollama.auto_start']=True
    def get(*a):c.cancel_active();raise requests.ConnectionError('inert')
    s.on_get=get
    with patch.object(oc,'ensure_ollama_running') as launch:
        with pytest.raises(OperationCancelled):c._list_models()
        launch.assert_not_called()

@pytest.mark.parametrize('hook',('normal','stale','unknown','equal_blob','wrong_blob','tuned_match','tuned_wrong_size'))
def test_planner_alias_provenance(client,hook):
    cfg,c,s=client
    # Explicitly exercise the preferred alias; the stock prompt-only alias now
    # follows the shared base to avoid warming a duplicate model runner.
    cfg.values['llm_agent.fine_tuned']='neo-agent'
    def post(url,kw):
        assert url.endswith('/api/show')
        candidate=kw['json']['model'].startswith('neo-agent')
        if hook=='normal':return Response({'modelfile':'FROM base:3b'})
        if hook=='unknown':return Response({})
        if hook=='stale':return Response({'modelfile':'FROM base:0.5b'})
        if hook in ('equal_blob','wrong_blob'):
            char='a' if (not candidate or hook=='equal_blob') else 'b'
            return Response({'modelfile':'FROM /fake/sha256-'+char*64})
        return Response({'model_info':{'general.architecture':'qwen2','general.parameter_count':500 if candidate and hook=='tuned_wrong_size' else 3000}})
    s.on_post=post
    expected='neo-agent:latest' if hook in ('normal','equal_blob') else 'base:3b'
    assert c.resolve_agent_model()==expected
    assert not s.generations() and all('/api/show' in x[1] or '/api/tags' in x[1] for x in s.calls)

@pytest.mark.parametrize('what',('server','model','role','cancel'))
def test_alias_metadata_change_is_rejected(client,what):
    cfg,c,s=client
    cfg.values['llm_agent.fine_tuned']='neo-agent'
    def post(*a):change(cfg,c,'planner',what);return Response({'modelfile':'FROM base:3b'})
    s.on_post=post
    with pytest.raises(OperationCancelled):c.resolve_agent_model()


def run_operation(op,s):
    results=[];errors=[];op.completed.connect(results.append);op.failed.connect(errors.append)
    with patch('jarvis.model_management.requests.Session',return_value=s):op._run()
    return results,errors

@pytest.mark.parametrize('action',('start','download'))
def test_maintenance_cancelled_failed_probe_never_launches(action):
    op=ModelOperation(action,'chat:0.5b','http://127.0.0.1:11434',auto_start=True);s=Session()
    def get(*a):op.cancel();raise requests.ConnectionError('inert')
    s.on_get=get
    with patch('jarvis.ollama_runtime.ensure_ollama_running') as launch:
        result,errors=run_operation(op,s);launch.assert_not_called()
    assert not errors and result[0]['status']=='cancelled'

@pytest.mark.parametrize('action',('start','list','check','download'))
def test_maintenance_cancelled_final_probe_not_ready(action):
    op=ModelOperation(action,'chat:0.5b','http://127.0.0.1:11434');s=Session()
    def get(*a):op.cancel();return Response({'models':[{'name':'chat:0.5b'}]})
    s.on_get=get
    result,errors=run_operation(op,s)
    assert not errors and result[0]['status']=='cancelled' and not result[0]['ready']


def test_delete_commit_survives_repeated_cancel_during_verification():
    op=ModelOperation('delete','chat:0.5b','http://127.0.0.1:11434');s=Session();deleted=[]
    def delete(*a):deleted.append(True);op.cancel();return Response()
    def get(*a):
        if deleted:op.cancel()
        return Response({'models':[] if deleted else [{'name':'chat:0.5b'}]})
    s.on_get=get;s.on_delete=delete
    result,errors=run_operation(op,s)
    assert not errors and result[0]['status']=='deleted' and op._cancelled.is_set()
    with pytest.raises(AttributeError):op.model='unrelated:7b'
    with pytest.raises(AttributeError):op.base_url='http://127.0.0.1:22434'


def test_unload_stays_at_captured_endpoint(client):
    from jarvis.runtime.model_life173 import release
    cfg,c,s=client
    def get(*a):cfg.values['ollama.base_url']='http://127.0.0.1:22434';return Response({'models':[{'name':'chat:0.5b'}]})
    s.on_get=get
    assert release(c)==1
    assert all('11434' in row[1] for row in s.calls)


def test_real_generation_lock_queue_rejects_configuration_change(client):
    cfg,c,s=client;entered=threading.Event();errors=[]
    old=c.resolve_chat_model
    c.resolve_chat_model=lambda:(entered.set(),old())[1]
    assert oc._GENERATION_LOCK.acquire(timeout=.5)
    def worker():
        cancellation_manager().reset()
        try:invoke(c,'text')
        except Exception as e:errors.append(e)
    thread=threading.Thread(target=worker)
    try:
        thread.start();assert entered.wait(1)
        deadline=time.monotonic()+1
        while oc._GENERATION_WAITING<1 and time.monotonic()<deadline:time.sleep(.005)
        assert oc._GENERATION_WAITING>=1
        cfg.values['ollama.base_url']='http://127.0.0.1:22434'
    finally:oc._GENERATION_LOCK.release();thread.join(2)
    assert not thread.is_alive() and len(errors)==1 and isinstance(errors[0],OperationCancelled)
    assert not s.generations() and oc._GENERATION_ACTIVE==0

@pytest.mark.parametrize('key',('llm_agent.model','llm_agent.fine_tuned'))
def test_explicit_custom_model_is_authoritative_without_alias_metadata(client,key):
    cfg,c,s=client;cfg.values[key]='my-custom:7b'
    s.on_get=lambda *a:Response({'models':[{'name':'my-custom:7b'},{'name':'base:3b'}]})
    assert c.resolve_agent_model()=='my-custom:7b'
    assert not any('/api/show' in row[1] for row in s.calls)
    assert invoke(c,'planner')['_model']=='my-custom:7b'

@pytest.mark.parametrize('key',('llm_agent.model','llm_agent.fine_tuned'))
def test_missing_explicit_custom_model_does_not_fallback(client,key):
    cfg,c,s=client;cfg.values[key]='my-custom:7b'
    with pytest.raises(oc.OllamaError,match='my-custom:7b'):c.resolve_agent_model()
    assert not any('/api/show' in row[1] for row in s.calls)


def test_same_size_unrelated_managed_finetune_is_not_provenance(client):
    cfg,c,s=client;cfg.values['llm_agent.fine_tuned']='neo-agent-ft'
    s.on_get=lambda *a:Response({'models':[{'name':'neo-agent-ft:latest'},{'name':'base:3b'}]})
    s.on_post=lambda url,kw:Response({'model_info':{'general.architecture':'qwen2','general.parameter_count':3000},'modelfile':'FROM /fake/sha256-'+('a' if kw['json']['model']=='base:3b' else 'b')*64})
    assert c.resolve_agent_model()=='base:3b'

@pytest.mark.parametrize('what',('server','model','role','cancel'))
@pytest.mark.parametrize('response_kind',('empty','context_error'))
def test_image_analysis_retry_retains_outer_identity(client,tmp_path,what,response_kind):
    from PIL import Image
    cfg,c,s=client;image=tmp_path/'inert.png';Image.new('RGB',(8,8)).save(image)
    c._apply_active_game_priority_guard=lambda **kw:None
    calls=[]
    def chat(*a,**kw):
        calls.append(kw)
        change(cfg,c,'image',what)
        if response_kind=='context_error':raise oc.OllamaError('context limit')
        return {'message':{'content':''}}
    c.chat=chat
    with pytest.raises(OperationCancelled):c.analyze_image(image,'inert')
    assert len(calls)==1


def test_release_lifecycle_failure_does_not_guess_unload_target(client):
    from jarvis.runtime import model_life173
    _,c,s=client
    with patch.object(model_life173,'release',side_effect=RuntimeError('inert')):
        assert c.release_models()==0
    assert s.calls==[]


def test_maintenance_cancel_during_malformed_result_is_cancellation():
    op=ModelOperation('check','chat:0.5b','http://127.0.0.1:11434');s=Session()
    def get(*a):op.cancel();raise ValueError('inert malformed response')
    s.on_get=get
    result,errors=run_operation(op,s)
    assert result[0]['status']=='cancelled' and not errors

@pytest.mark.parametrize('language',('ru','en','de','fr','es','it','pt'))
@pytest.mark.parametrize('kind',('changed','missing'))
def test_new_request_diagnostics_use_configured_language(client,language,kind):
    import re
    cfg,c,s=client;cfg.values['app.language']=language
    if kind=='missing':
        cfg.values['llm_agent.model']='custom:1b'
        with pytest.raises(oc.OllamaError) as error:c.resolve_agent_model()
        assert 'custom:1b' in str(error.value)
    else:
        request=c._capture_request('chat');cfg.values['local_chat.model']='new:7b'
        with pytest.raises(OperationCancelled) as error:c._check_request(request)
    text=str(error.value)
    assert bool(re.search('[А-Яа-я]',text)) == (language=='ru')
    assert text and 'model' not in text if language=='ru' else text
