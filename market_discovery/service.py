"""Market discovery on the local daily warehouse.

This module deliberately does not fetch remote quotes.  It creates candidates
from the same daily Parquet data used by indicators and backtests, then emits
explainable price/volume signals for the observation funnel.
"""

from __future__ import annotations

import math
from typing import Optional

import duckdb

from StockInvestmentTool.warehouse.storage import Warehouse


DEFAULT_CONDITIONS = {
    "keyword": "",
    "market": "ALL",
    "board": "ALL",
    "industry": "ALL",
    "lookback_days": 3,
    "min_up_days": 0,
    # Empty range fields must not impose an implicit restriction.
    "max_down_days": None,
    "return_min_pct": None,
    "return_max_pct": None,
    "volume_ratio_min": None,
    "volume_ratio_max": None,
    "period_return_min": None,
    "period_return_max": None,
    "up_days_min": None,
    "up_days_max": None,
    "down_days_min": None,
    "down_days_max": None,
    "amplitude_min": None,
    "amplitude_max": None,
    "amount_avg_min": None,
    "amount_avg_max": None,
    "volume_5_20_min": None,
    "volume_5_20_max": None,
    "turnover_min": None,
    "turnover_max": None,
    "price_min": None,
    "price_max": None,
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


def _has_column(con, files: str, column: str) -> bool:
    """探测 daily 分区是否含指定列（列缺失时容错，避免查询报错）。"""
    try:
        con.execute(f"SELECT {column} FROM read_parquet({files}) LIMIT 1")
        return True
    except Exception:
        return False


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
    current["max_down_days"] = (int(_number(current.get("max_down_days"), "max_down_days", minimum=0, maximum=lookback))
                                 if current.get("max_down_days") not in (None, "") else None)
    for key in ("return_min_pct", "return_max_pct", "volume_ratio_min", "volume_ratio_max",
                "period_return_min", "period_return_max", "amount_avg_min", "amount_avg_max",
                "volume_5_20_min", "volume_5_20_max", "turnover_min", "turnover_max",
                "price_min", "price_max", "amplitude_min", "amplitude_max"):
        current[key] = _number(current.get(key), key, minimum=-1000, maximum=1000)
    for key in ("up_days_min", "up_days_max", "down_days_min", "down_days_max"):
        current[key] = int(_number(current.get(key), key, minimum=0, maximum=lookback) or 0) if current.get(key) not in (None, "") else None
    current["min_history"] = int(_number(current.get("min_history"), "min_history", minimum=20, maximum=5000) or 80)
    current["require_price_up"] = bool(current.get("require_price_up"))
    current["require_volume_decline"] = bool(current.get("require_volume_decline"))
    current["signal"] = str(current.get("signal") or "").strip().lower()
    current["keyword"] = str(current.get("keyword") or "").strip()
    current["market"] = str(current.get("market") or "ALL").upper()
    current["board"] = str(current.get("board") or "ALL").lower()
    current["industry"] = str(current.get("industry") or "ALL").strip()
    if current["market"] not in ("ALL", "SH", "SZ", "BJ"):
        raise ValueError("未知市场")
    if current["board"] not in ("all", "main", "cyb", "kcb", "bse"):
        raise ValueError("未知板块")
    if current["signal"] not in ("", "price_up_volume_down", "price_down_volume_up", "volume_spike", "price_up_volume_up"):
        raise ValueError("未知价量信号")
    return current


def discover_stocks(conditions: Optional[dict] = None, *, top_n: int = 50,
                    as_of: str = "", warehouse: Optional[Warehouse] = None,
                    page: int = 1, page_size: int = 20,
                    sort: str = "return_pct", descending: bool = True) -> dict:
    """Screen local daily data and return rows plus the applied data date."""
    c = _conditions(conditions)
    top_n = max(1, min(int(top_n), 5000))
    page = max(1, int(page))
    page_size = max(10, min(int(page_size), 100))
    warehouse = warehouse or Warehouse()
    files = _files(warehouse)
    as_of_sql = "" if not as_of else "AND date <= ?"
    lookback = c["lookback_days"]
    con = duckdb.connect()
    try:
        has_pe = _has_column(con, files, "pe_ttm")
        pe_sql = ", pe_ttm, pb_mrq" if has_pe else ""
        query = f"""
        WITH base AS (
          SELECT code, CAST(date AS DATE) AS date, close, high, low, volume, amount
                 {pe_sql},
                 LAG(close, 1) OVER (PARTITION BY code ORDER BY date) AS prev_close,
                 LAG(close, {lookback}) OVER (PARTITION BY code ORDER BY date) AS old_close,
                 LAG(volume, {lookback}) OVER (PARTITION BY code ORDER BY date) AS old_volume,
                 AVG(volume) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS avg_volume_5,
                 AVG(volume) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS avg_volume_20,
                 AVG(volume) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN {lookback} PRECEDING AND 1 PRECEDING) AS avg_volume_prev_n,
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
               old_volume, avg_volume_5, avg_volume_20, avg_volume_prev_n, history_count, rn
               {pe_sql}
        FROM latest WHERE rn <= ? AND history_count >= ?
        """
        params = ([as_of, lookback + 1, c["min_history"]] if as_of else [lookback + 1, c["min_history"]])
        rows = con.execute(query, params).fetchdf().to_dict("records")
    finally:
        con.close()

    grouped = {}
    for row in rows:
        grouped.setdefault(str(row["code"]), []).append(row)
    results = []
    for code, history in grouped.items():
        history.sort(key=lambda item: int(item["rn"]))
        latest = history[0]
        prior = history[1:]
        close = _float(latest.get("close"))
        old_close = _float(history[-1].get("close")) if len(history) > lookback else None
        ret = (close / old_close - 1) * 100 if close is not None and old_close else None
        up_days = sum(_float(item.get("close")) is not None and _float(item.get("prev_close")) is not None and item["close"] > item["prev_close"] for item in history[:lookback])
        down_days = sum(_float(item.get("close")) is not None and _float(item.get("prev_close")) is not None and item["close"] < item["prev_close"] for item in history[:lookback])
        # history is latest-first; count only the current run, not the max run.
        consecutive_up = consecutive_down = 0
        for item in history[:lookback]:
            if item.get("prev_close") is None or item["close"] == item["prev_close"]:
                break
            if item["close"] > item["prev_close"] and consecutive_down == 0:
                consecutive_up += 1
            else:
                break
        for item in history[:lookback]:
            if item.get("prev_close") is None or item["close"] == item["prev_close"]:
                break
            if item["close"] < item["prev_close"] and consecutive_up == 0:
                consecutive_down += 1
            else:
                break
        volumes = [_float(item.get("volume")) for item in history[:lookback]]
        amounts = [_float(item.get("amount")) for item in history[:lookback]]
        valid_volumes = [v for v in volumes if v is not None]
        valid_amounts = [v for v in amounts if v is not None]
        volume_ratio = _float(latest.get("volume")) / _float(latest.get("avg_volume_prev_n")) if _float(latest.get("avg_volume_prev_n")) else None
        volume_5_20 = _float(latest.get("avg_volume_5")) / _float(latest.get("avg_volume_20")) if _float(latest.get("avg_volume_20")) else None
        amplitude = ((max((_float(item.get("high")) for item in history[:lookback] if _float(item.get("high")) is not None), default=0) / min((_float(item.get("low")) for item in history[:lookback] if _float(item.get("low")) is not None), default=1)) - 1) * 100
        up_volumes = [_float(item.get("volume")) for item in history[:lookback] if _float(item.get("close")) is not None and _float(item.get("prev_close")) is not None and item["close"] > item["prev_close"]]
        down_volumes = [_float(item.get("volume")) for item in history[:lookback] if _float(item.get("close")) is not None and _float(item.get("prev_close")) is not None and item["close"] < item["prev_close"]]
        up_down_volume = sum(up_volumes) / len(up_volumes) / (sum(down_volumes) / len(down_volumes)) if up_volumes and down_volumes and sum(down_volumes) else None
        volume_change = (volume_ratio - 1) * 100 if volume_ratio is not None else None
        row = latest
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
        if c["min_up_days"] is not None and up_days < c["min_up_days"]:
            continue
        if c["max_down_days"] is not None and down_days > c["max_down_days"]:
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
        results.append({"code": code, "date": str(row["date"])[:10],
                        "price": _round(close), "return_pct": _round(ret),
                        "up_days": up_days, "down_days": down_days,
                        "consecutive_up": consecutive_up, "consecutive_down": consecutive_down,
                        "amplitude_pct": _round(amplitude), "amount_avg": _round(sum(valid_amounts) / len(valid_amounts) if valid_amounts else None),
                        "volume_ratio_5": _round(volume_ratio), "volume_5_20": _round(volume_5_20),
                        "up_down_volume": _round(up_down_volume),
                        "volume_change_pct": _round(volume_change),
                        "amount": _round(row.get("amount")), "amount_avg_yi": _round((sum(valid_amounts) / len(valid_amounts) / 1e8) if valid_amounts else None), "turnover": _round(row.get("turn")),
                        "pe_ttm": _round(row.get("pe_ttm")), "pb": _round(row.get("pb_mrq")),
                        "signal_tags": tags,
                        "explanations": explanations})
    industry_lookup = _industry_lookup(warehouse, as_of) if c["industry"] != "ALL" else None
    if industry_lookup is not None:
        results = [item for item in results if item["code"] in industry_lookup]
    _attach_names(results, warehouse, industry_lookup=industry_lookup)
    results = [item for item in results if _matches_identity(item, c)]
    sort_key = sort if sort in {"price", "return_pct", "up_days", "down_days", "volume_ratio_5", "volume_5_20", "turnover", "pe_ttm", "pb", "amount_avg", "amplitude_pct"} else "return_pct"
    results.sort(key=lambda item: (item.get(sort_key) is None, item.get(sort_key) if item.get(sort_key) is not None else 0, item["code"]), reverse=descending)
    total_count = len(results)
    if top_n < total_count:
        results = results[:top_n]
    start = (page - 1) * page_size
    page_items = results[start:start + page_size]
    return {"conditions": c, "as_of": as_of or (results[0]["date"] if results else None),
            "count": len(page_items), "total_count": total_count, "page": page,
            "page_size": page_size, "pages": max(1, math.ceil(total_count / page_size)), "items": page_items}


def _matches_identity(item: dict, conditions: dict) -> bool:
    code = item["code"]
    if conditions["keyword"] and conditions["keyword"].lower() not in (code + " " + item.get("name", "")).lower():
        return False
    if conditions["market"] != "ALL" and not code.startswith(conditions["market"].lower()):
        return False
    if conditions["industry"] != "ALL" and item.get("industry") != conditions["industry"]:
        return False
    from StockInvestmentTool.screener.board import detect_board
    board = detect_board(code)
    if conditions["board"] != "all":
        if conditions["board"] == "main" and board not in ("main_sh", "main_sz"):
            return False
        if conditions["board"] == "cyb" and board != "cyb":
            return False
        if conditions["board"] == "kcb" and board != "kcb":
            return False
        if conditions["board"] == "bse" and board != "bse":
            return False
    for key, low_key, high_key in (("price", "price_min", "price_max"), ("return_pct", "period_return_min", "period_return_max"),
                                   ("up_days", "up_days_min", "up_days_max"), ("down_days", "down_days_min", "down_days_max"),
                                   ("volume_ratio_5", "volume_ratio_min", "volume_ratio_max"), ("volume_5_20", "volume_5_20_min", "volume_5_20_max"),
                                   ("amount_avg_yi", "amount_avg_min", "amount_avg_max"), ("amplitude_pct", "amplitude_min", "amplitude_max"),
                                   ("turnover", "turnover_min", "turnover_max"), ("pe_ttm", "pe_min", "pe_max"),
                                   ("pb", "pb_min", "pb_max"), ("consecutive_up", "consecutive_up_min", "consecutive_up_max"),
                                   ("consecutive_down", "consecutive_down_min", "consecutive_down_max")):
        value = item.get(key)
        if conditions.get(low_key) is not None and (value is None or value < conditions[low_key]):
            return False
        if conditions.get(high_key) is not None and (value is None or value > conditions[high_key]):
            return False
    return True


def stock_series(code: str, *, days: int = 120, as_of: str = "",
                 warehouse: Optional[Warehouse] = None) -> dict:
    """Return local daily series for the discovery detail panel."""
    warehouse = warehouse or Warehouse()
    files = _files(warehouse)
    days = max(20, min(int(days), 750))
    con = duckdb.connect()
    try:
        query = f"SELECT CAST(date AS DATE) AS date, open, close, high, low, volume, amount FROM read_parquet({files}) WHERE code=? "
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
            "open": [_round(x) for x in frame.get("open", [])],
            "close": [_round(x) for x in frame.get("close", [])],
            "high": [_round(x) for x in frame.get("high", [])],
            "low": [_round(x) for x in frame.get("low", [])],
            "volume": [_round(x) for x in frame.get("volume", [])],
            "amount": [_round(x) for x in frame.get("amount", [])]}


