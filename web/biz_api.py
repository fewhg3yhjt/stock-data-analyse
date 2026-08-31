# -*- coding: utf-8 -*-
"""新业务平面 API。

本蓝图只负责 HTTP DTO 和应用服务调用，不直接读取 Parquet、选择数据源或
操作旧 portfolio/meta/job_runs 业务表。数据输入由 DatasetAccess 提供。
"""

from __future__ import annotations

import os
from datetime import datetime

import flask

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.models import DataContext, StrategyContext, SimulationPlan, new_id
from StockInvestmentTool.biz.observation import ObservationService
from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.screen import ScreenDefinition, ScreenExecutor
from StockInvestmentTool.biz.workflow import BusinessWorkflowService, WorkflowError
from StockInvestmentTool.biz.research import ResearchService
from StockInvestmentTool.biz.regime import MarketRegimeService
from StockInvestmentTool.biz.simulation import execute_simulation
from StockInvestmentTool.biz.strategy import StrategySpec, compile_strategy
from StockInvestmentTool.biz.data_access import load_market_data

biz_api = flask.Blueprint("biz_api", __name__, url_prefix="/api/biz")


def _repo() -> BusinessRepository:
    return BusinessRepository(BusinessDB())


def _error(code: str, message: str, status: int = 400):
    return flask.jsonify({
        "error": {"code": code, "message": message, "retryable": False, "details": {}},
        "request_id": flask.request.headers.get("X-Request-ID", ""),
    }), status


@biz_api.post("/screens/preview")
def preview_screen():
    """用已加载数据预览筛选，不创建运行记录。"""
    payload = flask.request.get_json(silent=True) or {}
    condition = payload.get("condition_spec")
    if not payload.get("start_date") or not payload.get("end_date"):
        return _error("SCREEN_INVALID", "start_date 和 end_date 必填")
    if not isinstance(condition, dict):
        return _error("SCREEN_INVALID", "condition_spec 必须是对象")
    try:
        from StockInvestmentTool.warehouse.storage import Warehouse
        data = load_market_data(
            Warehouse(), start_date=payload.get("start_date"),
            end_date=payload.get("end_date"), symbols=payload.get("symbols"),
            required_quality="WARNING",
        )
        definition = ScreenDefinition(
            screen_id=payload.get("screen_id", "preview"), name=payload.get("name", "预览"),
            condition_spec=condition, sort_spec=payload.get("sort_spec", {}),
        )
        candidates, meta = ScreenExecutor(definition, data.data).execute(
            payload.get("as_of") or payload.get("end_date") or datetime.utcnow().strftime("%Y-%m-%d")
        )
        return flask.jsonify({"data": {
            "candidates": [c.__dict__ for c in candidates], "meta": meta,
            "data_context": data.context,
        }, "request_id": flask.request.headers.get("X-Request-ID", "")})
    except Exception as exc:  # noqa: BLE001
        return _error("SCREEN_RUN_FAILED", str(exc), 500)


