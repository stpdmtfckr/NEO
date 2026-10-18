"""R006 download entrypoint regressions using requests.Response byte streaming."""
from types import SimpleNamespace
from unittest.mock import patch
import json
import pytest
import requests
import jarvis.model_management as mm
from jarvis.model_management import ModelOperation
from jarvis.testing.isolation import external_io_blocked

MODEL='inert:1b';URL='http://127.0.0.1:11434'
class Raw:
    def __init__(self,chunks,on_chunk=None):
        self.chunks=chunks;self.on_chunk=on_chunk;self.count=0;self.byte_count=0
        self.args=[];self.closed=False;self.released=False
    def stream(self,amt,decode_content=True):
        self.args.append((amt,decode_content))
        for chunk in self.chunks:
            self.count+=1
            if isinstance(chunk,bytes):self.byte_count+=len(chunk)
            if self.on_chunk:self.on_chunk(self.count,chunk)
            yield chunk
    def close(self):self.closed=True
    def release_conn(self):self.released=True


def stream_response(chunks,on_chunk=None):
    r=requests.Response();r.status_code=200;r.raw=Raw(chunks,on_chunk)
    r.iter_lines=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('Production download must not call iter_lines'))
    return r

def json_response(data):
    r=requests.Response();r.status_code=200;r._content=json.dumps(data).encode();r._content_consumed=True
    return r

def wire(data):return json.dumps(data,ensure_ascii=False,separators=(',',':')).encode('utf-8')
def split(data,width):return [data[i:i+width] for i in range(0,len(data),width)]

class Session:
    def __init__(self,response,before=(),after=(MODEL,)):
        self.response=response;self.before=list(before);self.after=list(after);self.calls=[];self.closed=False
    def get(self,url,**kw):
        self.calls.append(('get',url,kw))
        names=self.after if any(x[0]=='post' for x in self.calls) else self.before
        return json_response({'models':[{'name':name} for name in names]})
    def post(self,url,**kw):self.calls.append(('post',url,kw));return self.response
    def close(self):self.closed=True

@pytest.fixture(autouse=True)
def inert():
    with external_io_blocked():yield

@pytest.fixture
def op():return ModelOperation('download',MODEL,URL)

def direct_pull(op,response):
    session=Session(response);op._session=session;op._pull();return session

def run(op,response,before=(),after=(MODEL,)):
    result=[];errors=[];progress=[];finished=[]
    op.completed.connect(result.append);op.failed.connect(errors.append)
    op.progress.connect(lambda text,pct:progress.append((text,pct)))
    op.finished.connect(lambda:finished.append(True))
    session=Session(response,before,after)
    with patch.object(mm.requests,'Session',return_value=session):op._run()
    return result,errors,progress,finished,session

@pytest.mark.parametrize('mode',('cancel','deadline','oversize'))
def test_exact_original_400_chunk_path_checks_before_newline(op,mode):
    now=[100.0]
    def arrived(index,_chunk):
        if index==1:
            if mode=='cancel':op.cancel()
            elif mode=='deadline':now[0]=2000.0
    r=stream_response([b'x'*1024]*400,arrived)
    error=InterruptedError if mode=='cancel' else TimeoutError if mode=='deadline' else ValueError
    with patch.object(mm.time,'monotonic',side_effect=lambda:now[0]):
        with pytest.raises(error):direct_pull(op,r)
    expected=65 if mode=='oversize' else 1
    assert r.raw.count==expected and r.raw.byte_count==expected*1024
    assert r.raw.args==[(1,True)] and (r.raw.closed or r.raw.released)

@pytest.mark.parametrize('mode',('cancel','deadline'))
def test_one_byte_drip_and_empty_chunks_check_control_state(op,mode):
    now=[100.0]
    def arrived(index,_chunk):
        if index==3:
            if mode=='cancel':op.cancel()
            else:now[0]=2000.0
    r=stream_response([b'{',b'',b'"',b'x']*100,arrived)
    with patch.object(mm.time,'monotonic',side_effect=lambda:now[0]):
        with pytest.raises(InterruptedError if mode=='cancel' else TimeoutError):direct_pull(op,r)
    assert r.raw.count==3 and r.raw.byte_count==2


def test_one_byte_no_newline_growth_stops_at_limit_plus_one(op):
    r=stream_response((b'x' for _ in range(100000)))
    with pytest.raises(ValueError,match='длинную строку'):direct_pull(op,r)
    assert r.raw.count==65537 and r.raw.byte_count==65537

