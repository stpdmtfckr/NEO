"""Transport/state regressions. Fake API/voice/socket edges, real adapter logic."""
import asyncio
import json
from types import SimpleNamespace
import threading
import time
import pytest

from tests.test_media_reliability import Config, Track, local_adapter
from jarvis.integrations import yandex_music as ym
from jarvis.integrations.background.local_player import BackgroundLocalPlayer
from jarvis.integrations.background.discord_bot import DiscordBackgroundBot
from jarvis.runtime.deferred_effects import DeferredEffects
from jarvis.runtime.wave_history import merge_entries, history_lock
from jarvis.runtime.desktop_services import is_vpn_interface, NetworkState
from jarvis.runtime.media_events import publish_now_playing, drain_now_playing
from jarvis.runtime.localized_text import render_action_result
from jarvis.runtime.natural_commands import LANGUAGES


def wait_until(predicate,seconds=2):
    deadline=time.monotonic()+seconds
    while not predicate() and time.monotonic()<deadline:time.sleep(.002)
    assert predicate()


def test_feedback_lane_never_waits_for_http_and_keeps_order():
    entered,release=threading.Event(),threading.Event();sent=[]
    def send(value):
        entered.set();release.wait(2);sent.append(value['n'])
    lane=DeferredEffects(send,capacity=3,idle_seconds=.1)
    assert lane._worker is None
    began=time.monotonic();lane.submit({'n':1});assert time.monotonic()-began<.2
    assert entered.wait(1)
    for n in range(2,7):lane.submit({'n':n})
    release.set();wait_until(lambda:lane.sent==4)
    assert sent==[1,4,5,6] and lane.dropped==2
    wait_until(lambda:lane._worker is None)
    lane.close();assert not lane.submit({'n':7})


def test_history_merge_is_bounded_and_uses_fingerprint_not_only_track_id():
    rows=merge_entries([{'id':'album1','fingerprint':'artist/song','ts':10},
                        {'id':'album2','fingerprint':'artist/song','ts':11},
                        {'id':'future','fingerprint':'other','ts':1e20},
                        {'id':'invalid','ts':float('nan')}],now=20)
    assert {x['id'] for x in rows}=={'album2','future'}
    assert rows[-1]['ts']==20
    assert len(merge_entries([{'id':str(i),'ts':20} for i in range(4000)],now=20))==2000


def test_cross_adapter_history_is_merged_not_overwritten(local_adapter):
    first,_=local_adapter
    second=ym.YandexMusicAdapter(first.config)
    first.remember_wave_track(Track('A'));second.remember_wave_track(Track('B'))
    ids,_=first._wave_recent_sets()
    assert {'A','B'}<=ids
    ids,_=second._wave_repeat_floor()
    assert {'A','B'}<=ids


def test_wave_final_continuity_keeps_recent_floor(local_adapter,monkeypatch):
    adapter,_=local_adapter
    repeated=Track('A');alternate=Track('duplicate_id');alternate.title=repeated.title
    adapter.remember_wave_track(repeated)
    monkeypatch.setattr(adapter,'_api_client',lambda:object())
    monkeypatch.setattr(adapter,'_station_tracks',lambda *a,**k:([repeated,alternate,Track('B')],'batch'))
    result=adapter.wave_tracks(fast_start=True)
    assert [ym._track_id(x) for x in result['tracks']]==['B']
    assert result['repeat_floor_enforced']


def test_wave_direct_start_fallback_cannot_repeat_recent(local_adapter,monkeypatch):
    adapter,_=local_adapter;adapter.config.values['integrations.yandex_music.enabled']=True
    adapter.remember_wave_track(Track('A'))
    monkeypatch.setattr(adapter,'_api_client',lambda:object())
    monkeypatch.setattr(adapter,'wave_tracks',lambda **_:(_ for _ in ()).throw(RuntimeError('main branch failed')))
    monkeypatch.setattr(adapter,'_station_tracks',lambda *a,**k:([Track('A'),Track('B')],'new-batch'))
    monkeypatch.setattr(adapter,'_ensure_wave_queue_depth',lambda *_:None)
    monkeypatch.setattr(ym.time,'sleep',lambda *_:None)
    result=adapter.my_wave()
    assert result['title']=='B' and result['verified']


def test_batch_origin_not_replaced_by_refill(local_adapter,monkeypatch):
    adapter,_=local_adapter
    a,b=Track('A'),Track('B')
    client=SimpleNamespace(rotor_station_tracks=lambda *args,**kwargs:SimpleNamespace(sequence=[SimpleNamespace(track=b)],batch_id='batch-B'))
    monkeypatch.setattr(adapter,'_api_client',lambda:client)
    adapter._station_tracks('user:onyourwave')
    adapter._station_batch_id='batch-A'
    assert adapter._batch_for_track(b,'user:onyourwave','wrong')=='batch-B'
    assert adapter._station_batch_id=='batch-A'
    stable=adapter._stable_track(b)
    assert adapter._batch_for_track(stable,'user:onyourwave','wrong')=='batch-B'


