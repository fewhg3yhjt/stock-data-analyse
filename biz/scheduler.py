# -*- coding: utf-8 -*-
"""业务平面调度适配器。

与数据平面的 scheduler 解耦：调度只创建 BusinessRequest/JobRun，实际执行
由 BusinessTaskService.run_next() 完成。这里不采集数据、不直接运行数据任务。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.task_registry import register_business_tasks
from StockInvestmentTool.biz.tasks import BusinessTaskService

logger = logging.getLogger(__name__)

TZ = "Asia/Shanghai"


def _tz() -> str:
    return TZ


@dataclass
class BusinessSchedule:
    task_key: str
    trigger: Any
    enabled: bool = True


class BusinessScheduler:
    """业务任务的 APScheduler 适配器。"""

    def __init__(self, scheduler: Any, repo: BusinessRepository | None = None):
        self.scheduler = scheduler
        self.service = BusinessTaskService(repo or BusinessRepository())
        register_business_tasks(self.service)
        self.schedules: dict[str, BusinessSchedule] = {}

    def register_interval(self, task_key: str, *, minutes: int, input_data: dict | None = None) -> None:
        """按分钟调度业务任务；调度回调只入队，不在调度线程执行任务。"""
        from apscheduler.triggers.interval import IntervalTrigger

        trigger = IntervalTrigger(minutes=minutes)
        self.scheduler.add_job(
            self.enqueue, trigger=trigger, id=f"biz:{task_key}", replace_existing=True,
            kwargs={"task_key": task_key, "input_data": input_data or {}},
            max_instances=1, coalesce=True, misfire_grace_time=300,
        )
        self.schedules[task_key] = BusinessSchedule(task_key, trigger)

    def register_cron(self, task_key: str, *, hour: int, minute: int,
                      day_of_week: str = "mon-fri", input_data: dict | None = None) -> None:
        """按 cron 调度业务任务（收盘后/盘中时段）。"""
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger(day_of_week=day_of_week, hour=hour, minute=minute, timezone=_tz())
        self.scheduler.add_job(
            self.enqueue, trigger=trigger, id=f"biz:{task_key}", replace_existing=True,
            kwargs={"task_key": task_key, "input_data": input_data or {}},
            max_instances=1, coalesce=True, misfire_grace_time=3600,
        )
        self.schedules[task_key] = BusinessSchedule(task_key, trigger)

    def enqueue(self, task_key: str, input_data: dict | None = None) -> str:
        request = self.service.enqueue(task_key, trigger_type="scheduled", input_data=input_data or {})
        self.service.create_run_for_request(request.request_id)
        return request.request_id

    def run_one(self):
        """由独立 Worker 调用，执行一个已持久化的业务运行。"""
        return self.service.run_next()

    def registered(self) -> list[dict]:
        jobs = {getattr(job, "id", "") for job in self.scheduler.get_jobs()}
        return [
            {"task_key": key, "enabled": item.enabled, "registered": f"biz:{key}" in jobs}
            for key, item in sorted(self.schedules.items())
        ]
