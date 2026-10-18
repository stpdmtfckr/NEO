"""Discord lookup lifecycle with real offscreen Qt and inert transport edges."""
from __future__ import annotations
import os
import socket
import threading
import time
import weakref
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import shiboken6
from PySide6.QtCore import QObject, Signal, QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QApplication
from jarvis.testing.atlas_session import AtlasConfig
from jarvis.testing.isolation import external_io_blocked
from jarvis.ui import settings_dialog as ui


def pump(app, predicate=lambda:True, timeout=3):
    deadline=time.monotonic()+timeout
    while True:
        app.processEvents()
        if predicate():return
        if time.monotonic()>=deadline:pytest.fail('Qt completion barrier was not reached')
        time.sleep(.002)


def delete_events(app):
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
    app.processEvents()


@pytest.fixture(scope='module')
def app():return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def external_guard(monkeypatch):
    # Each test owns its inert process-lane state. No real Gateway exists here.
    monkeypatch.setattr(ui, '_DISCORD_DISCOVERY_LANE184', threading.Lock())
    monkeypatch.setattr(ui, '_DISCORD_DISCOVERY_PENDING184', None)
    with external_io_blocked(),patch.object(os,'system',side_effect=PermissionError('test forbids shell')),patch.object(socket.socket,'sendto',side_effect=PermissionError('test forbids UDP')):
        yield


@pytest.fixture
def dialog_factory(app,tmp_path,monkeypatch):
    windows=[];popups=[];errors=[]
    monkeypatch.setattr(ui.QMessageBox,'information',lambda *a,**kw:popups.append(('info',a,threading.get_ident())))
    monkeypatch.setattr(ui.QMessageBox,'warning',lambda *a,**kw:popups.append(('warning',a,threading.get_ident())))
    monkeypatch.setattr('sys.excepthook',lambda *a:errors.append(a))
    def make():
        config=AtlasConfig(tmp_path/('profile-'+str(len(windows))))
        config.data['app']['language']='ru'
        config.data['integrations']['discord'].update(enabled=True,servers=[
            {'name':'Alpha','guild_id':'synthetic-alpha','discord_user_id':'owner-alpha','bot_token_encrypted':'INERT_ALPHA'},
            {'name':'Beta','guild_id':'synthetic-beta','discord_user_id':'owner-beta','bot_token_encrypted':'INERT_BETA'},
        ])
        dialog=ui.SettingsDialog(config,deferred=True)
        dialog._settings_build_timer160.stop()
        page=dialog._discord_tab();page.setParent(dialog)
        dialog._root_layout.addWidget(page)
        dialog.discord_servers_table.selectRow(0)
        dialog.show();app.processEvents()
        windows.append(dialog)
        return dialog
    yield make,popups,errors
    for window in windows:
        if shiboken6.isValid(window):window.reject();window.deleteLater()
    delete_events(app)
    assert errors==[]


class FakeSignals(QObject):
    result=Signal(object)
    error=Signal(str)
    finished=Signal()


class FakeWorker:
    def __init__(self,function,*args,**kwargs):
        self.function=function;self.args=args;self.kwargs=kwargs;self.signals=FakeSignals()


@pytest.fixture
def fake_pool(monkeypatch):
    started=[]
    monkeypatch.setattr(ui,'FunctionWorker',FakeWorker)
    monkeypatch.setattr(ui,'QThreadPool',SimpleNamespace(globalInstance=lambda:SimpleNamespace(start=started.append)))
    return started


def finish(worker,app):
    worker.signals.finished.emit();pump(app)


