"""Read-only caption inventory and catalogue checks; no windows/services started.

The AST inventory distinguishes literal captions from dynamic text. It does not
claim that every executed branch or external provider message was reviewed.
"""
from __future__ import annotations
import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from jarvis.i18n import GENERIC_HELP, tr

CODES = ("ru", "en", "de", "fr", "es", "it", "pt")
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
METHODS = {"setText": (0,), "setWindowTitle": (0,), "setTitle": (0,), "setPlaceholderText": (0,),
           "setToolTip": (0,), "setStatusTip": (0,), "addAction": (0,), "addTab": (1,),
           "addRow": (0,), "addButton": (0,), "addItem": (0,), "insertItem": (1,)}
METHODS.update({"_help": (0,), "_add_field": (1, 3), "_status_text": (1,)})
CONSTRUCTORS = {"QLabel", "QPushButton", "QCheckBox", "QRadioButton", "QGroupBox", "QAction", "QMenu"}
TRANSLATORS = {"tr", "L", "_t", "_localize_text", "localize_runtime_text", "_localize_owner"}


def name(node):
    return node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""


def inspect_expression(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, "literal"
    if isinstance(node, ast.Call):
        fn = name(node.func)
        if fn in TRANSLATORS and node.args:
            index = 1 if fn == "_localize_owner" else 0
            if len(node.args) > index:
                text, kind = inspect_expression(node.args[index])
                return text, "translated_" + kind
        if fn in {"dt", "word", "standard_text"} and node.args:
            key, kind = inspect_expression(node.args[0])
            return key, "catalogue_key" if "literal" in kind else "dynamic_catalogue_key"
        if fn == "format" and isinstance(node.func, ast.Attribute):
            text, _ = inspect_expression(node.func.value)
            return text, "dynamic_format"
    if isinstance(node, ast.JoinedStr):
        text = "".join(x.value if isinstance(x, ast.Constant) and isinstance(x.value, str) else "{payload}" for x in node.values)
        return text, "dynamic_fstring"
    return ast.unparse(node), "dynamic_expression"


def audit():
    catalogues = {}
    for code in CODES:
        path = ROOT / "jarvis/locales/ui" / (code + ".json")
        catalogues[code] = json.loads(path.read_text(encoding="utf-8"))["strings"]
    union = set().union(*(set(rows) for rows in catalogues.values()))
    checks = {}
    for code, rows in catalogues.items():
        checks[code] = {"count": len(rows), "missing_keys": sorted(union - set(rows)),
                        "blank_values": [k for k, v in rows.items() if not str(v).strip()],
                        "generic_values": [k for k, v in rows.items() if v in GENERIC_HELP.values()],
                        "cyrillic_values": [k for k, v in rows.items() if code != "ru" and CYRILLIC.search(v)]}
    occurrences = []
    for path in sorted((ROOT / "jarvis/ui").glob("*.py")):
        if path.name in {"qt_localization.py", "desktop_strings.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            method = name(node.func)
            positions = METHODS.get(method, (0,) if method in CONSTRUCTORS else ())
            if method == "localized_message_box":
                positions = (2, 3)
            if method in {"question", "warning", "critical", "information"} and "MessageBox" in ast.unparse(node.func):
                positions = (1, 2)
            if method in {"addItems", "setHorizontalHeaderLabels", "setVerticalHeaderLabels"} and node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
                # Qt's batch caption setters also contain visible labels.
                candidates = [(i, item) for i, item in enumerate(node.args[0].elts)]
            else:
                candidates = [(pos, node.args[pos]) for pos in positions if len(node.args) > pos]
            for pos, expression in candidates:
                source, kind = inspect_expression(expression)
                # A widget argument in overloaded addRow/addAction is not text.
                if kind == "dynamic_expression" and not isinstance(expression, (ast.JoinedStr, ast.BinOp, ast.Call)):
                    continue
                row = {"file": str(path.relative_to(ROOT)), "line": node.lineno, "call": method,
                       "argument": pos, "kind": kind, "source": source}
                if kind in {"literal", "translated_literal"} and source.strip():
                    translated = {code: tr(source, code) for code in CODES}
                    row["resolved"] = translated
                    row["unresolved_cyrillic_languages"] = [code for code in CODES[1:] if CYRILLIC.search(translated[code])]
                    row["generic_languages"] = [code for code in CODES[1:] if translated[code] in GENERIC_HELP.values()]
                    row["english_source_candidate"] = bool(not CYRILLIC.search(source) and re.search(r"[A-Za-z]{3,}", source))
                occurrences.append(row)
    return {"schema": 1, "method": "static AST of jarvis/ui/*.py caption calls plus exact runtime tr resolution; no Agent/audio/OS/provider execution",
            "limits": ["Dynamic expressions require branch-specific runtime tests; they are listed, not silently certified.",
                       "Latin text may be a brand, format, key, user value or actual untranslated caption; English candidates require review.",
                       "Native-language correctness is not proven by script/alphabet checks."],
            "catalogues": checks, "kind_counts": dict(Counter(x["kind"] for x in occurrences)),
            "caption_occurrences": occurrences}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"catalogues": report["catalogues"], "kind_counts": report["kind_counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
