"""R006: bounded NDJSON parsing through real requests.Response, inert raw I/O."""
from types import SimpleNamespace
from unittest.mock import patch
import json
import pytest
import requests
import jarvis.ollama_client as oc
from jarvis.runtime.cancellation import OperationCancelled, cancellation_manager
from jarvis.testing.isolation import external_io_blocked

class Config:
    def __init__(self):
        self.values={'app.language':'en','ollama.base_url':'http://127.0.0.1:11434',
                     'ollama.auto_start':False,'local_chat.model':'chat:0.5b',
                     'llm_agent.model':'custom-planner:1b','llm_agent.base_model':'base:1b',
                     'ollama.vision_model':'vision:1b'}
    def get(self,key,default=None):return self.values.get(key,default)

class Raw:
    def __init__(self,chunks):self.chunks=chunks;self.read=0;self.closed=False;self.released=False;self.args=[]
    def stream(self,amt,decode_content=True):
        self.args.append((amt,decode_content))
        for chunk in self.chunks:
            self.read+=1
            yield chunk
    def close(self):self.closed=True
    def release_conn(self):self.released=True


def response(chunks):
    result=requests.Response();result.status_code=200;result.raw=Raw(chunks)
    result.iter_lines=lambda *a,**k:(_ for _ in ()).throw(AssertionError('requests.iter_lines must never be used'))
    return result


def chunks(data,width):return [data[i:i+width] for i in range(0,len(data),width)]
def encoded(value):return json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8')

@pytest.fixture(autouse=True)
def isolated():
    cancellation_manager().reset()
    with external_io_blocked():yield
    cancellation_manager().reset()

@pytest.fixture
def client():return oc.OllamaClient(Config())

def records(client,data,width=64):
    result=response(chunks(data,width))
    return list(client._stream_records(result,client._capture_request('chat'))),result

@pytest.mark.parametrize('width',(1,2,3,4,7,63,64,65,4096))
@pytest.mark.parametrize('ending',(b'\n',b'\r\n',b''))
def test_unicode_and_record_boundaries(client,width,ending):
    values=[{'message':{'content':'Привет 😀 café 你好\n"quote"'}},{'message':{'content':'終'},'done':True}]
    wire=b'\n\r\n'+encoded(values[0])+b'\r\n'+encoded(values[1])+ending
    actual,result=records(client,wire,width)
    assert actual==values and result.raw.args==[(64,True)]


def test_many_records_in_one_chunk_and_many_small_chunks(client):
    values=[{'message':{'content':str(i)}} for i in range(1000)]
    wire=b'\n'.join(map(encoded,values))+b'\n'
    assert records(client,wire,len(wire))[0]==values
    assert records(client,wire,17)[0]==values


def test_native_record_limit_stops_newline_free_growth_before_more_input(client):
    limit=oc._MAX_STREAM_RECORD_BYTES
    def endless():
        while True:yield b'x'*64
    result=response(endless())
    with pytest.raises(oc.OllamaError,match=str(limit)):
        list(client._stream_records(result,client._capture_request('chat')))
    assert result.raw.read==limit//64+1


def test_huge_single_transport_chunk_is_rejected_before_buffer_copy(client,monkeypatch):
    monkeypatch.setattr(oc,'_MAX_STREAM_RECORD_BYTES',128)
    result=response([b'x'*100000,b'never consumed'])
    with pytest.raises(oc.OllamaError,match='128'):
        list(client._stream_records(result,client._capture_request('chat')))
    assert result.raw.read==1

@pytest.mark.parametrize('ending',(b'\n',b'\r\n',b'\r',b''))
def test_exact_record_byte_limit_and_one_byte_over(client,monkeypatch,ending):
    wire=encoded({'message':{'content':'😀'*7}})
    monkeypatch.setattr(oc,'_MAX_STREAM_RECORD_BYTES',len(wire))
    assert records(client,wire+ending,1)[0][0]['message']['content']=='😀'*7
    monkeypatch.setattr(oc,'_MAX_STREAM_RECORD_BYTES',len(wire)-1)
    with pytest.raises(oc.OllamaError,match='byte limit'):records(client,wire+ending,1)

