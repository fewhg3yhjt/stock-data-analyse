"""Atomic candidate publication for staged dataset partitions."""

from __future__ import annotations

import os
import shutil
import sqlite3
import hashlib
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4


class PublishLockError(RuntimeError):
    """无法在超时内获取分区发布锁。"""


class Publisher:
    def __init__(self, warehouse, lock_timeout: float = 60.0):
        self.warehouse = warehouse
        self.lock_timeout = lock_timeout

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("""CREATE TABLE IF NOT EXISTS dataset_publish_locks (
            dataset_name TEXT NOT NULL, partition_key TEXT NOT NULL,
            lock_key TEXT NOT NULL, acquired_at TEXT NOT NULL,
            PRIMARY KEY(dataset_name, partition_key))""")

    def _acquire_partition_lock(self, dataset_name: str, partition: str,
                                *, retry_wait: float = 0.2) -> str:
        """按 dataset_name + partition_key 获取分区锁（工作项 7）。

        BEGIN IMMEDIATE 保证并发写互斥；锁行即临界区持有凭据。
        """
        lock_key = uuid4().hex
        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                conn = sqlite3.connect(self.warehouse.meta_db_path, timeout=self.lock_timeout)
                conn.execute("BEGIN IMMEDIATE")
                self._ensure_schema(conn)
                conn.execute(
                    "INSERT INTO dataset_publish_locks (dataset_name,partition_key,lock_key,acquired_at) VALUES (?,?,?,?)",
                    (dataset_name, partition, lock_key, datetime.now().isoformat(timespec="seconds")))
                conn.commit()
                conn.close()
                return lock_key
            except sqlite3.OperationalError:
                conn.close()
                if time.monotonic() >= deadline:
                    raise PublishLockError(
                        f"获取发布分区锁超时: {dataset_name}/{partition}") from None
                time.sleep(retry_wait)

    def _release_partition_lock(self, dataset_name: str, partition: str, lock_key: str) -> None:
        with sqlite3.connect(self.warehouse.meta_db_path) as conn:
            conn.execute(
                "DELETE FROM dataset_publish_locks WHERE dataset_name=? AND partition_key=? AND lock_key=?",
                (dataset_name, partition, lock_key))

    def publish(self, version_id: str) -> dict:
        # 用 SQLite 写事务串行化发布，不再创建/依赖持久化发布锁。
        # 文件仍通过临时文件 + os.replace 原子切换，避免业务读到半文件。
        conn = sqlite3.connect(self.warehouse.meta_db_path, timeout=self.lock_timeout)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT dataset_name,partition_key,candidate_path,quality_status,publish_status FROM dataset_versions WHERE version_id=?", (version_id,)).fetchone()
            if not row or row[3] not in ("PASS", "WARNING"):
                raise ValueError("版本不存在或质量不允许发布")
            result = self._publish_locked(conn, version_id, row, "")
            conn.commit()
            if row[0] == "stock_daily":
                # Public JSON is a derived, static representation of formal
                # data. Export only after the database current pointer commits.
                from StockInvestmentTool.warehouse.public_export import export_public_daily
                result["public_exports"] = export_public_daily(self.warehouse)
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _publish_locked(self, conn, version_id: str, row, lock_key: str) -> dict:
        candidate = Path(row[2] or "")
        if not candidate.exists():
            raise ValueError("候选文件不存在")
        current = conn.execute("SELECT version_id FROM dataset_current WHERE dataset_name=? AND partition_key=?", (row[0], row[1])).fetchone()
        if row[4] == "published" and current and current[0] == version_id:
            return {"version_id": version_id, "path": str(self.warehouse.daily_partition(row[1]) if row[0] == "stock_daily" else Path(row[2])), "already_published": True}
        version_row = conn.execute("SELECT checksum FROM dataset_versions WHERE version_id=?", (version_id,)).fetchone()
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != version_row[0]:
            raise ValueError("候选文件 checksum 不匹配")
        target = self.warehouse.daily_partition(row[1])
        if row[0] != "stock_daily":
            from StockInvestmentTool.warehouse.dataset_config import load_dataset_config
            definition = load_dataset_config(row[0])["dataset"]["partition"]["path"]
            relative = definition.removeprefix("warehouse/")
            target = self.warehouse.base_dir / relative.replace("{partition}", row[1]).replace("{run_date}", row[1]).replace("{code}", row[1])
        target.parent.mkdir(parents=True, exist_ok=True)
        rollback_path = None
        if current:
            previous = conn.execute("SELECT published_path FROM dataset_versions WHERE version_id=?", (current[0],)).fetchone()
            if previous and previous[0] and Path(previous[0]).exists():
                rollback_path = self.warehouse.base_dir / "rollback" / row[0] / row[1] / f"{current[0]}.parquet"
                rollback_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(previous[0], rollback_path)
        conn.execute("UPDATE dataset_versions SET publish_status='publishing' WHERE version_id=?", (version_id,))
        temp = target.with_name(f".{target.name}.{version_id}.tmp")
        try:
            shutil.copyfile(row[2], temp)
            os.replace(temp, target)
        except Exception:
            temp.unlink(missing_ok=True)
            conn.execute("UPDATE dataset_versions SET publish_status='publish_failed' WHERE version_id=?", (version_id,))
            raise
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("UPDATE dataset_versions SET published_path=?,previous_version_id=?,rollback_path=?,publish_status='published',published_at=? WHERE version_id=?",
                     (str(target), current[0] if current else None, str(rollback_path) if rollback_path else None, now, version_id))
        conn.execute("INSERT INTO dataset_current (dataset_name,partition_key,version_id,published_at) VALUES (?,?,?,?) ON CONFLICT(dataset_name,partition_key) DO UPDATE SET version_id=excluded.version_id,published_at=excluded.published_at",
                     (row[0], row[1], version_id, now))
        if current and current[0] != version_id:
            conn.execute("UPDATE dataset_versions SET publish_status='superseded' WHERE version_id=?", (current[0],))
        return {"version_id": version_id, "path": str(target), "previous_version_id": current[0] if current else None}

    def rollback(self, dataset_name: str, partition: str) -> dict:
        with sqlite3.connect(self.warehouse.meta_db_path) as conn:
            current = conn.execute("SELECT version_id FROM dataset_current WHERE dataset_name=? AND partition_key=?", (dataset_name, partition)).fetchone()
            if not current:
                raise ValueError("没有当前正式版本")
            previous = conn.execute("SELECT previous_version_id FROM dataset_versions WHERE version_id=?", (current[0],)).fetchone()
        if not previous or not previous[0]:
            raise ValueError("没有可回滚的上一正式版本")
        with sqlite3.connect(self.warehouse.meta_db_path) as conn:
            conn.execute("UPDATE dataset_versions SET quality_status='PASS',publish_status='validated' WHERE version_id=?", (previous[0],))
        return self.publish(previous[0])

    def recovery_check(self) -> list[str]:
        """Return publish consistency errors without changing or deleting data."""
        errors = []
        with sqlite3.connect(self.warehouse.meta_db_path) as conn:
            rows = conn.execute("SELECT version_id,published_path,checksum,publish_status FROM dataset_versions WHERE publish_status IN ('published','publishing')").fetchall()
        for version_id, path, checksum, status in rows:
            if status == "publishing":
                errors.append(f"未完成发布: {version_id}")
                continue
            if not path or not Path(path).exists():
                errors.append(f"正式文件不存在: {version_id}")
            elif hashlib.sha256(Path(path).read_bytes()).hexdigest() != checksum:
                errors.append(f"正式文件 checksum 不匹配: {version_id}")
        return errors
