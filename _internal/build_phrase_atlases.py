"""Maintainer build: expand native task constructions and precompute CPU indexes.

No model is loaded. Run from a development environment after editing locale JSON.
The generated cache contains only public command examples, never user history.
"""
from __future__ import annotations
import json
from pathlib import Path
import zlib
import hashlib
import re
import unicodedata
from types import SimpleNamespace
from jarvis.runtime.natural_commands import LANGUAGES, TASK_FRAMES, norm
from jarvis.runtime.feature_commands import exact_phrases
from jarvis.runtime.atlas2 import PhraseAtlas2


def build() -> dict:
    root = Path(__file__).resolve().parent
    result = {}
    foreign_hashes = {}
    canonical = {}
    variants = {}
    from jarvis.routing.intent_core import assistive_groups
    for group in assistive_groups():
        for code, phrases in group["phrases"].items():
            variants.setdefault(group["tool"] + "." + group["action"], {}).setdefault(code, []).extend(phrases)
    for pack in sorted((root / "config" / "command_variants").glob("*.json")):
        for action, languages in json.loads(pack.read_text(encoding="utf-8")).items():
            for code, phrases in languages.items():
                variants.setdefault(action, {}).setdefault(code, []).extend(phrases)
    for pack in sorted((root / "config" / "canonical_phrases").glob("*.json")):
        rows = json.loads(pack.read_text(encoding="utf-8"))
        for action, values in rows.items():
            if not isinstance(values, list) or len(values) != 7 or not all(isinstance(v, str) and v.strip() for v in values):
                raise ValueError("Invalid canonical phrase: " + action)
            canonical[action] = values
    for code in LANGUAGES:
        path = root / "jarvis" / "locales" / "commands" / (code + ".json")
        data = json.loads(path.read_text(encoding="utf-8"))
        actions = data["action_phrases"]
        short_pack = json.loads((root / "config" / "short_commands153.json").read_text(encoding="utf-8"))
        for phrase, (tool, action) in short_pack["languages"].get(code, {}).items():
            bucket = actions.setdefault(tool + "." + action, [])
            if phrase not in bucket:
                bucket.append(phrase)
        native = {action: values[LANGUAGES.index(code)] for action, values in canonical.items()}
        for action, phrase in native.items():
            previous = actions.get(action, [])
            # Remove previously generated task wrappers before rebuilding them
            # around the newly reviewed native command, not the legacy wording.
            old = [p for p in previous if not any(norm(p).startswith(norm(frame) + ":") for frame in TASK_FRAMES[code])]
            actions[action] = list(dict.fromkeys([phrase, *variants.get(action, {}).get(code, []), *old]))
        for phrase, (tool, action) in exact_phrases(code).items():
            actions.setdefault(tool + "." + action, [])
            if phrase not in actions[tool + "." + action]:
                actions[tool + "." + action].append(phrase)
        for action, phrases in actions.items():
            if not phrases:
                continue
            seen = {norm(x) for x in phrases}
            native_forms = [native.get(action, phrases[0]), *variants.get(action, {}).get(code, [])]
            for base in native_forms:
                for frame in TASK_FRAMES[code]:
                    phrase = frame + ": " + base
                    if norm(phrase) not in seen:
                        phrases.append(phrase)
                        seen.add(norm(phrase))
        from jarvis.runtime.catalogue_migration152 import migrate
        moved = migrate(actions, code)
        from jarvis.runtime.catalogue_migration157 import migrate_search_heads157
        corrected_search = migrate_search_heads157(actions, code)
        # Migrate auxiliary lists too: otherwise replay supplements removed
        # search-labelled examples back from native_variants.
        variant_rows={action: [phrase, *variants.get(action, {}).get(code, [])] for action, phrase in native.items()}
        migrate(variant_rows, code)
        migrate_search_heads157(variant_rows, code)
        data["action_count"] = len(actions)
        data["natural_expansion_version"] = 3
        data["canonical_phrases"] = native
        data["native_variants"] = variant_rows
        data["semantic_migration152"] = moved
        data["semantic_migration157"] = corrected_search
        data["note"] = "NEO native action examples and indirect task framing. Parameter-bearing examples require slot filling; counts are not execution guarantees."
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for phrases in actions.values():
            for phrase in phrases:
                normalized = unicodedata.normalize("NFKC", str(phrase)).casefold().replace("’", "'")
                normalized = re.sub(r"[^a-zà-öø-ÿа-яё0-9]+", " ", normalized)
                normalized = re.sub(r"\s+", " ", normalized).strip()
                digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
                if code not in foreign_hashes.setdefault(digest, []):
                    foreign_hashes[digest].append(code)
        atlas = PhraseAtlas2(SimpleNamespace(get=lambda key, default=None: default))
        rows = atlas._seed_rows(code)
        # A maximum of six distinct seeds per intent bounds semantic RAM/CPU.
        # All examples remain in the O(1) exact index.
        seen_count = {}
        selected = []
        for row in rows:
            intent = row["intent"]
            if seen_count.get(intent, 0) < 6:
                selected.append(row)
                seen_count[intent] = seen_count.get(intent, 0) + 1
        cache = root / "config" / "atlas2" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        matrix = atlas._vectorizer.encode(row["phrase"] for row in selected)
        import numpy as np
        np.save(cache / f"semantic_{code}.npy", matrix, allow_pickle=False)
        (cache / f"semantic_{code}.rows.json").write_text(json.dumps(selected, ensure_ascii=False), encoding="utf-8")
        (cache / f"semantic_{code}.version").write_text("unicode-v2:" + str(zlib.crc32(path.read_bytes())), encoding="utf-8")
        (cache / f"static_exact_{code}.json").write_text(json.dumps(atlas._exact_index(code), ensure_ascii=False), encoding="utf-8")
        native_counts = [len(values) for values in data["native_variants"].values()]
        result[code] = {"actions": len(actions), "phrases": sum(len(x) for x in actions.values()), "semantic_rows": len(selected), "native_forms": sum(native_counts), "native_min_per_action": min(native_counts), "native_max_per_action": max(native_counts), "legacy_and_task_frames": sum(len(x) for x in actions.values()) - sum(native_counts)}
    (root / "config" / "atlas2" / "foreign_phrase_hashes.json").write_text(json.dumps({"schema_version": 2, "hash_owners": foreign_hashes}, ensure_ascii=False), encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
