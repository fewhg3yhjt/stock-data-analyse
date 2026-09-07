"""Minute take-profit V11 shadow/notify state machine.

The current Tencent minute feed exposes the per-minute close path plus volume
and amount. V11 therefore builds its intraday structure from that path and
never creates an automatic sell action.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)
V11_EVENT_TYPE = "MINUTE_TAKE_PROFIT_V11"


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _config() -> dict:
    return {
        "mode": os.getenv("POSITION_V11_MODE", "notify").strip().lower(),
        "candidate_atr": _float_env("POSITION_V11_CANDIDATE_ATR", 0.75),
        "swing_atr": _float_env("POSITION_V11_SWING_ATR", 0.75),
        "confirmation_bars": max(1, int(os.getenv("POSITION_V11_CONFIRMATION_BARS", "3"))),
        "volume_surge_ratio": _float_env("POSITION_V11_VOLUME_SURGE_RATIO", 1.8),
    }


def _minute_frame(symbol: str, day: str | None = None) -> pd.DataFrame:
    from StockInvestmentTool.warehouse.minute import MinuteStore, normalize_minute_code

    target = day or datetime.now().strftime("%Y-%m-%d")
    frame = MinuteStore().read(target, normalize_minute_code(symbol))
    if frame is None or frame.empty:
        return pd.DataFrame()
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
    frame = frame.dropna(subset=["time", "close"]).sort_values("time")
    return frame[(frame["time"].dt.strftime("%H:%M") >= "09:30") &
                 (frame["time"].dt.strftime("%H:%M") <= "15:00")].reset_index(drop=True)


def _daily_context(symbol: str, day: str) -> tuple[float | None, float | None]:
    """Return T-1 close and T-1 ATR14 from published daily data only."""
    try:
        from StockInvestmentTool.portfolio.monitor import PriceMonitor

        end = (pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        frame = PriceMonitor().fetch_kline(
            symbol, start_date=(pd.Timestamp(day) - pd.Timedelta(days=90)).strftime("%Y-%m-%d"),
            end_date=end,
        )
        if frame is None or frame.empty:
            return None, None
        frame = frame.sort_values("date")
        last = frame.iloc[-1]
        prev_close = float(last["close"])
        atr = last.get("atr14")
        if atr is None or pd.isna(atr):
            high = pd.to_numeric(frame["high"], errors="coerce")
            low = pd.to_numeric(frame["low"], errors="coerce")
            close = pd.to_numeric(frame["close"], errors="coerce")
            tr = pd.concat([high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
            atr = tr.rolling(14).mean().iloc[-1]
        return prev_close, float(atr) if atr is not None and not pd.isna(atr) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("V11 daily context unavailable %s: %s", symbol, exc)
        return None, None


def _swings(closes: list[float], threshold: float) -> tuple[list[float], list[float]]:
    """Compress close path into effective highs/lows using an ATR price step."""
    if not closes or threshold <= 0:
        return [], []
    direction = 0
    anchor = closes[0]
    high = anchor
    low = anchor
    highs: list[float] = []
    lows: list[float] = []
    for price in closes[1:]:
        high = max(high, price)
        low = min(low, price)
        if direction >= 0 and high - price >= threshold:
            highs.append(high)
            anchor = price
            direction = -1
            high = price
            low = price
        elif direction <= 0 and price - low >= threshold:
            lows.append(low)
            anchor = price
            direction = 1
            high = price
            low = price
        elif abs(price - anchor) >= threshold and direction == 0:
            direction = 1 if price > anchor else -1
    return highs, lows


def evaluate(cycle: dict, *, current_price: float | None, prior_context: dict | None = None) -> dict:
    """Evaluate one position and return V11 context plus optional notification."""
    if current_price is None:
        return {"state": "DATA_UNAVAILABLE", "notify": False, "context": {"v11_status": "DATA_UNAVAILABLE"}}
    config = _config()
    day = datetime.now().strftime("%Y-%m-%d")
    frame = _minute_frame(cycle["symbol"], day)
    if frame.empty:
        return {"state": "DATA_UNAVAILABLE", "notify": False, "context": {"v11_status": "DATA_UNAVAILABLE"}}
    closes = frame["close"].astype(float).tolist()
    day_open = float(closes[0])
    running_high = max(closes)
    running_low = min(closes)
    prev_close, atr = _daily_context(cycle["symbol"], day)
    cycle_cost = cycle["average_cost"] if "average_cost" in cycle.keys() else None
    avg_cost = float((prior_context or {}).get("average_cost") or cycle_cost or 0)
    profit_pct = (current_price / avg_cost - 1) if avg_cost else None
    max_profit_pct = (running_high / avg_cost - 1) if avg_cost else None
    atr_pct = atr / prev_close if atr and prev_close else None
    drawdown_pct = (running_high - current_price) / running_high if running_high else 0.0
    drawdown_atr = (running_high - current_price) / atr if atr else None
    swing_step = (atr or 0) * config["swing_atr"]
    highs, lows = _swings(closes, swing_step)
    lower_high = len(highs) >= 2 and highs[-1] < highs[-2]
    lower_low = len(lows) >= 2 and lows[-1] < lows[-2]
    break_open = current_price < day_open
    volume_mean = frame["volume"].rolling(10, min_periods=3).mean().iloc[-1]
    recent_amount = frame["amount"].tail(3).mean() if "amount" in frame else 0
    amount_mean = frame["amount"].rolling(20, min_periods=5).mean().iloc[-1] if "amount" in frame else 0
    volume_weak = bool(amount_mean and recent_amount > amount_mean * config["volume_surge_ratio"] and current_price < running_high)
    profitable = profit_pct is not None and profit_pct > 0
    candidate = profitable and drawdown_atr is not None and drawdown_atr >= config["candidate_atr"]
    structure = lower_high and lower_low
    if not profitable and (lower_low or break_open):
        status = "STOP_LOSS"
    elif candidate and structure:
        status = "TAKE_PROFIT"
    elif candidate and (break_open or lower_high or lower_low or volume_weak):
        status = "TAKE_PROFIT_PENDING"
    elif candidate:
        status = "REVERSAL_CANDIDATE"
    else:
        status = "HOLD"
    context = {
        "v11_status": status, "v11_mode": config["mode"], "v11_as_of": str(frame["time"].iloc[-1]),
        "entry_price": avg_cost, "prev_close": prev_close, "day_open": day_open,
        "running_high": running_high, "running_low": running_low, "current_price": current_price,
        "profit_pct": profit_pct, "max_profit_pct": max_profit_pct, "drawdown_pct": drawdown_pct,
        "atr_pct": atr_pct, "drawdown_atr": drawdown_atr, "effective_swing_atr": config["swing_atr"],
        "lower_high": lower_high, "lower_low": lower_low, "break_open": break_open,
        "volume_weak": volume_weak, "candidate_atr": config["candidate_atr"],
    }
    previous = (prior_context or {}).get("v11_status")
    notify = config["mode"] in {"notify", "shadow"} and status in {"TAKE_PROFIT_PENDING", "TAKE_PROFIT", "STOP_LOSS"} and status != previous
    return {"state": status, "notify": notify, "context": context}


def notification_payload(cycle: dict, result: dict) -> dict:
    context = result["context"]
    symbol = str(cycle["symbol"] if "symbol" in cycle.keys() else "")
    display_name = symbol
    try:
        from StockInvestmentTool.warehouse.storage import Warehouse
        instrument = Warehouse().get_instrument(symbol)
        display_name = (instrument or {}).get("name") or symbol
    except Exception:
        pass
    status_labels = {
        "STOP_LOSS": "风险提醒",
        "TAKE_PROFIT_PENDING": "止盈待确认",
        "TAKE_PROFIT": "止盈提醒",
    }
    status = result["state"]
    reasons = []
    if context.get("profit_pct") is not None and context["profit_pct"] <= 0:
        reasons.append(f"当前价未高于持仓均价（收益率 {context['profit_pct']:.2%}）")
    if context.get("break_open"):
        reasons.append(f"当前价 {context['current_price']:.4f} 跌破当日开盘 {context['day_open']:.4f}")
    if context.get("lower_high"):
        reasons.append("分钟走势形成更低的高点（Lower High）")
    if context.get("lower_low"):
        reasons.append("分钟走势形成更低的低点（Lower Low）")
    if context.get("volume_weak"):
        reasons.append("最近分钟成交额放大且价格低于盘中高点")
    reason = "；".join(reasons) or "达到 V11 状态判断条件"
    atr = (context.get("prev_close") or 0) * (context.get("atr_pct") or 0)
    drawdown = (context.get("running_high") or 0) - (context.get("current_price") or 0)
    threshold = atr * (context.get("candidate_atr") or 0)
    return {
        "subject": f"[分钟止盈V11·{status_labels.get(status, status)}] {display_name}（{symbol}）",
        "text": (
            f"分钟级止盈 V11：{status_labels.get(status, status)}\n"
            f"证券：{display_name}\n"
            f"代码：{symbol}\n"
            f"数据时间：{context.get('v11_as_of', '暂无')}\n"
            f"当前价：{context.get('current_price', '暂无')}\n"
            f"持仓均价：{context.get('entry_price', '暂无')}\n"
            f"当日开盘：{context.get('day_open', '暂无')}\n"
            f"盘中最高：{context.get('running_high', '暂无')}\n"
            f"前一日收盘：{context.get('prev_close', '暂无')}\n"
            f"前一日 ATR14：{atr:.4f}\n"
            f"实际回撤：{drawdown:.4f} = 盘中最高 {context.get('running_high', '暂无')} - 当前价 {context.get('current_price', '暂无')}\n"
            f"回撤 ATR 倍数：{context.get('drawdown_atr', '暂无')} = 实际回撤 ÷ ATR14\n"
            f"候选门槛：{context.get('candidate_atr', 0.75)} ATR = {threshold:.4f}\n"
            f"触发依据：{reason}\n"
            "处理方式：仅发送提醒，不自动下单、不自动卖出。"
        ),
        "action": "NOTIFY", "context": context,
    }
