"""Real Agent/registry and queued approval edges with a fully inert OBS backend."""
from copy import deepcopy
from pathlib import Path
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from jarvis.runtime.streaming_commands184 import LANGUAGES, obs_input_scope
from jarvis.testing.isolation import external_io_blocked
from jarvis.tools.registry import ToolExecution

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = json.loads((ROOT / 'config/canonical_phrases/obs184.json').read_text(encoding='utf-8'))
VALUES = {'scene': ' edge Scene ', 'input': '<b>Mic</b>', 'source': ' Camera ', 'volume': '37.5'}
FIXTURE_CAPABILITIES = {'models_disabled_before_agent': False, 'scheduler_calls_suppressed': {}}


class FakeObs:
    def __init__(self):
        self.identity = ('inert-obs', 1)
        self.calls = []
        self.message = 'OBS fixture result'

    def confirmation_identity(self):
        return self.identity

    def execute(self, args, *, confirmed_stream=False, expected_identity=None):
        self.calls.append((deepcopy(args), confirmed_stream, expected_identity))
        allowed = args['action'] != 'stream_start' or (confirmed_stream and expected_identity == self.identity)
        return {'ok': allowed, 'status': 'changed' if allowed else 'confirmation_required', 'message': self.message}


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    from jarvis.testing.atlas_session import AtlasSession, AtlasConfig
    original_init = AtlasConfig.__init__
    def inert_profile(config, *args, **kwargs):
        original_init(config, *args, **kwargs)
        config.data.setdefault('neo_model', {}).update(enabled=False)
        config.data['neo_model'].setdefault('schedule', {})['enabled'] = False
        config.data.setdefault('llm_agent', {}).update(enabled=False, auto_install=False)
        config.data['llm_agent'].setdefault('finetune', {})['auto'] = False
        config.data.setdefault('local_chat', {}).update(enabled=False, live_small_talk=False, direct_chat=False)
        config.data.setdefault('plugins', {}).update(enabled=False, directory=str(config.paths.root / 'plugins'))
        config.data['ollama'].update(vision_enabled=False, requests_enabled=False, auto_start=False,
                                    auto_install=False, preload_model=False)
        config.save()
        FIXTURE_CAPABILITIES['models_disabled_before_agent'] = True
        FIXTURE_CAPABILITIES['direct_chat_disabled'] = True
        FIXTURE_CAPABILITIES['plugin_directory'] = str(config.paths.root / 'plugins')
    def suppress_scheduler(name):
        def disabled(config):
            assert not config.get('neo_model.enabled') and not config.get('llm_agent.enabled')
            counts = FIXTURE_CAPABILITIES['scheduler_calls_suppressed']
            counts[name] = counts.get(name, 0) + 1
        return disabled
    # These two constructor schedules are outside a routing test. Suppress only
    # their registration; the runner still counts and rejects any model call.
    with external_io_blocked(), patch.object(AtlasConfig, '__init__', inert_profile), \
            patch('jarvis.neo_model.jobs.start_scheduler', suppress_scheduler('neo_model')), \
            patch('jarvis.neo_llm.jobs.start_scheduler', suppress_scheduler('neo_llm')):
        previous_threads = set(threading.enumerate())
        result = AtlasSession(tmp_path_factory.mktemp('obs-agent') / 'session')
        for name, definition in result.agent.tools._tools.items():
            if name != 'obs_control':
                definition.handler = lambda args: ToolExecution(True, 'Inert unrelated tool')
        result.agent.tools.set_execution_guard(
            lambda name, args, risk: result.calls.append({'tool': name, 'arguments': deepcopy(args)}) or None)
        yield result
        result.finish()
        for worker in set(threading.enumerate()) - previous_threads:
            if worker.name == 'neo-grammar-warmup':
                worker.join(timeout=5)
                assert not worker.is_alive()


