# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import argparse
import sys

from jarvis.runtime.youtube_runtime import ensure_js_runtime, find_js_runtime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            kind, path = find_js_runtime()
            if not path:
                return 2
            if not args.quiet:
                print(f"YouTube audio runtime ready: {kind} {path}")
            return 0
        kind, path = ensure_js_runtime(progress=not args.quiet)
        if not args.quiet:
            print(f"YouTube audio runtime ready: {kind} {path}")
        return 0
    except Exception as exc:
        if not args.quiet:
            print(f"YouTube audio runtime preparation failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
