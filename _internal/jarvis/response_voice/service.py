"""Bounded off-thread voice and model operations, with no Qt object in a worker."""
from __future__ import annotations
import json
from collections import deque
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from .model_store import ModelStore, VoiceModelError, Cancelled
from .runtime import ensure_runtime, runtime_available, runtime_python
from .diagnostics import STAGES, record_failure, stage_event

DEFAULTS = {'enabled': False, 'speaker': 'Aiden', 'volume': 80, 'cpu_threads': 2,
            'idle_unload_seconds': 15, 'max_characters': 600, 'generation_timeout_seconds': 180,
            'tier': 'standard', 'robot_effect': 45}


def options(config):
    raw = config.get('response_voice', {}) or {}
    out = dict(DEFAULTS, **raw) if isinstance(raw, dict) else dict(DEFAULTS)
    out['enabled'] = out['enabled'] is True
    out['speaker'] = out['speaker'] if out['speaker'] in ('Aiden', 'Serena') else 'Aiden'
    out['tier'] = out['tier'] if out['tier'] in ('light', 'standard', 'heavy') else 'standard'
    for key, low, high in [('volume',0,100),('cpu_threads',1,4),('idle_unload_seconds',0,120),
                           ('max_characters',100,2000),('generation_timeout_seconds',30,600),('robot_effect',0,100)]:
        try: out[key] = max(low, min(high, int(out[key])))
        except (ValueError, TypeError): out[key] = DEFAULTS[key]
    return out


