# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import ctypes
import json
import asyncio
import logging
import difflib
import os
import re
import time
import webbrowser
import subprocess
import threading
from pathlib import Path
from urllib.parse import quote_plus
from dataclasses import dataclass
from typing import Any

import psutil


logger = logging.getLogger(__name__)


class MediaVerificationError(RuntimeError):
    """A media request was issued but the requested post-state was not proven.

    ``safe_to_fallback`` is deliberately conservative.  It is True only when the
    exact matched media session was re-read and is still observably in the
    opposite state, which means a single UI fallback cannot double-toggle a
    command that may already have succeeded.
    """

    def __init__(self, message: str, *, safe_to_fallback: bool = False, state_after: str = "", command_sent: bool = False):
        super().__init__(message)
        self.safe_to_fallback = bool(safe_to_fallback)
        self.state_after = str(state_after or "")
        self.command_sent = bool(command_sent)

BROWSER_PROCESSES = {
    "chrome.exe", "msedge.exe", "opera.exe", "opera_gx.exe", "firefox.exe",
    "browser.exe", "yandex.exe", "brave.exe", "vivaldi.exe",
}

SERVICE_ALIASES: dict[str, tuple[str, ...]] = {
    "youtube": ("youtube", "ютуб"),
    "ютуб": ("youtube", "ютуб"),
    "вконтакте": ("vk", "вк", "vkontakte", "вконтакте", "vk.com", "vk.ru"),
    "вк": ("vk", "вк", "vkontakte", "вконтакте", "vk.com", "vk.ru"),
    "yandex music": ("yandex music", "яндекс музыка", "яндекс музы", "music.yandex"),
    "яндекс музыка": ("yandex music", "яндекс музыка", "яндекс музы", "music.yandex"),
    "яндекс": ("yandex", "яндекс"),
    "music": ("music", "музыка", "песня", "трек", "audio", "аудио"),
    "video": ("youtube", "ютуб", "video", "видео"),
}

_BROWSER_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "firefox.exe": ("firefox", "mozilla", "mozilla firefox", "мозила", "мозилла", "фаерфокс"),
    "chrome.exe": ("chrome", "google chrome", "хром"),
    "msedge.exe": ("edge", "microsoft edge", "эдж"),
    "opera.exe": ("opera", "опера"),
    "opera_gx.exe": ("opera gx", "опера gx", "опера гх"),
    "browser.exe": ("yandex browser", "яндекс браузер", "яндекс"),
    "yandex.exe": ("yandex browser", "яндекс браузер", "яндекс"),
    "brave.exe": ("brave", "брейв"),
    "vivaldi.exe": ("vivaldi", "вивальди"),
}

_LAST_BROWSER_HANDLE = 0
_TAB_URL_CACHE: dict[tuple[int, int, str], tuple[float, str]] = {}
_BROWSER_LOCK = threading.RLock()


class VideoNavigationUnavailable(RuntimeError):
    """NEO 187 HREQ-026: the player itself offers no next/previous video."""

    def __init__(self, action: str):
        super().__init__(f"YouTube offers no {action} video")
        self.action = str(action)


async def _session_title187(session) -> str:
    try:
        props = await session.try_get_media_properties_async()
        return str(getattr(props, "title", "") or "").strip()
    except Exception:
        return ""


async def _skip_media_session187(session, action: str, info: dict[str, Any]) -> dict[str, Any]:
    """Move to another video through GSMTC and prove it by a changed title.

    The player's own IsNext/IsPreviousEnabled flag decides availability, so
    a single video without a playlist reports «no previous video» instead of
    pretending; a restart of the same video is not a confirmed navigation.
    """
    try:
        controls = getattr(session.get_playback_info(), "controls", None)
    except Exception:
        controls = None
    enabled = getattr(controls, "is_next_enabled" if action == "next" else "is_previous_enabled", None) if controls is not None else None
    if enabled is False:
        raise VideoNavigationUnavailable(action)
    method = getattr(session, "try_skip_next_async" if action == "next" else "try_skip_previous_async", None)
    if not callable(method):
        raise VideoNavigationUnavailable(action)
    before = await _session_title187(session) or str(info.get("title", "") or "")
    ok = await method()
    if ok is False:
        raise VideoNavigationUnavailable(action)
    after = before
    for _ in range(30):
        await asyncio.sleep(0.10)
        after = await _session_title187(session)
        if after and after != before:
            return {**info, "action": action, "title_before": before, "title": after, "verified": True,
                    "control_success": True, "method": "exact_tab_gsmtc_skip",
                    "background_control": True, "input_stolen": False}
    raise MediaVerificationError(f"YouTube did not confirm the {action} video", safe_to_fallback=False,
                                 state_after=after, command_sent=True)


def normalize(value: str) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", text).strip()


def canonical_service(value: str) -> str:
    text = normalize(value)
    if not text:
        return ""
    if "youtube" in text or "ютуб" in text:
        return "youtube"
    if re.search(r"\b(?:vk|вк)\b", text) or "вконтакт" in text:
        return "вконтакте"
    if ("яндекс" in text or "yandex" in text) and ("музык" in text or "music" in text):
        return "яндекс музыка"
    if "яндекс" in text or "yandex" in text:
        return "яндекс"
    if any(token in text for token in ("music", "музык", "песн", "трек", "audio", "аудио")):
        return "music"
    if any(token in text for token in ("video", "видео", "ролик")):
        return "video"
    return text


def service_aliases(value: str) -> tuple[str, ...]:
    canonical = canonical_service(value)
    return SERVICE_ALIASES.get(canonical, (canonical,) if canonical else ())


@dataclass
class BrowserTab:
    title: str
    process_name: str
    window_title: str
    wrapper: Any
    window: Any
    index: int = 0
    url: str = ""


def _extract_url_candidate(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    # Firefox/Chromium accessibility fields sometimes contain a description
    # around the URL rather than the URL alone. Extract only a plausible web URL.
    match = re.search(r"(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?", raw, flags=re.I)
    return match.group(0) if match else ""


def _tab_accessible_url(wrapper: Any) -> str:
    values: list[str] = []
    try:
        values.append(str(wrapper.element_info.help_text or ""))
        values.append(str(wrapper.element_info.automation_id or ""))
    except Exception:
        pass
    try:
        values.append(str(wrapper.iface_value.CurrentValue or ""))
    except Exception:
        pass
    try:
        legacy = wrapper.iface_legacy_iaccessible
        values.append(str(getattr(legacy, "CurrentValue", "") or ""))
        values.append(str(getattr(legacy, "CurrentDescription", "") or ""))
    except Exception:
        pass
    for value in values:
        candidate = _extract_url_candidate(value)
        if candidate:
            return candidate
    return ""


def _tab_cache_key(tab: BrowserTab) -> tuple[int, int, str]:
    return (
        int(getattr(tab.window, "handle", 0) or 0),
        int(tab.index or 0),
        str(tab.process_name or "").casefold(),
    )



def _cache_tab_url(tab: BrowserTab, url: str) -> None:
    value = str(url or "").strip()
    if value:
        _TAB_URL_CACHE[_tab_cache_key(tab)] = (time.monotonic(), value)
        tab.url = value


def _cached_tab_url(tab: BrowserTab, max_age: float = 1800.0) -> str:
    item = _TAB_URL_CACHE.get(_tab_cache_key(tab))
    if not item:
        return ""
    when, value = item
    if time.monotonic() - when > max_age:
        _TAB_URL_CACHE.pop(_tab_cache_key(tab), None)
        return ""
    return value


def _process_name(pid: int) -> str:
    try:
        return str(psutil.Process(pid).name() or "").casefold()
    except Exception:
        return ""


def _rect_values(wrapper: Any) -> tuple[int, int, int, int] | None:
    try:
        rect = wrapper.rectangle()
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        return None


def _browser_tab_items(window: Any) -> list[Any]:
    """Return only the real browser-chrome tabs, never page-internal TabItems.

    VK, settings pages and many web apps expose their own TabItem controls. RC10
    counted those as browser tabs (e.g. VK's «Фото»), making ordinal navigation
    non-deterministic. Real Firefox/Chromium tabs live in the narrow top chrome
    band, so geometry is a reliable cross-browser discriminator.
    """
    try:
        win_rect = window.rectangle()
        items = list(window.descendants(control_type="TabItem"))
    except Exception:
        return []
    top = int(win_rect.top)
    bottom = int(win_rect.bottom)
    left = int(win_rect.left)
    right = int(win_rect.right)
    height = max(1, bottom - top)
    top_limit = top + min(125, max(82, int(height * 0.14)))
    decorated: list[tuple[int, int, Any]] = []
    seen: set[tuple[int, int, int, int, str]] = set()
    for ordinal, item in enumerate(items):
        rect = _rect_values(item)
        if rect is None:
            continue
        l, t, r, b = rect
        item_h = b - t
        item_w = r - l
        if t < top - 8 or t > top_limit or b > top_limit + 18:
            continue
        if item_h <= 5 or item_h > 72 or item_w < 18:
            continue
        if r < left or l > right:
            continue
        try:
            title = str(item.window_text() or item.element_info.name or "").strip()
        except Exception:
            title = ""
        key = (l, t, r, b, title)
        if key in seen:
            continue
        seen.add(key)
        decorated.append((l, ordinal, item))
    decorated.sort(key=lambda x: (x[0], x[1]))
    return [item for _left, _ordinal, item in decorated]


def _tab_items_for_window(window: Any) -> list[Any]:
    return _browser_tab_items(window)


def _browser_query_matches(process: str, window_title: str, query: str) -> bool:
    q = normalize(query)
    if not q or q in {"browser", "браузер"}:
        return True
    aliases = _BROWSER_NAME_ALIASES.get(process, ())
    hay = normalize(window_title)
    if any(normalize(alias) in q or q in normalize(alias) for alias in aliases):
        return True
    return q in hay or any(normalize(alias) in hay and difflib.SequenceMatcher(None, q, normalize(alias)).ratio() >= 0.62 for alias in aliases)


def _browser_windows(browser: str = "") -> list[Any]:
    if os.name != "nt":
        return []
    try:
        from pywinauto import Desktop
        windows = Desktop(backend="uia").windows(visible_only=True)
    except Exception:
        return []
    result: list[Any] = []
    for window in windows:
        try:
            process = _process_name(int(window.element_info.process_id))
            if process not in BROWSER_PROCESSES:
                continue
            title = str(window.window_text() or "")
            if browser and not _browser_query_matches(process, title, browser):
                continue
            if _browser_tab_items(window):
                result.append(window)
        except Exception:
            continue
    return result


def _is_tab_selected(tab: Any) -> bool:
    try:
        return bool(tab.iface_selection_item.CurrentIsSelected)
    except Exception:
        pass
    try:
        return bool(tab.get_selection_item_state())
    except Exception:
        return False


def _select_tab(window: Any, tab: Any, *, verify: bool = True) -> bool:
    global _LAST_BROWSER_HANDLE
    try:
        window.set_focus()
    except Exception:
        pass
    if _is_tab_selected(tab):
        try:
            _LAST_BROWSER_HANDLE = int(getattr(window, "handle", 0) or 0)
        except Exception:
            pass
        return True
    for method_name in ("select", "invoke", "click_input", "click"):
        try:
            getattr(tab, method_name)()
        except Exception:
            continue
        if not verify:
            return True
        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline:
            if _is_tab_selected(tab):
                try:
                    _LAST_BROWSER_HANDLE = int(getattr(window, "handle", 0) or 0)
                except Exception:
                    pass
                return True
            time.sleep(0.025)
    return _is_tab_selected(tab) if verify else False


def list_browser_tabs(browser: str = "") -> list[BrowserTab]:
    found: list[BrowserTab] = []
    for window in _browser_windows(browser):
        try:
            pid = int(window.element_info.process_id)
            process = _process_name(pid)
            window_title = str(window.window_text() or "")
        except Exception:
            continue
        for index, tab in enumerate(_browser_tab_items(window), 1):
            try:
                title = str(tab.window_text() or tab.element_info.name or "").strip()
            except Exception:
                title = ""
            if title:
                entry = BrowserTab(title, process, window_title, tab, window, index=index)
                entry.url = _tab_accessible_url(tab) or _cached_tab_url(entry)
                if _is_tab_selected(tab):
                    selected_url = _strict_selected_tab_url(window, timeout=0.75)
                    if selected_url:
                        _cache_tab_url(entry, selected_url)
                found.append(entry)
    return found


def _title_service_score(title: str, target: str) -> float:
    hay = normalize(title)
    target = canonical_service(target)
    if not hay:
        return 0.0
    if target == "music":
        if "youtube" in hay or "ютуб" in hay:
            return 0.0
        if ("яндекс" in hay or "yandex" in hay) and ("музык" in hay or "music" in hay):
            return 1.0
        if re.search(r"\b(?:vk|вк)\b", hay) or "вконтакт" in hay:
            return 0.96
        if any(x in hay for x in ("моя музыка", "spotify", "soundcloud", "deezer")):
            return 0.92
    elif target == "video":
        if any(x in hay for x in ("youtube", "ютуб")):
            return 1.0
        if any(x in hay for x in ("video", "видео", "twitch", "твич")):
            return 0.88
    aliases = service_aliases(target)
    best = 0.0
    for alias in aliases:
        alias_n = normalize(alias)
        if not alias_n:
            continue
        if alias_n in hay:
            best = max(best, 1.0)
        elif hay:
            best = max(best, difflib.SequenceMatcher(None, alias_n, hay).ratio() * 0.62)
    return best



def _url_matches_service(url: str, target: str) -> bool:
    value = str(url or "").casefold()
    target = canonical_service(target)
    if target == "вконтакте":
        return any(domain in value for domain in ("vk.com/", "vk.ru/", "m.vk.com/", "m.vk.ru/")) or value.rstrip("/").endswith(("vk.com", "vk.ru"))
    if target == "яндекс музыка":
        return "music.yandex." in value
    if target == "youtube":
        return "youtube.com/" in value or "youtu.be/" in value
    if target == "video":
        return any(x in value for x in ("youtube.com/", "twitch.tv/", "rutube.ru/"))
    if target == "music":
        # Music domain is deliberately limited to VK/Yandex/other dedicated music
        # services. YouTube (including music.youtube.com) is excluded because the
        # user explicitly wants music and video to be independent domains.
        return any(x in value for x in ("music.yandex.", "vk.com/audio", "vk.ru/audio", "open.spotify.com"))
    return False


def _read_address_bar(window: Any) -> str:
    """Read the current tab URL through UIA without keyboard focus if possible."""
    try:
        wr = window.rectangle()
        top_limit = int(wr.top) + min(150, max(100, int((wr.bottom - wr.top) * 0.17)))
        edits = list(window.descendants(control_type="Edit"))
    except Exception:
        edits = []
    candidates: list[tuple[int, str]] = []
    for item in edits:
        rect = _rect_values(item)
        if rect and rect[1] > top_limit:
            continue
        value = ""
        try:
            value = str(item.iface_value.CurrentValue or "").strip()
        except Exception:
            try:
                value = str(item.get_value() or "").strip()
            except Exception:
                value = ""
        low = value.casefold()
        if value and ("://" in low or re.search(r"\b[a-z0-9.-]+\.(?:ru|com|net|org|io|tv|me)(?:/|$)", low)):
            score = 2 if "://" in low else 1
            candidates.append((score, value))
    if candidates:
        candidates.sort(key=lambda x: (x[0], len(x[1])), reverse=True)
        return candidates[0][1]
    return ""


def _clipboard_address_bar(window: Any) -> str:
    """Last-resort URL read for the *currently selected* tab only."""
    try:
        import pyautogui
        import pyperclip
        old = pyperclip.paste()
        try:
            window.set_focus()
            pyautogui.hotkey("ctrl", "l")
            time.sleep(0.035)
            pyautogui.hotkey("ctrl", "c")
            time.sleep(0.035)
            value = str(pyperclip.paste() or "").strip()
            pyautogui.press("esc")
            return value
        finally:
            try:
                pyperclip.copy(old)
            except Exception:
                pass
    except Exception:
        return ""

def _looks_like_web_url(value: str) -> bool:
    raw = str(value or "").strip()
    return bool(re.match(r"^(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/:?#]|$)", raw, flags=re.I))


def _strict_selected_tab_url(
    window: Any, *, timeout: float = 0.9, allow_keyboard_fallback: bool = False
) -> str:
    """Read a stable selected-tab URL without disturbing the browser UI.

    Media control used to call ``Ctrl+L``/``Ctrl+C`` repeatedly while trying to
    prove the selected URL.  On a real Firefox/Chromium window this visibly opened
    and closed the address bar several times and, worse, stole focus from the
    YouTube player.  URL discovery is evidence, not an action, so the normal path
    is now *read-only UI Automation only*.

    ``allow_keyboard_fallback`` exists for rare explicit navigation/debug callers
    that truly need a URL and accept one focus change.  Even there it is attempted
    at most once after passive UIA failed; media play/pause never enables it.
    """
    deadline = time.monotonic() + max(0.20, float(timeout))
    previous = ""
    stable_hits = 0
    observed: list[str] = []
    time.sleep(0.04)
    while time.monotonic() < deadline:
        try:
            value = str(_read_address_bar(window) or "").strip()
        except Exception:
            value = ""
        if _looks_like_web_url(value):
            observed.append(value)
            if value == previous:
                stable_hits += 1
                if stable_hits >= 1:
                    return value
            else:
                previous = value
                stable_hits = 0
        time.sleep(0.07)

    unique = {x for x in observed if x}
    if len(unique) == 1:
        return next(iter(unique))

    if allow_keyboard_fallback:
        # One and only one foreground-changing read.  Never loop this fallback.
        try:
            candidate = str(_clipboard_address_bar(window) or "").strip()
        except Exception:
            candidate = ""
        return candidate if _looks_like_web_url(candidate) else ""
    return ""


def _selected_tab_with_index(window: Any) -> tuple[Any | None, int]:
    items = _browser_tab_items(window)
    for index, item in enumerate(items, 1):
        if _is_tab_selected(item):
            return item, index
    return (items[0], 1) if items else (None, 0)


def _page_fingerprint(window: Any, *, limit_chars: int = 7000) -> str:
    """Collect visible page text for correlation with a Windows media session.

    No element is clicked.  The fingerprint is used only as evidence that a GSMTC
    title/artist belongs to the exact tab the user selected.
    """
    try:
        wr = window.rectangle()
        page_floor = int(wr.top) + min(155, max(95, int((wr.bottom - wr.top) * 0.12)))
    except Exception:
        page_floor = -10**9
    pieces: list[str] = []
    seen: set[str] = set()
    for control_type in ("Text", "Button", "Document", "ListItem"):
        try:
            controls = window.descendants(control_type=control_type)
        except Exception:
            continue
        for item in controls[:850]:
            rect = _rect_values(item)
            if rect is not None and rect[1] < page_floor:
                continue
            try:
                name = str(item.window_text() or item.element_info.name or "").strip()
            except Exception:
                name = ""
            norm = normalize(name)
            if not norm or norm in seen or len(norm) > 240:
                continue
            seen.add(norm)
            pieces.append(name)
            if sum(len(x) + 1 for x in pieces) >= limit_chars:
                return " | ".join(pieces)[:limit_chars]
    return " | ".join(pieces)[:limit_chars]



def _selected_tab(window: Any) -> Any | None:
    items = _browser_tab_items(window)
    for item in items:
        if _is_tab_selected(item):
            return item
    return items[0] if items else None


def _probe_tab_url(tab: BrowserTab) -> str:
    if os.name != "nt":
        return ""
    if not _select_tab(tab.window, tab.wrapper, verify=True):
        return ""
    url = _strict_selected_tab_url(tab.window, timeout=0.85)
    if url:
        _cache_tab_url(tab, url)
    return url



def _explicit_title_match(tab: BrowserTab, target: str) -> bool:
    # A browser window title normally mirrors only the currently selected tab.
    # Mixing it into every tab made all tabs look like the selected page (e.g.
    # every Firefox tab became "Google Search" or "YouTube"). Use the real
    # TabItem title only; URL probing handles ambiguous service names.
    text = normalize(tab.title)
    target = canonical_service(target)
    if target == "вконтакте":
        return "вконтакт" in text or bool(re.search(r"\b(?:vk|вк)\b", text))
    if target == "яндекс музыка":
        return ("яндекс" in text or "yandex" in text) and ("музык" in text or "music" in text)
    if target == "youtube":
        return "youtube" in text or "ютуб" in text
    return False


def find_service_tab(target: str, browser: str = "") -> BrowserTab | None:
    """Find a named service without sweeping through every tab.

    Resolution order: selected tab URL -> explicit service title -> at most four
    plausible ambiguous tabs. Original selections are restored when no match is
    found, preventing the old endless tab-hopping behaviour.
    """
    tabs = list_browser_tabs(browser)
    if not tabs:
        return None
    target = canonical_service(target)

    # v3.9.9: if the user is already looking at the requested service, that exact
    # selected tab wins before any cached URL from another tab. A stale cache from
    # a second YouTube tab could otherwise make NEO control a different media
    # session while reporting success for the video visible on screen.
    windows_seen: list[Any] = []
    for tab in tabs:
        if any(getattr(tab.window, "handle", None) == getattr(x, "handle", None) for x in windows_seen):
            continue
        windows_seen.append(tab.window)
        selected = next((
            x for x in tabs
            if getattr(x.window, "handle", None) == getattr(tab.window, "handle", None)
            and _is_tab_selected(x.wrapper)
        ), None)
        if selected is None:
            continue
        url = _strict_selected_tab_url(selected.window, timeout=0.55)
        selected.url = url
        if url:
            _cache_tab_url(selected, url)
        if _url_matches_service(url, target) or _explicit_title_match(selected, target):
            return selected

    # Then use accessibility/cache metadata for the remaining tabs. This requires no tab
    # switching and is the preferred path for VK/Yandex/YouTube.
    for tab in tabs:
        known_url = tab.url or _cached_tab_url(tab)
        if known_url and _url_matches_service(known_url, target):
            # A cached address can belong to the tab that was selected *before*
            # Firefox finished switching its accessibility tree.  Never let such
            # a cache override a strongly contradictory exact TabItem title.
            title_n = normalize(tab.title)
            title_is_youtube = "youtube" in title_n or "ютуб" in title_n
            title_is_music = any(x in title_n for x in ("моя музыка", "яндекс музыка", "yandex music", "вконтакт"))
            if target == "youtube" and title_is_music:
                continue
            if target in {"вконтакте", "яндекс музыка"} and title_is_youtube:
                continue
            tab.url = known_url
            return tab

    # Fastest and least invasive path: inspect the currently selected tab in each
    # browser window. The user's VK screenshot is exactly this case (vk.ru/audios).
    windows_seen: list[Any] = []
    for tab in tabs:
        if any(getattr(tab.window, "handle", None) == getattr(x, "handle", None) for x in windows_seen):
            continue
        windows_seen.append(tab.window)
        selected = next((x for x in tabs if getattr(x.window, "handle", None) == getattr(tab.window, "handle", None) and _is_tab_selected(x.wrapper)), None)
        if selected is None:
            continue
        # Firefox can expose the newly-selected TabItem before the address bar
        # has finished switching away from the previous page.  Use the same
        # stabilized read as ordinal tab routing so a stale YouTube URL can
        # never reclassify a VK/Yandex tab.
        url = _strict_selected_tab_url(selected.window, timeout=0.90)
        selected.url = url
        if url:
            _cache_tab_url(selected, url)
        if _url_matches_service(url, target):
            return selected

    # A clear title such as YouTube or Yandex Music is trustworthy. «Моя музыка»
    # alone is intentionally NOT clear because both VK and Yandex may use it.
    explicit = [tab for tab in tabs if _explicit_title_match(tab, target)]
    if explicit:
        explicit.sort(key=lambda t: _title_service_score(t.title, target), reverse=True)
        return explicit[0]

    scored = sorted(
        ((_title_service_score(tab.title, target), tab) for tab in tabs),
        key=lambda item: item[0], reverse=True,
    )
    candidates = [(score, tab) for score, tab in scored if score >= 0.66][:2]
    if not candidates:
        return None

    originals: dict[int, Any] = {}
    for _score, tab in candidates:
        handle = int(getattr(tab.window, "handle", 0) or 0)
        originals.setdefault(handle, _selected_tab(tab.window))

    found: BrowserTab | None = None
    try:
        for _score, tab in candidates:
            url = _probe_tab_url(tab)
            tab.url = url
            if _url_matches_service(url, target):
                found = tab
                break
    finally:
        if found is None:
            for _handle, original in originals.items():
                if original is None:
                    continue
                # The wrapper carries its own window relation; find matching window.
                window = next((t.window for _s, t in candidates if int(getattr(t.window, "handle", 0) or 0) == _handle), None)
                if window is not None:
                    _select_tab(window, original, verify=False)
    return found


def find_service_tab_passive(target: str, browser: str = "") -> BrowserTab | None:
    """Resolve a media service without selecting a tab or focusing its window.

    This is intentionally conservative for background play/pause. If metadata is
    ambiguous, return None rather than moving the user's mouse, switching tabs, or
    stealing keyboard focus.
    """
    tabs = list_browser_tabs(browser)
    if not tabs:
        return None
    target = canonical_service(target)

    # Selected tab metadata is safe to inspect even when the browser itself is in
    # the background. Prefer it if it is already the requested service.
    for tab in tabs:
        if not _is_tab_selected(tab.wrapper):
            continue
        known_url = tab.url or _cached_tab_url(tab)
        if (known_url and _url_matches_service(known_url, target)) or _explicit_title_match(tab, target):
            tab.url = known_url or tab.url
            return tab

    candidates: list[BrowserTab] = []
    for tab in tabs:
        known_url = tab.url or _cached_tab_url(tab)
        if known_url and _url_matches_service(known_url, target):
            tab.url = known_url
            candidates.append(tab)
            continue
        if _explicit_title_match(tab, target):
            candidates.append(tab)

    # A single passive candidate is safe. With multiple candidates, select only
    # when one title is clearly stronger; otherwise do not guess.
    unique: list[BrowserTab] = []
    seen = set()
    for tab in candidates:
        key = (int(getattr(tab.window, "handle", 0) or 0), int(tab.index or 0), normalize(tab.title))
        if key not in seen:
            seen.add(key); unique.append(tab)
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        ranked = sorted(((_title_service_score(tab.title, target), tab) for tab in unique), key=lambda x: x[0], reverse=True)
        if ranked[0][0] >= 0.92 and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.12):
            return ranked[0][1]
    return None


