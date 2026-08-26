"""买后操作顾问 — 核心决策引擎

输入: Position + 最新K线 + 参考价格
输出: ActionAdvice

检查顺序（优先级从高到低）:
  ① 硬止损       → sell_all (urgent)
  ② 技术止损     → sell_all (urgent)
  ③ 左侧固定止盈 → partial_sell
  ④ 右侧移动止盈 → sell_all
  ⑤ 后续批次买入 → buy_more
  ⑥ 规则C趋势   → buy_more
  否则          → hold

关键设计: 策略规则（快照锁定） × 参考价格（每次从最新行情重算）。
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from StockInvestmentTool.core.registry import SchemeRegistry
from StockInvestmentTool.portfolio.models import (
    ActionAdvice,
    Position,
    PHASE_ACCUMULATING,
    PHASE_HOLDING,
    PHASE_LEFT_SIDE,
    PHASE_RIGHT_SIDE,
    ADVICE_BUY_MORE,
    ADVICE_PARTIAL_SELL,
    ADVICE_SELL_ALL,
    ADVICE_HOLD,
    ADVICE_ADJUST_STOP,
    load_snapshot_scheme,
)
from StockInvestmentTool.strategy.multi_buy import MultiBuyStrategy
from StockInvestmentTool.strategy.take_profit import (
    left_side_sell_action,
    right_side_sell_action,
)
from StockInvestmentTool.strategy.market_state import determine_market_state_from_df
from StockInvestmentTool.strategy.stock_classifier import classify_stock
from StockInvestmentTool.datasource.indicators import TechnicalIndicators
from StockInvestmentTool.indicators.context import IndicatorContext
from StockInvestmentTool.strategy.position_state import PositionStateMachine, EVENT_BREAKOUT

logger = logging.getLogger(__name__)


class AdvisorContext:
    """一次分析计算出的参考价格集合（每次刷新时重算）"""

    def __init__(self):
        self.row = None
        self.current_price = 0.0
        self.recent_low = 0.0            # 最近交易日最低价
        self.year_high = 0.0             # 滚动252日最高价
        self.weak_support = 0.0
        self.strong_support = 0.0
        self.extreme_anchor = 0.0
        self.ma_20 = 0.0
        self.ma_60 = 0.0
        self.volume_ma_5 = 0.0
        self.last_volume = 0.0
        self.trend = ""
        self.market_state = ""
        self.rebound_from_month_low = 0.0
        self.indicators: dict = {}          # 指标体系计算结果（可配置/组合指标）
        self._dynamic_weak_support = False  # v4.8 基金动态弱支撑（MA20 切换）标记

    def to_dict(self) -> dict:
        return {
            "current_price": self.current_price,
            "recent_low": self.recent_low,
            "year_high": self.year_high,
            "weak_support": self.weak_support,
            "strong_support": self.strong_support,
            "extreme_anchor": self.extreme_anchor,
            "ma_20": self.ma_20,
            "ma_60": self.ma_60,
            "volume_ma_5": self.volume_ma_5,
            "last_volume": self.last_volume,
            "trend": self.trend,
            "market_state": self.market_state,
            "rebound_from_month_low": self.rebound_from_month_low,
            "indicators": self.indicators,
            "dynamic_weak_support": self._dynamic_weak_support,
        }


class PostPurchaseAdvisor:
    """买后操作顾问

    Args:
        storage: 持仓存储
        registry: 方案注册中心（用于快照无效时回退加载当前方案）
    """

    def __init__(self, storage=None, registry: Optional[SchemeRegistry] = None):
        from StockInvestmentTool.portfolio.storage import PortfolioStorage
        self.storage = storage or PortfolioStorage()
        self.registry = registry or SchemeRegistry()
        self.state_machine = PositionStateMachine()

    # ── 参考价格计算 ─────────────────────────────────

    def compute_context(self, kline: pd.DataFrame,
                        dividend_anchor: Optional[float] = None,
                        scheme_snapshot: Optional[dict] = None) -> AdvisorContext:
        """从最新 K 线计算全部参考价格"""
        ctx = AdvisorContext()

        if kline is None or len(kline) == 0:
            return ctx

        last = kline.iloc[-1]
        ctx.row = last
        ctx.current_price = float(last.get("close", 0))
        ctx.recent_low = float(last.get("low", 0))
        ctx.last_volume = float(last.get("volume", 0))
        ctx.volume_ma_5 = float(last.get("volume_ma_5", 0)) if "volume_ma_5" in kline.columns else 0
        ctx.year_high = float(kline["high"].tail(252).max())
        ctx.ma_20 = float(last.get("ma_20", 0)) if "ma_20" in kline.columns else 0
        ctx.ma_60 = float(last.get("ma_60", 0)) if "ma_60" in kline.columns else 0

        # 用统一 IndicatorContext 计算完整指标（可配置/可组合），补充到 context
        # （FR-1.3：全库唯一指标求值入口，替代散落 hardcode 计算）
        self._ind_ctx = None
        try:
            from StockInvestmentTool.indicators.engine import IndicatorRegistry
            self._ind_ctx = IndicatorContext(kline)
            reg = IndicatorRegistry()
            ind_values = self._ind_ctx.latest(list(reg.all_names()))
            ctx.indicators = {k: v for k, v in ind_values.items() if v is not None}
        except Exception as e:
            logger.debug("指标体系计算失败: %s", e)

        # 趋势与市场状态
        try:
            ctx.trend = TechnicalIndicators.trend_judgment(kline)
        except Exception:
            ctx.trend = ""
        try:
            ctx.market_state = determine_market_state_from_df(kline)
        except Exception:
            ctx.market_state = ""

        # 从近1月低点反弹幅度
        try:
            recent_1m = kline["low"].tail(21).min()
            if recent_1m > 0:
                ctx.rebound_from_month_low = (ctx.current_price - recent_1m) / recent_1m * 100
        except Exception:
            pass

        # 支撑位（FR-1.2 统一骨架，替代 MultiBuyStrategy 内部实现）
        try:
            scheme = (load_snapshot_scheme(scheme_snapshot) if scheme_snapshot else None)
            strategy = MultiBuyStrategy(
                dividend_anchor=dividend_anchor, scheme=scheme,
            )
            from StockInvestmentTool.strategy.support import RowContext
            weak, strong, extreme = strategy._get_support_levels(
                last, IndicatorContext(kline)
            )
            ctx.weak_support, ctx.strong_support, ctx.extreme_anchor = weak, strong, extreme
        except Exception as e:
            logger.warning("支撑位计算失败: %s", e)

        return ctx

    # ── 各规则检查 ───────────────────────────────────

    def _check_hard_stop(self, position: Position, ctx: AdvisorContext,
                         scheme, check_results: dict) -> Optional[ActionAdvice]:
        """① 硬止损: 最近最低价 ≤ 均价 × (1 - 扣减率)"""
        if position.avg_cost <= 0:
            return None
        rule = scheme.find_sell_rule("hard_stop")
        params = rule.params if rule is not None else {
            "stop_loss_by_type": {position.stock_type: self._stop_loss_rate(scheme, position.stock_type)}
        }
        from StockInvestmentTool.strategy.context import RuleContext
        from StockInvestmentTool.strategy.rule_registry import dispatch_rule

        result = dispatch_rule("sell", "hard_stop", RuleContext(
            row=ctx.row,
            avg_cost=position.avg_cost,
            current_price=ctx.current_price,
            extra={"stock_type": position.stock_type},
        ), params)
        detail = result.detail or {}
        stop = float(detail.get("stop_price", position.avg_cost * 0.85))
        triggered = bool(result.triggered)
        check_results["hard_stop"] = {
            "stop_price": round(stop, 2),
            "recent_low": round(ctx.recent_low, 2),
            "triggered": triggered,
        }
        if triggered:
            return ActionAdvice(
                position_id=position.id, stock_code=position.stock_code,
                stock_name=position.stock_name,
                advice_type=ADVICE_SELL_ALL, urgency="urgent",
                reason=f"⚠️ 硬止损触发：最近最低价{ctx.recent_low:.2f} ≤ 止损线{stop:.2f}"
                        f"（成本{position.avg_cost:.2f} × {1-stop/position.avg_cost:.0%}）",
                suggested_price=ctx.current_price,
                suggested_shares=position.total_shares,
                suggested_amount=position.market_value,
                check_results=dict(check_results),
            )
        return None

    def _check_technical_stop(self, position: Position, ctx: AdvisorContext,
                              scheme, check_results: dict) -> Optional[ActionAdvice]:
        """② 技术止损: 最近最低价 < 强支撑 且 放量"""
        surge_th = getattr(scheme.risk, "volume_surge_threshold", 1.8)
        vol_surge = ctx.volume_ma_5 > 0 and ctx.last_volume > ctx.volume_ma_5 * surge_th
        triggered = ctx.strong_support > 0 and ctx.recent_low < ctx.strong_support and vol_surge
        check_results["technical_stop"] = {
            "strong_support": round(ctx.strong_support, 2),
            "recent_low": round(ctx.recent_low, 2),
            "volume_surge": vol_surge,
            "triggered": triggered,
        }
        if triggered:
            return ActionAdvice(
                position_id=position.id, stock_code=position.stock_code,
                stock_name=position.stock_name,
                advice_type=ADVICE_SELL_ALL, urgency="urgent",
                reason=f"⚠️ 技术止损触发：最低价{ctx.recent_low:.2f} 放量跌破强支撑{ctx.strong_support:.2f}",
                suggested_price=ctx.current_price,
                suggested_shares=position.total_shares,
                suggested_amount=position.market_value,
                check_results=dict(check_results),
            )
        return None

    def _check_left_side(self, position: Position, ctx: AdvisorContext,
                         scheme, check_results: dict) -> Optional[ActionAdvice]:
        """③ 左侧固定止盈: 前高90-100%区间分档减持"""
        if position.position_phase not in (PHASE_ACCUMULATING, PHASE_HOLDING):
            return None

        rule = scheme.find_sell_rule("left_side_fixed")
        zones = None
        sell_ratio = None
        half_profit = None
        if rule is not None:
            params = rule.params or {}
            z = params.get("zones")
            if isinstance(z, list) and z:
                zones = {}
                for item in z:
                    rng = item.get("range", [])
                    lo = float(rng[0]) if len(rng) > 0 else 0.0
                    hi = float(rng[1]) if len(rng) > 1 and rng[1] is not None else None
                    zones[item.get("name", "zone")] = (lo, hi)
                sell_ratio = float(z[0].get("sell_ratio", 0.20))
            if "half_profit_threshold" in params:
                half_profit = float(params["half_profit_threshold"])

        tier, ratio = left_side_sell_action(
            current_price=ctx.current_price,
            avg_cost=position.avg_cost,
            year_high=ctx.year_high,
            left_tier_sold=position.left_tier_sold,
            zones=zones, sell_ratio=sell_ratio, half_profit=half_profit,
        )

        check_results["left_side"] = {
            "year_high": round(ctx.year_high, 2),
            "pct_of_year_high": round(ctx.current_price / ctx.year_high * 100, 1) if ctx.year_high > 0 else 0,
            "tier": tier,
            "sell_ratio": ratio,
            "left_tier_sold": position.left_tier_sold,
        }

        # 补充止盈区间的具体价格范围（前高×区间边界），便于通知展示"怎么得来的"
        if ctx.year_high > 0 and zones:
            lo_pct = zones.get("预警区", (0.90, 0.95))[0] if "预警区" in zones else 0.90
            hi_pct = zones.get("第一止盈区", (0.95, 1.00))[1] if "第一止盈区" in zones else 1.00
            check_results["left_side"]["zone_price_lo"] = round(ctx.year_high * lo_pct, 2)
            check_results["left_side"]["zone_price_hi"] = round(ctx.year_high * hi_pct, 2)
        # 建议减仓股数（当前档位的减持数量）
        if tier in (1, 2) and position.left_tier_sold < tier and ratio > 0:
            check_results["left_side"]["sell_shares"] = round(position.total_shares * ratio, 0)

        if tier in (1, 2) and position.left_tier_sold < tier:
            sell_shares = round(position.total_shares * ratio, 2)
            tier_label = {1: "预警区", 2: "第一止盈区"}.get(tier, f"第{tier}档")
            return ActionAdvice(
                position_id=position.id, stock_code=position.stock_code,
                stock_name=position.stock_name,
                advice_type=ADVICE_PARTIAL_SELL, urgency="attention",
                reason=f"📈 左侧止盈({tier_label})：当前价{ctx.current_price:.2f}"
                       f"达前高{ctx.year_high:.2f}的{ctx.current_price/ctx.year_high*100:.1f}%，建议减持{ratio*100:.0f}%",
                suggested_price=ctx.current_price,
                suggested_shares=sell_shares,
                suggested_amount=round(sell_shares * ctx.current_price, 2),
                check_results=dict(check_results),
            )
        if tier == 3:
            check_results["left_side"]["transition"] = True
            # 突破前高 → 建议转右侧，记录状态但不强制操作
            if position.left_tier_sold >= 1:
                if self.state_machine.can(position.position_phase, EVENT_BREAKOUT):
                    position.position_phase = self.state_machine.transition(
                        position.position_phase, EVENT_BREAKOUT
                    )
        return None

    def _check_right_side(self, position: Position, ctx: AdvisorContext,
                          scheme, check_results: dict) -> Optional[ActionAdvice]:
        """④ 右侧移动止盈: 突破后从峰值回撤"""
        if position.position_phase not in (PHASE_LEFT_SIDE, PHASE_RIGHT_SIDE):
            return None

        rule = scheme.find_sell_rule("right_side_trailing")
        drawdown_by_type = None
        if rule is not None:
            dd = (rule.params or {}).get("drawdown_by_type")
            if isinstance(dd, dict) and dd:
                drawdown_by_type = {k: float(v) for k, v in dd.items()}

        should_sell = right_side_sell_action(
            peak_price=position.peak_price,
            current_price=ctx.current_price,
            stock_type=position.stock_type,
            drawdown_by_type=drawdown_by_type,
        )
        dd_pct = (position.peak_price - ctx.current_price) / position.peak_price * 100 \
            if position.peak_price > 0 else 0

        # 右侧止盈触发线 = 峰值 × (1 - 回撤阈值)；跌破即触发清仓
        dd_threshold = None
        if drawdown_by_type:
            dd_threshold = drawdown_by_type.get(position.stock_type)
        trigger_price = round(
            position.peak_price * (1 - dd_threshold), 2
        ) if dd_threshold and position.peak_price > 0 else None

        check_results["right_side"] = {
            "peak_price": round(position.peak_price, 2),
            "drawdown_pct": round(dd_pct, 2),
            "trigger_price": trigger_price,          # 右侧止盈触发线（跌破即清仓）
            "triggered": should_sell,
        }
        if should_sell:
            return ActionAdvice(
                position_id=position.id, stock_code=position.stock_code,
                stock_name=position.stock_name,
                advice_type=ADVICE_SELL_ALL, urgency="attention",
                reason=f"📉 右侧移动止盈触发：从峰值{position.peak_price:.2f}回撤{dd_pct:.1f}%，建议清仓",
                suggested_price=ctx.current_price,
                suggested_shares=position.total_shares,
                suggested_amount=position.market_value,
                check_results=dict(check_results),
            )
        return None

    def _check_buy_more(self, position: Position, ctx: AdvisorContext,
                        scheme, check_results: dict) -> Optional[ActionAdvice]:
        """⑤ 后续批次买入: 当前价 ≤ 下一批次触发价"""
        if position.buy_stage >= 3 or position.position_phase == PHASE_RIGHT_SIDE:
            return None

        rule = scheme.find_buy_rule("support_level")
        stages = []
        if rule is not None:
            stages = (rule.params or {}).get("buy_stages", [])
        if not stages:
            return None

        # 下一批次 = buy_stage（0-indexed 下一个未买的批次）
        if position.buy_stage >= len(stages):
            return None
        next_stage = stages[position.buy_stage]
        trigger = self._stage_trigger(next_stage, ctx, scheme)

        check_results["buy_more"] = {
            "next_stage": position.buy_stage + 1,
            "label": next_stage.get("label", ""),
            "trigger_price": round(trigger, 2),
            "current_price": round(ctx.current_price, 2),
            "triggered": trigger > 0 and ctx.current_price <= trigger,
        }
        if trigger > 0 and ctx.current_price <= trigger:
            ratio = float(next_stage.get("ratio", 0.20))
            suggested_shares = round(self._buy_amount(position, ratio) / ctx.current_price, 2) \
                if ctx.current_price > 0 else 0
            return ActionAdvice(
                position_id=position.id, stock_code=position.stock_code,
                stock_name=position.stock_name,
                advice_type=ADVICE_BUY_MORE, urgency="attention",
                reason=f"💰 第{position.buy_stage + 1}批买入触发：当前价{ctx.current_price:.2f}"
                       f" ≤ {next_stage.get('label', '')}({trigger:.2f})，建议买入约{ratio*100:.0f}%仓位",
                suggested_price=trigger,
                suggested_shares=suggested_shares,
                suggested_amount=round(suggested_shares * ctx.current_price, 2),
                check_results=dict(check_results),
            )
        return None

    def _check_rule_c(self, position: Position, ctx: AdvisorContext,
                      scheme, check_results: dict) -> Optional[ActionAdvice]:
        """⑥ 规则C: 趋势跟随加仓"""
        rule = scheme.find_buy_rule("trend_following")
        if rule is None:
            return None
        from StockInvestmentTool.strategy.multi_buy import MultiBuyStrategy
        triggered, failed = MultiBuyStrategy.rule_c_triggered(
            trend=ctx.trend,
            market_state=ctx.market_state,
            rebound_from_month_low=ctx.rebound_from_month_low,
            stock_type=position.stock_type,
        )
        check_results["rule_c"] = {"triggered": triggered, "failed": failed}
        if triggered and position.buy_stage < 3:
            ratio = 0.20
            suggested_shares = round(self._buy_amount(position, ratio) / ctx.current_price, 2) \
                if ctx.current_price > 0 else 0
            return ActionAdvice(
                position_id=position.id, stock_code=position.stock_code,
                stock_name=position.stock_name,
                advice_type=ADVICE_BUY_MORE, urgency="normal",
                reason=f"🚀 规则C趋势跟随触发：多头排列 + {ctx.market_state}，建议加仓",
                suggested_price=ctx.current_price,
                suggested_shares=suggested_shares,
                suggested_amount=round(suggested_shares * ctx.current_price, 2),
                check_results=dict(check_results),
            )
        return None

    # ── 辅助方法 ─────────────────────────────────────

    @staticmethod
    def _stop_loss_rate(scheme, stock_type: str) -> float:
        """从方案获取止损扣减率"""
        rule = scheme.find_sell_rule("hard_stop")
        if rule is not None:
            by_type = (rule.params or {}).get("stop_loss_by_type")
            if isinstance(by_type, dict) and by_type:
                return float(by_type.get(stock_type, 0.10 if stock_type == "E" else 0.15))
        if scheme.risk.stop_loss_by_type:
            return float(scheme.risk.stop_loss_by_type.get(stock_type, 0.10 if stock_type == "E" else 0.15))
        # ETF/场内基金默认扣减 10%（=90% 止损，基金框架 v4.8：宽基/主动 90%）
        return 0.10 if stock_type == "E" else 0.15

    @staticmethod
    def _stage_trigger(stage: dict, ctx: AdvisorContext, scheme) -> float:
        """计算批次触发价"""
        use_special = stage.get("use_special")
        if use_special == "dividend_anchor_4pct":
            return ctx.extreme_anchor
        pos = stage.get("position_index")
        levels = [ctx.strong_support, ctx.weak_support]
        if isinstance(pos, int) and 0 <= pos < len(levels):
            return levels[pos]
        label = stage.get("label", "")
        if "弱" in label:
            return ctx.weak_support
        if "强" in label:
            return ctx.strong_support
        if "极端" in label:
            return ctx.extreme_anchor
        return 0

    @staticmethod
    def _buy_amount(position: Position, ratio: float) -> float:
        """建议买入金额（简化：按当前市值 × 比例）"""
        return position.market_value * ratio

    # ── 主入口 ───────────────────────────────────────

    def analyze_position(self, position: Position, kline: pd.DataFrame,
                         dividend_anchor: Optional[float] = None,
                         current_price: Optional[float] = None) -> ActionAdvice:
        """分析单个持仓，返回操作建议"""
        # 平仓/无仓位不分析
        if position.status != "open" or position.total_shares <= 0:
            return ActionAdvice(
                position_id=position.id, stock_code=position.stock_code,
                stock_name=position.stock_name,
                advice_type=ADVICE_HOLD, reason="持仓已平仓或无仓位",
            )

        scheme = load_snapshot_scheme(position.scheme_snapshot)
        if scheme is None:
            # 快照无效 → 回退到当前方案
            try:
                scheme = self.registry.get(position.scheme_name)
            except Exception:
                return ActionAdvice(
                    position_id=position.id, stock_code=position.stock_code,
                    stock_name=position.stock_name,
                    advice_type=ADVICE_ADJUST_STOP, urgency="urgent",
                    reason=f"⚠️ 方案 '{position.scheme_name}' 加载失败，无法生成建议。请检查方案配置或升级方案。",
                )

        ctx = self.compute_context(kline, dividend_anchor, position.scheme_snapshot)
        if current_price:
            ctx.current_price = current_price
        if ctx.current_price > position.peak_price:
            position.peak_price = ctx.current_price  # 更新峰值

        # v4.8 动态弱支撑（基金/ETF 专用）: 牛市初/中期 + 现价>静态弱支撑 + MA20有值
        # → 弱支撑动态切换为 MA20（趋势跟随），强支撑保持静态（极限防守线）
        if position.stock_type == "E" and ctx.ma_20 > 0 and ctx.weak_support > 0 \
                and ctx.current_price > ctx.weak_support \
                and ctx.market_state in ("牛市初期", "牛市中期"):
            ctx.weak_support = ctx.ma_20
            ctx._dynamic_weak_support = True  # 标记供前端/日志展示

        check_results: dict = {}
        # 保存"当时计算的指标"快照，供每日操作日志追溯（当前价/支撑位/均线/趋势等）
        check_results["context"] = ctx.to_dict()

        # 优先级: 止损 > 止盈 > 加仓
        for check in (self._check_hard_stop, self._check_technical_stop,
                      self._check_left_side, self._check_right_side,
                      self._check_buy_more, self._check_rule_c):
            advice = check(position, ctx, scheme, check_results)
            if advice is not None:
                advice.check_results = check_results
                return advice

        # 全部未触发 → 持有
        return ActionAdvice(
            position_id=position.id, stock_code=position.stock_code,
            stock_name=position.stock_name,
            advice_type=ADVICE_HOLD, urgency="normal",
            reason=f"当前价{ctx.current_price:.2f}，成本{position.avg_cost:.2f}，"
                   f"浮动盈亏{position.unrealized_pnl_pct:+.2f}%。所有策略规则均未触发，继续持有。",
            suggested_price=position.stop_loss_price,
            check_results=check_results,
        )
