"""Low-frequency coordination of opt-in downloads, network and local history."""
from __future__ import annotations

import logging
import os
import queue
import re
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from jarvis.runtime.event_store import EventStore

logger = logging.getLogger(__name__)
TEMP_SUFFIXES = (".crdownload", ".part", ".partial", ".download", ".tmp")


def canonical_download(path: str) -> str:
    value = str(Path(path).resolve(strict=False))
    for suffix in TEMP_SUFFIXES:
        if value.casefold().endswith(suffix):
            return value[:-len(suffix)]
    return value


class DownloadTracker:
    """Persistent lifecycle; only paths signalled by the OS are reconciled."""
    def __init__(self, store: EventStore, stable_seconds: float = 6):
        self.store = store
        self.stable_seconds = stable_seconds
        self.active: dict[str, dict] = {}
        for row in store.active_downloads():
            self.active[row["path"]] = {"id":row["id"],"actual":row.get("actual_path") or row["path"],"size":row["size"],"stable":time.monotonic(),"observed":False,"identity":row.get("file_identity", ""),"provider":not bool(row["heuristic"])}

    @staticmethod
    def _identity(stat) -> str:
        # File ID remains stable while content grows; mtime/ctime do not.
        birth = getattr(stat, "st_birthtime_ns", "")
        return f"{stat.st_dev}:{stat.st_ino}:{birth}"

    def on_file(self, path: str, *, destination: str = "", event_type: str = "created", now: float | None = None):
        if not self.store.policy.allows_event('download.observed',{'path':path,'destination':destination}):
            return
        now = time.monotonic() if now is None else now
        key = canonical_download(path)
        actual = Path(destination or path).resolve(strict=False)
        final_key = canonical_download(str(actual))
        if event_type == "deleted":
            item = self.active.get(key)
            if item is not None:
                item.setdefault("missing_since", now)
            return
        if not actual.is_file():
            return
        try:
            stat = actual.stat()
            size = stat.st_size
            identity = self._identity(stat)
        except OSError:
            return
        item = self.active.pop(key,None) if destination else self.active.get(key)
        if item is None and destination:
            item = self.active.get(final_key)
        if item is not None and item.get("provider"):
            self.active[final_key] = item
            return
        if item is None:
            old = self.store.latest_download(final_key) or (self.store.latest_download(key) if destination else None)
            temporary = str(actual).casefold().endswith(TEMP_SUFFIXES)
            if old and old["state"] in {"completed", "failed", "cancelled", "unknown"}:
                if event_type == "modified" and not temporary:
                    return  # Editing an old download is not another download.
                if old.get("file_identity") == identity and not temporary:
                    return  # Duplicate create/rename delivered after completion.
            elif old is None and event_type == "modified" and not temporary:
                return  # Do not announce edits of files predating this watcher.
            did = self.store.transition_download(final_key,"downloading",size=size,
                file_identity=identity, actual_path=str(actual))
            if not did:
                return
            item = {"id":did,"size":size,"stable":now,"observed":True,"identity":identity}
        if item.get("size") == size and item.get("actual") == str(actual):
            self.active[final_key] = item
            return
        if item.get("size") != size or item.get("actual") != str(actual):
            item["stable"] = now
        item.update(actual=str(actual),size=size,observed=True)
        self.active[final_key] = item
        self.store.transition_download(final_key,"downloading",size=size,download_id=item["id"],
            file_identity=identity, actual_path=str(actual))

    def provider_state(self, download_id: str, path: str, state: str, *, source: str, size: int = 0,
                       started: float = 0, source_url: str = "", app: str = "", total_size: int = 0,
                       actual_path: str = "", notify: bool = True, allow_correction: bool = False):
        """Authoritative browser/provider lifecycle, using its stable download ID."""
        if not download_id or not source or source == "unknown":
            raise ValueError("Provider lifecycle requires source and stable download ID")
        if not self.store.policy.allows_event('download.provider',{'path':path,'actual_path':actual_path,'source_kind':source}):
            return ''
        key = canonical_download(path)
        identity = ""
        try:
            identity = self._identity(Path(actual_path or path).stat())
        except OSError:
            pass
        did = self.store.transition_download(key,state,size=size,source=source,
            download_id=source+":"+download_id,provider_id=download_id,heuristic=False,
            actual_path=actual_path or path,file_identity=identity,started=started,source_url=source_url,
            app=app,total_size=total_size,notify=notify,allow_correction=allow_correction)
        persisted = self.store.provider_download(source,download_id)
        for old_key, item in list(self.active.items()):
            if old_key != key and item["id"] == did:
                self.active.pop(old_key,None)
        if state in {"completed","failed","cancelled","unknown"} or (persisted and persisted["id"] == did and persisted["state"] in {"completed","failed","cancelled","unknown"}):
            self.active.pop(key,None)
        else:
            self.active[key] = {"id":did,"actual":actual_path or path,"size":size,"stable":time.monotonic(),"observed":True,"provider":True}
        return did

    def reconcile(self, now: float | None = None):
        now = time.monotonic() if now is None else now
        for key,item in list(self.active.items()):
            if not self.store.policy.allows_event('download.observed',{'path':key,'actual_path':item.get('actual','')}):
                self.active.pop(key,None)
                continue
            if item.get("provider"):
                continue  # A stable/absent file cannot overrule provider status.
            actual = Path(item["actual"])
            if not actual.exists():
                # A process restart may leave only the temporary name. Do not
                # label an absent file 'failed' without provider evidence.
                alternatives = [Path(key+s) for s in TEMP_SUFFIXES]+[Path(key)]
                actual = next((p for p in alternatives if p.is_file()),actual)
            try:
                stat = actual.stat()
            except OSError:
                missing_since = item.setdefault("missing_since",now)
                if now-missing_since >= max(6.0,self.stable_seconds):
                    self.store.transition_download(key,"unknown",size=item["size"],download_id=item["id"])
                    self.active.pop(key,None)
                continue
            item.pop("missing_since",None)
            if stat.st_size != item["size"]:
                item.update(size=stat.st_size,stable=now,actual=str(actual),observed=True)
                self.store.transition_download(key,"downloading",size=stat.st_size,download_id=item["id"],
                    file_identity=self._identity(stat), actual_path=str(actual))
            temporary = str(actual).casefold().endswith(TEMP_SUFFIXES)
            if not temporary and now-item["stable"] >= self.stable_seconds:
                self.store.transition_download(str(actual),"completed",size=stat.st_size,download_id=item["id"],
                    file_identity=self._identity(stat), actual_path=str(actual))
                self.active.pop(key,None)


