"""Local LoRA fine-tuning of the NEO agent model, end to end, resumable.

    python -m jarvis.neo_llm.pipeline run [--force] [--local-only]

Started by NEO in the background (jarvis.neo_llm.jobs) when the computer is idle, or by
«дообучи модель ассистента». Everything stays on this computer. Stages (each one is
skipped when its result already exists, so an interrupted run continues):

Automatic idle runs use --local-only: setup must already be complete, and model
libraries run offline. Only explicit training may install tools or download weights.

1. venv      — a separate Python environment in %LOCALAPPDATA%/NeoJarvis/llm_train/venv
2. packages  — PyTorch (CUDA build when an NVIDIA GPU is present), transformers, peft
3. base      — the Hugging Face weights of the same Qwen2.5 Instruct size NEO uses
4. data      — NEO's own command data in the runtime prompt format (jarvis.neo_llm.export)
5. teacher   — chat rows get their reply from the current model, so fine-tuning keeps chat
6. train     — LoRA on the assistant answers only; pauses while you use the computer
7. import    — the merged weights become a unique, initially unselected Ollama alias
8. evaluate  — current model vs candidate on held-out NEO data (never seen in training)
9. decide    — the candidate replaces the current model only if it is clearly better;
               the previous version is kept for «откати модель ассистента».

Exit codes: 0 finished (promoted or rejected) · 10 stopped (will resume) · 1 failed.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, Callable
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
TORCH_INDEXES = ('https://download.pytorch.org/whl/cu124', 'https://download.pytorch.org/whl/cu121')
PACKAGES = ('transformers==4.46.3', 'peft==0.13.2', 'accelerate==1.1.1', 'safetensors', 'huggingface_hub>=0.24')
CANDIDATE, FINE_TUNED, PREVIOUS = 'neo-agent-ft-candidate', 'neo-agent-ft', 'neo-agent-ft-prev'
EXIT_STOPPED = 10
EXIT_WAITING = 20
WAITING_NOTE = ('Automatic fine-tuning needs local training tools and the selected model weights. '
                'Say "fine-tune the assistant model" '
                'to set them up explicitly; that first run downloads tools and weights. '
                'Automatic training also requires a localhost/loopback Ollama address in developer settings.')
logger = logging.getLogger(__name__)


# --------------------------------------------------------------- small helpers
def work_root(config: Any) -> Path:
    return Path(config.paths.root) / 'llm_train'


def venv_python(root: Path) -> Path:
    return root / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def training_model(config: Any) -> tuple[str, dict[str, str]]:
    """Resolve the actual weights exactly; an Ollama tag is not an HF checkpoint."""
    from jarvis.runtime.settings170 import SIZES
    size = str(config.get('llm_agent.size', '0.5b') or '0.5b').strip().casefold()
    if size not in SIZES:
        raise ValueError(f'Unsupported fine-tuning size: {size}. Select 0.5b or 1.5b explicitly.')
    selected = SIZES[size]
    base = str(config.get('llm_agent.base_model', '') or '').strip()
    if base and base.casefold() != selected['ollama'].casefold():
        raise ValueError(f'Fine-tuning size {size} requires {selected["ollama"]}; '
                         f'the configured base {base} has no matching supported training weights.')
    return size, selected


def base_folder(config: Any) -> Path:
    _size, selected = training_model(config)
    return work_root(config) / 'base' / selected['hf'].replace('/', '__')


def local_base_ready(folder: Path) -> bool:
    """Check small metadata and file sizes, never import torch or read the weights."""
    try:
        for name in ('config.json', 'tokenizer_config.json', 'tokenizer.json'):
            if not (folder / name).is_file() or not (folder / name).stat().st_size:
                return False
        index = folder / 'model.safetensors.index.json'
        if index.exists():
            shards = set(json.loads(index.read_text(encoding='utf-8'))['weight_map'].values())
            return bool(shards) and all(
                isinstance(name, str) and Path(name).name == name and
                (folder / name).is_file() and (folder / name).stat().st_size > 0
                for name in shards)
        weights = folder / 'model.safetensors'
        return weights.is_file() and weights.stat().st_size > 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def loopback_endpoint(url: str) -> bool:
    """No DNS lookup: automatic training may only send examples to this computer."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or not parsed.hostname:
            return False
        _ = parsed.port  # reject malformed ports before a request is attempted
        if parsed.hostname.lower() == 'localhost':
            return True
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except (ValueError, TypeError):
        return False


