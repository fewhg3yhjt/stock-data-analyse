# -*- coding: utf-8 -*-
"""初筛主流程 — 股票池 → 板块/名称/股价 → 增强 → 估值规则 → 汇总

每个阶段都记录数量，方便看出哪道规则筛掉最多。
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from StockInvestmentTool.screener import board as board_mod
from StockInvestmentTool.screener import sources
from StockInvestmentTool.screener.rules import ScreenRules

logger = logging.getLogger(__name__)

# 标准快照列
SNAPSHOT_COLS = ["code", "name", "price", "change_pct", "volume", "amount"]
# 增强列（腾讯/东财，可能部分缺失）
ENRICH_COLS = [
    "pe_ttm", "pb", "total_mcap", "float_mcap",
    "turnover", "vol_ratio", "limit_up", "limit_down", "high", "low",
]


@dataclass
class ScreenReport:
    """一次初筛的完整结果与过程统计。"""

    started_at: str
    duration_s: float
    rules: dict
    universe_source: str
    enrich_source: str
    stages: list[tuple[str, int]] = field(default_factory=list)
    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    industry_summary: Optional[pd.DataFrame] = None
    warning: str = ""

    @property
    def matched(self) -> int:
        return len(self.df)


class Screener:
    def __init__(self, rules: Optional[ScreenRules] = None):
        self.rules = rules or ScreenRules()

    # ── 主入口 ───────────────────────────────────────────

    def run(self) -> ScreenReport:
        t0 = time.time()
        report = ScreenReport(
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            duration_s=0.0,
            rules=self.rules.to_dict(),
            universe_source=self.rules.universe_source,
            enrich_source=self.rules.enrich_source,
        )

        # ① 股票池
        df, used = sources.fetch_universe(
            self.rules.universe_source, self.rules.universe_fallback
        )
        report.universe_source = used
        report.stages.append(("股票池", len(df)))

        # ② 板块判定 + 过滤
        df = df.copy()
        df["board"] = df["code"].map(board_mod.detect_board)
        df, n = self._filter_board(df)
        report.stages.append(("板块过滤", n))

        # ③ 名称过滤（ST/关键词）
        df, n = self._filter_name(df)
        report.stages.append(("名称过滤(ST/关键词)", n))

        # ④ 股价过滤
        df, n = self._filter_price(df)
        report.stages.append(("股价过滤", n))

        # ⑤ 增强字段（PE/PB/市值/换手率）
        if self.rules.enrich_enabled:
            df, enrich_used, warn = self._enrich(df)
            report.enrich_source = enrich_used
            if warn:
                report.warning = warn
            report.stages.append((f"增强字段({enrich_used})", len(df)))

        # ⑥ 估值/规模过滤
        df, n = self._filter_valuation(df)
        if self.rules.has_valuation_filters():
            report.stages.append(("估值/规模过滤", n))

        # ⑦ 排序 + 截断
        df = self._sort(df)
        report.stages.append(("初筛命中", len(df)))

        # ⑧ 同花顺行业热度汇总（参考）
        if self.rules.ths_industry_summary:
            try:
                report.industry_summary = sources.ths_industry_summary()
            except Exception as e:
                logger.warning("同花顺行业汇总失败: %s", e)

        report.df = df.reset_index(drop=True)
        report.duration_s = round(time.time() - t0, 2)
        return report

    # ── 通用应用器 ───────────────────────────────────────

    @staticmethod
    def _apply(df: pd.DataFrame, keep_mask: pd.Series) -> tuple[pd.DataFrame, int]:
        out = df[keep_mask.fillna(False)].copy()
        return out, len(out)

    # ── 各阶段过滤器 ─────────────────────────────────────

    def _filter_board(self, df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        r = self.rules
        if r.boards_include:
            keep = df["board"].isin(r.boards_include)
        elif r.boards_exclude:
            keep = df["board"].notna() & ~df["board"].isin(r.boards_exclude)
        else:
            keep = df["board"].notna()
        return self._apply(df, keep)

    def _filter_name(self, df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        r = self.rules
        keep = pd.Series(True, index=df.index)
        if r.exclude_st:
            keep &= ~df["name"].astype(str).str.upper().str.contains("ST", na=False)
        for kw in r.exclude_keywords:
            keep &= ~df["name"].astype(str).str.contains(kw, na=False)
        return self._apply(df, keep)

    def _filter_price(self, df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        r = self.rules
        keep = df["price"].notna()
        if r.price_min is not None:
            keep &= df["price"] >= r.price_min
        if r.price_max is not None:
            keep &= df["price"] <= r.price_max
        return self._apply(df, keep)

    def _enrich(self, df: pd.DataFrame) -> tuple[pd.DataFrame, str, str]:
        """为命中集合补 PE/PB/市值/换手率；主增强源失败自动切备胎。"""
        r = self.rules
        codes = df["code"].tolist()
        for name in (r.enrich_source, r.enrich_fallback):
            if not name or name == "none":
                continue
            try:
                if name == "tencent":
                    extra = sources.tencent_quotes(codes)
                    cols = ["code"] + [c for c in ENRICH_COLS if c in extra.columns]
                    extra = extra[cols]
                elif name == "em":
                    full = sources.em_spot()
                    cols = ["code"] + [c for c in ENRICH_COLS if c in full.columns]
                    extra = full[cols].drop_duplicates("code")
                else:
                    logger.warning("未知增强源: %s", name)
                    continue
                merged = df.merge(extra, on="code", how="left")
                for c in ENRICH_COLS:
                    if c not in merged.columns:
                        merged[c] = None
                logger.info("增强源 %s 成功: %d 行", name, len(extra))
                return merged, name, ""
            except Exception as e:
                logger.warning("增强源 %s 失败(%s: %s)，尝试备胎", name, type(e).__name__, e)
        return df, "none", "增强字段获取失败，PE/PB/市值留空"

    def _filter_valuation(self, df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        r = self.rules
        keep = pd.Series(True, index=df.index)
        for attr, col in (
            ("max_pe_ttm", "pe_ttm"),
            ("max_pb", "pb"),
            ("max_turnover", "turnover"),
        ):
            v = getattr(r, attr)
            if v is not None and col in df.columns:
                keep &= df[col].isna() | (df[col] <= v)
        for attr, col in (("min_total_mcap", "total_mcap"), ("min_float_mcap", "float_mcap")):
            v = getattr(r, attr)
            if v is not None and col in df.columns:
                keep &= df[col].isna() | (df[col] >= v)
        return self._apply(df, keep)

    @staticmethod
    def _sort(df: pd.DataFrame) -> pd.DataFrame:
        if "pe_ttm" in df.columns:
            return df.sort_values("pe_ttm", na_position="last")
        return df.sort_values("code")