def test_repeated_clicks_have_one_request_and_one_popup(dialog_factory,fake_pool,app):
    make,popups,_=dialog_factory;d=make()
    for _ in range(100):d._discord_discover_selected_channels()
    assert len(fake_pool)==1 and not d.discord_discover_button184.isEnabled()
    w=fake_pool[0]
    w.signals.result.emit({'voice_channels':[{'name':'Voice A'}],'text_channels':[{'name':'Text A'}]})
    w.signals.result.emit({'voice_channels':[{'name':'Duplicate'}]});pump(app)
    assert len(popups)==1 and 'Voice A' in popups[0][1][2] and 'Text A' in popups[0][1][2]
    assert popups[0][2]==threading.get_ident()
    assert d._discord_discovery_worker is w
    finish(w,app)
    assert d._discord_discovery_worker is None and d.discord_discover_button184.isEnabled()
    d._discord_discover_selected_channels();assert len(fake_pool)==2


@pytest.mark.parametrize('signal',['result','error'])
@pytest.mark.parametrize('destroy',[False,True])
def test_closed_or_destroyed_dialog_discards_late_delivery(dialog_factory,fake_pool,app,signal,destroy):
    make,popups,_=dialog_factory;d=make();d._discord_discover_selected_channels()
    w=fake_pool[0];relay=d._discord_discovery_relay184;token=relay.cancelled
    d.reject()
    if destroy:d.deleteLater();delete_events(app);assert not shiboken6.isValid(d)
    assert token.is_set()
    if signal=='result':w.signals.result.emit({'voice_channels':[{'name':'Too late'}]})
    else:w.signals.error.emit('synthetic late error')
    finish(w,app)
    assert popups==[]


def test_direct_destruction_without_finished_cancels_and_disconnects(dialog_factory,fake_pool,app):
    make,popups,_=dialog_factory;d=make();d._discord_discover_selected_channels()
    w=fake_pool[0];relay=d._discord_discovery_relay184;token=relay.cancelled
    d.deleteLater();delete_events(app)
    assert not shiboken6.isValid(d) and not shiboken6.isValid(relay) and token.is_set()
    w.signals.result.emit({'voice_channels':[{'name':'Too late'}]});w.signals.error.emit('late');finish(w,app)
    assert popups==[]


@pytest.mark.parametrize('mutation',['selection','selection_back','rename','user_id','token','saved_profile','profile_root','apply'])
def test_profile_or_row_changes_invalidate_result(dialog_factory,fake_pool,app,mutation):
    make,popups,_=dialog_factory;d=make();d._discord_discover_selected_channels();w=fake_pool[0]
    if mutation in {'selection','selection_back'}:
        d.discord_servers_table.selectRow(1)
        if mutation=='selection_back':d.discord_servers_table.selectRow(0)
    elif mutation=='rename':d.discord_servers_table.item(0,1).setText('Changed')
    elif mutation=='user_id':d.discord_servers_table.item(0,3).setText('changed-owner')
    elif mutation=='token':d.discord_servers_table.cellWidget(0,2).setText('INERT_EDIT')
    elif mutation=='saved_profile':d.config.data['integrations']['discord']['servers'][0]['guild_id']='changed'
    elif mutation=='profile_root':d.config=AtlasConfig(d.config.paths.root/'switched')
    elif mutation=='apply':d.settings_saved.emit()
    d._discord_discover_selected_channels();assert len(fake_pool)==1
    w.signals.result.emit({'voice_channels':[{'name':'Stale'}]});finish(w,app)
    assert popups==[] and d.discord_discover_button184.isEnabled()


def test_old_finished_cannot_release_new_request(dialog_factory,fake_pool,app):
    make,_,_=dialog_factory;d=make();d._discord_discover_selected_channels()
    old=d._discord_discovery_relay184;finish(fake_pool[0],app)
    d._discord_discover_selected_channels();new=d._discord_discovery_relay184;new_worker=d._discord_discovery_worker
    d._discord_discovery_finished184(old)
    fake_pool[0].signals.finished.emit();pump(app)
    assert d._discord_discovery_relay184 is new and d._discord_discovery_worker is new_worker
    assert not d.discord_discover_button184.isEnabled()
    finish(new_worker,app);assert d.discord_discover_button184.isEnabled()


