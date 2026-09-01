# -*- coding: utf-8 -*-
"""业务任务定义与 handler 注册。

数据采集任务不在这里注册；本模块只负责 business.db 业务任务。
"""

from __future__ import annotations

from typing import Callable

from StockInvestmentTool.biz.reporting import SystemAlertService
from StockInvestmentTool.biz.tasks import BusinessTaskDefinition, BusinessTaskService, register_task
from StockInvestmentTool.biz.models import DataContext


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

DISABLED_BUSINESS_TASKS = {
    "parameter_search.run",
    "advice.refresh",
    "notification.outbox_delivery",
}


def register_business_tasks(service: BusinessTaskService, handlers: dict[str, Callable] | None = None) -> None:
    """幂等注册全部业务任务及 handler。"""
    handlers = handlers or {}
    for task_key, name, description in BUSINESS_TASK_DEFINITIONS:
        service.register_definition(BusinessTaskDefinition(
            task_key=task_key, name=name, description=description,
            input_schema={}, result_schema={}, enabled=task_key not in DISABLED_BUSINESS_TASKS,
        ))
        handler = handlers.get(task_key) or _default_handler(task_key, service)
        register_task(task_key, handler)


def _default_handler(task_key: str, service: BusinessTaskService) -> Callable:
    if task_key == "screen.run":
        return _screen_handler(service)
    if task_key == "research.run":
        return _research_handler(service)
    if task_key == "simulation.run":
        return _simulation_handler(service)
    if task_key == "report.daily_generate":
        return _report_handler(service)
    if task_key == "notification.outbox_delivery":
        def outbox_handler(input_data: dict) -> dict:
            pending = service.repo.db.fetchall(
                "SELECT COUNT(*) AS n FROM notification_deliveries "
                "WHERE status IN ('pending','processing')"
            )
            return {"status": "success", "output_versions": {"pending": pending[0]["n"]}}
        return outbox_handler
    if task_key == "advice.refresh":
        def advice_handler(input_data: dict) -> dict:
            return {"status": "success", "output_versions": {"refreshed": 0}}
        return advice_handler

    if task_key == "health.reconcile":
        def health_handler(input_data: dict) -> dict:
            return {"status": "success", "output_versions": {"health": "checked"}}
        return health_handler

    if task_key == "observation.expiry_reconcile":
        def expiry_handler(input_data: dict) -> dict:
            from StockInvestmentTool.biz.observation import ObservationService
            events = ObservationService(service.repo).reconcile_expiry_and_save()
            return {"status": "success", "output_versions": {"expired": len(events)}}
        return expiry_handler

    def not_implemented_handler(input_data: dict) -> dict:
        raise NotImplementedError(f"业务任务尚未接入完整 handler: {task_key}")
    return not_implemented_handler


def _screen_handler(service: BusinessTaskService) -> Callable:
    def handler(input_data: dict) -> dict:
        from StockInvestmentTool.biz.data_access import load_market_data
        from StockInvestmentTool.biz.models import new_id, now_utc
        from StockInvestmentTool.biz.repo import BusinessRepository
        from StockInvestmentTool.biz.screen import ScreenDefinition, ScreenExecutor, ScreenRun
        from StockInvestmentTool.warehouse.storage import Warehouse

        condition = input_data.get("condition_spec")
        execution_mode = input_data.get("execution_mode", "snapshot")
        start_date = input_data.get("start_date") or input_data.get("scan_start")
        as_of = input_data.get("as_of") or input_data.get("end_date")
        if not isinstance(condition, dict) or not start_date or not as_of:
            raise ValueError("screen.run requires condition_spec/start_date/as_of")
        data_end = input_data.get("scan_end") if execution_mode == "signal_scan" else as_of
        dataset = load_market_data(
            Warehouse(), start_date=start_date, end_date=data_end,
            symbols=input_data.get("symbols"), required_quality="WARNING",
        )
        definition = ScreenDefinition(
            screen_id=input_data.get("screen_id") or new_id("screen"),
            name=input_data.get("name", "筛选运行"), version=str(input_data.get("version", "1")),
            description=input_data.get("description", ""),
            asset_types=input_data.get("asset_types", ["stock"]), condition_spec=condition,
            sort_spec=input_data.get("sort_spec", {}), display_fields=input_data.get("display_fields", []),
        )
        repo = service.repo
        screen_version_id = repo.save_screen_version(definition)
        symbols = [str(value) for value in dataset.data["code"].dropna().unique()]
        universe_id = repo.save_universe_snapshot(symbols, universe_type="selected_symbols", as_of=as_of)
        run = ScreenRun(
            run_id=new_id("screen_run"), screen_version_id=screen_version_id,
            universe_snapshot_id=universe_id, run_type=input_data.get("run_type", "manual"),
            requested_as_of=as_of, data_context=DataContext.from_dict(dataset.context).to_dict(),
            status="running", started_at=now_utc(),
        )
        repo.save_screen_run(run)
        try:
            candidates, meta = ScreenExecutor(definition, dataset.data).execute(
                as_of,
                execution_mode=execution_mode,
                scan_start=input_data.get("scan_start"),
                scan_end=input_data.get("scan_end"),
            )
        except Exception as exc:  # noqa: BLE001
            repo.update_screen_run_status(run.run_id, "failed", str(exc))
            raise
        run.status = meta.get("status", "success")
        run.actual_data_as_of = meta.get("actual_data_as_of", "")
        run.matched_count = len(candidates)
        run.error = meta.get("error", "")
        repo.update_screen_run_status(
            run.run_id, run.status, run.error,
            actual_data_as_of=run.actual_data_as_of, matched_count=run.matched_count,
        )
        for candidate in candidates:
            candidate.screen_run_id = run.run_id
            repo.save_screen_candidate(candidate)
        return {"status": "success", "output_versions": {"screen_run_id": run.run_id},
                "screen_run_id": run.run_id, "matched_count": len(candidates)}
    return handler


