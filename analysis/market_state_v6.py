"""V6.0 市场状态判定器 — 六态市场状态（第二编第五步 5.2）

对齐《四维一体实战投资体系 V6.0》：

  市场状态      判定条件                                            仓位乘数  唯一适用的买入策略
  强多头        MA5>MA20>MA60 且 股价>MA250 且 MA250向上           1.0×      规则C：右侧趋势跟随
  强多头(超买)  强多头条件 + 乖离率>20%                            自动降级   降级为谨慎多头，切回规则A（等回调）
  弱多头        均线多头 但 股价<MA250                            0.7×      规则A：左侧挂单等待
  震荡市        均线粘合缠绕，股价围绕MA250反复                    1.0×      规则A：左侧挂单等待
  弱空头        均线空头 但 股价>MA250                            0.5×      禁止操作或极小仓位
  强空头        均线空头 且 股价<MA250 且 MA250向下               0×        禁止操作（空仓等待）

MA250 方向量化复用 indicators.ma_slope（今日 vs 5日前，±0.5% 走平带）。
乖离率 bias_ratio = (close − MA250)/MA250×100（indicators 已实现）。
"""

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from StockInvestmentTool.datasource.indicators import TechnicalIndicators

# 市场状态常量
STRONG_BULL = "强多头"
STRONG_BULL_OVERBOUGHT = "强多头（超买）"
WEAK_BULL = "弱多头"
SIDEWAYS = "震荡市"
WEAK_BEAR = "弱空头"
STRONG_BEAR = "强空头"

# 仓位乘数（与方案 §5.2 表一致）
STATE_MULTIPLIER = {
    STRONG_BULL: 1.0,
    STRONG_BULL_OVERBOUGHT: 0.0,   # 超买→自动降级，无买入资格（切规则A等待回调）
    WEAK_BULL: 0.7,
    SIDEWAYS: 1.0,
    WEAK_BEAR: 0.5,
    STRONG_BEAR: 0.0,
}

# 乖离率超买阈值（>20% 触发强多头降级）
OVERBOUGHT_BIAS = 20.0


@dataclass
class MarketStateResult:
    state: str
    multiplier: float
    rule: str                       # 唯一适用的买入规则：规则C / 规则A / 禁止操作
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "multiplier": self.multiplier,
            "rule": self.rule,
            "detail": self.detail,
        }


def _is_bull_alignment(df: pd.DataFrame, last) -> bool:
    """多头排列：MA5 > MA20 > MA60（容忍 NaN 视为不满足）"""
    try:
        return last["ma5"] > last["ma20"] > last["ma60"]
    except (KeyError, TypeError):
        return False


def _is_bear_alignment(df: pd.DataFrame, last) -> bool:
    """空头排列：MA5 < MA20 < MA60"""
    try:
        return last["ma5"] < last["ma20"] < last["ma60"]
    except (KeyError, TypeError):
        return False


def judge_market_state(df: pd.DataFrame) -> MarketStateResult:
    """V6.0 六态市场状态判定（纯函数，输入需含 ma5/20/60/250 + close + bias_ratio）

    Parameters
    ----------
    df : pd.DataFrame
        compute_all 后的 K 线（含 ma5/ma20/ma60/ma240/bias_ratio）。
        若缺 ma240（不足1年数据），返回基于均线排列的近似状态并标注 data_insufficient。

    Returns
    -------
    MarketStateResult
        state / multiplier / rule / detail（含各判定输入值，便于复盘）
    """
    if df is None or df.empty:
        return MarketStateResult(
            state="数据不足", multiplier=0.0, rule="禁止操作",
            detail={"reason": "无数据"},
        )

    last = df.iloc[-1]
    ma240_missing = pd.isna(last.get("ma240", float("nan")))
    bias = last.get("bias_ratio", float("nan"))
    if pd.isna(bias):
        bias = None

    bull_align = _is_bull_alignment(df, last)
    bear_align = _is_bear_alignment(df, last)
    price = last.get("close", float("nan"))
    ma240 = last.get("ma240", float("nan"))
    price_above_ma240 = (not ma240_missing) and price > ma240

    # MA250 方向（强多头/强空头需要）
    ma240_dir = None
    if not ma240_missing:
        slope = TechnicalIndicators.ma_slope(df, "ma240")
        ma240_dir = slope  # "向上"/"走平"/"向下"/"数据不足"

    detail = {
        "close": float(price) if not pd.isna(price) else None,
        "ma5": float(last["ma5"]) if not pd.isna(last.get("ma5", float("nan"))) else None,
        "ma20": float(last["ma20"]) if not pd.isna(last.get("ma20", float("nan"))) else None,
        "ma60": float(last["ma60"]) if not pd.isna(last.get("ma60", float("nan"))) else None,
        "ma240": float(ma240) if not ma240_missing else None,
        "bias_ratio": bias,
        "ma240_direction": ma240_dir,
        "bull_alignment": bull_align,
        "bear_alignment": bear_align,
        "price_above_ma240": price_above_ma240,
    }

    # ── 强多头：均线多头 + 股价>MA250 + MA250向上 ──
    if bull_align and price_above_ma240 and ma240_dir == "向上":
        if bias is not None and bias > OVERBOUGHT_BIAS:
            state = STRONG_BULL_OVERBOUGHT
            rule = "规则A（等回调）"
            detail["reason"] = f"强多头但乖离率 {bias:.1f}% > {OVERBOUGHT_BIAS}%，自动降级"
        else:
            state = STRONG_BULL
            rule = "规则C（右侧趋势跟随）"
            detail["reason"] = "均线多头 + 站上MA250 + MA250向上"
        return MarketStateResult(state, STATE_MULTIPLIER[state], rule, detail)

    # ── 弱多头：均线多头 但 股价<MA250 ──
    if bull_align and not price_above_ma240:
        state = WEAK_BULL
        return MarketStateResult(
            state, STATE_MULTIPLIER[state], "规则A（左侧挂单等待）",
            {**detail, "reason": "均线多头但股价在MA250下方（年线下方）"},
        )

    # ── 弱空头：均线空头 但 股价>MA250 ──
    if bear_align and price_above_ma240:
        state = WEAK_BEAR
        return MarketStateResult(
            state, STATE_MULTIPLIER[state], "禁止操作或极小仓位",
            {**detail, "reason": "均线空头但股价仍在MA250上方（震荡偏空）"},
        )

    # ── 强空头：均线空头 + 股价<MA250 + MA250向下/方向数据不足 ──
    # 边界 bar（ma240 刚满窗、斜率需再等 5 天）ma_slope 返回"数据不足"，
    # 此时空头排列+跌破年线已是明确空头形态，若放行会误落震荡市→允许
    # 规则A买入，违背"排除比预测重要、不误买"原则，故按强空头处理。
    # （MA250 走平仍归震荡市，避免把横盘小幅回踩误判为空头）
    if bear_align and not price_above_ma240 and ma240_dir in ("向下", "数据不足"):
        state = STRONG_BEAR
        return MarketStateResult(
            state, STATE_MULTIPLIER[state], "禁止操作（空仓等待）",
            {**detail, "reason": "均线空头 + MA250下方 + MA250向下（或方向数据不足）"},
        )

    # ── 震荡市：均线粘合（非多头非空头），其余情况 ──
    state = SIDEWAYS
    return MarketStateResult(
        state, STATE_MULTIPLIER[state], "规则A（左侧挂单等待）",
        {**detail, "reason": "均线粘合缠绕（非多头非空头）"},
    )
