"""V6.0 宏观刹车片 — 总仓位上限控制（第一编）

对齐《四维一体实战投资体系 V6.0》：

第一章 股债收益差 → 总仓位上限
  股债收益差 = 沪深300 PE倒数 − 10Y国债收益率
  > 5.5%        极度低估 → 上限100%（软参考，不强制满仓）
  4.0%~5.5%     中性偏低 → 70%~80%
  2.5%~4.0%     中性偏高 → 50%~60%
  < 2.5%        极度高估 → ≤30%（硬约束，必须遵守）

第二章 大盘年线熔断（上位补充，优先级高于股债收益差）
  沪深300 收盘价跌破 MA250 → 触发预警
  连续3个交易日收于 MA250 下方 → 强制总仓位上限 ≤30%
  例外: 若股债收益差 >5.5%（极度低估）→ 暂缓熔断，改观察期：
       不强制降仓，但禁止新开仓，直至指数重回年线上方

第三章 宏观估值调节系数
  利率调节系数 = 中值(0.8, 1+(3.0%−当前10Y国债)×0.2, 1.2)  （以速查表为准，
    公式正文 ×0.4 为文档笔误: 2.0%→1.2, 2.5%→1.1, 3.0%→1.0, 3.5%→0.9, 4.0%→0.8）
  M2同比 → PEG上限/股息率要求: >10%→PEG1.0/股息率2.5%；8-10%→0.8/3.0%；<8%→0.6/3.5%

优先级: 熔断 > 股债收益差（熔断例外条款再考虑收益差）。
"""

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from StockInvestmentTool.datasource.indicators import TechnicalIndicators

# 股债收益差档位
ERP_BANDS = [
    (None, 0.025, "极度高估", 0.30),      # < 2.5% → ≤30% 硬约束
    (0.025, 0.04, "中性偏高", 0.60),      # 2.5%~4.0% → 50%~60%
    (0.04, 0.055, "中性偏低", 0.80),      # 4.0%~5.5% → 70%~80%
    (0.055, None, "极度低估", 1.00),      # > 5.5% → 100% 软参考
]
ERP_HARD_LOW = 0.025    # 硬约束阈值
ERP_EXCEPTION_HIGH = 0.055  # 熔断例外阈值
FUSE_DAYS = 3           # 连续收于年线下天数


@dataclass
class MacroBrakeResult:
    """宏观刹车片综合判定结果"""
    max_position_pct: float            # 总仓位上限（占总账户 %）
    market_level: str                  # 股债收益差水位
    fuse_state: str                    # 熔断状态: 未触发/预警/已熔断/观察期
    rate_factor: float                 # 利率调节系数
    m2_level: str                      # M2 流动性档位
    peg_cap: Optional[float]           # 流动性调节后 PEG 上限
    dividend_req: Optional[float]      # 流动性调节后股息率要求 %
    erp: Optional[float]               # 股债收益差（小数）
    notes: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "max_position_pct": self.max_position_pct,
            "market_level": self.market_level,
            "fuse_state": self.fuse_state,
            "rate_factor": self.rate_factor,
            "m2_level": self.m2_level,
            "peg_cap": self.peg_cap,
            "dividend_req": self.dividend_req,
            "erp": self.erp,
            "notes": self.notes,
            "detail": self.detail,
        }


def judge_fuse(csi300_kline: Optional[pd.DataFrame]) -> str:
    """沪深300 年线熔断状态（纯函数）。

    Parameters
    ----------
    csi300_kline : compute_all 后的沪深300 日K线（含 ma_250）。
        None/缺 ma_250 → "未触发"（数据不足，不误杀）。

    Returns
    -------
    str: "未触发" / "预警" / "已熔断" / "观察期"
    """
    if csi300_kline is None or csi300_kline.empty:
        return "未触发"
    df = csi300_kline.copy()
    if "ma_250" not in df.columns:
        return "未触发"
    below = (df["close"] < df["ma_250"]).fillna(False)
    if not below.any():
        return "未触发"
    # 当前（最新）收盘是否仍在年线下：已在年线上 → 预警解除
    if not bool(below.iloc[-1]):
        return "未触发"
    # 连续收于年线下天数（从最新往回数）
    streak = 0
    for v in below.iloc[::-1]:
        if v:
            streak += 1
        else:
            break
    if streak >= FUSE_DAYS:
        return "已熔断"
    return "预警"


