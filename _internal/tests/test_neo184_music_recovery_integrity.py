"""Source, routing and cancellation regressions; all external edges are inert."""
import os, socket, threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from jarvis.testing.isolation import external_io_blocked
from jarvis.integrations import yandex_music as ym
from jarvis.runtime import media_activity as ma

class Config:
    def __init__(self,root):
        self.paths=SimpleNamespace(root=root)
        self.values={'integrations.yandex_music.enabled':True}
    def get(self,key,default=None): return self.values.get(key,default)

class FakePlayer:
    """Records transport only; never decodes or plays a byte."""
    def __init__(self):
        self.state='playing';self.title='Current';self.url='inert:current'
        self.starts=[];self.stops=0;self.closes=0;self.resumes=0
        self.resume_verified=True
    def status_if_running(self):
        return dict(state=self.state,title=self.title,url=self.url,position_ms=1000,duration_ms=180000)
    def set_volume(self,value): return {'verified':True}
    def play(self,url,**kw):
        kw.get('before_start',lambda:None)()
        self.starts.append(kw.get('title'));self.title=kw.get('title');self.url=url;self.state='playing'
        return dict(self.status_if_running(),verified=True)
    def stop(self):
        self.stops+=1;self.state='stopped'
        return dict(self.status_if_running(),verified=True)
    def pause(self):
        self.state='paused';return dict(self.status_if_running(),verified=True)
    def close(self):
        self.closes+=1;self.state='stopped';self.url=''
    def resume(self):
        self.resumes+=1
        if self.resume_verified:self.state='playing'
        return dict(self.status_if_running(),verified=self.resume_verified)

@pytest.fixture(autouse=True)
def io_guard(monkeypatch):
    with external_io_blocked(),patch.object(os,'system',side_effect=PermissionError('audit blocks shell')),patch.object(socket.socket,'sendto',side_effect=PermissionError('audit blocks UDP')):
        monkeypatch.setattr(ma,'_STATE',{'owner':'','state':'','updated':0.0})
        monkeypatch.setattr(ma,'_OWNERS',{})
        yield

@pytest.fixture
def rig(tmp_path,monkeypatch):
    monkeypatch.setattr(ym.YandexMusicAdapter,'clear_transient_cache_on_startup_once',lambda *a:{})
    player=FakePlayer()
    monkeypatch.setattr(ym,'local_player',lambda:player)
    def make(name='a'):
        obj=ym.YandexMusicAdapter(Config(tmp_path/name))
        # Only ancillary effects are replaced; transport, source preparation,
        # validation, metadata commitment and cancellation are production code.
        monkeypatch.setattr(obj,'_after_playback_effect',lambda *a,**kw:None)
        return obj
    obj=make()
    audio=tmp_path/'inert-audio';audio.write_bytes(b'INERT; NEVER PLAYED')
    def entry(name,cached=True):
        row={'id':'youtube:'+name,'track_id':'youtube:'+name,'title':name,'artist':'Synthetic',
             '_source_type':'youtube','_source_url':'https://www.youtube.com/watch?v='+name}
        if cached:row['_youtube_cached_path']=str(audio)
        return row
    obj._current_track_obj=entry('Current');obj._current_track_meta={'id':'youtube:Current','title':'Current'}
    obj._station='mixed:youtube';obj._station_queue=[entry('Next')]
    return obj,player,entry,make,audio

def test_failed_next_download_preserves_still_playing_old_track(rig,monkeypatch):
    obj,p,entry,_,_=rig
    obj._station_queue=[entry('Broken',cached=False)]
    def fail(_):raise RuntimeError('synthetic unsupported media, before player.play')
    monkeypatch.setattr(obj._youtube,'download_audio',fail)
    with pytest.raises(RuntimeError,match='Не удалось запустить следующий'):obj.transport('next')
    assert p.starts==[] and p.closes==0 and p.state=='playing'
    assert obj._current_track_obj['title']=='Current' and obj._station_queue[0]['title']=='Broken'

@pytest.mark.parametrize('action',['play','next'])
def test_stopped_youtube_transport_works_with_yandex_disabled(rig,action):
    obj,p,*_=rig
    obj.config.values['integrations.yandex_music.enabled']=False
    p.state='stopped'
    assert obj.local_youtube_transport_allowed(action)
    assert obj.transport(action)['verified']
    assert p.state=='playing'
    assert p.starts==(['Next'] if action=='next' else [])
    assert p.resumes==(1 if action=='play' else 0)

@pytest.mark.parametrize('state',['playing','paused','buffering','starting'])
def test_protection_active_youtube_allowed_with_yandex_disabled(rig,state):
    obj,p,*_=rig
    obj.config.values['integrations.yandex_music.enabled']=False;p.state=state
    assert obj.local_youtube_transport_allowed('play') and obj.local_youtube_transport_allowed('next')

def test_second_next_coalesces_without_cancelling_preparation(rig,monkeypatch):
    obj,p,entry,_,audio=rig
    obj._station_queue=[entry('Next',cached=False)]
    entered=threading.Event();release=threading.Event();results={}
    def download(_):
        entered.set();assert release.wait(3)
        return {'local_path':str(audio),'title':'Next'}
    monkeypatch.setattr(obj._youtube,'download_audio',download)
    first=threading.Thread(target=lambda:results.update(first=obj.transport('next')))
    first.start();assert entered.wait(3)
    try:results['second']=obj.transport('next')
    finally:release.set();first.join(3)
    assert not first.is_alive()
    assert results['first']['verified'] and results['second']['coalesced']
    assert p.starts==['Next'] and obj._station_queue==[]

