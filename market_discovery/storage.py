"""Small durable ledger for local discovery runs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


class DiscoveryRunStore:
    def __init__(self, path: Path | str | None = None):
        if path is None:
            from StockInvestmentTool.config import Config
            path = Config.DATA_DIR / "discovery_runs.db"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS discovery_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                as_of TEXT,
                conditions TEXT NOT NULL,
                result_count INTEGER NOT NULL,
                data_source TEXT NOT NULL
            )""")

    def save(self, *, as_of: str | None, conditions: dict, result_count: int) -> int:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "INSERT INTO discovery_runs(created_at,as_of,conditions,result_count,data_source) VALUES(?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), as_of,
                 json.dumps(conditions, ensure_ascii=False), int(result_count), "warehouse.daily"),
            )
            return int(cur.lastrowid)

    def recent(self, limit: int = 20) -> list[dict]:
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM discovery_runs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["conditions"] = json.loads(item["conditions"])
            result.append(item)
        return result
