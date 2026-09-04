"""Public, read-only market data API for external tools and LLMs."""

from __future__ import annotations

import math
from datetime import date, datetime

import flask
import numpy as np
import pandas as pd

from StockInvestmentTool.warehouse.dataset_config import load_dataset_config
from StockInvestmentTool.warehouse.datasets import DatasetAccess, DatasetAccessError
from StockInvestmentTool.warehouse.storage import Warehouse


public_api = flask.Blueprint("public_api", __name__)

_MAX_PAGE_SIZE = 1000
_REPORTS = {
    "stock_daily": {"path": "/api/public/stock-daily", "date_field": "date"},
    "industry_daily": {"path": "/api/public/industry-daily", "date_field": "trading_date"},
    "industry_membership": {"path": "/api/public/stock-sectors", "date_field": "snapshot_date"},
    "ths_industry_membership": {"path": "/api/public/stock-sectors", "date_field": "snapshot_date"},
}


def _error(message: str, status: int = 400):
    return flask.jsonify({"status": "error", "error": message}), status


def _date_arg(name: str) -> str | None:
    value = (flask.request.args.get(name) or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"{name} 必须为 YYYY-MM-DD") from exc


def _page_args() -> tuple[int, int]:
    try:
        page = int(flask.request.args.get("page", 1))
        page_size = int(flask.request.args.get("page_size", 100))
    except ValueError as exc:
        raise ValueError("page 和 page_size 必须为整数") from exc
    if page < 1:
        raise ValueError("page 必须大于等于 1")
    if not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise ValueError(f"page_size 必须在 1 到 {_MAX_PAGE_SIZE} 之间")
    return page, page_size


def _json_value(value):
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return _json_value(value.item())
    return value