def activate_service_tab(target: str, browser: str = "") -> BrowserTab | None:
    tab = find_service_tab(target, browser)
    if tab is None:
        return None
    if not _select_tab(tab.window, tab.wrapper, verify=True):
        return None
    return tab


def current_browser_service(browser: str = "") -> str:
    """Return a concrete service for the selected/foreground browser tab."""
    window = _foreground_browser_window(browser)
    if window is None:
        return ""
    url = _strict_selected_tab_url(window, timeout=0.75)
    for service in ("вконтакте", "яндекс музыка", "youtube"):
        if _url_matches_service(url, service):
            return service
    selected = _selected_tab(window)
    if selected is not None:
        try:
            title = str(selected.window_text() or selected.element_info.name or "")
        except Exception:
            title = ""
        for service in ("яндекс музыка", "youtube", "вконтакте"):
            temp = BrowserTab(title, _process_name(int(window.element_info.process_id)), str(window.window_text() or ""), selected, window)
            if _explicit_title_match(temp, service):
                return service
        title_n = normalize(title)
        # Firefox/VK/Yandex can expose only a generic title such as «Моя музыка».
        # Keep it as the MUSIC domain instead of pretending it is VK or Yandex.
        if ("моя музыка" in title_n or "music" in title_n or "музыка" in title_n) and "youtube" not in title_n and "ютуб" not in title_n:
            return "music"
    return ""


def _foreground_browser_window(browser: str = "") -> Any | None:
    global _LAST_BROWSER_HANDLE
    if os.name != "nt":
        return None
    try:
        from pywinauto import Desktop
        handle = int(ctypes.windll.user32.GetForegroundWindow())
        if handle:
            window = Desktop(backend="uia").window(handle=handle)
            pid = int(window.element_info.process_id)
            process = _process_name(pid)
            title = str(window.window_text() or "")
            if process in BROWSER_PROCESSES and _browser_query_matches(process, title, browser):
                _LAST_BROWSER_HANDLE = handle
                return window
        if _LAST_BROWSER_HANDLE:
            try:
                window = Desktop(backend="uia").window(handle=_LAST_BROWSER_HANDLE)
                pid = int(window.element_info.process_id)
                process = _process_name(pid)
                title = str(window.window_text() or "")
                if process in BROWSER_PROCESSES and _browser_query_matches(process, title, browser):
                    return window
            except Exception:
                _LAST_BROWSER_HANDLE = 0
    except Exception:
        pass
    windows = _browser_windows(browser)
    return windows[0] if windows else None


def activate_tab_by_index(index: int, browser: str = "") -> dict[str, Any]:
    """Activate an ordinal browser tab and verify the exact selected index.

    Browser-native Ctrl+1..8 is preferred because Firefox implements it directly.
    UI Automation is retained as a fallback and for later tabs.  The URL is read
    only after selection stabilizes, preventing stale URL/service classification.
    """
    if os.name != "nt":
        raise OSError("Управление вкладками доступно только в Windows")
    index = int(index)
    if index < 1:
        raise ValueError("Номер вкладки начинается с 1")
    window = _foreground_browser_window(browser)
    if window is None:
        raise RuntimeError(f"Не вижу открытого окна браузера{(' «' + browser + '»') if browser else ''}")
    items = _browser_tab_items(window)
    if index > len(items):
        raise RuntimeError(f"В браузере только {len(items)} вкладок")

    activated = False
    if index <= 8:
        try:
            import pyautogui
            window.set_focus()
            pyautogui.hotkey("ctrl", str(index))
            deadline = time.monotonic() + 0.65
            while time.monotonic() < deadline:
                selected, selected_index = _selected_tab_with_index(window)
                if selected is not None and selected_index == index:
                    activated = True
                    break
                time.sleep(0.035)
        except Exception:
            activated = False

    if not activated:
        items = _browser_tab_items(window)
        if index > len(items) or not _select_tab(window, items[index - 1], verify=True):
            raise RuntimeError(f"Браузер не подтвердил переход на вкладку {index}")

    # Refresh wrappers after switching; Firefox can recreate the selected TabItem.
    items = _browser_tab_items(window)
    selected, selected_index = _selected_tab_with_index(window)
    if selected is None or selected_index != index:
        raise RuntimeError(f"Браузер выбрал вкладку {selected_index or '?'} вместо {index}")
    try:
        title = str(selected.window_text() or selected.element_info.name or f"вкладка {index}").strip()
    except Exception:
        title = f"вкладка {index}"
    process = _process_name(int(window.element_info.process_id))
    url = _strict_selected_tab_url(window, timeout=0.95)
    entry = BrowserTab(title, process, str(window.window_text() or ""), selected, window, index=index, url=url)
    if url:
        _cache_tab_url(entry, url)
    return {
        "index": index,
        "title": title,
        "url": url,
        "browser": process,
        "window_handle": int(getattr(window, "handle", 0) or 0),
        "verified": True,
    }



def activate_tab_by_title(query: str, browser: str = "") -> dict[str, Any]:
    """Activate a tab by service/title without confusing it with another browser."""
    query = str(query or "").strip()
    if not query:
        raise ValueError("Не указано название вкладки")
    canonical = canonical_service(query)
    if canonical == "video":
        canonical = "youtube"
    if canonical == "music":
        # A generic music tab is strictly VK/Yandex Music; never let a YouTube
        # title containing “music” win a fuzzy tab match.
        current = current_browser_service(browser)
        services = ([current] if current in {"вконтакте", "яндекс музыка"} else []) + ["вконтакте", "яндекс музыка"]
        seen = set()
        for service in services:
            if service in seen:
                continue
            seen.add(service)
            tab = activate_service_tab(service, browser)
            if tab is not None:
                url = _strict_selected_tab_url(tab.window, timeout=0.90) or tab.url
                if url:
                    _cache_tab_url(tab, url)
                return {"index": tab.index, "title": tab.title, "url": url, "browser": tab.process_name, "verified": True}
        raise RuntimeError("Не нашёл вкладку VK или Яндекс Музыки; YouTube для запроса про музыку не открываю")
    if canonical in {"вконтакте", "яндекс музыка", "youtube"}:
        tab = activate_service_tab(canonical, browser)
        if tab is None:
            raise RuntimeError(f"Не нашёл вкладку «{query}» в выбранном браузере")
        url = _strict_selected_tab_url(tab.window, timeout=0.90) or tab.url
        if url:
            _cache_tab_url(tab, url)
        return {"index": tab.index, "title": tab.title, "url": url, "browser": tab.process_name, "verified": True}
    tabs = list_browser_tabs(browser)
    if not tabs:
        raise RuntimeError(f"Не вижу открытого окна браузера{(' «' + browser + '»') if browser else ''}")
    q = normalize(re.sub(r"^(?:с|со)\s+", "", query))
    best: tuple[float, BrowserTab] | None = None
    for tab in tabs:
        hay = normalize(tab.title)
        if not hay:
            continue
        score = difflib.SequenceMatcher(None, q, hay).ratio()
        if q in hay or hay in q:
            score = max(score, 0.92)
        if best is None or score > best[0]:
            best = (score, tab)
    if best is None or best[0] < 0.62:
        raise RuntimeError(f"Не нашёл вкладку, похожую на «{query}», в выбранном браузере")
    tab = best[1]
    if not _select_tab(tab.window, tab.wrapper, verify=True):
        raise RuntimeError(f"Браузер не подтвердил переход на вкладку «{tab.title}»")
    url = _strict_selected_tab_url(tab.window, timeout=0.90)
    if url:
        _cache_tab_url(tab, url)
    return {"index": tab.index, "title": tab.title, "url": url, "browser": tab.process_name, "verified": True, "score": round(best[0], 3)}


