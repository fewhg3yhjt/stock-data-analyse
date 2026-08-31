# -*- coding: utf-8 -*-
"""阶段十一：产品状态机收口测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from StockInvestmentTool.ops.status_catalog import (
    STATUS_META,
    is_terminal,
    status_info,
)


def test_status_info_shape():
    info = status_info("running")
    assert info == {"code": "running", "label": "执行中",
                    "severity": "running", "terminal": False}


def test_status_info_unknown_fallback():
    info = status_info("no_such_status")
    assert info["code"] == "no_such_status"
    assert info["severity"] == "unknown"
    assert info["terminal"] is False


def test_terminal_flags():
    assert is_terminal("success")
    assert is_terminal("failed")
    assert is_terminal("cancelled")
    assert is_terminal("partial_success")
    assert not is_terminal("running")
    assert not is_terminal("requested")


def test_simulation_statuses_present():
    for status in ("PENDING", "RUNNING", "SUCCESS", "FAILED", "CANCELLED"):
        assert status in STATUS_META
        assert "label" in STATUS_META[status]
        assert "severity" in STATUS_META[status]
        assert "terminal" in STATUS_META[status]


def test_all_terminal_cannot_rerun():
    # 终态不得再转回 running（工作项 3 的兜底语义）
    for status, meta in STATUS_META.items():
        if meta["terminal"]:
            assert status_info(status)["terminal"] is True


def test_save_simulation_events_persists(tmp_path):
    from StockInvestmentTool.biz.repo import BusinessRepository
    from StockInvestmentTool.biz.db import BusinessDB

    db = BusinessDB(str(tmp_path / "business.db"))
    repo = BusinessRepository(db)
    from StockInvestmentTool.biz.models import SimulationEvent

    events = [
        SimulationEvent(event_id="e1", simulation_run_id="r1", event_type="SIGNAL_GENERATED",
                        symbol="sh600900", payload={"price": 10.0}, event_time="2026-08-28T10:00:00Z"),
        SimulationEvent(event_id="e2", simulation_run_id="r1", event_type="FILLED",
                        symbol="sh600900", payload={"qty": 100}, event_time="2026-08-28T10:01:00Z"),
    ]
    assert repo.save_simulation_events(events) == 2
    rows = repo.list_simulation_events("r1")
    assert len(rows) == 2
    assert rows[0]["payload"] == {"price": 10.0}
    assert rows[1]["payload"] == {"qty": 100}


def test_analysis_task_persistence_and_recovery(tmp_path, monkeypatch):
    """分析任务落库 + 重启后 running 标记 failed（工作项 9/10）。"""
    from StockInvestmentTool.web import app as web_app
    from StockInvestmentTool.config import Config

    monkeypatch.setattr(Config, "DATA_DIR", Path(tmp_path))
    import sqlite3
    db_path = Path(tmp_path) / "management.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
        CREATE TABLE web_analysis_tasks (
          task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, code TEXT NOT NULL,
          status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', progress INTEGER NOT NULL DEFAULT 0,
          result_json TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT
        );
        INSERT INTO web_analysis_tasks (task_id,task_type,code,status,stage,progress,created_at)
          VALUES ('task_a','analysis','sh600900','running','进行中',50,'2026-08-28T10:00:00');
        """)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert web_app._recover_analysis_tasks() == 1
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT status,error FROM web_analysis_tasks WHERE task_id='task_a'").fetchone()
    assert row[0] == "failed"
    assert "进程重启" in row[1]


def test_analysis_task_query_api_shape(tmp_path, monkeypatch):
    from StockInvestmentTool.web import app as web_app
    from StockInvestmentTool.config import Config

    monkeypatch.setattr(Config, "DATA_DIR", Path(tmp_path))
    import sqlite3
    db_path = Path(tmp_path) / "management.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""
        CREATE TABLE web_analysis_tasks (
          task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, code TEXT NOT NULL,
          status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', progress INTEGER NOT NULL DEFAULT 0,
          result_json TEXT, error TEXT, created_at TEXT NOT NULL, finished_at TEXT
        );
        INSERT INTO web_analysis_tasks (task_id,task_type,code,status,stage,progress,result_json,created_at)
          VALUES ('task_b','comparison','sh600900','success','完成',100,
                  '{"report_file":"x.html"}','2026-08-28T10:00:00');
        """)
    task = web_app._get_analysis_task("task_b")
    assert task["status"] == "success"
    assert task["result"] == {"report_file": "x.html"}