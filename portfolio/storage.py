"""SQLite 持久化 — 持仓/交易/自选池/建议 的 CRUD

设计:
  - 单一数据库文件 portfolio.db（放在 Config.DATA_DIR）
  - 默认组合（id=1）自动创建
  - scheme_snapshot / check_results 存 JSON 文本
  - 所有写操作带事务，保证一致性
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from StockInvestmentTool.config import Config
from StockInvestmentTool.portfolio.models import (
    ActionAdvice,
    Position,
    Portfolio,
    Simulation,
    Transaction,
    WatchlistItem,
    json_dumps,
    json_loads,
    _now,
)

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolios (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '默认组合',
    cash_available REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL DEFAULT 1,
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    stock_type TEXT NOT NULL DEFAULT 'B',
    scheme_name TEXT NOT NULL DEFAULT '',
    scheme_snapshot TEXT NOT NULL DEFAULT '{}',
    total_shares REAL NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0,
    total_cost REAL NOT NULL DEFAULT 0,
    current_price REAL NOT NULL DEFAULT 0,
    peak_price REAL NOT NULL DEFAULT 0,
    position_phase TEXT NOT NULL DEFAULT 'accumulating',
    buy_stage INTEGER NOT NULL DEFAULT 0,
    left_tier_sold INTEGER NOT NULL DEFAULT 0,
    stop_loss_price REAL NOT NULL DEFAULT 0,
    buy_date TEXT NOT NULL DEFAULT '',
    last_operated_date TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    trans_type TEXT NOT NULL,
    date TEXT NOT NULL DEFAULT '',
    price REAL NOT NULL DEFAULT 0,
    shares REAL NOT NULL DEFAULT 0,
    amount REAL NOT NULL DEFAULT 0,
    fee REAL NOT NULL DEFAULT 0,
    pnl REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL,
    stock_name TEXT NOT NULL,
    asset_type TEXT NOT NULL DEFAULT 'stock',
    target_capital REAL NOT NULL DEFAULT 0,
    weak_support REAL NOT NULL DEFAULT 0,
    strong_support REAL NOT NULL DEFAULT 0,
    extreme_anchor REAL NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    added_time TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS advices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL,
    stock_code TEXT NOT NULL DEFAULT '',
    stock_name TEXT NOT NULL DEFAULT '',
    advice_type TEXT NOT NULL DEFAULT 'hold',
    urgency TEXT NOT NULL DEFAULT 'normal',
    reason TEXT NOT NULL DEFAULT '',
    suggested_price REAL NOT NULL DEFAULT 0,
    suggested_shares REAL NOT NULL DEFAULT 0,
    suggested_amount REAL NOT NULL DEFAULT 0,
    check_results TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS simulations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_code TEXT NOT NULL UNIQUE,
    stock_name TEXT NOT NULL DEFAULT '',
    scheme_name TEXT NOT NULL DEFAULT 'default_value',
    stock_type TEXT NOT NULL DEFAULT 'B',
    snapshot TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_positions_code ON positions(stock_code);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_transactions_pos ON transactions(position_id);
CREATE INDEX IF NOT EXISTS idx_advices_pos ON advices(position_id);
"""


