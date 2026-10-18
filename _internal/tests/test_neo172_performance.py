"""NEO 172: optimisations must not slow the animation; OCR cache; bounded disk use; settings."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_animation_frames_are_not_slower_than_before():
    pytest.importorskip('PySide6')
    import sys
    sys.path.insert(0, str(ROOT / 'tools'))
    from frame_bench172 import measure
    baseline = json.loads((ROOT / 'tests' / 'fixtures' / 'frames_baseline172.json').read_text(encoding='utf-8'))['states']
    now = measure(frames=60)
    assert set(now) == set(baseline)                                  # every animation state is measured

    def slow(state, row):
        before = baseline[state]['mean_ms']        # the baseline mean is close to its median
        # a whole-machine hiccup moves a few frames; a real regression moves the typical frame
        return row['median_ms'] > 16.6 or row['median_ms'] > max(before * 2.5, before + 4.0)
    suspects = [s for s, row in now.items() if slow(s, row)]
    if suspects:                                                      # measure again: noise does not repeat
        again = measure(frames=120, states=tuple(suspects))
        suspects = [s for s in suspects if slow(s, again[s])]
    assert not suspects, {s: (baseline[s], now[s]) for s in suspects}


def test_same_window_is_not_recognised_twice(monkeypatch):
    from PIL import Image
    from jarvis.runtime import screen_ocr168
    calls = []
    monkeypatch.setattr(screen_ocr168, 'recognize', lambda image, max_edge=2400: (calls.append(1), ([screen_ocr168.OcrLine('Ошибка 118', ())], 1.0))[1])
    screen_ocr168._CACHE.clear()
    image = Image.new('RGB', (800, 600), 'white')
    assert screen_ocr168.text_lines(image) == ['Ошибка 118'] and screen_ocr168.text_lines(image.copy()) == ['Ошибка 118']
    assert len(calls) == 1
    other = Image.new('RGB', (800, 600), 'black')
    screen_ocr168.text_lines(other)
    assert len(calls) == 2
    monkeypatch.setattr(screen_ocr168, '_CACHE_SECONDS', -1)
    screen_ocr168.text_lines(image)
    assert len(calls) == 3                                             # an old result is not reused
    screen_ocr168._CACHE.clear()


def test_cache_is_bounded_and_user_backups_are_kept(tmp_path):
    from jarvis.runtime import housekeeping172 as hk
    cache = tmp_path / 'cache'
    (cache / 'youtube_audio').mkdir(parents=True)
    old = cache / 'youtube_audio' / 'old.m4a'
    old.write_bytes(b'x' * 100)
    stale = time.time() - 30 * 86400
    os.utime(old, (stale, stale))
    fresh = [cache / f'f{i}.bin' for i in range(3)]
    for i, path in enumerate(fresh):
        path.write_bytes(b'x' * (30 * 1024 * 1024))
        os.utime(path, (time.time() - 100 + i, time.time() - 100 + i))
    removed, freed = hk.clean_cache(cache, days=14, max_mb=60)
    assert not old.exists() and not (cache / 'youtube_audio').exists()
    assert not fresh[0].exists() and fresh[1].exists() and fresh[2].exists()     # oldest first until ≤ 60 MB
    assert removed == 2 and freed == 100 + 30 * 1024 * 1024
    backups = tmp_path / 'backups'
    backups.mkdir()
    for i in range(14):
        path = backups / f'settings-before-v16{i}-v1.json'
        path.write_text('{}', encoding='utf-8')
        os.utime(path, (time.time() - 1000 + i, time.time() - 1000 + i))
    (backups / 'file_mutations.json').write_text('{}', encoding='utf-8')
    (backups / 'report.docx.bak').write_bytes(b'user file')
    assert hk.clean_backups(backups, keep=10) == 4
    assert (backups / 'file_mutations.json').exists() and (backups / 'report.docx.bak').exists()
    assert len(list(backups.glob('settings-before-*.json'))) == 10


def test_housekeeping_runs_once_a_day(tmp_path):
    from jarvis.runtime import housekeeping172 as hk
    config = SimpleNamespace(get=lambda k, d=None: d, paths=SimpleNamespace(root=tmp_path, cache=tmp_path / 'cache', backups=tmp_path / 'b'))
    assert hk.run(config) is not None and hk.run(config) is None
    assert hk.run(config, force=True) is not None


def test_settings_172_defaults_migration_and_dialog():
    from jarvis.runtime import settings172
    defaults = json.loads((ROOT / 'config' / 'default_settings.json').read_text(encoding='utf-8'))
    assert defaults['settings_version'] >= 172
    assert defaults['learning']['collect_phrases'] is False                     # only with the user's consent
    assert defaults['help'] == {'confirm_before_search': True, 'use_screen_model': True}
    assert defaults['local_chat']['stream'] is True and defaults['memory']['propose'] is True
    data = {'memory': {'enabled': False}, 'settings_version': 171}
    settings172.migrate(data, 171)
    assert data['memory'] == {'enabled': False, 'propose': True} and data['settings_version'] >= 172
    source = (ROOT / 'jarvis' / 'ui' / 'settings_dialog.py').read_text(encoding='utf-8')
    for key in ('learning_collect172', 'memory_propose172', 'local_stream172', 'help_confirm172', 'help_vision172'):
        assert source.count(key) >= 3, key
    labels = [line.split('QCheckBox("', 1)[1].split('")', 1)[0] for line in source.splitlines() if '172 = QCheckBox("' in line]
    assert len(labels) == 5
    for code in ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt'):
        table = json.loads((ROOT / 'jarvis' / 'locales' / 'ui' / f'{code}.json').read_text(encoding='utf-8'))
        flat = json.dumps(table, ensure_ascii=False)
        for label in labels:
            assert json.dumps(label, ensure_ascii=False) in flat, (code, label)


def test_heavy_work_of_172_stays_off_the_ui_thread():
    source = (ROOT / 'jarvis' / 'ui' / 'main_window.py').read_text(encoding='utf-8')
    block = source[source.index('def _weekly_learning_report172'):source.index('def _drain_timer_events')]
    assert 'FunctionWorker' in block and 'thread_pool.start' in block and '3600.0' in block
    from jarvis.runtime import housekeeping172
    assert 'threading.Thread' in (ROOT / 'jarvis' / 'runtime' / 'housekeeping172.py').read_text(encoding='utf-8')
