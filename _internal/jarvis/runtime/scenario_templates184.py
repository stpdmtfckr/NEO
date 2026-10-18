"""Create disabled scenario drafts without inspecting or opening any target."""
from __future__ import annotations

from uuid import uuid4


TEMPLATE_KINDS = ("work", "game", "study")


def make_template(kind: str, language: str = "en") -> dict:
    """Return a fresh draft; callers choose a unique name before saving it.

    The empty paths are deliberate. A user must configure or remove those
    steps before validation can succeed. Constructing a draft has no execution
    side effects and does not enable it.
    """
    if kind not in TEMPLATE_KINDS:
        raise ValueError(f"Unknown scenario template: {kind!r}")

    # Import here so importing runtime templates does not initialize UI code.
    from jarvis.ui.scenario_strings import st

    roles = {
        "work": ("template_work_app", "template_work_folder"),
        "game": ("template_game_app", "template_chat_app"),
        "study": ("template_materials", "template_notes"),
    }

    def open_step(role: str) -> dict:
        return {
            "type": "open",
            "label": st(role, language),
            "enabled": True,
            "path": "",
            "monitor": 0,
            "window_title": "",
            "launch_wait": 0.7,
        }

    first, last = roles[kind]
    return {
        "id": str(uuid4()),
        "name": st(f"template_{kind}", language),
        "phrase": "",
        "aliases": [],
        "enabled": False,
        "on_failure": "stop",
        "max_seconds": 300,
        "template": kind,
        "template_help": st("template_help", language),
        "steps": [
            open_step(first),
            {
                "type": "wait",
                "label": st("template_pause", language),
                "enabled": True,
                "seconds": 0.5,
            },
            open_step(last),
        ],
    }
