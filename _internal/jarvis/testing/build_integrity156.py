"""Record source fingerprints; optionally verify legacy developer manifests.

Current packages carry no separate manifest. The test runner compares snapshots
before and after its checks; release packaging verifies ZIP contents externally.
Legacy manifests are still understood for developer compatibility.
Only distribution code/data are inspected. User profiles and recordings are never
read, hashed or included in diagnostics.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re

MANIFEST = "RELEASE_MANIFEST.json"
EXCLUDED = {"__pycache__", ".pytest_cache", ".ruff_cache", "private_workers", "ui_private"}
INTERNAL_SUFFIXES = {".py", ".json", ".npy", ".version", ".ps1", ".cmd", ".txt"}


def source_snapshot(project: Path) -> dict[str, str]:
    project = Path(project).resolve()
    paths = [p for p in project.iterdir() if p.is_file() and p.suffix.lower() in {".cmd", ".exe"}]
    internal = project / "_internal"
    if internal.is_dir():
        paths.extend(p for p in internal.rglob("*") if p.is_file()
                     and not EXCLUDED.intersection(p.relative_to(internal).parts)
                     and p.suffix.lower() in INTERNAL_SUFFIXES)
    result = {}
    for path in sorted(paths):
        if path.is_symlink():
            raise ValueError("Linked release source: " + path.relative_to(project).as_posix())
        result[path.relative_to(project).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def write_manifest(project: Path) -> Path:
    project = Path(project).resolve()
    files = source_snapshot(project)
    if not files or "_internal/VERSION.txt" not in files:
        raise ValueError("Not a complete NEO project")
    value = {"schema": 1, "build": (project / "_internal/VERSION.txt").read_text(encoding="utf-8").splitlines()[0],
             "algorithm": "sha256", "scope": "release source, phrase data, launchers; excludes user profile",
             "files": files}
    destination = project / MANIFEST
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(destination)
    return destination


def verify_release(project: Path, *, snapshot: dict[str, str] | None = None) -> dict:
    project = Path(project).resolve()
    manifest = project / MANIFEST
    if not manifest.exists():
        return {"status": "NOT_PRESENT", "reason": "No optional legacy release manifest"}
    try:
        if manifest.is_symlink() or manifest.stat().st_size > 4_000_000:
            raise ValueError("Invalid release manifest file")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        files = data.get("files", {})
        if data.get("schema") != 1 or data.get("algorithm") != "sha256" or not isinstance(files, dict) or not files:
            raise ValueError("Invalid release manifest schema")
        for name, digest in files.items():
            path = PurePosixPath(name)
            if (path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name
                    or not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest)):
                raise ValueError("Invalid release manifest entry")
        actual = source_snapshot(project) if snapshot is None else snapshot
        missing = sorted(set(files) - set(actual))
        extra = sorted(set(actual) - set(files))
        changed = sorted(key for key in set(files) & set(actual) if files[key] != actual[key])
        version = (project / "_internal/VERSION.txt").read_text(encoding="utf-8").splitlines()[0]
        okay = not (missing or extra or changed) and version == data.get("build")
        return {"status": "PASS" if okay else "MISMATCH", "build": version,
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "files_checked": len(actual), "missing": missing, "unexpected": extra, "changed": changed,
                "reason": "" if okay else "Mixed or incomplete release files; extract into a new folder"}
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        return {"status": "INVALID", "reason": str(exc)[:300]}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--write", action="store_true", help="Release packager only, after final source freeze")
    args = parser.parse_args()
    if args.write:
        write_manifest(args.project)
    result = verify_release(args.project)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
