# -*- coding: utf-8 -*-
"""代码注册指标示例 — 演示如何用 Python 函数定义指标

函数签名约定: fn(df: pd.DataFrame, env: dict) -> pd.Series
  - df: 原始行情（date/open/high/low/close/volume）
  - env: 已计算的指标环境（可用 env["ma20"] 等引用其他指标）
  - 返回: 与 df 对齐的 Series
"""

import pandas as pd


def example_indicator(df: pd.DataFrame, env: dict) -> pd.Series:
    """示例：量价齐升指标 = 收盘涨跌幅 × 成交量变化。

    仅作演示，展示如何通过代码构建组合指标。
    """
    close = df["close"]
    pct_chg = close.pct_change()
    vol_chg = df["volume"].pct_change()
    return pct_chg * vol_chg * 10000


def volume_price_trend(df: pd.DataFrame, env: dict) -> pd.Series:
    """量价趋势：近5日量价是否同向（>0 量价齐升，<0 背离）。"""
    close_ret = df["close"].pct_change(5)
    vol_ret = df["volume"].pct_change(5)
    return close_ret * vol_ret * 10000
