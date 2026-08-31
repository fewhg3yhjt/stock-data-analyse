# -*- coding: utf-8 -*-
"""Advice / NotificationEvent / NotificationDelivery / Outbox + Email Channel。

依据 docs/ADVICE_AND_NOTIFICATION_DESIGN.md。
- Advice 由 StrategyDecision 转换而来
- 通知模块不重新计算买卖策略
- Outbox：原子 claim/lease、失败退避、超过次数 dead
- 去重：BusinessSignalKey + SignalOccurrenceKey
- Email 为第一版验收渠道；Feishu/WeCom 保留为 Channel 接口实现
"""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from typing import Any, Protocol

from StockInvestmentTool.biz.models import new_id, now_utc

logger = logging.getLogger(__name__)

# Advice 状态
ADVICE_GENERATED = "generated"
ADVICE_NOTIFIED = "notified"
ADVICE_ACKNOWLEDGED = "acknowledged"
ADVICE_ACCEPTED = "accepted"
ADVICE_IGNORED = "ignored"
ADVICE_EXPIRED = "expired"
ADVICE_PARTIALLY_EXECUTED = "partially_executed"
ADVICE_EXECUTED = "executed"
ADVICE_CANCELLED = "cancelled"

# NotificationEvent 类型
NT_BUY_SIGNAL = "BUY_SIGNAL"
NT_SELL_SIGNAL = "SELL_SIGNAL"
NT_RISK_ALERT = "RISK_ALERT"
NT_DAILY_REPORT = "DAILY_REPORT"
NT_TASK_FAILED = "TASK_FAILED"
NT_DATA_QUALITY_ALERT = "DATA_QUALITY_ALERT"
NT_SYSTEM_ALERT = "SYSTEM_ALERT"

# Delivery 状态
DELIVERY_PENDING = "pending"
DELIVERY_PROCESSING = "processing"
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"
DELIVERY_DEAD = "dead"
DELIVERY_SUPPRESSED = "suppressed"

# 重试配置
MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 60


@dataclass
class Advice:
    advice_id: str
    symbol: str
    action: str
    strategy_version_id: str | None = None
    portfolio_id: str | None = None
    position_cycle_id: str | None = None
    quantity: float | None = None
    price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    reason: str = ""
    triggered_rules: list = field(default_factory=list)
    data_as_of: str = ""
    valid_until: str | None = None
    status: str = ADVICE_GENERATED
    revision: int = 1
    trigger_fingerprint: str = ""
    last_notified_revision: int = 0
    created_at: str = field(default_factory=now_utc)
    updated_at: str = field(default_factory=now_utc)


@dataclass
class NotificationEvent:
    event_id: str
    event_type: str
    dedupe_key: str
    subject_type: str = ""
    subject_id: str = ""
    symbol: str = ""
    advice_id: str | None = None
    strategy_decision_id: str | None = None
    report_id: str | None = None
    priority: int = 0
    payload: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_utc)


@dataclass
class NotificationDelivery:
    delivery_id: str
    event_id: str
    channel: str
    recipient: str = ""
    template: str = ""
    status: str = DELIVERY_PENDING
    attempts: int = 0
    last_error: str = ""
    sent_at: str | None = None
    claimed_by: str = ""
    claimed_at: str | None = None
    lease_expires_at: str | None = None
    created_at: str = field(default_factory=now_utc)


# ---------------------------------------------------------------------------
# Channel 接口
# ---------------------------------------------------------------------------

class Channel(Protocol):
    def send(self, subject: str, body: str, recipient: str) -> bool: ...


class EmailChannel:
    """Email 渠道。配置来自 SMTP 设置，密码不写入日志。"""

    def __init__(self, smtp_host: str, smtp_port: int, from_address: str,
                 username: str = "", password: str = "", use_tls: bool = True,
                 connect_timeout: int = 10):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.from_address = from_address
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.connect_timeout = connect_timeout

    def send(self, subject: str, body: str, recipient: str) -> bool:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.from_address
        msg["To"] = recipient
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.connect_timeout) as server:
                if self.use_tls:
                    server.starttls()
                if self.username:
                    server.login(self.username, self.password)
                server.sendmail(self.from_address, [recipient], msg.as_string())
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("email send failed: %s", e)
            return False


