"""Fresh state/approval/ownership fixtures; all providers are synthetic."""
from pathlib import Path
import threading
import pytest
from jarvis.integrations.obs_service184 import ObsService
from jarvis.integrations.obs_websocket184 import ObsError
from tests.test_neo184_obs_transport import no_external_io


class Config:
    def __init__(self):self.values={"streaming.enabled":True,"app.language":"en"}
    def get(self,key,default=None):return self.values.get(key,default)


class Peer:
    def __init__(self,folder):
        self.folder=folder;self.connected=False;self.collection_generation=0;self.calls=[]
        self.recording=self.paused=self.streaming=self.replay=False
        self.scene="Main";self.muted=False;self.volume=1.;self.visible=True;self.last="";self.write_clip=True
    def connect(self):self.connected=True;self.collection_generation+=1
    def close(self):self.connected=False;self.collection_generation+=1
    def request(self,name,data=None,*,expected_collection=None):
        if expected_collection is not None and expected_collection!=self.collection_generation:raise ObsError("busy")
        data=data or {};self.calls.append((name,data))
        if name=="GetRecordStatus":return {"outputActive":self.recording,"outputPaused":self.paused,"outputDuration":1500}
        if name=="GetStreamStatus":return {"outputActive":self.streaming,"outputReconnecting":False,"outputDuration":2000,"outputSkippedFrames":3,"outputTotalFrames":60}
        if name=="GetReplayBufferStatus":return {"outputActive":self.replay}
        if name=="GetSceneList":return {"currentProgramSceneName":self.scene,"scenes":[{"sceneName":x} for x in ("Main"," Studio ","<b>Literal</b>")]}
        if name=="GetInputList":return {"inputs":[{"inputName":"Mic"},{"inputName":"Music"}]}
        if name=="GetInputMute":return {"inputMuted":self.muted}
        if name=="GetInputVolume":return {"inputVolumeMul":self.volume}
        if name=="GetSceneItemList":return {"sceneItems":[{"sourceName":"Title","sceneItemId":7}]}
        if name=="GetSceneItemEnabled":return {"sceneItemEnabled":self.visible}
        if name=="GetLastReplayBufferReplay":return {"savedReplayPath":self.last}
        if name=="StartRecord":self.recording=True
        elif name=="PauseRecord":self.paused=True
        elif name=="ResumeRecord":self.paused=False
        elif name=="StopRecord":
            self.recording=self.paused=False;p=self.folder/'record.mkv';p.write_bytes(b'synthetic');return {"outputPath":str(p)}
        elif name=="StartStream":self.streaming=True
        elif name=="StopStream":self.streaming=False
        elif name=="StartReplayBuffer":self.replay=True
        elif name=="StopReplayBuffer":self.replay=False
        elif name=="SetCurrentProgramScene":self.scene=data["sceneName"]
        elif name=="SetInputMute":self.muted=data["inputMuted"]
        elif name=="SetInputVolume":self.volume=data["inputVolumeMul"]
        elif name=="SetSceneItemEnabled":self.visible=data["sceneItemEnabled"]
        elif name=="SaveReplayBuffer":
            self.last=str(self.folder/'clip.mkv')
            if self.write_clip:Path(self.last).write_bytes(b'synthetic')
        else:raise AssertionError(name)
        return {}


@pytest.fixture
def service(tmp_path):
    p=Peer(tmp_path);s=ObsService(Config(),client_factory=lambda **kw:p,file_timeout=.2)
    return s,p


def test_disabled_and_idle_create_no_provider_or_worker(service):
    s,p=service;s.config.values['streaming.enabled']=False
    assert s.execute({'action':'record_start'})['status']=='disabled' and not p.calls
    s.game_started((10,1.));assert s._game is None


def test_record_state_and_verified_stop_path(service):
    s,p=service
    for action in ('record_start','record_pause','record_resume','record_stop'):
        result=s.execute({'action':action});assert result['ok'],result
    assert Path(result['path']).is_file() and not p.recording
    assert s.execute({'action':'record_resume'})['status']=='request_failed'


def test_existing_output_is_idempotent_not_toggle(service):
    s,p=service;p.recording=True
    assert s.execute({'action':'record_start'})['status']=='already'
    assert not any(n=='StartRecord' for n,_ in p.calls)


