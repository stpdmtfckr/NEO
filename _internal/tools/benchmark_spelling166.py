"""Misheard-word benchmark for spoken_spelling165 (no Agent, no audio).

Correct phrases of each language (canonical commands, command variants, social
dialogue) get one misheard word: a similar-sounding letter, a doubled or dropped
letter, or swapped letters. Reports how many are written correctly again and how
many correct phrases were changed by mistake.

Usage (from _internal): python tools/benchmark_spelling166.py
"""
from __future__ import annotations

import glob
import json
from pathlib import Path
import random
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jarvis.runtime.command_typos165 import _SOUND  # noqa: E402
from jarvis.runtime.natural_commands import LANGUAGES  # noqa: E402
from jarvis.runtime import spoken_spelling165 as spelling  # noqa: E402


def corpus(code: str) -> list[str]:
    rows: list[str] = []
    index = LANGUAGES.index(code)
    for path in glob.glob(str(ROOT / 'config/canonical_phrases/*.json')):
        rows += [v[index] for v in json.loads(Path(path).read_text(encoding='utf-8')).values()]
    for path in glob.glob(str(ROOT / 'config/command_variants/*.json')):
        rows += [p for v in json.loads(Path(path).read_text(encoding='utf-8')).values() for p in v.get(code, [])]
    from jarvis.runtime.social_atlas import catalog
    rows += list(catalog(code))
    rows = [r for r in rows if '{' not in r and not re.search(r'[A-Z]{2}|\d', r)]
    return sorted(set(r.lower() for r in rows))


def mishear(phrase: str, code: str, rng: random.Random) -> str | None:
    words = [m for m in re.finditer(r'[^\W\d_]{5,}', phrase)]
    words = [m for m in words if bool(re.fullmatch('[а-яё]+', m.group(0))) == (code == 'ru')]
    if not words:
        return None
    m = rng.choice(words)
    w = m.group(0)
    pairs = {a: b for a, b in _SOUND[code]} | {b: a for a, b in _SOUND[code]}
    kind = rng.choice(('sound', 'double', 'drop', 'swap'))
    p = rng.randrange(1, len(w) - 1)
    if kind == 'sound':
        spots = [i for i, c in enumerate(w) if c in pairs and i > 0]
        if not spots:
            return None
        p = rng.choice(spots)
        w2 = w[:p] + pairs[w[p]] + w[p + 1:]
    elif kind == 'double':
        w2 = w[:p] + w[p] + w[p:]
    elif kind == 'drop':
        w2 = w[:p] + w[p + 1:]
    else:
        w2 = w[:p] + w[p + 1] + w[p] + w[p + 2:]
    return None if w2 == w else phrase[:m.start()] + w2 + phrase[m.end():]


def main() -> int:
    print(f"{'lang':<6}{'phrases':>8}{'misheard':>10}{'fixed':>8}{'changed wrongly':>17}{'clean changed':>15}")
    for code in LANGUAGES:
        rows = corpus(code)
        rng = random.Random(5)
        fixed = wrong = total = 0
        for phrase in rows:
            heard = mishear(phrase, code, rng)
            if not heard:
                continue
            total += 1
            out = spelling.correct_transcript(heard, code, wait=True)[0]
            fixed += out == phrase
            wrong += out != phrase and out != heard
        clean = sum(spelling.correct_transcript(p, code, wait=True)[0] != p for p in rows)
        print(f"{code:<6}{len(rows):>8}{total:>10}{fixed / total:>8.0%}{wrong / total:>17.1%}{clean:>15}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