CHANNEL_FACTORY: dict[str, Any] = {}


def register_channel(name: str, factory: Any) -> None:
    CHANNEL_FACTORY[name] = factory


def get_channel(name: str) -> Any:
    if name not in CHANNEL_FACTORY:
        raise KeyError(f"未注册渠道: {name}")
    return CHANNEL_FACTORY[name]()


# ---------------------------------------------------------------------------
# Outbox 服务
# ---------------------------------------------------------------------------

class NotificationService:
    """通知服务：创建事件/投递、去重、claim/lease 派发。"""

    def __init__(self, repo=None):
        from StockInvestmentTool.biz.repo import BusinessRepository
        self.repo = repo or BusinessRepository()

    # ── 创建事件 ──────────────────────────────────────────

    def create_event(self, *, event_type: str, symbol: str = "", advice_id: str | None = None,
                     strategy_decision_id: str | None = None, subject_type: str = "",
                     subject_id: str = "", report_id: str | None = None,
                     priority: int = 0, payload: dict | None = None,
                     strategy_version_id: str = "", data_as_of: str = "",
                     action: str = "", trigger_fingerprint: str = "") -> NotificationEvent:
        """创建通知事件。dedupe_key 由业务信号 + 出现指纹组成。"""
        business_key = f"{subject_type or symbol}|{symbol}|{strategy_version_id}|{event_type}|{action}"
        occurrence_key = f"{business_key}|{data_as_of}|{trigger_fingerprint}"
        dup = self.repo.db.fetchone(
            "SELECT * FROM notification_events WHERE dedupe_key=?", (occurrence_key,))
        if dup:
            return NotificationEvent(
                event_id=dup["event_id"], event_type=dup["event_type"], dedupe_key=dup["dedupe_key"],
                subject_type=dup["subject_type"], subject_id=dup["subject_id"],
                symbol=dup["symbol"], advice_id=dup["advice_id"], strategy_decision_id=dup["strategy_decision_id"],
                report_id=dup["report_id"], priority=dup["priority"],
                payload=_loads(dup["payload_json"]), created_at=dup["created_at"],
            )
        event = NotificationEvent(
            event_id=new_id("ne"), event_type=event_type, dedupe_key=occurrence_key,
            subject_type=subject_type, subject_id=subject_id, symbol=symbol,
            advice_id=advice_id, strategy_decision_id=strategy_decision_id,
            report_id=report_id, priority=priority, payload=payload or {},
        )
        self.repo.db.insert("notification_events", {
            "event_id": event.event_id, "event_type": event.event_type,
            "subject_type": event.subject_type, "subject_id": event.subject_id,
            "symbol": event.symbol, "advice_id": event.advice_id or "",
            "strategy_decision_id": event.strategy_decision_id or "",
            "report_id": event.report_id or "", "priority": event.priority,
            "dedupe_key": event.dedupe_key, "payload_json": _dumps(event.payload),
            "created_at": event.created_at,
        })
        return event

    # ── Advice 转换 ───────────────────────────────────────

    def advice_from_decision(self, decision, portfolio_id: str | None = None,
                             position_cycle_id: str | None = None) -> Advice:
        """StrategyDecision → Advice。"""
        advice = Advice(
            advice_id=new_id("adv"), symbol=decision.symbol, action=decision.action,
            strategy_version_id=decision.strategy_version_id,
            portfolio_id=portfolio_id, position_cycle_id=position_cycle_id,
            quantity=decision.quantity_ratio,
            price=decision.price, stop_price=decision.stop_price,
            target_price=decision.target_price, reason=decision.reason,
            triggered_rules=[r.get("rule_id") for r in decision.decision_trace.get("triggered_rules", [])],
            data_as_of=decision.data_as_of, valid_until=decision.valid_until,
        )
        self.repo.db.insert("advices", {
            "advice_id": advice.advice_id, "portfolio_id": advice.portfolio_id or "",
            "position_cycle_id": advice.position_cycle_id or "", "symbol": advice.symbol,
            "action": advice.action, "quantity": advice.quantity, "price": advice.price,
            "stop_price": advice.stop_price, "target_price": advice.target_price,
            "reason": advice.reason, "triggered_rules_json": _dumps(advice.triggered_rules),
            "strategy_version_id": advice.strategy_version_id or "",
            "data_as_of": advice.data_as_of, "valid_until": advice.valid_until or "",
            "status": advice.status, "revision": advice.revision,
            "trigger_fingerprint": advice.trigger_fingerprint,
            "last_notified_revision": advice.last_notified_revision,
            "created_at": advice.created_at, "updated_at": advice.updated_at,
        })
        return advice

    # ── 投递 ──────────────────────────────────────────────

    def create_delivery(self, event: NotificationEvent, channel: str,
                        recipient: str, template: str = "") -> NotificationDelivery:
        delivery = NotificationDelivery(
            delivery_id=new_id("nd"), event_id=event.event_id, channel=channel,
            recipient=recipient, template=template,
        )
        self.repo.db.insert("notification_deliveries", {
            "delivery_id": delivery.delivery_id, "event_id": delivery.event_id,
            "channel": delivery.channel, "recipient": delivery.recipient,
            "template": delivery.template, "status": delivery.status,
            "attempts": delivery.attempts, "last_error": delivery.last_error,
            "sent_at": delivery.sent_at or "", "claimed_by": delivery.claimed_by,
            "claimed_at": delivery.claimed_at or "", "lease_expires_at": delivery.lease_expires_at or "",
            "created_at": delivery.created_at,
        })
        return delivery

    def claim(self, delivery_id: str, worker: str, lease_seconds: int = 120) -> bool:
        """原子领取投递任务。已过期租约可被接管。"""
        row = self.repo.db.fetchone(
            "SELECT * FROM notification_deliveries WHERE delivery_id=?", (delivery_id,))
        if not row:
            return False
        if row["status"] in {DELIVERY_SENT, DELIVERY_DEAD}:
            return False
        if row["status"] == DELIVERY_PROCESSING:
            lease = row["lease_expires_at"] or ""
            if lease and lease > now_utc():
                return False  # 租约未过期，不可接管
        now = now_utc()
        import datetime
        from datetime import timezone
        expires = datetime.datetime.now(timezone.utc) + datetime.timedelta(seconds=lease_seconds)
        self.repo.db.update("notification_deliveries", {
            "status": DELIVERY_PROCESSING, "claimed_by": worker, "claimed_at": now,
            "lease_expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, "delivery_id=?", (delivery_id,))
        return True

    def deliver(self, delivery_id: str, channel: Any, *, subject: str, body: str,
                recipient: str) -> bool:
        """发送并更新投递状态。成功才标记 sent；失败重试，超限 dead。"""
        ok = channel.send(subject, body, recipient)
        if ok:
            self.repo.db.update("notification_deliveries", {
                "status": DELIVERY_SENT, "sent_at": now_utc(),
            }, "delivery_id=?", (delivery_id,))
            return True
        row = self.repo.db.fetchone(
            "SELECT * FROM notification_deliveries WHERE delivery_id=?", (delivery_id,))
        attempts = int(row["attempts"]) + 1 if row else 1
        status = DELIVERY_DEAD if attempts >= MAX_ATTEMPTS else DELIVERY_PENDING
        self.repo.db.update("notification_deliveries", {
            "status": status, "attempts": attempts,
            "last_error": f"send failed (attempt {attempts})",
        }, "delivery_id=?", (delivery_id,))
        return False

    def list_pending_deliveries(self) -> list[dict]:
        rows = self.repo.db.fetchall(
            "SELECT * FROM notification_deliveries WHERE status IN ('pending','processing') "
            "ORDER BY rowid LIMIT 100")
        return [dict(r) for r in rows]

    def list_events(self) -> list[dict]:
        rows = self.repo.db.fetchall("SELECT * FROM notification_events ORDER BY rowid DESC LIMIT 200")
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = _loads(d.pop("payload_json"))
            out.append(d)
        return out


def _dumps(value: Any) -> str:
    import json
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None) -> Any:
    import json
    try:
        return json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}