"""Market discovery on the local daily warehouse.

This module deliberately does not fetch remote quotes.  It creates candidates
from the same daily Parquet data used by indicators and backtests, then emits
explainable price/volume signals for the observation funnel.
"""

from __future__ import annotations

import math
from typing import Optional

import pandas as pd

from StockInvestmentTool.warehouse.storage import Warehouse
from StockInvestmentTool.warehouse.datasets import DatasetAccess, DatasetAccessError


DEFAULT_CONDITIONS = {
    "keyword": "",
    "market": "ALL",
    "board": "ALL",
    "industry": "ALL",
    "category": "csrc",
    "sector_id": "ALL",
    "sector_name": "",
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

CATEGORY_LABELS = {
    "csrc": "证监会行业",
    "ths_industry": "同花顺行业",
    "ths_concept": "同花顺概念",
}


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


def _prepare_daily(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize the Published stock_daily shape for discovery calculations."""
    result = frame.copy()
    if result.empty:
        return result
    result["code"] = result["code"].astype(str).str.lower().str.replace(".", "", regex=False)
    result["date"] = pd.to_datetime(result["date"])
    for column in ("open", "high", "low", "close", "volume", "amount", "turn"):
        if column not in result:
            result[column] = None
        result[column] = pd.to_numeric(result[column], errors="coerce")
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
    current["category"] = str(current.get("category") or "csrc").strip().lower()
    current["sector_id"] = str(current.get("sector_id") or "ALL").strip()
    current["sector_name"] = str(current.get("sector_name") or "").strip()
    if current["category"] not in ("csrc", "ths_industry", "ths_concept"):
        raise ValueError("未知行业分类")
    if current["market"] not in ("ALL", "SH", "SZ", "BJ"):
        raise ValueError("未知市场")
    if current["board"] not in ("all", "main", "cyb", "kcb", "bse"):
        raise ValueError("未知板块")
    if current["signal"] not in ("", "price_up_volume_down", "price_down_volume_up", "volume_spike", "price_up_volume_up"):
        raise ValueError("未知价量信号")
    return current


def _discovery_range(as_of: str, lookback: int, min_history: int) -> tuple[str, str]:
    """Bound the published read while retaining the requested history window."""
    end = pd.Timestamp(as_of) if as_of else pd.Timestamp.now().normalize()
    # A trading year is roughly 250 calendar days. Keep a generous buffer for
    # holidays and suspensions without loading the whole warehouse by default.
    calendar_days = max(180, int(min_history * 2.5 + lookback * 3))
    if min_history > 1000:
        calendar_days = max(calendar_days, 3650)
    return (str((end - pd.Timedelta(days=calendar_days)).date()),
            str(end.date()))


def discover_stocks(conditions: Optional[dict] = None, *, top_n: int = 50,
                    as_of: str = "", warehouse: Optional[Warehouse] = None,
                    page: int = 1, page_size: int = 20,
                    sort: str = "return_pct", descending: bool = True,
                    allow_legacy: bool = False, membership_as_of: str = "") -> dict:
    """Screen the published stock_daily dataset.

    ``allow_legacy`` exists for explicitly marked test/fixture reads only.  It
    is deliberately false for the production business entry point.
    """
    c = _conditions(conditions)
    top_n = max(1, min(int(top_n), 5000))
    page = max(1, int(page))
    page_size = max(10, min(int(page_size), 100))
    warehouse = warehouse or Warehouse()
    lookback = c["lookback_days"]
    data_start, data_end = _discovery_range(as_of, lookback, c["min_history"])
    industry_lookup = None
    category = c["category"]
    membership_as_of = str(membership_as_of or "").strip()[:10]
    membership_context = None
    industry_symbols = None
    try:
        lookup_as_of = membership_as_of if category == "ths_industry" and membership_as_of else as_of
        industry_lookup = _industry_lookup(warehouse, lookup_as_of, category=category)
        membership_context = {"dataset": "ths_industry_membership" if category == "ths_industry" else "industry_membership",
                              "requested_as_of": lookup_as_of or None,
                              "source": "published_dataset"}
        if c["sector_id"] != "ALL":
            # The category-specific sector ID is the authoritative condition.
            # ``industry`` is a legacy display label and may contain a code
            # prefix, so requiring both values would reject valid links.
            industry_symbols = [code for code, label in industry_lookup.items()
                                if label["sector_id"] == c["sector_id"]]
        elif c["industry"] != "ALL" or c["sector_name"]:
            wanted = c["sector_name"] or c["industry"]
            industry_symbols = [code for code, label in industry_lookup.items()
                                if label["sector_name"] == wanted or label["label"] == wanted]
        elif category != "ths_concept":
            industry_symbols = list(industry_lookup)
    except DatasetAccessError:
        if allow_legacy and category == "csrc" and c["industry"] == "ALL" and c["sector_id"] == "ALL":
            industry_lookup = None
        else:
            industry_lookup = {}
    if category in ("ths_concept",) or (industry_lookup == {} and not allow_legacy):
        industry_symbols = []
    daily_result = DatasetAccess(warehouse).load_dataset(
        "stock_daily", start_date=data_start, end_date=data_end,
        symbols=industry_symbols, required_quality="WARNING",
        allow_legacy=allow_legacy,
    )
    daily = _prepare_daily(daily_result.data)
    if as_of:
        daily = daily[daily["date"] <= pd.Timestamp(as_of)]
    rows = []
    for code, group in daily.groupby("code", sort=False):
        group = group.sort_values("date").copy()
        group["prev_close"] = group["close"].shift(1)
        group["old_close"] = group["close"].shift(lookback)
        group["old_volume"] = group["volume"].shift(lookback)
        group["avg_volume_5"] = group["volume"].rolling(5, min_periods=1).mean()
        group["avg_volume_20"] = group["volume"].rolling(20, min_periods=1).mean()
        group["avg_volume_prev_n"] = group["volume"].shift(1).rolling(lookback, min_periods=1).mean()
        group["history_count"] = len(group)
        group["is_up"] = group["close"] > group["prev_close"]
        group["is_down"] = group["close"] < group["prev_close"]
        group["rn"] = range(len(group), 0, -1)
        group["up_days"] = group["is_up"].rolling(lookback, min_periods=1).sum()
        group["down_days"] = group["is_down"].rolling(lookback, min_periods=1).sum()
        rows.extend(group.tail(lookback + 1).to_dict("records"))

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
        day_return = ((close / _float(latest.get("prev_close")) - 1) * 100
                      if close is not None and _float(latest.get("prev_close")) else None)
        results.append({"code": code, "date": str(row["date"])[:10],
                        "price": _round(close), "return_pct": _round(ret),
                        "day_return_pct": _round(day_return),
                        "up_days": up_days, "down_days": down_days,
                        "consecutive_up": consecutive_up, "consecutive_down": consecutive_down,
                        "amplitude_pct": _round(amplitude), "amount_avg": _round(sum(valid_amounts) / len(valid_amounts) if valid_amounts else None),
                        "volume_ratio_5": _round(volume_ratio), "volume_5_20": _round(volume_5_20),
                        "up_down_volume": _round(up_down_volume),
                        "volume_change_pct": _round(volume_change),
                        "amount": _round(row.get("amount")), "amount_avg_yi": _round((sum(valid_amounts) / len(valid_amounts) / 1e8) if valid_amounts else None), "turnover": _round(row.get("turn")),
                         # Valuation is not part of the stock_daily contract.
                         "pe_ttm": None, "pb": None,
                        "signal_tags": tags,
                        "explanations": explanations})
    if industry_lookup is not None:
        results = [item for item in results if item["code"] in industry_lookup]
    _attach_names(results, warehouse, industry_lookup=industry_lookup, category=category)
    category_label = CATEGORY_LABELS[category]
    for item in results:
        item.setdefault("category", category)
        item.setdefault("category_label", category_label)
    results = [item for item in results if _matches_identity(item, c)]
    sort_key = sort if sort in {"price", "return_pct", "up_days", "down_days", "volume_ratio_5", "volume_5_20", "turnover", "pe_ttm", "pb", "amount_avg", "amplitude_pct"} else "return_pct"
    results.sort(key=lambda item: (item.get(sort_key) is None, item.get(sort_key) if item.get(sort_key) is not None else 0, item["code"]), reverse=descending)
    total_count = len(results)
    if top_n < total_count:
        results = results[:top_n]
    start = (page - 1) * page_size
    page_items = results[start:start + page_size]
    actual_as_of = max((item["date"] for item in results), default=None)
    return {"conditions": c, "as_of": actual_as_of,
            "source": {"stock_daily": daily_result.context,
                          "industry_membership": "published" if industry_lookup is not None else None,
                          "category": category,
                         "category_label": category_label,
                        "membership_as_of": membership_as_of or None,
                        "membership_context": membership_context},
            "count": len(page_items), "total_count": total_count, "page": page,
            "page_size": page_size, "pages": max(1, math.ceil(total_count / page_size)), "items": page_items}


def _matches_identity(item: dict, conditions: dict) -> bool:
    code = item["code"]
    if conditions["keyword"] and conditions["keyword"].lower() not in (code + " " + item.get("name", "")).lower():
        return False
    if conditions["market"] != "ALL" and not code.startswith(conditions["market"].lower()):
        return False
    if conditions["industry"] != "ALL" and conditions["sector_id"] == "ALL" and item.get("industry") != conditions["industry"]:
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
                 warehouse: Optional[Warehouse] = None,
                 allow_legacy: bool = False) -> dict:
    """Return a date-bounded series from Published stock_daily."""
    warehouse = warehouse or Warehouse()
    days = max(20, min(int(days), 750))
    result = DatasetAccess(warehouse).load_dataset(
        "stock_daily", symbols=[str(code).lower().replace(".", "")],
        required_quality="WARNING", allow_legacy=allow_legacy,
    )
    frame = _prepare_daily(result.data)
    if as_of:
        frame = frame[frame["date"] <= pd.Timestamp(as_of)]
    frame = frame.sort_values("date").tail(days)
    close = pd.to_numeric(frame["close"], errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    for window in (5, 10, 20, 60):
        frame[f"ma{window}"] = close.rolling(window, min_periods=window).mean()
    volume_ma5 = volume.shift(1).rolling(5, min_periods=5).mean()
    volume_ma20 = volume.shift(1).rolling(20, min_periods=20).mean()
    day_range = pd.to_numeric(frame["high"], errors="coerce") - pd.to_numeric(frame["low"], errors="coerce")
    close_location = (close - pd.to_numeric(frame["low"], errors="coerce")) / day_range.replace(0, pd.NA)
    return {"code": code, "dates": [str(x)[:10] for x in frame["date"]],
            "open": [_round(x) for x in frame.get("open", [])],
            "close": [_round(x) for x in frame.get("close", [])],
            "high": [_round(x) for x in frame.get("high", [])],
            "low": [_round(x) for x in frame.get("low", [])],
             "volume": [_round(x) for x in frame.get("volume", [])],
              "amount": [_round(x) for x in frame.get("amount", [])],
             "ma": {f"ma{window}": [_round(x) for x in frame[f"ma{window}"]]
                    for window in (5, 10, 20, 60)},
             "volume_facts": {
                 "volume": _round(volume.iloc[-1]) if len(volume) else None,
                 "volume_ma5": _round(volume_ma5.iloc[-1]) if len(volume_ma5) else None,
                 "volume_ma20": _round(volume_ma20.iloc[-1]) if len(volume_ma20) else None,
                 "volume_ratio_5": _round(volume.iloc[-1] / volume_ma5.iloc[-1]) if len(volume) and pd.notna(volume_ma5.iloc[-1]) and volume_ma5.iloc[-1] else None,
                 "day_return_pct": _round((close.iloc[-1] / close.iloc[-2] - 1) * 100) if len(close) > 1 and close.iloc[-2] else None,
                 "close_location": _round(close_location.iloc[-1]) if len(close_location) else None,
             },
             "context": {"stock_daily": result.context}}


def stock_frame_with_indicators(code: str, *, days: int = 750,
                                as_of: str = "",
                                 warehouse: Optional[Warehouse] = None,
                                 indicator_columns: Optional[list[str]] = None,
                                 allow_legacy: bool = False) -> pd.DataFrame:
    """取某标的原始 OHLCV + 统一指标列（indicators 分区），按日期对齐。

    供策略层（operation_points 等）消费统一指标层，避免自算。
    """
    warehouse = warehouse or Warehouse()
    code_nodot = str(code).lower().replace(".", "")
    days = max(20, min(int(days), 750))
    daily_result = DatasetAccess(warehouse).load_dataset(
        "stock_daily", symbols=[code_nodot],
        required_quality="WARNING", allow_legacy=allow_legacy,
    )
    frame = _prepare_daily(daily_result.data)
    if as_of:
        frame = frame[frame["date"] <= pd.Timestamp(as_of)].tail(days)
    frame = frame.sort_values("date").tail(days)
    try:
        indicator_result = DatasetAccess(warehouse).load_dataset(
            "indicators", symbols=[code_nodot],
            required_quality="PASS", allow_legacy=False,
        )
        ind = indicator_result.data.copy()
    except DatasetAccessError:
        ind = None
        indicator_result = None
    if ind is not None and not ind.empty:
        ind["date"] = pd.to_datetime(ind["date"])
        if as_of:
            ind = ind[ind["date"] <= pd.Timestamp(as_of)]
        ind = ind.sort_values("date").reset_index(drop=True)
        want = [c for c in (indicator_columns or ["ma5", "ma20", "ma60", "atr14"])
                if c in ind.columns]
        if want:
            frame = frame.merge(ind[["date"] + want], on="date", how="left")
    return frame


def _industry_lookup(warehouse: Warehouse, as_of: str = "", *, category: str = "csrc") -> dict[str, dict]:
    """Return published CSRC industry labels keyed by canonical symbol."""
    from StockInvestmentTool.warehouse.datasets import DatasetAccess

    if category == "ths_concept":
        return {}
    dataset = "ths_industry_membership" if category == "ths_industry" else "industry_membership"
    result = DatasetAccess(warehouse).load_dataset(
        dataset, end_date=as_of or None, required_quality="PASS",
        allow_legacy=False,
    )
    frame = result.data
    if frame.empty:
        return {}
    if category == "csrc":
        frame = frame[frame["industry_classification"].astype(str) == "csrc"]
        return {str(row.code).lower().replace(".", ""): {"label": f"{row.industry_code}{row.industry_name}", "sector_id": str(row.industry_code), "sector_name": str(row.industry_name)} for row in frame.itertuples()}
    lookup = {}
    for row in frame.itertuples():
        code = str(row.code).strip().lower().replace(".", "")
        if code:
            lookup[code] = {"label": f"{row.industry_id}{row.industry_name}", "sector_id": str(row.industry_id),
                            "sector_name": str(row.industry_name)}
    return lookup


def _attach_names(items: list[dict], warehouse: Warehouse,
                  *, industry_lookup: dict[str, dict] | None = None,
                  category: str = "csrc") -> None:
    if not items:
        return
    codes = [item["code"] for item in items]
    lookup = {code: (item.get("name", ""), item.get("industry", ""))
              for code, item in warehouse.get_instruments(codes).items()}
    for item in items:
        name, industry = lookup.get(item["code"], ("", ""))
        item["name"] = name or item["code"]
        meta = (industry_lookup or {}).get(item["code"], {})
        item["industry"] = (meta.get("label", "")
                             if industry_lookup is not None else industry or "")
        item["category"] = category
        if meta:
            item.update({"category_label": CATEGORY_LABELS.get(category, category),
                         "sector_id": meta["sector_id"],
                         "sector_name": meta.get("sector_name", meta["label"]),
                         "as_of": item.get("date")})
        else:
            item["category_label"] = CATEGORY_LABELS.get(category, category)


def _float(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _round(value, digits: int = 2):
    value = _float(value)
    return round(value, digits) if value is not None else None
