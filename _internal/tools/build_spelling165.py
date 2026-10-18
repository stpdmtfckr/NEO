"""Build config/spelling165_<lang>.json for correcting misheard words in speech.

Developer tool (``pip install wordfreq pymorphy3 pymorphy3-dicts-ru``); the app
only reads the generated files, one language at a time.

* ``targets`` - 20k-35k frequent words (wordfreq Zipf >= 3.0), most frequent
  first: what a misheard word may become;
* ``known``   - CRC32 of words that are correct as heard: every wordfreq word with
  Zipf >= 1.8 (rare misspellings such as «превет», Zipf 1.3, stay out), plus for
  Russian every OpenCorpora form that looks like a target (so rare but correct
  inflections are never "fixed").

Usage: python tools/build_spelling165.py [lang ...]
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LANGUAGES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
# Richly inflected languages need more word forms for the same coverage.
MAX_TARGETS = {'ru': 35000, 'de': 30000}


def _alpha(word: str) -> bool:
    return word.replace('-', '').replace("'", '').replace('’', '').isalpha()


def _lists(code: str) -> tuple[list[str], list[str]]:
    from wordfreq import top_n_list, zipf_frequency
    targets, known = [], []
    for word in top_n_list(code, 400000):
        word = word.casefold().replace('ё', 'е')
        if not _alpha(word) or len(word) < 2:
            continue
        zipf = zipf_frequency(word, code)
        if zipf < 1.8:
            break
        known.append(word)
        if zipf >= 3.0 and len(word) >= 3 and len(targets) < MAX_TARGETS.get(code, 20000):
            targets.append(word)
    return list(dict.fromkeys(targets)), known


def _near_targets(task: tuple[list[str], list[str]]) -> list[str]:
    """Russian forms within the voice budget of a target word."""
    targets, chunk = task
    from jarvis.runtime.command_typos165 import _deletes, distance
    from jarvis.runtime.spoken_spelling165 import voice_budget
    index: dict[str, list[str]] = {}
    for word in targets:
        for key in {word} | _deletes(word):
            index.setdefault(key, []).append(word)
    found = []
    for form in chunk:
        limit = voice_budget(form)
        if not limit:
            continue
        for key in {form} | _deletes(form):
            if any(distance(form, t, 'ru', limit) <= limit for t in index.get(key, ()) if t != form):
                found.append(form)
                break
    return found


def main(argv: list[str]) -> int:
    from jarvis.runtime.command_typos165 import pack_hashes, word_hash
    codes = [c for c in argv if c in LANGUAGES] or list(LANGUAGES)
    for code in codes:
        targets, known = _lists(code)
        extra: list[str] = []
        if code == 'ru':
            import pymorphy3
            forms = sorted({w.casefold().replace('ё', 'е') for w, *_ in pymorphy3.MorphAnalyzer().dictionary.iter_known_words()
                            if 4 <= len(w) <= 24 and w.isalpha()})
            chunks = [(targets, forms[i:i + 20000]) for i in range(0, len(forms), 20000)]
            with ProcessPoolExecutor(max_workers=os.cpu_count() or 2) as pool:
                extra = [w for part in pool.map(_near_targets, chunks) for w in part]
        data = {'schema': 1, 'language': code, 'hash': 'crc32-utf8',
                'source': 'wordfreq (CC BY-SA 4.0)' + ('; OpenCorpora via pymorphy3 (CC BY-SA)' if code == 'ru' else ''),
                'targets': '\n'.join(targets),
                'known': pack_hashes(word_hash(w) for w in [*known, *extra])}
        target = ROOT / 'config' / f'spelling165_{code}.json'
        target.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
        print(f'{code}: {len(targets)} targets, {len(known) + len(extra)} known '
              f'-> {target.name} ({target.stat().st_size // 1024} KB)', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