@biz_api.post("/screen-runs")
def create_screen_run():
    """执行并持久化一次正式筛选运行。"""
    payload = flask.request.get_json(silent=True) or {}
    condition = payload.get("condition_spec")
    as_of = payload.get("as_of") or payload.get("end_date")
    if not isinstance(condition, dict) or not as_of or not payload.get("start_date"):
        return _error("SCREEN_INVALID", "condition_spec 和 as_of 必填")
    try:
        from StockInvestmentTool.warehouse.storage import Warehouse
        dataset = load_market_data(
            Warehouse(), start_date=payload.get("start_date"),
            end_date=as_of, symbols=payload.get("symbols"),
            required_quality="WARNING",
        )
        definition = ScreenDefinition(
            screen_id=payload.get("screen_id") or new_id("screen"),
            name=payload.get("name", "筛选运行"), version=str(payload.get("version", "1")),
            condition_spec=condition, sort_spec=payload.get("sort_spec", {}),
            display_fields=payload.get("display_fields", []),
        )
        repo = _repo()
        screen_version_id = repo.save_screen_version(definition)
        symbols = [str(x) for x in dataset.data["code"].dropna().unique()] if "code" in dataset.data else []
        universe_id = repo.save_universe_snapshot(symbols, universe_type="selected_symbols", as_of=as_of)
        candidates, meta = ScreenExecutor(definition, dataset.data).execute(as_of)
        run = __import__("StockInvestmentTool.biz.screen", fromlist=["ScreenRun"]).ScreenRun(
            run_id=new_id("screen_run"), screen_version_id=screen_version_id,
            universe_snapshot_id=universe_id, run_type=payload.get("run_type", "manual"),
            requested_as_of=as_of, actual_data_as_of=meta.get("actual_data_as_of", ""),
            data_context=dataset.context, status=meta.get("status", "success"),
            matched_count=len(candidates), started_at=meta.get("started_at", ""),
            finished_at=meta.get("finished_at", ""), error=meta.get("error", ""),
        )
        repo.save_screen_run(run)
        for candidate in candidates:
            candidate.screen_run_id = run.run_id
            repo.save_screen_candidate(candidate)
        return flask.jsonify({"data": {
            "run": run.__dict__,
            "candidates": [c.__dict__ for c in candidates],
            "data_context": dataset.context,
        }, "request_id": flask.request.headers.get("X-Request-ID", "")}), 201
    except Exception as exc:  # noqa: BLE001
        return _error("SCREEN_RUN_FAILED", str(exc), 500)


@biz_api.get("/screen-runs/<run_id>")
def get_screen_run(run_id: str):
    repo = _repo()
    row = repo.db.fetchone("SELECT * FROM screen_runs WHERE screen_run_id=?", (run_id,))
    if not row:
        return _error("SCREEN_RUN_NOT_FOUND", "筛选运行不存在", 404)
    run = dict(row)
    run["data_context"] = __import__("StockInvestmentTool.biz.db", fromlist=["loads_json"]).loads_json(run.pop("data_context_json"))
    run["candidates"] = repo.list_candidates(run_id)
    return flask.jsonify({"data": run, "request_id": flask.request.headers.get("X-Request-ID", "")})


@biz_api.get("/screen-runs/<run_id>/candidates")
def get_screen_candidates(run_id: str):
    repo = _repo()
    if not repo.db.fetchone("SELECT screen_run_id FROM screen_runs WHERE screen_run_id=?", (run_id,)):
        return _error("SCREEN_RUN_NOT_FOUND", "筛选运行不存在", 404)
    return flask.jsonify({"data": {"run_id": run_id, "candidates": repo.list_candidates(run_id)},
                          "request_id": flask.request.headers.get("X-Request-ID", "")})


@biz_api.post("/research-runs")
def create_research_run():
    """执行并持久化一次研究运行。"""
    payload = flask.request.get_json(silent=True) or {}
    symbol = payload.get("symbol")
    if not symbol or not payload.get("start_date") or not payload.get("as_of"):
        return _error("RESEARCH_INVALID", "symbol、start_date 和 as_of 必填")
    try:
        from StockInvestmentTool.warehouse.storage import Warehouse
        from StockInvestmentTool.indicators.engine import IndicatorRegistry
        from StockInvestmentTool.indicators.context import IndicatorContext
        symbol = __import__("StockInvestmentTool.biz.code", fromlist=["normalize"]).normalize(symbol)
        dataset = load_market_data(
            Warehouse(), start_date=payload.get("start_date"),
            end_date=payload.get("as_of"), symbols=[symbol],
            required_quality="WARNING",
        )
        strategy = None
        strategy_version_id = payload.get("strategy_version_id", "")
        if isinstance(payload.get("strategy"), dict):
            strategy = compile_strategy(StrategySpec(**payload["strategy"]))
        regime = MarketRegimeService(dataset.data, dataset.context).compute(
            payload.get("as_of") or str(dataset.data["date"].iloc[-1])[:10]
        )
        result = ResearchService(dataset.data, dataset.context, strategy, regime.to_dict()).run()
        repo = _repo()
        repo.save_market_regime(regime)
        repo.save_research_run(
            result, dataset.context, subject_type=payload.get("subject_type", "single_symbol"),
            symbol=symbol, strategy_version_id=strategy_version_id,
            observation_id=payload.get("observation_id", ""),
            source_screen_run_id=payload.get("screen_run_id", ""),
            source_candidate_id=payload.get("candidate_id", ""),
        )
        for decision in result.decisions:
            decision.strategy_version_id = strategy_version_id or decision.strategy_version_id
            decision.research_run_id = result.research_run_id
            repo.save_decision(decision)
        return flask.jsonify({"data": {
            "research_run_id": result.research_run_id,
            "result": result.__dict__, "data_context": dataset.context,
        }, "request_id": flask.request.headers.get("X-Request-ID", "")}), 201
    except Exception as exc:  # noqa: BLE001
        return _error("RESEARCH_RUN_FAILED", str(exc), 500)


