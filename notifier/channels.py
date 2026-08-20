# -*- coding: utf-8 -*-
"""消息推送渠道适配器 — 企业微信 / 飞书 群机器人 webhook

设计原则:
    - 消息直达自己的 App（企业微信/飞书），不经过任何第三方中转
    - 渠道通过环境变量/配置选择: NOTIFY_CHANNEL = wecom | feishu
    - 统一入口 send()：外部只需关心「发一段文本」即可
"""

import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class ChannelError(RuntimeError):
    """推送失败（网络/接口返回错误）。"""


# ── 企业微信群机器人 ─────────────────────────────────────

class WecomWebhook:
    """企业微信群机器人。payload 支持 text / markdown。"""

    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout

    def send(self, content: str, msgtype: str = "markdown") -> dict:
        payload = {"msgtype": msgtype, msgtype: {"content": content}}
        return _post_webhook(self.url, payload, self.timeout)


# ── 飞书群机器人 ─────────────────────────────────────────

class FeishuWebhook:
    """飞书群自定义机器人。默认 text，可切 interactive 卡片。"""

    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout

    def send(self, content: str, msgtype: str = "text") -> dict:
        if msgtype == "interactive":
            payload = {
                "msgtype": "interactive",
                "card": _feishu_card(content),
            }
        else:
            # text 消息；企微 markdown 语法在飞书会原样显示，做轻度清洗
            payload = {"msgtype": "text", "content": {"text": _feishu_text(content)}}
        return _post_webhook(self.url, payload, self.timeout)


def _feishu_text(md: str) -> str:
    """企微 markdown → 飞书纯文本（去掉 markdown 装饰符号）。"""
    lines = []
    for line in md.splitlines():
        line = line.strip()
        if not line:
            lines.append("")
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)          # 标题
        line = re.sub(r"^>\s*", "", line)               # 引用
        line = line.replace("**", "").replace("`", "")  # 加粗/行内码
        lines.append(line)
    return "\n".join(lines)


def _feishu_card(md: str) -> dict:
    """企微 markdown → 飞书 interactive 卡片（lark_md 基本语法）。"""
    return {
        "header": {"title": {"tag": "plain_text", "content": "股票提醒"}},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": md}}],
    }


# ── 通用 ─────────────────────────────────────────────────

def _post_webhook(url: str, payload: dict, timeout: float) -> dict:
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise ChannelError(f"webhook 请求失败: {e}") from e
    try:
        data = resp.json()
    except ValueError:
        raise ChannelError(f"webhook 返回非 JSON(status={resp.status_code}): {resp.text[:200]}") from None
    errcode = data.get("errcode") if isinstance(data, dict) else None
    if errcode not in (0, None):
        raise ChannelError(f"webhook 返回错误 errcode={errcode}: {data}")
    return data


def make_channel(channel: str, url: str, timeout: float = 10.0):
    """按渠道名构造发送器。"""
    channel = (channel or "").lower()
    if channel == "wecom":
        return WecomWebhook(url, timeout)
    if channel in ("feishu", "lark", "飞书"):
        return FeishuWebhook(url, timeout)
    raise ChannelError(f"未知通知渠道: {channel}（支持 wecom / feishu）")