def test_protection_stop_during_already_started_preparation_cancels(rig,monkeypatch):
    obj,p,entry,_,audio=rig
    obj._station_queue=[entry('Next',cached=False)]
    entered=threading.Event();release=threading.Event();results={}
    def download(_):
        entered.set();assert release.wait(3)
        return {'local_path':str(audio),'title':'Next'}
    monkeypatch.setattr(obj._youtube,'download_audio',download)
    first=threading.Thread(target=lambda:results.update(first=obj.transport('next')))
    first.start();assert entered.wait(3)
    try:obj.transport('stop')
    finally:release.set();first.join(3)
    assert results['first']['cancelled'] and p.starts==[] and p.state=='stopped'

def test_delayed_previous_cannot_restart_after_stop(rig,monkeypatch):
    obj,p,entry,_,_=rig
    obj._history=[entry('Previous')]
    entered=threading.Event();release=threading.Event();results={}
    original=obj._play_track_object
    def scheduled_call(*a,**kw):
        # Barrier models a scheduler pause between previous's snapshot and the
        # production call that captures serial. No arguments/state are changed.
        entered.set();assert release.wait(3)
        return original(*a,**kw)
    monkeypatch.setattr(obj,'_play_track_object',scheduled_call)
    first=threading.Thread(target=lambda:results.update(previous=obj.transport('previous')))
    first.start();assert entered.wait(3)
    try:
        obj.transport('stop');assert p.state=='stopped'
    finally:release.set();first.join(3)
    assert not first.is_alive()
    assert results['previous']['cancelled'] and p.starts==[] and p.state=='stopped'
    assert obj._history[-1]['title']=='Previous'

def test_generic_continue_after_youtube_stop_keeps_youtube(rig):
    from jarvis.tools.domains.browser_media import register_browser_media_tools
    obj,p,_,_,_=rig
    obj.config.values.update({'integrations.yandex_music.enabled':False,
        'integrations.spotify.enabled':True,'integrations.music.active_provider':'spotify'})
    registered={};spotify_calls=[]
    spotify=SimpleNamespace(execute=lambda action,**kw:spotify_calls.append(action) or {'verified':True,'state':'playing'})
    registry=SimpleNamespace(register=lambda definition:registered.setdefault(definition.name,definition))
    register_browser_media_tools(registry,config=obj.config,yandex_adapter=obj,spotify_adapter=spotify,youtube_adapter=SimpleNamespace(),discord_adapter=SimpleNamespace())
    ma.note_tool_execution('music',{'action':'play'},True,{'provider':'yandex_music','content_provider':'youtube'})
    stop=registered['music'].handler({'action':'stop'})
    assert stop.success and stop.data['provider']=='yandex_music' and p.state=='stopped'
    # Mirrors registry.py:347 after a successful handler.
    ma.note_tool_execution('music',{'action':'stop'},stop.success,stop.data)
    assert ma.recent_music_owner()=='yandex_music'
    resume=registered['music'].handler({'action':'play'})
    assert resume.success and resume.data['provider']=='yandex_music' and spotify_calls==[]
    assert p.resumes==1 and p.state=='playing'

def test_stale_adapter_rebuilds_current_shared_player_source(rig):
    obj,p,entry,make,_=rig
    newer=make('a') # Two adapters under the same profile, one singleton player.
    obj._play_track_object(entry('OldOwnerTrack'))
    newer._play_track_object(entry('NewestTrack'))
    newer.transport('stop')
    p.resume_verified=False # Model stopped/invalid worker requiring normal recovery.
    result=obj.transport('play')
    assert result['verified'] and p.starts==['OldOwnerTrack','NewestTrack','NewestTrack']
    assert p.title=='NewestTrack' and newer._current_track_obj['title']=='NewestTrack'


def test_agent_literal_continue_after_youtube_stop_keeps_youtube(rig,tmp_path,monkeypatch):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.tools.domains.browser_media import register_browser_media_tools
    from jarvis.tools.registry import ToolExecution
    obj,p,*_=rig
    session=AtlasSession(tmp_path/'agent_case')
    try:
        session.reset_case('ru')
        for definition in session.agent.tools._tools.values():
            definition.handler=lambda args:ToolExecution(True,'Inert unrelated tool',{})
        session.config.data['integrations']['yandex_music']['enabled']=False
        session.config.data['integrations']['spotify']['enabled']=True
        session.config.data['integrations'].setdefault('music',{})['active_provider']='spotify'
        obj.config=session.config
        spotify_calls=[]
        spotify=SimpleNamespace(execute=lambda action,**kw:spotify_calls.append(action) or {'verified':True,'state':'playing'})
        register_browser_media_tools(SimpleNamespace(register=lambda d:session.agent.tools._tools.__setitem__(d.name,d)),config=session.config,yandex_adapter=obj,
            spotify_adapter=spotify,youtube_adapter=SimpleNamespace(),discord_adapter=SimpleNamespace())
        # Permit only the two exact media commands against this inert adapter.
        # Every unrelated action remains blocked by AtlasSession's guard.
        session._permitted=[('music',{'action':'stop'}),('music',{'action':'play'})]
        ma.note_tool_execution('music',{'action':'play'},True,{'provider':'yandex_music','content_provider':'youtube'})
        stopped=session.agent.handle('останови музыку')
        assert stopped.success and p.state=='stopped'
        resumed=session.agent.handle('продолжи')
        assert resumed.success and spotify_calls==[]
        assert p.resumes==1 and p.state=='playing'
    finally:
        session.finish()


def test_protection_network_failure_during_next_preserves_old_player(rig,monkeypatch):
    obj,p,entry,_,_=rig
    obj._station_queue=[entry('Broken',cached=False)]
    def fail(_):raise ConnectionError('synthetic network failure')
    monkeypatch.setattr(obj._youtube,'download_audio',fail)
    with pytest.raises(ConnectionError):obj.transport('next')
    assert p.starts==[] and p.closes==0 and p.state=='playing'
    assert obj._current_track_obj['title']=='Current'


def run_in_thread(call):
    result={}
    def run():
        try:result['value']=call()
        except BaseException as exc:result['error']=exc
    thread=threading.Thread(target=run);thread.start()
    return thread,result


