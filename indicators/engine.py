# -*- coding: utf-8 -*-
"""指标体系 — 可配置/可组合/可编程的指标计算引擎

设计:
  - 基础指标（原子）: MA(任意N)/MIN/MAX/涨跌幅等，注册为安全函数
  - 组合指标（表达式）: 用已有指标 + 算术/函数表达式，如 0.95*MA20
  - 代码指标（注册）: Python 函数动态扩展，可作其他指标输入
  - 数据源: 只读 warehouse/daily 加工层（原始行情）
  - 输出: 指标宽表（时间序列）+ 最新值，供决策/展示/扫描

指标配置: schemes/indicators.yaml
存储: warehouse/indicators/ 分区（可选，按需模式可不落盘）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# 基础指标函数（原子操作，注册为表达式可用）
# ═══════════════════════════════════════════════════════════

def _ma(series, n: int) -> pd.Series:
    """移动平均线（对 Series 做 N 期均值）"""
    return pd.Series(series).rolling(int(n)).mean()


def _min2(a, b):
    """逐元素 MIN（Series 或标量）"""
    import numpy as np
    return np.minimum(a, b)


def _max2(a, b):
    """逐元素 MAX（Series 或标量）"""
    import numpy as np
    return np.maximum(a, b)


def _abs(v):
    return abs(v)


def _rsi(series, n: int = 14) -> pd.Series:
    """Wilder-style RSI using rolling average gains and losses."""
    values = pd.Series(series)
    delta = values.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(int(n), min_periods=int(n)).mean()
    avg_loss = loss.rolling(int(n), min_periods=int(n)).mean()
    relative_strength = avg_gain / avg_loss.replace(0, pd.NA)
    result = 100 - (100 / (1 + relative_strength))
    result = result.where(avg_loss.ne(0), 100.0)
    return result.astype(float)


def _macd(series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    """Return the MACD line (fast EMA minus slow EMA)."""
    values = pd.Series(series)
    fast_ema = values.ewm(span=int(fast), adjust=False, min_periods=int(fast)).mean()
    slow_ema = values.ewm(span=int(slow), adjust=False, min_periods=int(slow)).mean()
    return fast_ema - slow_ema


def _volatility(series, n: int = 20) -> pd.Series:
    """Annualized rolling volatility of close-to-close returns in percent."""
    returns = pd.Series(series).pct_change()
    return returns.rolling(int(n), min_periods=int(n)).std() * (252 ** 0.5) * 100


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average True Range（简单 N 期 TR 均值，与 V1 操作点策略同口径）。"""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    previous_close = df["close"].astype(float).shift(1)
    true_range = pd.concat([
        high - low,
        (high - previous_close).abs(),
        (low - previous_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.rolling(int(n), min_periods=int(n)).mean()


def _pct_chg(df, env=None) -> pd.Series:
    """日涨跌幅 % = close 相对前一日变化百分比。"""
    return df["close"].astype(float).pct_change() * 100


def _change_amount(df, env=None) -> pd.Series:
    """日涨跌额 = close 相对前一日差值。"""
    return df["close"].astype(float).diff()


def _amplitude(df, env=None) -> pd.Series:
    """振幅 % = (最高价 - 最低价) / 昨日收盘 × 100。"""
    return (df["high"].astype(float) - df["low"].astype(float)) / df["close"].astype(float).shift(1) * 100


def _vol_ma5(df, env=None) -> pd.Series:
    """成交量 5 日均线（手口径保留，量能辅助）。"""
    return df["volume"].astype(float).rolling(5).mean()


def _low_3m(df, env=None) -> pd.Series:
    """近 3 月低点（63 个交易日滚动最低价）。"""
    return df["low"].astype(float).rolling(63, min_periods=1).min()


def _year_low(df, env=None) -> pd.Series:
    """年内低点（全历史/年内最低价）。"""
    return df["low"].astype(float).expanding(min_periods=1).min()


def _bias_ratio(df, env=None) -> pd.Series:
    """乖离率 = (close - ma240) / ma240 × 100（年线统一为 ma240）。"""
    close = df["close"].astype(float)
    ma240 = close.rolling(240).mean()
    return (close - ma240) / ma240 * 100


def _vol_ratio(df, env=None) -> pd.Series:
    """量比 = 当日成交量 / 5日均量。"""
    volume = df["volume"].astype(float)
    return volume / volume.rolling(5).mean()


def _ret_5d(df, env=None) -> pd.Series:
    """5 日动量 % = 收盘价五日变化百分比。"""
    return df["close"].astype(float).pct_change(5) * 100


def _ret_20d(df, env=None) -> pd.Series:
    """20 日动量 % = 收盘价二十日变化百分比。"""
    return df["close"].astype(float).pct_change(20) * 100


def _high_20d(df, env=None) -> pd.Series:
    """20 日滚动最高价。"""
    return df["high"].astype(float).rolling(20).max()


def _low_20d(df, env=None) -> pd.Series:
    """20 日滚动最低价。"""
    return df["low"].astype(float).rolling(20).min()


# 安全函数白名单（表达式可调用）
SAFE_FUNCS: dict[str, Callable] = {
    "MA": _ma,
    "MIN": _min2,
    "MAX": _max2,
    "ABS": _abs,
}


@dataclass
class IndicatorDef:
    """指标定义"""
    name: str
    kind: str            # base / composite / code / decision
    expr: str = ""       # 表达式（base/composite）
    fn: Optional[Callable] = None  # code 指标的 Python 函数
    description: str = ""
    applies_to: list[str] = field(default_factory=lambda: ["stock", "etf"])


class IndicatorRegistry:
    """指标注册表：管理基础/组合/代码指标定义。

    从 schemes/indicators.yaml 加载 + 支持代码注册。
    """

    def __init__(self, config_path: Optional[Path] = None):
        from StockInvestmentTool.config import Config
        self.config_path = config_path or Config.BASE_DIR / "schemes" / "indicators.yaml"
        self._bases: dict[str, IndicatorDef] = {}      # 基础指标（原子）
        self._composites: dict[str, IndicatorDef] = {}  # 组合指标（表达式）
        self._code: dict[str, IndicatorDef] = {}       # 代码注册指标
        self._load_config()

    def _load_config(self):
        """从 indicators.yaml 加载指标定义（含内置基础指标）。"""
        # Built-ins use the canonical metric key as both name and output column.
        builtin = {
            "ma5": IndicatorDef("ma5", "base", "MA(close,5)", description="5日均线"),
            "ma10": IndicatorDef("ma10", "base", "MA(close,10)", description="10日均线"),
            "ma20": IndicatorDef("ma20", "base", "MA(close,20)", description="20日均线"),
            "ma60": IndicatorDef("ma60", "base", "MA(close,60)", description="60日均线"),
            "ma120": IndicatorDef("ma120", "base", "MA(close,120)", description="120日均线"),
            "ma240": IndicatorDef("ma240", "base", "MA(close,240)", description="240日均线"),
        }
        self._bases.update(builtin)
        self._code.update({
            "rsi14": IndicatorDef("rsi14", "code", fn=lambda df, env: _rsi(df["close"], 14), description="14日相对强弱指标"),
            "macd": IndicatorDef("macd", "code", fn=lambda df, env: _macd(df["close"]), description="MACD线"),
            "volatility_20": IndicatorDef("volatility_20", "code", fn=lambda df, env: _volatility(df["close"], 20), description="20日年化波动率"),
            "atr14": IndicatorDef("atr14", "code", fn=lambda df, env: _atr(df, 14), description="14日平均真实波幅"),
            "pct_chg": IndicatorDef("pct_chg", "code", fn=_pct_chg, description="日涨跌幅"),
            "change_amount": IndicatorDef("change_amount", "code", fn=_change_amount, description="日涨跌额"),
            "amplitude": IndicatorDef("amplitude", "code", fn=_amplitude, description="振幅百分比"),
            "vol_ma5": IndicatorDef("vol_ma5", "code", fn=_vol_ma5, description="成交量5日均线"),
            "low_3m": IndicatorDef("low_3m", "code", fn=_low_3m, description="近3月低点"),
            "year_low": IndicatorDef("year_low", "code", fn=_year_low, description="年内低点"),
            "bias_ratio": IndicatorDef("bias_ratio", "code", fn=_bias_ratio, description="MA240乖离率"),
            "vol_ratio": IndicatorDef("vol_ratio", "code", fn=_vol_ratio, description="量比"),
            "ret_5d": IndicatorDef("ret_5d", "code", fn=_ret_5d, description="5日动量"),
            "ret_20d": IndicatorDef("ret_20d", "code", fn=_ret_20d, description="20日动量"),
            "high_20d": IndicatorDef("high_20d", "code", fn=_high_20d, description="20日高点"),
            "low_20d": IndicatorDef("low_20d", "code", fn=_low_20d, description="20日低点"),
        })

        # 从 YAML 加载自定义指标
        if not self.config_path.exists():
            return
        try:
            import yaml
            with open(self.config_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("指标配置加载失败 %s: %s", self.config_path, e)
            return

        # 基础指标（自定义 MA 等）
        for item in data.get("bases", []) or []:
            name = item.get("name", "")
            if name:
                name = name.lower()
                self._bases[name] = IndicatorDef(
                    name, "base", item.get("expr", ""),
                    description=item.get("description", ""),
                )
        # 组合指标（表达式引用已有）
        for item in data.get("composite", []) or []:
            name = item.get("name", "")
            if name:
                name = name.lower()
                self._composites[name] = IndicatorDef(
                    name, "composite", item.get("expr", ""),
                    description=item.get("description", ""),
                )
        # 代码注册指标
        for item in data.get("code", []) or []:
            name = item.get("name", "")
            module = item.get("module", "")
            fn_name = item.get("function", "")
            if name and module and fn_name:
                try:
                    import importlib
                    mod = importlib.import_module(module)
                    fn = getattr(mod, fn_name)
                    name = name.lower()
                    self._code[name] = IndicatorDef(
                        name, "code", fn=fn,
                        description=item.get("description", ""),
                    )
                except Exception as e:
                    logger.warning("代码指标加载失败 %s: %s", name, e)

        # 用户指标必须使用 canonical snake_case 名称。
        try:
            from StockInvestmentTool.indicators.store import list_indicators
            for item in list_indicators():
                if not item.get("enabled", True) or not item.get("name"):
                    continue
                target = self._bases if item.get("kind") == "base" else self._composites
                name = item["name"]
                target[name] = IndicatorDef(
                    name, item.get("kind", "composite"), item.get("expr", ""),
                    description=item.get("description", ""),
                    applies_to=item.get("applies_to", ["stock", "etf"]),
                )
        except Exception as e:
            logger.warning("用户指标加载失败: %s", e)

    # ── 注册 ─────────────────────────────────────────

    def register_code(self, name: str, fn: Callable, description: str = ""):
        """代码注册一个指标函数（可作其他指标输入）。"""
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("指标名称必须使用小写 snake_case")
        self._code[name] = IndicatorDef(name, "code", fn=fn, description=description)

    def all_names(self) -> list[str]:
        return list(self._bases) + list(self._composites) + list(self._code)

    def get(self, name: str) -> Optional[IndicatorDef]:
        return (self._bases.get(name) or self._composites.get(name)
                or self._code.get(name))

    # ── 表达式解析与求值 ─────────────────────────────

    def _parse_expr(self, expr: str, series: dict[str, pd.Series]) -> pd.Series:
        """解析并求值一个指标表达式。

        支持: 算术(+-*/) / 函数调用(MA/MIN/MAX/ABS) / 引用已有指标名
        返回: pd.Series
        """
        # 递归替换: 把函数调用和指标引用转成 pandas Series 运算
        # 用受限的 eval，只允许白名单函数和已计算指标
        allowed = {k: v for k, v in SAFE_FUNCS.items()}
        allowed.update({k: v for k, v in series.items()})

        # 预处理: 数字常量包装为常量序列（避免标量/序列混合问题）
        env = dict(allowed)
        # 用简单方法: 把表达式里的指标名替换为 series 引用
        return self._safe_eval(expr, env)

    @staticmethod
    def _safe_eval(expr: str, env: dict) -> pd.Series:
        """受限求值：仅允许白名单函数和已定义变量。"""
        # 安全检查: 禁止 __ 双下划线（防属性访问攻击）
        if "__" in expr:
            raise ValueError(f"表达式含非法字符: {expr}")
        try:
            return eval(expr, {"__builtins__": {}}, env)  # noqa: S307
        except Exception as e:
            raise ValueError(f"表达式求值失败 '{expr}': {e}") from e

    # ── 计算 ─────────────────────────────────────────

    def compute(self, df: pd.DataFrame,
                names: Optional[list[str]] = None) -> dict[str, pd.Series]:
        """计算指定指标（按依赖顺序）。

        Args:
            df: 原始行情 DataFrame（含 date/open/high/low/close/volume）
            names: 要算的指标名（None=全部）

        Returns:
            {指标名: Series(与 df 对齐)}
        """
        if df is None or df.empty:
            return {}
        names = names or self.all_names()

        # 基础原始列（作为可引用变量）
        env: dict[str, pd.Series] = {
            "close": df["close"], "open": df["open"],
            "high": df["high"], "low": df["low"],
            "volume": df["volume"],
        }
        # 涨跌幅（基础）
        env["pct_chg"] = df["close"].pct_change() * 100

        result: dict[str, pd.Series] = {}

        # ① 基础指标（算完加入 env，供组合引用）
        for name in names:
            d = self._bases.get(name)
            if d and d.expr:
                try:
                    val = self._parse_expr(d.expr, env)
                    result[name] = val
                    env[name] = val
                except Exception as e:
                    logger.warning("基础指标 %s 计算失败: %s", name, e)
        # ② 组合指标（引用基础 + 已算组合）
        for name in names:
            d = self._composites.get(name)
            if d and d.expr:
                try:
                    val = self._parse_expr(d.expr, env)
                    result[name] = val
                    env[name] = val
                except Exception as e:
                    logger.warning("组合指标 %s 计算失败: %s", name, e)
        # ③ 代码指标
        for name in names:
            d = self._code.get(name)
            if d and d.fn:
                try:
                    val = d.fn(df, dict(env))
                    result[name] = val
                except Exception as e:
                    logger.warning("代码指标 %s 计算失败: %s", name, e)

        # 只返回请求的指标
        return {k: v for k, v in result.items() if k in names}

    def latest(self, df: pd.DataFrame, names: Optional[list[str]] = None) -> dict:
        """计算指标的最新值（供决策/展示）。"""
        series = self.compute(df, names)
        out = {}
        for name, s in series.items():
            vals = s.dropna()
            out[name] = round(float(vals.iloc[-1]), 4) if len(vals) else None
        return out

    def evaluate_expression(self, df: pd.DataFrame, expr: str) -> pd.Series:
        """Evaluate an ad-hoc expression against configured indicators.

        This is deliberately read-only: callers can validate and preview an
        expression before deciding whether it belongs in indicators.yaml.
        """
        expr = (expr or "").strip()
        if not expr:
            raise ValueError("指标表达式不能为空")
        if df is None or df.empty:
            raise ValueError("缺少行情数据，无法计算指标表达式")
        env: dict[str, pd.Series] = {
            "close": df["close"], "open": df["open"],
            "high": df["high"], "low": df["low"],
            "volume": df["volume"],
            "pct_chg": df["close"].pct_change() * 100,
        }
        computed = self.compute(df)
        env.update(computed)
        value = self._parse_expr(expr, env)
        if not isinstance(value, pd.Series):
            value = pd.Series(value, index=df.index)
        return pd.to_numeric(value, errors="coerce")
