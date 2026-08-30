# -*- coding: utf-8 -*-
"""ScreenExecutor / ConditionCompiler：条件选股。

依据 docs/SCREENING_AND_MARKET_ANALYSIS_DESIGN.md。
- 数据来源：仅 DatasetAccess.load_dataset() 的 stock_daily/indicators
- 全市场筛选不得逐股调用 Python 解释器；混合执行：
    SQL-capable → DuckDB SQL；vectorizable → 向量化；unsupported → 精确评估
- SQL 阶段只产生精确命中集的保守超集：RuleRegistry exact ⊆ SQL candidate set
- 最终命中由 RuleRegistry 精确评估决定
- 每个候选保存 condition_results（命中值/阈值/解释）与 data_as_of
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from StockInvestmentTool.biz.code import normalize
from StockInvestmentTool.biz.models import new_id, now_utc, stable_hash
from StockInvestmentTool.biz.rules import (
    ConditionScope,
    collect_dependencies,
    evaluate_conditions_tree,
)

logger = logging.getLogger(__name__)

# 可直接下推 SQL 的条件类型（保守超集实现第一阶段仅 exact，SQL 优化留待指标宽表成熟）
SQL_CAPABLE_TYPES = {"comparison", "between", "and", "or"}
VECTORIZABLE_TYPES = {"comparison", "between"}


@dataclass
class ScreenDefinition:
    """可复用筛选配置（ScreenVersion 载体）。"""

    screen_id: str
    name: str
    version: str = "1"
    description: str = ""
    asset_types: list = field(default_factory=lambda: ["stock"])
    condition_spec: dict = field(default_factory=dict)
    sort_spec: dict = field(default_factory=dict)   # field/direction/nulls/tie_breaker
    display_fields: list = field(default_factory=list)

    def config_hash(self) -> str:
        return stable_hash({
            "screen_id": self.screen_id, "name": self.name, "version": self.version,
            "asset_types": self.asset_types, "condition_spec": self.condition_spec,
            "sort_spec": self.sort_spec, "display_fields": self.display_fields,
        })


@dataclass
class ScreenCandidate:
    candidate_id: str
    screen_run_id: str
    symbol: str
    name: str = ""
    asset_type: str = "stock"
    industry: str = ""
    rank_no: int | None = None
    score: float | None = None
    matched: int = 1
    condition_results: dict = field(default_factory=dict)
    display_values: dict = field(default_factory=dict)
    data_as_of: str = ""
    expires_at: str | None = None


@dataclass
class ScreenRun:
    run_id: str
    screen_version_id: str
    universe_snapshot_id: str = ""
    run_type: str = "manual"
    requested_as_of: str = ""
    actual_data_as_of: str = ""
    data_context: dict = field(default_factory=dict)
    status: str = "requested"
    matched_count: int = 0
    started_at: str = ""
    finished_at: str = ""
    error: str = ""


class ConditionCompiler:
    """把 ConditionSpec 编译为可执行计划，并处理 SQL 超集约束。

    第一阶段实现：
      - exact evaluation（RuleRegistry）作为唯一最终判定
      - SQL 超集：对 comparison/between 提供"可下推"标记，SQL 优化引擎接入时校验
        `RuleRegistry exact ⊆ SQL candidate set`
    """

    def __init__(self, condition_spec: dict):
        self.condition_spec = condition_spec
        self.dependencies = collect_dependencies(condition_spec)

    def compile_mode(self) -> dict:
        """返回每个条件节点的执行模式标记。"""
        return self._mode(self.condition_spec)

    def _mode(self, node: dict) -> dict:
        ctype = node.get("type", "")
        if ctype in {"and", "or"}:
            return {
                "type": ctype,
                "mode": "exact_only",
                "children": [self._mode(c) for c in node.get("conditions", [])],
            }
        if ctype == "not":
            return {"type": ctype, "mode": "exact_only",
                    "children": [self._mode(node.get("condition", {}))]}
        if ctype in SQL_CAPABLE_TYPES:
            return {"type": ctype, "mode": "conservative_sql"}
        return {"type": ctype, "mode": "exact_only"}


class ScreenExecutor:
    """执行一次筛选：输入已加载的 DataFrame（stock_daily+indicators 合并宽表）。"""

    def __init__(
        self,
        definition: ScreenDefinition,
        df: pd.DataFrame,               # 合并后的全市场宽表（含指标列）
        registry: Any = None,
    ):
        self.definition = definition
        self.df = df.reset_index(drop=True)
        self.registry = registry

    # ── 执行 ──────────────────────────────────────────────

    def execute(self, as_of: str) -> tuple[list[ScreenCandidate], dict]:
        """返回 (candidates, run_meta)。as_of 为请求交易日（YYYY-MM-DD）。"""
        compiler = ConditionCompiler(self.definition.condition_spec)
        modes = compiler.compile_mode()

        # 1. 索引行
        dates = self.df["date"].astype(str).str[:10]
        if "date" in self.df.columns:
            # 目标日：取 <= as_of 的最后一个交易日
            mask = dates <= as_of
            if not mask.any():
                return [], {"status": "failed", "error": f"无 {as_of} 及之前的数据"}
            target_dates = dates[mask]
            last_date = target_dates.iloc[-1]
            target_df = self.df[dates == last_date]
        else:
            target_df = self.df
            last_date = as_of

        # 2. 按 symbol 逐行精确评估（第一阶段：全精确）
        candidates: list[ScreenCandidate] = []
        scope = ConditionScope(target_df, registry=self.registry)

        for idx, row in target_df.iterrows():
            symbol = normalize(str(row["code"])) if "code" in row else ""
            if not symbol:
                continue
            # 构建单行评估上下文：条件树引用字段/指标，需在行维度取值
            # ConditionScope 以整表为上下文，这里对单行构建"值字典"评估
            row_result = self._evaluate_row(target_df, idx, compiler)
            if row_result["passed"]:
                candidates.append(ScreenCandidate(
                    candidate_id=new_id("cand"),
                    screen_run_id="",
                    symbol=symbol,
                    name=str(row.get("name", "")),
                    asset_type=str(row.get("asset_type", "stock")),
                    condition_results=row_result["results"],
                    display_values=self._display_values(row, self.definition.display_fields),
                    data_as_of=last_date,
                ))

        # 3. 排序
        candidates = self._sort(candidates)

        run_meta = {
            "requested_as_of": as_of,
            "actual_data_as_of": last_date,
            "symbol_count": int(target_df["code"].nunique() if "code" in target_df else 0),
            "matched_count": len(candidates),
            "compile_modes": modes,
            "status": "success",
        }
        return candidates, run_meta

    # ── 内部 ──────────────────────────────────────────────

    def _evaluate_row(self, df: pd.DataFrame, idx: int, compiler: ConditionCompiler) -> dict:
        """对单行目标数据评估条件树。

        取该 symbol 的完整历史行，定位到目标日期行位置，
        使 cross/consecutive/count 等序列条件在正确时间窗口内评估。
        """
        symbol = str(df.loc[idx, "code"])
        hist = self.df[self.df["code"] == symbol].reset_index(drop=True)
        if hist.empty:
            return {"passed": False, "results": {}}
        row_date = pd.to_datetime(df.loc[idx, "date"]).strftime("%Y-%m-%d")
        hist_dates = hist["date"].astype(str).str[:10]
        match_idx = hist_dates[hist_dates == row_date]
        if match_idx.empty:
            return {"passed": False, "results": {}}
        row_pos = int(match_idx.index[0])
        hist_scope = ConditionScope(hist, registry=self.registry)
        tree = evaluate_conditions_tree(compiler.condition_spec, hist_scope, at=row_pos)
        return {"passed": tree["passed"], "results": tree}

    def _display_values(self, row: pd.Series, fields: list[str]) -> dict:
        out: dict = {}
        for f in (fields or []):
            if f in row.index:
                v = row[f]
                out[f] = v if not hasattr(v, "item") else v.item()
        return out

    def _sort(self, candidates: list[ScreenCandidate]) -> list[ScreenCandidate]:
        spec = self.definition.sort_spec or {}
        field_name = spec.get("field", "symbol")
        direction = spec.get("direction", "asc")
        reverse = str(direction).lower() == "desc"

        def key_fn(c: ScreenCandidate) -> Any:
            if field_name == "symbol":
                return c.symbol
            if field_name == "score":
                return c.score if c.score is not None else float("-inf")
            return c.symbol

        ordered = sorted(candidates, key=key_fn, reverse=reverse)
        for i, c in enumerate(ordered):
            c.rank_no = i + 1
        return ordered