def join_thread(thread,result):
    thread.join(3)
    assert not thread.is_alive(), 'transport must not leak its transition lane'
    return result


@pytest.mark.parametrize('duplicates',[2,3,10])
def test_repeated_next_keeps_one_preparation_and_one_queue_pop(rig,monkeypatch,duplicates):
    obj,p,entry,_,audio=rig
    obj._station_queue=[entry('Next',False),entry('AfterNext')]
    entered=threading.Event();release=threading.Event()
    def download(_):
        entered.set();assert release.wait(3)
        return {'local_path':str(audio),'title':'Next'}
    monkeypatch.setattr(obj._youtube,'download_audio',download)
    thread,result=run_in_thread(lambda:obj.transport('next'))
    assert entered.wait(3)
    try:
        for _ in range(duplicates):assert obj.transport('next')['coalesced']
    finally:release.set();join_thread(thread,result)
    assert result['value']['verified'] and p.starts==['Next']
    assert [x['title'] for x in obj._station_queue]==['AfterNext']


@pytest.mark.parametrize('action,method',[('next','_next_background'),('previous','_previous_background')])
def test_stop_wins_before_delayed_transport_helper_starts(rig,monkeypatch,action,method):
    obj,p,entry,*_=rig;obj._history=[entry('Previous')]
    original=getattr(obj,method);entered=threading.Event();release=threading.Event()
    def delayed(*args,**kwargs):
        entered.set();assert release.wait(3)
        return original(*args,**kwargs)
    monkeypatch.setattr(obj,method,delayed)
    thread,result=run_in_thread(lambda:obj.transport(action));assert entered.wait(3)
    try:assert obj.transport('stop')['verified']
    finally:release.set();join_thread(thread,result)
    assert result['value']['cancelled'] and p.state=='stopped' and not p.starts
    assert not obj._advance_lock.locked()


def test_newer_adapter_source_invalidates_older_pending_download(rig,monkeypatch):
    obj,p,entry,make,audio=rig;newer=make('a')
    entered=threading.Event();release=threading.Event()
    def download(_):
        entered.set();assert release.wait(3)
        return {'local_path':str(audio),'title':'Obsolete'}
    monkeypatch.setattr(obj._youtube,'download_audio',download)
    thread,result=run_in_thread(lambda:obj._play_track_object(entry('Obsolete',False)))
    assert entered.wait(3)
    try:assert newer._play_track_object(entry('Newest'))['verified']
    finally:release.set();join_thread(thread,result)
    assert isinstance(result.get('error'),ym.PlaybackSuperseded)
    assert p.starts==['Newest'] and p.title=='Newest'


def test_failed_new_source_keeps_old_ownership_and_clears_pending(rig,monkeypatch):
    from jarvis.integrations.media_state import local_playback_session
    obj,p,entry,make,_=rig;other=make('a')
    obj._play_track_object(entry('Owner'))
    def fail(_):raise RuntimeError('inert prepare failure')
    monkeypatch.setattr(other._youtube,'download_audio',fail)
    with pytest.raises(RuntimeError):other._play_track_object(entry('Broken',False))
    session=local_playback_session(p)
    assert session.owner() is obj and session.preparing_owner() is None
    p.resume_verified=False
    assert other.transport('play')['verified']
    assert p.starts==['Owner','Owner']


@pytest.mark.parametrize('same_adapter',[False,True])
def test_old_auto_advance_cannot_cancel_explicit_source_preparation(rig,monkeypatch,same_adapter):
    obj,p,entry,make,audio=rig
    obj._play_track_object(entry('Old'),station='mixed:youtube',queue=[entry('UnwantedAuto')])
    other=obj if same_adapter else make('a')
    entered=threading.Event();release=threading.Event()
    def expand(_url,**kwargs):
        entered.set();assert release.wait(3)
        return {'items':[{'id':'NewSource','title':'NewSource','webpage_url':'https://www.youtube.com/watch?v=NewSource'}]}
    monkeypatch.setattr(other._youtube,'expand',expand)
    monkeypatch.setattr(other._youtube,'download_audio',lambda _:{'local_path':str(audio),'title':'NewSource'})
    thread,result=run_in_thread(lambda:other.play_youtube_url('https://www.youtube.com/watch?v=NewSource'))
    assert entered.wait(3)
    try:assert obj._next_background(auto=True)['cancelled']
    finally:release.set();join_thread(thread,result)
    assert result['value']['verified'] and p.starts==['Old','NewSource']


def test_stop_during_url_expansion_cancels_before_download(rig,monkeypatch):
    obj,p,*_=rig
    entered=threading.Event();release=threading.Event();downloads=[]
    def expand(_url,**kwargs):
        entered.set();assert release.wait(3)
        return {'items':[{'id':'TooLate','title':'TooLate','webpage_url':'https://www.youtube.com/watch?v=TooLate'}]}
    monkeypatch.setattr(obj._youtube,'expand',expand)
    monkeypatch.setattr(obj._youtube,'download_audio',lambda _:downloads.append('unexpected'))
    thread,result=run_in_thread(lambda:obj.play_youtube_url('https://www.youtube.com/watch?v=TooLate'))
    assert entered.wait(3)
    try:assert obj.transport('stop')['verified']
    finally:release.set();join_thread(thread,result)
    assert result['value']['cancelled'] and p.state=='stopped' and not p.starts and not downloads


def test_stop_cancels_first_pending_source_without_starting_player(rig,monkeypatch):
    obj,p,entry,_,audio=rig
    obj._current_track_obj=None;obj._current_track_meta={};p.url='';p.state='stopped'
    entered=threading.Event();release=threading.Event()
    def download(_):
        entered.set();assert release.wait(3)
        return {'local_path':str(audio),'title':'TooLate'}
    monkeypatch.setattr(obj._youtube,'download_audio',download)
    thread,result=run_in_thread(lambda:obj._play_track_object(entry('TooLate',False)))
    assert entered.wait(3)
    try:assert obj.transport('stop')['cancelled_preparation']
    finally:release.set();join_thread(thread,result)
    assert isinstance(result.get('error'),ym.PlaybackSuperseded)
    assert p.stops==0 and not p.starts and p.state=='stopped'


