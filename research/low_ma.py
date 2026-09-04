"""Auditable LowMA pullback research strategy.

This module deliberately does not alter the production strategy engines.  It
owns the research-specific semantics that the existing next-open simulator
cannot express: a signal at T creates a limit order for T+1, and the order is
filled only when T+1's OHLC range covers the limit price.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class LowMAConfig:
    """Configuration for one isolated LowMA experiment."""

    entry_path: str = "path_a"
    limit_period: int = 5
    position_limit: float = 0.25
    close_location_low: float = 1 / 3
    close_location_high: float = 2 / 3
    sell_volume_ratio_low: float = 0.8
    sell_volume_ratio_high: float = 1.2
    structure_confirm_days: int = 2
    breakeven_activation: float | None = 0.05
    trend_activation: float | None = 0.10
    trailing_drawdown: float | None = 0.07
    hard_stop_pct: float | None = None
    range_exit_position: float = 0.80

    def __post_init__(self) -> None:
        if self.entry_path not in {"baseline", "path_a", "path_b"}:
            raise ValueError("entry_path must be baseline, path_a, or path_b")
        if self.limit_period not in {5, 10, 20}:
            raise ValueError("limit_period must be 5, 10, or 20")
        if self.structure_confirm_days < 1:
            raise ValueError("structure_confirm_days must be >= 1")
        for name in (
            "position_limit", "close_location_low", "close_location_high",
            "range_exit_position",
        ):
            value = float(getattr(self, name))
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if float(self.sell_volume_ratio_low) < 0 or float(self.sell_volume_ratio_high) <= 0:
            raise ValueError("sell volume ratios must be non-negative")
        for name in ("breakeven_activation", "trend_activation", "trailing_drawdown", "hard_stop_pct"):
            value = getattr(self, name)
            if value is not None and not 0 < float(value) < 1:
                raise ValueError(f"{name} must be between 0 and 1")


def build_low_ma_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Build all features from the current row and its historical rows only."""

    required = {"date", "code", "open", "high", "low", "close", "volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"LowMA data missing columns: {sorted(missing)}")
    result = frame.copy()
    result["date"] = pd.to_datetime(result["date"])
    result = result.sort_values(["code", "date"]).drop_duplicates(["code", "date"])
    groups = result.groupby("code", group_keys=False)
    result["high20"] = groups["high"].transform(lambda s: s.rolling(20, min_periods=20).max())
    result["low20"] = groups["low"].transform(lambda s: s.rolling(20, min_periods=20).min())
    denominator = result["high20"] - result["low20"]
    result["position20"] = (result["close"] - result["low20"]) / denominator.replace(0, np.nan)
    for period in (5, 10, 20):
        result[f"lowma{period}"] = groups["low"].transform(
            lambda s, n=period: s.rolling(n, min_periods=n).mean()
        )
    result["prev_close"] = groups["close"].shift(1)
    result["prior20_low"] = groups["low"].transform(
        lambda s: s.shift(1).rolling(20, min_periods=20).min()
    )
    result["new20_low"] = result["low"] < result["prior20_low"]
    result["volume_avg5_prior"] = groups["volume"].transform(
        lambda s: s.shift(1).rolling(5, min_periods=5).mean()
    )
    result["volume_down"] = (
        (result["close"] < result["prev_close"])
        & (result["volume"] > result["volume_avg5_prior"])
    )
    result["base_qualified"] = (
        (result["position20"] <= 0.25)
        & ~result["new20_low"]
        & ~groups["new20_low"].shift(1).fillna(False).astype(bool)
        & ~result["volume_down"]
        & ~groups["volume_down"].shift(1).fillna(False).astype(bool)
    )
    day_range = result["high"] - result["low"]
    result["close_location"] = (result["close"] - result["low"]) / day_range.replace(0, np.nan)
    result["down_volume"] = result["volume"].where(
        result["close"] < result["prev_close"]
    )
    result["recent_down_volume_avg"] = result.groupby("code")["down_volume"].transform(
        lambda s: s.shift(1).rolling(5, min_periods=1).mean()
    )
    result["sell_volume_ratio"] = result["volume"] / result["recent_down_volume_avg"]
    result["path_a"] = (
        result["base_qualified"]
        & result["close_location"].gt(1 / 3)
        & result["close_location"].le(2 / 3)
        & result["sell_volume_ratio"].ge(0.8)
        & result["sell_volume_ratio"].le(1.2)
    )
    result["path_b"] = (
        result["base_qualified"]
        & result["close_location"].le(1 / 3)
        & result["sell_volume_ratio"].lt(0.8)
    )
    return result.drop(columns=["down_volume"]).reset_index(drop=True)


def _fill_price(row: pd.Series, limit_price: float) -> float | None:
    """Model a next-day limit buy with price improvement at the open."""

    open_price = float(row["open"])
    if float(row["low"]) <= open_price <= limit_price:
        return open_price
    if float(row["low"]) > limit_price or float(row["high"]) < limit_price:
        return None
    return limit_price


def _sell_price(row: pd.Series, stop_price: float | None = None) -> tuple[float, str] | None:
    """Return conservative daily stop execution, including gap-through stops."""

    if stop_price is None:
        return float(row["open"]), "next_open"
    if float(row["open"]) <= stop_price:
        return float(row["open"]), "gap_through_stop"
    if float(row["low"]) <= stop_price:
        return stop_price, "stop_price"
    return float(row["open"]), "next_open"


def _trade_result(position: dict[str, Any], exit_row: pd.Series, signal_date: str,
                  exit_reason: str, execution_type: str, end_index: int) -> dict[str, Any]:
    entry_price = float(position["entry_price"])
    section = position["frame"].iloc[position["entry_index"]: end_index + 1]
    exit_price = float(exit_row["exit_price"])
    return {
        "stock": position["stock"],
        "code": position["code"],
        "qualification_date": position["qualification_date"],
        "position20": position["position20"],
        "lowma5": position["lowma5"],
        "lowma10": position["lowma10"],
        "lowma20": position["lowma20"],
        "limit_price": position["limit_price"],
        "entry_date": position["entry_date"],
        "entry_open": position["entry_open"],
        "entry_high": position["entry_high"],
        "entry_low": position["entry_low"],
        "entry_close": position["entry_close"],
        "entry_price": entry_price,
        "exit_signal_date": signal_date,
        "exit_date": exit_row["date"],
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "exit_execution_type": execution_type,
        "forced_settlement": False,
        "holding_days": end_index - position["entry_index"],
        "return_pct": (exit_price / entry_price - 1) * 100,
        "mae_pct": (float(section["low"].min()) / entry_price - 1) * 100,
        "mfe_pct": (float(section["high"].max()) / entry_price - 1) * 100,
    }


def run_low_ma_experiment(frame: pd.DataFrame, config: LowMAConfig | None = None) -> dict[str, pd.DataFrame]:
    """Run one or more symbols and return auditable events, trades and summaries.

    Signals are evaluated on row T.  Entries and exits execute on T+1.  A
    caller should supply a warm-up range before the report start date; this
    function does not silently fetch or invent missing history.
    """

    config = config or LowMAConfig()
    data = build_low_ma_features(frame)
    data["base_qualified"] = (
        (data["position20"] <= config.position_limit)
        & ~data["new20_low"]
        & ~data.groupby("code")["new20_low"].shift(1).fillna(False).astype(bool)
        & ~data["volume_down"]
        & ~data.groupby("code")["volume_down"].shift(1).fillna(False).astype(bool)
    )
    data["path_a"] = (
        data["base_qualified"]
        & data["close_location"].gt(config.close_location_low)
        & data["close_location"].le(config.close_location_high)
        & data["sell_volume_ratio"].ge(config.sell_volume_ratio_low)
        & data["sell_volume_ratio"].le(config.sell_volume_ratio_high)
    )
    data["path_b"] = (
        data["base_qualified"]
        & data["close_location"].le(config.close_location_low)
        & data["sell_volume_ratio"].lt(config.sell_volume_ratio_low)
    )
    trades: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for code, source in data.groupby("code", sort=True):
        stock_frame = source.reset_index(drop=True)
        stock = str(code)
        position: dict[str, Any] | None = None
        pending_exit: tuple[int, str, float | None] | None = None
        exited_today = False
        below_reference_days = 0
        breakeven_armed = False
        trailing_armed = False
        peak_high = 0.0
        for i, row in stock_frame.iterrows():
            if position is not None and pending_exit is not None:
                signal_i, reason, stop_price = pending_exit
                execution = _sell_price(row, stop_price)
                if execution is not None:
                    price, execution_type = execution
                    exit_row = row.copy()
                    exit_row["exit_price"] = price
                    trades.append(_trade_result(position, exit_row, str(stock_frame.iloc[signal_i]["date"])[:10], reason, execution_type, i))
                    position = None
                    pending_exit = None
                    below_reference_days = 0
                    breakeven_armed = False
                    trailing_armed = False
                    exited_today = True

            if exited_today:
                exited_today = False
                continue

            if position is None and i > 0:
                signal = stock_frame.iloc[i - 1]
                path_passed = (
                    bool(signal["base_qualified"])
                    if config.entry_path == "baseline"
                    else bool(signal[config.entry_path])
                )
                limit_value = signal[f"lowma{config.limit_period}"]
                if path_passed and pd.notna(limit_value):
                    limit_price = float(limit_value)
                    fill_price = _fill_price(row, limit_price)
                    filled = fill_price is not None
                    events.append({
                        "date": row["date"], "stock": stock, "code": code,
                        "event": "LIMIT_FILLED" if filled else "LIMIT_NOT_FILLED",
                        "qualification_date": signal["date"], "position20": signal["position20"],
                        "lowma5": signal["lowma5"], "lowma10": signal["lowma10"], "lowma20": signal["lowma20"],
                        "limit_price": limit_price, "open": row["open"], "high": row["high"],
                        "low": row["low"], "close": row["close"], "filled": filled,
                        "execution_price": fill_price,
                        "reason": "T+1 range covers limit; open price improvement allowed" if filled else "T+1 range does not cover limit",
                    })
                    if filled:
                        position = {
                            "stock": stock, "code": code, "qualification_date": str(signal["date"])[:10],
                            "position20": float(signal["position20"]), "lowma5": float(signal["lowma5"]),
                            "lowma10": float(signal["lowma10"]), "lowma20": float(signal["lowma20"]),
                            "limit_price": limit_price, "entry_date": str(row["date"])[:10],
                            "entry_open": float(row["open"]), "entry_high": float(row["high"]),
                            "entry_low": float(row["low"]), "entry_close": float(row["close"]),
                            "entry_price": float(fill_price), "entry_index": i, "frame": stock_frame,
                            "reference_low20": float(signal["low20"]),
                        }
                        peak_high = float(row["high"])

            if position is None or i <= position["entry_index"] or pending_exit is not None:
                continue
            peak_high = max(peak_high, float(row["high"]))
            peak_return = peak_high / position["entry_price"] - 1
            below_reference_days = below_reference_days + 1 if float(row["close"]) < position["reference_low20"] else 0
            if config.breakeven_activation is not None:
                breakeven_armed = breakeven_armed or peak_return >= config.breakeven_activation
            if config.trend_activation is not None:
                trailing_armed = trailing_armed or peak_return >= config.trend_activation

            stop_price = None
            reason = None
            if config.hard_stop_pct is not None and float(row["low"]) <= position["entry_price"] * (1 - config.hard_stop_pct):
                stop_price = position["entry_price"] * (1 - config.hard_stop_pct)
                reason = "HARD_STOP"
            elif below_reference_days >= config.structure_confirm_days:
                reason = "STRUCTURE_STOP"
            elif trailing_armed and config.trailing_drawdown is not None and float(row["close"]) <= peak_high * (1 - config.trailing_drawdown):
                reason = "RIGHT_TRAILING"
            elif breakeven_armed and float(row["close"]) <= position["entry_price"]:
                reason = "BREAKEVEN_PROTECTION"
            elif not trailing_armed and float(row["position20"]) >= config.range_exit_position:
                reason = "RANGE_EXIT"
            if reason:
                pending_exit = (i, reason, stop_price)
                events.append({
                    "date": row["date"], "stock": stock, "code": code,
                    "event": "EXIT_SIGNAL", "exit_reason": reason,
                    "close": row["close"], "position20": row["position20"],
                    "peak_high": peak_high, "peak_return_pct": peak_return * 100,
                    "below_reference_days": below_reference_days,
                    "breakeven_armed": breakeven_armed, "trailing_armed": trailing_armed,
                    "stop_price": stop_price,
                })

        if position is not None:
            final = stock_frame.iloc[-1]
            final_row = final.copy()
            final_row["exit_price"] = float(final["close"])
            trade = _trade_result(position, final_row, "", "END_OF_PERIOD", "close", len(stock_frame) - 1)
            trade["forced_settlement"] = True
            trades.append(trade)

    trades_df = pd.DataFrame(trades)
    events_df = pd.DataFrame(events)
    if not trades_df.empty:
        summaries = []
        for stock, group in trades_df.groupby("stock", sort=True):
            summaries.append(_summary_row(stock, group))
        summary_df = pd.DataFrame(summaries)
    else:
        summary_df = pd.DataFrame()
    kline_rows = data[[
        "code", "date", "open", "high", "low", "close", "volume",
        "lowma5", "lowma10", "lowma20", "high20", "low20", "position20",
    ]].copy()
    kline_rows["date"] = kline_rows["date"].dt.strftime("%Y-%m-%d")
    curve_rows: list[dict[str, Any]] = []
    for stock, stock_kline in kline_rows.groupby("code", sort=True):
        stock_trades = trades_df[trades_df["code"].astype(str) == str(stock)] if not trades_df.empty else pd.DataFrame()
        equity = 1.0
        active = None
        for _, row in stock_kline.iterrows():
            date = row["date"]
            if active is None and not stock_trades.empty:
                matches = stock_trades[stock_trades["entry_date"] == date]
                if not matches.empty:
                    active = matches.iloc[0]
            if active is not None:
                entry_date = active["entry_date"]
                exit_date = active["exit_date"]
                entry_price = float(active["entry_price"])
                if date == exit_date:
                    strategy_value = equity * float(active["exit_price"]) / entry_price
                    active = None
                    equity = strategy_value
                else:
                    strategy_value = equity * float(row["close"]) / entry_price
            else:
                strategy_value = equity
            curve_rows.append({
                "stock": str(stock),
                "date": date,
                "strategy": strategy_value,
                "buy_hold": float(row["close"]) / float(stock_kline.iloc[0]["close"]),
            })
    return {
        "features": data,
        "events": events_df,
        "trades": trades_df,
        "summary": summary_df,
        "kline": kline_rows,
        "curves": pd.DataFrame(curve_rows),
    }


def run_low_ma_dataset(warehouse: Any, *, start_date: str, end_date: str,
                       symbols: list[str] | None = None,
                       config: LowMAConfig | None = None,
                       required_quality: str = "PASS") -> dict[str, pd.DataFrame]:
    """Load an explicit Published stock_daily range and run the experiment."""

    from StockInvestmentTool.warehouse.datasets import load_dataset

    if not start_date or not end_date:
        raise ValueError("start_date and end_date are required")
    dataset = load_dataset(
        warehouse, "stock_daily", start_date=start_date, end_date=end_date,
        symbols=symbols, required_quality=required_quality, allow_legacy=False,
    )
    result = run_low_ma_experiment(dataset.data, config)
    result["data_context"] = dataset.context
    return result


def _summary_row(stock: str, trades: pd.DataFrame) -> dict[str, Any]:
    return {
        "stock": stock,
        "trades": len(trades),
        "win_rate_pct": float((trades["return_pct"] > 0).mean() * 100),
        "cumulative_return_pct": float(((1 + trades["return_pct"] / 100).prod() - 1) * 100),
        "avg_return_pct": float(trades["return_pct"].mean()),
        "median_return_pct": float(trades["return_pct"].median()),
        "max_loss_pct": float(trades["return_pct"].min()),
        "avg_holding_days": float(trades["holding_days"].mean()),
        "avg_mae_pct": float(trades["mae_pct"].mean()),
        "median_mae_pct": float(trades["mae_pct"].median()),
        "avg_mfe_pct": float(trades["mfe_pct"].mean()),
        "median_mfe_pct": float(trades["mfe_pct"].median()),
    }


__all__ = ["LowMAConfig", "build_low_ma_features", "run_low_ma_experiment", "run_low_ma_dataset"]
