"""Measure NEO's request path the same way for two builds: python tools/bench169.py [--out result.json]

Runs the real Agent in an isolated profile (tool execution blocked, no network) and reports:
* start: time and memory to create the assistant (agent + tool registry);
* idle: CPU time used while nothing happens for 10 s, and thread count;
* rules: latency of commands answered by the rules (no model at all);
* model: latency of rephrased commands answered by the NEO command model (build 169+),
  the model's cold load time and its memory;
* index: local file index of 300 generated files (full, then incremental rescan).
Run it on build 168 and 169 on the same machine to compare.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RULES = [('ru', 'прокрути вниз'), ('ru', 'открой приложение Steam'), ('ru', 'покажи номера'), ('en', 'scroll down'),
         ('en', 'show numbers'), ('ru', 'нажми Enter'), ('ru', 'сделай скриншот'), ('en', 'take a screenshot')]
REPHRASED = [('ru', 'будь добр, прокрути страничку вниз'), ('ru', 'не мог бы ты сделать звук потише'),
             ('ru', 'пожалуйста, закрой это окошко'), ('en', 'could you scroll the page down a little'),
             ('en', 'would you please make the sound a bit quieter'), ('ru', 'сделай мне скрин экрана пожалуйста')]


def _stats(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {'median_ms': round(statistics.median(ordered), 2), 'p95_ms': round(ordered[int(len(ordered) * 0.95) - 1 if len(ordered) > 1 else 0], 2),
            'max_ms': round(ordered[-1], 2)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default='')
    args = parser.parse_args()
    import psutil
    process = psutil.Process()
    result: dict = {'build': (ROOT / 'VERSION.txt').read_text(encoding='utf-8').strip(), 'cpu_count': os.cpu_count()}
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        rss0 = process.memory_info().rss
        started = time.perf_counter()
        session = AtlasSession(Path(tempfile.mkdtemp()) / 'run')
        result['start'] = {'seconds': round(time.perf_counter() - started, 2), 'rss_mb': round((process.memory_info().rss - rss0) / 1048576, 1)}
        cpu0 = sum(process.cpu_times()[:2])
        time.sleep(10)
        result['idle'] = {'cpu_seconds_per_10s': round(sum(process.cpu_times()[:2]) - cpu0, 3), 'threads': process.num_threads()}

        def run(rows: list[tuple[str, str]], repeat: int = 5) -> dict:
            times, routes = [], {}
            for _ in range(repeat):
                for code, text in rows:
                    session.reset_case(code)
                    begin = time.perf_counter()
                    turn = session.agent.handle(text)
                    times.append((time.perf_counter() - begin) * 1000)
                    call = session.calls[0] if session.calls else None
                    routes[text] = (call['tool'] + '.' + str(call['arguments'].get('action', ''))) if call else \
                        ('ask:' + (turn.tool_details or [''])[-1])
            return {**_stats(times), 'routes': routes}

        result['rules'] = run(RULES)
        try:
            from jarvis.neo_model import runtime, store
            folder = store.active_folder(session.config)
            rss1 = process.memory_info().rss
            begin = time.perf_counter()
            runtime.model(session.config)
            result['model_load'] = {'version': folder.name if folder else None, 'cold_ms': round((time.perf_counter() - begin) * 1000, 1),
                                    'rss_mb': round((process.memory_info().rss - rss1) / 1048576, 1)}
        except ImportError:
            result['model_load'] = None
        result['rephrased'] = run(REPHRASED)
        session.finish()
    try:
        from jarvis.runtime.knowledge169 import KnowledgeIndex
        folder = Path(tempfile.mkdtemp()) / 'docs'
        folder.mkdir()
        words = 'plan budget travel meeting report invoice project summary notes schedule'.split()
        for index in range(300):
            (folder / f'doc{index:03d}.md').write_text(
                '\n\n'.join(f'# Part {p}\n' + ' '.join(words[(index + p + k) % len(words)] for k in range(120)) for p in range(8)), encoding='utf-8')
        knowledge = KnowledgeIndex(Path(tempfile.mkdtemp()) / 'index.sqlite3')
        rss2, cpu2, begin = process.memory_info().rss, sum(process.cpu_times()[:2]), time.perf_counter()
        stats = knowledge.update_folders([str(folder)], pause=0)
        full = time.perf_counter() - begin
        cpu_full = sum(process.cpu_times()[:2]) - cpu2
        begin = time.perf_counter()
        knowledge.update_folders([str(folder)], pause=0)
        again = time.perf_counter() - begin
        begin = time.perf_counter()
        for _ in range(50):
            knowledge.search('travel budget meeting')
        search_ms = (time.perf_counter() - begin) * 1000 / 50
        result['index'] = {'files': stats['indexed'], 'chunks': knowledge.stats()['chunks'], 'full_seconds': round(full, 2),
                           'cpu_seconds': round(cpu_full, 2), 'incremental_seconds': round(again, 3), 'search_ms': round(search_ms, 2),
                           'rss_mb': round((process.memory_info().rss - rss2) / 1048576, 1)}
    except ImportError:
        result['index'] = None
    text = json.dumps(result, ensure_ascii=False, indent=1)
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