class PortfolioStorage:
    """SQLite 持仓存储"""

    def __init__(self, db_path: Optional[Path | str] = None):
        self.db_path = Path(db_path) if db_path else Config.DATA_DIR / "portfolio.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── 连接管理 ─────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """建表 + 确保默认组合存在 + 轻量迁移"""
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # 迁移：watchlist 增加 added_time（观察起点），老库补默认当天
            cols = {r[1] for r in conn.execute("PRAGMA table_info(watchlist)").fetchall()}
            if "added_time" not in cols:
                conn.execute("ALTER TABLE watchlist ADD COLUMN added_time TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE watchlist SET added_time=? WHERE added_time='' OR added_time IS NULL",
                         (datetime.now().strftime("%Y-%m-%d"),))
            # 确保默认组合
            cur = conn.execute("SELECT id FROM portfolios WHERE id=1")
            if cur.fetchone() is None:
                conn.execute(
                    "INSERT INTO portfolios (id, name, cash_available, created_at) VALUES (1, ?, ?, ?)",
                    ("默认组合", 0.0, _now()),
                )
                logger.info("初始化默认组合")

    # ══════════════════════════════════════════════════
    # Portfolio
    # ══════════════════════════════════════════════════

    def get_portfolio(self, portfolio_id: int = 1) -> Portfolio:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM portfolios WHERE id=?", (portfolio_id,)).fetchone()
        if row is None:
            return Portfolio(id=portfolio_id, name="默认组合")
        return Portfolio(
            id=row["id"], name=row["name"],
            cash_available=row["cash_available"], created_at=row["created_at"],
        )

    def set_cash(self, cash: float, portfolio_id: int = 1):
        with self._connect() as conn:
            conn.execute(
                "UPDATE portfolios SET cash_available=? WHERE id=?", (cash, portfolio_id)
            )

    def adjust_cash(self, delta: float, portfolio_id: int = 1) -> float:
        """调整可用资金，返回调整后余额"""
        with self._connect() as conn:
            row = conn.execute("SELECT cash_available FROM portfolios WHERE id=?", (portfolio_id,)).fetchone()
            new_cash = max(0.0, (row["cash_available"] if row else 0.0) + delta)
            conn.execute(
                "UPDATE portfolios SET cash_available=? WHERE id=?", (new_cash, portfolio_id)
            )
            return new_cash

    # ══════════════════════════════════════════════════
    # Position
    # ══════════════════════════════════════════════════

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Position:
        return Position(
            id=row["id"],
            portfolio_id=row["portfolio_id"],
            stock_code=row["stock_code"],
            stock_name=row["stock_name"],
            stock_type=row["stock_type"],
            scheme_name=row["scheme_name"],
            scheme_snapshot=json_loads(row["scheme_snapshot"]),
            total_shares=row["total_shares"],
            avg_cost=row["avg_cost"],
            total_cost=row["total_cost"],
            current_price=row["current_price"],
            peak_price=row["peak_price"],
            position_phase=row["position_phase"],
            buy_stage=row["buy_stage"],
            left_tier_sold=row["left_tier_sold"],
            stop_loss_price=row["stop_loss_price"],
            buy_date=row["buy_date"],
            last_operated_date=row["last_operated_date"],
            status=row["status"],
            notes=row["notes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_position(self, position: Position) -> Position:
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO positions
                   (portfolio_id, stock_code, stock_name, stock_type, scheme_name, scheme_snapshot,
                    total_shares, avg_cost, total_cost, current_price, peak_price,
                    position_phase, buy_stage, left_tier_sold, stop_loss_price,
                    buy_date, last_operated_date, status, notes, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (position.portfolio_id, position.stock_code, position.stock_name,
                 position.stock_type, position.scheme_name, json_dumps(position.scheme_snapshot),
                 position.total_shares, position.avg_cost, position.total_cost,
                 position.current_price, position.peak_price,
                 position.position_phase, position.buy_stage, position.left_tier_sold,
                 position.stop_loss_price,
                 position.buy_date, position.last_operated_date,
                 position.status, position.notes, now, now),
            )
            position.id = cur.lastrowid
            position.created_at = now
            position.updated_at = now
        return position

    def get_position(self, position_id: int) -> Optional[Position]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        return self._row_to_position(row) if row else None

    def get_positions(self, status: Optional[str] = None) -> list[Position]:
        """列出持仓；status=None 返回全部"""
        sql = "SELECT * FROM positions"
        params = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_position(r) for r in rows]

    def get_open_positions(self) -> list[Position]:
        return self.get_positions(status="open")

    def update_position(self, position: Position):
        position.updated_at = _now()
        with self._connect() as conn:
            conn.execute(
                """UPDATE positions SET
                   scheme_name=?, scheme_snapshot=?, total_shares=?, avg_cost=?,
                   total_cost=?, current_price=?, peak_price=?, position_phase=?,
                   buy_stage=?, left_tier_sold=?, stop_loss_price=?,
                   last_operated_date=?, status=?, notes=?, updated_at=?
                   WHERE id=?""",
                (position.scheme_name, json_dumps(position.scheme_snapshot),
                 position.total_shares, position.avg_cost,
                 position.total_cost, position.current_price,
                 position.peak_price, position.position_phase,
                 position.buy_stage, position.left_tier_sold,
                 position.stop_loss_price, position.last_operated_date,
                 position.status, position.notes, position.updated_at, position.id),
            )

    def delete_position(self, position_id: int):
        """删除持仓（级联删除交易与建议）"""
        with self._connect() as conn:
            conn.execute("DELETE FROM transactions WHERE position_id=?", (position_id,))
            conn.execute("DELETE FROM advices WHERE position_id=?", (position_id,))
            conn.execute("DELETE FROM positions WHERE id=?", (position_id,))

    def get_position_by_code(self, stock_code: str, status: str = "open") -> Optional[Position]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM positions WHERE stock_code=? AND status=? LIMIT 1",
                (stock_code, status),
            ).fetchone()
        return self._row_to_position(row) if row else None

    # ══════════════════════════════════════════════════
    # Transaction
    # ══════════════════════════════════════════════════

    def add_transaction(self, txn: Transaction) -> Transaction:
        txn.created_at = txn.created_at or _now()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO transactions
                   (position_id, trans_type, date, price, shares, amount, fee, pnl, reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (txn.position_id, txn.trans_type, txn.date, txn.price,
                 txn.shares, txn.amount, txn.fee, txn.pnl, txn.reason, txn.created_at),
            )
            txn.id = cur.lastrowid
        return txn

    def get_transactions(self, position_id: int) -> list[Transaction]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE position_id=? ORDER BY date, id",
                (position_id,),
            ).fetchall()
        return [self._txn_from_row(r) for r in rows]

    def get_transaction(self, transaction_id: int) -> Optional[Transaction]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM transactions WHERE id=?", (transaction_id,),
            ).fetchone()
        return self._txn_from_row(row) if row else None

    def update_transaction_reason(self, transaction_id: int, reason: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE transactions SET reason=? WHERE id=?",
                (reason, transaction_id),
            )

    @staticmethod
    def _txn_from_row(row: sqlite3.Row) -> Transaction:
        return Transaction(
            id=row["id"], position_id=row["position_id"], trans_type=row["trans_type"],
            date=row["date"], price=row["price"], shares=row["shares"],
            amount=row["amount"], fee=row["fee"], pnl=row["pnl"],
            reason=row["reason"], created_at=row["created_at"],
        )

    def realized_pnl(self, position_id: int) -> float:
        """持仓累计已实现盈亏（sell/sell_all/dividend 的 pnl 之和）"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl),0) AS total FROM transactions WHERE position_id=?",
                (position_id,),
            ).fetchone()
        return row["total"] if row else 0.0

    # ══════════════════════════════════════════════════
    # Watchlist
    # ══════════════════════════════════════════════════

    @staticmethod
    def _wl_from_row(row: sqlite3.Row) -> WatchlistItem:
        return WatchlistItem(
            id=row["id"], stock_code=row["stock_code"], stock_name=row["stock_name"],
            asset_type=row["asset_type"], target_capital=row["target_capital"],
            weak_support=row["weak_support"], strong_support=row["strong_support"],
            extreme_anchor=row["extreme_anchor"], notes=row["notes"],
            added_time=row["added_time"],
        )

    def add_watchlist(self, item: WatchlistItem) -> WatchlistItem:
        if not item.added_time:
            item.added_time = _now()[:10]
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO watchlist
                   (stock_code, stock_name, asset_type, target_capital,
                    weak_support, strong_support, extreme_anchor, notes, added_time)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (item.stock_code, item.stock_name, item.asset_type, item.target_capital,
                 item.weak_support, item.strong_support, item.extreme_anchor, item.notes,
                 item.added_time),
            )
            item.id = cur.lastrowid
        return item

    def update_watchlist_added_time(self, item_id: int, added_time: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE watchlist SET added_time=? WHERE id=?", (added_time, item_id),
            )

    def get_watchlist(self) -> list[WatchlistItem]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM watchlist ORDER BY id").fetchall()
        return [self._wl_from_row(r) for r in rows]

    def get_watchlist_item(self, item_id: int) -> Optional[WatchlistItem]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM watchlist WHERE id=?", (item_id,)).fetchone()
        return self._wl_from_row(row) if row else None

    def update_watchlist_notes(self, item_id: int, notes: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE watchlist SET notes=? WHERE id=?", (notes, item_id),
            )

    def delete_watchlist(self, item_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM watchlist WHERE id=?", (item_id,))

    # ══════════════════════════════════════════════════
    # Simulation（模拟快照）
    # ══════════════════════════════════════════════════

    @staticmethod
    def _sim_from_row(row) -> Simulation:
        return Simulation(
            id=row["id"], stock_code=row["stock_code"], stock_name=row["stock_name"],
            scheme_name=row["scheme_name"], stock_type=row["stock_type"],
            snapshot=json_loads(row["snapshot"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def upsert_simulation(self, sim: Simulation) -> Simulation:
        """按 stock_code 覆盖写（同股票只保留一条模拟）。"""
        now = _now()
        sim.updated_at = now
        if not sim.created_at:
            sim.created_at = now
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO simulations
                   (stock_code, stock_name, scheme_name, stock_type, snapshot,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(stock_code) DO UPDATE SET
                     stock_name=excluded.stock_name,
                     scheme_name=excluded.scheme_name,
                     stock_type=excluded.stock_type,
                     snapshot=excluded.snapshot,
                     updated_at=excluded.updated_at""",
                (sim.stock_code, sim.stock_name, sim.scheme_name, sim.stock_type,
                 json_dumps(sim.snapshot), sim.created_at, now),
            )
            # ON CONFLICT 覆盖写时 lastrowid 不可靠，回查真实 id
            row = conn.execute(
                "SELECT id FROM simulations WHERE stock_code=?", (sim.stock_code,)
            ).fetchone()
            sim.id = row["id"] if row else sim.id
        return sim

    def get_simulations(self) -> list[Simulation]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM simulations ORDER BY updated_at DESC").fetchall()
        return [self._sim_from_row(r) for r in rows]

    def get_simulation_by_code(self, stock_code: str) -> Optional[Simulation]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM simulations WHERE stock_code=?", (stock_code,)
            ).fetchone()
        return self._sim_from_row(row) if row else None

    def delete_simulation(self, sim_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM simulations WHERE id=?", (sim_id,))

    # ══════════════════════════════════════════════════
    # 数据管理（清空/重置）
    # ══════════════════════════════════════════════════

    def reset_data(self, scope: str = "all", keep_cash: bool = False) -> dict:
        """按范围清空数据。

        scope:
            all         → 清空持仓/交易/建议/自选/模拟，可保留现金
            positions   → 清空持仓 + 交易 + 建议（保留自选/模拟）
            watchlist   → 清空自选
            simulations → 清空模拟
        keep_cash: all 时是否保留当前现金（默认归零，便于重新配置本金）。
        """
        deleted = {"positions": 0, "transactions": 0, "advices": 0,
                   "watchlist": 0, "simulations": 0}
        with self._connect() as conn:
            if scope in ("all", "positions"):
                for table, key in (("advices", "advices"), ("transactions", "transactions"),
                                   ("positions", "positions")):
                    deleted[key] = conn.execute(f"DELETE FROM {table}").rowcount
                if scope == "all" and not keep_cash:
                    conn.execute("UPDATE portfolios SET cash_available=0 WHERE id=1")
            if scope in ("all", "watchlist"):
                deleted["watchlist"] = conn.execute("DELETE FROM watchlist").rowcount
            if scope in ("all", "simulations"):
                deleted["simulations"] = conn.execute("DELETE FROM simulations").rowcount
        logger.warning("数据已重置(scope=%s keep_cash=%s): %s", scope, keep_cash, deleted)
        return deleted

    # ══════════════════════════════════════════════════
    # ActionAdvice
    # ══════════════════════════════════════════════════

    def save_advice(self, advice: ActionAdvice) -> ActionAdvice:
        advice.created_at = advice.created_at or _now()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO advices
                   (position_id, stock_code, stock_name, advice_type, urgency,
                    reason, suggested_price, suggested_shares, suggested_amount,
                    check_results, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (advice.position_id, advice.stock_code, advice.stock_name,
                 advice.advice_type, advice.urgency, advice.reason,
                 advice.suggested_price, advice.suggested_shares, advice.suggested_amount,
                 json_dumps(advice.check_results), advice.created_at),
            )
            advice.id = cur.lastrowid
        return advice

    def get_latest_advice(self, position_id: int) -> Optional[ActionAdvice]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM advices WHERE position_id=? ORDER BY id DESC LIMIT 1",
                (position_id,),
            ).fetchone()
        return self._advice_from_row(row) if row else None

    def get_advice_history(self, position_id: int, limit: int = 20) -> list[ActionAdvice]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM advices WHERE position_id=? ORDER BY id DESC LIMIT ?",
                (position_id, limit),
            ).fetchall()
        return [self._advice_from_row(r) for r in rows]

    def list_recent_advices(self, limit: int = 100,
                            position_id: Optional[int] = None) -> list[ActionAdvice]:
        """跨持仓查询最近建议记录（按时间倒序，供操作日志页）

        Args:
            limit: 最多返回条数
            position_id: 可选，只查某个持仓
        """
        sql = "SELECT * FROM advices"
        params: tuple = ()
        if position_id is not None:
            sql += " WHERE position_id=?"
            params = (position_id,)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(sql, params + (limit,)).fetchall()
        return [self._advice_from_row(r) for r in rows]

    @staticmethod
    def _advice_from_row(row: sqlite3.Row) -> ActionAdvice:
        return ActionAdvice(
            id=row["id"], position_id=row["position_id"],
            stock_code=row["stock_code"], stock_name=row["stock_name"],
            advice_type=row["advice_type"], urgency=row["urgency"],
            reason=row["reason"], suggested_price=row["suggested_price"],
            suggested_shares=row["suggested_shares"], suggested_amount=row["suggested_amount"],
            check_results=json_loads(row["check_results"]), created_at=row["created_at"],
        )
