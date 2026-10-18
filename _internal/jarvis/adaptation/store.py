# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Persistent Adaptive Learning Store (spec sections 5, 6, 78-80, 85-86).

One SQLite file, ``adaptive_memory.sqlite3``, next to the user's other data.
It is deliberately *not* ``assistant.db``: adaptive learning must be resettable
(section 98) without touching settings, history or any other user data.

Concurrency
-----------
NEO runs background workers, so the store is accessed from several threads.
Rather than sharing one connection across threads (the classic source of
``SQLite objects created in a thread can only be used in that same thread``),
each thread gets its own connection from :class:`_ConnectionPool`, and writes
are serialised by a process-wide ``RLock``.  Every connection is closed
explicitly on :meth:`AdaptiveStore.close` — ``with sqlite3.connect(...)`` only
commits, it does **not** close (section 78).

Corruption
----------
If the file is unreadable the store quarantines it and starts empty rather than
raising (section 80).  Losing learned aliases is a downgrade; failing to start
is a defect.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .confidence import ConfidenceInputs, ConfidenceModel, DEFAULT_MODEL
from .events import FeedbackStrength, LearningEvent, RuleKind, RuleState

logger = logging.getLogger(__name__)

__all__ = ["AdaptiveStore", "AdaptiveRule", "StoreUnavailable"]

SCHEMA_VERSION = 1
_DEFAULT_FILENAME = "adaptive_memory.sqlite3"


class StoreUnavailable(RuntimeError):
    """Raised internally when the backing file cannot be used at all."""


@dataclass(slots=True)
class AdaptiveRule:
    rule_id: int
    kind: str
    key: str
    value: str
    scope: str = "global"
    game_id: str = ""
    language: str = ""
    source: str = "runtime"
    state: str = RuleState.SHADOW
    confidence: float = 0.0
    strength: FeedbackStrength = FeedbackStrength.WEAK
    use_count: int = 0
    positive_count: int = 0
    negative_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    last_used: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.state == RuleState.ACTIVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "kind": self.kind,
            "key": self.key,
            "value": self.value,
            "scope": self.scope,
            "game_id": self.game_id,
            "language": self.language,
            "source": self.source,
            "state": self.state,
            "confidence": round(self.confidence, 4),
            "strength": FeedbackStrength.parse(self.strength).label,
            "use_count": self.use_count,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used": self.last_used,
            "payload": dict(self.payload),
        }


