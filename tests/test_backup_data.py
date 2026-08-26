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
