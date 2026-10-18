"""Train, evaluate and save one version of the NEO command model.

    python -m jarvis.neo_model.train --out <folder> [--corrections corrections.jsonl] [--epochs 5]

The folder receives intent.npz, slots.npz, meta.json (labels, sizes, metrics,
dataset fingerprint) and dataset/*.jsonl. Nothing is activated here: the version
store (jarvis.neo_model.store) compares it with the active model first.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable

import numpy as np

from jarvis.neo_model import dataset as ds
from jarvis.neo_model.features import sentence_features, token_features, tokens
from jarvis.neo_model.network import IntentNet, SlotNet, make_batch

ROOT = Path(__file__).resolve().parents[2]
DIM = 1 << 15          # hashed sentence features (int8 table, see network.py)
SLOT_DIM = 1 << 14
HIDDEN = 64


def _tag_rows(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Token words and their tags ('O' or slot name) from character spans."""
    words, tags = [], []
    for word, start, end in tokens(row['text']):
        tag = 'O'
        for span in row['slots']:
            if start >= span['start'] and end <= span['end'] + 1:
                tag = span['name']
                break
        words.append(word)
        tags.append(tag)
    return words, tags


def train_version(out: Path, *, corrections: list[dict[str, Any]] | None = None, epochs: int = 5,
                  log: Callable[[str], None] = print, root: Path = ROOT,
                  dim: int = DIM, slot_dim: int = SLOT_DIM, hidden: int = HIDDEN) -> dict[str, Any]:
    started = time.perf_counter()
    out.mkdir(parents=True, exist_ok=True)
    data = ds.build(root, corrections=corrections)
    info = ds.save(data, out / 'dataset')
    log(f"dataset {info['fingerprint']}: {info['counts']}")
    labels = sorted({r['intent'] for r in data['train']} | {ds.NONE})
    label_index = {name: i for i, name in enumerate(labels)}

    def features(row: dict[str, Any]) -> tuple[np.ndarray, float]:
        return sentence_features(row['text'], row['language'], row.get('context', ''), dim)

    train_rows = data['train']
    y = [label_index[r['intent']] for r in train_rows]
    order = np.random.default_rng(2).permutation(len(train_rows))
    batches = [make_batch([features(train_rows[i]) for i in order[s:s + 256]], [y[i] for i in order[s:s + 256]])
               for s in range(0, len(order), 256)]
    net = IntentNet(dim, hidden, len(labels))
    net.train(batches, epochs=epochs, log=log)

    # Slot tagger: only examples that have slots.
    tag_names = ['O'] + sorted({s['name'] for r in train_rows for s in r['slots']})
    tag_index = {t: i for i, t in enumerate(tag_names)}
    slot_rows, slot_labels = [], []
    for row in train_rows:
        if not row['slots']:
            continue
        words, tags = _tag_rows(row)
        for position, tag in enumerate(tags):
            slot_rows.append(token_features(words, position, row['intent'], slot_dim))
            slot_labels.append(tag_index.get(tag, 0))
    slots = SlotNet(slot_dim, len(tag_names))
    slots.train(slot_rows, slot_labels, epochs=4, log=log)

    net.save(out / 'intent.npz')
    slots.save(out / 'slots.npz')
    meta = {'labels': labels, 'tags': tag_names, 'dim': dim, 'slot_dim': slot_dim, 'hidden': hidden,
            'dataset': info, 'created': time.strftime('%Y-%m-%dT%H:%M:%S'), 'epochs': epochs,
            'train_seconds': round(time.perf_counter() - started, 1)}
    (out / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
    from jarvis.neo_model.evaluate import evaluate_folder
    metrics = evaluate_folder(out, data)
    meta['metrics'] = metrics
    meta['size_bytes'] = sum(p.stat().st_size for p in (out / 'intent.npz', out / 'slots.npz'))
    (out / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8')
    log(f"metrics: {json.dumps(metrics, ensure_ascii=False)}")
    return meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--corrections', default='')
    parser.add_argument('--epochs', type=int, default=5)
    parser.add_argument('--low-priority', action='store_true')
    parser.add_argument('--dim', type=int, default=DIM)
    parser.add_argument('--slot-dim', type=int, default=SLOT_DIM)
    parser.add_argument('--hidden', type=int, default=HIDDEN)
    args = parser.parse_args(argv)
    if args.low_priority:
        try:
            import psutil
            process = psutil.Process()
            process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == 'nt' else 10)
        except Exception:
            pass
    corrections = []
    if args.corrections and Path(args.corrections).exists():
        corrections = [json.loads(line) for line in Path(args.corrections).read_text(encoding='utf-8').splitlines() if line.strip()]
    train_version(Path(args.out), corrections=corrections, epochs=args.epochs, dim=args.dim, slot_dim=args.slot_dim, hidden=args.hidden)
    return 0


if __name__ == '__main__':
    sys.exit(main())