def close_tab_by_index(index: int, browser: str = "") -> dict[str, Any]:
    """Close one exact ordinal browser tab and verify the browser state changed.

    The old implementation waited only 100 ms after Ctrl+W and then trusted a
    single UIA tab-count read. Chromium/Firefox often refresh their accessibility
    tab strip later than that, which produced false failures even when the tab did
    close.  This version keeps the exact window selected by activate_tab_by_index,
    refocuses it, uses browser-native close shortcuts, and polls a freshly wrapped
    tab strip before declaring success.
    """
    if os.name != "nt":
        raise OSError("Управление вкладками доступно только в Windows")
    index = int(index)
    data = activate_tab_by_index(index, browser)
    handle = int(data.get("window_handle", 0) or 0)
    closed_title = str(data.get("title", "") or "").strip()

    def reacquire() -> Any | None:
        if handle:
            try:
                from pywinauto import Desktop
                win = Desktop(backend="uia").window(handle=handle)
                # Touch element_info so a dead HWND is rejected immediately.
                _ = int(win.element_info.process_id)
                return win
            except Exception:
                return None
        return _foreground_browser_window(browser)

    window = reacquire()
    if window is None:
        raise RuntimeError("После выбора вкладки потеряно окно браузера")
    before_items = _browser_tab_items(window)
    before = len(before_items)
    if before <= 0:
        raise RuntimeError("Браузер не сообщил список вкладок перед закрытием")

    # Reconfirm the exact ordinal immediately before the destructive shortcut.
    selected, selected_index = _selected_tab_with_index(window)
    if selected is None or selected_index != index:
        raise RuntimeError(f"Перед закрытием выбрана вкладка {selected_index or '?'} вместо {index}")
    try:
        window.set_focus()
    except Exception:
        pass
    time.sleep(0.06)

    def snapshot() -> tuple[int, int, str]:
        win = reacquire()
        if win is None:
            return 0, 0, ""
        items = _browser_tab_items(win)
        sel, sel_index = _selected_tab_with_index(win)
        sel_title = ""
        if sel is not None:
            try:
                sel_title = str(sel.window_text() or sel.element_info.name or "").strip()
            except Exception:
                pass
        return len(items), int(sel_index or 0), sel_title

    def wait_closed(timeout: float = 1.8) -> tuple[bool, int, int, str]:
        deadline = time.monotonic() + timeout
        last = (before, index, closed_title)
        while time.monotonic() < deadline:
            count, sel_index, sel_title = snapshot()
            last = (count, sel_index, sel_title)
            # Closing the last tab may destroy the whole browser window.
            if count == 0:
                return True, count, sel_index, sel_title
            if count < before:
                return True, count, sel_index, sel_title
            time.sleep(0.06)
        return False, last[0], last[1], last[2]

    import pyautogui
    attempts: list[str] = []
    for shortcut in (("ctrl", "w"), ("ctrl", "f4")):
        win = reacquire()
        if win is None:
            data.update({"verified": True, "closed": True, "method": attempts[-1] if attempts else "browser_window_closed"})
            return data
        try:
            win.set_focus()
        except Exception:
            pass
        time.sleep(0.04)
        try:
            pyautogui.hotkey(*shortcut)
            method = "+".join(shortcut)
            attempts.append(method)
        except Exception as exc:
            attempts.append("+".join(shortcut) + ":failed")
            if shortcut == ("ctrl", "f4"):
                raise RuntimeError(f"Не удалось закрыть вкладку: {exc}") from exc
            continue
        ok, after, selected_after, title_after = wait_closed()
        if ok:
            data.update({
                "verified": True,
                "closed": True,
                "method": method,
                "tabs_before": before,
                "tabs_after": after,
                "selected_index_after": selected_after,
                "selected_title_after": title_after,
                "attempts": attempts,
            })
            return data
        # Retry with Ctrl+F4 only when the fresh tab strip still proves that the
        # requested tab did not close.  We never fire two close shortcuts blindly.

    count, selected_after, title_after = snapshot()
    raise RuntimeError(
        f"Браузер не подтвердил закрытие вкладки {index} (было вкладок: {before}, стало: {count}; "
        f"выбрана после попытки: {selected_after or '?'})"
    )


def click_first_page_link(browser: str = "") -> dict[str, Any]:
    """Click the first plausible web-page hyperlink in the configured browser.

    This deliberately ignores browser chrome and common Google navigation links.
    It is intended for a voice workflow like: search for X -> open first result.
    """
    if os.name != "nt":
        raise OSError("Переход по ссылке доступен только в Windows")
    window = _foreground_browser_window(browser)
    if window is None:
        raise RuntimeError(f"Не вижу открытого окна браузера{(' «' + browser + '»') if browser else ''}")
    try:
        window.set_focus()
    except Exception:
        pass
    time.sleep(0.12)
    try:
        wr = window.rectangle()
        top_floor = int(wr.top) + min(150, max(95, int((wr.bottom - wr.top) * 0.12)))
        controls = list(window.descendants(control_type="Hyperlink"))
    except Exception as exc:
        raise RuntimeError(f"Не удалось прочитать ссылки страницы: {exc}") from exc

    banned = (
        "google", "поиск", "search", "картинки", "images", "видео", "videos",
        "новости", "news", "карты", "maps", "инструменты", "tools", "войти", "sign in",
        "настройки", "settings", "справка", "help", "кэш", "cached",
    )
    candidates: list[tuple[int, int, Any, str]] = []
    for ordinal, item in enumerate(controls):
        rect = _rect_values(item)
        if rect is None:
            continue
        l, t, r, b = rect
        if t < top_floor or r - l < 18 or b - t < 8:
            continue
        try:
            name = str(item.window_text() or item.element_info.name or "").strip()
            help_text = str(item.element_info.help_text or "").strip()
        except Exception:
            name, help_text = "", ""
        text = normalize(f"{name} {help_text}")
        if not name or len(text) < 3:
            continue
        if any(text == word or text.startswith(word + " ") for word in banned):
            continue
        # Skip obvious browser/site navigation and Google-owned result widgets.
        if "google com" in text and len(text.split()) <= 5:
            continue
        candidates.append((t, ordinal, item, name))
    if not candidates:
        raise RuntimeError("Не нашёл подходящую первую ссылку на странице")
    candidates.sort(key=lambda x: (x[0], x[1]))
    _t, _ordinal, item, name = candidates[0]
    for method in ("invoke", "click_input", "click"):
        try:
            getattr(item, method)()
            time.sleep(0.18)
            return {"title": name, "browser": browser, "verified": True}
        except Exception:
            continue
    raise RuntimeError(f"Нашёл первую ссылку «{name}», но браузер не дал её открыть")


def _normalized_control_name(item: Any) -> str:
    parts: list[str] = []
    try:
        parts.append(str(item.window_text() or ""))
    except Exception:
        pass
    try:
        parts.append(str(item.element_info.name or ""))
        parts.append(str(item.element_info.help_text or ""))
        parts.append(str(item.element_info.automation_id or ""))
    except Exception:
        pass
    return normalize(" ".join(parts))


def _action_control_needles(action: str, target: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    action = str(action or "").casefold()
    if action in {"pause", "stop"}:
        return ("пауза", "pause", "приостанов", "остановить", "останов"), ("воспроизвести", "play", "продолжить")
    if action == "play":
        return ("воспроизвести", "play", "продолжить", "возобнов"), ("пауза", "pause")
    if action == "next":
        return ("следующ", "next", "вперед", "forward"), ("предыдущ", "previous", "назад")
    if action == "previous":
        return ("предыдущ", "previous", "назад", "back"), ("следующ", "next", "вперед")
    if action == "play_pause":
        return ("пауза", "pause", "воспроизвести", "play", "продолжить"), ()
    if action == "fullscreen":
        # YouTube exposes localized accessible names for both entering and
        # leaving fullscreen. Treat both as the same explicit toggle action.
        return (
            "fullscreen", "full screen", "exit full screen", "во весь экран", "полноэкран",
            "выйти из полноэкран", "vollbild", "plein écran", "plein ecran",
            "pantalla completa", "schermo intero", "tela cheia", "ecrã inteiro", "ecra inteiro",
        ), ()
    if action == "theater":
        return (
            "theater mode", "theatre mode", "cinema mode", "default view",
            "театральн", "широкий экран", "обычный режим", "kinomodus",
            "mode cinéma", "mode cinema", "modo cine", "modalità cinema",
            "modalita cinema", "modo cinema",
        ), ()
    return (), ()


def _send_media_vk(vk_code: int) -> None:
    KEYEVENTF_KEYUP = 0x0002
    ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)


def _focus_page_document(window: Any) -> bool:
    """Move keyboard focus into the web page without touching the address bar.

    RC19 used javascript: bookmarklets for page control. Firefox can intentionally
    treat pasted javascript: text as a search query, which navigates away from the
    user's page. RC20 never writes executable text into the address bar.
    """
    try:
        window.set_focus()
    except Exception:
        pass
    try:
        wr = window.rectangle()
        top_limit = int(wr.top) + min(180, max(110, int((wr.bottom - wr.top) * 0.18)))
    except Exception:
        top_limit = -10**9
    for control_type in ("Document", "Pane"):
        try:
            items = list(window.descendants(control_type=control_type))
        except Exception:
            continue
        for item in items:
            rect = _rect_values(item)
            if rect is not None and rect[1] < top_limit:
                continue
            for method in ("set_focus", "focus"):
                try:
                    getattr(item, method)()
                    return True
                except Exception:
                    continue
    try:
        import pyautogui
        # Escape exits the address bar/search overlays without navigating.
        pyautogui.press("esc")
    except Exception:
        pass
    return False


def _verified_service_url(tab: BrowserTab, target: str) -> str:
    """Return a stabilized selected-tab URL only for the requested service."""
    url = _strict_selected_tab_url(tab.window, timeout=0.90) or tab.url or _cached_tab_url(tab)
    if url:
        _cache_tab_url(tab, url)
    if not _url_matches_service(url, target):
        raise RuntimeError(
            f"Выбранная вкладка не подтверждена как «{canonical_service(target)}» "
            f"(URL: {url or 'не удалось прочитать'}). Другую медиа-вкладку не трогаю."
        )
    return url


def _run_page_media_action(action: str, target: str, browser: str = "", tab: BrowserTab | None = None) -> dict[str, Any] | None:
    """Legacy DOM hook intentionally disabled.

    Never paste javascript: into a browser address bar. Firefox/Chromium may search
    that text on the web, replacing the user's current tab with a random result.
    Service control is handled with UI Automation and verified service-local keys.
    """
    return None


def _youtube_active_tab(browser: str = "") -> BrowserTab:
    """Activate one verified YouTube tab for an explicit user media command."""
    tab = activate_service_tab("youtube", browser) or activate_service_tab("youtube", "")
    if tab is None:
        raise RuntimeError("Не нашёл открытую вкладку YouTube")
    try:
        tab.window.set_focus()
    except Exception:
        pass
    selected, index = _selected_tab_with_index(tab.window)
    if selected is not None:
        tab.wrapper = selected
        tab.index = index
    fresh = _strict_selected_tab_url(tab.window, timeout=0.9)
    if fresh:
        tab.url = fresh
        _cache_tab_url(tab, fresh)
    if not (_url_matches_service(tab.url, "youtube") or _explicit_title_match(tab, "youtube")):
        raise RuntimeError("Активированная вкладка не подтверждена как YouTube")
    return tab


def _youtube_focus_player(tab: BrowserTab) -> None:
    try:
        tab.window.set_focus()
    except Exception:
        pass
    state, control, _name = _youtube_player_button_state(tab)
    if control is not None:
        try:
            control.set_focus()
            return
        except Exception:
            pass
    _focus_page_document(tab.window)


def _youtube_current_url(tab: BrowserTab) -> str:
    try:
        value = _strict_selected_tab_url(tab.window, timeout=0.55)
        if value:
            tab.url = value
            _cache_tab_url(tab, value)
            return value
    except Exception:
        pass
    return str(tab.url or "")


def _youtube_wait_url_change(tab: BrowserTab, before: str, *, timeout: float = 4.0) -> str:
    deadline = time.monotonic() + max(0.5, float(timeout))
    before_n = str(before or "").strip()
    last = before_n
    while time.monotonic() < deadline:
        time.sleep(0.15)
        last = _youtube_current_url(tab)
        if last and last != before_n:
            return last
    return last


def _youtube_send_key(tab: BrowserTab, *, key: str = "", hotkey: tuple[str, ...] = ()) -> None:
    import pyautogui
    _youtube_focus_player(tab)
    if hotkey:
        pyautogui.hotkey(*hotkey)
    elif key:
        pyautogui.press(key)
    else:
        raise ValueError("YouTube key is empty")


def _youtube_click_named(tab: BrowserTab, include: tuple[str, ...], *, exclude: tuple[str, ...] = (), control_types: tuple[str, ...] = ("Button", "MenuItem", "ListItem", "Text", "Custom"), timeout: float = 1.4) -> tuple[Any, str]:
    deadline = time.monotonic() + max(0.25, timeout)
    include_n = tuple(normalize(x) for x in include if x)
    exclude_n = tuple(normalize(x) for x in exclude if x)
    last_names: list[str] = []
    while time.monotonic() < deadline:
        candidates: list[tuple[float, Any, str]] = []
        for ctype in control_types:
            try:
                controls = list(tab.window.descendants(control_type=ctype))
            except Exception:
                continue
            for item in controls[:1800]:
                name = _normalized_control_name(item)
                if not name:
                    continue
                if len(last_names) < 12:
                    last_names.append(name)
                if exclude_n and any(tok and tok in name for tok in exclude_n):
                    continue
                if not any(tok and tok in name for tok in include_n):
                    continue
                score = 10.0
                if any(name == tok for tok in include_n):
                    score += 4.0
                if ctype in {"Button", "MenuItem", "ListItem"}:
                    score += 2.0
                rect = _rect_values(item)
                if rect is not None:
                    l,t,r,b = rect
                    if r-l <= 2 or b-t <= 2:
                        continue
                    score += 0.5
                candidates.append((score, item, name))
        candidates.sort(key=lambda row: row[0], reverse=True)
        for _score, item, name in candidates[:20]:
            for method_name in ("invoke", "click_input", "click"):
                try:
                    getattr(item, method_name)()
                    time.sleep(0.12)
                    return item, name
                except Exception:
                    continue
        time.sleep(0.12)
    raise RuntimeError("Не нашёл нужный элемент YouTube: " + "/".join(include))


def _youtube_show_controls(tab: BrowserTab) -> None:
    try:
        import pyautogui
        wr = tab.window.rectangle()
        old = pyautogui.position()
        pyautogui.moveTo(int(wr.left + (wr.right-wr.left)*0.50), int(wr.top + (wr.bottom-wr.top)*0.55), duration=0.05)
        time.sleep(0.15)
        try:
            pyautogui.moveTo(old.x, old.y, duration=0.03)
        except Exception:
            pass
    except Exception:
        pass


