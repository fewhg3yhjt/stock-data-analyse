# -*- coding: utf-8 -*-
"""业务平台持久层：business.db 的建表 SQL、连接与迁移。

依据 docs/NEW_SYSTEM_STORAGE_DESIGN.md §4/§5/§6 定义全部业务表。
所有时间统一使用 UTC 时间戳；业务日期单独保存为 YYYY-MM-DD。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from StockInvestmentTool.biz.config import business_db_path

# ---------------------------------------------------------------------------
# 建表 SQL
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ============ 配置与策略 ============
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',      -- draft/validated/published/enabled/disabled/archived
    current_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_version_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    published_at TEXT,
    enabled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(strategy_id, version_no),
    UNIQUE(strategy_id, config_hash),
    FOREIGN KEY(strategy_id) REFERENCES strategies(strategy_id)
);

CREATE TABLE IF NOT EXISTS strategy_validations (
    validation_id TEXT PRIMARY KEY,
    strategy_version_id TEXT NOT NULL,
    valid INTEGER NOT NULL,
    errors_json TEXT NOT NULL DEFAULT '[]',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    dependencies_json TEXT NOT NULL DEFAULT '[]',
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(strategy_version_id) REFERENCES strategy_versions(strategy_version_id)
);

CREATE TABLE IF NOT EXISTS strategy_dependencies (
    dependency_id TEXT PRIMARY KEY,
    strategy_version_id TEXT NOT NULL,
    dependency_type TEXT NOT NULL,   -- indicator / rule / dataset
    ref_name TEXT NOT NULL,
    ref_version TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(strategy_version_id, dependency_type, ref_name),
    FOREIGN KEY(strategy_version_id) REFERENCES strategy_versions(strategy_version_id)
);

CREATE TABLE IF NOT EXISTS market_regimes (
    regime_id TEXT PRIMARY KEY,
    regime TEXT NOT NULL,
    as_of TEXT NOT NULL,
    confidence REAL,
    algorithm_version TEXT NOT NULL DEFAULT '',
    input_snapshot_json TEXT NOT NULL DEFAULT '{}',
    explanation TEXT NOT NULL DEFAULT '',
    data_context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(as_of, algorithm_version)
);

-- ============ 选股与研究 ============
CREATE TABLE IF NOT EXISTS screens (
    screen_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    asset_types TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft',
    current_version_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS screen_versions (
    screen_version_id TEXT PRIMARY KEY,
    screen_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    config_json TEXT NOT NULL,          -- condition_spec / sort_spec / display_fields
    config_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    published_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(screen_id, version_no),
    UNIQUE(screen_id, config_hash),
    FOREIGN KEY(screen_id) REFERENCES screens(screen_id)
);

CREATE TABLE IF NOT EXISTS universe_snapshots (
    universe_snapshot_id TEXT PRIMARY KEY,
    universe_type TEXT NOT NULL,
    symbols_json TEXT NOT NULL,
    symbol_count INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    as_of TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS screen_runs (
    screen_run_id TEXT PRIMARY KEY,
    screen_version_id TEXT NOT NULL,
    universe_snapshot_id TEXT NOT NULL,
    run_type TEXT NOT NULL DEFAULT 'manual',
    requested_as_of TEXT NOT NULL,
    actual_data_as_of TEXT,
    data_context_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',
    matched_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(screen_version_id) REFERENCES screen_versions(screen_version_id),
    FOREIGN KEY(universe_snapshot_id) REFERENCES universe_snapshots(universe_snapshot_id)
);

CREATE TABLE IF NOT EXISTS screen_candidates (
    candidate_id TEXT PRIMARY KEY,
    screen_run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    asset_type TEXT NOT NULL DEFAULT 'stock',
    industry TEXT NOT NULL DEFAULT '',
    rank_no INTEGER,
    score REAL,
    matched INTEGER NOT NULL DEFAULT 1,
    condition_results_json TEXT NOT NULL DEFAULT '{}',
    display_values_json TEXT NOT NULL DEFAULT '{}',
    data_as_of TEXT NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(screen_run_id, symbol),
    FOREIGN KEY(screen_run_id) REFERENCES screen_runs(screen_run_id)
);
CREATE INDEX IF NOT EXISTS idx_screen_candidates_run_rank ON screen_candidates(screen_run_id, rank_no);

CREATE TABLE IF NOT EXISTS research_runs (
    research_run_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,          -- single_symbol / observation
    symbol TEXT NOT NULL DEFAULT '',
    observation_id TEXT,
    source_screen_run_id TEXT,
    source_candidate_id TEXT,
    strategy_version_id TEXT,
    data_context_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',
    result_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    finished_at TEXT,
    error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS research_evidence (
    evidence_id TEXT PRIMARY KEY,
    research_run_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL,         -- technical/fundamental/valuation/market/strategy_rule/risk
    source TEXT NOT NULL DEFAULT '',
    metric_name TEXT NOT NULL DEFAULT '',
    actual_value REAL,
    threshold_value REAL,
    assessment TEXT NOT NULL DEFAULT '',
    explanation TEXT NOT NULL DEFAULT '',
    data_as_of TEXT NOT NULL,
    input_snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(research_run_id) REFERENCES research_runs(research_run_id)
);

CREATE TABLE IF NOT EXISTS research_reports (
    report_id TEXT PRIMARY KEY,
    research_run_id TEXT NOT NULL,
    format TEXT NOT NULL DEFAULT 'markdown',
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(research_run_id) REFERENCES research_runs(research_run_id)
);

-- ============ 统一决策 ============
CREATE TABLE IF NOT EXISTS strategy_decisions (
    decision_id TEXT PRIMARY KEY,
    strategy_version_id TEXT NOT NULL,
    research_run_id TEXT,
    simulation_run_id TEXT,
    observation_id TEXT,
    position_cycle_id TEXT,
    symbol TEXT NOT NULL,
    decision_time TEXT NOT NULL,
    data_as_of TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity REAL,
    quantity_ratio REAL,
    price REAL,
    stop_price REAL,
    target_price REAL,
    input_snapshot_json TEXT NOT NULL,
    decision_trace_json TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    valid_until TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_symbol_time ON strategy_decisions(symbol, decision_time);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_version_date ON strategy_decisions(strategy_version_id, data_as_of);

-- ============ 模拟 ============
CREATE TABLE IF NOT EXISTS simulation_plans (
    plan_id TEXT PRIMARY KEY,
    strategy_version_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    universe_snapshot_id TEXT,
    source_screen_run_id TEXT,
    observation_id TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    initial_cash REAL NOT NULL,
    position_sizing_json TEXT NOT NULL DEFAULT '{}',
    execution_rules_json TEXT NOT NULL DEFAULT '{}',
    cost_config_json TEXT NOT NULL DEFAULT '{}',
    benchmark TEXT NOT NULL DEFAULT '',
    data_context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS simulation_runs (
    run_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',
    data_context_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    finished_at TEXT,
    error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS simulation_fills (
    fill_id TEXT PRIMARY KEY,
    simulation_run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,                  -- BUY / SELL
    signal_time TEXT NOT NULL,
    execution_time TEXT,
    signal_price REAL,
    execution_price REAL,
    quantity REAL NOT NULL,
    gross_amount REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    tax REAL NOT NULL DEFAULT 0,
    slippage REAL NOT NULL DEFAULT 0,
    decision_id TEXT,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simulation_fills_run_time ON simulation_fills(simulation_run_id, execution_time);
CREATE INDEX IF NOT EXISTS idx_simulation_fills_run_symbol ON simulation_fills(simulation_run_id, symbol, execution_time);

CREATE TABLE IF NOT EXISTS simulation_lots (
    lot_id TEXT PRIMARY KEY,
    simulation_run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    quantity REAL NOT NULL,
    remaining_quantity REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_fee REAL NOT NULL DEFAULT 0,
    source_fill_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_simulation_lots_run_symbol ON simulation_lots(simulation_run_id, symbol);

CREATE TABLE IF NOT EXISTS simulation_events (
    event_id TEXT PRIMARY KEY,
    simulation_run_id TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,            -- SIGNAL_GENERATED/FILLED/STOP_TRIGGERED/TAKE_PROFIT_TRIGGERED/END_OF_PERIOD/DATA_GAP/...
    payload_json TEXT NOT NULL DEFAULT '{}',
    event_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS simulation_results (
    result_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    initial_cash REAL NOT NULL,
    final_equity REAL NOT NULL,
    total_return REAL,
    benchmark_return REAL,
    excess_return REAL,
    max_drawdown REAL,
    win_rate REAL,
    profit_factor REAL,
    trade_count INTEGER NOT NULL DEFAULT 0,
    average_holding_days REAL,
    fees REAL NOT NULL DEFAULT 0,
    slippage REAL NOT NULL DEFAULT 0,
    equity_curve_json TEXT NOT NULL DEFAULT '[]',
    comparison_status TEXT NOT NULL DEFAULT 'unavailable',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parameter_search_runs (
    search_run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    base_strategy_version_id TEXT,
    status TEXT NOT NULL DEFAULT 'requested',
    started_at TEXT,
    finished_at TEXT,
    error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS parameter_search_results (
    result_id TEXT PRIMARY KEY,
    search_run_id TEXT NOT NULL,
    simulation_run_id TEXT NOT NULL,
    param_config_hash TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

-- ============ 观察与账户 ============
CREATE TABLE IF NOT EXISTS watch_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    asset_type TEXT NOT NULL DEFAULT 'stock',
    purpose TEXT NOT NULL DEFAULT 'research',
    target_amount REAL,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    started_at TEXT NOT NULL,
    paused_at TEXT,
    ended_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watch_subscriptions_symbol ON watch_subscriptions(symbol);

CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    asset_type TEXT NOT NULL DEFAULT 'stock',
    status TEXT NOT NULL DEFAULT 'discovered',
    current_strategy_version_id TEXT,
    observation_reason TEXT NOT NULL DEFAULT '',
    target_amount REAL,
    started_at TEXT NOT NULL,
    expires_at TEXT,
    latest_data_as_of TEXT,
    latest_simulation_run_id TEXT,
    latest_research_run_id TEXT,
    promoted_position_cycle_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_status ON observations(status);
CREATE INDEX IF NOT EXISTS idx_observations_symbol ON observations(symbol);

CREATE TABLE IF NOT EXISTS observation_sources (
    link_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL,
    source_type TEXT NOT NULL,           -- screen/strategy/money_flow/manual/holding/imported
    screen_run_id TEXT,
    screen_candidate_id TEXT,
    source_strategy_version_id TEXT,
    discovered_at TEXT NOT NULL,
    reason_snapshot_json TEXT NOT NULL DEFAULT '{}',
    data_as_of TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(observation_id) REFERENCES observations(observation_id)
);

CREATE TABLE IF NOT EXISTS observation_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL,
    snapshot_time TEXT NOT NULL,
    data_as_of TEXT NOT NULL,
    strategy_version_id TEXT,
    price REAL,
    market_regime_json TEXT NOT NULL DEFAULT '{}',
    entry_plan_json TEXT NOT NULL DEFAULT '{}',
    stop_plan_json TEXT NOT NULL DEFAULT '{}',
    decision_action TEXT NOT NULL DEFAULT '',
    decision_reason TEXT NOT NULL DEFAULT '',
    data_context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(observation_id) REFERENCES observations(observation_id)
);

CREATE TABLE IF NOT EXISTS observation_events (
    event_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_time TEXT NOT NULL,
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(observation_id) REFERENCES observations(observation_id)
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    account_type TEXT NOT NULL DEFAULT 'real',   -- real / paper
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolios (
    portfolio_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    name TEXT NOT NULL,
    benchmark_symbol TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES accounts(account_id)
);

CREATE TABLE IF NOT EXISTS position_cycles (
    position_cycle_id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',      -- planned/open/closed/cancelled
    phase TEXT NOT NULL DEFAULT 'accumulating', -- accumulating/holding/left_take_profit/right_trailing/stopped/closed
    strategy_version_id TEXT,
    observation_id TEXT,
    simulation_run_id TEXT,
    entry_plan_snapshot_json TEXT NOT NULL DEFAULT '{}',
    scheme_snapshot_json TEXT NOT NULL DEFAULT '{}',
    opened_at TEXT,
    closed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(portfolio_id) REFERENCES portfolios(portfolio_id)
);
CREATE INDEX IF NOT EXISTS idx_position_cycles_portfolio_status ON position_cycles(portfolio_id, status);

CREATE TABLE IF NOT EXISTS position_lots (
    lot_id TEXT PRIMARY KEY,
    position_cycle_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    quantity REAL NOT NULL,
    remaining_quantity REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_fee REAL NOT NULL DEFAULT 0,
    source_execution_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(position_cycle_id) REFERENCES position_cycles(position_cycle_id)
);

CREATE TABLE IF NOT EXISTS execution_lot_allocations (
    allocation_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    quantity REAL NOT NULL,
    cost_amount REAL NOT NULL,
    fee_allocated REAL NOT NULL DEFAULT 0,
    tax_allocated REAL NOT NULL DEFAULT 0,
    realized_pnl REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(execution_id, lot_id),
    FOREIGN KEY(execution_id) REFERENCES executions(execution_id),
    FOREIGN KEY(lot_id) REFERENCES position_lots(lot_id)
);
CREATE INDEX IF NOT EXISTS idx_execution_lot_allocations_execution ON execution_lot_allocations(execution_id);
CREATE INDEX IF NOT EXISTS idx_execution_lot_allocations_lot ON execution_lot_allocations(lot_id);

CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    position_cycle_id TEXT,
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,            -- BUY/SELL/CASH_DIVIDEND/FEE/CASH_ADJUSTMENT/CORRECTION/BONUS_SHARE/STOCK_SPLIT/RIGHTS_ISSUE/COST_ADJUSTMENT
    trade_time TEXT NOT NULL,
    quantity REAL,
    price REAL,
    gross_amount REAL,
    fee REAL NOT NULL DEFAULT 0,
    tax REAL NOT NULL DEFAULT 0,
    net_amount REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    advice_id TEXT,
    decision_id TEXT,
    simulation_run_id TEXT,
    external_ref TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(portfolio_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_executions_portfolio_time ON executions(portfolio_id, trade_time);

CREATE TABLE IF NOT EXISTS cash_ledger_entries (
    cash_entry_id TEXT PRIMARY KEY,
    portfolio_id TEXT NOT NULL,
    entry_type TEXT NOT NULL,            -- INITIAL/BUY/SELL/DIVIDEND/FEE/ADJUSTMENT/CORRECTION
    amount REAL NOT NULL,
    execution_id TEXT,
    balance_after REAL NOT NULL,
    entry_time TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(portfolio_id, execution_id, entry_type)
);

CREATE TABLE IF NOT EXISTS position_lot_adjustments (
    adjustment_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL,
    event_type TEXT NOT NULL,            -- BONUS_SHARE/STOCK_SPLIT/RIGHTS_ISSUE/COST_ADJUSTMENT
    quantity_delta REAL NOT NULL DEFAULT 0,
    price_factor REAL NOT NULL DEFAULT 1,
    event_time TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY(lot_id) REFERENCES position_lots(lot_id)
);

CREATE TABLE IF NOT EXISTS position_events (
    position_event_id TEXT PRIMARY KEY,
    position_cycle_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_time TEXT NOT NULL,
    old_phase TEXT NOT NULL DEFAULT '',
    new_phase TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    advice_id TEXT,
    decision_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(position_cycle_id) REFERENCES position_cycles(position_cycle_id)
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    position_cycle_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    quantity REAL NOT NULL,
    average_cost REAL,
    cost_basis REAL,
    market_price REAL,
    market_value REAL,
    unrealized_pnl REAL,
    realized_pnl REAL,
    return_rate REAL,
    phase TEXT NOT NULL DEFAULT '',
    data_context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

-- ============ 建议、通知与复盘 ============
CREATE TABLE IF NOT EXISTS advices (
    advice_id TEXT PRIMARY KEY,
    portfolio_id TEXT,
    position_cycle_id TEXT,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity REAL,
    price REAL,
    stop_price REAL,
    target_price REAL,
    reason TEXT NOT NULL DEFAULT '',
    triggered_rules_json TEXT NOT NULL DEFAULT '[]',
    strategy_version_id TEXT,
    data_as_of TEXT,
    valid_until TEXT,
    status TEXT NOT NULL DEFAULT 'generated',
    revision INTEGER NOT NULL DEFAULT 1,
    trigger_fingerprint TEXT NOT NULL DEFAULT '',
    last_notified_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    subject_type TEXT NOT NULL DEFAULT '',
    subject_id TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    advice_id TEXT,
    strategy_decision_id TEXT,
    report_id TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    dedupe_key TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(dedupe_key)
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    delivery_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL DEFAULT '',
    template TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',   -- pending/processing/sent/failed/dead/suppressed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    claimed_by TEXT NOT NULL DEFAULT '',
    claimed_at TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES notification_events(event_id)
);

CREATE TABLE IF NOT EXISTS daily_reports (
    report_id TEXT PRIMARY KEY,
    report_date TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    data_as_of TEXT,
    market_snapshot_json TEXT NOT NULL DEFAULT '{}',
    observation_snapshot_json TEXT NOT NULL DEFAULT '{}',
    portfolio_snapshot_json TEXT NOT NULL DEFAULT '{}',
    advice_ids_json TEXT NOT NULL DEFAULT '[]',
    sections_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_alerts (
    alert_id TEXT PRIMARY KEY,
    alert_type TEXT NOT NULL,
    resource TEXT NOT NULL DEFAULT '',
    failure_code TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'detected',
    priority INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    detected_at TEXT NOT NULL,
    notified_at TEXT,
    acknowledged_at TEXT,
    recovered_at TEXT,
    closed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_system_alerts_status ON system_alerts(status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_system_alert_active
    ON system_alerts(alert_type, resource, failure_code, status);

CREATE TABLE IF NOT EXISTS performance_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    query_id TEXT NOT NULL,
    as_of TEXT NOT NULL,
    cash REAL,
    market_value REAL,
    equity REAL,
    net_investment REAL,
    realized_pnl REAL,
    unrealized_pnl REAL,
    fees REAL,
    return_rate REAL,
    data_context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performance_comparisons (
    comparison_id TEXT PRIMARY KEY,
    portfolio_id TEXT,
    cycle_id TEXT,
    actual_return REAL,
    simulation_return REAL,
    benchmark_return REAL,
    actual_excess_vs_benchmark REAL,
    actual_gap_vs_simulation REAL,
    simulation_excess_vs_benchmark REAL,
    start_date TEXT,
    end_date TEXT,
    assumptions_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position_cycle_reviews (
    review_id TEXT PRIMARY KEY,
    position_cycle_id TEXT NOT NULL,
    discovery_reason TEXT NOT NULL DEFAULT '',
    research_summary TEXT NOT NULL DEFAULT '',
    simulation_run_id TEXT,
    planned_entry_json TEXT NOT NULL DEFAULT '{}',
    actual_entry_json TEXT NOT NULL DEFAULT '{}',
    planned_exit_json TEXT NOT NULL DEFAULT '{}',
    actual_exit_json TEXT NOT NULL DEFAULT '{}',
    advice_summary TEXT NOT NULL DEFAULT '',
    execution_deviation_json TEXT NOT NULL DEFAULT '{}',
    result_summary TEXT NOT NULL DEFAULT '',
    lessons TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_evidence (
    evidence_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(review_id) REFERENCES position_cycle_reviews(review_id)
);

-- ============ 业务任务与审计 ============
CREATE TABLE IF NOT EXISTS business_task_definitions (
    task_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    input_schema_json TEXT NOT NULL DEFAULT '{}',
    result_schema_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS business_task_config_versions (
    config_version_id TEXT PRIMARY KEY,
    task_key TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT NOT NULL,
    UNIQUE(task_key, version_no)
);

CREATE TABLE IF NOT EXISTS business_execution_requests (
    request_id TEXT PRIMARY KEY,
    task_key TEXT NOT NULL,
    config_version_id TEXT,
    trigger_type TEXT NOT NULL DEFAULT 'manual',
    input_json TEXT NOT NULL DEFAULT '{}',
    requested_at TEXT NOT NULL,
    FOREIGN KEY(task_key) REFERENCES business_task_definitions(task_key)
);

CREATE TABLE IF NOT EXISTS business_job_runs (
    run_id TEXT PRIMARY KEY,
    request_id TEXT,
    task_key TEXT NOT NULL,
    config_version TEXT,
    trigger_type TEXT NOT NULL DEFAULT 'manual',
    input_versions_json TEXT NOT NULL DEFAULT '{}',
    output_versions_json TEXT NOT NULL DEFAULT '{}',
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'requested',
    started_at TEXT,
    heartbeat_at TEXT,
    finished_at TEXT,
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_business_job_runs_status ON business_job_runs(status);
CREATE INDEX IF NOT EXISTS idx_business_job_runs_task ON business_job_runs(task_key);

CREATE TABLE IF NOT EXISTS business_job_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    event_time TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(run_id) REFERENCES business_job_runs(run_id)
);

CREATE TABLE IF NOT EXISTS business_task_locks (
    lock_key TEXT PRIMARY KEY,
    owner_run_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT,
    artifact_type TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    checksum TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_lineage (
    lineage_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    upstream_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    action TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_entity_map (
    map_id TEXT PRIMARY KEY,
    legacy_db TEXT NOT NULL,
    legacy_type TEXT NOT NULL,
    legacy_id TEXT NOT NULL,
    new_type TEXT NOT NULL,
    new_id TEXT NOT NULL,
    migration_version TEXT NOT NULL,
    classification TEXT NOT NULL DEFAULT 'migrated',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(legacy_db, legacy_type, legacy_id, migration_version)
);
"""


