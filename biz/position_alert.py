# -*- coding: utf-8 -*-
"""持仓目标价通知策略。

依据 docs/ADVICE_AND_NOTIFICATION_DESIGN.md。
- 为单只持仓股票配置目标价规则（rule_type + direction + threshold_pct），存 position_alert_rules，
  以 symbol（股票代码，如 sh510300）关联，评估时按持仓运行状态里的 symbol 匹配规则。
- rule_type:
    high_pct          目标价 = 后高 × threshold_pct%
    high_minus_cost_pct 目标价 = (后高 - 成本) × threshold_pct% + 成本
- direction:
    below  现价 ≤ 目标价（回撤/跌破提醒）
    above  现价 ≥ 目标价（冲高/上行提醒）
- 评估时用 position_runtime_states 中已算好的 highest_since_entry（后高）与
  average_cost（成本），命中即生成去重 NotificationEvent，由 outbox 投递 Email。
- 通知只产生事件，不改变真实持仓。
"""

from __future__ import annotations

import logging
import os

from StockInvestmentTool.biz.models import now_utc, new_id

logger = logging.getLogger(__name__)

POSITION_PRICE_BELOW_EVENT_TYPE = "POSITION_PRICE_BELOW"
POSITION_PRICE_ABOVE_EVENT_TYPE = "POSITION_PRICE_ABOVE"

# 合法规则类型 / 方向
RULE_TYPES = {"high_pct", "high_minus_cost_pct"}
DIRECTIONS = {"below", "above"}


# ── 符号规范化 ────────────────────────────────────────────

def normalize_symbol(symbol: str) -> str:
    """将股票代码规范化为无点格式（sh600900），与 position_runtime 一致。

    复用数据模块的 normalize_minute_code（= normalize_code().replace('.','').lower()），
    避免前端/各模块因带点/不带点不一致导致规则匹配失败。
    """
    from StockInvestmentTool.warehouse.minute import normalize_minute_code

    symbol = str(symbol or "").strip().lower()
    if not symbol:
        return ""
    try:
        return normalize_minute_code(symbol)
    except Exception:  # noqa: BLE001
        return symbol.replace(".", "").lower()


# ── 规则 CRUD ──────────────────────────────────────────────

def list_rules(repo=None, symbol: str | None = None) -> list[dict]:
    """查询持仓目标价规则；symbol 为空则全部。"""
    from StockInvestmentTool.biz.repo import BusinessRepository

    repo = repo or BusinessRepository()
    symbol = normalize_symbol(symbol) if symbol else None
    if symbol:
        rows = repo.db.fetchall(
            "SELECT * FROM position_alert_rules WHERE symbol=? ORDER BY created_at",
            (symbol,),
        )
    else:
        rows = repo.db.fetchall("SELECT * FROM position_alert_rules ORDER BY symbol, created_at")
    return [dict(r) for r in rows]


def add_rule(repo=None, *, symbol: str, rule_type: str, direction: str,
             threshold_pct: float, note: str = "", enabled: bool = True) -> dict:
    """新增一条目标价规则；返回规则 dict。"""
    from StockInvestmentTool.biz.repo import BusinessRepository

    if rule_type not in RULE_TYPES:
        raise ValueError(f"未知规则类型: {rule_type}（可选 {sorted(RULE_TYPES)}）")
    if direction not in DIRECTIONS:
        raise ValueError(f"未知方向: {direction}（可选 {sorted(DIRECTIONS)}）")
    try:
        threshold_pct = float(threshold_pct)
    except (TypeError, ValueError):
        raise ValueError(f"非法阈值: {threshold_pct}")
    if not 0 < threshold_pct <= 100:
        raise ValueError("阈值需在 (0, 100] 区间")
    symbol = normalize_symbol(symbol)
    if not symbol:
        raise ValueError("symbol 不能为空")

    repo = repo or BusinessRepository()
    rule = {
        "rule_id": new_id("par"),
        "symbol": symbol,
        "rule_type": rule_type,
        "direction": direction,
        "threshold_pct": threshold_pct,
        "enabled": 1 if enabled else 0,
        "note": note,
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }
    repo.db.insert("position_alert_rules", rule)
    return rule


def delete_rule(repo, rule_id: str) -> bool:
    """删除一条规则；返回是否删除成功。"""
    from StockInvestmentTool.biz.repo import BusinessRepository

    repo = repo or BusinessRepository()
    repo.db.execute("DELETE FROM position_alert_rules WHERE rule_id=?", (rule_id,))
    return True


def set_rule_enabled(repo, rule_id: str, enabled: bool) -> bool:
    """启停一条规则。"""
    from StockInvestmentTool.biz.repo import BusinessRepository

    repo = repo or BusinessRepository()
    repo.db.update("position_alert_rules",
                   {"enabled": 1 if enabled else 0, "updated_at": now_utc()},
                   "rule_id=?", (rule_id,))
    return True


# ── 目标价计算 ────────────────────────────────────────────

def _state_value(state: dict, key: str):
    """读取持仓状态字段；优先顶层，回退 data_context（average_cost 存于 context）。"""
    if state.get(key) is not None:
        return state.get(key)
    context = state.get("data_context") or {}
    return context.get(key)