def calc_erp(max_position_pct: Optional[float], pe: Optional[float],
             bond_yield_10y: Optional[float]) -> tuple[Optional[float], str]:
    """股债收益差 → (erp 小数, 水位)"""
    if not pe or pe <= 0 or bond_yield_10y is None:
        return None, "数据不足"
    erp = 1.0 / pe - bond_yield_10y / 100.0
    for lo, hi, level, _cap in ERP_BANDS:
        if (lo is None or erp >= lo) and (hi is None or erp < hi):
            return erp, level
    return erp, "未知"


def calc_rate_factor(bond_yield_10y: Optional[float]) -> float:
    """利率调节系数（对齐第一编3.1速查表）。

    速查表（每 0.5% 步进 0.1）：
      2.0%→1.2, 2.5%→1.1, 3.0%→1.0, 3.5%→0.9, 4.0%+→0.8
    即 系数 = 中值(0.8, 1+(3.0−国债)×0.2, 1.2)。公式正文的 ×0.4 为文档笔误，
    与速查表矛盾，此处以速查表为准。
    """
    if bond_yield_10y is None:
        return 1.0
    return max(0.8, min(1.2, 1.0 + (3.0 - bond_yield_10y) * 0.2))


def calc_m2_level(m2_growth: Optional[float]) -> tuple[str, Optional[float], Optional[float]]:
    """M2 同比 → (档位, PEG上限, 股息率要求%)"""
    if m2_growth is None:
        return "数据不足", None, None
    if m2_growth > 10:
        return "宽松(>10%)", 1.0, 2.5
    if m2_growth >= 8:
        return "中性(8-10%)", 0.8, 3.0
    return "紧缩(<8%)", 0.6, 3.5


def judge_macro_brake(*, erp: Optional[float] = None,
                      csi300_kline: Optional[pd.DataFrame] = None,
                      bond_yield_10y: Optional[float] = None,
                      m2_growth: Optional[float] = None) -> MacroBrakeResult:
    """宏观刹车片综合判定（纯函数）。

    优先级: 熔断 > 股债收益差。熔断例外（erp>5.5%）→ 观察期，不强制降仓。

    Parameters
    ----------
    erp : 股债收益差（小数）；None 时用 pe + bond 内部计算
    csi300_kline : compute_all 后沪深300 K线（年线熔断判定）
    bond_yield_10y : 10Y 国债收益率 %（利率系数 + erp 计算）
    m2_growth : M2 同比 %（流动性调节）
    """
    # ── 股债收益差 → 水位与上限 ──
    erp_val, level = erp, None
    max_pos = 1.00
    notes: list[str] = []
    if erp is None:
        erp_val, level = None, "数据不足"
        notes.append("股债收益差数据不足: 不设上限限制（按100%处理，需人工核对宏观数据）")
    else:
        for lo, hi, lv, cap in ERP_BANDS:
            if (lo is None or erp >= lo) and (hi is None or erp < hi):
                level, max_pos = lv, cap
                break

    # ── 年线熔断 ──
    fuse = judge_fuse(csi300_kline)
    fuse_state = fuse

    # ── 年线熔断 ──
    fuse = judge_fuse(csi300_kline)
    fuse_state = fuse
    is_fused = fuse == "已熔断"
    if is_fused:
        # 例外: 股债收益差 >5.5% → 观察期，不强制降仓，禁止新开仓
        if erp is not None and erp > ERP_EXCEPTION_HIGH:
            fuse_state = "观察期"
            notes.append("熔断触发但股债收益差>5.5%(极度低估): 暂缓熔断，观察期——不强制降仓但禁止新开仓")
        else:
            max_pos = min(max_pos, 0.30)
            notes.append("沪深300连续3日收于年线下: 强制总仓位上限≤30%")
    elif fuse == "预警":
        notes.append("沪深300跌破MA250: 触发预警，开始监控")

    # ── 利率系数 + M2 流动性 ──
    rate_factor = calc_rate_factor(bond_yield_10y)
    m2_level, peg_cap, dividend_req = calc_m2_level(m2_growth)

    return MacroBrakeResult(
        max_position_pct=round(max_pos * 100, 1),
        market_level=level or "数据不足",
        fuse_state=fuse_state,
        rate_factor=round(rate_factor, 3),
        m2_level=m2_level,
        peg_cap=peg_cap,
        dividend_req=dividend_req,
        erp=erp_val,
        notes=notes,
        detail={
            "erp_bands": [
                {"water": lv, "cap_pct": cap * 100} for _lo, _hi, lv, cap in ERP_BANDS
            ],
        },
    )
