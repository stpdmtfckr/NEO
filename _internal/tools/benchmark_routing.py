"""Reproducible pure routing comparison; never import Agent or execute a tool.

Each build/language/trial runs in a fresh subprocess with an isolated profile.
The fixed native-language corpus below is authored independently of route output.
This is a Linux/Windows Python microbenchmark, not a desktop/ASR performance test.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

LANGUAGES = ("ru", "en", "de", "fr", "es", "it", "pt")

# Rows are ID, expected tool, expected argument subset, and seven native texts.
# None means classification must not claim an executable route. Missing payload
# and explicit negation are included, even when a baseline did not guard them.
CORPUS = (
    ("file", "desktop", {"action": "open", "hint": "file", "query": "RAM.txt"}, (
        "открой файл RAM.txt", "open the file RAM.txt", "öffne die Datei RAM.txt",
        "ouvre le fichier RAM.txt", "abre el archivo RAM.txt", "apri il file RAM.txt", "abre o ficheiro RAM.txt")),
    ("folder", "desktop", {"action": "open", "hint": "folder", "query": "Archive"}, (
        "открой папку Archive", "open the folder Archive", "öffne den Ordner Archive",
        "ouvre le dossier Archive", "abre la carpeta Archive", "apri la cartella Archive", "abre a pasta Archive")),
    ("app", "application", {"action": "open"}, (
        "открой приложение Steam", "open application Steam", "öffne das Programm Steam",
        "ouvre l'application Steam", "abre la aplicación Steam", "apri l'applicazione Steam", "abre o aplicativo Steam")),
    ("game", "application", {"action": "open", "kind": "game", "name": "Portal 2"}, (
        "открой игру Portal 2", "open game Portal 2", "öffne das Spiel Portal 2",
        "ouvre le jeu Portal 2", "abre el juego Portal 2", "apri il gioco Portal 2", "abre o jogo Portal 2")),
    ("url", "browser", {"action": "open_url", "value": "https://example.com/docs"}, (
        "открой https://example.com/docs", "open https://example.com/docs", "öffne https://example.com/docs",
        "ouvre https://example.com/docs", "abre https://example.com/docs", "apri https://example.com/docs", "abre https://example.com/docs")),
    ("screenshot", "screen", {"action": "capture"}, (
        "сделай скриншот", "take a screenshot", "mache einen Screenshot", "fais une capture d'écran",
        "haz una captura de pantalla", "fai uno screenshot", "faz uma captura de ecrã")),
    ("clipboard", "clipboard", {"action": "clear"}, (
        "очисти буфер обмена", "clear clipboard", "leere die Zwischenablage", "vide le presse-papiers",
        "vacía el portapapeles", "svuota gli appunti", "limpa a área de transferência")),
    ("literal_title", "desktop", {"action": "open", "hint": "file", "query": "Don't Stop.txt"}, (
        "Не мог бы ты открыть файл Don't Stop.txt", "Could you open the file Don't Stop.txt",
        "Bitte öffne die Datei Don't Stop.txt", "Peux-tu ouvrir le fichier Don't Stop.txt",
        "Podrías abrir el archivo Don't Stop.txt", "Puoi aprire il file Don't Stop.txt", "Poderia abrir o arquivo Don't Stop.txt")),
    ("negated_app", None, {}, (
        "не открывай приложение Steam", "don't open application Steam", "öffne das Programm Steam nicht",
        "n'ouvre pas l'application Steam", "no abras la aplicación Steam", "non aprire l'applicazione Steam", "não abra o aplicativo Steam")),
    ("negated_url", None, {}, (
        "не открывай https://example.com/docs", "do not open https://example.com/docs", "öffne https://example.com/docs nicht",
        "n'ouvre pas https://example.com/docs", "no abras https://example.com/docs", "non aprire https://example.com/docs", "não abra https://example.com/docs")),
    ("missing_file", None, {}, (
        "открой файл", "open the file", "öffne die Datei", "ouvre le fichier", "abre el archivo", "apri il file", "abre o ficheiro")),
    ("missing_app", None, {}, (
        "открой приложение", "open application", "öffne das Programm", "ouvre l'application", "abre la aplicación", "apri l'applicazione", "abre o aplicativo")),
    ("unknown", None, {}, (
        "Что такое энтропия?", "What is entropy?", "Was ist Entropie?", "Qu'est-ce que l'entropie ?",
        "¿Qué es la entropía?", "Che cos'è l'entropia?", "O que é entropia?")),
    ("ambiguous", None, {}, (
        "сделай это", "do that", "mach das", "fais cela", "haz eso", "fai quello", "faz isso")),
)


def distribution(values):
    items = sorted(float(value) for value in values)
    if not items:
        return {"n": 0, "p50": None, "p95": None, "p99": None}
    def quantile(q):
        rank = (len(items) - 1) * q
        low = int(rank)
        high = min(low + 1, len(items) - 1)
        return round(items[low] + (items[high] - items[low]) * (rank - low), 6)
    return {"n": len(items), "p50": quantile(.50), "p95": quantile(.95), "p99": quantile(.99)}


def fingerprint(root: Path):
    paths = list((root / "jarvis/runtime").glob("*.py"))
    paths += list((root / "jarvis/routing").glob("*.py"))
    paths += list((root / "jarvis/locales/commands").glob("*.json"))
    paths += list((root / "config/atlas2").rglob("*"))
    paths += list((root / "config/canonical_phrases").glob("*.json"))
    paths += list((root / "config/command_variants").glob("*.json"))
    paths += [root / "config/phrase_atlas.json", root / "jarvis/language.py", root / "jarvis/tools/argument_validation.py"]
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        if path.is_file() and path.suffix in {".json", ".py", ".npy", ".txt"}:
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    catalogue = root / "tests/fixtures/command_catalogue.json"
    if catalogue.is_file():
        digest.update(b"tests/fixtures/command_catalogue.json")
        digest.update(catalogue.read_bytes())
    return digest.hexdigest()


def environment():
    result = {"platform": platform.platform(), "machine": platform.machine(), "python": sys.version,
              "logical_cpus": os.cpu_count(), "blas_threads": 1, "physical_cpu": platform.processor()}
    try:
        model = next(line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                     if line.startswith("model name"))
        result["physical_cpu"] = model
        result["cgroup_cpu_max"] = Path("/sys/fs/cgroup/cpu.max").read_text().strip()
        result["load_average"] = list(os.getloadavg())
    except (OSError, StopIteration, AttributeError):
        pass
    return result


def correct_route(case_id, tool, expected_args, result):
    if tool is None:
        return result is None
    if result is None:
        return False
    # Both existing public tools open a literal file. A new exact template may
    # select filesystem.open while the mature router selects desktop.open.
    # Preserve the target comparison rather than penalizing equivalent owners.
    if case_id in {"file", "literal_title"} and result["tool"] == "filesystem":
        return (result["arguments"].get("action") == "open"
                and str(result["arguments"].get("source", "")).casefold() == expected_args["query"].casefold())
    return result["tool"] == tool and all(
        str(result["arguments"].get(k, "")).casefold() == str(value).casefold()
        for k, value in expected_args.items())


def worker(args):
    root = Path(args.worker_root).resolve()
    before = fingerprint(root)
    sys.path.insert(0, str(root))
    # Classification cannot contact network, spawn processes, or execute tools.
    # An audit hook fails the run if an imported dependency attempts either.
    blocked = []
    def audit(event, values):
        if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
            blocked.append(event)
            raise RuntimeError("Routing benchmark blocked side effect: " + event)
    sys.addaudithook(audit)
    started = time.perf_counter()
    from jarvis.runtime.router import HybridRouter
    from jarvis.runtime.atlas2 import PhraseAtlas2
    from jarvis.routing.domain_dispatcher import DomainDispatcher
    from jarvis.runtime.multilingual_commands import normalize_multilingual_command, command_language_violation
    from jarvis.runtime.text_normalization import normalize_spoken_command
    from jarvis.runtime.phrase_atlas import normalize_phrase_atlas
    # Reproduce only the pure, stateless guards that precede routing in current
    # Agent. No dialogue history or confidence model is fabricated for baseline.
    try:
        from jarvis.runtime.natural_commands import negated_action, missing_target
    except ImportError:
        negated_action = missing_target = None
    try:
        from jarvis.runtime.feature_commands import parse_feature_command
        from jarvis.runtime.canonical_commands import parse_canonical_command
        catalogue = json.loads((root / "tests/fixtures/command_catalogue.json").read_text(encoding="utf-8"))
        schemas = {item["tool"]: item["schema"] for item in catalogue["tools"]}
    except ImportError:
        parse_feature_command = parse_canonical_command = None
        schemas = {}
    import_ms = (time.perf_counter() - started) * 1000
    class Config:
        def get(self, key, default=None):
            return args.language if key == "app.language" else default
    started = time.perf_counter()
    router = HybridRouter()
    atlas = PhraseAtlas2(Config(), context_memory=None)
    dispatcher = DomainDispatcher(Config(), router=router, atlas2=atlas)
    construct_ms = (time.perf_counter() - started) * 1000
    def classify(raw):
        if negated_action and negated_action(raw, args.language):
            return None, "preflight.negated"
        if missing_target and missing_target(raw, args.language):
            return None, "preflight.missing_target"
        if command_language_violation(raw, args.language)[0]:
            return None, "preflight.language"
        if parse_feature_command:
            feature = parse_feature_command(raw, args.language)
            if feature:
                return {"tool": feature[0], "arguments": feature[1]}, "native.feature"
            native = parse_canonical_command(raw, args.language, schemas.__getitem__)
            if native:
                if native.error:
                    return None, "native.clarify." + native.error
                return {"tool": native.tool, "arguments": native.arguments}, "native.canonical"
        canonical = normalize_phrase_atlas(normalize_spoken_command(normalize_multilingual_command(raw, args.language)))
        decision = dispatcher.resolve(canonical, semantic_text=raw)
        routed = decision.routed
        return ({"tool": routed.tool, "arguments": dict(routed.arguments)} if routed else None), decision.source
    lang_i = LANGUAGES.index(args.language)
    started = time.perf_counter()
    classify(CORPUS[0][3][lang_i])
    first_route_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    exact = atlas._exact_index(args.language)
    exact_init_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    rows, matrix = atlas._semantic_index(args.language)
    semantic_init_ms = (time.perf_counter() - started) * 1000
    cases = []
    for case_id, tool, expected_args, texts in CORPUS:
        raw = texts[lang_i]
        result, source = classify(raw)
        ok = correct_route(case_id, tool, expected_args, result)
        cases.append({"id": case_id, "text": raw, "expected": {"tool": tool, "arguments": expected_args} if tool else None,
                      "actual": result, "source": source, "correct": ok, "positive": tool is not None,
                      "samples_ms": [], "cpu_samples_ms": []})
    # Rotate phrase order to avoid the same route always following the same route.
    # Repeating the literal commands measures a warm parser, not memoized answers.
    for iteration in range(args.iterations):
        for offset in range(len(cases)):
            case = cases[(iteration + offset) % len(cases)]
            cpu_started = time.process_time_ns()
            started = time.perf_counter_ns()
            classify(case["text"])
            case["samples_ms"].append((time.perf_counter_ns() - started) / 1e6)
            case["cpu_samples_ms"].append((time.process_time_ns() - cpu_started) / 1e6)
    # Measure the selected locale's hash table separately from the whole parser.
    exact_samples = []
    exact_keys = tuple(exact)[:64]
    for _ in range(args.iterations):
        for key in exact_keys:
            started = time.perf_counter_ns()
            exact.get(key)
            exact_samples.append((time.perf_counter_ns() - started) / 1e6)
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_bytes = int(peak if sys.platform == "darwin" else peak * 1024)
        peak_source = "resource.getrusage.ru_maxrss"
    except ImportError:
        import psutil
        mem = psutil.Process().memory_info()
        peak_bytes = int(getattr(mem, "peak_wset", mem.rss))
        peak_source = "psutil.peak_wset" if hasattr(mem, "peak_wset") else "psutil.current_rss_not_peak"
    after = fingerprint(root)
    output = {"language": args.language, "source_sha256": before, "source_stable": before == after,
              "import_ms": import_ms, "construct_ms": construct_ms, "first_route_ms": first_route_ms,
              "exact_index_init_ms": exact_init_ms, "semantic_index_init_ms": semantic_init_ms,
              "exact_rows": len(exact), "semantic_rows": len(rows), "semantic_matrix_bytes": matrix.nbytes,
              "peak_process_rss_bytes": peak_bytes, "peak_rss_source": peak_source,
              "exact_hash_lookup_ms": distribution(exact_samples), "cases": cases,
              "blocked_side_effect_attempts": blocked, "actions_executed": 0,
              "guards": {"negated_action": bool(negated_action), "missing_target": bool(missing_target)},
              "native_parser": bool(parse_canonical_command)}
    print(json.dumps(output, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, help="Baseline project or _internal directory")
    parser.add_argument("--current", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--languages", default=",".join(LANGUAGES))
    parser.add_argument("--reuse-baseline", type=Path, help="Reuse verified unchanged baseline trials from a previous report; rerun current only")
    parser.add_argument("--worker-root", help=argparse.SUPPRESS)
    parser.add_argument("--language", choices=LANGUAGES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_root:
        worker(args)
        return
    if not args.baseline or not args.output:
        parser.error("--baseline and --output are required")
    if args.iterations < 1 or args.trials < 1:
        parser.error("--iterations and --trials must be positive")
    languages = args.languages.split(",")
    if any(code not in LANGUAGES for code in languages):
        parser.error("unsupported language")
    roots = {"baseline": args.baseline.resolve(), "current": args.current.resolve()}
    roots = {label: (root / "_internal" if (root / "_internal").is_dir() else root) for label, root in roots.items()}
    before = {label: fingerprint(root) for label, root in roots.items()}
    initial_environment = environment()
    trials = []
    reused = None
    reuse_metadata = None
    if args.reuse_baseline:
        previous_bytes = args.reuse_baseline.read_bytes()
        reused = json.loads(previous_bytes)
        if reused["method"]["iterations_per_phrase_per_trial"] != args.iterations or reused["method"]["trials"] != args.trials:
            parser.error("reused baseline iterations/trial count differs")
        for key in ("platform", "machine", "python", "physical_cpu", "logical_cpus", "blas_threads"):
            if reused["environment"].get(key) != initial_environment.get(key):
                parser.error("reused baseline environment differs: " + key)
        baseline_trials = [row for row in reused["trials"] if row["build"] == "baseline" and row["language"] in languages]
        if len(baseline_trials) != args.trials * len(languages):
            parser.error("reused report does not contain all baseline trials")
        for trial in baseline_trials:
            if not trial["source_stable"] or trial["source_sha256"] != before["baseline"] or trial["blocked_side_effect_attempts"]:
                parser.error("baseline source/side-effect stability check failed")
            expected = [{"id": row[0], "text": row[3][LANGUAGES.index(trial["language"])],
                         "expected": {"tool": row[1], "arguments": row[2]} if row[1] else None} for row in CORPUS]
            observed = [{key: case[key] for key in ("id", "text", "expected")} for case in trial["cases"]]
            if expected != observed:
                parser.error("fixed corpus differs from reused baseline")
        trials.extend(baseline_trials)
        reuse_metadata = {"report_sha256": hashlib.sha256(previous_bytes).hexdigest(), "captured_at": reused["created_at"],
                          "environment": reused["environment"], "note": "Baseline trials and pooled metrics retained unchanged; final current trials captured afterward on the same host/runtime. Final comparison is not temporally interleaved."}
    for trial in range(args.trials):
        for language in languages:
            # Alternate build order to reduce systematic filesystem-cache bias.
            order = list(roots) if trial % 2 == 0 else list(reversed(roots))
            if reused:
                order = ["current"]
            for build in order:
                with tempfile.TemporaryDirectory(prefix="neo-route-bench-") as profile:
                    env = dict(os.environ)
                    env.update({"APPDATA": profile, "LOCALAPPDATA": profile, "XDG_CACHE_HOME": profile,
                                "PYTHONPATH": str(roots[build]), "PYTHONDONTWRITEBYTECODE": "1",
                                "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
                    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker-root", str(roots[build]),
                        "--language", language, "--iterations", str(args.iterations)], env=env, cwd=profile,
                        check=True, capture_output=True, text=True, timeout=180)
                    result = json.loads(completed.stdout)
                    result.update({"build": build, "trial": trial + 1})
                    trials.append(result)
                    print(f"{build} {language} trial {trial + 1}: {sum(c['correct'] for c in result['cases'])}/{len(CORPUS)} correct", file=sys.stderr)
    after = {label: fingerprint(root) for label, root in roots.items()}
    summary = []
    for language in languages:
        language_trials = [trial for trial in trials if trial["language"] == language]
        common = {case["id"] for case in language_trials[0]["cases"] if case["positive"]}
        for trial in language_trials:
            common &= {case["id"] for case in trial["cases"] if case["positive"] and case["correct"]}
        for build in roots:
            selected = [trial for trial in language_trials if trial["build"] == build]
            if build == "baseline" and reused:
                saved_summary = next(row for row in reused["summary"] if row["build"] == build and row["language"] == language)
                if saved_summary["common_correct_ids"] != sorted(common):
                    parser.error("baseline common-case intersection changed; perform a full rerun")
                summary.append(saved_summary)
                continue
            all_cases = selected[0]["cases"]
            counts = Counter()
            for case in all_cases:
                counts["positive" if case["positive"] else "negative"] += 1
                counts["correct"] += case["correct"]
                counts["positive_correct"] += case["positive"] and case["correct"]
                counts["false_route"] += not case["positive"] and case["actual"] is not None
                counts["claimed"] += case["actual"] is not None
            summary.append({"language": language, "build": build, "corpus_counts": dict(counts),
                "claimed_route_precision": counts["positive_correct"] / counts["claimed"] if counts["claimed"] else None,
                "common_correct_ids": sorted(common),
                "warm_common_commands_ms": distribution([v for trial in selected for case in trial["cases"] if case["id"] in common for v in case["samples_ms"]]),
                "warm_common_commands_cpu_ms": distribution([v for trial in selected for case in trial["cases"] if case["id"] in common for v in case["cpu_samples_ms"]]),
                "warm_all_positive_commands_ms": distribution([v for trial in selected for case in trial["cases"] if case["positive"] for v in case["samples_ms"]]),
                "cold_import_ms": distribution([trial["import_ms"] for trial in selected]),
                "cold_ready_with_indexes_ms": distribution([sum(trial[key] for key in ("import_ms", "construct_ms", "first_route_ms", "exact_index_init_ms", "semantic_index_init_ms")) for trial in selected]),
                "cold_first_route_ms": distribution([trial["first_route_ms"] for trial in selected]),
                "cold_exact_index_ms": distribution([trial["exact_index_init_ms"] for trial in selected]),
                "cold_semantic_index_ms": distribution([trial["semantic_index_init_ms"] for trial in selected]),
                "peak_process_rss_bytes": max(trial["peak_process_rss_bytes"] for trial in selected),
                "exact_rows": selected[0]["exact_rows"], "semantic_rows": selected[0]["semantic_rows"],
                "failures": [{k: c[k] for k in ("id", "text", "expected", "actual", "source")} for c in all_cases if not c["correct"]]})
    # Keep per-case percentiles and classification evidence, not thousands of raw
    # repeat samples, in the deliverable. Summary percentiles pooled above.
    for trial in trials:
        for case in trial["cases"]:
            if "samples_ms" in case:
                case["warm_ms"] = distribution(case.pop("samples_ms"))
                case["cpu_ms"] = distribution(case.pop("cpu_samples_ms"))
    stable = before == after and all(trial["source_stable"] and trial["source_sha256"] == before[trial["build"]] for trial in trials)
    report = {"schema": 1, "created_at": datetime.now(timezone.utc).isoformat(), "valid_stable_sources": stable,
              "environment": initial_environment,
              "benchmark_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "reused_baseline": reuse_metadata,
              "method": {"trials": args.trials, "iterations_per_phrase_per_trial": args.iterations,
                         "languages": languages, "cases_per_language": len(CORPUS), "fresh_process_each_trial": True,
                         "context_memory": None, "network_and_subprocess_audit_guard": True,
                         "actions_executed": 0, "new_feature_and_canonical_parser_included": True,
                         "cold_means": "fresh Python process; host filesystem cache is not flushed",
                         "accuracy_scope": "fixed 8 positive + 6 negative/ambiguous native phrases per language; no execution or full-Agent dialogue test",
                         "timing_scope": "stateless negation/missing-target guards when available, selected-language gate, native feature/canonical parser when present, then three normalizers and DomainDispatcher/PhraseAtlas2",
                         "memory_scope": "peak entire Python subprocess with selected locale exact and semantic indexes deliberately materialized"},
              "observed_current_gates": {
                  "fixed_corpus_all_correct": all(row["corpus_counts"]["correct"] == len(CORPUS) for row in summary if row["build"] == "current"),
                  "no_false_routes_on_fixed_negatives": all(not row["corpus_counts"]["false_route"] for row in summary if row["build"] == "current"),
                  "PERF06_small_warm_positive_sample_p95_within_30ms": all(row["warm_all_positive_commands_ms"]["p95"] <= 30 for row in summary if row["build"] == "current"),
                  "full_Windows_performance_gate": "not measured; requires reference desktop, audio and UI workload"},
              "source_sha256_before": before, "source_sha256_after": after, "summary": summary, "trials": trials}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not stable:
        raise SystemExit("INVALID: routing sources changed during the benchmark; rerun after edits stop")


if __name__ == "__main__":
    main()
