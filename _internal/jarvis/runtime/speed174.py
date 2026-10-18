"""NEO 174: faster understanding without new models.

* NEO checks a phrase against well over 512 regular expressions. Python keeps only the
  last 512 compiled ones, so every request compiled hundreds of them again. A larger
  cache keeps them compiled (a few MB of memory).
* The first command after start paid about 1.5 s for lazily built phrase tables. A
  background warm-up builds them after start, including the selected semantic
  locale that static sample phrases do not exercise. It never runs a command,
  touches the screen or the network.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)
REGEX_CACHE = 4096


def enlarge_regex_cache(size: int = REGEX_CACHE) -> int:
    """Raise Python's compiled-pattern cache (the stdlib default is 512)."""
    try:
        if int(getattr(re, '_MAXCACHE', 0) or 0) < size:
            re._MAXCACHE = size          # stdlib attribute read on every re.* call
        return int(re._MAXCACHE)
    except Exception:
        return 0


_SAMPLES = {
    'ru': ('открой настройки звука', 'сделай скриншот', 'поставь музыку на паузу', 'какая погода'),
    'en': ('open sound settings', 'take a screenshot', 'pause the music', 'what is the weather'),
}


def warm_up(agent: Any, code: str = 'ru') -> dict[str, float]:
    """Build the lazily created phrase tables in the background; returns ms per stage."""
    timings: dict[str, float] = {}
    samples = _SAMPLES.get(code, _SAMPLES['en'])

    def stage(name: str, action) -> None:
        started = time.perf_counter()
        try:
            action()
        except Exception:
            logger.debug('Warm-up stage %s failed', name, exc_info=True)
        timings[name] = round((time.perf_counter() - started) * 1000.0, 1)

    atlas = getattr(agent, 'atlas2', None)
    router = getattr(agent, 'router', None)
    if atlas is not None:
        stage('atlas.exact', lambda: [atlas.exact_hash_match(text, language=code) for text in samples])
        if router is not None:
            stage('atlas.resolve', lambda: [atlas.resolve(text, router=router, language=code) for text in samples])
    if router is not None:
        stage('router', lambda: [router.route(text) for text in samples])

    def apps() -> None:
        from jarvis.tools import apps_tool173
        for text in samples:
            apps_tool173.parse(text, code)
    stage('apps', apps)

    def explain() -> None:
        from jarvis.runtime import explain172
        for text in samples:
            explain172.parse(text)
    stage('explain', explain)

    def typos() -> None:
        from jarvis.runtime import command_typos165
        command_typos165.word_options('открйо' if code == 'ru' else 'opne', code)
    stage('typos', typos)

    def command_model() -> None:
        from jarvis.runtime import neo_model169
        neo_model169.propose(agent, samples[0], code)       # a proposal only, nothing is executed
    stage('neo_model', command_model)
    # Static samples can all resolve before semantic matching. Prepare its table
    # explicitly on this existing background worker, after the cheap stages.
    # Valid shipped caches remain memory-mapped; only a missing/stale cache is
    # rebuilt. Early semantic requests may still wait for that same build.
    if atlas is not None:
        stage('atlas.semantic', lambda: atlas.prepare_semantic(code))
    logger.info('Understanding warm-up: %s', timings)
    return timings