_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Aggregated belief. One row per (kind, scope, game, language, key, value).
CREATE TABLE IF NOT EXISTS rules (
    rule_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,
    scope          TEXT NOT NULL DEFAULT 'global',
    game_id        TEXT NOT NULL DEFAULT '',
    language       TEXT NOT NULL DEFAULT '',
    key            TEXT NOT NULL,
    value          TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT 'runtime',
    state          TEXT NOT NULL DEFAULT 'shadow',
    strength       INTEGER NOT NULL DEFAULT 1,
    confidence     REAL NOT NULL DEFAULT 0.0,
    use_count      INTEGER NOT NULL DEFAULT 0,
    positive_count INTEGER NOT NULL DEFAULT 0,
    negative_count INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL DEFAULT 0,
    last_used      REAL NOT NULL DEFAULT 0,
    payload        TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rules_identity
    ON rules(kind, scope, game_id, language, key, value);
CREATE INDEX IF NOT EXISTS idx_rules_lookup ON rules(kind, scope, state, confidence);
CREATE INDEX IF NOT EXISTS idx_rules_key    ON rules(kind, key);

-- Bounded audit trail. Consolidation folds these into `rules` and prunes.
CREATE TABLE IF NOT EXISTS learning_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT 'global',
    game_id    TEXT NOT NULL DEFAULT '',
    language   TEXT NOT NULL DEFAULT '',
    key        TEXT NOT NULL DEFAULT '',
    value      TEXT NOT NULL DEFAULT '',
    strength   INTEGER NOT NULL DEFAULT 1,
    positive   INTEGER NOT NULL DEFAULT 1,
    source     TEXT NOT NULL DEFAULT 'runtime',
    reason     TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0,
    consolidated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_created ON learning_events(created_at);
CREATE INDEX IF NOT EXISTS idx_events_pending ON learning_events(consolidated, created_at);

CREATE TABLE IF NOT EXISTS confidence_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id    INTEGER NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    delta      REAL NOT NULL DEFAULT 0,
    state      TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_confidence_rule ON confidence_history(rule_id, created_at);
"""

# The specification names nine entities. They share one physical table because
# their metadata and lifecycle are identical; these views give the requested
# names a real, queryable existence for tooling and debugging.
_VIEWS = {
    "speech_corrections": RuleKind.SPEECH_CORRECTION,
    "intent_examples": RuleKind.INTENT_EXAMPLE,
    "wake_candidates": RuleKind.WAKE_CANDIDATE,
    "wake_negatives": RuleKind.WAKE_NEGATIVE,
    "routing_corrections": RuleKind.ROUTING_CORRECTION,
    "user_vocabulary": RuleKind.USER_VOCABULARY,
}


class _ConnectionPool:
    """One connection per thread, all of them explicitly closeable."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.RLock()
        self._closed = False

    def get(self) -> sqlite3.Connection:
        if self._closed:
            raise StoreUnavailable("adaptive store is closed")
        con = getattr(self._local, "con", None)
        if con is not None:
            return con
        con = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA busy_timeout=5000")
            con.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error:
            logger.debug("Adaptive store pragma setup failed", exc_info=True)
        self._local.con = con
        with self._lock:
            self._all.append(con)
        return con

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for con in self._all:
                try:
                    con.close()
                except Exception:
                    logger.debug("Adaptive store connection close failed", exc_info=True)
            self._all.clear()
        self._local = threading.local()


class AdaptiveStore:
    """Persistent, thread-safe, self-healing learning store."""

    def __init__(
        self,
        path: str | Path,
        *,
        model: ConfidenceModel | None = None,
        max_events: int = 20000,
        enabled: bool = True,
    ) -> None:
        self.path = Path(path)
        self.model = model or DEFAULT_MODEL
        self.max_events = max(500, int(max_events))
        self._write_lock = threading.RLock()
        self._pool: _ConnectionPool | None = None
        self._degraded = not enabled
        self._degraded_reason = "" if enabled else "disabled_by_settings"
        if enabled:
            self._open()

    # -- lifecycle ------------------------------------------------------------ #

    @classmethod
    def for_config(cls, config: Any, *, model: ConfidenceModel | None = None) -> "AdaptiveStore":
        """Build a store from the app config, with sane defaults (section 81)."""
        enabled = True
        root: Path | None = None
        try:
            enabled = bool(config.get("adaptation.enabled", True))
        except Exception:
            enabled = True
        try:
            configured = str(config.get("adaptation.store_path", "") or "").strip()
            if configured:
                root = Path(os.path.expandvars(os.path.expanduser(configured)))
        except Exception:
            root = None
        if root is None:
            try:
                root = Path(str(config.paths.root)) / _DEFAULT_FILENAME
            except Exception:
                root = Path.home() / ".local" / "share" / "NeoJarvis" / _DEFAULT_FILENAME
        try:
            max_events = int(config.get("adaptation.max_events", 20000) or 20000)
        except Exception:
            max_events = 20000
        return cls(root, model=model, max_events=max_events, enabled=enabled)

    def _open(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._degrade(f"cannot create directory: {exc}")
            return
        try:
            self._pool = _ConnectionPool(self.path)
            con = self._pool.get()
            con.executescript(_DDL)
            self._install_views(con)
            self._migrate(con)
        except Exception as exc:
            # Section 80: a broken adaptive DB must never stop NEO from starting.
            logger.warning("Adaptive memory unusable (%s); attempting recovery", exc)
            if self._quarantine():
                try:
                    self._pool = _ConnectionPool(self.path)
                    con = self._pool.get()
                    con.executescript(_DDL)
                    self._install_views(con)
                    self._migrate(con)
                    logger.info("Adaptive memory recreated after recovery")
                    return
                except Exception as exc2:
                    self._degrade(f"recovery failed: {exc2}")
                    return
            self._degrade(str(exc))

    def _install_views(self, con: sqlite3.Connection) -> None:
        # Remove the retired game-entity alias view from profiles created by the
        # old local-corpus implementation. Existing audit rows are left inert.
        con.execute("DROP VIEW IF EXISTS entity_aliases")
        for view, kind in _VIEWS.items():
            con.execute(f"DROP VIEW IF EXISTS {view}")
            con.execute(
                f"CREATE VIEW {view} AS SELECT * FROM rules WHERE kind = '{kind}'"
            )

    def _migrate(self, con: sqlite3.Connection) -> None:
        """Forward-only migrations; user data is never dropped (section 79)."""
        row = con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        current = int(row["value"]) if row and str(row["value"]).isdigit() else 0
        if current > SCHEMA_VERSION:
            # A newer NEO wrote this file. Read it, do not rewrite the schema.
            logger.info("Adaptive memory schema v%s is newer than v%s; running read-compatible",
                        current, SCHEMA_VERSION)
            return
        if current == SCHEMA_VERSION:
            return
        # (No historical migrations yet — v0 means "freshly created".)
        con.execute(
            "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        con.execute(
            "INSERT INTO schema_meta(key,value) VALUES('created_at',?) "
            "ON CONFLICT(key) DO NOTHING",
            (str(time.time()),),
        )
        logger.info("Adaptive memory schema initialised at v%s (%s)", SCHEMA_VERSION, self.path)

    def _quarantine(self) -> bool:
        try:
            if not self.path.exists():
                return True
            broken = self.path.with_suffix(self.path.suffix + f".corrupt-{int(time.time())}")
            self.path.replace(broken)
            for suffix in ("-wal", "-shm"):
                side = Path(str(self.path) + suffix)
                if side.exists():
                    try:
                        side.unlink()
                    except OSError:
                        pass
            logger.warning("Quarantined corrupt adaptive memory to %s", broken.name)
            return True
        except Exception:
            logger.debug("Adaptive memory quarantine failed", exc_info=True)
            return False

    def _degrade(self, reason: str) -> None:
        self._degraded = True
        self._degraded_reason = reason
        if self._pool is not None:
            self._pool.close()
        self._pool = None
        logger.warning("Adaptive learning disabled for this session: %s", reason)

    @property
    def available(self) -> bool:
        return not self._degraded and self._pool is not None

    @property
    def degraded_reason(self) -> str:
        return self._degraded_reason

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def __enter__(self) -> "AdaptiveStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- low level ------------------------------------------------------------ #

    def _con(self) -> sqlite3.Connection | None:
        if not self.available:
            return None
        try:
            return self._pool.get()  # type: ignore[union-attr]
        except Exception as exc:
            self._degrade(str(exc))
            return None

    # -- writes --------------------------------------------------------------- #

    def record_event(self, event: LearningEvent) -> int:
        """Append to the audit log. Cheap; safe to call on a live turn."""
        con = self._con()
        if con is None:
            return 0
        try:
            with self._write_lock:
                cur = con.execute(
                    "INSERT INTO learning_events"
                    "(kind,scope,game_id,language,key,value,strength,positive,source,reason,payload,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.kind, event.scope, event.game_id, event.language,
                        event.key, event.value, int(FeedbackStrength.parse(event.strength)),
                        1 if event.positive else 0, event.source, event.reason,
                        json.dumps(event.payload, ensure_ascii=False), time.time(),
                    ),
                )
                return int(cur.lastrowid or 0)
        except Exception:
            logger.debug("Adaptive event write failed", exc_info=True)
            return 0

    def observe(
        self,
        event: LearningEvent,
        *,
        quality: ConfidenceInputs | None = None,
        log_event: bool = True,
    ) -> AdaptiveRule | None:
        """Fold one observation into the aggregated rule and recompute state.

        This is the *only* way belief changes.  It never stores raw duplicates:
        the 500th identical observation increments ``positive_count`` on one row
        (section 86).
        """
        con = self._con()
        if con is None:
            return None
        if log_event:
            self.record_event(event)
        strength = FeedbackStrength.parse(event.strength)
        now = time.time()
        try:
            with self._write_lock:
                row = con.execute(
                    "SELECT * FROM rules WHERE kind=? AND scope=? AND game_id=? AND language=?"
                    " AND key=? AND value=?",
                    (event.kind, event.scope, event.game_id, event.language, event.key, event.value),
                ).fetchone()

                if row is None:
                    positive = 1 if event.positive else 0
                    negative = 0 if event.positive else 1
                    con.execute(
                        "INSERT INTO rules"
                        "(kind,scope,game_id,language,key,value,source,state,strength,confidence,"
                        " use_count,positive_count,negative_count,created_at,updated_at,last_used,payload)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,0,?)",
                        (
                            event.kind, event.scope, event.game_id, event.language,
                            event.key, event.value, event.source, RuleState.SHADOW,
                            int(strength), 0.0, positive, negative, now, now,
                            json.dumps(event.payload, ensure_ascii=False),
                        ),
                    )
                    row = con.execute(
                        "SELECT * FROM rules WHERE kind=? AND scope=? AND game_id=? AND language=?"
                        " AND key=? AND value=?",
                        (event.kind, event.scope, event.game_id, event.language, event.key, event.value),
                    ).fetchone()
                    if row is None:
                        return None
                    old_confidence = 0.0
                    positive_count, negative_count = positive, negative
                    best_strength = strength
                    state = RuleState.SHADOW
                    created_at = now
                    use_count = 0
                    payload = dict(event.payload)
                else:
                    old_confidence = float(row["confidence"] or 0.0)
                    positive_count = int(row["positive_count"] or 0) + (1 if event.positive else 0)
                    # Weighted negative evidence: an explicit "no, that's wrong"
                    # outweighs a single incidental miss, so one STRONG rejection
                    # is enough to push a self-inferred rule out of `active`.
                    # The honest event count lives in payload["negative_events"].
                    negative_step = 0 if event.positive else (2 if strength is FeedbackStrength.STRONG else 1)
                    negative_count = int(row["negative_count"] or 0) + negative_step
                    # `strength` records the best *supporting* evidence a rule has
                    # ever had. A negative signal contradicts the rule; letting it
                    # raise the strength would raise the Beta prior and make a
                    # rejection look like a confirmation.
                    best_strength = FeedbackStrength.parse(row["strength"])
                    if event.positive:
                        best_strength = max(best_strength, strength)
                    state = str(row["state"] or RuleState.SHADOW)
                    created_at = float(row["created_at"] or now)
                    use_count = int(row["use_count"] or 0)
                    payload = _merge_payload(row["payload"], event.payload)
                    if not event.positive:
                        payload["negative_events"] = int(payload.get("negative_events", 0) or 0) + 1

                inputs = ConfidenceInputs(
                    positive_count=positive_count,
                    negative_count=negative_count,
                    strength=best_strength,
                    age_seconds=max(0.0, now - created_at),
                    idle_seconds=0.0,       # this observation *is* the recency
                    use_count=use_count,
                )
                if quality is not None:
                    inputs.asr_confidence = quality.asr_confidence
                    inputs.phonetic_similarity = quality.phonetic_similarity
                    inputs.context_match = quality.context_match
                    inputs.intent_match = quality.intent_match
                    inputs.game_match = quality.game_match

                confidence = self.model.score(inputs)
                new_state = self.model.next_state(
                    state, confidence,
                    positive_count=positive_count, negative_count=negative_count,
                    strength=best_strength,
                )
                rule_id = int(row["rule_id"])
                con.execute(
                    "UPDATE rules SET positive_count=?, negative_count=?, strength=?, confidence=?,"
                    " state=?, updated_at=?, payload=?, source=? WHERE rule_id=?",
                    (positive_count, negative_count, int(best_strength), confidence,
                     new_state, now, json.dumps(payload, ensure_ascii=False),
                     event.source or str(row["source"] or "runtime"), rule_id),
                )
                con.execute(
                    "INSERT INTO confidence_history(rule_id,confidence,delta,state,reason,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (rule_id, confidence, confidence - old_confidence, new_state,
                     event.reason or ("positive" if event.positive else "negative"), now),
                )
                if new_state != state:
                    logger.info(
                        "Adaptive rule %s kind=%s scope=%s '%s'->'%s' state=%s->%s confidence=%.3f (%s)",
                        rule_id, event.kind, event.scope, event.key, event.value,
                        state, new_state, confidence, FeedbackStrength.parse(best_strength).label,
                    )
                return self._row_to_rule(
                    con.execute("SELECT * FROM rules WHERE rule_id=?", (rule_id,)).fetchone()
                )
        except Exception:
            logger.debug("Adaptive observation failed", exc_info=True)
            return None

    def mark_used(self, rule_id: int) -> None:
        con = self._con()
        if con is None:
            return
        try:
            with self._write_lock:
                con.execute(
                    "UPDATE rules SET use_count = use_count + 1, last_used = ? WHERE rule_id = ?",
                    (time.time(), int(rule_id)),
                )
        except Exception:
            logger.debug("Adaptive use marking failed", exc_info=True)

    def set_state(self, rule_id: int, state: str, *, reason: str = "manual") -> bool:
        if state not in RuleState.ALL:
            return False
        con = self._con()
        if con is None:
            return False
        try:
            with self._write_lock:
                con.execute(
                    "UPDATE rules SET state=?, updated_at=? WHERE rule_id=?",
                    (state, time.time(), int(rule_id)),
                )
                con.execute(
                    "INSERT INTO confidence_history(rule_id,confidence,delta,state,reason,created_at)"
                    " SELECT rule_id, confidence, 0, ?, ?, ? FROM rules WHERE rule_id=?",
                    (state, reason, time.time(), int(rule_id)),
                )
            return True
        except Exception:
            logger.debug("Adaptive state change failed", exc_info=True)
            return False

    def forget(self, rule_id: int) -> bool:
        con = self._con()
        if con is None:
            return False
        try:
            with self._write_lock:
                con.execute("DELETE FROM rules WHERE rule_id=?", (int(rule_id),))
                con.execute("DELETE FROM confidence_history WHERE rule_id=?", (int(rule_id),))
            return True
        except Exception:
            logger.debug("Adaptive forget failed", exc_info=True)
            return False

    def reset(self, *, kind: str = "") -> int:
        """Erase learned adaptation without touching any other user data (section 98)."""
        con = self._con()
        if con is None:
            return 0
        try:
            with self._write_lock:
                if kind:
                    removed = con.execute("SELECT COUNT(*) AS n FROM rules WHERE kind=?", (kind,)).fetchone()["n"]
                    con.execute("DELETE FROM confidence_history WHERE rule_id IN "
                                "(SELECT rule_id FROM rules WHERE kind=?)", (kind,))
                    con.execute("DELETE FROM rules WHERE kind=?", (kind,))
                    con.execute("DELETE FROM learning_events WHERE kind=?", (kind,))
                else:
                    removed = con.execute("SELECT COUNT(*) AS n FROM rules").fetchone()["n"]
                    con.execute("DELETE FROM confidence_history")
                    con.execute("DELETE FROM rules")
                    con.execute("DELETE FROM learning_events")
                try:
                    con.execute("VACUUM")
                except sqlite3.Error:
                    pass
            logger.info("Adaptive learning reset (%s rules removed, kind=%s)", removed, kind or "all")
            return int(removed or 0)
        except Exception:
            logger.debug("Adaptive reset failed", exc_info=True)
            return 0

    # -- reads ---------------------------------------------------------------- #

    def rules(
        self,
        kind: str,
        *,
        scopes: Sequence[str] = (),
        states: Sequence[str] = (RuleState.ACTIVE,),
        language: str = "",
        key: str = "",
        limit: int = 500,
        min_confidence: float = 0.0,
    ) -> list[AdaptiveRule]:
        con = self._con()
        if con is None:
            return []
        clauses = ["kind = ?"]
        params: list[Any] = [kind]
        if scopes:
            clauses.append("scope IN (%s)" % ",".join("?" for _ in scopes))
            params.extend(scopes)
        if states:
            clauses.append("state IN (%s)" % ",".join("?" for _ in states))
            params.extend(states)
        if language:
            clauses.append("(language = ? OR language = '')")
            params.append(language)
        if key:
            clauses.append("key = ?")
            params.append(key)
        if min_confidence > 0:
            clauses.append("confidence >= ?")
            params.append(float(min_confidence))
        params.append(max(1, int(limit)))
        try:
            rows = con.execute(
                "SELECT * FROM rules WHERE " + " AND ".join(clauses) +
                " ORDER BY confidence DESC, positive_count DESC LIMIT ?",
                params,
            ).fetchall()
            return [self._row_to_rule(r) for r in rows if r is not None]
        except Exception:
            logger.debug("Adaptive rule read failed", exc_info=True)
            return []

    def rule_count(self, kind: str = "") -> int:
        con = self._con()
        if con is None:
            return 0
        try:
            if kind:
                row = con.execute("SELECT COUNT(*) AS n FROM rules WHERE kind=?", (kind,)).fetchone()
            else:
                row = con.execute("SELECT COUNT(*) AS n FROM rules").fetchone()
            return int(row["n"] if row else 0)
        except Exception:
            return 0

    def pending_events(self, limit: int = 2000) -> list[sqlite3.Row]:
        con = self._con()
        if con is None:
            return []
        try:
            return list(con.execute(
                "SELECT * FROM learning_events WHERE consolidated = 0 ORDER BY created_at LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall())
        except Exception:
            return []

    def snapshot(self) -> dict[str, Any]:
        """Everything the settings UI needs to show 'what NEO has learned'."""
        out: dict[str, Any] = {
            "available": self.available,
            "path": str(self.path),
            "degraded_reason": self._degraded_reason,
            "schema_version": SCHEMA_VERSION,
            "counts": {},
            "events": 0,
        }
        con = self._con()
        if con is None:
            return out
        try:
            for kind in RuleKind.ALL:
                row = con.execute(
                    "SELECT COUNT(*) AS total,"
                    " SUM(CASE WHEN state='active' THEN 1 ELSE 0 END) AS active,"
                    " SUM(CASE WHEN state='shadow' THEN 1 ELSE 0 END) AS shadow,"
                    " SUM(CASE WHEN state='blocked' THEN 1 ELSE 0 END) AS blocked"
                    " FROM rules WHERE kind=?", (kind,),
                ).fetchone()
                out["counts"][kind] = {
                    "total": int(row["total"] or 0),
                    "active": int(row["active"] or 0),
                    "shadow": int(row["shadow"] or 0),
                    "blocked": int(row["blocked"] or 0),
                }
            row = con.execute("SELECT COUNT(*) AS n FROM learning_events").fetchone()
            out["events"] = int(row["n"] or 0)
        except Exception:
            logger.debug("Adaptive snapshot failed", exc_info=True)
        return out

    # -- maintenance ---------------------------------------------------------- #

    def decay(self, *, now_ts: float | None = None) -> int:
        """Recompute confidence with recency decay and re-evaluate state."""
        con = self._con()
        if con is None:
            return 0
        now_ts = float(now_ts if now_ts is not None else time.time())
        changed = 0
        try:
            with self._write_lock:
                rows = con.execute(
                    "SELECT * FROM rules WHERE state IN (?,?)", (RuleState.ACTIVE, RuleState.SHADOW)
                ).fetchall()
                for row in rows:
                    last_touch = max(float(row["last_used"] or 0.0), float(row["updated_at"] or 0.0))
                    idle = max(0.0, now_ts - last_touch) if last_touch else 0.0
                    strength = FeedbackStrength.parse(row["strength"])
                    inputs = ConfidenceInputs(
                        positive_count=int(row["positive_count"] or 0),
                        negative_count=int(row["negative_count"] or 0),
                        strength=strength,
                        age_seconds=max(0.0, now_ts - float(row["created_at"] or now_ts)),
                        idle_seconds=idle,
                        use_count=int(row["use_count"] or 0),
                    )
                    confidence = self.model.score(inputs)
                    state = self.model.next_state(
                        str(row["state"] or RuleState.SHADOW), confidence,
                        positive_count=inputs.positive_count,
                        negative_count=inputs.negative_count,
                        strength=strength,
                    )
                    if abs(confidence - float(row["confidence"] or 0.0)) < 1e-4 and state == row["state"]:
                        continue
                    con.execute(
                        "UPDATE rules SET confidence=?, state=? WHERE rule_id=?",
                        (confidence, state, int(row["rule_id"])),
                    )
                    changed += 1
        except Exception:
            logger.debug("Adaptive decay failed", exc_info=True)
        return changed

    def prune(self, *, keep_events: int | None = None, max_age_days: float = 120.0) -> dict[str, int]:
        """Retention (section 85). Rules survive; the raw audit log does not."""
        con = self._con()
        if con is None:
            return {"events": 0, "history": 0, "rules": 0}
        keep = int(keep_events if keep_events is not None else self.max_events)
        cutoff = time.time() - max(1.0, float(max_age_days)) * 86400.0
        removed = {"events": 0, "history": 0, "rules": 0}
        try:
            with self._write_lock:
                cur = con.execute(
                    "DELETE FROM learning_events WHERE event_id NOT IN "
                    "(SELECT event_id FROM learning_events ORDER BY created_at DESC LIMIT ?)",
                    (keep,),
                )
                removed["events"] = int(cur.rowcount or 0)
                cur = con.execute(
                    "DELETE FROM confidence_history WHERE created_at < ?", (cutoff,)
                )
                removed["history"] = int(cur.rowcount or 0)
                cur = con.execute(
                    "DELETE FROM rules WHERE state=? AND updated_at < ?",
                    (RuleState.RETIRED, cutoff),
                )
                removed["rules"] = int(cur.rowcount or 0)
        except Exception:
            logger.debug("Adaptive prune failed", exc_info=True)
        return removed

    def mark_consolidated(self, event_ids: Iterable[int]) -> None:
        ids = [int(i) for i in event_ids]
        if not ids:
            return
        con = self._con()
        if con is None:
            return
        try:
            with self._write_lock:
                con.executemany(
                    "UPDATE learning_events SET consolidated = 1 WHERE event_id = ?",
                    [(i,) for i in ids],
                )
        except Exception:
            logger.debug("Adaptive consolidation marking failed", exc_info=True)

    # -- helpers -------------------------------------------------------------- #

    @staticmethod
    def _row_to_rule(row: sqlite3.Row | None) -> AdaptiveRule:
        if row is None:
            return AdaptiveRule(0, "", "", "")
        try:
            payload = json.loads(row["payload"] or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        return AdaptiveRule(
            rule_id=int(row["rule_id"]),
            kind=str(row["kind"] or ""),
            key=str(row["key"] or ""),
            value=str(row["value"] or ""),
            scope=str(row["scope"] or "global"),
            game_id=str(row["game_id"] or ""),
            language=str(row["language"] or ""),
            source=str(row["source"] or "runtime"),
            state=str(row["state"] or RuleState.SHADOW),
            confidence=float(row["confidence"] or 0.0),
            strength=FeedbackStrength.parse(row["strength"]),
            use_count=int(row["use_count"] or 0),
            positive_count=int(row["positive_count"] or 0),
            negative_count=int(row["negative_count"] or 0),
            created_at=float(row["created_at"] or 0.0),
            updated_at=float(row["updated_at"] or 0.0),
            last_used=float(row["last_used"] or 0.0),
            payload=payload,
        )


def _merge_payload(raw: Any, extra: Mapping[str, Any]) -> dict[str, Any]:
    try:
        base = json.loads(raw or "{}")
        if not isinstance(base, dict):
            base = {}
    except Exception:
        base = {}
    base.update({k: v for k, v in (extra or {}).items()})
    return base
