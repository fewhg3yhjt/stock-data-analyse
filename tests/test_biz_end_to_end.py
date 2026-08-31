# -*- coding: utf-8 -*-
"""新业务领域的跨模块闭环验收。"""

import numpy as np
import pandas as pd

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.models import SimulationPlan, StrategyContext, new_id
from StockInvestmentTool.biz.notification import NotificationService
from StockInvestmentTool.biz.observation import (
    OBS_OBSERVING,
    OBS_READY_FOR_ENTRY,
    ObservationService,
)
from StockInvestmentTool.biz.performance import PerformanceService, ReviewService
from StockInvestmentTool.biz.portfolio import EVT_BUY, PortfolioService
from StockInvestmentTool.biz.regime import MarketRegimeService
from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.research import ResearchService
from StockInvestmentTool.biz.screen import ScreenDefinition, ScreenExecutor, ScreenRun
from StockInvestmentTool.biz.simulation import execute_simulation
from StockInvestmentTool.biz.strategy import StrategySpec, compile_strategy
from StockInvestmentTool.biz.valuation import PositionValuationService


def market_data():
    n = 40
    close = 10.0 * (1.003 ** np.arange(n))
    return pd.DataFrame({
        "date": pd.bdate_range("2026-06-01", periods=n),
        "code": ["sh600908"] * n,
        "open": close * 0.99,
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": [1000] * n,
        "amount": [10000] * n,
        "ma20": pd.Series(close).rolling(20, min_periods=1).mean(),
        "ma60": pd.Series(close).rolling(60, min_periods=1).mean(),
    })


def strategy():
    return compile_strategy(StrategySpec(
        strategy_id="e2e_strategy",
        name="E2E 策略",
        version="1",
        entry_rules=[{"rule_id": "entry", "action": "BUY", "when": {
            "type": "comparison", "left": {"field": "close"},
            "operator": ">", "right": {"value": 10.0}}}],
        exit_rules=[{"rule_id": "exit", "action": "SELL_ALL", "when": {
            "type": "comparison", "left": {"field": "close"},
            "operator": "<", "right": {"value": 5.0}}}],
        risk={"hard_stop_ratio": 0.5},
        position_sizing={"initial_ratio": 0.2},
    ))