def _youtube_set_settings_option(tab: BrowserTab, section: str, value: str) -> dict[str, Any]:
    """Set an exact YouTube Settings submenu item through accessibility UI."""
    _youtube_focus_player(tab)
    _youtube_show_controls(tab)
    _youtube_click_named(tab, ("настройки", "settings"), exclude=("настройки аккаунта", "account settings"), control_types=("Button",), timeout=2.0)
    section_n = str(section or "").casefold()
    if section_n == "quality":
        section_tokens = ("качество", "quality", "qualität", "qualité", "calidad", "qualità", "qualidade")
    elif section_n == "speed":
        section_tokens = ("скорость воспроизведения", "playback speed", "скорость", "speed", "wiedergabegeschwindigkeit", "vitesse de lecture", "velocidad de reproducción", "velocità di riproduzione", "velocidade de reprodução")
    else:
        raise ValueError(section)
    _youtube_click_named(tab, section_tokens, control_types=("MenuItem", "ListItem", "Button", "Text", "Custom"), timeout=2.0)
    raw = str(value or "").strip().casefold()
    if section_n == "quality":
        if raw in {"auto", "авто", "automatic"}:
            option_tokens = ("авто", "auto", "automatisch", "automatique", "automático", "automatica")
        else:
            digits = re.sub(r"[^0-9]", "", raw)
            if not digits:
                raise RuntimeError("Не указано качество YouTube")
            option_tokens = (f"{digits}p", digits)
    else:
        if raw in {"1", "1.0", "normal", "обычная", "нормальная"}:
            option_tokens = ("обычная", "normal", "normalerweise", "normale")
        else:
            normalized_speed = raw.replace(",", ".").rstrip("0").rstrip(".")
            option_tokens = (normalized_speed,)
    _item, chosen = _youtube_click_named(tab, option_tokens, control_types=("MenuItem", "ListItem", "Button", "Text", "Custom"), timeout=2.2)
    return {
        "target": "youtube", "domain": "video", "action": f"set_{section_n}",
        "value": value, "selected": chosen, "method": "youtube_uia_settings_menu",
        "verified": True, "input_stolen": True, "tab": tab.title, "tab_index": tab.index,
        "url": _youtube_current_url(tab), "fingerprint": _page_fingerprint(tab.window),
    }


def _youtube_keyboard_navigation(tab: BrowserTab, action: str) -> dict[str, Any]:
    before = _youtube_current_url(tab)
    next_tokens = ("следующее видео", "next video", "nächstes video", "vidéo suivante", "video siguiente", "video successivo", "próximo vídeo", "próximo video")
    prev_tokens = ("предыдущее видео", "previous video", "vorheriges video", "vidéo précédente", "video anterior", "video precedente", "vídeo anterior")
    try:
        _youtube_show_controls(tab)
        _item, chosen = _youtube_click_named(tab, next_tokens if action == "next" else prev_tokens,
            exclude=("рекомен", "recommended"), control_types=("Button",), timeout=0.75)
        after = _youtube_wait_url_change(tab, before, timeout=1.45)
        if after and after != before and _url_matches_service(after, "youtube"):
            return {"target":"youtube","domain":"video","action":action,"method":"youtube_uia_navigation",
                    "control":chosen,"verified":True,"input_stolen":True,"url_before":before,"url":after,"tab_index":tab.index}
    except Exception:
        pass
    _youtube_focus_player(tab)
    import pyautogui
    if action == "next":
        pyautogui.hotkey("shift", "n"); method = "youtube_shift_n"
    else:
        pyautogui.hotkey("shift", "p"); method = "youtube_shift_p"
    after = _youtube_wait_url_change(tab, before, timeout=1.55)
    if after and after != before and _url_matches_service(after, "youtube"):
        return {"target":"youtube","domain":"video","action":action,"method":method,"verified":True,"input_stolen":True,"url_before":before,"url":after,"tab_index":tab.index}
    if action == "previous":
        pyautogui.hotkey("alt", "left")
        hist = _youtube_wait_url_change(tab, before, timeout=1.25)
        if hist and hist != before and ("youtube.com/watch" in hist.casefold() or "youtube.com/shorts/" in hist.casefold() or "youtu.be/" in hist.casefold()):
            return {"target":"youtube","domain":"video","action":action,"method":"youtube_history_previous_video","verified":True,"input_stolen":True,"url_before":before,"url":hist,"tab_index":tab.index}
        if hist and hist != before:
            try: pyautogui.hotkey("alt", "right")
            except Exception: pass
    raise RuntimeError("YouTube did not confirm navigation to another video")


def _youtube_keyboard_seek(tab: BrowserTab, action: str, seconds: float) -> dict[str, Any]:
    """Relative YouTube seek as CURRENT_POSITION +/- delta, never absolute delta.

    HOTFIX101 fixes the observed +15 -> 00:15 bug by reading the current
    position first and delegating to the verified absolute-seek implementation.
    Player-key shortcuts remain a last-resort fallback only when no timeline can
    be read at all.
    """
    requested = max(1, min(3600, int(round(abs(float(seconds or 10))))))
    forward = action == "seek_forward"
    before = _youtube_current_position(tab)
    if before >= 0:
        target = max(0.0, before + (requested if forward else -requested))
        result = _youtube_seek_absolute(tab, target)
        return {**result, "action": action, "method": "youtube_relative_current_plus_delta/" + str(result.get("method", "")),
                "seconds_requested": requested, "position_before": round(before, 2),
                "position_target": round(target, 2), "relative_seek": True}

    # No readable timeline: use YouTube's own relative keyboard commands. This
    # branch is intentionally marked effect-unverified rather than pretending an
    # absolute 00:SS jump is correct.
    _youtube_focus_player(tab)
    import pyautogui
    tens, remainder = divmod(requested, 10)
    key10 = "l" if forward else "j"
    key5 = "right" if forward else "left"
    for _ in range(min(tens, 360)):
        pyautogui.press(key10)
    if remainder >= 8:
        pyautogui.press(key10)
    elif remainder >= 3:
        pyautogui.press(key5)
    return {"target":"youtube","domain":"video","action":action,"method":"youtube_relative_keys_fallback",
            "seconds_requested":requested,"verified":True,"effect_verified":False,"command_sent":True,
            "relative_seek":True,"input_stolen":True,"tab":tab.title,"tab_index":tab.index,
            "url":_youtube_current_url(tab),"fingerprint":_page_fingerprint(tab.window)}


def _clock_to_seconds(value: str) -> float | None:
    parts = [p for p in str(value or "").strip().split(":") if p != ""]
    if len(parts) not in {2, 3} or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 2:
        return float(nums[0] * 60 + nums[1])
    return float(nums[0] * 3600 + nums[1] * 60 + nums[2])


def _position_from_youtube_slider_name(name: str) -> float:
    """Return current YouTube position from an accessibility seek-slider label."""
    clocks = re.findall(r"(?<!\d)(?:\d{1,2}:)?\d{1,3}:\d{2}(?!\d)", str(name or ""))
    if not clocks:
        return -1.0
    parsed = _clock_to_seconds(clocks[0])
    return float(parsed) if parsed is not None else -1.0


def _youtube_current_position(tab: BrowserTab) -> float:
    """Read the real current position without moving the video."""
    try:
        info = _read_exact_tab_media_session(tab, "video")
        value = info.get("position_seconds")
        if value is not None and float(value) >= 0:
            return float(value)
    except Exception:
        pass
    try:
        for slider in list(tab.window.descendants(control_type="Slider"))[:80]:
            name = _slider_name(slider)
            if _is_volume_slider(name):
                continue
            value = _position_from_youtube_slider_name(name)
            if value >= 0:
                return value
    except Exception:
        pass
    return -1.0


def _duration_from_youtube_slider_name(name: str) -> float:
    # Chromium often exposes labels such as “Seek slider, 1:24 of 12:03” or
    # localized equivalents. The last clock token is the total duration.
    clocks = re.findall(r"(?<!\d)(?:\d{1,2}:)?\d{1,3}:\d{2}(?!\d)", str(name or ""))
    for token in reversed(clocks):
        parsed = _clock_to_seconds(token)
        if parsed and parsed > 0:
            return parsed
    return 0.0


def _youtube_seek_absolute(tab: BrowserTab, seconds: float) -> dict[str, Any]:
    requested = max(0.0, min(24*3600.0, float(seconds or 0.0)))
    # Prefer exact Windows timeline positioning when Chromium exposes it.
    try:
        data = _control_exact_tab_media_session(tab, "seek_to", "video", seconds=requested)
        return {**data, "target":"youtube", "input_stolen":False}
    except Exception:
        pass
    # Accessibility fallback: use YouTube progress/seek slider ratio.
    _youtube_focus_player(tab)
    duration = 0.0
    try:
        info = _read_exact_tab_media_session(tab, "video")
        duration = float(info.get("duration_seconds", 0.0) or 0.0)
    except Exception:
        pass
    sliders = []
    try:
        sliders = list(tab.window.descendants(control_type="Slider"))
    except Exception:
        pass
    for slider in sliders[:80]:
        name = _slider_name(slider)
        if _is_volume_slider(name):
            continue
        if duration <= 0:
            duration = _duration_from_youtube_slider_name(name)
        try:
            iface = slider.iface_range_value
            minimum = float(iface.CurrentMinimum); maximum = float(iface.CurrentMaximum)
            if maximum <= minimum:
                continue
            if duration <= 0 and maximum > 100:
                duration = maximum - minimum
            if duration <= 0:
                continue
            ratio = max(0.0, min(1.0, requested / duration))
            target_value = minimum + (maximum-minimum) * ratio
            try:
                iface.SetValue(target_value)
                time.sleep(0.35)
                return {"target":"youtube","domain":"video","action":"seek_to","seconds":requested,"method":"youtube_uia_seek_slider","verified":True,"input_stolen":True,"tab":tab.title,"tab_index":tab.index,"url":_youtube_current_url(tab)}
            except Exception:
                rect = slider.rectangle()
                import pyautogui
                x = int(rect.left + max(2, min(max(3, rect.right-rect.left-2), (rect.right-rect.left)*ratio)))
                y = int((rect.top+rect.bottom)/2)
                pyautogui.click(x,y)
                time.sleep(0.35)
                return {"target":"youtube","domain":"video","action":"seek_to","seconds":requested,"method":"youtube_progress_click","verified":True,"input_stolen":True,"tab":tab.title,"tab_index":tab.index,"url":_youtube_current_url(tab)}
        except Exception:
            continue
    raise RuntimeError("Не удалось получить управляемую шкалу времени YouTube")


def control_youtube_shorts(direction: str, browser: str = "") -> dict[str, Any]:
    """Navigate Shorts; prefer YouTube accessibility controls, then arrow fallback."""
    tab = _youtube_active_tab(browser)
    before = _youtube_current_url(tab)
    if "/shorts/" not in before.casefold():
        raise RuntimeError("The current YouTube tab is not in the Shorts player")
    tokens_next = ("следующее видео", "next video", "next short", "nächstes video", "vidéo suivante", "video siguiente", "video successivo", "próximo vídeo")
    tokens_prev = ("предыдущее видео", "previous video", "previous short", "vorheriges video", "vidéo précédente", "video anterior", "video precedente", "vídeo anterior")
    try:
        _youtube_show_controls(tab)
        _item, chosen = _youtube_click_named(tab, tokens_next if direction == "next" else tokens_prev,
            control_types=("Button",), timeout=0.65)
        after = _youtube_wait_url_change(tab, before, timeout=1.3)
        if after and after != before and "/shorts/" in after.casefold():
            return {"target":"youtube","domain":"video","action":f"shorts_{direction}","method":"youtube_shorts_uia",
                    "control":chosen,"verified":True,"input_stolen":True,"url_before":before,"url":after,"tab_index":tab.index}
    except Exception:
        pass
    _youtube_focus_player(tab)
    import pyautogui
    pyautogui.press("down" if direction == "next" else "up")
    after = _youtube_wait_url_change(tab, before, timeout=1.55)
    if not after or after == before or "/shorts/" not in after.casefold():
        raise RuntimeError("YouTube Shorts did not confirm navigation to another Short")
    return {"target":"youtube","domain":"video","action":f"shorts_{direction}","method":"youtube_shorts_arrow",
            "verified":True,"input_stolen":True,"url_before":before,"url":after,"tab_index":tab.index}


async def _control_unique_browser_media_session_async(action: str, browser: str = "", seconds: float = 10.0) -> dict[str, Any] | None:
    """Cold-start YouTube transport via the live Windows GSMTC session list.

    The manager is requested at command time, so this works when YouTube was open
    before NEO launched and NEO has no remembered session. Selection remains
    background-only: no tab activation, focus, mouse, or keyboard input.

    If more than one browser media session exists, passive YouTube tab titles/URLs
    are used to rank the sessions. We never guess between ambiguous candidates.
    """
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager

    requested_family = _browser_identity_family(browser)

    # Read browser tabs passively. This is deliberately best-effort: GSMTC can still
    # work when UI Automation is temporarily unavailable, provided the media session
    # itself carries explicit YouTube evidence.
    youtube_contexts: list[dict[str, Any]] = []
    try:
        for tab in list_browser_tabs(browser):
            known_url = tab.url or _cached_tab_url(tab)
            if known_url:
                tab.url = known_url
            if not ((known_url and _url_matches_service(known_url, "youtube")) or _explicit_title_match(tab, "youtube")):
                continue
            youtube_contexts.append({
                "index": int(tab.index or 0),
                "title": tab.title,
                "url": tab.url,
                "browser": tab.process_name,
                "window_handle": int(getattr(tab.window, "handle", 0) or 0),
                "fingerprint": "",  # never inspect/switch a background document here
            })
    except Exception:
        youtube_contexts = []

    manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
    sessions = list(manager.get_sessions() or [])
    candidates: list[tuple[Any, dict[str, Any], float]] = []
    blocked = ("yandex", "яндекс", "spotify", "вконтакте", "vk music", "soundcloud")
    for session in sessions:
        try:
            source = str(getattr(session, "source_app_user_model_id", "") or "")
            family = _browser_identity_family(source)
            if requested_family and family and family != requested_family:
                continue
            # Ignore known non-browser sessions. Opaque browser AUMIDs are still
            # allowed when passive YouTube evidence can correlate them below.
            if not family and requested_family and source and not _media_source_can_match_browser(source, requested_family):
                continue
            props = await session.try_get_media_properties_async()
            playback = session.get_playback_info()
            info = {
                "source": source,
                "browser_family": family,
                "title": str(getattr(props, "title", "") or ""),
                "artist": str(getattr(props, "artist", "") or ""),
                "album": str(getattr(props, "album_title", "") or ""),
                "status": str(getattr(getattr(playback, "playback_status", None), "name", getattr(playback, "playback_status", "")) or ""),
            }
            haystack = f"{info['title']} {info['artist']} {info['album']} {source}".casefold()
            if any(token in haystack for token in blocked):
                continue
            score = 0.0
            for context in youtube_contexts:
                score = max(score, media_session_tab_score(info, context, "video"))
            # Some browser builds expose YouTube in the session source/album even if
            # UIA cannot enumerate tabs. This is explicit evidence, not a guess.
            if "youtube" in haystack or "youtu.be" in haystack or "ютуб" in haystack:
                score = max(score, 0.94)
            candidates.append((session, info, score))
        except Exception:
            continue

    if not candidates:
        return None

    # Prefer a uniquely correlated YouTube session. If metadata is generic but there
    # is exactly one compatible browser session AND at least one passive YouTube tab,
    # that is safe enough: there is no alternative browser media target to confuse it with.
    candidates.sort(key=lambda item: item[2], reverse=True)
    selected: tuple[Any, dict[str, Any], float] | None = None
    if candidates[0][2] >= 0.88 and (len(candidates) == 1 or candidates[0][2] - candidates[1][2] >= 0.07):
        selected = candidates[0]
    elif (len(candidates) == 1 and youtube_contexts and candidates[0][2] >= 0.5
          and candidates[0][1].get("browser_family")):
        # A lone generic browser session is used only with partial positive
        # evidence; score 0 with an unknown family is no identity at all.
        selected = candidates[0]
    if selected is None:
        return None

    session, info, score = selected
    status_before = normalize(str(info.get("status", "") or ""))
    if action == "status":
        state = "playing" if "playing" in status_before else "paused" if "paused" in status_before else status_before
        return {**info, "state": state, "verified": bool(state), "score": round(score, 3), "method": "cold_youtube_gsmtc", "cold_discovery": True}

    if action in {"seek_forward", "seek_backward", "seek_to"}:
        try:
            timeline = session.get_timeline_properties()
            pos = getattr(timeline, "position", None)
            end = getattr(timeline, "end_time", None)
            if not hasattr(pos, "total_seconds"):
                return None
            before = float(pos.total_seconds())
            requested = max(0.0, float(seconds or 0.0))
            if action == "seek_forward":
                target = before + max(1.0, min(3600.0, abs(requested or 10.0)))
            elif action == "seek_backward":
                target = before - max(1.0, min(3600.0, abs(requested or 10.0)))
            else:
                target = requested
            target = max(0.0, target)
            duration = 0.0
            if hasattr(end, "total_seconds"):
                duration = max(0.0, float(end.total_seconds()))
                if duration > 0:
                    target = min(target, max(0.0, duration - 0.25))
            method = getattr(session, "try_change_playback_position_async", None)
            if not callable(method):
                return None
            ok = await method(int(target * 10_000_000))
            if ok is False:
                return None
            observed = before
            verified = False
            for _ in range(22):
                await asyncio.sleep(0.07)
                try:
                    after = session.get_timeline_properties()
                    after_pos = getattr(after, "position", None)
                    if hasattr(after_pos, "total_seconds"):
                        observed = float(after_pos.total_seconds())
                except Exception:
                    continue
                if abs(observed - target) <= 3.0:
                    verified = True
                    break
            if not verified:
                return None
            return {
                **info, "action": action, "position_before": round(before, 2),
                "position_target": round(target, 2), "position_after": round(observed, 2),
                "duration_seconds": round(duration, 2) if duration > 0 else 0.0,
                "verified": True, "control_success": True, "score": round(score, 3),
                "method": "cold_youtube_gsmtc_seek", "cold_discovery": True,
                "background_control": True, "input_stolen": False,
            }
        except Exception:
            return None

    if action in {"next", "previous"}:
        data = await _skip_media_session187(session, action, info)
        return {**data, "score": round(score, 3), "method": "cold_youtube_gsmtc_skip", "cold_discovery": True}

    method_name = {
        "play": "try_play_async", "pause": "try_pause_async", "stop": "try_pause_async",
        "play_pause": "try_toggle_play_pause_async", "next": "try_skip_next_async", "previous": "try_skip_previous_async",
    }.get(action)
    if not method_name or not hasattr(session, method_name):
        return None
    ok = await getattr(session, method_name)()
    if ok is False:
        return None

    # TryPlay/TryPause are idempotent and act immediately. Verification is read-only
    # and intentionally short so command latency stays low while still catching a
    # rejected/stale session. Two desired samples are enough on this cold path.
    verified = action not in {"play", "pause", "stop"}
    status_after = ""
    if action in {"play", "pause", "stop"}:
        desired = ("playing",) if action == "play" else ("paused", "stopped", "closed")
        stable = 0
        for _ in range(14):
            await asyncio.sleep(0.06)
            try:
                pb = session.get_playback_info()
                status_after = normalize(str(getattr(getattr(pb, "playback_status", None), "name", getattr(pb, "playback_status", "")) or ""))
            except Exception:
                continue
            if any(token in status_after for token in desired):
                stable += 1
                if stable >= 2:
                    verified = True
                    break
            else:
                stable = 0
    if not verified:
        return None
    return {
        **info, "status_before": status_before, "status_after": status_after,
        "verified": True, "control_success": True, "score": round(score, 3),
        "method": "cold_youtube_gsmtc", "cold_discovery": True,
    }


