"""One-click atlas replay with typed fixtures and dialogue contracts.

The production Agent/dialogue/registry are used in a separate test profile.
All resolved atlas phrases are submitted. Unknown slots are reported, not guessed. Ordinary tool
side effects and external I/O are blocked, not reported as successful execution.
The worker is separate so a stuck phrase cannot freeze the report window.
"""
from __future__ import annotations

import argparse
from collections import Counter
import contextlib
from dataclasses import dataclass, asdict
from datetime import datetime
import gzip
import faulthandler
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
import zipfile

# Direct CLI execution starts in tools/, before setup_imports runs.
INTERNAL = Path(__file__).resolve().parents[1]
if str(INTERNAL) not in sys.path:
    sys.path.insert(0, str(INTERNAL))
from jarvis.testing.release_version import release_number

LANGUAGES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
NAMES = ('Русский', 'English', 'Deutsch', 'Français', 'Español', 'Italiano', 'Português')
PROTOCOL = '@@NEO_ATLAS_ALL@@'
VERSION = f'ATLAS-ALL-{release_number(INTERNAL)}.1'
FAILURES = frozenset({'NO_ROUTE', 'ROUTE_MISMATCH', 'INVALID_ARGUMENTS', 'UNEXPECTED_ACTION',
                      'ERROR', 'TIMEOUT', 'EMPTY_RESPONSE', 'LANGUAGE_ERROR', 'ARGUMENT_MISMATCH', 'REACTION_MISMATCH', 'OUTCOME_MISMATCH', 'NEEDS_FIXTURE', 'DIALOGUE_MISMATCH'})


def stamp():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def write_json(path: Path, obj):
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
    temp.replace(path)


def setup_imports(root: Path):
    internal = root / '_internal'
    if not (internal / 'jarvis/testing/atlas_contracts149.py').is_file():
        raise RuntimeError('NEO atlas contracts not found. Run NEO_TEST.cmd from the complete assistant folder.')
    sys.path.insert(0, str(internal))
    return internal


@dataclass(frozen=True)
class Phrase:
    locale: str
    phrase: str
    expected_tool: str = ''
    expected_action: str = ''
    source: str = 'action_phrases'
    category: str = ''
    expected_args: dict | None = None
    expected_kind: str = ''
    scenario_id: str = ''
    keep_context: bool = False
    mock_result: dict | None = None
    outcome_fixture: dict | None = None
    expected_social_topic: str = ''
    expected_responses: list[str] | None = None
    folder_windows_fixture: list[dict] | None = None


def dialogue_followup_cases(internal: Path, code: str):
    """Each reviewed contextual reply gets its own opening/followup scenario.

    Context and reply membership are checked against declared pack data; this
    verifies the reviewed contract, not general conversational intelligence.
    """
    for version in (155, 156, 160):
        path = internal / 'config' / f'social_dialogue{version}.json'
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        groups = {row['family']: row for row in data['groups']}
        for index, (text, row) in enumerate(data.get('followups', {}).get(code, {}).items()):
            prerequisite = row['requires'][0]
            opening = groups[prerequisite]['phrases'][code][0]
            answers = row.get('answers') or [row['answer']]
            scenario = f'social_context{version}_{code}_{index}'
            yield Phrase(code, opening, source=f'context{version}', expected_kind='response',
                         scenario_id=scenario, expected_social_topic=prerequisite)
            yield Phrase(code, text, source=f'context{version}', expected_kind='response',
                         scenario_id=scenario, keep_context=True,
                         expected_social_topic=row['topic'], expected_responses=answers)


def load_cases(internal: Path, code: str):
    """Keep all main-atlas rows, supplement unique native/canonical/social rows.

    Do not merge identical text belonging to different expected actions: such
    ambiguity is useful evidence, not a reason to repaint a result as passing.
    """
    for source in sorted((internal/'config').glob('regression*.json')):
        rows=json.loads(source.read_text(encoding='utf-8'))
        if isinstance(rows,dict): rows=rows.get('cases',[])
        for row in rows:
            if row.get('locale')==code: yield Phrase(**row)
    yield from dialogue_followup_cases(internal, code)
    from jarvis.testing.typo_cases161 import cases as spelling_cases161
    for row in spelling_cases161(internal, code):
        yield Phrase(**row)
    from jarvis.testing.atlas_scenarios149 import cases, outcome_cases
    for row in cases(code):
        yield Phrase(**row)
    for row in outcome_cases(code):
        yield Phrase(**row)
    pack = json.loads((internal / 'jarvis/locales/commands' / (code + '.json')).read_text(encoding='utf-8'))
    if pack.get('language') != code:
        raise ValueError('Atlas language mismatch: ' + code)
    seen = set()
    for source in ('action_phrases', 'native_variants', 'canonical_phrases'):
        for key, values in pack.get(source, {}).items():
            tool, sep, action = key.partition('.')
            if not sep:
                raise ValueError('Invalid atlas action: ' + key)
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                raise ValueError('Invalid phrase array: ' + key)
            for phrase in values:
                if not isinstance(phrase, str) or not phrase.strip():
                    raise ValueError('Empty/non-text atlas phrase: ' + key)
                signature = (tool, action, phrase)
                if source != 'action_phrases' and signature in seen:
                    continue
                seen.add(signature)
                yield Phrase(code, phrase, tool, action, source)
    from jarvis.runtime.social_atlas import catalog
    for phrase, (category, _family) in catalog(code).items():
        yield Phrase(code, phrase, source='social_atlas', category=category)


