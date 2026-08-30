"""SQLite repository for the first backend-domain persistence slice."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from StockInvestmentTool.config import Config
from StockInvestmentTool.domain.features import FeatureDefinition, now_text
from StockInvestmentTool.domain.rules import RuleDefinition
from StockInvestmentTool.domain.simulation import (
    SIM_PENDING,
    SimulationEvent,
    SimulationPlan,
    SimulationRun,
    SimulationTrade,
)
from StockInvestmentTool.domain.stock_sets import StockSet


def _dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _dumps_list(value: list[str]) -> str:
    return json.dumps(value or [], ensure_ascii=False, sort_keys=True)


def _loads(value: str) -> Any:
    return json.loads(value or "{}")


def _loads_list(value: str) -> list[str]:
    data = json.loads(value or "[]")
    return data if isinstance(data, list) else []


_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS feature_definitions (
    feature_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    value_type TEXT NOT NULL,
    required_context TEXT NOT NULL,
    supported_usages TEXT NOT NULL DEFAULT '[]',
    source_datasets TEXT NOT NULL DEFAULT '[]',
    calculation_key TEXT NOT NULL DEFAULT '',
    lookback_days INTEGER NOT NULL DEFAULT 0,
    frequency TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rule_definitions (
    rule_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    expression_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS stock_sets (
    stock_set_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL,
    source_ref_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS stock_set_members (
    stock_set_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(stock_set_id, instrument),
    FOREIGN KEY(stock_set_id) REFERENCES stock_sets(stock_set_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS simulation_plans (
    plan_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    stock_set_id TEXT NOT NULL,
    buy_rule_id TEXT NOT NULL DEFAULT '',
    buy_rule_snapshot TEXT NOT NULL DEFAULT '{}',
    sell_rule_id TEXT NOT NULL DEFAULT '',
    sell_rule_snapshot TEXT NOT NULL DEFAULT '{}',
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    initial_capital REAL NOT NULL,
    position_rule_json TEXT NOT NULL DEFAULT '{}',
    execution_rule_json TEXT NOT NULL DEFAULT '{}',
    risk_rule_json TEXT NOT NULL DEFAULT '{}',
    broker_fee_profile_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(stock_set_id) REFERENCES stock_sets(stock_set_id)
);

CREATE TABLE IF NOT EXISTS simulation_runs (
    run_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    data_versions_json TEXT NOT NULL DEFAULT '{}',
    feature_versions_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    progress REAL NOT NULL DEFAULT 0,
    result_summary_json TEXT NOT NULL DEFAULT '{}',
    error_summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(plan_id) REFERENCES simulation_plans(plan_id)
);

CREATE TABLE IF NOT EXISTS simulation_trades (
    trade_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    buy_signal_date TEXT NOT NULL DEFAULT '',
    buy_date TEXT NOT NULL DEFAULT '',
    buy_price REAL NOT NULL DEFAULT 0,
    sell_signal_date TEXT NOT NULL DEFAULT '',
    sell_date TEXT NOT NULL DEFAULT '',
    sell_price REAL NOT NULL DEFAULT 0,
    quantity REAL NOT NULL DEFAULT 0,
    fees REAL NOT NULL DEFAULT 0,
    pnl REAL NOT NULL DEFAULT 0,
    pnl_pct REAL NOT NULL DEFAULT 0,
    buy_reason_json TEXT NOT NULL DEFAULT '{}',
    sell_reason_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(run_id) REFERENCES simulation_runs(run_id)
);

CREATE TABLE IF NOT EXISTS simulation_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    event_time TEXT NOT NULL,
    instrument TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(run_id) REFERENCES simulation_runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_feature_usage ON feature_definitions(enabled, required_context);
CREATE INDEX IF NOT EXISTS idx_rule_status ON rule_definitions(status);
CREATE INDEX IF NOT EXISTS idx_stock_set_members_instrument ON stock_set_members(instrument);
CREATE INDEX IF NOT EXISTS idx_simulation_runs_status ON simulation_runs(status);
CREATE INDEX IF NOT EXISTS idx_simulation_runs_plan ON simulation_runs(plan_id);
CREATE INDEX IF NOT EXISTS idx_simulation_trades_run ON simulation_trades(run_id);
CREATE INDEX IF NOT EXISTS idx_simulation_events_run ON simulation_events(run_id);
"""


class BackendDomainRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else Config.DATA_DIR / "backend_domain.db"
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

    def save_feature(self, feature: FeatureDefinition) -> FeatureDefinition:
        feature.validate()
        feature.updated_at = now_text()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO feature_definitions
                   (feature_id,name,category,value_type,required_context,supported_usages,
                    source_datasets,calculation_key,lookback_days,frequency,version,enabled,
                    created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(feature_id) DO UPDATE SET
                    name=excluded.name, category=excluded.category,
                    value_type=excluded.value_type, required_context=excluded.required_context,
                    supported_usages=excluded.supported_usages,
                    source_datasets=excluded.source_datasets,
                    calculation_key=excluded.calculation_key,
                    lookback_days=excluded.lookback_days, frequency=excluded.frequency,
                    version=excluded.version, enabled=excluded.enabled,
                    updated_at=excluded.updated_at""",
                (
                    feature.feature_id,
                    feature.name,
                    feature.category,
                    feature.value_type,
                    feature.required_context,
                    _dumps_list(feature.supported_usages),
                    _dumps_list(feature.source_datasets),
                    feature.calculation_key,
                    feature.lookback_days,
                    feature.frequency,
                    feature.version,
                    1 if feature.enabled else 0,
                    feature.created_at,
                    feature.updated_at,
                ),
            )
        return feature

    def get_feature(self, feature_id: str) -> FeatureDefinition | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM feature_definitions WHERE feature_id=?", (feature_id,)
            ).fetchone()
        return self._row_to_feature(row) if row else None

    def list_features(self) -> list[FeatureDefinition]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM feature_definitions ORDER BY feature_id").fetchall()
        return [self._row_to_feature(row) for row in rows]

    def save_rule(self, rule: RuleDefinition) -> RuleDefinition:
        rule.validate()
        rule.updated_at = now_text()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO rule_definitions
                   (rule_id,name,expression_json,status,version,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(rule_id) DO UPDATE SET
                    name=excluded.name, expression_json=excluded.expression_json,
                    status=excluded.status, version=excluded.version,
                    updated_at=excluded.updated_at""",
                (
                    rule.rule_id,
                    rule.name,
                    _dumps(rule.expression_json),
                    rule.status,
                    rule.version,
                    rule.created_at,
                    rule.updated_at,
                ),
            )
        return rule

    def get_rule(self, rule_id: str) -> RuleDefinition | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM rule_definitions WHERE rule_id=?", (rule_id,)).fetchone()
        return self._row_to_rule(row) if row else None

    def create_stock_set(self, stock_set: StockSet) -> StockSet:
        stock_set.validate()
        stock_set.updated_at = now_text()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO stock_sets
                   (stock_set_id,name,description,source_type,source_ref_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    stock_set.stock_set_id,
                    stock_set.name,
                    stock_set.description,
                    stock_set.source_type,
                    stock_set.source_ref_id,
                    stock_set.created_at,
                    stock_set.updated_at,
                ),
            )
        return stock_set

    def get_stock_set(self, stock_set_id: str) -> StockSet | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM stock_sets WHERE stock_set_id=?", (stock_set_id,)).fetchone()
        return self._row_to_stock_set(row) if row else None

    def add_stock_set_members(self, stock_set_id: str, instruments: list[str]) -> int:
        added_at = now_text()
        unique = sorted({i.strip() for i in instruments if i and i.strip()})
        with self._connect() as conn:
            cur = conn.executemany(
                "INSERT OR IGNORE INTO stock_set_members(stock_set_id,instrument,added_at) VALUES(?,?,?)",
                [(stock_set_id, instrument, added_at) for instrument in unique],
            )
            conn.execute("UPDATE stock_sets SET updated_at=? WHERE stock_set_id=?", (added_at, stock_set_id))
            return cur.rowcount

    def remove_stock_set_members(self, stock_set_id: str, instruments: list[str]) -> int:
        with self._connect() as conn:
            cur = conn.executemany(
                "DELETE FROM stock_set_members WHERE stock_set_id=? AND instrument=?",
                [(stock_set_id, instrument) for instrument in instruments],
            )
            conn.execute("UPDATE stock_sets SET updated_at=? WHERE stock_set_id=?", (now_text(), stock_set_id))
            return cur.rowcount

    def list_stock_set_members(self, stock_set_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT instrument FROM stock_set_members WHERE stock_set_id=? ORDER BY instrument",
                (stock_set_id,),
            ).fetchall()
        return [row["instrument"] for row in rows]

    def save_simulation_plan(self, plan: SimulationPlan) -> SimulationPlan:
        plan.validate()
        plan.updated_at = now_text()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO simulation_plans
                   (plan_id,name,stock_set_id,buy_rule_id,buy_rule_snapshot,sell_rule_id,
                    sell_rule_snapshot,start_date,end_date,initial_capital,position_rule_json,
                    execution_rule_json,risk_rule_json,broker_fee_profile_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan.plan_id,
                    plan.name,
                    plan.stock_set_id,
                    plan.buy_rule_id,
                    _dumps(plan.buy_rule_snapshot),
                    plan.sell_rule_id,
                    _dumps(plan.sell_rule_snapshot),
                    plan.start_date,
                    plan.end_date,
                    plan.initial_capital,
                    _dumps(plan.position_rule_json),
                    _dumps(plan.execution_rule_json),
                    _dumps(plan.risk_rule_json),
                    plan.broker_fee_profile_id,
                    plan.created_at,
                    plan.updated_at,
                ),
            )
        return plan

    def get_simulation_plan(self, plan_id: str) -> SimulationPlan | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM simulation_plans WHERE plan_id=?", (plan_id,)).fetchone()
        return self._row_to_simulation_plan(row) if row else None

    def create_simulation_run(self, run: SimulationRun) -> SimulationRun:
        run.validate()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO simulation_runs
                   (run_id,plan_id,status,data_versions_json,feature_versions_json,started_at,
                    finished_at,progress,result_summary_json,error_summary,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run.run_id,
                    run.plan_id,
                    run.status or SIM_PENDING,
                    _dumps(run.data_versions_json),
                    _dumps(run.feature_versions_json),
                    run.started_at,
                    run.finished_at,
                    run.progress,
                    _dumps(run.result_summary_json),
                    run.error_summary,
                    run.created_at,
                    run.updated_at,
                ),
            )
        return run

    def get_simulation_run(self, run_id: str) -> SimulationRun | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM simulation_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._row_to_simulation_run(row) if row else None

    def update_simulation_run(self, run: SimulationRun) -> SimulationRun:
        run.validate()
        run.updated_at = now_text()
        with self._connect() as conn:
            conn.execute(
                """UPDATE simulation_runs SET status=?, data_versions_json=?, feature_versions_json=?,
                   started_at=?, finished_at=?, progress=?, result_summary_json=?, error_summary=?,
                   updated_at=? WHERE run_id=?""",
                (
                    run.status,
                    _dumps(run.data_versions_json),
                    _dumps(run.feature_versions_json),
                    run.started_at,
                    run.finished_at,
                    run.progress,
                    _dumps(run.result_summary_json),
                    run.error_summary,
                    run.updated_at,
                    run.run_id,
                ),
            )
        return run

    def add_simulation_trade(self, trade: SimulationTrade) -> SimulationTrade:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO simulation_trades
                   (trade_id,run_id,instrument,buy_signal_date,buy_date,buy_price,
                    sell_signal_date,sell_date,sell_price,quantity,fees,pnl,pnl_pct,
                    buy_reason_json,sell_reason_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trade.trade_id,
                    trade.run_id,
                    trade.instrument,
                    trade.buy_signal_date,
                    trade.buy_date,
                    trade.buy_price,
                    trade.sell_signal_date,
                    trade.sell_date,
                    trade.sell_price,
                    trade.quantity,
                    trade.fees,
                    trade.pnl,
                    trade.pnl_pct,
                    _dumps(trade.buy_reason_json),
                    _dumps(trade.sell_reason_json),
                    trade.created_at,
                ),
            )
        return trade

    def list_simulation_trades(self, run_id: str) -> list[SimulationTrade]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM simulation_trades WHERE run_id=? ORDER BY buy_date, trade_id", (run_id,)
            ).fetchall()
        return [self._row_to_simulation_trade(row) for row in rows]

    def add_simulation_event(self, event: SimulationEvent) -> SimulationEvent:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO simulation_events(event_id,run_id,event_time,instrument,event_type,payload_json)
                   VALUES(?,?,?,?,?,?)""",
                (
                    event.event_id,
                    event.run_id,
                    event.event_time,
                    event.instrument,
                    event.event_type,
                    _dumps(event.payload_json),
                ),
            )
        return event

    def list_simulation_events(self, run_id: str) -> list[SimulationEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM simulation_events WHERE run_id=? ORDER BY event_time, event_id", (run_id,)
            ).fetchall()
        return [self._row_to_simulation_event(row) for row in rows]

    @staticmethod
    def _row_to_feature(row: sqlite3.Row) -> FeatureDefinition:
        return FeatureDefinition(
            feature_id=row["feature_id"],
            name=row["name"],
            category=row["category"],
            value_type=row["value_type"],
            required_context=row["required_context"],
            supported_usages=_loads_list(row["supported_usages"]),
            source_datasets=_loads_list(row["source_datasets"]),
            calculation_key=row["calculation_key"],
            lookback_days=int(row["lookback_days"]),
            frequency=row["frequency"],
            version=int(row["version"]),
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_rule(row: sqlite3.Row) -> RuleDefinition:
        return RuleDefinition(
            rule_id=row["rule_id"],
            name=row["name"],
            expression_json=_loads(row["expression_json"]),
            status=row["status"],
            version=int(row["version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_stock_set(row: sqlite3.Row) -> StockSet:
        return StockSet(
            stock_set_id=row["stock_set_id"],
            name=row["name"],
            description=row["description"],
            source_type=row["source_type"],
            source_ref_id=row["source_ref_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_simulation_plan(row: sqlite3.Row) -> SimulationPlan:
        return SimulationPlan(
            plan_id=row["plan_id"],
            name=row["name"],
            stock_set_id=row["stock_set_id"],
            buy_rule_id=row["buy_rule_id"],
            buy_rule_snapshot=_loads(row["buy_rule_snapshot"]),
            sell_rule_id=row["sell_rule_id"],
            sell_rule_snapshot=_loads(row["sell_rule_snapshot"]),
            start_date=row["start_date"],
            end_date=row["end_date"],
            initial_capital=float(row["initial_capital"]),
            position_rule_json=_loads(row["position_rule_json"]),
            execution_rule_json=_loads(row["execution_rule_json"]),
            risk_rule_json=_loads(row["risk_rule_json"]),
            broker_fee_profile_id=row["broker_fee_profile_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_simulation_run(row: sqlite3.Row) -> SimulationRun:
        return SimulationRun(
            run_id=row["run_id"],
            plan_id=row["plan_id"],
            status=row["status"],
            data_versions_json=_loads(row["data_versions_json"]),
            feature_versions_json=_loads(row["feature_versions_json"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            progress=float(row["progress"]),
            result_summary_json=_loads(row["result_summary_json"]),
            error_summary=row["error_summary"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_simulation_trade(row: sqlite3.Row) -> SimulationTrade:
        return SimulationTrade(
            trade_id=row["trade_id"],
            run_id=row["run_id"],
            instrument=row["instrument"],
            buy_signal_date=row["buy_signal_date"],
            buy_date=row["buy_date"],
            buy_price=float(row["buy_price"]),
            sell_signal_date=row["sell_signal_date"],
            sell_date=row["sell_date"],
            sell_price=float(row["sell_price"]),
            quantity=float(row["quantity"]),
            fees=float(row["fees"]),
            pnl=float(row["pnl"]),
            pnl_pct=float(row["pnl_pct"]),
            buy_reason_json=_loads(row["buy_reason_json"]),
            sell_reason_json=_loads(row["sell_reason_json"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_simulation_event(row: sqlite3.Row) -> SimulationEvent:
        return SimulationEvent(
            event_id=row["event_id"],
            run_id=row["run_id"],
            event_time=row["event_time"],
            instrument=row["instrument"],
            event_type=row["event_type"],
            payload_json=_loads(row["payload_json"]),
        )
