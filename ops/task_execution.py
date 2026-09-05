"""Unified execution workers for configured data tasks."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from StockInvestmentTool.fundflow.capture import capture_money_flow
from StockInvestmentTool.ops.task_center import TaskCenter
from StockInvestmentTool.warehouse.backfill import ValuationBackfill
from StockInvestmentTool.warehouse.collector import MarketCollector
from StockInvestmentTool.warehouse.daily_build import DailyBuilder
from StockInvestmentTool.warehouse.fundamentals_collect import FundamentalsCollector
from StockInvestmentTool.warehouse.indicators_build import IndicatorsBuilder
from StockInvestmentTool.warehouse.pipeline_state import PipelineState
from StockInvestmentTool.warehouse.publish import Publisher
from StockInvestmentTool.warehouse.quality import check_stock_daily
from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.storage import Warehouse

logger = logging.getLogger(__name__)


def _new_source_task(warehouse, request, task_key, run_id):
    from StockInvestmentTool.warehouse import financial_reports, valuation_snapshot
    if task_key == "financial_reports_capture":
        return financial_reports.collect(warehouse, symbols=_symbols(request), start_date=request.get("period_start"),
                                         end_date=request.get("period_end"), timeout=float(request.get("request_timeout", 15)),
                                         deadline=float(request["task_timeout"]) if request.get("task_timeout") else None,
                                           query_interval=request.get("query_interval"),
                                           checkpoint_batch_id=request.get("checkpoint_batch_id") or request.get("source_batch_id"),
                                           batch_size=int(request.get("financial_batch_size", 100)),
                                           failure_threshold=int(request.get("failure_threshold", 20)),
                                          job_run_id=run_id)
    return valuation_snapshot.collect(warehouse, symbols=_symbols(request), start_date=request.get("period_start"),
                                      end_date=request.get("period_end"), timeout=float(request.get("request_timeout", 15)),
                                      deadline=float(request["task_timeout"]) if request.get("task_timeout") else None,
                                      query_interval=request.get("query_interval"),
                                      job_run_id=run_id)


def _source_build(warehouse, request, dataset_name):
    if not request.get("input_batch_id"):
        raise ValueError(f"{dataset_name}_build requires explicit input_batch_id")
    path = _batch(warehouse, request["input_batch_id"])
    default_partition = request.get("period_end") or request.get("period_start")
    frame = pd.read_parquet(path)
    if dataset_name == "financial_reports":
        from StockInvestmentTool.warehouse.financial_reports import normalize_reports
        frame = normalize_reports(frame)
        frame["report_date"] = pd.to_datetime(frame["report_date"]).dt.strftime("%Y-%m-%d")
        partitions = {str(value)[:7] for value in frame["report_date"].dropna()}
    else:
        from StockInvestmentTool.warehouse.valuation_snapshot import normalize_quotes
        frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.strftime("%Y-%m-%d")
        partitions = {str(value)[:10] for value in frame["trade_date"].dropna()}
    if not partitions:
        partitions = {default_partition if dataset_name == "valuation_snapshot" else default_partition[:7]}
    import hashlib
    keys = {"financial_reports": ["report_date", "code", "statement_type"], "valuation_snapshot": ["trade_date", "code"]}[dataset_name]
    versions = {}
    for partition in sorted(partitions):
        partition_value = partition if dataset_name == "valuation_snapshot" else partition[:7]
        date_prefix_length = 10 if dataset_name == "valuation_snapshot" else 7
        part_frame = frame[frame[keys[0]].astype(str).str[:date_prefix_length].eq(partition_value)].drop_duplicates(keys).sort_values(keys).reset_index(drop=True)
        candidate = warehouse.base_dir / "candidates" / dataset_name / partition
        candidate.mkdir(parents=True, exist_ok=True)
        fingerprint = hashlib.sha256(part_frame.to_json(orient="records", date_format="iso").encode()).hexdigest()
        output = candidate / f"{dataset_name}_{partition}_{fingerprint[:12]}.parquet"
        if not output.exists(): part_frame.to_parquet(output, index=False)
        build = {"version_id": f"{dataset_name}_{partition.replace('-', '')}_{fingerprint[:12]}", "dataset_name": dataset_name,
                 "partition": partition, "path": output, "row_count": len(part_frame),
                 "symbol_count": part_frame["code"].nunique(), "checksum": hashlib.sha256(output.read_bytes()).hexdigest()}
        versions[partition] = PipelineState(warehouse.meta_db_path).create_version(
            build, source_batches=[request["input_batch_id"]], dataset_name=dataset_name,
            schema_version=f"{dataset_name}.v1", builder_version=f"{dataset_name}_builder.v1")
    return {"rows": len(frame), "output_versions": versions, "versions": versions}


def _new_quality(warehouse, request, dataset_name):
    versions = request.get("input_versions") or {}
    if not versions:
        raise ValueError(f"{dataset_name}_quality requires explicit input_versions")
    expected_symbols = request.get("expected_symbols")
    if expected_symbols is None:
        expected_symbols = len(_symbols(request)) or None
    reports = {}
    for partition, version in versions.items():
        with sqlite3.connect(warehouse.meta_db_path) as conn:
            row = conn.execute("SELECT candidate_path FROM dataset_versions WHERE version_id=? AND dataset_name=?", (version, dataset_name)).fetchone()
        if not row:
            raise ValueError(f"版本不存在: {dataset_name}/{version}")
        frame = pd.read_parquet(row[0])
        keys = {"financial_reports": ["report_date", "code", "statement_type"], "valuation_snapshot": ["trade_date", "code"]}[dataset_name]
        if dataset_name == "valuation_snapshot":
            checks = {
                "empty": not frame.empty,
                "required_columns": all(c in frame for c in keys),
                "duplicate_keys": not frame.duplicated(keys).any() if all(c in frame for c in keys) else False,
                "valid_dates": bool(pd.to_datetime(frame.get("trade_date"), errors="coerce").notna().all()) if "trade_date" in frame else False,
                "valid_prices": bool((pd.to_numeric(frame.get("price"), errors="coerce") > 0).all()) if "price" in frame and not frame.empty else False,
                "core_non_null_rate": float(frame[["trade_date", "code"]].notna().mean().min()) if all(c in frame for c in keys) and not frame.empty else 0.0,
            }
        else:
            if dataset_name == "financial_reports":
                from StockInvestmentTool.warehouse.financial_reports import financial_reports_quality
                report = financial_reports_quality(frame, expected_symbols=expected_symbols)
                PipelineState(warehouse.meta_db_path).quality(version, status=report["status"],
                                                               checks=report["checks"], publish_allowed=report["publish_allowed"])
                reports[partition] = report
                continue
            checks = {"empty": not frame.empty, "required_columns": all(c in frame for c in keys),
                      "duplicate_keys": not frame.duplicated(keys).any() if all(c in frame for c in keys) else False,
                      "valid_dates": bool(pd.to_datetime(frame.get("report_date"), errors="coerce").notna().all()) if "report_date" in frame else False,
                      "core_non_null_rate": float(frame[["report_date", "code", "statement_type"]].notna().mean().min()) if all(c in frame for c in keys) and not frame.empty else 0.0,
                      "financial_value_present": bool(frame[["revenue", "net_profit_parent", "parent_equity"]].notna().any(axis=1).all()) if all(c in frame for c in ("revenue", "net_profit_parent", "parent_equity")) and not frame.empty else True}
        status = "PASS" if all(checks.values()) else "FAIL"
        PipelineState(warehouse.meta_db_path).quality(version, status=status, checks=checks, publish_allowed=status == "PASS")
        reports[partition] = {"status": status, "checks": checks, "publish_allowed": status == "PASS"}
    return {"status": "PASS" if all(x["status"] == "PASS" for x in reports.values()) else "FAIL", "reports": reports,
            "publish_allowed": all(x["publish_allowed"] for x in reports.values()), "input_versions": versions,
            "output_versions": versions}


def _months(start: str | None, end: str | None) -> list[str]:
    end = end or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    start = start or (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
    return [value.strftime("%Y-%m") for value in pd.date_range(
        pd.Timestamp(start).replace(day=1), pd.Timestamp(end).replace(day=1), freq="MS")]


def _symbols(request: dict) -> list[str]:
    return [str(value).lower().replace(".", "") for value in request.get("symbols", [])]


def _batch(warehouse: Warehouse, batch_id: str) -> Path:
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        row = conn.execute("SELECT raw_path FROM source_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"Raw Batch 不存在: {batch_id}")
    return Path(row[0])


def _capture(warehouse: Warehouse, request: dict, run_id: int) -> dict:
    symbols = _symbols(request)
    task_timeout = request.get("task_timeout")
    result = MarketCollector(warehouse=warehouse, query_interval=0.3).sync_daily(
        start_date=request.get("period_start"), end_date=request.get("period_end"), symbols=symbols,
        include_etf=True, source="tencent", target="raw:tencent", capture_raw=True,
         flush_every=10, job_run_id=run_id, asset_types=["stock", "etf"],
         timeout=float(task_timeout) if task_timeout is not None else None)
    if not result.get("source_batch_id") or result.get("raw_capture_failed"):
        raise RuntimeError("Raw Batch 未成功落盘")
    return result


def _build(warehouse: Warehouse, request: dict) -> dict:
    versions = {}
    rows = 0
    batch_ids = []
    for partition in _months(request.get("period_start"), request.get("period_end")):
        if request.get("input_batch_id"):
            path = _batch(warehouse, request["input_batch_id"])
            selected = [("tencent", path, request["input_batch_id"])]
        else:
            # 自动合并所有重叠该月的 Raw Batch（含补漏 batch），避免只取最新
            # 单个 batch 导致 coverage 骤降。
            selected = DailyBuilder(warehouse).select_raw_batches(partition)
        build = DailyBuilder(warehouse).build_partition(partition, selected, include_current=True)
        ids = [item[2] for item in selected]
        version = PipelineState(warehouse.meta_db_path).create_version(build, source_batches=ids)
        versions[partition] = {"version": version, "build": build}
        rows += build["row_count"]
        batch_ids += ids
    batch_id = request.get("input_batch_id") or (batch_ids[0] if batch_ids else None)
    return {"rows": rows, "months": len(versions),
            "versions": versions,
            "output_versions": {partition: item["version"] for partition, item in versions.items()},
            "source_batch_id": batch_id, "source_batch_ids": batch_ids}


def _quality(warehouse: Warehouse, request: dict) -> dict:
    versions = request.get("input_versions") or {}
    versions = {key: value.get("version") if isinstance(value, dict) else value
                for key, value in versions.items()}
    if not versions:
        with sqlite3.connect(warehouse.meta_db_path) as conn:
            rows = conn.execute(
                "SELECT partition_key,version_id FROM dataset_versions v "
                "WHERE dataset_name='stock_daily' AND publish_status='candidate' "
                "AND created_at=(SELECT MAX(v2.created_at) FROM dataset_versions v2 "
                "WHERE v2.dataset_name=v.dataset_name AND v2.partition_key=v.partition_key "
                "AND v2.publish_status='candidate')"
            ).fetchall()
        versions = {partition: version for partition, version in rows}
    # expected_symbols 基准：优先使用执行请求固化 symbols/symbol_count，
    # 禁止使用候选自身 symbol_count（避免覆盖率自证恒为 1.0）。
    expected_symbols = request.get("expected_symbols")
    if expected_symbols is None:
        symbols = _symbols(request)
        expected_symbols = len(symbols) if symbols else None
    reports = {}
    allowed = True
    statuses = []
    for partition, version in versions.items():
        with sqlite3.connect(warehouse.meta_db_path) as conn:
            row = conn.execute("SELECT candidate_path,symbol_count FROM dataset_versions WHERE version_id=?", (version,)).fetchone()
        if not row:
            raise RuntimeError(f"数据版本不存在: {version}")
        report = check_stock_daily(row[0], expected_symbols=expected_symbols,
                                   expected_trade_date=request.get("period_end") if partition == str(request.get("period_end", ""))[:7] else None,
                                   source_conflicts=[])
        PipelineState(warehouse.meta_db_path).quality(version, status=report["status"],
                                                       checks=report["checks"], publish_allowed=report["publish_allowed"])
        reports[partition] = report
        allowed = allowed and report["publish_allowed"]
        statuses.append(report["status"])
    # 真实聚合状态：任一 FAIL 则 FAIL；否则按最差质量结论，不得把 WARNING 伪装为 PASS。
    if "FAIL" in statuses:
        quality_status = "FAIL"
    elif "WARNING" in statuses:
        quality_status = "WARNING"
    else:
        quality_status = "PASS"
    return {"rows": len(reports), "ok": allowed,
            "status": quality_status, "publish_allowed": allowed,
            "failed_count": sum(1 for status in statuses if status == "FAIL"),
            "reports": reports,
            "input_versions": versions, "output_versions": versions if allowed else {}}


def _publish(warehouse: Warehouse, request: dict) -> dict:
    versions = request.get("input_versions") or {}
    versions = {key: value.get("version") if isinstance(value, dict) else value
                for key, value in versions.items()}
    if not versions:
        # 禁止隐式历史候选补位：Publish 没有明确版本时必须失败。
        raise RuntimeError("Publish 缺少明确的 input_versions，禁止隐式选择历史 Candidate")
    published = {}
    for partition, version in versions.items():
        with sqlite3.connect(warehouse.meta_db_path) as conn:
            row = conn.execute("SELECT quality_status,publish_status FROM dataset_versions WHERE version_id=?", (version,)).fetchone()
        if not row:
            raise RuntimeError(f"数据版本不存在: {version}")
        if row[0] not in ("PASS", "WARNING"):
            raise RuntimeError(f"版本 {version} 质量未通过，禁止发布 (quality={row[0]})")
        if row[1] != "candidate":
            raise RuntimeError(f"版本 {version} 不是 candidate，禁止发布 (publish_status={row[1]})")
        published[partition] = Publisher(warehouse).publish(version)
    return {"rows": len(published), "published": published,
            "ok": True,
            "input_versions": versions,
            "output_versions": {key: value.get("version_id", versions[key])
                                 for key, value in published.items()}}


def _auxiliary(warehouse: Warehouse, request: dict, task_key: str) -> dict:
    symbols = [code for code in _symbols(request) if code.startswith(("sh6", "sz0", "sz3", "bj4", "bj8"))]
    if task_key in {"industry_capture", "industry_membership_capture"}:
        from StockInvestmentTool.warehouse.industry import IndustryCollector
        result = IndustryCollector(warehouse).collect_membership(codes=symbols)
        return {"rows": result["success"], "symbols": result["success"], "raw_batch_id": result["raw_batch_id"], "failed": result["failed"]}
    if task_key == "industry_daily_capture":
        from StockInvestmentTool.warehouse.industry import IndustryCollector
        result = IndustryCollector(warehouse).collect_daily(
            start_date=request.get("period_start"), end_date=request.get("period_end"))
        return result
    if task_key == "fundamentals_capture":
        output = FundamentalsCollector(warehouse=warehouse).collect_fundamentals(codes=symbols, years=5)
        output["rows"] = output.get("done", 0)
        output["symbols"] = output.get("done", 0)
        output["success_codes"] = [code for code in symbols if warehouse.fundamental_path(code).exists()]
        return output
    if task_key == "valuation_capture":
        # 估值是全市场逐只串行拉取，必须设 whole-task deadline，否则会长时间
        # 占用 web worker、拖垮其它页面请求。到期即收尾为超时，其余标的留给下一轮。
        import time as _time
        started = _time.monotonic()
        deadline = started + float(request.get("task_timeout") or 900)
        frames, failed_codes, pending = [], [], []
        first_frame = None
        for code in symbols:
            if _time.monotonic() >= deadline:
                pending.extend(symbols[symbols.index(code):] if code in symbols else [])
                break
            try:
                frame = ValuationBackfill.fetch_valuation_em(code, request.get("period_start"), request.get("period_end"))
            except Exception as exc:  # noqa: BLE001
                failed_codes.append(code)
                logger.warning("估值拉取失败 %s: %s", code, exc)
                continue
            if not frame.empty:
                if first_frame is None:
                    first_frame = frame
                frames.append(frame)
                for _, group in frame.groupby(frame["date"].dt.strftime("%Y-%m")):
                    warehouse.raw.upsert_rows("valuation", group)
        timed_out = bool(pending)
        if not frames:
            return {"rows": 0, "symbols": 0, "raw_batch_id": None, "failed_codes": failed_codes,
                    "timed_out": timed_out, "pending": len(pending)}
        raw = capture_frames(warehouse, dataset_name="valuation_daily", source_name="eastmoney", frames=frames,
                             trade_date_start=request.get("period_start"), trade_date_end=request.get("period_end"),
                             expected_symbols=len(symbols), success_symbols=len(frames), universe_id="valuation_task")
        return {"rows": sum(len(frame) for frame in frames), "symbols": len(frames), "raw_batch_id": raw["batch_id"],
                "failed_codes": failed_codes, "timed_out": timed_out, "pending": len(pending)}
    output = capture_money_flow("stock", "now", warehouse=warehouse)
    return output


def worker(task_key: str, warehouse: Warehouse, request: dict, run_id: int) -> dict:
    if task_key in {"financial_reports_capture", "valuation_snapshot_capture"}:
        return _new_source_task(warehouse, request, task_key, run_id)
    if task_key in {"financial_reports_build", "valuation_snapshot_build"}:
        return _source_build(warehouse, request, task_key.removesuffix("_build"))
    if task_key in {"financial_reports_quality", "valuation_snapshot_quality"}:
        return _new_quality(warehouse, request, task_key.removesuffix("_quality"))
    if task_key in {"financial_reports_publish", "valuation_snapshot_publish"}:
        return _publish(warehouse, request)
    if task_key == "valuation_daily_build":
        from StockInvestmentTool.warehouse.valuation_build import build_valuation_daily
        input_versions = request.get("input_versions") or {}
        if not input_versions.get("financial_reports") or not input_versions.get("valuation_snapshot"):
            raise ValueError("valuation_daily_build requires explicit financial and snapshot input_versions")
        from StockInvestmentTool.warehouse.datasets import DatasetAccess
        start, end = request.get("period_start"), request.get("period_end")
        # 财报只在实际报告期分区存在；不要按交易日窗口强制要求每个月都有财报。
        financial = DatasetAccess(warehouse).load_dataset(
            "financial_reports", partition_versions=input_versions["financial_reports"]
        ).data
        if start or end:
            dates = pd.to_datetime(financial["report_date"], errors="coerce")
            if start:
                financial = financial[dates >= pd.Timestamp(start)]
            if end:
                financial = financial[dates <= pd.Timestamp(end)]
        snapshots = DatasetAccess(warehouse).load_dataset("valuation_snapshot", start, end,
            partition_versions=input_versions["valuation_snapshot"]).data
        frame = build_valuation_daily(financial, snapshots)
        if frame.empty:
            raise ValueError("valuation_daily_build produced no rows")
        partition = (end or start)[:7]
        path = warehouse.base_dir / "candidates" / "valuation_daily" / partition / f"valuation_daily_{partition}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path, index=False)
        versions = PipelineState(warehouse.meta_db_path).record_output_versions(
            dataset_name="valuation_daily", paths={partition: path}, input_dataset="financial_reports",
            input_versions=input_versions, builder_version="valuation_daily_builder.v1", schema_version="valuation_daily.v2")
        return {"rows": len(frame), "output_versions": versions, "versions": versions}
    if task_key == "valuation_daily_quality":
        from StockInvestmentTool.warehouse.valuation_build import quality_report
        versions = request.get("input_versions") or {}
        reports = {}
        for partition, version in versions.items():
            with sqlite3.connect(warehouse.meta_db_path) as conn:
                row = conn.execute("SELECT candidate_path FROM dataset_versions WHERE version_id=?", (version,)).fetchone()
            if not row: raise ValueError(f"版本不存在: {version}")
            report = quality_report(pd.read_parquet(row[0]), expected_symbols=request.get("expected_symbols"))
            PipelineState(warehouse.meta_db_path).quality(version, status=report["status"], checks=report["checks"], publish_allowed=report["publish_allowed"])
            reports[partition] = report
        return {"status": "PASS" if all(x["status"] == "PASS" for x in reports.values()) else "WARNING", "reports": reports, "input_versions": versions, "output_versions": versions}
    if task_key == "valuation_daily_publish":
        return _publish(warehouse, request)
    if task_key == "stock_daily_capture":
        return _capture(warehouse, request, run_id)
    if task_key == "stock_daily_build":
        return _build(warehouse, request)
    if task_key == "stock_daily_quality":
        return _quality(warehouse, request)
    if task_key == "stock_daily_publish":
        return _publish(warehouse, request)
    if task_key == "indicators_build":
        result = IndicatorsBuilder(warehouse, allow_legacy=False).build_all(
            symbols=_symbols(request), asset_types=["stock", "etf"], months=_months(request.get("period_start"), request.get("period_end")),
            partition_versions=request.get("input_versions") or None)
        # Indicators are formal derivatives of the Published daily dataset.
        # Record alignment explicitly so a stale derived partition is visible
        # instead of being mistaken for a current successful rebuild.
        alignment = []
        with sqlite3.connect(warehouse.meta_db_path) as conn:
            for partition, version_id in (result.get("output_versions") or {}).items():
                daily = conn.execute(
                    "SELECT max_date FROM dataset_versions v "
                    "JOIN dataset_current c ON c.version_id=v.version_id "
                    "WHERE c.dataset_name='stock_daily' AND c.partition_key=?",
                    (partition,),
                ).fetchone()
                derived = conn.execute(
                    "SELECT max_date FROM dataset_versions WHERE version_id=?",
                    (version_id,),
                ).fetchone()
                if daily and derived:
                    alignment.append({"partition": partition,
                                      "daily_max_date": daily[0],
                                      "indicators_max_date": derived[0],
                                      "matches": daily[0] == derived[0]})
        result["date_alignment"] = alignment
        if any(not item["matches"] for item in alignment):
            result["status"] = "partial_success"
        return result
    if task_key == "industry_features_build":
        from StockInvestmentTool.warehouse.industry_features import IndustryFeaturesBuilder
        end = request.get("period_end") or request.get("as_of")
        start = request.get("period_start") or end
        if not end:
            raise ValueError("industry_features_build requires period_end/as_of")
        return IndustryFeaturesBuilder(warehouse, allow_legacy=False).build(
            start_date=start, end_date=end, as_of=request.get("as_of") or end,
            partition_versions=request.get("input_versions") or None)
    if task_key == "industry_rotation_build":
        from StockInvestmentTool.warehouse.industry_rotation import IndustryRotationBuilder
        end = request.get("period_end") or request.get("as_of")
        start = request.get("period_start") or end
        if not end:
            raise ValueError("industry_rotation_build requires period_end/as_of")
        return IndustryRotationBuilder(warehouse, allow_legacy=False).build(
            start_date=start, end_date=end, as_of=request.get("as_of") or end,
            partition_versions=request.get("input_versions") or None)
    if task_key in {"industry_capture", "industry_membership_capture", "industry_daily_capture", "fundamentals_capture", "valuation_capture", "money_flow_capture"}:
        return _auxiliary(warehouse, request, task_key)
    raise ValueError(f"未注册的任务: {task_key}")


def execute_task(db_path: Path, task_key: str, payload: dict,
                 request_id: str | None = None) -> dict:
    center = TaskCenter(db_path, db_path)
    task = center.task(task_key)
    if task is None:
        raise ValueError(f"任务不存在: {task_key}")
    symbols = payload.get("symbols") or []
    # Industry index capture discovers its 90 THS industries from the source;
    # it is not a security-universe task and must not require instruments with
    # type=industry in the management catalog.
    requires_security_scope = task_key != "industry_daily_capture"
    if not symbols and requires_security_scope:
        config = json.loads(task["config_versions"][0]["config"]) if task.get("config_versions") else {}
        asset_types = set((config.get("scope") or {}).get("asset_types") or [])
        catalog = Warehouse(meta_db_path=db_path).list_instruments(asset_types=asset_types)
        symbols = [item["code"] for item in catalog]
    if not symbols and requires_security_scope:
        raise ValueError(f"任务 {task_key} 没有可执行的证券范围")
    payload = {**payload, "symbols": symbols}
    if request_id is None:
        request_id = center.create_request(task_key, payload.get("trigger_type", "manual"),
                                           period_start=payload.get("period_start"), period_end=payload.get("period_end"),
                                           symbols=symbols, requested_by=payload.get("requested_by", "admin"),
                                           input_versions=payload.get("input_versions") or {})
    from StockInvestmentTool.ops.task_runner import TaskRunner
    warehouse = Warehouse(meta_db_path=db_path)
    runner = TaskRunner(db_path, db_path)
    result = runner.execute(task_key, lambda run_id, request: worker(task_key, warehouse, {**request, **payload}, run_id),
                            request_id=request_id, input_dataset=payload.get("input_dataset", ""),
                            output_dataset=payload.get("output_dataset", ""),
                            parent_run_id=payload.get("parent_run_id"))
    result["request_id"] = request_id
    result["status_url"] = f"/api/data/jobs/{result.get('run_id')}"
    return result


def precreate_request(db_path: Path, task_key: str, payload: dict) -> str:
    """202 返回前同步持久化 Request（不创建 run，由后台线程执行时创建）。"""
    center = TaskCenter(db_path, db_path)
    symbols = payload.get("symbols") or []
    return center.create_request(task_key, payload.get("trigger_type", "manual"),
                                 period_start=payload.get("period_start"), period_end=payload.get("period_end"),
                                 symbols=symbols, requested_by=payload.get("requested_by", "admin"),
                                 input_versions=payload.get("input_versions") or {})


def execute_pipeline(db_path: Path, task_keys: list[str], payload: dict | None = None) -> dict:
    """Run configured task keys in order and pass each result to its child."""
    if not task_keys:
        raise ValueError("流水线至少需要一个任务")
    payload = dict(payload or {})
    runs = []
    parent_run_id = None
    input_versions = payload.get("input_versions") or {}
    for task_key in task_keys:
        current = {**payload, "input_versions": input_versions, "parent_run_id": parent_run_id}
        item = execute_task(db_path, task_key, current)
        runs.append(item)
        result = item.get("result") or {}
        parent_run_id = item.get("run_id")
        output_versions = result.get("output_versions") or result.get("versions") or {}
        if output_versions:
            input_versions = output_versions
        if result.get("source_batch_id"):
            payload["input_batch_id"] = result["source_batch_id"]
        # skipped means this stage produced no new input (for example its lock
        # is held or it is already up-to-date). Downstream build/quality/publish
        # must never run against stale versions in that case.
        if item.get("status") not in {"success", "partial_success"}:
            break
    return {"status": runs[-1].get("status", "failed"), "runs": runs,
            "request_ids": [item.get("request_id") for item in runs]}