def check_phrase(session, case):
    from jarvis.testing.atlas_contracts149 import render, evaluate, pending_call
    from jarvis.language import language_code
    from jarvis.tools.registry import ToolExecution
    from jarvis.runtime.localized_text import render_action_result
    code = case['locale']
    if language_code(session.config) != code:
        raise RuntimeError('Language was not applied before phrase: ' + code)
    # Keep context ONLY inside an explicit same-ID scenario, never between
    # unrelated catalogue rows or after a worker restart/failed predecessor.
    sid = case.get('scenario_id', '')
    continuation = bool(case.get('keep_context'))
    if continuation and getattr(session, '_scenario_id', '') != sid:
        return dict(case, input=case['phrase'], status='ERROR', response='Scenario context was lost', submitted=False, reply_received=False, duration_ms=0)
    if not continuation:
        session.reset_case(code)
    else:
        session.calls.clear(); session.executions.clear(); session._permitted.clear()
    session._scenario_id = sid
    begin = time.perf_counter()
    text, used, expected, unresolved = render(case, session.workspace)
    row = dict(case, input=text, template=case['phrase'], fixture_values=used,
               expected_call=expected, unresolved_slots=unresolved,
               execution='BLOCKED_TEST_MODE', settings_language=language_code(session.config), submitted=False)
    if unresolved:
        return dict(row, status='NEEDS_FIXTURE', response='No typed fixture: '+', '.join(unresolved), duration_ms=0, reply_received=False)
    fixture = case.get('outcome_fixture')
    if fixture:
        rendered = render_action_result('yandex_music', {'action':fixture['action']}, fixture['success'], '', fixture['data'], code)
        # Independent expected semantic marker per language, not merely nonempty.
        from jarvis.testing.outcome_oracle149 import check_outcome
        okay = check_outcome(fixture['kind'], rendered, code)
        return dict(row, response=rendered, status='OUTCOME_PASS' if okay else 'OUTCOME_MISMATCH',
                    execution='FORMATTER_FIXTURE_ONLY', duration_ms=round((time.perf_counter()-begin)*1000,3), reply_received=False)
    prepared = time.perf_counter()
    restore = None
    folder_restore = None
    folder_fixture = case.get('folder_windows_fixture')
    if folder_fixture is not None:
        if ((expected['tool'], expected['arguments'].get('action')) != ('desktop', 'close_folder')
                or not isinstance(folder_fixture, list) or len(folder_fixture) > 40
                or any(not isinstance(item, dict) or set(item) != {'hwnd', 'pid', 'created', 'title'}
                       or type(item['hwnd']) is not int or item['hwnd'] <= 0
                       or type(item['pid']) is not int or item['pid'] <= 0
                       or not isinstance(item['created'], (int, float))
                       or not isinstance(item['title'], str) for item in folder_fixture)):
            raise ValueError('Invalid test-owned Explorer fixture')
    mock = case.get('mock_result')
    if mock:
        if (expected['tool'],expected['arguments'].get('action')) not in {('timer','timer'),('timer','reschedule')}:
            raise ValueError('Only timer state simulation is authorized in this test')
        tool = session.agent.tools.get(expected['tool']); restore = tool.handler
        args = dict(expected['arguments']); args.update(case.get('expected_args') or {})
        session._permitted.append((expected['tool'],args))
        tool.handler = lambda _args: ToolExecution(mock['success'], mock['text'], dict(mock['data']))
        row['execution'] = 'SIMULATED_TIMER_RESULT_NO_LIVE_ACTION'
    if folder_fixture is not None:
        from jarvis.runtime import folder_context156
        folder_restore = folder_context156.snapshot
        folder_context156.snapshot = lambda: [dict(item) for item in folder_fixture]
        row['execution'] = 'SIMULATED_EXPLORER_SNAPSHOT_ACTION_BLOCKED'
    try:
        row['submitted'] = True
        turn = session.agent.handle(text)
        handled = time.perf_counter()
        actual = list(session.calls)
        if turn.pending:
            actual.append({'tool':turn.pending.tool_name,'arguments':turn.pending.arguments,
                           'risk':turn.pending.risk.label,'confirmation_required':True})
        observed, differences, checked = evaluate(case, turn, actual, session.executions,
                session.agent.conversations.get('desktop'), session.agent._pending_target_choice, expected)
        row['observed_pending_call'] = pending_call(session.agent.conversations.get('desktop'),
                                                    session.agent._pending_target_choice)
        reply = turn.text
        wanted_topic = case.get('expected_social_topic')
        if wanted_topic:
            from jarvis.runtime.social_atlas import norm
            conversation = getattr(session.agent, '_conversation_key', 'desktop')
            actual_topic = session.agent.social_atlas.context_topic(session=conversation, code=code)
            answers = case.get('expected_responses')
            response_matches = not answers or any(norm(answer) == norm(reply) for answer in answers)
            row.update(expected_social_topic=wanted_topic, observed_social_topic=actual_topic,
                       reviewed_response_checked=bool(answers), reviewed_response_matches=response_matches)
            if observed != 'DIALOGUE_RESPONSE' or actual_topic != wanted_topic or not response_matches:
                observed = 'DIALOGUE_MISMATCH'
            elif answers:
                observed = 'CONTEXT_RESPONSE_PASS'
        unmasked = reply
        for value in used.values(): unmasked = unmasked.replace(str(value), '')
        import re
        wrong_script = code != 'ru' and bool(re.search('[А-Яа-яЁё]', unmasked)) and not re.search('[А-Яа-яЁё]', text)
        row.update(response=reply,calls=actual,outcome=turn.outcome,routing_status=observed,status=observed,
                   argument_differences=differences,checked_arguments=checked,reply_received=True,
                   localization_warning=wrong_script,
                   timings_ms={'prepare':round((prepared-begin)*1000,3),'agent':round((handled-prepared)*1000,3),
                               'evaluation':round((time.perf_counter()-handled)*1000,3)})
    except Exception as exc:
        row.update(status='ERROR',routing_status='ERROR',response=f'{type(exc).__name__}: {exc}',
                   traceback=traceback.format_exc(),reply_received=False)
    finally:
        if folder_restore is not None:
            folder_context156.snapshot = folder_restore
        if restore is not None: tool.handler = restore
        session._permitted.clear()
    row['duration_ms'] = round((time.perf_counter()-begin)*1000,3)
    return row


