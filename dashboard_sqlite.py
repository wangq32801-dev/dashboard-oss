"""SQLite shadow store for the dashboard.

This module is deliberately side-effect free until ``migrate_snapshot`` is
called.  It provides a stable target for the eventual cut-over while the
existing JSON/Markdown/TickTick paths remain authoritative.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
  source TEXT NOT NULL, source_revision TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_kind ON entities(kind);
CREATE TABLE IF NOT EXISTS migration_conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT, reason TEXT NOT NULL,
  payload TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

def open_db(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    try:
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        return db
    except Exception:
        db.close()
        raise

@contextmanager
def managed_db(path: str | Path):
    """Open a shadow database and always close it after the transaction."""
    db = open_db(path)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def migrate_snapshot(db: sqlite3.Connection, items: Iterable[dict[str, Any]], *, revision: str) -> dict[str, int]:
    """Upsert a normalized snapshot; duplicate IDs are recorded, never lost."""
    counts = {"inserted": 0, "updated": 0, "conflicts": 0}
    for item in items:
        entity_id = str(item["id"])
        payload = json.dumps(item.get("payload", item), ensure_ascii=False, sort_keys=True)
        source = str(item.get("source", "unknown"))
        kind = str(item.get("kind", "unknown"))
        now = str(item.get("updated_at", revision))
        row = db.execute("SELECT payload, source FROM entities WHERE id=?", (entity_id,)).fetchone()
        if row and row["payload"] != payload and row["source"] != source:
            db.execute("INSERT INTO migration_conflicts(entity_id,reason,payload,created_at) VALUES(?,?,?,?)",
                       (entity_id, "same id from different sources", payload, now))
            counts["conflicts"] += 1
            continue
        if row:
            db.execute("UPDATE entities SET kind=?,payload=?,source=?,source_revision=?,updated_at=? WHERE id=?",
                       (kind, payload, source, revision, now, entity_id))
            counts["updated"] += 1
        else:
            db.execute("INSERT INTO entities VALUES(?,?,?,?,?,?)",
                       (entity_id, kind, payload, source, revision, now))
            counts["inserted"] += 1
    db.execute("INSERT INTO meta(key,value) VALUES('last_revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (revision,))
    db.commit()
    return counts

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="建立驾驶舱 SQLite 影子快照")
    p.add_argument("snapshot", help="JSON 数组快照文件")
    p.add_argument("--db", default="dashboard-shadow.sqlite3")
    args = p.parse_args()
    data = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("snapshot must be a JSON array")
    with managed_db(args.db) as conn:
        print(migrate_snapshot(conn, data, revision=str(Path(args.snapshot).stat().st_mtime_ns)))
