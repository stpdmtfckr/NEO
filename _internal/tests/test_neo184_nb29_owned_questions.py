"""NB29: only owned, unchanged question generations may be superseded by a fact.

Legacy global APIs deliberately retain their behavior; these tests cover the new
conditional cleanup path, not broad isolation of every follow-up resolver.
"""
import re
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jarvis.runtime import screen_help172 as screen, tidy172 as tidy
from jarvis.runtime import translation184 as translation, web_answers171 as web
from jarvis.runtime import everyday172, memory169
from jarvis.runtime.conversation_journal import conversation_scope
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked

MODULES = [screen, tidy, translation, web]


def seed(module, label='Synthetic question'):
    if module is screen:
        screen.ask(label)
    elif module is tidy:
        tidy.remember_plan(SimpleNamespace(folder=label))
    elif module is translation:
        translation.ask(translation.Request(label, 'ru', code='en'))
    else:
        web.remember(label, [web.related_offer(label)], 'en', full=label)


def active(module):
    return module.pending_plan() is not None if module is tidy else bool(module.pending())


def legacy_forget(module):
    if module is tidy:
        module.forget_plan()
    elif module is translation:
        module.forget_question()
    else:
        module.forget()


@pytest.fixture(autouse=True)
def clean_questions():
    with external_io_blocked():
        for module in MODULES:
            legacy_forget(module)
        yield
        for module in MODULES:
            legacy_forget(module)


@pytest.mark.parametrize('module', MODULES, ids=lambda m: m.__name__.split('.')[-1])
@pytest.mark.parametrize('owner', ['desktop', 'telegram:a', 'telegram:b'])
def test_creation_tags_owner_and_current_generation(module, owner):
    with conversation_scope(owner):
        seed(module)
        token = module.owned_question_token()
        assert token is not None
    foreign = 'telegram:other' if owner == 'desktop' else 'desktop'
    with conversation_scope(foreign):
        assert module.owned_question_token() is None
        assert module.forget_if_owned(token) is False
        assert module.forget_if_owned(None) is False
        assert active(module)
    with conversation_scope(owner):
        assert module.owned_question_token() is token
        assert module.forget_if_owned(token) is True
        assert not active(module)
        assert module.forget_if_owned(token) is False


@pytest.mark.parametrize('module', MODULES, ids=lambda m: m.__name__.split('.')[-1])
@pytest.mark.parametrize('replacement_scope', ['telegram:a', 'telegram:b'])
def test_stale_token_cannot_clear_replaced_question_even_with_identical_payload(module, replacement_scope):
    with conversation_scope('telegram:a'):
        seed(module)
        old = module.owned_question_token()
    with conversation_scope(replacement_scope):
        seed(module)  # intentionally identical content; ownership is not inferred from text
        new = module.owned_question_token()
        assert new is not None and new is not old
    with conversation_scope('telegram:a'):
        assert module.forget_if_owned(old) is False
    with conversation_scope(replacement_scope):
        assert module.owned_question_token() is new and active(module)


@pytest.mark.parametrize('module', MODULES, ids=lambda m: m.__name__.split('.')[-1])
@pytest.mark.parametrize('replacement_scope', ['telegram:a', 'desktop'])
def test_concurrent_replacement_wins_over_captured_cleanup_token(module, replacement_scope):
    captured, release = threading.Event(), threading.Event()
    results = []
    with conversation_scope('telegram:a'):
        seed(module, 'Before replacement')
    def cleanup():
        with conversation_scope('telegram:a'):
            token = module.owned_question_token()
            captured.set()
            assert release.wait(5)
            results.append(module.forget_if_owned(token))
    worker = threading.Thread(target=cleanup)
    worker.start()
    try:
        assert captured.wait(5)
        with conversation_scope(replacement_scope):
            seed(module, 'After replacement')
            replacement = module.owned_question_token()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and results == [False]
    with conversation_scope(replacement_scope):
        assert module.owned_question_token() is replacement and active(module)


@pytest.mark.parametrize('module', MODULES, ids=lambda m: m.__name__.split('.')[-1])
def test_unscoped_creation_defaults_to_desktop_and_legacy_clear_remains_global(module):
    seed(module)
    with conversation_scope('desktop'):
        assert module.owned_question_token() is not None
    with conversation_scope('telegram:a'):
        assert module.owned_question_token() is None
        legacy_forget(module)  # the old API intentionally keeps its old semantics
    assert not active(module)


