"""Simulation plan/run domain objects and state constants."""

from __future__ import annotations

from dataclasses import dataclass, field

from StockInvestmentTool.domain.features import now_text


SIM_PENDING = "PENDING"
SIM_RUNNING = "RUNNING"
SIM_SUCCESS = "SUCCESS"
SIM_FAILED = "FAILED"
SIM_CANCELLED = "CANCELLED"

TERMINAL_SIM_STATUSES = {SIM_SUCCESS, SIM_FAILED, SIM_CANCELLED}
VALID_SIM_STATUSES = {SIM_PENDING, SIM_RUNNING, *TERMINAL_SIM_STATUSES}

ALLOWED_SIM_TRANSITIONS = {
    SIM_PENDING: {SIM_RUNNING, SIM_CANCELLED},
    SIM_RUNNING: {SIM_SUCCESS, SIM_FAILED, SIM_CANCELLED},
    SIM_SUCCESS: set(),
    SIM_FAILED: set(),
    SIM_CANCELLED: set(),
}


@dataclass
class SimulationPlan:
    plan_id: str
    name: str
    stock_set_id: str
    start_date: str
    end_date: str
    initial_capital: float
    buy_rule_id: str = ""
    buy_rule_snapshot: dict = field(default_factory=dict)
    sell_rule_id: str = ""
    sell_rule_snapshot: dict = field(default_factory=dict)
    position_rule_json: dict = field(default_factory=dict)
    execution_rule_json: dict = field(default_factory=dict)
    risk_rule_json: dict = field(default_factory=dict)
    broker_fee_profile_id: str = ""
    created_at: str = field(default_factory=now_text)
    updated_at: str = field(default_factory=now_text)

    def validate(self) -> None:
        if not self.plan_id:
            raise ValueError("plan_id is required")
        if not self.name:
            raise ValueError("simulation plan name is required")
        if not self.stock_set_id:
            raise ValueError("stock_set_id is required")
        if not self.start_date or not self.end_date:
            raise ValueError("start_date and end_date are required")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be > 0")


@dataclass
class SimulationRun:
    run_id: str
    plan_id: str
    status: str = SIM_PENDING
    data_versions_json: dict = field(default_factory=dict)
    feature_versions_json: dict = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    progress: float = 0.0
    result_summary_json: dict = field(default_factory=dict)
    error_summary: str = ""
    created_at: str = field(default_factory=now_text)
    updated_at: str = field(default_factory=now_text)

    def validate(self) -> None:
        if not self.run_id:
            raise ValueError("run_id is required")
        if not self.plan_id:
            raise ValueError("plan_id is required")
        if self.status not in VALID_SIM_STATUSES:
            raise ValueError(f"invalid simulation status: {self.status}")
        if self.progress < 0 or self.progress > 100:
            raise ValueError("progress must be between 0 and 100")


@dataclass
class SimulationTrade:
    trade_id: str
    run_id: str
    instrument: str
    buy_signal_date: str = ""
    buy_date: str = ""
    buy_price: float = 0.0
    sell_signal_date: str = ""
    sell_date: str = ""
    sell_price: float = 0.0
    quantity: float = 0.0
    fees: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    buy_reason_json: dict = field(default_factory=dict)
    sell_reason_json: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_text)


@dataclass
class SimulationEvent:
    event_id: str
    run_id: str
    event_time: str
    instrument: str
    event_type: str
    payload_json: dict = field(default_factory=dict)
