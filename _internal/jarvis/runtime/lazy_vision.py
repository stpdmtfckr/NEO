# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""On-demand visual-context gate for NEO JARVIS.

The module intentionally does *not* capture the desktop on a timer. It only
returns a decision for the current utterance. Normal window/file commands use
cheap Windows/UIA context; pixels and Vision are requested only for explicit or
deictic visual work.
"""

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class VisionDecision:
    refresh_foreground: bool = False
    refresh_explorer: bool = False
    capture: bool = False
    analyze: bool = False
    reason: str = "none"


_DEICTIC_RE = re.compile(
    r"\b(?:это|этот|эта|эту|эти|его|ее|её|там|здесь|тут|текущ\w*|выбран\w*|"
    r"this|that|there|here|current|selected)\b",
    re.I,
)
_UI_ACTION_RE = re.compile(
    r"\b(?:нажм\w*|клик\w*|щелк\w*|перетащ\w*|выбер\w*|выдел\w*|"
    r"click|press|drag|select)\b",
    re.I,
)
_FILE_RE = re.compile(r"\b(?:файл\w*|папк\w*|директор\w*|каталог\w*|документ\w*|проводник\w*|explorer|file|folder)\b", re.I)
_WINDOW_RE = re.compile(r"\b(?:окн\w*|вкладк\w*|window|tab)\b", re.I)
_EXPLICIT_VISION_RE = re.compile(
    r"\b(?:что\s+видишь|посмотри\s+на\s+экран|что\s+на\s+экране|проанализируй\s+экран|"
    r"прочитай\s+экран|ошибка\s+на\s+экране|скриншот|screenshot|what\s+do\s+you\s+see|analy[sz]e\s+screen)\b",
    re.I,
)


class LazyVisionGate:
    def __init__(self, config: Any = None) -> None:
        self.config = config

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            return self.config.get(key, default) if self.config is not None else default
        except Exception:
            return default

    def decide(self, text: str, *, semantic_requires_vision: bool = False) -> VisionDecision:
        value = str(text or "").casefold().replace("ё", "е").strip()
        if not value:
            return VisionDecision()
        if not bool(self._cfg("routing.lazy_vision_enabled", True)):
            # Compatibility mode: preserve old broad preflight behavior.
            broad = bool(_FILE_RE.search(value) or _WINDOW_RE.search(value) or (_DEICTIC_RE.search(value) and _UI_ACTION_RE.search(value)))
            return VisionDecision(
                refresh_foreground=broad, refresh_explorer=bool(_FILE_RE.search(value)),
                capture=broad, analyze=bool(_DEICTIC_RE.search(value)), reason="compatibility",
            )

        # A game-specific semantic intent may explicitly request vision. It is
        # still lazy because this decision occurs only after that phrase triggers.
        if semantic_requires_vision:
            return VisionDecision(True, False, True, True, "game_semantic_trigger")

        # Explicit screen questions are handled by screen.analyze later in the
        # direct router; do not pre-capture a duplicate image here.
        if _EXPLICIT_VISION_RE.search(value):
            return VisionDecision(False, False, False, False, "explicit_screen_tool_handles_capture")

        deictic = bool(_DEICTIC_RE.search(value))
        ui_action = bool(_UI_ACTION_RE.search(value))
        has_file = bool(_FILE_RE.search(value))
        has_window = bool(_WINDOW_RE.search(value))

        if deictic and ui_action:
            # "нажми это" genuinely needs fresh pixels/UI visual context.
            return VisionDecision(True, has_file, True, True, "deictic_ui_action")
        if has_file:
            # Explorer/UIA path context is enough for named file/folder work.
            return VisionDecision(True, True, False, False, "filesystem_context_only")
        if has_window:
            # HWND/process/title is enough for named window movement/minimize/etc.
            return VisionDecision(True, False, False, False, "window_context_only")
        return VisionDecision()


__all__ = ["VisionDecision", "LazyVisionGate"]
