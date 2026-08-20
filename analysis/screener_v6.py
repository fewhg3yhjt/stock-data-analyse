"""V6.0 排雷 + 盈利能力判定器 — 第二编第一/三步

对齐《四维一体实战投资体系 V6.0》：

第一步 排雷（底线检查）：
  红线项（任一触发即终止 ❌ 不碰）:
    - 审计意见 非标准无保留意见
    - 经营现金流 近3年总和为负
    - 大股东减持 近一季度净减持 > 0.5%
    - 有息负债率(非金融) > 60%
    - 财务造假 近5年被证监会立案/处罚
  预警项（标记 ⚠️ 不否决）:
    - 商誉/净资产 > 30% （减值风险）
    - 客户集中度 前五大客户 > 50%
    - 应收账款/营业收入 > 80% → 硬约束: 盈利能力直接不及格 + 仓位上限减半
  排雷结论: ✅ 通过 / ⚠️ 黄灯通过 / ❌ 终止

第三步 盈利能力（≥4项及格含 即通过）:
  ROE:        优秀>15% | 及格8-15% | 不及格<8%
  净利润率:   优秀>10% | 及格5-10% | 不及格<5%
  净现比:     优秀>1   | 及格0.8-1 | 不及格<0.8
  毛利率趋势: 优秀上升 | 及格稳定 | 不及格下降
  应收账款/营收: <30%优 | 30-60%及格 | >80%直接判不及格(纸面富贵型)
"""

from dataclasses import dataclass, field
from typing import Optional


# ═══════════════════════════════════════════════════════════
# 排雷
# ═══════════════════════════════════════════════════════════

# 预警阈值
GOODWILL_RATIO_WARN = 0.30      # 商誉/净资产 >30%
CUSTOMER_CONC_WARN = 0.50       # 前五大客户 >50%
RECEIVABLE_REVENUE_HARD = 0.80  # 应收/营收 >80% 硬约束
DEBT_RATIO_RED = 0.60           # 有息负债率 >60% 红线
REDUCE_RED = 0.005              # 近一季度净减持 >0.5% 红线


@dataclass
class ScreenerResult:
    """排雷 + 盈利能力综合结果"""
    # 排雷
    mine_passed: bool              # True=通过(含黄灯) False=终止
    mine_status: str               # "通过" / "黄灯通过" / "终止"
    # 盈利能力
    profit_passed: bool
    red_flags: list[str] = field(default_factory=list)     # 红线触发项
    warn_flags: list[str] = field(default_factory=list)    # 预警项
    profit_ratings: dict = field(default_factory=dict)     # {指标: 优秀/及格/不及格}
    profit_passed_count: int = 0
    paper_rich: bool = False       # 纸面富贵型（应收/营收>80%）
    # 结论
    conclusion: str = ""           # 可操作/低仓位参与/不碰
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "mine_passed": self.mine_passed,
            "mine_status": self.mine_status,
            "red_flags": self.red_flags,
            "warn_flags": self.warn_flags,
            "profit_passed": self.profit_passed,
            "profit_ratings": self.profit_ratings,
            "profit_passed_count": self.profit_passed_count,
            "paper_rich": self.paper_rich,
            "conclusion": self.conclusion,
            "detail": self.detail,
        }