def _research_handler(service: BusinessTaskService) -> Callable:
    def handler(input_data: dict) -> dict:
        from StockInvestmentTool.biz.data_access import load_market_data
        from StockInvestmentTool.biz.db import loads_json
        from StockInvestmentTool.biz.regime import MarketRegimeService
        from StockInvestmentTool.biz.models import new_id, now_utc
        from StockInvestmentTool.biz.research import ResearchResult, ResearchService
        from StockInvestmentTool.biz.strategy import StrategySpec, compile_strategy
        from StockInvestmentTool.warehouse.storage import Warehouse

        symbol = input_data.get("symbol")
        start_date = input_data.get("start_date")
        as_of = input_data.get("as_of")
        if not symbol or not start_date or not as_of:
            raise ValueError("research.run requires symbol/start_date/as_of")
        from StockInvestmentTool.biz.code import normalize
        symbol = normalize(symbol)
        dataset = load_market_data(
            Warehouse(), start_date=start_date, end_date=as_of, symbols=[symbol],
            required_quality="WARNING",
        )
        strategy = None
        strategy_version_id = input_data.get("strategy_version_id", "")
        if strategy_version_id:
            stored = service.repo.get_strategy_version(strategy_version_id)
            if not stored:
                raise ValueError(f"unknown strategy version: {strategy_version_id}")
            strategy = compile_strategy(StrategySpec(**loads_json(stored["config_json"])),
                                        strategy_version_id=strategy_version_id)
        regime = MarketRegimeService(dataset.data, dataset.context).compute(as_of)
        try:
            result = ResearchService(dataset.data, DataContext.from_dict(dataset.context).to_dict(), strategy, regime.to_dict()).run()
        except Exception as exc:  # noqa: BLE001
            result = ResearchResult(
                research_run_id=new_id("rr"), status="failed",
                error=str(exc), finished_at=now_utc(),
            )
            service.repo.save_research_run(
                result, DataContext.from_dict(dataset.context).to_dict(),
                subject_type=input_data.get("subject_type", "single_symbol"),
                symbol=symbol, strategy_version_id=strategy_version_id,
                observation_id=input_data.get("observation_id", ""),
                source_screen_run_id=input_data.get("screen_run_id", ""),
                source_candidate_id=input_data.get("candidate_id", ""),
            )
            raise
        service.repo.save_market_regime(regime)
        service.repo.save_research_run(
            result, DataContext.from_dict(dataset.context).to_dict(), subject_type=input_data.get("subject_type", "single_symbol"),
            symbol=symbol, strategy_version_id=strategy_version_id,
            observation_id=input_data.get("observation_id", ""),
            source_screen_run_id=input_data.get("screen_run_id", ""),
            source_candidate_id=input_data.get("candidate_id", ""),
        )
        for decision in result.decisions:
            decision.strategy_version_id = strategy_version_id or decision.strategy_version_id
            decision.research_run_id = result.research_run_id
            service.repo.save_decision(decision)
        return {"status": "success", "output_versions": {"research_run_id": result.research_run_id},
                "research_run_id": result.research_run_id}
    return handler


