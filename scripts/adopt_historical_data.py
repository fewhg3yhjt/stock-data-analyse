#!/usr/bin/env python3
"""Adopt audited legacy candidates into the production data model."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.ops.task_runner import TaskRunner
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.storage import Warehouse


DATASETS = {
    "stock_daily": "stock_daily",
    "valuation_daily": "valuation_daily",
    "fundamentals": "fundamentals",
    "indicators": "indicators",
    "industry": "industry",
    "money_flow_daily": "money_flow_daily",
}


def _target_path(warehouse: Warehouse, dataset: str, partition: str) -> Path:
    if dataset == "stock_daily":
        return warehouse.daily_partition(partition)
    if dataset == "indicators":
        return warehouse.indicator_dir / f"{partition}.parquet"
    if dataset == "fundamentals":
        return warehouse.fundamental_path(partition)
    if dataset == "valuation_daily":
        return warehouse.raw.partition_path("valuation", partition)
    if dataset == "industry":
        return warehouse.base_dir / "raw" / "baostock" / "industry" / f"{partition}.parquet"
    if dataset == "money_flow_daily":
        return warehouse.base_dir / "raw" / "ths" / "money_flow_daily" / f"{partition}.parquet"
    raise ValueError(f"未知数据集: {dataset}")


def _source_batch(center: TaskCenter, dataset: str, files: list[Path], run_id: int) -> str:
    from StockInvestmentTool.warehouse.source_batches import SourceBatchStore
    dates = []
    for path in files[:20]:
        try:
            frame = pd.read_parquet(path, columns=["date"] if dataset != "fundamentals" else ["stat_date"])
            column = "date" if "date" in frame else "stat_date"
            dates.extend([str(pd.to_datetime(frame[column]).min())[:10], str(pd.to_datetime(frame[column]).max())[:10]])
        except Exception:
            pass
    start = min(dates) if dates else datetime.now().strftime("%Y-%m-%d")
    end = max(dates) if dates else start
    store = SourceBatchStore(center.db_path)
    batch_id = store.start(dataset_name=dataset, source_name="legacy", run_date=datetime.now().strftime("%Y-%m-%d"),
                           trade_date_start=start, trade_date_end=end, expected_symbols=len(files),
                           universe_id=f"legacy_{dataset}", request_context={"migration": True}, job_run_id=run_id)
    digest = hashlib.sha256("".join(str(path) + str(path.stat().st_size) for path in files).encode()).hexdigest()
    store.finish(batch_id, success_symbols=len(files), failed_symbols=0, skipped_symbols=0,
                 row_count=0, raw_path=None, checksum=digest, file_size=0, status="success")
    return batch_id


def adopt(root: Path, management_db: Path, datasets: list[str]) -> dict:
    root = Path(root).resolve()
    center = TaskCenter(management_db, management_db)
    center.sync_definitions()
    center.sync_metrics()
    runner = TaskRunner(management_db, management_db)
    request_id = center.create_request("stock_daily_build", "backfill", requested_by="historical_adoption")
    request = center.request(request_id)
    run_id = runner.jobs.start("stock_daily_build", display_name="历史数据接入",
                               request_id=request_id, config_version=request["config_version"],
                               trigger_type="backfill", input_dataset="legacy", output_dataset="datasets")
    center.update_request(request_id, "running")
    result = {"request_id": request_id, "run_id": run_id, "datasets": {}, "status": "success"}
    state = PipelineState(management_db)
    warehouse = Warehouse()
    warehouse.meta_db_path = management_db
    try:
        for dataset in datasets:
            files = sorted((root / dataset).rglob("*.parquet"))
            if not files:
                result["datasets"][dataset] = {"status": "skipped", "files": 0}
                continue
            batch_id = _source_batch(center, dataset, files, run_id)
            versions = {}
            for path in files:
                partition = path.parent.name
                if dataset in {"industry", "money_flow_daily"}:
                    partition = path.stem
                if dataset == "fundamentals":
                    partition = path.stem
                frame = pd.read_parquet(path, columns=["date", "code"] if dataset not in {"fundamentals", "money_flow_daily", "industry"} else (["stat_date", "code"] if dataset == "fundamentals" else ["period", "code"] if dataset == "money_flow_daily" else ["code"]))
                date_column = "date" if "date" in frame else "stat_date" if "stat_date" in frame else None
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                version_id = f"{dataset}_{partition.replace('-', '').replace('/', '_')}_legacy_{checksum[:12]}"
                with sqlite3.connect(management_db) as conn:
                    conn.execute("""INSERT OR IGNORE INTO dataset_versions
                        (version_id,dataset_name,partition_key,candidate_path,published_path,source_batches,
                         row_count,symbol_count,min_date,max_date,schema_version,checksum,builder_version,
                         quality_status,publish_status,created_at,published_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (version_id, dataset, partition, str(path), str(path), json.dumps([batch_id]), len(frame),
                         int(frame["code"].nunique()), str(pd.to_datetime(frame[date_column]).min())[:10] if date_column in frame else None,
                         str(pd.to_datetime(frame[date_column]).max())[:10] if date_column in frame else None, f"{dataset}.v1", checksum,
                         "legacy_adoption.v1", "PASS", "candidate", datetime.now().isoformat(timespec="seconds"),
                         None))
                    conn.execute("""INSERT OR IGNORE INTO dataset_quality_results
                        (quality_id,dataset_name,partition_key,version_id,status,checks,affected_symbols,publish_allowed,checked_at,checker_version)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""", (f"quality_{version_id}", dataset, partition, version_id,
                        "PASS", json.dumps({"legacy_adoption": True}), "[]", 1,
                        datetime.now().isoformat(timespec="seconds"), "legacy_adoption.v1"))
                PipelineState(management_db).quality(version_id, status="PASS",
                                                     checks={"legacy_adoption": True}, publish_allowed=True)
                target = _target_path(warehouse, dataset, partition)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target != path:
                    import shutil
                    temp = target.with_name(f".{target.name}.{version_id}.tmp")
                    shutil.copyfile(path, temp)
                    import os
                    os.replace(temp, target)
                    with sqlite3.connect(management_db) as conn:
                        conn.execute("UPDATE dataset_versions SET published_path=? WHERE version_id=?", (str(target), version_id))
                with sqlite3.connect(management_db) as conn:
                    conn.execute("UPDATE dataset_versions SET publish_status='published' WHERE version_id=?", (version_id,))
                    conn.execute("INSERT OR REPLACE INTO dataset_current(dataset_name,partition_key,version_id,published_at) VALUES(?,?,?,?)",
                                 (dataset, partition, version_id, datetime.now().isoformat(timespec="seconds")))
                versions[partition] = version_id
                artifact = center.register_artifact(run_id=run_id, dataset_name=dataset,
                                                    artifact_type="published_dataset", partition_key=partition,
                                                    file_path=target)
                raw_artifact = center.register_artifact(run_id=run_id, dataset_name=dataset,
                                                        artifact_type="raw_batch", partition_key=None,
                                                        file_path=path)
                center.link_lineage(raw_artifact, artifact, "legacy_input")
            result["datasets"][dataset] = {"status": "success", "files": len(files), "versions": len(versions), "batch_id": batch_id}
        center.update_request(request_id, "success")
        runner.jobs.finish(run_id, "success", result)
        return result
    except Exception as exc:
        result.update({"status": "failed", "error": str(exc)})
        center.update_request(request_id, "failed")
        runner.jobs.finish(run_id, "failed", result, error=str(exc))
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="接管历史迁移候选到生产数据模型")
    parser.add_argument("--root", type=Path, default=Path("output/data/migration_candidates"))
    parser.add_argument("--management-db", type=Path, default=Path("output/data/management.db"))
    parser.add_argument("--datasets", default=",".join(DATASETS))
    args = parser.parse_args(argv)
    print(json.dumps(adopt(args.root, args.management_db, [x.strip() for x in args.datasets.split(",") if x.strip()]), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
