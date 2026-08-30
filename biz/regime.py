# -*- coding: utf-8 -*-
"""MarketRegimeService：统一市场状态事实。

依据 docs/DOMAIN_MODEL_AND_CONTRACTS.md §4.5。
- MarketRegime 由业务平面的 MarketRegimeService 生产，不由数据采集任务或各研究请求私算
- 输入：DatasetResult(stock_daily/indicators)
- 结果写入 business.db.market_regimes
- 研究、选股、Simulation、LiveAdvice 使用同一 MarketRegime 记录
- 指定历史 as_of 无记录时，先按同一算法版本计算并持久化
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from StockInvestmentTool.biz.models import new_id, now_utc, stable_hash

logger = logging.getLogger(__name__)

REGIMES = {"strong_bull", "overbought_bull", "weak_bull", "range", "weak_bear", "strong_bear"}
ALGORITHM_VERSION = "market_regime.v1"


@dataclass
class MarketRegime:
    regime_id: str
    regime: str
    as_of: str
    confidence: float | None = None
    algorithm_version: str = ALGORITHM_VERSION
    input_snapshot: dict = field(default_factory=dict)
    explanation: str = ""
    data_context: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_utc)

    def to_dict(self) -> dict:
        return {
            "regime_id": self.regime_id,
            "regime": self.regime,
            "as_of": self.as_of,
            "confidence": self.confidence,
            "algorithm_version": self.algorithm_version,
            "input_snapshot": self.input_snapshot,
            "explanation": self.explanation,
            "data_context": self.data_context,
        }


class MarketRegimeService:
    """基于已发布指数/全市场数据计算市场状态。"""

    def __init__(self, df: pd.DataFrame | None = None, context: dict | None = None):
        self.df = df
        self.context = context or {}

    # ── 计算 ──────────────────────────────────────────────

    def compute(self, as_of: str) -> MarketRegime:
        """基于注入的指数行情 DataFrame 计算市场状态。"""
        if self.df is None or self.df.empty:
            return MarketRegime(
                regime_id=new_id("regime"), regime="range", as_of=as_of,
                confidence=None, explanation="无市场指数数据，默认 range",
            )

        close = self.df["close"].astype(float)
        # 窗口指标
        ma20 = close.rolling(20, min_periods=1).mean()
        ma60 = close.rolling(60, min_periods=1).mean()
        cur = float(close.iloc[-1])
        cur_ma20 = float(ma20.iloc[-1]) if pd.notna(ma20.iloc[-1]) else cur
        cur_ma60 = float(ma60.iloc[-1]) if pd.notna(ma60.iloc[-1]) else cur

        # 动量：近 20 日收益
        ret20 = (cur - float(close.iloc[-21])) / float(close.iloc[-21]) if len(close) > 21 and float(close.iloc[-21]) != 0 else 0.0

        # 规则：价格相对 MA20/MA60 位置 + 动量
        if cur > cur_ma20 and cur > cur_ma60:
            if ret20 > 0.05:
                regime = "strong_bull"
            elif ret20 > 0:
                regime = "weak_bull"
            else:
                regime = "range"
        elif cur > cur_ma60:
            regime = "weak_bull" if ret20 > 0 else "range"
        elif cur < cur_ma20 and cur < cur_ma60:
            if ret20 < -0.05:
                regime = "strong_bear"
            elif ret20 < 0:
                regime = "weak_bear"
            else:
                regime = "range"
        elif cur < cur_ma60:
            regime = "weak_bear"
        else:
            regime = "range"

        # 过热判断
        if regime == "strong_bull" and ret20 > 0.10:
            regime = "overbought_bull"

        return MarketRegime(
            regime_id=new_id("regime"),
            regime=regime,
            as_of=as_of,
            confidence=self._confidence(ret20, cur_ma20, cur_ma60),
            input_snapshot={
                "price": cur, "ma20": cur_ma20, "ma60": cur_ma60,
                "ret20": ret20, "algorithm_version": ALGORITHM_VERSION,
            },
            explanation=f"价格 {cur:.4g} 相对 MA20 {cur_ma20:.4g} / MA60 {cur_ma60:.4g}，20日动量 {ret20:.2%}",
            data_context=self.context,
        )

    def _confidence(self, ret20: float, ma20: float, ma60: float) -> float:
        """置信度：动量越强、均线差距越大，置信越高。"""
        conf = min(abs(ret20) * 5, 1.0) * 0.6
        if ma20 and ma60:
            spread = abs(ma20 - ma60) / ma60 if ma60 != 0 else 0.0
            conf += min(spread * 3, 1.0) * 0.4
        return round(min(conf, 1.0), 3)

    def fingerprint(self) -> str:
        """算法版本 + 输入数据范围的稳定指纹，用于入库去重。"""
        return stable_hash({"algorithm": ALGORITHM_VERSION})

    @staticmethod
    def valid_regime(regime: str) -> bool:
        return regime in REGIMES