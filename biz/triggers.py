# -*- coding: utf-8 -*-
"""盘中触发器（可视化配置）迁移到新 biz 通知体系。

替代旧 notifier/triggers.py + web/scheduler 中的触发器评估：
- 规则存储：YAML（biz/notify_rules.yaml），schema 与旧 notifier 保持一致，前端契约不变
- 条件评估：action（操作建议）/ price_change（价格涨跌幅）/ indicator（指标阈值）
  —— 数据源继续复用既有模块（portfolio.dashboard / screener.sources / datasource）
- 投递：走 biz/notification.py（NotificationEvent → Delivery → outbox → email）
- 盘中配额：每日通知上限，避免轮询频率变成推送频率
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

RULES_PATH = Path(__file__).resolve().parent / "notify_rules.yaml"

# 主题（与 daily_digest 对齐）
TOPIC_PRICE = "price"
TOPIC_FUNDFLOW = "fundflow"
TOPIC_SUMMARY = "summary"
TOPIC_ORDERS = "orders"

# 事件类型
TRIGGER_EVENT_TYPE = "TRIGGER"

# 由业务模块直接产生的通知事件也登记在触发器中心。它们不是轮询型
# 触发器，规则中心只负责统一展示和管理，实际事件仍由对应业务入口产生。
NOTIFICATION_SUBSCRIPTION_KIND = "notification_subscription"


def _notification_trigger_defaults() -> list[dict]:
    """返回所有通知事件的内置登记规则。"""
    return [
        {
            "id": "notification_test",
            "name": "测试通知",
            "enabled": True,
            "kind": NOTIFICATION_SUBSCRIPTION_KIND,
            "event_type": "TEST_NOTIFICATION",
            "conditions": [], "logic": "AND",
            "schedule": {}, "channel": "email", "priority": "instant",
        },
        {
            "id": "notification_trigger",
            "name": "自定义触发器消息",
            "enabled": False,
            "kind": NOTIFICATION_SUBSCRIPTION_KIND,
            "event_type": "TRIGGER",
            "conditions": [], "logic": "AND",
            "schedule": {}, "channel": "email", "priority": "instant",
        },
        {
            "id": "notification_daily_report",
            "name": "每日盘后汇总",
            "enabled": False,
            "kind": NOTIFICATION_SUBSCRIPTION_KIND,
            "event_type": "DAILY_REPORT",
            "conditions": [], "logic": "AND",
            "schedule": {"mode": "daily", "time": "15:35"},
            "channel": "email", "priority": "batch",
        },
        {
            "id": "notification_task_failed",
            "name": "任务失败/超时",
            "enabled": True,
            "kind": NOTIFICATION_SUBSCRIPTION_KIND,
            "event_type": "TASK_FAILED",
            "conditions": [], "logic": "AND",
            "schedule": {}, "channel": "email", "priority": "instant",
        },
        {
            "id": "notification_system_alert",
            "name": "系统健康告警",
            "enabled": True,
            "kind": NOTIFICATION_SUBSCRIPTION_KIND,
            "event_type": "SYSTEM_ALERT",
            "conditions": [], "logic": "AND",
            "schedule": {}, "channel": "email", "priority": "instant",
        },
        {
            "id": "notification_data_quality",
            "name": "数据质量异常",
            "enabled": False,
            "kind": NOTIFICATION_SUBSCRIPTION_KIND,
            "event_type": "DATA_QUALITY_ALERT",
            "conditions": [], "logic": "AND",
            "schedule": {}, "channel": "email", "priority": "instant",
        },
        {
            "id": "notification_position_signal",
            "name": "持仓策略信号",
            "enabled": False,
            "kind": NOTIFICATION_SUBSCRIPTION_KIND,
            "event_type": "POSITION_SIGNAL",
            "conditions": [], "logic": "AND",
            "schedule": {}, "channel": "email", "priority": "instant",
        },
        {
            "id": "notification_position_drawdown",
            "name": "持仓高点回撤",
            "enabled": False,
            "kind": NOTIFICATION_SUBSCRIPTION_KIND,
            "event_type": "POSITION_DRAWDOWN",
            "conditions": [], "logic": "AND",
            "schedule": {}, "channel": "email", "priority": "instant",
        },
        {
            "id": "notification_position_price",
            "name": "持仓目标价提醒",
            "enabled": False,
            "kind": NOTIFICATION_SUBSCRIPTION_KIND,
            "event_types": ["POSITION_PRICE_ABOVE", "POSITION_PRICE_BELOW"],
            "conditions": [], "logic": "AND",
            "schedule": {}, "channel": "email", "priority": "instant",
        },
        {
            "id": "notification_trade_signals",
            "name": "买卖风险信号",
            "enabled": False,
            "kind": NOTIFICATION_SUBSCRIPTION_KIND,
            "event_types": ["BUY_SIGNAL", "SELL_SIGNAL", "RISK_ALERT"],
            "conditions": [], "logic": "AND",
            "schedule": {}, "channel": "email", "priority": "instant",
        },
    ]

# 盘中每日通知上限
INTRADAY_DAILY_LIMIT = 3

SCHEDULE_MODES = [
    {"mode": "intraday", "label": "盘中每 N 分钟", "interval_minutes": 10},
    {"mode": "post_close", "label": "盘后定时 HH:MM", "time": "15:35"},
    {"mode": "daily", "label": "每日一次 HH:MM", "time": "15:35"},
]


# ── 规则模型 ────────────────────────────────────────────────

@dataclass
class TriggerRule:
    """一条触发器规则（结构对齐旧 notifier/triggers.py）。"""

    id: str = ""
    name: str = ""
    enabled: bool = True
    conditions: list = field(default_factory=list)
    logic: str = "AND"
    schedule: dict = field(default_factory=dict)
    channel: str = "email"
    priority: str = "batch"
    use_email_to: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "enabled": self.enabled,
            "conditions": [c if isinstance(c, dict) else c for c in self.conditions],
            "logic": self.logic, "schedule": self.schedule,
            "channel": self.channel, "priority": self.priority,
            "use_email_to": self.use_email_to,
        }


def _default_config() -> dict:
    """默认触发器配置（保护内置默认行为）。"""
    return {
        "rules": [
            {
                "id": "actionable_intraday",
                "name": "盘中操作建议（即时）",
                "enabled": True,
                "conditions": [{"type": "action",
                                "params": {"advice_types": ["partial_sell", "sell_all", "buy_more", "adjust_stop"]}}],
                "logic": "AND",
                "schedule": {"mode": "intraday", "interval_minutes": 10},
                "channel": "email",
                "priority": "instant",
            },
            {
                "id": "post_close_summary",
                "name": "盘后持仓汇总（批次）",
                "enabled": True,
                "conditions": [{"type": "action",
                                "params": {"advice_types": ["hold", "partial_sell", "sell_all", "buy_more"]}}],
                "logic": "OR",
                "schedule": {"mode": "post_close", "time": "15:35"},
                "channel": "email",
                "priority": "batch",
            },
            {
                "id": "price_threshold",
                "name": "自选价格阈值（批次）",
                "enabled": True,
                "conditions": [{"type": "action", "params": {"advice_types": ["hold"]}}],
                "logic": "AND",
                "schedule": {"mode": "post_close", "time": "15:35"},
                "channel": "email",
                "priority": "batch",
            },
            *_notification_trigger_defaults(),
        ]
    }


# ── 存储 / 读取 ─────────────────────────────────────────────

def load_triggers(path: Optional[Path | str] = None) -> list[dict]:
    """读取触发器规则列表，并补齐缺失的通知登记规则。"""
    path = Path(path) if path else RULES_PATH
    if not path.exists():
        rules = _default_config()["rules"]
        save_triggers(rules, path)
        return rules
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        rules = data.get("rules") or []
        return sync_notification_triggers(path, rules=rules)
    except Exception as e:  # noqa: BLE001
        logger.warning("触发器配置读取失败(%s)，退回默认: %s", path, e)
        return _default_config()["rules"]


def sync_notification_triggers(path: Optional[Path | str] = None,
                                *, rules: Optional[list[dict]] = None) -> list[dict]:
    """将缺失的业务通知登记规则幂等同步到触发器配置。

    仅按稳定 id 判断是否已存在，不覆盖用户对已有规则的修改。
    """
    target = Path(path) if path else RULES_PATH
    if rules is None:
        if not target.exists():
            current = list(_default_config()["rules"])
        else:
            try:
                with open(target, encoding="utf-8") as f:
                    current = list((yaml.safe_load(f) or {}).get("rules") or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("触发器配置读取失败(%s)，无法同步通知登记规则: %s", target, exc)
                current = []
    else:
        current = list(rules)
    existing = {str(rule.get("id")) for rule in current if isinstance(rule, dict)}
    missing = [rule for rule in _notification_trigger_defaults()
               if rule["id"] not in existing]
    if missing:
        current.extend(missing)
        save_triggers(current, target)
        logger.info("通知登记规则已同步: 新增 %d 条", len(missing))
    return current


def save_triggers(rules: list[dict], path: Optional[Path | str] = None) -> Path:
    """原子保存触发器配置。"""
    path = Path(path) if path else RULES_PATH
    content = yaml.safe_dump({"rules": rules}, allow_unicode=True, sort_keys=False)
    _atomic_write(path, content)
    logger.info("触发器已保存: %s (%d 条)", path, len(rules))
    return path


def enabled_triggers(path: Optional[Path | str] = None) -> list[dict]:
    """返回启用的触发器。"""
    return [r for r in load_triggers(path) if r.get("enabled", True)
            and r.get("kind") != NOTIFICATION_SUBSCRIPTION_KIND]


def _atomic_write(path: Path, content: str) -> None:
    """原子写文件（先写临时文件再替换）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


