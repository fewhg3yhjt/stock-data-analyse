"""Create a consistent local backup of portfolio, warehouse metadata and config.

The script never deletes source data. The destination must be a new or empty
directory supplied by the operator; off-host copying/encryption remains an
operational responsibility.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


def _backup_sqlite(source: Path, target: Path) -> None:
    source_conn = sqlite3.connect(source)
    target_conn = sqlite3.connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


def create_backup(destination: Path, source_root: Path) -> Path:
    destination = destination.expanduser().resolve()
    source_root = source_root.expanduser().resolve()
    if destination == source_root or source_root in destination.parents:
        raise ValueError("备份目录不能位于项目数据目录内部")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"备份目录必须为空: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    data = source_root / "output" / "data"
    for relative in ("warehouse", "portfolio.db"):
        source = data / relative
        if not source.exists():
            continue
        target = destination / "data" / relative
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("*.tmp"))
        elif source.name.endswith(".db"):
            target.parent.mkdir(parents=True, exist_ok=True)
            _backup_sqlite(source, target)
    for relative in (".env", "schemes/custom", "notifier/notify_rules.yaml"):
        source = source_root / relative
        if not source.exists():
            continue
        target = destination / relative
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("*.tmp"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    (destination / "BACKUP_CREATED_AT").write_text(
        datetime.now().isoformat(timespec="seconds") + "\n", encoding="utf-8"
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="备份 StockInvestmentTool 运行数据")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    path = create_backup(args.destination, args.source_root)
    print(f"backup created: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
