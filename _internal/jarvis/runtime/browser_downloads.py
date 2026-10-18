"""Bounded read-only Chromium download metadata; never queries browsing URLs.

The persisted history enum differs from the runtime DownloadItem enum:
0=in progress, 1=complete, 2=cancelled, 3=historical bug, 4=interrupted.
See docs/DOWNLOAD_SOURCES.md for primary schema sources and limitations.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit

WINDOWS_EPOCH_OFFSET = 11644473600
MAX_PROFILES = 12
MAX_ROWS = 64
STATES = {0: "downloading", 1: "completed", 2: "cancelled", 3: "unknown", 4: "failed"}
BROWSERS = (
    ("Chrome", "chrome.exe", "Google/Chrome/User Data"),
    ("Edge", "msedge.exe", "Microsoft/Edge/User Data"),
    ("Brave", "brave.exe", "BraveSoftware/Brave-Browser/User Data"),
)


@dataclass(frozen=True)
class BrowserProfile:
    browser: str
    app: str
    history: Path

    @property
    def source(self):
        return self.app

    @property
    def identity(self):
        return hashlib.sha256(os.path.normcase(str(self.history.absolute())).encode()).hexdigest()[:16]


def sanitize_source_url(value: str) -> str:
    """Keep the download origin/path, removing ALL query, fragment and userinfo."""
    try:
        parsed = urlsplit(str(value or "")[:8192])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname.encode("idna").decode("ascii")
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port:
            host += ":" + str(parsed.port)
        segments = []
        for segment in parsed.path.split("/"):
            # Signed URLs sometimes place opaque credentials in path segments.
            if len(segment) > 80 or re.search(r"(?:token|password|secret|signature|auth)[=:]",segment,re.I):
                segment = "[redacted]"
            segments.append(segment.split(";",1)[0])
        path = "/".join(segments)[:512]
        return urlunsplit((parsed.scheme, host, path, "", ""))[:768]
    except (ValueError, UnicodeError):
        return ""


def _local_path(path: Path) -> bool:
    return not str(path).startswith(("\\\\", "//")) and not path.is_symlink() and not getattr(path,"is_junction",lambda:False)()


def discover_profiles() -> list[BrowserProfile]:
    if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
        return []
    base = Path(os.environ["LOCALAPPDATA"])
    if not _local_path(base):
        return []
    result = []
    for browser, app, suffix in BROWSERS:
        root = base / suffix
        if not _local_path(root):
            continue
        try:
            with os.scandir(root) as entries:
                candidates = []
                for index, entry in enumerate(entries):
                    if index >= 96:
                        break
                    if entry.name == "Default" or re.fullmatch(r"Profile \d{1,3}",entry.name):
                        candidates.append(Path(entry.path))
            for folder in sorted(candidates,key=lambda p:(p.name != "Default",p.name))[:4]:
                history = folder / "History"
                if _local_path(folder) and _local_path(history) and history.is_file():
                    result.append(BrowserProfile(browser,app,history))
        except OSError:
            continue
    return result[:MAX_PROFILES]


def _signature(path: Path):
    rows = []
    for candidate in (path, Path(str(path)+"-wal")):
        try:
            stat = candidate.stat()
            rows.append((stat.st_size,stat.st_mtime_ns))
        except OSError:
            rows.append(None)
    return tuple(rows)


def read_downloads(profile: BrowserProfile, *, deadline: float | None = None) -> list[dict] | None:
    """None = unavailable/locked/unsupported, [] = valid empty downloads table.

    No retry, backup, copy, VACUUM, journal mode change, writes, or browsing-history
    query. Live WAL sidecars are used normally; immutable=1 would hide updates.
    """
    deadline = min(deadline or time.monotonic()+.10,time.monotonic()+.10)
    db = None
    try:
        if not _local_path(profile.history) or not profile.history.is_file() or time.monotonic() >= deadline:
            return None
        uri = profile.history.absolute().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri,uri=True,timeout=.025,isolation_level=None)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=25")
        db.execute("PRAGMA trusted_schema=OFF")
        def authorize(operation, first, second, _database, _trigger):
            if operation == sqlite3.SQLITE_READ:
                return sqlite3.SQLITE_OK if first in {"downloads","downloads_url_chains","sqlite_master","sqlite_schema"} else sqlite3.SQLITE_DENY
            if operation == sqlite3.SQLITE_PRAGMA:
                return sqlite3.SQLITE_OK if first == "table_info" and second in {"downloads","downloads_url_chains"} else sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK if operation in {sqlite3.SQLITE_SELECT,sqlite3.SQLITE_FUNCTION} else sqlite3.SQLITE_DENY
        db.set_authorizer(authorize)
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 500)
        db.row_factory = sqlite3.Row
        columns = {row["name"] for row in db.execute("PRAGMA table_info(downloads)")}
        if not {"id", "state", "start_time", "received_bytes"}.issubset(columns):
            return None
        path_column = next((name for name in ("target_path","full_path","current_path") if name in columns),"")
        if not path_column:
            return None
        def field(name, fallback="0", text=False):
            if name not in columns:
                return fallback + " AS " + name
            return (f"substr(CAST({name} AS TEXT),1,8192)" if text else name) + " AS " + name
        selection = [field("id"),field("guid","''",True),field("state"),field("start_time"),field("received_bytes"),
                     field("total_bytes"),field("current_path","''",True),field("interrupt_reason"),field("transient"),
                     f"substr(CAST({path_column} AS TEXT),1,8192) AS target_path",field("url","''",True)]
        # ORDER BY integer primary key bounds the read even with years of history.
        rows = db.execute("SELECT "+",".join(selection)+" FROM downloads ORDER BY id DESC LIMIT ?",(MAX_ROWS,)).fetchall()
        chain_columns = {row["name"] for row in db.execute("PRAGMA table_info(downloads_url_chains)")}
        has_chain = {"id","chain_index","url"}.issubset(chain_columns)
        result = []
        for row in rows:
            if time.monotonic() >= deadline:
                return None  # Do not establish an incomplete startup baseline.
            state, ident = int(row["state"]), int(row["id"])
            if state not in STATES or ident <= 0 or int(row["transient"] or 0):
                continue
            raw_time = float(row["start_time"])
            started = raw_time / 1_000_000 - WINDOWS_EPOCH_OFFSET if raw_time > 1e13 else raw_time
            if not 0 < started <= time.time()+86400:
                continue
            path = str(row["target_path"] or row["current_path"] or "")
            if not path or "\x00" in path or not Path(path).is_absolute():
                continue
            source_url = str(row["url"] or "")
            if has_chain:
                item = db.execute("SELECT substr(url,1,8192) FROM downloads_url_chains WHERE id=? ORDER BY chain_index LIMIT 1",(ident,)).fetchone()
                if item:
                    source_url = item[0]
            provider_id = profile.identity + ":" + hashlib.sha256((str(row["guid"] or "")+":"+str(ident)+":"+str(raw_time)).encode()).hexdigest()[:24]
            result.append({"provider_id":provider_id,"browser_id":ident,"path":path,
                           "actual_path":str(row["current_path"] or path),"state":STATES[state],
                           "started":started,"size":max(0,int(row["received_bytes"] or 0)),
                           "total_size":max(0,int(row["total_bytes"] or 0)),
                           "source_url":sanitize_source_url(source_url),"app":profile.app,"source":profile.source})
        return result
    except (OSError,sqlite3.Error,ValueError,TypeError,OverflowError):
        return None
    finally:
        if db is not None:
            db.close()


class BrowserDownloadObserver:
    def __init__(self, profiles: list[BrowserProfile] | None = None):
        self._provided = profiles
        self._profiles = list(profiles or [])[:MAX_PROFILES]
        self._states: dict[str, dict] = {}
        self._next_discovery = self._next_poll = 0.
        self._enabled = False

    def poll(self, config, tracker) -> dict:
        enabled = bool(config.get("security.smart_downloads_enabled",False))
        if not enabled:
            self._enabled = False
            self._states.clear()
            self._next_poll = 0.
            return {"enabled":False,"profiles":0,"updated":0}
        now = time.monotonic()
        if now < self._next_poll:
            return {"enabled":True,"profiles":0,"updated":0}
        self._next_poll = now + 2
        self._enabled = True
        if self._provided is None and now >= self._next_discovery:
            self._profiles = discover_profiles()
            self._next_discovery = now+60
        from jarvis.runtime.file_index import known_folders
        roots = [Path(x) for x in (config.get("downloads.directories",[]) or known_folders("Downloads"))][:16]
        roots = [p.resolve(strict=False) for p in roots if p.is_absolute() and _local_path(p)]
        if not roots:
            return {"enabled":True,"profiles":0,"updated":0}
        deadline = now + .25
        checked = changed = unavailable = 0
        for profile in self._profiles:
            if time.monotonic() >= deadline:
                break
            key = profile.identity
            state = self._states.setdefault(key,{"signature":None,"seen":OrderedDict(),"baseline":False,
                                                 "high_water":0,"activated":time.time()})
            signature = _signature(profile.history)
            if signature == state["signature"]:
                continue
            checked += 1
            rows = read_downloads(profile,deadline=deadline)
            if rows is None:
                unavailable += 1
                continue
            baseline = not state["baseline"]
            for row in reversed(rows):
                path = Path(row["path"])
                if not _local_path(path) or not any(path.resolve(strict=False).is_relative_to(root) for root in roots):
                    continue
                actual = Path(row["actual_path"])
                if not actual.is_absolute() or not _local_path(actual) or not any(actual.resolve(strict=False).is_relative_to(root) for root in roots):
                    row["actual_path"] = str(path)
                if not tracker.store.policy.allows_event('download.provider',row):
                    continue
                pid = row["provider_id"]
                fingerprint = (row["state"],row["size"],row["total_size"],row["path"],row["source_url"])
                previous = state["seen"].get(pid)
                known = tracker.store.provider_download(profile.source,pid) if baseline else None
                watcher = tracker.store.latest_download(str(path.resolve(strict=False))) if baseline and known is None else None
                same_watcher = bool(watcher and watcher["heuristic"] and abs(row["started"]-float(watcher["started"] or watcher["updated"]))<=180
                                    and (watcher["state"] in {"detected","downloading","paused"} or row["started"] <= watcher["updated"]+.01))
                if previous == fingerprint:
                    continue
                if not baseline and previous is None and row["browser_id"] <= state["high_water"] and row["started"] < state["activated"]:
                    continue  # Deleting recent history must not reveal old downloads as new.
                # Startup old terminal rows are observations, never new downloads.
                if not baseline or known or same_watcher or row["state"] == "downloading":
                    args = {name:row[name] for name in ("source","size","started","source_url","app","total_size","actual_path")}
                    if not baseline and previous is None and row["state"] != "downloading":
                        tracker.provider_state(pid,row["path"],"downloading",**args,allow_correction=True)
                    tracker.provider_state(pid,row["path"],row["state"],**args,notify=not baseline or bool(known),allow_correction=True)
                    changed += 1
                state["seen"][pid] = fingerprint
                state["seen"].move_to_end(pid)
                while len(state["seen"]) > 256:
                    state["seen"].popitem(last=False)
            state["baseline"] = True
            state["high_water"] = max([state["high_water"]] + [row["browser_id"] for row in rows])
            state["signature"] = signature
        return {"enabled":True,"profiles":checked,"updated":changed,"unavailable":unavailable}


def poll(config, tracker) -> dict:
    """Shared service hook, with state scoped to that tracker rather than global."""
    observer = getattr(tracker,"_browser_download_observer",None)
    if observer is None:
        observer = BrowserDownloadObserver()
        tracker._browser_download_observer = observer
    return observer.poll(config,tracker)
