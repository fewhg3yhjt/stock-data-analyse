"""Operation points strategy V1.0.

This is intentionally independent from the legacy V4.5/V6 engines.  It turns
an as-of daily frame into an auditable state/price/entry plan and provides a
small T+1 daily backtest with the same calculations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class OperationPointConfig:
    strategy_type: str = "operation_points_v1"
    name: str = "operation_points_default"
    version: str = "1.0"
    lookback_days: int = 20
    ma_slope_days: int = 10
    ma_slope_threshold: float = 0.02
    center_shift_threshold: float = 0.03
    ma_distance_threshold: float = 0.03
    ma20_cross_count_threshold: int = 3
    low_position_threshold: float = 0.25
    stop_atr_k: float = 0.3
    min_rr: float = 2.0
    trend_entry_atr: float = 1.0
    overheat_atr: float = 2.0
    bottoming_lookback: int = 60
    fee_rate: float = 0.0003
    stamp_tax_rate: float = 0.001
    slippage_rate: float = 0.0005

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: Optional[dict] = None):
        values = values or {}
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        data = {key: value for key, value in values.items() if key in allowed}
        result = cls(**data)
        result.validate()
        return result

    def validate(self) -> None:
        if not 5 <= int(self.lookback_days) <= 250:
            raise ValueError("lookback_days 必须在 5 到 250 之间")
        if not 1 <= int(self.ma_slope_days) <= self.lookback_days:
            raise ValueError("ma_slope_days 必须不大于 lookback_days")
        for name in ("ma_slope_threshold", "center_shift_threshold", "ma_distance_threshold"):
            value = float(getattr(self, name))
            if not 0 <= value <= 0.5:
                raise ValueError(f"{name} 必须在 0 到 0.5 之间")
        if not 0 <= float(self.low_position_threshold) <= 1:
            raise ValueError("low_position_threshold 必须在 0 到 1 之间")
        if not 0.05 <= float(self.stop_atr_k) <= 5:
            raise ValueError("stop_atr_k 必须在 0.05 到 5 之间")
        if not 0.1 <= float(self.min_rr) <= 20:
            raise ValueError("min_rr 必须在 0.1 到 20 之间")
        if not 0 <= float(self.trend_entry_atr) <= 5 or not 0 <= float(self.overheat_atr) <= 10:
            raise ValueError("趋势 ATR 参数超出范围")


@dataclass
class OperationPointResult:
    strategy: dict
    state: str
    state_reasons: list[str]
    current_price: Optional[float]
    position20: Optional[float]
    position_label: str
    h20: Optional[float]
    l20: Optional[float]
    center20: Optional[float]
    center_shift: Optional[float]
    ma20: Optional[float]
    ma60: Optional[float]
    atr14: Optional[float]
    ma20_slope: Optional[float]
    ma_distance: Optional[float]
    ma20_cross_count: int
    entry_mode: str
    operation: str
    operation_reason: str
    buy_watch: bool
    bottom_buy_watch: bool
    buy_signal: bool
    plan_buy_price: Optional[float]
    stop_price: Optional[float]
    target1: Optional[float]
    target2: Optional[float]
    risk: Optional[float]
    reward1: Optional[float]
    rr1: Optional[float]
    execution: str
    calculation_as_of: Optional[str]
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


STATES = {
    "STATE_UPTREND": "上涨趋势",
    "STATE_RANGE": "标准震荡",
    "STATE_BOTTOMING": "下跌后筑底",
    "STATE_TOP_RANGE": "上涨后高位震荡",
    "STATE_DOWNTREND": "下降趋势",
    "STATE_UNKNOWN": "数据不足",
}


def add_indicators(frame: pd.DataFrame, config: OperationPointConfig) -> pd.DataFrame:
    """Return a copy with the V1 daily indicators."""
    required = {"date", "open", "high", "low", "close", "volume", "amount"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"日线缺少字段: {', '.join(sorted(missing))}")
    df = frame.copy().sort_values("date").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ma5"] = df["close"].rolling(5, min_periods=5).mean()
    df["ma20"] = df["close"].rolling(20, min_periods=20).mean()
    df["ma60"] = df["close"].rolling(60, min_periods=60).mean()
    previous_close = df["close"].shift(1)
    true_range = pd.concat([
        df["high"] - df["low"],
        (df["high"] - previous_close).abs(),
        (df["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = true_range.rolling(14, min_periods=14).mean()
    df["h20"] = df["high"].rolling(config.lookback_days, min_periods=config.lookback_days).max()
    df["l20"] = df["low"].rolling(config.lookback_days, min_periods=config.lookback_days).min()
    df["center20"] = (df["h20"] + df["l20"]) / 2
    df["ma20_slope"] = df["ma20"].div(df["ma20"].shift(config.ma_slope_days)) - 1
    df["center_shift"] = df["center20"].div(df["center20"].shift(config.ma_slope_days)) - 1
    df["ma_distance"] = (df["ma20"] - df["ma60"]).abs().div(df["ma60"].abs())
    above = df["close"] >= df["ma20"]
    df["ma20_cross"] = above.ne(above.shift(1)).astype(int)
    df["ma20_cross_count"] = df["ma20_cross"].rolling(config.lookback_days, min_periods=config.lookback_days).sum()
    df["position20"] = (df["close"] - df["l20"]).div(df["h20"] - df["l20"])
    df["amount_avg_n"] = df["amount"].rolling(config.lookback_days, min_periods=config.lookback_days).mean()
    df["volume_prev_n_avg"] = df["volume"].shift(1).rolling(config.lookback_days, min_periods=config.lookback_days).mean()
    df["volume_ratio_n"] = df["volume"].div(df["volume_prev_n_avg"])
    return df


def identify_state(df: pd.DataFrame, config: OperationPointConfig) -> tuple[str, list[str]]:
    row = df.iloc[-1]
    values = {key: row.get(key) for key in ("ma20_slope", "center_shift", "ma_distance", "ma20", "ma60", "ma20_cross_count")}
    if any(pd.isna(value) for value in values.values()):
        return "STATE_UNKNOWN", ["历史数据不足以计算全部状态指标"]
    slope, shift, distance = float(values["ma20_slope"]), float(values["center_shift"]), float(values["ma_distance"])
    ma20, ma60 = float(values["ma20"]), float(values["ma60"])
    reasons = []
    if slope > config.ma_slope_threshold and shift > config.center_shift_threshold and ma20 >= ma60:
        return "STATE_UPTREND", ["MA20 斜率明显向上", "20日价格中枢明显上移", "MA20 不低于 MA60"]
    if slope < -config.ma_slope_threshold and shift < -config.center_shift_threshold and ma20 <= ma60:
        return "STATE_DOWNTREND", ["MA20 斜率明显向下", "20日价格中枢明显下移", "MA20 不高于 MA60"]
    stable = [abs(slope) <= config.ma_slope_threshold,
              distance <= config.ma_distance_threshold,
              abs(shift) <= config.center_shift_threshold,
              int(values["ma20_cross_count"]) >= config.ma20_cross_count_threshold]
    if sum(stable) >= 3:
        return "STATE_RANGE", ["MA20 斜率基本平稳", "均线距离较近", "价格中枢相对稳定", "价格多次穿越 MA20"][:sum(stable)]
    long_window = df.tail(config.bottoming_lookback)
    if len(long_window) >= config.bottoming_lookback and slope < 0 and slope > -config.ma_slope_threshold * 2 and abs(shift) <= config.center_shift_threshold:
        recent_lows = long_window["low"].tail(10)
        previous_lows = long_window["low"].head(max(1, len(long_window) - 10))
        if not recent_lows.min() < previous_lows.min():
            return "STATE_BOTTOMING", ["较长周期曾经走弱", "MA20 下行速度减弱", "中枢开始趋稳", "近期未持续刷新结构低点"]
    return "STATE_RANGE", ["未满足趋势或筑底的强条件，按标准震荡观察"]


def calculate(df: pd.DataFrame, config: Optional[OperationPointConfig] = None,
              *, as_of=None) -> OperationPointResult:
    config = config or OperationPointConfig()
    config.validate()
    enriched = add_indicators(df, config)
    if as_of is not None:
        cutoff = pd.to_datetime(as_of)
        enriched = enriched[enriched["date"] <= cutoff].reset_index(drop=True)
    if len(enriched) < max(60, config.bottoming_lookback):
        return OperationPointResult(config.to_dict(), "STATE_UNKNOWN", ["日线历史不足"], None, None, "数据不足", None, None, None, None, None, None, None, None, None, 0, "NONE", "数据不足", False, False, False, None, None, None, None, None, None, None, "无法生成 T+1 计划", str(enriched["date"].iloc[-1])[:10] if len(enriched) else None)
    row = enriched.iloc[-1]
    state, reasons = identify_state(enriched, config)
    close, h20, l20, atr = [float(row[key]) for key in ("close", "h20", "l20", "atr14")]
    width = h20 - l20
    position = max(0.0, min(1.0, (close - l20) / width)) if width > 0 else None
    position_label = "低位区域" if position is not None and position <= .25 else "中低区域" if position is not None and position <= .5 else "中高区域" if position is not None and position <= .75 else "高位区域" if position is not None else "区间无效"
    buy_watch = state == "STATE_RANGE" and position is not None and position <= config.low_position_threshold
    bottom_watch = state == "STATE_BOTTOMING" and position is not None and position <= config.low_position_threshold
    entry_mode = "RANGE_ENTRY" if buy_watch or bottom_watch else "NONE"
    if state == "STATE_UPTREND" and row["ma20"] > row["ma60"] and row["atr14"] > 0:
        distance_atr = (close - float(row["ma20"])) / atr
        if distance_atr > config.overheat_atr:
            entry_mode, operation = "TREND_ENTRY", "NO_BUY"
            operation_reason = "上涨趋势短期偏离 MA20 超过过热阈值，禁止追高"
        elif -0.5 <= distance_atr <= config.trend_entry_atr:
            entry_mode, operation = "TREND_ENTRY", "TREND_PULLBACK_WATCH"
            operation_reason = "上涨趋势中价格位于 MA20/ATR 回踩观察范围"
        else:
            operation, operation_reason = "WAIT", "上涨趋势尚未进入趋势回踩范围"
    elif state == "STATE_DOWNTREND":
        operation, operation_reason = "NO_BUY", "下降趋势，无论区间位置多低都不启动低吸"
    elif buy_watch or bottom_watch:
        operation, operation_reason = "BUY_WATCH", "价格进入低位观察区，仍需等待止跌确认"
    else:
        operation, operation_reason = "WAIT", "当前价格未进入定义的买入观察区域"
    yesterday = enriched.iloc[-2]
    buy_signal = (buy_watch and close > float(yesterday["close"]) and close > float(row["open"]) and float(row["low"]) >= float(yesterday["low"]) - .2 * atr)
    if buy_signal:
        operation = "BUY_SIGNAL"
        operation_reason = "收盘高于昨日和今日开盘，且未明显跌破昨日低点"
    stop = l20 - config.stop_atr_k * atr if entry_mode == "RANGE_ENTRY" else None
    target1, target2 = (h20 + l20) / 2, h20
    plan_price = close
    risk, reward, rr = plan_price - stop if stop else None, target1 - plan_price if stop else None, None
    if risk and risk > 0:
        rr = reward / risk
        if buy_signal and rr < config.min_rr:
            operation, operation_reason = "NO_BUY", f"第一目标盈亏比 {rr:.2f} 低于最低要求 {config.min_rr:.2f}"
            buy_signal = False
    return OperationPointResult(config.to_dict(), state, reasons, close, position, position_label, h20, l20, (h20 + l20) / 2, float(row["center_shift"]), float(row["ma20"]), float(row["ma60"]), atr, float(row["ma20_slope"]), float(row["ma_distance"]), int(row["ma20_cross_count"]), entry_mode, operation, operation_reason, buy_watch, bottom_watch, buy_signal, plan_price, stop, target1, target2, risk, reward, rr, "T 日收盘确认，T+1 交易日开盘执行" if buy_signal else "等待条件满足", str(row["date"])[:10], {"ma5": float(row["ma5"]), "volume_ratio_n": float(row["volume_ratio_n"]) if pd.notna(row["volume_ratio_n"]) else None})


def run_backtest(df: pd.DataFrame, config: Optional[OperationPointConfig] = None) -> dict:
    """Basic T+1 backtest for the V1 signal, with conservative same-day exits."""
    config = config or OperationPointConfig()
    enriched = add_indicators(df, config)
    trades, equity = [], []
    cash = 100000.0
    for index in range(max(60, config.bottoming_lookback), len(enriched) - 1):
        visible = enriched.iloc[:index + 1].copy()
        result = calculate(visible, config)
        if result.operation != "BUY_SIGNAL" or result.rr1 is None:
            equity.append(cash)
            continue
        next_row = enriched.iloc[index + 1]
        entry = float(next_row["open"]) * (1 + config.slippage_rate)
        stop, target = result.stop_price, result.target1
        if stop is None or target is None or entry <= stop or target <= entry:
            continue
        exit_price, reason = None, "期末"
        for future_index in range(index + 1, len(enriched)):
            current = enriched.iloc[future_index]
            if float(current["low"]) <= stop:
                exit_price, reason = stop * (1 - config.slippage_rate), "结构止损"
                exit_date = str(current["date"])[:10]
                break
            if float(current["high"]) >= target:
                exit_price, reason = target * (1 - config.slippage_rate + config.fee_rate), "第一目标"
                exit_date = str(current["date"])[:10]
                break
        if exit_price is None:
            exit_price, exit_date = float(enriched.iloc[-1]["close"]), str(enriched.iloc[-1]["date"])[:10]
        gross = exit_price / entry - 1
        net = gross - config.fee_rate - config.stamp_tax_rate
        trades.append({"signal_date": result.calculation_as_of, "entry_date": str(next_row["date"])[:10], "exit_date": exit_date, "entry": round(entry, 4), "exit": round(exit_price, 4), "return_pct": round(net * 100, 2), "reason": reason, "state": result.state})
    wins = [trade for trade in trades if trade["return_pct"] > 0]
    losses = [trade for trade in trades if trade["return_pct"] <= 0]
    return {"strategy": config.to_dict(), "trades": trades, "trade_count": len(trades), "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0, "average_profit": round(sum(t["return_pct"] for t in wins) / len(wins), 2) if wins else 0, "average_loss": round(sum(t["return_pct"] for t in losses) / len(losses), 2) if losses else 0, "profit_factor": round(sum(t["return_pct"] for t in wins) / abs(sum(t["return_pct"] for t in losses)), 2) if losses and sum(t["return_pct"] for t in losses) else None}
