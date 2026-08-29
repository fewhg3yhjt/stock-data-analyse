# -*- coding: utf-8 -*-
"""全市场因子宽表计算 — 月度分块，绝不全量载入内存

设计:
  - 因子是「价格/量能/估值」等衍生序列，是后续全市场分析的核心输入
  - 每个因子按股票滚动计算需要历史窗口（如 MA20 需前 20 日），因此
    「按月独立算」会缺滚动窗口 —— 这里采用「按标的滑动窗口 + 结果按月落盘」
  - 为控制内存：逐标的读取其仓库内全部数据（单标的全史 ~1250 行，极小），
    计算因子后按月份切分写入对应月份因子分区
  - 需要较大历史窗口的因子（MA60/MA120）依赖足够长的历史，首建时
    建议先同步足够历史（如 5 年）再算因子
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd

from StockInvestmentTool.warehouse.storage import Warehouse

logger = logging.getLogger(__name__)

# 默认因子列（前缀列来自日线，新增为计算列）
FACTOR_COLUMNS = [
    "date", "code", "close", "volume", "amount",
    "ma5", "ma10", "ma20", "ma60", "ma120",
    "bias_ratio",          # 乖离率 (close - MA250)/MA250
    "vol_ratio",           # 量比 = volume / 5日均量
    "pct_chg",             # 单日涨跌幅 %
    "volatility_20",       # 20日年化波动率 %
    "pe_ttm", "pb",
    "ret_5d", "ret_20d",   # 5/20 日动量
    "high_20d", "low_20d", # 20 日高低
]


class FactorEngine:
    """因子宽表计算引擎。"""

    def __init__(self, warehouse: Optional[Warehouse] = None, allow_legacy: bool = False):
        self.warehouse = warehouse or Warehouse()
        self.allow_legacy = allow_legacy

    def compute_factor_row(self, df: pd.DataFrame) -> pd.DataFrame:
        """对单只标的的全史日线计算因子（df 需按日期升序）。

        返回含原始列 + 因子列的 DataFrame。
        """
        df = df.sort_values("date").reset_index(drop=True).copy()
        # Historical daily sources use vendor spellings; retain canonical values.
        if "pe_ttm" not in df.columns and "peTTM" in df.columns:
            df["pe_ttm"] = df["peTTM"]
        if "pb" not in df.columns and "pbMRQ" in df.columns:
            df["pb"] = df["pbMRQ"]
        close = df["close"]

        # 均线
        for w in (5, 10, 20, 60, 120, 250):
            df[f"ma{w}"] = close.rolling(w).mean()

        # 乖离率 (close - MA250)/MA250
        df["bias_ratio"] = (close - df["ma250"]) / df["ma250"] * 100

        # 量比 = 当日量 / 5日均量
        vol_ma5 = df["volume"].rolling(5).mean()
        df["vol_ratio"] = df["volume"] / vol_ma5

        # 单日涨跌幅
        df["pct_chg"] = close.pct_change() * 100

        # 20日年化波动率（对数收益率）
        log_ret = (close / close.shift(1)).apply(lambda x: __import__("math").log(x))
        df["volatility_20"] = log_ret.rolling(20).std() * (252 ** 0.5) * 100

        # 动量
        df["ret_5d"] = close.pct_change(5) * 100
        df["ret_20d"] = close.pct_change(20) * 100

        # 20日高低
        df["high_20d"] = df["high"].rolling(20).max()
        df["low_20d"] = df["low"].rolling(20).min()

        # 只保留因子列（含原始 pe_ttm/pb）
        keep = [c for c in FACTOR_COLUMNS if c in df.columns]
        return df[keep]

    def build_factors(self, symbols: Optional[list[str]] = None,
                      max_symbols: Optional[int] = None,
                      progress_callback=None, changed_start: Optional[str] = None,
                      changed_end: Optional[str] = None, asset_types: Optional[list[str]] = None,
                      months: Optional[list[str]] = None) -> dict:
        """全市场因子宽表计算（分组一次遍历 + 按月落盘）。

        需仓库已有日线分区（先跑 collect sync_daily）。

        性能设计: 每个月份分区只读一次，按 code 分组合并成各标的全史，
        再逐标计算因子。避免「每标的 × 每月」的 N×M 次分区读取。
        内存峰值 = 全量日线一份（近3年全市场约 500-600MB，2C2G 可承受，
        作为独立离线任务运行；与 web 同进程时建议错峰）。
        """
        months = months or self.warehouse.available_months("daily")
        if not months:
            logger.warning("无日线分区，请先运行 sync")
            return {"symbols": 0, "months": 0, "rows": 0, "failed": [], "skipped": True,
                    "elapsed_sec": 0, "input_dataset": "stock_daily", "input_versions": {},
                    "input_fallback_used": False, "output_versions": {}}

        # ① 每个分区只读一次，按 code 分组，累积各标的全史
        logger.info("因子计算: 载入 %d 个月分区...", len(months))
        per_code: dict[str, list[pd.DataFrame]] = {}
        input_versions = {}
        for ym in months:
            try:
                from StockInvestmentTool.warehouse.datasets import load_dataset
                month_start = pd.Timestamp(f"{ym}-01")
                month_end = month_start + pd.offsets.MonthEnd(1)
                loaded = load_dataset(self.warehouse, "stock_daily", str(month_start.date()), str(month_end.date()),
                                      allow_legacy=self.allow_legacy)
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

        # ② 限定标的集
        if symbols is None:
            symbols = list(per_code.keys())
        from StockInvestmentTool.warehouse.asset_profiles import select_symbols
        symbols, asset_type_counts = select_symbols(
            symbols, asset_types=asset_types, known_types=self.warehouse.instrument_types()
        )
        if max_symbols:
            symbols = symbols[:max_symbols]
        logger.info("因子计算: %d 标的", len(symbols))

        # ③ 逐标的算因子，按月份累积
        month_bufs: dict[str, pd.DataFrame] = {}
        done = 0
        failed: list[str] = []
        output_rows = 0
        t0 = time.time()
        output_months = None
        if changed_start and changed_end:
            from StockInvestmentTool.warehouse.incremental import affected_partitions
            output_months = set(affected_partitions(changed_start, changed_end))
        for i, code in enumerate(symbols, 1):
            if progress_callback:
                progress_callback(i - 1, len(symbols), code, "计算因子")
            frames = per_code.get(code)
            if not frames:
                continue
            df = pd.concat(frames, ignore_index=True).sort_values("date")
            try:
                fdf = self.compute_factor_row(df)
            except Exception as e:
                logger.warning("因子计算 %s 失败: %s", code, e)
                failed.append(str(code))
                continue
            for ym, grp in fdf.groupby(fdf["date"].dt.strftime("%Y-%m")):
                if output_months is not None and ym not in output_months:
                    continue
                cur = month_bufs.get(ym)
                if cur is not None and len(cur):
                    month_bufs[ym] = pd.concat([cur, grp], ignore_index=True)
                else:
                    month_bufs[ym] = grp.copy()
            done += 1
            output_rows += len(fdf)
            if progress_callback:
                progress_callback(i, len(symbols), code, "因子已计算")
            if i % 500 == 0 or i == len(symbols):
                logger.info("因子进度 %d/%d，完成 %d 只", i, len(symbols), done)

        # ④ 统一写盘
        for ym, df in month_bufs.items():
            existing = self.warehouse.read_factor(ym)
            if existing is not None and len(existing):
                selected_codes = set(df["code"].astype(str))
                existing = existing[~existing["code"].astype(str).isin(selected_codes)]
                df = pd.concat([existing, df], ignore_index=True)
            df = df.drop_duplicates(subset=["date", "code"]).sort_values(["date", "code"])
            self.warehouse.write_factor_partition(ym, df)

        elapsed = time.time() - t0
        from StockInvestmentTool.warehouse.pipeline_state import PipelineState
        output_versions = PipelineState(self.warehouse.meta_db_path).record_output_versions(
            dataset_name="factors",
            paths={ym: self.warehouse.factor_dir / f"{ym}.parquet" for ym in month_bufs},
            input_dataset="stock_daily", input_versions=input_versions,
            builder_version="factors_builder.v1", schema_version="factors.v1",
        )
        from StockInvestmentTool.ops.task_center import TaskCenter
        center = TaskCenter(self.warehouse.meta_db_path)
        center.sync_metrics()
        latest_period = max(months) if months else None
        for metric in [item["metric_key"] for item in center.list_metrics() if item["producer_task"] == "factors_build"]:
            center.update_metric_health(metric, latest_period=latest_period, covered_objects=done,
                                        expected_objects=len(symbols), status="healthy" if not failed else "partial",
                                        message="; ".join(failed[:5]),
                                        asset_type_counts=asset_type_counts)
        logger.info("因子计算完成: %d 只, 覆盖 %d 个月, 耗时 %.1fs",
                    done, len(month_bufs), elapsed)
        return {"symbols": done, "months": len(month_bufs), "rows": output_rows,
                "failed": failed[:100], "failed_count": len(failed),
                "skipped": not symbols, "elapsed_sec": round(elapsed, 1),
                "input_dataset": "stock_daily", "input_versions": input_versions,
                "input_fallback_used": not bool(input_versions), "output_versions": output_versions,
                "asset_type_counts": asset_type_counts}