# ── 邮件收件人配置 ──────────────────────────────────────────

def email_recipients() -> list[str]:
    """读取邮件收件人列表（.env EMAIL_TO，逗号分隔）。"""
    to = os.getenv("EMAIL_TO", "")
    return [x.strip() for x in to.split(",") if x.strip()]


def set_email_recipients(to_list: list[str]) -> None:
    """写邮件收件人（.env EMAIL_TO）。"""
    from dotenv import set_key

    env = _env_file()
    env.parent.mkdir(parents=True, exist_ok=True)
    set_key(str(env), "EMAIL_TO", ",".join(to_list))


def mail_config_status() -> dict:
    """只返回是否已配置，不回显明文（沿用安全约定）。"""
    from dotenv import dotenv_values

    env = _env_file()
    values = dotenv_values(env) if env.exists() else {}
    return {
        "email_configured": bool(values.get("EMAIL_USER")),
        "email_to": bool(values.get("EMAIL_TO")),
        "email_to_addresses": [x for x in (values.get("EMAIL_TO") or "").split(",") if x.strip()],
        "env_file": str(env),
    }


def _env_file() -> Path:
    from StockInvestmentTool.portfolio.settings import _env_file as _f
    return _f()


# ── 条件评估 ────────────────────────────────────────────────

