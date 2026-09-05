"""Standalone iTick watch-pool minute collector.

This module intentionally bypasses the warehouse minute pipeline. It only
fetches and appends raw iTick bars into output/itick_watchpool/.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

API_URL = "https://api-free.itick.org/stock/kline"
TZ = timezone(timedelta(hours=8))
DEFAULT_KEY_FILE = "/home/ubuntu/.config/itick/api_key"
DEFAULT_OUTPUT_DIR = Path("output/itick_watchpool")
DEFAULT_BATCH_SIZE = 4
DEFAULT_INTERVAL_SECONDS = 15.0
BAR_COLUMNS = ["date", "instrument", "open", "high", "low", "close", "volume", "amount"]
logger = logging.getLogger(__name__)


def _market(instrument: str) -> str:
    return "SH" if instrument.startswith(("5", "6", "9")) else "SZ"


def _normalize(code: str) -> str:
    value = str(code or "").strip().lower().replace(".", "")
    if value[:2] in {"sh", "sz"}:
        return value[2:]
    return value


def _key(path: str | None = None) -> str:
    candidate = path or os.getenv("ITICK_API_KEY_FILE", DEFAULT_KEY_FILE)
    value = Path(candidate).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"iTick API key is empty: {candidate}")
    return value


def _regular_bars(payload: dict, instrument: str) -> pd.DataFrame:
    rows = []
    for item in payload.get("data") or []:
        if not all(item.get(key) is not None for key in ("t", "o", "h", "l", "c", "v", "tu")):
            continue
        rows.append({
            "date": pd.to_datetime(item["t"], unit="ms", utc=True).tz_convert(TZ).tz_localize(None),
            "instrument": f"{instrument}.{_market(instrument)}",
            "open": float(item["o"]),
            "high": float(item["h"]),
            "low": float(item["l"]),
            "close": float(item["c"]),
            "volume": float(item["v"]),
            "amount": float(item["tu"]),
        })
    return pd.DataFrame(rows, columns=BAR_COLUMNS)


def _state_path(root: Path) -> Path:
    return root / "state.json"


def _load_state(root: Path) -> dict:
    path = _state_path(root)
    if not path.exists():
        return {"date": None, "cursor": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"date": None, "cursor": 0}


def _save_state(root: Path, state: dict) -> None:
    _state_path(root).write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")


def _append(root: Path, frame: pd.DataFrame, day: str) -> int:
    if frame.empty:
        return 0
    instrument = str(frame["instrument"].iloc[0]).upper().replace(".", "_")
    target = root / day / f"{instrument}_1m.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    old = pd.read_csv(target) if target.exists() else pd.DataFrame(columns=BAR_COLUMNS)
    merged = pd.concat([old, frame], ignore_index=True)
    merged["date"] = pd.to_datetime(merged["date"])
    merged = merged.drop_duplicates(["instrument", "date"], keep="last").sort_values("date")
    merged.to_csv(target, index=False, encoding="utf-8-sig")
    return len(frame)


def collect_watchpool_batch(
    codes: list[str],
    *,
    day: str | None = None,
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    batch_size: int = DEFAULT_BATCH_SIZE,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    key_file: str | None = None,
) -> dict:
    """Fetch one round-robin batch for one explicit trading date."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    target_day = day or datetime.now(TZ).strftime("%Y-%m-%d")
    unique = sorted({_normalize(code) for code in codes if _normalize(code)})
    if not unique:
        return {"ok": False, "reason": "empty_watchpool", "codes": 0, "rows": 0, "errors": {}}

    state = _load_state(root)
    if state.get("date") != target_day:
        state = {"date": target_day, "cursor": 0}
    size = max(1, min(int(batch_size), 5))
    selected = [unique[(int(state.get("cursor", 0)) + i) % len(unique)] for i in range(min(size, len(unique)))]
    state["cursor"] = (int(state.get("cursor", 0)) + len(selected)) % len(unique)
    _save_state(root, state)

    key = _key(key_file)
    session = requests.Session()
    result = {"ok": True, "date": target_day, "selected": selected, "codes": 0, "rows": 0, "errors": {}}
    for index, instrument in enumerate(selected):
        if index:
            time.sleep(max(0.0, float(interval_seconds)))
        try:
            end = datetime.strptime(target_day, "%Y-%m-%d").replace(hour=15, tzinfo=TZ)
            params = {
                "region": _market(instrument), "code": instrument, "kType": "1",
                "limit": "500", "et": str(int(end.timestamp() * 1000)),
            }
            response = session.get(API_URL, params=params, headers={"accept": "application/json", "token": key}, timeout=60)
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") not in (0, "0"):
                raise RuntimeError(payload.get("msg") or str(payload.get("code")))
            frame = _regular_bars(payload, instrument)
            if not frame.empty:
                frame = frame[frame["date"].dt.strftime("%Y-%m-%d") == target_day]
            result["rows"] += _append(root, frame, target_day)
            result["codes"] += 1
        except Exception as exc:  # noqa: BLE001 - independent collector records per-code failures
            result["errors"][instrument] = str(exc)
            logger.warning("iTick watch-pool fetch failed %s: %s", instrument, exc)
    return result


def collect_watchpool_once(*, day: str | None = None, **kwargs) -> dict:
    """Read the current unified watch pool and collect its next batch."""
    from StockInvestmentTool.warehouse.online import _default_observe_codes

    return collect_watchpool_batch(_default_observe_codes(), day=day, **kwargs)
