"""Build config/typo_lexicon165.json: real words that merely *look* like typos.

Developer tool; the app only reads the generated JSON. Needs
``pip install pymorphy3 pymorphy3-dicts-ru wordfreq``. For every language a word
form is listed when it is not a command word but lies within the typo budget of one:

* ``soft``   - soft repair edits it only into a fully literal command
  («какая пагода» -> «какая погода»), never inside a payload («кино», «крови»);
* ``stable`` - a non-imperative verb form («включил», «opened», «öffnete»). Even
  the last-chance rescue keeps it, so a statement never turns into a command.

Russian forms and their grammar come from the OpenCorpora dictionary (pymorphy3);
the other languages use the 200k most frequent wordfreq words and past-tense /
participle endings. Words are stored as sorted CRC32 values (4 bytes each).

Usage: python tools/build_typo_lexicon165.py [lang ...]
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_VERB_FORMS = {'VERB', 'INFN', 'GRND', 'PRTF', 'PRTS'}


def _russian_words() -> dict[str, bool]:
    """word -> True when one of its readings is a non-imperative verb form."""
    import pymorphy3
    words: dict[str, bool] = {}
    for word, tag, *_ in pymorphy3.MorphAnalyzer().dictionary.iter_known_words():
        word = word.casefold().replace('ё', 'е')
        if not (4 <= len(word) <= 24 and word.isalpha()):
            continue
        verbal = tag.POS in _VERB_FORMS and 'impr' not in tag
        words[word] = words.get(word, False) or verbal
    return words


# Past tense, participles and gerunds: statements about an action, not a command.
_STATEMENT_ENDINGS = {
    'en': r'.{2,}(?:ed|ing)',
    'de': r'(?:ge.{2,}(?:t|en)|.{3,}(?:te|ten|test|tet))',
    'fr': r'.{2,}(?:é|ée|és|ées|ait|aient|ais|ant)',
    'es': r'.{2,}(?:ó|aba|aban|ado|ada|ados|adas|ido|ida|idos|idas|aron|ieron|ando|iendo)',
    'it': r'.{2,}(?:ato|ata|ati|ate|ito|ita|iti|ite|ava|avano|ò|ando|endo)',
    'pt': r'.{2,}(?:ou|ado|ada|ados|adas|ido|ida|idos|idas|ava|avam|aram|eram|iram|ando|endo|indo)',
}


def _frequent_words(code: str) -> dict[str, bool]:
    import re
    from wordfreq import top_n_list, zipf_frequency
    pattern = re.compile(_STATEMENT_ENDINGS[code])
    words: dict[str, bool] = {}
    for word in top_n_list(code, 200000):
        word = word.casefold()
        # Rare entries are mostly common misspellings («tomorow»): not real words.
        if zipf_frequency(word, code) < 1.8:
            break
        if 4 <= len(word) <= 24 and word.isalpha():
            words[word] = bool(pattern.fullmatch(word))
    return words


def _near(task: tuple[str, list[str]]) -> list[str]:
    code, chunk = task
    from jarvis.runtime import command_typos165 as typos
    typos._tables = lambda _code: None  # judge every word afresh, not by a previous build
    vocabulary = typos.vocabulary(code)
    return [w for w in chunk if w not in vocabulary and typos.word_options(w, code, True)]


def main(argv: list[str]) -> int:
    from jarvis.runtime.command_typos165 import pack_hashes, word_hash
    target = ROOT / 'config' / 'typo_lexicon165.json'
    codes = [c for c in argv if c in ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')] or ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt']
    try:  # rebuilding some languages keeps the others
        languages = dict(json.loads(target.read_text(encoding='utf-8')).get('languages', {}))
    except (OSError, ValueError):
        languages = {}
    with ProcessPoolExecutor(max_workers=os.cpu_count() or 2) as pool:
        for code in codes:
            words = _russian_words() if code == 'ru' else _frequent_words(code)
            names = sorted(words)
            chunks = [(code, names[i:i + 20000]) for i in range(0, len(names), 20000)]
            near = sorted(w for part in pool.map(_near, chunks) for w in part)
            stable = [w for w in near if words[w]]
            soft = [w for w in near if not words[w]]
            languages[code] = {'soft': pack_hashes(word_hash(w) for w in soft),
                               'stable': pack_hashes(word_hash(w) for w in stable)}
            print(f'{code}: {len(names)} forms scanned, {len(soft)} soft, {len(stable)} stable', flush=True)
    data = {'schema': 2, 'hash': 'crc32-utf8',
            'note': 'Real word forms near command words. Soft typo repair edits soft words only '
                    'into fully literal commands; stable (non-imperative verb) forms never change.',
            'source': 'ru: OpenCorpora dictionary via pymorphy3 (CC BY-SA); others: wordfreq (CC BY-SA 4.0)',
            'languages': {code: languages[code] for code in ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt') if code in languages}}
    target.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f'-> {target.name} ({target.stat().st_size // 1024} KB)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