@pytest.mark.parametrize('wire',[
    b'\v\n',b'\f\n',b'{broken}\n',b'[]\n',b'null\n',b'"scalar"\n',b'1\n',b'{"message":[]}\n',
    b'{"message":{"content":"\xff"}}\n',b'{"x":NaN}\n',b'{"x":Infinity}\n',
    b'{"message":{"content":"raw\nnewline"}}\n',
])
def test_malformed_records_raise_clear_errors(client,wire):
    with pytest.raises(oc.OllamaError,match='invalid UTF-8/JSON'):records(client,wire,3)

@pytest.mark.parametrize('wire',[b'{',b'{"message":',b'{"message":{"content":"unfinished',b'{"message":{"content":"\xf0\x9f'])
def test_partial_final_records_are_not_silently_discarded(client,wire):
    with pytest.raises(oc.OllamaError,match='incomplete UTF-8/JSON'):records(client,wire,1)


def test_bad_later_record_preserves_prior_delivery_but_raises(client):
    first={'message':{'content':'valid first'}}
    result=response([encoded(first)+b'\n{bad}\n'])
    it=client._stream_records(result,client._capture_request('chat'))
    assert next(it)==first
    with pytest.raises(oc.OllamaError,match='invalid'):next(it)

@pytest.mark.parametrize('kind',('manager','epoch','model','server','role'))
def test_cancellation_and_changes_are_checked_without_a_newline(client,kind):
    def incoming():
        yield b'{"message":{"content":"'
        for _ in range(10):yield b'a'
        if kind=='manager':cancellation_manager().cancel('stop')
        elif kind=='epoch':client.cancel_active()
        elif kind=='model':client.config.values['local_chat.model']='new:7b'
        elif kind=='server':client.config.values['ollama.base_url']='http://127.0.0.1:22434'
        else:client.config.values['local_chat.enabled']=False
        yield b'still no newline'
        raise AssertionError('Must stop before reading another chunk')
    result=response(incoming())
    with pytest.raises(OperationCancelled):list(client._stream_records(result,client._capture_request('chat')))
    assert result.raw.read==12


def test_cancellation_after_a_record_stops_before_next_transport_read(client):
    result=response([encoded({'message':{'content':'first'}})+b'\n',b'never read'])
    it=client._stream_records(result,client._capture_request('chat'))
    assert next(it)['message']['content']=='first'
    cancellation_manager().cancel('stop')
    with pytest.raises(OperationCancelled):next(it)
    assert result.raw.read==1


def test_cancellation_between_records_in_same_chunk(client):
    record=encoded({'message':{'content':'first'}})+b'\n'
    it=client._stream_records(response([record*3]),client._capture_request('chat'))
    next(it);cancellation_manager().cancel('stop')
    with pytest.raises(OperationCancelled):next(it)


def test_read_deadline_callback_runs_while_record_is_incomplete(client):
    calls=[]
    def deadline():
        calls.append(True)
        if len(calls)==20:raise oc.OllamaError('read deadline')
    result=response([b'{' for _ in range(100)])
    with pytest.raises(oc.OllamaError,match='read deadline'):
        list(client._stream_records(result,client._capture_request('chat'),on_read=deadline))
    assert result.raw.read<20


def test_legacy_complete_record_adapter_remains_compatible_and_bounded(client,monkeypatch):
    adapter=SimpleNamespace(iter_lines=lambda **kw:iter(['{"message":{"content":"😀"}}']))
    assert list(client._stream_records(adapter,client._capture_request('chat')))[0]['message']['content']=='😀'
    monkeypatch.setattr(oc,'_MAX_STREAM_RECORD_BYTES',16)
    adapter=SimpleNamespace(iter_lines=lambda **kw:iter([b'x'*17]))
    with pytest.raises(oc.OllamaError,match='16'):list(client._stream_records(adapter,client._capture_request('chat')))


