# -*- coding: utf-8 -*-
"""全量指标批量生成 — 用指标体系计算全市场指标宽表，落 indicators/ 分区

设计（按标的批次流式，控制内存）:
  - 目标标的按批处理（batch_size），每批只把「该批标的全史」载入内存
  - 每个 daily 分区用 pyarrow filters 下推只读命中标的的行
  - 逐标的用 IndicatorRegistry 计算可配置/组合指标
  - 按月累积落盘到 warehouse/indicators/YYYY-MM.parquet
  - 内存峰值 = 单批标的全史一份（batch_size=500 约 40MB，1GiB 容器可承受）

衔接: 由 run_warehouse_daily（收盘后采集）自动触发，
      源头 daily 更新后，下游 indicators 自动重建。
"""

from __future__ import annotations

import gc
import logging
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from StockInvestmentTool.indicators.engine import IndicatorRegistry
from StockInvestmentTool.warehouse.datasets import DatasetAccess, DatasetAccessError
from StockInvestmentTool.warehouse.storage import Warehouse

logger = logging.getLogger(__name__)

# 最长滚动指标窗口（ma240 ≈ 240 交易日）。为保证窄窗口构建（如仅当月增量）
# 也能算出真实 ma5/ma20/ma60/ma240，加载历史需向前扩展的月份数。
# 240 交易日 ≈ 12 个月自然月，多留 2 个月缓冲覆盖节假日/停牌导致的交易日稀疏。
LOOKBACK_MONTHS = 14


def _extend_load_months(months: list[str], *, lookback: int = LOOKBACK_MONTHS) -> list[str]:
    """Expand a (possibly single-month) output window into a load window that
    goes back far enough for the longest rolling indicator.

    The output month(s) are unchanged; this only enlarges the read history so
    rolling means are not NaN just because an incremental build only requested
    the current month.
    """
    if not months:
        return months
    try:
        first = pd.Timestamp(months[0]).replace(day=1)
    except (TypeError, ValueError):
        return months
    start = (first - pd.offsets.MonthBegin(lookback)).strftime("%Y-%m")
    # union all months from start..latest requested month
    result = []
    cursor = pd.Timestamp(start)
    end = pd.Timestamp(months[-1]).replace(day=1)
    while cursor <= end:
        result.append(cursor.strftime("%Y-%m"))
        cursor = cursor + pd.offsets.MonthBegin(1)
    return result


def _indicator_schema_columns() -> set[str]:
    """Standard indicator dataset columns that must always be present.

    Derived from config/datasets/indicators.yaml fields. Keeping these columns
    (as NaN when a short month has no history) prevents cross-partition schema
    mismatch when readers glob across monthly parquet files.
    """
    from StockInvestmentTool.warehouse.dataset_config import load_dataset_config
    try:
        config = load_dataset_config("indicators")
        return {item["name"] for item in config.get("fields", [])}
    except Exception:
        return {
            "date", "code", "close", "ma5", "ma10", "ma20", "ma60", "ma120",
            "ma240", "ma17", "ma63", "rsi14", "macd", "atr14", "change_amount",
            "amplitude", "vol_ma5", "low_3m", "year_low", "volatility_20",
            "pct_chg", "vol_ratio", "ret_5d", "ret_20d", "high_20d", "low_20d",
            "bias_ratio", "take_profit_reference", "dual_ma_low", "amplitude_abs",
            "custom_example",
        }


