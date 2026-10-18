"""Evaluate a NEO command model version on the held-out test sets.

    python -m jarvis.neo_model.evaluate <version folder> [--compare <other folder>] [--rules]

Metrics (all on examples the model was not trained on):
* test_accuracy — intent correct on unseen phrasings (commands and non-commands);
* command_accuracy / typo_accuracy — commands only / the typo test set;
* none_false_command — questions and small talk the model would treat as a confident command;
* confident_wrong — commands the model is confident about but wrong (the dangerous case);
* slot_exact — every slot value extracted exactly;
* rules_accuracy — the existing grammar alone on the same commands (when --rules).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

import numpy as np

EXECUTE_THRESHOLD = 0.9


def _load(folder: Path, split: str) -> list[dict[str, Any]]:
    path = folder / 'dataset' / f'{split}.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def _predict_all(model: Any, rows: list[dict[str, Any]]) -> tuple[list[str], np.ndarray]:
    labels, confidences = [], []
    for start in range(0, len(rows), 2048):
        chunk = rows[start:start + 2048]
        probs = model.ranked_many([(r['text'], r['language'], r.get('context', '')) for r in chunk])
        best = probs.argmax(axis=1)
        labels += [model.labels[i] for i in best]
        confidences += list(probs[np.arange(len(chunk)), best])
    return labels, np.array(confidences)


def evaluate_folder(folder: Path, data: dict[str, list[dict[str, Any]]] | None = None, *, rules: bool = False) -> dict[str, Any]:
    from jarvis.neo_model.dataset import NONE
    from jarvis.neo_model.predictor import NeoCommandModel
    model = NeoCommandModel(folder)
    test = data['test'] if data else _load(folder, 'test')
    typo = data['typo_test'] if data else _load(folder, 'typo_test')
    started = time.perf_counter()
    predicted, confidence = _predict_all(model, test)
    per_example_ms = (time.perf_counter() - started) * 1000 / max(1, len(test))
    gold = [r['intent'] for r in test]
    correct = np.array([p == g for p, g in zip(predicted, gold)])
    is_none = np.array([g == NONE for g in gold])
    confident = confidence >= EXECUTE_THRESHOLD
    metrics: dict[str, Any] = {
        'test_examples': len(test),
        'test_accuracy': round(float(correct.mean()), 4) if len(test) else 0.0,
        'command_accuracy': round(float(correct[~is_none].mean()), 4) if (~is_none).any() else 0.0,
        'confident_coverage': round(float((correct & confident & ~is_none).sum() / max(1, (~is_none).sum())), 4),
        'confident_wrong': round(float((~correct & confident & ~is_none).sum() / max(1, (~is_none).sum())), 4),
        'none_false_command': round(float(np.array([p != NONE for p in np.array(predicted)[is_none]]).astype(bool)[confident[is_none]].sum() / max(1, is_none.sum())), 4) if is_none.any() else 0.0,
        'predict_ms_per_request': round(per_example_ms, 3),
    }
    by_language = {}
    for code in sorted({r['language'] for r in test}):
        mask = np.array([r['language'] == code for r in test])
        by_language[code] = round(float(correct[mask].mean()), 4)
    metrics['by_language'] = by_language
    if typo:
        typo_pred, _ = _predict_all(model, typo)
        metrics['typo_accuracy'] = round(float(np.mean([p == r['intent'] for p, r in zip(typo_pred, typo)])), 4)
        metrics['typo_examples'] = len(typo)
    slot_rows = [r for r in test if r['slots']][:3000]
    if slot_rows:
        exact = 0
        for row in slot_rows:
            found = model.slots(row['text'], row['intent'])
            wanted = {s['name']: s['value'] for s in row['slots']}
            exact += all(found.get(k, '').casefold() == v.casefold() for k, v in wanted.items())
        metrics['slot_exact'] = round(exact / len(slot_rows), 4)
        metrics['slot_examples'] = len(slot_rows)
    if rules:
        metrics['rules_accuracy'] = rules_accuracy([r for r in test if r['intent'] != NONE])
    return metrics


def rules_accuracy(rows: list[dict[str, Any]]) -> float | None:
    """The existing grammar (canonical templates) alone, on the same test commands."""
    fixture = Path(__file__).resolve().parents[2] / 'tests' / 'fixtures' / 'command_catalogue.json'
    if not fixture.exists():
        return None
    schemas = {row['tool']: row['schema'] for row in json.loads(fixture.read_text(encoding='utf-8'))['tools']}
    from jarvis.runtime.canonical_commands import parse_canonical_command
    hits = 0
    for row in rows:
        try:
            parsed = parse_canonical_command(row['text'], row['language'], lambda name: schemas.get(name, {'properties': {}}))
        except Exception:
            parsed = None
        if parsed is not None and not parsed.error and f'{parsed.tool}.{(parsed.arguments or {}).get("action", "")}' == row['intent']:
            hits += 1
    return round(hits / max(1, len(rows)), 4)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder')
    parser.add_argument('--compare', default='')
    parser.add_argument('--rules', action='store_true')
    args = parser.parse_args(argv)
    result = {'model': evaluate_folder(Path(args.folder), rules=args.rules)}
    if args.compare:
        # Same test set (from the first folder) for both versions.
        from jarvis.neo_model.evaluate import _load as load
        folder = Path(args.folder)
        data = {'test': load(folder, 'test'), 'typo_test': load(folder, 'typo_test')}
        result['compare'] = evaluate_folder(Path(args.compare), data)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