@pytest.mark.parametrize('module', MODULES, ids=lambda m: m.__name__.split('.')[-1])
def test_expired_question_is_not_claimed_by_new_cleanup(module, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    with conversation_scope('telegram:a'):
        seed(module)
        assert module.owned_question_token() is not None
        clock[0] += 10000
        assert not active(module)
        assert module.owned_question_token() is None


def test_tidy_conditional_clear_preserves_existing_big_file_listing():
    tidy.remember_big([(10, 'synthetic-fixture.txt')])
    seed(tidy)
    token = tidy.owned_question_token()
    assert tidy.forget_if_owned(token)
    assert tidy.big_row(1) == (10, 'synthetic-fixture.txt')


@pytest.mark.parametrize('kind', ['which', 'year'])
def test_new_web_clarification_gets_its_own_origin_and_token(kind):
    with conversation_scope('desktop'):
        if kind == 'which':
            offers = [web.related_offer('First fixture'), web.related_offer('Second fixture')]
        else:
            offers = [web.Offer('other_year', 'Another year', '', re.compile('year'))]
        web.remember('Synthetic topic', offers, 'en')
        old = web.owned_question_token()
    with conversation_scope('telegram:a'):
        reply = web.resolve('yes')  # old global resolve behavior is unchanged
        assert reply['kind'] == kind
        new = web.owned_question_token()
        assert new is not None and new is not old
    with conversation_scope('desktop'):
        assert web.forget_if_owned(old) is False
    with conversation_scope('telegram:a'):
        assert web.owned_question_token() is new


@pytest.fixture(scope='module')
def session(tmp_path_factory):
    with external_io_blocked():
        value = AtlasSession(tmp_path_factory.mktemp('nb29_owned_questions') / 'run', locale='en')
        value.config.data['local_chat']['live_small_talk'] = False
        yield value
        value.finish()


@pytest.fixture
def rig(session, monkeypatch):
    session.reset_case('en')
    effects = []
    def forbidden(name):
        def handler(args):
            effects.append((name, dict(args)))
            raise AssertionError('No external effects in NB29 ownership checks: ' + name)
        return handler
    for name, definition in session.agent.tools._tools.items():
        if name != 'memory':
            monkeypatch.setattr(definition, 'handler', forbidden(name))
    session.agent.tools.set_execution_guard(lambda name, args, risk: session.calls.append((name, dict(args))) or None)
    yield session, effects
    assert effects == []


def transport(scope):
    return {} if scope == 'desktop' else {'input_source': 'telegram', 'conversation_id': scope.split(':', 1)[1]}


@pytest.mark.parametrize('question_scope,fact_scope', [
    ('desktop', 'desktop'), ('telegram:a', 'telegram:a'), ('telegram:b', 'telegram:b'),
    ('desktop', 'telegram:a'), ('telegram:a', 'desktop'), ('telegram:a', 'telegram:b'),
])
@pytest.mark.parametrize('old_confirmation', [False, True])
@pytest.mark.parametrize('write_error', [False, True])
def test_public_fact_clears_own_questions_preserves_foreign_and_drops_old_action(
        rig, monkeypatch, question_scope, fact_scope, old_confirmation, write_error):
    session, effects = rig
    agent = session.agent
    if old_confirmation:
        proposed = agent.handle('turn off the computer', **transport(fact_scope))
        assert proposed.pending is not None
    with conversation_scope(question_scope):
        for module in MODULES:
            seed(module, 'Owned synthetic fixture')
        before = [module.owned_question_token() for module in MODULES]
        everyday172.set_offer('Scoped memory fixture')
    if write_error:
        original = agent.database.set_memory
        def fail_note(key, value):
            if key.startswith('note:'):
                assert agent._pending_context is None
                raise sqlite3.OperationalError('synthetic SQLite failure')
            return original(key, value)
        monkeypatch.setattr(agent.database, 'set_memory', fail_note)
    turn = agent.handle('remember that Synthetic fact: do not run anything', **transport(fact_scope))
    assert turn.pending is None and agent._pending_context is None
    assert turn.tool_details == ['memory.remember_failed' if write_error else 'memory.remember_that']
    with conversation_scope(question_scope):
        after = [module.owned_question_token() for module in MODULES]
        if question_scope == fact_scope:
            assert after == [None] * 4 and not any(active(module) for module in MODULES)
            assert everyday172._offer_pending() == ''
        else:
            assert all(a is b for a, b in zip(after, before)) and all(active(module) for module in MODULES)
            assert everyday172._offer_pending() == 'Scoped memory fixture'
    agent.confirm_pending(True, **transport(fact_scope))
    assert session.calls == [] and effects == []


@pytest.mark.parametrize('replaced', MODULES, ids=lambda m: m.__name__.split('.')[-1])
def test_fact_snapshots_tokens_before_cleanup_and_keeps_later_replacement(rig, monkeypatch, replaced):
    session, _ = rig
    with conversation_scope('telegram:a'):
        for module in MODULES:
            seed(module, 'First question')
    replacement = []
    original = screen.forget_if_owned
    def replace_during_cleanup(token):
        seed(replaced, 'Question created after snapshot')
        replacement.append(replaced.owned_question_token())
        return original(token)
    monkeypatch.setattr(screen, 'forget_if_owned', replace_during_cleanup)
    session.agent.handle('remember that Synthetic replacement-race fact', input_source='telegram', conversation_id='a')
    with conversation_scope('telegram:a'):
        assert replacement and replaced.owned_question_token() is replacement[0]
        assert active(replaced)
        assert not any(active(module) for module in MODULES if module is not replaced)
    assert session.calls == []