def check_mine(
    *,
    audit_opinion_ok: Optional[bool] = True,        # 审计意见是否标准无保留
    ocf_3y_sum: Optional[float] = None,             # 近3年经营现金流总和（元）
    holder_reduce_ratio: Optional[float] = None,    # 近一季度净减持比例（0-1）
    interest_debt_ratio: Optional[float] = None,    # 有息负债率（0-1，非金融）
    is_financial: bool = False,                     # 是否金融股（跳过有息负债率红线）
    regulator_punished: Optional[bool] = False,     # 近5年证监会立案/处罚
    goodwill_ratio: Optional[float] = None,         # 商誉/净资产（0-1）
    customer_concentration: Optional[float] = None, # 前五大客户占比（0-1）
    receivables_revenue: Optional[float] = None,    # 应收账款/营业收入（0-1）
) -> tuple[bool, str, list[str], list[str], dict]:
    """排雷判定（纯函数）。

    Returns: (mine_passed, mine_status, red_flags, warn_flags, detail)
      mine_passed False=红线触发终止；True=通过(可能黄灯)。
    """
    red: list[str] = []
    warn: list[str] = []
    detail: dict = {}

    # ── 红线项 ──
    if audit_opinion_ok is False:
        red.append("审计意见非标准无保留")
    if ocf_3y_sum is not None and ocf_3y_sum < 0:
        red.append(f"近3年经营现金流总和为负（{ocf_3y_sum:.2e}元）")
    if holder_reduce_ratio is not None and holder_reduce_ratio > REDUCE_RED:
        red.append(f"近一季度净减持 {holder_reduce_ratio*100:.2f}% > 0.5%")
    if (not is_financial and interest_debt_ratio is not None
            and interest_debt_ratio > DEBT_RATIO_RED):
        red.append(f"有息负债率 {interest_debt_ratio*100:.1f}% > 60%")
    if regulator_punished:
        red.append("近5年曾被证监会立案/处罚")

    # ── 预警项 ──
    if goodwill_ratio is not None and goodwill_ratio > GOODWILL_RATIO_WARN:
        warn.append(f"商誉/净资产 {goodwill_ratio*100:.1f}% > 30%（减值风险）")
    if customer_concentration is not None and customer_concentration > CUSTOMER_CONC_WARN:
        warn.append(f"前五大客户 {customer_concentration*100:.1f}% > 50%（单一大客户依赖）")
    if receivables_revenue is not None and receivables_revenue > RECEIVABLE_REVENUE_HARD:
        warn.append(f"应收账款/营收 {receivables_revenue*100:.1f}% > 80%（硬约束）")

    detail = {
        "audit_opinion_ok": audit_opinion_ok,
        "ocf_3y_sum": ocf_3y_sum,
        "holder_reduce_ratio": holder_reduce_ratio,
        "interest_debt_ratio": interest_debt_ratio,
        "is_financial": is_financial,
        "goodwill_ratio": goodwill_ratio,
        "customer_concentration": customer_concentration,
        "receivables_revenue": receivables_revenue,
    }

    if red:
        return False, "终止", red, warn, detail
    if warn:
        return True, "黄灯通过", red, warn, detail
    return True, "通过", red, warn, detail


# ═══════════════════════════════════════════════════════════
# 盈利能力
# ═══════════════════════════════════════════════════════════

def _rate_roe(roe: float) -> str:
    if roe >= 15:
        return "优秀"
    if roe >= 8:
        return "及格"
    return "不及格"


def _rate_net_margin(margin: float) -> str:
    if margin >= 10:
        return "优秀"
    if margin >= 5:
        return "及格"
    return "不及格"


def _rate_cash_ratio(ratio: float) -> str:
    if ratio >= 1.0:
        return "优秀"
    if ratio >= 0.8:
        return "及格"
    return "不及格"


def _rate_margin_trend(trend: str) -> str:
    return {"上升": "优秀", "稳定": "及格", "下降": "不及格"}.get(trend, "不及格")


def _rate_receivable_revenue(ratio: float) -> str:
    """应收/营收评级: <30%优 | 30-60%及格 | >80%不及格（30-60及格的中间段也算及格）"""
    if ratio < 0.30:
        return "优秀"
    if ratio <= 0.80:
        return "及格"
    return "不及格"


