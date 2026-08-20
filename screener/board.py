# -*- coding: utf-8 -*-
"""板块判定 — 代码前缀 → 交易板块（含需额外开通的权限标注）

A 股交易权限差异是初筛的第一道门槛:
  - 沪主板 / 深主板: 普通账户即可
  - 创业板(300/301)、科创板(688/689)、北交所: 需单独开通权限
本模块把「股票池全量」缩到「当前账户实际可交易」范围。
"""

from typing import Optional

# 板块定义: key → {中文名, 代码前缀, 需要额外开通的权限(None=普通账户)}
BOARDS: dict[str, dict] = {
    "main_sh": {"name": "沪主板", "prefixes": ("600", "601", "603", "605"), "permission": None},
    "main_sz": {"name": "深主板", "prefixes": ("000", "001", "002", "003"), "permission": None},
    "cyb": {"name": "创业板", "prefixes": ("300", "301"), "permission": "创业板"},
    "kcb": {"name": "科创板", "prefixes": ("688", "689"), "permission": "科创板"},
    "bse": {"name": "北交所", "prefixes": ("43", "83", "87", "88", "92"), "permission": "北交所"},
}

# 6 位纯数字 → 交易所前缀（sh/sz/bj）
def exchange_prefix(digits: str) -> Optional[str]:
    """根据 6 位代码推断交易所前缀。"""
    if digits.startswith(("600", "601", "603", "605", "688", "689")):
        return "sh"
    if digits.startswith(("000", "001", "002", "003", "300", "301")):
        return "sz"
    if digits.startswith(("43", "83", "87", "88", "92")):
        return "bj"
    return None


def normalize(code: str) -> str:
    """把 sh600900 / sz.000001 / 600900 / bj920982 统一为带前缀小写 sh600900。"""
    code = code.strip().lower().replace(".", "")
    if code.startswith(("sh", "sz", "bj")):
        return code
    if len(code) == 6 and code.isdigit():
        prefix = exchange_prefix(code)
        if prefix:
            return f"{prefix}{code}"
    return code


def detect_board(code: str) -> Optional[str]:
    """返回股票所属板块 key（main_sh/main_sz/cyb/kcb/bse），无法判定返回 None。"""
    code = normalize(code)
    digits = code[2:] if code[:2] in ("sh", "sz", "bj") else code
    for key, meta in BOARDS.items():
        if digits.startswith(meta["prefixes"]):
            return key
    return None


def board_name(key: Optional[str]) -> str:
    """板块 key → 中文名。"""
    if not key:
        return "未知"
    return BOARDS.get(key, {}).get("name", "未知")
