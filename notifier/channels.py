# -*- coding: utf-8 -*-
"""消息推送渠道适配器 — 企业微信 / 飞书 群机器人 webhook / 邮件 SMTP

设计原则:
    - 消息直达自己的 App（企业微信/飞书）或邮箱（SMTP），不经过任何第三方中转
    - 渠道通过环境变量/配置选择: NOTIFY_CHANNEL = wecom | feishu | email
    - 统一入口 send()：外部只需关心「发一段文本」即可
"""

import logging
import os
import re
import smtplib
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
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


# ── 邮件 SMTP ─────────────────────────────────────────────

class EmailSender:
    """SMTP 邮件发送（QQ/163 等，SSL 465）。

    配置走环境变量（.env）:
        EMAIL_SMTP_HOST   smtp.qq.com
        EMAIL_SMTP_PORT   465
        EMAIL_USER        发件邮箱账号
        EMAIL_PASSWORD    SMTP 授权码（非登录密码）
        EMAIL_TO          收件人（逗号分隔多个）
    """

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 user: Optional[str] = None, password: Optional[str] = None,
                 to: Optional[str] = None, timeout: float = 30.0):
        self.host = host or os.getenv("EMAIL_SMTP_HOST", "smtp.qq.com")
        self.port = int(port or os.getenv("EMAIL_SMTP_PORT", "465"))
        self.user = user or os.getenv("EMAIL_USER", "")
        self.password = password or os.getenv("EMAIL_PASSWORD", "")
        self.to = to or os.getenv("EMAIL_TO", "")
        self.timeout = timeout

    def send(self, content: str, subject: str = "股票提醒",
             images: Optional[list[str]] = None) -> dict:
        """发送邮件。

        Args:
            content: 正文文本（自动转 HTML，保留换行）。
            subject: 标题。
            images: 可选图片文件路径列表（PNG），内嵌 CID 展示。
        """
        if not self.user or not self.password:
            raise ChannelError("邮件未配置 EMAIL_USER / EMAIL_PASSWORD（.env）")
        if not self.to:
            raise ChannelError("邮件未配置 EMAIL_TO 收件人（.env）")

        # 纯文本 → HTML（换行转 <br>）
        body_html = "<br>".join(
            line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            for line in content.splitlines()
        )

        if images:
            msg = MIMEMultipart("related")
            msg_alt = MIMEMultipart("alternative")
            msg_alt.attach(MIMEText(content, "plain", "utf-8"))
            msg_alt.attach(MIMEText(body_html, "html", "utf-8"))
            msg.attach(msg_alt)
            for idx, img_path in enumerate(images):
                try:
                    with open(img_path, "rb") as f:
                        mime = MIMEImage(f.read())
                    mime.add_header("Content-ID", f"<snapshot{idx}>")
                    mime.add_header("Content-Disposition", "inline", filename=f"snapshot{idx}.png")
                    msg.attach(mime)
                except OSError as e:
                    logger.warning("内嵌图片读取失败 %s: %s", img_path, e)
        else:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(content, "plain", "utf-8"))
            msg.attach(MIMEText(body_html, "html", "utf-8"))

        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header("Stock投资助手", "utf-8")), self.user))
        msg["To"] = ",".join(self.to.split(","))
        msg["Date"] = formatdate(localtime=True)

        try:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout) as s:
                s.login(self.user, self.password)
                s.sendmail(self.user, [x.strip() for x in self.to.split(",") if x.strip()], msg.as_string())
        except (smtplib.SMTPException, OSError) as e:
            raise ChannelError(f"邮件发送失败: {e}") from e
        logger.info("邮件已发送: %s → %s", self.user, self.to)
        return {"ok": True, "to": self.to, "images": len(images or [])}


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
    """按渠道名构造发送器。

    url 参数: webhook 渠道用 URL；email 渠道忽略（配置走环境变量）。
    """
    channel = (channel or "").lower()
    if channel == "wecom":
        return WecomWebhook(url, timeout)
    if channel in ("feishu", "lark", "飞书"):
        return FeishuWebhook(url, timeout)
    if channel in ("email", "mail", "smtp", "邮件"):
        return EmailSender()
    raise ChannelError(f"未知通知渠道: {channel}（支持 wecom / feishu / email）")