def _evaluate_condition(condition: dict, data: dict) -> bool:
    """评估一条触发条件（action / price_change / indicator）。"""
    ctype = condition.get("type")
    params = condition.get("params") or {}
    if ctype == "action":
        wanted = set(params.get("advice_types") or [])
        if not wanted:
            return bool(data.get("positions"))
        return any(
            (p.get("advice") or {}).get("advice_type") in wanted
            for p in data.get("positions") or []
        )
    if ctype == "price_change":
        return bool(_watch_price_lines(params))
    if ctype == "indicator":
        return _indicator_condition_matches(params, data)
    return False


def _indicator_condition_matches(params: dict, data: dict) -> bool:
    """评估指标阈值/穿越（对任一持仓）。"""
    from StockInvestmentTool.datasource.fetcher import StockDataFetcher
    from StockInvestmentTool.datasource.base import FallbackDataSource
    from StockInvestmentTool.indicators.context import IndicatorContext

    name = str(params.get("name") or "").strip()
    operator = str(params.get("operator") or "above").strip().lower()
    if not name:
        return False
    try:
        threshold = float(params.get("value"))
    except (TypeError, ValueError):
        return False
    source = FallbackDataSource()
    for position in data.get("positions") or []:
        code = position.get("stock_code") or position.get("code")
        if not code:
            continue
        frame = source.fetch_kline(code)
        if frame is None or len(frame) < 2:
            continue
        try:
            current = IndicatorContext(frame, row_index=len(frame) - 1).eval(name)
            previous = IndicatorContext(frame, row_index=len(frame) - 2).eval(name)
        except (ValueError, KeyError):
            continue
        if operator == "above" and current > threshold:
            return True
        if operator == "below" and current < threshold:
            return True
        if operator == "cross_above" and previous <= threshold < current:
            return True
        if operator == "cross_below" and previous >= threshold > current:
            return True
    return False


