# -*- coding: utf-8 -*-
"""贴源层（ODS）— 各接口原始数据独立存放，互不覆盖

设计目标:
  - 每个数据源一个独立目录，原始数据「原样落地」，不合并、不改写
  - 源与源之间绝不互相覆盖（腾讯的不会冲掉 baostock 的，反之亦然）
  - 加工层（warehouse/daily）从贴源层读取并合并生成「完整宽表」
  - 支持单标的、单字段的重新获取与回写（不重拉全量）

布局:
  warehouse/
  ├── raw/
  │   ├── tencent/YYYY-MM.parquet    腾讯K线源（OHLCV+amount+turn）
  │   ├── baostock/YYYY-MM.parquet   baostock源（含历史PE/PB，待接入）
  │   └── valuation/YYYY-MM.parquet  估值源（历史PE/PB，单点回补）
  ├── daily/YYYY-MM.parquet          加工层：完整宽表（下游唯一数据源）
  └── meta.db
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 各源的标准列（源原始字段，尽量保持原样）
# tencent: 腾讯 proxy newfqkline（OHLCV + 换手 + 成交额）
TENCENT_COLUMNS = [
    "date", "code", "open", "high", "low", "close",
    "volume", "amount", "turn",
]
# baostock: 完整字段（历史 PE/PB）
BAOSTOCK_COLUMNS = [
    "date", "code", "open", "high", "low", "close",
    "volume", "amount", "turn", "peTTM", "pbMRQ", "tradestatus",
]


class RawStore:
    """贴源层存储：按源 + 月分区读写原始数据。

    Args:
        base_dir: 仓库根目录（通常 = Warehouse.base_dir）
    """

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.raw_dir = self.base_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def source_dir(self, source: str) -> Path:
        """某源的目录（自动创建）"""
        d = self.raw_dir / source
        d.mkdir(parents=True, exist_ok=True)
        return d

    def partition_path(self, source: str, month: str) -> Path:
        return self.source_dir(source) / f"{month}.parquet"

    # ── 写入 ─────────────────────────────────────────

    def write(self, source: str, month: str, df: pd.DataFrame,
              overwrite: bool = False) -> int:
        """把某源某月的数据写入贴源层分区。

        默认不覆盖（append 语义）: 已有分区则合并该标的的新数据。
        相同 date+code 只保留一份（幂等）。

        Args:
            source: 数据源名（tencent/baostock/valuation/...）
            month: YYYY-MM
            df: 需含 date/code 列
            overwrite: True 时整体覆写该分区

        Returns:
            写入后的行数
        """
        path = self.partition_path(source, month)
        if overwrite:
            df = df.copy()
        else:
            existing = self.read(source, month)
            if existing is not None and len(existing):
                df = pd.concat([existing, df], ignore_index=True)
            df = df.copy()
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
        if "code" in df.columns:
            df["code"] = df["code"].astype(str)
        df = df.drop_duplicates(subset=["date", "code"])
        df = df.sort_values(["date", "code"])
        try:
            df.to_parquet(path, index=False, engine="pyarrow", compression="snappy")
        except ImportError:
            df.to_parquet(path, index=False, compression="snappy")
        logger.info("贴源层写入 %s/%s: %d 行", source, path.name, len(df))
        return len(df)

    # ── 读取 ─────────────────────────────────────────

    def read(self, source: str, month: str) -> Optional[pd.DataFrame]:
        """读某源某月分区，不存在返回 None"""
        path = self.partition_path(source, month)
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"])
            return df
        except Exception as e:
            logger.warning("贴源层读取失败 %s: %s", path, e)
            return None

    def available_months(self, source: str) -> list[str]:
        return sorted(p.stem for p in self.source_dir(source).glob("*.parquet"))

    def sources(self) -> list[str]:
        """已存在的贴源目录名"""
        if not self.raw_dir.exists():
            return []
        return sorted(d.name for d in self.raw_dir.iterdir() if d.is_dir())

    # ── 单标的/单字段回补 ─────────────────────────────

    def upsert_rows(self, source: str, df: pd.DataFrame) -> int:
        """把某标的的（部分字段）数据回补进对应月份分区。

        用于「缺失字段单点回补」: 只更新传入的 date+code 对应行的字段，
        其余字段保留不动。按月份拆分写入。

        Args:
            source: 数据源名
            df: 含 date/code + 待回补字段

        Returns:
            更新的行数
        """
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df["code"] = df["code"].astype(str)
        total = 0
        for ym, grp in df.groupby(df["date"].dt.strftime("%Y-%m")):
            path = self.partition_path(source, ym)
            existing = self.read(source, ym)
            new_idx = grp.set_index(["date", "code"])
            if existing is None:
                new = grp.copy()
            else:
                # 合并索引：existing ∪ new（new 的行可能不存在于 existing）
                idx = existing.set_index(["date", "code"])
                # 补上分区没有但回补数据含有的列
                for col in new_idx.columns:
                    if col not in idx.columns:
                        idx[col] = pd.NA
                # 用 reindex 扩展索引到并集，再覆盖回补列的对应行
                all_index = idx.index.union(new_idx.index)
                idx = idx.reindex(all_index)
                update_cols = [c for c in new_idx.columns if c in idx.columns]
                for col in update_cols:
                    idx.loc[new_idx.index, col] = new_idx[col]
                new = idx.reset_index()
            new = new.drop_duplicates(subset=["date", "code"])
            new = new.sort_values(["date", "code"])
            new.to_parquet(path, index=False, engine="pyarrow", compression="snappy")
            total += len(new_idx)
        logger.info("贴源层回补 %s: %d 行", source, total)
        return total