def assess_profitability(
    *,
    roe: Optional[float] = None,
    net_profit_margin: Optional[float] = None,
    cash_ratio: Optional[float] = None,
    gross_margin_trend: Optional[str] = None,
    receivables_revenue: Optional[float] = None,
) -> tuple[bool, dict, int, bool]:
    """盈利能力评估（纯函数）。

    Returns: (passed, ratings, passed_count, paper_rich)
      passed: 及格(含优秀)项数 ≥4 → True
      paper_rich: 应收/营收 >80% → True（直接判不及格 + 仓位减半）
    """
    ratings: dict[str, str] = {}
    if roe is not None:
        ratings["ROE"] = _rate_roe(roe)
    if net_profit_margin is not None:
        ratings["净利润率"] = _rate_net_margin(net_profit_margin)
    if cash_ratio is not None:
        ratings["净现比"] = _rate_cash_ratio(cash_ratio)
    if gross_margin_trend is not None:
        ratings["毛利率趋势"] = _rate_margin_trend(gross_margin_trend)
    if receivables_revenue is not None:
        ratings["应收账款/营收"] = _rate_receivable_revenue(receivables_revenue)

    # 应收/营收>80% 硬约束：直接不及格（纸面富贵型）
    paper_rich = (receivables_revenue is not None
                  and receivables_revenue > RECEIVABLE_REVENUE_HARD)
    if paper_rich:
        ratings["应收账款/营收"] = "不及格"

    passed_count = sum(1 for v in ratings.values() if v in ("优秀", "及格"))
    passed = passed_count >= 4 and not paper_rich
    return passed, ratings, passed_count, paper_rich


def judge_screener_v6(
    *,
    # 排雷输入
    audit_opinion_ok: Optional[bool] = True,
    ocf_3y_sum: Optional[float] = None,
    holder_reduce_ratio: Optional[float] = None,
    interest_debt_ratio: Optional[float] = None,
    is_financial: bool = False,
    regulator_punished: Optional[bool] = False,
    goodwill_ratio: Optional[float] = None,
    customer_concentration: Optional[float] = None,
    receivables_revenue: Optional[float] = None,
    # 盈利输入
    roe: Optional[float] = None,
    net_profit_margin: Optional[float] = None,
    cash_ratio: Optional[float] = None,
    gross_margin_trend: Optional[str] = None,
) -> ScreenerResult:
    """排雷 + 盈利能力综合判定（纯函数，V6.0 六步法 ①③）。

    结论规则：
      - 排雷红线触发 → ❌ 不碰
      - 排雷通过但盈利能力不及格 → 不碰（或低仓位）
      - 排雷通过 + 盈利及格 → 可操作；黄灯/纸面富贵 → 低仓位参与
    """
    mine_passed, mine_status, red, warn, detail = check_mine(
        audit_opinion_ok=audit_opinion_ok,
        ocf_3y_sum=ocf_3y_sum,
        holder_reduce_ratio=holder_reduce_ratio,
        interest_debt_ratio=interest_debt_ratio,
        is_financial=is_financial,
        regulator_punished=regulator_punished,
        goodwill_ratio=goodwill_ratio,
        customer_concentration=customer_concentration,
        receivables_revenue=receivables_revenue,
    )

    profit_passed, ratings, passed_count, paper_rich = assess_profitability(
        roe=roe,
        net_profit_margin=net_profit_margin,
        cash_ratio=cash_ratio,
        gross_margin_trend=gross_margin_trend,
        receivables_revenue=receivables_revenue,
    )

    # ── 结论 ──
    if not mine_passed:
        conclusion = "不碰"
    elif paper_rich:
        conclusion = "低仓位参与（纸面富贵型，仓位上限减半）"
    elif not profit_passed:
        conclusion = "低仓位参与（盈利能力未达及格）"
    elif mine_status == "黄灯通过":
        conclusion = "低仓位参与（排雷黄灯，需关注预警项）"
    else:
        conclusion = "可操作"

    return ScreenerResult(
        mine_passed=mine_passed,
        mine_status=mine_status,
        red_flags=red,
        warn_flags=warn,
        profit_passed=profit_passed,
        profit_ratings=ratings,
        profit_passed_count=passed_count,
        paper_rich=paper_rich,
        conclusion=conclusion,
        detail=detail,
    )
