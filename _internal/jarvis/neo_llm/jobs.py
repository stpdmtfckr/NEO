"""NEO side of the agent-model fine-tuning: start in the background, pause while you work, status, rollback.

* The pipeline (jarvis.neo_llm.pipeline) runs as a separate low-priority process.
* A scheduler checks once a minute. It starts the pipeline only when fine-tuning is
  switched on, the computer has been idle for ``llm_agent.finetune.idle_minutes``,
  no game is running and this data was not trained yet. Automatic runs require
  existing local tools and selected weights; only explicit training may set these up.
* While it runs: as soon as you use the keyboard/mouse or a game starts, NEO creates a
  pause file and training waits between steps; it continues when you are away again.
"""
from __future__ import annotations

import json
from collections import OrderedDict
import logging
import os
import re
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any

from jarvis.neo_llm.pipeline import (CANDIDATE, FINE_TUNED, PREVIOUS, ROOT, WAITING_NOTE,
                                     base_folder, local_prerequisites, training_model, work_root)

logger = logging.getLogger(__name__)
_JOB: dict[str, Any] = {'process': None, 'started': 0.0}
_LOCK = threading.Lock()
_SCHEDULER = {'started': False}
_SETUP_LOCK = threading.Lock()
_SETUP_RETRY_SECONDS = 30 * 60
_SETUP_WAIT: OrderedDict[tuple[str, str], float] = OrderedDict()


def _setup_key(config: Any) -> tuple[str, str]:
    try:
        selected = str(base_folder(config))
    except ValueError:
        selected = repr((config.get('llm_agent.size', '0.5b'), config.get('llm_agent.base_model', '')))
    return str(work_root(config)), selected


def _waiting_for_setup(config: Any) -> bool:
    with _SETUP_LOCK:
        return time.monotonic() < _SETUP_WAIT.get(_setup_key(config), 0.0)


def _wait_for_setup(config: Any, missing: list[str]) -> None:
    root = work_root(config)
    root.mkdir(parents=True, exist_ok=True)
    payload = {'stage': 'waiting_setup', 'time': time.strftime('%Y-%m-%dT%H:%M:%S'),
               'size': str(config.get('llm_agent.size', '0.5b') or '0.5b'),
               'missing': missing, 'note': WAITING_NOTE}
    path = root / 'status.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(temporary, path)
    with _SETUP_LOCK:
        key = _setup_key(config)
        _SETUP_WAIT[key] = time.monotonic() + _SETUP_RETRY_SECONDS
        _SETUP_WAIT.move_to_end(key)
        while len(_SETUP_WAIT) > 16:
            _SETUP_WAIT.popitem(last=False)
    logger.info('%s Missing: %s', WAITING_NOTE, ', '.join(missing))


def _clear_setup_wait(config: Any) -> None:
    root = str(work_root(config))
    with _SETUP_LOCK:
        for key in list(_SETUP_WAIT):
            if key[0] == root:
                del _SETUP_WAIT[key]


def running() -> bool:
    process = _JOB['process']
    return process is not None and process.poll() is None


def start(config: Any, *, force: bool = False, local_only: bool = False) -> bool:
    with _LOCK:
        if running():
            return False
        if local_only:
            if _waiting_for_setup(config):
                return False
            missing = local_prerequisites(config)
            if missing:
                _wait_for_setup(config, missing)
                return False
        _clear_setup_wait(config)
        root = work_root(config)
        root.mkdir(parents=True, exist_ok=True)
        (root / 'stop.flag').unlink(missing_ok=True)
        (root / 'pause.flag').unlink(missing_ok=True)
        command = [sys.executable, '-m', 'jarvis.neo_llm.pipeline', 'run'] + (['--force'] if force else [])
        if local_only:
            command.append('--local-only')
        flags = (0x08000000 | 0x00004000) if os.name == 'nt' else 0     # no window, below normal priority
        with (root / 'pipeline.log').open('a', encoding='utf-8') as handle:
            _JOB['process'] = subprocess.Popen(command, cwd=str(ROOT), stdout=handle,
                                               stderr=subprocess.STDOUT, creationflags=flags)
        _JOB['started'] = time.monotonic()
        logger.info('Agent model fine-tuning started (pid %s)', _JOB['process'].pid)
        return True


def stop(config: Any) -> bool:
    root = work_root(config)
    root.mkdir(parents=True, exist_ok=True)
    (root / 'stop.flag').write_text('stop', encoding='utf-8')
    (root / 'pause.flag').unlink(missing_ok=True)
    return running()


def set_paused(config: Any, paused: bool) -> None:
    flag = work_root(config) / 'pause.flag'
    try:
        if paused:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text('user active', encoding='utf-8')
        else:
            flag.unlink(missing_ok=True)
    except OSError:
        pass


def read_status(config: Any) -> dict[str, Any]:
    root = work_root(config)
    status: dict[str, Any] = {}
    for name in ('status.json', 'train_status.json'):
        path = root / name
        if path.exists():
            try:
                status[name.split('.')[0]] = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                pass
    history = root / 'history.jsonl'
    if history.exists():
        lines = [line for line in history.read_text(encoding='utf-8').splitlines() if line.strip()]
        if lines:
            status['last_report'] = json.loads(lines[-1])
    status['running'] = running()
    status['paused'] = (root / 'pause.flag').exists()
    return status


