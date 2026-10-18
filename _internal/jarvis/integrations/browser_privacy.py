# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import psutil


@dataclass(frozen=True)
class BrowserProfile:
    key: str
    label: str
    root: Path
    processes: tuple[str, ...]
    firefox: bool = False


def _env(name: str) -> Path:
    value = os.environ.get(name, "")
    return Path(value) if value else Path.home()


def _candidates() -> tuple[BrowserProfile, ...]:
    local = _env("LOCALAPPDATA")
    roaming = _env("APPDATA")
    return (
        BrowserProfile("chrome", "Google Chrome", local / "Google" / "Chrome" / "User Data", ("chrome.exe",)),
        BrowserProfile("edge", "Microsoft Edge", local / "Microsoft" / "Edge" / "User Data", ("msedge.exe",)),
        BrowserProfile("brave", "Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data", ("brave.exe",)),
        BrowserProfile("vivaldi", "Vivaldi", local / "Vivaldi" / "User Data", ("vivaldi.exe",)),
        BrowserProfile("yandex", "Yandex Browser", local / "Yandex" / "YandexBrowser" / "User Data", ("browser.exe", "yandex.exe")),
        BrowserProfile("opera", "Opera", roaming / "Opera Software" / "Opera Stable", ("opera.exe",)),
        BrowserProfile("opera_gx", "Opera GX", roaming / "Opera Software" / "Opera GX Stable", ("opera.exe",)),
        BrowserProfile("firefox", "Mozilla Firefox", roaming / "Mozilla" / "Firefox", ("firefox.exe",), True),
    )


def available_browsers() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in _candidates():
        if item.root.exists():
            rows.append({"key": item.key, "label": item.label, "path": str(item.root)})
    return rows


def _resolve(key: str) -> BrowserProfile:
    wanted = str(key or "").strip().casefold().replace(" ", "_")
    aliases = {
        "google_chrome": "chrome", "google": "chrome", "хром": "chrome",
        "microsoft_edge": "edge", "эдж": "edge", "mozilla": "firefox", "мозилла": "firefox",
        "яндекс": "yandex", "yandex_browser": "yandex", "opera_gx": "opera_gx", "опера_gx": "opera_gx",
    }
    wanted = aliases.get(wanted, wanted)
    for item in _candidates():
        if item.key == wanted:
            if not item.root.exists():
                raise FileNotFoundError(f"Профиль {item.label} на этом компьютере не найден.")
            return item
    raise ValueError("Не удалось определить браузер. Сначала запросите список доступных браузеров.")


def _running_processes(profile: BrowserProfile) -> list[str]:
    names = {x.casefold() for x in profile.processes}
    found: set[str] = set()
    for proc in psutil.process_iter(["name"]):
        try:
            name = str(proc.info.get("name", "") or "").casefold()
            if name in names:
                found.add(name)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sorted(found)


def _require_closed(profile: BrowserProfile) -> None:
    running = _running_processes(profile)
    if running:
        raise RuntimeError(
            f"Сначала полностью закройте {profile.label}. Очистка профиля не выполняется, пока браузер запущен."
        )


def _chromium_profile_dirs(root: Path) -> Iterable[Path]:
    # Opera uses the selected root as one profile; Chromium-family browsers keep
    # Default/Profile N directories below User Data.
    if root.name in {"Opera Stable", "Opera GX Stable"}:
        yield root
        return
    for child in root.iterdir() if root.exists() else ():
        if child.is_dir() and (child.name == "Default" or child.name.startswith("Profile ") or child.name == "Guest Profile"):
            yield child


def _clear_chromium_history(root: Path) -> dict[str, int]:
    names = {
        "History", "History-journal", "Visited Links", "Top Sites", "Top Sites-journal",
        "Network Action Predictor", "Network Action Predictor-journal",
    }
    deleted = 0
    profiles = 0
    for profile_dir in _chromium_profile_dirs(root):
        profiles += 1
        for name in names:
            path = profile_dir / name
            if not path.exists():
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                deleted += 1
            except FileNotFoundError:
                pass
    return {"profiles": profiles, "files_deleted": deleted}


def _clear_firefox_history(root: Path) -> dict[str, int]:
    profiles_root = root / "Profiles"
    changed = 0
    profiles = 0
    if not profiles_root.exists():
        return {"profiles": 0, "databases_cleaned": 0}
    for profile in profiles_root.iterdir():
        db = profile / "places.sqlite"
        if not profile.is_dir() or not db.exists():
            continue
        profiles += 1
        conn = sqlite3.connect(str(db), timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("DELETE FROM moz_historyvisits")
            try:
                conn.execute("DELETE FROM moz_inputhistory")
            except sqlite3.DatabaseError:
                pass
            # Keep bookmarks and their URLs. Clearing visits is enough to remove
            # browser history without destroying bookmark data.
            try:
                conn.execute("UPDATE moz_places SET visit_count=0, last_visit_date=NULL")
            except sqlite3.DatabaseError:
                pass
            conn.commit()
            changed += 1
        finally:
            conn.close()
        for suffix in ("-wal", "-shm"):
            try:
                (profile / ("places.sqlite" + suffix)).unlink(missing_ok=True)
            except Exception:
                pass
    return {"profiles": profiles, "databases_cleaned": changed}


def clear_history(browser: str) -> dict[str, object]:
    profile = _resolve(browser)
    _require_closed(profile)
    detail = _clear_firefox_history(profile.root) if profile.firefox else _clear_chromium_history(profile.root)
    return {
        "browser": profile.key,
        "label": profile.label,
        "root": str(profile.root),
        "mode": "history",
        **detail,
    }


def full_cleanup(browser: str) -> dict[str, object]:
    profile = _resolve(browser)
    _require_closed(profile)
    root = profile.root.resolve()
    # Safety fence: only known browser profile roots produced above are accepted.
    known = {item.root.resolve() for item in _candidates()}
    if root not in known:
        raise RuntimeError("Защитная проверка пути профиля браузера не пройдена.")
    if not root.exists():
        raise FileNotFoundError(f"Данные {profile.label} уже отсутствуют.")
    shutil.rmtree(root)
    return {
        "browser": profile.key,
        "label": profile.label,
        "root": str(root),
        "mode": "full_cleanup",
        "deleted": True,
        "note": "Удалены локальные данные профиля. Облачная синхронизация может восстановить их после повторного входа в аккаунт.",
    }
