"""Deterministic rapid-switch failures; inert media only, no external I/O."""
from __future__ import annotations
import threading
import time
from types import SimpleNamespace
from pathlib import Path
import pytest
from jarvis.integrations import media_state as ms, youtube_audio as yt, discord_navigation as dn
from jarvis.integrations.yandex_music import YandexMusicAdapter
class Config:
    def __init__(self, root):
        self.paths=SimpleNamespace(root=root)
        self.values={}
    def get(self, key, default=None):
        return self.values.get(key, default)


def wait_until(predicate, timeout=2):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            pytest.fail('barrier was not reached')
        time.sleep(.002)


def test_latest_waiter_only_executes_and_concurrency_is_one():
    entered, release = threading.Event(), threading.Event()
    calls, results, active = [], {}, []
    class Owner:
        @ms.latest_transport
        def transition(self, name):
            active.append(name)
            assert len(active) == 1
            try:
                calls.append(name)
                if name == 'A':
                    entered.set()
                    assert release.wait(2)
                ms.transport_checkpoint(self)
                return {'name': name, 'verified': True}
            finally:
                active.remove(name)
    owner = Owner()
    def run(n): results[n] = owner.transition(n)
    a = threading.Thread(target=run, args=('A',)); a.start(); assert entered.wait(2)
    b = threading.Thread(target=run, args=('B',)); b.start()
    wait_until(lambda: owner._transport_request_serial == 2)
    c = threading.Thread(target=run, args=('C',)); c.start()
    wait_until(lambda: owner._transport_request_serial == 3)
    release.set()
    for t in (a,b,c): t.join(2); assert not t.is_alive()
    assert calls == ['A', 'C']
    assert results['A']['cancelled'] and results['B']['cancelled']
    assert results['C']['name'] == 'C' and not active


def test_captured_guard_rejects_old_request_on_other_thread():
    owner = SimpleNamespace(_transport_request_serial=1, _transport_request_local=threading.local())
    owner._transport_request_local.serial=1
    check = ms.transport_guard(owner)
    owner._transport_request_serial=2
    seen=[]
    def run():
        try: check()
        except ms.MediaRequestSuperseded: seen.append('cancelled')
    thread=threading.Thread(target=run); thread.start(); thread.join(2)
    assert seen == ['cancelled']


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(YandexMusicAdapter, 'clear_transient_cache_on_startup_once', lambda self: {})
    obj = dn.ReliableDiscordAdapter(Config(tmp_path))
    profile = {'guild_id':'123','name':'Test'}
    monkeypatch.setattr(obj, '_background_profile', lambda *a: profile)
    monkeypatch.setattr(obj, '_local_voice_is_connected', lambda *a: True)
    monkeypatch.setattr(obj, '_remote_for_any_profile', lambda *a: None)
    monkeypatch.setattr(obj, '_broadcast_queue_snapshot', lambda *a, **kw: None)
    monkeypatch.setattr(obj, '_start_queue_monitor', lambda *a: None)
    monkeypatch.setattr(obj, '_ensure_wave_queue_depth_background', lambda *a: None)
    monkeypatch.setattr(obj, '_prefetch_next_queue_item', lambda *a: None)
    monkeypatch.setattr(obj, '_wave_event', lambda *a: None)
    monkeypatch.setattr(obj, '_remember_wave_id', lambda *a, **kw: None)
    state = obj._queue_state(profile)
    monkeypatch.setattr(obj, '_local_queue_snapshot', lambda *a: {'current': obj._public_queue_entry(state.get('current') or {}), 'queue':list(state.get('items') or []), 'connected':True})
    return obj, state, profile


