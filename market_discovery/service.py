"""Market discovery on the local daily warehouse.

This module deliberately does not fetch remote quotes.  It creates candidates
from the same daily Parquet data used by indicators and backtests, then emits
explainable price/volume signals for the observation funnel.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import duckdb

from StockInvestmentTool.warehouse.storage import Warehouse


DEFAULT_CONDITIONS = {
    "lookback_days": 3,
    "min_up_days": 0,
    "max_down_days": 3,
    "return_min_pct": None,
    "return_max_pct": None,
    "volume_ratio_min": None,
    "volume_ratio_max": None,
    "require_price_up": False,
    "require_volume_decline": False,
    "signal": "",
    "min_history": 80,
}


def _files(warehouse: Warehouse) -> str:
    paths = [warehouse.daily_partition(month) for month in warehouse.available_months("daily")]
    paths = [str(path) for path in paths if path.exists()]
    if not paths:
        raise ValueError("本地 daily 仓库没有可用分区")
    return "[" + ",".join("'" + path.replace("'", "''") + "'" for path in paths) + "]"


def _number(value, name: str, *, minimum=None, maximum=None) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum) or (maximum is not None and result > maximum):
        raise ValueError(f"{name} 超出有效范围")
    return result


def _conditions(raw: Optional[dict]) -> dict:
    current = {**DEFAULT_CONDITIONS, **(raw or {})}
    lookback = int(_number(current.get("lookback_days"), "lookback_days", minimum=2, maximum=60) or 3)
    current["lookback_days"] = lookback
    current["min_up_days"] = int(_number(current.get("min_up_days"), "min_up_days", minimum=0, maximum=lookback) or 0)
    current["max_down_days"] = int(_number(current.get("max_down_days"), "max_down_days", minimum=0, maximum=lookback) if current.get("max_down_days") not in (None, "") else lookback)
    for key in ("return_min_pct", "return_max_pct", "volume_ratio_min", "volume_ratio_max"):
        current[key] = _number(current.get(key), key, minimum=-1000, maximum=1000)
    current["min_history"] = int(_number(current.get("min_history"), "min_history", minimum=20, maximum=5000) or 80)
    current["require_price_up"] = bool(current.get("require_price_up"))
    current["require_volume_decline"] = bool(current.get("require_volume_decline"))
    current["signal"] = str(current.get("signal") or "").strip().lower()
    if current["signal"] not in ("", "price_up_volume_down", "price_down_volume_up", "volume_spike", "price_up_volume_up"):
        raise ValueError("未知价量信号")
    return current


def discover_stocks(conditions: Optional[dict] = None, *, top_n: int = 50,
                    as_of: str = "", warehouse: Optional[Warehouse] = None) -> dict:
    """Screen local daily data and return rows plus the applied data date."""
    c = _conditions(conditions)
    top_n = max(1, min(int(top_n), 500))
    warehouse = warehouse or Warehouse()
    files = _files(warehouse)
    as_of_sql = "" if not as_of else "AND date <= ?"
    lookback = c["lookback_days"]
    con = duckdb.connect()
    try:
        query = f"""
        WITH base AS (
          SELECT code, CAST(date AS DATE) AS date, close, high, low, volume, amount,
                 LAG(close, 1) OVER (PARTITION BY code ORDER BY date) AS prev_close,
                 LAG(close, {lookback}) OVER (PARTITION BY code ORDER BY date) AS old_close,
                 LAG(volume, {lookback}) OVER (PARTITION BY code ORDER BY date) AS old_volume,
                 AVG(volume) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS avg_volume_5,
                 AVG(volume) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS avg_volume_20,
                 COUNT(*) OVER (PARTITION BY code) AS history_count
          FROM read_parquet({files})
          WHERE 1=1 {as_of_sql}
        ), changes AS (
          SELECT *, close > prev_close AS is_up, close < prev_close AS is_down
          FROM base
        ), source AS (
          SELECT *,
                 SUM(CASE WHEN is_up THEN 1 ELSE 0 END) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN {lookback - 1} PRECEDING AND CURRENT ROW) AS up_days,
                 SUM(CASE WHEN is_down THEN 1 ELSE 0 END) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN {lookback - 1} PRECEDING AND CURRENT ROW) AS down_days
          FROM changes
        ), latest AS (
          SELECT *, ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) AS rn
          FROM source
        )
        SELECT code, date, close, high, low, volume, amount, prev_close, old_close,
               old_volume, avg_volume_5, avg_volume_20, history_count, up_days, down_days,
               (close / NULLIF(old_close, 0) - 1) * 100 AS return_pct,
               volume / NULLIF(avg_volume_5, 0) AS volume_ratio_5,
               close / NULLIF(avg_volume_20, 0) AS close_vs_volume20,
               volume / NULLIF(old_volume, 0) AS volume_change_ratio
        FROM latest WHERE rn = 1 AND history_count >= ?
        """
        params = ([as_of, c["min_history"]] if as_of else [c["min_history"]])
        rows = con.execute(query, params).fetchdf().to_dict("records")
    finally:
        con.close()

    results = []
    for row in rows:
        ret = _float(row.get("return_pct"))
        up_days = int(row.get("up_days") or 0)
        down_days = int(row.get("down_days") or 0)
        volume_ratio = _float(row.get("volume_ratio_5"))
        old_volume = _float(row.get("old_volume"))
        volume_change = (_float(row.get("volume_change_ratio")) - 1) * 100 if old_volume else None
        price_up = ret is not None and ret > 0
        volume_down = volume_change is not None and volume_change < 0
        volume_spike = volume_ratio is not None and volume_ratio >= 1.8
        price_up_volume_up = price_up and volume_change is not None and volume_change > 0
        price_down_volume_up = ret is not None and ret < 0 and volume_change is not None and volume_change > 0
        tags = []
        explanations = []
        if price_up and volume_down:
            tags.append("上涨缩量")
            explanations.append("近阶段价格上涨，但成交量较阶段起点下降，后劲需要观察")
        if price_down_volume_up:
            tags.append("下跌放量")
            explanations.append("价格走弱且成交量上升，短线风险增加")
        if volume_spike:
            tags.append("放量")
            explanations.append("最新成交量达到 5 日均量的 %.1fx" % volume_ratio)
        if price_up_volume_up:
            tags.append("上涨放量")
            explanations.append("价格上涨同时成交量较阶段起点增加")
        if not tags:
            tags.append("价量平稳")
        if up_days >= 2 and volume_down:
            explanations.append("近 %d 日上涨 %d 天，量能未同步增强" % (c["lookback_days"], up_days))
        if c["min_up_days"] and up_days < c["min_up_days"]:
            continue
        if down_days > c["max_down_days"]:
            continue
        if c["return_min_pct"] is not None and (ret is None or ret < c["return_min_pct"]):
            continue
        if c["return_max_pct"] is not None and (ret is None or ret > c["return_max_pct"]):
            continue
        if c["volume_ratio_min"] is not None and (volume_ratio is None or volume_ratio < c["volume_ratio_min"]):
            continue
        if c["volume_ratio_max"] is not None and (volume_ratio is None or volume_ratio > c["volume_ratio_max"]):
            continue
        if c["require_price_up"] and not price_up:
            continue
        if c["require_volume_decline"] and not volume_down:
            continue
        signal_map = {"price_up_volume_down": price_up and volume_down,
                      "price_down_volume_up": price_down_volume_up,
                      "volume_spike": volume_spike,
                      "price_up_volume_up": price_up_volume_up}
        if c["signal"] and not signal_map[c["signal"]]:
            continue
        results.append({"code": str(row["code"]), "date": str(row["date"])[:10],
                        "price": _round(row.get("close")), "return_pct": _round(ret),
                        "up_days": up_days, "down_days": down_days,
                        "volume_ratio_5": _round(volume_ratio),
                        "volume_change_pct": _round(volume_change),
                        "amount": _round(row.get("amount")), "signal_tags": tags,
                        "explanations": explanations})
    results.sort(key=lambda item: (-(item["return_pct"] or -999), -(item["volume_ratio_5"] or 0), item["code"]))
    total_count = len(results)
    results = results[:top_n]
    _attach_names(results, warehouse)
    return {"conditions": c, "as_of": as_of or (results[0]["date"] if results else None),
            "count": len(results), "total_count": total_count, "items": results}


def stock_series(code: str, *, days: int = 120, as_of: str = "",
                 warehouse: Optional[Warehouse] = None) -> dict:
    """Return local daily series for the discovery detail panel."""
    warehouse = warehouse or Warehouse()
    files = _files(warehouse)
    days = max(20, min(int(days), 750))
    con = duckdb.connect()
    try:
        query = f"SELECT CAST(date AS DATE) AS date, close, high, low, volume, amount FROM read_parquet({files}) WHERE code=? "
        params = [code]
        if as_of:
            query += "AND date <= ? "
            params.append(as_of)
        query += "ORDER BY date DESC LIMIT ?"
        params.append(days)
        frame = con.execute(query, params).fetchdf().sort_values("date")
    finally:
        con.close()
    return {"code": code, "dates": [str(x)[:10] for x in frame["date"]],
            "close": [_round(x) for x in frame.get("close", [])],
            "high": [_round(x) for x in frame.get("high", [])],
            "low": [_round(x) for x in frame.get("low", [])],
            "volume": [_round(x) for x in frame.get("volume", [])]}


def _attach_names(items: list[dict], warehouse: Warehouse) -> None:
    if not items:
        return
    codes = [item["code"] for item in items]
    placeholders = ",".join("?" for _ in codes)
    with warehouse._conn() as conn:
        rows = conn.execute(f"SELECT code, name, industry FROM instruments WHERE code IN ({placeholders})", codes).fetchall()
    lookup = {row[0]: (row[1], row[2]) for row in rows}
    for item in items:
        name, industry = lookup.get(item["code"], ("", ""))
        item["name"] = name or item["code"]
        item["industry"] = industry or ""


def _float(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _round(value, digits: int = 2):
    value = _float(value)
    return round(value, digits) if value is not None else None
