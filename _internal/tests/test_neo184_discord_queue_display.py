"""Discord notices use physical playback identity; no network or audio edges."""
from collections import deque
from types import MethodType, SimpleNamespace
from unittest.mock import Mock
import threading
import time
import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication
from jarvis.runtime import media_events as bus
from jarvis.testing.isolation import external_io_blocked
from jarvis.ui.main_window import MainWindow
from jarvis.integrations.discord_navigation import ReliableDiscordAdapter

@pytest.fixture
def rig(monkeypatch):
    app = QApplication.instance() or QApplication([])
    bus.drain_now_playing(1000)
    monkeypatch.setattr(bus, '_LATEST_BY_SOURCE', {})
    import jarvis.ui.main_window as ui
    monkeypatch.setattr(ui, 'discord_session_meter', lambda: SimpleNamespace(set_active=lambda value:None))
    monkeypatch.setattr(ui, 'note_media_observation', lambda *a, **k:None)
    w = QObject()
    for key, value in dict(_quitting=False, _language_code=lambda:'ru', _quick_media_server=lambda:'Synthetic guild',
        _busy=False, _pending_response_bubble=None, _last_now_playing_by_source={},
        _media_notice_suppressed_until={}, _music_overlay_last_event={}, _refresh_robot_stage_info=Mock(),
        _pending_media_results157={}, _ambient_typing_queue=deque(), _typing_text='', _stream_text='',
        _discord_observer_last_key='', _music_visuals_visible=lambda:False,
        config=SimpleNamespace(get=lambda k,d=None:d)).items():setattr(w,key,value)
    w.bubbles=[]
    def append(text,**kwargs):
        row={'text':text};w.bubbles.append(row);return row
    w._append_assistant=append
    w.chat=SimpleNamespace(update_bubble=lambda row,text:row.update(text=text))
    for name in ['_now_playing_queue_preview','_media_queue_ready158','_remember_media_bubble157',
        '_refresh_media_bubble_queue157','_drain_media_now_playing_events','_discord_observer_ready']:
        setattr(w,name,MethodType(getattr(MainWindow,name),w))
    gate=threading.Event();started=threading.Event()
    data={'server':'Synthetic guild','current':{'title':'На обратной стороне земли','artist':'Дора','track_id':'B'},
          'queue':[{'track_id':str(i),'artist':'Synthetic','title':'Next '+str(i)} for i in range(1,6)],
          'playing':True, 'connected':True}
    def execute(owner,args):
        assert owner=='discord' and args=={'action':'music_queue_status','server':'Synthetic guild'}
        started.set();assert gate.wait(2)
        return SimpleNamespace(success=True,data=data.copy())
    w.agent=SimpleNamespace(tools=SimpleNamespace(execute=execute))
    with external_io_blocked():
        yield app,w,gate,started,data
    gate.set()
    reader=getattr(w,'_media_queue_reader158',None)
    if reader:reader.close()
    bus.drain_now_playing(1000)


def pump(app, predicate):
    end=time.monotonic()+1.2
    while not predicate() and time.monotonic()<end:
        app.processEvents();time.sleep(.002)
    assert predicate()

@pytest.mark.parametrize('repeat_source',['observer','shared_snapshot'])
def test_status_repeat_during_queue_fetch_cannot_orphan_playback_bubble(rig,repeat_source):
    app,w,gate,started,data=rig
    current=data['current']
    bus.publish_now_playing('discord',current['title'],current['artist'],current['track_id'])
    w._drain_media_now_playing_events()
    assert started.wait(1)
    assert 'недоступна' in w.bubbles[0]['text']
    original=bus.latest_now_playing('discord')
    if repeat_source=='observer':w._discord_observer_ready(data)
    else:
        adapter=ReliableDiscordAdapter.__new__(ReliableDiscordAdapter)
        adapter._remote_status_cache={}
        adapter._cache_remote_snapshot({'guild_id':'123','name':'Synthetic guild'},data)
    w._drain_media_now_playing_events()
    gate.set();pump(app,lambda:not w._media_queue_reader158._running)
    current=bus.latest_now_playing('discord')
    print({'source':repeat_source,'original_event':original.event_id,'latest_event':current.event_id,
           'bubble_event':w._media_bubbles157['discord'][0],'queue_count':len(data['queue']),'bubble':w.bubbles[0]['text']})
    assert len(w.bubbles)==1
    assert 'Next 5' in w.bubbles[0]['text']


