"""NB35/R054: an application menu shown by NEO is answered by «1», «второй», «second»…"""
from types import SimpleNamespace

import pytest

from jarvis.runtime import everyday172, local_chat169, reply_memory168
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked

OPTIONS = [{'label': 'Cheat Engine', 'kind': 'app', 'value': 'C:/fixture/ce.exe', 'score': 0.81},
           {'label': 'Cheat Engine Tutorial', 'kind': 'app', 'value': 'C:/fixture/tut.exe', 'score': 0.80}]


@pytest.fixture
def session(tmp_path):
    with external_io_blocked():
        everyday172.clear_offer(); reply_memory168.clear(); local_chat169.clear_history()
        value = AtlasSession(tmp_path / 'run')
        value.config.data['local_chat']['live_small_talk'] = False
        yield value
        value.finish()
        everyday172.clear_offer(); reply_memory168.clear(); local_chat169.clear_history()


def _menu(agent):
    execution = SimpleNamespace(success=False, text='menu', data={'requires_clarification': True, 'options': OPTIONS})
    agent._remember_execution_context('application', {'action': 'open', 'name': 'cheat', 'kind': 'app'}, execution)
    assert agent._pending_target_choice['options'][1]['label'] == 'Cheat Engine Tutorial'


@pytest.mark.parametrize('code,reply,label', [
    ('ru', '2', 'Cheat Engine Tutorial'), ('ru', 'второй', 'Cheat Engine Tutorial'), ('ru', 'первый вариант', 'Cheat Engine'),
    ('en', 'the second one', 'Cheat Engine Tutorial'), ('en', '1', 'Cheat Engine'),
    ('de', 'zweite', 'Cheat Engine Tutorial'), ('es', 'el primero', 'Cheat Engine'),
])
def test_numbered_reply_selects_the_shown_option(session, code, reply, label):
    session.config.data['app']['language'] = code
    agent = session.agent
    _menu(agent)
    agent.handle(reply)
    calls = [c for c in session.calls if c['tool'] == 'application']
    assert calls and calls[-1]['arguments']['name'] == label
    assert agent._pending_target_choice is None


def test_decline_and_expiry_drop_the_menu(session):
    session.config.data['app']['language'] = 'ru'
    agent = session.agent
    _menu(agent)
    agent.handle('отмена')
    assert agent._pending_target_choice is None
    _menu(agent)
    agent._pending_target_choice['expires_at'] = 0
    agent.handle('2')
    assert not [c for c in session.calls if c['tool'] == 'application' and c['arguments'].get('name') == 'Cheat Engine Tutorial']