@biz_api.get("/research-runs/<run_id>")
def get_research_run(run_id: str):
    repo = _repo()
    result = repo.get_research_run(run_id)
    if not result:
        return _error("RESEARCH_RUN_NOT_FOUND", "研究运行不存在", 404)
    result["evidence"] = repo.list_research_evidence(run_id)
    return flask.jsonify({"data": result, "request_id": flask.request.headers.get("X-Request-ID", "")})


@biz_api.get("/research-runs/<run_id>/evidence")
def get_research_evidence(run_id: str):
    repo = _repo()
    if not repo.get_research_run(run_id):
        return _error("RESEARCH_RUN_NOT_FOUND", "研究运行不存在", 404)
    return flask.jsonify({"data": {"run_id": run_id, "evidence": repo.list_research_evidence(run_id)},
                          "request_id": flask.request.headers.get("X-Request-ID", "")})


@biz_api.post("/simulation-runs")
def create_simulation_run():
    """执行并持久化单标的模拟运行。"""
    payload = flask.request.get_json(silent=True) or {}
    strategy_data = payload.get("strategy")
    if (not isinstance(strategy_data, dict) or not payload.get("symbol")
            or not payload.get("start_date") or not payload.get("end_date")):
        return _error("SIMULATION_INVALID", "strategy、symbol、start_date 和 end_date 必填")
    try:
        from StockInvestmentTool.warehouse.storage import Warehouse
        from StockInvestmentTool.biz.code import normalize
        symbol = normalize(payload["symbol"])
        dataset = load_market_data(
            Warehouse(), start_date=payload.get("start_date"),
            end_date=payload.get("end_date"), symbols=[symbol],
            required_quality="WARNING",
        )
        strategy = compile_strategy(StrategySpec(**strategy_data))
        repo = _repo()
        strategy_version_id = payload.get("strategy_version_id") or repo.save_strategy_version(
            strategy.spec.strategy_id, int(strategy.spec.version), strategy_data, strategy.config_hash,
        )
        plan = SimulationPlan(
            plan_id=new_id("plan"), strategy_version_id=strategy_version_id,
            name=payload.get("name", "模拟运行"), start_date=payload["start_date"],
            end_date=payload["end_date"], initial_cash=float(payload.get("initial_cash", 100000)),
            cost_config=payload.get("cost_config", {}), benchmark=payload.get("benchmark", "sh000300"),
            data_context=dataset.context,
        )
        repo.save_simulation_plan(plan)
        run, result, fills, events = execute_simulation(plan, dataset.data, strategy=strategy)
        repo.save_simulation_run(run)
        for fill in fills:
            repo.save_simulation_fill(fill)
        repo.save_simulation_result(result)
        return flask.jsonify({"data": {
            "run": run.__dict__, "result": result.__dict__,
            "fills": [f.__dict__ for f in fills], "events": [e.__dict__ for e in events],
            "data_context": dataset.context,
        }, "request_id": flask.request.headers.get("X-Request-ID", "")}), 201
    except Exception as exc:  # noqa: BLE001
        return _error("SIMULATION_RUN_FAILED", str(exc), 500)


