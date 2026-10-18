"""NEO 188 NEO-PRIVACY-002 / TZ-CORRECTION-TRAINING-DATA-CONTROL-01.

A confirmed correction («нет, я имел в виду …») keeps working at once as an
exact local rule (memory169.correction_rule). Using it to TRAIN a model is a
separate decision:

* off by default — applying a correction is not consent to training;
* a global switch (``privacy.train_on_corrections``) plus per-example exclusion;
* training gets a separate MINIMIZED copy: private values that appear in both
  the phrase and the target arguments are replaced consistently by typed
  synthetic values; free text, secrets, values that cannot be matched on both
  sides, or anything doubtful exclude the WHOLE example (fail closed);
* eligibility is evaluated at the moment of use; the fingerprint of the
  eligible set enters the run key, so a stale prepared train.jsonl is never
  reused after an exclusion.

Excluding a correction does not «unlearn» an already trained model: the old
adapter/checkpoint may still carry its influence; that is said in Settings.
The operational correction rule and the corrections file are never modified
here.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_FREE_TEXT_KEYS = {'text', 'message', 'content', 'body', 'note', 'caption', 'reply', 'prompt', 'description'}
_PATH_KEYS = {'path', 'source', 'destination', 'folder', 'file', 'directory'}
_NAME_KEYS = {'name', 'query', 'title', 'target', 'playlist', 'artist', 'app', 'application', 'label', 'server', 'channel',
              'contact', 'recipient', 'chat', 'user', 'device_name'}
_SYNTHETIC = {
    'path': ('C:\\Users\\User\\Documents\\file{n}.txt',),
    'url': ('https://example.com/page{n}',),
    'email': ('user{n}@example.com',),
    'name': ('Example{n}',),
}
_URL = re.compile(r'(?i)\bhttps?://\S+')
_EMAIL = re.compile(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+')
_WIN_PATH = re.compile(r'(?i)\b[A-Z]:\\[^\r\n"<>|?*]+')


def allowed(config: Any) -> bool:
    """The global, explicit opt-in. Default: off."""
    try:
        return bool(config.get('privacy.train_on_corrections', False))
    except Exception:
        return False


def _norm(text: str) -> str:
    return ' '.join(re.sub(r'[^\w\s]', ' ', str(text or '').casefold().replace('ё', 'е')).split())


def example_id(row: dict[str, Any]) -> str:
    """Stable ID of one correction (language + normalized phrase); no text in it."""
    key = str(row.get('language', '')) + '\0' + _norm(row.get('text', ''))
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]


def _exclusions_path(config: Any) -> Path | None:
    root = getattr(getattr(config, 'paths', None), 'root', None)
    return Path(root) / 'neo_model' / 'training_exclusions.json' if root else None


def excluded_ids(config: Any) -> set[str]:
    path = _exclusions_path(config)
    if path is None or not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise RuntimeError('training exclusions are unreadable')   # fail closed: no training copy
    return {str(x) for x in data.get('excluded', [])} if isinstance(data, dict) else set()


def set_excluded(config: Any, identifier: str, excluded: bool = True) -> bool:
    path = _exclusions_path(config)
    if path is None:
        return False
    try:
        current = excluded_ids(config)
    except RuntimeError:
        current = set()
    (current.add if excluded else current.discard)(str(identifier))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'excluded': sorted(current)}, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)
    return True


def _contains_secret(text: str) -> bool:
    from jarvis.runtime.diagnostics import redact_credentials
    value = str(text or '')
    return redact_credentials(value) != value


def _kind(key: str, value: str) -> str | None:
    low = key.casefold()
    if _URL.fullmatch(value.strip()):
        return 'url'
    if _EMAIL.fullmatch(value.strip()):
        return 'email'
    if low in _PATH_KEYS or _WIN_PATH.fullmatch(value.strip()) or value.startswith(('/', '\\\\')):
        return 'path'
    if low in _NAME_KEYS:
        return 'name'
    return None


def minimize(row: dict[str, Any]) -> dict[str, Any] | None:
    """A consistent private-value-free training pair, or None to exclude it entirely."""
    text = ' '.join(str(row.get('text', '') or '').split())
    right = row.get('right') if isinstance(row.get('right'), dict) else None
    if not text or not right or not right.get('tool') or _contains_secret(text):
        return None
    arguments = dict(right.get('arguments') or {})
    if _contains_secret(json.dumps(arguments, ensure_ascii=False)):
        return None
    counters: dict[str, int] = {}
    replaced: dict[str, str] = {}
    new_arguments: dict[str, Any] = {}
    for key, value in arguments.items():
        if key == 'action' or not isinstance(value, str) or not value.strip():
            if isinstance(value, (dict, list)):
                return None              # nested payloads cannot be matched reliably
            new_arguments[key] = value
            continue
        if key.casefold() in _FREE_TEXT_KEYS:
            return None                  # free text: the meaning cannot be kept safely
        kind = _kind(key, value)
        if kind is None:
            if len(value) > 40 or any(ch.isdigit() for ch in value) and len(value) > 12:
                return None              # an unknown, possibly private value
            new_arguments[key] = value
            continue
        original = value.strip()
        if original.casefold() not in replaced:
            if original.casefold() not in text.casefold():
                return None              # not visible verbatim in the phrase → no consistent pair
            counters[kind] = counters.get(kind, 0) + 1
            replaced[original.casefold()] = _SYNTHETIC[kind][0].format(n=counters[kind])
        synthetic = replaced[original.casefold()]
        new_arguments[key] = synthetic
        text = re.sub(re.escape(original), synthetic.replace('\\', '\\\\'), text, flags=re.I)
    # Anything private still visible in the phrase but not mapped excludes the example.
    if _URL.search(text) and not all(u.startswith('https://example.com/') for u in _URL.findall(text)):
        return None
    if _EMAIL.search(text) and not all(e.endswith('@example.com') for e in _EMAIL.findall(text)):
        return None
    if _WIN_PATH.search(text) and not all('Users\\User\\Documents' in p for p in _WIN_PATH.findall(text)):
        return None
    return {'language': row.get('language', ''), 'text': text, 'right': {'tool': right['tool'], 'arguments': new_arguments},
            'confirmed': True, 'minimized': True, 'id': example_id(row)}


def eligible(config: Any, rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Minimized copies allowed for training NOW (empty when off or anything is unclear)."""
    if not allowed(config):
        return []
    try:
        excluded = excluded_ids(config)
    except RuntimeError:
        return []
    if rows is None:
        from jarvis.runtime import memory169
        rows = memory169.load_corrections(config) if memory169.corrections_path(config) else []
    out = []
    for row in rows:
        if not isinstance(row, dict) or example_id(row) in excluded:
            continue
        copy = minimize(row)
        if copy is not None:
            out.append(copy)
    return out


def fingerprint(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(row, ensure_ascii=False, sort_keys=True).encode('utf-8'))
    return digest.hexdigest()[:16] if rows else 'none'


def write_training_copy(config: Any, path: Path) -> tuple[Path | None, str]:
    """Write the eligible minimized copy for a training run; (None, 'none') when nothing is allowed."""
    rows = eligible(config)
    key = fingerprint(rows)
    if not rows:
        Path(path).unlink(missing_ok=True)
        return None, key
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(path).with_suffix('.tmp')
    temporary.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
    temporary.replace(path)
    return Path(path), key


_NOTE = ('Исправления команд работают сразу и без обучения. Для обучения моделей NEO используются только разрешённые исправления, '
         'и только в обезличенном виде. Уже обученная модель может сохранять влияние прежних данных: запрет действует на следующие обучения.')


def settings_note() -> str:
    return _NOTE