@pytest.fixture
def routed(session, monkeypatch):
    session.reset_case('en')
    fake = FakeObs()
    monkeypatch.setattr('jarvis.integrations.obs_service184.shared', lambda config: fake)
    # A permissive saved policy must never remove the fresh go-live question.
    monkeypatch.setattr(session.agent, '_confirmation_policy', lambda *args: False)
    with external_io_blocked():
        yield session, fake


def phrase(intent, code='en'):
    return CANONICAL['obs_control.' + intent][LANGUAGES.index(code)].format(
        **{key: value if key == 'volume' else '"' + value + '"' for key, value in VALUES.items()})


@pytest.mark.parametrize('code', LANGUAGES)
def test_all_twenty_actions_reach_real_agent_with_exact_literal_arguments(routed, code):
    session, fake = routed
    for intent in CANONICAL:
        session.reset_case(code)
        fake.calls.clear()
        action = intent.split('.')[1]
        from jarvis.runtime.streaming_commands184 import parse_obs_command
        text = phrase(action, code)
        expected = parse_obs_command(text, code)[1]
        turn = session.agent.handle(text)
        if action == 'stream_start':
            assert turn.pending and turn.pending.arguments == expected and turn.pending.confirmation_id
            assert not fake.calls and not session.calls
            assert session.agent.confirm_pending(False, confirmation_id=turn.pending.confirmation_id).pending is None
        else:
            assert not turn.pending and turn.success, (code, action, turn.text, session.calls)
            assert session.calls == [{'tool': 'obs_control', 'arguments': expected}]
            assert fake.calls == [(expected, False, None)]
            assert turn.tool_details == ['obs_control.' + action + ': success']


@pytest.mark.parametrize('source', ['text', 'voice', 'telegram'])
def test_fresh_confirmation_is_required_and_one_use_in_each_conversation(routed, source):
    session, fake = routed
    extra = {'input_source': source, 'conversation_id': 'fixture-chat'}
    turn = session.agent.handle(phrase('stream_start'), **extra)
    assert turn.pending and not fake.calls
    assert not session.agent.confirm_pending(True, **extra).success
    assert not fake.calls
    token = turn.pending.confirmation_id
    done = session.agent.confirm_pending(True, confirmation_id=token, **extra)
    assert done.success and fake.calls == [({'action': 'stream_start'}, True, fake.identity)]
    assert done.tool_details == ['obs_control.stream_start: success']
    assert not session.agent.confirm_pending(True, confirmation_id=token, **extra).success
    assert len(fake.calls) == 1


@pytest.mark.parametrize('code,yes', [('ru','да'), ('en','yes'), ('de','ja'), ('fr','oui'),
    ('es','sí'), ('it','sì'), ('pt','sim')])
def test_explicit_locale_reply_approves_only_existing_live_invitation(routed, code, yes):
    session, fake = routed
    session.reset_case(code)
    pending = session.agent.handle(phrase('stream_start', code))
    assert pending.pending and not fake.calls
    assert session.agent.handle(yes).success
    assert fake.calls == [({'action': 'stream_start'}, True, fake.identity)]


def test_reconnect_expiry_and_cancel_cannot_reuse_invitation(routed):
    session, fake = routed
    pending = session.agent.handle(phrase('stream_start')).pending
    fake.identity = ('inert-obs', 2)
    assert not session.agent.confirm_pending(True, confirmation_id=pending.confirmation_id).success
    assert not fake.calls
    pending = session.agent.handle(phrase('stream_start')).pending
    session.agent._pending_context['created_at'] -= 121
    assert not session.agent.confirm_pending(True, confirmation_id=pending.confirmation_id).success
    assert not fake.calls
    pending = session.agent.handle(phrase('stream_start')).pending
    session.agent.confirm_pending(False, confirmation_id=pending.confirmation_id)
    assert not session.agent.confirm_pending(True, confirmation_id=pending.confirmation_id).success
    assert not fake.calls