def rollback(config: Any) -> str:
    """Select a verified prior alias without copying over any model weights."""
    snapshot = {}
    def read(data):
        snapshot.update(json.loads(json.dumps(data)))
        return False
    config.update(read)

    def get(data, key, default=None):
        value = data
        for part in key.split('.'):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    current = str(get(snapshot, 'llm_agent.fine_tuned', '') or '')
    previous = str(get(snapshot, 'llm_agent.fine_tuned_previous', '') or '')
    if get(snapshot, 'llm_agent.fine_tuned_previous') is None and current == FINE_TUNED:
        previous = PREVIOUS  # Historical names still require verified provenance.
    target = ''
    view = SimpleNamespace(get=lambda key, default=None: get(snapshot, key, default))
    if previous and _model_exists(view, previous):
        from jarvis.ollama_client import OllamaClient
        if not OllamaClient._managed_agent_alias(previous):
            target = previous  # An explicit custom choice remains authoritative.
        else:
            from jarvis.neo_llm.pipeline import model_digest, training_model
            try:
                _size, selected = training_model(view)
                proofs = get(snapshot, 'llm_agent.fine_tuned_provenance', {})
                proof = proofs.get(previous.removesuffix(':latest'), {}) if isinstance(proofs, dict) else {}
                endpoint = str(get(snapshot, 'ollama.base_url', 'http://localhost:11434') or 'http://localhost:11434').rstrip('/')
                if (proof.get('hf_base') == selected['hf'] and proof.get('ollama_base') == selected['ollama']
                        and proof.get('base_url') == endpoint and proof.get('run_key')
                        and re.fullmatch(r'sha256:[0-9a-f]{64}', str(proof.get('model_digest', '')))
                        and proof.get('model_digest') == model_digest(endpoint, previous)):
                    target = previous
            except (ValueError, TypeError, AttributeError):
                pass

    def identity(data):
        return (get(data, 'ollama.base_url'), *(get(data, 'llm_agent.' + field) for field in
                ('model', 'size', 'base_model', 'fine_tuned', 'fine_tuned_previous', 'fine_tuned_provenance')))

    def apply(data):
        if identity(data) != identity(snapshot):
            return False
        agent = data.setdefault('llm_agent', {})
        agent['fine_tuned'] = target
        agent['fine_tuned_previous'] = ''
    if config.update(apply) is False:
        return str(config.get('llm_agent.fine_tuned', '') or config.get('llm_agent.model', 'neo-agent') or 'neo-agent')
    try:
        from jarvis.runtime import agent_llm170
        agent_llm170.reset_availability()
    except Exception:
        pass
    return target or str(config.get('llm_agent.model', 'neo-agent') or 'neo-agent')


def _model_exists(config: Any, name: str) -> bool:
    try:
        from jarvis.neo_llm.setup import has_model, installed
        base_url = str(config.get('ollama.base_url', 'http://localhost:11434') or 'http://localhost:11434').rstrip('/')
        return has_model(installed(base_url), name)
    except Exception:
        return False


def _game_running() -> bool:
    try:
        from jarvis.gaming.runtime import active_game_context
        return bool(active_game_context(max_age_seconds=120.0))
    except Exception:
        return False


def due(config: Any, idle: float, *, game: bool) -> bool:
    if not bool(config.get('llm_agent.enabled', True)) or not bool(config.get('llm_agent.finetune.auto', True)):
        return False
    if running() or game:
        return False
    if idle < float(config.get('llm_agent.finetune.idle_minutes', 20) or 20) * 60:
        return False
    if _waiting_for_setup(config):
        return False
    # Scheduling needs only the latest stage, not the full growing evaluation history.
    try:
        status = json.loads((work_root(config) / 'status.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        status = {}
    if status.get('stage') == 'failed':
        # A failed run is retried at most once a day.
        try:
            failed_at = time.mktime(time.strptime(status.get('time', ''), '%Y-%m-%dT%H:%M:%S'))
        except (ValueError, OverflowError):
            failed_at = 0.0
        if time.time() - failed_at < 24 * 3600:
            return False
    state_path = work_root(config) / 'state.json'
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            state = {}
        if state.get('finished_key') and state.get('finished_key') == state.get('run_key') and status.get('stage') == 'done':
            try:
                _size, selected = training_model(config)
            except ValueError:
                return True  # local prerequisites reports the mismatch without starting training
            if not str(state['finished_key']).startswith(selected['hf'] + '|'):
                return True
            # Already fine-tuned for this data; a new correction changes the key (checked by the pipeline itself).
            return _new_corrections(config, state)
    return True


def _new_corrections(config: Any, state: dict[str, Any]) -> bool:
    try:
        from jarvis.runtime import training_corrections188
        key = training_corrections188.fingerprint(training_corrections188.eligible(config))
    except Exception:
        return False
    return f'corrections={key}|' not in str(state.get('finished_key', ''))


def start_scheduler(config: Any) -> None:
    """One timer per NEO run: starts fine-tuning when due, pauses it while you use the computer."""
    if _SCHEDULER['started']:
        return
    _SCHEDULER['started'] = True
    from jarvis.neo_model.jobs import idle_seconds

    def tick() -> None:
        try:
            if running():
                set_paused(config, _game_running() or idle_seconds() < 60)
            elif (os.name == 'nt' and bool(config.get('llm_agent.enabled', True)) and
                  bool(config.get('llm_agent.finetune.auto', True)) and not _waiting_for_setup(config)):
                if due(config, idle_seconds(), game=_game_running()):
                    start(config, local_only=True)
        except Exception:
            logger.debug('Fine-tuning scheduler tick failed', exc_info=True)
        finally:
            timer = threading.Timer(60, tick)
            timer.daemon = True
            timer.start()

    timer = threading.Timer(120, tick)
    timer.daemon = True
    timer.start()


__all__ = ['CANDIDATE', 'FINE_TUNED', 'PREVIOUS', 'due', 'read_status', 'rollback', 'running', 'set_paused', 'start',
           'start_scheduler', 'stop']