def test_stale_transport_snapshot_cannot_rebuild_older_owner(rig,monkeypatch):
    obj,p,entry,make,_=rig;newer=make('a')
    obj._play_track_object(entry('Old'))
    entered=threading.Event();release=threading.Event();original=p.status_if_running
    main_ident=threading.get_ident()
    def snapshot():
        if threading.get_ident()!=main_ident:
            entered.set();assert release.wait(3)
        return original()
    monkeypatch.setattr(p,'status_if_running',snapshot)
    p.resume_verified=False
    thread,result=run_in_thread(lambda:obj.transport('play'));assert entered.wait(3)
    try:newer._play_track_object(entry('New'))
    finally:release.set();join_thread(thread,result)
    assert result['value']['cancelled'] and p.starts==['Old','New'] and p.title=='New'


def test_failed_resume_rebuild_releases_pending_marker(rig,monkeypatch):
    from jarvis.integrations.media_state import local_playback_session
    obj,p,entry,*_=rig
    obj._current_track_obj=entry('Broken',False);p.resume_verified=False
    monkeypatch.setattr(obj._youtube,'download_audio',lambda _:(_ for _ in ()).throw(RuntimeError('inert failure')))
    with pytest.raises(RuntimeError):obj.transport('play')
    assert local_playback_session(p).preparing_owner() is None


@pytest.mark.parametrize('action',['play','next','previous','queue_status'])
def test_disabled_catalogue_never_authorizes_yandex_queue_items(rig,action):
    obj,p,entry,*_=rig;obj.config.values['integrations.yandex_music.enabled']=False
    non_youtube={'id':'saved:1','title':'Catalogue item','_source_type':'yandex_saved'}
    if action=='play':obj._current_track_obj=non_youtube
    elif action=='next':obj._station_queue=[non_youtube]
    elif action=='previous':obj._history=[non_youtube]
    else:obj._current_track_obj=non_youtube
    p.state='stopped'
    assert not obj.local_youtube_transport_allowed(action)


def make_provider_registry(obj,spotify_calls):
    from jarvis.tools.domains.browser_media import register_browser_media_tools
    obj.config.values.update({'integrations.yandex_music.enabled':False,
        'integrations.spotify.enabled':True,'integrations.music.active_provider':'spotify'})
    entries={};spotify=SimpleNamespace(execute=lambda action,**kw:spotify_calls.append(action) or {'verified':True,'state':'playing'})
    register_browser_media_tools(SimpleNamespace(register=lambda d:entries.setdefault(d.name,d)),config=obj.config,
        yandex_adapter=obj,spotify_adapter=spotify,youtube_adapter=SimpleNamespace(),discord_adapter=SimpleNamespace())
    return entries


@pytest.mark.parametrize('explicit',[False,True])
def test_newer_spotify_or_explicit_spotify_wins_over_stopped_youtube(rig,explicit):
    obj,p,*_=rig;p.state='stopped';calls=[];entries=make_provider_registry(obj,calls)
    ma.note_tool_execution('music',{'action':'stop'},True,{'provider':'yandex_music'})
    if not explicit:ma.note_tool_execution('spotify',{'action':'play'},True,{'provider':'spotify'})
    result=entries['spotify' if explicit else 'music'].handler({'action':'play'})
    assert result.success and result.data['provider']=='spotify' and calls==['play']
    assert not p.resumes


def test_passive_stopped_observation_keeps_explicit_resume_source(rig):
    obj,p,*_=rig
    ma.note_tool_execution('music',{'action':'stop'},True,{'provider':'yandex_music'})
    ma.note_media_observation('yandex_music',playing=False,paused=False)
    assert ma.recent_music_owner()=='yandex_music'


@pytest.mark.parametrize('action',['pause','resume','stop'])
def test_native_guarded_transport_rejects_before_any_ipc(monkeypatch,action):
    from jarvis.integrations.background.local_player import BackgroundLocalPlayer
    player=BackgroundLocalPlayer();sent=[]
    monkeypatch.setattr(player,'_ensure',lambda:None)
    monkeypatch.setattr(player,'status_if_running',lambda:{'state':'stopped'})
    monkeypatch.setattr(player,'_request_unlocked',lambda payload,**kw:sent.append(payload) or {})
    def cancelled():raise ym.PlaybackSuperseded('cancelled at native submission')
    with pytest.raises(ym.PlaybackSuperseded):player.guarded_transport(action,before_send=cancelled)
    assert sent==[]


def test_next_stop_next_waits_for_old_cleanup_then_starts_latest(rig,monkeypatch):
    obj,p,entry,_,audio=rig
    obj._station_queue=[entry('Next',False)]
    entered=threading.Event();release=threading.Event();count=[]
    def download(_):
        count.append(1)
        if len(count)==1:
            entered.set();assert release.wait(3)
        return {'local_path':str(audio),'title':'Next'}
    monkeypatch.setattr(obj._youtube,'download_audio',download)
    first,r1=run_in_thread(lambda:obj.transport('next'));assert entered.wait(3)
    obj.transport('stop')
    second,r2=run_in_thread(lambda:obj.transport('next'))
    try:
        # Wait until the latest command has its own admitted request token.
        import time
        deadline=time.monotonic()+2
        while obj._manual_transport_serial<3 and time.monotonic()<deadline:time.sleep(.002)
        assert obj._manual_transport_serial==3
    finally:release.set();join_thread(first,r1);join_thread(second,r2)
    assert r1['value']['cancelled'] and r2['value']['verified']
    assert p.starts==['Next'] and obj._station_queue==[]


