# -*- coding: utf-8 -*-
"""初筛规则 — 数据模型 + YAML 加载 + CLI 覆盖

优先级: 内置默认值 < screen_rules.yaml < 命令行参数
"""

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

RULES_FILE = Path(__file__).resolve().parent / "screen_rules.yaml"


@dataclass
class ScreenRules:
    # ── 股票池 ─────────────────────────────────────────
    universe_source: str = "sina"       # sina | em | tencent | baostock
    universe_fallback: str = "em"       # 主源失败时的备胎（None=不降级）

    # ── 板块（include 与 exclude 二选一，include 优先）──
    boards_include: Optional[list[str]] = None   # 如 ["main_sh","main_sz"]
    boards_exclude: Optional[list[str]] = None   # 如 ["cyb","kcb","bse"]

    # ── 名称 ───────────────────────────────────────────
    exclude_st: bool = True             # 剔除 ST/*ST
    exclude_keywords: list[str] = field(default_factory=list)  # 名称含任一关键词剔除

    # ── 股价（元）──────────────────────────────────────
    price_min: Optional[float] = None
    price_max: Optional[float] = 35.0

    # ── 增强字段（PE/PB/市值/换手率，来源腾讯批量报价）──
    enrich_enabled: bool = True
    enrich_source: str = "tencent"      # tencent | em | none
    enrich_fallback: str = "em"         # 增强失败时的备胎

    # ── 估值/规模过滤（依赖增强字段）──────────────────
    max_pe_ttm: Optional[float] = None  # 动态PE上限
    max_pb: Optional[float] = None      # 市净率上限
    min_total_mcap: Optional[float] = None  # 总市值下限（亿元）
    min_float_mcap: Optional[float] = None  # 流通市值下限（亿元）
    max_turnover: Optional[float] = None    # 换手率上限（%）

    # ── 输出 ───────────────────────────────────────────
    output_dir: Optional[str] = None    # None → output/screener
    output_formats: list[str] = field(default_factory=lambda: ["csv", "json"])
    max_rows: int = 1000                # 导出条数上限

    # ── 附注 ───────────────────────────────────────────
    ths_industry_summary: bool = True   # 附导同花顺行业热度汇总（仅参考）

    # ── 加载 ───────────────────────────────────────────

    @classmethod
    def from_yaml(cls, path: Optional[Path | str] = None) -> "ScreenRules":
        """从 YAML 加载规则（缺省用包内 screen_rules.yaml）。"""
        path = Path(path) if path else RULES_FILE
        rules = cls()
        if not path.exists():
            return rules
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        uni = data.get("universe") or {}
        rules.universe_source = uni.get("source", rules.universe_source)
        rules.universe_fallback = uni.get("fallback", rules.universe_fallback)

        boards = data.get("boards") or {}
        rules.boards_include = boards.get("include")
        rules.boards_exclude = boards.get("exclude")

        nf = data.get("name_filter") or {}
        rules.exclude_st = nf.get("exclude_st", rules.exclude_st)
        rules.exclude_keywords = nf.get("exclude_keywords", rules.exclude_keywords)

        price = data.get("price") or {}
        rules.price_min = price.get("min", rules.price_min)
        rules.price_max = price.get("max", rules.price_max)

        en = data.get("enrich") or {}
        rules.enrich_enabled = en.get("enabled", rules.enrich_enabled)
        rules.enrich_source = en.get("source", rules.enrich_source)
        rules.enrich_fallback = en.get("fallback", rules.enrich_fallback)

        val = data.get("valuation") or {}
        rules.max_pe_ttm = val.get("max_pe_ttm", rules.max_pe_ttm)
        rules.max_pb = val.get("max_pb", rules.max_pb)
        rules.min_total_mcap = val.get("min_total_mcap", rules.min_total_mcap)
        rules.min_float_mcap = val.get("min_float_mcap", rules.min_float_mcap)
        rules.max_turnover = val.get("max_turnover", rules.max_turnover)

        out = data.get("output") or {}
        rules.output_dir = out.get("dir", rules.output_dir)
        rules.output_formats = out.get("formats", rules.output_formats)
        rules.max_rows = out.get("max_rows", rules.max_rows)

        ann = data.get("annotate") or {}
        rules.ths_industry_summary = ann.get("industry_summary_ths", rules.ths_industry_summary)
        return rules

    def has_valuation_filters(self) -> bool:
        """是否配置了任意估值/规模过滤（决定阶段是否单独上报）。"""
        return any(
            v is not None
            for v in (
                self.max_pe_ttm, self.max_pb,
                self.min_total_mcap, self.min_float_mcap,
                self.max_turnover,
            )
        )

    # ── 序列化（用于导出时存档）──────────────────────

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)
