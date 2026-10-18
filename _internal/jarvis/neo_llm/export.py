"""Training and evaluation data for the NEO agent model, in the exact runtime prompt format.

    python -m jarvis.neo_llm.export --out <folder> [--max-examples 9000] [--seed 7]

Sources (all already inside NEO, nothing personal):
* the NEO command dataset (phrase atlas, grammar packs, typos, context phrases), split
  by template exactly like the NEO command model: evaluation rows come only from
  templates the training rows never used;
* synthetic sequences of 2–3 commands joined with «, / и / затем …» → a list of commands;
* browser flows («открой браузер, загугли X и перейди по первой ссылке» → one command);
* the user's confirmed corrections (train only);
* "not a command" rows (chat, questions): their target reply is written later by the
  base model itself (``teacher`` rows), so fine-tuning does not unlearn conversation.

Every example is rendered with :mod:`jarvis.neo_llm.prompt` — the same system prompt,
the same candidate list construction (NEO command model ranking + everyday commands)
and the same JSON answer the model must give at run time.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import sys
from typing import Any

from jarvis.neo_llm import prompt

ROOT = Path(__file__).resolve().parents[2]
CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
JOINERS = {
    'ru': (', ', ' и ', ', затем ', ' и потом '), 'en': (', ', ' and ', ', then ', ' and then '),
    'de': (', ', ' und ', ', dann ', ' und danach '), 'fr': (', ', ' et ', ', puis ', ' et ensuite '),
    'es': (', ', ' y ', ', luego ', ' y después '), 'it': (', ', ' e ', ', poi ', ' e poi '),
    'pt': (', ', ' e ', ', depois ', ' e depois '),
}
# Commands that make sense inside a sequence (open, go, type, click, media…).
CHAIN_TOOLS = {'application', 'browser', 'browser_nav', 'ui', 'window', 'copy', 'media', 'music', 'audio', 'capture',
               'screen', 'clipboard', 'timer', 'weather', 'mini_browser', 'yandex_music', 'spotify'}
WEB_FLOWS = {
    'ru': ['открой браузер, загугли {q} и перейди по первой ссылке', 'найди в гугле {q} и открой первый сайт',
           'загугли {q} и зайди на первый сайт', 'мне нужно {q}, найди в интернете и открой первую ссылку'],
    'en': ['open the browser, google {q} and go to the first link', 'search google for {q} and open the first site',
           'google {q} and open the first result', 'find {q} online and open the first link'],
    'de': ['öffne den Browser, google {q} und öffne den ersten Link', 'suche bei Google nach {q} und öffne die erste Seite'],
    'fr': ['ouvre le navigateur, cherche {q} sur google et ouvre le premier lien', 'cherche {q} sur internet et ouvre le premier site'],
    'es': ['abre el navegador, busca en google {q} y abre el primer enlace', 'busca {q} en internet y abre el primer sitio'],
    'it': ['apri il browser, cerca su google {q} e apri il primo link', 'cerca {q} su internet e apri il primo sito'],
    'pt': ['abre o navegador, pesquisa no google {q} e abre o primeiro link', 'pesquisa {q} na internet e abre o primeiro site'],
}
QUERIES = {
    'ru': ['рецепт борща', 'погода в Казани', 'отзывы о пылесосе', 'курс доллара', 'как поменять масло', 'расписание электричек'],
    'en': ['borscht recipe', 'weather in Boston', 'vacuum cleaner reviews', 'dollar exchange rate', 'how to change oil'],
    'de': ['Rezept Borschtsch', 'Wetter in Berlin', 'Staubsauger Test'], 'fr': ['recette de bortsch', 'météo à Lyon', 'avis aspirateur'],
    'es': ['receta de borsch', 'tiempo en Madrid', 'opiniones aspiradora'], 'it': ['ricetta borscht', 'meteo Roma', 'recensioni aspirapolvere'],
    'pt': ['receita de borscht', 'tempo em Lisboa', 'opiniões aspirador'],
}


def _predictor():
    try:
        from jarvis.neo_model.predictor import NeoCommandModel
        from jarvis.neo_model.store import BUNDLED
        return NeoCommandModel(BUNDLED)
    except Exception:
        return None


def candidates(predictor: Any, pieces: list[str], code: str, gold: list[str], rng: random.Random, limit: int = 14) -> list[str]:
    """Same construction as agent_llm170.candidate_intents; gold intents are always present."""
    from jarvis.neo_model.dataset import NONE
    ranked: list[str] = []
    if predictor is not None:
        per_piece = max(3, limit // max(1, len(pieces)))
        for piece in pieces:
            for intent, _p in predictor.ranked(piece, code, '', top=per_piece + 1):
                if intent != NONE and intent not in ranked:
                    ranked.append(intent)
    ranked = ranked[:limit]
    for intent in gold:
        if intent not in ranked and intent not in prompt.CORE_INTENTS:
            ranked.insert(rng.randrange(len(ranked) + 1), intent)
    return ranked + [i for i in prompt.CORE_INTENTS if i not in ranked]


def understood_as(cmd: str, code: str, intent: str) -> bool:
    """NEO's grammar reads ``cmd`` as ``intent`` (among its most specific matching templates)."""
    from jarvis.runtime.canonical_commands import _matching_templates, strip_request_shell
    raw = strip_request_shell(str(cmd or '').strip().lstrip('¿¡'), code).strip()
    try:
        matched = _matching_templates(raw, code)
    except Exception:
        return False
    if not matched:
        return False
    top = max(row.specificity for row, _ in matched)
    # Several intents can share one phrase (music.find_song / music.find_track): any of them is the same command.
    return intent in {row.intent for row, _ in matched if row.specificity == top}