def test_stop_cancels_next_waiter_behind_already_cancelled_preparation(rig,monkeypatch):
    obj,p,entry,_,audio=rig;obj._station_queue=[entry('Next',False)]
    entered=threading.Event();release=threading.Event()
    def download(_):
        entered.set();assert release.wait(3)
        return {'local_path':str(audio),'title':'Next'}
    monkeypatch.setattr(obj._youtube,'download_audio',download)
    first,r1=run_in_thread(lambda:obj.transport('next'));assert entered.wait(3)
    obj.transport('stop')
    second,r2=run_in_thread(lambda:obj.transport('next'))
    try:
        import time
        deadline=time.monotonic()+2
        while obj._manual_transport_serial<3 and time.monotonic()<deadline:time.sleep(.002)
        assert obj._manual_transport_serial==3
        obj.transport('stop')
    finally:release.set();join_thread(first,r1);join_thread(second,r2)
    assert r1['value']['cancelled'] and r2['value']['cancelled']
    assert not p.starts and p.state=='stopped' and not obj._advance_lock.locked()


def test_disabled_mixed_shuffle_rejects_catalogue_before_materialization(rig,monkeypatch):
    obj,p,entry,*_=rig;obj.config.values['integrations.yandex_music.enabled']=False
    forbidden={'id':'saved:1','title':'Forbidden','_source_type':'yandex_saved'}
    obj._station_queue=[entry('Allowed'),forbidden]
    monkeypatch.setattr(obj,'_pop_sequence_track',lambda queue:forbidden)
    monkeypatch.setattr(obj,'_materialize_track',lambda track:pytest.fail('disabled catalogue materialized'))
    with pytest.raises(RuntimeError,match='отключена'):obj.transport('next')
    assert not p.starts and p.closes==0 and p.state=='playing'


def test_unavailable_worker_still_allows_selected_youtube_recovery(rig,monkeypatch):
    obj,p,*_=rig;obj.config.values['integrations.yandex_music.enabled']=False
    monkeypatch.setattr(p,'status_if_running',lambda:None)
    assert obj.local_youtube_transport_allowed('play')
    assert obj.local_youtube_transport_allowed('next')


def test_cancellable_resolver_receives_guard_and_stop_prevents_start(rig,monkeypatch):
    from jarvis.integrations.media_state import MediaRequestSuperseded
    obj,p,entry,_,audio=rig;obj._station_queue=[entry('Next',False)]
    entered=threading.Event();release=threading.Event();cancelled=[]
    def download(_url,*,check_cancelled):
        entered.set();assert release.wait(3)
        try:check_cancelled()
        except MediaRequestSuperseded:
            cancelled.append(True);raise
        return {'local_path':str(audio),'title':'Next'}
    monkeypatch.setattr(obj._youtube,'download_audio',download)
    thread,result=run_in_thread(lambda:obj.transport('next'));assert entered.wait(3)
    try:obj.transport('stop')
    finally:release.set();join_thread(thread,result)
    assert cancelled==[True] and result['value']['cancelled'] and not p.starts


def test_stale_adapter_reads_current_owner_queue(rig):
    obj,p,entry,make,_=rig;newer=make('a')
    obj._play_track_object(entry('Old'),queue=[entry('OldNext')])
    newer._play_track_object(entry('CurrentOwner'),queue=[entry('CurrentNext')])
    actual=newer.queue_status()
    assert obj.queue_status()==actual
    assert actual['current']['title']=='CurrentOwner'
    assert actual['queue'][0]['title']=='CurrentNext'


def test_review_same_track_recovery_cannot_cancel_new_explicit_preparation(rig,monkeypatch):
    obj,p,entry,make,audio=rig;other=make('b')
    obj._play_track_object(entry('OwnerA'),station='mixed:youtube',queue=[entry('NextA')],auto_advance=True)
    entered=threading.Event();release=threading.Event()
    def expand(_url,**kwargs):
        entered.set();assert release.wait(3)
        return {'items':[{'id':'NewB','title':'NewB','webpage_url':'https://www.youtube.com/watch?v=NewB'}]}
    monkeypatch.setattr(other._youtube,'expand',expand)
    monkeypatch.setattr(other._youtube,'download_audio',lambda _:{'local_path':str(audio),'title':'NewB'})
    worker,result=run_in_thread(lambda:other.play_youtube_url('https://www.youtube.com/watch?v=NewB'))
    assert entered.wait(3)
    try:
        with pytest.raises(ym.PlaybackSuperseded):
            obj._play_track_object(obj._current_track_obj,station=obj._station,queue=obj._station_queue,
                auto_advance=True,remember_previous=False,expected_manual_serial=obj._manual_transport_serial)
    finally:release.set();join_thread(worker,result)
    assert result['value']['verified'] and p.starts==['OwnerA','NewB']


def test_review_late_empty_queue_next_cannot_stop_new_owner(rig,monkeypatch):
    obj,p,entry,make,_=rig;newer=make('b')
    obj._play_track_object(entry('OwnerA'),station='mixed:youtube',queue=[],auto_advance=True)
    entered=threading.Event();release=threading.Event();original=p.status_if_running
    main=threading.get_ident();reads=[]
    def status():
        if threading.get_ident()!=main:
            reads.append(1)
            if len(reads)==2:entered.set();assert release.wait(3)
        return original()
    monkeypatch.setattr(p,'status_if_running',status)
    worker,result=run_in_thread(lambda:obj.transport('next'));assert entered.wait(3)
    try:newer._play_track_object(entry('NewB'),station='mixed:youtube',queue=[entry('NextB')])
    finally:release.set();join_thread(worker,result)
    assert result['value']['cancelled'] and p.title=='NewB' and p.state=='playing' and p.stops==0