def is_vpn_interface(name: str) -> bool:
    """Recognise VPN devices, never the generic Windows IPv6 tunnel adapters.

    'tun' as a substring of 'Tunneling Pseudo-Interface' is not evidence of VPN.
    This is an interface hint only, not a claim about traffic/privacy protection.
    """
    value = str(name).strip().casefold()
    if any(token in value for token in ("pseudo-interface", "pseudo interface", "teredo", "isatap", "6to4", "loopback")):
        return False
    return bool(re.search(r"(?:\bvpn\b|wireguard|tailscale|openvpn|wintun|tap-windows|\btun\d*\b|\bwg\d+\b)", value))


class NetworkState:
    def __init__(self):
        self.signature = None
        self.vpns: set[str] = set()
        self.high_count = self.low_count = 0
        self.high = False
        self.connected: bool | None = None
        self._candidate = None
        self._candidate_count = 0
        self._online_candidate = None
        self._online_count = 0
        self._vpn_announced_at: dict[tuple[str, str], float] = {}
        # NEO 188 NEO-NET-001: an outage is separate from a slow link.
        self.offline = False
        self.offline_count = self.online_count = 0
        self.transitions = 0
        self.last_probe = ""

    def observe(self, signature: tuple, vpns: set[str], latency: float | None, threshold: float, context: dict | None = None) -> list[dict]:
        """One probe sample. ``latency`` < 0 means EVERY light probe failed.

        NEO 188 NEO-NET-001: three outcomes are kept apart —
        * ``network.latency_high`` / ``network.latency_normal``: the link works but is
          slow (for example saturated by a download), then normal again;
        * ``network.offline`` / ``network.restored``: no probe reaches the internet
          (or no interface is up), then the connection is back;
        * a single unreachable endpoint (``context['endpoint_failed']``) is only a
          probe failure: never a global outage and never a «restored» message.
        Debounce/hysteresis: three bad samples to enter a state, two good to leave.
        """
        context = context or {}
        events = []
        # Commit only two consecutive matching samples. An enumeration race or
        # a temporary IPv6 address churn must not reset every music API client.
        vpns = {name for name in vpns if is_vpn_interface(name)}
        candidate = (signature, frozenset(vpns))
        if self.signature is None:
            self.signature, self.vpns = signature, set(vpns)
        elif candidate != (self.signature, frozenset(self.vpns)):
            self._candidate_count = self._candidate_count + 1 if self._candidate == candidate else 1
            self._candidate = candidate
            if self._candidate_count >= 2:
                events.append({"type": "network_changed", "data": {"interfaces_changed": True}})
                now = time.monotonic()
                for change, names in (("connected", vpns-self.vpns), ("disconnected", self.vpns-vpns)):
                    for name in sorted(names):
                        key = (change, name.casefold())
                        # At most one same-direction notice per interface/minute;
                        # reconnection hooks still see every stable transition.
                        if now - self._vpn_announced_at.get(key, -1e9) >= 60:
                            events.append({"type": "notification", "message_key": "network.vpn_"+change, "data": {"name": name}})
                            self._vpn_announced_at[key] = now
                self._vpn_announced_at = {key: stamp for key, stamp in self._vpn_announced_at.items() if now-stamp < 3600}
                self.signature, self.vpns = signature, set(vpns)
                self._candidate = None
                self._candidate_count = 0
        else:
            self._candidate = None
            self._candidate_count = 0
        if latency is not None:
            online = latency >= 0
            if self.connected is None:
                self.connected = online
            elif online != self.connected:
                self._online_count = self._online_count + 1 if self._online_candidate == online else 1
                self._online_candidate = online
                if self._online_count >= 2:
                    events.append({"type": "network_changed", "data": {"online": online}})
                    self.connected = online
                    self._online_count = 0
            else:
                self._online_count = 0
                self._online_candidate = None
            self.last_probe = "offline" if not online else ("endpoint_failed" if context.get("endpoint_failed") else
                                                            ("slow" if latency > threshold else "ok"))
            # Outage: every probe failed three times in a row; back after two answers.
            if not online:
                self.offline_count += 1
                self.online_count = 0
            else:
                self.online_count += 1
                self.offline_count = 0
            if not self.offline and self.offline_count >= 3:
                self.offline = True
                self.transitions += 1
                # A slow-link state ends with the outage; recovery reports the outage only.
                self.high, self.high_count, self.low_count = False, 0, 0
                events.append({"type": "notification", "message_key": "network.offline", "data": {}})
            elif self.offline and self.online_count >= 2:
                self.offline = False
                self.transitions += 1
                events.append({"type": "notification", "message_key": "network.restored", "data": {}})
            if online and not self.offline:
                slow = latency > threshold
                self.high_count = self.high_count + 1 if slow else 0
                self.low_count = self.low_count + 1 if not slow and latency < threshold * .7 else 0
                if not self.high and self.high_count >= 3:
                    self.high = True
                    self.transitions += 1
                    data = {"latency_ms": round(latency)}
                    if context.get("downloading"):
                        data["downloading"] = True
                    events.append({"type": "notification", "message_key": "network.latency_high", "data": data})
                elif self.high and self.low_count >= 2:
                    self.high = False
                    self.transitions += 1
                    events.append({"type": "notification", "message_key": "network.latency_normal", "data": {}})
        return events


