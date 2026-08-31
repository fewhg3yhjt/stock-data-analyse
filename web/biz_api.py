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
from StockInvestmentTool.biz.models import DataContext, StrategyContext
from StockInvestmentTool.biz.observation import ObservationService
from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.screen import ScreenDefinition, ScreenExecutor
from StockInvestmentTool.biz.workflow import BusinessWorkflowService, WorkflowError

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
    if not isinstance(condition, dict):
        return _error("SCREEN_INVALID", "condition_spec 必须是对象")
    try:
        from StockInvestmentTool.warehouse.datasets import load_dataset
        from StockInvestmentTool.warehouse.storage import Warehouse
        data = load_dataset(
            Warehouse(), "stock_daily", start_date=payload.get("start_date"),
            end_date=payload.get("end_date"), symbols=payload.get("symbols"),
            required_quality="WARNING", allow_legacy=False,
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