@pytest.mark.parametrize('replacement', ['obs', 'nonobs'])
def test_old_nonce_rejects_newer_obs_and_nonobs_pending_without_consuming_it(routed, replacement):
    session, fake = routed
    old = session.agent.handle(phrase('stream_start')).pending.confirmation_id
    new = session.agent.handle(phrase('stream_start')).pending.confirmation_id
    if replacement == 'nonobs':
        session.agent._pending_context = {'messages': [], 'call': {'function': {
            'name': 'application', 'arguments': {'action': 'open', 'name': 'fixture'}}}}
    context = session.agent._pending_context
    assert old != new
    assert not session.agent.confirm_pending(True, confirmation_id=old).success
    assert session.agent._pending_context is context and not fake.calls


def test_conversation_isolation_and_nonconfirmation_supersession(routed):
    session, fake = routed
    pending = session.agent.handle(phrase('stream_start'), input_source='telegram', conversation_id='one').pending
    assert not session.agent.confirm_pending(True, input_source='telegram', conversation_id='two',
                                            confirmation_id=pending.confirmation_id).success
    assert not fake.calls
    assert session.agent.handle(phrase('status'), input_source='telegram', conversation_id='one').success
    assert not session.agent.confirm_pending(True, input_source='telegram', conversation_id='one',
                                            confirmation_id=pending.confirmation_id).success
    assert [row[0]['action'] for row in fake.calls] == ['status']


def test_new_obs_command_supersedes_nonobs_pending_and_recovers_only_unbound_cancel(routed):
    session, fake = routed
    from jarvis.runtime.cancellation import cancellation_manager, OperationCancelled
    cancel = cancellation_manager()
    session.agent._pending_context = {'call': {'function': {'name': 'application', 'arguments': {}}}}
    cancel.cancel('inert previous cancellation')
    assert session.agent.handle(phrase('status')).success
    assert session.agent._pending_context is None
    assert [row[0]['action'] for row in fake.calls] == ['status']
    fake.calls.clear()
    generation = cancel.current_generation()
    cancel.cancel('inert queued cancellation')
    with cancel.bound_generation(generation):
        try:
            turn = session.agent.handle(phrase('status'))
        except OperationCancelled:
            pass
        else:
            assert not turn.success
        assert cancel.is_cancelled()
    assert not fake.calls


def test_registry_rejects_unbound_rewrite_and_capture_and_adapter_grant_is_one_use(routed):
    session, fake = routed
    from jarvis.tools.obs_tool184 import confirmed_stream_start
    for args in ({'action': 'status'}, {'action': 'stream_start'}, {'action': 'stream_start', 'confirmed': True}):
        assert not session.agent.tools.execute('obs_control', args).success
    with obs_input_scope('do not start streaming in OBS', 'en'):
        assert not session.agent.tools.execute('obs_control', {'action': 'stream_start'}).success
        assert not session.agent.tools.execute('capture', {'action': 'clip'}).success
    assert not fake.calls
    args = {'action': 'stream_start'}
    with confirmed_stream_start('obs_control', args, phrase('stream_start'), fake.identity):
        assert session.agent.tools.execute('obs_control', args).success
        assert not session.agent.tools.execute('obs_control', args).success
    assert [row[1] for row in fake.calls] == [True, False]


def test_original_raw_input_owns_repaired_voice_and_details_never_contain_result_html(routed):
    session, fake = routed
    text = phrase('scene_switch')
    fake.message = 'OBS scenes: <img src="file:///private"> C:/private/clip.mkv'
    turn = session.agent.handle_stream(text, raw_user_text=text, input_source='voice')
    assert turn.success and fake.calls[0][0]['scene'] == VALUES['scene']
    assert turn.tool_details == ['obs_control.scene_switch: success']
    assert 'file:///private' not in str(turn.tool_details)
    session.reset_case('en')
    fake.calls.clear()
    turn = session.agent.handle_stream(phrase('stream_start'), raw_user_text='start the show', input_source='voice')
    assert not fake.calls and (not turn.pending or turn.pending.tool_name != 'obs_control')


