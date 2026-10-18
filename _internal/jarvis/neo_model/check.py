"""Check (and if needed rebuild) the NEO command model — used by the installer and NEO.cmd.

    python -m jarvis.neo_model.check [--install] [--json]

* verifies the files of the shipped model against checksums.json;
* loads it and runs a few requests, reporting load time, memory and latency;
* with --install, a missing or damaged model is rebuilt on this computer from the
  phrase atlas shipped with NEO (takes several minutes, needs no internet).

Exit codes: 0 ready, 3 rebuilt locally, 2 failed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from jarvis.neo_model.store import BUNDLED

SMOKE = [('открой браузер', 'ru'), ('scroll down a bit please', 'en'), ('wie geht es dir', 'de')]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def write_checksums(folder: Path) -> None:
    sums = {name: sha256(folder / name) for name in ('intent.npz', 'slots.npz', 'meta.json')}
    (folder / 'checksums.json').write_text(json.dumps(sums, indent=1), encoding='utf-8')


def verify(folder: Path) -> tuple[bool, str]:
    sums_path = folder / 'checksums.json'
    if not sums_path.exists():
        return False, 'checksums.json missing'
    sums = json.loads(sums_path.read_text(encoding='utf-8'))
    for name, expected in sums.items():
        path = folder / name
        if not path.exists():
            return False, f'{name} missing'
        if sha256(path) != expected:
            return False, f'{name} damaged'
    return True, 'ok'


def measure(folder: Path) -> dict:
    import psutil
    process = psutil.Process()
    before = process.memory_info().rss
    started = time.perf_counter()
    from jarvis.neo_model.predictor import NeoCommandModel
    model = NeoCommandModel(folder)
    load_ms = (time.perf_counter() - started) * 1000
    results = [model.ranked(text, code)[0] for text, code in SMOKE]
    started = time.perf_counter()
    for _ in range(50):
        model.ranked(SMOKE[1][0], 'en')
    latency = (time.perf_counter() - started) * 1000 / 50
    return {'version': folder.name, 'load_ms': round(load_ms, 1), 'predict_ms': round(latency, 2),
            'ram_mb': round((process.memory_info().rss - before) / 1048576, 1),
            'disk_mb': round(sum(p.stat().st_size for p in folder.iterdir() if p.is_file()) / 1048576, 1),
            'smoke': [[intent, round(p, 3)] for intent, p in results]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    folders = sorted(p for p in BUNDLED.iterdir() if p.is_dir()) if BUNDLED.exists() else []
    report: dict = {'folder': str(BUNDLED)}
    status = 2
    for folder in reversed(folders):
        ok, reason = verify(folder)
        report[folder.name] = reason
        if ok:
            report.update(measure(folder))
            status = 0
            break
    if status != 0 and args.install:
        print('The NEO command model is missing or damaged; building it on this computer (several minutes)...', flush=True)
        from jarvis.neo_model.train import train_version
        out = BUNDLED / 'v1-local'
        train_version(out, epochs=5, log=lambda line: print(line, flush=True))
        write_checksums(out)
        report.update(measure(out))
        status = 3
    print(json.dumps(report, ensure_ascii=False) if args.json else
          '\n'.join(f'{k}: {v}' for k, v in report.items()))
    return status


if __name__ == '__main__':
    sys.exit(main())