def test_real_requests_response_cannot_fall_back_to_line_reader(client):
    result=response([b'{}\n']);result.iter_content=None
    with pytest.raises(oc.OllamaError,match='invalid'):list(client._stream_records(result,client._capture_request('chat')))

@pytest.mark.parametrize('language',('ru','en','de','fr','es','it','pt'))
@pytest.mark.parametrize('kind',('oversized','malformed','truncated'))
def test_stream_error_messages_use_selected_language(client,monkeypatch,language,kind):
    import re
    client.config.values['app.language']=language
    monkeypatch.setattr(oc,'_MAX_STREAM_RECORD_BYTES',32)
    wire=b'x'*33 if kind=='oversized' else b'{bad}\n' if kind=='malformed' else b'{'
    with pytest.raises(oc.OllamaError) as failure:records(client,wire,7)
    assert bool(re.search('[А-Яа-я]',str(failure.value)))==(language=='ru')
    assert 'Ollama' in str(failure.value)

MODES=('text','planner','image')
def setup_stream(client,mode,wire,width=7):
    result=response(chunks(wire,width))
    sent=[]
    def post(url,**kw):sent.append((url,kw));return result
    client._session=SimpleNamespace(post=post,close=lambda:None)
    client.resolve_chat_model=lambda:'chat:0.5b'
    client.resolve_agent_model=lambda:'custom-planner:1b'
    client.resolve_model=lambda *a,**kw:'vision:1b'
    return result,sent

def invoke(client,mode,callback):
    messages=[{'role':'user','content':'inert'}]
    if mode=='text':return client.chat_text(messages,on_delta=callback)
    if mode=='planner':return client.agent_json(messages,{},on_reply=callback)
    messages[0]['images']=['aW5lcnQ=']
    return client.chat_stream(messages,on_content=callback)

@pytest.mark.parametrize('mode',MODES)
def test_normal_stream_end_to_end_keeps_existing_returns_and_callbacks(client,mode):
    pieces=['Привет ', '😀']
    if mode=='planner':pieces=['{"reply":"Привет ', '😀"}']
    wire=b'\r\n'.join(encoded({'message':{'content':p},'done':False}) for p in pieces)+b'\n'+encoded({'done':True})
    result,sent=setup_stream(client,mode,wire,1);seen=[]
    value=invoke(client,mode,seen.append)
    if mode=='text':assert value=='Привет 😀'
    elif mode=='planner':assert value['reply']=='Привет 😀' and value['_model']=='custom-planner:1b'
    else:assert value['message']['content']=='Привет 😀'
    assert ''.join(seen)=='Привет 😀' and len(sent)==1 and (result.raw.closed or result.raw.released)
    assert not client._active_streams and oc._GENERATION_ACTIVE==0

@pytest.mark.parametrize('mode',MODES)
@pytest.mark.parametrize('wire',(b'x'*129,b'{"message":',b'{broken}\n'))
def test_protocol_error_closes_stream_and_releases_slot(client,monkeypatch,mode,wire):
    monkeypatch.setattr(oc,'_MAX_STREAM_RECORD_BYTES',128)
    result,sent=setup_stream(client,mode,wire);seen=[]
    with pytest.raises(oc.OllamaError):invoke(client,mode,seen.append)
    assert not seen and (result.raw.closed or result.raw.released) and not client._active_streams
    assert oc._GENERATION_ACTIVE==0 and not oc._GENERATION_LOCK.locked()