def _orders_lines(data: dict, advice_types: Optional[set[str]] = None) -> list[str]:
    """持仓指令 → 文本行（含操作建议/盈亏）。"""
    positions = data.get("positions") or []
    if not positions:
        return []
    detail_lines = []
    for p in positions:
        adv = p.get("advice") or {}
        if advice_types and adv.get("advice_type") not in advice_types:
            continue
        label = p.get("advice_label") or "—"
        reason = (adv.get("reason") or "")[:60]
        line = (f"{p.get('stock_name')}({p.get('stock_code')}): {label}"
                f" 现价{p.get('current_price')} 盈亏{p.get('unrealized_pnl_pct')}%")
        if reason:
            line += f"｜{reason}"
        detail_lines.append(line)
    if not detail_lines:
        return []
    return [f"持仓 {len(detail_lines)} 只 | 总盈亏 {data.get('summary', {}).get('total_pnl_pct', '—')}%", *detail_lines]


def _watch_price_lines(params: Optional[dict] = None) -> list[str]:
    """自选价格涨跌幅触发 → 文本行。"""
    try:
        from StockInvestmentTool.screener.sources import tencent_quotes

        params = params or {}
        from StockInvestmentTool.biz.daily_digest import load_digest_rules
        rules = load_digest_rules()
        codes = [w["code"] for w in rules.watchlist if w.get("code")]
        if not codes:
            return []
        quotes = tencent_quotes(codes).to_dict("records")
        if not params:
            from StockInvestmentTool.biz.daily_digest import build_price_messages
            return build_price_messages(rules, quotes)
        direction = str(params.get("direction", "up")).lower()
        try:
            threshold = float(params.get("pct", 0))
        except (TypeError, ValueError):
            threshold = 0
        lines = []
        for quote in quotes:
            change = float(quote.get("change_pct") or 0)
            matched = change >= threshold if direction == "up" else change <= -threshold
            if matched:
                lines.append(f"{quote.get('name') or quote.get('code')}: 涨跌幅 {change:+.2f}%（阈值 {direction} {threshold:.2f}%）")
        return lines
    except Exception as e:  # noqa: BLE001
        logger.warning("价格阈值检查失败: %s", e)
        return []


# ── 盘中每日配额 ────────────────────────────────────────────

def _state_file() -> Path:
    from StockInvestmentTool.config import Config
    return Config.DATA_DIR / "biz_trigger_state.json"


def _load_state() -> dict:
    import json

    path = _state_file()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


def _save_state(state: dict) -> None:
    import json

    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


def _intraday_quota(rule: dict) -> dict:
    """每日盘中通知配额检查。"""
    now = datetime.now()
    limit = max(1, int(os.getenv("INTRADAY_NOTIFY_DAILY_LIMIT", str(INTRADAY_DAILY_LIMIT))))
    key = f"trigger:{rule.get('id') or rule.get('name') or 'intraday'}:{now:%Y-%m-%d}"
    state = _load_state()
    count = int(state.get(key, 0) or 0)
    return {"allowed": count < limit, "state": state, "key": key,
            "count": count, "limit": limit}


def _commit_intraday_quota(quota: dict) -> None:
    state = dict(quota["state"])
    state[quota["key"]] = int(quota["count"]) + 1
    _save_state(state)


# ── 主入口 ──────────────────────────────────────────────────

