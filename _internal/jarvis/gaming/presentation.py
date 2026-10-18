# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import re
import time
from typing import Any


def format_duration(seconds: int | float) -> str:
    seconds = max(0, int(seconds or 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин {secs:02d} с"
    if minutes:
        return f"{minutes} мин {secs:02d} с"
    return f"{secs} с"


def dynamic_session_text(panel: dict[str, Any], now: float | None = None) -> str:
    """Render the elapsed-time line from a panel binding without touching disk."""
    text = str(panel.get("text", "") or "")
    binding = panel.get("binding", {}) if isinstance(panel.get("binding"), dict) else {}
    if str(binding.get("type", "")).casefold() != "game_session":
        return text
    status = str(binding.get("status", "running") or "running").casefold()
    try:
        if status == "ended":
            elapsed = int(binding.get("session_seconds") or 0)
        else:
            started_at = float(binding.get("session_started_at") or 0.0)
            elapsed = max(0, int(float(now or time.time()) - started_at)) if started_at else int(binding.get("session_seconds") or 0)
    except Exception:
        return text
    line = f"Сессия: {format_duration(elapsed)}"
    if re.search(r"(?m)^Сессия:\s*.*$", text):
        return re.sub(r"(?m)^Сессия:\s*.*$", line, text, count=1)
    return (line + "\n" + text).strip()
