# -*- coding: utf-8 -*-
"""股票消息提醒 — 价格阈值 / 资金流信号 / 每日盘后汇总

推送渠道: 企业微信 或 飞书 群机器人 webhook（NOTIFY_CHANNEL 选择）。
webhook URL 走环境变量（.env），不落库: FEISHU_WEBHOOK_URL / WECOM_WEBHOOK_URL。

数据源复用:
    - 实时价: screener.sources.tencent_quotes（腾讯批量报价，不封IP）
    - 资金流: fundflow.sources（同花顺）
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from StockInvestmentTool.notifier.channels import make_channel

logger = logging.getLogger(__name__)

RULES_FILE = Path(__file__).resolve().parent / "rules.yaml"


@dataclass
class NotifyRules:
    channel: str = "feishu"                       # feishu | wecom
    watchlist: list[dict] = field(default_factory=list)
    fundflow: dict = field(default_factory=lambda: {"enable": False, "sustained_top": 10})
    daily: dict = field(default_factory=lambda: {"enable": True, "include_screener": False})

    @classmethod
    def from_yaml(cls, path: Optional[Path | str] = None) -> "NotifyRules":
        path = Path(path) if path else RULES_FILE
        rules = cls()
        if not path.exists():
            return rules
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        rules.channel = data.get("channel", rules.channel)
        rules.watchlist = data.get("watchlist", [])
        rules.fundflow = data.get("fundflow", rules.fundflow)
        rules.daily = data.get("daily", rules.daily)
        return rules

    def webhook_url(self) -> str:
        """返回渠道的 webhook URL（email 渠道返回 ''，发送器从环境变量读配置）。"""
        if self.channel in ("email", "mail", "smtp"):
            return ""  # email 渠道无 webhook，配置走环境变量
        env_key = "FEISHU_WEBHOOK_URL" if self.channel in ("feishu", "lark") else "WECOM_WEBHOOK_URL"
        url = os.getenv(env_key, "")
        if not url:
            raise RuntimeError(
                f"未配置 {env_key}，请在 StockInvestmentTool/.env 中填入群机器人 webhook 地址"
            )
        return url


# ── 消息构造 ─────────────────────────────────────────────

def build_price_messages(rules: NotifyRules, quotes) -> list[str]:
    """比对 watchlist 阈值，返回触发的提醒消息（可能为空列表）。"""
    if not rules.watchlist:
        return []
    by_code = {q.get("code"): q for q in quotes}
    msgs = []
    for item in rules.watchlist:
        code = (item.get("code") or "").lower()
        q = by_code.get(code)
        if q is None:
            logger.warning("watchlist 代码 %s 无行情", code)
            continue
        name = item.get("name") or q.get("name") or code
        price = q.get("price")
        chg = q.get("change_pct")
        bits = []
        pa = item.get("price_above")
        pb = item.get("price_below")
        pc = item.get("change_pct")
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
                f"**{name}**（{code}）现价 {price}，{'; '.join(bits)}"
                if price is not None else f"**{name}**（{code}），{'; '.join(bits)}"
            )
    return msgs


def build_fundflow_messages(rules: NotifyRules, overview: dict, sustained, divergent, turn) -> list[str]:
    """资金流信号 → 提醒文本。"""
    if not rules.fundflow.get("enable"):
        return []
    min_net = float(rules.fundflow.get("min_net_yi") or 0)
    top = int(rules.fundflow.get("sustained_top") or 10)
    lines = [f"**资金流信号**（{'大盘净额 ' + str(overview.get('全市场净额合计(亿)', '—')) + '亿' if overview else ''}）"]
    added = 0
    if sustained is not None and not sustained.empty:
        for _, r in sustained.head(top).iterrows():
            name = r.get("name") or r.get("板块")
            net_days = r.get("net_days") or r.get("多日净额(亿)")
            if net_days is not None and abs(net_days) >= min_net:
                lines.append(f"- {name}: 多日净额 {net_days:.1f}亿（持续流入）")
                added += 1
    if added == 0:
        lines.append("- 今日无达到阈值的持续流入标的")
    if divergent is not None and not divergent.empty and rules.fundflow.get("divergent_top"):
        lines.append("**⚠️ 背离警示**（价涨钱走）:")
        for _, r in divergent.head(int(rules.fundflow.get("divergent_top") or 5)).iterrows():
            name = r.get("name") or r.get("板块")
            lines.append(f"- {name}: 涨{r.get('chg')}% 但净额 {r.get('net')}亿")
    return ["\n".join(lines)]


def build_daily_messages(rules: NotifyRules, overview: dict, industry_top, industry_bottom) -> list[str]:
    """每日盘后资金流汇总。"""
    if not rules.daily.get("enable"):
        return []
    lines = ["**📊 今日A股资金流概览**"]
    if overview:
        lines.append(
            f"> 上涨 {overview.get('上涨')} / 下跌 {overview.get('下跌')}，"
            f"主力净额合计 {overview.get('全市场净额合计(亿)')}亿，"
            f"背离 {overview.get('价涨钱走(背离)')} 家"
        )
    if industry_top is not None and not industry_top.empty:
        lines.append("**行业净流入 TOP5**: " + "、".join(
            f"{r.get('name')}({r.get('net'):.1f}亿)" for _, r in industry_top.head(5).iterrows()
            if r.get("net") is not None))
    if industry_bottom is not None and not industry_bottom.empty:
        lines.append("**行业净流出 TOP5**: " + "、".join(
            f"{r.get('name')}({r.get('net'):.1f}亿)" for _, r in industry_bottom.tail(5).iterrows()
            if r.get("net") is not None))
    return ["\n".join(lines)]


def build_orders_messages(data: dict) -> list[str]:
    """作战仓「今日指令」→ 推送文本（账户总览 + 每只持仓指令）。"""
    summary = data.get("summary") or {}
    lines = [f"**⚔️ 今日持仓指令**（{data.get('data_date', '')}）"]
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
            f"- **{p.get('stock_name')}**({p.get('stock_code')}): {label} "
            f"现价{p.get('current_price')} 盈亏{p.get('unrealized_pnl_pct')}%"
        )
        if reason:
            msg += f"｜{reason}"
        lines.append(msg)
    return ["\n".join(lines)]


def build_actionable_messages(data: dict) -> list[str]:
    """只推「有操作建议」的持仓（右侧止盈/止损/加仓/调止损，排除 hold）。

    用于实时通知：盘中刷新持仓时，仅当某只触发操作建议才推送，
    避免噪音。
    """
    positions = data.get("positions") or []
    actionable = []
    for p in positions:
        adv = p.get("advice") or {}
        # is_actionable: buy_more/partial_sell/sell_all/adjust_stop（非 hold）
        if adv.get("advice_type") and adv.get("is_actionable"):
            actionable.append(p)
    if not actionable:
        return []

    lines = [f"**🔔 操作提醒**（{data.get('data_date', '')}）"]
    for p in actionable:
        adv = p.get("advice") or {}
        label = p.get("advice_label") or adv.get("advice_type", "")
        reason = adv.get("reason", "")
        urgency = adv.get("urgency", "")
        msg = (
            f"- **{p.get('stock_name')}**({p.get('stock_code')}): {label}"
            f"[{urgency}] 现价{p.get('current_price')}"
            f" 盈亏{p.get('unrealized_pnl_pct')}%"
        )
        if reason:
            msg += f"\n  > {reason}"
        lines.append(msg)
    return ["\n".join(lines)]


# ── 推送入口 ─────────────────────────────────────────────

def send_all(channel, url, messages: list[str],
             images: Optional[list[list[str]]] = None) -> int:
    """推送多条消息（每条单独发送），返回成功条数。

    images: 可选，与 messages 等长的图片列表（每条消息对应一组图片路径）。
    """
    if not messages:
        logger.info("无待推送消息")
        return 0
    sender = make_channel(channel, url)
    sent = 0
    for i, msg in enumerate(messages):
        imgs = images[i] if images and i < len(images) else None
        sender.send(msg, images=imgs)
        sent += 1
    logger.info("已推送 %d 条消息（渠道 %s）", sent, channel)
    return sent