def publish(data, *, observation=False, announce=True):
    current=data['current']
    return bus.publish_now_playing('discord',current['title'],current.get('artist',''),current['track_id'],
        playback_generation=data.get('playback_generation',0),playback_scope=data.get('playback_scope',''),
        observation=observation,reason='shared_queue_state' if observation else 'track_started',announce=announce)


def complete(rig):
    app,w,gate,started,data=rig
    gate.set();pump(app,lambda:not w._media_queue_reader158._running)


def test_reordered_queue_replaces_same_bubble_from_authoritative_snapshot(rig):
    app,w,gate,started,data=rig
    publish(data);w._drain_media_now_playing_events();complete(rig)
    row=w.bubbles[0];before=bus.latest_now_playing('discord').event_id
    data['queue'].reverse()
    publish(data,observation=True);w._drain_media_now_playing_events();complete(rig)
    assert len(w.bubbles)==1 and bus.latest_now_playing('discord').event_id==before
    assert row['text'].splitlines()[2]=='1. Synthetic — Next 5'


def test_finite_queue_end_is_empty_and_fetch_failure_is_unavailable(rig):
    app,w,gate,started,data=rig
    publish(data);w._drain_media_now_playing_events();complete(rig)
    data['queue']=[]
    publish(data,observation=True);w._drain_media_now_playing_events();complete(rig)
    assert 'Очередь: пуста.' in w.bubbles[0]['text'] and 'Next' not in w.bubbles[0]['text']
    w.agent.tools.execute=lambda *args:SimpleNamespace(success=False,data={})
    publish(data,observation=True);w._drain_media_now_playing_events();complete(rig)
    assert 'недоступна' in w.bubbles[0]['text'] and 'пуста' not in w.bubbles[0]['text']

@pytest.mark.parametrize('change',['track','generation','guild','owner','source'])
def test_fresh_status_from_another_playback_never_becomes_this_queue(rig,change):
    app,w,gate,started,data=rig
    data.update(playback_generation=3,playback_scope='owner:guild:source')
    publish(data);w._drain_media_now_playing_events();assert started.wait(1)
    if change=='track':data['current']={**data['current'],'track_id':'other','title':'Other'}
    elif change=='generation':data['playback_generation']=4
    else:data['playback_scope']=data['playback_scope'].replace(change,'different')
    complete(rig)
    assert 'Next' not in w.bubbles[0]['text'] and 'недоступна' in w.bubbles[0]['text']

@pytest.mark.parametrize('change',['generation','guild','owner','source'])
def test_repeat_same_track_in_new_playback_gets_fresh_identity(rig,change):
    app,w,gate,started,data=rig
    data.update(playback_generation=3,playback_scope='owner:guild:source')
    publish(data);w._drain_media_now_playing_events();complete(rig)
    before=bus.latest_now_playing('discord').event_id
    if change=='generation':data['playback_generation']=4
    else:data['playback_scope']=data['playback_scope'].replace(change,'different')
    data['queue']=[{'title':'Fresh queue'}]
    publish(data,observation=True);w._drain_media_now_playing_events();complete(rig)
    assert bus.latest_now_playing('discord').event_id!=before
    assert len(w.bubbles)==2 and 'Fresh queue' in w.bubbles[-1]['text']
    assert 'Next 5' in w.bubbles[0]['text']


def test_explicit_repeat_same_generation_is_not_deduplicated(rig):
    app,w,gate,started,data=rig
    first=publish(data);second=publish(data)
    assert first!=second


def test_slow_old_observation_cannot_replace_new_transport_generation(rig):
    app,w,gate,started,data=rig
    data.update(playback_generation=4,playback_scope='host:guild:epoch')
    current=publish(data)
    data['playback_generation']=3
    assert publish(data,observation=True)==current
    assert bus.latest_now_playing('discord').playback_generation==4


