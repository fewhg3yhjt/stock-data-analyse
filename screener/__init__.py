"""A股初筛模块 — 按可配置规则导出目标股票集合

独立子包，与主分析流程解耦。规则见 screen_rules.yaml。

用法:
    python -m StockInvestmentTool.screener                     # 用 screen_rules.yaml
    python -m StockInvestmentTool.screener --boards main_sh,main_sz --max-price 35

数据流:
    股票池(新浪/东财全市场快照)
        → 板块过滤(代码前缀判定 + 权限映射)
        → 名称过滤(ST/关键词)
        → 股价过滤
        → 腾讯批量报价增强(PE/PB/市值/换手率, 不封IP)
        → 估值规则过滤
        → 导出 CSV/JSON + 同花顺行业热度汇总(参考)

子模块:
    - board.py     板块判定
    - rules.py     规则模型 + YAML 加载
    - sources.py   数据源适配器
    - pipeline.py  主流程
    - export.py    导出
    - cli.py       命令行入口
"""

__all__ = ["pipeline", "rules", "sources", "board", "export"]
