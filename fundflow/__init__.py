"""A股资金流分析模块 — 短线视角的资金流向与趋势

短线核心假设: 价格是资金流动的结果，跟踪主力净额的方向与持续性
比静态估值更能反映"当前正在发生什么"。

数据源: 同花顺资金流（行业/概念/个股 × 即时/3日/5日/10日排行），
不依赖易被封的东财接口。

用法:
    python -m StockInvestmentTool.fundflow                       # 即时+3日趋势
    python -m StockInvestmentTool.fundflow --trend-days 3,5      # 加5日趋势
    python -m StockInvestmentTool.fundflow --sections stock      # 只看个股

子模块:
    - sources.py   数据源适配器（同花顺 + 单位解析）
    - analysis.py  趋势判断/背离检测/榜单
    - export.py    导出
    - cli.py       命令行入口
"""

__all__ = ["sources", "analysis", "export"]