def desktop_harness(agent):
    from jarvis.ui.main_window import MainWindow
    harness = SimpleNamespace(agent=agent, _agent_generation=10, _language_code=lambda: 'en',
        _flush_response_stream=Mock(), _stream_flush_timer=Mock(), _append_tool_details=Mock(),
        _localize_text=lambda text: text, _pending_response_bubble=object(), chat=Mock(), _set_busy=Mock(),
        _stream_response_event=Mock(), _handle_agent_result=Mock(), _handle_agent_error=Mock(), thread_pool=Mock())
    harness._run_confirm_pending_stream = lambda *args, **kwargs: MainWindow._run_confirm_pending_stream(harness, *args, **kwargs)
    return harness


class HeldWorker:
    def __init__(self, function, *args, **kwargs):
        self.function, self.args = function, args
        self.signals = SimpleNamespace(progress=Mock(), result=Mock(), error=Mock())

    def run(self):
        return self.function(*self.args, on_event=lambda event: None)


@pytest.mark.parametrize('decision', ['yes', 'no', 'new-generation'])
def test_desktop_question_queues_exact_nonce_and_obeys_cancel_or_new_navigation(routed, monkeypatch, decision):
    session, fake = routed
    from jarvis.ui import main_window as desktop
    turn = session.agent.handle(phrase('stream_start'))
    h = desktop_harness(session.agent)
    def answer(*args):
        if decision == 'new-generation':
            h._agent_generation += 1
        return desktop.QMessageBox.No if decision == 'no' else desktop.QMessageBox.Yes
    question = Mock(side_effect=answer)
    monkeypatch.setattr(desktop.QMessageBox, 'question', question)
    monkeypatch.setattr(desktop, 'FunctionWorker', HeldWorker)
    desktop.MainWindow._handle_agent_turn(h, turn)
    assert question.call_args.args[-1] == desktop.QMessageBox.No
    assert not fake.calls
    if decision == 'new-generation':
        h.thread_pool.start.assert_not_called()
    else:
        worker = h.thread_pool.start.call_args.args[0]
        assert worker.args[-1] == turn.pending.confirmation_id
        worker.run()
        assert bool(fake.calls) == (decision == 'yes')


@pytest.mark.parametrize('replacement', ['obs', 'nonobs', 'generation'])
def test_queued_desktop_approval_cannot_confirm_a_later_request(routed, replacement):
    session, fake = routed
    h = desktop_harness(session.agent)
    pending = session.agent.handle(phrase('stream_start')).pending
    if replacement == 'generation':
        h._agent_generation += 1
    elif replacement == 'obs':
        session.agent.handle(phrase('stream_start'))
    else:
        session.agent._pending_context = {'call': {'function': {'name': 'application', 'arguments': {}}}}
    _, turn = h._run_confirm_pending_stream(True, 10, pending.confirmation_id, on_event=lambda event: None)
    assert not turn.success and not fake.calls


def telegram_harness(session, monkeypatch):
    from jarvis.integrations import telegram_remote as remote
    monkeypatch.setattr(remote, 'telegram_critical_phrase', lambda config: 'inert test phrase')
    controller = object.__new__(remote.TelegramRemoteController)
    controller.config, controller._agent = session.config, session.agent
    controller._lock = threading.Lock()
    controller._stop = threading.Event()
    controller._auth_failures = {}
    controller._secure_pending = controller._input = None
    controller._send_message = Mock()
    controller._notify_desktop = Mock()
    controller._roll_panel_to_bottom = Mock()
    controller._delete_one = Mock()
    controller._track_message = Mock()
    controller._answer_callback = Mock()
    controller._handle_pair = Mock(return_value=False)
    controller._message_center = Mock()
    controller._seen_callback_ids = {}
    controller._plain_confirm_by_chat = {}
    controller._view_by_chat = {}
    controller.config.data['integrations']['telegram'].update(
        remote_control_enabled=True, allowed_user_ids=['owner'], allowed_chat_ids=['one'])
    controller._tg_message = controller._tg_runtime_text = controller._tg_text = lambda value: value
    return controller