def _control_unique_browser_media_session(action: str, browser: str = "", seconds: float = 10.0) -> dict[str, Any] | None:
    if os.name != "nt":
        return None
    try:
        return _run_winrt_coro(_control_unique_browser_media_session_async(action, browser, seconds),
                               timeout=5.5 if action in {"next", "previous"} else 2.2)
    except (VideoNavigationUnavailable, MediaVerificationError):
        raise  # NEO 187: an answered navigation request is a result, not «no session»
    except Exception:
        logger.debug("Cold browser GSMTC discovery failed", exc_info=True)
        return None


def control_service_media(action: str, target: str, browser: str = "", seconds: float = 10.0) -> dict[str, Any]:
    if os.name != "nt":
        raise OSError("Управление вкладкой доступно только в Windows")
    action = "pause" if action == "stop" else str(action or "")
    target = canonical_service(target)
    # Transport stays background-only through GSMTC.  HOTFIX105 restores the two
    # explicitly requested visual-layout controls (fullscreen/theater) via the
    # exact YouTube tab and UI Automation. No blind mouse coordinates or global
    # keyboard shortcuts are used for these layout actions.
    # NEO 187 HREQ-026: next/previous video return through the same verified
    # background session (title must change); still no keyboard or mouse.
    youtube_background = {"play", "pause", "play_pause", "seek_forward", "seek_backward", "seek_to", "status", "next", "previous"}
    youtube_layout = {"fullscreen", "theater"}
    youtube_removed = {"shorts_next", "shorts_previous", "set_speed", "set_quality"}
    if target == "youtube":
        if action in youtube_removed:
            raise RuntimeError(
                "Эта команда YouTube отключена: её нельзя гарантированно выполнить безопасно. "
                "Доступны поиск/открытие видео, пауза, продолжение, проверяемая перемотка, "
                "полноэкранный и театральный режим."
            )
        if action in youtube_layout:
            tab = activate_service_tab("youtube", browser) or (None if browser else activate_service_tab("youtube", ""))
            if tab is None:
                raise RuntimeError("Не нашёл открытую вкладку YouTube для изменения режима просмотра")
            selected, index = _selected_tab_with_index(tab.window)
            if selected is not None:
                tab.wrapper = selected; tab.index = index
            fresh = _strict_selected_tab_url(tab.window, timeout=0.75)
            if fresh:
                tab.url = fresh; _cache_tab_url(tab, fresh)
            data = _control_media_on_tab(tab, action, "youtube", seconds=seconds)
            return {**data, "background_control": False, "input_stolen": True, "visual_layout_action": True, "target": "youtube"}
        if action in youtube_background:
            # HIST-028: a browser the user named stays the scope on every path;
            # only an unnamed request may fall back to any open browser.
            passive = (
                find_service_tab_passive("youtube", browser)
                or (None if browser else find_service_tab_passive("youtube", ""))
                or _find_youtube_tab_from_live_sessions(browser)
                or (None if browser else _find_youtube_tab_from_live_sessions(""))
            )
            if passive is not None:
                try:
                    if action == "status":
                        return {**_read_exact_tab_media_session(passive, "video"), "background_control": True, "input_stolen": False, "target": "youtube"}
                    return {**_control_exact_tab_media_session(passive, action, "video", seconds=seconds), "background_control": True, "input_stolen": False, "target": "youtube"}
                except (MediaVerificationError, VideoNavigationUnavailable):
                    raise
                except Exception:
                    logger.debug("Exact YouTube GSMTC path unavailable; trying cold background discovery", exc_info=True)
            cold = _control_unique_browser_media_session(action, browser, seconds)
            if cold is not None:
                return {**cold, "background_control": True, "input_stolen": False, "target": "youtube"}
            raise RuntimeError(
                "Не найдено однозначное управляемое YouTube media session. "
                "NEO не будет использовать клавиатуру или мышь как запасной способ."
            )

    # Non-media browser operations may still intentionally work with a selected
    # page.  Media integrations never reach this path for YouTube transport.
    tab = activate_service_tab(target, browser)
    if tab is None:
        raise RuntimeError(f"Не нашёл открытую вкладку для «{target or 'плеера'}»")
    selected, index = _selected_tab_with_index(tab.window)
    if selected is not None:
        tab.wrapper = selected
        tab.index = index
    fresh = _strict_selected_tab_url(tab.window, timeout=0.75)
    if fresh:
        tab.url = fresh
        _cache_tab_url(tab, fresh)
    return _control_media_on_tab(tab, action, target, seconds=seconds)




def selected_browser_tab(browser: str = "") -> BrowserTab | None:
    """Return the currently selected tab with a stable ordinal and fresh URL."""
    window = _foreground_browser_window(browser)
    if window is None:
        return None
    selected, index = _selected_tab_with_index(window)
    if selected is None:
        return None
    try:
        title = str(selected.window_text() or selected.element_info.name or "").strip()
    except Exception:
        title = ""
    process = _process_name(int(window.element_info.process_id))
    url = _strict_selected_tab_url(window, timeout=0.75)
    tab = BrowserTab(title, process, str(window.window_text() or ""), selected, window, index=index, url=url)
    if url:
        _cache_tab_url(tab, url)
    return tab




def selected_browser_tab_context(browser: str = "") -> dict[str, Any]:
    """Return stable metadata for the selected browser tab without changing tabs."""
    tab = selected_browser_tab(browser)
    if tab is None:
        raise RuntimeError("Не вижу выбранную вкладку браузера")
    return {
        "index": int(tab.index or 0),
        "title": tab.title,
        "url": tab.url,
        "browser": tab.process_name,
        "window_handle": int(getattr(tab.window, "handle", 0) or 0),
        "fingerprint": _page_fingerprint(tab.window),
    }


def browser_tab_context_by_index(index: int, browser: str = "") -> dict[str, Any]:
    data = activate_tab_by_index(index, browser)
    window = _foreground_browser_window(browser)
    if window is None:
        raise RuntimeError("После переключения потеряно окно браузера")
    selected, selected_index = _selected_tab_with_index(window)
    if selected is None or selected_index != int(index):
        raise RuntimeError(f"Активна вкладка {selected_index or '?'}, а ожидалась {index}")
    title = str(data.get("title", "") or "")
    url = str(data.get("url", "") or "")
    # One final fresh URL read. If Firefox hides the address value, title and page
    # fingerprint still provide safe tab-local evidence without touching other tabs.
    fresh = _strict_selected_tab_url(window, timeout=0.55)
    if fresh:
        url = fresh
    return {
        **data,
        "index": int(index),
        "title": title,
        "url": url,
        "fingerprint": _page_fingerprint(window),
    }


def media_session_tab_score(info: dict[str, Any], context: dict[str, Any], domain: str) -> float:
    """Correlate one GSMTC session with one *exact* browser tab.

    `GetCurrentSession()` is not an identity API. A browser can expose several
    sessions, so fallback is permitted only when media title/artist is visibly
    present in the exact tab, or the YouTube session title closely matches that
    tab's own title. This prevents video commands from starting music and vice versa.
    """
    domain = canonical_service(domain)
    title = normalize(str(context.get("title", "") or ""))
    url = str(context.get("url", "") or "").casefold()
    fingerprint = normalize(str(context.get("fingerprint", "") or ""))
    media_title = normalize(str(info.get("title", "") or ""))
    artist = normalize(str(info.get("artist", "") or ""))
    source = normalize(str(info.get("source", "") or ""))

    title_youtube = "youtube" in title or "ютуб" in title
    title_music = any(x in title for x in ("моя музыка", "яндекс музыка", "yandex music", "вконтакт", " vk "))
    # For an exact ordinal tab, the TabItem title belongs to that tab while Firefox's
    # address-bar value may lag briefly after selection. Strong title evidence therefore
    # wins a transient URL conflict instead of reviving the RC23 stale-URL bug.
    is_youtube_tab = title_youtube or (_url_matches_service(url, "youtube") and not title_music)
    is_music_tab = title_music or (_url_matches_service(url, "music") and not title_youtube)

    if domain in {"video", "youtube"}:
        if not is_youtube_tab:
            return 0.0
        clean_title = re.sub(r"\\s*[-—|]\\s*(?:youtube|ютуб).*$", "", title).strip()
        score = 0.0
        if media_title and clean_title:
            if media_title in clean_title or clean_title in media_title:
                score = 0.98
            else:
                ratio = difflib.SequenceMatcher(None, media_title, clean_title).ratio()
                if ratio >= 0.68:
                    score = 0.70 + (ratio - 0.68) * 0.80
        if media_title and media_title in fingerprint:
            score = max(score, 0.96)
        # Artist alone is not enough for a video session: an artist/channel name can
        # easily appear in recommendations while a different media source is active.
        # A source containing YouTube is useful evidence, but never sufficient alone.
        if score and ("youtube" in source or "ютуб" in source):
            score = min(1.0, score + 0.03)
        return min(1.0, score)

    if domain in {"music", "вконтакте", "яндекс музыка"}:
        if not is_music_tab:
            return 0.0
        score = 0.0
        # The track title is mandatory evidence.  A common artist name by itself is
        # too weak when several Firefox media sessions exist at the same time.
        if media_title and media_title in fingerprint:
            score = 0.97
            if artist and artist in fingerprint:
                score = 1.0
        # Concrete URL adds confidence but does not identify the song/session itself.
        if score and domain == "вконтакте" and _url_matches_service(url, "вконтакте"):
            score = min(1.0, score + 0.02)
        if score and domain == "яндекс музыка" and _url_matches_service(url, "яндекс музыка"):
            score = min(1.0, score + 0.02)
        return score
    return 0.0



def _run_winrt_coro(coro, timeout: float = 5.0):
    """Run a WinRT coroutine away from any Qt/worker event loop."""
    result: list[Any] = []
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=runner, name="jarvis-browser-winrt", daemon=True)
    thread.start()
    thread.join(max(0.8, float(timeout)))
    if thread.is_alive():
        raise TimeoutError("Windows media API не ответил вовремя")
    if errors:
        raise errors[0]
    return result[0] if result else None



def _browser_identity_family(value: str) -> str:
    """Return a coarse browser family from an exe name or Windows AppUserModelID.

    ``GlobalSystemMediaTransportControlsSession.SourceAppUserModelId`` is an
    AppUserModelID, not an executable path.  Desktop browsers often expose something
    readable (for example ``firefox.exe``), but packaged/registered builds may expose
    an opaque identifier.  An opaque AUMID must therefore *not* be treated as proof
    that the session belongs to another browser.
    """
    raw = str(value or "").casefold().replace("\\", "/")
    name = raw.rsplit("/", 1)[-1]
    compact = re.sub(r"[^a-z0-9]+", "", name)
    families = (
        (("firefox",), "firefox"),
        (("brave",), "brave"),
        (("vivaldi",), "vivaldi"),
        (("opera", "operagx"), "opera"),
        (("msedge", "microsoftedge", "edge"), "edge"),
        (("chrome", "googlechrome", "chromium"), "chrome"),
    )
    for tokens, family in families:
        if any(token in compact for token in tokens):
            return family
    return ""


def _lone_session_is_tab_identity(info: dict[str, Any], expected_process: str, tab_title: str) -> bool:
    source_family = _browser_identity_family(str(info.get("source", "") or ""))
    tab_family = _browser_identity_family(expected_process)
    if not source_family or not tab_family or source_family != tab_family:
        return False
    title = " ".join(str(info.get(key, "") or "") for key in ("title", "artist", "album")).casefold().strip()
    if not title or "youtube" in title or "youtu.be" in title:
        return True
    tab = str(tab_title or "").casefold()
    # Generic browser metadata («Firefox», the browser's own name) is not
    # evidence of another player; a specific different title is.
    generic = {"chrome", "google chrome", "firefox", "mozilla firefox", "microsoft edge", "edge", "opera", "brave", "vivaldi"}
    return title in generic or (len(title) >= 4 and title in tab)


def _media_source_can_match_browser(source: str, process_name: str) -> bool:
    """Reject a *known* different browser, but allow opaque Windows AUMIDs.

    Media title/fingerprint matching still has to clear the strict score and ambiguity
    thresholds below.  This only prevents a false negative caused by assuming that an
    AppUserModelID must contain the executable filename.
    """
    expected_family = _browser_identity_family(process_name)
    source_family = _browser_identity_family(source)
    if expected_family and source_family:
        return expected_family == source_family
    return True

async def _find_youtube_tab_from_live_sessions_async(browser: str = "") -> BrowserTab | None:
    """Resolve an already-open YouTube tab from live GSMTC sessions, read-only.

    The session manager is requested at *command time*, not cached at NEO startup.
    Therefore a YouTube video that was already open/playing/paused before NEO
    launched can still be discovered.  No tab is selected and no browser is focused.
    """
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager

    tabs = list_browser_tabs(browser)
    candidates: list[BrowserTab] = []
    seen: set[tuple[int, int, str]] = set()
    for tab in tabs:
        known_url = tab.url or _cached_tab_url(tab)
        if known_url:
            tab.url = known_url
        if not ((known_url and _url_matches_service(known_url, "youtube")) or _explicit_title_match(tab, "youtube")):
            continue
        key = (int(getattr(tab.window, "handle", 0) or 0), int(tab.index or 0), normalize(tab.title))
        if key not in seen:
            seen.add(key)
            candidates.append(tab)
    if not candidates:
        return None

    manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
    sessions = list(manager.get_sessions() or [])
    if not sessions:
        return candidates[0] if len(candidates) == 1 else None

    session_info: list[tuple[Any, dict[str, Any]]] = []
    for session in sessions:
        try:
            props = await session.try_get_media_properties_async()
            playback = session.get_playback_info()
            session_info.append((session, {
                "source": str(getattr(session, "source_app_user_model_id", "") or ""),
                "title": str(getattr(props, "title", "") or ""),
                "artist": str(getattr(props, "artist", "") or ""),
                "album": str(getattr(props, "album_title", "") or ""),
                "status": str(getattr(getattr(playback, "playback_status", None), "name", getattr(playback, "playback_status", "")) or ""),
            }))
        except Exception:
            continue

    ranked: list[tuple[float, BrowserTab]] = []
    for tab in candidates:
        context = {
            "index": int(tab.index or 0),
            "title": tab.title,
            "url": tab.url,
            "browser": tab.process_name,
            "window_handle": int(getattr(tab.window, "handle", 0) or 0),
            # Fingerprint belongs to the selected document and may not describe a
            # background tab. Title/URL correlation is safer here.
            "fingerprint": "",
        }
        expected_process = Path(str(tab.process_name or "")).name.casefold()
        best = 0.0
        for _session, info in session_info:
            if expected_process and not _media_source_can_match_browser(str(info.get("source", "") or ""), expected_process):
                continue
            best = max(best, media_session_tab_score(info, context, "video"))
        if best > 0:
            ranked.append((best, tab))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if ranked and ranked[0][0] >= 0.88:
        if len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.07:
            return ranked[0][1]
    # A lone YouTube tab is safe even if Windows exposes an empty/opaque media title.
    return candidates[0] if len(candidates) == 1 else None