class DesktopServices:
    def __init__(self, config: Any, callback: Callable[[dict],None] | None = None):
        self.config = config
        self.callback = callback
        self.store = EventStore(config.paths.root,config)
        self.downloads = DownloadTracker(self.store)
        self.network = NetworkState()
        self._stop = threading.Event()
        self._reload = threading.Event()
        self._queue: queue.Queue = queue.Queue(maxsize=2048)
        self._thread = None
        self._observer = None
        self._drop_count = 0
        self._download_roots: list[Path] = []
        self._activity_roots: list[Path] = []
        self._activity_retry = None
        from jarvis.runtime.system_watch188 import Watchers
        self.watchers188 = Watchers()
        from jarvis.runtime.file_index import configure_index_policy
        configure_index_policy(config)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,name="NEO-desktop-events",daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=4)
        pending = getattr(self, "_activity_retry", None)
        if pending:
            logger.warning("Desktop activity shutdown with %s uncommitted history events after a storage error", len(pending["records"]))
        from jarvis.runtime.file_index import close_indexes
        close_indexes()

    def reconfigure(self):
        from jarvis.runtime.observation_policy import ObservationPolicy
        self.store.policy = ObservationPolicy(self.config)
        from jarvis.runtime.file_index import configure_index_policy
        configure_index_policy(self.config)
        self._reload.set()

    def _emit(self, event: dict):
        if callable(self.callback):
            self.callback(event)

    def _watch(self):
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
        from jarvis.runtime.file_index import known_folders
        self._download_roots = [Path(p) for p in (self.config.get("downloads.directories",[]) or known_folders("Downloads"))] if self.config.get("security.smart_downloads_enabled",False) else []
        default_activity = [p for name in ("Desktop", "Documents", "Downloads") for p in known_folders(name)]
        self._activity_roots = [Path(p) for p in (self.config.get("activity.directories",[]) or default_activity)] if self.config.get("activity.enabled",True) else []
        roots = {p.resolve(strict=False) for p in [*self._download_roots,*self._activity_roots] if p.is_dir()}
        if not roots:
            return
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
            service = self
            class Handler(FileSystemEventHandler):
                def on_any_event(self, event):
                    if event.is_directory or event.event_type not in {"created","modified","moved","deleted"}:
                        return
                    payload = {'path':event.src_path,'destination':getattr(event,'dest_path','')}
                    if not (service.store.policy.allows_event('activity.file',payload) or
                            service.store.policy.allows_event('download.observed',payload)):
                        return
                    try:
                        service._queue.put_nowait((event.event_type,event.src_path,getattr(event,"dest_path", "")))
                    except queue.Full:
                        service._drop_count += 1
            observer = Observer(timeout=1)
            handler = Handler()
            for root in roots:
                observer.schedule(handler,str(root),recursive=True)
            observer.start()
            self._observer = observer
        except ImportError:
            logger.warning("Filesystem notifications unavailable: watchdog is not installed")
        except OSError:
            logger.exception("Filesystem watcher could not start")

    @staticmethod
    def _under(path: str, roots: list[Path]):
        p = Path(path).resolve(strict=False)
        return any(p.is_relative_to(r.resolve(strict=False)) for r in roots)

    def observe_activity(self, payload: dict):
        if not self.config.get("activity.enabled",True):
            return
        if not self.store.policy.allows_event('activity.window',payload):
            return
        process = str(payload.get("process_name",""))
        title = str(payload.get("title",""))
        excluded = self.config.get("activity.excluded_apps",[]) or []
        private = ("incognito","inprivate","private browsing","приватн","инкогнито","navigation privée","privates fenster","navegación privada")
        if process.casefold() in {str(x).casefold() for x in excluded} or any(x in title.casefold() for x in private):
            return
        self.store.record("activity.window",{"process_name":process,"title":title[:300],"folder_path":payload.get("folder_path",""),
                                             'pid':payload.get('pid',0),'process_path':payload.get('process_path','')})

    def _network_probe(self):
        import psutil
        stats, addresses = psutil.net_if_stats(), psutil.net_if_addrs()
        signature = tuple(sorted((n,tuple(sorted(a.address for a in addresses.get(n,[]) if a.family in {socket.AF_INET,socket.AF_INET6}))) for n,s in stats.items() if s.isup))
        vpns = {name for name, _ in signature if is_vpn_interface(name)}
        latency = None
        context = {"endpoint_failed": False, "downloading": bool(getattr(getattr(self, "downloads", None), "active", None))}
        if self.config.get("security.latency_notifications_enabled",False):
            # Measures reachability/TCP latency, deliberately not bandwidth: no
            # speedtest, no test download. NEO 188 NEO-NET-001: when the main
            # endpoint fails, two independent light TCP probes (by IP, no DNS)
            # decide between «this endpoint failed» and «the internet is gone».
            latency = -1
            for index, target in enumerate((("www.searchapi.io", 443), ("1.1.1.1", 443), ("8.8.8.8", 443))):
                started = time.monotonic()
                try:
                    with socket.create_connection(target, timeout=1.5):
                        latency = (time.monotonic()-started)*1000
                    context["endpoint_failed"] = index > 0
                    break
                except OSError:
                    continue
            if not any(name for name, _ in signature if not str(name).casefold().startswith(("lo", "loopback"))):
                latency = -1          # no interface is up at all
        # 186: an observed edge is a pending notice until its row is committed.
        # Each notice carries a stable event id, so a retry after a storage error
        # (or after a commit whose acknowledgement was lost) records it once; the
        # reconnection hooks (network_changed) are emitted once and never replayed.
        pending = getattr(self, "_network_pending186", None)
        if pending is None:
            pending = self._network_pending186 = []
        for event in self.network.observe(signature,vpns,latency,float(self.config.get("security.latency_threshold_ms",250)),context):
            key = event.get("message_key","")
            if key.startswith("network.vpn") and not self.config.get("security.vpn_notifications_enabled",False):
                continue
            if event["type"] == "network_changed":
                self._emit(event)
            else:
                pending.append((uuid.uuid4().hex, key, event["data"]))
        del pending[:-32]
        while pending:
            event_id, key, data = pending[0]
            self.store.record(key, data, event_id, notify=True)
            pending.pop(0)

    def _observe_installers(self):
        """Observe real executable identity; a downloaded filename is not a launch.

        Exit means only the observed process ended, never installation success.
        MSI/bootstrap child completion requires a provider-specific signal.
        """
        import psutil
        completed = {os.path.normcase(str(Path(row["path"]).resolve(strict=False))): row
                     for row in self.store.downloads(200) if row["state"] == "completed" and Path(row["path"]).suffix.casefold() == ".exe"}
        running = self.store.running_installations()
        if not completed and not running:
            return
        live = {}
        for process in psutil.process_iter(["pid", "exe", "create_time"]):
            try:
                info = process.info
                pid = int(info["pid"])
                created = float(info.get("create_time") or 0.0)
                live[pid] = created
                executable = str(info.get("exe") or "")
                if not executable or not created:
                    continue
                key = os.path.normcase(str(Path(executable).resolve(strict=False)))
                if key in completed:
                    row = completed[key]
                    self.store.installation_started(row["id"], row["path"], pid, created)
            except (OSError, ValueError, psutil.Error):
                continue
        for row in running:
            current = live.get(row["pid"])
            if current and abs(current-row["process_created"]) <= .001:
                continue
            if current is None and psutil.pid_exists(row["pid"]):
                continue  # Access denied / snapshot race is not proof of exit.
            if current == 0:
                continue
            self.store.installation_ended(row["id"])

    def _run(self):
        while not self._stop.is_set():
            try:
                self._serve()
            except (OSError, sqlite3.Error):
                logger.exception("Desktop observers deferred after a transient storage/device error")
                if self._stop.wait(2.0):
                    break

    def _flush_activity_batch(self) -> bool:
        """Retry one bounded history slice without replaying download callbacks."""
        pending = getattr(self, "_activity_retry", None)
        if not pending:
            return True
        if time.monotonic() < pending["retry_at"]:
            return False
        try:
            # EventStore rechecks the current privacy policy on every attempt.
            # Stable IDs also make a retry safe if a commit result was ambiguous.
            self.store.record_activity_batch(pending["records"], event_ids=pending["ids"])
        except (OSError, sqlite3.Error):
            pending["attempts"] += 1
            if pending["attempts"] >= 3:
                logger.error("Desktop activity history dropped after 3 failed storage attempts: %s events", len(pending["records"]), exc_info=True)
                self._activity_retry = None
            else:
                pending["retry_at"] = time.monotonic() + 2 ** pending["attempts"]
                logger.warning("Desktop activity storage unavailable; retaining %s history events for retry %s/3", len(pending["records"]), pending["attempts"] + 1)
            return False
        self._activity_retry = None
        return True

    def _drain_file_events(self, recent: dict[tuple, float], *, max_events=24, budget_seconds=.008):
        """Consume a bounded slice; leave the remaining work for the next turn.

        File bursts previously caused up to 128 separate SQLite connections and
        commits without yielding. Batching is only for activity history; download
        state transitions retain their order and original transaction semantics.
        """
        if not self._flush_activity_batch():
            return 0
        started = time.monotonic()
        records = []
        processed = 0
        downloads = bool(self.config.get("security.smart_downloads_enabled", False))
        activity = bool(self.config.get("activity.enabled", True))
        excluded = [Path(p) for p in self.config.get("activity.excluded_paths", []) or []]
        try:
            for _ in range(max_events):
                if self._stop.is_set() or (processed and time.monotonic()-started >= budget_seconds):
                    break
                try:
                    kind, path, dest = self._queue.get_nowait()
                except queue.Empty:
                    break
                processed += 1
                now = time.monotonic()
                marker = (kind, path, dest)
                if now-recent.get(marker, -100) < .25:
                    continue
                recent[marker] = now
                if activity and self._under(path, self._activity_roots) and not self._under(path, excluded):
                    records.append(("activity.file."+kind, {"path": path, "destination": dest}))
                if downloads and self._under(path, self._download_roots):
                    self.downloads.on_file(path, destination=dest, event_type=kind)
        finally:
            # Keep prior observations even if a later download callback fails.
            if records:
                self._activity_retry = {"records": records, "ids": [uuid.uuid4().hex for _ in records],
                                        "attempts": 0, "retry_at": 0.0}
                self._flush_activity_batch()
        return processed

    def _serve(self):
        try:
            self._watch()
            self.store.prune(int(self.config.get("activity.retention_days",30)))
            next_network = next_reconcile = 0.
            next_prune = time.monotonic() + 3600
            recent: dict[tuple,float] = {}
            inflight: dict[str,float] = {}
            while not self._stop.wait(.08 if not self._queue.empty() and not self._activity_retry else .5):
                now = time.monotonic()
                if self._reload.is_set():
                    self._reload.clear()
                    self._watch()
                self._drain_file_events(recent)
                recent = {k:v for k,v in recent.items() if now-v < 2}
                if self._drop_count:
                    logger.warning("Desktop filesystem queue dropped %s events; current downloads will be reconciled",self._drop_count)
                    self._drop_count = 0
                if now >= next_reconcile and self.config.get("security.smart_downloads_enabled",False):
                    from jarvis.runtime.browser_downloads import poll as poll_browser_downloads
                    poll_browser_downloads(self.config,self.downloads)
                    self.downloads.reconcile()
                    next_reconcile = now + 2
                elif not self.config.get("security.smart_downloads_enabled",False):
                    observer = getattr(self.downloads,"_browser_download_observer",None)
                    if observer is not None:
                        observer.poll(self.config,self.downloads)  # Clear baseline, no filesystem/DB read.
                # Passive interface snapshots drive integration reconnection even
                # with both notification toggles off. No TCP probe in that case.
                if now>=next_network:
                    try:
                        self._network_probe()
                        if self.config.get("security.smart_downloads_enabled",False):
                            self._observe_installers()
                    except (OSError,RuntimeError):
                        logger.debug("Network observer unavailable",exc_info=True)
                    next_network = now+10
                if now>=next_prune:
                    self.store.prune(int(self.config.get("activity.retention_days",30)))
                    next_prune = now+3600
                watchers = getattr(self, "watchers188", None)
                if watchers is not None and now >= watchers.next_run and watchers.enabled(self.config):
                    # NEO 188 HREQ-020/021: temperatures, process spikes, Steam installs.
                    watchers.next_run = now + watchers.interval(self.config)
                    try:
                        for kind, payload in watchers.run(self.config, now):
                            self.store.record(kind, payload, notify=True)
                    except Exception:
                        logger.debug("System watchers unavailable", exc_info=True)
                prefixes = []
                if self.config.get("security.smart_downloads_enabled",False): prefixes.extend(("download.", "installation."))
                if self.config.get("security.vpn_notifications_enabled",False): prefixes.append("network.vpn_")
                if self.config.get("security.latency_notifications_enabled",False): prefixes.extend(("network.latency_", "network.restored", "network.offline"))
                if self.config.get("monitoring.hardware_alerts_enabled",False): prefixes.append("hardware.")
                if self.config.get("monitoring.process_alerts_enabled",False): prefixes.append("process.spike")
                if self.config.get("monitoring.steam_installs_enabled",False): prefixes.append("installation.steam_")
                for event in self.store.pending(prefixes=tuple(prefixes)):
                    event_id = event["event_id"]
                    if now-inflight.get(event_id,-100) < 5:
                        continue
                    try:
                        self._emit(event)
                        inflight[event_id] = now
                        # Delivery is acknowledged by the GUI after updating its
                        # event-id card; emission into Qt's queue is not delivery.
                    except Exception:
                        logger.debug("Desktop event delivery deferred",exc_info=True)
                inflight = {key:stamp for key,stamp in inflight.items() if now-stamp < 30}
        finally:
            if self._observer:
                self._observer.stop()
                self._observer.join(timeout=2)
                self._observer = None


