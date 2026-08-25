# -*- coding: utf-8 -*-
"""通知编排器核心 — 片段/渠道/渲染器/聚合器（FR-3）

设计意图（HLD §4.2 / ADR-7 内存聚合器）：
  - 信号源只产 `NotificationFragment`（topic + lines + priority），不关心最终怎么发；
  - 聚合器按「批次」把片段按 topic 分节合并为一个 `Digest`；
  - 渠道 `Channel.send(digest)` 统一发送接口（对齐 EmailSender/webhook 签名差异）；
  - 渲染器 `Renderer.render(digest, channel)` 按渠道定制消息体；
  - 渠道 + 渲染器用工厂装配，新增渠道零侵入。

三个扩展点：
  - 信号源：`agg.add(fragment)`；新增信号源 = 新增片段生产者；
  - 渠道：实现 `Channel` + 注册到 `CHANNEL_FACTORY`；
  - 渲染器：实现 `Renderer` + 注册到 `RENDERER_FACTORY`。

批次（priority）：
  - priority=0 即时（盘中操作建议），不聚合，直接发送；
  - priority=1 随批次（盘后周期），聚合为一封/一条。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

logger = logging.getLogger(__name__)

# 主题常量（默认分节，允许自定义字符串）
TOPIC_PRICE = "price"
TOPIC_FUNDFLOW = "fundflow"
TOPIC_ORDERS = "orders"
TOPIC_SUMMARY = "summary"

# 即时 / 批次
PRIORITY_INSTANT = 0
PRIORITY_BATCH = 1


@dataclass
class NotificationFragment:
    """通知片段（信号源产出）。

    topic: 分节主题（不硬编码枚举，一次消息内按 topic 分节去重）
    title: 分节标题
    lines: 正文行
    priority: 0=即时（直接发），1=随批次（进聚合器）
    """

    topic: str
    title: str
    lines: list = field(default_factory=list)
    priority: int = PRIORITY_BATCH


@dataclass
class Digest:
    """一批可发送的通知：按 topic 分节。"""

    sections: list = field(default_factory=list)   # [{topic, title, lines}]
    meta: dict = field(default_factory=dict)        # 批次/渠道/日期等

    def is_empty(self) -> bool:
        return not any(l for s in self.sections for l in s["lines"])

    def to_text(self) -> str:
        parts = []
        for s in self.sections:
            lines = [l for l in s["lines"] if l]
            if not lines:
                continue
            parts.append(f"{s['title']}\n" + "\n".join(f"- {l}" for l in lines))
        return "\n\n".join(parts)

    def to_markdown(self) -> str:
        parts = []
        for s in self.sections:
            lines = [l for l in s["lines"] if l]
            if not lines:
                continue
            parts.append(f"**{s['title']}**\n" + "\n".join(f"- {l}" for l in lines))
        return "\n\n".join(parts)


class Channel(Protocol):
    """统一发送渠道接口。

    实现需具备 `send(digest: Digest) -> dict`；to 兼容 EmailSender/webhook 签名。
    """

    def send(self, digest: Digest, **kwargs) -> dict:
        ...


class Renderer(Protocol):
    """按渠道渲染 Digest。"""

    def render(self, digest: Digest, channel: str, **kwargs) -> str:
        ...


class MessageAggregator:
    """内存聚合器：add(fragment) → digest()。

    按 topic 分节；同一 topic 合并 lines；空返回 None（不发送）。
    """

    def __init__(self):
        self._sections: dict[str, dict] = {}

    def add(self, frag: NotificationFragment):
        if not frag.lines:
            return
        sec = self._sections.setdefault(frag.topic, {"topic": frag.topic,
                                                     "title": frag.title,
                                                     "lines": []})
        # 去重（同内容行不重复）
        for line in frag.lines:
            if line not in sec["lines"]:
                sec["lines"].append(line)

    def digest(self, meta: Optional[dict] = None) -> Optional[Digest]:
        sections = [s for s in self._sections.values() if any(l for l in s["lines"])]
        if not sections:
            return None
        return Digest(sections=sections, meta=meta or {})

    def reset(self):
        self._sections.clear()


# ── 内置渲染器 ─────────────────────────────────────────────

class MarkdownRenderer:
    """企微 markdown 渲染（默认）。"""

    def render(self, digest: Digest, channel: str = "wecom", **kwargs) -> str:
        return digest.to_markdown()


class TextRenderer:
    """飞书纯文本渲染（去 markdown 装饰）。"""

    def render(self, digest: Digest, channel: str = "feishu", **kwargs) -> str:
        return digest.to_text()


class HtmlRenderer:
    """邮件 HTML 渲染（按 topic 分节成卡片）。"""

    def render(self, digest: Digest, channel: str = "email", **kwargs) -> str:
        if digest.is_empty():
            return ""
        cards = []
        for s in digest.sections:
            if not s["lines"]:
                continue
            rows = "".join(
                f'<tr><td style="padding:6px 10px;font-size:13px;'
                f'border-bottom:1px solid #e5e7eb;">{l}</td></tr>'
                for l in s["lines"]
            )
            cards.append(
                f'<div style="margin-bottom:14px;">'
                f'<div style="font-size:14px;font-weight:bold;color:#111827;'
                f'margin-bottom:6px;">{s["title"]}</div>'
                f'<table style="width:100%;border-collapse:collapse;background:#fff;'
                f'border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">'
                f'{rows}</table></div>'
            )
        return (
            f'<div style="font-family:Arial,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
            f'background:#f5f6f8;padding:16px;">'
            + "".join(cards) + "</div>"
        )


# ── 渠道工厂 ───────────────────────────────────────────────

def _email_channel(digest: Digest, **kwargs) -> dict:
    """Email 渠道 send：把 Digest 渲染为 HTML 正文发送。"""
    from StockInvestmentTool.notifier.channels import EmailSender

    subject = kwargs.get("subject") or (digest.meta.get("subject", "股票通知"))
    to = kwargs.get("to")
    sender = EmailSender(to=to) if to else EmailSender()
    html = HtmlRenderer().render(digest, "email")
    return sender.send(html, subject=subject, is_html=True)


def make_channel_adaptor(channel_name: str):
    """构造一个统一的 `send(digest)` 可调用（对齐 Channel 协议）。

    对 webhook 渠道：把 Digest 渲染为文本/markdown，走既有 make_channel；
    对 email 渠道：渲染 HTML 走 EmailSender。
    """
    from StockInvestmentTool.notifier.channels import make_channel
    name = (channel_name or "feishu").lower()

    def _send(digest: Digest, **kwargs) -> dict:
        if digest.is_empty():
            return {"ok": True, "skipped": True}
        if name in ("email", "mail", "smtp"):
            return _email_channel(digest, **kwargs)
        url = kwargs.get("url") or _webhook_url(name)
        channel = make_channel(name, url)
        rendered = TextRenderer().render(digest, name)
        # 企微用 markdown，飞书用纯文本
        return channel.send(rendered, msgtype="markdown" if name == "wecom" else "text")

    return _send


def _webhook_url(channel: str) -> str:
    import os
    env = "FEISHU_WEBHOOK_URL" if channel in ("feishu", "lark") else "WECOM_WEBHOOK_URL"
    url = os.getenv(env, "")
    if not url:
        raise RuntimeError(f"未配置 {env}，请在 .env 中填入 webhook 地址")
    return url


# 工厂装配（新增渠道在此注册）
CHANNEL_FACTORY: dict[str, Callable] = {
    "email": make_channel_adaptor("email"),
    "feishu": make_channel_adaptor("feishu"),
    "wecom": make_channel_adaptor("wecom"),
    "lark": make_channel_adaptor("feishu"),
}

RENDERER_FACTORY: dict[str, Callable] = {
    "email": HtmlRenderer,
    "feishu": TextRenderer,
    "wecom": MarkdownRenderer,
    "lark": TextRenderer,
}


def live_send_digest(digest: Digest, channel: str, **kwargs) -> dict:
    """按渠道发送一个 Digest（用工厂装配，返回发送结果）。"""
    name = (channel or "feishu").lower()
    sender = CHANNEL_FACTORY.get(name, CHANNEL_FACTORY["feishu"])
    return sender(digest, **kwargs)


# ── 聚合便捷入口（把既有 build_* 消息接入批量聚合）────────────

def build_digest_from_messages(topic_sections: list) -> Optional[Digest]:
    """把一批 signal（pre-obtained fragment 或 ops）聚合成一个 Digest。

    Args:
        topic_sections: 进入聚合器的片段列表，每项为
            NotificationFragment，或 (topic, title, lines[, priority])。
            priority 缺省 = PRIORITY_BATCH。
    """
    agg = MessageAggregator()
    for item in topic_sections or []:
        if isinstance(item, NotificationFragment):
            frag = item
        else:
            topic, title, lines = item[0], item[1], item[2]
            priority = item[3] if len(item) > 3 else PRIORITY_BATCH
            frag = NotificationFragment(topic, title, lines, priority)
        agg.add(frag)
    return agg.digest()


def send_batch_digest(digest: Digest, channel: str, **kwargs) -> dict:
    """发送一个批次 Digest（聚合后按渠道发一封/一条）。"""
    if digest is None or digest.is_empty():
        logger.info("聚合 Digest 为空，跳过发送（无触发内容）")
        return {"ok": True, "skipped": True}
    return live_send_digest(digest, channel, **kwargs)
