"""Inert regressions for settings refresh and Atlas dependency identity."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import zlib

import numpy as np
import pytest

from jarvis.runtime import atlas2 as atlas_module
from jarvis.runtime.atlas2 import PhraseAtlas2


def corpus(phrase='alpha screenshot'):
    return json.dumps({'action_phrases': {'screen.capture': [phrase]}}, sort_keys=True).encode()


@pytest.fixture
def local_atlas(tmp_path, monkeypatch):
    files = tmp_path / 'commands'
    files.mkdir()
    for locale in ('en', 'ru', 'de'):
        (files / (locale + '.json')).write_bytes(corpus())
    monkeypatch.setattr(PhraseAtlas2, '_commands_path', staticmethod(lambda locale: files / (locale + '.json')))
    monkeypatch.setattr(PhraseAtlas2, '_curated_rows', lambda self: [])
    # Redirect only the test module's resource anchor; never mutate shipped data.
    monkeypatch.setattr(atlas_module, '__file__', str(tmp_path / 'jarvis/runtime/atlas2.py'))
    config = {'app.language': 'en', 'routing.semantic_dimension': 384}
    memory = object()
    return SimpleNamespace(root=tmp_path, files=files, config=config, memory=memory,
                           atlas=PhraseAtlas2(config, context_memory=memory))


def refresh(owner, monkeypatch):
    """Run real Agent refresh with every unrelated service boundary inert."""
    from jarvis import agent as module
    monkeypatch.setattr(module, 'resource_budget', lambda: SimpleNamespace(configure=lambda _config: None))
    monkeypatch.setattr(module, 'LazyVisionGate', lambda _config: object())
    monkeypatch.setattr(module, 'GameWebKnowledgeService', lambda *a, **k: SimpleNamespace(close=lambda: None))
    agent = SimpleNamespace(config=owner.config, context_memory=owner.memory, atlas2=owner.atlas,
                            router=object(), ollama=SimpleNamespace(refresh_model_settings=lambda: None),
                            game_web=SimpleNamespace(close=lambda: None), _active_game_id=lambda: '',
                            _lock=threading.RLock())
    lock = agent._lock
    module.Agent.refresh_runtime_settings(agent)
    assert agent._lock is lock
    assert agent.domain_dispatcher.atlas2 is agent.atlas2
    return agent.atlas2


def warm(owner):
    owner.atlas._exact_index('en')
    owner.atlas.prepare_semantic('en')
    assert owner.atlas.compatible_with(owner.config, context_memory=owner.memory)


def test_appearance_save_retains_exact_and_semantic_objects(local_atlas, monkeypatch):
    owner = local_atlas
    warm(owner)
    exact, semantic = owner.atlas._exact_cache['en'], owner.atlas._semantic_cache['en']
    owner.config['ui.window_opacity_percent'] = 73
    result = refresh(owner, monkeypatch)
    assert result is owner.atlas
    assert result._exact_cache['en'] is exact and result._semantic_cache['en'] is semantic


@pytest.mark.parametrize('key,value', [
    ('app.language', 'de'), ('routing.semantic_dimension', 768),
    ('routing.atlas2_enabled', False), ('routing.static_hash_enabled', False),
    ('routing.semantic_enabled', False),
])
def test_setting_dependency_change_replaces(local_atlas, monkeypatch, key, value):
    owner = local_atlas
    warm(owner)
    owner.config[key] = value
    replacement = refresh(owner, monkeypatch)
    assert replacement is not owner.atlas
    assert replacement.config is owner.config and replacement.memory is owner.memory
    assert not replacement._semantic_cache and not replacement._exact_cache
    if key == 'routing.semantic_dimension':
        replacement.prepare_semantic('en')
        assert replacement._semantic_cache['en'][1].shape == (1, 768)
    elif key == 'app.language':
        replacement.prepare_semantic('de')
        assert list(replacement._semantic_cache) == ['de']
    elif key in ('routing.atlas2_enabled', 'routing.semantic_enabled'):
        replacement.prepare_semantic('en')
        assert not replacement._semantic_cache
        assert replacement.semantic_match('alpha screenshot', language='en') is None


@pytest.mark.parametrize('kind', ['config', 'context'])
def test_owner_replacement_rebinds(local_atlas, monkeypatch, kind):
    owner = local_atlas
    warm(owner)
    if kind == 'config':
        owner.config = deepcopy(owner.config)
    else:
        owner.memory = object()
    result = refresh(owner, monkeypatch)
    assert result is not owner.atlas and result.config is owner.config and result.memory is owner.memory


@pytest.mark.parametrize('initial,replacement,effective', [(0, 384, 384), (1, 191, 192), (4096, 2048, 2048)])
def test_equal_effective_dimension_retains(local_atlas, initial, replacement, effective):
    owner = local_atlas
    owner.config['routing.semantic_dimension'] = initial
    owner.atlas = PhraseAtlas2(owner.config, context_memory=owner.memory)
    warm(owner)
    owner.config['routing.semantic_dimension'] = replacement
    assert owner.atlas._vectorizer.dimension == effective
    assert owner.atlas.compatible_with(owner.config, context_memory=owner.memory)


def test_normalized_locale_and_live_thresholds_retain(local_atlas):
    owner = local_atlas
    warm(owner)
    owner.config.update({'app.language': ' EN ', 'routing.semantic_threshold': 1.1, 'routing.semantic_margin': .2})
    assert owner.atlas.compatible_with(owner.config, context_memory=owner.memory)
    assert owner.atlas.semantic_match('alpha screenshot', language='en') is None


def test_same_size_content_invalidates_even_with_equal_stat_signature(local_atlas, monkeypatch):
    owner = local_atlas
    path = owner.files / 'en.json'
    fixed_stat = owner.atlas._file_stat(path.stat())
    monkeypatch.setattr(PhraseAtlas2, '_file_stat', staticmethod(lambda value: fixed_stat))
    warm(owner)
    before = path.read_bytes()
    after = corpus('omega screenshot')
    assert len(before) == len(after) and before != after
    path.write_bytes(after)
    assert not owner.atlas.compatible_with(owner.config, context_memory=owner.memory)
    replacement = refresh(owner, monkeypatch)
    assert replacement.exact_hash_match('alpha screenshot', language='en') is None
    assert replacement.exact_hash_match('omega screenshot', language='en').intent == 'screen.capture'
    replacement.prepare_semantic('en')
    assert replacement._semantic_cache['en'][0][0]['phrase'] == 'omega screenshot'


def test_lazy_command_aba_is_bound_to_b_generation(local_atlas, monkeypatch):
    owner = local_atlas
    path = owner.files / 'en.json'
    original = path.read_bytes()
    path.write_bytes(corpus('omega screenshot'))
    owner.atlas.prepare_semantic('en')
    generation = owner.atlas._semantic_generations['en']
    assert generation[0].sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_bytes(original)
    assert not owner.atlas.compatible_with(owner.config, context_memory=owner.memory)
    replacement = refresh(owner, monkeypatch)
    replacement.prepare_semantic('en')
    assert replacement._semantic_cache['en'][0][0]['phrase'] == 'alpha screenshot'


def test_language_memo_does_not_feed_stale_a_into_b_build(local_atlas):
    owner = local_atlas
    owner.atlas._exact_index('en')
    (owner.files / 'en.json').write_bytes(corpus('omega screenshot'))
    owner.atlas.prepare_semantic('en')
    assert owner.atlas._semantic_cache['en'][0][0]['phrase'] == 'omega screenshot'
    assert not owner.atlas.compatible_with(owner.config, context_memory=owner.memory)


def test_generation_change_during_encode_refuses_retention(local_atlas, monkeypatch):
    owner = local_atlas
    real_encode = owner.atlas._vectorizer.encode
    def changed(texts):
        result = real_encode(texts)
        (owner.files / 'en.json').write_bytes(corpus('omega screenshot'))
        return result
    monkeypatch.setattr(owner.atlas._vectorizer, 'encode', changed)
    owner.atlas.prepare_semantic('en')
    assert owner.atlas._semantic_cache['en'][0][0]['phrase'] == 'alpha screenshot'
    assert owner.atlas._semantic_generations['en'] is None
    assert not owner.atlas.compatible_with(owner.config, context_memory=owner.memory)


def test_busy_semantic_build_does_not_wait_on_refresh(local_atlas, monkeypatch):
    owner = local_atlas
    entered, release = threading.Event(), threading.Event()
    real_encode = owner.atlas._vectorizer.encode
    failures = []
    def paused(texts):
        entered.set()
        assert release.wait(5)
        return real_encode(texts)
    def build():
        try:
            owner.atlas.prepare_semantic('en')
        except BaseException as exc:
            failures.append(exc)
    monkeypatch.setattr(owner.atlas._vectorizer, 'encode', paused)
    worker = threading.Thread(target=build, name='inert-atlas-build')
    worker.start()
    try:
        assert entered.wait(3)
        started = time.monotonic()
        assert refresh(owner, monkeypatch) is not owner.atlas
        assert time.monotonic() - started < 1
        assert not release.is_set()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not failures


def test_cache_and_generation_metadata_keep_one_locale(local_atlas):
    atlas = local_atlas.atlas
    for locale in ('en', 'de', 'ru', 'en'):
        atlas.prepare_semantic(locale)
        atlas._exact_index(locale)
        assert list(atlas._semantic_cache) == list(atlas._semantic_generations) == [locale]
        assert list(atlas._exact_cache) == list(atlas._exact_generations) == [locale]
        assert atlas._language_cache[0] == locale


@pytest.mark.parametrize('key', ['routing.atlas2_enabled', 'routing.semantic_enabled'])
def test_initial_disabled_state_stays_cold(local_atlas, key):
    owner = local_atlas
    owner.config[key] = False
    atlas = PhraseAtlas2(owner.config, context_memory=owner.memory)
    atlas.prepare_semantic('en')
    assert not atlas._semantic_cache
    assert atlas.semantic_match('alpha screenshot', language='en') is None
    assert not atlas.compatible_with(owner.config, context_memory=owner.memory)


@pytest.mark.parametrize('failure', ['missing', 'oversized', 'nonregular', 'unreadable'])
def test_unprovable_save_dependency_refuses_without_uncapped_read(local_atlas, monkeypatch, failure):
    owner = local_atlas
    warm(owner)
    path = owner.files / 'en.json'
    monkeypatch.setattr(PhraseAtlas2, '_read_build_dependency', classmethod(lambda *a: pytest.fail('Save tried build-only read')))
    if failure == 'missing':
        path.unlink()
    elif failure == 'oversized':
        with path.open('r+b') as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
    elif failure == 'nonregular':
        path.unlink()
        path.mkdir()
    else:
        monkeypatch.setattr(PhraseAtlas2, '_open_dependency', staticmethod(lambda _path: (_ for _ in ()).throw(PermissionError('inert denial'))))
    assert not owner.atlas.compatible_with(owner.config, context_memory=owner.memory)


def make_shipped(owner):
    root = owner.root / 'config/atlas2/cache'
    root.mkdir(parents=True)
    rows = owner.atlas._seed_rows('en')
    (root / 'semantic_en.rows.json').write_text(json.dumps(rows), encoding='utf-8')
    (root / 'semantic_en.version').write_text('unicode-v2:' + str(zlib.crc32((owner.files / 'en.json').read_bytes())), encoding='utf-8')
    np.save(root / 'semantic_en.npy', owner.atlas._vectorizer.encode(row['phrase'] for row in rows))
    return root


@pytest.mark.parametrize('changed', ['none', 'rows', 'matrix', 'stamp'])
def test_shipped_cache_dependencies(local_atlas, monkeypatch, changed):
    owner = local_atlas
    root = make_shipped(owner)
    monkeypatch.setattr(owner.atlas._vectorizer, 'encode', lambda *a: pytest.fail('Valid shipped cache rebuilt'))
    owner.atlas.prepare_semantic('en')
    matrix = owner.atlas._semantic_cache['en'][1]
    assert isinstance(matrix, np.memmap) and not matrix.flags.writeable
    assert owner.atlas.compatible_with(owner.config, context_memory=owner.memory)
    if changed == 'rows':
        path = root / 'semantic_en.rows.json'
        path.write_bytes(path.read_bytes().replace(b'alpha', b'omega'))
    elif changed == 'matrix':
        path = root / 'semantic_en.npy'
        with path.open('r+b') as stream:
            stream.seek(matrix.offset)
            stream.write(np.float32(.25).tobytes())
    elif changed == 'stamp':
        path = root / 'semantic_en.version'
        path.write_bytes(path.read_bytes().replace(b'unicode', b'xnicode'))
    assert owner.atlas.compatible_with(owner.config, context_memory=owner.memory) is (changed == 'none')


def test_mapped_b_payload_cannot_be_labelled_as_a(local_atlas, monkeypatch):
    owner = local_atlas
    root = make_shipped(owner)
    other = root / 'other.npy'
    np.save(other, np.ones((1, 384), dtype=np.float32))
    real_load = np.load
    monkeypatch.setattr(atlas_module.np, 'load', lambda *a, **kw: real_load(other, **kw))
    owner.atlas.prepare_semantic('en')
    assert owner.atlas._semantic_generations['en'] is None
    assert not owner.atlas.compatible_with(owner.config, context_memory=owner.memory)


@pytest.mark.parametrize('broken', ['missing_version', 'invalid_version', 'invalid_rows', 'invalid_matrix'])
def test_repaired_shipped_dependency_invalidates_fallback(local_atlas, broken):
    """Repair must retry shipped data even when command bytes are unchanged."""
    owner = local_atlas
    root = make_shipped(owner)
    suffix = ('version' if broken.endswith('version') else 'rows.json' if broken.endswith('rows') else 'npy')
    path = root / ('semantic_en.' + suffix)
    original = path.read_bytes()
    if broken == 'missing_version':
        path.unlink()
    else:
        path.write_bytes(b'inert invalid shipped dependency')
    owner.atlas.prepare_semantic('en')
    assert not isinstance(owner.atlas._semantic_cache['en'][1], np.memmap)
    path.write_bytes(original)
    assert not owner.atlas.compatible_with(owner.config, context_memory=owner.memory)


def test_repair_during_fallback_build_does_not_relabel_generation(local_atlas, monkeypatch):
    owner = local_atlas
    root = make_shipped(owner)
    stamp = root / 'semantic_en.version'
    original = stamp.read_bytes()
    stamp.unlink()
    real_encode = owner.atlas._vectorizer.encode
    def repair(texts):
        matrix = real_encode(texts)
        stamp.write_bytes(original)
        return matrix
    monkeypatch.setattr(owner.atlas._vectorizer, 'encode', repair)
    owner.atlas.prepare_semantic('en')
    assert not isinstance(owner.atlas._semantic_cache['en'][1], np.memmap)
    assert owner.atlas._semantic_generations['en'] is None
    assert not owner.atlas.compatible_with(owner.config, context_memory=owner.memory)


def test_transient_load_error_retries_valid_unchanged_shipped_cache(local_atlas, monkeypatch):
    owner = local_atlas
    root = make_shipped(owner)
    paths = [owner.files / 'en.json', *(root / ('semantic_en.' + suffix)
             for suffix in ('version', 'rows.json', 'npy'))]
    before = {path: path.read_bytes() for path in paths}
    real_load = np.load
    calls = []
    def transient(path, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            raise OSError('One inert transient np.load failure; bytes unchanged')
        return real_load(path, **kwargs)
    monkeypatch.setattr(atlas_module.np, 'load', transient)
    owner.atlas.prepare_semantic('en')
    assert not isinstance(owner.atlas._semantic_cache['en'][1], np.memmap)
    assert owner.atlas._semantic_generations['en'] is None
    owner.config['ui.window_opacity_percent'] = 73
    replacement = refresh(owner, monkeypatch)
    assert replacement is not owner.atlas
    replacement.prepare_semantic('en')
    rows, matrix = replacement._semantic_cache['en']
    assert isinstance(matrix, np.memmap) and matrix.shape == (len(rows), 384)
    assert replacement.compatible_with(owner.config, context_memory=owner.memory)
    assert len(calls) == 2
    assert {path: path.read_bytes() for path in paths} == before


def test_semantic_negation_and_destructive_guards_unchanged(local_atlas):
    owner = local_atlas
    (owner.files / 'en.json').write_text(json.dumps({'action_phrases': {
        'screen.capture': ['take a screenshot'], 'filesystem.delete': ['delete the file']}}), encoding='utf-8')
    for text, intent, executable in [('take a screenshot', 'screen.capture', True),
                                     ('do not take a screenshot', 'screen.capture', False),
                                     ('delete the file', 'filesystem.delete', False)]:
        result = owner.atlas.semantic_match(text, language='en')
        assert result.intent == intent and result.executable is executable
