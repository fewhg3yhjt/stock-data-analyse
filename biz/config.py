# -*- coding: utf-8 -*-
"""业务平台配置与路径隔离。

业务库路径可通过环境变量 BUSINESS_DB_PATH 覆盖，默认 output/data/business.db。
测试必须使用临时路径，禁止写生产业务库。
"""

from __future__ import annotations

import os
from pathlib import Path

from StockInvestmentTool.config import Config


def business_db_path() -> Path:
    """返回业务库路径。支持环境变量 BUSINESS_DB_PATH 隔离（测试/验证用）。"""
    override = os.environ.get("BUSINESS_DB_PATH")
    if override:
        path = Path(override)
    else:
        path = Config.DATA_DIR / "business.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path