def test_review_collected_new_owner_does_not_reenable_stale_automatic_queue(rig,tmp_path):
    import gc,weakref
    from jarvis.integrations.media_state import local_playback_session
    obj,p,entry,*_=rig
    obj._play_track_object(entry('OwnerA'),station='mixed:youtube',queue=[entry('NextA')],auto_advance=True)
    newer=ym.YandexMusicAdapter(Config(tmp_path/'ephemeral'))
    newer._after_playback_effect=lambda *a,**kw:None
    newer._play_track_object(entry('NewB'),station='mixed:youtube',queue=[])
    ref=weakref.ref(newer);del newer;gc.collect()
    assert ref() is None and local_playback_session(p).owner() is None
    assert obj._next_background(auto=True)['cancelled']
    assert p.starts==['OwnerA','NewB'] and p.title=='NewB'


@pytest.mark.parametrize('action,expected_state',[('stop','stopped'),('pause','paused')])
def test_review_first_source_verification_interrupt_sends_native_control(rig,monkeypatch,action,expected_state):
    obj,p,entry,*_=rig
    obj._current_track_obj=None;obj._current_track_meta={};p.url='';p.state='stopped'
    entered=threading.Event();release=threading.Event();original=p.play
    def play(*args,**kwargs):
        result=original(*args,**kwargs)
        entered.set();assert release.wait(3)
        return result
    monkeypatch.setattr(p,'play',play)
    worker,result=run_in_thread(lambda:obj._play_track_object(entry('FirstSource')));assert entered.wait(3)
    try:
        stopped=obj.transport(action)
        assert stopped['verified'] and p.state==expected_state
    finally:release.set();join_thread(worker,result)
    assert isinstance(result.get('error'),ym.PlaybackSuperseded)
    assert p.state==expected_state
    if action=='stop':assert p.stops==1


def test_review_queue_from_stale_adapter_updates_current_owner(rig,monkeypatch):
    obj,p,entry,make,_=rig;newer=make('b')
    obj._play_track_object(entry('OwnerA'),station='mixed:youtube',queue=[entry('NextA')])
    newer._play_track_object(entry('NewB'),station='mixed:youtube',queue=[entry('NextB')])
    monkeypatch.setattr(newer._youtube,'expand',lambda _url,**kwargs:{'items':[{'id':'QueuedC','title':'QueuedC','webpage_url':'https://www.youtube.com/watch?v=QueuedC'}]})
    monkeypatch.setattr(newer,'_start_station_monitor',lambda:None)
    monkeypatch.setattr(newer,'_start_prefetch_window',lambda *a,**kw:None)
    result=obj.execute('youtube_url',query='https://www.youtube.com/watch?v=QueuedC',queue=True)
    assert result['verified'] and [x['title'] for x in newer._station_queue]==['NextB','QueuedC']
    assert [x['title'] for x in obj._station_queue]==['NextA']
    assert [x['title'] for x in obj.queue_status()['queue']]==['NextB','QueuedC']


def test_review_queue_expansion_revalidates_owner_before_mutation(rig,monkeypatch):
    obj,p,entry,make,_=rig;newer=make('b')
    obj._play_track_object(entry('OwnerA'),station='mixed:youtube',queue=[entry('NextA')])
    entered=threading.Event();release=threading.Event()
    def expand(_url,**kwargs):
        entered.set();assert release.wait(3)
        return {'items':[{'id':'QueuedC','title':'QueuedC','webpage_url':'https://www.youtube.com/watch?v=QueuedC'}]}
    monkeypatch.setattr(obj._youtube,'expand',expand)
    worker,result=run_in_thread(lambda:obj.execute('youtube_url',query='https://www.youtube.com/watch?v=QueuedC',queue=True))
    assert entered.wait(3)
    try:newer._play_track_object(entry('NewB'),station='mixed:youtube',queue=[entry('NextB')])
    finally:release.set();join_thread(worker,result)
    assert result['value']['cancelled']
    assert [x['title'] for x in newer._station_queue]==['NextB']
    assert [x['title'] for x in obj._station_queue]==['NextA']


@pytest.mark.parametrize('same_adapter',[False,True])
def test_review_monitor_recovery_seek_rejects_newer_source_epoch(rig,monkeypatch,same_adapter):
    import time as real_time
    obj,p,entry,make,_=rig;newer=obj if same_adapter else make('b')
    obj._play_track_object(entry('OwnerA'),station='mixed:youtube',queue=[entry('NextA')],auto_advance=True)
    clock=[100.0];calls=[];original_status=p.status_if_running
    class TwoTicks:
        count=0
        def wait(self,interval):
            self.count+=1;clock[0]+=3
            return self.count>2
    obj._monitor_stop=TwoTicks();obj._transition_guard_until=0;obj._manual_transport_guard_until=0;obj._current_started_monotonic=1
    monkeypatch.setattr(ym,'time',SimpleNamespace(monotonic=lambda:clock[0],time=real_time.time,sleep=lambda _:None))
    def status():
        calls.append('status');result=original_status()
        if len(calls)==1:result.update(error='synthetic network interruption',error_kind='network',position_ms=42000)
        return result
    monkeypatch.setattr(p,'status_if_running',status)
    p.recreate_if_unresponsive=lambda **kw:False
    seeks=[]
    def seek(position,*,before_send):
        before_send();seeks.append((position,p.title));return {'seek_applied':True}
    p.seek=seek;original_play=obj._play_track_object
    def recovered_then_new_source(*args,**kwargs):
        data=original_play(*args,**kwargs)
        # Use the original entry point for a same-adapter replacement to avoid
        # recursively invoking this deliberate completion-boundary barrier.
        if same_adapter:original_play(entry('NewB'),station='mixed:youtube',queue=[])
        else:newer._play_track_object(entry('NewB'),station='mixed:youtube',queue=[])
        return data
    monkeypatch.setattr(obj,'_play_track_object',recovered_then_new_source)
    obj._station_monitor()
    assert p.starts==['OwnerA','OwnerA','NewB'] and p.title=='NewB' and seeks==[]


