"""Collect historical 1-minute OHLCV from iTick with free-tier throttling.

The API key is read from /home/ubuntu/.config/itick/api_key by default and is
never written to output files. Pages are checkpointed outside the repository
so an interrupted run can resume without re-requesting completed pages.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests


API_URL = "https://api-free.itick.org/stock/kline"
SHANGHAI = timezone(timedelta(hours=8))
SYMBOLS = (
    "000400", "000425", "000725", "601318", "600036", "600519", "000333",
    "300750", "002594", "002230", "603501", "601899", "600031", "600941", "300308",
)
START = datetime(2026, 6, 15, 9, 30, tzinfo=SHANGHAI)
END = datetime(2026, 9, 4, 15, 0, tzinfo=SHANGHAI)
PAGE_SIZE = 500
MIN_REQUEST_INTERVAL = 15.0
OUTPUT_COLUMNS = [
    "date", "instrument", "open", "high", "low", "close", "volume", "amount",
]

logger = logging.getLogger("collect_itick_minute")


def _epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _format_time(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _parse_page(rows: list[dict], instrument: str) -> pd.DataFrame:
    parsed = []
    for row in rows:
        if not all(row.get(key) is not None for key in ("t", "o", "h", "l", "c", "v", "tu")):
            continue
        parsed.append({
            "date": pd.to_datetime(row["t"], unit="ms", utc=True).tz_convert(SHANGHAI).tz_localize(None),
            "instrument": f"{instrument}.{'SH' if instrument.startswith(('5', '6', '9')) else 'SZ'}",
            "open": float(row["o"]),
            "high": float(row["h"]),
            "low": float(row["l"]),
            "close": float(row["c"]),
            "volume": float(row["v"]),
            "amount": float(row["tu"]),
        })
    return pd.DataFrame(parsed, columns=OUTPUT_COLUMNS)


def _request(session: requests.Session, key: str, instrument: str, et: int) -> tuple[pd.DataFrame, int | None]:
    params = {
        "region": "SH" if instrument.startswith(("5", "6", "9")) else "SZ",
        "code": instrument,
        "kType": "1",
        "limit": str(PAGE_SIZE),
        "et": str(et),
    }
    response = session.get(API_URL, params=params, headers={"accept": "application/json", "token": key}, timeout=60)
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else 60.0
        raise RuntimeError(f"rate limited; retry after {wait:.0f}s")
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in (0, "0"):
        raise RuntimeError(f"iTick error: {payload.get('msg') or payload.get('code')}")
    frame = _parse_page(payload.get("data") or [], instrument)
    if frame.empty:
        return frame, None
    return frame, int(frame["date"].min().tz_localize(SHANGHAI).timestamp() * 1000)


def collect_one(session: requests.Session, key: str, instrument: str, checkpoint_dir: Path) -> pd.DataFrame:
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = checkpoint_dir / f"{instrument}.parquet"
    pages = []
    if checkpoint.exists():
        pages.append(pd.read_parquet(checkpoint))
        current_min = int(pd.to_datetime(pages[-1]["date"]).min().tz_localize(SHANGHAI).timestamp() * 1000)
        et = current_min - 60_000
        logger.info("resume %s from %s", instrument, _format_time(et))
    else:
        et = _epoch_ms(END)

    last_request = 0.0
    while True:
        if last_request:
            time.sleep(max(0.0, MIN_REQUEST_INTERVAL - (time.monotonic() - last_request)))
        while True:
            last_request = time.monotonic()
            try:
                frame, next_et = _request(session, key, instrument, et)
                break
            except RuntimeError as exc:
                if "rate limited" not in str(exc):
                    raise
                logger.warning("%s", exc)
                time.sleep(60.0)
        if frame.empty:
            break
        pages.append(frame)
        merged = pd.concat(pages, ignore_index=True).drop_duplicates("date").sort_values("date")
        merged.to_parquet(checkpoint, index=False)
        logger.info("%s: %d rows through %s", instrument, len(merged), merged["date"].min())
        if next_et is None or pd.Timestamp(next_et, unit="ms", tz=SHANGHAI).tz_localize(None) < START.replace(tzinfo=None):
            break
        et = next_et - 60_000

    result = pd.concat(pages, ignore_index=True).drop_duplicates("date").sort_values("date")
    result = result[(result["date"] >= START.replace(tzinfo=None)) & (result["date"] <= END.replace(tzinfo=None))]
    return result.reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", default="/home/ubuntu/.config/itick/api_key")
    parser.add_argument("--checkpoint-dir", default="/tmp/opencode/itick-minute-checkpoints")
    parser.add_argument("--output-dir", default="output/itick_minute")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key = Path(args.key_file).read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit("iTick API key is empty")

    checkpoint_dir = Path(args.checkpoint_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    for instrument in SYMBOLS:
        frame = collect_one(session, key, instrument, checkpoint_dir)
        if frame.empty:
            raise SystemExit(f"no data returned for {instrument}")
        frame.to_parquet(output_dir / f"{instrument}_1m_raw.parquet", index=False)
        logger.info("saved %s: %d rows, %s ~ %s", instrument, len(frame), frame["date"].min(), frame["date"].max())


if __name__ == "__main__":
    main()
