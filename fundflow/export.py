# -*- coding: utf-8 -*-
"""资金流结果导出 — output/fundflow/ 下多 CSV + 汇总 JSON

payload 要求完全 JSON 可序列化（DataFrame 先转 records），
CSV 也从 records 重建，避免混用类型。
"""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from StockInvestmentTool.config import Config

DEFAULT_OUTPUT_DIR = Config.OUTPUT_DIR / "fundflow"


def _out_dir(output_dir: str | None) -> Path:
    out = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    return out


_RENAME_SECTOR = {
    "name": "板块", "index": "指数", "chg": "涨跌幅%", "in": "流入(亿)",
    "out": "流出(亿)", "net": "净额(亿)", "net_days": "多日净额(亿)",
    "chg_days": "多日涨跌%", "trend": "资金趋势", "leader": "领涨股",
    "leader_chg": "领涨涨跌%", "count": "家数",
}
_RENAME_STOCK = {
    "name": "名称", "chg": "涨跌幅%", "in": "流入(亿)",
    "out": "流出(亿)", "net": "净额(亿)", "amount": "成交额(亿)",
    "net_days": "多日净额(亿)", "chg_days": "多日涨跌%", "trend": "资金趋势",
}
_COMMON = {
    "code": "代码", "price": "现价", "turnover": "换手率%",
}


def _fmt_rows(rows: list[dict], rename: dict) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.rename(columns={k: v for k, v in rename.items() if k in df.columns})


def export_result(payload: dict, output_dir: str | None = None) -> list[Path]:
    """payload 结构:
        {
          "meta": {...},
          "overview": {...},
          "industry": {"now": [records], "days": [records]},
          "concept":  {...同},
          "stock": {"now": [records], "analysis": {榜名: [records]}},
        }
    返回生成的文件路径列表。
    """
    out_dir = _out_dir(output_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    paths: list[Path] = []

    def write_csv(name: str, rows: list[dict], rename: dict) -> Path:
        path = out_dir / f"fundflow_{ts}_{name}.csv"
        _fmt_rows(rows, rename).to_csv(path, index=False, encoding="utf-8-sig")
        paths.append(path)
        return path

    for kind in ("industry", "concept"):
        block = payload.get(kind)
        if not block or not block.get("now"):
            continue
        write_csv(f"{kind}_now", block["now"], _RENAME_SECTOR)
        if block.get("days"):
            write_csv(f"{kind}_{payload['meta'].get('trend_period', '3d')}", block["days"], _RENAME_SECTOR)

    stock = payload.get("stock")
    if stock and stock.get("now"):
        write_csv("stock_now", stock["now"], {**_RENAME_STOCK, **_COMMON})
        for name, rows in (stock.get("analysis") or {}).items():
            if rows:
                write_csv(f"stock_{name}", rows, {**_RENAME_STOCK, **_COMMON})

    json_path = out_dir / f"fundflow_{ts}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    paths.append(json_path)
    return paths