@biz_api.get("/simulation-runs/<run_id>")
def get_simulation_run(run_id: str):
    repo = _repo()
    run = repo.db.fetchone("SELECT * FROM simulation_runs WHERE run_id=?", (run_id,))
    if not run:
        return _error("SIMULATION_RUN_NOT_FOUND", "模拟运行不存在", 404)
    result = repo.get_simulation_result(run_id)
    fills = repo.list_simulation_fills(run_id)
    data = dict(run)
    data["data_context"] = __import__("StockInvestmentTool.biz.db", fromlist=["loads_json"]).loads_json(data.pop("data_context_json"))
    data["result"] = result
    data["fills"] = fills
    return flask.jsonify({"data": data, "request_id": flask.request.headers.get("X-Request-ID", "")})


@biz_api.get("/portfolios/<portfolio_id>/summary")
def get_portfolio_summary(portfolio_id: str):
    from StockInvestmentTool.biz.portfolio import PortfolioService
    from StockInvestmentTool.biz.valuation import PositionValuationService

    repo = _repo()
    portfolio = PortfolioService(repo).get_portfolio(portfolio_id)
    if not portfolio:
        return _error("PORTFOLIO_NOT_FOUND", "组合不存在", 404)
    # API 调用方应传入已通过 DatasetAccess 获取的行情；没有行情时返回事实层摘要。
    summary = {
        "portfolio_id": portfolio_id,
        "cash_balance": PortfolioService(repo).cash_balance(portfolio_id),
        "market_value": None,
        "total_assets": None,
        "valuation_status": "unavailable",
    }
    return flask.jsonify({"data": summary, "request_id": flask.request.headers.get("X-Request-ID", "")})


@biz_api.get("/advices")
def list_advices():
    repo = _repo()
    rows = repo.db.fetchall("SELECT * FROM advices ORDER BY created_at DESC LIMIT 200")
    return flask.jsonify({"data": {"items": [dict(row) for row in rows]},
                          "request_id": flask.request.headers.get("X-Request-ID", "")})


@biz_api.get("/notifications/events")
def list_notification_events():
    from StockInvestmentTool.biz.notification import NotificationService
    return flask.jsonify({"data": {"items": NotificationService(_repo()).list_events()},
                          "request_id": flask.request.headers.get("X-Request-ID", "")})


@biz_api.get("/notifications/deliveries")
def list_notification_deliveries():
    repo = _repo()
    rows = repo.db.fetchall("SELECT * FROM notification_deliveries ORDER BY created_at DESC LIMIT 200")
    return flask.jsonify({"data": {"items": [dict(row) for row in rows]},
                          "request_id": flask.request.headers.get("X-Request-ID", "")})


@biz_api.get("/position-cycles/<cycle_id>/snapshots/<as_of>")
def get_position_snapshot(cycle_id: str, as_of: str):
    from StockInvestmentTool.biz.portfolio import PortfolioService
    from StockInvestmentTool.biz.valuation import PositionValuationService
    repo = _repo()
    snapshot = PositionValuationService(PortfolioService(repo)).get_snapshot(cycle_id, as_of)
    if not snapshot:
        return _error("POSITION_SNAPSHOT_NOT_FOUND", "持仓估值快照不存在", 404)
    return flask.jsonify({"data": snapshot, "request_id": flask.request.headers.get("X-Request-ID", "")})


@biz_api.post("/advices/<advice_id>/execution")
def record_advice_execution(advice_id: str):
    payload = flask.request.get_json(silent=True) or {}
    if payload.get("executed_quantity") is None:
        return _error("INVALID_EXECUTION", "executed_quantity 必填")
    from StockInvestmentTool.biz.notification import NotificationService
    try:
        result = NotificationService(_repo()).record_execution(
            advice_id, executed_quantity=float(payload["executed_quantity"]),
            requested_quantity=float(payload["requested_quantity"])
            if payload.get("requested_quantity") is not None else None,
        )
        return flask.jsonify({"data": result, "request_id": flask.request.headers.get("X-Request-ID", "")})
    except KeyError as exc:
        return _error("ADVICE_NOT_FOUND", str(exc), 404)


