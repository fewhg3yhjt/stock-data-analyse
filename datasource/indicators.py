"""技术指标计算与估值辅助"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class TechnicalIndicators:
    """技术指标计算（纯函数，不修改输入）。

    指标计算统一复用 IndicatorRegistry（与 indicators 分区同口径）；
    本类仅保留非指标的判定/辅助函数（均线排列、MA 方向、支撑压力、波动率）。
    """

    @staticmethod
    def compute_all(df: pd.DataFrame) -> pd.DataFrame:
        """批量计算常用技术指标列（统一走 IndicatorRegistry）。

        产出：ma5/10/20/60/120/240、change_pct（=引擎 pct_chg）、change_amount、
        amplitude、vol_ma5、low_3m、year_low、bias_ratio。
        缺失的原始列（date/open/high/low/close/volume/amount/turn/pe_ttm/pb_mrq）原样保留。
        """
        if df is None or df.empty:
            return df.copy()
        from StockInvestmentTool.indicators.engine import IndicatorRegistry
        import pandas as _pd
        df = df.copy()
        reg = IndicatorRegistry()
        ind_names = ["ma5", "ma10", "ma20", "ma60", "ma120", "ma240",
                     "pct_chg", "change_amount", "amplitude", "vol_ma5",
                     "low_3m", "year_low", "bias_ratio"]
        computed = reg.compute(df, ind_names)
        # 引擎列 → DataFrame 列（pct_chg 兼容为 change_pct，保留旧列名）
        for name in ind_names:
            s = computed.get(name)
            if s is None:
                continue
            col = "change_pct" if name == "pct_chg" else name
            df[col] = s.values
        return df

    @staticmethod
    def _ensure_ma(df: pd.DataFrame, windows: list[int]) -> pd.DataFrame:
        """确保 df 含指定 ma 列（缺失时用统一引擎补齐，口径与 indicators 分区一致）。"""
        missing = [w for w in windows if f"ma{w}" not in df.columns]
        if not missing:
            return df
        from StockInvestmentTool.indicators.engine import IndicatorRegistry
        out = df.copy()
        computed = IndicatorRegistry().compute(out, [f"ma{w}" for w in missing])
        for w in missing:
            s = computed.get(f"ma{w}")
            if s is not None:
                out[f"ma{w}"] = s.values
        return out

    @staticmethod
    def ma_slope(df: pd.DataFrame, ma_col: str = "ma60",
                 compare_days: int = 5, tolerance: float = 0.005) -> str:
        """MA 方向量化判定（V6.0: MA60 方向对支撑位的影响）

        - 向上: 今日 MA > 5 个交易日前 MA → 支撑增强，阵地可上修 +3%
        - 走平: 今日 MA 在 5 日前 MA 的 ±0.5% 范围内 → 支撑中性，维持原值
        - 向下: 今日 MA < 5 个交易日前 MA → 支撑减弱，阵地需下修 -3%

        判定所需的 ma 列缺失时用统一引擎补齐（不依赖调用方先 compute_all）。

        Returns:
            "向上" / "走平" / "向下" / "数据不足"
        """
        df = TechnicalIndicators._ensure_ma(df, [20, 60, 240])
        if ma_col not in df.columns or len(df) < compare_days + 1:
            return "数据不足"
        today = df[ma_col].iloc[-1]
        past = df[ma_col].iloc[-1 - compare_days]
        if pd.isna(today) or pd.isna(past) or past <= 0:
            return "数据不足"
        if today > past * (1 + tolerance):
            return "向上"
        if today < past * (1 - tolerance):
            return "向下"
        return "走平"

    # ── 均线排列判定 ─────────────────────────────────

    @staticmethod
    def trend_judgment(df: pd.DataFrame) -> str:
        """判定均线排列: 多头 / 空头 / 震荡（ma 列缺失时统一引擎补齐）。"""
        df = TechnicalIndicators._ensure_ma(df, [5, 20, 60])
        if any(c not in df.columns for c in ("ma5", "ma20", "ma60")):
            return "数据不足"
        last = df.iloc[-1]
        try:
            if last["ma5"] > last["ma20"] > last["ma60"]:
                return "多头排列（MA5 > MA20 > MA60）"
            elif last["ma5"] < last["ma20"] < last["ma60"]:
                return "空头排列（MA5 < MA20 < MA60）"
            else:
                return "震荡格局"
        except KeyError:
            return "数据不足"

    # ── 支撑/压力位计算 ─────────────────────────────

    @staticmethod
    def support_resistance(df: pd.DataFrame, months_back: int = 3) -> dict:
        """计算支撑位与压力位"""
        recent = df.tail(63 * months_back)  # 近似月数
        return {
            "recent_low": round(recent["low"].min(), 2),
            "recent_high": round(recent["high"].max(), 2),
            "year_high": round(df["high"].max(), 2),
            "year_low": round(df["low"].min(), 2),
            "current_price": round(df["close"].iloc[-1], 2),
        }

    # ── 波动率 ──────────────────────────────────────

    @staticmethod
    def annualized_volatility(df: pd.DataFrame) -> float:
        """年化波动率（基于对数收益率）"""
        log_ret = np.log(df["close"] / df["close"].shift(1)).dropna()
        return round(float(log_ret.std() * np.sqrt(252) * 100), 2)


class ValuationHelper:
    """估值辅助（PE 百分位 + 三重锚 + 交叉支撑）"""

    @staticmethod
    def pe_percentile(df: pd.DataFrame) -> dict:
        """计算 PE 及其历史百分位"""
        pe_col = "pe_ttm" if "pe_ttm" in df.columns else ("peTTM" if "peTTM" in df.columns else None)
        if pe_col is None:
            return {}
        pe = df[pe_col].dropna()
        if len(pe) < 20:
            return {}
        current = pe.iloc[-1]
        rank = (pe < current).sum() / len(pe) * 100
        return {
            "current_pe": round(float(current), 2),
            "pe_percentile": round(float(rank), 1),
            "pe_median": round(float(pe.median()), 2),
            "pe_p70": round(float(pe.quantile(0.70)), 2),
            "pe_p80": round(float(pe.quantile(0.80)), 2),
            "pe_p90": round(float(pe.quantile(0.90)), 2),
        }

    @staticmethod
    def triple_anchor(dividends: list[dict], current_price: float) -> dict:
        """股息率三重锚定价

        锚定价① = 每股分红 ÷ 近5年平均股息率  (历史合理中枢)
        锚定价② = 每股分红 ÷ 3.4%            (安全边际线)
        锚定价③ = 每股分红 ÷ 4.0%            (极端低估线)

        Parameters
        ----------
        dividends : list[dict]
            baostock 分红数据，含 dividSum(每股股利) 字段
        current_price : float
            当前股价
        """
        div_per_share = 0.0
        valid_divs = [d for d in dividends if d.get("dividSum") and float(d["dividSum"]) > 0]

        if not valid_divs:
            return {"error": "分红数据不足"}

        # 最近一年每股分红
        latest_div = sorted(valid_divs, key=lambda x: x.get("pubDate", ""), reverse=True)
        div_per_share = float(latest_div[0]["dividSum"])

        # 历史股息率
        hist_yields = []
        for d in valid_divs:
            if d.get("dividSum") and float(d["dividSum"]) > 0:
                hist_yields.append(float(d["dividSum"]) / current_price)

        avg_yield = np.mean(hist_yields) if hist_yields else 0.035

        anchor1 = round(div_per_share / avg_yield, 2) if avg_yield > 0 else 0
        anchor2 = round(div_per_share / 0.034, 2)
        anchor3 = round(div_per_share / 0.040, 2)

        return {
            "div_per_share": round(div_per_share, 4),
            "avg_5y_yield": round(float(avg_yield * 100), 2),
            "anchor_price_1": anchor1,
            "anchor_price_2": anchor2,
            "anchor_price_3": anchor3,
            "current_price": current_price,
            "judgment": ValuationHelper._anchor_judgment(
                current_price, anchor1, anchor2, anchor3
            ),
        }

    @staticmethod
    def _anchor_judgment(price: float, a1: float, a2: float, a3: float) -> str:
        """三重锚综合判定"""
        if price <= a3 and a3 > 0:
            return "❌ 极端低估（低于锚定价③），可关注"
        elif price <= a2 and a2 > 0:
            return "⚠️ 较低估（低于锚定价②），可分批建仓"
        elif price <= a1 and a1 > 0:
            return "✅ 合理偏低（低于锚定价①），具备安全边际"
        else:
            return "✅ 合理（高于锚定价①），正常持有"

    @staticmethod
    def cross_validation(technical: dict, valuation: dict) -> dict:
        """技术支撑 + 估值锚 → 交叉验证支撑位"""
        candidates = []
        sources = []

        # 估值锚③（极端低估线）
        if valuation and valuation.get("anchor_price_3", 0) > 0:
            candidates.append(valuation["anchor_price_3"])
            sources.append("股息率极端低估锚(4.0%)")

        # 近12个月最低价
        if technical.get("year_low"):
            candidates.append(technical["year_low"])
            sources.append("近12月最低价")

        # MA60
        if "ma60" in technical:
            candidates.append(technical["ma60"])
            sources.append("MA60均线")

        # 近3月低点
        if technical.get("recent_low"):
            candidates.append(technical["recent_low"])
            sources.append("近3月低点")

        if not candidates:
            return {"strong_support": 0, "weak_support": 0}

        sorted_vals = sorted(candidates)
        return {
            "strong_support": round(sorted_vals[0], 2),
            "strong_source": sources[candidates.index(sorted_vals[0])],
            "weak_support": round(sorted_vals[1], 2) if len(sorted_vals) > 1 else round(sorted_vals[0], 2),
            "weak_source": sources[candidates.index(sorted_vals[1])] if len(sorted_vals) > 1 else sources[0],
            "all_levels": dict(zip(sources, [round(v, 2) for v in candidates])),
        }

    # ── Prompt v4.5 评级方法 ──────────────────────────────────────

    @staticmethod
    def rate_pe_percentile(percentile: float) -> str:
        """PE历史分位评级"""
        if percentile < 30:
            return "便宜"
        if percentile <= 70:
            return "合理"
        return "偏贵"

    @staticmethod
    def rate_peg(peg: float) -> str:
        """PEG评级"""
        if peg < 1:
            return "便宜"
        if peg <= 1.5:
            return "合理"
        return "偏贵"

    @staticmethod
    def rate_dividend_yield(yield_pct: float) -> str:
        """股息率评级"""
        if yield_pct >= 3.4:
            return "优秀"
        if yield_pct >= 2.0:
            return "及格"
        return "不及格"

    @staticmethod
    def erp(pe: float, bond_yield: float = 0.025) -> float:
        """计算股债利差 ERP = 1/PE - 10Y国债收益率"""
        if pe <= 0:
            return 0.0
        return round(1.0 / pe - bond_yield, 6)

    @staticmethod
    def rate_erp(erp_value: float) -> str:
        """ERP三档判定"""
        if erp_value > 0.04:
            return "便宜"
        if erp_value >= 0.02:
            return "合理"
        return "偏贵"
