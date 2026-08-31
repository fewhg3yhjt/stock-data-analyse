# -*- coding: utf-8 -*-

import sqlite3

from StockInvestmentTool.biz.db import BusinessDB
from StockInvestmentTool.biz.migration import LegacyMigration, MIGRATION_VERSION


def make_legacy(path):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE portfolios (id INTEGER PRIMARY KEY, name TEXT, cash_available REAL, created_at TEXT);
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY, portfolio_id INTEGER, stock_code TEXT, stock_name TEXT,
            stock_type TEXT, scheme_name TEXT, scheme_snapshot TEXT, total_shares REAL,
            avg_cost REAL, total_cost REAL, current_price REAL, peak_price REAL,
            position_phase TEXT, buy_stage TEXT, left_tier_sold TEXT, stop_loss_price REAL,
            buy_date TEXT, last_operated_date TEXT, status TEXT, notes TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, position_id INTEGER, trans_type TEXT, date TEXT,
            price REAL, shares REAL, amount REAL, fee REAL, pnl REAL, reason TEXT, created_at TEXT
        );
        CREATE TABLE watchlist (
            id INTEGER PRIMARY KEY, stock_code TEXT, stock_name TEXT, asset_type TEXT,
            target_capital REAL, weak_support REAL, strong_support REAL, extreme_anchor REAL,
            notes TEXT, added_time TEXT, source TEXT, sim_entry TEXT
        );
        CREATE TABLE advices (
            id INTEGER PRIMARY KEY, position_id INTEGER, stock_code TEXT, stock_name TEXT,
            advice_type TEXT, urgency TEXT, reason TEXT, suggested_price REAL,
            suggested_shares REAL, suggested_amount REAL, check_results TEXT, created_at TEXT
        );
        CREATE TABLE simulations (
            id INTEGER PRIMARY KEY, stock_code TEXT, stock_name TEXT, scheme_name TEXT,
            stock_type TEXT, snapshot TEXT, created_at TEXT, updated_at TEXT
        );
        INSERT INTO portfolios VALUES (1, '默认组合', 100000, '2026-08-01');
        INSERT INTO positions VALUES (1, 1, 'sh.600908', '测试股', 'B', 'old', '{}', 1000, 10, 10000, 11, 12, 'holding', '', '', 8, '2026-08-01', '2026-08-01', 'active', 'note', 't', 't');
        INSERT INTO transactions VALUES (1, 1, 'buy', '2026-08-01', 10, 1000, 10000, 10, 0, '建仓', 't');
        INSERT INTO watchlist VALUES (1, '600908', '测试股', 'stock', 10000, 9, 10, 8, 'watch', '2026-08-01', 'manual', '{}');
        INSERT INTO advices VALUES (1, 1, 'sh.600908', '测试股', 'sell', 'normal', 'reason', 12, 500, 6000, '{}', '2026-08-02');
        INSERT INTO simulations VALUES (1, 'sh.600908', '测试股', 'old', 'B', '{}', 't', 't');
        """)


def test_evaluate_and_migrate_is_idempotent(tmp_path):
    source = tmp_path / "portfolio.db"
    make_legacy(source)
    target = BusinessDB(tmp_path / "business.db")
    migration = LegacyMigration(source, target)

    report = migration.evaluate()
    assert report.classification_counts["migratable"] == 6
    first = migration.migrate()
    second = migration.migrate()
    assert first.mappings == 6
    assert second.mappings == 0
    assert target.fetchone("SELECT symbol FROM position_cycles")["symbol"] == "sh600908"
    assert target.fetchone("SELECT symbol FROM watch_subscriptions")["symbol"] == "sh600908"
    assert target.fetchone("SELECT COUNT(*) AS n FROM legacy_entity_map")["n"] == 6
    assert target.fetchone(
        "SELECT classification FROM legacy_entity_map WHERE migration_version=? LIMIT 1",
        (MIGRATION_VERSION,),
    )["classification"] in {"migrated", "reconstructed"}


def test_invalid_source_code_is_reported_without_writing_fact(tmp_path):
    source = tmp_path / "portfolio.db"
    make_legacy(source)
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE watchlist SET stock_code='bad-code' WHERE id=1")
    target = BusinessDB(tmp_path / "business.db")
    report = LegacyMigration(source, target).migrate()
    assert report.classification_counts["unknown"] >= 1
    assert target.fetchone("SELECT COUNT(*) AS n FROM watch_subscriptions")["n"] == 0
