"""Discord queue UI lifecycle regressions with real registry and Qt mailbox code.

Only providers, audio-meter observation and persistence boundaries are synthetic.
A local reader clock and Events model slow status reads without real service delays.
"""
from collections import deque
from copy import deepcopy
from dataclasses import asdict
from types import MethodType, SimpleNamespace
import threading
import time


def _run_case(app, tmp_path, case_name):
    # Import complete delivered modules. No AST extraction or rewritten methods.
    from PySide6.QtCore import QObject, QThread
    from jarvis.ui import main_window as ui
    from jarvis.ui import media_queue158 as queue_module
    from PySide6.QtCore import QCoreApplication, QEvent
    from jarvis.runtime import media_events as bus
    from jarvis.runtime.cancellation import cancellation_manager
    from jarvis.tools.registry import ToolRegistry
    from jarvis.tools.domains.browser_media import register_browser_media_tools
    from jarvis.integrations.discord_navigation import ReliableDiscordAdapter

    evidence = {'trace':[], 'registry_results':[], 'provider_calls':[], 'ui_updates':[], 'max_queue_threads':0}
    original_queue_time = queue_module.time
    logical_time = [0.0]
    queue_module.time = SimpleNamespace(monotonic=lambda:logical_time[0])
    lock = threading.Lock()
    now = time.monotonic
    bus.drain_now_playing(1000)
    previous_latest = bus._LATEST_BY_SOURCE
    bus._LATEST_BY_SOURCE = {}
    cancellation_manager().reset()

    class Config:
        paths = SimpleNamespace(root=tmp_path / 'profile', logs=tmp_path / 'profile' / 'logs',
            cache=tmp_path / 'profile' / 'cache', settings=tmp_path / 'profile' / 'settings.json')
        overrides = {}
        def get(self, key, default=None):
            if key in self.overrides:
                return self.overrides[key]
            return {'app.language':'ru', 'integrations.discord.enabled':True,
                'integrations.discord.default_server':'Synthetic guild', 'plugins.enabled':False,
                'neo_model.enabled':False, 'ollama.requests_enabled':False}.get(key, default)
    config = Config()
    profile = {'guild_id':'123', 'name':'Synthetic guild'}
    def row(name, title=None, artist='Synthetic'):
        return {'track_id':name, 'title':title or ('Following '+name), 'artist':artist}
    first = row('A', 'Living Life, In The Night', 'Cheriimoya, Sierra Kidd')
    second = row('B', 'МОЯ', 'ДЕТИ RAVE')
    state = {'current':{**first, '_transport_generation':1, 'generation':1},
        'items':[second]+[row(name) for name in 'CDEFG'], 'history':[],
        'generation':1, 'source_epoch':9, 'source_mode':'my_wave', 'source_title':'Моя волна'}
    physical = {'current':deepcopy(first), 'playing':True, 'paused':False, 'connected':True,
        'playback_generation':1}
    adapter = ReliableDiscordAdapter.__new__(ReliableDiscordAdapter)
    adapter.config = config
    adapter._instance_id = 'synthetic-owner'
    adapter._queue_lock = threading.RLock()
    adapter._queue_state = lambda requested: state
    jobs = []
    next_after = [None]
    provider_failure = [False]
    database_failure = [False]
    next_job = [None]
    def playback_status(requested):
        assert requested == profile
        assert QThread.currentThread() != app.thread() or next_job[0] is None
        job = next_job[0]
        if job is not None:
            next_job[0] = None
            job['entered'].set()
            assert job['release'].wait(4), 'synthetic provider barrier timeout'
        return deepcopy(physical)
    adapter._background_for_profile = lambda requested: SimpleNamespace(playback_status=playback_status)
    def hold_next():
        job = {'entered':threading.Event(), 'release':threading.Event()}
        jobs.append(job)
        next_job[0] = job
        return job
    def hold_after_snapshot():
        job = {'entered':threading.Event(), 'release':threading.Event()}
        jobs.append(job)
        next_after[0] = job
        return job
    def snapshot():
        return adapter._local_queue_snapshot(profile)
    def publish(data, observation=False):
        current = data['current']
        return bus.publish_now_playing('discord', current['title'], current['artist'], current['track_id'],
            playback_generation=data['playback_generation'], playback_scope=data['playback_scope'],
            observation=observation, reason='shared_discord_observer' if observation else 'track_started')

    class Provider:
        def execute(self, action, **kwargs):
            assert action in {'music_my_wave', 'music_queue_status'}
            assert kwargs.get('server') == 'Synthetic guild'
            with lock:
                evidence['provider_calls'].append({'action':action, 'thread':threading.current_thread().name})
            if action == 'music_queue_status' and provider_failure[0]:
                provider_failure[0] = False
                raise RuntimeError('Synthetic status failure')
            data = snapshot()
            if action == 'music_queue_status' and next_after[0] is not None:
                job = next_after[0]
                next_after[0] = None
                job['entered'].set()
                assert job['release'].wait(4), 'after-snapshot barrier timeout'
            if action == 'music_my_wave':
                publish(data)
                data = {**data, 'title':data['current']['title'], 'artist':data['current']['artist'],
                    'submitted':True}
            return data

    registry = ToolRegistry.__new__(ToolRegistry)
    registry.config = config
    registry._tools = {}
    registry.tool_plugin_owners = {}
    registry.execution_guard = None
    registry.execution_transport = 'local'
    def log_action(*args):
        evidence['trace'].append({'kind':'database.log_action','tool':args[0],'success':args[3]})
        if args[1].get('action') == 'music_queue_status' and database_failure[0]:
            database_failure[0] = False
            raise RuntimeError('Synthetic journal boundary failure')
    registry.database = SimpleNamespace(log_action=log_action)
    registry.context_memory = SimpleNamespace(record_action=lambda *a, **k:None,
        record_domain_action=lambda *a, **k:None)
    registry.conversation_journal = SimpleNamespace(enabled=lambda:False,
        record_execution=lambda *a, **k:None)
    def listener(name, arguments, result):
        data = result.data if isinstance(result.data, dict) else {}
        current = data.get('current') if isinstance(data.get('current'), dict) else {}
        with lock:
            evidence['registry_results'].append({'tool':name, 'arguments':deepcopy(arguments),
                'success':result.success, 'outcome':result.outcome, 'type':type(result).__name__,
                'data_keys':sorted(data), 'scope':data.get('playback_scope'),
                'generation':data.get('playback_generation'), 'current':deepcopy(current),
                'queue':deepcopy(data.get('queue')), 'thread':threading.current_thread().name})
    registry.execution_listener = listener
    register_browser_media_tools(registry, config=config, yandex_adapter=None, spotify_adapter=None,
        youtube_adapter=None, discord_adapter=Provider())
    evidence['registered_tools'] = sorted(registry._tools)
    evidence['discord_schema'] = registry.get('discord').parameters
    original_meter, original_observation = ui.discord_session_meter, ui.note_media_observation
    ui.discord_session_meter = lambda: SimpleNamespace(set_active=lambda value:None)
    ui.note_media_observation = lambda *a, **k:None
    w = QObject()
    for name, value in dict(_quitting=False, _language_code=lambda:'ru', _busy=False,
        _pending_response_bubble=None, _last_now_playing_by_source={}, _media_notice_suppressed_until={},
        _music_overlay_last_event={}, _refresh_robot_stage_info=lambda:None,
        _pending_media_results157={}, _ambient_typing_queue=deque(), _typing_text='', _stream_text='',
        _discord_observer_last_key='', _music_visuals_visible=lambda:False,
        config=config, agent=SimpleNamespace(tools=registry)).items():
        setattr(w, name, value)
    w.bubbles = []
    def append(text, **kwargs):
        bubble = {'text':text}
        w.bubbles.append(bubble)
        return bubble
    w._append_assistant = append
    def update_bubble(bubble, text):
        evidence['ui_updates'].append({'case':evidence.get('active_case'),'before':bubble['text'],'after':text, 'logical_seconds':logical_time[0]})
        bubble.update(text=text)
    w.chat = SimpleNamespace(update_bubble=update_bubble)
    for name in ('_quick_media_server', '_now_playing_queue_preview', '_media_queue_ready158',
        '_remember_media_bubble157', '_refresh_media_bubble_queue157', '_drain_media_now_playing_events',
        '_discord_observer_ready'):
        setattr(w, name, MethodType(getattr(ui.MainWindow, name), w))
    def pump(predicate, label):
        deadline = now() + 2
        while not predicate() and now() < deadline:
            app.processEvents()
            evidence['max_queue_threads'] = max(evidence['max_queue_threads'],len([t for t in threading.enumerate() if t.name.startswith('NEO-QueuePreview-')]))
            time.sleep(.002)
        assert predicate(), label
    def trace(label):
        event = bus.latest_now_playing('discord')
        reader = getattr(w, '_media_queue_reader158', None)
        evidence['trace'].append({'kind':label, 'case':evidence.get('active_case'), 'logical_seconds':logical_time[0], 'event':asdict(event) if event else None,
            'bubbles':deepcopy(w.bubbles), 'revision':dict(reader._revision) if reader else {},
            'cache':repr(reader._cache) if reader else '',
            'running':{key:{'identity':value.identity,'revision':value.revision}
                for key,value in reader._running.items()} if reader else {},
            'wanted':{key:{'identity':value.identity,'revision':value.revision}
                for key,value in reader._wanted.items()} if reader else {}})

    def close_reader():
        reader = getattr(w, '_media_queue_reader158', None)
        if reader is not None:
            reader.close()
        for job in jobs:
            job['release'].set()
        deadline = now()+2
        while now()<deadline:
            owned=[t for t in threading.enumerate() if t.name.startswith('NEO-QueuePreview-')]
            if not owned:
                break
            for thread in owned:
                thread.join(.03)
            app.processEvents()
        assert not [t for t in threading.enumerate() if t.name.startswith('NEO-QueuePreview-')]
        if reader is not None:
            reader.deleteLater()
            w._media_queue_reader158 = None
            QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
            app.processEvents()

    def reset(name):
        close_reader()
        evidence['active_case']=name
        logical_time[0]=0.
        next_job[0]=next_after[0]=None
        jobs.clear()
        config.overrides.clear()
        w._discord_queue_observation_inactive158=False
        provider_failure[0]=database_failure[0]=False
        bus.drain_now_playing(1000)
        bus._LATEST_BY_SOURCE={}
        cancellation_manager().reset()
        w.bubbles.clear()
        for field in ('_last_now_playing_by_source','_media_bubbles157','_last_now_playing_event158',
                '_media_notice_suppressed_until','_music_overlay_last_event','_deferred_queue_notice157'):
            setattr(w,field,{})
        w.agent.tools=registry
        state.clear()
        state.update(current={**first,'_transport_generation':1,'generation':1},
            items=[deepcopy(second)]+[row(name) for name in 'CDEFG'],history=[],generation=1,
            source_epoch=9,source_mode='my_wave',source_title='Моя волна')
        physical.clear()
        physical.update(current=deepcopy(first),playing=True,paused=False,connected=True,playback_generation=1)

    def initial():
        with bus.command_result_owns_media_notice():
            result=registry.execute('discord',{'action':'music_my_wave','server':'Synthetic guild'})
        assert result.success and '5.' in result.text
        event=bus.latest_now_playing('discord')
        assert event and not event.announce
        bubble=append(result.text)
        w._remember_media_bubble157(event,bubble,result.text)
        w._drain_media_now_playing_events()
        return event,bubble

    def observe(data=None):
        w._discord_observer_ready(snapshot() if data is None else data)
        w._drain_media_now_playing_events()
        live=len([t for t in threading.enumerate() if t.name.startswith('NEO-QueuePreview-')])
        evidence['max_queue_threads']=max(evidence['max_queue_threads'],live)
        assert live<=1, 'Duplicate concurrent queue lane threads'

    def wait_done():
        pump(lambda:not w._media_queue_reader158._running,'queue reader did not finish')
        app.processEvents()

    def queue_calls():
        return len([x for x in evidence['provider_calls'] if x['action']=='music_queue_status'])

    def check(condition,message):
        if not condition:
            raise AssertionError(message)

    def start_late():
        event,bubble=initial()
        state['items']=[row('late-'+str(i),'Late-only queue '+str(i)) for i in range(1,7)]
        observed=snapshot()
        job=hold_after_snapshot()
        observe(observed)
        assert job['entered'].wait(1)
        return event,bubble,job,observed

    def case_initial_slow():
        event,bubble=initial();before=bubble['text'];observed=snapshot();count=queue_calls()
        held=hold_next();observe(observed);assert held['entered'].wait(1)
        reader=w._media_queue_reader158
        for tick in (5.,10.,15.):
            logical_time[0]=tick
            observe(deepcopy(observed))
            check(bubble['text']==before,'Known initial queue erased by passive observation')
            check(reader._running['discord'].revision==reader._revision.get('discord',0),'Passive observation invalidated admitted read')
            check(queue_calls()==count+1,'Duplicate provider read during held request')
        trace('initial_queue_retained_through_three_observer_intervals')
        held['release'].set();wait_done()
        check('5. Synthetic — Following F' in bubble['text'],'Initial queue not published after slow read')
        check(len(w.bubbles)==1,'Duplicate initial playback popup')

    def case_automatic_next():
        initial();observe();wait_done()
        state.update(current={**second,'_transport_generation':2,'generation':2},
            items=[row(name) for name in 'CDEFG'],generation=2)
        physical.update(current=deepcopy(second),playback_generation=2)
        observed=snapshot();held=hold_next();publish(observed);w._drain_media_now_playing_events()
        assert held['entered'].wait(1)
        for tick in (5.,10.,15.):
            logical_time[0]=tick;observe(deepcopy(observed))
        held['release'].set();wait_done()
        check(len(w.bubbles)==2,'Automatic next duplicated or lost playback popup')
        check('1. Synthetic — Following C' in w.bubbles[-1]['text'] and '5. Synthetic — Following G' in w.bubbles[-1]['text'],
            'Automatic next did not show its own next five')
        check('МОЯ' in w.bubbles[-1]['text'],'Wrong current track after automatic next')

    def case_explicit_reorder():
        event,bubble=initial();observed=snapshot();held=hold_after_snapshot();observe(observed)
        assert held['entered'].wait(1)
        reader=w._media_queue_reader158;old_revision=reader._running['discord'].revision
        state['items'].reverse()
        current=bus.latest_now_playing('discord')
        bus.publish_now_playing('discord',current.title,current.artist,current.track_id,reason='queue_updated',
            playback_generation=current.playback_generation,playback_scope=current.playback_scope,announce=True)
        w._drain_media_now_playing_events()
        check(reader._revision['discord']==old_revision+1,'Real queue edit did not invalidate pending old queue')
        held['release'].set();wait_done()
        check('1. Synthetic — Following G' in bubble['text'],'Real reorder was lost')
        check(len(w.bubbles)==1,'Queue edit created duplicate current-track popup')

    def case_cache_ttl():
        event,bubble=initial();observe();wait_done();before=bubble['text'];count=queue_calls()
        for _ in range(100):w._now_playing_queue_preview('discord')
        check(queue_calls()==count,'Fresh-cache repaint loop reexecuted status')
        state['items'].reverse();logical_time[0]=2.9
        cached=w._now_playing_queue_preview('discord')
        check(queue_calls()==count and 'Following F' in cached,'Ordinary preview ignored fresh cache')
        observed=snapshot();held=hold_after_snapshot();observe(observed)
        assert held['entered'].wait(1)
        reader=w._media_queue_reader158;revision=reader._running['discord'].revision
        for _ in range(3):observe(deepcopy(observed))
        check(queue_calls()==count+1,'Passive observations duplicated admitted refresh')
        check(reader._running['discord'].revision==revision==reader._revision.get('discord',0),
            'Passive fresh-cache refresh cancelled its admitted read')
        check(bubble['text']==before,'Known queue disappeared before passive refresh completed')
        held['release'].set();wait_done()
        check('1. Synthetic — Following G' in bubble['text'],'Immediate passive reorder was lost inside TTL')
        state['items']=[];observe();wait_done()
        check('Очередь: пуста.' in bubble['text'],'Immediate passive queue end was hidden by fresh cache')
        count=queue_calls();state['items']=[row(name,'TTL refill '+name) for name in 'BCDEFG']
        logical_time[0]=5.8
        check('Очередь: пуста.' in w._now_playing_queue_preview('discord'),'Fresh cached empty queue changed without refresh')
        check(queue_calls()==count,'Ordinary preview refreshed before three-second TTL')
        logical_time[0]=6.0;held=hold_after_snapshot();w._now_playing_queue_preview('discord')
        assert held['entered'].wait(1)
        for _ in range(100):w._now_playing_queue_preview('discord')
        check(queue_calls()==count+1,'Expired-cache repaint loop duplicated read')
        held['release'].set();wait_done()
        check('TTL refill F' in bubble['text'],'Ordinary preview failed to refresh expired cache')

    def case_failure(kind):
        event,bubble=initial();count=queue_calls()
        if kind=='loader_exception':database_failure[0]=True
        else:provider_failure[0]=True
        observe();wait_done()
        check('недоступна' in bubble['text'],'Completed failure concealed by previously known queue')
        reader=w._media_queue_reader158
        if kind=='loader_exception':check(reader._cache['discord'][3] is None,'Loader exception was not cached as None')
        for _ in range(100):w._now_playing_queue_preview('discord')
        check(queue_calls()==count+1,'Cached failure retried at repaint rate')
        observe();wait_done()
        check('5. Synthetic — Following F' in bubble['text'],'Immediate passive retry failed to replace unavailable state')
        check(queue_calls()==count+2,'Failure recovery invoked unexpected extra retries')

    def case_late_transition(kind):
        event,bubble,held,observed=start_late()
        before=bubble['text'];updates=len(evidence['ui_updates'])
        if kind in ('next','source_epoch'):
            state.update(current={**second,'_transport_generation':2,'generation':2},
                items=[row(name,'Fresh '+name) for name in 'CDEFG'],generation=2)
            physical.update(current=deepcopy(second),playback_generation=2)
            if kind=='source_epoch':state['source_epoch']=10
            committed=snapshot();publish(committed);w._drain_media_now_playing_events()
        elif kind=='guild':
            config.overrides['integrations.discord.default_server']='Other guild'
        elif kind in ('stop','pause'):
            stopped={**observed,'playing':False,'paused':kind=='pause'}
            observe(stopped)
            check(bus.latest_now_playing('discord').playing is False,'Stopped observation did not reach event bus')
        elif kind=='disconnect':
            disconnected={**observed,'current':{},'connected':False,'playing':False,'paused':False}
            observe(disconnected)
            trace('explicit_empty_metadata_disconnect_before_late_result')
        elif kind=='unknown':
            observe({'current':{}})
            trace('absent_status_fields_before_late_result')
        elif kind=='registry':
            replacement=ToolRegistry.__new__(ToolRegistry)
            replacement.__dict__.update(registry.__dict__)
            w.agent.tools=replacement
            evidence['trace'].append({'kind':'synthetic_registry_replacement','case':evidence.get('active_case'),
                'old_registry_id':id(registry),'new_registry_id':id(replacement),
                'scope':'robustness control, not ordinary Settings Save'})
        held['release'].set();wait_done();trace('late_result_after_'+kind)
        if kind in ('next','source_epoch'):
            check('Late-only' not in bubble['text'],'Old playback bubble repainted by late queue after new playback')
            check(len(w.bubbles)==2 and 'Fresh G' in w.bubbles[-1]['text'],'New playback did not receive its own queue')
        elif kind in ('unknown','pause'):
            check('Late-only queue 5' in bubble['text'],'Absent status fields incorrectly cancelled known playback')
        else:
            check('Late-only' not in bubble['text'],'Late old queue repainted after '+kind)
            check(len(evidence['ui_updates'])==updates,'Late callback updated bubble after '+kind)


    def case_stop_resume_fresh():
        event,bubble=initial();observe();wait_done()
        initial_calls=queue_calls()
        logical_time[0]=4.
        state['items']=[row('late-'+str(i),'Pre-stop pending '+str(i)) for i in range(1,7)]
        observed=snapshot();held=hold_after_snapshot();observe(observed)
        assert held['entered'].wait(1)
        stopped={**observed,'playing':False,'paused':False,'verified':True}
        observe(stopped)
        reader=w._media_queue_reader158
        check('discord' not in reader._cache,'Stop retained pre-stop cache')
        check(getattr(w,'_discord_queue_observation_inactive158',False),'True Stop not marked inactive')
        for _ in range(100):w._now_playing_queue_preview('discord')
        check(queue_calls()==initial_calls+1,'Inactive queue retried while stopped')
        update_start=len(evidence['ui_updates'])
        state['items']=[row(name,'Resumed fresh '+name) for name in 'BCDEFG']
        resumed=snapshot();observe(resumed)
        check(not getattr(w,'_discord_queue_observation_inactive158',False),'Confirmed resume did not reopen queue')
        check('discord' not in reader._cache,'Resume reused invalidated pre-stop cache')
        held['release'].set();wait_done()
        check('Resumed fresh F' in bubble['text'],'Resume did not fetch fresh same-identity queue')
        check(not any('Pre-stop pending' in item['after'] for item in evidence['ui_updates'][update_start:]),
            'Pre-stop pending result repainted during same-identity resume')
        check(queue_calls()==initial_calls+2,'Resume failed to coalesce invalidated old and fresh read')

    def case_non_authoritative_false(kind):
        event,bubble,held,observed=start_late()
        unknown={**observed,'current':{},'connected':False,'playing':False,'paused':False}
        if kind=='pending':unknown.update(shared_state_pending=True,verified=False)
        else:unknown.update(stale_shared_snapshot=True,verified=True)
        observe(unknown)
        trace(kind+'_false_snapshot_before_completion')
        check(not getattr(w,'_discord_queue_observation_inactive158',False),kind+' snapshot invented an authoritative stop')
        held['release'].set();wait_done()
        check('Late-only queue 5' in bubble['text'],kind+' false status discarded still-current queue')

    def case_terminal_event():
        event,bubble,held,observed=start_late()
        bus.publish_now_playing('discord',event.title,event.artist,event.track_id,
            reason='stopped',playing=False,observation=True,playback_generation=event.playback_generation,
            playback_scope=event.playback_scope,announce=False)
        w._drain_media_now_playing_events()
        check(getattr(w,'_discord_queue_observation_inactive158',False),'Terminal event did not mark queue inactive')
        held['release'].set();wait_done()
        check('Late-only' not in bubble['text'],'Explicit terminal event allowed late repaint')

    def case_queued_active_before_disconnect():
        event,bubble,held,observed=start_late()
        w._discord_observer_ready(deepcopy(observed))  # Intentionally queued, not yet drained.
        disconnected={**observed,'current':{},'connected':False,'playing':False,'paused':False,'verified':True}
        w._discord_observer_ready(disconnected)
        check(getattr(w,'_discord_queue_observation_inactive158',False),'Confirmed disconnect failed before backlog drain')
        trace('confirmed_disconnect_with_older_active_event_queued')
        w._drain_media_now_playing_events()
        trace('older_active_event_drained_after_disconnect')
        check(getattr(w,'_discord_queue_observation_inactive158',False),
            'Older queued active observation reopened queue after confirmed disconnect')
        held['release'].set();wait_done()
        check('Late-only' not in bubble['text'],'Queued old observation allowed late queue repaint after disconnect')


    def case_queued_terminal_before_new_start():
        event,bubble,held,observed=start_late()
        bus.publish_now_playing('discord',event.title,event.artist,event.track_id,
            reason='stopped',playing=False,observation=True,playback_generation=event.playback_generation,
            playback_scope=event.playback_scope,announce=False)
        state.update(current={**second,'_transport_generation':2,'generation':2},
            items=[row(name,'Newest start '+name) for name in 'CDEFG'],generation=2)
        physical.update(current=deepcopy(second),playback_generation=2)
        committed=snapshot();publish(committed)
        trace('older_terminal_then_new_start_before_drain')
        w._drain_media_now_playing_events()
        check(not getattr(w,'_discord_queue_observation_inactive158',False),
            'Older queued terminal event blocked newer physical start')
        held['release'].set();wait_done()
        check('Late-only' not in bubble['text'],'Old queue painted across terminal and newer start')
        check(len(w.bubbles)==2 and 'Newest start G' in w.bubbles[-1]['text'],
            'Newest physical start did not receive its fresh queue')

    tests=[('initial_slow_repeat',case_initial_slow),('automatic_next_slow_repeat',case_automatic_next),
        ('explicit_queue_reorder',case_explicit_reorder),('cache_ttl_no_busyloop',case_cache_ttl),
        ('loader_exception_and_retry',lambda:case_failure('loader_exception')),
        ('registry_failure_and_retry',lambda:case_failure('registry_failure')),
        ('late_result_after_next',lambda:case_late_transition('next')),
        ('late_result_after_source_epoch',lambda:case_late_transition('source_epoch')),
        ('late_result_after_guild_change',lambda:case_late_transition('guild')),
        ('pause_keeps_queue_readable',lambda:case_late_transition('pause')),
        ('late_result_after_stop',lambda:case_late_transition('stop')),
        ('late_result_after_explicit_disconnect',lambda:case_late_transition('disconnect')),
        ('unknown_empty_status_is_not_stop',lambda:case_late_transition('unknown')),
        ('late_result_after_registry_replacement',lambda:case_late_transition('registry')),
        ('stop_resume_requires_fresh_queue',case_stop_resume_fresh),
        ('pending_false_status_is_not_stop',lambda:case_non_authoritative_false('pending')),
        ('stale_false_status_is_not_stop',lambda:case_non_authoritative_false('stale')),
        ('explicit_terminal_event_rejects_late_queue',case_terminal_event),
        ('older_active_event_cannot_reopen_disconnected_queue',case_queued_active_before_disconnect),
        ('older_terminal_event_cannot_block_new_start',case_queued_terminal_before_new_start)]
    try:
        reset(case_name)
        dict(tests)[case_name]()
        return evidence
    except BaseException as error:
        error.queue_evidence = evidence
        raise
    finally:
        close_reader()
        ui.discord_session_meter,ui.note_media_observation=original_meter,original_observation
        queue_module.time=original_queue_time
        bus.drain_now_playing(1000)
        bus._LATEST_BY_SOURCE=previous_latest


CASE_NAMES = ('initial_slow_repeat', 'automatic_next_slow_repeat', 'explicit_queue_reorder', 'cache_ttl_no_busyloop', 'loader_exception_and_retry', 'registry_failure_and_retry', 'late_result_after_next', 'late_result_after_source_epoch', 'late_result_after_guild_change', 'pause_keeps_queue_readable', 'late_result_after_stop', 'late_result_after_explicit_disconnect', 'unknown_empty_status_is_not_stop', 'late_result_after_registry_replacement', 'stop_resume_requires_fresh_queue', 'pending_false_status_is_not_stop', 'stale_false_status_is_not_stop', 'explicit_terminal_event_rejects_late_queue', 'older_active_event_cannot_reopen_disconnected_queue', 'older_terminal_event_cannot_block_new_start')


def pytest_generate_tests(metafunc):
    if "queue_case" in metafunc.fixturenames:
        metafunc.parametrize("queue_case", CASE_NAMES)


def test_discord_queue_preview_lifecycle(queue_case, tmp_path):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    _run_case(app, tmp_path, queue_case)