_TEXT = {
 "ru":("Началась загрузка: {name}","Загрузка завершена: {name}","Ошибка загрузки: {name}","Загрузка отменена: {name}","Подключён VPN-интерфейс: {name}","Отключён VPN-интерфейс: {name}","Обнаружена высокая задержка или сбой сети.","Сетевое соединение восстановлено."),
 "en":("Download started: {name}","Download completed: {name}","Download failed: {name}","Download cancelled: {name}","VPN interface connected: {name}","VPN interface disconnected: {name}","High network latency or a connection problem detected.","Network connection restored."),
 "de":("Download gestartet: {name}","Download abgeschlossen: {name}","Download fehlgeschlagen: {name}","Download abgebrochen: {name}","VPN-Schnittstelle verbunden: {name}","VPN-Schnittstelle getrennt: {name}","Hohe Netzwerkverzögerung oder eine Verbindungsstörung erkannt.","Netzwerkverbindung wiederhergestellt."),
 "es":("Descarga iniciada: {name}","Descarga completada: {name}","Error de descarga: {name}","Descarga cancelada: {name}","Interfaz VPN conectada: {name}","Interfaz VPN desconectada: {name}","Se ha detectado una latencia alta o un problema de conexión.","Conexión de red restablecida."),
 "fr":("Téléchargement démarré : {name}","Téléchargement terminé : {name}","Échec du téléchargement : {name}","Téléchargement annulé : {name}","Interface VPN connectée : {name}","Interface VPN déconnectée : {name}","Une latence élevée ou un problème de connexion a été détecté.","Connexion réseau rétablie."),
 "it":("Download avviato: {name}","Download completato: {name}","Download non riuscito: {name}","Download annullato: {name}","Interfaccia VPN connessa: {name}","Interfaccia VPN disconnessa: {name}","Rilevata una latenza elevata o un problema di connessione.","Connessione di rete ripristinata."),
 "pt":("Download iniciado: {name}","Download concluído: {name}","Falha no download: {name}","Download cancelado: {name}","Interface VPN conectada: {name}","Interface VPN desconectada: {name}","Foi detetada uma latência elevada ou um problema de ligação.","Conexão de rede restabelecida."),
}
_KEYS=("download.started","download.completed","download.failed","download.cancelled","network.vpn_connected","network.vpn_disconnected","network.latency_high","network.restored")
_INSTALLATION_TEXT = {
 "ru": ("Запущен загруженный файл: {name}", "Процесс загруженного файла завершился: {name}. Результат установки не подтверждён."),
 "en": ("Downloaded program launched: {name}", "Downloaded program ended: {name}. Installation success has not been confirmed."),
 "de": ("Heruntergeladenes Programm gestartet: {name}", "Heruntergeladenes Programm beendet: {name}. Installationserfolg nicht bestätigt."),
 "es": ("Programa descargado iniciado: {name}", "Programa descargado terminado: {name}. Instalación no confirmada."),
 "fr": ("Programme téléchargé lancé : {name}", "Programme téléchargé terminé : {name}. Installation non confirmée."),
 "it": ("Programma scaricato avviato: {name}", "Programma scaricato terminato: {name}. Installazione non confermata."),
 "pt": ("Programa transferido iniciado: {name}", "Programa transferido terminado: {name}. Instalação não confirmada."),
}
_DOWNLOAD_OBSERVATION = {
 "ru": ("Файл больше не меняется: {name}. Завершение загрузки браузером не подтверждено.", "Состояние загрузки неизвестно, файл недоступен: {name}"),
 "en": ("File is no longer changing: {name}. Browser download completion is unconfirmed.", "Download status unknown; file unavailable: {name}"),
 "de": ("Datei ändert sich nicht mehr: {name}. Browser-Abschluss unbestätigt.", "Downloadstatus unbekannt; Datei nicht verfügbar: {name}"),
 "es": ("El archivo ya no cambia: {name}. Finalización del navegador no confirmada.", "Estado de descarga desconocido; archivo no disponible: {name}"),
 "fr": ("Le fichier ne change plus : {name}. Fin du téléchargement non confirmée par le navigateur.", "État du téléchargement inconnu ; fichier indisponible : {name}"),
 "it": ("Il file non cambia più: {name}. Fine del download non confermata dal browser.", "Stato del download sconosciuto; file non disponibile: {name}"),
 "pt": ("O ficheiro já não muda: {name}. Conclusão pelo navegador não confirmada.", "Estado da transferência desconhecido; ficheiro indisponível: {name}"),
}


