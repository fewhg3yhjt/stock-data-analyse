"""Human-readable definitions for the indicator catalogue."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class IndicatorDocumentation:
    meaning: str
    calculation: str
    data_requirements: str = "日线：date、open、high、low、close、volume"
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class IndicatorDocumentationRegistry:
    def __init__(self):
        self._items = self._build()

    @staticmethod
    def _build() -> dict[str, IndicatorDocumentation]:
        items = {
            "MA5": IndicatorDocumentation("最近 5 个交易日收盘价的平均水平", "MA5 = 最近 5 日 close 的算术平均"),
            "MA10": IndicatorDocumentation("最近 10 个交易日收盘价的平均水平", "MA10 = 最近 10 日 close 的算术平均"),
            "MA20": IndicatorDocumentation("最近 20 个交易日收盘价的平均水平", "MA20 = 最近 20 日 close 的算术平均"),
            "MA60": IndicatorDocumentation("最近 60 个交易日收盘价的平均水平", "MA60 = 最近 60 日 close 的算术平均"),
            "MA120": IndicatorDocumentation("最近 120 个交易日收盘价的平均水平", "MA120 = 最近 120 日 close 的算术平均"),
            "MA240": IndicatorDocumentation("最近 240 个交易日收盘价的平均水平", "MA240 = 最近 240 日 close 的算术平均"),
            "ATR14": IndicatorDocumentation("衡量近期价格波动幅度的平均值", "ATR14 = 最近 14 日 True Range 的平均值；TR=max(high-low, |high-昨日close|, |low-昨日close|)"),
            "H20": IndicatorDocumentation("最近 20 个交易日的最高价", "H20 = 最近 20 日 high 的最大值"),
            "L20": IndicatorDocumentation("最近 20 个交易日的最低价", "L20 = 最近 20 日 low 的最小值"),
            "CENTER20": IndicatorDocumentation("最近 20 日价格区间的中点", "CENTER20 = (H20 + L20) / 2"),
            "POSITION20": IndicatorDocumentation("当前收盘价在最近 20 日高低区间中的相对位置", "POSITION20 = (close - L20) / (H20 - L20)，范围通常为 0 到 1"),
            "MA20_SLOPE": IndicatorDocumentation("MA20 在指定比较周期内的相对变化幅度", "MA20_SLOPE = (MA20_today - MA20_N_days_ago) / MA20_N_days_ago"),
            "CENTER_SHIFT": IndicatorDocumentation("20 日价格中枢在比较周期内的相对漂移", "CENTER_SHIFT = (CENTER20_today - CENTER20_N_days_ago) / CENTER20_N_days_ago"),
            "MA_DISTANCE": IndicatorDocumentation("MA20 与 MA60 的相对距离", "MA_DISTANCE = abs(MA20 - MA60) / abs(MA60)"),
            "MA20_CROSS_COUNT": IndicatorDocumentation("统计周期内收盘价穿越 MA20 的次数", "相邻交易日 close 相对 MA20 的上下方向发生变化时计数 1 次"),
            "POST_HIGH": IndicatorDocumentation("买入日期之后的有效最高价，用于止盈提醒", "max(买入价、分钟覆盖日的分钟高点、其余交易日的日线 high)，严格排除买入日", "需要交易买入日期/价格，以及可见的日线和分钟数据", "现有腾讯分钟文件无真实 high 时使用 close 作为分钟高点代理"),
            "POST_LOW": IndicatorDocumentation("买入日期之后的有效最低价，用于风险和复盘", "min(买入价、分钟覆盖日的分钟低点、其余交易日的日线 low)，严格排除买入日", "需要交易买入日期/价格，以及可见的日线和分钟数据", "现有腾讯分钟文件无真实 low 时使用 close 作为分钟低点代理"),
            "pct_chg": IndicatorDocumentation("相邻交易日收盘价的百分比变化", "pct_chg = (今日 close / 昨日 close - 1) × 100%"),
            "volume_ratio": IndicatorDocumentation("当前成交量相对于指定历史均量的比例", "volume_ratio = 当前 volume / 历史均量；窗口由调用方决定"),
            "volume_5_20": IndicatorDocumentation("最近 5 日平均成交量相对于最近 20 日平均成交量的比例", "volume_5_20 = MA(volume,5) / MA(volume,20)"),
        }
        items.update({
            "止盈参考线": IndicatorDocumentation("以 MA20 的 95% 作为参考价格线", "0.95 × MA20"),
            "双均线低点": IndicatorDocumentation("MA20 与 MA240 中较低值的 95%", "0.95 × MIN(MA20, MA240)"),
            "振幅_abs": IndicatorDocumentation("单日最高价与最低价之间的绝对价差", "ABS(high - low)"),
            "自定义示例": IndicatorDocumentation("代码指标示例，用于演示注册式指标扩展", "由注册的 Python 指标函数计算", "取决于代码指标函数的输入", "代码指标的具体实现以注册函数为准"),
        })
        return items

    def get(self, name: str, *, fallback: str = "") -> IndicatorDocumentation:
        return self._items.get(name, IndicatorDocumentation(fallback or "由指标定义提供的可计算数值", "请查看该指标的表达式或注册函数"))


def documentation_for(name: str, *, fallback: str = "") -> dict:
    return IndicatorDocumentationRegistry().get(name, fallback=fallback).to_dict()
