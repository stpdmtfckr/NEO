"""Sequential real-Agent QA in a disposable profile, with fail-closed execution.

The full atlas is a ROUTING test, not a claim of 120,000 real desktop operations.
Only a separately enumerated, owned fixture set may execute. No account tokens
are loaded. The runner never approves a destructive confirmation. Intended for
an isolated QA process: a UI or CLI drives this same session synchronously.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
import copy
import gzip
import json
import logging
from pathlib import Path
import re
import time
import threading
from typing import Any, Callable, Iterator

from jarvis.agent import Agent
from jarvis.config import AppPaths, ConfigManager
from jarvis.runtime.cancellation import cancellation_manager
from jarvis.runtime.conversation import ConversationStore
from jarvis.runtime.natural_commands import LANGUAGES
from jarvis.routing.intent_core import assistive_groups

ROOT = Path(__file__).resolve().parents[2]


class AtlasConfig(ConfigManager):
    """Never migrate, read, save, or apply autostart to the user's real profile."""
    def __init__(self, root: Path, code: str = 'ru'):
        self.default_settings_path = ROOT / 'config/default_settings.json'
        self._defaults_cache = json.loads(self.default_settings_path.read_text(encoding='utf-8'))
        self.data = copy.deepcopy(self._defaults_cache)
        self._settings_signature = None
        self._active_wake_aliases_cache = None
        self._bootstrap_root = root.resolve()
        self.paths = AppPaths(self._bootstrap_root, root/'settings.json', root/'assistant.db',
                              root/'logs', root/'screenshots', root/'backups', root/'cache')
        for folder in (root, self.paths.logs, self.paths.screenshots, self.paths.backups, self.paths.cache):
            folder.mkdir(parents=True, exist_ok=True)
        for group in ('plugins','gaming','activity','downloads','file_index','briefing','voice_unlock','ambient_learning'):
            self.data.setdefault(group, {})['enabled'] = False
        for settings in self.data.get('integrations', {}).values():
            if isinstance(settings, dict):
                settings['enabled'] = False
        self.data['wake_word']['enabled'] = False
        self.data['ollama'].update(requests_enabled=False, auto_start=False, auto_install=False, preload_model=False)
        self.data.setdefault('screen', {})['enabled'] = False
        self.data['app']['language'] = code
        self.data['user_name_enabled'] = False
        self.data.setdefault('ui', {})['start_with_windows'] = False
        self.data.setdefault('history', {})['enabled'] = False
        self.save()

    def save(self, data: dict | None = None) -> None:
        if data is not None:
            self.data = data
        # NO ConfigManager.save(): that method also applies OS-side preferences.
        self.paths.settings.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')

    def reload_if_changed(self) -> bool:
        return False


class AuditAgent(Agent):
    """Same command/dialogue/gateway code; no implicit observation of the PC."""
    def _refresh_foreground_context(self) -> dict[str, Any]:
        self._last_foreground_context = {}
        return {}

    def _needs_screen_aware_preflight(self, text: str) -> bool:
        return False

    def _capture_action_screen(self, text: str) -> None:
        return None

    def _active_game_id(self) -> str:
        return ''


@dataclass(frozen=True)
class Case:
    locale: str
    phrase: str
    expected_tool: str = ''
    expected_action: str = ''
    group: str = 'atlas'
    negative: bool = False


def atlas_cases(locales: list[str], selection: str = 'full') -> Iterator[Case]:
    for code in locales:
        if code not in LANGUAGES:
            raise ValueError('Unsupported locale: ' + code)
        if selection == 'groups':
            for group in assistive_groups():
                for phrase in group['phrases'][code]:
                    yield Case(code, phrase, group['tool'], group['action'], 'meaning_group')
        else:
            pack = json.loads((ROOT/'jarvis/locales/commands'/f'{code}.json').read_text(encoding='utf-8'))
            key = 'native_variants' if selection == 'native' else 'action_phrases'
            for action, phrases in pack[key].items():
                tool, _, operation = action.partition('.')
                for phrase in phrases:
                    yield Case(code, phrase, tool, operation)


