"""Create an honest environment-specific hash lock from pip's installation reports."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import time


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def build_lock(runtime: Path) -> dict:
    candidates = {}
    for path in (runtime / ".neo_install_reports").glob("*.json"):
        report = json.loads(path.read_text(encoding="utf-8-sig"))
        for item in report.get("install", []):
            metadata = item.get("metadata", {})
            name, version = metadata.get("name"), metadata.get("version")
            archive = item.get("download_info", {}).get("archive_info", {})
            digest = archive.get("hashes", {}).get("sha256", "")
            if not digest and str(archive.get("hash", "")).startswith("sha256="):
                digest = archive["hash"].split("=", 1)[1]
            if name and version and re.fullmatch("[0-9a-fA-F]{64}", digest):
                candidates[(canonical(name), version)] = digest.lower()
    installed = {canonical(dist.metadata["Name"]): dist.version for dist in importlib.metadata.distributions() if dist.metadata.get("Name")}
    lines, missing = [], []
    for name, version in sorted(installed.items()):
        digest = candidates.get((name, version))
        if digest:
            lines.append(f"{name}=={version} --hash=sha256:{digest}")
        else:
            missing.append(f"{name}=={version}")
    result = {"python": sys.version, "created_at": time.time(), "complete": not missing, "package_count": len(installed), "missing_hashes": missing, "requirements_sha256": hashlib.sha256((Path(__file__).parent / "requirements.txt").read_bytes()).hexdigest(), "scope": "exact versions/artifact hashes of this installed Windows environment; not a claim of clean-install verification"}
    (runtime / "runtime_lock_report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    destination = runtime / "requirements.lock.txt"
    if not missing:
        destination.write_text("# NEO verified-installation artifact lock; generated on the target computer.\n" + "\n".join(lines) + "\n", encoding="utf-8")
    else:
        destination.unlink(missing_ok=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args()
    if args.runtime.resolve() != Path(sys.prefix).resolve():
        print("Refusing to record a lock for a different interpreter environment.")
        return 2
    result = build_lock(args.runtime)
    print(json.dumps(result, indent=2))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
