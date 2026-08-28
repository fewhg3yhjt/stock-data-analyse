# -*- coding: utf-8 -*-
"""单点回补 — 缺失字段的定向获取与回写

用于「加工层某字段缺失」时，从特定数据源拉取该字段并回写，
不重拉全量、不影响其他字段。

当前支持:
  - 历史估值（PE-TTM / PB）: 东财 stock_value_em（与 baostock 口径一致）

流程:
  ① 从数据源拉取单标的的估值历史 → 标准 DataFrame（date/code/peTTM/pbMRQ）
  ② 写入贴源层 raw/valuation/YYYY-MM.parquet（upsert 语义，幂等）
  ③ 重新加工 raw → daily，把 peTTM/pbMRQ 合并进宽表
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from StockInvestmentTool.warehouse.storage import Warehouse
from StockInvestmentTool.warehouse.process import ProcessEngine
from StockInvestmentTool.warehouse.source_capture import capture_frames

logger = logging.getLogger(__name__)


class ValuationBackfill:
    """历史估值（PE/PB）单点回补器。"""

    def __init__(self, warehouse: Optional[Warehouse] = None):
        self.warehouse = warehouse or Warehouse()

    # ── 数据源：东财历史估值 ──────────────────────────

    @staticmethod
    def fetch_valuation_em(code: str, start: str, end: str) -> pd.DataFrame:
        """从东财拉取单标的的历史估值（PE-TTM/PB）。

        Args:
            code: 无点格式（600900 或 sh600900）
            start/end: YYYY-MM-DD

        Returns:
            DataFrame: date/code/peTTM/pbMRQ（升序），失败抛异常
        """
        import warnings
        warnings.filterwarnings("ignore")
        import akshare as ak

        digits = code.replace("sh", "").replace("sz", "").replace("bj", "").replace(".", "")
        df = ak.stock_value_em(symbol=digits)
        if df is None or df.empty:
            raise RuntimeError(f"东财估值无数据: {code}")

        out = pd.DataFrame({
            "date": pd.to_datetime(df["数据日期"]),
            "peTTM": pd.to_numeric(df["PE(TTM)"], errors="coerce"),
            "pbMRQ": pd.to_numeric(df["市净率"], errors="coerce"),
        })
        out["code"] = code
        out = out[(out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))]
        out = out.dropna(subset=["peTTM", "pbMRQ"], how="all")
        out = out.sort_values("date").drop_duplicates("date")
        return out

    # ── 回补主流程 ───────────────────────────────────

    def backfill(self, code: str, start: str, end: str,
                 reprocess: bool = True) -> dict:
        """回补单标的的 PE/PB，并（可选）重新加工 daily。

        Args:
            code: sh600900 格式
            start/end: YYYY-MM-DD
            reprocess: True 时回补后重新加工 daily（把 PE/PB 并入宽表）

        Returns:
            dict: 回补行数 / 覆盖月份
        """
        df = self.fetch_valuation_em(code, start, end)
        if df.empty:
            return {"rows": 0, "months": []}

        # 保留旧的按月估值回补路径，同时留下不可覆盖的来源批次证据。
        raw = capture_frames(
            self.warehouse, dataset_name="valuation_daily", source_name="eastmoney",
            frames=[df], run_date=datetime.now().strftime("%Y-%m-%d"),
            trade_date_start=start, trade_date_end=end, expected_symbols=1,
            success_symbols=1, universe_id=f"valuation_{datetime.now():%Y%m%d}",
            request_context={"code": code, "start": start, "end": end},
        )

        # 写入贴源层 raw/valuation/（按月份拆分，upsert）
        months = []
        for ym, grp in df.groupby(df["date"].dt.strftime("%Y-%m")):
            self.warehouse.raw.upsert_rows("valuation", grp)
            months.append(ym)
        logger.info("估值回补 %s: %d 行, 覆盖 %d 个月", code, len(df), len(months))

        # 重新加工 daily（把 raw/valuation 的 PE/PB 并入宽表）
        if reprocess:
            pe = ProcessEngine(self.warehouse)
            result = pe.build_all(months=sorted(set(months)))
            logger.info("估值回补后加工完成: %s", result)

        return {"rows": len(df), "months": sorted(set(months)), "raw_batch_id": raw["batch_id"]}

    def backfill_many(self, codes: list[str], start: str, end: str,
                      reprocess: bool = True) -> dict:
        """批量回补多只股票的 PE/PB。"""
        total = 0
        failed: list[str] = []
        for code in codes:
            try:
                r = self.backfill(code, start, end, reprocess=False)
                total += r["rows"]
            except Exception as e:
                failed.append(code)
                logger.warning("回补 %s 失败: %s", code, e)
        if reprocess and total:
            pe = ProcessEngine(self.warehouse)
            pe.build_all()
        return {"rows": total, "failed": failed}