def worker_main(root: Path, session_dir: Path):
    """A worker has no credentials and no access to the real application profile."""
    os.environ['LOCALAPPDATA'] = str(session_dir / 'os_local')
    os.environ['APPDATA'] = str(session_dir / 'os_roaming')
    os.environ['NEOJARVIS_CACHE_DIR'] = str(session_dir / 'cache')
    os.environ['NEO_TEST_MODE'] = '1'
    setup_imports(root)
    wire = sys.stdout
    def send(payload):
        wire.write(PROTOCOL + json.dumps(payload, ensure_ascii=False, default=str) + '\n')
        wire.flush()
    # Imports/legacy print() cannot corrupt the line protocol.
    with contextlib.redirect_stdout(sys.stderr):
        from jarvis.testing.isolation import external_io_blocked
        from jarvis.testing.atlas_session import AtlasSession
        from jarvis.language import language_code
        with external_io_blocked():
            session = AtlasSession(session_dir, locale='ru')
            from jarvis.testing.atlas_contracts149 import prepare_fixtures
            prepare_fixtures(session)
            logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
            send({'id': 0, 'kind': 'ready', 'version': VERSION, 'pid': os.getpid()})
            try:
                for line in sys.stdin:
                    request = json.loads(line)
                    ident = request['id']
                    try:
                        if request['op'] == 'language':
                            code = request['locale']
                            if code not in LANGUAGES:
                                raise ValueError('Unsupported locale')
                            session.reset_case(code)
                            session.config.save()
                            persisted = json.loads(session.config.paths.settings.read_text(encoding='utf-8'))
                            if persisted['app']['language'] != code or language_code(session.config) != code:
                                raise RuntimeError('Language settings verification failed')
                            send({'id': ident, 'kind': 'language', 'locale': code,
                                  'saved_language': persisted['app']['language'],
                                  'agent_language': language_code(session.config), 'time': stamp()})
                        elif request['op'] == 'phrase':
                            faulthandler.dump_traceback_later(25, file=sys.stderr)
                            try:
                                send({'id': ident, 'kind': 'result', 'row': check_phrase(session, request['case'])})
                            finally:
                                faulthandler.cancel_dump_traceback_later()
                        elif request['op'] == 'finish':
                            send({'id': ident, 'kind': 'finished'})
                            break
                        else:
                            raise ValueError('Unknown worker operation')
                    except Exception as exc:
                        send({'id': ident, 'kind': 'error', 'error': f'{type(exc).__name__}: {exc}',
                              'traceback': traceback.format_exc()})
            finally:
                session.finish()
    return 0