def test_error_then_result_has_one_terminal_popup_and_releases_busy_state(dialog_factory,fake_pool,app):
    make,popups,_=dialog_factory;d=make();d._discord_discover_selected_channels();w=fake_pool[0]
    w.signals.error.emit('RuntimeError: synthetic failure');w.signals.result.emit({'voice_channels':[{'name':'Not valid'}]})
    finish(w,app)
    assert len(popups)==1 and popups[0][0]=='warning'
    assert d._discord_discovery_worker is None and d.discord_discover_button184.isEnabled()


def test_finished_before_result_suppresses_out_of_order_terminal(dialog_factory,fake_pool,app):
    make,popups,_=dialog_factory;d=make();d._discord_discover_selected_channels();w=fake_pool[0]
    w.signals.finished.emit();w.signals.result.emit({'voice_channels':[{'name':'Late'}]});pump(app)
    assert popups==[] and d._discord_discovery_worker is None


def test_snapshot_preserves_target_and_worker_inputs_do_not_retain_dialog(dialog_factory,fake_pool,monkeypatch):
    from jarvis.integrations import discord_navigation
    make,_,_=dialog_factory;d=make();d._discord_discover_selected_channels();w=fake_pool[0]
    seen=[]
    class Adapter:
        def __init__(self,config):self.config=config
        def execute(self,action,**kwargs):
            seen.append((action,kwargs,self.config.get('integrations.discord.servers')[0]['guild_id']))
            return {'voice_channels':[]}
    monkeypatch.setattr(discord_navigation,'ReliableDiscordAdapter',Adapter)
    d.config.data['integrations']['discord']['servers'][0]['guild_id']='changed-after-start'
    assert w.function is ui._discover_discord_channels184
    assert all(not isinstance(x,ui.SettingsDialog) for x in w.args)
    assert w.function(*w.args)=={'voice_channels':[]}
    assert seen==[('list_channels',{'server':'Alpha'},'synthetic-alpha')]


def test_cancelled_before_run_does_not_construct_adapter(dialog_factory,fake_pool,monkeypatch):
    from jarvis.integrations import discord_navigation
    make,_,_=dialog_factory;d=make();d._discord_discover_selected_channels();w=fake_pool[0]
    monkeypatch.setattr(discord_navigation,'ReliableDiscordAdapter',lambda *a:pytest.fail('cancelled request started adapter'))
    d.reject()
    assert w.function(*w.args)=={'cancelled':True}


def test_submission_failure_recovers_controls_and_reports_once(dialog_factory,fake_pool,monkeypatch):
    make,popups,_=dialog_factory;d=make()
    def fail(_):raise RuntimeError('inert pool rejection')
    monkeypatch.setattr(ui,'QThreadPool',SimpleNamespace(globalInstance=lambda:SimpleNamespace(start=fail)))
    d._discord_discover_selected_channels()
    assert d._discord_discovery_worker is None and d._discord_discovery_relay184 is None
    assert d.discord_discover_button184.isEnabled() and len(popups)==1 and popups[0][0]=='warning'


