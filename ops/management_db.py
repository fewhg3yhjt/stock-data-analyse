"""Unified management database for task and data-center runtime state.

Definitions still come from YAML. This database stores their runtime projection,
execution facts, metric health, artifacts, and lineage in one queryable place.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable


MANAGEMENT_SCHEMA_VERSION = "management.v1"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ManagementDB:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS management_meta (
              key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
              migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL,
              checksum_before TEXT, checksum_after TEXT
            );
            CREATE TABLE IF NOT EXISTS job_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, job_name TEXT NOT NULL,
              started_at TEXT NOT NULL, finished_at TEXT,
              status TEXT NOT NULL DEFAULT 'running', result TEXT NOT NULL DEFAULT '{}',
              error TEXT NOT NULL DEFAULT '', run_date TEXT, display_name TEXT NOT NULL DEFAULT '',
              scheduled_at TEXT, phase TEXT NOT NULL DEFAULT '', progress INTEGER NOT NULL DEFAULT 0,
              processed INTEGER, total INTEGER, current_item TEXT NOT NULL DEFAULT '',
              input_dataset TEXT NOT NULL DEFAULT '', output_dataset TEXT NOT NULL DEFAULT '',
              parent_run_id INTEGER, updated_at TEXT, request_id TEXT,
              config_version INTEGER, trigger_type TEXT NOT NULL DEFAULT 'scheduled',
              period_start TEXT, period_end TEXT, timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
              record_origin TEXT NOT NULL DEFAULT 'new'
            );
            CREATE TABLE IF NOT EXISTS job_plan (
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT NOT NULL,
              task_key TEXT NOT NULL, display_name TEXT NOT NULL, scheduled_at TEXT,
              status TEXT NOT NULL DEFAULT 'scheduled', phase TEXT NOT NULL DEFAULT '',
              progress INTEGER NOT NULL DEFAULT 0, processed INTEGER, total INTEGER,
              current_item TEXT NOT NULL DEFAULT '', input_dataset TEXT NOT NULL DEFAULT '',
              output_dataset TEXT NOT NULL DEFAULT '', blocked_by TEXT NOT NULL DEFAULT '',
              run_id INTEGER, error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
              UNIQUE(run_date, task_key)
            );
            CREATE TABLE IF NOT EXISTS task_definitions (
              task_key TEXT PRIMARY KEY, display_name TEXT NOT NULL, stage TEXT NOT NULL,
              task_type TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
              active_config_version INTEGER, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_config_versions (
              task_key TEXT NOT NULL, version INTEGER NOT NULL, config TEXT NOT NULL,
              checksum TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
              activated_at TEXT, PRIMARY KEY(task_key, version)
            );
            CREATE TABLE IF NOT EXISTS task_execution_requests (
              request_id TEXT PRIMARY KEY, task_key TEXT NOT NULL, trigger_type TEXT NOT NULL,
              period_start TEXT, period_end TEXT, symbols TEXT, config_version INTEGER,
              requested_by TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_run_events (
              event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
              event_time TEXT NOT NULL, level TEXT NOT NULL, phase TEXT NOT NULL,
              event_type TEXT NOT NULL, message TEXT NOT NULL, processed INTEGER,
              total INTEGER, current_item TEXT, payload TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS task_artifacts (
              artifact_id TEXT PRIMARY KEY, run_id INTEGER, dataset_name TEXT,
              artifact_type TEXT NOT NULL, partition_key TEXT, file_path TEXT NOT NULL,
              file_name TEXT NOT NULL, file_format TEXT, row_count INTEGER,
              symbol_count INTEGER, min_date TEXT, max_date TEXT, checksum TEXT,
              size_bytes INTEGER, status TEXT NOT NULL, created_at TEXT NOT NULL,
              record_origin TEXT NOT NULL DEFAULT 'new'
            );
            CREATE TABLE IF NOT EXISTS artifact_lineage (
              upstream_artifact_id TEXT NOT NULL, downstream_artifact_id TEXT NOT NULL,
              relation_type TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(upstream_artifact_id, downstream_artifact_id)
            );
            CREATE TABLE IF NOT EXISTS dataset_registry (
              dataset_name TEXT PRIMARY KEY, display_name TEXT NOT NULL,
              description TEXT NOT NULL, grain TEXT NOT NULL, primary_keys TEXT NOT NULL,
              partition_type TEXT NOT NULL, storage_path TEXT NOT NULL,
              update_frequency TEXT, enabled INTEGER NOT NULL DEFAULT 1,
              schema_version TEXT NOT NULL, config_checksum TEXT NOT NULL DEFAULT '',
              config_path TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dataset_fields (
              dataset_name TEXT NOT NULL, field_name TEXT NOT NULL, display_name TEXT NOT NULL,
              data_type TEXT NOT NULL, unit TEXT, nullable INTEGER NOT NULL DEFAULT 1,
              is_primary_key INTEGER NOT NULL DEFAULT 0, description TEXT,
              schema_version TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              PRIMARY KEY(dataset_name, field_name)
            );
            CREATE TABLE IF NOT EXISTS dataset_sources (
              dataset_name TEXT NOT NULL, source_name TEXT NOT NULL, role TEXT NOT NULL,
              priority INTEGER NOT NULL, field_mapping TEXT NOT NULL, unit_conversions TEXT NOT NULL,
              request_defaults TEXT, enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              PRIMARY KEY(dataset_name, source_name)
            );
            CREATE TABLE IF NOT EXISTS dataset_consumers (
              dataset_name TEXT NOT NULL, consumer_name TEXT NOT NULL, consumer_type TEXT NOT NULL,
              fields_used TEXT, purpose TEXT NOT NULL, required_quality TEXT NOT NULL,
              fallback_policy TEXT NOT NULL, blocked_actions TEXT, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, PRIMARY KEY(dataset_name, consumer_name)
            );
            CREATE TABLE IF NOT EXISTS dataset_partitions (
              dataset_name TEXT NOT NULL, partition_key TEXT NOT NULL, file_path TEXT NOT NULL,
              min_date TEXT, max_date TEXT, row_count INTEGER NOT NULL,
              symbol_count INTEGER NOT NULL, file_size INTEGER, schema_hash TEXT,
              checksum TEXT, current_version_id TEXT, status TEXT NOT NULL, updated_at TEXT NOT NULL,
              PRIMARY KEY(dataset_name, partition_key)
            );
            CREATE TABLE IF NOT EXISTS source_batches (
              batch_id TEXT PRIMARY KEY, dataset_name TEXT NOT NULL, source_name TEXT NOT NULL,
              job_run_id INTEGER, run_date TEXT NOT NULL, trade_date_start TEXT,
              trade_date_end TEXT, universe_id TEXT, expected_symbols INTEGER NOT NULL,
              success_symbols INTEGER NOT NULL DEFAULT 0, failed_symbols INTEGER NOT NULL DEFAULT 0,
              skipped_symbols INTEGER NOT NULL DEFAULT 0, row_count INTEGER NOT NULL DEFAULT 0,
              raw_path TEXT, schema_version TEXT NOT NULL, request_context TEXT,
              checksum TEXT, file_size INTEGER, status TEXT NOT NULL, started_at TEXT NOT NULL,
              finished_at TEXT, error_summary TEXT, failure_details TEXT,
              record_origin TEXT NOT NULL DEFAULT 'new'
            );
            CREATE TABLE IF NOT EXISTS dataset_versions (
              version_id TEXT PRIMARY KEY, dataset_name TEXT NOT NULL, partition_key TEXT NOT NULL,
              candidate_path TEXT, published_path TEXT, previous_version_id TEXT,
              input_versions TEXT, source_batches TEXT NOT NULL, row_count INTEGER NOT NULL,
              symbol_count INTEGER NOT NULL, min_date TEXT, max_date TEXT,
              schema_version TEXT NOT NULL, checksum TEXT NOT NULL, generated_by_job INTEGER,
              builder_version TEXT NOT NULL, quality_status TEXT, publish_status TEXT NOT NULL,
              created_at TEXT NOT NULL, validated_at TEXT, published_at TEXT,
              error_summary TEXT, rollback_path TEXT, record_origin TEXT NOT NULL DEFAULT 'new'
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
            CREATE TABLE IF NOT EXISTS metric_definitions (
              metric_key TEXT PRIMARY KEY, display_name TEXT NOT NULL, category TEXT NOT NULL,
              definition TEXT NOT NULL, unit TEXT, producer_task TEXT NOT NULL,
              builtin INTEGER NOT NULL, editable INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS metric_versions (
              metric_key TEXT NOT NULL, version INTEGER NOT NULL, definition TEXT NOT NULL,
              checksum TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
              activated_at TEXT, PRIMARY KEY(metric_key, version)
            );
            CREATE TABLE IF NOT EXISTS metric_health (
              metric_key TEXT PRIMARY KEY, metric_version INTEGER, latest_period TEXT,
              expected_period TEXT, covered_objects INTEGER, expected_objects INTEGER,
              coverage REAL, last_success_at TEXT, last_run_id INTEGER, status TEXT NOT NULL,
              message TEXT NOT NULL DEFAULT '', coverage_by_type TEXT NOT NULL DEFAULT '{}',
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_metric_links (
              task_key TEXT NOT NULL, metric_key TEXT NOT NULL, relation_type TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(task_key, metric_key)
            );
            CREATE TABLE IF NOT EXISTS web_analysis_tasks (
              task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, code TEXT NOT NULL,
              status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', progress INTEGER NOT NULL DEFAULT 0,
              result_json TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT
            );
            """)
            conn.execute("INSERT OR REPLACE INTO management_meta(key,value,updated_at) VALUES(?,?,?)",
                         ("schema_version", MANAGEMENT_SCHEMA_VERSION, now()))
            conn.execute("INSERT OR REPLACE INTO management_meta(key,value,updated_at) VALUES(?,?,?)",
                         ("initialized_at", now(), now()))
            self._record_migrations(conn)

    @staticmethod
    def _quick_check(conn) -> str:
        row = conn.execute("PRAGMA quick_check").fetchone()
        return str(row[0] if row else "unknown")

    def _record_migrations(self, conn) -> None:
        """登记已执行的正式迁移（每个只执行一次，前后 quick_check）。"""
        migrations = [
            ("v1_management_initial", "统一管理库初始 Schema"),
            ("v2_job_runs_legacy_import", "旧 job_runs.db 只读迁移"),
            ("v3_web_analysis_tasks", "Web 分析任务持久化"),
        ]
        applied = {row[0] for row in conn.execute("SELECT migration_id FROM schema_migrations")}
        for migration_id, _desc in migrations:
            if migration_id in applied:
                continue
            before = self._quick_check(conn)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(migration_id,applied_at) VALUES(?,?)",
                         (migration_id, now()))
            after = self._quick_check(conn)
            conn.execute("UPDATE schema_migrations SET checksum_before=?, checksum_after=? WHERE migration_id=?",
                         (before, after, migration_id))

    def table_exists(self, table: str) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def copy_legacy_db(self, source: Path | str, *, kind: str) -> dict:
        """Copy legacy rows into this DB without changing the source database."""
        source = Path(source)
        if not source.exists():
            return {"source": str(source), "kind": kind, "tables": {}, "skipped": True}
        counts = {}
        with sqlite3.connect(source) as old, self.connect() as new:
            old.row_factory = sqlite3.Row
            tables = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("job_runs", "job_plan", "task_definitions", "task_config_versions",
                          "task_execution_requests", "task_run_events", "task_artifacts",
                          "artifact_lineage", "dataset_registry", "dataset_fields", "dataset_sources",
                          "dataset_consumers", "dataset_partitions", "source_batches", "dataset_versions",
                          "dataset_current", "dataset_quality_results", "metric_definitions",
                          "metric_versions", "metric_health", "task_metric_links"):
                if table not in tables:
                    continue
                rows = old.execute(f"SELECT * FROM {table}").fetchall()
                if not rows:
                    counts[table] = 0
                    continue
                columns = [item[1] for item in old.execute(f"PRAGMA table_info({table})")]
                target_columns = {item[1] for item in new.execute(f"PRAGMA table_info({table})")}
                common = [column for column in columns if column in target_columns]
                marks = ",".join("?" for _ in common)
                for row in rows:
                    values = [row[column] for column in common]
                    if "record_origin" in target_columns and "record_origin" not in common:
                        common_with_origin = common + ["record_origin"]
                        marks_with_origin = marks + ",?"
                        new.execute(f"INSERT OR IGNORE INTO {table} ({','.join(common_with_origin)}) VALUES ({marks_with_origin})",
                                    values + [f"legacy_{kind}"])
                    else:
                        new.execute(f"INSERT OR IGNORE INTO {table} ({','.join(common)}) VALUES ({marks})", values)
                counts[table] = len(rows)
            new.execute("INSERT OR REPLACE INTO management_meta(key,value,updated_at) VALUES(?,?,?)",
                        (f"migrated_{kind}", json.dumps(counts, ensure_ascii=False), now()))
            if kind == "job_runs":
                new.execute("""UPDATE job_runs SET status='failed',finished_at=?,updated_at=?,
                    error=CASE WHEN error='' THEN '旧任务进程已结束，迁移时回收运行状态' ELSE error END
                    WHERE record_origin='legacy_job_runs' AND status='running'""", (now(), now()))
        return {"source": str(source), "kind": kind, "tables": counts, "skipped": False}

    def migrate_from(self, *, job_db: Path | str | None = None,
                     warehouse_db: Path | str | None = None) -> dict:
        result = {"definitions": self.seed_definitions(), "job_runs": None, "warehouse": None}
        if job_db:
            result["job_runs"] = self.copy_legacy_db(job_db, kind="job_runs")
        if warehouse_db:
            result["warehouse"] = self.copy_legacy_db(warehouse_db, kind="warehouse")
        return result

    def seed_definitions(self) -> dict:
        """Load the current declarative definitions into this database."""
        from StockInvestmentTool.ops.task_center import TaskCenter
        from StockInvestmentTool.warehouse.metadata import MetadataStore

        task_center = TaskCenter(self.db_path)
        task_count = task_center.sync_definitions()
        metric_count = task_center.sync_metrics()
        metadata = MetadataStore(self.db_path)
        dataset_names = ("stock_daily", "industry", "fundamentals", "valuation_daily",
                         "money_flow_daily", "indicators")
        for name in dataset_names:
            metadata.register_dataset(name)
        return {"tasks": task_count, "metrics": metric_count, "datasets": len(dataset_names)}

    def counts(self) -> dict[str, int]:
        tables = ("task_definitions", "task_execution_requests", "job_runs", "task_run_events",
                  "task_artifacts", "dataset_registry", "dataset_versions", "metric_definitions",
                  "metric_health", "artifact_lineage")
        with self.connect() as conn:
            return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}

    def seed_health_from_warehouse(self, warehouse_dir: Path | str) -> dict:
        """Seed conservative health facts from existing result files.

        This indexes what is present; it does not claim a missing data family is
        healthy and never rewrites the source files.
        """
        import pandas as pd

        root = Path(warehouse_dir)
        results = {}
        for directory, metric_keys in (("daily", ("close", "volume", "amount")),
                                        ("indicators", ("ma5", "ma20", "rsi14", "macd", "volatility_20")),
                                        ("factors", ("ret_5d", "ret_20d", "vol_ratio", "high_20d", "low_20d"))):
            paths = sorted((root / directory).glob("*.parquet"))
            if not paths:
                continue
            frames = []
            for path in paths:
                try:
                    frames.append(pd.read_parquet(path, columns=["date", "code"]))
                except Exception:
                    continue
            if not frames:
                continue
            frame = pd.concat(frames, ignore_index=True)
            latest = str(pd.to_datetime(frame["date"], errors="coerce").max())[:10]
            covered = int(frame["code"].astype(str).nunique())
            for metric_key in metric_keys:
                self._upsert_health(metric_key, latest, covered, covered, "healthy", "已有正式结果文件")
            results[directory] = {"latest_date": latest, "covered_objects": covered, "metrics": len(metric_keys)}
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO management_meta(key,value,updated_at) VALUES(?,?,?)",
                         ("health_seeded_from", str(root), now()))
        return results

    def _upsert_health(self, metric_key, latest, covered, expected, status, message):
        with self.connect() as conn:
            conn.execute("""INSERT INTO metric_health
                (metric_key,latest_period,expected_period,covered_objects,expected_objects,coverage,
                 last_success_at,status,message,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(metric_key) DO UPDATE SET
                latest_period=excluded.latest_period,expected_period=excluded.expected_period,
                covered_objects=excluded.covered_objects,expected_objects=excluded.expected_objects,
                coverage=excluded.coverage,last_success_at=excluded.last_success_at,
                status=excluded.status,message=excluded.message,updated_at=excluded.updated_at""",
                         (metric_key, latest, latest, covered, expected, 1.0, now(), status, message, now()))