def _find_youtube_tab_from_live_sessions(browser: str = "") -> BrowserTab | None:
    if os.name != "nt":
        return None
    try:
        return _run_winrt_coro(_find_youtube_tab_from_live_sessions_async(browser), timeout=5.0)
    except Exception:
        logger.debug("Live-session YouTube resolver failed", exc_info=True)
        return None


async def _find_exact_tab_media_session_async(tab: BrowserTab, domain: str, *, strict_match: bool = False):
    """Return the uniquely matched GSMTC session for *tab* without changing it.

    The important property of this helper is that it is read-only.  It can therefore
    be used before/after a UIA click to verify state without risking a second toggle.
    """
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager

    context = {
        "index": int(tab.index or 0),
        "title": tab.title,
        "url": tab.url,
        "browser": tab.process_name,
        "window_handle": int(getattr(tab.window, "handle", 0) or 0),
        "fingerprint": _page_fingerprint(tab.window),
    }
    manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
    sessions = list(manager.get_sessions() or [])
    ranked: list[tuple[float, Any, dict[str, Any]]] = []
    compatible: list[tuple[Any, dict[str, Any]]] = []
    expected_process = Path(str(tab.process_name or "")).name.casefold()
    for session in sessions:
        try:
            props = await session.try_get_media_properties_async()
            playback = session.get_playback_info()
            info = {
                "source": str(getattr(session, "source_app_user_model_id", "") or ""),
                "title": str(getattr(props, "title", "") or ""),
                "artist": str(getattr(props, "artist", "") or ""),
                "album": str(getattr(props, "album_title", "") or ""),
                "status": str(getattr(getattr(playback, "playback_status", None), "name", getattr(playback, "playback_status", "")) or ""),
            }
            # SourceAppUserModelId is an AppUserModelID, not necessarily an exe name.
            # Reject only when both sides reveal a known browser family and they differ.
            # Opaque AUMIDs are allowed through to the strict title/fingerprint scorer;
            # ambiguity detection below still prevents us from guessing between sessions.
            if expected_process and not _media_source_can_match_browser(str(info["source"] or ""), expected_process):
                continue
            compatible.append((session, info))
            score = media_session_tab_score(info, context, domain)
            if score > 0:
                ranked.append((score, session, info))
        except Exception:
            continue

    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked or ranked[0][0] < 0.90:
        # Chromium/Firefox occasionally expose a generic GSMTC title while the
        # actual selected YouTube tab is still known through passive UIA. If there
        # is exactly one browser-compatible media session, it is safe to use it:
        # no tab activation, mouse movement or keyboard input is involved.
        tab_hint = f"{tab.title} {tab.url}".casefold()
        confirmed_youtube = ("youtube" in tab_hint) or ("youtu.be" in tab_hint)
        if not strict_match and domain == "video" and confirmed_youtube and len(compatible) == 1:
            session, info = compatible[0]
            # VIDEO185-SELECTOR-IDENTITY-01: a lone candidate is accepted only
            # with positive identity — the same known browser family as the
            # tab — and metadata that does not name some other media.
            if _lone_session_is_tab_identity(info, expected_process, tab.title):
                return 0.905, session, info, context
        raise RuntimeError("Не удалось однозначно сопоставить YouTube-вкладку с её Windows media session")
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.07:
        raise RuntimeError("Нашёл несколько похожих media sessions и не буду угадывать")
    score, session, info = ranked[0]
    return score, session, info, context


async def _read_exact_tab_media_session_async(tab: BrowserTab, domain: str, *, strict_match: bool = False) -> dict[str, Any]:
    """Read the exact selected tab's media state without issuing any command."""
    score, session, info, context = await _find_exact_tab_media_session_async(tab, domain, strict_match=True) if strict_match else await _find_exact_tab_media_session_async(tab, domain)
    try:
        timeline = session.get_timeline_properties()
        pos = getattr(timeline, "position", None)
        end = getattr(timeline, "end_time", None)
        if hasattr(pos, "total_seconds"):
            info["position_seconds"] = float(pos.total_seconds())
        if hasattr(end, "total_seconds"):
            info["duration_seconds"] = float(end.total_seconds())
    except Exception:
        pass
    # Normalize WinRT PlaybackStatus into the same state fields used by the
    # rest of NEO.  This keeps passive YouTube status probes useful for
    # quick commands while remaining completely input-free.
    status_name = str(info.get("status", "") or "").strip().casefold()
    if "playing" in status_name:
        info.update({"state": "playing", "playing": True, "paused": False})
    elif "paused" in status_name:
        info.update({"state": "paused", "playing": False, "paused": True})
    elif any(token in status_name for token in ("stopped", "closed")):
        info.update({"state": "stopped", "playing": False, "paused": False})
    return {**context, **info, "method": "exact_tab_gsmtc_read", "score": round(score, 3)}


async def _control_exact_tab_media_session_async(tab: BrowserTab, action: str, domain: str, seconds: float = 10.0, *, expected_session: dict | None = None) -> dict[str, Any]:
    """Set an explicit state on the uniquely matched GSMTC session and verify it.

    ``TryPlayAsync`` and ``TryPauseAsync`` are state-setting requests, not a blind
    play/pause toggle.  That makes GSMTC the preferred path for explicit voice
    commands.  A command is considered successful only after PlaybackStatus agrees.
    """
    score, session, info, context = (await _find_exact_tab_media_session_async(tab, domain, strict_match=True)
                                     if expected_session is not None else await _find_exact_tab_media_session_async(tab, domain))
    if expected_session is not None and any(str(info.get(key, '') or '') != value for key, value in expected_session.items()):
        raise RuntimeError("Scheduled video changed; no command sent")

    if action == "seek_to":
        timeline = session.get_timeline_properties()
        pos = getattr(timeline, "position", None)
        end = getattr(timeline, "end_time", None)
        before_position = float(pos.total_seconds()) if hasattr(pos, "total_seconds") else 0.0
        target = max(0.0, float(seconds or 0.0))
        if hasattr(end, "total_seconds"):
            duration = max(0.0, float(end.total_seconds()))
            if duration > 0:
                target = min(target, max(0.0, duration - 0.25))
                info["duration_seconds"] = round(duration, 2)
        method = getattr(session, "try_change_playback_position_async", None)
        if not callable(method):
            raise RuntimeError("Windows media session не поддерживает точный переход по времени")
        ok = await method(int(target * 10_000_000))
        if ok is False:
            raise RuntimeError("Windows media session отклонила точный переход YouTube")
        observed = before_position
        verified = False
        for _ in range(30):
            await asyncio.sleep(0.10)
            try:
                after_timeline = session.get_timeline_properties()
                after_pos = getattr(after_timeline, "position", None)
                if hasattr(after_pos, "total_seconds"):
                    observed = float(after_pos.total_seconds())
            except Exception:
                continue
            if abs(observed - target) <= 3.0:
                verified = True
                break
        if not verified:
            raise MediaVerificationError(
                f"Windows принял переход на {target:.1f} с, но позиция осталась {observed:.1f} с",
                safe_to_fallback=False, state_after=str(observed), command_sent=True,
            )
        return {**context, **info, "method":"exact_tab_gsmtc_seek_to", "verified":True, "score":round(score,3),
                "control_success":True, "action":"seek_to", "position_before":round(before_position,2),
                "position_target":round(target,2), "position_after":round(observed,2)}

    if action in {"seek_forward", "seek_backward"}:
        timeline = session.get_timeline_properties()
        pos = getattr(timeline, "position", None)
        end = getattr(timeline, "end_time", None)
        if not hasattr(pos, "total_seconds"):
            raise RuntimeError("YouTube media session не сообщает текущую позицию видео")
        before_position = float(pos.total_seconds())
        requested = max(1.0, min(3600.0, abs(float(seconds or 10.0))))
        sign = 1.0 if action == "seek_forward" else -1.0
        target = max(0.0, before_position + sign * requested)
        if hasattr(end, "total_seconds"):
            duration = max(0.0, float(end.total_seconds()))
            if duration > 0:
                target = min(target, max(0.0, duration - 0.25))
                info["duration_seconds"] = round(duration, 2)
        method = getattr(session, "try_change_playback_position_async", None)
        if not callable(method):
            raise RuntimeError("Windows media session не поддерживает точную перемотку")
        ok = await method(int(target * 10_000_000))
        if ok is False:
            raise RuntimeError("Windows media session отклонила перемотку YouTube")

        # Do not equate API acceptance with success. Read TimelineProperties until
        # the playhead actually moves in the requested direction / reaches target.
        observed = before_position
        verified = False
        minimum_move = min(max(1.0, requested * 0.25), 5.0)
        for _ in range(30):
            await asyncio.sleep(0.10)
            try:
                after_timeline = session.get_timeline_properties()
                after_pos = getattr(after_timeline, "position", None)
                if not hasattr(after_pos, "total_seconds"):
                    continue
                observed = float(after_pos.total_seconds())
            except Exception:
                continue
            close_to_target = abs(observed - target) <= 3.0
            directional = (observed - before_position >= minimum_move) if sign > 0 else (before_position - observed >= minimum_move)
            if close_to_target or directional:
                verified = True
                break
        if not verified:
            raise MediaVerificationError(
                f"Windows принял перемотку, но позиция видео не изменилась достаточно (было {before_position:.1f}, стало {observed:.1f})",
                safe_to_fallback=False, state_after=str(observed), command_sent=True,
            )
        return {
            **context, **info, "method": "exact_tab_gsmtc_seek", "verified": True,
            "score": round(score, 3), "control_success": True, "action": action,
            "seconds_requested": requested, "position_before": round(before_position, 2),
            "position_target": round(target, 2), "position_after": round(observed, 2),
        }

    # Do not short-circuit explicit YouTube play/pause from the *pre-command*
    # PlaybackStatus. Chromium can publish that status late or for a neighbouring
    # player, which turns an explicit voice command into a false no-op.
    # TryPlayAsync/TryPauseAsync are idempotent state-setting requests, so for the
    # video domain it is safer to issue the requested state and verify afterwards.
    status_n = normalize(str(info.get("status", "")))
    if domain != "video":
        if action == "play" and "playing" in status_n:
            return {**context, **info, "method": "exact_tab_gsmtc", "verified": True, "score": round(score, 3), "already_in_requested_state": True}
        if action in {"pause", "stop"} and any(x in status_n for x in ("paused", "stopped", "closed")):
            return {**context, **info, "method": "exact_tab_gsmtc", "verified": True, "score": round(score, 3), "already_in_requested_state": True}

    if action in {"next", "previous"} and domain == "video":
        return {**context, **(await _skip_media_session187(session, action, info)), "score": round(score, 3)}

    method_name = {
        "play": "try_play_async",
        "pause": "try_pause_async",
        "stop": "try_pause_async",
        "play_pause": "try_toggle_play_pause_async",
        "next": "try_skip_next_async",
        "previous": "try_skip_previous_async",
    }.get(action)
    if not method_name or not hasattr(session, method_name):
        raise RuntimeError(f"Windows media session не поддерживает команду {action}")
    ok = await getattr(session, method_name)()
    if ok is False:
        raise RuntimeError(f"Windows media session отклонила команду {action}")

    verified_state = action not in {"play", "pause", "stop"}
    status_verified = False
    status_after = ""
    observed_statuses: list[str] = []
    if action in {"play", "pause", "stop"}:
        desired_states = {"playing"} if action == "play" else {"paused", "stopped", "closed"}
        opposite_states = {"paused", "stopped"} if action == "play" else {"playing"}
        stable_hits = 0
        for _ in range(36):
            await asyncio.sleep(0.10)
            try:
                playback_after = session.get_playback_info()
                status_after = normalize(str(getattr(getattr(playback_after, "playback_status", None), "name", getattr(playback_after, "playback_status", "")) or ""))
            except Exception:
                continue
            if status_after:
                observed_statuses.append(status_after)
                info["status_after"] = status_after
            if any(token in status_after for token in desired_states):
                stable_hits += 1
                if stable_hits >= 3:
                    verified_state = True
                    status_verified = True
                    break
            else:
                stable_hits = 0
        # HOTFIX12: Chromium's PlaybackStatus and TimelineProperties are not updated
        # atomically. In HOTFIX10 a stale timeline could turn a *successful* YouTube
        # Play/Pause into a false failure even though the video visibly changed
        # state. Three consecutive desired PlaybackStatus samples are now sufficient
        # proof after an accepted idempotent TryPlay/TryPause request. Timeline is a
        # useful second witness, but disagreement is treated as telemetry lag rather
        # than overriding a stable requested status.
        if verified_state and action in {"play", "pause", "stop"}:
            timeline_samples: list[float] = []
            for _ in range(6):
                await asyncio.sleep(0.20)
                try:
                    timeline_now = session.get_timeline_properties()
                    pos_now = getattr(timeline_now, "position", None)
                    if hasattr(pos_now, "total_seconds"):
                        timeline_samples.append(float(pos_now.total_seconds()))
                except Exception:
                    pass
            if len(timeline_samples) >= 2:
                movement = timeline_samples[-1] - timeline_samples[0]
                info["timeline_samples"] = [round(x, 2) for x in timeline_samples]
                info["timeline_movement"] = round(movement, 3)
                movement_agrees = (
                    (action == "play" and movement >= 0.18)
                    or (action in {"pause", "stop"} and abs(movement) <= 0.35)
                )
                info["verification"] = (
                    "status_and_timeline"
                    if movement_agrees
                    else "stable_status_timeline_lag"
                )
            elif status_verified:
                info["verification"] = "stable_status"

        if not verified_state:
            # v3.9: Chromium/YouTube occasionally keeps PlaybackStatus stale even
            # though TryPauseAsync/TryPlayAsync succeeded.  TimelineProperties is
            # a second *read-only* witness: a paused playhead remains effectively
            # stationary, while a playing one advances.  This avoids sending a
            # dangerous second toggle merely because the status enum lagged.
            timeline_samples: list[float] = []
            for _ in range(4):
                await asyncio.sleep(0.22)
                try:
                    timeline_now = session.get_timeline_properties()
                    pos_now = getattr(timeline_now, "position", None)
                    if hasattr(pos_now, "total_seconds"):
                        timeline_samples.append(float(pos_now.total_seconds()))
                except Exception:
                    pass
            if len(timeline_samples) >= 2:
                movement = timeline_samples[-1] - timeline_samples[0]
                info["timeline_samples"] = [round(x, 2) for x in timeline_samples]
                info["timeline_movement"] = round(movement, 3)
                if action in {"pause", "stop"} and abs(movement) <= 0.30:
                    verified_state = True
                    info["verification"] = "timeline_stable_paused"
                    info["status_after"] = status_after or "paused_by_timeline"
                elif action == "play" and movement >= 0.30:
                    verified_state = True
                    info["verification"] = "timeline_advancing_playing"
                    info["status_after"] = status_after or "playing_by_timeline"

        if not verified_state:
            known_opposite = bool(status_after and any(token in status_after for token in opposite_states))
            info["verification"] = "accepted_but_not_in_requested_state" if known_opposite else "accepted_but_unverified"
            info["observed_statuses"] = observed_statuses[-10:]
            raise MediaVerificationError(
                (
                    f"Команда {action} была отправлена в YouTube, но Windows не смог "
                    "надёжно прочитать итоговое состояние"
                ),
                safe_to_fallback=known_opposite,
                state_after=status_after,
                command_sent=True,
            )
    return {**context, **info, "method": "exact_tab_gsmtc", "verified": True, "score": round(score, 3), "control_success": True}

def _control_exact_tab_media_session(tab: BrowserTab, action: str, domain: str, seconds: float = 10.0) -> dict[str, Any]:
    if os.name != "nt":
        raise OSError("Windows media API доступен только в Windows")
    return _run_winrt_coro(_control_exact_tab_media_session_async(tab, action, domain, seconds), timeout=7.0)


def _read_exact_tab_media_session(tab: BrowserTab, domain: str = "video") -> dict[str, Any]:
    if os.name != "nt":
        raise OSError("Windows media API доступен только в Windows")
    return _run_winrt_coro(_read_exact_tab_media_session_async(tab, domain), timeout=4.0)


