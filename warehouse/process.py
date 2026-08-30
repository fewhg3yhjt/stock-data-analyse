# -*- coding: utf-8 -*-
"""加工层 — 贴源层 raw/* → 完整宽表 daily/

职责:
  - 从各贴源层源读取原始数据
  - 按 (date, code) 合并多个源的字段，统一单位
  - 输出到加工层 daily/YYYY-MM.parquet（下游筛选/分析的唯一数据源）

字段优先级（同字段多源时，优先级高的源覆盖）:
  baostock（字段最全，含历史PE/PB） > tencent（快、含amount/turn）
  实际按各源覆盖范围合并：取所有源中「非空」的字段。

单位约定（统一到与 baostock 一致）:
  volume: 股
  amount: 元
  turn:   %（换手率）
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from StockInvestmentTool.warehouse.storage import Warehouse

logger = logging.getLogger(__name__)

# 完整宽表标准列
WIDE_COLUMNS = [
    "date", "code", "open", "high", "low", "close",
    "volume", "amount", "turn", "pe_ttm", "pb_mrq", "tradestatus",
]

# 各源提供的字段（用于合并时判断覆盖）
SOURCE_FIELDS = {
    "tencent": ["date", "code", "open", "high", "low", "close",
                "volume", "amount", "turn"],
    "baostock": ["date", "code", "open", "high", "low", "close",
                  "volume", "amount", "turn", "pe_ttm", "pb_mrq", "tradestatus"],
}


class ProcessEngine:
    """贴源层 → 加工层 的合并引擎。"""

    def __init__(self, warehouse: Optional[Warehouse] = None):
        self.warehouse = warehouse or Warehouse()

    # ── 单月合并 ──────────────────────────────────────

    def merge_month(self, month: str, sources: Optional[list[str]] = None) -> pd.DataFrame:
        """合并某月全部可用源 → 完整宽表。

        Args:
            month: YYYY-MM
            sources: 参与合并的源（None=贴源层全部源）

        Returns:
            合并后的宽表 DataFrame
        """
        sources = sources or self.warehouse.raw.sources()
        if not sources:
            return pd.DataFrame()

        frames = []
        for src in sources:
            df = self.warehouse.raw.read(src, month)
            if df is None or df.empty:
                continue
            frames.append(df)
        if not frames:
            return pd.DataFrame()

        # 按 (date, code) 合并，字段取并集，非空优先
        merged = frames[0].copy()
        for df in frames[1:]:
            merged = pd.concat([merged, df], ignore_index=True)
        merged["date"] = pd.to_datetime(merged["date"])
        merged["code"] = merged["code"].astype(str)

        # 按 (date, code) 分组合并：每组的字段值优先取非空
        # 用 groupby + first(non-null) 实现「同键多行 → 合并非空字段」
        grouped = merged.groupby(["date", "code"], as_index=False).agg(
            lambda s: s.dropna().iloc[0] if s.notna().any() else pd.NA
        )
        grouped = grouped.sort_values(["date", "code"]).reset_index(drop=True)
        return grouped

    # ── 全量加工 ──────────────────────────────────────

    def build_all(self, months: Optional[list[str]] = None,
                  sources: Optional[list[str]] = None) -> dict:
        """把贴源层全部月份加工为 daily/ 完整宽表。

        Args:
            months: 指定月份（None=贴源层全部月份并集）
            sources: 参与合并的源（None=全部）

        Returns:
            dict: 各月写入行数
        """
        if months is None:
            src_months = set()
            for src in (sources or self.warehouse.raw.sources()):
                src_months.update(self.warehouse.raw.available_months(src))
            months = sorted(src_months)
        if not months:
            logger.warning("贴源层无数据，无法加工")
            return {}

        result = {}
        for month in months:
            df = self.merge_month(month, sources)
            if df.empty:
                continue
            # 对齐标准列（缺的补 NaN）
            for c in WIDE_COLUMNS:
                if c not in df.columns:
                    df[c] = float("nan")
            df = df[[c for c in WIDE_COLUMNS if c in df.columns]]
            self.warehouse.write_daily_partition(month, df)
            result[month] = len(df)
        logger.info("加工层生成: %d 个月，总 %d 行", len(result), sum(result.values()))
        return result
