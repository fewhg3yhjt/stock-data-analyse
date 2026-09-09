"""Security universe snapshots with an explicit authoritative fallback policy."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


ACTIVE_STATUSES = {"1", "active", "trading", "正常", "交易"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class UniverseStore:
    """Persist daily universe observations and resolve safe fallback lists."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """CREATE TABLE IF NOT EXISTS universe_snapshots (
                    snapshot_date TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    authoritative INTEGER NOT NULL DEFAULT 0,
                    complete INTEGER NOT NULL DEFAULT 0,
                    entity_count INTEGER NOT NULL DEFAULT 0,
                    fetched_at TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS universe_snapshot_items (
                    snapshot_date TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    trade_status TEXT NOT NULL DEFAULT '',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(snapshot_date, entity_id, entity_type)
                );
                CREATE INDEX IF NOT EXISTS idx_universe_items_lookup
                    ON universe_snapshot_items(snapshot_date, entity_type, is_active, entity_id);
                """
            )

    def record_snapshot(self, snapshot_date: str, items: list[dict], *, source: str,
                        authoritative: bool, complete: bool, error_message: str = "",
                        metadata: dict | None = None) -> dict:
        now = _now()
        normalized = []
        for item in items:
            code = str(item.get("code") or item.get("entity_id") or "").strip().lower().replace(".", "")
            entity_type = str(item.get("type") or item.get("entity_type") or "").strip().lower()
            if not code or entity_type not in {"stock", "etf", "index", "industry"}:
                continue
            trade_status = str(item.get("tradeStatus") or item.get("trade_status") or "")
            normalized.append({
                "entity_id": code, "entity_type": entity_type,
                "name": str(item.get("name") or ""),
                "trade_status": trade_status,
                "is_active": int(str(trade_status).lower() in ACTIVE_STATUSES or not trade_status),
            })
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO universe_snapshots
                   (snapshot_date,source,authoritative,complete,entity_count,fetched_at,error_message,metadata)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(snapshot_date) DO UPDATE SET source=excluded.source,
                     authoritative=excluded.authoritative,complete=excluded.complete,
                     entity_count=excluded.entity_count,fetched_at=excluded.fetched_at,
                     error_message=excluded.error_message,metadata=excluded.metadata""",
                (snapshot_date, source, int(authoritative), int(complete), len(normalized),
                 now, error_message, json.dumps(metadata or {}, ensure_ascii=False)),
            )
            conn.execute("DELETE FROM universe_snapshot_items WHERE snapshot_date=?", (snapshot_date,))
            conn.executemany(
                """INSERT INTO universe_snapshot_items
                   (snapshot_date,entity_id,entity_type,name,trade_status,is_active,source,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                [(snapshot_date, item["entity_id"], item["entity_type"], item["name"],
                  item["trade_status"], item["is_active"], source, now) for item in normalized],
            )
        return {"snapshot_date": snapshot_date, "source": source,
                "authoritative": bool(authoritative), "complete": bool(complete),
                "entity_count": len(normalized), "error_message": error_message,
                "items": normalized}

    def latest_snapshot(self, *, as_of: str, entity_types: set[str] | None = None) -> dict | None:
        with self._connect() as conn:
            snapshot = conn.execute(
                "SELECT * FROM universe_snapshots WHERE snapshot_date<=? ORDER BY snapshot_date DESC LIMIT 1",
                (as_of,),
            ).fetchone()
            if snapshot is None:
                return None
            params: list[object] = [snapshot["snapshot_date"]]
            query = "SELECT * FROM universe_snapshot_items WHERE snapshot_date=? AND is_active=1"
            if entity_types:
                query += " AND entity_type IN (" + ",".join("?" for _ in entity_types) + ")"
                params.extend(sorted(entity_types))
            items = [dict(row) for row in conn.execute(query, params).fetchall()]
        result = dict(snapshot)
        result["metadata"] = json.loads(result.get("metadata") or "{}")
        result["items"] = items
        return result

    def resolve(self, *, snapshot_date: str, fetch_full, entity_types: set[str],
                fallback_to_catalog: list[dict] | None = None) -> dict:
        """Use a full source when available; otherwise use history without retiring entities."""
        error = ""
        try:
            items = list(fetch_full(snapshot_date) or [])
            selected = [item for item in items if str(item.get("type") or "").lower() in entity_types]
            if selected:
                return self.record_snapshot(snapshot_date, selected, source="authoritative",
                                             authoritative=True, complete=True,
                                             metadata={"requested_entity_types": sorted(entity_types)})
            error = "全量 Universe 返回空"
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        historical = self.latest_snapshot(as_of=snapshot_date, entity_types=entity_types)
        if historical and historical.get("items"):
            return {"snapshot_date": snapshot_date, "source": "historical_snapshot",
                    "authoritative": False, "complete": False,
                    "entity_count": len(historical["items"]), "error_message": error,
                    "items": historical["items"]}
        catalog = [item for item in (fallback_to_catalog or [])
                   if str(item.get("type") or "").lower() in entity_types]
        return self.record_snapshot(snapshot_date, catalog, source="instrument_catalog",
                                    authoritative=False, complete=False,
                                    error_message=error,
                                    metadata={"fallback": "catalog"})

    def active_codes(self, *, snapshot_date: str, entity_types: set[str]) -> list[str]:
        snapshot = self.latest_snapshot(as_of=snapshot_date, entity_types=entity_types)
        return [str(item["entity_id"]) for item in (snapshot or {}).get("items", [])]
