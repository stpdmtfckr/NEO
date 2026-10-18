"""Install the NEO agent model: pull the small Qwen base, create «neo-agent», check it answers.

    python -m jarvis.neo_llm.setup [--size 0.5b|1.5b] [--no-settings] [--check-only]

Used by the installer (NEO_Setup.exe), by «NEO.cmd --install-deps» and by NEO itself
when the model is missing. Talks to the local Ollama HTTP API only.

Exit codes: 0 ready · 2 Ollama not reachable · 3 download failed · 4 create failed ·
5 the model did not answer in the expected format · 6 unsupported configured base.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import Any, Callable

import requests

ROOT = Path(__file__).resolve().parents[2]
AGENT_NAME = 'neo-agent'
SYSTEM = ("You are NEO, a local assistant on the user's Windows computer. You act only through NEO commands "
          "and always answer with one JSON object as the instructions describe.")
PARAMETERS = {'temperature': 0.2, 'num_ctx': 3072, 'top_p': 0.9}


def _url(config: Any | None) -> str:
    value = 'http://localhost:11434'
    if config is not None:
        value = str(config.get('ollama.base_url', value) or value)
    return value.rstrip('/')


def installed(base_url: str) -> list[str]:
    response = requests.get(f'{base_url}/api/tags', timeout=10)
    response.raise_for_status()
    return [str(m.get('name', '')) for m in response.json().get('models', []) if m.get('name')]


def has_model(names: list[str], wanted: str) -> bool:
    return wanted in names or f'{wanted}:latest' in names


def pull(base_url: str, model: str, log: Callable[[str], None] = print) -> None:
    last = -10
    with requests.post(f'{base_url}/api/pull', json={'model': model, 'name': model, 'stream': True}, stream=True,
                       timeout=(10, 3600)) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line:
                continue
            event = json.loads(line)
            if event.get('error'):
                raise RuntimeError(str(event['error']))
            total, done = event.get('total') or 0, event.get('completed') or 0
            if total:
                percent = int(done * 100 / total)
                if percent >= last + 10:
                    last = percent
                    log(f'{model}: {percent}%')
            elif event.get('status'):
                log(f"{model}: {event['status']}")


def modelfile(base: str) -> str:
    lines = [f'FROM {base}', f'SYSTEM """{SYSTEM}"""']
    lines += [f'PARAMETER {k} {v}' for k, v in PARAMETERS.items()]
    return '\n'.join(lines) + '\n'


def create(base_url: str, name: str, base: str, log: Callable[[str], None] = print) -> None:
    """Create ``name`` FROM ``base`` with NEO's system prompt and parameters (new API, then the older one)."""
    attempts = [
        {'model': name, 'from': base, 'system': SYSTEM, 'parameters': PARAMETERS, 'stream': False},
        {'name': name, 'modelfile': modelfile(base), 'stream': False},
    ]
    error = ''
    for body in attempts:
        response = requests.post(f'{base_url}/api/create', json=body, timeout=(10, 900))
        if response.status_code < 400:
            log(f'{name}: created from {base}')
            return
        error = response.text[:300]
    raise RuntimeError(error or 'create failed')


def smoke_test(base_url: str, model: str) -> float:
    """One real request in the agent format; returns the time in seconds."""
    from jarvis.neo_llm import prompt
    patterns = [prompt.pattern_for(i, 'en') for i in prompt.CORE_INTENTS]
    body = {'model': model, 'stream': False, 'think': False, 'format': prompt.SCHEMA, 'keep_alive': '1m',
            'messages': prompt.messages('open the first search result for cats', 'en', patterns),
            'options': {'num_predict': 80, 'temperature': 0}}
    started = time.perf_counter()
    response = requests.post(f'{base_url}/api/chat', json=body, timeout=(10, 600))
    if response.status_code == 400 and 'think' in response.text:
        body.pop('think')
        response = requests.post(f'{base_url}/api/chat', json=body, timeout=(10, 600))
    response.raise_for_status()
    content = str((response.json().get('message') or {}).get('content', ''))
    value = json.loads(content)
    if not isinstance(value, dict) or not ({'do', 'reply', 'ask'} & set(value)):
        raise ValueError(content[:200])
    return time.perf_counter() - started


def save_settings(size: str, base: str) -> None:
    from jarvis.config import ConfigManager
    config = ConfigManager(ROOT / 'config' / 'default_settings.json')
    data = json.loads(json.dumps(config.data))
    agent = data.setdefault('llm_agent', {})
    agent.update({'size': size, 'base_model': base, 'model': AGENT_NAME})
    config.save(data)


