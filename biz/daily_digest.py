# -*- coding: utf-8 -*-
"""每日盘后汇总与盘中触发器迁移到新 biz 通知体系。

替代旧 notifier/notify.py + notifier/core.py 的 digest 组装与触发器执行：
- 数据获取继续复用既有数据源（fundflow / screener / portfolio.dashboard）
- 消息组装为纯函数（从旧 notifier 迁移，逻辑保持一致）
- 投递统一走 biz/notification.py（NotificationEvent → Delivery → outbox）
- 通知只产生事件，不改变持仓
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

TOPIC_PRICE = "price"
TOPIC_FUNDFLOW = "fundflow"
TOPIC_SUMMARY = "summary"
TOPIC_ORDERS = "orders"


@dataclass
class DigestSection:
    topic: str
    title: str
    lines: list[str]
    priority: int = 1


@dataclass
class DigestRules:
    channel: str = "email"
    watchlist: list[dict] = field(default_factory=list)
    fundflow: dict = field(default_factory=lambda: {"enable": True, "sustained_top": 10})
    daily: dict = field(default_factory=lambda: {"enable": True})


def load_digest_rules(path: Optional[str] = None) -> DigestRules:
    """读取盘后汇总规则配置（biz/rules.yaml）。

    迁移自旧 notifier/rules.yaml；若旧文件仍存在则读旧路径。
    """
    from pathlib import Path

    candidate = Path(path) if path else None
    if candidate is None:
        legacy = Path(__file__).resolve().parent.parent / "notifier" / "rules.yaml"
        candidate = Path(__file__).resolve().parent / "rules.yaml"
        if not candidate.exists() and legacy.exists():
            candidate = legacy
    if not candidate.exists():
        return DigestRules()
    import yaml
    with open(candidate, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return DigestRules(
        channel=str(data.get("channel", "email")),
        watchlist=list(data.get("watchlist") or []),
        fundflow=dict(data.get("fundflow") or {"enable": True, "sustained_top": 10}),
        daily=dict(data.get("daily") or {"enable": True}),
    )


# ── 消息构造（纯函数，从旧 notifier/notify.py 迁移）───────────────────

def build_price_messages(rules: DigestRules, quotes) -> list[str]:
    if not rules.watchlist:
        return []
    by_code = {str(q.get("code", "")).lower(): q for q in quotes}
    msgs = []
    for item in rules.watchlist:
        code = str(item.get("code") or "").lower()
        q = by_code.get(code)
        if q is None:
            continue
        name = item.get("name") or q.get("name") or code
        price = q.get("price")
        chg = q.get("change_pct")
        bits = []
        pa, pb, pc = item.get("price_above"), item.get("price_below"), item.get("change_pct")
        if price is not None:
            if pa is not None and price >= pa:
                bits.append(f"突破 {pa}")
            if pb is not None and price <= pb:
                bits.append(f"跌破 {pb}")
        if chg is not None and pc is not None and abs(chg) >= pc:
            direction = "涨" if chg > 0 else "跌"
            bits.append(f"单日{direction}{abs(chg):.1f}%")
        if bits:
            msgs.append(
                f"{name}（{code}）现价 {price}，{'；'.join(bits)}"
                if price is not None else f"{name}（{code}），{'；'.join(bits)}"
            )
    return msgs


def build_fundflow_messages(rules: DigestRules, overview: dict, sustained, divergent, turn) -> list[str]:
    if not rules.fundflow.get("enable"):
        return []
    min_net = float(rules.fundflow.get("min_net_yi") or 0)
    top = int(rules.fundflow.get("sustained_top") or 10)
    lines = [f"资金流信号（{'大盘净额 ' + str(overview.get('全市场净额合计(亿)', '—')) + '亿' if overview else ''}）"]
    added = 0
    if sustained is not None and not sustained.empty:
        for _, r in sustained.head(top).iterrows():
            name = r.get("name") or r.get("板块")
            net_days = r.get("net_days") or r.get("多日净额(亿)")
            if net_days is not None and abs(net_days) >= min_net:
                lines.append(f"- {name}: 多日净额 {net_days:.1f}亿（持续流入）")
                added += 1
    if added == 0:
        lines.append("- 今日无达到阈值的持续流入证券")
    if divergent is not None and not divergent.empty and rules.fundflow.get("divergent_top"):
        lines.append("背离警示（价涨钱走）:")
        for _, r in divergent.head(int(rules.fundflow.get("divergent_top") or 5)).iterrows():
            name = r.get("name") or r.get("板块")
            lines.append(f"- {name}: 涨{r.get('chg')}% 但净额 {r.get('net')}亿")
    return ["\n".join(lines)]


def build_daily_messages(rules: DigestRules, overview: dict, industry_top, industry_bottom) -> list[str]:
    if not rules.daily.get("enable"):
        return []
    lines = ["今日A股资金流概览"]
    if overview:
        lines.append(
            f"> 上涨 {overview.get('上涨')} / 下跌 {overview.get('下跌')}，"
            f"主力净额合计 {overview.get('全市场净额合计(亿)')}亿，"
            f"背离 {overview.get('价涨钱走(背离)')} 家"
        )
    if industry_top is not None and not industry_top.empty:
        lines.append("行业净流入 TOP5: " + "、".join(
            f"{r.get('name')}({r.get('net'):.1f}亿)" for _, r in industry_top.head(5).iterrows()
            if r.get("net") is not None))
    if industry_bottom is not None and not industry_bottom.empty:
        lines.append("行业净流出 TOP5: " + "、".join(
            f"{r.get('name')}({r.get('net'):.1f}亿)" for _, r in industry_bottom.tail(5).iterrows()
            if r.get("net") is not None))
    return ["\n".join(lines)]


def build_orders_messages(data: dict) -> list[str]:
    summary = data.get("summary") or {}
    lines = [f"今日持仓指令（{data.get('data_date', '')}）"]
    lines.append(
        f"> 持仓 {summary.get('position_count', 0)} 只 | 总盈亏 "
        f"{summary.get('total_pnl_pct', '—')}% | {data.get('risk_status', '')}"
    )
    positions = data.get("positions") or []
    if not positions:
        lines.append("- 当前无持仓")
    for p in positions:
        label = p.get("advice_label", "—")
        reason = (p.get("advice") or {}).get("reason", "")
        msg = (
            f"- {p.get('stock_name')}({p.get('stock_code')}): {label} "
            f"现价{p.get('current_price')} 盈亏{p.get('unrealized_pnl_pct')}%"
        )
        if reason:
            msg += f"｜{reason}"
        lines.append(msg)
    return ["\n".join(lines)]


# ── 数据获取（复用既有数据源，不迁移数据能力）─────────────────────

def _gather_digest_data(rules: DigestRules) -> list[DigestSection]:
    """拉取价格/资金流/盘后/持仓指令数据并组装为 DigestSection 列表。"""
    from StockInvestmentTool.screener.sources import tencent_quotes
    from StockInvestmentTool.fundflow import analysis, sources

    sections: list[DigestSection] = []

    codes = [str(w.get("code") or "") for w in rules.watchlist if w.get("code")]
    if codes:
        try:
            quotes = tencent_quotes(codes).to_dict("records")
            lines = build_price_messages(rules, quotes)
            if lines:
                sections.append(DigestSection(TOPIC_PRICE, "自选价格提醒", lines))
        except Exception as exc:  # noqa: BLE001
            logger.warning("自选价格数据获取失败: %s", exc)

    try:
        stk_now = sources.fetch_stock("now")
        overview = analysis.market_overview(stk_now)
        stk_3d = sources.fetch_stock("3d")
        stock_res = analysis.stock_analysis(stk_now, stk_3d, top=15)
        ind_now = sources.fetch_sector("industry", "now")
        ind_3d = sources.fetch_sector("industry", "3d")
        industries = analysis.merge_trend(ind_now, ind_3d, on="name")
        sustained = stock_res["持续流入榜"]
        divergent = stock_res["价涨钱走(背离)榜"]
        turn = industries[industries["trend"] == "转为流入"].sort_values("net", ascending=False)

        lines = build_fundflow_messages(rules, overview, sustained, divergent, turn)
        if lines:
            sections.append(DigestSection(TOPIC_FUNDFLOW, "资金流信号", lines))
        lines = build_daily_messages(rules, overview, ind_now, ind_3d)
        if lines:
            sections.append(DigestSection(TOPIC_SUMMARY, "盘后市场汇总", lines))
    except Exception as exc:  # noqa: BLE001
        logger.warning("资金流/盘后数据获取失败: %s", exc)

    try:
        from StockInvestmentTool.portfolio.manager import PortfolioManager
        from StockInvestmentTool.portfolio.dashboard import DashboardService
        data = DashboardService(PortfolioManager()).war_room()
        lines = build_orders_messages(data)
        if lines:
            sections.append(DigestSection(TOPIC_ORDERS, "今日持仓指令", lines))
    except Exception as exc:  # noqa: BLE001
        logger.warning("持仓指令数据获取失败: %s", exc)

    return sections


def _render_text(sections: list[DigestSection]) -> str:
    parts = []
    for sec in sections:
        body = "\n".join(sec.lines)
        parts.append(f"【{sec.title}】\n{body}")
    return "\n\n".join(parts)


def _render_html(sections: list[DigestSection]) -> str:
    cards = []
    for sec in sections:
        body = "<br>".join(sec.lines)
        cards.append(
            f'<div style="margin-bottom:14px;padding:12px 14px;background:#f8f9fb;'
            f'border-radius:8px;"><div style="font-weight:bold;margin-bottom:6px;">'
            f'{sec.title}</div><div style="font-size:13px;">{body}</div></div>'
        )
    return (
        '<div style="font-family:Arial,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;">'
        + "".join(cards) + "</div>"
    )


# ── 主入口 ────────────────────────────────────────────────

def build_daily_digest(repo=None) -> dict:
    """生成每日盘后汇总：拉数据 → 组装 → 创建通知事件 + 投递。

    返回 {sections, event_id, delivery_id}；无内容时 skipped。
    """
    from datetime import datetime
    from StockInvestmentTool.biz.notification import NotificationService

    rules = load_digest_rules()
    sections = _gather_digest_data(rules)
    if not sections:
        return {"sections": 0, "skipped": True}

    subject = f"股票盘后汇总 {datetime.now():%Y-%m-%d}"
    text = _render_text(sections)
    html = _render_html(sections)
    data_date = datetime.now().strftime("%Y-%m-%d")

    notification = NotificationService(repo)
    event = notification.create_event(
        event_type="DAILY_REPORT",
        subject_type="report",
        subject_id=data_date,
        priority=1,
        payload={"subject": subject, "text": text, "html": html, "sections": len(sections)},
        data_as_of=data_date,
        action="REPORT",
        trigger_fingerprint=f"daily|{data_date}",
    )
    delivery_id = ""
    delivery = notification.create_rule_delivery(event, template="daily_digest")
    if delivery:
        delivery_id = delivery.delivery_id
    return {"sections": len(sections), "event_id": event.event_id, "delivery_id": delivery_id}


def _email_recipients() -> str:
    import os
    return os.getenv("EMAIL_TO", "")


def send_pending_deliveries(repo=None, *, limit: int = 50) -> dict:
    """投递 outbox 中 pending 投递（供 notification.outbox_delivery 任务调用）。

    复用 biz/notification.py 的 claim/deliver；仅支持 email 渠道。
    """
    import os
    from StockInvestmentTool.biz.notification import NotificationService, EmailChannel

    service = NotificationService(repo)
    pending = service.list_pending_deliveries()[:limit]
    delivered = 0
    failed = 0
    for item in pending:
        delivery_id = item["delivery_id"]
        if not service.claim(delivery_id, "outbox-worker", lease_seconds=120):
            continue
        event = service.repo.db.fetchone(
            "SELECT * FROM notification_events WHERE event_id=?", (item["event_id"],)
        )
        if event is None:
            service.repo.db.update(
                "notification_deliveries",
                {"status": "failed", "last_error": "event_not_found", "claimed_by": "", "lease_expires_at": ""},
                "delivery_id=?", (delivery_id,),
            )
            continue
        from StockInvestmentTool.biz.db import loads_json
        payload = loads_json(event["payload_json"])
        subject = payload.get("subject", f"通知 {event['event_type']}")
        body = payload.get("text", payload.get("html", ""))
        recipient = item.get("recipient") or _email_recipients()
        if not recipient:
            service.repo.db.update(
                "notification_deliveries",
                {"status": "suppressed", "last_error": "recipient_missing", "claimed_by": "", "lease_expires_at": ""},
                "delivery_id=?", (delivery_id,),
            )
            continue
        host = os.getenv("EMAIL_SMTP_HOST", "smtp.qq.com")
        port = int(os.getenv("EMAIL_SMTP_PORT", "465"))
        user = os.getenv("EMAIL_USER", "")
        password = os.getenv("EMAIL_PASSWORD", "")
        channel = EmailChannel(host, port, user, username=user, password=password,
                               use_tls=str(os.getenv("EMAIL_USE_TLS", "1")) == "1")
        if service.deliver(delivery_id, channel, subject=subject, body=body,
                           recipient=recipient, worker="outbox-worker"):
            delivered += 1
        else:
            failed += 1
    return {"delivered": delivered, "failed": failed}