_NET188 = {
 "network.latency_high": ("Сеть отвечает с высокой задержкой. Подключение к интернету при этом есть.",
                          "The network is responding slowly (high latency). The internet connection is still there.",
                          "Das Netzwerk antwortet langsam (hohe Latenz). Die Internetverbindung besteht weiterhin.",
                          "Le réseau répond lentement (latence élevée). La connexion Internet est toujours là.",
                          "La red responde con lentitud (latencia alta). La conexión a internet sigue activa.",
                          "La rete risponde lentamente (latenza elevata). La connessione a internet è ancora presente.",
                          "A rede está a responder devagar (latência elevada). A ligação à internet mantém-se."),
 "network.latency_high.downloading": ("Сеть отвечает с высокой задержкой, пока идёт загрузка. Подключение к интернету есть.",
                                      "The network is responding slowly while a download is running. The internet connection is there.",
                                      "Das Netzwerk antwortet langsam, während ein Download läuft. Die Internetverbindung besteht.",
                                      "Le réseau répond lentement pendant un téléchargement. La connexion Internet est là.",
                                      "La red responde con lentitud mientras hay una descarga. La conexión a internet está activa.",
                                      "La rete risponde lentamente mentre è in corso un download. La connessione a internet c’è.",
                                      "A rede está lenta enquanto decorre uma transferência. A ligação à internet mantém-se."),
 "network.latency_normal": ("Задержка сети снова в норме.", "Network latency is back to normal.", "Die Netzwerklatenz ist wieder normal.",
                            "La latence du réseau est revenue à la normale.", "La latencia de la red vuelve a ser normal.",
                            "La latenza di rete è tornata normale.", "A latência da rede voltou ao normal."),
 "network.offline": ("Похоже, подключение к интернету пропало: проверочные адреса не отвечают.",
                     "The internet connection seems to be lost: the check addresses do not answer.",
                     "Die Internetverbindung scheint unterbrochen: Die Prüfadressen antworten nicht.",
                     "La connexion Internet semble perdue : les adresses de contrôle ne répondent pas.",
                     "Parece que se ha perdido la conexión a internet: las direcciones de prueba no responden.",
                     "Sembra che la connessione a internet sia persa: gli indirizzi di controllo non rispondono.",
                     "Parece que a ligação à internet caiu: os endereços de verificação não respondem."),
 "network.restored": ("Подключение к интернету восстановлено.", "The internet connection is back.", "Die Internetverbindung ist wieder da.",
                      "La connexion Internet est rétablie.", "La conexión a internet se ha restablecido.",
                      "La connessione a internet è stata ripristinata.", "A ligação à internet foi restabelecida."),
}
_NET188_ORDER = ("ru", "en", "de", "fr", "es", "it", "pt")


