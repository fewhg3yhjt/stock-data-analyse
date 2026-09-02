"""回测引擎 — 对策略进行历史模拟，输出交易明细与权益曲线

重构后支持从 SchemeConfig 读取回测参数（配置驱动）。
使用 Prompt v4.5 止盈策略（左侧固定止盈 + 右侧移动止盈）。
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from StockInvestmentTool.core.scheme import SchemeConfig
from StockInvestmentTool.strategy.take_profit import TakeProfitOptimizer


class BacktestEngine:
    """回测引擎（委托给 TakeProfitOptimizer 执行实际逻辑）

    Args:
        df: K线数据
        scheme: 可选策略方案（推荐），提供 sell_rules / risk / backtest
        initial_cash: 兼容旧调用
        stock_type: 兼容旧调用
        dividend_anchor: 兼容旧调用
    """

    def __init__(
        self,
        df: pd.DataFrame,
        initial_cash: float = 100_000,
        stock_type: str = "B",
        dividend_anchor: Optional[float] = None,
        scheme: Optional[SchemeConfig] = None,
    ):
        # TakeProfitOptimizer owns the working copy; avoid duplicating it here.
        self.df = df
        self.initial_cash = initial_cash
        self.stock_type = stock_type
        self.dividend_anchor = dividend_anchor
        self.scheme = scheme
        self._result: Optional[dict] = None

    def _build_optimizer(self) -> TakeProfitOptimizer:
        """构造优化器（scheme 优先，无则用旧参数）"""
        return TakeProfitOptimizer(
            df=self.df,
            initial_cash=self.initial_cash,
            stock_type=self.stock_type,
            dividend_anchor=self.dividend_anchor,
            scheme=self.scheme,
        )

    def optimize_and_backtest(
        self,
        top_n: int = 5,
        trail_thresholds: Optional[list[float]] = None,
        buy_offsets: Optional[list[float]] = None,
    ) -> dict:
        """执行：参数搜索 → 最优参数回测

        Prompt v4.5 搜索参数：
        - trail_threshold: 右侧移动止盈回撤阈值
        - buy_offset: 买入阈值偏移量
        """
        optimizer = self._build_optimizer()

        best_combo, best_return, all_results = optimizer.optimize(
            trail_thresholds=trail_thresholds,
            buy_offsets=buy_offsets,
        )

        # Top N
        sorted_results = sorted(all_results, key=lambda x: x[2], reverse=True)
        top_combos = []
        for trail, offset, ret in sorted_results[:top_n]:
            top_combos.append({
                "trail_threshold": trail,
                "offset": offset,
                "return": round(ret, 2),
            })

        # 用最优参数跑详细回测
        trail_threshold, offset = best_combo
        detailed = optimizer.run_detailed(trail_threshold, offset)

        self._result = {
            "best_params": {
                "trail_threshold": trail_threshold,
                "offset": offset,
            },
            "best_return": round(best_return, 2),
            "top_combos": top_combos,
            "backtest": detailed,
        }
        return self._result

    def run_custom(self, trail_threshold: float = 0.05,
                   offset: float = 0.0,
                   trade_start_date: Optional[str] = None) -> dict:
        """用自定义参数回测

        Args:
            trail_threshold: 右侧移动止盈回撤阈值 (默认5%)
            offset: 买入阈值偏移量
        """
        optimizer = self._build_optimizer()
        result = optimizer.run_detailed(trail_threshold, offset, trade_start_date=trade_start_date)
        self._result = {
            "custom_params": {
                "trail_threshold": trail_threshold,
                "offset": offset,
            },
            "backtest": result,
        }
        return self._result