@pytest.mark.parametrize('decision', ['approve', 'wrong-phrase', 'expired', 'cancel', 'new-obs', 'new-nonobs'])
def test_telegram_secure_request_keeps_exact_nonce_and_never_confirms_later_action(routed, monkeypatch, decision):
    session, fake = routed
    remote = telegram_harness(session, monkeypatch)
    turn = session.agent.handle(phrase('stream_start'), input_source='telegram', conversation_id='one')
    remote._request_secure_action(kind='agent', chat_id='one', user_id='owner', label=turn.pending.summary,
        tool_name=turn.pending.tool_name, arguments=turn.pending.arguments, confirmation_id=turn.pending.confirmation_id)
    pending = remote._secure_pending
    assert pending.confirmation_id == turn.pending.confirmation_id and not fake.calls
    if decision == 'new-obs':
        session.agent.handle(phrase('stream_start'), input_source='telegram', conversation_id='one')
    elif decision == 'new-nonobs':
        session.agent._pending_context = {'call': {'function': {'name': 'application', 'arguments': {}}}}
    elif decision == 'expired':
        pending.expires_at = 0
    elif decision == 'cancel':
        remote._discard_agent_confirmation184(pending, 'one')
    remote._handle_critical_phrase('one', 'owner', 'wrong' if decision == 'wrong-phrase' else 'inert test phrase')
    assert bool(fake.calls) == (decision == 'approve')
    if decision == 'approve':
        assert fake.calls == [({'action': 'stream_start'}, True, fake.identity)]
        remote._handle_critical_phrase('one', 'owner', 'inert test phrase')
        assert len(fake.calls) == 1


@pytest.mark.parametrize('owner', ['slot', 'note', 'dictation', 'fact'])
def test_literal_payload_owners_keep_embedded_exact_obs_command(routed, owner):
    session, fake = routed
    text = phrase('stream_start')
    from jarvis.runtime.dialogue_tasks import PendingIntent, Slot
    if owner == 'slot':
        session.agent.conversations.get('desktop').pending_intent = PendingIntent(
            'text_edit', {'action': 'dictate'}, (Slot('text', 'text', 'text'),), 'en', time.monotonic() + 60)
    elif owner == 'note':
        session.agent._set_pending_overlay_followup('append_text', 'OBS Fixture Note')
    elif owner == 'dictation':
        text = 'type: ' + text
    else:
        text = 'remember that ' + text
    turn = session.agent.handle(text)
    assert not fake.calls and not turn.pending
    assert all(row['tool'] != 'obs_control' for row in session.calls)
    if owner in {'slot', 'dictation'}:
        assert session.calls == [{'tool': 'text_edit', 'arguments': {'action': 'dictate', 'text': phrase('stream_start')}}]
    elif owner == 'note':
        assert len(session.calls) == 1 and session.calls[0]['tool'] == 'overlay'
        assert session.calls[0]['arguments']['text'] == phrase('stream_start')


def test_pending_note_uses_original_obs_voice_payload_before_rewrite(routed):
    session, fake = routed
    session.agent._set_pending_overlay_followup('append_text', 'OBS Fixture Note')
    original = 'start recording in OBS'
    turn = session.agent.handle_stream('normalized replacement', raw_user_text=original, input_source='voice')
    assert not fake.calls and not turn.pending
    assert len(session.calls) == 1 and session.calls[0]['tool'] == 'overlay'
    assert session.calls[0]['arguments']['text'] == original


@pytest.mark.parametrize('text', [
    'do not start streaming in OBS', '"start streaming in OBS"',
    'what does start streaming in OBS mean', 'stop recording in OBS in 10 minutes',
    'in 10 minutes stop recording in OBS', 'stop OBS recording in 10 minutes',
    'switch OBS scene to Studio in 10 minutes', 'show source Camera in 10 minutes in OBS',
])
def test_negative_quoted_explanatory_and_delayed_obs_never_execute_immediately(routed, text):
    session, fake = routed
    turn = session.agent.handle(text)
    assert not fake.calls and (not turn.pending or turn.pending.tool_name != 'obs_control')
    assert session.calls == []


