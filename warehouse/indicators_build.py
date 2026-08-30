# -*- coding: utf-8 -*-
"""全量指标批量生成 — 用指标体系计算全市场指标宽表，落 indicators/ 分区

设计（与 factors.py 相同的内存模式）:
  - 每个 daily 分区只读一次，按 code 分组
  - 逐标的用 IndicatorRegistry 计算可配置/组合指标
  - 按月累积落盘到 warehouse/indicators/YYYY-MM.parquet
  - 内存峰值 = 全量日线一份（2C2G 可承受，作为离线任务）

衔接: 由 run_warehouse_daily（收盘后采集）自动触发，
      源头 daily 更新后，下游 indicators 自动重建。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd

from StockInvestmentTool.indicators.engine import IndicatorRegistry
from StockInvestmentTool.warehouse.storage import Warehouse

logger = logging.getLogger(__name__)


class IndicatorsBuilder:
    """全市场指标宽表批量生成器。"""

    def __init__(self, warehouse: Optional[Warehouse] = None,
                 registry: Optional[IndicatorRegistry] = None, allow_legacy: bool = False):
        self.warehouse = warehouse or Warehouse()
        self.registry = registry or IndicatorRegistry()
        self.allow_legacy = allow_legacy

    def build_all(self, symbols: Optional[list[str]] = None,
                  max_symbols: Optional[int] = None,
                  metrics: Optional[list[str]] = None,
                  flush_every: int = 500,
                   progress_callback=None, changed_start: Optional[str] = None,
                   changed_end: Optional[str] = None, asset_types: Optional[list[str]] = None,
                   months: Optional[list[str]] = None,
                   partition_versions: Optional[dict[str, str]] = None) -> dict:
        """全市场指标宽表生成（分组一次遍历 + 分批落盘）。

        需仓库已有 daily 分区（先跑 sync_daily）。

        Args:
            flush_every: 每处理 N 个标的落盘一次，防止中断丢失全部内存成果。

        Returns:
            dict: 标的数 / 覆盖月份 / 耗时
        """
        months = months or self.warehouse.available_months("daily")
        if not months:
            logger.warning("无日线分区，请先运行 sync")
            return {"symbols": 0, "months": 0, "rows": 0, "failed": [], "skipped": True,
                    "elapsed_sec": 0, "input_dataset": "stock_daily", "input_versions": {},
                    "input_fallback_used": False, "output_versions": {}}

        # ① 每个分区只读一次，按 code 分组，累积各标的全史
        logger.info("指标计算: 载入 %d 个月分区...", len(months))
        per_code: dict[str, list[pd.DataFrame]] = {}
        input_versions = {}
        for ym in months:
            try:
                from StockInvestmentTool.warehouse.datasets import load_dataset
                month_start = pd.Timestamp(f"{ym}-01")
                month_end = month_start + pd.offsets.MonthEnd(1)
                loaded = load_dataset(self.warehouse, "stock_daily", str(month_start.date()), str(month_end.date()),
                                      allow_legacy=self.allow_legacy,
                                      partition_versions=partition_versions)
                df = loaded.data
                input_versions.update(loaded.context.get("partition_versions", {}))
            except Exception:
                if not self.allow_legacy:
                    raise
                df = self.warehouse.read_daily(ym)
            if df is None or df.empty or "code" not in df.columns:
                continue
            for code, grp in df.groupby("code"):
                per_code.setdefault(code, []).append(grp)

        if symbols is None:
            symbols = list(per_code.keys())
        from StockInvestmentTool.warehouse.asset_profiles import select_symbols
        symbols, asset_type_counts = select_symbols(
            symbols, asset_types=asset_types, known_types=self.warehouse.instrument_types()
        )
        if max_symbols:
            symbols = symbols[:max_symbols]
        logger.info("指标计算: %d 标的", len(symbols))

        # ② 逐标的算指标，按月份累积；每 flush_every 个标的落盘一次并清空，
        #    避免中断丢失全部成果（2C2G 下内存也有界）。
        month_bufs: dict[str, pd.DataFrame] = {}
        done = 0
        failed: list[str] = []
        output_rows = 0
        t0 = time.time()
        written_months: set[str] = set()
        output_months = None
        if changed_start and changed_end:
            from StockInvestmentTool.warehouse.incremental import affected_partitions
            output_months = set(affected_partitions(changed_start, changed_end))

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
                self.warehouse.write_indicator_partition(ym, df)
                written_months.add(ym)
            month_bufs.clear()

        for i, code in enumerate(symbols, 1):
            if progress_callback:
                progress_callback(i - 1, len(symbols), code, "计算指标")
            frames = per_code.get(code)
            if not frames:
                continue
            df = pd.concat(frames, ignore_index=True).sort_values("date")
            try:
                ind_series = self.registry.compute(df, metrics)
            except Exception as e:
                logger.warning("指标计算 %s 失败: %s", code, e)
                failed.append(str(code))
                continue
            if not ind_series:
                continue
            # 组装指标宽表（date/code + 各指标列）
            out = pd.DataFrame({"date": df["date"], "code": code})
            for name, s in ind_series.items():
                if s is not None:
                    out[name.lower()] = s.values
            for ym, grp in out.groupby(out["date"].dt.strftime("%Y-%m")):
                if output_months is not None and ym not in output_months:
                    continue
                cur = month_bufs.get(ym)
                if cur is not None and len(cur):
                    month_bufs[ym] = pd.concat([cur, grp], ignore_index=True)
                else:
                    month_bufs[ym] = grp.copy()
            done += 1
            output_rows += len(out)
            if progress_callback:
                progress_callback(i, len(symbols), code, "指标已计算")
            if i % flush_every == 0 or i == len(symbols):
                _flush()
                logger.info("指标进度 %d/%d，完成 %d 只（已落盘）", i, len(symbols), done)

        # 兜底落盘
        if month_bufs:
            _flush()

        elapsed = time.time() - t0
        from StockInvestmentTool.warehouse.pipeline_state import PipelineState
        output_versions = PipelineState(self.warehouse.meta_db_path).record_output_versions(
            dataset_name="indicators",
            paths={ym: self.warehouse.indicator_dir / f"{ym}.parquet" for ym in written_months},
            input_dataset="stock_daily", input_versions=input_versions,
            builder_version="indicators_builder.v1", schema_version="indicators.v1",
        )
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
        return {"symbols": done, "months": len(written_months),
                 "rows": output_rows,
                 "failed": failed[:100], "failed_count": len(failed),
                 "skipped": not symbols, "elapsed_sec": round(elapsed, 1),
                 "input_dataset": "stock_daily", "input_versions": input_versions,
                 "input_fallback_used": not bool(input_versions), "output_versions": output_versions,
                 "asset_type_counts": asset_type_counts}
