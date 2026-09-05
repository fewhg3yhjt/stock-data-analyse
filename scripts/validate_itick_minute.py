"""Validate and publish iTick 1-minute OHLCV files without altering raw prices."""

from __future__ import annotations

import json
from pathlib import Path

import baostock as bs
import pandas as pd


INPUT_DIR = Path("output/itick_minute")
REPORT_PATH = INPUT_DIR / "quality_report.json"
SYMBOLS = ("000400", "000425", "000725")
START = pd.Timestamp("2026-06-15 09:30:00")
END = pd.Timestamp("2026-09-04 15:00:00")


def _factor_events(instrument: str) -> pd.DataFrame:
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock login failed: {login.error_msg}")
    try:
        rs = bs.query_adjust_factor(
            code=f"sz.{instrument}", start_date="2026-06-15", end_date="2026-09-04"
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if rs.error_code != "0":
            raise RuntimeError(f"adjust factor query failed: {rs.error_msg}")
        return pd.DataFrame(
            rows,
            columns=["code", "event_date", "fore_factor", "back_factor", "adjust_factor"],
        )
    finally:
        bs.logout()


def _expected_slots() -> set[str]:
    morning = pd.date_range("09:30", "11:29", freq="min").strftime("%H:%M")
    afternoon = pd.date_range("13:00", "14:59", freq="min").strftime("%H:%M")
    return set(morning) | set(afternoon)


def validate_one(instrument: str) -> dict:
    source = INPUT_DIR / f"{instrument}_1m_raw.parquet"
    frame = pd.read_parquet(source)
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame[(frame["date"] >= START) & (frame["date"] <= END)].copy()
    frame["trade_date"] = frame["date"].dt.strftime("%Y-%m-%d")
    frame["time"] = frame["date"].dt.strftime("%H:%M")

    # Exclude auction records such as 09:25; keep the regular-session 15:00 bar if present.
    regular = frame[frame["time"].isin(_expected_slots() | {"15:00"})].copy()
    factor_events = _factor_events(instrument)
    regular["adjust_factor"] = 1.0
    for event in factor_events.itertuples(index=False):
        event_date = str(event.event_date)
        regular.loc[regular["trade_date"] < event_date, "adjust_factor"] = float(event.adjust_factor)

    bad_ohlc = regular[
        (regular["high"] < regular[["open", "close", "low"]].max(axis=1))
        | (regular["low"] > regular[["open", "close", "high"]].min(axis=1))
        | (regular[["open", "high", "low", "close"]] <= 0).any(axis=1)
    ]
    bad_volume = regular[(regular["volume"] < 0) | (regular["amount"] < 0)]
    expected = _expected_slots()
    daily = []
    for day, group in regular.groupby("trade_date"):
        actual = set(group["time"])
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected - {"15:00"})
        daily.append({
            "trade_date": day,
            "rows": int(len(group)),
            "missing_regular_minutes": missing,
            "unexpected_minutes": unexpected,
            "has_15_00": "15:00" in actual,
        })

    final_columns = [
        "date", "instrument", "open", "high", "low", "close", "volume", "amount", "adjust_factor",
    ]
    regular[final_columns].sort_values("date").to_parquet(INPUT_DIR / f"{instrument}_1m.parquet", index=False)
    return {
        "instrument": f"{instrument}.SZ",
        "rows_raw": int(len(frame)),
        "rows_regular": int(len(regular)),
        "trade_days": len(daily),
        "raw_range": [str(frame["date"].min()), str(frame["date"].max())],
        "regular_range": [str(regular["date"].min()), str(regular["date"].max())],
        "ohlc_invalid_rows": int(len(bad_ohlc)),
        "negative_volume_rows": int((regular["volume"] < 0).sum()),
        "negative_amount_rows": int((regular["amount"] < 0).sum()),
        "null_rows": int(regular[final_columns].isna().any(axis=1).sum()),
        "factor_events": factor_events.to_dict("records"),
        "days_with_missing_minutes": [item for item in daily if item["missing_regular_minutes"]],
        "daily_counts": daily,
        "raw_file": str(source),
        "published_file": str(INPUT_DIR / f"{instrument}_1m.parquet"),
        "adjusted_file": None,
    }


def main() -> None:
    report = {instrument: validate_one(instrument) for instrument in SYMBOLS}
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps({key: {
        "rows_raw": value["rows_raw"],
        "rows_regular": value["rows_regular"],
        "trade_days": value["trade_days"],
        "bad_ohlc": value["ohlc_invalid_rows"],
        "missing_days": len(value["days_with_missing_minutes"]),
        "factor_events": len(value["factor_events"]),
    } for key, value in report.items()}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