@pytest.mark.parametrize('destroy',[False,True])
def test_real_worker_is_nonblocking_gui_delivered_and_safe_after_destroy(dialog_factory,app,monkeypatch,destroy):
    from jarvis.integrations import discord_navigation
    make,popups,_=dialog_factory;d=make()
    entered=threading.Event();release=threading.Event();worker_threads=[]
    class Adapter:
        def __init__(self,config):self.config=config
        def execute(self,action,**kwargs):
            worker_threads.append(threading.get_ident());entered.set();assert release.wait(4)
            return {'voice_channels':[{'name':'From worker'}]}
    monkeypatch.setattr(discord_navigation,'ReliableDiscordAdapter',Adapter)
    beats=[];timer=QTimer();timer.setInterval(2);timer.timeout.connect(lambda:beats.append(1));timer.start()
    started=time.monotonic();d._discord_discover_selected_channels()
    assert time.monotonic()-started<.35
    signals=d._discord_discovery_worker.signals;relay=d._discord_discovery_relay184
    try:
        pump(app,lambda:entered.is_set() and len(beats)>=5)
        for _ in range(40):d._discord_discover_selected_channels()
        assert len(worker_threads)==1 and worker_threads[0]!=threading.get_ident()
        if destroy:
            d.deleteLater();delete_events(app)
            assert not shiboken6.isValid(d) and not shiboken6.isValid(relay)
        release.set()
        pump(app,lambda:bool(popups) if not destroy else not shiboken6.isValid(signals))
        if destroy:assert popups==[]
        else:
            assert len(popups)==1 and popups[0][2]==threading.get_ident()
            pump(app,lambda:d._discord_discovery_worker is None)
        delete_events(app)
        assert not shiboken6.isValid(relay)
    finally:
        release.set();timer.stop()


def test_real_unsubmitted_worker_releases_inputs_and_signal_object(dialog_factory,app,monkeypatch):
    make,popups,_=dialog_factory;d=make();original=ui.FunctionWorker;workers=[]
    def worker(*args,**kwargs):
        obj=original(*args,**kwargs);workers.append(obj);return obj
    def fail(_):raise RuntimeError('synthetic pool rejection before run')
    monkeypatch.setattr(ui,'FunctionWorker',worker)
    monkeypatch.setattr(ui,'QThreadPool',SimpleNamespace(globalInstance=lambda:SimpleNamespace(start=fail)))
    d._discord_discover_selected_channels()
    signals=workers[0].signals
    assert workers[0].function is None and signals._job_inputs is None
    delete_events(app)
    assert not shiboken6.isValid(signals)
    assert d.findChildren(ui._DiscordDiscoveryRelay184)==[]
    assert len(popups)==1 and d.discord_discover_button184.isEnabled()


def test_repeated_request_completion_does_not_accumulate_relays(dialog_factory,fake_pool,app):
    make,_,_=dialog_factory;d=make()
    for _ in range(30):
        d._discord_discover_selected_channels();worker=fake_pool[-1]
        assert len(d.findChildren(ui._DiscordDiscoveryRelay184))==1
        worker.signals.result.emit({'voice_channels':[],'text_channels':[]});finish(worker,app);delete_events(app)
        assert d.findChildren(ui._DiscordDiscoveryRelay184)==[]
        assert d._discord_discovery_worker is None and d._discord_discovery_relay184 is None


def test_snapshot_is_compatible_with_actual_read_only_discord_route(dialog_factory,fake_pool,monkeypatch):
    from jarvis.integrations import discord_navigation as dn
    make,_,_=dialog_factory;d=make();d._discord_discover_selected_channels();worker=fake_pool[0]
    monkeypatch.setattr(dn.YandexMusicAdapter,'clear_transient_cache_on_startup_once',lambda *_:{})
    seen=[]
    bot=SimpleNamespace(list_accessible_channels=lambda profile:seen.append(dict(profile)) or {'voice_channels':[{'name':'Inert'}]})
    monkeypatch.setattr(dn.ReliableDiscordAdapter,'_background_for_profile',lambda self,profile:bot)
    d.config.data['integrations']['discord']['servers'][0]['name']='Changed after admission'
    result=worker.function(*worker.args)
    assert result=={'voice_channels':[{'name':'Inert'}]}
    assert seen[0]['name']=='Alpha' and seen[0]['guild_id']=='synthetic-alpha'


