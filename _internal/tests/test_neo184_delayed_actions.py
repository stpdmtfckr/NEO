"""Delayed actions: inert real-Agent routes, fake clocks, fail-closed live targets."""
import asyncio
import copy
import json
import os
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from jarvis.runtime import delayed_actions184 as delayed
from jarvis.runtime import timers
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution


@pytest.fixture(autouse=True)
def no_external_io():
    with external_io_blocked(), patch.object(os, 'system', side_effect=AssertionError('no shell')), patch.object(socket.socket, 'sendto', side_effect=AssertionError('no UDP')):
        yield


PHRASES = {
 'ru': ('выключи компьютер через 10минут', 'отключи пк через десять минут', 'останови видео через 10 минут', 'поставь на паузу видео через 10 минут', 'закрой браузер через 10 минут', 'закрой окно браузера через 10 минут'),
 'en': ('shut down computer in10minutes', 'turn off the pc after ten minutes', 'stop video in10minutes', 'pause the video in 10 minutes', 'close browser in10minutes', 'quit the browser after 10 minutes'),
 'de': ('fahre den Computer in 10 Minuten herunter', 'schalte den Computer aus in zehn Minuten', 'stoppe das Video in 10 Minuten', 'pausiere das Video nach zehn Minuten', 'schließe den Browser in 10 Minuten', 'beende den Browser nach zehn Minuten'),
 'fr': ("éteins l’ordinateur dans 10 minutes", 'arrête le pc après dix minutes', 'arrête la vidéo dans 10 minutes', 'mets en pause la vidéo après dix minutes', 'ferme le navigateur dans 10 minutes', 'quitte le navigateur après dix minutes'),
 'es': ('apaga el ordenador en 10 minutos', 'desconecta la computadora dentro de diez minutos', 'detén el vídeo en 10 minutos', 'pausa el video dentro de diez minutos', 'cierra el navegador en 10 minutos', 'cierra la ventana del navegador dentro de diez minutos'),
 'it': ('spegni il computer tra 10 minuti', 'arresta il pc fra dieci minuti', 'ferma il video tra 10 minuti', 'metti in pausa il video fra dieci minuti', 'chiudi il browser tra 10 minuti', 'termina il browser fra dieci minuti'),
 'pt': ('desliga o computador em 10 minutos', 'desligue o pc daqui a dez minutos', 'para o vídeo em 10 minutos', 'pause o vídeo daqui a dez minutos', 'fecha o navegador em 10 minutos', 'feche a janela do navegador daqui a dez minutos'),
}

