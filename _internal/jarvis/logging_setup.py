# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import sys
from pathlib import Path
from jarvis.runtime.diagnostics import RedactingFormatter


def configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    # Long always-listening sessions can produce a lot of diagnostic telemetry.
    # Keep bounded rotating logs instead of an ever-growing file that increases
    # disk I/O and makes crash-recovery collection progressively more expensive.
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            log_dir / "neo_jarvis.log",
            maxBytes=12 * 1024 * 1024,
            backupCount=4,
            encoding="utf-8",
            delay=True,
        ),
    ]
    # pythonw.exe intentionally has no console/stdout/stderr. Keep file logging
    # fully functional without creating a broken StreamHandler(None).
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(RedactingFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=handlers,
        force=True,
    )
