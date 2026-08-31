# -*- coding: utf-8 -*-
"""biz 包单元测试：business.db repository。"""

import os
import tempfile

import pytest

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.models import (
    SimulationFill,
    SimulationResult,
    SimulationRun,
    StrategyDecision,
    new_id,
)
from StockInvestmentTool.biz.regime import MarketRegime, MarketRegimeService
from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.research import ResearchResult
from StockInvestmentTool.biz.screen import ScreenCandidate, ScreenDefinition, ScreenRun


@pytest.fixture
def repo(tmp_path):
    db = BusinessDB(tmp_path / "test_business.db")
    return BusinessRepository(db)


class TestStrategy:
    def test_save_and_get_version(self, repo):
        vid = repo.save_strategy_version("s1", 1, {"name": "trend", "version": "1"}, "hash1")
        assert vid
        got = repo.get_strategy_version(vid)
        assert got["strategy_id"] == "s1"
        assert got["config_hash"] == "hash1"
        assert len(repo.list_strategy_versions("s1")) == 1


class TestDecision:
    def test_save_and_get_decision(self, repo):
        repo.save_strategy_version("s1", 1, {"name": "s1", "version": "1"}, "hash-s1")
        dec = StrategyDecision(
            decision_id=new_id("dec"), strategy_id="s1", strategy_version="1",
            symbol="sh600908", decision_time="2026-08-14T15:00:00Z", data_as_of="2026-08-14",
            action="BUY", quantity_ratio=0.2, price=11.8,
            strategy_version_id=repo.list_strategy_versions("s1")[0]["strategy_version_id"],
            input_snapshot={"symbol": "sh600908"},
            decision_trace={"evaluated_rules": [], "triggered_rules": [], "suppressed_rules": [], "final_action": "BUY"},
        )
        repo.save_decision(dec)
        got = repo.get_decision(dec.decision_id)
        assert got["action"] == "BUY"
        assert got["input_snapshot"] == {"symbol": "sh600908"}
        assert got["decision_trace"]["final_action"] == "BUY"


class TestMarketRegime:
    def test_save_and_get(self, repo):
        r = MarketRegime(regime_id=new_id("regime"), regime="weak_bull", as_of="2026-08-14",
                         input_snapshot={"price": 3000})
        repo.save_market_regime(r)
        got = repo.get_market_regime("2026-08-14")
        assert got["regime"] == "weak_bull"
        assert got["input_snapshot"]["price"] == 3000


class TestScreen:
    def test_save_screen_and_run(self, repo):
        definition = ScreenDefinition(screen_id="sc1", name="高价股", version="1",
                                      condition_spec={"type": "comparison",
                                                      "left": {"field": "close"}, "operator": ">", "right": {"value": 12}})
        svid = repo.save_screen_version(definition)
        usid = repo.save_universe_snapshot(["sh600908"], as_of="2026-08-14")
        run = ScreenRun(run_id=new_id("run"), screen_version_id=svid,
                        universe_snapshot_id=usid, requested_as_of="2026-08-14",
                        actual_data_as_of="2026-08-14", status="success", matched_count=1)
        repo.save_screen_run(run)
        cand = ScreenCandidate(candidate_id=new_id("cand"), screen_run_id=run.run_id,
                               symbol="sh600908", rank_no=1, data_as_of="2026-08-14",
                               condition_results={"passed": True})
        repo.save_screen_candidate(cand)
        cands = repo.list_candidates(run.run_id)
        assert len(cands) == 1
        assert cands[0]["symbol"] == "sh600908"
        assert cands[0]["condition_results"]["passed"] is True


class TestResearch:
    def test_save_and_get(self, repo):
        result = ResearchResult(research_run_id=new_id("rr"), status="success",
                                technical_assessment={"status": "ok", "price": 11.8},
                                market_assessment={"regime": "weak_bull"},
                                fundamental_assessment={"status": "deferred"},
                                strategy_decision_ids=["dec1"])
        rid = repo.save_research_run(result, {"quality_status": "PASS"}, symbol="sh600908")
        got = repo.get_research_run(rid)
        assert got["status"] == "success"
        assert got["result"]["technical_assessment"]["price"] == 11.8
        evs = repo.list_research_evidence(rid)
        assert len(evs) >= 3  # technical/valuation/fundamental/market


class TestSimulation:
    def test_save_run_fills_result(self, repo):
        run = SimulationRun(run_id=new_id("run"), plan_id="p1", status="running")
        repo.save_simulation_run(run)
        repo.update_simulation_run_status(run.run_id, "success")
        fill = SimulationFill(fill_id=new_id("fill"), simulation_run_id=run.run_id,
                              symbol="sh600908", side="BUY", signal_time="2026-08-14",
                              execution_time="2026-08-15", signal_price=11.5,
                              execution_price=11.6, quantity=100, gross_amount=1160.0,
                              fee=1.16)
        repo.save_simulation_fill(fill)
        result = SimulationResult(run_id=run.run_id, initial_cash=100000.0, final_equity=101000.0,
                                  total_return=0.01, comparison_status="unavailable",
                                  equity_curve=[{"date": "2026-08-14", "equity": 101000.0}])
        repo.save_simulation_result(result)
        fills = repo.list_simulation_fills(run.run_id)
        assert len(fills) == 1
        assert fills[0]["side"] == "BUY"
        got = repo.get_simulation_result(run.run_id)
        assert got["total_return"] == 0.01
        assert got["equity_curve"][0]["equity"] == 101000.0

    def test_repo_isolated_from_production(self, tmp_path):
        """repository 必须能用隔离临时库，不依赖生产 business.db。"""
        db = BusinessDB(tmp_path / "iso.db")
        repo = BusinessRepository(db)
        r = MarketRegime(regime_id=new_id("regime"), regime="range", as_of="2026-01-01")
        repo.save_market_regime(r)
        assert repo.get_market_regime("2026-01-01")["regime"] == "range"
