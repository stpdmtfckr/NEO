"""Gateway lifecycle tests, no bot credentials or external connections."""
from __future__ import annotations
import asyncio
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import pytest

from jarvis.integrations.background import gateway_recovery as recovery


def owner():
    return SimpleNamespace(_closing=threading.Event(), _ready=threading.Event(),
        _failed=threading.Event(), _lock=threading.RLock(), _client=None, _error='',
        _music_dashboard_messages={1:'old-http-message'}, _timer_dashboard_messages={2:'old-http-message'})


def test_each_reconnect_gets_fresh_real_aiohttp_connector():
    import aiohttp
    bot=owner(); clients=[]; events=[]
    class Client:
        def __init__(self):
            clients.append(self)
            self.connector=aiohttp.TCPConnector()
            self.session=None
        async def login(self, token):
            self.session=aiohttp.ClientSession(connector=self.connector)
            assert not self.session.closed and not self.connector.closed
            events.append('login')
        async def connect(self, *, reconnect):
            assert reconnect is False
            if len(clients)==1:
                raise OSError('network changed fixture')
            bot._closing.set()
        async def close(self):
            if self.session is not None:
                await self.session.close()
            else:
                await self.connector.close()
            bot._network_changed.set()
        def clear(self):
            pytest.fail('Closed connector must not be reused with clear()')
    asyncio.run(recovery.run_gateway(bot,Client,'not-a-real-token'))
    assert len(clients)==2 and clients[0].connector is not clients[1].connector
    assert all(c.connector.closed for c in clients)
    assert events==['login','login']
    assert not bot._music_dashboard_messages and not bot._timer_dashboard_messages


@pytest.mark.parametrize('exception_name',['LoginFailure','PrivilegedIntentsRequired'])
def test_bad_credentials_are_not_retried(exception_name):
    bot=owner(); created=[]
    class Client:
        def __init__(self):created.append(self)
        async def login(self,token):raise type(exception_name,(Exception,),{})('fixture invalid auth')
        async def close(self):pass
    asyncio.run(recovery.run_gateway(bot,Client,'dummy'))
    assert len(created)==1 and bot._failed.is_set() and bot._ready.is_set()
    assert bot._gateway_phase=='failed'


def test_login_timeout_does_not_become_session_lifetime_timeout(monkeypatch):
    monkeypatch.setattr(recovery,'LOGIN_TIMEOUT',.025)
    bot=owner(); clients=[]
    class Client:
        def __init__(self):clients.append(self)
        async def login(self,token):
            if len(clients)==1:await asyncio.sleep(10)
        async def connect(self,*,reconnect):
            await asyncio.sleep(.06)
            bot._closing.set()
        async def close(self):bot._network_changed.set()
    asyncio.run(recovery.run_gateway(bot,Client,'dummy'))
    assert len(clients)==2


def test_close_failure_blocks_overlapping_replacement():
    bot=owner(); created=[]
    class Client:
        def __init__(self):created.append(self)
        async def login(self,token):raise OSError('offline fixture')
        async def close(self):raise RuntimeError('closing failed fixture')
    asyncio.run(recovery.run_gateway(bot,Client,'dummy'))
    assert len(created)==1 and bot._failed.is_set()
    assert 'teardown failed' in bot._error


def test_cancellation_propagates_and_closes_client():
    bot=owner(); closed=[]
    class Client:
        async def login(self,token):raise asyncio.CancelledError()
        async def close(self):closed.append(True)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(recovery.run_gateway(bot,Client,'dummy'))
    assert closed==[True] and not bot._failed.is_set()


def test_error_reason_and_traceback_keep_cause_but_not_actual_token(caplog):
    bot=owner(); credential='unstructured-private-fixture-credential'
    class Client:
        async def login(self,token):raise RuntimeError('Session is closed '+token)
        async def close(self):bot._closing.set()
    asyncio.run(recovery.run_gateway(bot,Client,credential))
    assert 'Session is closed' in caplog.text and 'RuntimeError' in caplog.text
    assert credential not in caplog.text and credential not in bot._error
    assert 'phase=login' in caplog.text


def test_repeated_errors_are_counted_and_rate_limited(caplog):
    bot=owner(); attempts=[]
    class Client:
        def __init__(self):attempts.append(self)
        async def login(self,token):raise OSError('unchanged offline fixture')
        async def close(self):
            bot._network_changed.set()
            if len(attempts)==4:bot._closing.set()
    asyncio.run(recovery.run_gateway(bot,Client,'dummy'))
    assert len(attempts)==4
    assert caplog.text.count('Gateway unavailable:')==1


def test_actual_discord_factory_runs_inside_async_loop_when_dependency_available(tmp_path):
    discord=pytest.importorskip('discord',reason='Requires the installed discord.py runtime; no network is used')
    from jarvis.integrations.background.discord_bot import DiscordBackgroundBot
    cfg=SimpleNamespace(paths=SimpleNamespace(root=tmp_path), get=lambda key, default=None:default)
    bot=DiscordBackgroundBot(cfg)
    async def run():
        first=bot._create_gateway_client(); second=bot._create_gateway_client()
        try:
            assert first is not second and first.http is not second.http
            assert hasattr(first,'on_ready') and hasattr(second,'on_ready')
        finally:
            await first.close();await second.close()
    asyncio.run(run())
