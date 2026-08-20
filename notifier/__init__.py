"""股票消息提醒模块 — 价格阈值 / 资金流信号 / 每日盘后汇总

推送渠道: 企业微信 或 飞书 群机器人 webhook（消息直达自己的 App，
不经任何第三方中转）。

用法:
    python -m StockInvestmentTool.notifier --test      # 测试 webhook
    python -m StockInvestmentTool.notifier --all       # price + fundflow + daily

子模块:
    - channels.py  渠道适配器（企微/飞书 webhook）
    - notify.py    规则加载 + 消息构造 + 推送
    - cli.py       命令行入口
"""

__all__ = ["channels", "notify"]
