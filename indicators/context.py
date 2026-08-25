# -*- coding: utf-8 -*-
"""统一指标求值入口（FR-1.3）

设计意图（HLD §3.3 / 解释器模式 + 统一上下文，消除「多 context 类」新重复）：
  - 全库**唯一**指标求值入口 IndicatorContext，围绕一次行情计算构建；
  - 支撑位来源（MaSource/RollingLowSource/IndicatorExprSource）、规则 executor 的
    RuleContext、advisor 都从同一口井打水；
  - 既支持「整表计算」（compute_all），也支持「按指标名/表达式单点取值」；
  - 老 yaml 写死字段名（ma_60 / low_3m / year_low / dividend_anchor）由
    `strategy/support.py` 的 `SUPPORT_SOURCE_FACTORY` 兼容映射，存量方案零迁移。

数据边界（HLD ADR-6）：IndicatorContext 只读**原始行情**列 + 已配置指标，
不引入数据源差异。行情列由上游（DataSource 返回的原始列）提供。
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 表达式中可直接引用的原始行情列白名单
_RAW_COLUMNS = ("open", "high", "low", "close", "volume", "amount",
                "peTTM", "pbMRQ", "turn", "pct_chg")


class IndicatorContext:
    """围绕一次行情计算构建的统一指标求值入口。

    用法：
        ctx = IndicatorContext(df)          # df: 原始行情 DataFrame
        ctx["MA20"]                         # 按指标名取当前（末行）值
        ctx.eval("0.95*MA20")               # 表达式求值
        ctx.ma(60)                          # 便捷原子：MA60
        ctx.rolling_low(63)                 # 便捷原子：近63日最低

    row 参数：默认取 df 末行（最新值）；回测中可指定 i 以取某行。
    """

    def __init__(
        self,
        df: Optional[pd.DataFrame] = None,
        row: Optional[pd.Series] = None,
        registry=None,
        row_index: int = -1,
    ):
        from StockInvestmentTool.indicators.engine import IndicatorRegistry

        self._registry = registry or IndicatorRegistry()
        self._df = df
        self.row = row
        self._row_index = row_index
        self._series_cache: dict[str, pd.Series] = {}
        self._raw_env: dict[str, pd.Series] = {}

    # ── 数据装载 ──────────────────────────────────────────

    def _compute_series(self, name: str) -> pd.Series:
        """计算某个指标名的完整 Series（按需 + 缓存）。"""
        if name in self._series_cache:
            return self._series_cache[name]
        if self._df is None:
            raise ValueError("IndicatorContext 缺少 df，无法计算指标")

        # 原始列直接可用
        series = self._registry.compute(self._df, [name])
        if name in series:
            self._series_cache[name] = series[name]
        else:
            # 退化为原始列（如 close）
            if name in self._df.columns:
                self._series_cache[name] = self._df[name]
            else:
                raise ValueError(f"指标/列不存在: {name}")
        return self._series_cache[name]

    def _resolve(self, name: str) -> float:
        """解析一个名称的当前值（供 __getitem__ 与表达式环境）。"""
        s = self._compute_series(name)
        idx = self._row_index if self._row_index >= 0 else len(s) - 1
        vals = s.dropna()
        if len(vals) == 0:
            return 0.0
        if idx >= len(s):
            idx = len(s) - 1
        v = s.iloc[idx]
        if v is None or pd.isna(v):
            return 0.0
        return float(v)

    # ── 表达式求值 ────────────────────────────────────────

    def eval(self, expr: str) -> float:
        """求值任意表达式（含引用指标名 / 原始列 / 白名单函数）。

        例如 "0.95*MA20"、"MIN(MA20,MA240)"、"0.95*MIN(MA20,MA240)"。
        表达式错误时抛 ValueError，含指标名以便排查（SRD FR-1.3 验收）。
        """
        expr = (expr or "").strip()
        if not expr:
            return 0.0
        if "__" in expr:
            raise ValueError(f"表达式含非法字符: {expr}")

        # 纯数值
        try:
            return float(expr)
        except (ValueError, TypeError):
            pass

        # 先尝试按「精确指标名」（引用一个指标，无运算）
        if self._is_simple_ref(expr):
            return self._resolve(expr)

        # 构建求值环境：原始列 + 已算指标
        env = self._build_env()
        try:
            val = eval(expr, {"__builtins__": {}}, env)  # noqa: S307
        except Exception as e:
            raise ValueError(f"指标表达式求值失败 '{expr}': {e}") from e

        # 序列 → 取当前值
        if isinstance(val, pd.Series):
            idx = self._row_index if self._row_index >= 0 else len(val) - 1
            v = val.iloc[idx]
            return float(v) if v is not None and not pd.isna(v) else 0.0
        return float(val)

    def _is_simple_ref(self, expr: str) -> bool:
        """是否为纯名称引用（无运算符），绕开 eval 直接走指标解析。"""
        return bool(re_expr_full.match(expr)) or expr in _RAW_COLUMNS

    def _build_env(self) -> dict:
        """构建表达式求值环境（含白名单函数 + 已解析指标/原始列）。"""
        from StockInvestmentTool.indicators.engine import SAFE_FUNCS

        env = dict(SAFE_FUNCS)
        if self._df is None:
            return env

        # 原始列
        for c in self._df.columns:
            if c in _RAW_COLUMNS or c.startswith("ma_") or c in ("low_3m", "year_low", "bias_ratio"):
                env[c] = self._df[c]
        env.setdefault("pct_chg", self._df["close"].pct_change() * 100 if "close" in self._df else pd.Series(dtype=float))

        # 已配置指标（基础/组合/代码）
        try:
            computed = self._registry.compute(self._df)
            for name, s in computed.items():
                env.setdefault(name, s)
                self._series_cache.setdefault(name, s)
        except Exception as e:
            logger.debug("指标计算失败，部分表达式可能不可用: %s", e)
        return env

    # ── 便捷原子 ──────────────────────────────────────────

    def __getitem__(self, name: str) -> float:
        return self._resolve(name)

    def ma(self, window: int) -> float:
        """MA(window) 当前值（任意窗口）。"""
        if self._df is None or "close" not in self._df.columns:
            return 0.0
        # 优先走已配置指标（如 MA60）；任意窗口回退到 rolling 计算
        try:
            return self._resolve(f"MA{window}")
        except Exception:
            idx = self._row_index if self._row_index >= 0 else len(self._df) - 1
            s = self._df["close"].iloc[: idx + 1].rolling(int(window)).mean()
            v = s.dropna()
            return float(v.iloc[-1]) if len(v) else 0.0

    def rolling_low(self, window: Optional[int] = None) -> float:
        """近 N 日（默认全部）最低价。"""
        if self._df is None or "low" not in self._df.columns:
            return 0.0
        col = self._df["low"]
        if window:
            col = col.tail(window)
        v = col.min()
        return float(v) if v is not None and not pd.isna(v) else 0.0

    def year_high(self) -> float:
        """近 12 月（252 日，min_periods=60）最高价 —— 与回测同一口径。"""
        if self._df is None or "high" not in self._df.columns:
            return 0.0
        idx = self._row_index if self._row_index >= 0 else len(self._df) - 1
        window = self._df["high"].iloc[: idx + 1].tail(252)
        v = window.max()
        return float(v) if v is not None and not pd.isna(v) else 0.0

    def _eval_series(self, expr: str) -> pd.Series:
        raise NotImplementedError("改由 eval 单点求值替代")

    def _series_value(self, s: pd.Series) -> float:
        if s is None or len(s) == 0:
            return 0.0
        v = s.dropna()
        if len(v) == 0:
            return 0.0
        return float(v.iloc[-1])

    # ── 批量取值 ──────────────────────────────────────────

    def latest(self, names: Optional[list[str]] = None) -> dict:
        """计算一批指标的最新值（dict）。"""
        from StockInvestmentTool.indicators.engine import IndicatorRegistry
        reg = self._registry or IndicatorRegistry()
        series = reg.compute(self._df, names)
        out = {}
        for name, s in series.items():
            v = s.dropna()
            out[name] = round(float(v.iloc[-1]), 4) if len(v) else None
        return out


import re  # noqa: E402  (放置在函数化使用处之后，便于阅读)
re_expr_full = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


__all__ = ["IndicatorContext"]
