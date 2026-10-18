# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Telegram-only remote keyboard injection for Windows.

The parser deliberately lives outside the generic tool registry: remote keystrokes
are exposed only by Telegram Remote and therefore cannot accidentally become a
voice/local-Agent capability.  '+' means one simultaneous chord.  A sequence of
known key names separated by commas or spaces means sequential presses.  Anything
else is treated as literal text and is typed with Win32 Unicode SendInput.
"""

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import os
import re
from typing import Iterable


@dataclass(frozen=True, slots=True)
class ParsedRemoteKeys:
    mode: str  # key / chord / sequence / text
    keys: tuple[str, ...] = ()
    text: str = ""


# Canonical names -> Windows virtual-key codes.
_VK: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "lwin": 0x5B,
    "rwin": 0x5C,
    "apps": 0x5D,
    "num0": 0x60,
    "num1": 0x61,
    "num2": 0x62,
    "num3": 0x63,
    "num4": 0x64,
    "num5": 0x65,
    "num6": 0x66,
    "num7": 0x67,
    "num8": 0x68,
    "num9": 0x69,
    "multiply": 0x6A,
    "add": 0x6B,
    "subtract": 0x6D,
    "decimal": 0x6E,
    "divide": 0x6F,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "lshift": 0xA0,
    "rshift": 0xA1,
    "lctrl": 0xA2,
    "rctrl": 0xA3,
    "lalt": 0xA4,
    "ralt": 0xA5,
    "volumemute": 0xAD,
    "volumedown": 0xAE,
    "volumeup": 0xAF,
    "medianext": 0xB0,
    "mediaprev": 0xB1,
    "mediastop": 0xB2,
    "mediaplaypause": 0xB3,
}
for _i in range(1, 25):
    _VK[f"f{_i}"] = 0x6F + _i
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _VK[_c.casefold()] = ord(_c)
for _d in "0123456789":
    _VK[_d] = ord(_d)


_ALIASES: dict[str, str] = {
    "control": "ctrl", "контрол": "ctrl", "контроль": "ctrl", "левый ctrl": "lctrl",
    "left ctrl": "lctrl", "right ctrl": "rctrl", "правый ctrl": "rctrl", "правый control": "rctrl",
    "шифт": "shift", "left shift": "lshift", "левый shift": "lshift", "левый шифт": "lshift",
    "right shift": "rshift", "правый shift": "rshift", "правый шифт": "rshift",
    "альт": "alt", "left alt": "lalt", "левый alt": "lalt", "right alt": "ralt", "правый alt": "ralt",
    "return": "enter", "ввод": "enter", "энтер": "enter",
    "escape": "esc", "эскейп": "esc", "эск": "esc",
    "caps lock": "capslock", "капс lock": "capslock", "капслок": "capslock", "капс": "capslock",
    "back space": "backspace", "бэкспейс": "backspace", "бекспейс": "backspace",
    "пробел": "space",
    "del": "delete", "удалить": "delete",
    "pgup": "pageup", "page up": "pageup", "страница вверх": "pageup",
    "pgdn": "pagedown", "page down": "pagedown", "страница вниз": "pagedown",
    "arrow left": "left", "left arrow": "left", "стрелка влево": "left", "стрелочка влево": "left", "влево": "left",
    "arrow right": "right", "right arrow": "right", "стрелка вправо": "right", "стрелочка вправо": "right", "вправо": "right",
    "arrow up": "up", "up arrow": "up", "стрелка вверх": "up", "стрелочка вверх": "up", "вверх": "up",
    "arrow down": "down", "down arrow": "down", "стрелка вниз": "down", "стрелочка вниз": "down", "вниз": "down",
    "windows": "lwin", "win": "lwin", "вин": "lwin", "super": "lwin",
    "context": "apps", "menu": "apps", "контекстное меню": "apps",
    "print screen": "printscreen", "prtsc": "printscreen",
    "ins": "insert", "home key": "home", "end key": "end",
    "volume mute": "volumemute", "volume down": "volumedown", "volume up": "volumeup",
    "media next": "medianext", "media previous": "mediaprev", "media prev": "mediaprev",
    "media stop": "mediastop", "media play pause": "mediaplaypause",
}
# Canonical names are also accepted directly.
for _name in tuple(_VK):
    _ALIASES.setdefault(_name, _name)

# Fn is intentionally recognized so the UI can explain why Windows cannot inject it.
_FN_ALIASES = {"fn", "function", "фн", "функция"}


def _clean_token(value: str) -> str:
    text = str(value or "").strip().casefold()
    text = text.replace("ё", "е")
    text = re.sub(r"[\-_]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_key(value: str) -> str | None:
    token = _clean_token(value)
    if token in _FN_ALIASES:
        return "fn"
    alias = _ALIASES.get(token)
    if alias:
        return alias
    # One Latin letter or decimal digit is a physical key, useful for games.
    if len(token) == 1 and ("a" <= token <= "z" or token.isdigit()):
        return token
    return None


def _all_known(parts: Iterable[str]) -> tuple[str, ...] | None:
    result: list[str] = []
    for part in parts:
        key = canonical_key(part)
        if key is None:
            return None
        result.append(key)
    return tuple(result) if result else None


def parse_remote_key_input(value: str) -> ParsedRemoteKeys:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Пустое значение")

    if "+" in raw:
        parts = [x.strip() for x in raw.split("+") if x.strip()]
        keys = _all_known(parts)
        if not keys:
            raise ValueError("В комбинации через + должны быть только названия клавиш, например Ctrl+Shift+Esc")
        return ParsedRemoteKeys("chord", keys=keys)

    # Commas/semicolons explicitly mean sequential key presses.
    if "," in raw or ";" in raw:
        parts = [x.strip() for x in re.split(r"[,;]+", raw) if x.strip()]
        keys = _all_known(parts)
        if keys:
            return ParsedRemoteKeys("sequence", keys=keys)

    single = canonical_key(raw)
    if single is not None:
        return ParsedRemoteKeys("key", keys=(single,))

    # "Ctrl Backspace Enter" is also a convenient sequential notation. Do not
    # reinterpret ordinary prose: every whitespace token must be a known key.
    parts = [x for x in re.split(r"\s+", raw) if x]
    if len(parts) > 1:
        keys = _all_known(parts)
        if keys:
            return ParsedRemoteKeys("sequence", keys=keys)

    return ParsedRemoteKeys("text", text=raw)


def _ensure_no_fn(parsed: ParsedRemoteKeys) -> None:
    if "fn" in parsed.keys:
        raise ValueError(
            "Клавиша Fn не имеет стандартного виртуального кода Windows и программно не нажимается через SendInput. "
            "Остальные клавиши и комбинации поддерживаются."
        )


def _win32_send(parsed: ParsedRemoteKeys) -> None:
    if os.name != "nt":
        raise OSError("Удалённое нажатие клавиш доступно только в Windows")
    _ensure_no_fn(parsed)

    ULONG_PTR = ctypes.c_size_t
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_KEYBOARD = 1
    extended_vks = {
        _VK["pageup"], _VK["pagedown"], _VK["end"], _VK["home"],
        _VK["left"], _VK["up"], _VK["right"], _VK["down"],
        _VK["insert"], _VK["delete"], _VK["rctrl"], _VK["ralt"],
        _VK["lwin"], _VK["rwin"], _VK["apps"], _VK["printscreen"],
    }

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        # Win32 INPUT is a union of mouse/keyboard/hardware. Omitting the mouse
        # member makes ctypes calculate the wrong cbSize on 64-bit Windows and
        # SendInput rejects otherwise valid Telegram key events.
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT

    def key_event(vk: int, up: bool = False) -> INPUT:
        flags = KEYEVENTF_EXTENDEDKEY if vk in extended_vks else 0
        if up:
            flags |= KEYEVENTF_KEYUP
        return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, 0, flags, 0, 0))

    def unicode_event(unit: int, up: bool = False) -> INPUT:
        flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
        return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, int(unit), flags, 0, 0))

    events: list[INPUT] = []
    if parsed.mode == "text":
        # Send UTF-16 code units directly: unlike pyautogui.write this handles
        # Cyrillic and does not depend on the active keyboard layout.
        encoded = parsed.text.encode("utf-16-le")
        units = [int.from_bytes(encoded[i:i + 2], "little") for i in range(0, len(encoded), 2)]
        for unit in units:
            # Map literal tab/newline to real navigation keys; all normal symbols
            # use KEYEVENTF_UNICODE.
            if unit in {0x000A, 0x000D}:
                events.extend((key_event(_VK["enter"]), key_event(_VK["enter"], True)))
            elif unit == 0x0009:
                events.extend((key_event(_VK["tab"]), key_event(_VK["tab"], True)))
            else:
                events.extend((unicode_event(unit), unicode_event(unit, True)))
    elif parsed.mode == "chord":
        for name in parsed.keys:
            events.append(key_event(_VK[name]))
        for name in reversed(parsed.keys):
            events.append(key_event(_VK[name], True))
    else:
        for name in parsed.keys:
            events.extend((key_event(_VK[name]), key_event(_VK[name], True)))

    if not events:
        return
    payload = (INPUT * len(events))(*events)
    ctypes.set_last_error(0)
    sent = int(user32.SendInput(len(events), payload, ctypes.sizeof(INPUT)) or 0)
    if sent != len(events):
        code = int(ctypes.get_last_error() or 0)
        detail = f"SendInput отправил {sent} из {len(events)} событий (cbSize={ctypes.sizeof(INPUT)})"
        if sent == 0:
            detail += ". Если целевое приложение запущено от администратора, NEO тоже должен иметь тот же уровень прав (ограничение Windows UIPI)."
        raise OSError(code, detail)


def execute_remote_key_input(value: str) -> tuple[bool, str]:
    try:
        parsed = parse_remote_key_input(value)
        _win32_send(parsed)
    except Exception as exc:
        return False, str(exc)

    if parsed.mode == "text":
        preview = parsed.text if len(parsed.text) <= 60 else parsed.text[:57] + "…"
        return True, f"Введён текст: {preview}"
    if parsed.mode == "chord":
        return True, "Нажата комбинация: " + "+".join(parsed.keys)
    if parsed.mode == "sequence":
        return True, "Последовательно нажаты: " + " → ".join(parsed.keys)
    return True, "Нажата клавиша: " + parsed.keys[0]


__all__ = ["ParsedRemoteKeys", "canonical_key", "parse_remote_key_input", "execute_remote_key_input"]