def event_text(event: dict, language: str = "en") -> str:
    key=event.get("message_key","")
    if key.startswith(("hardware.temp_", "process.spike", "installation.steam_")):
        from jarvis.runtime.system_watch188 import event_text as watch_text
        return watch_text(key, dict(event.get("data") or {}), language)
    if key in _NET188:
        row = _NET188[key + ".downloading"] if key == "network.latency_high" and (event.get("data") or {}).get("downloading") else _NET188[key]
        return row[_NET188_ORDER.index(language) if language in _NET188_ORDER else 1]
    if key == "download.unknown" or (key == "download.completed" and event.get("data",{}).get("heuristic",False)):
        values = {"name":"", **event.get("data",{})}
        return _DOWNLOAD_OBSERVATION.get(language,_DOWNLOAD_OBSERVATION["en"])[int(key == "download.unknown")].format_map(values)
    if key in {"installation.started", "installation.ended"}:
        values = {"name":"", **event.get("data",{})}
        return _INSTALLATION_TEXT.get(language, _INSTALLATION_TEXT["en"])[int(key.endswith("ended"))].format_map(values)
    if key not in _KEYS:
        return ""
    values = {"name":"",**event.get("data",{})}
    return _TEXT.get(language,_TEXT["en"])[_KEYS.index(key)].format_map(values)
