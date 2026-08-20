# -*- coding: utf-8 -*-
"""初筛结果导出 — CSV + JSON（默认输出到 output/screener/）

CSV 用 utf-8-sig 编码（Excel 直接打开不乱码）；
JSON 内含 规则/过程统计/命中明细，方便下游程序直接消费。
"""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from StockInvestmentTool.config import Config
from StockInvestmentTool.screener.pipeline import ScreenReport

DEFAULT_OUTPUT_DIR = Config.OUTPUT_DIR / "screener"


def _output_dir(rules_dir: str | None) -> Path:
    if rules_dir:
        out = Path(rules_dir)
    else:
        out = DEFAULT_OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    return out


def _build_rows(report: ScreenReport) -> pd.DataFrame:
    """统一导出列（中文名），只保留口径清晰的字段。"""
    df = report.df.copy()
    from StockInvestmentTool.screener.board import board_name

    rename = {
        "code": "代码",
        "name": "名称",
        "price": "现价",
        "change_pct": "涨跌幅%",
        "pe_ttm": "PE(TTM)",
        "pb": "PB",
        "total_mcap": "总市值(亿)",
        "float_mcap": "流通市值(亿)",
        "turnover": "换手率%",
        "vol_ratio": "量比",
        "limit_up": "涨停价",
        "limit_down": "跌停价",
        "high": "最高",
        "low": "最低",
    }
    if "board" in df.columns:
        df["板块"] = df["board"].map(board_name)
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    # 僵尸报价状态列（停牌/北交所老码迁移，非当日真实成交）
    if "is_stale" in df.columns:
        df["状态"] = df.apply(
            lambda r: ("⚠" + str(r.get("stale_reason", ""))) if r.get("is_stale") else "正常",
            axis=1,
        )
    # 列顺序: 代码 → 名称 → 板块 → 行情/估值
    order = ["代码", "名称", "板块", "现价", "涨跌幅%", "PE(TTM)", "PB",
             "总市值(亿)", "流通市值(亿)", "换手率%", "量比",
             "涨停价", "跌停价", "最高", "最低", "状态"]
    cols = [c for c in order if c in df.columns]
    return df[cols]


def export_result(report: ScreenReport, rules_dir: str | None = None) -> list[Path]:
    """导出 CSV/JSON 到 output/screener/，返回生成的文件路径列表。"""
    out_dir = _output_dir(rules_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    rows = _build_rows(report)
    formats = getattr(report.rules, "output_formats", ["csv", "json"])
    max_rows = getattr(report.rules, "max_rows", 1000)
    rows = rows.head(max_rows)

    paths: list[Path] = []
    if "csv" in formats:
        path = out_dir / f"screen_{ts}.csv"
        rows.to_csv(path, index=False, encoding="utf-8-sig")
        paths.append(path)

    if "json" in formats:
        payload = {
            "generated_at": report.started_at,
            "duration_s": report.duration_s,
            "universe_source": report.universe_source,
            "enrich_source": report.enrich_source,
            "rules": report.rules,
            "stages": [{"stage": s, "count": c} for s, c in report.stages],
            "warning": report.warning,
            "matched": report.matched,
            "stocks": rows.to_dict("records"),
        }
        path = out_dir / f"screen_{ts}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        paths.append(path)

    return paths