@biz_api.post("/reports/daily")
def create_daily_report():
    payload = flask.request.get_json(silent=True) or {}
    if not payload.get("report_date"):
        return _error("REPORT_INVALID", "report_date 必填")
    from StockInvestmentTool.biz.reporting import ReportingService
    report = ReportingService(_repo()).create_report(
        payload["report_date"], data_as_of=payload.get("data_as_of"),
        market_snapshot=payload.get("market_snapshot"),
        observation_snapshot=payload.get("observation_snapshot"),
        portfolio_snapshot=payload.get("portfolio_snapshot"),
        advice_ids=payload.get("advice_ids"), sections=payload.get("sections"),
    )
    return flask.jsonify({"data": report.__dict__, "request_id": flask.request.headers.get("X-Request-ID", "")}), 201


@biz_api.get("/reports/daily/<report_date>")
def get_daily_report(report_date: str):
    from StockInvestmentTool.biz.reporting import ReportingService
    report = ReportingService(_repo()).get_daily_report(report_date)
    if not report:
        return _error("REPORT_NOT_FOUND", "日报不存在", 404)
    return flask.jsonify({"data": report, "request_id": flask.request.headers.get("X-Request-ID", "")})


@biz_api.get("/system/alerts")
def list_system_alerts():
    from StockInvestmentTool.biz.reporting import SystemAlertService
    return flask.jsonify({"data": {"items": SystemAlertService(_repo()).list_active()},
                          "request_id": flask.request.headers.get("X-Request-ID", "")})


@biz_api.post("/candidates/<candidate_id>/observation")
def candidate_observation(candidate_id: str):
    try:
        obs = BusinessWorkflowService(_repo()).observe_candidate(candidate_id)
        return flask.jsonify({"data": obs.__dict__, "request_id": flask.request.headers.get("X-Request-ID", "")}), 201
    except WorkflowError as exc:
        return _error(str(exc), str(exc))


@biz_api.get("/observations/<observation_id>")
def get_observation(observation_id: str):
    obs = ObservationService(_repo()).get_observation(observation_id)
    if not obs:
        return _error("OBSERVATION_NOT_FOUND", "观察对象不存在", 404)
    return flask.jsonify({"data": obs.__dict__, "request_id": flask.request.headers.get("X-Request-ID", "")})


@biz_api.post("/observations/<observation_id>/ready-for-entry")
def ready_for_entry(observation_id: str):
    service = ObservationService(_repo())
    obs = service.get_observation(observation_id)
    if not obs:
        return _error("OBSERVATION_NOT_FOUND", "观察对象不存在", 404)
    try:
        event = service.transition_and_save(obs, "ready_for_entry", reason="用户确认可进入建仓")
        return flask.jsonify({"data": {"observation": obs.__dict__, "event": event.__dict__},
                              "request_id": flask.request.headers.get("X-Request-ID", "")})
    except ValueError as exc:
        return _error("OBSERVATION_INVALID_TRANSITION", str(exc))


@biz_api.post("/observations/<observation_id>/entry")
def record_entry(observation_id: str):
    payload = flask.request.get_json(silent=True) or {}
    if not payload.get("portfolio_id") or not payload.get("idempotency_key"):
        return _error("INVALID_ENTRY", "portfolio_id 和 idempotency_key 必填")
    service = BusinessWorkflowService(_repo())
    try:
        context = service.build_entry_context(observation_id, payload["portfolio_id"])
        execution, observation = service.record_entry(
            context, quantity=float(payload["quantity"]), price=float(payload["price"]),
            fee=float(payload.get("fee", 0)), tax=float(payload.get("tax", 0)),
            idempotency_key=payload["idempotency_key"],
        )
        return flask.jsonify({"data": {"execution": execution.__dict__, "observation": observation.__dict__},
                              "request_id": flask.request.headers.get("X-Request-ID", "")}), 201
    except WorkflowError as exc:
        status = 409 if str(exc) in {"ENTRY_CONFIRMATION_REQUIRED", "INSUFFICIENT_CASH"} else 400
        return _error(str(exc), str(exc), status)