def test_late_wave_refill_cannot_overwrite_new_station(local_adapter,monkeypatch):
    adapter,_=local_adapter
    entered,release=threading.Event(),threading.Event()
    def delayed(**kw):
        entered.set();release.wait(2)
        return {'tracks':[Track(c) for c in 'BCDEFGHI'],'batch_id':'old'}
    adapter._station='user:onyourwave';adapter._current_track_obj=Track('A');adapter._station_queue=[]
    monkeypatch.setattr(adapter,'wave_tracks',delayed)
    adapter._ensure_wave_queue_depth(5)
    assert entered.wait(1)
    with adapter._playback_lock:
        adapter._wave_queue_generation+=1
        adapter._station_queue=[Track('Z')]
    release.set();wait_until(lambda:not adapter._wave_refill_active)
    assert [ym._track_id(t) for t in adapter._station_queue]==['Z']


def test_coalesced_next_does_not_claim_verified_playback(local_adapter):
    adapter,_=local_adapter
    adapter._advance_lock.acquire()
    try:
        result=adapter._next_background()
        assert result['starting'] and result['requested'] and result['coalesced']
        assert not result['verified']
    finally:adapter._advance_lock.release()


def test_cached_player_status_never_blocks_gui_or_starts_player(monkeypatch):
    player=BackgroundLocalPlayer();entered,release=threading.Event(),threading.Event()
    monkeypatch.setattr(player,'_alive',lambda:True)
    player._status_cache={'state':'playing','title':'A'};player._status_at=time.monotonic()
    calls=[]
    def delayed(*args,**kwargs):
        calls.append(kwargs);entered.set();release.wait(2);return {'state':'playing'}
    monkeypatch.setattr(player,'_request',delayed)
    began=time.monotonic();snapshot=player.cached_status()
    assert time.monotonic()-began<.2 and snapshot['state']=='playing'
    assert entered.wait(1)
    for _ in range(20):player.cached_status()
    assert len(calls)==1 and calls[0]['ensure'] is False
    release.set();wait_until(lambda:not player._status_inflight)
    monkeypatch.setattr(player,'_alive',lambda:False)
    assert player.cached_status()['state']=='stopped'


def test_old_status_response_cannot_overwrite_a_new_stop(monkeypatch):
    import jarvis.integrations.background.local_player as lp
    player=BackgroundLocalPlayer();entered,release=threading.Event(),threading.Event()
    monkeypatch.setattr(player,'_alive',lambda:True)
    class Socket:
        def __enter__(self):return self
        def __exit__(self,*_):return False
        def settimeout(self,*_):pass
        def sendall(self,payload):self.action=json.loads(payload)['action']
        def makefile(self,*_):return self
        def readline(self,*_):
            if self.action=='status':entered.set();release.wait(2)
            return json.dumps({'ok':True,'state':'playing' if self.action=='status' else 'stopped'}).encode()
    monkeypatch.setattr(lp.socket,'create_connection',lambda *a,**kw:Socket())
    thread=threading.Thread(target=lambda:player._request({'action':'status'},ensure=False));thread.start()
    assert entered.wait(1)
    player._request({'action':'stop'},ensure=False)
    release.set();thread.join(2)
    assert player._status_cache['state']=='stopped'


@pytest.mark.parametrize('name',['Tunneled Tunneling Pseudo-Interface','Teredo Tunneling Pseudo-Interface','isatap.{abc}','6to4','Loopback Pseudo-Interface 1','Ethernet','Wi-Fi'])
def test_generic_windows_interfaces_are_not_vpn(name):assert not is_vpn_interface(name)

@pytest.mark.parametrize('name',['WireGuard Tunnel','OpenVPN','Tailscale','tun0','wg0','Wintun','My VPN'])
def test_explicit_vpn_names(name):assert is_vpn_interface(name)


def test_network_dedup_debounce_and_vpn_notice_rate():
    state=NetworkState();a=(('Ethernet','ip'),);b=(*a,('WireGuard','vpn'))
    assert state.observe(a,set(),None,100)==[]
    for _ in range(10):
        assert state.observe(b,{'WireGuard'},None,100)==[]
        assert state.observe(a,set(),None,100)==[]
    assert state.observe(b,{'WireGuard'},None,100)==[]
    events=state.observe(b,{'WireGuard'},None,100)
    assert sum(e.get('message_key')=='network.vpn_connected' for e in events)==1
    for _ in range(40):assert state.observe(b,{'WireGuard'},None,100)==[]
    state.observe(a,set(),None,100);state.observe(a,set(),None,100)
    state.observe(b,{'WireGuard'},None,100)
    events=state.observe(b,{'WireGuard'},None,100)
    assert any(e['type']=='network_changed' for e in events)
    assert not any(e.get('message_key')=='network.vpn_connected' for e in events)