@pytest.mark.parametrize('case',['no_selection','empty_name','unsaved_name'])
def test_invalid_selection_never_starts_lookup(dialog_factory,fake_pool,case):
    make,popups,_=dialog_factory;d=make()
    if case=='no_selection':d.discord_servers_table.setCurrentCell(-1,-1)
    elif case=='empty_name':d.discord_servers_table.item(0,1).setText('')
    else:d.discord_servers_table.item(0,1).setText('Unsaved')
    d._discord_discover_selected_channels()
    assert fake_pool==[] and len(popups)==1 and d.discord_discover_button184.isEnabled()


@pytest.mark.parametrize('rebuild',[False,True])
def test_removed_or_rebuilt_media_page_ignores_old_request(dialog_factory,fake_pool,app,rebuild):
    make,popups,_=dialog_factory;d=make();d._discord_discover_selected_channels();w=fake_pool[0]
    # The page is a dialog child, while the dialog-owned relay remains alive.
    page=d.discord_servers_table
    while page.parentWidget() is not d:page=page.parentWidget()
    page.deleteLater();delete_events(app)
    if rebuild:
        replacement=d._discord_tab();replacement.setParent(d);d._root_layout.addWidget(replacement)
        d.discord_servers_table.selectRow(0)
        assert not d.discord_discover_button184.isEnabled()
    w.signals.result.emit({'voice_channels':[{'name':'Old page'}]});finish(w,app)
    assert popups==[] and d._discord_discovery_worker is None
    if rebuild:assert d.discord_discover_button184.isEnabled()


@pytest.fixture
def owned_transports(monkeypatch,tmp_path):
    """Use actual adapter allocation/routing; replace only the Gateway client."""
    from jarvis.integrations import discord_navigation as dn
    config=AtlasConfig(tmp_path/'transport-profile')
    config.data['integrations']['discord'].update(enabled=True,servers=[
        {'name':'Alpha','guild_id':'synthetic-alpha','bot_token_encrypted':'INERT_ALPHA'},
        {'name':'Beta','guild_id':'synthetic-beta','bot_token_encrypted':'INERT_BETA'},
    ])
    snapshot=ui._DiscordDiscoveryConfig184(config)
    world=SimpleNamespace(config=snapshot,bots=[],lookups=[],closed=[],lookup=None,close=None,module=dn)
    class Bot:
        def __init__(self,config,token_encrypted=''):
            self.config=config;self.token_encrypted=token_encrypted
            self.gateway_running=False;self.music_playing=False;self.close_calls=[]
            world.bots.append(self)
        def set_control_handler(self,handler):self.control=handler
        def set_music_control_handler(self,handler):self.music_control=handler
        @property
        def owner(self):return self.control.__self__
        def list_accessible_channels(self,profile):
            self.gateway_running=True
            world.lookups.append((self,dict(profile),threading.get_ident()))
            if world.lookup is not None:return world.lookup(self,profile)
            return {'server':profile['name'],'voice_channels':[{'name':'Inert voice'}],'verified':True}
        def close(self,timeout=3.0):
            self.close_calls.append((timeout,threading.get_ident()));world.closed.append(self)
            result=True if world.close is None else world.close(self)
            if result is True:self.gateway_running=False
            return result
    monkeypatch.setattr(dn,'DiscordBackgroundBot',Bot)
    monkeypatch.setattr(dn.YandexMusicAdapter,'clear_transient_cache_on_startup_once',lambda *_:{})
    return world


def test_r038_sequential_lookups_close_only_their_dedicated_bots(owned_transports):
    world=owned_transports
    # Same config/token on a separate real adapter represents the app's existing
    # music connection. The probe must never scan/close that separate owner.
    active=world.module.ReliableDiscordAdapter(world.config)
    music_bot=active._background_for_profile(active._background_profile('Alpha'))
    music_bot.gateway_running=True;music_bot.music_playing=True
    for _ in range(12):
        result=ui._discover_discord_channels184(world.config,'Alpha',threading.Event())
        assert result['verified']
    probes=[bot for bot in world.bots if bot is not music_bot]
    assert len(probes)==12 and all(len(bot.close_calls)==1 and not bot.gateway_running for bot in probes)
    assert all(bot.close_calls[0][0]==3.0 for bot in probes)
    assert all(bot.owner._background_bots=={} for bot in probes)
    assert music_bot.close_calls==[] and music_bot.gateway_running and music_bot.music_playing
    assert active._background_bots and all(bot.owner._discovery_transport_cleanup184['complete'] for bot in probes)