def test_review_native_volume_guard_rejects_before_ipc(monkeypatch):
    from jarvis.integrations.background.local_player import BackgroundLocalPlayer
    player=BackgroundLocalPlayer();sent=[]
    monkeypatch.setattr(player,'_ensure',lambda:None)
    monkeypatch.setattr(player,'_request_unlocked',lambda payload,**kw:sent.append(payload) or {})
    def cancelled():raise ym.PlaybackSuperseded('superseded source must not change volume')
    with pytest.raises(ym.PlaybackSuperseded):player.guarded_volume(35,before_send=cancelled)
    assert sent==[]


def test_review_stale_download_does_not_change_new_source_volume(rig,monkeypatch):
    obj,p,entry,make,audio=rig;newer=make('b')
    entered=threading.Event();release=threading.Event();volumes=[]
    monkeypatch.setattr(p,'set_volume',lambda value:volumes.append(value) or {'verified':True})
    def download(_):
        entered.set();assert release.wait(3)
        return {'local_path':str(audio),'title':'Stale'}
    monkeypatch.setattr(obj._youtube,'download_audio',download)
    worker,result=run_in_thread(lambda:obj._play_track_object(entry('Stale',False)));assert entered.wait(3)
    try:newer._play_track_object(entry('New'))
    finally:release.set();join_thread(worker,result)
    assert isinstance(result.get('error'),ym.PlaybackSuperseded)
    assert len(volumes)==1 and p.starts==['New']


def test_review_clear_queue_invalidates_pending_expansion(rig,monkeypatch):
    obj,p,entry,*_=rig
    obj._play_track_object(entry('Owner'),station='mixed:youtube',queue=[entry('OldNext')])
    entered=threading.Event();release=threading.Event()
    def expand(_url,**kwargs):
        entered.set();assert release.wait(3)
        return {'items':[{'id':'Late','title':'Late','webpage_url':'https://www.youtube.com/watch?v=Late'}]}
    monkeypatch.setattr(obj._youtube,'expand',expand)
    worker,result=run_in_thread(lambda:obj.execute('youtube_url',query='https://www.youtube.com/watch?v=Late',queue=True))
    assert entered.wait(3)
    try:assert obj.queue_clear()['verified']
    finally:release.set();join_thread(worker,result)
    assert result['value']['cancelled'] and obj._station_queue==[]


def test_review_short_stream_rejection_cannot_stop_newer_source(rig,monkeypatch):
    from tests.test_media_reliability import Track
    obj,p,entry,make,_=rig;newer=make('b');native_play=p.play
    def play(*args,**kwargs):
        result=native_play(*args,**kwargs)
        if kwargs.get('title')=='Short':result['duration_ms']=0
        return result
    monkeypatch.setattr(p,'play',play)
    monkeypatch.setattr(obj,'_stream_candidates_for_track',lambda *a:[{'id':'Short','title':'Short','url':'inert:short','expected_duration_ms':180000}])
    monkeypatch.setattr(obj,'_cached_candidate_path',lambda *a:'/inert-prepared')
    def delayed_duration():
        newer._play_track_object(entry('NewB'),station='mixed:youtube',queue=[])
        return {'duration_ms':1000}
    p.status=delayed_duration
    with pytest.raises(ym.PlaybackSuperseded):obj._play_track_object(Track('Short'),fast_transition=True)
    assert p.starts==['Short','NewB'] and p.state=='playing' and p.title=='NewB' and p.stops==0


def test_review_orphaned_shared_source_does_not_expose_old_queue(rig,tmp_path):
    import gc,weakref
    obj,p,entry,*_=rig
    obj._play_track_object(entry('Old'),station='mixed:youtube',queue=[entry('OldNext')])
    newer=ym.YandexMusicAdapter(Config(tmp_path/'gone'));newer._after_playback_effect=lambda *a,**kw:None
    newer._play_track_object(entry('New'),station='mixed:youtube',queue=[])
    ref=weakref.ref(newer);del newer;gc.collect();assert ref() is None
    with pytest.raises(RuntimeError,match='больше недоступен'):obj.queue_status()
    with pytest.raises(RuntimeError,match='больше недоступен'):obj.status()


@pytest.mark.parametrize('same_adapter',[False,True])
def test_review_transport_status_delay_cannot_cancel_later_source_preparation(rig,monkeypatch,same_adapter):
    obj,p,entry,make,audio=rig;newer=obj if same_adapter else make('b')
    obj._play_track_object(entry('OwnerA'),station='mixed:youtube',queue=[])
    transport_entered=threading.Event();release_transport=threading.Event()
    source_entered=threading.Event();release_source=threading.Event()
    original_status=p.status_if_running;transport_thread=[]
    def status():
        if threading.get_ident() in transport_thread:
            transport_entered.set();assert release_transport.wait(3)
        return original_status()
    monkeypatch.setattr(p,'status_if_running',status)
    def resume():
        transport_thread.append(threading.get_ident())
        return obj.transport('play')
    first,r1=run_in_thread(resume);assert transport_entered.wait(3)
    def expand(_url,**kwargs):
        source_entered.set();assert release_source.wait(3)
        return {'items':[{'id':'NewB','title':'NewB','webpage_url':'https://www.youtube.com/watch?v=NewB'}]}
    monkeypatch.setattr(newer._youtube,'expand',expand)
    monkeypatch.setattr(newer._youtube,'download_audio',lambda _:{'local_path':str(audio),'title':'NewB'})
    second,r2=run_in_thread(lambda:newer.play_youtube_url('https://www.youtube.com/watch?v=NewB'))
    assert source_entered.wait(3)
    try:
        release_transport.set();join_thread(first,r1)
    finally:release_source.set();join_thread(second,r2)
    assert r1['value'].get('cancelled') and r2['value']['verified']
    assert p.resumes==0 and p.starts==['OwnerA','NewB']