def _tab_domain(tab: BrowserTab, requested: str) -> str:
    requested = canonical_service(requested)
    title = normalize(tab.title)
    url = str(tab.url or "").casefold()
    title_youtube = "youtube" in title or "ютуб" in title
    url_youtube = _url_matches_service(url, "youtube")
    if requested in {"video", "youtube"}:
        if title_youtube or url_youtube:
            return "youtube"
        raise RuntimeError("Выбранная вкладка не похожа на YouTube; музыку не трогаю")
    if requested in {"music", "вконтакте", "яндекс музыка"}:
        if title_youtube:
            raise RuntimeError("Выбранная вкладка является YouTube; для команды про музыку её не трогаю")
        if _url_matches_service(url, "вконтакте"):
            return "вконтакте"
        if _url_matches_service(url, "яндекс музыка"):
            return "яндекс музыка"
        if any(x in title for x in ("моя музыка", "музыка", "music", "аудио")):
            return "music"
        # If the user explicitly selected the tab by number and requested a concrete
        # music service, a missing Firefox URL must not cause us to jump elsewhere.
        if requested in {"вконтакте", "яндекс музыка"} and not url:
            return requested
        raise RuntimeError("Выбранная вкладка не похожа на VK/Яндекс Музыку; YouTube не трогаю")
    raise RuntimeError(f"Неизвестный медиадомен: {requested}")



def _youtube_player_button_state(tab: BrowserTab) -> tuple[str, Any | None, str]:
    """Infer exact YouTube playback state from the player action button.

    YouTube exposes a Pause button while playing and a Play/Resume button while
    paused. We only trust page-region buttons and reject ambiguous/conflicting UIA
    trees. This is a real observable state, not merely a successful click call.
    """
    try:
        wr = tab.window.rectangle()
        height = max(1, int(wr.bottom - wr.top))
        page_top = int(wr.top) + min(170, max(95, int(height * 0.12)))
        page_bottom = int(wr.bottom) - max(8, int(height * 0.02))
    except Exception:
        wr = None
        height = 1
        page_top, page_bottom = -10**9, 10**9
    try:
        controls = list(tab.window.descendants(control_type="Button"))
    except Exception:
        return "unknown", None, ""

    play_tokens = ("воспроизвести", "продолжить", "возобновить", "play", "resume")
    pause_tokens = ("пауза", "pause")
    banned = (
        "autoplay", "автовоспроиз", "play all", "воспроизвести все", "playlist",
        "плейлист", "play on tv", "воспроизвести на телевизоре", "google play",
        "download", "скачать", "trailer", "трейлер",
    )
    ranked: list[tuple[float, str, Any, str]] = []
    for item in controls[:1600]:
        # Chromium/Firefox can leave hidden duplicate media buttons in the UIA
        # tree.  A stale hidden “Play” beside the visible “Pause” is enough to
        # invert a stateful voice command. Trust only controls that Windows says
        # are actually visible/enabled and have a real on-screen rectangle.
        try:
            if hasattr(item, "is_visible") and not bool(item.is_visible()):
                continue
        except Exception:
            pass
        try:
            if hasattr(item, "is_enabled") and not bool(item.is_enabled()):
                continue
        except Exception:
            pass
        name = _normalized_control_name(item)
        if not name or any(token in name for token in banned):
            continue
        rect = _rect_values(item)
        if rect is not None:
            l, t, r, b = rect
            if r - l <= 2 or b - t <= 2:
                continue
            if t < page_top or b > page_bottom:
                continue
            if wr is not None and (r < int(wr.left) or l > int(wr.right)):
                continue
        state = ""
        score = 0.0
        if any(token in name for token in pause_tokens):
            state = "playing"
            score = 4.5
        elif any(token in name for token in play_tokens):
            state = "paused"
            score = 4.5
        if not state:
            continue
        if "(k" in name or " k " in f" {name} ":
            score += 2.0
        if any(token in name for token in ("player", "плеер", "video", "видео")):
            score += 0.7
        if rect is not None and wr is not None:
            cy = (rect[1] + rect[3]) / 2.0
            relative_y = (cy - int(wr.top)) / max(1.0, float(height))
            if relative_y <= 0.80:
                score += 0.4
        ranked.append((score, state, item, name))
    if not ranked:
        return "unknown", None, ""
    ranked.sort(key=lambda x: x[0], reverse=True)
    best = ranked[0]
    if len(ranked) > 1 and ranked[1][1] != best[1] and abs(best[0] - ranked[1][0]) < 0.25:
        return "unknown", None, ""
    return best[1], best[2], best[3]


def _youtube_wait_for_page_state(tab: BrowserTab, desired: str, *, timeout: float = 1.2) -> tuple[bool, str]:
    """Verify the selected YouTube page without consulting Windows media sessions.

    Require two consecutive matching accessibility reads. This is intentionally
    independent from GSMTC so a stale/different browser media session cannot verify
    itself after NEO targeted the wrong playback session.
    """
    deadline = time.monotonic() + max(0.35, float(timeout))
    last = "unknown"
    hits = 0
    while time.monotonic() < deadline:
        try:
            state, _control, _name = _youtube_player_button_state(tab)
        except Exception:
            state = "unknown"
        last = state
        if state == desired:
            hits += 1
            if hits >= 2:
                return True, state
        else:
            hits = 0
        time.sleep(0.10)
    return False, last


def _youtube_wait_for_state(tab: BrowserTab, desired: str, *, timeout: float = 2.2) -> tuple[bool, str]:
    """Verify YouTube state from UIA, with read-only GSMTC as a second witness.

    Browser accessibility trees sometimes update the visible Play/Pause label later
    than playback itself.  A read-only GSMTC check prevents false negatives without
    issuing a second media command.
    """
    deadline = time.monotonic() + max(0.4, float(timeout))
    last = "unknown"
    poll = 0
    while time.monotonic() < deadline:
        state, _control, _name = _youtube_player_button_state(tab)
        last = state
        if state == desired:
            return True, state
        poll += 1
        if os.name == "nt" and poll % 5 == 0:
            try:
                media = _read_exact_tab_media_session(tab, "video")
                status = normalize(str(media.get("status", "")))
                if "playing" in status:
                    last = "playing"
                elif any(token in status for token in ("paused", "stopped", "closed")):
                    last = "paused"
                if last == desired:
                    return True, last
            except Exception:
                pass
        time.sleep(0.10)
    return False, last


def _youtube_explicit_playback_action(tab: BrowserTab, action: str) -> dict[str, Any]:
    """Set an explicit YouTube state with at most one effective UI action.

    Play and pause are *states*, not toggles.  We first read the visible YouTube
    player button; if the requested state is already true we do nothing. Otherwise
    exactly one UIA interaction is allowed.  We never stack click/invoke/K retries
    after an interaction has been accepted because a delayed accessibility update
    could make the second retry undo the first one.
    """
    desired = "playing" if action == "play" else "paused"
    state, control, control_name = _youtube_player_button_state(tab)
    if state == desired:
        # The exact verified YouTube page is the primary witness in HOTFIX100.
        # Two consecutive matching UIA samples are enough for an idempotent no-op;
        # GSMTC remains an optional second witness but is no longer required because
        # stale browser media sessions were the root cause of false failures.
        page_ok, page_state = _youtube_wait_for_page_state(tab, desired, timeout=0.55)
        media_confirmed = False
        try:
            media = _read_exact_tab_media_session(tab, "video")
            status = normalize(str(media.get("status", "")))
            media_confirmed = (desired == "playing" and "playing" in status) or (
                desired == "paused" and any(x in status for x in ("paused", "stopped", "closed"))
            )
        except Exception:
            pass
        if page_ok or media_confirmed:
            return {
                "target": "youtube", "domain": "video", "action": action,
                "method": "youtube_verified_noop", "verified": True,
                "page_verified": bool(page_ok), "media_session_verified": bool(media_confirmed),
                "already_in_requested_state": True, "state_after": page_state if page_ok else desired,
                "control": control_name, "tab": tab.title, "tab_index": tab.index,
                "url": _youtube_current_url(tab), "fingerprint": _page_fingerprint(tab.window),
            }
        raise RuntimeError("Не удалось стабильно подтвердить текущее состояние YouTube; toggle не нажимал.")

    opposite = "paused" if action == "play" else "playing"
    if control is None or state != opposite:
        raise RuntimeError(
            "Не удалось надёжно определить текущее состояние YouTube. "
            "Ничего не переключал, чтобы случайно не сделать обратное действие."
        )

    try:
        tab.window.set_focus()
    except Exception:
        pass
    _focus_page_document(tab.window)

    attempted: list[str] = []
    command_sent = False
    used_method = ""
    # A successful method call counts as the one allowed state transition. A
    # different method is tried only when the previous method itself raised before
    # completing, i.e. no interaction can be assumed to have reached the player.
    for method_name in ("click_input", "invoke", "click"):
        try:
            method = getattr(control, method_name)
        except Exception:
            continue
        try:
            method()
            attempted.append(method_name)
            command_sent = True
            used_method = method_name
            break
        except Exception:
            attempted.append(method_name + ":failed")
            continue

    # If UIA could identify a concrete state but exposes no invokable button, one
    # keyboard K is safe because no prior interaction was sent and the tab/page is
    # focused. This is the only place an explicit-state command may use K.
    if not command_sent:
        try:
            import pyautogui
            pyautogui.press("k")
            attempted.append("keyboard_k")
            command_sent = True
            used_method = "keyboard_k"
        except Exception:
            pass

    if not command_sent:
        raise RuntimeError("Не удалось отправить команду YouTube через доступный элемент плеера.")

    ok, after = _youtube_wait_for_state(tab, desired, timeout=4.0)
    if ok:
        return {
            "target": "youtube", "domain": "video", "action": action,
            "method": f"youtube_uia_verified:{used_method}", "verified": True,
            "state_before": state, "state_after": after, "control": control_name,
            "attempted": attempted, "tab": tab.title, "tab_index": tab.index,
            "url": tab.url, "fingerprint": _page_fingerprint(tab.window),
        }

    raise RuntimeError(
        "Команда YouTube была отправлена один раз, но новое состояние не удалось "
        f"подтвердить (ожидалось: {desired}, наблюдается: {after or 'unknown'}). "
        "Команду успешной не считаю. Повторно переключать плеер не стал, чтобы не отменить возможное успешное действие."
    )


def _control_media_on_tab(tab: BrowserTab, action: str, requested_domain: str, seconds: float = 10.0) -> dict[str, Any]:
    action = "pause" if action == "stop" else str(action or "")
    target = _tab_domain(tab, requested_domain)

    # HOTFIX100: explicit YouTube commands may use a verified focus/UI fallback.
    # The fallback is restricted to the exact YouTube tab and is required for
    # controls that Windows Media Session does not expose (Shorts, theater, quality).


    try:
        tab.window.set_focus()
    except Exception:
        pass
    _focus_page_document(tab.window)
    positive, negative = _action_control_needles(action, target)
    try:
        wr = tab.window.rectangle()
        page_floor = int(wr.top) + min(155, max(95, int((wr.bottom - wr.top) * 0.12)))
        page_top_limit = int(wr.top) + min(520, max(320, int((wr.bottom - wr.top) * 0.55)))
    except Exception:
        page_floor, page_top_limit = -10**9, 10**9

    if target == "youtube":
        try:
            import pyautogui
            wr = tab.window.rectangle()
            pyautogui.moveTo(int(wr.left + (wr.right-wr.left)*0.50), int(wr.top + (wr.bottom-wr.top)*0.32), duration=0.04)
            time.sleep(0.08)
        except Exception:
            pass


    scored: list[tuple[float, Any, str]] = []
    opposite: list[str] = []
    banned = (
        "google play", "play store", "app store", "магазин приложений", "скачать",
        "download", "установить", "install", "открыть приложение", "open app",
    )
    for control_type in ("Button",):
        try:
            controls = tab.window.descendants(control_type=control_type)
        except Exception:
            continue
        for item in controls[:1200]:
            name = _normalized_control_name(item)
            if not name or any(bad in name for bad in banned):
                continue
            rect = _rect_values(item)
            if rect is not None and rect[1] < page_floor:
                continue
            pos_hit = any(token in name for token in positive)
            neg_hit = any(token in name for token in negative)
            if neg_hit and action in {"play", "pause"}:
                opposite.append(name)
            score = 4.2 if pos_hit else 0.0
            if neg_hit:
                score -= 5.5
            if action in {"next", "previous"} and any(x in name for x in ("трек", "песн", "аудиозапис", "компози", "track", "next", "previous")):
                score += 1.0
            if action in {"play", "pause", "play_pause"} and any(x in name for x in ("player", "плеер", "музык", "аудио", "video", "видео")):
                score += 0.8
            if rect is not None and rect[1] <= page_top_limit:
                score += 0.35
            if score > 0:
                scored.append((score, item, name))
    scored.sort(key=lambda x: x[0], reverse=True)
    for score, item, name in scored[:16]:
        if score < 3.7:
            continue
        for method_name in ("invoke", "click_input", "click"):
            try:
                getattr(item, method_name)()
                time.sleep(0.10)
                return {
                    "target": target, "domain": canonical_service(requested_domain), "action": action,
                    "method": f"tab_uia:{method_name}", "control": name, "verified": True,
                    "tab": tab.title, "tab_index": tab.index, "url": tab.url,
                    "fingerprint": _page_fingerprint(tab.window),
                }
            except Exception:
                continue
    if action in {"play", "pause"} and opposite:
        return {
            "target": target, "domain": canonical_service(requested_domain), "action": action,
            "method": "tab_state_noop", "control": opposite[0], "verified": True,
            "already_in_requested_state": True, "tab": tab.title, "tab_index": tab.index,
            "url": tab.url, "fingerprint": _page_fingerprint(tab.window),
        }

    # UI Automation can hide YouTube's Play button (especially in Firefox).
    # For explicit stateful commands, use the exact tab-correlated Windows media
    # session before considering keyboard shortcuts. This fixes the case where
    # NEO switched to the correct video tab but never actually resumed it.
    if target == "youtube" and action in {"play", "pause", "play_pause", "stop", "next", "previous"}:
        try:
            return _control_exact_tab_media_session(tab, action, "video")
        except Exception as exc:
            logger.debug("Exact-tab GSMTC fallback unavailable: %s", exc)

    # Only non-stateful YouTube shortcuts are safe. Never use K for explicit play/pause.
    if target == "youtube":
        try:
            import pyautogui
            _focus_page_document(tab.window)
            if action == "play_pause":
                pyautogui.press("k")
                return {"target": target, "domain": "video", "action": action, "method": "youtube_k_toggle", "verified": True, "tab": tab.title, "tab_index": tab.index, "url": tab.url, "fingerprint": _page_fingerprint(tab.window)}
            if action == "next":
                pyautogui.hotkey("shift", "n")
                return {"target": target, "domain": "video", "action": action, "method": "youtube_shift_n", "verified": True, "tab": tab.title, "tab_index": tab.index, "url": tab.url, "fingerprint": _page_fingerprint(tab.window)}
            if action == "previous":
                pyautogui.hotkey("shift", "p")
                return {"target": target, "domain": "video", "action": action, "method": "youtube_shift_p", "verified": True, "tab": tab.title, "tab_index": tab.index, "url": tab.url, "fingerprint": _page_fingerprint(tab.window)}
            if action in {"seek_forward", "seek_backward"}:
                seconds = max(1, int(round(float(seconds or 10.0))))
                forward = action == "seek_forward"
                # YouTube keyboard shortcuts are player-scoped in current desktop
                # builds. Focus an actual player control when UIA exposes one.
                _state, player_control, _player_name = _youtube_player_button_state(tab)
                if player_control is not None:
                    try:
                        player_control.set_focus()
                    except Exception:
                        _focus_page_document(tab.window)
                else:
                    _focus_page_document(tab.window)
                before_read = None
                try:
                    before_read = _read_exact_tab_media_session(tab, "video")
                except Exception:
                    pass
                tens, remainder = divmod(seconds, 10)
                key10 = "l" if forward else "j"
                key5 = "right" if forward else "left"
                for _ in range(min(tens, 360)):
                    pyautogui.press(key10)
                if remainder >= 3:
                    pyautogui.press(key5)
                time.sleep(0.35)
                verified = False
                after_read = None
                try:
                    after_read = _read_exact_tab_media_session(tab, "video")
                    before_pos = float((before_read or {}).get("position_seconds", 0.0))
                    after_pos = float((after_read or {}).get("position_seconds", 0.0))
                    min_move = min(max(1.0, seconds * 0.20), 4.0)
                    verified = after_pos - before_pos >= min_move if forward else before_pos - after_pos >= min_move
                except Exception:
                    verified = False
                return {
                    "target": target, "domain": "video", "action": action,
                    "method": "youtube_player_focused_seek_keys", "seconds_requested": seconds,
                    "verified": verified, "command_sent": True,
                    "position_before": (before_read or {}).get("position_seconds"),
                    "position_after": (after_read or {}).get("position_seconds") if after_read else None,
                    "tab": tab.title, "tab_index": tab.index, "url": tab.url,
                    "fingerprint": _page_fingerprint(tab.window),
                }
        except Exception:
            pass
    context = {
        "index": tab.index, "title": tab.title, "url": tab.url, "browser": tab.process_name,
        "window_handle": int(getattr(tab.window, "handle", 0) or 0),
        "fingerprint": _page_fingerprint(tab.window),
        "target": target, "domain": canonical_service(requested_domain),
    }
    exc = RuntimeError(f"Не нашёл доступный элемент плеера {action} в выбранной вкладке")
    setattr(exc, "tab_context", context)
    raise exc


