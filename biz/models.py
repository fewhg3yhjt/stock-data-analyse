# -*- coding: utf-8 -*-
"""核心领域对象：DataContext / StrategyContext / StrategyDecision / ConditionSpec /
RuleSpec / SimulationPlan / SimulationRun / SimulationFill / SimulationResult。

依据 docs/DOMAIN_MODEL_AND_CONTRACTS.md 与 docs/STRATEGY_CORE_AND_SIMULATION_DESIGN.md。
纯 dataclass + 校验，不含 IO。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def stable_hash(data: Any) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)


def loads(value: str | None) -> Any:
    try:
        return json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# DataContext
# ---------------------------------------------------------------------------

@dataclass
class DataContext:
    """一次业务运行的统一数据上下文。"""

    dataset_refs: dict = field(default_factory=dict)      # dataset_name -> {version, quality}
    indicator_refs: dict = field(default_factory=dict)
    requested_start: str | None = None
    requested_end: str | None = None
    returned_start: str | None = None
    returned_end: str | None = None
    quality_status: str = "unknown"
    source: str = ""
    fallback_used: bool = False
    is_stale: bool = False

    def to_dict(self) -> dict:
        return {
            "dataset_refs": self.dataset_refs,
            "indicator_refs": self.indicator_refs,
            "requested_start": self.requested_start,
            "requested_end": self.requested_end,
            "returned_start": self.returned_start,
            "returned_end": self.returned_end,
            "quality_status": self.quality_status,
            "source": self.source,
            "fallback_used": self.fallback_used,
            "is_stale": self.is_stale,
        }

    @staticmethod
    def from_dict(data: dict) -> "DataContext":
        return DataContext(**data)


# ---------------------------------------------------------------------------
# ConditionSpec / RuleSpec
# ---------------------------------------------------------------------------

# 条件类型
CONDITION_TYPES = {"comparison", "cross", "between", "consecutive", "count", "and", "or", "not"}

# 动作
ACTIONS = {"WATCH", "BUY", "BUY_MORE", "SELL_PARTIAL", "SELL_ALL", "HOLD", "WAIT", "NO_ACTION"}


def validate_condition_spec(spec: dict) -> list[str]:
    """校验 ConditionSpec，返回错误列表（空表示合法）。"""
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["condition must be a dict"]
    ctype = spec.get("type")
    if ctype not in CONDITION_TYPES:
        return [f"unsupported condition type: {ctype!r}"]
    if ctype == "and" or ctype == "or":
        children = spec.get("conditions", [])
        if not isinstance(children, list) or not children:
            errors.append(f"{ctype} requires non-empty conditions list")
        for child in children:
            errors.extend(validate_condition_spec(child))
    elif ctype == "not":
        child = spec.get("condition")
        if not isinstance(child, dict):
            errors.append("not requires a condition")
        else:
            errors.extend(validate_condition_spec(child))
    elif ctype == "consecutive":
        days = spec.get("days")
        if not isinstance(days, int) or days < 1:
            errors.append("consecutive requires days >= 1")
        if not isinstance(spec.get("condition"), dict):
            errors.append("consecutive requires a condition")
    elif ctype == "comparison":
        if "left" not in spec or "right" not in spec or "operator" not in spec:
            errors.append("comparison requires left/operator/right")
    elif ctype == "cross":
        if "left" not in spec or "right" not in spec or "direction" not in spec:
            errors.append("cross requires left/direction/right")
    elif ctype == "between":
        if "field" not in spec or "low" not in spec or "high" not in spec:
            errors.append("between requires field/low/high")
    elif ctype == "count":
        if "condition" not in spec or "window" not in spec or "gte" not in spec:
            errors.append("count requires condition/window/gte")
    return errors


@dataclass
class ConditionEvalResult:
    """一次条件评估结果。"""

    passed: bool
    actual_values: dict = field(default_factory=dict)
    threshold_values: dict = field(default_factory=dict)
    explanation: str = ""
    dependencies: list = field(default_factory=list)
    evaluation_status: str = "evaluated"   # evaluated / missing_data / error

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "actual_values": self.actual_values,
            "threshold_values": self.threshold_values,
            "explanation": self.explanation,
            "dependencies": self.dependencies,
            "evaluation_status": self.evaluation_status,
        }


# ---------------------------------------------------------------------------
# StrategyContext / StrategyDecision
# ---------------------------------------------------------------------------

@dataclass
class StrategyContext:
    """策略、选股、研究和模拟的统一运行时输入。"""

    symbol: str
    evaluation_time: str
    data_as_of: str
    market_data: Any = None               # pd.DataFrame 或 None
    indicator_context: Any = None         # IndicatorContext 或 None
    fundamental_values: dict = field(default_factory=dict)
    market_regime: dict | None = None
    position_state: str = "none"
    position_quantity: float = 0.0
    position_state_avg_cost: float | None = None
    cash_available: float = 0.0
    previous_decisions: list = field(default_factory=list)
    data_context: DataContext = field(default_factory=DataContext)


@dataclass
class StrategyDecision:
    """一次策略评估的不可变结果。"""

    decision_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    decision_time: str
    data_as_of: str
    action: str
    quantity_ratio: float | None = None
    price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    input_dependencies: list = field(default_factory=list)
    input_snapshot: dict = field(default_factory=dict)
    decision_trace: dict = field(default_factory=dict)
    reason: str = ""
    valid_until: str | None = None
    # 关联（可选）
    strategy_version_id: str | None = None
    research_run_id: str | None = None
    simulation_run_id: str | None = None
    observation_id: str | None = None
    position_cycle_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "strategy_version_id": self.strategy_version_id,
            "symbol": self.symbol,
            "decision_time": self.decision_time,
            "data_as_of": self.data_as_of,
            "action": self.action,
            "quantity_ratio": self.quantity_ratio,
            "price": self.price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "input_dependencies": self.input_dependencies,
            "input_snapshot": self.input_snapshot,
            "decision_trace": self.decision_trace,
            "reason": self.reason,
            "valid_until": self.valid_until,
            "research_run_id": self.research_run_id,
            "simulation_run_id": self.simulation_run_id,
            "observation_id": self.observation_id,
            "position_cycle_id": self.position_cycle_id,
        }


# ---------------------------------------------------------------------------
# 模拟对象
# ---------------------------------------------------------------------------

SIM_STATUSES = {"requested", "running", "success", "partial_success", "failed", "cancelled"}


@dataclass
class SimulationPlan:
    plan_id: str
    strategy_version_id: str
    start_date: str
    end_date: str
    initial_cash: float
    name: str = ""
    universe_snapshot_id: str | None = None
    source_screen_run_id: str | None = None
    observation_id: str | None = None
    position_sizing: dict = field(default_factory=dict)
    execution_rules: dict = field(default_factory=dict)
    cost_config: dict = field(default_factory=dict)
    benchmark: str = ""
    data_context: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_utc)
    updated_at: str = field(default_factory=now_utc)

    def validate(self) -> None:
        if not self.plan_id or not self.strategy_version_id:
            raise ValueError("plan_id and strategy_version_id are required")
        if not self.start_date or not self.end_date:
            raise ValueError("start_date/end_date are required")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be > 0")


@dataclass
class SimulationRun:
    run_id: str
    plan_id: str
    status: str = "requested"
    data_context: dict = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    error: str = ""

    def validate(self) -> None:
        if not self.run_id or not self.plan_id:
            raise ValueError("run_id and plan_id are required")
        if self.status not in SIM_STATUSES:
            raise ValueError(f"invalid status: {self.status}")


@dataclass
class SimulationFill:
    """单边模拟成交。"""

    fill_id: str
    simulation_run_id: str
    symbol: str
    side: str                        # BUY / SELL
    signal_time: str
    execution_time: str | None = None
    signal_price: float | None = None
    execution_price: float | None = None
    quantity: float = 0.0
    gross_amount: float = 0.0
    fee: float = 0.0
    tax: float = 0.0
    slippage: float = 0.0
    decision_id: str | None = None
    reason: str = ""
    created_at: str = field(default_factory=now_utc)

    def validate(self) -> None:
        if self.side not in {"BUY", "SELL"}:
            raise ValueError(f"side must be BUY or SELL, got {self.side}")
        if self.quantity <= 0:
            raise ValueError("quantity must be > 0")


@dataclass
class SimulationLot:
    lot_id: str
    simulation_run_id: str
    symbol: str
    opened_at: str
    quantity: float
    remaining_quantity: float
    entry_price: float
    entry_fee: float = 0.0
    source_fill_id: str | None = None


@dataclass
class SimulationEvent:
    event_id: str
    simulation_run_id: str
    event_type: str
    event_time: str = field(default_factory=now_utc)
    symbol: str = ""
    payload: dict = field(default_factory=dict)


@dataclass
class SimulationResult:
    run_id: str
    initial_cash: float
    final_equity: float
    total_return: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    max_drawdown: float | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    trade_count: int = 0
    average_holding_days: float | None = None
    fees: float = 0.0
    slippage: float = 0.0
    equity_curve: list = field(default_factory=list)
    comparison_status: str = "unavailable"
    created_at: str = field(default_factory=now_utc)
