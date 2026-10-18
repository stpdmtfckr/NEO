# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import sys


def main() -> int:
    if sys.platform != "win32":
        print("Yandex Music probe skipped: not Windows")
        return 0
    try:
        from jarvis.music_app_control import find_yandex_music_install
        item = find_yandex_music_install()
        if item:
            print("Yandex Music desktop app detected: " + str(item.get("name") or item.get("app_id") or item.get("path") or "yes"))
            return 0
        print("Yandex Music desktop app not detected")
        return 3
    except Exception as exc:
        print("Yandex Music probe error: " + str(exc))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