def test_terminal_media_state_with_empty_title_is_not_dropped():
    drain_now_playing(1000)
    publish_now_playing('discord','','','',reason='owner_left',playing=False)
    rows=drain_now_playing()
    assert len(rows)==1 and rows[0].playing is False and rows[0].reason=='owner_left'
    publish_now_playing('discord','','','',reason='track_started',playing=True)
    assert drain_now_playing()==[]


@pytest.mark.parametrize('code',LANGUAGES)
def test_pause_result_is_sentence_without_old_now_playing_title(code):
    text=render_action_result('yandex_music',{'action':'pause'},True,'Пауза Яндекс Музик',
                              {'content_provider':'yandex_music','verified':True,'state':'paused','title':'OLDTRACK'},code)
    assert 'OLDTRACK' not in text and len(text.split())>=4
    assert text!='Пауза Яндекс Музик'


def voice_fixture(tmp_path):
    bot=DiscordBackgroundBot(Config(tmp_path))
    bot._client=SimpleNamespace(user=SimpleNamespace(id=999))
    owner=SimpleNamespace(id=11,bot=False);guest=SimpleNamespace(id=22,bot=False)
    channel=SimpleNamespace(id=33,members=[guest],voice_states={22:object()})
    calls=[]
    class Voice:
        def __init__(self):self.channel=channel
        def is_playing(self):return True
        def is_paused(self):return False
        def stop(self):calls.append('stop')
        async def disconnect(self,**kwargs):calls.append('disconnect')
    guild=SimpleNamespace(id=44,name='test',voice_client=Voice())
    owner.guild=guest.guild=guild
    bot._bind_voice_owner(guild,{'discord_user_id':'11'})
    async def no_dashboard(*_):pass
    bot._remove_music_dashboard=no_dashboard
    return bot,owner,guest,channel,guild,calls


def test_owner_leave_with_guests_present(tmp_path):
    async def scenario():
        bot,owner,guest,channel,guild,calls=voice_fixture(tmp_path)
        await bot._handle_voice_state_update(owner,SimpleNamespace(channel=channel),SimpleNamespace(channel=None))
        task=bot._empty_voice_tasks[guild.id]
        await task
        assert calls==['stop','disconnect']
    asyncio.run(scenario())


def test_owner_return_cancels_pending_leave_mute_does_not_restart_it(tmp_path):
    async def scenario():
        bot,owner,guest,channel,guild,calls=voice_fixture(tmp_path)
        await bot._handle_voice_state_update(owner,SimpleNamespace(channel=channel),SimpleNamespace(channel=None))
        old=bot._empty_voice_tasks[guild.id]
        await bot._handle_voice_state_update(guest,SimpleNamespace(channel=channel),SimpleNamespace(channel=channel))
        assert bot._empty_voice_tasks[guild.id] is old
        channel.members.append(owner);channel.voice_states[owner.id]=object()
        await bot._handle_voice_state_update(owner,SimpleNamespace(channel=None),SimpleNamespace(channel=channel))
        await bot._empty_voice_tasks[guild.id]
        assert calls==[]
    asyncio.run(scenario())


def test_clearing_explicit_owner_does_not_retain_old_identity(tmp_path):
    bot,owner,guest,channel,guild,calls=voice_fixture(tmp_path)
    assert bot._voice_owner_ids[guild.id]==11
    bot._bind_voice_owner(guild,{'discord_user_id':''})
    assert guild.id not in bot._voice_owner_ids


@pytest.mark.parametrize('code',LANGUAGES)
def test_paused_status_does_not_claim_a_new_pause_action(code):
    from jarvis.runtime.localized_text import _render_media_result
    data={'content_provider':'yandex_music','state':'paused','title':'Old track','verified':True}
    status=_render_media_result('yandex_music','status',True,data,code)
    action=_render_media_result('yandex_music','pause',True,data,code)
    assert status!=action and 'Old track' not in status


def test_wave_event_requires_observed_playing_state():
    import inspect
    source=inspect.getsource(ym.YandexMusicAdapter._play_track_object)
    assert 'playing=str(played.get("state", "") or "").casefold() == "playing" and not bool(played.get("starting"))' in source


def test_quick_ack_quotes_actual_accepted_phrase_separate_from_canonical():
    from jarvis.quick_media_commands import QuickMediaCommand
    from jarvis.ui.quick_media_messages import acknowledgement
    command=QuickMediaCommand("previous","auto","предыдущий",recognized="назад")
    reply=acknowledgement(command.action,command.recognized or command.heard,"ru")
    assert '«назад»' in reply and 'предыдущий трек' in reply
