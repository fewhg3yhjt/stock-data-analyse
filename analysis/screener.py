"""排雷模块 — 对齐个股 v4.5 Part1 / 基金 v4.5-Fund Part1

Prompt原文（个股）:
  红线项（任一触发即终止）:
    财务造假 → 近5年被证监会处罚/立案
    审计意见 → 非标准无保留意见
    经营现金流 → 近2年连续为负
    重大违规 → 欺诈/挪用资金/实控人违法

  预警项（标记⚠️,不否决）:
    商誉/净资产 >30%
    大股东减持 近1年>2%
    客户集中度 前五大客户>50%
    有息负债率 >60%
"""

from typing import Optional


# ── 红线判定 ────────────────────────────────────────────

RED_LINE_CHECKS = {
    "财务造假": "近5年被证监会处罚/立案",
    "审计意见": "非标准无保留意见",
    "经营现金流": "近2年连续为负",
    "重大违规": "欺诈/挪用资金/实控人违法",
}


def check_red_lines(
    financial_fraud: bool = False,
    audit_opinion: bool = False,
    negative_cashflow: bool = False,
    major_violation: bool = False,
) -> list[dict]:
    """检查红线项

    Returns:
        [{"name": str, "triggered": bool, "reason": str}, ...]
    """
    results = [
        {"name": "财务造假", "triggered": financial_fraud,
         "reason": RED_LINE_CHECKS["财务造假"] if financial_fraud else "无异常"},
        {"name": "审计意见", "triggered": audit_opinion,
         "reason": RED_LINE_CHECKS["审计意见"] if audit_opinion else "标准无保留意见"},
        {"name": "经营现金流", "triggered": negative_cashflow,
         "reason": RED_LINE_CHECKS["经营现金流"] if negative_cashflow else "正常"},
        {"name": "重大违规", "triggered": major_violation,
         "reason": RED_LINE_CHECKS["重大违规"] if major_violation else "无记录"},
    ]
    return results


def has_red_line_triggered(red_lines: list[dict]) -> bool:
    """是否有红线触发"""
    return any(r["triggered"] for r in red_lines)


# ── 预警判定 ────────────────────────────────────────────

WARNING_CHECKS = {
    "商誉/净资产": {"threshold": 0.30, "unit": "ratio"},
    "大股东减持": {"threshold": 0.02, "unit": "ratio"},
    "客户集中度": {"threshold": 0.50, "unit": "ratio"},
    "有息负债率": {"threshold": 0.60, "unit": "ratio"},
}


def check_warnings(
    goodwill_ratio: Optional[float] = None,
    major_shareholder_reduction: Optional[float] = None,
    customer_concentration: Optional[float] = None,
    debt_ratio: Optional[float] = None,
) -> list[dict]:
    """检查预警项

    Returns:
        [{"name": str, "triggered": bool, "value": float, "threshold": float}, ...]
    """
    items = [
        ("商誉/净资产", goodwill_ratio, 0.30),
        ("大股东减持", major_shareholder_reduction, 0.02),
        ("客户集中度", customer_concentration, 0.50),
        ("有息负债率", debt_ratio, 0.60),
    ]
    results = []
    for name, value, threshold in items:
        triggered = value is not None and value > threshold
        results.append({
            "name": name,
            "triggered": triggered,
            "value": value,
            "threshold": threshold,
        })
    return results


def warning_count(warnings: list[dict]) -> int:
    """预警项触发的数量"""
    return sum(1 for w in warnings if w["triggered"])


# ── 结论 ────────────────────────────────────────────────

def screener_conclusion(
    red_lines: list[dict],
    warnings: list[dict],
) -> str:
    """排雷结论

    Returns:
        "✅ 通过" / "⚠️ 黄灯通过" / "❌ 终止"
    """
    if has_red_line_triggered(red_lines):
        return "❌ 终止"
    if warning_count(warnings) > 0:
        return "⚠️ 黄灯通过"
    return "✅ 通过"


# ── 基金排雷 ────────────────────────────────────────────

FUND_RED_LINES = {
    "基金管理人违规": "近3年重大违规处罚",
    "基金规模<5000万": "清盘风险",
    "跟踪误差>2%": "年化>2%",
    "折溢价率持续>±3%": "持续异常",
    "托管人重大风险": "托管出问题",
}


def fund_screener_check(
    management_violation: bool = False,
    scale_below_50m: bool = False,
    tracking_error_above_2pct: bool = False,
    premium_discount_abnormal: bool = False,
    custodian_risk: bool = False,
) -> tuple[str, list[dict]]:
    """基金排雷

    Returns:
        (结论, 明细)
    """
    checks = [
        ("基金管理人违规", management_violation, FUND_RED_LINES["基金管理人违规"]),
        ("基金规模<5000万", scale_below_50m, FUND_RED_LINES["基金规模<5000万"]),
        ("跟踪误差>2%", tracking_error_above_2pct, FUND_RED_LINES["跟踪误差>2%"]),
        ("折溢价异常", premium_discount_abnormal, FUND_RED_LINES["折溢价率持续>±3%"]),
        ("托管人风险", custodian_risk, FUND_RED_LINES["托管人重大风险"]),
    ]
    triggered = any(c[1] for c in checks)
    conclusion = "❌ 终止" if triggered else "✅ 通过"
    return conclusion, [
        {"name": c[0], "triggered": c[1], "reason": c[2]} for c in checks
    ]