def test_live_requires_external_identity_grant_not_tool_arguments(service):
    s,p=service
    assert s.execute({'action':'stream_start','confirmed_stream':True})['status']=='confirmation_required'
    assert not p.calls
    identity=s.confirmation_identity()
    assert s.execute({'action':'stream_start'},confirmed_stream=True,expected_identity=identity)['ok']
    assert p.streaming and s.execute({'action':'stream_stop'})['ok']


@pytest.mark.parametrize('change',['port','secret','disconnect','disabled','observed_close'])
def test_live_grant_bound_to_exact_connection(service,change):
    s,p=service;s.execute({'action':'connect'});identity=s.confirmation_identity()
    if change=='port':s.config.values['streaming.port']=4456
    elif change=='secret':s.set_session_password('synthetic')
    elif change=='disconnect':s.execute({'action':'disconnect'})
    elif change=='disabled':s.config.values['streaming.enabled']=False
    else:p.close()
    assert s.execute({'action':'stream_start'},confirmed_stream=True,expected_identity=identity)['status']=='confirmation_required'
    assert not any(n=='StartStream' for n,_ in p.calls)


def test_reconnect_during_preflight_does_not_inherit_live_grant(service):
    s,p=service;old=p.request;lost=[False]
    def read(name,*a,**kw):
        if name=='GetStreamStatus' and not lost[0]:lost[0]=True;raise ObsError('connection_failed')
        return old(name,*a,**kw)
    p.request=read
    assert s.execute({'action':'stream_start'},confirmed_stream=True,expected_identity=s.confirmation_identity())['status']=='confirmation_required'
    assert not p.streaming


def test_disconnect_between_identity_check_and_ensure_needs_confirmation(service):
    s,p=service;s.execute({'action':'connect'});identity=s.confirmation_identity();old=s._ensure;lost=[False]
    def ensure():
        if not lost[0]:lost[0]=True;p.close()
        old()
    s._ensure=ensure
    assert s.execute({'action':'stream_start'},confirmed_stream=True,expected_identity=identity)['status']=='confirmation_required'
    assert not p.streaming


def test_replay_new_path_and_unverified_file(service):
    s,p=service;p.replay=True
    result=s.execute({'action':'replay_save'});assert result['ok'] and Path(result['path']).is_file()
    p.write_clip=False
    result=s.execute({'action':'replay_save'});assert result['status']=='file_unverified' and 'path' not in result


def test_scene_source_audio_status_and_literal_names(service):
    s,p=service;s.config.values['streaming.microphone_input']='Mic'
    assert s.execute({'action':'scene_switch','scene':' Studio '})['ok'] and p.scene==' Studio '
    assert s.execute({'action':'input_mute'})['ok'] and p.muted
    assert s.execute({'action':'input_volume','input':'Music','volume':25})['ok'] and p.volume==.25
    assert s.execute({'action':'source_hide','source':'Title'})['ok'] and not p.visible
    state=s.status();assert state['connected'] and state['dropped_frames']==3 and state['microphone_muted'] is True


@pytest.mark.parametrize('value',[-1,101,True,float('nan'),float('inf'),'50'])
def test_bad_volume_never_writes(service,value):
    s,p=service;assert not s.execute({'action':'input_volume','input':'Mic','volume':value})['ok']
    assert not any(n=='SetInputVolume' for n,_ in p.calls)


def test_collection_change_prevents_stale_scene_item_id_write(service):
    s,p=service;old=p.request
    def read(name,*a,**kw):
        result=old(name,*a,**kw)
        if name=='GetSceneItemList':p.collection_generation+=2
        return result
    p.request=read
    result=s.execute({'action':'source_hide','source':'Title'})
    assert result['status']=='busy'
    # HIST-075: the terminal answer is a refusal, not the progress text «Проверка OBS выполняется…».
    from jarvis.integrations.obs_text184 import ROWS
    assert result['message'] not in ROWS['busy'] and result['message'] in ROWS['busy_terminal']
    assert not any(n=='SetSceneItemEnabled' for n,_ in p.calls)


def test_mutation_delivery_uncertain_is_not_repeated(service):
    s,p=service;old=p.request
    def request(name,*a,**kw):
        result=old(name,*a,**kw)
        if name=='StartRecord':raise ObsError('uncertain')
        return result
    p.request=request
    assert s.execute({'action':'record_start'})['status']=='uncertain'
    assert len([n for n,_ in p.calls if n=='StartRecord'])==1


