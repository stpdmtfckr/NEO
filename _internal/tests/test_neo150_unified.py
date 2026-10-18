"""No live credentials, no downloads or desktop actions."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import threading
import zipfile
import pytest

class Config:
    def __init__(self,values):self.values=values
    def get(self,k,d=None):return self.values.get(k,d)

@pytest.mark.parametrize('http,expected',[(200,'AUTHENTICATED'),(401,'AUTH_REJECTED_OR_EXPIRED'),(403,'FORBIDDEN'),(429,'RATE_LIMITED'),(503,'FAIL')])
def test_auth_probe_status_and_redaction(http,expected):
    from jarvis.testing.integration_probes150 import check
    response=Mock(status_code=http);response.json.return_value={'id':'PRIVATE_ACCOUNT','secret':'PRIVATE_KEY'}
    session=Mock();session.get.return_value=response
    cfg=Config({'integrations.discord.enabled':True,'integrations.discord.bot_token_encrypted':'plain64:VEVTVA=='})
    # A credential is injected locally to test the boundary, not sent to a server.
    import integration_test_runner as api
    from unittest.mock import patch
    with patch.object(api,'_credential',return_value='NEVER_EXPORT_THIS'):
        row=check('Discord',cfg,session)
    assert row['status']==expected,row
    serialized=json.dumps(row)
    assert 'NEVER_EXPORT_THIS' not in serialized and 'PRIVATE_ACCOUNT' not in serialized
    assert session.get.call_args.kwargs['allow_redirects'] is False
    assert session.get.call_args.kwargs['headers']['Authorization']=='Bot NEVER_EXPORT_THIS'
    assert session.get.call_args.args[0].endswith('/users/@me')
    session.post.assert_not_called()

def test_unconfigured_no_network():
    from jarvis.testing.integration_probes150 import check
    session=Mock()
    assert check('Telegram',Config({'integrations.telegram.enabled':True}),session)['status']=='NOT_CONFIGURED'
    assert check('Discord',Config({}),session)['status']=='DISABLED'
    session.get.assert_not_called()

def test_pack_never_traverses_private_profile(tmp_path):
    from tools.test_all import pack
    root=tmp_path/'test';root.mkdir();(root/'private').mkdir();(root/'private/settings.json').write_text('SECRET_NOT_EXPORTED')
    archive=pack(root,{'status':'FINISHED'},None)
    with zipfile.ZipFile(archive) as z:
        assert 'private/settings.json' not in z.namelist()
        assert all(b'SECRET_NOT_EXPORTED' not in z.read(n) for n in z.namelist())

def test_not_run_probes_are_not_network_failure():
    from tools.startup_test import diagnostic_summary
    row=diagnostic_summary({'status':'BLOCKED_EXISTING_INSTANCE','network_summary':{},'exit_code':2})
    assert row['network_warning_targets']==[]

@pytest.mark.parametrize('mode,restored,keep',[('wave',False,False),('my_tracks',False,False),('playlist',False,False),('single',True,False),('single',False,True)])
def test_explicit_track_drops_generated_but_not_manual_queue(mode,restored,keep):
    from jarvis.integrations.discord_navigation import ReliableDiscordAdapter
    adapter=ReliableDiscordAdapter.__new__(ReliableDiscordAdapter);adapter._queue_lock=threading.RLock()
    state={'items':[{'title':'previous'}],'source_mode':mode,'source_epoch':7,'restored_queue':restored}
    adapter._queue_state=lambda p:state
    adapter._select_single_source({})
    assert state['source_mode']=='single' and state['source_epoch']==8
    assert bool(state['items'])==keep and not state['restored_queue']

def test_stale_wave_fetch_cannot_append_after_manual_track(monkeypatch):
    from jarvis.integrations.discord_navigation import ReliableDiscordAdapter
    import jarvis.integrations.discord_navigation as module
    adapter=ReliableDiscordAdapter.__new__(ReliableDiscordAdapter);adapter._queue_lock=threading.RLock()
    state={'items':[],'source_mode':'wave','source_epoch':7,'source_data':{}}
    adapter._queues={'guild':state};adapter._profile_key=lambda p:'guild';adapter._queue_state=lambda p:state
    entered=threading.Event();release=threading.Event();threads=[]
    def fetch(*a,**kw):entered.set();release.wait(3);return {'tracks':[{'id':'unwanted','title':'random'}]}
    adapter._yandex=SimpleNamespace(wave_tracks=fetch)
    adapter._entry_from_track=lambda track,**kw:{'track_id':track['id'],'title':track['title']}
    adapter._prefetch_upcoming=lambda *a,**k:None;adapter._broadcast_queue_snapshot=lambda *a,**k:None
    real_thread=threading.Thread
    def owned_thread(*args,**kwargs):
        thread=real_thread(*args,**kwargs);threads.append(thread);return thread
    monkeypatch.setattr(module.threading,'Thread',owned_thread)
    adapter._ensure_wave_queue_depth_background({})
    assert entered.wait(2)
    try:
        adapter._select_single_source({})
        # Even if a NEW Wave was started already, a previous Wave result is stale.
        state.update(source_mode='wave',source_epoch=9,wave_background_refill_active=True)
    finally:release.set()
    threads[0].join(3)
    assert not threads[0].is_alive() and state['items']==[]
    assert state['wave_background_refill_active'] is True
