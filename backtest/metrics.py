"""绩效指标计算"""

import numpy as np
import pandas as pd


class PerformanceMetrics:
    """回测绩效指标"""

    @staticmethod
    def total_return(final_value: float, initial_value: float) -> float:
        return (final_value - initial_value) / initial_value * 100

    @staticmethod
    def max_drawdown(equity_curve: list[dict]) -> float:
        """最大回撤 (%)"""
        df = pd.DataFrame(equity_curve)
        peak = df["total_asset"].cummax()
        dd = (df["total_asset"] - peak) / peak * 100
        return round(float(dd.min()), 2)

    @staticmethod
    def sharpe_ratio(equity_curve: list[dict],
                     risk_free_rate: float = 0.02) -> float:
        """夏普比率（年化）"""
        df = pd.DataFrame(equity_curve)
        returns = df["total_asset"].pct_change().dropna()
        if len(returns) < 2 or returns.std() == 0:
            return 0.0
        excess = returns.mean() * 252 - risk_free_rate
        vol = returns.std() * np.sqrt(252)
        return round(float(excess / vol), 2) if vol > 0 else 0.0

    @staticmethod
    def calmar_ratio(equity_curve: list[dict]) -> float:
        """Calmar 比率 = 年化收益率 / 最大回撤绝对值"""
        df = pd.DataFrame(equity_curve)
        total_days = len(df)
        if total_days < 2:
            return 0.0
        annual_return = (df["total_asset"].iloc[-1] / df["total_asset"].iloc[0]) ** (
            252 / total_days
        ) - 1
        mdd = PerformanceMetrics.max_drawdown(equity_curve)
        if mdd == 0:
            return 0.0
        return round(float(annual_return / abs(mdd) * 100), 2)

    @staticmethod
    def win_rate(trades: list[dict]) -> float:
        """胜率（止盈交易 / 所有含利润的交易）"""
        closed = [t for t in trades if t.get("pnl") is not None and "止盈" in t.get("type", "")]
        if not closed:
            return 0.0
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        return round(len(wins) / len(closed) * 100, 1)

    @staticmethod
    def summary(result: dict) -> dict:
        """从 run_detailed 的 result 计算全部指标"""
        bt = result.get("backtest", result)
        equity = bt.get("equity_curve", [])
        trades = bt.get("trades", [])

        return {
            "total_return": bt.get("total_return", 0),
            "buy_hold_return": bt.get("buy_hold_return", 0),
            "excess_return": bt.get("excess_return", 0),
            "max_drawdown": PerformanceMetrics.max_drawdown(equity),
            "sharpe_ratio": PerformanceMetrics.sharpe_ratio(equity),
            "calmar_ratio": PerformanceMetrics.calmar_ratio(equity),
            "win_rate": PerformanceMetrics.win_rate(trades),
            "trade_count": bt.get("trade_count", 0),
            "initial_cash": bt.get("initial_cash", 0),
            "final_asset": bt.get("final_asset", 0),
        }
