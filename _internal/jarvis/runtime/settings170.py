"""NEO 170 settings: the small NEO agent model and its local fine-tuning.

``migrate`` only adds missing keys; values the user changed are kept.
"""
from __future__ import annotations

from typing import Any

SETTINGS_VERSION = 170

# Sizes offered in Settings and by the installer. Both are Qwen2.5 Instruct models
# (Apache-2.0): the Ollama tag runs the assistant, the Hugging Face id is the base
# for local LoRA fine-tuning (same architecture and tokenizer).
SIZES: dict[str, dict[str, str]] = {
    '1.5b': {'ollama': 'qwen2.5:1.5b', 'hf': 'Qwen/Qwen2.5-1.5B-Instruct'},
    '0.5b': {'ollama': 'qwen2.5:0.5b', 'hf': 'Qwen/Qwen2.5-0.5B-Instruct'},
}

NEW_DEFAULTS: dict[str, dict[str, Any]] = {
    'llm_agent': {
        'enabled': True,
        'size': '0.5b',
        'model': 'neo-agent',            # created from base_model with NEO's Modelfile
        'base_model': 'qwen2.5:0.5b',
        'fine_tuned': '',                # «neo-agent-ft» after a successful, better LoRA version
        'allow_large_fallback': False,   # a missing small model never selects larger weights
        'auto_install': True,            # install the model in the background if it is missing
        'keep_alive': '5m',
        'num_ctx': 3072,
        'max_tokens': 320,
        'temperature': 0.2,
        'timeout_seconds': 60,
        'candidates': 14,
        'finetune': {'auto': True, 'idle_minutes': 20, 'max_examples': 0, 'epochs': 1, 'cpu_threads': 2},
    },
}


def model_for(size: str) -> dict[str, str]:
    return SIZES.get(str(size or '0.5b').casefold(), SIZES['0.5b'])


def _merge(target: dict[str, Any], defaults: dict[str, Any]) -> None:
    for key, value in defaults.items():
        if key not in target:
            target[key] = value if not isinstance(value, dict) else dict(value)
        elif isinstance(value, dict) and isinstance(target[key], dict):
            _merge(target[key], value)


def migrate(data: dict[str, Any], version: int) -> None:
    for section, values in NEW_DEFAULTS.items():
        current = data.get(section)
        if not isinstance(current, dict):
            current = data[section] = {}
        _merge(current, values)
    data['settings_version'] = max(int(data.get('settings_version', 0) or 0), SETTINGS_VERSION)