def test_r038_lookup_failure_still_closes_owned_transport(owned_transports):
    world=owned_transports
    def fail(bot,profile):raise ValueError('synthetic lookup failure')
    world.lookup=fail
    with pytest.raises(ValueError,match='synthetic lookup failure'):
        ui._discover_discord_channels184(world.config,'Alpha',threading.Event())
    assert len(world.bots)==1 and len(world.closed)==1 and not world.bots[0].gateway_running
    assert world.bots[0].owner._discovery_transport_cleanup184['complete']


def test_r038_cancel_during_lookup_still_closes_owned_transport(owned_transports):
    world=owned_transports;cancelled=threading.Event()
    def cancel(bot,profile):cancelled.set();return {'voice_channels':[{'name':'Discarded'}]}
    world.lookup=cancel
    assert ui._discover_discord_channels184(world.config,'Alpha',cancelled)=={'cancelled':True}
    assert len(world.closed)==1 and not world.bots[0].gateway_running


def test_r038_cancel_after_real_adapter_construction_skips_gateway(owned_transports,monkeypatch):
    world=owned_transports;cancelled=threading.Event();base=world.module.ReliableDiscordAdapter;constructed=[]
    class CancelAfterConstruction(base):
        def __init__(self,config):
            super().__init__(config);constructed.append(self);cancelled.set()
    monkeypatch.setattr(world.module,'ReliableDiscordAdapter',CancelAfterConstruction)
    assert ui._discover_discord_channels184(world.config,'Alpha',cancelled)=={'cancelled':True}
    assert len(constructed)==1 and world.bots==[] and world.lookups==[]
    assert constructed[0]._discovery_transport_cleanup184=={'attempted':0,'closed':0,'remaining':0,'complete':True,'errors':[]}


@pytest.mark.parametrize('lookup_fails',[False,True])
@pytest.mark.parametrize('close_outcome',['false','raises','none'])
def test_r038_unconfirmed_close_is_retained_and_reported(owned_transports,caplog,lookup_fails,close_outcome):
    world=owned_transports
    if lookup_fails:
        def fail(bot,profile):raise ValueError('synthetic lookup failure')
        world.lookup=fail
    def close(bot):
        if close_outcome=='raises':raise RuntimeError('synthetic close failure')
        return False if close_outcome=='false' else None
    world.close=close
    with pytest.raises(RuntimeError,match='закрытие временного подключения Discord') as captured:
        ui._discover_discord_channels184(world.config,'Alpha',threading.Event())
    report=captured.value.discovery_transport_cleanup
    assert report['attempted']==1 and report['closed']==0 and not report['complete']
    if lookup_fails:assert 'synthetic lookup failure' in str(captured.value) and isinstance(captured.value.__cause__,ValueError)
    bot=world.bots[0]
    assert bot.gateway_running and list(bot.owner._background_bots.values())==[bot]
    assert bot.owner._discovery_transport_cleanup184 is report
    assert 'cleanup unconfirmed' in caplog.text


def test_r038_multiple_owned_clients_are_deduplicated_and_all_attempted(owned_transports):
    world=owned_transports
    def lookup(bot,profile):
        other=bot.owner._background_for_profile(bot.owner._background_profile('Beta'))
        other.gateway_running=True
        bot.owner._background_bots['alias-for-alpha']=bot
        return {'voice_channels':[]}
    world.lookup=lookup
    ui._discover_discord_channels184(world.config,'Alpha',threading.Event())
    assert len(world.bots)==2 and len(world.closed)==2
    assert all(len(bot.close_calls)==1 and not bot.gateway_running for bot in world.bots)
    assert world.bots[0].owner._background_bots=={}


