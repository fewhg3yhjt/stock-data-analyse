# -*- coding: utf-8 -*-
"""旧 portfolio.db 的一次性只读迁移评估与导入（migration-only）。

该模块不是运行时兼容层。源库只读，目标库必须由调用方显式传入，默认不
允许指向业务默认库，避免误改生产业务数据。迁移完成后旧模型不再被业务
服务读取。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from StockInvestmentTool.biz.code import InvalidCodeError, normalize
from StockInvestmentTool.biz.db import BusinessDB, dumps_json, now_utc
from StockInvestmentTool.biz.models import new_id

MIGRATION_VERSION = "portfolio.v1"


@dataclass
class MigrationReport:
    source: str
    target: str
    classification_counts: dict[str, int] = field(default_factory=dict)
    mappings: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, classification: str, message: str = "") -> None:
        self.classification_counts[classification] = self.classification_counts.get(classification, 0) + 1
        if message:
            self.warnings.append(message)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "migration_version": MIGRATION_VERSION,
            "classification_counts": self.classification_counts,
            "mappings": self.mappings,
            "warnings": self.warnings,
            "errors": self.errors,
        }


class LegacyMigration:
    """portfolio.db → business.db 的评估/迁移器。"""

    def __init__(self, source_path: str | Path, target_db: BusinessDB):
        self.source_path = Path(source_path)
        self.target = target_db
        if not self.source_path.exists():
            raise FileNotFoundError(self.source_path)
        if self.source_path.resolve() == self.target.db_path.resolve():
            raise ValueError("源库和目标库不能相同")

    def evaluate(self) -> MigrationReport:
        report = MigrationReport(str(self.source_path), str(self.target.db_path))
        with sqlite3.connect(f"file:{self.source_path}?mode=ro", uri=True) as source:
            source.row_factory = sqlite3.Row
            self._evaluate_table(source, "portfolios", report, "portfolio")
            self._evaluate_table(source, "positions", report, "position")
            self._evaluate_table(source, "transactions", report, "transaction")
            self._evaluate_table(source, "watchlist", report, "watchlist")
            self._evaluate_table(source, "advices", report, "advice")
            self._evaluate_table(source, "simulations", report, "simulation")
        return report

    def migrate(self) -> MigrationReport:
        report = self.evaluate()
        # 迁移写入目标事务；旧库连接保持只读。
        with sqlite3.connect(f"file:{self.source_path}?mode=ro", uri=True) as source:
            source.row_factory = sqlite3.Row
            with self.target.transaction() as target:
                portfolio_map = self._migrate_portfolios(source, target, report)
                self._migrate_initial_cash(source, target, portfolio_map, report)
                position_map = self._migrate_positions(source, target, portfolio_map, report)
                self._migrate_transactions(source, target, position_map, report)
                self._repair_sell_allocations(source, target, position_map, report)
                self._migrate_watchlist(source, target, report)
                self._migrate_advices(source, target, position_map, report)
                self._migrate_simulations(source, target, report)
        return report

    def repair_sell_allocations(self) -> MigrationReport:
        """为已迁移但尚无 Lot 分配的历史卖出补建可重算明细。"""
        report = MigrationReport(str(self.source_path), str(self.target.db_path))
        with sqlite3.connect(f"file:{self.source_path}?mode=ro", uri=True) as source:
            source.row_factory = sqlite3.Row
            with self.target.transaction() as target:
                position_map = {
                    str(row["legacy_id"]): row["new_id"]
                    for row in target.execute(
                        "SELECT legacy_id,new_id FROM legacy_entity_map "
                        "WHERE legacy_db=? AND legacy_type='position' AND migration_version=?",
                        (str(self.source_path), MIGRATION_VERSION),
                    ).fetchall()
                }
                self._repair_sell_allocations(source, target, position_map, report)
        return report

    def _repair_sell_allocations(self, source, target, position_map, report):
        rows = source.execute(
            "SELECT * FROM transactions ORDER BY date, id"
        ).fetchall()
        open_lots: dict[str, list[dict]] = {}
        for row in rows:
            cycle_id = position_map.get(str(row["position_id"]))
            if not cycle_id:
                continue
            trans_type = str(row["trans_type"] or "").lower()
            if trans_type in {"buy", "买入"}:
                execution = target.execute(
                    "SELECT execution_id FROM executions WHERE idempotency_key=?",
                    (f"legacy-transaction-{row['id']}",),
                ).fetchone()
                if not execution:
                    continue
                lot = target.execute(
                    "SELECT * FROM position_lots WHERE source_execution_id=?",
                    (execution[0],),
                ).fetchone()
                if lot:
                    open_lots.setdefault(cycle_id, []).append({
                        "lot_id": lot["lot_id"],
                        "quantity": float(row["shares"] or 0),
                        "price": float(row["price"] or 0),
                        "entry_fee": float(row["fee"] or 0),
                    })
            elif trans_type in {"sell", "卖出"}:
                execution = target.execute(
                    "SELECT execution_id,fee,tax,price,gross_amount FROM executions "
                    "WHERE idempotency_key=?",
                    (f"legacy-transaction-{row['id']}",),
                ).fetchone()
                if not execution or target.execute(
                    "SELECT 1 FROM execution_lot_allocations WHERE execution_id=? LIMIT 1",
                    (execution[0],),
                ).fetchone():
                    continue
                remaining = float(row["shares"] or 0)
                allocations = []
                for lot in open_lots.get(cycle_id, []):
                    if remaining <= 0:
                        break
                    consume = min(remaining, lot["quantity"])
                    gross = consume * float(execution["price"] or row["price"] or 0)
                    allocations.append({
                        "lot_id": lot["lot_id"], "quantity": consume, "gross": gross,
                        "cost": consume * lot["price"],
                        "entry_fee": lot["entry_fee"] * consume / lot["quantity"] if lot["quantity"] else 0,
                    })
                    lot["quantity"] -= consume
                    remaining -= consume
                total_gross = sum(item["gross"] for item in allocations)
                for item in allocations:
                    share = item["gross"] / total_gross if total_gross else 0
                    fee = float(execution["fee"] or 0) * share
                    tax = float(execution["tax"] or 0) * share
                    target.execute(
                        """INSERT OR IGNORE INTO execution_lot_allocations
                           (allocation_id,execution_id,lot_id,quantity,cost_amount,fee_allocated,tax_allocated,realized_pnl,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (new_id("alloc"), execution[0], item["lot_id"], item["quantity"],
                         item["cost"], fee + item["entry_fee"], tax,
                         item["gross"] - item["cost"] - item["entry_fee"] - fee - tax, now_utc()),
                    )
                    report.mappings += 1

    def _evaluate_table(self, source, table: str, report: MigrationReport, kind: str) -> None:
        try:
            rows = source.execute(f"SELECT id FROM {table}").fetchall()
        except sqlite3.OperationalError:
            report.add("unknown", f"旧库缺少表: {table}")
            return
        for row in rows:
            report.add("migratable", f"{kind}:{row['id']}")

    def _already_mapped(self, conn, legacy_type: str, legacy_id: str) -> str | None:
        row = conn.execute(
            "SELECT new_id FROM legacy_entity_map WHERE legacy_db=? AND legacy_type=? "
            "AND legacy_id=? AND migration_version=?",
            (str(self.source_path), legacy_type, str(legacy_id), MIGRATION_VERSION),
        ).fetchone()
        # 空 new_id 仍代表该旧记录已经处理过（例如仅有快照的旧模拟）。
        return (row[0] or "__mapped__") if row else None

    def _map(self, conn, legacy_type: str, legacy_id: str, new_type: str, new_id_value: str,
             classification: str = "migrated", note: str = "") -> None:
        conn.execute(
            """INSERT OR IGNORE INTO legacy_entity_map
               (map_id,legacy_db,legacy_type,legacy_id,new_type,new_id,migration_version,classification,note,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (new_id("map"), str(self.source_path), legacy_type, str(legacy_id), new_type,
             new_id_value, MIGRATION_VERSION, classification, note, now_utc()),
        )

    def _migrate_portfolios(self, source, target, report):
        result = {}
        rows = source.execute("SELECT * FROM portfolios ORDER BY id").fetchall()
        for row in rows:
            old_id = str(row["id"])
            existing = self._already_mapped(target, "portfolio", old_id)
            if existing:
                result[old_id] = existing
                continue
            account_id = self._account_for_portfolio(target, old_id)
            portfolio_id = new_id("pf")
            ts = now_utc()
            target.execute(
                """INSERT INTO portfolios
                   (portfolio_id,account_id,name,benchmark_symbol,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (portfolio_id, account_id, row["name"] or f"旧组合-{old_id}", "", "active", ts, ts),
            )
            self._map(target, "portfolio", old_id, "Portfolio", portfolio_id)
            result[old_id] = portfolio_id
            report.mappings += 1
        return result

    def _migrate_initial_cash(self, source, target, portfolio_map, report):
        """根据旧组合余额反推迁移时的初始现金，并建立 INITIAL 流水。"""
        portfolios = source.execute("SELECT * FROM portfolios ORDER BY id").fetchall()
        for portfolio in portfolios:
            new_portfolio_id = portfolio_map.get(str(portfolio["id"]))
            if not new_portfolio_id:
                continue
            transaction_rows = source.execute(
                """SELECT t.trans_type, t.amount, t.fee
                   FROM transactions t JOIN positions p ON p.id=t.position_id
                   WHERE p.portfolio_id=? ORDER BY t.date, t.id""",
                (portfolio["id"],),
            ).fetchall()
            current_cash = float(portfolio["cash_available"] or 0.0)
            net_trade_cash = 0.0
            for row in transaction_rows:
                trans_type = str(row["trans_type"] or "").lower()
                amount = float(row["amount"] or 0.0)
                fee = float(row["fee"] or 0.0)
                if trans_type in {"buy", "买入"}:
                    net_trade_cash -= amount + fee
                elif trans_type in {"sell", "卖出"}:
                    net_trade_cash += amount - fee
            initial_cash = current_cash - net_trade_cash
            exists = target.execute(
                "SELECT 1 FROM cash_ledger_entries WHERE portfolio_id=? AND entry_type='INITIAL'",
                (new_portfolio_id,),
            ).fetchone()
            if not exists:
                ts = now_utc()
                target.execute(
                    """INSERT INTO cash_ledger_entries
                       (cash_entry_id,portfolio_id,entry_type,amount,balance_after,execution_id,entry_time,reason,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (new_id("cash"), new_portfolio_id, "INITIAL", initial_cash, initial_cash,
                     "", portfolio["created_at"] or ts, "历史余额反推初始现金（reconstructed）", ts),
                )

    def _account_for_portfolio(self, target, legacy_id: str) -> str:
        row = target.execute(
            "SELECT account_id FROM accounts WHERE account_id=?", (f"legacy_acc_{legacy_id}",)
        ).fetchone()
        if row:
            return row[0]
        ts = now_utc()
        account_id = f"legacy_acc_{legacy_id}"
        target.execute(
            """INSERT INTO accounts(account_id,name,currency,account_type,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (account_id, f"迁移账户-{legacy_id}", "CNY", "real", "active", ts, ts),
        )
        return account_id

    def _migrate_positions(self, source, target, portfolio_map, report):
        result = {}
        rows = source.execute("SELECT * FROM positions ORDER BY id").fetchall()
        for row in rows:
            old_id = str(row["id"])
            existing = self._already_mapped(target, "position", old_id)
            if existing:
                result[old_id] = existing
                continue
            try:
                symbol = normalize(row["stock_code"])
            except (InvalidCodeError, TypeError) as exc:
                report.add("unknown", f"position:{old_id} code 无法归一: {exc}")
                continue
            cycle_id = new_id("pc")
            portfolio_id = portfolio_map.get(str(row["portfolio_id"]))
            if not portfolio_id:
                report.add("unknown", f"position:{old_id} 找不到旧 portfolio")
                continue
            status = "closed" if str(row["status"]).lower() in {"closed", "已清仓", "inactive"} else "open"
            phase = "closed" if status == "closed" else "holding"
            ts = now_utc()
            target.execute(
                """INSERT INTO position_cycles
                   (position_cycle_id,portfolio_id,symbol,status,phase,entry_plan_snapshot_json,
                    scheme_snapshot_json,opened_at,closed_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (cycle_id, portfolio_id, symbol, status, phase, "{}",
                 dumps_json({"legacy": True, "scheme_name": row["scheme_name"],
                             "classification": "reconstructed"}),
                 row["buy_date"] or ts[:10], None if status == "open" else row["last_operated_date"],
                 ts, ts),
            )
            self._map(target, "position", old_id, "PositionCycle", cycle_id,
                      "reconstructed", "策略版本和部分历史阶段无法从旧记录确定")
            result[old_id] = cycle_id
            report.mappings += 1
        return result

    def _migrate_transactions(self, source, target, position_map, report):
        rows = source.execute("SELECT * FROM transactions ORDER BY id").fetchall()
        for row in rows:
            old_id = str(row["id"])
            if self._already_mapped(target, "transaction", old_id):
                continue
            cycle_id = position_map.get(str(row["position_id"]))
            if not cycle_id:
                report.add("unknown", f"transaction:{old_id} 找不到 PositionCycle")
                continue
            trans_type = str(row["trans_type"]).lower()
            event_type = "BUY" if trans_type in {"buy", "买入"} else "SELL" if trans_type in {"sell", "卖出"} else "CORRECTION"
            cycle = target.execute(
                "SELECT portfolio_id,symbol FROM position_cycles WHERE position_cycle_id=?", (cycle_id,)
            ).fetchone()
            execution_id = new_id("exe")
            ts = row["date"] or now_utc()
            target.execute(
                """INSERT INTO executions
                   (execution_id,portfolio_id,position_cycle_id,symbol,event_type,trade_time,quantity,price,
                    gross_amount,fee,tax,net_amount,reason,advice_id,decision_id,simulation_run_id,external_ref,idempotency_key,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (execution_id, cycle["portfolio_id"], cycle_id, normalize(cycle["symbol"]), event_type, ts,
                 row["shares"], row["price"], row["amount"], row["fee"] or 0, 0,
                 row["amount"] or 0, row["reason"] or "历史迁移", "", "", "",
                 f"legacy-transaction-{old_id}", f"legacy-transaction-{old_id}", now_utc()),
            )
            if event_type == "BUY":
                target.execute(
                    """INSERT INTO position_lots
                       (lot_id,position_cycle_id,symbol,opened_at,quantity,remaining_quantity,
                        entry_price,entry_fee,source_execution_id,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (new_id("lot"), cycle_id, normalize(cycle["symbol"]), ts,
                     row["shares"] or 0, row["shares"] or 0, row["price"] or 0,
                     (row["fee"] or 0), execution_id, now_utc()),
                )
            elif event_type == "SELL":
                remaining = float(row["shares"] or 0)
                lots = target.execute(
                    "SELECT lot_id,remaining_quantity FROM position_lots WHERE position_cycle_id=? "
                    "AND remaining_quantity>0 ORDER BY opened_at,lot_id",
                    (cycle_id,),
                ).fetchall()
                for lot in lots:
                    if remaining <= 0:
                        break
                    consume = min(remaining, float(lot["remaining_quantity"]))
                    target.execute(
                        "UPDATE position_lots SET remaining_quantity=? WHERE lot_id=?",
                        (float(lot["remaining_quantity"]) - consume, lot["lot_id"]),
                    )
                    remaining -= consume
            # 交易现金流水按旧交易金额重建；不能把旧余额字段当作新事实。
            cash_row = target.execute(
                "SELECT balance_after FROM cash_ledger_entries WHERE portfolio_id=? ORDER BY rowid DESC LIMIT 1",
                (cycle["portfolio_id"],),
            ).fetchone()
            previous_cash = float(cash_row["balance_after"]) if cash_row else 0.0
            cash_delta = -(float(row["amount"] or 0) + float(row["fee"] or 0)) if event_type == "BUY" else \
                float(row["amount"] or 0) - float(row["fee"] or 0) if event_type == "SELL" else 0.0
            target.execute(
                """INSERT INTO cash_ledger_entries
                   (cash_entry_id,portfolio_id,entry_type,amount,balance_after,execution_id,entry_time,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (new_id("cash"), cycle["portfolio_id"], event_type, cash_delta,
                 previous_cash + cash_delta, execution_id, ts, "历史交易迁移", now_utc()),
            )
            self._map(target, "transaction", old_id, "Execution", execution_id,
                      "reconstructed", "旧交易字段已映射，原始来源保留在映射表")
            report.mappings += 1

    def _migrate_watchlist(self, source, target, report):
        rows = source.execute("SELECT * FROM watchlist ORDER BY id").fetchall()
        for row in rows:
            old_id = str(row["id"])
            if self._already_mapped(target, "watchlist", old_id):
                continue
            try:
                symbol = normalize(row["stock_code"])
            except (InvalidCodeError, TypeError) as exc:
                report.add("unknown", f"watchlist:{old_id} code 无法归一: {exc}")
                continue
            subscription_id = new_id("sub")
            ts = now_utc()
            target.execute(
                """INSERT INTO watch_subscriptions
                   (subscription_id,symbol,name,asset_type,purpose,target_amount,notes,status,started_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (subscription_id, symbol, row["stock_name"] or "", row["asset_type"] or "stock",
                 "research", row["target_capital"], row["notes"] or "", "active",
                 row["added_time"] or ts, ts, ts),
            )
            self._map(target, "watchlist", old_id, "WatchSubscription", subscription_id,
                      "reconstructed", "旧 source/sim_entry 未直接伪装成新观察事实")
            report.mappings += 1

    def _migrate_advices(self, source, target, position_map, report):
        rows = source.execute("SELECT * FROM advices ORDER BY id").fetchall()
        for row in rows:
            old_id = str(row["id"])
            if self._already_mapped(target, "advice", old_id):
                continue
            try:
                symbol = normalize(row["stock_code"])
            except (InvalidCodeError, TypeError):
                report.add("unknown", f"advice:{old_id} code 无法归一")
                continue
            advice_id = new_id("adv")
            ts = row["created_at"] or now_utc()
            target.execute(
                """INSERT INTO advices
                   (advice_id,position_cycle_id,symbol,action,price,reason,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (advice_id, position_map.get(str(row["position_id"])), symbol,
                 str(row["advice_type"] or "NO_ACTION").upper(), row["suggested_price"],
                 row["reason"] or "历史迁移", "generated", ts, ts),
            )
            self._map(target, "advice", old_id, "Advice", advice_id,
                      "reconstructed", "旧建议与新 StrategyDecision 的完整关系无法确定")
            report.mappings += 1

    def _migrate_simulations(self, source, target, report):
        rows = source.execute("SELECT * FROM simulations ORDER BY id").fetchall()
        for row in rows:
            old_id = str(row["id"])
            if self._already_mapped(target, "simulation", old_id):
                continue
            self._map(target, "simulation", old_id, "SimulationRun", "",
                      "unknown", "旧 simulations 仅有快照，无法重建为新的逐笔 SimulationFill")
            report.mappings += 1


def migration_report_json(report: MigrationReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
