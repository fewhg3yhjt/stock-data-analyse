from __future__ import annotations

import pytest

from StockInvestmentTool.domain.features import (
    CATEGORY_POSITION,
    CATEGORY_TECHNICAL,
    CONTEXT_POSITION,
    CONTEXT_SECURITY,
    FeatureDefinition,
    USAGE_BUY,
    USAGE_NOTIFY,
    USAGE_SCREEN,
    VALUE_BOOLEAN,
    VALUE_NUMBER,
)
from StockInvestmentTool.domain.simulation import SIM_CANCELLED, SIM_RUNNING, SIM_SUCCESS
from StockInvestmentTool.repositories.backend_domain import BackendDomainRepository
from StockInvestmentTool.services.feature_service import FeatureService
from StockInvestmentTool.services.rule_engine import RuleEngine
from StockInvestmentTool.services.simulation_service import SimulationService
from StockInvestmentTool.services.stock_set_service import StockSetService


@pytest.fixture
def repo(tmp_path):
    return BackendDomainRepository(tmp_path / "backend_domain.db")


def test_feature_usage_validation_blocks_wrong_context(repo):
    svc = FeatureService(repo)
    svc.register_feature(FeatureDefinition(
        feature_id="highest_since_entry",
        name="买入后最高价",
        category=CATEGORY_POSITION,
        value_type=VALUE_NUMBER,
        required_context=CONTEXT_POSITION,
        supported_usages=[USAGE_NOTIFY],
    ))

    assert svc.require_feature_usage("highest_since_entry", USAGE_NOTIFY, CONTEXT_POSITION).feature_id == "highest_since_entry"
    with pytest.raises(ValueError):
        svc.require_feature_usage("highest_since_entry", USAGE_BUY, CONTEXT_SECURITY)


def test_rule_engine_validates_and_evaluates_feature_tree(repo):
    FeatureService(repo).register_feature(FeatureDefinition(
        feature_id="industry_reflow",
        name="板块回流",
        category=CATEGORY_TECHNICAL,
        value_type=VALUE_BOOLEAN,
        required_context=CONTEXT_SECURITY,
        supported_usages=[USAGE_SCREEN],
    ))
    FeatureService(repo).register_feature(FeatureDefinition(
        feature_id="distance_to_pressure",
        name="距压力区",
        category=CATEGORY_TECHNICAL,
        value_type=VALUE_NUMBER,
        required_context=CONTEXT_SECURITY,
        supported_usages=[USAGE_SCREEN],
    ))
    expression = {
        "operator": "AND",
        "children": [
            {"feature": "industry_reflow", "operator": "IS_TRUE"},
            {"feature": "distance_to_pressure", "operator": "BETWEEN", "value": [0.01, 0.04]},
        ],
    }

    engine = RuleEngine(repo)
    engine.validate_rule(expression, usage=USAGE_SCREEN, context=CONTEXT_SECURITY)
    assert engine.evaluate_rule(expression, {"industry_reflow": True, "distance_to_pressure": 0.03}) is True
    assert engine.evaluate_rule(expression, {"industry_reflow": True, "distance_to_pressure": 0.08}) is False


def test_stock_set_members_are_deduplicated(repo):
    svc = StockSetService(repo)
    stock_set = svc.create_stock_set("研究集合", ["sh600000", "sh600000", "sz000001"], stock_set_id="ss_test")

    assert stock_set.stock_set_id == "ss_test"
    assert svc.snapshot_members("ss_test") == ["sh600000", "sz000001"]
    assert svc.add_members("ss_test", ["sz000001", "sh600519"]) == 1
    assert svc.snapshot_members("ss_test") == ["sh600000", "sh600519", "sz000001"]


def test_simulation_plan_run_trade_event_flow(repo):
    stock_sets = StockSetService(repo)
    stock_sets.create_stock_set("模拟样本", ["sh600000"], stock_set_id="ss_sim")

    simulations = SimulationService(repo)
    plan = simulations.create_plan(
        plan_id="sp_test",
        name="前高下方蓄势模拟",
        stock_set_id="ss_sim",
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100000,
        buy_rule_snapshot={"operator": "AND", "children": []},
    )
    run = simulations.enqueue_run(plan.plan_id, run_id="sr_test", data_versions_json={"stock_daily": "v1"})

    assert run.status == "PENDING"
    assert simulations.start_run(run.run_id).status == SIM_RUNNING
    simulations.record_event(run.run_id, simulations.make_event(run.run_id, "BUY_SIGNAL", instrument="sh600000"))
    trade = simulations.make_trade(
        run.run_id,
        "sh600000",
        buy_date="2024-02-01",
        buy_price=10,
        sell_date="2024-03-01",
        sell_price=11,
        quantity=1000,
        fees=8,
        pnl=992,
        pnl_pct=0.0992,
        buy_reason_json={"rule": "reflow"},
    )
    simulations.record_trade(run.run_id, trade)
    finished = simulations.finish_success(run.run_id, result_summary_json={"total_return": 0.0992})

    assert finished.status == SIM_SUCCESS
    assert finished.progress == 100
    assert repo.list_simulation_trades(run.run_id)[0].pnl == 992
    assert repo.list_simulation_events(run.run_id)[0].event_type == "BUY_SIGNAL"


def test_simulation_service_rejects_invalid_transition(repo):
    StockSetService(repo).create_stock_set("模拟样本", ["sh600000"], stock_set_id="ss_sim")
    simulations = SimulationService(repo)
    plan = simulations.create_plan(
        plan_id="sp_test",
        name="测试模拟",
        stock_set_id="ss_sim",
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100000,
    )
    run = simulations.enqueue_run(plan.plan_id, run_id="sr_test")

    assert simulations.cancel_run(run.run_id).status == SIM_CANCELLED
    with pytest.raises(ValueError):
        simulations.start_run(run.run_id)
