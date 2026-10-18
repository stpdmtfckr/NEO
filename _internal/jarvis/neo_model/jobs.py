"""Retraining the NEO command model on this computer.

Training starts only when the user asks («дообучи модель команд») or, if the user
switched it on, on a local schedule while the computer is idle. It runs in a
separate process with below-normal priority, never inside the voice command path.
When it finishes, the new version is compared with the active one on the same
test set and becomes active only if it is not worse; otherwise it is kept aside.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from jarvis.neo_model import store

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
_JOB: dict[str, Any] = {'process': None, 'version': '', 'started': 0.0, 'result': None}
_LOCK = threading.Lock()


def running() -> bool:
    process = _JOB.get('process')
    return process is not None and process.poll() is None


def last_result() -> dict[str, Any] | None:
    return _JOB.get('result')


def start(config: Any, *, on_done: Callable[[dict[str, Any]], None] | None = None) -> str:
    """Start training in the background; returns the new version id."""
    with _LOCK:
        if running():
            return _JOB['version']
        version = store.next_version_id(config)
        out = store.user_root(config) / 'versions' / version
        out.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, '-m', 'jarvis.neo_model.train', '--out', str(out), '--low-priority',
                   '--epochs', str(int(config.get('neo_model.epochs', 5) or 5))]
        # NEO 188 NEO-PRIVACY-002: only an explicitly allowed, minimized copy is
        # handed to training (never the raw corrections file); checked right now.
        if bool(config.get('neo_model.train_on_corrections', True)):
            from jarvis.runtime import training_corrections188
            copy, _key = training_corrections188.write_training_copy(config, out.parent / (version + '.corrections.jsonl'))
            if copy is not None:
                command += ['--corrections', str(copy)]
        log_path = store.user_root(config) / 'training.log'
        handle = log_path.open('w', encoding='utf-8')
        flags = 0x08000000 if os.name == 'nt' else 0          # CREATE_NO_WINDOW
        process = subprocess.Popen(command, cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT, creationflags=flags)
        _JOB.update(process=process, version=version, started=time.monotonic(), result=None)

    def watch() -> None:
        code = process.wait()
        handle.close()
        result: dict[str, Any] = {'version': version, 'exit_code': code, 'seconds': round(time.monotonic() - _JOB['started'], 1)}
        if code == 0:
            try:
                promoted, problems, report = store.promote(config, out)
                result.update(promoted=promoted, problems=problems, new=report['new'], old=report['old'])
            except Exception as exc:
                logger.exception('Model comparison failed')
                result.update(promoted=False, problems=[f'comparison failed: {exc}'])
        else:
            result.update(promoted=False, problems=[f'training failed (exit {code}), see {log_path}'])
        _JOB['result'] = result
        if result.get('promoted'):
            from jarvis.neo_model import runtime
            runtime.unload()     # the next request loads the new active version
        logger.info('NEO model training finished: %s', json.dumps({k: v for k, v in result.items() if k not in {'new', 'old'}}))
        if on_done is not None:
            try:
                on_done(result)
            except Exception:
                logger.debug('Training callback failed', exc_info=True)

    threading.Thread(target=watch, name='neo-model-training', daemon=True).start()
    return version


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input (Windows); 0 elsewhere."""
    if os.name != 'nt':
        return 0.0
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


def schedule_due(config: Any, now: time.struct_time | None = None) -> bool:
    """Optional local schedule (off by default): the chosen hour, once a day, only when the PC is idle."""
    if not bool(config.get('neo_model.schedule.enabled', False)) or running():
        return False
    now = now or time.localtime()
    hour = int(config.get('neo_model.schedule.hour', 3) or 3)
    if now.tm_hour != hour:
        return False
    last = store.state(config).get('last_scheduled_day', '')
    if last == time.strftime('%Y-%m-%d', now):
        return False
    return idle_seconds() >= float(config.get('neo_model.schedule.idle_minutes', 20) or 20) * 60


_SCHEDULER_STARTED = [False]


def start_scheduler(config: Any) -> None:
    """A single timer that wakes every 10 minutes only when the schedule is switched on (read at start-up)."""
    if _SCHEDULER_STARTED[0] or not bool(config.get('neo_model.schedule.enabled', False)):
        return
    _SCHEDULER_STARTED[0] = True

    def tick() -> None:
        try:
            if schedule_due(config):
                data = store.state(config)
                data['last_scheduled_day'] = time.strftime('%Y-%m-%d')
                store._save_state(config, data)
                start(config)
        finally:
            timer = threading.Timer(600, tick)
            timer.daemon = True
            timer.start()

    timer = threading.Timer(600, tick)
    timer.daemon = True
    timer.start()
