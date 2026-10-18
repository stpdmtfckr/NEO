"""Fresh deterministic wire fixtures. No socket or OBS runtime is used."""
import base64
import hashlib
import json
import threading
import time
import pytest
from jarvis.integrations.obs_websocket184 import ObsClient,ObsError
from jarvis.testing.isolation import external_io_blocked


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    import os,socket
    import jarvis.testing.isolation as isolation
    for owner,name in ((os,"system"),(socket.socket,"bind"),(socket.socket,"listen"),(socket.socket,"sendto")):
        monkeypatch.setattr(owner,name,isolation._blocked)
    with external_io_blocked():
        yield


class Wire:
    def __init__(self,auth=True):
        data={"rpcVersion":1}
        if auth:data["authentication"]={"salt":"salt","challenge":"challenge"}
        self.queue=[{"op":0,"d":data}]
        self.sent=[];self.closed=False;self.fail=None;self.wrong_id=False
    def send(self,raw):
        message=json.loads(raw);self.sent.append(message)
        if message["op"]==1:
            self.queue.append({"op":2,"d":{"negotiatedRpcVersion":1}});return
        d=message["d"]
        if d["requestType"]==self.fail:return
        data={"availableRequests":["GetVersion","GetRecordStatus","StartRecord","SetSceneItemEnabled"]} if d["requestType"]=="GetVersion" else {"outputActive":False}
        self.queue.append({"op":7,"d":{"requestId":"wrong" if self.wrong_id else d["requestId"],"requestType":d["requestType"],
             "requestStatus":{"result":True,"code":100},"responseData":data}})
    def recv(self,timeout=None):
        if self.queue:return json.dumps(self.queue.pop(0))
        raise TimeoutError("untrusted provider text")
    def close(self):self.closed=True


def client(wire,**kwargs):
    return ObsClient(connector=lambda *a,**kw:wire,password_getter=lambda:"synthetic-only",**kwargs)


def test_challenge_and_response_contract():
    w=Wire();c=client(w);c.connect()
    secret=base64.b64encode(hashlib.sha256(b"synthetic-onlysalt").digest()).decode()
    auth=base64.b64encode(hashlib.sha256((secret+"challenge").encode()).digest()).decode()
    assert w.sent[0]=={"op":1,"d":{"rpcVersion":1,"authentication":auth,"eventSubscriptions":66}}
    assert c.request("GetRecordStatus")=={"outputActive":False}


@pytest.mark.parametrize('host',['0.0.0.0','192.168.1.1','example.com','127.0.0.1@evil','127.0.0.1/path'])
def test_nonloopback_rejected_before_connect(host):
    calls=[]
    with pytest.raises(ObsError,match="local_only"):
        ObsClient(host=host,connector=lambda *a:calls.append(a)).connect()
    assert not calls


def test_auth_disabled_or_missing_secret_fails_closed():
    w=Wire(False)
    with pytest.raises(ObsError,match="auth_required"):client(w).connect()
    assert w.closed and not w.sent
    w=Wire()
    with pytest.raises(ObsError,match="credential_missing"):
        ObsClient(connector=lambda *a:w).connect()
    assert w.closed and not w.sent


def test_capability_and_correlation_fail_closed():
    w=Wire();c=client(w);c.connect()
    with pytest.raises(ObsError,match="unsupported"):c.request("StartStream")
    w=Wire();w.wrong_id=True
    with pytest.raises(ObsError):client(w).connect()
    assert w.closed


def test_mutation_lost_ack_is_not_repeated():
    w=Wire();w.fail="StartRecord";c=client(w);c.connect()
    with pytest.raises(ObsError,match="uncertain") as e:c.request("StartRecord")
    assert "untrusted" not in str(e.value)
    assert len([x for x in w.sent if x["d"].get("requestType")=="StartRecord"])==1
    assert not c.connected


def test_server_comment_not_reflected():
    w=Wire();c=client(w);c.connect();send=w.send
    def reject(raw):
        send(raw);w.queue[-1]["d"]["requestStatus"]={"result":False,"code":500,"comment":"private secret"}
    w.send=reject
    with pytest.raises(ObsError) as e:c.request("StartRecord")
    assert e.value.key=="request_failed" and e.value.code==500 and "private" not in str(e.value)


def test_queued_collection_transition_blocks_reads_and_writes():
    w=Wire();c=client(w);c.connect();generation=c.collection_generation
    w.queue.append({"op":5,"d":{"eventType":"CurrentSceneCollectionChanging"}})
    for kind in ("GetRecordStatus","StartRecord"):
        with pytest.raises(ObsError,match="busy"):c.request(kind)
    assert not any(x["d"].get("requestType") in {"GetRecordStatus","StartRecord"} for x in w.sent)
    w.queue.append({"op":5,"d":{"eventType":"CurrentSceneCollectionChanged"}})
    with pytest.raises(ObsError,match="busy"):
        c.request("SetSceneItemEnabled",{"sceneItemId":7},expected_collection=generation)
    assert c.request("GetRecordStatus")=={"outputActive":False}


def test_blocked_send_deadline_aborts_and_reaps_watchdog():
    class Blocked(Wire):
        def __init__(self):super().__init__();self.aborted=threading.Event()
        def send(self,raw):
            if json.loads(raw)["d"].get("requestType")=="StartRecord":
                self.sent.append(json.loads(raw));assert self.aborted.wait(1);raise OSError("synthetic loss")
            super().send(raw)
        def abort(self):self.aborted.set()
    w=Blocked();c=client(w,timeout=.1);c.connect();started=time.monotonic()
    with pytest.raises(ObsError,match="uncertain"):c.request("StartRecord")
    assert time.monotonic()-started<.8 and w.aborted.is_set() and w.closed
    assert not any(t.name=="NEO-OBS-SendDeadline" for t in threading.enumerate())