def test_stalled_youtube_skip_is_superseded_by_wave_and_late_finish_is_inert(adapter, tmp_path, monkeypatch):
    obj,state,profile=adapter
    audio=tmp_path/'silent.wav';audio.write_bytes(b'RIFF'+b'\0'*40)
    old={'title':'Current','track_id':'current','query':'Current'}
    entry={'title':'Old YouTube','track_id':'old','query':'Old YouTube', '_source_type':'youtube','_source_url':'https://www.youtube.com/watch?v=synthetic01'}
    state.update(current=old,items=[entry],history=[])
    pending=[]; starts=[]; stops=[]
    monkeypatch.setattr(dn,'PREFETCH',SimpleNamespace(submit=lambda key,fn: pending.append(fn) or True))
    monkeypatch.setattr(obj._youtube,'download_audio',lambda url:{'local_path':str(audio),'title':'Old YouTube'})
    def play(*a,**kw):
        kw['before_start']()
        starts.append(kw['title'])
        return {'verified':True,'submitted':True,'playback_generation':len(starts),'title':kw['title']}
    bot=SimpleNamespace(play_url=play, voice_action=lambda *a,**kw:stops.append(a[1]),playback_status=lambda *a:{'connected':True})
    monkeypatch.setattr(obj,'_background_for_profile',lambda *a:bot)
    monkeypatch.setattr(obj._yandex,'wave_tracks',lambda **kw:{})
    monkeypatch.setattr(obj,'_wave_entries',lambda *a,**kw:[{'title':'Wave','query':'Wave','track_id':'wave','_prepared_streams':[{'source':str(audio),'source_method':'cached_local_file','title':'Wave'}]}])
    result={}
    thread=threading.Thread(target=lambda:result.update(obj.music_skip()))
    thread.start(); wait_until(lambda: bool(pending))
    newest=obj.music_my_wave()
    thread.join(2);assert not thread.is_alive()
    assert result['cancelled'] and not result['verified']
    assert newest['verified'] and state['current']['title']=='Wave'
    assert starts==['Wave'] and stops==[]
    for job in pending: job()
    assert starts==['Wave'] and state['current']['title']=='Wave'


def test_prefetch_failure_does_not_stop_current_and_explicit_wave_recovers(adapter, monkeypatch):
    obj,state,profile=adapter
    old={'title':'Current','track_id':'current','query':'Current'}
    item={'title':'Broken','track_id':'bad','query':'Broken','_source_type':'youtube','_source_url':'https://www.youtube.com/watch?v=synthetic02'}
    state.update(current=old,items=[item],history=[])
    stopped=[]
    monkeypatch.setattr(obj,'_background_for_profile',lambda *a:SimpleNamespace(voice_action=lambda *a,**k:stopped.append(a)))
    def fail(url): raise ConnectionError('synthetic failure')
    monkeypatch.setattr(obj._youtube,'download_audio',fail)
    monkeypatch.setattr(dn,'PREFETCH',SimpleNamespace(submit=lambda key,job: (job(),True)[1]))
    with pytest.raises(ConnectionError):obj.music_skip()
    assert state['current']['track_id']=='current' and state['items'][0]['track_id']=='bad'
    assert state['transport_faulted'] and not stopped
    state['source_mode']='wave'
    monkeypatch.setattr(obj,'_local_queue_snapshot',lambda *a:{'playing':True,'current':old})
    # Explicit Wave request is allowed after the failed call; no stale transition lock.
    assert obj.music_my_wave()['unchanged']


def test_two_prefetch_workers_and_bounded_backlog(monkeypatch):
    created=[]
    class DormantThread:
        def __init__(self,**kw):created.append(kw)
        def start(self):pass
    monkeypatch.setattr(ms.threading,'Thread',DormantThread)
    prefetch=ms.BoundedPrefetch()
    accepted=[prefetch.submit(str(i),lambda:None) for i in range(1000)]
    assert sum(accepted)==20 and prefetch._jobs.qsize()==20
    assert len(prefetch._keys)==20 and len(created)==2
    assert prefetch.submit('0',lambda:None) and len(created)==2


def test_youtube_failed_prefetch_and_foreground_share_cooldown(tmp_path,monkeypatch):
    resolver=yt.YouTubeAudioResolver(Config(tmp_path));calls=[];now=[10.0]
    monkeypatch.setattr(yt.time,'monotonic',lambda:now[0])
    monkeypatch.setattr(yt,'_DOWNLOAD_FAILURES',yt.OrderedDict())
    def fail(url):calls.append(url);raise ConnectionError('synthetic 403')
    monkeypatch.setattr(resolver,'_download_audio_bounded',fail)
    url='https://www.youtube.com/watch?v=synthetic03'
    with pytest.raises(ConnectionError):resolver.download_audio(url)
    with pytest.raises(ConnectionError,match='15'):resolver.download_audio(url)
    assert len(calls)==1
    now[0]+=16
    monkeypatch.setattr(resolver,'_download_audio_bounded',lambda url:{'local_path':'synthetic'})
    assert resolver.download_audio(url)['local_path']=='synthetic'
    assert not yt._DOWNLOAD_FAILURES


