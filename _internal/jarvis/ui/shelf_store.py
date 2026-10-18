"""Persistent, Qt-free shared desktop object service for UI, voice and Telegram.

Deletion removes a card only. Managed assets are retained for recovery; external
originals are never modified. Every mutation is a SQLite transaction and updates
require the version the caller actually viewed (optimistic concurrency).
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any
import uuid

from jarvis.ui.desktop_events import request_desktop


class VersionConflict(ValueError):
    pass


class ShelfStore:
    KINDS = frozenset({"note", "checklist", "file", "image", "text", "link"})
    EDITABLE = frozenset({"title", "content", "items", "pinned", "position", "size", "screen", "dpi", "shelf", "board", "order", "visible"})
    MAX_TEXT = 250_000
    MAX_FILE_BYTES = 512 * 1024 * 1024

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "data" / "shelf"
        self.assets = self.root / "assets"
        self.assets.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "objects.sqlite3"
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY, version INTEGER NOT NULL, updated REAL NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, document TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS versions (id TEXT NOT NULL, version INTEGER NOT NULL, updated REAL NOT NULL, document TEXT NOT NULL, PRIMARY KEY(id,version))")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, updated REAL NOT NULL, document TEXT NOT NULL)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _decode(row) -> dict[str, Any]:
        if row is None:
            raise KeyError("Shelf object not found")
        return json.loads(row[0])

    def get(self, object_id: str) -> dict[str, Any]:
        with self._db() as db:
            return self._decode(db.execute("SELECT document FROM objects WHERE id=? AND deleted=0", (object_id,)).fetchone())

    def list(self, kind: str | None = None, shelf: str | None = None) -> list[dict[str, Any]]:
        with self._db() as db:
            items = [json.loads(row[0]) for row in db.execute("SELECT document FROM objects WHERE deleted=0 ORDER BY updated,id")]
        items = [x for x in items if (kind is None or x["kind"] == kind) and (shelf is None or x.get("shelf") == shelf)]
        return sorted(items, key=lambda x: (int(x.get("order", 0)), x["created"], x["id"]))

    @classmethod
    def _validate(cls, document: dict[str, Any]) -> None:
        if document.get("kind") not in cls.KINDS:
            raise ValueError("Unsupported shelf object kind")
        for field in ("title", "content"):
            document[field] = str(document.get(field, ""))
            if len(document[field]) > cls.MAX_TEXT:
                raise ValueError("Shelf text exceeds limit")
        document["title"] = document["title"][:200]
        items = document.get("items", []) or []
        if not isinstance(items, list) or len(items) > 5000:
            raise ValueError("Checklist exceeds limit")
        document["items"] = [{"text": str(x.get("text", ""))[:5000], "checked": bool(x.get("checked", False))} if isinstance(x, dict) else {"text": str(x)[:5000], "checked": False} for x in items]
        for field, default in (("size", [420, 360]), ("position", [])):
            raw = document.get(field, default)
            if not isinstance(raw, (tuple, list)) or len(raw) not in (0, 2):
                raise ValueError("Invalid desktop geometry")
            document[field] = [int(x) for x in raw]
        document["shelf"] = str(document.get("shelf", "default"))[:80]
        document["board"] = str(document.get("board", ""))
        if document["board"] not in {"", "checklist", "files", "notes"}:
            raise ValueError("Unsupported shelf section")
        document["order"] = int(document.get("order", 0))
        document["pinned"] = bool(document.get("pinned", False))

    def create(self, kind: str, title: str = "", content: str = "", items: list | None = None, path: str = "", managed: bool = False, **metadata: Any) -> dict[str, Any]:
        now = time.time()
        doc = {"id": uuid.uuid4().hex, "kind": kind, "title": title, "content": content, "items": items or [], "path": str(path), "managed": bool(managed), "version": 1, "created": now, "updated": now, "pinned": False, "position": [], "size": [420, 360], "screen": "", "dpi": 96, "shelf": "default", "order": 0, "visible": True}
        doc.update({k: v for k, v in metadata.items() if k in self.EDITABLE})
        self._validate(doc)
        raw = json.dumps(doc, ensure_ascii=False)
        with self._db() as db:
            db.execute("INSERT INTO objects(id,version,updated,document) VALUES(?,?,?,?)", (doc["id"], 1, now, raw))
            db.execute("INSERT INTO versions VALUES(?,?,?,?)", (doc["id"], 1, now, raw))
        request_desktop("shelf.refresh")
        return doc

    def update(self, object_id: str, expected_version: int, **changes: Any) -> dict[str, Any]:
        unknown = set(changes) - self.EDITABLE
        if unknown:
            raise ValueError("Uneditable shelf fields: " + ", ".join(sorted(unknown)))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            doc = self._decode(db.execute("SELECT document FROM objects WHERE id=? AND deleted=0", (object_id,)).fetchone())
            if doc["version"] != int(expected_version):
                raise VersionConflict("Object changed in another window. Reload before saving.")
            doc.update(changes)
            doc.update(version=doc["version"] + 1, updated=time.time())
            self._validate(doc)
            raw = json.dumps(doc, ensure_ascii=False)
            db.execute("UPDATE objects SET version=?,updated=?,document=? WHERE id=?", (doc["version"], doc["updated"], raw, object_id))
            db.execute("INSERT INTO versions VALUES(?,?,?,?)", (object_id, doc["version"], doc["updated"], raw))
            # A bounded version journal keeps user recovery without unbounded growth.
            db.execute("DELETE FROM versions WHERE id=? AND version < ?", (object_id, max(1, doc["version"] - 49)))
        request_desktop("shelf.refresh")
        return doc

    def delete(self, object_id: str, expected_version: int | None = None) -> None:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT version FROM objects WHERE id=? AND deleted=0", (object_id,)).fetchone()
            if not row:
                raise KeyError(object_id)
            if expected_version is not None and row[0] != expected_version:
                raise VersionConflict("Object changed before deletion")
            db.execute("UPDATE objects SET deleted=1,updated=? WHERE id=?", (time.time(), object_id))
        request_desktop("shelf.refresh")

    def history(self, object_id: str) -> list[dict[str, Any]]:
        with self._db() as db:
            return [json.loads(x[0]) for x in db.execute("SELECT document FROM versions WHERE id=? ORDER BY version DESC", (object_id,))]

    def save_draft(self, base: dict[str, Any], changes: dict[str, Any]) -> None:
        """Keep a conflicting local edit independently of the current object.

        This is recovery data, never an implicit overwrite or a newer object
        version. A subsequent editor must resolve the conflict explicitly.
        """
        if base.get("kind") not in {"note", "text", "checklist"}:
            raise ValueError("Only text widgets have recoverable drafts")
        candidate = {**base, **{k: v for k, v in changes.items() if k in self.EDITABLE}}
        self._validate(candidate)
        payload = {"base": base, "changes": {k: candidate[k] for k in changes if k in self.EDITABLE}}
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO drafts VALUES(?,?,?)", (base["id"], time.time(), json.dumps(payload, ensure_ascii=False)))

    def draft(self, object_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT document FROM drafts WHERE id=?", (object_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def drafts(self) -> list[dict[str, Any]]:
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT document FROM drafts ORDER BY updated")]

    def clear_draft(self, object_id: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM drafts WHERE id=?", (object_id,))

    @staticmethod
    def board_for(document: dict[str, Any]) -> str:
        return document.get("board") or ("checklist" if document["kind"] == "checklist" else "notes" if document["kind"] in {"note", "text"} else "files")

    def place(self, object_ids: list[str], *, shelf: str, board: str, index: int) -> None:
        """Move/reorder cards atomically without changing their kinds or content."""
        if board not in {"checklist", "files", "notes"} or not isinstance(object_ids, list) or len(object_ids) > 1000 or not all(isinstance(x, str) for x in object_ids):
            raise ValueError("Invalid shelf placement")
        ids = list(dict.fromkeys(object_ids))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            documents = [json.loads(row[0]) for row in db.execute("SELECT document FROM objects WHERE deleted=0")]
            by_id = {doc["id"]: doc for doc in documents}
            if any(oid not in by_id for oid in ids):
                raise KeyError("Shelf object no longer exists")
            remaining = sorted([doc for doc in documents if doc["id"] not in ids and doc.get("shelf", "default") == shelf and self.board_for(doc) == board], key=lambda doc: (doc.get("order", 0), doc["created"], doc["id"]))
            offset = max(0, min(int(index), len(remaining)))
            remaining[offset:offset] = [by_id[oid] for oid in ids]
            for order, doc in enumerate(remaining):
                changes = {"shelf": shelf, "board": board, "order": order}
                if all(doc.get(k) == v for k, v in changes.items()):
                    continue
                doc.update(changes)
                doc.update(version=doc["version"] + 1, updated=time.time())
                self._validate(doc)
                raw = json.dumps(doc, ensure_ascii=False)
                db.execute("UPDATE objects SET version=?,updated=?,document=? WHERE id=?", (doc["version"], doc["updated"], raw, doc["id"]))
                db.execute("INSERT INTO versions VALUES(?,?,?,?)", (doc["id"], doc["version"], doc["updated"], raw))
                db.execute("DELETE FROM versions WHERE id=? AND version<?", (doc["id"], max(1, doc["version"] - 49)))
        request_desktop("shelf.refresh")

    def import_file(self, path: str | Path, managed: bool = False, **metadata: Any) -> dict[str, Any]:
        source = Path(path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError("Only regular files can be added to the shelf")
        if managed and source.stat().st_size > self.MAX_FILE_BYTES:
            raise ValueError("File exceeds managed shelf limit; use a reference")
        target = source
        if managed:
            # Unique destination: never overwrite another file with the same name.
            folder = self.assets / uuid.uuid4().hex
            folder.mkdir()
            target = folder / source.name
            try:
                with source.open("rb") as src, target.open("xb") as dest:
                    shutil.copyfileobj(src, dest, 1024 * 1024)
            except BaseException:
                target.unlink(missing_ok=True)
                folder.rmdir()
                raise
        kind = "image" if source.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"} else "file"
        return self.create(kind, title=source.name, path=str(target), managed=managed, **metadata)

    def migrate_overlays(self, panels: list[dict[str, Any]]) -> int:
        """Import legacy notes once, leaving their source intact for compatibility."""
        count = 0
        for panel in panels:
            if panel.get("kind") not in {"text", "checklist", "note"}:
                continue
            identity = "overlay:" + str(panel.get("name", panel.get("id", panel.get("title", ""))))
            if not identity.removeprefix("overlay:"):
                continue
            with self._db() as db:
                exists = db.execute("SELECT 1 FROM metadata WHERE key=?", (identity,)).fetchone()
            if exists:
                continue
            item = self.create("checklist" if panel.get("kind") == "checklist" else "note", title=str(panel.get("title", panel.get("name", ""))), content=str(panel.get("text", "")), items=panel.get("items", []))
            with self._db() as db:
                db.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)", (identity, item["id"]))
            count += 1
        return count