@pytest.mark.parametrize('mode',MODES)
def test_cancel_during_unterminated_stream_closes_and_sends_no_partial_text(client,mode):
    result,sent=setup_stream(client,mode,b'')
    def incoming():
        yield b'{"message":{"content":"unfinished'
        cancellation_manager().cancel('stop')
        yield b'next bytes'
        raise AssertionError('Cancelled stream consumed further bytes')
    result.raw.chunks=incoming();seen=[]
    with pytest.raises(OperationCancelled):invoke(client,mode,seen.append)
    assert not seen and (result.raw.closed or result.raw.released) and not client._active_streams and oc._GENERATION_ACTIVE==0


def test_image_thinking_tools_and_visible_markup_are_preserved(client):
    rows=[{'message':{'thinking':'private','content':'<think>hidden</think>Hello '}},
          {'message':{'content':'there','tool_calls':[{'function':{'name':'noop','arguments':{}}}]}},
          {'done':True,'eval_count':2}]
    result,sent=setup_stream(client,'image',b'\n'.join(map(encoded,rows))+b'\n',5)
    seen=[];thinking=[]
    value=client.chat_stream([{'role':'user','images':['aW5lcnQ=']}],on_content=seen.append,on_thinking=thinking.append)
    assert ''.join(seen)=='Hello there' and thinking==['private']
    assert value['message']['content']=='Hello there' and value['message']['thinking']=='private'
    assert len(value['message']['tool_calls'])==1 and value['eval_count']==2

@pytest.mark.parametrize('mode',MODES)
def test_server_error_record_remains_an_error(client,mode):
    setup_stream(client,mode,encoded({'error':'inert server error'})+b'\n')
    with pytest.raises(oc.OllamaError,match='inert server error'):invoke(client,mode,lambda _:None)


def test_image_first_visible_budget_starts_after_generation_queue(client):
    result,sent=setup_stream(client,'image',encoded({'message':{'content':'ready'},'done':True})+b'\n')
    now=[0.0]
    with patch.object(oc.time,'monotonic',side_effect=lambda:now[0]),patch.object(oc,'_acquire_generation_slot',side_effect=lambda:now.__setitem__(0,10.0)),patch.object(oc,'_release_generation_slot'):
        value=client.chat_stream([{'role':'user','images':['aW5lcnQ=']}],first_visible_timeout_override=3)
    assert value['message']['content']=='ready' and len(sent)==1


def test_image_total_deadline_still_includes_generation_queue(client):
    result,sent=setup_stream(client,'image',encoded({'message':{'content':'ready'},'done':True})+b'\n')
    now=[0.0]
    with patch.object(oc.time,'monotonic',side_effect=lambda:now[0]),patch.object(oc,'_acquire_generation_slot',side_effect=lambda:now.__setitem__(0,10.0)),patch.object(oc,'_release_generation_slot'):
        with pytest.raises(oc.OllamaError,match='лимит ответа'):
            client.chat_stream([{'role':'user','images':['aW5lcnQ=']}],first_visible_timeout_override=3,total_timeout_override=5)
    assert result.raw.read==0


def test_image_first_visible_deadline_fires_during_newline_free_input(client):
    result,sent=setup_stream(client,'image',b'');now=[0.0]
    def incoming():
        now[0]=2.0;yield b'{"message":{"content":"'
        now[0]=4.0;yield b'unfinished'
        raise AssertionError('Read past first-visible deadline')
    result.raw.chunks=incoming()
    with patch.object(oc.time,'monotonic',side_effect=lambda:now[0]),patch.object(oc,'_acquire_generation_slot'),patch.object(oc,'_release_generation_slot'):
        with pytest.raises(oc.OllamaError,match='видимый ответ'):
            client.chat_stream([{'role':'user','images':['aW5lcnQ=']}],first_visible_timeout_override=3)
    assert result.raw.read==2 and (result.raw.closed or result.raw.released)


def test_legacy_invalid_unicode_is_a_clear_protocol_error(client):
    adapter=SimpleNamespace(iter_lines=lambda **kw:iter(['\ud800']))
    with pytest.raises(oc.OllamaError,match='invalid UTF-8/JSON'):
        list(client._stream_records(adapter,client._capture_request('chat')))