def spoken_text(text, limit):
    text = re.sub(r'```.*?```', '', str(text), flags=re.S)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\[([^]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'[*_#`]+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(' ', 1)[0].rstrip(' ,;:') + '.'
    return text


class ProcessTransport:
    def __init__(self, profile, model, threads):
        from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
        self.events = queue.Queue(maxsize=32)
        internal = Path(__file__).resolve().parents[2]
        env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                   PYTHONPATH=str(internal), PYTHONUTF8='1', OMP_NUM_THREADS=str(threads), MKL_NUM_THREADS=str(threads))
        self.process = subprocess.Popen([str(runtime_python(profile)), '-u', '-m', 'jarvis.response_voice.worker',
                                         str(model), str(threads)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, encoding='utf-8', env=env,
                                         cwd=str(internal), **hidden_process_kwargs())
        threading.Thread(target=self._read, daemon=True, name='NEO-Voice-Protocol').start()

    def _read(self):
        try:
            for line in self.process.stdout:
                if len(line) > 4096: continue
                try: event = json.loads(line)
                except ValueError: continue
                try: self.events.put(event, timeout=.1)
                except queue.Full: break
        finally:
            try: self.events.put({'state':'process_exited'}, timeout=.1)
            except queue.Full: pass

    def send(self, request):
        self.process.stdin.write(json.dumps(request, ensure_ascii=False) + '\n')
        self.process.stdin.flush()

    def receive(self, timeout):
        return self.events.get(timeout=timeout)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()  # Immediate cancellation also stops audio; no GUI-thread wait.

    def reap(self):
        self.close()
        self.process.wait(timeout=10)
        for stream in (self.process.stdin, self.process.stdout):
            if stream: stream.close()


class SystemProcessTransport(ProcessTransport):
    """Light tier: Windows voices in NEO's own Python; no model files, no voice runtime."""
    def __init__(self, profile, model, threads):
        from jarvis.runtime.hidden_subprocess import hidden_process_kwargs
        from jarvis.runtime.process_host171 import base_python
        self.events = queue.Queue(maxsize=32)
        internal = Path(__file__).resolve().parents[2]
        env = dict(os.environ, PYTHONPATH=str(internal), PYTHONUTF8='1')
        self.process = subprocess.Popen([str(base_python()), '-u', '-m', 'jarvis.response_voice.system_worker'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding='utf-8', env=env, cwd=str(internal), **hidden_process_kwargs())
        threading.Thread(target=self._read, daemon=True, name='NEO-Voice-Protocol').start()


class VoiceService:
    def __init__(self, config, *, store=None, process_factory=None, runtime_check=None, runtime_installer=None):
        self.config = config
        # NEO 185: three tiers (light = Windows voices, standard = Qwen3-TTS 0.6B, heavy = 1.7B).
        # Injected parts (tests) pin the service to them; otherwise the tier follows Settings.
        self._fixed185 = store is not None or process_factory is not None
        self._custom_runtime185 = (runtime_check, runtime_installer)
        self._custom_factory185 = process_factory
        self._tier185 = None
        self.store = store
        self._apply_tier185(options(config)['tier'] if store is None else None)
        self._queue = queue.Queue(maxsize=2)
        self._lock = threading.RLock()
        self._generation = 0
        self._closed = False
        self._thread = None
        self._process = None
        self._process_threads = None
        self._operation_thread = None
        self._operation_cancel = threading.Event()
        self._voice_cancel = threading.Event()
        self._retired_processes = deque()
        self._playback_started = 0.0
        self._playback_intervals = deque(maxlen=32)
        self._playing = False
        self._echo_until = 0.0
        self._voice_state = 'idle'
        # NEO 188 NEO-TTS-001: one lifecycle and exactly one terminal outcome per utterance.
        self._utterance_seq = 0
        self._utterances = {}
        self._terminal_listeners = []
        self._state = {'state':self.store.verification_error() or 'unchecked', 'done':0,
                       'total':self.store.manifest['total_bytes'], 'running':False}
        if self.store.tombstone.exists(): self._state['state'] = 'deleted'

    def _apply_tier185(self, name):
        runtime_check, runtime_installer = self._custom_runtime185
        if name is not None:
            from .tiers import store_for
            self.store = store_for(self.config.paths.root, name)
        light = name == 'light'
        self._tier185 = name
        self._process_factory = self._custom_factory185 or (SystemProcessTransport if light else ProcessTransport)
        self._runtime_check = runtime_check or ((lambda _profile: True) if light else runtime_available)
        self._runtime_installer = runtime_installer or ((lambda *_a, **_k: None) if light else ensure_runtime)

    def select_tier(self, name):
        """Settings page: follow the tier picked in the (not yet saved) dialog; None = saved value."""
        self._selected185 = name if name in ('light', 'standard', 'heavy') else None
        self._sync_tier185()

    @property
    def tier(self):
        return self._tier185

    def _sync_tier185(self):
        """Follow a tier change from Settings; only between operations, never mid-speech."""
        if self._fixed185:
            return
        name = getattr(self, '_selected185', None) or options(self.config)['tier']
        with self._lock:
            if name == self._tier185 or self._state['running']:
                return
        self.cancel()
        with self._lock:
            self._apply_tier185(name)
            self._state = {'state': self.store.verification_error() or 'unchecked', 'done': 0,
                           'total': self.store.manifest['total_bytes'], 'running': False}
            if self.store.tombstone.exists(): self._state['state'] = 'deleted'

    def snapshot(self):
        self._sync_tier185()
        runtime_installed = self._runtime_check(self.config.paths.root)
        with self._lock:
            return dict(self._state, voice_state=self._voice_state, active=self.active, playing=self._playing,
                        runtime_installed=runtime_installed)

    TERMINAL = ('completed', 'cancelled_before_start', 'interrupted', 'failed_before_start', 'failed_after_start', 'finished_without_playback')

    def add_terminal_listener(self, callback):
        """callback(uid, outcome) — called at most once per utterance (from the voice worker thread)."""
        with self._lock:
            self._terminal_listeners.append(callback)

    def utterance_status(self, uid):
        with self._lock:
            row = self._utterances.get(uid)
            return dict(row) if row else None

    def _mark(self, uid, phase):
        with self._lock:
            row = self._utterances.get(uid)
            if row is None or row['status'] in self.TERMINAL:
                return
            row['status'] = phase
            if phase == 'playing':
                row['played'] = True

    def _terminal(self, uid, outcome):
        """First terminal event wins; duplicates and late callbacks are ignored."""
        with self._lock:
            row = self._utterances.get(uid)
            if row is None or row['status'] in self.TERMINAL:
                return False
            if outcome in ('cancelled', 'failed'):
                outcome = ('interrupted' if outcome == 'cancelled' else 'failed_after_start') if row.get('played') else \
                          ('cancelled_before_start' if outcome == 'cancelled' else 'failed_before_start')
            if outcome == 'completed' and not row.get('played'):
                outcome = 'finished_without_playback'      # synthesis done but no playback was observed
            row['status'] = outcome
            listeners = tuple(self._terminal_listeners)
            while len(self._utterances) > 64:
                self._utterances.pop(next(iter(self._utterances)))
        for callback in listeners:
            try:
                callback(uid, outcome)
            except Exception:
                pass
        return True

    @property
    def active(self):
        with self._lock:
            return self._voice_state in ('queued','checking','loading','synthesizing','playing') or not self._queue.empty()

    @property
    def echo_guard_active(self):
        with self._lock:
            return self._playing or time.monotonic() < self._echo_until

    def rejects_recording(self, recording):
        """Keep playback intervals so delayed ASR cannot execute an old self-echo."""
        with self._lock:
            if self.echo_guard_active: return True
            if recording is None: return False
            end = float(getattr(recording, "captured_at", 0.0) or 0.0)
            start = float(getattr(recording, "capture_started_at", 0.0) or end)
            return bool(end and any(start <= right and end >= left for left, right in self._playback_intervals))

    def _end_playback(self):
        if self._playing:
            self._echo_until = time.monotonic() + .7
            self._playback_intervals.append((self._playback_started, self._echo_until))
        self._playing = False

    def _publish_worker(self, *, generation=None, operation=None, voice_state=None, terminal=False, **event):
        """Publish only for the still-current owner after any blocking boundary."""
        with self._lock:
            if self._closed:
                return False
            if generation is not None:
                if generation != self._generation or self._state['running']:
                    return False
            elif operation is not None:
                if (operation is not self._operation_cancel or not self._state['running']
                        or (operation.is_set() and not terminal)):
                    return False
            else:
                raise ValueError('worker publication requires an owner')
            if voice_state is not None:
                self._voice_state = voice_state
                if voice_state == 'playing':
                    if not self._playing:
                        self._playback_started = time.monotonic()
                    self._playing = True
                elif voice_state not in ('queued', 'checking', 'loading', 'synthesizing'):
                    self._end_playback()
            self._state.update(event)
            return True

    def model_operation(self, action):
        if action not in ('download','check','delete'): return False
        self._sync_tier185()
        with self._lock:
            if self._closed or self._state['running']: return False
            self._operation_cancel = threading.Event()
            self._state.update(state='checking' if action == 'check' else action, running=True, done=0, failure=None)
            self._operation_thread = threading.Thread(target=self._operate, args=(action, self._operation_cancel),
                                                       daemon=True, name='NEO-Voice-Model')
            operation_thread = self._operation_thread
            owner = self._operation_cancel
        try:
            self.cancel()
            operation_thread.start()
        except Exception as error:
            # HIST-034: a failure between the reservation and the worker start (e.g.
            # closing the previous voice process) ends this operation's ownership,
            # so speech and a retry work again; a newer owner is never cleared.
            failure = {'code': 'prepare_failed', 'detail': type(error).__name__}
            self._publish_worker(operation=owner, terminal=True, state='prepare_failed', failure=failure, running=False)
            return False
        return True

    def _operate(self, action, cancel):
        stage={'download':'model_manifest','check':'model_check','delete':'model_delete'}[action]
        stage_event(action,stage)
        def progress(**event):
            nonlocal stage
            next_stage=event.get('stage') or {'downloading':'model_download','checking':'model_check','runtime':'runtime'}.get(event.get('state'))
            if next_stage in STAGES and next_stage!=stage:
                stage=next_stage;stage_event(action,stage)
            self._publish_worker(operation=cancel,**event)
        try:
            if action == 'delete':
                # Reap worker off the GUI thread before deleting weights on Windows.
                thread = self._thread
                if thread and thread.is_alive(): thread.join(timeout=12)
                if thread and thread.is_alive(): raise VoiceModelError('busy')
                result = self.store.delete()
            elif action == 'check':
                result = self.store.check(cancel, progress)
            else:
                getattr(self.store, 'validate_before_download', self.store._validate)()
                stage='model_download';stage_event(action,stage)
                result = self.store.download(cancel, progress, explicit=True)
                with self.store.operation_lock():
                    if self.store.tombstone.exists(): raise VoiceModelError('deleted')
                    stage='runtime';stage_event(action,stage)
                    self._runtime_installer(self.config.paths.root, cancel, progress)
            if cancel.is_set() and action != 'delete':
                raise Cancelled('cancelled')
            self._publish_worker(operation=cancel, terminal=True, **result)
        except Exception as error:
            if cancel.is_set() or isinstance(error,Cancelled):
                self._publish_worker(operation=cancel,terminal=True,state='cancelled')
            else:
                failure=record_failure(self.config.paths.root,error,action=action,stage=stage)
                self._publish_worker(operation=cancel,terminal=True,state=failure['code'],failure=failure)
        finally:
            self._publish_worker(operation=cancel, terminal=True, running=False)

    def cancel_operation(self):
        with self._lock:
            self._operation_cancel.set()
            if self._state['running']:
                self._state['state'] = 'cancelling'

    def submit(self, text, language, *, preview=False, overrides=None):
        settings = options(self.config)
        if overrides: settings.update(overrides)
        if self._closed or settings['volume'] == 0 or (not settings['enabled'] and not preview) or language not in ('ru','en','de','fr','es','it','pt'):
            return False
        text = spoken_text(text, settings['max_characters'])
        if not text: return False
        self._sync_tier185()
        with self._lock:
            if self._closed or self._state['running']: return False
            verification_error = self.store.verification_error()
            if verification_error:
                self._voice_state = verification_error; return False
            if self.store.tombstone.exists():
                self._voice_state = 'deleted'; return False
            self._utterance_seq += 1
            uid = self._utterance_seq
            self._utterances[uid] = {'status': 'queued', 'played': False, 'generation': self._generation}
            request = (self._generation, text, language, dict(settings, _uid188=uid))
            dropped = None
            try: self._queue.put_nowait(request)
            except queue.Full:
                try: dropped = self._queue.get_nowait()
                except queue.Empty: pass
                self._queue.put_nowait(request)
            if self._voice_state == 'idle': self._voice_state = 'queued'
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True, name='NEO-Response-Voice')
                self._thread.start()
        if dropped is not None:
            self._terminal(dropped[3].get('_uid188'), 'cancelled')       # replaced in the queue before it started
        return uid

    def apply_preferences(self):
        settings = options(self.config)
        if not settings['enabled'] or settings['volume'] == 0:
            self.cancel()

    def cancel(self):
        dropped = []
        with self._lock:
            self._generation += 1
            self._voice_cancel.set()
            self._voice_cancel = threading.Event()
            while not self._queue.empty():
                try: dropped.append(self._queue.get_nowait())
                except queue.Empty: break
            # Revoke ownership immediately. An idle queue.get may wake for a new
            # generation before the old worker next reaches its loop boundary.
            process, self._process = self._process, None
            self._process_threads = None
            if process is not None:
                self._retired_processes.append(process)
            self._end_playback()
            self._voice_state = 'idle'
            current = [uid for uid, row in self._utterances.items() if row['status'] not in self.TERMINAL]
        for request in dropped:
            self._terminal(request[3].get('_uid188'), 'cancelled')
        for uid in current:
            self._terminal(uid, 'cancelled')          # the one being synthesized/played now
        if process:
            process.close()  # Never hold the GUI-shared lock during transport calls.

    def close(self):
        with self._lock:
            self._closed = True
        self.cancel_operation()
        self.cancel()

    @staticmethod
    def _reap_process(process):
        try: process.reap()
        except Exception: pass

    def _reap_retired(self):
        # Called only by the voice worker, never a GUI callback. Retirement may
        # happen while it is blocked in queue.get or handling a stale event.
        with self._lock:
            retired = tuple(self._retired_processes)
            self._retired_processes.clear()
        for process in retired:
            self._reap_process(process)

    def _drop_process(self):
        with self._lock:
            process, self._process = self._process, None
            self._process_threads = None
        if process is not None:
            self._reap_process(process)

    def _run(self):
        try:
            idle_deadline = time.monotonic()
            while not self._closed:
                self._reap_retired()
                try: generation, text, language, settings = self._queue.get(timeout=.1)
                except queue.Empty:
                    # Exit promptly after cancellation and during model operations.
                    if self._process is None or self._state['running']: return
                    if time.monotonic() >= idle_deadline: return
                    continue
                # cancel() can detach a helper while queue.get is waiting. Reap
                # it before this newly dequeued generation obtains another helper.
                self._reap_retired()
                uid = settings.get('_uid188')
                with self._lock:
                    stale = generation != self._generation
                    voice_cancel = self._voice_cancel
                if stale:
                    self._terminal(uid, 'cancelled')
                    continue
                self._mark(uid, 'synthesizing')
                try:
                    cached = self.store.ready_cached()
                    if not self._publish_worker(generation=generation, voice_state=None if cached else 'checking'):
                        raise Cancelled('cancelled')
                    if not cached:
                        result = self.store.check(voice_cancel, lambda owner=generation, **event: self._publish_worker(generation=owner, **event))
                        if not self._publish_worker(generation=generation, **result):
                            raise Cancelled('cancelled')
                    if not self._runtime_check(self.config.paths.root):
                        raise VoiceModelError('runtime_missing')
                    if self._process and self._process_threads != settings['cpu_threads']:
                        self._drop_process()
                    deadline = time.monotonic() + settings['generation_timeout_seconds']
                    with self._lock:
                        if not self._publish_worker(generation=generation):
                            raise Cancelled('cancelled')
                        process = self._process
                        if process is None:
                            self._publish_worker(generation=generation, voice_state='loading')
                    if process is None:
                        # Popen can block. Publish ownership only after launch returns and
                        # this generation is still live; cancelled launches are reaped here.
                        candidate = self._process_factory(self.config.paths.root, self.store.root, settings['cpu_threads'])
                        with self._lock:
                            stale = not self._publish_worker(generation=generation)
                            if not stale:
                                process = self._process = candidate
                                self._process_threads = settings['cpu_threads']
                        if stale:
                            self._reap_process(candidate)
                            raise Cancelled('cancelled')
                    if not self._publish_worker(generation=generation):
                        raise Cancelled('cancelled')
                    # The helper is already registered, so Stop can kill it while a pipe
                    # write/flush blocks. No transport operation holds the shared lock.
                    process.send({'text':text,'language':language,'speaker':settings['speaker'],'volume':settings['volume'],
                                  'robot':settings.get('robot_effect', 0)})
                    while True:
                        if generation != self._generation or self._closed: raise Cancelled('cancelled')
                        if time.monotonic() > deadline: raise VoiceModelError('synthesis_timeout')
                        try: event = process.receive(.1)
                        except queue.Empty: continue
                        state = event.get('state')
                        update = state if state in ('loading','synthesizing','playing') else None
                        if not self._publish_worker(generation=generation, voice_state=update):
                            raise Cancelled('cancelled')
                        if state == 'playing':
                            self._mark(uid, 'playing')
                        if state == 'done': break
                        if state in ('synthesis_failed','process_exited'): raise VoiceModelError('synthesis_failed')
                    self._publish_worker(generation=generation, voice_state='idle')
                    self._terminal(uid, 'completed')
                    idle_deadline = time.monotonic() + settings['idle_unload_seconds']
                except Exception as error:
                    self._drop_process()
                    self._reap_retired()
                    state = 'idle' if isinstance(error, Cancelled) else str(error) if isinstance(error, VoiceModelError) else 'synthesis_failed'
                    self._terminal(uid, 'cancelled' if isinstance(error, Cancelled) else 'failed')
                    self._publish_worker(generation=generation, voice_state=state)
        finally:
            self._drop_process()
            self._reap_retired()
            # submit() can race with the last queue check; restart under the same lock.
            with self._lock:
                self._thread = None
                if not self._closed and not self._queue.empty() and not self._state['running']:
                    self._thread = threading.Thread(target=self._run, daemon=True, name='NEO-Response-Voice')
                    self._thread.start()
