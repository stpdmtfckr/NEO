"""R034/NB28: prepare semantic tables without execution or exact-route stalls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import os
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from jarvis.runtime.atlas2 import PhraseAtlas2
from jarvis.runtime.router import hybrid_router
from jarvis.runtime import speed174
from jarvis.testing.isolation import external_io_blocked


@pytest.fixture(autouse=True)
def isolated_io():
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError('Semantic preparation must not access external I/O')

    with ExitStack() as stack:
        stack.enter_context(external_io_blocked())
        for target, name in ((socket.socket, 'sendto'), (os, 'system')):
            stack.enter_context(patch.object(target, name, forbidden))
        yield
    assert not attempts


def agent_for(atlas):
    return SimpleNamespace(atlas2=atlas, router=hybrid_router(), config=atlas.config,
                           tools=SimpleNamespace(execute=Mock(side_effect=AssertionError('No commands'))),
                           ollama=SimpleNamespace(chat=Mock(side_effect=AssertionError('No model'))))


def tiny_rows(language, *, data=None):
    return [{'intent': 'screen.capture', 'tool': 'screen', 'action': 'capture',
             'arguments': {'action': 'capture'}, 'domain': 'system',
             'phrase': 'take a screenshot', 'language': language, 'executable': True}]


def invalidate_version(monkeypatch, mode='missing'):
    # r3 reads binary dependencies for both load and generation identity.
    # This fixture exposes one consistent logical missing/stale stamp to both.
    if mode == 'missing':
        original = PhraseAtlas2._open_dependency

        def open_dependency(path):
            if path.name.startswith('semantic_') and path.suffix == '.version':
                raise FileNotFoundError(2, 'In-memory missing cache simulation', str(path))
            return original(path)

        monkeypatch.setattr(PhraseAtlas2, '_open_dependency', staticmethod(open_dependency))
    else:
        from dataclasses import replace
        import hashlib
        import zlib
        original = PhraseAtlas2._read_dependency
        stale = b'stale-version'

        def read_dependency(cls, path, *, content=False):
            payload, identity = original(path, content=content)
            if path.name.startswith('semantic_') and path.suffix == '.version':
                # Synthetic identity belongs to the injected logical bytes. It
                # is not evidence about the unchanged on-disk version stamp.
                identity = replace(identity, stat=(*identity.stat[:2], len(stale), *identity.stat[3:]),
                                   sha256=hashlib.sha256(stale).hexdigest(),
                                   crc32=zlib.crc32(stale))
                return stale if content else b'', identity
            return payload, identity

        monkeypatch.setattr(PhraseAtlas2, '_read_dependency', classmethod(read_dependency))


def test_static_warmup_also_loads_valid_shipped_semantic_cache(monkeypatch):
    atlas = PhraseAtlas2({'neo_model.enabled': False})
    agent = agent_for(atlas)
    builds = Mock(side_effect=AssertionError('Valid shipped cache must not rebuild'))
    monkeypatch.setattr(atlas._vectorizer, 'encode', builds)
    timings = speed174.warm_up(agent, 'ru')
    assert list(atlas._semantic_cache) == ['ru']
    assert 'atlas.semantic' in timings
    assert list(timings)[-1] == 'atlas.semantic'
    rows, matrix = atlas._semantic_cache['ru']
    assert len(rows) == 2507 and isinstance(matrix, np.memmap)
    assert not matrix.flags.writeable
    atlas.prepare_semantic('ru')
    assert atlas._semantic_cache['ru'][1] is matrix
    assert builds.call_count == 0
    assert agent.tools.execute.call_count == agent.ollama.chat.call_count == 0


def test_missing_cache_is_built_on_background_caller_before_first_typo(monkeypatch):
    atlas = PhraseAtlas2({'neo_model.enabled': False})
    agent = agent_for(atlas)
    invalidate_version(monkeypatch)
    builds = []
    original_encode = atlas._vectorizer.encode

    def encode(texts):
        texts = list(texts)
        builds.append((len(texts), threading.get_ident()))
        return original_encode(texts)

    monkeypatch.setattr(atlas._vectorizer, 'encode', encode)
    with ThreadPoolExecutor(max_workers=1) as worker:
        timings = worker.submit(speed174.warm_up, agent, 'ru').result(timeout=60)
    assert 'atlas.semantic' in timings
    assert len(builds) == 1 and builds[0][0] > 20000
    assert builds[0][1] != threading.get_ident()
    rows, matrix = atlas._semantic_cache['ru']
    assert matrix.shape == (len(rows), atlas._vectorizer.dimension)
    assert matrix.dtype == np.float32
    started = time.perf_counter()
    result = atlas.resolve('открйо настройки звука', router=agent.router, language='ru')
    elapsed = (time.perf_counter() - started) * 1000
    assert len(builds) == 1
    assert result.source in {'semantic.vector', 'fallback'}
    assert agent.tools.execute.call_count == agent.ollama.chat.call_count == 0
    print(f'background_rebuild_ms={timings["atlas.semantic"]}; first_typo_after_ms={elapsed:.2f}; rows={len(rows)}')


@pytest.mark.parametrize('disabled_key', ['routing.atlas2_enabled', 'routing.semantic_enabled'])
def test_disabled_semantics_do_not_prepare(disabled_key, monkeypatch):
    atlas = PhraseAtlas2({disabled_key: False, 'neo_model.enabled': False})
    prepare = Mock(side_effect=AssertionError('Disabled semantic table must stay cold'))
    monkeypatch.setattr(atlas, '_semantic_index', prepare)
    speed174.warm_up(agent_for(atlas), 'ru')
    atlas.prepare_semantic('de')
    assert not atlas._semantic_cache and prepare.call_count == 0


@pytest.mark.parametrize('invalid', ['missing', 'stale'])
def test_only_requested_locale_prepared_and_cache_remains_bounded(invalid, monkeypatch):
    atlas = PhraseAtlas2({})
    invalidate_version(monkeypatch, invalid)
    locales = []

    def seeds(language, *, data=None):
        locales.append(language)
        return tiny_rows(language)

    monkeypatch.setattr(atlas, '_seed_rows', seeds)
    for requested, expected in [(' DE ', 'de'), ('de', 'de'), ('en', 'en'), ('fr', 'fr'),
                                ('es', 'es'), ('it', 'it'), ('pt', 'pt'), ('invalid', 'ru')]:
        atlas.prepare_semantic(requested)
        assert list(atlas._semantic_cache) == [expected]
    assert locales == ['de', 'en', 'fr', 'es', 'it', 'pt', 'ru']


def test_background_build_deduplicates_semantic_waiters_without_blocking_exact(monkeypatch):
    atlas = PhraseAtlas2({})
    invalidate_version(monkeypatch)
    monkeypatch.setattr(atlas, '_seed_rows', tiny_rows)
    # Populate the exact route before starting the background rebuild, as warm_up does.
    assert atlas.exact_hash_match('take a screenshot', language='en').intent == 'screen.capture'
    entered = threading.Event()
    release = threading.Event()
    requested = threading.Event()
    builds = []
    original_encode = atlas._vectorizer.encode

    def slow_encode(texts):
        builds.append(threading.get_ident())
        entered.set()
        assert release.wait(timeout=10), 'Test did not release the inert encoder'
        return original_encode(texts)

    def semantic_request():
        requested.set()
        # A cold/in-progress request must wait, not silently skip semantic routing.
        return atlas.semantic_match('take a screenshot', language='en')

    monkeypatch.setattr(atlas._vectorizer, 'encode', slow_encode)
    with ThreadPoolExecutor(max_workers=4) as workers:
        try:
            background = workers.submit(atlas.prepare_semantic, 'en')
            assert entered.wait(timeout=5)
            repeated = workers.submit(atlas.prepare_semantic, 'en')
            semantic = workers.submit(semantic_request)
            assert requested.wait(timeout=5)
            exact = workers.submit(atlas.exact_hash_match, 'take a screenshot', language='en')
            assert exact.result(timeout=2).intent == 'screen.capture'
            static = atlas.resolve('static fixture', router=SimpleNamespace(route=lambda *a, **k: 'static'), language='en')
            assert static.routed == 'static' and static.source == 'static.regex'
            assert not semantic.done() and not repeated.done()
        finally:
            release.set()
        background.result(timeout=5)
        repeated.result(timeout=5)
        assert semantic.result(timeout=5).intent == 'screen.capture'
    assert len(builds) == 1 and list(atlas._semantic_cache) == ['en']


def test_failed_background_preparation_can_retry_without_partial_cache(monkeypatch):
    atlas = PhraseAtlas2({'neo_model.enabled': False})
    invalidate_version(monkeypatch)
    monkeypatch.setattr(atlas, '_seed_rows', tiny_rows)
    original_encode = atlas._vectorizer.encode
    builds = Mock(side_effect=[RuntimeError('inert interrupted build'), original_encode(['take a screenshot'])])
    monkeypatch.setattr(atlas._vectorizer, 'encode', builds)
    agent = agent_for(atlas)
    agent.router = SimpleNamespace(route=lambda *a, **k: 'static')
    timings = speed174.warm_up(agent, 'en')
    assert 'atlas.semantic' in timings and not atlas._semantic_cache
    atlas.prepare_semantic('en')
    assert list(atlas._semantic_cache) == ['en'] and builds.call_count == 2


def test_locale_switch_serializes_builds_and_keeps_only_latest_matrix(monkeypatch):
    atlas = PhraseAtlas2({})
    invalidate_version(monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    switched = threading.Event()
    locales = []

    def seeds(language, *, data=None):
        locales.append(language)
        if language == 'en':
            entered.set()
            assert release.wait(timeout=10)
        return tiny_rows(language)

    def switch():
        switched.set()
        atlas.prepare_semantic('de')

    monkeypatch.setattr(atlas, '_seed_rows', seeds)
    with ThreadPoolExecutor(max_workers=2) as workers:
        try:
            first = workers.submit(atlas.prepare_semantic, 'en')
            assert entered.wait(timeout=5)
            second = workers.submit(switch)
            assert switched.wait(timeout=5)
            assert locales == ['en'] and not second.done()
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert locales == ['en', 'de'] and list(atlas._semantic_cache) == ['de']


@pytest.mark.parametrize('text,intent,executable', [
    ('take a screenshot', 'screen.capture', True),
    ('do not take a screenshot', 'screen.capture', False),
    ('delete the file', 'filesystem.delete', False),
])
def test_preparation_preserves_semantic_execution_guards(text, intent, executable, monkeypatch):
    atlas = PhraseAtlas2({})
    invalidate_version(monkeypatch)
    rows = tiny_rows('en') + [{'intent': 'filesystem.delete', 'tool': 'filesystem',
                              'action': 'delete', 'arguments': {'action': 'delete'},
                              'domain': 'system', 'phrase': 'delete the file',
                              'language': 'en', 'executable': False}]
    monkeypatch.setattr(atlas, '_seed_rows', lambda language, *, data=None: rows)
    before = atlas.semantic_match(text, language='en')
    atlas.prepare_semantic('en')
    after = atlas.semantic_match(text, language='en')
    assert before == after and after.intent == intent and after.executable is executable
