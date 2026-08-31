"""Backup utility tests; all fixtures use temporary directories."""

from __future__ import annotations

import sqlite3

from scripts.backup_data import create_backup


def test_backup_copies_data_and_uses_sqlite_backup(tmp_path):
    root = tmp_path / "project"
    (root / "output/data/warehouse").mkdir(parents=True)
    conn = sqlite3.connect(root / "output/data/portfolio.db")
    conn.execute("CREATE TABLE sample (value TEXT)")
    conn.execute("INSERT INTO sample VALUES ('ok')")
    conn.commit()
    conn.close()
    (root / "output/data/warehouse/marker.txt").write_text("warehouse", encoding="utf-8")

    destination = create_backup(tmp_path / "backup", root)

    assert (destination / "data/portfolio.db").exists()
    assert (destination / "data/warehouse/marker.txt").read_text() == "warehouse"
    conn = sqlite3.connect(destination / "data/portfolio.db")
    assert conn.execute("SELECT value FROM sample").fetchone()[0] == "ok"
    conn.close()


def test_backup_covers_key_databases_and_verifies(tmp_path):
    """备份覆盖管理库/通知库/业务库，并生成 quick_check 验证报告（工作项 7/8）。"""
    root = tmp_path / "project"
    (root / "output/data").mkdir(parents=True)
    for name in ("management.db", "notification_outbox.db", "business.db"):
        conn = sqlite3.connect(root / "output/data" / name)
        conn.execute(f"CREATE TABLE sample_{name.split('.')[0]} (value TEXT)")
        conn.commit()
        conn.close()

    destination = create_backup(tmp_path / "backup", root)

    verify = (destination / "BACKUP_VERIFY.txt").read_text(encoding="utf-8")
    assert "management.db: ok" in verify
    assert "notification_outbox.db: ok" in verify
    assert "business.db: ok" in verify
    for name in ("management.db", "notification_outbox.db", "business.db"):
        conn = sqlite3.connect(destination / "data" / name)
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        conn.close()
