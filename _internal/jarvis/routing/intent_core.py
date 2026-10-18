"""Incremental declarative intent groups; the mature registry remains authoritative.

Only complete selected-locale phrases bind a reviewed tool/action. No model,
heuristic action guessing, plugins or new execution privileges are introduced.
Definitions and language variants are shared with the corpus runner.
"""
from __future__ import annotations
import json
import re
from functools import lru_cache
from pathlib import Path
from jarvis.runtime.natural_commands import LANGUAGES, norm, strip_request_shell

def phrase_key(text: str) -> str:
    return norm(re.sub(r"[,;:]+", " ", str(text)))

@lru_cache(maxsize=1)
def assistive_groups() -> tuple[dict, ...]:
    path = Path(__file__).resolve().parents[2] / 'config' / 'intents' / 'assistive146.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema_version') != 1:
        raise ValueError('Unsupported intent group schema')
    return tuple(data['groups'])

@lru_cache(maxsize=7)
def _selected_index(code: str) -> dict[str, tuple[str, str]]:
    if code not in LANGUAGES:
        return {}
    result = {}
    for group in assistive_groups():
        target = (str(group['tool']), str(group['action']))
        for phrase in group['phrases'].get(code, []):
            key = phrase_key(strip_request_shell(phrase, code))
            if key in result and result[key] != target:
                raise ValueError('Conflicting reviewed intent phrase: ' + code + ':' + key)
            result[key] = target
    return result

def match_assistive_group(text: str, code: str) -> tuple[str, dict] | None:
    row = _selected_index(code).get(phrase_key(strip_request_shell(text, code)))
    return (row[0], {'action': row[1]}) if row else None
