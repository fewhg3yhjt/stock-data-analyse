"""Persist standardized money-flow snapshots as immutable raw batches."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from StockInvestmentTool.warehouse.source_capture import capture_frames
from StockInvestmentTool.warehouse.storage import Warehouse


def capture_money_flow(kind: str = "stock", period: str = "now",
                       *, warehouse: Optional[Warehouse] = None,
                       frame: Optional[pd.DataFrame] = None) -> dict:
    """Fetch or persist one standardized money-flow snapshot."""
    from StockInvestmentTool.fundflow.sources import fetch_sector, fetch_stock

    warehouse = warehouse or Warehouse()
    frame = frame if frame is not None else (fetch_stock(period) if kind == "stock" else fetch_sector(kind, period))
    if frame is None or frame.empty:
        raise ValueError("资金流数据为空")
    out = frame.copy()
    out["period"] = period
    if "code" not in out:
        out["code"] = out.get("name", pd.Series(range(len(out)))).astype(str)
    result = capture_frames(
        warehouse, dataset_name="money_flow_daily", source_name="ths", frames=[out],
        expected_symbols=int(out["code"].nunique()), success_symbols=int(out["code"].nunique()),
        universe_id=f"money_flow_{kind}_{datetime.now():%Y%m%d}",
        request_context={"kind": kind, "period": period},
    )
    return {"kind": kind, "period": period, "rows": len(out),
            "raw_batch_id": result["batch_id"], "raw_path": str(result["raw"]["path"])}
