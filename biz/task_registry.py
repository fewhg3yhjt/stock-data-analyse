# -*- coding: utf-8 -*-
"""业务任务定义与 handler 注册。

数据采集任务不在这里注册；本模块只负责 business.db 业务任务。
"""

from __future__ import annotations

from typing import Callable

from StockInvestmentTool.biz.reporting import SystemAlertService
from StockInvestmentTool.biz.tasks import BusinessTaskDefinition, BusinessTaskService, register_task


BUSINESS_TASK_DEFINITIONS = (
    ("screen.run", "执行选股", "筛选并保存 ScreenRun/ScreenCandidate"),
    ("research.run", "执行研究", "执行并保存 ResearchRun/StrategyDecision"),
    ("simulation.run", "执行模拟", "执行并保存 SimulationRun/Fill/Result"),
    ("parameter_search.run", "参数搜索", "执行多组模拟参数搜索"),
    ("report.daily_generate", "生成日报", "生成结构化 DailyReport"),
    ("observation.expiry_reconcile", "观察过期检查", "推进过期 Observation"),
    ("advice.refresh", "刷新建议", "刷新持仓建议"),
    ("notification.outbox_delivery", "投递通知", "领取并投递 Outbox"),
    ("health.reconcile", "健康检查", "记录系统健康状态"),
)


def register_business_tasks(service: BusinessTaskService, handlers: dict[str, Callable] | None = None) -> None:
    """幂等注册全部业务任务及 handler。"""
    handlers = handlers or {}
    for task_key, name, description in BUSINESS_TASK_DEFINITIONS:
        service.register_definition(BusinessTaskDefinition(
            task_key=task_key, name=name, description=description,
            input_schema={}, result_schema={}, enabled=True,
        ))
        handler = handlers.get(task_key) or _default_handler(task_key, service)
        register_task(task_key, handler)


def _default_handler(task_key: str, service: BusinessTaskService) -> Callable:
    if task_key == "health.reconcile":
        def health_handler(input_data: dict) -> dict:
            return {"status": "success", "output_versions": {"health": "checked"}}
        return health_handler

    if task_key == "observation.expiry_reconcile":
        def expiry_handler(input_data: dict) -> dict:
            from StockInvestmentTool.biz.observation import ObservationService
            observations = ObservationService(service.repo).list_active_observations()
            events = ObservationService(service.repo).reconcile_expiry(observations)
            for obs, event in zip((o for o in observations if o.status == "expired"), events):
                ObservationService(service.repo).update_observation(obs)
                ObservationService(service.repo).save_event(obs.observation_id, event)
            return {"status": "success", "output_versions": {"expired": len(events)}}
        return expiry_handler

    def not_implemented_handler(input_data: dict) -> dict:
        raise NotImplementedError(f"业务任务尚未接入完整 handler: {task_key}")
    return not_implemented_handler
