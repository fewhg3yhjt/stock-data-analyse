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
            "ma5": IndicatorDocumentation("观察最近5个交易日的平均收盘价格，反映短期价格趋势", "将最近5个交易日的 close 相加后除以5", "日线 close，至少5个交易日", "数值上升表示短期均价上移，不代表未来必然上涨"),
            "ma10": IndicatorDocumentation("观察最近10个交易日的平均收盘价格，过滤单日波动", "将最近10个交易日的 close 相加后除以10", "日线 close，至少10个交易日"),
            "ma20": IndicatorDocumentation("观察约一个月的平均收盘价格，常用于趋势和回调参考", "将最近20个交易日的 close 相加后除以20", "日线 close，至少20个交易日"),
            "ma60": IndicatorDocumentation("观察约一个季度的平均收盘价格，常用于中期趋势和支撑判断", "将最近60个交易日的 close 相加后除以60", "日线 close，至少60个交易日"),
            "ma120": IndicatorDocumentation("观察约半年平均收盘价格，判断中长期趋势位置", "将最近120个交易日的 close 相加后除以120", "日线 close，至少120个交易日"),
            "ma240": IndicatorDocumentation("观察约一年平均收盘价格，作为长期趋势参考", "将最近240个交易日的 close 相加后除以240", "日线 close，至少240个交易日"),
            "ma17": IndicatorDocumentation("观察最近17个交易日的平均收盘价格，用于自定义短期趋势参考", "将最近17个交易日的 close 相加后除以17", "日线 close，至少17个交易日"),
            "ma63": IndicatorDocumentation("观察最近63个交易日的平均收盘价格，约对应一个季度", "将最近63个交易日的 close 相加后除以63", "日线 close，至少63个交易日"),
            "rsi14": IndicatorDocumentation("衡量近期上涨和下跌力量的相对强弱，常用于识别超买超卖", "计算14日平均上涨幅度与平均下跌幅度的比值，再转换为0到100的 RSI；高值表示近期上涨力量较强", "日线 close，至少14个交易日", "RSI高不等于立即下跌，RSI低也不等于立即上涨"),
            "macd": IndicatorDocumentation("衡量短期和长期价格趋势差异，用于观察趋势方向和动能变化", "快速指数均线(12日)减去慢速指数均线(26日)，返回 MACD 线", "日线 close，至少26个交易日", "这里只展示 MACD 线，不包含信号线和柱体"),
            "volatility_20": IndicatorDocumentation("衡量最近20日收益波动大小，用于判断价格风险和波动环境", "先计算日收益率，再计算20日滚动标准差并年化：标准差×√252×100%", "日线 close，至少20个交易日", "波动率越高表示波动越大，不表示涨跌方向"),
            "atr14": IndicatorDocumentation("衡量最近14日平均真实价格波动幅度，常用于设置风险距离", "每日真实波幅 TR=max(high-low,|high-昨日close|,|low-昨日close|)，再取14日平均", "日线 high、low、close，至少14个交易日", "ATR是价格幅度，不是涨跌方向"),
            "change_amount": IndicatorDocumentation("表示当天收盘价比前一交易日变动了多少元", "今日 close - 昨日 close", "日线 close，至少2个交易日"),
            "amplitude": IndicatorDocumentation("表示当天最高价和最低价相对昨日收盘价的波动范围", "(今日 high - 今日 low) ÷ 昨日 close × 100%", "日线 high、low、close，至少2个交易日"),
            "vol_ma5": IndicatorDocumentation("表示最近5个交易日的平均成交量，用作放量判断基准", "最近5个交易日 volume 的算术平均", "日线 volume，至少5个交易日"),
            "low_3m": IndicatorDocumentation("表示最近约3个月交易日内出现过的最低价，用于寻找短中期支撑", "最近63个交易日 low 的滚动最低值", "日线 low，至少1个交易日；完整窗口建议63日"),
            "year_low": IndicatorDocumentation("表示从数据起点至当前的最低价，作为年内或历史低点参考", "low 的累计最小值", "日线 low，至少1个交易日"),
            "bias_ratio": IndicatorDocumentation("表示当前收盘价相对240日均线偏离了多少，用于观察长期趋势距离", "(close - MA240) ÷ MA240 × 100%", "日线 close，至少240个交易日", "正值表示在长期均线上方，负值表示在下方"),
            "vol_ratio": IndicatorDocumentation("表示当天成交量相对最近5日平均成交量的倍数，用于识别量能放大或萎缩", "当天 volume ÷ 最近5日 volume 均值", "日线 volume，至少5个交易日", "当前实现的均值包含当天；技术止损另使用前5日均量避免自我污染"),
            "ret_5d": IndicatorDocumentation("表示最近5个交易日的收盘价涨跌幅，用于观察短期动量", "(今日 close ÷ 5日前 close - 1) × 100%", "日线 close，至少6个交易日"),
            "ret_20d": IndicatorDocumentation("表示最近20个交易日的收盘价涨跌幅，用于观察月度动量", "(今日 close ÷ 20日前 close - 1) × 100%", "日线 close，至少21个交易日"),
            "high_20d": IndicatorDocumentation("表示最近20个交易日的最高价，用于观察短期压力位", "最近20个交易日 high 的滚动最大值", "日线 high，至少20个交易日"),
            "low_20d": IndicatorDocumentation("表示最近20个交易日的最低价，用于观察短期支撑位", "最近20个交易日 low 的滚动最小值", "日线 low，至少20个交易日"),
            "amplitude_abs": IndicatorDocumentation("表示当天最高价与最低价之间的绝对价格波动", "ABS(high - low)", "日线 high、low"),
            "custom_example": IndicatorDocumentation("演示如何注册一个代码指标；当前结果用于验证指标扩展链路", "对输入行情执行注册的示例函数，返回该函数计算的序列", "取决于代码指标函数，目前使用日线行情", "这是示例指标，不应直接作为投资结论"),
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
            "current_price": IndicatorDocumentation("当前分析时点的股票价格", "取行情数据中最新可用交易日的 close；盘中页面可能另有独立快照", "日线或在线行情数据", "当前价不是技术指标，不用于替代收盘价"),
            "trend": IndicatorDocumentation("根据均线排列和价格走势归纳的趋势描述", "由 TechnicalIndicators.trend_judgment 根据 MA5、MA20、MA60 等关系判断", "至少需要包含 close 的日线数据", "这是趋势分类，不是对未来涨跌的预测"),
            "volatility": IndicatorDocumentation("描述价格近期波动程度的统计值", "由分析引擎根据日收益率波动计算，具体窗口以分析结果为准", "日线 close 序列", "波动率高表示价格变化幅度较大，不代表方向"),
            "pe": IndicatorDocumentation("市盈率，用于观察价格相对每股收益的估值水平", "PE = 股票价格 / 每股收益，项目沿用数据源提供的 PE(TTM)", "需要 PE(TTM) 基本行情字段", "负 PE 或缺失 PE 不应被解释为低估"),
            "pe_percentile": IndicatorDocumentation("当前 PE 在历史 PE 样本中的百分位", "将当前 PE 与可用历史 PE 样本排序后计算所处百分位", "需要连续的历史 PE 数据", "百分位越低只表示历史相对位置较低，不单独构成买入结论"),
            "year_high": IndicatorDocumentation("滚动前高，用于左侧止盈的参考高点", "在指定回看窗口内取日线 high 的最大值；默认窗口为 252 个交易日", "日线 high 序列；窗口可在策略参数中调整", "这是滚动参考高点，不等于持仓以来峰值"),
            "position_peak_price": IndicatorDocumentation("持仓以来的价格峰值，用于右侧移动止盈", "从首次建仓日起，持续取日线最高价与分钟实时价格中的最大值", "持仓买入记录、日线 high、分钟 close/high（如有）", "按持仓状态维护，不是普通日线指标"),
            "position_drawdown": IndicatorDocumentation("当前价格相对持仓峰值的回撤比例", "(持仓峰值 - 当前价) / 持仓峰值", "持仓峰值与最新实时价格", "正数表示回撤；达到策略阈值后才触发右侧移动止盈"),
            "right_side_trigger_price": IndicatorDocumentation("右侧移动止盈触发价", "持仓峰值 × (1 - 当前股票类型的回撤阈值)", "持仓峰值、当前股票类型和策略中的回撤阈值", "只有进入右侧跟踪阶段后才作为卖出判断"),
            "position_phase": IndicatorDocumentation("持仓当前所处的策略阶段", "由交易、突破和止盈/止损事件推进状态机", "持仓交易记录、当前价格与策略判断", "阶段决定哪些策略规则当前生效"),
        }
        items.update({
            "止盈参考线": IndicatorDocumentation("以 MA20 的 95% 作为参考价格线", "0.95 × MA20"),
            "双均线低点": IndicatorDocumentation("MA20 与 MA240 中较低值的 95%", "0.95 × MIN(MA20, MA240)"),
            "振幅_abs": IndicatorDocumentation("单日最高价与最低价之间的绝对价差", "ABS(high - low)"),
            "自定义示例": IndicatorDocumentation("代码指标示例，用于演示注册式指标扩展", "由注册的 Python 指标函数计算", "取决于代码指标函数的输入", "代码指标的具体实现以注册函数为准"),
            "dual_ma_low": IndicatorDocumentation("在短期和长期趋势之间取更保守的支撑参考，再预留安全折扣", "先计算短周期和长周期收盘价均线，取两者较低值，再乘以(1-安全折扣)", "日线 close，至少覆盖最长均线周期", "适合支撑位或左侧买入参考，不代表确定底部"),
            "take_profit_reference": IndicatorDocumentation("把均线下方或附近的价格作为止盈参考线", "计算指定周期收盘价均线，再乘以参考比例", "日线 close，至少覆盖均线周期", "这是参考线，不是自动卖出承诺"),
        })
        return items

    def get(self, name: str, *, fallback: str = "") -> IndicatorDocumentation:
        return self._items.get(name, IndicatorDocumentation(fallback or "由指标定义提供的可计算数值", "请查看该指标的表达式或注册函数"))


def documentation_for(name: str, *, fallback: str = "") -> dict:
    return IndicatorDocumentationRegistry().get(name, fallback=fallback).to_dict()
