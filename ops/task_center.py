"""Task and metric management services, independent from web presentation."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml
from StockInvestmentTool.warehouse.dataset_config import load_dataset_config

from StockInvestmentTool.ops.terminology import artifact_labels, task_labels


TASK_CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "tasks"
METRIC_CONFIG = Path(__file__).resolve().parents[1] / "config" / "metrics" / "catalog.yaml"


def management_db_path(default: Path | str | None = None) -> Path:
    """Resolve the active management DB without changing the default legacy path."""
    import os
    if os.getenv("MANAGEMENT_DB_PATH"):
        return Path(os.environ["MANAGEMENT_DB_PATH"])
    if default is not None:
        return Path(default)
    from StockInvestmentTool.config import Config
    return Config.DATA_DIR / "management.db"


class TaskConfigError(ValueError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_task_definitions(directory: Path = TASK_CONFIG_DIR) -> list[dict]:
    definitions = []
    for path in sorted(directory.glob("*.yaml")):
        if path.name == "README.md":
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise TaskConfigError(f"任务配置无法解析: {path}") from exc
        task = data.get("task") or {}
        schedule = data.get("schedule") or {}
        if not task.get("key") or not task.get("display_name"):
            raise TaskConfigError(f"任务缺少 key/display_name: {path}")
        if schedule.get("timezone") != "Asia/Shanghai":
            raise TaskConfigError(f"任务必须使用北京时间: {task['key']}")
        data["_config_path"] = str(path)
        data["_checksum"] = hashlib.sha256(path.read_bytes()).hexdigest()
        definitions.append(data)
    return definitions


class TaskCenter:
    def __init__(self, db_path: Path | str, metadata_db_path: Path | str | None = None):
        self.db_path = Path(db_path)
        self.metadata_db_path = Path(metadata_db_path) if metadata_db_path else self.db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()
        if self.metadata_db_path != self.db_path:
            self._initialize_metric_tables(self.metadata_db_path)

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self):
        with self._connect() as conn:
            conn.executescript("""
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
              input_versions TEXT NOT NULL DEFAULT '{}', requested_by TEXT NOT NULL,
              status TEXT NOT NULL, created_at TEXT NOT NULL
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
              size_bytes INTEGER, status TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifact_lineage (
              upstream_artifact_id TEXT NOT NULL, downstream_artifact_id TEXT NOT NULL,
              relation_type TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(upstream_artifact_id, downstream_artifact_id)
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
              message TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_metric_links (
              task_key TEXT NOT NULL, metric_key TEXT NOT NULL, relation_type TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(task_key, metric_key)
            );
            CREATE TABLE IF NOT EXISTS schema_migrations (
              migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL,
              checksum_before TEXT, checksum_after TEXT
            );
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(task_execution_requests)")}
            if "input_versions" not in columns:
                conn.execute("ALTER TABLE task_execution_requests ADD COLUMN input_versions TEXT NOT NULL DEFAULT '{}' ")
            self._run_schema_migrations(conn)

    @staticmethod
    def _quick_check(conn) -> str:
        row = conn.execute("PRAGMA quick_check").fetchone()
        return str(row[0] if row else "unknown")

    def _run_schema_migrations(self, conn) -> None:
        """正式增量迁移机制：每个迁移只执行一次，前后运行 SQLite quick_check。

        migrations 为有序列表，新增迁移按追加顺序登记。
        """
        migrations: list[tuple[str, str]] = [
            ("v1_initial_task_center", "任务中心初始 Schema"),
            ("v2_job_runs_managed", "运行记录收敛到 management.db"),
        ]
        applied = {row[0] for row in conn.execute("SELECT migration_id FROM schema_migrations")}
        for migration_id, _desc in migrations:
            if migration_id in applied:
                continue
            before = self._quick_check(conn)
            conn.execute("INSERT OR IGNORE INTO schema_migrations(migration_id,applied_at) VALUES(?,?)",
                         (migration_id, _now()))
            after = self._quick_check(conn)
            conn.execute("UPDATE schema_migrations SET checksum_before=?, checksum_after=? WHERE migration_id=?",
                         (before, after, migration_id))

    @staticmethod
    def _initialize_metric_tables(db_path: Path) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.executescript("""
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
              message TEXT NOT NULL DEFAULT '', coverage_by_type TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS task_metric_links (
              task_key TEXT NOT NULL, metric_key TEXT NOT NULL, relation_type TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(task_key, metric_key)
            );
            """)

    def sync_definitions(self) -> int:
        count = 0
        for data in load_task_definitions():
            task = data["task"]
            key = task["key"]
            raw = json.dumps({k: v for k, v in data.items() if not k.startswith("_")}, ensure_ascii=False, sort_keys=True)
            with self._connect() as conn:
                current = conn.execute("SELECT MAX(version) FROM task_config_versions WHERE task_key=?", (key,)).fetchone()[0] or 0
                existing = conn.execute("SELECT checksum FROM task_config_versions WHERE task_key=? AND version=?", (key, current)).fetchone()
                if not existing or existing[0] != data["_checksum"]:
                    current += 1
                    conn.execute("INSERT INTO task_config_versions VALUES (?,?,?,?,?,?,?)",
                                 (key, current, raw, data["_checksum"], "active" if current == 1 else "draft", _now(), _now() if current == 1 else None))
                conn.execute("""INSERT INTO task_definitions(task_key,display_name,stage,task_type,enabled,active_config_version,updated_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(task_key) DO UPDATE SET display_name=excluded.display_name,
                    stage=excluded.stage,task_type=excluded.task_type,updated_at=excluded.updated_at""",
                             (key, task["display_name"], task.get("stage", ""), task.get("type", ""),
                              int(data.get("schedule", {}).get("enabled", False)), self._active_version(conn, key), _now()))
            with sqlite3.connect(self.metadata_db_path) as metric_conn:
                metric_conn.execute("CREATE TABLE IF NOT EXISTS task_metric_links (task_key TEXT NOT NULL, metric_key TEXT NOT NULL, relation_type TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(task_key, metric_key))")
                for metric in task.get("producer_metrics", []):
                    metric_conn.execute("INSERT OR IGNORE INTO task_metric_links VALUES(?,?,?,1)", (key, metric, "output"))
            count += 1
        return count

    @staticmethod
    def _active_version(conn, task_key: str) -> int | None:
        row = conn.execute(
            "SELECT version FROM task_config_versions WHERE task_key=? AND status='active' ORDER BY version DESC LIMIT 1",
            (task_key,),
        ).fetchone()
        return row[0] if row else None

    def activate_task_config(self, task_key: str, version: int) -> None:
        """Make a validated task configuration version active."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status,config FROM task_config_versions WHERE task_key=? AND version=?",
                (task_key, int(version)),
            ).fetchone()
            if row is None:
                raise TaskConfigError("任务配置版本不存在")
            conn.execute(
                "UPDATE task_config_versions SET status='superseded' WHERE task_key=? AND status='active'",
                (task_key,),
            )
            conn.execute(
                "UPDATE task_config_versions SET status='active',activated_at=? WHERE task_key=? AND version=?",
                (_now(), task_key, int(version)),
            )
            conn.execute(
                "UPDATE task_definitions SET active_config_version=?,updated_at=? WHERE task_key=?",
                (int(version), _now(), task_key),
            )
            config = json.loads(row[1])
            conn.execute(
                "UPDATE task_definitions SET enabled=?,updated_at=? WHERE task_key=?",
                (int(bool((config.get("schedule") or {}).get("enabled", False))), _now(), task_key),
            )

    def validate_task_config(self, config: dict) -> dict:
        """Validate the stable task configuration shape before saving it."""
        if not isinstance(config, dict) or not isinstance(config.get("task"), dict):
            raise TaskConfigError("任务配置必须包含 task 对象")
        task = config["task"]
        for key in ("key", "display_name"):
            if not task.get(key):
                raise TaskConfigError(f"任务缺少 {key}")
        schedule = config.get("schedule") or {}
        if schedule.get("timezone") != "Asia/Shanghai":
            raise TaskConfigError("任务必须使用北京时间")
        if schedule.get("enabled") is not None and not isinstance(schedule["enabled"], bool):
            raise TaskConfigError("schedule.enabled 必须是布尔值")
        execution = config.get("execution") or {}
        if int(execution.get("retry_limit", 0)) < 0:
            raise TaskConfigError("retry_limit 不能为负数")
        return {"valid": True, "task_key": task["key"]}

    def save_task_config(self, task_key: str, config: dict, *, activate: bool = False) -> int:
        """Persist a draft task configuration, optionally making it active."""
        self.validate_task_config(config)
        if config["task"].get("key") != task_key:
            raise TaskConfigError("配置中的任务 key 与请求不一致")
        raw = json.dumps(config, ensure_ascii=False, sort_keys=True)
        checksum = hashlib.sha256(raw.encode()).hexdigest()
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM task_definitions WHERE task_key=?", (task_key,)).fetchone():
                raise TaskConfigError(f"任务不存在: {task_key}")
            version = (conn.execute("SELECT MAX(version) FROM task_config_versions WHERE task_key=?", (task_key,)).fetchone()[0] or 0) + 1
            now = _now()
            conn.execute("INSERT INTO task_config_versions VALUES (?,?,?,?,?,?,?)",
                         (task_key, version, raw, checksum, "active" if activate else "validated", now, now if activate else None))
            if activate:
                conn.execute("UPDATE task_config_versions SET status='superseded' WHERE task_key=? AND version<>? AND status='active'", (task_key, version))
                conn.execute("UPDATE task_definitions SET active_config_version=?,enabled=?,updated_at=? WHERE task_key=?",
                             (version, int((config.get("schedule") or {}).get("enabled", False)), now, task_key))
        return version

    def set_task_enabled(self, task_key: str, enabled: bool) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT active_config_version FROM task_definitions WHERE task_key=?", (task_key,)).fetchone()
            if not row:
                raise TaskConfigError(f"任务不存在: {task_key}")
            config_row = conn.execute(
                "SELECT config FROM task_config_versions WHERE task_key=? AND version=?",
                (task_key, row[0]),
            ).fetchone()
            if config_row:
                config = json.loads(config_row[0])
                config.setdefault("schedule", {})["enabled"] = bool(enabled)
                raw_config = json.dumps(config, ensure_ascii=False, sort_keys=True)
                conn.execute(
                    "UPDATE task_config_versions SET config=?,checksum=? WHERE task_key=? AND version=?",
                    (raw_config, hashlib.sha256(raw_config.encode()).hexdigest(), task_key, row[0]),
                )
            conn.execute("UPDATE task_definitions SET enabled=?,updated_at=? WHERE task_key=?",
                         (int(bool(enabled)), _now(), task_key))

    def sync_metrics(self) -> int:
        data = yaml.safe_load(METRIC_CONFIG.read_text(encoding="utf-8")) or {}
        count = 0
        with sqlite3.connect(self.metadata_db_path) as conn:
            for metric in data.get("metrics", []):
                now = _now()
                conn.execute("""INSERT INTO metric_definitions VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(metric_key) DO UPDATE SET display_name=excluded.display_name,
                    category=excluded.category,definition=excluded.definition,unit=excluded.unit,
                    producer_task=excluded.producer_task,builtin=excluded.builtin,editable=excluded.editable,
                    updated_at=excluded.updated_at""",
                            (metric["key"], metric["name"], metric["category"], metric["definition"], metric.get("unit"),
                             metric["producer_task"], int(metric.get("builtin", False)), int(metric.get("editable", False)), 1, now))
                checksum = hashlib.sha256(json.dumps(metric, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                version = conn.execute("SELECT MAX(version) FROM metric_versions WHERE metric_key=?", (metric["key"],)).fetchone()[0] or 0
                if version == 0:
                    conn.execute("INSERT INTO metric_versions VALUES(?,?,?,?,?,?,?)",
                                 (metric["key"], 1, json.dumps(metric, ensure_ascii=False), checksum, "active", now, now))
                conn.execute("INSERT OR IGNORE INTO task_metric_links VALUES(?,?,?,1)", (metric["producer_task"], metric["key"], "output"))
                count += 1
        return count

    def update_metric_health(self, metric_key: str, *, latest_period: str | None,
                             covered_objects: int, expected_objects: int | None,
                             last_run_id: int | None = None, status: str = "healthy",
                             message: str = "", metric_version: int | None = None,
                             asset_type_counts: dict | None = None) -> None:
        expected = expected_objects or 0
        coverage = (covered_objects / expected) if expected else None
        with sqlite3.connect(self.metadata_db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(metric_health)")}
            if "coverage_by_type" not in columns:
                conn.execute("ALTER TABLE metric_health ADD COLUMN coverage_by_type TEXT NOT NULL DEFAULT '{}' ")
            conn.execute("""INSERT INTO metric_health
                (metric_key,metric_version,latest_period,expected_period,covered_objects,
                 expected_objects,coverage,last_success_at,last_run_id,status,message,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(metric_key) DO UPDATE SET
                metric_version=excluded.metric_version,latest_period=excluded.latest_period,
                expected_period=excluded.expected_period,covered_objects=excluded.covered_objects,
                expected_objects=excluded.expected_objects,coverage=excluded.coverage,
                 last_success_at=excluded.last_success_at,last_run_id=excluded.last_run_id,
                 status=excluded.status,message=excluded.message,updated_at=excluded.updated_at""",
                         (metric_key, metric_version, latest_period, latest_period, covered_objects,
                          expected_objects, coverage, _now() if status == "healthy" else None,
                          last_run_id, status, message, _now()))
            conn.execute("UPDATE metric_health SET coverage_by_type=? WHERE metric_key=?",
                         (json.dumps(asset_type_counts or {}, ensure_ascii=False), metric_key))

    def metric_applicability(self, metric_key: str, asset_types: list[str]) -> dict[str, str]:
        from StockInvestmentTool.warehouse.asset_profiles import applicability
        return {asset_type: applicability(asset_type, "metrics", metric_key) for asset_type in asset_types}

    def task_artifacts(self, task_key: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if task_key:
                rows = conn.execute("""SELECT a.*, r.job_name, r.status AS run_status
                    FROM task_artifacts a LEFT JOIN job_runs r ON r.id=a.run_id
                    WHERE r.job_name=? ORDER BY a.created_at DESC""", (task_key,)).fetchall()
            else:
                rows = conn.execute("""SELECT a.*, r.job_name, r.status AS run_status
                    FROM task_artifacts a LEFT JOIN job_runs r ON r.id=a.run_id
                    ORDER BY a.created_at DESC""").fetchall()
        return [artifact_labels(dict(row)) for row in rows]

    def set_metric_definition(self, metric_key: str, *, display_name: str, category: str,
                              definition: str, unit: str = "", producer_task: str,
                              builtin: bool = False) -> int:
        with sqlite3.connect(self.metadata_db_path) as conn:
            row = conn.execute("SELECT builtin, editable FROM metric_definitions WHERE metric_key=?", (metric_key,)).fetchone()
            if row and row[0] and not row[1]:
                raise TaskConfigError("基础指标不可修改")
            now = _now()
            conn.execute("""INSERT INTO metric_definitions
                (metric_key,display_name,category,definition,unit,producer_task,builtin,editable,enabled,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(metric_key) DO UPDATE SET
                display_name=excluded.display_name,category=excluded.category,definition=excluded.definition,
                unit=excluded.unit,producer_task=excluded.producer_task,updated_at=excluded.updated_at""",
                         (metric_key, display_name, category, definition, unit, producer_task, int(builtin), 1, 1, now))
            version = (conn.execute("SELECT MAX(version) FROM metric_versions WHERE metric_key=?", (metric_key,)).fetchone()[0] or 0) + 1
            checksum = hashlib.sha256(f"{metric_key}:{version}:{definition}".encode()).hexdigest()
            conn.execute("INSERT INTO metric_versions VALUES(?,?,?,?,?,?,?)",
                         (metric_key, version, definition, checksum, "active", now, now))
            conn.execute("""INSERT INTO metric_health(metric_key,metric_version,status,message,updated_at)
                         VALUES(?,?,?,?,?) ON CONFLICT(metric_key) DO UPDATE SET
                         metric_version=excluded.metric_version,status='stale',message='指标定义已变更，需要重新生成',updated_at=excluded.updated_at""",
                         (metric_key, version, "stale", "指标定义已变更，需要重新生成", now))
            return version

    def disable_metric(self, metric_key: str) -> None:
        with sqlite3.connect(self.metadata_db_path) as conn:
            row = conn.execute("SELECT builtin FROM metric_definitions WHERE metric_key=?", (metric_key,)).fetchone()
            if row is None:
                raise TaskConfigError("指标不存在")
            if row[0]:
                raise TaskConfigError("基础指标不可停用")
            conn.execute("UPDATE metric_definitions SET enabled=0,updated_at=? WHERE metric_key=?", (_now(), metric_key))

    def create_request(self, task_key: str, trigger_type: str, *, period_start=None, period_end=None,
                       symbols=None, requested_by="admin", input_versions=None) -> str:
        if trigger_type not in {"scheduled", "manual", "backfill", "retry", "shadow"}:
            raise TaskConfigError("非法任务触发类型")
        if period_start and period_end and str(period_end) < str(period_start):
            raise TaskConfigError("任务结束周期不能早于开始周期")
        request_id = f"req_{datetime.now():%Y%m%d%H%M%S}_{hashlib.sha1(f'{task_key}{_now()}'.encode()).hexdigest()[:10]}"
        with self._connect() as conn:
            row = conn.execute("SELECT active_config_version FROM task_definitions WHERE task_key=?", (task_key,)).fetchone()
            if not row:
                raise TaskConfigError(f"任务不存在: {task_key}")
            conn.execute("""INSERT INTO task_execution_requests
                (request_id,task_key,trigger_type,period_start,period_end,symbols,config_version,
                 input_versions,requested_by,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                         (request_id, task_key, trigger_type, period_start, period_end,
                          json.dumps(symbols or [], ensure_ascii=False), row[0],
                          json.dumps(input_versions or {}, ensure_ascii=False), requested_by,
                          "requested", _now()))
        return request_id

    def request(self, request_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_execution_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["symbols"] = json.loads(item.get("symbols") or "[]")
        except (TypeError, ValueError):
            item["symbols"] = []
        try:
            item["input_versions"] = json.loads(item.get("input_versions") or "{}")
        except (TypeError, ValueError):
            item["input_versions"] = {}
        return item

    def update_request(self, request_id: str, status: str) -> None:
        if status not in {"requested", "running", "success", "partial_success", "failed", "timeout", "skipped", "cancelled"}:
            raise TaskConfigError("非法执行请求状态")
        with self._connect() as conn:
            conn.execute("UPDATE task_execution_requests SET status=? WHERE request_id=?", (status, request_id))

    def recover_inflight_requests(self, *, before: str | None = None) -> int:
        """Recover abandoned running requests without touching live work.

        ``before`` is required by periodic recovery so a request started at the
        same scheduler tick is not immediately marked failed. Startup callers
        pass the process start boundary to reclaim work killed by that restart.
        """
        with self._connect() as conn:
            if before:
                cur = conn.execute(
                    "UPDATE task_execution_requests SET status='failed' "
                    "WHERE status='running' AND created_at<?", (before,))
            else:
                cur = conn.execute(
                    "UPDATE task_execution_requests SET status='failed' WHERE status='running'")
        return cur.rowcount

    def event(self, run_id: int, message: str, *, level="INFO", phase="", event_type="log",
              processed=None, total=None, current_item=None, payload=None):
        now = _now()
        with self._connect() as conn:
            conn.execute("INSERT INTO task_run_events(run_id,event_time,level,phase,event_type,message,processed,total,current_item,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (run_id, now, level, phase, event_type, message, processed, total, current_item,
                          json.dumps(payload or {}, ensure_ascii=False, default=str)))
        log_path = self.db_path.parent / "task_logs" / f"{run_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{now} [{level}] [{phase}] {message}\n")

    def register_artifact(self, *, run_id=None, dataset_name=None, artifact_type="file", partition_key=None,
                          file_path: Path | str, status="ready") -> str:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(path)
        artifact_id = f"artifact_{hashlib.sha256(str(path).encode()).hexdigest()[:20]}"
        frame = None
        try:
            if path.suffix == ".parquet":
                frame = pd.read_parquet(path)
            elif path.suffix == ".csv":
                frame = pd.read_csv(path, nrows=10000)
        except Exception:
            frame = None
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        with self._connect() as conn:
            conn.execute("""INSERT OR REPLACE INTO task_artifacts
                (artifact_id,run_id,dataset_name,artifact_type,partition_key,file_path,file_name,
                 file_format,row_count,symbol_count,min_date,max_date,checksum,size_bytes,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (artifact_id, run_id, dataset_name, artifact_type, partition_key, str(path), path.name,
                          path.suffix.lstrip("."), len(frame) if frame is not None else None,
                          int(frame["code"].nunique()) if frame is not None and "code" in frame else None,
                          str(pd.to_datetime(frame["date"]).min())[:10] if frame is not None and "date" in frame else None,
                          str(pd.to_datetime(frame["date"]).max())[:10] if frame is not None and "date" in frame else None,
                          checksum, path.stat().st_size, status, _now()))
        return artifact_id

    def preview_artifact(self, artifact_id: str, *, limit=50, offset=0) -> dict:
        limit = max(1, min(int(limit), 200))
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if not row:
            raise FileNotFoundError(artifact_id)
        path = Path(row["file_path"])
        if not path.exists():
            raise FileNotFoundError(path)
        allowed_roots = [self.db_path.parent.resolve(), self.metadata_db_path.parent.resolve()]
        resolved = path.resolve()
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            raise PermissionError("产物路径不在允许的数据目录内")
        if path.suffix == ".parquet":
            frame = pd.read_parquet(path).iloc[offset:offset + limit]
        elif path.suffix == ".csv":
            frame = pd.read_csv(path, skiprows=range(1, offset + 1), nrows=limit)
        else:
            raise ValueError("仅支持 Parquet/CSV 预览")
        return {"artifact": dict(row), "columns": list(frame.columns), "rows": frame.where(pd.notna(frame), None).to_dict("records")}

    def list_tasks(self):
        with self._connect() as conn:
            return [task_labels(dict(row)) for row in conn.execute("SELECT * FROM task_definitions ORDER BY stage,task_key")]

    def active_configs(self) -> dict[str, dict]:
        """Return the active config for every task from management.db.

        唯一事实源：Active Config（management.db task_config_versions.status='active'）。
        YAML 仅在初始同步时写入；运行期调度以这里为准。
        """
        result = {}
        with self._connect() as conn:
            rows = conn.execute("""SELECT t.task_key, t.enabled, t.stage, t.task_type,
                v.version, v.config
                FROM task_definitions t
                JOIN task_config_versions v ON v.task_key=t.task_key AND v.version=t.active_config_version
                WHERE t.active_config_version IS NOT NULL""").fetchall()
        for key, enabled, stage, task_type, version, raw in rows:
            try:
                config = json.loads(raw or "{}")
            except (TypeError, ValueError):
                config = {}
            schedule = config.get("schedule") or {}
            result[key] = {
                "task_key": key,
                "display_name": config.get("task", {}).get("display_name", key),
                "stage": stage,
                "task_type": task_type,
                "version": version,
                "enabled": bool(enabled and schedule.get("enabled", False)),
                "schedule": schedule,
                "config": config,
            }
        return result

    def list_metrics(self):
        with sqlite3.connect(self.metadata_db_path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute("""SELECT d.*, h.latest_period,h.expected_period,h.covered_objects,
                h.expected_objects,h.coverage,h.last_success_at,h.last_run_id,h.status AS health_status,h.message
                FROM metric_definitions d LEFT JOIN metric_health h ON h.metric_key=d.metric_key ORDER BY d.category,d.metric_key""")]

    def data_assets(self, *, category: str | None = None, status: str | None = None,
                    date: str | None = None) -> list[dict]:
        """Return user-facing data assets, not technical batch artifacts."""
        with sqlite3.connect(self.metadata_db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""SELECT d.*, h.latest_period, h.expected_period,
                h.covered_objects, h.expected_objects, h.coverage, h.last_success_at,
                h.last_run_id, h.status AS health_status, h.message, h.coverage_by_type
                FROM metric_definitions d LEFT JOIN metric_health h ON h.metric_key=d.metric_key
                ORDER BY d.category, d.display_name""").fetchall()
            assets = [dict(row) for row in rows]
            datasets = conn.execute("""SELECT dataset_name AS metric_key, display_name,
                '数据集' AS category, description AS definition, NULL AS unit,
                NULL AS producer_task, 1 AS builtin, 0 AS editable, enabled,
                updated_at, NULL AS latest_period, NULL AS expected_period,
                NULL AS covered_objects, NULL AS expected_objects, NULL AS coverage,
                NULL AS last_success_at, NULL AS last_run_id, NULL AS health_status,
                NULL AS message, '{}' AS coverage_by_type
                 FROM dataset_registry ORDER BY display_name""").fetchall()
            assets.extend(dict(row) for row in datasets)
            for asset in assets[-len(datasets):] if datasets else []:
                current = conn.execute(
                    "SELECT v.max_date,v.quality_status,v.published_at,v.row_count,v.symbol_count "
                    "FROM dataset_current c JOIN dataset_versions v ON v.version_id=c.version_id "
                    "WHERE c.dataset_name=? ORDER BY c.partition_key DESC LIMIT 1",
                    (asset["metric_key"],),
                ).fetchone()
                if current:
                    asset["latest_period"] = current[0]
                    asset["last_success_at"] = current[2]
                    asset["covered_objects"] = current[4]
                    asset["expected_objects"] = current[4]
                    asset["coverage"] = 1.0
                    asset["health_status"] = "healthy" if current[1] in ("PASS", "WARNING") else "critical"
                    asset["message"] = f"当前版本 {current[3]} 行"
            metric_dataset = {
                "close": "stock_daily", "volume": "stock_daily", "amount": "stock_daily",
                "pe_ttm": "valuation_daily", "pb_mrq": "valuation_daily",
                "roe": "fundamentals", "money_flow_net": "money_flow_daily",
            }
            metric_dataset.update({key: "indicators" for key in {
                "ma5", "ma10", "ma20", "ma60", "ma120", "ma240", "ma17", "ma63", "rsi14", "macd",
                "volatility20", "volatility_20", "atr14", "change_amount", "amplitude", "vol_ma5", "low_3m",
                "year_low", "pct_chg", "vol_ratio", "ret_5d", "ret_20d", "high_20d", "low_20d", "bias_ratio",
                "take_profit_reference", "dual_ma_low", "amplitude_abs", "custom_example",
            }})
            dataset_health = {item["metric_key"]: item for item in assets[-len(datasets):]} if datasets else {}
            published_facts = {}
            for dataset_name in {item["metric_key"] for item in assets[-len(datasets):]} if datasets else set():
                rows = conn.execute(
                    """SELECT v.max_date, v.quality_status, v.published_at, v.row_count, v.symbol_count,
                              v.published_path
                       FROM dataset_current c JOIN dataset_versions v ON v.version_id=c.version_id
                       WHERE c.dataset_name=? AND v.publish_status='published'
                       ORDER BY v.max_date DESC, c.partition_key DESC""", (dataset_name,)
                ).fetchall()
                if rows:
                    latest = rows[0]
                    latest_date = latest[0]
                    if not latest_date and latest[5]:
                        try:
                            frame = pd.read_parquet(latest[5])
                            date_columns = {
                                "date", "trade_date", "trading_date", "snapshot_date",
                                "report_date", "stat_date", "period",
                            }
                            candidates = [column for column in frame.columns if column in date_columns]
                            if candidates:
                                values = pd.concat([pd.to_datetime(frame[column], errors="coerce") for column in candidates])
                                if values.notna().any():
                                    latest_date = values.max().strftime("%Y-%m-%d")
                        except Exception:
                            pass
                    try:
                        partition_type = load_dataset_config(dataset_name)["dataset"]["partition"]["type"]
                    except Exception:
                        partition_type = "month"
                    covered = (sum(int(row[4] or 0) for row in rows)
                               if partition_type == "symbol" else max(int(row[4] or 0) for row in rows))
                    published_facts[dataset_name] = {
                        "latest_period": latest_date, "quality_status": latest[1],
                        "published_at": latest[2], "covered_objects": covered,
                    }
            for asset in assets:
                dataset_name = (asset.get("metric_key") if asset.get("metric_key") in dataset_health
                                else metric_dataset.get(asset.get("metric_key")))
                fact = published_facts.get(dataset_name)
                if fact:
                    asset["latest_period"] = fact["latest_period"]
                    asset["last_success_at"] = fact["published_at"]
                    asset["covered_objects"] = fact["covered_objects"]
                    asset["expected_objects"] = fact["covered_objects"]
                    asset["coverage"] = 1.0
                    asset["health_status"] = "healthy" if fact["quality_status"] in ("PASS", "WARNING") else "critical"
                    asset["message"] = f"Published {dataset_name} · {fact['quality_status']}"
                elif dataset_name:
                    dataset = dataset_health.get(dataset_name)
                    if dataset and not asset.get("health_status") and dataset.get("health_status"):
                        asset["health_status"] = dataset["health_status"]
                        asset["latest_period"] = dataset.get("latest_period")
                        asset["last_success_at"] = dataset.get("last_success_at")
                        asset["covered_objects"] = dataset.get("covered_objects")
                        asset["expected_objects"] = dataset.get("expected_objects")
                        asset["coverage"] = dataset.get("coverage")
                        asset["message"] = f"关联数据集 {dataset_name}"
            assets = [item for item in assets if item.get("metric_key") != "volatility20"]
        if category:
            assets = [item for item in assets if item.get("category") == category]
        if status:
            assets = [item for item in assets if item.get("health_status") == status]
        if date:
            assets = [item for item in assets if str(item.get("latest_period") or "") <= date]
        return assets

    def task_overview(self) -> dict:
        with self._connect() as conn:
            today = datetime.now().strftime("%Y-%m-%d")
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM job_runs WHERE started_at LIKE ? GROUP BY status", (today + "%",)).fetchall()
            running = conn.execute("SELECT COUNT(*) FROM job_runs WHERE status='running'").fetchone()[0]
            completed = conn.execute("SELECT COUNT(*) FROM job_runs WHERE started_at LIKE ? AND status IN ('success','partial_success')", (today + "%",)).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM job_runs WHERE started_at LIKE ?", (today + "%",)).fetchone()[0]
        return {"counts": {row[0]: row[1] for row in rows}, "running": running,
                "today_completed": completed, "today_total": total, "date": today}

    def task_catalog(self) -> list[dict]:
        """Return one task object with configuration and runtime facts."""
        with self._connect() as conn:
            definitions = [dict(row) for row in conn.execute("SELECT * FROM task_definitions ORDER BY stage, task_key")]
            for item in definitions:
                configs = conn.execute("SELECT config,version,status FROM task_config_versions WHERE task_key=? ORDER BY version DESC", (item["task_key"],)).fetchall()
                active = next((dict(row) for row in configs if row["status"] == "active"), None)
                item["config"] = json.loads(active["config"]) if active else {}
                item["schedule"] = item["config"].get("schedule", {})
                item["scope"] = item["config"].get("scope", {})
                item["execution"] = item["config"].get("execution", {})
                item["policy"] = item["config"].get("policy", {})
                item["config_versions"] = [dict(row) for row in configs]
                aliases = {
                    "stock_daily_capture": ["stock_daily_capture", "daily_sync"],
                    "stock_daily_build": ["stock_daily_build"],
                    "stock_daily_quality": ["stock_daily_quality"],
                    "stock_daily_publish": ["stock_daily_publish"],
                    "indicators_build": ["indicators_build", "rebuild_indicators"],
                }
                runtime_names = aliases.get(item["task_key"], [item["task_key"]])
                marks = ",".join("?" for _ in runtime_names)
                latest = conn.execute(f"SELECT * FROM job_runs WHERE job_name IN ({marks}) ORDER BY id DESC LIMIT 1", runtime_names).fetchone()
                running = conn.execute(f"SELECT * FROM job_runs WHERE job_name IN ({marks}) AND status='running' ORDER BY id DESC LIMIT 1", runtime_names).fetchone()
                item["latest_run"] = dict(latest) if latest else None
                item["running_run"] = dict(running) if running else None
                item["runtime_task_keys"] = runtime_names
        return [task_labels(item) for item in definitions]

    def task_overview_payload(self) -> dict:
        tasks = self.task_catalog()
        groups = {}
        for task in tasks:
            groups.setdefault(task["stage"], []).append(task)
        return {"summary": self.task_overview(), "tasks": tasks,
                "groups": [{"stage": stage, "tasks": items} for stage, items in groups.items()]}

    def task(self, task_key: str) -> dict | None:
        with self._connect() as conn:
            task = conn.execute("SELECT * FROM task_definitions WHERE task_key=?", (task_key,)).fetchone()
            if task is None:
                return None
            configs = conn.execute("SELECT * FROM task_config_versions WHERE task_key=? ORDER BY version DESC", (task_key,)).fetchall()
        with sqlite3.connect(self.metadata_db_path) as conn:
            conn.row_factory = sqlite3.Row
            links = conn.execute("SELECT * FROM task_metric_links WHERE task_key=? ORDER BY metric_key", (task_key,)).fetchall()
        result = dict(task)
        result["config_versions"] = [dict(row) for row in configs]
        result["metrics"] = [dict(row) for row in links]
        return result

    def events(self, run_id: int, *, limit: int = 200, after_id: int = 0) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM task_run_events WHERE run_id=? AND event_id>? ORDER BY event_id LIMIT ?",
                                (int(run_id), int(after_id), limit)).fetchall()
        return [artifact_labels(dict(row)) for row in rows]

    def artifacts(self, run_id: int | None = None, artifact_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if artifact_id:
                rows = conn.execute("SELECT * FROM task_artifacts WHERE artifact_id=?", (artifact_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM task_artifacts WHERE run_id=? ORDER BY created_at", (int(run_id),)).fetchall()
        return [artifact_labels(dict(row)) for row in rows]

    def link_lineage(self, upstream_artifact_id: str, downstream_artifact_id: str, relation_type: str = "input") -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO artifact_lineage VALUES(?,?,?,?)",
                         (upstream_artifact_id, downstream_artifact_id, relation_type, _now()))

    def lineage(self, artifact_id: str, direction: str = "both") -> dict:
        if direction not in {"upstream", "downstream", "both"}:
            raise ValueError("非法血缘方向")
        with self._connect() as conn:
            result = {"upstream": [], "downstream": []}
            if direction in {"upstream", "both"}:
                result["upstream"] = [dict(row) for row in conn.execute(
                    "SELECT l.*, a.file_name, a.dataset_name, a.artifact_type FROM artifact_lineage l "
                    "LEFT JOIN task_artifacts a ON a.artifact_id=l.upstream_artifact_id WHERE l.downstream_artifact_id=?",
                    (artifact_id,)).fetchall()]
            if direction in {"downstream", "both"}:
                result["downstream"] = [dict(row) for row in conn.execute(
                    "SELECT l.*, a.file_name, a.dataset_name, a.artifact_type FROM artifact_lineage l "
                    "LEFT JOIN task_artifacts a ON a.artifact_id=l.downstream_artifact_id WHERE l.upstream_artifact_id=?",
                    (artifact_id,)).fetchall()]
        return result