def test_review_delayed_old_end_of_media_cannot_skip_new_same_adapter_source(rig,monkeypatch):
    import time as real_time
    obj,p,entry,_,audio=rig
    obj._play_track_object(entry('OldA'),station='mixed:youtube',queue=[entry('OldNextA')],auto_advance=True)
    clock=[100.0];original_status=p.status_if_running;calls=[]
    class TwoTicks:
        count=0
        def wait(self,interval):
            self.count+=1;clock[0]+=11
            return self.count>2
    obj._monitor_stop=TwoTicks();obj._transition_guard_until=0;obj._manual_transport_guard_until=0;obj._current_started_monotonic=1
    monkeypatch.setattr(ym,'time',SimpleNamespace(monotonic=lambda:clock[0],time=real_time.time,sleep=lambda _:None))
    monkeypatch.setattr(obj._youtube,'expand',lambda _url,**kwargs:{'items':[{'id':name,'title':name,'webpage_url':'https://www.youtube.com/watch?v='+name} for name in ('NewSource','NewNext')]})
    monkeypatch.setattr(obj._youtube,'download_audio',lambda url,**kwargs:{'local_path':str(audio),'title':url.split('=')[-1]})
    def status():
        calls.append(1)
        if len(calls)==1:return {'state':'playing','title':'OldA','url':'inert:old','position_ms':175000,'duration_ms':180000}
        if len(calls)==2:
            assert obj.play_youtube_url('https://www.youtube.com/watch?v=NewSource')['verified']
            return {'state':'stopped','title':'OldA','url':'inert:old','media_status':'EndOfMedia','position_ms':180000,'duration_ms':180000}
        return original_status()
    monkeypatch.setattr(p,'status_if_running',status)
    obj._station_monitor()
    assert p.starts==['OldA','NewSource'] and p.title=='NewSource'
    assert [x['title'] for x in obj._station_queue]==['NewNext']


@pytest.mark.parametrize('same_adapter',[False,True])
def test_review_old_observation_cannot_admit_new_auto_next(rig,same_adapter):
    from jarvis.integrations.media_state import local_playback_session
    obj,p,entry,make,_=rig;newer=obj if same_adapter else make('b')
    obj._play_track_object(entry('OldA'),station='mixed:youtube',queue=[entry('OldNext')],auto_advance=True)
    serial=obj._manual_transport_serial;observed_epoch=local_playback_session(p).epoch
    newer._play_track_object(entry('NewB'),station='mixed:youtube',queue=[entry('NewNext')],auto_advance=True)
    result=obj._observed_auto_next(serial,observed_epoch)
    assert result['cancelled'] and p.starts==['OldA','NewB']
    assert [x['title'] for x in newer._station_queue]==['NewNext']


@pytest.mark.parametrize('same_adapter',[False,True])
@pytest.mark.parametrize('action',['stop','pause'])
@pytest.mark.parametrize('native_resume_verified',[False,True])
def test_review_interrupted_submission_rebuilds_last_verified_source(rig,monkeypatch,same_adapter,action,native_resume_verified):
    from jarvis.integrations.media_state import local_playback_session
    obj,p,entry,make,_=rig;newer=obj if same_adapter else make('b')
    obj._play_track_object(entry('OldA'),station='mixed:youtube',queue=[entry('OldNextA')])
    entered=threading.Event();release=threading.Event();original=p.play
    def play(*args,**kwargs):
        data=original(*args,**kwargs)
        if kwargs.get('title')=='NewB':entered.set();assert release.wait(3)
        return data
    monkeypatch.setattr(p,'play',play)
    worker,result=run_in_thread(lambda:newer._play_track_object(entry('NewB'),station='mixed:youtube',queue=[entry('NewNextB')]))
    assert entered.wait(3)
    try:
        controlled=obj.transport(action)
        assert controlled['verified'] and p.state==('stopped' if action=='stop' else 'paused')
    finally:release.set();join_thread(worker,result)
    assert isinstance(result.get('error'),ym.PlaybackSuperseded)
    assert local_playback_session(p).needs_source_rebuild()
    p.resume_verified=native_resume_verified
    resumed=obj.transport('play')
    assert resumed['verified'] and p.resumes==0
    assert p.starts==['OldA','NewB','OldA'] and p.title==obj._current_track_obj['title']=='OldA'
    assert [x['title'] for x in obj._station_queue]==['OldNextA']
    assert local_playback_session(p).owner() is obj and not local_playback_session(p).needs_source_rebuild()


@pytest.mark.parametrize('action',['stop','pause'])
def test_review_failed_submission_before_native_replace_does_not_promote_new_metadata(rig,monkeypatch,action):
    from jarvis.integrations.media_state import local_playback_session
    obj,p,entry,make,_=rig;newer=make('b')
    obj._play_track_object(entry('OldA'),station='mixed:youtube',queue=[entry('OldNextA')])
    original=p.play
    def failed_send(*args,**kwargs):
        if kwargs.get('title')=='UnsentB':
            kwargs['before_start']()
            raise RuntimeError('synthetic IPC send failed before source replacement')
        return original(*args,**kwargs)
    monkeypatch.setattr(p,'play',failed_send)
    with pytest.raises(RuntimeError,match='IPC send failed'):
        newer._play_track_object(entry('UnsentB'),station='mixed:youtube',queue=[entry('WrongNext')])
    assert p.starts==['OldA'] and local_playback_session(p).needs_source_rebuild()
    assert obj.transport(action)['verified']
    assert obj.transport('play')['verified']
    assert p.resumes==0 and p.starts==['OldA','OldA'] and p.title=='OldA'
    assert obj._current_track_obj['title']=='OldA' and [x['title'] for x in obj._station_queue]==['OldNextA']