@pytest.mark.parametrize('user,chat,kind', [('intruder','one','private'), ('owner','two','private'),
    ('owner','one','group')])
def test_incoming_telegram_owner_boundaries_preserve_pending_stream(routed, monkeypatch, user, chat, kind):
    session, fake = routed
    remote = telegram_harness(session, monkeypatch)
    turn = session.agent.handle(phrase('stream_start'), input_source='telegram', conversation_id='one')
    remote._request_secure_action(kind='agent', chat_id='one', user_id='owner', label=turn.pending.summary,
        tool_name=turn.pending.tool_name, arguments=turn.pending.arguments, confirmation_id=turn.pending.confirmation_id)
    pending = remote._secure_pending
    message = {'message_id': 1, 'text': 'inert test phrase', 'from': {'id': user}, 'chat': {'id': chat, 'type': kind}}
    remote._handle_message(message)
    remote._handle_callback({'id': 'fixture-callback', 'data': 'ui:cancel', 'from': {'id': user}, 'message': message})
    assert remote._secure_pending is pending and not fake.calls
    remote._delete_one.assert_not_called()


def test_authorized_telegram_message_deletes_phrase_and_callback_cancel_consumes_nonce(routed, monkeypatch):
    session, fake = routed
    remote = telegram_harness(session, monkeypatch)
    for cancel in (True, False):
        turn = session.agent.handle(phrase('stream_start'), input_source='telegram', conversation_id='one')
        remote._request_secure_action(kind='agent', chat_id='one', user_id='owner', label=turn.pending.summary,
            tool_name=turn.pending.tool_name, arguments=turn.pending.arguments, confirmation_id=turn.pending.confirmation_id)
        message = {'message_id': 2, 'text': 'inert test phrase', 'from': {'id': 'owner'}, 'chat': {'id': 'one', 'type': 'private'}}
        if cancel:
            remote._handle_callback({'id': 'cancel-live', 'data': 'ui:cancel', 'from': {'id': 'owner'}, 'message': message})
            assert remote._secure_pending is None and not fake.calls
            assert not session.agent.confirm_pending(True, input_source='telegram', conversation_id='one',
                                                    confirmation_id=turn.pending.confirmation_id).success
        else:
            remote._handle_message(message)
            remote._delete_one.assert_called_once_with('one', 2)
            assert fake.calls == [({'action': 'stream_start'}, True, fake.identity)]


def test_generated_catalogue_matches_real_registry_including_obs_and_scenario_retry(routed):
    session, _ = routed
    from jarvis.security import Risk
    generated = json.loads((ROOT / 'tests/fixtures/command_catalogue.json').read_text(encoding='utf-8'))
    rows = {row['tool']: row for row in generated['tools']}
    assert set(rows) == set(session.agent.tools._tools)
    assert generated['tool_count'] == len(rows) == 62
    assert generated['enumerated_action_count'] == sum(len(row['actions']) for row in rows.values()) == 598  # 186: + system_monitor.battery; 187: + timer.cancel_all, media.next/previous (HREQ-026); 188: + knowledge.ask_file/compare (NEO-DOC-001), show/open/reveal_source (NEO-DOC-002), office.chart (NEO-OFFICE-001), office.copy_formula (NEO-OFFICE-002)
    for name, definition in session.agent.tools._tools.items():
        assert rows[name]['schema'] == definition.parameters, name
        assert rows[name]['actions'] == definition.parameters.get('properties', {}).get('action', {}).get('enum', []), name
    assert rows['scenario']['actions'] == ['list', 'run', 'retry']
    assert rows['obs_control']['plugin_owner'] == session.agent.tools.tool_plugin_owners['obs_control']
    for action in rows['obs_control']['actions']:
        assert session.agent.tools.risk_for('obs_control', {'action': action}) == (Risk.HIGH if action == 'stream_start' else Risk.SAFE)
