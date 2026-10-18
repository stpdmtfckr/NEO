# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import queue
from dataclasses import dataclass


@dataclass(frozen=True)
class UICommand:
    action: str


_COMMANDS: "queue.Queue[UICommand]" = queue.Queue(maxsize=32)


def request_show_main_window() -> None:
    """Ask the Qt UI thread to restore the already-running main window."""
    try:
        _COMMANDS.put_nowait(UICommand("show_main_window"))
    except queue.Full:
        # Show requests are idempotent; one pending event is enough.
        pass


def request_quit_application() -> None:
    """Ask the Qt UI thread to perform JARVIS' normal full shutdown."""
    try:
        _COMMANDS.put_nowait(UICommand("quit_application"))
    except queue.Full:
        # Shutdown is idempotent; a later retry/OS close path remains available.
        pass


def drain_ui_commands(limit: int = 32) -> list[UICommand]:
    result: list[UICommand] = []
    for _ in range(max(1, int(limit))):
        try:
            result.append(_COMMANDS.get_nowait())
        except queue.Empty:
            break
    return result
