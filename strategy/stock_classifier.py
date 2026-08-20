"""股票类型分类器 — 对齐个股 v4.5 Part7.一

Prompt原文:
  A. 高成长:   营收/利润增速>20%, 高PE     → 科技/新能源
  B. 价值白马: 稳定增长<15%, 高ROE         → 消费/医药
  C. 强周期:   随宏观/价格波动             → 有色/化工/航运
  D. 深度价值: 低增长、高股息               → 银行/公用事业
"""

from typing import Optional

# 强周期行业列表
CYCLICAL_INDUSTRIES = [
    "有色", "化工", "航运", "钢铁", "煤炭",
    "石油", "建材", "机械", "地产", "农业",
]


def classify_stock(
    industry: str = "",
    roe: float = 0,
    revenue_growth: float = 0,
    div_yield: float = 0,
    pe: float = 0,
) -> str:
    """按 Prompt 规则对股票分类

    Returns:
        "A" = 高成长, "B" = 价值白马, "C" = 强周期, "D" = 深度价值
    """
    # A: 高成长 — 增速>20%, 高PE
    if revenue_growth > 20 and pe > 30:
        return "A"

    # C: 强周期 — 特定行业
    if any(ind in industry for ind in CYCLICAL_INDUSTRIES):
        return "C"

    # D: 深度价值 — 高股息+低增长
    if div_yield > 4.0 and revenue_growth < 5:
        return "D"

    # B: 价值白马 — 高ROE, 稳定增长
    if roe > 15 and 5 <= revenue_growth <= 20:
        return "B"

    # 兜底: 按特征模糊匹配
    if revenue_growth > 15 and pe > 25:
        return "A"
    if div_yield > 3.0:
        return "D"
    if roe > 15:
        return "B"

    return "B"  # 默认价值白马