def stock_frame_with_indicators(code: str, *, days: int = 750,
                                as_of: str = "",
                                warehouse: Optional[Warehouse] = None,
                                indicator_columns: Optional[list[str]] = None) -> pd.DataFrame:
    """取某标的原始 OHLCV + 统一指标列（indicators 分区），按日期对齐。

    供策略层（operation_points 等）消费统一指标层，避免自算。
    """
    import pandas as _pd
    warehouse = warehouse or Warehouse()
    code_nodot = str(code).lower().replace(".", "")
    files = _files(warehouse)
    days = max(20, min(int(days), 750))
    con = duckdb.connect()
    try:
        query = (f"SELECT CAST(date AS DATE) AS date, open, close, high, low, volume, amount "
                 f"FROM read_parquet({files}) WHERE code=? ")
        params = [code_nodot]
        if as_of:
            query += "AND date <= ? "
            params.append(as_of)
        query += "ORDER BY date DESC LIMIT ?"
        params.append(days)
        frame = con.execute(query, params).fetchdf().sort_values("date")
    finally:
        con.close()
    ind = warehouse.read_indicator_code(code_nodot, days=days)
    if ind is not None and not ind.empty:
        ind = ind.sort_values("date").reset_index(drop=True)
        want = [c for c in (indicator_columns or ["ma5", "ma20", "ma60", "atr14"])
                if c in ind.columns]
        if want:
            frame = frame.merge(ind[["date"] + want], on="date", how="left")
    return frame


def _industry_lookup(warehouse: Warehouse, as_of: str = "") -> dict[str, str]:
    """Return published CSRC industry labels keyed by canonical symbol."""
    from StockInvestmentTool.warehouse.datasets import DatasetAccess

    result = DatasetAccess(warehouse).load_dataset(
        "industry_membership", end_date=as_of or None, required_quality="PASS",
    )
    frame = result.data
    if frame.empty:
        return {}
    frame = frame[frame["industry_classification"].astype(str) == "csrc"]
    return {
        str(row.code): f"{row.industry_code}{row.industry_name}"
        for row in frame.itertuples()
    }


def _attach_names(items: list[dict], warehouse: Warehouse,
                  *, industry_lookup: dict[str, str] | None = None) -> None:
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
        item["industry"] = (industry_lookup or {}).get(item["code"], industry or "")


def _float(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _round(value, digits: int = 2):
    value = _float(value)
    return round(value, digits) if value is not None else None
