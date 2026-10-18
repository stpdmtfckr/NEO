"""End-to-end typo benchmark through the real Agent. No command is executed.

Every canonical command of a language is routed once as written; its tool.action
is the oracle. Then realistic typos of it (swapped/dropped/doubled letters,
neighbouring keys, similar-sounding letters, fused words, short words, two typos
and, for Russian, the wrong keyboard layout) must reach the same tool.action.
Execution is blocked by AtlasSession.

Usage (from _internal): python tools/benchmark_typos165.py [--lang ru] [--json report.json]
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
from pathlib import Path
import random
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LAYOUT = str.maketrans('ёйцукенгшщзхъфывапролджэячсмитьбю', "`qwertyuiop[]asdfghjkl;'zxcvbnm,.")
KINDS = ('swap', 'delete', 'double', 'neighbour_key', 'vowel', 'fused', 'short_word', 'two_typos', 'layout')
NEIGHBOURS: dict[str, str] = {}
VOWELS: dict[str, str] = {}


def use_language(code: str) -> None:
    from jarvis.runtime.command_typos165 import _ROWS, _SOUND
    NEIGHBOURS.clear()
    VOWELS.clear()
    for row in _ROWS[code]:
        for i, ch in enumerate(row):
            NEIGHBOURS[ch] = row[max(0, i - 1):i] + row[i + 1:i + 2]
    for a, b in _SOUND[code][:4]:
        VOWELS.setdefault(a, b)
        VOWELS.setdefault(b, a)


def mutate(phrase: str, kind: str, rng: random.Random, code: str = 'ru') -> str | None:
    words = phrase.split(' ')
    if kind == 'layout':
        return phrase.translate(LAYOUT) if code == 'ru' else None
    if kind == 'fused':
        if len(words) < 2:
            return None
        k = rng.randrange(len(words) - 1)
        return ' '.join(words[:k] + [words[k] + words[k + 1]] + words[k + 2:])
    if kind == 'short_word':
        spots = [k for k, w in enumerate(words) if 2 <= len(w) <= 3 and w.isalpha() and w[::-1] != w]
        if not spots:
            return None
        k = rng.choice(spots)
        words[k] = words[k][::-1]
        return ' '.join(words)
    spots = [k for k, w in enumerate(words) if len(w) >= 4 and w.isalpha()]
    if not spots:
        return None
    k = rng.choice(spots)
    w = words[k]
    p = rng.randrange(1, len(w) - 1)
    if kind in {'swap', 'two_typos'}:
        if w[p] == w[p + 1]:
            return None
        w = w[:p] + w[p + 1] + w[p] + w[p + 2:]
    elif kind == 'delete':
        w = w[:p] + w[p + 1:]
    elif kind == 'double':
        w = w[:p] + w[p] + w[p:]
    elif kind == 'neighbour_key':
        if not NEIGHBOURS.get(w[p]):
            return None
        w = w[:p] + rng.choice(NEIGHBOURS[w[p]]) + w[p + 1:]
    elif kind == 'vowel':
        vowels = [i for i, c in enumerate(w) if c in VOWELS]
        if not vowels:
            return None
        i = rng.choice(vowels)
        w = w[:i] + VOWELS[w[i]] + w[i + 1:]
    words[k] = w
    if kind == 'two_typos':
        others = [j for j in spots if j != k]
        if not others:
            return None
        j = rng.choice(others)
        other = words[j]
        q = rng.randrange(1, len(other) - 1)
        words[j] = other[:q] + other[q + 1:]
    return ' '.join(words)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--json', help='write every case to this JSON file')
    parser.add_argument('--lang', default='ru', choices=('ru', 'en', 'de', 'fr', 'es', 'it', 'pt'))
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    code = args.lang
    use_language(code)
    from jarvis.runtime.natural_commands import LANGUAGES
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked

    phrases = []
    for path in sorted(glob.glob(str(ROOT / 'config/canonical_phrases/*.json'))):
        for variants in json.loads(Path(path).read_text(encoding='utf-8')).values():
            filler = {'ru': 'тест'}.get(code, 'test')
            phrases.append(re.sub(r'\{\w+\}', filler, variants[LANGUAGES.index(code)]).lower())
    phrases = sorted(set(phrases))

    def route(session, text):
        session.reset_case(code)
        started = time.perf_counter()
        turn = session.agent.handle(text)
        elapsed = (time.perf_counter() - started) * 1000
        pending = session.agent.conversations.get('desktop').pending_intent
        if session.calls:
            call = session.calls[0]
            return f"{call['tool']}.{call['arguments'].get('action', '')}", elapsed
        if pending is not None:
            return f"ask:{pending.tool}.{pending.arguments.get('action', '')}", elapsed
        if turn.pending is not None:
            return f'confirm:{turn.pending.tool_name}', elapsed
        return 'none:' + ','.join(turn.tool_details or []), elapsed

    rows, times = [], []
    with external_io_blocked():
        session = AtlasSession(Path(tempfile.mkdtemp()) / 'typos165', locale=code)
        oracle = {}
        for phrase in phrases:
            got, _ = route(session, phrase)
            if not got.startswith('none:'):
                oracle[phrase] = got
        rng = random.Random(11)
        for kind in KINDS:
            for phrase in phrases:
                typo = mutate(phrase, kind, rng, code)
                if phrase not in oracle or not typo or typo == phrase:
                    continue
                got, elapsed = route(session, typo)
                times.append(elapsed)
                rows.append({'kind': kind, 'phrase': phrase, 'typo': typo, 'expected': oracle[phrase], 'got': got})
        session.finish()

    print(f'{"typo kind":<15}{"cases":>7}{"understood":>12}{"other tool":>12}')
    for kind in (*KINDS, None):
        part = [r for r in rows if kind is None or r['kind'] == kind]
        if not part:
            continue
        ok = sum(r['got'] == r['expected'] for r in part)
        wrong = sum(r['got'] != r['expected'] and not r['got'].startswith(('none', 'ask', 'confirm')) for r in part)
        print(f'{kind or "TOTAL":<15}{len(part):>7}{ok / len(part):>12.0%}{wrong / len(part):>12.1%}')
    times.sort()
    print(f'latency p50={times[len(times) // 2]:.1f}ms p95={times[int(len(times) * .95)]:.1f}ms')
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
