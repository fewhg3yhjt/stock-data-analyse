"""交易事件派生指标。

技术指标由 IndicatorContext 负责；本模块只处理依赖交易事件的数据，保证
Advisor、回测和复盘使用相同的后高/后低口径，而由调用方决定可见数据范围。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd


@dataclass
class TradeMetrics:
    post_high: float
    post_low: float
    post_high_at: Optional[str] = None
    post_low_at: Optional[str] = None
    post_high_source: Optional[str] = None
    post_low_source: Optional[str] = None
    source_field: Optional[str] = None
    price_basis: str = "raw"
    is_provisional: bool = False
    calculation_as_of: Optional[str] = None
    covered_minute_days: list[str] = field(default_factory=list)
    daily_fallback_days: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _date(value) -> Optional[pd.Timestamp]:
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.normalize()


def calculate_post_metrics(*, buy_date: str, buy_price: float,
                           daily: Optional[pd.DataFrame] = None,
                           minute: Optional[pd.DataFrame] = None,
                           as_of=None, price_basis: str = "raw") -> TradeMetrics:
    """Calculate post-high/post-low strictly after ``buy_date``.

    Minute ``high`` is preferred when present. Existing Tencent files contain
    one-minute close prices only, so close is used as an explicitly reported
    proxy. Daily high/low fills only trading days not covered by minute data.
    """
    buy_day = _date(buy_date)
    if buy_day is None:
        raise ValueError("买入日期无效")
    price = float(buy_price or 0)
    if price <= 0:
        raise ValueError("买入价格必须大于 0")
    cutoff = pd.to_datetime(as_of, errors="coerce") if as_of is not None else None
    if cutoff is not None and pd.isna(cutoff):
        cutoff = None
    metrics = TradeMetrics(post_high=price, post_low=price, price_basis=price_basis,
                           calculation_as_of=cutoff.isoformat() if cutoff is not None else None)
    minute_days: set[str] = set()
    candidates_high: list[tuple[float, str, str]] = []
    candidates_low: list[tuple[float, str, str]] = []

    if minute is not None and not minute.empty:
        frame = minute.copy()
        time_col = "time" if "time" in frame.columns else "trade_date"
        frame["_time"] = pd.to_datetime(frame[time_col], errors="coerce")
        frame = frame.dropna(subset=["_time"])
        frame = frame[frame["_time"].dt.normalize() > buy_day]
        if cutoff is not None:
            frame = frame[frame["_time"] <= cutoff]
        if not frame.empty:
            frame["_day"] = frame["_time"].dt.strftime("%Y-%m-%d")
            minute_days = set(frame["_day"])
            high_col = "high" if "high" in frame.columns and frame["high"].notna().any() else "close"
            low_col = "low" if "low" in frame.columns and frame["low"].notna().any() else "close"
            metrics.source_field = high_col if high_col == low_col else f"{high_col}/{low_col}"
            for _, row in frame.iterrows():
                high = pd.to_numeric(row.get(high_col), errors="coerce")
                low = pd.to_numeric(row.get(low_col), errors="coerce")
                stamp = row["_time"].strftime("%Y-%m-%d %H:%M:%S")
                if pd.notna(high):
                    candidates_high.append((float(high), stamp, "minute"))
                if pd.notna(low):
                    candidates_low.append((float(low), stamp, "minute"))

    if daily is not None and not daily.empty:
        frame = daily.copy()
        if "date" not in frame.columns:
            raise ValueError("日线数据缺少 date 字段")
        frame["_day"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
        frame = frame.dropna(subset=["_day"])
        frame = frame[frame["_day"] > buy_day]
        if cutoff is not None:
            frame = frame[frame["_day"] <= cutoff.normalize()]
        fallback_days: set[str] = set()
        for _, row in frame.iterrows():
            day = row["_day"].strftime("%Y-%m-%d")
            if day in minute_days:
                continue
            fallback_days.add(day)
            high = pd.to_numeric(row.get("high"), errors="coerce")
            low = pd.to_numeric(row.get("low"), errors="coerce")
            if pd.notna(high):
                candidates_high.append((float(high), day, "daily"))
            if pd.notna(low):
                candidates_low.append((float(low), day, "daily"))
        metrics.daily_fallback_days = sorted(fallback_days)

    if candidates_high:
        value, stamp, source = max(candidates_high, key=lambda item: item[0])
        metrics.post_high, metrics.post_high_at, metrics.post_high_source = max(price, value), stamp, source
    if candidates_low:
        value, stamp, source = min(candidates_low, key=lambda item: item[0])
        metrics.post_low, metrics.post_low_at, metrics.post_low_source = min(price, value), stamp, source
    metrics.covered_minute_days = sorted(minute_days)
    if cutoff is not None and cutoff.date() == datetime.now().date():
        metrics.is_provisional = True
    return metrics


def load_local_minute(code: str, *, warehouse=None) -> Optional[pd.DataFrame]:
    """Load local minute rows for one code without contacting a provider."""
    from StockInvestmentTool.warehouse.storage import Warehouse
    from StockInvestmentTool.warehouse.minute import normalize_minute_code

    warehouse = warehouse or Warehouse()
    store = warehouse.minute_store()
    frames = []
    normalized = normalize_minute_code(code)
    for day in store.days():
        frame = store.read(day, normalized)
        if not frame.empty:
            frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else None
