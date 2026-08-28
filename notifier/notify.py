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
        lines.append("- 今日无达到阈值的持续流入证券")
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


def build_actionable_html(data: dict) -> str:
    """把有操作建议的持仓渲染为 HTML 邮件正文（摘要卡片 + 关键点位）。

    用内联样式（邮件客户端兼容），供邮件渠道直接发送。
    """
    positions = data.get("positions") or []
    actionable = [p for p in positions
                  if (p.get("advice") or {}).get("is_actionable")]
    if not actionable:
        return ""

    # 建议标签颜色
    def label_color(t: str) -> str:
        return {
            "buy_more": "#1a73e8",
            "partial_sell": "#e67e22",
            "sell_all": "#dc3545",
            "adjust_stop": "#9c27b0",
        }.get(t, "#6b7280")

    def label_text(t: str) -> str:
        return {
            "buy_more": "加仓", "partial_sell": "减仓",
            "sell_all": "清仓", "adjust_stop": "调止损",
        }.get(t, t)

    cards = []
    for p in actionable:
        adv = p.get("advice") or {}
        atype = adv.get("advice_type", "")
        reason = adv.get("reason", "")
        color = label_color(atype)
        ltext = label_text(atype)
        cur = p.get("current_price")
        avg = p.get("avg_cost")
        pnl = p.get("unrealized_pnl_pct")
        pnl_color = "#28a745" if (pnl or 0) >= 0 else "#dc3545"
        rs = p.get("right_side") or {}
        ls = p.get("left_side") or {}
        bm = p.get("buy_more") or {}
        hard_cap = p.get("hard_cap")

        # 关键点位行
        points = []
        if hard_cap:
            points.append(f'<td style="padding:6px 10px;font-size:12px;"><span style="color:#1a73e8;">止盈硬上限</span><br><b>{hard_cap}</b></td>')
        if rs.get("trigger_price"):
            points.append(f'<td style="padding:6px 10px;font-size:12px;"><span style="color:#e67e22;">右侧止盈线</span><br><b>{rs["trigger_price"]}</b></td>')
        if ls.get("year_high"):
            points.append(f'<td style="padding:6px 10px;font-size:12px;"><span style="color:#e67e22;">前高</span><br><b>{ls["year_high"]}</b></td>')
        if bm.get("trigger_price"):
            points.append(f'<td style="padding:6px 10px;font-size:12px;"><span style="color:#1a73e8;">补仓线</span><br><b>{bm["trigger_price"]}</b></td>')
        if not points:
            points.append(f'<td style="padding:6px 10px;font-size:12px;color:#6b7280;">—</td>')

        reason_html = (f'<div style="font-size:12px;color:#374151;background:#f8f9fb;'
                       f'padding:8px 10px;border-left:3px solid {color};'
                       f'margin-top:8px;border-radius:0 4px 4px 0;">{reason}</div>'
                       if reason else "")

        # 操作细节：左侧止盈的具体区间价格 + 减仓股数（"怎么得来的"）
        detail_html = ""
        if atype == "partial_sell" and ls:
            zone = {1: "预警区", 2: "第一止盈区"}.get(ls.get("tier"), "")
            ratio_pct = round((ls.get("sell_ratio") or 0) * 100)
            shares = ls.get("sell_shares")
            parts = [f"前高 {ls.get('year_high')}"]
            if ls.get("zone_price_lo") and ls.get("zone_price_hi"):
                parts.append(f"{zone}区间 {ls['zone_price_lo']}~{ls['zone_price_hi']}")
            parts.append(f"当前价占前高 {ls.get('pct_of_year_high')}%")
            detail = f"减仓 {ratio_pct}%"
            if shares:
                detail += f"（约 {int(shares)} 股）"
            parts.append(detail)
            detail_html = (f'<div style="font-size:12px;color:#111827;background:#fff8e1;'
                           f'padding:8px 10px;border-left:3px solid #f59e0b;'
                           f'margin-top:6px;border-radius:0 4px 4px 0;">'
                           f'<b>操作依据</b>：{"，".join(parts)}</div>')
        elif atype == "sell_all" and rs.get("trigger_price"):
            detail_html = (f'<div style="font-size:12px;color:#111827;background:#fdecea;'
                           f'padding:8px 10px;border-left:3px solid #dc3545;'
                           f'margin-top:6px;border-radius:0 4px 4px 0;">'
                           f'<b>操作依据</b>：峰值 {rs.get("peak_price")}，'
                           f'跌破右侧止盈线 {rs.get("trigger_price")} 触发清仓'
                           f'（已回撤 {rs.get("drawdown_pct")}%）</div>')

        cards.append(f'''
<table style="width:100%;border-collapse:separate;border-spacing:0;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;margin-bottom:14px;font-family:Arial,'PingFang SC','Microsoft YaHei',sans-serif;overflow:hidden;">
  <tr>
    <td style="padding:12px 14px;border-bottom:1px solid #e5e7eb;background:#f8f9fb;">
      <span style="font-size:15px;font-weight:bold;color:#111827;">{p.get('stock_name')}</span>
      <span style="font-size:12px;color:#6b7280;margin-left:6px;">{p.get('stock_code')}</span>
      <span style="float:right;background:{color};color:#fff;padding:2px 12px;border-radius:12px;font-size:12px;font-weight:bold;">{ltext}</span>
    </td>
  </tr>
  <tr>
    <td style="padding:12px 14px;">
      <table style="width:100%;">
        <tr>
          <td style="padding:6px 10px;font-size:12px;"><span style="color:#6b7280;">现价</span><br><b style="font-size:15px;">{cur}</b></td>
          <td style="padding:6px 10px;font-size:12px;"><span style="color:#6b7280;">成本</span><br><b>{avg}</b></td>
          <td style="padding:6px 10px;font-size:12px;"><span style="color:#6b7280;">盈亏</span><br><b style="color:{pnl_color};">{pnl}%</b></td>
          {''.join(points)}
        </tr>
      </table>
      {reason_html}
      {detail_html}
    </td>
  </tr>
</table>''')

    return (
        f'<div style="font-family:Arial,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
        f'background:#f5f6f8;padding:16px;">'
        f'<div style="font-size:16px;font-weight:bold;color:#111827;margin-bottom:12px;">'
        f'🔔 持仓操作提醒（{data.get("data_date", "")}）</div>'
        + "".join(cards) +
        '</div>'
    )