@pytest.mark.parametrize('code,text,index', [(c,t,i) for c,rows in PHRASES.items() for i,t in enumerate(rows)])
def test_multilingual_complete_grammar(code,text,index):
    assert delayed.parse(text,code) == {'operation':'schedule','action':('shutdown','stop_video','close_browser')[index//2],'seconds':600.}

@pytest.mark.parametrize('text', ['stop video in -10 minutes', 'stop video in 0 seconds', 'stop video in 10000000000 hours', 'stop video in 10', 'stop video in nan minutes', 'stop video in 10 minutes and shut down computer', 'stop video in 10 minutes; rm -rf /'])
def test_invalid_duration_does_not_become_command(text):
    assert delayed.parse(text,'en')['seconds'] is None

@pytest.mark.parametrize('text', ['do not stop video in 10 minutes', 'what does stop video in 10 minutes mean', 'type stop video in 10 minutes', 'the document says: stop video in 10 minutes', 'open calculator in 10 minutes'])
def test_noncommands_never_schedule(text):
    assert delayed.parse(text,'en') is None

@pytest.mark.parametrize('code',PHRASES)
def test_language_isolation(code):
    if code != 'en':
        assert delayed.parse(PHRASES['en'][0],code) is None


@pytest.fixture
def clock(monkeypatch):
    clock=SimpleNamespace(wall=1000000000., mono=100.)
    monkeypatch.setattr(timers.time,'time',lambda:clock.wall)
    monkeypatch.setattr(timers.time,'monotonic',lambda:clock.mono)
    return clock

@pytest.fixture
def manager(tmp_path,monkeypatch):
    manager=timers.TimerManager()
    monkeypatch.setattr(manager,'_ensure_thread_locked',lambda:None)
    manager.configure(tmp_path,'en')
    return manager


def spec(action='shutdown',confirmed=True):
    return {'action':action,'language':'en','target':{},'risk':2 if action=='shutdown' else 0,'confirmed':confirmed}


def test_due_once_durable_consumption_and_cancel(manager,clock):
    one=manager.create_action(10,'Shutdown',spec())
    two=manager.create_action(10,'Other shutdown',spec())
    clock.mono += 10
    manager._tick();manager._tick()
    assert [e.timer_id for e in manager.drain()] == [one.id,two.id]
    assert manager.cancel(two.id)==1
    assert manager.take_action(two.id) is None
    assert manager.take_action(one.id).id==one.id
    assert manager.take_action(one.id) is None
    assert json.loads(manager._path.read_text())==[]


def test_monotonic_delay_survives_wall_clock_changes(manager,clock):
    item=manager.create_action(600,'Shutdown',spec())
    clock.wall += 86400
    manager._tick()
    assert manager.drain()==[] and manager.remaining(item)==600
    clock.wall -= 172800
    clock.mono += 600
    manager._tick()
    assert manager.take_action(item.id).id==item.id


def test_long_overdue_action_never_executes(manager,clock):
    item=manager.create_action(10,'Shutdown',spec())
    clock.mono += 41
    manager._tick()
    with pytest.raises(timers.ActionExpiredError):manager.take_action(item.id)
    assert not manager.list()


def test_restart_cancels_future_and_expired_actions_but_keeps_timer(manager,clock,monkeypatch):
    manager.create_action(600,'Future shutdown',spec())
    manager.create_action(1,'Expired shutdown',spec())
    reminder=manager.create(600,'Tea')
    clock.wall += 5
    new=timers.TimerManager();monkeypatch.setattr(new,'_ensure_thread_locked',lambda:None)
    new.configure(manager._path.parent,'en')
    assert [row.id for row in new.list()]==[reminder.id]
    assert len(new.drain())==2
    assert [row['id'] for row in json.loads(manager._path.read_text())]==[reminder.id]


def test_write_failures_never_commit_or_execute(manager,clock,monkeypatch):
    fail=lambda: (_ for _ in ()).throw(OSError('disk full'))
    with monkeypatch.context() as m:
        m.setattr(manager,'_save_locked',fail)
        with pytest.raises(OSError):manager.create_action(10,'Shutdown',spec())
    assert not manager.list() and not manager._action_deadlines
    item=manager.create_action(10,'Shutdown',spec());clock.mono+=10;manager._tick()
    with monkeypatch.context() as m:
        m.setattr(manager,'_save_locked',fail)
        with pytest.raises(OSError):manager.take_action(item.id)
        with pytest.raises(OSError):manager.cancel(item.id)
    assert manager.list()[0].id==item.id

@pytest.mark.parametrize('seconds',[float('nan'),float('inf'),0,-1,604801])
def test_duration_bound(manager,seconds):
    with pytest.raises(ValueError):manager.create_action(seconds,'Shutdown',spec())


def test_structured_allowlist_and_capacity(manager):
    for bad in ({'action':'powershell'},dict(spec(),command='shutdown now')):
        with pytest.raises(ValueError):manager.create_action(60,'Bad',bad)
    for i in range(32):manager.create_action(60,str(i),spec())
    with pytest.raises(ValueError):manager.create_action(60,'Overflow',spec())


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    from jarvis.testing.atlas_session import AtlasSession
    with external_io_blocked():
        session=AtlasSession(tmp_path_factory.mktemp('delayed_real_agent')/'session')
        for definition in session.agent.tools._tools.values():
            definition.handler=lambda args:ToolExecution(True,'QA inert',{'verified':True})
        session.agent.tools.set_execution_guard(lambda name,args,risk:session.calls.append({'tool':name,'arguments':copy.deepcopy(args)}) or None)
        yield session
        session.finish()

@pytest.fixture
def routed(session,manager,monkeypatch):
    session.reset_case('en')
    monkeypatch.setattr(delayed,'_manager',lambda *_:manager)
    monkeypatch.setattr(delayed,'capture_target',lambda action:{} if action=='shutdown' else {'label':'Bound target'})
    monkeypatch.setattr(delayed,'bound_video_tab',lambda target:object())
    monkeypatch.setattr(delayed,'resolve_target',lambda target:object())
    return session


def test_shutdown_confirmation_schedules_then_runs_once(routed,manager,clock):
    turn=routed.agent.handle('shut down computer in10minutes')
    assert turn.pending and not manager.list() and not routed.calls
    result=routed.agent.confirm_pending(True)
    assert result.outcome=='scheduled' and not routed.calls
    item=manager.list()[0];clock.mono+=600;manager._tick()
    result=routed.agent.run_delayed_action184(item.id)
    assert result.success and 'request sent' in result.text
    assert routed.calls==[{'tool':'system','arguments':{'action':'shutdown','delay_seconds':0}}]
    routed.agent.run_delayed_action184(item.id)
    assert len(routed.calls)==1


def test_rejected_confirmation_cannot_schedule(routed,manager):
    assert routed.agent.handle('shut down computer in10minutes').pending
    result=routed.agent.confirm_pending(False)
    assert result.outcome=='cancelled' and not manager.list() and not routed.calls

@pytest.mark.parametrize('action,text',[('stop_video','stop video in10minutes'),('close_browser','close browser in10minutes')])
def test_target_change_before_due_prevents_any_tool(routed,manager,clock,monkeypatch,action,text):
    assert routed.agent.handle(text).success
    item=manager.list()[0]
    def changed(_):raise ValueError('different target')
    monkeypatch.setattr(delayed,'bound_video_tab' if action=='stop_video' else 'resolve_target',changed)
    clock.mono+=600;manager._tick()
    result=routed.agent.run_delayed_action184(item.id)
    assert result.success is False and not routed.calls


def test_cancel_by_id_and_list_preserve_ordinary_timer(routed,manager):
    plain=manager.create(30,'Tea')
    assert routed.agent.handle('stop video in10minutes').success
    action=[row for row in manager.list() if row.kind=='action'][0]
    listed=routed.agent.handle('list delayed actions')
    assert action.id in listed.text and action.label in listed.text and '10 minutes' in listed.text
    assert routed.agent.handle('cancel action '+action.id).success
    assert [row.id for row in manager.list()]==[plain.id] and not routed.calls


def test_ambiguous_cancel_needs_identity(routed,manager):
    routed.agent.handle('stop video in10minutes');routed.agent.handle('stop video in10minutes')
    turn=routed.agent.handle('cancel action Stop YouTube video')
    assert turn.outcome=='clarification' and len(manager.list())==2


def test_remote_origin_never_becomes_desktop_permission(routed,manager):
    turn=routed.agent.handle('shut down computer in10minutes',input_source='telegram',conversation_id='chat-1')
    assert not turn.pending and not manager.list() and not routed.calls and 'desktop' in turn.text


def test_tool_failure_never_claims_completion(routed,manager,clock,monkeypatch):
    routed.agent.handle('stop video in10minutes');item=manager.list()[0]
    monkeypatch.setattr(routed.agent.tools,'execute',lambda *_:ToolExecution(False,'failed'))
    clock.mono+=600;manager._tick()
    assert routed.agent.run_delayed_action184(item.id).success is False


def test_due_registry_boundary_is_preserved(routed,manager,clock):
    routed.agent.handle('stop video in10minutes');item=manager.list()[0]
    routed.agent.tools.set_execution_guard(lambda *_:'Blocked by transport policy')
    clock.mono+=600;manager._tick()
    assert routed.agent.run_delayed_action184(item.id).success is False


def test_same_session_revalidation_before_pause(monkeypatch):
    from jarvis import browser_control
    expected={'source':'browser','title':'Old video','artist':'Author','album':''}
    calls=[]
    class FakeSession:
        async def try_pause_async(self):calls.append('pause');return True
    async def changed(*_,**__):return 1.,FakeSession(),dict(expected,title='New video'),{}
    monkeypatch.setattr(browser_control,'_find_exact_tab_media_session_async',changed)
    with pytest.raises(RuntimeError,match='changed'):
        asyncio.run(browser_control._control_exact_tab_media_session_async(object(),'pause','video',expected_session=expected))
    assert not calls


def test_window_pid_reuse_does_not_match(monkeypatch):
    from jarvis import browser_control
    target={'hwnd':5,'pid':1,'created':10,'process':'chrome.exe'}
    window=SimpleNamespace(handle=5)
    monkeypatch.setattr(browser_control,'_browser_windows',lambda *_:[window])
    monkeypatch.setattr(delayed,'_window_identity',lambda _:dict(target,created=20))
    with pytest.raises(ValueError):delayed.resolve_target(target)


@pytest.mark.parametrize('code,text,index', [(c,t,i) for c,rows in PHRASES.items() for i,t in enumerate(rows)])
def test_real_agent_all_locales(routed,manager,code,text,index):
    routed.reset_case(code)
    result=routed.agent.handle(text)
    if index//2==0:
        assert result.pending and not manager.list()
        result=routed.agent.confirm_pending(True)
    assert result.success and result.outcome=='scheduled' and len(manager.list())==1
    assert manager.list()[0].action_spec['action']==('shutdown','stop_video','close_browser')[index//2]
    assert not routed.calls


def test_invalid_delay_does_not_fall_through_to_shutdown(routed,manager):
    result=routed.agent.handle('shut down computer in -10 minutes')
    assert result.success is False and not result.pending and not manager.list() and not routed.calls


def test_new_schedule_replaces_old_unconfirmed_shutdown(routed,manager):
    assert routed.agent.handle('shut down computer in10minutes').pending
    assert routed.agent.handle('stop video in10minutes').success
    assert not routed.agent._pending_context
    routed.agent.confirm_pending(True)
    assert [row.action_spec['action'] for row in manager.list()]==['stop_video']


def test_risk_policy_tightens_before_due(routed,manager,clock,monkeypatch):
    routed.agent.handle('stop video in10minutes');item=manager.list()[0]
    monkeypatch.setattr(routed.agent,'_needs_confirmation',lambda *_:True)
    clock.mono+=600;manager._tick()
    assert not routed.agent.run_delayed_action184(item.id).success and not routed.calls


def test_agent_unverified_tool_never_claims_completion(routed,manager,clock,monkeypatch):
    routed.agent.handle('stop video in10minutes');item=manager.list()[0]
    monkeypatch.setattr(routed.agent.tools,'execute',lambda *_:ToolExecution(True,'Request sent',{'verified':False}))
    clock.mono+=600;manager._tick()
    result=routed.agent.run_delayed_action184(item.id)
    assert result.success is False and 'completion verified' not in result.text


def test_countdown_uses_action_name_and_monotonic_time(manager,clock):
    from jarvis.ui.main_window import MainWindow
    text=[]
    label=SimpleNamespace(setText=text.append,adjustSize=lambda:None,show=lambda:None,hide=lambda:None,raise_=lambda:None)
    window=SimpleNamespace(timer_stage_label=label,_timer_manager=manager,config=SimpleNamespace(get=lambda _,d:d),_position_timer_stage_label=lambda:None)
    manager.create_action(600,'Stop YouTube video · Example',spec('stop_video'))
    clock.wall+=86400;clock.mono+=1
    MainWindow._refresh_timer_stage_label(window)
    assert text==['Stop YouTube video · Example — 9:59']


def test_ui_dispatches_internal_id_without_user_scroll_and_cancel_wins(manager,clock):
    from jarvis.ui.main_window import MainWindow
    calls=[]
    window=SimpleNamespace(_timer_manager=manager,_busy=True,_quitting=False,_append_assistant=lambda _:None,
        agent=SimpleNamespace(_pending_context=None),_send_text=lambda *a,**kw:calls.append((a,kw)))
    item=manager.create_action(10,'Stop video',spec('stop_video'));clock.mono+=10;manager._tick()
    MainWindow._drain_timer_events(window)
    assert not calls
    manager.cancel(item.id);window._busy=False
    MainWindow._drain_timer_events(window)
    assert not calls
    item=manager.create_action(10,'Stop video',spec('stop_video'));clock.mono+=10;manager._tick()
    MainWindow._drain_timer_events(window)
    assert calls==[(('Stop video',),{'input_source':'text','user_submission':False,'delayed_action_id184':item.id})]


def test_ui_does_not_displace_existing_confirmation(manager,clock):
    from jarvis.ui.main_window import MainWindow
    calls=[]
    window=SimpleNamespace(_timer_manager=manager,_busy=False,_quitting=False,_append_assistant=lambda _:None,
        agent=SimpleNamespace(_pending_context={'existing':True}),_send_text=lambda *a,**kw:calls.append((a,kw)))
    manager.create_action(10,'Shutdown',spec());clock.mono+=10;manager._tick()
    MainWindow._drain_timer_events(window)
    assert not calls and len(window._delayed_actions184)==1


def test_changed_tab_source_cannot_be_rebound_to_new_video(monkeypatch):
    from jarvis import browser_control
    target={'hwnd':7,'pid':1,'created':1.,'process':'chrome.exe','url':'https://www.youtube.com/watch?v=old','tab_title':'Old video'}
    selected=SimpleNamespace(window_text=lambda:'Old video')
    window=SimpleNamespace(window_text=lambda:'Old video - Chrome')
    monkeypatch.setattr(delayed,'resolve_target',lambda _:window)
    monkeypatch.setattr(browser_control,'_selected_tab_with_index',lambda _:(selected,1))
    monkeypatch.setattr(browser_control,'_strict_selected_tab_url',lambda *a,**k:'https://www.youtube.com/watch?v=new')
    with pytest.raises(ValueError,match='changed'):delayed.bound_video_tab(target)


def test_expired_due_action_has_visible_failure(routed,manager,clock):
    routed.agent.handle('stop video in10minutes');item=manager.list()[0]
    clock.mono+=631;manager._tick()
    turn=routed.agent.run_delayed_action184(item.id)
    assert turn.success is False and 'expired' in turn.text and not routed.calls


def test_pending_browser_save_prompt_is_not_reported_closed(routed,manager,clock,monkeypatch):
    routed.agent.handle('close browser in10minutes');item=manager.list()[0]
    monkeypatch.setattr(routed.agent.tools,'execute',lambda *_:ToolExecution(False,'Close requested',{'requested':True,'pending':True,'verified':False}))
    clock.mono+=600;manager._tick()
    turn=routed.agent.run_delayed_action184(item.id)
    assert turn.success is False and 'still open' in turn.text


def test_queued_actions_cannot_be_evicted_by_recurring_commands(manager,clock):
    from jarvis.ui.main_window import MainWindow
    window=SimpleNamespace(_timer_manager=manager,_busy=True,_quitting=False,_append_assistant=lambda _:None,
        agent=SimpleNamespace(_pending_context=None),_send_text=lambda *_a,**_k:None)
    item=manager.create_action(10,'Shutdown',spec());clock.mono+=10;manager._tick()
    for i in range(20):manager._emit('command',f'Command {i}',str(i))
    MainWindow._drain_timer_events(window)
    assert list(window._delayed_actions184)==[('Shutdown',item.id)]
    assert len(window._scheduled_commands172)==8


def test_restart_ignores_nonlist_json(manager,monkeypatch):
    manager._path.write_text('null')
    other=timers.TimerManager();monkeypatch.setattr(other,'_ensure_thread_locked',lambda:None)
    other.configure(manager._path.parent,'en')
    assert not other.list()


def test_other_foreground_player_is_never_redirected_to_youtube(monkeypatch):
    from jarvis import browser_control
    tab=SimpleNamespace(window=SimpleNamespace(handle=5))
    monkeypatch.setattr(browser_control,'selected_browser_tab',lambda:tab)
    monkeypatch.setattr(delayed,'_foreground_identity',lambda:(7,os.getpid()+1))
    with pytest.raises(ValueError,match='Select the requested browser'):delayed.capture_target('stop_video')


def test_ambiguous_browser_without_recent_identity_is_not_guessed(monkeypatch):
    from jarvis import browser_control
    tab=SimpleNamespace(window=SimpleNamespace(handle=5))
    monkeypatch.setattr(browser_control,'selected_browser_tab',lambda:tab)
    monkeypatch.setattr(browser_control,'_LAST_BROWSER_HANDLE',0)
    monkeypatch.setattr(browser_control,'_browser_windows',lambda:[object(),object()])
    monkeypatch.setattr(delayed,'_foreground_identity',lambda:(7,os.getpid()))
    with pytest.raises(ValueError,match='Several browser windows'):delayed.capture_target('close_browser')


def test_non_youtube_selected_tab_cannot_be_scheduled(monkeypatch):
    from jarvis import browser_control
    tab=SimpleNamespace(window=SimpleNamespace(handle=5),title='Other player',url='https://example.com/video',process_name='chrome.exe')
    monkeypatch.setattr(browser_control,'selected_browser_tab',lambda:tab)
    monkeypatch.setattr(delayed,'_foreground_identity',lambda:(5,10))
    monkeypatch.setattr(delayed,'_window_identity',lambda _:dict(hwnd=5,pid=10,created=1.,process='chrome.exe'))
    with pytest.raises(RuntimeError):delayed.capture_target('stop_video')


def test_delayed_media_rejects_legacy_single_compatible_session_fallback(monkeypatch):
    import sys
    from types import ModuleType
    from jarvis import browser_control as browser
    calls=[]
    class FakeSession:
        source_app_user_model_id='chrome.exe'
        async def try_get_media_properties_async(self):return SimpleNamespace(title='Unrelated video B',artist='Other author',album_title='')
        def get_playback_info(self):return SimpleNamespace(playback_status=SimpleNamespace(name='Playing'))
        async def try_pause_async(self):calls.append('pause');return True
        def get_timeline_properties(self):raise RuntimeError('No timeline')
    class Manager:
        @staticmethod
        async def request_async():return SimpleNamespace(get_sessions=lambda:[FakeSession()])
    module=ModuleType('winsdk.windows.media.control');module.GlobalSystemMediaTransportControlsSessionManager=Manager
    monkeypatch.setitem(sys.modules,'winsdk.windows.media.control',module)
    monkeypatch.setattr(browser,'_page_fingerprint',lambda _:'Selected video A')
    monkeypatch.setattr(browser,'media_session_tab_score',lambda *_:0.)
    tab=browser.BrowserTab('Selected video A - YouTube','chrome.exe','Browser',None,SimpleNamespace(handle=7),url='https://www.youtube.com/watch?v=A')
    # 186 (VIDEO185-SELECTOR-IDENTITY-01): a lone session whose title contradicts the
    # YouTube tab is not that tab's player, not even on the immediate non-strict path.
    with pytest.raises(RuntimeError):
        asyncio.run(browser._read_exact_tab_media_session_async(tab,'video'))
    with pytest.raises(RuntimeError):
        asyncio.run(browser._read_exact_tab_media_session_async(tab,'video',strict_match=True))
    expected={'source':'chrome.exe','title':'Unrelated video B','artist':'Other author','album':''}
    with pytest.raises(RuntimeError):
        asyncio.run(browser._control_exact_tab_media_session_async(tab,'pause','video',expected_session=expected))
    assert calls==[]


def test_restart_cleanup_write_failure_keeps_actions_disabled_and_starts_timers(manager,monkeypatch):
    ordinary=manager.create(600,'Tea')
    manager.create_action(600,'Shutdown',spec())
    restarted=timers.TimerManager();started=[]
    monkeypatch.setattr(restarted,'_ensure_thread_locked',lambda:started.append(True))
    monkeypatch.setattr(restarted,'_save_locked',lambda:(_ for _ in ()).throw(OSError('read-only store')))
    restarted.configure(manager._path.parent,'en')
    assert [row.id for row in restarted.list()]==[ordinary.id]
    assert started==[True] and not restarted._action_deadlines
    events=restarted.drain()
    assert any('Could not update saved actions' in event.text for event in events)
    assert all(event.kind!='action' for event in events)