def command(row: dict[str, Any]) -> str | None:
    """The NEO command a dataset row means, written with the prompt's pattern for its intent."""
    pattern = prompt.pattern_for(row['intent'], row['language'])
    if not pattern:
        return None
    values = {slot['name']: slot.get('value') or row['text'][slot['start']:slot['end']] for slot in row.get('slots') or []}
    filled = prompt.fill(pattern, values)
    if '{' in filled or not understood_as(filled, row['language'], row['intent']):
        return None
    return filled


def correction_command(intent: str, code: str, arguments: dict[str, Any]) -> str | None:
    """The command for a confirmed correction, from the tool arguments it ran with."""
    import re
    from jarvis.runtime.canonical_commands import _SLOT_NAMES
    pattern = prompt.pattern_for(intent, code)
    if not pattern:
        return None
    names = _SLOT_NAMES.get(intent, {})
    values = {}
    for placeholder in re.findall(r'\{([a-z_0-9]+)\}', pattern):
        value = arguments.get(names.get(placeholder, placeholder), arguments.get(placeholder))
        if value in (None, ''):
            return None
        values[placeholder] = str(value)
    return prompt.fill(pattern, values)


def _example(predictor, rng, text: str, code: str, gold: list[str], *, do: list[str] | None = None,
             teacher: bool = False, meta: dict | None = None, pieces: list[str] | None = None) -> dict[str, Any]:
    intents = candidates(predictor, pieces or [text], code, gold, rng)
    patterns = [p for p in (prompt.pattern_for(i, code) for i in intents) if p]
    messages = prompt.messages(text, code, patterns)
    if not teacher:
        messages.append({'role': 'assistant', 'content': prompt.target(do=do)})
    return {'messages': messages, 'teacher': teacher, 'meta': dict(meta or {}, language=code, gold=gold, do=do or [])}