def render_fixture_phrase(phrase: str, workspace: Path) -> tuple[str, list[str]]:
    """Resolve known public example slots; never guess unresolved slot semantics."""
    values = {'source':str(workspace/'source.txt'), 'destination':str(workspace/'destination'),
              'path':str(workspace/'source.txt'), 'folder':str(workspace),
              'seconds':'900', 'minutes':'15', 'title':'NEO QA', 'content':'NEO QA text',
              'text':'NEO QA text', 'new_name':'renamed.txt', 'name':'NEO QA',
              'query':'NEO QA', 'volume':'25', 'percent':'25', 'count':'2',
              'to':'neo-qa@example.invalid', 'recipient':'neo-qa@example.invalid',
              'subject':'NEO QA', 'body':'NEO QA text', 'url':'https://example.invalid/',
              'server':'NEO QA', 'channel':'NEO QA', 'id':'NEO_QA_NOT_A_REAL_OBJECT'}
    unresolved = []
    def substitute(match):
        key = match.group(1)
        if key not in values:
            unresolved.append(key)
            return match.group(0)
        return values[key]
    return re.sub(r'\{([A-Za-z_][A-Za-z_0-9]*)\}', substitute, phrase), unresolved


class AtlasSession:
    def __init__(self, output: Path, *, locale: str = 'ru'):
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.workspace = self.output/'owned_fixtures'
        self.workspace.mkdir()
        self.config = AtlasConfig(self.output/'isolated_profile', locale)
        self.agent = AuditAgent(self.config)
        self.agent.tools.set_execution_guard(self._guard)
        self.agent.database.log_action = lambda *_args, **_kwargs: None
        self.calls: list[dict[str, Any]] = []
        self.executions: list[Any] = []
        self._permitted: list[tuple[str, dict]] = []
        self.stop = threading.Event()
        self.counts: Counter[str] = Counter()
        self.by_locale: dict[str, Counter] = {}
        self.started = time.time()
        self._closed = False
        self._journal = gzip.open(self.output/'commands.jsonl.gz', 'wt', encoding='utf-8')
        self._original_execute = self.agent.tools.execute
        def recorded_execute(name, arguments):
            result = self._original_execute(name, arguments)
            self.executions.append(result)
            return result
        self.agent.tools.execute = recorded_execute
        self.manifest = {'schema_version':1, 'mode':'isolated-real-agent',
            'real_desktop_actions':False, 'network_integrations':False,
            'route_pass_contract':'Expected tool and action only; not full parameter or real-world success verification',
            'profile':str(self.config.paths.root), 'fixtures':str(self.workspace),
            'unavailable_checks':['microphone and wake latency','real Windows window interactions',
                                  'authenticated Yandex/Spotify/Discord/Telegram actions',
                                  'restoring arbitrary user applications/media']}
        self._write('manifest.json', self.manifest)

    def _write(self, name: str, data: Any) -> None:
        temporary = self.output/(name+'.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.output/name)

    def _guard(self, name, args, risk):
        self.calls.append({'tool':name, 'arguments':dict(args), 'risk':risk.label})
        if (name, args) in self._permitted:
            # One-shot exact argument permit: an unexpected second call is denied.
            self._permitted.remove((name, args))
            return None
        return {'ru':'Проверка: команда распознана и проверена; реальное действие заблокировано.',
                'en':'Test: the command passed routing and validation; real execution was blocked.',
                'de':'Test: Befehl erkannt und geprüft; echte Ausführung blockiert.',
                'fr':'Test : commande reconnue et validée ; exécution réelle bloquée.',
                'es':'Prueba: orden reconocida y validada; ejecución real bloqueada.',
                'it':'Test: comando riconosciuto e verificato; esecuzione reale bloccata.',
                'pt':'Teste: comando reconhecido e validado; execução real bloqueada.'}[self.config.get('app.language')]

    def reset_case(self, code: str) -> None:
        self.config.data['app']['language'] = code
        self.agent.reset()
        self.agent.conversations = ConversationStore()
        self.agent.social_atlas.clear_context()
        self.calls.clear()
        self.executions.clear()
        self._permitted.clear()
        cancellation_manager().reset()

    def record(self, row: dict) -> dict:
        row.setdefault('sequence', sum(self.counts.values())+1)
        row.setdefault('time', datetime.now().astimezone().isoformat())
        self._journal.write(json.dumps(row, ensure_ascii=False, default=str)+'\n')
        self._journal.flush()
        self.counts[row['status']] += 1
        self.by_locale.setdefault(row.get('locale','fixture'), Counter())[row['status']] += 1
        return row

    def check(self, case: Case) -> dict:
        self.reset_case(case.locale)
        phrase, missing = render_fixture_phrase(case.phrase, self.workspace)
        row = {**asdict(case), 'input':phrase, 'execution':'blocked', 'unresolved_slots':missing}
        if missing:
            return self.record({**row, 'status':'NEEDS_FIXTURE', 'response':'Unresolved template slots: '+', '.join(missing)})
        started = time.perf_counter()
        try:
            turn = self.agent.handle(phrase)
            actual = list(self.calls)
            if turn.pending:
                actual.append({'tool':turn.pending.tool_name,'arguments':turn.pending.arguments,
                               'risk':turn.pending.risk.label,'confirmation_required':True})
            row.update(response=turn.text, calls=actual, outcome=turn.outcome,
                       duration_ms=round((time.perf_counter()-started)*1000,3))
            matching = any(call['tool'] == case.expected_tool and
                           str(call['arguments'].get('action','')) == case.expected_action for call in actual)
            # All proposed operations must agree: a matching first call is not
            # enough to hide an extra unintended action in a compound plan.
            unexpected = any(call['tool'] != case.expected_tool or
                             str(call['arguments'].get('action','')) != case.expected_action for call in actual)
            state = self.agent.conversations.get('desktop')
            if case.negative:
                row['status'] = 'NEGATIVE_PASS' if not actual else 'UNEXPECTED_ACTION'
            elif matching and not unexpected:
                row['status'] = 'CONFIRMATION_REQUIRED' if turn.pending else 'ROUTE_PASS'
            elif actual:
                row['status'] = 'ROUTE_MISMATCH'
            elif state.pending_intent or state.pending_shell or self.agent._pending_target_choice:
                row['status'] = 'NEEDS_INPUT'
            else:
                row['status'] = 'NO_ROUTE'
            if any(isinstance(result.data,dict) and result.data.get('invalid_tool_call') for result in self.executions):
                row['status'] = 'INVALID_ARGUMENTS'
        except Exception as exc:
            # A parser failure is a test result, not a successful skip.
            row.update(status='ERROR', response=f'{type(exc).__name__}: {exc}', duration_ms=round((time.perf_counter()-started)*1000,3))
            logging.getLogger(__name__).exception('Atlas case failed')
        return self.record(row)

    def live(self, code: str, phrase: str, permit: tuple[str,dict], verify: Callable[[Any], bool]) -> dict:
        self.reset_case(code)
        self._permitted = [permit]
        began = time.perf_counter()
        try:
            turn = self.agent.handle(phrase)
            success = bool(self.executions and self.executions[-1].success and verify(self.executions[-1]))
            return self.record({'locale':code,'input':phrase,'group':'owned_live_fixture',
                'status':'LIVE_PASS' if success else 'LIVE_FAIL','response':turn.text,
                'execution':'real-owned-fixture','calls':copy.deepcopy(self.calls),
                'duration_ms':round((time.perf_counter()-began)*1000,3)})
        finally:
            self._permitted.clear()

    def live_fixtures(self, locales: list[str]) -> Iterator[dict]:
        """Real chance and persistent timer actions, followed by verified cleanup.

        Filesystem operations get their own exact owned path permits below. No
        broad 'undo' is sent: it could undo something done by the user instead.
        """
        from jarvis.runtime.timers import timer_manager
        for code in locales:
            if self.stop.is_set(): break
            for group in assistive_groups():
                if group['tool'] != 'chance': continue
                action = group['action']
                def verify(result):
                    data = result.data or {}
                    return 1 <= data.get('value',0) <= 6 if data.get('kind') == 'die' else data.get('side') in {'heads','tails'}
                yield self.live(code,group['phrases'][code][0],('chance',{'action':action}),verify)
            timer_phrases = dict(zip(LANGUAGES,['таймер 15 минут','timer 15 minutes','Timer 15 Minuten',
                'minuteur 15 minutes','temporizador 15 minutos','timer 15 minuti','temporizador 15 minutos']))
            manager = timer_manager(self.config.paths.root,code)
            before = {item.id for item in manager.list()}
            try:
                yield self.live(code,timer_phrases[code],('timer',{'action':'timer','seconds':900.0}),
                    lambda result: len(manager.list()) == len(before)+1 and bool(result.data.get('id')))
            finally:
                for item in manager.list():
                    if item.id not in before: manager.cancel(item.id)
                clean = {item.id for item in manager.list()} == before
                yield self.record({'locale':code,'group':'cleanup','status':'CLEANUP_PASS' if clean else 'CLEANUP_FAIL',
                    'response':'Only timers created by this fixture were removed.', 'execution':'real-owned-fixture'})

    def file_fixtures(self, locales: list[str]) -> Iterator[dict]:
        """Copy/move/rename known disposable files; verify bytes then clean only them."""
        import hashlib
        from jarvis.runtime.canonical_commands import parse_canonical_command
        pack = {}
        for file in (ROOT/'config/canonical_phrases').glob('*.json'):
            pack.update(json.loads(file.read_text(encoding='utf-8')))
        payload = b'NEO 146 isolated fixture. No user files.\n'
        expected_hash = hashlib.sha256(payload).hexdigest()
        for code in locales:
            for action in ('copy','move','rename'):
                if self.stop.is_set(): return
                folder = self.workspace/f'{code}_{action}'
                folder.mkdir(exist_ok=False)
                source, destination = folder/'source.txt', folder/'result.txt'
                source.write_bytes(payload)
                phrase = pack['filesystem.'+action][LANGUAGES.index(code)].format(
                    source=str(source),destination=str(destination),new_name='result.txt')
                permit = ('filesystem',{'action':action,'source':str(source),
                          **({'new_name':'result.txt'} if action=='rename' else {'destination':str(destination)})})
                try:
                    yield self.live(code,phrase,permit,lambda result:
                        destination.is_file() and destination.read_bytes()==payload and
                        (source.exists() if action=='copy' else not source.exists()))
                except Exception as exc:
                    yield self.record({'locale':code,'status':'LIVE_FAIL','group':'owned_live_fixture',
                                       'input':phrase,'response':f'{type(exc).__name__}: {exc}'})
                finally:
                    preserved = []
                    for target in (source,destination):
                        if target.is_symlink():
                            preserved.append(str(target)); continue
                        if target.exists():
                            if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==expected_hash:
                                target.unlink()
                            else: preserved.append(str(target))
                    try: folder.rmdir()
                    except OSError: preserved.append(str(folder))
                    yield self.record({'locale':code,'group':'cleanup',
                        'status':'CLEANUP_FAIL' if preserved else 'CLEANUP_PASS',
                        'response':'Changed/unexpected files preserved: '+str(preserved) if preserved else
                                   'Only exact fixture files removed; baseline restored.',
                        'execution':'real-owned-fixture'})

    def finish(self) -> dict:
        if self._closed:
            return self.summary
        self._closed = True
        self._journal.close()
        self.summary = {'schema_version':1,'started':self.started,'finished':time.time(),
                        'stopped':self.stop.is_set(),'counts':dict(self.counts),
                        'by_locale':{k:dict(v) for k,v in self.by_locale.items()},
                        'scope':self.manifest, 'duration_seconds':round(time.time()-self.started,3)}
        self._write('summary.json', self.summary)
        lines = ['NEO 4.0.20.146 — atlas session', 'Counts are routing/validation checks unless LIVE_PASS.',
                 'NEEDS_INPUT, NEEDS_FIXTURE, NO_ROUTE are not successful live executions.',
                 'No user applications were opened/closed; no music account was controlled.',
                 'Microphone, Windows GUI, live APIs require separate acceptance.', '',
                 *[f'{key}: {value}' for key,value in self.counts.items()]]
        (self.output/'SUMMARY.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        try: self.agent.database.close()
        except Exception: logging.getLogger(__name__).debug('QA database close',exc_info=True)
        try: self.agent.game_web.close()
        except Exception: logging.getLogger(__name__).debug('QA game-web close',exc_info=True)
        return self.summary