def ensure(size: str | None = None, *, config: Any | None = None, write_settings: bool = True, check_only: bool = False,
           log: Callable[[str], None] = print) -> int:
    from jarvis.runtime.settings170 import SIZES
    if size is None and config is not None:
        from jarvis.neo_llm.pipeline import training_model
        try:
            size, selected = training_model(config)
        except ValueError as exc:
            log(str(exc))
            return 6
    else:
        size = str(size or '0.5b').strip().casefold()
        if size not in SIZES:
            log(f'Unsupported assistant model size: {size}.')
            return 6
        selected = SIZES[size]
    base = selected['ollama']
    base_url = _url(config)
    try:
        from jarvis.ollama_runtime import ensure_ollama_running
        ensure_ollama_running(base_url, timeout=30)
    except Exception:
        pass
    try:
        names = installed(base_url)
    except requests.RequestException as exc:
        log(f'Ollama is not reachable at {base_url}: {exc}')
        return 2
    if check_only:
        ready = has_model(names, AGENT_NAME)
        log(f'{AGENT_NAME}: {"installed" if ready else "missing"}')
        return 0 if ready else 4
    if not has_model(names, base):
        log(f'Downloading {base}…')
        try:
            pull(base_url, base, log)
        except Exception as exc:
            log(f'Download of {base} failed: {exc}')
            return 3
    try:
        create(base_url, AGENT_NAME, base, log)
    except Exception as exc:
        log(f'Creating {AGENT_NAME} failed: {exc}')
        return 4
    try:
        seconds = smoke_test(base_url, AGENT_NAME)
        log(f'{AGENT_NAME} answers in the NEO format ({seconds:.1f} s for the first answer, including loading).')
    except Exception as exc:
        log(f'{AGENT_NAME} did not answer in the expected format: {str(exc)[:200]}')
        return 5
    if write_settings:
        try:
            save_settings(size, base)
        except Exception as exc:
            log(f'Settings were not updated: {exc}')
    return 0


_BACKGROUND = {'thread': None, 'status': '', 'code': None}


def ensure_background(config: Any, *, size: str | None = None, force: bool = False) -> bool:
    """Install the agent model in a background thread (once per NEO run unless forced). True if started."""
    import threading
    thread = _BACKGROUND['thread']
    if thread is not None and (thread.is_alive() or not force):
        return False
    if not force and not bool(config.get('llm_agent.auto_install', True)):
        return False
    wanted = str(size or config.get('llm_agent.size', '0.5b') or '0.5b')
    if size is None:
        from jarvis.neo_llm.pipeline import training_model
        try:
            wanted, _selected = training_model(config)
        except ValueError as exc:
            _BACKGROUND.update(status=str(exc), code=6)
            return False
    defaults = {'ollama.base_url': 'http://localhost:11434', 'llm_agent.size': '0.5b',
                'llm_agent.base_model': 'qwen2.5:0.5b', 'llm_agent.model': AGENT_NAME,
                'llm_agent.fine_tuned': ''}

    def selection() -> tuple[str, ...]:
        return tuple(_url(config) if key == 'ollama.base_url' else str(config.get(key, value) or value).strip()
                     for key, value in defaults.items())

    captured = selection()
    captured_values = dict(zip(defaults, captured))
    operation_config = SimpleNamespace(get=captured_values.get)

    def log(line: str) -> None:
        _BACKGROUND['status'] = line
        import logging
        logging.getLogger(__name__).info('Agent model setup: %s', line)

    def run() -> None:
        try:
            if selection() != captured:
                log('Model settings changed before setup started; request setup again for the new selection.')
                _BACKGROUND['code'] = 7
                return
            _BACKGROUND['code'] = ensure(wanted, config=operation_config, write_settings=False, log=log)
            if _BACKGROUND['code'] == 0:
                if selection() != captured:
                    log('Setup finished for the previous selection; the current model settings were preserved.')
                    return
                def commit(data):
                    # The callback receives one current snapshot under the same
                    # transaction as Save. No backend work occurs under its lock.
                    values = []
                    for key, default in defaults.items():
                        section, field = key.split('.', 1)
                        node = data.get(section, {})
                        value = node.get(field, default) if isinstance(node, dict) else default
                        value = str(value or default).strip()
                        values.append(value.rstrip('/') if key == 'ollama.base_url' else value)
                    if tuple(values) != captured:
                        return False
                    data.setdefault('llm_agent', {}).update({'size': wanted, 'base_model': model_for_size(wanted)})
                if config.update(commit) is False:
                    log('Setup finished for the previous selection; the current model settings were preserved.')
                    return
                from jarvis.runtime import agent_llm170
                agent_llm170.reset_availability()
        except Exception as exc:
            log(f'failed: {exc}')
            _BACKGROUND['code'] = 1

    thread = threading.Thread(target=run, name='NEO-agent-model-setup', daemon=True)
    _BACKGROUND['thread'] = thread
    thread.start()
    return True


def model_for_size(size: str) -> str:
    from jarvis.runtime.settings170 import model_for
    return model_for(size)['ollama']


def background_status() -> tuple[str, int | None, bool]:
    thread = _BACKGROUND['thread']
    return str(_BACKGROUND['status']), _BACKGROUND['code'], bool(thread is not None and thread.is_alive())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--size', default='0.5b', choices=['0.5b', '1.5b'])
    parser.add_argument('--no-settings', action='store_true')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args(argv)
    return ensure(args.size, write_settings=not args.no_settings, check_only=args.check_only)


if __name__ == '__main__':
    sys.exit(main())