def build_orders_html(data: dict) -> str:
    """把全部持仓渲染为 HTML 邮件正文（盘后汇总，含每只状态/建议/盈亏）。"""
    positions = data.get("positions") or []
    if not positions:
        return "<div>当前无持仓</div>"

    summary = data.get("summary") or {}
    rows = []
    for p in positions:
        adv = p.get("advice") or {}
        atype = adv.get("advice_type", "")
        label = p.get("advice_label") or "—"
        label_color = {
            "buy_more": "#1a73e8", "partial_sell": "#e67e22",
            "sell_all": "#dc3545", "adjust_stop": "#9c27b0",
        }.get(atype, "#6b7280")
        pnl = p.get("unrealized_pnl_pct")
        pnl_color = "#28a745" if (pnl or 0) >= 0 else "#dc3545"
        reason = (adv.get("reason") or "")[:80]
        rows.append(
            f'<tr>'
            f'<td style="padding:8px 10px;font-size:13px;border-bottom:1px solid #e5e7eb;">'
            f'{p.get("stock_name")}<span style="color:#6b7280;font-size:11px;"> {p.get("stock_code")}</span></td>'
            f'<td style="padding:8px 10px;font-size:13px;border-bottom:1px solid #e5e7eb;">{p.get("current_price")}</td>'
            f'<td style="padding:8px 10px;font-size:13px;border-bottom:1px solid #e5e7eb;">{p.get("avg_cost")}</td>'
            f'<td style="padding:8px 10px;font-size:13px;color:{pnl_color};border-bottom:1px solid #e5e7eb;">{pnl}%</td>'
            f'<td style="padding:8px 10px;font-size:13px;border-bottom:1px solid #e5e7eb;">'
            f'<span style="background:{label_color};color:#fff;padding:1px 8px;border-radius:10px;font-size:11px;">{label}</span>'
            + (f'<div style="font-size:11px;color:#6b7280;margin-top:2px;">{reason}</div>' if reason else "")
            + '</td>'
            '</tr>'
        )

    return (
        f'<div style="font-family:Arial,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
        f'background:#f5f6f8;padding:16px;">'
        f'<div style="font-size:16px;font-weight:bold;color:#111827;margin-bottom:6px;">'
        f'📊 盘后持仓汇总（{data.get("data_date", "")}）</div>'
        f'<div style="font-size:13px;color:#6b7280;margin-bottom:12px;">'
        f'持仓 {summary.get("position_count", 0)} 只 | 总盈亏 '
        f'{summary.get("total_pnl_pct", "—")}% | {data.get("risk_status", "")}</div>'
        f'<table style="width:100%;border-collapse:collapse;background:#fff;'
        f'border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">'
        f'<tr style="background:#f8f9fb;">'
        f'<th style="padding:8px 10px;font-size:12px;text-align:left;color:#6b7280;">股票</th>'
        f'<th style="padding:8px 10px;font-size:12px;text-align:left;color:#6b7280;">现价</th>'
        f'<th style="padding:8px 10px;font-size:12px;text-align:left;color:#6b7280;">成本</th>'
        f'<th style="padding:8px 10px;font-size:12px;text-align:left;color:#6b7280;">盈亏</th>'
        f'<th style="padding:8px 10px;font-size:12px;text-align:left;color:#6b7280;">建议</th>'
        f'</tr>{"".join(rows)}</table></div>'
    )

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