def test_r038_one_failed_close_does_not_skip_other_owned_clients(owned_transports):
    world=owned_transports
    def lookup(bot,profile):
        other=bot.owner._background_for_profile(bot.owner._background_profile('Beta'));other.gateway_running=True
        return {'voice_channels':[]}
    world.lookup=lookup
    world.close=lambda bot:bot.token_encrypted=='INERT_BETA'
    with pytest.raises(RuntimeError):ui._discover_discord_channels184(world.config,'Alpha',threading.Event())
    assert len(world.closed)==2
    owner=world.bots[0].owner
    assert owner._discovery_transport_cleanup184['closed']==1
    assert list(owner._background_bots.values())==[world.bots[0]]


def test_r038_cleanup_does_not_hold_adapter_registry_lock(owned_transports):
    world=owned_transports
    def close(bot):
        entered=threading.Event()
        def acquire():
            with bot.owner._background_bots_lock:entered.set()
        probe=threading.Thread(target=acquire);probe.start()
        assert entered.wait(1), 'cleanup held registry lock while waiting for close'
        probe.join(1);assert not probe.is_alive()
        return True
    world.close=close
    assert ui._discover_discord_channels184(world.config,'Alpha',threading.Event())['verified']


@pytest.mark.parametrize('view_action',['keep','reject','destroy'])
@pytest.mark.parametrize('close_verified',[False,True])
def test_r038_real_worker_cleanup_is_off_gui_and_cancellation_during_close_wins(dialog_factory,owned_transports,app,view_action,close_verified):
    make,popups,_=dialog_factory;d=make();world=owned_transports
    entered=threading.Event();release=threading.Event()
    def close(bot):entered.set();assert release.wait(4);return close_verified
    world.close=close
    beats=[];timer=QTimer();timer.setInterval(2);timer.timeout.connect(lambda:beats.append(1));timer.start()
    began=time.monotonic();d._discord_discover_selected_channels();assert time.monotonic()-began<.35
    signals=d._discord_discovery_worker.signals
    try:
        pump(app,lambda:entered.is_set() and len(beats)>=5)
        assert not popups and not d.discord_discover_button184.isEnabled()
        assert world.closed[0].close_calls[0][1]!=threading.get_ident()
        if view_action=='reject':d.reject()
        elif view_action=='destroy':d.deleteLater();delete_events(app);assert not shiboken6.isValid(d)
        release.set()
        pump(app,lambda:not shiboken6.isValid(signals))
        if view_action=='keep':assert len(popups)==1 and popups[0][0]==('info' if close_verified else 'warning')
        else:assert popups==[]
        assert world.bots[0].gateway_running is (not close_verified)
        assert world.bots[0].owner._discovery_transport_cleanup184['complete'] is close_verified
    finally:release.set();timer.stop()


def test_r038_live_view_warns_instead_of_claiming_success_when_close_unconfirmed(dialog_factory,owned_transports,app):
    make,popups,_=dialog_factory;d=make();world=owned_transports;world.close=lambda bot:False
    d._discord_discover_selected_channels()
    pump(app,lambda:d._discord_discovery_worker is None)
    assert len(popups)==1 and popups[0][0]=='warning'
    assert 'закрытие временного подключения Discord' in popups[0][1][2]
    assert world.bots[0].gateway_running


def test_r038_cancelled_lookup_does_not_hide_failed_transport_cleanup(owned_transports,caplog):
    world=owned_transports;cancelled=threading.Event()
    def cancel(bot,profile):cancelled.set();return {'voice_channels':[]}
    world.lookup=cancel;world.close=lambda bot:False
    with pytest.raises(RuntimeError) as captured:
        ui._discover_discord_channels184(world.config,'Alpha',cancelled)
    assert not captured.value.discovery_transport_cleanup['complete']
    assert world.bots[0].gateway_running and world.bots[0].owner._background_bots
    assert 'cleanup unconfirmed' in caplog.text


