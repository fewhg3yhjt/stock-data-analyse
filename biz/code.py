# -*- coding: utf-8 -*-
"""canonical security code 规范。

依据 docs/DATA_PIPELINE_V1_DESIGN.md §4.0：
  - 业务、存储、API 和数据集统一使用 canonical code：sh600908 / sz000823 / bj920982
  - 规则：小写、交易所前缀、6 位数字、无点号
  - 裸 6 位代码只允许作为外部输入，进入系统后必须补齐交易所前缀
  - Baostock 的 sh.600908 只允许存在于 Source Adapter 调用边界
  - 后缀式 000300.SH 只允许作为外部输入解析，不得落库
"""

from __future__ import annotations

import re

_CANONICAL_RE = re.compile(r"^(sh|sz|bj)\d{6}$")
_RAW6_RE = re.compile(r"^\d{6}$")
# 前缀.数字 如 sh.600908
_PREFIX_DOT_RE = re.compile(r"^(sh|sz|bj)\.(\d{6})$")
# 数字.后缀 如 600908.SH / 000300.SH
_SUFFIX_DOT_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ|SS|SSE|SZSE)$", re.IGNORECASE)

# 后缀 → 前缀 映射
_SUFFIX_TO_PREFIX = {
    "SH": "sh",
    "SS": "sh",
    "SSE": "sh",
    "SZ": "sz",
    "SZSE": "sz",
    "BJ": "bj",
}


class InvalidCodeError(ValueError):
    """证券代码无法转换为 canonical 形式。"""


def _infer_prefix(code6: str) -> str:
    """根据 6 位数字推断交易所前缀。"""
    if code6.startswith("6"):
        return "sh"            # 600/601/603/605/688/689 沪市A股
    if code6.startswith("5"):
        return "sh"            # 5xx 上交所基金/ETF（510300 沪深300ETF）
    if code6.startswith("92"):
        return "bj"            # 920 北交所新代码段
    if code6.startswith("9"):
        return "sh"            # 900 B 股
    if code6.startswith("4") or code6.startswith("8"):
        return "bj"            # 北交所 / 新三板
    return "sz"                # 0/1/2/3 深市（000/001/002/003/300/301/200/159/16x）


def normalize(code: str | None) -> str:
    """将任意可接受的外部代码形式转换为 canonical code。

    支持输入：
      - canonical：sh600908 / sz000823
      - 裸 6 位：600908（按规则推断前缀）
      - 前缀点号：sh.600908（Baostock 边界）
      - 数字点后缀：600908.SH / 000300.SH / 000001.SZ
      - 大小写混用：SH600908 / Sh.600908
    """
    if code is None:
        raise InvalidCodeError("code is None")
    raw = str(code).strip().lower()
    if not raw:
        raise InvalidCodeError("code is empty")

    if _CANONICAL_RE.match(raw):
        return raw

    if _RAW6_RE.match(raw):
        return _infer_prefix(raw) + raw

    m = _PREFIX_DOT_RE.match(raw)
    if m:
        return m.group(1) + m.group(2)

    m = _SUFFIX_DOT_RE.match(str(code).strip())
    if m:
        prefix = _SUFFIX_TO_PREFIX[m.group(2).upper()]
        return prefix + m.group(1)

    raise InvalidCodeError(f"cannot normalize code: {code!r}")


def is_canonical(code: str) -> bool:
    return bool(_CANONICAL_RE.match(code))


def validate_canonical(code: str) -> str:
    """校验并返回 canonical code，非法时抛 InvalidCodeError。"""
    norm = normalize(code)
    if not is_canonical(norm):
        raise InvalidCodeError(f"invalid canonical code: {code!r}")
    return norm