def test_rapid_next_reconnect_cannot_install_previous_queue(rig):
    app,w,gate,started,data=rig
    data.update(playback_generation=1,playback_scope='old-host:guild:epoch')
    publish(data);w._drain_media_now_playing_events();assert started.wait(1)
    data.update(playback_generation=2)
    data['current']={'title':'Intermediate','artist':'Synthetic','track_id':'C'}
    publish(data);w._drain_media_now_playing_events()
    data.update(playback_generation=1,playback_scope='reconnected-host:guild:epoch')
    data['current']={'title':'Reconnected','artist':'Synthetic','track_id':'D'}
    data['queue']=[{'title':'Reconnected next '+str(i)} for i in range(1,6)]
    publish(data);w._drain_media_now_playing_events();complete(rig)
    assert 'Reconnected next 5' in w.bubbles[-1]['text']
    assert 'Reconnected next' not in w.bubbles[0]['text']
    assert 'Reconnected next' not in w.bubbles[1]['text']


def test_discord_control_snapshot_preserves_scope_generation_without_private_fields():
    from jarvis.integrations.background.discord_bot import _public_control_data
    data=_public_control_data({'playback_scope':'host:123:4','playback_generation':5,'guild_id':'123',
                               'token':'never exported','url':'https://private.invalid'})
    assert data=={'playback_scope':'host:123:4','playback_generation':5,'guild_id':'123'}

@pytest.mark.parametrize('mode',['manual','automatic'])
def test_real_adapter_next_preserves_queue_and_updates_current_plus_five(rig,tmp_path,monkeypatch,mode):
    from jarvis.integrations import discord_navigation as dc
    from jarvis.integrations import yandex_music as ym
    app,w,gate,started,data=rig
    monkeypatch.setattr(ym.YandexMusicAdapter,'clear_transient_cache_on_startup_once',lambda self:{})
    cfg=SimpleNamespace(paths=SimpleNamespace(root=tmp_path),get=lambda key,default=None:default)
    adapter=ReliableDiscordAdapter(cfg)
    profile={'name':'Synthetic guild','guild_id':'123'}
    rows=[{'title':'Hotel Room','artist':'Ax and the Hatchetmen','track_id':'A'},dict(data['current'])]
    rows += [{'track_id':name,'title':'Song '+name,'artist':'Synthetic'} for name in 'CDEFG']
    state=adapter._queue_state(profile)
    state.update(current={**rows[0],'generation':1,'_transport_generation':1,'seen_active':True,
                          'started_at':time.monotonic()-180,'_expected_duration_ms':180000},
                 items=rows[1:],generation=1,source_epoch=9)
    physical={'current':dict(rows[0]),'playing':True,'connected':True,'playback_generation':1}
    starts=[]
    def play_url(profile,url,**kwargs):
        kwargs.get('before_start',lambda:None)()
        starts.append(kwargs['title'])
        physical.update(current={'title':kwargs['title'],'artist':kwargs['artist']},playing=True,
                        playback_generation=physical['playback_generation']+1)
        return {**physical,'title':kwargs['title'],'artist':kwargs['artist'],'verified':True}
    bot=SimpleNamespace(playback_status=lambda profile:dict(physical),play_url=play_url)
    monkeypatch.setattr(adapter,'_background_for_profile',lambda profile:bot)
    monkeypatch.setattr(adapter,'_background_profile',lambda server:profile)
    monkeypatch.setattr(adapter,'_prefetch_queue_entry',lambda entry:None)
    monkeypatch.setattr(adapter,'_prefetch_next_queue_item',lambda state:None)
    monkeypatch.setattr(adapter,'_start_queue_monitor',lambda profile:None)
    monkeypatch.setattr(adapter,'_broadcast_queue_snapshot',lambda *args,**kwargs:None)
    monkeypatch.setattr(adapter,'_prepared_queue_streams',lambda entry:[
        {'id':entry['track_id'],'title':entry['title'],'artist':entry['artist'],'url':'inert:never-played'}])
    w.agent.tools.execute=lambda owner,args:SimpleNamespace(success=True,data=adapter.music_queue_status(args['server']))
    first=adapter._local_queue_snapshot(profile)
    publish(first);w._drain_media_now_playing_events();complete(rig)
    assert '5. Synthetic — Song F' in w.bubbles[-1]['text'] and 'Song G' not in w.bubbles[-1]['text']
    if mode=='manual':
        result=adapter.music_skip(profile['name']);assert result['verified']
    else:
        physical.update(playing=False,last_finished_generation=1)
        ticks=[]
        def sleep(seconds):
            ticks.append(seconds)
            if len(ticks)>1:raise RuntimeError('monitor tick complete')
        monkeypatch.setattr(dc,'time',SimpleNamespace(sleep=sleep,monotonic=time.monotonic,time=time.time))
        with pytest.raises(RuntimeError,match='monitor tick complete'):
            adapter._queue_monitor(adapter._profile_key(profile))
    committed=bus.latest_now_playing('discord')
    assert committed.title==rows[1]['title'] and committed.playback_generation==2
    assert committed.playback_scope==adapter._media_playback_scope(profile)
    w._drain_media_now_playing_events()
    snapshot=adapter._local_queue_snapshot(profile)
    w._discord_observer_ready(snapshot);w._drain_media_now_playing_events();complete(rig)
    assert bus.latest_now_playing('discord').event_id==committed.event_id
    assert [item['track_id'] for item in snapshot['queue']]==list('CDEFG')
    assert starts==[rows[1]['title']] and len(w.bubbles)==2
    assert rows[1]['title'] in w.bubbles[-1]['text'] and '5. Synthetic — Song G' in w.bubbles[-1]['text']
    assert '1. Synthetic — Song C' in w.bubbles[-1]['text']