def test_cache_lock_wait_is_bounded(tmp_path,monkeypatch):
    calls=[]
    class BusyLock:
        def acquire(self,**kw):calls.append(kw);return False
    monkeypatch.setattr(yt,'cache_lock',lambda key:BusyLock())
    resolver=yt.YouTubeAudioResolver(Config(tmp_path))
    with pytest.raises(TimeoutError):resolver.download_audio('https://www.youtube.com/watch?v=synthetic04')
    assert calls==[{'timeout':20.0}]


def test_partial_cleanup_never_deletes_parallel_track(tmp_path):
    own=tmp_path/'own.part';other=tmp_path/'other.part';own.write_bytes(b'1');other.write_bytes(b'2')
    yt.YouTubeAudioResolver._purge_partial_downloads(tmp_path,'own')
    assert not own.exists() and other.read_bytes()==b'2'


def test_voice_empty_invalidates_preparation(adapter):
    obj,state,profile=adapter
    state['manual_action_serial']=3;state['source_epoch']=7
    obj._handle_music_panel_action(profile,'voice_empty')
    assert state['manual_action_serial']==4 and state['source_epoch']==8
    assert obj._transport_request_serial==1


def test_cancelled_panel_never_claims_switched(adapter,monkeypatch):
    obj,state,profile=adapter
    monkeypatch.setattr(obj,'music_skip',lambda *a:{'verified':False,'cancelled':True})
    result=obj._handle_music_panel_action(profile,'next')
    assert not result['verified'] and 'Переключил' not in result['message']


def test_request_hook_does_not_hold_global_session_lock():
    acquired=threading.Event();threads=[]
    class Owner:
        def _supersede_music_request(self):
            def probe():
                with ms._STATE_LOCK:acquired.set()
            thread=threading.Thread(target=probe);threads.append(thread);thread.start()
            assert acquired.wait(1), 'coordinator inverted queue/session lock order'
        @ms.latest_transport
        def start(self):return True
    assert Owner().start()
    for thread in threads:thread.join(1);assert not thread.is_alive()


def test_actual_prefetch_workers_remain_two_and_jobs_drain():
    prefetch=ms.BoundedPrefetch();release=threading.Event();guard=threading.Lock()
    entered=[];peak=[0];active=[0];finished=[]
    def job():
        with guard:
            active[0]+=1;peak[0]=max(peak[0],active[0]);entered.append(threading.current_thread())
        assert release.wait(3)
        with guard:active[0]-=1;finished.append(1)
    assert prefetch.submit('first',job) and prefetch.submit('second',job)
    wait_until(lambda:len(entered)==2)
    accepted=sum(prefetch.submit('queued-'+str(i),job) for i in range(1000))
    assert accepted==20 and prefetch._jobs.qsize()==20 and len(prefetch._keys)==22
    release.set();wait_until(lambda:prefetch._jobs.unfinished_tasks==0)
    assert peak==[2] and len(finished)==22 and not prefetch._keys
    assert len(set(entered))==2 and all(thread.daemon for thread in entered)


def test_evicted_successful_youtube_cache_is_prepared_again(adapter,tmp_path,monkeypatch):
    obj,_,_=adapter;audio=tmp_path/'fresh.wav';audio.write_bytes(b'RIFF'+b'\0'*40)
    event=threading.Event();event.set()
    entry={'_source_type':'youtube','_source_url':'https://www.youtube.com/watch?v=synthetic05',
           '_youtube_prefetch_started':True,'_youtube_prefetch_event':event,
           '_youtube_cached_path':str(tmp_path/'removed.wav')}
    calls=[]
    monkeypatch.setattr(obj._youtube,'download_audio',lambda url:calls.append(url) or {'local_path':str(audio)})
    monkeypatch.setattr(dn,'PREFETCH',SimpleNamespace(submit=lambda key,job:(job(),True)[1]))
    obj._prefetch_queue_entry(entry)
    assert len(calls)==1 and entry['_youtube_cached_path']==str(audio)


