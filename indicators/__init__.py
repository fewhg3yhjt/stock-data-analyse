"""indicators — 可配置/可组合/可编程的指标体系

设计:
  - 基础指标（原子）: MA(任意N)/MIN/MAX/涨跌幅等
  - 组合指标（表达式）: 引用已有指标组合计算
  - 代码指标（注册）: Python 函数动态扩展
  - 数据源: warehouse/daily 加工层（只读）
  - 输出: 指标宽表（时间序列）+ 最新值

入口:
    from StockInvestmentTool.indicators.engine import IndicatorRegistry
    reg = IndicatorRegistry()
    values = reg.latest(df, ["MA20", "MA60"])
"""

from StockInvestmentTool.indicators.engine import (
    IndicatorDef,
    IndicatorRegistry,
    SAFE_FUNCS,
)

__all__ = ["IndicatorDef", "IndicatorRegistry", "SAFE_FUNCS"]