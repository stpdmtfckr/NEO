# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import sys

from jarvis.runtime.launcher_console import launcher_console


def main() -> int:
    action = str(sys.argv[1] if len(sys.argv) > 1 else "show").strip().lower()
    console = launcher_console()
    if action == "hide":
        console.hide()
    elif action == "toggle":
        console.toggle()
    else:
        console.show(activate=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
