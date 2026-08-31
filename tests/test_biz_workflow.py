# -*- coding: utf-8 -*-
"""跨模块应用服务测试。"""

import pytest

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.observation import OBS_OBSERVING, OBS_READY_FOR_ENTRY, ObservationService
from StockInvestmentTool.biz.repo import BusinessRepository
from StockInvestmentTool.biz.workflow import BusinessWorkflowService, WorkflowError


def test_candidate_to_entry_is_one_business_flow(tmp_path):
    repo = BusinessRepository(BusinessDB(tmp_path / "workflow.db"))
    repo.db.insert("screens", {
        "screen_id": "screen1", "name": "s", "status": "published",
        "current_version_id": "", "created_at": "t", "updated_at": "t",
    })
    repo.db.insert("screen_versions", {
        "screen_version_id": "screenv1", "screen_id": "screen1", "version_no": 1,
        "config_json": "{}", "config_hash": "h", "status": "published",
        "published_at": "t", "created_at": "t",
    })
    repo.db.insert("universe_snapshots", {
        "universe_snapshot_id": "u1", "universe_type": "selected_symbols",
        "symbols_json": '["sh600908"]', "symbol_count": 1, "fingerprint": "f",
        "as_of": "2026-08-14", "created_at": "t",
    })
    repo.db.insert("screen_runs", {
        "screen_run_id": "run1", "screen_version_id": "screenv1",
        "universe_snapshot_id": "u1", "run_type": "manual",
        "requested_as_of": "2026-08-14", "actual_data_as_of": "2026-08-14",
        "data_context_json": '{"quality_status":"PASS"}', "status": "success",
        "matched_count": 1, "started_at": "t", "finished_at": "t", "error": "",
    })
    repo.db.insert("screen_candidates", {
        "candidate_id": "candidate1", "screen_run_id": "run1", "symbol": "sh600908",
        "name": "test", "asset_type": "stock", "industry": "", "rank_no": 1,
        "score": None, "matched": 1, "condition_results_json": '{"passed":true}',
        "display_values_json": "{}", "data_as_of": "2026-08-14", "expires_at": "",
        "created_at": "t",
    })
    workflow = BusinessWorkflowService(repo)
    obs = workflow.observe_candidate("candidate1")
    assert obs.symbol == "sh600908"
    workflow.observations.transition_and_save(obs, OBS_OBSERVING)
    plan = workflow.create_simulation_plan(
        obs.observation_id, strategy_version_id="sv1", start_date="2026-01-01",
        end_date="2026-08-14", initial_cash=100000,
    )
    assert plan.observation_id == obs.observation_id

    # 建仓前必须人工确认
    with pytest.raises(WorkflowError):
        workflow.build_entry_context(obs.observation_id, "pf1")
    workflow.observations.transition_and_save(obs, OBS_READY_FOR_ENTRY)

    _, portfolio = workflow.portfolio.ensure_default_account_portfolio()
    workflow.portfolio.initialize_cash(portfolio.portfolio_id, 100000)
    entry_context = workflow.build_entry_context(obs.observation_id, portfolio.portfolio_id)
    execution, promoted = workflow.record_entry(
        entry_context, quantity=1000, price=10.0, idempotency_key="entry1",
    )
    assert execution.event_type == "BUY"
    assert promoted.status == "promoted"
    assert promoted.promoted_position_cycle_id
    assert workflow.portfolio.cash_balance(portfolio.portfolio_id) == pytest.approx(90000)

    # 同一 key 重放不得重复扣款
    replay, promoted_again = workflow.record_entry(
        entry_context, quantity=1000, price=10.0, idempotency_key="entry1",
    )
    assert replay.execution_id == execution.execution_id
    assert workflow.portfolio.cash_balance(portfolio.portfolio_id) == pytest.approx(90000)


def test_entry_rolls_back_when_cash_is_insufficient(tmp_path):
    repo = BusinessRepository(BusinessDB(tmp_path / "workflow2.db"))
    obs_service = ObservationService(repo)
    obs = obs_service.create_observation("sh600908")
    obs_service.transition_and_save(obs, OBS_OBSERVING)
    obs_service.transition_and_save(obs, OBS_READY_FOR_ENTRY)
    workflow = BusinessWorkflowService(repo)
    _, portfolio = workflow.portfolio.ensure_default_account_portfolio()
    workflow.portfolio.initialize_cash(portfolio.portfolio_id, 1)
    context = workflow.build_entry_context(obs.observation_id, portfolio.portfolio_id)
    with pytest.raises(WorkflowError):
        workflow.record_entry(context, quantity=1000, price=10, idempotency_key="bad")
    assert repo.db.fetchone("SELECT COUNT(*) AS n FROM executions")["n"] == 0
    assert repo.db.fetchone("SELECT COUNT(*) AS n FROM position_cycles")["n"] == 0