@pytest.mark.parametrize('width',(1,2,3,7,64,256,8192))
@pytest.mark.parametrize('ending',(b'\n',b'\r\n',b''))
def test_utf8_crlf_and_complete_final_record_preserve_progress(op,width,ending):
    rows=[{'status':'загрузка 😀 你好 café','total':100,'completed':25},{'status':'success'}]
    payload=b' \t\r\n'+wire(rows[0])+b'\r\n'+wire(rows[1])+ending
    r=stream_response(split(payload,width));progress=[]
    op.progress.connect(lambda text,pct:progress.append((text,pct)))
    session=direct_pull(op,r)
    assert any('загрузка 😀 你好 café (25%)' in text and pct==25 for text,pct in progress)
    assert progress[-1]==(MODEL+': success',-1)
    assert session.calls[0][1]==URL+'/api/pull'
    assert session.calls[0][2]['json']=={'model':MODEL,'stream':True}
    assert session.calls[0][2]['timeout']==(2,5) and session.calls[0][2]['allow_redirects'] is False


def test_utf8_bom_accepted_like_original_byte_json_loader(op):
    r=stream_response(split(b'\xef\xbb\xbf'+wire({'status':'success'}),1))
    direct_pull(op,r)

@pytest.mark.parametrize('ending',(b'\n',b'\r\n',b'\r',b''))
def test_exact_native_cap_excludes_record_terminator(op,ending):
    empty=wire({'status':'success','padding':''})
    value={'status':'success','padding':'x'*(65536-len(empty))}
    payload=wire(value);assert len(payload)==65536
    direct_pull(op,stream_response(split(payload+ending,257)))
    value['padding']+='x'
    with pytest.raises(ValueError,match='длинную строку'):
        direct_pull(op,stream_response(split(wire(value)+ending,257)))


def test_giant_single_chunk_is_rejected_without_reading_further(op):
    r=stream_response([b'x'*1000000,b'never read'])
    with pytest.raises(ValueError,match='длинную строку'):direct_pull(op,r)
    assert r.raw.count==1


def test_size_limit_counts_utf8_bytes(op,monkeypatch):
    payload=wire({'status':'😀'*8})
    monkeypatch.setattr(mm,'_MAX_PULL_RECORD_BYTES',len(payload)-1)
    with pytest.raises(ValueError,match='длинную строку'):direct_pull(op,stream_response(split(payload,1)))

@pytest.mark.parametrize('payload',[
    b'{bad}\n',b'{',b'{"status":"unfinished',b'{"status":"\xf0\x9f',b'{"status":"\xff"}\n',
    b'[]\n',b'null\n',b'1\n',b'"text"\n',b'{"total":NaN}\n',b'{"total":Infinity}\n',b'\v\n',b'\f\n',
])
def test_malformed_and_incomplete_records_fail_clearly(op,payload):
    r=stream_response(split(payload,1))
    with pytest.raises(ValueError,match='Неверный ответ загрузки Ollama'):direct_pull(op,r)
    assert r.raw.closed or r.raw.released


def test_partial_later_record_does_not_reuse_previous_progress(op):
    r=stream_response([wire({'status':'pulling','total':100,'completed':25})+b'\n{'])
    progress=[];op.progress.connect(lambda text,pct:progress.append((text,pct)))
    with pytest.raises(ValueError):direct_pull(op,r)
    assert len(progress)==1 and progress[0][1]==25


def test_cancel_from_progress_stops_before_next_record_in_same_chunk(op):
    first=wire({'status':'pulling','total':100,'completed':25})
    r=stream_response([first+b'\n'+wire({'status':'success'})+b'\n',b'never read'])
    progress=[]
    def cancel(text,pct):progress.append((text,pct));op.cancel()
    op.progress.connect(cancel)
    with pytest.raises(InterruptedError):direct_pull(op,r)
    assert len(progress)==1 and progress[0][1]==25 and r.raw.count==1


def test_absolute_deadline_also_applies_between_records(op):
    now=[100.0]
    r=stream_response([wire({'status':'pulling','total':100,'completed':25})+b'\n'+wire({'status':'success'})+b'\n'])
    progress=[]
    def deadline(text,pct):progress.append((text,pct));now[0]=2000.0
    op.progress.connect(deadline)
    with patch.object(mm.time,'monotonic',side_effect=lambda:now[0]):
        with pytest.raises(TimeoutError):direct_pull(op,r)
    assert len(progress)==1

@pytest.mark.parametrize('mode',('cancel','deadline'))
def test_control_state_is_checked_when_transport_ends_or_raises(op,mode):
    now=[100.0]
    def incoming():
        yield b' '
        if mode=='cancel':op.cancel();raise requests.ConnectionError('read interrupted')
        now[0]=2000.0
    r=stream_response(incoming())
    with patch.object(mm.time,'monotonic',side_effect=lambda:now[0]):
        with pytest.raises(InterruptedError if mode=='cancel' else TimeoutError):direct_pull(op,r)
    assert r.raw.count==1


