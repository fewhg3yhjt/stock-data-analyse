"""Read-only unit audit for stock_daily Raw Batches and Published partitions."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pandas as pd

from StockInvestmentTool.warehouse import Warehouse


def audit(partition: str, *, code: str | None = None) -> tuple[pd.DataFrame, dict]:
    warehouse = Warehouse()
    normalized = (code or "").lower().replace(".", "")
    with sqlite3.connect(warehouse.meta_db_path) as conn:
        rows = conn.execute(
            """SELECT batch_id, source_name, raw_path, request_context,
                      trade_date_start, trade_date_end
               FROM source_batches
               WHERE dataset_name='stock_daily' AND status IN ('success','partial_success')
                 AND trade_date_start <= ? AND trade_date_end >= ?
               ORDER BY finished_at, batch_id""",
            (f"{partition}-31", f"{partition}-01"),
        ).fetchall()
    facts = []
    for batch_id, source, raw_path, context_json, start, end in rows:
        path = Path(raw_path or "")
        if not path.exists():
            continue
        context = json.loads(context_json or "{}")
        frame = pd.read_parquet(path)
        if "date" not in frame or "code" not in frame:
            continue
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame[frame["date"].dt.strftime("%Y-%m") == partition]
        if normalized:
            frame = frame[frame["code"].astype(str).str.lower().str.replace(".", "", regex=False) == normalized]
        for row in frame.to_dict(orient="records"):
            volume = float(row.get("volume") or 0)
            amount = float(row.get("amount") or 0)
            close = float(row.get("close") or 0)
            implied = amount / (volume * close) if volume and close else None
            inferred = (
                "likely_hand_wan_yuan" if implied is not None and 0.002 <= implied <= 0.05
                else "likely_share_yuan" if implied is not None and 0.2 <= implied <= 5.0
                else "unresolved"
            )
            facts.append({
                "layer": "raw", "batch_id": batch_id, "source": source,
                "volume_unit": (context.get("units") or {}).get("volume"),
                "amount_unit": (context.get("units") or {}).get("amount"),
                "unit_resolution": (context.get("units") or {}).get("resolution"),
                "date": str(row.get("date"))[:10], "code": row.get("code"),
                "volume": row.get("volume"), "amount": row.get("amount"),
                "close": row.get("close"), "turn": row.get("turn"),
                "implied_amount_volume_close": implied,
                "audit_unit_inference": inferred,
            })
    published = pd.read_parquet(warehouse.daily_partition(partition))
    published["date"] = pd.to_datetime(published["date"], errors="coerce")
    if normalized:
        published = published[published["code"].astype(str).str.lower().str.replace(".", "", regex=False) == normalized]
    for row in published.to_dict(orient="records"):
        volume = float(row.get("volume") or 0)
        amount = float(row.get("amount") or 0)
        close = float(row.get("close") or 0)
        facts.append({
            "layer": "published", "batch_id": "", "source": "stock_daily",
            "volume_unit": "share", "amount_unit": "yuan", "unit_resolution": "published_contract",
            "date": str(row.get("date"))[:10], "code": row.get("code"),
            "volume": volume, "amount": amount, "close": close, "turn": row.get("turn"),
            "implied_amount_volume_close": amount / (volume * close) if volume and close else None,
        })
    frame = pd.DataFrame(facts)
    summary = {
        "partition": partition,
        "code": normalized or None,
        "raw_batches": len(rows),
        "raw_unit_groups": frame[frame["layer"] == "raw"].fillna("missing").groupby(
            ["volume_unit", "amount_unit", "unit_resolution", "audit_unit_inference"], dropna=False
        ).size().reset_index(name="rows").to_dict(orient="records"),
        "published_unit_anomalies": int((
            (frame["layer"] == "published")
            & ((frame["implied_amount_volume_close"] < 0.2) | (frame["implied_amount_volume_close"] > 5.0))
        ).sum()),
    }
    return frame, summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only stock_daily unit audit")
    parser.add_argument("--partition", required=True, help="YYYY-MM")
    parser.add_argument("--code", default="", help="optional normalized stock code")
    parser.add_argument("--output-dir", required=True, help="host-visible output directory")
    args = parser.parse_args()
    frame, summary = audit(args.partition, code=args.code)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"stock_daily_unit_audit_{args.partition.replace('-', '')}_{(args.code or 'all').replace('.', '')}"
    frame.to_csv(out / f"{stem}.csv", index=False)
    (out / f"{stem}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"csv": str(out / f"{stem}.csv"), "summary": str(out / f"{stem}.json"), **summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