def test_observed_pause_resume_keeps_same_playback_bubble(rig):
    app,w,gate,started,data=rig
    data.update(playback_generation=7,playback_scope='host:guild:epoch')
    publish(data);w._drain_media_now_playing_events();complete(rig)
    event_id=bus.latest_now_playing('discord').event_id
    data.update(playing=False,paused=True)
    w._discord_observer_ready(data);w._drain_media_now_playing_events();complete(rig)
    data.update(playing=True,paused=False)
    w._discord_observer_ready(data);w._drain_media_now_playing_events();complete(rig)
    assert len(w.bubbles)==1 and 'Next 5' in w.bubbles[0]['text']
    assert bus.latest_now_playing('discord').event_id==event_id


def test_stale_observation_preserves_overlay_and_cached_remote_queue(rig):
    app,w,gate,started,data=rig
    data.update(playback_generation=7,playback_scope='host:guild:epoch')
    publish(data);w._drain_media_now_playing_events();complete(rig)
    old={**data,'playback_generation':6,'current':{'title':'Old title','track_id':'old'}}
    before=dict(w._music_overlay_last_event)
    w._discord_observer_ready(old)
    assert w._music_overlay_last_event==before
    adapter=ReliableDiscordAdapter.__new__(ReliableDiscordAdapter)
    adapter._remote_status_cache={}
    profile={'guild_id':'123','name':'Synthetic guild'}
    adapter._cache_remote_snapshot(profile,data)
    actual=adapter._cache_remote_snapshot(profile,old)
    assert actual['playback_generation']==7
    assert adapter._remote_status_cache['123'][1]['current']['track_id']=='B'


def test_failed_fetch_recovers_on_later_status_without_reusing_old_queue(rig):
    app,w,gate,started,data=rig
    original=w.agent.tools.execute
    w.agent.tools.execute=lambda *args:SimpleNamespace(success=False,data={})
    publish(data);w._drain_media_now_playing_events();complete(rig)
    assert 'недоступна' in w.bubbles[0]['text']
    w.agent.tools.execute=original
    publish(data,observation=True);w._drain_media_now_playing_events();complete(rig)
    assert len(w.bubbles)==1 and 'Next 5' in w.bubbles[0]['text']


def test_paused_queue_completion_preserves_all_five_before_resume(rig):
    app,w,gate,started,data=rig
    data.update(playback_generation=7,playback_scope='host:123:9')
    w._discord_observer_ready(data);w._drain_media_now_playing_events();complete(rig)
    before=w.bubbles[0]['text'];event_id=bus.latest_now_playing('discord').event_id
    assert 'Next 5' in before
    data.update(playing=False,paused=True)
    w._discord_observer_ready(data);w._drain_media_now_playing_events();complete(rig)
    assert bus.latest_now_playing('discord').event_id==event_id
    assert len(w.bubbles)==1 and 'Next 5' in w.bubbles[0]['text']


