"""Theme-aware state icons for Qt style sheets (UI185-THEME-ARROW-01 / CHECKMARK-01).

The shipped SVGs drew every arrow light (#DDE8F6) and every tick dark (#071018);
on the light themes the arrows nearly vanished and on dark fills the tick did.
Here the stroke is chosen by contrast against the colour the icon is drawn on and
small tinted copies are written once to the cache folder.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor

_SHAPES = {
    "chevron_down": ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
                     '<path d="M3.5 5.75 8 10.25l4.5-4.5" fill="none" stroke="{color}" stroke-width="1.8" '
                     'stroke-linecap="round" stroke-linejoin="round"/></svg>'),
    "chevron_up": ('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
                   '<path d="M3.5 10.25 8 5.75l4.5 4.5" fill="none" stroke="{color}" stroke-width="1.8" '
                   'stroke-linecap="round" stroke-linejoin="round"/></svg>'),
    "checkmark": ('<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18">'
                  '<path d="M4 9.4 7.25 12.5 14 5.5" fill="none" stroke="{color}" stroke-width="2.2" '
                  'stroke-linecap="round" stroke-linejoin="round"/></svg>'),
}


def _luminance(color: QColor) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(color.redF()) + 0.7152 * channel(color.greenF()) + 0.0722 * channel(color.blueF())


def contrast(first: str, second: str) -> float:
    a, b = sorted((_luminance(QColor(first)), _luminance(QColor(second))))
    return (b + 0.05) / (a + 0.05)


def readable_on(background: str, preferred: str = "", minimum: float = 3.0) -> str:
    """``preferred`` when it reaches ``minimum`` contrast on ``background``, else the
    better of near-black and near-white."""
    if preferred and QColor(preferred).isValid() and contrast(preferred, background) >= minimum:
        return QColor(preferred).name()
    return max(("#071018", "#FFFFFF"), key=lambda value: contrast(value, background))


def icon_path(folder: Path, shape: str, color: str) -> str:
    color = QColor(color).name()
    target = Path(folder) / f"{shape}_{color.lstrip('#')}.svg"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        temporary.write_text(_SHAPES[shape].format(color=color), encoding="utf-8")
        temporary.replace(target)
    return target.as_posix()
