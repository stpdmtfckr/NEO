"""Durable bounded desktop events. No audio, document contents or credentials."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from jarvis.runtime.observation_policy import ObservationPolicy


class EventStore:
    def __init__(self, root: Path, config=None):
        self.policy = ObservationPolicy(config, root=root)
        self.path = Path(root) / "data" / "desktop_events.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, created REAL NOT NULL,
              payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS events_kind_time ON events(kind,created);
            CREATE INDEX IF NOT EXISTS events_delivery_time ON events(delivered,created);
            CREATE TABLE IF NOT EXISTS downloads (
              id TEXT PRIMARY KEY, path TEXT NOT NULL, state TEXT NOT NULL,
              size INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL,
              source TEXT NOT NULL DEFAULT 'unknown', heuristic INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS downloads_path ON downloads(path,updated);
            CREATE TABLE IF NOT EXISTS download_aliases (
              source TEXT NOT NULL, provider_id TEXT NOT NULL, download_id TEXT NOT NULL,
              PRIMARY KEY(source,provider_id));
            CREATE TABLE IF NOT EXISTS installations (
              id TEXT PRIMARY KEY, download_id TEXT NOT NULL, path TEXT NOT NULL,
              pid INTEGER NOT NULL, process_created REAL NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(downloads)")}
            for name in ("file_identity", "actual_path"):
                if name not in columns:
                    db.execute(f"ALTER TABLE downloads ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            for name, definition in (("started", "REAL NOT NULL DEFAULT 0"),
                                     ("source_url", "TEXT NOT NULL DEFAULT ''"),
                                     ("app", "TEXT NOT NULL DEFAULT ''"),
                                     ("total_size", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    db.execute(f"ALTER TABLE downloads ADD COLUMN {name} {definition}")
            db.execute("CREATE INDEX IF NOT EXISTS downloads_actual_path ON downloads(actual_path,updated)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=3000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, kind: str, payload: dict, event_id: str | None = None, *, notify: bool = False) -> bool:
        if not self.policy.allows_event(kind, payload):
            return False
        with self.connect() as db:
            cur = db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)", (
                event_id or uuid.uuid4().hex, kind, time.time(),
                json.dumps(payload, ensure_ascii=False, default=str), 0 if notify else 1))
            return cur.rowcount > 0

    def record_activity_batch(self, records: list[tuple[str, dict]], *, event_ids: list[str] | None = None) -> int:
        """Persist a small watcher slice with one connection/commit.

        Apply the same privacy policy as record(); notification lifecycle writes
        stay on their existing transactional path and are never coalesced here.
        """
        if event_ids is not None and len(event_ids) != len(records):
            raise ValueError("Each activity record requires its own stable event ID")
        identities = event_ids if event_ids is not None else [uuid.uuid4().hex for _ in records]
        rows = [(identity, kind, time.time(),
                 json.dumps(payload, ensure_ascii=False, default=str), 1)
                for identity, (kind, payload) in zip(identities, records)
                if kind.startswith("activity.file.") and self.policy.allows_event(kind, payload)]
        if not rows:
            return 0
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)", rows)
        return len(rows)

    def pending(self, limit: int = 50, *, prefixes: tuple[str, ...] | None = None) -> list[dict]:
        where, values = "delivered=0", []
        if prefixes is not None:
            if not prefixes:
                return []
            where += " AND (" + " OR ".join("kind LIKE ? ESCAPE '\\'" for _ in prefixes) + ")"
            values.extend(prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%" for prefix in prefixes)
        wanted = max(1, min(200, limit))
        values.append(5000)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE " + where + " ORDER BY created LIMIT ?", values).fetchall()
        result = []
        suppressed = []
        for row in rows:
            payload = json.loads(row["payload"])
            if not self.policy.allows_event(row["kind"], payload):
                suppressed.append(row["id"])
                continue
            if len(result) >= wanted:
                continue
            result.append({"type":"notification", "event_id":row["id"], "message_key":row["kind"],
                           "created":row["created"], "data":payload, "correction":bool(payload.get("correction")),
                           "revision":int(payload.get("revision",0))})
        self._suppress(suppressed)
        return result

    def _suppress(self, ids):
        # Migration is persistent: old implementation noise never repeatedly
        # fills the queue and cannot reappear in an exported activity history.
        if ids:
            with self.connect() as db:
                db.executemany("UPDATE events SET delivered=2 WHERE id=?", ((i,) for i in ids))

    def acknowledge(self, event_id: str, revision: int | None = None):
        with self.connect() as db:
            if revision is None:
                db.execute("UPDATE events SET delivered=1 WHERE id=? AND delivered<>2", (event_id,))
            else:
                row = db.execute("SELECT payload FROM events WHERE id=?",(event_id,)).fetchone()
                if row and int(json.loads(row["payload"]).get("revision",0)) == revision:
                    db.execute("UPDATE events SET delivered=1 WHERE id=? AND payload=? AND delivered<>2",(event_id,row["payload"]))

    def query(self, *, kind: str = "", query: str = "", since: float = 0,
              until: float | None = None, limit: int = 100) -> list[dict]:
        clauses, values = ["created>=?", "created<=?", "delivered<>2"], [since, until if until is not None else time.time()]
        if kind:
            clauses.append("kind LIKE ? ESCAPE '\\'")
            values.append(kind.replace("%",r"\%").replace("_",r"\_")+"%")
        if query:
            clauses.append("payload LIKE ? ESCAPE '\\'")
            values.append("%"+query.replace("\\","\\\\").replace("%",r"\%").replace("_",r"\_")+"%")
        wanted = max(1,min(500,limit))
        values.append(min(5000,max(500,wanted*10)))
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE "+" AND ".join(clauses)+" ORDER BY created DESC LIMIT ?", values).fetchall()
        result, suppressed = [], []
        for row in rows:
            payload = json.loads(row['payload'])
            if not self.policy.allows_event(row['kind'],payload):
                suppressed.append(row['id'])
            elif len(result) < wanted:
                result.append({'id':row['id'],'kind':row['kind'],'created':row['created'],'data':payload})
        self._suppress(suppressed)
        return result

    def downloads(self, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            rows = [dict(x) for x in db.execute("SELECT * FROM downloads ORDER BY updated DESC LIMIT 2000")]
        return [r for r in rows if self.policy.allows_event('download.history',r)][:max(1,min(200,limit))]

    def active_downloads(self) -> list[dict]:
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM downloads WHERE state IN ('detected','downloading','paused') ORDER BY updated LIMIT 2000")]
        return [r for r in rows if self.policy.allows_event('download.history',r)]

    def latest_download(self, path: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM downloads WHERE path=? ORDER BY updated DESC LIMIT 1", (path,)).fetchone()
        return dict(row) if row is not None else None

    def provider_download(self, source: str, provider_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT d.* FROM downloads d JOIN download_aliases a ON a.download_id=d.id WHERE a.source=? AND a.provider_id=?",
                             (source, provider_id)).fetchone()
        return dict(row) if row is not None else None

    def installation_started(self, download_id: str, path: str, pid: int, process_created: float) -> str:
        """Called only after matching a real process executable to a download."""
        if not self.policy.allows_event('installation.started',{'path':path}):
            return ''
        ident = f"{download_id}:{int(pid)}:{process_created:.6f}"
        now = time.time()
        with self.connect() as db:
            row = db.execute("SELECT id FROM downloads WHERE id=? AND state='completed'", (download_id,)).fetchone()
            if row is None:
                raise ValueError("Installation must reference a completed tracked download")
            db.execute("INSERT OR IGNORE INTO installations VALUES(?,?,?,?,?,'running',?)", (ident, download_id, path, int(pid), process_created, now))
            db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,0)", (ident+":start", "installation.started", now,
                json.dumps({"path":path, "name":Path(path).name, "pid":int(pid), "installation_id":ident},ensure_ascii=False)))
        return ident

    def installation_ended(self, ident: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM installations WHERE id=? AND state='running'", (ident,)).fetchone()
            if row is None:
                return
            now = time.time()
            db.execute("UPDATE installations SET state='ended',updated=? WHERE id=?", (now,ident))
            db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,0)", (ident+":end", "installation.ended", now,
                json.dumps({"path":row["path"], "name":Path(row["path"]).name, "installation_id":ident, "success":None},ensure_ascii=False)))

    def running_installations(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM installations WHERE state='running'")]

    def transition_download(self, path: str, state: str, *, size: int = 0,
                            source: str = "unknown", download_id: str = "", heuristic: bool = True,
                            file_identity: str = "", actual_path: str = "", provider_id: str = "",
                            started: float = 0, source_url: str = "", app: str = "", total_size: int = 0,
                            notify: bool = True, allow_correction: bool = False) -> str:
        if state not in {"detected","downloading","paused","completed","failed","cancelled","unknown"}:
            raise ValueError("invalid download state")
        if not self.policy.allows_event('download.'+state,{'path':path,'actual_path':actual_path,'source_kind':source}):
            return ''
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if provider_id:
                alias = db.execute("SELECT download_id FROM download_aliases WHERE source=? AND provider_id=?",(source,provider_id)).fetchone()
                if alias:
                    download_id = alias["download_id"]
                else:
                    # Attach the browser's stable identity to the same observed
                    # lifecycle, not to a previous download reusing its filename.
                    candidates = db.execute("SELECT * FROM downloads WHERE (path=? OR actual_path=?) AND heuristic=1 ORDER BY updated DESC LIMIT 4",(path,actual_path or path)).fetchall()
                    for candidate in candidates:
                        initial = float(candidate["started"] or candidate["updated"])
                        same_time = bool(started and abs(initial-started) <= 180)
                        same_file = bool(file_identity and candidate["file_identity"] == file_identity)
                        active = candidate["state"] in {"detected","downloading","paused"}
                        overlaps = active or started <= float(candidate["updated"]) + .01
                        if same_time and overlaps and (same_file or active or abs(float(candidate["updated"])-now) <= 15):
                            download_id = candidate["id"]
                            break
            old = db.execute("SELECT * FROM downloads WHERE id=?", (download_id,)).fetchone() if download_id else db.execute(
                "SELECT * FROM downloads WHERE path=? ORDER BY updated DESC LIMIT 1", (path,)).fetchone()
            # Duplicate final notifications after a restart refer to the existing
            # session. New sessions must explicitly start with detected/downloading.
            terminal = {"completed","failed","cancelled","unknown"}
            if old and old["state"] in terminal and state in {"detected","downloading"} and not download_id:
                old = None
            did = old["id"] if old else (download_id or uuid.uuid4().hex)
            correction_allowed = bool(provider_id and (old is not None and (old["heuristic"] or allow_correction)))
            if old and old["state"] in terminal and state != old["state"] and not correction_allowed:
                return did
            # Reading startup history may silently seed an ongoing browser row.
            # An existing watcher lifecycle, however, still deserves its final.
            notify = bool(notify or (provider_id and old is not None and old["heuristic"]))
            identity = file_identity or (old["file_identity"] if old else "")
            actual = actual_path or (old["actual_path"] if old else "") or path
            initial = float(started or (old["started"] if old else 0) or now)
            metadata_url = source_url or (old["source_url"] if old else "")
            metadata_app = app or (old["app"] if old else "")
            total_size = max(0,int(total_size or (old["total_size"] if old else 0)))
            db.execute("INSERT INTO downloads(id,path,state,size,updated,source,heuristic,file_identity,actual_path,started,source_url,app,total_size) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,state=excluded.state,size=excluded.size,updated=excluded.updated,source=excluded.source,heuristic=excluded.heuristic,file_identity=excluded.file_identity,actual_path=excluded.actual_path,started=excluded.started,source_url=excluded.source_url,app=excluded.app,total_size=excluded.total_size",
                       (did,path,state,max(0,int(size)),now,source,int(heuristic),identity,actual,initial,metadata_url,metadata_app,total_size))
            if provider_id:
                db.execute("INSERT OR REPLACE INTO download_aliases VALUES(?,?,?)",(source,provider_id,did))
            if state == "downloading" or state in terminal:
                kind = "download.started" if state == "downloading" else "download."+state
                payload = {"download_id":did,"path":path,"name":Path(path).name,"size":max(0,int(size)),"source":source,"heuristic":heuristic,
                           "source_url":metadata_url,"app":metadata_app,"total_size":total_size}
                event_id = did+":download.started" if state == "downloading" else did+":terminal"
                existing = db.execute("SELECT * FROM events WHERE id=?",(event_id,)).fetchone()
                if state in terminal and existing is None:
                    legacy_ids = [did+":download."+value for value in sorted(terminal)]
                    existing = db.execute("SELECT * FROM events WHERE id IN (?,?,?,?) ORDER BY created LIMIT 1",legacy_ids).fetchone()
                    if existing:
                        event_id = existing["id"]
                if existing and provider_id:
                    previous = json.loads(existing["payload"])
                    was_correction = bool(previous.pop("correction",False))
                    revision = int(previous.pop("revision",0))
                    if previous != payload or existing["kind"] != kind:
                        # Correct the same card, including a premature filesystem
                        # completion, without issuing another start/end message.
                        payload["correction"] = bool(existing["delivered"] or was_correction)
                        payload["revision"] = revision+1
                        db.execute("UPDATE events SET kind=?,payload=?,delivered=? WHERE id=?",
                                   (kind,json.dumps(payload,ensure_ascii=False),0 if notify else 1,event_id))
                elif existing is None:
                    db.execute("INSERT INTO events VALUES(?,?,?,?,?)",
                               (event_id,kind,now,json.dumps(payload,ensure_ascii=False),0 if notify else 1))
            return did

    def clear_activity(self):
        with self.connect() as db:
            db.execute("DELETE FROM events WHERE kind NOT LIKE 'download.%' AND kind NOT LIKE 'installation.%'")

    def prune(self, days: int = 30):
        cutoff = time.time()-max(1,min(3650,days))*86400
        with self.connect() as db:
            # Retention also applies to notifications that were disabled for a
            # long period. Otherwise undelivered rows grow without any bound.
            db.execute("DELETE FROM events WHERE created<?",(cutoff,))
            db.execute("DELETE FROM downloads WHERE updated<? AND state IN ('completed','failed','cancelled','unknown')",(cutoff,))
            db.execute("DELETE FROM installations WHERE updated<? AND state='ended'",(cutoff,))
            db.execute("DELETE FROM download_aliases WHERE download_id NOT IN (SELECT id FROM downloads)")


def read_observed_history(config, *, scope: str, period: str = 'yesterday',
                          kind: str = '', query: str = '', limit: int = 100) -> dict:
    """Read bounded desktop evidence; never share it with another conversation.

    Calendar boundaries use the OS local timezone on each date, including DST.
    Recording being paused does not erase previously authorized observations.
    This is evidence of observed activity, not a complete operating-system log.
    """
    result = {'scope':scope,'available':False,'recording_enabled':bool(config.get('activity.enabled',True)),
              'source':'desktop_events','rows':[],'incomplete':True}
    if scope != 'desktop':
        return result
    now = datetime.now()
    midnight = now.replace(hour=0,minute=0,second=0,microsecond=0)
    if period == 'yesterday':
        start, end = midnight-timedelta(days=1), midnight
    elif period == 'today':
        start, end = midnight, now
    elif period in {'recent','all',''}:
        days = max(1,min(3650,int(config.get('activity.retention_days',30) or 30)))
        start, end = now-timedelta(days=days), now
    else:
        raise ValueError('Unsupported observed-history period')
    # Reading an absent journal does not initialize a database just to answer
    # a social question or imply that history had been recorded.
    path = Path(config.paths.root)/'data'/'desktop_events.sqlite3'
    result.update(since=start.timestamp(),until=end.timestamp(),timezone=str(now.astimezone().tzinfo))
    if not path.is_file():
        return result
    wanted = max(1,min(100,int(limit)))
    rows = EventStore(config.paths.root,config).query(kind=kind or 'activity',query=query,
                since=start.timestamp(),until=end.timestamp()-0.000001,limit=min(500,wanted*5))
    seen, visible = set(), []
    for row in rows:
        payload = row['data']
        # Collapse repeated focus/file notifications for the same observation
        # within a minute, retaining separate actions and different targets.
        key = (row['kind'],int(row['created']//60),str(payload.get('process_name',payload.get('process',''))).casefold(),
               str(payload.get('title','')),str(payload.get('path',payload.get('folder_path',''))),str(payload.get('destination','')))
        if key in seen:
            continue
        seen.add(key)
        visible.append(row)
        if len(visible) >= wanted:
            break
    result.update(available=True,rows=visible,incomplete=True)
    return result
