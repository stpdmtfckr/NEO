# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

from pathlib import Path

from jarvis.config import ConfigManager


def main() -> int:
    root = Path(__file__).resolve().parent
    config = ConfigManager(root / "config" / "default_settings.json")
    config.reload()
    tg = config.data.setdefault("integrations", {}).setdefault("telegram", {})
    tg["enabled"] = False
    tg["bot_token_encrypted"] = ""
    tg["allowed_user_ids"] = []
    tg["allowed_chat_ids"] = []
    tg["pairing_enabled"] = False
    tg["pairing_secret_encrypted"] = ""
    tg["pairing_expires_at"] = 0
    config.save(config.data)
    try:
        state = Path(config.paths.root) / "telegram_remote_state.json"
        state.unlink(missing_ok=True)
    except Exception:
        pass
    print("Telegram Remote отключён. Локальный token, pairing и allowlist очищены.")
    print("Перезапусти NEO, если он сейчас открыт.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