def build(*, max_examples: int = 9000, seed: int = 7, corrections: list[dict] | None = None,
          languages: tuple[str, ...] = CODES, chat: int = 300, eval_size: int = 400) -> dict[str, list[dict[str, Any]]]:
    from jarvis.neo_model import dataset
    rng = random.Random(seed)
    data = dataset.build(ROOT, languages=list(languages), corrections=corrections, seed=seed)
    predictor = _predictor()

    def pool(rows):
        grouped: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            if row['intent'] != dataset.NONE and not row.get('context'):
                grouped.setdefault((row['intent'], row['language']), []).append(row)
        return grouped

    train_pool, test_pool = pool(data['train']), pool(data['test'])
    budget_single = int(max_examples * 0.72)
    per_key = max(1, -(-budget_single // max(1, len(train_pool))))     # ceil: fill the budget, stop at the limit
    out: dict[str, list[dict[str, Any]]] = {'train': [], 'eval': []}

    def singles(grouped, limit, split):
        keys = sorted(grouped)
        rng.shuffle(keys)
        for key in keys:
            rows = grouped[key][:]
            rng.shuffle(rows)
            for row in rows[:per_key if split == 'train' else 1]:
                cmd = command(row)
                if cmd is None:
                    continue
                out[split].append(_example(predictor, rng, row['text'], row['language'], [row['intent']], do=[cmd],
                                           meta={'kind': 'single', 'intent': row['intent'], 'source': row.get('source', '')}))
                if len([e for e in out[split] if e['meta']['kind'] == 'single']) >= limit:
                    return

    singles(train_pool, budget_single, 'train')
    singles(test_pool, int(eval_size * 0.6), 'eval')

    def sequences(grouped, count, split):
        by_language: dict[str, list[dict]] = {}
        for (intent, code), rows in grouped.items():
            if intent.split('.', 1)[0] in CHAIN_TOOLS:
                by_language.setdefault(code, []).extend(rows[:6])
        codes = sorted(by_language)
        for _ in range(count):
            code = rng.choice(codes)
            rows = rng.sample(by_language[code], k=min(len(by_language[code]), rng.choice((2, 2, 3))))
            if len({r['intent'] for r in rows}) < len(rows):
                continue
            cmds = [command(r) for r in rows]
            if any(c is None for c in cmds):
                continue
            joiners = JOINERS[code]
            text = rows[0]['text']
            for row in rows[1:]:
                text += rng.choice(joiners) + row['text']
            out[split].append(_example(predictor, rng, text, code, [r['intent'] for r in rows], do=cmds,
                                       pieces=[r['text'] for r in rows], meta={'kind': 'sequence'}))

    sequences(train_pool, int(max_examples * 0.18), 'train')
    sequences(test_pool, int(eval_size * 0.2), 'eval')

    for split, count in (('train', max(40, int(max_examples * 0.04))), ('eval', max(10, eval_size // 20))):
        for _ in range(count):
            code = rng.choice([c for c in languages if c in WEB_FLOWS])
            query = rng.choice(QUERIES[code])
            text = rng.choice(WEB_FLOWS[code]).format(q=query)
            cmd = prompt.fill(prompt.pattern_for('browser.search_first', code), {'query': query})
            out[split].append(_example(predictor, rng, text, code, ['browser.search_first'], do=[cmd], meta={'kind': 'web'}))

    for row in corrections or []:
        text, code = str(row.get('text', '')), str(row.get('language', 'ru'))
        right = row.get('right') or {}
        intent = f"{right.get('tool', '')}.{(right.get('arguments') or {}).get('action', '')}"
        cmd = correction_command(intent, code, dict(right.get('arguments') or {}))
        if text and code in languages and cmd:
            # A confirmed correction is the user's own wording: include it several times.
            for _ in range(3):
                out['train'].append(_example(predictor, rng, text, code, [intent], do=[cmd], meta={'kind': 'correction'}))

    negatives = [r for r in data['train'] if r['intent'] == dataset.NONE]
    rng.shuffle(negatives)
    for row in negatives[:chat]:
        out['train'].append(_example(predictor, rng, row['text'], row['language'], [], teacher=True, meta={'kind': 'chat'}))
    test_negatives = [r for r in data['test'] if r['intent'] == dataset.NONE]
    rng.shuffle(test_negatives)
    for row in test_negatives[:max(10, eval_size // 5)]:
        out['eval'].append(_example(predictor, rng, row['text'], row['language'], [], teacher=True, meta={'kind': 'chat'}))
    rng.shuffle(out['train'])
    out['info'] = [{'fingerprint': dataset.fingerprint(data), 'train': len(out['train']), 'eval': len(out['eval']), 'seed': seed}]
    return out


def save(built: dict[str, list[dict[str, Any]]], folder: Path) -> dict[str, Any]:
    folder.mkdir(parents=True, exist_ok=True)
    for split in ('train', 'eval'):
        with (folder / f'{split}.jsonl').open('w', encoding='utf-8') as handle:
            for row in built[split]:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    info = built['info'][0]
    (folder / 'info.json').write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding='utf-8')
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--max-examples', type=int, default=9000)
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--corrections', default='')
    args = parser.parse_args(argv)
    corrections = []
    if args.corrections and Path(args.corrections).exists():
        corrections = [json.loads(line) for line in Path(args.corrections).read_text(encoding='utf-8').splitlines() if line.strip()]
    info = save(build(max_examples=args.max_examples, seed=args.seed, corrections=corrections), Path(args.out))
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