def model_digest(base_url: str, model: str, *, local_only: bool = False) -> str:
    """Read the exact installed manifest identity, never infer it from model size."""
    if local_only and not loopback_endpoint(base_url):
        return ''
    import requests
    session = requests.Session()
    if local_only:
        session.trust_env = False
    response = None
    try:
        response = session.get(f'{base_url}/api/tags', timeout=(5, 10), allow_redirects=False)
        response.raise_for_status()
        wanted = model if ':' in model.rsplit('/', 1)[-1] else model + ':latest'
        for row in response.json().get('models', []):
            name = str(row.get('name', ''))
            name = name if ':' in name.rsplit('/', 1)[-1] else name + ':latest'
            if name != wanted:
                continue
            digest = str(row.get('digest', '')).lower()
            if re.fullmatch(r'(?:sha256:)?[0-9a-f]{64}', digest):
                return 'sha256:' + digest.removeprefix('sha256:')
    except Exception:
        logger.debug('Cannot verify the installed training model identity', exc_info=True)
    finally:
        if response is not None:
            response.close()
        session.close()
    return ''


def local_prerequisites(config: Any, *, base_url: str | None = None) -> list[str]:
    """Missing setup for an automatic run; strictly local, bounded, read-only checks."""
    root = work_root(config)
    missing = []
    if not venv_python(root).is_file():
        missing.append('training environment')
    try:
        installed = json.loads((root / 'venv' / 'neo_packages.json').read_text(encoding='utf-8'))
        if not isinstance(installed, dict) or not installed.get('torch'):
            missing.append('training packages')
    except (OSError, ValueError):
        missing.append('training packages')
    try:
        if not local_base_ready(base_folder(config)):
            missing.append('selected model weights and tokenizer')
    except ValueError as exc:
        missing.append(str(exc))
    endpoint = base_url if base_url is not None else str(config.get('ollama.base_url', 'http://localhost:11434') or 'http://localhost:11434')
    if not loopback_endpoint(endpoint):
        missing.append('local Ollama endpoint')
    return missing


