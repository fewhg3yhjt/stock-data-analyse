# -*- coding: utf-8 -*-
"""业务平面对数据平面的只读访问组合器。"""

from __future__ import annotations

import pandas as pd

from StockInvestmentTool.biz.code import normalize
from StockInvestmentTool.warehouse.datasets import DatasetResult, load_dataset


def load_market_data(warehouse, *, start_date: str, end_date: str,
                     symbols: list[str] | None = None,
                     required_quality: str = "WARNING") -> DatasetResult:
    """读取 Published 日线和指标并按 date/code 合并。

    不直接访问文件或 management.db；指标缺失时保留日线并在 context 标记，
    由上层按策略依赖决定是否阻断。
    """
    daily = load_dataset(
        warehouse, "stock_daily", start_date=start_date, end_date=end_date,
        symbols=symbols, required_quality=required_quality, allow_legacy=False,
    )
    if daily.data.empty:
        return daily
    try:
        indicators = load_dataset(
            warehouse, "indicators", start_date=start_date, end_date=end_date,
            symbols=symbols, required_quality=required_quality, allow_legacy=False,
        )
    except Exception:
        indicators = None
    if indicators is None or indicators.data.empty:
        context = dict(daily.context)
        context["indicator_dataset_unavailable"] = True
        context["indicator_refs"] = {}
        return DatasetResult(data=daily.data, context=context)

    left = daily.data.copy()
    right = indicators.data.copy()
    for frame in (left, right):
        frame["code"] = frame["code"].astype(str).map(_safe_normalize)
        frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
    columns = [c for c in right.columns if c not in {"date", "code", "close"}]
    merged = left.merge(right[["date", "code", *columns]],
                        on=["date", "code"], how="left", sort=False)
    context = dict(daily.context)
    context["indicator_context"] = indicators.context
    context["indicator_refs"] = indicators.context.get("dataset_refs", {})
    context["indicator_dataset_unavailable"] = False
    context["data_as_of"] = min(
        value for value in (daily.context.get("data_as_of"), indicators.context.get("data_as_of"))
        if value
    ) if daily.context.get("data_as_of") and indicators.context.get("data_as_of") else (
        daily.context.get("data_as_of") or indicators.context.get("data_as_of")
    )
    return DatasetResult(data=merged, context=context)


def _safe_normalize(value: str) -> str:
    try:
        return normalize(value)
    except ValueError:
        return value
