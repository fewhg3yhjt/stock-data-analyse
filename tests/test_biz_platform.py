# -*- coding: utf-8 -*-
"""biz 包单元测试：平台治理（Health / Backup / 错误结构）。"""

import sqlite3

import pytest

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.platform import (
    ApiError,
    BackupService,
    HealthService,
    error_response,
    success_response,
)


class TestApiError:
    def test_success(self):
        assert success_response({"a": 1}, "req1") == {"data": {"a": 1}, "request_id": "req1"}

    def test_error_structure(self):
        err = ApiError("TASK_CONFLICT", "任务正在运行", retryable=True, details={"run_id": "r1"})
        out = error_response(err, "req1")
        assert out["error"]["code"] == "TASK_CONFLICT"
        assert out["error"]["retryable"] is True
        assert out["request_id"] == "req1"
        assert "message" in out["error"]


class TestHealth:
    def test_live(self, tmp_path):
        hs = HealthService(BusinessDB(tmp_path / "h.db"))
        assert hs.live()["status"] == "ok"

    def test_ready_with_business_db(self, tmp_path):
        hs = HealthService(BusinessDB(tmp_path / "h.db"), management_db_path="")
        ready = hs.ready()
        assert ready["status"] in {"ok", "degraded"}
        names = {c["name"] for c in ready["components"]}
        assert "business.db" in names
        # management.db 未配置 → disabled
        mgmt = next(c for c in ready["components"] if c["name"] == "management.db")
        assert mgmt["status"] == "disabled"

    def test_ready_business_db_unhealthy(self, tmp_path):
        BusinessDB(tmp_path / "h.db")
        # 独立坏的 management.db 文件
        bad_mgmt = tmp_path / "bad_mgmt.db"
        bad_mgmt.write_text("not a sqlite db")
        hs = HealthService(BusinessDB(tmp_path / "h.db"), management_db_path=str(bad_mgmt))
        ready = hs.ready()
        mgmt = next(c for c in ready["components"] if c["name"] == "management.db")
        assert mgmt["status"] == "unhealthy"
        assert mgmt["blocking"] is True

    def test_details(self, tmp_path):
        hs = HealthService(BusinessDB(tmp_path / "h.db"))
        d = hs.details()
        assert "business_db" in d
        assert d["status"] in {"ok", "degraded"}


class TestBackup:
    def test_backup_and_restore(self, tmp_path):
        db = BusinessDB(tmp_path / "src.db")
        db.insert("strategies", {"strategy_id": "s1", "name": "n", "status": "draft",
                                 "current_version_id": "", "created_at": "t", "updated_at": "t"})
        backup_root = tmp_path.parent / "backups_x"  # 数据目录之外
        svc = BackupService(backup_root)
        dest = svc.backup_sqlite(db.db_path, "business")
        assert dest.exists()
        cs = svc.checksum(dest)
        assert len(cs) == 64
        assert svc.restore_check(dest) is True
        # 读回备份内容
        with sqlite3.connect(str(dest)) as conn:
            row = conn.execute("SELECT name FROM strategies WHERE strategy_id='s1'").fetchone()
        assert row[0] == "n"

    def test_backup_set_manifest(self, tmp_path):
        db = BusinessDB(tmp_path / "src2.db")
        backup_root = tmp_path.parent / "b2_x"
        svc = BackupService(backup_root)
        result = svc.backup_set({"business.db": db.db_path})
        assert len(result["entries"]) == 1
        manifest = backup_root / "manifest.json"
        assert manifest.exists()
        assert result["entries"][0]["checksum"]

    def test_backup_dir_inside_data_rejected(self, tmp_path):
        db = BusinessDB(tmp_path / "data" / "src.db")
        # 备份目录放在 data 内部 → 拒绝
        svc = BackupService(tmp_path / "data" / "backups")
        with pytest.raises(ValueError):
            svc.backup_sqlite(db.db_path, "business")