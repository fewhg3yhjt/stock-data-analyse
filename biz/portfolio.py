# -*- coding: utf-8 -*-
"""账户、持仓与交易：PositionCycle / PositionLot / Execution / CashLedger + FIFO。

依据 docs/ACCOUNT_PORTFOLIO_AND_TRADING_DESIGN.md。
- Execution 是交易事实，Position 是聚合结果
- 现金余额 = 初始现金 + CashLedgerEntry.amount
- FIFO 批次核算，卖出优先匹配最早未结批次
- idempotency_key 保证重复提交不重复记账
- 建仓/买入/卖出/分红/现金调整在同一事务中完成
- 公司行为（送股/转增/拆股）经 PositionLotAdjustment 调整
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from StockInvestmentTool.biz.models import new_id, now_utc

logger = logging.getLogger(__name__)

# PositionCycle 状态
CYCLE_PLANNED = "planned"
CYCLE_OPEN = "open"
CYCLE_CLOSED = "closed"
CYCLE_CANCELLED = "cancelled"

# 阶段
PHASE_ACCUMULATING = "accumulating"
PHASE_HOLDING = "holding"
PHASE_LEFT_TAKE_PROFIT = "left_take_profit"
PHASE_RIGHT_TRAILING = "right_trailing"
PHASE_STOPPED = "stopped"
PHASE_CLOSED = "closed"

# Execution 事件类型
EVT_BUY = "BUY"
EVT_SELL = "SELL"
EVT_CASH_DIVIDEND = "CASH_DIVIDEND"
EVT_FEE = "FEE"
EVT_CASH_ADJUSTMENT = "CASH_ADJUSTMENT"
EVT_CORRECTION = "CORRECTION"
EVT_BONUS_SHARE = "BONUS_SHARE"
EVT_STOCK_SPLIT = "STOCK_SPLIT"
EVT_RIGHTS_ISSUE = "RIGHTS_ISSUE"
EVT_COST_ADJUSTMENT = "COST_ADJUSTMENT"

# CashLedger entry_type
LEDGER_INITIAL = "INITIAL"
LEDGER_BUY = "BUY"
LEDGER_SELL = "SELL"
LEDGER_DIVIDEND = "DIVIDEND"
LEDGER_FEE = "FEE"
LEDGER_ADJUSTMENT = "ADJUSTMENT"
LEDGER_CORRECTION = "CORRECTION"

# 状态机：phase 转移
PHASE_TRANSITIONS: dict[str, set[str]] = {
    PHASE_ACCUMULATING: {PHASE_HOLDING, PHASE_LEFT_TAKE_PROFIT, PHASE_RIGHT_TRAILING,
                         PHASE_STOPPED, PHASE_CLOSED},
    PHASE_HOLDING: {PHASE_LEFT_TAKE_PROFIT, PHASE_RIGHT_TRAILING, PHASE_STOPPED, PHASE_CLOSED},
    PHASE_LEFT_TAKE_PROFIT: {PHASE_RIGHT_TRAILING, PHASE_STOPPED, PHASE_CLOSED},
    PHASE_RIGHT_TRAILING: {PHASE_STOPPED, PHASE_CLOSED},
    PHASE_STOPPED: {PHASE_CLOSED},
    PHASE_CLOSED: set(),
}


class PositionStateError(ValueError):
    """非法持仓阶段转换。"""


@dataclass
class Account:
    account_id: str
    name: str
    currency: str = "CNY"
    account_type: str = "real"   # real/paper
    status: str = "active"
    created_at: str = field(default_factory=now_utc)
    updated_at: str = field(default_factory=now_utc)


@dataclass
class Portfolio:
    portfolio_id: str
    account_id: str
    name: str
    benchmark_symbol: str = ""
    status: str = "active"
    created_at: str = field(default_factory=now_utc)
    updated_at: str = field(default_factory=now_utc)


@dataclass
class PositionCycle:
    position_cycle_id: str
    portfolio_id: str
    symbol: str
    status: str = CYCLE_PLANNED
    phase: str = PHASE_ACCUMULATING
    strategy_version_id: str | None = None
    observation_id: str | None = None
    simulation_run_id: str | None = None
    entry_plan_snapshot: dict = field(default_factory=dict)
    scheme_snapshot: dict = field(default_factory=dict)
    opened_at: str | None = None
    closed_at: str | None = None
    created_at: str = field(default_factory=now_utc)
    updated_at: str = field(default_factory=now_utc)


@dataclass
class PositionLot:
    lot_id: str
    position_cycle_id: str
    symbol: str
    opened_at: str
    quantity: float
    remaining_quantity: float
    entry_price: float
    entry_fee: float = 0.0
    source_execution_id: str | None = None
    created_at: str = field(default_factory=now_utc)


@dataclass
class Execution:
    execution_id: str
    portfolio_id: str
    symbol: str
    event_type: str
    trade_time: str
    quantity: float | None = None
    price: float | None = None
    gross_amount: float | None = None
    fee: float = 0.0
    tax: float = 0.0
    net_amount: float = 0.0
    reason: str = ""
    advice_id: str | None = None
    decision_id: str | None = None
    position_cycle_id: str | None = None
    simulation_run_id: str | None = None
    external_ref: str = ""
    idempotency_key: str = ""
    created_at: str = field(default_factory=now_utc)


@dataclass
class CashLedgerEntry:
    cash_entry_id: str
    portfolio_id: str
    entry_type: str
    amount: float
    balance_after: float
    entry_time: str
    execution_id: str | None = None
    reason: str = ""
    created_at: str = field(default_factory=now_utc)


class PortfolioService:
    """持仓记账服务：账户/组合/周期/批次/现金统一入口。"""

    def __init__(self, repo=None):
        from StockInvestmentTool.biz.repo import BusinessRepository
        self.repo = repo or BusinessRepository()

    # ── 账户/组合 ─────────────────────────────────────────

    def ensure_default_account_portfolio(self, name: str = "默认组合") -> tuple[Account, Portfolio]:
        """新系统启动时创建默认账户与组合（幂等）。"""
        row = self.repo.db.fetchone("SELECT * FROM portfolios LIMIT 1")
        if row:
            acc = self.repo.db.fetchone("SELECT * FROM accounts WHERE account_id=?", (row["account_id"],))
            return Account(**dict(acc)), Portfolio(**dict(row))
        acc = Account(account_id=new_id("acc"), name="默认账户", account_type="real")
        pf = Portfolio(portfolio_id=new_id("pf"), account_id=acc.account_id, name=name)
        self.repo.db.insert("accounts", {
            "account_id": acc.account_id, "name": acc.name, "currency": acc.currency,
            "account_type": acc.account_type, "status": acc.status,
            "created_at": acc.created_at, "updated_at": acc.updated_at,
        })
        self.repo.db.insert("portfolios", {
            "portfolio_id": pf.portfolio_id, "account_id": pf.account_id, "name": pf.name,
            "benchmark_symbol": pf.benchmark_symbol, "status": pf.status,
            "created_at": pf.created_at, "updated_at": pf.updated_at,
        })
        return acc, pf

    def get_portfolio(self, portfolio_id: str) -> Portfolio | None:
        row = self.repo.db.fetchone("SELECT * FROM portfolios WHERE portfolio_id=?", (portfolio_id,))
        return Portfolio(**dict(row)) if row else None

    # ── 现金 ──────────────────────────────────────────────

    def cash_balance(self, portfolio_id: str) -> float:
        row = self.repo.db.fetchone(
            "SELECT balance_after FROM cash_ledger_entries WHERE portfolio_id=? "
            "ORDER BY rowid DESC LIMIT 1", (portfolio_id,))
        if row:
            return float(row["balance_after"])
        # 无流水则 0（初始现金由 INITIAL 流水显式建立）
        return 0.0

    def initialize_cash(self, portfolio_id: str, amount: float, *, reason: str = "初始现金") -> CashLedgerEntry:
        """建立 INITIAL 现金流水（幂等：同一组合只允许一条 INITIAL）。"""
        exists = self.repo.db.fetchone(
            "SELECT * FROM cash_ledger_entries WHERE portfolio_id=? AND entry_type=?",
            (portfolio_id, LEDGER_INITIAL))
        if exists:
            return self._row_to_ledger(exists)
        entry = CashLedgerEntry(
            cash_entry_id=new_id("cash"), portfolio_id=portfolio_id,
            entry_type=LEDGER_INITIAL, amount=amount, balance_after=amount,
            entry_time=now_utc(), reason=reason,
        )
        self._insert_ledger(entry)
        return entry

    # ── 建仓 / 交易 ───────────────────────────────────────

    def open_cycle(self, portfolio_id: str, symbol: str, *, strategy_version_id: str | None = None,
                   observation_id: str | None = None, entry_plan: dict | None = None,
                   scheme_snapshot: dict | None = None) -> PositionCycle:
        cycle = PositionCycle(
            position_cycle_id=new_id("pc"), portfolio_id=portfolio_id, symbol=symbol,
            status=CYCLE_OPEN, phase=PHASE_ACCUMULATING,
            strategy_version_id=strategy_version_id, observation_id=observation_id,
            entry_plan_snapshot=entry_plan or {}, scheme_snapshot=scheme_snapshot or {},
            opened_at=now_utc(),
        )
        self._insert_cycle(cycle)
        return cycle

    def record_execution(self, portfolio_id: str, cycle_id: str, *, event_type: str,
                         trade_time: str, quantity: float | None = None, price: float | None = None,
                         fee: float = 0.0, tax: float = 0.0, reason: str = "",
                         idempotency_key: str = "", advice_id: str | None = None,
                         decision_id: str | None = None, symbol: str | None = None) -> Execution:
        """录入一笔执行并记账（BUY/SELL/DIVIDEND 等），幂等。"""
        if not idempotency_key:
            raise ValueError("idempotency_key 必填")
        # A transaction must cover the fact, lot, ledger and phase updates.
        with self.repo.db.transaction() as conn:
            dup = conn.execute(
                "SELECT * FROM executions WHERE portfolio_id=? AND idempotency_key=?",
                (portfolio_id, idempotency_key)).fetchone()
            if dup:
                return self._row_to_execution(dup)

            cycle = self._get_cycle_conn(conn, cycle_id)
            sym = symbol or cycle.symbol
            gross = (quantity or 0) * (price or 0) if event_type in {EVT_BUY, EVT_SELL} else None
            net = (gross or 0) - fee - tax if event_type in {EVT_BUY, EVT_SELL} else \
                  (quantity or 0) if event_type in {EVT_CASH_DIVIDEND, EVT_CASH_ADJUSTMENT} else \
                  -fee if event_type == EVT_FEE else 0.0

            exec_obj = Execution(
                execution_id=new_id("exe"), portfolio_id=portfolio_id,
                position_cycle_id=cycle_id, symbol=sym, event_type=event_type,
                trade_time=trade_time, quantity=quantity, price=price, gross_amount=gross,
                fee=fee, tax=tax, net_amount=net, reason=reason,
                advice_id=advice_id, decision_id=decision_id, idempotency_key=idempotency_key,
            )
            self._insert_execution_conn(conn, exec_obj)

            if event_type == EVT_BUY:
                self._apply_buy_conn(conn, exec_obj)
            elif event_type == EVT_SELL:
                self._apply_sell_conn(conn, exec_obj)
            elif event_type == EVT_CASH_DIVIDEND:
                self._apply_cash_in_conn(conn, exec_obj, LEDGER_DIVIDEND, net)
            elif event_type == EVT_CASH_ADJUSTMENT:
                self._apply_cash_in_conn(conn, exec_obj, LEDGER_ADJUSTMENT, net)
            elif event_type == EVT_FEE:
                self._apply_cash_out_conn(conn, exec_obj, LEDGER_FEE, fee)
            elif event_type in {EVT_BONUS_SHARE, EVT_STOCK_SPLIT, EVT_RIGHTS_ISSUE, EVT_COST_ADJUSTMENT}:
                self._apply_company_action_conn(conn, exec_obj)
            return exec_obj

    # ── 内部记账 ──────────────────────────────────────────

    def _apply_buy(self, exe: Execution) -> None:
        cash = self.cash_balance(exe.portfolio_id)
        total_cost = exe.gross_amount + exe.fee + exe.tax
        if cash < total_cost:
            raise ValueError(f"现金不足: 需 {total_cost:.2f}，现有 {cash:.2f}")
        lot = PositionLot(
            lot_id=new_id("lot"), position_cycle_id=exe.position_cycle_id,
            symbol=exe.symbol, opened_at=exe.trade_time, quantity=exe.quantity or 0,
            remaining_quantity=exe.quantity or 0, entry_price=exe.price or 0,
            entry_fee=exe.fee + exe.tax, source_execution_id=exe.execution_id,
        )
        self._insert_lot(lot)
        self._append_ledger(exe, LEDGER_BUY, -total_cost)
        self._advance_phase(exe)

    def _apply_buy_conn(self, conn, exe: Execution) -> None:
        cash = self._cash_balance_conn(conn, exe.portfolio_id)
        total_cost = exe.gross_amount + exe.fee + exe.tax
        if cash < total_cost:
            raise ValueError(f"现金不足: 需 {total_cost:.2f}，现有 {cash:.2f}")
        lot = PositionLot(
            lot_id=new_id("lot"), position_cycle_id=exe.position_cycle_id,
            symbol=exe.symbol, opened_at=exe.trade_time, quantity=exe.quantity or 0,
            remaining_quantity=exe.quantity or 0, entry_price=exe.price or 0,
            entry_fee=exe.fee + exe.tax, source_execution_id=exe.execution_id,
        )
        self._insert_lot_conn(conn, lot)
        self._append_ledger_conn(conn, exe, LEDGER_BUY, -total_cost)
        self._advance_phase_conn(conn, exe)

    def _apply_sell(self, exe: Execution) -> None:
        qty = exe.quantity or 0
        remaining = self._sellable_quantity(exe.position_cycle_id)
        if qty > remaining:
            raise ValueError(f"卖出数量超持仓: 卖出 {qty}，可用 {remaining}")
        # FIFO 消耗
        lots = self.repo.db.fetchall(
            "SELECT * FROM position_lots WHERE position_cycle_id=? AND remaining_quantity>0 "
            "ORDER BY opened_at, lot_id", (exe.position_cycle_id,))
        sell_qty = qty
        for lot_row in lots:
            if sell_qty <= 0:
                break
            consume = min(lot_row["remaining_quantity"], sell_qty)
            new_remaining = lot_row["remaining_quantity"] - consume
            self.repo.db.update("position_lots", {"remaining_quantity": new_remaining},
                                "lot_id=?", (lot_row["lot_id"],))
            sell_qty -= consume
        proceeds = (exe.gross_amount or 0) - exe.fee - exe.tax
        self._append_ledger(exe, LEDGER_SELL, proceeds)
        self._advance_phase(exe)
        # 清仓
        if self._sellable_quantity(exe.position_cycle_id) == 0:
            self._close_cycle(exe.position_cycle_id)

    def _apply_sell_conn(self, conn, exe: Execution) -> None:
        qty = exe.quantity or 0
        remaining = self._sellable_quantity_conn(conn, exe.position_cycle_id)
        if qty > remaining:
            raise ValueError(f"卖出数量超持仓: 卖出 {qty}，可用 {remaining}")
        lots = conn.execute(
            "SELECT * FROM position_lots WHERE position_cycle_id=? AND remaining_quantity>0 "
            "ORDER BY opened_at, lot_id", (exe.position_cycle_id,)).fetchall()
        sell_qty = qty
        allocations = []
        for lot_row in lots:
            if sell_qty <= 0:
                break
            consume = min(lot_row["remaining_quantity"], sell_qty)
            conn.execute(
                "UPDATE position_lots SET remaining_quantity=? WHERE lot_id=?",
                (lot_row["remaining_quantity"] - consume, lot_row["lot_id"]))
            allocations.append({
                "lot_id": lot_row["lot_id"],
                "quantity": consume,
                "gross": consume * float(exe.price or 0),
                "cost": consume * float(lot_row["entry_price"] or 0),
                "entry_fee": (float(lot_row["entry_fee"] or 0)
                              * consume / float(lot_row["quantity"] or 1)),
            })
            sell_qty -= consume
        total_gross = sum(item["gross"] for item in allocations)
        for item in allocations:
            share = item["gross"] / total_gross if total_gross else 0.0
            fee_allocated = exe.fee * share
            tax_allocated = exe.tax * share
            realized = (item["gross"] - item["cost"] - item["entry_fee"]
                        - fee_allocated - tax_allocated)
            conn.execute(
                """INSERT INTO execution_lot_allocations
                   (allocation_id,execution_id,lot_id,quantity,cost_amount,fee_allocated,tax_allocated,realized_pnl,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (new_id("alloc"), exe.execution_id, item["lot_id"], item["quantity"],
                 item["cost"], fee_allocated, tax_allocated, realized, now_utc()),
            )
        proceeds = (exe.gross_amount or 0) - exe.fee - exe.tax
        self._append_ledger_conn(conn, exe, LEDGER_SELL, proceeds)
        self._advance_phase_conn(conn, exe)
        if self._sellable_quantity_conn(conn, exe.position_cycle_id) == 0:
            self._close_cycle_conn(conn, exe.position_cycle_id)

    def _apply_cash_in(self, exe: Execution, entry_type: str, amount: float) -> None:
        self._append_ledger(exe, entry_type, amount)

    def _apply_cash_in_conn(self, conn, exe: Execution, entry_type: str, amount: float) -> None:
        self._append_ledger_conn(conn, exe, entry_type, amount)

    def _apply_cash_out(self, exe: Execution, entry_type: str, amount: float) -> None:
        self._append_ledger(exe, entry_type, -amount)

    def _apply_cash_out_conn(self, conn, exe: Execution, entry_type: str, amount: float) -> None:
        self._append_ledger_conn(conn, exe, entry_type, -amount)

    def _apply_company_action(self, exe: Execution) -> None:
        """送股/转增/拆股：调整 Lot 数量与价格因子。"""
        if exe.quantity is not None and exe.quantity > 0:
            # quantity 表示新增股数（送股）或比例因子
            lots = self.repo.db.fetchall(
                "SELECT * FROM position_lots WHERE position_cycle_id=? AND remaining_quantity>0",
                (exe.position_cycle_id,))
            for lot_row in lots:
                old_qty = lot_row["remaining_quantity"]
                if exe.event_type in {EVT_BONUS_SHARE, EVT_STOCK_SPLIT}:
                    new_qty = old_qty + exe.quantity if exe.event_type == EVT_BONUS_SHARE else old_qty * (exe.quantity or 1)
                else:
                    new_qty = old_qty
                self.repo.db.update("position_lots", {"remaining_quantity": new_qty},
                                    "lot_id=?", (lot_row["lot_id"],))

    def _apply_company_action_conn(self, conn, exe: Execution) -> None:
        if exe.quantity is None or exe.quantity <= 0:
            return
        lots = conn.execute(
            "SELECT * FROM position_lots WHERE position_cycle_id=? AND remaining_quantity>0",
            (exe.position_cycle_id,)).fetchall()
        for lot_row in lots:
            old_qty = lot_row["remaining_quantity"]
            if exe.event_type == EVT_BONUS_SHARE:
                new_qty = old_qty + exe.quantity
            elif exe.event_type == EVT_STOCK_SPLIT:
                new_qty = old_qty * exe.quantity
            else:
                new_qty = old_qty
            conn.execute(
                "UPDATE position_lots SET remaining_quantity=? WHERE lot_id=?",
                (new_qty, lot_row["lot_id"]))

    # ── 辅助 ──────────────────────────────────────────────

    def _sellable_quantity(self, cycle_id: str) -> float:
        row = self.repo.db.fetchone(
            "SELECT COALESCE(SUM(remaining_quantity),0) s FROM position_lots WHERE position_cycle_id=?",
            (cycle_id,))
        return float(row["s"]) if row else 0.0

    @staticmethod
    def _cash_balance_conn(conn, portfolio_id: str) -> float:
        row = conn.execute(
            "SELECT balance_after FROM cash_ledger_entries WHERE portfolio_id=? "
            "ORDER BY rowid DESC LIMIT 1", (portfolio_id,)).fetchone()
        return float(row["balance_after"]) if row else 0.0

    @staticmethod
    def _sellable_quantity_conn(conn, cycle_id: str) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(remaining_quantity),0) s FROM position_lots WHERE position_cycle_id=?",
            (cycle_id,)).fetchone()
        return float(row["s"]) if row else 0.0

    def _advance_phase(self, exe: Execution) -> None:
        cycle = self._get_cycle(exe.position_cycle_id)
        # 买入后 holding，卖出后仍 holding/closed
        new_phase = PHASE_HOLDING if exe.event_type == EVT_BUY else cycle.phase
        allowed = PHASE_TRANSITIONS.get(cycle.phase, set())
        if new_phase in allowed or new_phase == cycle.phase:
            self.repo.db.update("position_cycles", {"phase": new_phase, "updated_at": now_utc()},
                                "position_cycle_id=?", (exe.position_cycle_id,))

    def _advance_phase_conn(self, conn, exe: Execution) -> None:
        row = conn.execute(
            "SELECT phase FROM position_cycles WHERE position_cycle_id=?",
            (exe.position_cycle_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown position cycle: {exe.position_cycle_id}")
        current = row["phase"]
        new_phase = PHASE_HOLDING if exe.event_type == EVT_BUY else current
        if new_phase in PHASE_TRANSITIONS.get(current, set()) or new_phase == current:
            conn.execute(
                "UPDATE position_cycles SET phase=?, updated_at=? WHERE position_cycle_id=?",
                (new_phase, now_utc(), exe.position_cycle_id))
            if new_phase != current:
                conn.execute(
                    "INSERT INTO position_events "
                    "(position_event_id,position_cycle_id,event_type,event_time,old_phase,new_phase,reason,advice_id,decision_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (new_id("pe"), exe.position_cycle_id, exe.event_type, exe.trade_time,
                     current, new_phase, exe.reason, exe.advice_id or "", exe.decision_id or "", now_utc()),
                )

    def _close_cycle(self, cycle_id: str) -> None:
        self.repo.db.update("position_cycles", {
            "status": CYCLE_CLOSED, "phase": PHASE_CLOSED, "closed_at": now_utc(),
            "updated_at": now_utc(),
        }, "position_cycle_id=?", (cycle_id,))

    @staticmethod
    def _close_cycle_conn(conn, cycle_id: str) -> None:
        ts = now_utc()
        row = conn.execute(
            "SELECT phase FROM position_cycles WHERE position_cycle_id=?", (cycle_id,)
        ).fetchone()
        conn.execute(
            "UPDATE position_cycles SET status=?, phase=?, closed_at=?, updated_at=? WHERE position_cycle_id=?",
            (CYCLE_CLOSED, PHASE_CLOSED, ts, ts, cycle_id))
        if row and row["phase"] != PHASE_CLOSED:
            conn.execute(
                "INSERT INTO position_events "
                "(position_event_id,position_cycle_id,event_type,event_time,old_phase,new_phase,reason,advice_id,decision_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (new_id("pe"), cycle_id, "CLOSED", ts, row["phase"], PHASE_CLOSED,
                 "持仓数量归零", "", "", ts),
            )

    def _append_ledger(self, exe: Execution, entry_type: str, amount: float) -> None:
        prev = self.cash_balance(exe.portfolio_id)
        entry = CashLedgerEntry(
            cash_entry_id=new_id("cash"), portfolio_id=exe.portfolio_id,
            entry_type=entry_type, amount=amount, balance_after=prev + amount,
            entry_time=exe.trade_time, execution_id=exe.execution_id, reason=exe.reason,
        )
        self._insert_ledger(entry)

    def _append_ledger_conn(self, conn, exe: Execution, entry_type: str, amount: float) -> None:
        prev = self._cash_balance_conn(conn, exe.portfolio_id)
        conn.execute(
            "INSERT INTO cash_ledger_entries "
            "(cash_entry_id,portfolio_id,entry_type,amount,balance_after,execution_id,entry_time,reason,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (new_id("cash"), exe.portfolio_id, entry_type, amount, prev + amount,
             exe.execution_id, exe.trade_time, exe.reason, now_utc()))

    def _insert_ledger(self, entry: CashLedgerEntry) -> None:
        self.repo.db.insert("cash_ledger_entries", {
            "cash_entry_id": entry.cash_entry_id, "portfolio_id": entry.portfolio_id,
            "entry_type": entry.entry_type, "amount": entry.amount,
            "balance_after": entry.balance_after, "execution_id": entry.execution_id or "",
            "entry_time": entry.entry_time, "reason": entry.reason,
            "created_at": entry.created_at,
        })

    @staticmethod
    def _insert_lot_conn(conn, lot: PositionLot) -> None:
        conn.execute(
            "INSERT INTO position_lots "
            "(lot_id,position_cycle_id,symbol,opened_at,quantity,remaining_quantity,entry_price,entry_fee,source_execution_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (lot.lot_id, lot.position_cycle_id, lot.symbol, lot.opened_at, lot.quantity,
             lot.remaining_quantity, lot.entry_price, lot.entry_fee,
             lot.source_execution_id or "", lot.created_at))

    def _insert_cycle(self, cycle: PositionCycle) -> None:
        self.repo.db.insert("position_cycles", {
            "position_cycle_id": cycle.position_cycle_id, "portfolio_id": cycle.portfolio_id,
            "symbol": cycle.symbol, "status": cycle.status, "phase": cycle.phase,
            "strategy_version_id": cycle.strategy_version_id or "",
            "observation_id": cycle.observation_id or "",
            "simulation_run_id": cycle.simulation_run_id or "",
            "entry_plan_snapshot_json": _dumps(cycle.entry_plan_snapshot),
            "scheme_snapshot_json": _dumps(cycle.scheme_snapshot),
            "opened_at": cycle.opened_at or "", "closed_at": cycle.closed_at or "",
            "created_at": cycle.created_at, "updated_at": cycle.updated_at,
        })

    def _insert_lot(self, lot: PositionLot) -> None:
        self.repo.db.insert("position_lots", {
            "lot_id": lot.lot_id, "position_cycle_id": lot.position_cycle_id,
            "symbol": lot.symbol, "opened_at": lot.opened_at, "quantity": lot.quantity,
            "remaining_quantity": lot.remaining_quantity, "entry_price": lot.entry_price,
            "entry_fee": lot.entry_fee, "source_execution_id": lot.source_execution_id or "",
            "created_at": lot.created_at,
        })

    def _insert_execution(self, exe: Execution) -> None:
        self.repo.db.insert("executions", {
            "execution_id": exe.execution_id, "portfolio_id": exe.portfolio_id,
            "position_cycle_id": exe.position_cycle_id or "", "symbol": exe.symbol,
            "event_type": exe.event_type, "trade_time": exe.trade_time,
            "quantity": exe.quantity, "price": exe.price, "gross_amount": exe.gross_amount,
            "fee": exe.fee, "tax": exe.tax, "net_amount": exe.net_amount, "reason": exe.reason,
            "advice_id": exe.advice_id or "", "decision_id": exe.decision_id or "",
            "simulation_run_id": exe.simulation_run_id or "",
            "external_ref": exe.external_ref, "idempotency_key": exe.idempotency_key,
            "created_at": exe.created_at,
        })

    @staticmethod
    def _insert_execution_conn(conn, exe: Execution) -> None:
        conn.execute(
            "INSERT INTO executions "
            "(execution_id,portfolio_id,position_cycle_id,symbol,event_type,trade_time,quantity,price,gross_amount,fee,tax,net_amount,reason,advice_id,decision_id,simulation_run_id,external_ref,idempotency_key,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (exe.execution_id, exe.portfolio_id, exe.position_cycle_id or "", exe.symbol,
             exe.event_type, exe.trade_time, exe.quantity, exe.price, exe.gross_amount,
             exe.fee, exe.tax, exe.net_amount, exe.reason, exe.advice_id or "",
             exe.decision_id or "", exe.simulation_run_id or "", exe.external_ref,
             exe.idempotency_key, exe.created_at))

    def _get_cycle_conn(self, conn, cycle_id: str) -> PositionCycle:
        row = conn.execute(
            "SELECT * FROM position_cycles WHERE position_cycle_id=?", (cycle_id,)).fetchone()
        if not row:
            raise KeyError(f"unknown position cycle: {cycle_id}")
        return self._row_to_cycle(row)

    def _get_cycle(self, cycle_id: str) -> PositionCycle:
        row = self.repo.db.fetchone("SELECT * FROM position_cycles WHERE position_cycle_id=?", (cycle_id,))
        if not row:
            raise KeyError(f"unknown position cycle: {cycle_id}")
        return PositionCycle(
            position_cycle_id=row["position_cycle_id"], portfolio_id=row["portfolio_id"],
            symbol=row["symbol"], status=row["status"], phase=row["phase"],
            strategy_version_id=row["strategy_version_id"] or None,
            observation_id=row["observation_id"] or None,
            simulation_run_id=row["simulation_run_id"] or None,
            entry_plan_snapshot=_loads(row["entry_plan_snapshot_json"]),
            scheme_snapshot=_loads(row["scheme_snapshot_json"]),
            opened_at=row["opened_at"] or None, closed_at=row["closed_at"] or None,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def list_cycles(self, portfolio_id: str) -> list[PositionCycle]:
        rows = self.repo.db.fetchall(
            "SELECT * FROM position_cycles WHERE portfolio_id=? ORDER BY created_at", (portfolio_id,))
        return [self._row_to_cycle(r) for r in rows]

    def get_cycle(self, cycle_id: str) -> PositionCycle | None:
        try:
            return self._get_cycle(cycle_id)
        except KeyError:
            return None

    def realized_pnl(self, cycle_id: str) -> float:
        """周期已实现收益（卖出收入 - 对应批次成本 - 费用）。"""
        row = self.repo.db.fetchone(
            """SELECT COALESCE(SUM(a.realized_pnl), 0) AS pnl
               FROM execution_lot_allocations a
               JOIN executions e ON e.execution_id=a.execution_id
               WHERE e.position_cycle_id=?""",
            (cycle_id,),
        )
        allocation_count = self.repo.db.fetchone(
            """SELECT COUNT(*) AS n
               FROM execution_lot_allocations a
               JOIN executions e ON e.execution_id=a.execution_id
               WHERE e.position_cycle_id=?""",
            (cycle_id,),
        )
        if allocation_count and allocation_count["n"]:
            return float(row["pnl"] or 0) if row else 0.0
        # Only legacy/reconstructed rows may lack allocation details.
        # For a current partial sale, do not charge all BUY costs to realized PnL.
        sells = self.repo.db.fetchall(
            "SELECT * FROM executions WHERE position_cycle_id=? AND event_type='SELL'",
            (cycle_id,),
        )
        if not sells:
            return 0.0
        return float(sum(
            (s["gross_amount"] or 0) - (s["fee"] or 0) - (s["tax"] or 0)
            for s in sells
        ))

    def position_summary(self, cycle_id: str) -> dict:
        cycle = self._get_cycle(cycle_id)
        qty = self._sellable_quantity(cycle_id)
        cost_basis = 0.0
        lots = self.repo.db.fetchall(
            "SELECT * FROM position_lots WHERE position_cycle_id=? AND remaining_quantity>0",
            (cycle_id,))
        for lot in lots:
            cost_basis += lot["remaining_quantity"] * lot["entry_price"] + lot["entry_fee"]
        avg_cost = cost_basis / qty if qty else None
        return {
            "cycle_id": cycle_id, "symbol": cycle.symbol, "status": cycle.status,
            "phase": cycle.phase, "quantity": qty, "average_cost": avg_cost,
            "cost_basis": cost_basis, "realized_pnl": self.realized_pnl(cycle_id),
        }

    @staticmethod
    def _row_to_ledger(row) -> CashLedgerEntry:
        return CashLedgerEntry(
            cash_entry_id=row["cash_entry_id"], portfolio_id=row["portfolio_id"],
            entry_type=row["entry_type"], amount=row["amount"],
            balance_after=row["balance_after"], entry_time=row["entry_time"],
            execution_id=row["execution_id"] or None, reason=row["reason"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_execution(row) -> Execution:
        return Execution(
            execution_id=row["execution_id"], portfolio_id=row["portfolio_id"],
            symbol=row["symbol"], event_type=row["event_type"], trade_time=row["trade_time"],
            quantity=row["quantity"], price=row["price"], gross_amount=row["gross_amount"],
            fee=row["fee"], tax=row["tax"], net_amount=row["net_amount"], reason=row["reason"],
            advice_id=row["advice_id"] or None, decision_id=row["decision_id"] or None,
            position_cycle_id=row["position_cycle_id"] or None,
            simulation_run_id=row["simulation_run_id"] or None,
            external_ref=row["external_ref"], idempotency_key=row["idempotency_key"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_cycle(row) -> PositionCycle:
        return PositionCycle(
            position_cycle_id=row["position_cycle_id"], portfolio_id=row["portfolio_id"],
            symbol=row["symbol"], status=row["status"], phase=row["phase"],
            strategy_version_id=row["strategy_version_id"] or None,
            observation_id=row["observation_id"] or None,
            simulation_run_id=row["simulation_run_id"] or None,
            entry_plan_snapshot=_loads(row["entry_plan_snapshot_json"]),
            scheme_snapshot=_loads(row["scheme_snapshot_json"]),
            opened_at=row["opened_at"] or None, closed_at=row["closed_at"] or None,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


def _dumps(value: Any) -> str:
    import json
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None) -> Any:
    import json
    try:
        return json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}