def test_cancelled_download_does_not_poison_failure_cache(tmp_path,monkeypatch):
    resolver=yt.YouTubeAudioResolver(Config(tmp_path))
    monkeypatch.setattr(yt,'_DOWNLOAD_FAILURES',yt.OrderedDict())
    def cancelled():raise ms.MediaRequestSuperseded('newer')
    with pytest.raises(ms.MediaRequestSuperseded):
        resolver.download_audio('https://www.youtube.com/watch?v=synthetic06',check_cancelled=cancelled)
    assert not yt._DOWNLOAD_FAILURES


def test_two_stalled_downloads_do_not_block_prepared_wave(adapter,tmp_path,monkeypatch):
    obj,state,_=adapter;audio=tmp_path/'prepared.wav';audio.write_bytes(b'RIFF'+b'\0'*40)
    pool=ms.BoundedPrefetch();release=threading.Event();started=[];heard=[]
    def blocked():
        started.append(1);assert release.wait(3)
    pool.submit('stalled-a',blocked);pool.submit('stalled-b',blocked)
    wait_until(lambda:len(started)==2)
    monkeypatch.setattr(dn,'PREFETCH',pool)
    monkeypatch.setattr(obj._yandex,'wave_tracks',lambda **kw:{})
    monkeypatch.setattr(obj,'_wave_entries',lambda *a,**kw:[{'title':'Wave','track_id':'wave','query':'Wave',
        '_prepared_streams':[{'source':str(audio),'title':'Wave','source_method':'cached_local_file'}]}])
    def play(*a,**kw):kw['before_start']();heard.append(kw['title']);return {'verified':True,'submitted':True,'title':kw['title']}
    monkeypatch.setattr(obj,'_background_for_profile',lambda *a:SimpleNamespace(play_url=play))
    try:
        result=obj.music_my_wave()
        assert result['verified'] and heard==['Wave'] and not release.is_set()
    finally:
        release.set();wait_until(lambda:pool._jobs.unfinished_tasks==0)
    assert state['current']['track_id']=='wave' and not pool._keys


def test_cancelled_discord_tool_does_not_query_or_announce_success(tmp_path):
    from jarvis.tools.domains.browser_media import register_browser_media_tools
    registered={};calls=[]
    registry=SimpleNamespace(register=lambda definition:registered.setdefault(definition.name,definition))
    def execute(action,**kw):calls.append(action);return {'verified':False,'cancelled':True,'superseded':True}
    fake=SimpleNamespace(execute=execute)
    register_browser_media_tools(registry,config=Config(tmp_path),yandex_adapter=fake,spotify_adapter=fake,youtube_adapter=fake,discord_adapter=fake)
    result=registered['discord'].handler({'action':'music_skip'})
    assert not result.success and result.data['cancelled']
    assert calls==['music_skip'] and 'Переключил' not in result.text


@pytest.mark.parametrize('mode',['personal','automatic'])
def test_cancelled_preparation_does_not_leave_starting_when_new_lookup_fails(adapter,monkeypatch,mode):
    obj,state,profile=adapter
    entry={'title':'Preparing','track_id':'preparing','query':'Preparing'}
    monkeypatch.setattr(obj,'_prefetch_queue_entry',lambda *a:None)
    def cancelled(*a,**kw):raise dn.DiscordPlaybackCancelled('replaced by newer lookup')
    monkeypatch.setattr(obj,'_play_queue_entry',cancelled)
    if mode=='personal':
        with pytest.raises(dn.DiscordPlaybackCancelled):
            obj._begin_personal_source(profile,[entry],mode='wave',title='Wave')
    else:
        state.update(current=None,items=[entry])
        obj._advance_queue(obj._profile_key(profile))
    # The replacement provider then fails before committing a source. The old
    # placeholder must already be gone, with the track still explicitly retryable.
    assert state['current'] is None and state['transport_faulted']
    assert [x['track_id'] for x in state['items']]==['preparing']