def test_recovered_status_does_not_keep_a_previous_command_error(service):
    s,p=service;original=p.request
    def fail(name,*args,**kwargs):
        if name=='StartRecord':raise ObsError('request_failed',code=500)
        return original(name,*args,**kwargs)
    p.request=fail
    assert not s.execute({'action':'record_start'})['ok']
    assert s.status()['error']==''
    assert s.execute({'action':'status'})['ok']


@pytest.mark.parametrize('change',['port','secret','disabled'])
def test_read_failure_after_settings_change_cannot_reconnect_old_client(service,change):
    s,p=service;connect=p.connect;connects=[]
    def counted():connects.append(1);connect()
    p.connect=counted;original=p.request
    def failure(name,*args,**kwargs):
        if name=='GetRecordStatus':
            if change=='port':s.config.values['streaming.port']=4456
            elif change=='secret':s.set_session_password('new synthetic credential')
            else:s.config.values['streaming.enabled']=False
            raise ObsError('connection_failed')
        return original(name,*args,**kwargs)
    p.request=failure
    # HIST-037: a changed connection is reported as such; only switching off is «disabled».
    assert s.execute({'action':'record_start'})['status']==('disabled' if change=='disabled' else 'configuration_changed')
    assert len(connects)==1 and not any(n=='StartRecord' for n,_ in p.calls)


def test_old_client_password_getter_refuses_new_credentials(service):
    s,p=service;s.set_session_password('old synthetic credential')
    signature=s._connection_settings();getter=s._password_for(signature)
    assert getter()=='old synthetic credential'
    s.set_session_password('new synthetic credential')
    assert getter()==''


def test_password_apply_is_nonblocking_and_does_not_close_connection(service):
    s,p=service;s.execute({'action':'connect'});done=threading.Event()
    with s._lock:
        thread=threading.Thread(target=lambda:(s.set_session_password('synthetic'),done.set()));thread.start();assert done.wait(.5)
    thread.join(.5);assert p.connected


def test_game_end_cancels_pending_replay_mutation(service):
    s,p=service;s.config.values['streaming.auto_replay_on_game']=True
    entered,release=threading.Event(),threading.Event();old=p.request
    def read(name,*a,**kw):
        result=old(name,*a,**kw)
        if name=='GetReplayBufferStatus':entered.set();assert release.wait(1)
        return result
    p.request=read;s.game_started((10,1.));thread=s._game._thread
    try:assert entered.wait(1);s.game_ended()
    finally:release.set();thread.join(2)
    assert not thread.is_alive() and not any(n=='StartReplayBuffer' for n,_ in p.calls)


@pytest.mark.parametrize('change',['settings','secret'])
def test_game_settings_or_secret_change_cancels_replay_preflight(service,change):
    s,p=service;s.config.values['streaming.auto_replay_on_game']=True
    entered,release=threading.Event(),threading.Event();old=p.request
    def read(name,*args,**kwargs):
        result=old(name,*args,**kwargs)
        if name=='GetReplayBufferStatus':entered.set();assert release.wait(1)
        return result
    p.request=read;s.game_started((10,1.));thread=s._game._thread
    try:
        assert entered.wait(1)
        if change=='settings':s.config.values['streaming.auto_replay_on_game']=False
        else:s.set_session_password('changed synthetic credential')
    finally:release.set();thread.join(2)
    assert not thread.is_alive() and not any(n=='StartReplayBuffer' for n,_ in p.calls)


def test_latest_game_replaces_blocked_preflight_with_one_replay_start(service):
    s,p=service;s.config.values['streaming.auto_replay_on_game']=True
    entered,release=threading.Event(),threading.Event();old=p.request;first=[True]
    def read(name,*args,**kwargs):
        result=old(name,*args,**kwargs)
        if name=='GetReplayBufferStatus' and first[0]:first[0]=False;entered.set();assert release.wait(1)
        return result
    p.request=read;s.game_started((10,1.));thread=s._game._thread
    try:assert entered.wait(1);s.game_started((11,2.))
    finally:release.set();thread.join(2)
    assert not thread.is_alive() and len([n for n,_ in p.calls if n=='StartReplayBuffer'])==1
