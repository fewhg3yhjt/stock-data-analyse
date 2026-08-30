# -*- coding: utf-8 -*-
"""P0 集成冒烟：ScreenRun → ResearchRun → StrategyDecision → SimulationRun。

在容器内以真实 Published stock_daily/indicators 运行，验证 biz 全链路。
用法：
  sudo docker exec stock-invest python3 -m scripts.biz_p0_smoke
"""
from __future__ import annotations

import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_SMOKE_DB = os.path.join(tempfile.gettempdir(), "biz_p0_smoke.db")
if os.path.exists(_SMOKE_DB):
    os.remove(_SMOKE_DB)  # 冒烟专用临时库，幂等重建
os.environ.setdefault("BUSINESS_DB_PATH", _SMOKE_DB)

from StockInvestmentTool.biz.code import normalize
from StockInvestmentTool.biz.models import SimulationPlan, new_id, now_utc
from StockInvestmentTool.biz.regime import MarketRegimeService
from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.research import ResearchService
from StockInvestmentTool.biz.screen import ScreenDefinition, ScreenExecutor, ScreenRun
from StockInvestmentTool.biz.simulation import execute_simulation
from StockInvestmentTool.biz.strategy import StrategySpec, compile_strategy
from StockInvestmentTool.indicators.engine import IndicatorRegistry
from StockInvestmentTool.warehouse.datasets import load_dataset
from StockInvestmentTool.warehouse.storage import Warehouse


def load_symbol_history(symbol: str, start: str, end: str):
    w = Warehouse()
    res = load_dataset(w, "stock_daily", start_date=start, end_date=end,
                       symbols=[symbol], allow_legacy=False)
    if res.data.empty:
        raise RuntimeError(f"{symbol} 无数据")
    return res.data, res.context


def main() -> int:
    repo = BusinessRepository()
    print("business.db:", repo.db.db_path)

    # 1. 用真实代码范围跑一次小筛选（比较条件 close > 11.5，取最近一个交易日）
    w = Warehouse()
    screen_df = load_dataset(w, "stock_daily", start_date="2026-08-01",
                             end_date="2026-08-20", allow_legacy=False).data
    definition = ScreenDefinition(
        screen_id="smoke_screen", name="冒烟筛选", version="1",
        condition_spec={"type": "comparison", "left": {"field": "close"},
                        "operator": ">", "right": {"value": 11.5}},
        sort_spec={"field": "symbol", "direction": "asc"},
    )
    executor = ScreenExecutor(definition, screen_df)
    candidates, meta = executor.execute(as_of="2026-08-20")
    print(f"[Screen] 命中 {meta['matched_count']} 只（as_of={meta['actual_data_as_of']}）")
    if not candidates:
        print("[Screen] 无候选，退出")
        return 2
    target = candidates[0]
    print(f"[Screen] 首个候选: {target.symbol}")

    # 2. 个股研究
    hist, ctx = load_symbol_history(target.symbol, "2026-06-01", "2026-08-20")
    strategy_spec = StrategySpec(
        strategy_id="smoke_strategy", name="冒烟策略", version="1",
        entry_rules=[{"rule_id": "buy", "action": "BUY", "position_ratio": 0.2, "when": {
            "type": "comparison", "left": {"field": "close"}, "operator": ">", "right": {"value": 11.5}}}],
        exit_rules=[{"rule_id": "sell", "action": "SELL_ALL", "when": {
            "type": "comparison", "left": {"field": "close"}, "operator": "<", "right": {"value": 5}}}],
        risk={"hard_stop_ratio": 0.2, "max_position_ratio": 0.3},
        position_sizing={"mode": "fixed_ratio", "initial_ratio": 0.2},
        execution={"signal_at": "close", "execute_at": "next_open"},
        benchmark="sh000300",
    )
    strategy = compile_strategy(strategy_spec)
    svid = repo.save_strategy_version("smoke_strategy", 1,
                                      {"name": "smoke_strategy", "version": "1"},
                                      strategy_spec.config_hash())

    regime_svc = MarketRegimeService(hist, ctx)
    regime = regime_svc.compute(meta["actual_data_as_of"])
    repo.save_market_regime(regime)
    print(f"[Regime] {regime.regime} as_of={regime.as_of}")

    research = ResearchService(hist, ctx, strategy=strategy, market_regime=regime.to_dict())
    r_result = research.run()
    rrid = repo.save_research_run(r_result, ctx, symbol=target.symbol, strategy_version_id=svid)
    for d in r_result.decisions:
        repo.save_decision(d)
    print(f"[Research] status={r_result.status} decisions={r_result.strategy_decision_ids}")

    # 3. 模拟
    plan = SimulationPlan(
        plan_id=new_id("plan"), strategy_version_id=svid,
        start_date="2026-06-01", end_date="2026-08-20", initial_cash=100000.0,
        cost_config={"fee_rate": 0.001, "slippage": 0.0005},
        benchmark="sh000300",
    )
    run, result, fills, events = execute_simulation(
        plan, hist, strategy=strategy, registry=IndicatorRegistry())
    repo.save_simulation_run(run)
    for f in fills:
        repo.save_simulation_fill(f)
    repo.save_simulation_result(result)
    print(f"[Simulation] status={run.status} fills={len(fills)} "
          f"final_equity={result.final_equity:.2f} return={result.total_return:.4%} "
          f"benchmark={result.comparison_status}")

    # 4. 链路验收
    got_decision = repo.get_decision(r_result.strategy_decision_ids[0]) if r_result.strategy_decision_ids else None
    got_result = repo.get_simulation_result(run.run_id)
    ok = (meta["status"] == "success" and r_result.status == "success"
          and got_decision is not None and got_result is not None
          and result.comparison_status == "unavailable")
    if not ok:
        print(f"[P0-Smoke] 诊断: meta.status={meta.get('status')} "
              f"r_status={r_result.status} dec_saved={got_decision is not None} "
              f"res_saved={got_result is not None} comp={result.comparison_status} "
              f"decisions_objs={len(r_result.decisions)}")
    print(f"[P0-Smoke] {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())