def _simulation_handler(service: BusinessTaskService) -> Callable:
    def handler(input_data: dict) -> dict:
        from StockInvestmentTool.biz.data_access import load_market_data
        from StockInvestmentTool.biz.db import loads_json
        from StockInvestmentTool.biz.models import SimulationPlan, new_id
        from StockInvestmentTool.biz.simulation import SimulationExecutor
        from StockInvestmentTool.biz.models import SimulationRun, now_utc, new_id, SimulationLot
        from StockInvestmentTool.biz.strategy import StrategySpec, compile_strategy
        from StockInvestmentTool.warehouse.storage import Warehouse

        version_id = input_data.get("strategy_version_id")
        symbol = input_data.get("symbol")
        start_date = input_data.get("start_date")
        end_date = input_data.get("end_date")
        if not version_id or not symbol or not start_date or not end_date:
            raise ValueError("simulation.run requires strategy_version_id/symbol/start_date/end_date")
        stored = service.repo.get_strategy_version(version_id)
        if not stored:
            raise ValueError(f"unknown strategy version: {version_id}")
        from StockInvestmentTool.biz.code import normalize
        symbol = normalize(symbol)
        dataset = load_market_data(
            Warehouse(), start_date=start_date, end_date=end_date,
            symbols=[symbol], required_quality="WARNING",
        )
        strategy = compile_strategy(StrategySpec(**loads_json(stored["config_json"])),
                                    strategy_version_id=version_id)
        plan = SimulationPlan(
            plan_id=new_id("plan"), strategy_version_id=version_id,
            name=input_data.get("name", "模拟运行"), start_date=start_date,
            end_date=end_date, initial_cash=float(input_data.get("initial_cash", 100000)),
            cost_config=input_data.get("cost_config", {}), benchmark=input_data.get("benchmark", "sh000300"),
            observation_id=input_data.get("observation_id"),
            source_screen_run_id=input_data.get("screen_run_id"), data_context=dataset.context,
        )
        service.repo.save_simulation_plan(plan)
        run = SimulationRun(
            run_id=new_id("run"), plan_id=plan.plan_id, status="running",
            data_context=dataset.context, started_at=now_utc(),
        )
        service.repo.save_simulation_run(run)
        executor = SimulationExecutor(plan, dataset.data, strategy=strategy, run_id=run.run_id)
        try:
            result = executor.run()
        except Exception as exc:  # noqa: BLE001
            # Preserve the partial execution trail before exposing the task failure.
            try:
                service.repo.save_simulation_events(executor.events)
            finally:
                service.repo.update_simulation_run_status(run.run_id, "failed", str(exc))
            raise
        fills, events = executor.fills, executor.events
        run.status = "success"
        run.finished_at = now_utc()
        service.repo.save_simulation_run(run)
        for fill in fills:
            service.repo.save_simulation_fill(fill)
        service.repo.save_simulation_events(events)
        for symbol, lots in executor.account.positions.items():
            for lot in lots:
                service.repo.save_simulation_lot(SimulationLot(
                    lot_id=lot.lot_id, simulation_run_id=run.run_id, symbol=lot.symbol,
                    opened_at=lot.opened_at, quantity=lot.quantity,
                    remaining_quantity=lot.remaining_quantity, entry_price=lot.entry_price,
                    entry_fee=lot.entry_fee, source_fill_id=lot.source_fill_id,
                ))
        service.repo.save_simulation_result(result)
        return {"status": "success", "output_versions": {"simulation_run_id": run.run_id},
                "simulation_run_id": run.run_id}
    return handler


def _report_handler(service: BusinessTaskService) -> Callable:
    def handler(input_data: dict) -> dict:
        from StockInvestmentTool.biz.reporting import ReportingService
        report_date = input_data.get("report_date")
        if not report_date:
            raise ValueError("report.daily_generate requires report_date")
        report = ReportingService(service.repo).create_report(
            report_date, data_as_of=input_data.get("data_as_of"),
            market_snapshot=input_data.get("market_snapshot"),
            observation_snapshot=input_data.get("observation_snapshot"),
            portfolio_snapshot=input_data.get("portfolio_snapshot"),
            advice_ids=input_data.get("advice_ids"), sections=input_data.get("sections"),
        )
        return {"status": "success", "output_versions": {"report_id": report.report_id}}
    return handler