def run_trigger_rule(rule: dict, repo=None) -> dict:
    """执行一条触发器规则：按条件过滤持仓/信号，聚合为通知事件 + 投递。"""
    from StockInvestmentTool.biz.notification import NotificationService
    from StockInvestmentTool.biz.daily_digest import _email_recipients, send_pending_deliveries
    from StockInvestmentTool.portfolio.manager import PortfolioManager
    from StockInvestmentTool.portfolio.dashboard import DashboardService

    logger.info("执行触发器: %s", rule.get("name"))
    try:
        mgr = PortfolioManager()
        mgr.refresh_all()
        data = DashboardService(mgr).war_room()
    except Exception as e:  # noqa: BLE001
        logger.error("触发器数据获取失败: %s", e)
        return {"ok": False, "error": str(e)}

    conditions = [c for c in rule.get("conditions", []) if isinstance(c, dict)]
    condition_results = [_evaluate_condition(c, data) for c in conditions]
    logic = str(rule.get("logic", "AND")).upper()
    triggered = (all(condition_results) if logic == "AND" else any(condition_results)) if condition_results else True
    if not triggered:
        logger.info("触发器 %s 条件未满足: %s", rule.get("name"), condition_results)
        return {"ok": True, "skipped": True, "conditions": condition_results}

    sections: list[dict] = []
    action_types: set[str] = set()
    for condition in conditions:
        if condition.get("type") == "action":
            action_types.update((condition.get("params") or {}).get("advice_types") or [])
    if any(c.get("type") == "action" for c in conditions) or not conditions:
        orders_lines = _orders_lines(data, action_types or None)
        if orders_lines:
            sections.append({"topic": TOPIC_ORDERS, "title": "⚔️ 今日持仓指令", "lines": orders_lines})

    if any(c.get("type") == "price_change" for c in conditions):
        price_params = next((c.get("params") or {} for c in conditions if c.get("type") == "price_change"), {})
        price_lines = _watch_price_lines(price_params)
        if price_lines:
            sections.append({"topic": TOPIC_PRICE, "title": "💰 自选价格提醒", "lines": price_lines})

    if any(c.get("type") == "indicator" for c in conditions):
        indicator_lines = _indicator_lines(conditions, data)
        if indicator_lines:
            sections.append({"topic": TOPIC_SUMMARY, "title": "📊 指标提醒", "lines": indicator_lines})

    if not sections:
        logger.info("触发器 %s 无触发内容，跳过", rule.get("name"))
        return {"ok": True, "skipped": True}

    # 盘中轮询配额：普通告警不因轮询频率而提高推送频率
    cap_state = None
    if (rule.get("schedule") or {}).get("mode") == "intraday":
        cap_state = _intraday_quota(rule)
        if not cap_state["allowed"]:
            logger.info("触发器 %s 已达每日通知上限 %d 次", rule.get("name"), cap_state["limit"])
            return {"ok": True, "skipped": True, "reason": "daily_limit",
                    "daily_count": cap_state["count"], "daily_limit": cap_state["limit"]}

    subject = f"股票通知 {datetime.now():%Y-%m-%d}"
    text = _render_sections_text(sections)
    event = NotificationService(repo).create_event(
        event_type=TRIGGER_EVENT_TYPE,
        subject_type="trigger",
        subject_id=str(rule.get("id") or ""),
        priority=0 if rule.get("priority") == "instant" else 1,
        payload={"subject": subject, "text": text, "sections": sections, "rule": rule.get("name")},
        data_as_of=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        action=rule.get("name", "trigger"),
        trigger_fingerprint=f"{rule.get('id')}|{datetime.now():%Y-%m-%d %H}",
    )

    delivery_id = ""
    delivery = NotificationService(repo).create_rule_delivery(event, template="trigger")
    if delivery:
        delivery_id = delivery.delivery_id
        # 即时触发器立即投递；批次随 outbox_delivery 定时发送
        if rule.get("priority") == "instant":
            send_pending_deliveries(repo)
    if cap_state:
        _commit_intraday_quota(cap_state)
    return {"ok": True, "event_id": event.event_id, "delivery_id": delivery_id}


def _indicator_lines(conditions: list[dict], data: dict) -> list[str]:
    """收集指标条件命中的描述行。"""
    lines = []
    for condition in conditions:
        if condition.get("type") != "indicator":
            continue
        params = condition.get("params") or {}
        name = str(params.get("name") or "").strip()
        operator = str(params.get("operator") or "above").strip().lower()
        value = params.get("value")
        if not name:
            continue
        try:
            float(value)
        except (TypeError, ValueError):
            continue
        if _indicator_condition_matches(params, data):
            lines.append(f"{name} {operator} {value}")
    return lines


def _render_sections_text(sections: list[dict]) -> str:
    parts = []
    for sec in sections:
        body = "\n".join(sec["lines"])
        parts.append(f"【{sec['title']}】\n{body}")
    return "\n\n".join(parts)