class BusinessDB:
    """business.db 连接与初始化。"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else business_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(migration_id, applied_at) VALUES('v1_initial', datetime('now'))"
            )

    def connect(self) -> sqlite3.Connection:
        return self._connect()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._connect() as conn:
            return conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple = ()) -> Any:
        with self._connect() as conn:
            return conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list:
        with self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def insert(self, table: str, data: dict) -> None:
        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        with self._connect() as conn:
            conn.execute(sql, tuple(data.values()))

    def upsert(self, table: str, data: dict, conflict_target: str) -> None:
        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?"] * len(data))
        updates = ", ".join(f"{k}=excluded.{k}" for k in data if k != conflict_target)
        sql = (
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict_target}) DO UPDATE SET {updates}"
        )
        with self._connect() as conn:
            conn.execute(sql, tuple(data.values()))

    def update(self, table: str, data: dict, where: str, where_params: tuple = ()) -> None:
        sets = ", ".join(f"{k}=?" for k in data)
        sql = f"UPDATE {table} SET {sets} WHERE {where}"
        params = tuple(data.values()) + tuple(where_params)
        with self._connect() as conn:
            conn.execute(sql, params)

    def transaction(self):
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        return conn


def _json_default(value: Any) -> Any:
    return value


def dumps_json(value: Any) -> str:
    import json
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def loads_json(value: str | None) -> Any:
    import json
    try:
        return json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}


def now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
