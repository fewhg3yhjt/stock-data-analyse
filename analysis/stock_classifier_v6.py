"""V6.0 类型判定决策树 — 四步决策树 + 特殊强制条款（第二编第二步）

对齐《四维一体实战投资体系 V6.0》四步决策树：

  第一步：近4个季度扣非净利润是否持续为正？
      ├─ 否 → 第三类（科技成长/困境型）→ A类（高成长）
      └─ 是 → 第二步
  第二步：过去5年是否有超过2年扣非净利润下滑 > 30%？
      ├─ 是 → 第二类（强周期型）→ C类（强周期）
      └─ 否 → 第三步
  第三步：主营业务是否为银行/保险/券商？
      ├─ 是 → 第四类（金融杠杆型）→ D类（金融杠杆）
      └─ 否 → 第四步
  第四步：近3年营收CAGR > 25% 且 毛利率趋势向上（同比提升>1pct）？
      ├─ 是 → 第三类（科技成长型）→ A类（高成长）
      └─ 否 → 第一类（稳定价值型）→ B类（价值白马）或 E类（深度价值）

特殊强制条款：
  - 年营收 > 500亿 或 全球市占率前三 → 不得长期仅用第三类(PS)，必须双轨评估
  - 连续3年未亏损且 ROE 稳定 >15% 的周期股 → 可转为第一类
  - E类(深度价值): 第一类子类，低增长+高股息 → 由 B 类降级得出

输入财务史 fundamental_history 的派生指标，纯函数无副作用。
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StockTypeResult:
    stock_type: str          # A/B/C/D/E 类
    category: str            # 第一~四类 或 子类
    anchor_method: str       # 估值锚（PS/PE/PB/股息率三重锚）
    steps: list[str] = field(default_factory=list)   # 决策树每步结论
    flags: list[str] = field(default_factory=list)   # 特殊强制条款触发
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "stock_type": self.stock_type,
            "category": self.category,
            "anchor_method": self.anchor_method,
            "steps": self.steps,
            "flags": self.flags,
            "detail": self.detail,
        }


# 金融行业关键词（银行/保险/券商判定）
_FINANCIAL_KEYWORDS = ("银行", "保险", "证券", "券商", "信托")


def _is_financial(industry: str) -> bool:
    if not industry:
        return False
    return any(k in industry for k in _FINANCIAL_KEYWORDS)


def _deduced_classify(
    *,
    quarters_ded_positive: Optional[int],   # 近4季扣非为正的季度数
    ded_down_years: Optional[int],          # 5年内扣非下滑>30%的年数
    is_financial: bool,
    revenue_cagr_3y: Optional[float],       # 近3年营收CAGR %
    gross_margin_up: bool,                  # 毛利率同比提升>1pct
) -> tuple[str, list[str]]:
    """决策树纯逻辑，返回 (类型码, 每步结论)。

    供 classifier_v6 与测试直接调用（测试用 dict 输入，不构造 df）。
    """
    steps: list[str] = []

    # 第一步：近4季扣非为正
    if quarters_ded_positive is not None and quarters_ded_positive < 4:
        steps.append(f"第①步: 近4季扣非为正 {quarters_ded_positive}/4 → 否 → 第三类(困境型)")
        return "A", steps

    # 第二步：5年>2年扣非下滑>30%
    if ded_down_years is not None and ded_down_years > 2:
        steps.append(f"第②步: 5年扣非下滑>30%共{ded_down_years}年(>2) → 是 → 第二类(强周期)")
        return "C", steps

    # 第三步：金融判定
    if is_financial:
        steps.append("第③步: 主营业务为银行/保险/券商 → 是 → 第四类(金融杠杆)")
        return "D", steps

    # 第四步：营收CAGR>25% 且 毛利率向上
    steps.append("第③步: 非金融 → 否 → 第④步")
    if (revenue_cagr_3y is not None and revenue_cagr_3y > 25) and gross_margin_up:
        steps.append(f"第④步: 营收CAGR {revenue_cagr_3y:.1f}% > 25% 且 毛利率向上 → 是 → 第三类(科技成长)")
        return "A", steps

    steps.append("第④步: 非高成长 → 否 → 第一类(稳定价值)")
    return "B", steps


def classify_stock(
    *,
    quarters_ded_positive: Optional[int] = None,
    ded_down_years: Optional[int] = None,
    industry: Optional[str] = None,
    revenue_cagr_3y: Optional[float] = None,
    gross_margin_up: bool = False,
    annual_revenue: Optional[float] = None,   # 元
    roe_recent_3y: Optional[list[float]] = None,  # 近3年ROE
    losses_3y: bool = False,                  # 近3年是否曾亏损
    dividend_yield: Optional[float] = None,   # 股息率 %
) -> StockTypeResult:
    """V6.0 类型判定决策树（纯函数，参数均为已算好的派生指标）。

    Parameters
    ----------
    quarters_ded_positive : 近4季扣非净利润为正的季度数（0-4），None=数据不足
    ded_down_years : 5年内扣非同比下滑>30%的年数
    industry : 主营业务（用于金融判定）
    revenue_cagr_3y : 近3年营收 CAGR（%）
    gross_margin_up : 毛利率趋势是否向上（同比提升>1pct）
    annual_revenue : 最新年营收（元），触发 500亿 强制条款
    roe_recent_3y : 近3年 ROE 列表，触发周期转价值条款
    losses_3y : 近3年是否曾亏损
    dividend_yield : 股息率（%），决定 B/E 子类
    """
    is_fin = _is_financial(industry)
    stock_type, steps = _deduced_classify(
        quarters_ded_positive=quarters_ded_positive,
        ded_down_years=ded_down_years,
        is_financial=is_fin,
        revenue_cagr_3y=revenue_cagr_3y,
        gross_margin_up=gross_margin_up,
    )

    flags: list[str] = []
    anchor_map = {
        "A": ("高成长(第三类)", "PS + 营收增速"),
        "C": ("强周期(第二类)", "PB + 产品价格"),
        "D": ("金融杠杆(第四类)", "PB + ROE"),
        "B": ("价值白马(第一类)", "PE/PEG + 股息率"),
    }

    # ── 特殊强制条款 ──
    # 年营收 > 500亿 → 不得长期仅用第三类(PS)，必须双轨评估
    if annual_revenue is not None and annual_revenue > 500e8:  # 500亿元
        if stock_type == "A":
            flags.append("年营收>500亿: 不得长期仅用PS体系，必须双轨评估(PE/PB为主)")
            anchor_map["A"] = ("高成长(第三类)", "双轨评估: PE/PB为主 + PS参考")

    # 连续3年未亏损且 ROE 稳定>15% 的周期股 → 可转第一类
    if stock_type == "C" and roe_recent_3y and not losses_3y:
        if len(roe_recent_3y) >= 3 and all(r > 15 for r in roe_recent_3y[-3:]):
            flags.append("连续3年未亏损且ROE>15%: 周期股可转为第一类(价值)")
            stock_type = "B"
            anchor_map["B"] = ("价值白马(第一类)", "PE/PEG + 股息率")

    # ── B 类子类: E类(深度价值) — 低增长+高股息
    detail = {
        "quarters_ded_positive": quarters_ded_positive,
        "ded_down_years": ded_down_years,
        "industry": industry,
        "revenue_cagr_3y": revenue_cagr_3y,
        "gross_margin_up": gross_margin_up,
        "dividend_yield": dividend_yield,
    }
    if stock_type == "B" and dividend_yield is not None and dividend_yield >= 3.4:
        flags.append(f"股息率 {dividend_yield:.1f}% ≥ 3.4%: 深度价值子类")
        stock_type = "E"
        anchor_map["E"] = ("深度价值(第一类子类)", "股息率三重锚")

    category, anchor = anchor_map.get(stock_type, ("", ""))
    return StockTypeResult(
        stock_type=stock_type,
        category=category,
        anchor_method=anchor,
        steps=steps,
        flags=flags,
        detail=detail,
    )


def classify_from_fundamental_history(hist, *, industry: str = "",
                                      dividend_yield: Optional[float] = None,
                                      annual_revenue: Optional[float] = None) -> StockTypeResult:
    """从 fundamental_history（一行一季）派生指标并调用决策树。

    hist 需含列: stat_date, net_profit_ded, revenue（可选 revenue_yoy / 毛利率）。
    dividend_yield / annual_revenue 缺省走人工/默认。
    """
    if hist is None or hist.empty:
        return StockTypeResult("数据不足", "数据不足", "", steps=["无财务史数据"])

    hist = hist.sort_values("stat_date")
    ded = hist["net_profit_ded"] if "net_profit_ded" in hist else None
    rev = hist["revenue"] if "revenue" in hist else None
    gm = hist["gross_margin"] if "gross_margin" in hist else None

    # 近4季扣非为正
    quarters_ded_positive = None
    if ded is not None and len(ded) >= 4:
        recent4 = ded.tail(4).dropna()
        quarters_ded_positive = int((recent4 > 0).sum())

    # 5年扣非下滑>30%年数（每年取12月末为年值，无年度数据则按季度同比近似）
    ded_down_years = None
    if ded is not None and len(ded) >= 20:
        # 按年聚合: 取每年最后一条非空为年值
        year_vals = []
        hist2 = hist.copy()
        hist2["_y"] = hist2["stat_date"].dt.year
        for _, g in hist2.groupby("_y"):
            yv = g["net_profit_ded"].dropna()
            if not yv.empty:
                year_vals.append((int(_), float(yv.iloc[-1])))
        if len(year_vals) >= 2:
            downs = 0
            for i in range(len(year_vals)):
                prev = year_vals[i - 1][1]
                cur = year_vals[i][1]
                if i > 0 and prev > 0 and (prev - cur) / prev > 0.30:
                    downs += 1
            ded_down_years = downs

    # 近3年营收 CAGR
    revenue_cagr_3y = None
    if rev is not None and len(rev.dropna()) >= 4 * 3:
        rv = rev.dropna()
        first, last = rv.iloc[0], rv.iloc[-1]
        n_years = max(len(rv) / 4.0, 1.0)
        if first > 0 and last > 0:
            revenue_cagr_3y = ((last / first) ** (1 / n_years) - 1) * 100

    # 毛利率趋势向上（最新毛利率 vs 去年同期提升>1pct）
    gross_margin_up = False
    if gm is not None and len(gm.dropna()) >= 5:
        gv = gm.dropna()
        if len(gv) >= 5 and gv.iloc[-1] - gv.iloc[-5] > 1.0:
            gross_margin_up = True

    return classify_stock(
        quarters_ded_positive=quarters_ded_positive,
        ded_down_years=ded_down_years,
        industry=industry,
        revenue_cagr_3y=revenue_cagr_3y,
        gross_margin_up=gross_margin_up,
        annual_revenue=annual_revenue,
        dividend_yield=dividend_yield,
    )