def target_price(rule: dict, state: dict) -> float | None:
    """由规则 + 持仓运行状态算目标价；数据缺失返回 None。"""
    highest = _state_value(state, "highest_since_entry")
    cost = _state_value(state, "average_cost")
    if highest is None:
        return None
    pct = float(rule["threshold_pct"]) / 100.0
    rule_type = rule["rule_type"]
    if rule_type == "high_pct":
        return round(float(highest) * pct, 4)
    if rule_type == "high_minus_cost_pct":
        if cost is None:
            return None
        return round((float(highest) - float(cost)) * pct + float(cost), 4)
    return None


# ── 方向判断 ──────────────────────────────────────────────

def rule_matched(rule: dict, state: dict, current_price: float | None) -> bool:
    """判断本规则是否命中。current_price 缺失时不命中（避免误报）。"""
    if current_price is None:
        return False
    target = target_price(rule, state)
    if target is None:
        return False
    direction = rule["direction"]
    if direction == "below":
        return current_price <= target
    if direction == "above":
        return current_price >= target
    return False


# ── 通知事件 ──────────────────────────────────────────────

def _email_recipient() -> str:
    return os.getenv("EMAIL_TO", "")


def emit_alert_event(repo=None, *, rule: dict, state: dict,
                     current_price: float | None) -> dict | None:
    """规则命中 → 生成去重通知事件 + 投递记录；未命中或已去重返回 None。"""
    from StockInvestmentTool.biz.notification import NotificationService

    if not rule_matched(rule, state, current_price):
        return None

    from StockInvestmentTool.biz.repo import BusinessRepository
    repo = repo or BusinessRepository()

    target = target_price(rule, state)
    event_type = (POSITION_PRICE_ABOVE_EVENT_TYPE if rule["direction"] == "above"
                  else POSITION_PRICE_BELOW_EVENT_TYPE)
    direction_label = "冲高" if rule["direction"] == "above" else "回撤"
    rule_label = ("(后高-成本)×%" if rule["rule_type"] == "high_minus_cost_pct" else "后高×%")
    subject = f"[持仓{direction_label}提醒] {state.get('symbol')} 达目标价 {target}"

    text = (
        f"股票: {state.get('symbol')}\n"
        f"事件: 持仓{direction_label}目标价触发\n"
        f"规则: {rule_label}{rule['threshold_pct']:.0f}% ({rule['direction']})\n"
        f"目标价: {target}\n"
        f"现价: {current_price}\n"
        f"买入后最高: {state.get('highest_since_entry')}\n"
        f"成本均价: {state.get('average_cost')}\n"
        f"浮动盈亏: {state.get('unrealized_pnl')}\n"
        f"数据时间: {state.get('price_as_of')}（来源 {state.get('price_source')}）\n"
    )
    payload = {
        "subject": subject,
        "text": text,
        "symbol": state.get("symbol"),
        "rule_id": rule["rule_id"],
        "rule_type": rule["rule_type"],
        "direction": rule["direction"],
        "threshold_pct": rule["threshold_pct"],
        "target_price": target,
        "current_price": current_price,
        "highest_since_entry": state.get("highest_since_entry"),
        "average_cost": state.get("average_cost"),
        "price_as_of": state.get("price_as_of"),
        "price_source": state.get("price_source"),
    }
    # 去重粒度：持仓 + 规则 + 方向 + 数据日期（同日内只提醒一次）
    as_of_day = (state.get("price_as_of") or "")[:10]
    trigger_fingerprint = f"{rule['rule_id']}|{rule['direction']}|{as_of_day}"
    dedupe_key = (f"position_cycle|{state.get('symbol') or ''}||{event_type}|ALERT_PRICE"
                  f"|{as_of_day}|{trigger_fingerprint}")
    existing = repo.db.fetchone(
        "SELECT event_id FROM notification_events WHERE dedupe_key=?", (dedupe_key,))
    if existing:
        logger.info("持仓目标价通知已去重: %s (as_of=%s)", state.get("symbol"), as_of_day)
        return None
    event = NotificationService(repo).create_event(
        event_type=event_type,
        symbol=state.get("symbol") or "",
        subject_type="position_cycle",
        subject_id=rule["symbol"],
        priority=2,
        payload=payload,
        data_as_of=as_of_day or "",
        action="ALERT_PRICE",
        trigger_fingerprint=trigger_fingerprint,
    )
    recipient = _email_recipient()
    if recipient:
        NotificationService(repo).create_delivery(
            event, "email", recipient, template="position_price_alert"
        )
    logger.info("持仓目标价通知已生成: %s %s -> target=%s (as_of=%s)",
                state.get("symbol"), rule["direction"], target, as_of_day)
    return {"event_id": event.event_id, "target_price": target}


# ── 批量评估 ──────────────────────────────────────────────

def evaluate_position_alerts(repo, state: dict) -> int:
    """对单只持仓股票的全部启用规则执行一次评估；返回命中的规则数。

    state 为 position_runtime_states 的 dict（含 symbol / position_cycle_id /
    highest_since_entry / average_cost / current_price / price_as_of / price_source）。
    规则以 symbol（股票代码）关联，故取 state["symbol"] 匹配。
    """
    symbol = state.get("symbol")
    if not symbol:
        return 0
    rules = list_rules(repo, symbol=symbol)
    rules = [r for r in rules if r.get("enabled")]
    if not rules:
        return 0
    hit = 0
    current_price = state.get("current_price")
    for rule in rules:
        try:
            result = emit_alert_event(repo, rule=rule, state=state, current_price=current_price)
            if result:
                hit += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("持仓目标价规则命中处理失败 %s: %s", rule.get("rule_id"), exc)
    return hit