class IndicatorsBuilder:
    """全市场指标宽表批量生成器。"""

    def __init__(self, warehouse: Optional[Warehouse] = None,
                 registry: Optional[IndicatorRegistry] = None, allow_legacy: bool = False):
        self.warehouse = warehouse or Warehouse()
        self.registry = registry or IndicatorRegistry()
        self.allow_legacy = allow_legacy

    # ── 分区解析（一次校验，流式复用）──────────────────

    def _resolve_partitions(self, months: list[str],
                            partition_versions: Optional[dict[str, str]] = None,
                            *, lenient: bool = False) -> dict[str, dict]:
        """解析 daily 各月份分区的发布信息（路径/版本），并做一次质量+checksum 校验。

        Args:
            lenient: 为 True 时，缺失或未发布的月份只跳过（用于扩展的载入窗口），
                不抛错；为 False 时（默认，用于调用方请求的输出窗口）缺失即失败。

        Returns:
            {ym: {"path": Path | None, "version_id": str | None}}
            无 Published 且 allow_legacy 时 path=None（后续走 read_daily 直读）。
        """
        access = DatasetAccess(self.warehouse)
        versions = access.get_current_version("stock_daily")
        if partition_versions:
            versions = {m: {"version_id": vid} for m, vid in partition_versions.items()}
        resolved: dict[str, dict] = {}
        if not versions:
            if not self.allow_legacy:
                raise DatasetAccessError("没有 Published Dataset: stock_daily")
            return {ym: {"path": None, "version_id": None} for ym in months}
        for ym in months:
            current = versions.get(ym)
            if not current:
                if not self.allow_legacy:
                    if lenient:
                        logger.warning("载入窗口分区未发布，跳过（历史回看不足该月）: %s", ym)
                        continue
                    raise DatasetAccessError(f"分区没有正式版本: stock_daily/{ym}")
                resolved[ym] = {"path": None, "version_id": None}
                continue
            version, quality = access._version_context(current["version_id"])
            if not access._quality_allowed(quality, "WARNING"):
                if lenient:
                    logger.warning("载入窗口分区质量不满足，跳过: %s", ym)
                    continue
                raise DatasetAccessError(f"正式版本质量不满足要求: stock_daily/{ym}")
            path = Path(version["published_path"] or "")
            if not path.exists():
                if lenient:
                    logger.warning("载入窗口正式文件不存在，跳过: %s", ym)
                    continue
                raise DatasetAccessError(f"正式文件不存在: {path}")
            import hashlib
            if hashlib.sha256(path.read_bytes()).hexdigest() != version["checksum"]:
                logger.warning("正式文件 checksum 不匹配，继续处理: %s", path)
            resolved[ym] = {"path": path, "version_id": current["version_id"]}
        return resolved

    def _all_symbols(self, months: list[str]) -> list[str]:
        """从 daily 分区收集全部 code（只读 code 列，不载入全表）。"""
        import pyarrow.parquet as pq
        codes: set[str] = set()
        for ym in months:
            path = self.warehouse.daily_partition(ym)
            if not path.exists():
                continue
            codes.update(str(c) for c in pq.read_table(str(path), columns=["code"])["code"].to_pylist())
        return sorted(codes)

    def _load_batch(self, months: list[str], batch: list[str],
                    partitions: dict[str, dict]) -> dict[str, list[pd.DataFrame]]:
        """载入「单批标的全史」到内存（pyarrow filters 下推，只解压命中行）。"""
        batch_set = {str(code) for code in batch}
        per_code: dict[str, list[pd.DataFrame]] = {}
        for ym in months:
            info = partitions.get(ym)
            if info is None:
                continue
            path = info["path"]
            if path is None:
                df = self.warehouse.read_daily(ym)
                if df is not None and not df.empty:
                    df = df[df["code"].astype(str).isin(batch_set)]
            else:
                try:
                    df = pd.read_parquet(path, filters=[("code", "in", list(batch_set))])
                except Exception as e:
                    logger.warning("读取分区 %s 失败: %s", ym, e)
                    continue
            if df is None or df.empty or "code" not in df.columns:
                continue
            for code, grp in df.groupby("code"):
                per_code.setdefault(str(code), []).append(grp)
        return per_code

    def build_all(self, symbols: Optional[list[str]] = None,
                  max_symbols: Optional[int] = None,
                  metrics: Optional[list[str]] = None,
                  flush_every: int = 500,
                  batch_size: int = 500,
                   progress_callback=None, changed_start: Optional[str] = None,
                   changed_end: Optional[str] = None, asset_types: Optional[list[str]] = None,
                   months: Optional[list[str]] = None,
                   partition_versions: Optional[dict[str, str]] = None) -> dict:
        """全市场指标宽表生成（按标的批次流式，控制内存）。

        需仓库已有 daily 分区（先跑 sync_daily）。

        Args:
            flush_every: 每处理 N 个标的落盘一次，防止中断丢失全部内存成果。
            batch_size: 每批同时载入内存的标的数量（控制内存峰值）。

        Returns:
            dict: 标的数 / 覆盖月份 / 耗时
        """
        months = months or self.warehouse.available_months("daily")
        if not months:
            logger.warning("无日线分区，请先运行 sync")
            return {"symbols": 0, "months": 0, "rows": 0, "failed": [], "skipped": True,
                    "elapsed_sec": 0, "input_dataset": "stock_daily", "input_versions": {},
                    "input_fallback_used": False, "output_versions": {}}

        # 输出窗口限定为调用方请求的 months；加载窗口需向前扩展足够历史，
        # 否则增量（仅当月）构建时 ma5/ma20/ma60/ma240 因无历史而算出 NaN。
        output_months = set(months)
        load_months = _extend_load_months(months)

        # ① 解析分区（一次质量/checksum 校验），并确定标的全集。
        # 扩展的载入窗口用 lenient，缺失历史月份只跳过；调用方请求的输出月份
        # 必须可用，否则无意义地产出空分区。
        logger.info("指标计算: 解析 %d 个月分区版本（加载窗口 %d 个月）...",
                    len(months), len(load_months))
        partitions = self._resolve_partitions(load_months, partition_versions, lenient=True)
        input_versions = {ym: info["version_id"] for ym, info in partitions.items() if info["version_id"]}
        missing_output = [ym for ym in months if ym not in partitions]
        if missing_output and not self.allow_legacy:
            raise DatasetAccessError(f"输出月份没有正式版本: {missing_output}")
        if symbols is None:
            symbols = self._all_symbols(load_months)
        from StockInvestmentTool.warehouse.asset_profiles import select_symbols
        symbols, asset_type_counts = select_symbols(
            symbols, asset_types=asset_types, known_types=self.warehouse.instrument_types()
        )
        if max_symbols:
            symbols = symbols[:max_symbols]
        logger.info("指标计算: %d 标的，分批 %d", len(symbols), batch_size)
        if not symbols:
            return {"symbols": 0, "months": 0, "rows": 0, "failed": [], "skipped": True,
                    "elapsed_sec": 0, "input_dataset": "stock_daily", "input_versions": input_versions,
                    "input_fallback_used": not bool(input_versions), "output_versions": {},
                    "asset_type_counts": asset_type_counts}

        batches = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]
        total = len(symbols)

        # ② 分批流式：每批载入 → 逐标的算 → 落盘 → 清空，内存有界
        month_bufs: dict[str, pd.DataFrame] = {}
        done = 0
        failed: list[str] = []
        output_rows = 0
        t0 = time.time()
        written_months: set[str] = set()
        processed = 0
        # 输出月份默认限定为调用方请求的 months；若提供增量区间，再做一次交集。
        output_months = set(months)
        if changed_start and changed_end:
            from StockInvestmentTool.warehouse.incremental import affected_partitions
            output_months = output_months & set(affected_partitions(changed_start, changed_end))

        def _flush():
            for ym, df in month_bufs.items():
                if df is None or len(df) == 0:
                    continue
                existing = self.warehouse.read_indicator(ym)
                if existing is not None and len(existing):
                    selected_codes = set(df["code"].astype(str))
                    existing = existing[~existing["code"].astype(str).isin(selected_codes)]
                    df = pd.concat([existing, df], ignore_index=True)
                # Rebuilds may add indicator columns; prefer the new row so
                # stale rows from an older schema cannot mask fresh values.
                df = df.drop_duplicates(subset=["date", "code"], keep="last")
                df = df.sort_values(["date", "code"])
                # 指标分区必须保持统一 schema。对标准指标列（如 ma5/ma20/ma60）即使某月
                # 只有少量交易日、滚动均线全为 NaN，也要保留该列，避免跨分区 schema 不一致
                # （DuckDB glob 读取时报 schema mismatch）。只丢弃非标准列且全为空的列。
                if "code" in df.columns and len(df):
                    standard = _indicator_schema_columns()
                    extra = [c for c in df.columns if c not in standard and df[c].isna().all()]
                    if extra:
                        df = df.drop(columns=extra)
                self.warehouse.write_indicator_partition(ym, df)
                written_months.add(ym)
            month_bufs.clear()

        for batch in batches:
            per_code = self._load_batch(load_months, batch, partitions)
            for code in batch:
                if progress_callback:
                    progress_callback(processed, total, code, "计算指标")
                frames = per_code.get(code)
                if not frames:
                    processed += 1
                    continue
                df = pd.concat(frames, ignore_index=True).sort_values("date")
                try:
                    ind_series = self.registry.compute(df, metrics)
                except Exception as e:
                    logger.warning("指标计算 %s 失败: %s", code, e)
                    failed.append(str(code))
                    processed += 1
                    continue
                if not ind_series:
                    processed += 1
                    continue
                # 组装指标宽表（date/code + 各指标列）
                out = pd.DataFrame({"date": df["date"], "code": code})
                for name, s in ind_series.items():
                    if s is not None:
                        out[name.lower()] = s.values
                for ym, grp in out.groupby(out["date"].dt.strftime("%Y-%m")):
                    # 只落调用方请求的输出月份；扩展载入的历史月份仅用于计算，
                    # 不作为本轮输出。
                    if ym not in output_months:
                        continue
                    cur = month_bufs.get(ym)
                    if cur is not None and len(cur):
                        month_bufs[ym] = pd.concat([cur, grp], ignore_index=True)
                    else:
                        month_bufs[ym] = grp.copy()
                done += 1
                output_rows += len(out)
                processed += 1
                if progress_callback:
                    progress_callback(processed, total, code, "指标已计算")
                if processed % flush_every == 0 or processed == total:
                    _flush()
                    logger.info("指标进度 %d/%d，完成 %d 只（已落盘）", processed, total, done)
            # 释放本批内存，进入下一批
            per_code.clear()
            gc.collect()

        # 兜底落盘
        if month_bufs:
            _flush()

        elapsed = time.time() - t0
        from StockInvestmentTool.warehouse.pipeline_state import PipelineState
        from StockInvestmentTool.warehouse.publish import Publisher
        from StockInvestmentTool.warehouse.quality import check_derived_output
        state = PipelineState(self.warehouse.meta_db_path)
        # 派生数据集不得自动登记 PASS —— 先登记 candidate，再独立质量检查 + 发布
        output_versions = state.record_output_versions(
            dataset_name="indicators",
            paths={ym: self.warehouse.indicator_dir / f"{ym}.parquet" for ym in written_months},
            input_dataset="stock_daily", input_versions=input_versions,
            builder_version="indicators_builder.v1", schema_version="indicators.v1",
        )
        publisher = Publisher(self.warehouse)
        quality_summary = {"PASS": 0, "WARNING": 0, "FAIL": 0}
        for ym, version_id in output_versions.items():
            path = self.warehouse.indicator_dir / f"{ym}.parquet"
            result = check_derived_output(
                path, dataset_name="indicators",
                expected_symbols=len(symbols),
                core_non_null_columns=["ma5", "ma20", "ma60", "rsi14"],
                input_versions={ym: version_id},
            )
            quality_summary[result["status"]] = quality_summary.get(result["status"], 0) + 1
            state.quality(version_id, status=result["status"],
                          checks=result["checks"], publish_allowed=result["publish_allowed"],
                          affected_symbols=symbols[:1000])
            if result["status"] in ("PASS", "WARNING"):
                try:
                    publisher.publish(version_id)
                except Exception as e:
                    logger.warning("指标发布 %s 失败: %s", ym, e)
        from StockInvestmentTool.ops.task_center import TaskCenter
        center = TaskCenter(self.warehouse.meta_db_path)
        center.sync_metrics()
        latest_period = max(months) if months else None
        for metric in metrics or [item["metric_key"] for item in center.list_metrics() if item["producer_task"] == "indicators_build"]:
            center.update_metric_health(metric, latest_period=latest_period, covered_objects=done,
                                        expected_objects=len(symbols), status="healthy" if not failed else "partial",
                                        message="; ".join(failed[:5]),
                                        asset_type_counts=asset_type_counts)
        logger.info("指标计算完成: %d 只, 覆盖 %d 个月, 耗时 %.1fs",
                    done, len(written_months), elapsed)
        if quality_summary["FAIL"]:
            status = "failed"
        elif quality_summary["WARNING"]:
            status = "partial_success"
        elif failed:
            status = "partial_success"
        else:
            status = "success"
        return {"symbols": done, "months": len(written_months),
                 "rows": output_rows,
                 "failed": failed[:100], "failed_count": len(failed),
                 "status": status, "skipped": not symbols,
                 "elapsed_sec": round(elapsed, 1),
                 "quality_summary": quality_summary,
                 "input_dataset": "stock_daily", "input_versions": input_versions,
                 "input_fallback_used": not bool(input_versions), "output_versions": output_versions,
                 "asset_type_counts": asset_type_counts}