def test_full_business_flow(tmp_path):
    repo = BusinessRepository(BusinessDB(tmp_path / "business.db"))
    df = market_data()
    compiled = strategy()

    # ScreenRun -> ScreenCandidate
    screen = ScreenDefinition(
        screen_id="e2e_screen", name="E2E 筛选", version="1",
        condition_spec={"type": "comparison", "left": {"field": "close"},
                        "operator": ">", "right": {"value": 10.0}},
    )
    screen_version_id = repo.save_screen_version(screen)
    universe_id = repo.save_universe_snapshot(["sh600908"], as_of="2026-07-24")
    candidates, meta = ScreenExecutor(screen, df).execute("2026-07-24")
    assert candidates and meta["status"] == "success"
    run = ScreenRun(
        run_id=new_id("screen_run"), screen_version_id=screen_version_id,
        universe_snapshot_id=universe_id, requested_as_of="2026-07-24",
        actual_data_as_of=meta["actual_data_as_of"], status="success",
        matched_count=len(candidates), data_context={"quality_status": "PASS"},
    )
    repo.save_screen_run(run)
    candidate = candidates[0]
    candidate.screen_run_id = run.run_id
    repo.save_screen_candidate(candidate)

    # Candidate -> ResearchRun -> StrategyDecision -> Evidence
    regime = MarketRegimeService(df, {"quality_status": "PASS"}).compute("2026-07-24")
    repo.save_market_regime(regime)
    research = ResearchService(df, {"quality_status": "PASS"}, compiled, regime.to_dict())
    research_result = research.run()
    repo.save_research_run(
        research_result, {"quality_status": "PASS"}, symbol="sh600908",
        strategy_version_id="sv_e2e", source_screen_run_id=run.run_id,
        source_candidate_id=candidate.candidate_id,
    )
    for decision in research_result.decisions:
        decision.strategy_version_id = "sv_e2e"
        decision.research_run_id = research_result.research_run_id
        repo.save_decision(decision)
    assert repo.get_research_run(research_result.research_run_id)
    assert repo.list_research_evidence(research_result.research_run_id)

    # Research -> Observation -> SimulationPlan -> SimulationRun
    observations = ObservationService(repo)
    observation = observations.create_observation(
        "sh600908", source_type="screen", screen_run_id=run.run_id,
        screen_candidate_id=candidate.candidate_id,
        strategy_version_id="sv_e2e", data_as_of=meta["actual_data_as_of"],
    )
    observations.transition_and_save(observation, OBS_OBSERVING)
    observation.current_strategy_version_id = "sv_e2e"
    observations.update_observation(observation)
    plan = SimulationPlan(
        plan_id=new_id("plan"), strategy_version_id="sv_e2e",
        source_screen_run_id=run.run_id, observation_id=observation.observation_id,
        start_date="2026-06-01", end_date="2026-07-24", initial_cash=100000,
        benchmark="sh000300", cost_config={"fee_rate": 0.001},
    )
    repo.save_simulation_plan(plan)
    sim_run, sim_result, fills, _ = execute_simulation(plan, df, strategy=compiled)
    repo.save_simulation_run(sim_run)
    for fill in fills:
        repo.save_simulation_fill(fill)
    repo.save_simulation_result(sim_result)
    assert repo.get_simulation_result(sim_run.run_id)["run_id"] == sim_run.run_id

    # Observation -> ready_for_entry -> PositionCycle -> Execution -> valuation
    observations.transition_and_save(observation, OBS_READY_FOR_ENTRY)
    pf_service = PortfolioService(repo)
    _, portfolio = pf_service.ensure_default_account_portfolio()
    pf_service.initialize_cash(portfolio.portfolio_id, 100000)
    cycle = pf_service.open_cycle(
        portfolio.portfolio_id, "sh600908", strategy_version_id="sv_e2e",
        observation_id=observation.observation_id,
        entry_plan={"action": "BUY", "quantity": 1000},
    )
    execution = pf_service.record_execution(
        portfolio.portfolio_id, cycle.position_cycle_id, event_type=EVT_BUY,
        trade_time="2026-07-24", quantity=1000, price=float(df["close"].iloc[-1]),
        idempotency_key="e2e-buy-1",
    )
    observations.promote_and_save(observation, cycle.position_cycle_id)
    valuation = PositionValuationService(pf_service, df, {"quality_status": "PASS"})
    position_value = valuation.valuate_cycle(cycle.position_cycle_id)
    assert execution.execution_id
    assert position_value.quantity == 1000
    assert observation.status == "promoted"

    # Position -> Performance/Review -> Advice -> NotificationEvent/Delivery
    performance = PerformanceService(pf_service).compute(
        portfolio.portfolio_id, "2026-06-01", "2026-07-24", price_df=df,
    )
    assert performance.equity_curve
    review = ReviewService(repo).create_review(
        cycle.position_cycle_id, discovery_reason="screen", simulation_run_id=sim_run.run_id,
    )
    ReviewService(repo).add_evidence(
        review.review_id, source_type="execution", source_id=execution.execution_id,
    )
    notification = NotificationService(repo)
    advice = notification.advice_from_decision(research_result.decisions[0], portfolio_id=portfolio.portfolio_id)
    event = notification.create_event(
        event_type="BUY_SIGNAL", symbol=advice.symbol, advice_id=advice.advice_id,
        strategy_version_id="sv_e2e", data_as_of=advice.data_as_of, action=advice.action,
        trigger_fingerprint="e2e",
    )
    delivery = notification.create_delivery(event, "email", "test@example.com")
    assert notification.claim(delivery.delivery_id, "e2e-worker")
    assert notification.deliver(
        delivery.delivery_id, type("Channel", (), {"send": lambda *_: True})(),
        subject="E2E", body="E2E", recipient="test@example.com",
    )
    assert repo.db.fetchone(
        "SELECT status FROM notification_deliveries WHERE delivery_id=?", (delivery.delivery_id,)
    )["status"] == "sent"