def control_tab_media_by_index(action: str, domain: str, index: int, browser: str = "", seconds: float = 10.0) -> dict[str, Any]:
    # HOTFIX8: a media command must never activate a tab merely to control it.
    # Resolve the requested ordinal from the passive UIA tab list and let the
    # service-specific media path decide whether background control is possible.
    # This is especially important for YouTube, whose transport path is GSMTC-only.
    wanted = int(index)
    tabs = [tab for tab in list_browser_tabs(browser) if int(tab.index or 0) == wanted]
    if not tabs:
        raise RuntimeError(f"Не вижу вкладку браузера №{wanted}")
    if len(tabs) > 1:
        # Multiple browser windows can each have tab #N. Prefer an unambiguous
        # service match; otherwise fail instead of stealing focus to disambiguate.
        canonical = canonical_service(domain)
        matched = [
            tab for tab in tabs
            if _url_matches_service(tab.url or _cached_tab_url(tab), canonical)
            or _explicit_title_match(tab, canonical)
        ]
        if len(matched) == 1:
            tabs = matched
        else:
            raise RuntimeError(
                f"Вкладка №{wanted} неоднозначна между несколькими окнами браузера. "
                "NEO не переключал окна и не забирал ввод."
            )
    tab = tabs[0]
    return _control_media_on_tab(tab, action, domain, seconds=seconds)

def control_selected_tab_media(action: str, domain: str, browser: str = "", seconds: float = 10.0) -> dict[str, Any]:
    if os.name != "nt":
        raise OSError("Управление вкладкой доступно только в Windows")
    tab = selected_browser_tab(browser)
    if tab is None:
        raise RuntimeError("Не вижу выбранную вкладку браузера")
    return _control_media_on_tab(tab, action, domain, seconds=seconds)


def _slider_name(wrapper: Any) -> str:
    parts: list[str] = []
    for attr in ("window_text",):
        try:
            parts.append(str(getattr(wrapper, attr)() or ""))
        except Exception:
            pass
    try:
        parts.append(str(wrapper.element_info.name or ""))
    except Exception:
        pass
    try:
        parts.append(str(wrapper.element_info.automation_id or ""))
    except Exception:
        pass
    return normalize(" ".join(parts))


def _is_volume_slider(name: str) -> bool:
    return any(token in name for token in ("volume", "громк", "sound", "audio", "звук"))


def _range_percent(slider: Any) -> float | None:
    try:
        iface = slider.iface_range_value
        minimum = float(iface.CurrentMinimum)
        maximum = float(iface.CurrentMaximum)
        current = float(iface.CurrentValue)
        if maximum <= minimum:
            return None
        return max(0.0, min(100.0, (current - minimum) * 100.0 / (maximum - minimum)))
    except Exception:
        return None


def _set_range_value(slider: Any, percent: int) -> tuple[bool, float | None, str]:
    """Change a web slider with *real input* and verify it when possible.

    Chromium may expose RangeValuePattern yet silently ignore SetValue for DOM
    controls. RC6 treated that call as success. RC7 clicks the physical control,
    drives it with keyboard events and reads the value back before claiming success.
    """

    percent = max(0, min(100, int(percent)))
    try:
        rect = slider.rectangle()
        width = max(4, int(rect.right - rect.left))
        height = max(4, int(rect.bottom - rect.top))
        x = int(rect.left + max(2, min(width - 2, width * (percent / 100.0))))
        y = int(rect.top + height / 2)
        import pyautogui

        old_pos = pyautogui.position()
        try:
            pyautogui.moveTo(x, y, duration=0.08)
            pyautogui.click()
            time.sleep(0.12)

            observed = _range_percent(slider)
            if observed is not None and abs(observed - percent) <= 7.0:
                return True, observed, "physical_click"

            # Keyboard events fire the same DOM input/change handlers a human uses.
            # Most media volume sliders use 5% steps; if UIA reports another step we
            # discover it dynamically after Home + one Right.
            pyautogui.press("home")
            time.sleep(0.05)
            base = _range_percent(slider)
            pyautogui.press("right")
            time.sleep(0.05)
            one = _range_percent(slider)
            step = None
            if base is not None and one is not None and one > base + 0.05:
                step = one - base
            if step is None or step <= 0 or step > 25:
                step = 5.0
            extra = max(0, int(round(percent / step)) - 1)
            if extra:
                pyautogui.press("right", presses=min(120, extra), interval=0.015)
            time.sleep(0.10)
            observed = _range_percent(slider)
            if observed is None:
                # The control does not expose a readable value, but it did receive
                # real mouse/keyboard input. Report this as unverified, not fabricated.
                return False, None, "keyboard_unverified"
            if abs(observed - percent) <= max(7.0, step + 1.0):
                return True, observed, "keyboard_verified"
        finally:
            try:
                pyautogui.moveTo(old_pos.x, old_pos.y, duration=0.05)
            except Exception:
                pass
    except Exception:
        pass

    # Final compatibility attempt for controls that genuinely support RangeValue.
    try:
        iface = slider.iface_range_value
        minimum = float(iface.CurrentMinimum)
        maximum = float(iface.CurrentMaximum)
        value = minimum + (maximum - minimum) * (percent / 100.0)
        iface.SetValue(value)
        time.sleep(0.08)
        observed = _range_percent(slider)
        if observed is not None and abs(observed - percent) <= 5.0:
            return True, observed, "rangevalue_verified"
    except Exception:
        pass
    return False, _range_percent(slider), "failed"

def _hover_volume_button(window: Any) -> None:
    """Reveal auto-hidden player volume controls without toggling mute."""

    try:
        import pyautogui
        for control_type in ("Button", "Text"):
            for item in window.descendants(control_type=control_type):
                name = _slider_name(item)
                if not any(token in name for token in ("громк", "volume", "mute", "звук", "sound")):
                    continue
                rect = item.rectangle()
                if rect.right <= rect.left or rect.bottom <= rect.top:
                    continue
                pyautogui.moveTo(int((rect.left + rect.right) / 2), int((rect.top + rect.bottom) / 2), duration=0.08)
                time.sleep(0.35)
                return
    except Exception:
        return


def _set_html_media_volume(percent: int, target: str, browser: str = "") -> dict[str, Any] | None:
    """Disabled unsafe bookmarklet path.

    RC19 pasted javascript: into the address bar. Some browsers search that string
    instead of executing it. Volume is now changed only through UI Automation; if
    the site's slider is unavailable the caller can use the per-application mixer.
    """
    return None


def set_web_media_volume(percent: int, target: str = "", browser: str = "") -> dict[str, Any]:
    """Set the web player's own volume and verify that the DOM control moved."""

    if os.name != "nt":
        raise OSError("Управление ползунком браузера доступно только в Windows")
    percent = max(0, min(100, int(percent)))
    try:
        from pywinauto import Desktop
    except Exception as exc:
        raise RuntimeError("Не установлен pywinauto для управления элементами браузера") from exc

    target = canonical_service(target)
    direct = _set_html_media_volume(percent, target, browser)
    if direct is not None:
        return direct
    selected_tab = activate_service_tab(target, browser) if target and target not in {"browser", "браузер"} else None

    windows: list[Any] = []
    if selected_tab is not None:
        windows.append(selected_tab.window)
    try:
        foreground = int(__import__("ctypes").windll.user32.GetForegroundWindow())
        if foreground:
            windows.append(Desktop(backend="uia").window(handle=foreground))
    except Exception:
        pass
    try:
        for win in Desktop(backend="uia").windows(visible_only=True):
            pid = int(win.element_info.process_id)
            if _process_name(pid) in BROWSER_PROCESSES and all(
                getattr(win, "handle", None) != getattr(x, "handle", None) for x in windows
            ):
                windows.append(win)
    except Exception:
        pass

    candidates: list[tuple[float, str, Any, Any]] = []
    for window in windows:
        try:
            process = _process_name(int(window.element_info.process_id))
            if process not in BROWSER_PROCESSES:
                continue
            _hover_volume_button(window)
            for slider in window.descendants(control_type="Slider"):
                name = _slider_name(slider)
                if not _is_volume_slider(name):
                    continue
                score = 1.0
                if any(token in name for token in ("volume", "громк")):
                    score += 1.0
                if selected_tab is not None and getattr(window, "handle", None) == getattr(selected_tab.window, "handle", None):
                    score += 1.5
                candidates.append((score, name, slider, window))
        except Exception:
            continue

    if not candidates:
        raise RuntimeError(
            "Не нашёл ползунок громкости выбранного веб-плеера. Открой вкладку с плеером; "
            "NEO не будет подменять это изменением громкости Windows."
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    attempts: list[dict[str, Any]] = []
    for _priority, name, slider, _window in candidates:
        ok, observed, method = _set_range_value(slider, percent)
        attempts.append({"name": name, "observed": observed, "method": method})
        if ok:
            return {
                "percent": percent,
                "observed_percent": observed,
                "verified": observed is not None,
                "method": method,
                "target": target or "active browser tab",
                "slider": name or "volume",
                "tab": selected_tab.title if selected_tab else "",
            }
    raise RuntimeError(
        "Ползунок найден, но браузер не подтвердил изменение громкости. "
        f"Попытки: {attempts[:3]}"
    )


def _open_url_in_named_browser(url: str, browser: str = "") -> None:
    name = normalize(browser)
    if os.name != "nt" or not name:
        webbrowser.open(url)
        return
    # Reuse the configured browser window first; this prevents a command from
    # unexpectedly launching a different default browser.
    window = _foreground_browser_window(browser)
    if window is not None:
        try:
            import pyautogui
            import pyperclip
            window.set_focus()
            previous = pyperclip.paste()
            pyperclip.copy(url)
            pyautogui.hotkey("ctrl", "l")
            pyautogui.hotkey("ctrl", "v")
            pyautogui.press("enter")
            time.sleep(0.08)
            pyperclip.copy(previous)
            return
        except Exception:
            pass
    candidates: list[str] = []
    if any(x in name for x in ("firefox", "mozilla", "мозил", "фаерфокс")):
        candidates = [os.path.expandvars(r"%ProgramFiles%\Mozilla Firefox\firefox.exe"), os.path.expandvars(r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe")]
    elif "chrome" in name or "хром" in name:
        candidates = [os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"), os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")]
    elif "edge" in name or "эдж" in name:
        candidates = [os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"), os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")]
    elif "opera" in name or "опера" in name:
        candidates = [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\opera.exe"), os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera GX\opera.exe")]
    elif "yandex" in name or "яндекс" in name:
        candidates = [os.path.expandvars(r"%LOCALAPPDATA%\Yandex\YandexBrowser\Application\browser.exe")]
    for exe in candidates:
        if Path(exe).exists():
            subprocess.Popen([exe, url])
            return
    # Last resort only when the configured executable cannot be located.
    webbrowser.open(url)


def play_media_query(query: str, target: str = "", browser: str = "") -> dict[str, Any]:
    """Open a service search and best-effort start the most relevant result."""

    query = str(query or "").strip()
    if not query:
        raise ValueError("Не указано название трека")
    target = canonical_service(target) or "music"
    if target in {"music", "video"}:
        # Prefer a service that is already open/playing.
        for candidate in (("яндекс музыка", "вконтакте") if target == "music" else ("youtube",)):
            if find_service_tab(candidate, browser) is not None:
                target = candidate
                break
        else:
            target = "яндекс музыка" if target == "music" else "youtube"

    if target == "youtube":
        url = "https://www.youtube.com/results?search_query=" + quote_plus(query)
    elif target == "яндекс музыка":
        url = "https://music.yandex.ru/search?text=" + quote_plus(query)
    elif target == "вконтакте":
        url = "https://vk.com/audio?q=" + quote_plus(query)
    else:
        url = "https://www.google.com/search?q=" + quote_plus(query + " " + target)
    _open_url_in_named_browser(url, browser)

    if os.name != "nt":
        return {"query": query, "target": target, "url": url, "started": False}
    # Give the browser a moment, then use its accessibility tree instead of fixed
    # screen coordinates. If no trustworthy result is exposed we leave the search
    # page open rather than clicking something unrelated.
    try:
        from pywinauto import Desktop
        deadline = time.monotonic() + 5.0
        query_tokens = [x for x in normalize(query).split() if len(x) >= 2]
        best = None
        while time.monotonic() < deadline:
            time.sleep(0.35)
            foreground = int(__import__("ctypes").windll.user32.GetForegroundWindow())
            if not foreground:
                continue
            window = Desktop(backend="uia").window(handle=foreground)
            try:
                process = _process_name(int(window.element_info.process_id))
            except Exception:
                process = ""
            if process not in BROWSER_PROCESSES:
                continue
            for control_type in ("Hyperlink", "ListItem", "Button", "Text"):
                try:
                    items = window.descendants(control_type=control_type)
                except Exception:
                    continue
                for item in items[:600]:
                    try:
                        name = str(item.window_text() or item.element_info.name or "").strip()
                    except Exception:
                        continue
                    hay = normalize(name)
                    if not hay or len(hay) < 3:
                        continue
                    if any(bad in hay for bad in ("настройки", "settings", "главная", "home", "подпис", "реклама")):
                        continue
                    token_hits = sum(1 for token in query_tokens if token in hay)
                    lexical = difflib.SequenceMatcher(None, normalize(query), hay).ratio()
                    score = lexical + min(0.8, token_hits * 0.24)
                    if best is None or score > best[0]:
                        best = (score, item, name)
            if best and best[0] >= 0.52:
                break
        if best and best[0] >= 0.52:
            item = best[1]
            for method_name in ("invoke", "click_input", "click"):
                try:
                    getattr(item, method_name)()
                    return {"query": query, "target": target, "url": url, "started": True, "result": best[2], "score": round(best[0], 3)}
                except Exception:
                    continue
    except Exception:
        pass
    return {"query": query, "target": target, "url": url, "started": False}

def media_session_target_score(info: dict[str, Any], target: str, tabs: list[BrowserTab] | None = None) -> float:
    """Conservative service-level GSMTC score.

    This is intentionally weaker than `media_session_tab_score`. A generic Windows
    current session is never assumed to be the requested browser tab.
    """
    target = canonical_service(target)
    if not target:
        return 0.0
    hay = normalize(" ".join(str(info.get(k, "")) for k in ("source", "title", "artist", "album")))
    if target == "music" and ("youtube" in hay or "ютуб" in hay):
        return 0.0
    if target in {"video", "youtube"} and any(x in hay for x in ("yandex music", "яндекс музык", "вконтакт")):
        return 0.0

    direct = 0.0
    if target == "яндекс музыка" and (("yandex" in hay or "яндекс" in hay) and ("music" in hay or "музык" in hay)):
        direct = 1.0
    elif target == "вконтакте" and ("вконтакт" in hay or re.search(r"\bvk\b", hay)):
        direct = 1.0
    elif target in {"video", "youtube"} and ("youtube" in hay or "ютуб" in hay):
        direct = 0.96

    media_title = normalize(str(info.get("title", "") or ""))
    artist = normalize(str(info.get("artist", "") or ""))
    tabs = tabs if tabs is not None else list_browser_tabs()
    correlated = 0.0
    for tab in tabs:
        # IMPORTANT: never add tab.window_title here. It mirrors only the selected
        # tab and previously made every Firefox tab look like YouTube/Google Search.
        service_score = _title_service_score(tab.title, target)
        known_url = tab.url or _cached_tab_url(tab)
        if known_url and _url_matches_service(known_url, target):
            service_score = max(service_score, 1.0)
        if service_score < 0.70:
            continue
        tab_text = normalize(tab.title)
        title_score = 0.0
        if media_title and tab_text:
            if media_title in tab_text or tab_text in media_title:
                title_score = 1.0
            else:
                title_score = difflib.SequenceMatcher(None, media_title, tab_text).ratio()
        if artist and artist in tab_text:
            title_score = max(title_score, 0.86)
        # Generic «Моя музыка» without track evidence is not enough to select a
        # Windows session when several media sources are active.
        if title_score >= 0.66:
            correlated = max(correlated, service_score * (0.56 + 0.44 * title_score))
    return max(direct, correlated)

