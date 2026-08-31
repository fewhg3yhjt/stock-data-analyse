"""Version, quality and publish state for staged datasets."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd


def _now():
    return datetime.now().isoformat(timespec="seconds")


class PipelineState:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS dataset_versions (
              version_id TEXT PRIMARY KEY, dataset_name TEXT NOT NULL, partition_key TEXT NOT NULL,
              candidate_path TEXT, published_path TEXT, previous_version_id TEXT, input_versions TEXT,
              source_batches TEXT NOT NULL, row_count INTEGER NOT NULL, symbol_count INTEGER NOT NULL,
              min_date TEXT, max_date TEXT, schema_version TEXT NOT NULL, checksum TEXT NOT NULL,
              generated_by_job INTEGER, builder_version TEXT NOT NULL, quality_status TEXT,
              publish_status TEXT NOT NULL, created_at TEXT NOT NULL, validated_at TEXT,
              published_at TEXT, error_summary TEXT
            );
            CREATE TABLE IF NOT EXISTS dataset_current (
              dataset_name TEXT NOT NULL, partition_key TEXT NOT NULL, version_id TEXT NOT NULL,
              published_at TEXT NOT NULL, PRIMARY KEY(dataset_name, partition_key)
            );
            CREATE TABLE IF NOT EXISTS dataset_quality_results (
              quality_id TEXT PRIMARY KEY, dataset_name TEXT NOT NULL, partition_key TEXT NOT NULL,
              version_id TEXT NOT NULL, status TEXT NOT NULL, checks TEXT NOT NULL,
              affected_symbols TEXT, publish_allowed INTEGER NOT NULL, checked_at TEXT NOT NULL,
              checker_version TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dataset_publish_locks (
              dataset_name TEXT NOT NULL, partition_key TEXT NOT NULL,
              lock_key TEXT NOT NULL, acquired_at TEXT NOT NULL,
              PRIMARY KEY(dataset_name, partition_key)
            );
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(dataset_versions)")}
            if "rollback_path" not in columns:
                conn.execute("ALTER TABLE dataset_versions ADD COLUMN rollback_path TEXT")

    def create_version(self, build: dict, *, source_batches: list[str],
                       builder_version: str = "daily_builder.v1",
                       dataset_name: str = "stock_daily",
                       schema_version: str = "stock_daily.v1",
                       input_versions: dict | None = None,
                       publish_status: str = "candidate") -> str:
        version = build["version_id"]
        with sqlite3.connect(self.db_path) as conn:
            existing = conn.execute("SELECT version_id FROM dataset_versions WHERE version_id=?", (version,)).fetchone()
            if existing:
                return version
            conn.execute("""INSERT INTO dataset_versions
              (version_id,dataset_name,partition_key,candidate_path,source_batches,row_count,symbol_count,
               min_date,max_date,schema_version,checksum,builder_version,publish_status,created_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (version, dataset_name, build["partition"],
              str(build["path"]), json.dumps(source_batches), build["row_count"], build["symbol_count"],
              build.get("min_date"), build.get("max_date"), schema_version, build["checksum"],
              builder_version, publish_status, _now()))
            if input_versions:
                conn.execute("UPDATE dataset_versions SET input_versions=? WHERE version_id=?",
                             (json.dumps(input_versions, ensure_ascii=False), version))
        return version

    def record_output_versions(self, *, dataset_name: str, paths: dict[str, Path],
                               input_dataset: str, input_versions: dict,
                               builder_version: str, schema_version: str) -> dict[str, str]:
        """Record already-written derived partitions as candidates.

        派生数据集（indicators 等）不再自动登记 PASS/published —— 避免"输入版本自证"。
        仅登记 candidate 版本（quality_status 为空、publish_status='candidate'），
        由调用方显式执行质量检查（PipelineState.quality）与发布（publish_current）。
        """
        import hashlib
        versions = {}
        with sqlite3.connect(self.db_path) as conn:
            for partition, path in paths.items():
                if not path.exists():
                    continue
                output_checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                input_fingerprint = hashlib.sha256(
                    json.dumps(input_versions, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()
                version_id = f"{dataset_name}_{partition.replace('-', '')}_{output_checksum[:10]}_{input_fingerprint[:10]}"
                checksum = output_checksum
                previous = conn.execute(
                    "SELECT version_id FROM dataset_current WHERE dataset_name=? AND partition_key=?",
                    (dataset_name, partition),
                ).fetchone()
                frame = pd.read_parquet(path, columns=["date", "code"])
                min_date = str(pd.to_datetime(frame["date"]).min())[:10] if not frame.empty else None
                max_date = str(pd.to_datetime(frame["date"]).max())[:10] if not frame.empty else None
                conn.execute("""INSERT OR IGNORE INTO dataset_versions
                    (version_id,dataset_name,partition_key,candidate_path,published_path,previous_version_id,
                     input_versions,source_batches,row_count,symbol_count,min_date,max_date,schema_version,
                     checksum,builder_version,quality_status,publish_status,created_at,published_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (version_id, dataset_name, partition, str(path), None, previous[0] if previous else None,
                     json.dumps({input_dataset: input_versions}, ensure_ascii=False), "[]", len(frame),
                     int(frame["code"].nunique()) if "code" in frame else 0, min_date, max_date,
                     schema_version, checksum, builder_version, None, "candidate", _now(), None))
                versions[partition] = version_id
        return versions

    def record_file_versions(self, *, dataset_name: str, files: dict[str, Path],
                             source_batches: list[str], quality: dict,
                             builder_version: str = "source_capture.v1",
                             schema_version: str = "source.v1") -> dict[str, str]:
        """Register already-written auxiliary files as published versions.

        The operation is idempotent by checksum and keeps the current pointer
        on the newest file version without rewriting the file itself.
        """
        import hashlib
        versions = {}
        with sqlite3.connect(self.db_path) as conn:
            for partition, path in files.items():
                path = Path(path)
                if not path.exists():
                    continue
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                version_id = f"{dataset_name}_{partition.replace('-', '').replace('/', '_')}_{checksum[:12]}"
                frame = None
                try:
                    frame = pd.read_parquet(path)
                except Exception:
                    pass
                row_count = len(frame) if frame is not None else 0
                symbol_count = int(frame["code"].nunique()) if frame is not None and "code" in frame else 0
                min_date = max_date = None
                if frame is not None:
                    date_col = "date" if "date" in frame else "stat_date" if "stat_date" in frame else None
                    if date_col and len(frame):
                        values = pd.to_datetime(frame[date_col], errors="coerce")
                        min_date, max_date = str(values.min())[:10], str(values.max())[:10]
                conn.execute("""INSERT OR IGNORE INTO dataset_versions
                    (version_id,dataset_name,partition_key,candidate_path,published_path,source_batches,
                     row_count,symbol_count,min_date,max_date,schema_version,checksum,builder_version,
                     quality_status,publish_status,created_at,published_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (version_id, dataset_name, partition, str(path), str(path), json.dumps(source_batches),
                     row_count, symbol_count, min_date, max_date, schema_version, checksum,
                     builder_version, quality.get("status", "PASS"), "published", _now(), _now()))
                conn.execute("""INSERT INTO dataset_current(dataset_name,partition_key,version_id,published_at)
                    VALUES(?,?,?,?) ON CONFLICT(dataset_name,partition_key) DO UPDATE SET
                    version_id=excluded.version_id,published_at=excluded.published_at""",
                    (dataset_name, partition, version_id, _now()))
                conn.execute("""INSERT OR IGNORE INTO dataset_quality_results
                    (quality_id,dataset_name,partition_key,version_id,status,checks,affected_symbols,
                     publish_allowed,checked_at,checker_version)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (f"quality_{version_id}", dataset_name, partition, version_id,
                     quality.get("status", "PASS"), json.dumps(quality, ensure_ascii=False),
                     json.dumps([], ensure_ascii=False), int(quality.get("publish_allowed", True)),
                     _now(), "source_capture.v1"))
                versions[partition] = version_id
        return versions

    def update_rollback_path(self, version_id: str, path: Path) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE dataset_versions SET rollback_path=? WHERE version_id=?", (str(path), version_id))

    def quality(self, version_id: str, *, status: str, checks: dict,
                publish_allowed: bool, affected_symbols: list[str] | None = None) -> str:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT dataset_name,partition_key FROM dataset_versions WHERE version_id=?", (version_id,)).fetchone()
            if not row:
                raise ValueError("版本不存在")
            quality_id = f"quality_{uuid4().hex[:12]}"
            conn.execute("INSERT INTO dataset_quality_results VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (quality_id, row[0], row[1], version_id, status, json.dumps(checks),
                          json.dumps(affected_symbols or []),
                          int(publish_allowed), _now(), "quality.v1"))
            conn.execute("UPDATE dataset_versions SET quality_status=?,validated_at=? WHERE version_id=?",
                         (status, _now(), version_id))
        return quality_id

    def current(self, dataset_name: str, partition: str):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT version_id FROM dataset_current WHERE dataset_name=? AND partition_key=?",
                                (dataset_name, partition)).fetchone()


def recover_inflight_publishing(db_path: Path | str | None = None) -> int:
    """启动时回收遗留 publishing 版本（进程重启时发布中断）。

    按文件内容恢复（工作项 3）：
    - 正式文件 checksum 匹配新版本 → 补齐 dataset_current；
    - 正式文件不存在或 checksum 不匹配新版本 → 标记 publish_failed，
      不自动回退或选择其他版本（转人工）。
    """
    import hashlib
    if db_path is None:
        from StockInvestmentTool.config import Config
        db_path = Config.DATA_DIR / "management.db"
    recovered = 0
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT version_id,dataset_name,partition_key,published_path,checksum,previous_version_id "
            "FROM dataset_versions WHERE publish_status='publishing'").fetchall()
        for version_id, dataset_name, partition_key, published_path, checksum, previous_version_id in rows:
            path = Path(published_path) if published_path else None
            if path and path.exists() and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == checksum:
                now = datetime.now().isoformat(timespec="seconds")
                conn.execute("UPDATE dataset_versions SET publish_status='published',published_at=? WHERE version_id=?",
                             (now, version_id))
                conn.execute("""INSERT INTO dataset_current (dataset_name,partition_key,version_id,published_at)
                    VALUES (?,?,?,?) ON CONFLICT(dataset_name,partition_key)
                    DO UPDATE SET version_id=excluded.version_id,published_at=excluded.published_at""",
                             (dataset_name, partition_key, version_id, now))
                recovered += 1
            else:
                conn.execute("UPDATE dataset_versions SET publish_status='publish_failed' WHERE version_id=?",
                             (version_id,))
                recovered += 1
    return recovered
