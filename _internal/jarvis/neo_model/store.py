"""Versions of the NEO command model: which one is active, promotion checks and rollback.

* The version shipped with NEO lives in ``_internal/models/neo_command/<version>``.
* Versions trained on this computer live in ``<data>/neo_model/versions/<version>``.
* ``<data>/neo_model/state.json`` names the active and the previous version.

A new version becomes active only if, on the same test set, it is not worse than
the active one; otherwise it is kept for inspection and the working model stays.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import time
from typing import Any

BUNDLED = Path(__file__).resolve().parents[2] / 'models' / 'neo_command'
# How much worse a new version may be on each metric before it is rejected.
TOLERANCE = {'test_accuracy': -0.005, 'command_accuracy': -0.005, 'typo_accuracy': -0.01,
             'confident_wrong': 0.005, 'none_false_command': 0.01}


def user_root(config: Any) -> Path:
    return Path(config.paths.root) / 'neo_model'


def _state_path(config: Any) -> Path:
    return user_root(config) / 'state.json'


def state(config: Any) -> dict[str, Any]:
    path = _state_path(config)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(config: Any, data: dict[str, Any]) -> None:
    path = _state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    temporary.replace(path)


def _complete(folder: Path) -> bool:
    return all((folder / name).exists() for name in ('meta.json', 'intent.npz', 'slots.npz'))


def versions(config: Any) -> list[dict[str, Any]]:
    rows = []
    for origin, root in (('bundled', BUNDLED), ('local', user_root(config) / 'versions')):
        if not root.exists():
            continue
        for folder in sorted(root.iterdir()):
            if folder.is_dir() and _complete(folder):
                meta = json.loads((folder / 'meta.json').read_text(encoding='utf-8'))
                rows.append({'version': folder.name, 'origin': origin, 'path': str(folder), 'created': meta.get('created', ''),
                             'metrics': meta.get('metrics', {}), 'dataset': meta.get('dataset', {}).get('fingerprint', '')})
    return rows


def folder_of(config: Any, version: str) -> Path | None:
    for row in versions(config):
        if row['version'] == version:
            return Path(row['path'])
    return None


def active_folder(config: Any) -> Path | None:
    chosen = state(config).get('active')
    if chosen:
        folder = folder_of(config, chosen)
        if folder is not None:
            return folder
    bundled = [r for r in versions(config) if r['origin'] == 'bundled']
    return Path(bundled[-1]['path']) if bundled else None


def next_version_id(config: Any) -> str:
    existing = {r['version'] for r in versions(config)}
    number = 1 + sum(1 for v in existing if v.startswith('local-'))
    while True:
        candidate = f'local-{number:03d}-{time.strftime("%Y%m%d")}'
        if candidate not in existing:
            return candidate
        number += 1


def compare(new: dict[str, Any], old: dict[str, Any]) -> list[str]:
    """Metrics on which the new version is worse than allowed (empty list: may be promoted)."""
    problems = []
    for key, tolerance in TOLERANCE.items():
        if key not in new or key not in old:
            continue
        worse = (new[key] < old[key] + tolerance) if tolerance < 0 else (new[key] > old[key] + tolerance)
        if worse:
            problems.append(f'{key}: {old[key]} -> {new[key]}')
    return problems


def promote(config: Any, candidate: Path) -> tuple[bool, list[str], dict[str, Any]]:
    """Activate ``candidate`` if it is not worse than the active version on the candidate's test set."""
    from jarvis.neo_model.evaluate import _load, evaluate_folder
    data = {'test': _load(candidate, 'test'), 'typo_test': _load(candidate, 'typo_test')}
    new_metrics = evaluate_folder(candidate, data)
    current = active_folder(config)
    old_metrics = evaluate_folder(current, data) if current is not None else {}
    problems = compare(new_metrics, old_metrics) if old_metrics else []
    report = {'candidate': candidate.name, 'active_before': current.name if current else '', 'new': new_metrics, 'old': old_metrics}
    history = state(config)
    history.setdefault('trainings', []).append({'version': candidate.name, 'time': time.strftime('%Y-%m-%dT%H:%M:%S'),
                                                'promoted': not problems, 'problems': problems})
    history['trainings'] = history['trainings'][-50:]
    if not problems:
        history['previous'] = current.name if current else ''
        history['active'] = candidate.name
    _save_state(config, history)
    return not problems, problems, report


def rollback(config: Any, version: str = '') -> str | None:
    """Back to ``version`` or to the previous working version; returns the now active version."""
    data = state(config)
    target = version or data.get('previous') or ''
    if not target:
        bundled = [r['version'] for r in versions(config) if r['origin'] == 'bundled']
        target = bundled[-1] if bundled else ''
    if not target or folder_of(config, target) is None:
        return None
    data['previous'], data['active'] = data.get('active', ''), target
    _save_state(config, data)
    return target


def remove_local(config: Any, version: str) -> bool:
    if state(config).get('active') == version:
        return False
    folder = user_root(config) / 'versions' / version
    if not folder.exists():
        return False
    shutil.rmtree(folder)
    return True
