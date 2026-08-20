"""V6.0 买入决策树调度器 — 5 前置条件 + 按 6 态市场强制匹配 5 分支

对齐《四维一体实战投资体系 V6.0》第二编第五步：
  ① 前置5条件确认（任一不满足即阻止买入）
  ② 市场状态是买入规则的**唯一仲裁**，6 态强制匹配唯一分支
  ③ 仓位 = 类型上限 × 市场乘数 × 乖离率乘数（position_sizing）

分支表（§6.1）：
  ┌─────────────────┬──────────────┬───────────────────────────────────┐
  │ 市场状态        │ 适用规则      │ 批次结构（占计划仓位）             │
  ├─────────────────┼──────────────┼───────────────────────────────────┤
  │ 强多头(乖离≤20%)│ 规则C 右侧跟随│ 现价20% → MA20回调30% →弱支撑20%→机动30% │
  │ 强多头(超买>20%)│ 降级规则A 等回调│ MA20 30% → 弱支撑30% → 强支撑20%  │
  │ 弱多头/震荡     │ 规则A 左侧挂单│ 弱支撑30% → 强支撑40% → 极端锚20%   │
  │ 弱空头          │ 极小仓位      │ ≤标准×20% 或禁止                   │
  │ 强空头          │ 禁止操作      │ 空仓等待                           │
  └─────────────────┴──────────────┴───────────────────────────────────┘

超买口径说明：market_state_v6 对「强多头超买」返回乘数 0（无当下买入资格），
但 V6.0 明确该态切「规则A 等回调」（限价挂单，回调到位才成交）。因此本调度器
对超买态用强多头基准乘数(1.0)计仓，成交由批次阈值的 triggered 自然约束——
回调未到位则不成交。

纯函数无副作用，输入均为已算好的派生值，表驱动可测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from StockInvestmentTool.analysis.market_state_v6 import (
    STRONG_BULL, STRONG_BULL_OVERBOUGHT, WEAK_BULL, SIDEWAYS,
    WEAK_BEAR, STRONG_BEAR,
)
from StockInvestmentTool.strategy.position_sizing import compute_position_pct

# 分支常量
BRANCH_RULE_C = "规则C"              # 右侧趋势跟随
BRANCH_RULE_A = "规则A"              # 左侧挂单
BRANCH_RULE_A_OVERBOUGHT = "降级规则A"  # 超买等回调
BRANCH_TINY = "极小仓位"
BRANCH_FORBID = "禁止操作"

# 弱空头：极小仓位 = 标准仓位 × 20%
WEAK_BEAR_EXTRA_FACTOR = 0.2

# 超买态仓位按强多头基准计（成交靠回调批次阈值约束）
SIZING_MULTIPLIER = {
    STRONG_BULL: 1.0,
    STRONG_BULL_OVERBOUGHT: 1.0,
    WEAK_BULL: 0.7,
    SIDEWAYS: 1.0,
    WEAK_BEAR: 0.5,
    STRONG_BEAR: 0.0,
}


@dataclass
class BuyTreeResult:
    market_state: str
    branch: str                    # 规则C / 规则A / 降级规则A / 极小仓位 / 禁止操作
    batches: list[dict] = field(default_factory=list)
    position_pct: float = 0.0      # 单票仓位上限（占总账户 %）
    blocked: bool = False          # 前置条件未过
    blocked_by: list[str] = field(default_factory=list)
    precondition_detail: dict = field(default_factory=dict)
    reason: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "market_state": self.market_state,
            "branch": self.branch,
            "batches": self.batches,
            "position_pct": self.position_pct,
            "blocked": self.blocked,
            "blocked_by": self.blocked_by,
            "reason": self.reason,
            "detail": self.detail,
        }


# ── 分支匹配 ────────────────────────────────────────────

def match_branch(market_state: str) -> str:
    """6 态市场 → 唯一适用分支。"""
    return {
        STRONG_BULL: BRANCH_RULE_C,
        STRONG_BULL_OVERBOUGHT: BRANCH_RULE_A_OVERBOUGHT,
        WEAK_BULL: BRANCH_RULE_A,
        SIDEWAYS: BRANCH_RULE_A,
        WEAK_BEAR: BRANCH_TINY,
        STRONG_BEAR: BRANCH_FORBID,
    }.get(market_state, BRANCH_FORBID)   # 未知/数据不足 → 禁止操作（不误买）


# ── 5 前置条件 ──────────────────────────────────────────

PRECONDITION_LABELS = {
    "screener_pass": "六步法完成且排雷通过",
    "position_below_macro_cap": "总仓位低于宏观上限",
    "circuit_breaker_ok": "年线熔断未触发",
    "valuation_ok": "估值条件满足",
    "thesis_ok": "逻辑坚实（投资逻辑未证伪）",
}


def check_preconditions(
    *,
    screener_pass: bool = True,
    position_below_macro_cap: bool = True,
    circuit_breaker_ok: bool = True,
    valuation_ok: bool = True,
    thesis_ok: bool = True,
) -> tuple[bool, list[str], dict]:
    """5 前置条件确认。

    Returns: (passed, failed_labels, detail)
      全部满足 → passed=True；任一不满足 → 阻止买入。
    """
    checks = {
        "screener_pass": screener_pass,
        "position_below_macro_cap": position_below_macro_cap,
        "circuit_breaker_ok": circuit_breaker_ok,
        "valuation_ok": valuation_ok,
        "thesis_ok": thesis_ok,
    }
    failed = [
        PRECONDITION_LABELS[k] for k, ok in checks.items() if not ok
    ]
    return (len(failed) == 0, failed, dict(checks))


# ── 各分支批次结构 ──────────────────────────────────────

def _make_batch(stage: int, label: str, threshold: Optional[float],
                ratio: float, price: Optional[float]) -> dict:
    """构造批次项；threshold<=0 视为无阈值（机动仓，不自动触发）。"""
    t = round(threshold, 2) if threshold is not None and threshold > 0 else 0.0
    triggered = False
    if t > 0 and price is not None:
        triggered = bool(price <= t)
    # 「当前价(现价)」批：立即成交
    if label.startswith("当前价"):
        triggered = True
    return {
        "stage": stage,
        "label": label,
        "threshold": t,
        "ratio": ratio,
        "triggered": triggered,
    }


def batches_for_branch(
    branch: str,
    *,
    price: Optional[float] = None,
    ma20: Optional[float] = None,
    weak_support: Optional[float] = None,
    strong_support: Optional[float] = None,
    extreme_anchor: Optional[float] = None,
) -> list[dict]:
    """按分支生成批次结构（占计划仓位的比例，比例即配置表原文）。"""
    if branch == BRANCH_RULE_C:
        return [
            _make_batch(1, "当前价(现价)", price, 0.20, price),
            _make_batch(2, "MA20回调", ma20, 0.30, price),
            _make_batch(3, "综合弱支撑", weak_support, 0.20, price),
            _make_batch(4, "机动仓位", None, 0.30, price),
        ]
    if branch == BRANCH_RULE_A_OVERBOUGHT:
        return [
            _make_batch(1, "MA20", ma20, 0.30, price),
            _make_batch(2, "综合弱支撑", weak_support, 0.30, price),
            _make_batch(3, "综合强支撑", strong_support, 0.20, price),
        ]
    if branch == BRANCH_RULE_A:
        return [
            _make_batch(1, "综合弱支撑", weak_support, 0.30, price),
            _make_batch(2, "综合强支撑", strong_support, 0.40, price),
            _make_batch(3, "极端低估锚", extreme_anchor, 0.20, price),
        ]
    return []   # 极小仓位 / 禁止操作：无批次


# ── 综合判定 ────────────────────────────────────────────

def judge_buy_tree(
    *,
    market_state: str,
    stock_type: str = "B",
    price: Optional[float] = None,
    ma20: Optional[float] = None,
    weak_support: Optional[float] = None,
    strong_support: Optional[float] = None,
    extreme_anchor: Optional[float] = None,
    bias_ratio: Optional[float] = None,
    type_upgrade: bool = False,
    paper_rich: bool = False,
    # 5 前置条件（默认全部满足）
    screener_pass: bool = True,
    position_below_macro_cap: bool = True,
    circuit_breaker_ok: bool = True,
    valuation_ok: bool = True,
    thesis_ok: bool = True,
) -> BuyTreeResult:
    """V6.0 买入决策树综合判定（纯函数）。

    流程：前置条件 → 分支匹配 → 批次结构 → 仓位上限。
    """
    branch = match_branch(market_state)

    passed, failed, checks = check_preconditions(
        screener_pass=screener_pass,
        position_below_macro_cap=position_below_macro_cap,
        circuit_breaker_ok=circuit_breaker_ok,
        valuation_ok=valuation_ok,
        thesis_ok=thesis_ok,
    )

    if not passed:
        return BuyTreeResult(
            market_state=market_state,
            branch=branch,
            blocked=True,
            blocked_by=failed,
            precondition_detail=checks,
            reason="前置条件未满足，阻止买入",
        )

    # 强空头 / 数据不足 → 禁止操作
    if branch == BRANCH_FORBID:
        return BuyTreeResult(
            market_state=market_state,
            branch=branch,
            position_pct=0.0,
            precondition_detail=checks,
            reason="强空头（或数据不足）：空仓等待",
        )

    batches = batches_for_branch(
        branch, price=price, ma20=ma20,
        weak_support=weak_support, strong_support=strong_support,
        extreme_anchor=extreme_anchor,
    )

    # 仓位：类型上限 × 市场乘数 × 乖离率乘数
    mult = SIZING_MULTIPLIER.get(market_state, 0.0)
    pos = compute_position_pct(
        stock_type=stock_type,
        market_multiplier=mult,
        bias_ratio=bias_ratio,
        paper_rich=paper_rich,
        upgrade=type_upgrade,
    )

    # 弱空头：极小仓位 = 标准 × 20%
    if branch == BRANCH_TINY:
        pos = round(pos * WEAK_BEAR_EXTRA_FACTOR, 1)
        reason = "弱空头：极小仓位（标准×20%），仅限极端低估锚机会"

    elif branch == BRANCH_RULE_C:
        reason = "强多头：规则C 右侧趋势跟随"
    elif branch == BRANCH_RULE_A_OVERBOUGHT:
        reason = "强多头超买：降级规则A 等回调（限价挂单）"
    else:
        reason = f"{market_state}：规则A 左侧挂单"

    return BuyTreeResult(
        market_state=market_state,
        branch=branch,
        batches=batches,
        position_pct=pos,
        precondition_detail=checks,
        reason=reason,
        detail={
            "market_multiplier": mult,
            "bias_ratio": bias_ratio,
            "type_upgrade": type_upgrade,
            "paper_rich": paper_rich,
        },
    )