def test_repeated_status_updates_telegram_panel_without_duplicate_popup(rig,monkeypatch):
    import queue
    from jarvis.integrations.telegram_remote import TelegramRemoteController
    import jarvis.integrations.telegram_remote as telegram
    app,w,gate,started,data=rig
    logical_time=[100.]
    monkeypatch.setattr(telegram,'time',SimpleNamespace(monotonic=lambda:logical_time[0],sleep=lambda _:None))
    class Exhausted(Exception):pass
    class InertQueue(queue.Queue):
        def get(self,*args,**kwargs):
            try:return self.get_nowait()
            except queue.Empty:raise Exhausted()
        def get_nowait(self):return super().get(block=False)
    controller=TelegramRemoteController.__new__(TelegramRemoteController)
    for key,value in dict(_latest_media_by_source={},_media_history_by_source={},_stop=threading.Event(),
        _media_notifications=InertQueue(),_media_inflight_sources={},_media_result_event_ids=set(),
        _media_ack_until={},_media_last_key='',_media_last_at=0.,_allowed_chat_ids=lambda:['recording'],
        _panel_by_chat={'recording':42},_view_by_chat={'recording':'md'},_media_queue_suffix=lambda source:'',
        config=SimpleNamespace(get=lambda key,default=None:default)).items():setattr(controller,key,value)
    sends=[];panels=[]
    controller._send_message=lambda target,text:sends.append((target,text))
    controller._roll_panel_to_bottom=lambda *args:panels.append(args)
    refreshed=[];controller._show_view=lambda *args:refreshed.append(args)
    data.update(playback_generation=7,playback_scope='host:123:9')
    bus.subscribe_now_playing(controller._enqueue_media_notification)
    try:
        for index in range(3):
            logical_time[0]=100.+index*5
            data['position_ms']=index*5000
            if index==2:
                data['current']={'title':'Second','artist':'Synthetic','track_id':'C'}
                data['playback_generation']=8
            w._discord_observer_ready(data)
            with pytest.raises(Exhausted):controller._media_notification_loop()
            assert controller._latest_media_by_source['discord'].position_ms==index*5000
        assert len(sends)==2 and 'Second' in sends[-1][1]
        # NEO 187 HREQ-028: a posted notice re-rolls the menu below it; unchanged
        # playback refreshes the same menu in place instead of delete + resend.
        assert len(panels)==2 and refreshed==[('recording','md',42)]
    finally:bus.unsubscribe_now_playing(controller._enqueue_media_notification)

@pytest.mark.parametrize('scope,generation',[('host:123:9',8),('host:123:10',8)])
def test_old_epoch_or_generation_cannot_roll_back_bus_overlay_cache_or_dashboard(rig,scope,generation):
    app,w,gate,started,data=rig
    data.update(playback_generation=9,playback_scope='host:123:10')
    w._discord_observer_ready(data)
    current=bus.latest_now_playing('discord');overlay=dict(w._music_overlay_last_event)
    old={**data,'playback_scope':scope,'playback_generation':generation,
         'current':{'title':'Old source','track_id':'old'}}
    w._discord_observer_ready(old)
    assert bus.latest_now_playing('discord').event_id==current.event_id
    assert w._music_overlay_last_event==overlay
    assert publish(old,observation=True)==current.event_id
    adapter=ReliableDiscordAdapter.__new__(ReliableDiscordAdapter)
    adapter._instance_id='receiver';adapter._remote_status_cache={}
    profile={'guild_id':'123','name':'Synthetic guild'}
    adapter._profile_for_bus=lambda *args:profile
    dashboard=[]
    adapter._background_for_profile=lambda *args:SimpleNamespace(publish_music_state=lambda p,s:dashboard.append(s))
    for snapshot in [data,old]:
        adapter._handle_bus_message({'kind':'queue_state','sender_instance':'host','guild_id':'123',
                                     'server':'Synthetic guild','data':snapshot})
    assert adapter._remote_status_cache['123'][1]['current']['track_id']=='B'
    assert dashboard[-1]['current']['track_id']=='B'


def snapshot_adapter():
    def row(name,generation=None):
        result={'title':name,'artist':'Synthetic','track_id':name}
        if generation is not None:result['_transport_generation']=generation
        return result
    state={'current':row('A',1),'items':[row(name) for name in 'BCDEFG'],
           'source_epoch':9,'generation':1,'history':[]}
    adapter=ReliableDiscordAdapter.__new__(ReliableDiscordAdapter)
    adapter._queue_lock=threading.RLock();adapter._instance_id='host'
    adapter._snapshot_condition=threading.Condition(threading.RLock())
    adapter._snapshot_probes={}
    adapter._queue_state=lambda profile:state
    adapter._display_queue_entry=lambda value:dict(value)
    adapter._public_queue_entry=lambda value:dict(value)
    adapter.config=SimpleNamespace(get=lambda key,default=None:default)
    return adapter,state,row,{'guild_id':'123','name':'Synthetic guild'}

