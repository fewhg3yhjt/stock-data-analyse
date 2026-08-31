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
                sent_at TEXT,
                claimed_by TEXT,
                claimed_at TEXT,
                lease_expires_at TEXT
            )""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(notification_outbox)")}
            for column in ("claimed_by", "claimed_at", "lease_expires_at"):
                if column not in columns:
                    try:
                        conn.execute(f"ALTER TABLE notification_outbox ADD COLUMN {column} TEXT")
                    except sqlite3.OperationalError:
                        # 只读/无权限库：跳过该列迁移，租约功能对缺列降级
                        continue

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

    def claim_due(self, limit: int = 20, worker_id: str = "default",
                  lease_seconds: int = 300) -> list[dict]:
        """原子领取到期任务（工作项 1/2）。

        单事务内把 pending 且到期、且租约空闲/过期的任务置为 processing，
        避免多个 Worker 重复领取。领取者通过 claim_due/mark_* 持有租约。
        """
        now = datetime.now().isoformat(timespec="seconds")
        expire_at = (datetime.now() + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM notification_outbox "
                "WHERE ((status='pending' AND next_attempt_at<=?) "
                "OR (status='processing' AND lease_expires_at<=?)) "
                "AND (lease_expires_at IS NULL OR lease_expires_at<=?) "
                "ORDER BY id LIMIT ?",
                (now, now, now, int(limit)),
            ).fetchall()
            if not rows:
                return []
            ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE notification_outbox SET status='processing', claimed_by=?, "
                f"claimed_at=?, lease_expires_at=? WHERE id IN ({placeholders})",
                (worker_id, now, expire_at, *ids),
            )
        result = []
        for item_id in ids:
            item = self.get(item_id)
            if item:
                result.append(item)
        return result

    def renew_lease(self, item_id: int, worker_id: str = "default",
                    lease_seconds: int = 300) -> bool:
        """心跳续租（工作项 1）：仅当前 worker 可续。"""
        expire_at = (datetime.now() + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE notification_outbox SET claimed_at=?, lease_expires_at=? "
                "WHERE id=? AND claimed_by=? AND status='processing'",
                (datetime.now().isoformat(timespec="seconds"), expire_at, int(item_id), worker_id),
            )
        return cur.rowcount > 0

    def release_lease(self, item_id: int, worker_id: str = "default") -> bool:
        """主动释放租约（回到 pending，保留 attempts）。"""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE notification_outbox SET status='pending', claimed_by=NULL, "
                "claimed_at=NULL, lease_expires_at=NULL WHERE id=? AND claimed_by=?",
                (int(item_id), worker_id),
            )
        return cur.rowcount > 0

    def mark_sent(self, item_id: int, worker_id: str | None = None) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        if worker_id is None:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE notification_outbox SET status='sent', sent_at=?, claimed_by=NULL, "
                    "claimed_at=NULL, lease_expires_at=NULL WHERE id=?",
                    (now, int(item_id)))
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE notification_outbox SET status='sent', sent_at=?, claimed_by=NULL, "
                "claimed_at=NULL, lease_expires_at=NULL WHERE id=? AND claimed_by=?",
                (now, int(item_id), worker_id),
            )

    def mark_failed(self, item_id: int, attempts: int, error: str,
                    worker_id: str | None = None) -> None:
        next_attempt = int(attempts) + 1
        status = "dead" if next_attempt >= 5 else "pending"
        delay = min(60 * (2 ** min(int(attempts), 6)), 3600)
        next_at = datetime.now() + timedelta(seconds=delay)
        where = "WHERE id=?"
        params = [status, next_attempt, str(error)[:1000],
                  next_at.isoformat(timespec="seconds"), int(item_id)]
        if worker_id is not None:
            where = "WHERE id=? AND claimed_by=?"
            params.append(worker_id)
        with self._connect() as conn:
            conn.execute(
                f"""UPDATE notification_outbox
                   SET status=?, attempts=?, last_error=?, next_attempt_at=?,
                       claimed_by=NULL, claimed_at=NULL, lease_expires_at=NULL
                   {where}""",
                tuple(params),
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
                "SELECT id,channel,payload,status,attempts,last_error,created_at,sent_at "
                "FROM notification_outbox ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item["payload"])
            except Exception:
                item["payload"] = {}
            result.append(item)
        return result

    def get(self, item_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM notification_outbox WHERE id=?", (int(item_id),)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def retry(self, item_id: int) -> bool:
        """Make one failed/dead item eligible again without deleting its audit row."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE notification_outbox SET status='pending', next_attempt_at=?, last_error='' "
                "WHERE id=? AND status IN ('failed','dead','pending')",
                (datetime.now().isoformat(timespec="seconds"), int(item_id)),
            )
        return cur.rowcount > 0
