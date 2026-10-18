"""LoRA fine-tuning of the NEO agent model (runs in the separate training environment).

    <train-venv>/python train_lora.py --data train.jsonl --base <hf-id-or-folder> --out <folder>
        [--epochs 1] [--rank 16] [--lr 2e-4] [--max-len 1280] [--accum 8] [--max-hours 3] [--cpu-threads 2]
        [--pause-file p] [--stop-file s] [--status status.json] [--max-steps N] [--cache-dir d]

Self-contained on purpose: it imports only torch / transformers / peft, which live in
their own virtual environment, so NEO itself never loads them.

* Loss only on the assistant answer (the JSON), never on the prompt.
* Checkpoint every ``--save-every`` optimizer steps; running again resumes from it.
* A pause file makes it wait between steps (NEO creates it when you use the computer);
  a stop file saves a checkpoint and exits with code 10.
* ``--max-hours``: after the first steps the speed is measured and the number of steps
  is capped to fit that time (a slow CPU trains on fewer examples instead of for days).
* At the end the adapter is merged into the base weights and saved (safetensors,
  float16) with the tokenizer, ready for ``ollama create``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import random
import sys
import time

EXIT_STOPPED = 10


def _status(path: str, **values) -> None:
    if not path:
        return
    values['time'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    temporary = Path(path).with_suffix('.tmp')
    temporary.write_text(json.dumps(values, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(temporary, path)


def _encode(tokenizer, messages, max_len):
    """(input_ids, labels) with the prompt masked; None if the answer does not fit."""
    if not messages or messages[-1].get('role') != 'assistant':
        return None                          # a chat row whose reply was not written yet
    prompt_ids = tokenizer.apply_chat_template(messages[:-1], add_generation_prompt=True, tokenize=True)
    full_ids = tokenizer.apply_chat_template(messages, tokenize=True)
    if full_ids[:len(prompt_ids)] != prompt_ids or len(full_ids) <= len(prompt_ids):
        return None
    if len(full_ids) > max_len:
        return None
    labels = [-100] * len(prompt_ids) + full_ids[len(prompt_ids):]
    return full_ids, labels


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--cache-dir', default='')
    parser.add_argument('--epochs', type=float, default=1.0)
    parser.add_argument('--rank', type=int, default=16)
    parser.add_argument('--alpha', type=int, default=32)
    parser.add_argument('--lr', type=float, default=2e-4)
    parser.add_argument('--max-len', type=int, default=1280)
    parser.add_argument('--accum', type=int, default=8)
    parser.add_argument('--save-every', type=int, default=25)
    parser.add_argument('--max-hours', type=float, default=3.0)
    parser.add_argument('--max-steps', type=int, default=0)
    parser.add_argument('--device', default='auto')
    parser.add_argument('--cpu-threads', type=int, choices=range(1, 17), default=2)
    parser.add_argument('--pause-file', default='')
    parser.add_argument('--stop-file', default='')
    parser.add_argument('--status', default='')
    parser.add_argument('--seed', type=int, default=17)
    parser.add_argument('--no-merge', action='store_true')
    args = parser.parse_args(argv)

    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import LoraConfig, PeftModel, get_peft_model

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    checkpoint = out / 'checkpoint'
    device = ('cuda' if torch.cuda.is_available() else 'cpu') if args.device == 'auto' else args.device
    if device == 'cuda':
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    else:
        dtype = torch.float32
        torch.set_num_threads(min(args.cpu_threads, max(1, os.cpu_count() or 1)))
        torch.set_num_interop_threads(1)
    _status(args.status, stage='load', device=device)

    kwargs = {'cache_dir': args.cache_dir} if args.cache_dir else {}
    tokenizer = AutoTokenizer.from_pretrained(args.base, **kwargs)
    model = AutoModelForCausalLM.from_pretrained(args.base, torch_dtype=dtype, **kwargs)
    model.config.use_cache = False
    if device == 'cuda':
        free, _total = torch.cuda.mem_get_info()
        if free < 10 * 1024 ** 3:
            model.gradient_checkpointing_enable()
            model.enable_input_require_grads()
    targets = [n for n in ('q_proj', 'k_proj', 'v_proj', 'o_proj', 'gate_proj', 'up_proj', 'down_proj')
               if any(name.endswith(n) for name, _ in model.named_modules())]
    lora = LoraConfig(r=args.rank, lora_alpha=args.alpha, lora_dropout=0.05, target_modules=targets, task_type='CAUSAL_LM')
    state = {'step': 0, 'seen': 0, 'loss': None}
    if (checkpoint / 'adapter_config.json').exists():
        model = PeftModel.from_pretrained(model, str(checkpoint), is_trainable=True)
        state.update(json.loads((checkpoint / 'state.json').read_text(encoding='utf-8')))
    else:
        model = get_peft_model(model, lora)
    model.to(device)
    for parameter in model.parameters():
        if parameter.requires_grad:
            parameter.data = parameter.data.float()      # LoRA weights train in float32

    rows = [json.loads(line) for line in Path(args.data).read_text(encoding='utf-8').splitlines() if line.strip()]
    rng = random.Random(args.seed)
    rng.shuffle(rows)
    examples = [e for e in (_encode(tokenizer, r['messages'], args.max_len) for r in rows) if e is not None]
    if not examples:
        _status(args.status, stage='failed', error='no examples fit')
        return 2
    micro_total = int(len(examples) * args.epochs)
    total_steps = max(1, micro_total // args.accum)
    if args.max_steps:
        total_steps = min(total_steps, args.max_steps)
    trainable = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(trainable, lr=args.lr, weight_decay=0.0)
    if (checkpoint / 'optimizer.pt').exists():
        optimizer.load_state_dict(torch.load(checkpoint / 'optimizer.pt', map_location=device))
    warmup = max(1, min(20, total_steps // 10))

    def lr_at(step):
        if step < warmup:
            return args.lr * (step + 1) / warmup
        return args.lr * max(0.05, 0.5 * (1 + math.cos(math.pi * (step - warmup) / max(1, total_steps - warmup))))

    scaler = torch.amp.GradScaler('cuda') if device == 'cuda' and dtype == torch.float16 else None
    model.train()
    started = time.monotonic()
    timed_steps, timed_seconds = 0, 0.0
    step = int(state['step'])

    def save():
        model.save_pretrained(str(checkpoint))
        torch.save(optimizer.state_dict(), checkpoint / 'optimizer.pt')
        (checkpoint / 'state.json').write_text(json.dumps({'step': step, 'seen': state['seen'], 'loss': state['loss'],
                                                           'total_steps': total_steps}), encoding='utf-8')

    while step < total_steps:
        if args.stop_file and Path(args.stop_file).exists():
            save()
            _status(args.status, stage='stopped', step=step, total=total_steps, loss=state['loss'], device=device)
            return EXIT_STOPPED
        while args.pause_file and Path(args.pause_file).exists():
            _status(args.status, stage='paused', step=step, total=total_steps, loss=state['loss'], device=device)
            time.sleep(5)
            if args.stop_file and Path(args.stop_file).exists():
                break
        step_started = time.monotonic()
        optimizer.zero_grad(set_to_none=True)
        loss_sum = 0.0
        for group in optimizer.param_groups:
            group['lr'] = lr_at(step)
        for micro in range(args.accum):
            ids, labels = examples[(step * args.accum + micro) % len(examples)]
            input_ids = torch.tensor([ids], device=device)
            label_ids = torch.tensor([labels], device=device)
            with torch.autocast(device_type=device, dtype=dtype, enabled=device == 'cuda'):
                loss = model(input_ids=input_ids, labels=label_ids).loss / args.accum
            if scaler is not None:
                scaler.scale(loss).backward()
            else:
                loss.backward()
            loss_sum += float(loss.detach())
        if scaler is not None:
            scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(trainable, 1.0)
        if scaler is not None:
            scaler.step(optimizer)
            scaler.update()
        else:
            optimizer.step()
        step += 1
        state['seen'] += args.accum
        state['loss'] = round(loss_sum, 4) if state['loss'] is None else round(0.9 * state['loss'] + 0.1 * loss_sum, 4)
        timed_steps += 1
        timed_seconds += time.monotonic() - step_started
        if timed_steps == 5 and args.max_hours > 0:
            # Fit the run into the time budget: a slow machine trains on fewer examples.
            per_step = timed_seconds / timed_steps
            affordable = int(args.max_hours * 3600 / max(per_step, 1e-6))
            if affordable < total_steps:
                total_steps = max(step + 1, affordable)
        eta = (total_steps - step) * (timed_seconds / max(1, timed_steps))
        _status(args.status, stage='train', step=step, total=total_steps, loss=state['loss'], device=device,
                eta_seconds=int(eta), examples=len(examples))
        if step % args.save_every == 0:
            save()
    save()
    if args.no_merge:
        _status(args.status, stage='trained', step=step, total=total_steps, loss=state['loss'], device=device)
        return 0
    _status(args.status, stage='merge', step=step, total=total_steps, loss=state['loss'], device=device)
    merged = model.merge_and_unload()
    merged = merged.to(torch.float16)                # Ollama quantizes on import; float16 halves the files
    merged.config.use_cache = True
    target = out / 'merged'
    merged.save_pretrained(str(target), safe_serialization=True)
    tokenizer.save_pretrained(str(target))
    (out / 'train_report.json').write_text(json.dumps({
        'steps': step, 'examples_seen': state['seen'], 'examples_available': len(examples), 'loss': state['loss'],
        'device': device, 'dtype': str(dtype).replace('torch.', ''), 'minutes': round((time.monotonic() - started) / 60, 1),
        'rank': args.rank, 'lr': args.lr}, indent=1), encoding='utf-8')
    _status(args.status, stage='merged', step=step, total=total_steps, loss=state['loss'], device=device)
    return 0


if __name__ == '__main__':
    sys.exit(main())
