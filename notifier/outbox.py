"""Durable notification outbox for retryable Digest delivery."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


class NotificationOutbox:
    """Small SQLite-backed queue; independent from the portfolio database."""

    def __init__(self, db_path: Optional[Path | str] = None):
        if db_path is None:
            from StockInvestmentTool.config import Config
            db_path = Config.DATA_DIR / "notification_outbox.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                sent_at TEXT
            )""")

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def enqueue(self, channel: str, payload: dict) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO notification_outbox(channel,payload,next_attempt_at,created_at) VALUES(?,?,?,?)",
                (channel, json.dumps(payload, ensure_ascii=False), now, now),
            )
            return int(cur.lastrowid)

    def due(self, limit: int = 20) -> list[dict]:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notification_outbox WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT ?",
                (now, int(limit)),
            ).fetchall()
        return [dict(row, payload=json.loads(row["payload"])) for row in rows]

    def mark_sent(self, item_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE notification_outbox SET status='sent', sent_at=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), item_id),
            )

    def mark_failed(self, item_id: int, attempts: int, error: str) -> None:
        next_attempt = int(attempts) + 1
        status = "dead" if next_attempt >= 5 else "pending"
        delay = min(60 * (2 ** min(int(attempts), 6)), 3600)
        next_at = datetime.now() + timedelta(seconds=delay)
        with self._connect() as conn:
            conn.execute(
                """UPDATE notification_outbox
                   SET status=?, attempts=?, last_error=?, next_attempt_at=?
                   WHERE id=?""",
                (status, next_attempt, str(error)[:1000], next_at.isoformat(timespec="seconds"), item_id),
            )

    def pending_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM notification_outbox WHERE status='pending'").fetchone()[0])

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM notification_outbox GROUP BY status"
            ).fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    def recent(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,channel,status,attempts,last_error,created_at,sent_at "
                "FROM notification_outbox ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(row) for row in rows]
