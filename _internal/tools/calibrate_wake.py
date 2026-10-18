# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Calibrate the wake threshold against your own data.

    python3 tools/calibrate_wake.py \\
        --settings "_internal/config/default_settings.json" \\
        --phrase джарвис

Reads ``wake_word.aliases`` as the positive set -- these are real ASR
renderings you have already observed -- scores them plus an ordinary-word
negative corpus, and reports the separating threshold and which aliases can be
deleted outright.

Add your own negatives with ``--negatives file.txt``.  The highest-value
samples are the ASR transcripts of utterances where NEO woke and should not
have; the shipped build logs those lines ("Wake rejected; ... asr=[...]") and
then discards them.  ``--from-log neo_jarvis.log`` harvests them automatically.
"""

import argparse
import ast
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.wake.v2 import CONFUSABLE_WORDS, migrate_alias_list, phonetic_score

DEFAULT_NEGATIVES = """
включи выключи открой закрой поставь следующий предыдущий громче тише музыку
трек песню дискорд браузер ютуб погода время таймер заметку скриншот компьютер
система процессор память температура сколько какой какая где когда привет пока
спасибо хорошо ладно давай стоп хватит сервис работа задача поехали ребята
сейчас потом завтра сегодня вчера здравствуй здорово
service jeremy janice doris jarhead harvest garage charles george sergey
there it is choice voice noise horse house mouse first worst nurse purse
""".split()

_ASR_RE = re.compile(r"asr=(\[[^\]]*\])")


def harvest_from_log(path: Path) -> list[str]:
    """Pull ``asr=[...]`` payloads out of a neo_jarvis.log."""
    out: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for match in _ASR_RE.finditer(text):
        try:
            for item in ast.literal_eval(match.group(1)):
                item = str(item).strip().strip(".!?")
                if item:
                    out.append(item)
        except Exception:
            continue
    return out


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    idx = max(0, min(len(values) - 1, int(len(values) * q)))
    return values[idx]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", required=True, help="path to default_settings.json")
    ap.add_argument("--phrase", default="джарвис")
    ap.add_argument("--negatives", help="newline-separated words that must NOT wake")
    ap.add_argument("--from-log", help="neo_jarvis.log to harvest rejected ASR text from")
    ap.add_argument("--fp-weight", type=float, default=4.0,
                    help="how much worse a false accept is than a miss")
    args = ap.parse_args()

    data = json.loads(Path(args.settings).read_text(encoding="utf-8"))
    aliases = data.get("wake_word", {}).get("aliases", []) or []
    positives = sorted({str(a).strip().strip(".!?") for a in aliases if str(a).strip()})
    positives = [p for p in positives if p.casefold() not in CONFUSABLE_WORDS]

    negatives = list(DEFAULT_NEGATIVES)
    if args.negatives:
        negatives += [
            line.strip()
            for line in Path(args.negatives).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    if args.from_log:
        harvested = harvest_from_log(Path(args.from_log))
        # A harvested line that IS an alias is a positive, not a negative.
        alias_folds = {p.casefold() for p in positives}
        negatives += [h for h in harvested if h.casefold() not in alias_folds]
        print(f"harvested {len(harvested)} ASR lines from log")

    pos = sorted(phonetic_score(p, args.phrase) for p in positives)
    neg = sorted(phonetic_score(n, args.phrase) for n in negatives)

    print(f"positives: {len(pos)}  median={percentile(pos, 0.5):.3f}")
    if neg:
        worst = max(negatives, key=lambda n: phonetic_score(n, args.phrase))
        print(f"negatives: {len(neg)}  max={neg[-1]:.3f}  (worst: {worst!r})")
    print()

    # Two candidate thresholds are reported, because "how bad is a false wake"
    # is a product decision, not a statistical one:
    #
    #   zero_fa  -- the lowest threshold that admits NO negative at all. This is
    #               the right default: a false wake interrupts the user, opens
    #               the mic and can fire a command, whereas a miss costs one
    #               repeat. Percentages hide single events, so count exactly.
    #   weighted -- the fp-weighted optimum, if you would rather trade a handful
    #               of false wakes for recall.
    weighted = (-9.9, 0.62)
    zero_fa: float | None = None
    print(f"{'thr':>6} {'recall':>9} {'false_accepts':>15}")
    for step in range(50, 96, 2):
        t = step / 100
        hits = sum(1 for s in pos if s >= t)
        fa = sum(1 for s in neg if s >= t)
        recall = hits / max(len(pos), 1)
        far = fa / max(len(neg), 1)
        score = recall - args.fp_weight * far
        if score > weighted[0]:
            weighted = (score, t)
        if fa == 0 and zero_fa is None:
            zero_fa = t
        print(f"{t:6.2f} {recall:9.1%} {fa:9d} / {len(neg):<5d}")

    recommended = zero_fa if zero_fa is not None else weighted[1]
    print(f"\nrecommended base_threshold = {recommended:.2f}  "
          f"(lowest threshold with zero false accepts)")
    print(f"alternative (fp_weight={args.fp_weight})  = {weighted[1]:.2f}")
    best = (weighted[0], recommended)

    report = migrate_alias_list(aliases, args.phrase, threshold=best[1])
    print(f"\nalias list: {report['total_unique']} unique entries")
    print(f"  covered by phonetics : {report['covered_by_phonetics']} "
          f"({report['reduction_percent']}%) -- safe to delete")
    print(f"  DANGEROUS (ordinary words -- remove): {report['should_be_removed']}")
    needed = report["still_needed"]
    print(f"  keep as WakeConfig.exceptions ({len(needed)}) -- paste this:")
    print()
    print("    WakeConfig(")
    print(f"        phrase={args.phrase!r},")
    print(f"        base_threshold={recommended:.2f},")
    print("        exceptions=(")
    for alias in needed:
        print(f"            {alias!r},")
    print("        ),")
    print("    )")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