class Worker:
    def __init__(self, root: Path, output: Path, number: int):
        if os.name == 'nt':
            import psutil  # Installed NEO dependency; needed for redirector-child cleanup.
        self.events = queue.Queue()
        self.counter = 0
        self.closed = False
        self.fixture_root = output / 'private_workers' / f'session_{number:03d}' / 'owned_fixtures'
        self.stderr_path = output / f'worker_{number:03d}.log'
        self.err = self.stderr_path.open('w', encoding='utf-8', newline='\n')
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1', OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
        try:
            self.process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()),
                    '--root', str(root), '--output', str(output / 'private_workers' / f'session_{number:03d}'),
                    '--worker'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err,
                    encoding='utf-8', errors='replace', bufsize=1, env=env, creationflags=flags)
        except BaseException:
            self.err.close()
            raise
        self.reader = threading.Thread(target=self._read, name='AtlasPipe', daemon=True)
        self.reader.start()
        try:
            ready = self._wait(0, 60)
            if ready.get('kind') != 'ready':
                raise RuntimeError('Atlas worker did not become ready')
        except BaseException:
            self.close(force=True)
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                if line.startswith(PROTOCOL):
                    self.events.put(json.loads(line[len(PROTOCOL):]))
        except Exception as exc:
            self.events.put({'kind': 'pipe_error', 'error': str(exc)})
        finally:
            self.events.put({'kind': 'eof'})

    def _wait(self, ident, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'No answer within {timeout:g} seconds')
            try:
                reply = self.events.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f'No answer within {timeout:g} seconds') from None
            if reply.get('kind') in ('eof', 'pipe_error'):
                raise RuntimeError('Atlas worker stopped before answering; see worker log')
            if reply.get('id') != ident:
                raise RuntimeError('Mismatched response ID; stopped to avoid mixing replies')
            if reply.get('kind') == 'error':
                raise RuntimeError(reply.get('error', 'Worker error'))
            return reply

    def request(self, op, timeout=30, **payload):
        self.counter += 1
        self.process.stdin.write(json.dumps(dict(payload, id=self.counter, op=op), ensure_ascii=False) + '\n')
        self.process.stdin.flush()
        return self._wait(self.counter, timeout)

    def _terminate_owned_worker(self):
        # Windows venv python.exe can be a redirector with a child interpreter.
        # Kill only verified descendants of OUR live Popen handle, never by name.
        children = []
        if os.name == 'nt' and self.process.poll() is None:
            import psutil
            try:
                children = psutil.Process(self.process.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                pass
            for child in reversed(children):
                try:
                    child.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        if self.process.poll() is None:
            self.process.terminate()
        if children:
            _, alive = psutil.wait_procs(children, timeout=2)
            for child in alive:
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

    def close(self, force=False):
        if self.closed:
            return
        self.closed = True
        if not force and self.process.poll() is None:
            try:
                self.request('finish', timeout=5)
            except Exception:
                force = True
        if self.process.poll() is None:
            if force:
                self._terminate_owned_worker()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        if self.process.stdin:
            self.process.stdin.close()
        # Never block the controller by acquiring a stream lock held by a reader.
        if self.process.stdout and not self.reader.is_alive():
            self.process.stdout.close()
        self.err.close()


class Replay:
    def __init__(self, root, output, limit=0, timeout=30):
        self.root, self.output = root, output
        self.limit, self.timeout = limit, timeout
        self.stop = threading.Event()
        self.resume = threading.Event(); self.resume.set()
        self.delay = 0
        self.counts = Counter(); self.routing_counts = Counter(); self.by_locale = {}
        self.done = 0; self.total = 0; self.replies = 0; self.expected_replies = 0; self.started = time.time()
        self.error = ''; self.worker = None; self.worker_number = 0
        self.zip_path = None; self.exit_code = 2
        self.events = []
        self.manifest = {}
        self.output.mkdir(parents=True, exist_ok=True)
        if (self.output / 'manifest.json').exists() or (self.output / 'summary.json').exists():
            raise FileExistsError('Use a new report directory; previous results are never overwritten.')
        from jarvis.runtime.diagnostics import redact
        self.redact = redact

    def clean(self, value):
        if isinstance(value, dict):
            return {key: self.clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            return self.redact(value)
        return value

    def note(self, text):
        with (self.output / 'runner.log').open('a', encoding='utf-8', newline='\n') as log:
            log.write(stamp() + ' ' + self.redact(text) + '\n')

    def create_worker(self):
        self.worker_number += 1
        self.worker = Worker(self.root, self.output, self.worker_number)
        return self.worker

    def apply_language(self, code, show):
        reply = self.worker.request('language', locale=code)
        if reply.get('saved_language') != code or reply.get('agent_language') != code:
            raise RuntimeError('Language acknowledgement is incorrect')
        if not show({'kind': 'language', 'locale': code}):
            return False
        event = {key: value for key, value in reply.items() if key != 'id'}
        event['ui_language'] = code
        event['worker_number'] = self.worker_number
        self.events.append(event)
        with (self.output / 'language_switches.jsonl').open('a', encoding='utf-8', newline='\n') as file:
            file.write(json.dumps(event, ensure_ascii=False) + '\n')
        self.note('LANGUAGE_APPLIED=' + code)
        print('LANGUAGE_APPLIED=' + code, flush=True)
        return True

    def snapshot(self, final=False):
        return {'schema_version': 2, 'runner': VERSION, 'started': self.started, 'finished': time.time() if final else None,
                'duration_seconds': round(time.time() - self.started, 3), 'completed': self.done,
                'planned': self.total, 'all_cases_recorded': bool(final and self.done == self.total and not self.error),
                'agent_replies_received': self.replies, 'agent_replies_expected': self.expected_replies,
                'formatter_checks_recorded': self.counts.get('OUTCOME_PASS',0) + self.counts.get('OUTCOME_MISMATCH',0),
                'all_expected_agent_replies_received': bool(final and self.replies == self.expected_replies and self.done == self.total and not self.error),
                'all_phrases_processed': bool(final and self.done == self.total and not self.error and not self.counts.get('NEEDS_FIXTURE') and self.replies == self.expected_replies),
                'stopped': self.stop.is_set(), 'fatal_error': self.redact(self.error),
                'counts': dict(self.counts), 'routing_counts_including_templates': dict(self.routing_counts),
                'by_language': {k: dict(v) for k, v in self.by_locale.items()},
                'settings_switches': self.events, 'live_pc_actions': False,
                'scope': 'Actual Agent text input and registry proposals in an isolated profile; no ASR, live tools or account tests',
                'route_pass_does_not_verify_all_slot_values': True, 'arguments_pass_contract': 'All declared fixture/explicit expected values match; not unspecified slots or real execution', 'localization_warnings': getattr(self,'localization_warnings',0)}

    def run(self, show=lambda _event: True):
        """show returns only after the GUI consumed the input/result event."""
        journal = None
        try:
            internal = self.root / '_internal'
            inventory = {}; selected_inventory = {}; fingerprints = {}
            for code in LANGUAGES:
                inventory[code] = Counter(); selected_inventory[code] = Counter()
                for index, case in enumerate(load_cases(internal, code), 1):
                    inventory[code][case.source] += 1
                    if not self.limit or index <= self.limit:
                        selected_inventory[code][case.source] += 1
                p = internal / 'jarvis/locales/commands' / (code + '.json')
                fingerprints[str(p.relative_to(self.root))] = hashlib.sha256(p.read_bytes()).hexdigest()
            p = internal / 'jarvis/runtime/social_atlas.py'
            fingerprints[str(p.relative_to(self.root))] = hashlib.sha256(p.read_bytes()).hexdigest()
            for suffix in ('*.py','*.json','*.npy'):
                for path in internal.rglob(suffix):
                    if '__pycache__' not in path.parts:
                        fingerprints[str(path.relative_to(self.root))] = hashlib.sha256(path.read_bytes()).hexdigest()
            self.total = sum(min(sum(v.values()), self.limit) if self.limit else sum(v.values()) for v in inventory.values())
            self.expected_replies = self.total - sum(v.get('outcome_fixture',0) for v in selected_inventory.values())
            build = (internal / 'VERSION.txt').read_text(encoding='utf-8').strip()
            self.manifest = {'runner': VERSION, 'build': build, 'languages': list(LANGUAGES), 'planned': self.total,
                'inventory': {k: dict(v) for k, v in inventory.items()},
                'selected_inventory': {k:dict(v) for k,v in selected_inventory.items()},
                'agent_replies_expected': self.expected_replies, 'source_sha256': fingerprints,
                'limit_per_language': self.limit, 'phrase_timeout_seconds': self.timeout,
                'execution_mode': 'isolated-real-Agent-blocked-tools', 'user_settings_modified': False,
                'template_policy': 'Typed per-action fixtures; original template, input, values and expected call retained. Unknown slots are NEEDS_FIXTURE, never sent as literal braces.',
                'state_policy': 'Fresh state for catalogue rows; explicit scenario IDs preserve context. Timer outcomes explicitly mocked for ID continuity; no automatic risky confirmations.',
                'social_policy': 'DIALOGUE_RESPONSE means nonempty reply without tool proposal. CONTEXT_RESPONSE_PASS additionally verifies topic and membership in reviewed followup answers. Neither is a general semantic quality score.'}
            write_json(self.output / 'manifest.json', self.manifest)
            self.note('RUN_BEGIN planned=' + str(self.total))
            print('TOTAL_CHECKS=' + str(self.total), flush=True)
            print('AGENT_REQUESTS=' + str(self.expected_replies), flush=True)
            if not show({'kind': 'inventory', 'total': self.total}):
                self.stop.set()
            journal = gzip.open(self.output / 'commands.jsonl.gz', 'wt', encoding='utf-8', newline='\n')
            self.create_worker()
            failures_in_row = 0
            for code in LANGUAGES:
                if self.stop.is_set():
                    break
                if not self.apply_language(code, show):
                    break
                for local_index, case in enumerate(load_cases(internal, code), 1):
                    if self.limit and local_index > self.limit:
                        break
                    while not self.resume.wait(.1):
                        if self.stop.is_set():
                            break
                    if self.stop.is_set():
                        break
                    from jarvis.testing.atlas_contracts149 import render
                    displayed_input = render(asdict(case), self.worker.fixture_root)[0]
                    if not show({'kind': 'input', 'input': displayed_input, 'locale': code,
                                 'number': self.done + 1, 'total': self.total}):
                        break
                    began = time.perf_counter()
                    try:
                        row = self.worker.request('phrase', timeout=self.timeout, case=asdict(case))['row']
                        failures_in_row = 0
                        self.replies += int(row.get('reply_received', False))
                    except Exception as exc:
                        failures_in_row += 1
                        row = dict(asdict(case), status='TIMEOUT' if isinstance(exc, TimeoutError) else 'ERROR',
                                   response=f'{type(exc).__name__}: {exc}', input=case.phrase,
                                   submitted='unknown', reply_received=False, duration_ms=round((time.perf_counter() - began) * 1000, 3),
                                   execution='BLOCKED_TEST_MODE')
                        self.note(row['response'])
                        self.worker.close(force=True)
                        self.worker = None
                    self.done += 1
                    row.update(sequence=self.done, time=stamp(), locale_index=local_index)
                    row = self.clean(row)
                    journal.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
                    journal.flush()
                    self.counts[row['status']] += 1
                    self.localization_warnings = getattr(self,'localization_warnings',0) + int(row.get('localization_warning',False))
                    self.routing_counts[row.get('routing_status', row['status'])] += 1
                    self.by_locale.setdefault(code, Counter())[row['status']] += 1
                    if not show({'kind': 'result', 'row': row, 'number': self.done, 'total': self.total}):
                        self.stop.set()
                    if self.done % 250 == 0:
                        write_json(self.output / 'progress.json', self.snapshot())
                        print(f'PROGRESS={self.done}/{self.total} LANGUAGE={code}', flush=True)
                    if failures_in_row >= 3:
                        raise RuntimeError('Three consecutive worker errors; incomplete logs preserved.')
                    if self.worker is None and not self.stop.is_set():
                        self.create_worker()
                        if not self.apply_language(code, show):
                            self.stop.set()
                    if self.stop.wait(self.delay / 1000):
                        break
                self.note('LANGUAGE_FINISHED=' + code + ' ' + json.dumps(dict(self.by_locale.get(code, {}))))
            for relative, expected in fingerprints.items():
                if hashlib.sha256((self.root / relative).read_bytes()).hexdigest() != expected:
                    raise RuntimeError('Atlas changed during the run: ' + relative)
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                self.stop.set()
            else:
                self.error = f'{type(exc).__name__}: {exc}'
                self.note(traceback.format_exc())
        finally:
            if self.worker:
                try:
                    self.worker.close()
                except Exception:
                    self.note(traceback.format_exc())
            if journal:
                journal.close()
            self.finish()
        show({'kind': 'finished', 'summary': self.snapshot(True), 'zip': str(self.zip_path or ''), 'exit_code': self.exit_code})
        return self.exit_code

    def finish(self):
        summary = self.snapshot(True)
        self.exit_code = 2 if self.error or self.stop.is_set() or self.done != self.total else int(any(self.counts[x] for x in FAILURES))
        summary['exit_code'] = self.exit_code
        summary['status'] = 'INCOMPLETE' if self.exit_code == 2 else ('FINISHED_WITH_FINDINGS' if self.exit_code else 'FINISHED')
        write_json(self.output / 'summary.json', summary)
        lines = ['NEO — ПОСЛЕДОВАТЕЛЬНЫЙ ПРОГОН ФРАЗАТЛАСА', '',
                 'Обработано: %s / %s' % (self.done, self.total), 'Итог: ' + summary['status'],
                 'Время: %.1f секунд' % summary['duration_seconds'], '',
                 'Языки: RU -> EN -> DE -> FR -> ES -> IT -> PT',
                 'Язык app.language сохранялся в настройках тестового экземпляра перед каждым блоком.',
                 'Основной профиль пользователя не изменялся.', '',
                 'Реальные действия с ПК и аккаунтами заблокированы.',
                 'ROUTE_PASS: совпали инструмент и действие; НЕ доказаны все значения параметров или выполнение.',
                 'DIALOGUE_RESPONSE: получен текст без действия; естественность/смысл требуют анализа.',
                 'CONTEXT_RESPONSE_PASS: сохранена тема и ответ соответствует объявленным вариантам продолжения.',
                 'NEEDS_INPUT / CONFIRMATION_REQUIRED: уточнение/подтверждение, не выполненная команда.',
                 'ARGUMENTS_PASS: совпали действие и все объявленные тестом параметры; исполнение не проверено.',
                 'CLARIFICATION_PASS: задан вопрос о параметре именно ожидаемого действия.',
                 'NEGATIVE_PASS: отрицательная/неадресованная команда не предложила действие.',
                 'OUTCOME_PASS: отдельная проверка формулировки по моделируемому исходу, не запрос к Agent.',
                 'NEEDS_FIXTURE: неизвестный параметр не был отправлен; нужны конкретные тестовые данные.',
                 'ARGUMENT_MISMATCH / REACTION_MISMATCH / OUTCOME_MISMATCH: не совпал объявленный контракт.',
                 'Предупреждения языка (эвристика кириллицы): '+str(summary['localization_warnings']),
                 'Ответы Agent: '+str(self.replies)+' / '+str(self.expected_replies),
                 'NO_ROUTE / ROUTE_MISMATCH / INVALID_ARGUMENTS / ERROR / TIMEOUT: требуют разбора.',
                 'Поведение микрофона, окон Windows и музыкальных API этим прогоном не проверяется.', '',
                 *[f'{key}: {value}' for key, value in sorted(self.counts.items())], '', 'ПО ЯЗЫКАМ:']
        for code in LANGUAGES:
            lines.append(code.upper() + ': ' + json.dumps(dict(self.by_locale.get(code, {})), ensure_ascii=False))
        if self.error:
            lines.extend(['', 'Ошибка прогона: ' + self.redact(self.error)])
        (self.output / 'SUMMARY.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8-sig', newline='\n')
        table = ''.join('<tr><th>' + html.escape(code.upper()) + '</th><td>' +
                         html.escape(', '.join(f'{k}: {v}' for k, v in self.by_locale.get(code, {}).items())) + '</td></tr>'
                         for code in LANGUAGES)
        (self.output / 'RESULTS.html').write_text('<!doctype html><meta charset="utf-8"><title>NEO Atlas results</title>'
            '<style>body{font:16px system-ui;max-width:1100px;margin:35px auto;padding:0 20px}pre{white-space:pre-wrap}'
            'td,th{padding:12px;text-align:left;border-bottom:1px solid #ccc}</style>'
            '<h1>NEO — результаты фразатласа</h1><p>' + html.escape(summary['status']) + '</p>'
            '<table>' + table + '</table><pre>' + html.escape('\n'.join(lines)) + '</pre>', encoding='utf-8')
        # Only generated test evidence; no recursive export of profiles/DB/settings.
        members = ['manifest.json', 'summary.json', 'SUMMARY.txt', 'RESULTS.html', 'commands.jsonl.gz',
                   'language_switches.jsonl', 'runner.log']
        for file in sorted(self.output.glob('worker_*.log')):
            with file.open('rb') as stream:
                size = file.stat().st_size
                stream.seek(max(0, size - 16 * 1024 * 1024))
                text = stream.read().decode('utf-8', errors='replace')
            if size > 16 * 1024 * 1024:
                text = '[Older worker stderr omitted; per-phrase results are in commands.jsonl.gz.]\n' + text
            file.write_text(self.redact(text), encoding='utf-8', newline='\n')
            members.append(file.name)
        try:
            destination = self.output.parent / (self.output.name + '_RESULTS.zip')
            temporary = destination.with_suffix('.zip.tmp')
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for name in members:
                    file = self.output / name
                    if file.is_file():
                        archive.write(file, name)
            with zipfile.ZipFile(temporary) as archive:
                if archive.testzip() is not None:
                    raise RuntimeError('Report ZIP integrity check failed')
            temporary.replace(destination)
            self.zip_path = destination
            print('\nATLAS_STATUS=' + summary['status'], flush=True)
            print(f'PROCESSED={self.done}/{self.total}', flush=True)
            print('COUNTS=' + json.dumps(dict(self.counts), ensure_ascii=False), flush=True)
            print('SEND_THIS_ZIP=' + str(destination), flush=True)
        except Exception as exc:
            self.exit_code = 2
            print('ZIP_ERROR=' + repr(exc), flush=True)
            print('REPORT_DIRECTORY=' + str(self.output), flush=True)
        print('ATLAS_EXIT_CODE=' + str(self.exit_code), flush=True)


def gui_main(replay, auto_close=False):
    from PySide6.QtCore import QObject, QTimer, Signal, Slot, Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                                   QPushButton, QProgressBar, QComboBox, QGroupBox, QSpinBox)
    from jarvis.ui.chat_view import ChatView
    from jarvis.ui.robot_widget import RobotWidget

    class Signals(QObject):
        event = Signal(object)

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.done = False
            self.ack = threading.Event()
            self.signals = Signals(self)
            self.signals.event.connect(self.consume, Qt.QueuedConnection)
            self.setWindowTitle('NEO — весь фразатлас, 7 языков')
            self.resize(940, 820)
            icon = replay.root / '_internal/assets/neo_jarvis.ico'
            if icon.exists(): self.setWindowIcon(QIcon(str(icon)))
            layout = QVBoxLayout(self)
            notice = QLabel('Фразы проходят настоящий обработчик NEO в отдельном тестовом профиле.\n'
                'Реальные действия заблокированы. Таймерные результаты в сценариях моделируются; это не тест микрофона.\n'
                'Прогон начинается автоматически. Основные настройки NEO не меняются.')
            notice.setWordWrap(True); layout.addWidget(notice)
            self.robot = RobotWidget(parent=self); self.robot.setFixedHeight(105); layout.addWidget(self.robot)
            group = QGroupBox('Настройки тестового экземпляра — автоматическое переключение')
            settings = QHBoxLayout(group)
            self.language = QComboBox()
            for code, name in zip(LANGUAGES, NAMES): self.language.addItem(name, code)
            self.language.setEnabled(False); settings.addWidget(self.language)
            self.setting_status = QLabel('Ожидаю подтверждения app.language'); settings.addWidget(self.setting_status, 1)
            layout.addWidget(group)
            self.progress = QProgressBar(); self.progress.setRange(0, 1); layout.addWidget(self.progress)
            self.status = QLabel('Подготовка фразатласа…'); layout.addWidget(self.status)
            bar = QHBoxLayout(); layout.addLayout(bar)
            self.pause = QPushButton('Пауза'); self.pause.clicked.connect(self.pause_run); bar.addWidget(self.pause)
            self.stop_button = QPushButton('Остановить и сохранить логи'); self.stop_button.clicked.connect(self.stop_run); bar.addWidget(self.stop_button)
            bar.addWidget(QLabel('Пауза между фразами, мс:'))
            self.delay = QSpinBox(); self.delay.setRange(0, 2000); self.delay.setValue(0)
            self.delay.valueChanged.connect(lambda value: setattr(replay, 'delay', value)); bar.addWidget(self.delay)
            self.chat = ChatView(); layout.addWidget(self.chat, 1)
            self.path = QLabel(str(replay.output)); self.path.setWordWrap(True)
            self.path.setTextInteractionFlags(Qt.TextSelectableByMouse); layout.addWidget(self.path)
            self.open_result = QPushButton('Открыть итоговый отчёт'); self.open_result.setEnabled(False)
            self.open_result.clicked.connect(self.open_report); layout.addWidget(self.open_result)
            QTimer.singleShot(150, self.start)

        def start(self):
            self.thread = threading.Thread(target=replay.run, args=(self.show_event,), name='NEO-Atlas-Replay', daemon=False)
            self.thread.start()

        def show_event(self, event):
            self.ack.clear()
            self.signals.event.emit(event)
            while not self.ack.wait(.1):
                if replay.stop.is_set() and event['kind'] != 'finished':
                    return False
            return not replay.stop.is_set()

        @Slot(object)
        def consume(self, event):
            try:
                kind = event['kind']
                if kind == 'language':
                    code = event['locale']
                    self.language.setCurrentIndex(self.language.findData(code))
                    self.chat.set_language(code)
                    self.setting_status.setText(f'app.language = {code} — сохранено и проверено')
                elif kind == 'inventory':
                    self.progress.setRange(0, event['total'])
                elif kind == 'input':
                    if len(self.chat.items) > 40: self.chat.clear_messages()
                    self.chat.add_user(event['input'])
                    self.robot.set_state('thinking')
                    self.status.setText(f"{event['number']} / {event['total']} · {event['locale'].upper()} · ожидание ответа")
                elif kind == 'result':
                    row = event['row']
                    self.chat.add_assistant(f"[{row['status']}] {row.get('response', '')}")
                    self.robot.set_state('idle')
                    self.progress.setValue(event['number'])
                    self.status.setText(f"{event['number']} / {event['total']} · {row['locale'].upper()} · {row['status']}")
                elif kind == 'finished':
                    self.done = True
                    self.pause.setEnabled(False); self.stop_button.setEnabled(False); self.delay.setEnabled(False)
                    self.robot.set_state('idle')
                    summary = event['summary']
                    self.status.setText(f"Завершено: {summary['completed']} / {summary['planned']}. Результаты сохранены.")
                    self.path.setText('SEND_THIS_ZIP=' + event['zip'] if event['zip'] else 'REPORT_DIRECTORY=' + str(replay.output))
                    self.chat.add_assistant('Итоги: ' + ', '.join(f'{k}: {v}' for k, v in summary['counts'].items()))
                    self.open_result.setEnabled(True)
                    if auto_close: QTimer.singleShot(50, self.close)
            except Exception as exc:
                replay.error = 'GUI: ' + repr(exc)
                replay.note(traceback.format_exc())
                replay.stop.set()
            finally:
                self.ack.set()

        def pause_run(self):
            if replay.resume.is_set():
                replay.resume.clear(); self.pause.setText('Продолжить')
            else:
                replay.resume.set(); self.pause.setText('Пауза')

        def stop_run(self):
            replay.stop.set(); replay.resume.set(); self.ack.set()
            self.stop_button.setEnabled(False)
            self.status.setText('Завершаю текущий запрос и сохраняю отчёт…')

        def open_report(self):
            if os.name == 'nt': os.startfile(str(replay.output / 'RESULTS.html'))

        def closeEvent(self, event):
            if not self.done:
                self.stop_run(); event.ignore()
            else:
                event.accept()

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('NEO Atlas Replay')
    app.setOrganizationName('NeoJarvisTests')
    window = Window(); window.show()
    # Let Ctrl+C request a clean partial report; never kill normal NEO.
    signal.signal(signal.SIGINT, lambda *_: window.stop_run())
    pulse = QTimer(window); pulse.setInterval(200); pulse.timeout.connect(lambda: None); pulse.start()
    app.exec()
    if hasattr(window, 'thread'): window.thread.join(timeout=10)
    return replay.exit_code


def main(argv=None):
    global LANGUAGES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--languages', nargs='+', choices=LANGUAGES, default=list(LANGUAGES), help='Diagnostic subset; default is all seven languages')
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--auto-close', action='store_true')
    parser.add_argument('--limit-per-language', type=int, default=0)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args(argv)
    if args.limit_per_language < 0 or args.timeout <= 0: parser.error('Invalid limit/timeout')
    sys.dont_write_bytecode = True
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, 'reconfigure'): stream.reconfigure(encoding='utf-8', errors='replace')
    if args.worker: return worker_main(args.root.resolve(), args.output.resolve())
    if len(args.languages) != len(set(args.languages)): parser.error("Duplicate languages")
    if tuple(args.languages) != LANGUAGES and not args.headless: parser.error("Language subsets require --headless")
    LANGUAGES = tuple(args.languages)
    setup_imports(args.root.resolve())
    # Parent UI and worker never resolve application state to the user's profile.
    os.environ['LOCALAPPDATA'] = str(args.output / 'ui_private/local')
    os.environ['APPDATA'] = str(args.output / 'ui_private/roaming')
    os.environ['NEOJARVIS_CACHE_DIR'] = str(args.output / 'ui_private/cache')
    os.environ['NEO_TEST_MODE'] = '1'
    replay = Replay(args.root.resolve(), args.output.resolve(), args.limit_per_language, args.timeout)
    if args.headless:
        signal.signal(signal.SIGINT, lambda *_: replay.stop.set())
        return replay.run()
    try:
        return gui_main(replay, args.auto_close)
    except Exception as exc:
        replay.error = str(exc)
        replay.note(traceback.format_exc()); replay.finish()
        print('Не удалось открыть тестовое окно. Подробности в runner.log.', flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