def gpu_present() -> bool:
    exe = shutil.which('nvidia-smi') or (r'C:\Windows\System32\nvidia-smi.exe' if os.name == 'nt' else '')
    if not exe or not Path(exe).exists():
        return False
    try:
        return subprocess.run([exe, '-L'], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def torch_install_commands(python: str, gpu: bool) -> list[list[str]]:
    """Commands to try in order: CUDA builds when an NVIDIA GPU is present, then the default (CPU) build."""
    base = [python, '-m', 'pip', 'install', '--disable-pip-version-check', 'torch']
    commands = [base + ['--index-url', index] for index in TORCH_INDEXES] if gpu else []
    return commands + [base]


def ollama_exe() -> str:
    found = shutil.which('ollama')
    if found:
        return found
    for path in (Path(os.getenv('LOCALAPPDATA', '')) / 'Programs' / 'Ollama' / 'ollama.exe',
                 Path(os.getenv('ProgramFiles', '')) / 'Ollama' / 'ollama.exe'):
        if path.exists():
            return str(path)
    return ''


def modelfile(merged: Path) -> str:
    """Written next to the merged folder, so FROM is relative (Ollama resolves it from the Modelfile's folder)."""
    from jarvis.neo_llm.setup import PARAMETERS, SYSTEM
    lines = [f'FROM ./{merged.name}', f'SYSTEM """{SYSTEM}"""']
    lines += [f'PARAMETER {k} {v}' for k, v in PARAMETERS.items()]
    return '\n'.join(lines) + '\n'


def default_examples(gpu: bool) -> int:
    return 6000 if gpu else 1200


# --------------------------------------------------------------- evaluation
def score(rows: list[dict[str, Any]], answers: list[dict[str, Any] | None]) -> dict[str, float]:
    """How well answers match held-out NEO examples.

    * do_exact — the model's commands are exactly the expected NEO commands (same
      intents, same order, each one read by NEO's grammar as that intent);
    * chat_ok — for "not a command" rows the model answered in words, not with commands;
    * json_ok — the answer was a JSON object at all.
    """
    from jarvis.neo_llm.export import understood_as
    do_rows = do_hits = chat_rows = chat_hits = json_hits = 0
    for row, answer in zip(rows, answers):
        meta = row.get('meta') or {}
        if isinstance(answer, dict):
            json_hits += 1
        if meta.get('kind') == 'chat':
            chat_rows += 1
            chat_hits += bool(isinstance(answer, dict) and not answer.get('do') and (answer.get('reply') or answer.get('ask')))
            continue
        do_rows += 1
        commands = answer.get('do') if isinstance(answer, dict) else None
        gold = list(meta.get('gold') or [])
        code = meta.get('language', 'en')
        if isinstance(commands, list) and len(commands) == len(gold) and all(
                isinstance(c, str) and understood_as(c, code, intent) for c, intent in zip(commands, gold)):
            do_hits += 1
    return {'do_exact': round(do_hits / max(1, do_rows), 4),
            'chat_ok': round(chat_hits / chat_rows, 4) if chat_rows else None,
            'json_ok': round(json_hits / max(1, len(rows)), 4), 'rows': len(rows), 'do_rows': do_rows, 'chat_rows': chat_rows}


def sample(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` rows taken in turn from every kind (single, sequence, web, chat) — a fair small check."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_kind.setdefault((row.get('meta') or {}).get('kind', ''), []).append(row)
    picked: list[dict[str, Any]] = []
    while len(picked) < limit and any(by_kind.values()):
        for kind in sorted(by_kind):
            if by_kind[kind] and len(picked) < limit:
                picked.append(by_kind[kind].pop(0))
    return picked


def should_promote(current: dict[str, float], candidate: dict[str, float]) -> tuple[bool, list[str]]:
    reasons = []
    if candidate['json_ok'] < 0.95:
        reasons.append(f"JSON answers {candidate['json_ok']:.0%} < 95%")
    if candidate['do_exact'] < current['do_exact'] + 0.02:
        reasons.append(f"commands {candidate['do_exact']:.1%} not better than {current['do_exact']:.1%} (+2 points needed)")
    if candidate.get('chat_ok') is not None and current.get('chat_ok') is not None and candidate['chat_ok'] < current['chat_ok'] - 0.05:
        reasons.append(f"conversation {candidate['chat_ok']:.1%} worse than {current['chat_ok']:.1%}")
    return not reasons, reasons


def ask_model(base_url: str, model: str, messages: list[dict[str, str]], *, schema: dict | None, timeout: float = 180,
              num_predict: int = 200, local_only: bool = False) -> dict[str, Any] | str | None:
    if local_only and not loopback_endpoint(base_url):
        return None
    import requests
    body = {'model': model, 'messages': messages, 'stream': False, 'think': False, 'keep_alive': '5m',
            'options': {'temperature': 0, 'num_predict': num_predict}}
    if schema is not None:
        body['format'] = schema
    session = requests.Session() if local_only else None
    if session is not None:
        session.trust_env = False  # a system proxy must not receive local training examples
    post = session.post if session is not None else requests.post
    try:
        response = post(f'{base_url}/api/chat', json=body, timeout=(10, timeout), allow_redirects=not local_only)
        if response.status_code == 400 and 'think' in response.text:
            body.pop('think')
            response = post(f'{base_url}/api/chat', json=body, timeout=(10, timeout), allow_redirects=not local_only)
        response.raise_for_status()
        content = str((response.json().get('message') or {}).get('content', ''))
    except Exception:
        return None
    finally:
        if session is not None:
            session.close()
    if schema is None:
        return content.strip()
    try:
        value = json.loads(content)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def evaluate(base_url: str, model: str, rows: list[dict[str, Any]], progress: Callable[[int, int], None] = lambda *_: None,
             *, local_only: bool = False) -> dict[str, float]:
    from jarvis.neo_llm import prompt
    answers = []
    for index, row in enumerate(rows, 1):
        messages = [m for m in row['messages'] if m['role'] != 'assistant']
        answers.append(ask_model(base_url, model, messages, schema=prompt.SCHEMA, local_only=local_only))
        progress(index, len(rows))
    return score(rows, answers)


# --------------------------------------------------------------- the run
class Run:
    def __init__(self, config: Any, *, log: Callable[[str], None] = print, local_only: bool = False) -> None:
        self.config = config
        self.root = work_root(config)
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.local_only = local_only
        self.state_path = self.root / 'state.json'
        self.state = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {}
        self.size, selected = training_model(config)
        self.hf_id = selected['hf']
        self.base_model = selected['ollama']
        # Different weight shapes must never share adapters/optimizer checkpoints.
        # Keep old run files intact; they cannot establish ownership of a new base.
        self.run_root = self.root / 'runs' / self.hf_id.replace('/', '__')
        self.base_url = str(config.get('ollama.base_url', 'http://localhost:11434') or 'http://localhost:11434').rstrip('/')
        self.imported_digest = ''
        self.candidate = ''
        self.selected_agent = (str(config.get('llm_agent.model', 'neo-agent') or 'neo-agent'),
                               str(config.get('llm_agent.fine_tuned', '') or ''))
        self.selected_provenance = json.loads(json.dumps(config.get('llm_agent.fine_tuned_provenance', {}) or {}))

    # state ---------------------------------------------------------------
    def save_state(self) -> None:
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(tmp, self.state_path)

    def status(self, stage: str, **values: Any) -> None:
        payload = {'stage': stage, 'time': time.strftime('%Y-%m-%dT%H:%M:%S'), 'size': self.size, **values}
        (self.root / 'status.json').write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
        self.log(f'[{stage}] ' + ', '.join(f'{k}={v}' for k, v in values.items()))

    def stopped(self) -> bool:
        return (self.root / 'stop.flag').exists()

    def _run(self, command: list[str], *, low: bool = True, timeout: float | None = None) -> int:
        flags = 0
        if os.name == 'nt':
            flags = 0x08000000 | (0x00004000 if low else 0)       # CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS
        with (self.root / 'pipeline_tools.log').open('a', encoding='utf-8') as handle:
            handle.write('\n$ ' + ' '.join(command) + '\n')
            handle.flush()
            env = dict(os.environ, OLLAMA_HOST=self.base_url)
            if self.local_only:
                env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                           HF_DATASETS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', PIP_NO_INDEX='1',
                           NO_PROXY='*', no_proxy='*')
            process = subprocess.Popen(command, cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
                                       creationflags=flags, env=env)
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                return 124

    # stages --------------------------------------------------------------
    def venv(self) -> bool:
        python = venv_python(self.root)
        if python.exists():
            return True
        if self.local_only:
            return False
        self.status('venv')
        from jarvis.runtime.process_host171 import base_python
        return self._run([str(base_python()), '-m', 'venv', str(self.root / 'venv')], low=False) == 0 and python.exists()

    def packages(self) -> bool:
        python = str(venv_python(self.root))
        marker = self.root / 'venv' / 'neo_packages.json'
        if marker.exists():
            return True
        if self.local_only:
            return False
        gpu = gpu_present()
        self.status('packages', gpu=gpu)
        for command in torch_install_commands(python, gpu):
            if self._run(command) == 0:
                break
        else:
            return False
        if self._run([python, '-m', 'pip', 'install', '--disable-pip-version-check', *PACKAGES]) != 0:
            return False
        check = subprocess.run([python, '-c', 'import torch, json; print(json.dumps({"cuda": torch.cuda.is_available(), '
                                '"torch": torch.__version__}))'], capture_output=True, text=True, timeout=300)
        if check.returncode != 0:
            return False
        info = json.loads(check.stdout.strip().splitlines()[-1])
        marker.write_text(json.dumps({**info, 'gpu_seen': gpu}), encoding='utf-8')
        return True

    def base(self) -> Path | None:
        folder = self.root / 'base' / self.hf_id.replace('/', '__')
        if local_base_ready(folder):
            return folder
        if self.local_only:
            return None
        self.status('base', model=self.hf_id)
        script = ('import sys; from huggingface_hub import snapshot_download; '
                  'snapshot_download(sys.argv[1], local_dir=sys.argv[2], '
                  'allow_patterns=["*.json", "*.safetensors", "*.txt", "merges.txt", "vocab.json", "tokenizer*"])')
        code = self._run([str(venv_python(self.root)), '-c', script, self.hf_id, str(folder)])
        return folder if code == 0 and (folder / 'config.json').exists() else None

    def data(self, fingerprint_extra: str) -> Path | None:
        folder = self.root / 'data'
        info_path = folder / 'info.json'
        if info_path.exists() and json.loads(info_path.read_text(encoding='utf-8')).get('run_key') == fingerprint_extra:
            return folder
        self.status('data')
        from jarvis.neo_llm import export
        from jarvis.runtime import training_corrections188
        # NEO 188 NEO-PRIVACY-002: explicitly allowed, minimized copies only, evaluated now.
        corrections = training_corrections188.eligible(self.config)
        limit = int(self.config.get('llm_agent.finetune.max_examples', 0) or 0) or default_examples(self._gpu())
        built = export.build(max_examples=limit, corrections=corrections)
        info = export.save(built, folder)
        info['run_key'] = fingerprint_extra
        info_path.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding='utf-8')
        return folder

    def _gpu(self) -> bool:
        marker = self.root / 'venv' / 'neo_packages.json'
        if marker.exists():
            return bool(json.loads(marker.read_text(encoding='utf-8')).get('cuda'))
        return gpu_present()

    def teacher(self, folder: Path) -> bool:
        """Write replies for chat rows with the current model (keeps the fine-tuned model conversational)."""
        path = folder / 'train.jsonl'
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        todo = [r for r in rows if r.get('teacher') and r['messages'][-1]['role'] != 'assistant']
        if not todo:
            return True
        from jarvis.neo_llm import prompt
        from jarvis.runtime import local_chat169
        model = self.current_model()
        self.status('teacher', rows=len(todo), model=model)
        for index, row in enumerate(todo, 1):
            if self.stopped():
                break
            user = row['messages'][-1]['content']
            code = (row.get('meta') or {}).get('language', 'en')
            reply = ask_model(self.base_url, model, local_chat169.build_messages(user, code), schema=None,
                              num_predict=160, local_only=self.local_only)
            if isinstance(reply, str) and reply and not reply.lstrip().startswith('{'):
                row['messages'].append({'role': 'assistant', 'content': prompt.target(reply=reply)})
            else:
                row['teacher_failed'] = True
            if index % 25 == 0:
                self.status('teacher', done=index, rows=len(todo))
        kept = [r for r in rows if not r.get('teacher_failed')]
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in kept), encoding='utf-8')
        return not self.stopped()

    def train(self, base: Path, data: Path) -> int:
        out = self.run_root
        if (out / 'merged' / 'config.json').exists() and self.state.get('trained_key') == self.state.get('run_key'):
            return 0
        gpu = self._gpu()
        hours = float(self.config.get('llm_agent.finetune.max_hours', 0) or 0) or (3.0 if gpu else 6.0)
        cpu_threads = int(self.config.get('llm_agent.finetune.cpu_threads', 2))
        if not 1 <= cpu_threads <= 16:
            raise ValueError('Fine-tuning CPU threads must be between 1 and 16.')
        command = [str(venv_python(self.root)), str(Path(__file__).with_name('train_lora.py')),
                   '--data', str(data / 'train.jsonl'), '--base', str(base), '--out', str(out),
                   '--epochs', str(self.config.get('llm_agent.finetune.epochs', 1) or 1), '--max-hours', str(hours),
                   '--cpu-threads', str(cpu_threads),
                   '--pause-file', str(self.root / 'pause.flag'), '--stop-file', str(self.root / 'stop.flag'),
                   '--status', str(self.root / 'train_status.json')]
        self.status('train', device='cuda' if gpu else 'cpu', hours=hours)
        code = self._run(command)
        if code == 0:
            self.state['trained_key'] = self.state.get('run_key')
            self.save_state()
        return code

    def import_model(self) -> bool:
        self.imported_digest = ''
        exe = ollama_exe()
        merged = self.run_root / 'merged'
        if not exe or not (merged / 'config.json').exists():
            return False
        # A new alias for every import attempt: never overwrite a live selected
        # model or another run's candidate, including on retry/resume.
        self.candidate = 'neo-agent-ft-run-' + uuid.uuid4().hex
        self.status('import', model=self.candidate)
        (self.run_root / 'Modelfile').write_text(modelfile(merged), encoding='utf-8')
        for extra in (['--quantize', 'q4_K_M'], []):
            if self._run([exe, 'create', self.candidate, '-f', str(self.run_root / 'Modelfile'), *extra], low=False) == 0:
                self.imported_digest = model_digest(self.base_url, self.candidate, local_only=self.local_only)
                return bool(self.imported_digest)
        return False

    def current_model(self) -> str:
        from jarvis.ollama_client import OllamaClient
        overrides = {'ollama.base_url': self.base_url, 'llm_agent.size': self.size,
                     'llm_agent.base_model': self.base_model,
                     'llm_agent.model': self.selected_agent[0], 'llm_agent.fine_tuned': self.selected_agent[1],
                     'llm_agent.fine_tuned_provenance': self.selected_provenance}
        if self.local_only:
            if not loopback_endpoint(self.base_url):
                raise ValueError('Automatic training requires a local Ollama endpoint.')
            overrides.update({'ollama.auto_start': False, 'llm_agent.auto_install': False})
        config = SimpleNamespace(get=lambda key, default=None: overrides.get(key, self.config.get(key, default)))
        client = OllamaClient(config)
        try:
            return client.resolve_agent_model()
        finally:
            client._session.close()

    def decide(self, data: Path) -> dict[str, Any]:
        if not self.imported_digest or model_digest(self.base_url, self.candidate, local_only=self.local_only) != self.imported_digest:
            raise RuntimeError('The imported fine-tuning candidate could not be verified before evaluation.')
        rows = [json.loads(line) for line in (data / 'eval.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        limit = int(self.config.get('llm_agent.finetune.eval_rows', 240) or 240)
        rows = sample(rows, limit)
        current_name = self.current_model()
        self.status('evaluate', model=current_name, rows=len(rows))
        current = evaluate(self.base_url, current_name, rows, local_only=self.local_only)
        self.status('evaluate', model=self.candidate, rows=len(rows))
        candidate = evaluate(self.base_url, self.candidate, rows, local_only=self.local_only)
        promoted, reasons = should_promote(current, candidate)
        better = bool(promoted)
        exe = ollama_exe()
        if promoted:
            promoted, reason = self._promote(exe)
            if reason:
                reasons.append(reason)
        # HIST-071: quality («better») and delivery («promoted») are kept apart.
        report = {'time': time.strftime('%Y-%m-%dT%H:%M:%S'), 'size': self.size, 'current': current, 'candidate': candidate,
                  'candidate_model': self.candidate,
                  'current_model': current_name, 'promoted': promoted, 'better': better,
                  'activation_failed': better and not promoted, 'reasons': reasons}
        history = self.root / 'history.jsonl'
        with history.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(report, ensure_ascii=False) + '\n')
        return report

    def _selection_matches(self, data: dict) -> bool:
        agent = data.get('llm_agent', {})
        endpoint = str(data.get('ollama', {}).get('base_url', 'http://localhost:11434') or 'http://localhost:11434').rstrip('/')
        selection = (str(agent.get('model', 'neo-agent') or 'neo-agent'), str(agent.get('fine_tuned', '') or ''))
        proofs = agent.get('fine_tuned_provenance', {})
        key = self.selected_agent[1].removesuffix(':latest')
        current_proof = proofs.get(key) if isinstance(proofs, dict) else None
        selected_proof = self.selected_provenance.get(key) if isinstance(self.selected_provenance, dict) else None
        return (str(agent.get('size', '0.5b') or '0.5b').strip().casefold() == self.size
                and str(agent.get('base_model', self.base_model) or self.base_model).strip().casefold() == self.base_model.casefold()
                and endpoint == self.base_url and selection == self.selected_agent and current_proof == selected_proof)

    def _promote(self, exe: str) -> tuple[bool, str]:
        if not self.state.get('run_key'):
            return False, 'The training run identity is missing.'
        # Read the latest selection coherently, before even inspecting the staged
        # backend alias. Returning False makes this a read-only transaction.
        matches = [False]
        def inspect_selection(data):
            matches[0] = self._selection_matches(data)
            return False
        self.config.update(inspect_selection)
        if not matches[0]:
            return False, 'Model settings changed during training; the current selection was preserved.'
        if not re.fullmatch(r'neo-agent-ft-run-[0-9a-f]{32}', self.candidate):
            return False, 'The run-owned training alias is missing.'
        digest = self.imported_digest
        if model_digest(self.base_url, self.candidate, local_only=self.local_only) != digest:
            return False, 'The fine-tuning candidate changed during evaluation.'
        provenance = {'hf_base': self.hf_id, 'ollama_base': self.base_model, 'model_digest': digest,
                      'base_url': self.base_url, 'run_key': str(self.state.get('run_key', ''))}
        def apply(data):
            if not self._selection_matches(data):
                return False
            agent = data.setdefault('llm_agent', {})
            entries = agent.setdefault('fine_tuned_provenance', {})
            if not isinstance(entries, dict):
                entries = agent['fine_tuned_provenance'] = {}
            entries[self.candidate] = provenance
            agent['fine_tuned_previous'] = self.selected_agent[1]
            agent['fine_tuned'] = self.candidate

        try:
            committed = self.config.update(apply)
        except Exception as exc:
            # Save can commit before a post-save refresh fails. Read back the
            # persistent result before describing this as an unselected model.
            try:
                self.config.reload()
                saved = self.config.get('llm_agent.fine_tuned_provenance', {})
                if (self.config.get('llm_agent.fine_tuned') == self.candidate
                        and isinstance(saved, dict) and saved.get(self.candidate) == provenance):
                    return True, 'The model selection was saved, but a post-save refresh failed.'
            except Exception:
                pass
            # The old selected alias was never touched. Retain this unselected
            # staged alias for diagnosis rather than risk deleting a user's tag.
            return False, f'The staged model {self.candidate} was not selected; settings could not be saved: {type(exc).__name__}.'
        if committed is False:
            return False, 'Model settings changed during training; the current selection was preserved.'
        return True, ''

    def _save_setting(self, key: str, value: Any) -> None:
        # Training can outlive several settings edits. Merge only its result
        # through the existing locked read-modify-write API.
        def apply(data):
            data.setdefault('llm_agent', {})[key] = value
        self.config.update(apply)

    def execute(self, *, force: bool = False) -> int:
        if self.local_only:
            missing = local_prerequisites(self.config, base_url=self.base_url)
            if missing:
                self.status('waiting_setup', missing=missing, note=WAITING_NOTE)
                return EXIT_WAITING
        (self.root / 'stop.flag').unlink(missing_ok=True)
        from jarvis.runtime import training_corrections188
        # The eligible set's fingerprint (not a count) is part of the key: after an
        # exclusion a prepared train.jsonl with the old data is never reused.
        corrections = training_corrections188.fingerprint(training_corrections188.eligible(self.config))
        run_key = f'{self.hf_id}|corrections={corrections}|v188'
        if not force and self.state.get('finished_key') == run_key:
            self.status('done', note='already fine-tuned for this data')
            return 0
        self.state['run_key'] = run_key
        self.save_state()
        if not self.venv():
            self.status('failed', stage_failed='venv')
            return 1
        if self.stopped():
            return EXIT_STOPPED
        if not self.packages():
            self.status('failed', stage_failed='packages')
            return 1
        base = self.base()
        if base is None:
            self.status('failed', stage_failed='base')
            return 1
        data = self.data(run_key)
        if data is None:
            self.status('failed', stage_failed='data')
            return 1
        if not self.teacher(data):
            return EXIT_STOPPED
        code = self.train(base, data)
        if code == EXIT_STOPPED or self.stopped():
            self.status('stopped', note='will continue next time')
            return EXIT_STOPPED
        if code != 0:
            self.status('failed', stage_failed='train', exit_code=code)
            return 1
        if not self.import_model():
            self.status('failed', stage_failed='import')
            return 1
        report = self.decide(data)
        self.state['finished_key'] = run_key
        self.save_state()
        self.status('done', promoted=report['promoted'], current=report['current'], candidate=report['candidate'],
                    reasons=report['reasons'])
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['run'])
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--local-only', action='store_true', help='Use existing local training tools and weights; never install or download')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    from jarvis.config import ConfigManager
    config = ConfigManager(ROOT / 'config' / 'default_settings.json')
    try:
        return Run(config, local_only=args.local_only).execute(force=args.force)
    except Exception as exc:
        logger.exception('Fine-tuning failed')
        try:
            (work_root(config) / 'status.json').write_text(json.dumps({'stage': 'failed', 'error': str(exc)[:500],
                                                                      'time': time.strftime('%Y-%m-%dT%H:%M:%S')}),
                                                          encoding='utf-8')
        except OSError:
            pass
        return 1


if __name__ == '__main__':
    sys.exit(main())
