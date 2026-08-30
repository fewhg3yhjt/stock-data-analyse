# -*- coding: utf-8 -*-
"""biz 包单元测试：SimulationExecutor 单边成交模拟。"""

import numpy as np
import pandas as pd

from StockInvestmentTool.biz.models import SimulationPlan
from StockInvestmentTool.biz.simulation import execute_simulation
from StockInvestmentTool.biz.strategy import StrategySpec, compile_strategy


def make_df():
    """构造 30 日上升趋势行情，用于触发买入与持有。"""
    n = 30
    close = 10.0 * (1.02 ** np.arange(n))
    dates = pd.bdate_range("2026-07-01", periods=n)
    df = pd.DataFrame({
        "date": dates,
        "code": "sh600908",
        "open": close * 0.99, "high": close * 1.02, "low": close * 0.98, "close": close,
        "volume": [1000] * n, "amount": [10000] * n,
    })
    return df


def make_strategy():
    spec = StrategySpec(
        strategy_id="trend_pullback", name="趋势", version="1",
        entry_rules=[{
            "rule_id": "buy", "action": "BUY", "position_ratio": 0.2,
            "when": {"type": "comparison", "left": {"field": "close"},
                     "operator": ">", "right": {"value": 10}},
        }],
        exit_rules=[{
            "rule_id": "sell", "action": "SELL_ALL",
            "when": {"type": "comparison", "left": {"field": "close"},
                     "operator": "<", "right": {"value": 5}},
        }],
        risk={"hard_stop_ratio": 0.5, "max_position_ratio": 0.3},
        position_sizing={"mode": "fixed_ratio", "initial_ratio": 0.2},
        execution={"signal_at": "close", "execute_at": "next_open"},
    )
    return compile_strategy(spec)


def make_plan(initial_cash=100000.0):
    return SimulationPlan(
        plan_id="plan_test", strategy_version_id="sv_test",
        start_date="2026-07-01", end_date="2026-08-11", initial_cash=initial_cash,
        cost_config={"fee_rate": 0.001, "slippage": 0.0005},
        benchmark="sh000300",
    )


class TestSimulation:
    def test_buys_and_holds(self):
        plan = make_plan()
        df = make_df()
        strategy = make_strategy()
        run, result, fills, events = execute_simulation(plan, df, strategy=strategy)
        assert run.status == "success"
        assert any(f.side == "BUY" for f in fills)
        assert result.final_equity > plan.initial_cash  # 上升趋势盈利
        assert result.comparison_status == "unavailable"  # 基准未发布
        assert result.max_drawdown is not None
        assert result.equity_curve
        assert len(result.equity_curve) == len(df)

    def test_single_side_fills(self):
        plan = make_plan()
        df = make_df()
        strategy = make_strategy()
        _, result, fills, _ = execute_simulation(plan, df, strategy=strategy)
        # 单边成交：BUY/SELL 各为独立记录
        assert all(f.side in {"BUY", "SELL"} for f in fills)
        for f in fills:
            assert f.quantity > 0
            assert f.gross_amount > 0

    def test_cash_not_negative(self):
        plan = make_plan(initial_cash=1000.0)  # 现金很小，不应负
        df = make_df()
        strategy = make_strategy()
        _, result, fills, _ = execute_simulation(plan, df, strategy=strategy)
        assert result.final_equity >= 0
        assert result.final_equity <= plan.initial_cash * 2  # 不至于离谱

    def test_no_actions_on_flat(self):
        plan = make_plan()
        df = make_df()
        df["close"] = 10.0
        df["open"] = 10.0
        strategy = compile_strategy(StrategySpec(
            strategy_id="s", name="n", version="1",
            entry_rules=[{"rule_id": "b", "action": "BUY", "when": {
                "type": "comparison", "left": {"field": "close"}, "operator": ">", "right": {"value": 20}}}],
            exit_rules=[{"rule_id": "e", "action": "SELL_ALL", "when": {
                "type": "comparison", "left": {"field": "close"}, "operator": "<", "right": {"value": 5}}}],
            position_sizing={"initial_ratio": 0.2},
        ))
        _, result, fills, _ = execute_simulation(plan, df, strategy=strategy)
        assert not fills
        assert result.final_equity == pytest.approx(plan.initial_cash)

    def test_deterministic(self):
        plan = make_plan()
        df = make_df()
        strategy = make_strategy()
        r1 = execute_simulation(plan, df.copy(), strategy=strategy)
        r2 = execute_simulation(plan, df.copy(), strategy=strategy)
        assert r1[1].final_equity == pytest.approx(r2[1].final_equity)
        assert r1[1].equity_curve == r2[1].equity_curve


import pytest  # noqa: E402