@pytest.mark.parametrize('change',['next','source','reorder'])
def test_snapshot_retries_when_queue_changes_during_status_barrier(rig,change):
    adapter,state,row,profile=snapshot_adapter()
    entered=threading.Event();release=threading.Event();calls=[];result=[]
    physical={'current':row('A'),'playing':True,'connected':True,'playback_generation':1}
    def status(profile):
        calls.append(True)
        if len(calls)==1:entered.set();assert release.wait(2)
        return dict(physical)
    adapter._background_for_profile=lambda profile:SimpleNamespace(playback_status=status)
    worker=threading.Thread(target=lambda:result.append(adapter._local_queue_snapshot(profile)))
    worker.start();assert entered.wait(1)
    try:
        with adapter._queue_lock:
            if change=='reorder':state['items'].reverse()
            else:
                state.update(current=row('B',2),items=[row(name) for name in 'CDEFG'],generation=2)
                physical.update(current=row('B'),playback_generation=2)
                if change=='source':state['source_epoch']=10
    finally:release.set();worker.join(2)
    assert not worker.is_alive() and len(calls)==2
    snapshot=result[0]
    assert not snapshot.get('shared_state_pending')
    assert [item['track_id'] for item in snapshot['queue']]==[item['track_id'] for item in state['items']]
    assert snapshot['current']['track_id']==physical['current']['track_id']
    assert snapshot['playback_generation']==physical['playback_generation']
    assert snapshot['playback_scope'].endswith(':'+str(state['source_epoch']))


def test_snapshot_continuous_next_returns_pending_without_mixed_queue(rig):
    adapter,state,row,profile=snapshot_adapter();calls=[]
    def status(profile):
        calls.append(True)
        with adapter._queue_lock:
            generation=len(calls)+1
            state.update(current=row(str(generation),generation),generation=generation,
                         items=[row('following '+str(generation))])
        return {'current':row(str(generation)),'playing':True,'connected':True,'playback_generation':generation}
    adapter._background_for_profile=lambda profile:SimpleNamespace(playback_status=status)
    snapshot=adapter._local_queue_snapshot(profile)
    assert len(calls)==2 and snapshot['shared_state_pending']
    assert snapshot['queue']==[] and snapshot['current']=={}


def test_remote_status_returns_accepted_snapshot_instead_of_rejected_wire_packet(rig):
    app,w,gate,started,data=rig
    adapter=ReliableDiscordAdapter.__new__(ReliableDiscordAdapter)
    adapter._remote_status_cache={}
    profile={'guild_id':'123','name':'Synthetic guild'}
    data.update(playback_scope='host:123:10',playback_generation=9)
    adapter._cache_remote_snapshot(profile,data)
    old={**data,'playback_scope':'host:123:9','playback_generation':8,
         'current':{'title':'Old source','track_id':'old'}}
    adapter._bus_request=lambda *args,**kwargs:old
    accepted=adapter._remote_request(profile,{'bot_id':'host'},'queue_status')
    assert accepted['current']['track_id']=='B' and accepted['playback_generation']==9


def test_pending_local_snapshot_does_not_probe_remote_host(rig):
    adapter,state,row,profile=snapshot_adapter();calls=[]
    def status(profile):
        calls.append(True)
        with adapter._queue_lock:
            generation=len(calls)+1
            state.update(current=row(str(generation),generation),generation=generation)
        return {'current':row(str(generation)),'playing':True,'connected':True,'playback_generation':generation}
    adapter._background_for_profile=lambda profile:SimpleNamespace(playback_status=status)
    adapter._background_profile=lambda server:profile
    adapter._cached_shared_snapshot=lambda *args,**kwargs:None
    adapter._remote_for_any_profile=lambda *args:pytest.fail('a moving local queue must not trigger remote discovery')
    snapshot=adapter.music_queue_status(profile['name'])
    assert snapshot['shared_state_pending'] and len(calls)==2
