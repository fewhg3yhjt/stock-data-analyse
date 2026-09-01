"""StockInvestmentTool — A股股票投资分析工具包

一键分析入口:
    python -m StockInvestmentTool --code sh.600900 --name 长江电力 --backtest

功能模块:
    - datasource/ 行情数据获取与技术指标
    - strategy/  分批买入/止盈/止损策略
    - backtest/  回测引擎与绩效指标
    - analysis/  报告生成与可视化
    - prompt/    LLM Prompt 构建与自动调用
    - screener/  A股初筛（按规则导出目标股票集合）
    - fundflow/  资金流分析（同花顺，短线视角的资金方向与趋势）
    - notifier/  股票消息提醒（价格阈值/资金流信号/盘后汇总 → 飞书/企微）【已迁移至 biz】
    - portfolio/ 持仓管理 + 三页看板（观察池/作战仓/复盘底账）
"""

__version__ = "1.0.0"
