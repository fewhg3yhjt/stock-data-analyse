# -*- coding: utf-8 -*-
"""阶段四：统一 Scheduler 与任务配置事实源。

验证 APScheduler 的数据任务按 management.db Active Config 注册/重载，
而不是直接读 YAML。
"""

from __future__ import annotations

import json

import pytest

from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.web.scheduler import _reload_data_scheduler_jobs, _schedule_configured_data_tasks

apscheduler = pytest.importorskip("apscheduler")


def _make_app(tmp_path):
    from apscheduler.schedulers.background import BackgroundScheduler
    from StockInvestmentTool.web.scheduler import TZ

    sched = BackgroundScheduler(timezone=TZ)
    app = type("App", (), {})()
    app.extensions = {"scheduler": sched}
    center = TaskCenter(tmp_path / "runs.db")
    center.sync_definitions()
    return app, sched, center


def test_registered_jobs_follow_active_config(tmp_path, monkeypatch):
    app, sched, center = _make_app(tmp_path)
    monkeypatch.setattr("StockInvestmentTool.ops.task_center.management_db_path", lambda *a, **k: tmp_path / "runs.db")
    _schedule_configured_data_tasks(sched)
    ids = {job.id for job in sched.get_jobs()}
    # trading_day 频率且有 time 的任务应注册为 task:<key>
    assert "task:stock_daily_capture" in ids
    # 默认关闭的任务不注册
    assert "task:money_flow_capture" not in ids
    # after_upstream/manual 不独立注册（由上游串联）
    assert "task:stock_daily_build" not in ids
    assert "task:stock_daily_publish" not in ids
    assert "task:indicators_build" not in ids


def test_disable_removes_job_and_enable_readds(tmp_path, monkeypatch):
    app, sched, center = _make_app(tmp_path)
    monkeypatch.setattr("StockInvestmentTool.ops.task_center.management_db_path", lambda *a, **k: tmp_path / "runs.db")
    _schedule_configured_data_tasks(sched)
    assert any(job.id == "task:stock_daily_capture" for job in sched.get_jobs())

    # 停用 → 重载后移除
    center.set_task_enabled("stock_daily_capture", False)
    _reload_data_scheduler_jobs(app)
    assert not any(job.id == "task:stock_daily_capture" for job in sched.get_jobs())

    # 启用 → 重载后重新注册
    center.set_task_enabled("stock_daily_capture", True)
    _reload_data_scheduler_jobs(app)
    assert any(job.id == "task:stock_daily_capture" for job in sched.get_jobs())


def test_reload_does_not_touch_non_data_jobs(tmp_path, monkeypatch):
    from apscheduler.triggers.cron import CronTrigger
    from StockInvestmentTool.web.scheduler import TZ

    app, sched, center = _make_app(tmp_path)
    monkeypatch.setattr("StockInvestmentTool.ops.task_center.management_db_path", lambda *a, **k: tmp_path / "runs.db")

    def marker():
        pass

    sched.add_job(marker, CronTrigger(minute="*/5", timezone=TZ),
                  id="notification_outbox", coalesce=True, max_instances=1)
    _schedule_configured_data_tasks(sched)
    before = {job.id for job in sched.get_jobs()}
    assert "notification_outbox" in before

    center.set_task_enabled("stock_daily_capture", False)
    _reload_data_scheduler_jobs(app)
    after = {job.id for job in sched.get_jobs()}
    assert "notification_outbox" in after
    assert after < before  # 只有数据任务被移除


def test_draft_change_does_not_reschedule(tmp_path, monkeypatch):
    app, sched, center = _make_app(tmp_path)
    monkeypatch.setattr("StockInvestmentTool.ops.task_center.management_db_path", lambda *a, **k: tmp_path / "runs.db")
    _schedule_configured_data_tasks(sched)
    job = next(job for job in sched.get_jobs() if job.id == "task:stock_daily_capture")
    old = str(job.trigger)

    config = json.loads(center.task("stock_daily_capture")["config_versions"][0]["config"])
    config["schedule"]["time"] = "09:05"
    center.save_task_config("stock_daily_capture", config, activate=False)
    _reload_data_scheduler_jobs(app)
    job = next(job for job in sched.get_jobs() if job.id == "task:stock_daily_capture")
    assert str(job.trigger) == old

    version = center.save_task_config("stock_daily_capture", config, activate=True)
    _reload_data_scheduler_jobs(app)
    job = next(job for job in sched.get_jobs() if job.id == "task:stock_daily_capture")
    assert str(job.trigger) != old