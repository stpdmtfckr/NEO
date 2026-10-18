# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _audit_row(item: dict[str, Any]) -> dict[str, Any]:
    """NEO 188 URLP-06: legacy audit rows are re-sanitized whenever they are read."""
    from jarvis.runtime.diagnostics import redact_credentials, sanitize_credentials
    try:
        arguments = json.loads(str(item.pop("arguments_json", "{}") or "{}"))
    except Exception:
        arguments = {}
    item["arguments"] = sanitize_credentials(arguments) if isinstance(arguments, dict) else {}
    item["result_text"] = redact_credentials(str(item.get("result_text", "") or ""))
    item["success"] = bool(item.get("success"))
    return item


def _compact_action_result(value: str, limit: int = 2400) -> str:
    """Persist useful tool history without volatile media payload bloat."""
    raw = str(value or "")
    try:
        payload = json.loads(raw)
    except Exception:
        return raw[:limit]

    volatile_keys = {"levels", "audio_levels", "spectrum", "waveform", "pcm", "audio_b64"}

    def compact(item: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "<omitted>"
        if isinstance(item, dict):
            out: dict[str, Any] = {}
            for key, val in list(item.items())[:80]:
                skey = str(key)
                low = skey.casefold()
                if low in volatile_keys:
                    continue
                if low in {"url", "stream_url", "audio_url"} and isinstance(val, str) and len(val) > 320:
                    out[skey] = "<omitted long media URL>"
                else:
                    out[skey] = compact(val, depth + 1)
            return out
        if isinstance(item, (list, tuple)):
            rows = [compact(x, depth + 1) for x in list(item)[:24]]
            if len(item) > 24:
                rows.append(f"<+{len(item) - 24} items>")
            return rows
        if isinstance(item, str):
            return item if len(item) <= 600 else item[:600] + "…"
        return item

    try:
        text = json.dumps(compact(payload), ensure_ascii=False, default=str)
    except Exception:
        text = raw
    return text[:limit]


class Database:
    def __init__(self, path: Path, *, prune_history: bool = True):
        self.path = path
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None
        self._initialize(prune_history=prune_history)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _db(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            connection = self._connect()
            self._connection = connection
        return connection

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

    def _initialize(self, *, prune_history: bool = True) -> None:
        with closing(self._connect()) as db:
            # WAL keeps readers from blocking action/memory writes and normally
            # avoids a journal-file create/delete cycle on every committed action.
            # SQLite keeps its default FULL durability; this is a performance mode,
            # not a reduced-safety synchronous setting.
            try:
                db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS action_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    result_text TEXT NOT NULL
                );
                """
            )
            # Action history is a recent-context/undo aid, not an unbounded
            # telemetry warehouse. Keep enough rows for diagnostics while making
            # startup recover old installations whose media polling created very
            # large databases.
            if prune_history:
                try:
                    db.execute(
                        "DELETE FROM action_log WHERE id < (SELECT COALESCE(MAX(id), 0) - 5000 FROM action_log)"
                    )
                except sqlite3.Error:
                    pass
            db.commit()

    def set_memory(self, key: str, value: str) -> None:
        key = key.strip()
        if not key:
            raise ValueError("Memory key cannot be empty")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            db = self._db()
            try:
                db.execute(
                    """
                    INSERT INTO memories(key, value, updated_at) VALUES(?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (key, value.strip(), now),
                )
                db.commit()
            except BaseException:
                self._rollback_memory_write(db)
                raise

    def _rollback_memory_write(self, db) -> None:
        try:
            db.rollback()
        except Exception:
            # A connection that cannot roll back must never commit the failed
            # mutation together with a later, unrelated write.
            self._connection = None
            try:
                db.close()
            except Exception:
                pass

    def get_memory(self, key: str, default: str = "") -> str:
        with self._lock:
            db = self._db()
            row = db.execute("SELECT value FROM memories WHERE key = ?", (key.strip(),)).fetchone()
        return str(row["value"] if row is not None else default)

    def delete_memory(self, key: str) -> bool:
        with self._lock:
            db = self._db()
            try:
                cur = db.execute("DELETE FROM memories WHERE key = ?", (key.strip(),))
                db.commit()
            except BaseException:
                self._rollback_memory_write(db)
                raise
            return cur.rowcount > 0

    def change_memory_snapshot(self, rows: list[dict[str, str]], *, value: str | None = None) -> bool:
        """Update one row or delete a reviewed set atomically, only if unchanged.

        The memory domain validates scope and values before calling this storage API.
        A stale snapshot or a failed commit leaves every row unchanged.
        """
        if value is not None and len(rows) != 1:
            raise ValueError("Updates require one memory row")
        if not rows or len({row['key'] for row in rows}) != len(rows):
            return False
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            db = self._db()
            try:
                for row in rows:
                    expected = (row['key'], row['value'], row['updated_at'])
                    if value is None:
                        cur = db.execute("DELETE FROM memories WHERE key=? AND value=? AND updated_at=?", expected)
                    else:
                        cur = db.execute("UPDATE memories SET value=?, updated_at=? WHERE key=? AND value=? AND updated_at=?",
                                         (value, now, *expected))
                    if cur.rowcount != 1:
                        db.rollback()
                        return False
                db.commit()
            except BaseException:
                self._rollback_memory_write(db)
                raise
        return True

    def list_memories(self, limit: int = 50) -> list[dict[str, str]]:
        with self._lock:
            db = self._db()
            rows = db.execute(
                "SELECT key, value, updated_at FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_user_memories(self, exclude_prefixes: tuple[str, ...], limit: int = 500) -> list[dict[str, str]]:
        """NEO 184: newest records whose key starts with none of ``exclude_prefixes``.

        The prefixes are filtered in SQL *before* the limit, so thousands of internal
        records can never hide an older user record."""
        clauses = " AND ".join("substr(key, 1, ?) != ?" for _ in exclude_prefixes) or "1"
        params: list[Any] = []
        for prefix in exclude_prefixes:
            params.extend((len(prefix), prefix))
        params.append(int(limit))
        with self._lock:
            db = self._db()
            rows = db.execute(
                f"SELECT key, value, updated_at FROM memories WHERE {clauses} ORDER BY updated_at DESC LIMIT ?", params
            ).fetchall()
        return [dict(row) for row in rows]

    def log_action(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        risk: str,
        success: bool,
        result_text: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        # NEO 188 NEO-PRIVACY-003: the audit is a derived copy; the executed
        # arguments themselves were not changed. Redact before compacting.
        from jarvis.runtime.diagnostics import redact_credentials, sanitize_credentials
        compact_result = _compact_action_result(redact_credentials(str(result_text or "")))
        arguments = sanitize_credentials(arguments) if isinstance(arguments, dict) else arguments
        with self._lock:
            db = self._db()
            cur = db.execute(
                """
                INSERT INTO action_log(created_at, tool_name, arguments_json, risk, success, result_text)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False),
                    risk,
                    1 if success else 0,
                    compact_result,
                ),
            )
            # Prune in coarse batches so the common tool path still pays only one
            # INSERT/commit. SQLite rowids are monotonic for this table.
            try:
                row_id = int(cur.lastrowid or 0)
                if row_id and row_id % 256 == 0:
                    db.execute("DELETE FROM action_log WHERE id < ?", (max(0, row_id - 5000),))
            except Exception:
                pass
            db.commit()

    def recent_actions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            db = self._db()
            rows = db.execute(
                "SELECT id, created_at, tool_name, arguments_json, risk, success, result_text FROM action_log ORDER BY id DESC LIMIT ?",
                (max(1, min(1000, int(limit))),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(_audit_row(dict(row)))
        return result

    def action_by_id(self, action_id: int) -> dict[str, Any] | None:
        with self._lock:
            db = self._db()
            row = db.execute(
                "SELECT id, created_at, tool_name, arguments_json, risk, success, result_text FROM action_log WHERE id=?",
                (int(action_id),),
            ).fetchone()
        if row is None:
            return None
        return _audit_row(dict(row))
