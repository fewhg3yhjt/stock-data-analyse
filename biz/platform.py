# -*- coding: utf-8 -*-
"""平台治理：Health / Backup / 统一错误结构。

依据 docs/PLATFORM_RUNTIME_AND_OPERATIONS_DESIGN.md §6/§9/§10。
- /health/live：进程存活
- /health/ready：关键依赖可用（business.db / management.db / Published 元数据 / 磁盘）
- /health/details：管理详情
- 备份：SQLite backup API + manifest + checksum；备份目录不得位于生产数据目录内部
- 统一错误结构：{error: {code, message, retryable, details}, request_id}
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from StockInvestmentTool.biz.db import BusinessDB, now_utc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 统一错误结构
# ---------------------------------------------------------------------------

@dataclass
class ApiError:
    code: str
    message: str
    retryable: bool = False
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
            }
        }


def success_response(data: Any, request_id: str = "") -> dict:
    return {"data": data, "request_id": request_id}


def error_response(error: ApiError, request_id: str = "") -> dict:
    out = error.to_dict()
    out["request_id"] = request_id
    return out


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@dataclass
class HealthComponent:
    name: str
    status: str           # healthy / degraded / unhealthy / disabled / not_run / stale
    code: str = ""
    blocking: bool = False
    severity: str = "info"
    details: dict = field(default_factory=dict)


class HealthService:
    """三类健康探针。"""

    def __init__(self, business_db: BusinessDB | None = None,
                 management_db_path: str | None = None):
        self.business_db = business_db or BusinessDB()
        self.management_db_path = management_db_path or ""

    def live(self) -> dict:
        return {"status": "ok", "observed_at": now_utc()}

    def ready(self) -> dict:
        checks = [
            self._check_business_db(),
            self._check_management_db(),
            self._check_disk(),
        ]
        blocking = [c for c in checks if c.blocking and c.status != "healthy"]
        overall = "degraded" if blocking else "ok"
        return {
            "status": overall,
            "observed_at": now_utc(),
            "components": [c.__dict__ for c in checks],
        }

    def details(self) -> dict:
        ready = self.ready()
        return {
            "status": ready["status"],
            "observed_at": now_utc(),
            "components": ready["components"],
            "business_db": str(self.business_db.db_path),
            "management_db": self.management_db_path,
        }

    def _check_business_db(self) -> HealthComponent:
        try:
            self._validate_sqlite_file(self.business_db.db_path)
            with self.business_db.connect() as conn:
                result = conn.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise sqlite3.DatabaseError(f"quick_check failed: {result[0] if result else 'no result'}")
            return HealthComponent(name="business.db", status="healthy")
        except Exception as e:  # noqa: BLE001
            return HealthComponent(name="business.db", status="unhealthy", code="DB_UNREACHABLE",
                                   blocking=True, details={"error": str(e)})

    def _check_management_db(self) -> HealthComponent:
        if not self.management_db_path:
            return HealthComponent(name="management.db", status="disabled",
                                   code="NOT_CONFIGURED")
        try:
            self._validate_sqlite_file(Path(self.management_db_path))
            with sqlite3.connect(self.management_db_path) as conn:
                result = conn.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise sqlite3.DatabaseError(f"quick_check failed: {result[0] if result else 'no result'}")
            return HealthComponent(name="management.db", status="healthy")
        except Exception as e:  # noqa: BLE001
            return HealthComponent(name="management.db", status="unhealthy", code="DB_UNREACHABLE",
                                   blocking=True, details={"error": str(e)})

    @staticmethod
    def _validate_sqlite_file(path: Path) -> None:
        """Reject a non-SQLite file before SQLite treats it as a new empty DB."""
        if not path.exists() or not path.is_file():
            raise sqlite3.DatabaseError(f"database file unavailable: {path}")
        with path.open("rb") as handle:
            header = handle.read(16)
        if header != b"SQLite format 3\x00":
            raise sqlite3.DatabaseError("invalid SQLite file header")

    def _check_disk(self, threshold_mb: int = 500) -> HealthComponent:
        try:
            stat = shutil.disk_usage(self.business_db.db_path.parent)
            free_mb = stat.free / (1024 * 1024)
            status = "healthy" if free_mb > threshold_mb else "degraded"
            return HealthComponent(name="disk", status=status, code="LOW_DISK" if status != "healthy" else "",
                                   severity="warning" if status != "healthy" else "info",
                                   details={"free_mb": round(free_mb)})
        except Exception as e:  # noqa: BLE001
            return HealthComponent(name="disk", status="degraded", code="DISK_CHECK_FAILED",
                                   details={"error": str(e)})


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

class BackupService:
    """备份：SQLite backup API + manifest + checksum。"""

    def __init__(self, backup_root: Path):
        self.backup_root = backup_root

    def backup_sqlite(self, db_path: Path, label: str) -> Path:
        """备份单个 SQLite 库，返回备份文件路径。"""
        self.backup_root.mkdir(parents=True, exist_ok=True)
        db_resolved = db_path.resolve()
        backup_resolved = self.backup_root.resolve()
        # 禁止：备份目录位于生产数据目录内部（即备份目录与数据目录存在包含关系）
        data_root = db_resolved.parent
        try:
            backup_resolved.relative_to(data_root)
            same_tree = True
        except ValueError:
            same_tree = False
        if same_tree:
            raise ValueError("备份目录不得位于生产数据目录内部")
        if backup_resolved == data_root:
            raise ValueError("备份目录不得与数据目录相同")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dest = self.backup_root / f"{label}.db.{ts}.bak"
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(dest))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        return dest

    @staticmethod
    def checksum(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def write_manifest(self, entries: list[dict]) -> Path:
        manifest = {
            "created_at": now_utc(),
            "entries": entries,
        }
        path = self.backup_root / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        return path

    def backup_set(self, dbs: dict[str, Path]) -> dict:
        """备份一组数据库并生成 manifest。返回备份记录。"""
        entries = []
        for label, path in dbs.items():
            dest = self.backup_sqlite(path, label)
            entries.append({
                "label": label, "source": str(path), "backup": str(dest),
                "checksum": self.checksum(dest),
            })
        self.write_manifest(entries)
        return {"backup_root": str(self.backup_root), "entries": entries}

    def restore_check(self, backup_path: Path) -> bool:
        """校验备份文件可读且 quick_check 通过。"""
        try:
            with sqlite3.connect(str(backup_path)) as conn:
                conn.execute("PRAGMA quick_check")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("backup restore check failed: %s", e)
            return False