def test_r038_late_owned_client_cannot_be_reported_as_complete_cleanup(owned_transports):
    world=owned_transports
    def close(bot):
        later=bot.owner._background_for_profile(bot.owner._background_profile('Beta'))
        later.gateway_running=True
        return True
    world.close=close
    with pytest.raises(RuntimeError) as captured:
        ui._discover_discord_channels184(world.config,'Alpha',threading.Event())
    report=captured.value.discovery_transport_cleanup
    assert report['attempted']==1 and report['closed']==1 and report['remaining']==1
    assert not report['complete']
    assert len(world.bots)==2 and world.bots[1].gateway_running
    assert list(world.bots[0].owner._background_bots.values())==[world.bots[1]]


def test_r038_already_cancelled_lookup_never_allocates_transport(owned_transports):
    world=owned_transports;cancelled=threading.Event();cancelled.set()
    assert ui._discover_discord_channels184(world.config,'Alpha',cancelled)=={'cancelled':True}
    assert world.bots==[] and world.lookups==[] and world.closed==[]


def test_r038_repeated_failed_cleanup_never_allocates_another_gateway(owned_transports):
    world=owned_transports;world.close=lambda bot:False
    for index in range(15):
        with pytest.raises(RuntimeError,match='закрытие временного подключения Discord'):
            ui._discover_discord_channels184(world.config,'Alpha' if index%2 else 'Beta',threading.Event())
        assert len(world.bots)==1 and len(world.lookups)==1
        assert ui._DISCORD_DISCOVERY_PENDING184 is world.bots[0].owner
        assert world.bots[0].gateway_running
    assert len(world.bots[0].close_calls)==15
    # A new lookup is admitted only after the prior owner actually confirms close.
    world.close=lambda bot:True
    result=ui._discover_discord_channels184(world.config,'Alpha',threading.Event())
    assert result['verified'] and len(world.bots)==2 and len(world.lookups)==2
    assert all(not bot.gateway_running for bot in world.bots)
    assert ui._DISCORD_DISCOVERY_PENDING184 is None


def test_r038_concurrent_lookup_is_rejected_before_new_adapter_or_gateway(owned_transports):
    world=owned_transports;entered=threading.Event();release=threading.Event();record={}
    def lookup(bot,profile):entered.set();assert release.wait(3);return {'verified':True}
    world.lookup=lookup
    def first():
        try:record['result']=ui._discover_discord_channels184(world.config,'Alpha',threading.Event())
        except BaseException as exc:record['error']=exc
    thread=threading.Thread(target=first);thread.start();assert entered.wait(3)
    try:
        for _ in range(10):
            with pytest.raises(RuntimeError,match='уже выполняется'):
                ui._discover_discord_channels184(world.config,'Beta',threading.Event())
        assert len(world.bots)==1 and len(world.lookups)==1
    finally:release.set();thread.join(3)
    assert not thread.is_alive() and record['result']['verified']
    assert ui._DISCORD_DISCOVERY_PENDING184 is None and not world.bots[0].gateway_running


def test_r038_cancel_during_pending_cleanup_prevents_replacement_lookup(owned_transports):
    world=owned_transports;world.close=lambda bot:False
    with pytest.raises(RuntimeError):ui._discover_discord_channels184(world.config,'Alpha',threading.Event())
    cancelled=threading.Event()
    def close(bot):cancelled.set();return True
    world.close=close
    assert ui._discover_discord_channels184(world.config,'Beta',cancelled)=={'cancelled':True}
    assert len(world.bots)==1 and len(world.lookups)==1 and not world.bots[0].gateway_running
    assert ui._DISCORD_DISCOVERY_PENDING184 is None