from ._neo184_queue_rollback_cases import CASES as ROLLBACK_CASES, exercise_case as exercise_rollback_case


@pytest.mark.parametrize('spec', ROLLBACK_CASES, ids=[case[0] for case in ROLLBACK_CASES])
def test_failed_switch_preserves_acknowledged_queue_edits(adapter, monkeypatch, spec):
    obj, state, profile = adapter
    report = exercise_rollback_case(dn, obj, state, profile, monkeypatch.setattr, spec)
    assert report['status'] == 'PASS', report


from ._neo184_pause_rollback_cases import CASES as PAUSE_CASES, exercise_case as exercise_pause_case


@pytest.mark.parametrize('spec', PAUSE_CASES, ids=[case[0] for case in PAUSE_CASES])
def test_failed_switch_preserves_pause_owner(adapter, monkeypatch, spec):
    obj, state, profile = adapter
    report = exercise_pause_case(dn, obj, state, profile, monkeypatch.setattr, spec)
    assert report['status'] == 'PASS', report


@pytest.mark.parametrize('operation', ['skip', 'previous'])
def test_two_public_failed_switches_restore_pause_before_newer_snapshot(adapter, monkeypatch, operation):
    """Actual public wrappers; Events prove ordering, with bounded cleanup."""
    obj, state, profile = adapter
    current = {'title': 'A', 'track_id': 'A', 'query': 'A', 'generation': 7,
               'started_at': 1.0, '_paused_seconds': 3.25}
    target = {'title': 'B', 'track_id': 'B', 'query': 'B', '_source_type': 'youtube',
              '_source_url': 'https://www.youtube.com/watch?v=syntheticB1'}
    second = {'title': 'C', 'track_id': 'C', 'query': 'C'}
    history = [{'title': 'H', 'track_id': 'H', 'query': 'H'}]
    if operation == 'previous':
        history.append(dict(target))
    state.update(current=current, items=[target, second], history=list(history), generation=7,
                 source_mode='playlist', source_title='Synthetic playlist',
                 source_data={'synthetic': True}, manual_paused=False, manual_action_serial=0)
    controls, downloads, method_snapshots, hook_serials = [], [], [], []
    entered, release, newer_header = threading.Event(), threading.Event(), threading.Event()
    results, failures = {}, {}

    def diagnostic(assertion):
        return {'assertion': assertion, 'operation': operation, 'controls': controls,
                'downloads': downloads, 'method_snapshots': method_snapshots,
                'hook_serials': hook_serials, 'results': results,
                'failures': {key: (type(exc).__name__, str(exc)) for key, exc in failures.items()},
                'first_preparation_entered': entered.is_set(), 'released': release.is_set(),
                'newer_header_seen': newer_header.is_set(),
                'manual_paused': state.get('manual_paused'),
                'manual_action_serial': state.get('manual_action_serial'),
                'pause_control_serial': state.get('pause_control_serial', 0),
                'request_serial': getattr(obj, '_transport_request_serial', 0),
                'current': dict(state.get('current') or {})}

    def voice_action(received_profile, action, **kwargs):
        controls.append(action)
        assert received_profile == profile and controls == ['pause'], diagnostic("received_profile == profile and controls == ['pause']")
        return {'verified': True, 'synthetic': True, 'paused': True}

    def forbidden_play(*args, **kwargs):
        raise BaseException('No playback edge is permitted in the chained-failure test')

    monkeypatch.setattr(obj, '_background_for_profile', lambda *a: SimpleNamespace(
        voice_action=voice_action, play_url=forbidden_play))
    seeded = obj.execute('music_pause', server=profile['name'])
    assert seeded['verified'] and state['manual_paused'] is True, diagnostic("seeded['verified'] and state['manual_paused'] is True")
    original_current = dict(current)
    initial_manual = state['manual_action_serial']
    initial_request = obj._transport_request_serial
    initial_pause_control = state['pause_control_serial']

    def profile_at_method_entry(*args):
        with obj._queue_lock:
            method_snapshots.append((state['manual_paused'], state['manual_action_serial']))
        return profile

    original_hook = obj._supersede_music_request
    def supersede():
        original_hook()
        hook_serials.append(state['manual_action_serial'])
        if len(hook_serials) == 2:
            newer_header.set()

    def download(url):
        downloads.append(url)
        if len(downloads) == 1:
            entered.set()
            assert release.wait(2), diagnostic('release.wait(2)')
        raise ConnectionError('Synthetic chained preparation failure')

    monkeypatch.setattr(obj, '_background_profile', profile_at_method_entry)
    monkeypatch.setattr(obj, '_supersede_music_request', supersede)
    monkeypatch.setattr(obj._youtube, 'download_audio', download)
    monkeypatch.setattr(dn, 'PREFETCH', SimpleNamespace(submit=lambda key, job: (job(), True)[1]))

    def request(label):
        try:
            results[label] = obj.execute('music_' + operation, server=profile['name'])
        except BaseException as exc:
            failures[label] = exc

    first_thread = threading.Thread(target=request, args=('first',), name='NB13 first switch')
    second_thread = threading.Thread(target=request, args=('second',), name='NB13 newer switch')
    started = []
    try:
        first_thread.start(); started.append(first_thread)
        assert entered.wait(2), diagnostic('entered.wait(2)')
        second_thread.start(); started.append(second_thread)
        assert newer_header.wait(2), diagnostic('newer_header.wait(2)')
        # The second method cannot snapshot while the first owns the transition
        # lock. This is the actual serialized wrapper path, not a staged header.
        assert method_snapshots == [(True, initial_manual + 1)], diagnostic('method_snapshots == [(True, initial_manual + 1)]')
        release.set()
    finally:
        release.set()
        for thread in started:
            thread.join(3)
    import json
    print(json.dumps({'record': 'CONCURRENT_WRAPPER_OBSERVATION', 'operation': operation,
        'actual_concurrent_wrappers': True, 'download_callbacks': len(downloads),
        'manual_paused_after': state.get('manual_paused'),
        'manual_serial_after': state.get('manual_action_serial'),
        'request_serial_after': obj._transport_request_serial,
        'pause_control_serial_after': state.get('pause_control_serial', 0),
        'method_entry_snapshots': method_snapshots,
        'threads_alive': [thread.is_alive() for thread in started],
        'first_result': results.get('first'),
        'second_error_type': type(failures.get('second')).__name__}, sort_keys=True), flush=True)
    assert all(not thread.is_alive() for thread in started), diagnostic('all((not thread.is_alive() for thread in started))')
    assert results.get('first', {}).get('cancelled') and not results['first']['verified'], diagnostic("results.get('first', {}).get('cancelled') and (not results['first']['verified'])")
    assert isinstance(failures.get('second'), ConnectionError), diagnostic("isinstance(failures.get('second'), ConnectionError)")
    assert set(failures) == {'second'} and set(results) == {'first'}, diagnostic("set(failures) == {'second'} and set(results) == {'first'}")
    assert method_snapshots == [(True, initial_manual + 1), (True, initial_manual + 2)], diagnostic('method_snapshots == [(True, initial_manual + 1), (True, initial_manual + 2)]')
    assert hook_serials == [initial_manual + 1, initial_manual + 2], diagnostic('hook_serials == [initial_manual + 1, initial_manual + 2]')
    assert state['manual_paused'] is True and state['current'] == original_current, diagnostic("state['manual_paused'] is True and state['current'] == original_current")
    assert state['manual_action_serial'] == initial_manual + 2, diagnostic("state['manual_action_serial'] == initial_manual + 2")
    assert obj._transport_request_serial == initial_request + 2, diagnostic('obj._transport_request_serial == initial_request + 2')
    assert state['pause_control_serial'] == initial_pause_control, diagnostic("state['pause_control_serial'] == initial_pause_control")
    assert len(state['items']) == 2 and state['items'][0] is target and state['items'][1] is second, diagnostic("len(state['items']) == 2 and state['items'][0] is target and (state['items'][1] is second)")
    assert state['history'] == history and state['transport_faulted'] is True, diagnostic("state['history'] == history and state['transport_faulted'] is True")
    assert controls == ['pause'] and len(downloads) == 2, diagnostic("controls == ['pause'] and len(downloads) == 2")
