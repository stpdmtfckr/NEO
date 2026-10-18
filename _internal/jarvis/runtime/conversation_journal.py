"""Small scoped dialogue journal; never restores pending actions or credentials."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
import time

from jarvis.runtime.context_memory import _redact_text
from jarvis.runtime.diagnostics import _CREDENTIAL, _BEARER, _TELEGRAM
from jarvis.runtime.observation_policy import ObservationPolicy

_SCOPE = ContextVar("neo_conversation_scope", default="desktop")


def current_scope():
    return _SCOPE.get()


@contextmanager
def conversation_scope(scope):
    token = _SCOPE.set(str(scope))
    try:
        yield
    finally:
        _SCOPE.reset(token)


class ConversationJournal:
    def __init__(self, config):
        self.config = config
        self.path = Path(config.paths.root) / "data" / "conversation.sqlite3"
        self._ready = False

    def enabled(self):
        return bool(self.config.get("dialogue.memory.enabled", True)) and bool(self.config.get("activity.enabled", True)) and not bool(self.config.get("activity.paused", False))

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                if not self._ready:
                    db.executescript("""PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS turns(id INTEGER PRIMARY KEY,scope TEXT NOT NULL,created REAL NOT NULL,role TEXT NOT NULL,text TEXT NOT NULL,outcome TEXT NOT NULL DEFAULT '');
                    CREATE INDEX IF NOT EXISTS turns_scope_time ON turns(scope,created);
                    CREATE TABLE IF NOT EXISTS objects(id INTEGER PRIMARY KEY,scope TEXT NOT NULL,created REAL NOT NULL,tool TEXT NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS objects_scope_time ON objects(scope,created);
                    """)
                    self._ready = True
                yield db
        finally:
            db.close()

    def _safe(self, text):
        value = str(text or "")
        value = value if len(value) <= 6000 else _redact_text(value)
        # A credential-bearing turn is omitted, including its assistant echo.
        if _CREDENTIAL.search(value) or _BEARER.search(value) or _TELEGRAM.search(value) or re.search(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}", value) or re.search(r"(?i)\b(?:password|passwd|passwort|contraseña|senha|парол[ьяею]?|api[_ -]?key|access[_ -]?token|токен|ключ\s+api|mot\s+de\s+passe)\b", value):
            return None
        policy = ObservationPolicy(self.config, root=self.config.paths.root)
        for path in re.findall(r"[A-Za-z]:[\\/][^\r\n\"<>]+|/(?:home|Users|workspace)/[^\r\n\"<>]+", value):
            if not policy.allows_path(path.strip(" .,;!?")):
                return None
        for key in ("activity.excluded_paths", "activity.excluded_apps"):
            if any(str(item).casefold() in value.casefold() for item in self.config.get(key, []) or [] if str(item).strip()):
                return None
        return _redact_text(value)

    def _prune(self, db, scope):
        days = max(1, min(365, int(self.config.get("dialogue.memory.retention_days", 7) or 7)))
        cap = max(10, min(1000, int(self.config.get("dialogue.memory.max_turns", 200) or 200)))
        cutoff = time.time() - days * 86400
        for table in ("turns", "objects"):
            db.execute("DELETE FROM " + table + " WHERE created<?", (cutoff,))
            db.execute("DELETE FROM " + table + " WHERE scope=? AND id NOT IN (SELECT id FROM " + table + " WHERE scope=? ORDER BY id DESC LIMIT ?)", (scope, scope, cap))

    def record_turn(self, scope, request, answer, *, outcome="", now=None):
        if not self.enabled():
            return
        user, assistant = self._safe(request), self._safe(answer)
        if user is None or assistant is None:
            return
        stamp = time.time() if now is None else now
        with self.connect() as db:
            db.executemany("INSERT INTO turns(scope,created,role,text,outcome) VALUES(?,?,?,?,?)", [(scope, stamp, "user", user, ""), (scope, stamp, "assistant", assistant, str(outcome))])
            self._prune(db, scope)

    def record_execution(self, scope, tool, arguments, execution, *, now=None):
        if not self.enabled() or not execution.success or tool not in {"application", "filesystem", "desktop", "game", "window", "system"}:
            return
        data = execution.data if isinstance(execution.data, dict) else {}
        if any(data.get(key) for key in ("queued", "pending", "ui_request", "requires_confirmation", "outcome_pending")) or data.get("completed") is False:
            return
        allowed = {"name", "path", "source", "destination", "query", "kind", "process_name", "pid", "game_name", "game_id"}
        payload = {key: value for key, value in {**arguments, **data}.items() if key in allowed and isinstance(value, (str, int))}
        policy = ObservationPolicy(self.config, root=self.config.paths.root)
        if any(not policy.allows_path(str(payload[key])) for key in ("path", "source", "destination") if payload.get(key)):
            return
        if any(self._safe(value) is None for value in payload.values() if isinstance(value, str)):
            return
        encoded = json.dumps(payload, ensure_ascii=False)
        if self._safe(encoded) is None or not payload:
            return
        action = str(arguments.get("action", ""))
        with self.connect() as db:
            db.execute("INSERT INTO objects(scope,created,tool,action,payload) VALUES(?,?,?,?,?)", (scope, time.time() if now is None else now, tool, action, encoded))
            self._prune(db, scope)

    def recent(self, scope, *, limit=20):
        if not self.enabled() or not self.path.exists():
            return []
        with self.connect() as db:
            self._prune(db, scope)
            rows = db.execute("SELECT role,text,outcome,created FROM turns WHERE scope=? ORDER BY id DESC LIMIT ?", (scope, min(200, max(1, limit)))).fetchall()
        # NEO 188 URLP-05: old rows are re-checked on read; only the cleaned text leaves.
        result = []
        for row in reversed(rows):
            safe = self._safe(row["text"])
            if safe is not None:
                result.append(dict(dict(row), text=safe))
        return result

    def objects(self, scope, *, since=0, until=None, limit=30):
        if not self.enabled() or not self.path.exists():
            return []
        with self.connect() as db:
            self._prune(db, scope)
            rows = db.execute("SELECT created,tool,action,payload FROM objects WHERE scope=? AND created>=? AND created<? ORDER BY id DESC LIMIT ?", (scope, since, until or time.time() + 1, max(1, min(200, limit)))).fetchall()
        values = [{"created": row["created"], "tool": row["tool"], "action": row["action"],
                   "data": {key: (_redact_text(value) if isinstance(value, str) else value) for key, value in json.loads(row["payload"]).items()}}
                  for row in rows]
        policy = ObservationPolicy(self.config, root=self.config.paths.root)
        return [row for row in values if all(self._safe(v) is not None for v in row["data"].values() if isinstance(v, str)) and all(policy.allows_path(row["data"][key]) for key in ("path", "source", "destination") if row["data"].get(key))]

    def clear(self, scope):
        if not self.path.exists():
            return
        with self.connect() as db:
            db.execute("DELETE FROM turns WHERE scope=?", (scope,))
            db.execute("DELETE FROM objects WHERE scope=?", (scope,))


def local_period(config, period):
    from zoneinfo import ZoneInfo
    try:
        zone = ZoneInfo(str(config.get("app.timezone", "") or config.get("integrations.google_calendar.timezone", "") or ""))
        now = datetime.now(zone)
    except (ValueError, KeyError):
        # Naive local dates resolve their own UTC offset on timestamp(),
        # including yesterday's offset if a DST transition occurred overnight.
        now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "yesterday":
        return (start - timedelta(days=1)).timestamp(), start.timestamp()
    return start.timestamp(), now.timestamp() + 1
