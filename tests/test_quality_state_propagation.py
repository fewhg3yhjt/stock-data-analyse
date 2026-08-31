# -*- coding: utf-8 -*-
"""阶段二：质量结果与任务状态传播测试。

核心目标：阻止 Quality FAIL -> Job success -> Pipeline Publish 的错误流转。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from StockInvestmentTool.ops.job_runs import JobRunStore
from StockInvestmentTool.ops.task_execution import _publish, _quality
from StockInvestmentTool.ops.task_runner import TaskRunner
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.storage import Warehouse


def _candidate(warehouse: Warehouse, close: float = 10.2, duplicate: bool = False) -> str:
    path = warehouse.base_dir / "candidate.parquet"
    rows = [{"date": pd.Timestamp("2026-08-28"), "code": "sh600000", "open": 10.0,
             "high": 10.5, "low": 9.8, "close": close, "volume": 100.0, "amount": 1000.0}]
    if duplicate:
        rows.append(dict(rows[0]))
    pd.DataFrame(rows).to_parquet(path, index=False)
    build = {"version_id": "cand-v1", "partition": "2026-08", "path": path,
             "row_count": len(rows), "symbol_count": 1,
             "checksum": __import__("hashlib").sha256(path.read_bytes()).hexdigest()}
    state = PipelineState(warehouse.meta_db_path)
    return state.create_version(build, source_batches=[])


def test_quality_fail_result_maps_to_failed_job_status():
    assert JobRunStore.result_status({"rows": 2, "status": "FAIL", "publish_allowed": False}) == "failed"
    assert JobRunStore.result_status({"rows": 2, "status": "PASS", "publish_allowed": True}) == "success"
    assert JobRunStore.result_status({"rows": 2, "status": "WARNING", "publish_allowed": True}) == "partial_success"
    assert JobRunStore.result_status({"rows": 2, "status": "WARNING", "publish_allowed": False}) == "failed"


def test_publish_allowed_false_never_maps_to_success():
    assert JobRunStore.result_status({"rows": 100, "ok": True, "publish_allowed": False}) == "failed"
    assert JobRunStore.result_status({"rows": 100, "publish_allowed": False}) == "failed"


def test_ok_false_maps_to_failed_even_with_rows():
    assert JobRunStore.result_status({"rows": 100, "ok": False}) == "failed"


def test_explicit_status_wins_over_legacy_inference():
    assert JobRunStore.result_status({"rows": 0, "status": "failed", "failed_count": 0}) == "failed"
    assert JobRunStore.result_status({"rows": 5, "status": "partial_success"}) == "partial_success"


def test_quality_worker_returns_real_aggregate_not_fake_pass(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    version = _candidate(warehouse, duplicate=True)  # 重复主键 -> FAIL
    result = _quality(warehouse, {"input_versions": {"2026-08": version}})
    assert result["status"] == "FAIL"
    assert result["publish_allowed"] is False
    assert result["ok"] is False
    assert result["failed_count"] >= 1
    # 数据库中的质量结论必须同步为 FAIL
    with __import__("sqlite3").connect(warehouse.meta_db_path) as conn:
        status = conn.execute("SELECT quality_status FROM dataset_versions WHERE version_id=?", (version,)).fetchone()[0]
    assert status == "FAIL"


def test_quality_warning_is_not_disguised_as_pass(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    path = warehouse.base_dir / "candidate.parquet"
    frame = pd.DataFrame({"date": [pd.Timestamp("2026-08-27")] * 1000,
                          "code": [f"sh{i:06d}" for i in range(1000)],
                          "open": [10.0] * 1000, "high": [10.5] * 1000,
                          "low": [9.8] * 1000, "close": [10.2] * 1000,
                          "volume": [100.0] * 1000, "amount": [1000.0] * 1000})
    frame.to_parquet(path, index=False)
    build = {"version_id": "warn-v1", "partition": "2026-08", "path": path,
             "row_count": 1000, "symbol_count": 1000,
             "checksum": __import__("hashlib").sha256(path.read_bytes()).hexdigest()}
    state = PipelineState(warehouse.meta_db_path)
    version = state.create_version(build, source_batches=[])
    # 请求固化 2000 标的，实际只有 1000 -> coverage 不足 -> WARNING/FAIL，不可能是 PASS
    result = _quality(warehouse, {"input_versions": {"2026-08": version},
                                  "symbols": [f"sh{i:06d}" for i in range(2000)]})
    assert result["status"] in {"WARNING", "FAIL"}
    assert result["status"] != "PASS"


def test_publish_without_explicit_versions_fails(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    with pytest.raises(RuntimeError, match="input_versions"):
        _publish(warehouse, {})


def test_publish_rejects_unqualified_candidate(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    version = _candidate(warehouse)
    with __import__("sqlite3").connect(warehouse.meta_db_path) as conn:
        conn.execute("UPDATE dataset_versions SET quality_status='FAIL' WHERE version_id=?", (version,))
    with pytest.raises(RuntimeError, match="质量未通过"):
        _publish(warehouse, {"input_versions": {"2026-08": version}})


def test_publish_rejects_non_candidate_status(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse")
    version = _candidate(warehouse)
    with __import__("sqlite3").connect(warehouse.meta_db_path) as conn:
        conn.execute("UPDATE dataset_versions SET quality_status='PASS', publish_status='published' WHERE version_id=?", (version,))
    with pytest.raises(RuntimeError, match="不是 candidate"):
        _publish(warehouse, {"input_versions": {"2026-08": version}})


def test_pipeline_breaks_after_quality_fail(tmp_path):
    """Quality FAIL 后 Publish 不得执行（无历史补位、状态断链）。"""
    warehouse = Warehouse(tmp_path / "warehouse")
    runner = TaskRunner(tmp_path / "tasks.db", tmp_path / "tasks.db")
    runner.center.sync_definitions()
    state = PipelineState(tmp_path / "tasks.db")

    # 预置一个历史上 PASS 的 candidate（曾发布过的正式版本），
    # 验证本次失败后不会发布历史版本。
    good_path = tmp_path / "warehouse" / "good.parquet"
    pd.DataFrame({"date": [pd.Timestamp("2026-08-28")], "code": ["sh600000"],
                  "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2],
                  "volume": [100.0], "amount": [1000.0]}).to_parquet(good_path, index=False)
    good_build = {"version_id": "good-v1", "partition": "2026-08", "path": good_path,
                  "row_count": 1, "symbol_count": 1,
                  "checksum": __import__("hashlib").sha256(good_path.read_bytes()).hexdigest()}
    state.create_version(good_build, source_batches=[])
    state.quality("good-v1", status="PASS", checks={}, publish_allowed=True)

    def quality_worker(run_id, request):
        return {"rows": 1, "status": "FAIL", "publish_allowed": False, "failed_count": 1}

    def publish_worker(run_id, request):
        raise AssertionError("质量失败后 Publish 不应执行")

    result = runner.execute_pipeline(
        [("stock_daily_quality", quality_worker), ("stock_daily_publish", publish_worker)],
        period_start="2026-08-01", period_end="2026-08-28",
    )
    assert result["status"] == "failed"
    assert len(result["runs"]) == 1  # 只有 quality，publish 未执行
    assert result["runs"][0]["status"] == "failed"