def test_progress_throttling_duplicate_suppression_and_success_are_preserved(op):
    rows=[{'status':'pulling','total':100,'completed':1},{'status':'pulling','total':100,'completed':2},
          {'status':'pulling','total':100,'completed':3},{'status':'pulling','total':100,'completed':3},{'status':'success'}]
    times=[100.0,100.1,100.21,100.22,100.23];now=[100.0]
    r=stream_response([wire(row)+b'\n' for row in rows],lambda index,_:now.__setitem__(0,times[index-1]))
    seen=[];op.progress.connect(lambda text,pct:seen.append((text,pct)))
    with patch.object(mm.time,'monotonic',side_effect=lambda:now[0]):direct_pull(op,r)
    assert [pct for _,pct in seen]==[1,3,-1] and seen[-1][0].endswith(': success')


def test_long_sequence_of_records_does_not_accumulate_record_bytes(op):
    rows=[wire({'status':'pulling','total':1000,'completed':i})+b'\n' for i in range(2000)]
    rows.append(wire({'status':'success'})+b'\n')
    direct_pull(op,stream_response(rows))


def test_success_is_verified_against_installed_exact_model(op):
    r=stream_response(split(wire({'status':'success'}),1))
    result,errors,progress,finished,s=run(op,r)
    assert not errors and result[0]['status']=='installed' and result[0]['model']==MODEL
    assert [row[0] for row in s.calls]==['get','post','get'] and s.closed and finished==[True]
    assert all(row[1].startswith(URL) for row in s.calls)
    assert op._session is None


def test_success_text_without_installed_model_still_fails(op):
    result,errors,_,finished,s=run(op,stream_response([wire({'status':'success'})]),after=())
    assert not result and errors and finished==[True] and s.closed

@pytest.mark.parametrize('mode',('cancel','deadline','oversize','malformed'))
def test_failures_never_verify_or_claim_installation(op,mode):
    now=[100.0]
    def event(index,_):
        if index==1:
            if mode=='cancel':op.cancel()
            elif mode=='deadline':now[0]=2000.0
    data=[b'x'*1024]*400 if mode!='malformed' else [b'{bad}\n']
    r=stream_response(data,event)
    with patch.object(mm.time,'monotonic',side_effect=lambda:now[0]):result,errors,_,finished,s=run(op,r)
    if mode=='cancel':assert result[0]['status']=='cancelled' and not errors
    else:assert not result and errors
    assert [row[0] for row in s.calls]==['get','post']
    assert finished==[True] and s.closed and (r.raw.closed or r.raw.released)


def test_cancellation_of_last_success_record_does_not_report_installation(op):
    r=stream_response([wire({'status':'success'})+b'\n'])
    op.progress.connect(lambda text,pct:op.cancel() if text.endswith(': success') else None)
    result,errors,_,_,s=run(op,r)
    assert result[0]['status']=='cancelled' and not errors
    assert [row[0] for row in s.calls]==['get','post']


def test_record_only_adapters_keep_compatibility_but_requests_cannot_fallback(op):
    class Adapter:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def raise_for_status(self):pass
        def iter_lines(self,**kw):yield wire({'status':'success'})
    direct_pull(op,Adapter())
    r=stream_response([b'{}\n']);r.iter_content=None
    with pytest.raises(ValueError,match='Неверный ответ'):direct_pull(op,r)


def test_operation_target_and_redirect_policy_remain_immutable(op):
    with pytest.raises(AttributeError):op.base_url='http://127.0.0.1:22434'
    with pytest.raises(AttributeError):op.model='another:7b'
    r=stream_response([wire({'status':'success'})]);result,errors,_,_,s=run(op,r)
    assert result[0]['model']==MODEL and not errors
    assert all(row[1].startswith(URL) for row in s.calls)
    assert all(row[2]['allow_redirects'] is False for row in s.calls)

@pytest.mark.parametrize('mode',('cancel','deadline'))
def test_control_change_during_record_decode_prevents_progress(op,mode):
    now=[100.0];original=json.loads;seen=[]
    op.progress.connect(lambda text,pct:seen.append((text,pct)))
    def decode(*args,**kwargs):
        value=original(*args,**kwargs)
        if mode=='cancel':op.cancel()
        else:now[0]=2000.0
        return value
    r=stream_response([wire({'status':'success'})])
    with patch.object(mm.time,'monotonic',side_effect=lambda:now[0]),patch.object(mm.json,'loads',side_effect=decode):
        with pytest.raises(InterruptedError if mode=='cancel' else TimeoutError):direct_pull(op,r)
    assert not seen