def _records(frame: pd.DataFrame) -> list[dict]:
    return [
        {str(key): _json_value(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _response(report: str, frame: pd.DataFrame, *, page: int, page_size: int,
              start: str | None = None, end: str | None = None, context: dict | None = None):
    total = len(frame)
    total_pages = math.ceil(total / page_size) if total else 0
    offset = (page - 1) * page_size
    payload = {
        "status": "ok",
        "report": report,
        "start": start,
        "end": end,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1 and total > 0,
        "data": _records(frame.iloc[offset:offset + page_size]),
    }
    if context:
        payload["meta"] = {
            "data_as_of": context.get("data_as_of"),
            "quality_status": context.get("quality_status"),
            "source": context.get("source"),
            "partition_versions": context.get("partition_versions", {}),
        }
    return flask.jsonify(payload)


def _latest_snapshot_date(access: DatasetAccess, dataset_name: str) -> str | None:
    versions = access.get_current_version(dataset_name)
    return max(versions) if versions else None


def _load_snapshot(access: DatasetAccess, dataset_name: str, as_of: str | None):
    snapshot_date = as_of or _latest_snapshot_date(access, dataset_name)
    if not snapshot_date:
        raise DatasetAccessError(f"{dataset_name} 没有 Published Dataset")
    return access.load_dataset(dataset_name, end_date=snapshot_date, required_quality="WARNING")


def _membership_frame(as_of: str | None) -> tuple[pd.DataFrame, dict]:
    access = DatasetAccess(Warehouse())
    frames, contexts = [], []
    for dataset_name, classification, sector_id in (
        ("industry_membership", "csrc", "industry_code"),
        ("ths_industry_membership", "ths_industry", "industry_id"),
    ):
        try:
            result = _load_snapshot(access, dataset_name, as_of)
        except DatasetAccessError:
            continue
        frame = result.data.copy()
        if frame.empty:
            continue
        if "industry_classification" in frame:
            frame["classification"] = frame["industry_classification"].fillna(classification)
        else:
            frame["classification"] = classification
        frame["sector_id"] = frame[sector_id].astype(str)
        frame["sector_name"] = frame["industry_name"].astype(str)
        frame["symbol"] = frame["code"].astype(str)
        frames.append(frame)
        contexts.append(result.context)
    if not frames:
        raise DatasetAccessError("没有可读取的 Published 行业成员数据")
    frame = pd.concat(frames, ignore_index=True)
    context = {"data_as_of": max((item.get("data_as_of") or "") for item in contexts),
               "quality_status": "WARNING", "source": "published_dataset",
               "partition_versions": {key: value for item in contexts for key, value in item.get("partition_versions", {}).items()}}
    return frame, context


@public_api.route("/reports", methods=["GET"])
def reports():
    warehouse = Warehouse()
    access = DatasetAccess(warehouse)
    data = []
    for dataset_name, details in _REPORTS.items():
        config = load_dataset_config(dataset_name)
        versions = access.get_current_version(dataset_name) or {}
        data.append({
            "report": dataset_name,
            "display_name": config["dataset"]["display_name"],
            "description": config["dataset"]["description"],
            "path": details["path"],
            "date_field": details["date_field"],
            "fields": [field["name"] for field in config["fields"]],
            "published_partitions": sorted(versions),
            "latest_published_partition": max(versions) if versions else None,
        })
    data.extend([
        {"report": "stocks", "display_name": "股票标的目录", "path": "/api/public/stocks",
         "description": "当前采集的股票、ETF 和指数基础资料", "fields": ["code", "name", "type", "board", "listed_date", "industry", "updated_at"]},
        {"report": "sectors", "display_name": "板块目录", "path": "/api/public/sectors",
         "description": "已发布行业成员快照中的板块目录", "fields": ["sector_id", "sector_name", "classification", "source", "snapshot_date", "captured_at"]},
        {"report": "stock_sectors", "display_name": "股票板块关系", "path": "/api/public/stock-sectors",
         "description": "已发布行业成员快照中的股票与板块对应关系", "fields": ["symbol", "sector_id", "sector_name", "classification", "source", "snapshot_date"]},
    ])
    return flask.jsonify({"status": "ok", "report": "reports", "data": data})


@public_api.route("/stocks", methods=["GET"])
def stocks():
    try:
        page, page_size = _page_args()
        symbol = (flask.request.args.get("symbol") or "").strip().lower().replace(".", "")
        query = (flask.request.args.get("q") or "").strip().lower()
        asset_type = (flask.request.args.get("type") or "").strip().lower()
        frame = pd.DataFrame(Warehouse().list_instruments())
        if frame.empty:
            frame = pd.DataFrame(columns=["code", "name", "type", "board", "listed_date", "industry", "updated_at"])
        if symbol:
            frame = frame[frame["code"].astype(str).str.lower() == symbol]
        if query:
            frame = frame[frame["code"].astype(str).str.lower().str.contains(query, regex=False) |
                          frame["name"].astype(str).str.lower().str.contains(query, regex=False)]
        if asset_type:
            frame = frame[frame["type"].astype(str).str.lower() == asset_type]
        return _response("stocks", frame.sort_values("code"), page=page, page_size=page_size)
    except ValueError as exc:
        return _error(str(exc))


@public_api.route("/sectors", methods=["GET"])
def sectors():
    try:
        page, page_size = _page_args()
        as_of = _date_arg("as_of")
        classification = (flask.request.args.get("classification") or "").strip()
        source = (flask.request.args.get("source") or "").strip()
        frame, context = _membership_frame(as_of)
        if classification:
            frame = frame[frame["classification"].astype(str) == classification]
        if source:
            frame = frame[frame["source"].astype(str) == source]
        columns = [name for name in ("sector_id", "sector_name", "classification", "source", "snapshot_date", "captured_at") if name in frame]
        frame = frame[columns].drop_duplicates().sort_values(["classification", "sector_id"])
        return _response("sectors", frame, page=page, page_size=page_size, end=as_of, context=context)
    except (ValueError, DatasetAccessError) as exc:
        return _error(str(exc), 404 if isinstance(exc, DatasetAccessError) else 400)


@public_api.route("/stock-sectors", methods=["GET"])
def stock_sectors():
    try:
        page, page_size = _page_args()
        as_of = _date_arg("as_of")
        symbol = (flask.request.args.get("symbol") or "").strip().lower().replace(".", "")
        sector_id = (flask.request.args.get("sector_id") or "").strip()
        classification = (flask.request.args.get("classification") or "").strip()
        frame, context = _membership_frame(as_of)
        if symbol:
            frame = frame[frame["symbol"].astype(str).str.lower() == symbol]
        if sector_id:
            frame = frame[frame["sector_id"].astype(str) == sector_id]
        if classification:
            frame = frame[frame["classification"].astype(str) == classification]
        preferred = ["symbol", "stock_name", "sector_id", "sector_name", "classification", "snapshot_date", "source", "captured_at"]
        columns = [name for name in preferred if name in frame]
        frame = frame[columns].sort_values(["symbol", "classification", "sector_id"])
        return _response("stock_sectors", frame, page=page, page_size=page_size, end=as_of, context=context)
    except (ValueError, DatasetAccessError) as exc:
        return _error(str(exc), 404 if isinstance(exc, DatasetAccessError) else 400)


def _daily_report(dataset_name: str, report: str, *, code_field: str, code_arg: str):
    try:
        page, page_size = _page_args()
        start, end = _date_arg("start"), _date_arg("end")
        if not start or not end:
            raise ValueError("start 和 end 为必填日期参数")
        if start > end:
            raise ValueError("start 不能晚于 end")
        result = DatasetAccess(Warehouse()).load_dataset(
            dataset_name, start_date=start, end_date=end, required_quality="WARNING"
        )
        frame = result.data
        value = (flask.request.args.get(code_arg) or "").strip().lower().replace(".", "")
        if value:
            frame = frame[frame[code_field].astype(str).str.lower().str.replace(".", "", regex=False) == value]
        date_field = "date" if dataset_name == "stock_daily" else "trading_date"
        sort_columns = [name for name in (date_field, code_field) if name in frame]
        if sort_columns:
            frame = frame.sort_values(sort_columns)
        return _response(report, frame, page=page, page_size=page_size, start=start, end=end, context=result.context)
    except (ValueError, DatasetAccessError) as exc:
        return _error(str(exc), 404 if isinstance(exc, DatasetAccessError) else 400)


@public_api.route("/stock-daily", methods=["GET"])
def stock_daily():
    return _daily_report("stock_daily", "stock_daily", code_field="code", code_arg="symbol")


@public_api.route("/industry-daily", methods=["GET"])
def industry_daily():
    return _daily_report("industry_daily", "industry_daily", code_field="industry_id", code_arg="industry_id")


@public_api.route("/openapi.json", methods=["GET"])
def openapi():
    paths = {
        "/api/public/reports": {"get": {"summary": "List available public reports and fields"}},
        "/api/public/stocks": {"get": {"summary": "List collected stock instruments"}},
        "/api/public/sectors": {"get": {"summary": "List published sector catalog"}},
        "/api/public/stock-sectors": {"get": {"summary": "List published stock-sector relations"}},
        "/api/public/stock-daily": {"get": {"summary": "Query published stock daily records; start and end are required"}},
        "/api/public/industry-daily": {"get": {"summary": "Query published industry daily records; start and end are required"}},
    }
    return flask.jsonify({
        "openapi": "3.0.3",
        "info": {"title": "StockInvestmentTool Public Market Data API", "version": "v1",
                 "description": "Public read-only access to published market datasets. No API key is required."},
        "paths": paths,
    })
